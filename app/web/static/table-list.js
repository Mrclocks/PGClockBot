/* Phone list layout for tables marked .table-list-sm (styles in panel.css).
   - «N سرویس» buttons open the row's nested list (aria-expanded / aria-controls).
   - The header sort menu offers only the columns a template marks with
     data-list-sort-label (one direction each, data-list-sort-dir) and drives the table's
     own header sort (panel.js), so the desktop and the list stay in the same order.
   - A modal body marked [data-load-fragment] is filled from the opener's data-modal-load. */
(function () {
  'use strict';

  /* A menu with fewer choices than this is no choice at all: it is not shown. */
  var MIN_SORT_OPTIONS = 2;

  /* [{listLabel, listDir, listOrder, sortType}] in header order (null for a column that
     is not offered) → menu options {value: "index:direction", label}, by listOrder and
     then header order, so every list reads «جدیدترین» first and «نام» last. */
  function sortOptions(headers) {
    var picked = [];
    headers.forEach(function (h, index) {
      if (!h || !h.sortType || !h.listLabel) return;
      var order = Number(h.listOrder);
      picked.push({
        order: Number.isFinite(order) ? order : Infinity,
        index: index,
        value: index + ':' + (h.listDir === 'descending' ? 'descending' : 'ascending'),
        label: h.listLabel,
      });
    });
    if (picked.length < MIN_SORT_OPTIONS) return [];
    picked.sort(function (a, b) { return a.order - b.order || a.index - b.index; });
    return picked.map(function (p) { return { value: p.value, label: p.label }; });
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
      return {
        listLabel: (th.getAttribute('data-list-sort-label') || '').trim(),
        listDir: th.getAttribute('data-list-sort-dir'),
        listOrder: th.getAttribute('data-list-sort-order'),
        sortType: th.getAttribute('data-sort-type'),
      };
    }));
    if (!options.length) {
      /* Nothing worth choosing: drop the menu so no empty control is left in the header. */
      var enhanced = select.closest('.ui-select');
      (enhanced || select).remove();
      return;
    }
    /* An empty, disabled first option keeps «مرتب‌سازی» as the closed label (panel.js reads
       the placeholder attribute) without listing it as a choice in the open menu. */
    var placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.disabled = true;
    placeholder.selected = true;
    select.appendChild(placeholder);
    select.setAttribute('placeholder', 'مرتب‌سازی');
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
