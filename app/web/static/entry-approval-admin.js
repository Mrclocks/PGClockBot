/* «تأیید ورود کاربران جدید»: list the requests and approve, reject or block them.
   All server text is inserted with textContent, never as HTML. */
(function (root) {
  "use strict";

  const STATUS = {
    pending: {label: "در انتظار", tone: "warn"},
    rejected: {label: "رد شده", tone: "neutral"},
    expired: {label: "منقضی", tone: "neutral"},
    blocked: {label: "مسدود", tone: "danger"},
  };
  // Which buttons each status offers; mirrors the transitions the server allows.
  const ACTIONS_BY_STATUS = {
    pending: ["approve", "reject", "block"],
    rejected: ["approve", "block"],
    blocked: ["approve"],
  };
  const CONFIRM = {
    approve: {title: "تأیید ورود", message: "منوها برای این کاربر باز می‌شود و پیام تأیید برایش می‌رود.", confirmLabel: "تأیید"},
    reject: {title: "رد درخواست", message: "پیام رد برای کاربر می‌رود؛ طبق تنظیمات می‌تواند بعداً دوباره درخواست بدهد.", confirmLabel: "رد", warn: true},
    block: {title: "مسدودسازی", message: "کاربر دیگر نمی‌تواند درخواست ورود بدهد تا وقتی خودتان تأییدش کنید.", confirmLabel: "مسدود", danger: true},
  };

  function statusInfo(row) {
    if (row.status === "rejected" && row.expired) return STATUS.expired;
    return STATUS[row.status] || {label: row.status, tone: "neutral"};
  }

  function rowModel(row) {
    const name = row.customer_name || (row.username ? "@" + row.username : String(row.customer));
    const when = row.status === "pending" ? row.requested_at : (row.decided_at || row.requested_at);
    return {
      name: name,
      // The handle goes on the second line only when the first line is a real name.
      username: row.username && row.customer_name ? "@" + row.username : "",
      customer: String(row.customer),
      when: when,
      status: statusInfo(row),
      actions: ACTIONS_BY_STATUS[row.status] || [],
    };
  }

  function faNumber(value) {
    const numbers = root.PanelNumbers;
    return numbers && numbers.faNumber ? numbers.faNumber(value) : String(value);
  }

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }

  function fill(tr, model) {
    tr.querySelector('[data-ea="name"]').textContent = model.name;
    tr.querySelector('[data-ea="username"]').textContent = model.username;
    tr.querySelector('[data-ea="customer"]').textContent = model.customer;
    tr.querySelector('[data-ea="when"]').textContent = model.when ? new Date(model.when).toLocaleString("fa-IR") : "—";
    const badge = tr.querySelector('[data-ea="status"]');
    badge.textContent = model.status.label;
    badge.classList.add(model.status.tone);
    tr.querySelectorAll("[data-ea-action]").forEach(function (button) {
      if (model.actions.indexOf(button.dataset.eaAction) === -1) button.remove();
    });
  }

  function render(card, data) {
    const body = card.querySelector("#ea-body");
    const rows = data.requests || [];
    body.replaceChildren();
    card.querySelector("#ea-count").textContent = faNumber(rows.length) + " درخواست";
    if (!rows.length) {
      body.appendChild(card.querySelector("#ea-empty-" + data.view).content.cloneNode(true));
      return;
    }
    const template = card.querySelector("#ea-row");
    rows.forEach(function (row) {
      const fragment = template.content.cloneNode(true);
      const tr = fragment.querySelector("tr");
      tr.dataset.eaId = String(row.id);
      fill(tr, rowModel(row));
      body.appendChild(fragment);
    });
  }

  async function load(card) {
    const msg = card.querySelector("#ea-msg");
    try {
      const res = await fetch(card.dataset.url + "?view=" + encodeURIComponent(card.dataset.view), {
        credentials: "same-origin", headers: {Accept: "application/json"},
      });
      if (res.status === 403) return; // no permission: the card stays hidden
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error("load");
      card.hidden = false;
      msg.textContent = "";
      render(card, data);
    } catch (err) {
      card.hidden = false;
      msg.textContent = "بارگذاری درخواست‌ها ناموفق بود؛ صفحه را دوباره باز کنید.";
    }
  }

  async function send(card, id, action) {
    const msg = card.querySelector("#ea-msg");
    try {
      const res = await fetch(card.dataset.url + "/" + encodeURIComponent(id) + "/" + action, {
        method: "POST",
        credentials: "same-origin",
        headers: {"X-CSRF-Token": csrfToken(), Accept: "application/json"},
      });
      const data = await res.json();
      msg.textContent = res.ok && data.ok ? "" : (data.error || "انجام نشد");
    } catch (err) {
      msg.textContent = "انجام نشد؛ دوباره تلاش کنید.";
    }
    load(card);
  }

  function onAction(card, button) {
    const tr = button.closest("tr");
    const action = button.dataset.eaAction;
    if (!tr || !CONFIRM[action]) return;
    const run = function () { send(card, tr.dataset.eaId, action); };
    if (typeof root.panelConfirm !== "function") { run(); return; }
    root.panelConfirm(CONFIRM[action]).then(function (result) { if (result && result.ok) run(); });
  }

  function setView(card, view) {
    card.dataset.view = view;
    card.querySelectorAll("[data-ea-view]").forEach(function (tab) {
      const on = tab.dataset.eaView === view;
      tab.classList.toggle("on", on);
      tab.setAttribute("aria-selected", on ? "true" : "false");
    });
    load(card);
  }

  function boot() {
    const card = document.getElementById("ea-requests");
    if (!card) return;
    card.dataset.view = "pending";
    card.addEventListener("click", function (event) {
      const tab = event.target.closest("[data-ea-view]");
      if (tab) { setView(card, tab.dataset.eaView); return; }
      const button = event.target.closest("[data-ea-action]");
      if (button) onAction(card, button);
    });
    load(card);
  }

  if (typeof module === "object" && module.exports) {
    module.exports = {rowModel: rowModel, statusInfo: statusInfo, ACTIONS_BY_STATUS: ACTIONS_BY_STATUS};
    return;
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})(typeof window !== "undefined" ? window : globalThis);
