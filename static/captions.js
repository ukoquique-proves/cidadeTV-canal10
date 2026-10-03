/*
 * captions.js — decides WHEN each caption is shown.  Pure logic (no DOM, no
 * timers), so it runs in the browser and under `node tests/captions.test.js`.
 *
 * The server sends  { pt, es, age_start, age_end }  where age_* are seconds
 * before "now" (at the moment of sending) that the speech started / ended AT
 * THE LIVE EDGE.  The player is `latencyS` seconds behind the live edge, so the
 * speech reaches the viewer's screen
 *
 *      latencyS - age_start     seconds after the message arrives.
 *
 * If that is negative the caption is LATE (the pipeline took longer than the
 * video delay): it is shown immediately and counted in the stats, so the UI can
 * tell the user to increase the video delay.
 *
 * `offsetS` is a manual fine-tune (+ = show captions later).
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.CaptionScheduler = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  const MIN_SHOW_MS = 1200;  // never flash a caption for less than this
  const MAX_SHOW_MS = 8000;  // never keep one longer than this
  const HOLD_MS = 400;       // linger a little after the speech ends
  const MAX_WAIT_MS = 6000;  // a caption stuck this long behind others is dropped (catch up)

  class CaptionScheduler {
    constructor() {
      this.queue = [];      // waiting captions, sorted by showAt
      this.current = null;  // caption on screen
      this.stats = { total: 0, late: 0, lateSumS: 0, dropped: 0 };
    }

    push(msg, nowMs, latencyS, offsetS) {
      const showDelayMs = (latencyS - msg.age_start + (offsetS || 0)) * 1000;
      const showAt = nowMs + Math.max(0, showDelayMs);
      const idealHide = nowMs + (latencyS - msg.age_end + (offsetS || 0)) * 1000 + HOLD_MS;
      const hideAt = Math.min(Math.max(idealHide, showAt + MIN_SHOW_MS), showAt + MAX_SHOW_MS);

      this.stats.total += 1;
      if (showDelayMs < -250) {                 // more than 250 ms late
        this.stats.late += 1;
        this.stats.lateSumS += -showDelayMs / 1000;
      }
      this.queue.push({ pt: msg.pt || '', es: msg.es || '', showAt, hideAt, shownAt: 0 });
      this.queue.sort((a, b) => a.showAt - b.showAt);
    }

    /** Call often (e.g. every 100 ms).  Returns {pt, es} to display, or null. */
    tick(nowMs) {
      if (this.current && nowMs >= this.current.hideAt) this.current = null;

      // captions that waited too long behind others are stale: drop them to catch up
      while (this.queue.length && nowMs - this.queue[0].showAt > MAX_WAIT_MS) {
        this.queue.shift();
        this.stats.dropped += 1;
      }

      const next = this.queue[0];
      if (next && next.showAt <= nowMs &&
          (!this.current || nowMs >= this.current.shownAt + MIN_SHOW_MS)) {
        this.current = this.queue.shift();
        this.current.shownAt = nowMs;
        this.current.hideAt = Math.max(this.current.hideAt, nowMs + MIN_SHOW_MS);
      }
      return this.current ? { pt: this.current.pt, es: this.current.es } : null;
    }

    clear() { this.queue = []; this.current = null; }

    resetStats() { this.stats = { total: 0, late: 0, lateSumS: 0, dropped: 0 }; }

    get lateAvgS() { return this.stats.late ? this.stats.lateSumS / this.stats.late : 0; }
  }

  CaptionScheduler.MIN_SHOW_MS = MIN_SHOW_MS;
  CaptionScheduler.HOLD_MS = HOLD_MS;
  return CaptionScheduler;
});
