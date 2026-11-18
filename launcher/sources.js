/* Read-only device labels from the Home worker; input actions stay port-based. */
(function () {
  "use strict";
  var fs = window.tizen && window.tizen.filesystem;
  var prefix = "wgt-private/hdmi-sources.";
  var defaults = { 1: "Apple TV", 2: "Blu-ray", 3: "Arc Ultra", 4: "HDMI 4" };
  var ids = { 1: "apple-tv", 2: "hdmi-2", 3: "hdmi-3", 4: "hdmi-4" };
  var icons = { 1: "apple", 2: "disc", 3: "audio", 4: "hdmi" };
  var tiles = {};
  var art = {};
  var title = document.getElementById("selection-title");
  Object.keys(ids).forEach(function (port) {
    tiles[port] = document.querySelector('[data-id="' + ids[port] + '"]');
    art[icons[port]] = tiles[port].querySelector("svg");
  });
  var generation = 0;
  var timer;

  function useful(name) {
    return typeof name === "string" && name.trim() &&
      !/^(?:HDMI\s*\d*|unknown|none)$/i.test(name.trim());
  }

  function presentation(port, sources) {
    var row = sources.find(function (item) { return item.port === port && item.connected; });
    var name = row && (useful(row.name) ? row.name.trim() : useful(row.detectedName) ? row.detectedName.trim() : "");
    name = name || defaults[port];
    var identity = name + " " + (row ? row.detectedName : "");
    var icon = /apple\s*tv/i.test(identity) ? "apple" :
      /blu[ -]?ray|\bdvd\b|disc\s*player/i.test(identity) ? "disc" :
      /arc ultra|sonos|soundbar/i.test(identity) || port === 3 ? "audio" : "hdmi";
    return { name: name, icon: icon };
  }

  function validate(reply, token) {
    if (!reply || reply.token !== token || !Array.isArray(reply.sources) || reply.sources.length > 4) return null;
    var seen = new Set();
    for (var row of reply.sources) {
      if (!row || !Number.isInteger(row.port) || row.port < 1 || row.port > 4 || seen.has(row.port) ||
          typeof row.connected !== "boolean" || [row.name, row.detectedName].some(function (name) {
            return typeof name !== "string" || name.length > 128 || /[\u0000-\u001f\u007f]/.test(name);
          })) return null;
      seen.add(row.port);
    }
    return reply.sources;
  }

  function render(sources) {
    Object.keys(tiles).forEach(function (key) {
      var port = Number(key), tile = tiles[key], view = presentation(port, sources);
      var label = tile.querySelector(".tile-label");
      var accessibleName = view.name + ", HDMI " + port + (port === 3 ? ", audio only, unavailable" : "");
      if (label.textContent !== view.name) label.textContent = view.name;
      if (tile.getAttribute("aria-label") !== accessibleName) tile.setAttribute("aria-label", accessibleName);
      if (icons[port] !== view.icon) {
        var drawing = art[view.icon].cloneNode(true);
        // Paint definitions need unique IDs when two ports use the same artwork.
        drawing.querySelectorAll("[id]").forEach(function (node) {
          var old = node.id, next = old + "-port-" + port;
          node.id = next;
          drawing.querySelectorAll('[fill="url(#' + old + ')"]').forEach(function (shape) {
            shape.setAttribute("fill", "url(#" + next + ")");
          });
        });
        tile.querySelector("svg").replaceWith(drawing);
        icons[port] = view.icon;
      }
      if (tile.classList.contains("selected") && title.textContent !== view.name) title.textContent = view.name;
    });
  }

  function refresh(reset) {
    var current = ++generation;
    window.clearTimeout(timer);
    if (document.hidden) return;
    if (reset) render([]);
    if (!fs || !fs.openFile || !window.crypto || !window.crypto.getRandomValues) return;
    function finish(rows) {
      if (current !== generation) return;
      render(rows || []);
      timer = window.setTimeout(function () { refresh(false); }, 30000);
    }
    try {
      var token = Array.from(window.crypto.getRandomValues(new Uint8Array(16)), function (b) {
        return b.toString(16).padStart(2, "0");
      }).join("");
      fs.openFile(prefix + "reply", "a").close();
      var request = fs.openFile(prefix + "request", "w");
      try { request.writeString(token + "\n"); } finally { request.close(); }
      var deadline = window.performance.now() + 20000;
      function poll() {
        if (current !== generation || document.hidden) return;
        try {
          var file = fs.openFile(prefix + "reply", "r"), text;
          try { text = file.readString(8193); } finally { file.close(); }
          var rows = null;
          if (text.length && text.length <= 8192) {
            try { rows = validate(JSON.parse(text), token); } catch (error) { /* Incomplete or old reply. */ }
          }
          if (rows) { finish(rows); return; }
          var remaining = deadline - window.performance.now();
          if (remaining <= 0) { finish(null); return; }
          // The worker normally checks requests every three seconds. Check less
          // often after its first cycle, while retaining the final deadline read.
          var delay = remaining > 15000 ? 500 : 2000;
          timer = window.setTimeout(poll, Math.min(delay, remaining));
        } catch (error) { finish(null); }
      }
      timer = window.setTimeout(poll, 250);
    } catch (error) { finish(null); }
  }

  window.LauncherSources = { presentation: presentation, validate: validate };
  document.addEventListener("visibilitychange", function () { refresh(true); });
  refresh(true);
}());
