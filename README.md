# Spacekeeper

Capture and restore macOS fullscreen window arrangements through yabai.

**Status: Poetry package, Click command scaffold, and working prototypes. CLI capture/list/restore behavior is not implemented yet.**

The first version will save named snapshots of the running macOS session and restore fullscreen states and original monitors. YAML presets, flexible app/title matching, preserve filters, reopening missing windows, and scripting-addition-assisted Space ordering are planned for later versions.

## Start here

- [Agreed design and v0 acceptance criteria](docs/design.md)
- [Prototype scripts and limitations](prototypes/README.md)
- [Future scripting addition and Space ordering](docs/draft/HANDOFF.yabai-sa-and-space-reordering.md)
- Local implementation takeover instructions: `private/HANDOFF.md` (intentionally ignored)

## Development setup

Requires Python 3.12+ and Poetry 2.x:

```sh
poetry install
poetry run spacekeeper --help
poetry run spacekeeper capture --help
poetry run spacekeeper restore --help
```

Poetry creates a local `.venv`. The Click scaffold provides `capture [NAME]
[--force]`, `list`, `restore [NAME]`, and `--version`. Capture/list/restore currently
exit with an explicit not-implemented error and do not query or change the desktop.
You can also run `poetry run python -m spacekeeper --help`.

Build the wheel and source distribution with `poetry build`.

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
