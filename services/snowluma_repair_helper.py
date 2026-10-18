#!/usr/bin/env python3
"""Single-account profile transaction. Runs inside the shared QQ container.

No implicit logout, backup pruning or shared-container lifecycle operations.
The Manager verifies the login surface before committing the transaction.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
from uuid import uuid4

ROOT = Path('/app/data/login-repair')


def supervisor(*args):
    result = subprocess.run(['supervisorctl', *args], capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError('supervisor %s failed' % args[0])
    return result.stdout


def snapshot():
    # supervisorctl status returns nonzero when *any* program is stopped.
    result = subprocess.run(['supervisorctl', 'status'], capture_output=True, text=True, timeout=30)
    rows = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            rows[parts[0]] = [parts[1], parts[3].rstrip(',') if len(parts) > 3 and parts[2] == 'pid' else '']
    if not rows:
        raise RuntimeError('supervisor status unavailable')
    return rows


def resolve(program, expected_uin):
    import runpy
    mapping = runpy.run_path('/tmp/dump-qr.py')['ACCOUNTS']
    if not re.fullmatch(r'qq(?:-[A-Za-z0-9_-]+)?', program) or not expected_uin.isdigit():
        raise ValueError('invalid account')
    img, raw = mapping[program]
    home = Path(raw)
    # Explicitly allow the original default account HOME, but no other app paths.
    if not (raw == '/app' and program == 'qq' and img == 'app'):
        if not re.fullmatch(r'/app/qq-[0-9]+', raw) or home.name != 'qq-' + expected_uin:
            raise ValueError('account HOME does not match configured uin')
    if home.resolve() != home or not home.is_dir():
        raise ValueError('unsafe account HOME')
    cfg = home / '.config'
    if cfg.is_symlink() or cfg.resolve() != cfg:
        raise ValueError('unsafe config directory')
    profile = cfg / 'QQ'
    if profile.is_symlink():
        raise ValueError('unsafe profile')
    return home, profile


def assert_no_clients(home):
    # Fail closed rather than killing arbitrary descendants or guessing ownership.
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / 'cmdline').read_bytes().split(b'\0')
            env = (entry / 'environ').read_bytes().split(b'\0')
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            raise RuntimeError('cannot verify account processes')
        if args and Path(os.fsdecode(args[0])).name == 'qq' and b'HOME=' + os.fsencode(home) in env:
            raise RuntimeError('account QQ process still alive; profile unchanged')


def stop(program, home):
    state = snapshot().get(program, ['UNKNOWN'])[0]
    if state != 'STOPPED':
        supervisor('stop', program)
    if snapshot().get(program, ['UNKNOWN'])[0] != 'STOPPED':
        raise RuntimeError('account did not stop')
    assert_no_clients(home)


def save(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def begin(program, uin):
    home, profile = resolve(program, uin)
    before = snapshot()
    if before.get(program, [''])[0] != 'RUNNING':
        raise RuntimeError('repair requires a running account')
    if not profile.is_dir():
        raise RuntimeError('profile missing')
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    if ROOT.is_symlink() or ROOT.resolve() != ROOT:
        raise ValueError('unsafe backup directory')
    # Bind mount is necessary: backups must survive container replacement.
    if os.stat(ROOT).st_dev == os.stat('/').st_dev:
        raise RuntimeError('backup directory is not on persistent storage')
    if os.stat(profile).st_dev != os.stat(ROOT).st_dev:
        raise RuntimeError('profile and backup must share a filesystem for atomic rename')
    if os.statvfs(ROOT).f_bavail * os.statvfs(ROOT).f_frsize < 64 * 1024 * 1024:
        raise RuntimeError('insufficient space for a fresh login profile')
    transaction = ROOT / (time.strftime('%Y%m%d-%H%M%S-') + uuid4().hex)
    transaction.mkdir(mode=0o700)
    data = {'program': program, 'uin': uin, 'home': str(home), 'profile': str(profile),
            'before': before, 'phase': 'preparing', 'created_at': time.time()}
    manifest = transaction / 'manifest.json'
    save(manifest, data)
    try:
        stop(program, home)
        # Revalidate after stopping, before touching persistent state.
        resolve(program, uin)
        profile.rename(transaction / 'original')
        data['phase'] = 'profile_saved'
        save(manifest, data)
        supervisor('start', program)
        data['phase'] = 'verifying'
        save(manifest, data)
    except Exception as exc:
        try:
            rollback(transaction, program, uin)
        except Exception as rollback_error:
            raise RuntimeError('repair failed; rollback failed; transaction=%s: %s' % (transaction, rollback_error)) from exc
        raise RuntimeError('repair failed; original profile restored: %s' % transaction) from exc
    return {'transaction': transaction.name, 'backup': str(transaction / 'original'), 'phase': 'verifying'}


def load(transaction, program, uin):
    if transaction.parent != ROOT or transaction.is_symlink() or transaction.resolve() != transaction:
        raise ValueError('unsafe transaction path')
    data = json.loads((transaction / 'manifest.json').read_text())
    home, profile = resolve(program, uin)
    if data['program'] != program or data['uin'] != uin or data['profile'] != str(profile):
        raise ValueError('transaction account mismatch')
    return data, home, profile


def rollback(transaction, program, uin):
    data, home, profile = load(transaction, program, uin)
    original = transaction / 'original'
    if data['phase'] in ('committed', 'rolled_back'):
        raise RuntimeError('transaction is already final')
    if original.exists():
        stop(program, home)
        if profile.exists():
            profile.rename(transaction / ('failed-' + uuid4().hex))
        original.rename(profile)
    if snapshot().get(program, [''])[0] != 'RUNNING':
        supervisor('start', program)
    data['phase'] = 'rolled_back'
    save(transaction / 'manifest.json', data)
    return {'phase': 'rolled_back', 'transaction': transaction.name}


def finish(transaction, program, uin):
    data, _home, _profile = load(transaction, program, uin)
    if data['phase'] != 'verifying':
        raise RuntimeError('transaction not awaiting verification')
    after = snapshot()
    changed = [p for p, state in data['before'].items() if p != program and after.get(p) != state]
    data.update(phase='committed', completed_at=time.time(), other_program_changes=changed)
    save(transaction / 'manifest.json', data)
    return {'phase': 'committed', 'backup': str(transaction / 'original'), 'other_program_changes': changed}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['begin', 'rollback', 'finish'])
    parser.add_argument('program')
    parser.add_argument('uin')
    parser.add_argument('transaction', nargs='?')
    args = parser.parse_args()
    if args.mode == 'begin':
        result = begin(args.program, args.uin)
    else:
        token = args.transaction or ''
        if not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[a-f0-9]{32}', token):
            raise ValueError('invalid transaction')
        result = globals()[args.mode](ROOT / token, args.program, args.uin)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
