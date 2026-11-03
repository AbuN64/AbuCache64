# AbuCache64

A Python package that holds the fast paths of an N64 decompilation build. unbake imports it and drives it from its own worker pool.

- `abucache.compile` caches a unit compile by content. The key is the preprocessed bytes, the exact commands and the hash of the tool files. Every ROM version whose input is identical shares one compile.
- `abucache.link` filters the symbol file down to what the object references, builds the linker script and commands, and links with ld and one objcopy per section. Nothing reads the ROM.
- `abucache.store` is the content cache that unbake keeps its step results in. A command only records what it used. The collect of unused entries runs in its own detached process, so no command waits on it.
- `abucache.runtime`, `abucache.keys`, `abucache.files`, `abucache.proc` and `abucache.slice` are the base every tool shares. `runtime` reads the host's one runtime file (cores, workers, memory, named capacities, cache, stage log and event feed). `keys` holds the canonical digest, the code identity of a producer and the `Key` of a stored result. `files` writes atomically and locks with a deadline. `proc` runs a child in its own process group with exact accounting. `slice` moves a session command into its systemd slice and leaves services where their unit put them.
- `abucache.stages`, `abucache.memo` and `abucache.events` are the one timing path, the command's memo and the one event writer. `stages` records every command, stage, native child and pool dispatch as a `stage/1` row in the stage log and notes long lock and slot waits in the host's wait log, so a slow or stuck job is seen without per-tool code. Every command also binds itself to the runtime file's `[events]` section (`abucache.feed`): a stage or wait past `slow_seconds` and `overrun_percent` of its last wall is written to the event feed twice, once while it still runs (a watchdog thread) and once when it closes. No tool calls anything for this or for the wait log. A caller that needs a stage's duration (a session's elapsed report, a capture helper) never reads a clock: `with stages.timed(name) as timing` yields a `Timing` whose `seconds` is the monotonic time so far while the block runs and its final wall after, with `running`, `status` and `recorded`. Inside a command it is the same stage `stages.stage` opens; outside one (another thread, no command) it is measured and not recorded, so each thread and session owns its own `Timing`. An exception ends the block with its status and propagates. unbake keeps no stage tree of its own: its profile, pool records and wait notes are these rows, and it adds only its niceness, its hold beat and the binding of the host config (`stages.configure`). `memo` holds a command's pure values and trims them under memory pressure. `events` writes and reads the append-only event feed (schema 2) and ships its JSON schema.
- `abucache.slots` is the host's one set of worker slots: flock files under `$XDG_RUNTIME_DIR/unbake-pool-2` that every command of every tool divides fairly (`cores`, the make `jobserver` and the `run` command body). A holder may name `task_memory`, so its grant never exceeds the tasks the free memory holds, and a named capacity (`[capacities]` in the runtime file) is a second token class for something the host has only a few of. Waits and short shares are stages of the command's tree. Waiters for a core slot and for a named capacity are served first come, first served. The priority job (the ship/land lock holder) is an explicit field, never an environment variable: `Slots(priority=True)`, `slots.configure(runtime, priority=True)` or `python -m abucache.slots --priority -- CMD`; it splits the budget first, leaves the rest to the others (down to none) and queues ahead of every waiter. A command with at most `EXPRESS_AT` (2) chunks takes the express slot (`slot-express.lock`, one beyond the budget) when every slot is held, so a small command never queues behind heavy ones. A wait of a minute or more is one line of the host's `waits.jsonl` naming the `project` that waited (`stages.configure(project=...)`).
- `abucache.pool` is the one forked executor (`map`, `gather`): workers fork once per command, keyed results are stored through `store.Namespace` with a sealed `<stage>.group` entry, and a tool plugs in through `register_shared`, `register_context`, `register_passthrough`, `register_warm` and `carry`. A failure is `TaskFailed(kind, stage, index, reason, notes)` with kind crash, memory or stalled.
- `abucache.store` also has `Namespace(cache, tool, kind)`, the key space of one tool's results (`"<tool>.<kind>:<key>"`), and `pack`/`unpack`, the stored form of an object. A root with no git history keeps its entries live for the runtime's `keep_seconds`.
- `abucache.require(1)` is the call every tool makes at import: it fails fast when the installed abucache speaks another contract.

> This is a personal tool. It is not officially maintained and may change or break without notice.

## Install

```sh
pip install git+https://github.com/AbuN64/cache-64
```

## How the compile cache stays honest

abucache knows no compiler. The caller gives the commands and the tool files. The commands run in a private directory that holds only the input under its own base name. A result that still holds a path of the run is returned but never stored.

## Trace

`abucache.trace()` prints one line per step to stderr. Each line holds the exact argv, and the compile line says whether the cache hit.

## Development

Run `ci/check`.

## License

Released under [GNU GPL version 3 or later](LICENSE).

## One owner per cache

The tools that use abucache (CakeUnbake64, AbuDecomp64, CakeTaste64, RecipeMaker64 and the game repositories unbake
generates) run `ci/abucache-audit` in their check entry point: a new cache, slot, pool, executor or timing path outside
abucache is refused with this package named as its owner.

Class admission uses `abucache.slots.Admission(Policy(...))`, on the same slot
files as `Slots`. All six Policy fields are caller-declared: `reserve_cores`,
`floor_gib`, `disk_gib`, `rss_gib`, `stamp_gib`, `stamp_jobs`. `hold(class, weight,
label, mem_gib=...)` guards work, releases on every exit and pins the calling
thread and its children to the granted cores. `admitted(class)` guards functions.
Classes order owner, interactive, batch, background, with FIFO within each class.
Owner may use reserve cores and bypass resource floors. Other requests refuse
before work if their declared memory or disk cannot fit. Waits use queue/release
notifications and holder process exit; no retry duration controls completion.

`python -m abucache.slots --make make <targets...>` (or `slots.run_make`) admits a generated make invocation.
It requires a valid runtime for fresh admission, preserves a live inherited pipe/FIFO jobserver, and holds its
positive grant until the child exits. `JOBS` and raw make width flags do not grant slots. Closed jobserver descriptors
and invalid inherited widths refuse rather than silently creating another parallel build.

Generated make uses `--make-parent <pid>` to retain its original argument list, including options GNU make omits
from MAKEFLAGS. Directory changes are already applied; unadmitted width flags are removed. The generated delegate
preserves MAKELEVEL so existing top-level maintenance and recursive build behavior keep their meaning.
