/* Plan-scoped customer volume/duration stairs on the fixed user-plan form. */
(function (root) {
  "use strict";

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

  function syncSelectTone(select) {
    if (!select) return;
    const opt = select.selectedOptions && select.selectedOptions[0];
    const tone = (opt && opt.getAttribute("data-tone")) || "default";
    select.setAttribute("data-tone", tone);
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
    const sample = root.querySelector(
      "select[name='plan_vol_style'], select[name='plan_dur_style']"
    );
    return sample ? sample.innerHTML : '<option value="inherit">ارث از پیش‌فرض</option>';
  }

  function buildRow(kind, amount, styleValue, styleOptionsHtml) {
    const isVol = kind === "volume";
    const amountName = isVol ? "plan_vol_amount" : "plan_dur_amount";
    const styleName = isVol ? "plan_vol_style" : "plan_dur_style";
    const amountMax = isVol ? "100000" : "120";
    const amountLabel = isVol ? "مقدار حجم بسته" : "مقدار ماه بسته";
    const styleLabel = isVol ? "رنگ دکمه بسته حجم" : "رنگ دکمه بسته زمان";
    const removeLabel = isVol ? "حذف بسته حجم" : "حذف بسته زمان";
    const wrap = document.createElement("div");
    wrap.className = "extra-pack-row";
    wrap.setAttribute("data-user-pack-row", "");
    wrap.innerHTML =
      `<input name="${amountName}" class="extra-pack-amount" type="number" min="1" max="${amountMax}" ` +
      `step="1" inputmode="numeric" dir="ltr" value="${Number(amount) || 1}" ` +
      `aria-label="${amountLabel}" placeholder="مقدار" required />` +
      `<select name="${styleName}" class="btn-color-select extra-pack-style" data-tone="default" aria-label="${styleLabel}">${styleOptionsHtml}</select>` +
      `<button type="button" class="btn btn-danger btn-sm extra-pack-remove" data-user-pack-remove aria-label="${removeLabel}">حذف</button>`;
    const select = wrap.querySelector("select");
    if (select) {
      const want = styleValue == null ? "inherit" : String(styleValue);
      select.value = want;
      if (select.value !== want) select.value = "inherit";
      syncSelectTone(select);
    }
    return wrap;
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
  }

  function bind(root) {
    if (!root || root.dataset.bound === "1") return;
    root.dataset.bound = "1";
    root.addEventListener("click", (event) => {
      const add = event.target.closest("[data-user-pack-add]");
      if (add && root.contains(add)) {
        const kind = add.getAttribute("data-user-pack-add");
        const box = root.querySelector(`[data-user-pack-rows="${kind}"]`);
        if (!box) return;
        box.appendChild(buildRow(kind, 1, "inherit", styleOptionsFrom(root)));
        return;
      }
      const remove = event.target.closest("[data-user-pack-remove]");
      if (remove && root.contains(remove)) {
        const row = remove.closest("[data-user-pack-row]");
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
    document.querySelectorAll(selector || "[data-user-plan-packs-root]").forEach((el) => bind(el));
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
    root.UserPlanAddonPacks = api;
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", () => mount());
    } else {
      mount();
    }
  }
})(typeof window !== "undefined" ? window : globalThis);
