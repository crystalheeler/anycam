// Behavioural test for the Enhanced View JS (2.6.3 live view, 2.6.5 changes).
// Loads the REAL vendored www/video-rtc.js as the base class, extracts the
// go2rtc block and the focus block from the built page script, and drives
// them with a fake clock and a stub DOM.
// Run: node tests/test_page.mjs <page.js> <video-rtc.js>   (tests/run_tests.py
// builds page.js from the real page and calls this)
import fs from 'node:fs';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';

const [pageJs, vrtcPath] = process.argv.slice(2);
let pass = 0, fail = 0;
const check = (name, cond, detail = '') => {
  if (cond) { pass++; console.log('  PASS  ' + name); }
  else { fail++; console.log('  FAIL  ' + name + (detail ? '  [' + detail + ']' : '')); }
};

// ── fake clock ───────────────────────────────────────────────────────────────
let now = 0, seq = 0;
const timers = new Map();
globalThis.setTimeout = (fn, ms) => { const id = ++seq; timers.set(id, {at: now + (ms || 0), fn, every: 0}); return id; };
globalThis.setInterval = (fn, ms) => { const id = ++seq; timers.set(id, {at: now + ms, fn, every: ms}); return id; };
globalThis.clearTimeout = globalThis.clearInterval = id => { timers.delete(id); };
function advance(ms) {
  const end = now + ms;
  for (;;) {
    let next = null;
    for (const [id, t] of timers) if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t];
    if (!next) break;
    const [id, t] = next;
    now = t.at;
    if (t.every) t.at += t.every; else timers.delete(id);
    t.fn();
  }
  now = end;
}
const flush = () => new Promise(r => setImmediate(r));

// ── DOM stubs ────────────────────────────────────────────────────────────────
function el(id, extra = {}) {
  const cls = new Set();
  return Object.assign({id, style: {}, innerHTML: '', textContent: '', disabled: false,
    value: '', title: '', children: [], src: '',
    classList: {toggle(c, on) { on ? cls.add(c) : cls.delete(c); }, contains: c => cls.has(c)},
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    addEventListener() {},
    closest() { return null; }, remove() {}}, extra);
}
const DOM = {
  'focus-img': el('focus-img'), 'focus-video': el('focus-video'),
  'focus-info': el('focus-info'),
  'focus-classic-grp': el('focus-classic-grp', {style: {display: 'none'}}),
  'focus-overlay': el('focus-overlay', {style: {display: 'none'}}),
  'focus-loading': el('focus-loading', {style: {display: 'none'}}),
  'focus-loading-note': el('focus-loading-note'),
  'focus-warning': el('focus-warning', {style: {display: 'none'}}),
  'focus-warn-text': el('focus-warn-text'),
};
globalThis.HTMLElement = class { appendChild(c) { return c; } };
globalThis.WebSocket = {CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3};
globalThis.window = globalThis;
globalThis.location = {origin: 'https://ha.local'};
// HA's app panel: the page runs in its iframe.
const posted = [];
globalThis.parent = {postMessage: (msg, origin) => posted.push([msg, origin])};
// Touch-screen orientation query, flipped by the tests.
const mq = {matches: false, _l: [], addEventListener(t, f) { this._l.push(f); }};
globalThis.matchMedia = q => { mq.query = q; return mq; };
const rotate = land => { mq.matches = land; mq._l.forEach(f => f()); };
const registry = {};
globalThis.customElements = {get: n => registry[n], define: (n, c) => { registry[n] = c; }};
globalThis.document = {
  hidden: false,
  getElementById: id => DOM[id] || null,
  querySelector: () => null,
  createElement: tag => (registry[tag] ? new registry[tag]() : el(tag)),
  addEventListener() {},
};

// Load the real base class. It extends HTMLElement at evaluation time.
const vrtc = await import(pathToFileURL(vrtcPath).href);

// ── extract the blocks from the built page ───────────────────────────────────
const page = fs.readFileSync(pageJs, 'utf8');
const start = page.indexOf('/* ── go2rtc live view (2.6.3, Tier 2)');
const mid = page.indexOf('/* ── Focus / enhanced view');
const end = page.indexOf('/* ── Storage browser');
if (start < 0 || mid < 0 || end < 0) { console.log('could not locate the blocks'); process.exit(2); }
let block = page.slice(start, mid);
const focusBlock = page.slice(mid, end);
const importCall = "import(BASE + '/go2rtc/video-rtc.js')";
if (!block.includes(importCall)) { console.log('import call not found'); process.exit(2); }
block = block.replace(importCall, 'Promise.resolve(globalThis.__VRTC)');
globalThis.__VRTC = vrtc;

// Host globals the block expects from the rest of the page.
const calls = {toast: [], poll: [], fetch: []};
Object.assign(globalThis, {
  BASE: '/api/hassio_ingress/TOKEN',
  showToast: (m, e) => calls.toast.push([m, !!e]),
  // Faithful to the real _startFocusPoll, whose first line sets _focusCamId.
  _startFocusPoll: (id) => { vm.runInThisContext('_focusCamId = ' + JSON.stringify(id));
                             calls.poll.push(id); return Promise.resolve(); },
  displayName: c => c.name,
  esc: s => String(s).replace(/</g, '&lt;'),
  fetch: async (url) => { calls.fetch.push(url); return {json: async () => globalThis.__nextInfo}; },
});
vm.runInThisContext('let _focusCamId = null;');
vm.runInThisContext(block);
const g = expr => vm.runInThisContext(expr);

console.log('\n[J] player class');
check('player module loads', await g('_go2rtcLoadPlayer()') === true);
const AnyCamVideo = registry['anycam-video'];
check("custom element 'anycam-video' defined", typeof AnyCamVideo === 'function');
check('AnyCamVideo extends the real VideoRTC', AnyCamVideo && AnyCamVideo.prototype instanceof vrtc.VideoRTC);
check('second load is cached, no second define', await g('_go2rtcLoadPlayer()') === true);
{
  const v = new AnyCamVideo(); const seen = [];
  v.onanycam = k => seen.push(k);
  v.wsState = WebSocket.OPEN; v.onclose();
  check('onclose: unexpected close is reported', seen.includes('close'));
  const w = new AnyCamVideo(); const seen2 = [];
  w.onanycam = k => seen2.push(k);
  w.wsState = WebSocket.CLOSED; w.onclose();
  check('onclose: intended close after WebRTC handoff is NOT reported', !seen2.includes('close'));
  const p = new AnyCamVideo(); p.oninit();
  check('oninit: blank poster set (no Android gray play icon)',
        typeof p.video.poster === 'string' && p.video.poster.startsWith('data:image/gif;base64,'));
  check('oninit: no controls, muted', p.video.controls === false && p.video.muted === true);
}

// ── session helpers ──────────────────────────────────────────────────────────
const cam = {id: 'cam1', name: 'Hikvision <PTZ>', stream_codec: 'hevc'};
let session = 100;
function mount(extra = {}) {
  calls.toast.length = calls.poll.length = 0;
  session++;
  g('_focusSession = ' + session);
  g(`_go2rtcMount('cam1', ${JSON.stringify(cam)}, ${JSON.stringify(Object.assign({stream: 's0', profile: 0, codec: 'hevc'}, extra))}, ${session})`);
  const s = g('_go2rtc');
  s.el.ondisconnect = () => { s._disconnected = true; };
  s.el.remove = () => { s._removed = true; };
  return s;
}
const ev = (k, v) => g(`_go2rtcEvent(${session}, ${JSON.stringify(k)}, ${JSON.stringify(v)})`);
const live = () => g('_go2rtc');
const declined = () => g('_go2rtcDeclined');
const loading = () => DOM['focus-loading'].style.display;

console.log('\n[K] mount');
let s = mount();
check('mount: video wrapper shown, JPEG image hidden',
      DOM['focus-video'].style.display === 'block' && DOM['focus-img'].style.display === 'none');
check('mount: player socket URL uses the ingress path + stream name',
      s.el.wsURL === 'wss://ha.local/api/hassio_ingress/TOKEN/go2rtc/ws?src=s0', s.el.wsURL);
check('mount: mode webrtc,mse and video only', s.el.mode === 'webrtc,mse' && s.el.media === 'video');
check('mount: Classic button shown', DOM['focus-classic-grp'].style.display === '');
check('mount: loading message shown', loading() === 'block');
check('info bar escapes the camera name', DOM['focus-info'].innerHTML.includes('Hikvision &lt;PTZ>'));
check('info bar says connecting before first frame', DOM['focus-info'].innerHTML.includes('connecting'));
check("info bar: no 'decoded on this device' text", !DOM['focus-info'].innerHTML.includes('decoded on this device'));

console.log('\n[L] fallback decisions before the first frame');
s = mount();
ev('open', ['mse', 'webrtc']);
ev('error', 'webrtc/offer: ICE failed');
check('WebRTC-only error does NOT fall back (MSE may still play)', live() && calls.poll.length === 0);
ev('error', 'mse: streams: codecs not matched: video:H265');
check('every attempted mode errored -> falls back', live() === null && calls.poll.length === 1);
check('codec failure IS remembered (same result on every open)', !!declined()['cam1']);
check('fallback toast is marked as an error', calls.toast.length === 1 && calls.toast[0][1] === true);
check('fallback tears down the player', s._disconnected && s._removed);
check('fallback restores the JPEG image and hides Classic',
      DOM['focus-img'].style.display === '' && DOM['focus-video'].style.display === 'none'
      && DOM['focus-classic-grp'].style.display === 'none');
g("delete _go2rtcDeclined['cam1']");

s = mount();
ev('open', ['mse', 'webrtc']);
ev('error', 'anycam: go2rtc unreachable');
check('error naming no mode is fatal at once', live() === null && calls.poll.length === 1);
check('error naming no mode is NOT remembered', !declined()['cam1']);

s = mount();
ev('open', ['mse']);
ev('close');
check('one unexpected close before first frame: keep trying', live() !== null && calls.poll.length === 0);
ev('close');
check('second unexpected close: fall back', live() === null && calls.poll.length === 1);
check('dropped connection is NOT remembered', !declined()['cam1']);

console.log('\n[M] watchdog (30 s since 2.6.5)');
s = mount();
advance(29900);
check('no fall back at 29.9 s', live() !== null);
advance(200);
check('falls back once 30 s pass with no video', live() === null && calls.poll.length === 1);
check('reason names the timeout', (calls.toast[0] || [''])[0].includes('no video within 30 s'));
check('timeout is NOT remembered: next open tries live view again', !declined()['cam1']);

s = mount();
advance(23000);
ev('playing');
check('the .73 case: first frame at 23 s plays live', live() && live().played === true);
check('loading message hidden on the first frame', loading() === 'none');

s = mount();
document.hidden = true;
advance(30100);
check('hidden tab: watchdog re-arms instead of failing', live() !== null);
document.hidden = false;
advance(30100);
check('visible again: watchdog then fails normally', live() === null);

console.log('\n[N] after the first frame');
s = mount();
ev('open', ['mse', 'webrtc']);
advance(3000);
ev('playing');
check('playing marks the session played', live() && live().played === true);
advance(40000);
check('watchdog cleared once playing (no fall back at 43 s)', live() !== null);
ev('error', 'mse: something');
ev('close'); ev('close'); ev('close');
check('errors and closes after first frame never force classic', live() !== null && calls.poll.length === 0);
check('close after first frame shows reconnecting', live().mode === 'reconnecting');
ev('mode', 'WebRTC');
check('mode event updates the label', live().mode === 'WebRTC'
      && DOM['focus-info'].innerHTML.includes('Live (WebRTC)'));

s = mount();
s.el.video = {videoWidth: 3840, videoHeight: 2160,
              getVideoPlaybackQuality: () => ({totalVideoFrames: s._f || 0})};
s._f = 0; advance(1000);
check('stats: no frames yet, not played', live().played === false);
s._f = 25; advance(1000);
check('stats backup: frames decoded counts as played', live().played === true);
check('stats backup: loading message hidden', loading() === 'none');
s._f = 50; advance(1000);
check('stats: fps measured as frames per second', live().fps === 25);
check('info bar shows resolution and fps', DOM['focus-info'].innerHTML.includes('3840x2160 · 25 fps'));

console.log('\n[O] user actions');
s = mount();
g('focusUseClassic()');
await flush();
check('Classic button: switches to the classic view', live() === null && calls.poll.length === 1);
check('Classic button: NOT remembered as a failure', !declined()['cam1']);
check('Classic button: toast is not an error', calls.toast[0] && calls.toast[0][1] === false);

g("_go2rtcDeclined['cam1'] = 'test'");
calls.fetch.length = 0;
const r = await g(`_go2rtcTryFocus('cam1', ${JSON.stringify(cam)}, _focusSession)`);
check('declined camera goes straight to classic, no server call', r === false && calls.fetch.length === 0);
g("delete _go2rtcDeclined['cam1']");
check('profile switching is gone', g('typeof _go2rtcSwitchProfile') === 'undefined');

console.log('\n[P] landscape kiosk');
check('query is landscape on a touch screen',
      mq.query === '(orientation: landscape) and (pointer: coarse)', mq.query);
const ov = DOM['focus-overlay'];
posted.length = 0;
rotate(true);
check('overlay closed: rotating does nothing', posted.length === 0 && !ov.classList.contains('focus-landscape'));
ov.style.display = 'flex';
rotate(true);
check('open + landscape: full-screen class set', ov.classList.contains('focus-landscape'));
check('open + landscape: HA asked for kiosk mode',
      posted.length === 1 && posted[0][0].type === 'home-assistant/subscribe-properties'
      && posted[0][0].kioskMode === true);
check('message targets this origin only', posted[0] && posted[0][1] === 'https://ha.local');
rotate(true);
check('no duplicate request on a repeated event', posted.length === 1);
rotate(false);
check('portrait: class removed and kiosk released',
      !ov.classList.contains('focus-landscape') && posted.length === 2
      && posted[1][0].type === 'home-assistant/unsubscribe-properties');
rotate(true);
ov.style.display = 'none';
g('_focusLandscapeSync()');
check('closing in landscape releases kiosk mode',
      posted.length === 4 && posted[3][0].type === 'home-assistant/unsubscribe-properties'
      && !ov.classList.contains('focus-landscape'));
rotate(false);

// ── B4: camera values in onclick attributes (2.6.6) ─────────────────────────
console.log('\n[T] escaping camera values in click handlers (B4)');
{
  const grab = name => {
    const i = page.indexOf('function ' + name + '(');
    let depth = 0, j = page.indexOf('{', i);
    for (let k = j; k < page.length; k++) {
      if (page[k] === '{') depth++;
      else if (page[k] === '}' && --depth === 0) return page.slice(i, k + 1);
    }
  };
  vm.runInThisContext(grab('esc') + '\n' + grab('jsArg') + '\nglobalThis.__esc = esc; globalThis.__jsArg = jsArg;');
  // What a browser does: decode the attribute's entities, then run it as JS.
  const decode = a => a.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<')
                       .replace(/&gt;/g, '>').replace(/&amp;/g, '&');
  const got = [];
  globalThis.__f = (...a) => got.push(a);
  const hostile = ["x\\');alert(1);//", 'a"b', "O'Brien", '<script>alert(1)</script>',
                   '192.168.50.217_onvif_Prof\\ile"1', 'ch7 & more'];
  let ok = true, broke = false;
  globalThis.alert = () => { broke = true; };
  for (const h of hostile) {
    const attr = '__f(' + __jsArg('cam') + ',' + __jsArg(h) + ')';
    if (/["<>]/.test(attr)) ok = false;        // nothing that ends the attribute or opens a tag
    got.length = 0;
    try { vm.runInThisContext(decode(attr)); } catch (e) { ok = false; }
    if (!(got.length === 1 && got[0][0] === 'cam' && got[0][1] === h)) ok = false;
  }
  check('hostile names and IDs reach the handler unchanged', ok);
  check('no hostile value runs its own code', !broke);
  check("esc() also escapes single quotes", __esc("a'b") === 'a&#39;b');
  check('no card button pastes a camera value between quotes any more',
        !/\\'' \+ cam\./.test(page) && !page.includes("name.replace(/'/g"));
  check('classic info bar escapes the camera name', page.includes('const name   = esc(displayName(cam));'));
  check('IP now in the Identity table, escaped there',
        page.includes("rows.push(['IP address', cam.ip])") && page.includes("esc(v) + '</td></tr>'"));
}

// ── live cards (2.6.6, C1) ───────────────────────────────────────────────────
console.log('\n[S] live cards');
{
  g('_focusCamId = null');
  const snaps = [], focused = [], created = [];
  globalThis.CSS = {escape: s => s};
  globalThis.startSnap = id => snaps.push(id);
  globalThis.openFocus = id => focused.push(id);
  const cards = {};
  function drawCard(id) {   // what renderGrid does: a fresh img, placeholder and wrap
    const wrap = el('wrap-' + id);
    const img = el('img-' + id, {dataset: {snap: id}});
    wrap.appendChild(img);
    cards[id] = {wrap, img, ph: el('ph-' + id, {style: {display: 'flex'}})};
    return cards[id];
  }
  const origQS = document.querySelector, origGet = document.getElementById, origCreate = document.createElement;
  document.querySelector = sel => {
    const m = sel.match(/data-snap="([^"]+)"/); return m && cards[m[1]] ? cards[m[1]].img : null;
  };
  document.getElementById = id => (id.startsWith('ph-') && cards[id.slice(3)]) ? cards[id.slice(3)].ph : origGet(id);
  document.createElement = tag => {
    const e = origCreate(tag);
    if (tag === 'anycam-video') {
      Object.defineProperty(e, 'isConnected', {get() { return !!e.parentNode; }});
      e.style = {}; e.onconnect = () => false; e._dc = 0; e._cc = 0;
      e.disconnectedCallback = () => { e._dc++; }; e.connectedCallback = () => { e._cc++; };
      e.ondisconnect = () => { e._off = true; }; e.remove = () => { e.parentNode = null; e._removed = true; };
      created.push(e);
    }
    return e;
  };
  let cardInfo = {ok: true, stream: 'anycam_lorex_c0', codec: ''};
  const fetched = [];
  globalThis.fetch = async url => { fetched.push(url); return {json: async () => cardInfo}; };
  const cst = id => g('_cardLive')[id];

  drawCard('lorex');
  check('attach: card goes live, no snapshots', g("cardLiveAttach('lorex')") === true && snaps.length === 0);
  await flush(); await flush();
  const p = created[0];
  check('attach: asks the server which stream the card plays',
        fetched.some(u => u.endsWith('/api/go2rtc/card/lorex')));
  check('player placed in the card, hidden until it plays',
        p && p.parentNode === cards.lorex.wrap && p.style.opacity === '0');
  check('player pauses off screen (visibilityThreshold set)', p.visibilityThreshold === 0.01);
  check('player socket uses the card stream', p.wsURL && p.wsURL.endsWith('/go2rtc/ws?src=anycam_lorex_c0'));
  check('loading placeholder still shown while connecting', cards.lorex.ph.style.display === 'flex');
  g("_cardLiveEvent('lorex', _cardLive['lorex'], 'playing')");
  check('first frame: player shown, placeholder hidden', p.style.opacity === '1' && cards.lorex.ph.style.display === 'none');
  drawCard('lorex');   // renderGrid rewrote the card
  g("cardLiveAttach('lorex')");
  check('redraw: the same player moves into the new card, still shown',
        created.length === 1 && p.parentNode === cards.lorex.wrap && cards.lorex.ph.style.display === 'none');
  p.onclick();
  check('clicking the live card opens Enhanced View', focused[0] === 'lorex');
  g('cardLivePauseAll(true)'); g('cardLivePauseAll(false)');
  check('Enhanced View pauses, then resumes the card', p._dc === 1 && p._cc === 1);

  cardInfo = {ok: false, reason: 'no stream small enough for a card (2560 wide)'};
  drawCard('hik');
  g("cardLiveAttach('hik')"); await flush(); await flush();
  check('server says no: card uses snapshots', snaps.includes('hik') && !cst('hik'));
  snaps.length = 0; drawCard('hik');
  check('... and stays on snapshots for this page (no retry)', g("cardLiveAttach('hik')") === false);

  cardInfo = {ok: false, reason: 'go2rtc is not running', retry: true};
  drawCard('early');
  g("cardLiveAttach('early')"); await flush(); await flush();
  check('go2rtc still starting: snapshots now, live retried later (not remembered)',
        snaps.includes('early') && g("_cardLiveOff['early'].retryAt") > 0);

  cardInfo = {ok: true, stream: 'anycam_ff_c0'};
  drawCard('ffx');
  g("cardLiveAttach('ffx')"); await flush(); await flush();
  const sx = cst('ffx');
  g("_cardLiveEvent('ffx', _cardLive['ffx'], 'open', ['mse'])");
  g("_cardLiveEvent('ffx', _cardLive['ffx'], 'error', 'mse: codecs not matched: video:H265')");
  check('browser cannot play the codec: snapshots, remembered',
        snaps.includes('ffx') && !cst('ffx') && g("_cardLiveOff['ffx'].retryAt") === 0 && sx.el._removed);

  cardInfo = {ok: true, stream: 'anycam_slow_c0'};
  drawCard('slow');
  g("cardLiveAttach('slow')"); await flush(); await flush();
  const ss = cst('slow');
  advance(31000);
  check('off screen (not connected): no give-up at 31 s', cst('slow') === ss);
  ss.el.ws = {};   // now on screen and connected
  advance(31000);
  check('connected, no video in 30 s: snapshots', !cst('slow') && snaps.includes('slow'));
  check('... retried after 5 minutes, not remembered for good',
        g("_cardLiveOff['slow'].retryAt") > 0 && g("cardLiveAttach('slow')") === false);
  g("_cardLiveOff['slow'].retryAt = 1");
  check('after the retry time: live again', g("cardLiveAttach('slow')") === true);
  await flush(); await flush();

  delete cards.lorex;
  g('cardLivePrune()');
  check('card removed: its player is dropped', !cst('lorex') && p._removed);

  document.querySelector = origQS; document.getElementById = origGet; document.createElement = origCreate;
}

// ── classic engine: loading gate ─────────────────────────────────────────────
console.log('\n[Q] classic view loading gate');
const snap = {focusFrames: '0', status: 'ok', mode: 'rtsp', count: 1};
const posts = [];
Object.assign(globalThis, {
  cameras: [{id: 'cam2', name: 'Oak', status: 'ready', stream_codec: 'h264'}],
  CFG_ADAPTIVE_QUALITY: true,
  performance: {now: () => now},
  URL: {createObjectURL: () => 'blob:x', revokeObjectURL() {}},
  _snapErrors: {},
  fetch: async (url, opts) => {
    if (url.includes('/api/go2rtc/focus/')) return {json: async () => ({ok: false, reason: 'test: classic'})};
    if (url.includes('/snapshot/')) {
      const h = {'X-Frame-Count': String(snap.count), 'X-Focus-Frames': snap.focusFrames,
                 'X-Stream-Status': snap.status, 'X-Snap-Mode': snap.mode,
                 'X-Step-Res': '1280x720', 'X-Step-FPS': 'uncapped'};
      return {ok: true, status: 200, headers: {get: k => h[k] ?? null}, blob: async () => 'B'};
    }
    posts.push([url, opts && opts.method]);
    return {ok: true, json: async () => ({})};
  },
});
// The harness already declared _focusCamId for the first block.
vm.runInThisContext(focusBlock.replace(/^let _focusCamId\s*=\s*null;/m, '_focusCamId = null;'));
const img = DOM['focus-img'];
async function pump(n) { for (let i = 0; i < n; i++) { advance(60); await flush(); await flush(); } }
await g("openFocus('cam2')");
await pump(3);
check('classic: loading message shown while only the thumbnail exists', loading() === 'block');
check('classic: picture hidden meanwhile', img.style.visibility === 'hidden');
check('classic: server told to enter focus', posts.some(p => p[0].endsWith('/snap/focus/cam2') && p[1] === 'POST'));
snap.status = 'connecting'; snap.count = 2;
await pump(3);
check("classic: 'connecting' goes into the loading message",
      DOM['focus-loading-note'].textContent === 'Still connecting to the camera');
check('classic: no warning box over the loading message', DOM['focus-warning'].style.display !== 'flex');
snap.status = 'ok'; snap.focusFrames = '1'; snap.count = 3;
await pump(3);
check('classic: first frame of this session hides the loading message', loading() === 'none');
check('classic: picture shown', img.style.visibility === '');
check('classic: frame drawn', img.src === 'blob:x');
snap.focusFrames = '0'; snap.count = 4;
await pump(3);
check('classic: a later restart does not bring the loading message back', loading() === 'none');
snap.status = 'connecting';
await pump(2);
check("classic: after the first frame, 'connecting' uses the warning box as before",
      DOM['focus-warning'].style.display === 'flex');
await g('closeFocus()');
check('close: loading hidden and picture visibility reset',
      loading() === 'none' && img.style.visibility === '');
check('close: manual controls gone from the page code',
      ['focusPickRes', 'focusPickFps', 'focusResetAuto', '_loadFocusProfiles', '_applyFocusTier']
        .every(f => g('typeof ' + f) === 'undefined'));

// ── motion button state (2.6.5) ──────────────────────────────────────────────
console.log('\n[R] motion state mirrored from the server');
{
  const m0 = page.indexOf('/* ── Motion detection state');
  const m1 = page.indexOf('/* ── go2rtc live view (2.6.3, Tier 2)');
  const motionBlock = page.slice(m0, m1).replace(/^setInterval\(.*\);$/gm, '');   // top-level timers only
  const redrawn = [], sent = [];
  let server = {'ch7': {enabled: true, recording: false}};
  Object.assign(globalThis, {
    cameras: [{id: 'ch7', status: 'ready'}, {id: 'ch2', status: 'ready'}],
    updateCard: cam => redrawn.push(cam.id),
    fetch: async (url, opts) => {
      if (url.endsWith('/api/motion')) return {json: async () => server};
      sent.push([url, opts && JSON.parse(opts.body)]);
      return {json: async () => ({motion_enabled: true, recording: false})};
    },
  });
  vm.runInThisContext(motionBlock);
  await g('syncMotion(false)');
  check('page load: armed camera shows Armed (state read from the server)',
        g('_motionEnabled')['ch7'] === true && !g('_motionEnabled')['ch2']);
  check('page load: no redraw before the grid is drawn', redrawn.length === 0);
  await g("toggleMotion('ch2')");
  check('button asks for the opposite of what it shows',
        sent.length === 1 && sent[0][0].endsWith('/cameras/ch2/motion') && sent[0][1].enabled === true);
  server = {'ch7': {enabled: true, recording: true}};
  redrawn.length = 0;
  await g('syncMotion(true)');
  check('poll: recording starts -> card redrawn as REC', g('_recording')['ch7'] === true && redrawn.includes('ch7'));
  server = {};
  redrawn.length = 0;
  await g('syncMotion(true)');
  check('poll: disarmed on another device -> card redrawn', g('_motionEnabled')['ch7'] === false && redrawn.includes('ch7'));

  // 2.6.6 cog menu: live reading and read-only state
  const ids = ['cs-peak', 'cs-peak-mark', 'cs-level', 'cs-level-val', 'cs-cooldown', 'cs-tail',
               'cs-clip', 'cs-path', 'cs-save', 'cs-reset', 'cs-global', 'cs-mode', 'cs-night-note'];
  ids.forEach(id => { DOM[id] = el(id); });
  const peak = d => { g('_csPeak(' + JSON.stringify(d) + ')'); return DOM['cs-peak'].textContent; };
  check('cog: not armed -> asks to arm', peak({armed: false}).startsWith('Arm this camera'));
  check('cog: armed, nothing yet', peak({armed: true, peak_level: null}).startsWith('No movement'));
  check('cog: reading in slider units, marker placed',
        peak({armed: true, peak_level: 72}) === 'Biggest recent movement: records at 72 or higher.'
        && DOM['cs-peak-mark'].style.left === ((72 - 1) / 99 * 100) + '%' && DOM['cs-peak-mark'].style.display === '');
  check('cog: too small for any setting', peak({armed: true, peak_level: 101}).includes('too small'));
  // 2.6.7 night boost: the mode line and the missed-switch note
  peak({armed: true, peak_level: 95, night: true, night_boost: 15, night_note: null});
  check('cog: night -> mode line shows +15, highlighted, no note',
        DOM['cs-mode'].textContent === 'Night mode (IR): sensitivity +15'
        && DOM['cs-mode'].classList.contains('cs-night') && DOM['cs-night-note'].style.display === 'none');
  check('cog: night reading stays in slider units',
        DOM['cs-peak'].textContent === 'Biggest recent movement: records at 95 or higher.');
  peak({armed: true, peak_level: 60, night: false, night_boost: 15, night_note: 'No switch to night mode (IR) around sunset (19:02).'});
  check('cog: day -> plain mode line, missed-switch note shown',
        DOM['cs-mode'].textContent === 'Day mode: sensitivity as set'
        && !DOM['cs-mode'].classList.contains('cs-night')
        && DOM['cs-night-note'].style.display === '' && DOM['cs-night-note'].textContent.startsWith('No switch'));
  peak({armed: false, night: true, night_boost: 15});
  check('cog: not armed -> no mode line', DOM['cs-mode'].textContent === '');
  g("_csFill({settings:{level:40,cooldown:5,tail:3,clip:'30s',path:'/media/anycam'}, global:true, clip_choices:['30s']})");
  check('cog: global on -> every control disabled, notice shown',
        ['cs-level', 'cs-cooldown', 'cs-tail', 'cs-clip', 'cs-path', 'cs-save', 'cs-reset'].every(id => DOM[id].disabled)
        && DOM['cs-global'].style.display === '' && DOM['cs-level-val'].textContent === 40);
  g("_csFill({settings:{level:40,cooldown:5,tail:3,clip:'30s',path:'/media/anycam'}, global:false, clip_choices:['30s']})");
  check('cog: global off -> controls enabled', ['cs-level', 'cs-save'].every(id => !DOM[id].disabled)
        && DOM['cs-global'].style.display === 'none');
}

console.log(`\n${pass}/${pass + fail} passed`);
process.exit(fail ? 1 : 0);
