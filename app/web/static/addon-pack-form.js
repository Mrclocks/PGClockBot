/* General volume/time pack form on the plans page: several pack rows when adding,
   one row when editing an existing pack. */
(function (root) {
  "use strict";

  const MAX_ROWS = 10;
  const KIND_RULES = {
    volume: { unit: "گیگ", step: "0.1", min: "0.1", inputmode: "decimal", hint: "حجم به گیگ — اعشار مجاز است" },
    duration: { unit: "روز", step: "1", min: "1", inputmode: "numeric", hint: "تعداد روز — فقط عدد صحیح" },
  };

  function rulesFor(kind) {
    return KIND_RULES[kind] || KIND_RULES.volume;
  }

  /** Whether another row may be added, and whether a row may be removed. */
  function rowLimits(count, editing) {
    return { canAdd: !editing && count < MAX_ROWS, canRemove: !editing && count > 1 };
  }

  function bind(form) {
    const list = form.querySelector("[data-addon-row-list]");
    const addBtn = form.querySelector("[data-addon-row-add]");
    const kindEl = form.querySelector("#service-addon-kind");
    const hintEl = form.querySelector("#service-addon-amount-hint");
    const unitHead = form.querySelector("[data-addon-unit-head]");
    const template = form.querySelector("template[data-addon-row-template]");
    if (!list || !template) return null;
    // A fresh row from the template: the page enhances inserted selects and number fields itself.
    const blank = template.content.querySelector("[data-addon-row]");
    let editing = false;

    function rows() {
      return Array.from(list.querySelectorAll("[data-addon-row]"));
    }

    function syncKind() {
      const rules = rulesFor(kindEl ? kindEl.value : "volume");
      rows().forEach((row) => {
        const amount = row.querySelector('input[name="amount"]');
        amount.step = rules.step;
        amount.min = rules.min;
        amount.inputMode = rules.inputmode;
        row.querySelector("[data-addon-unit]").textContent = rules.unit;
      });
      if (unitHead) unitHead.textContent = ` (${rules.unit})`;
      if (hintEl) hintEl.textContent = rules.hint;
    }

    function syncLimits() {
      const limits = rowLimits(rows().length, editing);
      if (addBtn) addBtn.hidden = !limits.canAdd;
      rows().forEach((row) => {
        row.querySelector("[data-addon-row-remove]").hidden = !limits.canRemove;
      });
    }

    function syncTone(select) {
      if (root.PackRows) root.PackRows.syncSelectTone(select);
    }

    function addRow() {
      const row = blank.cloneNode(true);
      list.appendChild(row);
      // Number fields, money formatting and the colour menu are enhanced by the page on this event.
      document.dispatchEvent(new CustomEvent("panel:dom-ready", { detail: { root: row } }));
      syncTone(row.querySelector("select"));
      syncKind();
      syncLimits();
      return row;
    }

    function setRows(count) {
      rows().slice(count).forEach((row) => row.remove());
      while (rows().length < count) addRow();
      syncKind();
      syncLimits();
    }

    list.addEventListener("click", (event) => {
      const remove = event.target.closest("[data-addon-row-remove]");
      if (!remove || rowLimits(rows().length, editing).canRemove === false) return;
      remove.closest("[data-addon-row]").remove();
      syncLimits();
    });
    list.addEventListener("change", (event) => {
      if (event.target.matches("select.btn-color-select")) syncTone(event.target);
    });
    if (addBtn) addBtn.addEventListener("click", () => {
      const row = addRow();
      row.querySelector('input[name="amount"]').focus();
    });
    if (kindEl) kindEl.addEventListener("change", syncKind);
    syncKind();
    syncLimits();

    return {
      /** Edit mode: one row filled from the pack; add and remove are hidden. */
      editPack(values) {
        editing = true;
        setRows(1);
        const row = rows()[0];
        row.querySelector('input[name="amount"]').value = values.amount || "";
        const price = row.querySelector('input[name="price"]');
        price.value = values.price || "";
        price.dispatchEvent(new Event("input", { bubbles: true }));
        const select = row.querySelector("select");
        select.value = values.style || "inherit";
        if (select.value !== (values.style || "inherit")) select.value = "inherit";
        syncTone(select);
        syncLimits();
      },
      /** Back to adding: one empty row. */
      reset() {
        editing = false;
        rows().forEach((row) => row.remove());
        addRow();
      },
      syncKind,
    };
  }

  const api = { rulesFor, rowLimits, bind, MAX_ROWS };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.AddonPackForm = api;
})(typeof window !== "undefined" ? window : globalThis);
