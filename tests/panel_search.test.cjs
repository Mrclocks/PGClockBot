"use strict";
const assert = require("node:assert/strict");
const { nextIndex, isSafeHref, normalizeQuery, renderHits } = require("../app/web/static/panel-search.js");

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
console.log("panel_search tests passed");
