"""Pure restore policy. Plans contain identities and targets, never commands.

An executor must re-observe ownership, monitor UUIDs, and normal destination
Spaces before each mutation. An accepted plan is not evidence of restoration.
"""

from dataclasses import asdict, dataclass
from collections import defaultdict
from enum import StrEnum

from spacekeeper.app_matching import restore_name_key
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
    PRESET_UNMATCHED = "preset_unmatched"
    FALLBACK_UNMATCHED = "fallback_unmatched"
    NAME_NO_MATCH = "name_no_match"
    NAME_AMBIGUOUS = "name_ambiguous"
    SESSION_MISMATCH = "session_mismatch"
    VISIBLE_WINDOW_UNAVAILABLE = "visible_window_unavailable"
    FOCUS_UNAVAILABLE = "focus_unavailable"


@dataclass(frozen=True)
class WindowTarget:
    identity: WindowIdentity
    display_uuid: str
    fullscreen: bool
    # None leaves Dock state outside this target's scope (presets and extras).
    minimized: bool | None = None


@dataclass(frozen=True)
class WindowDecision:
    window_id: int
    status: DecisionStatus
    reason: Reason
    target: WindowTarget | None = None
    monitor_fallback: bool = False
    source_window_id: int | None = None


@dataclass(frozen=True)
class VisibleTarget:
    display_uuid: str
    identity: WindowIdentity


@dataclass(frozen=True)
class PlanIssue:
    reason: Reason
    window_id: int | None = None
    display_uuid: str | None = None
    source_window_id: int | None = None


@dataclass(frozen=True)
class RestorePlan:
    accepted: bool
    windows: tuple[WindowDecision, ...] = ()
    visible: tuple[VisibleTarget, ...] = ()
    focus: WindowIdentity | None = None
    issues: tuple[PlanIssue, ...] = ()
    name_fallback: bool = False

    def to_dict(self) -> dict:
        """A machine-readable result; status values describe planning only."""
        return asdict(self)


def unsupported_reason(window: Window, *, allow_minimized: bool = False) -> Reason | None:
    if window.sticky or not window.root_window or (window.role, window.subrole) != ("AXWindow", "AXStandardWindow"):
        return Reason.PROTECTED_WINDOW
    if window.hidden or (window.minimized and (not allow_minimized or window.fullscreen)):
        return Reason.HIDDEN_OR_MINIMIZED
    if window.identity is None:
        return Reason.IDENTITY_UNAVAILABLE
    if window.display_uuid is None or window.space_id is None:
        return Reason.PLACEMENT_UNAVAILABLE
    return None


def _window_target(wanted: Window, actual: Window, connected: set[str], normal_displays: set[str],
                   *, source_window_id: int | None = None) -> WindowDecision:
    fallback = wanted.display_uuid not in connected
    destination = actual.display_uuid if fallback else wanted.display_uuid
    assert destination is not None and actual.identity is not None
    target = WindowTarget(actual.identity, destination, wanted.fullscreen, wanted.minimized)
    if actual.display_uuid != destination and destination not in normal_displays:
        return WindowDecision(actual.id, DecisionStatus.BLOCKED, Reason.NO_NORMAL_DESTINATION,
                              monitor_fallback=fallback, source_window_id=source_window_id)
    status = (DecisionStatus.UNCHANGED if (actual.display_uuid, actual.fullscreen, actual.minimized) ==
              (destination, wanted.fullscreen, wanted.minimized)
              else DecisionStatus.PLANNED)
    reason = Reason.ALREADY_MATCHES if status == DecisionStatus.UNCHANGED else Reason.RESTORE_SAVED
    return WindowDecision(actual.id, status, reason, target, fallback, source_window_id)


def _views(saved: Snapshot, targets: dict[int, WindowTarget | None], connected: set[str],
           issues: list[PlanIssue], *, name_fallback: bool = False):
    visible = []
    def unavailable(reason, window_id, display_uuid=None):
        if name_fallback:
            if not any(i.source_window_id == window_id for i in issues):
                issues.append(PlanIssue(reason, display_uuid=display_uuid, source_window_id=window_id))
        else:
            issues.append(PlanIssue(reason, window_id, display_uuid))
    for space in sorted((s for s in saved.spaces if s.visible), key=lambda s: s.display_uuid):
        if space.display_uuid not in connected:
            continue
        target = targets.get(space.visible_window_id)
        if target is not None and not target.minimized and target.display_uuid == space.display_uuid:
            visible.append(VisibleTarget(space.display_uuid, target.identity))
        elif space.visible_window_id is not None:
            unavailable(Reason.VISIBLE_WINDOW_UNAVAILABLE, space.visible_window_id, space.display_uuid)
    focused = next((w for w in saved.windows if w.focused), None)
    focus_target = targets.get(focused.id) if focused is not None else None
    if focus_target is not None and focus_target.minimized:
        focus_target = None
    if focused is not None and focus_target is None:
        unavailable(Reason.FOCUS_UNAVAILABLE, focused.id)
    if focus_target is not None and focused.display_uuid not in connected:
        # A window from a missing monitor now shares its fallback monitor with
        # that monitor's saved visible window. Final focus takes precedence;
        # two different fullscreen/desktop Spaces cannot both be visible there.
        visible = [target for target in visible if target.display_uuid != focus_target.display_uuid]
        visible.append(VisibleTarget(focus_target.display_uuid, focus_target.identity))
        visible.sort(key=lambda target: target.display_uuid)
    return tuple(visible), focus_target.identity if focus_target is not None else None


def plan_restore(saved: Snapshot, current: Snapshot, *, preserve: tuple[WindowRule, ...] = (),
                 fallback: bool = True) -> RestorePlan:
    """Return deterministic per-window targets and final visibility/focus intent.

    Same-session IDs require the original owner. Stale boot/login sessions can
    match unique app/title pairs (Code uses workspace suffixes) to fresh
    identities; other windows stay untouched. Different users and opt-out
    callers retain strict rejection.
    """
    if saved.session != current.session:
        if fallback and saved.session.uid == current.session.uid:
            return _name_restore(saved, current)
        return RestorePlan(False, issues=(PlanIssue(Reason.SESSION_MISMATCH),))

    live_windows = {w.id: w for w in current.windows}
    saved_ids = {w.id for w in saved.windows}
    connected = {d.uuid for d in current.displays}
    normal_displays = {s.display_uuid for s in current.spaces if not s.fullscreen}
    decisions = []

    for wanted in sorted(saved.windows, key=lambda w: w.id):
        if unsupported_reason(wanted) == Reason.PROTECTED_WINDOW:
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
        unsupported = (unsupported_reason(wanted, allow_minimized=True) or
                       unsupported_reason(actual, allow_minimized=True))
        if unsupported is not None:
            decisions.append(WindowDecision(wanted.id, DecisionStatus.UNSUPPORTED, unsupported))
            continue

        decisions.append(_window_target(wanted, actual, connected, normal_displays))

    for extra in sorted((w for w in current.windows if w.id not in saved_ids), key=lambda w: w.id):
        unsupported = unsupported_reason(extra)
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
    issues = []
    visible, focus = _views(saved, targets, connected, issues)
    return RestorePlan(True, tuple(decisions), visible, focus, tuple(issues))


def _name_restore(saved: Snapshot, current: Snapshot) -> RestorePlan:
    """A partial name restore never treats old window IDs as live references."""
    wanted_windows = [w for w in saved.windows if unsupported_reason(w) != Reason.PROTECTED_WINDOW]
    sources, live = defaultdict(list), defaultdict(list)
    for window in wanted_windows:
        sources[restore_name_key(window)].append(window)
    for window in current.windows:
        live[restore_name_key(window)].append(window)
    connected = {d.uuid for d in current.displays}
    normal_displays = {s.display_uuid for s in current.spaces if not s.fullscreen}
    decisions, targets, issues = {}, {}, []
    for wanted in sorted(wanted_windows, key=lambda w: w.id):
        unsupported = unsupported_reason(wanted, allow_minimized=True)
        if unsupported is not None:
            issues.append(PlanIssue(unsupported, source_window_id=wanted.id))
            continue
        candidates = live[restore_name_key(wanted)]
        if not candidates:
            issues.append(PlanIssue(Reason.NAME_NO_MATCH, source_window_id=wanted.id))
        elif len(sources[restore_name_key(wanted)]) != 1 or len(candidates) != 1:
            issues.append(PlanIssue(Reason.NAME_AMBIGUOUS, source_window_id=wanted.id))
        else:
            actual = candidates[0]
            unsupported = unsupported_reason(actual, allow_minimized=True)
            if unsupported is not None:
                issues.append(PlanIssue(unsupported, actual.id, source_window_id=wanted.id))
                continue
            decision = _window_target(wanted, actual, connected, normal_displays, source_window_id=wanted.id)
            decisions[actual.id] = decision
            targets[wanted.id] = decision.target
    for window in current.windows:
        if window.id not in decisions:
            decisions[window.id] = WindowDecision(window.id, DecisionStatus.EXCLUDED, Reason.FALLBACK_UNMATCHED)
    visible, focus = _views(saved, targets, connected, issues, name_fallback=True)
    return RestorePlan(True, tuple(decisions[id] for id in sorted(decisions)), visible, focus, tuple(issues), True)
