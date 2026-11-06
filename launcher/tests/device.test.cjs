const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "device.js"), "utf8");
const appleTV = { id: "apple-tv", type: "source", port: 1 };
const settings = { id: "settings", type: "settings" };
const art = { id: "art-mode", type: "art" };

function createDevice(options = {}) {
  const calls = [];
  const timers = new Map();
  let timerID = 0;
  let selectedSource = { type: "HDMI", number: 1 };
  const native = {
    ApplicationControlData: function (key, value) {
      this.key = key;
      this.value = Array.from(value);
    },
    ApplicationControl: function (operation, uri, mime, category, data) {
      Object.assign(this, { operation, uri, mime, category, data: Array.from(data) });
    },
    application: {
      launch(id, success, error) {
        calls.push({ method: "launch", id, success, error });
      },
      launchAppControl(control, id, success, error) {
        calls.push({ method: "launchAppControl", control, id, success, error });
      }
    },
    systeminfo: {
      getPropertyValue(property, success, error) {
        calls.push({ method: "getPropertyValue", property, success, error });
      }
    },
    tvwindow: {
      setSource(descriptor, success, error) {
        calls.push({ method: "setSource", descriptor, success, error });
      },
      getSource() {
        calls.push({ method: "getSource" });
        return selectedSource;
      }
    }
  };
  const window = {
    tizen: options.browser ? undefined : native,
    setTimeout(callback, delay) {
      const id = ++timerID;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeout(id) { timers.delete(id); }
  };
  vm.runInNewContext(source, { window, console }, { filename: "device.js" });

  return {
    device: window.LauncherDevice,
    native,
    calls,
    timers,
    select(descriptor) { selectedSource = descriptor; },
    latest(method) { return calls.findLast(call => call.method === method); },
    expire() {
      assert.equal(timers.size, 1, "one bounded action should be pending");
      const [id, timer] = timers.entries().next().value;
      assert.equal(timer.delay, 8000);
      timers.delete(id);
      timer.callback();
    }
  };
}

function discover(harness, descriptor = { type: "HDMI", number: 1 }) {
  const request = harness.latest("getPropertyValue");
  assert.equal(request.property, "VIDEOSOURCE");
  request.success({ connected: [descriptor], disconnected: [] });
  return harness.latest("setSource");
}

function controlsByKey(control) {
  return Object.fromEntries(control.data.map(entry => [entry.key, entry.value]));
}

test("Settings launches the exact native settings entry and display contract", async () => {
  const h = createDevice();
  const result = h.device.activate(settings);
  const call = h.latest("launchAppControl");
  assert.equal(call.id, "org.tizen.menu");
  assert.equal(call.control.operation, "http://tizen.org/appcontrol/operation/default");
  assert.equal(call.control.uri, null);
  assert.equal(call.control.mime, null);
  assert.equal(call.control.category, null);
  assert.deepEqual(controlsByKey(call.control), {
    Entry: ["s/setting"],
    __K_EX_FORCE_BOOST: ["enable"],
    __K_SPLASH_SCREEN: ["LA_UI_VR_TRANSPARENT"],
    __K_EX_SPLASH_SCREEN_PROGRESS_BAR: ["false"]
  });
  call.success();
  await result;
  assert.equal(h.timers.size, 0);
});

test("Art opens fullscreen through the exact native app", async () => {
  const h = createDevice();
  const result = h.device.activate(art);
  const call = h.latest("launchAppControl");
  assert.equal(call.id, "org.tizen.art-app");
  assert.equal(call.control.operation, "http://tizen.org/appcontrol/operation/default");
  assert.deepEqual(controlsByKey(call.control), { mode: ["fullscreen"] });
  call.success();
  await result;
});

test("Back explicitly opens stock Home", async () => {
  const h = createDevice();
  const result = h.device.back();
  const call = h.latest("launch");
  assert.equal(call.id, "com.samsung.tv.csfs");
  assert.equal(h.calls.length, 1);
  call.success();
  await result;
});

test("source changes preserve the TV descriptor and verify it before opening the viewer", async t => {
  for (const [id, port, connection] of [
    ["apple-tv", 1, "connected"],
    ["hdmi-2", 2, "disconnected"],
    ["hdmi-4", 4, "connected"]
  ]) {
    await t.test(id, async () => {
      const h = createDevice();
      const descriptor = { type: "HDMI", number: port, vendorField: "preserved" };
      const result = h.device.activate({ id, type: "source", port });
      const inventory = { connected: [], disconnected: [] };
      inventory[connection].push(descriptor);
      h.latest("getPropertyValue").success(inventory);
      const change = h.latest("setSource");
      assert.equal(change.descriptor, descriptor);
      assert.equal(h.latest("launch"), undefined);
      h.select({ type: "HDMI", number: port });
      change.success();
      assert.deepEqual(h.calls.map(call => call.method), [
        "getPropertyValue", "setSource", "getSource", "launch"
      ]);
      const viewer = h.latest("launch");
      assert.equal(viewer.id, "org.tizen.tv-viewer");
      viewer.success();
      await result;
    });
  }
});

test("missing, malformed and ambiguous source inventories never change input", async t => {
  const descriptor = { type: "HDMI", number: 1 };
  for (const [name, inventory] of [
    ["missing HDMI", { connected: [{ type: "HDMI", number: 2 }], disconnected: [] }],
    ["duplicate descriptor", { connected: [descriptor], disconnected: [descriptor] }],
    ["wrong number type", { connected: [{ type: "HDMI", number: "1" }], disconnected: [] }],
    ["invalid inventory", { connected: [descriptor] }],
    ["empty inventory", null]
  ]) {
    await t.test(name, async () => {
      const h = createDevice();
      const rejection = assert.rejects(h.device.activate(appleTV), /unavailable|could not read/);
      h.latest("getPropertyValue").success(inventory);
      await rejection;
      assert.deepEqual(h.calls.map(call => call.method), ["getPropertyValue"]);
      assert.equal(h.timers.size, 0);
    });
  }
});

test("source readback mismatch or failure prevents the viewer launch", async t => {
  for (const wrong of [null, { type: "TV", number: 1 }, { type: "HDMI", number: 2 }]) {
    await t.test(JSON.stringify(wrong), async () => {
      const h = createDevice();
      const rejection = assert.rejects(h.device.activate(appleTV), /did not confirm HDMI 1/);
      h.select(wrong);
      discover(h).success();
      await rejection;
      assert.equal(h.latest("launch"), undefined);
    });
  }
  await t.test("readback throws", async () => {
    const h = createDevice();
    h.native.tvwindow.getSource = () => { throw { name: "SecurityError" }; };
    const rejection = assert.rejects(h.device.activate(appleTV), /could not be confirmed.*SecurityError/);
    discover(h).success();
    await rejection;
    assert.equal(h.latest("launch"), undefined);
  });
});

test("native failures reject with useful context and stop the remaining action", async t => {
  for (const stage of ["inventory", "setSource", "viewer", "settings", "art", "back"]) {
    await t.test(stage, async () => {
      const h = createDevice();
      const result = stage === "back" ? h.device.back() : h.device.activate(
        stage === "settings" ? settings : stage === "art" ? art : appleTV
      );
      const rejection = assert.rejects(result, /SecurityError/);
      if (stage === "setSource" || stage === "viewer") discover(h);
      if (stage === "viewer") h.latest("setSource").success();
      const method = {
        inventory: "getPropertyValue", setSource: "setSource", viewer: "launch",
        settings: "launchAppControl", art: "launchAppControl", back: "launch"
      }[stage];
      h.latest(method).error({ name: "SecurityError" });
      await rejection;
      assert.equal(h.timers.size, 0);
      if (stage === "inventory" || stage === "setSource") {
        assert.equal(h.latest("launch"), undefined);
      }
    });
  }
});

test("synchronous native errors reject and release the action slot", async () => {
  const h = createDevice();
  h.native.systeminfo.getPropertyValue = () => { throw { name: "NotSupportedError" }; };
  await assert.rejects(h.device.activate(appleTV), /NotSupportedError/);
  assert.equal(h.timers.size, 0);
  const recovery = h.device.back();
  h.latest("launch").success();
  await recovery;
});

test("timeout during discovery or source change ignores late callbacks", async t => {
  for (const stage of ["inventory", "setSource"]) {
    await t.test(stage, async () => {
      const h = createDevice();
      const rejection = assert.rejects(h.device.activate(appleTV), /did not confirm the action/);
      const pending = stage === "inventory" ? h.latest("getPropertyValue") : discover(h);
      h.expire();
      await rejection;
      if (stage === "inventory") pending.success({ connected: [{ type: "HDMI", number: 1 }], disconnected: [] });
      else pending.success();
      pending.error({ name: "LateError" });
      assert.equal(h.latest("launch"), undefined);
      assert.equal(h.latest("getSource"), undefined);
      if (stage === "inventory") assert.equal(h.latest("setSource"), undefined);
    });
  }
});

test("native launch timeouts reject without inventing a completed action", async t => {
  for (const action of [settings, art, null]) {
    await t.test(action ? action.id : "back", async () => {
      const h = createDevice();
      const rejection = assert.rejects(action ? h.device.activate(action) : h.device.back(), /did not confirm the action/);
      const pending = h.calls.at(-1);
      h.expire();
      await rejection;
      pending.success();
      pending.error({ name: "LateError" });
      assert.equal(h.calls.length, 1);
      assert.equal(h.timers.size, 0);
    });
  }
});

test("an in-flight action rejects another action, including Back", async () => {
  const h = createDevice();
  const first = h.device.activate(appleTV);
  await assert.rejects(h.device.activate(settings), /already in progress/);
  await assert.rejects(h.device.back(), /already in progress/);
  assert.equal(h.calls.length, 1);
  const rejection = assert.rejects(first, /did not confirm the action/);
  const stale = h.latest("getPropertyValue");
  h.expire();
  await rejection;

  const next = h.device.activate(settings);
  stale.success({ connected: [{ type: "HDMI", number: 1 }], disconnected: [] });
  stale.error({ name: "LateError" });
  await assert.rejects(h.device.back(), /already in progress/);
  assert.equal(h.latest("setSource"), undefined);
  h.latest("launchAppControl").success();
  await next;
});

test("browser preview reports unavailable and performs no native action", async () => {
  const h = createDevice({ browser: true });
  assert.equal(h.device.available, false);
  await assert.rejects(h.device.activate(appleTV), /Open this app on the TV/);
  await assert.rejects(h.device.back(), /Open this app on the TV/);
  assert.equal(h.calls.length, 0);
  assert.equal(h.timers.size, 0);
});

test("HDMI 3 and unknown or mismatched actions are rejected before device access", async () => {
  const h = createDevice();
  for (const action of [
    null, {}, { id: "hdmi-3", type: "source", port: 3 },
    { id: "apple-tv", type: "source", port: 3 },
    { id: "hdmi-2", type: "source", port: 4 },
    { id: "settings", type: "source", port: 1 },
    { id: "unknown", type: "art" }
  ]) {
    await assert.rejects(h.device.activate(action), /shortcut is unavailable/);
  }
  assert.equal(h.calls.length, 0);
  assert.equal(h.timers.size, 0);
});
