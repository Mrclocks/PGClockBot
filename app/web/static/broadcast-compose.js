/* Group-message composer: keeps the segment tab on the chosen segment's group, shows each segment's default day
   window, and asks the server how many people the audience reaches (debounced; the latest answer wins). The send
   button and the confirm dialog say the count. The server validates and counts again on send. «ارسال آزمایشی»
   posts the form to the test URL and shows the answer under the button, so the composed message stays. */
(function (root) {
  "use strict";

  const DEBOUNCE_MS = 300;
  const AUDIENCE_FIELDS = ["segment", "days", "plan_id", "min_wallet"];
  const TEXT_MAX = 4000;
  const PHOTO_TYPES = ["image/jpeg", "image/png", "image/webp"];
  const VIDEO_TYPES = ["video/mp4"];

  /** "photo", "video" or null for a chosen file (by its type, then its extension). */
  function mediaKind(file) {
    if (!file) return null;
    const type = String(file.type || "").toLowerCase();
    if (PHOTO_TYPES.indexOf(type) >= 0) return "photo";
    if (VIDEO_TYPES.indexOf(type) >= 0) return "video";
    const name = String(file.name || "").toLowerCase();
    if (/\.(jpe?g|png|webp)$/.test(name)) return "photo";
    if (/\.mp4$/.test(name)) return "video";
    return null;
  }

  /** The text field's rules: optional caption with media, required message without. */
  function textRules(hasMedia, captionMax) {
    return hasMedia
      ? { required: false, maxLength: Number(captionMax) || 1024 }
      : { required: true, maxLength: TEXT_MAX };
  }

  function faInt(value) {
    const numbers = root.PanelNumbers;
    return numbers && numbers.formatInt ? numbers.formatInt(value) : String(Math.round(Number(value) || 0));
  }

  /** Query string of the audience fields that are set (``notice`` only when switched on). */
  function audienceQuery(values) {
    const params = new URLSearchParams();
    AUDIENCE_FIELDS.forEach((name) => {
      const value = String((values && values[name]) || "").trim();
      if (value) params.set(name, value);
    });
    if (values && values.notice) params.set("notice", "1");
    return params.toString();
  }

  /** Text of the send button for ``count`` recipients (null while unknown). */
  function sendLabel(count) {
    if (count == null) return "ارسال پیام";
    if (count <= 0) return "مخاطبی ندارد";
    return "ارسال برای " + faInt(count) + " نفر";
  }

  /** The confirm dialog's question for this audience. */
  function confirmText(label, count) {
    const who = label ? "«" + label + "»" : "گروه انتخاب‌شده";
    const size = count == null ? "" : " (" + faInt(count) + " نفر)";
    return "پیام برای " + who + size + " در صف ارسال قرار بگیرد؟ پیامِ فرستاده‌شده پس گرفته نمی‌شود.";
  }

  /** Hint under the day field for a segment whose default window is ``days``. */
  function daysHint(days) {
    return days ? "خالی بماند: " + faInt(days) + " روز" : "";
  }

  /** The status line after a test send: the server's message, or a safe fallback. */
  function testStatus(ok, data) {
    const text = (data && (ok ? data.message : data.error)) || "";
    if (text) return { ok, text };
    return { ok: false, text: "ارسال آزمایشی انجام نشد؛ اتصال را بررسی کنید." };
  }

  function bindTest(form) {
    const button = form.querySelector("[data-bc-test]");
    const status = form.querySelector("[data-bc-test-status]");
    const url = form.getAttribute("data-test-url");
    if (!button || !status || !url) return;

    function show(result) {
      status.classList.toggle("is-ok", result.ok);
      status.classList.toggle("is-error", !result.ok);
      status.textContent = result.text;
    }

    button.addEventListener("click", async () => {
      button.disabled = true;
      status.classList.remove("is-ok", "is-error");
      status.textContent = "در حال ارسال آزمایشی…";
      try {
        const response = await fetch(url, { method: "POST", body: new FormData(form) });
        let data = null;
        try { data = await response.json(); } catch (_err) { data = null; }
        show(testStatus(response.ok, data));
      } catch (_err) {
        show(testStatus(false, null));
      } finally {
        button.disabled = false;
      }
    });
  }

  function bind(form) {
    bindTest(form);
    const segment = form.querySelector("#bc-segment");
    const group = form.querySelector("[data-bc-group]");
    const days = form.querySelector("[data-bc-days]");
    const daysHintEl = form.querySelector("[data-bc-days-hint]");
    const reach = form.querySelector("[data-bc-reach]");
    const countEl = form.querySelector("[data-bc-count]");
    const labelEl = form.querySelector("[data-bc-label]");
    const send = form.querySelector("[data-bc-send]");
    const notice = form.querySelector("#bc-notice");
    const url = form.getAttribute("data-audience-url");
    if (!segment || !reach || !url) return;
    let timer = null;
    let seq = 0;

    function values() {
      const out = { segment: segment.value, notice: Boolean(notice && notice.checked) };
      // The panel moves a select's name onto a mirror input; the control itself keeps the value.
      form.querySelectorAll("[data-bc-days], [data-bc-filter]").forEach((el) => {
        const name = el.getAttribute("name") || el.getAttribute("data-ui-select-name");
        if (name && !el.closest("[hidden]")) out[name] = el.value;
      });
      return out;
    }

    function show(state, count, label) {
      reach.classList.toggle("is-loading", state === "loading");
      reach.classList.toggle("is-error", state === "error");
      countEl.textContent = state === "ok" ? faInt(count) : "…";
      labelEl.textContent = label;
      if (send) {
        send.textContent = sendLabel(state === "ok" ? count : null);
        send.disabled = state === "ok" && count <= 0;
      }
      form.setAttribute("data-confirm", confirmText(state === "ok" ? label : "", state === "ok" ? count : null));
    }

    async function refresh() {
      const mine = ++seq;
      show("loading", null, "در حال شمارش مخاطبان…");
      try {
        const response = await fetch(url + "?" + audienceQuery(values()), { credentials: "same-origin", headers: { Accept: "application/json" } });
        const data = await response.json();
        if (mine !== seq) return;
        if (!response.ok) show("error", null, data.error || "شمارش انجام نشد");
        else show("ok", data.count, data.label || "");
      } catch (_err) {
        if (mine === seq) show("error", null, "شمارش انجام نشد؛ اتصال را بررسی کنید.");
      }
    }

    function schedule() {
      clearTimeout(timer);
      timer = setTimeout(refresh, DEBOUNCE_MS);
    }

    function syncSegment() {
      const option = segment.selectedOptions[0];
      if (!option) return;
      if (group && group.value !== option.getAttribute("data-group")) {
        group.value = option.getAttribute("data-group");
        group.dispatchEvent(new Event("change", { bubbles: true }));
      }
      if (days) days.placeholder = faInt(option.getAttribute("data-days") || 0);
      if (daysHintEl) daysHintEl.textContent = daysHint(Number(option.getAttribute("data-days")));
    }

    segment.addEventListener("change", () => { syncSegment(); schedule(); });
    form.addEventListener("input", (event) => { if (event.target.matches("[data-bc-days], [data-bc-filter]")) schedule(); });
    form.addEventListener("change", (event) => { if (event.target.matches("[data-bc-filter], #bc-notice")) schedule(); });
    syncSegment();
    refresh();
    bindMedia(form);
  }

  /** Preview the chosen photo or video above the message and relax the text rules for a caption. */
  function bindMedia(form) {
    const input = form.querySelector("[data-bc-media]");
    const text = form.querySelector("[data-bc-text]");
    const box = form.querySelector("[data-bc-media-preview]");
    const clear = form.querySelector("[data-bc-media-clear]");
    if (!input || !box) return;
    const img = box.querySelector("[data-bc-media-img]");
    const video = box.querySelector("[data-bc-media-video]");
    let objectUrl = "";

    function sync() {
      const file = input.files && input.files[0];
      const kind = mediaKind(file);
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = kind ? URL.createObjectURL(file) : "";
      if (img) { img.hidden = kind !== "photo"; if (kind === "photo") img.src = objectUrl; else img.removeAttribute("src"); }
      if (video) { video.hidden = kind !== "video"; if (kind === "video") video.src = objectUrl; else video.removeAttribute("src"); }
      box.hidden = !kind;
      if (clear) clear.hidden = !file;
      if (text) {
        const rules = textRules(Boolean(kind), form.getAttribute("data-caption-max"));
        text.required = rules.required;
        text.maxLength = rules.maxLength;
      }
    }

    input.addEventListener("change", sync);
    if (clear) {
      clear.addEventListener("click", () => {
        input.value = "";
        input.dispatchEvent(new Event("change", { bubbles: true }));
      });
    }
    sync();
  }

  const api = { audienceQuery, sendLabel, confirmText, daysHint, testStatus, mediaKind, textRules, bind };
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.BroadcastCompose = api;
    const start = () => document.querySelectorAll("[data-bc-form]").forEach(bind);
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
  }
})(typeof window !== "undefined" ? window : globalThis);
