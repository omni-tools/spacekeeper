from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from uuid import UUID

from spacekeeper.planner import plan_restore
from spacekeeper.restore import RestoreCoordinator
from spacekeeper.snapshot import ProcessIdentity
from spacekeeper.storage import SnapshotStore
from spacekeeper.yabai import Yabai
from tests.fake_yabai import Clock, FakeYabai
from tests.support import A, B, DISPLAYS, SESSION, observe, window


STALE = replace(SESSION, boot_uuid=str(UUID(int=2000)), login_session_id=200)


class NameFallbackTests(unittest.TestCase):
    def test_code_matches_trailing_workspace_despite_active_file_changes(self):
        for app in ('Code', 'Code - OSS', 'Code - Insiders'):
            for before, after in (('old.py — Workspace', 'new.py — WORKSPACE'),
                                  ('Workspace', 'new.py — Workspace'),
                                  ('old.py — Workspace', 'Workspace'),
                                  ('file — with separator — Workspace', 'new.py — Workspace')):
                with self.subTest(app=app, before=before, after=after):
                    saved = observe(window(app=app, title=before, minimized=True), session=STALE)
                    actual = window(9, app=app, title=after)
                    plan = plan_restore(saved, observe(actual))
                    self.assertFalse(plan.issues)
                    self.assertEqual(plan.windows[0].source_window_id, 1)
                    self.assertEqual(plan.windows[0].target.identity, actual.identity)
                    self.assertTrue(plan.windows[0].target.minimized)

    def test_duplicate_code_workspaces_remain_ambiguous(self):
        scenarios = (
            ((window(app='Code', title='one.py — Workspace'),),
             (window(9, app='Code', title='one.py — Workspace'),
              window(10, app='Code', title='two.py — Workspace'))),
            ((window(app='Code', title='one.py — Workspace'),
              window(2, app='Code', title='two.py — Workspace')),
             (window(9, app='Code', title='one.py — Workspace'),)),
        )
        for sources, current in scenarios:
            with self.subTest(sources=len(sources), current=len(current)):
                plan = plan_restore(observe(*sources, session=STALE), observe(*current))
                self.assertTrue(all(d.target is None for d in plan.windows))
                self.assertTrue(all(i.reason.value == 'name_ambiguous' for i in plan.issues))

    def test_workspace_matching_stays_within_the_same_code_app(self):
        for source_app, target_app in (('Browser', 'Browser'), ('Code', 'Code - OSS')):
            with self.subTest(source_app=source_app, target_app=target_app):
                saved = observe(window(app=source_app, title='old.py — Workspace'), session=STALE)
                plan = plan_restore(saved, observe(window(9, app=target_app, title='new.py — Workspace'),
                                                   window(10, app=target_app, title='Another workspace')))
                self.assertTrue(all(d.target is None for d in plan.windows))
                self.assertEqual(plan.issues[0].reason.value, 'name_no_match')

    def test_code_extension_hosts_are_distinct_from_regular_workspaces(self):
        saved = observe(window(app='Code', title='[Extension Development Host] old.py — Workspace'), session=STALE)
        current = observe(window(9, app='Code', title='[Extension Development Host] new.py — Workspace'),
                          window(10, app='Code', title='regular.py — Workspace'))
        plan = plan_restore(saved, current)
        self.assertFalse(plan.issues)
        self.assertIsNotNone(plan.windows[0].target)
        self.assertIsNone(plan.windows[1].target)
        bare = replace(saved, windows=(replace(saved.windows[0], title='[Extension Development Host] Workspace'),))
        self.assertEqual(plan_restore(bare, current).windows[0].target.identity, current.windows[0].identity)

    def test_code_same_session_owner_changes_still_cannot_match_by_workspace(self):
        saved = observe(window(app='Code', title='old.py — Workspace'))
        actual = window(app='Code', title='new.py — Workspace', owner=ProcessIdentity(101, 2_000_000, 0))
        decision = plan_restore(saved, observe(actual)).windows[0]
        self.assertEqual(decision.reason.value, 'owner_changed')
        self.assertIsNone(decision.target)

    def test_code_workspace_match_restores_dock_state_with_fresh_owner_guards(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            saved = observe(window(app='Code', title='old.py — Workspace', minimized=True), session=STALE)
            fake, clock = FakeYabai(window(9, app='Code', title='new.py — Workspace')), Clock()
            adapter = Yabai(executable='/synthetic/yabai', runner=fake, identities=fake, clock=clock)
            coordinator = RestoreCoordinator(adapter, SnapshotStore(root/'state'), lock_path=root/'lock/restore.lock',
                                             timeout=1, clock=clock, sleep=clock.sleep)
            report = coordinator.restore(saved)
            self.assertTrue(report.complete, report)
            self.assertTrue(fake.windows[9].minimized)
            self.assertEqual(report.windows[0].source_window_id, 1)
            self.assertEqual(report.windows[0].attempted_operations, ('minimize',))

    def test_missing_focus_monitor_does_not_leave_conflicting_visibility_targets(self):
        for old_session, ids in ((SESSION, (1, 2)), (STALE, (9, 10))):
            with self.subTest(name_fallback=old_session == STALE):
                saved = observe(window(1, title='Left'),
                                window(2, title='Right', display=B, fullscreen=True, focused=True), session=old_session)
                current = observe(window(ids[0], title='Left', focused=True),
                                  window(ids[1], title='Right', fullscreen=True), displays=DISPLAYS[:1])
                plan = plan_restore(saved, current)
                self.assertEqual(plan.focus.window_id, ids[1])
                self.assertEqual([(v.display_uuid, v.identity.window_id) for v in plan.visible], [(A, ids[1])])

    def test_collapsed_monitor_restore_can_verify_focus_and_visibility_together(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            saved = observe(window(1, title='Left'),
                            window(2, title='Right', display=B, fullscreen=True, focused=True), session=STALE)
            fake, clock = FakeYabai(window(9, title='Left', focused=True),
                                    window(10, title='Right', fullscreen=True)), Clock()
            fake.displays, fake.normal_displays = DISPLAYS[:1], (A,)
            adapter = Yabai(executable='/synthetic/yabai', runner=fake, identities=fake, clock=clock)
            coordinator = RestoreCoordinator(adapter, SnapshotStore(root/'state'), lock_path=root/'lock/restore.lock',
                                             timeout=1, clock=clock, sleep=clock.sleep)
            report = coordinator.restore(saved)
            self.assertTrue(report.complete, report)
            self.assertTrue(fake.windows[10].focused)
    def test_stale_names_rebind_current_owners_and_never_reuse_old_ids(self):
        saved = observe(window(fullscreen=True, display=B, focused=True), session=STALE)
        actual = window(9, app='SYNTHETIC EDITOR', title='SYNTHETIC DOCUMENT')
        collision = window(1, app='Other', fullscreen=True)
        plan = plan_restore(saved, observe(actual, collision))
        self.assertTrue(plan.accepted)
        self.assertTrue(plan.name_fallback)
        decisions = {d.window_id: d for d in plan.windows}
        self.assertEqual(len(decisions), len(plan.windows))
        matched = decisions[9]
        self.assertEqual(matched.source_window_id, 1)
        self.assertEqual(matched.target.identity, actual.identity)
        self.assertEqual((matched.target.display_uuid, matched.target.fullscreen), (B, True))
        self.assertEqual(decisions[1].reason.value, 'fallback_unmatched')
        self.assertIsNone(decisions[1].target)
        self.assertEqual(plan.focus, actual.identity)
        self.assertEqual(plan.visible[0].identity, actual.identity)

    def test_missing_and_ambiguous_names_are_partial_without_guessing(self):
        scenarios = (
            (observe(window(), session=STALE),
             observe(window(9, title='Different'), window(10, title='Another')), 'name_no_match'),
            (observe(window(), window(2), session=STALE), observe(window(9)), 'name_ambiguous'),
            (observe(window(), session=STALE), observe(window(9), window(10)), 'name_ambiguous'),
            (observe(window(), session=STALE), observe(window(9), window(10, hidden=True)), 'name_ambiguous'),
        )
        for saved, current, reason in scenarios:
            with self.subTest(reason=reason):
                plan = plan_restore(saved, current)
                self.assertTrue(plan.accepted)
                self.assertTrue(plan.name_fallback)
                self.assertTrue(all(d.target is None for d in plan.windows))
                self.assertTrue(any(i.reason.value == reason and i.source_window_id == 1 for i in plan.issues))

    def test_no_fallback_and_different_users_still_reject_the_snapshot(self):
        saved = observe(window(), session=STALE)
        current = observe(window(9))
        self.assertFalse(plan_restore(saved, current, fallback=False).accepted)
        self.assertFalse(plan_restore(replace(saved, session=replace(STALE, uid=502)), current).accepted)

    def test_titles_are_literal_names_not_glob_patterns(self):
        saved = observe(window(title='File*.txt'), session=STALE)
        missing = plan_restore(saved, observe(window(9, title='FileOne.txt'), window(10, title='Other')))
        self.assertTrue(any(i.reason.value == 'name_no_match' for i in missing.issues))
        matched = plan_restore(saved, observe(window(9, title='FILE*.TXT')))
        self.assertIsNotNone(matched.windows[0].target)

    def test_reused_numeric_id_is_matched_by_name_to_its_current_owner(self):
        actual = window(owner=ProcessIdentity(101, 2_000_000, 0))
        plan = plan_restore(observe(window(fullscreen=True), session=STALE), observe(actual))
        self.assertTrue(plan.name_fallback)
        self.assertEqual(len(plan.windows), 1)
        self.assertEqual(plan.windows[0].target.identity, actual.identity)
        self.assertNotEqual(plan.windows[0].target.identity, window().identity)

    def test_same_session_replacement_retains_strict_owner_rejection(self):
        actual = window(owner=ProcessIdentity(101, 2_000_000, 0))
        plan = plan_restore(observe(window(fullscreen=True)), observe(actual))
        self.assertFalse(plan.name_fallback)
        self.assertEqual(plan.windows[0].reason.value, 'owner_changed')

    def test_missing_monitor_and_unsupported_current_owners_are_reported(self):
        saved = observe(window(fullscreen=True, display=B), session=STALE)
        matched = plan_restore(saved, observe(window(9), displays=DISPLAYS[:1]))
        self.assertTrue(matched.windows[0].monitor_fallback)
        self.assertEqual(matched.windows[0].target.display_uuid, A)
        unsupported = plan_restore(saved, observe(window(9, owner=None)))
        self.assertTrue(any(i.reason.value == 'identity_unavailable' for i in unsupported.issues))
        self.assertTrue(all(d.target is None for d in unsupported.windows))

    def test_restore_executes_in_current_session_and_verifies_unmatched_windows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            saved = observe(window(fullscreen=True, display=B, focused=True), session=STALE)
            untouched = window(1, app='Other', fullscreen=True)
            fake, clock = FakeYabai(untouched, window(9, focused=True)), Clock()
            adapter = Yabai(executable='/synthetic/yabai', runner=fake, identities=fake, clock=clock)
            coordinator = RestoreCoordinator(adapter, SnapshotStore(root/'state'), lock_path=root/'lock/restore.lock',
                                             clock=clock, sleep=clock.sleep)
            report = coordinator.restore(saved)
            self.assertTrue(report.complete, report)
            self.assertTrue(report.name_fallback)
            self.assertEqual(fake.windows[1], untouched)
            self.assertTrue(fake.windows[9].fullscreen)
            self.assertEqual(fake.windows[9].display_uuid, B)
            self.assertTrue(Path(report.backup_path).is_file())
            self.assertEqual(next(w for w in report.windows if w.window_id == 9).source_window_id, 1)

    def test_owner_replacement_after_name_binding_cannot_authorize_a_toggle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake, clock = FakeYabai(window(9)), Clock()
            fake.after_command = lambda operation: fake.windows.update({
                9: replace(fake.windows[9], owner=ProcessIdentity(109, 2_000_000, 0))})
            adapter = Yabai(executable='/synthetic/yabai', runner=fake, identities=fake, clock=clock)
            coordinator = RestoreCoordinator(adapter, SnapshotStore(root/'state'), lock_path=root/'lock/restore.lock',
                                             timeout=1, clock=clock, sleep=clock.sleep)
            report = coordinator.restore(observe(window(fullscreen=True), session=STALE))
            self.assertFalse(report.complete)
            self.assertTrue(any(w.reason == 'owner_changed' for w in report.windows))
            self.assertFalse(any('--toggle' in command for command in fake.commands))

    def test_unmatched_changes_do_not_mislabel_a_matched_window_with_the_same_source_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake, clock = FakeYabai(window(1, app='Other', fullscreen=True), window(9)), Clock()
            def changed(operation):
                fake.windows[1] = replace(fake.windows[1], fullscreen=False, space_id=101)
            fake.after_command = changed
            adapter = Yabai(executable='/synthetic/yabai', runner=fake, identities=fake, clock=clock)
            coordinator = RestoreCoordinator(adapter, SnapshotStore(root/'state'), lock_path=root/'lock/restore.lock',
                                             timeout=1, clock=clock, sleep=clock.sleep)
            report = coordinator.restore(observe(window(fullscreen=True, focused=True), session=STALE))
            self.assertFalse(report.complete)
            self.assertTrue(any(i.reason == 'fallback_unmatched_changed' for i in report.issues))
            self.assertEqual(next(w for w in report.windows if w.window_id == 9).status, 'restored')
