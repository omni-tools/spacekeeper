from dataclasses import replace
import unittest

from spacekeeper.snapshot import Frame, ProcessIdentity, SessionIdentity, Snapshot, SnapshotError
from tests.support import A, B, SESSION, observe, window


class SnapshotTests(unittest.TestCase):
    def test_round_trip_preserves_observations_and_additional_metadata(self):
        original = replace(observe(window(fullscreen=True, focused=True)), metadata={
            "yabai": {"windows": [{"id": 1, "title": "Synthetic document", "can-move": True}]},
            "notes": [None, 4, 2.5],
        })
        restored = Snapshot.from_json(original.to_json())
        self.assertEqual(original, restored)
        self.assertEqual(original.metadata, restored.metadata)
        data = restored.to_dict()
        data["metadata"]["notes"].append("changed")
        self.assertNotEqual(restored.metadata, data["metadata"])

    def test_metadata_input_is_copied(self):
        metadata = {"nested": [1]}
        result = replace(observe(), metadata=metadata)
        metadata["nested"].append(2)
        self.assertEqual(result.metadata, {"nested": [1]})

    def test_invalid_wire_types_and_fields(self):
        original = observe(window()).to_dict()
        cases = []
        for key, value in (("schema_version", True), ("schema_version", 99), ("windows", {}),
                           ("session", None), ("unexpected", 1), ("captured_at", "2026-01-01"),
                           ("metadata", {"x": float("nan")})):
            cases.append(dict(original, **{key: value}))
        for value in cases:
            with self.subTest(value=value), self.assertRaises(SnapshotError):
                Snapshot.from_dict(value)
        unversioned = dict(original)
        del unversioned["schema_version"]
        with self.assertRaises(SnapshotError):
            Snapshot.from_dict(unversioned)
        for text in ("[]", "null", "{", '{"schema_version":1,"schema_version":1}'):
            with self.subTest(text=text), self.assertRaises(SnapshotError):
                Snapshot.from_json(text)

    def test_missing_fields_and_wrong_nested_types(self):
        for path in (("windows", "owner"), ("windows", "id"), ("spaces", "fullscreen"),
                     ("displays", "frame")):
            data = observe(window()).to_dict()
            del data[path[0]][0][path[1]]
            # fullscreen has no default; an owner is explicit even when unreadable.
            with self.subTest(path=path), self.assertRaises(SnapshotError):
                Snapshot.from_dict(data)
        for field, value in (("id", True), ("fullscreen", "false"), ("owner", []),
                             ("pid", "101"), ("title", None), ("root_window", 1)):
            data = observe(window()).to_dict()
            data["windows"][0][field] = value
            with self.subTest(field=field), self.assertRaises(SnapshotError):
                Snapshot.from_dict(data)

    def test_invalid_identity_and_frame_data(self):
        cases = (
            lambda: SessionIdentity("invalid", 501, 1),
            lambda: SessionIdentity(SESSION.boot_uuid, True, 1),
            lambda: SessionIdentity(SESSION.boot_uuid, 501, 0xFFFFFFFF),
            lambda: ProcessIdentity(0, 1, 0),
            lambda: ProcessIdentity(1, 0, 0),
            lambda: ProcessIdentity(1, 1, 1_000_000),
            lambda: ProcessIdentity(2**32, 1, 0),
            lambda: Frame(0, 0, -1, 5),
            lambda: Frame(float("inf"), 0, 1, 1),
            lambda: Frame(True, 0, 1, 1),
            lambda: Frame(10**1000, 0, 1, 1),
            lambda: window(owner=ProcessIdentity(999, 1, 0)),
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(SnapshotError):
                case()

    def test_uuid_case_is_normalized(self):
        lower = SessionIdentity("abcdef00-0000-0000-0000-000000000001", 501, 1)
        upper = SessionIdentity(lower.boot_uuid.upper(), 501, 1)
        self.assertEqual(lower, upper)

    def test_duplicate_ids_and_indexes(self):
        base = observe(window())
        changes = (
            {"windows": base.windows * 2}, {"spaces": base.spaces * 2},
            {"displays": base.displays * 2},
            {"displays": (base.displays[0], replace(base.displays[1], index=1))},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(SnapshotError):
                replace(base, **change)

    def test_unknown_references_and_conflicting_membership(self):
        base = observe(window())
        changes = (
            lambda: replace(base, windows=(replace(base.windows[0], display_uuid="00000000-0000-0000-0000-000000000099"),)),
            lambda: replace(base, windows=(replace(base.windows[0], space_id=999),)),
            lambda: replace(base, windows=(replace(base.windows[0], display_uuid=B),)),
            lambda: replace(base, spaces=(replace(base.spaces[0], window_ids=(99,), visible_window_id=99), base.spaces[1])),
            lambda: replace(base, spaces=(replace(base.spaces[0], window_ids=(1, 1)), base.spaces[1])),
            lambda: replace(base, spaces=(replace(base.spaces[0], window_ids=(), visible_window_id=None), base.spaces[1])),
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(SnapshotError):
                change()

    def test_conflicting_owners_focus_and_visibility(self):
        with self.assertRaises(SnapshotError):
            observe(window(), window(2, pid=101, owner=ProcessIdentity(101, 2_000_000, 0)))
        with self.assertRaises(SnapshotError):
            observe(window(focused=True), window(2, focused=True))
        with self.assertRaises(SnapshotError):
            observe(window(fullscreen=True), window(2, fullscreen=True), visible_ids=(1, 2))

    def test_capture_interval_and_timezone(self):
        with self.assertRaises(SnapshotError):
            replace(observe(), capture_started_at="2026-01-02T00:00:00+00:00")
        with self.assertRaises(SnapshotError):
            replace(observe(), captured_at="2026-01-01T12:00:00")

    def test_unknown_owner_and_unplaced_windows_are_representable(self):
        value = observe(window(owner=None, display_uuid=None, space_id=None))
        self.assertEqual(Snapshot.from_json(value.to_json()), value)

    def test_non_json_metadata_is_rejected(self):
        for value in ({1: "bad key"}, {"set": {1}}, {"tuple": (1,)}, {"nan": float("nan")}):
            with self.subTest(value=value), self.assertRaises(SnapshotError):
                replace(observe(), metadata=value)
