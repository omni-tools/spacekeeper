from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from spacekeeper.cli import main
from spacekeeper.restore import RestoreCoordinator, restore_lock
from spacekeeper.storage import SnapshotStore
from spacekeeper.yabai import Yabai
from tests.fake_yabai import Clock, FakeYabai
from tests.support import A, B, SESSION, observe, window


class CliTests(unittest.TestCase):
    def test_preset_capture_preview_apply_and_overwrite(self):
        captured = self.invoke('preset', 'capture', 'work')
        self.assertEqual(captured.exit_code, 0, captured.output)
        self.assertEqual(self.invoke('preset', 'capture', 'work').exit_code, 1)
        self.assertEqual(self.invoke('preset', 'capture', 'work', '--force').exit_code, 0)
        self.assertIn('work', self.invoke('preset', 'list').output)
        self.assertIn('windows:', self.invoke('preset', 'show', 'work').output)
        self.device.windows[1] = replace(self.device.windows[1], fullscreen=True, space_id=1001)
        self.assertEqual(self.invoke('preset', 'plan', 'work').exit_code, 0)
        self.assertFalse(self.state.exists())
        result = self.invoke('preset', 'apply', 'work')
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(self.device.windows[1].fullscreen)
        self.assertEqual(len(list((self.state/'backups').glob('*.json'))), 1)

    def test_preset_list_show_and_invalid_yaml_do_not_require_native_access(self):
        with patch('spacekeeper.cli.Yabai', side_effect=AssertionError('Unexpected native access')):
            self.assertEqual(self.invoke('preset', 'list').exit_code, 0)
            self.assertFalse(self.config.exists())
            (self.config/'presets').mkdir(parents=True)
            (self.config/'presets/bad.yaml').write_text('windows: []\nwindows: []')
            self.assertEqual(self.invoke('preset', 'show', 'bad').exit_code, 1)
            self.assertEqual(self.invoke('preset', 'apply', 'bad').exit_code, 1)
        self.assertFalse(self.state.exists())

    def test_preserve_cli_rules_and_configuration_can_be_disabled(self):
        self.invoke('capture')
        self.device.windows[2] = window(2, fullscreen=True, display=B, title='Meeting')
        result = self.invoke('restore', '--preserve', 'synthetic*', '*meeting*')
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(self.device.windows[2].fullscreen)
        self.config.mkdir()
        (self.config/'config.json').write_text(json.dumps({'preserve': [{'app': '*'}]}))
        self.assertEqual(self.invoke('restore').exit_code, 0)
        self.assertTrue(self.device.windows[2].fullscreen)
        result = self.invoke('restore', '--no-preserve')
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(self.device.windows[2].fullscreen)

    def test_invalid_preserve_options_never_access_the_desktop(self):
        with patch('spacekeeper.cli.Yabai', side_effect=AssertionError('Unexpected native access')):
            for args in (('restore', '--preserve', '', '*'),
                         ('restore', '--preserve', '*', '*', '--no-preserve')):
                self.assertEqual(self.invoke(*args).exit_code, 2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state, self.config = self.root / 'state', self.root / 'config'
        self.runner, self.clock = CliRunner(), Clock()
        self.device = FakeYabai(window(focused=True))
        adapter = Yabai(executable='/synthetic/yabai', runner=self.device, identities=self.device, clock=self.clock)
        factory = patch('spacekeeper.cli.Yabai', return_value=adapter)
        factory.start()
        self.addCleanup(factory.stop)
        def coordinator(desktop, store, **options):
            return RestoreCoordinator(desktop, store, lock_path=self.root / 'lock/restore.lock', timeout=1,
                                      poll_interval=0.1, clock=self.clock, sleep=self.clock.sleep,
                                      on_backup=options.get('on_backup'), on_plan=options.get('on_plan'))
        factory = patch('spacekeeper.cli.RestoreCoordinator', side_effect=coordinator)
        factory.start()
        self.addCleanup(factory.stop)

    def invoke(self, *args, **kwargs):
        return self.runner.invoke(main, ['--config-dir', str(self.config), '--state-dir', str(self.state), *args], **kwargs)

    def test_capture_list_restore_and_backup_recovery_end_to_end(self):
        captured = self.invoke('capture', 'desk')
        self.assertEqual(captured.exit_code, 0, captured.output)
        self.assertEqual(self.device.commands, [])
        self.assertIn('desk', self.invoke('list').output)
        self.device.windows[1] = replace(self.device.windows[1], fullscreen=True, display_uuid=B, space_id=1001)
        self.device.windows[2] = window(2, fullscreen=True)
        restored = self.invoke('restore', 'desk')
        self.assertEqual(restored.exit_code, 0, restored.output)
        self.assertIn('Restore complete', restored.output)
        self.assertEqual((self.device.windows[1].display_uuid, self.device.windows[1].fullscreen), (A, False))
        self.assertFalse(self.device.windows[2].fullscreen)
        backup = next((self.state / 'backups').glob('*.json'))
        recovered = self.invoke('restore', '--backup', backup.stem)
        self.assertEqual(recovered.exit_code, 0, recovered.output)
        self.assertEqual((self.device.windows[1].display_uuid, self.device.windows[1].fullscreen), (B, True))
        self.assertTrue(self.device.windows[2].fullscreen)

    def test_default_overwrite_named_force_and_invalid_names(self):
        for args in (('capture',), ('capture',), ('capture', 'work')):
            self.assertEqual(self.invoke(*args).exit_code, 0)
        self.assertEqual(self.invoke('capture', 'work').exit_code, 1)
        self.assertEqual(self.invoke('capture', 'work', '--force').exit_code, 0)
        self.assertEqual(self.invoke('capture', '../escape').exit_code, 2)

    def test_list_does_not_initialize_yabai_or_create_storage(self):
        with patch('spacekeeper.cli.Yabai', side_effect=AssertionError('Unexpected native access')):
            result = self.invoke('list')
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.config.exists())

    def test_corrupt_and_missing_snapshots_fail_without_mutation(self):
        self.assertEqual(self.invoke('restore', 'missing').exit_code, 1)
        (self.state / 'snapshots').mkdir(parents=True)
        (self.state / 'snapshots/bad.json').write_text('broken')
        self.assertEqual(self.invoke('restore', 'bad').exit_code, 1)
        listed = self.invoke('list')
        self.assertEqual(listed.exit_code, 1)
        self.assertIn('INVALID', listed.output)
        self.assertEqual(self.device.commands, [])

    def test_incomplete_restore_exits_one_with_backup_and_report(self):
        SnapshotStore(self.state).save(observe(window(fullscreen=True)))
        self.device.effects['toggle'] = False
        result = self.invoke('restore')
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn('Pre-restore backup:', result.output)
        self.assertIn('Restore incomplete', result.output)
        self.assertEqual(len(list((self.state / 'backups').glob('*.json'))), 1)
        self.assertEqual(len(list((self.state / 'reports').glob('*.json'))), 1)

    def test_no_fallback_rejects_old_session_without_mutation(self):
        SnapshotStore(self.state).save(observe(window(), session=replace(SESSION, login_session_id=101)))
        result = self.invoke('restore', '--no-fallback')
        self.assertEqual(result.exit_code, 1)
        self.assertIn('session_mismatch', result.output)
        self.assertEqual(self.device.commands, [])

    def test_old_session_uses_default_name_fallback_with_current_window_ids(self):
        saved = observe(window(fullscreen=True, display=B, focused=True),
                        session=replace(SESSION, login_session_id=101))
        SnapshotStore(self.state).save(saved)
        extra = window(1, app='Other', fullscreen=True)
        self.device.windows = {1: extra, 9: window(9, focused=True)}
        result = self.invoke('restore')
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('using partial app/title name fallback', result.output)
        self.assertIn('Window 9: matched snapshot window 1', result.output)
        self.assertNotIn('will exit fullscreen', result.output)
        self.assertEqual(self.device.windows[1], extra)
        self.assertEqual((self.device.windows[9].display_uuid, self.device.windows[9].fullscreen), (B, True))
        report = json.loads(next((self.state/'reports').glob('*.json')).read_text())
        self.assertTrue(report['name_fallback'])
        self.assertEqual(next(w for w in report['windows'] if w['window_id'] == 9)['source_window_id'], 1)

    def test_partial_name_fallback_reports_missing_sources_and_exits_one(self):
        SnapshotStore(self.state).save(observe(window(title='Missing title'),
                                              session=replace(SESSION, login_session_id=101)))
        result = self.invoke('restore')
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn('name_no_match (snapshot window 1)', result.output)
        self.assertEqual(self.device.commands, [])

    def test_concurrent_restore_reports_lock_failure(self):
        SnapshotStore(self.state).save(observe(window()))
        with restore_lock(self.root / 'lock/restore.lock'):
            result = self.invoke('restore')
        self.assertEqual(result.exit_code, 1)
        self.assertIn('already running', result.output)
        self.assertEqual(self.device.commands, [])

    def test_backup_selection_is_unambiguous(self):
        self.assertEqual(self.invoke('restore', 'desk', '--backup', 'previous').exit_code, 2)

    def test_help_version_and_usage_without_desktop(self):
        with patch('spacekeeper.cli.Yabai', side_effect=AssertionError('Unexpected native access')):
            for args in (('--help',), ('--version',), ('capture', '--help'), ('restore', '--help')):
                result = self.invoke(*args)
                self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.invoke('unknown').exit_code, 2)

    def test_config_file_environment_and_cli_precedence(self):
        self.config.mkdir()
        (self.config / 'config.json').write_text(json.dumps({'state_dir': 'relative-state', 'timeout': 20}))
        result = self.runner.invoke(main, ['--config-dir', str(self.config), 'capture'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue((self.config / 'relative-state/snapshots/default.json').exists())
        env_state = self.root / 'environment-state'
        result = self.runner.invoke(main, ['--config-dir', str(self.config), 'capture'],
                                    env={'SPACEKEEPER_STATE_DIR': str(env_state)})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue((env_state / 'snapshots/default.json').exists())
        self.assertEqual(self.invoke('capture', env={'SPACEKEEPER_STATE_DIR': str(env_state)}).exit_code, 0)
        self.assertTrue((self.state / 'snapshots/default.json').exists())

    def test_invalid_configuration_is_clear_and_read_only(self):
        self.config.mkdir()
        for data in ('{', '[]', '{"timeout":true}', '{"timeout":NaN}', '{"unexpected":1}', '{"state_dir":42}',
                     json.dumps({'timeout': 10**500}), json.dumps({'state_dir': 'bad\0path'})):
            (self.config / 'config.json').write_text(data)
            result = self.invoke('list')
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn('Error:', result.output)
        self.assertFalse(self.state.exists())
