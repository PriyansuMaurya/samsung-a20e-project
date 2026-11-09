// Galaxy Console: overview, history charts, services, logs and controls.
// No framework, no build step. Everything is fetched from the same origin and
// the server decides what the session may see.
(function () {
  'use strict';

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  var ROUTES = {
    overview: ['Overview', 'Live state of the phone, the site and the tunnel.'],
    metrics: ['Metrics', 'CPU, memory, battery and storage over time.'],
    services: ['Services', 'Processes, restart counts and controls.'],
    networking: ['Networking', 'Hostnames, tunnel connector and access rules.'],
    logs: ['Logs', 'Search the server log files.'],
    terminal: ['Terminal', 'Run commands as the Termux user.'],
    settings: ['Settings', 'Probe configuration, storage and audit trail.']
  };

  var state = {
    route: 'overview',
    range: '24h',
    status: null,
    timelines: {},
    settings: null,
    events: null,
    logs: null,
    statusTimer: null,
    logTimer: null,
    scrollPositions: {},
    settingsEdit: 0,
    settingsSaved: 0,
    loading: false
  };

  // ---------------------------------------------------------------- helpers
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        if (key === 'text') { node.textContent = attrs[key]; }
        else if (key === 'html') { node.innerHTML = attrs[key]; }
        else if (key === 'class') { node.className = attrs[key]; }
        else if (key.slice(0, 2) === 'on') { node.addEventListener(key.slice(2), attrs[key]); }
        else if (attrs[key] !== null && attrs[key] !== undefined) { node.setAttribute(key, attrs[key]); }
      });
    }
    (children || []).forEach(function (child) {
      if (child === null || child === undefined) { return; }
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }

  function pad(value) { return (value < 10 ? '0' : '') + value; }

  // The API mixes epoch seconds with "YYYY-MM-DDTHH:MM:SS" strings, depending on
  // which payload the value came from.
  function toDate(value) {
    if (value === null || value === undefined || value === '') { return null; }
    var date = typeof value === 'number' ? new Date(value * 1000)
      : new Date(String(value).replace(' ', 'T'));
    return isNaN(date.getTime()) ? null : date;
  }

  function clock(ts) {
    var d = toDate(ts);
    if (!d) { return '—'; }
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  function hhmm(ts) {
    var d = toDate(ts);
    if (!d) { return '—'; }
    return pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function dayLabel(ts) {
    var d = toDate(ts);
    if (!d) { return ''; }
    return ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()] + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function ago(seconds) {
    if (seconds === null || seconds === undefined) { return 'no sample'; }
    if (seconds < 5) { return 'just now'; }
    if (seconds < 90) { return Math.round(seconds) + 's ago'; }
    if (seconds < 5400) { return Math.round(seconds / 60) + 'm ago'; }
    if (seconds < 172800) { return Math.round(seconds / 3600) + 'h ago'; }
    return Math.round(seconds / 86400) + 'd ago';
  }

  function gb(value) { return value === null || value === undefined ? '—' : value.toFixed(1) + ' GB'; }

  function api(path, options) {
    var opts = Object.assign({ cache: 'no-store', credentials: 'same-origin', headers: {} }, options || {});
    if (opts.body) { opts.headers['Content-Type'] = 'application/json'; }
    return fetch(path, opts).then(function (response) {
      if (response.status === 401) {
        window.location.href = '/login';
        throw new Error('signed out');
      }
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) { throw new Error(data.error || 'HTTP ' + response.status); }
        return data;
      });
    });
  }

  var toastTimer = null;
  function toast(message, kind) {
    var node = $('#toast');
    node.textContent = message;
    node.dataset.kind = kind || 'ok';
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { node.hidden = true; }, kind === 'error' ? 7000 : 4000);
  }

  // ------------------------------------------------------------- navigation
  function show(route) {
    if (!ROUTES[route]) { route = 'overview'; }
    state.scrollPositions[state.route] = window.scrollY;
    state.route = route;
    document.body.classList.toggle('terminal-view', route === 'terminal');
    $$('.view').forEach(function (view) {
      view.hidden = view.id !== 'view-' + route;
    });
    $$('#nav a').forEach(function (link) {
      if (link.dataset.route === route) { link.setAttribute('aria-current', 'page'); }
      else { link.removeAttribute('aria-current'); }
    });
    $('#page-title').textContent = ROUTES[route][0];
    $('#page-sub').textContent = ROUTES[route][1];
    document.title = ROUTES[route][0] + ' · Galaxy Console';
    closeDrawer();
    // Views stay mounted: the shell, its stream and each form belong to the
    // dashboard session, rather than the currently visible navigation item.
    window.scrollTo(0, state.scrollPositions[route] || 0);
    scheduleLogs();
    if (route === 'terminal' && terminalId) {
      window.requestAnimationFrame(fitTerminal);
    }
    if (route === 'metrics') { renderCharts(); }
    if (route === 'logs') { loadLogs(); }
    if (route === 'settings') { loadSettings(); loadEvents(); }
    if (route === 'services' || route === 'networking') { loadEvents(); }
  }

  function routeFromHash() {
    var match = /^#\/([a-z]+)/.exec(window.location.hash || '');
    return match ? match[1] : 'overview';
  }

  function openDrawer() {
    $('#sidebar').classList.add('open');
    $('#scrim').hidden = false;
    $('#menu').setAttribute('aria-expanded', 'true');
  }
  function closeDrawer() {
    $('#sidebar').classList.remove('open');
    $('#scrim').hidden = true;
    $('#menu').setAttribute('aria-expanded', 'false');
  }

  // --------------------------------------------------------------- overview
  var COMPONENT_LABELS = {
    phone: 'Device health', nginx: 'nginx', api: 'Status API',
    tunnel: 'Cloudflare Tunnel', https: 'Public HTTPS'
  };

  function renderComponents(status) {
    var host = $('#components');
    host.textContent = '';
    Object.keys(COMPONENT_LABELS).forEach(function (key) {
      var component = (status.components || {})[key];
      if (!component) { return; }
      var time = component.at ? clock(component.at) :
        (component.state === 'unknown' ? 'not monitored' : 'no reading');
      host.appendChild(el('div', { class: 'component', 'data-state': component.state }, [
        el('div', { class: 'c-top' }, [
          el('span', { class: 'dot', 'aria-hidden': 'true' }),
          el('span', { text: COMPONENT_LABELS[key] })
        ]),
        el('div', { class: 'c-detail', title: component.detail, text: component.detail }),
        el('span', { class: 'c-time', text: time })
      ]));
    });
  }

  function renderAlerts(status) {
    var host = $('#alerts');
    host.textContent = '';
    var alerts = status.alerts || [];
    if (!alerts.length) {
      host.appendChild(el('span', { class: 'alert ok', text: 'All checks passing' }));
      host.hidden = false;
      return;
    }
    alerts.forEach(function (alert) {
      host.appendChild(el('span', { class: 'alert ' + alert.level, text: alert.text }));
    });
    host.hidden = false;
  }

  function stamp(key, text, stale) {
    var node = $('[data-stamp="' + key + '"]');
    if (node) { node.textContent = stale ? text + ' · stale' : text; }
  }

  function setKpi(id, value, sub, kpiState) {
    var node = $('#kpi-' + id);
    node.textContent = value;
    var article = node.closest('.kpi');
    if (kpiState) { article.dataset.state = kpiState; } else { delete article.dataset.state; }
    var subNode = $('#kpi-' + id + '-sub');
    if (subNode) { subNode.textContent = sub; }
  }

  function renderKpis(status) {
    var sampleAt = status.sample && status.sample.at;
    var stale = Boolean(status.sample && status.sample.stale);
    var sampleAge = status.sample ? status.sample.age_s : null;

    setKpi('uptime', status.uptime, 'since boot · ' + (status.host.cores || '?') + ' cores');
    stamp('uptime', clock(status.server_time), false);

    var cpu = status.cpu || {};
    if (cpu.pct === null || cpu.pct === undefined) {
      setKpi('cpu', 'Unavailable', 'Android blocks /proc/stat', 'unknown');
      stamp('cpu', 'no reading', false);
    } else {
      setKpi('cpu', cpu.pct + '%', 'of one core, server processes');
      stamp('cpu', clock(status.sample.ts), stale);
    }

    var mem = status.memory;
    if (mem) {
      setKpi('memory', mem.used_percent + '%',
        mem.used_mb + ' of ' + mem.total_mb + ' MB · swap ' + mem.swap_used_percent + '%',
        mem.used_percent >= 90 ? 'crit' : mem.used_percent >= 80 ? 'warn' : null);
    } else {
      setKpi('memory', 'Unavailable', '/proc/meminfo unreadable', 'unknown');
    }
    stamp('memory', clock(status.server_time), false);

    var disk = status.storage;
    if (disk) {
      setKpi('storage', gb(disk.free_gb), disk.used_percent + '% of ' + gb(disk.total_gb) + ' used',
        disk.used_percent >= 90 ? 'crit' : null);
    } else {
      setKpi('storage', 'Unavailable', 'filesystem not readable', 'unknown');
    }
    stamp('storage', clock(status.server_time), false);

    var batt = status.battery;
    if (batt && batt.percent !== null && batt.percent !== undefined) {
      var sub = batt.charging ? 'On the charger' : 'Discharging';
      if (batt.temperature_c !== null && batt.temperature_c !== undefined) { sub += ' · ' + batt.temperature_c + '°C'; }
      setKpi('battery', batt.percent + '%', sub,
        batt.percent <= 15 && !batt.charging ? 'warn' : null);
    } else {
      setKpi('battery', 'Unavailable', 'termux-api not installed', 'unknown');
    }
    stamp('battery', clock(status.server_time), false);

    var tun = status.tunnel || {};
    if (tun.requests === null || tun.requests === undefined) {
      setKpi('requests', 'Unavailable', tun.reachable ? 'metrics have no counter' : 'connector metrics unreachable', 'unknown');
    } else {
      setKpi('requests', String(tun.requests), (tun.errors || 0) + ' errors · ' + (tun.connections || 0) + ' connections');
    }
    stamp('tunnel', clock(status.server_time), false);

    $('#sampled').textContent = sampleAt ? 'sample ' + clock(status.sample.ts) + ' · ' + ago(sampleAge) : 'no sample yet';
    $('#sampled').title = stale ? 'The sampler is behind; values may be out of date.' : 'Live reads plus the last stored sample.';
  }

  function renderHealth(status) {
    var pill = $('#health');
    var states = { ok: 'Healthy', warn: 'Degraded', crit: 'Down' };
    pill.dataset.state = status.health;
    pill.textContent = states[status.health] || 'Unknown';
    if (status.alerts && status.alerts.length) {
      pill.textContent += ' · ' + status.alerts.length + (status.alerts.length === 1 ? ' issue' : ' issues');
    }
  }

  var STATE_LABEL = { ok: 'up', warn: 'warn', crit: 'down', unknown: 'unknown' };

  function serviceRow(service) {
    return el('tr', null, [
      el('td', null, [
        el('div', { class: 'name' }, [
          el('span', { text: service.label }),
          el('small', { text: service.pid ? 'pid ' + service.pid : 'not running' })
        ])
      ]),
      el('td', null, [el('span', { class: 'state ' + (service.running ? 'up' : 'down'), text: service.running ? 'running' : 'stopped' })]),
      el('td', { class: 'right mono', text: service.uptime || '—' }),
      el('td', { class: 'right mono', text: service.rss_mb === null ? '—' : service.rss_mb + ' MB' })
    ]);
  }

  function renderOverviewServices(status) {
    var body = $('#overview-services tbody');
    body.textContent = '';
    if (!status.services || !status.services.length) {
      body.appendChild(el('tr', null, [el('td', { colspan: '4', class: 'empty', text: 'No service data.' })]));
      return;
    }
    status.services.forEach(function (service) { body.appendChild(serviceRow(service)); });
  }

  function renderEventList(host, items, options) {
    host.textContent = '';
    if (!items.length) {
      host.appendChild(el('li', { class: 'empty', text: options && options.empty || 'Nothing recorded.' }));
      return;
    }
    items.slice(0, options && options.limit || 12).forEach(function (item) {
      var when = item.at ? new Date(item.at.replace(' ', 'T')) : null;
      var css = item.kind === 'admin' ? '' : item.level || '';
      host.appendChild(el('li', null, [
        el('span', { class: 'when', text: when ? pad(when.getHours()) + ':' + pad(when.getMinutes()) : '—' }),
        el('span', { class: 'what', text: item.text }),
        el('span', { class: 'kind ' + (options && options.kindClass ? options.kindClass(item) : ''), text: item.kind || '' })
      ]));
    });
  }

  function renderOverviewEvents() {
    var timeline = state.timelines[state.range];
    var host = $('#overview-events');
    if (!timeline || !timeline.events.length) {
      host.textContent = '';
      host.appendChild(el('li', { class: 'empty', text: 'Nothing recorded in this range.' }));
      return;
    }
    var recent = timeline.events.slice(-8).reverse().map(function (event) {
      return {
        at: new Date(event.t * 1000).toISOString().replace('T', ' ').slice(0, 19),
        kind: event.kind,
        text: event.detail || event.kind
      };
    });
    renderEventList(host, recent, { limit: 8, empty: 'Nothing recorded in this range.' });
  }

  function renderEventsStrip(selector, timeline) {
    var host = $(selector);
    host.textContent = '';
    if (!timeline || !timeline.events.length) {
      host.appendChild(el('span', { class: 'chip', text: 'No reboots, restarts or disconnects in this range' }));
      return;
    }
    timeline.events.slice(-10).forEach(function (event) {
      host.appendChild(el('span', { class: 'chip ' + event.kind }, [
        el('span', { class: 'k', 'aria-hidden': 'true' }),
        el('b', { text: hhmm(event.t) }),
        el('span', { text: event.detail || event.kind })
      ]));
    });
  }

  // ----------------------------------------------------------------- charts
  function fitCanvas(canvas) {
    var rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) { return null; }
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);
    return { ctx: ctx, w: rect.width, h: rect.height };
  }

  var COLORS = {
    lime: '#c3f451', amber: '#f2b441', red: '#ff6b5c', line: '#1b2732',
    grid: '#16202a', muted: '#7d93a2', dim: '#4d6472', panel: '#0d141a'
  };

  function drawTimeline(canvas, timeline) {
    var box = fitCanvas(canvas);
    var empty = canvas.parentNode.querySelector('.chart-empty');
    if (!box) { return; }
    var points = timeline ? timeline.points : [];
    if (!points.length) {
      if (empty) { empty.hidden = false; }
      return;
    }
    if (empty) { empty.hidden = true; }
    var ctx = box.ctx;
    var lanes = [
      ['nginx', 'nginx', points.map(function (p) { return p.nginx; })],
      ['tunnel', 'Tunnel', points.map(function (p) { return p.tunnel; })],
      ['https', 'Public HTTPS', points.map(function (p) { return p.https; })]
    ];
    var padL = box.w < 420 ? 74 : 96;
    var padR = 6;
    var padT = 22;
    var padB = 20;
    var plotW = Math.max(10, box.w - padL - padR);
    var plotH = Math.max(30, box.h - padT - padB);
    var gap = 6;
    var laneH = Math.max(6, (plotH - gap * 2) / 3);
    var span = Math.max(1, timeline.until - timeline.since);
    var x = function (ts) { return padL + (ts - timeline.since) / span * plotW; };

    // time ruler
    ctx.font = '10px ui-monospace, Consolas, monospace';
    ctx.fillStyle = COLORS.dim;
    ctx.textBaseline = 'top';
    var ticks = box.w < 480 ? 3 : 5;
    for (var t = 0; t <= ticks; t++) {
      var ts = timeline.since + span * (t / ticks);
      var px = x(ts);
      ctx.strokeStyle = COLORS.grid;
      ctx.beginPath();
      ctx.moveTo(Math.round(px) + .5, padT - 4);
      ctx.lineTo(Math.round(px) + .5, padT + plotH);
      ctx.stroke();
      var label = span > 172800 ? dayLabel(ts) : hhmm(ts);
      ctx.fillStyle = COLORS.dim;
      var width = ctx.measureText(label).width;
      ctx.fillText(label, Math.min(box.w - padR - width, Math.max(padL, px - width / 2)), 4);
    }

    lanes.forEach(function (lane, index) {
      var y = padT + index * (laneH + gap);
      ctx.fillStyle = COLORS.muted;
      ctx.font = '11px ui-sans-serif, system-ui, sans-serif';
      ctx.textBaseline = 'middle';
      ctx.fillText(lane[1], 0, y + laneH / 2);
      ctx.fillStyle = '#101a22';
      ctx.fillRect(padL, y, plotW, laneH);
      // Widths follow the clock, so a single recent bucket stays a sliver at
      // the right edge instead of stretching across the whole range.
      points.forEach(function (point, i) {
        var value = lane[2][i];
        if (value === null || value === undefined) { return; }
        ctx.fillStyle = value ? COLORS.lime : COLORS.red;
        var left = x(point.t);
        var right = x(point.t + timeline.bucket_s);
        ctx.fillRect(left, y, Math.max(1.5, right - left - 0.8), laneH);
      });
      // lane frame
      ctx.strokeStyle = COLORS.grid;
      ctx.strokeRect(padL + .5, y + .5, plotW - 1, laneH - 1);
    });

    var markers = (timeline.events || []).filter(function (event) {
      return ['reboot', 'restart', 'down', 'tunnel-down', 'https-down', 'tunnel-up', 'https-up'].indexOf(event.kind) >= 0;
    });
    markers.forEach(function (event) {
      var px = x(event.t);
      if (px < padL || px > padL + plotW) { return; }
      var down = ['reboot', 'down', 'tunnel-down', 'https-down'].indexOf(event.kind) >= 0;
      ctx.strokeStyle = down ? '#ff6b5c88' : '#c3f45166';
      ctx.beginPath();
      ctx.moveTo(Math.round(px) + .5, padT);
      ctx.lineTo(Math.round(px) + .5, padT + plotH - 2);
      ctx.stroke();
      ctx.fillStyle = down ? COLORS.red : COLORS.lime;
      ctx.beginPath();
      ctx.moveTo(px - 3, padT + plotH);
      ctx.lineTo(px + 3, padT + plotH);
      ctx.lineTo(px, padT + plotH + 4.5);
      ctx.closePath();
      ctx.fill();
    });
  }

  function drawLine(canvas, points, key, options) {
    var box = fitCanvas(canvas);
    var empty = canvas.parentNode.querySelector('.chart-empty');
    if (!box) { return; }
    var values = points.map(function (p) { return p[key]; }).filter(function (v) {
      return v !== null && v !== undefined;
    });
    if (!values.length) {
      if (empty) { empty.textContent = 'No samples in this range.'; empty.hidden = false; }
      return;
    }
    if (empty) { empty.hidden = true; }
    var ctx = box.ctx;
    var padL = 38, padR = 8, padT = 12, padB = 20;
    var plotW = Math.max(10, box.w - padL - padR);
    var plotH = Math.max(20, box.h - padT - padB);
    var min = Math.min.apply(null, values);
    var max = Math.max.apply(null, values);
    if (options && options.fixed) { min = options.fixed[0]; max = options.fixed[1]; }
    if (max - min < 1) { max = min + 1; }
    var pad = (max - min) * 0.12;
    if (!(options && options.fixed)) { min -= pad; max += pad; }
    var span = Math.max(1, (points[points.length - 1].t - points[0].t));
    var start = points[0].t;
    var x = function (ts) { return padL + (ts - start) / span * plotW; };
    var y = function (value) { return padT + (1 - (value - min) / (max - min)) * plotH; };

    // charging bands
    if (options && options.bands) {
      ctx.fillStyle = '#c3f45114';
      points.forEach(function (point, i) {
        if (point.plugged !== 1) { return; }
        var next = Math.min(points[i + 1] ? points[i + 1].t : point.t + state.timelines[state.range].bucket_s,
          point.t + state.timelines[state.range].bucket_s);
        ctx.fillRect(x(point.t), padT, Math.max(1, x(next) - x(point.t)), plotH);
      });
    }

    ctx.font = '10px ui-monospace, Consolas, monospace';
    ctx.textBaseline = 'middle';
    for (var g = 0; g <= 2; g++) {
      var value = min + (max - min) * (g / 2);
      var py = y(value);
      ctx.strokeStyle = COLORS.grid;
      ctx.beginPath();
      ctx.moveTo(padL, Math.round(py) + .5);
      ctx.lineTo(padL + plotW, Math.round(py) + .5);
      ctx.stroke();
      ctx.fillStyle = COLORS.dim;
      var text = options && options.formatTick ? options.formatTick(value) : Math.round(value) + '';
      ctx.fillText(text, 0, py);
    }

    var color = options && options.color || COLORS.lime;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    var drawing = false;
    var previous = null;
    var bucket = state.timelines[state.range].bucket_s;
    points.forEach(function (point) {
      var value = point[key];
      if (value === null || value === undefined) { drawing = false; previous = null; return; }
      if (!drawing || point.t - previous > bucket) { ctx.moveTo(x(point.t), y(value)); drawing = true; }
      else { ctx.lineTo(x(point.t), y(value)); }
      previous = point.t;
    });
    ctx.stroke();

    ctx.fillStyle = COLORS.dim;
    ctx.textBaseline = 'top';
    var labelCount = box.w < 420 ? 3 : 5;
    for (var i = 0; i <= labelCount; i++) {
      var ts = start + span * (i / labelCount);
      var pyLabel = padT + plotH + 5;
      var label = span > 172800 ? dayLabel(ts) : hhmm(ts);
      var width = ctx.measureText(label).width;
      ctx.fillText(label, Math.min(box.w - padR - width, Math.max(padL, x(ts) - width / 2)), pyLabel);
    }
  }

  function statLine(id, values, suffix) {
    var node = $(id);
    var clean = values.filter(function (v) { return v !== null && v !== undefined; });
    if (!clean.length) { node.textContent = 'not available'; return; }
    var min = Math.min.apply(null, clean);
    var max = Math.max.apply(null, clean);
    var avg = clean.reduce(function (a, b) { return a + b; }, 0) / clean.length;
    node.textContent = 'low ' + min.toFixed(0) + suffix + ' · avg ' + avg.toFixed(0) + suffix + ' · high ' + max.toFixed(0) + suffix;
  }

  function renderCharts() {
    var timeline = state.timelines[state.range];
    drawTimeline($('#chart-timeline-overview'), timeline);
    drawTimeline($('#chart-timeline-metrics'), timeline);
    if (!timeline) { return; }
    var points = timeline.points;
    drawLine($('#chart-cpu'), points, 'cpu', { color: COLORS.lime, formatTick: function (v) { return (v < 10 ? v.toFixed(1) : v.toFixed(0)) + '%'; } });
    drawLine($('#chart-mem'), points, 'mem', { color: '#8fd0ff', fixed: [0, 100], formatTick: function (v) { return v.toFixed(0) + '%'; }, fill: '#8fd0ff10' });
    drawLine($('#chart-batt'), points, 'batt', { color: COLORS.amber, fixed: [0, 100], bands: true, formatTick: function (v) { return v.toFixed(0) + '%'; } });
    drawLine($('#chart-disk'), points, 'free_gb', { color: '#9fb6c8', formatTick: function (v) { return v.toFixed(1); } });
    statLine('#stat-cpu', points.map(function (p) { return p.cpu; }), '%');
    statLine('#stat-mem', points.map(function (p) { return p.mem; }), '%');
    statLine('#stat-batt', points.map(function (p) { return p.batt; }), '%');
    statLine('#stat-disk', points.map(function (p) { return p.free_gb; }), ' GB');

    var lastPoint = points[points.length - 1];
    $('#timeline-sub').textContent = timeline.points.length
      ? timeline.points.length + (timeline.points.length === 1 ? ' bucket of ' : ' buckets of ') +
        timeline.bucket_s + 's since ' + clock(timeline.since) + '. Gaps mean no sample.'
      : 'Nginx, tunnel and public HTTPS, sampled continuously.';
    $('#timeline-sub-2').textContent = 'Gaps mean no sample was recorded, not that the service was healthy.';
    renderEventsStrip('#events-overview', timeline);
    renderEventsStrip('#events-metrics', timeline);
    var legend = '<span class="item"><span class="swatch"></span>answering</span>' +
      '<span class="item"><span class="swatch" style="background:#ff6b5c"></span>failing</span>' +
      '<span class="item"><span class="swatch gap"></span>no sample</span>' +
      '<span class="item">▾ reboot, restart or disconnect</span>';
    $('#legend-timeline-overview').innerHTML = legend;
    $('#legend-timeline-metrics').innerHTML = legend;
    if (lastPoint) { $('#metrics-sub').textContent = timeline.points.length + ' buckets of ' + timeline.bucket_s + 's, newest ' + clock(lastPoint.t) + '.'; }
  }

  // --------------------------------------------------------------- services
  function renderServices(status) {
    var body = $('#services-table tbody');
    body.textContent = '';
    (status.services || []).forEach(function (service) {
      var control = '—';
      if (service.key === 'nginx' || service.key === 'status') {
        control = el('button', {
          class: 'btn small', type: 'button', 'data-action': 'server-restart', 'data-phrase': 'restart',
          text: 'Restart site'
        });
      } else if (service.key === 'cloudflared') {
        control = el('button', {
          class: 'btn small', type: 'button', 'data-action': 'tunnel-restart', 'data-phrase': 'restart the tunnel',
          text: 'Restart tunnel'
        });
      } else {
        control = el('span', { class: 'muted', text: 'no control' });
      }
      body.appendChild(el('tr', null, [
        el('td', null, [
          el('div', { class: 'name' }, [
            el('span', { text: service.label }),
            el('small', { text: service.pid ? 'pid ' + service.pid : 'not running' })
          ])
        ]),
        el('td', null, [el('span', { class: 'state ' + (service.running ? 'up' : 'down'), text: service.running ? 'running' : 'stopped' })]),
        el('td', { class: 'right mono', text: service.uptime || '—' }),
        el('td', { class: 'right mono', text: String(service.restarts_24h || 0) }),
        el('td', { class: 'right mono', text: service.rss_mb === null ? '—' : service.rss_mb + ' MB' }),
        el('td', null, [el('div', { class: 'err', title: service.last_error || '', text: service.last_error || 'none recorded' })]),
        el('td', { class: 'right' }, [control])
      ]));
    });
  }

  function renderAudit(host, audits) {
    host.textContent = '';
    if (!audits || !audits.length) {
      host.appendChild(el('li', { class: 'empty', text: 'No admin actions recorded.' }));
      return;
    }
    audits.slice(0, 10).forEach(function (entry) {
      var when = entry.at ? new Date(entry.at.replace(' ', 'T')) : null;
      var outcome = entry.outcome || '';
      host.appendChild(el('li', null, [
        el('span', { class: 'when', text: when ? pad(when.getHours()) + ':' + pad(when.getMinutes()) : '—' }),
        el('span', { class: 'what', text: (entry.user || '?') + ' · ' + entry.action }),
        el('span', {
          class: 'kind ' + (outcome === 'ok' || outcome === 'scheduled' ? '' : outcome === 'failed' ? 'crit' : 'warn'),
          text: outcome
        })
      ]));
    });
  }

  // ------------------------------------------------------------- networking
  function renderNetworking(status) {
    var host = $('#host-cards');
    host.textContent = '';
    (status.networking.hostnames || []).forEach(function (entry) {
      var last = entry.last;
      var state = last ? (last.ok ? 'ok' : 'crit') : 'unknown';
      host.appendChild(el('div', { class: 'host-card', 'data-state': state }, [
        el('div', { class: 'hc-top' }, [
          el('div', null, [
            el('h3', { text: entry.host }),
            el('p', { class: 'hc-role', text: entry.role })
          ]),
          el('span', { class: 'state ' + (last ? (last.ok ? 'up' : 'down') : 'unknown'), text: last ? (last.ok ? 'answering' : 'failing') : 'no check' })
        ]),
        el('div', { class: 'hc-check' }, [
          el('span', { class: 'hc-ms', text: last && last.ms !== null ? last.ms + ' ms' : (last ? '—' : 'Not monitored') }),
          el('span', { class: 'hc-when', text: last ? clock(last.ts) + ' · HTTP ' + (last.status || '—') : 'waiting for the first probe' })
        ])
      ]));
    });

    var tun = status.tunnel || {};
    $('#tunnel-state').textContent = tun.disabled ? 'stopped by an operator' : (tun.ready ? 'connected' : 'not ready');
    $('#tunnel-id').textContent = tun.connector_id ? tun.connector_id.slice(0, 18) + '…' : '—';
    $('#tunnel-conns').textContent = tun.connections === null ? '—' : String(tun.connections);
    $('#tunnel-requests').textContent = tun.requests === null ? 'not reported' : String(tun.requests);
    $('#tunnel-errors').textContent = tun.errors === null ? 'not reported' : String(tun.errors);
    $('#tunnel-concurrent').textContent = tun.concurrent === null ? '—' : String(tun.concurrent);
    var codes = tun.response_codes || {};
    $('#tunnel-codes').textContent = Object.keys(codes).length
      ? Object.keys(codes).map(function (code) { return code + ' × ' + codes[code]; }).join(' · ')
      : 'none yet';
    $('#tunnel-runtime').textContent = tun.memory_mb === null || tun.memory_mb === undefined
      ? '—' : tun.memory_mb + ' MB · ' + (tun.goroutines || 0) + ' goroutines';

    var table = $('#host-table tbody');
    table.textContent = '';
    (status.networking.hostnames || []).forEach(function (entry) {
      var last = entry.last;
      table.appendChild(el('tr', null, [
        el('td', { class: 'mono', text: entry.host }),
        el('td', { class: 'muted', text: entry.role }),
        el('td', { class: 'mono', text: last ? clock(last.ts) + ' · ' + ago(status.server_time ? status.server_time - last.ts : 0) : 'never' }),
        el('td', { class: 'right' }, [
          el('span', {
            class: 'state ' + (last ? (last.ok ? 'up' : 'down') : 'unknown'),
            text: last ? (last.ok ? 'HTTPS ' + last.status : 'failed') : 'Not monitored'
          })
        ]),
        el('td', { class: 'right mono', text: last && last.ms !== null ? last.ms + ' ms' : '—' })
      ]));
    });
  }

  // ------------------------------------------------------------------ logs
  function loadLogs() {
    var service = $('#log-service').value;
    var severity = $('#log-severity').value;
    var query = $('#log-query').value.trim();
    var url = '/api/logs?service=' + encodeURIComponent(service) +
      '&severity=' + encodeURIComponent(severity) + '&q=' + encodeURIComponent(query);
    api(url).then(function (data) {
      if (service !== $('#log-service').value || severity !== $('#log-severity').value ||
          query !== $('#log-query').value.trim()) { return; }
      state.logs = data;
      var host = $('#log-lines');
      var scrollTop = host.scrollTop;
      host.textContent = '';
      if (!data.lines.length) {
        host.appendChild(el('li', { class: 'empty', text: 'No lines match this filter.' }));
      } else {
        data.lines.slice().reverse().forEach(function (line) {
          host.appendChild(el('li', null, [
            el('span', { class: 'lt', text: line.ts || '—' }),
            el('span', { class: 'ls ' + line.sev, text: line.sev }),
            el('span', { class: 'lm', text: line.text })
          ]));
        });
      }
      host.scrollTop = scrollTop;
      $('#log-meta').textContent = data.matched + ' of ' + data.total + ' lines match · ' +
        (data.sizes_kb[service] === null ? 'file missing' : data.sizes_kb[service] + ' kB on disk') +
        (data.redactions ? ' · ' + data.redactions + ' credential' + (data.redactions === 1 ? '' : 's') + ' redacted' : '');
    }).catch(function (error) {
      $('#log-meta').textContent = 'Could not read logs: ' + error.message;
    });
  }

  function scheduleLogs() {
    clearInterval(state.logTimer);
    state.logTimer = null;
    if (state.route === 'logs' && $('#log-follow').checked) {
      state.logTimer = setInterval(function () {
        if (document.visibilityState === 'visible') { loadLogs(); }
      }, 15000);
    }
  }

  // -------------------------------------------------------------- settings
  function loadSettings() {
    api('/api/settings').then(function (data) {
      state.settings = data;
      var settings = data.settings;
      if (state.settingsEdit === state.settingsSaved) {
        $('#probe-hosts').checked = Boolean(settings.host_probes);
      }
      $('#probe-note').textContent = 'Public HTTPS is not monitored externally.';
      $('#db-path').textContent = data.db.path;
      $('#db-size').textContent = data.db.size_kb + ' kB';
      $('#db-samples').textContent = (data.db.rows.samples || 0) + ' rows';
      $('#db-retention').textContent = data.db.raw_retention_days + ' days raw';
      $('#db-interval').textContent = data.sample_seconds + ' s';
      $('#db-session').textContent = data.session_hours + ' h';
      if (state.status) { renderBuild(state.status); }
    }).catch(function (error) { toast('Settings: ' + error.message, 'error'); });
  }

  function renderBuild(status) {
    $('#build-device').textContent = status.host.device;
    $('#build-android').textContent = (status.host.android || '—') + ' · ' + status.host.arch;
    $('#build-kernel').textContent = status.host.kernel;
    $('#build-python').textContent = status.host.python;
    $('#build-nginx').textContent = status.versions.nginx || 'not found';
    $('#build-cloudflared').textContent = status.versions.cloudflared || 'not found';
    $('#host-line').textContent = status.host.device;
    $('#cpu-note').textContent = 'Share of one core used by nginx, the API, cloudflared and the supervisor. ' +
      'System-wide CPU is not readable on this Android build, so it is not shown.';
    $('#mem-note').textContent = 'Android memory in use on a ' + status.host.cores + '-core ' + status.host.device + '.';
  }

  function loadEvents() {
    api('/api/events?limit=40').then(function (data) {
      renderAudit($('#audit-list'), data.audits);
      renderAudit($('#settings-audit'), data.audits);
    }).catch(function () { /* the audit trail is useful, not critical */ });
  }

  // ------------------------------------------------------------ confirmation
  function confirmAction(title, body, phrase) {
    return new Promise(function (resolve) {
      var dialog = $('#confirm-dialog');
      $('#confirm-title').textContent = title;
      $('#confirm-body').textContent = body;
      var wrap = $('#confirm-phrase-wrap');
      var input = $('#confirm-input');
      var go = $('#confirm-go');
      input.value = '';
      if (phrase) {
        wrap.hidden = false;
        $('#confirm-phrase').textContent = phrase;
        go.disabled = true;
        input.oninput = function () { go.disabled = input.value.trim().toLowerCase() !== phrase; };
      } else {
        wrap.hidden = true;
        go.disabled = false;
        input.oninput = null;
      }
      dialog.returnValue = '';
      dialog.showModal();
      dialog.addEventListener('close', function onClose() {
        dialog.removeEventListener('close', onClose);
        resolve(dialog.returnValue === 'confirm');
      }, { once: true });
    });
  }

  function runAction(action, phrase) {
    var labels = {
      'server-restart': ['Restart the server?', 'nginx reloads and the API restarts, so this page drops out for a few seconds.'],
      'server-stop': ['Stop the server?', 'The site, this console and the API stop. Nothing restarts them until you run serverctl start on the phone.'],
      'tunnel-start': ['Start the tunnel?', 'The connector starts and the public hostnames come back.'],
      'tunnel-stop': ['Stop the tunnel?', 'Both public hostnames go offline until the tunnel is started again.'],
      'tunnel-restart': ['Restart the tunnel?', 'The connector reconnects; the public hostnames are offline for a few seconds.']
    };
    var copy = labels[action] || ['Confirm action?', 'This changes the server.'];
    confirmAction(copy[0], copy[1], phrase).then(function (ok) {
      if (!ok) { return; }
      api('/api/action', { method: 'POST', body: JSON.stringify({ action: action }) })
        .then(function (data) {
          toast((data.label || action) + ': ' + (data.output || 'done'), 'ok');
          // The server and tunnel actions drop this page's own connection, so
          // give the connector time to come back before asking for data again.
          setTimeout(refreshStatus, /^(server-|tunnel-(stop|restart))/.test(action) ? 7000 : 1800);
          loadEvents();
        })
        .catch(function (error) { toast('Action failed: ' + error.message, 'error'); });
    });
  }

  // xterm renders the PTY stream; session authentication remains server-side.
  var terminalId = null;
  var terminalStream = null;
  var terminalEmulator = null;
  var terminalFit = null;
  var terminalPending = '';
  var terminalSending = false;
  var terminalResize = Promise.resolve();

  // xterm's screen-reader mode keeps a parallel DOM copy of the buffer, which is
  // the accessible path but much slower to render on large output. It stays off
  // unless the operator asks for it, and the choice survives reloads.
  var A11Y_KEY = 'galaxy-screen-reader-mode';
  function a11yEnabled() {
    try { return window.localStorage.getItem(A11Y_KEY) === '1'; }
    catch (error) { return false; }
  }
  function terminalRequest(path, body) {
    return api('/api/terminal/' + path, { method: 'POST', body: JSON.stringify(body) });
  }
  function closeTerminal() {
    var id = terminalId;
    terminalId = null;
    terminalPending = '';
    terminalSending = false;
    if (terminalStream) { terminalStream.close(); terminalStream = null; }
    if (terminalEmulator) { terminalEmulator.dispose(); terminalEmulator = null; terminalFit = null; }
    $('#terminal-session').hidden = true;
    $('#terminal-lock').hidden = false;
    $('#terminal-status').textContent = 'Terminal locked.';
    if (state.route === 'terminal') { $('#terminal-password').focus(); }
    if (id) { terminalRequest('close', { id: id }).catch(function () {}); }
  }
  // One long-lived stream carries shell output, so echo no longer waits for a
  // 500 ms poll tick plus a round trip.
  function startTerminalStream() {
    var id = terminalId;
    if (!id) { return; }
    if (terminalStream) { terminalStream.close(); }
    var stream = new EventSource('/api/terminal/stream?id=' + encodeURIComponent(id));
    terminalStream = stream;
    stream.onmessage = function (event) {
      if (terminalId !== id || !terminalEmulator) { return; }
      var binary = atob(event.data);
      var bytes = new Uint8Array(binary.length);
      for (var i = 0; i < binary.length; i++) { bytes[i] = binary.charCodeAt(i); }
      terminalEmulator.write(bytes);
    };
    stream.onerror = function () {
      stream.close();
      if (terminalId !== id) { return; }
      closeTerminal();
      $('#terminal-status').textContent = 'Terminal disconnected: connection closed';
    };
  }

  // Keystrokes are coalesced while a write is in flight. Sending one request
  // per character serialised a whole word behind one round trip each.
  function flushTerminalInput() {
    if (!terminalId || terminalSending || !terminalPending) { return; }
    var id = terminalId;
    var chunk = terminalPending.slice(0, 2000);
    terminalPending = terminalPending.slice(chunk.length);
    terminalSending = true;
    terminalRequest('input', { id: id, input: chunk }).then(function () {
      terminalSending = false;
      if (terminalId === id) { flushTerminalInput(); }
    }).catch(function (error) {
      terminalSending = false;
      if (terminalId === id) { closeTerminal(); $('#terminal-status').textContent = 'Terminal disconnected: ' + error.message; }
    });
  }

  function fitTerminal() {
    // A hidden xterm has no useful dimensions; keep the shell's current size
    // until its view is visible again.
    if (state.route !== 'terminal' || !terminalId || !terminalFit || !terminalEmulator) { return; }
    terminalFit.fit();
    var id = terminalId;
    var cols = terminalEmulator.cols, rows = terminalEmulator.rows;
    if (cols < 2 || rows < 2 || cols > 500 || rows > 300) { return; }
    terminalResize = terminalResize.catch(function () {}).then(function () {
      if (terminalId !== id) { return; }
      return terminalRequest('resize', { id: id, cols: cols, rows: rows });
    }).catch(function (error) { if (terminalId === id) { $('#terminal-status').textContent = 'Resize failed: ' + error.message; } });
  }

  // ------------------------------------------------------------------ data
  function loadTimeline(range) {
    return api('/api/timeline?range=' + encodeURIComponent(range)).then(function (data) {
      state.timelines[range] = data;
      if (range === state.range) { renderCharts(); renderOverviewEvents(); }
      return data;
    }).catch(function (error) {
      toast('Timeline: ' + error.message, 'error');
    });
  }

  function renderStatus(status) {
    state.status = status;
    renderHealth(status);
    renderComponents(status);
    renderAlerts(status);
    renderKpis(status);
    renderOverviewServices(status);
    renderServices(status);
    renderNetworking(status);
    renderBuild(status);
  }

  function refreshStatus() {
    if (state.loading) { return; }
    state.loading = true;
    api('/api/status').then(function (status) {
      renderStatus(status);
    }).catch(function (error) {
      toast('Status: ' + error.message, 'error');
      var pill = $('#health');
      pill.dataset.state = 'crit';
      pill.textContent = 'API unreachable';
    }).then(function () { state.loading = false; });
  }

  function setRange(range) {
    state.range = range;
    $$('.segmented button').forEach(function (button) {
      button.classList.toggle('on', button.dataset.range === range);
    });
    loadTimeline(range);
  }

  // ------------------------------------------------------------------- init
  function wire() {
    window.addEventListener('hashchange', function () { show(routeFromHash()); });
    $('#menu').addEventListener('click', function () {
      if ($('#sidebar').classList.contains('open')) { closeDrawer(); } else { openDrawer(); }
    });
    $('#scrim').addEventListener('click', closeDrawer);
    $('#refresh').addEventListener('click', function () {
      refreshStatus();
      loadTimeline(state.range);
      loadEvents();
      if (state.route === 'logs') { loadLogs(); }
      toast('Refreshed');
    });

    $$('.segmented').forEach(function (group) {
      group.addEventListener('click', function (event) {
        var button = event.target.closest('button[data-range]');
        if (button) { setRange(button.dataset.range); }
      });
    });

    document.addEventListener('click', function (event) {
      var button = event.target.closest('[data-action]');
      if (!button) { return; }
      event.preventDefault();
      runAction(button.dataset.action, button.dataset.phrase || '');
    });

    $('#terminal-login').addEventListener('submit', function (event) {
      event.preventDefault();
      var field = $('#terminal-password');
      var button = $('#terminal-login button');
      var password = field.value;
      field.value = '';
      button.disabled = true;
      terminalRequest('open', { password: password }).then(function (data) {
        terminalId = data.id;
        $('#terminal-lock').hidden = true;
        $('#terminal-session').hidden = false;
        $('#terminal-status').textContent = 'Terminal open as the Termux user.';
        terminalEmulator = new Terminal({ cursorBlink: false, fontFamily: 'ui-monospace, Consolas, monospace', fontSize: 14,
          scrollback: 2000, screenReaderMode: a11yToggle.checked, theme: { background: '#070a0e', foreground: '#e9f1f6', cursor: '#c3f451' } });
        terminalFit = new FitAddon.FitAddon();
        terminalEmulator.loadAddon(terminalFit);
        terminalEmulator.open($('#terminal-output'));
        terminalEmulator.onData(function (input) {
          if (!terminalId) { return; }
          terminalPending += input;
          flushTerminalInput();
        });
        fitTerminal();
        if (state.route === 'terminal') { terminalEmulator.focus(); }
        startTerminalStream();
      }).catch(function (error) {
        if (terminalId) { closeTerminal(); }
        $('#terminal-status').textContent = error.message;
      }).finally(function () { button.disabled = false; });
    });
    $('#terminal-close').addEventListener('click', closeTerminal);
    var a11yToggle = $('#terminal-a11y');
    a11yToggle.checked = a11yEnabled();
    a11yToggle.addEventListener('change', function () {
      try { window.localStorage.setItem(A11Y_KEY, a11yToggle.checked ? '1' : '0'); }
      catch (error) { /* private mode: the choice just does not persist */ }
      if (terminalEmulator) { terminalEmulator.options.screenReaderMode = a11yToggle.checked; }
    });
    window.addEventListener('pagehide', function () {
      if (terminalId) {
        fetch('/api/terminal/close', { method: 'POST', credentials: 'same-origin', keepalive: true,
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: terminalId }) });
      }
    });

    $('#log-reload').addEventListener('click', loadLogs);
    $('#log-service').addEventListener('change', loadLogs);
    $('#log-severity').addEventListener('change', loadLogs);
    $('#log-follow').addEventListener('change', scheduleLogs);
    var searchTimer = null;
    $('#log-query').addEventListener('input', function () {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(loadLogs, 350);
    });

    $('#probe-hosts').addEventListener('change', function () { state.settingsEdit++; });
    $('#probe-form').addEventListener('submit', function (event) {
      event.preventDefault();
      var savedEdit = state.settingsEdit;
      api('/api/settings', {
        method: 'POST',
        body: JSON.stringify({
          host_probes: $('#probe-hosts').checked
        })
      }).then(function () {
        state.settingsSaved = savedEdit;
        toast('Probe settings saved');
        loadSettings();
        refreshStatus();
      }).catch(function (error) { toast(error.message, 'error'); });
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && $('#sidebar').classList.contains('open')) { closeDrawer(); }
    });

    var resizeTimer = null;
    window.addEventListener('resize', function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () { renderCharts(); fitTerminal(); }, 160);
    });

    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') { refreshStatus(); }
    });
  }

  function start() {
    wire();
    show(routeFromHash());
    refreshStatus();
    loadTimeline(state.range);
    loadEvents();
    loadSettings();
    state.statusTimer = setInterval(function () {
      if (document.visibilityState === 'visible') { refreshStatus(); }
    }, 10000);
    setInterval(function () {
      if (document.visibilityState === 'visible' && state.route !== 'logs') { loadTimeline(state.range); }
    }, 60000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
