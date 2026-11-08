import os, shutil, traceback
for path in ('/proc/uptime', '/proc/meminfo'):
    try:
        with open(path) as f: print(path, repr(f.read(400)))
    except Exception as e: print(path, repr(e))
try: print('disk', shutil.disk_usage(os.path.expanduser('~')))
except Exception: traceback.print_exc()
