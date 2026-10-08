"""The page builder: the HTML page, its CSS and the index handler.

Moved out of camera_discovery.py in 3.0.0-rc1.5 (build plan E1); the function
bodies are unchanged.

This file cannot import camera_discovery.py (see anycam_host.py). The names in
NEEDS are set on this module at start-up; H reads a camera_discovery.py
value at the moment of use.
"""
import json
import logging
from aiohttp import web

from anycam_host import H
from page_script import (
    PAGE_SCRIPT as _JS,
)

log = logging.getLogger("anycam")

# Taken from camera_discovery.py at start-up (anycam_host.bind).
NEEDS = (
    'CFG_ADAPTIVE_QUALITY', 'CFG_UNRESTRICTED_BROWSER', 'COMMUNITY_ENDPOINT', 'INGRESS_PATH',
)

HTML = None   # built once on first request


def build_html() -> str:
    js_code = _JS.replace('___BASE___', INGRESS_PATH)
    js_code = js_code.replace('___UNRESTRICTED___',
                               'true' if CFG_UNRESTRICTED_BROWSER else 'false')
    js_code = js_code.replace('___ADAPTIVE_QUALITY___',
                               'true' if CFG_ADAPTIVE_QUALITY else 'false')
    # 2.6.6 (B5): never filled before, so the literal placeholder read as a
    # configured endpoint: "Share with community" showed ticked and did
    # nothing. A JSON string literal, with < escaped for the <script> block.
    js_code = js_code.replace('___COMMUNITY___',
                               json.dumps(COMMUNITY_ENDPOINT).replace('<', '\\u003c'))
    # CSS uses {{ }} for literal braces in Python f-string
    css = f"""\
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#111318;--surface:#1e2029;--surface2:#272a35;--border:#2e3140;
  --primary:#5b8af5;--primary-dim:#3a5dbf;--green:#4caf7d;--yellow:#f5b942;
  --red:#e05c5c;--blue:#5bc4f5;--orange:#f5944a;--text:#e4e6f0;--text-dim:#8a8fa8;
  --radius:12px;--card-w:320px;
}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;min-height:100vh;display:flex;flex-direction:column}}
header{{background:var(--surface);border-bottom:1px solid var(--border);
        padding:10px 18px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;
        position:sticky;top:0;z-index:10}}
header h1{{font-size:1rem;font-weight:700;display:flex;align-items:center;gap:8px;margin-right:auto}}
.btn{{padding:6px 14px;border-radius:8px;border:none;cursor:pointer;font-size:.8rem;
      font-weight:600;transition:opacity .15s,background .15s;white-space:nowrap}}
.btn:disabled{{opacity:.4;cursor:default}}
.btn-primary{{background:var(--primary);color:#fff}}
.btn-primary:not(:disabled):hover{{background:var(--primary-dim)}}
.btn-secondary{{background:var(--surface2);color:var(--text);border:1px solid var(--border)}}
.btn-secondary:hover,.btn-secondary.active{{background:var(--primary);color:#fff;border-color:var(--primary)}}
.btn-danger{{background:var(--red);color:#fff}}
.btn-ghost{{background:transparent;color:var(--text-dim);border:1px solid var(--border)}}
.btn-ghost:hover{{color:var(--text)}}
.btn-sm{{padding:4px 10px;font-size:.74rem}}
.sweep-toggle{{display:flex;align-items:center;gap:5px;font-size:.78rem;color:var(--text-dim);cursor:pointer;white-space:nowrap}}
.sweep-toggle input{{accent-color:var(--primary);cursor:pointer}}
#status-bar{{padding:6px 18px;font-size:.78rem;color:var(--text-dim);
             display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--border);min-height:30px}}
.stage-badge{{background:var(--primary);color:#fff;border-radius:4px;padding:1px 7px;
              font-size:.7rem;font-weight:700;white-space:nowrap}}
.progress-track{{flex:1;height:3px;background:var(--surface2);border-radius:2px;overflow:hidden;max-width:180px}}
.progress-fill{{height:100%;background:var(--primary);border-radius:2px;transition:width .4s ease;width:0%}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
.scanning .progress-fill{{animation:pulse 1.2s infinite}}
.view{{display:none;flex:1;overflow:auto}}
.view.active{{display:flex;flex-direction:column}}
#cam-grid{{padding:18px;display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--card-w),1fr));gap:16px;align-content:start;align-items:start}}
#empty-state{{grid-column:1/-1;text-align:center;padding:60px 20px;color:var(--text-dim)}}
#empty-state svg{{opacity:.2;display:block;margin:0 auto 14px}}
.camera-card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
              overflow:hidden;display:flex;flex-direction:column;transition:box-shadow .2s}}
.camera-card:hover{{box-shadow:0 4px 20px rgba(0,0,0,.5)}}
.camera-card.uncertain{{border-color:rgba(245,148,74,.4)}}
.feed-wrap{{position:relative;width:100%;aspect-ratio:16/9;background:#000;
            display:flex;align-items:center;justify-content:center;overflow:hidden}}
.feed-wrap img,.feed-wrap video{{width:100%;height:100%;object-fit:cover;display:block}}
/* 2.6.6 live cards: the player sits over the placeholder until it plays */
.feed-wrap anycam-video{{position:absolute;inset:0;display:block;transition:opacity .3s;cursor:pointer}}
/* 3.1.0 (C19): an MJPEG card's live picture, the same way */
.feed-wrap img.card-live{{position:absolute;inset:0;transition:opacity .3s;cursor:pointer}}
/* 3.1.0 (D3): drag to move a card */
.card-drag{{flex-shrink:0;cursor:grab;color:var(--text-dim);touch-action:none;user-select:none;
            padding:0 2px;line-height:1.1;font-size:1rem}}
.card-drag:hover{{color:var(--text)}}
.camera-card.drag-src{{opacity:.5}}
/* 3.7.0-rc2.0 (D4): a line between the cards where the dragged card lands */
#card-drop-line{{position:fixed;display:none;z-index:50;background:var(--primary);border-radius:2px;
                 pointer-events:none;box-shadow:0 0 6px var(--primary)}}
.feed-placeholder{{display:flex;flex-direction:column;align-items:center;gap:6px;
                   color:var(--text-dim);font-size:.78rem;text-align:center;padding:10px}}
.feed-placeholder svg{{opacity:.3}}
.info-overlay{{position:absolute;inset:0;background:rgba(10,12,18,.88);
               display:flex;flex-direction:column;align-items:center;
               justify-content:center;gap:8px;padding:14px;text-align:center}}
.info-overlay .pi{{font-size:1.8rem}}
.info-overlay p{{font-size:.75rem;color:var(--text-dim);line-height:1.4}}
.info-overlay a{{color:var(--primary);font-size:.76rem}}
.info-overlay code{{font-size:.68rem;color:var(--text-dim);word-break:break-all;background:var(--surface2);padding:3px 6px;border-radius:4px}}
.card-info{{padding:10px 12px 5px;display:flex;align-items:flex-start;gap:8px}}
.card-cog{{margin-left:auto;flex-shrink:0;background:transparent;border:none;color:var(--text-dim);cursor:pointer;padding:2px;line-height:0;border-radius:6px}}
.card-cog:hover{{color:var(--text);background:var(--surface2)}}
.card-name{{font-size:.86rem;font-weight:600;flex:0 1 auto;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}}
.card-info-btn{{flex-shrink:0;background:transparent;border:none;color:var(--text-dim);cursor:pointer;padding:1px;line-height:0;border-radius:50%;margin-top:1px}}
.card-info-btn:hover,.card-info-btn.open{{color:var(--primary)}}
.card-lock{{margin-left:auto;display:inline-flex;align-items:center}}
.card-name:hover{{color:var(--primary)}}
.badges{{padding:0 12px 8px;display:flex;flex-wrap:wrap;gap:4px}}
.badge{{font-size:.68rem;font-weight:600;padding:2px 6px;border-radius:20px}}
.lock-badge{{background:transparent !important;padding:0 !important;display:inline-flex;align-items:center;line-height:1}}
.status-dot{{width:8px;height:8px;border-radius:50%;flex-shrink:0;margin-top:5px}}
.dot-ready{{background:var(--green);box-shadow:0 0 5px var(--green)}}
.dot-warning{{background:var(--yellow);box-shadow:0 0 5px var(--yellow)}}
.dot-info{{background:var(--blue);box-shadow:0 0 5px var(--blue)}}
.dot-uncertain{{background:var(--orange);box-shadow:0 0 5px var(--orange)}}
.dot-error{{background:var(--red);box-shadow:0 0 5px var(--red)}}
.dot-upgrade{{background:var(--orange);box-shadow:0 0 8px var(--orange);animation:pulse 2s infinite}}
.scan-timer-badge{{font-size:.74rem;color:var(--primary);font-weight:700;white-space:nowrap}}
.arp-host-section{{padding:14px 18px 0;display:flex;flex-direction:column;gap:6px}}
.arp-section-label{{font-size:.75rem;font-weight:600;color:var(--text-dim);letter-spacing:.04em}}
.arp-host-list{{background:var(--surface);border:1px solid var(--border);border-radius:8px;
               padding:10px;display:flex;flex-direction:column;gap:3px;max-height:220px;overflow-y:auto}}
.arp-empty{{font-size:.78rem;color:var(--text-dim);padding:4px 0}}
.arp-select-row{{display:flex;align-items:center;gap:6px;padding-bottom:6px;
                border-bottom:1px solid var(--border);margin-bottom:4px}}
.arp-row{{display:flex;align-items:center;gap:8px;padding:3px 4px;border-radius:5px;
         cursor:pointer;font-size:.82rem}}
.arp-row:hover{{background:var(--surface2)}}
.arp-row input{{accent-color:var(--primary);cursor:pointer;flex-shrink:0}}
.arp-ip{{font-family:monospace;font-weight:600;color:var(--primary)}}
.arp-host{{color:var(--text-dim)}}
.id-section{{margin:0 12px 8px;border:1px solid var(--border);border-radius:8px;overflow:hidden}}
.id-table{{width:100%;border-collapse:collapse;font-size:.74rem}}
.id-table td{{padding:4px 10px;border-top:1px solid var(--border);vertical-align:top}}
.id-table tr:first-child td{{border-top:none}}
.id-key{{color:var(--text-dim);font-weight:600;white-space:nowrap;width:90px}}
.cred-form{{margin:0 12px 10px;background:var(--surface2);border:1px solid var(--border);
            border-radius:8px;padding:10px;display:flex;flex-direction:column;gap:6px}}
.cred-form label{{font-size:.7rem;color:var(--text-dim);font-weight:600;letter-spacing:.04em}}
.cred-form input{{width:100%;background:var(--bg);border:1px solid var(--border);
                  border-radius:6px;color:var(--text);font-size:.82rem;padding:5px 8px;outline:none}}
.cred-form input:focus{{border-color:var(--primary)}}
.cred-row{{display:flex;gap:5px}}
.cred-row .btn{{flex:1}}
.cred-error{{font-size:.71rem;color:var(--red);display:none}}
.cred-error.visible{{display:block}}
.card-actions{{padding:0 12px 10px;display:flex;gap:4px;flex-wrap:wrap;align-items:center}}
.card-actions .btn-sm{{padding:3px 7px;font-size:.72rem}}
#pscan-view{{padding:16px 18px;gap:12px}}
.pscan-header{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:4px}}
.pscan-header .pscan-ctl{{margin-left:auto}}
.pscan-ctl{{display:flex;gap:6px;flex-wrap:wrap}}
.pscan-top{{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end}}
.pscan-top input{{background:var(--surface);border:1px solid var(--border);color:var(--text);
                  border-radius:8px;padding:7px 12px;font-size:.88rem;outline:none;
                  flex:1;min-width:200px;max-width:400px}}
.pscan-top input:focus{{border-color:var(--primary)}}
#pscan-msg{{font-size:.78rem;color:var(--text-dim);padding:2px 0;line-height:1.5}}
.live-ports-box{{background:var(--surface);border:1px solid var(--border);
               border-radius:8px;padding:10px 12px;max-height:220px;overflow-y:auto;
               display:flex;flex-direction:column;gap:3px;margin-bottom:8px;
               font-family:monospace;font-size:.82rem}}
.live-port-row{{display:flex;align-items:center;gap:6px;padding:1px 0}}
.live-port-row .pnum{{color:var(--primary);font-weight:700;min-width:52px}}
.live-proto{{color:var(--text-dim)}}
.section-header td{{background:var(--surface2);font-size:.73rem;font-weight:700;
                     color:var(--text-dim);letter-spacing:.04em;padding:6px 10px;
                     border-bottom:1px solid var(--border)}}
.other-header{{cursor:pointer}}
.other-header:hover td{{background:var(--border)}}
.port-table{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;flex:1}}
.port-table table{{width:100%;border-collapse:collapse;font-size:.8rem}}
.port-table th{{background:var(--surface2);padding:7px 10px;text-align:left;font-size:.72rem;color:var(--text-dim);font-weight:600;letter-spacing:.04em;border-bottom:1px solid var(--border)}}
.port-table td{{padding:7px 10px;border-bottom:1px solid var(--border);vertical-align:top}}
.port-table tr:last-child td{{border-bottom:none}}
.port-table tr:hover td{{background:var(--surface2)}}
.pnum{{font-weight:700;color:var(--primary);font-family:monospace}}
.psvc{{color:var(--green);font-weight:600}}
.pscripts{{font-family:monospace;font-size:.7rem;color:var(--text-dim);white-space:pre-wrap;word-break:break-all;max-height:80px;overflow:auto}}
#add-view{{padding:18px;max-width:520px}}
#add-view h2{{font-size:.95rem;font-weight:600;margin-bottom:14px}}
.fgrid{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}
.fgrid .full{{grid-column:1/-1}}
/* ── Record button variants ── */
.btn-rec-off{{background:#2a2a2a;color:#888;border:1px solid #444}}
.btn-rec-on{{background:#1e3a1e;color:#6fcf97;border:1px solid #2d5a2d}}
.btn-rec-active{{background:#4a1a1a;color:#ff6b6b;border:1px solid #8b2020;animation:rec-pulse 1.2s ease-in-out infinite}}
@keyframes rec-pulse{{0%,100%{{opacity:1}}50%{{opacity:.6}}}}
/* ── Storage view ── */
#storage-view{{padding:20px}}
#disk-bar-wrap{{flex:1}}
#disk-label{{font-size:.82rem;color:var(--text-dim);display:block;margin-bottom:6px}}
#disk-bar-track{{height:8px;background:#2a2a2a;border-radius:4px;overflow:hidden}}
#disk-bar-fill{{height:100%;border-radius:4px;transition:width .4s,background .4s}}
.stor-files{{padding:8px}}
.btn-xs{{padding:3px 8px;font-size:.72rem}}
/* ── Focus overlay ── */
#focus-overlay{{position:fixed;inset:0;background:#000;z-index:9000;display:flex;flex-direction:column;align-items:stretch;padding:0}}
#focus-img{{width:100vw;height:calc(100vh - 52px - 44px);height:calc(100dvh - 52px - 44px);object-fit:contain;display:block;margin:44px 0 0 0}}
/* 2.6.3 go2rtc live view: same box as #focus-img, swapped in its place */
#focus-video{{width:100vw;height:calc(100vh - 52px - 44px);height:calc(100dvh - 52px - 44px);margin:44px 0 0 0;background:#000}}
#focus-video anycam-video{{display:block;width:100%;height:100%}}
#focus-video video{{object-fit:contain;background:#000}}
#focus-bar{{position:absolute;bottom:0;left:0;right:0;height:52px;background:rgba(0,0,0,.85);display:flex;align-items:center;justify-content:space-between;padding:0 16px;gap:12px;z-index:9001;border-top:1px solid #333}}
#focus-info{{font-size:.78rem;color:#aaa;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}}
#focus-controls{{display:flex;align-items:center;gap:12px;flex-shrink:0}}
.qswitch{{display:flex;flex-direction:column;gap:2px;font-size:13px;color:#ddd;cursor:pointer}}
.qswitch .qrow{{display:flex;align-items:center;gap:6px}}
.qswitch .qsub{{font-size:11px;color:#999}}
#focus-close{{position:absolute;top:6px;right:14px;background:transparent;border:2.5px solid #e03;color:#e03;font-size:1rem;font-weight:bold;width:32px;height:32px;border-radius:50%;cursor:pointer;z-index:9002;line-height:1;display:flex;align-items:center;justify-content:center}}
#focus-close:hover{{background:#e03;color:#fff}}
#focus-loading{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:9001;color:#ddd;font-size:1rem;text-align:center;pointer-events:none}}
#focus-loading-note{{color:#999;font-size:.8rem;margin-top:8px}}
/* 2.6.5: landscape on a touch screen, set by _focusLandscapeSync */
#focus-overlay.focus-landscape #focus-bar{{display:none}}
#focus-overlay.focus-landscape #focus-img,#focus-overlay.focus-landscape #focus-video{{height:100vh;height:100dvh;margin:0}}
#focus-warning{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);background:#1a1a1a;border:1px solid var(--orange);border-radius:10px;padding:24px;max-width:480px;text-align:center;z-index:9003;display:flex;flex-direction:column;gap:14px;align-items:center}}
#focus-warn-text{{color:#f5b942;font-size:.9rem;line-height:1.5}}
/* ── Toast ── */
#toast{{position:fixed;bottom:28px;left:50%;transform:translateX(-50%);padding:10px 22px;border-radius:24px;color:#fff;font-size:.85rem;z-index:9100;pointer-events:none;transition:opacity .3s}}
/* ── Storage view layout ── */
#storage-view{{padding:16px 18px}}
#storage-topbar{{display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap}}
/* ── Explorer pane (white box with nav inside) ── */
#stor-explorer{{background:#fff;border:1px solid #d0d0d0;border-radius:4px;overflow:hidden;color:#000}}
#stor-nav-bar{{display:flex;align-items:center;gap:4px;padding:6px 8px;background:#f3f3f3;border-bottom:1px solid #d0d0d0}}
#stor-nav-bar button{{background:none;border:1px solid transparent;color:#333;border-radius:3px;width:28px;height:24px;cursor:pointer;font-size:.85rem;line-height:1;transition:background .1s;flex-shrink:0}}
#stor-nav-bar button:disabled{{opacity:.35;cursor:default}}
#stor-nav-bar button:not(:disabled):hover{{background:#e0e0e0;border-color:#c0c0c0}}
#stor-breadcrumb{{flex:1;background:#fff;border:1px solid #c0c0c0;border-radius:2px;padding:3px 8px;font-size:.82rem;color:#111;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-left:4px;display:flex;align-items:center;gap:0;cursor:text}}
#stor-breadcrumb:hover{{border-color:#888}}
#stor-path-input{{width:100%;border:none;outline:none;font-size:.82rem;color:#111;background:transparent;padding:0}}
#stor-path-root{{color:#0066cc;cursor:pointer;border-radius:2px;padding:1px 3px}}
#stor-path-root:hover{{background:#e8f0fe}}
#stor-path-folder{{color:#111;font-weight:600}}
/* ── Column headers ── */
#stor-col-headers{{display:grid;grid-template-columns:1fr 180px 130px 90px 80px;gap:0;padding:5px 8px;background:#f3f3f3;border-bottom:1px solid #d0d0d0;font-size:.78rem;font-weight:600;color:#333}}
#stor-col-headers span{{cursor:pointer;user-select:none;padding:2px 4px;border-radius:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
#stor-col-headers span:hover{{background:#e0e0e0}}
#stor-col-headers .stor-col-acts{{cursor:default}}
#stor-col-headers .stor-col-acts:hover{{background:none}}
/* ── File/folder rows ── */
#storage-list{{max-height:calc(100vh - 260px);max-height:calc(100dvh - 260px);overflow-y:auto}}
.stor-row{{display:grid;grid-template-columns:1fr 180px 130px 90px 80px;gap:0;padding:3px 8px;font-size:.82rem;color:#111;align-items:center;border-bottom:1px solid #f0f0f0;cursor:default}}
.stor-row:hover{{background:#cce8ff}}
.stor-row:last-child{{border-bottom:none}}
.stor-row-name{{display:flex;align-items:center;gap:6px;overflow:hidden}}
.stor-row-name-text{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.stor-row-name-text.editable:hover{{color:#0066cc;cursor:pointer;text-decoration:underline}}
.stor-row-date,.stor-row-type,.stor-row-size{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#444;font-size:.78rem}}
.stor-row-acts{{display:flex;gap:4px;justify-content:flex-end;opacity:0}}
.stor-row:hover .stor-row-acts{{opacity:1}}
.stor-row-folder{{font-weight:500}}
.stor-empty-msg{{text-align:center;padding:60px 20px;color:#888;font-size:.9rem}}
/* ── Logs view ── */
/* ── header h1 cursor ── */
header h1{{cursor:pointer}}
.field label{{display:block;font-size:.7rem;color:var(--text-dim);font-weight:600;letter-spacing:.04em;margin-bottom:3px}}
.field input,.field select{{width:100%;background:var(--surface);border:1px solid var(--border);
  color:var(--text);border-radius:8px;padding:7px 10px;font-size:.84rem;outline:none}}
.field input:focus,.field select:focus{{border-color:var(--primary)}}
.field select option{{background:var(--surface2)}}
#add-error{{font-size:.78rem;color:var(--red);display:none;padding:6px 0}}
.modal-backdrop{{position:fixed;inset:0;background:rgba(0,0,0,.7);display:none;align-items:center;justify-content:center;z-index:100}}
.modal-backdrop.open{{display:flex}}
.modal{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
        padding:20px;width:min(360px,90vw);display:flex;flex-direction:column;gap:12px}}
.modal h3{{font-size:.92rem;font-weight:600}}
.modal input{{width:100%;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:.86rem;padding:7px 10px;outline:none}}
.modal input:focus{{border-color:var(--primary)}}
.modal-btns{{display:flex;gap:8px;justify-content:flex-end}}
/* 2.6.6 camera settings (cog menu) */
.cs-modal{{width:min(440px,94vw)}}
.cs-section{{font-size:.7rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--text-dim);margin-top:4px}}
.cs-label{{display:flex;justify-content:space-between;font-size:.84rem}}
.cs-val{{font-weight:700;color:var(--primary);min-width:2.5em;text-align:right}}
.cs-slider{{position:relative}}
.cs-modal .cs-slider input[type=range]{{width:100%;padding:0;border:none;background:transparent;accent-color:var(--primary);cursor:pointer}}
.cs-mode{{font-size:12px;color:var(--text-dim);margin-top:4px}}
.cs-mode.cs-night{{color:var(--blue)}}
.cs-night-note{{font-size:12px;color:var(--orange);margin-top:4px}}
.cs-peak-mark{{position:absolute;top:-2px;width:3px;height:22px;margin-left:-1px;background:var(--orange);border-radius:2px;pointer-events:none}}
.cs-ends{{display:flex;justify-content:space-between;font-size:.7rem;color:var(--text-dim);margin-top:-6px}}
.cs-peak{{font-size:.78rem;color:var(--orange);min-height:1.2em}}
.cs-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}
.cs-modal label{{display:flex;flex-direction:column;gap:4px;font-size:.76rem;color:var(--text-dim)}}
.cs-modal select{{background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:.86rem;padding:7px 8px}}
.cs-help{{font-size:.72rem;color:var(--text-dim);line-height:1.45}}
.cs-global{{font-size:.78rem;color:var(--yellow);border:1px solid var(--yellow);border-radius:8px;padding:8px 10px}}
.cs-error{{font-size:.78rem;color:var(--red);min-height:1em}}
.cs-modal input:disabled,.cs-modal select:disabled{{opacity:.45;cursor:not-allowed}}
/* 3.4.0 (C17): zones in the settings panel */
.cs-zone{{display:grid;grid-template-columns:1fr 1.4fr auto;align-items:center;gap:8px;font-size:.82rem}}
.cs-zone-name{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.cs-zone input[type=range]{{width:100%;padding:0;border:none;background:transparent;accent-color:var(--primary)}}
.cs-modal label.cs-check{{flex-direction:row;align-items:center;gap:8px;font-size:.84rem;color:var(--text)}}
/* 3.7.0-rc2.0 (B28): .modal input sets width 100%, which pushed the box away from its text */
.modal label.cs-check input[type=checkbox]{{width:auto;flex:0 0 auto;margin:0;padding:0;accent-color:var(--primary)}}
/* 3.4.0 (C17): the drawing window over Enhanced View */
#focus-zone-ctl{{display:flex;align-items:center;gap:10px;flex-shrink:0;font-size:.78rem;color:#ccc}}
#focus-zone-ctl label{{display:flex;align-items:center;gap:4px;cursor:pointer}}
#zone-svg,#zone-still{{position:fixed;z-index:9002;pointer-events:none}}
#zone-svg.zone-edit{{pointer-events:auto;cursor:crosshair;touch-action:none}}
#zone-svg .zone-shape{{fill:rgba(79,142,247,.18);stroke:#4f8ef7;stroke-width:2}}
#zone-svg polyline.zone-shape{{fill:none;stroke-dasharray:6 4}}
#zone-svg .zone-sel{{stroke:#ffd34d;fill:rgba(255,211,77,.16)}}
#zone-svg .zone-off{{stroke:#888;fill:rgba(128,128,128,.18)}}
#zone-svg .zone-rec{{stroke:#ff5252;fill:rgba(255,82,82,.2);stroke-width:3}}
#zone-svg .zone-label{{fill:#fff;font-size:13px;text-anchor:middle;paint-order:stroke;stroke:#000;stroke-width:3px;pointer-events:none}}
#zone-svg .zone-pt{{fill:#fff;stroke:#ffd34d;stroke-width:2;cursor:move}}
#zone-svg .zone-first{{fill:#ffd34d}}
#zone-svg .zone-near{{fill:#4cd964;stroke:#fff}}
#zone-svg .zone-mid{{fill:rgba(255,211,77,.55);stroke:none;cursor:copy}}
#zone-svg .zone-rubber{{stroke:#ffd34d;stroke-width:2;stroke-dasharray:4 4;pointer-events:none}}
#zone-panel{{position:fixed;right:12px;top:56px;width:280px;max-height:calc(100dvh - 130px);overflow:auto;z-index:9003;
             background:rgba(20,22,28,.94);border:1px solid #333;border-radius:10px;padding:12px;display:flex;flex-direction:column;gap:8px;color:#ddd;font-size:.8rem}}
#zone-panel .zp-head{{font-weight:700;font-size:.9rem;display:flex;align-items:center;justify-content:space-between;
                      gap:8px;cursor:move;touch-action:none;user-select:none;margin:-4px -4px 0;padding:4px}}
#zone-panel .zp-fold{{background:transparent;border:1px solid #444;border-radius:6px;color:#ddd;cursor:pointer;
                      width:28px;height:24px;line-height:1;font-size:.9rem}}
/* 3.7.0-rc2.0 (C22): folded, only the title bar shows */
#zone-panel.zp-folded > :not(.zp-head){{display:none}}
#zone-panel .zp-hint{{color:#aaa;line-height:1.4}}
#zone-panel .zp-hint.zone-bad{{color:#ff7070}}
#zone-panel .zp-btns{{display:flex;flex-wrap:wrap;gap:6px}}
#zone-panel .zp-end{{justify-content:flex-end}}
#zone-panel .zp-only{{display:flex;align-items:center;gap:6px}}
.zl-row{{border:1px solid #333;border-radius:8px;padding:8px;display:grid;grid-template-columns:1fr auto;gap:6px;cursor:pointer}}
.zl-row.zl-sel{{border-color:#ffd34d}}
.zl-name{{background:#111;border:1px solid #444;border-radius:6px;color:#eee;padding:4px 6px;font-size:.82rem;min-width:0}}
.zl-lev{{grid-column:1 / span 2;display:flex;align-items:center;gap:8px}}
.zl-lev input{{flex:1;accent-color:var(--primary)}}
.zl-note{{grid-column:1 / span 2;color:#f5b942;font-size:.74rem}}
.zl-empty{{color:#888}}
.stor-zone{{margin-left:8px;font-size:.72rem;color:var(--orange);border:1px solid var(--orange);border-radius:6px;padding:0 5px}}
@media (max-width:700px){{#zone-panel{{left:8px;right:8px;top:auto;bottom:60px;width:auto;max-height:45dvh}}
  #focus-zone-ctl label{{display:none}}}}
.nc-modal-inner{{width:min(460px,94vw)}}
.nc-subtitle{{font-size:.82rem;color:var(--text-dim);margin-top:-4px}}
.nc-reasons{{display:flex;flex-wrap:wrap;gap:7px}}
.nc-reason-btn{{background:var(--surface2);border:1px solid var(--border);color:var(--text);
                border-radius:8px;padding:7px 12px;font-size:.78rem;cursor:pointer;
                transition:background .15s,border-color .15s;white-space:nowrap}}
.nc-reason-btn:hover{{background:var(--surface);border-color:var(--primary)}}
.nc-reason-btn.active{{background:var(--red);border-color:var(--red);color:#fff;font-weight:600}}
.nc-detail-row input{{width:100%;background:var(--bg);border:1px solid var(--border);
                      border-radius:8px;color:var(--text);font-size:.82rem;padding:7px 10px;outline:none}}
.nc-detail-row input:focus{{border-color:var(--primary)}}
.nc-share-row{{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px}}
.nc-share-label{{display:flex;align-items:flex-start;gap:8px;font-size:.8rem;cursor:pointer}}
.nc-share-label input{{flex-shrink:0;margin-top:2px;accent-color:var(--primary)}}
.nc-share-note{{font-size:.7rem;color:var(--text-dim);margin-top:5px;line-height:1.4}}
/* 2.4.0-rc2.0: Locked Streams modal — surfaces 401-locked RTSP paths
   the path walker found while collecting locked candidates. Same
   visual treatment as the notCam modal but with a credential-entry
   pane embedded. */
.locked-modal-inner{{width:min(520px,94vw)}}
.locked-subtitle{{font-size:.82rem;color:var(--text-dim);margin-top:-4px;line-height:1.5}}
.locked-list{{background:var(--surface2);border:1px solid var(--border);border-radius:8px;
              padding:8px 10px;max-height:180px;overflow-y:auto;font-family:monospace;
              font-size:.78rem;display:flex;flex-direction:column;gap:6px}}
.locked-row{{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px;
             padding:4px 6px;border-radius:5px;background:var(--bg)}}
.locked-row code{{color:var(--primary);background:transparent;font-size:.78rem}}
.locked-meta{{font-size:.68rem;color:var(--text-dim);font-family:monospace}}
.locked-cred-form{{display:flex;flex-direction:column;gap:6px;
                   background:var(--surface2);border:1px solid var(--border);
                   border-radius:8px;padding:10px}}
.locked-cred-form label{{font-size:.7rem;color:var(--text-dim);font-weight:600;letter-spacing:.04em}}
.locked-cred-form input{{width:100%;background:var(--bg);border:1px solid var(--border);
                         border-radius:8px;color:var(--text);font-size:.82rem;padding:7px 10px;outline:none}}
.locked-cred-form input:focus{{border-color:var(--primary)}}
.badge.locked-streams-badge:hover{{background:#3a2e54 !important}}"""

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AnyCam</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.7/dist/hls.min.js"></script>
<style>
""" + css + """
</style>
</head>
<body>

<header>
  <h1 onclick="switchView('cameras')" title="Click for Home" style="cursor:pointer">
    <svg id="status-cam-icon" width="27" height="27" viewBox="0 0 24 24"
         fill="none" stroke="#43a047" stroke-width="2"
         style="cursor:pointer;flex-shrink:0;vertical-align:middle"
         onclick="openHALog();event.stopPropagation()">
      <title>System Stability &mdash; See Logs</title>
      <path d="M15 10l4.553-2.069A1 1 0 0121 8.87v6.26a1 1 0 01-1.447.9L15 14"/>
      <rect x="1" y="7" width="14" height="10" rx="2" ry="2"/>
    </svg>
    <span style="margin-left:10px">AnyCam &mdash; Home</span>
  </h1>
  <span id="cam-count" style="color:var(--text-dim);font-size:.78rem"></span>
  <label class="sweep-toggle" title="Scan ports 0-10000 on live hosts that don't respond to camera ports">
    <input type="checkbox" id="broad-sweep">
    <span>Deeper Scan<br><small style="font-weight:400;opacity:.6;font-size:.68rem">Ports 1&#x2013;10,000</small></span>
  </label>
  <button class="btn btn-primary"   id="scan-btn"  onclick="startScan()">&#x1F50D; Scan Network</button>
  <button class="btn btn-secondary" id="pscan-btn"    onclick="switchView('pscan')">&#x1F50E; Port Scan</button>
  <button class="btn btn-secondary" id="add-btn"      onclick="switchView('add')">&#x2795; Connect Camera</button>
  <button class="btn btn-secondary" id="storage-btn"  onclick="switchView('storage')">&#x1F4BE; Storage</button>

</header>

<div id="status-bar">
  <span id="stage-badge" class="stage-badge" style="display:none"></span>
  <span id="status-msg">Idle &#x2014; click Scan Network to start.</span>
  <div class="progress-track" id="progress-track" style="display:none">
    <div class="progress-fill" id="progress-fill"></div>
  </div>
  <span id="scan-timer" style="display:none;font-size:.74rem;color:var(--primary);font-weight:600;white-space:nowrap"></span>
  <button id="scan-cancel-btn" onclick="cancelScan()"
          style="display:none;margin-left:auto;padding:3px 12px;font-size:.75rem;
                 background:#8b2020;color:#fff;border:none;border-radius:6px;cursor:pointer">
    &#x2715; Cancel
  </button>
</div>

<div class="view active" id="cameras-view">
  <div id="cam-grid">
    <div id="empty-state">
      <svg width="60" height="60" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M15 10l4.553-2.069A1 1 0 0121 8.87v6.26a1 1 0 01-1.447.9L15 14"/>
        <rect x="1" y="7" width="14" height="10" rx="2" ry="2"/>
      </svg>
      <p id="empty-idle">No cameras found.<br>Click <strong>Scan Network</strong> to discover cameras on your subnet.</p>
      <p id="empty-scanning" style="display:none">No Cameras Found Yet</p>
    </div>
  </div>
</div>

<div class="view" id="pscan-view">
  <!-- Back button upper left + controls -->
  <div class="pscan-header">
    <button class="btn btn-ghost btn-sm" onclick="switchView('cameras')">&#x2190; Back to Cameras</button>
    <div class="pscan-ctl">
      <button class="btn btn-primary"   id="ps-start"  onclick="startPortScan()">&#x25B6; Scan All Ports</button>
      <button class="btn btn-secondary" id="ps-pause"  onclick="togglePause()" style="display:none">&#x23F8; Pause</button>
      <button class="btn btn-danger"    id="ps-cancel" onclick="cancelPortScan()" style="display:none">&#x2715; Cancel</button>
    </div>
  </div>
  <!-- ARP-discovered hosts with checkboxes -->
  <div class="arp-host-section">
    <div class="arp-section-label">&#x1F4E1; Discovered hosts <span style="color:var(--text-dim);font-weight:400">(check any to include in port scan)</span></div>
    <div id="arp-host-list" class="arp-host-list">
      <span class="arp-empty">No hosts yet — run a network scan first.</span>
    </div>
  </div>
  <!-- Manual IP input -->
  <div class="pscan-top">
    <input type="text" id="pscan-ip" placeholder="Or enter an IP address manually (e.g. 192.168.1.100)"
           onkeydown="if(event.key==='Enter')startPortScan()"/>
  </div>
  <div id="pscan-msg" style="padding:6px 2px;font-size:.8rem;color:var(--text-dim)">Select hosts above and/or enter an IP, then click Scan All Ports.<br><small style="opacity:.7">Note: navigating away will not cancel an in-progress scan.</small></div>
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:4px">
    <div class="progress-track" id="ps-prog-track" style="display:none;flex:1;max-width:none">
      <div class="progress-fill" id="ps-prog-fill"></div>
    </div>
    <span id="ps-timer" style="display:none;font-size:.74rem;color:var(--primary);font-weight:600;white-space:nowrap"></span>
  </div>
  <!-- Live port discovery feed — shown during scan, replaced by table when done -->
  <div id="live-ports-box" class="live-ports-box" style="display:none"></div>
  <div class="port-table" id="port-table" style="display:none">
    <table>
      <thead><tr><th>Port</th><th>Proto</th><th>Service</th><th>Product / Version</th><th>Script Output</th></tr></thead>
      <tbody id="port-tbody"></tbody>
    </table>
  </div>
</div>

<div class="view" id="add-view">
  <div style="margin-bottom:14px">
    <button class="btn btn-ghost btn-sm" onclick="switchView('cameras')">&#x2190; Back to Cameras</button>
  </div>
  <h2>Connect Known Camera</h2>
  <div class="fgrid">
    <div class="field full"><label>CAMERA NAME</label><input type="text" id="add-name" placeholder="e.g. Front Door"/></div>
    <div class="field"><label>IP ADDRESS</label><input type="text" id="add-ip" placeholder="192.168.1.100"/></div>
    <div class="field"><label>PORT</label><input type="number" id="add-port" value="554" min="1" max="65535"/></div>
    <div class="field"><label>PROTOCOL</label>
      <select id="add-proto" onchange="onProtoChange()">
        <option value="RTSP">RTSP</option><option value="ONVIF">ONVIF</option>
        <option value="MJPEG">HTTP MJPEG</option><option value="HLS">HLS</option>
        <option value="RTMP">RTMP</option><option value="WebRTC">WebRTC</option>
        <option value="WS-RTSP">WS-RTSP</option>
      </select>
    </div>
    <div class="field full" id="path-field">
      <label>STREAM PATH <span style="font-weight:400;color:var(--text-dim)">(optional)</span></label>
      <input type="text" id="add-path" placeholder="/stream  or  /cam/realmonitor?channel=1"/>
    </div>
    <div class="field"><label>USERNAME <span style="font-weight:400;color:var(--text-dim)">(optional)</span></label>
      <input type="text" id="add-user" autocomplete="username"/></div>
    <div class="field"><label>PASSWORD <span style="font-weight:400;color:var(--text-dim)">(optional)</span></label>
      <input type="password" id="add-pass" autocomplete="current-password"/></div>
  </div>
  <div id="add-error"></div>
  <div style="display:flex;gap:10px;margin-top:12px">
    <button class="btn btn-primary" id="add-btn2" onclick="submitAddCamera()">Connect</button>
  </div>
</div>

<div class="modal-backdrop" id="rename-modal">
  <div class="modal">
    <h3>Rename Camera</h3>
    <input type="text" id="rename-input" placeholder="Camera name"/>
    <div class="modal-btns">
      <button class="btn btn-ghost btn-sm" onclick="closeRename()">Cancel</button>
      <button class="btn btn-primary btn-sm" onclick="submitRename()">Save</button>
    </div>
  </div>
</div>

<!-- 2.6.6: per-camera settings, opened from the cog on each card -->
<div class="modal-backdrop" id="cam-settings-modal" onclick="if(event.target.id==='cam-settings-modal')closeCamSettings()">
  <div class="modal cs-modal">
    <h3 id="cs-title">Settings</h3>
    <div class="cs-global" id="cs-global" style="display:none">Global recording settings are on in the
      Configuration tab, so they apply to every camera. Turn them off there to set each camera here.</div>
    <div class="cs-section">Motion</div>
    <div class="cs-label"><span>Sensitivity</span><span class="cs-val" id="cs-level-val">63</span></div>
    <div class="cs-slider">
      <input type="range" id="cs-level" min="1" max="100" step="1" oninput="csLevelShow()">
      <div class="cs-peak-mark" id="cs-peak-mark" style="display:none" title="Biggest recent movement"></div>
    </div>
    <div class="cs-ends"><span>Less sensitive</span><span>More sensitive</span></div>
    <div class="cs-peak" id="cs-peak"></div>
    <div class="cs-mode" id="cs-mode"></div>
    <div class="cs-night-note" id="cs-night-note" style="display:none"></div>
    <div class="cs-section">Zones</div>
    <div id="cs-zones"></div>
    <div class="cs-peak" id="cs-zone-peaks"></div>
    <label class="cs-check" id="cs-zones-only-row" style="display:none"><input type="checkbox" id="cs-zones-only" onchange="csZonesOnly()"> Detection in zones only</label>
    <button class="btn btn-ghost btn-sm" onclick="csEditZones()">Edit zones</button>
    <div class="cs-help">A zone belongs to one view of the camera. If the camera turns (PTZ), its zones no longer match the picture.</div>
    <div class="cs-section">Recording</div>
    <div class="cs-grid">
      <label>Cooldown (s)<input type="number" id="cs-cooldown" min="1" max="300"></label>
      <label>Tail (s)<input type="number" id="cs-tail" min="0" max="30"></label>
      <label>File length<select id="cs-clip"></select></label>
    </div>
    <label class="cs-full">Recording folder<input type="text" id="cs-path" spellcheck="false"></label>
    <div class="cs-help">Under /media. For a Samba or NFS share, add it in Home Assistant
      (Settings, System, Storage, Add network storage, usage Media); it appears as
      /media/&lt;name&gt;. To copy recordings to a server by SFTP, FTPS or FTP, use Remote Storage.</div>
    <button class="btn btn-ghost btn-sm" onclick="openUpload(_csCamId)">&#x21E7; Remote Storage</button>
    <div class="cs-error" id="cs-error"></div>
    <div class="modal-btns">
      <button class="btn btn-ghost btn-sm" id="cs-reset" onclick="resetCamSettings()" style="margin-right:auto">Defaults</button>
      <button class="btn btn-ghost btn-sm" onclick="closeCamSettings()">Cancel</button>
      <button class="btn btn-primary btn-sm" id="cs-save" onclick="saveCamSettings()">Save</button>
    </div>
  </div>
</div>

<!-- 3.6.0 (C14): recording upload -->
<div class="modal-backdrop" id="upload-modal" onclick="if(event.target.id==='upload-modal')closeUpload()">
  <div class="modal cs-modal">
    <h3 id="up-title">Remote Storage</h3>
    <label id="up-mode-row" style="display:none">This camera
      <select id="up-mode" onchange="upModeShow()">
        <option value="global">Use the global destination</option>
        <option value="own">Its own destination</option>
        <option value="off">Do not upload</option>
      </select></label>
    <div class="cs-help" id="up-global-note"></div>
    <div id="up-fields" style="display:flex;flex-direction:column;gap:10px">
      <div class="cs-grid">
        <label>Protocol<select id="up-proto" onchange="upProtoPort()">
          <option value="sftp">SFTP</option><option value="ftps">FTPS</option><option value="ftp">FTP</option>
        </select></label>
        <label>Server<input type="text" id="up-host" spellcheck="false" autocomplete="off"></label>
        <label>Port<input type="number" id="up-port" min="1" max="65535"></label>
      </div>
      <div class="cs-grid">
        <label>User<input type="text" id="up-user" spellcheck="false" autocomplete="off"></label>
        <label>Password<input type="password" id="up-pass" autocomplete="new-password"></label>
        <label>Folder<input type="text" id="up-path" spellcheck="false"></label>
      </div>
      <label class="cs-check"><input type="checkbox" id="up-delete" checked> Delete the local copy after upload</label>
      <div class="cs-help" id="up-ftp-warn" style="display:none">FTP sends the password and the
        recordings unencrypted. Use SFTP or FTPS when the server offers them.</div>
      <div class="cs-help">Each recording goes to &lt;folder&gt;/&lt;camera&gt;/ once it is finished.
        A failed upload is tried again later; the file stays here until it succeeds.</div>
    </div>
    <div class="cs-help" id="up-status"></div>
    <div class="cs-error" id="up-error"></div>
    <div class="modal-btns">
      <button class="btn btn-ghost btn-sm" id="up-remove" onclick="removeUpload()" style="margin-right:auto;display:none">Remove</button>
      <button class="btn btn-ghost btn-sm" onclick="testUpload()">Test</button>
      <button class="btn btn-ghost btn-sm" onclick="closeUpload()">Cancel</button>
      <button class="btn btn-primary btn-sm" onclick="saveUpload()">Save</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="nc-modal" onclick="if(event.target.id==='nc-modal')closeNotCamModal()">
  <div class="modal nc-modal-inner">
    <h3>&#x1F6AB; Not a Camera</h3>
    <p class="nc-subtitle">What kind of device is <strong id="nc-device-label"></strong>?</p>
    <div class="nc-reasons">
      <button class="nc-reason-btn" data-reason="printer"    onclick="selectReason(this)">&#x1F5A8; Printer</button>
      <button class="nc-reason-btn" data-reason="router"     onclick="selectReason(this)">&#x1F310; Router / Firewall</button>
      <button class="nc-reason-btn" data-reason="nas"        onclick="selectReason(this)">&#x1F4BE; NAS / Storage</button>
      <button class="nc-reason-btn" data-reason="computer"   onclick="selectReason(this)">&#x1F4BB; Computer</button>
      <button class="nc-reason-btn" data-reason="tv"         onclick="selectReason(this)">&#x1F4FA; Smart TV</button>
      <button class="nc-reason-btn" data-reason="iot"        onclick="selectReason(this)">&#x1F4F1; IoT Device</button>
      <button class="nc-reason-btn" data-reason="unknown"    onclick="selectReason(this)">&#x2753; Not sure</button>
    </div>
    <div class="nc-detail-row">
      <input type="text" id="nc-detail" placeholder="Optional: any extra detail (e.g. model name)" maxlength="200"/>
    </div>
    <div class="nc-share-row" id="nc-share-row">
      <label class="nc-share-label">
        <input type="checkbox" id="nc-share">
        <span>Share this anonymously to help improve AnyCam for everyone</span>
      </label>
      <p class="nc-share-note">No IP addresses are sent — only device type, OUI, open ports, and service banners.</p>
    </div>
    <div class="modal-btns">
      <button class="btn btn-ghost btn-sm" onclick="closeNotCamModal()">Cancel</button>
      <button class="btn btn-danger btn-sm" onclick="submitNotCam()">Confirm — Not a Camera</button>
    </div>
  </div>
</div>

<!-- 2.4.0-rc2.0: Layered Stream Discovery — locked streams modal.
     Surfaces 401-locked RTSP paths the path walker found while probing
     this camera. User enters credentials; the existing cred-auth flow
     attempts to authenticate against ALL paths (working unauth + locked),
     so successful auth automatically unlocks any of these that share the
     same auth domain (which they all do — same-realm filter ensures it). -->
<div class="modal-backdrop" id="locked-modal" onclick="if(event.target.id==='locked-modal')closeLockedStreams()">
  <div class="modal locked-modal-inner">
    <h3>&#x1F512; Locked Streams</h3>
    <p class="locked-subtitle">
      AnyCam found <strong id="locked-count"></strong> additional stream(s) on
      <strong id="locked-device-label"></strong> that require credentials.
      Enter the camera's username and password to unlock them.
    </p>
    <div class="locked-list" id="locked-list"></div>
    <div class="locked-cred-form">
      <label>USERNAME</label>
      <input type="text" id="locked-user" autocomplete="username">
      <label>PASSWORD</label>
      <input type="password" id="locked-pass"
        autocomplete="current-password"
        onkeydown="if(event.key==='Enter')submitLockedCreds()">
      <div class="cred-error" id="locked-err"></div>
    </div>
    <div class="modal-btns">
      <button class="btn btn-ghost btn-sm" onclick="closeLockedStreams()">Cancel</button>
      <button class="btn btn-primary btn-sm" onclick="submitLockedCreds()">Unlock</button>
    </div>
  </div>
</div>

<script>
""" + js_code + """
</script>
<!-- ── Storage view ───────────────────────────────────────────────────────── -->
<div class="view" id="storage-view">
  <div id="storage-topbar">
    <button class="btn btn-ghost btn-sm" onclick="switchView('cameras')">&#x2190; Back to Cameras</button>
    <div id="disk-bar-wrap">
      <span id="disk-label">Loading...</span>
      <div id="disk-bar-track"><div id="disk-bar-fill"></div></div>
    </div>
    <button class="btn btn-secondary btn-sm" onclick="openUpload(null)" title="Copy recordings to a server by SFTP, FTPS or FTP">&#x21E7; Remote Storage</button>
    <button class="btn btn-secondary btn-sm" onclick="loadStorage()">&#x21BB; Refresh</button>
  </div>
  <!-- Explorer pane: nav bar + column headers + file list all inside white box -->
  <div id="stor-explorer">
    <div id="stor-nav-bar">
      <button id="stor-back-btn" onclick="storNavBack()" title="Back" disabled>&#x2190;</button>
      <button id="stor-up-btn"   onclick="storNavUp()"   title="Up"   disabled>&#x2191;</button>
      <div id="stor-breadcrumb" onclick="if(!event.target.closest('#stor-path-root'))_pathBarEdit()">
        <span id="stor-path-root" onclick="_storNavTo(null);event.stopPropagation()"
              style="cursor:pointer;padding:2px 6px;border-radius:3px;color:#0066cc"
              title="/media/anycam">/media/anycam</span>
        <span id="stor-path-sep" style="display:none;color:#888;padding:0 2px">&rsaquo;</span>
        <span id="stor-path-folder" style="font-weight:600;color:#111;padding:2px 4px"></span>
      </div>
    </div>
    <div id="stor-col-headers">
      <span class="stor-col-name"  onclick="storSort('name')"  id="sorth-name">Name &#x25B2;</span>
      <span class="stor-col-date"  onclick="storSort('date')"  id="sorth-date">Date Modified</span>
      <span class="stor-col-type"  onclick="storSort('type')"  id="sorth-type">Type</span>
      <span class="stor-col-size"  onclick="storSort('size')"  id="sorth-size">Size</span>
      <span class="stor-col-acts"></span>
    </div>
    <div id="storage-list"></div>
  </div>
</div>



<!-- ── Focus / full-screen enhanced view overlay ──────────────────────────── -->
<div id="focus-overlay" style="display:none">
  <button id="focus-close" onclick="closeFocus()" title="Exit enhanced view (Esc)">&#x2715;</button>
  <div id="focus-warning" style="display:none">
    <span id="focus-warn-text"></span>
    <button onclick="document.getElementById('focus-warning').style.display='none'">OK</button>
  </div>
  <img id="focus-img" alt="">
  <div id="focus-video" style="display:none"></div>
  <div id="focus-loading" style="display:none">
    <div>Loading feed, please wait…</div>
    <div id="focus-loading-note"></div>
  </div>
  <!-- 3.4.0 (C17): detection zones -->
  <canvas id="zone-still" style="display:none"></canvas>
  <svg id="zone-svg" style="display:none" xmlns="http://www.w3.org/2000/svg"></svg>
  <div id="zone-panel" style="display:none">
    <div class="zp-head" id="zone-head" onpointerdown="zonePanelDragStart(event)" title="Drag to move">
      <span>Detection zones</span>
      <button class="zp-fold" id="zone-fold" onclick="zoneFold()" title="Fold or unfold" aria-label="Fold or unfold">▾</button>
    </div>
    <div id="zone-hint" class="zp-hint"></div>
    <div id="zone-list"></div>
    <label class="zp-only"><input type="checkbox" id="zone-only" onchange="zoneOnlyChange()"> Detection in zones only</label>
    <div class="zp-btns">
      <button class="btn btn-ghost btn-sm" id="zone-new" onclick="zoneNew()">+ New zone</button>
      <button class="btn btn-ghost btn-sm" id="zone-undo" onclick="zoneUndo()">Undo</button>
      <button class="btn btn-ghost btn-sm" id="zone-close" onclick="zoneCloseShape()">Close shape</button>
      <button class="btn btn-ghost btn-sm" id="zone-finish" onclick="zoneFinish(false)">Finish</button>
      <button class="btn btn-ghost btn-sm" id="zone-pause" onclick="zonePause()">Pause</button>
    </div>
    <div class="zp-btns zp-end">
      <button class="btn btn-ghost btn-sm" id="zone-cancel" onclick="zoneCancel()">Cancel</button>
      <button class="btn btn-primary btn-sm" onclick="zoneDone()">Done</button>
    </div>
  </div>
  <div id="focus-bar">
    <div id="focus-info">Loading…</div>
    <div id="focus-zone-ctl">
      <label class="fz-show"><input type="checkbox" id="focus-zone-show" onchange="zoneShowToggle()"> Show zones</label>
      <button class="btn btn-ghost btn-sm" onclick="zoneEditOpen()">Zones</button>
    </div>
    <div id="focus-controls"></div>
  </div>
</div>

<div id="card-drop-line"></div>

<!-- ── Toast notification ─────────────────────────────────────────────────── -->
<div id="toast" style="display:none"></div>

</body>
</html>"""


async def handle_index(request: web.Request) -> web.Response:
    global HTML
    if HTML is None:
        HTML = build_html()
    return web.Response(text=HTML, content_type="text/html")
