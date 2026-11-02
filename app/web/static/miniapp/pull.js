/* Mini App pull-to-refresh: pull down at the top of a screen to load it again. */
(function (root) {
  "use strict";

  // The page moves half as far as the finger; past ARM_PX a release refreshes.
  const DAMPING = 0.5;
  const ARM_PX = 64;
  const MAX_PX = 96;

  /**
   * ``indicator`` is the spinner element; ``canStart()`` says whether a pull may begin
   * (no open sheet, app loaded); ``onRefresh()`` returns a promise. Listeners are passive,
   * so scrolling is never blocked.
   */
  function attach({ doc, indicator, canStart, onRefresh, scrollTop }) {
    const top = scrollTop || (() => root.scrollY || 0);
    let startY = null;
    let pulled = 0;
    let busy = false;

    function show(px) {
      pulled = px;
      indicator.style.transform = `translateY(${px}px) rotate(${Math.round(px * 3)}deg)`;
      indicator.style.opacity = String(Math.min(1, px / ARM_PX));
      indicator.classList.toggle("is-armed", px >= ARM_PX);
    }

    function reset() {
      startY = null;
      indicator.classList.remove("is-loading");
      show(0);
    }

    doc.addEventListener("touchstart", (event) => {
      const single = event.touches && event.touches.length === 1;
      startY = !busy && single && top() <= 0 && canStart() ? event.touches[0].clientY : null;
    }, { passive: true });

    doc.addEventListener("touchmove", (event) => {
      if (startY == null) return;
      const dy = event.touches[0].clientY - startY;
      show(dy > 0 && top() <= 0 ? Math.min(MAX_PX, dy * DAMPING) : 0);
    }, { passive: true });

    doc.addEventListener("touchend", async () => {
      if (startY == null) return;
      startY = null;
      if (pulled < ARM_PX) {
        reset();
        return;
      }
      busy = true;
      indicator.classList.add("is-loading");
      try {
        await onRefresh();
      } finally {
        busy = false;
        reset();
      }
    }, { passive: true });

    doc.addEventListener("touchcancel", reset, { passive: true });
  }

  root.PGMini = Object.assign(root.PGMini || {}, { pull: Object.freeze({ attach, ARM_PX, DAMPING }) });
})(typeof window === "object" ? window : globalThis);
