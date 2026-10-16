const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const events = {};
const pageEvents = {};
const timers = [];
class Form {
  constructor() { this.method = "post"; this.dataset = {}; this.elements = []; this.attributes = {}; }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
}
class Button {
  constructor() { this.type = "submit"; this.disabled = false; this.dataset = {}; }
}
const form = new Form();
// form.elements includes controls associated with form= even outside its DOM subtree.
const externalSubmit = new Button();
form.elements.push(externalSubmit);
let confirmed = true;
const context = {
  HTMLFormElement: Form, HTMLButtonElement: Button, HTMLInputElement: class {},
  document: {
    addEventListener(type, callback) { events[type] = callback; },
    querySelectorAll(selector) { return selector.startsWith("form") ? [form] : [externalSubmit]; },
  },
  window: {
    setTimeout(callback) { timers.push(callback); },
    addEventListener(type, callback) { pageEvents[type] = callback; },
    confirm() { return confirmed; },
  },
};
vm.runInNewContext(fs.readFileSync("estacao/static/js/form-guard.js", "utf8"), context);
for (const name of ["index.html", "admin_painel.html"]) {
  const html = fs.readFileSync(`estacao/templates/${name}`, "utf8");
  for (const [, pattern] of html.matchAll(/pattern="([^"]+)"/g)) {
    // HTML pattern uses Unicode sets (v); unescaped parentheses/hyphens disable validation.
    const regex = new RegExp(`^(?:${pattern})$`, "v");
    assert.equal(regex.test("(67) 99999-9999"), true);
    assert.equal(regex.test("+55 (67) 99999-9999"), true);
    assert.equal(regex.test("123"), false);
    assert.equal(regex.test("67abc999999999"), false);
  }
}
function submit(defaultPrevented = false) {
  const event = {target: form, defaultPrevented, preventDefault() { this.defaultPrevented = true; }};
  events.submit(event);
  return event;
}
assert.equal(submit(true).defaultPrevented, true);
assert.equal(form.dataset.submitting, undefined, "a rejected form can be corrected and resubmitted");
form.dataset.confirm = "Delete?";
confirmed = false;
assert.equal(submit().defaultPrevented, true);
assert.equal(form.dataset.submitting, undefined, "declining confirmation must not lock the form");
confirmed = true;
assert.equal(submit().defaultPrevented, false, "the first valid POST must proceed");
assert.equal(externalSubmit.disabled, false, "submitter value remains available for serialization");
assert.equal(submit().defaultPrevented, true, "a rapid duplicate POST must be blocked");
timers.forEach(callback => callback());
assert.equal(externalSubmit.disabled, true, "external submit buttons must also be blocked");
pageEvents.pageshow();
assert.equal(externalSubmit.disabled, false);
assert.equal(form.dataset.submitting, undefined);
assert.equal(submit().defaultPrevented, false, "returning from navigation restores the form");
console.log("Form submission, confirmation, duplicate blocking and navigation recovery: passed");
