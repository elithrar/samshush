(function () {
  "use strict";

  var indicator = document.getElementById("microphone-recording");
  var dataControl = window.tizen && window.tizen.datacontrol;
  if (!dataControl) return;

  var provider = "http://samshush.local/datacontrol/provider/MicrophoneStatus";
  var maxReplyAge = 2000;
  var consumer;
  var sequence = 0;
  var timer;
  var expiry;

  function reset() {
    sequence++;
    window.clearTimeout(timer);
    window.clearTimeout(expiry);
    indicator.hidden = true;
  }

  function sample() {
    if (document.hidden) return;
    var request = ++sequence;
    var started = performance.now();
    function finish(state) {
      if (request !== sequence || document.hidden) return;
      sequence++; // Discard duplicate callbacks and replies after a timeout.
      window.clearTimeout(timer);
      window.clearTimeout(expiry);
      var remaining = maxReplyAge - (performance.now() - started);
      indicator.hidden = state !== "active" || remaining <= 0;
      if (!indicator.hidden) {
        expiry = window.setTimeout(function () { indicator.hidden = true; }, remaining);
      }
      if (state === "unavailable") consumer = null;
      timer = window.setTimeout(sample, state === "unavailable" ? 10000 : 1000);
    }

    timer = window.setTimeout(function () { finish("unavailable"); }, maxReplyAge);
    try {
      if (!consumer) consumer = dataControl.getDataControlConsumer(provider, "status", "MAP");
      consumer.getValue(request, "recording", function (values, reply) {
        if (reply === request && Array.isArray(values) && values.length === 1) finish(values[0]);
        else finish("unavailable");
      }, function () { finish("unavailable"); });
    } catch (error) {
      finish("unavailable");
    }
  }

  document.addEventListener("visibilitychange", function () {
    reset();
    if (!document.hidden) sample();
  });
  window.addEventListener("pagehide", reset);
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) { reset(); sample(); }
  });
  sample();
}());
