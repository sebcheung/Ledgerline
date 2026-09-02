// Ticks webhook retry countdowns once a second between SSE refreshes.
//
// Each `.countdown[data-seconds]` element carries a value already computed
// server-side from the Postgres clock (ledger.readmodels.webhooks). This
// script never re-derives it from a client-side clock comparison, only
// counts down from it using the browser's own Date.now() deltas -- that is
// what makes it skew-corrected: a wrong workstation clock can never produce
// a wrong countdown, because the workstation's *absolute* clock setting is
// never read, only elapsed time since this exact element first appeared.
//
// No cross-tick bookkeeping is needed to detect "this is fresh data": an
// SSE swap replaces the DOM node outright, so a newly-rendered countdown is
// always a brand-new element with no `data-loaded-at-ms` yet -- the first
// tick to see it sets that baseline.
(function () {
  "use strict";

  function formatCountdown(seconds) {
    if (seconds === null || Number.isNaN(seconds)) {
      return "—";
    }
    if (seconds <= 0) {
      return "due now";
    }
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    var secs = Math.floor(seconds % 60);
    if (hours) {
      return "in " + hours + "h " + minutes + "m";
    }
    if (minutes) {
      return "in " + minutes + "m " + secs + "s";
    }
    return "in " + secs + "s";
  }

  function tick() {
    document.querySelectorAll(".countdown[data-seconds]").forEach(function (el) {
      var raw = el.getAttribute("data-seconds");
      if (raw === "" || raw === null) {
        return;
      }
      if (!el.dataset.loadedAtMs) {
        el.dataset.loadedAtMs = String(Date.now());
        el.dataset.baseSeconds = raw;
      }
      var baseSeconds = Number.parseFloat(el.dataset.baseSeconds);
      if (Number.isNaN(baseSeconds)) {
        return;
      }
      var elapsedSeconds = (Date.now() - Number(el.dataset.loadedAtMs)) / 1000;
      el.textContent = formatCountdown(Math.round(baseSeconds - elapsedSeconds));
    });
  }

  setInterval(tick, 1000);
})();
