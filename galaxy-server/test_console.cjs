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
