"""App-specific title rules for stale-session snapshot matching.

Rules are pure functions receiving a case-folded title. Add a function and an
entry in APP_TITLE_RULES to support another app; the planner stays unchanged.
App names remain exact (case-insensitive), and the planner owns uniqueness and
process identity checks. Preset and preserve glob selectors do not use these rules.
"""

from collections.abc import Callable

from spacekeeper.snapshot import Window


def code_workspace(title: str) -> str:
    """Ignore the active file while distinguishing extension development hosts."""
    host = '[extension development host] '
    prefix = host if title.startswith(host) else ''
    workspace = title.removeprefix(prefix).rsplit(' — ', 1)[-1].strip()
    return prefix + workspace if workspace else title


# Keys are case-folded app names. Unregistered apps keep literal full titles.
APP_TITLE_RULES: dict[str, Callable[[str], str]] = {
    'code': code_workspace,
    'code - oss': code_workspace,
    'code - insiders': code_workspace,
}


def restore_name_key(window: Window) -> tuple[str, str]:
    """Return comparable app/title names without selecting or trusting a window."""
    app, title = window.app.casefold(), window.title.casefold()
    rule = APP_TITLE_RULES.get(app)
    return app, rule(title) if rule is not None else title
