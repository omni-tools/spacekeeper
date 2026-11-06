"""Restore coordination: global exclusion, backup first, final verification."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import fcntl
import os
from pathlib import Path
import stat
import time
from typing import TYPE_CHECKING, Callable

from spacekeeper.identity import IdentityUnavailable
from spacekeeper.matching import WindowRule
from spacekeeper.planner import DecisionStatus, Reason, RestorePlan, WindowTarget, plan_restore
from spacekeeper.snapshot import Snapshot, WindowIdentity
from spacekeeper.storage import SnapshotStore
from spacekeeper.transitions import Desktop, TransitionStatus, WindowTransitions
from spacekeeper.yabai import YabaiError, YabaiFailure

if TYPE_CHECKING:
    from spacekeeper.presets import Preset


class RestoreError(RuntimeError):
    """Restore could not start, or a report could not be persisted."""


@contextmanager
def restore_lock(path: Path | None = None):
    # Desktop focus is shared even when callers select different state roots.
    path = path if path is not None else Path('/private/tmp') / f"spacekeeper-{os.geteuid()}" / "restore.lock"
    fd = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = path.parent.lstat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid() or directory.st_mode & 0o077:
            raise RestoreError("Restore lock directory must be private and owned by the current user.")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise RestoreError("Restore lock must be a private regular file owned by the current user.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RestoreError("Another restore is already running.") from error
    except OSError as error:
        if fd is not None:
            os.close(fd)
            fd = None
        raise RestoreError("Cannot acquire the restore lock.") from error
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    try:
        yield
    finally:
        if fd is not None:
            # Never unlink the lock: a second inode would permit two owners.
            os.close(fd)


@dataclass(frozen=True)
class WindowOutcome:
    window_id: int
    status: str
    reason: str
    monitor_fallback: bool = False
    attempted_operations: tuple[str, ...] = ()
    source_window_id: int | None = None
    match_method: str | None = None


@dataclass(frozen=True)
class RestoreIssue:
    scope: str
    reason: str
    window_id: int | None = None
    display_uuid: str | None = None
    source_window_id: int | None = None


@dataclass(frozen=True)
class RestoreReport:
    complete: bool
    backup_path: str
    report_path: str
    windows: tuple[WindowOutcome, ...]
    issues: tuple[RestoreIssue, ...]
    name_fallback: bool = False

    @property
    def missed(self) -> int:
        """Count distinct unsuccessful windows across source and live references."""
        bindings = {w.window_id: w.source_window_id for w in self.windows if w.source_window_id is not None}
        bindings.update({i.window_id: i.source_window_id for i in self.issues
                         if i.window_id is not None and i.source_window_id is not None})
        def key(window_id, source_window_id=None):
            source = source_window_id if source_window_id is not None else bindings.get(window_id)
            return ('source', source) if source is not None else ('live', window_id)
        missed = {key(w.window_id, w.source_window_id) for w in self.windows
                  if w.status not in ('restored', 'unchanged', 'excluded')}
        missed.update(key(i.window_id, i.source_window_id) for i in self.issues
                      if i.window_id is not None or i.source_window_id is not None)
        return len(missed)

    @property
    def hard_failure(self) -> bool:
        """Distinguish partial planning misses from operations/verification failure."""
        return (any(w.status == 'failed' for w in self.windows) or
                any(i.scope != 'planning' or (i.window_id is None and i.source_window_id is None)
                    for i in self.issues) or (not self.complete and self.missed == 0))

    def to_dict(self) -> dict:
        return asdict(self)


class RestoreCoordinator:
    def __init__(self, desktop: Desktop, store: SnapshotStore, *, lock_path: Path | None = None,
                 timeout: float = 30, poll_interval: float = 0.2,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 on_backup: Callable[[Path], None] | None = None,
                 on_plan: Callable[[RestorePlan], None] | None = None):
        self.desktop, self.store, self.lock_path = desktop, store, lock_path
        self.timeout, self.interval, self.clock, self.sleep = timeout, poll_interval, clock, sleep
        self.on_backup = on_backup
        self.on_plan = on_plan
        self.transitions = WindowTransitions(desktop, transition_timeout=timeout, poll_interval=poll_interval,
                                             clock=clock, sleep=sleep)

    def _observe(self, deadline: float) -> Snapshot:
        last_error = None
        while self.clock() < deadline:
            try:
                return self.desktop.observe(deadline=deadline)
            except YabaiError as error:
                if error.reason not in (YabaiFailure.DESKTOP_CHANGED, YabaiFailure.TIMEOUT):
                    raise
                # A client query may time out while yabai is handling display
                # changes. Retry reads within the original observation budget;
                # this never retries a focus, fullscreen, move, or Dock command.
                last_error = error
                self.sleep(min(self.interval, max(0, deadline - self.clock())))
        detail = f" Last query error: {last_error}" if last_error is not None else ''
        raise YabaiError(YabaiFailure.TIMEOUT,
                         "Desktop observation timed out while waiting for yabai." + detail + " "
                         "Retry after the desktop settles, or increase --timeout.") from last_error

    @staticmethod
    def _focus_matches(snapshot: Snapshot, identity: WindowIdentity, display_uuid: str | None) -> bool:
        window = next((w for w in snapshot.windows if w.identity == identity), None)
        if (window is None or window.minimized or window.hidden or
                (display_uuid is not None and window.display_uuid != display_uuid)):
            return False
        return window.focused and any(s.id == window.space_id and s.visible and s.focused for s in snapshot.spaces)

    def _focus(self, identity: WindowIdentity, session, display_uuid: str | None) -> None:
        deadline = self.clock() + self.timeout
        initial = self.desktop.focus(identity, session, deadline=deadline)
        consecutive = int(initial is not None and initial.focus_ready and
                          (display_uuid is None or initial.window.display_uuid == display_uuid))
        if consecutive:
            self.sleep(min(self.interval, max(0, deadline - self.clock())))
        while self.clock() < deadline:
            try:
                observed = self.desktop.observe_window(identity, session, deadline=deadline)
                matches = observed.focus_ready and (
                    display_uuid is None or observed.window.display_uuid == display_uuid)
                consecutive = consecutive + 1 if matches else 0
            except YabaiError as error:
                if error.reason not in (YabaiFailure.DESKTOP_CHANGED, YabaiFailure.WINDOW_MISSING):
                    raise
                consecutive = 0
            if consecutive == 2 and self.clock() < deadline:
                return
            self.sleep(min(self.interval, max(0, deadline - self.clock())))
        raise YabaiError(YabaiFailure.TIMEOUT, "Focus/visibility restoration was not confirmed.")

    def restore(self, saved: Snapshot, *, preserve: tuple[WindowRule, ...] = (), fallback: bool = True) -> RestoreReport:
        return self._restore(saved, lambda before: plan_restore(saved, before, preserve=preserve, fallback=fallback))

    def apply_preset(self, preset: 'Preset') -> RestoreReport:
        return self._restore(None, preset.plan)

    def _restore(self, saved: Snapshot | None, build_plan: Callable[[Snapshot], RestorePlan]) -> RestoreReport:
        with restore_lock(self.lock_path):
            before = self._observe(self.clock() + self.timeout)
            backup = self.store.backup(before)  # Must succeed and read back before any mutation.
            if self.on_backup is not None:
                self.on_backup(backup)
            plan = build_plan(before)
            if self.on_plan is not None:
                self.on_plan(plan)
            saved = before if saved is None else saved
            outcomes, issues = [], [RestoreIssue("planning", item.reason.value, item.window_id, item.display_uuid,
                                                item.source_window_id)
                                    for item in plan.issues]
            session_changed = False
            saved_ids = {window.id for window in saved.windows}
            protected_saved = {window.id for window in saved.windows if window.sticky or not window.root_window or
                               (window.role, window.subrole) != ('AXWindow', 'AXStandardWindow')}
            if plan.accepted:
                for decision in plan.windows:
                    if session_changed:
                        outcomes.append(WindowOutcome(decision.window_id, "failed", "session_changed",
                                                      decision.monitor_fallback, source_window_id=decision.source_window_id,
                                                      match_method=decision.match_method))
                    elif decision.status == DecisionStatus.PLANNED and decision.target is not None:
                        result = self.transitions.apply(decision.target, before.session)
                        status = "restored" if result.status == TransitionStatus.VERIFIED else result.status.value
                        outcomes.append(WindowOutcome(decision.window_id, status, result.reason or decision.reason.value,
                                                      decision.monitor_fallback, result.attempted_operations,
                                                      decision.source_window_id, decision.match_method))
                        session_changed = result.reason == YabaiFailure.SESSION_CHANGED
                    else:
                        expected_exclusion = (decision.reason == Reason.PROTECTED_WINDOW and
                                              (decision.window_id in protected_saved or decision.window_id not in saved_ids))
                        status = "excluded" if expected_exclusion else decision.status.value
                        outcomes.append(WindowOutcome(decision.window_id, status, decision.reason.value,
                                                      decision.monitor_fallback, source_window_id=decision.source_window_id,
                                                      match_method=decision.match_method))

                eligible = {outcome.window_id for outcome in outcomes if outcome.status in ("restored", "unchanged")}
                already_matches = (not any(d.status == DecisionStatus.PLANNED for d in plan.windows) and
                                   not self._verify(plan, before, before))
                if not session_changed and not already_matches:
                    # Make the final focused window the last visibility operation
                    # when possible, then reuse its just-confirmed focus result.
                    visible = sorted(plan.visible, key=lambda target: target.identity == plan.focus)
                    last_focused = None
                    for target in visible:
                        if target.identity.window_id not in eligible:
                            continue
                        try:
                            self._focus(target.identity, before.session, target.display_uuid)
                            last_focused = target.identity
                        except (YabaiError, IdentityUnavailable) as error:
                            last_focused = None
                            issues.append(RestoreIssue("visibility", _reason(error), target.identity.window_id,
                                                       target.display_uuid))
                            if isinstance(error, YabaiError) and error.reason == YabaiFailure.SESSION_CHANGED:
                                session_changed = True
                                break
                    if (not session_changed and plan.focus is not None and plan.focus.window_id in eligible and
                            last_focused != plan.focus):
                        try:
                            self._focus(plan.focus, before.session, None)
                        except (YabaiError, IdentityUnavailable) as error:
                            issues.append(RestoreIssue("focus", _reason(error), plan.focus.window_id))

                try:
                    deadline, stable = self.clock() + self.timeout, 0
                    remaining = [RestoreIssue("verification", "timeout")]
                    while self.clock() < deadline:
                        final = self._observe(deadline)
                        remaining = self._verify(plan, before, final)
                        stable = stable + 1 if not remaining else 0
                        if stable == 2:
                            break
                        self.sleep(min(self.interval, max(0, deadline - self.clock())))
                    else:
                        if not remaining:
                            remaining = [RestoreIssue("verification", "timeout")]
                    issues.extend(remaining)
                except (YabaiError, IdentityUnavailable) as error:
                    issues.append(RestoreIssue("verification", _reason(error)))

            failed_ids = {issue.window_id for issue in issues if issue.scope == "verification" and issue.window_id is not None}
            outcomes = [replace(outcome, status="failed", reason="final_state_mismatch")
                        if outcome.window_id in failed_ids and outcome.status in ("restored", "unchanged")
                        else outcome for outcome in outcomes]
            complete = plan.accepted and not issues and all(o.status in ("restored", "unchanged", "excluded") for o in outcomes)
            report = RestoreReport(complete, str(backup), str(self.store.root / "reports" / backup.name),
                                   tuple(outcomes), tuple(issues), plan.name_fallback)
            try:
                self.store.report(backup, report.to_dict())
            except Exception as error:
                raise RestoreError(f"Cannot save the restore report. Pre-restore backup: {backup}") from error
            return report

    @staticmethod
    def _verify(plan: RestorePlan, before: Snapshot, final: Snapshot) -> list[RestoreIssue]:
        if final.session != before.session:
            return [RestoreIssue("verification", "session_changed")]
        issues = []
        current = {w.id: w for w in final.windows}
        original = {w.id: w for w in before.windows}
        for decision in plan.windows:
            window = current.get(decision.window_id)
            target = decision.target
            if target is not None:
                if window is None or window.identity != target.identity:
                    issues.append(RestoreIssue("verification", "missing_or_changed_owner", decision.window_id))
                elif ((window.display_uuid, window.fullscreen) != (target.display_uuid, target.fullscreen) or
                      (target.minimized is not None and window.minimized != target.minimized)):
                    issues.append(RestoreIssue("verification", "window_target_mismatch", decision.window_id))
            elif decision.reason in (Reason.EXTRA_ORDINARY, Reason.PRESERVED, Reason.PRESET_UNMATCHED,
                                     Reason.FALLBACK_UNMATCHED):
                previous = original[decision.window_id]
                if window is None or window.identity != previous.identity or any(
                    getattr(window, key) != getattr(previous, key)
                    for key in ("fullscreen", "minimized", "hidden", "display_uuid", "space_id", "frame")
                ):
                    reason = {Reason.EXTRA_ORDINARY: 'extra_ordinary_changed', Reason.PRESERVED: 'preserved_window_changed',
                              Reason.PRESET_UNMATCHED: 'unmatched_window_changed',
                              Reason.FALLBACK_UNMATCHED: 'fallback_unmatched_changed'}[decision.reason]
                    issues.append(RestoreIssue("verification", reason, decision.window_id))
        for target in plan.visible:
            window = current.get(target.identity.window_id)
            if (window is None or window.minimized or window.hidden or window.identity != target.identity or
                    window.display_uuid != target.display_uuid or not any(
                s.id == window.space_id and s.visible for s in final.spaces
            )):
                issues.append(RestoreIssue("verification", "visible_window_mismatch", target.identity.window_id,
                                           target.display_uuid))
        if plan.focus is not None and not RestoreCoordinator._focus_matches(final, plan.focus, None):
            issues.append(RestoreIssue("verification", "focused_window_mismatch", plan.focus.window_id))
        known_ids = {decision.window_id for decision in plan.windows}
        for window in final.windows:
            if (window.id not in known_ids and window.fullscreen and not window.sticky and window.root_window and
                    (window.role, window.subrole) == ("AXWindow", "AXStandardWindow")):
                issues.append(RestoreIssue("verification", "new_fullscreen_window", window.id))
        return issues


def _reason(error: Exception) -> str:
    if isinstance(error, YabaiError):
        return error.reason.value
    return YabaiFailure.IDENTITY_UNAVAILABLE.value
