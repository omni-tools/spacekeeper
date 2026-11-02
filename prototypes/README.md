# Imported prototypes

These scripts were used to investigate and restore a real desktop before Spacekeeper existed. They are a behavioral reference, not the v0 CLI.

| Script | Purpose |
| --- | --- |
| `capture-yabai-layout.py OUTPUT.json` | Reconstructs the working capture command as a standalone script; queries and checks Space membership before/after capture |
| `restore-yabai-fullscreen-monitors.py SNAPSHOT.json` | Restores surviving fullscreen windows to the saved monitor using exit → move → focus → fullscreen |
| `restore-yabai-layout.py SNAPSHOT.json` | Broader restore prototype: fullscreen/monitor correction, ordinary frames, and attempted Space order; bounded concurrent frame tasks |

Migration changes: replaced machine-specific snapshot paths with a required argument and added usage guards. Real snapshots remain outside the repository. The capture script is reconstructed from the actual capture command; the two restore scripts are imported from the working files.

Known differences from the agreed v0:

- Both restore scripts execute at top level and lack the planned package/CLI separation.
- They skip missing/changed owners but lack robust session/reboot identity.
- They do not implement the required exit-fullscreen policy for extra windows.
- They do not implement named snapshot storage or overwrite semantics.
- The broad helper rejects a changed display geometry instead of implementing the agreed missing-monitor fallback.
- The fullscreen helper uses a saved display UUID to find the current index; the broad helper still assumes saved/current display indexes agree in some paths.
- The broad helper attempts SA-dependent ordering; v0 explicitly defers it.
- A missing target normal Space, failure partway through transitions, stale selection, hidden/minimized windows, and user interaction during restore need clearer recovery/reporting.
- Reports and backups are stored next to the input file, not under the eventual state-root policy. Some incomplete outcomes currently exit zero: inspect reports.

Before migration, a live check confirmed 16 saved fullscreen windows on their original monitors. That does not prove exact Space ordering, every prototype branch, or the future v0 implementation. A monitor-first function regression check passed after correcting the original fullscreen-before-monitor bug.
