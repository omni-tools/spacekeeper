"""Query budgets for real restore paths against the synthetic yabai service."""

from pathlib import Path
import tempfile
import unittest

from spacekeeper.planner import WindowTarget
from spacekeeper.restore import RestoreCoordinator
from spacekeeper.storage import SnapshotStore
from spacekeeper.transitions import WindowTransitions
from spacekeeper.yabai import Yabai
from tests.fake_yabai import Clock, FakeYabai
from tests.support import A, B, SESSION, observe, window


class QueryBudgetTests(unittest.TestCase):
    def test_already_focused_guard_can_seed_confirmation_without_a_duplicate_read(self):
        fake, clock = FakeYabai(window(focused=True)), Clock()
        engine = WindowTransitions(self.adapter(fake, clock), clock=clock, sleep=clock.sleep)
        result = engine.apply(WindowTarget(window().identity, A, True), SESSION)
        self.assertEqual(result.status, 'verified')
        self.assertLessEqual(sum(args[2] == '--windows' for args in fake.queries), 12)

    def test_snapshot_commands_do_not_import_the_yaml_parser(self):
        import subprocess
        import sys
        code = ('from click.testing import CliRunner; from spacekeeper.cli import main; import sys; '
                'result=CliRunner().invoke(main,["--version"]); '
                'assert result.exit_code==0; assert "yaml" not in sys.modules')
        subprocess.run([sys.executable, '-c', code], check=True, capture_output=True)

    def adapter(self, fake, clock):
        return Yabai(executable='/synthetic/yabai', runner=fake, identities=fake, clock=clock)

    def full_window_queries(self, fake):
        return [args for args in fake.queries if args[2] == '--windows' and '--window' not in args]

    def test_window_transition_does_not_read_every_other_window(self):
        fake = FakeYabai(window(fullscreen=True, display=B), *[window(i) for i in range(2, 20)])
        clock = Clock()
        engine = WindowTransitions(self.adapter(fake, clock), clock=clock, sleep=clock.sleep)
        result = engine.apply(WindowTarget(window().identity, A, True), SESSION)
        self.assertEqual(result.status, 'verified')
        self.assertEqual((fake.windows[1].display_uuid, fake.windows[1].fullscreen), (A, True))
        self.assertEqual(self.full_window_queries(fake), [])

    def test_no_change_restore_only_reads_global_backup_and_final_verification(self):
        self.check_restore(window(focused=True), window(focused=True))

    def test_fullscreen_move_only_reads_global_backup_and_final_verification(self):
        self.check_restore(window(fullscreen=True, focused=True, display=B),
                           window(fullscreen=True, focused=True))

    def check_restore(self, actual, wanted):
        fake, clock = FakeYabai(actual), Clock()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            coordinator = RestoreCoordinator(self.adapter(fake, clock), SnapshotStore(root/'state'),
                                             lock_path=root/'lock/restore.lock', clock=clock, sleep=clock.sleep)
            report = coordinator.restore(observe(wanted))
            self.assertTrue(report.complete, report)
            self.assertTrue(Path(report.backup_path).is_file())
            # Two coherent passes for each of backup + two stable final reads.
            self.assertEqual(len(self.full_window_queries(fake)), 6)
