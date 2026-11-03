"""Pure restore policy. Plans contain identities and targets, never commands.

An executor must re-observe ownership, monitor UUIDs, and normal destination
Spaces before each mutation. An accepted plan is not evidence of restoration.
"""

from dataclasses import asdict, dataclass
from enum import StrEnum

from spacekeeper.matching import WindowRule
from spacekeeper.snapshot import Snapshot, Window, WindowIdentity


class DecisionStatus(StrEnum):
    PLANNED = "planned"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"
    EXCLUDED = "excluded"


class Reason(StrEnum):
    RESTORE_SAVED = "restore_saved"
    ALREADY_MATCHES = "already_matches"
    CLOSED = "closed"
    OWNER_CHANGED = "owner_changed"
    IDENTITY_UNAVAILABLE = "identity_unavailable"
    PROTECTED_WINDOW = "protected_window"
    HIDDEN_OR_MINIMIZED = "hidden_or_minimized"
    PLACEMENT_UNAVAILABLE = "placement_unavailable"
    NO_NORMAL_DESTINATION = "no_normal_destination"
    EXTRA_FULLSCREEN = "extra_fullscreen"
    EXTRA_ORDINARY = "extra_ordinary"
    PRESERVED = "preserved_extra"
    SESSION_MISMATCH = "session_mismatch"
    VISIBLE_WINDOW_UNAVAILABLE = "visible_window_unavailable"
    FOCUS_UNAVAILABLE = "focus_unavailable"


@dataclass(frozen=True)
class WindowTarget:
    identity: WindowIdentity
    display_uuid: str
    fullscreen: bool


@dataclass(frozen=True)
class WindowDecision:
    window_id: int
    status: DecisionStatus
    reason: Reason
    target: WindowTarget | None = None
    monitor_fallback: bool = False


@dataclass(frozen=True)
class VisibleTarget:
    display_uuid: str
    identity: WindowIdentity


@dataclass(frozen=True)
class PlanIssue:
    reason: Reason
    window_id: int | None = None
    display_uuid: str | None = None


@dataclass(frozen=True)
class RestorePlan:
    accepted: bool
    windows: tuple[WindowDecision, ...] = ()
    visible: tuple[VisibleTarget, ...] = ()
    focus: WindowIdentity | None = None
    issues: tuple[PlanIssue, ...] = ()

    def to_dict(self) -> dict:
        """A machine-readable result; status values describe planning only."""
        return asdict(self)


def _unsupported(window: Window) -> Reason | None:
    if window.sticky or not window.root_window or (window.role, window.subrole) != ("AXWindow", "AXStandardWindow"):
        return Reason.PROTECTED_WINDOW
    if window.hidden or window.minimized:
        return Reason.HIDDEN_OR_MINIMIZED
    if window.identity is None:
        return Reason.IDENTITY_UNAVAILABLE
    if window.display_uuid is None or window.space_id is None:
        return Reason.PLACEMENT_UNAVAILABLE
    return None


def plan_restore(saved: Snapshot, current: Snapshot, *, preserve: tuple[WindowRule, ...] = ()) -> RestorePlan:
    """Return deterministic per-window targets and final visibility/focus intent.

    Reused saved IDs are skipped and are not reclassified as extra windows.
    This prevents the extra-fullscreen policy from acting on an owner collision.
    """
    if saved.session != current.session:
        return RestorePlan(False, issues=(PlanIssue(Reason.SESSION_MISMATCH),))

    live_windows = {w.id: w for w in current.windows}
    saved_ids = {w.id for w in saved.windows}
    connected = {d.uuid for d in current.displays}
    normal_displays = {s.display_uuid for s in current.spaces if not s.fullscreen}
    decisions = []

    for wanted in sorted(saved.windows, key=lambda w: w.id):
        if _unsupported(wanted) == Reason.PROTECTED_WINDOW:
            decisions.append(WindowDecision(wanted.id, DecisionStatus.UNSUPPORTED, Reason.PROTECTED_WINDOW))
            continue
        actual = live_windows.get(wanted.id)
        if actual is None:
            decisions.append(WindowDecision(wanted.id, DecisionStatus.SKIPPED, Reason.CLOSED))
            continue
        if wanted.identity is None or actual.identity is None:
            decisions.append(WindowDecision(wanted.id, DecisionStatus.UNSUPPORTED, Reason.IDENTITY_UNAVAILABLE))
            continue
        if wanted.identity != actual.identity:
            decisions.append(WindowDecision(wanted.id, DecisionStatus.SKIPPED, Reason.OWNER_CHANGED))
            continue
        unsupported = _unsupported(wanted) or _unsupported(actual)
        if unsupported is not None:
            decisions.append(WindowDecision(wanted.id, DecisionStatus.UNSUPPORTED, unsupported))
            continue

        fallback = wanted.display_uuid not in connected
        destination = actual.display_uuid if fallback else wanted.display_uuid
        # Both observations have a known placement after the checks above.
        assert destination is not None and actual.identity is not None
        target = WindowTarget(actual.identity, destination, wanted.fullscreen)
        needs_move = actual.display_uuid != destination
        if needs_move and destination not in normal_displays:
            decisions.append(WindowDecision(wanted.id, DecisionStatus.BLOCKED, Reason.NO_NORMAL_DESTINATION,
                                            monitor_fallback=fallback))
        elif not needs_move and wanted.fullscreen == actual.fullscreen:
            decisions.append(WindowDecision(wanted.id, DecisionStatus.UNCHANGED, Reason.ALREADY_MATCHES,
                                            target, fallback))
        else:
            decisions.append(WindowDecision(wanted.id, DecisionStatus.PLANNED, Reason.RESTORE_SAVED,
                                            target, fallback))

    for extra in sorted((w for w in current.windows if w.id not in saved_ids), key=lambda w: w.id):
        unsupported = _unsupported(extra)
        if unsupported is not None:
            decisions.append(WindowDecision(extra.id, DecisionStatus.UNSUPPORTED, unsupported))
        elif not extra.fullscreen:
            decisions.append(WindowDecision(extra.id, DecisionStatus.UNCHANGED, Reason.EXTRA_ORDINARY))
        elif any(rule.matches(extra) for rule in preserve):
            decisions.append(WindowDecision(extra.id, DecisionStatus.EXCLUDED, Reason.PRESERVED))
        else:
            assert extra.identity is not None and extra.display_uuid is not None
            decisions.append(WindowDecision(extra.id, DecisionStatus.PLANNED, Reason.EXTRA_FULLSCREEN,
                                            WindowTarget(extra.identity, extra.display_uuid, False)))

    targets = {d.window_id: d.target for d in decisions if d.target is not None}
    visible, issues = [], []
    for space in sorted((s for s in saved.spaces if s.visible), key=lambda s: s.display_uuid):
        if space.display_uuid not in connected:
            continue
        target = targets.get(space.visible_window_id)
        if target is not None and target.display_uuid == space.display_uuid:
            visible.append(VisibleTarget(space.display_uuid, target.identity))
        elif space.visible_window_id is not None:
            issues.append(PlanIssue(Reason.VISIBLE_WINDOW_UNAVAILABLE, space.visible_window_id, space.display_uuid))

    focused = next((w for w in saved.windows if w.focused), None)
    focus_target = targets.get(focused.id) if focused is not None else None
    if focused is not None and focus_target is None:
        issues.append(PlanIssue(Reason.FOCUS_UNAVAILABLE, focused.id))

    return RestorePlan(True, tuple(decisions), tuple(visible),
                       focus_target.identity if focus_target is not None else None, tuple(issues))
