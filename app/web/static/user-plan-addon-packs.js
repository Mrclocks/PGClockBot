/* Plan-scoped customer volume/duration stairs on the fixed user-plan form. */
(function (root) {
  "use strict";

  const PackRows = typeof module === "object" && module.exports ? require("./pack-rows.js") : root.PackRows;

  const DEFAULTS = {
    volume: [1, 5, 10, 50],
    duration: [1, 3, 6, 12],
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
    root.querySelectorAll("[data-user-pack-row]").forEach((row) => setRowEnabled(row, visible));
    root.querySelectorAll("[data-user-pack-add]").forEach((btn) => {
      btn.hidden = !visible;
      if (visible) btn.removeAttribute("disabled");
      else btn.setAttribute("disabled", "disabled");
    });
  }

  function styleOptionsFrom(root) {
    const sample = root.querySelector("select.extra-pack-style");
    return sample ? sample.innerHTML : '<option value="inherit">ارث از پیش‌فرض</option>';
  }

  const ROW_SPECS = {
    volume: { rowAttr: "data-user-pack-row", removeAttr: "data-user-pack-remove", amountName: "plan_vol_amount", styleName: "plan_vol_style", amountMax: "100000", unit: "گیگ", amountLabel: "مقدار حجم بسته", styleLabel: "رنگ دکمه بسته حجم", removeLabel: "حذف بسته حجم" },
    duration: { rowAttr: "data-user-pack-row", removeAttr: "data-user-pack-remove", amountName: "plan_dur_amount", styleName: "plan_dur_style", amountMax: "120", unit: "ماه", amountLabel: "مقدار ماه بسته", styleLabel: "رنگ دکمه بسته زمان", removeLabel: "حذف بسته زمان" },
  };

  function buildRow(kind, amount, styleValue, styleOptionsHtml) {
    return PackRows.buildRow(ROW_SPECS[kind], amount, styleValue, styleOptionsHtml);
  }

  function seedDefaultsIfEmpty(root) {
    if (!root) return;
    ["volume", "duration"].forEach((kind) => {
      const box = root.querySelector(`[data-user-pack-rows="${kind}"]`);
      if (!box || box.querySelector("[data-user-pack-row]")) return;
      const attr = kind === "volume" ? "data-default-volume" : "data-default-duration";
      const amounts = parseCsvInts(root.getAttribute(attr), DEFAULTS[kind]);
      const html = styleOptionsFrom(root);
      amounts.forEach((n) => box.appendChild(buildRow(kind, n, "inherit", html)));
    });
    PackRows.refresh(root);
  }

  function bind(root) {
    if (!root || root.dataset.bound === "1") return;
    root.dataset.bound = "1";
    PackRows.bindView(root);
    root.addEventListener("click", (event) => {
      const add = event.target.closest("[data-user-pack-add]");
      if (add && root.contains(add)) {
        const kind = add.getAttribute("data-user-pack-add");
        const box = root.querySelector(`[data-user-pack-rows="${kind}"]`);
        if (!box) return;
        box.appendChild(buildRow(kind, 1, "inherit", styleOptionsFrom(root)));
        PackRows.refresh(root);
        return;
      }
      const remove = event.target.closest("[data-user-pack-remove]");
      if (remove && root.contains(remove)) {
        const row = remove.closest("[data-user-pack-row]");
        if (row) row.remove();
        PackRows.refresh(root);
      }
    });
  }

  function mount(selector) {
    document.querySelectorAll(selector || "[data-user-plan-packs-root]").forEach((el) => bind(el));
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
    root.UserPlanAddonPacks = api;
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", () => mount());
    } else {
      mount();
    }
  }
})(typeof window !== "undefined" ? window : globalThis);
