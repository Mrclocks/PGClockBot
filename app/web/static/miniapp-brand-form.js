/* «مینی‌اپ» settings tab: live preview of the brand colour; picking a custom colour selects it. */
(function () {
  "use strict";

  const DEFAULT_ACCENT = "#f97316";
  const HEX = /^#[0-9a-f]{6}$/i;
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
    const custom = root.querySelector("[data-accent-custom]");
    if (!picked) return DEFAULT_ACCENT;
    if (picked.value === "custom") return custom && HEX.test(custom.value) ? custom.value : DEFAULT_ACCENT;
    return picked.getAttribute("data-accent") || DEFAULT_ACCENT;
  }

  function bind(root) {
    const preview = root.querySelector("[data-accent-preview]");
    const custom = root.querySelector("[data-accent-custom]");
    const customOption = root.querySelector("[data-accent-custom-option]");
    const customRadio = customOption && customOption.querySelector("input");
    const update = () => {
      if (!preview) return;
      const colour = chosenColour(root);
      preview.style.setProperty("--preview", colour);
      preview.style.setProperty("--preview-ink", inkFor(colour));
    };
    root.addEventListener("change", update);
    if (custom && customRadio) {
      custom.addEventListener("input", () => {
        customRadio.checked = true;
        if (HEX.test(custom.value)) customOption.style.setProperty("--tag-color", custom.value);
        update();
      });
    }
    update();
  }

  function start() {
    document.querySelectorAll("[data-miniapp-accent]").forEach(bind);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
