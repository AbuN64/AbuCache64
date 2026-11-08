# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 mo
"""Fixtures for the slot tests: declared storage of their own and a runtime file made from a few numbers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from abucache import runtime, slots

TEXT = """
[resources]
cores = {cores}
workers = {workers}
worker_memory = 1073741824
parent_memory = 0
reserve_memory = {reserve}

[capacities]
{capacities}

[cache]
dir = "{cache}"
bytes = 1000000
keep_seconds = 3600

[stages]
log_bytes = 4096

[events]
feed = "{feed}"
slow_seconds = {slow}
overrun_percent = 150
"""

ADMITTED = "16"  # the JOBS every test runs under unless it sets its own: wider than any fixture's cores


@pytest.fixture(autouse=True)
def _admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every width abucache resolves comes from the admitted JOBS (runtime.admitted_jobs); a test is admitted."""
    monkeypatch.setenv("JOBS", ADMITTED)


@pytest.fixture
def rundir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Declared storage of this test alone, with no cores or capacity inherited from a holder."""
    directory = tmp_path / "storage"
    directory.mkdir()
    transient = tmp_path / "run"
    transient.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(transient))
    path = tmp_path / "runtime.toml"
    path.write_text(
        TEXT.format(cores=8, workers=4, reserve=0, capacities="", cache=directory,
                    feed=tmp_path / "feed.jsonl", slow=60)
    )
    monkeypatch.setenv("ABU_RUNTIME", str(path))
    monkeypatch.setattr(slots, "default", slots.Slots(runtime.load(path)))
    monkeypatch.delenv(slots.CORES_ENV, raising=False)
    monkeypatch.delenv(slots.CAPACITIES_ENV, raising=False)
    return directory


@pytest.fixture
def make_runtime(tmp_path: Path, rundir: Path) -> Callable[..., runtime.Runtime]:
    def make(
        workers: int = 4, capacities: dict[str, int] | None = None, reserve: int = 0, slow: int = 60
    ) -> runtime.Runtime:
        lines = "\n".join(f"{name} = {size}" for name, size in (capacities or {}).items())
        path = tmp_path / "runtime.toml"
        path.write_text(
            TEXT.format(
                cores=max(workers, 8),
                workers=workers,
                reserve=reserve,
                capacities=lines,
                cache=rundir,
                feed=tmp_path / "feed.jsonl",
                slow=slow,
            )
        )
        return runtime.load(path)

    return make
