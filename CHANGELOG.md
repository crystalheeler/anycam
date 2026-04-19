# AnyCam — Changelog

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
