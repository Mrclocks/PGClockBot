/* «⋯» menu of the page header (DESIGN_SYSTEM.md §7). On phones the page's secondary actions and its help sit in a
   small sheet under the capsule; this opens and closes it. On desktop the toggle is hidden and the actions stay
   inline, so nothing here runs. Disclosure pattern: the button carries aria-expanded and aria-controls. */
(function (root) {
  "use strict";

  /** Next open state of the menu after an event; pure so it can be tested without a DOM. */
  function nextOpenState(isOpen, event) {
    switch (event) {
      case "toggle":
        return !isOpen;
      case "escape":
      case "outside":
      case "item":
      case "layout":
        return false;
      default:
        return isOpen;
    }
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { nextOpenState: nextOpenState };
  }
  if (typeof document === "undefined") return;

  function init() {
    var toggle = document.querySelector("[data-page-more-toggle]");
    var menu = toggle && document.getElementById(toggle.getAttribute("aria-controls"));
    if (!toggle || !menu) return;
    var isOpen = false;

    function items() {
      return Array.prototype.filter.call(menu.querySelectorAll("a, button"), function (el) {
        return !el.disabled && el.offsetParent !== null;
      });
    }

    function apply(event, opts) {
      var next = nextOpenState(isOpen, event);
      if (next === isOpen) return;
      isOpen = next;
      menu.classList.toggle("is-open", isOpen);
      toggle.setAttribute("aria-expanded", String(isOpen));
      if (isOpen && opts && opts.keyboard) {
        var first = items()[0];
        if (first) first.focus();
      }
      if (!isOpen && opts && opts.returnFocus) toggle.focus();
    }

    toggle.addEventListener("click", function (e) {
      e.stopPropagation();
      // A click with detail 0 comes from the keyboard (Enter/Space): move focus into the menu.
      apply("toggle", { keyboard: e.detail === 0 });
    });
    menu.addEventListener("click", function (e) {
      if (isOpen && e.target.closest("a, button")) apply("item");
    });
    document.addEventListener("click", function (e) {
      if (isOpen && !menu.contains(e.target)) apply("outside");
    });
    document.addEventListener("keydown", function (e) {
      if (isOpen && e.key === "Escape") apply("escape", { returnFocus: true });
    });
    var phone = root.matchMedia ? root.matchMedia("(max-width: 900px)") : null;
    if (phone) {
      var onLayout = function () { apply("layout"); };
      if (phone.addEventListener) phone.addEventListener("change", onLayout);
      else if (phone.addListener) phone.addListener(onLayout);
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})(typeof window !== "undefined" ? window : this);
