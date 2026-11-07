(function () {
  "use strict";

  var actions = {
    "apple-tv": { id: "apple-tv", type: "source", port: 1 },
    "hdmi-2": { id: "hdmi-2", type: "source", port: 2 },
    "hdmi-4": { id: "hdmi-4", type: "source", port: 4 },
    "settings": { id: "settings", type: "settings" },
    "art-mode": { id: "art-mode", type: "art" }
  };
  var tiles = Array.prototype.slice.call(document.querySelectorAll(".tile:not(:disabled)"));
  var title = document.getElementById("selection-title");
  var error = document.getElementById("action-error");
  var clock = document.getElementById("clock");
  var adapter = window.LauncherDevice;
  var schedule = window.LauncherSchedule;
  var timeGroup = document.querySelector(".time-controls");
  var controls = Array.from(timeGroup.querySelectorAll(".time-control"));
  var art = tiles.find(function (tile) { return tile.dataset.id === "art-mode"; });
  var savedTimes = null;
  var scheduleBusy = false;
  var retryTimer;
  var selected = 0;
  var busy = false;

  function actionFor(tile) {
    var action = actions[tile.dataset.id];
    return Object.assign({}, action);
  }

  function reportError(reason) {
    error.textContent = reason && reason.message ? reason.message : "This action is unavailable.";
    error.hidden = false;
  }

  function notify(name, value) {
    if (typeof api[name] === "function") {
      try { api[name](value); } catch (reason) { console.error(reason); }
    }
  }

  function select(index, focus) {
    if (index < 0 || index >= tiles.length) return false;
    selected = index;
    controls.forEach(function (control) { control.tabIndex = -1; });
    tiles.forEach(function (tile, position) {
      tile.classList.toggle("selected", position === selected);
      tile.tabIndex = position === selected ? 0 : -1;
    });
    title.textContent = tiles[selected].querySelector(".tile-label").textContent;
    error.hidden = true;
    error.textContent = "";
    if (focus) tiles[selected].focus();
    notify("onSelection", actionFor(tiles[selected]));
    return true;
  }

  function perform(method, action) {
    if (busy) return;
    error.hidden = true;
    if (!adapter || typeof adapter[method] !== "function") {
      reportError(new Error("No TV connection."));
      return;
    }
    busy = true;
    var tile = tiles[selected];
    tile.classList.add("pending");
    tile.setAttribute("aria-busy", "true");
    Promise.resolve().then(function () {
      return adapter[method](action);
    }).then(function (result) {
      notify("onAction", { action: action, method: method, result: result, error: null });
    }, function (reason) {
      reportError(reason);
      notify("onAction", { action: action, method: method, error: reason });
    }).then(function () {
      busy = false;
      tile.classList.remove("pending");
      tile.removeAttribute("aria-busy");
    });
  }

  function activate() {
    perform("activate", actionFor(tiles[selected]));
  }

  function displayTimes(pair) {
    savedTimes = pair;
    if (!pair && controls.includes(document.activeElement)) {
      var message = error.hidden ? "" : error.textContent;
      api.select("art-mode");
      if (message) reportError(new Error(message));
    }
    timeGroup.hidden = !pair;
    if (!pair) return;
    controls.forEach(function (control) {
      var minutes = pair[control.dataset.time];
      var hour = Math.floor(minutes / 60);
      var minute = minutes % 60;
      var value = String(hour % 12 || 12) + (minute ? ":" + String(minute).padStart(2, "0") : "") +
        (hour >= 12 ? " PM" : " AM");
      control.querySelector(".time-value").textContent = value;
      control.setAttribute("aria-valuenow", String(minutes));
      control.setAttribute("aria-valuetext", value);
    });
  }

  function refreshTimes() {
    window.clearTimeout(retryTimer);
    if (!schedule || !schedule.available || document.hidden || scheduleBusy) return;
    scheduleBusy = true;
    schedule.read().then(displayTimes, function () {
      displayTimes(null);
      if (!document.hidden) retryTimer = window.setTimeout(refreshTimes, 30000);
    }).finally(function () { scheduleBusy = false; });
  }

  function adjustTime(control, step) {
    if (!savedTimes || scheduleBusy) return;
    var next = { start: savedTimes.start, end: savedTimes.end };
    var field = control.dataset.time;
    next[field] = (next[field] + step + 1440) % 1440;
    if (next.start === next.end) {
      reportError(new Error("Choose two different times."));
      return;
    }
    error.hidden = true;
    scheduleBusy = true;
    control.setAttribute("aria-busy", "true");
    schedule.set(savedTimes, next).then(function (confirmed) {
      displayTimes(confirmed);
      scheduleBusy = false;
    }, function (reason) {
      scheduleBusy = false;
      reportError(reason);
      refreshTimes();
    }).finally(function () { control.removeAttribute("aria-busy"); });
  }

  function timeKey(event) {
    var index = controls.indexOf(document.activeElement);
    var key = {37:"ArrowLeft", 38:"ArrowUp", 39:"ArrowRight", 40:"ArrowDown", 13:"Enter"}[event.keyCode] || event.key;
    if (index < 0) {
      if (document.activeElement !== art || key !== "ArrowDown" || timeGroup.hidden) return false;
      controls[0].focus();
    } else if (key === "ArrowLeft" || key === "ArrowRight") {
      if (!event.repeat) adjustTime(controls[index], key === "ArrowLeft" ? -60 : 60);
    } else if (key === "ArrowUp" && index > 0) {
      controls[index - 1].focus();
    } else if (key === "ArrowDown") {
      controls[Math.min(index + 1, controls.length - 1)].focus();
    } else if (key === "ArrowUp" || key === "Enter" || key === "Escape" || key === "Backspace" ||
        key === "BrowserBack" || event.keyCode === 10009) {
      if (!event.repeat) api.select("art-mode");
    } else return false;
    return true;
  }

  function updateClock() {
    var now = new Date();
    var hour = now.getHours();
    var minutes = String(now.getMinutes()).padStart(2, "0");
    clock.dateTime = now.toISOString();
    clock.textContent = (hour % 12 || 12) + ":" + minutes;
    var period = document.createElement("span");
    period.className = "period";
    period.textContent = hour >= 12 ? "PM" : "AM";
    clock.appendChild(period);
  }

  var api = {
    onSelection: null,
    onAction: null,
    setAdapter: function (next) {
      adapter = next;
      document.title = adapter && adapter.available === true ? "Home" : "Home — preview";
    },
    select: function (id) {
      return select(tiles.findIndex(function (tile) { return tile.dataset.id === id; }), true);
    },
    getSelection: function () { return actionFor(tiles[selected]); }
  };
  window.LauncherUI = api;

  tiles.forEach(function (tile, index) {
    tile.addEventListener("focus", function () {
      if (!tile.classList.contains("selected")) select(index, false);
    });
    tile.addEventListener("click", function () {
      select(index, false);
      activate();
    });
  });

  controls.forEach(function (control) {
    control.addEventListener("focus", function () {
      select(tiles.indexOf(art), false);
      art.classList.remove("selected");
      art.tabIndex = -1;
      control.tabIndex = 0;
    });
  });

  document.addEventListener("keydown", function (event) {
    if (timeKey(event)) { event.preventDefault(); return; }
    var key = event.key;
    var code = event.keyCode;
    if (key === "ArrowLeft" || code === 37) {
      event.preventDefault();
      select(Math.max(0, selected - 1), true);
    } else if (key === "ArrowRight" || code === 39) {
      event.preventDefault();
      select(Math.min(tiles.length - 1, selected + 1), true);
    } else if (key === "Enter" || code === 13) {
      event.preventDefault();
      if (!event.repeat) activate();
    } else if (key === "Escape" || key === "Backspace" || key === "BrowserBack" || code === 10009) {
      event.preventDefault();
      if (!event.repeat) perform("back", null);
    }
  });

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      updateClock();
      select(selected, true);
      refreshTimes();
    }
  });

  api.setAdapter(adapter);
  updateClock();
  window.setInterval(updateClock, 1000);
  select(0, true);
  refreshTimes();
}());
