/* The user card opened from a table row carries that row's «⋯» actions (approve, reject, renew, …).
   The row's own forms and buttons are copied, so every action keeps its route, permission check and
   confirmation; actions the card already has (edit, message, the card itself) are not repeated.
   A row without other actions leaves the card with its default buttons. */
(function () {
  'use strict';
  var CARD_OWN_MODALS = ['modal-user-card', 'modal-user-message', 'modal-user-edit', 'modal-reseller-edit'];
  var DEFAULT_TITLE = 'اقدام‌های این ردیف';
  var ACTION_SELECTOR = 'form, button, a[href]';

  function sectionTitle(context) {
    var label = String(context || '').trim();
    return label ? 'اقدام‌های ' + label : DEFAULT_TITLE;
  }

  function opensCardOwnModal(el) {
    var openers = [el].concat(Array.prototype.slice.call(el.querySelectorAll('[data-modal-open]')));
    return openers.some(function (node) {
      return CARD_OWN_MODALS.indexOf(node.getAttribute('data-modal-open')) !== -1;
    });
  }

  function isRowAction(el) {
    return el.matches(ACTION_SELECTOR) && !opensCardOwnModal(el);
  }

  function rowActions(opener) {
    var row = opener && opener.closest('tr');
    var menu = row && row.querySelector('.row-actions-menu');
    return menu ? Array.prototype.filter.call(menu.children, isRowAction) : [];
  }

  // A copy without ids (they stay unique on the page) and at the card's button size.
  function copyAction(el) {
    var copy = el.cloneNode(true);
    [copy].concat(Array.prototype.slice.call(copy.querySelectorAll('[id], .btn-sm'))).forEach(function (node) {
      node.removeAttribute('id');
      node.classList.remove('btn-sm');
    });
    return copy;
  }

  function buildSection(doc, title, actions) {
    var section = doc.createElement('section');
    section.className = 'edit-section user-card-row-actions';
    var head = doc.createElement('header');
    head.className = 'edit-section-head';
    var heading = doc.createElement('h3');
    heading.textContent = title;
    head.appendChild(heading);
    var grid = doc.createElement('div');
    grid.className = 'user-card-row-actions-grid';
    actions.forEach(function (el) { grid.appendChild(copyAction(el)); });
    section.appendChild(head);
    section.appendChild(grid);
    return section;
  }

  if (typeof module === 'object' && module.exports) {
    module.exports = { sectionTitle: sectionTitle, CARD_OWN_MODALS: CARD_OWN_MODALS };
    return;
  }

  document.addEventListener('panel:fragment-loaded', function (e) {
    var body = e.target;
    var detail = e.detail || {};
    if (!body || body.id !== 'user-card-body' || !detail.ok) return;
    var actions = rowActions(detail.button);
    if (!actions.length) return;
    var section = buildSection(document, sectionTitle(detail.button.getAttribute('data-card-context')), actions);
    var identity = body.querySelector('.edit-section--identity');
    if (identity) identity.after(section);
    else body.prepend(section);
  });
})();
