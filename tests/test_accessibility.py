from types import SimpleNamespace
import unittest

from spacekeeper.accessibility import AccessibilityError, FullscreenSetter
from tests.fake_yabai import Clock
from tests.support import window


class AccessibilityTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.released, self.writes, self.timeouts = [], [], []
        self.ids = {201: 99, 202: 1}
        self.trusted, self.array_type, self.status = True, 7, 0
        self.on_enumerate = lambda: None
        def copied(app, key, output):
            output._obj.value = 301
            return 0
        def native_id(element, output):
            output._obj.value = self.ids[element]
            self.on_enumerate()
            return 0
        def timeout(element, duration):
            self.timeouts.append(duration)
            return 0
        def write(element, key, value):
            self.writes.append((element, key, value))
            return self.status
        setter = FullscreenSetter.__new__(FullscreenSetter)
        setter.clock, setter.true, setter.false = self.clock, 1001, 1002
        setter.ax = SimpleNamespace(AXIsProcessTrusted=lambda: self.trusted,
                                    AXUIElementCreateApplication=lambda pid: 101,
                                    AXUIElementCopyAttributeValue=copied, _AXUIElementGetWindow=native_id,
                                    AXUIElementSetMessagingTimeout=timeout, AXUIElementSetAttributeValue=write)
        setter.cf = SimpleNamespace(CFStringCreateWithCString=lambda _, name, encoding: 401 if name == b'AXWindows' else 402,
                                    CFGetTypeID=lambda _: self.array_type, CFArrayGetTypeID=lambda: 7,
                                    CFArrayGetCount=lambda _: len(self.ids),
                                    CFArrayGetValueAtIndex=lambda _, index: list(self.ids)[index],
                                    CFRelease=lambda ref: self.released.append(getattr(ref, 'value', ref)))
        self.setter = setter

    def test_sets_requested_boolean_on_exact_native_window_and_releases_references(self):
        guards = []
        for desired, expected in ((True, 1001), (False, 1002), (False, 1002)):
            self.setter.set_fullscreen(window().identity, desired, timeout=1, guard=lambda: guards.append(True))
            self.assertEqual(self.writes[-1], (202, 402, expected))
        self.assertEqual(len(guards), 3)
        self.assertEqual(self.released, [301, 402, 401, 101] * 3)
        self.assertTrue(all(0 < value <= 1 for value in self.timeouts))

    def test_owner_check_after_enumeration_can_stop_the_write(self):
        def changed_owner():
            raise RuntimeError('owner changed')
        with self.assertRaisesRegex(RuntimeError, 'owner changed'):
            self.setter.set_fullscreen(window().identity, True, timeout=1, guard=changed_owner)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.released, [301, 402, 401, 101])

    def test_missing_native_id_never_falls_back_to_title_or_window_order(self):
        self.ids[202] = 88
        with self.assertRaises(AccessibilityError) as error:
            self.setter.set_fullscreen(window().identity, True, timeout=1, guard=lambda: None)
        self.assertEqual(error.exception.reason, 'window_missing')
        self.assertEqual(error.exception.stage, 'fullscreen.ax_window_lookup')
        self.assertEqual(self.writes, [])

    def test_permission_failure_does_not_allocate_or_write(self):
        self.trusted = False
        with self.assertRaises(AccessibilityError) as error:
            self.setter.set_fullscreen(window().identity, True, timeout=1, guard=lambda: None)
        self.assertEqual(error.exception.reason, 'access_denied')
        self.assertEqual(error.exception.stage, 'fullscreen.permission')
        self.assertEqual(self.writes, [])
        self.assertEqual(self.released, [])

    def test_timeout_during_lookup_stops_before_mutation_and_releases_references(self):
        self.on_enumerate = lambda: self.clock.sleep(2)
        with self.assertRaises(AccessibilityError) as error:
            self.setter.set_fullscreen(window().identity, True, timeout=1, guard=lambda: None)
        self.assertEqual(error.exception.reason, 'timeout')
        self.assertEqual(error.exception.stage, 'fullscreen.ax_window_lookup')
        self.assertEqual(self.writes, [])
        self.assertEqual(self.released, [301, 402, 401, 101])

    def test_bad_window_array_and_native_setter_error_are_reported(self):
        self.array_type = 99
        with self.assertRaises(AccessibilityError) as error:
            self.setter.set_fullscreen(window().identity, True, timeout=1, guard=lambda: None)
        self.assertEqual(error.exception.reason, 'invalid_response')
        self.assertEqual(error.exception.stage, 'fullscreen.ax_windows')
        self.assertEqual(self.writes, [])
        self.array_type, self.status = 7, -25211
        with self.assertRaises(AccessibilityError) as error:
            self.setter.set_fullscreen(window().identity, False, timeout=1, guard=lambda: None)
        self.assertEqual(error.exception.reason, 'access_denied')
        self.assertEqual(error.exception.stage, 'fullscreen.ax_set_attribute')

    def test_rejects_non_boolean_and_invalid_timeout(self):
        for desired, timeout in ((1, 1), (True, 0), (False, float('inf'))):
            with self.subTest(desired=desired, timeout=timeout), self.assertRaises(ValueError):
                self.setter.set_fullscreen(window().identity, desired, timeout=timeout, guard=lambda: None)
        self.assertEqual(self.writes, [])
