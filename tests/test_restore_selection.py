from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from spacekeeper.planner import DecisionStatus, plan_restore
from spacekeeper.restore import RestoreCoordinator
from spacekeeper.storage import SnapshotStore
from spacekeeper.yabai import Yabai
from tests.fake_yabai import Clock, FakeYabai
from tests.support import A, B, SESSION, observe, window


class RestoreSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.saved = observe(window(1, title='ordinary', focused=True),
                             window(2, title='fullscreen', fullscreen=True),
                             window(3, title='minimized', minimized=True))
        self.current = (window(1, title='ordinary', minimized=True),
                        window(2, title='fullscreen', display=B),
                        window(3, title='minimized', fullscreen=True, focused=True),
                        window(4, title='extra', fullscreen=True, display=B))

    def coordinator(self, fake):
        clock = Clock()
        return RestoreCoordinator(Yabai(executable='/synthetic/yabai', runner=fake, identities=fake, clock=clock),
                                  SnapshotStore(self.root / 'state'), lock_path=self.root / 'lock/restore',
                                  timeout=1, clock=clock, sleep=clock.sleep, poll_interval=0.1)

    def test_saved_state_filters_restore_selected_windows_and_preserve_others(self):
        cases = ((dict(only_fullscreen=True), {2}),
                 (dict(only_minimize=True), {3}),
                 (dict(fullscreen=False), {1, 3}),
                 (dict(fullscreen=False, minimize=False), {1}),
                 (dict(only_fullscreen=True, minimize=False), {2}),
                 (dict(only_minimize=True, fullscreen=False), {3}))
        for options, selected in cases:
            with self.subTest(options=options):
                fake = FakeYabai(*self.current)
                report = self.coordinator(fake).restore(self.saved, **options)
                self.assertTrue(report.complete, report)
                self.assertEqual(report.missed, 0)
                for wanted in self.saved.windows:
                    actual = fake.windows[wanted.id]
                    if wanted.id in selected:
                        self.assertEqual((actual.display_uuid, actual.fullscreen, actual.minimized),
                                         (wanted.display_uuid, wanted.fullscreen, wanted.minimized))
                    else:
                        previous = self.current[wanted.id - 1]
                        # Restoring selected focus can defocus another window.
                        self.assertEqual(replace(actual, focused=previous.focused), previous)
                self.assertEqual(fake.windows[4], self.current[3])

    def test_no_minimize_alone_keeps_existing_extra_fullscreen_cleanup(self):
        fake = FakeYabai(*self.current)
        report = self.coordinator(fake).restore(self.saved, minimize=False)
        self.assertTrue(report.complete, report)
        self.assertFalse(fake.windows[4].fullscreen)
        self.assertEqual(replace(fake.windows[3], focused=self.current[2].focused), self.current[2])

    def test_excluded_closed_windows_and_focus_do_not_count_as_misses(self):
        fake = FakeYabai(window(2, fullscreen=True))
        report = self.coordinator(fake).restore(self.saved, only_fullscreen=True)
        self.assertTrue(report.complete, report)
        self.assertEqual(report.missed, 0)
        self.assertEqual(fake.commands, [])

    def test_missing_selected_windows_still_count_as_missed(self):
        fake = FakeYabai(window(1))
        report = self.coordinator(fake).restore(self.saved, only_fullscreen=True)
        self.assertFalse(report.complete)
        self.assertEqual(report.missed, 1)

    def test_empty_selection_is_successful_and_leaves_extras_untouched(self):
        saved = observe(window(1, focused=True))
        fake = FakeYabai(*self.current)
        report = self.coordinator(fake).restore(saved, only_fullscreen=True)
        self.assertTrue(report.complete, report)
        self.assertEqual(fake.commands, [])
        self.assertEqual(tuple(fake.windows.values()), self.current)

    def test_conflicts_are_rejected_before_desktop_reads_or_backup(self):
        fake = FakeYabai(*self.current)
        for options in (dict(only_fullscreen=True, only_minimize=True),
                        dict(only_fullscreen=True, fullscreen=False),
                        dict(only_minimize=True, minimize=False)):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    self.coordinator(fake).restore(self.saved, **options)
                self.assertEqual(sum(fake.query_counts.values()), 0)
                self.assertFalse((self.root / 'state').exists())

    def test_fallback_exclusions_remain_in_uniqueness_checks(self):
        saved = observe(window(1, title='same', fullscreen=True), window(2, title='same'))
        current = observe(window(10, title='same'), session=replace(SESSION, login_session_id=200))
        plan = plan_restore(saved, current, only_fullscreen=True)
        self.assertTrue(plan.name_fallback)
        self.assertEqual([(i.reason, i.source_window_id) for i in plan.issues], [('name_ambiguous', 1)])
        self.assertTrue(all(d.target is None for d in plan.windows))

    def test_fallback_filters_use_source_state_not_live_state(self):
        fake = FakeYabai(window(10, title='fullscreen', display=B),
                         window(20, title='ordinary', fullscreen=True))
        fake.session_id = replace(SESSION, login_session_id=200)
        ordinary = fake.windows[20]
        report = self.coordinator(fake).restore(self.saved, only_fullscreen=True)
        self.assertTrue(report.complete, report)
        self.assertEqual(report.missed, 0)
        self.assertTrue(fake.windows[10].fullscreen)
        self.assertEqual(fake.windows[10].display_uuid, A)
        self.assertEqual(fake.windows[20], ordinary)

    def test_changes_to_excluded_windows_are_detected(self):
        fake = FakeYabai(*self.current)
        def changed(operation):
            if operation == 'toggle':
                fake.windows[1] = replace(fake.windows[1], minimized=False)
        fake.after_command = changed
        report = self.coordinator(fake).restore(self.saved, only_fullscreen=True)
        self.assertFalse(report.complete)
        self.assertTrue(any(i.reason == 'state_filtered_changed' and i.window_id == 1 for i in report.issues))

    def test_new_unrelated_fullscreen_window_does_not_fail_filtered_restore(self):
        fake = FakeYabai(*self.current)
        def appeared(operation):
            fake.windows[5] = window(5, fullscreen=True, display=B)
        fake.after_command = appeared
        report = self.coordinator(fake).restore(self.saved, only_fullscreen=True)
        self.assertTrue(report.complete, report)
        self.assertTrue(fake.windows[5].fullscreen)

    def test_fullscreen_order_only_contains_selected_saved_spaces(self):
        saved = observe(window(1, fullscreen=True), window(2, fullscreen=True), window(3, minimized=True))
        for options, groups in ((dict(only_fullscreen=True), 1), (dict(only_minimize=True), 0),
                                (dict(fullscreen=False), 0)):
            with self.subTest(options=options):
                plan = plan_restore(saved, saved, **options)
                self.assertEqual(len(plan.fullscreen_order), groups)
                self.assertTrue(all(d.status == DecisionStatus.EXCLUDED for d in plan.windows
                                    if d.target is None))

    def test_filtered_fullscreen_restore_can_ignore_order(self):
        saved = observe(window(1, fullscreen=True, focused=True), window(2, fullscreen=True))
        fake = FakeYabai(*saved.windows, window(3, fullscreen=True, display=B))
        fake.space_indexes = {1001: 4, 1002: 3}
        report = self.coordinator(fake).restore(saved, only_fullscreen=True, ignore_position=True)
        self.assertTrue(report.complete, report)
        self.assertEqual(fake.commands, [])
