/* Private mailbox shared with the installed Art quiet-hours worker. */
(function () {
  "use strict";
  var fs = window.tizen && window.tizen.filesystem;
  var path = "wgt-private/quiet-hours.";
  var available = Boolean(fs && fs.openFile && window.crypto && window.crypto.getRandomValues);
  var pending = false;
  var unconfirmed = "The TV could not confirm that change. Check the times and try again.";

  function validPair(pair) {
    return pair && Number.isInteger(pair.start) && pair.start >= 0 && pair.start < 1440 &&
      Number.isInteger(pair.end) && pair.end >= 0 && pair.end < 1440 && pair.start !== pair.end;
  }

  function readReply(token) {
    var file = fs.openFile(path + "reply", "r");
    var text;
    try { text = file.readString(128); } finally { file.close(); }
    var match = /^([0-9a-f]{32}) ([0-9a-f]{32}) (ok|conflict) (0|[1-9]\d{0,3}) (0|[1-9]\d{0,3})\n$/.exec(text);
    if (!match || match[0] !== text || match[1] !== token) return null;
    var pair = { session: match[2], start: Number(match[4]), end: Number(match[5]) };
    return validPair(pair) ? { result: match[3], pair: pair } : null;
  }

  function request(previous, next) {
    if (!available || pending) return Promise.reject(new Error("Quiet hours are unavailable."));
    var saving = Boolean(previous);
    if (saving && (!validPair(previous) || !/^[0-9a-f]{32}$/.test(previous.session) || !validPair(next))) {
      return Promise.reject(new Error("Choose two different times."));
    }
    pending = true;
    return new Promise(function (resolve, reject) {
      var done = false;
      var pollTimer;
      var deadline = window.setTimeout(function () {
        finish(new Error(unconfirmed));
      }, 12000);
      function finish(error, pair) {
        if (done) return;
        done = true;
        pending = false;
        window.clearTimeout(deadline);
        window.clearTimeout(pollTimer);
        if (error) reject(error); else resolve(pair);
      }
      function failure() { finish(new Error(unconfirmed)); }
      try {
        var bytes = window.crypto.getRandomValues(new Uint8Array(16));
        var token = Array.from(bytes, function (b) { return b.toString(16).padStart(2, "0"); }).join("");
        var message = saving ? ["set", token, previous.session, previous.start, previous.end, next.start, next.end] : ["get", token];
        var line = message.join(" ") + "\n";
        // The app creates its reply file so the worker can preserve its security label.
        fs.openFile(path + "reply", "a").close();
        // The worker ignores incomplete lines. Opening with "w" discards the previous request.
        var file = fs.openFile(path + "request", "w");
        try { file.writeString(line); } finally { file.close(); }
        var check = fs.openFile(path + "request", "r");
        try {
          if (check.readString(128) !== line) throw new Error("Incomplete quiet-hours request");
        } finally { check.close(); }
        function poll() {
          if (done) return;
          try {
            var reply = readReply(token);
            if (!reply) { pollTimer = window.setTimeout(poll, 250); return; }
            if (reply.result !== "ok" || (saving && (reply.pair.session !== previous.session ||
                reply.pair.start !== next.start || reply.pair.end !== next.end))) {
              finish(new Error(unconfirmed));
            } else finish(null, reply.pair);
          } catch (error) { failure(); }
        }
        poll();
      } catch (error) { failure(); }
    });
  }

  window.LauncherSchedule = {
    available: available,
    read: function () { return request(null, null); },
    set: function (previous, next) { return request(previous, next); }
  };
}());
