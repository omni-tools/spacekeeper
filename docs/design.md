# Spacekeeper: agreed design

Confirmed 2026-11-02. Python program in public `omni-tools/spacekeeper`, MIT licensed. The initial repository contains prototypes and this design; the next implementation session builds v0.

## v0: capture and restore the current session

Proposed command surface (to implement):

```sh
spacekeeper capture [NAME] [--force]
spacekeeper list
spacekeeper restore [NAME] [--no-fallback]
```

Omitting NAME selects `default`. Capture automatically replaces `default`; replacing any other existing name requires `--force`. Restore always saves a timestamped pre-restore backup before mutations. Validate names and use atomic writes so names cannot escape the storage directory and an interrupted capture cannot destroy the previous snapshot.

Within the same running macOS session, match window ID plus owning process identity. Skip closed/replaced owners and report them. After a reboot or login change, the default is a partial name fallback for the same effective UID: only a unique case-insensitive app/title pair in both observations can rebind a saved target to a current window and its fresh process identity. For Code, Code - OSS, and Code - Insiders, normalize titles to their trailing workspace name after the last ` — `, or the whole title when there is no separator. Keep Extension Development Host windows distinct from regular workspace windows, require the same app, and reject duplicate workspace keys in either observation. All other apps retain literal full-title matching. Missing, ambiguous, or unsupported sources are reported as incomplete. Unmatched current windows retain their placement/fullscreen. Old snapshot IDs remain source references and never select current targets. Different users are rejected; `--no-fallback` restores strict boot/login rejection. No apps/windows are launched or recreated.

### Restore behavior

For windows represented by the snapshot, restore native fullscreen on/off, minimized state in the Dock, and their saved monitor when connected. Match monitors by UUID rather than current arrangement index. If a saved monitor is disconnected, keep the window on its current monitor and restore fullscreen/minimized state there; report the fallback.

Snapshot targets explicitly include minimized state, in both original-session and name-fallback restores. Restore a minimized window from the Dock before any monitor or fullscreen changes, then minimize last when the saved state requires it. An already-matching minimized window stays in the Dock without focus operations. Final verification includes minimized state, and unmatched windows retain their Dock state. Hidden apps and protected windows remain unsupported. Presets do not manage minimized state and continue to reject minimized matches.

**In same-session restore, extra windows absent from the snapshot exit fullscreen by default.** They stay open. Extra ordinary windows remain unchanged. Do not treat sticky/system/overlay windows as restore targets. Optional preserve filters exclude matching extra fullscreen windows from this policy. In stale-session name fallback, unmatched current windows remain untouched without needing preserve filters. Matched saved windows follow their snapshot targets.

Use the verified monitor-first sequence for a fullscreen window on the wrong monitor:

1. Exit fullscreen if necessary and confirm completion.
2. Re-resolve a normal Space on the target monitor and move the window there.
3. Focus that window; verify both focus and target monitor.
4. Enter fullscreen and verify both fullscreen state and monitor UUID.

The same sequence applies when entering fullscreen on a connected saved monitor. A native fullscreen Space cannot receive an ordinary window. If no normal destination exists, report the window as unrestored; v0 must not invent a different destination or require SA to create a Space.

When turning fullscreen off, use the existing normal desktop; v0 does not promise exact Space identity, desktop index, or frame coordinates. Native fullscreen creates/removes Spaces, so their IDs and Mission Control indexes may change. Re-resolve identities before each mutation, and poll state with bounded waits instead of trusting command exit status or fixed delays alone.

Restore the saved visible window on each available monitor and final focus last where surviving identities permit it. If the final focused window falls back from a disconnected monitor, its visibility takes precedence on its current monitor; do not require a different saved Space to remain visible on the same monitor. Verify final state and report unsupported, skipped, failed, and successfully restored items separately. Missing SA is expected in v0: there is no Space-order promise and no automatic installation, privilege change, SIP change, or Recovery operation.

### Execution and storage

Keep focus/fullscreen changes and Space-related operations sequential: global focus and changing Space indexes make concurrent mutations unsafe. Parallelize read-only snapshot queries. Independent frame moves/resizes can use up to four workers in a future frame-restoration feature, with per-window command order preserved. Current prototypes demonstrate this; no speedup has been measured.

Full observations are required for capture, the pre-restore backup, planning, and final verification. During edits, query only the affected window and fresh display/Space topology. Keep coherent double reads, fresh session/process ownership, focus checks before fullscreen toggles, stable transition polling, and destination index resolution immediately before a move. Reuse a just-confirmed transition or focus result when no intervening operation invalidated it. An already-matching plan can skip intermediate focus work, but success still requires two full final observations. No observation is cached across restore invocations.

Default runtime storage:

- Snapshots/backups: `~/.local/state/spacekeeper`.
- Later YAML presets/configuration: `~/.config/spacekeeper`.

Both roots must be overridable. Respect the chosen state/config root consistently. Use --state-dir and --config-dir to select roots; CLI/environment overrides take precedence over optional config.json settings and defaults. Keep runtime data outside Git. Capture all yabai display/Space/window metadata needed for later restore capabilities, including monitor UUIDs, Space stable IDs, frames, owner identity, fullscreen and focus state. A snapshot is an observation, not an atomic macOS transaction; validate topology/membership around capture or retry it.

Use shell-free subprocess argument arrays, bounded timeouts and output, clear errors, and one restore at a time. Changes are opt-in through an explicit restore invocation; capture/list are read-only. A CLI boundary lets VS Code and other languages integrate later without sharing Python implementation code. The earlier JSON-output proposal was not explicitly settled: design a machine-readable result model now and confirm the external JSON option when integration is implemented.

### Suggested implementation boundaries

- **Yabai adapter:** queries, commands, process execution, capability observations.
- **Snapshot/storage:** versioned schema, session identity, atomic named saves and backups.
- **Planner:** identity matching and a deterministic restore plan, independent of subprocesses.
- **Executor:** ordered transitions, polling, progress, bounded independent work, verification.
- **CLI:** argument parsing, output and exit behavior. Choose dependencies only when the implementation needs them; v0's core operations fit the standard library.

Reuse behavior from the prototypes; replace their top-level script structure and hardcoded assumptions. Neither prototype is the finished v0 implementation.

## Features beyond v0

1. **Preserve filters (implemented):** case-insensitive app/title globs leave matching extra fullscreen windows untouched. A rule requires app and optionally title (default `*`); its fields match together and rules combine with OR. `restore --preserve APP_GLOB TITLE_GLOB` adds to `config.json`'s `preserve` list. `--no-preserve` disables configured rules. Final verification checks preserved ownership, fullscreen, monitor, Space, and frame.
2. **YAML presets (implemented):** named YAML files under the config root's `presets/` directory contain app/device rules with optional title and fullscreen fields. Exact app names win; explicit globs can match several apps; otherwise only a unique normalized substring of at least three characters is accepted. Title globs are case-insensitive. Device aliases bind to UUIDs, `primary` is current yabai index 1, and shortened aliases must be unambiguous. Every matching window receives the rule; unmatched windows retain placement/fullscreen. Conflicts, no-match rules, disconnected monitors, and unsupported matches fail before edits. The last matched window per monitor becomes visible, and the last rule's last matched window receives focus. Capture escapes app/title glob characters and rejects identical selectors with different targets. Apply uses the common backup/guard/verification coordinator. Commands: `preset capture/list/show/plan/apply`.
3. **SA-assisted positioning:** restore exact Space monitor placement/order when scripting addition capabilities are available. See the [SA handoff](draft/HANDOFF.yabai-sa-and-space-reordering.md). Normal frame restore can be supported separately where Accessibility allows it; it does not intrinsically require SA.
4. **Respawning:** optionally launch missing apps/windows; define app-specific restoration capabilities and explicit consent separately.
5. **Integrations:** a native VS Code companion or other program can consume the CLI. No VS Code dependency in the backend.

A minimal preset shape (fullscreen defaults to true; external aliases must be defined as monitor UUIDs):

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

The package uses uv, Python 3.12+, and Click for the command structure, with a `spacekeeper` console entry point. The snapshot model, read-only identity reader, and pure restore planner form the first implementation round. The second round adds the bounded yabai adapter and internal per-window transition engine, tested against a synthetic command/state simulator; adapter observation is checked separately on macOS. A scoped native TextEdit test has completed fullscreen entry, a monitor round trip, extra-fullscreen exit, and cleanup. It exposed a temporary window-query gap during fullscreen that is now covered by regression tests. Named storage, verified backups, per-user locking, restore coordination, final visibility/focus, whole-restore verification, and capture/list/restore commands are implemented. Backup recovery uses restore --backup ID. Exit codes are 0 for success, 1 for incomplete restores or operational errors, and 2 for usage errors. Structured reports are private files; a public machine-readable stdout interface remains deferred. A scoped quiet native CLI restore has exited 0 with complete final verification and verified cleanup; capture/list and isolated wheel installation also passed. Other apps and topology changes remain outside that native smoke coverage. Later-feature schemas and matching policy are intentionally deferred.
