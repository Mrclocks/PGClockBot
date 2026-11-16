"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");

class Element {
  constructor(tagName = "div") {
    this.tagName = tagName.toUpperCase();
    this.nodeType = 1;
    this.attributes = {};
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.className = "";
    this._text = "";
    this.classList = {
      contains: (name) => this.className.split(" ").includes(name),
      add: (...names) => names.forEach((name) => this.classList.toggle(name, true)),
      remove: (...names) => names.forEach((name) => this.classList.toggle(name, false)),
      toggle: (name, enabled) => {
        const classes = new Set(this.className.split(" ").filter(Boolean));
        if (enabled) classes.add(name);
        else classes.delete(name);
        this.className = [...classes].join(" ");
      },
    };
  }
  // Like the DOM: an element's text is its own text followed by its children's.
  get textContent() {return this._text + this.children.map((child) => child.textContent || "").join("");}
  set textContent(value) {this._text = String(value); this.children = [];}
  get childNodes() {return this.children;}
  setAttribute(name, value) {this.attributes[name] = value;}
  getAttribute(name) {return this.attributes[name] ?? null;}
  hasAttribute(name) {return name in this.attributes;}
  removeAttribute(name) {delete this.attributes[name];}
  append(...children) {this.children.push(...children);}
  appendChild(child) {this.append(child);}
  replaceChildren(...children) {this._text = ""; this.children = children;}
  after(node) {this.nextElementSibling = node;}
  matches(selector) {return selector === "input, textarea" && ["INPUT", "TEXTAREA"].includes(this.tagName);}
  closest(selector) {return selector === "[data-copy]" && this.hasAttribute("data-copy") ? this : null;}
  querySelector() {return null;}
  querySelectorAll() {return [];}
  addEventListener() {}
}

class TextArea extends Element {
  constructor(name = "s_welcome_text", value = "") {
    super("textarea");
    this.name = name;
    this.value = value;
    this.validity = {valid: true};
  }
}

class Input extends TextArea {
  constructor(name, value, type = "text") {
    super(name, value);
    this.tagName = "INPUT";
    this.type = type;
  }
}

function panel({value = "", specs = {welcome_text: {name: "نام", user_name: "نام کاربر"}}} = {}) {
  const control = new TextArea("s_welcome_text", value);
  const config = new Element("script");
  config.textContent = JSON.stringify(specs);
  const listeners = new Map();
  const fields = [control];
  const copied = [];
  const prompts = [];
  const timers = [];
  const observers = [];
  const document = {
    body: new Element("body"), documentElement: new Element("html"), readyState: "complete", cookie: "",
    getElementById: (id) => id === "template-variable-specs" ? config : null,
    createElement: (tag) => new Element(tag), querySelector: () => null,
    querySelectorAll: (selector) => selector === "input, textarea" ? fields : [],
    addEventListener: (name, fn) => listeners.set(name, [...(listeners.get(name) || []), fn]),
  };
  const navigator = {userAgent: "", clipboard: {writeText: async (text) => copied.push(text)}};
  const window = {
    addEventListener() {}, location: {href: "https://panel.example/settings"},
    matchMedia: () => ({matches: true, addEventListener() {}}),
    prompt: (label, text) => prompts.push([label, text]),
  };
  const context = vm.createContext({
    document, navigator, window, location: window.location, HTMLElement: Element,
    HTMLTextAreaElement: TextArea, HTMLInputElement: Input, HTMLSelectElement: class extends Element {},
    Node: {ELEMENT_NODE: 1},
    MutationObserver: class {constructor(callback) {observers.push(callback);} observe() {}},
    localStorage: {getItem: () => null, setItem() {}},
    setTimeout: (callback) => {timers.push(callback); return timers.length;}, clearTimeout() {},
    setInterval() {}, clearInterval() {}, requestAnimationFrame() {}, URL, console,
  });
  for (const file of ["panel.js", "template-variables.js"]) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, "../app/web/static", file), "utf8"), context);
  }
  function dispatch(name, target = control, detail) {
    const event = {target, detail, preventDefault() {}, stopPropagation() {}};
    for (const fn of listeners.get(name) || []) fn(event);
  }
  function badges(field = control) {return field.nextElementSibling.children[0].children;}
  function click(badge) {if (!badge.disabled) dispatch("click", badge);}
  return {control, badges, dispatch, click, copied, prompts, navigator, timers, fields, observers};
}

test("used variables stay green and unused field variables appear as neutral copy buttons", () => {
  const ui = panel({value: "سلام {name} {name} {unknown}"});
  const badges = ui.badges();
  assert.deepEqual(badges.map((badge) => badge.textContent), ["{name}", "{unknown}", "{user_name}"]);
  assert.deepEqual(badges.map((badge) => badge.className), [
    "template-variable-token is-valid", "template-variable-token is-invalid", "template-variable-token is-unused",
  ]);
  for (const badge of [badges[0], badges[2]]) {
    assert.equal(badge.tagName, "BUTTON");
    assert.equal(badge.type, "button");
    assert.equal(badge.dir, "ltr");
    assert.equal(badge.disabled, false);
    assert.equal(badge.getAttribute("data-copy"), badge.textContent);
    assert.match(badge.getAttribute("aria-label"), /کپی متغیر/);
  }
});

test("empty templates still show every supported variable", () => {
  const ui = panel();
  assert.equal(ui.badges().length, 2);
  assert.ok(ui.badges().every((badge) => badge.classList.contains("is-unused")));
  assert.equal(ui.control.nextElementSibling.hidden, false);
  assert.equal(ui.control.nextElementSibling.children[1].hidden, true);
  assert.equal(ui.control.getAttribute("aria-invalid"), null);
});

test("typing and deleting variables updates their state without duplicate feedback", () => {
  const ui = panel();
  const feedback = ui.control.nextElementSibling;
  ui.control.value = "{user_name}";
  ui.dispatch("input");
  assert.equal(ui.badges()[0].classList.contains("is-valid"), true);
  ui.control.value = "";
  ui.dispatch("change");
  assert.ok(ui.badges().every((badge) => badge.classList.contains("is-unused")));
  assert.equal(ui.control.nextElementSibling, feedback);
});

test("clicking used and unused variables copies the exact braces and restores the label", async () => {
  const ui = panel({value: "{name}"});
  for (const badge of ui.badges()) {
    const token = badge.textContent;
    ui.click(badge);
    await Promise.resolve();
    assert.equal(ui.copied.at(-1), token);
    assert.equal(badge.textContent, "کپی شد");
    ui.timers.at(-1)();
    assert.equal(badge.textContent, token);
  }
});

test("unsupported, nested, spaced and incomplete tokens cannot be copied", async () => {
  const ui = panel({value: "{unknown} {{name}} { name } } {name"});
  const invalid = ui.badges().filter((badge) => badge.classList.contains("is-invalid"));
  assert.equal(invalid.length, 5);
  for (const badge of invalid) {
    assert.equal(badge.disabled, true);
    assert.equal(badge.getAttribute("aria-disabled"), "true");
    assert.equal(badge.getAttribute("data-copy"), null);
    ui.click(badge);
    ui.dispatch("click", badge);
  }
  await Promise.resolve();
  assert.deepEqual(ui.copied, []);
  assert.deepEqual(ui.prompts, []);
  assert.equal(ui.control.getAttribute("aria-invalid"), "true");
  assert.match(ui.control.nextElementSibling.children[1].textContent, /پشتیبانی نمی‌شود/);
});

test("clipboard failure and unavailable clipboard offer the existing manual copy prompt", async () => {
  const ui = panel();
  ui.navigator.clipboard.writeText = async () => {throw new Error("denied");};
  ui.click(ui.badges()[0]);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(ui.prompts, [["کپی کنید:", "{name}"]]);
  assert.equal(ui.badges()[0].textContent, "{name}");
  delete ui.navigator.clipboard;
  ui.click(ui.badges()[1]);
  assert.deepEqual(ui.prompts.at(-1), ["کپی کنید:", "{user_name}"]);
});

test("dynamic gateway fields use only their field metadata", () => {
  const ui = panel({specs: {welcome_text: {name: "نام"}, gateway_link: {amount: "مبلغ"}}});
  const gateway = new Input("destination", "https://example.test/{amount}", "url");
  gateway.className = "pay-dest-gw-link";
  ui.fields.push(gateway);
  ui.dispatch("panel:dom-ready", gateway, {root: gateway});
  assert.deepEqual(ui.badges(gateway).map((badge) => badge.textContent), ["{amount}"]);
  assert.equal(ui.badges(gateway)[0].classList.contains("is-valid"), true);
  const unknown = new TextArea("other_field", "{name}");
  ui.dispatch("input", unknown);
  assert.equal(unknown.nextElementSibling, undefined);
});

test("invalid tokens and metadata remain text even when they contain hostile markup", () => {
  const hostile = '<img src=x onerror="alert(1)">';
  const ui = panel({value: "{" + hostile + "}", specs: {welcome_text: {name: hostile}}});
  assert.equal(ui.badges()[0].textContent, "{" + hostile + "}");
  // The token's only child is the plain-text <bdi> the script builds; hostile markup never becomes elements.
  assert.ok(ui.badges().every((badge) => badge.children.every((child) => child.tagName === "BDI" && child.children.length === 0)));
  assert.match(ui.badges()[1].title, /<img/);
  assert.equal(ui.badges()[0].getAttribute("data-copy"), null);
});

test("long supported tokens copy their full value instead of the shortened badge", async () => {
  const name = "name_" + "a".repeat(70);
  const ui = panel({specs: {welcome_text: {[name]: "نام"}}});
  const badge = ui.badges()[0];
  assert.equal(badge.textContent.length, 64);
  ui.click(badge);
  await Promise.resolve();
  assert.deepEqual(ui.copied, ["{" + name + "}"]);
});
