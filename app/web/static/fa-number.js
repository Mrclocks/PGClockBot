/* Figures as the panel shows them (DESIGN_SYSTEM.md §2): Persian digits, «٬» between thousands and «٫» as the
   decimal mark, the same as the server's web_numbers filters. Display text only; never feed the result back into a
   form value or a calculation. Loaded once in the page head and required directly by Node tests. */
(function (root) {
  "use strict";

  var DIGITS = "۰۱۲۳۴۵۶۷۸۹";

  function faDigits(value) {
    return String(value).replace(/[0-9]/g, function (d) { return DIGITS[Number(d)]; });
  }

  /** A number already written with ASCII digits («1,234.5») in its Persian form («۱٬۲۳۴٫۵»). */
  function faNumber(text) {
    // No lookbehind: older Safari cannot parse it, and a syntax error would stop the whole file.
    return faDigits(String(text).replace(/,/g, "٬")).replace(/([۰-۹])\.(?=[۰-۹])/g, "$1٫");
  }

  /** A whole amount grouped by thousands: 390000 → «۳۹۰٬۰۰۰». */
  function formatInt(value) {
    return faNumber(Math.round(Number(value) || 0).toLocaleString("en-US"));
  }

  var api = { faDigits: faDigits, faNumber: faNumber, formatInt: formatInt };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.PanelNumbers = api;
})(typeof window !== "undefined" ? window : this);
