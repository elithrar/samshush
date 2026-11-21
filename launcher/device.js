/* Public Tizen APIs; the exact native target contracts are documented in README. */
(function () {
  "use strict";

  var native = window.tizen;
  var available = Boolean(native && native.application && native.systeminfo && native.tvwindow);
  var inFlight = false;
  var operation = "http://tizen.org/appcontrol/operation/default";

  function failure(error, message) {
    var detail = error && error.name;
    return new Error(message + (detail ? " (" + detail + ")" : ""));
  }

  function run(work) {
    if (!available) return Promise.reject(new Error("Open this app on the TV to use its controls."));
    if (inFlight) return Promise.reject(new Error("An action is already in progress."));
    inFlight = true;
    return new Promise(function (resolve, reject) {
      var finished = false;
      var timer = window.setTimeout(function () {
        finish(new Error("The TV did not confirm the action. Check the screen before trying again."));
      }, 8000);
      function finish(error) {
        if (finished) return;
        finished = true;
        inFlight = false;
        window.clearTimeout(timer);
        if (error) reject(error); else resolve();
      }
      function active() { return !finished; }
      try { work(finish, active); } catch (error) { finish(failure(error, "The TV could not open this screen.")); }
    });
  }

  function launchControl(id, entries, finish) {
    var data = Object.keys(entries).map(function (key) {
      return new native.ApplicationControlData(key, [entries[key]]);
    });
    var control = new native.ApplicationControl(operation, null, null, null, data);
    native.application.launchAppControl(control, id, function () { finish(); }, function (error) {
      finish(failure(error, "The TV could not open this screen."));
    });
  }

  function selectSource(port, finish, active) {
    native.systeminfo.getPropertyValue("VIDEOSOURCE", function (video) {
      if (!active()) return;
      try {
        if (!video || !Array.isArray(video.connected) || !Array.isArray(video.disconnected)) {
          throw new Error("Invalid source inventory");
        }
        var matches = video.connected.concat(video.disconnected).filter(function (source) {
          return source && source.type === "HDMI" && source.number === port;
        });
        if (matches.length !== 1) {
          finish(new Error("HDMI " + port + " is unavailable. Check the device connection."));
          return;
        }
        // Pass the TV's own descriptor, including disconnected entries when supplied.
        native.tvwindow.setSource(matches[0], function () {
          if (!active()) return;
          try {
            var selected = native.tvwindow.getSource();
            if (!selected || selected.type !== "HDMI" || selected.number !== port) {
              finish(new Error("The TV did not confirm HDMI " + port + "."));
              return;
            }
            native.application.launch("org.tizen.tv-viewer", function () { finish(); }, function (error) {
              finish(failure(error, "HDMI " + port + " was selected, but the TV viewer could not open."));
            });
          } catch (error) { finish(failure(error, "The input change could not be confirmed.")); }
        }, function (error) {
          if (active()) finish(failure(error, "The TV could not switch inputs."));
        });
      } catch (error) { finish(failure(error, "The TV could not read its HDMI inputs.")); }
    }, function (error) {
      if (active()) finish(failure(error, "The TV could not read its HDMI inputs."));
    });
  }

  function activate(action) {
    if (!action || !(
      (action.id === "apple-tv" && action.type === "source" && action.port === 1) ||
      (action.id === "hdmi-2" && action.type === "source" && action.port === 2) ||
      (action.id === "hdmi-4" && action.type === "source" && action.port === 4) ||
      (action.id === "settings" && action.type === "settings") ||
      (action.id === "art-mode" && action.type === "art")
    )) return Promise.reject(new Error("This shortcut is unavailable."));
    return run(function (finish, active) {
      if (action.type === "source") {
        selectSource(action.port, finish, active);
      } else if (action.type === "settings") {
        launchControl("org.tizen.menu", {
          Entry: "s/setting",
          __K_EX_FORCE_BOOST: "enable",
          __K_SPLASH_SCREEN: "LA_UI_VR_TRANSPARENT",
          __K_EX_SPLASH_SCREEN_PROGRESS_BAR: "false"
        }, finish);
      } else {
        launchControl("org.tizen.art-app", { mode: "fullscreen" }, finish);
      }
    });
  }

  function back() {
    return run(function (finish) {
      // Explicit stock Home access remains available independently of the assigned default.
      native.application.launch("com.samsung.tv.csfs", function () { finish(); }, function (error) {
        finish(failure(error, "Samsung Home could not open."));
      });
    });
  }

  window.LauncherDevice = {
    available: available,
    activate: activate,
    back: back
  };
}());
