# Spacekeeper

Capture and restore macOS fullscreen window arrangements through yabai.

**Status: v0 capture/list/restore CLI implemented, with synthetic end-to-end tests and a scoped native CLI restore check.**

Save named snapshots of the running macOS session and restore fullscreen states, Dock minimization, and original monitors. Preserve filters keep selected extra fullscreen windows untouched. YAML presets apply app/title/device rules to current windows. Reopening missing windows and scripting-addition-assisted Space ordering are planned for later versions.

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

**Same-session restore exits fullscreen for extra windows absent from the snapshot.**
Extra ordinary windows stay open and unchanged. Capture and list do not change
desktop windows. Within a running boot/login session, matching uses window IDs
and process creation identities.

After a reboot or login change, restore defaults to a partial name fallback for
the same user. A saved app/title pair must match exactly one window in both the
snapshot and the current desktop, case-insensitively. Names are literal, not
globs. For Code, Code - OSS, and Code - Insiders, matching uses the trailing
workspace name after the last ` — ` instead of the whole title: `old.py — codex`
matches `new.py — codex` or `codex`. The app must still match, and duplicate
workspace names in either observation are ambiguous. Extension Development Host
windows are matched separately from regular windows of the same workspace.
Matched windows use their current process identities and restore the saved
fullscreen/monitor/minimized targets; unmatched current windows stay untouched, including
fullscreen windows. Missing, ambiguous, and unsupported sources are reported and
make the restore incomplete (exit 1). Reports identify source snapshot IDs
separately from current window IDs. Another user's snapshot is still rejected.
If a disconnected monitor sends the final focused window onto another monitor,
that window takes precedence over the other monitor's saved visible window.

```sh
spacekeeper restore alpha                # Partial name fallback if the session is stale.
spacekeeper restore alpha --no-fallback  # Require the original boot/login session.
```

Same-session closed/replaced owners remain skipped; name fallback is for stale
boot/login sessions. Restore does not reopen apps/windows or restore Space
ordering or ordinary window frames. `--no-fallback` also applies with `--backup`.
Snapshot restore minimizes matched windows saved in the Dock and restores matched
windows saved outside it. Monitor and fullscreen changes happen before final
minimization; a minimized target is never focused for final visibility. Hidden
apps and protected/system windows remain unsupported.

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
are `state_dir`, `yabai` (executable path), `timeout` (seconds, default 12), and
`preserve` (app/title rules).
Relative paths in this file resolve against its directory. Neither listing nor
configuration loading creates missing directories.

Preserve filters match app and title globs case-insensitively. Both patterns in a
rule must match; any matching rule preserves an extra fullscreen window. Saved
windows still follow the snapshot. Configure default rules in `config.json` or
add rules for one restore:

```sh
spacekeeper restore work --preserve 'Browser*' '*meeting*'
spacekeeper restore work --preserve 'Video Call' '*'
spacekeeper restore work --no-preserve  # Ignore configured rules.
```

```json
{"preserve": [{"app": "Browser*", "title": "*meeting*"}, {"app": "Video Call"}]}
```

CLI rules add to configured rules. `--no-preserve` cannot be combined with
`--preserve`. Preserved windows are reported as excluded and verified unchanged.

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

## YAML presets

Presets live in `~/.config/spacekeeper/presets/NAME.yaml`, under the selected
`--config-dir`. They describe matching apps/titles and monitors independently of
snapshot window IDs, so they can be reused in another login session.

```sh
spacekeeper preset capture work        # Save supported current windows as rules.
spacekeeper preset list
spacekeeper preset show work
spacekeeper preset plan work           # Read-only preview against current windows.
spacekeeper preset apply work          # Changes the desktop; saves a backup first.
spacekeeper preset capture work --force
```

```yaml
version: 1
devices:
  external: 00000000-0000-0000-0000-000000000002 # Replace with a captured monitor UUID.
windows:
  - app: Code
    device: primary
  - app: Chrome
    title: '*Research*'
    device: external
    fullscreen: true
```

App matching prefers an exact case-insensitive name. Explicit app globs match all
matching apps; otherwise a unique substring of at least three letters/digits can
resolve a shortened app name, ignoring spacing and punctuation. Title selectors
are case-insensitive globs. Each rule applies to all matching windows.
`primary` means current yabai display index 1; captured `display-N` aliases bind
to UUIDs. Device aliases also accept unambiguous shortened names, or a connected
monitor UUID can be used directly. Disconnected monitors, ambiguous names,
unsupported matches, unmatched rules, and conflicting targets fail before edits.

Fullscreen defaults to `true`; set it to `false` for ordinary windows. Unmatched
windows retain their fullscreen state, monitor, Space, and frame. The last
matched window on each destination monitor becomes visible, and the last rule's
last matched window receives final focus. Apply uses the same lock, verified
backup, ownership guards, and final verification as snapshot restore. Snapshot
preserve filters do not override preset rules.

Capture escapes literal wildcard characters and stores explicit title selectors.
Identical app/title selectors with different targets cannot be represented and
are reported instead of guessed. Preset names require `--force` for replacement.
YAML uses safe loading; duplicate keys, aliases, unknown fields, and invalid types
are rejected. Presets must fit within 64 KiB.
The YAML parser is loaded only by preset commands; snapshot commands do not load it.

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
before window operations. Full observations derive window placement from Space
membership, with individual placement queries for ambiguous or unplaced windows.
Intermediate restore checks read only the affected window plus fresh monitor/Space
topology, with fresh session and owner identities around both query passes. They
never substitute for the full backup or final desktop verification.
A move selects a normal Space by stable ID, then
resolves its current index immediately before the command. Capability failures
are reported without attempting scripting-addition setup.

`transitions.py` implements the internal, sequential per-window engine: confirm
fullscreen exit, move to the target monitor, confirm focus there, then enter
fullscreen and verify both state and monitor. Bounded polling requires consecutive
matching observations of the window and its Space. Focus must include a visible,
focused owning Space. A guard-refused focus change before a fullscreen command
can be retried up to three times within the same deadline; a sent toggle is never
blindly retried. Closed windows, changed owners, timeouts, failed commands,
and unsupported operations return explicit incomplete results. An operation's
attempt log does not claim that the operation succeeded.
Native fullscreen can temporarily omit a surviving window from yabai queries;
polling tolerates that gap within its deadline and rechecks ownership when the
window returns. A permanently missing or replaced window is still skipped.
Target reads also retry the brief interval when fullscreen has exposed a window
before its Space query includes that window's membership, without resending the toggle.
The last confirmed transition result is reused when it already satisfies the
window target; an identical verification loop is not repeated.
A fresh guard that finds a window already focused can supply the first settling
sample. Confirmation still requires a new observation after the polling interval;
no observation from before a mutation is reused to verify that mutation.

`restore.py` holds a per-user restore lock across state directories and verifies
its fresh backup before invoking window transitions. It restores visible windows
and final focus last, verifies the whole result, and writes a private report beside
the backup collection. A single matching observation is insufficient for success.
If the initial observation already matches every target, visible window, and
focus, restore skips intermediate focus work and still requires two full final
observations. Otherwise, it focuses the final active window last and reuses that
confirmed focus instead of repeating the same operation.
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

The optional adapter checks perform full and target-only read-only observations
through the running yabai service, including full snapshot serialization:

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

Before these observation optimizations, the completed v0 CLI passed a quiet
native smoke test: a
disposable TextEdit window entered fullscreen and moved to another monitor, then
CLI restore applied the extra-fullscreen exit policy and verified the complete
saved result. It exited 0 with no report issues; existing window states, visible
Spaces, and final focus were restored, and the test document was closed. The
check used a scoped native runner that permitted fullscreen/monitor mutations
only for that disposable window. Real capture/list commands and an isolated
wheel installation were checked separately. Other applications and topology
changes still require their own live coverage.

Identity matching uses boot UUID, user/login session, and window ID plus owning
PID/process start time. Unreadable owners are unsupported. A reused saved window
ID with a different owner is skipped, including by the extra-fullscreen policy.
Sticky, system, overlay, hidden, and minimized windows are currently unsupported.
Window ID reuse within the same surviving process and WindowServer restarts
within a login session are not independently detected by these identity tokens.

Read-only measurements on one desktop with 65 windows compared the original
implementation with these optimizations: median capture CLI time was 709 ms
versus 416 ms (five runs each), and an individual pre-operation guard was 564 ms
versus 232 ms (seven runs each). These are local component measurements, not
end-to-end native restore timings. Synthetic query-budget tests also require
full-desktop observations only for the backup and two final verification reads
on settled restore paths; intermediate reads remain limited to their target.

## Continuous integration

GitHub Actions runs the synthetic tests on Ubuntu and macOS 15 with Python
3.12–3.15 for pushes to `main`, pull requests, and manual runs. A packaging job
builds the wheel and source distribution, checks their contents and private-path
exclusions, installs both in fresh environments, and checks CLI entry points
outside the checkout. Distributions are retained as artifacts for seven days.
Native identity/yabai checks remain explicit local tests; CI requires no desktop
session, Accessibility permission, or yabai service.

Reproduce the core checks locally:

```sh
uv run --locked python -m unittest discover -s tests -t . -p 'test_*.py' -v
uv build --no-sources
uv run --locked python scripts/check-distributions.py
```

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
