#!/usr/bin/env python3
import http.server, json, os, shutil, time

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/api/status':
            self.send_error(404); return
        try:
            seconds = int(time.clock_gettime(time.CLOCK_BOOTTIME))
            with open('/proc/meminfo') as f: mem = {p[0].rstrip(':'): int(p[1]) for line in f if len(p := line.split()) >= 2 and p[1].isdigit()}
            percent = round(100 * (1 - mem['MemAvailable'] / mem['MemTotal'])) if 'MemAvailable' in mem else None
            payload = json.dumps({'uptime': f'{seconds // 86400}d {(seconds % 86400) // 3600}h', 'memory_percent': percent, 'storage_gb': round(shutil.disk_usage(os.path.expanduser('~')).free / 1024**3, 1)}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Cache-Control', 'no-store'); self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
        except (OSError, KeyError, ValueError): self.send_error(503)
    def log_message(self, format, *args): pass

http.server.HTTPServer(('127.0.0.1', 8765), Handler).serve_forever()
