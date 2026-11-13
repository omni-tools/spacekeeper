"""Progress follows real synthetic restore work without additional observations."""

from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import click

from spacekeeper.restore import RestoreCoordinator, RestoreProgress
from spacekeeper.storage import SnapshotStore
from spacekeeper.terminal_progress import progress_text, restore_spinner
from spacekeeper.yabai import Yabai, YabaiError, YabaiFailure
from tests.fake_yabai import Clock, FakeYabai
from tests.support import B, observe, window
from tests.test_restore_recovery import LookupGap


class ProgressTests(unittest.TestCase):
    def restore(self, fake, saved, callback=None, **options):
        events, clock = [], Clock()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = Yabai(executable='/synthetic/yabai', runner=fake, identities=fake,
                            fullscreen_setter=fake, clock=clock)
            coordinator = RestoreCoordinator(adapter, SnapshotStore(root/'state'), lock_path=root/'lock/restore',
                                              timeout=1, poll_interval=.1, clock=clock, sleep=clock.sleep,
                                              on_progress=None if callback is False else callback or events.append)
            report = coordinator.restore(saved, **options)
            self.assertTrue(Path(report.report_path).is_file())
        return report, events

    def test_counts_unique_targets_and_changed_states_including_noops(self):
        fake = FakeYabai(window(1, focused=True), window(2), window(3), window(4, minimized=True))
        saved = observe(window(1, fullscreen=True, focused=True), window(2, minimized=True),
                        window(3), window(4))
        report, events = self.restore(fake, saved)
        self.assertTrue(report.complete, report)
        self.assertEqual([e.phase for e in events[:3]], ['Observing', 'Planning', 'Restoring'])
        self.assertIsNone(events[0].windows_total)
        final = events[-1]
        self.assertEqual(final, RestoreProgress('Complete', 4, 4, 1, 1, 2, 2, 0))
        restoring = [e for e in events if e.phase == 'Restoring']
        self.assertEqual([e.windows_done for e in restoring], [0, 1, 2, 3, 4])
        self.assertIn('Verifying', [e.phase for e in events])

    def test_monitor_round_trips_are_counted_once(self):
        fake = FakeYabai(window(1, fullscreen=True, focused=True, display=B),
                         window(2, minimized=True, display=B))
        report, events = self.restore(fake, observe(window(1, fullscreen=True, focused=True),
                                                    window(2, minimized=True)))
        self.assertTrue(report.complete, report)
        self.assertEqual(events[-1], RestoreProgress('Complete', 2, 2, 1, 1, 1, 1, 0))

    def test_state_filters_exclude_untouched_targets_and_extra_windows(self):
        fake = FakeYabai(window(1, focused=True), window(2), window(3, fullscreen=True))
        report, events = self.restore(fake, observe(window(1, fullscreen=True, focused=True),
                                                    window(2, minimized=True)), only_fullscreen=True)
        self.assertTrue(report.complete, report)
        self.assertEqual(events[-1], RestoreProgress('Complete', 1, 1, 1, 1, 0, 0, 0))

    def test_extra_fullscreen_cleanup_is_included(self):
        report, events = self.restore(FakeYabai(window(1, focused=True), window(2, fullscreen=True)),
                                      observe(window(1, focused=True)))
        self.assertTrue(report.complete, report)
        self.assertEqual(events[-1], RestoreProgress('Complete', 2, 2, 1, 1, 0, 0, 0))

    def test_missing_source_counts_as_warning_not_processable_target(self):
        fake = FakeYabai(window(1, focused=True))
        report, events = self.restore(fake, observe(window(1, focused=True), window(2)))
        self.assertFalse(report.complete)
        self.assertEqual(events[-1].windows_total, 1)
        self.assertEqual(events[-1].errors, 1)

    def test_recovery_clears_warning_without_increasing_completed_counts(self):
        fake = LookupGap(window(1, focused=True), window(2))
        report, events = self.restore(fake, observe(window(1, fullscreen=True, focused=True),
                                                    window(2, fullscreen=True)))
        self.assertTrue(report.complete, report)
        self.assertEqual(fake.lookups, {1: 2, 2: 2})
        self.assertEqual(max(e.errors for e in events), 2)
        self.assertEqual(events[-1].errors, 0)
        self.assertTrue(all(e.windows_done <= 2 and e.fullscreen_done <= 2 for e in events))

    def test_pending_edits_are_not_errors(self):
        report, events = self.restore(FakeYabai(window(focused=True)),
                                      observe(window(fullscreen=True, focused=True)))
        self.assertTrue(report.complete)
        self.assertTrue(all(e.errors == 0 for e in events))

    def test_global_verification_failure_is_visible(self):
        fake = FakeYabai(window(focused=True))
        with patch.object(RestoreCoordinator, '_verify', side_effect=YabaiError(
                YabaiFailure.ACCESS_DENIED, 'Synthetic permission failure')):
            # Planned transition avoids the pre-edit no-change verification.
            report, events = self.restore(fake, observe(window(fullscreen=True, focused=True)))
        self.assertFalse(report.complete)
        self.assertGreater(events[-1].errors, 0)

    def test_broken_callback_cannot_interrupt_restore_or_report(self):
        calls = []
        def broken(event):
            calls.append(event)
            raise OSError('Synthetic closed terminal')
        report, _ = self.restore(FakeYabai(window(focused=True)),
                                 observe(window(fullscreen=True, focused=True)), callback=broken)
        self.assertTrue(report.complete, report)
        self.assertEqual(len(calls), 1)

    def test_progress_adds_no_queries(self):
        saved = observe(window(fullscreen=True, focused=True))
        enabled, disabled = FakeYabai(window(focused=True)), FakeYabai(window(focused=True))
        self.restore(enabled, saved)
        self.restore(disabled, saved, callback=False)
        self.assertEqual(enabled.queries, disabled.queries)


class TerminalProgressTests(unittest.TestCase):
    def test_narrow_terminal_keeps_status_on_one_line(self):
        from rich.console import Console, Group
        console = Console(width=45)
        status = console.status(Group(progress_text('beta', RestoreProgress('Restoring', 12, 27, 3, 5, 7, 16, 2))))
        self.assertEqual(len(console.render_lines(status.renderable)), 1)

    def test_text_styles_and_literal_name(self):
        text = progress_text('[beta]', RestoreProgress('Restoring', 12, 27, 3, 5, 7, 16, 2))
        self.assertEqual(text.plain, 'Restoring [beta] · windows 12/27 · fullscreen 3/5 · minimized 7/16 · ⚠️ 2')
        self.assertTrue(text.no_wrap)
        self.assertEqual(text.overflow, 'ellipsis')
        styles = {text.plain[s.start:s.end]: s.style for s in text.spans}
        self.assertEqual(styles['[beta]'], 'bold blue')
        self.assertEqual(styles[' · ⚠️ 2'], 'yellow')

    def test_redirected_stream_or_dumb_terminal_never_starts_status(self):
        for stdout, stderr, term in ((False, True, 'xterm'), (True, False, 'xterm'), (True, True, 'dumb')):
            with self.subTest(stdout=stdout, stderr=stderr, term=term), \
                    patch('sys.stdout.isatty', return_value=stdout), \
                    patch('sys.stderr.isatty', return_value=stderr), patch.dict('os.environ', TERM=term), \
                    patch('rich.console.Console.status') as status:
                with restore_spinner('beta') as callback:
                    self.assertIsNone(callback)
                status.assert_not_called()

    def test_status_stops_for_success_exception_and_interrupt(self):
        for error in (None, RuntimeError('failure'), KeyboardInterrupt()):
            with self.subTest(error=error), patch('sys.stdout.isatty', return_value=True), \
                    patch('sys.stderr.isatty', return_value=True), patch.dict('os.environ', TERM='xterm'), \
                    patch('rich.console.Console.status') as status:
                try:
                    with restore_spinner('beta') as callback:
                        callback(RestoreProgress('Verifying', 1, 1))
                        if error is not None:
                            raise error
                except BaseException as caught:
                    self.assertIs(caught, error)
                status.return_value.start.assert_called_once()
                status.return_value.update.assert_called_once()
                status.return_value.stop.assert_called_once()

    def test_real_rich_output_preserves_verbose_lines_and_restores_cursor(self):
        class Terminal(StringIO):
            def isatty(self):
                return True
        stdout, stderr = Terminal(), Terminal()
        with patch('sys.stdout', stdout), patch('sys.stderr', stderr), patch.dict('os.environ', TERM='xterm'):
            with restore_spinner('beta') as callback:
                callback(RestoreProgress('Verifying', 1, 1))
                click.echo('Pre-restore backup: synthetic')
            click.echo('Workspace snapshot beta was restored (0 missed)')
        self.assertIn('Pre-restore backup: synthetic', stderr.getvalue())
        self.assertIn('\x1b[?25l', stderr.getvalue())
        self.assertIn('\x1b[?25h', stderr.getvalue())
        self.assertEqual(stdout.getvalue(), 'Workspace snapshot beta was restored (0 missed)\n')

    def test_status_failure_falls_back_and_cleanup_failure_does_not_mask_restore(self):
        for start_fails in (False, True):
            with self.subTest(start_fails=start_fails), patch('sys.stdout.isatty', return_value=True), \
                    patch('sys.stderr.isatty', return_value=True), patch.dict('os.environ', TERM='xterm'), \
                    patch('rich.console.Console.status') as status:
                status.return_value.stop.side_effect = OSError('Closed terminal')
                if start_fails:
                    status.return_value.start.side_effect = OSError('Closed terminal')
                with restore_spinner('beta') as callback:
                    self.assertEqual(callback is None, start_fails)
