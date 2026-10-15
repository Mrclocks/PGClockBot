/* «وضعیت اتصال» settings tab: runtime switch, dedicated account and host management.
   All server text is inserted with textContent, never as HTML. */
(function () {
  "use strict";

  const root = document.getElementById("cm-panel");
  if (!root) return;

  const base = root.dataset.base;
  const CORE_TILES = {
    not_configured: ["is-err", "تنظیم نشده"],
    configured: ["is-neutral", "تأیید نشده"],
    ready: ["is-ok", "تأیید شد"],
    invalid: ["is-err", "نامعتبر"],
  };
  const state = {nextId: null, editing: null, pickerNext: null};

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

  function byId(id) { return document.getElementById(id); }

  function formatTime(iso) {
    if (!iso) return "—";
    const when = new Date(iso);
    return when.toLocaleDateString("fa-IR") + " " + when.toLocaleTimeString("fa-IR");
  }

  function showMessage(ok, text) {
    const box = byId("cm-msg");
    box.replaceChildren();
    if (!text) return;
    const flash = el("div", "flash " + (ok ? "ok" : "err"));
    const copy = el("div", "flash-copy");
    copy.appendChild(el("strong", "", text));
    flash.appendChild(copy);
    box.appendChild(flash);
  }

  async function request(path, method, params) {
    const options = {method: method, credentials: "same-origin", headers: {Accept: "application/json"}};
    if (method === "POST") {
      options.headers["X-CSRF-Token"] = csrfToken();
      options.body = new URLSearchParams(params || {});
    }
    try {
      const res = await fetch(base + path, options);
      return await res.json();
    } catch (err) {
      return {ok: false, error: "ارتباط با سرور برقرار نشد؛ دوباره تلاش کنید"};
    }
  }

  function confirmAction(options) {
    if (typeof window.panelConfirm !== "function") return Promise.resolve(window.confirm(options.message));
    return window.panelConfirm(options).then(function (result) { return !!(result && result.ok); });
  }

  function tile(tone, title, lines) {
    const box = el("div", "bot-conn " + tone);
    box.setAttribute("role", "listitem");
    const text = el("span", "bot-conn-text");
    text.appendChild(el("span", "bot-conn-k", title));
    lines.filter(Boolean).forEach(function (line) { text.appendChild(el("span", "bot-conn-cap muted", line)); });
    box.appendChild(text);
    return box;
  }

  function renderTiles(data) {
    const row = byId("cm-conn");
    row.replaceChildren();
    row.appendChild(tile(data.enabled ? "is-ok" : "is-neutral", "پایش", [data.enabled ? "فعال" : "متوقف"]));
    const core = CORE_TILES[data.core] || CORE_TILES.not_configured;
    row.appendChild(tile(core[0], "هستهٔ Xray", [core[1]]));
    row.appendChild(tile(data.has_account ? "is-ok" : "is-err", "حساب پایش",
      [data.has_account ? data.account_username : "تنظیم نشده"]));
    const synced = data.catalog_synced_at ? formatTime(data.catalog_synced_at) : "هنوز همگام نشده";
    row.appendChild(tile(data.catalog_fresh ? "is-ok" : "is-neutral", "همگام‌سازی هاست‌ها", [synced]));
  }

  function button(label, className, onClick) {
    const node = el("button", "btn " + className, label);
    node.type = "button";
    node.addEventListener("click", onClick);
    return node;
  }

  function publishSwitch(target) {
    const wrap = el("label", "ui-switch");
    const input = el("input");
    input.type = "checkbox";
    input.checked = target.is_public;
    input.disabled = !target.enabled;
    input.setAttribute("aria-label", "نمایش عمومی " + (target.public_name || "هاست " + target.host_id));
    input.addEventListener("change", function () { togglePublic(target, input); });
    wrap.appendChild(input);
    const track = el("span", "ui-switch-track");
    track.setAttribute("aria-hidden", "true");
    wrap.appendChild(track);
    return wrap;
  }

  function targetRow(target) {
    const tr = el("tr");
    const name = el("td", "", (target.public_name || "بدون نام عمومی") + " · #" + target.host_id);
    tr.appendChild(name);
    const status = el("td");
    status.appendChild(el("span", "badge " + target.tone, target.status_label));
    if (target.reason_label) status.appendChild(el("small", "muted", " " + target.reason_label));
    tr.appendChild(status);
    tr.appendChild(el("td", "", target.delay_ms === null ? "—" : target.delay_ms.toLocaleString("fa-IR") + " میلی‌ثانیه"));
    tr.appendChild(el("td", "muted", formatTime(target.checked_at)));
    const pub = el("td");
    pub.appendChild(publishSwitch(target));
    tr.appendChild(pub);
    const cell = el("td");
    const actions = el("div", "actions");
    cell.appendChild(actions);
    if (target.enabled) {
      const check = button(target.manual_pending ? "در صف" : "بررسی اکنون", "btn-ghost", function () { checkNow(target, check); });
      check.disabled = target.manual_pending;
      actions.appendChild(check);
      actions.appendChild(button("ویرایش", "btn-ghost", function () { openEdit(target); }));
      actions.appendChild(button("توقف", "btn-ghost", function () { pauseTarget(target); }));
    } else {
      actions.appendChild(button("انتخاب دوباره", "btn-ghost", function () { selectHost(target.host_id); }));
    }
    tr.appendChild(cell);
    return tr;
  }

  function renderTargets(data, append) {
    const body = byId("cm-targets-body");
    if (!append) body.replaceChildren();
    if (!append && !data.targets.length) {
      const tr = el("tr");
      const cell = el("td", "muted", "هنوز هاستی انتخاب نشده است");
      cell.colSpan = 6;
      tr.appendChild(cell);
      body.appendChild(tr);
    }
    data.targets.forEach(function (target) { body.appendChild(targetRow(target)); });
    state.nextId = data.next_id;
    byId("cm-more-row").hidden = data.next_id === null;
  }

  async function load(options) {
    const opts = options || {};
    const query = new URLSearchParams();
    if (opts.append && state.nextId !== null) query.set("after_id", String(state.nextId));
    if (opts.verify) query.set("verify", "1");
    const data = await request("/status?" + query.toString(), "GET");
    if (!data.ok) {
      showMessage(false, data.error || "بارگذاری وضعیت ناموفق بود");
      return;
    }
    if (!opts.append) {
      renderTiles(data);
      const toggle = byId("cm-enabled");
      toggle.checked = data.enabled;
      toggle.disabled = false;
      const username = byId("cm-username");
      if (document.activeElement !== username) username.value = data.account_username || "";
    }
    renderTargets(data, !!opts.append);
  }

  async function run(path, params, success) {
    const data = await request(path, "POST", params);
    showMessage(!!data.ok, data.ok ? success : data.error || "انجام نشد");
    await load();
    return !!data.ok;
  }

  async function toggleEnabled() {
    const toggle = byId("cm-enabled");
    const want = toggle.checked;
    if (!want) {
      const ok = await confirmAction({
        title: "توقف پایش", message: "پایش و نمایش عمومی همهٔ هاست‌ها متوقف شود؟", warn: true, confirmLabel: "توقف",
      });
      if (!ok) { toggle.checked = true; return; }
    }
    toggle.disabled = true;
    await run("/enabled", {enabled: want ? "1" : "0"}, want ? "پایش فعال شد" : "پایش متوقف شد");
  }

  async function saveAccount() {
    const username = (byId("cm-username").value || "").trim();
    if (!username) return;
    const ok = await confirmAction({
      title: "تغییر حساب پایش", message: "پایش خاموش و نمایش عمومی برداشته می‌شود. ادامه می‌دهید؟",
      warn: true, confirmLabel: "ذخیره",
    });
    if (ok) await run("/account", {username: username}, "حساب پایش ذخیره شد؛ پایش را دوباره روشن کنید");
  }

  async function togglePublic(target, input) {
    const want = input.checked;
    if (want) {
      const ok = await confirmAction({
        title: "نمایش عمومی", message: "نام و وضعیت این هاست برای مشتریان در ربات نمایش داده شود؟", confirmLabel: "نمایش",
      });
      if (!ok) { input.checked = false; return; }
    }
    input.disabled = true;
    await run("/targets/" + target.id + "/edit", {field: "is_public", value: want ? "1" : "0"},
      want ? "نمایش عمومی روشن شد" : "نمایش عمومی خاموش شد");
  }

  async function checkNow(target, node) {
    node.disabled = true;
    await run("/targets/" + target.id + "/check", {}, "بررسی در صف قرار گرفت");
  }

  async function pauseTarget(target) {
    const ok = await confirmAction({
      title: "توقف هاست", message: "پایش این هاست متوقف و از نمایش عمومی برداشته شود؟", warn: true, confirmLabel: "توقف",
    });
    if (ok) await run("/targets/" + target.id + "/pause", {}, "پایش این هاست متوقف شد");
  }

  function openEdit(target) {
    state.editing = target;
    byId("cm-edit-title").textContent = "ویرایش هاست #" + target.host_id;
    byId("cm-edit-name").value = target.public_name || "";
    byId("cm-edit-order").value = String(target.display_order);
    byId("cm-edit").hidden = false;
    byId("cm-edit-name").focus();
  }

  function closeEdit() {
    state.editing = null;
    byId("cm-edit").hidden = true;
  }

  async function saveEdit() {
    const target = state.editing;
    if (!target) return;
    const name = (byId("cm-edit-name").value || "").trim();
    const order = (byId("cm-edit-order").value || "").trim();
    const calls = [];
    if (name !== (target.public_name || "")) calls.push({field: "public_name", value: name});
    if (order !== String(target.display_order)) calls.push({field: "display_order", value: order});
    byId("cm-edit-save").disabled = true;
    let ok = true;
    for (const params of calls) {
      const data = await request("/targets/" + target.id + "/edit", "POST", params);
      if (!data.ok) { ok = false; showMessage(false, data.error || "انجام نشد"); break; }
    }
    byId("cm-edit-save").disabled = false;
    if (ok) {
      showMessage(true, calls.length ? "تغییرات ذخیره شد" : "تغییری برای ذخیره نیست");
      closeEdit();
    }
    await load();
  }

  async function selectHost(hostId) {
    const ok = await run("/targets", {host_id: String(hostId)}, "هاست انتخاب شد؛ نمایش عمومی ابتدا خاموش است");
    if (ok) byId("cm-picker").hidden = true;
  }

  function renderPicker(data, append) {
    const body = byId("cm-picker-body");
    if (!append) body.replaceChildren();
    if (!append && !data.hosts.length) {
      const tr = el("tr");
      const cell = el("td", "muted", "هاست فعال دیگری در دسترس نیست");
      cell.colSpan = 2;
      tr.appendChild(cell);
      body.appendChild(tr);
    }
    data.hosts.forEach(function (host) {
      const tr = el("tr");
      tr.appendChild(el("td", "", host.name + " · #" + host.id));
      const cell = el("td");
      const actions = el("div", "actions");
      actions.appendChild(button("انتخاب", "", function () { selectHost(host.id); }));
      cell.appendChild(actions);
      tr.appendChild(cell);
      body.appendChild(tr);
    });
    state.pickerNext = data.next_id;
    byId("cm-picker-more-row").hidden = data.next_id === null;
  }

  async function openPicker(append) {
    const query = append && state.pickerNext !== null ? "?after_id=" + state.pickerNext : "";
    const data = await request("/hosts" + query, "GET");
    if (!data.ok) {
      showMessage(false, data.error || "دریافت هاست‌ها ناموفق بود");
      return;
    }
    renderPicker(data, !!append);
    byId("cm-picker").hidden = false;
  }

  byId("cm-enabled").addEventListener("change", toggleEnabled);
  byId("cm-refresh").addEventListener("click", function () { load(); });
  byId("cm-verify-core").addEventListener("click", function () { load({verify: true}); });
  byId("cm-save-account").addEventListener("click", saveAccount);
  byId("cm-more").addEventListener("click", function () { load({append: true}); });
  byId("cm-add-host").addEventListener("click", function () { openPicker(false); });
  byId("cm-picker-more").addEventListener("click", function () { openPicker(true); });
  byId("cm-picker-close").addEventListener("click", function () { byId("cm-picker").hidden = true; });
  byId("cm-edit-save").addEventListener("click", saveEdit);
  byId("cm-edit-cancel").addEventListener("click", closeEdit);
  load();
})();
