"""Bounded yabai queries and guarded window operations; no storage or CLI IO."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import json
import math
import os
from pathlib import Path
import time
from typing import Callable

from spacekeeper.identity import IdentityReader, IdentityUnavailable
from spacekeeper.accessibility import AccessibilityError, FullscreenSetter
from spacekeeper.process import CommandRunner, ProcessError, ProcessFailure, SubprocessRunner
from spacekeeper.snapshot import (
    Display, Frame, ProcessIdentity, SessionIdentity, Snapshot, SnapshotError, Space, Window, WindowIdentity,
)
from spacekeeper.space_order import FullscreenOrder, SpaceOrderError, resolve_order


# Placement is joined from Space membership. Asking yabai to resolve space/display
# for every window repeats expensive WindowServer lookups on large desktops.
WINDOW_PROPERTIES = ('id,pid,app,title,frame,role,subrole,root-window,has-focus,'
                     'is-native-fullscreen,is-minimized,is-hidden,is-sticky,can-move,can-resize')
SPACE_PROPERTIES = 'id,uuid,index,display,windows,is-native-fullscreen,is-visible,has-focus'


@dataclass(frozen=True)
class WindowObservation:
    """Fresh target and topology, with Space membership scoped to this window.

    This is not a whole-desktop snapshot and must never be persisted as a backup.
    """
    session: SessionIdentity
    window: Window
    displays: tuple[Display, ...]
    spaces: tuple[Space, ...]

    @property
    def focus_ready(self) -> bool:
        return _focus_ready(self, self.window)


class YabaiFailure(StrEnum):
    NOT_FOUND = "not_found"
    COMMAND_FAILED = "command_failed"
    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output_limit"
    INVALID_RESPONSE = "invalid_response"
    DESKTOP_CHANGED = "desktop_changed"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    ACCESS_DENIED = "access_denied"
    SERVICE_UNAVAILABLE = "service_unavailable"
    SESSION_CHANGED = "session_changed"
    WINDOW_MISSING = "window_missing"
    OWNER_CHANGED = "owner_changed"
    IDENTITY_UNAVAILABLE = "identity_unavailable"
    UNSUPPORTED_WINDOW = "unsupported_window"
    DISPLAY_MISSING = "display_missing"
    NO_NORMAL_SPACE = "no_normal_space"
    DESTINATION_CHANGED = "destination_changed"
    WINDOW_STATE_CHANGED = "window_state_changed"
    FOCUS_CHANGED = "focus_changed"


class YabaiError(RuntimeError):
    def __init__(self, reason: YabaiFailure, message: str, *, returncode: int | None = None,
                 stage: str | None = None):
        super().__init__(message)
        self.reason = reason
        self.returncode = returncode
        self.stage = stage


def resolve_yabai(executable: str | None = None) -> str:
    if executable is not None:
        if not isinstance(executable, str) or not Path(executable).is_absolute() or "\0" in executable:
            raise ValueError("The yabai executable must be an absolute path.")
        return executable
    directories = ["/opt/homebrew/bin", "/usr/local/bin"]
    directories.extend(part for part in os.get_exec_path() if Path(part).is_absolute())
    for directory in directories:
        candidate = Path(directory) / "yabai"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise YabaiError(YabaiFailure.NOT_FOUND, "yabai was not found; configure an absolute executable path.")


class Yabai:
    def __init__(self, *, executable: str | None = None, runner: CommandRunner | None = None,
                 identities: IdentityReader | None = None, command_timeout: float = 10,
                 output_limit: int = 4 * 1024 * 1024, capture_attempts: int = 3,
                 clock: Callable[[], float] = time.monotonic, fullscreen_setter=None):
        if not math.isfinite(command_timeout) or command_timeout <= 0:
            raise ValueError("command_timeout must be positive and finite.")
        if type(output_limit) is not int or output_limit < 1:
            raise ValueError("output_limit must be positive.")
        if type(capture_attempts) is not int or capture_attempts < 1:
            raise ValueError("capture_attempts must be positive.")
        self.executable = resolve_yabai(executable)
        self.runner = runner if runner is not None else SubprocessRunner()
        self.identities = identities if identities is not None else IdentityReader()
        self.command_timeout = command_timeout
        self.output_limit = output_limit
        self.capture_attempts = capture_attempts
        self.clock = clock
        self.fullscreen_setter = fullscreen_setter

    def _command(self, *args: str, deadline: float | None = None) -> bytes:
        timeout = self.command_timeout if deadline is None else min(self.command_timeout, deadline - self.clock())
        if timeout <= 0:
            raise YabaiError(YabaiFailure.TIMEOUT, "The operation deadline expired.")
        try:
            result = self.runner((self.executable, *args), timeout=timeout, output_limit=self.output_limit)
        except ProcessError as error:
            reasons = {ProcessFailure.NOT_FOUND: YabaiFailure.NOT_FOUND, ProcessFailure.TIMEOUT: YabaiFailure.TIMEOUT,
                       ProcessFailure.OUTPUT_LIMIT: YabaiFailure.OUTPUT_LIMIT}
            raise YabaiError(reasons.get(error.reason, YabaiFailure.COMMAND_FAILED), str(error)) from error
        if len(result.stdout) + len(result.stderr) > self.output_limit:
            raise YabaiError(YabaiFailure.OUTPUT_LIMIT, "yabai output exceeded its limit.")
        if result.returncode != 0:
            diagnostic = result.stderr.decode("utf-8", errors="replace").lower()
            if ("--window" in args and "query" in args and
                    ("could not retrieve window details" in diagnostic or
                     "could not locate window with the specified id" in diagnostic)):
                reason = YabaiFailure.WINDOW_MISSING
            elif "scripting-addition" in diagnostic or "scripting addition" in diagnostic:
                reason = YabaiFailure.CAPABILITY_UNAVAILABLE
            elif "accessibility" in diagnostic or "permission" in diagnostic:
                reason = YabaiFailure.ACCESS_DENIED
            elif "socket" in diagnostic or "connect" in diagnostic:
                reason = YabaiFailure.SERVICE_UNAVAILABLE
            else:
                reason = YabaiFailure.COMMAND_FAILED
            # Raw command output may contain private titles; do not echo it.
            raise YabaiError(reason, f"yabai command failed: {reason}.", returncode=result.returncode)
        return result.stdout

    def query(self, domain: str, *, properties: str | None = None, window_id: int | None = None,
              deadline: float | None = None) -> list[dict]:
        if domain not in ("displays", "spaces", "windows"):
            raise ValueError("Unknown yabai query domain.")
        if window_id is not None and (domain != 'windows' or type(window_id) is not int or window_id < 1):
            raise ValueError('A positive window ID may only select a window query.')
        args = ('-m', 'query', '--' + domain)
        if properties is not None:
            args += (properties,)
        if window_id is not None:
            args += ('--window', str(window_id))
        def pairs(items):
            if len(dict(items)) != len(items):
                raise ValueError("Duplicate object keys.")
            return dict(items)
        try:
            data = json.loads(self._command(*args, deadline=deadline), object_pairs_hook=pairs)
            if window_id is not None:
                if type(data) is not dict or type(data.get('id')) is not int or data['id'] != window_id:
                    raise ValueError
                data = [data]
            if type(data) is not list or not all(type(item) is dict for item in data):
                raise ValueError
            return data
        except YabaiError as error:
            if error.reason == YabaiFailure.TIMEOUT:
                raise YabaiError(YabaiFailure.TIMEOUT, f"yabai {domain} query timed out.") from error
            raise
        except (ValueError, UnicodeDecodeError, RecursionError) as error:
            raise YabaiError(YabaiFailure.INVALID_RESPONSE, f"Invalid yabai {domain} response.") from error

    def _queries(self, deadline: float | None) -> dict[str, list[dict]]:
        domains = ("displays", "spaces", "windows")
        with ThreadPoolExecutor(max_workers=3) as pool:
            raw = dict(zip(domains, pool.map(
                lambda domain: self.query(domain, properties=WINDOW_PROPERTIES if domain == 'windows' else None,
                                          deadline=deadline), domains)))
        try:
            membership = {}
            for space in raw['spaces']:
                for window_id in space['windows']:
                    membership.setdefault(window_id, []).append(space)
            for window in raw['windows']:
                matches = membership.get(window['id'], [])
                if len(matches) == 1:
                    window.update(space=matches[0]['index'], display=matches[0]['display'])
                else:
                    # Sticky windows can belong to several Spaces; hidden/system
                    # windows can have none. Preserve their actual placement.
                    placement = self.query('windows', properties='id,pid,display,space',
                                           window_id=window['id'], deadline=deadline)[0]
                    if placement['pid'] != window['pid']:
                        raise YabaiError(YabaiFailure.DESKTOP_CHANGED, 'Window owner changed during capture.')
                    window.update(space=placement['space'], display=placement['display'])
        except YabaiError as error:
            if error.reason == YabaiFailure.WINDOW_MISSING:
                raise YabaiError(YabaiFailure.DESKTOP_CHANGED, 'Window closed during capture.') from error
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise YabaiError(YabaiFailure.INVALID_RESPONSE, 'Invalid yabai placement data.') from error
        return raw

    def _owners(self, raw_windows: list[dict]) -> tuple[dict[int, ProcessIdentity | None], dict[str, str]]:
        owners, issues = {}, {}
        for raw in raw_windows:
            pid = raw.get("pid")
            if type(pid) is not int or not 0 < pid <= 0x7FFFFFFF:
                raise YabaiError(YabaiFailure.INVALID_RESPONSE, "Invalid window owner PID.")
            if pid not in owners:
                try:
                    owners[pid] = self.identities.process(pid)
                except IdentityUnavailable as error:
                    owners[pid] = None
                    issues[str(pid)] = error.reason.value
        return owners, issues

    def _window_queries(self, window_id: int, deadline: float | None) -> dict[str, list[dict]]:
        domains = ('displays', 'spaces', 'windows')
        properties = {'displays': 'id,uuid,index,frame', 'spaces': SPACE_PROPERTIES,
                      'windows': WINDOW_PROPERTIES + ',display,space'}
        with ThreadPoolExecutor(max_workers=3) as pool:
            raw = dict(zip(domains, pool.map(
                lambda domain: self.query(domain, properties=properties[domain],
                                          window_id=window_id if domain == 'windows' else None,
                                          deadline=deadline), domains)))
        try:
            # Other windows are irrelevant to a target guard. Full observations
            # at backup/final verification still validate the entire desktop.
            for space in raw['spaces']:
                if type(space['windows']) is not list or any(type(wid) is not int for wid in space['windows']):
                    raise ValueError('Invalid membership.')
                space['windows'] = [wid for wid in space['windows'] if wid == window_id]
            window = raw['windows'][0]
            if type(window.get('space')) is int and window['space'] > 0:
                if any(type(space.get('index')) is not int for space in raw['spaces']):
                    raise ValueError('Invalid Space index.')
                space = next((s for s in raw['spaces'] if s['index'] == window['space']), None)
                if space is None or window_id not in space['windows']:
                    # Native fullscreen can expose the window before the Space
                    # query includes its new membership. Poll without resending
                    # the toggle; never normalize this gap into a stable sample.
                    raise YabaiError(YabaiFailure.DESKTOP_CHANGED, 'Target Space membership is still changing.')
        except (KeyError, TypeError, ValueError) as error:
            raise YabaiError(YabaiFailure.INVALID_RESPONSE, 'Invalid yabai Space membership.') from error
        return raw

    def _read(self, deadline: float | None, window_id: int | None = None) -> Snapshot:
        """Read two coherent passes, retrying topology/state/owner changes.

        Titles may change during a read; they are metadata, not identity. Raw
        queries are retained in memory only. This is not a macOS transaction.
        """
        read = self._queries if window_id is None else lambda deadline: self._window_queries(window_id, deadline)
        for _ in range(self.capture_attempts):
            started = datetime.now().astimezone().isoformat()
            session = self.identities.session()
            try:
                before = read(deadline)
                owners_before, _ = self._owners(before["windows"])
                after = read(deadline)
            except YabaiError as error:
                if error.reason == YabaiFailure.DESKTOP_CHANGED:
                    continue
                raise
            owners, issues = self._owners(after["windows"])
            if session != self.identities.session() or owners_before != owners:
                continue
            if _signature(before) != _signature(after):
                continue
            try:
                return _normalize(after, owners, session, started, issues)
            except (SnapshotError, KeyError, TypeError, ValueError) as error:
                raise YabaiError(YabaiFailure.INVALID_RESPONSE, "yabai returned an inconsistent observation.") from error
        raise YabaiError(YabaiFailure.DESKTOP_CHANGED, "Desktop or ownership changed during observation; retry later.")

    def observe(self, *, deadline: float | None = None) -> Snapshot:
        """Observe every window for capture, backup, planning, and final verification."""
        return self._read(deadline)

    def observe_window(self, identity: WindowIdentity, session: SessionIdentity,
                       *, deadline: float | None = None) -> WindowObservation:
        """Observe and guard only this owner/window against fresh global topology."""
        observed = self._read(deadline, identity.window_id)
        if observed.session != session:
            raise YabaiError(YabaiFailure.SESSION_CHANGED, "The graphical session has changed.")
        window = next((w for w in observed.windows if w.id == identity.window_id), None)
        if window is None:
            raise YabaiError(YabaiFailure.WINDOW_MISSING, "The window has closed.")
        if window.identity is None:
            raise YabaiError(YabaiFailure.IDENTITY_UNAVAILABLE, "Window ownership cannot be established.")
        if window.identity != identity:
            raise YabaiError(YabaiFailure.OWNER_CHANGED, "The window's owner has changed.")
        if (window.sticky or window.hidden or not window.root_window or
                (window.role, window.subrole) != ("AXWindow", "AXStandardWindow") or
                window.display_uuid is None or window.space_id is None):
            raise YabaiError(YabaiFailure.UNSUPPORTED_WINDOW, "The window is not a supported restore target.")
        return WindowObservation(observed.session, window, observed.displays, observed.spaces)

    def _guard(self, identity: WindowIdentity, session: SessionIdentity,
               deadline: float | None, *, allow_minimized: bool = False) -> tuple[WindowObservation, Window]:
        observed = self.observe_window(identity, session, deadline=deadline)
        if observed.window.minimized and not allow_minimized:
            raise YabaiError(YabaiFailure.UNSUPPORTED_WINDOW, "Restore a minimized window before focus or placement changes.")
        return observed, observed.window

    def check_owner(self, identity: WindowIdentity, session: SessionIdentity) -> None:
        if self.identities.session() != session:
            raise YabaiError(YabaiFailure.SESSION_CHANGED, 'The graphical session has changed.', stage='owner.session')
        if self.identities.process(identity.owner.pid) != identity.owner:
            raise YabaiError(YabaiFailure.OWNER_CHANGED, 'The window process has changed.', stage='owner.process')

    def _request_guard(self, identity: WindowIdentity, session: SessionIdentity,
                       deadline: float | None, *, allow_minimized: bool = False,
                       no_op_dock_state: bool | None = None) -> dict:
        """One targeted read, plus fresh cheap ownership checks around it.

        Requests do not promise postconditions. The coordinator must verify all
        results globally; callers must refresh topology at dependent moves.
        """
        self.check_owner(identity, session)
        try:
            raw = self.query('windows', window_id=identity.window_id, deadline=deadline,
                             properties='id,pid,role,subrole,root-window,is-hidden,is-sticky,is-minimized,has-focus')[0]
        except YabaiError as error:
            error.stage = error.stage or 'window.query'
            raise
        self.check_owner(identity, session)
        try:
            if raw['pid'] != identity.owner.pid:
                raise YabaiError(YabaiFailure.OWNER_CHANGED, 'The window has a different process owner.')
            flags = ('root-window', 'is-hidden', 'is-sticky', 'is-minimized', 'has-focus')
            if any(type(raw[key]) is not bool for key in flags):
                raise ValueError
            if no_op_dock_state is not None and raw['is-minimized'] == no_op_dock_state:
                # No edit is authorized or needed. A changed AX role after a
                # completed minimize must not turn a verified no-op into failure.
                return raw
            if (raw['is-hidden'] or raw['is-sticky'] or not raw['root-window'] or
                    (raw['role'], raw['subrole']) != ('AXWindow', 'AXStandardWindow')):
                raise YabaiError(YabaiFailure.UNSUPPORTED_WINDOW, 'The window is not a supported restore target.')
            if raw['is-minimized'] and not allow_minimized:
                raise YabaiError(YabaiFailure.WINDOW_STATE_CHANGED, 'The window is still minimized.')
        except (KeyError, TypeError, ValueError) as error:
            raise YabaiError(YabaiFailure.INVALID_RESPONSE, 'Invalid window request guard.') from error
        return raw

    def request_focus(self, identity: WindowIdentity, session: SessionIdentity,
                      *, deadline: float | None = None, force: bool = False) -> None:
        raw = self._request_guard(identity, session, deadline)
        if force or not raw['has-focus']:
            self._command('-m', 'window', '--focus', str(identity.window_id), deadline=deadline)

    def request_minimize(self, identity: WindowIdentity, session: SessionIdentity, desired: bool,
                         *, deadline: float | None = None) -> None:
        if type(desired) is not bool:
            raise ValueError('desired must be a boolean.')
        raw = self._request_guard(identity, session, deadline, allow_minimized=True, no_op_dock_state=desired)
        if raw['is-minimized'] == desired:
            return
        try:
            self._command('-m', 'window', '--minimize' if desired else '--deminimize',
                          str(identity.window_id), deadline=deadline)
        except YabaiError as error:
            if error.reason != YabaiFailure.COMMAND_FAILED:
                raise
            # yabai rejects an already-minimized/deminimized window. An
            # asynchronous request can finish between the guard and command.
            # Re-read that identity instead of retrying or claiming failure.
            actual = self._request_guard(identity, session, deadline, allow_minimized=True, no_op_dock_state=desired)
            if actual['is-minimized'] != desired:
                raise error

    def request_fullscreen(self, identity: WindowIdentity, session: SessionIdentity, desired: bool,
                           *, deadline: float | None = None) -> None:
        if type(desired) is not bool:
            raise ValueError('desired must be a boolean.')
        self._request_guard(identity, session, deadline)
        timeout = self.command_timeout if deadline is None else min(self.command_timeout, deadline - self.clock())
        if timeout <= 0:
            raise YabaiError(YabaiFailure.TIMEOUT, 'The fullscreen request deadline expired.')
        try:
            if self.fullscreen_setter is None:
                self.fullscreen_setter = FullscreenSetter(clock=self.clock)
            self.fullscreen_setter.set_fullscreen(identity, desired, timeout=timeout,
                                                 guard=lambda: self.check_owner(identity, session))
        except AccessibilityError as error:
            raise YabaiError(YabaiFailure(error.reason), str(error),
                             stage=error.stage or 'fullscreen.native') from error

    def request_move_to_display(self, identity: WindowIdentity, session: SessionIdentity, display_uuid: str,
                                observed: WindowObservation, *, deadline: float | None = None) -> None:
        if observed.session != session or observed.window.identity != identity:
            raise YabaiError(YabaiFailure.OWNER_CHANGED, 'The move observation has changed identity.')
        if observed.window.fullscreen or observed.window.minimized:
            raise YabaiError(YabaiFailure.WINDOW_STATE_CHANGED, 'The window is not ready to move.')
        if display_uuid not in {d.uuid for d in observed.displays}:
            raise YabaiError(YabaiFailure.DISPLAY_MISSING, 'The destination monitor disconnected.')
        candidates = sorted((s for s in observed.spaces if s.display_uuid == display_uuid and not s.fullscreen),
                            key=lambda s: s.index)
        if not candidates:
            raise YabaiError(YabaiFailure.NO_NORMAL_SPACE, 'No normal destination Space is available.')
        self.check_owner(identity, session)
        self._command('-m', 'window', str(identity.window_id), '--space', str(candidates[0].index), deadline=deadline)

    def minimize(self, identity: WindowIdentity, session: SessionIdentity, desired: bool,
                 *, deadline: float | None = None) -> None:
        if type(desired) is not bool:
            raise ValueError("desired must be a boolean.")
        _, window = self._guard(identity, session, deadline, allow_minimized=True)
        if window.minimized == desired:
            return
        if window.fullscreen:
            raise YabaiError(YabaiFailure.WINDOW_STATE_CHANGED, "Exit fullscreen before minimizing a window.")
        self._command("-m", "window", "--minimize" if desired else "--deminimize",
                      str(identity.window_id), deadline=deadline)

    def focus(self, identity: WindowIdentity, session: SessionIdentity,
              *, deadline: float | None = None) -> WindowObservation | None:
        observed, window = self._guard(identity, session, deadline)
        if _focus_ready(observed, window):
            return observed
        self._command("-m", "window", "--focus", str(identity.window_id), deadline=deadline)
        return None

    def fullscreen(self, identity: WindowIdentity, session: SessionIdentity, desired: bool,
                   display_uuid: str, *, deadline: float | None = None) -> None:
        if type(desired) is not bool:
            raise ValueError("desired must be a boolean.")
        observed, window = self._guard(identity, session, deadline)
        if window.display_uuid != display_uuid:
            raise YabaiError(YabaiFailure.WINDOW_STATE_CHANGED, "Window monitor changed before fullscreen transition.")
        if window.fullscreen == desired:
            return
        if not _focus_ready(observed, window):
            raise YabaiError(YabaiFailure.FOCUS_CHANGED, "Window or Space focus changed before fullscreen transition.")
        self._command("-m", "window", str(identity.window_id), "--toggle", "native-fullscreen", deadline=deadline)

    def move_to_display(self, identity: WindowIdentity, session: SessionIdentity, display_uuid: str,
                        *, deadline: float | None = None) -> None:
        observed, window = self._guard(identity, session, deadline)
        if window.fullscreen:
            raise YabaiError(YabaiFailure.WINDOW_STATE_CHANGED, "Exit fullscreen before moving a window.")
        if window.display_uuid == display_uuid:
            return
        if display_uuid not in {d.uuid for d in observed.displays}:
            raise YabaiError(YabaiFailure.DISPLAY_MISSING, "The destination monitor is disconnected.")
        candidates = sorted((s for s in observed.spaces if s.display_uuid == display_uuid and not s.fullscreen),
                            key=lambda s: s.index)
        if not candidates:
            raise YabaiError(YabaiFailure.NO_NORMAL_SPACE, "No normal destination Space is available.")
        selected_id = candidates[0].id
        # Focus/fullscreen operations change Mission Control indexes. Resolve again.
        observed, window = self._guard(identity, session, deadline)
        destination = next((s for s in observed.spaces if s.id == selected_id and
                            s.display_uuid == display_uuid and not s.fullscreen), None)
        if destination is None:
            raise YabaiError(YabaiFailure.DESTINATION_CHANGED, "The selected destination Space has changed.")
        if window.fullscreen:
            raise YabaiError(YabaiFailure.WINDOW_STATE_CHANGED, "Window entered fullscreen before its move.")
        self._command("-m", "window", str(identity.window_id), "--space", str(destination.index), deadline=deadline)

    def move_fullscreen_space_after(self, members: tuple[WindowIdentity, ...],
                                    predecessor: tuple[WindowIdentity, ...], session: SessionIdentity,
                                    display_uuid: str, *, deadline: float | None = None) -> None:
        # A global observation binds both owners and current Space indexes in
        # one stable topology. Never command a saved Space index or window ID.
        observed = self.observe(deadline=deadline)
        if observed.session != session:
            raise YabaiError(YabaiFailure.SESSION_CHANGED, 'The graphical session has changed.')
        try:
            anchor, moving = resolve_order(FullscreenOrder(display_uuid, (predecessor, members)), observed)
        except SpaceOrderError as error:
            raise YabaiError(YabaiFailure.WINDOW_STATE_CHANGED, str(error)) from error
        if moving.index > anchor.index:
            return
        if set(moving.window_ids) != {i.window_id for i in members}:
            raise YabaiError(YabaiFailure.UNSUPPORTED_WINDOW,
                             'Cannot reorder a fullscreen Space containing unrelated windows.')
        # Moving an earlier Space to a later Space inserts it after that Space.
        # This operation requires yabai's scripting addition; failure is reported.
        self._command('-m', 'space', str(moving.index), '--move', str(anchor.index), deadline=deadline)


def _focus_ready(observed: Snapshot | WindowObservation, window: Window) -> bool:
    return not window.minimized and not window.hidden and window.focused and any(
        s.id == window.space_id and s.display_uuid == window.display_uuid and s.visible and s.focused
        for s in observed.spaces)


def _signature(raw: dict[str, list[dict]]) -> str:
    # Titles and app labels can change without invalidating a window identity.
    windows = [{k: v for k, v in window.items() if k not in ("title", "app")}
               for window in raw["windows"]]
    data = {"displays": list(raw["displays"]), "spaces": list(raw["spaces"]), "windows": windows}
    # Query ordering is not an identity or topology change.
    for records in data.values():
        records.sort(key=lambda item: json.dumps(item, sort_keys=True))
    return json.dumps(data, sort_keys=True)


def _normalize(raw: dict[str, list[dict]], owners: dict[int, ProcessIdentity | None], session: SessionIdentity,
               started: str, issues: dict[str, str]) -> Snapshot:
    displays = tuple(Display(d["uuid"], d["index"], Frame(**d["frame"])) for d in raw["displays"])
    display_map = {d.index: d.uuid for d in displays}
    raw_spaces = {s["index"]: s for s in raw["spaces"]}
    if len(raw_spaces) != len(raw["spaces"]):
        raise SnapshotError("Duplicate Space indexes.")
    windows = []
    for w in raw["windows"]:
        space = raw_spaces.get(w["space"])
        if type(w["space"]) is not int or type(w["display"]) is not int:
            raise SnapshotError("Invalid window placement indexes.")
        if w["space"] != 0 and space is None:
            raise SnapshotError("Unknown window Space.")
        if w["display"] != 0 and w["display"] not in display_map:
            raise SnapshotError("Unknown window display.")
        windows.append(Window(w["id"], w["pid"], w["app"], w["title"], display_map.get(w["display"]),
                              space["id"] if space is not None else None, Frame(**w["frame"]),
                              w["is-native-fullscreen"], owners[w["pid"]], w["has-focus"], w["is-sticky"],
                              w["is-minimized"], w["is-hidden"], w["root-window"], w["role"], w["subrole"]))
    window_map = {w.id: w for w in windows}
    spaces = []
    for s in raw["spaces"]:
        if type(s["display"]) is not int or type(s["windows"]) is not list or type(s["uuid"]) is not str:
            raise SnapshotError("Invalid Space placement or membership.")
        members = tuple(s["windows"])
        candidates = [window_map[id] for id in members if id in window_map and
                      not (window_map[id].sticky or window_map[id].hidden or window_map[id].minimized) and
                      window_map[id].root_window and window_map[id].role == "AXWindow" and
                      window_map[id].subrole == "AXStandardWindow"]
        # yabai preserves the Space's window ordering. Prefer actual focus when known.
        selected = next((w for w in candidates if w.focused), candidates[0] if candidates else None)
        spaces.append(Space(s["id"], s["index"], display_map[s["display"]], s["is-native-fullscreen"],
                            s["is-visible"], members, s["uuid"] or None, s["has-focus"],
                            selected.id if selected is not None and s["is-visible"] else None))
    return Snapshot(session, started, datetime.now().astimezone().isoformat(), displays, tuple(spaces), tuple(windows),
                    metadata={"yabai": raw, "owner_issues": issues})
