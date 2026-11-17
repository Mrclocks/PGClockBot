/* On a wide screen a settings area shows its list as a menu beside every page, so the list page itself opens the
   first page of the area. It runs as the list page loads (before the list is drawn) and replaces the history entry,
   so «back» skips the list. A list that carries a message (?ok= / ?err=) stays, so the message is not lost. */
(function () {
  'use strict';
  var DESKTOP_QUERY = '(min-width: 1200px)';

  // The page to open instead of the list, or null to stay: a same-site path, on a desktop, with no message to show.
  function firstPageTarget(first, isDesktop, search) {
    var href = String(first || '');
    if (!isDesktop || !/^\/(?!\/)/.test(href)) return null;
    var params = new URLSearchParams(String(search || ''));
    if (params.has('ok') || params.has('err')) return null;
    return href;
  }

  if (typeof module === 'object' && module.exports) {
    module.exports = { firstPageTarget: firstPageTarget, DESKTOP_QUERY: DESKTOP_QUERY };
    return;
  }
  var script = document.currentScript;
  var isDesktop = !!(window.matchMedia && window.matchMedia(DESKTOP_QUERY).matches);
  var target = firstPageTarget(script && script.getAttribute('data-settings-first'), isDesktop, window.location.search);
  if (target) window.location.replace(target);
})();
