/* «مینی‌اپ» settings tab: live preview of the chosen preset brand colour. */
(function () {
  "use strict";

  const DEFAULT_ACCENT = "#f97316";
  const INK_DARK = "#1c1007";

  function luminance(hex) {
    const channel = (offset) => {
      const v = parseInt(hex.slice(offset, offset + 2), 16) / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
  }

  /** Same rule as the Mini App (miniapp/core.js): dark ink or white, whichever reads better. */
  function inkFor(hex) {
    const l = luminance(hex);
    return (l + 0.05) / (luminance(INK_DARK) + 0.05) >= 1.05 / (l + 0.05) ? INK_DARK : "#ffffff";
  }

  /** The colour the form would save, for the preview. */
  function chosenColour(root) {
    const picked = root.querySelector('input[name="miniapp_accent_color"]:checked');
    return (picked && picked.getAttribute("data-accent")) || DEFAULT_ACCENT;
  }

  function bind(root) {
    const preview = root.querySelector("[data-accent-preview]");
    const update = () => {
      if (!preview) return;
      const colour = chosenColour(root);
      preview.style.setProperty("--preview", colour);
      preview.style.setProperty("--preview-ink", inkFor(colour));
    };
    root.addEventListener("change", update);
    update();
  }

  function start() {
    document.querySelectorAll("[data-miniapp-accent]").forEach(bind);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
