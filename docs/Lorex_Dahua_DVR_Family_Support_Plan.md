# Lorex / Dahua DVR Family Support — Plan

**Working name:** Lorex/Dahua DVR Family Support
**Status:** Investigated, not yet implemented
**Recommended timing:** After the in-progress main-code refactor lands
**Target version:** TBD (user's call — fits as an rc on the current minor, or as part of the next minor)
**Depends on:** Plan 2 (RTSP OPTIONS Fingerprint Helper) is recommended-but-not-strictly-required

---

## Background

Live-tested device: **Lorex D861A8B-Z** (8-channel 4K analog DVR), at 192.168.50.217.
- Device Model in UI: D861A8
- Web Version: V3.2.7.83177
- Firmware Version: 00030
- Build Date: 2022-06-24
- Hardware Version: V1.0
- Serial: ND012006139117

Lorex was acquired by Dahua (after FLIR). Every Lorex DVR/NVR runs Dahua firmware with Lorex skinning. The eBay listings for D871A explicitly show "Brand: Lorex, Dahua." So this plan is really "Dahua DVR/NVR family support, with Lorex as the most common rebrand."

The covered families include at minimum:
- Lorex D861, D862, D863, D871A, D871B, D841A, D881 (DVRs)
- Lorex N841, N844, N846, N861, N881, N882, N910, N920, N864 (NVRs)
- Dahua DVR/NVR direct-branded
- Amcrest DVR/NVR (Dahua OEM rebrand)
- Likely also: Q-See, Swann (some models), LaView (Dahua-derived)

## What the live test showed

### The user's nc OPTIONS probe at 192.168.50.217:554

```
$ printf 'OPTIONS rtsp://192.168.50.217:554/cam/realmonitor?channel=1&subtype=0 RTSP/1.0\r\nCSeq: 1\r\n\r\n' \
  | nc -w 3 192.168.50.217 554
RTSP/1.0 401 Unauthorized
CSeq: 1
WWW-Authenticate: Digest realm="Login to 0123456789abcdef0123456789abcdef", nonce="fedcba9876543210fedcba9876543210"
```

Five fingerprintable signals from a single TCP exchange:
1. Server speaks RTSP on 554 (status line is RTSP/1.0)
2. Dahua URL pattern `/cam/realmonitor?channel=N&subtype=M` is the valid path
3. Auth scheme is **Digest** (not Basic)
4. Realm format is `Login to <32-hex-string>` — strong Dahua/Lorex fingerprint
5. Server returned 401 with no `Server:` header (Dahua DVRs intentionally omit it from RTSP responses, unlike Hipcam family which echoes "Hipcam RealServer/V1.0")

### The user's authenticated channel iteration

```bash
for ch in 1..8: curl -v --digest -u admin:PASS \
  "rtsp://192.168.50.217:554/cam/realmonitor?channel=$ch&subtype=0" --max-time 4
```

Result: **all 8 channels returned `RTSP/1.0 200 OK`** even though there are only ~5 physical cameras connected. **The DVR allocates virtual stream slots for every configured channel regardless of whether a camera is plugged in.** A 200 on DESCRIBE does not mean "live feed available" on this device family.

To distinguish populated vs empty channels we'd need one of:
- Parse the SDP body for `m=video` lines with valid codec params
- Run SETUP and check that we get a session ID with non-zero ssrc
- Run SETUP+PLAY and check whether RTP packets actually arrive within ~2s

Easiest: SDP body inspection. A populated channel returns SDP with `a=rtpmap:96 H264/90000` (or H265) and a `cliprect`/`framerate` line. An empty channel returns either no SDP or SDP without a meaningful video track. Confirm experimentally before committing.

### Lockout behavior (corrected from earlier draft)

User confirmed: **10 failed login attempts then lockout** (not 5 as initially documented). Lockout duration is ~30 minutes or until power-cycle. **Successful auth then valid 200/404/etc on subsequent paths does NOT increment the counter** — only auth-failure (401 with bad Digest response) does. So channel iteration with valid creds is unconstrained by this throttle.

### The DVR's Network → Port screen showed

- Max Connection: 20 (range 0-128)
- TCP Port: **35000** (Dahua-proprietary; default 37777 — user has customized)
- UDP Port: **35001** (paired with TCP; default 37778)
- HTTP Port: 80
- HTTPS Port: 443 (disabled)
- RTSP Port: 554
- RTSP Format string displayed in UI: `rtsp://<Username>:<Password>@<IP Address>:<Port>/cam/realmonitor?channel=1&subtype=0`
  - "channel: Camera, 1-8; subtype: Type, Main Stream 0, Sub Stream 1"

### The DVR's Account screen showed

Single user `admin` (group: admin, password strength: Strong). **No separate ONVIF user.** This particular firmware (V3.2.7.83177 / 2022-06-24) has unified the ONVIF account with the system admin — earlier Dahua firmware had a separate ONVIF user at Setting → System → Account → Onvif User, but the user's screenshot shows that submenu is gone. ONVIF auth on this device should accept the admin credentials directly.

### Why ONVIF returned 0 profiles in the rc2.1 log

Most likely cause: ONVIF service itself is disabled at the DVR level. Dahua firmware ships with "ONVIF function default is closed" (DahuaWiki), and on this firmware vintage the toggle lives under Network → Connection (or Network → Advanced → ONVIF, depending on minor build). The user has not enabled it. **For this plan we assume ONVIF stays disabled** — we're going to bypass ONVIF entirely and use direct RTSP path-walking with channel iteration.

## What's wrong in current code (rc2.6)

Walking through the rc2.6 log for the cred-auth attempt at 01:04:30:

```
01:04:30  ONVIF profiles found: 0          ← <1s, ONVIF disabled at device
01:04:30  Trying direct RTSP on the Lorex DVR:554
01:04:31  Layer 1 walk (1s, 25 paths, no match)   ← walks our generic 25 paths
01:04:31  Layer 2 starts                          ← grinds same paths × 5s × 10
01:05:16  Layer 2 bails after 10 failures         ← ~45s wasted
01:05:16  Trying direct RTSP on the Lorex DVR:80           ← stored ONVIF port
01:05:16  Layer 1 walk (no match)
01:05:17  Layer 2 starts
01:06:02  Layer 2 bails                           ← another ~45s wasted
01:06:02  RTSP result: None
```

Total: 92s for "Verifying..." → "Could not connect with those credentials."

Three failure modes compounded:

1. **No Lorex/Dahua DVR entry in CAMERA_DB.** Our brand-id pipeline saw HTTP page title `WEB SERVICE` (from probe_http_identity at 01:02:08) and didn't match anything. Result: no brand-aware shortcuts fired, no streaming recipe was applied, no throttle awareness.

2. **The 25-path generic walk doesn't include `/cam/realmonitor?channel=N&subtype=M` for N=1..8.** It almost certainly includes channel=1 (Dahua's most common pattern) but for an 8-channel DVR with cameras on channels 3, 5, 6, 7, channel=1 returning 200 OK with empty SDP looks like "no stream" to our walker. Even if it parsed the 200 as success, the resulting URL would point to an empty channel.

3. **Layer 2 wastes 90s.** Both ports (the Lorex DVR:554 and the Lorex DVR:80) trigger Layer 2 because Layer 1's `looks_like_rtsp` flag is True (the DVR DOES respond with RTSP/1.0 401). Layer 2 then grinds the same path list with 5s spacing, finds nothing new, and bails after 10 attempts × ~4.5s each = ~45s × 2 ports = ~90s.

## Proposed design

### 1. New CAMERA_DB entry

```python
{
    "name": "Lorex/Dahua DVR-NVR",
    "aliases": [
        "lorex dvr", "lorex nvr",
        "dahua dvr", "dahua nvr",
        "amcrest dvr", "amcrest nvr",
        # Specific model series we know about:
        "d861", "d862", "d863", "d871", "d841", "d881",
        "n841", "n844", "n846", "n861", "n864", "n881", "n882", "n910", "n920",
    ],
    "http_headers": [
        # The HTTP server doesn't always populate Server:; identify primarily by page title
    ],
    "page_titles": ["web service"],   # case-insensitive substring match
    "rtsp_realm_regex": r"^Login to [0-9a-f]{32}$",   # NEW field — strong Dahua signal
    "rtsp_realm_regex_confidence": "HIGH",
    "onvif_endpoints": ["/onvif/device_service"],
    "default_ports": [554, 80, 35000, 37777, 443, 8000],
    "default_ports_confidence": "HIGH",
    "throttle_type": "auth_attempt_lockout",
    "throttle_type_confidence": "HIGH",
    "throttle_amount": "10 failed auth attempts then ~30 min lockout (or until power cycle)",
    "throttle_amount_confidence": "HIGH",  # user-confirmed on D861A8 V3.2.7.83177
    "throttle_notes": "Lockout only counts FAILED auth (bad Digest response). Successful "
                      "auth followed by 200/404/etc on subsequent paths does NOT increment "
                      "the counter. Channel iteration with valid creds is unthrottled.",
    "throttle_notes_confidence": "HIGH",
    "request_behaviors": "Digest auth only (no Basic). Realm pattern 'Login to <32-hex>'. "
                         "Server header omitted from RTSP responses. ONVIF disabled by default; "
                         "lives under Network -> Connection or Network -> Advanced depending on "
                         "firmware minor build.",
    "request_behaviors_confidence": "HIGH",
    "skip_layer2": True,   # NEW field — Layer 2 grinding adds no value when paths are deterministic
    "skip_layer2_confidence": "HIGH",
    "skip_onvif": False,   # try ONVIF first (some users will have it enabled), fall through fast
    "streaming_recipe": {
        "type": "channel_iterate",
        "path_template": "/cam/realmonitor?channel={ch}&subtype={st}",
        "channels": list(range(1, 17)),   # 1..16 covers 4ch/8ch/16ch DVRs and most NVRs
        "subtypes": [0, 1],               # 0=mainstream, 1=substream
        "fallback_paths": [
            "/h264/ch1/main/av_stream",   # legacy Dahua firmware
            "/live/ch1/main",              # very old firmware
        ],
        "populated_channel_test": "sdp_has_video_track",  # see "SDP body inspection" below
    },
    "notes": "Lorex (post-Dahua-acquisition), Dahua direct, Amcrest (rebrand). Web admin "
             "at port 80 shows page title 'WEB SERVICE' before init. Default TCP/UDP "
             "ports 37777/37778 are user-configurable (the live D861A8 has them on "
             "35000/35001). RTSP URL pattern is universal across the family.",
}
```

### 2. Skip ONVIF on this family — or, equivalently, fail-fast on it

When `manufacturer` matches Lorex/Dahua DVR-NVR family during cred-auth:
- Attempt ONVIF once with a short timeout (~3s). If it returns 0 profiles, log it as expected and move on. Don't retry.
- If ONVIF returned profiles, use them. If it didn't, fall through to channel iteration directly.

This handles both the "user has ONVIF enabled" and "user has ONVIF disabled" cases without burning 30+ seconds on the latter.

### 3. Skip Layer 2 entirely on this family

When `skip_layer2: True` is set on the matched CAMERA_DB entry, `find_rtsp_path` returns immediately after Layer 1 with whatever result Layer 1 produced. Layer 2 is path-blind grinding; it cannot find a path that's not in our list, and our list for this family is deterministic. Saves 45s per port on failure.

### 4. Channel iteration in Layer 1

When Layer 1 sees `streaming_recipe.type == "channel_iterate"`, it expands the recipe into a path list:
- 16 channels × 2 subtypes = 32 channel-specific paths
- Plus 2 fallback paths
- Total 34 paths

All walked through the SAME single socket (Layer 1 is single-socket-multi-method). The Hipcam-style throttle short-circuit is irrelevant here because Lorex/Dahua doesn't TCP-rate-limit — but the single-socket walk is universally safe and that's what we use.

For each path Layer 1 sends:
1. OPTIONS (or DESCRIBE — TBD which is faster; DESCRIBE returns SDP body which we need anyway)
2. Reads response
3. If 401 with WWW-Authenticate, captures the realm + nonce, sends Digest-authenticated DESCRIBE
4. If 200 OK with SDP, captures the SDP body and inspects for video tracks
5. If SDP has a valid `m=video` track with a populated codec param, marks this channel as populated
6. Records all populated channels for the cred-auth result

### 5. SDP body inspection — distinguishing populated vs empty channels

A populated channel returns SDP like:
```
v=0
m=video 0 RTP/AVP 96
a=rtpmap:96 H264/90000
a=fmtp:96 packetization-mode=1;profile-level-id=...
a=control:trackID=0
```

An empty channel returns either:
- No SDP body (Content-Length: 0)
- SDP with no `m=video` line
- SDP with `m=video` but `a=rtpmap` references a no-op codec like `MPEG4-GENERIC` only

Heuristic: **populated_channel** if SDP contains `m=video` AND at least one `a=rtpmap:.+ (H264|H265|HEVC|MPEG4)`. Confirm against a 200-OK-but-empty channel before finalizing the heuristic. The user's curl loop only grepped status lines, not SDP — we don't have empirical data on what an empty channel's SDP looks like on this exact firmware.

**Action item before coding:** ask user to grab full DESCRIBE bodies for one populated and one empty channel:

```bash
curl -v --digest -u 'admin:PASS' -X DESCRIBE \
  -H 'Accept: application/sdp' \
  "rtsp://192.168.50.217:554/cam/realmonitor?channel=2&subtype=0" \
  --max-time 4
```

Run for a known-populated channel and a known-empty one. The diff tells us which SDP feature reliably distinguishes them.

### 6. Cred-auth flow on this family

```
1. Brand-id has matched Lorex/Dahua DVR-NVR via page_title or rtsp_realm_regex
2. Try ONVIF once (3s timeout), use profiles if any returned
3. If ONVIF returned 0 profiles:
   a. Walk channel iteration (single socket, 32 paths + 2 fallbacks)
   b. For each 200 OK response, parse SDP and check for populated video track
   c. Build streams[] from all populated channels found
4. Skip Layer 2 entirely (skip_layer2: True)
5. Total wall-clock: ~3s ONVIF attempt + ~5s channel walk = ~8s cred-auth
   (vs 92s in rc2.6)
```

### 7. Throttle integration

The Throttle-Aware Probe Pacing plan filed earlier handles `rate_limit_per_ip_tcp` (Hipcam family). The Lorex/Dahua family has a different throttle type — `auth_attempt_lockout` — which has different mechanics:

- Per-IP counter increments only on FAILED auth (bad Digest response)
- Resets after successful auth from same IP, or after ~30 min, or on power-cycle
- Limit is 10 failed attempts (per user)

In-code policy: when running cred-auth on this family with credentials we haven't yet confirmed, **stop after 1 failure**. If the first DESCRIBE returns 401 + bad Digest is rejected with another 401 from the server, the credentials are wrong; we've used 1 of 10 attempts; surface the failure to the user and don't retry channels. Once we have ONE successful auth, all subsequent same-session requests use the same nonce/cnonce and are not rate-limited.

This is the OPPOSITE of the Hipcam family, where every TCP open hits the rate limit regardless of auth outcome. So we want a different code path: not "delay between probes" but "stop on first auth failure, retry only the rest of the URL space."

## Files to modify

| File | Change | Est LoC |
|---|---|---|
| `camera_discovery.py` | New CAMERA_DB entry for Lorex/Dahua DVR-NVR | ~50 |
| `camera_discovery.py` | New brand-id signal: RTSP realm regex matching (depends on Plan 2's fingerprint helper, OR can be done with a separate one-shot OPTIONS call) | ~25 |
| `camera_discovery.py` | New `streaming_recipe` field handler in Layer 1 walker (channel iteration) | ~40 |
| `camera_discovery.py` | New `skip_layer2` field check in `find_rtsp_path` | ~5 |
| `camera_discovery.py` | New `skip_onvif`/`onvif_fail_fast` short timeout in cred-auth flow | ~15 |
| `camera_discovery.py` | SDP populated-channel heuristic (after empirical check on user device) | ~20 |
| `camera_discovery.py` | New throttle policy: `auth_attempt_lockout` — stop on first auth failure | ~15 |

Total: ~170 lines additive. No CAMERA_DB schema breaking changes (all new fields are optional with safe defaults).

## Tests / acceptance

1. Lorex D861A8B-Z at 192.168.50.217: cred-auth completes in <10s, returns N populated channels (N = number of cameras physically connected, ≥5)
2. Lorex D861A8B-Z: card title displays "Lorex/Dahua DVR-NVR"
3. Lorex D861A8B-Z: Identity panel shows page_title="WEB SERVICE", rtsp_realm="Login to 0123456789abcdef0123456789abcdef"
4. With wrong password: cred-auth fails after 1 attempt, shows "Authentication failed (1 of 10 attempts before lockout — please verify password)"
5. Hikvision: behavior unchanged (no brand-id collision)
6. Hipcam: behavior unchanged (different throttle type, different code path)
7. Layer 2 not invoked on the Lorex DVR (verify via log: no "Layer 2 starts" line)

## Open questions before coding

1. **DESCRIBE vs OPTIONS for channel walk.** DESCRIBE returns SDP we need; OPTIONS is lighter. Probably use OPTIONS first (returns Public: methods + auth challenge) then one authenticated DESCRIBE per channel. Confirm bandwidth/latency wash isn't an issue.
2. **Empty-channel SDP shape.** Need user data: full DESCRIBE body for one populated, one empty channel. Decides our populated-channel heuristic.
3. **Range of channels.** Plan currently goes 1..16. NVRs can have 64+ channels; do we cap at 16 or extend to 32/64? Probably 16 by default with a per-entry override field for NVR models. The 8-channel DVR users won't notice the extra 8 attempts (all return fast 404 or empty-200).
4. **TCP/UDP port discovery.** D861A8 has TCP on 35000, default is 37777. Should we probe both, or detect via the HTTP admin (which exposes the port config)? Probing is simpler. Adding 35000 to default_ports covers the user's device.
5. **Family overlap.** Some Dahua IP cameras (not DVRs/NVRs) also use `/cam/realmonitor?channel=1&subtype=0`. Should the CAMERA_DB entry cover them, or do we keep DVR-NVR separate from Dahua-IPC? Probably separate entries with shared fingerprints — IPC has channel=1 only (single-camera devices), DVR-NVR has channel=1..16.

## Source signal that triggered this work

User reported D861A8B-Z stuck at "Verifying..." for 90s before "Could not connect." Log showed ONVIF returning 0 profiles, then Layer 1 walking 25 generic paths (none matching the Dahua channel/subtype URL), then Layer 2 grinding 90s on two ports before bailing. nc OPTIONS probe by user confirmed the device is RTSP+Digest with the canonical Dahua URL pattern.

---

*Filed during AnyCam 2.2.9rc1 troubleshooting session, 2026-05-02.*
*Triggered by: Lorex D861A8B-Z (Lorex SMART 4K Ultra HD DVR) at 192.168.50.217 failing cred-auth.*
*Defer reason: user is mid-refactor on main code; merge after refactor lands.*
