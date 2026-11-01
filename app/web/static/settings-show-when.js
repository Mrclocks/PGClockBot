/* Show or hide settings fields that depend on another field's value.
   A field carries data-show-when="s_<controlling key>:<value>,<value>"; it is hidden, not disabled,
   so its saved value is still submitted and survives switching the mode back and forth. */
(function () {
  'use strict';

  function isShown(rule, current) {
    var split = String(rule || '').split(':');
    if (split.length !== 2 || !split[0]) return true;
    return split[1].split(',').indexOf(String(current)) !== -1;
  }

  function controllerName(rule) {
    return String(rule || '').split(':')[0];
  }

  // An unchecked switch submits nothing, so it reads as "0".
  function controlValue(control) {
    if (control.type === 'checkbox') return control.checked ? control.value : '0';
    return control.value;
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { isShown: isShown, controllerName: controllerName, controlValue: controlValue };
  }
  if (typeof document === 'undefined') return;

  function refresh(form) {
    form.querySelectorAll('[data-show-when]').forEach(function (field) {
      var rule = field.getAttribute('data-show-when');
      var control = form.querySelector('[name="' + controllerName(rule) + '"]');
      if (control) field.hidden = !isShown(rule, controlValue(control));
    });
  }

  document.querySelectorAll('form.settings-form').forEach(function (form) {
    if (!form.querySelector('[data-show-when]')) return;
    refresh(form);
    form.addEventListener('change', function () { refresh(form); });
  });
})();
