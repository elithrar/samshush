const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "weather.js"), "utf8");
const minute = 60000;
const flush = () => new Promise(resolve => setImmediate(resolve));

function fixture(now, extra = {}) {
  return {
    utc_offset_seconds: 0,
    current_units: { temperature_2m: "°C" },
    daily_units: { temperature_2m_max: "°C", temperature_2m_min: "°C" },
    current: { time: Math.floor(now / 1000), temperature_2m: 17, weather_code: 61, is_day: 1 },
    daily: { time: [Math.floor(now / 86400000) * 86400], temperature_2m_max: [19],
      temperature_2m_min: [12], weather_code: [61] },
    ...extra
  };
}

function createWeather(options = {}) {
  let now = Date.UTC(2026, 5, 1, 12);
  let nextTimer = 0;
  const timers = new Map();
  const requests = [];
  const configs = [];
  const logs = [];
  const events = new Map();
  const elements = Object.fromEntries(["weather", "weather-current", "weather-icon", "weather-forecast"].map(id =>
    [id, { hidden: true, textContent: "", innerHTML: "", attributes: {},
      setAttribute(name, value) { this.attributes[name] = value; } }]));
  const document = {
    hidden: Boolean(options.hidden),
    getElementById(id) { return elements[id]; },
    addEventListener(name, listener) { events.set(name, listener); },
    removeEventListener(name, listener) { if (events.get(name) === listener) events.delete(name); }
  };
  const window = {
    location: { protocol: options.protocol || "https:" },
    AbortController: options.noAbort ? undefined : AbortController,
    setTimeout(callback, delay) {
      const id = ++nextTimer;
      timers.set(id, { callback, at: now + delay, delay });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    XMLHttpRequest: function () {
      configs.push(this);
      this.open = (method, url, async) => { Object.assign(this, { method, url, async }); };
      this.send = body => { this.body = body; };
      this.abort = () => { this.aborted = true; this.onabort(); };
      this.respond = (value, status = 200) => {
        this.status = status;
        this.responseText = typeof value === "string" ? value : JSON.stringify(value);
        this.onload();
      };
    },
    fetch(url, settings) {
      return new Promise((resolve, reject) => requests.push({ url, settings, resolve, reject }));
    }
  };
  class Clock extends Date { static now() { return now; } }
  const quietConsole = Object.fromEntries(["warn", "info", "error"].map(level =>
    [level, message => logs.push({ level, message })]));
  vm.runInNewContext(source, { window, document, Date: Clock, console: quietConsole }, { filename: "weather.js" });
  return {
    api: window.LauncherWeather, elements, timers, requests, configs, logs,
    get now() { return now; },
    async configure(value = { latitude: 0, longitude: 0 }, status = 200) {
      configs.at(-1).respond(value, status);
      await flush();
    },
    async respond(value = fixture(now), request = requests.at(-1)) {
      request.resolve({ ok: true, json: async () => value });
      await flush();
    },
    async fail(request = requests.at(-1)) {
      request.reject(new Error("Offline"));
      await flush();
    },
    visible(value) { document.hidden = !value; events.get("visibilitychange")?.(); },
    async advance(milliseconds) {
      const end = now + milliseconds;
      for (;;) {
        const entries = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at);
        if (!entries.length) break;
        const [id, timer] = entries[0];
        timers.delete(id);
        now = timer.at;
        timer.callback();
        await flush();
      }
      now = end;
      await flush();
    }
  };
}

test("local configuration is bounded, supports packaged file URLs, and exposes no location defaults", async () => {
  const h = createWeather({ protocol: "file:" });
  assert.equal(h.configs[0].method, "GET");
  assert.equal(h.configs[0].url, "weather-config.json");
  assert.equal(h.configs[0].timeout, 8000);
  assert.equal(h.elements.weather.hidden, true);
  await h.configure({ latitude: 0, longitude: 0 }, 0);
  assert.equal(h.requests.length, 1);
  const url = new URL(h.requests[0].url);
  assert.equal(url.origin, "https://api.open-meteo.com");
  assert.equal(url.pathname, "/v1/forecast");
  assert.deepEqual(Object.fromEntries(url.searchParams), {
    latitude: "0", longitude: "0", current: "temperature_2m,weather_code,is_day",
    daily: "temperature_2m_max,temperature_2m_min,weather_code", temperature_unit: "celsius",
    timezone: "auto", forecast_days: "1", timeformat: "unixtime"
  });
  assert.equal(h.requests[0].settings.credentials, "omit");
  assert.equal(h.requests[0].settings.cache, "no-store");
  await h.respond();
  assert.equal(h.elements.weather.hidden, false);
  assert.equal(h.logs.length, 0);
  assert.equal(h.elements["weather-current"].textContent, "17°C");
  assert.equal(h.elements["weather-forecast"].textContent, "Light rain · H 19°  L 12°");
  assert.equal(h.elements["weather-icon"].attributes["aria-label"], "Light rain");
  assert.match(h.elements["weather-icon"].innerHTML, /<path/);
});

test("empty, invalid and unavailable configuration never sends coordinates", async t => {
  const cases = [null, [], { latitude: 0 }, { latitude: "0", longitude: 0 },
    { latitude: 91, longitude: 0 }, { latitude: 0, longitude: -181 },
    { latitude: 0, longitude: 0, extra: true }, "{broken", "x".repeat(4097)];
  for (const value of cases) {
    await t.test(JSON.stringify(value).slice(0, 80), async () => {
      const h = createWeather();
      await h.configure(value);
      assert.equal(h.requests.length, 0);
      assert.equal(h.elements.weather.hidden, true);
    });
  }
  const disabled = createWeather();
  await disabled.configure({});
  await disabled.advance(60 * minute);
  assert.equal(disabled.configs.length, 1);
  assert.equal(disabled.requests.length, 0);
  assert.equal(disabled.timers.size, 0);
  const networkZero = createWeather();
  await networkZero.configure({ latitude: 0, longitude: 0 }, 0);
  assert.equal(networkZero.requests.length, 0, "HTTP status zero is not a successful HTTP response");
  const timeout = createWeather();
  await timeout.advance(8000);
  assert.equal(timeout.configs[0].aborted, true);
  assert.equal(timeout.requests.length, 0);
  const unsupported = createWeather({ noAbort: true });
  assert.equal(unsupported.configs.length, 0);
  assert.equal(unsupported.requests.length, 0);
});

test("visible polling remains ten minutes apart and concurrent refreshes do not overlap", async () => {
  const h = createWeather();
  await h.configure();
  await h.api.refresh();
  await h.api.refresh();
  assert.equal(h.requests.length, 1);
  await h.respond();
  await h.advance(10 * minute - 1);
  assert.equal(h.requests.length, 1);
  await h.advance(1);
  assert.equal(h.requests.length, 2);
  await h.api.refresh();
  assert.equal(h.requests.length, 2);
  await h.respond();
  await h.advance(10 * minute);
  assert.equal(h.requests.length, 3);
});

test("hide at minute four and show at minute eight still refreshes at minute ten", async () => {
  const h = createWeather();
  await h.configure();
  await h.respond();
  await h.advance(4 * minute);
  h.visible(false);
  await h.advance(4 * minute);
  h.visible(true);
  await flush();
  assert.equal(h.requests.length, 1);
  await h.advance(2 * minute);
  assert.equal(h.requests.length, 2);
});

test("hidden polling stops and stale weather refreshes immediately on return", async () => {
  const h = createWeather();
  await h.configure();
  await h.respond();
  h.visible(false);
  await h.advance(31 * minute);
  assert.equal(h.requests.length, 1);
  assert.equal(h.elements.weather.hidden, true);
  h.visible(true);
  await flush();
  assert.equal(h.requests.length, 2);
  await h.respond();
  assert.equal(h.elements.weather.hidden, false);
  assert.equal(h.logs.length, 0, "visibility cancellation is not a weather failure");
});

test("initially hidden apps wait for visibility and cancelled local reads can restart", async () => {
  const h = createWeather({ hidden: true });
  assert.equal(h.configs.length, 0);
  h.visible(true);
  assert.equal(h.configs.length, 1);
  h.visible(false);
  h.visible(true);
  await flush();
  assert.equal(h.configs[0].aborted, true);
  assert.equal(h.configs.length, 2);
  await h.configure({});
  assert.equal(h.requests.length, 0);
  assert.equal(h.logs.length, 0);
  assert.equal(h.timers.size, 0);
});

test("hide/show during a request cancels it, ignores late data, and does not stick busy", async () => {
  const h = createWeather();
  await h.configure();
  const cancelled = h.requests[0];
  h.visible(false);
  h.visible(true);
  await flush();
  assert.equal(cancelled.settings.signal.aborted, true);
  assert.equal(h.requests.length, 2);
  const late = fixture(h.now);
  late.current.temperature_2m = 55;
  await h.respond(late, cancelled);
  assert.equal(h.elements.weather.hidden, true);
  await h.respond();
  assert.equal(h.elements["weather-current"].textContent, "17°C");
  assert.equal(h.elements.weather.hidden, false);
});

test("the eight-second request bound includes decoding the response body", async () => {
  const h = createWeather();
  await h.configure();
  let finishBody;
  h.requests[0].resolve({ ok: true, json: () => new Promise(resolve => { finishBody = resolve; }) });
  await flush();
  await h.advance(8000);
  assert.equal(h.requests[0].settings.signal.aborted, true);
  finishBody(fixture(h.now));
  await flush();
  assert.equal(h.elements.weather.hidden, true);
});

test("request timeout aborts at eight seconds and cannot publish a late response", async () => {
  const h = createWeather();
  await h.configure();
  const expired = h.requests[0];
  await h.advance(8000);
  assert.equal(expired.settings.signal.aborted, true);
  await h.respond(fixture(h.now), expired);
  assert.equal(h.elements.weather.hidden, true);
  h.api.refresh();
  await flush();
  assert.equal(h.requests.length, 2);
  await h.respond();
  assert.equal(h.elements.weather.hidden, false);
});

test("failed requests keep recent data, back off across visibility changes, and hide it at thirty minutes", async () => {
  const h = createWeather();
  await h.configure();
  await h.respond();
  await h.advance(10 * minute);
  await h.fail();
  assert.equal(h.elements.weather.hidden, false);
  await h.advance(2 * minute);
  h.visible(false);
  h.visible(true);
  await flush();
  assert.equal(h.requests.length, 2, "a failed attempt retains its ten-minute backoff");
  await h.advance(8 * minute);
  assert.equal(h.requests.length, 3);
  await h.fail();
  await h.advance(10 * minute);
  assert.equal(h.elements.weather.hidden, true);
  assert.equal(h.elements["weather-current"].textContent, "17°C", "no fake value or error copy is substituted");
});

test("failures and recovery log once per transition without request or location details", async () => {
  const h = createWeather();
  await h.configure();
  h.requests[0].reject(new Error("Sensitive request detail from fetch"));
  await flush();
  h.api.refresh();
  await flush();
  await h.fail();
  assert.deepEqual(h.logs, [{ level: "warn", message: "Weather unavailable: forecast request failed" }]);
  h.api.refresh();
  await flush();
  await h.respond();
  assert.deepEqual(h.logs.at(-1), { level: "info", message: "Weather available" });
  assert.equal(h.logs.length, 2);
});

test("yesterday's daily forecast expires at the location's midnight", async () => {
  const h = createWeather({ hidden: true });
  await h.advance(12 * 60 * minute - 5 * minute);
  h.visible(true);
  await h.configure();
  await h.respond();
  assert.equal(h.elements.weather.hidden, false);
  await h.advance(5 * minute);
  assert.equal(h.elements.weather.hidden, true);
  assert.equal(h.requests.length, 1);
});

test("malformed, wrong-unit, stale and unknown-code responses remain hidden", async t => {
  const mutations = [
    v => { v.current.temperature_2m = NaN; }, v => { v.current.temperature_2m = 90; },
    v => { v.current.weather_code = 42; }, v => { v.current.is_day = true; },
    v => { v.current.time -= 1800; }, v => { v.current.time += 901; },
    v => { v.current_units.temperature_2m = "°F"; }, v => { v.daily_units.temperature_2m_max = "°F"; },
    v => { v.daily.temperature_2m_min = []; }, v => { v.daily.temperature_2m_min[0] = 30; },
    v => { v.daily.weather_code[0] = 100; }, v => { v.daily.time[0] -= 86400; },
    v => { v.utc_offset_seconds = Infinity; }, v => { v.current = null; }
  ];
  for (const [index, mutate] of mutations.entries()) {
    await t.test(String(index), async () => {
      const h = createWeather();
      await h.configure();
      const value = fixture(h.now);
      mutate(value);
      await h.respond(value);
      assert.equal(h.elements.weather.hidden, true);
      assert.equal(h.elements["weather-current"].textContent, "");
    });
  }
});

test("weather codes select accessible day/night symbols, including the current storm code", async t => {
  for (const [code, day, label] of [[0, 0, "Clear at night"], [2, 0, "Partly cloudy at night"],
    [3, 1, "Overcast"], [45, 1, "Fog"], [75, 1, "Heavy snow"], [97, 1, "Heavy thunderstorm"]]) {
    await t.test(label, async () => {
      const h = createWeather();
      await h.configure();
      const value = fixture(h.now);
      value.current.weather_code = code;
      value.current.is_day = day;
      await h.respond(value);
      assert.equal(h.elements.weather.hidden, false);
      assert.equal(h.elements["weather-icon"].attributes["aria-label"], label);
      assert.equal(h.elements["weather-icon"].attributes.role, "img");
      assert.match(h.elements["weather-icon"].innerHTML, /<path/);
    });
  }
});

test("stop releases timers, aborts work and prevents later rendering", async () => {
  const h = createWeather();
  await h.configure();
  h.api.stop();
  await flush();
  assert.equal(h.requests[0].settings.signal.aborted, true);
  assert.equal(h.timers.size, 0);
  await h.respond();
  h.visible(true);
  await h.advance(60 * minute);
  assert.equal(h.requests.length, 1);
  assert.equal(h.elements.weather.hidden, true);
});
