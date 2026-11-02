/* Switches between the user-plan and reseller-plan panels without a reload.
   The tab links are real (?tab=resellers), so the page also works without this script. */
(function () {
  'use strict';
  var nav = document.querySelector('[data-plans-tabs]');
  if (!nav) return;
  var tabs = nav.querySelectorAll('[data-plans-tab]');
  var panels = document.querySelectorAll('[data-plans-panel]');

  function show(name, updateUrl) {
    tabs.forEach(function (tab) {
      var on = tab.getAttribute('data-plans-tab') === name;
      tab.classList.toggle('active', on);
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    panels.forEach(function (panel) {
      panel.hidden = panel.getAttribute('data-plans-panel') !== name;
    });
    if (updateUrl && window.history && window.history.replaceState) {
      var url = new URL(window.location.href);
      if (name === 'resellers') url.searchParams.set('tab', 'resellers');
      else url.searchParams.delete('tab');
      url.hash = '';
      window.history.replaceState(null, '', url.pathname + url.search);
    }
  }

  nav.addEventListener('click', function (event) {
    var tab = event.target.closest('[data-plans-tab]');
    if (!tab || event.metaKey || event.ctrlKey || event.shiftKey) return;
    event.preventDefault();
    show(tab.getAttribute('data-plans-tab'), true);
  });

  // Redirects after a save point at an anchor inside one of the panels (#reseller-plans, #user-service-addons).
  var target = window.location.hash ? document.getElementById(window.location.hash.slice(1)) : null;
  var panel = target && target.closest('[data-plans-panel]');
  if (panel) {
    show(panel.getAttribute('data-plans-panel'), false);
    target.scrollIntoView();
  }
})();
