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
    let timer = 0;
    let active = -1;
    let seq = 0;

    function close() {
      box.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
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
        close();
      }
    });
    document.addEventListener("click", (event) => {
      if (!wrap.contains(event.target)) close();
    });
  }

  const api = { nextIndex, isSafeHref, normalizeQuery, renderHits };
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PanelSearch = api;
    document.querySelectorAll("[data-panel-search]").forEach(bind);
  }
})(typeof window !== "undefined" ? window : globalThis);
