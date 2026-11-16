(function () {
  'use strict';

  const config = document.getElementById('template-variable-specs');
  if (!config) return;
  let specs;
  try { specs = JSON.parse(config.textContent); } catch (_) { return; }

  const fields = new WeakMap();
  let sequence = 0;
  const has = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const shortToken = (token) => token.length > 64 ? token.slice(0, 61) + '...' : token;

  function fieldKey(control) {
    if (!(control instanceof HTMLTextAreaElement || control instanceof HTMLInputElement)) return null;
    if (control instanceof HTMLInputElement && !['text', 'url'].includes(control.type)) return null;
    const key = control.classList.contains('pay-dest-gw-link')
      ? 'gateway_link'
      : (control.name || '').replace(/^s_/, '');
    return has(specs, key) ? key : null;
  }

  function tokensIn(text, allowed) {
    const tokens = new Map();
    function add(token) {
      const match = /^\{([A-Za-z_][A-Za-z0-9_]*)\}$/.exec(token);
      tokens.set(token, {
        token: token,
        valid: !!match && has(allowed, match[1]),
        used: true,
        malformed: !match,
        title: match && allowed[match[1]],
      });
    }
    // Keep nested or incomplete braces together so they cannot appear valid.
    let start = -1;
    let depth = 0;
    for (let i = 0; i < text.length; i += 1) {
      if (text[i] === '{') {
        if (depth === 0) start = i;
        depth += 1;
      } else if (text[i] === '}') {
        if (depth === 0) add('}');
        else if (--depth === 0) add(text.slice(start, i + 1));
      }
    }
    if (depth > 0) add(text.slice(start));
    Object.entries(allowed).forEach(([name, title]) => {
      const token = '{' + name + '}';
      if (!tokens.has(token)) tokens.set(token, { token: token, valid: true, used: false, title: title });
    });
    return Array.from(tokens.values());
  }

  function tokenBadge(item) {
    const badge = document.createElement('button');
    const status = !item.valid ? 'is-invalid' : (item.used ? 'is-valid' : 'is-unused');
    const label = !item.valid ? 'نامعتبر' : (item.used ? 'معتبر' : 'استفاده‌نشده');
    badge.type = 'button';
    badge.className = 'template-variable-token ' + status;
    badge.dir = 'ltr';
    // The token reads left to right in its own inline box, so the capsule centres its letters (panel.css).
    const text = document.createElement('bdi');
    text.dir = 'ltr';
    text.textContent = shortToken(item.token);
    badge.appendChild(text);
    badge.disabled = !item.valid;
    badge.title = item.token + ': ' + (item.valid
      ? item.title + ' — برای کپی کلیک کنید'
      : (item.malformed ? 'قالب متغیر نادرست است' : 'در این بخش پشتیبانی نمی‌شود'));
    badge.setAttribute('aria-label', item.token + ': ' + label + (item.valid ? '؛ کپی متغیر' : ''));
    if (item.valid) {
      badge.setAttribute('data-copy', item.token);
      badge.setAttribute('aria-live', 'polite');
      badge.setAttribute('aria-atomic', 'true');
    } else badge.setAttribute('aria-disabled', 'true');
    return badge;
  }

  function render(control, state) {
    const tokens = tokensIn(control.value || '', specs[state.key]);
    const invalid = tokens.filter((item) => !item.valid);
    state.tokens.replaceChildren();
    tokens.forEach((item) => state.tokens.appendChild(tokenBadge(item)));

    const unsupported = invalid.filter((item) => !item.malformed);
    const messages = [];
    if (unsupported.length) {
      const subject = unsupported.length === 1 ? 'متغیر ' : 'متغیرهای ';
      const ending = unsupported.length === 1 ? ' پشتیبانی نمی‌شود.' : ' پشتیبانی نمی‌شوند.';
      messages.push(subject + unsupported.map((item) => item.token).join('، ') + ' در این بخش' + ending);
    }
    if (invalid.some((item) => item.malformed)) {
      messages.push('قالب متغیر نادرست است؛ از قالب {name} بدون فاصله استفاده کنید.');
    }
    const message = messages.join(' ');
    if (state.error.textContent !== message) state.error.textContent = message;
    state.error.hidden = !message;
    state.feedback.hidden = tokens.length === 0;
    control.classList.toggle('template-variable-invalid', invalid.length > 0);

    if (invalid.length) {
      if (control.getAttribute('aria-invalid') !== 'true') {
        state.previousInvalid = control.getAttribute('aria-invalid');
        state.ownsInvalid = true;
        control.setAttribute('aria-invalid', 'true');
      }
    } else if (state.ownsInvalid) {
      // Leave errors owned by the panel's ordinary form validator intact.
      if (!control.closest('.is-invalid') && control.validity.valid) {
        if (state.previousInvalid === null) control.removeAttribute('aria-invalid');
        else control.setAttribute('aria-invalid', state.previousInvalid);
      }
      state.ownsInvalid = false;
    }
  }

  function bind(control) {
    const key = fieldKey(control);
    if (!key) return;
    let state = fields.get(control);
    if (!state) {
      const feedback = document.createElement('div');
      feedback.className = 'template-variable-feedback';
      feedback.setAttribute('data-template-variable-field', key);
      const tokens = document.createElement('div');
      tokens.className = 'template-variable-tokens';
      const error = document.createElement('small');
      error.className = 'template-variable-error';
      do { error.id = 'template-variable-error-' + (++sequence); } while (document.getElementById(error.id));
      error.setAttribute('aria-live', 'polite');
      error.setAttribute('aria-atomic', 'true');
      feedback.append(tokens, error);
      const descriptions = (control.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
      descriptions.push(error.id);
      control.setAttribute('aria-describedby', descriptions.join(' '));
      state = { key: key, feedback: feedback, tokens: tokens, error: error, ownsInvalid: false };
      fields.set(control, state);
    }
    if (control.nextElementSibling !== state.feedback) control.after(state.feedback);
    render(control, state);
  }

  function bindAll(root) {
    if (!root || !root.querySelectorAll) return;
    if (root.matches && root.matches('input, textarea')) bind(root);
    root.querySelectorAll('input, textarea').forEach(bind);
  }

  function boot() {
    bindAll(document);
    document.addEventListener('input', (event) => bind(event.target));
    document.addEventListener('change', (event) => bind(event.target));
    document.addEventListener('reset', (event) => {
      setTimeout(() => { if (!event.defaultPrevented) bindAll(event.target); }, 0);
    });
    document.addEventListener('submit', (event) => bindAll(event.target));
    document.addEventListener('panel:dom-ready', (event) => bindAll((event.detail && event.detail.root) || document));
    new MutationObserver((changes) => {
      changes.forEach((change) => change.addedNodes.forEach((node) => {
        if (node.nodeType === Node.ELEMENT_NODE) bindAll(node);
      }));
    }).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
