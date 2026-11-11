from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from spacekeeper.presets import Preset, PresetError, PresetStore
from spacekeeper.restore import RestoreCoordinator
from spacekeeper.storage import SnapshotStore, StorageError
from spacekeeper.yabai import Yabai
from tests.fake_yabai import Clock, FakeYabai
from tests.support import A, B, SESSION, observe, window


def preset(rules, devices=None):
    import yaml
    return Preset.from_yaml(yaml.safe_dump({'devices': devices or {}, 'windows': rules}))


class PresetTests(unittest.TestCase):
    def test_fuzzy_app_alias_and_optional_title_apply_to_all_matches(self):
        layout = preset([{'app': 'Editor', 'title': '*document*', 'device': 'ext'}], {'external': B})
        plan = layout.plan(observe(window(), window(2), window(3, title='Other')))
        self.assertEqual([d.target.display_uuid for d in plan.windows if d.target], [B, B])
        self.assertEqual(plan.windows[2].reason.value, 'preset_unmatched')

    def test_exact_app_beats_substring_and_explicit_globs_can_match_multiple_apps(self):
        snapshot = observe(window(app='Code'), window(2, app='Code Insiders'))
        exact = preset([{'app': 'Code', 'device': 'primary'}]).plan(snapshot)
        self.assertEqual([d.window_id for d in exact.windows if d.target], [1])
        glob = preset([{'app': 'code*', 'device': 'primary'}]).plan(snapshot)
        self.assertEqual([d.window_id for d in glob.windows if d.target], [1, 2])

    def test_ambiguous_missing_or_conflicting_rules_never_guess(self):
        snapshot = observe(window(app='Code Editor'), window(2, app='Code Browser'))
        variants = [([{'app': 'Code', 'device': 'primary'}], {}),
                    ([{'app': 'absent', 'device': 'primary'}], {}),
                    ([{'app': '*', 'device': 'external'}], {}),
                    ([{'app': '*', 'device': 'extern'}], {'external-a': A, 'external-b': B}),
                    ([{'app': '*', 'device': A}, {'app': '*', 'device': B}], {})]
        for rules, devices in variants:
            with self.subTest(rules=rules), self.assertRaises(PresetError):
                preset(rules, devices).plan(snapshot)

    def test_missing_monitor_and_unsupported_matches_are_explicit_errors(self):
        for snapshot in (observe(window(), displays=observe(window()).displays[:1]),
                         observe(window(minimized=True))):
            with self.subTest(snapshot=snapshot), self.assertRaises(PresetError):
                preset([{'app': '*', 'device': B}]).plan(snapshot)

    def test_yaml_validation_rejects_duplicates_tags_aliases_and_invalid_types(self):
        for source in ('[]', 'windows: []', 'windows: []\nwindows: []',
                       'windows: !!python/object/apply:os.system [echo]',
                       'devices: &d {}\nwindows: [*d]',
                       'windows: [{app: true, device: primary}]',
                       'windows: [{app: Editor, device: primary, fullscreen: 1}]',
                       'windows: [{app: Editor, device: primary, extra: true}]',
                       'devices: {external: 1}\nwindows: [{app: Editor, device: primary}]',
                       'windows: [{app: Editor, device: primary}]\nversion: true',
                       'x'*65537):
            with self.subTest(source=source[:80]), self.assertRaises(PresetError):
                Preset.from_yaml(source)

    def test_capture_round_trip_escapes_literal_glob_characters_and_supports_new_session(self):
        original = observe(window(app='Editor [dev]', title='File *?.txt', focused=True),
                           window(2, app='Other', title='', display=B))
        exported = Preset.capture(original)
        loaded = Preset.from_yaml(exported.to_yaml())
        replacement = observe(window(8, app='Editor [dev]', title='File *?.txt'),
                              window(9, app='Other', title='', display=B),
                              session=replace(SESSION, login_session_id=999))
        plan = loaded.plan(replacement)
        self.assertTrue(plan.accepted)
        self.assertEqual({d.window_id for d in plan.windows if d.target}, {8, 9})
        self.assertEqual({d.target.identity for d in plan.windows}, {w.identity for w in replacement.windows})

    def test_capture_rejects_unexpressible_identical_selectors_with_different_targets(self):
        with self.assertRaises(PresetError):
            Preset.capture(observe(window(), window(2, display=B)))

    def test_store_is_private_atomic_requires_force_and_lists_without_creating(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'config'
            store = PresetStore(root)
            self.assertEqual(store.entries(), ())
            self.assertFalse(root.exists())
            layout = preset([{'app': '*', 'device': 'primary'}])
            with self.assertRaises(StorageError):
                store.save(layout, '../bad')
            self.assertFalse(root.exists())
            path = store.save(layout, 'work')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.load('work'), layout)
            with self.assertRaises(PresetError):
                store.save(layout, 'work')
            store.save(layout, 'work', force=True)
            path.unlink()
            path.symlink_to(Path(temp)/'other')
            with self.assertRaises((StorageError, PresetError)):
                store.load('work')
            with self.assertRaises(StorageError):
                store.save(layout, 'work', force=True)

    def test_apply_uses_backup_guarded_transitions_and_preserves_unmatched_fullscreen(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            extra = window(2, app='Other', fullscreen=True)
            fake, clock = FakeYabai(window(focused=True), extra), Clock()
            adapter = Yabai(executable='/synthetic/yabai', runner=fake, identities=fake, fullscreen_setter=fake, clock=clock)
            coordinator = RestoreCoordinator(adapter, SnapshotStore(root/'state'), lock_path=root/'lock/restore.lock',
                                             clock=clock, sleep=clock.sleep)
            layout = preset([{'app': 'Editor', 'device': B}])
            report = coordinator.apply_preset(layout)
            self.assertTrue(report.complete, report)
            self.assertEqual((fake.windows[1].display_uuid, fake.windows[1].fullscreen), (B, True))
            self.assertEqual(fake.windows[2], extra)
            self.assertTrue(Path(report.backup_path).is_file())
            self.assertFalse(any(command[2] == '2' or
                                 (command[2] == '--focus' and command[3] == '2') for command in fake.commands))

    def test_unmatched_window_changes_cannot_be_reported_as_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake, clock = FakeYabai(window(), window(2, app='Other')), Clock()
            fake.after_command = lambda operation: fake.windows.pop(2, None)
            adapter = Yabai(executable='/synthetic/yabai', runner=fake, identities=fake, fullscreen_setter=fake, clock=clock)
            coordinator = RestoreCoordinator(adapter, SnapshotStore(root/'state'), lock_path=root/'lock/restore.lock',
                                             timeout=1, clock=clock, sleep=clock.sleep)
            report = coordinator.apply_preset(preset([{'app': 'Editor', 'device': A}]))
            self.assertFalse(report.complete)
            self.assertTrue(any(issue.reason == 'unmatched_window_changed' for issue in report.issues))

    def test_plan_errors_stop_before_any_window_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake, clock = FakeYabai(window()), Clock()
            adapter = Yabai(executable='/synthetic/yabai', runner=fake, identities=fake, fullscreen_setter=fake, clock=clock)
            coordinator = RestoreCoordinator(adapter, SnapshotStore(root/'state'), lock_path=root/'lock/restore.lock',
                                             clock=clock, sleep=clock.sleep)
            with self.assertRaises(PresetError):
                coordinator.apply_preset(preset([{'app': 'Absent', 'device': A}]))
            self.assertEqual(fake.commands, [])
