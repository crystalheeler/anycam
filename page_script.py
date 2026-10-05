"""The JavaScript of AnyCam's page.

One raw string, so braces, backslashes and quotes are all literal.
camera_discovery.build_html() fills the triple-underscore placeholders with
str.replace(). Moved out of camera_discovery.py in 3.0.0-rc1.0 (build
plan E1, stage 1); the script is unchanged.
"""

PAGE_SCRIPT = r"""
const BASE = '___BASE___';
const CFG_UNRESTRICTED_BROWSER = ___UNRESTRICTED___;
const CFG_ADAPTIVE_QUALITY     = ___ADAPTIVE_QUALITY___;
const STORAGE_UNRESTRICTED = ___UNRESTRICTED___;
const COG_SVG = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
  + ' stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/>'
  + '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33'
  + ' 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06'
  + 'a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09'
  + 'A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68'
  + ' 1.65 1.65 0 0 0 10 3.17V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06'
  + 'a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09'
  + 'a1.65 1.65 0 0 0-1.51 1z"/></svg>';
const INFO_SVG = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
  + ' stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9.5"/>'
  + '<line x1="12" y1="11" x2="12" y2="16.5"/><circle cx="12" cy="7.6" r="0.6" fill="currentColor"/></svg>';
const PROTO_ICONS = {RTSP:'📹',ONVIF:'🔭',MJPEG:'🖼️',HLS:'📡',RTMP:'📺',WebRTC:'🔗','WS-RTSP':'🔌',HTTP:'🌐',DVR:'💾'};
const PROTO_CLR   = {
  RTSP:['1e3a5f','79b8ff'],ONVIF:['2d1e4a','c09eff'],MJPEG:['1e3a30','79ffcd'],
  HLS:['3a2e1e','ffb879'],RTMP:['3a1e1e','ff7979'],WebRTC:['1e2d3a','79d4ff'],
  'WS-RTSP':['2d3a1e','b8ff79'],HTTP:['2a2a2a','aaaaaa'],DVR:['3a1e3a','ff79ff']
};
const DPORT = {RTSP:554,ONVIF:80,MJPEG:80,HLS:80,RTMP:1935,WebRTC:443,'WS-RTSP':8554};

let cameras=[], pollT=null, pscanT=null, renameId=null, _paused=false;

/* ── View switching ────────────────────────────────────────────────────────── */
function switchView(v) {
  document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
  const viewMap = {cameras: 'cameras-view', pscan: 'pscan-view', add: 'add-view', storage: 'storage-view'};
  document.getElementById(viewMap[v] || 'cameras-view').classList.add('active');
  document.getElementById('pscan-btn').classList.toggle('active', v === 'pscan');
  document.getElementById('add-btn').classList.toggle('active', v === 'add');
  document.getElementById('storage-btn').classList.toggle('active', v === 'storage');
  if (v === 'storage') loadStorage();
  if (v === 'pscan') {
    loadArpHosts();
    fetch(BASE + '/api/pscan/status').then(r => r.json()).then(s => {
      if (s.running) {
        document.getElementById('ps-start').disabled = true;
        document.getElementById('ps-pause').style.display = '';
        document.getElementById('ps-cancel').style.display = '';
        document.getElementById('ps-prog-track').style.display = '';
        _paused = s.paused || false;
        document.getElementById('ps-pause').textContent = _paused ? '▶ Resume' : '⏸ Pause';
        pollPscan();
      } else if (s.results && s.results.length) {
        // Restore completed results
        renderPorts(s.results);
        const msgEl = document.getElementById('pscan-msg');
        if (s.message) msgEl.textContent = s.message;
      }
    });
  }
}

/* ── Camera scan ───────────────────────────────────────────────────────────── */
async function startScan() {
  if (!document.getElementById('cameras-view').classList.contains('active'))
    switchView('cameras');
  const broad = document.getElementById('broad-sweep').checked;
  const r = await fetch(BASE + '/api/scan', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({broad_sweep: broad})
  });
  if (!r.ok) { const d = await r.json(); alert('Scan error: ' + (d.error || r.status)); return; }
  document.getElementById('scan-btn').disabled = true;
  document.getElementById('progress-track').style.display = '';
  document.getElementById('status-bar').classList.add('scanning');
  pollScan();
}

async function pollScan() {
  clearTimeout(pollT);
  try {
    const s = await (await fetch(BASE + '/api/scan/status')).json();
    document.getElementById('status-msg').textContent = s.message;
    document.getElementById('progress-fill').style.width = s.progress + '%';
    const badge = document.getElementById('stage-badge');
    if (s.stage && s.stage > 0) {
      badge.textContent = s.stage_label || ('Stage ' + s.stage);
      badge.style.display = '';
    } else {
      badge.style.display = 'none';
    }
    // ETA countdown — server provides eta in seconds remaining
    // We adjust client-side for time since last poll to keep it smooth
    const timerEl = document.getElementById('scan-timer');
    if (s.running) {
      const serverEta    = s.eta || 0;
      const serverElap   = s.elapsed || 0;
      const clientElap   = s.started_at ? (Date.now()/1000) - s.started_at : serverElap;
      const secondsGone  = Math.max(0, clientElap - serverElap);
      const etaAdj       = Math.max(0, Math.round(serverEta - secondsGone));
      const etaM = Math.floor(etaAdj / 60), etaSec = etaAdj % 60;
      timerEl.textContent = 'estimated ' + etaM + ':' + String(etaSec).padStart(2, '0') + ' remaining';
      timerEl.style.display = '';
    } else if (s.elapsed) {
      const m = Math.floor(s.elapsed / 60), sec = Math.round(s.elapsed) % 60;
      timerEl.textContent = 'Completed in ' + m + ':' + String(sec).padStart(2, '0');
      timerEl.style.display = '';
    } else {
      timerEl.style.display = 'none';
    }
    _updateCancelBtn(s.running);
    if (s.running) {
      pollT = setTimeout(pollScan, 1500);
    } else {
      document.getElementById('scan-btn').disabled = false;
      document.getElementById('status-bar').classList.remove('scanning');
      document.getElementById('progress-track').style.display = 'none';
      badge.style.display = 'none';
      if (s.progress >= 100) await loadCameras();
    }
  } catch(e) {
    pollT = setTimeout(pollScan, 3000);
  }
}

async function loadCameras() {
  try {
    cameras = await (await fetch(BASE + '/api/cameras')).json();
    await syncMotion(false);   // 2.6.5: cards start with the server's motion state
    renderGrid();
  } catch(e) {
    console.error('loadCameras error:', e);
  }
}

/* ── Snapshot polling ──────────────────────────────────────────────────────── */
/* Instead of one long-lived multipart/x-mixed-replace stream (which HA's
   nginx ingress terminates after a short time), we poll /snapshot/{id}?t=...
   every 125ms.  Each request is a normal fast HTTP round-trip.
   Debug info is logged to the browser console (open DevTools → Console). */
const _snapTimers  = {};   // camId → setTimeout handle
const _snapErrors  = {};   // camId → consecutive error count
const _snapErrSince = {};  // camId → time of the first error in the run
// 2.6.5 (B14): the server answers 503 while a camera's stream is still
// starting, so errors alone do not mean the stream failed. The H.264 camera
// with keyframes far apart took up to 28 s to its first frame (2026-09-29 log),
// and one 30 s read timeout plus a retry takes about 60 s. Say "Stream
// unavailable" only after 90 s of errors.
const SNAP_UNAVAILABLE_MS = 90000;

function startSnap(camId) {
  stopSnap(camId);
  _snapErrors[camId] = 0;
  const poll = () => {
    const display = document.querySelector('[data-snap="' + camId + '"]');
    if (!display) { stopSnap(camId); return; }   // card was removed
    const loader = new Image();
    loader.onload = () => {
      _snapErrors[camId] = 0;
      delete _snapErrSince[camId];
      // Show img, hide placeholder
      display.src         = loader.src;
      display.style.display = '';
      const ph = document.getElementById('ph-' + camId);
      if (ph) ph.style.display = 'none';
      // Next poll: 125ms (~8fps) matches server vf=fps=8
      _snapTimers[camId] = setTimeout(poll, 125);
    };
    loader.onerror = () => {
      _snapErrors[camId] = (_snapErrors[camId] || 0) + 1;
      const errs = _snapErrors[camId];
      console.warn('[AnyCam] snapshot error #' + errs + ' for ' + camId);
      if (!_snapErrSince[camId]) _snapErrSince[camId] = Date.now();
      const failed = Date.now() - _snapErrSince[camId] >= SNAP_UNAVAILABLE_MS;
      if (errs >= 3) {
        // After 3 consecutive errors, show the placeholder
        display.style.display = 'none';
        const ph = document.getElementById('ph-' + camId);
        if (ph) {
          ph.querySelector('span').textContent =
            failed ? 'Stream unavailable' : 'Loading feed, please wait…';
          ph.style.display = 'flex';
        }
      }
      // Back off: 500ms for first few errors, 2s after 5 errors
      _snapTimers[camId] = setTimeout(poll, errs > 5 ? 2000 : 500);
    };
    loader.src = BASE + '/snapshot/' + camId + '?t=' + Date.now();
  };
  poll();   // start immediately
}

function stopSnap(camId) {
  if (_snapTimers[camId]) { clearTimeout(_snapTimers[camId]); delete _snapTimers[camId]; }
}

/* ── Scan cancel ───────────────────────────────────────────────────────────── */
async function cancelScan() {
  await fetch(BASE + '/api/scan/cancel', {method: 'POST'}).catch(() => {});
}


/* ── Open HA addon log page ─────────────────────────────────────────────────── */
async function openHALog() {
  // Navigate the top-level HA window (not this ingress iframe) to the addon log page.
  // 3.0.1 (B26): the add-on's ID differs between a copy in /addons and a copy
  // from an add-on store, so ask the add-on for it.
  let slug = 'local_camera_discovery';
  try {
    const r = await (await fetch(BASE + '/api/self')).json();
    if (r && r.slug) slug = r.slug;
  } catch (e) {}
  window.top.location.href = window.top.location.origin + '/config/app/' + encodeURIComponent(slug) + '/logs';
}

/* ── Camera page opener (Firefox addon or direct) ────────────────────────────── */
// Firefox addon slug — if installed, its ingress is at /api/hassio_ingress/...
// We detect it by trying the supervisor addon info endpoint via a known pattern.
// Since we don't have hassio_api, we probe the Firefox ingress panel URL.
const FIREFOX_SLUG = 'firefox';
let _firefoxIngress = null;   // cached ingress path once found

async function _detectFirefox() {
  // Try the HA frontend addon page to see if firefox panel exists
  try {
    const r = await fetch(window.location.origin + '/hassio/addon/' + FIREFOX_SLUG, {
      method: 'GET', redirect: 'follow',
    });
    // If we get a 200 (the HA page rendered), Firefox is installed
    return r.ok || r.status === 200;
  } catch(e) {
    return false;
  }
}

async function openCameraPage(ip) {
  const cameraUrl = 'http://' + ip;
  // Always show the modal with options
  _showBrowserModal(ip, cameraUrl);
}

async function _showBrowserModal(ip, cameraUrl) {
  document.getElementById('browser-modal')?.remove();
  const firefoxInstalled = await _detectFirefox();
  const modal = document.createElement('div');
  modal.id = 'browser-modal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9500;display:flex;align-items:center;justify-content:center';
  const ffLabel = firefoxInstalled ? '🦊 Open in Firefox' : '🦊 Get Firefox for HA';
  const inner = document.createElement('div');
  inner.style.cssText = 'background:var(--card-bg);border:1px solid var(--border);border-radius:12px;padding:28px 32px;max-width:420px;width:90%;text-align:center';
  inner.innerHTML = '<div style="font-size:1.5rem;margin-bottom:10px">🌐</div>'
    + '<div style="font-weight:600;margin-bottom:6px">Open Camera Page</div>'
    + '<div style="font-size:.82rem;color:var(--text-dim);margin-bottom:20px">' + esc(cameraUrl) + '</div>'
    + '<div style="display:flex;flex-direction:column;gap:10px">'
    + '<a class="btn btn-primary" id="bmNewTab" href="' + esc(cameraUrl) + '" target="_blank" rel="noopener">↗ Open in New Tab</a>'
    + '<button class="btn btn-secondary" id="bmFirefox">' + ffLabel + '</button>'
    + '<button class="btn btn-ghost" id="bmCancel">Cancel</button>'
    + '</div>';
  modal.appendChild(inner);
  document.body.appendChild(modal);
  const close = () => modal.remove();
  modal.querySelector('#bmNewTab').addEventListener('click', close);
  modal.querySelector('#bmCancel').addEventListener('click', close);
  modal.querySelector('#bmFirefox').addEventListener('click', () => {
    close();
    if (firefoxInstalled) _openInFirefox(ip);
    else _promptGetFirefox();
  });
  modal.addEventListener('click', e => { if (e.target === modal) close(); });
}

function _openInFirefox(ip) {
  // Navigate to the Firefox HA addon panel — user can then type the camera IP
  // (we cannot inject a URL into Firefox via ingress directly)
  const ffUrl = window.location.origin + '/hassio/ingress/' + FIREFOX_SLUG;
  const win = window.open(ffUrl, '_blank');
  // Show a toast telling the user to navigate to the IP
  setTimeout(() => showToast('Firefox opened — navigate to http://' + ip), 600);
}

function _promptGetFirefox() {
  document.getElementById('browser-modal')?.remove();
  const modal = document.createElement('div');
  modal.id = 'browser-modal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9500;display:flex;align-items:center;justify-content:center';
  const inner = document.createElement('div');
  inner.style.cssText = 'background:var(--card-bg);border:1px solid var(--border);border-radius:12px;padding:28px 32px;max-width:420px;width:90%;text-align:center';
  inner.innerHTML = '<div style="font-size:1.5rem;margin-bottom:10px">🦊</div>'
    + '<div style="font-weight:600;margin-bottom:8px">Firefox not installed</div>'
    + '<div style="font-size:.82rem;color:var(--text-dim);margin-bottom:20px">'
    + 'The Firefox add-on lets you browse camera pages inside Home Assistant. '
    + 'Add the repository from mincka/ha-addons and install Firefox.</div>'
    + '<div style="display:flex;gap:10px;justify-content:center">'
    + '<a class="btn btn-primary" id="ffGetIt" href="' + window.location.origin + '/hassio/store" target="_blank">Go Get It</a>'
    + '<button class="btn btn-ghost" id="ffCancel">Cancel</button>'
    + '</div>';
  modal.appendChild(inner);
  document.body.appendChild(modal);
  const close = () => modal.remove();
  modal.querySelector('#ffGetIt').addEventListener('click', close);
  modal.querySelector('#ffCancel').addEventListener('click', close);
  modal.addEventListener('click', e => { if (e.target === modal) close(); });
}



/* ── Status dot ────────────────────────────────────────────────────────────── */
let _lastLogTime = 0;
async function pollStatusDot() {
  try {
    const d   = await (await fetch(BASE + '/api/logs?since=' + _lastLogTime)).json();
    const icon = document.getElementById('status-cam-icon');
    if (!icon) return;
    if (d.status === 'error') {
      icon.style.stroke = '#e53935';   // red — errors
    } else if (d.status === 'warning') {
      icon.style.stroke = '#f9c700';   // yellow — warnings
    } else {
      icon.style.stroke = '#43a047';   // green — all clear
    }
    if (d.entries && d.entries.length) {
      _lastLogTime = d.entries[d.entries.length - 1].t;
    }
  } catch(e) {}
}
setInterval(pollStatusDot, 5000);
pollStatusDot();

/* ── Cancel button visibility ─────────────────────────────────────────────── */
/* Show the cancel button while scan is running */
const _origPollScan = typeof pollScan !== 'undefined' ? pollScan : null;
function _updateCancelBtn(running) {
  const btn = document.getElementById('scan-cancel-btn');
  if (btn) btn.style.display = running ? '' : 'none';
  _setEmptyText(running);
}

// 3.0.0-rc1.4 (B21): while a scan runs, the empty page says "No Cameras
// Found Yet". "Click Scan Network" shows only when no scan is running, for
// example after every camera is removed.
function _setEmptyText(scanning) {
  const idle = document.getElementById('empty-idle');
  const busy = document.getElementById('empty-scanning');
  if (idle) idle.style.display = scanning ? 'none' : '';
  if (busy) busy.style.display = scanning ? '' : 'none';
}

/* ── Log view ──────────────────────────────────────────────────────────────── */

/* ── Storage navigation state ─────────────────────────────────────────────── */
let _storHistory  = [null];   // null = root, string = folder name
let _storHistIdx  = 0;
let _storCurrent  = null;     // null = root view, string = folder name

function _storNavTo(folder) {
  // Trim forward history when navigating to a new place
  _storHistory = _storHistory.slice(0, _storHistIdx + 1);
  _storHistory.push(folder);
  _storHistIdx = _storHistory.length - 1;
  _storCurrent = folder;
  _renderStorageView();
}

function storNavBack() {
  if (_storHistIdx <= 0) return;
  _storHistIdx--;
  _storCurrent = _storHistory[_storHistIdx];
  _renderStorageView();
}

function storNavUp() {
  if (_storCurrent === null) return;   // already at root (ceiling)
  if (STORAGE_UNRESTRICTED) {
    // In unrestricted mode _storCurrent is a full path string
    const parts = _storCurrent.replace(/\/+$/, '').split('/');
    parts.pop();
    const parent = parts.join('/') || '/';
    _storNavTo(parent === '/' ? null : parent);
  } else {
    // Scoped mode: two levels max (null = /media/anycam root, string = subfolder)
    // Any subfolder is one level below root, so up always goes to root
    _storNavTo(null);
  }
}

/* Sort state */
let _storSortKey = 'name', _storSortAsc = true;

function storSort(key) {
  if (_storSortKey === key) _storSortAsc = !_storSortAsc;
  else { _storSortKey = key; _storSortAsc = key === 'name'; }
  // Update header arrows
  ['name','date','type','size'].forEach(k => {
    const el = document.getElementById('sorth-' + k);
    if (!el) return;
    const base = k.charAt(0).toUpperCase() + k.slice(1);
    const labels = {name:'Name',date:'Date Modified',type:'Type',size:'Size'};
    el.innerHTML = labels[k] + (k === _storSortKey ? (' ' + (_storSortAsc ? '&#x25B2;' : '&#x25BC;')) : '');
  });
  _renderStorageView();
}

function _updateNavButtons() {
  const back    = document.getElementById('stor-back-btn');
  const up      = document.getElementById('stor-up-btn');
  const sep     = document.getElementById('stor-path-sep');
  const folderEl= document.getElementById('stor-path-folder');
  if (!back) return;
  back.disabled = _storHistIdx <= 0;
  up.disabled   = _storCurrent === null;
  if (sep && folderEl) {
    if (_storCurrent === null) {
      sep.style.display    = 'none';
      folderEl.textContent = '';
    } else {
      sep.style.display    = 'inline';
      // In unrestricted mode show only the last path component
      folderEl.textContent = STORAGE_UNRESTRICTED
        ? (_storCurrent.replace(/\/+$/, '').split('/').filter(Boolean).pop() || '/')
        : _storCurrent;
    }
  }
}

/* ── Editable path bar ────────────────────────────────────────────────────── */
function _pathBarEdit() {
  const bc = document.getElementById('stor-breadcrumb');
  if (!bc || bc.querySelector('#stor-path-input')) return;  // already editing
  // Compute the current full display path
  const root = STORAGE_UNRESTRICTED ? '/' : '/media/anycam';
  let fullPath = root;
  if (_storCurrent !== null) {
    fullPath = STORAGE_UNRESTRICTED ? _storCurrent : (root + '/' + _storCurrent);
  }
  // Replace breadcrumb contents with an input
  bc.innerHTML = '';
  const inp = document.createElement('input');
  inp.id = 'stor-path-input';
  inp.value = fullPath;
  inp.style.cssText = 'width:100%;border:none;outline:none;font-size:.82rem;color:#111;background:transparent;padding:0';
  bc.appendChild(inp);
  inp.focus();
  // Place cursor at end
  inp.setSelectionRange(inp.value.length, inp.value.length);

  function _commit() {
    const val = inp.value.trim().replace(/\/+$/, '') || '/';
    _pathBarCommit(val);
  }
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); _commit(); }
    if (e.key === 'Escape') { _renderBreadcrumb(); }
  });
  inp.addEventListener('blur', _commit);
}

function _pathBarCommit(path) {
  // Validate the path against known data
  if (!_storageData) { _renderBreadcrumb(); return; }
  const root = STORAGE_UNRESTRICTED ? '/' : '/media/anycam';
  const rootNorm = root.replace(/\/+$/, '');
  const pathNorm = path.replace(/\/+$/, '') || '/';

  if (pathNorm === rootNorm || pathNorm === '/media/anycam' || pathNorm === '/') {
    // Navigating to the root ceiling
    _storNavTo(null);
    return;
  }

  if (STORAGE_UNRESTRICTED) {
    // Accept any path string in unrestricted mode — server will validate
    _storNavTo(pathNorm);
    return;
  }

  // Scoped mode: path must be /media/anycam or /media/anycam/<folder>
  if (!pathNorm.startsWith(rootNorm + '/')) {
    alert('Folder not found: ' + path + '\n\nBrowser is scoped to ' + root);
    _renderBreadcrumb();
    return;
  }
  const sub = pathNorm.slice(rootNorm.length + 1);
  const folders = (_storageData.folders || []).map(f => f.folder || f.name);
  if (!folders.includes(sub)) {
    alert('Folder not found: ' + path);
    _renderBreadcrumb();
    return;
  }
  _storNavTo(sub);
}

function _renderBreadcrumb() {
  const bc = document.getElementById('stor-breadcrumb');
  if (!bc) return;
  const rootLabel = STORAGE_UNRESTRICTED ? '/' : '/media/anycam';
  bc.innerHTML =
    '<span id="stor-path-root" onclick="_storNavTo(null)"'
    + ' style="cursor:pointer;padding:2px 6px;border-radius:3px;color:#0066cc"'
    + ' title="' + rootLabel + '">' + rootLabel + '</span>'
    + '<span id="stor-path-sep" style="display:none;color:#888;padding:0 2px">&rsaquo;</span>'
    + '<span id="stor-path-folder" style="font-weight:600;color:#111;padding:2px 4px"></span>';
  // Re-apply click-to-edit on the whole bar
  bc.onclick = e => { if (!e.target.closest('#stor-path-root')) _pathBarEdit(); };
  _updateNavButtons();
}


function renderStorage(d) {
  const disk = d.disk || {};
  const pct  = disk.pct_used || 0;
  const lbl  = document.getElementById('disk-label');
  const fill = document.getElementById('disk-bar-fill');
  if (lbl)  lbl.textContent = pct + '% used — ' + (disk.free_gb || 0) + ' GB free of ' + (disk.total_gb || 0) + ' GB';
  if (fill) {
    fill.style.width      = Math.min(pct, 100) + '%';
    fill.style.background = pct > 90 ? 'var(--red)' : pct > 70 ? 'var(--orange)' : 'var(--primary)';
  }
  _storHistory = [null]; _storHistIdx = 0; _storCurrent = null;
  _renderBreadcrumb();
  _renderStorageView();
}

function _sortRows(rows) {
  return rows.slice().sort((a, b) => {
    let av, bv;
    if (_storSortKey === 'name')      { av = a.name.toLowerCase(); bv = b.name.toLowerCase(); }
    else if (_storSortKey === 'date') { av = a.mtime || 0;         bv = b.mtime || 0; }
    else if (_storSortKey === 'size') { av = a.size_mb || 0;        bv = b.size_mb || 0; }
    else if (_storSortKey === 'type') { av = a._type || '';         bv = b._type || ''; }
    else { av = ''; bv = ''; }
    if (av < bv) return _storSortAsc ? -1 :  1;
    if (av > bv) return _storSortAsc ?  1 : -1;
    return 0;
  });
}

function _renderStorageView() {
  // Refresh breadcrumb text (don't interrupt if user is actively editing)
  if (!document.getElementById('stor-path-input')) _renderBreadcrumb();
  _updateNavButtons();
  const list = document.getElementById('storage-list');
  if (!list || !_storageData) return;
  const folders = _storageData.folders || [];

  if (_storCurrent === null) {
    // Root: show all camera folders as rows
    if (!folders.length) {
      list.innerHTML = '<div class="stor-empty-msg">📁<br><br>No recordings yet.<br>'
        + 'Enable motion detection on a camera card to start recording.</div>';
      return;
    }
    // Annotate for sorting
    const rows = folders.map(f => ({
      name: f.folder, _type: 'Folder', size_mb: f.size_mb, mtime: 0,
      count: f.count, _isFolder: true
    }));
    const folderFrag = document.createDocumentFragment();
    _sortRows(rows).forEach(f => {
      const row = document.createElement('div');
      row.className = 'stor-row stor-row-folder';
      row.innerHTML = '<div class="stor-row-name">'
        + '<span style="font-size:1rem;flex-shrink:0;margin-right:4px">📁</span>'
        + '<span class="stor-row-name-text editable">' + esc(f.name) + '</span></div>'
        + '<div class="stor-row-date"></div>'
        + '<div class="stor-row-type">File folder</div>'
        + '<div class="stor-row-size"></div>'
        + '<div class="stor-row-acts"><span style="font-size:.72rem;color:#888">'
        + f.count + ' clip' + (f.count !== 1 ? 's' : '') + '</span></div>';
      // Single-click anywhere on row → navigate into folder
      // Double-click on name → rename
      let _folderClickTimer = null;
      row.addEventListener('click', e => {
        if (e.detail === 2) return;   // let dblclick handle it
        clearTimeout(_folderClickTimer);
        _folderClickTimer = setTimeout(() => _storNavTo(f.name), 200);
      });
      row.querySelector('.stor-row-name-text').addEventListener('dblclick', e => {
        clearTimeout(_folderClickTimer);
        storRenameFolder(f.name);
      });
      row.style.cursor = 'pointer';
      row.addEventListener('dragover', e => e.preventDefault());
      row.addEventListener('drop',     e => storDrop(e, f.name));
      folderFrag.appendChild(row);
    });
    list.innerHTML = '';
    list.appendChild(folderFrag);
  } else {
    // Folder view: show files
    const folder = folders.find(f => f.folder === _storCurrent);
    const files  = folder ? folder.files : [];
    if (!files.length) {
      list.innerHTML = '<div class="stor-empty-msg">🎬<br><br>No recordings in this folder.</div>';
      return;
    }
    const rows = files.map(f => ({
      ...f, _type: f.name.endsWith('.mp4') ? 'MP4 Video' : f.name.endsWith('.mkv') ? 'MKV Video' : 'File',
      _isFolder: false
    }));
    const fileFrag = document.createDocumentFragment();
    _sortRows(rows).forEach(f => {
      const path = _storCurrent + '/' + f.name;
      const dt   = f.mtime ? new Date(f.mtime * 1000).toLocaleString() : '';
      const sz   = f.size_mb ? f.size_mb + ' MB' : '';
      const row  = document.createElement('div');
      row.className = 'stor-row';
      row.draggable = true;
      row.innerHTML = '<div class="stor-row-name"><span>🎬</span>'
        + '<span class="stor-row-name-text editable">' + esc(f.name) + '</span></div>'
        + '<div class="stor-row-date">' + dt + '</div>'
        + '<div class="stor-row-type">' + esc(f._type) + '</div>'
        + '<div class="stor-row-size">' + sz + '</div>'
        + '<div class="stor-row-acts">'
        + '<a class="stor-dl" href="' + BASE + '/api/storage/download?path='
        + encodeURIComponent(path) + '" download title="Download" style="font-size:.9rem;text-decoration:none">⬇</a>'
        + '&nbsp;<span class="stor-del" title="Delete" style="cursor:pointer;font-size:.9rem;color:#c00">🗑</span>'
        + '</div>';
      // Single-click on name → download; double-click → rename
      const fileNameEl = row.querySelector('.stor-row-name-text');
      let _fileClickTimer = null;
      fileNameEl.addEventListener('click', e => {
        if (e.detail === 2) return;
        clearTimeout(_fileClickTimer);
        _fileClickTimer = setTimeout(() => {
          const a = document.createElement('a');
          a.href = BASE + '/api/storage/download?path=' + encodeURIComponent(path);
          a.download = f.name;
          a.click();
        }, 200);
      });
      fileNameEl.addEventListener('dblclick', e => {
        clearTimeout(_fileClickTimer);
        storRenameFile(path, f.name);
      });
      fileNameEl.style.cursor = 'pointer';
      row.querySelector('.stor-del').addEventListener('click', () => storDeleteFile(path));
      row.addEventListener('dragstart', e => storDragStart(e, path, _storCurrent));
      fileFrag.appendChild(row);
    });
    list.innerHTML = '';
    list.appendChild(fileFrag);
  }
}

/* Called after renderGrid() to start polling for all visible snap cameras */
function initSnaps() {
  document.querySelectorAll('[data-snap]').forEach(img => {
    // Click-to-focus: open enhanced view on click
    img.style.cursor = 'pointer';
    img.onclick = () => openFocus(img.dataset.snap);
    // 2.6.6: live first; snapshots when the card cannot play live.
    if (!cardLiveAttach(img.dataset.snap)) startSnap(img.dataset.snap);
  });
}

/* ── Motion detection state (mirrored from server) ──────────────────────── */
const _motionEnabled = {};   // camId → bool
const _recording     = {};   // camId → bool

async function toggleMotion(camId) {
  try {
    // 2.6.5: ask for the opposite of what the button shows, not a blind flip.
    const r = await fetch(BASE + '/api/cameras/' + camId + '/motion', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: !_motionEnabled[camId]}),
    });
    const d = await r.json();
    _motionEnabled[camId] = d.motion_enabled;
    _recording[camId]     = d.recording || false;
    // Rebuild just this card
    const cam = cameras.find(c => c.id === camId);
    if (cam) updateCard(cam);
  } catch(e) { console.error('toggleMotion error:', e); }
}

/* Mirror the server's motion state for every camera: at page load and every
 * 3 s. 2.6.5: one request for all cameras. Before, the page asked only about
 * cameras it already believed armed, so after a page load it never learned
 * that a camera was armed, and another device's change never showed. */
async function syncMotion(redraw) {
  try {
    const all = await (await fetch(BASE + '/api/motion')).json();
    cameras.forEach(cam => {
      const m  = all[cam.id] || {};
      const on = !!m.enabled, rec = !!m.recording;
      if (on !== !!_motionEnabled[cam.id] || rec !== !!_recording[cam.id]) {
        _motionEnabled[cam.id] = on;
        _recording[cam.id]     = rec;
        if (redraw) updateCard(cam);
      }
    });
  } catch(e) {}
}
setInterval(() => syncMotion(true), 3000);

/* ── Camera settings: the cog menu (2.6.6) ───────────────────────────────
 * One camera's motion and recording settings. The sensitivity slider runs
 * from 1 (left, least sensitive) to 100 (right, most sensitive) and shows
 * its value as it moves. Under it, a live reading: the lowest setting that
 * would have recorded the biggest movement of the last minute or two, also
 * marked on the slider. Read-only while the Configuration tab's global
 * recording settings are on. */
let _csCamId = null;
let _csTimer = null;

async function openCamSettings(camId) {
  const cam = cameras.find(c => c.id === camId);
  _csCamId = camId;
  document.getElementById('cs-title').textContent = 'Settings — ' + (cam ? displayName(cam) : camId);
  document.getElementById('cs-error').textContent = '';
  document.getElementById('cam-settings-modal').classList.add('open');
  await _csLoad(true);
  clearInterval(_csTimer);
  _csTimer = setInterval(() => _csLoad(false), 3000);
}

function closeCamSettings() {
  clearInterval(_csTimer);
  _csTimer = null;
  _csCamId = null;
  document.getElementById('cam-settings-modal').classList.remove('open');
}

// fill: true sets every field; false refreshes only the live reading, so
// a poll never overwrites what the user is typing.
async function _csLoad(fill) {
  const camId = _csCamId;
  if (!camId) return;
  let d;
  try {
    d = await (await fetch(BASE + '/api/cameras/' + encodeURIComponent(camId) + '/motion/settings')).json();
  } catch (e) { return; }
  if (camId !== _csCamId || !d || !d.settings) return;
  if (fill) _csFill(d);
  _csPeak(d);
}

function _csFill(d) {
  const st = d.settings;
  const sel = document.getElementById('cs-clip');
  sel.innerHTML = d.clip_choices.map(c => '<option value="' + esc(c) + '">' + esc(c) + '</option>').join('');
  document.getElementById('cs-level').value    = st.level;
  document.getElementById('cs-cooldown').value = st.cooldown;
  document.getElementById('cs-tail').value     = st.tail;
  sel.value                                    = st.clip;
  document.getElementById('cs-path').value     = st.path;
  csLevelShow();
  const off = !!d.global;
  ['cs-level', 'cs-cooldown', 'cs-tail', 'cs-clip', 'cs-path', 'cs-save', 'cs-reset']
    .forEach(id => { document.getElementById(id).disabled = off; });
  document.getElementById('cs-global').style.display = off ? '' : 'none';
}

function csLevelShow() {
  document.getElementById('cs-level-val').textContent = document.getElementById('cs-level').value;
}

function _csPeak(d) {
  const txt  = document.getElementById('cs-peak');
  const mark = document.getElementById('cs-peak-mark');
  const mode = document.getElementById('cs-mode');
  // 2.6.7: night boost. The slider keeps what the user set; at night the
  // camera works at that setting plus the boost.
  mode.textContent = !d.armed ? '' : d.night
    ? 'Night mode (IR): sensitivity +' + d.night_boost
    : 'Day mode: sensitivity as set';
  mode.classList.toggle('cs-night', !!d.night);
  const note = document.getElementById('cs-night-note');
  note.textContent = d.night_note || '';
  note.style.display = d.night_note ? '' : 'none';
  mark.style.display = 'none';
  if (!d.armed) {
    txt.textContent = 'Arm this camera (Record button) to see how strongly movement registers.';
  } else if (d.peak_level === null || d.peak_level === undefined) {
    txt.textContent = 'No movement seen yet. Walk past the camera to test.';
  } else if (d.peak_level > 100) {
    txt.textContent = 'Recent movement was too small to record, even at 100.';
  } else {
    txt.textContent = 'Biggest recent movement: records at ' + d.peak_level + ' or higher.';
    mark.style.left = ((d.peak_level - 1) / 99 * 100) + '%';
    mark.style.display = '';
  }
}

async function saveCamSettings() {
  const camId = _csCamId;
  if (!camId) return;
  const body = {
    level:    parseInt(document.getElementById('cs-level').value, 10),
    cooldown: parseInt(document.getElementById('cs-cooldown').value, 10),
    tail:     parseInt(document.getElementById('cs-tail').value, 10),
    clip:     document.getElementById('cs-clip').value,
    path:     document.getElementById('cs-path').value.trim(),
  };
  const err = document.getElementById('cs-error');
  try {
    const r = await fetch(BASE + '/api/cameras/' + encodeURIComponent(camId) + '/motion/settings',
                          {method: 'POST', headers: {'Content-Type': 'application/json'},
                           body: JSON.stringify(body)});
    const d = await r.json();
    if (!r.ok) { err.textContent = d.error || 'Could not save'; return; }
  } catch (e) { err.textContent = 'Could not save: ' + e; return; }
  closeCamSettings();
  showToast('Settings saved');
}

async function resetCamSettings() {
  const camId = _csCamId;
  if (!camId) return;
  try {
    const r = await fetch(BASE + '/api/cameras/' + encodeURIComponent(camId) + '/motion/settings',
                          {method: 'DELETE'});
    const d = await r.json();
    if (!r.ok) { document.getElementById('cs-error').textContent = d.error || 'Could not reset'; return; }
    _csFill(d);
    _csPeak(d);
  } catch (e) {}
}

/* ── go2rtc live view (2.6.3, Tier 2) ──────────────────────────────────────
 * Enhanced View plays through the bundled
 * go2rtc: WebRTC or MSE, passed through with no decode on the Pi. The
 * classic JPEG path (_startFocusPoll) is untouched and is the fallback.
 *
 * AnyCamVideo subclasses go2rtc's VideoRTC, the extension pattern go2rtc
 * documents in www/video-stream.js; www/video-rtc.js itself is vendored
 * unmodified. VideoRTC runs MSE and WebRTC in parallel, plays MSE first,
 * and switches to WebRTC only when WebRTC wins its codec comparison.
 *
 * VideoRTC never gives up: on a dead socket it reconnects every 15 s,
 * forever. That is right once a stream has played. Before that it is
 * wrong — an unplayable codec, or a proxy that kills the socket, would
 * look exactly like the freeze-and-resume symptom this work exists to
 * remove. So until the first frame this code decides, and falls back to
 * the classic view after GO2RTC_FIRST_FRAME_MS with no video, after two
 * unexpected socket closes, or once every attempted mode has errored.
 * After the first frame it never falls back on its own.
 *
 * 2.6.5: 30 s, up from 12 s. A player cannot draw until the stream's first
 * keyframe. An H.264 camera with keyframes far apart took 19 to 28 s to its
 * first frame on every connection in the 2026-09-29 log, so 12 s always
 * failed there.
 */
const GO2RTC_FIRST_FRAME_MS = 30000;
const FOCUS_BLANK_POSTER = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
let _go2rtc          = null;   // live session state; see _go2rtcMount
let _go2rtcWatchdog  = null;
let _go2rtcStatsTid  = null;
let _go2rtcPlayer    = null;   // Promise<boolean>: player module loaded
let _focusEngine     = null;   // 'go2rtc' | 'legacy' | null
let _focusSession    = 0;      // bumped on every open and close
// Cameras whose browser cannot play the stream, camId -> reason. Only a
// codec or mode error is remembered: it fails the same way on every open
// and would show the same red message each time. A timeout or a dropped
// connection is not remembered (2.6.5): the HA app keeps this page open
// for days, and in 2.6.3 one slow start kept a camera on the classic view
// until the page was reloaded.
const _go2rtcDeclined = {};

function _go2rtcLoadPlayer() {
  if (_go2rtcPlayer) return _go2rtcPlayer;
  if (!('customElements' in window) || !('WebSocket' in window)) {
    _go2rtcPlayer = Promise.resolve(false);
    return _go2rtcPlayer;
  }
  _go2rtcPlayer = import(BASE + '/go2rtc/video-rtc.js').then(mod => {
    if (!customElements.get('anycam-video')) {
      class AnyCamVideo extends mod.VideoRTC {
        oninit() {
          super.oninit();
          // Surveillance view: no scrub bar, and muted so autoplay is never blocked.
          this.video.controls = false;
          this.video.muted    = true;
          // 2.6.5: an empty poster. Without one, Android WebView (the HA
          // app) draws a large gray play icon until the first frame.
          this.video.poster   = FOCUS_BLANK_POSTER;
          this.video.addEventListener('playing', () => this._emit('playing'));
        }
        onopen() {
          const modes = super.onopen();
          this.onmessage['anycam'] = msg => {
            if (msg.type === 'error') this._emit('error', String(msg.value || ''));
            else if (msg.type === 'mse') this._emit('mode', 'MSE');
          };
          this._emit('open', modes);
          return modes;
        }
        onclose() {
          // VideoRTC closes the socket on purpose after a WebRTC handoff, and
          // marks wsState CLOSED first. Report only closes it did not intend.
          if (this.wsState !== WebSocket.CLOSED) this._emit('close');
          return super.onclose();
        }
        onpcvideo(video2) {
          super.onpcvideo(video2);
          if (this.pcState !== WebSocket.CLOSED) this._emit('mode', 'WebRTC');
        }
        _emit(kind, value) {
          if (typeof this.onanycam === 'function') this.onanycam(kind, value);
        }
      }
      customElements.define('anycam-video', AnyCamVideo);
    }
    return true;
  }).catch(err => {
    console.warn('[AnyCam] live view player did not load — classic view only:', err);
    return false;
  });
  return _go2rtcPlayer;
}

// Ask the server for a go2rtc stream for one camera profile.
/* ── 3.0.1 (C10): H.265 and the browser ─────────────────────────────────
 * Chrome and Edge play H.265. Firefox plays it only on Windows, through
 * Windows itself, which needs Microsoft's "HEVC Video Extensions" and a
 * graphics chip that decodes H.265. A page cannot install a codec. So the
 * page asks the browser first: when it cannot play H.265, Enhanced View
 * plays the camera's own H.264 stream if it has one, cards skip H.265
 * streams, and otherwise the user gets a plain message.
 */
let _h265Support = null;
function _browserPlaysH265() {
  if (_h265Support !== null) return _h265Support;
  let ok = false;
  try {
    const MS = window.ManagedMediaSource || window.MediaSource;
    ok = !!(MS && MS.isTypeSupported
            && (MS.isTypeSupported('video/mp4; codecs="hvc1.1.6.L153.B0"')
                || MS.isTypeSupported('video/mp4; codecs="hev1.1.6.L153.B0"')));
  } catch (e) {}
  _h265Support = ok;
  return ok;
}

function _isH265(codec) {
  const c = String(codec || '').toLowerCase();
  return c === 'hevc' || c === 'h265';
}

function _h264ProfileIdx(cam) {
  const ps = (cam && cam.stream_profiles) || [];
  for (let i = 0; i < ps.length; i++) {
    if (String(ps[i].stream_codec || '').toLowerCase() === 'h264') return i;
  }
  return -1;
}

function _h265Help() {
  const ua = navigator.userAgent || '';
  if (/Windows/.test(ua) && /Firefox\//.test(ua))
    return 'This browser cannot play H.265 video. Install "HEVC Video Extensions" from the '
         + 'Microsoft Store, or use Chrome or Edge.';
  return 'This browser cannot play H.265 video. Use Chrome or Edge.';
}

// go2rtc's error text, for a person. The original stays in the console.
function _readableLiveError(reason) {
  const r = String(reason || '');
  if (/codecs not matched/i.test(r) && /H26?5|hevc/i.test(r)) return 'this browser cannot play H.265 video';
  if (/codecs not matched/i.test(r)) return "this browser cannot play this camera's video format";
  if (/ICE|webrtc\/offer|webrtc\/answer/i.test(r)) return 'the live connection could not be set up';
  if (/^no video within|^connection closed|^no answer|^go2rtc/i.test(r)) return r;
  return 'the live stream did not start';
}

async function _go2rtcStreamInfo(camId, profIdx) {
  try {
    const r = await fetch(BASE + '/api/go2rtc/focus/' + encodeURIComponent(camId)
                          + '?profile=' + profIdx);
    return await r.json();
  } catch (e) {
    return {ok: false, reason: 'no answer from the addon'};
  }
}

// Try to open Enhanced View on go2rtc. Returns true when this call handled
// the session (playing, or the session was closed meanwhile); false means
// the caller should use the classic view.
async function _go2rtcTryFocus(camId, cam, session) {
  if (_go2rtcDeclined[camId]) return false;
  if (!(await _go2rtcLoadPlayer())) return false;
  if (session !== _focusSession) return true;
  let info = await _go2rtcStreamInfo(camId, 0);
  if (session !== _focusSession) return true;
  if (!info || !info.ok) {
    console.info('[AnyCam] live view not used for ' + camId + ': '
                 + ((info && info.reason) || 'no answer'));
    return false;
  }
  // 3.0.1 (C10): an H.265 stream in a browser that cannot play H.265
  if (_isH265(info.codec) && !_browserPlaysH265()) {
    const alt = _h264ProfileIdx(cam);
    if (alt < 0) {
      console.info('[AnyCam] live view not used for ' + camId + ': H.265 not playable here');
      showToast(_h265Help() + ' Using the classic view.', true);
      return false;
    }
    info = await _go2rtcStreamInfo(camId, alt);
    if (session !== _focusSession) return true;
    if (!info || !info.ok) return false;
    showToast("This browser cannot play H.265 video, so live view plays the camera's H.264 stream.", false);
  }
  _focusCamId  = camId;
  _focusEngine = 'go2rtc';
  await fetch(BASE + '/snap/focus/' + camId + '?engine=go2rtc',
              {method: 'POST'}).catch(() => {});
  if (session !== _focusSession) return true;
  _go2rtcMount(camId, cam, info, session);
  return true;
}

function _go2rtcMount(camId, cam, info, session) {
  const img  = document.getElementById('focus-img');
  const wrap = document.getElementById('focus-video');
  img.style.display  = 'none';
  wrap.innerHTML     = '';
  wrap.style.display = 'block';
  const el = document.createElement('anycam-video');
  el.mode  = 'webrtc,mse';
  el.media = 'video';
  _go2rtc = {
    el, camId, cam, session,
    stream: info.stream,
    codec: String(info.codec || cam.stream_codec || '').toUpperCase(),
    played: false, closes: 0, modes: [], errs: {},
    mode: 'connecting', lastFrames: 0, fps: null,
  };
  el.onanycam = (kind, value) => _go2rtcEvent(session, kind, value);
  wrap.appendChild(el);
  el.src = BASE + '/go2rtc/ws?src=' + encodeURIComponent(info.stream);
  clearTimeout(_go2rtcWatchdog);
  const watchdog = () => {
    // A hidden tab pauses VideoRTC by design; do not blame the stream for it.
    if (document.hidden) { _go2rtcWatchdog = setTimeout(watchdog, GO2RTC_FIRST_FRAME_MS); return; }
    _go2rtcFail(session, 'no video within ' + (GO2RTC_FIRST_FRAME_MS / 1000) + ' s', false);
  };
  _go2rtcWatchdog = setTimeout(watchdog, GO2RTC_FIRST_FRAME_MS);
  clearInterval(_go2rtcStatsTid);
  _go2rtcStatsTid = setInterval(() => _go2rtcStats(session), 1000);
  _focusLoading(true);
  _go2rtcUpdateInfo();
}

function _go2rtcEvent(session, kind, value) {
  const g = _go2rtc;
  if (!g || g.session !== session) return;
  if (kind === 'playing') {
    _go2rtcPlayed(g);
  } else if (kind === 'open') {
    g.modes = Array.isArray(value) ? value : [];
  } else if (kind === 'mode') {
    g.mode = value;
    _go2rtcUpdateInfo();
  } else if (kind === 'error') {
    console.warn('[AnyCam] go2rtc: ' + value);
    if (g.played) return;   // VideoRTC recovers on its own once it has played
    // go2rtc prefixes errors with the mode that failed ("mse: ...",
    // "webrtc/offer: ..."). A WebRTC failure alone is normal behind a VPN
    // while MSE keeps playing, so wait until every attempted mode has
    // failed. An error naming no mode is fatal.
    const failed = g.modes.find(m => value.startsWith(m));
    if (!failed) { _go2rtcFail(session, value || 'stream error', false); return; }
    g.errs[failed] = true;
    if (g.modes.length && g.modes.every(m => g.errs[m])) _go2rtcFail(session, value, true);
  } else if (kind === 'close') {
    if (g.played) { g.mode = 'reconnecting'; _go2rtcUpdateInfo(); return; }
    if (++g.closes >= 2) _go2rtcFail(session, 'connection closed before the first frame', false);
  }
}

function _go2rtcPlayed(g) {
  if (g.played) return;
  g.played = true;
  clearTimeout(_go2rtcWatchdog);
  _go2rtcWatchdog = null;
  _focusLoading(false);
  _go2rtcUpdateInfo();
}

// Once a second: measure decoded fps on this device, and catch a first frame
// in browsers that do not fire 'playing' for a MediaStream source.
function _go2rtcStats(session) {
  const g = _go2rtc;
  if (!g || g.session !== session || !g.el.video) return;
  const v = g.el.video;
  const q = (typeof v.getVideoPlaybackQuality === 'function') ? v.getVideoPlaybackQuality() : null;
  const frames = q ? q.totalVideoFrames : (v.webkitDecodedFrameCount || 0);
  const delta = frames - g.lastFrames;
  g.lastFrames = frames;
  // The counter restarts when VideoRTC swaps MSE for WebRTC; skip that tick.
  g.fps = delta >= 0 ? delta : null;
  if (!g.played && frames > 0 && v.videoWidth > 0) _go2rtcPlayed(g);
  _go2rtcUpdateInfo();
}

function _go2rtcUpdateInfo() {
  const g = _go2rtc;
  if (!g) return;
  const infoEl = document.getElementById('focus-info');
  const v = g.el.video;
  const res = (v && v.videoWidth) ? v.videoWidth + 'x' + v.videoHeight : '…';
  const fps = (g.played && g.fps !== null) ? g.fps + ' fps' : '…';
  const label = g.played ? g.mode : 'connecting…';
  infoEl.innerHTML =
    esc(displayName(g.cam)) + ' — <b>Live (' + esc(label) + '):</b> '
    + res + ' · ' + fps + (g.codec ? ' · ' + esc(g.codec) : '');
}

function _go2rtcUnmount() {
  clearTimeout(_go2rtcWatchdog);
  clearInterval(_go2rtcStatsTid);
  _go2rtcWatchdog = null;
  _go2rtcStatsTid = null;
  const g = _go2rtc;
  _go2rtc = null;
  if (g && g.el) {
    g.el.onanycam = null;
    // Close the socket and peer connection now. Removing the element alone
    // leaves them open for VideoRTC's 5 s DISCONNECT_TIMEOUT, holding the
    // camera's RTSP session in go2rtc for that long.
    try { g.el.ondisconnect(); } catch (e) {}
    if (g.el.reconnectTID) { clearTimeout(g.el.reconnectTID); g.el.reconnectTID = 0; }
    g.el.remove();
  }
  const wrap = document.getElementById('focus-video');
  if (wrap) { wrap.innerHTML = ''; wrap.style.display = 'none'; }
  const img = document.getElementById('focus-img');
  if (img) img.style.display = '';
}

// Leave live view for the classic engine inside the same focus session.
function _go2rtcToClassic(camId, cam, message, isError) {
  _go2rtcUnmount();
  _focusEngine = 'legacy';
  if (message) showToast(message, !!isError);
  _startFocusPoll(camId, cam);
}

// remember: true only for a failure that repeats on every open (see
// _go2rtcDeclined).
function _go2rtcFail(session, reason, remember) {
  const g = _go2rtc;
  if (!g || g.session !== session) return;
  console.warn('[AnyCam] live view failed for ' + g.camId + ': ' + reason
               + ' — using the classic view');
  if (remember) _go2rtcDeclined[g.camId] = reason;
  // 3.0.1 (C10): readable text, and the fix when the browser cannot play H.265
  const readable = _readableLiveError(reason);
  const help = readable === 'this browser cannot play H.265 video' ? ' ' + _h265Help() : '';
  _go2rtcToClassic(g.camId, g.cam,
                   'Live view unavailable (' + readable + ') — using the classic view.' + help, true);
}

// 3.0.1 (C20): the Classic button is gone. The classic view stays as the
// automatic fallback (_go2rtcFail, openFocus).

/* ── Enhanced View loading message (2.6.5) ───────────────────────────────
 * Shown from open until the first frame of this session, in both engines.
 * The classic view hides its picture meanwhile: until the new stream
 * delivers, the server holds only the card's small thumbnail frame. */
function _focusLoading(on) {
  const el = document.getElementById('focus-loading');
  if (!el) return;
  el.style.display = on ? 'block' : 'none';
  if (on) _focusLoadingNote('');
}

function _focusLoadingNote(text) {
  const el = document.getElementById('focus-loading-note');
  if (el) el.textContent = text;
}

/* ── Enhanced View landscape (2.6.5) ─────────────────────────────────────
 * Landscape on a touch screen fills the screen with the picture: the
 * bottom bar hides, and the page asks Home Assistant to hide its title
 * bar. HA's app panel turns on kiosk mode when the add-on page sends
 * subscribe-properties with kioskMode, and off again on unsubscribe
 * (home-assistant/frontend, src/panels/app/ha-panel-app.ts). The
 * Fullscreen API is not an option: HA's iframe does not allow it. */
const _focusLandMq = window.matchMedia
  ? window.matchMedia('(orientation: landscape) and (pointer: coarse)') : null;
let _haKioskOn = false;

function _haKiosk(on) {
  if (on === _haKioskOn || window.parent === window) return;
  _haKioskOn = on;
  try {
    window.parent.postMessage({
      type: on ? 'home-assistant/subscribe-properties'
               : 'home-assistant/unsubscribe-properties',
      kioskMode: true,
    }, window.location.origin);
  } catch (e) {}
}

function _focusLandscapeSync() {
  const ov = document.getElementById('focus-overlay');
  if (!ov) return;
  const on = ov.style.display !== 'none' && !!(_focusLandMq && _focusLandMq.matches);
  ov.classList.toggle('focus-landscape', on);
  _haKiosk(on);
}

if (_focusLandMq) {
  if (_focusLandMq.addEventListener) _focusLandMq.addEventListener('change', _focusLandscapeSync);
  else if (_focusLandMq.addListener) _focusLandMq.addListener(_focusLandscapeSync);
}

/* ── Live cards (2.6.6, build plan C1) ───────────────────────────────────
 * Each card plays the camera's smallest stream through go2rtc, chosen by
 * the server (/api/go2rtc/card). A card that cannot play live uses the
 * snapshot path (startSnap), as before 2.6.6.
 *
 * One player per camera lives in _cardLive for the whole page. renderGrid
 * and updateCard rewrite a card's HTML on every refresh, so the player is
 * moved into the new card instead of rebuilt: VideoRTC keeps its stream
 * across a move (its disconnect waits DISCONNECT_TIMEOUT, and the
 * reconnect cancels that wait).
 *
 * VideoRTC pauses a player that is off screen (visibilityThreshold) or on
 * a hidden page (visibilityCheck), which gives "only the cards on screen
 * play". Enhanced View pauses every card while it is open.
 */
const _cardLive      = {};   // camId -> {el, played, modes, errs, closes, tid}
const _cardLiveOff   = {};   // camId -> {reason, retryAt}: use snapshots
const CARD_LIVE_RETRY_MS = 5 * 60 * 1000;   // retry a timeout after 5 min
let _cardLiveTick = null;

// Called by initSnaps for each card image. True when the card is (or is
// becoming) live, false when the caller should poll snapshots.
function cardLiveAttach(camId) {
  const off = _cardLiveOff[camId];
  if (off && (off.retryAt === 0 || Date.now() < off.retryAt)) return false;
  if (off) delete _cardLiveOff[camId];
  const st = _cardLive[camId];
  if (!st) { _cardLiveStart(camId); _cardLivePlace(camId); return true; }
  _cardLivePlace(camId);
  return true;
}

// Put the camera's player into its current card, over the placeholder.
function _cardLivePlace(camId) {
  const st  = _cardLive[camId];
  const img = document.querySelector('[data-snap="' + CSS.escape(camId) + '"]');
  if (!st || !st.el || !img) return;
  const wrap = img.parentNode;
  if (st.el.parentNode !== wrap) wrap.appendChild(st.el);
  if (st.played) _cardLiveShow(camId);
}

function _cardLiveShow(camId) {
  const st = _cardLive[camId];
  if (st && st.el) st.el.style.opacity = '1';
  const ph = document.getElementById('ph-' + camId);
  if (ph) ph.style.display = 'none';
}

// 3.1.0 (C19): a phone gets still pictures for a camera whose smallest
// stream is wider than a card needs; a computer plays it live.
function _isPhone() {
  const uad = navigator.userAgentData;
  if (uad && typeof uad.mobile === 'boolean') return uad.mobile;
  return /Android.+Mobile|iPhone|iPod|Windows Phone/i.test(navigator.userAgent || '');
}

function _cardQuery() {
  const q = [];
  // 3.0.1 (C10): a browser that cannot play H.265 asks for another stream
  if (!_browserPlaysH265()) q.push('h265=0');
  if (!_isPhone()) q.push('wide=1');
  return q.length ? '?' + q.join('&') : '';
}

async function _cardLiveStart(camId) {
  const st = {el: null, played: false, modes: [], errs: {}, closes: 0, tid: 0};
  _cardLive[camId] = st;
  let info = null;
  try {
    info = await (await fetch(BASE + '/api/go2rtc/card/' + encodeURIComponent(camId)
                              + _cardQuery())).json();
  } catch (e) {}
  if (_cardLive[camId] !== st) return;
  if (!info || !info.ok) {
    // Remembered for the page unless the server says to retry (go2rtc
    // still starting) or did not answer.
    _cardLiveFail(camId, st, (info && info.reason) || 'no answer from the addon', !!info && !info.retry);
    return;
  }
  if (info.kind === 'mjpeg') { _cardLiveMjpeg(camId, st, info); return; }
  if (!(await _go2rtcLoadPlayer())) { _cardLiveFail(camId, st, 'player did not load', true); return; }
  if (_cardLive[camId] !== st) return;
  const el = document.createElement('anycam-video');
  el.className = 'card-live';
  el.mode  = 'webrtc,mse';
  el.media = 'video';
  el.visibilityThreshold = 0.01;   // read once, when the player is first attached
  el.style.opacity = '0';          // not display:none: that reads as off screen
  el.onanycam = (kind, value) => _cardLiveEvent(camId, st, kind, value);
  el.onclick  = () => openFocus(camId);
  st.el = el;
  _cardLivePlace(camId);
  if (!el.isConnected) { _cardLiveFail(camId, st, 'card is gone', false); return; }
  el.src = BASE + '/go2rtc/ws?src=' + encodeURIComponent(info.stream);
  _cardLiveArm(camId, st);
  if (!_cardLiveTick) _cardLiveTick = setInterval(_cardLiveCheck, 1000);
}

/* ── 3.1.0 (C19): a card that plays the camera's MJPEG stream ────────────
 * The add-on sends each JPEG over a WebSocket (anycam_mjpeg.py); the card
 * shows the newest one. A picture that arrives while the one before is
 * still loading replaces the waiting one, so the card never falls behind.
 */
function _wsURL(path) {
  const u = new URL(BASE + path, location.href);
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
  return u.href;
}

function _cardLiveMjpeg(camId, st, info) {
  const img = document.createElement('img');
  img.className = 'card-live';
  img.alt = 'Live';
  img.style.opacity = '0';
  img.onclick = () => openFocus(camId);
  st.el = img;
  st.kind = 'mjpeg';
  st.url = info.url;
  _cardLivePlace(camId);
  if (!img.isConnected) { _cardLiveFail(camId, st, 'card is gone', false); return; }
  if (!document.hidden && !_focusCamId) _cardMjpegOpen(camId, st);
  else st.paused = true;
  _cardLiveArm(camId, st);
}

function _cardMjpegOpen(camId, st) {
  if (st.ws) return;
  let ws;
  try { ws = new WebSocket(_wsURL(st.url)); }
  catch (e) { _cardLiveFail(camId, st, 'the live stream could not open', false); return; }
  ws.binaryType = 'blob';
  st.ws = ws;
  ws.onmessage = ev => {
    if (_cardLive[camId] !== st || st.ws !== ws) return;
    if (typeof ev.data === 'string') { _cardLiveFail(camId, st, ev.data.replace(/^error: /, ''), false); return; }
    if (st.loading) { st.next = ev.data; return; }
    _cardMjpegShow(camId, st, ev.data);
  };
  ws.onclose = () => {
    if (st.ws !== ws) return;
    st.ws = null;
    if (_cardLive[camId] === st) _cardLiveFail(camId, st, 'the live stream closed', false);
  };
}

function _cardMjpegShow(camId, st, blob) {
  st.loading = true;
  const url = URL.createObjectURL(blob);
  const done = ok => {
    URL.revokeObjectURL(url);     // the shown picture stays; only the blob goes
    st.loading = false;
    if (_cardLive[camId] !== st) return;
    if (ok) _cardLivePlayed(camId, st);
    const next = st.next;
    st.next = null;
    if (next) _cardMjpegShow(camId, st, next);
  };
  st.el.onload  = () => done(true);
  st.el.onerror = () => done(false);
  st.el.src = url;
}

function _cardMjpegClose(st) {
  const ws = st.ws;
  st.ws = null;
  st.next = null;
  if (ws) { ws.onmessage = null; ws.onclose = null; try { ws.close(); } catch (e) {} }
}

// A hidden page or an open Enhanced View closes the MJPEG streams, as
// VideoRTC does for the other live cards.
function _cardMjpegPause(pause) {
  Object.keys(_cardLive).forEach(camId => {
    const st = _cardLive[camId];
    if (st.kind !== 'mjpeg') return;
    st.paused = pause;
    if (pause) _cardMjpegClose(st);
    else if (st.el && st.el.isConnected) _cardMjpegOpen(camId, st);
  });
}
document.addEventListener('visibilitychange', () => _cardMjpegPause(document.hidden || !!_focusCamId));

// Give up on a card that shows no video within GO2RTC_FIRST_FRAME_MS of
// actually connecting. An off-screen or hidden player is not connected,
// so the time does not count against it.
function _cardLiveArm(camId, st) {
  clearTimeout(st.tid);
  st.tid = setTimeout(() => {
    if (_cardLive[camId] !== st || st.played) return;
    const connected = st.kind === 'mjpeg' ? !!st.ws : !!(st.el.ws || st.el.pc);
    if (document.hidden || _focusCamId || !connected) { _cardLiveArm(camId, st); return; }
    _cardLiveFail(camId, st, 'no video within ' + (GO2RTC_FIRST_FRAME_MS / 1000) + ' s', false);
  }, GO2RTC_FIRST_FRAME_MS);
}

// Once a second: catch a first frame in browsers that do not fire 'playing'.
function _cardLiveCheck() {
  Object.keys(_cardLive).forEach(camId => {
    const st = _cardLive[camId];
    const v = st.el && st.el.video;
    if (!st.played && v && v.videoWidth > 0) _cardLivePlayed(camId, st);
  });
}

function _cardLivePlayed(camId, st) {
  if (st.played) return;
  st.played = true;
  clearTimeout(st.tid);
  _cardLiveShow(camId);
}

function _cardLiveEvent(camId, st, kind, value) {
  if (_cardLive[camId] !== st) return;
  if (kind === 'playing') {
    _cardLivePlayed(camId, st);
  } else if (kind === 'open') {
    st.modes = Array.isArray(value) ? value : [];
  } else if (kind === 'error') {
    if (st.played) return;   // VideoRTC recovers on its own once it has played
    const failed = st.modes.find(m => value.startsWith(m));
    if (!failed) { _cardLiveFail(camId, st, value || 'stream error', false); return; }
    st.errs[failed] = true;
    // Every mode failed: this browser cannot play the codec. That will not
    // change during this page load.
    if (st.modes.length && st.modes.every(m => st.errs[m])) _cardLiveFail(camId, st, value, true);
  } else if (kind === 'close') {
    if (!st.played && ++st.closes >= 2) _cardLiveFail(camId, st, 'connection closed before the first frame', false);
  }
}

// Switch one card to snapshots. remember: the failure repeats on every try
// (codec, no suitable stream), so do not retry during this page load.
function _cardLiveFail(camId, st, reason, remember) {
  if (_cardLive[camId] !== st) return;
  console.info('[AnyCam] card ' + camId + ' uses snapshots: ' + reason);
  clearTimeout(st.tid);
  if (st.kind === 'mjpeg') _cardMjpegClose(st);
  if (st.el) {
    st.el.onanycam = null;
    try { st.el.ondisconnect(); } catch (e) {}
    if (st.el.reconnectTID) { clearTimeout(st.el.reconnectTID); st.el.reconnectTID = 0; }
    st.el.remove();
  }
  delete _cardLive[camId];
  _cardLiveOff[camId] = {reason, retryAt: remember ? 0 : Date.now() + CARD_LIVE_RETRY_MS};
  if (document.querySelector('[data-snap="' + CSS.escape(camId) + '"]')) startSnap(camId);
}

// Enhanced View open: stop every card's stream; closed: resume them.
function cardLivePauseAll(pause) {
  _cardMjpegPause(pause || document.hidden);
  Object.values(_cardLive).forEach(st => {
    if (st.kind === 'mjpeg' || !st.el || !st.el.isConnected) return;
    if (pause) st.el.disconnectedCallback();
    else st.el.connectedCallback();
  });
}

// Drop the players of cards no longer on the page (camera removed).
function cardLivePrune() {
  Object.keys(_cardLive).forEach(camId => {
    if (document.querySelector('[data-snap="' + CSS.escape(camId) + '"]')) return;
    const st = _cardLive[camId];
    _cardLiveFail(camId, st, 'card removed', false);
    delete _cardLiveOff[camId];
  });
}

/* ── Focus / enhanced view ───────────────────────────────────────────────── */
let _focusCamId   = null;
let _focusTimer   = null;
let _focus4kWarnTimer = null;   // auto-dismiss timer for the 4K-fallback toast

async function openFocus(camId) {
  const cam = cameras.find(c => c.id === camId);
  if (!cam || cam.status !== 'ready') return;

  const session = ++_focusSession;
  document.getElementById('focus-overlay').style.display = 'flex';
  document.getElementById('focus-info').textContent = displayName(cam) + ' — loading…';
  _focusLoading(true);
  _focusLandscapeSync();
  cardLivePauseAll(true);   // 2.6.6: one stream at a time on the viewing device
  // 2.6.3: live view through go2rtc first; the classic view when go2rtc is
  // not ready, cannot serve this camera, or this browser cannot play it.
  if (await _go2rtcTryFocus(camId, cam, session)) return;
  if (session !== _focusSession) return;
  _focusEngine = 'legacy';
  _startFocusPoll(camId, cam);
}

async function _startFocusPoll(camId, cam) {
  _focusCamId = camId;
  const img   = document.getElementById('focus-img');
  // 2.6.5: hide the picture until this session's stream delivers (see
  // _focusLoading). Hidden before the POST: the server may answer with
  // the card's last frame at any time after it.
  let _ready = false;
  img.style.visibility = 'hidden';
  _focusLoading(true);
  // Tell server: enter focus mode (other cams throttle, this cam goes native res)
  await fetch(BASE + '/snap/focus/' + camId, {method: 'POST'}).catch(() => {});

  const infoEl = document.getElementById('focus-info');
  const codec  = (cam.stream_codec || '?').toUpperCase();
  const name   = esc(displayName(cam));   // 2.6.6 (B4): innerHTML below

  // Show placeholder until first real measurements arrive
  infoEl.textContent = name + ' — loading…';

  // Live measurement state.
  // fetch() is used instead of Image() so we can read X-Frame-Count response
  // header and only count frames that are actually new from ffmpeg.
  // Polling with Image() measures delivery rate (~16fps), not production rate.
  let _lastFrameCount = -1;   // server frame_count from last response
  let _newFrames      = 0;    // new frames seen in current 1-second window
  let _fpsWindowStart = performance.now();
  let _liveFps        = null;
  let _liveRes        = null;
  let _stepRes        = null;  // current ladder tier resolution from X-Step-Res header
  let _stepFps        = null;  // current ladder tier fps from X-Step-FPS header
  let _prevBlobUrl    = null;
  // 2.4.0-rc3.2: track snap_mode so we can show a one-time toast when
  // the server transitions RTSP → HTTP-snap fallback. Without this the
  // dropdown silently disables (the existing tooltip is hover-gated and
  // most users never see it). null = first response, no transition yet.
  let _lastSnapMode   = null;
  // 2.4.0-rc3.3 Bug B fix: track stream status so we show "Connecting…" /
  // "Switching transport…" toasts during the retry-cycle window where
  // ffmpeg is crashing. Without this, the focus view appears frozen.
  let _lastStreamStatus = null;

  // Info bar format:
  //   Name — Actual Feed: WxH · X fps  [Adapted Quality: WxH · fps]
  // "Actual Feed"     = measured from actual received JPEG + frame count.
  // "Adapted Quality" = current adaptive ladder tier from server headers.
  //                     Only shown when adaptive_quality config is on OR
  //                     the user has manually picked a resolution/fps tier.
  function _updateInfoBar() {
    const realRes  = _liveRes  || '…';
    const realFps  = _liveFps  !== null ? _liveFps + ' fps' : 'measuring…';
    const stepRes  = _stepRes  || '…';
    const stepFpsS = _stepFps  !== null
      ? (_stepFps === 'uncapped' ? 'uncapped' : _stepFps + ' fps')
      : '…';
    // Only show Adapted Quality when using ffmpeg (rtsp mode): in http
    // fallback mode there is no quality ladder.
    const httpMode    = _lastSnapMode === 'http';
    const showAdapted = !httpMode
      && typeof CFG_ADAPTIVE_QUALITY !== 'undefined' && CFG_ADAPTIVE_QUALITY;
    infoEl.innerHTML =
      name + ' — ' +
      '<b>Actual Feed:</b> ' + realRes + ' · ' + realFps +
      (showAdapted
        ? ' &nbsp;<b>Adapted Quality:</b> ' + stepRes + ' · ' + stepFpsS
        : '');
  }

  // Fetch-based polling at 60ms. X-Frame-Count tells us when a new frame
  // has actually been produced by ffmpeg vs the same buffered frame re-served.
  // X-Step-Res / X-Step-FPS carry the current adaptive ladder tier.
  const poll = () => {
    if (_focusCamId !== camId) return;
    fetch(BASE + '/snapshot/' + camId + '?t=' + Date.now() + '&focus=1')
      .then(resp => {
        if (!resp.ok) return null;
        const serverCount = parseInt(resp.headers.get('X-Frame-Count') || '-1');
        const stepRes     = resp.headers.get('X-Step-Res');
        const stepFps     = resp.headers.get('X-Step-FPS');
        const snapMode    = resp.headers.get('X-Snap-Mode') || 'rtsp';
        // 2.4.0-rc3.3 Bug B fix: X-Stream-Status surfaces the retry phase so
        // the user sees "Connecting…" or "Switching transport…" during the
        // 15-30s window where ffmpeg keeps crashing on 0-frame failures
        // before the http_snap fallback engages. Without this status, the
        // focus view appears frozen on the last cached frame with no
        // explanation. Possible values:
        //   ok                  — normal (frames flowing or fresh start, no toast)
        //   connecting          — ffmpeg has crashed at least once, still trying TCP
        //   switching_transport — TCP failed 3×, now trying UDP transport
        //   http_fallback       — both transports gave up, in http_snap mode
        //                         (handled separately via the existing httpFallback path)
        const streamStatus = resp.headers.get('X-Stream-Status') || 'ok';
        // 2.6.5: frames this camera has produced since this focus session
        // began. 0 means the server still holds only the card's thumbnail.
        const focusFrames  = parseInt(resp.headers.get('X-Focus-Frames') || '1');
        if (!_ready && focusFrames > 0) {
          _ready = true;
          img.style.visibility = '';
          _focusLoading(false);
        }
        if (stepRes) {
          // Detect 4K → smaller fallback so we can surface a one-time message.
          // _stepRes is the previously seen tier resolution; if it was 4K-class
          // (≥3840 wide) and the new tier is smaller, the adaptive controller
          // just stepped down due to fast-death (CPU couldn't keep up with 4K).
          //
          // 2.6.5: the manual Resolution control is gone, so every
          // step-down is automatic and always gets the message.
          if (_stepRes && _stepRes !== stepRes) {
            const oldW = parseInt((_stepRes.split('x')[0]) || '0');
            const newW = parseInt((stepRes.split('x')[0])  || '0');
            if (oldW >= 3840 && newW > 0 && newW < oldW) {
              const warnEl = document.getElementById('focus-warning');
              const txt    = document.getElementById('focus-warn-text');
              if (warnEl && txt) {
                txt.textContent = '4K too demanding for this hardware — falling back to secondary stream';
                warnEl.style.display = 'flex';
                clearTimeout(_focus4kWarnTimer);
                _focus4kWarnTimer = setTimeout(() => {
                  warnEl.style.display = 'none';
                }, 5000);
              }
            }
          }
          _stepRes = stepRes;
        }
        if (stepFps) _stepFps = stepFps;
        const httpFallback = (snapMode === 'http');
        // 2.4.0-rc3.2: surface the fallback transition as a visible toast.
        // We reuse the focus-warning element that already exists for the
        // 4K-too-demanding case. Toast stays visible the whole time
        // we're in HTTP fallback; clears the moment RTSP comes back.
        if (_lastSnapMode !== null && _lastSnapMode !== snapMode) {
          const warnEl = document.getElementById('focus-warning');
          const txt    = document.getElementById('focus-warn-text');
          if (warnEl && txt) {
            if (httpFallback) {
              txt.textContent = 'Live RTSP stream unavailable — showing periodic snapshots from this camera';
              warnEl.style.display = 'flex';
              // Don't auto-hide; this state persists for the whole focus session
              clearTimeout(_focus4kWarnTimer);
            } else {
              // Transitioned back to RTSP — clear the warning if it's ours
              if (txt.textContent.indexOf('periodic snapshots') !== -1) {
                warnEl.style.display = 'none';
              }
            }
          }
        }
        _lastSnapMode = snapMode;
        // 2.4.0-rc3.3 Bug B fix: surface stream-status transitions so the
        // user sees what's happening during the retry phase. The
        // "connecting" and "switching_transport" states fire during the
        // 15-30s window between the first ffmpeg failure and either
        // recovery or HTTP-snap fallback — without this, the focus view
        // looks frozen on the last cached frame with no explanation.
        // Suppress when snapMode is already 'http' since the existing
        // httpFallback toast covers that state more specifically.
        // 2.6.5: before the first frame the loading message carries this
        // status as its second line instead, so the two do not overlap.
        if (!_ready) {
          _focusLoadingNote(
            streamStatus === 'connecting' ? 'Still connecting to the camera'
            : streamStatus === 'switching_transport' ? 'Trying UDP: this camera does not accept RTSP over TCP'
            : '');
        } else if (_lastStreamStatus !== streamStatus && snapMode !== 'http') {
          const warnEl = document.getElementById('focus-warning');
          const txt    = document.getElementById('focus-warn-text');
          if (warnEl && txt) {
            // Only act on this status if our current message isn't already
            // a higher-priority one (4K-too-demanding, http_snap fallback).
            const curText = txt.textContent || '';
            const isOurStatus = (curText.indexOf('Connecting') !== -1 ||
                                 curText.indexOf('Switching transport') !== -1);
            const isFreshSlot = (warnEl.style.display !== 'flex' || isOurStatus);
            if (streamStatus === 'connecting' && isFreshSlot) {
              txt.textContent = 'Connecting to RTSP stream…';
              warnEl.style.display = 'flex';
              clearTimeout(_focus4kWarnTimer);  // we manage our own lifecycle
            } else if (streamStatus === 'switching_transport' && isFreshSlot) {
              txt.textContent = 'Switching transport (TCP → UDP) — camera does not support TCP RTSP';
              warnEl.style.display = 'flex';
              clearTimeout(_focus4kWarnTimer);
            } else if (streamStatus === 'ok' && isOurStatus) {
              // Clear our toast when stream recovers
              warnEl.style.display = 'none';
            }
          }
        }
        _lastStreamStatus = streamStatus;
        return resp.blob().then(blob => ({ blob, serverCount }));
      })
      .then(result => {
        if (!result || _focusCamId !== camId) return;
        const { blob, serverCount } = result;
        const isNew = serverCount >= 0 && serverCount !== _lastFrameCount;
        if (isNew) {
          _lastFrameCount = serverCount;
          _newFrames++;
          const blobUrl = URL.createObjectURL(blob);
          if (_prevBlobUrl) URL.revokeObjectURL(_prevBlobUrl);
          _prevBlobUrl = blobUrl;
          img.onload = () => {
            if (img.naturalWidth && img.naturalHeight) {
              _liveRes = img.naturalWidth + 'x' + img.naturalHeight;
            }
          };
          img.src = blobUrl;
        }
        // Update FPS display once per second
        const now     = performance.now();
        const elapsed = (now - _fpsWindowStart) / 1000;
        if (elapsed >= 1.0) {
          const measuredFps = Math.round(_newFrames / elapsed);
          // Grace period: only zero out fps if no frames for >4 seconds.
          // During a 2s restart gap the fps would otherwise flash to 0.
          if (measuredFps > 0 || elapsed >= 4.0) {
            _liveFps = measuredFps > 0 ? measuredFps : null;  // null = show "…"
          }
          _newFrames      = 0;
          _fpsWindowStart = now;
          _updateInfoBar();
        }
      })
      .catch(() => {})
      .finally(() => {
        if (_focusCamId === camId) _focusTimer = setTimeout(poll, 60);
      });
  };
  poll();
}

async function closeFocus() {
  _focusSession++;          // any open still awaiting the server bails out
  _focusCamId  = null;
  _focusEngine = null;
  _go2rtcUnmount();         // no-op unless live view was playing
  clearTimeout(_focusTimer);
  clearTimeout(_focus4kWarnTimer);
  await fetch(BASE + '/snap/focus', {method: 'DELETE'}).catch(() => {});
  document.getElementById('focus-overlay').style.display = 'none';
  document.getElementById('focus-img').src = '';
  document.getElementById('focus-img').style.visibility = '';
  document.getElementById('focus-warning').style.display = 'none';
  _focusLoading(false);
  _focusLandscapeSync();   // leaves HA kiosk mode
  cardLivePauseAll(false);
}

// Close focus on Escape key
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && _focusCamId) closeFocus();
  else if (e.key === 'Escape' && _csCamId) closeCamSettings();
});

// When the browser tab returns to focus after being backgrounded, the browser
// may have throttled or dropped pending image requests — resetting _snapErrors
// prevents those stale failures from triggering "Stream unavailable" on the card.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    Object.keys(_snapErrors).forEach(camId => {
      _snapErrors[camId] = 0;
      delete _snapErrSince[camId];
    });
  }
});

/* ── Storage browser ─────────────────────────────────────────────────────── */
let _storageData    = null;
let _dragSrc        = null;   // {path, folder} of file being dragged

async function loadStorage() {
  document.getElementById('storage-list').innerHTML
    = '<p style="color:var(--text-dim);padding:24px">Loading...</p>';
  try {
    const d = await (await fetch(BASE + '/api/storage')).json();
    _storageData = d;
    renderStorage(d);
  } catch(e) {
    document.getElementById('storage-list').innerHTML
      = '<p style="color:var(--red)">Error loading storage: ' + esc(String(e)) + '</p>';
  }
}



function storDragStart(e, path, folder) {
  _dragSrc = {path, folder};
  e.dataTransfer.effectAllowed = 'move';
}

async function storDrop(e, dstFolder) {
  e.preventDefault();
  if (!_dragSrc || _dragSrc.folder === dstFolder) return;
  const r = await fetch(BASE + '/api/storage/move', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({src_path: _dragSrc.path, dst_folder: dstFolder})
  });
  if (r.ok) { showToast('Moved to ' + dstFolder); loadStorage(); }
  else showToast('Move failed', true);
  _dragSrc = null;
}

async function storDeleteFile(path) {
  if (!confirm('Delete ' + path.split('/').pop() + '?')) return;
  const r = await fetch(BASE + '/api/storage/file', {
    method: 'DELETE', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path})
  });
  if (r.ok) { showToast('Deleted'); loadStorage(); }
  else showToast('Delete failed', true);
}

function storRenameFile(path, oldName) {
  const newName = prompt('Rename file:', oldName);
  if (!newName || newName === oldName) return;
  fetch(BASE + '/api/storage/rename', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({old_path: path, new_name: newName})
  }).then(r => { if (r.ok) { showToast('Renamed'); loadStorage(); } else showToast('Rename failed', true); });
}

function storRenameFolder(folder) {
  const newName = prompt('Rename folder:', folder);
  if (!newName || newName === folder) return;
  fetch(BASE + '/api/storage/rename', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({old_path: folder, new_name: newName})
  }).then(r => { if (r.ok) { showToast('Renamed'); loadStorage(); } else showToast('Rename failed', true); });
}

/* ── Toast notification ──────────────────────────────────────────────────── */
let _toastTimer = null;
function showToast(msg, isError = false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.background  = isError ? 'var(--red)' : 'var(--primary)';
  t.style.display     = 'block';
  t.style.opacity     = '1';
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => {
    t.style.opacity = '0';
    setTimeout(() => { t.style.display = 'none'; }, 300);
  // 2.4.0-rc2.8: 2500ms → 7000ms. The Deep Re-Probe completion toast
  // ("🔒 N locked stream(s) found", "no streams found", etc.) was
  // disappearing before users could read it. 7s gives enough time
  // to read a one-line message comfortably without lingering long
  // enough to feel obstructive.
  }, 7000);
}



/* ── Camera grid ───────────────────────────────────────────────────────────── */
// 2.4.0-rc3.3 (Camera Cards Fixed Position): cards used to swap positions
// when a user logged into a camera. Root cause: login changes the camera's
// ID (e.g. "10.0.0.22_onvif" → "10.0.0.22_onvif_MainStreamProfileToken")
// and the prior renderGrid logic matched by ID alone. The old ID disappeared
// from the cameras array, the corresponding DOM card was removed, and the
// new ID's card got appendChild'd at the end — visible as a "swap" since
// the old card vanished and a new one appeared in a different slot.
//
// Fix: match cards by a stable key derived from the camera's IP:port,
// which doesn't change across login. When an existing card's ID has
// changed (because the same IP:port now has a profile-tokened ID), we
// update its dataset.id in place and re-render its content — DOM
// position is naturally preserved because we never remove-and-re-append.
//
// Future drag-to-reorder feature can persist a user-set order in
// localStorage by capturing the DOM order of [data-stable-key] values
// and replaying it as a sort comparator at the top of renderGrid.
function _stableCardKey(cam) {
  // ip:port survives login (which mutates id but not network identity).
  // Falls back to id-as-key if a camera somehow has no ip (synthetic
  // entries during testing / dev), preserving old behavior in that edge.
  //
  // 2.5.0-rc1.7: append a #chN suffix when cam.channel is set, so multi-
  // channel DVR cards (Lorex/Dahua family) all sharing one ip:port get
  // distinct stable_keys. Without this, all 8 channel cards collapse
  // onto the same key — renderGrid's cardsByKey lookup returns the same
  // DOM element on every iteration, the loop overwrites that one card's
  // content with each iteration's HTML, and the new-card-append path is
  // never reached. Field-confirmed in 2.5.0-rc1.6 log: cred-auth
  // registered 7 channel cards in CAMERAS, +8s reload's renderGrid
  // collapsed them into one DOM card showing ch8's content (last
  // iteration wins). Only ch8 was [data-snap]'d, only ch8 was polled,
  // ch2-ch7 + parent idled out at 30s without ever being rendered.
  // Non-DVR cards have no `channel` field so they keep plain ip:port —
  // the rc3.3 fixed-position-on-login behaviour is preserved.
  if (cam.ip) {
    let key = cam.ip + ':' + (cam.port || '');
    if (cam.channel) key += '#ch' + cam.channel;
    return key;
  }
  return 'id:' + cam.id;
}

function renderGrid() {
  const grid  = document.getElementById('cam-grid');
  const empty = document.getElementById('empty-state');
  const count = document.getElementById('cam-count');
  count.textContent = '';   // device count shown in scan status bar — not duplicated here
  empty.style.display = cameras.length ? 'none' : '';

  // Build maps from existing DOM cards. cardsByKey is the primary lookup
  // (matches across login ID changes); cardsById covers the legacy edge
  // where an old card was rendered before the dataset.stableKey attribute
  // was introduced (first render after upgrade).
  const cardsByKey = new Map();
  const cardsById  = new Map();
  [...grid.querySelectorAll('.camera-card')].forEach(c => {
    if (c.dataset.id)        cardsById.set(c.dataset.id, c);
    if (c.dataset.stableKey) cardsByKey.set(c.dataset.stableKey, c);
  });

  const seenKeys = new Set();
  cameras.forEach(cam => {
    const key = _stableCardKey(cam);
    seenKeys.add(key);
    // Prefer key match (handles login ID change). Fall back to id match
    // (handles first render or stable_key-less legacy cards).
    let card = cardsByKey.get(key) || cardsById.get(cam.id);
    if (card) {
      // Existing card — update in place, preserving DOM position.
      if (card.dataset.id !== cam.id) {
        // ID changed (typical: login added a profile token). Preserve
        // position, update DOM identity, stop the old snap loop.
        stopSnap(card.dataset.id);
        card.dataset.id = cam.id;
      }
      card.dataset.stableKey = key;
      // Mirror updateCard's logic without re-querying — we already have
      // the element in hand.
      stopSnap(cam.id);
      card.innerHTML = cardHTML(cam);
      // uncertain class needs to be re-applied since we may have a fresh
      // verdict from the server (e.g. brand identification just ran).
      const isUncertain = (cam.verdict === 'uncertain' || cam.verdict === 'not_camera');
      card.classList.toggle('uncertain', isUncertain);
    } else {
      // New camera — append at the end. Future cards land here too.
      const newCard = buildCard(cam);
      newCard.dataset.stableKey = key;
      grid.appendChild(newCard);
    }
  });

  // Remove DOM cards whose stable key is no longer in the cameras array
  // (camera was deleted, marked not-camera, etc.). Using stable_key here
  // means a login-induced ID change does NOT trigger a removal, which is
  // the whole point of this rewrite.
  [...grid.querySelectorAll('.camera-card')].forEach(c => {
    const k = c.dataset.stableKey || ('id:' + c.dataset.id);
    if (!seenKeys.has(k)) c.remove();
  });

  _cardOrderApply(grid);
  grid.querySelectorAll('video[data-hls]').forEach(v => { if (!v._hls) initHls(v); });
  initSnaps();   // start polling for any newly added data-snap images
  cardLivePrune();
}

/* ── 3.1.0 (D3): drag a card to move it; one order for every viewer ───────
 * The add-on saves the order (/api/card_order) and sends the cameras in
 * it, so every viewer sees the same order at the next load. The drag uses
 * pointer events, which work with a mouse and with a finger. The card
 * moves once, when it is dropped: moving a card restarts its live stream.
 */
function _cardOrderApply(grid) {
  const want = cameras.map(_stableCardKey);
  const cards = [...grid.querySelectorAll('.camera-card')];
  const have = cards.map(c => c.dataset.stableKey);
  if (want.join('\n') === have.join('\n')) return;
  const byKey = new Map(cards.map(c => [c.dataset.stableKey, c]));
  want.forEach(k => { const c = byKey.get(k); if (c) grid.appendChild(c); });
}

let _cardDrag = null;   // {card, target, after}

function cardDragStart(ev, handle) {
  const card = handle.closest('.camera-card');
  if (!card || (ev.button !== undefined && ev.button !== 0)) return;
  ev.preventDefault();
  _cardDrag = {card, target: null, after: false};
  card.classList.add('drag-src');
  try { handle.setPointerCapture(ev.pointerId); } catch (e) {}
  handle.onpointermove = _cardDragMove;
  handle.onpointerup = handle.onpointercancel = e => _cardDragEnd(e, handle);
}

function _cardDragMark(target, after) {
  document.querySelectorAll('.camera-card.drop-before,.camera-card.drop-after')
    .forEach(c => c.classList.remove('drop-before', 'drop-after'));
  if (target) target.classList.add(after ? 'drop-after' : 'drop-before');
}

function _cardDragMove(ev) {
  if (!_cardDrag) return;
  const under = document.elementFromPoint(ev.clientX, ev.clientY);
  const target = under && under.closest('.camera-card');
  if (!target || target === _cardDrag.card) { _cardDrag.target = null; _cardDragMark(null); return; }
  const r = target.getBoundingClientRect();
  _cardDrag.target = target;
  _cardDrag.after = ev.clientX > r.left + r.width / 2;
  _cardDragMark(target, _cardDrag.after);
}

function _cardDragEnd(ev, handle) {
  const d = _cardDrag;
  _cardDrag = null;
  handle.onpointermove = handle.onpointerup = handle.onpointercancel = null;
  _cardDragMark(null);
  if (!d) return;
  d.card.classList.remove('drag-src');
  if (ev.type !== 'pointerup' || !d.target) return;
  d.target.parentNode.insertBefore(d.card, d.after ? d.target.nextSibling : d.target);
  cardOrderSave();
}

// Send the order on screen to the add-on, and keep `cameras` in it, so the
// next renderGrid does not move the cards back.
async function cardOrderSave() {
  const grid = document.getElementById('cam-grid');
  const keys = [...grid.querySelectorAll('.camera-card')].map(c => c.dataset.stableKey).filter(Boolean);
  const rank = new Map(keys.map((k, i) => [k, i]));
  cameras.sort((a, b) => (rank.get(_stableCardKey(a)) ?? 1e9) - (rank.get(_stableCardKey(b)) ?? 1e9));
  try {
    const r = await fetch(BASE + '/api/card_order', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({order: keys}),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
  } catch (e) { showToast('The card order could not be saved', true); }
}

function buildCard(cam) {
  const d = document.createElement('div');
  d.className = 'camera-card' + (cam.verdict === 'uncertain' || cam.verdict === 'not_camera' ? ' uncertain' : '');
  d.dataset.id = cam.id;
  // 2.4.0-rc3.3: stable_key set on creation so the ID-change-preserving
  // matcher in renderGrid finds this card on next update.
  d.dataset.stableKey = _stableCardKey(cam);
  d.innerHTML = cardHTML(cam);
  return d;
}
function updateCard(cam) {
  const c = document.querySelector('[data-id="' + cam.id + '"]');
  if (c) {
    stopSnap(cam.id);   // stop any existing snap loop for this card
    c.innerHTML = cardHTML(cam);
    initSnaps();        // restart if card now has a data-snap image
  }
}

function dotClass(cam) {
  if (cam.upgrade_missing)                 return 'dot-upgrade';
  if (cam.status === 'ready')              return 'dot-ready';
  if (cam.status === 'info')               return 'dot-info';
  if (cam.verdict === 'uncertain' ||
      cam.verdict === 'not_camera')        return 'dot-uncertain';
  if (cam.status === 'needs_credentials')  return 'dot-warning';
  // 2.4.0-rc2.2 — authenticating_throttled is a transient state during
  // the rate-limited auth window (~30s on rate_limit_per_ip_tcp brands).
  // Previously fell through to 'dot-error' (red), which was misleading
  // since the camera is mid-authentication, not failed. Now renders as
  // yellow (warning) like other transient/informational states.
  if (cam.status === 'authenticating_throttled')  return 'dot-warning';
  return 'dot-error';
}

/* rc2.5: pick the most useful port to show in the card badge.
   cam.port is the camera's primary HTTP/identification port (set at ONVIF
   discovery, often 80) and may differ from where the actual feed comes
   from (RTSP on 554, custom HTTP-snap port, etc.). Prefer the port parsed
   out of stream_url when present so the badge reflects the feed source. */
function cardPort(cam) {
  const m = (cam.stream_url || '').match(/:\/\/[^\/]*?:(\d+)/);
  return m ? m[1] : cam.port;
}

function feedHTML(cam) {
  const d = cam.display || 'proxy';

  // Post-upgrade: camera was saved but not found after upgrade scan
  if (cam.upgrade_missing)
    return '<div class="feed-placeholder">'
         + '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">'
         + '<circle cx="12" cy="12" r="10"/><path d="M4.93 4.93l14.14 14.14"/></svg>'
         + '<span style="color:var(--orange)"><strong>Not found after upgrade</strong><br>'
         + '<small style="opacity:.75">Was present before upgrade but did not respond.<br>'
         + 'May be offline, removed, or a false positive from the previous version.</small></span></div>';

  if (d === 'proxy' && cam.status === 'ready')
    // Snapshot polling: JS calls /snapshot/{id}?t=... every 125ms via startSnap().
    // Each request is a normal short HTTP round-trip — nginx/ingress handles it
    // correctly unlike long-lived multipart streams which ingress terminates early.
    return '<img class="live" data-snap="' + esc(cam.id) + '" alt="Live" style="display:none">'
         + '<div class="feed-placeholder" id="ph-' + esc(cam.id) + '">'
         + '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">'
         + '<path d="M15 10l4.553-2.069A1 1 0 0121 8.87v6.26a1 1 0 01-1.447.9L15 14"/>'
         + '<rect x="1" y="7" width="14" height="10" rx="2" ry="2"/></svg>'
         + '<span>Loading feed, please wait…</span></div>';

  if (d === 'hls' && cam.status === 'ready')
    return '<video data-hls="' + esc(cam.stream_url) + '" autoplay muted playsinline></video>';

  if (d === 'webrtc')
    return '<div class="info-overlay"><div class="pi">🔗</div><strong>WebRTC Detected</strong>'
         + '<p>' + esc(cam.info || '') + '</p>'
         + '<a href="' + esc(cam.signaling_url || '#') + '" target="_blank">Open endpoint ↗</a></div>';

  if (d === 'wsrtsp')
    return '<div class="info-overlay"><div class="pi">🔌</div><strong>WS-RTSP Detected</strong>'
         + '<p>' + esc(cam.info || '') + '</p>'
         + '<code>' + esc(cam.ws_url || '') + '</code></div>';

  // 3.0.1 (B2): a camera built into an appliance, found by its MAC address
  if (d === 'appliance')
    return '<div class="info-overlay"><div class="pi">📷</div><strong>Camera in an appliance</strong>'
         + '<p>' + esc(cam.info || '') + '</p></div>';

  if (cam.verdict === 'uncertain' || cam.verdict === 'not_camera')
    return '<div class="feed-placeholder">'
         + '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">'
         + '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>'
         + '<line x1="12" y1="16" x2="12.01" y2="16"/></svg>'
         + '<span>Unverified device<br><small>' + esc(cam.verdict_reason || '') + '</small></span></div>';

  return '<div class="feed-placeholder">'
       + '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">'
       + '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>'
       + '<path d="M7 11V7a5 5 0 0110 0v4"/></svg>'
       + '<span>Credentials required</span></div>';
}

function identityHTML(cam) {
  const rows = [];
  // 2.6.6: protocol, IP and port moved here from the card's badges, and
  // always first (CrystalHeeler, 2026-09-30).
  if (cam.protocol) rows.push(['Protocol', ((PROTO_ICONS[cam.protocol] || '') + ' ' + cam.protocol).trim()]);
  if (cam.ip)       rows.push(['IP address', cam.ip]);
  const port = cardPort(cam);
  if (port)         rows.push(['Port', String(port)]);
  if (cam.manufacturer)   rows.push(['Manufacturer', cam.manufacturer]);
  if (cam.mac_addr) {
    const macLabel = cam.mac_addr + (cam.mac_vendor ? '  (' + cam.mac_vendor + ')' : '');
    rows.push(['MAC / OUI', macLabel]);
  }
  if (cam.page_title)     rows.push(['Page title',   cam.page_title]);
  if (cam.server_header)  rows.push(['Server',       cam.server_header]);
  // 2.4.0-rc1.0: RTSP-layer fingerprint fields, captured by the OPTIONS
  // pre-probe. Distinct from the HTTP-layer Server / Page title above.
  if (cam.rtsp_server_header) rows.push(['RTSP server', cam.rtsp_server_header]);
  if (cam.rtsp_auth_realm)    rows.push(['RTSP realm',  cam.rtsp_auth_realm]);
  if (cam.hostname && cam.hostname !== cam.ip) rows.push(['Hostname', cam.hostname]);
  if (cam.device_notes)   rows.push(['Notes',        cam.device_notes]);
  // Stream technical details (populated once credentials are accepted)
  if (cam.stream_codec) {
    const codec = cam.stream_codec.toUpperCase()
                + (cam.stream_profile ? ' (' + cam.stream_profile + ')' : '');
    rows.push(['Video codec', codec]);
  }
  if (cam.stream_width && cam.stream_height) {
    const res = cam.stream_width + 'x' + cam.stream_height
              + (cam.stream_fps ? '  @  ' + cam.stream_fps + ' fps' : '');
    rows.push(['Resolution', res]);
  }
  if (cam.stream_audio)   rows.push(['Audio codec',  cam.stream_audio.toUpperCase()]);
  if (!rows.length) rows.push(['Identity', 'Nothing known yet']);
  // 2.6.6: opened and closed by the info icon after the name (toggleIdentity),
  // in the same place the Identity box used to open. _idOpen keeps it open
  // across card redraws.
  return '<div class="id-section" data-idp="' + esc(cam.id) + '"'
    + (_idOpen[cam.id] ? '' : ' style="display:none"') + '><table class="id-table">'
    + rows.map(([k,v]) => '<tr><td class="id-key">' + esc(k) + '</td><td>' + esc(v) + '</td></tr>').join('')
    + '</table></div>';
}

const _idOpen = {};   // camId -> true while its Identity panel is open

function toggleIdentity(camId) {
  _idOpen[camId] = !_idOpen[camId];
  const open = !!_idOpen[camId];
  const panel = document.querySelector('[data-idp="' + CSS.escape(camId) + '"]');
  const btn   = document.querySelector('[data-idbtn="' + CSS.escape(camId) + '"]');
  if (panel) panel.style.display = open ? '' : 'none';
  if (btn) {
    btn.classList.toggle('open', open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
}

function credFormHTML(cam) {
  if (cam.status !== 'needs_credentials' || ['webrtc','wsrtsp'].includes(cam.display)) return '';
  return '<div class="cred-form">'
    + '<label>USERNAME</label>'
    + '<input type="text" id="u_' + cam.id + '" placeholder="admin" autocomplete="username">'
    + '<label>PASSWORD</label>'
    + '<input type="password" id="p_' + cam.id + '" placeholder="••••••••"'
    + ' autocomplete="current-password"'
    + ' onkeydown="if(event.key===\'Enter\')submitCreds(' + jsArg(cam.id) + ')">'
    + '<div class="cred-error" id="err_' + cam.id + '"></div>'
    + '<div class="cred-row">'
    + '<button class="btn btn-primary btn-sm" onclick="submitCreds(' + jsArg(cam.id) + ')">Connect</button>'
    + '</div></div>';
}

function cardActions(cam, clearBtn, notCamBtn) {
  if (cam.upgrade_missing) {
    return '<button class="btn btn-ghost btn-sm" onclick="confirmCamera(' + jsArg(cam.id) + ')">Keep (may be offline)</button>'
         + '<button class="btn btn-danger btn-sm" onclick="deleteCamera(' + jsArg(cam.id) + ')">Remove</button>';
  }
  const motOn = !!_motionEnabled[cam.id];
  const recOn = !!_recording[cam.id];
  const recBtn = (cam.status === 'ready' && ['proxy'].includes(cam.display || 'proxy'))
    ? '<button class="btn btn-sm ' + (recOn ? 'btn-rec-active' : (motOn ? 'btn-rec-on' : 'btn-rec-off'))
      + '" onclick="toggleMotion(' + jsArg(cam.id) + ')" title="' + (motOn ? 'Motion recording on' : 'Enable motion recording') + '">'
      + (recOn ? '⏺ REC' : (motOn ? '⏺ Armed' : '⏺ Record')) + '</button>'
    : '';
  // Globe button: opens camera web page (via Firefox addon or new tab)
  const webBtn = (cam.status === 'ready' && cam.ip)
    ? '<button class="btn btn-ghost btn-sm" onclick="openCameraPage(' + jsArg(cam.ip) + ')" title="Open camera web page">🌐</button>'
    : '';
  // 2.4.0-rc2.4: Deep Re-Probe button. Available on any card where
  // we may have skipped paths during the original scan (Layer 1 early-
  // bail OR Layer 2 skip via brand flag) AND on needs_credentials
  // cards in general (user might want to re-probe after camera reboot
  // or firmware change). Highlighted with yellow accent + extended
  // label when early_bail_reason is set, indicating we know we
  // skipped some scanning for this specific card.
  // 2.4.0-rc2.6: loud in-progress styling + min-width to prevent
  // button reflow. The rc2.5 version used `btn-ghost btn-sm` for
  // the in-progress state, which rendered as nearly-invisible faint
  // text against the dark card. The button label "Deep Re-Probe
  // (skipped paths)" is also significantly wider than "Deep Re-
  // Probe" alone, so the row reflowed when state changed. Now: all
  // three states use the SAME button width (min-width 200px), and
  // in-progress gets the same yellow accent + spinner emoji as the
  // skipped-paths state for high visibility.
  // 2.4.0-rc2.8: button is visible IFF cam.early_bail_reason is set —
  // i.e. there are actually skipped paths to resume. After Deep Re-
  // Probe completes the backend clears early_bail_reason (api_deep_
  // reprobe at line ~9189), so the button vanishes once the work is
  // done. Cards that never had skipped paths (e.g. Lorex/Dahua DVR-
  // family which has skip_layer2: True and walks Layer 1 cleanly) get
  // no button at all — there's nothing for it to do. The previous
  // rc2.6 design had a "subdued ghost" state for these cards which
  // was misleading: clicking it would launch a "fresh full probe"
  // that had no extra capability beyond what the original scan did,
  // so the user always got "no streams found" with no actionable
  // next step. The in-progress state also requires early_bail_reason
  // to remain visible — without that, the moment Deep Re-Probe
  // completes and clears the flag, the button disappears.
  let reprobeBtn = '';
  const _reprobeStyle = 'min-width:200px;text-align:center;';
  const _showReprobe = (cam.status === 'needs_credentials' || cam.verdict === 'not_camera')
                       && (cam.deep_reprobe_in_progress || cam.early_bail_reason);
  if (_showReprobe) {
    if (cam.deep_reprobe_in_progress) {
      reprobeBtn = '<button class="btn btn-sm" disabled '
        + 'style="' + _reprobeStyle
        + 'background:#3a2e1e;color:#f5b942;border:1px solid #f5b942;opacity:0.85" '
        + 'title="Deep re-probe in progress — walking the unwalked paths">'
        + '⏳ Deep Re-Probe (running)</button>';
    } else {
      reprobeBtn = '<button class="btn btn-sm" '
        + 'style="' + _reprobeStyle
        + 'background:#3a2e1e;color:#f5b942;border:1px solid #f5b942" '
        + 'onclick="deepReprobe(' + jsArg(cam.id) + ')" '
        + 'title="Resume scan from where rc2.4 fast-skipped — walks the unwalked paths">'
        + '🔍 Deep Re-Probe (skipped paths)</button>';
    }
  }
  return clearBtn + reprobeBtn + notCamBtn + recBtn + webBtn
       + '<button class="btn btn-danger btn-sm" onclick="deleteCamera(' + jsArg(cam.id) + ')">Remove</button>';
}

// 2.4.0-rc2.4: Deep Re-Probe handler. Posts to api_deep_reprobe and
// progressively updates UI status. Backend handler is synchronous over
// HTTP — total wait is whatever Layer 1 resume + Layer 2 walk take
// (5-60s typical). We update the button to a spinner during the call,
// then refresh the camera list so the result renders.
async function deepReprobe(cid) {
  const cam = (cameras || []).find(c => c.id === cid);
  // Build a status-line message; keep the user informed about which
  // stages are running.
  const reasonText = cam && cam.early_bail_reason
    ? ' (resuming from skipped paths)' : '';
  showToast('Deep Re-Probe started' + reasonText + '…');
  // Optimistically flip the in-progress flag so the button disables
  if (cam) { cam.deep_reprobe_in_progress = true; renderGrid(); }
  try {
    const r = await fetch(BASE + '/api/cameras/' + cid + '/deep_reprobe',
                         { method: 'POST' });
    const d = await r.json();
    if (!r.ok) {
      showToast('Deep Re-Probe failed: ' + (d.error || r.statusText), true);
      return;
    }
    let msg = 'Deep Re-Probe complete: ';
    if (d.found_stream) msg += '✅ working stream found';
    else if (d.locked_count > 0) msg += '🔒 ' + d.locked_count + ' locked stream(s) found';
    else msg += 'no streams found';
    showToast(msg);
  } catch (e) {
    showToast('Deep Re-Probe error: ' + e, true);
  } finally {
    // Force refresh from server so cam.* fields (early_bail_reason,
    // locked_streams, status, deep_reprobe_attempts) reflect the new
    // backend state.
    try { await loadCameras(); } catch (_) {}
  }
}
/* rc2.1: generic-name detector. ONVIF often returns boilerplate names
   like "IPCAM" or "Network Camera" instead of a real model. When the
   camera record has a manufacturer (typically set from MAC OUI lookup),
   prefer that over the generic ONVIF default. Regex anchors the entire
   string so user-given names that happen to contain "Camera" (e.g.
   "Front Porch Camera") are NOT treated as generic. */
function _isGenericCamName(s) {
  if (!s) return true;
  // 2.4.0-rc2.1: added "general" — observed on Lorex/Dahua DVRs which
  // report ONVIF Name="General" by default. Without this, the card
  // displayed "General" (the generic ONVIF name) instead of the
  // brand-identified manufacturer ("Lorex / Dahua DVR-NVR Family").
  // "generic" included for parity (similar product lines).
  if (/^(ip\s*cam(era)?|network\s*camera|camera|onvif[\s_-]*(device|camera)?|webcam|video\s*server|general|generic)$/i.test(s.trim()))
    return true;

  // 2.4.0-rc2.3: also treat auto-discovered hostnames as generic so
  // displayName falls through to the identified manufacturer instead
  // of the hostname. This fixes a long-latent bug surfaced by rc2.2's
  // faster scan: prev_name defaults to hostname when no ONVIF name is
  // available, and hostnames like "D861A8.lan" or
  // "tplink.my.house" or just an IP weren't recognized as generic.
  // Patterns covered:
  //   • Bare IPv4 (e.g. "10.0.0.13")
  //   • Reverse-DNS / ISP-provided FQDNs (e.g. "*.attlocal.net",
  //     "*.lan", "*.local", "*.home", "*.localdomain", and any user-
  //     provided local DNS suffix that the user hasn't customized
  //     per-camera). Heuristic: anything containing a dot AND looking
  //     like a domain — bare DNS labels with no user formatting.
  //   • MAC-OUI / serial-derived hostnames (uppercase hex chunks like
  //     "D861A8", "00:00:5E:00:53:09", "SN0123456789-ABCDEF012345")
  //   • mDNS-style "*.my.house" / "*.<userdomain>" auto-publish names
  // User-given names like "Front Door Camera" or "Driveway" still fail
  // the regex and remain user-displayed.
  const t = s.trim();
  // Bare IPv4
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(t)) return true;
  // Serial-style hostname WITHOUT a dot (e.g. "SN0123456789-ABCDEF012345"
  // — printer/IoT devices that publish their MAC- or serial-derived
  // hostname directly without a domain). Heuristic: 6+ uppercase
  // alphanumerics, optionally hyphenated into multiple all-caps blocks.
  // User-given names (mixed case "Front Door") fail this anchor.
  if (/^[A-Z0-9]{6,}(-[A-Z0-9]+)+$/.test(t)) return true;
  // Bare hostname-as-FQDN: contains a dot AND the leftmost label looks
  // device-derived (all caps + hex/digits, or has multiple hex segments
  // separated by hyphens). User-given short names rarely look like this.
  if (t.indexOf('.') >= 0) {
    const left = t.split('.')[0];
    // Pure hex or alphanum-uppercase first label (e.g. "D861A8",
    // "SN0123456789-ABCDEF012345", "tplink", "homeassistant"-NO that's
    // mixed case - we explicitly want "device-derived" patterns)
    if (/^[A-F0-9]{4,}$/.test(left)) return true;             // pure hex
    if (/^[A-Z0-9]{6,}(-[A-Z0-9]+)*$/.test(left)) return true; // serial-style
    // Common reverse-DNS suffixes — names ending in these are
    // auto-derived, not user-named
    if (/\.(local|lan|home|localdomain|attlocal\.net|hsd1\.[a-z]+\.comcast\.net|fios-router\.home)$/i.test(t))
      return true;
    // mDNS/local broadcast pattern: any FQDN with 3+ labels where the
    // leftmost label is all-lowercase short device-name. This catches
    // "tplink.my.house", "homeassistant.local", etc. Conservative —
    // requires the host part to be a single short lowercase token.
    if (/^[a-z][a-z0-9]{2,15}\.[a-z0-9]+(\.[a-z0-9]+)+$/i.test(t))
      return true;
  }
  return false;
}

function displayName(cam) {
  if (cam.manufacturer && _isGenericCamName(cam.name || '')) {
    return cam.manufacturer;
  }
  return cam.name || cam.hostname || cam.ip;
}

function cardHTML(cam) {
  const name     = esc(displayName(cam));
  // Lock badge: yellow key when creds not stored, green key when stored.
  // Only shown for cameras that actually involve credentials — pure-public
  // streams (e.g. open MJPEG with no auth) get no badge.
  const _showLock = cam.has_credentials || cam.requires_credentials || cam.status === 'needs_credentials';
  const _keyFill  = cam.has_credentials ? '#3B6D11' : '#F2BD2A';
  const _keyStrk  = cam.has_credentials ? '#173404' : '#8B6F00';
  const _lockTtl  = cam.has_credentials ? 'Credentials stored' : 'Credentials required';
  const credBdg   = _showLock
    ? '<span class="badge lock-badge" title="' + _lockTtl + '">'
      + '<svg width="20" height="20" viewBox="0 0 24 24">'
      +   '<path d="M5 9V6a3 3 0 0 1 6 0v3" fill="none" stroke="#8B6F00" stroke-width="2" stroke-linecap="round"/>'
      +   '<rect x="2" y="9" width="12" height="10" rx="2" fill="#F2BD2A" stroke="#8B6F00" stroke-width="0.6"/>'
      +   '<circle cx="8" cy="13" r="1.1" fill="#5C4400"/>'
      +   '<rect x="7.4" y="13" width="1.2" height="3" fill="#5C4400"/>'
      +   '<circle cx="14.5" cy="7" r="2.85" fill="' + _keyFill + '" stroke="' + _keyStrk + '" stroke-width="0.7"/>'
      +   '<circle cx="14.5" cy="7" r="1.2" fill="#1a1a1a"/>'
      +   '<rect x="13.75" y="9.85" width="1.5" height="6.3" fill="' + _keyFill + '" stroke="' + _keyStrk + '" stroke-width="0.5"/>'
      +   '<rect x="15.25" y="13.5" width="2.5" height="1.05" fill="' + _keyFill + '" stroke="' + _keyStrk + '" stroke-width="0.3"/>'
      +   '<rect x="15.25" y="15" width="1.8" height="0.75" fill="' + _keyFill + '" stroke="' + _keyStrk + '" stroke-width="0.3"/>'
      + '</svg>'
      + '</span>'
    : '';
  const uncBdg   = (cam.verdict === 'uncertain' || cam.verdict === 'not_camera')
    ? '<span class="badge" style="background:#3a2e1e;color:#f5b942" title="'
      + esc(cam.verdict_reason || '') + '">⚠ Unverified</span>' : '';
  const clearBtn = cam.has_credentials
    ? '<button class="btn btn-ghost btn-sm" onclick="clearCreds(' + jsArg(cam.id) + ')">Clear Creds</button>' : '';
  const notCamBtn =
    '<button class="btn btn-ghost btn-sm" onclick="markNotCamera(' + jsArg(cam.id) + ')"'
    + ' title="Permanently hide — not a camera">🚫 Not a Camera</button>';
  const upgradeBdg = cam.upgrade_missing
    ? '<span class="badge" style="background:#3a2a10;color:var(--orange)">⚠ Not found after upgrade</span>' : '';
  const hevcPlusBdg = cam.hevc_plus_warning && !cam.has_sub_stream
    ? '<span class="badge" style="background:#3a1a1a;color:#ff7070" title="Camera streams H.265+ (Hikvision proprietary). Fix: camera web UI → Video → Encoding → change H.265+ to H.265">⚠ H.265+</span>'
    : cam.hevc_plus_fallback_active || (cam.hevc_plus_warning && cam.has_sub_stream)
    ? '<span class="badge" style="background:#1a3a1a;color:#6fcf97" title="H.265+ detected — switched to compatible sub-stream automatically">✓ H.265+ fallback</span>'
    : '';
  // 2.4.0-rc2.0 (Layered Stream Discovery): show "View Locked Streams (N)"
  // badge when the path-walker found additional 401-locked paths AND the
  // camera doesn't have stored credentials yet. Once credentials are
  // accepted, the badge disappears (the cred-auth flow handles those
  // streams, so surfacing them again would be confusing). Click opens
  // a modal listing the paths + realm with a credential entry prompt.
  const _locked = Array.isArray(cam.locked_streams) ? cam.locked_streams : [];
  const lockedBdg = (_locked.length > 0 && !cam.has_credentials)
    ? '<span class="badge locked-streams-badge" title="' + _locked.length
      + ' additional stream(s) found that require credentials"'
      + ' onclick="event.stopPropagation();openLockedStreams(' + jsArg(cam.id) + ')"'
      + ' style="background:#2d2640;color:#b39ddb;cursor:pointer">'
      + '🔒 ' + _locked.length + ' Locked Stream' + (_locked.length === 1 ? '' : 's')
      + '</span>'
    : '';

  return '<div class="feed-wrap">' + feedHTML(cam) + '</div>'
    + '<div class="card-info">'
    // 3.1.0 (D3): drag here to move the card
    + '<span class="card-drag" title="Drag to move this card" aria-label="Move card"'
    + ' onpointerdown="cardDragStart(event,this)">⠿</span>'
    + '<div class="status-dot ' + dotClass(cam) + '"></div>'
    + '<span class="card-name" title="' + name + '"'
    + ' onclick="openRename(' + jsArg(cam.id) + ',' + jsArg(displayName(cam)) + ')">'
    + name + '</span>'
    // 2.6.6: Identity opens from an info icon right after the name.
    + '<button class="card-info-btn' + (_idOpen[cam.id] ? ' open' : '') + '" title="Identity"'
    + ' aria-label="Identity" aria-expanded="' + (_idOpen[cam.id] ? 'true' : 'false') + '"'
    + ' data-idbtn="' + esc(cam.id) + '"'
    + ' onclick="event.stopPropagation();toggleIdentity(' + jsArg(cam.id) + ')">' + INFO_SVG + '</button>'
    // 2.6.6: per-camera settings (motion and recording), top right of the
    // card's lower half.
    + (cam.status === 'ready'
        ? '<button class="card-cog" title="Camera settings" aria-label="Camera settings"'
          + ' onclick="event.stopPropagation();openCamSettings(' + jsArg(cam.id) + ')">' + COG_SVG + '</button>'
        : '')
    + '</div>'
    + ((uncBdg + upgradeBdg + hevcPlusBdg + lockedBdg)
        ? '<div class="badges">' + uncBdg + upgradeBdg + hevcPlusBdg + lockedBdg + '</div>'
        : '')
    + identityHTML(cam)
    + credFormHTML(cam)
    // 2.6.6: the lock sits at the right end of the button row, the card's
    // bottom-right corner.
    + '<div class="card-actions">' + cardActions(cam, clearBtn, notCamBtn)
    + (credBdg ? '<span class="card-lock">' + credBdg + '</span>' : '')
    + '</div>';
}
/* ── HLS init ──────────────────────────────────────────────────────────────── */
function initHls(video) {
  video._hls = true;
  const src = video.dataset.hls;
  if (!src) return;
  if (Hls.isSupported()) {
    const h = new Hls();
    h.loadSource(src);
    h.attachMedia(video);
    h.on(Hls.Events.ERROR, (_, d) => { if (d.fatal) video.style.display = 'none'; });
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = src;
  }
}

/* ── Credentials ───────────────────────────────────────────────────────────── */
async function submitCreds(cid) {
  const u = document.getElementById('u_' + cid)?.value.trim() || '';
  const p = document.getElementById('p_' + cid)?.value || '';
  const e = document.getElementById('err_' + cid);
  /* 2.3.0: rate_limit_per_ip_tcp brands need single-socket validation +
     paced ffprobe, which makes cred-auth take ~25–30s. Tell the user
     that's expected so they don't think the UI is hung. */
  const cam = (typeof cameras !== 'undefined') ?
    cameras.find(c => c.id === cid) : null;
  const _throttledBrand = cam && (
    /^Hipcam|^Microseven|^Sricam|^Vstarcam|^Wansview|^Tenvis/i.test(cam.manufacturer || ''));
  e.textContent = _throttledBrand
    ? 'Authenticating (Camera rate-limited, ~30 seconds)…'
    : 'Verifying…';
  e.classList.add('visible');
  try {
    const r = await fetch(BASE + '/api/credentials', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({camera_id: cid, username: u, password: p})
    });
    const d = await r.json();
    if (r.ok) {
      e.classList.remove('visible');
      await loadCameras();
      // 2.5.0-rc1.6: channel-iterate brands (Lorex/Dahua DVR-NVR
      // family) spawn a background channel enumeration after cred-auth
      // returns 200. The first loadCameras() above runs before that
      // enumeration completes and sees only the parent card.
      //
      // 2.5.0-rc1.8: replaces the previous fixed setTimeout(loadCameras,
      // 8000) reload with a poll-until-done loop against the new
      // /api/dvr_enum/status/{camera_id} endpoint. The old fixed budget
      // had to assume worst-case wallclock (15 channels at ~300ms per
      // walker call plus politeness sleeps and snap_loop kickoffs);
      // the rc1.7 test log on CrystalHeeler's 7-channel Lorex showed
      // enumeration actually completing in ~3s, so the old fixed wait
      // cost ~5s of dead time before cards appeared. This poll wakes
      // the moment the backend signals done.
      //
      // Hard-cap at 12s (24 polls @ 500ms = +50% headroom over the old
      // fixed budget). If somehow done never flips true (backend bug
      // or unforeseen exception that bypasses the rc1.8 hardening),
      // the cap-fall-through calls loadCameras() anyway so the worst
      // case matches today's behavior — no regression.
      if (d.dvr_enumeration_pending) {
        let polls = 0;
        const MAX_POLLS = 24;        // 24 * 500ms = 12s hard cap
        const POLL_INTERVAL_MS = 500;
        const pollUrl = BASE + '/api/dvr_enum/status/' +
                        encodeURIComponent(cid);
        const tick = async () => {
          polls += 1;
          let done = false;
          try {
            const sr = await fetch(pollUrl);
            if (sr.ok) {
              const sd = await sr.json();
              done = !!sd.done;
            }
          } catch { /* network blip — keep polling until cap */ }
          if (done) {
            await loadCameras();
            return;
          }
          if (polls >= MAX_POLLS) {
            // Fall back to behaviour matching 2.5.0-rc1.7's fixed
            // setTimeout — refetch anyway so cards eventually surface
            // even on backend failure.
            await loadCameras();
            return;
          }
          setTimeout(tick, POLL_INTERVAL_MS);
        };
        setTimeout(tick, POLL_INTERVAL_MS);
      }
    }
    else e.textContent = d.error || 'Connection failed.';
  } catch { e.textContent = 'Network error.'; }
}

async function clearCreds(cid) {
  if (!confirm('Clear stored credentials for this camera?')) return;
  await fetch(BASE + '/api/cameras/' + cid + '/credentials', {method: 'DELETE'});
  await loadCameras();
}

async function deleteCamera(cid) {
  if (!confirm('Remove this camera?')) return;
  await fetch(BASE + '/api/cameras/' + cid, {method: 'DELETE'});
  cameras = cameras.filter(c => c.id !== cid);
  renderGrid();
}

async function confirmCamera(cid) {
  // User acknowledges camera may just be offline — clear the upgrade_missing flag
  await fetch(BASE + '/api/cameras/' + cid + '/confirm', {method: 'POST'});
  await loadCameras();
}

/* ── Not a Camera modal ────────────────────────────────────────────────────── */
let _notCamId = null;
const COMMUNITY_ENDPOINT = ___COMMUNITY___;   // 2.6.6 (B5): filled by build_html

function openNotCamModal(cid) {
  _notCamId = cid;
  const cam = cameras.find(c => c.id === cid);
  const label = cam ? (cam.manufacturer || cam.name || cam.ip) : cid;
  document.getElementById('nc-device-label').textContent = label;
  document.querySelectorAll('.nc-reason-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('nc-detail').value = '';
  document.getElementById('nc-share').checked = !!COMMUNITY_ENDPOINT;
  document.getElementById('nc-share-row').style.display = COMMUNITY_ENDPOINT ? '' : 'none';
  document.getElementById('nc-modal').classList.add('open');
  setTimeout(() => document.querySelector('.nc-reason-btn[data-reason="unknown"]')?.focus(), 50);
}

function closeNotCamModal() {
  document.getElementById('nc-modal').classList.remove('open');
  _notCamId = null;
}

function selectReason(btn) {
  document.querySelectorAll('.nc-reason-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

async function submitNotCam() {
  if (!_notCamId) { closeNotCamModal(); return; }
  const activeBtn   = document.querySelector('.nc-reason-btn.active');
  const reasonType  = activeBtn ? activeBtn.dataset.reason : 'unknown';
  const reasonDetail = document.getElementById('nc-detail').value.trim();
  const share       = document.getElementById('nc-share').checked;
  await fetch(BASE + '/api/cameras/' + _notCamId + '/not_camera', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({reason_type: reasonType, reason_detail: reasonDetail, share})
  });
  cameras = cameras.filter(c => c.id !== _notCamId);
  closeNotCamModal();
  renderGrid();
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('nc-modal').classList.contains('open'))
    closeNotCamModal();
});

async function markNotCamera(cid) { openNotCamModal(cid); }

/* ── 2.4.0-rc2.0 Layered Stream Discovery: Locked Streams modal ─────────── */
let _lockedCid = null;

function openLockedStreams(cid) {
  _lockedCid = cid;
  const cam = cameras.find(c => c.id === cid);
  if (!cam) return;
  const label = displayName(cam);
  const locked = Array.isArray(cam.locked_streams) ? cam.locked_streams : [];
  document.getElementById('locked-device-label').textContent = label;
  document.getElementById('locked-count').textContent = locked.length;
  const list = document.getElementById('locked-list');
  list.innerHTML = locked.map(ls => {
    const path = esc(ls.path || '?');
    const realm = ls.realm ? ' (realm: ' + esc(ls.realm) + ')' : '';
    const scheme = ls.scheme ? ' [' + esc(ls.scheme) + ']' : '';
    return '<div class="locked-row"><code>' + path + '</code>'
         + '<span class="locked-meta">' + scheme + realm + '</span></div>';
  }).join('');
  document.getElementById('locked-user').value = '';
  document.getElementById('locked-pass').value = '';
  document.getElementById('locked-err').textContent = '';
  document.getElementById('locked-err').classList.remove('visible');
  document.getElementById('locked-modal').classList.add('open');
  setTimeout(() => document.getElementById('locked-user').focus(), 50);
}

function closeLockedStreams() {
  document.getElementById('locked-modal').classList.remove('open');
  _lockedCid = null;
}

async function submitLockedCreds() {
  if (!_lockedCid) { closeLockedStreams(); return; }
  const u = document.getElementById('locked-user').value.trim();
  const p = document.getElementById('locked-pass').value;
  const e = document.getElementById('locked-err');
  if (!u || !p) {
    e.textContent = 'Username and password are required.';
    e.classList.add('visible');
    return;
  }
  e.textContent = 'Authenticating…';
  e.classList.add('visible');
  try {
    /* Reuses the existing cred-auth endpoint. The server-side flow runs
       _identity-preserving cred-auth which retries the path walker WITH
       credentials supplied, so any same-realm 401-locked paths get
       authenticated automatically. After success, the camera record is
       rebuilt with stream_url(s) populated and the locked_streams list
       remains for diagnostic display (badge hides because user_saved). */
    const r = await fetch(BASE + '/api/credentials', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      /* 2.6.0-rc3.0 Item 4: tag this POST as coming from the Locked
         Streams modal so the backend knows it's safe to clear
         camera.locked_streams after auth (the user walked through
         the badge flow on purpose). The regular Login button at
         submitCreds() leaves this flag absent (defaults to false on
         the backend), which preserves the badge. */
      body: JSON.stringify({camera_id: _lockedCid, username: u, password: p,
                            from_locked_streams_modal: true})
    });
    const d = await r.json();
    if (r.ok) {
      e.classList.remove('visible');
      closeLockedStreams();
      await loadCameras();
    } else {
      e.textContent = d.error || 'Authentication failed.';
    }
  } catch {
    e.textContent = 'Network error.';
  }
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('locked-modal').classList.contains('open'))
    closeLockedStreams();
});

/* ── Rename ────────────────────────────────────────────────────────────────── */
function openRename(cid, name) {
  renameId = cid;
  document.getElementById('rename-input').value = name;
  document.getElementById('rename-modal').classList.add('open');
  setTimeout(() => document.getElementById('rename-input').focus(), 50);
}
function closeRename() {
  document.getElementById('rename-modal').classList.remove('open');
  renameId = null;
}
async function submitRename() {
  const name = document.getElementById('rename-input').value.trim();
  if (!name || !renameId) { closeRename(); return; }
  await fetch(BASE + '/api/cameras/' + renameId + '/name', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name})
  });
  closeRename();
  await loadCameras();
}
document.getElementById('rename-modal').addEventListener('click',
  e => { if (e.target.id === 'rename-modal') closeRename(); });
document.getElementById('rename-input').addEventListener('keydown',
  e => { if (e.key === 'Enter') submitRename(); if (e.key === 'Escape') closeRename(); });

/* ── Port scanner ──────────────────────────────────────────────────────────── */
let _arpHosts = [];
let _selectedIPs = new Set();   // persists across view switches

async function loadArpHosts() {
  try {
    _arpHosts = await (await fetch(BASE + '/api/arp_hosts')).json();
    renderArpList();
  } catch(e) {
    console.warn('ARP hosts not available yet');
  }
}

function renderArpList() {
  const wrap = document.getElementById('arp-host-list');
  if (!_arpHosts.length) {
    wrap.innerHTML = '<span class="arp-empty">No hosts discovered yet — run a network scan first.</span>';
    return;
  }
  wrap.innerHTML = '<div class="arp-select-row">'
    + '<button class="btn btn-ghost btn-sm" onclick="arpSelectAll(true)">Select all</button>'
    + '<button class="btn btn-ghost btn-sm" onclick="arpSelectAll(false)">Clear all</button>'
    + '<span style="color:var(--text-dim);font-size:.75rem;margin-left:6px">'
    + _arpHosts.length + ' host' + (_arpHosts.length !== 1 ? 's' : '') + ' discovered</span>'
    + '</div>'
    + _arpHosts.map(h => {
        const checked = _selectedIPs.has(h.ip) ? ' checked' : '';
        return '<label class="arp-row">'
          + '<input type="checkbox" class="arp-cb" value="' + h.ip + '"'
          + checked + ' onchange="onCbChange(this)"> '
          + '<span class="arp-ip">' + h.ip + '</span>'
          + (h.hostname && h.hostname !== h.ip
              ? '<span class="arp-host"> — ' + esc(h.hostname) + '</span>' : '')
          + '</label>';
      }).join('');
}

function onCbChange(cb) {
  if (cb.checked) _selectedIPs.add(cb.value);
  else            _selectedIPs.delete(cb.value);
}

function arpSelectAll(val) {
  document.querySelectorAll('.arp-cb').forEach(cb => {
    cb.checked = val;
    if (val) _selectedIPs.add(cb.value);
    else     _selectedIPs.delete(cb.value);
  });
}

function getSelectedIPs() {
  const manual = document.getElementById('pscan-ip').value.trim();
  const combined = [...new Set([..._selectedIPs, ...(manual ? [manual] : [])])];
  return combined;
}

async function startPortScan() {
  const ips = getSelectedIPs();
  if (!ips.length) { alert('Select at least one host or enter an IP address.'); return; }

  const body = ips.length === 1 ? {ip: ips[0]} : {ips};
  const r = await fetch(BASE + '/api/pscan/start', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  if (!r.ok) { const d = await r.json(); alert(d.error || 'Scan failed'); return; }
  document.getElementById('ps-start').disabled = true;
  document.getElementById('ps-pause').style.display  = '';
  document.getElementById('ps-cancel').style.display = '';
  document.getElementById('ps-prog-track').style.display = '';
  document.getElementById('port-table').style.display = 'none';
  document.getElementById('port-tbody').innerHTML = '';
  document.getElementById('live-ports-box').innerHTML = '';
  document.getElementById('live-ports-box').style.display = 'none';
  _paused = false;
  pollPscan();
}

async function pollPscan() {
  clearTimeout(pscanT);
  try {
    const s = await (await fetch(BASE + '/api/pscan/status')).json();

    // Message + ETA
    const msgEl   = document.getElementById('pscan-msg');
    const timerEl = document.getElementById('ps-timer');
    msgEl.textContent = s.message;

    if (s.running && s.scan_start) {
      const clientElap = (Date.now()/1000) - s.scan_start;
      if (s.eta > 0) {
        const drift  = Math.max(0, clientElap - (s.elapsed || 0));
        const etaAdj = Math.max(0, Math.round(s.eta - drift));
        const etaM = Math.floor(etaAdj/60), etaSec = etaAdj % 60;
        timerEl.textContent = 'estimated ' + etaM + ':' + String(etaSec).padStart(2,'0') + ' remaining';
      } else {
        // No nmap ETA yet — show elapsed instead
        const m = Math.floor(clientElap/60), sec = Math.floor(clientElap) % 60;
        timerEl.textContent = m + ':' + String(sec).padStart(2,'0') + ' elapsed';
      }
      timerEl.style.display = '';
    } else if (!s.running && s.scan_start) {
      const elapsed = Math.round(s.elapsed || 0);
      const m = Math.floor(elapsed/60), sec = elapsed % 60;
      timerEl.textContent = 'Completed in ' + m + ':' + String(sec).padStart(2,'0');
      timerEl.style.display = '';
    } else {
      timerEl.style.display = 'none';
    }

    document.getElementById('ps-prog-fill').style.width = (s.progress || 0) + '%';

    // Live port discovery box (during scan)
    const liveBox = document.getElementById('live-ports-box');
    if (s.running && s.live_ports && s.live_ports.length) {
      liveBox.style.display = '';
      liveBox.innerHTML = s.live_ports.map(p =>
        '<div class="live-port-row">'
        + '<span class="pnum">' + p.port + '</span>'
        + '<span class="live-proto">/' + esc(p.proto) + '</span>'
        + '</div>'
      ).join('');
      liveBox.scrollTop = liveBox.scrollHeight;
    } else if (!s.running) {
      liveBox.style.display = 'none';
    }

    // Final results table (when done)
    if (!s.running && s.results && s.results.length) {
      renderPorts(s.results);
    }

    if (s.running) {
      pscanT = setTimeout(pollPscan, 2000);
    } else {
      document.getElementById('ps-start').disabled  = false;
      document.getElementById('ps-pause').style.display  = 'none';
      document.getElementById('ps-cancel').style.display = 'none';
    }
  } catch { pscanT = setTimeout(pollPscan, 3000); }
}

const CAM_PORTS   = new Set([554,8554,10554,1935,1936,2020,37777,34567,8765]);
const CAM_SERVICES = ['rtsp','onvif','rtmp','camera','ipcam','nvr','dvr','cctv',
                      'video server','webcam','dahua','hikvision','lorex','reolink',
                      'axis','amcrest','axis-cgi','mediamtx'];

function isCamPort(p) {
  if (CAM_PORTS.has(p.port)) return true;
  const combined = (p.service + ' ' + p.product).toLowerCase();
  return CAM_SERVICES.some(k => combined.includes(k));
}

function makePortRow(p, hasBatch) {
  const ver = [p.product, p.version, p.extra].filter(Boolean).join(' ');
  const scripts = Object.entries(p.scripts || {})
    .map(([k,v]) => '<b>' + esc(k) + '</b>: ' + esc(v)).join('\n');
  return '<tr>'
    + (hasBatch ? '<td class="arp-ip" style="white-space:nowrap">' + esc(p.scanned_ip || '') + '</td>' : '')
    + '<td class="pnum">' + p.port + '</td>'
    + '<td>' + esc(p.proto) + '</td>'
    + '<td class="psvc">' + esc(p.service) + '</td>'
    + '<td>' + esc(ver) + '</td>'
    + '<td>' + (scripts ? '<div class="pscripts">' + scripts + '</div>' : '—') + '</td>'
    + '</tr>';
}

function renderPorts(results) {
  if (!results.length) return;
  document.getElementById('port-table').style.display = '';
  const hasBatch = results.some(p => p.scanned_ip);
  const thead = document.querySelector('#port-table thead tr');
  if (hasBatch && !thead.querySelector('.batch-ip-col')) {
    const th = document.createElement('th');
    th.textContent = 'Host'; th.className = 'batch-ip-col';
    thead.insertBefore(th, thead.firstChild);
  } else if (!hasBatch) {
    const old = thead.querySelector('.batch-ip-col');
    if (old) old.remove();
  }

  const camPorts  = results.filter(isCamPort);
  const otherPorts = results.filter(p => !isCamPort(p));

  let html = '';
  if (camPorts.length) {
    html += '<tr class="section-header"><td colspan="99">&#x1F4F9; Likely camera-related ('
          + camPorts.length + ' port' + (camPorts.length !== 1 ? 's' : '') + ')</td></tr>';
    html += camPorts.map(p => makePortRow(p, hasBatch)).join('');
  }
  if (otherPorts.length) {
    html += '<tr class="section-header other-header" onclick="toggleOther(this)">'
          + '<td colspan="99">&#x25B6; Other ports ('
          + otherPorts.length + ') — click to show/hide</td></tr>';
    html += '<tbody class="other-ports" style="display:none">'
          + otherPorts.map(p => makePortRow(p, hasBatch)).join('')
          + '</tbody>';
  }
  document.getElementById('port-tbody').innerHTML = html;
}

function toggleOther(hdrRow) {
  const next = hdrRow.nextElementSibling;
  if (!next) return;
  const hidden = next.style.display === 'none';
  next.style.display = hidden ? '' : 'none';
  const arrow = hdrRow.querySelector('td');
  if (arrow) arrow.textContent = arrow.textContent.replace(hidden ? '▶' : '▼', hidden ? '▼' : '▶');
}

async function togglePause() {
  const btn = document.getElementById('ps-pause');
  if (!_paused) {
    await fetch(BASE + '/api/pscan/pause', {method: 'POST'});
    _paused = true; btn.textContent = '▶ Resume';
  } else {
    await fetch(BASE + '/api/pscan/resume', {method: 'POST'});
    _paused = false; btn.textContent = '⏸ Pause';
    pollPscan();
  }
}

async function cancelPortScan() {
  await fetch(BASE + '/api/pscan/cancel', {method: 'POST'});
  _paused = false;
  document.getElementById('ps-pause').textContent = '⏸ Pause';
  document.getElementById('ps-start').disabled = false;
  document.getElementById('ps-pause').style.display  = 'none';
  document.getElementById('ps-cancel').style.display = 'none';
}

/* ── Add camera ────────────────────────────────────────────────────────────── */
function onProtoChange() {
  const p = document.getElementById('add-proto').value;
  document.getElementById('add-port').value = DPORT[p] || 554;
  document.getElementById('path-field').style.display =
    ['RTSP','ONVIF','DVR'].includes(p) ? '' : 'none';
}

async function submitAddCamera() {
  const btn   = document.getElementById('add-btn2');
  const errEl = document.getElementById('add-error');
  errEl.style.display = 'none';
  btn.disabled = true; btn.textContent = 'Connecting…';
  try {
    const r = await fetch(BASE + '/api/cameras/add', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        name:      document.getElementById('add-name').value.trim(),
        ip:        document.getElementById('add-ip').value.trim(),
        port:      parseInt(document.getElementById('add-port').value) || 554,
        protocol:  document.getElementById('add-proto').value,
        rtsp_path: document.getElementById('add-path').value.trim(),
        username:  document.getElementById('add-user').value.trim(),
        password:  document.getElementById('add-pass').value,
      })
    });
    const d = await r.json();
    if (r.ok) { await loadCameras(); switchView('cameras'); }
    else { errEl.textContent = d.error || 'Connection failed.'; errEl.style.display = 'block'; }
  } catch { errEl.textContent = 'Network error.'; errEl.style.display = 'block'; }
  finally { btn.disabled = false; btn.textContent = 'Connect'; }
}

/* ── Helpers ───────────────────────────────────────────────────────────────── */
function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// 2.6.6 (B4): a value as a JavaScript string argument inside a double-quoted
// onclick attribute. JSON.stringify escapes quotes and backslashes for JS;
// esc() then protects the attribute (the browser turns &quot; back into "
// before the handler runs). Camera IDs can carry an ONVIF profile token and
// names are editable, so neither may be pasted between quotes raw.
function jsArg(s) {
  return esc(JSON.stringify(String(s)));
}

/* ── Init ──────────────────────────────────────────────────────────────────── */
(async () => {
  await loadCameras();
  const s = await (await fetch(BASE + '/api/scan/status')).json();
  if (s.running) {
    document.getElementById('scan-btn').disabled = true;
    document.getElementById('progress-track').style.display = '';
    document.getElementById('status-bar').classList.add('scanning');
    pollScan();
  }
})();
"""
