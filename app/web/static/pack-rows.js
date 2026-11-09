/* Shared view of the per-plan pack editors: one line per pack with amount, computed price,
   colour dot and delete button, plus the «حجم | ...» tabs with their counters. */
(function (root) {
  "use strict";

  const TRASH_ICON =
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/></svg>';

  /** Price of one pack: unit price × amount (whole toman), or null when either is missing. */
  function packPrice(rate, amount) {
    const r = Number(String(rate == null ? "" : rate).replace(/[^\d]/g, ""));
    const a = Number(amount);
    if (!Number.isFinite(r) || !Number.isFinite(a) || r <= 0 || a <= 0) return null;
    return Math.round(r * a);
  }

  function faDigits(n) {
    return String(n).replace(/\d/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[Number(d)]);
  }

  function escapeAttr(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function syncSelectTone(select) {
    if (!select) return;
    const opt = select.selectedOptions && select.selectedOptions[0];
    const tone = (opt && opt.getAttribute("data-tone")) || "default";
    select.setAttribute("data-tone", tone);
    const host = select.closest && select.closest("[data-ds-dot]");
    if (host) {
      host.setAttribute("data-tone", tone);
      host.setAttribute("data-value", select.value);
    }
  }

  const FALLBACK_STYLE_OPTION = '<option value="inherit">پیش‌فرض</option>';

  /** The colour <option>s for a new pack line: the editor's full list, else an existing line's, else only the default. */
  function styleOptionsHtml(editor) {
    const list = editor && editor.querySelector("template[data-pack-style-options]");
    if (list && list.innerHTML.trim()) return list.innerHTML;
    const sample = editor && editor.querySelector("select.extra-pack-style");
    return sample ? sample.innerHTML : FALLBACK_STYLE_OPTION;
  }

  /** One pack line. spec: {rowAttr, removeAttr, amountName, styleName, amountMax, unit, amountLabel, styleLabel, removeLabel} */
  function buildRow(spec, amount, styleValue, optionsHtml) {
    const wrap = document.createElement("div");
    wrap.className = "ds-list-row extra-pack-row";
    wrap.setAttribute(spec.rowAttr, "");
    wrap.innerHTML =
      `<span class="ds-unit ds-pack-amt"><input name="${spec.amountName}" class="extra-pack-amount" type="number" min="1" ` +
      `max="${spec.amountMax}" step="1" inputmode="numeric" dir="ltr" value="${Number(amount) || 1}" ` +
      `aria-label="${escapeAttr(spec.amountLabel)}" placeholder="مقدار" required />` +
      `<span class="ds-unit-label">${escapeAttr(spec.unit)}</span></span>` +
      `<span class="ds-list-value" data-pack-price></span>` +
      `<span class="ds-dot-select" data-ds-dot data-menu-min-width="168"><select name="${spec.styleName}" ` +
      `class="btn-color-select extra-pack-style" data-tone="default" aria-label="${escapeAttr(spec.styleLabel)}">${optionsHtml}</select></span>` +
      `<button type="button" class="ds-del extra-pack-remove" ${spec.removeAttr} aria-label="${escapeAttr(spec.removeLabel)}">${TRASH_ICON}</button>`;
    const select = wrap.querySelector("select");
    if (select) {
      const want = styleValue == null ? "inherit" : String(styleValue);
      select.value = want;
      if (select.value !== want) select.value = "inherit";
      syncSelectTone(select);
    }
    return wrap;
  }

  function rateInput(list) {
    const sel = list.getAttribute("data-rate");
    const form = list.closest("form");
    return sel && form ? form.querySelector(sel) : null;
  }

  /** Recompute every pack price and tab counter inside an editor root. */
  function refresh(editor) {
    editor.querySelectorAll("[data-pack-list]").forEach((list) => {
      const rate = rateInput(list);
      const rows = list.querySelectorAll(".extra-pack-row");
      rows.forEach((row) => {
        const out = row.querySelector("[data-pack-price]");
        const amount = row.querySelector(".extra-pack-amount");
        if (!out || !amount) return;
        const price = packPrice(rate ? rate.value : "", amount.value);
        out.textContent = "";
        if (price === null) {
          out.textContent = "—";
        } else {
          const num = document.createElement("bdi");
          num.setAttribute("dir", "ltr");
          num.textContent = price.toLocaleString("en-US");
          out.appendChild(num);
        }
      });
      const count = editor.querySelector(`[data-pack-count="${list.getAttribute("data-pack-list")}"]`);
      if (count) count.textContent = faDigits(rows.length);
    });
  }

  function showTab(editor, kind) {
    editor.querySelectorAll("[data-pack-tab]").forEach((tab) => {
      const on = tab.getAttribute("data-pack-tab") === kind;
      tab.classList.toggle("on", on);
      tab.setAttribute("aria-selected", on ? "true" : "false");
    });
    editor.querySelectorAll("[data-pack-list]").forEach((list) => {
      list.hidden = list.getAttribute("data-pack-list") !== kind;
    });
  }

  /** Tabs, live prices and colour dots for one editor root. */
  function bindView(editor) {
    editor.addEventListener("click", (event) => {
      const tab = event.target.closest("[data-pack-tab]");
      if (tab && editor.contains(tab)) showTab(editor, tab.getAttribute("data-pack-tab"));
    });
    editor.addEventListener("change", (event) => {
      if (event.target && event.target.matches("select.btn-color-select")) syncSelectTone(event.target);
    });
    editor.addEventListener("input", () => refresh(editor));
    const form = editor.closest("form");
    if (form) {
      form.addEventListener("input", (event) => {
        const lists = editor.querySelectorAll("[data-pack-list]");
        const isRate = Array.from(lists).some((l) => event.target && event.target.matches(l.getAttribute("data-rate")));
        if (isRate) refresh(editor);
      });
    }
    editor.querySelectorAll("select.btn-color-select").forEach(syncSelectTone);
    refresh(editor);
  }

  const api = { packPrice, faDigits, syncSelectTone, styleOptionsHtml, buildRow, refresh, showTab, bindView, TRASH_ICON };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.PackRows = api;
})(typeof window !== "undefined" ? window : globalThis);
