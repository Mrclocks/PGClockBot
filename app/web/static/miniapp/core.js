/* Mini App core: Telegram WebApp bridge, API client, escaping and formatting. */
(function (root) {
  "use strict";

  const settings = { currency: "تومان" };

  /** The Telegram WebApp object, read live: the SDK loads async and may arrive late or never. */
  function webApp() {
    return (root.Telegram && root.Telegram.WebApp) || null;
  }

  function launch() {
    return root.PGMiniLaunch || null;
  }

  // Read before the page rewrites the address (launch.js does the same even earlier).
  const hashInitData = (() => {
    const l = launch();
    if (l && l.initData) return l.initData;
    const raw = String((root.location && root.location.hash) || "").replace(/^#/, "");
    const at = raw.indexOf("tgWebAppData=");
    if (at < 0) return "";
    try {
      return new URLSearchParams(raw.slice(at)).get("tgWebAppData") || "";
    } catch (_) {
      return "";
    }
  })();

  // "pending" until the SDK script loads or fails; without launch.js its fate is unknown.
  let sdkState = launch() ? "pending" : (webApp() ? "ready" : "unknown");
  if (launch()) {
    Promise.resolve(launch().sdk).then((ok) => { sdkState = ok ? "ready" : "failed"; }, () => { sdkState = "failed"; });
  }

  function sdkPending() {
    return sdkState === "pending";
  }

  function sdkFailed() {
    return sdkState === "failed" && !webApp();
  }

  /** Resolves when the SDK has loaded or failed, or after ``ms``, whichever is first. */
  function whenTelegram(ms) {
    const l = launch();
    if (!l || !sdkPending()) return Promise.resolve(!!webApp());
    return Promise.race([
      Promise.resolve(l.sdk).then((ok) => !!ok, () => false),
      new Promise((resolve) => root.setTimeout(() => resolve(!!webApp()), ms)),
    ]);
  }

  /** The SDK's initData, else the one Telegram put in the address. */
  function getInitData() {
    const w = webApp();
    return (w && w.initData) || hashInitData;
  }

  // A shop's Mini App lives at /miniapp/s/<key>/; the key names the shop, and the server
  // still verifies every request with that shop bot's own signature.
  const SHOP_PATH = /^\/miniapp\/s\/([A-Za-z0-9_-]{16,32})\/?$/;

  /** The shop key from this page's address, or "" for the platform Mini App. */
  function shopKey() {
    const match = SHOP_PATH.exec((root.location && root.location.pathname) || "");
    return match ? match[1] : "";
  }

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  /** Only absolute http(s) links may be opened; everything else is dropped. */
  function safeUrl(value) {
    const s = String(value || "").trim();
    return /^https?:\/\/[^\s]+$/i.test(s) ? s : "";
  }

  /** Same-origin relative panel path, never protocol-relative or absolute. */
  function safePanelPath(path) {
    const p = String(path || "");
    if (!p.startsWith("/") || p.startsWith("//") || p.includes("://") || p.includes("\\")) return "";
    return p;
  }

  function num(value) {
    return (Number(value) || 0).toLocaleString("fa-IR");
  }

  function money(value) {
    return num(value).replace(/٬/g, ",") + " " + settings.currency;
  }

  /** Persian/Arabic digits to ASCII, separators and spaces removed; "" when not a whole number. */
  function parseAmount(value) {
    const ascii = String(value || "")
      .replace(/[۰-۹]/g, (d) => String(d.charCodeAt(0) - 0x06f0))
      .replace(/[٠-٩]/g, (d) => String(d.charCodeAt(0) - 0x0660))
      .replace(/[,٬،\s]/g, "");
    return /^\d{1,12}$/.test(ascii) ? Number(ascii) : null;
  }

  function setCurrency(value) {
    settings.currency = String(value || "تومان");
  }

  /** Persian digits in labels the server builds with ASCII digits ("27.9 از 30 گیگ"). */
  function faDigits(value) {
    return String(value == null ? "" : value).replace(/[0-9]/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[d]).replace(/\./g, "٫");
  }

  /** A date in the reader's Persian calendar, without the time. */
  function formatDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (!Number.isFinite(d.getTime())) return "";
    try {
      return new Intl.DateTimeFormat("fa-IR", { dateStyle: "medium" }).format(d);
    } catch (_) {
      return d.toLocaleDateString("fa-IR");
    }
  }

  function formatDateTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (!Number.isFinite(d.getTime())) return "";
    try {
      return new Intl.DateTimeFormat("fa-IR", { dateStyle: "medium", timeStyle: "short" }).format(d);
    } catch (_) {
      return d.toLocaleString("fa-IR");
    }
  }

  const MINUTE_MS = 60 * 1000;
  const RELATIVE_STEPS = [
    [60 * MINUTE_MS, MINUTE_MS, "minute"],
    [24 * 60 * MINUTE_MS, 60 * MINUTE_MS, "hour"],
    [7 * 24 * 60 * MINUTE_MS, 24 * 60 * MINUTE_MS, "day"],
  ];

  /** A past moment as «۵ دقیقه پیش» / «دیروز»; a week or more back, the Persian date. */
  function formatRelative(iso, now) {
    if (!iso) return "";
    const d = new Date(iso);
    if (!Number.isFinite(d.getTime())) return "";
    const ago = (Number(now) || Date.now()) - d.getTime();
    if (ago < MINUTE_MS) return "همین حالا";
    const step = RELATIVE_STEPS.find(([limit]) => ago < limit);
    if (!step) return formatDate(iso);
    const amount = Math.floor(ago / step[1]);
    try {
      return new Intl.RelativeTimeFormat("fa", { numeric: "auto" }).format(-amount, step[2]);
    } catch (_) {
      return formatDate(iso);
    }
  }

  function haptic(kind) {
    const tg = webApp();
    if (!tg || !tg.HapticFeedback) return;
    try {
      if (kind === "select") tg.HapticFeedback.selectionChanged();
      else tg.HapticFeedback.notificationOccurred(kind === "err" ? "error" : "success");
    } catch (_) {}
  }

  function toast(message, kind) {
    const doc = root.document;
    if (!doc || !doc.body) return;
    const el = doc.createElement("div");
    el.className = "toast" + (kind === "err" ? " is-error" : "");
    el.setAttribute("role", kind === "err" ? "alert" : "status");
    el.textContent = String(message || "");
    doc.body.appendChild(el);
    root.setTimeout(() => el.remove(), 3200);
    haptic(kind === "err" ? "err" : "ok");
  }

  function confirmAction(message) {
    const tg = webApp();
    if (tg && typeof tg.showConfirm === "function") {
      return new Promise((resolve) => {
        try {
          tg.showConfirm(String(message), (ok) => resolve(!!ok));
        } catch (_) {
          resolve(!!(root.confirm && root.confirm(String(message))));
        }
      });
    }
    return Promise.resolve(!!(root.confirm && root.confirm(String(message))));
  }

  function openExternal(url) {
    const safe = safeUrl(url);
    if (!safe) return false;
    const tg = webApp();
    if (tg && typeof tg.openLink === "function") tg.openLink(safe);
    else root.open(safe, "_blank", "noopener");
    return true;
  }

  /** t.me links open inside Telegram; anything else goes through openExternal. */
  function openTelegramLink(url) {
    const safe = safeUrl(url);
    const tg = webApp();
    if (tg && typeof tg.openTelegramLink === "function" && /^https:\/\/t\.me\//i.test(safe)) {
      tg.openTelegramLink(safe);
      return true;
    }
    return openExternal(safe);
  }

  class ApiError extends Error {
    constructor(message, status, code) {
      super(message);
      this.status = status;
      // Why the server turned the request away (``X-Mini-Error``), e.g. "expired"; "" if unsaid.
      this.code = code || "";
    }
  }

  const ERROR_CODE = /^[a-z_]{1,32}$/;

  /** The server's reason header, only when it is one of its short lowercase codes. */
  function errorCode(res) {
    const raw = res && res.headers && typeof res.headers.get === "function" ? res.headers.get("X-Mini-Error") : "";
    const code = String(raw || "");
    return ERROR_CODE.test(code) ? code : "";
  }

  // Called once when the server says this page's Telegram sign-in no longer works.
  let authLostHandler = null;

  function onAuthLost(handler) {
    authLostHandler = typeof handler === "function" ? handler : null;
  }

  const FALLBACK_ERROR = "ارتباط با سرور برقرار نشد؛ دوباره تلاش کنید.";

  const STATUS_ERRORS = {
    401: "نشست مینی‌اپ منقضی شده؛ مینی‌اپ را ببندید و دوباره باز کنید.",
    403: "این کار برای حساب شما مجاز نیست.",
    404: "مورد درخواستی پیدا نشد.",
    422: "ورودی نامعتبر است.",
  };

  /** Short Persian server text is shown as is; internal (ASCII) details never are. */
  function errorText(data, status) {
    const raw = data && (data.detail || data.message);
    const detail = typeof raw === "string" ? raw.trim() : "";
    if (detail && detail.length <= 200 && !/^[\x00-\x7F]*$/.test(detail)) return detail;
    return STATUS_ERRORS[status] || FALLBACK_ERROR;
  }

  /** ``opts.body`` is sent as JSON; ``opts.raw`` (a Blob) is sent as is, e.g. an image. */
  async function api(path, opts) {
    const options = opts || {};
    const hasBody = options.body !== undefined;
    const raw = options.raw;
    const contentType = raw ? raw.type || "application/octet-stream" : hasBody ? "application/json" : null;
    let res;
    try {
      res = await root.fetch(path, {
        method: options.method || "GET",
        headers: {
          "X-Telegram-Init-Data": getInitData(),
          ...(shopKey() ? { "X-Mini-Shop": shopKey() } : {}),
          ...(contentType ? { "Content-Type": contentType } : {}),
        },
        body: raw || (hasBody ? JSON.stringify(options.body) : undefined),
        cache: "no-store",
        credentials: "same-origin",
      });
    } catch (_) {
      throw new ApiError(FALLBACK_ERROR, 0);
    }
    // Binary responses (receipt photos) come back as a Blob; errors are still JSON.
    if (options.blob && res.ok) return res.blob();
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (_) {
      data = null;
    }
    if (!res.ok) {
      const err = new ApiError(errorText(data, res.status), res.status, errorCode(res));
      if (res.status === 401 && authLostHandler) authLostHandler(err);
      throw err;
    }
    return data;
  }

  async function copyText(text) {
    const s = String(text || "");
    if (!s) return false;
    try {
      if (root.navigator && root.navigator.clipboard && root.navigator.clipboard.writeText) {
        await root.navigator.clipboard.writeText(s);
        return true;
      }
    } catch (_) {}
    const doc = root.document;
    try {
      const ta = doc.createElement("textarea");
      ta.value = s;
      ta.setAttribute("readonly", "");
      ta.className = "visually-hidden";
      doc.body.appendChild(ta);
      ta.select();
      const ok = doc.execCommand("copy");
      ta.remove();
      return !!ok;
    } catch (_) {
      return false;
    }
  }

  /** Flash «کپی شد» on the button, then restore its own label. */
  function markCopied(btn) {
    if (!btn) return;
    if (!btn.dataset.copyIdle) btn.dataset.copyIdle = (btn.textContent || "").trim() || "کپی لینک";
    btn.textContent = "کپی شد";
    btn.classList.add("is-copied");
    if (btn._copyTimer) root.clearTimeout(btn._copyTimer);
    btn._copyTimer = root.setTimeout(() => {
      btn.textContent = btn.dataset.copyIdle;
      btn.classList.remove("is-copied");
    }, 1400);
  }

  /** Follow the Telegram light/dark theme; CSS reads Telegram's --tg-theme-* variables. */
  function applyTheme() {
    const doc = root.document;
    if (!doc || !doc.documentElement) return;
    const tg = webApp();
    const scheme = tg && tg.colorScheme === "light" ? "light" : "dark";
    doc.documentElement.setAttribute("data-theme", scheme);
    if (!tg) return;
    try {
      // The page uses secondary_bg (see miniapp.css), so Telegram's header and overscroll match it.
      tg.setHeaderColor("secondary_bg_color");
      tg.setBackgroundColor("secondary_bg_color");
    } catch (_) {}
  }

  const ACCENT_HEX = /^#[0-9a-f]{6}$/i;
  const INK_DARK = "#1c1007";
  const INK_LIGHT = "#ffffff";

  function luminance(hex) {
    const channel = (offset) => {
      const v = parseInt(hex.slice(offset, offset + 2), 16) / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
  }

  /** Text colour on a solid accent: whichever of dark ink or white reads better (WCAG contrast). */
  function inkFor(hex) {
    const l = luminance(hex);
    const onWhite = 1.05 / (l + 0.05);
    const onDark = (l + 0.05) / (luminance(INK_DARK) + 0.05);
    return onDark >= onWhite ? INK_DARK : INK_LIGHT;
  }

  /** The shop's accent colour on every button and highlight; "" or anything invalid keeps the default. */
  function applyBrand(brand) {
    const doc = root.document;
    const style = doc && doc.documentElement && doc.documentElement.style;
    if (!style || typeof style.setProperty !== "function") return;
    const accent = String((brand && brand.accent) || "");
    if (ACCENT_HEX.test(accent)) {
      style.setProperty("--brand-custom", accent);
      style.setProperty("--accent-ink", inkFor(accent));
    } else {
      style.removeProperty("--brand-custom");
      style.removeProperty("--accent-ink");
    }
  }

  /** Telegram's top insets apply only in fullscreen; CSS reads data-fullscreen. */
  function applyFullscreen() {
    const doc = root.document;
    if (!doc || !doc.documentElement) return;
    const tg = webApp();
    doc.documentElement.setAttribute("data-fullscreen", tg && tg.isFullscreen ? "1" : "0");
  }

  /** Close the Mini App so Telegram signs a fresh session on the next open. */
  function closeApp() {
    const tg = webApp();
    if (tg && typeof tg.close === "function") {
      try {
        tg.close();
        return true;
      } catch (_) {}
    }
    return false;
  }

  let telegramInitialized = false;

  /** Once, as soon as the SDK is there (it may arrive after the first screen). */
  function initTelegram() {
    const tg = webApp();
    if (!tg || telegramInitialized) return;
    telegramInitialized = true;
    try {
      tg.ready();
      tg.expand();
    } catch (_) {}
    try {
      // Pulling down at the top refreshes the screen (pull.js) instead of collapsing the app;
      // the app still closes or collapses from Telegram's own header.
      if (typeof tg.disableVerticalSwipes === "function") tg.disableVerticalSwipes();
    } catch (_) {}
    applyTheme();
    applyFullscreen();
    try {
      tg.onEvent("themeChanged", applyTheme);
      tg.onEvent("fullscreenChanged", applyFullscreen);
    } catch (_) {}
  }

  const core = Object.freeze({
    webApp, getInitData, whenTelegram, sdkPending, sdkFailed, shopKey, esc, safeUrl, safePanelPath, num, money, parseAmount, setCurrency, formatDateTime, formatDate, formatRelative, faDigits,
    haptic, toast, confirmAction, openExternal, openTelegramLink, api, ApiError, onAuthLost, closeApp, copyText, markCopied,
    applyTheme, applyBrand, inkFor, initTelegram,
  });
  root.PGMini = Object.assign(root.PGMini || {}, { core });
})(typeof window === "object" ? window : globalThis);
