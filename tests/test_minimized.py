from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from spacekeeper.restore import RestoreCoordinator
from spacekeeper.planner import WindowTarget, plan_restore
from spacekeeper.snapshot import ProcessIdentity
from spacekeeper.storage import SnapshotStore
from spacekeeper.transitions import TransitionStatus, WindowTransitions
from spacekeeper.yabai import Yabai, YabaiError, YabaiFailure
from tests.fake_yabai import Clock, FakeYabai
from tests.support import A, B, SESSION, observe, window


class MinimizedRestoreTests(unittest.TestCase):
    def engine(self, fake):
        clock = Clock()
        adapter = Yabai(executable='/synthetic/yabai', runner=fake, identities=fake, clock=clock)
        return WindowTransitions(adapter, transition_timeout=1, poll_interval=0.1, clock=clock, sleep=clock.sleep)

    def test_saved_dock_state_is_applied_in_original_and_stale_sessions(self):
        for stale in (False, True):
            with self.subTest(stale=stale), tempfile.TemporaryDirectory() as temp:
                saved = observe(window(title='Dock target', minimized=True),
                                window(2, title='Keep visible', focused=True),
                                session=replace(SESSION, login_session_id=200) if stale else SESSION)
                target_id, focus_id = (9, 10) if stale else (1, 2)
                fake, clock = FakeYabai(window(target_id, title='Dock target'),
                                        window(focus_id, title='Keep visible', focused=True)), Clock()
                adapter = Yabai(executable='/synthetic/yabai', runner=fake, identities=fake, clock=clock)
                root = Path(temp)
                coordinator = RestoreCoordinator(adapter, SnapshotStore(root/'state'), lock_path=root/'lock/restore.lock',
                                                 timeout=1, clock=clock, sleep=clock.sleep)
                report = coordinator.restore(saved)
                self.assertTrue(fake.windows[target_id].minimized, 'Saved minimized window stayed outside the Dock')
                self.assertTrue(report.complete, report)
                self.assertTrue(fake.windows[focus_id].focused)

    def test_minimize_exits_fullscreen_and_moves_before_using_the_dock(self):
        fake = FakeYabai(window(fullscreen=True, display=B))
        result = self.engine(fake).apply(WindowTarget(window().identity, A, False, True), SESSION)
        self.assertEqual(result.status, TransitionStatus.VERIFIED, result)
        self.assertEqual(result.attempted_operations, ('focus', 'exit_fullscreen', 'move_to_display', 'minimize'))
        self.assertEqual((fake.windows[1].display_uuid, fake.windows[1].fullscreen, fake.windows[1].minimized),
                         (A, False, True))

    def test_deminimize_precedes_monitor_move_and_fullscreen_entry(self):
        fake = FakeYabai(window(minimized=True, display=B))
        result = self.engine(fake).apply(WindowTarget(window().identity, A, True, False), SESSION)
        self.assertEqual(result.status, TransitionStatus.VERIFIED, result)
        self.assertEqual(result.attempted_operations, ('deminimize', 'move_to_display', 'focus', 'enter_fullscreen'))
        self.assertFalse(fake.windows[1].minimized)

    def test_matching_minimized_window_does_not_get_restored_or_focused(self):
        fake = FakeYabai(window(minimized=True))
        result = self.engine(fake).apply(WindowTarget(window().identity, A, False, True), SESSION)
        self.assertEqual(result.status, TransitionStatus.VERIFIED, result)
        self.assertEqual(fake.commands, [])

    def test_wrong_minimized_state_is_detected_in_final_verification(self):
        saved, before = observe(window(minimized=True)), observe(window())
        issues = RestoreCoordinator._verify(plan_restore(saved, before), before, before)
        self.assertTrue(any(i.reason == 'window_target_mismatch' for i in issues))

    def test_ignored_minimize_and_deminimize_commands_time_out(self):
        for desired in (True, False):
            with self.subTest(desired=desired):
                fake = FakeYabai(window(minimized=not desired))
                fake.effects['minimize' if desired else 'deminimize'] = False
                result = self.engine(fake).apply(WindowTarget(window().identity, A, False, desired), SESSION)
                self.assertEqual((result.status, result.reason), (TransitionStatus.FAILED, 'timeout'))
                self.assertEqual(len(fake.commands), 1)

    def test_new_owner_stops_minimize_after_intermediate_unminimize(self):
        fake = FakeYabai(window(minimized=True, display=B))
        def replace_owner(operation):
            if operation == 'deminimize':
                fake.windows[1] = replace(fake.windows[1], owner=ProcessIdentity(101, 2_000_000, 0))
        fake.after_command = replace_owner
        result = self.engine(fake).apply(WindowTarget(window().identity, A, False, True), SESSION)
        self.assertEqual((result.status, result.reason), (TransitionStatus.SKIPPED, 'owner_changed'))
        self.assertEqual(result.attempted_operations, ('deminimize',))

    def test_minimize_guard_rejects_changed_session_owner_hidden_or_protected_windows(self):
        for changes in ({'owner': ProcessIdentity(101, 2_000_000, 0)}, {'hidden': True}, {'sticky': True}):
            with self.subTest(changes=changes):
                fake = FakeYabai(window(**changes))
                with self.assertRaises(YabaiError):
                    self.engine(fake).desktop.minimize(window().identity, SESSION, True)
                self.assertEqual(fake.commands, [])
        fake = FakeYabai(window())
        with self.assertRaises(YabaiError) as caught:
            self.engine(fake).desktop.minimize(window().identity, replace(SESSION, login_session_id=200), True)
        self.assertEqual(caught.exception.reason, YabaiFailure.SESSION_CHANGED)
        self.assertEqual(fake.commands, [])

    def test_name_fallback_preserves_unmatched_dock_state(self):
        before = observe(window(9, title='Match'), window(10, title='Other', minimized=True))
        saved = observe(window(title='Match'), session=replace(SESSION, login_session_id=200))
        plan = plan_restore(saved, before)
        changed = observe(before.windows[0], replace(before.windows[1], minimized=False))
        issues = RestoreCoordinator._verify(plan, before, changed)
        self.assertTrue(any(i.reason == 'fallback_unmatched_changed' for i in issues))
