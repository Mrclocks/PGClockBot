/* Mini App views as escaped HTML strings. Pure functions: no DOM, no network. */
(function (root) {
  "use strict";

  const core = root.PGMini.core;
  const { esc, num, money, faDigits } = core;

  const STROKE = 'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"';
  const ICON_PATHS = {
    home: '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
    svc: '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
    shop: '<path d="M5 8h14l-1 12H6z"/><path d="M9 8V6a3 3 0 0 1 6 0v2"/>',
    account: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    store: '<path d="M4 10v10h16V10"/><path d="M3 10 5 4h14l2 6z"/>',
    ops: '<circle cx="12" cy="12" r="3"/><path d="M12 3v2M12 19v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M3 12h2M19 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    refresh: '<path d="M20 11a8 8 0 0 0-14.5-4.5L4 8"/><path d="M4 4v4h4"/><path d="M4 13a8 8 0 0 0 14.5 4.5L20 16"/><path d="M20 20v-4h-4"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    timer: '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l2 2M9 2h6"/>',
    auto: '<path d="M20 11a8 8 0 0 0-14.5-4.5L4 8"/><path d="M4 4v4h4"/><path d="M12 8v4l2 2"/>',
    cancel: '<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6M15 9l-6 6"/>',
    chevron: '<path d="m15 6-6 6 6 6"/>',
    // RTL: "back" points right.
    back: '<path d="m9 6 6 6-6 6"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
    chat: '<path d="M4 5h16v11H9l-5 4z"/><path d="M8 9h8M8 12h5"/>',
    gift: '<path d="M4 11h16v9H4zM3 7h18v4H3zM12 7v13"/><path d="M12 7c-1.5-3-5-3-5-1s3 1 5 1c2 0 5 1 5-1s-3.5-2-5 1"/>',
    wallet: '<rect x="3" y="6" width="18" height="14" rx="3"/><path d="M3 10h18"/><path d="M16.5 15h.01"/>',
    book: '<path d="M3 5h6a3 3 0 0 1 3 3v12a2 2 0 0 0-2-2H3z"/><path d="M21 5h-6a3 3 0 0 0-3 3v12a2 2 0 0 1 2-2h7z"/>',
    headset: '<path d="M4 13a8 8 0 0 1 16 0"/><path d="M4 13h3v6H5a1 1 0 0 1-1-1z"/><path d="M20 13h-3v6h2a1 1 0 0 0 1-1z"/>',
    users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14a6 6 0 0 1 3.5 6"/>',
    chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    receipt: '<path d="M6 3h12v18l-3-2-3 2-3-2-3 2z"/><path d="M9 8h6M9 12h6"/>',
    megaphone: '<path d="M3 11v2a1 1 0 0 0 1 1h3l7 4V6L7 10H4a1 1 0 0 0-1 1z"/><path d="M18 9a4 4 0 0 1 0 6"/>',
    tag: '<path d="M3 12V4h8l10 10-8 8z"/><path d="M7.5 8.5h.01"/>',
    link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    external: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  };

  // Shortcut tile colours; each is a CSS class with a light and a dark variant.
  const TONES = new Set(["brand", "green", "blue", "purple", "teal", "pink", "amber", "sky", "red"]);

  function icon(name, size) {
    const s = Number(size) || 20;
    return `<svg aria-hidden="true" focusable="false" width="${s}" height="${s}" viewBox="0 0 24 24" ${STROKE}>${ICON_PATHS[name] || ICON_PATHS.home}</svg>`;
  }

  function isOnHoldStatus(status) {
    const s = String(status || "").toLowerCase().replace(/-/g, "_");
    return s === "on_hold" || s === "onhold";
  }

  function statusTone(status) {
    const s = String(status || "").toLowerCase();
    if (s === "active") return "ok";
    if (s === "limited" || s === "disabled" || isOnHoldStatus(s)) return "warn";
    if (s === "expired") return "danger";
    return "";
  }

  function badge(text, tone) {
    return `<span class="badge${tone ? " is-" + esc(tone) : ""}">${esc(text)}</span>`;
  }

  function meterTone(pct) {
    if (pct >= 90) return "danger";
    if (pct >= 75) return "warn";
    return "";
  }

  /** 0–100 as a whole number, or null when the panel did not report usage. */
  function clampPct(pct) {
    if (pct == null || pct === "" || !Number.isFinite(Number(pct))) return null;
    return Math.max(0, Math.min(100, Math.round(Number(pct))));
  }

  const RING_RADIUS = 18;
  const RING_LENGTH = 2 * Math.PI * RING_RADIUS;

  /** Used traffic as a ring; the centre holds ``center`` (markup the caller already escaped) or the percentage. */
  function ringHtml(pct, { size, center } = {}) {
    const value = clampPct(pct);
    const px = Number(size) || 52;
    const tone = value != null && meterTone(value) ? "is-" + meterTone(value) : "";
    const used = value == null ? 0 : (RING_LENGTH * value) / 100;
    const label = value == null ? "مصرف نامشخص" : `${num(value)}٪ حجم مصرف شده`;
    const inner = center != null ? center : value == null ? "—" : esc(num(value) + "٪");
    return `<span class="ring ${tone}" style="width:${px}px;height:${px}px" role="img" aria-label="${esc(label)}">
      <svg aria-hidden="true" focusable="false" viewBox="0 0 44 44" width="${px}" height="${px}"><circle class="ring-track" cx="22" cy="22" r="${RING_RADIUS}"/>${used > 0 ? `<circle class="ring-value" cx="22" cy="22" r="${RING_RADIUS}" stroke-dasharray="${used.toFixed(1)} ${RING_LENGTH.toFixed(1)}"/>` : ""}</svg>
      <span class="ring-center">${inner}</span>
    </span>`;
  }

  /* ---------- brand, notice and shortcuts ---------- */

  const LOGO_PATH = /^\/media\/uploads\/[A-Za-z0-9_-]{1,96}\.jpg$/;

  function brandInitial(name) {
    const first = Array.from(String(name || "").trim())[0];
    return first || "؟";
  }

  /** Shop logo: the bot's profile photo when it loads, else the name's first letter. */
  function brandLogoHtml(brand) {
    const b = brand || {};
    const logo = LOGO_PATH.test(String(b.logo_url || "")) ? b.logo_url : "";
    return `<span class="brand-logo" aria-hidden="true"><span class="brand-initial">${esc(brandInitial(b.name))}</span>${logo ? `<img src="${esc(logo)}" alt="" data-brand-logo="1">` : ""}</span>`;
  }

  /** Top of every home screen: logo, shop name and caption, then the greeting and role. */
  function brandHeaderHtml(brand, { greeting, role } = {}) {
    const b = brand || {};
    return `<header class="brand-head">
      <div class="brand-id">${brandLogoHtml(b)}
        <div class="brand-text"><strong>${esc(b.name || "مینی‌اپ")}</strong>${b.tagline ? `<span class="muted sm">${esc(b.tagline)}</span>` : ""}</div>
      </div>
      ${role ? `<span class="role-chip">${esc(role)}</span>` : ""}
    </header>
    ${greeting ? `<p class="greeting"><span class="muted sm">خوش آمدید</span><strong>${esc(greeting)}</strong></p>` : ""}`;
  }

  function noticeBannerHtml(text) {
    const s = String(text || "").trim();
    if (!s) return "";
    return `<section class="promo" aria-label="اطلاعیهٔ فروشگاه">
      <span class="promo-icon">${icon("megaphone", 22)}</span>
      <span class="promo-body"><span class="sm">اطلاعیهٔ فروشگاه</span><strong>${esc(s)}</strong></span>
    </section>`;
  }

  /** One shortcut tile; ``item.attrs`` are extra data-* pairs for the click handler. */
  const BADGE_MAX = 99;

  /** A count on a tile («۳», or «۹۹+»); nothing for zero or a bad value. */
  function badgeCountHtml(count) {
    const n = Math.floor(Number(count));
    if (!Number.isFinite(n) || n <= 0) return "";
    const text = n > BADGE_MAX ? num(BADGE_MAX) + "+" : num(n);
    return `<span class="shortcut-badge" aria-hidden="true">${esc(text)}</span><span class="visually-hidden">، ${esc(text)} مورد</span>`;
  }

  function shortcutHtml(item) {
    const tone = TONES.has(item.tone) ? item.tone : "brand";
    const attrs = Object.entries(item.attrs || {})
      .map(([key, value]) => ` data-${esc(key)}="${esc(value)}"`).join("");
    return `<button type="button" class="shortcut" data-action="${esc(item.action)}"${attrs}>
      <span class="shortcut-icon tone-${tone}">${icon(item.icon, 24)}${badgeCountHtml(item.badge)}</span>
      <span class="shortcut-label">${esc(item.label)}</span>
    </button>`;
  }

  function shortcutsHtml(items, title) {
    const list = (items || []).filter(Boolean);
    if (!list.length) return "";
    return `<section class="shortcuts-card" aria-label="${esc(title || "دسترسی سریع")}">
      <h2 class="section-title">${esc(title || "دسترسی سریع")}</h2>
      <div class="shortcut-grid">${list.map(shortcutHtml).join("")}</div>
    </section>`;
  }

  /** Remaining-time label — never map on_hold + null days to «نامحدود». */
  function expireDaysLabel(s) {
    if (s && s.expire_days_label) return faDigits(s.expire_days_label);
    if (s && s.expire_days != null) return num(s.expire_days) + " روز";
    if (s && (s.pending_start || isOnHoldStatus(s.status))) return "پس از اتصال";
    if (!s || s.error || !s.status || s.status === "—") return "—";
    return "نامحدود";
  }

  function serviceStatusBadge(s) {
    if (s.is_cancelled) return badge("لغوشده", "danger");
    if (s.cancellation_pending) return badge("در انتظار لغو", "warn");
    return badge(s.status_fa || s.status || "—", statusTone(s.status));
  }

  function serviceErrorHtml(s) {
    if (!s.error) return "";
    return `<p class="inline-error" role="status">${esc(s.error_message || "دریافت اطلاعات سرویس ناموفق بود؛ دوباره تلاش کنید.")}</p>`;
  }

  const DAYS_DANGER = 1;
  const DAYS_WARN = 3;

  /** Colour for the days left: red on the last day, amber within three days. */
  function daysTone(days) {
    if (days == null || days === "" || !Number.isFinite(Number(days))) return "";
    if (Number(days) <= DAYS_DANGER) return "danger";
    return Number(days) <= DAYS_WARN ? "warn" : "";
  }

  function daysHtml(text, days) {
    const tone = daysTone(days);
    return `<span class="days${tone ? " is-" + tone : ""}">${esc(text)}</span>`;
  }

  function serviceRowHtml(s) {
    const id = Number(s.id) || 0;
    return `<button type="button" class="svc-row" data-action="open-service" data-id="${id}">
      ${ringHtml(s.traffic_pct)}
      <span class="svc-row-body">
        <span class="svc-row-head"><strong dir="ltr">${esc(s.username || "—")}</strong>${serviceStatusBadge(s)}</span>
        <span class="svc-row-meta"><span>${esc(faDigits(s.traffic || "—"))}</span>${daysHtml(expireDaysLabel(s), s.expire_days)}</span>
        ${serviceErrorHtml(s)}
      </span>
    </button>`;
  }

  function glanceRowHtml(g) {
    const id = Number(g.id) || 0;
    const tone = g.is_cancelled ? "danger" : g.cancellation_pending ? "warn" : statusTone(g.status);
    return `<button type="button" class="svc-row is-glance" data-action="open-service" data-id="${id}">
      <span class="svc-avatar${tone ? " is-" + tone : ""}">${icon("svc", 22)}</span>
      <span class="svc-row-body">
        <span class="svc-row-head"><strong dir="ltr">${esc(g.username || "—")}</strong>${serviceStatusBadge(g)}</span>
        <span class="svc-row-meta">${daysHtml(faDigits(g.remaining_label || "—"), g.days_left)}${icon("chevron", 16)}</span>
      </span>
    </button>`;
  }

  function attentionCardHtml(g) {
    const id = Number(g.id) || 0;
    const ended = ["expired", "limited"].includes(String(g.status || "").toLowerCase());
    const title = ended ? "سرویس به پایان رسیده" : "سرویس رو به پایان است";
    return `<div class="notice is-warn" role="status">
      <span class="notice-icon">${icon("clock", 20)}</span>
      <div class="notice-body"><strong>${esc(title)}: <span dir="ltr">${esc(g.username || "—")}</span></strong>
        <span>${esc(faDigits(g.remaining_label || ""))}</span></div>
      <button type="button" class="btn sm is-warn" data-action="open-service" data-id="${id}" data-then="renew">تمدید</button>
    </div>`;
  }

  /** Expiry in the Persian calendar; the server's label only for cases without a date (on hold). */
  function expireDateText(s) {
    return core.formatDate(s.expire_at) || faDigits(s.expire || "—");
  }

  function serviceUsageHtml(s) {
    const fetched = s.info_fetched_at ? core.formatDateTime(s.info_fetched_at) : "";
    const pct = clampPct(s.traffic_pct);
    // What people check first is what is left; the line below shows the usage.
    const left = s.traffic_left && s.traffic_left !== "—" ? faDigits(s.traffic_left) : "";
    const center = left
      ? `<span class="muted sm">باقی‌مانده</span><strong class="usage-left">${esc(left)}</strong>`
      : `<span class="muted sm">مصرف</span><strong>${pct == null ? "—" : esc(num(pct) + "٪")}</strong>`;
    const usedLine = `مصرف: ${faDigits(s.traffic || "—")}${left && pct != null ? ` (${num(pct)}٪)` : ""}`;
    return `<section class="card usage-card" aria-label="مصرف سرویس">
      <div class="card-head">${serviceStatusBadge(s)}${fetched ? `<span class="muted sm">به‌روز: ${esc(fetched)}</span>` : ""}</div>
      <div class="usage-ring">${ringHtml(s.traffic_pct, { size: 168, center })}</div>
      <p class="usage-line">${esc(usedLine)}</p>
      <div class="tile-grid">
        <div class="tile"><span>زمان باقی‌مانده</span><strong>${daysHtml(expireDaysLabel(s), s.expire_days)}</strong></div>
        <div class="tile"><span>انقضا</span><strong>${esc(expireDateText(s))}</strong></div>
        <div class="tile"><span>آخرین اتصال</span><strong title="${esc(core.formatDateTime(s.online_at))}">${esc(core.formatRelative(s.online_at) || "—")}</strong></div>
      </div>
      ${serviceErrorHtml(s)}
    </section>`;
  }

  /** Host only: the full link is a credential and must not sit on screen (screenshots). */
  function maskedUrl(url) {
    try {
      return new URL(url).host + "/•••";
    } catch (_) {
      return "•••";
    }
  }

  function serviceLinkHtml(s, features) {
    const id = Number(s.id) || 0;
    const url = core.safeUrl(s.subscription_url);
    if (!url) return "";
    return `<section class="card" aria-label="لینک اشتراک">
      <div class="card-head"><h2 class="card-title">لینک اشتراک</h2>
        <button type="button" class="link-btn" data-action="guides" data-id="${id}">راهنمای اتصال</button></div>
      <div class="link-box" dir="ltr">${icon("link", 16)}<span>${esc(maskedUrl(url))}</span></div>
      <div class="btn-row">
        <button type="button" class="btn" data-action="copy-link" data-id="${id}">کپی لینک</button>
        ${features.qr ? `<button type="button" class="btn ghost" data-action="show-qr" data-id="${id}">QR کد</button>` : ""}
        <button type="button" class="btn ghost" data-action="open-link" data-id="${id}">باز کردن</button>
      </div>
    </section>`;
  }

  function actionRow(action, id, iconName, label, extra) {
    const x = extra || {};
    const tone = x.danger ? "red" : TONES.has(x.tone) ? x.tone : "brand";
    return `<button type="button" class="list-row${x.danger ? " is-danger" : ""}" data-action="${esc(action)}" data-id="${id}"${x.kind ? ` data-kind="${esc(x.kind)}"` : ""}>
      <span class="row-icon tone-${tone}">${icon(iconName)}</span><span class="list-row-label">${esc(label)}</span>${icon("chevron", 16)}
    </button>`;
  }

  function serviceActionsHtml(s, features) {
    const id = Number(s.id) || 0;
    const open = !s.is_cancelled && !s.cancellation_pending;
    const rows = [];
    if (open && features.walletPay) rows.push(actionRow("renew", id, "refresh", "تمدید سرویس"));
    if (open && features.walletPay && s.addons_allowed) {
      rows.push(actionRow("addons", id, "plus", "افزایش حجم", { kind: "volume", tone: "blue" }));
      rows.push(actionRow("addons", id, "timer", "افزایش زمان", { kind: "duration", tone: "purple" }));
    }
    if (features.manage !== false) {
      if (open) rows.push(actionRow("automation", id, "auto", "تمدید خودکار", { tone: "teal" }));
      rows.push(actionRow("cancellation", id, "cancel", s.is_cancelled || s.cancellation_pending ? "وضعیت درخواست لغو" : "درخواست لغو سرویس", { danger: true }));
    }
    return rows.length ? `<section class="card list-card" aria-label="عملیات سرویس"><h2 class="card-title">مدیریت سرویس</h2>${rows.join("")}</section>` : "";
  }

  function serviceDetailHtml(s, features) {
    const id = Number(s.id) || 0;
    return `<header class="view-head">
        <div><span class="muted sm">جزئیات سرویس</span><h1 dir="ltr">${esc(s.username || "—")}</h1></div>
        <button type="button" class="icon-btn" data-action="refresh-service" data-id="${id}" aria-label="به‌روزرسانی اطلاعات از پنل">${icon("refresh")}</button>
      </header>
      ${serviceUsageHtml(s)}
      ${serviceLinkHtml(s, features)}
      ${serviceActionsHtml(s, features)}`;
  }

  function priceHtml(quote) {
    return root.PGClockCatalogPrice.renderPrice(quote, money, esc);
  }

  function planMetaText(p) {
    const days = p.days ? `${num(p.days)} روز` : "نامحدود";
    const gb = p.gb == null || p.gb === "" ? "حجم نامحدود" : `${num(p.gb)} گیگ`;
    return `${days}، ${gb}`;
  }

  function planOptionsHtml(plans, selectedId) {
    if (!plans || !plans.length) return emptyHtml("پلنی برای خرید فعال نیست.");
    return `<div class="plan-list" role="radiogroup" aria-label="پلن‌ها">${plans.map((p) => {
      const id = Number(p.id) || 0;
      const checked = id === Number(selectedId);
      return `<label class="plan-option${checked ? " is-selected" : ""}">
        <input type="radio" name="plan" value="${id}" data-action="select-plan" data-id="${id}"${checked ? " checked" : ""}>
        <span class="plan-meta"><strong>${esc(p.name)}</strong>${p.is_trial ? badge("تست", "ok") : ""}<span class="muted sm">${esc(planMetaText(p))}</span></span>
        <span class="plan-price">${priceHtml(p)}</span>
      </label>`;
    }).join("")}</div>`;
  }

  function lowBalanceHtml(features) {
    return `<p class="inline-error">موجودی کیف پول کافی نیست.</p>${(features || {}).topup === false ? ""
      : '<button type="button" class="btn ghost block" data-action="topup">شارژ کیف پول</button>'}`;
  }

  /** ``features`` (from the app) hides the code field and top-up button where a shop has not opened them. */
  function checkoutHtml(plan, wallet, features) {
    if (!plan) return `<section class="card"><p class="muted">یک پلن را انتخاب کنید.</p></section>`;
    const f = features || {};
    const price = Number(plan.price) || 0;
    const enough = price <= (Number(wallet) || 0);
    return `<section class="card" aria-label="خلاصهٔ سفارش">
      <div class="sum-row"><span class="muted">پلن</span><span>${esc(plan.name)}</span></div>
      <div class="sum-row"><span class="muted">موجودی کیف پول</span><span>${esc(money(wallet))}</span></div>
      <div class="sum-row is-total"><span>قابل پرداخت</span><span>${priceHtml(plan)}</span></div>
      ${plan.is_trial || f.gift === false ? "" : `<form class="code-form" data-form="discount">
        <label class="form-field"><span class="muted sm">کد تخفیف (اختیاری)</span>
          <span class="code-line"><input type="text" name="code" maxlength="64" autocomplete="off" dir="ltr" autocapitalize="characters">
          <button type="submit" class="btn ghost">اعمال</button></span></label>
      </form>`}
      ${enough ? "" : lowBalanceHtml(f)}
      <button type="button" class="btn block" data-action="buy" data-id="${Number(plan.id) || 0}"${enough ? "" : " disabled"}>${plan.is_trial ? "دریافت سرویس تست" : "پرداخت از کیف پول"}</button>
    </section>`;
  }

  function discountedCheckoutHtml(checkout, plan, wallet) {
    const enough = Number(checkout.amount) <= (Number(wallet) || 0);
    return `<section class="card" aria-label="خلاصهٔ سفارش با تخفیف">
      <div class="sum-row"><span class="muted">پلن</span><span>${esc(plan ? plan.name : "")}</span></div>
      <div class="sum-row"><span class="muted">قیمت</span><s>${esc(money(checkout.original_amount))}</s></div>
      <div class="sum-row"><span class="muted">تخفیف <bdi dir="ltr">${esc(checkout.code)}</bdi></span><span class="is-plus tx-amt"><span aria-hidden="true">−</span><bdi>${esc(money(checkout.discount))}</bdi></span></div>
      <div class="sum-row is-total"><span>قابل پرداخت</span><span>${esc(money(checkout.amount))}</span></div>
      ${enough ? "" : lowBalanceHtml()}
      <div class="btn-col">
        <button type="button" class="btn block" data-action="checkout-pay"${enough ? "" : " disabled"}>پرداخت از کیف پول</button>
        <button type="button" class="btn ghost block" data-action="checkout-cancel">حذف کد و انصراف</button>
      </div>
    </section>`;
  }

  function giftFormHtml() {
    return `<form class="code-form" data-form="gift">
      <label class="form-field"><span>کد هدیه</span>
        <input type="text" name="code" maxlength="64" autocomplete="off" dir="ltr" autocapitalize="characters"></label>
      <button type="submit" class="btn block">ثبت کد و شارژ کیف پول</button>
    </form>`;
  }

  function renewPlansHtml(plans, serviceId) {
    const list = (plans || []).filter((p) => !p.is_trial);
    if (!list.length) return emptyHtml("پلنی برای تمدید فعال نیست.");
    return `<p class="muted sm">پلن تمدید را انتخاب کنید؛ پیش از پرداخت، نتیجه را می‌بینید.</p>
      <div class="plan-list">${list.map((p) => `<button type="button" class="plan-option" data-action="preview-renew" data-id="${Number(serviceId) || 0}" data-plan="${Number(p.id) || 0}">
        <span class="plan-meta"><strong>${esc(p.name)}</strong><span class="muted sm">${esc(planMetaText(p))}</span></span>
        <span class="plan-price">${priceHtml(p)}</span>
      </button>`).join("")}</div>`;
  }

  function renewPreviewHtml(preview, serviceId, planId) {
    const sid = Number(serviceId) || 0;
    const pid = Number(planId) || 0;
    return `<div class="sum-row"><span class="muted">پلن</span><strong>${esc(preview.plan_name)}</strong></div>
      <div class="sum-row is-total"><span>مبلغ</span><span>${priceHtml(preview)}</span></div>
      ${(preview.lines || []).map((line) => `<p class="sm">${esc(line)}</p>`).join("")}
      ${(preview.warnings || []).map((line) => `<p class="inline-error">${esc(line)}</p>`).join("")}
      ${preview.notice ? `<p class="muted sm">${esc(preview.notice)}</p>` : ""}
      <div class="btn-col">
        <button type="button" class="btn block" data-action="confirm-renew" data-id="${sid}" data-plan="${pid}">تأیید و پرداخت از کیف پول</button>
        <button type="button" class="btn ghost block" data-action="renew" data-id="${sid}">انتخاب پلن دیگر</button>
      </div>`;
  }

  function addonPacksHtml(packs, serviceId) {
    if (!packs || !packs.length) return emptyHtml("بسته‌ای برای خرید فعال نیست.");
    const sid = Number(serviceId) || 0;
    return `<p class="muted sm">بسته پس از پرداخت به همین سرویس اضافه می‌شود.</p>
      <div class="plan-list">${packs.map((p) => `<button type="button" class="plan-option" data-action="buy-addon" data-id="${sid}" data-pack="${Number(p.id) || 0}">
        <span class="plan-meta"><strong>${esc(p.name)}</strong><span class="muted sm">+${esc(p.amount_label)}</span>${p.description ? `<span class="muted sm">${esc(p.description)}</span>` : ""}</span>
        <span class="plan-price">${priceHtml(p)}</span>
      </button>`).join("")}</div>`;
  }

  function automationHtml(data, serviceId) {
    const sid = Number(serviceId) || 0;
    const actions = (data && data.actions) || [];
    return `<p class="muted sm">با روشن کردن هر گزینه، خرید تکرارشونده با قیمت فعلی از کیف پول انجام می‌شود. بستهٔ زمان یا حجم برای مورد تمام‌شده اولویت دارد؛ در غیر این صورت تمدید کامل انجام می‌شود.</p>
      ${data && data.needs_review ? `<p class="inline-error">سفارش #${num(data.pending_order_id)} نیاز به بررسی پشتیبانی دارد؛ اجرای خودکار متوقف است.</p>` : ""}
      ${actions.map((a, index) => `<form class="auto-form" data-form="automation" data-id="${sid}" data-auto-action="${esc(a.action)}">
        <label class="check-row"><input type="checkbox" name="enabled"${a.enabled ? " checked" : ""}><span>${esc(a.label)}</span></label>
        <label class="form-field"><span class="visually-hidden">انتخاب پلن یا بسته برای ${esc(a.label)}</span>
          <select name="choice_id" id="auto-choice-${index}">
            <option value="">انتخاب پلن یا بسته</option>
            ${(a.choices || []).map((c) => `<option value="${Number(c.id) || 0}"${Number(c.id) === Number(a.choice_id) ? " selected" : ""}>${esc(c.name)} — ${esc(money(c.price))}</option>`).join("")}
          </select></label>
        ${a.unavailable ? '<p class="inline-error">گزینهٔ قبلی حذف یا غیرفعال شده؛ پلن یا بستهٔ جدید انتخاب کنید.</p>' : ""}
        <button type="submit" class="btn sm">ذخیره</button>
      </form>`).join("")}`;
  }

  function cancellationHtml(row, serviceId) {
    const sid = Number(serviceId) || 0;
    const canRequest = !row || ["rejected", "withdrawn"].includes(row.status);
    return `<p class="muted sm">مبلغ برگشتی را اپراتور تعیین می‌کند و پس از تأیید غیرفعال‌شدن سرویس به کیف پول برمی‌گردد. ثبت درخواست، سرویس را غیرفعال نمی‌کند.</p>
      ${row ? `<div class="card inset"><strong>${esc(row.label)}</strong>
        ${row.refund_amount == null ? "" : `<p class="sm">اعتبار برگشتی: ${esc(money(row.refund_amount))}</p>`}
        ${row.operator_note ? `<p class="sm pre-line">${esc(row.operator_note)}</p>` : ""}</div>` : ""}
      ${canRequest ? `<form data-form="cancellation" data-id="${sid}">
        <label class="form-field"><span>دلیل لغو</span><textarea name="reason" rows="3" maxlength="1000" required></textarea></label>
        <button type="submit" class="btn block is-danger">ثبت درخواست لغو</button>
      </form>` : ""}`;
  }

  function qrHtml(pngBase64) {
    const data = String(pngBase64 || "");
    if (!/^[A-Za-z0-9+/]+=*$/.test(data)) return errorHtml("ساخت QR ناموفق بود.");
    return `<div class="qr-frame"><img alt="QR کد لینک اشتراک" src="data:image/png;base64,${data}"></div>
      <p class="muted sm center">برای افزودن اشتراک، این کد را در برنامهٔ اتصال اسکن کنید.</p>`;
  }

  function activityHtml(rows) {
    if (!rows || !rows.length) return emptyHtml("هنوز تراکنشی ندارید.");
    return rows.map((a) => {
      const amount = Number(a.amount) || 0;
      const sign = amount > 0 ? "+" : amount < 0 ? "−" : "";
      const kind = amount < 0 ? "برداشت" : "واریز";
      return `<div class="tx-row">
        <span class="tx-meta"><span>${esc(a.reason || "—")}</span><span class="muted sm">${esc(core.formatDateTime(a.created_at))}</span></span>
        <span class="tx-amt${amount >= 0 ? " is-plus" : " is-minus"}"><span class="visually-hidden">${kind}</span><span aria-hidden="true">${sign}</span><bdi>${esc(money(Math.abs(amount)))}</bdi></span>
      </div>`;
    }).join("");
  }

  /** ``features`` (from the app) hides the buttons of sections a shop's Mini App has not opened. */
  function walletCardHtml(customer, { actions, features } = {}) {
    const c = customer || {};
    const f = features || {};
    const buttons = [
      f.topup !== false ? `<button type="button" class="btn on-hero" data-action="topup">${icon("plus", 18)}شارژ کیف پول</button>` : "",
      actions
        ? (f.shop !== false ? `<button type="button" class="btn hero-ghost" data-action="goto" data-view="shop">${icon("shop", 18)}خرید سرویس</button>` : "")
        : (f.gift !== false ? `<button type="button" class="btn hero-ghost" data-action="gift-code">${icon("gift", 18)}کد هدیه</button>` : ""),
    ].join("");
    return heroHtml({
      label: "موجودی کیف پول",
      chip: c.wallet_pay_enabled ? "پرداخت فعال" : "فقط مشاهده",
      value: money(c.wallet),
      footer: buttons ? `<div class="btn-row">${buttons}</div>` : "",
      aria: "کیف پول",
    });
  }

  /** The brand-coloured card at the top of a home screen. ``footer`` is trusted markup. */
  function heroHtml({ label, chip, value, sub, footer, aria }) {
    return `<section class="hero-card" aria-label="${esc(aria || label)}">
      <span class="hero-deco" aria-hidden="true"></span>
      <div class="hero-head"><span>${esc(label)}</span>${chip ? `<span class="hero-chip">${esc(chip)}</span>` : ""}</div>
      <p class="hero-value"><bdi>${esc(value)}</bdi></p>
      ${sub ? `<p class="hero-sub">${esc(sub)}</p>` : ""}
      ${footer || ""}
    </section>`;
  }

  const TOPUP_STATUS_TONES = { approved: "ok", rejected: "danger" };

  function topupFormHtml(options) {
    const methods = (options && options.methods) || [];
    if (!methods.length) {
      return emptyHtml(options && options.bot_only
        ? "شارژ کیف پول فعلاً فقط از داخل ربات ممکن است."
        : "روش شارژی فعال نیست.");
    }
    const presets = options.presets || [];
    return `<form class="topup-form" data-form="topup">
      <fieldset class="field-group"><legend>مبلغ شارژ</legend>
        <div class="chip-grid">${presets.map((n, i) => `<label class="chip"><input type="radio" name="preset" value="${Number(n) || 0}"${i === 0 ? " checked" : ""}><span><bdi>${esc(money(n))}</bdi></span></label>`).join("")}
          <label class="chip"><input type="radio" name="preset" value="custom"${presets.length ? "" : " checked"}><span>مبلغ دلخواه</span></label>
        </div>
        <label class="form-field"><span class="muted sm">مبلغ دلخواه (<bdi>${esc(money(options.min_amount))}</bdi> تا <bdi>${esc(money(options.max_amount))}</bdi>)</span>
          <input type="text" name="custom_amount" inputmode="numeric" autocomplete="off" dir="ltr" maxlength="16"></label>
      </fieldset>
      <fieldset class="field-group"><legend>روش پرداخت</legend>
        ${methods.map((m, i) => `<label class="method-option">
          <input type="radio" name="method" value="${esc(m.key)}"${i === 0 ? " checked" : ""}>
          <span class="plan-meta"><strong>${esc(m.label)}</strong>${m.needs_receipt ? '<span class="muted sm">پس از واریز، تصویر رسید را بفرستید</span>' : '<span class="muted sm">پرداخت آنلاین و تأیید خودکار</span>'}</span>
          ${(m.choices || []).length ? `<select name="dest_${esc(m.key)}" aria-label="مقصد ${esc(m.label)}">${m.choices.map((c) => `<option value="${esc(c.id)}">${esc(c.label)}</option>`).join("")}</select>` : ""}
        </label>`).join("")}
      </fieldset>
      ${options.bot_only ? '<p class="muted sm">روش «بزن به کارت» فقط از داخل ربات در دسترس است.</p>' : ""}
      <button type="submit" class="btn block">ادامه</button>
    </form>`;
  }

  function copyRow(label, value) {
    return `<div class="copy-row"><span class="muted sm">${esc(label)}</span>
      <div class="copy-line"><strong dir="ltr">${esc(value)}</strong>
      <button type="button" class="btn ghost sm" data-action="copy-value" data-value="${esc(value)}">کپی</button></div></div>`;
  }

  function receiptFormHtml(paymentId) {
    const id = Number(paymentId) || 0;
    return `<form class="receipt-form" data-form="receipt" data-id="${id}">
      <label class="file-field"><span class="file-field-label">انتخاب تصویر رسید</span>
        <span class="muted sm">عکس JPG یا PNG، حداکثر ۸ مگابایت</span>
        <input type="file" name="receipt" class="visually-hidden" accept="image/jpeg,image/png,image/*" data-input="receipt-file"></label>
      <button type="submit" class="btn block">ارسال رسید</button>
      <p class="muted sm">تصویر رسید برای شما در ربات هم فرستاده می‌شود. می‌توانید رسید را مستقیم در ربات هم بفرستید.</p>
    </form>`;
  }

  function topupPayHtml(t) {
    const rows = [`<div class="sum-row is-total"><span>مبلغ</span><span>${esc(money(t.amount))}</span></div>`];
    if (t.method === "card") {
      rows.push(copyRow("شمارهٔ کارت", t.card_number || ""));
      if (t.card_holder) rows.push(`<p class="sm">به نام ${esc(t.card_holder)}</p>`);
    } else if (t.method === "gateway") {
      rows.push(`<p class="sm">پرداخت از طریق ${esc(t.gateway_name || "درگاه")}</p>`);
      if (t.gateway_link) rows.push(`<button type="button" class="btn block ghost" data-action="open-topup-link">ورود به ${esc(t.gateway_name || "درگاه")}</button>`);
    } else if (t.method === "crypto") {
      rows.push(`<p class="sm">${esc(t.crypto_asset || "")}${t.crypto_network ? "، شبکهٔ " + esc(t.crypto_network) : ""}</p>`);
      rows.push(copyRow("آدرس کیف پول", t.crypto_address || ""));
    } else if (t.method === "psp") {
      rows.push(t.checkout_url
        ? '<button type="button" class="btn block" data-action="open-topup-link">ورود به درگاه پرداخت</button>'
        : errorHtml("لینک درگاه آماده نشد؛ دوباره تلاش کنید."));
      if (t.settlement_id) rows.push(`<button type="button" class="btn block ghost" data-action="check-topup" data-id="${Number(t.settlement_id) || 0}">بررسی پرداخت</button>`);
      rows.push('<p class="muted sm">پس از پرداخت، کیف پول خودکار شارژ می‌شود؛ اگر برنگشتید «بررسی پرداخت» را بزنید.</p>');
    }
    if (t.note) rows.push(`<p class="muted sm pre-line">${esc(t.note)}</p>`);
    rows.push(`<p class="muted sm">شمارهٔ پرداخت: ${esc(num(t.payment_id))}</p>`);
    if (t.needs_receipt) rows.push(receiptFormHtml(t.payment_id));
    return rows.join("");
  }

  function recentTopupsHtml(rows) {
    if (!rows || !rows.length) return "";
    return `<section class="card list-card" aria-label="شارژهای اخیر"><h2 class="card-title">شارژهای اخیر</h2>
      ${rows.map((r) => `<div class="list-row">
        <span class="list-row-label"><bdi>${esc(money(r.amount))}</bdi><span class="muted sm">، ${esc(core.formatDateTime(r.created_at))}</span></span>
        ${r.waiting_receipt
          ? `<button type="button" class="btn sm" data-action="topup-receipt" data-id="${Number(r.id) || 0}">ارسال رسید</button>`
          : badge(r.status_fa, TOPUP_STATUS_TONES[r.status] || "warn")}
      </div>`).join("")}
    </section>`;
  }

  const TICKET_TONES = { open: "warn", answered: "ok", closed: "" };

  function supportHtml(data) {
    const contacts = (data && data.contacts) || [];
    const tickets = (data && data.tickets) || [];
    return `<section class="card" aria-label="پشتیبانی">
        <p class="pre-line">${esc(data.text || "")}</p>
        <button type="button" class="btn block" data-action="ticket-new">تیکت جدید</button>
        ${contacts.length ? `<div class="btn-col">${contacts.map((c, i) => `<button type="button" class="btn ghost block" data-action="open-contact" data-index="${i}">گفتگو با ${esc(c.title)}</button>`).join("")}</div>` : ""}
      </section>
      <section class="card list-card" aria-label="تیکت‌های من"><h2 class="card-title">تیکت‌های من</h2>
        ${tickets.length ? tickets.map((t) => `<button type="button" class="list-row" data-action="open-ticket" data-id="${Number(t.id) || 0}">
          <span class="list-row-label"><span>${esc(t.subject)}</span><span class="muted sm">، <bdi>#${esc(num(t.id))}</bdi></span></span>
          ${badge(t.status_fa, TICKET_TONES[t.status] || "")}
        </button>`).join("") : '<p class="muted sm">هنوز تیکتی ندارید.</p>'}
      </section>`;
  }

  function ticketNewFormHtml() {
    return `<form class="ticket-form" data-form="ticket-new">
      <label class="form-field"><span>موضوع</span><input type="text" name="subject" maxlength="120" autocomplete="off"></label>
      <label class="form-field"><span>متن پیام</span><textarea name="body" rows="5" maxlength="2000"></textarea></label>
      <button type="submit" class="btn block">ثبت تیکت</button>
    </form>`;
  }

  function ticketThreadHtml(t) {
    const id = Number(t.id) || 0;
    return `<header class="view-head">
        <div><span class="muted sm">تیکت <bdi>#${esc(num(id))}</bdi></span><h1>${esc(t.subject)}</h1></div>
        <button type="button" class="icon-btn" data-action="refresh-ticket" data-id="${id}" aria-label="به‌روزرسانی گفتگو">${icon("refresh")}</button>
      </header>
      <div class="card-head">${badge(t.status_fa, TICKET_TONES[t.status] || "")}</div>
      <ol class="thread" aria-label="پیام‌ها">${(t.messages || []).map((m) => `<li class="bubble${m.from_staff ? " is-staff" : " is-mine"}">
        <span class="bubble-who muted sm">${m.from_staff ? "پشتیبانی" : "شما"}، ${esc(core.formatDateTime(m.created_at))}</span>
        <p class="pre-line">${esc(m.body)}</p></li>`).join("")}</ol>
      ${t.is_closed
        ? '<p class="muted sm center">این تیکت بسته شده است؛ برای ادامه، تیکت تازه بسازید.</p>'
        : `<form class="ticket-form" data-form="ticket-reply" data-id="${id}">
          <label class="form-field"><span class="visually-hidden">پاسخ شما</span><textarea name="body" rows="3" maxlength="2000" placeholder="پاسخ خود را بنویسید"></textarea></label>
          <button type="submit" class="btn block">ارسال</button>
        </form>`}`;
  }

  /** ``keys``: [key, label, isMoney, icon, tone] per tile; money tiles span the row. */
  function statsHtml(stats, keys, { columns } = {}) {
    const s = stats || {};
    return `<div class="tile-grid${columns === 2 ? " cols-2" : ""}">${keys.map(([key, label, isMoney, iconName, tone]) =>
      `<div class="tile stat-tile${isMoney ? " is-wide" : ""}">
        ${iconName ? `<span class="row-icon tone-${TONES.has(tone) ? tone : "brand"}">${icon(iconName, 18)}</span>` : ""}
        <span class="stat-text"><strong>${esc(isMoney ? money(s[key]) : num(s[key]))}</strong><span>${esc(label)}</span></span>
      </div>`
    ).join("")}</div>`;
  }

  /**
   * Counts that need someone to act: an in-app screen when allowed, else the panel page when one exists.
   * ``items``: [statKey, label, panelLinkId, inAppView, icon, tone].
   */
  function attentionListHtml(stats, links, items, inAppViews) {
    const byId = new Map((links || []).map((l) => [l.id, l]));
    const inApp = inAppViews || new Set();
    const rows = items
      .filter(([key]) => Number((stats || {})[key]) > 0)
      .map(([key, label, linkId, view, iconName, tone]) => {
        const link = byId.get(linkId);
        const body = `<span class="row-icon tone-${TONES.has(tone) ? tone : "amber"}">${icon(iconName || "clock")}</span>
          <span class="list-row-label">${esc(label)}</span><span class="count">${esc(num(stats[key]))}</span>`;
        if (view && inApp.has(view)) {
          return `<button type="button" class="list-row" data-action="goto" data-view="${esc(view)}">${body}</button>`;
        }
        return link
          ? `<button type="button" class="list-row" data-action="open-panel" data-path="${esc(link.path)}">${body}</button>`
          : `<div class="list-row">${body}</div>`;
      });
    if (!rows.length) return `<p class="all-clear">${icon("auto", 18)}<span>کار معوقی نیست.</span></p>`;
    return rows.join("");
  }

  // Icon and colour of each web-panel shortcut the server lists (by link id).
  const PANEL_LINK_LOOKS = {
    dashboard: ["chart", "sky"], home: ["chart", "sky"], finance: ["wallet", "green"],
    users: ["users", "blue"], resellers: ["store", "purple"], tickets: ["headset", "pink"], shop: ["ops", "teal"],
  };

  /** In-app shortcuts (``leading``) first, then one tile per web-panel link from the server. */
  /** ``badges`` maps a panel link id to a count shown on its tile (open tickets, for one). */
  function panelShortcutsHtml(links, { title, leading, badges } = {}) {
    const counts = badges || {};
    const panel = (links || []).map((l) => {
      const [iconName, tone] = PANEL_LINK_LOOKS[l.id] || ["external", "brand"];
      return { action: "open-panel", attrs: { path: l.path }, icon: iconName, tone, label: l.label, badge: counts[l.id] };
    });
    const items = (leading || []).concat(panel);
    const missing = panel.length ? "" : `<section class="card"><p class="muted sm">آدرس پنل وب در دسترس نیست.</p></section>`;
    return shortcutsHtml(items, title || "میانبرها") + missing;
  }

  /** ``previous`` is the tab just left; it settles back down while the new one rises. */
  function navHtml(nav, active, previous) {
    return (nav || []).map((item) => {
      const current = item.id === active;
      const leaving = !current && previous && item.id === previous;
      const cls = `nav-btn${current ? " is-active" : ""}${leaving ? " is-leaving" : ""}`;
      return `<button type="button" class="${cls}" data-action="goto" data-view="${esc(item.id)}"${current ? ' aria-current="page"' : ""}><span class="nav-icon">${icon(item.icon, 22)}</span><span>${esc(item.label)}</span></button>`;
    }).join("");
  }

  /** Visible way back for screens below a tab (Telegram's own Back button is easy to miss). */
  function backBarHtml(label) {
    return `<button type="button" class="back-link" data-action="back">${icon("back", 18)}<span>بازگشت به ${esc(label)}</span></button>`;
  }

  function guideListHtml(guides, serviceId) {
    if (!guides || !guides.length) return emptyHtml("هنوز آموزشی تعریف نشده است.");
    const sid = Number(serviceId) || 0;
    return `<p class="muted sm">برنامهٔ دستگاه خود را انتخاب کنید.</p>
      <div class="plan-list">${guides.map((g) => `<button type="button" class="plan-option" data-action="open-guide" data-guide="${esc(g.id)}" data-id="${sid}">
        <span class="plan-meta"><strong>${esc(g.title)}</strong>${g.media_count ? `<span class="muted sm">${esc(num(g.media_count))} عکس یا ویدیو</span>` : ""}</span>${icon("chevron", 16)}
      </button>`).join("")}</div>`;
  }

  function guideHtml(guide, serviceId, canOpenSubscription) {
    const sid = Number(serviceId) || 0;
    return `<p class="pre-line">${esc(guide.body)}</p>
      <div class="btn-col">
        ${guide.opens_subscription && canOpenSubscription ? `<button type="button" class="btn block" data-action="open-link" data-id="${sid}">افزودن اشتراک به برنامه</button>` : ""}
        ${(guide.links || []).map((link, i) => `<button type="button" class="btn ghost block" data-action="open-guide-link" data-guide="${esc(guide.id)}" data-index="${i}">${esc(link.label)}</button>`).join("")}
        ${guide.media_count ? `<button type="button" class="btn ghost block" data-action="send-guide" data-guide="${esc(guide.id)}">ارسال آموزش کامل با عکس و ویدیو در ربات</button>` : ""}
        <button type="button" class="btn ghost block" data-action="guides" data-id="${sid}">${icon("back", 16)} همهٔ آموزش‌ها</button>
      </div>`;
  }

  /* ---------- club: points, invites, rewards ---------- */

  function pointsText(value) {
    return `${num(value)} امتیاز`;
  }

  function rewardValueText(r) {
    const value = Number(r.value) || 0;
    if (r.type === "traffic_gb") return `${num(value)} گیگ ترافیک`;
    if (r.type === "time_days") return `${num(value)} روز اعتبار`;
    if (r.type === "wallet_credit") return `${money(value)} اعتبار کیف پول`;
    if (r.type === "discount_percent") return `${num(value)}٪ تخفیف`;
    return r.type_label || "";
  }

  /** The reward's amount alone («۵ گیگ», «۵۰,۰۰۰ تومان»): its name already says what it is. */
  function rewardAmountText(r) {
    const value = Number(r.value) || 0;
    if (r.type === "traffic_gb") return `${num(value)} گیگ`;
    if (r.type === "time_days") return `${num(value)} روز`;
    if (r.type === "wallet_credit") return money(value);
    if (r.type === "discount_percent") return `${num(value)}٪`;
    return r.type_label || "";
  }

  function clubPointsHtml(club) {
    const tier = club.tier || {};
    const next = tier.points_to_next == null ? "" : `<p class="hero-sub">${esc(pointsText(tier.points_to_next))} تا سطح بعد</p>`;
    return heroHtml({
      label: "امتیاز شما",
      chip: tier.name || "",
      value: num(club.points),
      footer: `${next}<p class="hero-sub">امتیاز این ماه: <bdi>${esc(num(club.month_points))}</bdi></p>`,
    });
  }

  function clubReferralHtml(ref) {
    const r = ref || {};
    const link = core.safeUrl(r.link);
    return `<section class="card" aria-label="دعوت دوستان"><h2 class="card-title">دعوت دوستان</h2>
      <p class="muted sm">با لینک یا کد خودتان دوستانتان را دعوت کنید و امتیاز بگیرید.</p>
      ${link ? copyRow("لینک دعوت", link) : ""}
      ${r.code ? copyRow("کد دعوت", r.code) : ""}
      <div class="tile-grid cols-2">
        <div class="tile"><span>دعوت‌های موفق</span><strong>${esc(num(r.invited))}</strong></div>
        <div class="tile"><span>امتیاز از دعوت</span><strong>${esc(num(r.earned_points))}</strong></div>
      </div>
      ${link ? '<button type="button" class="btn block" data-action="share-invite">ارسال لینک برای دوستان</button>' : ""}
    </section>`;
  }

  function clubRewardsHtml(rewards) {
    const rows = rewards || [];
    return `<section class="card list-card" aria-label="جوایز"><h2 class="card-title">جوایز</h2>
      ${rows.length ? rows.map((r) => `<div class="list-row">
        <span class="list-row-label"><span>${esc(r.name)}</span>
          <span class="muted sm reward-desc"><bdi>${esc(rewardAmountText(r))}</bdi>، ${esc(pointsText(r.cost))}</span>
          ${r.description ? `<span class="muted sm pre-line reward-desc">${esc(r.description)}</span>` : ""}</span>
        <button type="button" class="btn sm${r.affordable ? "" : " ghost"}" data-action="redeem-reward" data-id="${Number(r.id) || 0}"${r.affordable ? "" : ' disabled aria-disabled="true"'}>${r.affordable ? "دریافت" : "امتیاز کافی نیست"}</button>
      </div>`).join("") : '<p class="muted sm">هنوز جایزه‌ای تعریف نشده است.</p>'}
    </section>`;
  }

  function discountLimitText(d) {
    const parts = [`${num(d.percent)}٪ تخفیف`];
    if (Number(d.min_purchase) > 0) parts.push(`خرید از ${money(d.min_purchase)}`);
    if (d.max_discount != null) parts.push(`حداکثر ${money(d.max_discount)}`);
    if (d.expires_at) parts.push(`تا ${core.formatDateTime(d.expires_at)}`);
    return parts.join("، ");
  }

  function clubDiscountsHtml(discounts) {
    const rows = discounts || [];
    if (!rows.length) return "";
    return `<section class="card" aria-label="کدهای تخفیف من"><h2 class="card-title">کدهای تخفیف من</h2>
      ${rows.map((d) => copyRow(discountLimitText(d), d.code)).join("")}
      <p class="muted sm">هنگام خرید سرویس، کد را در بخش «کد تخفیف» وارد کنید.</p>
    </section>`;
  }

  function pointsHistoryRowsHtml(items) {
    return (items || []).map((t) => {
      const amount = Number(t.amount) || 0;
      return `<div class="tx-row">
        <span class="tx-meta"><span>${esc(t.label || "—")}</span><span class="muted sm">${esc(core.formatDateTime(t.created_at))}</span></span>
        <span class="tx-amt${amount >= 0 ? " is-plus" : " is-minus"}"><span class="visually-hidden">${amount < 0 ? "مصرف" : "دریافت"}</span><span aria-hidden="true">${amount > 0 ? "+" : amount < 0 ? "−" : ""}</span><bdi>${esc(num(Math.abs(amount)))}</bdi></span>
      </div>`;
    }).join("");
  }

  function clubHistoryHtml(items, hasMore) {
    return `<section class="card" aria-label="تاریخچهٔ امتیاز"><h2 class="card-title">تاریخچهٔ امتیاز</h2>
      ${items && items.length ? pointsHistoryRowsHtml(items) : '<p class="muted sm">هنوز امتیازی نگرفته‌اید.</p>'}
      ${hasMore ? '<button type="button" class="btn ghost block" data-action="club-history-more">موارد بیشتر</button>' : ""}
    </section>`;
  }

  function clubHtml(club, history) {
    if (!club.enabled) {
      return clubReferralHtml(club.referral) + '<section class="card"><p class="muted">امتیاز و جوایز فعلاً غیرفعال است.</p></section>';
    }
    return clubPointsHtml(club) + clubReferralHtml(club.referral) + clubRewardsHtml(club.rewards)
      + clubDiscountsHtml(club.discounts) + clubHistoryHtml(history.items, history.hasMore);
  }

  function rewardCodeHtml(code) {
    return `${copyRow("کد تخفیف", code)}<p class="muted sm">هنگام خرید سرویس، کد را در بخش «کد تخفیف» وارد کنید.</p>`;
  }

  function rewardServicePickerHtml(reward, services) {
    if (!services || !services.length) return emptyHtml("برای این جایزه سرویس فعالی ندارید.");
    return `<p class="muted sm">${esc(rewardValueText(reward))} به کدام سرویس اضافه شود؟ هزینه: ${esc(pointsText(reward.cost))}</p>
      <div class="plan-list">${services.map((s) => `<button type="button" class="plan-option" data-action="redeem-reward-service" data-id="${Number(reward.id) || 0}" data-service="${Number(s.id) || 0}">
        <span class="plan-meta"><strong dir="ltr">${esc(s.name)}</strong></span>${icon("chevron", 16)}
      </button>`).join("")}</div>`;
  }

  /* ---------- receipt review ---------- */

  function reviewKindText(item) {
    if (item.kind === "topup") return "شارژ کیف پول";
    return item.plan_name ? `خرید «${item.plan_name}»` : "خرید سرویس";
  }

  function reviewQueueHtml(data) {
    const items = (data && data.items) || [];
    if (!items.length) return emptyHtml("رسیدی در انتظار بررسی نیست.");
    const total = Number(data.total) || items.length;
    return `<section class="card list-card" aria-label="رسیدهای در انتظار">
      ${total > items.length ? `<p class="muted sm">${esc(num(items.length))} رسید قدیمی‌تر از ${esc(num(total))} رسید نمایش داده شده است.</p>` : ""}
      ${items.map((r) => `<button type="button" class="list-row" data-action="open-review" data-id="${Number(r.id) || 0}">
        <span class="list-row-label"><span>${esc(money(r.amount))}</span>
          <span class="muted sm">، ${esc(reviewKindText(r))}</span>
          <span class="muted sm reward-desc"><bdi>${esc(r.payer || "—")}</bdi>، ${esc(core.formatDateTime(r.created_at))}</span></span>
        ${icon("chevron", 16)}
      </button>`).join("")}
    </section>`;
  }

  /** Sheet body for one receipt; ``imageUrl`` is a blob: URL made by the app, or "" while loading. */
  function reviewSheetHtml(item, imageUrl, imageError) {
    const id = Number(item.id) || 0;
    const photo = imageUrl && /^blob:/.test(imageUrl)
      ? `<img class="receipt-photo" src="${esc(imageUrl)}" alt="تصویر رسید پرداخت #${esc(num(id))}">`
      : imageError ? `<p class="muted sm">${esc(imageError)}</p>` : loadingHtml(1);
    return `${photo}
      <div class="kv-list">
        <p><span class="muted">مبلغ</span> <strong>${esc(money(item.amount))}</strong></p>
        <p><span class="muted">نوع</span> ${esc(reviewKindText(item))}</p>
        <p><span class="muted">پرداخت‌کننده</span> <bdi>${esc(item.payer || "—")}</bdi></p>
        <p><span class="muted">زمان</span> ${esc(core.formatDateTime(item.created_at))}</p>
      </div>
      <div class="btn-row">
        <button type="button" class="btn" data-action="review-approve" data-id="${id}">تأیید پرداخت</button>
        <button type="button" class="btn ghost danger" data-action="review-reject" data-id="${id}">رد پرداخت</button>
      </div>`;
  }

  function loadingHtml(rows) {
    return `<div class="loading" aria-busy="true" aria-label="در حال بارگذاری">${'<div class="skeleton"></div>'.repeat(Number(rows) || 3)}</div>`;
  }

  function emptyHtml(message, cta) {
    return `<div class="empty"><p>${esc(message)}</p>${cta ? `<button type="button" class="btn" data-action="goto" data-view="${esc(cta.view)}">${esc(cta.label)}</button>` : ""}</div>`;
  }

  /** ``opts.label`` renames the action button; ``opts.code`` is shown small for support. */
  function errorHtml(message, retry, opts) {
    const o = opts || {};
    const label = o.label || "تلاش دوباره";
    const button = retry ? `<button type="button" class="btn ghost sm" data-action="${esc(retry)}">${esc(label)}</button>` : "";
    const code = o.code ? `<p class="error-code" dir="ltr">${esc(o.code)}</p>` : "";
    return `<div class="error-box" role="alert"><p>${esc(message)}</p>${button}${code}</div>`;
  }

  root.PGMini.render = Object.freeze({
    icon, isOnHoldStatus, statusTone, expireDaysLabel, ringHtml, serviceRowHtml, glanceRowHtml,
    brandHeaderHtml, noticeBannerHtml, shortcutsHtml, panelShortcutsHtml, heroHtml,
    attentionCardHtml, serviceDetailHtml, planOptionsHtml, checkoutHtml, discountedCheckoutHtml, giftFormHtml, renewPlansHtml,
    renewPreviewHtml, addonPacksHtml, automationHtml, cancellationHtml, qrHtml, activityHtml,
    backBarHtml, guideListHtml, guideHtml, rewardValueText, clubHtml, rewardCodeHtml, rewardServicePickerHtml, reviewQueueHtml, reviewSheetHtml, walletCardHtml, supportHtml, ticketNewFormHtml, ticketThreadHtml, topupFormHtml, topupPayHtml, receiptFormHtml, recentTopupsHtml, statsHtml, attentionListHtml, navHtml, loadingHtml, emptyHtml,
    errorHtml,
  });
})(typeof window === "object" ? window : globalThis);
