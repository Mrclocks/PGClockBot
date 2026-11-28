/* The header's action-center badge follows the list the modal just loaded. The page renders the badge from a
   briefly cached count, so an item resolved since then would keep showing «۱» over an empty list; the modal body
   (/inbox/panel) carries the fresh count in data-inbox-count and this script writes it back to the badge. */
(function (root) {
  'use strict';

  var MAX_SHOWN = 99;
  var BASE_LABEL = 'مرکز اقدام';

  function faDigits(value) {
    var numbers = root && root.PanelNumbers;
    return numbers ? numbers.faDigits(value) : String(value);
  }

  /** The count a modal body reports, or null when it carries none (an error page, an old fragment). */
  function readCount(raw) {
    if (raw == null || !/^\d+$/.test(String(raw).trim())) return null;
    return Number(String(raw).trim());
  }

  function badgeText(count) {
    return count > MAX_SHOWN ? faDigits(MAX_SHOWN) + '+' : faDigits(count);
  }

  function badgeLabel(count) {
    return count > 0 ? BASE_LABEL + '، ' + faDigits(count) + ' اعلان' : BASE_LABEL;
  }

  function applyCount(link, count) {
    var badge = link.querySelector('.topbar-inbox-count');
    link.setAttribute('aria-label', badgeLabel(count));
    if (count <= 0) {
      if (badge) badge.remove();
      return;
    }
    if (!badge) {
      badge = link.ownerDocument.createElement('span');
      badge.className = 'topbar-inbox-count';
      badge.setAttribute('aria-hidden', 'true');
      link.appendChild(badge);
    }
    badge.textContent = badgeText(count);
  }

  var api = { readCount: readCount, badgeText: badgeText, badgeLabel: badgeLabel, applyCount: applyCount };
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
    return;
  }

  document.addEventListener('panel:fragment-loaded', function (e) {
    var body = e.target;
    if (!body || body.id !== 'inbox-modal-body' || !(e.detail && e.detail.ok)) return;
    var panel = body.querySelector('[data-inbox-count]');
    var count = panel ? readCount(panel.getAttribute('data-inbox-count')) : null;
    var link = document.querySelector('.topbar-inbox');
    if (count === null || !link) return;
    applyCount(link, count);
  });
})(typeof window !== 'undefined' ? window : this);
