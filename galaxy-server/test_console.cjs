const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFile } = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');

test('navigation preserves the terminal, filters, drafts and scroll positions', async () => {
  const browser = await chromium.launch(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {});
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const calls = [];
    let finishOpen;
    const opened = new Promise(resolve => { finishOpen = resolve; });
    await page.addInitScript(() => {
      window.streams = [];
      window.EventSource = class {
        constructor() { this.closed = false; window.streams.push(this); }
        close() { this.closed = true; }
      };
    });
    await page.route('http://console.test/**', async route => {
      const url = new URL(route.request().url());
      const reply = data => route.fulfill({ json: data });
      if (url.pathname.startsWith('/api/')) {
        calls.push({ path: url.pathname, body: route.request().postDataJSON() });
        if (url.pathname === '/api/status') { return route.fulfill({ status: 503, json: { error: 'offline fixture' } }); }
        if (url.pathname === '/api/settings') { return reply({ settings: { host_probes: false }, db: { rows: {} } }); }
        if (url.pathname === '/api/events') { return reply({ audits: [] }); }
        if (url.pathname === '/api/timeline') { return reply({ points: [], events: [] }); }
        if (url.pathname === '/api/logs') {
          return reply({ lines: Array.from({ length: 100 }, (_, i) => ({ ts: '12:00', sev: 'info', text: 'line ' + i })), matched: 100, total: 100, sizes_kb: { nginx: 1 } });
        }
        if (url.pathname === '/api/terminal/open') { await opened; return reply({ id: 'retained-shell' }); }
        return reply({});
      }
      const file = url.pathname === '/console' ? 'console.html' : url.pathname.slice(1);
      let body = await readFile(path.join(__dirname, 'site', file));
      if (file === 'console.js') {
        body = 'const RealTerminal = window.Terminal; window.Terminal = class extends RealTerminal { constructor(options) { super(options); window.testTerminal = this; } };\n' + body;
      }
      return route.fulfill({ body, contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    await page.goto('http://console.test/console#/terminal');
    const navigate = async route => {
      await page.locator('[data-route="' + route + '"]').click();
      await page.locator('#view-' + route).waitFor({ state: 'visible' });
    };
    await page.locator('#terminal-password').fill('test-password');
    await page.locator('#terminal-login button').click();
    await navigate('logs');
    finishOpen();
    await page.waitForFunction(() => window.streams.length === 1);
    // Output continues to arrive while the terminal is hidden.
    await page.evaluate(() => window.streams[0].onmessage({ data: btoa('same shell\r\nhidden output\r\n') }));
    await page.locator('#log-query').fill('keep my search');
    await page.locator('#log-follow').uncheck();
    await page.waitForFunction(() => document.querySelectorAll('#log-lines li').length === 100);
    await navigate('settings');
    await page.locator('#probe-hosts').check();
    await page.evaluate(() => window.scrollTo(0, 250));
    const settingsScroll = await page.evaluate(() => window.scrollY);
    assert.ok(settingsScroll > 0);
    await navigate('metrics');
    await page.locator('#view-metrics button[data-range="7d"]').click();
    await navigate('terminal');
    await page.waitForFunction(() => window.testTerminal.buffer.active.getLine(1).translateToString(true) === 'hidden output');
    await page.locator('#terminal-output').click();
    await page.keyboard.type('pwd');
    await page.waitForFunction(() => window.testTerminal.cols > 2 && window.testTerminal.rows > 2);
    await navigate('settings');
    assert.equal(await page.locator('#probe-hosts').isChecked(), true);
    assert.equal(await page.evaluate(() => window.scrollY), settingsScroll);
    await navigate('logs');
    assert.equal(await page.locator('#log-query').inputValue(), 'keep my search');
    assert.equal(await page.locator('#log-follow').isChecked(), false);
    await navigate('metrics');
    assert.match(await page.locator('#view-metrics button[data-range="7d"]').getAttribute('class'), /on/);
    assert.equal(calls.filter(call => call.path === '/api/terminal/open').length, 1);
    assert.equal(calls.filter(call => call.path === '/api/terminal/close').length, 0);
    assert.equal(await page.evaluate(() => window.streams[0].closed), false);
    assert.ok(calls.some(call => call.path === '/api/terminal/input'));
    await navigate('terminal');
    await page.locator('#terminal-close').click();
    await page.waitForFunction(() => window.streams[0].closed);
    assert.equal(calls.filter(call => call.path === '/api/terminal/close').length, 1);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});

test('reload reattaches to open shells and extra shells get their own tab', async () => {
  const browser = await chromium.launch(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {});
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const calls = [];
    const shells = [];
    await page.addInitScript(() => {
      window.streams = [];
      window.EventSource = class {
        constructor(url) { this.url = url; this.closed = false; window.streams.push(this); }
        close() { this.closed = true; }
      };
    });
    await page.route('http://console.test/**', async route => {
      const url = new URL(route.request().url());
      const reply = data => route.fulfill({ json: data });
      if (url.pathname.startsWith('/api/')) {
        calls.push({ path: url.pathname, body: route.request().postDataJSON() });
        if (url.pathname === '/api/status') { return route.fulfill({ status: 503, json: { error: 'offline fixture' } }); }
        if (url.pathname === '/api/settings') { return reply({ settings: { host_probes: false }, db: { rows: {} } }); }
        if (url.pathname === '/api/events') { return reply({ audits: [] }); }
        if (url.pathname === '/api/timeline') { return reply({ points: [], events: [] }); }
        if (url.pathname === '/api/logs') { return reply({ lines: [], matched: 0, total: 0, sizes_kb: {} }); }
        // Shells live on the server, so a reload still finds them listed.
        if (url.pathname === '/api/terminal/list') {
          return reply({ terminals: shells.map((id, index) => ({ id, created: index })) });
        }
        if (url.pathname === '/api/terminal/open') {
          const id = 'shell-' + (shells.length + 1);
          shells.push(id);
          return reply({ id });
        }
        return reply({});
      }
      const file = url.pathname === '/console' ? 'console.html' : url.pathname.slice(1);
      const body = await readFile(path.join(__dirname, 'site', file));
      return route.fulfill({ body, contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html' });
    });

    await page.goto('http://console.test/console#/terminal');
    await page.locator('#terminal-password').fill('test-password');
    await page.locator('#terminal-login button').click();
    await page.waitForFunction(() => window.streams.length === 1);
    await page.waitForFunction(() => document.querySelectorAll('#terminal-tabs [data-terminal]').length === 1);

    // A second shell gets its own tab, its own stream and its own server id.
    await page.locator('#terminal-new').click();
    await page.waitForFunction(() => window.streams.length === 2);
    await page.waitForFunction(() => document.querySelectorAll('#terminal-tabs [data-terminal]').length === 2);
    const ids = await page.evaluate(() => Array.from(document.querySelectorAll('#terminal-tabs [data-terminal]')).map(tab => tab.dataset.terminal));
    assert.deepEqual(ids, ['shell-1', 'shell-2']);
    assert.equal(await page.locator('#terminal-tabs [data-terminal][aria-selected="true"]').getAttribute('data-terminal'), 'shell-2');

    // Reload: the page must not ask for the password again, must not open a
    // new shell, and must reattach to the two shells still on the server.
    await page.reload();
    await page.locator('#terminal-session').waitFor({ state: 'visible' });
    await page.waitForFunction(() => window.streams.length === 2);
    assert.equal(await page.locator('#terminal-lock').isHidden(), true);
    assert.equal(await page.locator('#terminal-tabs [data-terminal]').count(), 2);
    assert.equal(await page.locator('#terminal-tabs [data-terminal][aria-selected="true"]').getAttribute('data-terminal'), 'shell-2');
    assert.equal(calls.filter(call => call.path === '/api/terminal/open').length, 2);

    // A dropped stream is not a closed shell: the tab keeps its id, the page
    // reattaches on its own, and a further reload still finds both shells.
    await page.evaluate(() => window.streams[0].onerror());
    await page.waitForFunction(() => window.streams.length === 3);
    assert.equal(await page.locator('#terminal-tabs [data-terminal]').count(), 2);
    assert.equal(await page.locator('#terminal-lock').isHidden(), true);
    assert.match(await page.evaluate(() => window.streams[window.streams.length - 1].url), /id=shell-1/);
    await page.reload();
    await page.locator('#terminal-session').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#terminal-tabs [data-terminal]').count(), 2);
    assert.equal(await page.locator('#terminal-lock').isHidden(), true);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});

test('settings renders the remote telemetry card from the API', async () => {
  const browser = await chromium.launch(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {});
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://console.test/**', async route => {
      const url = new URL(route.request().url());
      const reply = data => route.fulfill({ json: data });
      if (url.pathname === '/api/telemetry') {
        return reply({ state: 'ok', enabled: true, configured: true, config_error: null,
          endpoint: 'us-east-1.aws.edge.axiom.co', dataset: 'galaxy-server',
          retention_days: 30, queued: 0, spool_events: 0, spool_kb: 0, exported: 42,
          failed: 0, rejected: 0, dropped: 0, batches: 3,
          last_success_at: '2026-11-11T10:00:00Z', last_error: null });
      }
      if (url.pathname === '/api/status') { return route.fulfill({ status: 503, json: { error: 'offline fixture' } }); }
      if (url.pathname === '/api/settings') {
        return reply({ settings: { host_probes: false }, sample_seconds: 30, session_hours: 12,
          db: { path: '/run/galaxy.db', size_kb: 1, rows: { samples: 1 }, raw_retention_days: 7 } });
      }
      if (url.pathname === '/api/events') { return reply({ audits: [] }); }
      if (url.pathname === '/api/timeline') { return reply({ points: [], events: [] }); }
      if (url.pathname.startsWith('/api/')) { return reply({}); }
      const file = url.pathname === '/console' ? 'console.html' : url.pathname.slice(1);
      const body = await readFile(path.join(__dirname, 'site', file));
      return route.fulfill({ body, contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html' });
    });

    await page.goto('http://console.test/console#/settings');
    await page.waitForFunction(() => document.querySelector('#telemetry-state').textContent === 'Exporting');
    assert.equal(await page.locator('#telemetry-dataset').textContent(), 'galaxy-server · us-east-1.aws.edge.axiom.co');
    assert.match(await page.locator('#telemetry-exported').textContent(), /42 events/);
    assert.equal(await page.locator('#telemetry-retention').textContent(), '30 days');
    assert.equal(await page.locator('#telemetry-last-success').textContent(), '2026-11-11T10:00:00Z');
    assert.equal(await page.locator('#telemetry-last-error').textContent(), 'none');
    assert.equal(await page.locator('#telemetry-note').textContent(), 'Events are sent to Axiom as well as kept in the local files.');
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});

test('device view drives the app through the proxy', async () => {
  const browser = await chromium.launch(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {});
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const calls = [];
    await page.route('http://console.test/**', async route => {
      const url = new URL(route.request().url());
      const reply = data => route.fulfill({ json: data });
      if (url.pathname.startsWith('/api/')) {
        calls.push({ path: url.pathname, body: route.request().postDataJSON() });
        if (url.pathname === '/api/status') { return route.fulfill({ status: 503, json: { error: 'offline fixture' } }); }
        if (url.pathname === '/api/settings') { return reply({ settings: { host_probes: false }, db: { rows: {} } }); }
        if (url.pathname === '/api/events') { return reply({ audits: [] }); }
        if (url.pathname === '/api/timeline') { return reply({ points: [], events: [] }); }
        if (url.pathname === '/api/device/summary') {
          return reply({
            paired: true,
            health: { ok: true, uptime_s: 120, camera_open: '' },
            capabilities: {
              model: 'SM-A202F', android: '11',
              permissions: { camera: true, microphone: true },
              brightness: 120, brightness_writable: true,
              cameras: [
                { id: '0', facing: 'back', flash: true, jpeg_sizes: ['1920x1080', '1280x720'] },
                { id: '1', facing: 'front', flash: false, jpeg_sizes: ['1280x720'] }
              ]
            },
            battery: { percentage: 74, plugged: false, temperature_c: 28.4, voltage_mv: 4100 },
            storage: { free_bytes: 5 * 1024 * 1024 * 1024, total_bytes: 23 * 1024 * 1024 * 1024,
              folders: { photos: { count: 1, bytes: 1024 }, recordings: { count: 1, bytes: 2048 } } },
            volume: { streams: { music: { index: 9, max: 15 }, alarm: { index: 15, max: 15 } } }
          });
        }
        if (url.pathname === '/api/device/sensors') {
          return reply({ accelerometer: { values: [0.1, 0.2, 9.8], name: 'LSM6DSL' } });
        }
        if (url.pathname === '/api/device/photos') {
          return reply({ files: [{ file: 'cam0-1.jpg', bytes: 2048, modified: 1700000000000 }] });
        }
        if (url.pathname === '/api/device/recordings') {
          return reply({ files: [{ file: 'mic-1.wav', bytes: 4096, modified: 1700000000000 }] });
        }
        if (url.pathname === '/api/device/media') {
          return reply({ files: [{ file: 'tone.wav', bytes: 32044, modified: 1700000000000 }] });
        }
        if (url.pathname === '/api/device/control') {
          const action = (route.request().postDataJSON() || {}).action;
          if (action === 'snapshot') { return reply({ ok: true, file: 'cam0-2.jpg', bytes: 4096, size: '1920x1080' }); }
          if (action === 'record-stop') { return reply({ ok: true, file: 'mic-2.wav', bytes: 8192, seconds: 4 }); }
          return reply({ ok: true });
        }
        return reply({});
      }
      const file = url.pathname === '/console' ? 'console.html' : url.pathname.slice(1);
      const body = await readFile(path.join(__dirname, 'site', file));
      return route.fulfill({ body, contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html' });
    });

    await page.goto('http://console.test/console#/device');
    await page.locator('#view-device').waitFor({ state: 'visible' });
    await page.waitForFunction(() => document.querySelector('#device-banner-text').textContent === 'Control app running');
    assert.match(await page.locator('#device-banner-detail').textContent(), /camera idle/);
    assert.equal(await page.locator('#dev-battery').textContent(), '74% · 4100 mV');
    assert.equal(await page.locator('#cam-id option').count(), 2);
    assert.equal(await page.locator('#sensor-table tbody tr').count(), 1);
    assert.match(await page.locator('#sensor-table tbody').textContent(), /0.1, 0.2, 9.8/);
    assert.equal(await page.locator('#photo-grid figure').count(), 1);
    assert.equal(await page.locator('#recordings-table tbody tr').count(), 1);
    assert.equal(await page.locator('#media-table tbody tr').count(), 1);
    assert.equal(await page.locator('#volume-sliders .slider-row').count(), 2);

    // Accessories go through the allowlisted proxy action, not a raw endpoint.
    await page.locator('#torch-toggle').click();
    await page.waitForFunction(() => document.querySelector('#torch-state').textContent === 'On');
    await page.locator('[data-vibrate="400"]').click();
    await page.locator('#brightness').fill('200');
    await page.evaluate(() => {
      const input = document.querySelector('#brightness');
      input.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await page.waitForFunction(() => true);
    await page.waitForTimeout(400);

    // Preview uses the stream endpoint and starts/stops with navigation.
    await page.locator('#cam-start').click();
    const source = await page.locator('#cam-preview').getAttribute('src');
    assert.match(source, /^\/api\/device\/camera\/stream\?camera=0&fps=4/);
    await page.locator('[data-route="overview"]').click();
    await page.waitForFunction(() => document.querySelector('#cam-preview').hasAttribute('src') === false);

    await page.locator('[data-route="device"]').click();
    await page.locator('#cam-snapshot').click();
    await page.locator('#mic-record').click();
    await page.waitForFunction(() => document.querySelector('#mic-record').textContent === 'Stop recording');
    await page.locator('#mic-record').click();
    await page.waitForFunction(() => /saved mic-2.wav/.test(document.querySelector('#mic-timer').textContent));

    const torch = calls.filter(call => call.path === '/api/device/control' && call.body.action === 'torch');
    assert.deepEqual(torch.map(call => call.body.payload.on), [true]);
    const vibrate = calls.filter(call => call.body && call.body.action === 'vibrate');
    assert.deepEqual(vibrate.map(call => call.body.payload.ms), [400]);
    const brightness = calls.filter(call => call.body && call.body.action === 'brightness');
    assert.deepEqual(brightness.map(call => call.body.payload.value), [200]);
    const stop = calls.filter(call => call.body && call.body.action === 'camera-stop');
    assert.ok(stop.length >= 1);
    const snapshot = calls.filter(call => call.body && call.body.action === 'snapshot');
    assert.equal(snapshot[0].body.payload.camera, '0');
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});
