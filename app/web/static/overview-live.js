/* Live figures of the bot and PasarGuard overviews: server meters, traffic rates, nodes.
   Text goes in through textContent only. The DOM is looked up on every tick, so the /pg/body
   swap and a missing card never break the poller. */
(function (root) {
  'use strict';
  var POLL_MS = 5000;
  // After failed polls, wait longer before asking again so a struggling panel is not pressed.
  var MAX_BACKOFF_MS = 60000;
  var SAMPLES = 60;
  var SPARK_W = 300;
  var SPARK_H = 60;

  function clampPct(value) {
    if (typeof value !== 'number' || !isFinite(value)) return null;
    return Math.max(0, Math.min(100, value));
  }
  function fmtPct(value) {
    var p = clampPct(value);
    return p === null ? '—' : Math.round(p) + '٪';
  }
  function memHint(d) {
    if (d.memory_ratio_text) return d.memory_ratio_text;
    if (d.memory_used_text && d.memory_total_text) return d.memory_used_text + ' / ' + d.memory_total_text;
    return null;
  }
  function partsText(parts) {
    var p = parts || {};
    var num = p.num != null && p.num !== '' ? String(p.num) : '—';
    return p.unit ? num + ' ' + p.unit : num;
  }
  function pushSample(buffer, value, size) {
    var next = buffer.slice(-(size - 1));
    next.push(typeof value === 'number' && isFinite(value) && value > 0 ? value : 0);
    return next;
  }
  /* Polyline points of samples on a shared peak. Like the sales chart, the newest sample sits on
     the left edge and older ones move right.
     A single sample is drawn as a level line so the chart shows from the first value. */
  function sparkPoints(values, peak, width, height) {
    if (!values.length) return '';
    var scale = peak > 0 ? peak : 1;
    function y(v) { return (height - (Math.min(v, scale) / scale) * (height - 2) - 1).toFixed(1); }
    if (values.length === 1) return '0.0,' + y(values[0]) + ' ' + width.toFixed(1) + ',' + y(values[0]);
    var step = width / (SAMPLES - 1);
    var last = values.length - 1;
    return values.map(function (v, i) {
      return (step * (last - i)).toFixed(1) + ',' + y(v);
    }).join(' ');
  }
  /* The line closed down to the bottom edge, for the shaded area under it. */
  function areaPoints(line, height) {
    if (!line) return '';
    var pts = line.split(' ');
    return pts[0].split(',')[0] + ',' + height + ' ' + line + ' ' + pts[pts.length - 1].split(',')[0] + ',' + height;
  }
  var RATE_UNITS = [[1, 'بایت'], [1024, 'کیلوبایت'], [Math.pow(1024, 2), 'مگ'], [Math.pow(1024, 3), 'گیگ'],
    [Math.pow(1024, 4), 'ترابایت']];
  /* Same amount and units as format_bytes_rate on the server. */
  function fmtRate(bytes) {
    if (typeof bytes !== 'number' || !isFinite(bytes) || bytes < 0) return '—';
    var unit = RATE_UNITS[0];
    RATE_UNITS.forEach(function (u) { if (bytes >= u[0]) unit = u; });
    var n = bytes / unit[0];
    var text = unit[0] === 1 ? String(Math.round(n))
      : n >= 100 ? n.toFixed(0) : n >= 10 ? String(Number(n.toFixed(1))) : String(Number(n.toFixed(2)));
    return text + ' ' + unit[1] + '/ثانیه';
  }
  function badgeClass(kind) {
    if (kind === 'ok') return 'badge connected';
    if (kind === 'warn') return 'badge connecting';
    if (kind === 'err') return 'badge danger';
    return 'badge neutral';
  }

  function setText(el, text) {
    if (el) el.textContent = text;
  }
  function applyMeter(box, pct, text, toneName, hint) {
    if (!box) return;
    var p = clampPct(pct);
    box.setAttribute('data-tone', toneName || 'neutral');
    setText(box.querySelector('[data-meter-val]'), text);
    var fill = box.querySelector('.ov-fill');
    if (fill) fill.style.setProperty('--p', String(p === null ? 0 : p));
    var track = box.querySelector('[role="meter"]');
    if (track) track.setAttribute('aria-valuenow', String(p === null ? 0 : Math.round(p)));
    if (hint) setText(box.querySelector('[data-meter-hint]'), hint);
  }
  function applyServer(card, d) {
    if (!card) return;
    applyMeter(card.querySelector('[data-meter="cpu"]'), d.cpu_percent, fmtPct(d.cpu_percent), d.cpu_tone,
      d.cpu_cores ? d.cpu_cores + ' هسته' : null);
    applyMeter(card.querySelector('[data-meter="mem"]'), d.memory_percent, fmtPct(d.memory_percent), d.mem_tone, memHint(d));
  }

  var series = { up: [], down: [] };
  function record(up, down) {
    series.up = pushSample(series.up, up, SAMPLES);
    series.down = pushSample(series.down, down, SAMPLES);
  }
  function drawChart(card) {
    var chart = card && card.querySelector('[data-live-chart]');
    if (!chart) return;
    chart.classList.toggle('has-data', series.up.length > 0);
    var peak = Math.max.apply(null, [0].concat(series.up, series.down));
    setText(chart.querySelector('[data-live-peak]'), fmtRate(peak));
    ['up', 'down'].forEach(function (dir) {
      var line = sparkPoints(series[dir], peak, SPARK_W, SPARK_H);
      var stroke = chart.querySelector('[data-spark="' + dir + '"]');
      var fill = chart.querySelector('[data-spark-fill="' + dir + '"]');
      if (stroke) stroke.setAttribute('points', line);
      if (fill) fill.setAttribute('points', areaPoints(line, SPARK_H));
    });
  }
  /* The rate rendered with the page starts the chart before the first poll. */
  function seedChart() {
    var card = document.querySelector('[data-pg-live]');
    if (!card) return;
    if (!series.up.length && card.hasAttribute('data-seed-up')) {
      record(Number(card.getAttribute('data-seed-up')), Number(card.getAttribute('data-seed-down')));
    }
    drawChart(card);
  }
  function applyLive(card, live) {
    if (!card || !live) return;
    setText(card.querySelector('[data-live-up-num]'), live.rate_up_num || '—');
    setText(card.querySelector('[data-live-up-unit]'), live.rate_up_unit || '');
    setText(card.querySelector('[data-live-down-num]'), live.rate_down_num || '—');
    setText(card.querySelector('[data-live-down-unit]'), live.rate_down_unit || '');
    if (typeof live.rate_up === 'number' || typeof live.rate_down === 'number') record(live.rate_up, live.rate_down);
    drawChart(card);
  }
  function applyNode(row, n) {
    var st = row.querySelector('[data-node-status]');
    if (st) {
      st.textContent = n.status || '—';
      st.className = badgeClass(n.status_kind);
    }
    applyMeter(row.querySelector('[data-meter="cpu"]'), n.cpu_percent, n.cpu_text || '—', n.cpu_tone);
    applyMeter(row.querySelector('[data-meter="mem"]'), n.mem_percent, n.mem_text || '—', n.mem_tone);
    row.querySelectorAll('[data-node-f]').forEach(function (el) {
      el.textContent = partsText(n[el.getAttribute('data-node-f')]);
    });
  }
  function applyNodes(list, nodes) {
    if (!list || !Array.isArray(nodes)) return;
    var rows = {};
    list.querySelectorAll('[data-node-id]').forEach(function (row) {
      rows[row.getAttribute('data-node-id')] = row;
    });
    nodes.forEach(function (n) {
      if (n && n.id != null && rows[String(n.id)]) applyNode(rows[String(n.id)], n);
    });
  }

  function setStale(source, stale) {
    document.querySelectorAll('.ov-live').forEach(function (el) {
      el.classList.toggle('is-stale', stale);
    });
    var note = document.getElementById('ov-live-status');
    if (!stale) {
      if (note) note.remove();
      return;
    }
    if (note || !source || !source.parentNode) return;
    note = document.createElement('div');
    note.id = 'ov-live-status';
    note.className = 'flash warn';
    note.setAttribute('role', 'status');
    note.textContent = 'آمار زنده به‌روز نشد؛ آخرین اطلاعات نمایش داده می‌شود و دوباره تلاش می‌کنیم.';
    source.parentNode.insertBefore(note, source);
  }

  function backoffDelay(failures) {
    if (!(failures > 0)) return 0;
    return Math.min(POLL_MS * Math.pow(2, failures - 1), MAX_BACKOFF_MS);
  }

  var pending = false;
  var failures = 0;
  var resumeAt = 0;
  function source() {
    return document.querySelector('[data-pg-live][data-metrics-url]')
      || document.querySelector('[data-server-meters][data-metrics-url]');
  }
  function tick() {
    var src = source();
    if (document.hidden || pending || !src || !root.PGClockPanelWidgetRequest) return;
    if (Date.now() < resumeAt) return;
    var url = src.getAttribute('data-metrics-url');
    pending = true;
    root.PGClockPanelWidgetRequest.fetchJson(url + (url.indexOf('?') >= 0 ? '&' : '?') + '_=' + Date.now(), {
      cache: 'no-store',
      credentials: 'same-origin'
    }).then(function (d) {
      failures = 0;
      resumeAt = 0;
      setStale(src, false);
      applyServer(document.querySelector('[data-server-meters]'), d || {});
      applyLive(document.querySelector('[data-pg-live]'), d && d.live);
      applyNodes(document.querySelector('[data-node-list]'), d && d.nodes);
    }).catch(function () {
      failures += 1;
      resumeAt = Date.now() + backoffDelay(failures);
      setStale(src, true);
    }).finally(function () {
      pending = false;
    });
  }

  var api = { clampPct: clampPct, fmtPct: fmtPct, memHint: memHint, partsText: partsText,
    pushSample: pushSample, sparkPoints: sparkPoints, areaPoints: areaPoints, fmtRate: fmtRate, badgeClass: badgeClass, SAMPLES: SAMPLES,
    backoffDelay: backoffDelay, POLL_MS: POLL_MS, MAX_BACKOFF_MS: MAX_BACKOFF_MS };
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
    return;
  }
  setInterval(tick, POLL_MS);
  setTimeout(tick, 1500);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', seedChart);
  else seedChart();
  document.addEventListener('visibilitychange', function () { if (!document.hidden) tick(); });
  document.addEventListener('panel-widgets-ready', function () { seedChart(); tick(); });
})(typeof window !== 'undefined' ? window : globalThis);
