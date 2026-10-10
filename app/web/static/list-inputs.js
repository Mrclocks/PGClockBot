/* Edit ordered list fields while preserving their existing form payloads. */
(function (root) {
  "use strict";

  const SEPARATORS = /[,،;؛\r\n]+/;
  const LIMITS = {telegram: ["1", "9223372036854775807"], money: ["1000", "2147483647"]};
  const ICON_PATHS = {
    add: "M8 3v10M3 8h10",
    edit: "M10 3l3 3M3 13l3-1 7-7-3-3-7 7-1 4Z",
    delete: "M2 4h12M6 4V2h4v2M4 4l1 10h6l1-10M7 7v4M9 7v4",
    check: "M3 8l3 3 7-7",
    close: "M4 4l8 8M12 4l-8 8",
  };
  const ACTION_ICONS = {
    save: "add", cancel: "close", edit: "edit", delete: "delete",
    "confirm-delete": "delete", "cancel-delete": "close",
  };
  const MESSAGES = {
    empty: "مقدار را وارد کنید.",
    duplicate: "این مقدار قبلاً در فهرست وجود دارد.",
    single: "هر بار یک مقدار وارد کنید.",
    telegram: "آیدی باید یک عدد صحیح مثبت و در محدودهٔ مجاز باشد.",
    money: "مبلغ باید عدد صحیح و بین ۱٬۰۰۰ تا ۲٬۱۴۷٬۴۸۳٬۶۴۷ تومان باشد.",
    required: "حداقل یک مورد اضافه کنید.",
  };

  function normalizeItem(raw, kind, normalize) {
    const value = String(raw == null ? "" : raw).trim();
    if (kind === "telegram" && /\s/.test(value)) return value;
    return LIMITS[kind] ? normalize(value).replace(/^0+(?=\d)/, "") : value;
  }

  function parseItems(raw, kind, normalize) {
    return String(raw == null ? "" : raw).split(SEPARATORS)
      .map((part) => normalizeItem(part, kind, normalize)).filter(Boolean);
  }

  function compareIntegers(left, right) {
    return left.length === right.length ? left.localeCompare(right) : left.length - right.length;
  }

  function validateItem(raw, kind, normalize) {
    const text = String(raw == null ? "" : raw).trim();
    if (!text) throw new Error(MESSAGES.empty);
    // A comma in a money entry is digit grouping, never another preset.
    if (kind !== "money" && SEPARATORS.test(text)) throw new Error(MESSAGES.single);
    const value = normalizeItem(text, kind, normalize);
    const limits = LIMITS[kind];
    if (limits && (!/^\d+$/.test(value) || compareIntegers(value, limits[0]) < 0 ||
        compareIntegers(value, limits[1]) > 0)) throw new Error(MESSAGES[kind]);
    return value;
  }

  function changeItem(items, raw, options) {
    const {kind, normalize, index = -1} = options;
    const value = validateItem(raw, kind, normalize);
    if (items.some((item, position) => position !== index && item === value)) {
      throw new Error(MESSAGES.duplicate);
    }
    const next = items.slice();
    if (index === -1) next.push(value);
    else if (index >= 0 && index < items.length) next[index] = value;
    else throw new RangeError("Invalid list item index");
    return next;
  }

  function removeItem(items, index) {
    if (!Number.isInteger(index) || index < 0 || index >= items.length) {
      throw new RangeError("Invalid list item index");
    }
    return items.filter((item, position) => position !== index);
  }

  const states = new WeakMap();
  const countFormat = new Intl.NumberFormat("fa-IR");
  let sequence = 0;
  let openState = null;

  if (typeof module === "object" && module.exports) {
    module.exports = {parseItems, validateItem, changeItem, removeItem, button, setButtonIcon, createControls, renderItems};
    return;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function setButtonIcon(node, icon, label) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "ico");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", ICON_PATHS[icon]);
    svg.appendChild(path);
    node.replaceChildren(svg);
    node.classList.toggle("icon-add", icon === "add" || icon === "check");
    node.classList.toggle("icon-remove", icon === "delete");
    node.setAttribute("aria-label", label);
    node.title = label;
  }

  function button(label, action, index) {
    const node = element("button", "icon-btn");
    node.type = "button";
    node.dataset.listAction = action;
    if (index != null) node.dataset.index = String(index);
    if (ACTION_ICONS[action]) setButtonIcon(node, ACTION_ICONS[action], label);
    return node;
  }

  function displayValue(state, value) {
    return state.kind === "money" ? root.PanelMoney.groupMoneyDigits(value) : value;
  }

  function renderSummary(state) {
    state.summary.replaceChildren();
    if (!state.items.length) {
      state.summary.appendChild(element("span", "muted", "افزودن اولین مورد"));
    }
    state.items.slice(0, 2).forEach((value) => {
      const chip = element("span", "list-editor-chip", displayValue(state, value));
      chip.dir = "ltr";
      chip.title = value;
      state.summary.appendChild(chip);
    });
    if (state.items.length > 2) {
      state.summary.appendChild(element("span", "muted", "+" + countFormat.format(state.items.length - 2)));
    }
    state.toggle.setAttribute("aria-label", state.label + "؛ " + countFormat.format(state.items.length) + " مورد؛ ویرایش فهرست");
    state.toggle.disabled = state.source.disabled || state.source.readOnly;
  }

  function renderRow(state, value, index) {
    const row = element("li", "list-editor-row");
    const copy = element("span", "list-editor-value", displayValue(state, value));
    copy.dir = "ltr";
    row.appendChild(copy);
    const actions = element("div", "list-editor-actions");
    if (state.deleting === index) {
      row.classList.add("is-confirming");
      row.appendChild(element("span", "list-editor-confirm", "این مورد از فهرست حذف شود؟"));
      const remove = button("تأیید حذف " + value, "confirm-delete", index);
      actions.append(remove, button("انصراف", "cancel-delete", index));
    } else {
      const edit = button("ویرایش " + value, "edit", index);
      const remove = button("حذف " + value, "delete", index);
      actions.append(edit, remove);
    }
    row.appendChild(actions);
    return row;
  }

  function renderItems(state) {
    const rows = state.items.map((value, index) => renderRow(state, value, index));
    state.itemsList.replaceChildren(...rows);
    state.empty.hidden = rows.length > 0;
    state.empty.textContent = "هنوز موردی اضافه نشده است.";
  }

  function showError(state, message) {
    state.error.textContent = message;
    state.error.hidden = !message;
    state.draft.setAttribute("aria-invalid", message ? "true" : "false");
    state.wrap.classList.toggle("is-invalid", !!message);
  }

  function resetDraft(state) {
    state.editing = -1;
    state.deleting = -1;
    state.draft.value = "";
    setButtonIcon(state.save, "add", "افزودن");
    state.draftLabel.textContent = "مقدار جدید";
    state.cancel.hidden = true;
    showError(state, "");
  }

  function readSource(state) {
    if (state.source.value !== state.lastValue) {
      state.resetValue = state.source.value;
      state.items = parseItems(state.source.value, state.kind, state.normalize);
      state.lastValue = state.source.value;
      resetDraft(state);
    }
    renderSummary(state);
    renderItems(state);
  }

  function syncSource(state, announcement) {
    state.source.value = state.items.join(",");
    state.lastValue = state.source.value;
    state.source.dispatchEvent(new Event("input", {bubbles: true}));
    state.source.dispatchEvent(new Event("change", {bubbles: true}));
    resetDraft(state);
    renderSummary(state);
    renderItems(state);
    state.live.textContent = announcement;
  }

  function closeEditor(state, restoreFocus = false) {
    state.panel.hidden = true;
    state.toggle.setAttribute("aria-expanded", "false");
    state.wrap.classList.remove("is-open");
    if (openState === state) openState = null;
    if (restoreFocus) state.toggle.focus();
  }

  function openEditor(state) {
    if (state.source.disabled || state.source.readOnly) return;
    if (openState && openState !== state) closeEditor(openState);
    readSource(state);
    state.panel.hidden = false;
    state.toggle.setAttribute("aria-expanded", "true");
    state.wrap.classList.add("is-open");
    openState = state;
    state.draft.focus();
  }

  function commitDraft(state) {
    try {
      state.items = changeItem(state.items, state.draft.value, {
        kind: state.kind, normalize: state.normalize, index: state.editing,
      });
      syncSource(state, state.editing === -1 ? "مورد اضافه شد." : "مورد ویرایش شد.");
      return true;
    } catch (error) {
      openEditor(state);
      showError(state, error.message);
      return false;
    }
  }

  function editItem(state, index) {
    state.editing = index;
    state.deleting = -1;
    state.draft.value = displayValue(state, state.items[index]);
    setButtonIcon(state.save, "check", "ثبت ویرایش");
    state.draftLabel.textContent = "ویرایش مقدار";
    state.cancel.hidden = false;
    showError(state, "");
    renderItems(state);
    state.draft.focus();
    state.draft.select();
  }

  function handleAction(state, action, index) {
    if (action === "save") {commitDraft(state); state.draft.focus();}
    if (action === "cancel") {resetDraft(state); renderItems(state); state.draft.focus();}
    if (action === "edit") editItem(state, index);
    if (action === "delete" || action === "cancel-delete") {
      state.deleting = action === "delete" ? index : -1;
      renderItems(state);
      const next = action === "delete" ? "confirm-delete" : "delete";
      const control = state.itemsList.querySelector('[data-list-action="' + next + '"][data-index="' + index + '"]');
      (control || state.draft).focus();
    }
    if (action === "confirm-delete") {
      // Discard an edit before removing a row so its index cannot target another value.
      state.items = removeItem(state.items, index);
      syncSource(state, "مورد از فهرست حذف شد.");
      state.draft.focus();
    }
  }

  function handleKeys(state, event) {
    if (event.isComposing) return;
    if (event.key === "Escape" && !state.panel.hidden) {
      event.preventDefault();
      event.stopPropagation();
      if (state.editing !== -1) {resetDraft(state); renderItems(state); state.draft.focus();}
      else closeEditor(state, true);
    } else if (event.key === "Enter" && event.target === state.draft) {
      event.preventDefault();
      commitDraft(state);
    } else if (event.key === "ArrowDown" && event.target === state.toggle) {
      event.preventDefault();
      openEditor(state);
    } else if (event.key === "Tab") {
      setTimeout(() => {if (!state.wrap.contains(document.activeElement)) closeEditor(state);}, 0);
    }
  }

  function createControls(state) {
    const id = "list-editor-" + (++sequence);
    state.wrap = element("div", "list-editor");
    state.toggle = button("", "toggle");
    state.toggle.className = "list-editor-toggle";
    state.toggle.id = id + "-toggle";
    state.toggle.setAttribute("aria-expanded", "false");
    state.toggle.setAttribute("aria-controls", id + "-panel");
    state.summary = element("span", "list-editor-summary");
    const caret = element("span", "list-editor-caret", "⌄");
    caret.setAttribute("aria-hidden", "true");
    state.toggle.append(state.summary, caret);
    state.panel = element("div", "list-editor-panel");
    state.panel.id = id + "-panel";
    state.panel.hidden = true;
    state.panel.setAttribute("role", "group");
    state.panel.setAttribute("aria-label", "ویرایش فهرست " + state.label);
    state.draftLabel = element("label", "list-editor-draft-label", "مقدار جدید");
    state.draftLabel.htmlFor = id + "-draft";
    state.draft = element("input", "list-editor-draft");
    state.draft.id = id + "-draft";
    state.draft.dir = "ltr";
    state.draft.autocomplete = "off";
    state.draft.placeholder = state.source.placeholder || "مقدار جدید";
    state.draft.dataset.normalizeDigits = "0";
    if (LIMITS[state.kind]) state.draft.inputMode = "numeric";
    state.save = button("افزودن", "save");
    state.cancel = button("انصراف", "cancel");
    state.cancel.hidden = true;
    const entry = element("div", "list-editor-entry");
    entry.append(state.draft, state.save, state.cancel);
    state.error = element("small", "field-error");
    state.error.id = id + "-error";
    state.error.setAttribute("role", "alert");
    state.error.hidden = true;
    state.draft.setAttribute("aria-describedby", state.error.id);
    state.itemsList = element("ul", "list-editor-items");
    state.empty = element("p", "muted list-editor-empty");
    state.live = element("span", "sr-only");
    state.live.setAttribute("aria-live", "polite");
    state.panel.append(state.draftLabel, entry, state.error, state.itemsList, state.empty,
      element("small", "muted", "تغییرات با ذخیرهٔ فرم اعمال می‌شوند."));
    state.wrap.append(state.toggle, state.panel, state.live);
  }

  function bind(source) {
    if (states.has(source)) return;
    const state = {
      source, kind: source.dataset.listEditor, label: source.getAttribute("aria-label") || "فهرست",
      normalize: root.normalizePanelNumberText, required: source.required,
      items: [], lastValue: null, editing: -1, deleting: -1,
    };
    createControls(state);
    source.before(state.wrap);
    source.type = "hidden";
    states.set(source, state);
    const label = source.parentElement.querySelector("label");
    if (label && label.htmlFor === source.id) label.htmlFor = state.toggle.id;
    readSource(state);
    source.addEventListener("input", () => readSource(state));
    source.addEventListener("change", () => readSource(state));
    state.wrap.addEventListener("click", (event) => {
      const control = event.target.closest("[data-list-action]");
      if (!control) return;
      // Editing rebuilds the clicked row; its detached target is still inside this action.
      event.stopPropagation();
      const action = control.dataset.listAction;
      if (action === "toggle") {
        if (state.panel.hidden) openEditor(state);
        else closeEditor(state);
      } else handleAction(state, action, Number(control.dataset.index));
    });
    state.wrap.addEventListener("keydown", (event) => handleKeys(state, event));
    state.draft.addEventListener("input", () => showError(state, ""));
    new MutationObserver(() => {
      renderSummary(state);
      if (source.disabled || source.readOnly) closeEditor(state);
    }).observe(source, {attributes: true, attributeFilter: ["disabled", "readonly"]});
  }

  function enhance(scope) {
    if (scope.matches && scope.matches("input[data-list-editor]")) bind(scope);
    scope.querySelectorAll("input[data-list-editor]").forEach(bind);
  }

  function prepareSubmit(event) {
    let firstInvalid = null;
    event.target.querySelectorAll("input[data-list-editor]").forEach((source) => {
      const state = states.get(source);
      if (!state || source.disabled || source.readOnly) return;
      readSource(state);
      if (state.draft.value.trim() && !commitDraft(state)) {
        firstInvalid = firstInvalid || state;
        return;
      }
      try {
        state.items.forEach((item) => validateItem(item, state.kind, state.normalize));
        if (state.required && !state.items.length) throw new Error(MESSAGES.required);
        source.value = state.items.join(",");
        state.lastValue = source.value;
      } catch (error) {
        showError(state, error.message);
        firstInvalid = firstInvalid || state;
      }
    });
    if (firstInvalid) {
      event.preventDefault();
      event.stopImmediatePropagation();
      openEditor(firstInvalid);
    }
  }

  function boot() {
    enhance(document);
    root.addEventListener("submit", prepareSubmit, true);
    // Closing on pointerdown would move a submit button before its click fires.
    document.addEventListener("click", (event) => {
      if (openState && !openState.wrap.contains(event.target)) closeEditor(openState);
    });
    document.addEventListener("panel:dom-ready", (event) => enhance((event.detail && event.detail.root) || document));
    document.addEventListener("reset", (event) => {
      setTimeout(() => event.target.querySelectorAll("input[data-list-editor]").forEach((source) => {
        const state = states.get(source);
        if (!state) return;
        // Hidden input values also change defaultValue; retain the loaded list.
        source.value = state.resetValue;
        state.lastValue = null;
        readSource(state);
        closeEditor(state);
      }), 0);
    });
    new MutationObserver((mutations) => {
      mutations.forEach((mutation) => mutation.addedNodes.forEach((node) => {
        if (node.nodeType === 1) enhance(node);
      }));
      if (openState && (!openState.wrap.isConnected || openState.wrap.closest("[hidden], .is-closing"))) {
        closeEditor(openState);
      }
    }).observe(document.body, {childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "class"]});
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, {once: true});
  else boot();
})(typeof window === "object" ? window : globalThis);
