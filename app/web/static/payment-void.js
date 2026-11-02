/* Opens the shared void dialog for one payment row; fills it with textContent only. */
(function () {
  'use strict';
  var modal = document.getElementById('modal-payment-void');
  var form = document.getElementById('payment-void-form');
  if (!modal || !form) return;
  var summary = document.getElementById('payment-void-summary');
  var refundField = document.getElementById('payment-void-refund-field');
  var topupNote = document.getElementById('payment-void-topup-note');

  function open(button) {
    var paymentId = button.getAttribute('data-payment-void-open');
    var isTopup = !!button.getAttribute('data-void-topup');
    form.setAttribute('action', '/payments/' + encodeURIComponent(paymentId) + '/void');
    form.reset();
    summary.textContent = 'پرداخت #' + paymentId + ' — ' + button.getAttribute('data-void-amount') + ' تومان';
    refundField.hidden = isTopup;
    topupNote.hidden = !isTopup;
    if (window.openModal) {
      window.openModal('modal-payment-void');
    } else {
      modal.hidden = false;
      modal.classList.add('open');
    }
  }

  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-payment-void-open]');
    if (!button) return;
    event.preventDefault();
    open(button);
  });
})();
