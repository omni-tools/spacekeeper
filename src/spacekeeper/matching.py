"""Explicit app/title glob selectors shared by restore filters and presets."""

from dataclasses import dataclass
from fnmatch import fnmatchcase

from spacekeeper.snapshot import Window


@dataclass(frozen=True)
class WindowRule:
    app: str
    title: str = '*'

    def __post_init__(self):
        if type(self.app) is not str or not self.app.strip():
            raise ValueError('The app pattern must be a nonempty string.')
        for value in (self.app, self.title):
            if type(value) is not str or '\0' in value or len(value) > 1024:
                raise ValueError('App and title patterns must be strings of at most 1024 characters.')

    def matches(self, window: Window) -> bool:
        return (fnmatchcase(window.app.casefold(), self.app.casefold()) and
                fnmatchcase(window.title.casefold(), self.title.casefold()))


def window_rules(data) -> tuple[WindowRule, ...]:
    if type(data) is not list or len(data) > 256:
        raise ValueError('Preserve rules must be a list of at most 256 app/title mappings.')
    rules = []
    for item in data:
        if type(item) is not dict or 'app' not in item or item.keys() - {'app', 'title'}:
            raise ValueError('Each preserve rule requires app and accepts an optional title.')
        rules.append(WindowRule(item['app'], item.get('title', '*')))
    return tuple(rules)
