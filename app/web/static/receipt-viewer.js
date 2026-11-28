/* The finance receipt modal. A payment may have more than one receipt photo (an album, a second page):
   the opener carries data-receipt-open (the first photo's URL) and data-receipt-count, and the modal
   steps through the photos with «قبلی» / «بعدی»; photo n is the same URL with ?n=<index>. */
(function (root) {
  'use strict';

  function faDigits(value) {
    var numbers = root && root.PanelNumbers;
    return numbers ? numbers.faDigits(value) : String(value);
  }

  /** The photo count an opener carries: a whole number from 1, else 1. */
  function readCount(raw) {
    var n = Number(String(raw == null ? '' : raw).trim());
    return Number.isInteger(n) && n >= 1 ? n : 1;
  }

  /** The URL of photo ``index`` (0 is the first), with a cache breaker so a reopened modal reloads it. */
  function photoUrl(base, index, stamp) {
    var url = String(base || '');
    var sep = url.indexOf('?') >= 0 ? '&' : '?';
    return url + sep + 'n=' + Number(index) + '&_=' + Number(stamp);
  }

  function counterText(index, count) {
    return 'تصویر ' + faDigits(index + 1) + ' از ' + faDigits(count);
  }

  var api = { readCount: readCount, photoUrl: photoUrl, counterText: counterText };
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
    return;
  }

  var modal = document.getElementById('modal-receipt');
  var img = document.getElementById('receipt-modal-img');
  if (!modal || !img) return;
  var nav = modal.querySelector('[data-receipt-nav]');
  var counter = modal.querySelector('[data-receipt-counter]');
  var prev = modal.querySelector('[data-receipt-step="-1"]');
  var next = modal.querySelector('[data-receipt-step="1"]');
  var view = { base: '', index: 0, count: 1, stamp: 0 };

  function show() {
    img.src = photoUrl(view.base, view.index, view.stamp);
    img.alt = 'رسید پرداخت — ' + counterText(view.index, view.count);
    if (nav) nav.hidden = view.count < 2;
    if (counter) counter.textContent = counterText(view.index, view.count);
    if (prev) prev.disabled = view.index <= 0;
    if (next) next.disabled = view.index >= view.count - 1;
  }

  function clear() {
    img.removeAttribute('src');
  }

  document.addEventListener('click', function (e) {
    var opener = e.target.closest('[data-receipt-open]');
    if (opener) {
      e.preventDefault();
      var base = opener.getAttribute('data-receipt-open');
      if (!base) return;
      view = { base: base, index: 0, count: readCount(opener.getAttribute('data-receipt-count')), stamp: Date.now() };
      clear();
      show();
      if (root.openModal) root.openModal('modal-receipt');
      return;
    }
    var step = e.target.closest('[data-receipt-step]');
    if (step && modal.contains(step)) {
      var to = view.index + Number(step.getAttribute('data-receipt-step'));
      if (to < 0 || to >= view.count) return;
      view.index = to;
      clear();
      show();
      return;
    }
    if (e.target === modal.querySelector('.ui-modal-backdrop') || e.target.closest('#modal-receipt [data-modal-close]')) {
      clear();
    }
  });
})(typeof window !== 'undefined' ? window : this);
