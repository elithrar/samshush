/* Open-Meteo weather fields: https://open-meteo.com/en/docs */
(function () {
  "use strict";

  var refreshDelay = 10 * 60 * 1000;
  var maxAge = 30 * 60 * 1000;
  var container = document.getElementById("weather");
  var current = document.getElementById("weather-current");
  var icon = document.getElementById("weather-icon");
  var forecast = document.getElementById("weather-forecast");
  var periods = document.getElementById("weather-periods");
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
    51: ["Light drizzle", "drizzle"], 53: ["Drizzle", "drizzle"], 55: ["Heavy drizzle", "drizzle"],
    56: ["Freezing drizzle", "drizzle"], 57: ["Freezing drizzle", "drizzle"],
    61: ["Light rain", "rain"], 63: ["Rain", "rain"], 65: ["Heavy rain", "rain"],
    66: ["Freezing rain", "rain"], 67: ["Freezing rain", "rain"],
    71: ["Light snow", "snow"], 73: ["Snow", "snow"], 75: ["Heavy snow", "snow"],
    77: ["Snow grains", "snow"], 80: ["Rain showers", "showers"],
    81: ["Rain showers", "showers"], 82: ["Heavy rain showers", "showers"],
    85: ["Snow showers", "snow"], 86: ["Heavy snow showers", "snow"],
    95: ["Thunderstorm", "storm"], 96: ["Thunderstorm with hail", "storm"],
    97: ["Heavy thunderstorm", "storm"], 99: ["Thunderstorm with hail", "storm"]
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

  function readPeriods(value, now) {
    var h = value.hourly;
    var units = value.hourly_units;
    var fields = ["time", "temperature_2m", "weather_code", "is_day", "rain", "showers"];
    if (!h || !units || units.time !== "unixtime" || units.temperature_2m !== "°C" ||
        units.rain !== "mm" || units.showers !== "mm" ||
        !fields.every(function (key) { return Array.isArray(h[key]) && h[key].length === 25; }) ||
        !h.time.every(function (time, i) { return Number.isSafeInteger(time) && time === h.time[0] + i * 3600; }) ||
        typeof value.timezone !== "string" || !value.timezone ||
        now / 1000 < h.time[0] || now / 1000 >= h.time[0] + 3600) return [];

    var hour;
    try {
      hour = new Intl.DateTimeFormat("en-US", { timeZone: value.timezone, hour: "numeric", hour12: true });
    } catch (error) { return []; }
    var result = [];
    for (var start = 0; start < 24; start += 6) {
      if (!finite(h.temperature_2m[start], -100, 70) ||
          (h.is_day[start] !== 0 && h.is_day[start] !== 1)) return [];
      var total = 0, wet = false;
      for (var i = start; i < start + 6; i++) {
        // Open-Meteo reports rain and showers for the hour ending at each timestamp.
        var rain = h.rain[i + 1], showers = h.showers[i + 1];
        var code = h.weather_code[i];
        if (!validCode(code) || !finite(rain, 0, 2000) || !finite(showers, 0, 2000)) return [];
        total += rain + showers;
        if ((code >= 51 && code <= 67) || (code >= 80 && code <= 82)) wet = true;
      }
      result.push({ start: hour.format(h.time[start] * 1000).replace(/\s+/g, " "), temperature: h.temperature_2m[start],
        code: h.weather_code[start], day: h.is_day[start], rainMm: total, wet: wet });
    }
    return result;
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
      date: date, offset: offset, periods: readPeriods(value, now) };
  }

  function urlFor(location) {
    return "https://api.open-meteo.com/v1/forecast?latitude=" + encodeURIComponent(location.latitude) +
      "&longitude=" + encodeURIComponent(location.longitude) +
      "&current=temperature_2m,weather_code,is_day&daily=temperature_2m_max,temperature_2m_min,weather_code" +
      "&hourly=temperature_2m,weather_code,is_day,rain,showers" +
      "&temperature_unit=celsius&timezone=auto&forecast_days=1&forecast_hours=25&timeformat=unixtime";
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

  function iconURL(code, day) {
    var symbol = conditions[code][1];
    if (!day && symbol === "sun") symbol = "moon";
    if (!day && symbol === "partly") symbol = "nightPartly";
    return "weather-icons.svg#" + symbol;
  }

  function renderPeriod(period) {
    var rain = period.wet ? "Rain" : "";
    if (period.rainMm > 0) {
      var inches = (period.rainMm / 25.4).toFixed(2);
      rain = inches === "0.00" ? "Trace rain" :
        '<img class="rain-drop" src="weather-icons.svg#drop" alt="Rain"> ' + inches + " in";
    }
    var label = conditions[period.code][0] + (period.day ? "" : " at night");
    return '<li class="weather-period"><span class="period-time">' + period.start + '</span>' +
      '<span class="period-conditions"><img class="weather-icon period-icon" src="' +
      iconURL(period.code, period.day) + '" alt="' + label + '"><span class="period-temperature">' +
      Math.round(period.temperature) + '°</span></span>' +
      (rain ? '<span class="period-rain">' + rain + '</span>' : '') + '</li>';
  }

  function render(weather) {
    var condition = conditions[weather.code];
    current.textContent = Math.round(weather.temperature) + "°C";
    icon.alt = condition[0] + (weather.day === 0 ? " at night" : "");
    icon.src = iconURL(weather.code, weather.day);
    forecast.textContent = conditions[weather.dailyCode][0] + " · H " + Math.round(weather.high) + "°  L " + Math.round(weather.low) + "°";
    forecast.setAttribute("aria-label", "Today: " + conditions[weather.dailyCode][0] + ", high " +
      Math.round(weather.high) + ", low " + Math.round(weather.low) + " degrees Celsius");
    periods.innerHTML = weather.periods.map(renderPeriod).join("");
    periods.hidden = weather.periods.length === 0;
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
  if (!container || !current || !icon || !forecast || !periods || typeof window.fetch !== "function" ||
      typeof window.AbortController !== "function" || typeof window.XMLHttpRequest !== "function") { disabled = true; return; }
  container.hidden = true;
  document.addEventListener("visibilitychange", visibilityChanged);
  refresh();
}());
