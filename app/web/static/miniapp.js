/* Telegram Mini App — full commerce + ops, panel visual language */
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
  let payMethods = null;
  let activeView = "home";
  let busy = false;
  let opsTab = "pending";

  const ICONS = {
    home: '<svg viewBox="0 0 24 24"><path d="M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1h-5v-6H10v6H5a1 1 0 0 1-1-1z"/></svg>',
    svc: '<svg viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M8 9h8M8 13h5"/></svg>',
    shop: '<svg viewBox="0 0 24 24"><path d="M6 8h12l-1 11H7L6 8z"/><path d="M9 8V6a3 3 0 0 1 6 0v2"/></svg>',
    wallet: '<svg viewBox="0 0 24 24"><rect x="3" y="6" width="18" height="12" rx="2"/><path d="M3 10h18M15 14h2"/></svg>',
    support: '<svg viewBox="0 0 24 24"><path d="M12 3a7 7 0 0 0-7 7v2a3 3 0 0 0 3 3h1v-5H7a5 5 0 0 1 10 0h-2v5h1a3 3 0 0 0 3-3v-2a7 7 0 0 0-7-7z"/><path d="M9 18h6v2H9z"/></svg>',
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
    if (low.startsWith("javascript:") || low.startsWith("data:") || low.startsWith("vbscript:")) {
      return "";
    }
    return s;
  }

  function money(n) {
    return (Number(n) || 0).toLocaleString("fa-IR") + " " + currency;
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
    const headers = { "X-Telegram-Init-Data": initData };
    let body = undefined;
    if (opts && opts.formData) {
      body = opts.formData;
    } else if (opts && opts.body) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, {
      method: (opts && opts.method) || "GET",
      headers,
      body,
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
    activeView = id || "home";
    if (location.hash.replace(/^#/, "") !== activeView) {
      history.replaceState(null, "", "#" + activeView);
    }
    document.querySelectorAll(".panel").forEach((el) => {
      el.classList.toggle("active", el.dataset.view === activeView);
    });
    document.querySelectorAll(".ma-nav button").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === activeView);
    });
  }

  function statusClass(st) {
    const s = String(st || "").toLowerCase();
    if (s.includes("active") || s === "فعال") return "ok";
    if (s.includes("expir") || s.includes("limited") || s.includes("disabled")) return "danger";
    if (s.includes("on_hold") || s.includes("warn")) return "warn";
    return "info";
  }

  function meterClass(pct) {
    if (pct == null) return "";
    if (pct >= 90) return "danger";
    if (pct >= 70) return "warn";
    return "";
  }

  async function copyText(text) {
    const t = String(text || "");
    if (!t) return false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(t);
        return true;
      }
    } catch (_) {}
    try {
      const ta = document.createElement("textarea");
      ta.value = t;
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch (_) {
      return false;
    }
  }

  function markCopied(btn) {
    const prev = btn.textContent;
    btn.textContent = "کپی شد";
    btn.classList.add("is-copied");
    setTimeout(() => {
      btn.textContent = prev;
      btn.classList.remove("is-copied");
    }, 1400);
  }

  function openPanelPath(path) {
    let p = String(path || "");
    if (!p.startsWith("/") || p.startsWith("//")) return;
    const base = ((state && state.panel_base) || "").replace(/\/$/, "");
    if (!base) {
      toast("آدرس پنل در دسترس نیست", "err");
      return;
    }
    const url = base + p;
    if (tg && tg.openLink) tg.openLink(url);
    else window.open(url, "_blank");
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
            `<div class="stat"><span>${esc(label)}</span><strong>${esc(num(stats[k]))}</strong></div>`
        )
        .join("") +
      "</div>"
    );
  }

  function linksHtml(links) {
    if (!links || !links.length) return '<p class="hint">آدرس پنل در دسترس نیست.</p>';
    return links
      .map(
        (l) =>
          `<div class="link-row"><div><strong>${esc(l.label)}</strong></div>
          <button type="button" class="btn btn-ghost sm" data-path="${esc(l.path)}">باز کردن</button></div>`
      )
      .join("");
  }

  function serviceCardHtml(s) {
    const pct = s.traffic_pct;
    const meter =
      pct == null
        ? ""
        : `<div class="meter ${meterClass(pct)}"><i style="width:${Math.min(100, pct)}%"></i></div>`;
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
        <div class="meta"><small>روز باقیمانده</small><strong>${
          s.expire_days == null ? "نامحدود" : esc(num(s.expire_days))
        }</strong></div>
      </div>
      ${s.paused ? '<p class="hint" style="color:#fca5a5">سرویس متوقف است</p>' : ""}
      ${
        s.auto_renew_enabled
          ? '<p class="hint">تمدید خودکار فعال است</p>'
          : ""
      }
      <div class="svc-actions">
        <button type="button" class="btn btn-ghost sm" data-copy="${url}">کپی لینک</button>
        <button type="button" class="btn btn-ghost sm" data-qr="${Number(s.id) || 0}">نمایش QR</button>
        <button type="button" class="btn btn-ghost sm" data-health="${Number(s.id) || 0}">تست اتصال</button>
        <button type="button" class="btn sm" data-checkout-renew="${Number(s.id) || 0}">تمدید</button>
        <button type="button" class="btn btn-ghost sm" data-auto-renew="${Number(s.id) || 0}">${
          s.auto_renew_enabled ? "خاموش‌کردن تمدیدخودکار" : "تمدید خودکار"
        }</button>
        <button type="button" class="btn btn-ghost sm" data-pause="${Number(s.id) || 0}" data-paused="${
          s.paused ? "1" : "0"
        }">${s.paused ? "از سرگیری" : "توقف"}</button>
      </div>
      <div class="qr-box" data-qr-box="${Number(s.id) || 0}" hidden></div>
      <div class="pay-sheet" data-pay-host="${Number(s.id) || 0}" hidden></div>
    </article>`;
  }

  function servicePeekHtml(s) {
    return `<div class="svc-peek">
      <div class="svc-peek-main">
        <strong dir="ltr">${esc(s.username || "—")}</strong>
        <div class="muted" style="font-size:12px">${esc(s.traffic || "—")} · ${
          s.expire_days == null ? "نامحدود" : esc(num(s.expire_days)) + " روز"
        }</div>
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
          ? `data-checkout-renew-plan="${Number(renewServiceId)}" data-plan="${Number(p.id) || 0}"`
          : `data-checkout-buy="${Number(p.id) || 0}"`;
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
        <span class="badge">${customer.wallet_pay_enabled ? "پرداخت فعال" : "شارژ / سایر روش‌ها"}</span>
      </div>
      <p class="wallet-label">موجودی</p>
      <p class="wallet-value">${esc(money(customer.wallet))}</p>
      <div class="wallet-actions">
        <button type="button" class="btn btn-ghost sm" data-goto="wallet">تراکنش‌ها</button>
        <button type="button" class="btn sm" data-goto="shop">خرید سرویس</button>
        <button type="button" class="btn btn-ghost sm" data-topup="1">شارژ</button>
      </div>
      ${
        customer.emergency_credit_debt
          ? `<p class="hint">بدهی اعتبار اضطراری: ${esc(money(customer.emergency_credit_debt))}</p>`
          : ""
      }
    </div>`;
  }

  function methodButtonsHtml(methods, prefix) {
    const list = methods || [];
    if (!list.length) return '<p class="muted">روش پرداختی فعال نیست</p>';
    return (
      '<div class="row-actions">' +
      list
        .map(
          (m) =>
            `<button type="button" class="btn sm" data-pay-method="${esc(prefix)}" data-method="${esc(
              m.id
            )}">${esc(m.label)}</button>`
        )
        .join("") +
      "</div>"
    );
  }

  function renderUserPanels(data) {
    const c = data.customer || {};
    const services = c.services || [];
    const homeSvcs = services.slice(0, 4);
    const suggest = c.renew_suggest;
    return {
      home: `
        ${walletCardHtml(c)}
        ${
          suggest
            ? `<div class="card">
                <h3>پیشنهاد تمدید</h3>
                <p class="hint">بر اساس مصرف شما: ${esc(suggest.plan_name)} — ${esc(
                  money(suggest.price)
                )}</p>
                <button type="button" class="btn sm" data-checkout-renew-plan="${Number(
                  suggest.service_id
                )}" data-plan="${Number(suggest.plan_id)}">تمدید پیشنهادی</button>
              </div>`
            : ""
        }
        <div class="card">
          <div class="card-head">
            <h3>سرویس‌های من</h3>
            <button type="button" class="btn btn-ghost sm" data-goto="services">همه</button>
          </div>
          ${
            homeSvcs.length
              ? homeSvcs.map((s) => servicePeekHtml(s)).join("")
              : '<p class="muted">سرویسی ندارید — از بخش خرید شروع کنید.</p>'
          }
        </div>`,
      services: `
        <div class="card"><h3>سرویس‌ها</h3>
          <p class="hint">لینک، QR، تست اتصال، تمدید و توقف زیر هر سرویس</p>
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
          <p class="hint">همه روش‌های پرداخت فعال داخل مینی‌اپ در دسترس است.</p>
          ${plansHtml(c.plans)}
        </div>`,
      wallet: `
        ${walletCardHtml(c)}
        <div class="card">
          <h3>شارژ / کد هدیه</h3>
          <div class="row-actions">
            <button type="button" class="btn sm" data-topup="1">شارژ کیف پول</button>
            <button type="button" class="btn btn-ghost sm" data-redeem="1">کد شارژ</button>
            <button type="button" class="btn btn-ghost sm" data-emergency="1">اعتبار اضطراری</button>
          </div>
          <div class="pay-sheet" data-wallet-sheet hidden></div>
        </div>
        <div class="card">
          <h3>تراکنش‌ها</h3>
          ${activityHtml(c.activity)}
        </div>`,
      support: `
        <div class="card">
          <h3>پشتیبانی</h3>
          <p class="hint">قبل از تیکت، وضعیت سرویس‌ها بررسی می‌شود.</p>
          <label class="form-field">موضوع
            <input type="text" id="ticket-subject" maxlength="120" placeholder="مثلاً قطعی اتصال" />
          </label>
          <label class="form-field">متن
            <textarea id="ticket-body" maxlength="2000" placeholder="مشکل را کوتاه بنویسید…"></textarea>
          </label>
          <div class="row-actions">
            <button type="button" class="btn btn-ghost sm" data-diagnose="1">تشخیص خودکار</button>
            <button type="button" class="btn sm" data-ticket-send="1">ارسال تیکت</button>
          </div>
          <div id="diagnose-box" class="hint"></div>
        </div>
        <div class="card">
          <h3>تیکت‌های من</h3>
          <div id="my-tickets"><p class="muted">در حال بارگذاری…</p></div>
        </div>`,
    };
  }

  function renderOpsShell(data) {
    const ops = data.ops || {};
    const coachingSlot =
      data.persona === "reseller"
        ? `<div class="card" id="coaching-card"><p class="muted">در حال بارگذاری کوچینگ…</p></div>`
        : "";
    return `
      <div class="card">
        <h3>${data.persona === "admin" ? "عملیات پلتفرم" : "عملیات فروشگاه"}</h3>
        ${statsHtml(ops.stats || {}, [
          ["users", "کاربران"],
          ["orders", "سفارش‌ها"],
          ["pending", "رسید معلق"],
          ["tickets", "تیکت باز"],
          ["revenue", "درآمد"],
        ].filter(([k]) => (ops.stats || {})[k] != null || k !== "revenue"))}
      </div>
      ${coachingSlot}
      <div class="card">
        <div class="section-tabs" id="ops-tabs">
          <button type="button" data-ops-tab="pending" class="active">رسیدها</button>
          <button type="button" data-ops-tab="customers">مشتریان</button>
          <button type="button" data-ops-tab="tickets">تیکت‌ها</button>
        </div>
        <div id="ops-body"><p class="muted">در حال بارگذاری…</p></div>
      </div>
      <div class="card">
        <h3>میانبر پنل وب</h3>
        <p class="hint">تنظیمات سنگین فقط در وب‌پنل</p>
        ${linksHtml(ops.panel_links)}
      </div>`;
  }

  async function ensurePayMethods() {
    if (payMethods) return payMethods;
    payMethods = await api("/api/mini/pay-methods");
    return payMethods;
  }

  async function startCheckout({ planId, serviceId }) {
    if (busy) return;
    if (!(state && state.commerce_allowed)) {
      toast("خرید برای این نقش مجاز نیست", "err");
      return;
    }
    busy = true;
    try {
      const methods = await ensurePayMethods();
      const body = { plan_id: planId };
      if (serviceId) body.service_id = serviceId;
      const order = await api("/api/mini/order", { method: "POST", body });
      const hostSel = serviceId
        ? `[data-pay-host="${serviceId}"]`
        : "[data-shop-pay-host]";
      let host = document.querySelector(hostSel);
      if (!host) {
        const shop = document.querySelector('[data-view="shop"] .card');
        if (shop) {
          host = document.createElement("div");
          host.className = "pay-sheet";
          host.setAttribute("data-shop-pay-host", "1");
          shop.appendChild(host);
        }
      }
      if (!host) {
        toast("سفارش ساخته شد — روش پرداخت را از کیف پول ادامه دهید", "ok");
        return;
      }
      host.hidden = false;
      host.dataset.orderId = String(order.order_id);
      host.innerHTML =
        `<div class="flash ok">سفارش #${esc(order.order_id)} — ${esc(money(order.amount))}</div>` +
        "<p class=\"hint\">روش پرداخت را انتخاب کنید</p>" +
        methodButtonsHtml(methods.methods, "order:" + order.order_id);
      bindActions(host);
    } catch (e) {
      toast(String(e.message || e), "err");
    } finally {
      busy = false;
    }
  }

  async function payOrder(orderId, method) {
    if (busy) return;
    busy = true;
    try {
      const res = await api("/api/mini/order/" + orderId + "/pay", {
        method: "POST",
        body: { method },
      });
      if (method === "wallet") {
        toast(res.message || "پرداخت شد", "ok");
        if (res.onboarding && res.onboarding.length) {
          showOnboarding(res.onboarding);
        }
        await reload();
        setView("services");
        return;
      }
      if (method === "psp" && res.checkout_url) {
        const url = safeUrl(res.checkout_url);
        if (url && tg && tg.openLink) tg.openLink(url);
        else if (url) window.open(url, "_blank");
        toast(res.message || "درگاه باز شد", "ok");
        return;
      }
      showReceiptUpload(res.payment_id, res);
    } catch (e) {
      toast(String(e.message || e), "err");
    } finally {
      busy = false;
    }
  }

  function showReceiptUpload(paymentId, meta) {
    const box = document.createElement("div");
    box.className = "card";
    box.innerHTML = `
      <h3>آپلود رسید</h3>
      ${
        meta && meta.card && meta.card.number
          ? `<p class="hint">کارت: <span dir="ltr">${esc(meta.card.number)}</span> — ${esc(
              meta.card.holder || ""
            )}</p>`
          : ""
      }
      ${meta && meta.gateway_text ? `<p class="hint">${esc(meta.gateway_text)}</p>` : ""}
      <p class="hint">${esc(meta && meta.message ? meta.message : "رسید را انتخاب کنید")}</p>
      <label class="form-field">تصویر رسید
        <input type="file" accept="image/*" id="receipt-file" />
      </label>
      <button type="button" class="btn sm" id="receipt-send">ارسال رسید</button>`;
    root.prepend(box);
    box.querySelector("#receipt-send").addEventListener("click", async () => {
      const input = box.querySelector("#receipt-file");
      if (!input.files || !input.files[0]) {
        toast("فایل انتخاب نشده", "err");
        return;
      }
      const fd = new FormData();
      fd.append("file", input.files[0]);
      try {
        const res = await api("/api/mini/payment/" + paymentId + "/receipt", {
          method: "POST",
          formData: fd,
        });
        toast(res.message || "رسید ثبت شد", "ok");
        box.remove();
        await reload();
      } catch (e) {
        toast(String(e.message || e), "err");
      }
    });
  }

  function showOnboarding(steps) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML =
      "<h3>راهنمای شروع</h3>" +
      (steps || [])
        .map(
          (s, i) =>
            `<div class="ops-row"><div><strong>${esc(i + 1)}. ${esc(s.title)}</strong>
            <div class="muted">${esc(s.body)}</div></div></div>`
        )
        .join("");
    root.prepend(card);
  }

  async function loadMyTickets() {
    const box = document.getElementById("my-tickets");
    if (!box) return;
    try {
      const data = await api("/api/mini/tickets");
      const rows = data.tickets || [];
      box.innerHTML = rows.length
        ? rows
            .map(
              (t) =>
                `<div class="ops-row"><div><strong>#${esc(t.id)} ${esc(
                  t.subject
                )}</strong><div class="muted">${esc(t.status)}</div></div></div>`
            )
            .join("")
        : '<p class="muted">تیکتی ندارید</p>';
    } catch (e) {
      box.innerHTML = `<p class="muted">${esc(e.message || e)}</p>`;
    }
  }

  async function loadOpsBody() {
    const body = document.getElementById("ops-body");
    if (!body) return;
    body.innerHTML = '<p class="muted">در حال بارگذاری…</p>';
    try {
      if (opsTab === "pending") {
        const data = await api("/api/mini/ops/pending-payments");
        const rows = data.payments || [];
        body.innerHTML = rows.length
          ? rows
              .map(
                (p) => `<div class="ops-row">
                  <div>
                    <strong>#${esc(p.id)} — ${esc(money(p.amount))}</strong>
                    <div class="muted">${esc(p.user_name)} · ${esc(p.method)}${
                  p.is_wallet_topup ? " · شارژ کیف" : ""
                }</div>
                  </div>
                  <div class="row-actions">
                    <button type="button" class="btn btn-ok sm" data-ops-review="${p.id}" data-action="approve">تأیید</button>
                    <button type="button" class="btn btn-danger sm" data-ops-review="${p.id}" data-action="reject">رد</button>
                  </div>
                </div>`
              )
              .join("")
          : '<p class="muted">رسید معلقی نیست</p>';
      } else if (opsTab === "customers") {
        body.innerHTML = `
          <label class="form-field">جستجو
            <input type="search" id="ops-customer-q" placeholder="نام / آیدی" />
          </label>
          <button type="button" class="btn sm" data-ops-search="1">جستجو</button>
          <div id="ops-customer-list" style="margin-top:12px"></div>`;
      } else if (opsTab === "tickets") {
        const data = await api("/api/mini/ops/tickets");
        const rows = data.tickets || [];
        body.innerHTML = rows.length
          ? rows
              .map(
                (t) => `<div class="ops-row">
                  <div><strong>#${esc(t.id)} ${esc(t.subject)}</strong></div>
                  <button type="button" class="btn sm" data-ops-ticket="${t.id}">پاسخ</button>
                </div>`
              )
              .join("")
          : '<p class="muted">تیکت بازی نیست</p>';
      }
      bindActions(body);
    } catch (e) {
      body.innerHTML = `<p class="flash err">${esc(e.message || e)}</p>`;
    }
  }

  async function loadCoaching() {
    const card = document.getElementById("coaching-card");
    if (!card) return;
    try {
      const data = await api("/api/mini/ops/coaching");
      card.innerHTML =
        "<h3>کوچینگ فروش</h3>" +
        statsHtml(data, [
          ["pending_receipts", "رسید معلق"],
          ["open_tickets", "تیکت باز"],
          ["delivered_orders", "تحویل‌شده"],
        ]) +
        "<ul class=\"hint\" style=\"padding-right:18px;margin:12px 0 0\">" +
        (data.tips || []).map((t) => `<li>${esc(t)}</li>`).join("") +
        "</ul>";
    } catch (e) {
      card.innerHTML = `<p class="muted">${esc(e.message || e)}</p>`;
    }
  }

  function bindActions(scope) {
    scope.querySelectorAll("[data-path]").forEach((btn) => {
      btn.addEventListener("click", () => openPanelPath(btn.getAttribute("data-path")));
    });
    scope.querySelectorAll("[data-goto]").forEach((btn) => {
      btn.addEventListener("click", () => {
        setView(btn.getAttribute("data-goto"));
        if (btn.getAttribute("data-goto") === "support") loadMyTickets();
      });
    });
    scope.querySelectorAll("[data-copy]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const ok = await copyText(btn.getAttribute("data-copy"));
        if (ok) markCopied(btn);
        else toast("کپی نشد", "err");
      });
    });
    scope.querySelectorAll("[data-qr]").forEach((btn) => {
      btn.addEventListener("click", () => showQr(Number(btn.getAttribute("data-qr"))));
    });
    scope.querySelectorAll("[data-health]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          const data = await api("/api/mini/service/" + btn.getAttribute("data-health") + "/health");
          toast(
            data.healthy ? "اتصال سالم به‌نظر می‌رسد" : (data.tips || []).join(" · ") || "نیاز به بررسی",
            data.healthy ? "ok" : "err"
          );
        } catch (e) {
          toast(String(e.message || e), "err");
        }
      });
    });
    scope.querySelectorAll("[data-checkout-buy]").forEach((btn) => {
      btn.addEventListener("click", () =>
        startCheckout({ planId: Number(btn.getAttribute("data-checkout-buy")) })
      );
    });
    scope.querySelectorAll("[data-checkout-renew]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const sid = Number(btn.getAttribute("data-checkout-renew"));
        const host = document.querySelector(`[data-pay-host="${sid}"]`);
        if (!host) return;
        if (!host.hidden) {
          host.hidden = true;
          host.innerHTML = "";
          return;
        }
        const c = (state && state.customer) || {};
        host.hidden = false;
        host.innerHTML =
          "<h3 style='margin:0 0 12px;font-size:13px'>انتخاب پلن تمدید</h3>" +
          plansHtml(
            (c.plans || []).filter((p) => !p.is_trial),
            { renewServiceId: sid }
          );
        bindActions(host);
      });
    });
    scope.querySelectorAll("[data-checkout-renew-plan]").forEach((btn) => {
      btn.addEventListener("click", () =>
        startCheckout({
          serviceId: Number(btn.getAttribute("data-checkout-renew-plan")),
          planId: Number(btn.getAttribute("data-plan")),
        })
      );
    });
    scope.querySelectorAll("[data-pay-method]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const prefix = btn.getAttribute("data-pay-method") || "";
        const method = btn.getAttribute("data-method");
        if (prefix.startsWith("order:")) {
          payOrder(Number(prefix.slice(6)), method);
        } else if (prefix.startsWith("topup:")) {
          // amount encoded after topup:
          const amount = Number(prefix.slice(6));
          doTopup(amount, method);
        }
      });
    });
    scope.querySelectorAll("[data-auto-renew]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const sid = Number(btn.getAttribute("data-auto-renew"));
        const svc = ((state.customer && state.customer.services) || []).find(
          (s) => Number(s.id) === sid
        );
        const enabled = !(svc && svc.auto_renew_enabled);
        try {
          const body = { enabled };
          if (enabled) body.plan_id = (svc && (svc.auto_renew_plan_id || svc.plan_id)) || null;
          const res = await api("/api/mini/service/" + sid + "/auto-renew", {
            method: "POST",
            body,
          });
          toast(res.auto_renew_enabled ? "تمدید خودکار فعال شد" : "تمدید خودکار خاموش شد", "ok");
          await reload();
        } catch (e) {
          toast(String(e.message || e), "err");
        }
      });
    });
    scope.querySelectorAll("[data-pause]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const sid = Number(btn.getAttribute("data-pause"));
        const paused = btn.getAttribute("data-paused") === "1";
        try {
          const res = await api("/api/mini/service/" + sid + "/pause", {
            method: "POST",
            body: { pause: !paused },
          });
          toast(res.message || "انجام شد", "ok");
          await reload();
        } catch (e) {
          toast(String(e.message || e), "err");
        }
      });
    });
    scope.querySelectorAll("[data-topup]").forEach((btn) => {
      btn.addEventListener("click", () => showTopupSheet());
    });
    scope.querySelectorAll("[data-redeem]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const code = window.prompt("کد شارژ را وارد کنید");
        if (!code) return;
        try {
          const res = await api("/api/mini/wallet/redeem", {
            method: "POST",
            body: { code },
          });
          toast(res.message || "اعمال شد", "ok");
          await reload();
        } catch (e) {
          toast(String(e.message || e), "err");
        }
      });
    });
    scope.querySelectorAll("[data-emergency]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          const res = await api("/api/mini/emergency-credit", { method: "POST", body: {} });
          toast("اعتبار " + money(res.credited) + " اضافه شد", "ok");
          await reload();
        } catch (e) {
          toast(String(e.message || e), "err");
        }
      });
    });
    scope.querySelectorAll("[data-diagnose]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          const res = await api("/api/mini/tickets", {
            method: "POST",
            body: { diagnose_only: true, body: "تشخیص", subject: "تشخیص" },
          });
          const box = document.getElementById("diagnose-box");
          if (box) {
            box.innerHTML = (res.diagnose && res.diagnose.tips && res.diagnose.tips.length)
              ? res.diagnose.tips.map((t) => `<div>• ${esc(t)}</div>`).join("")
              : "مشکل واضحی در سرویس‌ها دیده نشد.";
          }
        } catch (e) {
          toast(String(e.message || e), "err");
        }
      });
    });
    scope.querySelectorAll("[data-ticket-send]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const subject = (document.getElementById("ticket-subject") || {}).value || "";
        const body = (document.getElementById("ticket-body") || {}).value || "";
        try {
          const res = await api("/api/mini/tickets", {
            method: "POST",
            body: { subject, body },
          });
          toast(res.message || "ثبت شد", "ok");
          loadMyTickets();
        } catch (e) {
          toast(String(e.message || e), "err");
        }
      });
    });
    scope.querySelectorAll("[data-ops-tab]").forEach((btn) => {
      btn.addEventListener("click", () => {
        opsTab = btn.getAttribute("data-ops-tab") || "pending";
        scope.querySelectorAll("[data-ops-tab]").forEach((b) => {
          b.classList.toggle("active", b === btn);
        });
        loadOpsBody();
      });
    });
    scope.querySelectorAll("[data-ops-review]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          const res = await api(
            "/api/mini/ops/payments/" + btn.getAttribute("data-ops-review") + "/review",
            {
              method: "POST",
              body: { action: btn.getAttribute("data-action") },
            }
          );
          toast(res.message || "انجام شد", "ok");
          loadOpsBody();
        } catch (e) {
          toast(String(e.message || e), "err");
        }
      });
    });
    scope.querySelectorAll("[data-ops-search]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const q = (document.getElementById("ops-customer-q") || {}).value || "";
        const list = document.getElementById("ops-customer-list");
        if (!list) return;
        try {
          const data = await api("/api/mini/ops/customers?q=" + encodeURIComponent(q));
          const rows = data.customers || [];
          list.innerHTML = rows.length
            ? rows
                .map(
                  (c) => `<div class="ops-row">
                    <div><strong>${esc(c.name)}</strong><div class="muted" dir="ltr">${esc(
                    c.telegram_id
                  )}</div></div>
                    <button type="button" class="btn sm" data-ops-renew="${c.id}">تمدید سریع</button>
                  </div>`
                )
                .join("")
            : '<p class="muted">نتیجه‌ای نیست</p>';
          bindActions(list);
        } catch (e) {
          list.innerHTML = `<p class="flash err">${esc(e.message || e)}</p>`;
        }
      });
    });
    scope.querySelectorAll("[data-ops-renew]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          const res = await api(
            "/api/mini/ops/customers/" + btn.getAttribute("data-ops-renew") + "/quick-renew",
            { method: "POST", body: {} }
          );
          toast(res.message || "تمدید شد", "ok");
        } catch (e) {
          toast(String(e.message || e), "err");
        }
      });
    });
    scope.querySelectorAll("[data-ops-ticket]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const text = window.prompt("پاسخ تیکت");
        if (!text) return;
        try {
          const res = await api(
            "/api/mini/ops/tickets/" + btn.getAttribute("data-ops-ticket") + "/reply",
            { method: "POST", body: { body: text } }
          );
          toast(res.message || "ثبت شد", "ok");
          loadOpsBody();
        } catch (e) {
          toast(String(e.message || e), "err");
        }
      });
    });
  }

  async function showTopupSheet() {
    const host =
      document.querySelector("[data-wallet-sheet]") ||
      document.querySelector('[data-view="wallet"] .card');
    if (!host) return;
    const sheet = document.querySelector("[data-wallet-sheet]") || host;
    sheet.hidden = false;
    const methods = await ensurePayMethods();
    const nonWallet = (methods.methods || []).filter((m) => m.id !== "wallet");
    sheet.innerHTML = `
      <label class="form-field">مبلغ شارژ (تومان)
        <input type="number" id="topup-amount" min="1000" step="1000" placeholder="۵۰۰۰۰" />
      </label>
      <p class="hint">روش شارژ:</p>
      <div class="row-actions" id="topup-methods"></div>`;
    const wrap = sheet.querySelector("#topup-methods");
    nonWallet.forEach((m) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn sm";
      b.textContent = m.label;
      b.addEventListener("click", () => {
        const amount = Number((document.getElementById("topup-amount") || {}).value || 0);
        doTopup(amount, m.id);
      });
      wrap.appendChild(b);
    });
  }

  async function doTopup(amount, method) {
    if (busy) return;
    busy = true;
    try {
      const res = await api("/api/mini/wallet/topup", {
        method: "POST",
        body: { amount, method },
      });
      if (method === "psp" && res.checkout_url) {
        const url = safeUrl(res.checkout_url);
        if (url && tg && tg.openLink) tg.openLink(url);
        else if (url) window.open(url, "_blank");
        toast("درگاه شارژ باز شد", "ok");
        return;
      }
      showReceiptUpload(res.payment_id, res);
    } catch (e) {
      toast(String(e.message || e), "err");
    } finally {
      busy = false;
    }
  }

  async function showQr(serviceId) {
    const box = document.querySelector(`[data-qr-box="${serviceId}"]`);
    const trigger = document.querySelector(`[data-qr="${serviceId}"]`);
    if (!box) return;
    if (!box.hidden && box.querySelector("img")) {
      box.hidden = true;
      box.textContent = "";
      if (trigger) trigger.textContent = "نمایش QR";
      return;
    }
    box.hidden = false;
    box.textContent = "";
    box.appendChild(document.createTextNode("در حال ساخت QR…"));
    try {
      const data = await api("/api/mini/service/" + serviceId + "/qr");
      box.textContent = "";
      const img = document.createElement("img");
      img.alt = "QR";
      img.src = "data:image/png;base64," + data.png_base64;
      box.appendChild(img);
      if (trigger) trigger.textContent = "بستن QR";
    } catch (e) {
      box.textContent = "";
      box.appendChild(document.createTextNode(String(e.message || e)));
    }
  }

  function mount(data) {
    currency = data.currency || "تومان";
    state = data;
    payMethods = null;
    const name = (data.user && data.user.name) || "";
    helloEl.textContent = name ? "سلام " + name : "مینی‌اپ";
    roleEl.textContent = personaLabel(data.persona);
    subEl.textContent =
      data.persona === "admin"
        ? "عملیات پلتفرم — بدون خرید"
        : data.persona === "reseller"
          ? "خرید کامل + عملیات فروشگاه"
          : "خرید، تمدید، کیف پول و پشتیبانی";

    renderNav(data.nav);

    const panels = {};
    if (data.persona === "admin") {
      panels.home = `
        <div class="card">
          <h3>پنل سریع ادمین</h3>
          <p class="hint">خرید و کیف پول کاربر برای ادمین در مینی‌اپ فعال نیست.</p>
        </div>
        ${renderOpsShell(data)}`;
      panels.ops = renderOpsShell(data);
    } else {
      Object.assign(panels, renderUserPanels(data));
      if (data.persona === "reseller") {
        panels.ops = renderOpsShell(data);
        const ops = data.ops || {};
        const svcs = (data.customer && data.customer.services) || [];
        panels.home = `
          ${walletCardHtml(data.customer || {})}
          <div class="card">
            <div class="card-head">
              <h3>فروشگاه من</h3>
              <button type="button" class="btn btn-ghost sm" data-goto="ops">عملیات</button>
            </div>
            ${statsHtml(ops.stats || {}, [
              ["users", "کاربران"],
              ["pending", "رسید معلق"],
              ["tickets", "تیکت باز"],
            ])}
          </div>
          <div class="card">
            <div class="card-head"><h3>سرویس‌های من</h3>
              <button type="button" class="btn btn-ghost sm" data-goto="services">همه</button>
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
      .map((id) => `<section class="panel" data-view="${esc(id)}">${panels[id]}</section>`)
      .join("");

    bindActions(root);

    if (data.persona === "admin" || data.persona === "reseller") {
      loadOpsBody();
      if (data.persona === "reseller") loadCoaching();
    }

    const wanted = hashView();
    const ok = (data.nav || []).some((n) => n.id === wanted);
    setView(ok ? wanted : (data.nav[0] && data.nav[0].id) || "home");
    if ((ok ? wanted : "home") === "support") loadMyTickets();
  }

  async function reload() {
    const data = await api("/api/mini/me");
    mount(data);
  }

  async function boot() {
    if (!initData) {
      root.innerHTML =
        '<div class="card"><h3>مینی‌اپ</h3><p class="muted">این صفحه را از داخل تلگرام باز کنید.</p></div>';
      return;
    }
    try {
      await reload();
    } catch (e) {
      root.innerHTML = `<div class="card"><h3>خطا</h3><p class="flash err">${esc(
        e.message || e
      )}</p></div>`;
    }
  }

  window.addEventListener("hashchange", () => {
    if (!state) return;
    const wanted = hashView();
    const ok = (state.nav || []).some((n) => n.id === wanted);
    if (ok) {
      setView(wanted);
      if (wanted === "support") loadMyTickets();
    }
  });

  boot();
})();
