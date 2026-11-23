(function () {
  'use strict';
  // The live dashboard (dashboard_action.py serve): ask one small address every 5 seconds while this tab is visible, and reload
  // when the record changed. No record text is here, and nothing is written into the page but the one fixed line below.
  var first = null, failures = 0, waiting = false, timer = null;
  var stopped = 'The live dashboard stopped; this page is the last copy.';
  function busy() {
    var open = document.querySelector('dialog[open]');
    var focus = document.activeElement;
    return !!open || !!(focus && (focus.tagName === 'INPUT' || focus.tagName === 'SELECT' || focus.tagName === 'TEXTAREA'));
  }
  function stop() {
    window.clearInterval(timer);
    var line = document.getElementById('live-stopped');
    if (line) { line.textContent = stopped; line.hidden = false; }
  }
  async function ask() {
    if (document.visibilityState !== 'visible') { return; }
    try {
      var response = await fetch('api/changed', {cache: 'no-store', credentials: 'same-origin'});
      if (!response.ok) { throw new Error('no'); }
      var data = await response.json();
      failures = 0;
      if (first === null) { first = data.mark; return; }
      if (data.mark !== first) {
        if (busy()) { waiting = true; return; }
        window.location.reload();
      }
    } catch (error) {
      failures += 1;
      if (failures >= 3) { stop(); }
    }
  }
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'visible') { ask(); } });
  document.addEventListener('focusout', function () {
    if (waiting) { window.setTimeout(function () { if (!busy()) { window.location.reload(); } }, 300); }
  });
  ask();
  timer = window.setInterval(ask, 5000);
}());
