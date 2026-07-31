# RTSP OPTIONS Fingerprint Helper — Plan

**Working name:** RTSP OPTIONS Fingerprint Helper (a.k.a. "build nc functionality into our code")
**Status:** Investigated, not yet implemented
**Recommended timing:** After the in-progress main-code refactor lands
**Target version:** TBD (user's call — fits as an rc on the current minor, or as part of the next minor)
**Relationship to Plan 1 (Lorex/Dahua DVR Family Support):** complementary but independent. Plan 1 can use this helper for cleaner brand-id, but doesn't strictly require it; Plan 1 could open its own one-shot OPTIONS socket. Plan 2 stands on its own as core infrastructure that benefits every camera family — Hipcam, Hikvision, Axis, and any future entry.

---

## Background

During the Lorex D861A8B-Z troubleshooting, the diagnostic that nailed the device family in seconds was a one-line nc command:

```
$ printf 'OPTIONS rtsp://192.168.50.217:554/cam/realmonitor?channel=1&subtype=0 RTSP/1.0\r\nCSeq: 1\r\n\r\n' \
  | nc -w 3 192.168.50.217 554
RTSP/1.0 401 Unauthorized
CSeq: 1
WWW-Authenticate: Digest realm="Login to 0123456789abcdef0123456789abcdef", nonce="fedcba9876543210fedcba9876543210"
```

That single response delivered five identification signals: RTSP server confirmed, URL pattern valid, Digest auth scheme, realm format `Login to <32-hex>` (Dahua family fingerprint), and (when present on other devices) Server header. Currently AnyCam captures the Server header sometimes (rc2.1.1's walker upgrade) but throws away the realm, the nonce-shape, the Public methods list, and the auth scheme itself — all of which are independent fingerprints.

This plan extracts that nc-style probe into a reusable helper that runs ONCE per host during ONVIF post-scan, and feeds the captured fields into brand-id and downstream cred-auth.

## Why this is worth doing independently of Plan 1

Multiple camera families benefit from realm/auth-scheme/methods capture:

| Family | Fingerprint we'd gain |
|---|---|
| Lorex/Dahua DVR-NVR | realm regex `^Login to [0-9a-f]{32}$` |
| Hipcam family | realm exactly `Hipcam RealServer` (already partly captured via Server: header, but realm is a second redundant signal) |
| HiLook ColorVu | realm `IP Camera(AX598)` |
| go2rtc / mediamtx servers | realm typically `IPCam`, plus distinctive Public methods list |
| Hanwha | shared-Session-ID requirement is detectable via Public methods |
| Axis | Public methods includes `OPTIONS, DESCRIBE, ANNOUNCE, SETUP, PLAY, RECORD, PAUSE, TEARDOWN, GET_PARAMETER, SET_PARAMETER, REDIRECT` — distinctive ordering |
| Reolink | Server: `Rtsp Server/2.0` — clean signal |

Many of those overlap with HTTP-layer fingerprints we already capture (page title, Server). But the RTSP-layer fingerprint is independent — it works even when HTTP is on a non-standard port, when HTTP responds 200 with a generic page (the Lorex `WEB SERVICE` case), or when HTTP is firewalled off entirely.

A second benefit: capturing the `Public:` methods list tells us which RTSP methods the server claims to support before we try them. Some servers reject GET_PARAMETER (used as keepalive); some require ANNOUNCE for two-way audio; some advertise SETUP without supporting interleaved transport. We can use the methods list to skip features the server doesn't claim, instead of trying-then-failing.

## Proposed design

### 1. New helper: `_rtsp_options_fingerprint`

```python
async def _rtsp_options_fingerprint(
    host: str,
    port: int = 554,
    *,
    timeout: float = 3.0,
    path: str = "/",
) -> dict:
    """
    Open one TCP socket to host:port, send a single RTSP OPTIONS request,
    read the response, and return a dict of captured fingerprint fields.

    Returns dict with keys (all str/None except where noted):
        status:           int       e.g. 200, 401, 404
        looks_like_rtsp:  bool      True if any line started with "RTSP/"
        server_header:    str|None  value of Server: header
        auth_scheme:      str|None  "Digest" | "Basic" | None
        auth_realm:       str|None  realm from WWW-Authenticate
        auth_algorithm:   str|None  algorithm from WWW-Authenticate (MD5, SHA-256, etc)
        public_methods:   list[str] parsed from Public: header, e.g.
                                    ["OPTIONS", "DESCRIBE", "SETUP", ...]
        cseq:             str|None  echoed CSeq
        raw_response:     str       full response text (truncated to 4 KB)
        elapsed_ms:       float     wall-clock time of the round-trip
        error:            str|None  populated only on socket-level failure

    Failure semantics:
        - Connection refused / timeout / RST: returns dict with error set,
          looks_like_rtsp=False, all other fields None or empty.
        - Server speaks something else (HTTP, FTP, ...): looks_like_rtsp=False,
          status=None, raw_response captures whatever was received.
        - Server speaks RTSP but returns 401/404/4xx: looks_like_rtsp=True,
          status set, auth_* fields populated if challenge present.
        - Server speaks RTSP and returns 200: looks_like_rtsp=True, status=200,
          public_methods populated.

    Implementation notes:
        - Single TCP open + close. No retries. ~3s timeout.
        - Sends generic OPTIONS request with CSeq: 1 and a random User-Agent.
        - User-Agent should NOT identify as AnyCam (avoid creating a fingerprint
          servers could detect). Use something innocuous like
          "User-Agent: LibVLC/3.0.18" — common and uncontroversial.
        - Reads up to 4 KB or until "\r\n\r\n" or socket close, whichever first.
        - Parses headers via simple line-split + ":" partition. No full RFC 2326
          parser — we just need fingerprint fields, not strict compliance.
    """
```

Estimated implementation: ~80 lines including parser.

### 2. Wiring into existing flow

Two call sites, both in the discovery + cred-auth path:

**Site A: ONVIF post-scan, after probe_http_identity runs.**

`probe_http_identity` currently runs at line ~XXX of camera_discovery.py for ONVIF-discovered cameras (added in rc2.2). Right after it, before `find_rtsp_path`, call `_rtsp_options_fingerprint` on the same IP at port 554 (or the camera's discovered RTSP port). Populate `onvif_meta`:

```python
rtsp_fp = await _rtsp_options_fingerprint(ip, 554, timeout=3.0)
if rtsp_fp.get("server_header"):
    onvif_meta["server_header"] = rtsp_fp["server_header"]
if rtsp_fp.get("auth_realm"):
    onvif_meta["auth_realm"] = rtsp_fp["auth_realm"]
    onvif_meta["auth_scheme"] = rtsp_fp["auth_scheme"]
if rtsp_fp.get("public_methods"):
    onvif_meta["rtsp_public_methods"] = ",".join(rtsp_fp["public_methods"])
```

**Site B: Inside `_probe_host_port` for non-ONVIF discoveries.**

For cameras found via nmap-only or mDNS (no ONVIF), the fingerprint helper runs inline in `_probe_host_port` after the port is confirmed open, before path-walking. Same field-population logic.

### 3. Brand-id pipeline upgrade

`_identify_camera_brand` currently checks `http_headers`, `aliases`, `onvif_scopes`, and `page_titles`. Add two new fields it can match:

- `rtsp_realm_regex` — regex tested against `auth_realm`
- `rtsp_realm_substring` — substring tested against `auth_realm` (for simpler entries)

Both fields are optional per CAMERA_DB entry. Brand-id loops through all entries, scoring by how many fields match, and picks the highest-scoring match.

Confidence: a match on `rtsp_realm_regex` is HIGH-confidence because realm strings are server-baked and not user-customizable. A match on `server_header` is also HIGH-confidence. Combined matches (page_title + realm) should boost confidence further.

### 4. Captured fields on the camera record

Add these new fields to the camera dict (and the safe-camera redaction list):

| Field | Type | Source | Display in UI? |
|---|---|---|---|
| `rtsp_server_header` | str | OPTIONS response Server: | Identity panel, when populated |
| `rtsp_auth_realm` | str | OPTIONS response WWW-Authenticate realm= | Identity panel, when populated |
| `rtsp_auth_scheme` | str | OPTIONS response WWW-Authenticate scheme | hidden / debug only |
| `rtsp_public_methods` | str (CSV) | OPTIONS response Public: | hidden / debug only |

Existing `server_header` field becomes specifically the HTTP server header (already its semantic). The new `rtsp_server_header` is the RTSP-layer one. They can differ — Hipcam family has HTTP `Server: Hipcam` and RTSP `Server: Hipcam RealServer/V1.0`; Dahua DVRs have `Server: nginx` (or absent) on HTTP and no Server: at all on RTSP.

### 5. Cost / risk profile

**Cost**: one extra TCP open per host during ONVIF post-scan. ~3s timeout means worst-case ~3s added to scan per dead host, ~0.05s for a responsive host. For the typical 15-host scan, expected added time is < 1s for a healthy network.

**Risk to throttled hosts**: zero. The Hipcam family rate-limit allows ONE TCP open per ~5s window. The fingerprint probe IS the first TCP open — there's no preceding probe to collide with. After the fingerprint completes, the rest of the discovery flow already throttles correctly (rc2 work).

**Risk to lockout-throttled hosts (Lorex/Dahua DVR family)**: zero. The fingerprint probe sends OPTIONS, not a DESCRIBE-with-credentials. OPTIONS doesn't authenticate; it just gets the 401 challenge. No counter increment.

### 6. Edge cases

- **Server closes socket immediately (Hipcam if previous probe < 5s ago):** caught by the timeout/error path, returns dict with error set. Caller treats as "no fingerprint available" and proceeds with whatever HTTP-layer info we have.
- **Server sends multi-line headers (RFC 822 continuation):** rare in RTSP. Parser should handle by collapsing continuation lines onto the previous header.
- **Server sends Public: header on a 401 response (some Axis firmware does this):** parser captures both fields without confusion. The Public: list is independent of the auth challenge.
- **Server sends Server: header on a 4xx response (Reolink does this):** same — parser captures Server even on 401/404.
- **Server returns garbage / non-RTSP / connection reset:** `looks_like_rtsp` stays False, raw_response captures whatever bytes arrived. Higher layers can use this to short-circuit Layer 2 (rc2.1's fast-bail).
- **Server requires SSL on port 554 (rare):** SSL handshake fails on plain socket; helper returns error="ssl_required" or similar. Higher layer can retry on RTSPS port if any is configured.

### 7. Testing

Unit tests with mock socket:
1. 200 OK with full Public list → all fields populated
2. 401 with Digest realm → auth_* fields populated
3. 401 with Basic realm → auth_scheme="Basic"
4. 200 with no Server: → server_header=None, other fields fine
5. Connection refused → error set, looks_like_rtsp=False
6. Timeout → error set, looks_like_rtsp=False
7. HTTP 400 response (server is HTTP-only on this port) → looks_like_rtsp=False
8. Multi-line continuation header → folded correctly
9. Truncated response (closed socket mid-headers) → parses what it got
10. Realm value contains escaped quotes `realm="foo\"bar"` → unescaped correctly

Integration tests against real devices:
- Lorex D861A8B-Z (the Lorex DVR): expect 401 + realm `Login to 0123456789abcdef0123456789abcdef` + Digest scheme
- Hikvision: expect 401 + realm `IP Camera(NN)` (NN = device hash) + Digest
- Hipcam: expect 401 + realm `Hipcam RealServer` + Digest, plus Server: `Hipcam RealServer/V1.0`

## Files to modify

| File | Change | Est LoC |
|---|---|---|
| `camera_discovery.py` | New `_rtsp_options_fingerprint` helper | ~80 |
| `camera_discovery.py` | Wire into ONVIF post-scan flow | ~15 |
| `camera_discovery.py` | Wire into `_probe_host_port` for non-ONVIF cases | ~15 |
| `camera_discovery.py` | Extend `_identify_camera_brand` to check rtsp_realm fields | ~20 |
| `camera_discovery.py` | Add new fields to camera record + safe-camera redaction | ~10 |
| `camera_discovery.py` | UI: show realm / Server in Identity panel when populated | ~10 |
| `verify_release.py` | Add semantic contract for `_rtsp_options_fingerprint` | ~5 |

Total: ~155 lines additive. Pure infrastructure — no behavioral changes to existing camera handling unless brand-id matches a new field on an existing entry.

## Tests / acceptance

1. Cold scan against a network with the Microseven (Hipcam), the Hikvision (Hikvision), the Lorex DVR (Lorex DVR): all three are correctly fingerprinted, and `rtsp_auth_realm` field is populated for each
2. Identity panel displays `RTSP Auth Realm: <value>` for each of the three when populated
3. Scan time delta vs current rc2.6: < 1s added on a healthy network, < 3s added per dead host
4. Mock-socket unit tests pass for all 10 cases listed above
5. CAMERA_DB entries can match on `rtsp_realm_regex` and brand-id correctly identifies a Lorex DVR by realm pattern alone (with HTTP probe disabled in the test)

## Open questions before coding

1. **Run during initial nmap scan, or only during ONVIF post-scan?** Doing it during nmap means more parallel probes (nmap finds 10 hosts → fingerprint helper runs 10 times in parallel). Doing it during ONVIF post-scan is sequential and simpler. Initial recommendation: **ONVIF post-scan only** — keeps the fast path fast, and the cred-auth path benefits from the fingerprint anyway.

2. **Cache fingerprints across scans?** Once we know IP X is a Lorex DVR with realm Y, do we re-fingerprint on the next scan? Probably yes (firmware updates can change realms), but with a 24h TTL. Out of scope for first iteration — re-fingerprint every scan.

3. **DESCRIBE instead of OPTIONS?** DESCRIBE returns SDP body which is valuable for IP-camera identification (codec, framerate, resolution). But DESCRIBE on Lorex/Dahua DVR returns 200 even for empty channels (Plan 1 finding) — confusing. OPTIONS is simpler and unambiguous. Stay with OPTIONS for the fingerprint, do DESCRIBE separately during the streaming-recipe walk.

4. **One probe per host, or one per port?** Some devices have RTSP on multiple ports (Foscam port 88, Hanwha port 49152, etc.). For now: one per host on port 554, with optional re-probe on alternate ports if 554 is closed. Future iteration could probe all known-RTSP-ports for the matched brand.

## Source signal that triggered this work

User's nc command during Lorex D861A8B-Z troubleshooting:
```
$ printf 'OPTIONS rtsp://192.168.50.217:554/... RTSP/1.0\r\nCSeq: 1\r\n\r\n' | nc -w 3 192.168.50.217 554
```
Returned more identification info in 80 bytes than our entire 92-second cred-auth flow had captured. User asked "Could we just build the nc functionality into our code to do some of the probing?" — answer is yes, and this plan documents how.

---

*Filed during AnyCam 2.2.9rc1 troubleshooting session, 2026-05-02.*
*Sibling plan: Lorex_Dahua_DVR_Family_Support_Plan.md (Plan 1 of 2).*
*Defer reason: user is mid-refactor on main code; merge after refactor lands.*
