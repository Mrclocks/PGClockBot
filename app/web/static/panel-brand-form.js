/* Live preview for the panel brand card: colour, name and chosen logo.
   Presentation only — the server validates and decides everything on save. */
(function () {
  'use strict';

  var DEFAULT_LOGO_LIMIT_BYTES = 2 * 1024 * 1024;

  function init(form) {
    var preview = form.querySelector('[data-brand-preview]');
    if (!preview) return;
    var nameOut = preview.querySelector('[data-brand-preview-name]');
    var logoOut = preview.querySelector('[data-brand-preview-logo]');
    var nameIn = form.querySelector('[data-brand-name-input]');
    var fileIn = form.querySelector('[data-brand-logo-input]');
    var fallbackName = nameOut ? nameOut.textContent : '';
    var objectUrl = null;

    form.querySelectorAll('[data-brand-color-input]').forEach(function (radio) {
      radio.addEventListener('change', function () {
        if (!radio.checked) return;
        if (radio.value) preview.setAttribute('data-brand', radio.value);
        else preview.removeAttribute('data-brand');
      });
    });

    if (nameIn && nameOut) {
      nameIn.addEventListener('input', function () {
        nameOut.textContent = nameIn.value.trim() || fallbackName;
      });
    }

    if (fileIn && logoOut) {
      fileIn.addEventListener('change', function () {
        if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null; }
        var file = fileIn.files && fileIn.files[0];
        if (!file || file.size > DEFAULT_LOGO_LIMIT_BYTES || file.type.indexOf('image/') !== 0) return;
        objectUrl = URL.createObjectURL(file);
        logoOut.src = objectUrl;
      });
    }
  }

  document.querySelectorAll('[data-panel-brand-form]').forEach(init);
})();
