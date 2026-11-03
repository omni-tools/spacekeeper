"""Internal per-window transitions, exercised with an injected desktop.

The restore coordinator must persist its pre-restore backup and own the restore
lock before calling this engine. CLI wiring and that coordinator are separate.
This module neither creates backups nor exposes a standalone restore command.
"""

from dataclasses import asdict, dataclass
from enum import StrEnum
import math
import time
from typing import Callable, Protocol

from spacekeeper.identity import IdentityUnavailable
from spacekeeper.planner import WindowTarget
from spacekeeper.snapshot import SessionIdentity, Snapshot, Window, WindowIdentity
from spacekeeper.yabai import YabaiError, YabaiFailure


class Desktop(Protocol):
    def observe(self, *, deadline: float | None = None) -> Snapshot: ...
    def focus(self, identity: WindowIdentity, session: SessionIdentity, *, deadline: float | None = None) -> None: ...
    def fullscreen(self, identity: WindowIdentity, session: SessionIdentity, desired: bool,
                   display_uuid: str, *, deadline: float | None = None) -> None: ...
    def move_to_display(self, identity: WindowIdentity, session: SessionIdentity, display_uuid: str,
                        *, deadline: float | None = None) -> None: ...


class TransitionStatus(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class TransitionResult:
    identity: WindowIdentity
    status: TransitionStatus
    reason: str | None
    attempted_operations: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


class WindowTransitions:
    def __init__(self, desktop: Desktop, *, transition_timeout: float = 12, poll_interval: float = 0.2,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        if not all(math.isfinite(value) and value > 0 for value in (transition_timeout, poll_interval)):
            raise ValueError("Transition timeout and poll interval must be positive and finite.")
        self.desktop = desktop
        self.timeout = transition_timeout
        self.interval = poll_interval
        self.clock = clock
        self.sleep = sleep

    def _checked(self, target: WindowTarget, session: SessionIdentity, deadline: float) -> tuple[Snapshot, Window]:
        observed = self.desktop.observe(deadline=deadline)
        if observed.session != session:
            raise YabaiError(YabaiFailure.SESSION_CHANGED, "The graphical session changed during transition.")
        window = next((w for w in observed.windows if w.id == target.identity.window_id), None)
        if window is None:
            raise YabaiError(YabaiFailure.WINDOW_MISSING, "The window closed during transition.")
        if window.identity is None:
            raise YabaiError(YabaiFailure.IDENTITY_UNAVAILABLE, "Window ownership is unavailable.")
        if window.identity != target.identity:
            raise YabaiError(YabaiFailure.OWNER_CHANGED, "Window ownership changed during transition.")
        if (window.sticky or window.hidden or window.minimized or not window.root_window or
                (window.role, window.subrole) != ("AXWindow", "AXStandardWindow") or
                window.display_uuid is None or window.space_id is None):
            raise YabaiError(YabaiFailure.UNSUPPORTED_WINDOW, "The window is no longer a supported target.")
        if target.display_uuid not in {d.uuid for d in observed.displays}:
            raise YabaiError(YabaiFailure.DISPLAY_MISSING, "The target monitor disconnected during transition.")
        return observed, window

    def _wait(self, target: WindowTarget, session: SessionIdentity, deadline: float,
              predicate: Callable[[Window], bool], *, require_focus: bool = False,
              space_fullscreen: bool | None = None) -> Window:
        consecutive = 0
        last_error = None
        while self.clock() < deadline:
            try:
                observed, window = self._checked(target, session, deadline)
                last_error = None
                space = next((s for s in observed.spaces if s.id == window.space_id), None)
                matches = predicate(window)
                if require_focus:
                    matches = matches and window.focused and space is not None and space.visible and space.focused
                if space_fullscreen is not None:
                    matches = matches and space is not None and space.fullscreen == space_fullscreen
                consecutive = consecutive + 1 if matches else 0
            except YabaiError as error:
                if error.reason not in (YabaiFailure.DESKTOP_CHANGED, YabaiFailure.WINDOW_MISSING):
                    raise
                # Native fullscreen can briefly remove a surviving window from
                # query output. Wait for it to return, rechecking ownership each
                # time; neither a gap nor topology churn is a stable sample.
                consecutive = 0
                last_error = error
            if consecutive == 2 and self.clock() < deadline:
                return window
            self.sleep(min(self.interval, max(0, deadline - self.clock())))
        if last_error is not None and last_error.reason == YabaiFailure.WINDOW_MISSING:
            raise last_error
        raise YabaiError(YabaiFailure.TIMEOUT, "Window transition was not confirmed before its deadline.")

    def apply(self, target: WindowTarget, session: SessionIdentity) -> TransitionResult:
        """Apply one target sequentially. No later stage runs after a failure.

        VERIFIED refers only to this window's fullscreen/monitor postconditions;
        it does not verify the whole restore or final visibility/focus.
        """
        attempts = []
        try:
            _, current = self._checked(target, session, self.clock() + self.timeout)

            def focus(display_uuid: str, deadline: float) -> None:
                attempts.append("focus")
                self.desktop.focus(target.identity, session, deadline=deadline)
                self._wait(target, session, deadline, lambda w: w.display_uuid == display_uuid, require_focus=True)

            def change_fullscreen(desired: bool, display_uuid: str) -> Window:
                deadline = self.clock() + self.timeout
                for attempt in range(3):
                    focus(display_uuid, deadline)
                    attempts.append('enter_fullscreen' if desired else 'exit_fullscreen')
                    try:
                        self.desktop.fullscreen(target.identity, session, desired, display_uuid, deadline=deadline)
                    except YabaiError as error:
                        # Only this guard error guarantees no toggle was sent.
                        # Re-focus with fresh ownership checks, within the same
                        # deadline; never blindly retry an actual toggle.
                        if error.reason != YabaiFailure.FOCUS_CHANGED or attempt == 2:
                            raise
                        self.sleep(min(self.interval, max(0, deadline - self.clock())))
                        continue
                    return self._wait(target, session, deadline,
                                      lambda w: w.fullscreen == desired and (not desired or w.display_uuid == display_uuid),
                                      space_fullscreen=desired)
                raise YabaiError(YabaiFailure.TIMEOUT, 'Fullscreen transition did not complete.')

            if current.fullscreen and (not target.fullscreen or current.display_uuid != target.display_uuid):
                current = change_fullscreen(False, current.display_uuid)

            if current.display_uuid != target.display_uuid:
                deadline = self.clock() + self.timeout
                attempts.append("move_to_display")
                self.desktop.move_to_display(target.identity, session, target.display_uuid, deadline=deadline)
                current = self._wait(target, session, deadline,
                                     lambda w: not w.fullscreen and w.display_uuid == target.display_uuid,
                                     space_fullscreen=False)

            if target.fullscreen and not current.fullscreen:
                change_fullscreen(True, target.display_uuid)

            self._wait(target, session, self.clock() + self.timeout,
                       lambda w: w.fullscreen == target.fullscreen and w.display_uuid == target.display_uuid,
                       space_fullscreen=target.fullscreen)
            return TransitionResult(target.identity, TransitionStatus.VERIFIED, None, tuple(attempts))
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
