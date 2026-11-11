"""Explicit AXFullScreen requests, addressed by PID and native window ID.

No Accessibility references or process identities survive a request. Loading
this module is portable; native libraries are loaded only for an actual edit.
"""

import ctypes
import math
import sys
import time
from typing import Callable

from spacekeeper.snapshot import WindowIdentity


class AccessibilityError(RuntimeError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class FullscreenSetter:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        if sys.platform != 'darwin':
            raise AccessibilityError('capability_unavailable', 'Fullscreen changes require macOS Accessibility.')
        try:
            self.ax = ctypes.CDLL('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
            self.cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
            declarations = (
                (self.ax, 'AXIsProcessTrusted', [], ctypes.c_bool),
                (self.ax, 'AXUIElementCreateApplication', [ctypes.c_int], ctypes.c_void_p),
                (self.ax, 'AXUIElementCopyAttributeValue',
                 [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int),
                (self.ax, 'AXUIElementSetAttributeValue', [ctypes.c_void_p] * 3, ctypes.c_int),
                (self.ax, 'AXUIElementSetMessagingTimeout', [ctypes.c_void_p, ctypes.c_float], ctypes.c_int),
                # Also used by yabai/Hammerspoon to bind an AX reference to a CGWindowID.
                (self.ax, '_AXUIElementGetWindow', [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)], ctypes.c_int),
                (self.cf, 'CFStringCreateWithCString', [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32], ctypes.c_void_p),
                (self.cf, 'CFArrayGetCount', [ctypes.c_void_p], ctypes.c_long),
                (self.cf, 'CFArrayGetValueAtIndex', [ctypes.c_void_p, ctypes.c_long], ctypes.c_void_p),
                (self.cf, 'CFGetTypeID', [ctypes.c_void_p], ctypes.c_ulong),
                (self.cf, 'CFArrayGetTypeID', [], ctypes.c_ulong),
                (self.cf, 'CFRelease', [ctypes.c_void_p], None),
            )
            for library, name, arguments, result in declarations:
                function = getattr(library, name)
                function.argtypes, function.restype = arguments, result
            self.true = ctypes.c_void_p.in_dll(self.cf, 'kCFBooleanTrue').value
            self.false = ctypes.c_void_p.in_dll(self.cf, 'kCFBooleanFalse').value
        except (OSError, AttributeError, ValueError) as error:
            raise AccessibilityError('capability_unavailable', 'macOS fullscreen APIs are unavailable.') from error

    @staticmethod
    def _check(status: int) -> None:
        if status == 0:
            return
        reason = {-25202: 'window_missing', -25204: 'timeout', -25205: 'unsupported_window',
                  -25208: 'unsupported_window', -25211: 'access_denied'}.get(status, 'command_failed')
        raise AccessibilityError(reason, f'Fullscreen request failed: {reason}.')

    def set_fullscreen(self, identity: WindowIdentity, desired: bool, *, timeout: float,
                       guard: Callable[[], None]) -> None:
        if type(desired) is not bool or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Fullscreen state must be boolean and timeout positive and finite.')
        if not self.ax.AXIsProcessTrusted():
            raise AccessibilityError('access_denied', 'Accessibility permission is required for fullscreen changes.')
        deadline = self.clock() + timeout
        def remaining():
            value = deadline - self.clock()
            if value <= 0:
                raise AccessibilityError('timeout', 'Fullscreen request deadline expired.')
            return value
        owned = []
        windows = ctypes.c_void_p()
        try:
            app = self.ax.AXUIElementCreateApplication(identity.owner.pid)
            if not app:
                raise AccessibilityError('window_missing', 'The window application is unavailable.')
            owned.append(app)
            keys = []
            for name in (b'AXWindows', b'AXFullScreen'):
                key = self.cf.CFStringCreateWithCString(None, name, 0x08000100)
                if not key:
                    raise AccessibilityError('capability_unavailable', 'Cannot allocate Accessibility attributes.')
                owned.append(key)
                keys.append(key)
            self._check(self.ax.AXUIElementSetMessagingTimeout(app, remaining()))
            self._check(self.ax.AXUIElementCopyAttributeValue(app, keys[0], ctypes.byref(windows)))
            if not windows.value:
                raise AccessibilityError('window_missing', 'The application has no accessible windows.')
            if self.cf.CFGetTypeID(windows) != self.cf.CFArrayGetTypeID():
                raise AccessibilityError('invalid_response', 'Invalid Accessibility window list.')
            for index in range(self.cf.CFArrayGetCount(windows)):
                remaining()
                element = self.cf.CFArrayGetValueAtIndex(windows, index)
                self._check(self.ax.AXUIElementSetMessagingTimeout(element, remaining()))
                window_id = ctypes.c_uint32()
                if self.ax._AXUIElementGetWindow(element, ctypes.byref(window_id)) != 0:
                    continue
                if window_id.value != identity.window_id:
                    continue
                # Enumeration may have blocked. Re-check the boot/login and
                # process start identity immediately before the actual setter.
                guard()
                self._check(self.ax.AXUIElementSetMessagingTimeout(element, remaining()))
                self._check(self.ax.AXUIElementSetAttributeValue(element, keys[1], self.true if desired else self.false))
                return
            raise AccessibilityError('window_missing', 'No accessible window matches the requested native window ID.')
        finally:
            if windows.value:
                self.cf.CFRelease(windows)
            for reference in reversed(owned):
                self.cf.CFRelease(reference)
