/* Mini App launch: keeps Telegram's sign-in from the address and tracks the Telegram SDK.
 *
 * Runs right after the (async) SDK tag, before any other script can rewrite the address.
 * Telegram puts the signed initData in the address (#tgWebAppData=...) on every client. The
 * page reads it from there when the SDK cannot load, e.g. a desktop WebView that bypasses
 * the proxy set inside Telegram. The server verifies the signature either way.
 */
(function (root) {
  "use strict";

  const PARAM = "tgWebAppData";
  const MAX_CHARS = 8192;

  /** initData from a ``#…tgWebAppData=…`` address (route prefixes allowed), or "". */
  function initDataFromHash(hash) {
    const raw = String(hash || "").replace(/^#/, "");
    const at = raw.indexOf(PARAM + "=");
    if (at < 0) return "";
    try {
      const value = new URLSearchParams(raw.slice(at)).get(PARAM) || "";
      return value.length <= MAX_CHARS ? value : "";
    } catch (_) {
      return "";
    }
  }

  /** Resolves true once the SDK is running, false if its script failed to load. */
  function trackSdk(doc) {
    if (root.Telegram && root.Telegram.WebApp) return Promise.resolve(true);
    const tag = doc && typeof doc.getElementById === "function" ? doc.getElementById("tg-sdk") : null;
    if (!tag || typeof tag.addEventListener !== "function") return Promise.resolve(false);
    return new Promise((resolve) => {
      tag.addEventListener("load", () => resolve(!!(root.Telegram && root.Telegram.WebApp)), { once: true });
      tag.addEventListener("error", () => resolve(false), { once: true });
    });
  }

  root.PGMiniLaunch = Object.freeze({
    initData: initDataFromHash(root.location && root.location.hash),
    sdk: trackSdk(root.document),
    initDataFromHash,
  });
})(typeof window === "object" ? window : globalThis);
