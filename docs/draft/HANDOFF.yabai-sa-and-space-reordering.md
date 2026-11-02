# Handoff: yabai scripting addition and Space reordering

## Goal and current state

Enable yabai's scripting addition (SA), then restore monitor placement and ordering of macOS Spaces from a user-selected snapshot outside this repository. “Screen reordering” here means arranging Spaces within/between monitors, not changing physical monitor coordinates.

Observed during the 2026-11-02 prototype investigation: fullscreen/monitor restoration worked without SA; Space moves failed with scripting-addition errors while SIP was enabled. Recheck macOS, hardware, yabai, and SIP state on the target machine. SA support is a future version; see [the v0 design](../design.md).

## Enable SA — owner performs Recovery steps

This reduces macOS security protections. Proceed only with the owner's approval; creating this handoff does not authorize security changes.

1. Shut down, hold power until startup options, choose **Options → Continue**, then **Utilities → Terminal**.
2. For Apple Silicon on macOS 13 or newer, verify the linked upstream instructions before running in Recovery:

   ```sh
   csrutil enable --without fs --without debug --without nvram
   ```

3. Restart normally. Check `nvram boot-args` first; preserve any existing arguments. If unset:

   ```sh
   sudo nvram boot-args=-arm64e_preview_abi
   ```

4. Restart again, inspect `csrutil status`, then load SA:

   ```sh
   sudo yabai --load-sa
   ```

5. After a successful live Space move, optionally configure automatic reload on Dock restart using the upstream setup. Scope any passwordless sudo rule to the exact binary/hash and `--load-sa`; update the hash after upgrades. Reload signal:

   ```sh
   yabai -m signal --add event=dock_did_restart action="sudo yabai --load-sa"
   ```

Rollback: run `csrutil enable` in Recovery to restore SIP; restore the previous boot arguments as recorded before setup.

## Restore the saved arrangement

- Save a fresh displays/Spaces/windows JSON backup before mutations. Keep extra ordinary windows open; apply the agreed fullscreen policy from the design to extra fullscreen windows.
- Match displays by UUID, Spaces by UUID/stable ID, and windows by ID plus owner identity. Recreated fullscreen Spaces need an unambiguous surviving fullscreen-window match. Re-resolve current indexes immediately before every move.
- Restore fullscreen state and normal-window Space/frame first; wait for fullscreen transitions. Then send Spaces to their saved monitors, reorder within each monitor, and restore visible Spaces/focus last.
- Verified workaround without SA: exit native fullscreen, send the window to a normal Space on its saved monitor, focus it and confirm the monitor, then enter fullscreen again. Verify **both** fullscreen state and monitor UUID afterward. Entering fullscreen before correcting the monitor caused a real restore bug. This recreates the fullscreen Space; its original identity/order is not recovered.
- Space selectors use current Mission Control indexes, not stable Space IDs. Examples use placeholders:

   ```sh
   yabai -m space CURRENT_INDEX --display TARGET_DISPLAY_INDEX
   yabai -m space CURRENT_INDEX --move TARGET_SPACE_INDEX
   yabai -m space CURRENT_INDEX --move prev
   yabai -m space CURRENT_INDEX --move next
   ```

- `--move` requires both Spaces on the same monitor. Guard left/right boundaries explicitly. Preserve native fullscreen by moving its whole Space. A normal window can use `window WINDOW_ID --space TARGET_SPACE_INDEX`; the target cannot be a native fullscreen Space.
- Re-query after each operation and verify actual state. Stop the affected operation on a capability error; keep recovery data. Report unmatched identities and app-enforced frame differences separately.
- Parallelize independent window frame moves/resizes (up to four workers; move then resize per window) and snapshot queries. Keep focus/fullscreen transitions and Space moves sequential; they share global focus and changing Space indexes. The temporary restore helper implements this split; no speedup has been measured yet.

Reference prototypes: `prototypes/restore-yabai-layout.py` and `prototypes/restore-yabai-fullscreen-monitors.py`. Pass an explicit snapshot path; inspect their limitations before use. SA support and exact Space ordering are outside v0.

Done when a live Space move succeeds and the final queried monitor/order/window/fullscreen state matches the snapshot, with any unavoidable differences listed. Snapshots/tests alone do not prove SA works.

## Sources

- [SIP and Apple Silicon prerequisites](https://github.com/asmvik/yabai/wiki/Disabling-System-Integrity-Protection)
- [SA loading and automatic reload setup](https://github.com/asmvik/yabai/wiki/Installing-yabai-(latest-release)#configure-scripting-addition)
- [Command reference for installed version](https://github.com/asmvik/yabai/blob/v7.1.25/doc/yabai.asciidoc)
