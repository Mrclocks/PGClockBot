/* Marks the body with .kbd-up while the phone's on-screen keyboard is up, so the bottom bar can step aside.
   A focused field alone is not enough: a field focused on load or typed into from a hardware keyboard
   leaves the screen as it was, and the bar should stay. The keyboard is up when a text field has focus and
   the visual viewport has lost at least KEYBOARD_MIN_PX of the height it had at this width. */
(function (root) {
  "use strict";

  const KEYBOARD_MIN_PX = 120;
  const NON_TEXT_INPUTS = new Set(["checkbox", "radio", "button", "submit", "reset", "range", "color", "file", "image", "hidden"]);

  function isTextEntry(el) {
    if (!el || !el.tagName) return false;
    if (el.isContentEditable) return true;
    if (el.tagName === "TEXTAREA") return true;
    if (el.tagName !== "INPUT") return false;
    return !NON_TEXT_INPUTS.has(String(el.type || "text").toLowerCase());
  }

  function isKeyboardUp(fullHeight, height, textFocused) {
    return Boolean(textFocused) && fullHeight - height >= KEYBOARD_MIN_PX;
  }

  /** Tracks the tallest viewport seen at the current width (rotation or zoom starts over). */
  function viewportTracker() {
    let width = -1;
    let full = 0;
    return function fullHeight(viewWidth, viewHeight) {
      if (viewWidth !== width) {
        width = viewWidth;
        full = viewHeight;
      }
      full = Math.max(full, viewHeight);
      return full;
    };
  }

  function install(win) {
    const doc = win.document;
    const view = win.visualViewport;
    if (!view) return;
    const fullAt = viewportTracker();
    let frame = 0;
    function update() {
      frame = 0;
      const full = fullAt(Math.round(view.width), view.height);
      doc.body.classList.toggle("kbd-up", isKeyboardUp(full, view.height, isTextEntry(doc.activeElement)));
    }
    function schedule() {
      if (!frame) frame = win.requestAnimationFrame(update);
    }
    view.addEventListener("resize", schedule);
    doc.addEventListener("focusin", schedule);
    doc.addEventListener("focusout", schedule);
    update();
  }

  const api = { isTextEntry, isKeyboardUp, viewportTracker, KEYBOARD_MIN_PX };
  if (typeof module === "object" && module.exports) module.exports = api;
  else install(root);
})(typeof window !== "undefined" ? window : globalThis);
