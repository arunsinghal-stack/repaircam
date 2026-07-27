/* Small helpers shared by the pages. No framework, no build step — this has to
   keep working on whatever browser is on the bench tablet. */

window.RepairCam = (function () {
  'use strict';

  /** Update every [data-field="name"] inside root. */
  function setField(root, name, value) {
    var nodes = root.querySelectorAll('[data-field="' + name + '"]');
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].textContent !== value) nodes[i].textContent = value;
    }
  }

  /**
   * Poll /api/status and hand the result to onUpdate.
   * Backs off when the page is hidden so a tablet left on the bench overnight
   * is not hammering the recorder.
   */
  function pollStatus(onUpdate, intervalMs) {
    var interval = intervalMs || 1000;
    var failures = 0;

    function tick() {
      if (document.hidden) return schedule(5000);
      fetch('/api/status', { cache: 'no-store' })
        .then(function (response) {
          if (!response.ok) throw new Error('status ' + response.status);
          return response.json();
        })
        .then(function (data) {
          failures = 0;
          try { onUpdate(data); } catch (err) { console.error(err); }
          schedule(interval);
        })
        .catch(function () {
          // The recorder may be restarting. Slow down rather than give up.
          failures += 1;
          schedule(Math.min(interval * Math.pow(2, failures), 15000));
        });
    }

    function schedule(delay) { window.setTimeout(tick, delay); }
    schedule(interval);
  }

  /**
   * MJPEG streams die when ffmpeg exits or the network hiccups, and the browser
   * just leaves a broken image. Reload it with a cache-busting query so the
   * technician gets the picture back without touching anything.
   */
  function keepPreviewAlive(elementId) {
    var img = document.getElementById(elementId);
    if (!img) return;
    var base = img.src.split('?')[0];

    img.addEventListener('error', function () {
      window.setTimeout(function () { img.src = base + '?t=' + Date.now(); }, 3000);
    });

    // Coming back from a locked screen usually leaves a stalled stream behind.
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) img.src = base + '?t=' + Date.now();
    });
  }

  return { setField: setField, pollStatus: pollStatus, keepPreviewAlive: keepPreviewAlive };
})();
