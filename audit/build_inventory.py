import csv
import re
from pathlib import Path

root = Path(__file__).parent
details = (root / 'package-details.txt').read_text(encoding='utf-8-sig', errors='replace')
blocks = re.split(r'(?m)^  Package \[([^\]]+)\] \([^\n]*\):\s*$', details)
versions = {}
for name, block in zip(blocks[1::2], blocks[2::2]):
    version = re.search(r'(?m)^\s*versionName=(.*)$', block)
    code = re.search(r'(?m)^\s*versionCode=(\S+)', block)
    versions.setdefault(name, (version.group(1).strip() if version else '', code.group(1) if code else ''))
disabled = {s.removeprefix('package:').strip() for s in (root / 'disabled.txt').read_text(encoding='utf-8-sig').splitlines()}
rows = []
for line in (root / 'packages.txt').read_text(encoding='utf-8-sig').splitlines():
    match = re.match(r'package:(.*)=(\S+)\s+installer=(\S+)', line)
    if not match:
        continue
    path, name, installer = match.groups()
    version, code = versions.get(name, ('', ''))
    rows.append([name, version, code, installer, path, 'disabled' if name in disabled else 'installed', 'Not checked against official device-specific catalog'])
with (root / 'inventory.csv').open('w', encoding='utf-8-sig', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(['Package', 'Version name', 'Version code', 'Installer', 'APK path', 'State', 'Update status'])
    writer.writerows(sorted(rows))
print(f'{len(rows)} packages; {sum(bool(r[1]) for r in rows)} version names found')
