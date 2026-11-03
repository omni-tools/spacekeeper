"""Shell-free subprocess execution with a deadline and combined output bound."""

from dataclasses import dataclass
from enum import StrEnum
import math
import os
import selectors
import signal
import subprocess
import time
from typing import Protocol


class ProcessFailure(StrEnum):
    NOT_FOUND = "not_found"
    START_FAILED = "start_failed"
    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output_limit"


class ProcessError(RuntimeError):
    def __init__(self, reason: ProcessFailure):
        super().__init__(f"Command execution failed: {reason}.")
        self.reason = reason


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandRunner(Protocol):
    def __call__(self, argv: tuple[str, ...], *, timeout: float, output_limit: int) -> ProcessResult: ...


class SubprocessRunner:
    def __call__(self, argv: tuple[str, ...], *, timeout: float, output_limit: int) -> ProcessResult:
        if not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv):
            raise ValueError("argv must contain nonempty shell-free command arguments.")
        if not math.isfinite(timeout) or timeout <= 0 or type(output_limit) is not int or output_limit < 1:
            raise ValueError("A positive timeout and output limit are required.")
        deadline = time.monotonic() + timeout
        try:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, start_new_session=True, shell=False)
        except FileNotFoundError as error:
            raise ProcessError(ProcessFailure.NOT_FOUND) from error
        except OSError as error:
            raise ProcessError(ProcessFailure.START_FAILED) from error

        output = {"stdout": bytearray(), "stderr": bytearray()}
        size = 0
        try:
            with selectors.DefaultSelector() as selector:
                for stream, name in ((child.stdout, "stdout"), (child.stderr, "stderr")):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProcessError(ProcessFailure.TIMEOUT)
                    for key, _ in selector.select(remaining):
                        try:
                            chunk = os.read(key.fileobj.fileno(), min(65536, output_limit - size + 1))
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        size += len(chunk)
                        if size > output_limit:
                            raise ProcessError(ProcessFailure.OUTPUT_LIMIT)
                        output[key.data].extend(chunk)
            try:
                returncode = child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                raise ProcessError(ProcessFailure.TIMEOUT) from error
            return ProcessResult(returncode, bytes(output["stdout"]), bytes(output["stderr"]))
        finally:
            # Also stop descendants retaining our pipes after the command exits.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            child.stdout.close()
            child.stderr.close()
