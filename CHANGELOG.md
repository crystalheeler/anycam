# AnyCam — Changelog

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
