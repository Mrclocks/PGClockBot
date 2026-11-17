"use strict";
const assert = require("node:assert/strict");
const {
  nextIndex, isSafeHref, normalizeQuery, renderHits, isSearchHotkey,
} = require("../app/web/static/panel-search.js");

function fakeDoc() {
  const make = (tag) => ({
    tag, children: [], attrs: {}, className: "", textContent: "", href: "", id: "",
    setAttribute(k, v) { this.attrs[k] = v; },
    appendChild(c) { this.children.push(c); },
    append(...cs) { this.children.push(...cs); },
  });
  return { createElement: make };
}

assert.equal(nextIndex(-1, 1, 3), 0);
assert.equal(nextIndex(-1, -1, 3), 2);
assert.equal(nextIndex(2, 1, 3), 0);
assert.equal(nextIndex(0, -1, 3), 2);
assert.equal(nextIndex(0, 1, 0), -1);

assert.ok(isSafeHref("/settings?tab=pay"));
assert.ok(!isSafeHref("//evil.example"));
assert.ok(!isSafeHref("javascript:alert(1)"));
assert.ok(!isSafeHref("https://evil.example/"));

assert.equal(normalizeQuery("  کارت  "), "کارت");
assert.equal(normalizeQuery("x".repeat(80)).length, 60);

{
  const doc = fakeDoc();
  const box = doc.createElement("div");
  const n = renderHits(doc, box, [
    { label: "<img src=x onerror=1>", where: "تنظیمات", href: "/settings" },
    { label: "bad", where: "x", href: "javascript:alert(1)" },
  ]);
  assert.equal(n, 1);
  assert.equal(box.children.length, 1);
  assert.equal(box.children[0].children[0].textContent, "<img src=x onerror=1>");
  assert.equal(box.children[0].href, "/settings");
}
{
  const doc = fakeDoc();
  const box = doc.createElement("div");
  assert.equal(renderHits(doc, box, []), 0);
  assert.equal(box.children[0].textContent, "موردی پیدا نشد.");
}
{
  const idle = {typing: false, modalOpen: false};

  // Ctrl/⌘+K opens search from anywhere, even while typing in another field.
  assert.equal(isSearchHotkey({key: "k", ctrlKey: true}, idle), true);
  assert.equal(isSearchHotkey({key: "K", metaKey: true}, {typing: true, modalOpen: false}), true);
  // Never while a modal is open: focus must not leave it for the search behind it.
  assert.equal(isSearchHotkey({key: "k", ctrlKey: true}, {typing: false, modalOpen: true}), false);
  // Persian layout: the K key types «ن», the physical code still says KeyK.
  assert.equal(isSearchHotkey({key: "ن", code: "KeyK", ctrlKey: true}, idle), true);
  // Other combinations are left to the browser.
  assert.equal(isSearchHotkey({key: "k", ctrlKey: true, shiftKey: true}, idle), false);
  assert.equal(isSearchHotkey({key: "k", ctrlKey: true, altKey: true}, idle), false);
  assert.equal(isSearchHotkey({key: "k"}, idle), false);
  assert.equal(isSearchHotkey({key: "j", ctrlKey: true}, idle), false);

  // "/" only while nothing is being typed and no modal is open.
  assert.equal(isSearchHotkey({key: "/", code: "Slash"}, idle), true);
  assert.equal(isSearchHotkey({key: ".", code: "Slash"}, idle), true);
  assert.equal(isSearchHotkey({key: "/"}, {typing: true, modalOpen: false}), false);
  assert.equal(isSearchHotkey({key: "/"}, {typing: false, modalOpen: true}), false);
  assert.equal(isSearchHotkey({key: "?", code: "Slash", shiftKey: true}, idle), false);
  // Latin layouts with another sign on that key type the sign (German "-", French "!").
  assert.equal(isSearchHotkey({key: "-", code: "Slash"}, idle), false);
  assert.equal(isSearchHotkey({key: "!", code: "Slash"}, idle), false);
  // A layout with a non-Latin sign there still opens search; "/" opens it wherever it sits.
  assert.equal(isSearchHotkey({key: "ـ", code: "Slash"}, idle), true);
  assert.equal(isSearchHotkey({key: "/", code: "Digit7"}, idle), true);

  // Already handled, or mid-composition (IME), is never taken over.
  assert.equal(isSearchHotkey({key: "k", ctrlKey: true, defaultPrevented: true}, idle), false);
  assert.equal(isSearchHotkey({key: "/", isComposing: true}, idle), false);
  assert.equal(isSearchHotkey(null, idle), false);
}
console.log("panel_search tests passed");
