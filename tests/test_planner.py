from dataclasses import replace
import json
import unittest
from uuid import UUID

from spacekeeper.planner import DecisionStatus as Status, Reason, plan_restore
from spacekeeper.snapshot import ProcessIdentity
from tests.support import A, B, DISPLAYS, FRAME, SESSION, observe, window


class PlannerTests(unittest.TestCase):
    def test_different_boot_login_or_user_rejects_everything_including_extras(self):
        saved = observe(window())
        sessions = (replace(SESSION, boot_uuid=str(UUID(int=999))),
                    replace(SESSION, uid=502), replace(SESSION, login_session_id=101))
        for session in sessions:
            with self.subTest(session=session):
                plan = plan_restore(saved, observe(window(), window(2, fullscreen=True), session=session))
                self.assertFalse(plan.accepted)
                self.assertEqual(plan.windows, ())
                self.assertEqual(plan.visible, ())
                self.assertIsNone(plan.focus)
                self.assertEqual(plan.issues[0].reason, Reason.SESSION_MISMATCH)

    def test_closed_window_is_skipped(self):
        plan = plan_restore(observe(window()), observe())
        self.assertEqual((plan.windows[0].status, plan.windows[0].reason), (Status.SKIPPED, Reason.CLOSED))

    def test_reused_pid_or_window_id_never_becomes_an_extra_fullscreen_target(self):
        saved = observe(window())
        replacements = (window(pid=500, owner=ProcessIdentity(500, 1_000_000, 1), fullscreen=True),
                        window(owner=ProcessIdentity(101, 1_000_001, 1), fullscreen=True),
                        window(owner=ProcessIdentity(101, 1_000_000, 2), fullscreen=True))
        for replacement in replacements:
            with self.subTest(replacement=replacement):
                plan = plan_restore(saved, observe(replacement))
                self.assertEqual(len(plan.windows), 1)
                self.assertEqual(plan.windows[0].reason, Reason.OWNER_CHANGED)
                self.assertIsNone(plan.windows[0].target)

    def test_title_changes_do_not_change_identity(self):
        plan = plan_restore(observe(window()), observe(window(title="Changed document")))
        self.assertEqual(plan.windows[0].status, Status.UNCHANGED)

    def test_unknown_identity_is_never_trusted(self):
        pairs = ((observe(window(owner=None)), observe(window())),
                 (observe(window()), observe(window(owner=None))),
                 (observe(), observe(window(fullscreen=True, owner=None))))
        for saved, current in pairs:
            with self.subTest(saved=saved, current=current):
                decision = plan_restore(saved, current).windows[0]
                self.assertEqual(decision.reason, Reason.IDENTITY_UNAVAILABLE)
                self.assertIsNone(decision.target)

    def test_monitor_indexes_and_frames_do_not_drive_matching(self):
        saved = observe(window(fullscreen=True, display=A))
        displays = (replace(DISPLAYS[0], index=9, frame=FRAME), replace(DISPLAYS[1], index=1))
        plan = plan_restore(saved, observe(window(display=B), displays=displays))
        decision = plan.windows[0]
        self.assertEqual(decision.status, Status.PLANNED)
        self.assertEqual(decision.target.display_uuid, A)
        self.assertTrue(decision.target.fullscreen)
        self.assertNotIn("index", json.dumps(plan.to_dict()))

    def test_missing_monitor_uses_current_monitor_and_reports_fallback(self):
        plan = plan_restore(observe(window(fullscreen=True, display=B)),
                            observe(window(display=A), displays=DISPLAYS[:1]))
        decision = plan.windows[0]
        self.assertTrue(decision.monitor_fallback)
        self.assertEqual(decision.target.display_uuid, A)
        self.assertTrue(decision.target.fullscreen)

    def test_missing_monitor_fallback_can_already_match(self):
        plan = plan_restore(observe(window(fullscreen=True, display=B)),
                            observe(window(fullscreen=True, display=A), displays=DISPLAYS[:1]))
        self.assertEqual(plan.windows[0].status, Status.UNCHANGED)
        self.assertTrue(plan.windows[0].monitor_fallback)
        self.assertEqual(plan.visible, ())

    def test_fullscreen_wrong_monitor_requires_a_normal_destination(self):
        saved = observe(window(fullscreen=True, display=A))
        current = observe(window(fullscreen=True, display=B), normal_displays=(B,))
        decision = plan_restore(saved, current).windows[0]
        self.assertEqual(decision.status, Status.BLOCKED)
        self.assertEqual(decision.reason, Reason.NO_NORMAL_DESTINATION)
        self.assertIsNone(decision.target)

    def test_normal_window_monitor_move_also_requires_normal_destination(self):
        plan = plan_restore(observe(window(display=A)), observe(window(display=B), normal_displays=(B,)))
        self.assertEqual(plan.windows[0].reason, Reason.NO_NORMAL_DESTINATION)

    def test_matching_fullscreen_window_does_not_need_a_normal_destination(self):
        value = observe(window(fullscreen=True), normal_displays=())
        self.assertEqual(plan_restore(value, value).windows[0].status, Status.UNCHANGED)

    def test_fullscreen_on_off_and_normal_monitor_targets(self):
        for wanted_fullscreen, actual_fullscreen, actual_display in ((True, False, A), (False, True, A),
                                                                   (True, True, B), (False, False, B)):
            with self.subTest(wanted_fullscreen=wanted_fullscreen, actual_fullscreen=actual_fullscreen):
                wanted = window(fullscreen=wanted_fullscreen)
                actual = window(fullscreen=actual_fullscreen, display=actual_display)
                decision = plan_restore(observe(wanted), observe(actual)).windows[0]
                self.assertEqual(decision.status, Status.PLANNED)
                self.assertEqual(decision.target.display_uuid, A)
                self.assertEqual(decision.target.fullscreen, wanted_fullscreen)

    def test_extra_fullscreen_exits_and_extra_ordinary_is_untouched(self):
        plan = plan_restore(observe(), observe(window(fullscreen=True), window(2, display=B)))
        fullscreen, ordinary = plan.windows
        self.assertEqual(fullscreen.reason, Reason.EXTRA_FULLSCREEN)
        self.assertFalse(fullscreen.target.fullscreen)
        self.assertEqual(fullscreen.target.display_uuid, A)
        self.assertEqual(ordinary.reason, Reason.EXTRA_ORDINARY)
        self.assertEqual(ordinary.status, Status.UNCHANGED)
        self.assertIsNone(ordinary.target)

    def test_protected_hidden_and_minimized_windows_are_excluded(self):
        changes = ({"sticky": True}, {"root_window": False}, {"role": "AXSystemWide"},
                   {"subrole": "AXDialog"}, {"minimized": True}, {"hidden": True})
        for change in changes:
            for saved in (observe(), observe(window(fullscreen=True))):
                with self.subTest(change=change, saved=saved):
                    result = plan_restore(saved, observe(window(fullscreen=True, **change))).windows[0]
                    self.assertEqual(result.status, Status.UNSUPPORTED)
                    self.assertIsNone(result.target)

    def test_unplaced_window_is_unsupported(self):
        value = window(space_id=None, display_uuid=None)
        result = plan_restore(observe(value), observe(value)).windows[0]
        self.assertEqual(result.reason, Reason.PLACEMENT_UNAVAILABLE)

    def test_visibility_and_focus_are_separate_final_targets(self):
        one, two = window(focused=True), window(2, fullscreen=True, display=B)
        saved = observe(one, two)
        plan = plan_restore(saved, observe(replace(one, focused=False), two))
        self.assertEqual([(v.display_uuid, v.identity.window_id) for v in plan.visible], [(A, 1), (B, 2)])
        self.assertEqual(plan.focus, one.identity)

    def test_no_arbitrary_replacement_for_a_closed_visible_or_focused_window(self):
        saved = observe(window(focused=True), window(2))
        plan = plan_restore(saved, observe(window(2)))
        self.assertEqual(plan.visible, ())
        self.assertIsNone(plan.focus)
        self.assertEqual({i.reason for i in plan.issues}, {Reason.FOCUS_UNAVAILABLE, Reason.VISIBLE_WINDOW_UNAVAILABLE})

    def test_blocked_window_is_not_focused_or_made_visible(self):
        plan = plan_restore(observe(window(fullscreen=True, focused=True)),
                            observe(window(fullscreen=True, display=B), normal_displays=(B,)))
        self.assertEqual(plan.visible, ())
        self.assertIsNone(plan.focus)

    def test_plan_is_deterministic_and_does_not_mutate_inputs(self):
        saved, current = observe(window(2), window(1)), observe(window(3, fullscreen=True), window(1))
        before = saved.to_json(), current.to_json()
        plan = plan_restore(saved, current)
        reordered = replace(saved, windows=tuple(reversed(saved.windows)))
        self.assertEqual(plan, plan_restore(reordered, current))
        self.assertEqual(before, (saved.to_json(), current.to_json()))
        self.assertEqual([d.window_id for d in plan.windows], [1, 2, 3])
        self.assertTrue(json.loads(json.dumps(plan.to_dict()))["accepted"])
