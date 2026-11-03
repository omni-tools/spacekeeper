# Spacekeeper

Capture and restore macOS fullscreen window arrangements through yabai.

**Status: snapshot/identity/planner foundation, yabai adapter, internal window transitions, Click command scaffold, and working prototypes. CLI capture/list/restore behavior is not implemented yet.**

The first version will save named snapshots of the running macOS session and restore fullscreen states and original monitors. YAML presets, flexible app/title matching, preserve filters, reopening missing windows, and scripting-addition-assisted Space ordering are planned for later versions.

## Start here

- [Agreed design and v0 acceptance criteria](docs/design.md)
- [Prototype scripts and limitations](prototypes/README.md)
- [Future scripting addition and Space ordering](docs/draft/HANDOFF.yabai-sa-and-space-reordering.md)
- Local implementation takeover instructions: `private/HANDOFF.md` (intentionally ignored)

## Development setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/):

```sh
uv sync --locked
uv run spacekeeper --help
uv run spacekeeper capture --help
uv run spacekeeper restore --help
```

uv creates a local `.venv`. The Click scaffold provides `capture [NAME] [--force]`,
`list`, `restore [NAME]`, and `--version`. Capture/list/restore currently
exit with an explicit not-implemented error and do not query or change the desktop.
You can also run `uv run python -m spacekeeper --help`.

Build the wheel and source distribution with `uv build`.

## Backend foundation

`snapshot.py` validates observations and their JSON representation, including
display UUIDs, Space membership, window metadata, and session/process identity.
Additional metadata can retain raw yabai query data for later capabilities.
`identity.py` reads the caller's boot and graphical login session plus process
creation timestamps, without querying or changing desktop windows.
`planner.py` compares two observations and returns window targets, reasons for
skips or blocked work, monitor fallbacks, and final visibility/focus targets.
It performs no commands.

`storage.py` saves named observations atomically under `snapshots/`, with private,
verified pre-restore files in `backups/` and matching results in `reports/` under
the chosen state directory. Default captures replace automatically; other names
require an explicit overwrite. Listing and loading do not create storage.

`process.py` runs shell-free argument arrays with command deadlines and a combined
stdout/stderr limit. `yabai.py` uses that injectable runner for parallel read-only
queries, retries changing observations, and rechecks session/window ownership
before window operations. A move selects a normal Space by stable ID, then
resolves its current index immediately before the command. Capability failures
are reported without attempting scripting-addition setup.

`transitions.py` implements the internal, sequential per-window engine: confirm
fullscreen exit, move to the target monitor, confirm focus there, then enter
fullscreen and verify both state and monitor. Bounded polling requires consecutive
matching observations. Closed windows, changed owners, timeouts, failed commands,
and unsupported operations return explicit incomplete results. An operation's
attempt log does not claim that the operation succeeded.
Native fullscreen can temporarily omit a surviving window from yabai queries;
polling tolerates that gap within its deadline and rechecks ownership when the
window returns. A permanently missing or replaced window is still skipped.

The transition engine is not connected to the CLI. The next implementation round
adds the restore coordinator and restore lock using the snapshot storage,
final visibility/focus restoration, whole-restore verification, and CLI behavior.
The coordinator must hold its lock and persist a valid backup before invoking
window transitions.

Run the automated tests with synthetic observations and injected native calls:

```sh
uv run python -m unittest discover -v
```

Run the separate read-only macOS identity checks explicitly:

```sh
uv run python -m unittest tests.live_identity -v
```

These inspect the current process and a temporary child, without printing identity
values. They require access to the boot identifier and a graphical login session.
They do not verify fullscreen or monitor restoration.

The optional adapter check performs a read-only observation through the running
yabai service and validates its in-memory serialization:

```sh
uv run python -m unittest tests.live_yabai -v
```

It prints no window titles or identity values. Fullscreen transition tests use
an in-memory yabai simulator; they are not live desktop-restoration evidence.
A separate scoped native test has also completed fullscreen entry, a monitor
round trip through the planner, extra-fullscreen exit, and closed-window skipping
with a disposable TextEdit document. The test used a fresh backup and verified
that existing fullscreen/monitor states, visible windows, and final focus were
restored. This covers the internal engine with that app, rather than the future
whole-session restore CLI.

Identity matching uses boot UUID, user/login session, and window ID plus owning
PID/process start time. Unreadable owners are unsupported. A reused saved window
ID with a different owner is skipped, including by the extra-fullscreen policy.
Sticky, system, overlay, hidden, and minimized windows are currently unsupported.
Window ID reuse within the same surviving process and WindowServer restarts
within a login session are not independently detected by these identity tokens.

## Try the prototypes

Requires macOS, Python 3, a running yabai service, and yabai Accessibility permission. These are one-off prototypes, not the eventual CLI:

```sh
python3 prototypes/capture-yabai-layout.py /tmp/spacekeeper-layout.json
# These commands change the desktop. Inspect the snapshot first.
python3 prototypes/restore-yabai-fullscreen-monitors.py /tmp/spacekeeper-layout.json
python3 prototypes/restore-yabai-layout.py /tmp/spacekeeper-layout.json
```

The fullscreen helper restores surviving windows to their original monitors without requiring the scripting addition. The broader layout helper also attempts normal-window frames and Space ordering; ordering requires the scripting addition. Each restore writes a pre-restore backup and a result report next to its input snapshot. The prototypes do not yet implement v0's policy for extra fullscreen windows.

Live snapshots contain application/window titles and other personal state. Store them outside the repository; commit only synthetic fixtures.

MIT licensed. A tool from [Omni Tools](https://github.com/omni-tools).
