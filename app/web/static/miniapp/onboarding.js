/* Mini App onboarding views: referral code, required channels and entry terms.
 *
 * Someone who opens the Mini App from Telegram search has never seen the bot's chat, so the
 * steps the bot asks for there are shown here. Pure functions: no DOM, no network; every
 * value from the server is escaped on insertion.
 */
(function (root) {
  "use strict";

  const core = root.PGMini.core;
  const { esc } = core;
  const R = root.PGMini.render;

  // Matches the server's channel states (``app.services.miniapp_onboarding``).
  const CHANNEL_BADGES = {
    joined: ["عضو هستید", "ok"],
    missing: ["عضو نیستید", "warn"],
    unverified: ["قابل بررسی نیست", "danger"],
  };

  function errorLine(message) {
    return message ? `<p class="error-box" role="alert">${esc(message)}</p>` : "";
  }

  function referralHtml(step, error, code) {
    return `<section class="card" aria-labelledby="onboarding-title">
      <h1 id="onboarding-title" class="card-title">ثبت‌نام با کد معرف</h1>
      <p class="pre-line">${esc(step.text)}</p>
      <form class="code-form" data-form="onboarding-referral">
        <label class="form-field"><span>کد معرف</span>
          <input type="text" name="code" maxlength="64" autocomplete="off" dir="ltr" autocapitalize="characters" required value="${esc(code)}"></label>
        ${errorLine(error)}
        <button type="submit" class="btn block">ثبت‌نام</button>
      </form>
    </section>`;
  }

  function channelRowHtml(channel, index) {
    const tone = CHANNEL_BADGES[channel.status] || CHANNEL_BADGES.missing;
    const label = `<span class="list-row-label">${esc(channel.label)}</span>${R.badge(tone[0], tone[1])}`;
    if (!channel.url) {
      // The admin saved a private channel without its invite link: nothing to open.
      return `<div class="list-row">${label}<span class="muted sm">لینک عضویت تنظیم نشده</span></div>`;
    }
    return `<button type="button" class="list-row" data-action="onboarding-channel" data-index="${Number(index) || 0}">
        ${label}${R.icon("chevron", 16)}</button>`;
  }

  function forceJoinHtml(step, error) {
    const channels = Array.isArray(step.channels) ? step.channels : [];
    return `<section class="card" aria-labelledby="onboarding-title">
        <h1 id="onboarding-title" class="card-title">عضویت در کانال‌ها</h1>
        <p class="pre-line">${esc(step.text)}</p>
      </section>
      <section class="card list-card" aria-label="کانال‌ها">${channels.map(channelRowHtml).join("")}</section>
      ${errorLine(error)}
      <button type="button" class="btn block" data-action="onboarding-join-check">${esc(step.check_label || "عضو شدم")}</button>`;
  }

  function termsHtml(step, error) {
    return `<section class="card" aria-labelledby="onboarding-title">
        <h1 id="onboarding-title" class="card-title">قوانین</h1>
        <p class="pre-line">${esc(step.text)}</p>
      </section>
      ${errorLine(error)}
      <button type="button" class="btn block" data-action="onboarding-terms">${esc(step.button || "موافقم")}</button>`;
  }

  const STEP_VIEWS = { referral: referralHtml, force_join: forceJoinHtml, terms: termsHtml };

  function isStep(step) {
    return !!step && Object.prototype.hasOwnProperty.call(STEP_VIEWS, step.step);
  }

  /** The screen for one onboarding step; ``error`` is a message from the last attempt, ``code`` the typed referral code. */
  function stepHtml(step, error, code) {
    return isStep(step) ? STEP_VIEWS[step.step](step, error || "", code || "") : "";
  }

  /** The join link of the channel at ``index``, only when it is a Telegram link. */
  function channelUrl(step, index) {
    const channels = step && Array.isArray(step.channels) ? step.channels : [];
    const channel = channels[Number(index)];
    const url = channel ? String(channel.url || "") : "";
    return /^https:\/\/t\.me\/[^\s]+$/i.test(url) ? url : "";
  }

  const VPN_HINT = "اگر همین حالا داخل تلگرام هستید و فقط از پروکسی داخل تلگرام استفاده می‌کنید، "
    + "VPN سیستم را روشن کنید و مینی‌اپ را دوباره باز کنید.";

  /**
   * Opened in a browser: say so, and open the same Mini App in Telegram when the bot is known.
   * ``sdkBlocked``: telegram.org did not load, so a Telegram window behind a proxy looks the same.
   */
  function outsideTelegramHtml(botUsername, sdkBlocked) {
    const name = /^[A-Za-z][A-Za-z0-9_]{3,31}$/.test(String(botUsername || "")) ? botUsername : "";
    const link = name ? `https://t.me/${name}?startapp` : "";
    return `<div class="error-box" role="alert">
        <p>این صفحه فقط داخل تلگرام کار می‌کند؛ ورود شما را خود تلگرام تأیید می‌کند.</p>
        ${link
          ? `<a class="btn sm" href="${esc(link)}" rel="noopener">باز کردن در تلگرام</a>
             <p class="muted sm" dir="ltr">@${esc(name)}</p>`
          : "<p>مینی‌اپ را از دکمهٔ «Open» ربات در تلگرام باز کنید.</p>"}
        ${sdkBlocked ? `<p class="muted sm">${esc(VPN_HINT)}</p>` : ""}
      </div>`;
  }

  root.PGMini.onboarding = Object.freeze({ stepHtml, isStep, channelUrl, outsideTelegramHtml });
})(typeof window === "object" ? window : globalThis);
