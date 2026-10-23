/* Date/time form feedback; range calculations remain in the finance service. */
(function (root) {
  'use strict';
  function rangeError(start, end) {
    if (!start || !end) return 'تاریخ و ساعت شروع و پایان را کامل وارد کنید.';
    if (start >= end) return 'پایان بازه باید بعد از شروع آن باشد.';
    return '';
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { rangeError: rangeError };
    return;
  }
  var form = root.document.querySelector('[data-finance-range]');
  if (!form) return;
  var start = form.elements.start, end = form.elements.end;
  var status = form.querySelector('[data-finance-status]');
  var button = form.querySelector('[data-finance-submit]');
  var customToggle = root.document.querySelector('[data-finance-custom-toggle]');
  if (customToggle) {
    customToggle.addEventListener('click', function () {
      form.hidden = !form.hidden;
      customToggle.setAttribute('aria-expanded', String(!form.hidden));
      if (!form.hidden) start.focus();
    });
  }
  function validate() {
    var message = rangeError(start.value, end.value);
    end.setCustomValidity(message);
    return message;
  }
  form.addEventListener('input', validate);
  form.addEventListener('submit', function (event) {
    var message = validate();
    if (message) {
      event.preventDefault();
      status.textContent = message;
      end.reportValidity();
      return;
    }
    form.setAttribute('aria-busy', 'true');
    button.disabled = true;
    status.textContent = 'در حال آماده‌سازی گزارش…';
  });
  root.addEventListener('pageshow', function () {
    form.removeAttribute('aria-busy');
    button.disabled = false;
    status.textContent = '';
  });
})(typeof window !== 'undefined' ? window : globalThis);
