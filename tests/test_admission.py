# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 mo
"""Class and resource policy uses the host pool, with signal-driven release."""

import json
import os
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from abucache import slots


@pytest.fixture
def admission(rundir, monkeypatch):
    result = slots.Admission(slots.Policy(0, 4, 40, 0.75, 5, 2))
    monkeypatch.setattr(result, "_mem_gib", lambda field: 128)
    monkeypatch.setattr(result, "disk_free_gib", lambda output_dir: 1024)
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
    monkeypatch.setattr(admission, "disk_free_gib", lambda output_dir: 70)
    with pytest.raises(slots.AdmissionError, match="disk_floor"), admission.hold("batch", 2, output_dir=Path.cwd()):
        pytest.fail("work started without disk")
    assert not list(pool.glob("admit-*"))
    monkeypatch.setattr(admission, "disk_free_gib", lambda output_dir: 1024)
    monkeypatch.setattr(admission, "_mem_gib", lambda field: 8 if field == "MemAvailable" else 128)
    with pytest.raises(slots.AdmissionError, match="memory"), admission.hold("batch", mem_gib=5, output_dir=Path.cwd()):
        pytest.fail("work started without memory")
    assert not list(pool.glob("admit-*"))
    monkeypatch.setattr(os, "sched_setaffinity", lambda *args: None)
    with admission.hold("owner", mem_gib=5, output_dir=Path.cwd()) as grant:
        assert grant.weight == 1


def test_memory_reservation_is_atomic_between_grants(admission, monkeypatch):
    monkeypatch.setattr(admission, "_mem_gib", lambda field: 14 if field == "MemAvailable" else 128)
    first = admission.acquire("batch", mem_gib=5, output_dir=Path.cwd())
    try:
        with pytest.raises(slots.AdmissionError, match="memory"):
            admission.acquire("batch", mem_gib=5.5, output_dir=Path.cwd())
    finally:
        first.release()
    second = admission.acquire("batch", mem_gib=5.5, output_dir=Path.cwd())
    second.release()


@pytest.mark.parametrize("fail", [False, True])
def test_completion_and_failure_release_slots_and_affinity(admission, monkeypatch, fail):
    affinity = mock.Mock()
    monkeypatch.setattr(os, "sched_setaffinity", affinity)
    try:
        with admission.hold("batch", 2, output_dir=Path.cwd()) as grant:
            assert grant.numbers == {0, 1}
            assert slots._busy(admission.pool_dir() / "slot-0.lock")
            with admission.hold("background", output_dir=Path.cwd()) as nested:
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
    with pytest.raises(OSError, match="pin refused"), admission.hold("batch", output_dir=Path.cwd()):
        pytest.fail("work started without affinity")
    assert not slots._busy(admission.pool_dir() / "slot-0.lock")


def test_release_wakes_a_waiter_without_a_timer(admission, monkeypatch):
    monkeypatch.setattr(admission, "capacity", lambda: 1)
    first = admission.acquire("batch", output_dir=Path.cwd())
    sleeping = threading.Event()
    granted = threading.Event()
    real_select = slots.select.select

    def select_on_release(read, write, error, interval=None):
        assert interval is None
        sleeping.set()
        return real_select(read, write, error, interval)

    monkeypatch.setattr(slots.select, "select", select_on_release)

    def work():
        second = admission.acquire("batch", output_dir=Path.cwd())
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
        grant = admission.acquire("interactive", output_dir=Path.cwd())
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
    grant = admission.acquire("background", label="work", mem_gib=5, output_dir=Path.cwd())
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
    first = admission.acquire("batch", output_dir=Path.cwd())
    queued = {klass: threading.Event() for klass in slots.CLASSES}
    start = threading.Event()
    order = []
    real_until = slots._until

    def until(directory, attempt, **kwargs):
        queued[threading.current_thread().name].set()
        start.wait()  # all tickets are published before any waiter takes a queue snapshot
        return real_until(directory, attempt, **kwargs)

    monkeypatch.setattr(slots, "_until", until)

    def work(klass):
        grant = admission.acquire(klass, output_dir=Path.cwd())
        order.append(klass)
        grant.release()

    threads = []
    for klass in reversed(slots.CLASSES):
        thread = threading.Thread(target=work, args=(klass,), name=klass)
        threads.append(thread)
        thread.start()
        queued[klass].wait()
    first.release()
    start.set()
    for thread in threads:
        thread.join()
    assert order == list(slots.CLASSES)


def test_status_exposes_live_holders_to_consumers(admission):
    grant = admission.acquire("batch", 2, "build", output_dir=Path.cwd())
    try:
        status = admission.status(output_dir=Path.cwd())
        assert status["used"] == 2
        assert status["queue"] == []
        assert status["holders"] == [
            {"pid": os.getpid(), "klass": "batch", "label": "build", "memory": 1.5, "slots": [0, 1]}
        ]
    finally:
        grant.release()
    assert admission.status(output_dir=Path.cwd())["holders"] == []


def test_ticket_publication_cannot_be_deleted_by_a_competing_census(admission, monkeypatch):
    pool = admission.pool_dir()
    real_try = slots._try
    prepared = []

    def competing(path):
        if path.name.startswith(".admit-"):
            path.touch()  # exact interleaving: file exists before flock is acquired
            assert slots.Slots._queue(pool) == []
            assert path.exists()  # a queue census cannot unlink the unpublished inode
            prepared.append(path)
        return real_try(path)

    monkeypatch.setattr(slots, "_try", competing)
    grant = admission.acquire("batch", output_dir=Path.cwd())
    try:
        assert len(prepared) == 1
        assert not prepared[0].exists()
        assert admission.status(output_dir=Path.cwd())["holders"][0]["slots"] == [0]
    finally:
        grant.release()


@pytest.mark.parametrize(
    ("weight", "free_gib", "accepted"),
    [(1, 4.7, False), (1, 40, True), (1, 70, True), (2, 70, False), (2, 80, True)],
)
def test_disk_floor_counts_only_requested_slots(admission, monkeypatch, weight, free_gib, accepted):
    """The 09XN host had 4.7 GiB free and a 40 GiB floor per requested slot."""
    free_bytes = int(free_gib * 2**30)
    monkeypatch.setattr(admission, "disk_free_gib", lambda output_dir: free_bytes / 2**30)
    if accepted:
        grant = admission.acquire("batch", weight, output_dir=Path.cwd())
        try:
            assert grant.weight == weight
            assert len(grant.numbers) == weight
        finally:
            grant.release()
    else:
        with pytest.raises(slots.AdmissionError) as caught:
            admission.acquire("batch", weight, output_dir=Path.cwd())
        assert caught.value.key == "slots.disk_floor"
        assert f"root={Path.cwd()}" in caught.value.detail
        assert f"free={free_bytes} bytes" in caught.value.detail
        assert f"needed={40 * weight * 2**30} bytes for {weight} slots" in caught.value.detail
        assert not list(admission.pool_dir().glob("admit-*"))
        assert not list(admission.pool_dir().glob("holder-*"))
        assert not list(admission.pool_dir().glob("slot-*"))


def test_admission_measures_declared_storage_not_runtime_tmpfs(rundir, make_runtime, monkeypatch, tmp_path):
    runtime = make_runtime()
    admission = slots.Admission(slots.Policy(0, 4, 40, 0.75, 5, 2), runtime=runtime)
    transient = tmp_path / "runtime-tmpfs"
    transient.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(transient))
    measured = []

    def statvfs(path):
        measured.append(path)
        # The configured storage fits one slot; the runtime tmpfs does not.
        return SimpleNamespace(
            f_bavail=40 * 2**30 if path == Path.cwd() else int(4.7 * 2**30),
            f_frsize=1,
        )

    monkeypatch.setattr(os, "statvfs", statvfs)
    admission.check_disk("batch", 1, output_dir=Path.cwd())
    assert measured == [Path.cwd()]
    assert not list(transient.iterdir())
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert admission.pool_dir() == rundir / slots.POOL
    assert slots.wait_log().parent == admission.pool_dir()


def test_missing_storage_declaration_does_not_fall_back_to_runtime_tmpfs(rundir, monkeypatch, tmp_path):
    monkeypatch.setattr(slots, "default", slots.Slots())
    monkeypatch.setenv("ABU_RUNTIME", str(tmp_path / "missing.toml"))
    admission = slots.Admission(slots.Policy(0, 4, 40, 0.75, 5, 2))
    with pytest.raises(slots.runtime_file.RuntimeConfigError):
        admission.acquire("batch", output_dir=Path.cwd())
    assert not list((tmp_path / "run").iterdir())


def test_relative_storage_root_is_refused(make_runtime):
    runtime = make_runtime()
    admission = slots.Admission(
        slots.Policy(0, 4, 40, 0.75, 5, 2),
        runtime=replace(runtime, cache_dir=runtime.cache_dir.relative_to(runtime.cache_dir.parent)),
    )
    with pytest.raises(ValueError, match="absolute storage path"):
        admission.pool_dir()


def test_output_filesystem_admission_and_refusal(admission, monkeypatch, tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    pool = admission.pool_dir()
    measured = []
    free = 491

    def statvfs(path):
        measured.append(path)
        return SimpleNamespace(f_bavail=int((free if path == output else 4.7) * 2**30), f_frsize=1)

    monkeypatch.setattr(os, "statvfs", statvfs)
    monkeypatch.setattr(admission, "disk_free_gib", slots.Admission.disk_free_gib.__get__(admission))
    grant = admission.acquire("batch", output_dir=output)
    grant.release()
    assert measured == [output, output]
    assert pool not in measured
    free = 4.7
    with pytest.raises(slots.AdmissionError, match="slots.disk_floor") as caught:
        admission.acquire("batch", output_dir=output)
    assert str(output) in caught.value.detail
    assert not list(pool.glob("admit-*"))
    with pytest.raises(slots.AdmissionError, match="output directory is required"):
        admission.acquire("batch")


def test_output_directory_before_creation(admission, monkeypatch, tmp_path):
    monkeypatch.setattr(admission, "disk_free_gib", slots.Admission.disk_free_gib.__get__(admission))
    with mock.patch.object(os, "statvfs", return_value=SimpleNamespace(f_bavail=491, f_frsize=2**30)) as stat:
        admission.check_disk("batch", 1, output_dir=tmp_path / "new" / "run")
    stat.assert_called_once_with(tmp_path)
    assert not (tmp_path / "new").exists()


def test_nested_hold_requires_output_directory(admission, monkeypatch):
    monkeypatch.setenv(slots.CORES_ENV, "1")
    with pytest.raises(slots.AdmissionError, match="output directory is required"), admission.hold("batch"):
        pytest.fail("nested work must name its output")


def test_capacity_and_the_owner_limit_are_at_most_the_admitted_jobs(admission, monkeypatch, tmp_path):
    assert admission.capacity() == 8  # JOBS 16: the eight CPUs bound it
    monkeypatch.setenv("JOBS", "3")
    assert admission.capacity() == 3  # the admitted width, never the host's eight
    with pytest.raises(slots.AdmissionError, match="slots.capacity"):
        admission.acquire("owner", weight=4, output_dir=tmp_path)  # the owner limit is JOBS too, not the affinity
    monkeypatch.delenv("JOBS")
    with pytest.raises(slots.runtime_file.RuntimeConfigError):
        admission.capacity()
