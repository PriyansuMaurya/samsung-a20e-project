#!/usr/bin/env python3
"""Fetch the production branch and install validated releases on Termux."""
import fcntl
import os
import re
import sys
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.request

BASE = Path.home() / 'galaxy-server'
REPO = 'git@github.com:PriyansuMaurya/samsung-a20e-project.git'
FILES = ('site', 'status.py', 'serverctl', 'supervise.sh', 'autodeploy.py')


def run(*args, **kwargs):
    return subprocess.check_output(args, timeout=90, stderr=subprocess.STDOUT, **kwargs).decode().strip()


def copy_release(source):
    for name in FILES:
        target = BASE / name
        if name == 'site':
            # Keep the previous assets until the replacement is ready.
            pending = BASE / 'site.pending'
            shutil.rmtree(pending, ignore_errors=True)
            shutil.copytree(source / name, pending)
            old = BASE / 'site.previous'
            shutil.rmtree(old, ignore_errors=True)
            target.rename(old)
            pending.rename(target)
        else:
            pending = BASE / (name + '.pending')
            shutil.copy2(source / name, pending)
            pending.chmod(0o700)
            pending.replace(target)
    shell = BASE / 'priv/console.html.pending'
    shutil.copy2(BASE / 'site/console.html', shell)
    shell.chmod(0o600)
    shell.replace(BASE / 'priv/console.html')


def deploy(revision):
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Expected the commit SHA tested by GitHub Actions')
    if (BASE / 'run/server-disabled').exists():
        return
    checkout = BASE / 'run/deploy-repo'
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0',
               GIT_SSH_COMMAND=f'ssh -i {BASE}/run/deploy-key -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15')
    if not checkout.exists():
        run('git', 'init', str(checkout))
    marker = BASE / 'run/deployed-revision'
    if marker.exists() and marker.read_text().strip() == revision:
        return
    run('git', '-C', str(checkout), 'fetch', '--depth=1', REPO, revision, env=env)
    if run('git', '-C', str(checkout), 'rev-parse', 'FETCH_HEAD') != revision:
        raise RuntimeError('Fetched commit does not match the tested commit')
    run('git', '-C', str(checkout), 'checkout', '--force', '--detach', revision)
    source = checkout / 'galaxy-server'
    for name in FILES:
        if not (source / name).exists():
            raise RuntimeError(f'Release missing {name}')
    compile((source / 'status.py').read_bytes(), 'status.py', 'exec')
    compile((source / 'autodeploy.py').read_bytes(), 'autodeploy.py', 'exec')
    for name in ('serverctl', 'supervise.sh'):
        run('bash', '-n', str(source / name))
    with tempfile.TemporaryDirectory(dir=BASE / 'run', prefix='rollback-') as tmp:
        backup = Path(tmp)
        for name in FILES:
            if (BASE / name).is_dir():
                shutil.copytree(BASE / name, backup / name)
            else:
                shutil.copy2(BASE / name, backup / name)
        try:
            copy_release(source)
            run(str(BASE / 'serverctl'), 'restart')
            for attempt in range(20):
                try:
                    with urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=2) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(1)
            else:
                raise RuntimeError('Release failed health check')
        except Exception:
            copy_release(backup)
            run(str(BASE / 'serverctl'), 'restart')
            raise
    marker.write_text(revision + '\n')
    print(f'{time.strftime("%Y-%m-%dT%H:%M:%S")} deployed {revision}', flush=True)


if __name__ == '__main__':
    BASE.joinpath('run').mkdir(parents=True, exist_ok=True)
    with (BASE / 'run/deploy.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(0)
        try:
            deploy(sys.argv[1] if len(sys.argv) == 2 else '')
        except Exception as error:
            print(f'Deployment failed: {error}', flush=True)
            raise SystemExit(1)
