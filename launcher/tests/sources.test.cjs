const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function setup(connected = false) {
  const timers = new Map(), files = new Map(), events = {};
  let now = 0, next = 0;
  const counts = { reads: 0, text: 0, attributes: 0, clones: 0, replacements: 0 };
  function label(value) {
    return {
      get textContent() { return value; },
      set textContent(nextValue) { counts.text++; value = nextValue; }
    };
  }
  function svg(icon) {
    return { icon, querySelectorAll: () => [],
      cloneNode() { counts.clones++; return svg(icon); },
      replaceWith(nextDrawing) {
        const tile = tiles.find(tile => tile.drawing === this);
        counts.replacements++;
        tile.drawing = nextDrawing;
      }
    };
  }
  const tiles = ["Apple TV", "Blu-ray", "Arc Ultra", "HDMI 4"].map((name, index) => ({
    dataset: {}, label: label(name), drawing: svg(["apple","disc","audio","hdmi"][index]),
    attributes: { "aria-label": ["Apple TV, HDMI 1", "Blu-ray, HDMI 2",
      "HDMI 3, Arc Ultra, audio only, unavailable", "HDMI 4"][index] },
    querySelector(selector) { return selector === "svg" ? this.drawing : this.label; },
    classList: { contains: () => index === 1 },
    getAttribute(key) { return this.attributes[key]; },
    setAttribute(key, value) { counts.attributes++; this.attributes[key] = value; }
  }));
  const title = label("Blu-ray");
  const document = {
    hidden: false,
    querySelector(selector) { return tiles[selector.includes("apple-tv") ? 0 : Number(selector.match(/hdmi-(\d)/)[1])-1]; },
    getElementById: () => title,
    addEventListener(name, handler) { events[name] = handler; }
  };
  const window = {
    performance: { now: () => now },
    crypto: { getRandomValues(bytes) { return bytes.fill(++next); } },
    setTimeout(fn, delay) { timers.set(++next, { fn, delay, at: now + delay }); return next; },
    clearTimeout(id) { timers.delete(id); },
    tizen: connected ? { filesystem: { openFile(name) {
      return { close() {}, readString() { counts.reads++; return files.get(name) || ""; },
        writeString(value) { files.set(name, value); } };
    } } } : undefined
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../sources.js"),"utf8"),
    { window, document, console, Date: { now: () => now } });
  return { api: window.LauncherSources, tiles, title, document, files, events, timers, counts,
    advance(ms) {
      const end = now + ms;
      while (timers.size) {
        const [id, timer] = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
        if (timer.at > end) break;
        timers.delete(id); now = timer.at; timer.fn();
      }
      now = end;
    },
    reply(rows) {
      const token = files.get("wgt-private/hdmi-sources.request").trim();
      files.set("wgt-private/hdmi-sources.reply", JSON.stringify({ token, sources: rows }));
    }
  };
}

test("preview and unavailable worker retain Blu-ray artwork and accessible port labels", () => {
  const h = setup();
  assert.equal(h.tiles[1].label.textContent, "Blu-ray");
  assert.equal(h.tiles[1].drawing.icon, "disc");
  assert.equal(h.title.textContent, "Blu-ray");
  assert.equal(h.tiles[1].attributes["aria-label"], "Blu-ray, HDMI 2");
  assert.equal(h.counts.clones, 0);
  assert.equal(h.counts.replacements, 0);
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
  h.advance(250);
  assert.equal(h.title.textContent, "Cinema");
  h.advance(30000);
  assert.equal(h.title.textContent, "Cinema", "periodic refresh must not flash fallback labels");
  h.document.hidden = true; h.events.visibilitychange();
  assert.equal(h.timers.size, 0);
  const reads = h.counts.reads;
  h.advance(60000);
  assert.equal(h.counts.reads, reads);
  h.document.hidden = false; h.events.visibilitychange();
  assert.equal(h.title.textContent, "Blu-ray");
  h.advance(20000);
  assert.equal(h.title.textContent, "Blu-ray", "stale previous reply accepted");
  assert.equal([...h.timers.values()][0].delay, 30000);
});

test("repeated names leave labels, artwork and the selected title untouched", () => {
  const h = setup(true);
  const rows = [{ port: 2, connected: true, name: "Cinema", detectedName: "Apple TV" }];
  h.reply(rows); h.advance(250);
  assert.equal(h.tiles[1].attributes["aria-label"], "Cinema, HDMI 2");
  assert.equal(h.tiles[1].drawing.icon, "apple");
  assert.equal(h.tiles[0].drawing.icon, "apple", "the original artwork remains in place");
  const counts = { ...h.counts };
  h.advance(30000);
  h.reply(rows); h.advance(250);
  for (const key of ["text", "attributes", "clones", "replacements"]) {
    assert.equal(h.counts[key], counts[key], key);
  }
  h.advance(30000);
  const reads = h.counts.reads;
  h.advance(19999);
  assert.equal(h.title.textContent, "Cinema");
  h.advance(1);
  assert.equal(h.title.textContent, "Blu-ray");
  assert.equal(h.tiles[1].drawing.icon, "disc");
  assert.ok(h.counts.reads - reads <= 20, "a missing worker must not occupy the UI thread every 250 ms");
});

test("accepts replies during the normal worker cycle and at the end of the timeout", () => {
  for (const [replyAt, deadline] of [[3100, 3600], [19999, 20000]]) {
    const h = setup(true);
    h.advance(replyAt);
    h.reply([{ port: 2, connected: true, name: "Movie player", detectedName: "" }]);
    h.advance(deadline - replyAt);
    assert.equal(h.title.textContent, "Movie player");
  }
});
