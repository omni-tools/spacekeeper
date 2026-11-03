"""In-memory desktop speaking the real yabai argument/JSON protocol."""

from dataclasses import asdict, replace
import json
from threading import RLock

from spacekeeper.identity import IdentityFailure, IdentityUnavailable
from spacekeeper.process import ProcessResult
from tests.support import A, B, DISPLAYS, SESSION, observe


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class FakeYabai:
    def __init__(self, *windows):
        self.windows = {w.id: w for w in windows}
        self.displays = DISPLAYS
        self.normal_displays = (A, B)
        self.session_id = SESSION
        self.lock = RLock()
        self.commands = []
        self.timeouts = []
        self.query_counts = {domain: 0 for domain in ("displays", "spaces", "windows")}
        self.queries = []
        self.space_indexes = {}
        self.effects = {"focus": True, "toggle": True, "move": True}
        self.failures = {}
        self.on_query = lambda domain, count: None
        self.after_command = lambda operation: None
        self.on_process = lambda pid: None
        self.denied_owners = set()

    def session(self):
        return self.session_id

    def process(self, pid):
        with self.lock:
            self.on_process(pid)
            if pid in self.denied_owners:
                raise IdentityUnavailable(IdentityFailure.ACCESS_DENIED, "Synthetic permission denial.")
            value = next((w.owner for w in self.windows.values() if w.pid == pid), None)
            if value is None:
                raise IdentityUnavailable(IdentityFailure.PROCESS_MISSING, "Synthetic closed owner.")
            return value

    def raw(self):
        snapshot = observe(*self.windows.values(), displays=self.displays, normal_displays=self.normal_displays,
                           session=self.session_id)
        displays = [{"uuid": d.uuid, "index": d.index, "frame": asdict(d.frame)} for d in snapshot.displays]
        indexes = {s.id: self.space_indexes.get(s.id, s.index) for s in snapshot.spaces}
        display_indexes = {d.uuid: d.index for d in snapshot.displays}
        spaces = [{"id": s.id, "uuid": s.uuid or "", "index": indexes[s.id],
                   "display": display_indexes[s.display_uuid], "windows": list(s.window_ids),
                   "is-native-fullscreen": s.fullscreen, "is-visible": s.visible, "has-focus": s.focused}
                  for s in snapshot.spaces]
        windows = [{"id": w.id, "pid": w.pid, "app": w.app, "title": w.title,
                    "display": display_indexes.get(w.display_uuid, 0), "space": indexes.get(w.space_id, 0),
                    "frame": asdict(w.frame), "is-native-fullscreen": w.fullscreen, "has-focus": w.focused,
                    "is-sticky": w.sticky, "is-hidden": w.hidden, "is-minimized": w.minimized,
                    "root-window": w.root_window, "role": w.role, "subrole": w.subrole, "can-move": True}
                   for w in snapshot.windows]
        return {"displays": displays, "spaces": spaces, "windows": windows}

    def __call__(self, argv, *, timeout, output_limit):
        with self.lock:
            self.timeouts.append(timeout)
            args = argv[1:]
            if args[:2] == ("-m", "query"):
                self.queries.append(args)
                domain = args[2][2:]
                self.query_counts[domain] += 1
                self.on_query(domain, self.query_counts[domain])
                data = self.raw()[domain]
                if '--window' in args:
                    window_id = int(args[args.index('--window') + 1])
                    data = next((item for item in data if item['id'] == window_id), None)
                    if data is None:
                        return ProcessResult(1, b'', b'could not retrieve window details.')
                if len(args) > 3 and not args[3].startswith('--'):
                    properties = args[3].split(',')
                    def project(item):
                        return {key: value for key, value in item.items() if key in properties}
                    data = [project(item) for item in data] if isinstance(data, list) else project(data)
                return ProcessResult(0, json.dumps(data).encode(), b"")
            self.commands.append(args)
            if args[1:3] == ("window", "--focus"):
                operation, id = "focus", int(args[3])
            elif args[3] == "--space":
                operation, id = "move", int(args[2])
            else:
                operation, id = "toggle", int(args[2])
            if operation in self.failures:
                return ProcessResult(1, b"", self.failures[operation].encode())
            value = self.windows[id]
            if self.effects[operation]:
                if operation == "focus":
                    self.windows = {key: replace(w, focused=(key == id)) for key, w in self.windows.items()}
                elif operation == "toggle":
                    fullscreen = not value.fullscreen
                    space = 1000 + id if fullscreen else (101 if value.display_uuid == A else 102)
                    self.windows[id] = replace(value, fullscreen=fullscreen, space_id=space)
                elif operation == "move":
                    destination = next(s for s in self.raw()["spaces"] if s["index"] == int(args[4]))
                    if destination["is-native-fullscreen"]:
                        return ProcessResult(1, b"", b"Cannot insert a window into a fullscreen Space.")
                    display = next(d["uuid"] for d in self.raw()["displays"] if d["index"] == destination["display"])
                    self.windows[id] = replace(value, display_uuid=display, space_id=destination["id"], focused=False)
            self.after_command(operation)
            return ProcessResult(0, b"", b"")
