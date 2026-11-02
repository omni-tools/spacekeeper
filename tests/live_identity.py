"""Opt-in native checks: python -m unittest tests.live_identity -v.

Reads session identity and inspects this process plus a short-lived child.
Never queries yabai, records identity values, or modifies desktop state.
"""

import os
import subprocess
import sys
import time
import unittest

from spacekeeper.identity import IdentityFailure, IdentityReader, IdentityUnavailable


@unittest.skipUnless(sys.platform == "darwin", "Requires macOS")
class LiveIdentityTests(unittest.TestCase):
    def test_session_and_self_process_are_stable(self):
        reader = IdentityReader()
        session = reader.session()
        self.assertTrue(session == reader.session(), "Session identity changed between reads.")
        self.assertEqual(session.uid, os.geteuid())
        identity = reader.process(os.getpid())
        self.assertTrue(identity == reader.process(os.getpid()), "Process identity changed between reads.")
        self.assertTrue(0 < identity.start_seconds <= time.time())

    def test_new_process_creation_and_disappearance(self):
        reader = IdentityReader()
        started = time.time()
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            identity = reader.process(child.pid)
            timestamp = identity.start_seconds + identity.start_microseconds / 1_000_000
            self.assertTrue(started <= timestamp <= time.time(), "Unexpected child creation timestamp.")
            self.assertTrue(identity == reader.process(child.pid), "Child identity changed between reads.")
            child.stdin.close()
            child.wait(timeout=5)
            try:
                later = reader.process(child.pid)
            except IdentityUnavailable as error:
                self.assertEqual(error.reason, IdentityFailure.PROCESS_MISSING)
            else:
                self.assertTrue(later != identity, "Exited process still has its original identity.")
        finally:
            if child.stdin is not None and not child.stdin.closed:
                child.stdin.close()
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
