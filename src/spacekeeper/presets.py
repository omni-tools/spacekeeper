"""Declarative YAML layouts, deterministic matching, and private named storage."""

from dataclasses import dataclass
from fnmatch import fnmatchcase
from glob import escape
import os
from pathlib import Path
import stat
from uuid import UUID

import yaml

from spacekeeper.config import ConfigError
from spacekeeper.matching import WindowRule
from spacekeeper.planner import (DecisionStatus, Reason, RestorePlan, VisibleTarget, WindowDecision,
                                 WindowTarget, unsupported_reason)
from spacekeeper.snapshot import Snapshot
from spacekeeper.storage import SnapshotStore, StorageError, StorageFailure, validate_name


class PresetError(ConfigError):
    pass


class PresetLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise PresetError('YAML aliases are not supported in presets.')
        if len(self.states) > 32:
            raise PresetError('Preset nesting is too deep.')
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if type(key) is not str or key in result:
                raise PresetError('Preset mappings require unique string keys.')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def unique_name(selector: str, names: list[str], label: str) -> str:
    exact = [name for name in names if name.casefold() == selector.casefold()]
    if len(exact) == 1:
        return exact[0]
    def normalized(value):
        return ''.join(c for c in value.casefold() if c.isalnum())
    key = normalized(selector)
    matches = [name for name in names if len(key) >= 3 and key in normalized(name)]
    if len(matches) != 1:
        raise PresetError(f'{label} is ambiguous or has no match; use a more specific name.')
    return matches[0]


@dataclass(frozen=True)
class PresetRule:
    selector: WindowRule
    device: str
    fullscreen: bool = True


@dataclass(frozen=True)
class Preset:
    devices: dict[str, str]
    windows: tuple[PresetRule, ...]

    @classmethod
    def from_yaml(cls, source: str) -> 'Preset':
        if len(source.encode('utf-8')) > 65536:
            raise PresetError('Preset exceeds 64 KiB.')
        try:
            data = yaml.load(source, Loader=PresetLoader)
            if type(data) is not dict or data.keys() - {'version', 'devices', 'windows'}:
                raise PresetError('Preset accepts version, devices, and windows.')
            if type(data.get('version', 1)) is not int or data.get('version', 1) != 1:
                raise PresetError('Unsupported preset version.')
            devices = data.get('devices', {})
            if type(devices) is not dict or len(devices) > 64:
                raise PresetError('Devices must be a mapping of at most 64 aliases to monitor UUIDs.')
            aliases = {}
            for alias, value in devices.items():
                if (type(alias) is not str or not alias.strip() or len(alias) > 64 or '\0' in alias or
                        alias.casefold() == 'primary' or alias.casefold() in aliases):
                    raise PresetError('Device aliases must be unique, nonempty, and cannot replace primary.')
                if type(value) is not str:
                    raise PresetError('Device aliases must refer to monitor UUID strings.')
                aliases[alias.casefold()] = str(UUID(value))
            records = data.get('windows')
            if type(records) is not list or not 1 <= len(records) <= 256:
                raise PresetError('Windows must contain between 1 and 256 rules.')
            rules = []
            for item in records:
                if (type(item) is not dict or not {'app', 'device'} <= item.keys() or
                        item.keys() - {'app', 'title', 'device', 'fullscreen'}):
                    raise PresetError('Each window rule needs app and device; title and fullscreen are optional.')
                device = item['device']
                if type(device) is not str or not device.strip() or len(device) > 128 or '\0' in device:
                    raise PresetError('A window device must be a nonempty alias or UUID.')
                fullscreen = item.get('fullscreen', True)
                if type(fullscreen) is not bool:
                    raise PresetError('Window fullscreen must be a boolean.')
                rules.append(PresetRule(WindowRule(item['app'], item.get('title', '*')), device, fullscreen))
            return cls(aliases, tuple(rules))
        except PresetError:
            raise
        except (yaml.YAMLError, ValueError, TypeError, RecursionError) as error:
            raise PresetError('Invalid preset YAML or rule data.') from error

    def to_yaml(self) -> str:
        return yaml.safe_dump({'version': 1, 'devices': self.devices,
                               'windows': [{'app': r.selector.app, 'title': r.selector.title,
                                            'device': r.device, 'fullscreen': r.fullscreen} for r in self.windows]},
                              sort_keys=False, allow_unicode=True)

    def plan(self, current: Snapshot) -> RestorePlan:
        connected = {d.uuid for d in current.displays}
        aliases = dict(self.devices)
        primary = next((d for d in current.displays if d.index == 1), None)
        if primary is not None:
            aliases['primary'] = primary.uuid
        apps = sorted({w.app for w in current.windows})
        targets = {}
        visible = {}
        focus = None
        for index, rule in enumerate(self.windows, start=1):
            try:
                device = str(UUID(rule.device))
            except ValueError:
                device = aliases[unique_name(rule.device, list(aliases), f'Rule {index} device')]
            if device not in connected:
                raise PresetError(f'Rule {index} monitor is disconnected.')
            pattern = rule.selector.app
            if any(char in pattern for char in '*?['):
                selected_apps = {app for app in apps if fnmatchcase(app.casefold(), pattern.casefold())}
            else:
                selected_apps = {unique_name(pattern, apps, f'Rule {index} app')}
            matches = [w for w in current.windows if w.app in selected_apps and
                       fnmatchcase(w.title.casefold(), rule.selector.title.casefold())]
            if not matches:
                raise PresetError(f'Rule {index} matches no windows.')
            for window in sorted(matches, key=lambda w: w.id):
                unsupported = unsupported_reason(window)
                if unsupported is not None:
                    raise PresetError(f'Rule {index} matches an unsupported window ({unsupported.value}).')
                target = WindowTarget(window.identity, device, rule.fullscreen)
                if window.id in targets and targets[window.id] != target:
                    raise PresetError(f'Rule {index} conflicts with an earlier rule.')
                if (window.display_uuid != device and
                        not any(s.display_uuid == device and not s.fullscreen for s in current.spaces)):
                    raise PresetError(f'Rule {index} destination has no normal Space.')
                targets[window.id] = target
                visible[device] = VisibleTarget(device, window.identity)
                focus = window.identity
        decisions = []
        for window in sorted(current.windows, key=lambda w: w.id):
            target = targets.get(window.id)
            if target is None:
                decisions.append(WindowDecision(window.id, DecisionStatus.EXCLUDED, Reason.PRESET_UNMATCHED))
            else:
                matches = (window.fullscreen, window.display_uuid) == (target.fullscreen, target.display_uuid)
                decisions.append(WindowDecision(window.id, DecisionStatus.UNCHANGED if matches else DecisionStatus.PLANNED,
                                                Reason.ALREADY_MATCHES if matches else Reason.RESTORE_SAVED, target))
        return RestorePlan(True, tuple(decisions), tuple(visible.values()), focus)

    @classmethod
    def capture(cls, snapshot: Snapshot) -> 'Preset':
        devices = {f'display-{d.index}': d.uuid for d in snapshot.displays}
        aliases = {value: key for key, value in devices.items()}
        visible_ids = {s.visible_window_id for s in snapshot.spaces if s.visible}
        windows = sorted((w for w in snapshot.windows if unsupported_reason(w) is None),
                         key=lambda w: (w.focused, w.id in visible_ids, w.id))
        rules = {}
        for window in windows:
            key = (window.app.casefold(), window.title.casefold())
            value = PresetRule(WindowRule(escape(window.app), escape(window.title)),
                               aliases[window.display_uuid], window.fullscreen)
            if key in rules and rules[key] != value:
                raise PresetError('Identical app/title selectors have different targets; capture a narrower layout manually.')
            rules[key] = value
        if not rules:
            raise PresetError('No supported windows are available to capture.')
        preset = cls(devices, tuple(rules.values()))
        preset.plan(snapshot)  # Captured selectors must reproduce their own targets.
        return preset


class PresetStore(SnapshotStore):
    """Reuse atomic private-file writes, with YAML-specific reads and validation."""
    def __init__(self, config_dir: Path):
        super().__init__(config_dir, size_limit=65536)

    def load(self, name: str) -> Preset:
        path = self._directory('presets') / (validate_name(name) + '.yaml')
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise PresetError('Preset must be a regular YAML file.')
                data = stream.read(65537)
            return Preset.from_yaml(data.decode('utf-8'))
        except (OSError, UnicodeDecodeError) as error:
            raise PresetError('Cannot read the named preset YAML file.') from error

    def save(self, preset: Preset, name: str, *, force: bool = False) -> Path:
        name = validate_name(name)
        data = preset.to_yaml()
        Preset.from_yaml(data)
        try:
            return self._write(self._directory('presets', create=True) / (name + '.yaml'),
                               data.encode('utf-8'), replace=force)
        except StorageError as error:
            if error.reason == StorageFailure.EXISTS:
                raise PresetError('Preset already exists; use --force to replace it.') from error
            raise

    def entries(self) -> tuple[str, ...]:
        directory = self._directory('presets')
        if not directory.exists():
            return ()
        try:
            names = []
            for path in sorted(directory.glob('*.yaml')):
                try:
                    names.append(validate_name(path.stem))
                except StorageError:
                    continue
            return tuple(names)
        except OSError as error:
            raise PresetError('Cannot list preset files.') from error
