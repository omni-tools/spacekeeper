"""Transient terminal feedback; importing the CLI does not load Rich."""

from contextlib import contextmanager, suppress
import os
import sys

from spacekeeper.restore import RestoreProgress


def progress_text(name: str, progress: RestoreProgress):
    from rich.text import Text

    text = Text(no_wrap=True, overflow='ellipsis')
    text.append(progress.phase + ' ')
    text.append(name, style='bold blue')
    if progress.windows_total is not None:
        text.append(f' · windows {progress.windows_done}/{progress.windows_total}'
                    f' · fullscreen {progress.fullscreen_done}/{progress.fullscreen_total}'
                    f' · minimized {progress.minimized_done}/{progress.minimized_total}', style='dim')
    text.append(f' · ⚠️ {progress.errors}', style='yellow' if progress.errors else 'dim')
    return text


@contextmanager
def restore_spinner(name: str):
    """Yield a progress callback only when both output streams are interactive."""
    if not (sys.stdout.isatty() and sys.stderr.isatty()) or os.environ.get('TERM') == 'dumb':
        yield None
        return
    from rich.console import Console, Group

    # Spinner concatenates plain Text and loses its no_wrap setting. A Group
    # preserves the text renderable, keeping narrow terminals to one line.
    status = Console(file=sys.stderr, highlight=False).status(
        Group(progress_text(name, RestoreProgress())), spinner='dots', spinner_style='dim', refresh_per_second=10)
    try:
        status.start()
    except Exception:
        # Feedback must not prevent the requested restore from running.
        with suppress(Exception):
            status.stop()
        yield None
        return
    try:
        yield lambda progress: status.update(Group(progress_text(name, progress)))
    finally:
        with suppress(Exception):
            status.stop()
