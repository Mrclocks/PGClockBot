/* Telegram Mini App — mobile UX: buy/renew/addons, service details, QR, wallet */
(function () {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
    try {
      tg.setHeaderColor("#09090b");
      tg.setBackgroundColor("#09090b");
    } catch (_) {}
  }

  const initData = (tg && tg.initData) || "";
  const root = document.getElementById("root");
  const navEl = document.getElementById("nav");
  const helloEl = document.getElementById("hello");
  const subEl = document.getElementById("subtitle");
  const roleEl = document.getElementById("role-badge");

  let state = null;
  let currency = "تومان";
  let activeView = "home";
  let busy = false;
  let backBound = false;

  function homeViewId() {
    if (state && state.nav && state.nav.length) return state.nav[0].id;
    return "home";
  }

  function hasOpenOverlay() {
    return !!(
      document.querySelector(".qr-box:not([hidden])") ||
      document.querySelector(".renew-sheet:not([hidden]), .addon-sheet:not([hidden])")
    );
  }

  function closeOpenOverlays() {
    let closed = false;
    document.querySelectorAll(".qr-box:not([hidden])").forEach((box) => {
      box.hidden = true;
      box.classList.remove("is-ready", "is-loading");
      box.textContent = "";
      const id = box.getAttribute("data-qr-box");
      const trigger = id && document.querySelector(`[data-qr="${id}"]`);
      if (trigger) trigger.textContent = "نمایش QR";
      closed = true;
    });
    document.querySelectorAll(".renew-sheet:not([hidden]), .addon-sheet:not([hidden])").forEach((host) => {
      host.hidden = true;
      host.innerHTML = "";
      closed = true;
    });
    return closed;
  }

  /**
   * Android/iOS system back only navigates *inside* a Mini App when
   * Telegram.WebApp.BackButton is visible. Without show()+onClick, the OS
   * back key closes the WebApp (or appears to "do nothing" for in-app nav).
   * Chat bots cannot intercept hardware back — only this WebApp API can.
   */
  function syncTelegramBackButton() {
    if (!tg || !tg.BackButton) return;
    const show = hasOpenOverlay() || activeView !== homeViewId();
    try {
      if (show) tg.BackButton.show();
      else tg.BackButton.hide();
    } catch (_) {}
  }

  function onTelegramBack() {
    if (closeOpenOverlays()) {
      syncTelegramBackButton();
      return;
    }
    if (activeView !== homeViewId()) {
      setView(homeViewId());
      return;
    }
    syncTelegramBackButton();
  }

  function bindTelegramBackButton() {
    if (!tg || !tg.BackButton || backBound) return;
    backBound = true;
    try {
      tg.BackButton.onClick(onTelegramBack);
    } catch (_) {
      try {
        tg.onEvent("backButtonClicked", onTelegramBack);
      } catch (__) {}
    }
  }

  const ICONS = {
    home: '<svg viewBox="0 0 24 24"><path d="M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1h-5v-6H10v6H5a1 1 0 0 1-1-1z"/></svg>',
    svc: '<svg viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M8 9h8M8 13h5"/></svg>',
    shop: '<svg viewBox="0 0 24 24"><path d="M6 8h12l-1 11H7L6 8z"/><path d="M9 8V6a3 3 0 0 1 6 0v2"/></svg>',
    wallet: '<svg viewBox="0 0 24 24"><rect x="3" y="6" width="18" height="12" rx="2"/><path d="M3 10h18M15 14h2"/></svg>',
    ops: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 3v2M12 19v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M3 12h2M19 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  };

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  function safeUrl(u) {
    const s = String(u || "").trim();
    if (!s) return "";
    const low = s.toLowerCase();
    if (
      low.startsWith("javascript:") ||
      low.startsWith("data:") ||
      low.startsWith("vbscript:")
    ) {
      return "";
    }
    return s;
  }

  function money(n) {
    return num(n).replace(/٬/g, ",") + " " + currency;
  }

  function num(n) {
    return (Number(n) || 0).toLocaleString("fa-IR");
  }

  function toast(msg, kind) {
    const el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2800);
    if (tg && tg.HapticFeedback) {
      try {
        tg.HapticFeedback.notificationOccurred(kind === "err" ? "error" : "success");
      } catch (_) {}
    }
  }

  async function api(path, opts) {
    const res = await fetch(path, {
      method: (opts && opts.method) || "GET",
      headers: {
        "X-Telegram-Init-Data": initData,
        ...((opts && opts.body) ? { "Content-Type": "application/json" } : {}),
      },
      body: opts && opts.body ? JSON.stringify(opts.body) : undefined,
      cache: "no-store",
    });
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (_) {
      data = { detail: text };
    }
    if (!res.ok) {
      const detail = (data && (data.detail || data.message)) || text || res.statusText;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  function personaLabel(p) {
    if (p === "admin") return "ادمین";
    if (p === "reseller") return "نماینده";
    return "کاربر";
  }

  function hashView() {
    return (location.hash || "").replace(/^#/, "").trim() || "home";
  }

  function setView(id) {
    const next = id || "home";
    if (next !== activeView) {
      closeOpenOverlays();
    }
    activeView = next;
    if (location.hash.replace(/^#/, "") !== activeView) {
      history.replaceState(null, "", "#" + activeView);
    }
    document.querySelectorAll(".panel").forEach((el) => {
      el.classList.toggle("active", el.dataset.view === activeView);
    });
    document.querySelectorAll(".ma-nav button").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === activeView);
    });
    syncTelegramBackButton();
  }

  function openPanelPath(path) {
    const p = String(path || "");
    // Only same-origin relative panel paths — block protocol-relative / absolute hijacks
    if (!p.startsWith("/") || p.startsWith("//") || p.includes("://")) return;
    const base = (state && state.panel_base) || "";
    const url = safeUrl(base + p);
    if (!url) return;
    if (tg && tg.openLink) tg.openLink(url);
    else window.open(url, "_blank");
  }

  function statusClass(status) {
    const s = String(status || "").toLowerCase();
    if (s === "active") return "ok";
    if (s === "limited" || s === "disabled" || isOnHoldStatus(s)) return "warn";
    if (s === "expired") return "danger";
    return "";
  }

  function isOnHoldStatus(status) {
    const s = String(status || "")
      .toLowerCase()
      .replace(/-/g, "_");
    return s === "on_hold" || s === "onhold";
  }

  /** Remaining-time label — never map on_hold + null days to «نامحدود». */
  function expireDaysLabel(s, { withUnit } = {}) {
    if (s && s.expire_days_label) return String(s.expire_days_label);
    if (s && s.expire_days != null) {
      const n = num(s.expire_days);
      return withUnit ? n + " روز" : n;
    }
    if (s && (s.pending_start || isOnHoldStatus(s.status))) return "پس از اتصال";
    if (!s || s.error || !s.status || s.status === "—") return "—";
    return "نامحدود";
  }

  function meterClass(pct) {
    if (pct == null) return "";
    if (pct >= 90) return "danger";
    if (pct >= 75) return "warn";
    return "";
  }

  async function copyText(text) {
    const s = String(text || "");
    if (!s) return false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(s);
        return true;
      }
    } catch (_) {}
    try {
      const ta = document.createElement("textarea");
      ta.value = s;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
      return true;
    } catch (_) {
      return false;
    }
  }

  function markCopied(btn) {
    if (!btn) return;
    const idle =
      btn.dataset.copyIdle ||
      ((btn.textContent || "").trim() && (btn.textContent || "").trim() !== "کپی شد"
        ? (btn.textContent || "").trim()
        : "کپی لینک");
    btn.dataset.copyIdle = idle;
    btn.textContent = "کپی شد";
    btn.classList.add("is-copied");
    if (btn._copyTimer) clearTimeout(btn._copyTimer);
    btn._copyTimer = setTimeout(() => {
      btn.textContent = btn.dataset.copyIdle || "کپی لینک";
      btn.classList.remove("is-copied");
    }, 1400);
  }

  function renderNav(nav) {
    navEl.innerHTML = "";
    (nav || []).forEach((item) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.dataset.view = item.id;
      btn.innerHTML =
        (ICONS[item.icon] || ICONS.home) + "<span>" + esc(item.label) + "</span>";
      btn.addEventListener("click", () => setView(item.id));
      navEl.appendChild(btn);
    });
  }

  function statsHtml(stats, keys) {
    return (
      '<div class="stat-grid">' +
      keys
        .map(
          ([k, label]) =>
            `<div class="stat"><span>${esc(label)}</span><strong>${esc(
              num(stats[k])
            )}</strong></div>`
        )
        .join("") +
      "</div>"
    );
  }

  function linksHtml(links) {
    if (!links || !links.length) {
      return '<p class="hint">آدرس پنل در دسترس نیست.</p>';
    }
    return links
      .map(
        (l) =>
          `<div class="link-row"><div><strong>${esc(l.label)}</strong></div>
          <button type="button" class="btn ghost sm" data-path="${esc(l.path)}">باز کردن</button></div>`
      )
      .join("");
  }

  function serviceInfoErrorHtml(s) {
    if (!s.error) return "";
    return `<div class="error svc-info-error" role="status">⚠ ${esc(s.error_message || "دریافت اطلاعات سرویس ناموفق بود؛ دوباره تلاش کنید.")}
      ${s.error_code ? `<small class="svc-error-code" dir="ltr">${esc(s.error_code)}</small>` : ""}</div>`;
  }

  function serviceCardHtml(s) {
    const pct = s.traffic_pct;
    const meter =
      pct == null
        ? ""
        : `<div class="meter ${meterClass(pct)}"><i style="width:${Math.min(
            100,
            pct
          )}%"></i></div>`;
    const url = esc(safeUrl(s.subscription_url || ""));
    return `<article class="svc-card" data-svc-card="${Number(s.id) || 0}">
      <div class="svc-top">
        <div class="svc-title" dir="ltr">${esc(s.username || "—")}</div>
        <span class="badge ${statusClass(s.status)}">${esc(s.status_fa || s.status || "—")}</span>
      </div>
      <div class="muted" style="font-size:12px">حجم مصرفی</div>
      <div><strong>${esc(s.traffic || "—")}</strong></div>
      ${meter}
      <div class="meta-grid">
        <div class="meta"><small>انقضا</small><strong>${esc(s.expire || "—")}</strong></div>
        <div class="meta"><small>${s.pending_start || isOnHoldStatus(s.status) ? "مدت پس از اتصال" : "روز باقیمانده"}</small><strong>${esc(
          expireDaysLabel(s)
        )}</strong></div>
      </div>
      ${
        s.online_at
          ? `<p class="hint">آخرین آنلاین: ${esc(s.online_at)}</p>`
          : ""
      }
      ${serviceInfoErrorHtml(s)}
      ${s.info_fetched_at ? `<p class="hint">آخرین دریافت از پنل: ${esc(new Date(s.info_fetched_at).toLocaleTimeString("fa-IR"))}</p>` : ""}
      <div class="svc-actions">
        <button type="button" class="btn ghost sm" data-refresh-service="${Number(s.id) || 0}">به‌روزرسانی اطلاعات</button>
        <button type="button" class="btn ghost sm" data-copy="${url}">کپی لینک</button>
        <button type="button" class="btn ghost sm" data-qr="${Number(s.id) || 0}">نمایش QR</button>
        <button type="button" class="btn ghost sm" data-cancellation="${Number(s.id) || 0}">درخواست لغو</button>
        <button type="button" class="btn secondary sm" data-open-url="${url}">باز کردن لینک</button>
        ${!s.is_cancelled && !s.cancellation_pending ? `<button type="button" class="btn sm" data-renew="${Number(s.id) || 0}">تمدید</button>` : ""}
        ${s.addons_allowed && !s.is_cancelled && !s.cancellation_pending ? `
          <button type="button" class="btn secondary sm" data-addons="${Number(s.id)}" data-addon-kind="volume">افزایش حجم</button>
          <button type="button" class="btn secondary sm" data-addons="${Number(s.id)}" data-addon-kind="duration">افزایش زمان</button>
        ` : ""}
        ${!s.is_cancelled && !s.cancellation_pending ? `<button type="button" class="btn ghost sm" data-automation="${Number(s.id) || 0}">⚙️ تنظیمات خودکار</button>` : ""}
      </div>
      <div class="qr-box" data-qr-box="${Number(s.id) || 0}" hidden></div>
      <div class="renew-sheet" data-renew-host="${Number(s.id) || 0}" hidden></div>
      <div class="addon-sheet" data-addon-host="${Number(s.id) || 0}" hidden></div>
      <div class="renew-sheet" data-automation-host="${Number(s.id) || 0}" hidden></div>
      <div class="renew-sheet" data-cancellation-host="${Number(s.id) || 0}" hidden></div>
    </article>`;
  }

  function servicePeekHtml(s) {
    return `<div class="svc-peek">
      <div class="svc-peek-main">
        <strong dir="ltr">${esc(s.username || "—")}</strong>
        <div class="svc-peek-meta">
          <span>${esc(s.traffic || "—")}</span>
          <span>${esc(expireDaysLabel(s, { withUnit: true }))}</span>
        </div>
        ${serviceInfoErrorHtml(s)}
        ${s.error ? `<button type="button" class="btn ghost sm" data-refresh-service="${Number(s.id) || 0}">تلاش مجدد</button>` : ""}
      </div>
      <span class="badge ${statusClass(s.status)}">${esc(s.status_fa || s.status || "—")}</span>
    </div>`;
  }

  function plansHtml(plans, { renewServiceId } = {}) {
    const list = plans || [];
    if (!list.length) return '<p class="muted">پلنی فعال نیست</p>';
    return list
      .map((p) => {
        const action = renewServiceId
          ? `data-do-renew="${Number(renewServiceId)}" data-plan="${Number(p.id) || 0}"`
          : `data-buy="${Number(p.id) || 0}"`;
        const label = renewServiceId ? "تمدید" : p.is_trial ? "دریافت" : "خرید";
        return `<div class="plan-row">
          <div class="plan-meta">
            <strong>${esc(p.name)}</strong>
            <div class="muted">${esc(p.days)} روز · ${esc(p.gb ?? "∞")} گیگ${
          p.is_trial ? " · تست" : ""
        }</div>
          </div>
          <div style="display:flex;flex-direction:column;align-items:flex-end;gap:6px">
            <div class="price">${esc(money(p.price))}</div>
            <button type="button" class="btn sm" ${action}>${label}</button>
          </div>
        </div>`;
      })
      .join("");
  }

  function activityHtml(rows) {
    if (!rows || !rows.length) return '<p class="muted">تراکنشی نیست</p>';
    return rows
      .map((a) => {
        const amt = Number(a.amount) || 0;
        const cls = amt >= 0 ? "plus" : "minus";
        const sign = amt >= 0 ? "+" : "";
        let when = "";
        if (a.created_at) {
          try {
            when = new Date(a.created_at).toLocaleString("fa-IR");
          } catch (_) {}
        }
        return `<div class="tx-row">
          <div class="tx-meta">
            <strong>${esc(a.reason || "—")}</strong>
            <div class="muted">${esc(when)}</div>
          </div>
          <div class="tx-amt ${cls}">${esc(sign + money(amt))}</div>
        </div>`;
      })
      .join("");
  }

  function walletCardHtml(customer) {
    return `<div class="card wallet-card">
      <div class="card-head">
        <h3>کیف پول</h3>
        <span class="badge">${customer.wallet_pay_enabled ? "پرداخت فعال" : "فقط مشاهده"}</span>
      </div>
      <p class="wallet-label">موجودی</p>
      <p class="wallet-value">${esc(money(customer.wallet))}</p>
      <div class="wallet-actions">
        <button type="button" class="btn ghost sm" data-goto="wallet">تراکنش‌ها</button>
        <button type="button" class="btn sm" data-goto="shop">خرید سرویس</button>
      </div>
      <p class="hint">شارژ کیف پول از ربات انجام می‌شود؛ خرید، تمدید و افزایش حجم و زمان این‌جا با موجودی کیف پول است.</p>
    </div>`;
  }

  function renderUserPanels(data) {
    const c = data.customer || {};
    const services = c.services || [];
    const homeSvcs = services.slice(0, 4);
    return {
      home: `
        ${walletCardHtml(c)}
        <div class="card">
          <div class="card-head">
            <h3>سرویس‌های من</h3>
            <button type="button" class="btn ghost sm" data-goto="services">همه</button>
          </div>
          ${
            homeSvcs.length
              ? homeSvcs.map((s) => servicePeekHtml(s)).join("")
              : '<p class="muted">سرویسی ندارید — از بخش خرید شروع کنید.</p>'
          }
        </div>`,
      services: `
        <div class="card"><h3>سرویس‌ها</h3>
          <p class="hint">جزئیات، QR، تمدید و افزایش حجم و زمان زیر هر سرویس</p>
        </div>
        ${
          services.length
            ? services.map((s) => serviceCardHtml(s)).join("")
            : '<div class="card"><p class="muted">سرویسی ندارید</p></div>'
        }`,
      shop: `
        <div class="card">
          <div class="card-head"><h3>خرید پلن</h3>
            <span class="muted">${esc(money(c.wallet))}</span>
          </div>
          ${
            c.wallet_pay_enabled
              ? plansHtml(c.plans)
              : '<p class="muted">پرداخت با کیف پول در تنظیمات ربات غیرفعال است.</p>'
          }
          <p class="hint">پس از خرید، سرویس در تب «سرویس» ظاهر می‌شود.</p>
        </div>`,
      wallet: `
        <div class="card wallet-card">
          <h3>کیف پول</h3>
          <p class="wallet-label">موجودی</p>
          <p class="wallet-value">${esc(money(c.wallet))}</p>
        </div>
        <div class="card">
          <h3>تراکنش‌ها</h3>
          ${activityHtml(c.activity)}
        </div>`,
    };
  }

  function renderOpsExtra(data) {
    const ops = data.ops || {};
    if (data.persona === "admin") {
      return `
        <div class="card">
          <h3>نمای کلی پلتفرم</h3>
          ${statsHtml(ops.stats || {}, [
            ["users", "کاربران"],
            ["resellers", "نمایندگان"],
            ["orders", "سفارش‌ها"],
            ["pending", "رسید معلق"],
            ["tickets", "تیکت باز"],
            ["revenue", "درآمد"],
          ])}
        </div>
        <div class="card">
          <h3>میانبر پنل</h3>
          ${linksHtml(ops.panel_links)}
        </div>`;
    }
    if (data.persona === "reseller") {
      const shop = ops.shop || {};
      const billing = shop.billing;
      return `
        ${
          billing
            ? `<div class="card wallet-card">
                <h3>کیف پول فروشگاهی</h3>
                <p class="wallet-label">PAYG</p>
                <p class="wallet-value">${esc(money(billing.balance))}</p>
                ${billing.suspended ? '<p class="hint" style="color:#fca5a5">حساب مسدود است</p>' : ""}
              </div>`
            : ""
        }
        <div class="card">
          <h3>فروشگاه من</h3>
          ${statsHtml(ops.stats || {}, [
            ["users", "کاربران"],
            ["orders", "سفارش‌ها"],
            ["pending", "رسید معلق"],
            ["tickets", "تیکت باز"],
            ["services", "سرویس‌ها"],
            ["revenue", "درآمد"],
          ])}
          ${
            shop.bot_username
              ? `<p class="hint" dir="ltr">@${esc(shop.bot_username)}</p>`
              : ""
          }
        </div>
        <div class="card">
          <h3>میانبر پنل</h3>
          ${linksHtml(ops.panel_links)}
        </div>`;
    }
    return "";
  }

  const refreshingServices = new Set();

  async function refreshService(serviceId, button) {
    if (refreshingServices.has(serviceId)) return;
    refreshingServices.add(serviceId);
    button.disabled = true;
    const idle = button.textContent;
    button.textContent = "در حال دریافت…";
    try {
      const result = await api("/api/mini/service/" + serviceId);
      if (!result.service) throw new Error("missing_service_info");
      const services = (state && state.customer && state.customer.services) || [];
      const index = services.findIndex((s) => Number(s.id) === serviceId);
      if (index >= 0 && result.service) {
        services[index] = { ...services[index], ...result.service };
        mount(state);
      }
      if (result.service && result.service.error) {
        toast(result.service.error_message || "دریافت اطلاعات سرویس ناموفق بود", "err");
      } else {
        toast("اطلاعات سرویس به‌روز شد");
      }
    } catch (_) {
      toast("دریافت اطلاعات سرویس ناموفق بود؛ دوباره تلاش کنید.", "err");
    } finally {
      refreshingServices.delete(serviceId);
      if (button.isConnected) {
        button.disabled = false;
        button.textContent = idle;
      }
    }
  }

  async function showCancellation(serviceId) {
    const host = document.querySelector(`[data-cancellation-host="${serviceId}"]`);
    if (!host || host.cancellationBusy) return;
    host.hidden = false;
    host.innerHTML = '<p class="muted">در حال دریافت درخواست…</p>';
    try {
      const data = await api(`/api/mini/service/${serviceId}/cancellation`);
      const row = data.request;
      host.innerHTML = '<h4>درخواست لغو</h4><p class="hint">مبلغ را اپراتور تعیین می‌کند. اعتبار پس از تأیید غیرفعال‌شدن سرویس به کیف پول همین فروشگاه برمی‌گردد. ثبت درخواست، سرویس را غیرفعال نمی‌کند.</p>';
      if (row) host.innerHTML += `<p>${esc(row.label)}</p>${row.refund_amount == null ? "" : `<p>اعتبار برگشتی: ${esc(money(row.refund_amount))}</p>`}<p style="white-space:pre-wrap">${esc(row.operator_note || "")}</p>`;
      if (!row || ["rejected", "withdrawn"].includes(row.status)) {
        host.innerHTML += '<form data-cancellation-form><label>دلیل لغو<textarea name="reason" rows="3" maxlength="1000" required></textarea></label><button type="submit" class="btn sm">ثبت درخواست</button></form>';
        const form = host.querySelector("[data-cancellation-form]");
        form.addEventListener("submit", async (event) => {
          event.preventDefault();
          if (host.cancellationBusy) return;
          const reason = form.elements.reason.value.trim();
          if (!reason) return;
          host.cancellationBusy = true;
          form.querySelector("button").disabled = true;
          try {
            await api(`/api/mini/service/${serviceId}/cancellation`, {method: "POST", body: {reason}});
            toast("درخواست ثبت شد");
            host.cancellationBusy = false;
            await showCancellation(serviceId);
          } catch (err) { toast(err.message, "err"); }
          finally {
            host.cancellationBusy = false;
            form.querySelector("button").disabled = false;
          }
        });
      }
      host.innerHTML += `<button type="button" class="btn ghost sm" data-cancellation="${serviceId}">به‌روزرسانی درخواست</button>`;
      bindActions(host);
    } catch (err) { host.innerHTML = `<p class="hint">${esc(err.message)}</p>`; }
  }

  function bindActions(scope) {
    scope.querySelectorAll("[data-cancellation]").forEach((btn) => {
      btn.addEventListener("click", () => showCancellation(Number(btn.dataset.cancellation)));
    });
    scope.querySelectorAll("[data-refresh-service]").forEach((btn) => {
      btn.addEventListener("click", () => refreshService(Number(btn.dataset.refreshService), btn));
    });
    scope.querySelectorAll("[data-path]").forEach((btn) => {
      btn.addEventListener("click", () => openPanelPath(btn.getAttribute("data-path")));
    });
    scope.querySelectorAll("[data-goto]").forEach((btn) => {
      btn.addEventListener("click", () => setView(btn.getAttribute("data-goto")));
    });
    scope.querySelectorAll("[data-copy]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const ok = await copyText(btn.getAttribute("data-copy"));
        if (ok) markCopied(btn);
        else toast("کپی نشد", "err");
      });
    });
    scope.querySelectorAll("[data-open-url]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const url = safeUrl(btn.getAttribute("data-open-url"));
        if (!url) return;
        if (tg && tg.openLink) tg.openLink(url);
        else window.open(url, "_blank");
      });
    });
    scope.querySelectorAll("[data-qr]").forEach((btn) => {
      btn.addEventListener("click", () => showQr(Number(btn.getAttribute("data-qr"))));
    });
    scope.querySelectorAll("[data-renew]").forEach((btn) => {
      btn.addEventListener("click", () => showRenew(Number(btn.getAttribute("data-renew"))));
    });
    scope.querySelectorAll("[data-automation]").forEach((btn) => {
      btn.addEventListener("click", () => showAutomation(Number(btn.getAttribute("data-automation"))));
    });
    scope.querySelectorAll("[data-buy]").forEach((btn) => {
      btn.addEventListener("click", () => doBuy(Number(btn.getAttribute("data-buy"))));
    });
    scope.querySelectorAll("[data-do-renew]").forEach((btn) => {
      btn.addEventListener("click", () =>
        previewRenew(
          Number(btn.getAttribute("data-do-renew")),
          Number(btn.getAttribute("data-plan"))
        )
      );
    });
    scope.querySelectorAll("[data-confirm-renew]").forEach((btn) => {
      btn.addEventListener("click", () => doRenew(
        Number(btn.getAttribute("data-confirm-renew")), Number(btn.getAttribute("data-plan"))
      ));
    });
    scope.querySelectorAll("[data-renew-back]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const serviceId = Number(btn.getAttribute("data-renew-back"));
        const host = document.querySelector(`[data-renew-host="${serviceId}"]`);
        if (host) host.hidden = true;
        showRenew(serviceId);
      });
    });
    scope.querySelectorAll("[data-addons]").forEach((btn) => {
      btn.addEventListener("click", () => showAddons(
        Number(btn.getAttribute("data-addons")), btn.getAttribute("data-addon-kind")
      ));
    });
    scope.querySelectorAll("[data-do-addon]").forEach((btn) => {
      btn.addEventListener("click", () => doAddon(
        Number(btn.getAttribute("data-do-addon")), Number(btn.getAttribute("data-pack"))
      ));
    });
  }

  async function showAutomation(serviceId) {
    const host = document.querySelector(`[data-automation-host="${serviceId}"]`);
    if (!host) return;
    if (!host.hidden) { host.hidden = true; return; }
    host.hidden = false;
    host.textContent = "در حال دریافت تنظیمات…";
    try {
      const data = await api(`/api/mini/service/${serviceId}/automation`);
      host.innerHTML = `<p>با روشن کردن هر گزینه، خرید تکرارشونده با قیمت فعلی از کیف پول همین فروشگاه فعال می‌شود. بسته زمان یا حجم برای مورد تمام‌شده اولویت دارد؛ در غیر این صورت تمدید کامل انجام می‌شود.</p>
        ${data.needs_review ? `<p class="hint">⚠️ سفارش #${Number(data.pending_order_id)} نیاز به بررسی پشتیبانی دارد؛ اجرای خودکار متوقف است.</p>` : ""}
        ${data.actions.map((a) => `<form class="automation-option" data-auto-form="${esc(a.action)}">
          <label><input type="checkbox" name="enabled" ${a.enabled ? "checked" : ""}> ${esc(a.label)}</label>
          <select name="choice_id" aria-label="انتخاب پلن یا بسته">
            <option value="">انتخاب پلن یا بسته</option>
            ${a.choices.map((c) => `<option value="${Number(c.id)}" ${Number(c.id) === Number(a.choice_id) ? "selected" : ""}>${esc(c.name)} — ${esc(money(c.price))}</option>`).join("")}
          </select>
          ${a.unavailable ? `<p class="hint">⚠️ گزینه قبلی حذف یا غیرفعال شده؛ پلن یا بسته جدید انتخاب کنید.</p>` : ""}
          <button type="submit" class="btn sm">ذخیره</button>
        </form>`).join("")}`;
      host.querySelectorAll("[data-auto-form]").forEach((form) => {
        form.addEventListener("submit", async (event) => {
          event.preventDefault();
          const button = form.querySelector("button");
          button.disabled = true;
          try {
            const enabled = form.elements.enabled.checked;
            const choiceId = Number(form.elements.choice_id.value) || null;
            if (enabled && !choiceId) throw new Error("پلن یا بسته را انتخاب کنید");
            await api(`/api/mini/service/${serviceId}/automation`, {method: "POST", body: {action: form.dataset.autoForm, enabled, choice_id: choiceId}});
            toast("تنظیمات ذخیره شد");
            host.hidden = true;
            await showAutomation(serviceId);
          } catch (error) { toast(error.message || "ذخیره ناموفق"); }
          finally { button.disabled = false; }
        });
      });
    } catch (error) { host.textContent = error.message || "دریافت تنظیمات ناموفق"; }
    syncTelegramBackButton();
  }

  async function showQr(serviceId) {
    const box = document.querySelector(`[data-qr-box="${serviceId}"]`);
    const trigger = document.querySelector(`[data-qr="${serviceId}"]`);
    if (!box) return;
    if (!box.hidden && box.querySelector("img")) {
      box.hidden = true;
      box.classList.remove("is-ready", "is-loading");
      box.textContent = "";
      if (trigger) trigger.textContent = "نمایش QR";
      syncTelegramBackButton();
      return;
    }
    box.hidden = false;
    box.classList.add("is-loading");
    box.classList.remove("is-ready");
    box.textContent = "در حال ساخت QR…";
    syncTelegramBackButton();
    try {
      const data = await api("/api/mini/service/" + serviceId + "/qr");
      box.textContent = "";
      box.classList.remove("is-loading");
      box.classList.add("is-ready");
      const frame = document.createElement("div");
      frame.className = "qr-frame";
      const img = document.createElement("img");
      img.alt = "QR";
      img.src = "data:image/png;base64," + data.png_base64;
      frame.appendChild(img);
      box.appendChild(frame);
      const hint = document.createElement("div");
      hint.className = "hint";
      hint.textContent = "اسکن برای افزودن سابسکریپشن";
      box.appendChild(hint);
      if (trigger) trigger.textContent = "بستن QR";
    } catch (e) {
      box.classList.remove("is-loading", "is-ready");
      box.textContent = "";
      const err = document.createElement("div");
      err.className = "error";
      err.textContent = String(e.message || e);
      box.appendChild(err);
    }
    syncTelegramBackButton();
  }

  function showRenew(serviceId) {
    const host = document.querySelector(`[data-renew-host="${serviceId}"]`);
    if (!host) {
      setView("services");
      setTimeout(() => showRenew(serviceId), 50);
      return;
    }
    host.dataset.requestId = String(++renewalRequestId);
    renewalPreviews.delete(serviceId);
    if (!host.hidden) {
      host.hidden = true;
      host.innerHTML = "";
      syncTelegramBackButton();
      return;
    }
    const c = (state && state.customer) || {};
    host.hidden = false;
    host.innerHTML =
      "<h3 style='margin:0 0 12px;font-size:13px'>انتخاب پلن تمدید</h3>" +
      (c.wallet_pay_enabled
        ? plansHtml(
            (c.plans || []).filter((p) => !p.is_trial),
            { renewServiceId: serviceId }
          )
        : '<p class="muted">پرداخت کیف پول غیرفعال است</p>');
    bindActions(host);
    syncTelegramBackButton();
  }

  const addonPacks = new Map();
  let addonRequestId = 0;

  async function showAddons(serviceId, kind) {
    if (kind !== "volume" && kind !== "duration") return;
    const host = document.querySelector(`[data-addon-host="${serviceId}"]`);
    if (!host) return;
    if (!host.hidden && host.dataset.kind === kind) {
      host.hidden = true;
      host.textContent = "";
      syncTelegramBackButton();
      return;
    }
    closeOpenOverlays();
    addonPacks.delete(serviceId);
    const requestId = String(++addonRequestId);
    host.hidden = false;
    host.dataset.kind = kind;
    host.dataset.requestId = requestId;
    host.textContent = "در حال دریافت بسته‌ها…";
    syncTelegramBackButton();
    const c = (state && state.customer) || {};
    if (!c.wallet_pay_enabled) {
      host.textContent = "پرداخت با کیف پول غیرفعال است";
      return;
    }
    try {
      const data = await api("/api/mini/service/" + serviceId + "/addons?kind=" + encodeURIComponent(kind));
      if (host.hidden || host.dataset.requestId !== requestId || !host.isConnected) return;
      if (data.kind && data.kind !== kind) throw new Error("نوع بسته‌ها با بخش انتخاب‌شده مطابقت ندارد؛ دوباره تلاش کنید");
      const packs = (data.packs || []).filter((p) => p.kind === kind);
      addonPacks.set(serviceId, packs);
      const label = kind === "volume" ? "افزایش حجم" : "افزایش زمان";
      host.innerHTML = `<h3>${label}</h3><p class="hint">بسته پس از پرداخت به همین سرویس اضافه می‌شود.</p>` +
        (packs.length ? packs.map((p) => `<div class="plan-row">
          <div class="plan-meta"><strong>${esc(p.name)}</strong>
            <div class="muted">+${esc(p.amount_label)}</div>
            ${p.description ? `<p class="hint">${esc(p.description)}</p>` : ""}
          </div>
          <div class="addon-price"><div class="price">${esc(money(p.price))}</div>
            <button type="button" class="btn sm" data-do-addon="${serviceId}" data-pack="${Number(p.id)}">خرید بسته</button>
          </div>
        </div>`).join("") : '<p class="muted">بسته‌ای برای خرید فعال نیست</p>');
      bindActions(host);
    } catch (e) {
      if (!host.hidden && host.dataset.requestId === requestId && host.isConnected) {
        host.textContent = String(e.message || e);
      }
    }
  }

  async function doAddon(serviceId, packId) {
    if (busy) return;
    const c = (state && state.customer) || {};
    if (!(state && state.commerce_allowed) || !c.wallet_pay_enabled) {
      toast("پرداخت با کیف پول مجاز نیست", "err");
      return;
    }
    const pack = (addonPacks.get(serviceId) || []).find((p) => p.id === packId);
    const svc = (c.services || []).find((s) => s.id === serviceId);
    const host = document.querySelector(`[data-addon-host="${serviceId}"]`);
    if (!pack || !svc || !host || host.hidden || pack.kind !== host.dataset.kind) return;
    busy = true;
    try {
      const label = pack.kind === "volume" ? "افزایش حجم" : "افزایش زمان";
      const message = `${label} سرویس «${svc.username}» با بسته «${pack.name}» (+${pack.amount_label}) به مبلغ ${money(pack.price)} از کیف پول؟`;
      const ok = tg && tg.showConfirm
        ? await new Promise((resolve) => tg.showConfirm(message, resolve))
        : window.confirm(message);
      if (!ok) return;
      const res = await api("/api/mini/addon", {
        method: "POST", body: { service_id: serviceId, pack_id: packId, kind: pack.kind },
      });
      toast(res.message || "بسته اضافه شد", "ok");
      await reload();
      setView("services");
    } catch (e) {
      toast(String(e.message || e), "err");
    } finally {
      busy = false;
    }
  }

  async function doBuy(planId) {
    if (busy) return;
    if (!(state && state.commerce_allowed)) {
      toast("خرید برای این نقش مجاز نیست", "err");
      return;
    }
    const c = (state && state.customer) || {};
    if (!c.wallet_pay_enabled) {
      toast("پرداخت کیف پول غیرفعال است", "err");
      return;
    }
    if (tg && tg.showConfirm) {
      const plan = (c.plans || []).find((p) => p.id === planId);
      const ok = await new Promise((resolve) => {
        tg.showConfirm(
          "خرید «" + (plan ? plan.name : planId) + "» با کیف پول؟",
          resolve
        );
      });
      if (!ok) return;
    }
    busy = true;
    try {
      const res = await api("/api/mini/buy", { method: "POST", body: { plan_id: planId } });
      toast(res.message || "خرید شد", "ok");
      await reload();
      setView("services");
    } catch (e) {
      toast(String(e.message || e), "err");
    } finally {
      busy = false;
    }
  }

  const renewalPreviews = new Map();
  let renewalRequestId = 0;

  async function previewRenew(serviceId, planId) {
    const host = document.querySelector(`[data-renew-host="${serviceId}"]`);
    if (!host || busy || !(state && state.commerce_allowed)) return;
    renewalPreviews.delete(serviceId);
    const requestId = String(++renewalRequestId);
    host.dataset.requestId = requestId;
    host.innerHTML = '<p class="muted">دریافت پیش‌نمایش تمدید…</p>';
    try {
      const preview = await api(`/api/mini/service/${serviceId}/renewal-preview?plan_id=${planId}`);
      if (host.hidden || host.dataset.requestId !== requestId) return;
      renewalPreviews.set(serviceId, {planId, terms: preview.terms, price: preview.price, requestKey: preview.request_key});
      host.innerHTML = '<h3>پیش‌نمایش تمدید</h3><p>' + esc(preview.plan_name) + '</p>' +
        '<p>مبلغ: <b>' + esc(money(preview.price)) + '</b></p>' +
        (preview.lines || []).map((line) => '<p>' + esc(line) + '</p>').join('') +
        (preview.warnings || []).map((line) => '<p class="error">' + esc(line) + '</p>').join('') +
        '<p class="muted">' + esc(preview.notice) + '</p>' +
        `<button type="button" class="btn primary" data-confirm-renew="${serviceId}" data-plan="${planId}">تأیید و پرداخت با کیف پول</button>` +
        `<button type="button" class="btn ghost" data-renew-back="${serviceId}">انتخاب پلن دیگر</button>`;
      bindActions(host);
    } catch (e) {
      if (host.hidden || host.dataset.requestId !== requestId) return;
      host.innerHTML = '<p class="error">' + esc(String(e.message || e)) + '</p>' +
        `<button type="button" class="btn ghost" data-renew-back="${serviceId}">انتخاب پلن</button>`;
      bindActions(host);
    }
  }

  async function doRenew(serviceId, planId) {
    if (busy) return;
    if (!(state && state.commerce_allowed)) {
      toast("تمدید برای این نقش مجاز نیست", "err");
      return;
    }
    const preview = renewalPreviews.get(serviceId);
    if (!preview || preview.planId !== planId) return;
    busy = true;
    try {
      const res = await api("/api/mini/renew", {
        method: "POST",
        body: { service_id: serviceId, plan_id: planId, preview: {terms: preview.terms, price: preview.price, request_key: preview.requestKey} },
      });
      renewalPreviews.delete(serviceId);
      toast(res.message || "تمدید شد", "ok");
      await reload();
      setView("services");
    } catch (e) {
      toast(String(e.message || e), "err");
    } finally {
      busy = false;
    }
  }

  function mount(data) {
    currency = data.currency || "تومان";
    state = data;
    const name = (data.user && data.user.name) || "";
    helloEl.textContent = name ? "سلام " + name : "مینی‌اپ";
    roleEl.textContent = personaLabel(data.persona);
    subEl.textContent =
      data.persona === "admin"
        ? "فقط عملیات ادمین — بدون خرید"
        : data.persona === "reseller"
          ? "سرویس‌ها، خرید و پنل فروشگاه"
          : "سرویس‌ها، خرید و کیف پول";

    renderNav(data.nav);

    const panels = {};
    if (data.persona === "admin") {
      // Owner/admin: ops only — never commerce UI
      panels.home = `
        <div class="card">
          <h3>پنل سریع ادمین</h3>
          <p class="hint">خرید و کیف پول کاربر در مینی‌اپ برای ادمین فعال نیست — فقط نمای عملیاتی.</p>
        </div>
        ${renderOpsExtra(data)}`;
      panels.ops = renderOpsExtra(data);
    } else {
      Object.assign(panels, renderUserPanels(data));
      if (data.persona === "reseller") {
        const ops = data.ops || {};
        const peekStats = statsHtml(ops.stats || {}, [
          ["users", "کاربران"],
          ["orders", "سفارش‌ها"],
          ["pending", "رسید معلق"],
          ["tickets", "تیکت باز"],
        ]);
        const svcs = (data.customer && data.customer.services) || [];
        panels.ops = renderOpsExtra(data);
        panels.home = `
          ${walletCardHtml(data.customer || {})}
          <div class="card">
            <div class="card-head">
              <h3>فروشگاه من</h3>
              <button type="button" class="btn ghost sm" data-goto="ops">بیشتر</button>
            </div>
            ${peekStats}
          </div>
          <div class="card">
            <div class="card-head"><h3>سرویس‌های من</h3>
              <button type="button" class="btn ghost sm" data-goto="services">همه</button>
            </div>
            ${
              svcs.length
                ? svcs.slice(0, 4).map((s) => servicePeekHtml(s)).join("")
                : '<p class="muted">سرویسی ندارید</p>'
            }
          </div>`;
      }
    }

    root.innerHTML = Object.keys(panels)
      .map(
        (id) =>
          `<section class="panel" data-view="${esc(id)}">${panels[id]}</section>`
      )
      .join("");

    bindActions(root);

    const wanted = hashView();
    const ok = (data.nav || []).some((n) => n.id === wanted);
    setView(ok ? wanted : (data.nav[0] && data.nav[0].id) || "home");
  }

  async function reload() {
    const data = await api("/api/mini/me");
    mount(data);
  }

  async function load() {
    bindTelegramBackButton();
    root.innerHTML =
      '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
    try {
      await reload();
      syncTelegramBackButton();
    } catch (e) {
      root.textContent = "";
      const err = document.createElement("div");
      err.className = "error";
      err.appendChild(
        document.createTextNode("برای استفاده ابتدا ربات را /start کنید.")
      );
      err.appendChild(document.createElement("br"));
      err.appendChild(document.createTextNode(String(e.message || e)));
      root.appendChild(err);
      navEl.innerHTML = "";
      syncTelegramBackButton();
    }
  }

  window.addEventListener("hashchange", () => {
    if (!state) return;
    const wanted = hashView();
    const ok = (state.nav || []).some((n) => n.id === wanted);
    if (ok) setView(wanted);
  });

  load();
})();
