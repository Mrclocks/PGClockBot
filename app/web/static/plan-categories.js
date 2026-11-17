/* Plan categories in the plans page and plan forms: add a category from the plan form without leaving it, start a
   plan inside a category from its group menu, and open the categories modal ready for a new one. The chips
   themselves are a data-ds-choice group (panel-ds.js) over a hidden select. */
(function (root) {
  'use strict';

  var NAME_MAX = 128;
  var QUICK_URL = '/plans/categories/quick';
  var GENERIC_ERROR = 'افزودن دسته انجام نشد؛ دوباره تلاش کنید.';
  var TONES = ['default', 'primary', 'success', 'danger'];

  /* The category name to send, or '' when there is nothing usable to send. */
  function cleanName(raw) {
    var name = String(raw == null ? '' : raw).trim();
    return name.length > NAME_MAX ? '' : name;
  }

  /* {ok, id, name, tone} from the quick-create answer, or {ok: false, error} with a message safe to show. */
  function readQuickResult(data) {
    if (data && data.ok === true && Number.isInteger(data.id) && data.id > 0 && typeof data.name === 'string') {
      var tone = TONES.indexOf(data.tone) >= 0 ? data.tone : 'default';
      return { ok: true, id: data.id, name: data.name, tone: tone };
    }
    var error = data && typeof data.error === 'string' && data.error.trim() ? data.error.trim() : GENERIC_ERROR;
    return { ok: false, error: error };
  }

  /* Adds the category to the hidden select and as a chip before «بدون دسته», unless it is already there. */
  function addCategory(select, chips, doc, category) {
    var value = String(category.id);
    var existing = Array.prototype.find.call(select.options, function (o) { return o.value === value; });
    if (!existing) {
      var option = doc.createElement('option');
      option.value = value;
      option.textContent = category.name;
      select.appendChild(option);
      var chip = doc.createElement('button');
      chip.type = 'button';
      chip.setAttribute('role', 'radio');
      chip.setAttribute('data-value', value);
      chip.setAttribute('data-tone', TONES.indexOf(category.tone) >= 0 ? category.tone : 'default');
      chip.title = category.name;
      var dot = doc.createElement('span');
      dot.className = 'plan-cat-dot';
      dot.setAttribute('aria-hidden', 'true');
      var label = doc.createElement('span');
      label.className = 'plan-cat-chip-name';
      label.dir = 'auto';
      label.textContent = category.name;
      chip.appendChild(dot);
      chip.appendChild(label);
      chips.insertBefore(chip, chips.querySelector('[data-value=""]'));
    }
    return value;
  }

  function pick(select, value) {
    select.value = value;
    select.dispatchEvent(new Event('change', { bubbles: true }));
  }

  /* The category id a returning page should land on ("#plan-cat-12" -> "plan-cat-12"), or null. */
  function arrivalId(hash) {
    var match = /^#(plan-cat-[1-9][0-9]{0,9})$/.exec(String(hash || ''));
    return match ? match[1] : null;
  }

  var api = {
    cleanName: cleanName, readQuickResult: readQuickResult, addCategory: addCategory, arrivalId: arrivalId, NAME_MAX: NAME_MAX
  };
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
    return;
  }
  root.PlanCategories = api;
  var doc = root.document;

  function showError(field, message) {
    var box = field.querySelector('[data-plan-category-error]');
    if (!box) return;
    box.textContent = message || '';
    box.hidden = !message;
  }

  async function create(field) {
    var input = field.querySelector('[data-plan-category-new-name]');
    var button = field.querySelector('[data-plan-category-create]');
    var select = field.querySelector('[data-plan-category-select]');
    var chips = field.querySelector('[data-ds-choice]');
    var name = cleanName(input && input.value);
    if (!name) {
      showError(field, 'نام دسته را بنویسید (حداکثر ' + NAME_MAX + ' حرف).');
      if (input) input.focus();
      return;
    }
    if (!button || button.disabled || !root.panelFetch) return;
    button.disabled = true;
    showError(field, '');
    try {
      var body = new FormData();
      body.append('name', name);
      var style = field.querySelector('[data-plan-category-new-style]');
      body.append('button_style', style && style.value != null ? style.value : 'inherit');
      var res = await root.panelFetch(QUICK_URL, { method: 'POST', body: body });
      var result = readQuickResult(root.panelReadJson ? await root.panelReadJson(res) : null);
      if (!result.ok) {
        showError(field, result.error);
        return;
      }
      pick(select, addCategory(select, chips, doc, result));
      input.value = '';
      if (style) {
        style.value = 'inherit';
        style.dispatchEvent(new Event('change', { bubbles: true }));
      }
      var panel = field.querySelector('[data-plan-category-new]');
      if (panel) panel.hidden = true;
    } catch (_) {
      showError(field, GENERIC_ERROR);
    } finally {
      button.disabled = false;
    }
  }

  doc.addEventListener('click', function (event) {
    var createBtn = event.target.closest('[data-plan-category-create]');
    if (createBtn) {
      var field = createBtn.closest('[data-plan-category-field]');
      if (field) create(field);
      return;
    }
    // «پلن در این دسته»: the plan modal opens on the fixed form; preselect the category once it has switched.
    var startIn = event.target.closest('[data-plan-category]');
    if (startIn) {
      var id = startIn.getAttribute('data-plan-category');
      root.setTimeout(function () {
        var select = doc.getElementById('user-plan-category');
        if (select && Array.prototype.some.call(select.options, function (o) { return o.value === id; })) pick(select, id);
      }, 0);
      return;
    }
    // Opened from the plans list, the categories modal returns there after saving; from anywhere else it stays open.
    var opener = event.target.closest('[data-modal-open="modal-plan-categories"]');
    var back = doc.getElementById('plan-category-back');
    if (opener && back) back.value = opener.getAttribute('data-category-back') === 'list' ? 'list' : '';
    // «دستهٔ جدید» / «ساخت دسته»: the categories modal starts as an empty «add» form even after an edit.
    if (event.target.closest('[data-category-new]')) {
      root.setTimeout(function () {
        var cancel = doc.getElementById('plan-category-cancel-edit');
        if (cancel && !cancel.hidden) cancel.click();
        var name = doc.getElementById('plan-category-name');
        if (name) name.focus();
      }, 0);
    }
  });

  // Back from saving a category: bring its group into view and mark it for a moment.
  var arrived = arrivalId(root.location && root.location.hash);
  var arrivedRow = arrived && doc.getElementById(arrived);
  if (arrivedRow) {
    // After load: the browser's own jump to the anchor runs late and would put the group at the very top.
    var land = function () {
      arrivedRow.scrollIntoView({ block: 'center' });
      arrivedRow.classList.add('is-arrived');
      arrivedRow.addEventListener('animationend', function () { arrivedRow.classList.remove('is-arrived'); }, { once: true });
    };
    if (doc.readyState === 'complete') root.requestAnimationFrame(land);
    else root.addEventListener('load', function () { root.requestAnimationFrame(land); }, { once: true });
  }

  // Enter in the new-category name adds it instead of submitting the whole plan form.
  doc.addEventListener('keydown', function (event) {
    if (event.key !== 'Enter' || !event.target.matches || !event.target.matches('[data-plan-category-new-name]')) return;
    event.preventDefault();
    var field = event.target.closest('[data-plan-category-field]');
    if (field) create(field);
  });
})(typeof window !== 'undefined' ? window : globalThis);
