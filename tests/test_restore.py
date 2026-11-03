from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from spacekeeper.restore import RestoreCoordinator, RestoreError, restore_lock
from spacekeeper.matching import WindowRule
from spacekeeper.storage import SnapshotStore, StorageError, StorageFailure
from spacekeeper.yabai import Yabai, YabaiError, YabaiFailure
from tests.fake_yabai import Clock, FakeYabai
from tests.support import A, B, SESSION, observe, window


class RestoreTests(unittest.TestCase):
    def test_preserved_extra_stays_untouched_and_does_not_make_restore_incomplete(self):
        extra = window(2, fullscreen=True, display=B)
        desktop = FakeYabai(window(focused=True), extra)
        report = self.coordinator(desktop).restore(observe(window(focused=True)),
                                                   preserve=(WindowRule('Synthetic*'),))
        self.assertTrue(report.complete, report)
        self.assertEqual(desktop.windows[2], extra)
        self.assertEqual(report.windows[1].reason, 'preserved_extra')
        self.assertFalse(any('2' in command for command in desktop.commands))

    def test_preserved_window_changes_are_reported_by_final_verification(self):
        desktop = FakeYabai(window(focused=True), window(2, fullscreen=True, display=B))
        def changed(operation):
            if operation == 'toggle':
                desktop.windows[2] = replace(desktop.windows[2], title='same identity', fullscreen=False, space_id=102)
        desktop.after_command = changed
        report = self.coordinator(desktop).restore(observe(window(fullscreen=True, focused=True)),
                                                   preserve=(WindowRule('*'),))
        self.assertFalse(report.complete)
        self.assertTrue(any(i.reason == 'preserved_window_changed' for i in report.issues))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = SnapshotStore(self.root / 'state')
        self.lock = self.root / 'private/restore.lock'

    def coordinator(self, desktop, store=None):
        clock = Clock()
        adapter = Yabai(executable='/synthetic/yabai', runner=desktop, identities=desktop, clock=clock)
        return RestoreCoordinator(adapter, store or self.store, lock_path=self.lock, timeout=1,
                                  poll_interval=0.1, clock=clock, sleep=clock.sleep)

    def test_full_restore_uses_backup_before_commands_and_verifies_everything(self):
        saved = observe(window(fullscreen=True, focused=True))
        desktop = FakeYabai(window(fullscreen=True, display=B), window(2, fullscreen=True), window(3, display=B))
        real_runner = desktop.__call__
        coordinator = self.coordinator(desktop)
        def checked_runner(argv, **options):
            if argv[1:3] != ('-m', 'query'):
                backups = list((self.store.root / 'backups').glob('*.json'))
                self.assertEqual(len(backups), 1)
                self.assertEqual(len(self.store.load_backup(backups[0].stem).windows), 3)
                with self.assertRaises(RestoreError):
                    with restore_lock(self.lock):
                        self.fail('Restore lock was not held during mutation.')
            return real_runner(argv, **options)
        coordinator.desktop.runner = checked_runner
        report = coordinator.restore(saved)
        self.assertTrue(report.complete, report)
        self.assertEqual((desktop.windows[1].display_uuid, desktop.windows[1].fullscreen, desktop.windows[1].focused),
                         (A, True, True))
        self.assertFalse(desktop.windows[2].fullscreen)
        self.assertEqual(desktop.windows[3], window(3, display=B))
        persisted = json.loads(Path(report.report_path).read_text())
        self.assertTrue(persisted['complete'])
        self.assertEqual(persisted['backup_path'], report.backup_path)

    def test_backup_failure_prevents_every_mutation_and_releases_lock(self):
        desktop = FakeYabai(window())
        with patch.object(self.store, 'backup', side_effect=StorageError(StorageFailure.IO_ERROR, 'Synthetic failure')):
            with self.assertRaises(StorageError):
                self.coordinator(desktop).restore(observe(window(fullscreen=True)))
        self.assertEqual(desktop.commands, [])
        with restore_lock(self.lock):
            pass

    def test_rejected_session_still_has_backup_and_report_but_no_mutations(self):
        desktop = FakeYabai(window())
        saved = observe(window(fullscreen=True), session=replace(SESSION, login_session_id=101))
        report = self.coordinator(desktop).restore(saved)
        self.assertFalse(report.complete)
        self.assertEqual(report.issues[0].reason, 'session_mismatch')
        self.assertTrue(Path(report.backup_path).is_file())
        self.assertTrue(Path(report.report_path).is_file())
        self.assertEqual(desktop.commands, [])

    def test_global_lock_blocks_a_different_storage_root_before_observation(self):
        desktop = FakeYabai(window())
        with restore_lock(self.lock):
            with self.assertRaises(RestoreError):
                self.coordinator(desktop, SnapshotStore(self.root / 'other')).restore(observe())
        self.assertEqual(sum(desktop.query_counts.values()), 0)

    def test_lock_is_released_after_exception_and_not_unlinked(self):
        with self.assertRaises(ValueError):
            with restore_lock(self.lock):
                raise ValueError('Synthetic cancellation')
        self.assertTrue(self.lock.exists())
        with restore_lock(self.lock):
            pass

    def test_lock_rejects_symlink(self):
        self.lock.parent.mkdir(mode=0o700)
        outside = self.root / 'outside'
        outside.touch(mode=0o600)
        self.lock.symlink_to(outside)
        with self.assertRaises(RestoreError):
            with restore_lock(self.lock):
                self.fail('A symlink lock was accepted.')

    def test_failed_transition_is_not_a_complete_restore(self):
        desktop = FakeYabai(window())
        desktop.effects['toggle'] = False
        report = self.coordinator(desktop).restore(observe(window(fullscreen=True)))
        self.assertFalse(report.complete)
        self.assertTrue(any(w.status == 'failed' for w in report.windows))
        self.assertTrue(Path(report.report_path).exists())

    def test_focus_failure_is_reported_even_if_window_states_match(self):
        desktop = FakeYabai(window())
        desktop.effects['focus'] = False
        report = self.coordinator(desktop).restore(observe(window(focused=True)))
        self.assertFalse(report.complete)
        self.assertTrue(any(i.scope in ('focus', 'visibility') for i in report.issues))

    def test_matching_shortcut_still_detects_new_fullscreen_windows_in_final_verification(self):
        desktop = FakeYabai(window(focused=True))
        def new_window(domain, count):
            if domain == 'windows' and count == 3:
                desktop.windows[99] = window(99, fullscreen=True, display=B)
        desktop.on_query = new_window
        report = self.coordinator(desktop).restore(observe(window(focused=True)))
        self.assertFalse(report.complete)
        self.assertTrue(any(i.reason == 'new_fullscreen_window' for i in report.issues))
        self.assertEqual(desktop.commands, [])

    def test_last_visible_window_focus_is_not_repeated(self):
        desktop = FakeYabai(window())
        coordinator = self.coordinator(desktop)
        with patch.object(coordinator, '_focus', wraps=coordinator._focus) as focus:
            report = coordinator.restore(observe(window(focused=True)))
        self.assertTrue(report.complete, report)
        self.assertEqual(focus.call_count, 1)

    def test_focus_verification_tolerates_a_temporary_target_query_gap(self):
        desktop = FakeYabai(window())
        coordinator = self.coordinator(desktop)
        original = coordinator.desktop.observe_window
        gaps = []
        def gap(*args, **options):
            if desktop.commands and not gaps:
                gaps.append(True)
                raise YabaiError(YabaiFailure.WINDOW_MISSING, 'Synthetic focus animation.')
            return original(*args, **options)
        coordinator.desktop.observe_window = gap
        report = coordinator.restore(observe(window(focused=True)))
        self.assertTrue(report.complete, report)
        self.assertEqual(gaps, [True])

    def test_closed_saved_window_is_incomplete_but_protected_windows_are_excluded(self):
        report = self.coordinator(FakeYabai()).restore(observe(window(), visible_ids=()))
        self.assertFalse(report.complete)
        self.assertEqual(report.windows[0].status, 'skipped')
        protected = window(sticky=True)
        report = self.coordinator(FakeYabai(protected)).restore(observe(protected, visible_ids=()))
        self.assertTrue(report.complete)
        self.assertEqual(report.windows[0].status, 'excluded')

    def test_final_verification_detects_new_extra_fullscreen_without_mutating_it(self):
        desktop = FakeYabai(window())
        def user_opens_window(operation):
            if operation == 'toggle':
                desktop.windows[99] = window(99, fullscreen=True, display=B)
        desktop.after_command = user_opens_window
        report = self.coordinator(desktop).restore(observe(window(fullscreen=True)))
        self.assertFalse(report.complete)
        self.assertTrue(any(i.reason == 'new_fullscreen_window' and i.window_id == 99 for i in report.issues))
        self.assertTrue(desktop.windows[99].fullscreen)

    def test_session_change_stops_remaining_window_mutations(self):
        desktop = FakeYabai(window(), window(2))
        desktop.after_command = lambda operation: setattr(desktop, 'session_id', replace(SESSION, login_session_id=101))
        report = self.coordinator(desktop).restore(observe(window(fullscreen=True), window(2, fullscreen=True)))
        self.assertFalse(report.complete)
        self.assertFalse(any(command[2] == '2' for command in desktop.commands))

    def test_report_failure_identifies_the_recovery_backup(self):
        desktop = FakeYabai(window())
        with patch.object(self.store, 'report', side_effect=OSError('Synthetic disk failure')):
            with self.assertRaises(RestoreError) as caught:
                self.coordinator(desktop).restore(observe(window()))
        self.assertIn('Pre-restore backup:', str(caught.exception))
        self.assertEqual(len(list((self.store.root / 'backups').glob('*.json'))), 1)

    def test_system_only_visible_space_does_not_invent_a_focus_target(self):
        protected = window(sticky=True)
        saved = observe(protected)
        saved = replace(saved, spaces=tuple(replace(s, visible_window_id=None) for s in saved.spaces))
        report = self.coordinator(FakeYabai(protected)).restore(saved)
        self.assertTrue(report.complete)
        self.assertEqual(report.issues, ())

    def test_single_final_sample_at_deadline_is_not_complete(self):
        desktop = FakeYabai()
        coordinator = self.coordinator(desktop)
        original = coordinator.desktop.observe
        calls = []
        def slow_final(**options):
            value = original(**options)
            calls.append(True)
            if len(calls) > 1:
                coordinator.sleep(0.95)
            return value
        coordinator.desktop.observe = slow_final
        report = coordinator.restore(observe())
        self.assertFalse(report.complete)
        self.assertTrue(any(i.reason == 'timeout' for i in report.issues))

    def test_disappeared_system_window_is_an_exclusion_not_a_failed_target(self):
        report = self.coordinator(FakeYabai()).restore(observe(window(sticky=True), visible_ids=()))
        self.assertTrue(report.complete)
        self.assertEqual(report.windows[0].status, 'excluded')

    def test_previously_supported_window_becoming_protected_is_incomplete(self):
        saved = observe(window(), visible_ids=())
        report = self.coordinator(FakeYabai(window(sticky=True))).restore(saved)
        self.assertFalse(report.complete)
        self.assertEqual(report.windows[0].status, 'unsupported')
