"use strict";
const assert = require("node:assert/strict");
const { looksLikeSubscriptionLink, bind } = require("../app/web/static/users-link-search.js");

for (const link of [
  "https://sub.example.com/sub/AbcDef0123456789AbcDef0123456789",
  "  http://1.2.3.4:2096/sub/token  ",
  "HTTPS://Panel.Example.com/api/sub/x",
]) {
  assert.equal(looksLikeSubscriptionLink(link), true, link);
}
for (const text of ["", "علی رضایی", "@username", "123456789", "t.me/name", "https://example.com", "https:// /x", null]) {
  assert.equal(looksLikeSubscriptionLink(text), false, String(text));
}

function fakeForms(value) {
  const listeners = {};
  const query = { value };
  const field = { value: "" };
  const searchForm = {
    querySelector: () => query,
    addEventListener: (name, fn) => { listeners[name] = fn; },
  };
  let submitted = 0;
  const linkForm = { querySelector: () => field, requestSubmit: () => { submitted += 1; } };
  bind(searchForm, linkForm);
  let prevented = false;
  listeners.submit({ preventDefault: () => { prevented = true; } });
  return { query, field, prevented, submitted: () => submitted };
}

const link = fakeForms("https://sub.example.com/sub/tok123");
assert.equal(link.prevented, true);
assert.equal(link.field.value, "https://sub.example.com/sub/tok123");
assert.equal(link.query.value, "", "the link must not stay in the GET search box");
assert.equal(link.submitted(), 1);

const name = fakeForms("Ali");
assert.equal(name.prevented, false);
assert.equal(name.field.value, "");
assert.equal(name.submitted(), 0);

bind(null, null);
console.log("users_link_search tests passed");
