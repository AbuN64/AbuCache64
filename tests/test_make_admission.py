"""Make's public admission boundary, with process and admission seams mocked."""

import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from abucache import runtime, slots


def test_make_holds_grant_until_child_exit(monkeypatch):
    held = []

    @contextmanager
    def grant():
        held.append(True)
        try:
            yield 3, {slots.CORES_ENV: "3", "MAKEFLAGS": "-j3"}, ()
        finally:
            held.pop()

    def child(argv, *, env, pass_fds):
        assert held == [True]
        assert argv == ["make", "check", "_ABUCACHE_ADMITTED=1"]
        assert env[slots.CORES_ENV] == "3"
        assert "-j3" in env["MAKEFLAGS"] and "-j99" not in env["MAKEFLAGS"]
        return SimpleNamespace(returncode=7)

    monkeypatch.delenv(slots.CORES_ENV, raising=False)
    monkeypatch.setenv("JOBS", "99")
    monkeypatch.setenv("MAKEFLAGS", "-j99 -k")
    monkeypatch.setattr(runtime, "load", lambda: object())
    monkeypatch.setattr(slots, "configure", lambda value: None)
    monkeypatch.setattr(slots, "jobserver", grant)
    monkeypatch.setattr(slots.subprocess, "run", child)
    assert slots.run_make(["make", "check"]) == 7
    assert held == []


def test_refused_runtime_never_starts_make(monkeypatch):
    monkeypatch.delenv(slots.CORES_ENV, raising=False)
    monkeypatch.setenv("MAKEFLAGS", "")

    def refuse():
        raise runtime.RuntimeConfigError(Path("runtime.toml"), "resources.workers", "runtime refused")

    monkeypatch.setattr(runtime, "load", refuse)
    monkeypatch.setattr(slots.subprocess, "run", lambda *a, **kw: pytest.fail("unadmitted child"))
    with pytest.raises(runtime.RuntimeConfigError, match="runtime refused"):
        slots.run_make(["make"])


def test_inherited_jobserver_passes_descriptors_without_new_admission(monkeypatch):
    read, write = os.pipe()
    try:
        monkeypatch.delenv(slots.CORES_ENV, raising=False)
        flags = f"-j5 --jobserver-auth={read},{write}"
        monkeypatch.setenv("MAKEFLAGS", flags)
        monkeypatch.setattr(slots, "jobserver", lambda: pytest.fail("duplicate admission"))

        def child(argv, *, env, pass_fds):
            assert env["MAKEFLAGS"] == flags
            assert pass_fds == (read, write)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(slots.subprocess, "run", child)
        assert slots.run_make(["make"]) == 0
    finally:
        os.close(read)
        os.close(write)


@pytest.mark.parametrize("width", ["0", "-1", "junk", ""])
def test_nonpositive_inherited_width_refuses(monkeypatch, width):
    monkeypatch.setenv(slots.CORES_ENV, width)
    with pytest.raises(ValueError, match="positive"):
        slots.run_make(["make"])


def test_invalid_jobserver_refuses(monkeypatch, tmp_path):
    monkeypatch.delenv(slots.CORES_ENV, raising=False)
    with (tmp_path / "regular").open("w") as file:
        monkeypatch.setenv("MAKEFLAGS", f"--jobserver-auth={file.fileno()},{file.fileno()}")
        with pytest.raises(ValueError, match="pipe"):
            slots.run_make(["make"])


def test_make_arguments_cannot_override_grant():
    with pytest.raises(ValueError, match="slot admission"):
        slots.run_make(["make", "-j99"])


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["-j", "99", "-C", "already-applied", "-o", "old", "-W", "new", "check"], ["-o", "old", "-W", "new", "check"]),
        (["-kj99", "--jobs=99", "--directory=already-applied", "-f", "Other.mk"], ["-k", "-f", "Other.mk"]),
        (["-j", "check"], ["check"]),
        (["--", "-j", "filename"], ["--", "-j", "filename"]),
    ],
)
def test_parent_options_preserve_make_semantics(args, expected):
    assert slots._parent_options(args) == expected
