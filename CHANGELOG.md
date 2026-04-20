# AnyCam — Changelog

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
