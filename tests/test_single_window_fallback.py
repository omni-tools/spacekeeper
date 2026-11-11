from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from spacekeeper.planner import plan_restore
from spacekeeper.restore import RestoreCoordinator
from spacekeeper.snapshot import ProcessIdentity
from spacekeeper.storage import SnapshotStore
from spacekeeper.yabai import Yabai
from tests.fake_yabai import Clock, FakeYabai
from tests.support import SESSION, observe, window


STALE = replace(SESSION, login_session_id=200)


class SingleWindowFallbackTests(unittest.TestCase):
    def test_single_saved_and_current_app_window_match_despite_changed_title(self):
        saved = observe(window(app='Synthetic App', title='Old title', minimized=True), session=STALE)
        actual = window(9, app='SYNTHETIC APP', title='New title')
        plan = plan_restore(saved, observe(actual))
        self.assertFalse(plan.issues)
        decision = plan.windows[0]
        self.assertEqual(decision.source_window_id, 1)
        self.assertEqual(decision.target.identity, actual.identity)
        self.assertTrue(decision.target.minimized)
        self.assertEqual(decision.match_method, 'single_window_app')

    def test_multiple_observed_windows_prevent_app_only_matching(self):
        for extra in (window(10, title='Another'), window(10, hidden=True), window(10, minimized=True),
                      window(10, role='', subrole='', title='')):
            with self.subTest(extra=extra):
                plan = plan_restore(observe(window(title='Old'), session=STALE),
                                    observe(window(9, title='New'), extra))
                self.assertTrue(all(d.target is None for d in plan.windows))
        saved = observe(window(title='Old'), window(2, title='Other'), session=STALE)
        plan = plan_restore(saved, observe(window(9, title='New')))
        self.assertTrue(all(d.target is None for d in plan.windows))

    def test_known_helper_dialogs_do_not_block_a_single_main_window_match(self):
        saved = observe(window(title='Old'), window(2, sticky=True, subrole='AXDialog'), session=STALE)
        actual = window(9, title='New')
        current = observe(actual, window(10, sticky=True, subrole='AXDialog'))
        plan = plan_restore(saved, current)
        matches = [d for d in plan.windows if d.target is not None]
        self.assertFalse(plan.issues)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].target.identity, actual.identity)
        self.assertEqual(matches[0].match_method, 'single_window_app')

    def test_missing_role_metadata_is_reported_after_binding_the_single_main_window(self):
        saved = observe(window(title='Old'), window(2, sticky=True, subrole='AXDialog'), session=STALE)
        current = observe(window(9, title='', role='', subrole=''), window(10, sticky=True, subrole='AXDialog'))
        plan = plan_restore(saved, current)
        self.assertTrue(all(d.target is None for d in plan.windows))
        self.assertEqual(plan.issues[0].reason.value, 'metadata_unavailable')
        self.assertEqual(plan.issues[0].window_id, 9)

    def test_one_unmatched_remainder_is_not_a_single_window_app(self):
        saved = observe(window(title='Exact'), window(2, title='Old'), session=STALE)
        plan = plan_restore(saved, observe(window(9, title='Exact'), window(10, title='New')))
        self.assertIsNotNone(plan.windows[0].target)
        self.assertEqual(plan.windows[0].match_method, 'app_title')
        self.assertIsNone(plan.windows[1].target)

    def test_single_window_does_not_bypass_strict_mode_user_or_app_names(self):
        saved = observe(window(title='Old'), session=STALE)
        current = observe(window(9, title='New'))
        self.assertFalse(plan_restore(saved, current, fallback=False).accepted)
        self.assertFalse(plan_restore(replace(saved, session=replace(STALE, uid=502)), current).accepted)
        different_app = plan_restore(saved, observe(window(9, app='Other', title='New')))
        self.assertTrue(all(d.target is None for d in different_app.windows))
        replacement = window(title='New', owner=ProcessIdentity(101, 2_000_000, 0))
        strict = plan_restore(observe(window(title='Old')), observe(replacement))
        self.assertEqual(strict.windows[0].reason.value, 'owner_changed')

    def test_single_window_still_requires_supported_current_owner(self):
        for changes, reason in (({'owner': None}, 'identity_unavailable'),
                                ({'hidden': True}, 'hidden_or_minimized'),
                                ({'role': ''}, 'metadata_unavailable')):
            with self.subTest(changes=changes):
                saved = observe(window(title='Old'), session=STALE)
                plan = plan_restore(saved, observe(window(9, title='New', **changes)))
                self.assertTrue(all(d.target is None for d in plan.windows))
                self.assertEqual(plan.issues[0].reason.value, reason)

    def test_single_window_executes_and_reports_its_matching_method(self):
        with tempfile.TemporaryDirectory() as temp:
            helper = window(10, sticky=True, subrole='AXDialog')
            fake, clock = FakeYabai(window(9, title='New'), helper), Clock()
            adapter = Yabai(executable='/synthetic/yabai', runner=fake, identities=fake, fullscreen_setter=fake, clock=clock)
            root = Path(temp)
            coordinator = RestoreCoordinator(adapter, SnapshotStore(root/'state'), lock_path=root/'lock/restore.lock',
                                             timeout=1, clock=clock, sleep=clock.sleep)
            report = coordinator.restore(observe(window(title='Old', minimized=True),
                                                  window(2, sticky=True, subrole='AXDialog'),
                                                  session=STALE, visible_ids=()))
            self.assertTrue(report.complete, report)
            self.assertTrue(fake.windows[9].minimized)
            self.assertEqual(report.windows[0].match_method, 'single_window_app')
            self.assertEqual(fake.windows[10], helper)

    def test_owner_change_after_app_binding_prevents_all_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            fake, clock = FakeYabai(window(9, title='New')), Clock()
            adapter = Yabai(executable='/synthetic/yabai', runner=fake, identities=fake, fullscreen_setter=fake, clock=clock)
            root = Path(temp)
            def replace_owner(plan):
                fake.windows[9] = replace(fake.windows[9], owner=ProcessIdentity(109, 2_000_000, 0))
            coordinator = RestoreCoordinator(adapter, SnapshotStore(root/'state'), lock_path=root/'lock/restore.lock',
                                             timeout=1, clock=clock, sleep=clock.sleep, on_plan=replace_owner)
            report = coordinator.restore(observe(window(title='Old', minimized=True), session=STALE))
            self.assertFalse(report.complete)
            self.assertEqual(report.windows[0].reason, 'owner_changed')
            self.assertEqual(fake.commands, [])
