const $ = (selector) => document.querySelector(selector);
const REFRESH_MS = 10000;

function text(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value;
}

function bar(id, percent) {
  const element = document.getElementById(id);
  if (element) element.style.width = Math.max(0, Math.min(100, Number(percent) || 0)) + '%';
}

function toast(message, ok = true) {
  const element = $('#toast');
  element.textContent = message;
  element.classList.toggle('bad', !ok);
  element.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.hidden = true; }, 7000);
}

async function api(path, options) {
  const response = await fetch(path, { cache: 'no-store', ...options });
  if (!response.ok) throw new Error('HTTP ' + response.status);
  return response.json();
}

function setHealth(state, label) {
  const element = $('#health');
  element.dataset.state = state;
  element.textContent = label;
}

function renderStatus(status) {
  text('uptime', status.uptime);
  text('device', (status.device || 'device') + ' · ' + status.cores + ' cores');

  const memory = status.memory;
  if (memory) {
    text('memory-pct', memory.used_percent + '%');
    bar('memory-bar', memory.used_percent);
    text('memory-detail', memory.used_mb + ' / ' + memory.total_mb + ' MB used · swap ' + memory.swap_used_mb + '/' + memory.swap_total_mb + ' MB');
  } else {
    text('memory-pct', 'n/a');
    text('memory-detail', 'not readable');
  }

  const storage = status.storage;
  if (storage) {
    text('storage-pct', storage.used_percent + '%');
    bar('storage-bar', storage.used_percent);
    text('storage-detail', storage.used_gb + ' used · ' + storage.free_gb + ' free of ' + storage.total_gb + ' GB');
  } else {
    text('storage-pct', 'n/a');
    text('storage-detail', 'not readable');
  }

  const battery = status.battery;
  if (battery && battery.percent !== undefined && battery.percent !== null) {
    text('battery-pct', battery.percent + '%');
    bar('battery-bar', battery.percent);
    const parts = [];
    if (battery.status) parts.push(battery.status.toLowerCase());
    if (battery.plugged && battery.plugged !== 'UNPLUGGED') parts.push('plugged in');
    if (battery.temperature_c !== null && battery.temperature_c !== undefined) parts.push(battery.temperature_c + ' °C');
    text('battery-detail', parts.join(' · ') || 'reported by Termux:API');
  } else {
    text('battery-pct', 'n/a');
    text('battery-detail', 'Termux:API unavailable');
    bar('battery-bar', 0);
  }

  const rows = status.services.map((service) =>
    '<tr><td>' + service.name + '</td><td class="mono">' + (service.pid ?? '—') + '</td>' +
    '<td><span class="state ' + (service.running ? 'up' : 'down') + '">' + (service.running ? 'running' : 'stopped') + '</span></td></tr>'
  ).join('');
  document.getElementById('services').innerHTML =
    '<thead><tr><th>Service</th><th>PID</th><th>State</th></tr></thead><tbody>' + rows + '</tbody>';

  const tunnel = status.tunnel;
  text('tunnel-state', tunnel.disabled ? 'disabled' : (tunnel.ready ? 'connected' : 'not connected'));
  text('tunnel-conns', tunnel.ready ? tunnel.connections + ' registered' : '—');
  text('tunnel-id', tunnel.connector_id ? tunnel.connector_id.slice(0, 8) + '…' : '—');
  const toggle = $('#tunnel-toggle');
  toggle.textContent = tunnel.disabled ? 'Enable tunnel' : 'Disable tunnel';
  toggle.dataset.action = tunnel.disabled ? 'tunnel-start' : 'tunnel-stop';

  text('versions', 'nginx ' + (status.versions.nginx || '?') + ' · cloudflared ' + (status.versions.cloudflared || '?'));
  text('stamp', 'updated ' + status.generated_at);
  setHealth('up', status.server_disabled ? 'site stopped' : 'online');
}

async function refresh() {
  try {
    renderStatus(await api('/api/status'));
  } catch (error) {
    setHealth('down', 'unreachable');
    text('stamp', 'status service not answering');
  }
}

async function loadLog() {
  const name = $('#log-name').value;
  const lines = $('#log-lines').value;
  const output = $('#log');
  output.textContent = 'loading…';
  try {
    const data = await api('/api/logs?name=' + encodeURIComponent(name) + '&lines=' + encodeURIComponent(lines));
    output.textContent = data.lines.length ? data.lines.join('\n') : '(empty)';
    output.scrollTop = output.scrollHeight;
  } catch (error) {
    output.textContent = 'could not load ' + name + ': ' + error.message;
  }
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const action = button.dataset.action;
  if (action === 'server-stop' && !confirm('Stop the site? This page, the tunnel and the API all go down, and it stays down until you run serverctl start on the phone.')) return;
  button.disabled = true;
  try {
    const result = await api('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action })
    });
    toast(action + ': ' + (result.output || 'ok'));
  } catch (error) {
    toast(action + ' failed: ' + error.message, false);
  } finally {
    button.disabled = false;
    setTimeout(refresh, 2500);
  }
});

$('#refresh').addEventListener('click', refresh);
$('#log-refresh').addEventListener('click', loadLog);

refresh();
loadLog();
setInterval(refresh, REFRESH_MS);
