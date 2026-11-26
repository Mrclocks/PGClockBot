/* Buttons under a staff message: one collapsible row per button (what it opens, text, link or plan, colour),
   one to four rows, optionally filled from a saved set, mirrored in a Telegram-style preview. Every row keeps
   all five form fields so the server reads them by position; the server checks every row again. */
(function (root) {
  "use strict";

  const LINK = "link";
  const PLAN = "plan";
  const PER_ROW = 2;
  const NEW_TITLE = "دکمهٔ تازه";
  const PICK_KIND = "کارش را انتخاب کنید";
  const ENTITIES = { "&lt;": "<", "&gt;": ">", "&amp;": "&", "&quot;": '"', "&#39;": "'", "&nbsp;": " " };

  /** Whether another row may be added. */
  function canAdd(count, max) {
    return count < max;
  }

  /** The link field shows only for a link button. */
  function needsUrl(target) {
    return target === LINK;
  }

  /** The plan field shows only for a plan button. */
  function needsPlan(target) {
    return target === PLAN;
  }

  /** The buttons of saved set ``id`` from the list's data, at most ``max``. */
  function presetButtons(presets, id, max) {
    const found = (Array.isArray(presets) ? presets : []).find((p) => String(p.id) === String(id));
    return found && Array.isArray(found.buttons) ? found.buttons.slice(0, max) : [];
  }

  /** Buttons in rows of ``perRow``, as Telegram shows them under the message. */
  function previewRows(buttons, perRow) {
    const size = perRow || PER_ROW;
    const rows = [];
    for (let i = 0; i < buttons.length; i += size) rows.push(buttons.slice(i, i + size));
    return rows;
  }

  /** The text a customer reads: Telegram tags dropped, common entities decoded. Shown with textContent only. */
  function plainText(html) {
    return String(html || "")
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<[^>]*>/g, "")
      .replace(/&(lt|gt|amp|quot|#39|nbsp);/g, (m) => ENTITIES[m] || m);
  }

  /** The buttons a list starts with (``data-initial``, e.g. an FAQ answer's saved buttons), at most ``max``. */
  function initialButtons(raw, max) {
    let parsed = [];
    try {
      parsed = JSON.parse(raw || "[]");
    } catch (_err) {
      return [];
    }
    return Array.isArray(parsed) ? parsed.filter((b) => b && typeof b === "object" && b.target).slice(0, max) : [];
  }

  function readPresets(list) {
    try {
      return JSON.parse(list.getAttribute("data-presets") || "[]");
    } catch (_err) {
      return [];
    }
  }

  // The panel moves a select's name onto a hidden mirror input, so fields are found by their data attribute.
  const FIELDS = {
    btn_target: "[data-msg-btn-target]",
    btn_label: "[data-msg-btn-label]",
    btn_url: "[data-msg-btn-url]",
    btn_plan: "[data-msg-btn-plan]",
    btn_style: "[data-msg-btn-style]",
  };

  function field(row, name) {
    return row.querySelector(FIELDS[name]);
  }

  function optionText(select) {
    const opt = select && select.selectedOptions && select.selectedOptions[0];
    return opt && opt.value ? opt.textContent.trim() : "";
  }

  /** What one row will send, as the preview and the summary need it. */
  function rowState(row) {
    const target = field(row, "btn_target").value;
    const kind = optionText(field(row, "btn_target"));
    const plan = optionText(field(row, "btn_plan"));
    const typed = field(row, "btn_label").value.trim();
    const style = field(row, "btn_style");
    const tone = (style.selectedOptions[0] && style.selectedOptions[0].getAttribute("data-tone")) || "default";
    return { target, kind, plan, label: typed || (target === PLAN && plan ? "💎 " + plan : kind), tone };
  }

  function bind(list) {
    const rowsEl = list.querySelector("[data-msg-btn-rows]");
    const template = list.querySelector("template[data-msg-btn-template]");
    const addBtn = list.querySelector("[data-msg-btn-add]");
    if (!rowsEl || !template) return null;
    const max = Number(list.getAttribute("data-max")) || 4;
    const blank = template.content.querySelector("[data-msg-btn-row]");
    const scope = list.closest("form") || document;
    const preview = list.getAttribute("data-preview") ? document.querySelector(list.getAttribute("data-preview")) : null;

    const rows = () => Array.from(rowsEl.querySelectorAll("[data-msg-btn-row]"));

    function syncRow(row) {
      const state = rowState(row);
      row.querySelector("[data-msg-btn-url-field]").hidden = !needsUrl(state.target);
      row.querySelector("[data-msg-btn-plan-field]").hidden = !needsPlan(state.target);
      field(row, "btn_label").placeholder = state.kind ? "خالی بماند: " + state.kind : "خالی بماند: متن پیش‌فرض";
      row.querySelector("[data-msg-btn-title]").textContent = state.label || NEW_TITLE;
      row.querySelector("[data-msg-btn-kind]").textContent = state.kind || PICK_KIND;
      row.querySelector("[data-msg-btn-dot]").setAttribute("data-tone", state.tone);
      if (root.PackRows) root.PackRows.syncSelectTone(field(row, "btn_style"));
    }

    function renderPreview() {
      if (!preview) return;
      const keys = preview.querySelector("[data-msg-preview-keys]");
      keys.textContent = "";
      const shown = rows().map((row, index) => ({ row, index, state: rowState(row) })).filter((b) => b.state.target);
      previewRows(shown).forEach((line) => {
        const lineEl = document.createElement("div");
        lineEl.className = "tg-key-row";
        line.forEach((b) => {
          const key = document.createElement("button");
          key.type = "button";
          key.className = "tg-key";
          key.setAttribute("data-tone", b.state.tone);
          key.setAttribute("data-msg-btn-open", String(b.index));
          key.textContent = b.state.label;
          lineEl.appendChild(key);
        });
        keys.appendChild(lineEl);
      });
    }

    function syncAll() {
      rows().forEach(syncRow);
      if (addBtn) addBtn.hidden = !canAdd(rows().length, max);
      renderPreview();
    }

    function addRow(values, open) {
      if (!canAdd(rows().length, max)) return null;
      const row = blank.cloneNode(true);
      const v = values || {};
      field(row, "btn_target").value = v.target || "";
      field(row, "btn_label").value = v.label || "";
      field(row, "btn_url").value = v.url || "";
      field(row, "btn_plan").value = v.plan || "";
      field(row, "btn_style").value = v.style || "";
      row.open = open !== false;
      rows().forEach((other) => { if (row.open) other.open = false; });
      rowsEl.appendChild(row);
      // Chips, the colour menu and selects are enhanced by the page on this event.
      document.dispatchEvent(new CustomEvent("panel:dom-ready", { detail: { root: row } }));
      syncAll();
      return row;
    }

    function openRow(index) {
      const row = rows()[index];
      if (!row) return;
      row.open = true;
      row.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }

    rowsEl.addEventListener("click", (event) => {
      const remove = event.target.closest("[data-msg-btn-remove]");
      const move = event.target.closest("[data-msg-btn-move]");
      const row = event.target.closest("[data-msg-btn-row]");
      if (remove && row) {
        row.remove();
        syncAll();
      } else if (move && row) {
        const sibling = Number(move.getAttribute("data-msg-btn-move")) < 0 ? row.previousElementSibling : row.nextElementSibling;
        if (sibling) {
          if (Number(move.getAttribute("data-msg-btn-move")) < 0) rowsEl.insertBefore(row, sibling);
          else rowsEl.insertBefore(sibling, row);
          syncAll();
        }
      }
    });
    rowsEl.addEventListener("change", syncAll);
    rowsEl.addEventListener("input", syncAll);
    if (addBtn) {
      addBtn.addEventListener("click", () => {
        const row = addRow(null, true);
        if (row) row.querySelector(".msg-btn-chips button").focus();
      });
    }
    if (preview) {
      preview.addEventListener("click", (event) => {
        const key = event.target.closest("[data-msg-btn-open]");
        if (key) openRow(Number(key.getAttribute("data-msg-btn-open")));
      });
    }
    scope.addEventListener("click", (event) => {
      const chip = event.target.closest("[data-msg-btn-preset]");
      if (!chip || !scope.contains(list)) return;
      const buttons = presetButtons(readPresets(list), chip.getAttribute("data-msg-btn-preset"), max);
      if (!buttons.length) return;
      rows().forEach((row) => row.remove());
      buttons.forEach((b) => addRow(b, false));
    });
    initialButtons(list.getAttribute("data-initial"), max).forEach((b) => addRow(b, false));
    syncAll();
    return { addRow, rows, syncAll };
  }

  /** Mirror the message text into each preview bubble on the page. */
  function bindPreviewText(preview) {
    const source = document.querySelector(preview.getAttribute("data-source") || "");
    const textEl = preview.querySelector("[data-msg-preview-text]");
    if (!source || !textEl) return;
    const empty = preview.getAttribute("data-empty") || "";
    const sync = () => {
      const text = plainText(source.value).trim();
      textEl.textContent = text || empty;
      textEl.classList.toggle("is-empty", !text);
    };
    source.addEventListener("input", sync);
    sync();
  }

  function bindAll(scope) {
    (scope || document).querySelectorAll("[data-msg-btn-list]").forEach((list) => {
      if (list.dataset.msgBtnBound) return;
      list.dataset.msgBtnBound = "1";
      bind(list);
    });
    (scope || document).querySelectorAll("[data-msg-preview]").forEach((preview) => {
      if (preview.dataset.msgPreviewBound) return;
      preview.dataset.msgPreviewBound = "1";
      bindPreviewText(preview);
    });
  }

  const api = { canAdd, needsUrl, needsPlan, presetButtons, initialButtons, previewRows, plainText, bind, bindAll };
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.MessageButtons = api;
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => bindAll());
    else bindAll();
  }
})(typeof window !== "undefined" ? window : globalThis);
