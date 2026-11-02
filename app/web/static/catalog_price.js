/* Shared customer price rendering for temporary offers and permanent repricing. */
(function (root) {
  "use strict";
  function renderPrice(quote, money, esc) {
    const current = Number(quote.price);
    const previous = Number(quote.original_price);
    const hasOffer = quote.original_price != null && Number.isFinite(previous) && previous > current;
    // <bdi> keeps the amount and its currency in reading order inside RTL text.
    const price = '<b><bdi>' + esc(money(quote.price)) + '</bdi></b>';
    if (!hasOffer) return price;
    const end = new Date(quote.discount_ends_at);
    const deadline = Number.isFinite(end.getTime())
      ? new Intl.DateTimeFormat("fa-IR", {timeZone: "Asia/Tehran", dateStyle: "short", timeStyle: "short"}).format(end)
      : "";
    return '<s><bdi>' + esc(money(previous)) + '</bdi></s> ← ' + price +
      (deadline ? '<div class="muted">تخفیف تا ' + esc(deadline) + '</div>' : "");
  }
  const api = Object.freeze({renderPrice});
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.PGClockCatalogPrice = api;
})(typeof window === "object" ? window : globalThis);
