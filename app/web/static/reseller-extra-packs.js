/* Per-plan reseller extra pack rows in the plan modal / edit form. */
(function (root) {
  "use strict";

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

  function syncSelectTone(select) {
    if (!select) return;
    const opt = select.selectedOptions && select.selectedOptions[0];
    const tone = (opt && opt.getAttribute("data-tone")) || "default";
    select.setAttribute("data-tone", tone);
  }

  function setRowEnabled(row, enabled) {
    row.querySelectorAll("input, select, button").forEach((el) => {
      if (el.hasAttribute("data-extra-pack-add")) return;
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
  }

  function buildRow(kind, amount, styleValue, styleOptionsHtml) {
    const amountName = kind === "gb" ? "extra_gb_amount" : "extra_user_amount";
    const styleName = kind === "gb" ? "extra_gb_style" : "extra_user_style";
    const amountLabel = kind === "gb" ? "مقدار حجم بسته" : "تعداد کاربر بسته";
    const styleLabel = kind === "gb" ? "رنگ دکمه بسته حجم" : "رنگ دکمه بسته کاربر";
    const removeLabel = kind === "gb" ? "حذف بسته حجم" : "حذف بسته کاربر";
    const wrap = document.createElement("div");
    wrap.className = "form-row extra-pack-row";
    wrap.setAttribute("data-extra-pack-row", "");
    wrap.innerHTML =
      '<label class="form-field">مقدار' +
      `<input name="${amountName}" type="number" min="1" max="100000" step="1" inputmode="numeric" dir="ltr" value="${Number(amount) || 1}" aria-label="${amountLabel}" required />` +
      "</label>" +
      '<div class="plan-color-field"><div class="btn-color-card plan-color-card">' +
      '<span class="btn-color-label">رنگ دکمه</span>' +
      `<select name="${styleName}" class="btn-color-select" data-tone="default" aria-label="${styleLabel}">${styleOptionsHtml}</select>` +
      "</div></div>" +
      `<button type="button" class="btn btn-sm btn-ghost" data-extra-pack-remove aria-label="${removeLabel}">حذف</button>`;
    const select = wrap.querySelector("select");
    if (select) {
      const want = styleValue == null ? "inherit" : String(styleValue);
      select.value = want;
      if (select.value !== want) select.value = "inherit";
      syncSelectTone(select);
    }
    return wrap;
  }

  function styleOptionsFrom(root) {
    const sample = root.querySelector("select[name='extra_gb_style'], select[name='extra_user_style']");
    return sample ? sample.innerHTML : "";
  }

  function seedDefaultsIfEmpty(root) {
    if (!root) return;
    ["gb", "users"].forEach((kind) => {
      const box = root.querySelector(`[data-extra-packs-rows="${kind}"]`);
      if (!box || box.querySelector("[data-extra-pack-row]")) return;
      const key = kind === "gb" ? "defaultGb" : "defaultUsers";
      const attr = kind === "gb" ? "data-default-gb" : "data-default-users";
      const amounts = parseCsvInts(root.getAttribute(attr), DEFAULTS[kind]);
      const html = styleOptionsFrom(root);
      amounts.forEach((n) => box.appendChild(buildRow(kind, n, "inherit", html || '<option value="inherit">ارث از پیش‌فرض</option>')));
    });
  }

  function bind(root) {
    if (!root || root.dataset.bound === "1") return;
    root.dataset.bound = "1";
    root.addEventListener("click", (event) => {
      const add = event.target.closest("[data-extra-pack-add]");
      if (add && root.contains(add)) {
        const kind = add.getAttribute("data-extra-pack-add");
        const box = root.querySelector(`[data-extra-packs-rows="${kind}"]`);
        if (!box) return;
        const html = styleOptionsFrom(root);
        box.appendChild(buildRow(kind, 1, "inherit", html));
        return;
      }
      const remove = event.target.closest("[data-extra-pack-remove]");
      if (remove && root.contains(remove)) {
        const row = remove.closest("[data-extra-pack-row]");
        if (row) row.remove();
      }
    });
    root.addEventListener("change", (event) => {
      if (event.target && event.target.matches("select.btn-color-select")) {
        syncSelectTone(event.target);
      }
    });
  }

  function mount(selector) {
    document.querySelectorAll(selector || "[data-extra-packs-root]").forEach((root) => bind(root));
  }

  const api = {
    DEFAULTS,
    parseCsvInts,
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
