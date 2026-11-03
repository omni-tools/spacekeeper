from dataclasses import replace
import json
import unittest

from spacekeeper.planner import WindowTarget, plan_restore
from spacekeeper.snapshot import ProcessIdentity
from spacekeeper.transitions import TransitionStatus as Status, WindowTransitions
from spacekeeper.yabai import Yabai, YabaiError, YabaiFailure
from tests.fake_yabai import Clock, FakeYabai
from tests.support import A, B, DISPLAYS, SESSION, observe, window


class TransitionTests(unittest.TestCase):
    def engine(self, desktop):
        clock = Clock()
        adapter = Yabai(executable="/synthetic/yabai", runner=desktop, identities=desktop, clock=clock)
        return WindowTransitions(adapter, transition_timeout=1, poll_interval=0.1, clock=clock, sleep=clock.sleep)

    def target(self, *, fullscreen=True, display=A):
        return WindowTarget(window().identity, display, fullscreen)

    def test_monitor_first_regression_from_planner_through_real_adapter(self):
        saved, actual = window(fullscreen=True, display=A), window(fullscreen=True, display=B)
        plan = plan_restore(observe(saved), observe(actual))
        desktop = FakeYabai(actual)
        result = self.engine(desktop).apply(plan.windows[0].target, SESSION)
        self.assertEqual(result.status, Status.VERIFIED)
        self.assertEqual(result.attempted_operations,
                         ("focus", "exit_fullscreen", "move_to_display", "focus", "enter_fullscreen"))
        commands = desktop.commands
        self.assertEqual(commands[0], ("-m", "window", "--focus", "1"))
        self.assertEqual(commands[1], ("-m", "window", "1", "--toggle", "native-fullscreen"))
        self.assertEqual(commands[2][:4], ("-m", "window", "1", "--space"))
        self.assertEqual(commands[3], ("-m", "window", "--focus", "1"))
        self.assertEqual(commands[4], ("-m", "window", "1", "--toggle", "native-fullscreen"))
        self.assertEqual((desktop.windows[1].display_uuid, desktop.windows[1].fullscreen), (A, True))

    def test_ordinary_window_moves_before_entering_fullscreen(self):
        desktop = FakeYabai(window(display=B))
        result = self.engine(desktop).apply(self.target(), SESSION)
        self.assertEqual(result.status, Status.VERIFIED)
        self.assertEqual(result.attempted_operations, ("move_to_display", "focus", "enter_fullscreen"))

    def test_saved_normal_window_exits_fullscreen_and_moves(self):
        desktop = FakeYabai(window(fullscreen=True, display=B))
        result = self.engine(desktop).apply(self.target(fullscreen=False), SESSION)
        self.assertEqual(result.status, Status.VERIFIED)
        self.assertEqual(result.attempted_operations, ("focus", "exit_fullscreen", "move_to_display"))

    def test_matching_window_is_verified_without_commands(self):
        desktop = FakeYabai(window(fullscreen=True))
        result = self.engine(desktop).apply(self.target(), SESSION)
        self.assertEqual(result.status, Status.VERIFIED)
        self.assertEqual(desktop.commands, [])
        self.assertTrue(json.loads(json.dumps(result.to_dict()))["status"] == "verified")

    def test_extra_fullscreen_target_only_exits_fullscreen(self):
        extra = window(fullscreen=True)
        plan = plan_restore(observe(), observe(extra))
        desktop = FakeYabai(extra)
        result = self.engine(desktop).apply(plan.windows[0].target, SESSION)
        self.assertEqual(result.status, Status.VERIFIED)
        self.assertEqual(result.attempted_operations, ("focus", "exit_fullscreen"))

    def test_missing_monitor_fallback_is_executed_on_current_monitor(self):
        desktop = FakeYabai(window())
        desktop.displays = DISPLAYS[:1]
        plan = plan_restore(observe(window(fullscreen=True, display=B)), desktop_snapshot(desktop))
        result = self.engine(desktop).apply(plan.windows[0].target, SESSION)
        self.assertEqual(result.status, Status.VERIFIED)
        self.assertEqual(desktop.windows[1].display_uuid, A)

    def test_unconfirmed_exit_prevents_move_and_reentry(self):
        desktop = FakeYabai(window(fullscreen=True, display=B))
        desktop.effects["toggle"] = False
        result = self.engine(desktop).apply(self.target(), SESSION)
        self.assertEqual((result.status, result.reason), (Status.FAILED, "timeout"))
        self.assertEqual(result.attempted_operations, ("focus", "exit_fullscreen"))
        self.assertFalse(any("--space" in command for command in desktop.commands))

    def test_unconfirmed_move_prevents_entering_fullscreen(self):
        desktop = FakeYabai(window(display=B))
        desktop.effects["move"] = False
        result = self.engine(desktop).apply(self.target(), SESSION)
        self.assertEqual((result.status, result.reason), (Status.FAILED, "timeout"))
        self.assertEqual(result.attempted_operations, ("move_to_display",))
        self.assertFalse(any("--toggle" in command for command in desktop.commands))

    def test_unconfirmed_focus_prevents_entering_fullscreen(self):
        desktop = FakeYabai(window())
        desktop.effects["focus"] = False
        result = self.engine(desktop).apply(self.target(), SESSION)
        self.assertEqual((result.status, result.reason), (Status.FAILED, "timeout"))
        self.assertEqual(result.attempted_operations, ("focus",))
        self.assertFalse(any("--toggle" in command for command in desktop.commands))

    def test_wrong_monitor_after_fullscreen_entry_is_not_reported_verified(self):
        desktop = FakeYabai(window())
        def wrong_monitor(operation):
            if operation == "toggle":
                desktop.windows[1] = replace(desktop.windows[1], display_uuid=B)
        desktop.after_command = wrong_monitor
        result = self.engine(desktop).apply(self.target(), SESSION)
        self.assertEqual((result.status, result.reason), (Status.FAILED, "timeout"))

    def test_command_failure_stops_later_stages(self):
        desktop = FakeYabai(window(display=B))
        desktop.failures["move"] = "synthetic failed command"
        result = self.engine(desktop).apply(self.target(), SESSION)
        self.assertEqual((result.status, result.reason), (Status.FAILED, "command_failed"))
        self.assertEqual(result.attempted_operations, ("move_to_display",))

    def test_capability_failure_is_explicit_and_never_triggers_sa_setup(self):
        desktop = FakeYabai(window(display=B))
        desktop.failures["move"] = "scripting-addition is unavailable"
        result = self.engine(desktop).apply(self.target(), SESSION)
        self.assertEqual((result.status, result.reason), (Status.UNSUPPORTED, "capability_unavailable"))
        self.assertEqual(len(desktop.commands), 1)
        self.assertNotIn("--load-sa", str(desktop.commands))

    def test_closed_window_after_focus_is_skipped_before_toggle(self):
        desktop = FakeYabai(window())
        desktop.after_command = lambda operation: desktop.windows.clear()
        result = self.engine(desktop).apply(self.target(), SESSION)
        self.assertEqual((result.status, result.reason), (Status.SKIPPED, "window_missing"))
        self.assertEqual(result.attempted_operations, ("focus",))

    def test_replaced_owner_after_focus_is_skipped_before_toggle(self):
        desktop = FakeYabai(window())
        def replace_owner(operation):
            desktop.windows[1] = replace(desktop.windows[1], owner=ProcessIdentity(101, 2_000_000, 0))
        desktop.after_command = replace_owner
        result = self.engine(desktop).apply(self.target(), SESSION)
        self.assertEqual((result.status, result.reason), (Status.SKIPPED, "owner_changed"))
        self.assertEqual(result.attempted_operations, ("focus",))

    def test_session_change_stops_a_transition(self):
        desktop = FakeYabai(window())
        desktop.after_command = lambda operation: setattr(desktop, "session_id", replace(SESSION, login_session_id=101))
        result = self.engine(desktop).apply(self.target(), SESSION)
        self.assertEqual((result.status, result.reason), (Status.FAILED, "session_changed"))

    def test_disconnected_target_stops_later_stages(self):
        desktop = FakeYabai(window())
        def disconnect(operation):
            desktop.displays = DISPLAYS[1:]
            desktop.windows[1] = replace(desktop.windows[1], display_uuid=B, space_id=102)
        desktop.after_command = disconnect
        result = self.engine(desktop).apply(self.target(), SESSION)
        self.assertEqual((result.status, result.reason), (Status.FAILED, "display_missing"))
        self.assertEqual(result.attempted_operations, ("focus",))

    def test_transient_desktop_churn_is_polled_until_it_settles(self):
        desktop = FakeYabai(window())
        engine = self.engine(desktop)
        original = engine.desktop.observe
        failures = []
        def observe_during_animation(**options):
            if desktop.commands and not failures:
                failures.append(True)
                raise YabaiError(YabaiFailure.DESKTOP_CHANGED, "Synthetic fullscreen animation.")
            return original(**options)
        engine.desktop.observe = observe_during_animation
        result = engine.apply(self.target(), SESSION)
        self.assertEqual(result.status, Status.VERIFIED)
        self.assertEqual(failures, [True])

    def test_fullscreen_transition_can_temporarily_omit_the_same_window(self):
        desktop = FakeYabai(window())
        engine = self.engine(desktop)
        original = engine.desktop.observe
        omitted = []
        def observe_during_fullscreen_animation(**options):
            if any('--toggle' in command for command in desktop.commands) and not omitted:
                omitted.append(True)
                return observe()
            return original(**options)
        engine.desktop.observe = observe_during_fullscreen_animation
        result = engine.apply(self.target(), SESSION)
        self.assertEqual(result.status, Status.VERIFIED)
        self.assertEqual(omitted, [True])

    def test_owner_change_after_a_fullscreen_query_gap_is_still_skipped(self):
        desktop = FakeYabai(window())
        engine = self.engine(desktop)
        original = engine.desktop.observe
        omitted = []
        def observe_replaced_window(**options):
            if any('--toggle' in command for command in desktop.commands) and not omitted:
                omitted.append(True)
                desktop.windows[1] = replace(desktop.windows[1], owner=ProcessIdentity(101, 2_000_000, 0))
                return observe()
            return original(**options)
        engine.desktop.observe = observe_replaced_window
        result = engine.apply(self.target(), SESSION)
        self.assertEqual((result.status, result.reason), (Status.SKIPPED, 'owner_changed'))
        self.assertEqual(result.attempted_operations, ('focus', 'enter_fullscreen'))

    def test_missing_normal_destination_is_reported_without_moving(self):
        desktop = FakeYabai(window(display=B))
        desktop.normal_displays = (B,)
        result = self.engine(desktop).apply(self.target(), SESSION)
        self.assertEqual((result.status, result.reason), (Status.FAILED, "no_normal_space"))
        self.assertEqual(desktop.commands, [])

    def test_hidden_minimized_and_unknown_owners_are_not_mutated(self):
        for changes in ({"hidden": True}, {"minimized": True}, {"owner": None}):
            desktop = FakeYabai(window(**changes))
            with self.subTest(changes=changes):
                result = self.engine(desktop).apply(self.target(), SESSION)
                self.assertEqual(result.status, Status.UNSUPPORTED)
                self.assertEqual(desktop.commands, [])

    def test_multiple_stable_samples_are_required(self):
        desktop = FakeYabai(window())
        desktop.effects["focus"] = False
        def fleeting_focus(domain, count):
            if domain == "windows":
                # Exactly one coherent observation reports focus, then it is lost.
                desktop.windows[1] = replace(desktop.windows[1], focused=count in (5, 6))
        desktop.on_query = fleeting_focus
        result = self.engine(desktop).apply(self.target(), SESSION)
        self.assertEqual((result.status, result.reason), (Status.FAILED, "timeout"))
        self.assertFalse(any("--toggle" in command for command in desktop.commands))


def desktop_snapshot(desktop):
    return observe(*desktop.windows.values(), displays=desktop.displays, normal_displays=desktop.normal_displays)
