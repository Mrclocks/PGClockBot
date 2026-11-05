/* Plan-scoped customer volume/duration pack rows on the user plan form. */
(function (root) {
  "use strict";

  function syncSelectTone(select) {
    if (!select) return;
    const opt = select.selectedOptions && select.selectedOptions[0];
    const tone = (opt && opt.getAttribute("data-tone")) || "default";
    select.setAttribute("data-tone", tone);
  }

  function styleOptionsFrom(root) {
    const sample = root.querySelector(
      "select[name='plan_vol_style'], select[name='plan_dur_style']"
    );
    return sample ? sample.innerHTML : '<option value="inherit">ارث از پیش‌فرض</option>';
  }

  function buildRow(kind, amount, price, styleValue, styleOptionsHtml) {
    const isVol = kind === "volume";
    const amountName = isVol ? "plan_vol_amount" : "plan_dur_amount";
    const priceName = isVol ? "plan_vol_price" : "plan_dur_price";
    const styleName = isVol ? "plan_vol_style" : "plan_dur_style";
    const amountStep = isVol ? "0.1" : "1";
    const amountMin = isVol ? "0.1" : "1";
    const amountMax = isVol ? "100000" : "3650";
    const amountMode = isVol ? "decimal" : "numeric";
    const amountPh = isVol ? "گیگ" : "روز";
    const amountLabel = isVol ? "مقدار حجم بسته" : "مقدار زمان بسته";
    const priceLabel = isVol ? "قیمت بسته حجم" : "قیمت بسته زمان";
    const styleLabel = isVol ? "رنگ دکمه بسته حجم" : "رنگ دکمه بسته زمان";
    const removeLabel = isVol ? "حذف بسته حجم" : "حذف بسته زمان";
    const wrap = document.createElement("div");
    wrap.className = "extra-pack-row";
    wrap.setAttribute("data-user-pack-row", "");
    wrap.innerHTML =
      `<input name="${amountName}" class="extra-pack-amount" type="number" min="${amountMin}" max="${amountMax}" ` +
      `step="${amountStep}" inputmode="${amountMode}" dir="ltr" value="${amount}" ` +
      `aria-label="${amountLabel}" placeholder="${amountPh}" required />` +
      `<input name="${priceName}" class="extra-pack-price" type="number" data-money min="0" step="1" ` +
      `inputmode="numeric" dir="ltr" value="${Number(price) || 0}" aria-label="${priceLabel}" placeholder="قیمت" required />` +
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

  function bind(root) {
    if (!root || root.dataset.bound === "1") return;
    root.dataset.bound = "1";
    root.addEventListener("click", (event) => {
      const add = event.target.closest("[data-user-pack-add]");
      if (add && root.contains(add)) {
        const kind = add.getAttribute("data-user-pack-add");
        const box = root.querySelector(`[data-user-pack-rows="${kind}"]`);
        if (!box) return;
        const html = styleOptionsFrom(root);
        const amount = kind === "volume" ? 1 : 7;
        box.appendChild(buildRow(kind, amount, 0, "inherit", html));
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

  const api = { bind, mount, buildRow };

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
