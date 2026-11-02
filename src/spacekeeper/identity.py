"""Read-only macOS identity collection, with an injectable native boundary.

No identities are cached: a PID can be reused between observations. Callers
must collect/check ownership around window observations and again before use.
"""

import ctypes
from dataclasses import dataclass
from enum import StrEnum
import errno
import os
import sys
from typing import Protocol

from spacekeeper.snapshot import ProcessIdentity, SessionIdentity, SnapshotError


class IdentityFailure(StrEnum):
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    NOT_GRAPHICAL = "not_graphical"
    PROCESS_MISSING = "process_missing"
    ACCESS_DENIED = "access_denied"
    UNAVAILABLE = "unavailable"
    INVALID_DATA = "invalid_data"


class IdentityUnavailable(RuntimeError):
    def __init__(self, reason: IdentityFailure, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class SecuritySession:
    id: int
    graphical: bool


class IdentityBackend(Protocol):
    def boot_uuid(self) -> str: ...
    def security_session(self) -> SecuritySession: ...
    def uid(self) -> int: ...
    def process(self, pid: int) -> ProcessIdentity: ...


class IdentityReader:
    """Collect the caller's graphical session and individual process identities.

    Failures carry stable reasons. In particular, an unreadable owner must never
    be replaced by a PID-only identity or a guessed process creation time.
    """

    def __init__(self, backend: IdentityBackend | None = None):
        self._backend = backend if backend is not None else _DarwinBackend()

    def session(self) -> SessionIdentity:
        security = self._backend.security_session()
        if not security.graphical:
            raise IdentityUnavailable(IdentityFailure.NOT_GRAPHICAL, "The caller is not in a graphical login session.")
        try:
            return SessionIdentity(self._backend.boot_uuid(), self._backend.uid(), security.id)
        except SnapshotError as error:
            raise IdentityUnavailable(IdentityFailure.INVALID_DATA, "macOS returned invalid session identity data.") from error

    def process(self, pid: int) -> ProcessIdentity:
        if type(pid) is not int or not 0 < pid <= 0x7FFFFFFF:
            raise ValueError("pid must be a positive macOS process ID.")
        identity = self._backend.process(pid)
        if not isinstance(identity, ProcessIdentity) or identity.pid != pid:
            raise IdentityUnavailable(IdentityFailure.INVALID_DATA, "macOS returned identity for a different process.")
        return identity


class _ProcBsdInfo(ctypes.Structure):
    # Public layout: <sys/proc_info.h>, struct proc_bsdinfo. MAXCOMLEN is 16.
    _fields_ = [
        (name, ctypes.c_uint32) for name in (
            "pbi_flags", "pbi_status", "pbi_xstatus", "pbi_pid", "pbi_ppid",
            "pbi_uid", "pbi_gid", "pbi_ruid", "pbi_rgid", "pbi_svuid", "pbi_svgid", "rfu_1",
        )
    ] + [
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
    ] + [
        (name, ctypes.c_uint32) for name in ("pbi_nfiles", "pbi_pgid", "pbi_pjobc", "e_tdev", "e_tpgid")
    ] + [
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class _DarwinBackend:
    def __init__(self):
        if sys.platform != "darwin":
            raise IdentityUnavailable(IdentityFailure.UNSUPPORTED_PLATFORM, "Identity collection requires macOS.")
        try:
            system = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            self._sysctl = system.sysctlbyname
            self._sysctl.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                                     ctypes.c_void_p, ctypes.c_size_t]
            self._sysctl.restype = ctypes.c_int
            self._session = security.SessionGetInfo
            self._session.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
            self._session.restype = ctypes.c_int32
            self._process = libproc.proc_pidinfo
            self._process.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
            self._process.restype = ctypes.c_int
        except (OSError, AttributeError) as error:
            raise IdentityUnavailable(IdentityFailure.UNAVAILABLE, "macOS identity APIs are unavailable.") from error

    def uid(self) -> int:
        return os.geteuid()

    def boot_uuid(self) -> str:
        # Fixed bound; sysctlbyname never writes beyond the supplied length.
        buffer = ctypes.create_string_buffer(128)
        length = ctypes.c_size_t(len(buffer))
        ctypes.set_errno(0)
        if self._sysctl(b"kern.bootsessionuuid", buffer, ctypes.byref(length), None, 0) != 0:
            self._raise_errno("Boot identity is unavailable.")
        if not 0 < length.value <= len(buffer):
            raise IdentityUnavailable(IdentityFailure.INVALID_DATA, "macOS returned invalid boot identity data.")
        try:
            return buffer.value.decode("ascii")
        except UnicodeDecodeError as error:
            raise IdentityUnavailable(IdentityFailure.INVALID_DATA, "macOS returned invalid boot identity data.") from error

    def security_session(self) -> SecuritySession:
        session_id, attributes = ctypes.c_uint32(), ctypes.c_uint32()
        status = self._session(0xFFFFFFFF, ctypes.byref(session_id), ctypes.byref(attributes))
        if status != 0:
            reason = IdentityFailure.ACCESS_DENIED if status == -60502 else IdentityFailure.UNAVAILABLE
            raise IdentityUnavailable(reason, "Graphical session identity is unavailable.")
        return SecuritySession(session_id.value, bool(attributes.value & 0x0010))

    def process(self, pid: int) -> ProcessIdentity:
        info = _ProcBsdInfo()
        ctypes.set_errno(0)
        received = self._process(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if received <= 0:
            self._raise_errno("Process identity is unavailable.")
        if received != ctypes.sizeof(info) or info.pbi_pid != pid:
            raise IdentityUnavailable(IdentityFailure.INVALID_DATA, "macOS returned incomplete process identity data.")
        try:
            return ProcessIdentity(pid, info.pbi_start_tvsec, info.pbi_start_tvusec)
        except SnapshotError as error:
            raise IdentityUnavailable(IdentityFailure.INVALID_DATA, "macOS returned invalid process creation data.") from error

    @staticmethod
    def _raise_errno(message: str) -> None:
        code = ctypes.get_errno()
        if code in (errno.EPERM, errno.EACCES):
            reason = IdentityFailure.ACCESS_DENIED
        elif code == errno.ESRCH:
            reason = IdentityFailure.PROCESS_MISSING
        else:
            reason = IdentityFailure.UNAVAILABLE
        raise IdentityUnavailable(reason, message)
