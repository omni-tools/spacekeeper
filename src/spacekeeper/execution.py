"""Sequential optimistic edits seeded from one verified pre-restore snapshot.

Accepted requests are pending, not verified. Only the restore coordinator's
global verification can promote them. Readiness reads are reserved for steps
whose destination/state depends on a preceding asynchronous edit.
"""

from dataclasses import replace
import time
from typing import Callable

from spacekeeper.identity import IdentityUnavailable
from spacekeeper.planner import WindowTarget
from spacekeeper.snapshot import Snapshot
from spacekeeper.transitions import Desktop, TransitionResult, TransitionStatus
from spacekeeper.yabai import YabaiError, YabaiFailure


class OptimisticTransitions:
    def __init__(self, desktop: Desktop, *, timeout: float, interval: float,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.desktop, self.timeout, self.interval = desktop, timeout, interval
        self.clock, self.sleep = clock, sleep

    def _ready(self, target, session, *, display_uuid=None, deadline=None):
        deadline = min(self.clock() + self.timeout, deadline) if deadline is not None else self.clock() + self.timeout
        while self.clock() < deadline:
            try:
                observed = self.desktop.observe_window(target.identity, session, deadline=deadline)
                window = observed.window
                space = next((s for s in observed.spaces if s.id == window.space_id), None)
                if (not window.fullscreen and not window.minimized and space is not None and not space.fullscreen and
                        (display_uuid is None or window.display_uuid == display_uuid)):
                    return observed
            except YabaiError as error:
                if error.reason not in (YabaiFailure.DESKTOP_CHANGED, YabaiFailure.WINDOW_MISSING):
                    raise
            self.sleep(min(self.interval, max(0, deadline - self.clock())))
        raise YabaiError(YabaiFailure.TIMEOUT, 'The window did not become ready for its next restore step.')

    def apply(self, target: WindowTarget, session, before: Snapshot, *, deadline: float | None = None) -> TransitionResult:
        attempts = []
        try:
            def operation_deadline():
                return min(self.clock() + self.timeout, deadline) if deadline is not None else self.clock() + self.timeout
            current = next((w for w in before.windows if w.id == target.identity.window_id), None)
            if current is None:
                raise YabaiError(YabaiFailure.WINDOW_MISSING, 'The window has closed.')
            if current.identity != target.identity:
                raise YabaiError(YabaiFailure.OWNER_CHANGED, 'The window process has changed.')
            if target.fullscreen and target.minimized:
                raise YabaiError(YabaiFailure.UNSUPPORTED_WINDOW, 'A fullscreen target cannot be minimized.')
            if current.minimized and target.minimized is None:
                raise YabaiError(YabaiFailure.UNSUPPORTED_WINDOW, 'This target does not manage Dock state.')

            def dock(desired):
                attempts.append('minimize' if desired else 'deminimize')
                self.desktop.request_minimize(target.identity, session, desired,
                                               deadline=operation_deadline())

            def fullscreen(desired):
                attempts.append('focus')
                self.desktop.request_focus(target.identity, session, deadline=operation_deadline())
                attempts.append('enter_fullscreen' if desired else 'exit_fullscreen')
                self.desktop.request_fullscreen(target.identity, session, desired,
                                                 deadline=operation_deadline())

            deminimized = current.minimized and (not target.minimized or current.display_uuid != target.display_uuid)
            if deminimized:
                dock(False)
                current = replace(current, minimized=False)
            exited = current.fullscreen and (not target.fullscreen or current.display_uuid != target.display_uuid)
            if exited:
                fullscreen(False)
                current = replace(current, fullscreen=False)
            moved = current.display_uuid != target.display_uuid
            if moved:
                # Fullscreen exit/deminimize must complete before a normal-Space
                # move. This same fresh sample supplies its destination index.
                observed = self._ready(target, session, deadline=operation_deadline())
                attempts.append('move_to_display')
                self.desktop.request_move_to_display(target.identity, session, target.display_uuid, observed,
                                                      deadline=operation_deadline())
                current = replace(current, display_uuid=target.display_uuid)
            if target.fullscreen and not current.fullscreen:
                if moved or deminimized:
                    self._ready(target, session, display_uuid=target.display_uuid, deadline=operation_deadline())
                fullscreen(True)
            if target.minimized and not current.minimized:
                if moved or exited:
                    self._ready(target, session, display_uuid=target.display_uuid, deadline=operation_deadline())
                dock(True)
            return TransitionResult(target.identity, TransitionStatus.PENDING, None, tuple(attempts))
        except YabaiError as error:
            if error.reason in (YabaiFailure.WINDOW_MISSING, YabaiFailure.OWNER_CHANGED):
                status = TransitionStatus.SKIPPED
            elif error.reason in (YabaiFailure.IDENTITY_UNAVAILABLE, YabaiFailure.UNSUPPORTED_WINDOW,
                                  YabaiFailure.CAPABILITY_UNAVAILABLE, YabaiFailure.ACCESS_DENIED):
                status = TransitionStatus.UNSUPPORTED
            else:
                status = TransitionStatus.FAILED
            return TransitionResult(target.identity, status, error.reason.value, tuple(attempts))
        except IdentityUnavailable:
            return TransitionResult(target.identity, TransitionStatus.UNSUPPORTED,
                                    YabaiFailure.IDENTITY_UNAVAILABLE.value, tuple(attempts))
