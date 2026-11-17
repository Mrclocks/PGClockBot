/* Buttons under a staff message: rows of target, text, colour (and a link field for «لینک»),
   one to four rows, optionally filled from a saved set. The server checks every row again. */
(function (root) {
  "use strict";

  const LINK = "link";

  /** Whether another row may be added. */
  function canAdd(count, max) {
    return count < max;
  }

  /** The link field shows only for a link button. */
  function needsUrl(target) {
    return target === LINK;
  }

  /** The buttons of saved set ``id`` from the list's data, at most ``max``. */
  function presetButtons(presets, id, max) {
    const found = (Array.isArray(presets) ? presets : []).find((p) => String(p.id) === String(id));
    return found && Array.isArray(found.buttons) ? found.buttons.slice(0, max) : [];
  }

  function readPresets(list) {
    try {
      return JSON.parse(list.getAttribute("data-presets") || "[]");
    } catch (_err) {
      return [];
    }
  }

  function bind(list) {
    const rowsEl = list.querySelector("[data-msg-btn-rows]");
    const template = list.querySelector("template[data-msg-btn-template]");
    const addBtn = list.querySelector("[data-msg-btn-add]");
    if (!rowsEl || !template) return null;
    const max = Number(list.getAttribute("data-max")) || 4;
    const blank = template.content.querySelector("[data-msg-btn-row]");

    const rows = () => Array.from(rowsEl.querySelectorAll("[data-msg-btn-row]"));

    function syncRow(row) {
      const target = row.querySelector("[data-msg-btn-target]");
      const url = row.querySelector("[data-msg-btn-url]");
      if (target && url) url.hidden = !needsUrl(target.value);
    }

    function syncTone(select) {
      if (select && root.PackRows) root.PackRows.syncSelectTone(select);
    }

    function syncAdd() {
      if (addBtn) addBtn.hidden = !canAdd(rows().length, max);
    }

    function addRow(values) {
      if (!canAdd(rows().length, max)) return null;
      const row = blank.cloneNode(true);
      const v = values || {};
      row.querySelector('[name="btn_target"]').value = v.target || "";
      row.querySelector('[name="btn_label"]').value = v.label || "";
      row.querySelector('[name="btn_url"]').value = v.url || "";
      const style = row.querySelector('[name="btn_style"]');
      style.value = v.style || "";
      rowsEl.appendChild(row);
      // The colour menu and selects are enhanced by the page on this event.
      document.dispatchEvent(new CustomEvent("panel:dom-ready", { detail: { root: row } }));
      syncTone(style);
      syncRow(row);
      syncAdd();
      return row;
    }

    rowsEl.addEventListener("click", (event) => {
      const remove = event.target.closest("[data-msg-btn-remove]");
      if (!remove) return;
      remove.closest("[data-msg-btn-row]").remove();
      syncAdd();
    });
    rowsEl.addEventListener("change", (event) => {
      const row = event.target.closest("[data-msg-btn-row]");
      if (event.target.matches("[data-msg-btn-target]") && row) syncRow(row);
      if (event.target.matches("select.btn-color-select")) syncTone(event.target);
    });
    if (addBtn) {
      addBtn.addEventListener("click", () => {
        const row = addRow();
        if (row) row.querySelector("[data-msg-btn-target]").focus();
      });
    }
    const field = list.closest(".form-field");
    const presetSelect = field && field.querySelector("[data-msg-btn-preset]");
    if (presetSelect) {
      presetSelect.addEventListener("change", () => {
        const buttons = presetButtons(readPresets(list), presetSelect.value, max);
        if (!buttons.length) return;
        rows().forEach((row) => row.remove());
        buttons.forEach((b) => addRow(b));
      });
    }
    syncAdd();
    return { addRow, rows };
  }

  function bindAll(scope) {
    (scope || document).querySelectorAll("[data-msg-btn-list]").forEach((list) => {
      if (list.dataset.msgBtnBound) return;
      list.dataset.msgBtnBound = "1";
      bind(list);
    });
  }

  const api = { canAdd, needsUrl, presetButtons, bind, bindAll };
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.MessageButtons = api;
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => bindAll());
    else bindAll();
  }
})(typeof window !== "undefined" ? window : globalThis);
