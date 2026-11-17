/* «افزودن سرویس قبلی» on the customer's edit screen: check the link or panel username first, then
   allow attaching exactly the panel user that was shown. Server text is inserted with textContent,
   never as HTML. The edit body is loaded into a modal later, so events are delegated. */
(function (root) {
  "use strict";

  const FORM = "form[data-si-assign]";
  const FALLBACK_ERROR = "بررسی انجام نشد. دوباره تلاش کنید.";
  const ALREADY_YOURS = "این سرویس از قبل در سرویس‌های این مشتری است.";

  /** Label and value rows of a preview answer; missing values show as «—».
      ``toFa`` writes figures in Persian digits; the panel username stays as the panel spells it. */
  function previewRows(data, toFa) {
    const figures = typeof toFa === "function" ? toFa : function (text) { return text; };
    const value = function (key) {
      const text = data && typeof data[key] === "string" ? data[key].trim() : "";
      return text || "—";
    };
    return [
      ["کاربر پنل", value("panel_username")],
      ["وضعیت", value("status")],
      ["مصرف", figures(value("used"))],
      ["حجم", figures(value("limit"))],
      ["انقضا", figures(value("expire"))],
    ];
  }

  /** The panel user id the attach may use, or "" when the answer does not allow attaching. */
  function attachableId(data) {
    if (!data || data.ok !== true || data.already_yours === true) return "";
    const id = data.panel_user_id;
    return Number.isInteger(id) && id > 0 ? String(id) : "";
  }

  /** A safe message for a failed check. */
  function errorText(data) {
    return data && typeof data.error === "string" && data.error.trim() ? data.error.trim() : FALLBACK_ERROR;
  }

  function parts(form) {
    return {
      // The panel's select widget moves the name to a hidden mirror input, so look it up by name.
      kind: form.elements.namedItem("kind"),
      value: form.querySelector('input[name="value"]'),
      id: form.querySelector("[data-si-assign-id]"),
      result: form.querySelector("[data-si-assign-result]"),
      submit: form.querySelector("[data-si-assign-submit]"),
      check: form.querySelector("[data-si-assign-check]"),
      csrf: form.querySelector('input[name="csrf_token"]'),
    };
  }

  function reset(form) {
    const p = parts(form);
    p.id.value = "";
    p.submit.disabled = true;
    p.result.hidden = true;
    p.result.replaceChildren();
  }

  function el(doc, tag, className, text) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function showError(form, message) {
    const p = parts(form);
    p.result.replaceChildren(el(form.ownerDocument, "div", "flash err", message));
    p.result.hidden = false;
  }

  function showPreview(form, data) {
    const doc = form.ownerDocument;
    const p = parts(form);
    const row = el(doc, "div", "svc-stat-row");
    const numbers = root.PanelNumbers;
    previewRows(data, numbers && numbers.faNumber).forEach(function (pair) {
      const pill = el(doc, "div", "user-edit-wallet-pill svc-stat-pill");
      pill.appendChild(el(doc, "span", "muted", pair[0]));
      const strong = el(doc, "strong", "", pair[1]);
      if (pair[0] === "کاربر پنل") strong.setAttribute("dir", "ltr");
      pill.appendChild(strong);
      row.appendChild(pill);
    });
    const nodes = [row];
    if (data.already_yours === true) nodes.push(el(doc, "div", "ds-note", ALREADY_YOURS));
    p.result.replaceChildren.apply(p.result, nodes);
    p.result.hidden = false;
    p.id.value = attachableId(data);
    p.submit.disabled = !p.id.value;
  }

  async function check(form) {
    const p = parts(form);
    reset(form);
    if (!p.value.value.trim()) {
      showError(form, "لینک اشتراک یا نام کاربری پنل را وارد کنید.");
      return;
    }
    const body = new FormData();
    body.append("kind", p.kind.value);
    body.append("value", p.value.value);
    body.append("csrf_token", p.csrf ? p.csrf.value : "");
    p.check.disabled = true;
    p.check.setAttribute("aria-busy", "true");
    try {
      const res = await fetch(form.dataset.siPreviewUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {"X-CSRF-Token": p.csrf ? p.csrf.value : "", Accept: "application/json"},
        body: body,
      });
      const data = await res.json().catch(function () { return null; });
      if (res.ok && data && data.ok === true) showPreview(form, data);
      else showError(form, errorText(data));
    } catch (err) {
      showError(form, FALLBACK_ERROR);
    } finally {
      p.check.disabled = false;
      p.check.removeAttribute("aria-busy");
    }
  }

  function bind(doc) {
    doc.addEventListener("click", function (event) {
      const button = event.target.closest && event.target.closest("[data-si-assign-check]");
      const form = button && button.closest(FORM);
      if (form) check(form);
    });
    const onEdit = function (event) {
      const field = event.target.closest && event.target.closest("[data-si-assign-input]");
      const form = field && field.closest(FORM);
      if (form) reset(form);
    };
    doc.addEventListener("input", onEdit);
    doc.addEventListener("change", onEdit);
    // Enter in the field checks the service instead of submitting the form.
    doc.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      const field = event.target.closest && event.target.closest('input[data-si-assign-input]');
      const form = field && field.closest(FORM);
      if (!form) return;
      event.preventDefault();
      check(form);
    });
  }

  const api = { previewRows, attachableId, errorText };
  if (typeof module === "object" && module.exports) {
    module.exports = api;
    return;
  }
  root.ServiceImportAssign = api;
  if (root.document) bind(root.document);
})(typeof window !== "undefined" ? window : globalThis);
