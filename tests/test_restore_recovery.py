"""Recover temporary lookups without widening window ownership authority."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

import click
from click.testing import CliRunner

from spacekeeper.accessibility import AccessibilityError
from spacekeeper.cli import emit_report
from spacekeeper.process import ProcessResult
from spacekeeper.restore import RestoreCoordinator
from spacekeeper.snapshot import ProcessIdentity
from spacekeeper.storage import SnapshotStore
from spacekeeper.yabai import Yabai
from tests.fake_yabai import Clock, FakeYabai
from tests.support import observe, window


class LookupGap(FakeYabai):
    def __init__(self, *windows, mode='transient'):
        super().__init__(*windows)
        self.mode, self.lookups = mode, {}

    def set_fullscreen(self, identity, desired, *, guard, **options):
        guard()
        wid = identity.window_id
        self.lookups[wid] = self.lookups.get(wid, 0) + 1
        if self.mode == 'persistent' or self.lookups[wid] == 1:
            if self.mode == 'closed':
                self.windows.pop(wid)
            elif self.mode == 'owner_changed':
                self.windows[wid] = replace(self.windows[wid], owner=ProcessIdentity(identity.owner.pid, 2000000, 0))
            elif self.mode == 'protected':
                self.windows[wid] = replace(self.windows[wid], subrole='AXDialog')
            error = AccessibilityError('access_denied' if self.mode == 'denied' else 'window_missing',
                                       'Synthetic native lookup failure.')
            error.stage = 'fullscreen.permission' if self.mode == 'denied' else 'fullscreen.ax_window_lookup'
            raise error
        return super().set_fullscreen(identity, desired, guard=guard, **options)


class RestoreRecoveryTests(unittest.TestCase):
    def test_persistent_lookup_finishes_with_styled_partial_result_and_exit_zero(self):
        report, _, persisted = self.restore(LookupGap(window(focused=True), mode='persistent'),
                                           observe(window(fullscreen=True, focused=True)))
        self.assertFalse(persisted['complete'])
        @click.command()
        @click.pass_context
        def command(ctx):
            emit_report(ctx, report, name='synthetic', verbose=False)
        result = CliRunner().invoke(command, color=True)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIsNone(result.exception)
        self.assertEqual(result.output, 'Workspace snapshot ' + click.style('synthetic', fg='blue', bold=True) +
                         ' was partially restored ' + click.style('(1 missed)', dim=True) + '\n')

    def test_persistent_lookup_failure_does_not_fail_other_windows_or_whole_restore(self):
        class OneBrokenWindow(FakeYabai):
            def set_fullscreen(self, identity, desired, *, guard, **options):
                if identity.window_id == 1:
                    guard()
                    raise AccessibilityError('window_missing', 'Synthetic missing AX reference.',
                                             stage='fullscreen.ax_window_lookup')
                return super().set_fullscreen(identity, desired, guard=guard, **options)
        fake = OneBrokenWindow(window(1, focused=True), window(2))
        report, _, persisted = self.restore(fake, observe(window(1, fullscreen=True, focused=True),
                                                         window(2, fullscreen=True)))
        self.assertFalse(report.complete)
        self.assertEqual(report.missed, 1)
        self.assertEqual(next(w.status for w in report.windows if w.window_id == 2), 'restored')
        self.assertTrue(fake.windows[2].fullscreen)
        self.assertEqual(len(persisted['windows'][0]['failures']), 2)
        self.assertFalse(report.hard_failure)

    def restore(self, fake, saved):
        clock = Clock()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = Yabai(executable='/synthetic/yabai', runner=fake, identities=fake,
                            fullscreen_setter=fake, clock=clock)
            coordinator = RestoreCoordinator(adapter, SnapshotStore(root/'state'), lock_path=root/'lock/restore',
                                              timeout=1, poll_interval=.1, clock=clock, sleep=clock.sleep)
            report = coordinator.restore(saved)
            persisted = json.loads(Path(report.report_path).read_text())
        return report, clock, persisted

    def test_beta_pattern_recovers_both_missing_fullscreen_windows_before_ordering(self):
        fake = LookupGap(window(1, focused=True), window(2))
        saved = observe(window(1, fullscreen=True, focused=True), window(2, fullscreen=True))
        report, clock, persisted = self.restore(fake, saved)
        self.assertTrue(report.complete, report)
        self.assertEqual(fake.lookups, {1: 2, 2: 2})
        self.assertTrue(all(w.fullscreen for w in fake.windows.values()))
        self.assertTrue(all(o.reason == 'restore_saved' for o in report.windows))
        self.assertLess(clock.now, 1)
        self.assertTrue(all(w['failures'][0]['stage'] == 'fullscreen.ax_window_lookup' for w in persisted['windows']))
        self.assertFalse(any('--toggle' in cmd for cmd in fake.commands))

    def test_persistent_lookup_is_retried_once_and_does_not_wait_for_order(self):
        fake = LookupGap(window(1, focused=True), window(2), mode='persistent')
        report, clock, _ = self.restore(fake, observe(window(1, fullscreen=True, focused=True),
                                                      window(2, fullscreen=True)))
        self.assertFalse(report.complete)
        self.assertEqual(fake.lookups, {1: 2, 2: 2})
        self.assertLess(clock.now, 1)
        self.assertTrue(any(i.reason == 'fullscreen_order_blocked' for i in report.issues))
        self.assertFalse(any(i.reason == 'timeout' for i in report.issues))
        self.assertFalse(any(cmd[1] == 'space' for cmd in fake.commands))

    def test_closed_and_replaced_windows_never_get_a_second_request(self):
        for mode in ('closed', 'owner_changed'):
            with self.subTest(mode=mode):
                fake = LookupGap(window(focused=True), mode=mode)
                report, _, _ = self.restore(fake, observe(window(fullscreen=True, focused=True)))
                self.assertFalse(report.complete)
                self.assertEqual(fake.lookups, {1: 1})
                self.assertFalse(any('--set-fullscreen' in cmd for cmd in fake.commands))

    def test_newly_protected_window_is_not_edited_during_recovery(self):
        fake = LookupGap(window(focused=True), mode='protected')
        report, _, _ = self.restore(fake, observe(window(fullscreen=True, focused=True)))
        self.assertFalse(report.complete)
        self.assertEqual(fake.lookups, {1: 1})
        self.assertEqual(fake.windows[1].subrole, 'AXDialog')
        self.assertFalse(any('--set-fullscreen' in cmd for cmd in fake.commands))

    def test_permission_failure_does_not_authorize_recovery(self):
        fake = LookupGap(window(focused=True), mode='denied')
        report, _, persisted = self.restore(fake, observe(window(fullscreen=True, focused=True)))
        self.assertFalse(report.complete)
        self.assertEqual(fake.lookups, {1: 1})
        self.assertEqual(persisted['windows'][0]['failures'][0]['stage'], 'fullscreen.permission')
        self.assertFalse(any('--toggle' in cmd for cmd in fake.commands))

    def test_target_query_gap_is_distinguished_from_native_ax_lookup(self):
        class QueryGap(FakeYabai):
            target_queries = 0
            def __call__(self, argv, **options):
                result = super().__call__(argv, **options)
                if '--window' in argv:
                    self.target_queries += 1
                    if self.target_queries == 2:
                        return ProcessResult(1, b'', b'could not retrieve window details')
                return result
        fake = QueryGap(window(focused=True))
        report, _, persisted = self.restore(fake, observe(window(fullscreen=True, focused=True)))
        self.assertTrue(report.complete, report)
        self.assertEqual(persisted['windows'][0]['failures'][0]['stage'], 'window.query')

    def test_verbose_explains_recovered_failure_but_normal_output_stays_concise(self):
        fake = LookupGap(window(focused=True))
        report, _, _ = self.restore(fake, observe(window(fullscreen=True, focused=True)))
        self.assertTrue(report.complete, report)
        for verbose in (False, True):
            @click.command()
            @click.pass_context
            def command(ctx):
                emit_report(ctx, report, name='test', verbose=verbose)
            result = CliRunner().invoke(command)
            self.assertEqual(result.exit_code, 0, result.output)
            if verbose:
                self.assertIn('fullscreen.ax_window_lookup', result.output)
                self.assertIn('window_missing', result.output)
            else:
                self.assertEqual(result.output, 'Workspace snapshot test was restored (0 missed)\n')

    def test_owner_change_between_verification_and_retry_prevents_the_edit(self):
        fake, clock = LookupGap(window(focused=True)), Clock()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = Yabai(executable='/synthetic/yabai', runner=fake, identities=fake,
                            fullscreen_setter=fake, clock=clock)
            focus = adapter.request_focus
            calls = []
            def changed(*args, **options):
                calls.append(True)
                if len(calls) == 2:
                    fake.windows[1] = replace(fake.windows[1], owner=ProcessIdentity(101,2000000,0))
                return focus(*args, **options)
            adapter.request_focus = changed
            coordinator = RestoreCoordinator(adapter, SnapshotStore(root/'state'), lock_path=root/'lock/restore',
                                              timeout=1, poll_interval=.1, clock=clock, sleep=clock.sleep)
            report = coordinator.restore(observe(window(fullscreen=True, focused=True)))
        self.assertFalse(report.complete)
        self.assertEqual(fake.lookups, {1: 1})
        self.assertFalse(any('--set-fullscreen' in cmd for cmd in fake.commands))
        self.assertEqual(report.windows[0].failures[-1].stage, 'owner.process')

    def test_permission_failure_blocks_order_without_waiting_for_fullscreen(self):
        fake = LookupGap(window(1, focused=True), window(2), mode='denied')
        saved = observe(window(1, fullscreen=True, focused=True), window(2, fullscreen=True))
        report, clock, _ = self.restore(fake, saved)
        self.assertFalse(report.complete)
        self.assertLess(clock.now, 1)
        self.assertEqual(fake.lookups, {1: 1, 2: 1})
        self.assertTrue(any(i.reason == 'fullscreen_order_blocked' for i in report.issues))
        self.assertFalse(any(i.reason == 'timeout' for i in report.issues))

    def test_failed_recovery_keeps_both_failure_records(self):
        fake = LookupGap(window(focused=True), mode='persistent')
        report, _, persisted = self.restore(fake, observe(window(fullscreen=True, focused=True)))
        self.assertFalse(report.complete)
        self.assertEqual(len(persisted['windows'][0]['failures']), 2)
        self.assertTrue(all(f['stage'] == 'fullscreen.ax_window_lookup' for f in persisted['windows'][0]['failures']))
