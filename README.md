# Spacekeeper

Capture and restore macOS fullscreen window arrangements through yabai.

**Status: v0 capture/list/restore CLI implemented, with synthetic end-to-end tests. Broader live restore validation is still in progress.**

Save named snapshots of the running macOS session and restore fullscreen states and original monitors. YAML presets, flexible app/title matching, preserve filters, reopening missing windows, and scripting-addition-assisted Space ordering are planned for later versions.

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

uv creates a local `.venv`. The CLI provides `capture [NAME] [--force]`,
`list`, `restore [NAME]`, and `--version`.
You can also run `uv run python -m spacekeeper --help`.

Build the wheel and source distribution with `uv build`.

## Use

```sh
uv run spacekeeper capture             # Replace the default snapshot.
uv run spacekeeper capture work        # Existing named snapshots require --force.
uv run spacekeeper list
uv run spacekeeper restore work        # Changes the desktop.
```

**Restore exits fullscreen for extra windows absent from the snapshot.** Extra
ordinary windows stay open and unchanged. Capture and list do not change desktop
windows. Snapshots match surviving windows in the same boot/login session; they
do not reopen closed windows or restore Space ordering or ordinary window frames.

Snapshot names are 1–64 ASCII letters, digits, dots, underscores, or hyphens,
starting with a letter or digit. The default state directory is
`~/.local/state/spacekeeper`, with `snapshots/`, `backups/`, and `reports/`.
Restore prints its verified backup path before any window operations and saves
a detailed report. To recover that pre-restore state, use the backup filename
without `.json` as its ID:

```sh
uv run spacekeeper restore --backup BACKUP_ID
uv run spacekeeper --state-dir /path/to/state capture work
```

Global options precede the command. `--config-dir` selects the optional
`config.json` directory (default `~/.config/spacekeeper`). Its accepted fields
are `state_dir`, `yabai` (executable path), and `timeout` (seconds, default 12).
Relative paths in this file resolve against its directory. Neither listing nor
configuration loading creates missing directories.

```json
{"timeout": 20}
```

CLI options override `SPACEKEEPER_STATE_DIR`, `SPACEKEEPER_CONFIG_DIR`,
`SPACEKEEPER_YABAI`, and `SPACEKEEPER_TIMEOUT`; those override file settings and
defaults. `--yabai` and `--timeout` provide explicit overrides. Invalid
configuration is reported with an error. A per-user lock under
`/private/tmp/spacekeeper-UID/` prevents restores across different state roots
from running together.

Exit codes: **0** for success, **1** for operational errors or incomplete
restores, **2** for usage errors. A complete restore confirms supported targets,
visible windows, and focus. Expected system-window exclusions are listed
separately; closed, unsupported, blocked, or failed targets make the result
incomplete. Structured JSON reports are stored privately; stdout is human-readable.

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

`restore.py` holds a per-user restore lock across state directories and verifies
its fresh backup before invoking window transitions. It restores visible windows
and final focus last, verifies the whole result, and writes a private report beside
the backup collection. A single matching observation is insufficient for success.
Expected system-window exclusions are reported separately from unsupported,
skipped, blocked, and failed targets. The Click commands use this coordinator.

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
