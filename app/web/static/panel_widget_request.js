/* Time-bound fragment reads, including response bodies and ignored aborts. */
(function (root) {
  'use strict';
  // The server already bounds its PasarGuard reads; the rest of this wait is the
  // transfer over the viewer's own link, which can be slow on mobile networks.
  var READ_TIMEOUT_MS = 15000;
  var FAILURE_TEXT = {
    auth: 'نشست یا دسترسی شما نیاز به بررسی دارد. برای ادامه دوباره وارد شوید.',
    offline: 'اتصال اینترنت شما به وب‌پنل برقرار نشد. اتصال خود را بررسی کنید و دوباره تلاش کنید.',
    slow: 'پاسخ وب‌پنل بیش از حد طول کشید؛ ممکن است اینترنت شما یا سرور کند باشد.',
    server: 'دریافت آمار کامل نشد. سایر بخش‌های وب‌پنل در دسترس هستند.'
  };

  /* Which side failed: the viewer's connection, a slow transfer, the session, or the server. */
  function failureKind(error, online) {
    var message = error && error.message;
    if (message === 'widget-auth') return 'auth';
    if (online === false) return 'offline';
    if (message === 'widget-timeout') return 'slow';
    // fetch() rejects with a TypeError when the request never reached the server.
    if (error && error.name === 'TypeError') return 'offline';
    return 'server';
  }

  function failureMessage(error, online) {
    return FAILURE_TEXT[failureKind(error, online)];
  }

  function fetchRead(url, options, kind) {
    var controller = new AbortController();
    var timer;
    var opts = Object.assign({}, options, { signal: controller.signal });
    var deadline = new Promise(function (_, reject) {
      timer = setTimeout(function () {
        controller.abort();
        reject(new Error('widget-timeout'));
      }, READ_TIMEOUT_MS);
    });
    var read = Promise.resolve().then(function () { return fetch(url, opts); }).then(function (response) {
      if (response.type === 'opaqueredirect' || response.status === 401 || response.status === 403
          || response.status >= 300 && response.status < 400) {
        throw new Error('widget-auth');
      }
      if (!response.ok) throw new Error('widget-response');
      if (kind === 'json' && response.headers && response.headers.get('x-pg-read-status')) {
        throw new Error('widget-unavailable');
      }
      return kind === 'json' ? response.json() : response.text();
    });
    return Promise.race([read, deadline]).finally(function () { clearTimeout(timer); });
  }
  var api = {
    READ_TIMEOUT_MS: READ_TIMEOUT_MS,
    failureKind: failureKind,
    failureMessage: failureMessage,
    fetchBody: function (url, options) { return fetchRead(url, options, 'text'); },
    fetchJson: function (url, options) {
      return fetchRead(url, Object.assign({ redirect: 'manual' }, options), 'json');
    }
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.PGClockPanelWidgetRequest = api;
})(typeof window !== 'undefined' ? window : globalThis);
