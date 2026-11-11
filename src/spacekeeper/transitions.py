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
from spacekeeper.yabai import WindowObservation, YabaiError, YabaiFailure


class Desktop(Protocol):
    def observe(self, *, deadline: float | None = None) -> Snapshot: ...
    def observe_window(self, identity: WindowIdentity, session: SessionIdentity,
                       *, deadline: float | None = None) -> WindowObservation: ...
    def focus(self, identity: WindowIdentity, session: SessionIdentity,
              *, deadline: float | None = None) -> WindowObservation | None: ...
    def fullscreen(self, identity: WindowIdentity, session: SessionIdentity, desired: bool,
                   display_uuid: str, *, deadline: float | None = None) -> None: ...
    def minimize(self, identity: WindowIdentity, session: SessionIdentity, desired: bool,
                 *, deadline: float | None = None) -> None: ...
    def move_to_display(self, identity: WindowIdentity, session: SessionIdentity, display_uuid: str,
                        *, deadline: float | None = None) -> None: ...
    def move_fullscreen_space_after(self, members: tuple[WindowIdentity, ...],
                                    predecessor: tuple[WindowIdentity, ...], session: SessionIdentity,
                                    display_uuid: str, *, deadline: float | None = None) -> None: ...
    def request_focus(self, identity: WindowIdentity, session: SessionIdentity,
                      *, deadline: float | None = None, force: bool = False) -> None: ...
    def request_fullscreen(self, identity: WindowIdentity, session: SessionIdentity, desired: bool,
                           *, deadline: float | None = None) -> None: ...
    def request_minimize(self, identity: WindowIdentity, session: SessionIdentity, desired: bool,
                         *, deadline: float | None = None) -> None: ...
    def request_move_to_display(self, identity: WindowIdentity, session: SessionIdentity, display_uuid: str,
                                observed: WindowObservation, *, deadline: float | None = None) -> None: ...


class TransitionStatus(StrEnum):
    PENDING = "pending"
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

    def _checked(self, target: WindowTarget, session: SessionIdentity, deadline: float) -> tuple[WindowObservation, Window]:
        observed = self.desktop.observe_window(target.identity, session, deadline=deadline)
        if observed.window.minimized and target.minimized is None:
            raise YabaiError(YabaiFailure.UNSUPPORTED_WINDOW, "This target does not manage minimized state.")
        if target.display_uuid not in {d.uuid for d in observed.displays}:
            raise YabaiError(YabaiFailure.DISPLAY_MISSING, "The target monitor disconnected during transition.")
        return observed, observed.window

    def _wait(self, target: WindowTarget, session: SessionIdentity, deadline: float,
              predicate: Callable[[Window], bool], *, require_focus: bool = False,
              space_fullscreen: bool | None = None, initial: WindowObservation | None = None) -> Window:
        def matches(observed, window):
            space = next((s for s in observed.spaces if s.id == window.space_id), None)
            return (predicate(window) and
                    (not require_focus or (window.focused and space is not None and space.visible and space.focused)) and
                    (space_fullscreen is None or (space is not None and space.fullscreen == space_fullscreen)))
        consecutive = 0
        last_error = None
        if initial is not None and matches(initial, initial.window):
            consecutive = 1
            # Count the just-completed guard once, then require a new observation
            # after the usual interval. This never reuses a pre-mutation sample.
            self.sleep(min(self.interval, max(0, deadline - self.clock())))
        while self.clock() < deadline:
            try:
                observed, window = self._checked(target, session, deadline)
                last_error = None
                consecutive = consecutive + 1 if matches(observed, window) else 0
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

        VERIFIED refers only to this window's fullscreen/monitor/minimized postconditions;
        it does not verify the whole restore or final visibility/focus.
        """
        attempts = []
        try:
            if target.minimized and target.fullscreen:
                raise YabaiError(YabaiFailure.UNSUPPORTED_WINDOW, "A fullscreen target cannot be minimized.")
            initial, current = self._checked(target, session, self.clock() + self.timeout)
            confirmed = False

            def focus(display_uuid: str, deadline: float) -> None:
                attempts.append("focus")
                focused = self.desktop.focus(target.identity, session, deadline=deadline)
                self._wait(target, session, deadline, lambda w: w.display_uuid == display_uuid,
                           require_focus=True, initial=focused)

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

            def change_minimized(desired: bool) -> Window:
                deadline = self.clock() + self.timeout
                attempts.append('minimize' if desired else 'deminimize')
                self.desktop.minimize(target.identity, session, desired, deadline=deadline)
                return self._wait(target, session, deadline, lambda w: w.minimized == desired)

            if current.minimized and (not target.minimized or current.display_uuid != target.display_uuid):
                current = change_minimized(False)
                confirmed = True

            if current.fullscreen and (not target.fullscreen or current.display_uuid != target.display_uuid):
                current = change_fullscreen(False, current.display_uuid)
                confirmed = True

            if current.display_uuid != target.display_uuid:
                deadline = self.clock() + self.timeout
                attempts.append("move_to_display")
                self.desktop.move_to_display(target.identity, session, target.display_uuid, deadline=deadline)
                current = self._wait(target, session, deadline,
                                     lambda w: not w.fullscreen and w.display_uuid == target.display_uuid,
                                     space_fullscreen=False)
                confirmed = True

            if target.fullscreen and not current.fullscreen:
                current = change_fullscreen(True, target.display_uuid)
                confirmed = True

            if target.minimized and not current.minimized:
                current = change_minimized(True)
                confirmed = True

            # The last successful wait already confirmed these postconditions
            # twice. No operation occurred since; do not poll the same result again.
            def matches(window):
                return (window.fullscreen == target.fullscreen and window.display_uuid == target.display_uuid and
                        (target.minimized is None or window.minimized == target.minimized))
            if not confirmed or not matches(current):
                self._wait(target, session, self.clock() + self.timeout,
                           matches,
                           space_fullscreen=target.fullscreen, initial=initial if not attempts else None)
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
