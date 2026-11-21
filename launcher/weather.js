/* Open-Meteo current and daily fields: https://open-meteo.com/en/docs */
(function () {
  "use strict";

  var refreshDelay = 10 * 60 * 1000;
  var maxAge = 30 * 60 * 1000;
  var container = document.getElementById("weather");
  var current = document.getElementById("weather-current");
  var icon = document.getElementById("weather-icon");
  var forecast = document.getElementById("weather-forecast");
  var config = null;
  var configured = false;
  var disabled = false;
  var stopped = false;
  var busy = false;
  var refreshWhenIdle = false;
  var request = null;
  var refreshTimer = null;
  var expiryTimer = null;
  var lastAttempt = null;
  var lastWeather = null;
  var unavailable = false;
  var conditions = {
    0: ["Clear", "sun"], 1: ["Mostly clear", "sun"], 2: ["Partly cloudy", "partly"],
    3: ["Overcast", "cloud"], 45: ["Fog", "fog"], 48: ["Freezing fog", "fog"],
    51: ["Light drizzle", "rain"], 53: ["Drizzle", "rain"], 55: ["Heavy drizzle", "rain"],
    56: ["Freezing drizzle", "rain"], 57: ["Freezing drizzle", "rain"],
    61: ["Light rain", "rain"], 63: ["Rain", "rain"], 65: ["Heavy rain", "rain"],
    66: ["Freezing rain", "rain"], 67: ["Freezing rain", "rain"],
    71: ["Light snow", "snow"], 73: ["Snow", "snow"], 75: ["Heavy snow", "snow"],
    77: ["Snow grains", "snow"], 80: ["Rain showers", "rain"],
    81: ["Rain showers", "rain"], 82: ["Heavy rain showers", "rain"],
    85: ["Snow showers", "snow"], 86: ["Heavy snow showers", "snow"],
    95: ["Thunderstorm", "storm"], 96: ["Thunderstorm with hail", "storm"],
    97: ["Heavy thunderstorm", "storm"], 99: ["Thunderstorm with hail", "storm"]
  };
  var cloud = '<path d="M8 22a5 5 0 1 1 .6-10A7 7 0 0 1 22 11a5.5 5.5 0 1 1 1 11H8Z"/>';
  var sun = '<circle cx="16" cy="16" r="6"/><path d="M16 3v3m0 20v3M3 16h3m20 0h3M6.8 6.8l2.1 2.1m14.2 14.2 2.1 2.1M6.8 25.2l2.1-2.1M23.1 8.9l2.1-2.1"/>';
  var moon = '<path d="M24.6 21.8A11 11 0 0 1 10.2 7.4 11 11 0 1 0 24.6 21.8Z"/>';
  var symbols = {
    sun: sun,
    moon: moon,
    cloud: cloud,
    partly: '<g transform="translate(-1 -2) scale(.75)">' + sun + '</g>' + cloud,
    nightPartly: '<g transform="translate(-1 -3) scale(.8)">' + moon + '</g>' + cloud,
    rain: '<g transform="translate(0 -3)">' + cloud + '</g><path d="m10 23-2 4m9-4-2 4m9-4-2 4"/>',
    snow: '<g transform="translate(0 -4)">' + cloud + '</g><path d="M10 24v5m-2.2-3.8 4.4 2.6m0-2.6-4.4 2.6M22 24v5m-2.2-3.8 4.4 2.6m0-2.6-4.4 2.6"/>',
    storm: '<g transform="translate(0 -4)">' + cloud + '</g><path d="m17 19-5 6h5l-2 5 7-8h-5l2-3"/>',
    fog: '<g transform="translate(0 -5)">' + cloud + '</g><path d="M6 23h20M9 28h14"/>'
  };

  function finite(value, min, max) {
    return typeof value === "number" && Number.isFinite(value) && value >= min && value <= max;
  }

  function validCode(value) {
    return Number.isInteger(value) && Object.prototype.hasOwnProperty.call(conditions, value);
  }

  function readConfig(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid weather configuration");
    var keys = Object.keys(value);
    if (keys.length === 0) return null;
    if (keys.length !== 2 || !finite(value.latitude, -90, 90) || !finite(value.longitude, -180, 180)) {
      throw new Error("Invalid weather coordinates");
    }
    return { latitude: value.latitude, longitude: value.longitude };
  }

  function readWeather(value, now) {
    var c = value && value.current;
    var d = value && value.daily;
    var cu = value && value.current_units;
    var du = value && value.daily_units;
    var offset = value && value.utc_offset_seconds;
    if (!c || !d || !cu || !du || cu.temperature_2m !== "°C" ||
        du.temperature_2m_max !== "°C" || du.temperature_2m_min !== "°C" ||
        !finite(c.temperature_2m, -100, 70) || !validCode(c.weather_code) ||
        (c.is_day !== 0 && c.is_day !== 1) || !finite(c.time, 1, Number.MAX_SAFE_INTEGER) ||
        !Number.isInteger(offset) || !finite(offset, -43200, 50400)) throw new Error("Invalid current weather");
    var fields = ["time", "temperature_2m_max", "temperature_2m_min", "weather_code"];
    if (!fields.every(function (key) { return Array.isArray(d[key]) && d[key].length === 1; }) ||
        !finite(d.temperature_2m_max[0], -100, 70) || !finite(d.temperature_2m_min[0], -100, 70) ||
        d.temperature_2m_min[0] > d.temperature_2m_max[0] || !validCode(d.weather_code[0]) ||
        !finite(d.time[0], 1, Number.MAX_SAFE_INTEGER)) throw new Error("Invalid daily forecast");
    var observed = c.time * 1000;
    var date = Math.floor((now / 1000 + offset) / 86400);
    if (now - observed >= maxAge || observed - now > 15 * 60 * 1000 ||
        Math.floor((d.time[0] + offset) / 86400) !== date) throw new Error("Stale weather response");
    return { temperature: c.temperature_2m, code: c.weather_code, day: c.is_day,
      high: d.temperature_2m_max[0], low: d.temperature_2m_min[0], dailyCode: d.weather_code[0],
      expires: Math.min(Math.min(now, observed) + maxAge, ((date + 1) * 86400 - offset) * 1000),
      date: date, offset: offset };
  }

  function urlFor(location) {
    return "https://api.open-meteo.com/v1/forecast?latitude=" + encodeURIComponent(location.latitude) +
      "&longitude=" + encodeURIComponent(location.longitude) +
      "&current=temperature_2m,weather_code,is_day&daily=temperature_2m_max,temperature_2m_min,weather_code" +
      "&temperature_unit=celsius&timezone=auto&forecast_days=1&timeformat=unixtime";
  }

  function jsonRequest(url) {
    return new Promise(function (resolve, reject) {
      var controller = new window.AbortController();
      var finished = false;
      var pending = { cancel: function () { finish(cancellation()); controller.abort(); } };
      var timer = window.setTimeout(function () {
        finish(new Error("Weather request timed out"));
        controller.abort();
      }, 8000);
      request = pending;
      function finish(error, value) {
        if (finished) return;
        finished = true;
        window.clearTimeout(timer);
        if (request === pending) request = null;
        if (error) reject(error); else resolve(value);
      }
      Promise.resolve().then(function () {
        if (finished) return;
        return window.fetch(url, { signal: controller.signal, cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer" });
      }).then(function (response) {
        if (finished) return;
        if (!response || !response.ok) throw new Error("Weather request failed");
        return response.json();
      }).then(function (value) { if (!finished) finish(null, value); }, function (error) { finish(error); });
    });
  }

  function cancellation() {
    var error = new Error("Weather request cancelled");
    error.cancelled = true;
    return error;
  }

  function localConfig() {
    return new Promise(function (resolve, reject) {
      var xhr = new window.XMLHttpRequest();
      var finished = false;
      var pending = { cancel: function () { finish(cancellation()); xhr.abort(); } };
      var timer = window.setTimeout(function () {
        finish(new Error("Weather configuration timed out"));
        xhr.abort();
      }, 8000);
      request = pending;
      function finish(error, value) {
        if (finished) return;
        finished = true;
        window.clearTimeout(timer);
        if (request === pending) request = null;
        if (error) reject(error); else resolve(value);
      }
      xhr.onload = function () {
        if (finished) return;
        try {
          var localFile = window.location && window.location.protocol === "file:";
          if ((xhr.status !== 200 && !(xhr.status === 0 && localFile)) ||
              typeof xhr.responseText !== "string" || xhr.responseText.length > 4096) {
            throw new Error("Cannot read local weather configuration");
          }
          finish(null, JSON.parse(xhr.responseText));
        } catch (error) { finish(error); }
      };
      xhr.onerror = xhr.ontimeout = xhr.onabort = function () { finish(new Error("Weather configuration unavailable")); };
      try {
        // Packaged Tizen apps use file: URLs, which fetch need not support.
        xhr.open("GET", "weather-config.json", true);
        xhr.timeout = 8000;
        xhr.send(null);
      } catch (error) { finish(error); }
    });
  }

  function hideExpired() {
    if (!lastWeather || Date.now() >= lastWeather.expires ||
        Math.floor((Date.now() / 1000 + lastWeather.offset) / 86400) !== lastWeather.date) {
      container.hidden = true;
    }
  }

  function render(weather) {
    var condition = conditions[weather.code];
    var symbol = condition[1];
    if (weather.day === 0 && symbol === "sun") symbol = "moon";
    if (weather.day === 0 && symbol === "partly") symbol = "nightPartly";
    current.textContent = Math.round(weather.temperature) + "°C";
    icon.setAttribute("viewBox", "0 0 32 32");
    icon.setAttribute("role", "img");
    icon.setAttribute("aria-label", condition[0] + (weather.day === 0 ? " at night" : ""));
    icon.innerHTML = symbols[symbol];
    forecast.textContent = conditions[weather.dailyCode][0] + " · H " + Math.round(weather.high) + "°  L " + Math.round(weather.low) + "°";
    forecast.setAttribute("aria-label", "Today: " + conditions[weather.dailyCode][0] + ", high " +
      Math.round(weather.high) + ", low " + Math.round(weather.low) + " degrees Celsius");
    container.hidden = false;
    window.clearTimeout(expiryTimer);
    expiryTimer = window.setTimeout(hideExpired, Math.max(0, weather.expires - Date.now()));
  }

  function schedule() {
    window.clearTimeout(refreshTimer);
    refreshTimer = null;
    if (!stopped && !disabled && !document.hidden) refreshTimer = window.setTimeout(refresh,
      lastAttempt === null ? 0 : Math.max(0, refreshDelay - (Date.now() - lastAttempt)));
  }

  function refresh() {
    if (stopped || disabled || document.hidden || busy) return Promise.resolve(false);
    window.clearTimeout(refreshTimer);
    refreshTimer = null;
    hideExpired();
    busy = true;
    lastAttempt = Date.now();
    var work = configured ? Promise.resolve(config) : localConfig().then(function (value) {
      config = readConfig(value);
      configured = true;
      disabled = config === null;
      return config;
    });
    return work.then(function (location) {
      if (!location || stopped || document.hidden) return false;
      return jsonRequest(urlFor(location)).then(function (value) {
        if (stopped || document.hidden) return false;
        var weather = readWeather(value, Date.now());
        lastWeather = weather;
        render(weather);
        if (unavailable) console.info("Weather available");
        unavailable = false;
        return true;
      });
    }).catch(function (error) {
      if (!stopped && !document.hidden && !(error && error.cancelled) && !unavailable) {
        console.warn("Weather unavailable: " + (configured ? "forecast" : "configuration") + " request failed");
        unavailable = true;
      }
      hideExpired();
      return false;
    }).then(function (updated) {
      busy = false;
      if (refreshWhenIdle && !document.hidden && !stopped) {
        refreshWhenIdle = false;
        refresh();
      } else schedule();
      return updated;
    });
  }

  function visibilityChanged() {
    if (document.hidden) {
      window.clearTimeout(refreshTimer);
      refreshTimer = null;
      if (request) {
        refreshWhenIdle = true;
        request.cancel();
      }
    } else {
      hideExpired();
      if (refreshWhenIdle || lastAttempt === null || Date.now() - lastAttempt >= refreshDelay) {
        if (busy) refreshWhenIdle = true;
        else { refreshWhenIdle = false; refresh(); }
      } else schedule();
    }
  }

  function stop() {
    stopped = true;
    window.clearTimeout(refreshTimer);
    window.clearTimeout(expiryTimer);
    if (request) request.cancel();
    document.removeEventListener("visibilitychange", visibilityChanged);
  }

  window.LauncherWeather = { refresh: refresh, stop: stop };
  if (!container || !current || !icon || !forecast || typeof window.fetch !== "function" ||
      typeof window.AbortController !== "function" || typeof window.XMLHttpRequest !== "function") { disabled = true; return; }
  container.hidden = true;
  document.addEventListener("visibilitychange", visibilityChanged);
  refresh();
}());
