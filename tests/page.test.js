// Run:  npm i jsdom && node tests/page.test.js   (tests static/index.html with stubbed Hls/WebSocket)
const { JSDOM } = require('jsdom');
const fs = require('fs');
const assert = require('assert');
const root = require('path').resolve(__dirname, '..');
const html = fs.readFileSync(root + '/static/index.html', 'utf8');
const capJs = fs.readFileSync(root + '/static/captions.js', 'utf8');
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  const sockets = [], hlsInstances = [];
  const dom = new JSDOM(html, {
    url: 'http://localhost:8000/', runScripts: 'dangerously', pretendToBeVisual: true,
    beforeParse(w) {
      w.eval(capJs);                                    // what <script src=captions.js> would do
      class FakeWS { constructor(u) { this.url = u; this.readyState = 0; sockets.push(this); }
        close() { this.readyState = 3; } }
      FakeWS.OPEN = 1; FakeWS.CONNECTING = 0; w.WebSocket = FakeWS;
      class FakeHls { constructor(c) { this.cfg = c; this.latency = 10; this.handlers = {}; hlsInstances.push(this); }
        static isSupported() { return true; }
        loadSource() {} attachMedia() {} destroy() {} on(e, f) { this.handlers[e] = f; } startLoad() {} recoverMediaError() {} }
      FakeHls.Events = { MANIFEST_PARSED: 'm', ERROR: 'e' }; FakeHls.ErrorTypes = { NETWORK_ERROR: 'n', MEDIA_ERROR: 'd' };
      w.Hls = FakeHls;
      w.HTMLMediaElement.prototype.play = () => Promise.resolve();
    },
  });
  const w = dom.window, d = w.document;

  // 1) ws:// scheme on http, one socket at start, HLS got the configured delay
  assert.strictEqual(sockets.length, 1);
  assert.strictEqual(sockets[0].url, 'ws://localhost:8000/captions');
  assert.strictEqual(hlsInstances[0].cfg.liveSyncDuration, 10);

  // 2) error + close (what a real browser fires on failure) must produce ONE reconnect, not two
  sockets[0].onerror(); sockets[0].onclose();
  await sleep(1300);
  assert.strictEqual(sockets.length, 2, 'duplicate reconnect: ' + sockets.length + ' sockets');
  console.log('ok   single reconnect after error+close');

  // 3) a caption is shown at latency - age_start and hidden after the speech ends
  sockets[1].readyState = 1; sockets[1].onopen();
  sockets[1].onmessage({ data: JSON.stringify({ pt: 'Olá mundo', es: 'Hola mundo', age_start: 9.5, age_end: 9.0 }) });
  await sleep(250);  assert.strictEqual(d.getElementById('caption-pt').textContent, '', 'shown too early');
  await sleep(700);  assert.strictEqual(d.getElementById('caption-pt').textContent, 'Olá mundo');
                     assert.strictEqual(d.getElementById('caption-es').textContent, 'Hola mundo');
  await sleep(1500); assert.strictEqual(d.getElementById('caption-pt').textContent, '', 'not hidden');
  console.log('ok   caption shown on time and hidden afterwards');

  // 4) ping frames are ignored; late captions are counted and the UI warns
  sockets[1].onmessage({ data: '{"ping":true}' });
  for (let i = 0; i < 4; i++) sockets[1].onmessage({ data: JSON.stringify({ pt: 'x' + i, es: '', age_start: 14, age_end: 12 }) });
  await sleep(1200);
  const info = d.getElementById('sync-info');
  assert.ok(/atrasadas/.test(info.textContent) && info.className === 'warn', info.textContent);
  console.log('ok   late captions flagged:', info.textContent);

  // 5) "Aplicar" re-creates the player with the new delay; ±0.5 s nudge works
  d.getElementById('sync-input').value = '14';
  d.getElementById('btn-apply-sync').click();
  assert.strictEqual(hlsInstances[hlsInstances.length - 1].cfg.liveSyncDuration, 14);
  d.getElementById('btn-later').click(); d.getElementById('btn-later').click();
  assert.strictEqual(d.getElementById('offset-label').textContent, '1,0 s');
  console.log('ok   apply delay + nudge');
  w.close(); process.exit(0);
})().catch(e => { console.error('FAIL', e.message); process.exit(1); });
