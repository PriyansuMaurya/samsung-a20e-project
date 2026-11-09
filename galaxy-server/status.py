#!/usr/bin/env python3
"""Status, log and control API for the Galaxy home server.

Binds 127.0.0.1:8765 only. nginx serves the site, enforces HTTP basic auth and
proxies /api/ here, so none of this is reachable without the password. Actions
only ever run whitelisted serverctl subcommands; nothing from the request is
passed to a shell.
"""
import http.server
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

HOME = os.path.expanduser('~')
BASE = os.path.join(HOME, 'galaxy-server')
LOG_DIR = os.path.join(BASE, 'log')
RUN_DIR = os.path.join(BASE, 'run')
SERVERCTL = os.path.join(BASE, 'serverctl')

LOG_FILES = {
    'tunnel': 'tunnel.log',
    'supervisor': 'supervisor.log',
    'nginx-error': 'nginx-error.log',
    'status': 'status.log',
}

ACTIONS = {
    'server-restart': 'restart',
    'server-stop': 'stop',
    'tunnel-start': 'tunnel-start',
    'tunnel-stop': 'tunnel-stop',
    'tunnel-restart': 'tunnel-restart',
}

SERVICES = (
    ('nginx', 'nginx: master'),
    ('status', 'status.py'),
    ('supervisor', 'supervise.sh'),
    ('cloudflared', 'cloudflared tunnel run'),
)


def _run(*cmd, timeout=5):
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (done.stdout or '') + (done.stderr or '')
    except (OSError, subprocess.SubprocessError):
        return ''


def _versions():
    nginx = _run('nginx', '-v').strip().replace('nginx version: ', '')
    cloudflared = ''
    for word in _run('cloudflared', '--version').split():
        if word[:2].isdigit() and '.' in word:
            cloudflared = word
            break
    return {'nginx': nginx or None, 'cloudflared': cloudflared or None}


VERSIONS = _versions()
DEVICE = _run('getprop', 'ro.product.model').strip() or 'Samsung Galaxy A20e'


def scan_processes():
    found = {}
    try:
        entries = os.listdir('/proc')
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open('/proc/%s/cmdline' % entry, 'rb') as handle:
                command = handle.read().replace(b'\0', b' ').decode('utf-8', 'replace')
        except OSError:
            continue
        if not command:
            continue
        for name, needle in SERVICES:
            if needle in command and name not in found:
                found[name] = int(entry)
    return found


def memory():
    values = {}
    try:
        with open('/proc/meminfo') as handle:
            for line in handle:
                parts = line.replace(':', ' ').split()
                if len(parts) >= 2 and parts[1].isdigit():
                    values[parts[0]] = int(parts[1])
    except OSError:
        return None
    total = values.get('MemTotal')
    available = values.get('MemAvailable')
    if not total or available is None:
        return None
    used = total - available
    swap_total = values.get('SwapTotal', 0)
    swap_free = values.get('SwapFree', 0)
    return {
        'total_mb': round(total / 1024),
        'available_mb': round(available / 1024),
        'used_mb': round(used / 1024),
        'used_percent': round(100 * used / total),
        'cached_mb': round(values.get('Cached', 0) / 1024),
        'swap_total_mb': round(swap_total / 1024),
        'swap_used_mb': round((swap_total - swap_free) / 1024),
        'swap_used_percent': round(100 * (swap_total - swap_free) / swap_total) if swap_total else 0,
    }


def storage():
    try:
        usage = shutil.disk_usage(HOME)
    except OSError:
        return None
    return {
        'total_gb': round(usage.total / 1024 ** 3, 1),
        'used_gb': round(usage.used / 1024 ** 3, 1),
        'free_gb': round(usage.free / 1024 ** 3, 1),
        'used_percent': round(100 * usage.used / usage.total),
    }


def battery():
    """Needs the Termux:API app; returns None when it is absent."""
    try:
        done = subprocess.run(['termux-battery-status'], capture_output=True, text=True, timeout=4)
        if done.returncode != 0:
            return None
        data = json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    temperature = data.get('temperature')
    return {
        'percent': data.get('percentage'),
        'status': data.get('status'),
        'plugged': data.get('plugged'),
        'temperature_c': round(temperature, 1) if isinstance(temperature, (int, float)) else None,
        'health': data.get('health'),
    }


def tunnel():
    state = {
        'ready': False,
        'connections': 0,
        'connector_id': None,
        'disabled': os.path.exists(os.path.join(RUN_DIR, 'tunnel-disabled')),
    }
    try:
        with urllib.request.urlopen('http://127.0.0.1:20241/ready', timeout=2) as response:
            data = json.loads(response.read().decode('utf-8', 'replace'))
        state['ready'] = data.get('status') == 200
        state['connections'] = data.get('readyConnections') or 0
        state['connector_id'] = data.get('connectorId')
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return state


def status_payload():
    try:
        seconds = int(time.clock_gettime(time.CLOCK_BOOTTIME))
    except (OSError, AttributeError):
        seconds = 0
    processes = scan_processes()
    return {
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'device': DEVICE,
        'cores': os.cpu_count(),
        'uptime_seconds': seconds,
        'uptime': '%dd %dh %dm' % (seconds // 86400, (seconds % 86400) // 3600, (seconds % 3600) // 60),
        'memory': memory(),
        'storage': storage(),
        'battery': battery(),
        'services': [
            {'name': name, 'running': name in processes, 'pid': processes.get(name)}
            for name, _ in SERVICES
        ],
        'tunnel': tunnel(),
        'server_disabled': os.path.exists(os.path.join(RUN_DIR, 'server-disabled')),
        'versions': VERSIONS,
    }


def tail(name, lines):
    filename = LOG_FILES.get(name)
    if not filename:
        return None
    path = os.path.join(LOG_DIR, filename)
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            offset = max(0, size - 64 * 1024)
            handle.seek(offset)
            text = handle.read().decode('utf-8', 'replace')
    except OSError:
        return []
    return text.splitlines()[-lines:]


def run_action(action):
    """Only whitelisted actions reach serverctl, and never through a shell."""
    if action not in ACTIONS:
        return None
    subcommand = ACTIONS[action]
    if action in ('server-stop', 'server-restart'):
        # Answer first: stopping would otherwise kill this process mid-response.
        subprocess.Popen(
            ['sh', '-c', 'sleep 1; exec "$0" "$1" >/dev/null 2>&1' , SERVERCTL, subcommand],
            start_new_session=True,
        )
        return {'ok': True, 'action': action, 'output': 'scheduled in 1s'}
    output = _run(SERVERCTL, subcommand, timeout=25).strip()
    return {'ok': True, 'action': action, 'output': output[-2000:]}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'galaxy-status'

    def _send(self, code, payload, content_type='application/json'):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/status':
            try:
                self._send(200, status_payload())
            except (OSError, KeyError, ValueError, TypeError):
                self._send(503, {'error': 'status unavailable'})
            return
        if parsed.path == '/api/logs':
            query = parse_qs(parsed.query)
            name = (query.get('name') or [''])[0]
            try:
                lines = max(1, min(int((query.get('lines') or ['80'])[0]), 200))
            except ValueError:
                lines = 80
            content = tail(name, lines)
            if content is None:
                self._send(404, {'error': 'unknown log'})
                return
            self._send(200, {'name': name, 'lines': content})
            return
        self._send(404, {'error': 'not found'})

    def do_POST(self):
        if urlparse(self.path).path != '/api/action':
            self._send(404, {'error': 'not found'})
            return
        if not (self.headers.get('Content-Type') or '').startswith('application/json'):
            self._send(415, {'error': 'expected application/json'})
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if length <= 0 or length > 4096:
                raise ValueError
            action = json.loads(self.rfile.read(length).decode('utf-8')).get('action')
            if not isinstance(action, str):
                raise ValueError
        except (ValueError, TypeError):
            self._send(400, {'error': 'bad request'})
            return
        result = run_action(action)
        if result is None:
            self._send(400, {'error': 'unknown action'})
            return
        self._send(200, result)

    def log_message(self, format, *args):
        pass


if __name__ == '__main__':
    http.server.ThreadingHTTPServer(('127.0.0.1', 8765), Handler).serve_forever()
