/* After «راه‌اندازی مجدد پنل»: wait until the panel answers from a new process, then reload the page.
   The page carries the boot id it was restarted from (data-restart-wait); /health/detail reports the
   current one. Until the new process answers, the request fails or answers the old id, and the page keeps
   waiting, up to WAIT_LIMIT_MS. */
(function () {
  'use strict';

  var WAIT_LIMIT_MS = 120000;
  var POLL_MS = 1500;

  function restartFinished(health, preBoot) {
    return Boolean(health && health.boot_id && preBoot && String(health.boot_id) !== String(preBoot));
  }

  function nextUrl(href, finished) {
    var url = new URL(href);
    url.searchParams.delete('restarting');
    url.searchParams.set(finished ? 'ok' : 'err',
      finished ? 'پنل دوباره روشن شد' : 'پنل هنوز پاسخ نداده است؛ چند لحظهٔ دیگر صفحه را تازه کنید');
    return url.toString();
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { restartFinished: restartFinished, nextUrl: nextUrl };
    return;
  }

  var note = document.querySelector('[data-restart-wait]');
  if (!note) return;
  var preBoot = note.getAttribute('data-restart-wait');
  var started = Date.now();

  function readHealth() {
    return fetch('/health/detail', { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } })
      .then(function (res) { return res.ok ? res.json() : null; })
      .catch(function () { return null; });
  }

  function tick() {
    readHealth().then(function (health) {
      if (restartFinished(health, preBoot)) {
        window.location.replace(nextUrl(window.location.href, true));
      } else if (Date.now() - started > WAIT_LIMIT_MS) {
        window.location.replace(nextUrl(window.location.href, false));
      } else {
        window.setTimeout(tick, POLL_MS);
      }
    });
  }

  window.setTimeout(tick, POLL_MS);
})();
