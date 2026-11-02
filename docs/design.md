# Spacekeeper: agreed design

Confirmed 2026-11-02. Python program in public `omni-tools/spacekeeper`, MIT licensed. The initial repository contains prototypes and this design; the next implementation session builds v0.

## v0: capture and restore the current session

Proposed command surface (to implement):

```sh
spacekeeper capture [NAME] [--force]
spacekeeper list
spacekeeper restore [NAME]
```

Omitting NAME selects `default`. Capture automatically replaces `default`; replacing any other existing name requires `--force`. Restore always saves a timestamped pre-restore backup before mutations. Validate names and use atomic writes so names cannot escape the storage directory and an interrupted capture cannot destroy the previous snapshot.

Snapshots target surviving windows in the **same running macOS session**. Match window ID plus owning process identity, not title alone. Record enough session identity to reject stale ID reuse after a reboot/process replacement. Skip closed/reopened windows and report them; do not launch apps or recreate windows in v0. Titles are metadata, not stable identity.

### Restore behavior

For windows represented by the snapshot, restore native fullscreen on/off and their saved monitor when connected. Match monitors by UUID rather than current arrangement index. If a saved monitor is disconnected, keep the window on its current monitor and restore fullscreen state there; report the fallback.

**Extra windows absent from the snapshot exit fullscreen in v0.** They stay open. Extra ordinary windows remain unchanged. Do not treat sticky/system/overlay windows as restore targets. Preserve filters are deferred, so v0 must clearly document this fullscreen-exit policy.

Use the verified monitor-first sequence for a fullscreen window on the wrong monitor:

1. Exit fullscreen if necessary and confirm completion.
2. Re-resolve a normal Space on the target monitor and move the window there.
3. Focus that window; verify both focus and target monitor.
4. Enter fullscreen and verify both fullscreen state and monitor UUID.

The same sequence applies when entering fullscreen on a connected saved monitor. A native fullscreen Space cannot receive an ordinary window. If no normal destination exists, report the window as unrestored; v0 must not invent a different destination or require SA to create a Space.

When turning fullscreen off, use the existing normal desktop; v0 does not promise exact Space identity, desktop index, or frame coordinates. Native fullscreen creates/removes Spaces, so their IDs and Mission Control indexes may change. Re-resolve identities before each mutation, and poll state with bounded waits instead of trusting command exit status or fixed delays alone.

Restore the saved visible window on each available monitor and final focus last where surviving identities permit it. Verify final state and report unsupported, skipped, failed, and successfully restored items separately. Missing SA is expected in v0: there is no Space-order promise and no automatic installation, privilege change, SIP change, or Recovery operation.

### Execution and storage

Keep focus/fullscreen changes and Space-related operations sequential: global focus and changing Space indexes make concurrent mutations unsafe. Parallelize read-only snapshot queries. Independent frame moves/resizes can use up to four workers in a future frame-restoration feature, with per-window command order preserved. Current prototypes demonstrate this; no speedup has been measured.

Default runtime storage:

- Snapshots/backups: `~/.local/state/spacekeeper`.
- Later YAML presets/configuration: `~/.config/spacekeeper`.

Both roots must be overridable. Respect the chosen state/config root consistently. Resolve exact CLI option names and packaging details during implementation; these have not been chosen. Keep runtime data outside Git. Capture all yabai display/Space/window metadata needed for later restore capabilities, including monitor UUIDs, Space stable IDs, frames, owner identity, fullscreen and focus state. A snapshot is an observation, not an atomic macOS transaction; validate topology/membership around capture or retry it.

Use shell-free subprocess argument arrays, bounded timeouts and output, clear errors, and one restore at a time. Changes are opt-in through an explicit restore invocation; capture/list are read-only. A CLI boundary lets VS Code and other languages integrate later without sharing Python implementation code. The earlier JSON-output proposal was not explicitly settled: design a machine-readable result model now and confirm the external JSON option when integration is implemented.

### Suggested implementation boundaries

- **Yabai adapter:** queries, commands, process execution, capability observations.
- **Snapshot/storage:** versioned schema, session identity, atomic named saves and backups.
- **Planner:** identity matching and a deterministic restore plan, independent of subprocesses.
- **Executor:** ordered transitions, polling, progress, bounded independent work, verification.
- **CLI:** argument parsing, output and exit behavior. Choose dependencies only when the implementation needs them; v0's core operations fit the standard library.

Reuse behavior from the prototypes; replace their top-level script structure and hardcoded assumptions. Neither prototype is the finished v0 implementation.

## Later versions

1. **Preserve filters:** extra fullscreen windows matching app/title patterns stay untouched; other extra fullscreen windows exit fullscreen. The exact rule syntax is still to design.
2. **YAML presets:** minimal rules contain app name and device; optional title matching narrows a rule. Apply a rule to all matching windows. Support flexible/fuzzy names and capture the current arrangement into a preset. Define device aliases and ambiguous fuzzy-match behavior before implementation; the interview did not settle them. Avoid silently choosing an unrelated app/device.
3. **SA-assisted positioning:** restore exact Space monitor placement/order when scripting addition capabilities are available. See the [SA handoff](draft/HANDOFF.yabai-sa-and-space-reordering.md). Normal frame restore can be supported separately where Accessibility allows it; it does not intrinsically require SA.
4. **Respawning:** optionally launch missing apps/windows; define app-specific restoration capabilities and explicit consent separately.
5. **Integrations:** a native VS Code companion or other program can consume the CLI. No VS Code dependency in the backend.

A tentative preset shape, **illustrative rather than a finalized schema**:

```yaml
windows:
  - app: Code
    device: primary
  - app: Chrome
    title: "*Research*"
    device: external
```

## v0 completion criteria

- Capture/list/named/default restore work with the agreed overwrite rules and overridable storage.
- Same-session ownership checks prevent moving a different process's reused window ID.
- Connected monitors match by UUID; disconnected monitors follow the agreed fallback.
- The monitor-first fullscreen regression is covered at the planner/executor seam and verified live on macOS.
- Extra fullscreen windows exit fullscreen; extra ordinary windows remain open and unchanged.
- Capture/list are read-only; every restore has a valid pre-restore backup.
- Transition timeouts, closed windows, changing Space indexes, missing normal destination, and failed commands produce explicit incomplete results rather than false success.
- Tests use synthetic snapshots and an injected command runner. Include CLI/storage checks and executor sequencing; distinguish mocked results from live evidence.
- Public Git history contains no real snapshots, personal presets, machine UUIDs, window titles, or local absolute paths. Provide concise install/use/limitation documentation.

The initial package uses Poetry, Python 3.12+, and Click for the command structure, with a `spacekeeper` console entry point. Capture/list/restore are currently placeholders. Still open for implementation: machine-readable output interface and the precise exit-code contract for completed operations. Later-feature schemas and matching policy are intentionally deferred.
