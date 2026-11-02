import ctypes
import errno
import unittest
from unittest.mock import patch

from spacekeeper.identity import (
    IdentityFailure as Failure, IdentityReader, IdentityUnavailable, SecuritySession,
    _DarwinBackend, _ProcBsdInfo,
)
from spacekeeper.snapshot import ProcessIdentity
from tests.support import SESSION


class FakeBackend:
    def __init__(self):
        self.graphical = True
        self.identity = ProcessIdentity(100, 1_000_000, 123456)

    def boot_uuid(self):
        return SESSION.boot_uuid

    def security_session(self):
        return SecuritySession(SESSION.login_session_id, self.graphical)

    def uid(self):
        return SESSION.uid

    def process(self, pid):
        return self.identity


class IdentityTests(unittest.TestCase):
    def test_collects_session_and_microsecond_process_identity(self):
        backend = FakeBackend()
        reader = IdentityReader(backend)
        self.assertEqual(reader.session(), SESSION)
        self.assertEqual(reader.process(100), backend.identity)

    def test_process_identity_is_not_cached(self):
        backend = FakeBackend()
        reader = IdentityReader(backend)
        first = reader.process(100)
        backend.identity = ProcessIdentity(100, first.start_seconds, first.start_microseconds + 1)
        self.assertNotEqual(first, reader.process(100))

    def test_session_identity_is_not_cached(self):
        backend = FakeBackend()
        reader = IdentityReader(backend)
        first = reader.session()
        backend.security_session = lambda: SecuritySession(first.login_session_id + 1, True)
        self.assertNotEqual(first, reader.session())

    def test_nongraphical_caller_is_explicitly_unsupported(self):
        backend = FakeBackend()
        backend.graphical = False
        with self.assertRaises(IdentityUnavailable) as caught:
            IdentityReader(backend).session()
        self.assertEqual(caught.exception.reason, Failure.NOT_GRAPHICAL)

    def test_bad_session_data_never_becomes_a_trusted_identity(self):
        backend = FakeBackend()
        backend.boot_uuid = lambda: "invalid"
        with self.assertRaises(IdentityUnavailable) as caught:
            IdentityReader(backend).session()
        self.assertEqual(caught.exception.reason, Failure.INVALID_DATA)

    def test_invalid_pid_is_rejected_before_native_call(self):
        for pid in (0, -1, True, "100", 2**31):
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                IdentityReader(FakeBackend()).process(pid)

    def test_owner_mismatch_is_not_trusted(self):
        with self.assertRaises(IdentityUnavailable) as caught:
            IdentityReader(FakeBackend()).process(999)
        self.assertEqual(caught.exception.reason, Failure.INVALID_DATA)

    def test_platform_and_library_failures_are_explicit(self):
        with patch("spacekeeper.identity.sys.platform", "linux"):
            with self.assertRaises(IdentityUnavailable) as caught:
                IdentityReader()
            self.assertEqual(caught.exception.reason, Failure.UNSUPPORTED_PLATFORM)
        with patch("spacekeeper.identity.sys.platform", "darwin"), patch("ctypes.CDLL", side_effect=OSError):
            with self.assertRaises(IdentityUnavailable) as caught:
                IdentityReader()
            self.assertEqual(caught.exception.reason, Failure.UNAVAILABLE)


class NativeBoundaryTests(unittest.TestCase):
    def setUp(self):
        # Exercise the actual buffer decoding/error handling without system calls.
        self.backend = _DarwinBackend.__new__(_DarwinBackend)

    def test_bsdinfo_public_layout(self):
        self.assertEqual(ctypes.sizeof(_ProcBsdInfo), 136)
        self.assertEqual(_ProcBsdInfo.pbi_start_tvsec.offset, 120)
        self.assertEqual(_ProcBsdInfo.pbi_start_tvusec.offset, 128)

    def test_decodes_native_process_data(self):
        def native(pid, flavor, arg, buffer, size):
            self.assertEqual((pid, flavor, arg, size), (100, 3, 0, 136))
            info = ctypes.cast(buffer, ctypes.POINTER(_ProcBsdInfo)).contents
            info.pbi_pid, info.pbi_start_tvsec, info.pbi_start_tvusec = 100, 1_000_000, 123456
            return size
        self.backend._process = native
        self.assertEqual(self.backend.process(100), ProcessIdentity(100, 1_000_000, 123456))

    def test_partial_wrong_owner_or_invalid_native_process_data_is_rejected(self):
        for received, pid, seconds, micros in ((8, 100, 1, 0), (136, 99, 1, 0),
                                               (136, 100, 0, 0), (136, 100, 1, 1_000_000)):
            def native(request, flavor, arg, buffer, size):
                info = ctypes.cast(buffer, ctypes.POINTER(_ProcBsdInfo)).contents
                info.pbi_pid, info.pbi_start_tvsec, info.pbi_start_tvusec = pid, seconds, micros
                return received
            self.backend._process = native
            with self.subTest(received=received, pid=pid, seconds=seconds, micros=micros):
                with self.assertRaises(IdentityUnavailable) as caught:
                    self.backend.process(100)
                self.assertEqual(caught.exception.reason, Failure.INVALID_DATA)

    def test_missing_denied_and_unspecified_process_errors(self):
        for code, reason in ((errno.ESRCH, Failure.PROCESS_MISSING), (errno.EPERM, Failure.ACCESS_DENIED),
                             (errno.EACCES, Failure.ACCESS_DENIED), (0, Failure.UNAVAILABLE)):
            def native(*args):
                ctypes.set_errno(code)
                return 0
            self.backend._process = native
            with self.subTest(code=code), self.assertRaises(IdentityUnavailable) as caught:
                self.backend.process(100)
            self.assertEqual(caught.exception.reason, reason)

    def test_boot_query_has_a_fixed_buffer_and_read_only_arguments(self):
        def native(name, buffer, length, new, new_size):
            self.assertEqual(name, b"kern.bootsessionuuid")
            self.assertEqual((new, new_size), (None, 0))
            self.assertEqual(ctypes.cast(length, ctypes.POINTER(ctypes.c_size_t))[0], 128)
            value = SESSION.boot_uuid.encode("ascii") + b"\0"
            ctypes.memmove(buffer, value, len(value))
            ctypes.cast(length, ctypes.POINTER(ctypes.c_size_t))[0] = len(value)
            return 0
        self.backend._sysctl = native
        self.assertEqual(self.backend.boot_uuid(), SESSION.boot_uuid)

    def test_boot_access_failure_and_invalid_length(self):
        def denied(*args):
            ctypes.set_errno(errno.EPERM)
            return -1
        self.backend._sysctl = denied
        with self.assertRaises(IdentityUnavailable) as caught:
            self.backend.boot_uuid()
        self.assertEqual(caught.exception.reason, Failure.ACCESS_DENIED)
        def oversized(name, buffer, length, new, new_size):
            ctypes.cast(length, ctypes.POINTER(ctypes.c_size_t))[0] = 129
            return 0
        self.backend._sysctl = oversized
        with self.assertRaises(IdentityUnavailable) as caught:
            self.backend.boot_uuid()
        self.assertEqual(caught.exception.reason, Failure.INVALID_DATA)

    def test_security_session_attributes_and_failure(self):
        def native(request, session, attributes):
            self.assertEqual(request, 0xFFFFFFFF)
            ctypes.cast(session, ctypes.POINTER(ctypes.c_uint32))[0] = 100
            ctypes.cast(attributes, ctypes.POINTER(ctypes.c_uint32))[0] = 0x0010
            return 0
        self.backend._session = native
        self.assertEqual(self.backend.security_session(), SecuritySession(100, True))
        self.backend._session = lambda *args: -60502
        with self.assertRaises(IdentityUnavailable) as caught:
            self.backend.security_session()
        self.assertEqual(caught.exception.reason, Failure.ACCESS_DENIED)
