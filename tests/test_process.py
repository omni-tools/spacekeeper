import sys
import time
import unittest

from spacekeeper.process import ProcessError, ProcessFailure, SubprocessRunner


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.run = SubprocessRunner()

    def test_arguments_are_literal_and_both_streams_are_returned(self):
        literal = "$(this-is-not-a-command); a title with spaces"
        result = self.run((sys.executable, "-c", "import sys; print(sys.argv[1]); print('error', file=sys.stderr)", literal),
                          timeout=3, output_limit=4096)
        self.assertEqual(result.stdout.decode().strip(), literal)
        self.assertEqual(result.stderr, b"error\n")
        self.assertEqual(result.returncode, 0)

    def test_nonzero_exit_is_returned_without_claiming_success(self):
        result = self.run((sys.executable, "-c", "raise SystemExit(7)"), timeout=3, output_limit=4096)
        self.assertEqual(result.returncode, 7)

    def test_output_limit_is_enforced_while_the_process_is_running(self):
        started = time.monotonic()
        with self.assertRaises(ProcessError) as caught:
            self.run((sys.executable, "-c", "import os,time; os.write(1,b'x'*4096); time.sleep(30)"),
                     timeout=3, output_limit=100)
        self.assertEqual(caught.exception.reason, ProcessFailure.OUTPUT_LIMIT)
        self.assertLess(time.monotonic() - started, 3)

    def test_limit_applies_to_stdout_and_stderr_combined(self):
        with self.assertRaises(ProcessError) as caught:
            self.run((sys.executable, "-c", "import os; os.write(1,b'x'*70); os.write(2,b'y'*70)"),
                     timeout=3, output_limit=100)
        self.assertEqual(caught.exception.reason, ProcessFailure.OUTPUT_LIMIT)
        exact = self.run((sys.executable, "-c", "import os; os.write(1,b'x'*100)"), timeout=3, output_limit=100)
        self.assertEqual(len(exact.stdout), 100)

    def test_timeout_cleans_up_a_process_that_keeps_its_pipes_open(self):
        started = time.monotonic()
        with self.assertRaises(ProcessError) as caught:
            self.run((sys.executable, "-c", "import time; time.sleep(30)"), timeout=0.1, output_limit=100)
        self.assertEqual(caught.exception.reason, ProcessFailure.TIMEOUT)
        self.assertLess(time.monotonic() - started, 3)

    def test_timeout_also_applies_after_the_process_closes_its_streams(self):
        with self.assertRaises(ProcessError) as caught:
            self.run((sys.executable, "-c", "import os,time; os.close(1); os.close(2); time.sleep(30)"),
                     timeout=0.1, output_limit=100)
        self.assertEqual(caught.exception.reason, ProcessFailure.TIMEOUT)

    def test_missing_executable_and_invalid_options(self):
        with self.assertRaises(ProcessError) as caught:
            self.run(("/synthetic/nonexistent/spacekeeper-test",), timeout=1, output_limit=100)
        self.assertEqual(caught.exception.reason, ProcessFailure.NOT_FOUND)
        for timeout, limit in ((0, 1), (float("nan"), 1), (1, 0), (1, True)):
            with self.subTest(timeout=timeout, limit=limit), self.assertRaises(ValueError):
                self.run((sys.executable,), timeout=timeout, output_limit=limit)
