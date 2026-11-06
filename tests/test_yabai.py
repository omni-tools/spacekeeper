from dataclasses import replace
import json
import unittest

from spacekeeper.identity import IdentityFailure
from spacekeeper.process import ProcessError, ProcessFailure, ProcessResult
from spacekeeper.snapshot import ProcessIdentity
from spacekeeper.yabai import Yabai, YabaiError, YabaiFailure as Failure, resolve_yabai
from tests.fake_yabai import Clock, FakeYabai
from tests.support import A, B, SESSION, window


class AdapterTests(unittest.TestCase):
    def adapter(self, desktop):
        return Yabai(executable="/synthetic/yabai", runner=desktop, identities=desktop)

    def test_observation_normalizes_indexes_and_preserves_raw_metadata(self):
        desktop = FakeYabai(window(fullscreen=True, focused=True))
        desktop.space_indexes[1001] = 9
        observation = self.adapter(desktop).observe()
        self.assertEqual(observation.windows[0].space_id, 1001)
        self.assertEqual(observation.windows[0].identity, window().identity)
        self.assertTrue(observation.windows[0].fullscreen)
        self.assertTrue(observation.metadata["yabai"]["windows"][0]["can-move"])
        self.assertEqual(desktop.commands, [])

    def test_unreadable_owner_is_represented_and_reported(self):
        desktop = FakeYabai(window())
        desktop.denied_owners.add(101)
        observation = self.adapter(desktop).observe()
        self.assertIsNone(observation.windows[0].owner)
        self.assertEqual(observation.metadata["owner_issues"], {"101": IdentityFailure.ACCESS_DENIED.value})

    def test_capture_joins_placement_without_requesting_expensive_window_fields(self):
        desktop = FakeYabai(window(fullscreen=True, display=B), window(2))
        desktop.space_indexes[1001] = 9
        snapshot = self.adapter(desktop).observe()
        self.assertEqual((snapshot.windows[0].display_uuid, snapshot.windows[0].space_id), (B, 1001))
        queries = [args for args in desktop.queries if args[2] == '--windows']
        self.assertEqual(len(queries), 2)
        for args in queries:
            self.assertGreater(len(args), 3)
            self.assertNotIn('display', args[3].split(','))
            self.assertNotIn('space', args[3].split(','))

    def test_capture_uses_targeted_fallback_for_unplaced_windows(self):
        desktop = FakeYabai(window(display_uuid=None, space_id=None))
        snapshot = self.adapter(desktop).observe()
        self.assertIsNone(snapshot.windows[0].space_id)
        self.assertIsNone(snapshot.windows[0].display_uuid)
        targeted = [args for args in desktop.queries if '--window' in args]
        self.assertEqual(len(targeted), 2)
        self.assertTrue(all(args[-1] == '1' for args in targeted))

    def test_capture_preserves_ambiguous_sticky_placement_instead_of_guessing(self):
        desktop = FakeYabai(window(sticky=True))
        def runner(argv, **options):
            result = desktop(argv, **options)
            if argv[3] == '--spaces':
                spaces = json.loads(result.stdout)
                next(s for s in spaces if s['id'] == 102)['windows'].append(1)
                return ProcessResult(0, json.dumps(spaces).encode(), b'')
            return result
        snapshot = Yabai(executable='/synthetic/yabai', runner=runner, identities=desktop).observe()
        self.assertEqual((snapshot.windows[0].space_id, snapshot.windows[0].display_uuid), (101, A))
        self.assertEqual(len([args for args in desktop.queries if '--window' in args]), 2)

    def test_capture_retries_owner_changes_during_placement_fallback(self):
        desktop = FakeYabai(window(display_uuid=None, space_id=None))
        def runner(argv, **options):
            result = desktop(argv, **options)
            if '--window' in argv:
                data = json.loads(result.stdout)
                data['pid'] += 1
                return ProcessResult(0, json.dumps(data).encode(), b'')
            return result
        adapter = Yabai(executable='/synthetic/yabai', runner=runner, identities=desktop)
        with self.assertRaises(YabaiError) as caught:
            adapter.observe()
        self.assertEqual(caught.exception.reason, Failure.DESKTOP_CHANGED)
        self.assertEqual(desktop.query_counts['windows'], 6)

    def test_changing_topology_is_retried_but_bounded(self):
        desktop = FakeYabai(window())
        def churn(domain, count):
            if domain == "spaces":
                desktop.space_indexes[101] = 10 + count
        desktop.on_query = churn
        with self.assertRaises(YabaiError) as caught:
            self.adapter(desktop).observe()
        self.assertEqual(caught.exception.reason, Failure.DESKTOP_CHANGED)
        self.assertEqual(desktop.query_counts["spaces"], 6)
        self.assertEqual(desktop.commands, [])

    def test_owner_replacement_during_capture_forces_a_retry(self):
        desktop = FakeYabai(window())
        reads = []
        def replace_owner(pid):
            reads.append(pid)
            if len(reads) == 2:
                desktop.windows[1] = replace(desktop.windows[1], owner=ProcessIdentity(pid, 2_000_000, 0))
        desktop.on_process = replace_owner
        observation = self.adapter(desktop).observe()
        self.assertEqual(observation.windows[0].owner.start_seconds, 2_000_000)
        self.assertEqual(desktop.query_counts["windows"], 4)

    def test_title_changes_do_not_prevent_an_observation(self):
        desktop = FakeYabai(window())
        def title(domain, count):
            if domain == "windows":
                desktop.windows[1] = replace(desktop.windows[1], title=f"Synthetic document {count}")
        desktop.on_query = title
        self.assertEqual(self.adapter(desktop).observe().windows[0].title, "Synthetic document 2")

    def test_query_rejects_invalid_json_or_shapes(self):
        for response in (b"not JSON", b"{}", b"[1]", b'[{"id":1,"id":2}]', b"\xff"):
            adapter = Yabai(executable="/synthetic/yabai", identities=FakeYabai(),
                            runner=lambda *args, **kwargs: ProcessResult(0, response, b""))
            with self.subTest(response=response), self.assertRaises(YabaiError) as caught:
                adapter.query("windows")
            self.assertEqual(caught.exception.reason, Failure.INVALID_RESPONSE)

    def test_invalid_window_data_is_not_coerced_to_valid_state(self):
        desktop = FakeYabai(window())
        def runner(argv, **options):
            result = desktop(argv, **options)
            if argv[1:4] == ("-m", "query", "--windows"):
                data = json.loads(result.stdout)
                data[0]["is-native-fullscreen"] = "false"
                return ProcessResult(0, json.dumps(data).encode(), b"")
            return result
        adapter = Yabai(executable="/synthetic/yabai", runner=runner, identities=desktop)
        with self.assertRaises(YabaiError) as caught:
            adapter.observe()
        self.assertEqual(caught.exception.reason, Failure.INVALID_RESPONSE)

    def test_process_failures_map_to_adapter_errors(self):
        for reason, expected in ((ProcessFailure.NOT_FOUND, Failure.NOT_FOUND), (ProcessFailure.TIMEOUT, Failure.TIMEOUT),
                                 (ProcessFailure.OUTPUT_LIMIT, Failure.OUTPUT_LIMIT)):
            def runner(*args, **kwargs):
                raise ProcessError(reason)
            adapter = Yabai(executable="/synthetic/yabai", runner=runner, identities=FakeYabai())
            with self.subTest(reason=reason), self.assertRaises(YabaiError) as caught:
                adapter.query("spaces")
            self.assertEqual(caught.exception.reason, expected)
            if reason == ProcessFailure.TIMEOUT:
                self.assertIn('yabai spaces query timed out', str(caught.exception))

    def test_capability_permission_and_service_errors_do_not_expose_raw_output(self):
        for diagnostic, expected in (("cannot connect socket", Failure.SERVICE_UNAVAILABLE),
                                     ("accessibility permission denied", Failure.ACCESS_DENIED),
                                     ("scripting-addition unavailable", Failure.CAPABILITY_UNAVAILABLE),
                                     ("failed", Failure.COMMAND_FAILED)):
            response = (diagnostic + " Synthetic private title").encode()
            adapter = Yabai(executable="/synthetic/yabai", identities=FakeYabai(),
                            runner=lambda *args, **kwargs: ProcessResult(1, b"", response))
            with self.subTest(diagnostic=diagnostic), self.assertRaises(YabaiError) as caught:
                adapter.query("windows")
            self.assertEqual(caught.exception.reason, expected)
            self.assertNotIn("Synthetic private title", str(caught.exception))

    def test_deadline_bounds_queries_and_expires_without_running_a_command(self):
        desktop, clock = FakeYabai(window()), Clock()
        adapter = Yabai(executable="/synthetic/yabai", runner=desktop, identities=desktop, clock=clock)
        adapter.observe(deadline=0.5)
        self.assertTrue(all(timeout <= 0.5 for timeout in desktop.timeouts))
        clock.now = 1
        with self.assertRaises(YabaiError) as caught:
            adapter.query("windows", deadline=0.5)
        self.assertEqual(caught.exception.reason, Failure.TIMEOUT)

    def test_move_resolves_stable_space_id_again_before_using_its_index(self):
        desktop = FakeYabai(window(display=B))
        def change_index(domain, count):
            if domain == "spaces" and count == 3:
                desktop.space_indexes[101] = 77
        desktop.on_query = change_index
        self.adapter(desktop).move_to_display(window().identity, SESSION, A)
        self.assertEqual(desktop.commands[-1], ("-m", "window", "1", "--space", "77"))
        self.assertEqual(desktop.windows[1].display_uuid, A)

    def test_missing_or_replaced_destination_stops_before_move(self):
        desktop = FakeYabai(window(display=B))
        def remove_destination(domain, count):
            if domain == "spaces" and count == 3:
                desktop.normal_displays = (B,)
        desktop.on_query = remove_destination
        with self.assertRaises(YabaiError) as caught:
            self.adapter(desktop).move_to_display(window().identity, SESSION, A)
        self.assertEqual(caught.exception.reason, Failure.DESTINATION_CHANGED)
        self.assertEqual(desktop.commands, [])

    def test_mutation_guards_changed_owner_session_and_unsupported_windows(self):
        variants = (replace(window(), owner=ProcessIdentity(101, 2_000_000, 0)), window(sticky=True))
        for value in variants:
            desktop = FakeYabai(value)
            with self.subTest(value=value), self.assertRaises(YabaiError):
                self.adapter(desktop).focus(window().identity, SESSION)
            self.assertEqual(desktop.commands, [])
        desktop = FakeYabai(window())
        desktop.session_id = replace(SESSION, login_session_id=101)
        with self.assertRaises(YabaiError) as caught:
            self.adapter(desktop).focus(window().identity, SESSION)
        self.assertEqual(caught.exception.reason, Failure.SESSION_CHANGED)
        self.assertEqual(desktop.commands, [])

    def test_fullscreen_command_requires_confirmed_focus_and_monitor(self):
        for focused, display in ((False, A), (True, B)):
            desktop = FakeYabai(window(focused=focused, display=display))
            with self.subTest(focused=focused, display=display), self.assertRaises(YabaiError):
                self.adapter(desktop).fullscreen(window().identity, SESSION, True, A)
            self.assertEqual(desktop.commands, [])

    def test_focus_window_flag_does_not_override_inactive_space(self):
        desktop = FakeYabai(window(focused=True))
        adapter = self.adapter(desktop)
        original = adapter.observe_window
        def inactive_space(*args, **options):
            snapshot = original(*args, **options)
            if not desktop.commands:
                return replace(snapshot, spaces=tuple(replace(s, visible=False, focused=False, visible_window_id=None)
                                                      for s in snapshot.spaces))
            return snapshot
        adapter.observe_window = inactive_space
        adapter.focus(window().identity, SESSION)
        self.assertEqual(desktop.commands, [('-m', 'window', '--focus', '1')])

    def test_custom_executable_must_be_absolute(self):
        for executable in ("yabai", "./yabai", "/synthetic/\0yabai"):
            with self.subTest(executable=executable), self.assertRaises(ValueError):
                resolve_yabai(executable)

    def test_target_observation_checks_only_target_owners_and_ignores_unrelated_membership_churn(self):
        desktop = FakeYabai(window(focused=True), window(2))
        owners = []
        desktop.on_process = owners.append
        def unrelated_window(domain, count):
            if domain == 'spaces':
                desktop.windows[2 + count] = window(2 + count)
        desktop.on_query = unrelated_window
        actual = self.adapter(desktop).observe_window(window().identity, SESSION)
        self.assertEqual(actual.window.identity, window().identity)
        self.assertTrue(actual.focus_ready)
        self.assertEqual(owners, [101, 101])
        self.assertTrue(all('--window' in args for args in desktop.queries if args[2] == '--windows'))

    def test_target_owner_change_between_passes_cannot_authorize_a_mutation(self):
        desktop = FakeYabai(window())
        reads = []
        def replacement(pid):
            reads.append(pid)
            if len(reads) == 2:
                desktop.windows[1] = replace(desktop.windows[1], owner=ProcessIdentity(pid, 2_000_000, 0))
        desktop.on_process = replacement
        with self.assertRaises(YabaiError) as caught:
            self.adapter(desktop).focus(window().identity, SESSION)
        self.assertEqual(caught.exception.reason, Failure.OWNER_CHANGED)
        self.assertEqual(desktop.commands, [])

    def test_target_query_checks_id_and_rejects_malformed_objects(self):
        for response in (b'[]', b'{}', b'{"id":2}', b'{"id":1,"id":1}', b'null'):
            adapter = Yabai(executable='/synthetic/yabai', identities=FakeYabai(),
                            runner=lambda *args, **kwargs: ProcessResult(0, response, b''))
            with self.subTest(response=response), self.assertRaises(YabaiError) as caught:
                adapter.query('windows', window_id=1)
            self.assertEqual(caught.exception.reason, Failure.INVALID_RESPONSE)

    def test_missing_target_query_maps_native_error_without_exposing_diagnostics(self):
        desktop = FakeYabai()
        with self.assertRaises(YabaiError) as caught:
            self.adapter(desktop).observe_window(window().identity, SESSION)
        self.assertEqual(caught.exception.reason, Failure.WINDOW_MISSING)
        self.assertEqual(desktop.commands, [])

    def test_target_observation_rejects_invalid_membership_before_filtering_it(self):
        desktop = FakeYabai(window())
        def runner(argv, **options):
            result = desktop(argv, **options)
            if argv[3] == '--spaces':
                data = json.loads(result.stdout)
                data[0]['windows'] = 'bad membership'
                return ProcessResult(0, json.dumps(data).encode(), b'')
            return result
        adapter = Yabai(executable='/synthetic/yabai', runner=runner, identities=desktop)
        with self.assertRaises(YabaiError) as caught:
            adapter.observe_window(window().identity, SESSION)
        self.assertEqual(caught.exception.reason, Failure.INVALID_RESPONSE)

    def test_unsettled_target_membership_retries_are_bounded_before_mutation(self):
        desktop = FakeYabai(window())
        def runner(argv, **options):
            result = desktop(argv, **options)
            if argv[3] == '--spaces':
                data = json.loads(result.stdout)
                for space in data:
                    space['windows'] = []
                return ProcessResult(0, json.dumps(data).encode(), b'')
            return result
        adapter = Yabai(executable='/synthetic/yabai', runner=runner, identities=desktop)
        with self.assertRaises(YabaiError) as caught:
            adapter.focus(window().identity, SESSION)
        self.assertEqual(caught.exception.reason, Failure.DESKTOP_CHANGED)
        self.assertEqual(desktop.query_counts['spaces'], 3)
        self.assertEqual(desktop.commands, [])

    def test_malformed_target_state_is_still_an_invalid_response(self):
        desktop = FakeYabai(window())
        def runner(argv, **options):
            result = desktop(argv, **options)
            if argv[3] == '--windows':
                data = json.loads(result.stdout)
                data['is-native-fullscreen'] = 'false'
                return ProcessResult(0, json.dumps(data).encode(), b'')
            return result
        adapter = Yabai(executable='/synthetic/yabai', runner=runner, identities=desktop)
        with self.assertRaises(YabaiError) as caught:
            adapter.observe_window(window().identity, SESSION)
        self.assertEqual(caught.exception.reason, Failure.INVALID_RESPONSE)
