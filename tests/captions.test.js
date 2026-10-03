// Run:  node tests/captions.test.js
const assert = require('assert');
const CaptionScheduler = require('../static/captions.js');

const S = 1000;
function run(fn) { try { fn(); console.log('ok   ' + fn.name); } catch (e) { console.error('FAIL ' + fn.name + '\n' + e.stack); process.exitCode = 1; } }

run(function shows_exactly_after_latency_minus_age() {
  const s = new CaptionScheduler();
  // player 10 s behind live; speech started 6 s ago and ended 3 s ago at the live edge
  s.push({ pt: 'A', es: 'a', age_start: 6, age_end: 3 }, 0, 10, 0);
  assert.strictEqual(s.tick(3.9 * S), null);               // not before 4 s
  assert.deepStrictEqual(s.tick(4.0 * S), { pt: 'A', es: 'a' });
  assert.deepStrictEqual(s.tick(6.9 * S), { pt: 'A', es: 'a' });
  assert.strictEqual(s.stats.late, 0);
});

run(function hides_when_speech_has_ended() {
  const s = new CaptionScheduler();
  s.push({ pt: 'A', es: '', age_start: 6, age_end: 3 }, 0, 10, 0);
  s.tick(4 * S);
  assert.ok(s.tick(7.3 * S));          // 7 s speech end + 0.4 s hold
  assert.strictEqual(s.tick(7.5 * S), null);
});

run(function late_caption_is_shown_immediately_and_counted() {
  const s = new CaptionScheduler();
  s.push({ pt: 'L', es: '', age_start: 12, age_end: 9 }, 0, 10, 0);  // 2 s too late
  assert.deepStrictEqual(s.tick(0), { pt: 'L', es: '' });
  assert.strictEqual(s.stats.late, 1);
  assert.ok(Math.abs(s.lateAvgS - 2) < 1e-9);
  // still readable for at least MIN_SHOW even though its speech "already ended"
  assert.ok(s.tick(CaptionScheduler.MIN_SHOW_MS - 1));
});

run(function manual_offset_shifts_display_time() {
  const s = new CaptionScheduler();
  s.push({ pt: 'O', es: '', age_start: 6, age_end: 3 }, 0, 10, 1.5);  // +1.5 s later
  assert.strictEqual(s.tick(4 * S), null);
  assert.ok(s.tick(5.5 * S));
});

run(function simultaneous_captions_are_shown_in_order_without_skipping() {
  const s = new CaptionScheduler();
  s.push({ pt: '1', es: '', age_start: 20, age_end: 18 }, 0, 10, 0);
  s.push({ pt: '2', es: '', age_start: 18, age_end: 16 }, 0, 10, 0);
  assert.strictEqual(s.tick(0).pt, '1');
  assert.strictEqual(s.tick(500).pt, '1');                          // 1 stays for MIN_SHOW
  assert.strictEqual(s.tick(CaptionScheduler.MIN_SHOW_MS).pt, '2'); // then 2 (not skipped)
});

run(function stale_backlog_is_dropped_to_catch_up() {
  const s = new CaptionScheduler();
  s.push({ pt: 'old', es: '', age_start: 30, age_end: 28 }, 0, 10, 0);
  s.push({ pt: 'x', es: '', age_start: 30, age_end: 28 }, 0, 10, 0);
  s.tick(0);                                   // 'old' on screen, 'x' waiting
  s.tick(7000);                                // x waited 7 s > MAX_WAIT → dropped
  assert.strictEqual(s.stats.dropped, 1);
  assert.strictEqual(s.tick(7100), null);
});

run(function clear_empties_everything() {
  const s = new CaptionScheduler();
  s.push({ pt: 'x', es: '', age_start: 1, age_end: 0 }, 0, 10, 0);
  s.clear();
  assert.strictEqual(s.tick(20 * S), null);
});
