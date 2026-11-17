/* Settings modals as list → section (DESIGN_SYSTEM.md §3). A modal with [data-modal-list] opens on its list of
   sections; a row shows that section in the same modal and the header's back button returns to the list. The server
   renders the first state, so this file only switches views, keeps the URL in step and resets on close. */
(function (root) {
  "use strict";

  /** Next view after an action; pure so it can be tested without a DOM. */
  function nextView(view, action, key) {
    switch (action) {
      case "open":
        return key ? { name: "section", key: key } : view;
      case "back":
      case "close":
        return { name: "list", key: "" };
      default:
        return view;
    }
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { nextView: nextView };
  }
  if (typeof document === "undefined") return;

  function setUrl(modal, params) {
    try {
      var url = new URL(root.location.href);
      (modal.getAttribute("data-modal-strip-keys") || "").split(",").forEach(function (k) {
        if (k.trim()) url.searchParams.delete(k.trim());
      });
      new URLSearchParams(params || "").forEach(function (v, k) { url.searchParams.set(k, v); });
      var qs = url.searchParams.toString();
      root.history.replaceState({}, "", url.pathname + (qs ? "?" + qs : "") + url.hash);
    } catch (err) {
      /* URL sync only lets a reload reopen the section; the modal works without it. */
    }
  }

  function init(modal) {
    var list = modal.querySelector("[data-modal-list-rows]");
    if (!list) return;
    var back = modal.querySelector("[data-modal-list-back]");
    var heading = modal.querySelector(".ui-modal-head .modal-title h2");
    var areaTitle = modal.getAttribute("data-modal-list-title") || "";
    var rows = Array.prototype.slice.call(list.querySelectorAll("[data-modal-list-open]"));
    var panels = Array.prototype.slice.call(modal.querySelectorAll("[data-modal-tab-panel]"));
    var shownPanel = panels.filter(function (p) { return !p.hidden; })[0];
    var view = list.hidden && shownPanel
      ? nextView({ name: "list", key: "" }, "open", shownPanel.getAttribute("data-modal-tab-panel"))
      : { name: "list", key: "" };

    function rowFor(key) {
      return rows.filter(function (r) { return r.getAttribute("data-modal-list-open") === key; })[0] || null;
    }

    function render(focusTarget) {
      var row = view.name === "section" ? rowFor(view.key) : null;
      list.hidden = !!row;
      panels.forEach(function (p) { p.hidden = !row || p.getAttribute("data-modal-tab-panel") !== view.key; });
      if (back) back.hidden = !row;
      if (heading) heading.textContent = row ? row.querySelector(".hub-label").textContent : areaTitle;
      if (typeof root.pinModalStickySave === "function") root.pinModalStickySave(modal);
      if (focusTarget && !modal.hidden) focusTarget.focus();
    }

    list.addEventListener("click", function (e) {
      var row = e.target.closest("[data-modal-list-open]");
      if (!row) return;
      view = nextView(view, "open", row.getAttribute("data-modal-list-open"));
      setUrl(modal, row.getAttribute("data-modal-list-url"));
      render(back);
    });
    if (back) {
      back.addEventListener("click", function () {
        var from = rowFor(view.key);
        view = nextView(view, "back");
        setUrl(modal, modal.getAttribute("data-modal-list-url"));
        render(from);
      });
    }
    // Closing the modal resets it, so the next opening starts on the list.
    new MutationObserver(function () {
      if (modal.hidden && view.name !== "list") { view = nextView(view, "close"); render(); }
    }).observe(modal, { attributes: true, attributeFilter: ["hidden"] });
  }

  function start() {
    Array.prototype.forEach.call(document.querySelectorAll(".ui-modal[data-modal-list]"), init);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})(typeof window !== "undefined" ? window : this);
