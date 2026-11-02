#!/usr/bin/env python3
"""Capture a layout observation. Prototype: OUTPUT.json is required."""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def query(domain):
    return json.loads(subprocess.check_output(
        ['yabai', '-m', 'query', '--' + domain], text=True, timeout=10,
    ))


def structure(spaces):
    return sorted((s['id'], s['index'], s['display'], tuple(sorted(s['windows']))) for s in spaces)


if len(sys.argv) != 2 or sys.argv[1] in {'-h', '--help'}:
    print(f'Usage: {Path(sys.argv[0]).name} OUTPUT.json')
    raise SystemExit(0 if len(sys.argv) == 2 else 2)

started = datetime.now().astimezone().isoformat()
for attempt in range(3):
    before = query('spaces')
    displays = query('displays')
    spaces = query('spaces')
    windows = query('windows')
    after = query('spaces')
    if structure(before) == structure(spaces) == structure(after):
        break
else:
    raise SystemExit('Layout changed during capture; no snapshot saved.')

now = datetime.now().astimezone()
data = {
    'schema_version': 1,
    'capture_started_at': started,
    'captured_at': now.isoformat(),
    'yabai_version': subprocess.check_output(['yabai', '--version'], text=True, timeout=10).strip(),
    'notes': [
        'Read-only layout observation, not an atomic macOS snapshot.',
        'Space membership was checked before and after capture.',
        'Window IDs and Space indexes may change; display UUIDs and Space IDs are included.',
    ],
    'displays': displays,
    'spaces': spaces,
    'windows': windows,
}
path = Path(sys.argv[1]).expanduser()
path.parent.mkdir(parents=True, exist_ok=True)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write('\n')
assert json.loads(path.read_text()) == data
print(path.resolve())
print(f'{len(displays)} displays, {len(spaces)} Spaces, {len(windows)} windows')
