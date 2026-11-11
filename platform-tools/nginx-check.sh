#!/data/data/com.termux/files/usr/bin/bash
LOG=/sdcard/Download/nginx-check.log
exec > "$LOG" 2>&1
cd "$HOME/galaxy-server" || exit 1
cp run/nginx.conf run/nginx-test.conf
python - <<'PY'
import os
path = os.path.expanduser('~/galaxy-server/run/nginx-test.conf')
text = open(path).read()
addition = ("  log_format access '$request_method $uri $status $request_time $body_bytes_sent';\n"
            "  map $status $access_loggable { ~^[45] 1; default 0; }\n"
            "  access_log %s/log/access.log access if=$access_loggable;\n"
            % os.path.expanduser('~/galaxy-server'))
text = text.replace('  default_type application/octet-stream;\n',
                    '  default_type application/octet-stream;\n' + addition, 1)
open(path, 'w').write(text)
print('inserted:', 'log_format access' in text)
PY
echo "=== nginx -t ==="
nginx -t -c "$HOME/galaxy-server/run/nginx-test.conf"
echo "exit=$?"
rm -f run/nginx-test.conf
nginx -V 2>&1 | head -2
echo DONE > /sdcard/Download/nginx-check.done
