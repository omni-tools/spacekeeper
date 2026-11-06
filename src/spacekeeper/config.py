"""Read-only resolution of CLI/environment overrides, config.json, and defaults."""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import stat

from spacekeeper.matching import WindowRule, window_rules


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    config_dir: Path
    yabai: Path | None
    timeout: float
    preserve: tuple[WindowRule, ...] = ()

    @classmethod
    def load(cls, *, state_dir=None, config_dir=None, yabai=None, timeout=None):
        directory = (config_dir or Path.home() / '.config/spacekeeper').expanduser().resolve()
        try:
            fd = os.open(directory / 'config.json', os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ConfigError('Configuration must be a regular JSON file.')
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ConfigError('Configuration exceeds 64 KiB.')
            def pairs(items):
                if len(dict(items)) != len(items):
                    raise ConfigError('Configuration has duplicate keys.')
                return dict(items)
            data = json.loads(raw, object_pairs_hook=pairs)
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError, UnicodeDecodeError) as error:
            raise ConfigError('Cannot read a valid config.json in the configuration directory.') from error
        if type(data) is not dict or data.keys() - {'state_dir', 'yabai', 'timeout', 'preserve'}:
            raise ConfigError('Configuration accepts only state_dir, yabai, timeout, and preserve.')
        try:
            preserve = window_rules(data.get('preserve', []))
        except ValueError as error:
            raise ConfigError(str(error)) from error
        for key in ('state_dir', 'yabai'):
            if key in data and (type(data[key]) is not str or not data[key].strip() or '\0' in data[key]):
                raise ConfigError(f'Configuration {key} must be a nonempty path string.')
        def configured_path(key):
            value = Path(data[key]).expanduser()
            return value.resolve() if value.is_absolute() else (directory / value).resolve()
        state = state_dir.expanduser().resolve() if state_dir is not None else (
            configured_path('state_dir') if 'state_dir' in data else Path.home() / '.local/state/spacekeeper')
        binary = yabai.expanduser().resolve() if yabai is not None else (
            configured_path('yabai') if 'yabai' in data else None)
        seconds = timeout if timeout is not None else data.get('timeout', 30)
        if type(seconds) not in (int, float) or not 0.1 <= seconds <= 300 or not math.isfinite(seconds):
            raise ConfigError('Timeout must be a finite number between 0.1 and 300 seconds.')
        return cls(state, directory, binary, float(seconds), preserve)
