"""Small, explicitly synthetic desktop observations."""

from dataclasses import replace
from uuid import UUID

from spacekeeper.snapshot import Display, Frame, ProcessIdentity, SessionIdentity, Snapshot, Space, Window


A, B = str(UUID(int=1)), str(UUID(int=2))
SESSION = SessionIdentity(str(UUID(int=1000)), 501, 100)
FRAME = Frame(0, 0, 800, 600)
DISPLAYS = (Display(A, 1, FRAME), Display(B, 2, Frame(800, 0, 800, 600)))
STAMP = "2026-01-01T12:00:00+00:00"


def window(id: int = 1, *, fullscreen: bool = False, display: str = A, **changes) -> Window:
    pid = 100 + id
    space_id = 1000 + id if fullscreen else (101 if display == A else 102)
    value = Window(id, pid, "Synthetic Editor", "Synthetic document", display, space_id,
                   FRAME, fullscreen, ProcessIdentity(pid, 1_000_000, id))
    return replace(value, **changes)


def observe(*windows: Window, displays=DISPLAYS, normal_displays=(A, B), session=SESSION,
            visible_ids: tuple[int, ...] | None = None) -> Snapshot:
    spaces = {}
    for display in displays:
        if display.uuid in normal_displays:
            id = 101 if display.uuid == A else 102
            spaces[id] = Space(id, 1, display.uuid, False, False)
    for value in windows:
        if value.space_id is not None:
            spaces.setdefault(value.space_id, Space(value.space_id, 1, value.display_uuid, value.fullscreen, False))
    if visible_ids is None:
        by_display = {}
        for value in windows:
            if not value.hidden and not value.minimized and value.space_id is not None:
                by_display.setdefault(value.display_uuid, value.id)
        visible_ids = tuple(by_display.values())
    normalized = []
    for index, space in enumerate(sorted(spaces.values(), key=lambda s: s.id), start=1):
        members = tuple(w.id for w in windows if w.space_id == space.id)
        visible_id = next((id for id in visible_ids if id in members), None)
        normalized.append(replace(space, index=index, window_ids=members,
                                  visible=visible_id is not None, visible_window_id=visible_id,
                                  focused=any(w.focused and w.id in members for w in windows)))
    return Snapshot(session, STAMP, STAMP, tuple(displays), tuple(normalized), tuple(windows))
