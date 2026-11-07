/* Per-plan reseller extra pack rows in the plan modal / edit form. */
(function (root) {
  "use strict";

  const PackRows = typeof module === "object" && module.exports ? require("./pack-rows.js") : root.PackRows;

  const DEFAULTS = {
    gb: [1, 5, 10, 50],
    users: [1, 5, 10, 20],
  };

  function parseCsvInts(raw, fallback) {
    const parts = String(raw || "")
      .split(",")
      .map((p) => p.trim())
      .filter(Boolean)
      .map((p) => Number(p))
      .filter((n) => Number.isInteger(n) && n > 0);
    return parts.length ? parts : fallback.slice();
  }


  function setRowEnabled(row, enabled) {
    row.querySelectorAll("input, select, button").forEach((el) => {
      if (enabled) {
        el.removeAttribute("disabled");
        if (el.matches('input[type="number"]')) el.required = true;
      } else {
        el.setAttribute("disabled", "disabled");
        if (el.matches('input[type="number"]')) el.required = false;
      }
    });
  }

  function setEditorVisible(root, visible) {
    if (!root) return;
    root.hidden = !visible;
    root.querySelectorAll("[data-extra-pack-row]").forEach((row) => setRowEnabled(row, visible));
    root.querySelectorAll("[data-extra-pack-add]").forEach((btn) => {
      btn.hidden = !visible;
      if (visible) btn.removeAttribute("disabled");
      else btn.setAttribute("disabled", "disabled");
    });
  }

  const ROW_SPECS = {
    gb: { rowAttr: "data-extra-pack-row", removeAttr: "data-extra-pack-remove", amountName: "extra_gb_amount", styleName: "extra_gb_style", amountMax: "100000", unit: "گیگ", amountLabel: "مقدار حجم بسته", styleLabel: "رنگ دکمه بسته حجم", removeLabel: "حذف بسته حجم" },
    users: { rowAttr: "data-extra-pack-row", removeAttr: "data-extra-pack-remove", amountName: "extra_user_amount", styleName: "extra_user_style", amountMax: "100000", unit: "نفر", amountLabel: "تعداد کاربر بسته", styleLabel: "رنگ دکمه بسته کاربر", removeLabel: "حذف بسته کاربر" },
  };

  function buildRow(kind, amount, styleValue, styleOptionsHtml) {
    return PackRows.buildRow(ROW_SPECS[kind], amount, styleValue, styleOptionsHtml);
  }

  function styleOptionsFrom(root) {
    const sample = root.querySelector("select.extra-pack-style");
    return sample ? sample.innerHTML : '<option value="inherit">ارث از پیش‌فرض</option>';
  }

  function seedDefaultsIfEmpty(root) {
    if (!root) return;
    ["gb", "users"].forEach((kind) => {
      const box = root.querySelector(`[data-extra-packs-rows="${kind}"]`);
      if (!box || box.querySelector("[data-extra-pack-row]")) return;
      const attr = kind === "gb" ? "data-default-gb" : "data-default-users";
      const amounts = parseCsvInts(root.getAttribute(attr), DEFAULTS[kind]);
      const html = styleOptionsFrom(root);
      amounts.forEach((n) =>
        box.appendChild(buildRow(kind, n, "inherit", html || '<option value="inherit">ارث از پیش‌فرض</option>'))
      );
    });
    PackRows.refresh(root);
  }

  function bind(root) {
    if (!root || root.dataset.bound === "1") return;
    root.dataset.bound = "1";
    PackRows.bindView(root);
    root.addEventListener("click", (event) => {
      const add = event.target.closest("[data-extra-pack-add]");
      if (add && root.contains(add)) {
        const kind = add.getAttribute("data-extra-pack-add");
        const box = root.querySelector(`[data-extra-packs-rows="${kind}"]`);
        if (!box) return;
        const html = styleOptionsFrom(root);
        box.appendChild(buildRow(kind, 1, "inherit", html));
        PackRows.refresh(root);
        return;
      }
      const remove = event.target.closest("[data-extra-pack-remove]");
      if (remove && root.contains(remove)) {
        const row = remove.closest("[data-extra-pack-row]");
        if (row) row.remove();
        PackRows.refresh(root);
      }
    });
  }

  function mount(selector) {
    document.querySelectorAll(selector || "[data-extra-packs-root]").forEach((el) => bind(el));
  }

  const api = {
    DEFAULTS,
    parseCsvInts,
    packPrice: PackRows.packPrice,
    setEditorVisible,
    seedDefaultsIfEmpty,
    bind,
    mount,
    buildRow,
  };

  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.ResellerExtraPacks = api;
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", () => mount());
    } else {
      mount();
    }
  }
})(typeof window !== "undefined" ? window : globalThis);
