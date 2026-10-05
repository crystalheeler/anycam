"""AnyCam camera knowledge tables.

CAMERA_DB: how to recognise a brand, what is known about its behaviour, and
           (3.5.0, D2) the stream paths to try.
STREAM_DB: the stream paths by slug, built from CAMERA_DB.

Data only, apart from the STREAM_DB builder; nothing here imports the rest
of AnyCam.
Moved out of camera_discovery.py in 3.0.0-rc1.0 (build plan E1, stage 1);
the entries are unchanged. verify_release.py checks the throttle fields.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Camera manufacturer / model database
#
# Each entry:
#   "name"     : canonical display name
#   "aliases"  : alternate spellings / brand families
#   "http_titles"  : substrings to match in HTML <title> (case-insensitive)
#   "http_body"    : substrings to match anywhere in page body (case-insensitive)
#   "http_headers" : substrings to match in any HTTP response header value
#   "nmap_products": substrings to match in nmap service product/version field
#   "onvif_scopes" : substrings to match in ONVIF WS-Discovery scope strings
#   "default_ports": hint ports commonly used by this manufacturer; the scan's
#                    port list includes them (anycam_scan.CAMERA_RELEVANT_PORTS)
#   "streams"      : 3.5.0 (D2): stream paths to try, formerly STREAM_DB. Each:
#       slug  — the stream entry's key in STREAM_DB
#       rank  — its place in STREAM_DB: of two equally long matching
#               keywords, the lower rank wins (_match_stream_db)
#       match — substrings to look for in camera name / vendor / model (lowercase)
#       rtsp  — RTSP path candidates to probe (ordered: most likely first)
#       mjpeg — HTTP MJPEG stream path (None if not supported)
#       snap  — HTTP JPEG snapshot path (None if not available)
#       port  — default RTSP port (554 unless the brand uses something else)
#   "notes"        : human-readable notes shown in Identity section
# ─────────────────────────────────────────────────────────────────────────────

CAMERA_DB: list[dict] = [
    {
        "name": "Hikvision",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "hikvision", "rank": 0,
             "match": ["hikvision", "hikv", "ds-2", "ds-7", "ds-6", "isapi"],
             "rtsp":  ["/Streaming/Channels/101",
                       "/Streaming/Channels/102",
                       "/Streaming/Channels/103",
                       "/ISAPI/Streaming/channels/101",
                       "/ISAPI/Streaming/channels/102",
                       "/h.264/ch1/main/av_stream",
                       "/h.264/ch1/sub/av_stream"],
             "mjpeg": "/ISAPI/Streaming/channels/102/httpPreview",
             "snap":  "/ISAPI/Streaming/channels/101/picture",
             "port":  554},
            {"slug": "ezviz", "rank": 1,
             "match": ["ezviz"],
             "rtsp":  ["/Streaming/Channels/101",
                       "/Streaming/Channels/102"],
             "mjpeg": "/ISAPI/Streaming/channels/102/httpPreview",
             "snap":  "/ISAPI/Streaming/channels/101/picture",
             "port":  554},
        ],
        "aliases": ["hik", "hikvision", "ds-2"],
        "http_titles": ["hikvision", "ds-2", "network camera", "ivms"],
        "http_body":   ["hikvision", "ivms-4200", "ds-2cd", "ds-2de", "hik-connect"],
        "http_headers":["hikvision", "webs/hikvision"],
        "nmap_products":["hikvision", "hikvision ip camera"],
        "onvif_scopes": ["hikvision"],
        "default_ports": [554, 8000, 80],
        "notes": ("Hikvision IP camera or NVR/DVR. URL pattern "
                  "/Streaming/Channels/N0X (101=ch1 main, 102=ch1 sub). "
                  "Multi-layer HEVC (H.265+) requires "
                  "`-fflags +discardcorrupt` to decode reliably. "
                  "Camera-level: no documented throttle. NVR-level: 128 "
                  "concurrent users (configurable, 0=unlimited)."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "128 (NVR-level, configurable, 0=unlim)",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Server-wide cap on NVR products. webSDK browser "
                           "plugin has separate 5-stream cap. Camera-level "
                           "products have no documented connection cap and "
                           "tolerate rapid sequential probes (verified on "
                           "DS-2DE4A425IW)."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("Standard RFC 2326. Digest auth "
                              "(realm typically \"IP Camera(<chipset>)\")."),
        "request_behaviors_confidence":"HIGH",
        # 2.4.0-rc2.4: skip_layer2: True. Hikvision single-camera units
        # (e.g. CrystalHeeler's Hikvision DS-2DE4A425IW PTZ) require auth — observed
        # consistently across all Layer 1 paths returning 401 with the
        # same realm. Layer 2's multi-socket walk produces the same
        # 401s on the same paths because realm/scheme are server-side,
        # not socket-side, per RFC 7235 §2.2. Live-tested: the Hikvision burned
        # ~45s through Layer 2 bail-after-10 before giving up. Setting
        # this flag short-circuits Layer 2 immediately when the camera
        # is in pre-creds discovery state. Users who want to verify
        # Layer 2 didn't miss a firmware-quirk path can click the
        # per-card "Deep Re-Probe" button (rc2.4) which runs Layer 2
        # on demand. NVR variants of Hikvision (Hikvision NVR entry)
        # already have this flag for the same reason.
        "skip_layer2":            True,
        "skip_layer2_confidence": "HIGH",
    },
    {
        "name": "Dahua",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "dahua", "rank": 2,
             "match": ["dahua", "dh-ipc", "dh-sd", "ipc-hfw", "ipc-hdw", "ipc-hdb", "sd4", "sd5", "sd6", "hfw", "hdw"],
             "rtsp":  ["/cam/realmonitor?channel=1&subtype=0",
                       "/cam/realmonitor?channel=1&subtype=1",
                       "/cam/realmonitor?channel=1&subtype=2"],
             "mjpeg": "/cgi-bin/mjpg/video.cgi?channel=1&subtype=1",
             "snap":  "/cgi-bin/snapshot.cgi",
             "port":  554},
            {"slug": "imou", "rank": 3,
             "match": ["imou"],
             "rtsp":  ["/cam/realmonitor?channel=1&subtype=0",
                       "/cam/realmonitor?channel=1&subtype=1"],
             "mjpeg": "/cgi-bin/mjpg/video.cgi?channel=1&subtype=1",
             "snap":  "/cgi-bin/snapshot.cgi",
             "port":  554},
        ],
        "aliases": ["dahua", "dhip", "dh-ipc", "imou"],
        "http_titles": ["dahua", "ipc", "nvr", "xvr", "imou"],
        "http_body":   ["dahua technology", "dahua", "imou", "lechange", "dh-ipc"],
        "http_headers":["dahua", "dh-"],
        "nmap_products":["dahua"],
        "onvif_scopes": ["dahua"],
        "default_ports": [37777, 80, 554],
        "notes": ("Dahua Technology IP camera, NVR/DVR or IMOU device. URL "
                  "pattern /cam/realmonitor?channel=N&subtype=M. Native DVR "
                  "protocol on port 37777. Camera-level: no documented "
                  "throttle. NVR-level: 128 concurrent users "
                  "(configurable, 0=unlimited)."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "128 (NVR-level, configurable, 0=unlim)",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Server-wide cap on NVR products. Camera-level "
                           "products have no documented connection cap."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": "Standard RFC 2326. Digest auth.",
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "Lorex",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "lorex", "rank": 30,
             "match": ["lorex"],
             "rtsp":  ["/cam/realmonitor?channel=1&subtype=0",
                       "/cam/realmonitor?channel=1&subtype=1",
                       "/ch01/0"],
             "mjpeg": None,
             "snap":  "/cgi-bin/snapshot.cgi",
             "port":  554},
        ],
        "aliases": ["lorex", "flir lorex", "flirlorex"],
        "http_titles": ["lorex", "lorex nvr", "lorex dvr", "flirlorex"],
        "http_body":   ["lorex", "lorextechnology", "lorex technology",
                        "flirlorex", "flir lorex"],
        "http_headers":["lorex", "flirlorex"],
        "nmap_products":["lorex"],
        "onvif_scopes": ["lorex"],
        "default_ports": [80, 443, 8080, 8888, 554, 34567],
        "notes": ("Lorex (FLIR-acquired, Dahua-OEM hardware) NVR/DVR or IP "
                  "camera. Inherits Dahua URL patterns and behavior. "
                  "FLIR→Dahua acquisition 2018."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "MED",
        "throttle_amount":          "128 (NVR-level, inherits Dahua)",
        "throttle_amount_confidence":"MED",
        "throttle_notes": "Inherits Dahua throttle behavior (Dahua-OEM hardware).",
        "throttle_notes_confidence":"MED",
        "request_behaviors": "Standard RFC 2326. Inherits Dahua RTSP/HTTP behavior.",
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "Reolink",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "reolink", "rank": 24,
             "match": ["reolink", "rlc-", "rlk-", "rlp-", "rln-"],
             "rtsp":  ["/h264Preview_01_main",
                       "/h264Preview_01_sub",
                       "/Preview_01_main",
                       "/Preview_01_sub",
                       "/h265Preview_01_main"],
             "mjpeg": None,
             "snap":  "/cgi-bin/api.cgi?cmd=Snap&channel=0&rs=AnyCam",
             "port":  554},
        ],
        "aliases": ["reolink"],
        "http_titles": ["reolink"],
        "http_body":   ["reolink", "reolink app"],
        "http_headers":["reolink"],
        "nmap_products":["reolink"],
        "onvif_scopes": ["reolink"],
        "default_ports": [554, 80, 8080, 9000],
        "notes": ("Reolink IP camera or NVR. URL pattern "
                  "/h264Preview_<ch>_<main|sub> (older) or "
                  "/Preview_<ch>_<main|sub> (newer). Some models require "
                  "beta firmware to expose RTSP/ONVIF. Battery-WiFi models "
                  "have 5-min preview cap then sleep+disconnect — needs "
                  "≥20s RTSP timeout. Bad cseq errors after fast restart "
                  "need 5+s pause; some models (C2/RLC-410/420) "
                  "auto-recover after ~30s, others (older RLC-423) require "
                  "camera reboot."),
        "throttle_type":            "restart_cooldown",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "5+s pause between fast restarts (or 30s self-clean / camera reboot)",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Stop/restart RTSP within ~1-2s causes RTP-Cseq "
                           "stream corruption that persists endlessly until "
                           "the cooldown elapses. Battery-WiFi models also "
                           "have a session_time_cap of 5 min preview before "
                           "sleep. 8MP+ models (TrackMix etc.) are reported "
                           "as unstable on RTSP — Neolink workaround often "
                           "required."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("Standard RFC 2326. Battery models need "
                              "extended socket timeout (≥20s) for wake-up. "
                              "Stop processes via stdin 'q' rather than "
                              "SIGKILL to allow clean TEARDOWN."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Axis",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "axis", "rank": 5,
             "match": ["axis"],
             "rtsp":  ["/axis-media/media.amp",
                       "/axis-media/media.amp?videocodec=h264",
                       "/axis-media/media.amp?videocodec=h265",
                       "/mpeg4/media.amp"],
             "mjpeg": "/axis-cgi/mjpg/video.cgi",
             "snap":  "/axis-cgi/jpg/image.cgi",
             "port":  554},
        ],
        "aliases": ["axis communications", "axis network"],
        "http_titles": ["axis", "axis network camera", "axis video"],
        "http_body":   ["axis communications", "axis network camera", "axiscam"],
        "http_headers":["axis", "boa/"],
        "nmap_products":["axis network camera", "axis"],
        "onvif_scopes": ["axis"],
        "default_ports": [554, 80, 443],
        "notes": ("Axis Communications IP camera or encoder. URL "
                  "/axis-media/media.amp. GStreamer-based RTSP server. "
                  "Session timeout=60s default; OPTIONS keepalive every "
                  "30s recommended. Multi-session-on-one-TCP supported. "
                  "Companion line requires `Axis-Orig-Sw=true` query "
                  "param appended to RTSP URL — without it, returns 403. "
                  "Firmware 6.50+ returns 454 Session Not Found on session "
                  "timeout. ONVIF Profile S/G/T."),
        "throttle_type":            "unique_profile_cap",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "\"Too many viewers\" based on unique encoded profiles",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Cap is per UNIQUE stream profile, not per "
                           "client. If all clients request identical "
                           "settings, camera encodes once and serves all. "
                           "Companion variant returns 403 without "
                           "`Axis-Orig-Sw=true` query param — retry-on-403 "
                           "with that param appended works. Standards-strict "
                           "RFC 2326 implementation."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("RFC 2326 strict. Digest auth. Some models "
                              "(Companion) require `?Axis-Orig-Sw=true` "
                              "query param. SRTP/SRTCP supported on newer "
                              "firmware. RTSPS on rtsps:// URL."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Hanwha / Samsung Techwin",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "hanwha", "rank": 6,
             "match": ["hanwha", "wisenet", "samsung", "snv-", "xnv-", "qnv-", "pnv-", "qnd-", "xnd-", "pnd-"],
             "rtsp":  ["/profile1/media.smp",
                       "/profile2/media.smp",
                       "/profile10/media.smp",
                       "/0/profile2/media.smp"],
             "mjpeg": "/stw-cgi/video.cgi?msubmenu=stream&action=view&Profile=1&CodecType=MJPEG&Resolution=800x450&FrameRate=15&CompressionLevel=10",
             "snap":  "/stw-cgi/image.cgi?msubmenu=snapshot&action=view",
             "port":  554},
        ],
        "aliases": ["hanwha", "samsung techwin", "wisenet", "qnv", "xnv"],
        "http_titles": ["wisenet", "hanwha", "samsung techwin", "snv-", "qnv-"],
        "http_body":   ["hanwha", "wisenet", "samsung techwin"],
        "http_headers":["hanwha", "wisenet"],
        "nmap_products":["hanwha", "wisenet", "samsung techwin"],
        "onvif_scopes": ["hanwha", "samsung"],
        "default_ports": [554, 80, 8080],
        "notes": ("Hanwha Vision (formerly Samsung Techwin) — Wisenet "
                  "series. URL /profile<N>/media.smp; multi-sensor models "
                  "use /<sensor#>/profile<N>/media.smp (e.g. "
                  "/0/profile2/media.smp). Ports 3702 and 49152 are "
                  "RESERVED (ONVIF discovery + RTP) — DO NOT use as "
                  "RTSP. Requires shared Session ID across all requests "
                  "from one client; mismatched IDs counted as separate "
                  "users."),
        "throttle_type":            "shared_session_id_required",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "All requests from one client must share Session ID",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("If a client opens multiple RTSP requests with "
                           "different Session IDs, the camera counts each "
                           "as a separate viewer (consuming concurrent-user "
                           "quota). Probe behavior is unaffected — our "
                           "probe never reuses Session IDs across paths "
                           "(each probe is independent). Documented for "
                           "future warning to users with NVR setups."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("Standard RFC 2326 with strict Session ID "
                              "tracking. Digest auth. Profile-G recording "
                              "and Profile-T analytics support."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Amcrest",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "amcrest", "rank": 4,
             "match": ["amcrest"],
             "rtsp":  ["/cam/realmonitor?channel=1&subtype=0",
                       "/cam/realmonitor?channel=1&subtype=1"],
             "mjpeg": "/cgi-bin/mjpg/video.cgi?channel=1&subtype=1",
             "snap":  "/cgi-bin/snapshot.cgi",
             "port":  554},
        ],
        "aliases": ["amcrest", "amcrest technologies"],
        "http_titles": ["amcrest", "amcrest ip"],
        "http_body":   ["amcrest", "amcrestsecurity"],
        "http_headers":["amcrest"],
        "nmap_products":["amcrest"],
        "onvif_scopes": ["amcrest"],
        "default_ports": [37777, 80, 554],
        "notes": ("Amcrest IP camera or NVR (Dahua-OEM hardware). "
                  "Inherits Dahua URL patterns and throttle behavior."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "MED",
        "throttle_amount":          "128 (NVR-level, inherits Dahua)",
        "throttle_amount_confidence":"MED",
        "throttle_notes": "Inherits Dahua throttle behavior (Dahua-OEM hardware).",
        "throttle_notes_confidence":"MED",
        "request_behaviors": "Standard RFC 2326. Inherits Dahua RTSP/HTTP behavior.",
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "Uniview (UNV)",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "uniview", "rank": 7,
             "match": ["uniview", "unv", "ipc3", "ipc6", "ipc8"],
             "rtsp":  ["/media/video1",
                       "/media/video2",
                       "/media/video3"],
             "mjpeg": None,
             "snap":  "/images/snapshot.jpg",
             "port":  554},
        ],
        "aliases": ["uniview", "unv", "univideo"],
        "http_titles": ["uniview", "unv", "network camera"],
        "http_body":   ["uniview", "univideo", "unv camera"],
        "http_headers":["uniview", "unv"],
        "nmap_products":["uniview", "unv"],
        "onvif_scopes": ["uniview"],
        "default_ports": [554, 80],
        "notes": ("Uniview (UNV) IP camera or NVR. URL pattern "
                  "/media/video<N>. H.265 buggy on many models — Camect "
                  "documentation explicitly recommends H.264 over H.265 "
                  "on UNV cameras. No documented connection-rate "
                  "throttle."),
        "request_behaviors": ("Standard RFC 2326. Recommend H.264 "
                              "transport over H.265 for stream stability."),
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "Vivotek",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "vivotek", "rank": 8,
             "match": ["vivotek", "vivo", "fd8", "fd9", "ip8", "ip9", "cc8", "ms8"],
             "rtsp":  ["/live1s1",
                       "/live1s2",
                       "/live.sdp",
                       "/live2.sdp"],
             "mjpeg": "/video.mjpg",
             "snap":  "/cgi-bin/viewer/video.jpg",
             "port":  554},
        ],
        "aliases": ["vivotek"],
        "http_titles": ["vivotek", "network camera", "ip camera"],
        "http_body":   ["vivotek", "vvtk"],
        "http_headers":["vivotek", "vvtk-http"],
        "nmap_products":["vivotek"],
        "onvif_scopes": ["vivotek"],
        "default_ports": [554, 80, 8080],
        "notes": ("Vivotek IP camera or NVR. URL /live<N>s<M> or "
                  "/live.sdp. Documented 10-user concurrent limit (browser "
                  "+ VMS + NVR ALL count toward this). Server header: "
                  "\"Vivotek RtspServer\"."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "10 concurrent users (browser+VMS+NVR all count)",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Camera-level cap. Documented in Vivotek "
                           "support article. Does not affect rapid "
                           "sequential probes (we close socket between "
                           "paths, which releases the slot)."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("Standard RFC 2326. Server header "
                              "\"Vivotek RtspServer\" is identifying."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Bosch",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "bosch", "rank": 9,
             "match": ["bosch", "ndc-", "nti-", "nbn-", "nbe-", "nuc-"],
             "rtsp":  ["/rtsp_tunnel",
                       "/?inst=1",
                       "/?inst=2"],
             "mjpeg": None,
             "snap":  "/snap.jpg",
             "port":  554},
        ],
        "aliases": ["bosch security", "bosch camera", "autodome", "flexidome", "dinion"],
        "http_titles": ["bosch", "autodome", "flexidome", "dinion"],
        "http_body":   ["bosch security", "bosch camera", "dinion", "flexidome", "autodome"],
        "http_headers":["bosch"],
        "nmap_products":["bosch"],
        "onvif_scopes": ["bosch"],
        "default_ports": [554, 80, 443],
        "notes": "Bosch Security Systems IP camera",
    },
    {
        "name": "Pelco",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "pelco", "rank": 10,
             "match": ["pelco", "sarix", "optera", "spectra"],
             "rtsp":  ["/stream1",
                       "/stream2",
                       "/?video"],
             "mjpeg": "/media/mjpeg",
             "snap":  "/media/jpeg",
             "port":  554},
        ],
        "aliases": ["pelco", "sarix", "spectra", "optera"],
        "http_titles": ["pelco", "sarix", "spectra enhanced"],
        "http_body":   ["pelco", "sarix", "pelco.com"],
        "http_headers":["pelco"],
        "nmap_products":["pelco"],
        "onvif_scopes": ["pelco"],
        "default_ports": [554, 80],
        "notes": "Pelco IP camera (Motorola Solutions)",
    },
    {
        "name": "Sony",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "sony", "rank": 20,
             "match": ["sony", "snc-", "srg-", "srd-"],
             "rtsp":  ["/media/video1",
                       "/media/video2"],
             "mjpeg": "/image?speed=1&size=3",
             "snap":  "/oneshotimage.jpg",
             "port":  554},
        ],
        "aliases": ["sony ipela", "sony security", "snc-"],
        "http_titles": ["sony", "sony ipela", "snc-"],
        "http_body":   ["sony ipela", "sony security", "snc-rz", "snc-ep", "snc-vb"],
        "http_headers":["sony"],
        "nmap_products":["sony network camera", "sony ipela"],
        "onvif_scopes": ["sony"],
        "default_ports": [554, 80, 443],
        "notes": "Sony IPELA IP camera",
    },
    {
        "name": "Panasonic / i-PRO",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "panasonic", "rank": 14,
             "match": ["panasonic", "wv-s", "wv-x", "wv-v", "wv-u", "wv-sc", "bl-c", "i-pro"],
             "rtsp":  ["/MediaInput/h264",
                       "/MediaInput/h264/stream_1/ch_1",
                       "/MediaInput/h264/stream_2/ch_1"],
             "mjpeg": "/nphMotionJpeg?Resolution=640x480&Quality=Standard",
             "snap":  "/SnapShotJPEG?Resolution=640x480&Quality=Clarity",
             "port":  554},
        ],
        "aliases": ["panasonic", "i-pro", "ipro", "wv-"],
        "http_titles": ["panasonic", "i-pro", "network camera", "wv-"],
        "http_body":   ["panasonic", "i-pro", "wv-sc", "wv-sf", "wv-sp"],
        "http_headers":["panasonic", "i-pro"],
        "nmap_products":["panasonic network camera", "i-pro"],
        "onvif_scopes": ["panasonic"],
        "default_ports": [554, 80, 443],
        "notes": "Panasonic / i-PRO IP camera",
    },
    {
        "name": "Avigilon",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "avigilon", "rank": 11,
             "match": ["avigilon"],
             "rtsp":  ["/defaultPrimary?streamType=u",
                       "/defaultSecondary?streamType=u",
                       "/defaultPrimary-0?streamType=u",
                       "/defaultPrimary-1?streamType=u"],
             "mjpeg": None,
             "snap":  None,
             "port":  554},
        ],
        "aliases": ["avigilon", "motorola solutions"],
        "http_titles": ["avigilon"],
        "http_body":   ["avigilon", "avigilon corporation"],
        "http_headers":["avigilon"],
        "nmap_products":["avigilon"],
        "onvif_scopes": ["avigilon"],
        "default_ports": [554, 80, 443],
        "notes": "Avigilon (Motorola Solutions) IP camera or NVR",
    },
    {
        "name": "FLIR",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "flir", "rank": 37,
             "match": ["flir"],
             "rtsp":  ["/avc",
                       "/avc/ch1"],
             "mjpeg": None,
             "snap":  None,
             "port":  554},
        ],
        "aliases": ["flir systems", "flir camera"],
        "http_titles": ["flir", "flir systems"],
        "http_body":   ["flir systems", "flir camera", "flir.com"],
        "http_headers":["flir"],
        "nmap_products":["flir"],
        "onvif_scopes": ["flir"],
        "default_ports": [554, 80, 443],
        "notes": "FLIR Systems thermal/optical camera",
    },
    {
        "name": "Mobotix",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "mobotix", "rank": 12,
             "match": ["mobotix", "mx-", "mxfb"],
             "rtsp":  ["/mobotix.h264",
                       "/stream/profile0",
                       "/stream/profile1",
                       "/onvif/stream0/mobotix.mjpeg"],
             "mjpeg": "/cgi-bin/faststream.jpg?stream=MxPEG",
             "snap":  "/cgi-bin/faststream.jpg?stream=snapshot",
             "port":  554},
        ],
        "aliases": ["mobotix"],
        "http_titles": ["mobotix", "mx-"],
        "http_body":   ["mobotix", "mx-q", "mx-s", "mobotix.com"],
        "http_headers":["mobotix", "mx-httpd"],
        "nmap_products":["mobotix"],
        "onvif_scopes": ["mobotix"],
        "default_ports": [554, 80, 443],
        "notes": "Mobotix IP camera",
    },
    {
        "name": "ACTi",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "acti", "rank": 15,
             "match": ["acti", "tcm-", "kce-", "e21", "e22", "e23", "e24", "e31"],
             "rtsp":  ["/track1",
                       "/track2"],
             "mjpeg": None,
             "snap":  "/snapshot.jpg",
             "port":  554},
        ],
        "aliases": ["acti", "acti corporation"],
        "http_titles": ["acti"],
        "http_body":   ["acti corporation", "acti camera"],
        "http_headers":["acti"],
        "nmap_products":["acti"],
        "onvif_scopes": ["acti"],
        "default_ports": [554, 80, 443],
        "notes": "ACTi IP camera",
    },
    {
        "name": "GeoVision",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "geovision", "rank": 13,
             "match": ["geovision", "gv-", "geo-"],
             "rtsp":  ["/CH001.sdp",
                       "/CH002.sdp",
                       "/h264.sdp"],
             "mjpeg": "/mjpeg?cam=1",
             "snap":  "/PictureCatch.cgi?CH=1",
             "port":  8554},
        ],
        "aliases": ["geovision", "gv-"],
        "http_titles": ["geovision", "gv-"],
        "http_body":   ["geovision", "geo vision", "gv-bx", "gv-ptz"],
        "http_headers":["geovision"],
        "nmap_products":["geovision"],
        "onvif_scopes": ["geovision"],
        "default_ports": [554, 80, 4550],
        "notes": "GeoVision IP camera or NVR",
    },
    {
        "name": "Foscam",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "foscam", "rank": 25,
             "match": ["foscam", "fi8", "fi9", "r2", "r4"],
             "rtsp":  ["/videoMain",
                       "/videoSub"],
             "mjpeg": "/videostream.cgi",
             "snap":  "/cgi-bin/CGIProxy.fcgi?cmd=snapPicture2",
             "port":  88},
        ],
        "aliases": ["foscam"],
        "http_titles": ["foscam", "ip camera"],
        "http_body":   ["foscam", "foscam digital technologies"],
        "http_headers":["foscam"],
        "nmap_products":["foscam"],
        "onvif_scopes": ["foscam"],
        "default_ports": [554, 88, 80, 443],
        "notes": ("Foscam IP camera. URL paths /videoMain (high-res) and "
                  "/videoSub (low-res); audio at /audio. Some firmware "
                  "variants (FI9821P V3, FI98xx V3 series) use port 88 "
                  "for BOTH HTTP and RTSP, not the standard 554. Digest "
                  "auth required. ~4 concurrent connection cap reported "
                  "across community."),
        "throttle_type":            "concurrent_stream_cap",
        "throttle_type_confidence": "MED",
        "throttle_amount":          "~4 concurrent connections (community-reported)",
        "throttle_amount_confidence":"MED",
        "throttle_notes": ("Community-reported pattern across multiple "
                           "users. Does not affect rapid sequential "
                           "probes (we close socket between paths). "
                           "Some V3 firmware uses port 88 for both HTTP "
                           "and RTSP — try 88 if 554 connect fails."),
        "throttle_notes_confidence":"MED",
        "request_behaviors": ("Digest auth required. Some firmware "
                              "variants run RTSP on port 88 not 554."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Annke",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "annke", "rank": 27,
             "match": ["annke"],
             "rtsp":  ["/H264/ch1/main/av_stream",
                       "/H264/ch1/sub/av_stream",
                       "/Streaming/channels/101",
                       "/Streaming/channels/102"],
             "mjpeg": "/ISAPI/Streaming/channels/102/httpPreview",
             "snap":  "/ISAPI/Streaming/channels/101/picture",
             "port":  554},
        ],
        "aliases": ["annke"],
        "http_titles": ["annke"],
        "http_body":   ["annke", "annke.com"],
        "http_headers":["annke"],
        "nmap_products":["annke"],
        "onvif_scopes": ["annke"],
        "default_ports": [554, 80, 8000],
        "notes": ("Annke IP camera or NVR/DVR (Hikvision OEM hardware "
                  "for IP line; Dahua-OEM for some DVR products). "
                  "Inherits upstream throttle behavior."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "MED",
        "throttle_amount":          "Inherits upstream (Hikvision/Dahua)",
        "throttle_amount_confidence":"MED",
        "throttle_notes": ("OEM rebrand — behavior inherits from upstream "
                           "manufacturer (Hikvision IP, Dahua DVR)."),
        "throttle_notes_confidence":"MED",
        "request_behaviors": "Inherits Hikvision/Dahua RTSP/HTTP behavior.",
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "Swann",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "swann", "rank": 35,
             "match": ["swann"],
             "rtsp":  ["/ch01/0",
                       "/ch01/1",
                       "/Streaming/Channels/101",
                       "/cam/realmonitor?channel=1&subtype=0"],
             "mjpeg": None,
             "snap":  "/ISAPI/Streaming/channels/101/picture",
             "port":  554},
        ],
        "aliases": ["swann", "swann communications"],
        "http_titles": ["swann"],
        "http_body":   ["swann", "swann security", "swann communications"],
        "http_headers":["swann"],
        "nmap_products":["swann"],
        "onvif_scopes": ["swann"],
        "default_ports": [554, 80, 34567],
        "notes": ("Swann security camera or NVR/DVR. OEM rebrand — uses "
                  "Hikvision, Dahua, or Wansview hardware depending on "
                  "model. Behavior inherits from upstream manufacturer."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "LOW",
        "throttle_amount":          "Inherits upstream (Hikvision/Dahua/Wansview)",
        "throttle_amount_confidence":"LOW",
        "throttle_notes": ("OEM rebrand — varies by model. Some Swann "
                           "products are Wansview-OEM (Hipcam family) "
                           "and inherit that rate-limit behavior."),
        "throttle_notes_confidence":"MED",
        "request_behaviors": "Varies by hardware (Hikvision/Dahua/Wansview OEM).",
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "TP-Link Tapo / Kasa",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "tplink", "rank": 26,
             "match": ["tapo", "tp-link", "tplink", "c100", "c200", "c300", "c310", "c320", "vigi"],
             "rtsp":  ["/stream1",
                       "/stream2"],
             "mjpeg": None,
             "snap":  None,
             "port":  554},
        ],
        "aliases": ["tapo", "kasa", "tp-link"],
        "http_titles": ["tapo", "kasa", "tp-link"],
        "http_body":   ["tapo", "tp-link tapo", "kasa camera"],
        "http_headers":["tp-link", "tapo"],
        "nmap_products":["tp-link", "tapo"],
        "onvif_scopes": ["tapo", "tp-link"],
        "default_ports": [554, 2020, 80],
        "notes": ("TP-Link Tapo / Kasa smart camera. URLs /stream1 "
                  "(main) and /stream2 (sub). ONVIF service runs on "
                  "port 2020 (NOT 80 or 8080). Battery-powered models "
                  "(C410, C420, C425, D230) do NOT support RTSP — "
                  "cloud-only by design. Tapo Care subscription occupies "
                  "1 main stream slot. Camera-account credentials are "
                  "separate from Tapo cloud login."),
        "throttle_type":            "concurrent_stream_cap",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "2 main + 2 sub streams max (4 total)",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Each RTSP/ONVIF connection occupies one "
                           "stream slot. Tapo Care subscription (cloud "
                           "recording) consumes 1 main slot. Battery "
                           "models (C410/C420/C425/D230) have no RTSP "
                           "at all. Reboot the camera to disconnect all "
                           "stream slots when stuck."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("ONVIF on port 2020 (NOT 80). Standard "
                              "RTSP on 554. Battery models — no RTSP "
                              "support."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Night Owl",
        "aliases": ["night owl", "nightowl"],
        "http_titles": ["night owl"],
        "http_body":   ["night owl", "nightowl security"],
        "http_headers":["night owl"],
        "nmap_products":["night owl"],
        "onvif_scopes": ["nightowl"],
        "default_ports": [554, 80, 34567],
        "notes": ("Night Owl security camera or NVR/DVR. OEM rebrand — "
                  "uses Hikvision or Dahua hardware. Native DVR protocol "
                  "on port 34567 (Hisilicon-based)."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "LOW",
        "throttle_amount":          "Inherits upstream (Hikvision/Dahua)",
        "throttle_amount_confidence":"LOW",
        "throttle_notes": "OEM rebrand — behavior inherits from upstream manufacturer.",
        "throttle_notes_confidence":"LOW",
        "request_behaviors": "Inherits Hikvision/Dahua RTSP/HTTP behavior.",
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "iENSO",
        "aliases": ["ienso", "ienso inc", "ienso camera"],
        "http_titles": ["ienso", "ienso camera", "ienso inc"],
        "http_body":   ["ienso", "ienso inc", "ienso.com", "made in canada"],
        "http_headers":["ienso"],
        "nmap_products":["ienso"],
        "onvif_scopes": ["ienso"],
        "default_ports": [554, 80, 8080],
        "notes": "iENSO embedded IP camera (Canada)",
    },
    {
        "name": "Digital Watchdog",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "digital_watchdog", "rank": 19,
             "match": ["digital watchdog", "dw-", "dwc-"],
             "rtsp":  ["/1/stream1",
                       "/1/stream2"],
             "mjpeg": None,
             "snap":  None,
             "port":  554},
        ],
        "aliases": ["digital watchdog", "dw-"],
        "http_titles": ["digital watchdog", "dw megazip"],
        "http_body":   ["digital watchdog", "dwipnetwork", "dw.com"],
        "http_headers":["digital watchdog"],
        "nmap_products":["digital watchdog"],
        "onvif_scopes": ["digitalwatchdog"],
        "default_ports": [554, 80],
        "notes": "Digital Watchdog IP camera or NVR",
    },
    {
        "name": "March Networks",
        "aliases": ["march networks", "marchnetworks"],
        "http_titles": ["march networks"],
        "http_body":   ["march networks", "marchnetworks.com"],
        "http_headers":["march networks"],
        "nmap_products":["march networks"],
        "onvif_scopes": ["marchnetworks"],
        "default_ports": [554, 80],
        "notes": "March Networks IP camera or NVR",
    },
    {
        "name": "Nest / Google",
        "aliases": ["nest", "google nest"],
        "http_titles": ["nest", "dropcam"],
        "http_body":   ["nest labs", "google nest", "dropcam"],
        "http_headers":["nest"],
        "nmap_products":["nest", "dropcam"],
        "onvif_scopes": ["nest"],
        "default_ports": [554, 443, 80],
        "notes": ("Google Nest / Dropcam IP camera. CLOUD-ONLY by "
                  "design — no RTSP, ONVIF, or local stream API. Local "
                  "access requires reverse-engineered workarounds "
                  "(unsupported)."),
        "throttle_type":            "no_rtsp_support",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "n/a",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("No RTSP server present on device. Cloud-only "
                           "by manufacturer policy."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": "No local stream protocols supported.",
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Ring",
        "aliases": ["ring", "ring doorbell", "ring camera"],
        "http_titles": ["ring"],
        "http_body":   ["ring.com", "ring video", "ring doorbell"],
        "http_headers":["ring"],
        "nmap_products":["ring"],
        "onvif_scopes": ["ring"],
        "default_ports": [554, 443, 80],
        "notes": ("Ring doorbell or security camera (Amazon). CLOUD-ONLY "
                  "by design — no RTSP or ONVIF support."),
        "throttle_type":            "no_rtsp_support",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "n/a",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": "No RTSP server present on device. Cloud-only by Amazon design.",
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": "No local stream protocols supported.",
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Wyze",
        "aliases": ["wyze", "wyze cam"],
        "http_titles": ["wyze"],
        "http_body":   ["wyze", "wyzecam", "wyze cam"],
        "http_headers":["wyze"],
        "nmap_products":["wyze"],
        "onvif_scopes": ["wyze"],
        "default_ports": [554, 80],
        "notes": ("Wyze IP camera. RTSP NOT enabled by default — "
                  "requires flashing custom RTSP firmware (currently "
                  "in beta status; Wyze removed firmware files from "
                  "site). 3+ Wyze cameras on one network reportedly "
                  "causes instability."),
        "throttle_type":            "requires_custom_firmware",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "RTSP firmware aging/beta; not officially supported",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Wyze stock firmware has no RTSP. Beta RTSP "
                           "firmware exists but support is aging — "
                           "files removed from Wyze website. 3+ "
                           "cameras on same network → community-reported "
                           "instability."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("With custom firmware, standard RTSP. "
                              "Without it, no local stream protocols."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Eufy / Anker",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "eufy", "rank": 33,
             "match": ["eufy", "eufycam"],
             "rtsp":  ["/live0"],
             "mjpeg": None,
             "snap":  None,
             "port":  554},
        ],
        "aliases": ["eufy", "anker", "eufysecurity"],
        "http_titles": ["eufy", "eufysecurity"],
        "http_body":   ["eufy", "eufysecurity", "anker innovations"],
        "http_headers":["eufy"],
        "nmap_products":["eufy"],
        "onvif_scopes": ["eufy"],
        "default_ports": [554, 80, 443],
        "notes": ("Eufy (Anker) IP camera. CLOUD-ONLY by design — most "
                  "models do not expose RTSP. Some HomeBase-paired "
                  "cameras can be enabled for local RTSP via the Eufy "
                  "app (URL /live0) but the feature is not universal."),
        "throttle_type":            "no_rtsp_support",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "n/a (most models)",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("No RTSP server on most models. HomeBase users "
                           "can opt in to local RTSP per-camera in the "
                           "Eufy mobile app, exposing /live0."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("Cloud-only by default. Optional local "
                              "RTSP /live0 on HomeBase-paired models."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Arlo",
        "aliases": ["arlo", "arlo technologies"],
        "http_titles": ["arlo"],
        "http_body":   ["arlo", "arlo technologies", "netgear arlo"],
        "http_headers":["arlo"],
        "nmap_products":["arlo"],
        "onvif_scopes": ["arlo"],
        "default_ports": [554, 443, 80],
        "notes": ("Arlo wireless IP camera. CLOUD-ONLY by design — no "
                  "RTSP or ONVIF support."),
        "throttle_type":            "no_rtsp_support",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "n/a",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": "No RTSP server on device. Cloud-only by Arlo design.",
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": "No local stream protocols supported.",
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Verkada",
        "aliases": ["verkada"],
        "http_titles": ["verkada"],
        "http_body":   ["verkada", "verkada command"],
        "http_headers":["verkada"],
        "nmap_products":["verkada"],
        "onvif_scopes": ["verkada"],
        "default_ports": [443, 80],
        "notes": ("Verkada cloud-managed IP camera. Hardware is "
                  "Vivotek-OEM. CLOUD-ONLY API — no local RTSP exposed "
                  "to end users."),
        "throttle_type":            "no_rtsp_support",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "n/a (cloud-only)",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Cloud-only by design. Underlying Vivotek "
                           "hardware is locked behind Verkada Command "
                           "platform — no direct RTSP access."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": "Cloud-only. No local stream protocols exposed.",
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Luxonis / OAK",
        "aliases": ["luxonis", "oak-d", "oak camera", "depthAI"],
        "http_titles": ["luxonis", "oak"],
        "http_body":   ["luxonis", "oak-d", "depthai", "oak camera"],
        "http_headers":["luxonis"],
        "nmap_products":["luxonis", "mediamtx", "oak"],
        "onvif_scopes": ["luxonis"],
        "default_ports": [8765, 554, 80],
        "notes": "Luxonis OAK-D depth/AI camera",
    },
    {
        "name": "Tiandy",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "tiandy", "rank": 16,
             "match": ["tiandy", "tc-c", "tc-h", "tc-r"],
             "rtsp":  ["/profile1",
                       "/profile2"],
             "mjpeg": None,
             "snap":  None,
             "port":  554},
        ],
        "aliases": ["tiandy"],
        "http_titles": ["tiandy"],
        "http_body":   ["tiandy", "tiandy technologies", "tiandy.com"],
        "http_headers":["tiandy"],
        "nmap_products":["tiandy"],
        "onvif_scopes": ["tiandy"],
        "default_ports": [554, 80, 8000],
        "notes": "Tiandy Technologies IP camera or NVR",
    },
    {
        "name": "IndigoVision",
        "aliases": ["indigovision"],
        "http_titles": ["indigovision"],
        "http_body":   ["indigovision", "indigovision.com"],
        "http_headers":["indigovision"],
        "nmap_products":["indigovision"],
        "onvif_scopes": ["indigovision"],
        "default_ports": [554, 80, 443],
        "notes": "IndigoVision IP camera or NVR (Scotland)",
    },
    {
        "name": "Q-See",
        "aliases": ["q-see", "qsee"],
        "http_titles": ["q-see", "qsee"],
        "http_body":   ["q-see", "qsee", "q-see technologies"],
        "http_headers":["q-see"],
        "nmap_products":["q-see", "qsee"],
        "onvif_scopes": ["qsee"],
        "default_ports": [554, 80, 34567],
        "notes": ("Q-See consumer DVR/NVR or IP camera. OEM rebrand — "
                  "uses Hikvision or Dahua hardware. Native DVR protocol "
                  "on port 34567 (Hisilicon)."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "LOW",
        "throttle_amount":          "Inherits upstream (Hikvision/Dahua)",
        "throttle_amount_confidence":"LOW",
        "throttle_notes": "OEM rebrand — behavior inherits from upstream.",
        "throttle_notes_confidence":"LOW",
        "request_behaviors": "Inherits Hikvision/Dahua RTSP/HTTP behavior.",
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "LaView",
        "aliases": ["laview"],
        "http_titles": ["laview"],
        "http_body":   ["laview", "laview technology", "laview.us"],
        "http_headers":["laview"],
        "nmap_products":["laview"],
        "onvif_scopes": ["laview"],
        "default_ports": [554, 80, 8080],
        "notes": ("LaView IP camera or NVR. OEM rebrand — uses Hikvision "
                  "or Dahua hardware."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "LOW",
        "throttle_amount":          "Inherits upstream (Hikvision/Dahua)",
        "throttle_amount_confidence":"LOW",
        "throttle_notes": "OEM rebrand — behavior inherits from upstream.",
        "throttle_notes_confidence":"LOW",
        "request_behaviors": "Inherits Hikvision/Dahua RTSP/HTTP behavior.",
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "Zosi",
        "aliases": ["zosi"],
        "http_titles": ["zosi"],
        "http_body":   ["zosi", "zosi security", "zositechnology"],
        "http_headers":["zosi"],
        "nmap_products":["zosi"],
        "onvif_scopes": ["zosi"],
        "default_ports": [554, 80, 34567],
        "notes": ("Zosi budget security camera or NVR/DVR. OEM rebrand — "
                  "uses Hikvision or Dahua hardware. Native DVR on "
                  "port 34567 (Hisilicon)."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "LOW",
        "throttle_amount":          "Inherits upstream (Hikvision/Dahua)",
        "throttle_amount_confidence":"LOW",
        "throttle_notes": "OEM rebrand — behavior inherits from upstream.",
        "throttle_notes_confidence":"LOW",
        "request_behaviors": "Inherits Hikvision/Dahua RTSP/HTTP behavior.",
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "Sricam / Srihome",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "sricam", "rank": 38,
             "match": ["sricam", "ipcam", "generic"],
             "rtsp":  ["/11",
                       "/12",
                       "/1",
                       "/2",
                       "/onvif1"],
             "mjpeg": "/videostream.cgi",
             "snap":  "/tmpfs/snap.jpg",
             "port":  554},
        ],
        "aliases": ["sricam", "srihome"],
        "http_titles": ["sricam", "srihome"],
        "http_body":   ["sricam", "srihome", "sricam.com"],
        "http_headers":["sricam", "srihome", "rtspserver_0.0.0"],
        "nmap_products":["sricam", "srihome"],
        "onvif_scopes": ["sricam", "srihome"],
        "default_ports": [554, 80, 8080],
        "notes": ("Sricam / Srihome budget IP camera. Uses Hipcam "
                  "RealServer firmware family (see Hipcam/Microseven "
                  "entry for shared throttle behavior). URL path "
                  "/onvif2 also seen on some models. Server header: "
                  "\"RtspServer_0.0.0.2\"."),
        "throttle_type":            "rate_limit_per_ip_tcp",
        "throttle_type_confidence": "MED",
        "throttle_amount":          "~5s cooldown (inherits Hipcam family)",
        "throttle_amount_confidence":"MED",
        "throttle_notes": ("Hipcam RealServer firmware family — see "
                           "Hipcam/Microseven entry for full throttle "
                           "details and probe strategy."),
        "throttle_notes_confidence":"MED",
        "request_behaviors": ("Hipcam family — Digest auth (realm="
                              "\"Hipcam RealServer\"). URLs /11, /12, "
                              "/onvif2."),
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "Vstarcam",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "vstarcam", "rank": 34,
             "match": ["vstarcam", "c7", "c8", "c9"],
             "rtsp":  ["/udp/av0_0",
                       "/udp/av0_1",
                       "/tcp/av0_0",
                       "/udp/av0_2"],
             "mjpeg": "/videostream.cgi",
             "snap":  None,
             "port":  554},
        ],
        "aliases": ["vstarcam"],
        "http_titles": ["vstarcam"],
        "http_body":   ["vstarcam", "vstarcam.com"],
        "http_headers":["vstarcam"],
        "nmap_products":["vstarcam"],
        "onvif_scopes": ["vstarcam"],
        "default_ports": [554, 80, 8080],
        "notes": ("Vstarcam budget WiFi IP camera. Uses Hipcam "
                  "RealServer firmware family on most models — see "
                  "Hipcam/Microseven entry for shared throttle "
                  "behavior."),
        "throttle_type":            "rate_limit_per_ip_tcp",
        "throttle_type_confidence": "MED",
        "throttle_amount":          "~5s cooldown (inherits Hipcam family)",
        "throttle_amount_confidence":"MED",
        "throttle_notes": ("Hipcam RealServer firmware family — see "
                           "Hipcam/Microseven entry for full throttle "
                           "details."),
        "throttle_notes_confidence":"MED",
        "request_behaviors": ("Hipcam family. URLs /udp/av0_0, "
                              "/tcp/av0_0 also seen on some models."),
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "Wansview",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "wansview", "rank": 32,
             "match": ["wansview", "ncm-", "ncb-", "w2", "w3", "w4", "w5", "w6", "q5", "k1", "k2"],
             "rtsp":  ["/live/ch0",
                       "/live/ch1",
                       "/live/mpeg4"],
             "mjpeg": "/videostream.cgi",
             "snap":  "/mjpeg/snap.cgi?chn=0",
             "port":  554},
        ],
        "aliases": ["wansview"],
        "http_titles": ["wansview"],
        "http_body":   ["wansview", "wansview.com"],
        "http_headers":["wansview"],
        "nmap_products":["wansview"],
        "onvif_scopes": ["wansview"],
        "default_ports": [554, 80, 8080],
        "notes": ("Wansview budget IP camera. W2/W3 models use Hipcam "
                  "RealServer firmware family — URL /live/ch0. "
                  "WARNING: newer firmware versions are CLOUD-ONLY with "
                  "NO RTSP or ONVIF support (Wansview switched to "
                  "cloud-only on most products). Block firmware updates "
                  "via DNS to keep RTSP working on W2/W3."),
        "throttle_type":            "rate_limit_per_ip_tcp",
        "throttle_type_confidence": "MED",
        "throttle_amount":          "W2/W3: ~5s cooldown (Hipcam family); newer: no RTSP",
        "throttle_amount_confidence":"MED",
        "throttle_notes": ("W2/W3 inherit Hipcam family throttle. "
                           "Post-2019 firmware on most other models is "
                           "cloud-only with no RTSP support at all."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("W2/W3 (older FW): Hipcam family, URL "
                              "/live/ch0. Newer FW: cloud-only, no "
                              "RTSP."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Tenvis",
        "aliases": ["tenvis"],
        "http_titles": ["tenvis"],
        "http_body":   ["tenvis", "tenvis technology"],
        "http_headers":["tenvis"],
        "nmap_products":["tenvis"],
        "onvif_scopes": ["tenvis"],
        "default_ports": [554, 80, 8080],
        "notes": ("Tenvis IP camera. Uses Hipcam RealServer firmware "
                  "family on many models — see Hipcam/Microseven entry "
                  "for shared throttle behavior."),
        "throttle_type":            "rate_limit_per_ip_tcp",
        "throttle_type_confidence": "MED",
        "throttle_amount":          "~5s cooldown (inherits Hipcam family)",
        "throttle_amount_confidence":"MED",
        "throttle_notes": ("Hipcam RealServer firmware family — see "
                           "Hipcam/Microseven entry for full details."),
        "throttle_notes_confidence":"MED",
        "request_behaviors": "Hipcam family — Digest auth, URLs /11, /12.",
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "Instar",
        "aliases": ["instar"],
        "http_titles": ["instar"],
        "http_body":   ["instar", "instar gmbh", "instar.de"],
        "http_headers":["instar"],
        "nmap_products":["instar"],
        "onvif_scopes": ["instar"],
        "default_ports": [554, 80, 8080, 443],
        "notes": "Instar IP camera (Germany — popular in Europe)",
    },
    {
        "name": "Luma Surveillance",
        "aliases": ["luma surveillance", "luma", "snapav"],
        "http_titles": ["luma surveillance", "luma"],
        "http_body":   ["luma surveillance", "snapav", "luma.com"],
        "http_headers":["luma"],
        "nmap_products":["luma surveillance", "luma"],
        "onvif_scopes": ["luma"],
        "default_ports": [554, 80, 443],
        "notes": "Luma Surveillance IP camera or NVR (SnapAV) — Hikvision OEM",
    },
    {
        "name": "Speco Technologies",
        "aliases": ["speco", "speco technologies"],
        "http_titles": ["speco"],
        "http_body":   ["speco technologies", "speco", "specotech.com"],
        "http_headers":["speco"],
        "nmap_products":["speco"],
        "onvif_scopes": ["speco"],
        "default_ports": [554, 80, 443],
        "notes": "Speco Technologies IP camera or NVR",
    },
    {
        "name": "Oncam",
        "aliases": ["oncam", "oncam grandeye"],
        "http_titles": ["oncam", "grandeye"],
        "http_body":   ["oncam", "grandeye", "oncam.com"],
        "http_headers":["oncam"],
        "nmap_products":["oncam", "grandeye"],
        "onvif_scopes": ["oncam"],
        "default_ports": [554, 80, 443],
        "notes": "Oncam 360-degree fisheye IP camera",
    },
    {
        "name": "Illustra (Johnson Controls)",
        "aliases": ["illustra", "johnson controls", "tyco security"],
        "http_titles": ["illustra", "johnson controls"],
        "http_body":   ["illustra", "tyco security", "johnson controls"],
        "http_headers":["illustra", "tyco"],
        "nmap_products":["illustra", "johnson controls"],
        "onvif_scopes": ["illustra", "johnsoncontrols"],
        "default_ports": [554, 80, 443],
        "notes": "Illustra IP camera (Johnson Controls / Tyco Security)",
    },
    {
        "name": "Milesight",
        "aliases": ["milesight", "milesight iot"],
        "http_titles": ["milesight"],
        "http_body":   ["milesight", "milesight-iot.com"],
        "http_headers":["milesight"],
        "nmap_products":["milesight"],
        "onvif_scopes": ["milesight"],
        "default_ports": [554, 80, 8080, 443],
        "notes": "Milesight IP camera or NVR",
    },
    {
        "name": "Sunell",
        "aliases": ["sunell"],
        "http_titles": ["sunell"],
        "http_body":   ["sunell", "sunell technology", "sunell.com"],
        "http_headers":["sunell"],
        "nmap_products":["sunell"],
        "onvif_scopes": ["sunell"],
        "default_ports": [554, 80, 8000],
        "notes": "Sunell IP camera or NVR",
    },
    {
        "name": "TVT",
        "aliases": ["tvt", "tvt digital technology"],
        "http_titles": ["tvt", "tvt digital"],
        "http_body":   ["tvt digital", "tvt technology", "tvt-ip.com"],
        "http_headers":["tvt"],
        "nmap_products":["tvt"],
        "onvif_scopes": ["tvt"],
        "default_ports": [554, 80, 8000, 34567],
        "notes": "TVT Digital Technology IP camera or NVR (common OEM base)",
    },
    {
        "name": "Kedacom",
        "aliases": ["kedacom"],
        "http_titles": ["kedacom"],
        "http_body":   ["kedacom", "kedacom.com"],
        "http_headers":["kedacom"],
        "nmap_products":["kedacom"],
        "onvif_scopes": ["kedacom"],
        "default_ports": [554, 80, 443],
        "notes": "Kedacom IP camera or NVR",
    },
    {
        "name": "VideoIQ (Avigilon)",
        "aliases": ["videoiq"],
        "http_titles": ["videoiq"],
        "http_body":   ["videoiq", "videoiq.com"],
        "http_headers":["videoiq"],
        "nmap_products":["videoiq"],
        "onvif_scopes": ["videoiq"],
        "default_ports": [554, 80, 443],
        "notes": "VideoIQ analytics camera (absorbed by Avigilon/Motorola)",
    },
    {
        "name": "Samsung (standalone)",
        "aliases": ["samsung camera", "sno-", "snd-", "snh-"],
        "http_titles": ["samsung", "sno-", "snd-", "snh-"],
        "http_body":   ["samsung camera", "samsung techwin", "sno-", "snd-", "snh-"],
        "http_headers":["samsung"],
        "nmap_products":["samsung network camera"],
        "onvif_scopes": ["samsung"],
        "default_ports": [554, 80, 443],
        "notes": "Samsung standalone IP camera (pre-Hanwha rebranding)",
    },
    {
        "name": "Hipcam/Microseven",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "microseven", "rank": 39,
             "match": ["microseven", "m7d", "m7b", "m7t", "hipcam", "hiipcam", "hipcam realserver", "hiipcam/v100r003"],
             "rtsp":  ["/11",
                       "/12",
                       "/13",
                       "/h264major",
                       "/h264minor"],
             "mjpeg": "/auto.jpg",
             "snap":  "/tmpfs/snap.jpg",
             "port":  554},
        ],
        "aliases": ["microseven", "hipcam", "hiipcam", "m7d", "m7b", "m7t",
                    "srihome", "sricam", "vstarcam", "wansview"],
        "http_titles": ["microseven", "hipcam", "ipcam", "rtspserver"],
        "http_body":   ["microseven", "hipcam realserver", "rtspserver_0.0.0"],
        "http_headers":["hipcam realserver", "hipcam realserver/v1.0",
                        "hiipcam/v100r003", "vodserver/1.0.0",
                        "rtspserver_0.0.0.2", "rtspserver_0.0.0"],
        "nmap_products":["hipcam realserver", "hiipcam", "rtspserver_0.0.0"],
        "onvif_scopes": ["microseven", "hipcam"],
        "default_ports": [554, 80],
        "notes": "Hipcam RealServer firmware family — Microseven, Sricam, "
                 "Vstarcam, Wansview (W2/W3), Tenvis, many cheap Chinese "
                 "baby monitors / IPCAM rebrands. URL paths /11 (main) /12 "
                 "(sub) /13 /h264major /h264minor; HTTP snap at "
                 "/tmpfs/snap.jpg. Per-IP TCP rate-limit on RTSP port — "
                 "single-socket multi-method probing required.",
        # ─── rc2 throttle fields ───────────────────────────────────────
        "throttle_type":            "rate_limit_per_ip_tcp",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "~5s cooldown between TCP opens from same source IP",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("First TCP connection from a source IP always succeeds; "
                           "subsequent connections within ~5s are RST'd at the TCP layer. "
                           "Single-socket multi-method (OPTIONS+DESCRIBE+SETUP+TEARDOWN "
                           "on one socket) bypasses the throttle entirely. "
                           "CVE-2023-50685: malformed client_port in SETUP crashes the "
                           "RTSP service for ~45s. Tested on Microseven (4 browser "
                           "automation runs)."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("Digest auth required (realm=\"Hipcam RealServer\"); "
                              "rejects Basic auth. ONVIF returns no profiles for some "
                              "models — direct-RTSP probing required. Server header: "
                              "\"Hipcam RealServer/V1.0\" or "
                              "\"HiIpcam/V100R003 VodServer/1.0.0\"."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Honeywell",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "honeywell", "rank": 17,
             "match": ["honeywell", "equip-", "hc3", "hp4", "hd4", "hb4"],
             "rtsp":  ["/Streaming/Channels/101",
                       "/Streaming/Channels/102",
                       "/cam/realmonitor?channel=1&subtype=0"],
             "mjpeg": None,
             "snap":  "/ISAPI/Streaming/channels/101/picture",
             "port":  554},
        ],
        "aliases": ["honeywell", "hbt", "performance series", "hc30"],
        "http_titles": ["honeywell"],
        "http_body":   ["honeywell", "hbt"],
        "http_headers":["honeywell"],
        "nmap_products":["honeywell"],
        "onvif_scopes": ["honeywell"],
        "default_ports": [554, 80, 443],
        "notes": ("Honeywell IP camera — Performance Series HC30 line. "
                  "Mixed OEM (Hikvision or Dahua firmware depending on "
                  "year/line per SCW). Try Hikvision /Streaming/Channels/101 "
                  "and Dahua /cam/realmonitor first; fall back to ONVIF "
                  "discovery."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "LOW",
        "throttle_amount":          "Inherits upstream OEM",
        "throttle_amount_confidence":"LOW",
        "throttle_notes": ("OEM rebrand — behavior inherits from upstream "
                           "Hikvision or Dahua firmware (varies by year/line)."),
        "throttle_notes_confidence":"LOW",
        "request_behaviors": ("OEM-rebrand. Try Hikvision-style and "
                              "Dahua-style URL families before falling back "
                              "to ONVIF."),
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "Arecont Vision",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "arecont", "rank": 18,
             "match": ["arecont", "av2", "av5", "av10", "av20"],
             "rtsp":  ["/h264.sdp",
                       "/h264.sdp?res=full",
                       "/h264.sdp1",
                       "/h264.sdp2"],
             "mjpeg": "/mjpeg.cgi",
             "snap":  "/image.jpg",
             "port":  554},
        ],
        "aliases": ["arecont", "arecont vision", "contera", "megavideo"],
        "http_titles": ["arecont", "contera"],
        "http_body":   ["arecont vision", "megavideo"],
        "http_headers":["arecont"],
        "nmap_products":["arecont"],
        "onvif_scopes": ["arecont"],
        "default_ports": [554, 80, 443],
        "notes": ("Arecont Vision IP camera (multi-megapixel, "
                  "multi-imager surround). Single-sensor URL pattern: "
                  "/h264.sdp?res=[half/full]&ssn=N&doublescan=0&fps=N. "
                  "Multi-sensor: /h264.sdp<sensor#> where sensor# is 1..4. "
                  "ssn must be unique per stream."),
        "throttle_type":            "concurrent_stream_cap",
        "throttle_type_confidence": "LOW",
        "throttle_amount":          "Camera-dependent — not all units handle multiple full-res sessions",
        "throttle_amount_confidence":"LOW",
        "throttle_notes": ("Per OpenEye/AvertX docs: not all Arecont cameras "
                           "can handle multiple full-resolution sessions. "
                           "Reduce to half-res or single session per sensor "
                           "on overload."),
        "throttle_notes_confidence":"MED",
        "request_behaviors": ("HTTP Basic auth. Multi-imager pattern uses "
                              "/h264.sdp<N> per sensor. Streaming recipe is "
                              "deterministic — Layer 2 grinding wasteful."),
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "IQinVision",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "iqinvision", "rank": 21,
             "match": ["iqinvision", "iqm", "iqe"],
             "rtsp":  ["/rtsp/now.mp4"],
             "mjpeg": None,
             "snap":  None,
             "port":  554},
        ],
        "aliases": ["iqinvision", "iqeye"],
        "http_titles": ["iqinvision", "iqeye"],
        "http_body":   ["iqinvision", "iqeye"],
        "http_headers":["iqinvision"],
        "nmap_products":["iqinvision"],
        "onvif_scopes": ["iqinvision"],
        "default_ports": [554, 80],
        "notes": ("IQinVision IQeye IP camera (acquired by Vicon — older "
                  "line). ONVIF and PSIA compliant. Try generic /track1, "
                  "/track2 or ONVIF discovery."),
        "request_behaviors": ("Older PSIA-compliant line. ONVIF Profile S "
                              "on later firmware."),
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "OpenEye",
        "aliases": ["openeye", "ows", "apex", "owe"],
        "http_titles": ["openeye", "ows"],
        "http_body":   ["openeye", "openeye.net", "ows server"],
        "http_headers":["openeye"],
        "nmap_products":["openeye"],
        "onvif_scopes": ["openeye"],
        "default_ports": [554, 80, 443],
        "notes": ("OpenEye OWS Apex server / IP camera. Generic ONVIF + "
                  "RTSP /track1, /track2 paths."),
        "request_behaviors": ("OWS Apex server can ingest most third-party "
                              "cameras. Direct OpenEye cameras use ONVIF + "
                              "/track[N] paths."),
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "Verint",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "verint", "rank": 22,
             "match": ["verint"],
             "rtsp":  ["/live.sdp",
                       "/live2.sdp",
                       "/live3.sdp",
                       "/live4.sdp"],
             "mjpeg": None,
             "snap":  None,
             "port":  554},
        ],
        "aliases": ["verint", "nextiva", "s1700", "s1708"],
        "http_titles": ["verint", "nextiva"],
        "http_body":   ["verint", "nextiva", "video solutions"],
        "http_headers":["verint"],
        "nmap_products":["verint"],
        "onvif_scopes": ["verint"],
        "default_ports": [554, 80, 2543],
        "notes": ("Verint Nextiva enterprise camera/encoder. Older Nextiva "
                  "encoders (S1700/S1708) are RTP/UDP only on port 2543 "
                  "(no RTSP, telnet config). Newer Verint models are "
                  "ONVIF-compliant."),
        "request_behaviors": ("Older models lack RTSP entirely — RTP/UDP on "
                              "port 2543. Newer models ONVIF Profile S."),
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "Ubiquiti UniFi Protect",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "ubiquiti", "rank": 23,
             "match": ["ubiquiti", "unifi", "uvc-"],
             "rtsp":  ["/{camera_id}"],
             "mjpeg": None,
             "snap":  None,
             "port":  7447},
        ],
        "aliases": ["ubiquiti", "unifi", "unifi protect", "ubnt", "g3", "g4", "g5"],
        "http_titles": ["unifi", "unifi protect"],
        "http_body":   ["ubiquiti", "unifi protect", "ui.com"],
        "http_headers":["ubnt", "ubiquiti"],
        "nmap_products":["ubiquiti", "unifi"],
        "onvif_scopes": [],
        "default_ports": [7447, 7441, 443, 80],
        "notes": ("Ubiquiti UniFi Protect cameras (G3/G4/G5/AI Port). RTSP "
                  "must be enabled per-camera in UniFi Protect controller "
                  "Settings -> Advanced. Camera ID is alphanumeric slug "
                  "from UniFi UI. Non-standard ports: 7447 (RTSP) / "
                  "7441 (RTSPS). Cameras do NOT speak ONVIF directly — "
                  "all access goes through the Protect controller."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "MED",
        "throttle_amount":          "Configurable in Protect controller",
        "throttle_amount_confidence":"MED",
        "throttle_notes": ("Bandwidth/session caps live in the Protect "
                           "controller, not the camera."),
        "throttle_notes_confidence":"MED",
        "request_behaviors": ("Streams via UniFi Protect controller — not "
                              "direct from camera. ONVIF discovery will "
                              "fail; use vendor's API or copy the camera_id "
                              "from Protect UI. Skip ONVIF + skip Layer 2."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Hiseeu",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "hiseeu", "rank": 36,
             "match": ["hiseeu"],
             "rtsp":  ["/Streaming/Channels/101",
                       "/Streaming/Channels/102",
                       "/cam/realmonitor?channel=1&subtype=0"],
             "mjpeg": None,
             "snap":  "/ISAPI/Streaming/channels/101/picture",
             "port":  554},
        ],
        "aliases": ["hiseeu", "eseecloud", "esee"],
        "http_titles": ["hiseeu"],
        "http_body":   ["hiseeu", "eseecloud"],
        "http_headers":["hiseeu"],
        "nmap_products":["hiseeu"],
        "onvif_scopes": ["hiseeu"],
        "default_ports": [554, 80, 8554, 34567],
        "notes": ("Hiseeu wireless IP camera or NVR/gateway system. "
                  "Gateway-based: gateway exposes RTSP at port 80 with "
                  "/ch[N]_[feed].264 paths. Standalone cameras use "
                  "/onvif1 or generic /Streaming/Channels paths. "
                  "Inconsistent across models."),
        "request_behaviors": ("Wireless gateway-based system. URL pattern "
                              "varies — try /onvif1 and ONVIF discovery."),
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "Grandstream",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "grandstream", "rank": 31,
             "match": ["grandstream", "gxv3"],
             "rtsp":  ["/0",
                       "/1"],
             "mjpeg": None,
             "snap":  "/snapshot/view0.jpg",
             "port":  554},
        ],
        "aliases": ["grandstream", "gxv", "gsc"],
        "http_titles": ["grandstream"],
        "http_body":   ["grandstream networks", "grandstream.com"],
        "http_headers":["grandstream"],
        "nmap_products":["grandstream"],
        "onvif_scopes": ["grandstream"],
        "default_ports": [554, 80, 443, 8000],
        "notes": ("Grandstream GXV/GSC IP camera. Standard ONVIF Profile "
                  "S/T. Multiple stream profiles configurable."),
        "request_behaviors": "Standard ONVIF. Multiple configurable stream profiles.",
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "TRENDnet",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "trendnet", "rank": 28,
             "match": ["trendnet", "tv-ip"],
             "rtsp":  ["/channel1",
                       "/channel2"],
             "mjpeg": "/cgi/mjpg/mjpeg.cgi",
             "snap":  "/cgi-bin/video.jpg",
             "port":  554},
        ],
        "aliases": ["trendnet", "tv-ip"],
        "http_titles": ["trendnet", "tv-ip"],
        "http_body":   ["trendnet", "trendnet.com"],
        "http_headers":["trendnet"],
        "nmap_products":["trendnet"],
        "onvif_scopes": ["trendnet"],
        "default_ports": [554, 80],
        "notes": ("TRENDnet TV-IP series IP camera. URL pattern varies by "
                  "model generation. Newer: /Streaming/Channels/101 "
                  "(Hikvision-style). Older: /h264 or /play1.sdp. "
                  "Some use /live/0/SUB."),
        "request_behaviors": ("URL pattern varies by model generation — "
                              "try Hik-style, then /play1.sdp, then "
                              "/h264, then ONVIF discovery."),
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "D-Link",
        # 3.5.0 (D2): stream paths, formerly STREAM_DB
        "streams": [
            {"slug": "dlink", "rank": 29,
             "match": ["d-link", "dlink", "dcs-"],
             "rtsp":  ["/play1.sdp",
                       "/play2.sdp"],
             "mjpeg": "/video.cgi",
             "snap":  "/image.jpg",
             "port":  554},
        ],
        "aliases": ["d-link", "dlink", "dcs-"],
        "http_titles": ["d-link", "dlink", "dcs-"],
        "http_body":   ["d-link", "dlink.com"],
        "http_headers":["d-link"],
        "nmap_products":["d-link", "dlink"],
        "onvif_scopes": ["dlink"],
        "default_ports": [554, 80],
        "notes": ("D-Link DCS-series IP camera. URL pattern varies by "
                  "model. Older DCS-9xx: /play1.sdp. DCS-8526LH: "
                  "/live/profile.0 (main), /live/profile.1 (sub). "
                  "Some use /onvif/profile.0 or /live/ch00_0."),
        "request_behaviors": ("URL pattern varies by model. Try "
                              "/play1.sdp, /live/profile.0, "
                              "/onvif/profile.0, then ONVIF discovery."),
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "SCW (Security Camera Warehouse)",
        "aliases": ["scw", "security camera warehouse", "getscw"],
        "http_titles": ["scw", "security camera warehouse"],
        "http_body":   ["getscw.com", "security camera warehouse"],
        "http_headers":["scw"],
        "nmap_products":["scw"],
        "onvif_scopes": ["scw"],
        "default_ports": [554, 80],
        "notes": ("SCW Hikvision-compatible firmware on most lines. Use "
                  "/Streaming/Channels/101 pattern. SCW-branded NVRs "
                  "follow Hikvision NVR conventions."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "LOW",
        "throttle_amount":          "Inherits Hikvision firmware",
        "throttle_amount_confidence":"LOW",
        "throttle_notes": "Hikvision OEM — inherits parent throttle behavior.",
        "throttle_notes_confidence":"MED",
        "request_behaviors": ("Hikvision OEM. Use /Streaming/Channels/N0X "
                              "pattern. ISAPI snapshot URL works."),
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "Blink",
        "aliases": ["blink", "amazon blink", "blink mini", "blink outdoor"],
        "http_titles": ["blink"],
        "http_body":   ["blink", "amazon blink"],
        "http_headers":[],
        "nmap_products":["blink"],
        "onvif_scopes": [],
        "default_ports": [],
        "notes": ("Blink (Amazon) cloud-only camera. No local RTSP, no "
                  "local HTTP UI for streaming. Footage routes through "
                  "Amazon servers. Local discovery should mark these "
                  "as 'Not a Camera' since no controllable stream exists."),
        "throttle_type":            "no_rtsp_support",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "N/A — cloud only",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Cloud-only camera — no local RTSP path exists. "
                           "Sync Module 2 offers local storage but no RTSP."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("Cloud-only — no local stream access. Skip "
                              "ONVIF, skip Layer 2, skip all probes."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Lorex / Dahua DVR-NVR Family",
        "aliases": ["lorex dvr", "lorex nvr", "dahua dvr", "dahua nvr",
                    "amcrest dvr", "amcrest nvr",
                    "d861", "d862", "d863", "d871", "d841", "d881",
                    "n841", "n844", "n846", "n861", "n864", "n881", "n882",
                    "n910", "n920"],
        "http_titles": ["web service"],
        "http_body":   [],
        "http_headers":[],
        "nmap_products":[],
        "onvif_scopes": [],
        "default_ports": [554, 80, 35000, 37777, 443, 8000],
        "rtsp_realm_regex": r"^Login to [0-9a-f]{32}$",
        "rtsp_realm_regex_confidence": "HIGH",
        # 2.4.0-rc2.0: streaming_recipe added (data only; consumed in rc3.x).
        # Channel iteration for Dahua-format DVR/NVRs. populated_channel_test
        # uses SDP video-track presence to detect connected channels.
        "streaming_recipe": {
            "type": "channel_iterate",
            "path_template": "/cam/realmonitor?channel={ch}&subtype={st}",
            "channels": list(range(1, 33)),
            "channel_base": 1,
            "subtypes": [0, 1],
            "subtype_main": 0,
            "subtype_sub": 1,
            "fallback_paths": ["/h264/ch{ch}/main/av_stream", "/live/ch{ch}/main"],
            "populated_channel_test": "sdp_has_video_track",
            # 2.5.0-rc1.2: per-channel HTTP snapshot URL template. The
            # Dahua snapshot.cgi endpoint accepts ?channel=N to return
            # that specific physical channel's still image; without the
            # query param the DVR returns either the default channel
            # or a generic placeholder (~3.5 KB). Used by the channel
            # enumeration helper to build per-card snap URLs so each
            # channel's card thumbnail polls its own snapshot.
            "snap_url_template": "http://{ip}/cgi-bin/snapshot.cgi?channel={ch}",
        },
        "streaming_recipe_confidence": "HIGH",
        # 2.4.0-rc2.2 — skip_layer2: True. Multi-channel DVR/NVRs do
        # not benefit from Layer 2 fallback (multi-socket fanout) on a
        # single-IP target — the right path requires channel iteration
        # via streaming_recipe (consumed in rc3.x), not more retries on
        # a single-channel guess. Without this flag rc2.1's Layer 2
        # consumer falls through and grinds 50s on the Lorex DVR
        # before bail-after-10 fires; with the flag set, Layer 2 is
        # skipped immediately with a one-line log entry. Companion to
        # the rc2.1 code-side short-circuit at find_rtsp_path.
        "skip_layer2": True,
        "skip_layer2_confidence": "HIGH",
        "notes": ("Multi-channel DVR/NVR family — Lorex (post-Dahua-"
                  "acquisition), Dahua direct, Amcrest (rebrand). "
                  "Series: D861/862/863/871/841/881, N841/844/846/861/"
                  "864/881/882/910/920. Web admin shows page title 'WEB "
                  "SERVICE' before init. Default Dahua/Amcrest TCP/UDP "
                  "ports 37777/37778 are user-configurable (one observed "
                  "live unit had them on 35000/35001). RTSP Server: "
                  "header omitted. ONVIF disabled by default. URL pattern "
                  "/cam/realmonitor?channel={ch}&subtype={st} requires "
                  "channel iteration to find populated channels."),
        "throttle_type":            "auth_attempt_lockout",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "10 failed auth attempts then ~30 min lockout (or until power cycle)",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Lockout only counts FAILED auth (bad Digest "
                           "response). Successful auth followed by 200/404 "
                           "on subsequent paths does NOT increment the "
                           "counter. Channel iteration with valid creds is "
                           "unthrottled."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("Digest auth only (no Basic). Realm pattern "
                              "'Login to <32-hex>'. Server header omitted "
                              "from RTSP responses. ONVIF disabled by "
                              "default — lives under Network -> Connection "
                              "or Network -> Advanced depending on firmware "
                              "minor build."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Generic IP Camera",
        "aliases": ["webcam", "ipcam", "network camera"],
        "http_titles": ["ip camera", "network camera", "webcam", "ipcam",
                        "video server", "live view", "camera login"],
        "http_body":   ["ip camera", "network camera", "video surveillance",
                        "live view", "ptz control"],
        "http_headers":[],
        "nmap_products":["ip camera", "network camera", "video server", "webcam"],
        "onvif_scopes": [],
        "default_ports": [554, 80, 8080],
        "notes": "Generic IP camera (manufacturer unidentified)",
    },
    # ============================================================================
    # 2.4.0-rc2.0: NVR/DVR family entries with streaming_recipe (channel iteration)
    # ============================================================================
    # These are SEPARATE entries from the IP camera entries above because (per
    # design) channel iteration only applies to multi-channel boxes. Single
    # IP cameras of the same brand keep their static-path entries.
    # streaming_recipe field consumed in 2.4.0-rc3.x (Lorex/Dahua DVR family
    # support build); populated here in rc2.0 as the data foundation.
    {
        "name": "Hikvision NVR",
        "aliases": ["hikvision nvr", "hikvision dvr", "hilook nvr", "hilook dvr",
                    "ds-7604", "ds-7608", "ds-7616", "ds-7708", "ds-7716",
                    "ds-7732", "ds-9632", "ds-77 hghi", "ds-77 huhi",
                    "ds-77 hqhi", "turbo hd"],
        "http_titles": ["hikvision", "hikvision-webs"],
        "http_body":   [],
        "http_headers":["app-webs/"],
        "nmap_products":[],
        # 2.4.0-rc2.2 — onvif_scopes emptied. Previously contained
        # "onvif://www.onvif.org/Profile/Streaming" which is the
        # standard ONVIF Profile S spec identifier returned by EVERY
        # Profile-S-compliant ONVIF device on the planet (IP cameras,
        # NVRs, encoders, all of them). Per ONVIF Core Spec it carries
        # zero brand-specific signal — it identifies the protocol, not
        # the manufacturer. Including it here was scoring +1 to
        # Hikvision NVR for any ONVIF haystack and contributed to
        # CrystalHeeler's Hikvision DS-2DE4A425IW-DE single PTZ camera
        # being misclassified as a Hikvision NVR.
        "onvif_scopes": [],
        "default_ports": [554, 80, 8000, 443],
        "notes": ("Hikvision multi-channel NVR/DVR. DS-7xxx series NVRs and "
                  "DS-77xxx HUHI/HQHI/HGHI Turbo HD DVRs. HiLook is a "
                  "Hikvision sub-brand using identical RTSP format. Some "
                  "firmwares require disabling stream encryption "
                  "(Configuration > Network > Advanced Settings > Stream "
                  "Encryption > OFF) — must be done from direct-connected "
                  "monitor."),
        "streaming_recipe": {
            "type": "channel_iterate",
            "path_template": "/Streaming/Channels/{ch}{st:02d}",
            "channels": list(range(1, 33)),
            "channel_base": 1,
            "subtypes": [1, 2],
            "subtype_main": 1,
            "subtype_sub": 2,
            "populated_channel_test": "sdp_has_video_track",
        },
        "streaming_recipe_confidence": "HIGH",
    },
    {
        "name": "Hanwha NVR / Wisenet NVR",
        "aliases": ["hanwha nvr", "wisenet nvr", "samsung nvr",
                    "qrn", "prn", "xrn", "hrx", "hrd", "srn"],
        "http_titles": ["wisenet", "samsung"],
        "http_body":   [],
        "http_headers":[],
        "nmap_products":[],
        "onvif_scopes": [],
        "default_ports": [558, 554, 80, 8080, 443],
        "notes": ("Hanwha (formerly Samsung Techwin) Wisenet NVRs. IMPORTANT: "
                  "Channels are 0-based (Channel 1 in UI = 0 in RTSP URL). "
                  "RTSP port is the LAST device port in the configured range, "
                  "default 558 (NOT 554). Profile selection via /stw-cgi/ "
                  "media.cgi CGI command. HRX series with newer firmware uses "
                  "NVR-style URLs."),
        "streaming_recipe": {
            "type": "channel_iterate",
            "path_template": "/LiveChannel/{ch}/media.smp/profile={st}",
            "channels": list(range(0, 32)),
            "channel_base": 0,
            "subtypes": [1, 2],
            "subtype_main": 1,
            "subtype_sub": 2,
            "default_port": 558,
            "populated_channel_test": "sdp_has_video_track",
        },
        "streaming_recipe_confidence": "HIGH",
    },
    {
        "name": "Uniview NVR (UNV)",
        "aliases": ["uniview nvr", "unv nvr", "nvr301", "nvr3", "nvr5",
                    "nvr8", "ezview nvr"],
        "http_titles": ["uniview", "unv"],
        "http_body":   [],
        "http_headers":[],
        "nmap_products":[],
        "onvif_scopes": [],
        "default_ports": [554, 80, 9090],
        "notes": ("Uniview (UNV) NVR301/3/5/8 series. Channel iteration: c1, "
                  "c2, c3, ... (1-based). Stream s0=main, s1=sub. Some firmwares "
                  "have s0/s1 swapped — fall back accordingly."),
        "streaming_recipe": {
            "type": "channel_iterate",
            "path_template": "/unicast/c{ch}/s{st}/live",
            "channels": list(range(1, 33)),
            "channel_base": 1,
            "subtypes": [0, 1],
            "subtype_main": 0,
            "subtype_sub": 1,
            "populated_channel_test": "sdp_has_video_track",
        },
        "streaming_recipe_confidence": "HIGH",
    },
    {
        "name": "Reolink NVR / Home Hub",
        "aliases": ["reolink nvr", "rln8", "rln16", "rln36", "home hub",
                    "reolink home hub"],
        "http_titles": ["reolink"],
        "http_body":   [],
        "http_headers":[],
        "nmap_products":[],
        "onvif_scopes": [],
        "default_ports": [554, 80, 8080, 9000],
        "notes": ("Reolink RLN8-410, RLN16-410, RLN36 NVRs and Home Hub. "
                  "Channel ch is zero-padded 2-digit (01, 02, ...). 1-based "
                  "in RTSP URLs but 0-based in CGI/snapshot URLs (snap_channel_"
                  "offset=-1). Use api.cgi?cmd=GetChannelstatus to query "
                  "populated channels. Same RTSP format as Reolink IP cameras "
                  "(single camera = ch=01 only)."),
        "streaming_recipe": {
            "type": "channel_iterate",
            "path_template": "/Preview_{ch:02d}_{st}",
            "channels": list(range(1, 17)),
            "channel_base": 1,
            "subtypes": ["main", "sub"],
            "subtype_main": "main",
            "subtype_sub": "sub",
            "snap_channel_offset": -1,
            "populated_channel_test": "sdp_has_video_track",
        },
        "streaming_recipe_confidence": "HIGH",
    },
    {
        "name": "Vivotek NVR (Linux-based)",
        "aliases": ["vivotek nvr", "nd8321", "nd8322", "nd9322", "nd9442"],
        "http_titles": ["vivotek"],
        "http_body":   [],
        "http_headers":[],
        "nmap_products":[],
        "onvif_scopes": [],
        "default_ports": [554, 80, 8080],
        "notes": ("Vivotek Linux-based NVR (ND8x21, ND8322P, ND9x42P, ND9x44P "
                  "series). Camera ID format C_<N> where N is channel number. "
                  "Different format from Vivotek IP cameras themselves "
                  "(which use /live.sdp or /media2/stream.sdp?profile=)."),
        "streaming_recipe": {
            "type": "channel_iterate",
            "path_template": "/Media/Live/Normal?camera=C_{ch}&streamindex={st}",
            "channels": list(range(1, 33)),
            "channel_base": 1,
            "subtypes": [1, 2],
            "subtype_main": 1,
            "subtype_sub": 2,
            "populated_channel_test": "sdp_has_video_track",
        },
        "streaming_recipe_confidence": "HIGH",
    },
    {
        "name": "Amcrest NVR",
        "aliases": ["amcrest nvr", "amcrest dvr", "nv4108", "nv4216", "nv5216",
                    "nv4108-hs"],
        "http_titles": ["amcrest"],
        "http_body":   [],
        "http_headers":[],
        "nmap_products":[],
        "onvif_scopes": [],
        "default_ports": [554, 37777, 80],
        "notes": ("Amcrest NVR (NV4108E, NV4216E, NV5216E series). Dahua-OEM. "
                  "Same RTSP recipe as Dahua. Some older Amcrest NVRs also "
                  "support Reolink-style h264Preview format as a fallback."),
        "streaming_recipe": {
            "type": "channel_iterate",
            "path_template": "/cam/realmonitor?channel={ch}&subtype={st}",
            "channels": list(range(1, 33)),
            "channel_base": 1,
            "subtypes": [0, 1],
            "subtype_main": 0,
            "subtype_sub": 1,
            "fallback_paths": ["/h264Preview_{ch:02d}_main"],
            "populated_channel_test": "sdp_has_video_track",
        },
        "streaming_recipe_confidence": "HIGH",
    },
    {
        "name": "Swann NVR / DVR",
        "aliases": ["swann nvr", "swann dvr", "nvr-7090", "nvr-8580",
                    "nvr8000", "dvr-1590", "dvr-4575", "dvr-4980", "swnvk"],
        "http_titles": ["swann"],
        "http_body":   [],
        "http_headers":[],
        "nmap_products":[],
        "onvif_scopes": [],
        "default_ports": [554, 80, 1085],
        "notes": ("Swann NVR/DVR mostly Hikvision-OEM (newer NVR-7090, "
                  "NVR-8580). Older models may be Raysharp-OEM with "
                  "/ch{NN}/{stream} format. Try Hikvision format first, "
                  "fall back to Raysharp."),
        "streaming_recipe": {
            "type": "channel_iterate",
            "path_template": "/Streaming/Channels/{ch}{st:02d}",
            "channels": list(range(1, 17)),
            "channel_base": 1,
            "subtypes": [1, 2],
            "subtype_main": 1,
            "subtype_sub": 2,
            "fallback_paths": ["/ch{ch:02d}/0", "/ch{ch:02d}/1"],
            "populated_channel_test": "sdp_has_video_track",
        },
        "streaming_recipe_confidence": "MED",
    },
    {
        "name": "ANNKE NVR / DVR",
        "aliases": ["annke nvr", "annke dvr", "h800", "h500", "n48pbb",
                    "n46pbb", "dn81r", "dn82r"],
        "http_titles": ["annke"],
        "http_body":   [],
        "http_headers":[],
        "nmap_products":[],
        "onvif_scopes": [],
        "default_ports": [554, 80, 8000],
        "notes": ("ANNKE NVR/DVR (H800, H500, N48PBB, N46PBB, DN81R, DN82R "
                  "series). Hikvision-OEM. Same recipe as Hikvision NVR. "
                  "Older ANNKE may use Dahua-OEM (cam/realmonitor) — fall "
                  "back if Hikvision format fails entirely."),
        "streaming_recipe": {
            "type": "channel_iterate",
            "path_template": "/Streaming/Channels/{ch}{st:02d}",
            "channels": list(range(1, 17)),
            "channel_base": 1,
            "subtypes": [1, 2],
            "subtype_main": 1,
            "subtype_sub": 2,
            "fallback_paths": ["/H264/ch{ch}/main/av_stream",
                               "/cam/realmonitor?channel={ch}&subtype=0"],
            "populated_channel_test": "sdp_has_video_track",
        },
        "streaming_recipe_confidence": "HIGH",
    },
]


# ── 3.5.0 (D2): STREAM_DB, built from CAMERA_DB ─────────────────────────────
# Before 3.5.0 the stream paths were a second table, keyed by slug, kept by
# hand beside CAMERA_DB. Now each brand's paths are on its entry, and
# STREAM_DB is built here, in rank order, so every lookup is unchanged
# (docs/Camera_DB_Merge_Plan.md). Used for:
#   1. Post-login silent probe of alternate stream paths
#   2. Pre-login heuristic probing when ONVIF returns no profiles
def _build_stream_db(entries: list[dict]) -> dict:
    found = sorted(((s["rank"], s) for e in entries for s in e.get("streams", [])),
                   key=lambda rs: rs[0])
    return {s["slug"]: {"match": s["match"], "rtsp": s["rtsp"], "mjpeg": s["mjpeg"],
                        "snap": s["snap"], "port": s["port"]} for _rank, s in found}


STREAM_DB: dict = _build_stream_db(CAMERA_DB)
