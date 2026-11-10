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
