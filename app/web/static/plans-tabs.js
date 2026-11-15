/* Switches between the user-plan and reseller-plan panels without a reload, and tells the add-plan button which
   audience a new plan starts with. The tab links are real (?tab=resellers), so the page also works without this
   script. */
(function (root) {
  'use strict';

  var RESELLER_HASH_KINDS = {
    '#reseller-plans': 'fixed',
    '#reseller-plans-fixed': 'fixed',
    '#reseller-plans-payg': 'payg',
    '#reseller-plans-addons': 'addon_volume'
  };

  /* Audience and kind the add-plan form opens with: the reseller tab (or a redirect anchor inside it) starts a
     reseller plan, everything else a user plan. Only a platform admin can add reseller plans. */
  function addPlanDefaults(isPlatformAdmin, activeTab, hash) {
    if (!isPlatformAdmin) return { audience: 'users', kind: 'fixed' };
    if (Object.prototype.hasOwnProperty.call(RESELLER_HASH_KINDS, hash || '')) {
      return { audience: 'resellers', kind: RESELLER_HASH_KINDS[hash] };
    }
    return { audience: activeTab === 'resellers' ? 'resellers' : 'users', kind: 'fixed' };
  }

  function activeTab(doc) {
    var open = doc.querySelector('[data-plans-panel]:not([hidden])');
    return open ? open.getAttribute('data-plans-panel') : 'users';
  }

  var api = { addPlanDefaults: addPlanDefaults, activeTab: activeTab };
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
    return;
  }
  root.PlansTabs = api;

  var doc = root.document;
  var nav = doc.querySelector('[data-plans-tabs]');
  if (!nav) return;
  var tabs = nav.querySelectorAll('[data-plans-tab]');
  var panels = doc.querySelectorAll('[data-plans-panel]');

  function show(name, updateUrl) {
    tabs.forEach(function (tab) {
      var on = tab.getAttribute('data-plans-tab') === name;
      tab.classList.toggle('active', on);
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    panels.forEach(function (panel) {
      panel.hidden = panel.getAttribute('data-plans-panel') !== name;
    });
    if (updateUrl && root.history && root.history.replaceState) {
      var url = new URL(root.location.href);
      if (name === 'resellers') url.searchParams.set('tab', 'resellers');
      else url.searchParams.delete('tab');
      url.hash = '';
      root.history.replaceState(null, '', url.pathname + url.search);
    }
  }

  nav.addEventListener('click', function (event) {
    var tab = event.target.closest('[data-plans-tab]');
    if (!tab || event.metaKey || event.ctrlKey || event.shiftKey) return;
    event.preventDefault();
    show(tab.getAttribute('data-plans-tab'), true);
  });

  // Redirects after a save point at an anchor inside one of the panels (#reseller-plans, #user-service-addons).
  var target = root.location.hash ? doc.getElementById(root.location.hash.slice(1)) : null;
  var panel = target && target.closest('[data-plans-panel]');
  if (panel) {
    show(panel.getAttribute('data-plans-panel'), false);
    target.scrollIntoView();
  }
})(typeof window !== 'undefined' ? window : globalThis);
