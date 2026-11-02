/* Dashboard sales chart: smooth monotone curve, no library. Text lives in HTML so it never stretches.
   The same chart draws any daily count (orders, new users) when the card names its metric. */
(function (root) {
  'use strict';

  // The panel shows figures with Latin digits, so the chart does too; only the words are Persian.
  function fa(value) {
    return String(value);
  }

  function fullToman(value) {
    return Math.round(Number(value) || 0).toLocaleString('en-US');
  }

  function trimNumber(value, digits) {
    var fixed = Number(value).toFixed(digits);
    return fixed.indexOf('.') === -1 ? fixed : fixed.replace(/0+$/, '').replace(/\.$/, '');
  }

  // Short Toman amount: «4.82 میلیون» / «250 هزار» / «1.5 هزار» / «900».
  function compactToman(value) {
    var n = Math.abs(Number(value) || 0);
    if (n >= 1e9) return fa(trimNumber(n / 1e9, 2)) + ' میلیارد';
    if (n >= 1e6) return fa(trimNumber(n / 1e6, 2)) + ' میلیون';
    if (n >= 1e3) return fa(trimNumber(n / 1e3, 1)) + ' هزار';
    return fa(Math.round(n));
  }

  var SCALE_STEPS = 4;
  // Four equal steps on round numbers that cover the peak with a little headroom, so the curve
  // uses most of the plot height.
  function niceScale(peak) {
    var target = Math.max(peak, 1) * 1.1 / SCALE_STEPS;
    var power = Math.pow(10, Math.floor(Math.log10(target)));
    var steps = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
    var step = power * 10;
    for (var i = 0; i < steps.length; i++) {
      if (steps[i] * power >= target) { step = steps[i] * power; break; }
    }
    return { step: step, max: step * SCALE_STEPS };
  }

  // Counts are whole numbers: the same scale with a whole step of at least one.
  function niceCountScale(peak) {
    var step = Math.max(1, Math.ceil(niceScale(peak).step));
    return { step: step, max: step * SCALE_STEPS };
  }

  // Horizontal position of day ``index`` measured from the plot's left edge: the oldest day sits at
  // the right edge and today at the left, as Persian text reads. Points, labels, guide and pointer
  // all use this one measure.
  function dayX(index, count, width) {
    return count <= 1 ? width / 2 : width - (width * index) / (count - 1);
  }

  // Fritsch–Carlson monotone cubic: smooth, and never overshoots the data.
  function monotonePath(points) {
    var n = points.length;
    if (n === 0) return '';
    if (n === 1) return 'M' + points[0][0] + ',' + points[0][1];
    var dx = [], slope = [], tangent = [], i;
    for (i = 0; i < n - 1; i++) {
      dx[i] = points[i + 1][0] - points[i][0];
      slope[i] = (points[i + 1][1] - points[i][1]) / dx[i];
    }
    tangent[0] = slope[0];
    tangent[n - 1] = slope[n - 2];
    for (i = 1; i < n - 1; i++) {
      tangent[i] = slope[i - 1] * slope[i] <= 0 ? 0 : (slope[i - 1] + slope[i]) / 2;
    }
    for (i = 0; i < n - 1; i++) {
      if (slope[i] === 0) { tangent[i] = 0; tangent[i + 1] = 0; continue; }
      var a = tangent[i] / slope[i], b = tangent[i + 1] / slope[i], s = a * a + b * b;
      if (s > 9) {
        var k = 3 / Math.sqrt(s);
        tangent[i] = k * a * slope[i];
        tangent[i + 1] = k * b * slope[i];
      }
    }
    var d = 'M' + points[0][0] + ',' + points[0][1];
    for (i = 0; i < n - 1; i++) {
      var h = dx[i] / 3;
      d += ' C' + (points[i][0] + h) + ',' + (points[i][1] + tangent[i] * h) + ' ' +
        (points[i + 1][0] - h) + ',' + (points[i + 1][1] - tangent[i + 1] * h) + ' ' +
        points[i + 1][0] + ',' + points[i + 1][1];
    }
    return d;
  }

  var api = {
    fa: fa, fullToman: fullToman, compactToman: compactToman, niceScale: niceScale, niceCountScale: niceCountScale,
    dayX: dayX, monotonePath: monotonePath,
  };
  if (typeof module !== 'undefined' && module.exports) { module.exports = api; return; }
  if (!root.document) return;

  var SVG_NS = 'http://www.w3.org/2000/svg';
  var PAD = 14;
  var dayFormat = null;
  // Persian calendar and month names with Latin digits, like every other figure in the panel.
  try { dayFormat = new Intl.DateTimeFormat('fa-IR-u-ca-persian-nu-latn', { day: 'numeric', month: 'short' }); } catch (e) { dayFormat = null; }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function dayLabel(day, days) {
    if (days <= 7) return day.weekday;
    return dayFormat ? dayFormat.format(new Date(day.date + 'T12:00:00')) : day.date;
  }

  function Chart(card) {
    this.card = card;
    this.plot = card.querySelector('[data-plot]');
    this.total = card.querySelector('[data-total]');
    this.delta = card.querySelector('[data-delta]');
    this.empty = card.querySelector('[data-empty]');
    this.endpoint = card.getAttribute('data-endpoint');
    // Revenue in Toman unless the card names another daily figure, counted in its noun.
    this.metric = card.getAttribute('data-metric') || 'revenue';
    this.isCount = card.getAttribute('data-unit') === 'count';
    this.noun = card.getAttribute('data-noun') || '';
    this.extraKey = card.getAttribute('data-extra');
    this.extraNoun = card.getAttribute('data-extra-noun') || '';
    this.title = card.getAttribute('data-title') || 'نمودار فروش';
    this.prevNone = card.getAttribute('data-prev-none') || 'دورهٔ قبل فروشی نداشت';
    this.cache = {};
    this.days = 7;
    this.active = null;
    var self = this;
    ['data-trend', 'data-trend-30'].forEach(function (name) {
      var initial = null;
      try { initial = JSON.parse(card.getAttribute(name) || 'null'); } catch (e) { initial = null; }
      if (initial && initial.days) self.cache[initial.days.length] = initial;
    });
    this.bind();
    var first = Number(card.getAttribute('data-initial-days')) === 30 ? 30 : 7;
    if (this.cache[first]) this.show(first); else this.load(first);
  }

  Chart.prototype.amount = function (value) {
    return fullToman(value) + ' ' + (this.isCount ? this.noun : 'تومان');
  };

  Chart.prototype.bind = function () {
    var self = this;
    var buttons = this.card.querySelectorAll('[data-range]');
    Array.prototype.forEach.call(buttons, function (button) {
      button.addEventListener('click', function () { self.show(Number(button.getAttribute('data-range'))); });
    });
    var redraw = function () { self.draw(); };
    if (root.ResizeObserver) new root.ResizeObserver(redraw).observe(this.plot); else root.addEventListener('resize', redraw);
    this.plot.addEventListener('pointermove', function (event) { self.pick(event); });
    this.plot.addEventListener('pointerdown', function (event) { self.pick(event); });
    this.plot.addEventListener('pointerleave', function () { self.setActive(null); });
    this.plot.addEventListener('keydown', function (event) { self.key(event); });
    this.plot.addEventListener('blur', function () { self.setActive(null); });
  };

  Chart.prototype.show = function (days) {
    this.days = days;
    Array.prototype.forEach.call(this.card.querySelectorAll('[data-range]'), function (button) {
      var on = Number(button.getAttribute('data-range')) === days;
      button.classList.toggle('on', on);
      button.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    if (this.cache[days]) { this.render(); return; }
    this.load(days);
  };

  Chart.prototype.load = function (days) {
    var self = this;
    if (!this.endpoint) { this.card.classList.add('is-error'); return; }
    this.card.classList.add('is-loading');
    root.fetch(this.endpoint + '?days=' + days, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (response) { if (!response.ok) throw new Error('trend'); return response.json(); })
      .then(function (data) { self.cache[days] = data; if (self.days === days) self.render(); })
      .catch(function () { self.card.classList.add('is-error'); })
      .then(function () { self.card.classList.remove('is-loading'); });
  };

  Chart.prototype.render = function () {
    this.card.classList.remove('is-error');
    var data = this.cache[this.days];
    if (!data) return;
    if (this.total) this.total.textContent = this.amount(data.total);
    if (this.delta) {
      var pct = data.change_pct;
      var label = this.days === 7 ? 'نسبت به هفتهٔ قبل' : 'نسبت به ماه قبل';
      this.delta.className = 'sales-delta' + (pct == null || pct === 0 ? ' flat' : pct > 0 ? ' up' : ' down');
      this.delta.textContent = pct == null ? this.prevNone : (pct === 0 ? 'بدون تغییر ' : (pct > 0 ? '▲ ' : '▼ ')) + (pct === 0 ? '' : fa(Math.abs(pct)) + '٪ ') + label;
    }
    this.card.classList.toggle('is-empty', !data.has_data);
    if (this.empty) this.empty.hidden = !!data.has_data;
    this.plot.setAttribute('aria-label', this.title + ' ' + (this.days === 7 ? '7' : '30') + ' روز اخیر؛ جمع ' + this.amount(data.total));
    this.active = null;
    this.draw();
  };

  Chart.prototype.geometry = function (data, width, height) {
    var peak = 0, metric = this.metric;
    data.days.forEach(function (d) { peak = Math.max(peak, d[metric]); });
    var scale = this.isCount ? niceCountScale(peak) : niceScale(peak);
    var count = data.days.length;
    var xs = function (i) { return dayX(i, count, width); };
    var ys = function (v) { return height - PAD - (v / scale.max) * (height - PAD * 2); };
    return { scale: scale, x: xs, y: ys, count: count };
  };

  Chart.prototype.draw = function () {
    var data = this.cache[this.days];
    if (!data || !data.has_data) { this.plot.textContent = ''; return; }
    var width = this.plot.clientWidth, height = this.plot.clientHeight;
    if (!width || !height) return;
    var g = this.geometry(data, width, height);
    this.g = g;
    this.plot.textContent = '';
    var i;
    for (i = 0; i <= SCALE_STEPS; i++) {
      var value = g.scale.step * i, top = g.y(value) + 'px';
      var line = el('div', 'sc-grid'); line.style.top = top; this.plot.appendChild(line);
      var tick = el('span', 'sc-ytick', value ? (this.isCount ? fullToman(value) : compactToman(value)) : '0'); tick.style.top = top; this.plot.appendChild(tick);
    }
    var metric = this.metric;
    var points = data.days.map(function (d, index) { return [g.x(index), g.y(d[metric])]; });
    var path = monotonePath(points);
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'sc-svg');
    svg.setAttribute('width', width); svg.setAttribute('height', height);
    svg.setAttribute('aria-hidden', 'true');
    var area = document.createElementNS(SVG_NS, 'path');
    area.setAttribute('d', path + ' L' + points[points.length - 1][0] + ',' + height + ' L' + points[0][0] + ',' + height + ' Z');
    area.setAttribute('class', 'sc-area');
    var curve = document.createElementNS(SVG_NS, 'path');
    curve.setAttribute('d', path); curve.setAttribute('class', 'sc-line');
    svg.appendChild(area); svg.appendChild(curve);
    this.halo = document.createElementNS(SVG_NS, 'circle'); this.halo.setAttribute('class', 'sc-halo'); this.halo.setAttribute('r', 9);
    this.dot = document.createElementNS(SVG_NS, 'circle'); this.dot.setAttribute('class', 'sc-dot'); this.dot.setAttribute('r', 4.5);
    svg.appendChild(this.halo); svg.appendChild(this.dot);
    this.plot.appendChild(svg);
    var narrow = width < 520, days = this.days;
    var every = days <= 7 ? (narrow ? 3 : 1) : (narrow ? 14 : 5);
    data.days.forEach(function (d, index) {
      var last = index === data.days.length - 1, first = index === 0;
      var show = first || last || (days <= 7 ? (narrow ? index === 3 : true) : index % every === 0 && index < data.days.length - 3);
      if (!show) return;
      var label = el('span', 'sc-xtick' + (last ? ' is-end' : first ? ' is-start' : ''), last ? 'امروز' : dayLabel(d, days));
      if (!last && !first) label.style.left = g.x(index) + 'px';
      this.plot.appendChild(label);
    }, this);
    this.guide = el('div', 'sc-guide'); this.guide.hidden = true; this.plot.appendChild(this.guide);
    this.tip = el('div', 'sc-tip'); this.tip.hidden = true; this.tip.setAttribute('role', 'status'); this.plot.appendChild(this.tip);
    this.points = points;
    this.setActive(this.active);
  };

  Chart.prototype.setActive = function (index) {
    this.active = index;
    if (!this.dot || !this.points) return;
    var data = this.cache[this.days];
    var shown = index == null ? this.points.length - 1 : index;
    var p = this.points[shown];
    this.dot.setAttribute('cx', p[0]); this.dot.setAttribute('cy', p[1]);
    this.halo.setAttribute('cx', p[0]); this.halo.setAttribute('cy', p[1]);
    this.halo.style.opacity = index == null ? '0.0' : '0.16';
    this.guide.hidden = this.tip.hidden = index == null;
    if (index == null) return;
    var day = data.days[index];
    this.guide.style.left = p[0] + 'px';
    this.tip.textContent = '';
    var when = this.days <= 7 ? day.weekday : dayLabel(day, this.days);
    var detail = this.isCount
      ? (this.extraKey ? ' · ' + fa(day[this.extraKey]) + ' ' + this.extraNoun : '')
      : ' · ' + fa(day.orders) + ' سفارش';
    this.tip.appendChild(el('span', null, when + detail));
    this.tip.appendChild(el('b', null, this.amount(day[this.metric])));
    var tipWidth = this.tip.offsetWidth, width = this.plot.clientWidth;
    this.tip.style.left = Math.min(Math.max(p[0], tipWidth / 2), width - tipWidth / 2) + 'px';
  };

  Chart.prototype.pick = function (event) {
    var data = this.cache[this.days];
    if (!data || !data.has_data || !this.g) return;
    var rect = this.plot.getBoundingClientRect();
    var fromLeft = event.clientX - rect.left;
    var best = 0, bestDistance = Infinity;
    for (var i = 0; i < this.g.count; i++) {
      var distance = Math.abs(this.g.x(i) - fromLeft);
      if (distance < bestDistance) { bestDistance = distance; best = i; }
    }
    this.setActive(best);
  };

  Chart.prototype.key = function (event) {
    var data = this.cache[this.days];
    if (!data || !data.has_data) return;
    var last = data.days.length - 1;
    var current = this.active == null ? last : this.active;
    // Right-to-left: the right arrow goes back in time.
    if (event.key === 'ArrowRight') current = Math.max(0, current - 1);
    else if (event.key === 'ArrowLeft') current = Math.min(last, current + 1);
    else if (event.key === 'Home') current = 0;
    else if (event.key === 'End') current = last;
    else return;
    event.preventDefault();
    this.setActive(current);
  };

  function init() {
    Array.prototype.forEach.call(document.querySelectorAll('[data-sales-chart]'), function (card) {
      if (card.__salesChart) return;
      card.__salesChart = new Chart(card);
    });
  }
  document.addEventListener('DOMContentLoaded', init);
  document.addEventListener('panel-widgets-ready', init);
  if (document.readyState !== 'loading') init();
})(typeof window !== 'undefined' ? window : globalThis);
