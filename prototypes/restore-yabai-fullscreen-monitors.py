#!/usr/bin/env python3
import json
import subprocess
import sys
import time
from pathlib import Path

if len(sys.argv) != 2 or sys.argv[1] in {'-h', '--help'}:
    print(f'Usage: {Path(sys.argv[0]).name} SNAPSHOT.json')
    raise SystemExit(0 if len(sys.argv) == 2 else 2)

snapshot_path = Path(sys.argv[1]).expanduser().resolve()
root = snapshot_path.parent
saved = json.loads(snapshot_path.read_text())
actions = []

def query(kind):
    return json.loads(subprocess.check_output(['yabai', '-m', 'query', '--' + kind], text=True, timeout=10))

def command(*args):
    result = subprocess.run(['yabai', '-m', *map(str, args)], text=True, capture_output=True, timeout=15)
    actions.append({'args': args, 'code': result.returncode, 'message': (result.stdout + result.stderr).strip()})
    print(args, result.returncode, (result.stdout + result.stderr).strip(), flush=True)
    return result.returncode == 0

def get_window(old):
    return next((w for w in query('windows') if (w['id'], w['pid'], w['app']) == (old['id'], old['pid'], old['app'])), None)

def wait(old, predicate):
    end = time.monotonic() + 12
    while time.monotonic() < end:
        current = get_window(old)
        if current and predicate(current):
            time.sleep(.8)
            current = get_window(old)
            if current and predicate(current):
                return current
        time.sleep(.3)
    return None

before = {k: query(k) for k in ['displays', 'spaces', 'windows']}
stamp = time.strftime('%Y-%m-%d_%H-%M-%S')
backup = root / f'yabai-before-fullscreen-restore-{stamp}.json'
with backup.open('x') as f:
    json.dump(before, f, indent=2)
backup.chmod(0o600)
print('BACKUP', backup, flush=True)
old_displays = {d['index']: d['uuid'] for d in saved['displays']}
live_displays = {d['uuid']: d['index'] for d in before['displays']}

for old in saved['windows']:
    if not old['is-native-fullscreen']:
        continue
    current = get_window(old)
    target_display = live_displays.get(old_displays[old['display']])
    if not current or target_display is None:
        continue
    if current['is-native-fullscreen'] and current['display'] == target_display:
        continue
    print('RESTORE', old['id'], old['title'], 'to display', target_display, flush=True)
    if current['is-native-fullscreen']:
        if not command('window', '--focus', old['id']):
            continue
        wait(old, lambda w: w['has-focus'])
        if not command('window', old['id'], '--toggle', 'native-fullscreen'):
            continue
        current = wait(old, lambda w: not w['is-native-fullscreen'])
        if not current:
            print('STOP: fullscreen exit not confirmed', old['id'], flush=True)
            continue
    if current['display'] != target_display:
        # Choose a normal Space on the original monitor; fullscreen Spaces reject window insertion.
        spaces = query('spaces')
        destinations = sorted((s for s in spaces if s['display'] == target_display and not s['is-native-fullscreen']), key=lambda s: s['index'])
        if not destinations:
            print('STOP: no normal destination Space', old['id'], flush=True)
            continue
        target_id = destinations[0]['id']
        target = next(s for s in query('spaces') if s['id'] == target_id)
        command('window', old['id'], '--space', target['index'])
        current = wait(old, lambda w: w['display'] == target_display and not w['is-native-fullscreen'])
        if not current:
            print('STOP: monitor move not confirmed', old['id'], flush=True)
            # Return to its prior fullscreen state if moving was blocked.
            current = get_window(old)
            if current and not current['is-native-fullscreen']:
                command('window', '--focus', old['id'])
                command('window', old['id'], '--toggle', 'native-fullscreen')
                wait(old, lambda w: w['is-native-fullscreen'])
            continue
    if not command('window', '--focus', old['id']):
        continue
    current = wait(old, lambda w: w['has-focus'] and w['display'] == target_display)
    if not current:
        print('STOP: focus on original monitor not confirmed', old['id'], flush=True)
        continue
    command('window', old['id'], '--toggle', 'native-fullscreen')
    current = wait(old, lambda w: w['is-native-fullscreen'] and w['display'] == target_display)
    print('VERIFIED' if current else 'NOT VERIFIED', old['id'], flush=True)

# Recover the saved visible windows and final focus after mutations.
for oldspace in [s for s in saved['spaces'] if s['is-visible']]:
    owners = [w for w in saved['windows'] if w['space'] == oldspace['index'] and not w['is-sticky'] and not w['is-minimized']]
    for owner in owners:
        if get_window(owner):
            command('window', '--focus', owner['id'])
            time.sleep(.8)
            break
for old in saved['windows']:
    if old['has-focus'] and not old['is-sticky'] and get_window(old):
        command('window', '--focus', old['id'])
time.sleep(1)
after = {k: query(k) for k in ['displays', 'spaces', 'windows']}
remaining = []
for old in [w for w in saved['windows'] if w['is-native-fullscreen']]:
    current = next((w for w in after['windows'] if (w['id'], w['pid'], w['app']) == (old['id'], old['pid'], old['app'])), None)
    target = live_displays.get(old_displays[old['display']])
    if not current or current['display'] != target or not current['is-native-fullscreen']:
        remaining.append({'id': old['id'], 'title': old['title'], 'target_display': target, 'current_display': current['display'] if current else None, 'fullscreen': current['is-native-fullscreen'] if current else None})
report = root / f'yabai-fullscreen-restore-result-{stamp}.json'
with report.open('x') as f:
    json.dump({'actions': actions, 'remaining_fullscreen_or_monitor_differences': remaining, 'after': after}, f, indent=2)
report.chmod(0o600)
print('REPORT', report, flush=True)
print('REMAINING', json.dumps(remaining), flush=True)
