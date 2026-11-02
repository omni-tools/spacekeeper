#!/usr/bin/env python3
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from pathlib import Path

if len(sys.argv) != 2 or sys.argv[1] in {'-h', '--help'}:
    print(f'Usage: {Path(sys.argv[0]).name} SNAPSHOT.json')
    raise SystemExit(0 if len(sys.argv) == 2 else 2)

SNAPSHOT = Path(sys.argv[1]).expanduser().resolve()
saved = json.loads(SNAPSHOT.read_text())
events = []
event_lock = Lock()

def query(domain):
    return json.loads(subprocess.check_output(['yabai', '-m', 'query', '--' + domain], text=True, timeout=10))

def query_all():
    domains = ['displays', 'spaces', 'windows']
    with ThreadPoolExecutor(max_workers=3) as pool:
        return dict(zip(domains, pool.map(query, domains)))

def run(*args):
    result = subprocess.run(['yabai', '-m', *map(str, args)], capture_output=True, text=True, timeout=15)
    with event_lock:
        events.append({'args': args, 'returncode': result.returncode, 'output': (result.stdout + result.stderr).strip()})
        print(' '.join(map(str, args)), 'OK' if result.returncode == 0 else (result.stdout + result.stderr).strip(), flush=True)
    return result.returncode == 0

def window(wanted):
    current = next((w for w in query('windows') if w['id'] == wanted['id']), None)
    if current and (current['pid'], current['app']) == (wanted['pid'], wanted['app']):
        return current
    return None

def wait_fullscreen(wanted):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        current = window(wanted)
        if current and current['is-native-fullscreen'] == wanted['is-native-fullscreen']:
            time.sleep(1)
            return True
        time.sleep(.3)
    return False

def wait_window(wanted, predicate):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        current = window(wanted)
        if current and predicate(current):
            time.sleep(.8)
            current = window(wanted)
            if current and predicate(current):
                return current
        time.sleep(.3)
    return None

def restore_fullscreen(wanted, target_display):
    current = window(wanted)
    if not current:
        return False
    if current['is-native-fullscreen'] and current['display'] == target_display:
        return True
    if current['is-native-fullscreen']:
        if not run('window', '--focus', wanted['id']) or not wait_window(wanted, lambda w: w['has-focus']):
            return False
        if not run('window', wanted['id'], '--toggle', 'native-fullscreen'):
            return False
        current = wait_window(wanted, lambda w: not w['is-native-fullscreen'])
        if not current:
            return False
    if current['display'] != target_display:
        candidates = sorted((s for s in query('spaces') if s['display'] == target_display and not s['is-native-fullscreen']), key=lambda s: s['index'])
        if not candidates:
            return False
        destination_id = candidates[0]['id']
        destination = next((s for s in query('spaces') if s['id'] == destination_id and not s['is-native-fullscreen']), None)
        if not destination or not run('window', wanted['id'], '--space', destination['index']):
            return False
        if not wait_window(wanted, lambda w: w['display'] == target_display and not w['is-native-fullscreen']):
            return False
    if not run('window', '--focus', wanted['id']):
        return False
    if not wait_window(wanted, lambda w: w['has-focus'] and w['display'] == target_display):
        return False
    if not run('window', wanted['id'], '--toggle', 'native-fullscreen'):
        return False
    return bool(wait_window(wanted, lambda w: w['is-native-fullscreen'] and w['display'] == target_display))

backup = query_all()
displays = backup['displays']
if {d['uuid']: d['frame'] for d in displays} != {d['uuid']: d['frame'] for d in saved['displays']}:
    raise SystemExit('Monitor arrangement changed; refusing to apply saved coordinates.')
backup_path = SNAPSHOT.parent / ('yabai-before-restore-' + time.strftime('%Y-%m-%d_%H-%M-%S') + '.json')
with backup_path.open('x') as f:
    json.dump(backup, f, indent=2)
backup_path.chmod(0o600)
print('BACKUP', backup_path, flush=True)
saved_display_uuids = {d['index']: d['uuid'] for d in saved['displays']}
live_display_indexes = {d['uuid']: d['index'] for d in displays}

# Restore window states before attempting Space ordering; fullscreen creates/removes Spaces.
frame_jobs = []
for wanted in saved['windows']:
    current = window(wanted)
    if not current or current['is-sticky'] or not wanted.get('root-window'):
        continue
    if wanted['is-native-fullscreen']:
        target_display = live_display_indexes[saved_display_uuids[wanted['display']]]
        if not restore_fullscreen(wanted, target_display):
            print('FULLSCREEN/MONITOR NOT CONFIRMED', wanted['id'], flush=True)
        continue
    if current['is-native-fullscreen'] != wanted['is-native-fullscreen']:
        if not run('window', '--focus', wanted['id']):
            continue
        time.sleep(.4)
        if not run('window', wanted['id'], '--toggle', 'native-fullscreen') or not wait_fullscreen(wanted):
            print('FULLSCREEN NOT CONFIRMED', wanted['id'], flush=True)
            continue
    current = window(wanted)
    if not current or current['is-native-fullscreen'] or wanted['is-native-fullscreen']:
        continue
    oldspace = next((s for s in saved['spaces'] if s['index'] == wanted['space']), None)
    live_spaces = query('spaces')
    target = next((s for s in live_spaces if oldspace and s['uuid'] == oldspace['uuid']), None)
    current_space = next((s for s in live_spaces if s['index'] == current['space']), None)
    if target and current_space and target['id'] != current_space['id'] and not target['is-native-fullscreen']:
        run('window', wanted['id'], '--space', target['index'])
    if current['frame'] != wanted['frame']:
        frame_jobs.append(wanted)

def restore_frame(wanted):
    # Recheck ownership/state in each worker. Only coordinate/size commands run here;
    # focus, fullscreen, and Space mutations remain serialized above/below.
    current = window(wanted)
    if not current or current['is-native-fullscreen'] or current['is-sticky']:
        return
    frame = wanted['frame']
    if current.get('can-move') and any(abs(current['frame'][k] - frame[k]) > 1 for k in ['x', 'y']):
        if not run('window', wanted['id'], '--move', f'abs:{frame["x"]}:{frame["y"]}'):
            return
    if current.get('can-resize') and any(abs(current['frame'][k] - frame[k]) > 1 for k in ['w', 'h']):
        run('window', wanted['id'], '--resize', f'abs:{frame["w"]}:{frame["h"]}')

# Keep a window's move/resize ordered while restoring independent windows concurrently.
with ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(restore_frame, frame_jobs))

def match_space(wanted, current_spaces, current_windows):
    exact = next((s for s in current_spaces if s['uuid'] == wanted['uuid']), None)
    if exact:
        return exact
    if wanted['is-native-fullscreen']:
        owners = {w['id'] for w in saved['windows'] if w['space'] == wanted['index'] and w['is-native-fullscreen']}
        live_owners = {w['id'] for w in current_windows if w['id'] in owners and w['is-native-fullscreen']}
        candidates = [s for s in current_spaces if s['is-native-fullscreen'] and live_owners.intersection(s['windows'])]
        return candidates[0] if len(candidates) == 1 else None

# Re-resolve UUIDs (or fullscreen owner identity) to indexes before every mutation.
ordering_blocked = False
for wanted in saved['spaces']:
    spaces, windows = query('spaces'), query('windows')
    actual = match_space(wanted, spaces, windows)
    if actual and actual['display'] != wanted['display']:
        if not run('space', actual['index'], '--display', wanted['display']):
            ordering_blocked = True
            break
        time.sleep(.6)
for display in saved['displays']:
    if ordering_blocked:
        break
    targets = [s for s in saved['spaces'] if s['display'] == display['index']]
    for position, wanted in enumerate(targets):
        spaces, windows = query('spaces'), query('windows')
        actual = match_space(wanted, spaces, windows)
        same_display = sorted([s for s in spaces if s['display'] == display['index']], key=lambda s: s['index'])
        if not actual or position >= len(same_display):
            continue
        destination = same_display[position]
        if actual['id'] != destination['id']:
            if not run('space', actual['index'], '--move', destination['index']):
                ordering_blocked = True
                break
    if ordering_blocked:
        break

# Restore the visible Space on each display by focusing a surviving window in it.
for wanted_space in [s for s in saved['spaces'] if s['is-visible']]:
    owners = [w for w in saved['windows'] if w['space'] == wanted_space['index'] and not w['is-sticky'] and not w['is-minimized']]
    for owner in owners:
        if window(owner):
            run('window', '--focus', owner['id'])
            time.sleep(.6)
            break
for focused in [w for w in saved['windows'] if w['has-focus'] and not w['is-sticky']]:
    if window(focused):
        run('window', '--focus', focused['id'])

after = query_all()
remaining = []
for wanted in saved['windows']:
    current = next((w for w in after['windows'] if w['id'] == wanted['id'] and w['pid'] == wanted['pid'] and w['app'] == wanted['app']), None)
    if not current:
        remaining.append({'window': wanted['id'], 'issue': 'missing or changed owner'})
    elif current['is-native-fullscreen'] != wanted['is-native-fullscreen']:
        remaining.append({'window': wanted['id'], 'issue': 'fullscreen state'})
    elif current['display'] != live_display_indexes[saved_display_uuids[wanted['display']]] and not wanted['is-sticky']:
        remaining.append({'window': wanted['id'], 'issue': 'monitor', 'expected_uuid': saved_display_uuids[wanted['display']], 'actual_index': current['display']})
    elif not wanted['is-native-fullscreen'] and not wanted['is-sticky'] and current['frame'] != wanted['frame']:
        remaining.append({'window': wanted['id'], 'issue': 'frame', 'expected': wanted['frame'], 'actual': current['frame']})
for display in saved['displays']:
    desired = [match_space(s, after['spaces'], after['windows']) for s in saved['spaces'] if s['display'] == display['index']]
    actual = sorted([s for s in after['spaces'] if s['display'] == display['index']], key=lambda s: s['index'])
    if [s['id'] if s else None for s in desired] != [s['id'] for s in actual]:
        remaining.append({'display': display['index'], 'issue': 'Space ordering'})
report = {'source_snapshot': str(SNAPSHOT), 'actions': events, 'remaining': remaining, 'after': after}
path = SNAPSHOT.parent / ('yabai-restore-result-' + time.strftime('%Y-%m-%d_%H-%M-%S') + '.json')
with path.open('x') as f:
    json.dump(report, f, indent=2)
path.chmod(0o600)
print('REPORT', path)
print('REMAINING', json.dumps(remaining))
