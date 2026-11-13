const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function setup(connected = false) {
  const timers = new Map(), files = new Map(), events = {};
  let now = 0, next = 0;
  const svg = () => ({ cloneNode: svg, querySelectorAll: () => [], replaceWith() {} });
  const tiles = [1,2,3,4].map(port => ({
    dataset: {}, label: {}, attributes: {},
    querySelector(selector) { return selector === "svg" ? svg() : this.label; },
    classList: { contains: () => port === 2 },
    setAttribute(key, value) { this.attributes[key] = value; }
  }));
  const title = {};
  const document = {
    hidden: false,
    querySelector(selector) { return tiles[selector.includes("apple-tv") ? 0 : Number(selector.match(/hdmi-(\d)/)[1])-1]; },
    getElementById: () => title,
    addEventListener(name, handler) { events[name] = handler; }
  };
  const window = {
    crypto: { getRandomValues(bytes) { return bytes.fill(++next); } },
    setTimeout(fn, delay) { timers.set(++next, { fn, delay }); return next; },
    clearTimeout(id) { timers.delete(id); },
    tizen: connected ? { filesystem: { openFile(name) {
      return { close() {}, readString: () => files.get(name) || "",
        writeString(value) { files.set(name, value); } };
    } } } : undefined
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../sources.js"),"utf8"),
    { window, document, console, Date: { now: () => now } });
  return { api: window.LauncherSources, tiles, title, document, files, events, timers,
    tick(delay) {
      const [id, timer] = [...timers].find(([, t]) => t.delay === delay);
      timers.delete(id); now += delay; timer.fn();
    }
  };
}

test("preview and unavailable worker retain Blu-ray artwork and accessible port labels", () => {
  const h = setup();
  assert.equal(h.tiles[1].label.textContent, "Blu-ray");
  assert.equal(h.tiles[1].dataset.art, "disc");
  assert.equal(h.title.textContent, "Blu-ray");
  assert.equal(h.tiles[1].attributes["aria-label"], "Blu-ray, HDMI 2");
});

test("names follow the connected device; custom labels retain detected artwork", () => {
  const h = setup();
  const rows = [{ port: 2, connected: true, name: "Movie night", detectedName: "Blu-ray" }];
  assert.equal(h.api.presentation(2, rows).name, "Movie night");
  assert.equal(h.api.presentation(2, rows).icon, "disc");
  rows[0].name = "HDMI 2"; rows[0].detectedName = "Apple TV";
  assert.equal(h.api.presentation(2, rows).name, "Apple TV");
  assert.equal(h.api.presentation(2, rows).icon, "apple");
  rows[0].connected = false;
  assert.equal(h.api.presentation(2, rows).name, "Blu-ray");
});

test("wrong tokens, duplicate ports and malformed names cannot change labels", () => {
  const h = setup(), row = { port: 2, connected: true, name: "Blu-ray", detectedName: "" };
  for (const reply of [
    { token: "old", sources: [row] }, { token: "new", sources: [row,row] },
    { token: "new", sources: [{...row, name: "bad\u0000name"}] },
    { token: "new", sources: [{...row, port: 9}] }
  ]) assert.equal(h.api.validate(reply, "new"), null);
});

test("fresh replies update selected title; leaving Home cancels reads and stale replies", () => {
  const h = setup(true);
  const token = h.files.get("wgt-private/hdmi-sources.request").trim();
  h.files.set("wgt-private/hdmi-sources.reply", JSON.stringify({ token,
    sources: [{ port: 2, connected: true, name: "Cinema", detectedName: "Blu-ray" }] }));
  h.tick(250);
  assert.equal(h.title.textContent, "Cinema");
  h.tick(30000);
  assert.equal(h.title.textContent, "Cinema", "periodic refresh must not flash fallback labels");
  h.document.hidden = true; h.events.visibilitychange();
  assert.equal(h.timers.size, 0);
  h.document.hidden = false; h.events.visibilitychange();
  assert.equal(h.title.textContent, "Blu-ray");
  for (let i=0; i<80; i++) h.tick(250);
  assert.equal(h.title.textContent, "Blu-ray", "stale previous reply accepted");
  assert.equal([...h.timers.values()][0].delay, 30000);
});
