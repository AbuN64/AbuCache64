# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 mo
"""Class and resource policy uses the host pool, with signal-driven release."""

import json
import os
import threading
from unittest import mock

import pytest

from abucache import slots


@pytest.fixture
def admission(rundir, monkeypatch):
    result = slots.Admission(slots.Policy(0, 4, 40, 0.75, 5, 2))
    monkeypatch.setattr(result, "_mem_gib", lambda field: 128)
    monkeypatch.setattr(result, "disk_free_gib", lambda: 1024)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(8)))
    monkeypatch.delenv(slots.CORES_ENV, raising=False)
    return result


def test_class_order_and_fifo_share_the_native_queue(admission):
    pool = admission.pool_dir()
    handles = []
    try:
        for rank, stamp in [(3, 1), (2, 2), (1, 3), (0, 4), (2, 5)]:
            ticket = pool / f"admit-{rank}-{stamp:020d}-{os.getpid()}-0.lock"
            handles.append(slots._try(ticket))
        # Existing native queue participates at batch priority.
        native = pool / f"wait-1{4:020d}-{os.getpid()}"
        native.touch()
        queue = slots.Slots._queue(pool)
        assert [slots._ticket_order(p)[:2] for p in queue] == [(0, 4), (1, 3), (2, 2), (2, 4), (2, 5), (3, 1)]
        native.unlink()
    finally:
        for handle in handles:
            handle.close()


def test_disk_and_memory_refuse_before_queue_or_work(admission, monkeypatch):
    pool = admission.pool_dir()
    monkeypatch.setattr(admission, "disk_free_gib", lambda: 70)
    with pytest.raises(slots.AdmissionError, match="disk_floor"), admission.hold("batch", 2):
        pytest.fail("work started without disk")
    assert not list(pool.glob("admit-*"))
    monkeypatch.setattr(admission, "disk_free_gib", lambda: 1024)
    monkeypatch.setattr(admission, "_mem_gib", lambda field: 8 if field == "MemAvailable" else 128)
    with pytest.raises(slots.AdmissionError, match="memory"), admission.hold("batch", mem_gib=5):
        pytest.fail("work started without memory")
    assert not list(pool.glob("admit-*"))
    monkeypatch.setattr(os, "sched_setaffinity", lambda *args: None)
    with admission.hold("owner", mem_gib=5) as grant:
        assert grant.weight == 1


def test_memory_reservation_is_atomic_between_grants(admission, monkeypatch):
    monkeypatch.setattr(admission, "_mem_gib", lambda field: 14 if field == "MemAvailable" else 128)
    first = admission.acquire("batch", mem_gib=5)
    try:
        with pytest.raises(slots.AdmissionError, match="memory"):
            admission.acquire("batch", mem_gib=5.5)
    finally:
        first.release()
    second = admission.acquire("batch", mem_gib=5.5)
    second.release()


@pytest.mark.parametrize("fail", [False, True])
def test_completion_and_failure_release_slots_and_affinity(admission, monkeypatch, fail):
    affinity = mock.Mock()
    monkeypatch.setattr(os, "sched_setaffinity", affinity)
    try:
        with admission.hold("batch", 2) as grant:
            assert grant.numbers == {0, 1}
            assert slots._busy(admission.pool_dir() / "slot-0.lock")
            with admission.hold("background") as nested:
                assert nested is None
            if fail:
                raise RuntimeError("work failed")
    except RuntimeError as error:
        assert fail and str(error) == "work failed"
    assert not slots._busy(admission.pool_dir() / "slot-0.lock")
    assert not list(admission.pool_dir().glob("holder-*"))
    assert affinity.call_args.args == (0, set(range(8)))


def test_pin_failure_also_releases(admission, monkeypatch):
    monkeypatch.setattr(os, "sched_setaffinity", mock.Mock(side_effect=OSError("pin refused")))
    with pytest.raises(OSError, match="pin refused"), admission.hold("batch"):
        pytest.fail("work started without affinity")
    assert not slots._busy(admission.pool_dir() / "slot-0.lock")


def test_release_wakes_a_waiter_without_a_timer(admission, monkeypatch):
    monkeypatch.setattr(admission, "capacity", lambda: 1)
    first = admission.acquire("batch")
    sleeping = threading.Event()
    granted = threading.Event()
    real_select = slots.select.select

    def select_on_release(read, write, error, interval=None):
        assert interval is None
        sleeping.set()
        return real_select(read, write, error, interval)

    monkeypatch.setattr(slots.select, "select", select_on_release)

    def work():
        second = admission.acquire("batch")
        granted.set()
        second.release()

    thread = threading.Thread(target=work)
    thread.start()
    sleeping.wait()
    assert not granted.is_set()
    first.release()
    granted.wait()
    thread.join()


def test_native_slots_and_class_admission_use_identical_locks(admission, make_runtime):
    native = slots.Slots(make_runtime(workers=2))
    with native.cores(2) as count:
        assert count == 2
        grant = admission.acquire("interactive")
        try:
            assert grant.numbers == {2}
        finally:
            grant.release()


def test_declared_values_validate_and_stamp_width_uses_them(admission):
    with pytest.raises(ValueError):
        slots.Policy(0, 0, 0, 0, 0, 0)
    with pytest.raises(ValueError):
        admission.configure({"disk_gib": -1})
    admission.configure({"stamp_jobs": 4, "stamp_gib": 32})
    assert admission.stamp_slots() == 2
    assert admission.stamp_weight("batch") == 4
    assert admission.stamp_weight("owner") == 8
    assert admission.policy.disk_gib == 40


def test_holder_metadata_exposes_the_declared_memory_and_pid(admission):
    grant = admission.acquire("background", label="work", mem_gib=5)
    try:
        assert json.loads(grant.holder.read_text()) == {
            "pid": os.getpid(),
            "label": "work",
            "memory": 5,
            "klass": "background",
            "slots": [0],
        }
        assert slots._admission_pids(admission.pool_dir()) == [os.getpid()]
    finally:
        grant.release()


def test_real_waiters_grant_in_class_order(admission, monkeypatch):
    monkeypatch.setattr(admission, "capacity", lambda: 1)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0})
    first = admission.acquire("batch")
    queued = {klass: threading.Event() for klass in slots.CLASSES}
    order = []
    real_until = slots._until

    def until(directory, attempt, **kwargs):
        queued[threading.current_thread().name].set()
        return real_until(directory, attempt, **kwargs)

    monkeypatch.setattr(slots, "_until", until)

    def work(klass):
        grant = admission.acquire(klass)
        order.append(klass)
        grant.release()

    threads = []
    for klass in reversed(slots.CLASSES):
        thread = threading.Thread(target=work, args=(klass,), name=klass)
        threads.append(thread)
        thread.start()
        queued[klass].wait()
    first.release()
    for thread in threads:
        thread.join()
    assert order == list(slots.CLASSES)


def test_status_exposes_live_holders_to_consumers(admission):
    grant = admission.acquire("batch", 2, "build")
    try:
        status = admission.status()
        assert status["used"] == 2
        assert status["queue"] == []
        assert status["holders"] == [
            {"pid": os.getpid(), "klass": "batch", "label": "build", "memory": 1.5, "slots": [0, 1]}
        ]
    finally:
        grant.release()
    assert admission.status()["holders"] == []
