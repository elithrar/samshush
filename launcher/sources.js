/* Read-only device labels from the Home worker; input actions stay port-based. */
(function () {
  "use strict";
  var fs = window.tizen && window.tizen.filesystem;
  var prefix = "wgt-private/hdmi-sources.";
  var defaults = { 1: "Apple TV", 2: "Blu-ray", 3: "Arc Ultra", 4: "HDMI 4" };
  var ids = { 1: "apple-tv", 2: "hdmi-2", 3: "hdmi-3", 4: "hdmi-4" };
  var tiles = {};
  Object.keys(ids).forEach(function (port) {
    tiles[port] = document.querySelector('[data-id="' + ids[port] + '"]');
  });
  var art = {
    apple: tiles[1].querySelector("svg").cloneNode(true),
    disc: tiles[2].querySelector("svg").cloneNode(true),
    audio: tiles[3].querySelector("svg").cloneNode(true),
    hdmi: tiles[4].querySelector("svg").cloneNode(true)
  };
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
      tile.querySelector(".tile-label").textContent = view.name;
      tile.setAttribute("aria-label", view.name + ", HDMI " + port + (port === 3 ? ", audio only, unavailable" : ""));
      if (tile.dataset.art !== view.icon) {
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
        tile.dataset.art = view.icon;
      }
      if (tile.classList.contains("selected")) document.getElementById("selection-title").textContent = view.name;
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
      var started = Date.now();
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
          if (Date.now() - started >= 20000) { finish(null); return; }
          timer = window.setTimeout(poll, 250);
        } catch (error) { finish(null); }
      }
      poll();
    } catch (error) { finish(null); }
  }

  window.LauncherSources = { presentation: presentation, validate: validate };
  document.addEventListener("visibilitychange", function () { refresh(true); });
  refresh(true);
}());
