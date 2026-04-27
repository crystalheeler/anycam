## 2.2.4
SigRev-1 bundle (Items 1, 2, 3 — Items 4 and 7 were completed in 2.2.2/2.2.3):

- SigRev-1 Item 1 — Backoff jitter: the ffmpeg restart backoff sleep in snap_loop
  now applies ±10% random jitter (random.uniform(0.9, 1.1)) so multiple cameras
  that fail simultaneously don't all restart in lockstep and hammer the Pi together;
  added import random to module imports

- SigRev-1 Item 2 — Type hints: all 38 functions missing return annotations are now
  annotated (-> None, -> web.Response, -> ssl.SSLContext, -> str, -> int, etc.);
  added import ssl and import concurrent.futures to module imports for type accuracy

- SigRev-1 Item 3 — Thread pool cap: replaced all 44 run_in_executor(None, ...) calls
  with a named, bounded ThreadPoolExecutor(_THREAD_POOL, max_workers=12,
  thread_name_prefix='anycam'); prevents unbounded thread creation during full scans;
  12 workers chosen for I/O-bound network probing on a 4-core Pi; pool is shut down
  cleanly via _THREAD_POOL.shutdown(wait=False) when main() exits

Best-practice audit (required on this release):
  ✓ P1 Semantic contracts (6 functions verified)
  ✓ P2 No bare except clauses
  ✓ P3 No time.sleep() in async
  ✓ P4 No blocking open() in async
  ✓ P5 All functions annotated (0 missing — new)
  ✓ P6 No mutable default arguments
  ✓ J1-J4 JS checks clean
  ✓ H1-H2 HTML checks clean
  ✓ C1-C2 CSS typo / !important clean
  ✓ C3 Added dvh fallback to #storage-list max-height (body min-height
       left as-is: min-height:100vh is not affected by address-bar jump)
  ✓ PL1 No bare newlines in JS string literals (String.fromCharCode not needed)

## 2.2.3
- Bug fix: H.265+ warning badge on Hikvision card was a stale flag from
  cameras.json persisting across upgrades; verification scan RTSP-probe-fallback
  path now clears hevc_plus_warning=False whenever a camera verifies OK (was only
  cleared in the ONVIF re-auth path, which the Hikvision never reaches due to the SOAP 400)
- Bug fix: 'Stream unavailable' on card after browser tab backgrounded for 30+s;
  browser throttles/drops pending image requests while tab is hidden; those
  failures were accumulating in _snapErrors before the server could respond;
  added visibilitychange listener that resets all _snapErrors to 0 when the tab
  becomes visible again
- Bug fix: credentials exposed in logs — ffmpeg includes the full authenticated
  RTSP URL in its error messages; _drain_stderr was logging raw stderr without
  applying _strip_creds(); password now stripped before logging (SigRev-1 item 4)
- Bug fix: enhanced view for Microseven (broken RTSP) was looping ffmpeg
  crashes indefinitely instead of falling back to http_snap_loop; after 3
  consecutive 0-frame failures in native_res (enhanced view) mode, if the camera
  has an http_snap_url, snap_loop now falls back to http_snap_loop for the focus
  session; restores 2.1.9 behavior for cameras with broken RTSP
- All four fixes validated against best practices ruleset and semantic contracts

## 2.2.2
- Best-practice audit (first full audit against AnyCam Coding Best Practices doc):
  - P4 fixed: blocking open() in async def run_port_scan() replaced with
    loop.run_in_executor(None, lambda: open(xml_path).read()) so nmap XML
    parsing no longer holds the event loop
  - P6 fixed: mutable default dict {} in roundtrip() closure replaced with
    None default and extra = extra or {} guard inside the function
  - C3 fixed: #focus-img now has height:calc(100dvh - 52px - 44px) fallback
    after 100vh so mobile browsers do not jump when address bar hides/shows
- Added verify_release.py: standalone release verification script replacing the
  ad-hoc inline checks; runs 5 stages: syntax, semantic contracts, best-practice
  audit, version consistency, changelog; exits 1 on any failure
  - Semantic contract checks: 6 critical functions verified to contain their
    required identifiers (run_verification_scan, http_snap_loop, snap_loop,
    build_html, make_app, api_set_credentials)
  - Best-practice audit in script: mutable defaults, blocking open() in async,
    CSS // comments, display:flexbox typo
- Best practices ruleset (AnyCam_Coding_Best_Practices.md) established as
  permanent audit standard for all future releases

## 2.2.1
- run_verification_scan: was truncated — missing save_cameras(), SCAN_STATE
  cleanup, and the follow-up fresh network scan entirely; this caused the
  UI to show "Re-authenticating ONVIF… estimated 0:00 remaining" forever
  because SCAN_STATE["running"] was never set to False; fixed by adding
  proper cleanup block with save_cameras(), status update, await run_scan(),
  and a try/finally error fallback
- run_verification_scan: add HTTP snap URL as a third verification path —
  after RTSP probe fails, if camera has http_snap_url, does a quick GET;
  a 200 or 401 response confirms the camera is reachable; fixes Microseven
  the Microseven being incorrectly marked "Not found after upgrade"
- run_verification_scan: fix "was" version logging cosmetic bug — was reading
  load_runtime() after save_runtime() had already written the new version,
  so it always showed "was: current → now: current"; fixed by passing
  prev_version as a parameter from the call site
- http_snap_loop: persist upgraded https:// snap URL to cameras.json when
  redirect detected, so subsequent restarts use https:// directly without
  needing the redirect probe on every startup
- Note: not a 2.2.0 regression; truncation was pre-existing and not previously
  triggered because all prior installs were fresh installs skipping verification

## 2.2.1
- run_verification_scan: add HTTP snap URL as a third verification path —
  after RTSP probe fails, if camera has http_snap_url stored, does a quick
  GET to that URL; a 200 or 401 response both confirm the camera is reachable
  (401 = auth required = camera alive); fixes cameras like the Microseven
  being incorrectly marked 'Not found after upgrade' when their RTSP is broken
  but HTTP snap works fine
- run_verification_scan: fix cosmetic 'was' version logging bug — the log line
  was reading load_runtime() after save_runtime() had already written the new
  version, so it always showed 'was: 2.2.x → now: 2.2.x'; now passes
  prev_version as a parameter from the call site where it is read before
  save_runtime() is called
- Note: the 'Not found after upgrade' issue was pre-existing and not a 2.2.0
  regression; it was never triggered because all previous installs were fresh
  installs (which skip verification) rather than upgrades (Check for Updates)

## 2.2.0
- Enhanced view #1: video feed now has a 44px top margin so the X close button
  sits above the video rather than overlapping it; bottom black bar unchanged
- Enhanced view #2: Resolution and Frame Rate dropdowns are smaller and now
  have their labels ("Resolution", "Frame Rate") displayed beneath them in
  small text; FPS options renamed to include unit ("30 FPS", "15 FPS", etc.)
- Enhanced view #3: "Real Feed" renamed to "Actual Feed"
- Enhanced view #4: "Stepped Feed" renamed to "Adapted Quality"; only shown
  when adaptive_quality config toggle is on OR when the user has manually
  picked from the Resolution/Frame Rate dropdowns; _manualTierActive flag
  cleared when Auto button is pressed; CFG_ADAPTIVE_QUALITY injected from
  Python into JS at build time
- Enhanced view #5+#6: snap_loop now uses the ffmpeg pipeline (not
  http_snap_loop) when native_res=True (enhanced view) and the camera has
  a working stream_url (RTSP); fixes Resolution/FPS controls having no
  effect and fixes the 1fps ceiling — cameras with working RTSP now get real
  video in enhanced view; cameras with only HTTP snap (e.g. the Microseven) still use
  http_snap_loop in enhanced view as RTSP is not available on that camera
- Bumped to 2.2.0 (first double-digit minor version)

## 2.1.9
- ONVIF SOAP WS-Security: expanded all abbreviated namespace URIs (were using
  '...' placeholders causing malformed XML); added wsu:Timestamp block; added
  SOAP 1.1 fallback (text/xml) after SOAP 1.2 returns HTTP 400 — fixes
  Hikvision GetProfiles returning 400 Bad Request
- http_snap_loop: added one-time redirect probe (Options 1+2) — sends initial
  request with allow_redirects=False, detects http→https redirect, follows
  manually so Authorization header survives to final URL; upgrades stored
  snap_url to https:// permanently for the loop lifetime
- http_snap_loop: ffmpeg fallback (Option 4) — after 60 consecutive failures
  with no frame ever received, clears http_snap_url and exits; next
  handle_snapshot call will restart snap_loop on the ffmpeg path using the
  confirmed working RTSP URL
- Log level: replaced single LOG_LEVEL dropdown with four independent boolean
  toggles (Log Debug, Log Info, Log Warning, Log Error) in HA Config tab;
  uses _LevelFilter class to pass only enabled levels; all four read from
  environment at startup; LOG_DEBUG default off, LOG_INFO/WARNING/ERROR on
- config.yaml: all boolean options now default to false (low_fps_mode,
  limit_threads, adaptive_quality changed from true); added boot: auto so
  Start on Boot defaults enabled; log_level dropdown replaced by four booleans
- translations/en.yaml: added entries for log_debug/log_info/log_warning/
  log_error with descriptive text (Most detailed / Standard / Somewhat
  detailed / Least detailed); removed log_level entry
- System status icon tooltip: changed from "Home" to "System Stability — See Logs"

## 2.1.9
- Log level now configured in the HA add-on Configuration tab (log_level option:
  DEBUG / INFO / WARNING / ERROR, default INFO); takes effect on next add-on
  restart; reads LOG_LEVEL env var alongside all other CFG_ options at startup
- Removed the log level dropdown that was incorrectly placed in the AnyCam web
  UI header; POST /api/log_level endpoint retained for internal use
- config.yaml: added log_level option (default INFO) and schema entry
  list(DEBUG|INFO|WARNING|ERROR)
- Bumped version to 2.1.9 (was held at 2.1.8 in error across multiple releases)

## 2.1.8
- http_snap_loop: add HTTP Digest auth support — two-step flow: sends Basic auth
  first, detects WWW-Authenticate: Digest response, computes RFC 2617 MD5 Digest
  response and retries; fixes Hikvision ISAPI snapshot endpoint which rejects
  Basic auth (consistent with RTSP Digest challenge already seen in probing)
- http_snap_loop: reduce log flood — consecutive 401/error warnings now logged
  on the 1st failure and every 30th thereafter instead of every second
- UI: add Log Level dropdown to the header bar (DEBUG / INFO / WARNING / ERROR);
  calls POST /api/log_level and takes effect immediately without restart; shows
  toast confirmation on change
- api_set_log_level: new handler for POST /api/log_level; adjusts anycam logger
  level at runtime; library loggers (aiohttp, asyncio) remain at WARNING
- http_snap_loop: use TCPConnector(ssl=False) so cameras that redirect HTTP→HTTPS with a self-signed certificate (e.g. Hikvision on newer firmware) are served correctly; previously every snap attempt failed with SSLCertVerificationError
- STREAM_DB: populated snap URLs for microseven (/tmpfs/snap.jpg, confirmed
  from official ha.ivanfm.com source) and sricam/ipcam/generic (/tmpfs/snap.jpg,
  same hi3510/hi3516 chipset); Reolink snap set to CGI endpoint with URL-param
  auth; 13 new manufacturers added (Wansview, Eufy, Vstarcam, Honeywell,
  Arecont, Swann, FLIR, Digital Watchdog, Ubiquiti UniFi, Hiseeu, Sony,
  IQinVision, Verint); 200+ sources researched
- api_set_credentials: reads snap from STREAM_DB immediately after
  _match_stream_db() — no network call, pure dict lookup — and stores it as
  http_snap_url in the camera dict; also stores http_snap_auth_mode (basic or
  query_params for Reolink) and runs ONVIF GetSnapshotUri as secondary source
  when DB has no snap entry
- onvif_get_snapshot_uri(): new helper calling ONVIF GetSnapshotUri SOAP
  action; used only as fallback when db_entry is None or snap is None
- http_snap_loop(): new async function replacing the ffmpeg snap loop for
  cameras with a confirmed HTTP snapshot URL; polls at ~1 fps via aiohttp,
  writes JPEG bytes to _SNAP[camera_id]["frame"] — exactly the same buffer
  that handle_snapshot reads, so card view machinery is untouched; idle timeout
  30 s; supports both HTTP Basic auth (default) and URL-param auth (Reolink)
- snap_loop: routes to http_snap_loop when camera["http_snap_url"] is set,
  otherwise runs existing ffmpeg loop unchanged
- Logging: changed root logger level to DEBUG so all log.debug() calls are
  now visible (ONVIF parse errors, probe detail, SSDP/mDNS failures, snap
  frame counts, etc.); aiohttp, aiohttp.access, aiohttp.server, and asyncio
  library loggers suppressed to WARNING to avoid library noise drowning out
  camera events

## 2.1.7
- Fix A: RTSP transport auto-detection for non-compliant cameras. Many cheap/
  generic ONVIF cameras (Microseven, Sricam, etc.) accept the TCP RTSP SETUP
  request but reply with UDP in the Transport header. ffmpeg logs this as
  'Nonmatching transport in server reply' which surfaces as 'Invalid data found
  when processing input' — the exact error seen on camera 10.0.0.22 since
  version 2.1.0. After 3 consecutive 0-frame failures with TCP transport,
  snap_loop now automatically switches to UDP and resets the streak counter.
  If UDP also fails 3 times, it reverts to TCP. The preferred_transport is
  stored in the camera dict at runtime (not persisted to cameras.json — it
  rediscovers the right transport each addon start, which takes under a minute).
- Audit due at 2.1.9 (every other release per project rules, issues 2-10).

## 2.1.6
- Removed go2rtc entirely. go2rtc was introduced in v1.4.1 as a streaming
  backbone but was never wired to serve frames — snap_loop and handle_stream
  already use ffmpeg directly. go2rtc was running as a sidecar process that
  held the camera's single RTSP connection indefinitely, preventing snap_loop's
  ffmpeg from connecting and producing "Invalid data found when processing
  input" on cameras with a one-session limit (confirmed: 10.1.1the Microseven).
  The camera was working in v1.8.0–v1.8.3 precisely because go2rtc was
  absent in that session — snap_loop was the only RTSP client.
- Removed from Dockerfile: go2rtc binary download, BUILD_ARCH arg, wget
- Removed from run.sh: go2rtc config write, background start, readiness poll
- Removed from Python: go2rtc_add, go2rtc_remove, go2rtc_source,
  go2rtc_register_all functions; GO2RTC_PORT/RTSP_PORT/API constants;
  _go2rtc_streams global; all call sites at credential entry, camera
  deletion, ONVIF re-auth, and startup
- Retained: _fix_codec background task (runs ffprobe after ONVIF credential
  entry to detect real codec when ONVIF misreports it, e.g. "h264" vs hevc)

## 2.1.5
- Fix (definitive): Card view thumbnail polling now always uses stream_url
  (the main RTSP stream), never sub_stream_url. This is the root cause of the
  persistent the Microseven camera "Stream Unavailable" issue. History:
    - v1.7.7 introduced sub_stream_url usage for card thumbnails (CPU saving)
    - v1.8.7 implemented proper ONVIF multi-profile discovery, which for the
      first time populated sub_stream_url on the Microseven camera with the MJPEG
      sub-stream at /12
    - handle_snapshot then tried to use /12 for thumbnails — this failed because
      the sub-stream uses different codec/transport than the main stream
    - The camera had been working in card view before v1.8.7 because it was
      discovered via the db_probe path which never set sub_stream_url, so
      handle_snapshot fell through to stream_url (/11)
  sub_stream_url is now exclusively for the enhanced view adaptive focus ladder.
  The thumbnail loop already runs at fps=10,scale=640:-2, so the main stream
  is perfectly fine for card view on any hardware.

## 2.1.4
- Fix: ONVIF sub_stream_url is now always saved from the lowest-resolution ONVIF
  profile regardless of whether probe_rtsp succeeded. ONVIF authentication already
  confirms the URL and credentials are valid — "Connection reset by peer" from
  probe_rtsp just means the camera was busy during probing, not that the URL is
  broken. The v2.1.1 probe_ok filter was too aggressive and left cameras like
  10.0.0.22 with no sub_stream_url, forcing snap_loop to hammer an inaccessible
  4K main stream instead of using the manageable 1280x720 sub-stream.
- Fix: After accepting ONVIF credentials, AnyCam now runs ffprobe on the real
  stream 2 seconds later to detect the actual codec. ONVIF often reports "h264"
  when the camera is actually streaming HEVC. If ffprobe disagrees with ONVIF,
  the stored stream_codec is corrected so snap_loop builds the right ffmpeg
  pipeline. This runs as a background task so it does not block the response.
- Fix: After 5 consecutive 0-frame failures in snap_loop, the stored stream_codec
  is cleared at runtime so ffmpeg can auto-detect the codec on the next attempt.
  This catches cases where the codec correction task hasn't run yet or failed.
- Fix: build_authenticated_url now properly percent-encodes credentials per
  RFC 3986 §3.2.1 so special characters in passwords don't corrupt the URL.
  Uses urllib.parse.quote with the standard userinfo safe character set.

## 2.1.3
- Fix: fps display in enhanced view no longer flashes to 0 during the 2-second
  restart gap when H.265+ or other streams crash and restart. A 4-second grace
  period now holds the last known fps; after 4s with no new frames it shows
  measuring... instead of 0 fps.
- Includes all fixes from 2.1.2 (sub-stream fallback, Resolution/FPS dropdown
  appearance, _loadFocusProfiles empty-guard, startup migration).

## 2.1.2
- Fix: snap_loop now detects persistent sub-stream failures. After 5 consecutive
  0-frame restarts while polling the sub_stream_url, it permanently falls back
  to stream_url (main stream) and clears sub_stream_url from the camera record.
  This resolves the Microseven camera hammering /12 indefinitely when that stream
  rejects connections with "Invalid data found when processing input".
- Fix: load_cameras() startup migration now clears sub_stream_url entries that
  are identical to stream_url (degenerate duplicate saved by old code).
- Fix: _loadFocusProfiles() no longer clears the Resolution select's placeholder
  option when the server returns an empty profiles list — the "Resolution…"
  placeholder is preserved until real profile data is loaded.
- Fix: handle_focus_profiles produces meaningful dropdown labels even for cameras
  without full stream metadata (codec, resolution). Falls back to "Stream N"
  labeling instead of "Profile N".
- Fix: focus-select dropdowns now use a custom SVG down-arrow instead of the
  browser native dropdown indicator, which renders as ▲ in HA's ingress webview
  instead of the expected ▼. Selects now have appearance:none and explicit
  padding-right to accommodate the inline SVG arrow.

## 2.1.1
- Fix: AnyCam no longer creates a camera card for the HA Supervisor's Docker
  bridge IP (172.30.32.1) which mDNS was incorrectly discovering as a camera.
  All 172.x.x.x and 169.254.x.x addresses are now filtered out of the live
  host list before port scanning begins.
- Fix: AnyCam no longer probes its own ingress port (8099) on the Pi's local
  IP as a potential camera. _probe_host_port now skips port==PORT on local IP.
- Fix: Port Scan tab now shows all discovered live hosts after a network scan.
  ARP_HOSTS was defined but never populated — run_scan now fills it from nmap
  results plus any silent live hosts immediately after the focused port scan.
- Fix: sub_stream_url is now only stored when probe_rtsp actually succeeded
  for that ONVIF profile. Previously, a profile that returned "Connection reset
  by peer" was still saved as sub_stream_url, causing handle_snapshot to hammer
  a broken URL indefinitely (seen on 10.0.0.22 SecondStreamProfile /12).
- Fix: enhanced view bottom bar, Resolution/FPS dropdowns, red X close button,
  Auto button, and focus controls JS (focusPickRes, focusPickFps, focusResetAuto,
  _loadFocusProfiles) all confirmed at v2.0.5 state from session transcript v10.

## 2.1.0
- Restored all core functions lost in the v2.0.6 duplicate-section removal:
  build_authenticated_url, go2rtc_add, go2rtc_remove, go2rtc_source,
  go2rtc_register_all, probe_stream_details, _drain_stderr,
  _try_hevc_plus_fallback, run_scan, _probe_host_port, handle_stream,
  handle_stream_test — the app could start but crashed on any scan,
  credential entry, or stream operation without these
- Reconstructed from session transcripts to match original implementations:
  go2rtc_add uses correct PUT API (params=name, data=url body); go2rtc_source
  is sync and builds ffmpeg:// wrapper URLs for HEVC cameras; run_scan uses
  the real _probe_host_port helper for per-port probing; build_authenticated_url
  uses the "credentials" field (not "creds") consistent with api_set_credentials
- Added _go2rtc_streams tracking set and BLACKLIST global (loaded from disk)
- probe_stream_details uses ffprobe only (simpler and more reliable than
  going through go2rtc's stream API)
- handle_stream: real MJPEG proxy via ffmpeg pipe with hw decode support,
  proper _drain_stderr integration, and CRLF boundary headers
- _probe_host_port: real per-port probing helper supporting RTSP, MJPEG,
  HLS, RTMP, WebRTC, WS-RTSP with saved credential re-use
- _try_hevc_plus_fallback: uses "credentials" field, tries sub_stream_url
  then Hikvision path-convention sub-stream mapping
- run_scan: real 4-stage implementation preserving user-saved cameras,
  merging multicast-only ONVIF/SSDP results, and excluding BLACKLIST entries

## 2.0.7
- Fix: NameError crash on startup — _probe_hw_decoders() was only defined
  in the duplicate code section that was removed in v2.0.6. The function
  probes v4l2m2m and vaapi decoder availability at startup and populates
  _HW_UNAVAILABLE so snap_loop skips unavailable decoders. Re-implemented
  as a proper module-level async function before main().

## 2.0.6
- Post-upgrade scan now re-runs the full ONVIF authentication flow for any
  saved ONVIF camera that has stored credentials. Previously the verification
  scan only re-probed stream reachability; credential-dependent fixes (such
  as the v2.0.4 XAddrs dual-URL fix for the Hikvision PTZ) only took effect
  if the user manually re-entered credentials via Connect Camera. Now on
  every version update, ONVIF cameras with stored credentials are silently
  re-authenticated, stream profiles are refreshed, and stream_url /
  sub_stream_url / stream_profiles are all updated in-place. If re-auth
  fails (e.g. camera unreachable), the camera falls back to the existing
  basic RTSP probe and is marked unverified_after_upgrade as before.

## 2.0.5
- Fix: enhanced view adaptive ladder now correctly steps down when the stream
  produces 0 frames regardless of run duration. Previously, when ffmpeg hit
  its 30-second read timeout with zero frames decoded, run_dur ≈ 30s caused
  fast_death = (30s < 8s AND 0 < 15) = False, locking the tier as "stable"
  and displaying "0 fps" indefinitely. Now frames==0 is always treated as
  unstable and forces a step-down regardless of how long ffmpeg ran.
- Add: Resolution and FPS dropdowns in enhanced view bottom bar. The
  Resolution dropdown is populated from the camera's discovered stream
  profiles (e.g. "3840x2160 HEVC", "1280x720 H264"). The FPS dropdown
  offers Uncapped / 30 / 20 / 15 / 10 / 5 / 2 / 1 fps. Selecting from
  either locks the adaptive controller to that tier immediately (manual
  override). The Auto button clears the override and resumes adaptive
  stepping. A new server endpoint POST /snap/focus/tier accepts
  {profile_idx, fps} and GET /snap/focus/profiles returns profile metadata
  for the currently focused camera.
- Style: X close button in enhanced view is now a red circle (border: 2.5px
  solid #e03) positioned top-right at 10px/14px, styled to hover-fill red.
  No longer overlaps the camera's built-in OSD timestamp.
- Style: enhanced view info and controls moved to a fixed 52px bottom bar
  (dark background, border-top) keeping them separate from the video feed.
  Info text (camera name, real/stepped feed stats) on the left; dropdowns
  and Auto button on the right.

## 2.0.4
- Critical fix: ONVIF credential flow now works correctly for cameras that
  advertise multiple XAddrs in WS-Discovery (both IPv4 and IPv6 link-local).
  Previously, _onvif_media_url() joined all addresses into one space-separated
  string and passed the entire garbage value to the SOAP request, causing
  onvif_get_profiles() to return 0 profiles even with correct credentials.
  Now the function splits XAddrs on whitespace, skips IPv6 link-local (fe80::)
  addresses, and picks the first valid IPv4 http:// address. This fixes the
  Hikvision DS-2DE4A425IW-DE (10.0.0.33) which advertises both addresses.

## 2.0.3

### Bug fixes
- **Duplicate snap_loop after focus exit (root cause fix)**: `snap_loop`'s `finally` block was unconditionally setting `state["task"] = None` when any loop exited. When the lingering focus task finally wound down after being cancelled, it would overwrite the new card-view loop that `handle_snapshot` had already started — causing `handle_snapshot` to start yet another loop. Fixed by only clearing `state["task"]` when it still points to the current task, leaving any newer loop untouched.
- **Sub-stream codec mismatch**: when `handle_snapshot` starts a snap_loop against the sub-stream URL, it now passes a `snap_camera` dict with the sub-stream's codec/resolution instead of the main stream's. Without this, `snap_loop` would configure ffmpeg for e.g. H.264 while decoding an MJPEG sub-stream, causing "Invalid data found" errors.
- **Exponential backoff on 0-frame failures**: when ffmpeg repeatedly dies with 0 frames decoded (bad URL, wrong codec, camera rejecting connection), the restart delay now backs off exponentially: 2s → 4s → 8s → 16s → 32s (capped). Normal failures that produced at least one frame still restart at 2s. This prevents continuous hammering of a broken sub-stream URL.

## 2.0.2

### Improvement
- **Adaptive Quality toggle**: new config option (default ON) controls the adaptive profile/fps ladder in enhanced view. When OFF, enhanced view always uses the highest quality camera profile at uncapped fps with no stepping. When ON (default), the ladder steps through real camera profiles and fps tiers to find a stable setting.

## 2.0.1

### Bug fix
- **Hardware decode: use best available decoder for codec, not hardcoded Pi names** — `snap_loop` was hardcoding `hevc_v4l2m2m` / `h264_v4l2m2m` as the only hw decoder candidates. On Intel/AMD hardware, `hevc_vaapi` / `h264_vaapi` would be detected as available by `_probe_hw_decoders()` but never actually used. Now selects the best available decoder from `_HW_DECODER_CANDIDATES` (v4l2m2m preferred over vaapi) based on stream codec and probe results.

## 2.0.0

### Improvements
- **Multi-profile stream ladder**: ONVIF discovery now stores all discovered profiles as `stream_profiles` (sorted highest→lowest resolution). The adaptive focus ladder steps through real camera profiles instead of fake ffmpeg post-decode scaling. Switching to a lower profile genuinely reduces CPU decode pressure because the camera transmits fewer pixels over the network. All `scale=WxH` ffmpeg filter steps removed.
- **Hardware decode**: added `full_access: true` to config.yaml so the container can access all host hardware devices. At startup, `_probe_hw_decoders()` checks each candidate decoder (hevc_v4l2m2m, h264_v4l2m2m, hevc_vaapi, h264_vaapi) against both device file existence and ffmpeg compiled support, pre-populating `_HW_UNAVAILABLE` so snap_loop never attempts unavailable decoders. On non-Pi hardware, Pi-specific devices simply won't exist and are silently skipped.

### Notes
- Pi 4/5 hardware HEVC decode requires `dtoverlay=rpivid-v4l2` in `/boot/firmware/config.txt` and a reboot. Once set, the add-on will detect and use `hevc_v4l2m2m` automatically.
- Cameras discovered before v1.9.3 will not have `stream_profiles` stored — the ladder falls back to `stream_url`/`sub_stream_url` for those until re-discovered.

## 1.9.2

### Improvement
- **Resolution and codec detection: highest-wins arbitration** — when both probe (`probe_stream_details`) and ONVIF (`GetProfiles` XML) report values, whichever is "higher" is used. Buggy firmware tends to underreport (e.g. H264 for an HEVC stream, or 0x0 when probing fails), never overreport, so the higher value is almost always correct.
  - Resolution: highest pixel area wins (probe_w×probe_h vs onvif_w×onvif_h).
  - Codec: ranked by capability (hevc > h264 > mjpeg > mpeg4), higher rank wins.
  - Audio: ONVIF wins unconditionally (probe rarely detects audio correctly).
  - FPS: probe wins unconditionally (ONVIF's FrameRateLimit is a ceiling, not measured).
  - Log now shows `[probe]` or `[onvif]` source tag for resolution and codec per profile.

## 1.9.1

### Bug fixes
- **Post-exit step-down**: adaptive controller was firing after focus was already cleared (ffmpeg EOF fired after task.cancel() landed late). Guarded the entire step-down block with `_FOCUSED_CAMERA == camera_id` so it's a no-op once focus exits.
- **Stale restart count on re-entry**: `restarts_since_lock` was preserved across focus sessions, causing a premature step-down immediately on re-entering focus. Reset to 0 (along with `run_start`) in `handle_focus_set`.
- **Codec detection**: reverted codec to probe-first, ONVIF encoding as fallback only. ONVIF's `VideoEncoderConfiguration/Encoding` is unreliable on some Hikvision cameras (reports H264 for HEVC bitstreams). Probe reads the actual bitstream and is authoritative for codec.

### Improvements
- **Enhanced view info bar**: split into two sections — `Real Feed: WxH · fps` (measured from actual received frames) and `Stepped Feed: WxH · fps` (current adaptive ladder tier from server headers `X-Step-Res` / `X-Step-FPS`). Removed codec name and "full quality" text.
- **Uncapped tier restored**: each resolution block in the adaptive ladder now starts with an uncapped fps tier before stepping down through 30→1fps.

## 1.9.0

### Improvement
- **Adaptive ladder: fps-first per resolution block, every integer 30→1**: Each resolution block descends by 1fps per restart (30fps → 29fps → ... → 1fps) before the resolution drops. For a 4K camera with intermediate scales and a sub-stream, the ladder is:
  - 4K native: 30fps → 29fps → ... → 1fps  (30 tiers)
  - 1920px scaled: 30fps → ... → 1fps       (30 tiers, if native ≥ 2880px)
  - 1280px scaled: 30fps → ... → 1fps       (30 tiers, if native ≥ 1920px)
  - sub-stream: 30fps → ... → 1fps          (30 tiers)
  Each tier change requires exactly 1 restart at the current locked tier. Previous design had sparse fps steps and a resolution-first ordering that didn't match intended behavior.

## 1.8.9

### Bug fixes
- **Adaptive ladder: resolution-first ordering** — the quality ladder was fps-first (all 11 fps tiers at native res, then all 11 at 1920px, etc.), so stepping down always reduced fps at the same resolution before ever trying a lower resolution. Getting from 4K native to 1920px scaled required stepping through 11 fps tiers × 3 restarts = 33 restarts. The ladder is now resolution-first: for each fps tier, all available resolutions are tried before moving to the next fps tier. A single step-down now moves to the next lower resolution at the same fps, matching the expected behavior.
- **Adaptive restart limit reduced to 1** — with resolution-first ordering, a single restart at a locked tier is sufficient signal to try the next lower resolution. Limit of 3 was designed for fps-stepping (where you wanted confidence before giving up), not resolution-stepping (where each failure is immediate evidence the current quality doesn't work).
- **H.265+ flag reset on re-discovery** — `hevc_plus_warning` is now always reset to False when a camera is re-discovered, so stale flags from previous sessions or from when the wrong stream URL was stored don't carry forward. The flag is still set at runtime by `_drain_stderr` if ffmpeg genuinely encounters a multi-layer HEVC bitstream. Note: some Hikvision firmware versions output multi-layer HEVC bitstream even when H.265+ is set to OFF in the camera UI — this is a firmware bug, not a detection bug.

## 1.8.8

### Improvement
- **ONVIF as authoritative source for resolution, video codec, and audio codec**: `onvif_get_profiles` now also parses `AudioEncoderConfiguration/Encoding` from the GetProfiles XML. All three fields (resolution, video codec, audio codec) are now applied unconditionally from ONVIF data — probing via `probe_stream_details` (go2rtc/ffprobe) only contributes measured FPS, which ONVIF's `FrameRateLimit` ceiling doesn't accurately reflect. This removes the previous inconsistency where codec was "ONVIF if probe fails, else probe."

## 1.8.7

### Improvement
- **ONVIF profile selection: authoritative resolution source** — `probe_stream_details` (go2rtc/ffprobe) was the sole source of truth for resolution+codec when building the stream candidate list, but it frequently fails for H.265 streams, returning 0×0 and causing those streams to sort below lower-res streams that probe successfully. ONVIF's `GetProfiles` response already contains `VideoEncoderConfiguration/Resolution` and `Encoding` — this data is always present and never fails. Changed strategy: ONVIF is now the authoritative source for resolution and codec; probing is still used for actual FPS (which ONVIF's `FrameRateLimit` doesn't accurately reflect) and audio codec. This ensures H.265 main streams sort correctly above MJPEG sub-streams regardless of probe success.

## 1.8.6

### Bug fix
- **ONVIF profile resolution — wrong stream selected as main**: `probe_stream_details` often fails for H.265 streams (codec negotiation timeout), returning 0×0. When sorting ONVIF profiles by detected resolution, those streams sank to the bottom, causing a lower-resolution sub-stream that probed cleanly (e.g. MJPEG 704×480) to be selected as `stream_url`. Fix: `onvif_get_profiles` now also parses `VideoEncoderConfiguration/Resolution` and `Encoding` directly from the ONVIF XML — this data is always present and reliable. When `probe_stream_details` returns no resolution, the ONVIF-reported value is used instead, ensuring the sort is correct and the main high-res H.265 stream is properly selected as `stream_url`. Codec is also seeded from the ONVIF encoding field when probing fails.

## 1.8.5

### Improvements
- **Adaptive controller: restart-count instability** — added second instability signal alongside the existing fast-death check. If a locked tier restarts ≥3 times (regardless of individual run duration/frame count), the tier is considered unreliable and the controller unlocks and steps down. This catches cameras that drop RTSP connections on a regular cycle (e.g. every 20s) — previously these would lock as "stable" immediately and stay there forever.
- **Adaptive controller: intermediate resolution steps** — the quality ladder now includes scaled variants of the main stream (1920px-wide, 1280px-wide) inserted between the native-res fps tiers and the sub-stream. These are only added when the camera's native width is ≥1.5× the scale target (so never upscales). For a 4K camera this gives 4K→1920px→1280px→sub-stream instead of jumping straight from 4K to 480p.
- **Focus status bar: no more "0 fps"** — the JS FPS window now distinguishes three states: `X fps` (frames flowing), `buffering…` (had frames before, currently reconnecting), `connecting…` (waiting for very first frame). Previously any 1-second window with no new frames would show "0 fps".

## 1.8.4

### Bug fixes
- **Focus exit race — duplicate snap_loop tasks**: `task.cancel()` alone is unreliable when ffmpeg is streaming 4K data at high throughput — the event loop can't deliver `CancelledError` until a `read()` suspends, which may never happen while the pipe is full. Fixed by having `snap_loop` poll `_FOCUSED_CAMERA` itself: an explicit check at the top of the outer restart loop and after each frame parsed in the inner loop causes the native-res task to exit cleanly within one frame of focus being cleared. This eliminates the duplicate "idle 30s — stopping" entries and 4K frames arriving after focus exit seen in the log.
- **Idle timeout during focus (freeze bug)**: `_snap_last_access[camera_id]` was updated *after* the focus guard early-return in `handle_snapshot`, so the native-res snap_loop's idle timer never got reset while in focus mode. Loop died after 30s → screen froze. Fixed by updating `_snap_last_access` before the early return.
- **FPS display accuracy**: Focus status bar was measuring frame *delivery* rate (~16fps from 60ms polling) rather than actual frame *production* rate from ffmpeg. Switched focus poller from `Image()` to `fetch()` so the `X-Frame-Count` response header can be read; only increments the fps counter when the server-side frame count actually changes. Displayed fps now reflects true ffmpeg output rate (e.g. 3fps for 4K HEVC).

## 1.8.3

### Bug fix
- **Critical crash fix**: `UnboundLocalError: cannot access local variable 'url'` — when I added `url = effective_url` inside the `if native_res:` block of `_launch_snap`, Python treated `url` as a local variable for the entire function, making the outer closure variable inaccessible in the non-native_res branches. Fixed by using a separate `ffmpeg_url` variable instead of reassigning `url`.

## 1.8.2

### Focus view adaptive quality — complete rewrite

**Bug fixes:**
- **480p flickering eliminated**: handle_snapshot now returns the last buffered frame during focus mode instead of starting a competing 480p sub-stream task; the 2s restart gap no longer causes the quality controller to fight itself
- **FPS going "up" was an illusion**: the JS was measuring frame delivery rate from whichever task happened to be running — when the 480p task kicked in it delivered frames fast, making it look like fps increased; this is now impossible since only one task runs

**New adaptive quality ladder:**
- Ladder is now (resolution × fps) pairs, exhausting all fps tiers at the highest resolution before dropping to sub-stream resolution
- Tier order: main_stream @ [uncapped, 30, 20, 15, 10, 8, 5, 4, 3, 2, 1fps] → sub_stream @ [same fps sequence]
- Step-down only: never steps back up once stable
- Locks permanently when a stable tier is found; if a locked tier becomes unstable, unlocks and continues stepping down from there
- Per-camera memory: tier_idx and locked state persist across focus sessions within the same addon run

## 1.8.1

### Adaptive FPS — step-up removed
- **Step-down only**: adaptive fps controller now strictly steps down when a tier is unstable and locks there for the session — step-up logic removed entirely
- Once a stable fps tier is found, the controller stays there permanently until the user closes and re-opens enhanced view

## 1.8.0

### Adaptive FPS for enhanced/focus view
- **Adaptive fps controller**: focus/native_res mode now uses a self-tuning fps ladder `[uncapped, 10, 8, 5, 4, 3, 2]` to find the highest stable fps the hardware can sustain at full resolution
- **Step-down on instability**: if ffmpeg exits in under 8 seconds with fewer than 15 frames, the controller steps down one tier (slower fps) and relaunches — repeats until stable
- **Step-up on stability**: if a run lasts 45+ seconds, the controller steps up one tier (faster fps) to try recovering quality — drops back immediately if that tier is also unstable
- **Per-camera memory**: each camera remembers its best stable fps tier across focus sessions within the same addon run, so it converges faster on re-entry
- **Run_start reset on focus exit**: adaptive state cleans up cleanly when leaving focus view; tier is preserved but timing resets for next session

## 1.7.9

### Enhanced view improvements
- **Live resolution display**: status bar now shows actual decoded pixel dimensions from each JPEG frame (loader.naturalWidth × naturalHeight) rather than stored metadata
- **Live FPS display**: status bar counts real frame loads per second using a rolling 1-second window — updates every second with the true displayed frame rate
- **Status bar placeholder**: shows "loading…" on open then switches to live measurements after the first second
- **Fix: focus view was serving wrong stream**: handle_focus_set now always cancels the existing thumbnail snap_loop before starting a new native-res main-stream task — previously a running sub-stream task would block the high-quality task from starting
- **Higher JPEG quality in focus mode**: q:v lowered from 5 to 2 for focus/native_res frames (sharper output, less compression grain at 4K)
- **Config limits bypassed in focus mode**: Low FPS Mode and Limit Threads are now properly ignored when entering enhanced view; restored immediately on exit (snap_loop task cancelled on focus_clear)

## 1.7.8

### Bug fixes
- **Log link no longer duplicates HA sidebar**: clicking the status icon now navigates the top-level browser tab (window.top) instead of the ingress iframe, so the HA shell does not render inside itself
- **Status icon moved to left of title**: the green camera icon is now the leftmost element of the header h1, acting as both logo and live status indicator; the separate white camera SVG is removed
- **AnyCam — Home** added to header title text
- **Full quality in enhanced view**: when clicking a card to open focus/enhanced view, Low FPS Mode and Limit Threads config settings are bypassed for that stream so ffmpeg runs uncapped — full resolution, full fps, no thread limit

## 1.7.7

### Multi-stream handling
- **Single card per IP**: ONVIF cameras with multiple stream profiles (main + sub) now collapse into one card instead of creating separate cards per profile
- **Thumbnail uses sub-stream**: Card thumbnail polling uses the lowest-resolution stream for lower CPU/bandwidth; click-to-focus view switches to the full main stream
- **H.265+ badge suppressed**: If a working lower-res alternate stream is found, the H.265+ warning badge is automatically replaced with the green fallback badge

### Stream database
- **Embedded RTSP/MJPEG database**: Excel stream URL database converted to compact Python dict (STREAM_DB, 27 manufacturers) embedded in the addon for zero-I/O lookup
- **Post-login silent probe**: After credentials are accepted, silently probes manufacturer-specific alternate stream paths to discover streams not advertised pre-login
- **Manufacturer detection**: Heuristic matching on camera name/vendor/model to select the right URL patterns for probing

### Storage browser
- **Editable path bar**: Click anywhere on the breadcrumb bar to edit the path directly; Enter/blur navigates there; invalid paths show a popup then restore the previous valid path
- **Up arrow one level**: Up button now navigates one directory level up (not just to root); greyed at the ceiling (/media/anycam in scoped mode, / in unrestricted mode)
- **Unrestricted browser toggle**: New config option `unrestricted_storage_browser` (default: false) allows navigating the full filesystem instead of just /media/anycam

### Status indicator
- **Camera icon replaces dot**: The status indicator is now a camera SVG icon (50% larger, same shape as the AnyCam logo) with no fill — only the stroke colour changes
- **AnyCam text shifted right**: ~26px extra spacing between the logo icon and the AnyCam text for cleaner separation  
- **Same-tab log navigation**: Clicking the status icon now navigates in the same tab to Settings → Apps → AnyCam → Log (correct HA path), not a new tab

# AnyCam — Changelog

## 1.7.6
- Critical fix: storage view was rendering the OLD card-based layout instead
  of the Windows Explorer grid because two renderStorage() functions existed
  in the JS — the second (old) definition overrode the first (new) one.
  Old renderStorage removed; only the correct one calling _renderStorageView()
  remains.
- Critical fix: initSnaps() was defined twice; duplicate stub removed.
- Storage folder rows: entire row is now single-clickable to navigate into
  the folder (not just the name text); double-click on name still renames.
  Row cursor set to pointer to signal clickability.
- Storage file rows: single-click on filename now triggers a browser download
  of the MP4/MKV clip directly; double-click on filename still opens rename
  prompt. Download icon (⬇) in the actions column still works as before.
- Storage path breadcrumb now correctly shows /media/anycam (blue, clickable
  back to root) with › folder-name (bold) when inside a folder, and plain
  /media/anycam at root. _updateNavButtons() drives the display correctly.
- Forward button remains absent from nav bar (Windows Explorer style).

## 1.7.5
- Connect Camera page: Back button now matches all other pages — 'Back to
  Cameras' with btn-ghost btn-sm class, positioned at the top of the view
  before the form heading, identical to Port Scan and Storage pages
- Storage file browser nav bar: removed Forward button (Windows Explorer
  style only shows Back and Up); replaced path text box with a clickable
  breadcrumb: /media/anycam (blue, clickable → root) › folder-name (bold)
- Storage folder rows: type column now shows 'File folder' to match
  Windows Explorer; folder icon sized consistently at 1rem

## 1.7.4
- H.265+ automatic fallback for Hikvision (and compatible) cameras:
  When snap_loop detects the "Multi-layer HEVC coding is not implemented"
  ffmpeg error (set as camera["hevc_plus_warning"] = True by _drain_stderr),
  it now probes a prioritised list of alternate RTSP URLs on the FIRST
  restart after the flag appears:
    1. /Streaming/Channels/102 — Hikvision sub-stream (standard H.265,
       lower resolution, almost always decodable by ffmpeg)
    2. /Streaming/Channels/101?videoCodecType=H.264 — requests server-side
       transcode to H.264 (firmware-dependent, works on many models)
    3. /ISAPI/Streaming/channels/102 — ISAPI path sub-stream variant
    4. /ISAPI/Streaming/channels/101?videoCodecType=H.264 — ISAPI transcode
  Each candidate is probed with a raw RTSP OPTIONS socket call (4s timeout).
  The first URL that returns RTSP/1.0 200 is adopted; snap_loop switches to
  it, updates camera["stream_url"] in CAMERAS and saves to cameras.json,
  clears hevc_plus_warning, and sets hevc_plus_fallback_active = True.
  On subsequent addon restarts the camera loads from the fallback URL
  directly — no further probing needed.
  If no fallback URL responds, snap_loop continues with the original stream
  and -err_detect ignore_err extending stream life as before.
- Card badge updated: when hevc_plus_warning clears and fallback is active,
  the red "⚠ H.265+" badge is replaced by a green "✓ H.265+ fallback" badge
  confirming the automatic switch succeeded.
- New function _try_hevc_plus_fallback(camera_id, camera, orig_url) encapsulates
  all probe logic; easily extensible to other manufacturers using similar
  proprietary SVC codec extensions.

## 1.7.3
- Status dot moved inside <h1> tag, right after AnyCam text, so it appears
  immediately next to the logo as shown in screenshot (not as a separate
  sibling element after the h1)
- Status dot onclick now opens the real HA addon log page
  (/hassio/addon/camera_discovery/logs) in a new tab; event.stopPropagation()
  prevents the h1 click-home handler from also firing
- Storage view redesigned as Windows Explorer style:
  - Separate dark nav bar removed; nav controls (← → ↑ + path breadcrumb)
    now embedded inside the white explorer pane as its top toolbar
  - White (#fff) background for the file pane with light grey column headers
  - Four columns: Name | Date Modified | Type | Size — sortable by clicking
    any header, with ascending/descending arrow indicator
  - Folders listed at top, files below; hover highlight (#cce8ff, Windows blue)
  - Action buttons (⬇ download, 🗑 delete) appear on row hover only
  - Double-click any name to rename inline; folder rows navigate on single click
  - Drag-and-drop file move built with DOM event listeners (no quote collisions)
  - Empty state watermark centered in the white pane
- H.265+ warning badge: amber ⚠ H.265+ badge appears on Hikvision camera cards
  when the stream is detected as multi-layer HEVC (Hikvision proprietary codec);
  tooltip explains the fix (change H.265+ → H.265 in camera web UI)
- Removed duplicate "Not found after upgrade" badge that was rendering twice

## 1.7.2
- Remove (Option X) labels from Config tab toggle names
- Status dot colors: yellow (#f9c700) for warnings, red (#e53935) for errors,
  green (#43a047) for all clear — previously used orange for warnings
- Status dot click: opens actual HA addon log page at /hassio/addon/camera_discovery/logs
  in a new tab, rather than a custom in-app log view
- Removed custom System Log view and renderLogView JS — HA's built-in log is better
- Camera web page button: 🌐 button on each ready camera card; opens a modal offering
  to open the camera's IP in a new tab (always available) or in Firefox (if installed);
  Firefox detection probes /hassio/addon/firefox; if not installed, shows a Get It
  prompt linking to /hassio/store with a note to add mincka/ha-addons repository;
  if installed, opens the Firefox ingress panel in a new tab with a toast reminding
  the user to navigate to the camera IP manually (Firefox ingress does not accept
  URL injection from external callers)

## 1.7.1
- Fixes and improvements following 1.7.0 testing:

- Fix scan cancel logic: _SCAN_CANCELLED check at stage 1→2 was misplaced
  inside a nested if block causing IndentationError; fixed to check at top
  of each stage boundary as intended
- Add _LOG_BUFFER + _BufHandler: in-memory circular buffer of last 200
  WARNING/ERROR log entries; enables status dot to read health without
  requiring any external log access
- Status dot: green/amber/red circle next to AnyCam logo in header; polls
  /api/logs every 5s; amber = recent warnings, red = recent errors, green
  = all clear; click dot to open log view; title "System Stability"
- Logs view: new view showing recent warning/error entries in monochrome
  terminal style; accessible via status dot click or logs view
- AnyCam logo: clickable (→ cameras view) with title "Home"
- Scan cancel button: red × Cancel button appears in status bar while scan
  is running; POST /api/scan/cancel requests graceful abort at stage
  boundaries (between stages 1→2 and 3→4)
- Storage view: path navigation bar with Back / Forward / Up buttons and
  breadcrumb path display; root view shows camera folders as grid cards;
  folder view shows files as list; double-click name to rename
- Storage view: Back to Cameras button added
- H.265+ detection: when Hikvision (or other) camera streams in multi-layer
  HEVC (H.265+), _drain_stderr detects the ffmpeg "Multi-layer HEVC coding
  is not implemented" message and sets camera["hevc_plus_warning"] = True;
  Fix: camera web UI → Video → Encoding → change H.265+ to H.265
- -err_detect ignore_err added to snap_loop ffmpeg command; extends stream
  life before crash when camera sends malformed/partial HEVC frames
- Folder naming uses dashes not underscores (e.g. mainStreamProfile →
  main-stream-profile, hikvision-ds2de4a425iw-de)
- Focus view: image now fills full viewport (100vw × calc(100vh-50px)) with
  object-fit:contain; previously only occupied a small portion of screen
- Config descriptions added for all options A-F in HA Config tab

## 1.7.0
- Performance toggles (A-F) in HA App Configuration tab:
  A: low_fps_mode (default ON) — reduces HEVC output to 2fps while ffmpeg
     still decodes at source rate; dramatically cuts CPU encode/pipe cost;
     recommended for multi-camera Pi setups
  B: skip_nonref (default OFF) — adds -skip_frame nonref to ffmpeg input;
     skips B/P frames during HEVC decode (~40% CPU reduction, slightly choppy)
  C: limit_threads (default ON) — adds -threads 2 per ffmpeg process; prevents
     any single stream from monopolizing all 4 Pi cores
  D: stagger_polling (default OFF) — offsets each camera's snap_loop start by
     40ms * camera_index to spread CPU spikes across time
  F: hw_decode (default OFF) — enables hevc_v4l2m2m / h264_v4l2m2m hardware
     decoders; works on native Pi installs with V4L2 device access; not
     available in HA Docker containers; when enabled, failed decoders are
     NOT permanently marked unavailable (retried each stream start)
  Also: recordings_path, motion_sensitivity, motion_cooldown_secs,
        motion_clip_padding_secs configurable from the HA Config tab

- Motion detection + recording: toggle per-camera with the ⏺ Record button
  on each card; uses JPEG size comparison (fast, zero extra subprocess);
  when motion detected, spawns ffmpeg -c copy directly from camera RTSP URL
  for full-quality stream-copy recording (near-zero CPU); saves to
  /media/anycam/{camera_folder}/ as motion_YYYYMMDD_HHMMSS.mp4; stops
  recording after motion_cooldown_secs + motion_clip_padding_secs of
  no motion; button shows ⏺ Record (off) / ⏺ Armed (on, no motion) /
  ⏺ REC (active, pulsing red); motion state polled every 3s

- Click-to-focus enhanced view: click any live camera feed to enter
  full-screen mode; server sets _FOCUSED_CAMERA global, all other
  snap_loops throttle to 1fps, focused camera runs at native camera
  resolution/fps; CPU warning toast shown if estimated load >80% of Pi 4
  capacity (based on codec×pixels×fps heuristic); press Escape or X to exit;
  focus endpoints: POST /snap/focus/{id}, DELETE /snap/focus

- Built-in storage browser (Storage button in header):
  Shows disk usage bar (total/used/free/percent) for /media/anycam;
  lists per-camera folders with clip count and total size; shows each
  recording with size, date, download button, delete button; supports
  drag-and-drop to move clips between camera folders; double-click any
  filename or folder name to rename inline; folder names derived from
  camera display name (strips "Generic IP Camera", "(IP)", truncates to
  30 chars, filesystem-safe); storage endpoints: GET /api/storage,
  POST /api/storage/rename, POST /api/storage/move,
  DELETE /api/storage/file, GET /api/storage/download

- Removed duplicate device count from header (scan status bar already shows it)
- Renamed "Broad sweep" to "Deeper Scan" throughout UI and log messages
- Added Storage button to header bar (between Connect Camera and right edge)
- Toast notification system for storage actions (move, delete, rename)
- /snap/status endpoint updated with focus state info

## 1.6.1
- Critical fix: _drain_stderr was defined as a nested function inside
  handle_stream, making it invisible to snap_loop; every snap_loop call
  crashed immediately with NameError on the first _drain_stderr call,
  causing rapid restart loops and zero frames delivered; promoted
  _drain_stderr to module-level so both handle_stream and snap_loop
  can call it; this is the only thing preventing camera feeds from showing

## 1.6.0
- Architectural fix: replace long-lived multipart stream with snapshot polling.
  The browser now calls GET /snapshot/{id}?t=... every 125ms via JS setInterval.
  Each request is a normal short HTTP round-trip that HA's nginx ingress proxy
  handles correctly. Previously, one long-lived multipart/x-mixed-replace
  connection was used; nginx terminates these after a short burst (typically
  10-24 frames, ~1-3 seconds), causing "client disconnected" and blank cards.
  The /stream/{id} endpoint is kept but no longer used for live display.

- New background snap_loop task per camera: one persistent ffmpeg process runs
  per camera in the background, continuously decoding HEVC/H.264 and storing
  the latest JPEG frame in _SNAP[camera_id]['frame']. handle_snapshot returns
  whatever is in the buffer instantly — no ffmpeg startup cost per request.
  snap_loop auto-restarts on ffmpeg exit/crash (2s delay). Stops automatically
  after 30 seconds of no handle_snapshot calls (idle shutdown).

- New GET /snap/status endpoint: returns JSON health info for all active
  snapshot processes — pid, frame_count, frame_bytes, frame_age_s,
  last_poll_s, restarts. Open in browser to debug without reading logs.

- probe_rtsp_socket verbose label logging: pass label=camera_id/profile_name
  to get INFO-level logging of every RTSP round-trip (OPTIONS → result,
  DESCRIBE → 401 Digest realm/nonce, DESCRIBE authenticated → result).
  Previously all steps were silent on success and debug-only on failure.
  api_set_credentials now passes label so every probe step is visible in log.

- ONVIF credential flow: probe_rtsp no longer blocks card creation.
  When ONVIF GetProfiles SOAP returns profiles (proving credentials are valid),
  profile cards are created even if probe_rtsp returns False for the stream URL.
  A warning is logged explaining this. The probe_rtsp call is kept for
  informational logging but no longer gates card creation. Previously, probe_rtsp
  returning False (e.g. due to camera quirks in Digest auth negotiation, or
  rate-limiting after repeated debug sessions) would silently discard all
  profile cards even with correct credentials.

- Snap debug logging (server-side):
    SNAP [id]: starting background process (codec=..., res=...px)
    SNAP [id]: ffmpeg starting (codec=..., hw=hw:hevc_v4l2m2m OR sw, vf=...)
    SNAP [id]: frame N — X bytes (last poll Y.Zs ago)   [every 50 frames]
    SNAP [id]: hw decode timeout/EOF → sw               [hw fallback]
    SNAP [id]: ffmpeg EOF after N frames (rc=N)          [unexpected exit]
    SNAP [id]: 30s read timeout after N frames           [ffmpeg stalled]
    SNAP [id]: idle Ns — stopping                        [idle shutdown]
    SNAP [id]: restarting in 2s (#N)                     [before restart]
    SNAP [id]: loop done                                 [final exit]

- Snap debug logging (browser-side, open DevTools Console):
    [AnyCam] snapshot error #N for camera_id            [on failed frame fetch]
    After 3 consecutive errors: placeholder shown with "Stream unavailable"
    Error backoff: 500ms for errors 1-5, 2s for errors 6+

- probe_rtsp_socket verbose logging (when label supplied):
    [probe_rtsp id/profile] OPTIONS → OK
    [probe_rtsp id/profile] DESCRIBE → 401 Digest (realm=..., nonce=...)
    [probe_rtsp id/profile] DESCRIBE (authenticated) → 200 OK / error

## 1.5.4
- Revert pixel format to yuvj420p: this ffmpeg build's mjpeg encoder
  explicitly rejects yuv420p ('Incompatible pixel format') and only accepts
  yuvj420p (full-range JPEG format); -pix_fmt yuvj420p now set explicitly
  and -color_range 2 removed (redundant with yuvj420p)
- Filter swscaler deprecation lines from stderr log: the 'deprecated pixel
  format' warning is cosmetic-only and unavoidable with yuvj420p on this
  ffmpeg build; filtered in _drain_stderr so it no longer spams the HA log
- Add frame-sent logging: logs 'sent frame 1 (N bytes)' on the first frame
  and every 100 frames thereafter, confirming data is flowing from ffmpeg
  stdout through response.write() to the HTTP layer; also logs 'client
  disconnected after N frames' on ConnectionResetError/Aborted
- Note: 10.0.0.22 RTSP probe returning False is likely camera-side rate
  limiting from repeated connection attempts during debugging; re-entering
  credentials after a brief wait should restore it

## 1.5.3
- Fix swscaler deprecated pixel format warning: adding -pix_fmt yuv420p
  tells the mjpeg encoder to accept yuv420p directly (ffmpeg 5.0+),
  bypassing the internal conversion that triggered the swscaler warning

## 1.5.2
- Add X-Accel-Buffering: no response header: HA ingress is an nginx proxy;
  without this header nginx buffers the entire multipart/x-mixed-replace
  stream in memory before forwarding it to the browser, so the browser
  receives zero frames until the stream ends — this is the primary cause
  of the 'no live feed' symptom despite ffmpeg producing frames correctly
- Track hardware decoder availability at runtime: when hevc_v4l2m2m or
  h264_v4l2m2m reports 'Could not find a valid device', the decoder name
  is added to _HW_UNAVAILABLE (module-level set); subsequent stream
  requests skip hw decode immediately instead of wasting 3 seconds per
  attempt on a guaranteed failure
- Fix deprecated pixel format warning: change format=yuvj420p to
  format=yuv420p in all vf chains and add -color_range 2 to the ffmpeg
  command; yuvj420p is deprecated in ffmpeg 5+ and triggered a swscaler
  warning on every new scale context, filling the stderr pipe with noise

## 1.5.1
- Add diagnostic logging to handle_stream: log the ffmpeg command (creds
  stripped), the sanitized RTSP URL, ffmpeg exit code, and a warning on
  every ffmpeg EOF so silent failures are now visible in the HA log
- Fix stderr drain in finally block: previously stderr_t was cancelled
  before it could flush collected lines when ffmpeg crashed quickly; now
  we kill ffmpeg first, wait for it to exit, then await stderr_t (up to
  2s) so all ffmpeg error output is always captured and logged
- Handle asyncio.CancelledError in _drain_stderr: on cancellation, attempt
  one final read of any remaining buffered stderr bytes before exiting

## 1.5.0
- Remove go2rtc from streaming hot path (Option B architectural fix)
  ffmpeg now connects directly to the authenticated camera RTSP URL and
  outputs MJPEG frames to stdout; the go2rtc RTSP restream layer
  (localhost:8554) is no longer used for streaming, eliminating the
  phantom-registration bug where go2rtc created sourceless empty streams
  because the PUT /api/streams endpoint reads the source URL from the
  'src' query parameter, not the request body as our code incorrectly sent
- Remove double-transcode for HEVC cameras: previous pipeline decoded
  HEVC inside go2rtc then re-encoded to H.264, then ffmpeg decoded H.264
  again to produce MJPEG; new pipeline decodes HEVC once directly to MJPEG
- Add -nostdin flag and stdin=DEVNULL: prevents ffmpeg from inheriting the
  Python process stdin, which could cause unexpected blocking
- Add -an flag (no audio): drops audio stream entirely, saves CPU and
  prevents audio codec warnings from filling the stderr pipe
- Switch output format from -f mjpeg to -f image2pipe: both produce raw
  concatenated JPEGs but image2pipe is the documented format for pipe output
- Fix pixel format in vf chain: format=yuv420p -> format=yuvj420p
  The JPEG full-range variant avoids a deprecation warning ffmpeg emits
  when the mjpeg encoder receives limited-range yuv420p; the warning was
  silently filling the stderr pipe and could contribute to stderr deadlock
- Fix stderr deadlock: stderr is now drained by a concurrent asyncio Task
  (_drain_stderr) that reads stderr line-by-line throughout the stream;
  previously stderr was only read in the finally block after streaming
  ended, so any stderr output during streaming could fill the 64KB OS pipe
  buffer, blocking ffmpeg from writing to stdout, causing 30s timeouts
- Define SOI/EOI/CRLF as bytes([...]) literals in handle_stream: removes
  all escape sequence ambiguity that caused the broken frame-parser bug
- Add 4MB buf overflow cap: if buf exceeds 4MB without a complete JPEG
  frame, the buffer is discarded; this prevents unbounded memory growth
  when a camera sends corrupted or non-JPEG data
- Improve buf handling: bytes before SOI are trimmed immediately instead
  of carrying them through the next read loop iteration
- go2rtc is retained and still runs at startup for stream probing
  (the _probe_ code path), but is no longer required for viewing streams

## 1.4.4
- Critical fix: JPEG frame parser was broken — SOI (0xff 0xd8) and EOI (0xff 0xd9)
  markers were stored in source as double-escaped strings (\xff\xd8) meaning
  Python searched for literal 8-char ASCII sequences instead of 2-byte JPEG
  markers; no frame boundaries were ever found so the stream read loop ran until
  30-second timeout with zero frames delivered to the browser; fixed via binary
  replacement ensuring file has single-escaped ÿØ (4-char escape sequence
  that Python compiles to bytes 0xFF 0xD8 at runtime)
- HEVC cameras now registered with go2rtc using ffmpeg wrapper source:
  go2rtc_source() returns 'ffmpeg:rtsp://...#video=h264&width=640&fps=8' for
  HEVC streams; go2rtc uses its configured ffmpeg binary to transcode HEVC→H.264
  internally; the local RTSP restream (port 8554) then delivers H.264; our
  ffmpeg process reads H.264 from 127.0.0.1:8554 and easily converts to MJPEG
  For 4K HEVC: 480x270 @ 4fps; for 1080p/720p HEVC: 640x360 @ 8fps
- go2rtc_source() used consistently everywhere: handle_stream, go2rtc_register_all,
  ONVIF profile card registration, and credential save all pass camera dict so the
  correct source URL (direct or ffmpeg-wrapped) is used from the start
- Startup log now shows whether each camera is registered as direct or ffmpeg-wrapped
  and what codec is stored; Stream log shows go2rtc mode and source resolution

## 1.4.3
- Critical fix: added 'import aiohttp' to module imports; go2rtc_add/remove
  functions use aiohttp.ClientSession() but the module only had
  'from aiohttp import web' — every go2rtc API call failed with
  'NameError: name aiohttp is not defined', causing all streams to show
  unavailable even after credentials were accepted
- Critical fix: probe_rtsp_socket roundtrip() function was sending malformed
  RTSP requests because the CRLF separator used escaped backslash-r-backslash-n
  literal characters instead of actual carriage-return + line-feed bytes;
  rewrote using chr(13)+chr(10) which is unambiguous regardless of Python
  string escaping; the camera was receiving an invalid request, sending nothing
  back, and probe_rtsp_socket was timing out (taking the full 6 seconds) before
  returning False — causing all credential attempts to fail

## 1.4.2
- probe_stream_details: now queries go2rtc's /api/streams after registering
  the stream, then falls back to ffprobe only if go2rtc reports no track info;
  this avoids ffprobe entirely for cameras already known to go2rtc
- handle_stream_test (Test Stream button): replaced ffprobe with pure Python
  RTSP socket probe for connectivity check; queries go2rtc for codec/resolution
  details if not already stored on the camera; no subprocess needed
- handle_snapshot: replaced ffmpeg one-frame grab with go2rtc's /{id}.jpg
  snapshot endpoint; go2rtc handles the frame extraction natively
- api_delete_camera: now removes camera from go2rtc on deletion
- probe_stream_details: accepts optional cid param so caller-supplied stream
  name is used for go2rtc registration rather than a temp hash name

## 1.4.1
- Architecture change: replaced custom ffmpeg/ffprobe pipeline with go2rtc
  go2rtc is a purpose-built Go binary used by Frigate and HA's camera stack;
  it handles RTSP/ONVIF/HLS/RTMP, all codecs (HEVC/H.264/etc), auth (Basic/
  Digest/ONVIF), and reconnection natively; no more URL encoding fights,
  probesize limits, or hardware decode guesswork
  go2rtc runs as a sidecar on port 1984; streams are registered via its REST
  API and MJPEG output is proxied through the AnyCam server to the browser
  go2rtc binary auto-downloaded for the correct architecture in Dockerfile
  (arm64/armv6/amd64/386) using the BUILD_ARCH build arg
- Architecture change: replaced ffprobe stream verification with a pure Python
  RTSP socket probe (probe_rtsp_socket); sends RTSP OPTIONS + DESCRIBE with
  proper Digest and Basic auth negotiation via raw TCP socket; no external
  processes, no probesize limits, no URL encoding issues, works with any codec
  Credentials are never embedded in a URL string — passed as separate strings
  and encoded only into the RTSP Authorization header where needed
- go2rtc stream lifecycle: cameras are registered with go2rtc on startup,
  when credentials are accepted (both ONVIF profile cards and direct RTSP),
  and removed when cameras are deleted
- run.sh: go2rtc started before Python server with API readiness check

## 1.4.0
- probe_rtsp: removed -analyzeduration 1000000 -probesize 200000 flags that
  were causing RTSP verification to fail for high-res HEVC cameras; a single
  4K HEVC IDR frame can easily exceed the 200KB probesize limit, causing
  ffprobe to exit with error even on a valid stream; removed limits let ffprobe
  use its defaults (5MB / 5s) which are sufficient for any camera
- probe_rtsp: timeout increased from 4s to 8s (subprocess timeout 11s) to
  give HEVC streams time to deliver their first IDR frame
- probe_rtsp: stderr now logged as WARNING with redacted URL when ffprobe
  returns non-zero; exact ffprobe error message now visible in the log so
  future failures will explain themselves rather than just showing False
- probe_rtsp: credentials URL-encoded with the correct safe set (sub-delims
  kept literal, only @ : / ? # encoded) — consistent with build_authenticated_url

## 1.3.9
- Added full diagnostic logging to api_set_credentials — every step now logs
  at INFO/WARNING so credential failures are visible in the log:
  which protocol path is taken, ONVIF media URL, how many profiles returned,
  each profile's stream URL, probe_rtsp result per profile, which RTSP ports
  were tried, final success/failure outcome
- RTSP port 554 fallback for ONVIF cards: ONVIF cards store port 80 (the web
  UI port) but RTSP is always on port 554; if ONVIF SOAP fails and falls back
  to direct RTSP probing, we now always also try port 554 explicitly, not just
  the stored port; this fixes 'Could not connect' for cameras where ONVIF auth
  fails but direct RTSP works fine
- Also tries xaddrs port from ONVIF discovery as a third fallback

## 1.3.8
- URL credential encoding fix: the previous fix encoded ! as %21 but many
  RTSP servers (including Hikvision-based cameras) do not percent-decode
  credentials before authentication — they compare %21 literally against the
  stored password, causing authentication to fail; per RFC 3986, the userinfo
  component allows sub-delimiters (! $ & ' ( ) * + , ; =) unencoded; the
  safe set is now set to these sub-delimiters plus unreserved chars, so only
  truly URL-breaking characters are encoded: @ : / ? # [ ]
  Result: 'fuckyou!' stays as 'fuckyou!' in the URL (correct) while a
  password like 'p@ss:w0rd' becomes 'p%40ss%3Aw0rd' (necessary to avoid
  breaking the user:pass@host URL structure)

## 1.3.7
- Critical URL encoding fix: build_authenticated_url() now percent-encodes
  both username and password using urllib.parse.quote() before inserting
  into the RTSP/HTTP URL; special characters like ! @ : # ? in passwords
  were being passed raw, causing ffmpeg to fail with 'Invalid data found
  when processing input' — e.g. password 'abc!def' is now encoded as
  'abc%21def' in the URL string; this fix affects all stream protocols
- Replaced -pix_fmt yuvj420p flag with format=yuv420p inside the -vf
  filter chain; having -pix_fmt as a separate flag was conflicting with
  how the HEVC software decoder outputs frames and causing swscaler to
  run an extra conversion; moving it into the filter chain as
  'fps=N,scale=W:-2,format=yuv420p' is the correct approach

## 1.3.6
- Added -pix_fmt yuvj420p to ffmpeg output — yuvj420p is the JPEG full-range
  YUV pixel format that MJPEG encoding expects natively; without it, ffmpeg runs
  swscaler to convert pixel formats on every frame which is slower and was
  causing 'deprecated pixel format' warnings; forcing it eliminates the extra
  conversion step and ensures frames flush correctly
- Replaced blocking 4-second early-fail check for hardware decode with a fast
  2-second first-read timeout — the old approach spent 4 seconds doing nothing
  before switching to software, giving the HA ingress proxy time to drop the
  connection before the first MJPEG frame arrived; now if hardware produces no
  data in 2 seconds, we immediately kill it and retry software; if software
  produces no data in 30 seconds, we give up; total wait before first frame
  is now at most 2-3 seconds instead of potentially 10+ seconds

## 1.3.5
- Critical fix: replaced -stimeout with -timeout in ffmpeg command;
  -stimeout was deprecated and removed in newer ffmpeg versions — it caused
  ffmpeg to exit immediately with 'Unrecognized option stimeout / Option not found',
  producing zero frames and instant stream failure; -timeout is the correct
  option for modern ffmpeg RTSP connection timeout
- Critical fix: verification scan codec probe now uses build_authenticated_url()
  to get the credential-embedded URL for ffprobe; previously it used the bare
  saved stream_url which has credentials stripped by save_cameras(), causing
  ffprobe to get a 401 Unauthorized and return an empty dict, leaving
  stream_codec empty and hardware decode never triggered

## 1.3.4
- Critical bug fix: NameError 'hw_flags is not defined' crashed every stream
  request; hw_flags was renamed to hw_flags_inner inside _make_proc_args during
  a refactor but the _launch_ffmpeg(hw_flags) call site was not updated; streams
  always showed 'Stream unavailable' because of this exception
- Verification scan codec probing: fixed indentation error that prevented
  probe_stream_details() from running during the post-upgrade verification scan;
  codec info now correctly stored for all cameras missing stream_codec on startup;
  save_cameras() called after verification so codec data persists across restarts

## 1.3.3
- Critical fix: stream codec (HEVC/H.264) now populated during the post-upgrade
  verification scan for cameras that were saved before 1.3.2 was installed;
  previously 'codec=?' meant hardware decode was never attempted even though
  the camera supports it; now probe_stream_details() is called during
  verification when a camera has a stream_url but no stream_codec stored
- 4K HEVC handling: streams from sources >=3840px wide now use fps=4,scale=480:-2
  output filter (versus fps=8,scale=640:-2 for 1080p HEVC and fps=10,scale=640:-2
  for H.264); reduces the decode + re-encode workload to a level a Pi 4 can sustain
- Hardware decode early-fail detection: for HEVC/H.264 streams where a hardware
  decoder (hevc_v4l2m2m or h264_v4l2m2m) is attempted, a 4-second window checks
  if ffmpeg exits immediately with an error code; if so, stderr is logged and
  ffmpeg is restarted immediately with software decode (no waiting 30s)
- Timeout log message now includes frames_sent count, codec, and source resolution;
  zero frames + HEVC explicitly noted as likely hw decode availability issue
- ffmpeg stderr always drained and logged in finally block (not just on TimeoutError)

## 1.3.2
- HEVC/H.265 streaming support without changing camera settings:
  handle_stream now reads the stored stream_codec and selects the appropriate
  hardware decoder — hevc_v4l2m2m (Raspberry Pi 4 VideoCore VI) for H.265,
  h264_v4l2m2m for H.264; these use the Pi GPU rather than the CPU
  If hardware decode produces no frames, automatically retries with software
  decode (ffmpeg selects the native hevc/h264 software decoder)
  Output FPS reduced from 10 to 8 for HEVC streams to ease Pi CPU load
- Stream details auto-detected after credentials accepted:
  probe_stream_details() runs ffprobe on the confirmed stream URL and stores
  stream_codec, stream_width, stream_height, stream_fps, stream_profile,
  and stream_audio on the camera dict; called from both the RTSP/MJPEG/HLS
  credential path and the ONVIF profile card creation path
- Stream details shown in Identity section:
  Video codec row: e.g. 'HEVC (High)' or 'H264 (High)'
  Resolution row: e.g. '1920x1080  @  30 fps'
  Audio codec row: e.g. 'AAC' (if audio track present)
- _safe_cam API response now includes stream_codec, stream_width, stream_height,
  stream_fps, stream_profile, stream_audio fields

## 1.3.1
- Stream diagnostics: ffmpeg stderr is now ALWAYS captured and logged as WARNING
  when the stream ends (previously only logged on timeout); this makes silent
  failures visible — e.g. 'H.265 decoder not found' or 'RTSP auth failed'
- Stream log: stream URL logged at INFO level (credentials redacted) so you can
  see exactly what ffmpeg is connecting to
- ONVIF protocol now correctly gets -rtsp_transport tcp and -allowed_media_types video
  flags in ffmpeg (was only applied to RTSP and DVR); fixes Hikvision PTZ cameras
  that kept stream_url as ONVIF protocol after credential submission
- ffmpeg -stimeout 8000000 (8s connection timeout) and 30s per-frame read timeout
- New: GET /stream/{id}/test diagnostic endpoint — runs ffprobe on the stream URL
  and returns JSON with codec, resolution, FPS; 'Test Stream' button on each ready
  camera card opens an alert with this info, or the error message if unreachable
  Use this to diagnose 'Stream unavailable': if ffprobe succeeds but live view
  fails, the issue is ffmpeg decoding (usually H.265 on a Pi that only supports
  H.264 in software) — fix by setting the camera to H.264 720p
- Port scan timer: shows 'X:XX elapsed' at start (before nmap gives ETA),
  then 'estimated X:XX remaining' once nmap sends its first timing update,
  then 'Completed in X:XX' when done — now matches main scan timer behavior
- Port scan timer: uses .scan-timer-badge CSS class for consistent blue styling
- ETA unified: _total_estimate variable persists across all stages and only
  ever decreases; per-host updates use the same reference point (scan_start)

## 1.3.0
- ETA fix: eliminated per-stage ETA resets — a single _total_estimate variable
  is maintained from scan start and only ever refined downward; each stage
  updates the estimate based on new information but the countdown never jumps
  back up; eta is always computed as max(0, total_estimate - elapsed_since_start)
  Stage 1: refines using actual S1 time + live host count (10s/host heuristic)
  Stage 2: refines using actual S1+S2 elapsed / 0.55 (stage 2 = ~55% of work),
  floored at responding_hosts * 15s; takes the smaller of old vs new estimate
  Per-host: continuously updated using same total_estimate reference
- ONVIF stream fix: handle_stream now includes 'ONVIF' in the protocol list
  that gets -rtsp_transport tcp; Hikvision and many other cameras require TCP
  transport for RTSP — without it ffmpeg uses UDP which is often dropped
- Added -allowed_media_types video to RTSP/ONVIF/DVR ffmpeg flags to avoid
  stalling on audio-only streams before video frames arrive
- Added -stimeout 8000000 (8 seconds) RTSP connection timeout to ffmpeg so
  unreachable streams fail fast rather than hanging until the 30s read timeout
- ffmpeg stderr now captured and logged as WARNING on stream timeout so future
  stream failures are visible in the log for diagnosis
- Per-frame read timeout increased from 15s to 30s to accommodate cameras that
  take longer to start sending frames after connection is established

## 1.2.9
- Removed duplicate 'Completed in X:XX' from grey status bar message;
  completion time now shown only in the blue timer element to the right

## 1.2.8
- Not a Camera: replaced bare confirm() dialog with a proper modal:
  Shows device name/manufacturer prominently
  7 quick-select device type buttons: Printer, Router/Firewall, NAS/Storage,
  Computer, Smart TV, IoT Device, Not sure — selected button turns red
  Free-text detail field for optional extra info (model name, notes, etc.)
  Share anonymously checkbox — when checked, a device fingerprint is submitted
  to the configured community endpoint (no IP addresses, only OUI, device type,
  open ports, service banners, and page title)
  Keyboard: Escape closes the modal without blacklisting
- Feedback storage: each Not a Camera action now stored in /data/not_camera_feedback.json
  Record includes: cid, reason_type, reason_detail, fingerprint (OUI + vendor +
  port + protocol + service + page_title + manufacturer), share flag, timestamp, version
  Feedback persists across restarts and survives upgrades
- Community sharing architecture: ANYCAM_COMMUNITY_URL env var configures a community
  endpoint; when set and user checks Share, fingerprint is POSTed to /api/v1/report;
  silently ignored if endpoint unavailable; no endpoint configured by default
- build_fingerprint() helper extracts shareable device signatures from camera dicts
- submit_to_community() is a fire-and-forget async task (never blocks the UI)
- Broad sweep subtext: removed duplicate; label now shows Ports 1-10,000 cleanly

## 1.2.7
- Port scanner: ETA countdown shown from the moment scanning starts:
  Uses last_port_scan_duration from runtime.json as the initial estimate;
  updates every ~10 seconds using nmap's own timing output (--stats-every 10s);
  counts down smoothly client-side between polls accounting for poll drift;
  saves actual elapsed time to runtime.json on completion for next scan
- Port scanner: checkbox state now persists across view switches using a
  persistent _selectedIPs Set; selections survive navigating to Cameras and back;
  'Select all' / 'Clear all' also update the persistent set correctly
- Port scanner: live port discovery feed shown during scan:
  nmap -v flag emits 'Discovered open port X/tcp on Y' lines as ports are found;
  these are streamed line-by-line from stdout and added to PSCAN['live_ports'];
  a scrolling box (max 220px, auto-scrolls to bottom) shows ports as found;
  message updates to 'Scanning X... N open port(s) found' in real time;
  only open/active ports are shown, never closed or filtered ones
- Port scanner: live discovery box replaced by full results table when done;
  nmap XML now written to a temp file instead of stdout so we can stream
  the verbose text output for live discovery simultaneously
- Port scanner: ETA and live box both cleared when navigating back to page;
  completed results are restored from PSCAN state when returning to Port Scan
- api_pscan_status now includes elapsed (computed server-side from scan_start)
  so JS can accurately compute time drift between polls for smooth countdown

## 1.2.6
- ETA estimate shown from the very first second of scanning:
  On first-ever scan: initial estimate is 4 minutes (240s) for a typical /24 network
  On subsequent scans: uses actual duration from the most recent completed scan
  (stored as last_scan_duration in /data/runtime.json) as the starting estimate
- ETA progressively refined at each stage:
  After Stage 1 (ARP complete): refined using actual S1 time + host count
  formula (S1_elapsed * 10, floored at S1 + hosts * 8s)
  After Stage 2 (nmap complete): refined using S1+S2 elapsed / 0.55 to project
  100%, also floored at responding_hosts * 15s
  Per-host in Stage 3: updated continuously as each host is probed
- ETA display: 'estimated X:XX remaining' while running, counting down smoothly
  client-side between 1.5s polls; jumps are expected as estimates are refined
- Actual scan duration saved to runtime.json on completion for next scan's ETA
- ETA label changed from '~X:XX remaining' to 'estimated X:XX remaining'

## 1.2.5
- Critical bug fix: NameError crash in is_camera_positive() — 'reason' was
  referenced in the not_camera verdict log line but is not a parameter of that
  function; this caused run_scan() to crash immediately when any device with a
  not_camera verdict was encountered (e.g. a printer), leaving SCAN_STATE
  running=True forever and the UI stuck in an infinite polling loop
- Added safety wrapper around run_scan(): any uncaught exception now sets
  running=False with an error message so the UI never gets permanently stuck
- Stage label removed from status message text — stage is shown only in the
  purple badge; message now shows just what is happening (e.g. 'Probing
  192.168.50.3 (1/4)...') without the 'Stage 3/4 —' prefix duplication
- Scan timer changed from elapsed to ETA countdown:
  Early stages show 'X:XX elapsed'; once Stage 3 begins, an ETA is calculated
  by extrapolating from Stage 1+2 time (25% of work) to 100% and showing
  '~X:XX remaining' that counts down; completion shows 'Completed in X:XX'
- Docker IP access log filtering: a logging.Filter on aiohttp.access suppresses
  all log entries from 172.x.x.x addresses (HA Supervisor Docker bridge proxy);
  these requests are still served normally, just not logged
- Broad sweep checkbox now shows 'Ports 1–10,000' as small subtext below label
- Lorex probe also runs in ONVIF-only else branch (previous fix) — timeouts
  per-port are now explicit with asyncio.wait_for

## 1.2.4
- Lorex NVR identification improvements:
  Added 'flirlorex' and 'flir lorex' to all Lorex DB detection patterns;
  ONVIF-only device HTTP probe now tries ports 80, 443, 8080, 8888, 8090, 34567
  (NVRs often use non-standard web ports); added asyncio timeout and per-port
  logging so failures are visible in the log
- Docker/internal IP exclusion: hosts with 172.x.x.x or 169.254.x.x addresses
  are now excluded from the live host list; these are HA Supervisor Docker bridge
  IPs, not real LAN devices — this fixes the 'homeassistant' card from the
  Docker gateway appearing in scan results
- ACTi false positive fix (HP printer misidentified):
  identify_manufacturer() now uses word-boundary matching for keywords shorter
  than 6 characters; short strings like 'acti' no longer match as substrings
  inside words like 'interactive' or 'active' on unrelated device pages
- ACTi false positive fix (part 2): is_camera_positive() now respects the
  nmap not_camera verdict (printer, router, NAS, etc.) as an early rejection,
  skipping all protocol probing for devices nmap identified as non-cameras;
  OUI camera confirmation still overrides this if MAC OUI is a known camera maker
- Scan timing: SCAN_STATE now tracks started_at timestamp and elapsed seconds
- Elapsed timer shown in status bar to the right of the progress bar:
  'X:XX elapsed' while running, 'Completed in X:XX' when done
- Completion log message now includes elapsed time:
  'Scan complete — N device(s), N streaming. Completed in X:XX.'

Broad sweep reminder: enables Stage 4 — after the focused top-1000-port scan,
any live hosts that did not respond get scanned on ports 0-10,000 to catch
cameras on very non-standard ports. Adds 5-20 minutes depending on silent hosts.

## 1.2.3
- Deduplication fix: confirmed_ips now includes any card where manufacturer
  is identified OR protocol is a camera type OR verdict_reason starts with
  ONVIF/SSDP/mDNS — not just status=ready with non-HTTP protocol; this
  correctly suppresses the 192.168.50.210:80 and :443 noise cards when the
  iENSO was identified on port 8888 but still needs credentials
- AnyCam self-exclusion: nmap results for the local Pi IP have our own
  ingress port (8099) removed before probing; prevents AnyCam's own UI
  from appearing as a camera card (our page contains 'camera' everywhere)
- Lorex/ONVIF identity fix: when ONVIF merges into an existing nmap-found
  entry (e.g. device found on port 554 but ONVIF confirmed), HTTP identity
  probing now runs on ports 80/443/8080 if manufacturer is still empty;
  this allows Lorex NVRs found on RTSP to get HTTP-probed and identified
- Port Scanner: navigating away no longer cancels the scan; returning to
  Port Scan view resumes status polling automatically if scan is running
- Port Scanner: results split into two sections:
  'Likely camera-related' (camera ports + camera service keywords) shown
  fully expanded; 'Other ports' collapsed with click-to-expand toggle
- Port Scanner: 'Back to Cameras' button moved to upper left of the view
- Port Scanner: hint text added noting that navigating away won't cancel

## 1.2.2
- MAC address OUI lookup added to device identification pipeline:
  nmap reports MAC addresses and vendor names from its built-in OUI DB
  during ARP-based LAN scans; these are now extracted from nmap XML and
  stored on every camera card
- Full IEEE OUI database (~37,000 entries) downloaded from
  standards-oui.ieee.org on first startup and cached to /data/oui_cache.json;
  refreshed automatically when cache is older than 30 days; download runs
  as a background async task and never blocks the scan
- lookup_oui(mac): returns vendor name from full IEEE DB, or embedded fallback
- oui_is_camera(mac): returns True/False/None based on OUI vendor matching
  against CAMERA_DB aliases (camera) or NON_CAMERA_KEYWORDS (non-camera)
- is_camera_positive() now checks OUI immediately after multicast signals;
  a camera-manufacturer OUI is a definitive positive; a non-camera OUI
  (Cisco, Apple, HP, Ubiquiti, Synology, etc.) causes early rejection
  before any slower probes are attempted
- _parse_nmap_xml() extracts address[@addrtype=mac] addr and vendor attributes;
  supplements nmap vendor with full IEEE DB when nmap does not identify it
- _probe_host_port() and base() carry mac_addr and mac_vendor through to the
  camera dict; OUI vendor used to fill manufacturer field if HTTP probe
  did not identify one
- Identity card section now shows MAC / OUI row: address + vendor name in
  parentheses (e.g. "1C:C3:16:xx:xx:xx  (Hangzhou Hikvision Digital...")
- Embedded curated OUI sets: ~30 known camera-manufacturer OUI prefixes
  and ~60 known non-camera OUI prefixes (Cisco, Juniper, MikroTik, Ubiquiti,
  HP, Dell, Apple, Netgear, ASUS, Brother, Epson, Synology, QNAP)

## 1.2.1
- Lorex NVR detection fix: probe_http_identity now extracts script/link src
  attribute values from HTML (not just body text) — catches SPAs like Lorex
  where the manufacturer name appears only in JavaScript asset paths
  (e.g. src="/flirLorex/js/desktop/...") not as visible page text
- probe_http_identity now ignores SSL certificate errors (cameras and NVRs
  almost always use self-signed certs); previously a certificate error
  silently returned empty identity with no manufacturer detected
- probe_http_identity now tries multiple paths per host: /, /index.html,
  /login.htm, /login.html, /web/, /web/index.html, /cgi-bin/main-cgi,
  /view/index.shtml, /live, /admin/ — stops at first manufacturer match
- probe_http_identity now tries both http:// and https:// on every port
- Body read increased from 8KB to 16KB for better SPA coverage
- Focused nmap scan changed from fixed 15-port camera list to
  --top-ports 1000 (nmap's curated most-common-1000 ports list);
  covers all camera protocols plus thousands of other ports, catching
  cameras on non-standard ports identified by manufacturer name in banner
- IP-level deduplication: after all probing, if an IP already has a
  confirmed streaming camera card (status=ready, non-HTTP protocol),
  any sibling cards on that same IP that are HTTP-only/needs_credentials
  are automatically suppressed — prevents noise cards like serial-number
  hostnames appearing alongside confirmed camera cards
- Port Scanner: ARP-discovered hosts now listed above the IP input with
  checkboxes — IP and hostname shown; Select all / Clear buttons available
- Port Scanner: batch mode — check any number of hosts and click
  Scan All Ports to scan them sequentially; results table shows a Host
  column when more than one IP was scanned; results accumulate across all
  hosts in the batch
- ARP_HOSTS global stores last scan results; /api/arp_hosts endpoint
  returns them; Port Scanner auto-refreshes the list when opened

## 1.2.0
- Expanded CAMERA_DB from 33 to 53 entries (54 total minus 1 duplicate):
  Added: Tiandy, IndigoVision, Q-See, LaView, Zosi, Sricam/Srihome, Vstarcam,
  Wansview, Tenvis, Instar, Luma Surveillance (SnapAV), Speco Technologies, Oncam,
  Illustra (Johnson Controls), Milesight, Sunell, TVT Digital, Kedacom,
  VideoIQ (Avigilon), Samsung standalone (pre-Hanwha)
- Removed duplicate Reolink entry

## 1.1.9
- Camera manufacturer/model database (CAMERA_DB): 33 entries covering all major
  manufacturers — Hikvision, Dahua, Lorex, Reolink, Axis, Hanwha/Samsung, Amcrest,
  Uniview, Vivotek, Bosch, Pelco, Sony, Panasonic/i-PRO, Avigilon, FLIR, Mobotix,
  ACTi, GeoVision, Foscam, Annke, Swann, TP-Link Tapo, Night Owl, iENSO, Digital
  Watchdog, March Networks, Nest/Google, Ring, Wyze, Eufy/Anker, Arlo, Verkada, Luxonis
- Each DB entry contains signature patterns for: HTTP page titles, page body text,
  HTTP response headers, nmap service/product fields, and ONVIF WS-Discovery scope strings
- identify_manufacturer(): scores all DB entries against a text blob and returns the
  best-matching entry; used during probing and nmap banner analysis
- probe_http_identity(): replaces probe_http_for_camera() — fetches the HTTP root
  page (up to 8KB), extracts page title, Server header, and runs DB matching; returns
  structured dict with manufacturer, notes, title, server, is_camera, raw_snippet
- probe_http_for_camera() now a thin wrapper around probe_http_identity()
- is_camera_positive() now checks nmap banners against CAMERA_DB (not just keyword list)
  and also checks device hostnames against DB alias strings
- _probe_host_port() now calls probe_http_identity() on HTTP ports and attaches identity
  fields (manufacturer, device_notes, page_title, server_header) to every camera dict
- Camera display name auto-upgraded: if manufacturer is identified and the name is still
  the IP-derived default, the name is set to "Manufacturer (ip)" (e.g. "Lorex (192.168.50.3)")
- ONVIF-only devices (detected by multicast but not port scan) now also get HTTP identity
  probing on ports 80, 8080, 443
- Identity section on each camera card: collapsible "🔍 Identity" section showing
  Manufacturer, Page title, Server header, Hostname, and Notes rows
- Post-upgrade scan clarification: status message now explicitly says "scanning subnet
  for newly discoverable cameras" when the full scan runs after verification

## 1.1.8
- Version-aware startup logic — three distinct startup modes:
  new_install: no saved cameras and no prior version recorded → auto-scan as before
  routine: same version as last run (HAOS reboot, addon restart) → load cameras silently, no scan
  post_upgrade: version differs from last run → load cameras, then run verification scan + fresh full scan
- Post-upgrade verification scan:
  Each saved camera is probed individually using its own protocol (RTSP probe, MJPEG quick-check, etc.)
  Cameras still responding → kept as-is, upgrade_missing flag cleared
  Cameras not responding → marked upgrade_missing=True (kept in list, not deleted automatically)
  After verifying saved cameras, a full fresh scan runs to discover new cameras the upgraded
  detection code may now find
- Cameras marked upgrade_missing show:
  Pulsing orange status dot (distinct from yellow needs_credentials and red error)
  Orange badge: ⚠ Not found after upgrade
  Feed area shows explanatory overlay: was present before upgrade but did not respond;
  may be offline, removed, or a false positive from the previous version
  Two decision buttons: Keep (may be offline) clears the flag and restores normal card behaviour;
  Remove deletes permanently
- New /api/cameras/{id}/confirm endpoint: clears upgrade_missing flag
- Version persisted in /data/runtime.json on every startup so next run can compare
- Pre-1.1.8 upgrades handled: if runtime.json does not exist but cameras.json does,
  startup mode is treated as post_upgrade (covers users upgrading from any previous version)
- 172.30.32.x log entries clarification: these are the HA Supervisor Docker bridge IPs,
  not external clients — all browser requests are proxied through the Supervisor

## 1.1.7
- Extended is_camera_positive gate to cover all 7 supported protocols:
  RTMP: wired existing probe_rtmp() C0 handshake byte check into the gate
    (ports 1935/1936, ~50ms, definitively identifies RTMP server)
  MJPEG: new probe_mjpeg_quick() checks 6 common paths for
    Content-Type: multipart/x-mixed-replace or image/jpeg (~200ms)
  HLS: new probe_hls_quick() checks 5 common paths for #EXTM3U body
    or mpegurl Content-Type (~200ms)
  WebRTC: wired existing probe_webrtc() WHEP POST + heuristic GET
    into the gate for HTTP/HTTPS ports (~300ms)
  WS-RTSP: wired existing probe_ws_rtsp() WebSocket upgrade handshake
    (Sec-WebSocket-Protocol: rtsp) into the gate for any port (~200ms)
  RTSP/HTTP probes unchanged from 1.1.6
  ONVIF/SSDP/mDNS still confirmed instantly via Stage 1 multicast
- Probe order optimised: fastest and most definitive probes run first
  (RTMP handshake 50ms, RTSP OPTIONS 100ms) before slower HTTP probes
- WS-RTSP probe runs last on any port not covered by earlier checks
  (covers go2rtc on 8554, mediamtx on custom ports, etc.)

## 1.1.6
- Replaced passive keyword-matching filter with active camera-positive probing:
  Each device now only gets a card if at least one active test confirms it speaks
  camera protocols. Tests run in priority order (fastest first):
  1. Multicast-confirmed (ONVIF/SSDP/mDNS) — already done in Stage 1, instant
  2. RTSP OPTIONS handshake: sends a raw OPTIONS request to RTSP ports; any
     RTSP/1.0 response (including 401 Unauthorized) is camera-positive. Routers
     and printers do not speak RTSP and close the connection or return garbage.
  3. HTTP content probe: fetches the root page and scans the first 4KB of body
     and response headers for camera-specific strings (camera, onvif, rtsp,
     hikvision, dahua, stream, nvr, dvr, etc.). Much more reliable than nmap
     banner matching because it reads actual page content.
  4. DVR port assumption: ports 37777 (Dahua) and 34567 are camera-positive by
     definition.
  5. nmap keyword fallback: only if all above probes fail.
- User-saved cameras always appear regardless of probe result (they were already
  manually confirmed by the user).
- Result: only devices that actually speak camera protocols show as cards;
  routers, printers, NAS, and other non-camera devices are silently skipped.

## 1.1.5
- Fixed JavaScript not executing at all (buttons dead, cameras not showing despite being loaded):
  Root cause was Python f-string multi-level escaping — Python processes ''' → ''' in the f-string
  before JavaScript ever sees it, causing a JS SyntaxError that silently killed the entire script block.
  Fix: moved ALL JavaScript to a module-level raw string (_JS = r"""...""") so braces, backslashes,
  and quotes are all literal. Only BASE is injected via str.replace(). Verified clean with Node.js --check.
- Fixed onerror quote conflict: replaced inline onerror attribute string with imgError(this) helper function
  that lives in the main JS scope, eliminating nested quote problems entirely.
- ARP scan fallback: if ARP returns fewer than 3 hosts (can happen without raw socket privileges in Docker),
  automatically supplements with ICMP ping scan before proceeding to port scan.
- Local machine IP always added to live host list so OAK Camera addon RTSP on port 8765 is always scanned.
- Removed not_camera auto-suppression: all live devices with open ports now get a card regardless of
  classify_device() verdict. Only explicit user action (Not a Camera button) blacklists a device.
  This prevents real cameras with generic HTTP banners from being silently hidden.
- Better Stage 1 completion message: shows per-method discovery counts
  (e.g. "23 via ARP, 1 via ONVIF, 2 via SSDP").

## 1.1.4
- Replaced single-pass nmap with a 4-stage intelligent scan pipeline:
  Stage 1: ARP ping scan (nmap -sn -PR) finds live hosts in 3-8 seconds, eliminating timeout waste on dead IPs; ONVIF WS-Discovery, SSDP/UPnP, and mDNS/Bonjour all run in parallel
  Stage 2: Focused camera port scan runs only on confirmed-live hosts (not the full /24), cutting scan time dramatically
  Stage 3: Stream probing unchanged — RTSP path probe, MJPEG, HLS, RTMP, WebRTC, WS-RTSP
  Stage 4 (optional): Broad sweep of ports 0-10000 on live hosts that did not respond to camera ports — enabled via Broad sweep checkbox in the header
- Added SSDP/UPnP discovery (M-SEARCH on 239.255.255.250:1900) — detects cameras that announce via UPnP
- Added mDNS/Bonjour discovery (224.0.0.251:5353) — queries _rtsp._tcp, _onvif._tcp, _camera._tcp, _nvr._tcp; gracefully handles port-in-use (avahi) by falling back to send-only mode
- Stage indicator badge shown in status bar during scans (Stage 1/4, Stage 2/4, etc.)
- Broad sweep toggle checkbox added to header (passes broad_sweep flag in scan POST body)
- Scan start now accepts JSON body with broad_sweep option; no separate options endpoint needed
- HTML caching: build_html() called once and cached to avoid regenerating on every page load

## 1.1.3
- Added port 8765 to scan list — this is where the OAK Camera addon serves RTSP via mediamtx, so AnyCam can now find the Luxonis camera
- False-positive filtering: default gateway IP is now automatically skipped (routers are never cameras)
- False-positive filtering: nmap product/service banners checked against known non-camera keywords (router, printer, NAS, etc.); matched devices shown with orange ⚠ Unverified badge
- False-positive filtering: "Not a Camera" button on each card permanently blacklists the device by IP (stored in /data/blacklist.json, survives restarts and rescans)
- ONVIF multi-stream (NVR) support: when credentials are submitted for an ONVIF device, GetProfiles + GetStreamUri called via SOAP/WS-Security to enumerate all camera channels; each channel gets its own card
- Port Scanner: new "Port Scan" button opens a full-range scan (all 65535 ports) of any IP with -sV -sC -A flags for maximum detail; results shown in a table with port, service, version, and script output
- Port scanner supports Pause (SIGSTOP) and Cancel (SIGTERM) controls
- "Connect Known Camera" button opens a manual add form with fields for IP, port, protocol (all 7 supported), optional stream path, and credentials; validates by probing before saving
- UI now has 3 views switchable via header buttons: Camera Grid, Port Scan, Connect Known Camera
- Uncertain devices shown with orange dot and ⚠ Unverified badge; still visible so user can verify or dismiss
- "Not a Camera" button shown on uncertain and credential-required cards

## 1.1.2
- Fixed 404: HA ingress proxy strips the /api/hassio_ingress/TOKEN prefix before
  forwarding to the addon, so routes must be registered at bare paths (/, /api/cameras,
  etc.). INGRESS_PATH is now only used as the JavaScript BASE for browser fetch() calls.

## 1.1.1
- Fixed 404 on sidebar click: register index route for both /path and /path/ variants
  (HA ingress arrives without trailing slash; aiohttp treats slash vs no-slash as distinct routes)

## 1.1.0
- Added HTTP MJPEG detection: probes 17 common paths, checks Content-Type for multipart/x-mixed-replace or image/jpeg; streams via ffmpeg proxy
- Added HLS detection: probes 11 common .m3u8 paths, verifies #EXTM3U body or mpegurl Content-Type; plays via hls.js in browser (native Safari fallback)
- Added RTMP detection: TCP connect to ports 1935/1936, verifies handshake byte (0x03/0x06 S0); streams via ffmpeg proxy
- Added WebRTC detection (preliminary): probes WHEP endpoints via SDP POST and heuristic GET; shows info card with signaling URL — full in-browser negotiation not yet implemented
- Added RTSP-over-WebSocket detection (preliminary): raw WebSocket upgrade probe with Sec-WebSocket-Protocol: rtsp; shows info card with ws:// URL — full WS-RTSP playback planned for a future release
- Extended nmap port list to include 1935, 1936 (RTMP) and 8888 (common MJPEG/HLS)
- Protocol-aware ffmpeg input flags: RTSP uses -rtsp_transport tcp, HLS uses -re (native rate)
- Colour-coded protocol badges in UI: each protocol has its own distinct colour
- Protocol icons added to badges: 📹 RTSP, 🔭 ONVIF, 🖼️ MJPEG, 📡 HLS, 📺 RTMP, 🔗 WebRTC, 🔌 WS-RTSP
- WebRTC and WS-RTSP cards show blue status dot (info) rather than yellow (needs_credentials)
- Info cards for WebRTC/WS-RTSP include explanatory text and direct link/URL to the detected endpoint
- Credential verification now protocol-aware: MJPEG uses HTTP Basic auth probe, HLS uses M3U8 path probe, RTSP/ONVIF use ffprobe

## 1.0.0
- Initial release
- nmap scan on all 10 common camera ports (554, 8554, 80, 8080, 443, 8443, 2020, 37777, 34567, 10554)
- ONVIF WS-Discovery multicast probe (UDP 239.255.255.250:3702) runs in parallel with nmap
- Automatic RTSP path probing across 25 common paths per discovered device
- Fernet-encrypted credential storage in /data/cameras.json (key in /data/secret.key)
- Credentials verified against live stream before being saved
- Card grid UI with live MJPEG feeds via per-camera ffmpeg processes
- Credential prompt with password masking for cameras requiring authentication
- Auto-connect on startup using saved credentials from previous sessions
- Camera renaming (click camera name)
- Remove camera from list
- Clear stored credentials per camera
- ONVIF badge shown for ONVIF-confirmed devices
- Progress bar with live status messages during scan
- Auto-scan triggered on first launch if no saved cameras exist
- Appears in HA left sidebar as "AnyCam"
