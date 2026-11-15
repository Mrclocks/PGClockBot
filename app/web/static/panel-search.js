/* Sidebar search: asks /panel-search for pages and settings the signed-in staff may open
   and lists them under the box. Results are inserted with textContent only. */
(function (root) {
  "use strict";

  const DEBOUNCE_MS = 200;
  const MIN_QUERY = 2;

  /** Move the highlighted result by delta, wrapping around; -1 means none. */
  function nextIndex(current, delta, length) {
    if (length <= 0) return -1;
    if (current < 0) return delta > 0 ? 0 : length - 1;
    return (current + delta + length) % length;
  }

  /** Only same-origin panel paths are followed. */
  function isSafeHref(href) {
    return typeof href === "string" && href.startsWith("/") && !href.startsWith("//");
  }

  function normalizeQuery(text) {
    return String(text == null ? "" : text).trim().slice(0, 60);
  }

  /** The "/" key, by what it types or, on the Persian layout, by its place. Latin layouts that put
      another sign there (German "-", French "!") type that sign instead of opening search. */
  function isSlashKey(key, code) {
    if (key === "/") return true;
    return code === "Slash" && (key === "." || /[^\x00-\x7f]/.test(key));
  }

  /** Ctrl/⌘+K opens search from anywhere, "/" only while nothing is being typed; neither while a modal
      is open, since focus must stay inside it. Physical key codes keep both working on the Persian layout. */
  function isSearchHotkey(event, context) {
    if (!event || event.defaultPrevented || event.isComposing) return false;
    if (context && context.modalOpen) return false;
    const key = String(event.key || "").toLowerCase();
    const code = String(event.code || "");
    if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey) {
      return key === "k" || code === "KeyK";
    }
    if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return false;
    if (!isSlashKey(key, code)) return false;
    return !(context && context.typing);
  }

  function isTypingTarget(el) {
    if (!el || !el.tagName) return false;
    return /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || Boolean(el.isContentEditable);
  }

  function installHotkey(doc) {
    doc.addEventListener("keydown", (event) => {
      const context = {
        typing: isTypingTarget(event.target),
        modalOpen: doc.body.classList.contains("modal-open") || Boolean(doc.querySelector(".ui-modal.open")),
      };
      if (!isSearchHotkey(event, context)) return;
      // The header search on phones, the sidebar search on desktop: whichever is on screen.
      const input = [...doc.querySelectorAll("[data-panel-search-input]")].find((el) => el.getClientRects().length);
      if (!input) return;
      event.preventDefault();
      input.focus();
      input.select();
    });
  }

  function renderHits(doc, box, hits) {
    box.textContent = "";
    const safe = (hits || []).filter((h) => h && isSafeHref(h.href));
    if (!safe.length) {
      const empty = doc.createElement("p");
      empty.className = "side-search-empty";
      empty.textContent = "موردی پیدا نشد.";
      box.appendChild(empty);
      return 0;
    }
    safe.forEach((hit, i) => {
      const link = doc.createElement("a");
      link.className = "side-search-hit";
      link.href = hit.href;
      link.id = `panel-search-hit-${i}`;
      link.setAttribute("role", "option");
      const label = doc.createElement("span");
      label.className = "side-search-label";
      label.textContent = hit.label;
      const where = doc.createElement("span");
      where.className = "side-search-where";
      where.textContent = hit.where;
      link.append(label, where);
      box.appendChild(link);
    });
    return safe.length;
  }

  function bind(wrap) {
    const input = wrap.querySelector("[data-panel-search-input]");
    const box = wrap.querySelector("[data-panel-search-results]");
    if (!input || !box) return;
    const clear = wrap.querySelector("[data-panel-search-clear]");
    let timer = 0;
    let active = -1;
    let seq = 0;

    function close() {
      box.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    }

    /** Empty the field and let it fold back (the phone header field is open only while focused or filled). */
    function collapse() {
      clearTimeout(timer);
      seq++;
      input.value = "";
      close();
      if (clear) clear.blur();
      input.blur();
    }

    function highlight(index) {
      const hits = box.querySelectorAll(".side-search-hit");
      hits.forEach((h, i) => h.classList.toggle("is-active", i === index));
      active = index;
      if (index >= 0 && hits[index]) input.setAttribute("aria-activedescendant", hits[index].id);
      else input.removeAttribute("aria-activedescendant");
    }

    async function run(query) {
      const mine = ++seq;
      try {
        const res = await fetch(`/panel-search?q=${encodeURIComponent(query)}`, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (!res.ok) throw new Error(String(res.status));
        const data = await res.json();
        if (mine !== seq) return;
        renderHits(document, box, data.results);
      } catch (err) {
        if (mine !== seq) return;
        box.textContent = "";
        const fail = document.createElement("p");
        fail.className = "side-search-empty";
        fail.textContent = "جستجو انجام نشد. دوباره تلاش کنید.";
        box.appendChild(fail);
      }
      box.hidden = false;
      input.setAttribute("aria-expanded", "true");
      active = -1;
    }

    input.addEventListener("input", () => {
      clearTimeout(timer);
      const query = normalizeQuery(input.value);
      if (query.length < MIN_QUERY) {
        seq++;
        close();
        return;
      }
      timer = setTimeout(() => run(query), DEBOUNCE_MS);
    });
    input.addEventListener("keydown", (event) => {
      const hits = box.querySelectorAll(".side-search-hit");
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (box.hidden || !hits.length) return;
        event.preventDefault();
        highlight(nextIndex(active, event.key === "ArrowDown" ? 1 : -1, hits.length));
      } else if (event.key === "Enter" && active >= 0 && hits[active]) {
        event.preventDefault();
        hits[active].click();
      } else if (event.key === "Escape") {
        // First Escape closes the results; with none open, the phone header field folds back into its circle.
        if (!box.hidden || !clear) close();
        else collapse();
      }
    });
    document.addEventListener("click", (event) => {
      if (!wrap.contains(event.target)) close();
    });
    if (clear) {
      // The press must not blur the field first: an empty field would fold at once, the × would stop taking
      // pointer events, and the rest of the tap would land on the field under it and open it again.
      clear.addEventListener("mousedown", (event) => event.preventDefault());
      if (root.PanelTap) root.PanelTap.bind(clear, collapse);
      else clear.addEventListener("click", collapse);
    }
  }

  const api = { nextIndex, isSafeHref, normalizeQuery, renderHits, isSearchHotkey };
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PanelSearch = api;
    document.querySelectorAll("[data-panel-search]").forEach(bind);
    installHotkey(document);
  }
})(typeof window !== "undefined" ? window : globalThis);
