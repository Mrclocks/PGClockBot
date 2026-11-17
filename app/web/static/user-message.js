/* «پیام» on a user row or in the user card: point the shared message form at that person and clear it. */
(function () {
  'use strict';
  var USER_ID = /^[1-9][0-9]{0,18}$/;

  // Where the form posts and whom it names; null for an id that is not a plain positive number.
  function messageTarget(uid, name) {
    var id = String(uid || '');
    if (!USER_ID.test(id)) return null;
    return { action: '/users/' + id + '/message', label: String(name || '') || '#' + id };
  }

  if (typeof module === 'object' && module.exports) {
    module.exports = { messageTarget: messageTarget };
    return;
  }
  var form = document.getElementById('user-msg-form');
  if (!form) return;
  var target = document.getElementById('user-msg-target');
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-modal-open="modal-user-message"][data-msg-uid]');
    if (!btn) return;
    var picked = messageTarget(btn.getAttribute('data-msg-uid'), btn.getAttribute('data-msg-name'));
    if (!picked) return;
    form.action = picked.action;
    // The label «گیرنده:» is in the markup; only the name changes, as text.
    if (target) target.textContent = picked.label;
    var body = form.querySelector('textarea[name="body"]');
    if (body) body.value = '';
  });
})();
