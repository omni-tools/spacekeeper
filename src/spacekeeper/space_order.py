"""Relative fullscreen-Space intent, resolved through live window ownership."""

from dataclasses import dataclass

from spacekeeper.snapshot import Snapshot, Space, WindowIdentity


@dataclass(frozen=True)
class FullscreenOrder:
    display_uuid: str
    # Members of each saved fullscreen Space, in saved relative Space order.
    windows: tuple[tuple[WindowIdentity, ...], ...]


class SpaceOrderError(ValueError):
    def __init__(self, reason: str, window_id: int):
        super().__init__(reason)
        self.reason, self.window_id = reason, window_id


def resolve_order(target: FullscreenOrder, snapshot: Snapshot) -> tuple[Space, ...]:
    """Require each saved group to occupy one distinct native fullscreen Space."""
    windows = {w.id: w for w in snapshot.windows}
    spaces = {s.id: s for s in snapshot.spaces}
    result = []
    for members in target.windows:
        group = []
        for identity in members:
            window = windows.get(identity.window_id)
            if window is None or window.identity != identity:
                raise SpaceOrderError('missing_or_changed_owner', identity.window_id)
            space = spaces.get(window.space_id)
            if (not window.fullscreen or window.minimized or window.hidden or window.sticky or
                    not window.root_window or (window.role, window.subrole) != ('AXWindow', 'AXStandardWindow') or
                    window.display_uuid != target.display_uuid or space is None or not space.fullscreen or
                    space.display_uuid != target.display_uuid):
                raise SpaceOrderError('fullscreen_order_target_changed', identity.window_id)
            group.append(space)
        if len({s.id for s in group}) != 1 or group[0].id in {s.id for s in result}:
            raise SpaceOrderError('fullscreen_order_ambiguous', members[0].window_id)
        result.append(group[0])
    return tuple(result)
