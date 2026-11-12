/* Users page search: a pasted subscription link goes by POST to /users/find-by-link, never in the URL. */
(function (root) {
  "use strict";

  // A URL with a path (scheme://host/path): the shape of a subscription link, unlike a name or an id.
  const LINK_SHAPE = /^[a-z][a-z0-9+.-]*:\/\/[^\s/]+\/\S+$/i;

  /** True when the search text should be looked up as a subscription link. */
  function looksLikeSubscriptionLink(value) {
    return LINK_SHAPE.test(String(value || "").trim());
  }

  /** Send a pasted link through ``linkForm`` (POST) instead of the GET search. */
  function bind(searchForm, linkForm) {
    if (!searchForm || !linkForm) return;
    const query = searchForm.querySelector('input[name="q"]');
    const field = linkForm.querySelector('input[name="link"]');
    if (!query || !field) return;
    searchForm.addEventListener("submit", (event) => {
      const value = query.value.trim();
      if (!looksLikeSubscriptionLink(value)) return;
      event.preventDefault();
      field.value = value;
      query.value = "";
      if (typeof linkForm.requestSubmit === "function") linkForm.requestSubmit();
      else linkForm.submit();
    });
  }

  const api = { looksLikeSubscriptionLink, bind };
  if (typeof module === "object" && module.exports) {
    module.exports = api;
    return;
  }
  root.UsersLinkSearch = api;
  if (root.document) {
    bind(
      root.document.querySelector("form[data-users-search]"),
      root.document.getElementById("users-link-form"),
    );
  }
})(typeof window !== "undefined" ? window : globalThis);
