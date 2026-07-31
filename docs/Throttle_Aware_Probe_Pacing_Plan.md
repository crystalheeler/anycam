# Throttle-Aware Probe Pacing — Investigation & Plan

**Working name:** Throttle-Aware Probe Pacing (a.k.a. "TCP Hammer Cool-down Protection")
**Status:** Investigated, not yet implemented
**Recommended timing:** After 2.2.9 ships
**Target version:** TBD (probably 2.3.0 or 2.2.10)

---

## Background

During rc2.x testing, the user's Microseven camera (the Microseven) entered an extended firmware-level RTSP lockout that required a power-cycle to recover. Investigation showed AnyCam was opening **30+ TCP sockets** to that camera in 30 minutes, against a documented `rate_limit_per_ip_tcp` throttle of "5+ seconds between TCP opens". Our own CAMERA_DB has the throttle data with HIGH confidence — we just weren't using it during cred-auth or runtime restart.

This is a real bug class affecting the entire **Hipcam RealServer firmware family** (Microseven, Sricam, Vstarcam, Wansview-old, Tenvis, plus generic OEM-rebadged budget cameras worldwide). All five brands share the same firmware and same throttle. Power-cycle was required, suggesting an escalated firmware-level lockout beyond the documented 5-second cooldown.

---

## Investigation findings

### 1. CAMERA_DB throttle data (already collected, not consulted)

5 brands have `throttle_type: "rate_limit_per_ip_tcp"` with throttle_amount strings:

| Brand | throttle_amount | confidence |
|---|---|---|
| Hipcam/Microseven | "~5s cooldown between TCP opens from same source IP" | HIGH |
| Sricam / Srihome | "~5s cooldown (inherits Hipcam family)" | MED |
| Vstarcam | "~5s cooldown (inherits Hipcam family)" | MED |
| Wansview | "W2/W3: ~5s cooldown (Hipcam family); newer: no RTSP" | MED |
| Tenvis | "~5s cooldown (inherits Hipcam family)" | MED |

All five share a `~Ns cooldown` pattern. A regex like `r'~?\s*(\d+)\s*s'` extracts the seconds reliably across all five entries.

### 2. Where throttle-awareness already exists (✓ no change needed)

`find_rtsp_path` (discovery & cred-relogin) is already throttle-aware:
- Layer 2 multi-socket fallback skipped entirely for `rate_limit_per_ip_tcp` brands (line 3286)
- 5-second sleep between Layer 2 sockets when it does run (line 3309, hard-coded but correct)
- `no_rtsp_support` brands skipped before any TCP open (line 3206)
- `session_time_cap` brands get extended timeout (line 3214)

`_probe_rtsp_paths_single_socket` (Layer 1) walks all paths through ONE TCP socket — bypasses the throttle entirely by design. This is the universal-safe probe.

`_fix_codec` (rc2.6) uses 5s spacing between ffprobe retries. ✓

### 3. Where the leaks are (need fixing)

**Leak A: ONVIF profile probe loop in `api_set_credentials`** (line 6189)
For each profile returned by ONVIF, fires `probe_rtsp(stream_url, ...)` — opens a new TCP socket per profile, no pacing between iterations. the Microseven has 2 profiles → 2 back-to-back TCPs in <1 second.

**Leak B: `_probe_db_streams`** (line 6112)
Iterates DB-listed RTSP paths, calling `probe_rtsp` per path — fresh TCP each time, no pacing. Triggered after the ONVIF profile loop. Observed in rc2.6 log: 3 db_probe RSTs at 19:30:33 (2 within the same second).

**Leak C: Inter-stage gap during cred-auth**
Even if Leaks A and B are individually paced, the NEXT stage of cred-auth (`_fix_codec`) starts 1 second after the previous stage ends. There's no global "this IP is in cooldown" tracker.

**Leak D: snap_loop ffmpeg restart cadence** (line 5099)
After EOF, backoff is `2^(streak-1)` capped at 32s — sequence is 1s, 2s, 4s, 8s, 16s, 32s. The first few entries are inside the 5s Hipcam window. Each ffmpeg launch is a fresh TCP open.

**Leak E: `_fix_codec` first attempt timing**
First attempt is `await asyncio.sleep(1.0)` (line 6383). For a Hipcam camera that just finished its probe sequence, that's well inside the 5s window. The 5s spacing between RETRIES is correct, but the spacing before the FIRST retry should also be 5s for throttled cameras.

**Leak F (minor): `handle_stream_test` (Test Stream button)**
Single TCP open via ffprobe. Single click is fine, but rapid clicking could trigger throttle. User-initiated; low priority.

### 4. Brand-id timing — when is `manufacturer` available?

At `api_set_credentials` time, the camera record was already populated by discovery, which calls `_identify_camera_brand`. So `camera["manufacturer"]` is reliably set before any cred-auth probe fires. The throttle lookup is just `_identify_camera_brand(camera)` returning the matched CAMERA_DB entry with throttle_type/throttle_amount.

Edge case: the camera record might have empty `manufacturer` if discovery's brand-id failed. In that case we apply no throttle (current behavior — safe default).

### 5. Throttle data parsing

```python
def _parse_throttle_seconds(amount_str: str) -> float:
    """Extract seconds from CAMERA_DB throttle_amount string. Returns 0.0
    if no parseable value (callers treat 0 as 'no pacing required').
    All current rate_limit_per_ip_tcp entries match the '~Ns' pattern."""
    if not amount_str:
        return 0.0
    m = re.search(r'~?\s*(\d+)\s*s', amount_str)
    return float(m.group(1)) if m else 0.0
```

Tested against all 5 entries — extracts `5.0` from each. Pattern is robust.

---

## Proposed design

### Approach: hybrid in-sequence pacing + cross-sequence runtime tracker

Two complementary mechanisms:

**1. In-sequence pacing.** Inside any function that opens multiple TCP sockets in a loop, insert `await asyncio.sleep(throttle_s)` between iterations when `throttle_type == "rate_limit_per_ip_tcp"`. Affects: ONVIF profile probe loop, `_probe_db_streams`.

**2. Cross-sequence runtime tracker.** Module-level dict `_THROTTLE_TRACK: dict[str, float]` mapping `ip → last_tcp_open_timestamp`. Helper `_throttle_wait_if_needed(ip, throttle_s)` checks the tracker and sleeps the remainder of the cooldown window before returning. Called at the entry of any function that's about to open a TCP socket on a throttled camera. Updates the timestamp before returning.

### Concrete fixes per leak

| Leak | Fix |
|---|---|
| A — ONVIF profile loop | Sleep `throttle_s` between iterations when throttled |
| B — `_probe_db_streams` | Same. Plus: optionally skip entirely for `rate_limit_per_ip_tcp` brands (db_probes are a "nice to have" — ONVIF already gave us the profiles) |
| C — Inter-stage gap | `_throttle_wait_if_needed(ip)` at entry of each cred-auth stage |
| D — snap_loop backoff | Floor backoff at `throttle_s` for throttled cameras: `backoff = max(throttle_s, 2 ** min(streak-1, 4))` |
| E — `_fix_codec` first attempt | First sleep becomes `max(1.0, throttle_s)` for throttled cameras |
| F — Test Stream | Call `_throttle_wait_if_needed(ip)` before ffprobe; UI shows "Waiting for camera throttle..." if needed |

### Recommendation: ALSO skip db_probes for `rate_limit_per_ip_tcp`

Cred-auth path-by-path:
- **Without skip:** 2 ONVIF probes + 3 db_probes + 3 fix_codec attempts = 8 TCP opens × 5s = ~40s cred-auth
- **With db_probe skip:** 2 ONVIF probes + 3 fix_codec attempts = 5 TCP opens × 5s = ~25s cred-auth
- **Currently (rc2.6, broken):** 2 + 3 + 3 = 8 TCP opens in ~3 seconds, all fail, camera lockout

The db_probes are a "find extra streams not visible to ONVIF" feature. ONVIF already returned 2 profiles for the Microseven. The db_probe paths (/13, /h264major, /h264minor) wouldn't have added anything useful here. For Hipcam family cameras specifically, db_probe is unlikely to find anything ONVIF didn't, so the skip is low-cost.

### User-visible timing impact

For a 5-brand-affected camera (Hipcam family), cred-auth time goes from ~3 seconds (currently, all probes failing) to ~25 seconds (probes succeeding, paced). The trade-off is "longer cred-auth that actually works" vs "fast cred-auth that locks the camera out."

For all other cameras (~95% of the market — Hikvision, Dahua, Axis, Hanwha, etc.), nothing changes. The throttle gating is brand-specific.

### Edge cases addressed

- Camera with no `manufacturer` set → no throttle applied → current behavior (safe default)
- `MED`-confidence throttle entries → still applied (the throttle exists; confidence rates how well we documented the *exact* timing)
- User clicks Test Stream during in-progress cred-auth → second call's `_throttle_wait_if_needed` sleeps until the cooldown clears
- HEVC+ fallback probes (line 8759) → call sites updated to use the throttle helper
- Multiple cameras at the same IP (rare; HA-host with multiple addons) → tracker keys by IP, both share cooldown

---

## Implementation plan

### Code changes (estimated)

| File | Change | LoC |
|---|---|---|
| `camera_discovery.py` | Add `_THROTTLE_TRACK` dict and `_throttle_wait_if_needed` helper | ~30 |
| `camera_discovery.py` | Add `_parse_throttle_seconds` parser | ~10 |
| `camera_discovery.py` | Pace ONVIF profile probe loop | ~15 |
| `camera_discovery.py` | Pace `_probe_db_streams` + early-skip option | ~15 |
| `camera_discovery.py` | Floor `backoff` at throttle_s in snap_loop | ~5 |
| `camera_discovery.py` | First-attempt delay in `_fix_codec` | ~5 |
| `camera_discovery.py` | Pre-check in `handle_stream_test` | ~5 |
| `camera_discovery.py` | UI: optional "Waiting for camera (throttled)" status | ~10 |

Total: ~95 lines of additive code. No CAMERA_DB or schema changes.

### Tests / acceptance

1. Microseven cred-auth completes successfully (no probe RSTs) within ~25 seconds
2. Hikvision cred-auth completes in ~3 seconds (no change — not throttled)
3. the Microseven snap_loop ffmpeg restart cadence stays ≥5s between attempts
4. After 5+ minutes of normal use, `nc` test against the Microseven still returns `RTSP/1.0 200 OK` (camera not locked out)
5. Test Stream on the Microseven succeeds without triggering lockout

### Risk

Low. All code paths are additive — they delay things, never bypass safety checks. Worst case for non-throttled cameras: `_throttle_wait_if_needed` is called and immediately returns (no sleep). Worst case for throttled cameras: cred-auth takes longer but works correctly instead of failing.

### Open questions for future implementation

1. **UI feedback during pacing.** Should cred-auth show a progress indicator like "Authenticating (throttled camera, please wait ~25 seconds)..."? Probably yes — silent 25s is bad UX.
2. **db_probe skip default.** Should the skip be unconditional for `rate_limit_per_ip_tcp` brands, or configurable? Recommendation: unconditional skip for those brands (the time/risk trade-off is decisive).
3. **Aggressive cooldown detection.** If we observe back-to-back RST'd probes despite pacing, escalate the cooldown to 30s for that IP for the rest of the session? This handles the "extended firmware-level lockout" case we observed. Maybe overkill for a first iteration.

---

## What didn't make it into this plan (out of scope)

- **Other throttle types** (`concurrent_user_cap`, `restart_cooldown`, etc.) — those aren't TCP-rate-limit issues, they have different mechanics. A separate piece of work.
- **Per-camera "is the camera reachable right now?" health check** — would be useful but is a much larger refactor.
- **Camera-specific firmware quirks** beyond what's in CAMERA_DB.

---

*Filed by Claude during AnyCam rc2.6 troubleshooting session, 2026-04-30.*
*Triggered by: Microseven entering extended RTSP lockout requiring power cycle.*
*Source signal: ~30 TCP opens in ~30 minutes against a documented 5s/open throttle.*
