"""Human-readable CLI; structured restore reports are saved privately on disk."""
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
import time

import click

from spacekeeper.config import ConfigError, Settings
from spacekeeper.identity import IdentityUnavailable
from spacekeeper.matching import WindowRule
from spacekeeper.restore import RestoreCoordinator, RestoreError
from spacekeeper.storage import SnapshotStore, StorageError, validate_name
from spacekeeper.yabai import Yabai, YabaiError, YabaiFailure


class SnapshotName(click.ParamType):
    name = 'snapshot-name'

    def convert(self, value, param, ctx):
        try:
            return validate_name(value)
        except StorageError as error:
            self.fail(str(error), param, ctx)


NAME = SnapshotName()
DIRECTORY = click.Path(file_okay=False, path_type=Path)


@contextmanager
def operational_errors(*, verbose=True):
    try:
        yield
    except (StorageError, YabaiError, IdentityUnavailable, RestoreError, ConfigError) as error:
        if not verbose and isinstance(error, YabaiError) and error.reason == YabaiFailure.TIMEOUT:
            raise click.ClickException('Timed out waiting for the desktop to settle.') from error
        raise click.ClickException(str(error)) from error


def desktop(settings: Settings) -> Yabai:
    return Yabai(executable=str(settings.yabai) if settings.yabai is not None else None,
                 command_timeout=min(10, settings.timeout))


@click.group(context_settings={'help_option_names': ['-h', '--help']})
@click.version_option(package_name='spacekeeper')
@click.option('--state-dir', type=DIRECTORY, envvar='SPACEKEEPER_STATE_DIR',
              help='Snapshot/backup/report directory (default: ~/.local/state/spacekeeper).')
@click.option('--config-dir', type=DIRECTORY, envvar='SPACEKEEPER_CONFIG_DIR',
              help='Directory containing config.json (default: ~/.config/spacekeeper).')
@click.option('--yabai', type=click.Path(dir_okay=False, path_type=Path), envvar='SPACEKEEPER_YABAI',
              help='Path to yabai; otherwise discover it automatically.')
@click.option('--timeout', type=click.FloatRange(min=0.1, max=300), envvar='SPACEKEEPER_TIMEOUT',
              help='Per-transition/observation timeout in seconds (default: 30).')
@click.option('-v', '--verbose', 'global_verbose', is_flag=True, help='Show detailed restore diagnostics.')
@click.pass_context
def main(ctx: click.Context, state_dir, config_dir, yabai, timeout, global_verbose) -> None:
    """Capture and restore macOS fullscreen arrangements in the current session."""
    with operational_errors():
        ctx.obj = Settings.load(state_dir=state_dir, config_dir=config_dir, yabai=yabai, timeout=timeout)
        ctx.meta['verbose'] = global_verbose


@main.command()
@click.argument('name', default='default', type=NAME)
@click.option('--force', is_flag=True, help='Replace an existing named snapshot.')
@click.pass_obj
def capture(settings: Settings, name: str, force: bool) -> None:
    """Save NAME (default: default) without changing desktop windows.

    The default snapshot is replaced automatically. Other existing names
    require --force.
    """
    with operational_errors():
        snapshot = desktop(settings).observe(deadline=time.monotonic() + settings.timeout)
        path = SnapshotStore(settings.state_dir).save(snapshot, name, force=force)
        click.echo(f'Saved {name}: {len(snapshot.windows)} windows. {path}')


@main.command('list')
@click.pass_context
def list_snapshots(ctx: click.Context) -> None:
    """List snapshot names, capture times, and window counts."""
    with operational_errors():
        entries = SnapshotStore(ctx.obj.state_dir).entries()
        if not entries:
            click.echo('No snapshots.')
        for entry in entries:
            click.echo(f'{entry.name}\tINVALID ({entry.error})' if entry.error is not None else
                       f'{entry.name}\t{entry.captured_at}\t{entry.windows} windows')
        if any(entry.error is not None for entry in entries):
            ctx.exit(1)


@main.command()
@click.argument('name', required=False, type=NAME)
@click.option('--backup', 'backup_id', type=NAME, help='Restore a pre-restore backup by its filename ID.')
@click.option('--preserve', 'preserve_rules', type=(str, str), multiple=True, metavar='APP_GLOB TITLE_GLOB',
              help='Keep matching extra fullscreen windows untouched; repeat for more rules. Use * for any title.')
@click.option('--no-preserve', is_flag=True, help='Ignore configured preserve rules for this restore.')
@click.option('--no-fallback', is_flag=True,
              help='Reject stale boot/login snapshots instead of partially matching app and window names.')
@click.option('-v', '--verbose', is_flag=True, help='Show backup, matching, verification, and report details.')
@click.pass_context
def restore(ctx: click.Context, name: str | None, backup_id: str | None, preserve_rules, no_preserve: bool,
            no_fallback: bool, verbose: bool) -> None:
    """Restore NAME (default: default), with name fallback for stale sessions.

    Same-session restores exit fullscreen for extra windows. After a reboot or
    login change, unique app/title pairs are preferred. Code titles match by their
    trailing workspace name. Apps with one saved and one current main window can match
    regardless of title. Unmatched current windows stay untouched. A verified
    backup is saved before mutations.
    Missing/ambiguous windows make the result incomplete (exit 1).
    """
    verbose = verbose or ctx.meta.get('verbose', False)
    if backup_id is not None and name is not None:
        raise click.UsageError('Choose a snapshot name or --backup, not both.')
    if no_preserve and preserve_rules:
        raise click.UsageError('Choose --preserve or --no-preserve, not both.')
    try:
        preserve = () if no_preserve else ctx.obj.preserve + tuple(WindowRule(*rule) for rule in preserve_rules)
    except ValueError as error:
        raise click.BadParameter(str(error), param_hint='--preserve') from error
    settings = ctx.obj
    with operational_errors(verbose=verbose):
        store = SnapshotStore(settings.state_dir)
        saved = store.load_backup(backup_id) if backup_id is not None else store.load(name or 'default')
        def explain_plan(plan):
            if plan.name_fallback:
                click.echo('Saved session is stale; using partial name fallback. '
                           'Single-window apps may match regardless of title. '
                           'Unmatched current windows will stay untouched.')
            elif plan.accepted:
                click.echo('Unmatched extra fullscreen windows absent from this snapshot will exit fullscreen.')
                if preserve:
                    click.echo(f'Preserve filters: {len(preserve)} rule(s).')
        coordinator = RestoreCoordinator(desktop(settings), store, timeout=settings.timeout,
                                         on_backup=(lambda path: click.echo(f'Pre-restore backup: {path}')) if verbose else None,
                                         on_plan=explain_plan if verbose else None)
        report = coordinator.restore(saved, preserve=preserve, fallback=not no_fallback)
        emit_report(ctx, report, name=backup_id or name or 'default', verbose=verbose)


def emit_report(ctx, report, *, name=None, verbose=True):
    if verbose:
        emit_details(report)
    if name is not None:
        if report.hard_failure:
            if any(i.reason == 'session_mismatch' for i in report.issues):
                message = 'Snapshot belongs to a different boot/login session.'
            elif any(i.scope == 'verification' and i.reason == 'timeout' for i in report.issues):
                message = 'Desktop verification timed out.'
            else:
                message = 'Restore could not be verified.'
            raise click.ClickException(message)
        click.echo('Workspace snapshot ' + click.style(name, fg='blue', bold=True) + ' was restored ' +
                   click.style(f'({report.missed} missed)', dim=True))
    if not report.complete:
        ctx.exit(1)


def emit_details(report):
    counts = Counter(window.status for window in report.windows)
    summary = ', '.join(f'{count} {status}' for status, count in sorted(counts.items())) or 'no windows'
    click.echo(('Restore complete: ' if report.complete else 'Restore incomplete: ') + summary)
    for window in report.windows:
        if window.source_window_id is not None:
            method = 'single-window app fallback' if window.match_method == 'single_window_app' else 'app/title'
            click.echo(f'Window {window.window_id}: matched snapshot window {window.source_window_id} by {method}.')
        if window.monitor_fallback:
            click.echo(f'Window {window.window_id}: saved monitor disconnected; using its current monitor.')
        if window.status not in ('restored', 'unchanged', 'excluded'):
            click.echo(f'Window {window.window_id}: {window.status} ({window.reason}).')
    for issue in report.issues:
        locations = []
        if issue.source_window_id is not None:
            locations.append(f'snapshot window {issue.source_window_id}')
        if issue.window_id is not None:
            locations.append(f'window {issue.window_id}')
        click.echo(f'{issue.scope}: {issue.reason}' + (' (' + ', '.join(locations) + ')' if locations else ''))
    click.echo(f'Report: {report.report_path}')


@main.group('preset')
def preset_commands():
    """Capture, inspect, and apply named YAML layouts."""


@preset_commands.command('capture')
@click.argument('name', type=NAME)
@click.option('--force', is_flag=True, help='Replace an existing preset.')
@click.pass_obj
def capture_preset(settings: Settings, name: str, force: bool):
    """Capture supported windows as app/title/device rules without moving them."""
    from spacekeeper.presets import Preset, PresetStore
    with operational_errors():
        snapshot = desktop(settings).observe(deadline=time.monotonic() + settings.timeout)
        preset = Preset.capture(snapshot)
        path = PresetStore(settings.config_dir).save(preset, name, force=force)
        click.echo(f'Saved preset {name}: {len(preset.windows)} rules. {path}')


@preset_commands.command('list')
@click.pass_obj
def list_presets(settings: Settings):
    """List named presets without querying the desktop."""
    from spacekeeper.presets import PresetStore
    with operational_errors():
        names = PresetStore(settings.config_dir).entries()
        click.echo('\n'.join(names) if names else 'No presets.')


@preset_commands.command('show')
@click.argument('name', type=NAME)
@click.pass_obj
def show_preset(settings: Settings, name: str):
    """Print a validated preset's YAML without querying the desktop."""
    from spacekeeper.presets import PresetStore
    with operational_errors():
        click.echo(PresetStore(settings.config_dir).load(name).to_yaml(), nl=False)


@preset_commands.command('plan')
@click.argument('name', type=NAME)
@click.pass_obj
def plan_preset(settings: Settings, name: str):
    """Preview current matches and targets without moving windows or creating state."""
    from spacekeeper.presets import PresetStore
    with operational_errors():
        preset = PresetStore(settings.config_dir).load(name)
        snapshot = desktop(settings).observe(deadline=time.monotonic() + settings.timeout)
        plan = preset.plan(snapshot)
        for decision in plan.windows:
            if decision.target is not None:
                display = next(d.index for d in snapshot.displays if d.uuid == decision.target.display_uuid)
                click.echo(f'Window {decision.window_id}: {decision.status.value}; monitor {display}; '
                           f'fullscreen {str(decision.target.fullscreen).lower()}.')


@preset_commands.command('apply')
@click.argument('name', type=NAME)
@click.pass_context
def apply_preset(ctx: click.Context, name: str):
    """Apply rules to all matching windows; leave unmatched placements unchanged.

    Uses a verified pre-restore backup, ownership guards, and final verification.
    The last matched window per monitor becomes visible; the last rule's last
    matched window receives final focus. No missing applications are launched.
    """
    from spacekeeper.presets import PresetStore
    settings = ctx.obj
    with operational_errors():
        preset = PresetStore(settings.config_dir).load(name)
        coordinator = RestoreCoordinator(desktop(settings), SnapshotStore(settings.state_dir), timeout=settings.timeout,
                                         on_backup=lambda path: click.echo(f'Pre-restore backup: {path}'))
        emit_report(ctx, coordinator.apply_preset(preset))
