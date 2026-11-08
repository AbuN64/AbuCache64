# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 mo
"""The one runtime file of a host: cores, workers, memory, named capacities, the cache, the stage log
and the event feed.

`ABU_RUNTIME` names the file, else `~/.config/abu/runtime.toml`. Every key is required and an unknown key is an
error, so a typo never silently falls back to a default. A value the host can answer may say `"auto"`: cores,
workers, the three memory budgets and `cache.bytes` come from the host each time the file is loaded, the policy
numbers (`keep_seconds`, `log_bytes`, `slow_seconds`, `overrun_percent`) from `POLICY`. `provision` writes such a
file for a host that has none."""

from __future__ import annotations

import os
import shutil
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from abucache import files, keys

AUTO = "auto"
# What `"auto"` means for the policy keys, which no host can answer.
POLICY = {
    "cache.keep_seconds": 7 * 24 * 3600,
    "stages.log_bytes": 4 << 20,
    "events.slow_seconds": 60,
    "events.overrun_percent": 150,
}
# Shares of the usable memory: the parent command's budget and the reserve kept back from the task grants.
PARENT_SHARE, RESERVE_SHARE = 4, 8

_SCHEMA: dict[str, tuple[str, ...]] = {
    "resources": ("cores", "workers", "worker_memory", "parent_memory", "reserve_memory"),
    "cache": ("dir", "bytes", "keep_seconds"),
    "stages": ("log_bytes",),
    "events": ("feed", "slow_seconds", "overrun_percent"),
}


class RuntimeConfigError(Exception):
    """The runtime file is unreadable or wrong. `str(error)` starts with `file:dotted.key` (`file` alone when the
    whole file is at fault), then the reason."""

    def __init__(self, file: str, key: str, reason: str) -> None:
        self.file, self.key, self.reason = file, key, reason
        super().__init__(f"{file}:{key}: {reason}" if key else f"{file}: {reason}")


@dataclass(frozen=True)
class Runtime:
    cores: int
    workers: int
    worker_memory: int
    parent_memory: int
    reserve_memory: int
    capacities: Mapping[str, int]  # named capacities, e.g. {"repro": 1}
    cache_dir: Path
    cache_bytes: int
    cache_keep_seconds: int  # a cache root without git history keeps its entries live this long
    stage_log_bytes: int
    feed: Path  # the event feed the shared path writes slow stages, slot waits and queue time to
    slow_seconds: int  # a stage or wait this long (and over its expected time) is an event, running or closed
    overrun_percent: int  # "over its expected time": at least this percent of the previous command's wall
    digest: str  # keys.digest of every resolved value above


def _usable_memory() -> int:
    """Bytes this process may use: the cgroup limit when one is set, else the machine's RAM."""
    limit = Path("/sys/fs/cgroup/memory.max")
    text = limit.read_text(encoding="utf-8").strip() if limit.is_file() else "max"
    return int(text) if text.isdigit() else os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")


def _host_memory() -> int | None:
    """`_usable_memory`, or None when this host cannot say."""
    try:
        value = _usable_memory()
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def _auto(file: str, key: str, value: Any, derive: Any, *, minimum: int = 1) -> int:
    """An integer key that may say `"auto"`: the derived value, else the validated integer."""
    return max(minimum, derive()) if value == AUTO else _int(file, key, value, minimum=minimum)


def _free_bytes(directory: Path) -> int:
    """Free bytes of the filesystem `directory` will live on (its nearest existing parent)."""
    existing = directory
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    return shutil.disk_usage(existing).free


def _int(file: str, key: str, value: Any, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeConfigError(file, key, "must be an integer")
    if value < minimum:
        raise RuntimeConfigError(file, key, f"must be at least {minimum}")
    return value


def default_path() -> Path:
    """The runtime file this process reads: `ABU_RUNTIME`, else `~/.config/abu/runtime.toml`."""
    env = os.environ.get("ABU_RUNTIME")
    return Path(env) if env else Path.home() / ".config" / "abu" / "runtime.toml"


def load(path: Path | None = None) -> Runtime:
    """Read and resolve the runtime file. `"auto"` for `cores` is the CPUs this process may run on; for `workers`
    it is the smaller of that and `cores`, limited by memory: (usable memory - parent_memory) // worker_memory,
    at least 1."""
    path = path or default_path()
    file = str(path)
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise RuntimeConfigError(file, "", f"cannot be read: {error.strerror or error}") from error
    except tomllib.TOMLDecodeError as error:
        raise RuntimeConfigError(file, "", f"is not valid TOML: {error}") from error
    for section in (*_SCHEMA, "capacities"):
        if not isinstance(document.get(section), dict):
            raise RuntimeConfigError(file, section, "is missing")
    for section in document:
        if section not in _SCHEMA and section != "capacities":
            raise RuntimeConfigError(file, section, "is not a runtime section")
    for section, names in _SCHEMA.items():
        for name in names:
            if name not in document[section]:
                raise RuntimeConfigError(file, f"{section}.{name}", "is missing")
        for name in document[section]:
            if name not in names:
                raise RuntimeConfigError(file, f"{section}.{name}", "is not a runtime key")
    res, cache, stages = document["resources"], document["cache"], document["stages"]
    events = document["events"]
    memory_keys = ("worker_memory", "parent_memory", "reserve_memory")
    autos = [f"resources.{name}" for name in memory_keys if res[name] == AUTO]
    usable = _host_memory() if autos or res["workers"] == AUTO else 0
    if usable is None:
        raise RuntimeConfigError(
            file, ", ".join(autos or ["resources.workers"]), "cannot be derived: this host does not report its memory"
        )
    affinity = len(os.sched_getaffinity(0))
    cores = _auto(file, "resources.cores", res["cores"], lambda: affinity)
    worker_memory = _auto(file, "resources.worker_memory", res["worker_memory"], lambda: usable // cores)
    parent_memory = _auto(
        file, "resources.parent_memory", res["parent_memory"], lambda: usable // PARENT_SHARE, minimum=0
    )
    reserve_memory = _auto(
        file, "resources.reserve_memory", res["reserve_memory"], lambda: usable // RESERVE_SHARE, minimum=0
    )
    if res["workers"] == AUTO:
        workers = min(affinity, cores, max(1, (usable - parent_memory) // worker_memory))
    else:
        workers = _int(file, "resources.workers", res["workers"])
        if workers > cores:
            raise RuntimeConfigError(file, "resources.workers", "exceeds cores")
    capacities = {name: _int(file, f"capacities.{name}", value) for name, value in document["capacities"].items()}
    directory = cache["dir"]
    if not isinstance(directory, str) or not Path(directory).is_absolute():
        raise RuntimeConfigError(file, "cache.dir", "must be an absolute path")
    feed = events["feed"]
    if not isinstance(feed, str) or not Path(feed).is_absolute():
        raise RuntimeConfigError(file, "events.feed", "must be an absolute path")
    def policy(section: dict[str, Any], name: str, key: str, **kw: int) -> int:
        return _auto(file, key, section[name], lambda: POLICY[key], **kw)

    values: dict[str, Any] = {
        "cores": cores,
        "workers": workers,
        "worker_memory": worker_memory,
        "parent_memory": parent_memory,
        "reserve_memory": reserve_memory,
        "capacities": MappingProxyType(capacities),
        "cache_dir": Path(directory),
        "cache_bytes": _auto(file, "cache.bytes", cache["bytes"], lambda: _free_bytes(Path(directory)) // 2),
        "cache_keep_seconds": policy(cache, "keep_seconds", "cache.keep_seconds"),
        "stage_log_bytes": policy(stages, "log_bytes", "stages.log_bytes"),
        "feed": Path(feed),
        "slow_seconds": policy(events, "slow_seconds", "events.slow_seconds"),
        "overrun_percent": policy(events, "overrun_percent", "events.overrun_percent", minimum=100),
    }
    return Runtime(**values, digest=keys.digest(values))


def provision(path: Path | None = None) -> tuple[Path, bool]:
    """Make sure the host has a runtime file: an existing file is left untouched, otherwise one is written whose every
    number is `"auto"` (derived from the host at each load) and whose paths come from the user's cache and state
    homes. Returns (file, written). A host that cannot answer is refused once, naming every key the operator must
    write by hand in that file."""
    path = path or default_path()
    if path.exists():
        return path, False
    missing: list[str] = []
    if _host_memory() is None:
        missing += [f"resources.{name}" for name in ("worker_memory", "parent_memory", "reserve_memory")]
    try:
        home = Path.home()
    except RuntimeError:
        home = None
    cache_home = Path(os.environ.get("XDG_CACHE_HOME") or (home / ".cache" if home else ""))
    state_home = Path(os.environ.get("XDG_STATE_HOME") or (home / ".local" / "state" if home else ""))
    if not cache_home.is_absolute():
        missing.append("cache.dir")
    if not state_home.is_absolute():
        missing.append("events.feed")
    if missing:
        raise RuntimeConfigError(
            str(path), ", ".join(missing), "cannot be derived on this host: the operator must provide it"
        )
    text = f"""[resources]
cores = "auto"
workers = "auto"
worker_memory = "auto"
parent_memory = "auto"
reserve_memory = "auto"

[capacities]

[cache]
dir = "{cache_home / "abu" / "cache"}"
bytes = "auto"
keep_seconds = "auto"

[stages]
log_bytes = "auto"

[events]
feed = "{state_home / "abu" / "feed.jsonl"}"
slow_seconds = "auto"
overrun_percent = "auto"
"""
    files.write(path, text.encode())
    load(path)
    return path, True


def main(argv: list[str]) -> int:
    """`python -m abucache.runtime provision`: print the runtime file's path and whether it was just written."""
    if argv != ["provision"]:
        print("usage: python -m abucache.runtime provision", file=sys.stderr)
        return 2
    try:
        path, written = provision()
    except RuntimeConfigError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"{'wrote' if written else 'kept'} {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
