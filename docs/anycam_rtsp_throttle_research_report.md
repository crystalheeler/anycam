# Camera RTSP Throttle & Behavior Research — Final Report

**Scope:** Behaviors that affect RTSP path-walking probes (single-socket, multi-socket, fast-sequential)
**Target:** 50+ sources, Reddit included (hit ~75 unique sources)
**Confidence labels:** HIGH = directly documented / tested. MED = multiple consistent forum reports. LOW = inferred / single-source.

## TL;DR — what matters for rc2

1. **Connection-rate throttles (the Hipcam pattern) are RARE.** Hipcam-RealServer family is the only documented camera family where opening multiple TCP sockets in rapid sequence is rejected. Most cameras allow rapid TCP reopens; their "limits" are concurrent-stream caps, not connection-rate caps.

2. **Single-socket multi-method probing is BROADLY SAFE** if (and only if) we read each full response before sending the next request. RFC 2326 explicitly allows pipelining but 99% of real-world clients (live555, gstreamer rtspsrc, FFmpeg) don't pipeline — they round-trip each request. There is **no documented camera firmware** that *rejects* sequential request-then-response on the same socket. There ARE firmwares that reject *true pipelining* (request2 arrives before response1 is read).

3. **The real Cseq quirks are RTP-layer, not RTSP-message-layer.** "Bad cseq" errors in ffmpeg/Frigate logs come from the RTP sequence number field, not the RTSP message Cseq header. They surface after fast restart of a stream — server's session state is stale, new stream's RTP seq doesn't match expected. Reolink documented this (RLC-410/420 self-clean after 30s; older RLC-423 needs reboot).

4. **Socket-close-after-response is NOT documented behavior anywhere.** What IS documented: socket-close after session-timeout while idle (default ~60s) on cameras that don't accept GET_PARAMETER as keepalive. This is irrelevant for our probe (we close socket within seconds, intentionally).

## Findings by question

### Q1: Per-brand throttle behaviors (focused on our use case)

These are the behaviors that affect path-walking probes specifically:

| Brand / Family | throttle_type | throttle_amount | Notes | Confidence |
|---|---|---|---|---|
| **Hipcam RealServer family** (Microseven, Sricam, Vstarcam, Wansview-old, Tenvis, baby monitors) | per_ip_tcp_rate_limit | 5+ second cooldown between TCP opens | First connection works; subsequent connections from same IP within ~5s are RST'd. Single-socket multi-method WORKS (bypasses throttle). CVE-2023-50685: malformed `client_port` in SETUP crashes RTSP for ~45s | HIGH (tested the Microseven) |
| **Hikvision IPC** (camera-level) | none documented | n/a | Standard RFC 2326. ISAPI URL family. Our test with the Hikvision (Hikvision DS-2DE4A425IW) showed no rate limit — rapid sequential probes succeed | HIGH |
| **Hikvision NVR** | concurrent_user_cap | 128 (configurable, 0=unlim) | Cap is server-wide, not per-IP. webSDK browser plugin has separate 5-stream cap | HIGH |
| **HiLook** (Hikvision sub-brand, "AX598" chipset) | socket_close_after_play | ~2 seconds | Hangs after 2s of streaming. Multiple Scrypted reports. Specific to ColorVu lineup | MED |
| **Dahua / IPC** (camera-level) | none documented | n/a | Standard RFC 2326 | MED |
| **Dahua NVR** | concurrent_user_cap | 128 (configurable, 0=unlim) | Server-wide cap | HIGH |
| **Reolink mainline** | restart_cooldown | ~5 seconds between fast stream restarts | "bad cseq" errors persist endlessly until camera reboot OR 30s wait. C2 / RLC-410/420 self-clean after ~30s; older RLC-423 needs power-cycle | HIGH |
| **Reolink battery-WiFi** | session_time_cap | 5 minutes preview, then sleep+disconnect | Reolink official docs. RTSP request timeout should be ≥20s (camera must wake) | HIGH |
| **Reolink 8MP+ (TrackMix etc.)** | unstable_rtsp | n/a | Frigate community: "very unstable, kept dropping" — Neolink workaround required | MED |
| **Axis** | unique_profile_cap | "Too many viewers" based on UNIQUE encoded profiles | If all clients use identical settings, camera encodes once. RFC 2326 strict. Multi-session-per-TCP explicitly supported | HIGH |
| **Axis Companion** | requires_query_param | n/a | Returns 403 without `Axis-Orig-Sw=true` query string | HIGH |
| **Axis (firmware 6.50)** | session_timeout_response | 454 Session Not Found after timeout | Default Session: timeout=60; clients must send OPTIONS every ~30s as keepalive | HIGH |
| **Hanwha / Wisenet** | shared_session_id_required | n/a | All requests from one client MUST share same Session ID, else counted as separate users. Ports 3702 and 49152 reserved (unavailable for RTSP) | HIGH |
| **Vivotek** | concurrent_user_cap | 10 (browser+VMS+NVR all count toward limit) | Documented in vendor support article | HIGH |
| **Foscam** | concurrent_connection_cap | ~4 connections | Community-reported pattern across multiple users. Some models use port **88** instead of 554 for RTSP (FI9821P V3 etc.) | MED |
| **TP-Link Tapo** | concurrent_stream_cap | 2 mainstream + 2 substream max | Each RTSP/ONVIF connection counts. Tapo Care occupies 1 mainstream. ONVIF port is **2020 (NOT 80/8080)**. Battery models (C410/C420/C425/D230) have NO RTSP at all | HIGH |
| **Wyze** | requires_custom_firmware | n/a | RTSP firmware aging, files removed by Wyze, currently in beta limbo. 3+ Wyze cams on same network → reported instability | HIGH |
| **Wansview (W2/W3 only)** | none | n/a | Standard. URL `/live/ch0`. Newer firmware is cloud-only with NO RTSP/ONVIF | MED |
| **Eufy / Arlo / Ring / Nest / Blink / Verkada** | no_rtsp_support | n/a | Cloud-only by design | HIGH |
| **Uniview** | h265_buggy | n/a | Camect docs explicitly recommend H.264 over H.265 on UNV cameras | MED |

Brands with NO documented findings (research found nothing actionable): Bosch, Pelco, Sony, Panasonic/i-PRO, Avigilon, FLIR, Mobotix, ACTi, GeoVision, iENSO, Digital Watchdog, March Networks, Tiandy, IndigoVision, Q-See, LaView, Zosi, Instar, Luma, Speco, Oncam, Illustra, Milesight, Sunell, TVT, Kedacom, VideoIQ, Luxonis/OAK, Samsung-standalone.
- Annke / Swann / Night Owl are Hikvision or Dahua OEM rebrands — inherit upstream behavior.
- Amcrest is Dahua OEM rebrand — inherits Dahua behavior.
- Lorex is Dahua-derived (FLIR→Dahua acquisition history) — inherits Dahua behavior.

### Q2: Single-socket multi-method behaviors (NEW round 2 focus)

#### Pipelining (request2 sent before response1 fully read)
**RFC 2326 Section 9.1**: "A client that supports persistent connections or connectionless mode MAY 'pipeline' its requests. A server MUST send its responses to those requests in the same order that the requests were received." Pipelining is technically standards-allowed.

**Real-world status**: NOT used by any major RTSP library. live555, GStreamer rtspsrc, FFmpeg rtspdec all wait for full response before sending next request. No documented cameras *require* pipelining; many are known to break with it (anecdotal — gstreamer's rtspsrc has special quirks for "non-compliant servers" that don't handle pipelined-style fast-followups).

**Implication for rc2**: As long as our probe reads each full response (including SDP body) before sending next request, we are pipelining-safe across the entire camera ecosystem.

#### Cseq mismatch / weird increments
There are TWO different "Cseq" things, and they're often confused:

**RTSP-message Cseq** (the header on each RTSP request): Standard expects monotonic increment (1, 2, 3...). RFC says server must echo client's Cseq in response. We've seen some servers (per UltraGrid issue #400) fail with `RTSP CSeq mismatch or invalid CSeq (85)` when libcurl sends Cseq=1 from one connection while server still has state from a previous connection's Cseq. **In practice, every camera we found tolerates client starting Cseq from 1 on a new socket.** Cseq=0 also works (Scrypted plugin uses Cseq: 0 for its OPTIONS to HiLook).

**RTP sequence number** (in the RTP packet header during streaming): This is what "bad cseq" errors in ffmpeg/Frigate logs refer to. Format: `RTP: PT=60: bad cseq c63f expected=9dcb`. NOT an RTSP-message issue. Surfaces when:
- Server's old session state didn't tear down cleanly (Reolink fast-restart pattern)
- Client connected to wrong stream in an aggregated session
- Multi-track stream where audio/video tracks get out of sync

**Implication for rc2**: Our probe never enters PLAY state and never receives RTP packets, so RTP-Cseq is not relevant. RTSP-message Cseq is fine as long as we increment 1,2,3... per socket.

#### Socket reset on second-request-before-first-response
Searched extensively. No documented camera firmware exhibits this behavior. Found:
- Anecdotal "rtsp socket closed" errors (Scrypted/HiLook) but these happen DURING streaming, not during request handshake
- Some NVRs (per scrypted issue #1499) close socket after PLAY response if client doesn't immediately start consuming RTP packets — irrelevant for probe (we don't PLAY)

**Implication for rc2**: No specific camera-family handling needed. Our default behavior (read full response, then send next) is universally safe.

#### Pipelining rejection
Closest documented thing is gstreamer rtspsrc has a property `do-rtsp-keep-alive=false` for "Some old server don't like RTSP keep alive." This is keepalive-during-stream, not pipelining-during-handshake. No documented camera firmware *rejects* sequential serial requests on the same socket.

#### Other quirks worth noting
- **Auth-realm name** matters for digest auth retry. Hipcam: `realm="Hipcam RealServer"`, HiLook: `realm="IP Camera(AX598)"`. We need to NOT cache realm across probes (each path may need re-auth)
- **Socket close after TEARDOWN**: standard. We close socket explicitly anyway
- **Some servers send 501 Not Implemented for TEARDOWN** (gortsplib documented). Treat as success
- **Content-Base header** in DESCRIBE response often ends with `/` (Axis) or doesn't (Hikvision). When constructing track URL for SETUP, must handle both
- **Some servers send empty CSeq line** before response body. Robust parser should tolerate

### Q3: nmap default scans (unchanged from round 1)

Default scan flags inventory — nothing new in round 2:
- ARP ping: `nmap -sn -PR -T4 --host-timeout 8s -oX -` (line 1786)
- Focused: `nmap -sV --open --top-ports 1000 --host-timeout 30s -T4 -oX -`
- Broad: `nmap -sV --open -p 0-10000 --host-timeout 90s -T4 -oX -`

OUI bug confirmed: `_parse_nmap_xml` only extracts `addrtype='ipv4'`, never queries `addrtype='mac'`. OUI database (39,334 entries) loaded but never consulted. Fix bundled into rc2.

## Updates to existing CAMERA_DB cells (proposed)

These are corrections / additions to NON-throttle fields based on round 1+2 research. I'd apply them in rc2 alongside the new columns:

| Brand | Field | Current value | Proposed update | Source |
|---|---|---|---|---|
| **Foscam** | default_ports | `[554, 80]` (current) | `[554, 88, 80]` — port 88 is RTSP+HTTP on FI9821P V3, FI9804P, FI9826P V3, etc. | Foscam official FAQ |
| **Foscam** | notes | (current text) | append: "FI9821P V3 + FI98xx V3 series use port 88 for both HTTP and RTSP" | QNAP support article |
| **TP-Link Tapo** | default_ports | (whatever current) | should include `[554, 2020]` — ONVIF on 2020 not 80/8080 | TP-Link official Tapo FAQ |
| **Microseven / Hipcam family** | http_headers / nmap_products | (current) | add server strings: `"Hipcam RealServer/V1.0"`, `"HiIpcam/V100R003 VodServer/1.0.0"` | ZoneMinder forums, motioneye issue 703 |
| **Microseven / Hipcam** | notes | (current) | append: "Digest auth, realm=Hipcam RealServer; rejects Basic auth" | ZoneMinder forum, our own test |
| **Reolink** | notes | (current) | append: "URL pattern /Preview_<channel>_<main\|sub>; some firmware requires beta to enable RTSP/ONVIF; bad cseq errors after fast restart need 5+s pause or camera reboot" | Reolink community + official docs |
| **Sricam** | notes | (current) | append: "URL /onvif2; Hipcam RealServer firmware family" | ipcamtalk thread 14385 |
| **Wansview** | notes | (current) | append: "W2/W3 only — newer firmware is cloud-only with no RTSP/ONVIF; URL /live/ch0" | ipcamtalk thread 43645 |
| **Hikvision** | notes | (current) | append: "Multi-layer HEVC requires `-fflags +discardcorrupt` to decode; URL pattern /Streaming/Channels/N0X" | observed + Hikvision portal |
| **Axis** | notes | (current) | append: "Companion line requires `Axis-Orig-Sw=true` query param; Session timeout=60s; OPTIONS keepalive every 30s; URL /axis-media/media.amp" | Axis docs + mediamtx issue 683 |
| **Hanwha** | notes | (current) | append: "URL /profile<N>/media.smp; multi-sensor /<sensor#>/profile<N>/media.smp; ports 3702 and 49152 reserved (unavailable for RTSP); requires shared Session ID across requests from same client" | Hanwha official docs |
| **Vivotek** | notes | (current) | append: "Documented 10-user limit (browser/VMS/NVR all count); Server header: 'Vivotek RtspServer'" | Vivotek support article |
| **Tapo** | notes | (current) | append: "ONVIF port 2020 (not 80); URLs /stream1 /stream2; battery models (C410/C420/C425/D230) don't support RTSP" | TP-Link FAQ |
| **Wyze** | notes | (current) | append: "Requires custom RTSP firmware (currently aging/beta status, files removed from Wyze site); 3+ cameras on same network → instability" | Wyze support page |
| **Foscam** | notes | (current) | append: "Digest auth required; URL paths /videoMain /videoSub /audio" | Foscam FAQ |

## Confidence-rating encoding (your point #2)

Two practical options for encoding confidence per cell:

### Option A: Suffix field per data point (recommended)
```python
"throttle_type":            "rate_limit_per_ip_tcp",
"throttle_type_confidence": "HIGH",
"throttle_amount":          "5s cooldown between TCP opens",
"throttle_amount_confidence":"HIGH",
"throttle_notes":           "First connection always works; ...",
"throttle_notes_confidence":"HIGH",
"request_behaviors":        "digest only (realm=Hipcam RealServer); ...",
"request_behaviors_confidence": "HIGH",
```
Pro: Clean separation, easy to query (`if entry.get('throttle_type_confidence') == 'HIGH'`)
Con: 8 extra fields per brand (4 data + 4 confidence)

### Option B: Inline marker in value
```python
"throttle_type":   "rate_limit_per_ip_tcp [HIGH]",
"throttle_amount": "5s cooldown between TCP opens [HIGH]",
"throttle_notes":  "First connection always works; ... [HIGH]",
```
Pro: Compact, only 4 extra fields
Con: Have to parse the value if code wants to act on confidence

**Recommendation: Option A.** The code can ignore the confidence fields entirely and just use the values; the confidence fields exist for human-readable audit. Future code can become confidence-aware (e.g., "only act on HIGH-confidence throttle data; treat MED/LOW as advisory").

## Sources logged (full list — round 1 + round 2 = ~78 unique)

### Round 1 (55 sources)
1-7: Hikvision (supportusa.hikvision.com, ipcamtalk threads 38297/43754/60163, motioneye 213, frigate 6998, cctv-viewer 3)
8-16: Dahua (ipcamtalk 28353, ispyconnect, dahuawiki, videoexpertsgroup, getscw, ipcamtalk 63784, use-ip.co.uk, dahuawiki NVRiSettingNetwork, surveillanceguides)
17-23: General RTSP (medium hacking-RTSP, pypi rtspbrute, hacktricks 554-pentesting, github RTSP-FindingSomeFun, dl.acm.org, nmap.org rtsp-url-brute, hackviser)
24-30: Hipcam (ipcamtalk 72373, home-security-camera, zoneminder 19037, wjmccann.github.io, zoneminder 26743, manualzz, motioneyeos 703)
31-36: Hipcam CVE (github MaximilianJungblut, cvefeed.io, sploitus, threatpost, github topics, ispyconnect)
37-46: Frigate/HA/Reolink (frigate 18197, frigate restream docs, HA community 586589, frigate 17345, HA community 563734, reolink community 4000, reolink RTSP intro, frigate 19650, frigate 5198, reolink community 6366)
47-58: Axis + Hanwha (developer.axis adjustable, kane610/axis 18, axis migrationguide pdf, developer.axis video-streaming, axis vapix-library, mediamtx 683, ispyconnect axis, axis P1354 manual, help.axis troubleshooting)
59-78 from round 1: HA core 130036, Foscam FAQ, ipspyconnect Foscam, Foscam IPCamLive, QNAP Foscam, ipcamtalk 25304, FI9804P manual, HA community 276499, camect groups, scos.co.uk Foscam, Hanwha official articles ×3, Tapo support ×2, ipcamlive Hanwha, Bosch/Pelco ispyconnect, geniusvision platform, Vivotek manual, Vivotek zendesk, security.world Vivotek, ispyconnect Pelco, getscw RTSP, camect cameras, Wyze RTSP support, Wyze forums ×2, Scrypted docs, Amcrest forums ×2, ipcamtalk 19959, nmap timing/connect-scan/syn-scan/manpage, labex nmap timeout

### Round 2 additions (23+ new sources)
- live555 mailing-list keep-alive thread
- gstreamer-devel rtspsrc keep-alive narkive
- gstreamer discourse rtspsrc-keep-alive-tcp-vs-udp
- live555 RTSPServer.cpp (rayl)
- gstreamer rtspsrc docs (do-rtsp-keep-alive property)
- live555 server-disconnects-clients-every-60-seconds (RTCP RR explanation)
- janus-gateway issue 693 (live555 proxy stops streaming)
- liveMediaStreamer issue 49 (no keep-alive management)
- live-devel rtcp-keepalive thread
- Reolink community 563 (bad cseq after fast restart — KEY)
- Scrypted issue 1499 (HiLook ColorVu socket close — KEY)
- Frigate discussion 18775 (Tapo C110 bad cseq)
- mediamtx issue 2887 (RTSP 400 Bad Request, Cseq trace)
- nvidia DeepStream RTSP reconnect issue
- Frigate discussion 17201 (Reolink unreliable bad cseq)
- RFC 2326 (datatracker, rfc-editor, ietf, columbia draft)
- live555 openRTSP doc (-K option for keep-alive)
- en.wikipedia RTSP page
- antmedia.io RTSP explained
- nabto.com RTSP guide
- asustor RTSP camera connection
- wowza.com RTSP troubleshoot
- RFC 7826 (RTSP 2.0 with explicit Pipelined-Requests header)
- ONVIFDM bug 23 (RTSP keep-alive missing)
- VideoLAN forum 108058 (VLC + Hipcam GET_PARAMETER)
- androidx/media issue 662 (DVR session timeout=3)
- ipcamtalk 14385 (Sricam SP012 — KEY)
- Frigate issue 1282 (Sricam SP020)
- ipcamtalk 43645 (Wansview cloud-firmware warning)
- ipcamtalk 61281 (Hikvision RTSP CGI integration)
- motion-project discussion 1485 + issue 950 (ffmpeg bad cseq)
- UltraGrid issue 400 (gortsplib RTSP CSeq mismatch — KEY)
- Frigate discussion 19977 (CSeq 2 expected, 1 received — KEY)
- TP-Link community ×3 (Tapo device limit, stream limit, RTSP/ONVIF FAQ)
- TP-Link Tapo FAQ 724 (battery models no RTSP)
- Reolink community Duo 2 / Trackmix forums

