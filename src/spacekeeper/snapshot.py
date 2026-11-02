"""Validated observations shared by capture and restore planning.

Indexes describe an observation; UUIDs and owner identities drive matching.
``metadata`` retains additional JSON data (including raw yabai query results)
without making it part of the restore policy.
"""

from copy import deepcopy
from dataclasses import MISSING, asdict, dataclass, field, fields
from datetime import datetime
import json
import math
from uuid import UUID


SCHEMA_VERSION = 1


class SnapshotError(ValueError):
    """An observation is malformed or uses an unsupported schema."""


def _integer(value: int, label: str, minimum: int = 1) -> None:
    if type(value) is not int or value < minimum:
        raise SnapshotError(f"{label} must be an integer >= {minimum}.")


def _text(value: str, label: str, *, empty: bool = False) -> None:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise SnapshotError(f"{label} must be a string" + ("." if empty else " that is not empty."))


def _uuid(value: str, label: str) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError
        return str(UUID(value))
    except ValueError as error:
        raise SnapshotError(f"{label} must be a UUID.") from error


def _boolean_fields(record: object, *names: str) -> None:
    for name in names:
        if type(getattr(record, name)) is not bool:
            raise SnapshotError(f"{name} must be a boolean.")


def _unique(values: list, label: str) -> None:
    if len(set(values)) != len(values):
        raise SnapshotError(f"Duplicate {label}.")


@dataclass(frozen=True)
class SessionIdentity:
    boot_uuid: str
    uid: int
    login_session_id: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "boot_uuid", _uuid(self.boot_uuid, "boot_uuid"))
        _integer(self.uid, "uid", 0)
        _integer(self.login_session_id, "login_session_id")
        if self.login_session_id >= 0xFFFFFFFE:
            raise SnapshotError("login_session_id must identify a concrete session.")


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_seconds: int
    start_microseconds: int

    def __post_init__(self) -> None:
        _integer(self.pid, "pid")
        if self.pid > 0x7FFFFFFF:
            raise SnapshotError("pid is outside the macOS pid_t range.")
        _integer(self.start_seconds, "start_seconds")
        _integer(self.start_microseconds, "start_microseconds", 0)
        if self.start_microseconds >= 1_000_000:
            raise SnapshotError("start_microseconds must be less than 1000000.")


@dataclass(frozen=True)
class WindowIdentity:
    window_id: int
    owner: ProcessIdentity

    def __post_init__(self) -> None:
        _integer(self.window_id, "window_id")
        if not isinstance(self.owner, ProcessIdentity):
            raise SnapshotError("owner must be a ProcessIdentity.")


@dataclass(frozen=True)
class Frame:
    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        for name in ("x", "y", "w", "h"):
            value = getattr(self, name)
            try:
                finite = type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise SnapshotError(f"Frame {name} must be a finite number.")
        if self.w < 0 or self.h < 0:
            raise SnapshotError("Frame dimensions cannot be negative.")


@dataclass(frozen=True)
class Display:
    uuid: str
    index: int
    frame: Frame

    def __post_init__(self) -> None:
        object.__setattr__(self, "uuid", _uuid(self.uuid, "Display uuid"))
        _integer(self.index, "Display index")
        if not isinstance(self.frame, Frame):
            raise SnapshotError("Display frame must be a Frame.")


@dataclass(frozen=True)
class Space:
    id: int
    index: int
    display_uuid: str
    fullscreen: bool
    visible: bool
    window_ids: tuple[int, ...] = ()
    uuid: str | None = None
    focused: bool = False
    visible_window_id: int | None = None

    def __post_init__(self) -> None:
        _integer(self.id, "Space id")
        _integer(self.index, "Space index")
        object.__setattr__(self, "display_uuid", _uuid(self.display_uuid, "Space display_uuid"))
        if self.uuid is not None:
            object.__setattr__(self, "uuid", _uuid(self.uuid, "Space uuid"))
        _boolean_fields(self, "fullscreen", "visible", "focused")
        if type(self.window_ids) is not tuple:
            raise SnapshotError("Space window_ids must be a tuple.")
        for window_id in self.window_ids:
            _integer(window_id, "Space window ID")
        _unique(list(self.window_ids), "Space window IDs")
        if self.visible_window_id is not None:
            _integer(self.visible_window_id, "visible_window_id")
            if not self.visible or self.visible_window_id not in self.window_ids:
                raise SnapshotError("A visible window must belong to a visible Space.")


@dataclass(frozen=True)
class Window:
    id: int
    pid: int
    app: str
    title: str
    display_uuid: str | None
    space_id: int | None
    frame: Frame
    fullscreen: bool
    owner: ProcessIdentity | None
    focused: bool = False
    sticky: bool = False
    minimized: bool = False
    hidden: bool = False
    root_window: bool = True
    role: str = "AXWindow"
    subrole: str = "AXStandardWindow"

    def __post_init__(self) -> None:
        _integer(self.id, "Window id")
        _integer(self.pid, "Window pid")
        _text(self.app, "app")
        _text(self.title, "title", empty=True)
        _text(self.role, "role", empty=True)
        _text(self.subrole, "subrole", empty=True)
        if self.display_uuid is not None:
            object.__setattr__(self, "display_uuid", _uuid(self.display_uuid, "Window display_uuid"))
        if self.space_id is not None:
            _integer(self.space_id, "Window space_id")
        if not isinstance(self.frame, Frame):
            raise SnapshotError("Window frame must be a Frame.")
        if self.owner is not None:
            if not isinstance(self.owner, ProcessIdentity) or self.owner.pid != self.pid:
                raise SnapshotError("Window owner must identify the window's PID.")
        _boolean_fields(self, "fullscreen", "focused", "sticky", "minimized", "hidden", "root_window")

    @property
    def identity(self) -> WindowIdentity | None:
        return WindowIdentity(self.id, self.owner) if self.owner is not None else None


def _timestamp(value: str, label: str) -> datetime:
    _text(value, label)
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    except ValueError as error:
        raise SnapshotError(f"{label} must be an ISO timestamp with a timezone.") from error


def _json_value(value: object) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise SnapshotError("metadata must contain only finite JSON values with string keys.")


@dataclass(frozen=True)
class Snapshot:
    session: SessionIdentity
    capture_started_at: str
    captured_at: str
    displays: tuple[Display, ...]
    spaces: tuple[Space, ...]
    windows: tuple[Window, ...]
    schema_version: int = SCHEMA_VERSION
    metadata: dict[str, object] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise SnapshotError("Unsupported snapshot schema_version.")
        if not isinstance(self.session, SessionIdentity):
            raise SnapshotError("session must be a SessionIdentity.")
        if _timestamp(self.capture_started_at, "capture_started_at") > _timestamp(self.captured_at, "captured_at"):
            raise SnapshotError("Capture cannot end before it starts.")
        for name, kind in (("displays", Display), ("spaces", Space), ("windows", Window)):
            records = getattr(self, name)
            if type(records) is not tuple or not all(isinstance(item, kind) for item in records):
                raise SnapshotError(f"{name} must be a tuple of {kind.__name__} records.")
        if type(self.metadata) is not dict:
            raise SnapshotError("metadata must be an object.")
        _json_value(self.metadata)
        object.__setattr__(self, "metadata", deepcopy(self.metadata))
        _unique([d.uuid for d in self.displays], "display UUIDs")
        _unique([d.index for d in self.displays], "display indexes")
        _unique([s.id for s in self.spaces], "Space IDs")
        _unique([s.index for s in self.spaces], "Space indexes")
        _unique([s.uuid for s in self.spaces if s.uuid is not None], "Space UUIDs")
        _unique([w.id for w in self.windows], "window IDs")
        if sum(w.focused for w in self.windows) > 1 or sum(s.focused for s in self.spaces) > 1:
            raise SnapshotError("Only one window and one Space may have focus.")
        _unique([s.display_uuid for s in self.spaces if s.visible], "visible Spaces per display")
        displays = {d.uuid for d in self.displays}
        spaces = {s.id: s for s in self.spaces}
        windows = {w.id: w for w in self.windows}
        owners: dict[int, ProcessIdentity] = {}
        for window in self.windows:
            if window.owner is not None:
                if window.pid in owners and owners[window.pid] != window.owner:
                    raise SnapshotError("Conflicting process identities within one observation.")
                owners[window.pid] = window.owner
            if window.display_uuid is not None and window.display_uuid not in displays:
                raise SnapshotError("Window refers to an unknown display.")
            if window.space_id is not None:
                if window.space_id not in spaces:
                    raise SnapshotError("Window refers to an unknown Space.")
                if window.display_uuid != spaces[window.space_id].display_uuid:
                    raise SnapshotError("Window and Space disagree on their display.")
                if window.id not in spaces[window.space_id].window_ids:
                    raise SnapshotError("Window is missing from its Space's membership.")
        for space in self.spaces:
            if space.display_uuid not in displays:
                raise SnapshotError("Space refers to an unknown display.")
            for window_id in space.window_ids:
                if window_id not in windows:
                    raise SnapshotError("Space refers to an unknown window.")
                window = windows[window_id]
                if not window.sticky and window.space_id != space.id:
                    raise SnapshotError("Space membership disagrees with the window's Space.")

    def to_dict(self) -> dict:
        """Return independent JSON-compatible data."""
        return json.loads(json.dumps(asdict(self), allow_nan=False))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: dict) -> "Snapshot":
        """Decode strictly: malformed types and unknown fields are errors."""
        data = _record(cls, data)
        if type(data.get("schema_version")) is not int or data["schema_version"] != SCHEMA_VERSION:
            raise SnapshotError("Unsupported or missing snapshot schema_version.")
        data["session"] = SessionIdentity(**_record(SessionIdentity, data["session"]))
        displays, spaces, windows = [], [], []
        for item in _array(data["displays"], "displays"):
            record = _record(Display, item)
            record["frame"] = Frame(**_record(Frame, record["frame"]))
            displays.append(Display(**record))
        for item in _array(data["spaces"], "spaces"):
            record = _record(Space, item)
            if "window_ids" in record:
                record["window_ids"] = tuple(_array(record["window_ids"], "window_ids"))
            spaces.append(Space(**record))
        for item in _array(data["windows"], "windows"):
            record = _record(Window, item)
            record["frame"] = Frame(**_record(Frame, record["frame"]))
            if record["owner"] is not None:
                record["owner"] = ProcessIdentity(**_record(ProcessIdentity, record["owner"]))
            windows.append(Window(**record))
        data.update(displays=tuple(displays), spaces=tuple(spaces), windows=tuple(windows))
        return cls(**data)

    @classmethod
    def from_json(cls, text: str) -> "Snapshot":
        def object_pairs(pairs: list[tuple[str, object]]) -> dict:
            _unique([key for key, _ in pairs], "JSON object keys")
            return dict(pairs)

        try:
            return cls.from_dict(json.loads(text, object_pairs_hook=object_pairs))
        except (TypeError, ValueError, RecursionError) as error:
            if isinstance(error, SnapshotError):
                raise
            raise SnapshotError("Invalid snapshot JSON.") from error


def _record(kind: type, value: object) -> dict:
    if type(value) is not dict:
        raise SnapshotError(f"{kind.__name__} must be an object.")
    definitions = fields(kind)
    allowed = {f.name for f in definitions}
    required = {f.name for f in definitions if f.default is MISSING and f.default_factory is MISSING}
    if value.keys() - allowed or required - value.keys():
        raise SnapshotError(f"Invalid fields in {kind.__name__}.")
    return dict(value)


def _array(value: object, label: str) -> list:
    if type(value) is not list:
        raise SnapshotError(f"{label} must be an array.")
    return value
