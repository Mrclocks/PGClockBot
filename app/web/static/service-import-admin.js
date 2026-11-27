/* «افزودن سرویس قبلی» settings tab: live status, link test and recent tries.
   All server text is inserted with textContent, never as HTML. */
(function () {
  "use strict";

  const OUTCOME_LABELS = {
    added: "اضافه شد",
    upgraded: "اضافه شد",
    already_yours: "از قبل در لیست",
    refused: "رد شد",
    invalid: "لینک نامعتبر",
    error: "خطای پنل",
    pending: "نیمه‌تمام",
  };

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function tile(tone, title, lines) {
    const box = el("div", "bot-conn " + tone);
    box.setAttribute("role", "listitem");
    const text = el("span", "bot-conn-text");
    text.appendChild(el("span", "bot-conn-k", title));
    lines.forEach(function (line) {
      const cap = el("span", "bot-conn-cap muted", line);
      cap.setAttribute("dir", "ltr");
      text.appendChild(cap);
    });
    box.appendChild(text);
    return box;
  }

  function renderStatus(root, data) {
    const row = root.querySelector("#si-conn");
    row.replaceChildren();
    if (!data.ok) {
      row.appendChild(tile("is-err", "اتصال به پنل", [data.error || "نامشخص"]));
      return;
    }
    const identityTone = data.identity_ok ? "is-ok" : "is-err";
    if (data.platform) {
      const login = data.login === "api_key" ? "کلید API" : "رمز عبور";
      row.appendChild(tile(identityTone, "ورود ربات به پنل", [login + " · " + (data.signed_in_as || "نامشخص")]));
    }
    row.appendChild(tile(identityTone, "ادمین‌های مجاز", data.admins && data.admins.length ? data.admins : ["هیچ ادمینی مجاز نیست"]));
    row.appendChild(tile("is-ok", "مسیرهای مجاز لینک", data.paths && data.paths.length ? data.paths : ["—"]));
  }

  function renderAttempts(body, attempts) {
    body.replaceChildren();
    if (!attempts || !attempts.length) {
      const empty = el("tr");
      const cell = el("td", "muted", "هنوز تلاشی ثبت نشده");
      cell.colSpan = 4;
      empty.appendChild(cell);
      body.appendChild(empty);
      return;
    }
    attempts.forEach(function (item) {
      const tr = el("tr");
      const when = item.at ? new Date(item.at).toLocaleString("fa-IR") : "—";
      tr.appendChild(el("td", "", when));
      tr.appendChild(el("td", "", (item.name ? item.name + " · " : "") + item.customer));
      tr.appendChild(el("td", "", OUTCOME_LABELS[item.outcome] || item.outcome));
      tr.appendChild(el("td", "muted", item.label || "—"));
      body.appendChild(tr);
    });
  }

  function renderTest(box, data) {
    box.replaceChildren();
    const flash = el("div", "flash " + (data.ok ? "ok" : "err"));
    const copy = el("div", "flash-copy");
    if (data.ok) {
      copy.appendChild(el("strong", "", "این لینک پذیرفته می‌شود"));
      const parts = ["مسیر " + data.path];
      if (data.panel_username) parts.push("کاربر پنل: " + data.panel_username);
      if (data.already_attached) parts.push("این سرویس همین حالا به یک مشتری وصل است");
      copy.appendChild(el("span", "muted", parts.join(" — ")));
    } else {
      copy.appendChild(el("strong", "", "رد می‌شود"));
      const parts = [data.label || data.reason || "نامشخص"];
      if (data.owner_admin) parts.push("ادمین مالک سرویس: " + data.owner_admin);
      copy.appendChild(el("span", "muted", parts.join(" — ")));
    }
    flash.appendChild(copy);
    box.appendChild(flash);
  }

  async function loadStatus(root, refresh) {
    const url = root.dataset.statusUrl + (refresh ? "?refresh=1" : "");
    try {
      const res = await fetch(url, {credentials: "same-origin", headers: {Accept: "application/json"}});
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "خطا");
      renderStatus(root, data);
      renderAttempts(document.getElementById("si-attempts"), data.attempts);
    } catch (err) {
      renderStatus(root, {ok: false, error: "بارگذاری وضعیت ناموفق بود"});
    }
  }

  async function runTest(root) {
    const input = document.getElementById("si-link");
    const box = document.getElementById("si-test-result");
    const button = document.getElementById("si-test");
    const link = (input.value || "").trim();
    if (!link) return;
    button.disabled = true;
    try {
      const body = new URLSearchParams({link: link});
      const res = await fetch(root.dataset.testUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {"X-CSRF-Token": csrfToken(), Accept: "application/json"},
        body: body,
      });
      renderTest(box, await res.json());
    } catch (err) {
      renderTest(box, {ok: false, label: "بررسی ناموفق بود؛ دوباره تلاش کنید"});
    } finally {
      input.value = "";
      button.disabled = false;
    }
  }

  const SVG_NS = "http://www.w3.org/2000/svg";
  // Same drawings as icon('check') and icon('close') in _icons.html.
  const ICON_PATHS = { check: "M5 12.5l4.5 4.5L19 7.5", close: "M6 6l12 12M18 6L6 18" };

  function buttonIcon(name) {
    const svg = document.createElementNS(SVG_NS, "svg");
    const attrs = { class: "btn-ico", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.8",
      "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" };
    Object.keys(attrs).forEach(function (key) { svg.setAttribute(key, attrs[key]); });
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", ICON_PATHS[name]);
    svg.appendChild(path);
    return svg;
  }

  function requestButton(label, className, onClick, iconName) {
    const button = el("button", "btn " + className, label);
    if (iconName) button.prepend(buttonIcon(iconName));
    button.type = "button";
    button.addEventListener("click", onClick);
    return button;
  }

  async function decide(card, id, action, buttons) {
    const msg = document.getElementById("si-requests-msg");
    buttons.forEach(function (b) { b.disabled = true; });
    msg.textContent = "";
    try {
      const res = await fetch(card.dataset.url + "/" + id + "/" + action, {
        method: "POST",
        credentials: "same-origin",
        headers: {"X-CSRF-Token": csrfToken(), Accept: "application/json"},
      });
      const data = await res.json();
      if (!res.ok || !data.ok) msg.textContent = data.error || "انجام نشد";
    } catch (err) {
      msg.textContent = "انجام نشد؛ دوباره تلاش کنید";
    }
    loadRequests(card);
  }

  function renderRequests(card, rows) {
    const body = document.getElementById("si-requests-body");
    body.replaceChildren();
    if (!rows.length) {
      const tr = el("tr");
      const cell = el("td", "muted", "درخواستی در انتظار نیست");
      cell.colSpan = 4;
      tr.appendChild(cell);
      body.appendChild(tr);
      return;
    }
    rows.forEach(function (row) {
      const tr = el("tr");
      tr.appendChild(el("td", "", (row.customer_name ? row.customer_name + " · " : "") + row.customer));
      const user = el("td", "", row.panel_username);
      user.setAttribute("dir", "ltr");
      tr.appendChild(user);
      tr.appendChild(el("td", "muted", new Date(row.expires_at).toLocaleDateString("fa-IR")));
      const cell = el("td", "actions");
      const approve = requestButton("تأیید", "", function () { decide(card, row.id, "approve", [approve, reject]); }, "check");
      const reject = requestButton("رد", "btn-ghost", function () { decide(card, row.id, "reject", [approve, reject]); }, "close");
      cell.appendChild(approve);
      cell.appendChild(reject);
      tr.appendChild(cell);
      body.appendChild(tr);
    });
  }

  async function loadRequests(card) {
    try {
      const res = await fetch(card.dataset.url, {credentials: "same-origin", headers: {Accept: "application/json"}});
      if (!res.ok) return; // no approval permission: the card stays hidden
      const data = await res.json();
      card.hidden = false;
      renderRequests(card, data.requests || []);
    } catch (err) {
      card.hidden = true;
    }
  }

  function boot() {
    const root = document.getElementById("si-panel");
    if (!root) return;
    loadStatus(root, false);
    const requests = document.getElementById("si-requests");
    if (requests) loadRequests(requests);
    document.getElementById("si-refresh").addEventListener("click", function () { loadStatus(root, true); });
    document.getElementById("si-test").addEventListener("click", function () { runTest(root); });
    document.getElementById("si-link").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); runTest(root); }
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
