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
from spacekeeper.yabai import Yabai, YabaiError, YabaiFailure
from tests.fake_yabai import Clock, FakeYabai
from tests.support import A, B, SESSION, observe, window


class CliTests(unittest.TestCase):
    def test_state_filters_restore_named_and_backup_snapshots(self):
        saved = observe(window(fullscreen=True), window(2, minimized=True))
        store = SnapshotStore(self.state)
        store.save(saved, 'alpha')
        backup = store.backup(saved)
        for option, selected in (('--only-fullscreen', {1}), ('--only-minimize', {2}),
                                 ('--no-fullscreen', {2})):
            for source in (('alpha',), ('--backup', backup.stem)):
                with self.subTest(option=option, source=source):
                    self.device.windows = {1: window(), 2: window(2), 3: window(3, fullscreen=True, display=B)}
                    self.device.commands.clear()
                    result = self.invoke('restore', *source, option)
                    self.assertEqual(result.exit_code, 0, result.output)
                    self.assertEqual(self.device.windows[1].fullscreen, 1 in selected)
                    self.assertEqual(self.device.windows[2].minimized, 2 in selected)
                    self.assertTrue(self.device.windows[3].fullscreen)

    def test_conflicting_state_filters_fail_before_loading_snapshot_or_desktop(self):
        for flags in (('--only-fullscreen', '--only-minimize'),
                      ('--only-fullscreen', '--no-fullscreen'),
                      ('--only-minimize', '--no-minimize')):
            with self.subTest(flags=flags), patch('spacekeeper.cli.desktop') as device, \
                    patch.object(SnapshotStore, 'load') as load:
                result = self.invoke('restore', 'alpha', *flags)
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertIn('not both', result.output)
                device.assert_not_called()
                load.assert_not_called()
                self.assertFalse(self.state.exists())

    def test_verbose_state_filter_explanation_does_not_claim_extra_cleanup(self):
        SnapshotStore(self.state).save(observe(window()), 'alpha')
        result = self.invoke('restore', 'alpha', '--only-fullscreen', '--verbose')
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('other current windows will stay untouched', result.output)
        self.assertNotIn('will exit fullscreen', result.output)

    def test_ignore_position_works_for_snapshot_and_backup(self):
        saved = observe(window(fullscreen=True, focused=True), window(2, fullscreen=True))
        store = SnapshotStore(self.state)
        store.save(saved, 'alpha')
        backup = store.backup(saved)
        self.device.windows = {w.id: w for w in saved.windows}
        self.device.space_indexes = {1001: 4, 1002: 3}
        for args in (('restore', 'alpha', '--ignore-position'),
                     ('restore', '--backup', backup.stem, '--ignore-position')):
            with self.subTest(args=args):
                result = self.invoke(*args)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(self.device.commands, [])

    def test_no_minimize_ignores_dock_targets_for_named_and_backup_restores(self):
        store = SnapshotStore(self.state)
        saved = observe(window(minimized=True))
        store.save(saved, 'alpha')
        backup = store.backup(saved)
        original = self.device.windows[1]
        for args in (('restore', 'alpha', '--no-minimize'),
                     ('restore', '--backup', backup.stem, '--no-minimize')):
            with self.subTest(args=args):
                result = self.invoke(*args)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertIn('was restored (0 missed)', result.output)
                self.assertEqual(self.device.windows[1], original)
                self.assertEqual(self.device.commands, [])

    def test_restore_defaults_to_one_completion_line_and_persists_details(self):
        SnapshotStore(self.state).save(observe(window(focused=True)), 'alpha')
        result = self.invoke('restore', 'alpha')
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output, 'Workspace snapshot alpha was restored (0 missed)\n')
        self.assertEqual(len(list((self.state/'backups').glob('*.json'))), 1)
        self.assertEqual(len(list((self.state/'reports').glob('*.json'))), 1)

    def test_restore_completion_styles_name_and_missed_count(self):
        import click
        SnapshotStore(self.state).save(observe(window(focused=True)), 'alpha')
        result = self.invoke('restore', 'alpha', color=True)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(click.style('alpha', fg='blue', bold=True), result.output)
        self.assertIn(click.style('(0 missed)', dim=True), result.output)

    def test_verbose_can_precede_or_follow_restore_and_keeps_the_completion_last(self):
        SnapshotStore(self.state).save(observe(window(focused=True)), 'alpha')
        for args in (('--verbose', 'restore', 'alpha'), ('restore', 'alpha', '--verbose')):
            with self.subTest(args=args):
                result = self.invoke(*args)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertIn('Pre-restore backup:', result.output)
                self.assertIn('Report:', result.output)
                self.assertTrue(result.output.endswith('Workspace snapshot alpha was restored (0 missed)\n'))

    def test_default_timeout_allows_thirty_seconds_without_a_special_flag(self):
        from spacekeeper.config import Settings
        self.assertEqual(Settings.load(config_dir=self.config).timeout, 30)

    def test_query_timeout_names_the_read_stage_and_prevents_backup_or_commands(self):
        SnapshotStore(self.state).save(observe(window()))
        def timeout(domain, count):
            raise YabaiError(YabaiFailure.TIMEOUT, 'Command execution failed: timeout.')
        self.device.on_query = timeout
        result = self.invoke('restore', '--verbose')
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn('Desktop observation timed out', result.output)
        self.assertIn('yabai displays query timed out', result.output)
        self.assertIn('increase --timeout', result.output)
        self.assertNotIn('Pre-restore backup:', result.output)
        self.assertEqual(self.device.commands, [])

    def test_query_timeout_is_a_short_error_without_verbose(self):
        SnapshotStore(self.state).save(observe(window()))
        def timeout(domain, count):
            raise YabaiError(YabaiFailure.TIMEOUT, 'Details')
        self.device.on_query = timeout
        result = self.invoke('restore')
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.output, 'Error: Timed out waiting for the desktop to settle.\n')
        self.assertEqual(self.device.commands, [])

    def test_global_final_verification_timeout_cannot_print_zero_missed_success(self):
        SnapshotStore(self.state).save(observe(window(focused=True)))
        def timeout(domain, count):
            if domain == 'displays' and count > 2:
                raise YabaiError(YabaiFailure.TIMEOUT, 'Details')
        self.device.on_query = timeout
        result = self.invoke('restore')
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.output, 'Error: Desktop verification timed out.\n')
        self.assertNotIn('was restored', result.output)
        self.assertEqual(self.device.commands, [])

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
        adapter = Yabai(executable='/synthetic/yabai', runner=self.device, identities=self.device, fullscreen_setter=self.device, clock=self.clock)
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
        self.assertEqual(restored.output, 'Workspace snapshot desk was restored (0 missed)\n')
        self.assertEqual((self.device.windows[1].display_uuid, self.device.windows[1].fullscreen), (A, False))
        self.assertFalse(self.device.windows[2].fullscreen)
        backup = next((self.state / 'backups').glob('*.json'))
        recovered = self.invoke('restore', '--backup', backup.stem)
        self.assertEqual(recovered.exit_code, 0, recovered.output)
        self.assertEqual(recovered.output, f'Workspace snapshot {backup.stem} was restored (0 missed)\n')
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
        result = self.invoke('restore', '--verbose')
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn('Pre-restore backup:', result.output)
        self.assertIn('Restore incomplete', result.output)
        self.assertNotIn('was restored', result.output)
        self.assertEqual(len(list((self.state / 'backups').glob('*.json'))), 1)
        self.assertEqual(len(list((self.state / 'reports').glob('*.json'))), 1)

    def test_no_fallback_rejects_old_session_without_mutation(self):
        SnapshotStore(self.state).save(observe(window(), session=replace(SESSION, login_session_id=101)))
        result = self.invoke('restore', '--no-fallback')
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.output, 'Error: Snapshot belongs to a different boot/login session.\n')
        self.assertEqual(self.device.commands, [])

    def test_old_session_uses_default_name_fallback_with_current_window_ids(self):
        saved = observe(window(fullscreen=True, display=B, focused=True),
                        session=replace(SESSION, login_session_id=101))
        SnapshotStore(self.state).save(saved)
        extra = window(1, app='Other', fullscreen=True)
        self.device.windows = {1: extra, 9: window(9, focused=True)}
        result = self.invoke('restore', '--verbose')
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('using partial name fallback', result.output)
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
        self.device.windows[2] = window(2, title='Another window')
        result = self.invoke('restore')
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(result.output, 'Workspace snapshot default was restored (1 missed)\n')
        self.assertEqual(self.device.commands, [])
        detailed = self.invoke('restore', '--verbose')
        self.assertIn('name_no_match (snapshot window 1)', detailed.output)
        self.assertTrue(detailed.output.endswith('Workspace snapshot default was restored (1 missed)\n'))

    def test_single_window_app_fallback_is_explicit_in_stdout_and_report(self):
        SnapshotStore(self.state).save(observe(window(title='Old title', minimized=True),
                                              session=replace(SESSION, login_session_id=101)))
        result = self.invoke('restore', '--verbose')
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('by single-window app fallback.', result.output)
        self.assertTrue(self.device.windows[1].minimized)
        report = json.loads(next((self.state/'reports').glob('*.json')).read_text())
        self.assertEqual(report['windows'][0]['match_method'], 'single_window_app')

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
