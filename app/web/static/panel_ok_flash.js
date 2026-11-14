/* Page-level success flashes: remove after a short delay (same cadence as upload-toast). */
(function (root) {
  'use strict';
  // Keep in sync with the upload success toast lifetime in panel.js.
  var DISMISS_MS = 2800;

  function pageOkFlash(doc) {
    if (!doc || !doc.querySelector) return null;
    return doc.querySelector('.main-body > .flash.ok');
  }

  function stripOkParam(loc, hist) {
    if (!loc || !hist || typeof hist.replaceState !== 'function') return;
    try {
      var u = new URL(loc.href);
      if (!u.searchParams.has('ok')) return;
      u.searchParams.delete('ok');
      var qs = u.searchParams.toString();
      hist.replaceState({}, '', u.pathname + (qs ? '?' + qs : '') + u.hash);
    } catch (_) {
      /* Ignore malformed location in non-browser harnesses. */
    }
  }

  function scheduleDismiss(doc, loc, hist, schedule) {
    var flash = pageOkFlash(doc);
    if (!flash) return null;
    var run = typeof schedule === 'function' ? schedule : root.setTimeout;
    return run(function () {
      if (flash.parentNode) flash.remove();
      stripOkParam(loc, hist);
    }, DISMISS_MS);
  }

  var api = {
    DISMISS_MS: DISMISS_MS,
    pageOkFlash: pageOkFlash,
    stripOkParam: stripOkParam,
    scheduleDismiss: scheduleDismiss,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    root.PGClockPanelOkFlash = api;
    function boot() {
      scheduleDismiss(root.document, root.location, root.history, function (fn, ms) {
        return root.setTimeout(fn, ms);
      });
    }
    if (root.document && root.document.readyState === 'loading') {
      root.document.addEventListener('DOMContentLoaded', boot);
    } else {
      boot();
    }
  }
})(typeof window !== 'undefined' ? window : globalThis);
