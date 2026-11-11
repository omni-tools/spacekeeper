from pathlib import Path
import tempfile
import unittest

from spacekeeper.restore import RestoreCoordinator
from spacekeeper.storage import SnapshotStore
from spacekeeper.yabai import Yabai
from tests.fake_yabai import Clock, FakeYabai
from tests.support import A, B, observe, window


class OptimisticRestoreTests(unittest.TestCase):
    def run_restore(self, fake, saved, *, configure=None):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = Yabai(executable='/synthetic/yabai', runner=fake, identities=fake, fullscreen_setter=fake, clock=clock)
            coordinator = RestoreCoordinator(adapter, SnapshotStore(root/'state'), lock_path=root/'lock/restore',
                                              timeout=1, poll_interval=.1, clock=clock, sleep=clock.sleep)
            if configure is not None:
                configure(adapter, coordinator)
            report = coordinator.restore(saved)
        return report, clock

    def test_ten_minimizes_share_confirmation(self):
        fake = FakeYabai(*(window(i) for i in range(1, 11)))
        saved = observe(*(window(i, minimized=True) for i in range(1, 11)))
        report, clock = self.run_restore(fake, saved)
        self.assertTrue(report.complete, report)
        self.assertLessEqual(sum(fake.query_counts.values()), 40)
        self.assertLessEqual(clock.now, .2)
        self.assertTrue(all(w.minimized for w in fake.windows.values()))

    def test_fullscreen_entry_uses_explicit_state_and_shared_final_verification(self):
        fake = FakeYabai(*(window(i, focused=i == 1) for i in range(1, 11)))
        saved = observe(*(window(i, fullscreen=True, focused=i == 1) for i in range(1, 11)))
        report, clock = self.run_restore(fake, saved)
        self.assertTrue(report.complete, report)
        self.assertLessEqual(sum(fake.query_counts.values()), 80)
        self.assertFalse(any('--toggle' in cmd for cmd in fake.commands))

    def test_blocked_order_does_not_consume_final_verification_timeout(self):
        saved = observe(window(1, fullscreen=True, focused=True), window(2, fullscreen=True))
        fake = FakeYabai(*saved.windows)
        fake.space_indexes = {1001: 4, 1002: 3}
        fake.failures['space_order'] = 'cannot send message to scripting-addition'
        report, clock = self.run_restore(fake, saved)
        self.assertFalse(report.complete)
        self.assertLess(clock.now, .5)
        self.assertTrue(any(i.reason == 'capability_unavailable' for i in report.issues))
        self.assertTrue(any(i.reason == 'fullscreen_order_mismatch' for i in report.issues))

    def test_ineffective_commands_cannot_report_success(self):
        fake = FakeYabai(window())
        fake.effects['minimize'] = False
        report, _ = self.run_restore(fake, observe(window(minimized=True)))
        self.assertFalse(report.complete)
        self.assertEqual(report.missed, 1)

    def test_fullscreen_monitor_move_still_checks_destination(self):
        fake = FakeYabai(window(fullscreen=True, focused=True, display=B))
        report, _ = self.run_restore(fake, observe(window(fullscreen=True, focused=True)))
        self.assertTrue(report.complete, report)
        self.assertEqual((fake.windows[1].display_uuid, fake.windows[1].fullscreen), (A, True))
        self.assertFalse(any('--toggle' in cmd for cmd in fake.commands))

    def test_ineffective_minimize_gets_one_repair_from_fresh_observed_state(self):
        fake = FakeYabai(window())
        fake.effects['minimize'] = False
        fake.after_command = lambda operation: fake.effects.update(minimize=True)
        report, _ = self.run_restore(fake, observe(window(minimized=True)))
        self.assertTrue(report.complete, report)
        self.assertEqual(sum('--minimize' in cmd for cmd in fake.commands), 2)
        self.assertEqual(report.windows[0].status, 'restored')

    def test_fullscreen_repair_repeats_the_requested_value_instead_of_toggling(self):
        fake = FakeYabai(window(focused=True))
        fake.effects['toggle'] = False
        def accepted(operation):
            if operation == 'toggle':
                fake.effects['toggle'] = True
        fake.after_command = accepted
        report, _ = self.run_restore(fake, observe(window(fullscreen=True, focused=True)))
        self.assertTrue(report.complete, report)
        self.assertEqual([cmd[-1] for cmd in fake.commands if '--set-fullscreen' in cmd], ['true', 'true'])

    def test_owner_change_before_setter_prevents_fullscreen_mutation(self):
        from dataclasses import replace
        from spacekeeper.snapshot import ProcessIdentity
        fake = FakeYabai(window(focused=True))
        calls = []
        def changed(pid):
            calls.append(pid)
            if len(calls) == 5:
                fake.windows[1] = replace(fake.windows[1], owner=ProcessIdentity(101, 2000000, 0))
        fake.on_process = changed
        report, _ = self.run_restore(fake, observe(window(fullscreen=True, focused=True)))
        self.assertFalse(report.complete)
        self.assertFalse(any('--set-fullscreen' in cmd for cmd in fake.commands))

    def test_repeated_command_failure_is_bounded_and_never_reports_success(self):
        fake = FakeYabai(window())
        fake.failures['minimize'] = 'synthetic command failure'
        report, _ = self.run_restore(fake, observe(window(minimized=True)))
        self.assertFalse(report.complete)
        self.assertEqual(sum('--minimize' in cmd for cmd in fake.commands), 2)

    def test_native_permission_failure_has_no_toggle_fallback(self):
        from spacekeeper.accessibility import AccessibilityError
        fake = FakeYabai(window(focused=True))
        def denied(*args, **kwargs):
            raise AccessibilityError('access_denied', 'Synthetic permission denial.')
        fake.set_fullscreen = denied
        report, _ = self.run_restore(fake, observe(window(fullscreen=True, focused=True)))
        self.assertFalse(report.complete)
        self.assertTrue(any(w.reason == 'access_denied' for w in report.windows))
        self.assertFalse(any('--toggle' in cmd for cmd in fake.commands))

    def test_ten_deminimizes_share_confirmation(self):
        fake = FakeYabai(*(window(i, minimized=True) for i in range(1, 11)))
        saved = observe(*(window(i, focused=i == 1) for i in range(1, 11)))
        report, clock = self.run_restore(fake, saved)
        self.assertTrue(report.complete, report)
        self.assertLessEqual(sum(fake.query_counts.values()), 40)
        self.assertLessEqual(clock.now, .2)

    def test_ten_monitor_moves_keep_dependency_guards_with_lower_query_budget(self):
        fake = FakeYabai(*(window(i, fullscreen=True, display=B, focused=i == 1) for i in range(1, 11)))
        saved = observe(*(window(i, fullscreen=True, focused=i == 1) for i in range(1, 11)))
        report, _ = self.run_restore(fake, saved)
        self.assertTrue(report.complete, report)
        self.assertLessEqual(sum(fake.query_counts.values()), 210)

    def test_unconfirmed_fullscreen_exit_cannot_authorize_a_monitor_move(self):
        fake = FakeYabai(window(fullscreen=True, display=B, focused=True))
        fake.effects['toggle'] = False
        report, _ = self.run_restore(fake, observe(window(fullscreen=True, focused=True)))
        self.assertFalse(report.complete)
        self.assertFalse(any('--space' in cmd for cmd in fake.commands))

    def test_unconfirmed_monitor_move_cannot_authorize_fullscreen_entry(self):
        fake = FakeYabai(window(display=B, focused=True))
        fake.effects['move'] = False
        report, _ = self.run_restore(fake, observe(window(fullscreen=True, focused=True)))
        self.assertFalse(report.complete)
        self.assertFalse(any('--set-fullscreen' in cmd for cmd in fake.commands))

    def test_dock_request_handles_already_changed_state_after_a_command_error(self):
        from spacekeeper.process import ProcessResult
        class RacingMinimize(FakeYabai):
            def __call__(self, argv, **options):
                result = super().__call__(argv, **options)
                if '--minimize' in argv:
                    return ProcessResult(1, b'', b'window is already minimized')
                return result
        fake = RacingMinimize(window())
        report, _ = self.run_restore(fake, observe(window(minimized=True)))
        self.assertTrue(report.complete, report)
        self.assertEqual(sum('--minimize' in cmd for cmd in fake.commands), 1)

    def test_already_matching_dock_request_does_not_send_an_error_prone_command(self):
        from tests.support import SESSION
        fake = FakeYabai(window(minimized=True))
        adapter = Yabai(executable='/synthetic/yabai', runner=fake, identities=fake, fullscreen_setter=fake)
        adapter.request_minimize(window().identity, SESSION, True)
        self.assertEqual(fake.commands, [])

    def test_verified_state_can_resolve_an_uncertain_command_failure(self):
        import json
        from spacekeeper.process import ProcessResult
        class DelayedCache(FakeYabai):
            def __call__(self, argv, **options):
                result = super().__call__(argv, **options)
                if '--minimize' in argv:
                    return ProcessResult(1, b'', b'could not minimize window')
                if '--window' in argv:
                    raw = json.loads(result.stdout)
                    raw['is-minimized'] = False
                    return ProcessResult(0, json.dumps(raw).encode(), b'')
                return result
        fake = DelayedCache(window())
        report, _ = self.run_restore(fake, observe(window(minimized=True)))
        self.assertTrue(report.complete, report)
        self.assertEqual(report.windows[0].reason, 'verified_after_command_error')

    def test_already_minimized_window_needs_no_edit_even_if_ax_metadata_is_unavailable(self):
        import json
        from spacekeeper.process import ProcessResult
        class MinimizedMetadata(FakeYabai):
            def __call__(self, argv, **options):
                result = super().__call__(argv, **options)
                if '--minimize' in argv:
                    return ProcessResult(1, b'', b'window already minimized')
                if '--window' in argv:
                    raw = json.loads(result.stdout)
                    if raw['is-minimized']:
                        raw.update(role='', subrole='', **{'root-window': False})
                    return ProcessResult(0, json.dumps(raw).encode(), b'')
                return result
        fake = MinimizedMetadata(window())
        report, _ = self.run_restore(fake, observe(window(minimized=True)))
        self.assertTrue(report.complete, report)
        self.assertEqual(sum('--minimize' in cmd for cmd in fake.commands), 1)

    def test_a_needed_dock_edit_still_rejects_dialogs(self):
        from tests.support import SESSION
        from spacekeeper.yabai import YabaiError
        fake = FakeYabai(window(subrole='AXDialog', minimized=True))
        adapter = Yabai(executable='/synthetic/yabai', runner=fake, identities=fake, fullscreen_setter=fake)
        with self.assertRaises(YabaiError) as error:
            adapter.request_minimize(window().identity, SESSION, False)
        self.assertEqual(error.exception.reason, 'unsupported_window')
        self.assertEqual(fake.commands, [])

    def test_ordering_waits_for_pending_fullscreen_windows_missing_during_animation(self):
        from dataclasses import replace
        fake = FakeYabai(window(1, focused=True), window(2))
        gaps = []
        def configure(adapter, coordinator):
            original = adapter.observe
            def transient_gap(**options):
                snapshot = original(**options)
                if any('--set-fullscreen' in cmd for cmd in fake.commands) and not gaps:
                    gaps.append(True)
                    return replace(snapshot, windows=tuple(w for w in snapshot.windows if w.id != 2),
                                   spaces=tuple(s for s in snapshot.spaces if s.id != 1002))
                return snapshot
            adapter.observe = transient_gap
        saved = observe(window(1, fullscreen=True, focused=True), window(2, fullscreen=True))
        report, _ = self.run_restore(fake, saved, configure=configure)
        self.assertTrue(report.complete, report)
        self.assertEqual(gaps, [True])
