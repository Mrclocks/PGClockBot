/* Phone list layout for tables marked .table-list-sm (styles in panel.css).
   - «N سرویس» buttons open the row's nested list (aria-expanded / aria-controls).
   - The header sort menu lists each sortable column both ways and drives the table's
     own header sort (panel.js), so the desktop and the list stay in the same order.
   - A modal body marked [data-load-fragment] is filled from the opener's data-modal-load. */
(function () {
  'use strict';

  var DIRECTIONS = [['ascending', '↑'], ['descending', '↓']];

  /* [{label, sortType}] in header order (null for a column that does not sort) →
     menu options {value: "index:direction", label}. */
  function sortOptions(headers) {
    var out = [];
    headers.forEach(function (h, index) {
      if (!h || !h.sortType || !h.label) return;
      DIRECTIONS.forEach(function (d) {
        out.push({ value: index + ':' + d[0], label: h.label + ' ' + d[1] });
      });
    });
    return out;
  }

  function parseSortValue(value) {
    var parts = String(value || '').split(':');
    var index = Number(parts[0]);
    if (parts.length !== 2 || !Number.isInteger(index) || index < 0) return null;
    if (parts[1] !== 'ascending' && parts[1] !== 'descending') return null;
    return { index: index, direction: parts[1] };
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { sortOptions: sortOptions, parseSortValue: parseSortValue };
  }
  if (typeof document === 'undefined') return;

  function headerCells(table) {
    var row = table.tHead && table.tHead.rows[0];
    return row ? Array.prototype.slice.call(row.cells) : [];
  }

  function buildSortMenu(table) {
    var select = table.querySelector('[data-list-sort]');
    if (!select) return;
    var heads = headerCells(table);
    var options = sortOptions(heads.map(function (th) {
      return { label: (th.textContent || '').trim(), sortType: th.getAttribute('data-sort-type') };
    }));
    if (!options.length) {
      /* Nothing to sort by: drop the menu so no empty control is left in the header. */
      var enhanced = select.closest('.ui-select');
      (enhanced || select).remove();
      return;
    }
    var placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'مرتب‌سازی';
    select.appendChild(placeholder);
    options.forEach(function (o) {
      var opt = document.createElement('option');
      opt.value = o.value;
      opt.textContent = o.label;
      select.appendChild(opt);
    });
    select.hidden = false;
    select.addEventListener('change', function () {
      var pick = parseSortValue(select.value);
      var th = pick && heads[pick.index];
      if (!th) return;
      /* panel.js flips the current direction on click: set the opposite first. */
      th.setAttribute('aria-sort', pick.direction === 'ascending' ? 'descending' : 'ascending');
      th.click();
    });
  }

  function toggleChildren(button) {
    var box = document.getElementById(button.getAttribute('aria-controls') || '');
    if (!box) return;
    var open = button.getAttribute('aria-expanded') !== 'true';
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
    box.hidden = !open;
    var row = button.closest('tr');
    if (row) row.classList.toggle('is-expanded', open);
  }

  async function loadFragment(url, target) {
    target.innerHTML = '<p class="muted">در حال بارگذاری…</p>';
    try {
      var resp = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'text/html' } });
      var html = await resp.text();
      target.innerHTML = resp.ok || resp.status === 403 || resp.status === 404
        ? html
        : '<div class="flash err">بارگذاری ناموفق بود.</div>';
    } catch (e) {
      target.innerHTML = '<div class="flash err">بارگذاری ناموفق بود.</div>';
    }
  }

  document.addEventListener('click', function (e) {
    var expand = e.target.closest('.table-list-sm .li-expand');
    if (expand) {
      e.preventDefault();
      toggleChildren(expand);
      return;
    }
    /* Opening another modal from inside a fragment modal (the card's «ویرایش») replaces it. */
    var opener = e.target.closest('[data-modal-open]');
    var host = opener && opener.closest('.ui-modal');
    if (host && host.querySelector('[data-load-fragment]')) {
      var close = host.querySelector('[data-modal-close]');
      if (close) setTimeout(function () { close.click(); }, 0);
    }
  });

  /* The user card's photo fades in over the initial; a missing one (404) leaves the initial.
     load/error do not bubble, so listen in the capture phase. */
  document.addEventListener('load', function (e) {
    if (e.target instanceof Element && e.target.matches('img[data-user-photo]')) e.target.classList.add('is-loaded');
  }, true);
  document.addEventListener('error', function (e) {
    if (e.target instanceof Element && e.target.matches('img[data-user-photo]')) e.target.remove();
  }, true);

  document.addEventListener('panel:modal-load', function (ev) {
    var d = ev.detail || {};
    var target = d.target && document.getElementById(d.target);
    if (!target || !target.hasAttribute('data-load-fragment') || !d.url) return;
    loadFragment(d.url, target);
  });

  /* .has-more on a second line that runs past its edge and is not scrolled to the end. */
  function markOverflow(cell) {
    var hidden = cell.scrollWidth - cell.clientWidth - Math.abs(cell.scrollLeft);
    cell.classList.toggle('has-more', hidden > 1);
  }

  function watchOverflow(table) {
    var cells = table.querySelectorAll('td.li-sub');
    cells.forEach(function (cell) {
      markOverflow(cell);
      cell.addEventListener('scroll', function () { markOverflow(cell); }, { passive: true });
    });
    if (typeof ResizeObserver !== 'undefined') {
      var ro = new ResizeObserver(function () { cells.forEach(markOverflow); });
      ro.observe(table);
    }
  }

  document.querySelectorAll('table.table-list-sm').forEach(function (table) {
    buildSortMenu(table);
    watchOverflow(table);
  });
})();
