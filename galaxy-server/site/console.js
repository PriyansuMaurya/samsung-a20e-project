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
    device: ['Device', 'Cameras, microphone, accessories and media on the phone.'],
    music: ['Music', 'Search and stream Creative Commons tracks from Jamendo.'],
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
    if (!d) { return '-'; }
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  function hhmm(ts) {
    var d = toDate(ts);
    if (!d) { return '-'; }
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

  function gb(value) { return value === null || value === undefined ? '-' : value.toFixed(1) + ' GB'; }

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
    if (route === 'terminal' && activeTerminal) {
      window.requestAnimationFrame(function () {
        if (!activeTerminal) { return; }
        fitTerminal();
        activeTerminal.emulator.focus();
      });
    }
    if (route === 'metrics') { renderCharts(); }
    if (route === 'logs') { loadLogs(); }
    if (route === 'settings') { loadSettings(); loadEvents(); loadTelemetry(); }
    if (route === 'services' || route === 'networking') { loadEvents(); }
    // The player keeps its tracks (and any audio playing) after the first
    // load, so returning to this view does not restart the stream.
    if (route === 'music' && !music.loaded) { loadMusic(); }
    if (route === 'device') {
      loadDevice();
    } else {
      // Leaving the view releases the camera and the microphone, so the phone
      // is not held open by a tab the operator forgot about.
      if (deviceState.cameraOn) { stopCamera(true); }
      if (deviceState.listening) { stopListening(); }
    }
  }

  function routeFromHash() {
    var match = /^#\/([a-z]+)/.exec(window.location.hash || '');
    return match ? match[1] : 'overview';
  }

  // The drawer sits before the shell in the document, so a keyboard user who
  // opens it would otherwise tab through the page hidden behind the scrim.
  function openDrawer() {
    $('#sidebar').classList.add('open');
    $('#scrim').hidden = false;
    $('#menu').setAttribute('aria-expanded', 'true');
    var first = $('#nav a');
    if (first) { first.focus(); }
  }
  function closeDrawer() {
    var wasOpen = $('#sidebar').classList.contains('open');
    $('#sidebar').classList.remove('open');
    $('#scrim').hidden = true;
    $('#menu').setAttribute('aria-expanded', 'false');
    if (wasOpen) { $('#menu').focus(); }
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
      el('td', { class: 'right mono', text: service.uptime || '-' }),
      el('td', { class: 'right mono', text: service.rss_mb === null ? '-' : service.rss_mb + ' MB' })
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
        el('span', { class: 'when', text: when ? pad(when.getHours()) + ':' + pad(when.getMinutes()) : '-' }),
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
      var control = '-';
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
        el('td', { class: 'right mono', text: service.uptime || '-' }),
        el('td', { class: 'right mono', text: String(service.restarts_24h || 0) }),
        el('td', { class: 'right mono', text: service.rss_mb === null ? '-' : service.rss_mb + ' MB' }),
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
        el('span', { class: 'when', text: when ? pad(when.getHours()) + ':' + pad(when.getMinutes()) : '-' }),
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
          el('span', { class: 'hc-ms', text: last && last.ms !== null ? last.ms + ' ms' : (last ? '-' : 'Not monitored') }),
          el('span', { class: 'hc-when', text: last ? clock(last.ts) + ' · HTTP ' + (last.status || '-') : 'waiting for the first probe' })
        ])
      ]));
    });

    var tun = status.tunnel || {};
    $('#tunnel-state').textContent = tun.disabled ? 'stopped by an operator' : (tun.ready ? 'connected' : 'not ready');
    $('#tunnel-id').textContent = tun.connector_id ? tun.connector_id.slice(0, 18) + '…' : '-';
    $('#tunnel-conns').textContent = tun.connections === null ? '-' : String(tun.connections);
    $('#tunnel-requests').textContent = tun.requests === null ? 'not reported' : String(tun.requests);
    $('#tunnel-errors').textContent = tun.errors === null ? 'not reported' : String(tun.errors);
    $('#tunnel-concurrent').textContent = tun.concurrent === null ? '-' : String(tun.concurrent);
    var codes = tun.response_codes || {};
    $('#tunnel-codes').textContent = Object.keys(codes).length
      ? Object.keys(codes).map(function (code) { return code + ' × ' + codes[code]; }).join(' · ')
      : 'none yet';
    $('#tunnel-runtime').textContent = tun.memory_mb === null || tun.memory_mb === undefined
      ? '-' : tun.memory_mb + ' MB · ' + (tun.goroutines || 0) + ' goroutines';

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
        el('td', { class: 'right mono', text: last && last.ms !== null ? last.ms + ' ms' : '-' })
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
            el('span', { class: 'lt', text: line.ts || '-' }),
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

  function loadTelemetry() {
    api('/api/telemetry').then(function (data) {
      data = data || {};
      var labels = {off: 'Off', error: 'Configuration error', idle: 'Waiting for first batch',
                    ok: 'Exporting', degraded: 'Degraded'};
      $('#telemetry-state').textContent = labels[data.state] || 'Unknown';
      $('#telemetry-dataset').textContent = data.dataset
        ? data.dataset + ' · ' + (data.endpoint || '') : 'not configured';
      $('#telemetry-exported').textContent = (data.exported || 0) + ' events · ' +
        (data.batches || 0) + ' batches · ' + (data.failed || 0) + ' failed';
      $('#telemetry-queued').textContent = (data.queued || 0) + ' queued · ' +
        (data.dropped || 0) + ' dropped';
      $('#telemetry-spool').textContent = (data.spool_events || 0) + ' events · ' +
        (data.spool_kb || 0) + ' kB';
      $('#telemetry-retention').textContent = data.retention_days
        ? data.retention_days + ' days' : '-';
      $('#telemetry-last-success').textContent = data.last_success_at || 'never';
      $('#telemetry-last-error').textContent = data.last_error || 'none';
      var note = 'Events are sent to Axiom as well as kept in the local files.';
      if (data.config_error) { note = data.config_error; }
      else if (!data.enabled) { note = 'Remote export is off. Run serverctl telemetry-setup on the phone to enable it.'; }
      else if (data.state === 'degraded') { note = 'The last export failed; events are buffered and retried.'; }
      $('#telemetry-note').textContent = note;
    }).catch(function () { /* telemetry is optional; leave the card at its placeholders */ });
  }

  function renderBuild(status) {
    $('#build-device').textContent = status.host.device;
    $('#build-android').textContent = (status.host.android || '-') + ' · ' + status.host.arch;
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
  // Each tab owns one shell. Their ids are kept in sessionStorage so a reload
  // can reattach to the same shells instead of opening new ones, and the server
  // keeps a little scrollback for that redraw.
  var terminals = [];
  var activeTerminal = null;
  var terminalSecret = '';
  var TABS_KEY = 'galaxy-terminal-tabs';
  var TERMINAL_MAX = 4;

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

  function rememberTabs() {
    try {
      window.sessionStorage.setItem(TABS_KEY, JSON.stringify({
        active: activeTerminal ? activeTerminal.id : null,
        tabs: terminals.map(function (tab) {
          return { id: tab.id, title: tab.title };
        })
      }));
    } catch (error) { /* private mode: tabs last for this page only */ }
  }

  function savedTabs() {
    try {
      var saved = JSON.parse(window.sessionStorage.getItem(TABS_KEY));
      // Older pages stored a bare list of tabs.
      if (Array.isArray(saved)) { saved = { active: null, tabs: saved }; }
      if (!saved || !Array.isArray(saved.tabs)) { return { active: null, tabs: [] }; }
      return {
        active: typeof saved.active === 'string' ? saved.active : null,
        tabs: saved.tabs.filter(function (tab) {
          return tab && typeof tab.id === 'string' && typeof tab.title === 'string';
        })
      };
    } catch (error) { return { active: null, tabs: [] }; }
  }

  function makePane(tab) {
    var pane = document.createElement('div');
    pane.className = 'terminal-pane';
    pane.hidden = true;
    $('#terminal-output').appendChild(pane);
    var emulator = new Terminal({ cursorBlink: false, fontFamily: 'ui-monospace, Consolas, monospace', fontSize: 14,
      scrollback: 2000, screenReaderMode: a11yEnabled(),
      theme: { background: '#070a0e', foreground: '#e9f1f6', cursor: '#c3f451' } });
    var fit = new FitAddon.FitAddon();
    emulator.loadAddon(fit);
    emulator.open(pane);
    emulator.onData(function (input) {
      if (!tab.id) { return; }
      tab.pending += input;
      flushTerminalInput(tab);
    });
    tab.pane = pane;
    tab.emulator = emulator;
    tab.fit = fit;
    return tab;
  }

  function renderTabs() {
    var bar = $('#terminal-tabs');
    bar.textContent = '';
    terminals.forEach(function (tab) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'terminal-tab' + (tab === activeTerminal ? ' on' : '');
      button.setAttribute('role', 'tab');
      button.setAttribute('aria-selected', tab === activeTerminal ? 'true' : 'false');
      button.dataset.terminal = tab.id;
      button.appendChild(document.createTextNode(tab.title));
      var close = document.createElement('span');
      close.className = 'terminal-tab-close';
      close.setAttribute('aria-hidden', 'true');
      close.textContent = '×';
      button.appendChild(close);
      bar.appendChild(button);
    });
    $('#terminal-new').disabled = terminals.length >= TERMINAL_MAX;
  }

  function showTerminalView() {
    var hasTabs = terminals.length > 0;
    $('#terminal-lock').hidden = hasTabs;
    $('#terminal-session').hidden = !hasTabs;
  }

  function activate(id) {
    var tab = terminals.filter(function (item) { return item.id === id; })[0] || null;
    activeTerminal = tab;
    terminals.forEach(function (item) { item.pane.hidden = item !== tab; });
    renderTabs();
    rememberTabs();
    if (tab && state.route === 'terminal') {
      window.requestAnimationFrame(function () { fitTerminal(); tab.emulator.focus(); });
    }
  }

  function stopTerminalStream(tab) {
    if (tab.stream) { tab.stream.close(); tab.stream = null; }
    clearTimeout(tab.retry);
    tab.retry = null;
  }

  // The pty lives on the server, not in the socket: a dropped stream is a
  // temporary state, so ask whether the shell is still there and reattach
  // instead of forgetting the tab. Forgetting it also lost the shell id, which
  // is what made the next reload fall back to the password form.
  function reattachStream(tab) {
    var id = tab.id;
    if (!id || tab.retry) { return; }
    var delay = Math.min(600 * Math.max(tab.offline || 1, 1), 10000);
    tab.retry = setTimeout(function () {
      tab.retry = null;
      if (tab.id !== id) { return; }
      api('/api/terminal/list').then(function (data) {
        if (tab.id !== id) { return; }
        var alive = (data.terminals || []).some(function (item) { return item.id === id; });
        if (!alive) {
          dropTerminal(tab, false);
          toast('Terminal session ended', 'error');
          return;
        }
        // A fresh attach replays the server's whole buffer, so clear the screen
        // first or that replay doubles the text already on it.
        if (tab.emulator) { tab.emulator.reset(); }
        startTerminalStream(tab);
      }).catch(function () { reattachStream(tab); });
    }, delay);
  }

  function startTerminalStream(tab) {
    if (!tab.id) { return; }
    stopTerminalStream(tab);
    var id = tab.id;
    // One long-lived stream carries shell output, so echo does not wait for a
    // poll tick plus a round trip. The server replays recent output first.
    var stream = new EventSource('/api/terminal/stream?id=' + encodeURIComponent(id));
    tab.stream = stream;
    stream.onmessage = function (event) {
      if (tab.id !== id || !tab.emulator) { return; }
      var binary = atob(event.data);
      var bytes = new Uint8Array(binary.length);
      for (var i = 0; i < binary.length; i++) { bytes[i] = binary.charCodeAt(i); }
      tab.emulator.write(bytes);
    };
    stream.onopen = function () { if (tab.id === id) { tab.offline = 0; } };
    stream.onerror = function () {
      if (tab.id !== id || tab.stream !== stream) { return; }
      stream.close();
      tab.stream = null;
      tab.offline = (tab.offline || 0) + 1;
      reattachStream(tab);
    };
  }

  // Keystrokes are coalesced while a write is in flight. Sending one request
  // per character serialised a whole word behind one round trip each.
  function flushTerminalInput(tab) {
    if (!tab.id || tab.sending || !tab.pending) { return; }
    var id = tab.id;
    var chunk = tab.pending.slice(0, 2000);
    tab.pending = tab.pending.slice(chunk.length);
    tab.sending = true;
    terminalRequest('input', { id: id, input: chunk }).then(function () {
      tab.sending = false;
      flushTerminalInput(tab);
    }).catch(function (error) {
      tab.sending = false;
      if (tab.id !== id) { return; }
      if (/closed|ended/i.test(error.message)) {
        dropTerminal(tab, false);
        toast('Terminal session ended', 'error');
        return;
      }
      // The shell outlives a failed write, so the tab stays; the stream retry
      // above reattaches and the missing keystrokes are not worth losing it.
      toast('Terminal: ' + error.message, 'error');
    });
  }

  function fitTerminal() {
    // A hidden xterm has no useful dimensions; keep the shell's current size
    // until its view is visible again.
    var tab = activeTerminal;
    if (state.route !== 'terminal' || !tab || !tab.fit || !tab.emulator || tab.pane.hidden) { return; }
    tab.fit.fit();
    var id = tab.id;
    var cols = tab.emulator.cols, rows = tab.emulator.rows;
    if (cols < 2 || rows < 2 || cols > 500 || rows > 300) { return; }
    tab.resize = tab.resize.catch(function () {}).then(function () {
      if (!tab.id || tab.id !== id) { return; }
      return terminalRequest('resize', { id: id, cols: cols, rows: rows });
    }).catch(function (error) {
      if (tab.id === id) { toast('Resize failed: ' + error.message, 'error'); }
    });
  }

  function dropTerminal(tab, closeRemote) {
    var index = terminals.indexOf(tab);
    if (index === -1) { return; }
    var id = tab.id;
    terminals.splice(index, 1);
    tab.id = null;
    stopTerminalStream(tab);
    if (tab.emulator) { tab.emulator.dispose(); tab.emulator = null; }
    if (tab.pane && tab.pane.parentNode) { tab.pane.parentNode.removeChild(tab.pane); }
    rememberTabs();
    if (activeTerminal === tab) {
      activeTerminal = terminals[terminals.length - 1] || null;
      if (activeTerminal) { activate(activeTerminal.id); }
    }
    showTerminalView();
    renderTabs();
    if (!activeTerminal && state.route === 'terminal') { $('#terminal-password').focus(); }
    if (closeRemote && id) { terminalRequest('close', { id: id }).catch(function () {}); }
  }

  function nextShellTitle() {
    var number = 1;
    terminals.forEach(function (tab) {
      var match = /^Shell (\d+)$/.exec(tab.title);
      if (match) { number = Math.max(number, parseInt(match[1], 10) + 1); }
    });
    return 'Shell ' + number;
  }

  function openTerminal(password) {
    if (terminals.length >= TERMINAL_MAX) {
      return Promise.reject(new Error('Close a shell first (' + TERMINAL_MAX + ' max)'));
    }
    return terminalRequest('open', { password: password }).then(function (data) {
      var tab = { id: data.id, title: nextShellTitle(), pending: '', sending: false, resize: Promise.resolve() };
      terminals.push(tab);
      makePane(tab);
      showTerminalView();
      activate(tab.id);
      startTerminalStream(tab);
      rememberTabs();
      return tab;
    });
  }

  function askTerminalPassword() {
    $('#shell-password').value = '';
    $('#shell-dialog').showModal();
    $('#shell-password').focus();
  }

  // While the page asks which shells are still alive, do not show the password
  // form: to someone who was already unlocked a reload must not look like a
  // logout.
  function setRestoring(on) {
    $('#terminal-login').hidden = on;
    $('#terminal-reconnecting').hidden = !on;
  }

  // Reattach after a reload: sessionStorage names the shells this tab had, and
  // the server confirms which are still alive before any stream opens.
  function restoreTerminals() {
    var saved = savedTabs();
    if (!saved.tabs.length) { return; }
    setRestoring(true);
    api('/api/terminal/list').then(function (data) {
      var live = {};
      (data.terminals || []).forEach(function (item) { live[item.id] = true; });
      var restored = saved.tabs.filter(function (item) { return live[item.id]; });
      restored.forEach(function (item) {
        var tab = { id: item.id, title: item.title, pending: '', sending: false, resize: Promise.resolve() };
        terminals.push(tab);
        makePane(tab);
        // The server replays each shell's recent output when its stream opens.
        startTerminalStream(tab);
      });
      showTerminalView();
      renderTabs();
      rememberTabs();
      if (!terminals.length) { return; }
      var reopen = terminals.filter(function (tab) { return tab.id === saved.active; })[0];
      activate((reopen || terminals[0]).id);
    }).catch(function (error) {
      toast('Terminal: ' + error.message, 'error');
    }).then(function () { setRestoring(false); });
  }

  // ------------------------------------------------------------------ device
  var deviceState = {
    summary: null, photos: [], recordings: [], media: [], videos: [],
    cameraOn: false, torch: false, listening: null, recordStarted: 0,
    recordTimer: null, brightnessTimer: null, camRetry: null,
    videoStarted: 0, videoTimer: null
  };

  function deviceControl(action, payload) {
    return api('/api/device/control', {
      method: 'POST',
      body: JSON.stringify({ action: action, payload: payload || {} })
    });
  }

  function deviceBanner(kind, text, detail) {
    var banner = $('#device-banner');
    banner.dataset.state = kind;
    $('#device-banner-text').textContent = text;
    $('#device-banner-detail').textContent = detail || '';
  }

  function loadDevice() {
    if (state.route !== 'device') { return; }
    api('/api/device/summary').then(function (summary) {
      deviceState.summary = summary;
      if (!summary.paired) {
        $('#device-pair-form').hidden = false;
        deviceBanner('warn', 'Control app not paired', 'Open the Galaxy Device app and pair.');
        return;
      }
      if (summary.error) {
        deviceBanner('crit', 'Control app unreachable', summary.error);
        return;
      }
      var health = summary.health || {};
      var camera = health.camera_open ? 'camera ' + health.camera_open : 'camera idle';
      if (health.ok) {
        deviceBanner('ok', 'Control app running', camera);
      } else {
        deviceBanner('crit', 'Control app not answering', summary.error || '');
        return;
      }
      var caps = summary.capabilities || {};
      $('#device-pair-form').hidden = true;
      renderCapabilities(caps);
      renderRotation(caps.rotation || {});
      renderPower(summary);
      renderVolume(summary.volume || {});
      loadSensors();
      loadPhotos();
      loadRecordings();
      loadMedia();
      loadVideos();
    }).catch(function (error) {
      deviceBanner('crit', 'Control app unreachable', error.message);
    });
  }

  function renderCapabilities(caps) {
    var select = $('#cam-id');
    var previous = select.value;
    select.textContent = '';
    (caps.cameras || []).forEach(function (camera) {
      select.appendChild(el('option', {
        value: camera.id,
        text: camera.facing + ' (id ' + camera.id + ')' + (camera.flash ? ' · flash' : '')
      }));
    });
    if (!select.options.length) {
      select.appendChild(el('option', { value: '0', text: 'no camera reported' }));
    }
    if (previous) { select.value = previous; }
    renderCameraSizes(caps);

    var brightness = $('#brightness');
    if (typeof caps.brightness === 'number' && caps.brightness >= 0) {
      brightness.value = caps.brightness;
      $('#brightness-value').textContent = caps.brightness;
    }
    var perms = caps.permissions || {};
    $('#dev-perms').textContent = (perms.camera ? 'camera' : 'no camera') + ' · ' +
      (perms.microphone ? 'microphone' : 'no microphone') +
      (caps.brightness_writable ? ' · brightness' : ' · brightness locked');
  }

  function renderCameraSizes(caps) {
    var select = $('#cam-size');
    var wanted = select.value;
    select.textContent = '';
    var picked = (caps.cameras || []).filter(function (camera) {
      return camera.id === $('#cam-id').value;
    })[0] || (caps.cameras || [])[0];
    var sizes = (picked && picked.jpeg_sizes) || [];
    // Short label: the popup is narrow once the card is split in two, and the
    // full list of sizes is one click away in the same menu.
    select.appendChild(el('option', { value: '', text: 'auto' }));
    sizes.slice(0, 12).forEach(function (size) {
      select.appendChild(el('option', { value: size, text: size }));
    });
    select.value = wanted && sizes.indexOf(wanted) >= 0 ? wanted : '';
  }

  function renderRotation(rotation) {
    var select = $('#rotation');
    if (rotation.automatic) {
      select.value = 'auto';
    } else {
      select.value = String(rotation.user_degrees || 0);
    }
    select.disabled = rotation.writable === false;
    $('#rotation-state').textContent = rotation.writable === false ? 'locked'
      : (rotation.automatic ? 'auto' : (rotation.user_degrees || 0) + '°');
  }

  function setRotation(value) {
    var payload = value === 'auto' ? { degrees: null } : { degrees: Number(value) };
    deviceControl('rotation', payload).then(function (rotation) {
      renderRotation(rotation);
      toast(value === 'auto' ? 'Rotation follows the phone' : 'Screen turned to ' + value + '°');
    }).catch(function (error) { toast('Rotation: ' + error.message, 'error'); });
  }

  function mb(value) {
    if (typeof value !== 'number') { return '-'; }
    if (value >= 1024 * 1024 * 1024) { return (value / (1024 * 1024 * 1024)).toFixed(1) + ' GB'; }
    // Recordings are often under a megabyte, where "0 MB" said nothing.
    if (value < 1024 * 1024) { return Math.round(value / 1024) + ' kB'; }
    return Math.round(value / (1024 * 1024)) + ' MB';
  }

  function duration(seconds) {
    var total = Math.max(0, Math.round(seconds || 0));
    if (total < 60) { return total + 's'; }
    var minutes = Math.floor(total / 60);
    if (minutes < 60) { return minutes + 'm ' + (total % 60) + 's'; }
    return Math.floor(minutes / 60) + 'h ' + (minutes % 60) + 'm';
  }

  function renderPower(summary) {
    var battery = summary.battery || {};
    var storage = summary.storage || {};
    var folders = storage.folders || {};
    var health = summary.health || {};
    var percent = typeof battery.percentage === 'number' ? battery.percentage : null;
    $('#dev-battery').textContent = percent === null ? '-' : percent + '%';
    $('#dev-battery-note').textContent = [
      battery.plugged ? 'charging' : 'on battery',
      typeof battery.voltage_mv === 'number' ? battery.voltage_mv + ' mV' : ''
    ].filter(Boolean).join(' · ');
    $('#battery-meter').style.width = percent === null
      ? '0' : Math.max(2, Math.min(100, percent)) + '%';
    $('#dev-free').textContent = mb(storage.free_bytes);
    $('#dev-free-note').textContent = 'free of ' + mb(storage.total_bytes);
    $('#dev-temp').textContent = typeof battery.temperature_c === 'number'
      ? battery.temperature_c.toFixed(1) + ' °C' : '-';
    $('#dev-photos').textContent = ((folders.photos || {}).count || 0) + ' · ' +
      mb((folders.photos || {}).bytes || 0);
    $('#dev-recordings').textContent = ((folders.recordings || {}).count || 0) + ' · ' +
      mb((folders.recordings || {}).bytes || 0);
    $('#dev-uptime').textContent = duration(health.uptime_s);
    renderConnectivity(summary.connectivity || {});
  }

  function renderConnectivity(net) {
    // The tile carries the two things worth glancing at; the full sentence
    // stays under Details rather than stretching a status tile across a line.
    var kinds = [];
    if (net.wifi) { kinds.push('Wi-Fi'); }
    if (net.cellular) { kinds.push('Cellular'); }
    if (!kinds.length) { kinds.push('Offline'); }
    if (net.metered) { kinds.push('metered'); }
    if (net.validated) { kinds.push('validated'); }
    var speed = net.down_mbps ? net.down_mbps + '/' + (net.up_mbps || 0) + ' Mbps'
      : net.link_mbps ? net.link_mbps + ' Mbps link' : '';
    var wifi = net.wifi_enabled === undefined ? '' : (net.wifi_enabled ? 'Wi-Fi on' : 'Wi-Fi off');
    $('#dev-network').textContent = kinds[0];
    $('#dev-network-note').textContent = [net.ssid, speed,
      net.rssi_dbm ? net.rssi_dbm + ' dBm' : ''].filter(Boolean).join(' · ') ||
      (net.interface || 'no address');
    $('#dev-network-detail').textContent = kinds.join(' · ') +
      (net.ssid ? ' · ' + net.ssid : '') +
      (speed ? ' · ' + speed : '') + (wifi ? ' · ' + wifi : '') +
      (net.rssi_dbm ? ' · ' + net.rssi_dbm + ' dBm' : '');
    var addresses = (net.addresses || []).filter(function (value) {
      return value.indexOf('fe80') !== 0;
    });
    $('#dev-address').textContent = addresses.join(', ') ||
      (net.interface ? net.interface + ' (no address)' : '-');
    $('#dev-bluetooth').textContent = net.bluetooth_enabled === undefined ? '-'
      : (net.bluetooth_enabled ? 'on' : 'off');
    $('#dev-airplane').textContent = net.airplane_mode ? 'on' : 'off';
  }

  var VOLUME_LABELS = {
    music: 'Media', ring: 'Ring', alarm: 'Alarm',
    notification: 'Notifications', call: 'Call'
  };

  function renderVolume(volume) {
    var host = $('#volume-sliders');
    var streams = volume.streams || {};
    var names = Object.keys(streams);
    if (!names.length) {
      if (!host.children.length) {
        host.appendChild(el('p', { class: 'empty', text: 'Volume is unavailable.' }));
      }
      return;
    }
    host.textContent = '';
    names.forEach(function (name) {
      var stream = streams[name];
      var output = el('output', { text: stream.index + '/' + stream.max });
      var input = el('input', {
        type: 'range', min: '0', max: String(stream.max), step: '1',
        value: String(stream.index),
        'aria-label': (VOLUME_LABELS[name] || name) + ' volume'
      });
      input.addEventListener('input', function () {
        output.textContent = input.value + '/' + stream.max;
      });
      input.addEventListener('change', function () {
        deviceControl('volume', { stream: name, index: Number(input.value) })
          .catch(function (error) { toast('Volume: ' + error.message, 'error'); });
      });
      host.appendChild(el('div', { class: 'slider-row' }, [
        el('label', { text: VOLUME_LABELS[name] || name }), input, output
      ]));
    });
  }

  function renderSensors(payload) {
    var body = $('#sensor-table').querySelector('tbody');
    body.textContent = '';
    var names = Object.keys(payload || {});
    if (!names.length) {
      body.appendChild(el('tr', {}, [el('td', { colspan: '2', class: 'empty',
        text: 'No sensor answered.' })]));
      return;
    }
    names.forEach(function (name) {
      var sensor = payload[name] || {};
      var values = (sensor.values || []).join(', ') || '-';
      // "accelerometer LSM6DSL Accelerometer" said it twice: the row leads with
      // the human name, the chip model follows without repeating it.
      var label = name.charAt(0).toUpperCase() + name.slice(1);
      var model = (sensor.name || '').replace(
        new RegExp('\\s*' + name.replace(/[^a-z0-9]/gi, '') + '$', 'i'), '').trim();
      body.appendChild(el('tr', {}, [
        el('td', {}, [el('strong', { text: label }),
          model ? el('span', { class: 'muted', text: ' ' + model }) : null]),
        el('td', { class: 'right mono', text: values })
      ]));
    });
  }

  function loadSensors() {
    api('/api/device/sensors').then(renderSensors).catch(function () {
      /* A slow sensor read is not worth a toast on every poll. */
    });
  }

  function renderPhotos(files) {
    deviceState.photos = files;
    var grid = $('#photo-grid');
    grid.textContent = '';
    $('#photo-count').textContent = files.length ? files.length + ' stored' : '';
    if (!files.length) {
      grid.appendChild(el('p', { class: 'empty', text: 'No photos yet.' }));
      return;
    }
    files.slice().sort(function (a, b) { return b.modified - a.modified; }).forEach(function (file) {
      var figure = el('figure', {}, [
        el('img', {
          src: '/api/device/photo?file=' + encodeURIComponent(file.file),
          alt: 'Photo ' + file.file, loading: 'lazy'
        }),
        el('figcaption', {}, [
          el('span', { text: hhmm(file.modified / 1000) }),
          el('button', {
            class: 'btn small', type: 'button', text: 'Delete',
            onclick: function () {
              deviceControl('photo-delete', { file: file.file }).then(function () {
                toast('Photo deleted');
                loadPhotos();
              }).catch(function (error) { toast(error.message, 'error'); });
            }
          })
        ])
      ]);
      grid.appendChild(figure);
    });
  }

  function loadPhotos() {
    api('/api/device/photos').then(function (data) {
      renderPhotos(data.files || []);
    }).catch(function () { /* listed on the next refresh */ });
  }

  function renderRecordings(files) {
    deviceState.recordings = files;
    var list = $('#recordings-list');
    list.textContent = '';
    if (!files.length) {
      list.appendChild(el('li', { class: 'file-empty', text: 'No recordings yet.' }));
      return;
    }
    files.slice().sort(function (a, b) { return b.modified - a.modified; }).forEach(function (file) {
      list.appendChild(el('li', { class: 'file-row' }, [
        el('span', { class: 'file-name', text: file.file }),
        el('span', { class: 'file-meta',
          text: mb(file.bytes) + ' · ' + hhmm(file.modified / 1000) }),
        el('span', { class: 'file-actions' }, [
          el('button', { class: 'act', type: 'button', text: 'Play',
            onclick: function () {
              deviceControl('recording-play', { file: file.file }).then(function () {
                toast('Playing ' + file.file + ' on the phone');
              }).catch(function (error) { toast(error.message, 'error'); });
            } }),
          el('a', { class: 'act', href: '/api/device/recording?file=' +
            encodeURIComponent(file.file), text: 'Download' }),
          el('button', { class: 'act danger', type: 'button', text: 'Delete',
            onclick: function () {
              deviceControl('recording-delete', { file: file.file }).then(function () {
                toast('Recording deleted');
                loadRecordings();
              }).catch(function (error) { toast(error.message, 'error'); });
            } })
        ])
      ]));
    });
  }

  function loadRecordings() {
    api('/api/device/recordings').then(function (data) {
      renderRecordings(data.files || []);
    }).catch(function () { /* listed on the next refresh */ });
  }

  function renderMedia(files) {
    deviceState.media = files;
    var list = $('#media-list');
    list.textContent = '';
    if (!files.length) {
      list.appendChild(el('li', { class: 'file-empty', text: 'No uploaded media.' }));
      return;
    }
    files.forEach(function (file) {
      list.appendChild(el('li', { class: 'file-row' }, [
        el('span', { class: 'file-name', text: file.file }),
        el('span', { class: 'file-meta', text: mb(file.bytes) }),
        el('span', { class: 'file-actions' }, [
          el('button', { class: 'act', type: 'button', text: 'Play',
            onclick: function () {
              deviceControl('media-play', { file: file.file }).then(function () {
                $('#media-note').textContent = 'Playing ' + file.file + ' on the phone';
                toast('Playing on the phone');
              }).catch(function (error) { toast(error.message, 'error'); });
            } }),
          el('button', { class: 'act danger', type: 'button', text: 'Delete',
            onclick: function () {
              deviceControl('media-delete', { file: file.file }).then(function () {
                toast('File deleted');
                loadMedia();
              }).catch(function (error) { toast(error.message, 'error'); });
            } })
        ])
      ]));
    });
  }

  function loadMedia() {
    api('/api/device/media').then(function (data) {
      renderMedia(data.files || []);
    }).catch(function () { /* listed on the next refresh */ });
  }

  function startCamera() {
    clearTimeout(deviceState.camRetry);
    var id = $('#cam-id').value || '0';
    var size = $('#cam-size').value || '';
    var fps = $('#cam-fps').value || '4';
    var query = '?camera=' + encodeURIComponent(id) + '&fps=' + encodeURIComponent(fps) +
      (size ? '&size=' + encodeURIComponent(size) : '') + '&t=' + Date.now();
    var image = $('#cam-preview');
    image.src = '/api/device/camera/stream' + query;
    image.hidden = false;
    $('#cam-placeholder').hidden = true;
    $('#cam-frame').dataset.live = 'true';
    $('#cam-live').hidden = false;
    $('#cam-toggle').textContent = 'Stop preview';
    $('#cam-toggle').setAttribute('aria-pressed', 'true');
    deviceState.cameraOn = true;
  }

  function stopCamera(silent) {
    clearTimeout(deviceState.camRetry);
    var image = $('#cam-preview');
    image.hidden = true;
    image.removeAttribute('src');
    $('#cam-placeholder').hidden = false;
    $('#cam-frame').dataset.live = 'false';
    $('#cam-live').hidden = true;
    $('#cam-toggle').textContent = 'Start preview';
    $('#cam-toggle').setAttribute('aria-pressed', 'false');
    // Record stays available: the app reopens the sensor for a take, so the
    // preview is a way to watch, not a precondition.
    $('#video-toggle').textContent = 'Record video';
    clearInterval(deviceState.videoTimer);
    deviceState.videoStarted = 0;
    $('#video-state').textContent = 'not recording';
    deviceState.cameraOn = false;
    deviceControl('camera-stop').catch(function () {
      if (!silent) { toast('The camera may still be open', 'error'); }
    });
  }

  function startVideo() {
    deviceControl('video-start', { audio: !deviceState.listening && !deviceState.recordStarted })
      .then(function (status) {
        deviceState.videoStarted = Date.now();
        $('#video-toggle').textContent = 'Stop recording';
        $('#video-toggle').setAttribute('aria-pressed', 'true');
        $('#video-state').textContent = 'recording 720p30' +
          (status.audio ? ' with sound' : ' without sound (microphone busy)');
        clearInterval(deviceState.videoTimer);
        deviceState.videoTimer = setInterval(function () {
          var seconds = Math.round((Date.now() - deviceState.videoStarted) / 1000);
          $('#video-state').textContent = 'recording 720p30 · ' + seconds + 's';
        }, 500);
      }).catch(function (error) {
        toast('Video: ' + error.message, 'error');
      });
  }

  function stopVideo() {
    deviceControl('video-stop').then(function (result) {
      clearInterval(deviceState.videoTimer);
      deviceState.videoStarted = 0;
      $('#video-toggle').textContent = 'Record video';
      $('#video-toggle').setAttribute('aria-pressed', 'false');
      $('#video-state').textContent = result.file
        ? 'saved ' + result.file + ' · ' + Math.round(result.bytes / 1024) + ' kB · ' +
          result.seconds + 's'
        : 'not recording';
      loadVideos();
    }).catch(function (error) { toast('Video: ' + error.message, 'error'); });
  }

  function renderVideos(files) {
    deviceState.videos = files;
    var list = $('#videos-list');
    list.textContent = '';
    if (!files.length) {
      list.appendChild(el('li', { class: 'file-empty', text: 'No videos yet.' }));
      return;
    }
    files.slice().sort(function (a, b) { return b.modified - a.modified; }).forEach(function (file) {
      list.appendChild(el('li', { class: 'file-row' }, [
        el('span', { class: 'file-name', text: file.file }),
        el('span', { class: 'file-meta',
          text: mb(file.bytes) + ' · ' + hhmm(file.modified / 1000) }),
        el('span', { class: 'file-actions' }, [
          el('a', { class: 'act', href: '/api/device/video?file=' +
            encodeURIComponent(file.file), text: 'Download' }),
          el('button', { class: 'act danger', type: 'button', text: 'Delete',
            onclick: function () {
              deviceControl('video-delete', { file: file.file }).then(function () {
                toast('Video deleted');
                loadVideos();
              }).catch(function (error) { toast(error.message, 'error'); });
            } })
        ])
      ]));
    });
  }

  function loadVideos() {
    api('/api/device/videos').then(function (data) {
      renderVideos(data.files || []);
    }).catch(function () { /* listed on the next refresh */ });
  }

  function takeSnapshot() {
    deviceControl('snapshot', {
      camera: $('#cam-id').value || '0', size: $('#cam-size').value || ''
    }).then(function (result) {
      toast('Photo saved: ' + result.file + ' (' + Math.round(result.bytes / 1024) + ' kB)');
      loadPhotos();
    }).catch(function (error) { toast('Snapshot: ' + error.message, 'error'); });
  }

  function setTorch(on) {
    deviceControl('torch', { on: on }).then(function () {
      deviceState.torch = on;
      var button = $('#torch-toggle');
      button.textContent = on ? 'Turn off' : 'Turn on';
      button.setAttribute('aria-pressed', on ? 'true' : 'false');
      $('#torch-state').textContent = on ? 'On' : 'Off';
    }).catch(function (error) { toast('Flashlight: ' + error.message, 'error'); });
  }

  function startRecording() {
    deviceControl('record-start').then(function (result) {
      deviceState.recordStarted = Date.now();
      $('#mic-record').textContent = 'Stop recording';
      clearInterval(deviceState.recordTimer);
      deviceState.recordTimer = setInterval(function () {
        var seconds = Math.round((Date.now() - deviceState.recordStarted) / 1000);
        $('#mic-timer').textContent = 'recording ' + seconds + 's · ' + result.file;
      }, 500);
      toast('Recording the microphone');
    }).catch(function (error) { toast('Recording: ' + error.message, 'error'); });
  }

  function stopRecording() {
    deviceControl('record-stop').then(function (result) {
      clearInterval(deviceState.recordTimer);
      deviceState.recordStarted = 0;
      $('#mic-record').textContent = 'Start recording';
      $('#mic-timer').textContent = result.file
        ? 'saved ' + result.file + ' · ' + Math.round(result.bytes / 1024) + ' kB'
        : 'not recording';
      loadRecordings();
    }).catch(function (error) { toast('Recording: ' + error.message, 'error'); });
  }

  function stopListening() {
    var session = deviceState.listening;
    deviceState.listening = null;
    if (session) {
      try { session.abort.abort(); } catch (error) { /* already closed */ }
      try { session.context.close(); } catch (error) { /* already closed */ }
    }
    $('#mic-listen').textContent = 'Listen live';
    $('#mic-live').hidden = true;
  }

  function startListening() {
    if (deviceState.listening) { stopListening(); return; }
    var AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) {
      toast('This browser cannot decode raw audio', 'error');
      return;
    }
    var context = new AudioContextClass();
    var session = { context: context, abort: new AbortController(), header: 0, cursor: 0 };
    deviceState.listening = session;
    $('#mic-listen').textContent = 'Stop listening';
    $('#mic-live').hidden = false;
    // The phone sends a WAV header followed by endless 16-bit PCM. Web Audio
    // needs the samples, not a media element, so the header is skipped once.
    fetch('/api/device/audio/stream', { cache: 'no-store', signal: session.abort.signal })
      .then(function (response) {
        if (!response.ok || !response.body) { throw new Error('stream refused'); }
        var reader = response.body.getReader();
        function pump() {
          return reader.read().then(function (result) {
            if (result.done || deviceState.listening !== session) { return; }
            var chunk = result.value;
            if (session.header < 44) {
              var skip = Math.min(44 - session.header, chunk.length);
              session.header += skip;
              chunk = chunk.subarray(skip);
              if (!chunk.length) { return pump(); }
            }
            var count = chunk.length >> 1;
            var samples = new Float32Array(count);
            var view = new DataView(chunk.buffer, chunk.byteOffset, count * 2);
            for (var index = 0; index < count; index++) {
              samples[index] = view.getInt16(index * 2, true) / 32768;
            }
            var buffer = context.createBuffer(1, count, 16000);
            buffer.copyToChannel(samples, 0);
            var source = context.createBufferSource();
            source.buffer = buffer;
            source.connect(context.destination);
            var at = Math.max(context.currentTime + 0.08, session.cursor);
            source.start(at);
            session.cursor = at + buffer.duration;
            return pump();
          });
        }
        return pump();
      }).catch(function (error) {
        if (error.name !== 'AbortError') { toast('Microphone: ' + error.message, 'error'); }
      }).finally(function () {
        if (deviceState.listening === session) { stopListening(); }
      });
  }

  function uploadMedia(file) {
    var name = file.name.replace(/[^A-Za-z0-9._-]/g, '_');
    $('#media-note').textContent = 'Uploading ' + name + '…';
    fetch('/api/device/media/upload', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'X-Filename': name }, body: file
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) { throw new Error(data.error || 'upload failed'); }
        return data;
      });
    }).then(function (data) {
      $('#media-note').textContent = 'Uploaded ' + data.file;
      toast('Uploaded to the phone');
      loadMedia();
    }).catch(function (error) {
      $('#media-note').textContent = 'Upload failed';
      toast('Upload: ' + error.message, 'error');
    });
  }

  function wireDevice() {
    $('#device-pair-form').addEventListener('submit', function (event) {
      event.preventDefault();
      var button = $('#device-pair-form button[type="submit"]');
      button.disabled = true;
      api('/api/device/pair', { method: 'POST', body: JSON.stringify({}) })
        .then(function () {
          toast('Phone app paired');
          loadDevice();
        }).catch(function (error) {
          toast(error.message, 'error');
        }).finally(function () { button.disabled = false; });
    });
    $('#device-unpair').addEventListener('click', function () {
      api('/api/device/unpair', { method: 'POST', body: JSON.stringify({}) })
        .then(function () { toast('Pairing forgotten'); loadDevice(); })
        .catch(function (error) { toast(error.message, 'error'); });
    });
    $('#cam-id').addEventListener('change', function () {
      renderCameraSizes((deviceState.summary || {}).capabilities || {});
      if (deviceState.cameraOn) { startCamera(); }
    });
    $('#cam-size').addEventListener('change', function () {
      if (deviceState.cameraOn) { startCamera(); }
    });
    $('#cam-fps').addEventListener('change', function () {
      if (deviceState.cameraOn) { startCamera(); }
    });
    // A stream lives five minutes by design; over a tunnel it can also drop on
    // its own. Re-open it while the operator still has the view open.
    $('#cam-preview').addEventListener('error', function () {
      if (!deviceState.cameraOn) { return; }
      clearTimeout(deviceState.camRetry);
      deviceState.camRetry = setTimeout(function () {
        if (deviceState.cameraOn) { startCamera(); }
      }, 3000);
    });
    $('#cam-toggle').addEventListener('click', function () {
      if (deviceState.cameraOn) { stopCamera(false); } else { startCamera(); }
    });
    $('#video-toggle').addEventListener('click', function () {
      if (deviceState.videoStarted) { stopVideo(); } else { startVideo(); }
    });
    $('#cam-snapshot').addEventListener('click', takeSnapshot);
    $('#rotation').addEventListener('change', function () { setRotation(this.value); });
    $('#torch-toggle').addEventListener('click', function () {
      setTorch(!deviceState.torch);
    });
    $$('[data-vibrate]').forEach(function (button) {
      button.addEventListener('click', function () {
        deviceControl('vibrate', { ms: Number(button.dataset.vibrate) })
          .then(function () { toast('Phone vibrated'); })
          .catch(function (error) { toast('Vibration: ' + error.message, 'error'); });
      });
    });
    $('#brightness').addEventListener('input', function () {
      $('#brightness-value').textContent = this.value;
    });
    $('#brightness').addEventListener('change', function () {
      var value = Number(this.value);
      clearTimeout(deviceState.brightnessTimer);
      deviceState.brightnessTimer = setTimeout(function () {
        deviceControl('brightness', { value: value })
          .catch(function (error) { toast('Brightness: ' + error.message, 'error'); });
      }, 250);
    });
    $('#mic-listen').addEventListener('click', startListening);
    $('#mic-record').addEventListener('click', function () {
      if (deviceState.recordStarted) { stopRecording(); } else { startRecording(); }
    });
    $('#media-file').addEventListener('change', function () {
      var file = this.files && this.files[0];
      $('#media-file-name').textContent = file ? file.name : 'No file chosen';
      this.value = '';
      if (file) { uploadMedia(file); }
    });
    $('#media-stop').addEventListener('click', function () {
      deviceControl('media-control', { action: 'stop' })
        .then(function () { $('#media-note').textContent = 'Nothing playing'; })
        .catch(function (error) { toast(error.message, 'error'); });
    });
  }

  // ------------------------------------------------------------------ music
  // Only the search request goes through this server. Track audio is fetched
  // by the browser straight from Jamendo's CDN, so the phone proxies nothing.
  var music = { loaded: false, busy: false, query: '', category: '' };

  function musicStatus(text, kind) {
    var node = $('#music-status');
    node.textContent = text;
    node.hidden = !text;
    if (kind) { node.dataset.state = kind; } else { delete node.dataset.state; }
  }

  function trackCard(track) {
    var audio = el('audio', { controls: '', preload: 'none', src: track.audio });
    var art = track.image
      ? el('img', { class: 'track-art', src: track.image, alt: 'Cover art for ' + track.name, loading: 'lazy', width: '64', height: '64' })
      : el('span', { class: 'track-art placeholder', 'aria-hidden': 'true', text: '♪' });
    var card = el('li', { class: 'track', 'data-track': track.id }, [
      art,
      el('div', { class: 'track-body' }, [
        el('div', { class: 'track-head' }, [
          el('strong', { class: 'track-title', text: track.name, title: track.name }),
          el('span', { class: 'track-artist', text: track.artist, title: track.artist })
        ]),
        audio,
        el('p', { class: 'track-error', hidden: '', role: 'alert' })
      ])
    ]);
    // A stream can fail long after the search succeeded: the CDN can refuse a
    // track or the connection can drop mid-song. Say so on the card instead of
    // leaving a player that silently does nothing.
    audio.addEventListener('error', function () {
      card.dataset.state = 'error';
      var note = $('.track-error', card);
      note.textContent = 'Stream unavailable. Jamendo did not return audio for this track.';
      note.hidden = false;
      toast('Stream unavailable: ' + track.name, 'error');
    });
    audio.addEventListener('playing', function () {
      card.dataset.state = 'playing';
      $('.track-error', card).hidden = true;
    });
    audio.addEventListener('play', function () {
      // Ten players in one grid must not overlap.
      $$('#music-tracks audio').forEach(function (other) {
        if (other !== audio) { other.pause(); }
      });
    });
    audio.addEventListener('pause', function () {
      if (card.dataset.state === 'playing') { delete card.dataset.state; }
    });
    return card;
  }

  function renderMusic(data) {
    var list = $('#music-tracks');
    list.textContent = '';
    $('#music-count').textContent = data.count ? data.count + ' tracks' : '';
    if (data.configured === false) {
      musicStatus(data.error || 'Jamendo is not configured.', 'error');
      return;
    }
    if (!data.tracks || !data.tracks.length) {
      musicStatus('No tracks matched. Try another name or category.');
      return;
    }
    musicStatus('');
    data.tracks.forEach(function (track) { list.appendChild(trackCard(track)); });
  }

  function loadMusic() {
    if (music.busy) { return; }
    music.busy = true;
    var query = new URLSearchParams();
    if (music.query) { query.set('q', music.query); }
    if (music.category) { query.set('category', music.category); }
    musicStatus('Loading tracks…');
    $('#music-count').textContent = '';
    api('/api/music/search' + (query.toString() ? '?' + query : ''))
      .then(function (data) {
        music.loaded = true;
        renderMusic(data);
      })
      .catch(function (error) {
        music.loaded = true;
        $('#music-tracks').textContent = '';
        musicStatus(error.message, 'error');
        toast('Music: ' + error.message, 'error');
      })
      .then(function () { music.busy = false; });
  }

  function wireMusic() {
    $('#music-form').addEventListener('submit', function (event) {
      event.preventDefault();
      music.query = $('#music-query').value.trim();
      loadMusic();
    });
    $('#music-categories').addEventListener('click', function (event) {
      var button = event.target.closest('button[data-category]');
      if (!button) { return; }
      music.category = button.dataset.category;
      $$('#music-categories button').forEach(function (chip) {
        var active = chip === button;
        chip.classList.toggle('on', active);
        chip.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
      loadMusic();
    });
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
    wireDevice();
    wireMusic();
    window.addEventListener('pagehide', function () {
      if (deviceState.cameraOn) { stopCamera(true); }
      if (deviceState.listening) { stopListening(); }
    });
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
      openTerminal(password).then(function () {
        // Kept in memory only: later tabs in this page can open without asking
        // again. It is never written to storage.
        terminalSecret = password;
      }).catch(function (error) {
        toast(error.message, 'error');
      }).finally(function () { button.disabled = false; });
    });

    $('#terminal-new').addEventListener('click', function () {
      if (terminals.length >= TERMINAL_MAX) {
        toast('Close a shell first (' + TERMINAL_MAX + ' max)', 'error');
        return;
      }
      if (!terminalSecret) { askTerminalPassword(); return; }
      openTerminal(terminalSecret).catch(function (error) {
        if (/password/i.test(error.message)) { terminalSecret = ''; askTerminalPassword(); }
        else { toast(error.message, 'error'); }
      });
    });

    $('#terminal-tabs').addEventListener('click', function (event) {
      var button = event.target.closest('[data-terminal]');
      if (!button) { return; }
      if (event.target.classList.contains('terminal-tab-close')) {
        var closing = terminals.filter(function (tab) { return tab.id === button.dataset.terminal; })[0];
        if (closing) { dropTerminal(closing, true); }
        return;
      }
      activate(button.dataset.terminal);
    });

    $('#shell-form').addEventListener('submit', function (event) {
      event.preventDefault();
      var button = $('#shell-form button[type="submit"]');
      var password = $('#shell-password').value;
      button.disabled = true;
      openTerminal(password).then(function () {
        terminalSecret = password;
        $('#shell-dialog').close();
      }).catch(function (error) {
        toast(error.message, 'error');
      }).finally(function () { button.disabled = false; });
    });
    $('#shell-cancel').addEventListener('click', function () { $('#shell-dialog').close(); });

    $('#terminal-close').addEventListener('click', function () {
      if (activeTerminal) { dropTerminal(activeTerminal, true); }
    });
    var a11yToggle = $('#terminal-a11y');
    a11yToggle.checked = a11yEnabled();
    a11yToggle.addEventListener('change', function () {
      try { window.localStorage.setItem(A11Y_KEY, a11yToggle.checked ? '1' : '0'); }
      catch (error) { /* private mode: the choice just does not persist */ }
      terminals.forEach(function (tab) {
        if (tab.emulator) { tab.emulator.options.screenReaderMode = a11yToggle.checked; }
      });
    });
    // A reload is not a logout: leave the shells for the server to reap if the
    // page never comes back, and let the next load reattach instead of
    // refusing to open a new one.
    window.addEventListener('pagehide', rememberTabs);

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
    restoreTerminals();
    refreshStatus();
    loadTimeline(state.range);
    loadEvents();
    loadSettings();
    loadTelemetry();
    state.statusTimer = setInterval(function () {
      if (document.visibilityState === 'visible') { refreshStatus(); }
    }, 10000);
    setInterval(function () {
      if (document.visibilityState === 'visible' && state.route === 'device') {
        loadDevice();
      }
    }, 15000);
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
