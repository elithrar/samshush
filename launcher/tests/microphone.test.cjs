const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = readFileSync(join(__dirname, "..", "microphone.js"), "utf8");

function home({ hidden = false, supported = true } = {}) {
  let now = 0;
  let timerId = 0;
  const timers = new Map();
  const requests = [];
  const events = {};
  const indicator = { hidden: true };
  const document = {
    hidden,
    getElementById: () => indicator,
    addEventListener(name, fn) { events[name] = fn; }
  };
  const window = {
    addEventListener(name, fn) { events[name] = fn; },
    clearTimeout(id) { timers.delete(id); },
    setTimeout(fn, delay) { timers.set(++timerId, { fn, at: now + delay }); return timerId; }
  };
  if (supported) window.tizen = { datacontrol: {
    getDataControlConsumer(provider, id, type) {
      assert.equal(provider, "http://samshush.local/datacontrol/provider/MicrophoneStatus");
      assert.equal(id, "status");
      assert.equal(type, "MAP");
      return { getValue(request, key, success, failure) {
        assert.equal(key, "recording");
        requests.push({ request, success, failure });
      } };
    }
  } };
  vm.runInNewContext(script, { window, document, performance: { now: () => now } });
  return {
    indicator, timers, requests, events,
    visible(visible) { document.hidden = !visible; events.visibilitychange(); },
    reply(value) { const r = requests.at(-1); r.success([value], r.request); },
    stall(ms) { now += ms; },
    tick() {
      const [id, timer] = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
      timers.delete(id); now = Math.max(now, timer.at); timer.fn();
    }
  };
}

test("only a fresh active reply shows the icon; idle, unknown and failures clear it", () => {
  const h = home();
  assert.equal(h.indicator.hidden, true);
  h.reply("active");
  assert.equal(h.indicator.hidden, false);
  for (const state of ["idle", "unknown", "RUNNING", true]) {
    h.tick(); h.reply(state);
    assert.equal(h.indicator.hidden, true);
    h.tick(); h.reply("active");
    assert.equal(h.indicator.hidden, false);
  }
  h.tick(); h.requests.at(-1).failure();
  assert.equal(h.indicator.hidden, true);
});

test("timeout, delayed replies and mismatched ids cannot show stale activity", () => {
  const h = home();
  h.reply("active"); h.tick();
  const old = h.requests.at(-1);
  h.tick(); // the previous active reading expires while the next query is pending
  assert.equal(h.indicator.hidden, true);
  h.tick(); // pending query times out
  assert.equal(h.indicator.hidden, true);
  old.success(["active"], old.request);
  assert.equal(h.indicator.hidden, true);
  h.tick();
  h.requests.at(-1).success(["active"], old.request);
  assert.equal(h.indicator.hidden, true);
  h.tick(); h.stall(2001); h.reply("active");
  assert.equal(h.indicator.hidden, true);
});

test("hidden and unsupported Home stay silent; resuming requires a new sample", () => {
  const preview = home({ supported: false });
  assert.equal(preview.indicator.hidden, true);
  assert.equal(preview.timers.size, 0);
  const h = home({ hidden: true });
  assert.equal(h.requests.length, 0);
  h.visible(true);
  const beforeHide = h.requests.at(-1);
  h.visible(false);
  assert.equal(h.timers.size, 0);
  h.visible(true);
  beforeHide.success(["active"], beforeHide.request);
  assert.equal(h.indicator.hidden, true);
  h.reply("active");
  assert.equal(h.indicator.hidden, false);
  h.events.pagehide();
  assert.equal(h.indicator.hidden, true);
  assert.equal(h.timers.size, 0);
});
