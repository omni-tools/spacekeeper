from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from spacekeeper.planner import plan_restore
from spacekeeper.restore import RestoreCoordinator
from spacekeeper.snapshot import ProcessIdentity
from spacekeeper.storage import SnapshotStore
from spacekeeper.yabai import Yabai, YabaiError, YabaiFailure
from tests.fake_yabai import Clock, FakeYabai
from tests.support import A, B, SESSION, observe, window


class SpaceOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.saved = observe(window(1, fullscreen=True, focused=True), window(2, fullscreen=True),
                             window(3, fullscreen=True))
        self.fake = FakeYabai(*self.saved.windows)
        self.fake.space_indexes = {1001: 5, 1002: 4, 1003: 3}
        self.clock = Clock()
        self.adapter = Yabai(executable='/synthetic/yabai', runner=self.fake, identities=self.fake, fullscreen_setter=self.fake, clock=self.clock)
        self.coordinator = RestoreCoordinator(self.adapter, SnapshotStore(self.root / 'state'),
                                              lock_path=self.root / 'lock/restore', timeout=1,
                                              clock=self.clock, sleep=self.clock.sleep, poll_interval=0.1)

    def test_restore_repairs_reversed_order_without_fullscreen_toggles(self):
        report = self.coordinator.restore(self.saved)
        self.assertTrue(report.complete, report)
        spaces = sorted((s for s in self.adapter.observe().spaces if s.fullscreen), key=lambda s: s.index)
        self.assertEqual([s.id for s in spaces], [1001, 1002, 1003])
        self.assertEqual([cmd for cmd in self.fake.commands if cmd[1] == 'space'],
                         [('-m', 'space', '4', '--move', '5'), ('-m', 'space', '3', '--move', '5')])
        self.assertFalse(any('--toggle' in cmd for cmd in self.fake.commands))
        self.assertTrue(all(w.fullscreen for w in self.fake.windows.values()))

    def test_ignore_position_keeps_reversed_order_and_does_not_fail_verification(self):
        report = self.coordinator.restore(self.saved, ignore_position=True)
        self.assertTrue(report.complete, report)
        self.assertEqual(self.fake.space_indexes, {1001: 5, 1002: 4, 1003: 3})
        self.assertEqual(self.fake.commands, [])

    def test_correct_relative_order_with_different_absolute_slots_is_unchanged(self):
        self.fake.space_indexes = {1001: 8, 1002: 10, 1003: 12}
        report = self.coordinator.restore(self.saved)
        self.assertTrue(report.complete, report)
        self.assertEqual(self.fake.commands, [])

    def test_missing_scripting_addition_is_reported_without_retrying_mutation(self):
        self.fake.failures['space_order'] = 'cannot send message to scripting-addition'
        report = self.coordinator.restore(self.saved)
        self.assertFalse(report.complete)
        self.assertTrue(report.hard_failure)
        self.assertTrue(any(i.scope == 'ordering' and i.reason == 'capability_unavailable' for i in report.issues))
        self.assertEqual(len(self.fake.commands), 1)
        self.assertTrue(Path(report.backup_path).is_file())
        self.assertTrue(Path(report.report_path).is_file())

    def test_acknowledged_but_ineffective_move_does_not_report_success_or_retry(self):
        self.fake.effects['space_order'] = False
        report = self.coordinator.restore(self.saved)
        self.assertFalse(report.complete)
        self.assertEqual(len(self.fake.commands), 1)
        self.assertTrue(any(i.scope == 'ordering' and i.reason == 'timeout' for i in report.issues))

    def test_ordering_uses_new_identities_after_name_fallback(self):
        saved_windows = tuple(replace(w, title=str(w.id)) for w in self.saved.windows)
        saved = replace(self.saved, windows=saved_windows)
        self.fake.windows = {w.id + 10: replace(w, id=w.id + 10, pid=w.pid + 10,
                                               owner=ProcessIdentity(w.pid + 10, 2000000, w.id))
                             for w in saved_windows}
        self.fake.session_id = replace(SESSION, login_session_id=200)
        report = self.coordinator.restore(saved)
        self.assertTrue(report.complete, report)
        self.assertTrue(report.name_fallback)
        spaces = sorted((s for s in self.adapter.observe().spaces if s.fullscreen), key=lambda s: s.index)
        self.assertEqual([s.id for s in spaces], [1001, 1002, 1003])

    def test_ignore_position_still_restores_display_and_state(self):
        self.fake.windows = {1: window(fullscreen=True, focused=True, display=B)}
        report = self.coordinator.restore(observe(window(minimized=True)), ignore_position=True)
        self.assertTrue(report.complete, report)
        self.assertEqual((self.fake.windows[1].display_uuid, self.fake.windows[1].fullscreen,
                          self.fake.windows[1].minimized), (A, False, True))
        self.assertFalse(any(cmd[1] == 'space' for cmd in self.fake.commands))

    def test_plan_keeps_separate_original_display_sequences_on_monitor_fallback(self):
        saved = observe(window(1, fullscreen=True), window(2, fullscreen=True),
                        window(3, fullscreen=True, display=B), window(4, fullscreen=True, display=B))
        current = observe(*(replace(w, display_uuid=A) for w in saved.windows),
                          displays=saved.displays[:1], normal_displays=(A,))
        plan = plan_restore(saved, current)
        self.assertEqual([[[i.window_id for i in group] for group in order.windows]
                          for order in plan.fullscreen_order], [[[1], [2]], [[3], [4]]])

    def test_adapter_rejects_owner_change_and_cross_display_before_command(self):
        members, predecessor = (self.saved.windows[1].identity,), (self.saved.windows[0].identity,)
        for change in (dict(owner=ProcessIdentity(102, 9000000, 0)), dict(display_uuid=B)):
            with self.subTest(change=change):
                self.fake.windows[2] = replace(self.saved.windows[1], **change)
                with self.assertRaises(YabaiError):
                    self.adapter.move_fullscreen_space_after(members, predecessor, SESSION, A)
                self.assertEqual(self.fake.commands, [])

    def test_adapter_refuses_to_move_unrelated_split_view_member(self):
        self.fake.windows[4] = window(4, fullscreen=True, space_id=1002)
        with self.assertRaises(YabaiError) as error:
            self.adapter.move_fullscreen_space_after((self.saved.windows[1].identity,),
                                                     (self.saved.windows[0].identity,), SESSION, A)
        self.assertEqual(error.exception.reason, YabaiFailure.UNSUPPORTED_WINDOW)
        self.assertEqual(self.fake.commands, [])

    def test_adapter_uses_fresh_indexes_and_checks_session(self):
        self.fake.space_indexes = {1001: 10, 1002: 8, 1003: 9}
        self.adapter.move_fullscreen_space_after((self.saved.windows[1].identity,),
                                                 (self.saved.windows[0].identity,), SESSION, A)
        self.assertEqual(self.fake.commands, [('-m', 'space', '8', '--move', '10')])
        self.fake.commands.clear()
        self.fake.session_id = replace(SESSION, login_session_id=200)
        with self.assertRaises(YabaiError) as error:
            self.adapter.move_fullscreen_space_after((self.saved.windows[1].identity,),
                                                     (self.saved.windows[0].identity,), SESSION, A)
        self.assertEqual(error.exception.reason, YabaiFailure.SESSION_CHANGED)
        self.assertEqual(self.fake.commands, [])

    def test_shared_fullscreen_space_is_moved_as_one_selected_group(self):
        saved = observe(window(1, fullscreen=True, focused=True), window(2, fullscreen=True),
                        window(3, fullscreen=True, space_id=1002))
        self.fake.windows = {w.id: w for w in saved.windows}
        self.fake.space_indexes = {1001: 4, 1002: 3}
        report = self.coordinator.restore(saved)
        self.assertTrue(report.complete, report)
        self.assertEqual(len(self.fake.commands), 1)
        self.assertEqual(self.fake.windows[2].space_id, self.fake.windows[3].space_id)

    def test_saved_separate_spaces_merged_live_are_reported_as_ambiguous(self):
        self.fake.windows[2] = replace(self.fake.windows[2], space_id=1001)
        report = self.coordinator.restore(self.saved)
        self.assertFalse(report.complete)
        self.assertTrue(any(i.reason == 'fullscreen_order_ambiguous' for i in report.issues))
        self.assertEqual(self.fake.commands, [])

    def test_final_verification_detects_order_changed_after_ordering(self):
        self.saved = replace(self.saved, windows=tuple(replace(w, focused=w.id == 2) for w in self.saved.windows))
        def changed(operation):
            if operation == 'focus':
                self.fake.space_indexes = {1001: 5, 1002: 4, 1003: 3}
        self.fake.after_command = changed
        report = self.coordinator.restore(self.saved)
        self.assertFalse(report.complete)
        self.assertTrue(any(i.scope == 'verification' and i.reason == 'fullscreen_order_mismatch'
                            for i in report.issues))

    def test_ordinary_desktops_and_unrelated_fullscreen_spaces_keep_relative_order(self):
        # Two unrelated Spaces can shift absolute slots, but neither is moved
        # relative to the other while the selected fullscreen sequence is sorted.
        self.fake.windows[4] = window(4, fullscreen=True, display=B)
        self.fake.windows[5] = window(5, display=B)
        from spacekeeper.matching import WindowRule
        before = self.adapter.observe()
        report = self.coordinator.restore(self.saved, preserve=(WindowRule('*'),))
        self.assertTrue(report.complete, report)
        unrelated = {101, 102, 1004}
        self.assertEqual([s.id for s in sorted(before.spaces, key=lambda s: s.index) if s.id in unrelated],
                         [s.id for s in sorted(self.adapter.observe().spaces, key=lambda s: s.index)
                          if s.id in unrelated])
