"""Human-readable CLI; structured restore reports are saved privately on disk."""
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
import time

import click

from spacekeeper.config import ConfigError, Settings
from spacekeeper.identity import IdentityUnavailable
from spacekeeper.restore import RestoreCoordinator, RestoreError
from spacekeeper.storage import SnapshotStore, StorageError, validate_name
from spacekeeper.yabai import Yabai, YabaiError


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
def operational_errors():
    try:
        yield
    except (StorageError, YabaiError, IdentityUnavailable, RestoreError, ConfigError) as error:
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
              help='Per-transition/observation timeout in seconds (default: 12).')
@click.pass_context
def main(ctx: click.Context, state_dir, config_dir, yabai, timeout) -> None:
    """Capture and restore macOS fullscreen arrangements in the current session."""
    with operational_errors():
        ctx.obj = Settings.load(state_dir=state_dir, config_dir=config_dir, yabai=yabai, timeout=timeout)


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
@click.pass_context
def restore(ctx: click.Context, name: str | None, backup_id: str | None) -> None:
    """Restore NAME (default: default) in the same running session.

    Extra fullscreen windows absent from the snapshot exit fullscreen.
    Extra ordinary windows stay open and unchanged. A verified backup is
    saved before mutations. Incomplete restores exit with code 1.
    """
    if backup_id is not None and name is not None:
        raise click.UsageError('Choose a snapshot name or --backup, not both.')
    settings = ctx.obj
    with operational_errors():
        store = SnapshotStore(settings.state_dir)
        saved = store.load_backup(backup_id) if backup_id is not None else store.load(name or 'default')
        click.echo('Extra fullscreen windows absent from this snapshot will exit fullscreen.')
        coordinator = RestoreCoordinator(desktop(settings), store, timeout=settings.timeout,
                                         on_backup=lambda path: click.echo(f'Pre-restore backup: {path}'))
        report = coordinator.restore(saved)
        counts = Counter(window.status for window in report.windows)
        summary = ', '.join(f'{count} {status}' for status, count in sorted(counts.items())) or 'no windows'
        click.echo(('Restore complete: ' if report.complete else 'Restore incomplete: ') + summary)
        for window in report.windows:
            if window.monitor_fallback:
                click.echo(f'Window {window.window_id}: saved monitor disconnected; using its current monitor.')
            if window.status not in ('restored', 'unchanged', 'excluded'):
                click.echo(f'Window {window.window_id}: {window.status} ({window.reason}).')
        for issue in report.issues:
            click.echo(f'{issue.scope}: {issue.reason}' + (f' (window {issue.window_id})' if issue.window_id is not None else ''))
        click.echo(f'Report: {report.report_path}')
        if not report.complete:
            ctx.exit(1)
