"""Probers: RTSP, MJPEG, HLS, RTMP, WebRTC, the RTSP fingerprint, the HTTP identity check, ONVIF calls.

Moved out of camera_discovery.py in 3.0.0-rc1.3 (build plan E1); the function
bodies are unchanged.

This file cannot import camera_discovery.py (see anycam_host.py). The names in
NEEDS are set on this module at start-up; H reads a camera_discovery.py
value at the moment of use.
"""
import base64
import datetime
import hashlib
import logging
import os
import re
import socket
import ssl
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, quote

from anycam_host import H
from anycam_brand import (
    CAMERA_KEYWORDS, _DB_MANUFACTURERS, identify_manufacturer, lookup_oui,
    oui_is_camera,
)

log = logging.getLogger("anycam")

# Taken from camera_discovery.py at start-up (anycam_host.bind).
NEEDS = (
    'CURRENT_VERSION', 'RTSP_PATHS', '_identify_camera_brand', '_match_stream_db',
    '_record_rst_observation', '_strip_creds',
)
# Codec match for the "sdp_has_video_track" populated-channel test.
# Pattern intentionally matches both H264 and H.264 etc. by treating
# the dot as an optional character via the regex below.
_SDP_VIDEO_CODEC_RE = re.compile(
    r"a=rtpmap:\d+\s+(H\.?264|H\.?265|HEVC|MPEG[- ]?4)",
    re.IGNORECASE,
)


def _sdp_has_video_track(sdp_body: str) -> bool:
    """2.5.0-rc1.0: stricter populated-channel heuristic for DVR/NVR
    devices that allocate virtual stream slots regardless of whether a
    physical camera is connected to that channel.

    Field-tested case (Lorex D861A8B-Z): an 8-channel DVR with 5
    cameras connected returned RTSP/1.0 200 OK on ALL 8 channel-
    iterated DESCRIBE requests. Without a populated-channel filter, we
    would surface the first 200-OK channel as the working stream — and
    that channel might be one of the empty 3.

    Returns True when SDP contains an `m=video` line AND at least one
    `a=rtpmap` mapping to a real video codec (H.264 / H.265 / HEVC /
    MPEG4). Returns False when SDP is empty, lacks `m=video`, or has
    `m=video` but no recognised codec rtpmap.

    The plan flagged this heuristic as "needs empirical confirmation
    against a known-empty channel before finalising"; user opted to
    ship blind on the rationale that we will always be guessing for
    untested hardware. If field test on the Lorex D861A8B-Z surfaces
    phantom or missing cards, the fix lands as a 2.5.0-rc1.1 bumpfix
    refining either this regex or the `m=video` substring check."""
    if not sdp_body or "m=video" not in sdp_body.lower():
        return False
    return bool(_SDP_VIDEO_CODEC_RE.search(sdp_body))


def _expand_channel_iterate_paths(recipe: dict,
                                   channel_cap: int = 16) -> list[str]:
    """2.5.0-rc1.0: expand a `streaming_recipe` of type
    `channel_iterate` into an ordered RTSP path list.

    Per-channel ordering is `(channel, subtype)` lexicographic:
    channel 1 main, channel 1 sub, channel 2 main, channel 2 sub, ...
    Main-before-sub within a channel is intentional — when a camera
    sits on channel 3 the user wants `channel=3&subtype=0` (main)
    discovered before `channel=3&subtype=1` (sub) so the main stream
    becomes the primary stream URL.

    `channel_cap` defaults to 16 (4ch / 8ch / 16ch DVRs and most
    consumer NVRs). NVRs with >16 physical channels would need a
    per-entry override (deferred to a later release; tracked in the
    Lorex/Dahua DVR Family Support Plan). Caps the recipe's `channels`
    list at the cap value, then appends `fallback_paths` verbatim.

    Returns [] for unrecognised recipe shape (wrong `type`, missing
    `path_template`) so callers can fall through to the universal path
    list without special-casing."""
    if recipe.get("type") != "channel_iterate":
        return []
    template = recipe.get("path_template", "")
    if not template:
        return []
    raw_channels = recipe.get("channels", list(range(1, 17)))
    channels    = [c for c in raw_channels if c <= channel_cap]
    subtypes    = recipe.get("subtypes", [0, 1])
    paths: list[str] = []
    for ch in channels:
        for st in subtypes:
            try:
                paths.append(template.format(ch=ch, st=st))
            except (KeyError, IndexError):
                # Malformed template — skip rather than raise; the
                # walker can still try fallback_paths below.
                continue
    paths.extend(recipe.get("fallback_paths", []))
    # 2.5.0-rc1.1: filter out paths that still contain unexpanded
    # `{...}` placeholders (e.g. recipe fallback_paths declared as
    # `/h264/ch{ch}/main/av_stream` for legacy Dahua firmware — these
    # were intended to iterate channels but the helper currently emits
    # them verbatim; without the filter the walker probes the literal
    # string and the camera responds 401 to a path that can never
    # match). Future improvement: expand placeholders in fallback_paths
    # the same way as path_template above; tracked separately. For now
    # this filter prevents noise probing without losing real-channel
    # coverage (which path_template-based paths above already provide).
    paths = [p for p in paths if "{" not in p and "}" not in p]
    return paths


def _extract_channel_from_rtsp_url(url: str) -> str:
    """2.5.0-rc1.2: pull the `channel=N` value out of a Dahua-format
    RTSP URL like `rtsp://.../cam/realmonitor?channel=3&subtype=0`.
    Returns the channel string (e.g. "3") or "" if not found.

    Used by the post-cred-auth channel enumeration helper to know
    which channel the cred-auth flow already established working,
    so we don't re-walk it during enumeration of the other channels.
    Tolerant of URL form: query may use `&` or `?` separator,
    case-insensitive on the param name."""
    if not url:
        return ""
    m = re.search(r"[?&]channel=(\d+)", url, re.IGNORECASE)
    return m.group(1) if m else ""
MJPEG_PATHS = [
    "/video", "/mjpeg", "/stream", "/stream.mjpeg", "/stream.jpg",
    "/image.mjpeg", "/mjpg/video.mjpg", "/cgi-bin/mjpg/video.cgi",
    "/videostream.cgi", "/mjpeg.cgi", "/video.cgi", "/live.jpg",
    "/snapshot.cgi?count=0", "/video0.mjpeg", "/.mjpg",
    "/cgi-bin/video.cgi", "/axis-cgi/mjpg/video.cgi",
]
HLS_PATHS = [
    "/index.m3u8", "/stream.m3u8", "/live/stream.m3u8",
    "/hls/stream.m3u8", "/hls/index.m3u8", "/live.m3u8",
    "/playlist.m3u8", "/channel1/index.m3u8", "/live/index.m3u8",
    "/streams/live.m3u8", "/hls/live/index.m3u8",
]
WEBRTC_PATHS = [
    "/whep", "/webrtc", "/api/webrtc", "/webrtc/offer",
    "/api/whep", "/offer", "/api/offer", "/live/webrtc", "/stream/webrtc",
]
WS_RTSP_PATHS = [
    "/api/ws", "/ws", "/stream/ws", "/live/ws",
    "/ws/stream", "/websocket", "/stream",
]


def probe_rtsp_socket(host: str, port: int, path: str,
                      username: str = "", password: str = "",
                      timeout: float = 6.0,
                      label: str = "") -> bool:
    """
    Verify an RTSP stream is genuinely streamable.  Returns True only if
    OPTIONS + DESCRIBE succeed AND a SETUP round-trip on the first
    m=video track succeeds (TCP-interleaved first, UDP fallback).  This
    catches the common false-positive case where a camera 200-OKs DESCRIBE
    on a bare root path but rejects SETUP because no real track lives
    there (Microseven and similar firmware).

    Pass label="" for silent (debug-only) logging, or a non-empty
    string (e.g. camera_id/profile) for verbose INFO-level logging
    of each RTSP round-trip — useful when diagnosing credential failures.
    """
    rtsp_url = f"rtsp://{host}:{port}{path}"
    pfx = f"  [probe_rtsp {label or host + ':' + str(port) + path}]"

    def _log(msg: str) -> None:
        if label:
            log.info(pfx + " " + msg)
        else:
            log.debug(pfx + " " + msg)

    CRLF     = chr(13) + chr(10)
    CRLFCRLF = CRLF + CRLF

    def _build_auth(auth_val: str, method: str, uri: str) -> str | None:
        """Build an Authorization header value for the given method+uri,
        given the WWW-Authenticate value from a prior 401 response.

        2.2.9 — qop-aware per RFC 2617 §3.2.2. When the challenge carries
        qop="auth" (observed live on Hikvision DS-2DE4A425IW with realm
        "IP Camera(F0818)" — was previously emitting a no-qop challenge,
        switched to qop="auth" sometime during rc2.x debugging), the
        response digest formula changes to
            MD5(HA1:nonce:nc:cnonce:qop:HA2)
        and the Authorization header must include qop, cnonce, and nc.
        Falls back to the no-qop formula MD5(HA1:nonce:HA2) when qop is
        absent (Hipcam family, older Hikvision firmware, etc.).

        Reuses the server's nonce — RFC 2617 allows nonce reuse for
        subsequent requests in the same session, recomputing the response
        digest with the new method+uri in HA2 (and incrementing nc when
        qop is in play, though we only issue one qop-aware request per
        nonce here so nc=00000001 is correct)."""
        if auth_val.lower().startswith("digest"):
            realm_m = re.search(r'realm="([^"]*)"', auth_val)
            nonce_m = re.search(r'nonce="([^"]*)"', auth_val)
            if not (realm_m and nonce_m):
                return None
            realm, nonce = realm_m.group(1), nonce_m.group(1)
            qop_m = re.search(r'qop="?([^",]+)"?', auth_val)
            ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
            ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
            if qop_m:
                qop_val = qop_m.group(1).strip()
                # Pick "auth" when offered (most common); some servers send
                # "auth,auth-int" and we only do auth (no message-body integrity).
                qop = "auth" if "auth" in qop_val else qop_val.split(",")[0].strip()
                cnonce = os.urandom(8).hex()
                nc = "00000001"
                rsp = hashlib.md5(
                    f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
                ).hexdigest()
                return (f'Digest username="{username}", realm="{realm}", '
                        f'nonce="{nonce}", uri="{uri}", '
                        f'qop={qop}, nc={nc}, cnonce="{cnonce}", '
                        f'response="{rsp}"')
            rsp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            return (f'Digest username="{username}", realm="{realm}", '
                    f'nonce="{nonce}", uri="{uri}", response="{rsp}"')
        elif auth_val.lower().startswith("basic"):
            import base64 as _b64
            return "Basic " + _b64.b64encode(
                f"{username}:{password}".encode()).decode()
        return None

    def _parse_track_url(sdp: str, base_url: str) -> str | None:
        """Find first m=video block in the SDP, extract its a=control:
        value, and resolve it against base_url. Returns None if no
        m=video or no control attribute exists."""
        in_video        = False
        video_control   = None
        session_control = None
        for raw in sdp.split("\n"):
            line = raw.rstrip("\r").strip()
            if line.startswith("m="):
                if in_video:
                    break  # next media block — stop, video control is final
                in_video = line.startswith("m=video")
            elif line.startswith("a=control:"):
                ctrl = line[len("a=control:"):].strip()
                if in_video:
                    video_control = ctrl
                else:
                    session_control = ctrl
        ctrl = video_control or session_control
        if not ctrl:
            return None
        if ctrl == "*":
            return base_url
        if ctrl.lower().startswith("rtsp://"):
            return ctrl
        # Relative — append to base, ensuring exactly one separator
        return (base_url.rstrip("/") + "/" + ctrl)

    sock      = None
    next_cseq = 1
    auth_val  = None   # WWW-Authenticate value if any 401 was returned
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)

        def roundtrip(method: str, cseq: int,
                      extra: dict | None = None,
                      uri: str | None = None) -> str:
            """Send an RTSP request and read response (headers + body if
            Content-Length present). uri overrides the default rtsp_url
            (used for SETUP/TEARDOWN where the track URI differs)."""
            extra  = extra or {}   # safe: new dict each call
            target = uri or rtsp_url
            hdr    = "".join(k + ": " + v + CRLF for k, v in extra.items())
            req    = method + " " + target + " RTSP/1.0" + CRLF
            req   += "CSeq: " + str(cseq) + CRLF + hdr + CRLF
            sock.sendall(req.encode())
            buf = b""
            # Read until end-of-headers
            while CRLFCRLF.encode() not in buf and len(buf) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            text = buf.decode("utf-8", errors="replace")
            # If a Content-Length was advertised, read the body too
            cl = 0
            for line in text.split(CRLF):
                if line.lower().startswith("content-length:"):
                    try:
                        cl = int(line.split(":", 1)[1].strip())
                    except (ValueError, IndexError):
                        cl = 0
                    break
            if cl > 0:
                sep_idx   = text.find(CRLFCRLF)
                already   = (len(buf) - (sep_idx + 4)) if sep_idx >= 0 else 0
                remaining = max(0, cl - already)
                while remaining > 0:
                    chunk = sock.recv(min(remaining, 4096))
                    if not chunk:
                        break
                    buf       += chunk
                    remaining -= len(chunk)
            return buf.decode("utf-8", errors="replace")

        # ── OPTIONS ──────────────────────────────────────────────────────
        resp = roundtrip("OPTIONS", next_cseq); next_cseq += 1
        status_line = resp.split(CRLF)[0].strip()
        if "RTSP/1.0 2" not in resp:
            _log(f"OPTIONS → {status_line!r} (not 2xx — giving up)")
            return False
        _log("OPTIONS → OK")

        # ── DESCRIBE (with optional 401-retry) ───────────────────────────
        resp = roundtrip("DESCRIBE", next_cseq, {"Accept": "application/sdp"})
        next_cseq += 1
        status_line = resp.split(CRLF)[0].strip()
        if "RTSP/1.0 200" in resp:
            _log("DESCRIBE → 200 OK (no auth required)")
        elif "401" in resp:
            if not username:
                _log("DESCRIBE → 401 (no credentials provided — rejecting)")
                return False
            auth_line = next(
                (l for l in resp.splitlines()
                 if l.lower().startswith("www-authenticate:")), "")
            auth_val = auth_line.split(":", 1)[-1].strip() if auth_line else ""
            if not auth_val:
                _log("DESCRIBE → 401 with no WWW-Authenticate header")
                return False
            scheme = auth_val.split()[0] if auth_val else "?"
            _log(f"DESCRIBE → 401 ({scheme})")
            auth_hdr = _build_auth(auth_val, "DESCRIBE", rtsp_url)
            if not auth_hdr:
                _log(f"DESCRIBE → 401 unparseable auth: {auth_val[:60]!r}")
                return False
            resp = roundtrip("DESCRIBE", next_cseq,
                             {"Accept": "application/sdp",
                              "Authorization": auth_hdr})
            next_cseq += 1
            if "RTSP/1.0 200" not in resp:
                sl = resp.split(CRLF)[0].strip()
                _log(f"DESCRIBE (authenticated) → {sl!r}")
                return False
            _log("DESCRIBE (authenticated) → 200 OK")
        else:
            _log(f"DESCRIBE → {status_line!r} (not 200 / not 401 — rejecting)")
            return False

        # ── Parse SDP for first m=video track URL ────────────────────────
        body_idx  = resp.find(CRLFCRLF)
        sdp_text  = resp[body_idx + 4:] if body_idx >= 0 else ""
        track_url = _parse_track_url(sdp_text, rtsp_url)
        if not track_url:
            _log("DESCRIBE 200 but SDP has no m=video / a=control — rejecting")
            return False
        _log(f"SDP track URL: {track_url}")

        # ── SETUP: TCP-interleaved first, UDP fallback ───────────────────
        # Catches the false-positive case where DESCRIBE 200 OKs but the
        # camera has no real track at this path (rejects SETUP with 4xx).
        # We never bind UDP locally — server's 200 OK to SETUP is enough
        # to verify the stream is genuinely streamable.
        transports = [
            ("RTP/AVP/TCP;unicast;interleaved=0-1",            "TCP-interleaved"),
            ("RTP/AVP/UDP;unicast;client_port=50000-50001",    "UDP"),
        ]
        for transport_hdr, tlabel in transports:
            extra = {"Transport": transport_hdr}
            if auth_val:
                ah = _build_auth(auth_val, "SETUP", track_url)
                if ah:
                    extra["Authorization"] = ah
            resp = roundtrip("SETUP", next_cseq, extra, uri=track_url)
            next_cseq += 1
            sl = resp.split(CRLF)[0].strip()
            if "RTSP/1.0 200" in resp:
                # Extract Session header so TEARDOWN can release it
                session = ""
                for line in resp.split(CRLF):
                    if line.lower().startswith("session:"):
                        session = line.split(":", 1)[1].strip().split(";")[0].strip()
                        break
                _log(f"SETUP ({tlabel}) → 200 OK (session={session[:12]!r})")

                # ── TEARDOWN — release session cleanly ────────────────────
                td_extra: dict = {}
                if session:
                    td_extra["Session"] = session
                if auth_val:
                    ah = _build_auth(auth_val, "TEARDOWN", track_url)
                    if ah:
                        td_extra["Authorization"] = ah
                try:
                    roundtrip("TEARDOWN", next_cseq, td_extra, uri=track_url)
                    next_cseq += 1
                except Exception:
                    pass   # TEARDOWN failure is non-fatal — socket close
                           # also releases server-side state
                return True
            _log(f"SETUP ({tlabel}) → {sl!r}")

        return False

    except Exception as e:
        _log(f"exception: {e}")
        return False
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def probe_rtsp(url: str, username: str = "", password: str = "",
               timeout: int = 6, label: str = "") -> bool:
    """Thin wrapper — parses URL and delegates to probe_rtsp_socket.
    Pass label (e.g. camera_id/profile_name) to get verbose INFO-level logging."""
    try:
        from urllib.parse import urlparse
        p    = urlparse(url)
        host = p.hostname or ""
        port = p.port or 554
        path = p.path or "/"
        # Prefer caller-supplied credentials over any embedded in the URL
        u  = username or p.username or ""
        pw = password or p.password or ""
        return probe_rtsp_socket(host, port, path, u, pw,
                                 timeout=timeout, label=label)
    except Exception as e:
        log.debug(f"probe_rtsp: {e}")
        return False


def _probe_rtsp_paths_single_socket(
    host: str,
    port: int,
    paths: list[str],
    username: str = "",
    password: str = "",
    timeout: float = 6.0,
    extra_query: str = "",
    label: str = "",
    host_meta: dict | None = None,
    collect_locked: bool = False,
    expected_realm: str = "",
    deep_reprobe_mode: bool = False,
    brand_recipe_paths: list[str] | None = None,
) -> tuple[str | None, bool]:
    """rc2 Layer 1: Walk multiple RTSP paths through OPTIONS+DESCRIBE+
    SETUP+TEARDOWN on a SINGLE TCP socket. Returns a tuple
    (url_or_none, looks_like_rtsp_server).

    looks_like_rtsp_server is True if at least one response started with
    "RTSP/" — even if the status was 4xx/5xx, the server demonstrably
    speaks RTSP. False means we received NO RTSP-formatted responses
    (server is HTTP, raw TCP, or the path failed before any handshake).
    rc2.1 callers use this to fast-bail Layer 2 when the wrong port was
    probed (e.g. RTSP path-walk against port 80 of a Hikvision NVR).

    This is the universal-safe probe per RFC 2326 §9.1 (servers must
    queue per-socket requests in order; no documented camera firmware
    rejects sequential request-response on the same socket). Critical
    for cameras with per-IP TCP rate-limits (Hipcam/Microseven family
    — opening a second socket within ~5s of the first is RST'd, but
    walking paths on ONE socket bypasses the throttle entirely).

    Always reads the full response (headers + Content-Length-bounded
    body) before sending the next request — never pipelines.

    extra_query — appended to every path (e.g. "?Axis-Orig-Sw=true"
    for Axis Companion). Empty by default.

    rc2.1.1: host_meta — when provided, the walker captures the RTSP
    'Server:' header from the first response that includes one and
    writes it to host_meta["server_header"] in place. The orchestrator
    then re-runs brand identification post-walk so cameras that didn't
    identify from MAC OUI alone (Microseven — not in nmap_results,
    so no mac_vendor) can still be identified by their Hipcam RealServer
    server string.

    2.4.0-rc2.0 (Layered Stream Discovery): collect_locked — when True,
    AFTER finding a working unauthenticated URL the walker continues
    through remaining paths, collecting 401 responses whose realm
    matches expected_realm (or any 401 if expected_realm is empty).
    Each match is added to host_meta["locked_streams"] as a dict
    {"path": <path>, "realm": <realm>, "scheme": <scheme>}. The first
    working URL is still returned as the primary `found` value; the
    locked list surfaces in the UI as a "View Locked Streams (N)" badge
    so the user can supply credentials and unlock additional streams
    (sub-streams, third streams, audio-only feeds) that the unauth
    walk found but couldn't authenticate against.

    expected_realm — when non-empty, restricts locked-stream candidates
    to 401s whose realm matches. Prevents surfacing locked streams that
    need different credentials than the camera's primary auth domain.
    Captured upstream by the OPTIONS fingerprint helper (rc1.0) into
    host_meta["rtsp_auth_realm"]; the orchestrator passes that value
    here as expected_realm.
    """
    if not paths:
        return (None, False)

    # rc2.1: tracks whether ANY response from the server started with
    # "RTSP/" — used by the orchestrator to skip Layer 2 when we
    # confirmed the host doesn't speak RTSP at this port.
    looks_like_rtsp: bool = False

    # rc2.1.1: capture Server header from first response that has one.
    # Written back to host_meta at end so brand-id can re-run with it.
    captured_server: str = ""

    # 2.4.0-rc2.0 (Layered Stream Discovery): once a working URL is
    # found AND collect_locked=True, we keep walking and accumulate
    # 401-with-matching-realm into this list. Persisted to host_meta
    # at the finally block (alongside server_header) so the orchestrator
    # can surface them in the UI badge.
    locked_streams: list[dict] = []
    found_working_url: str | None = None

    pfx = f"  [probe_rtsp_walk {label or host + ':' + str(port)}]"

    def _log(msg: str) -> None:
        if label:
            log.info(pfx + " " + msg)
        else:
            log.debug(pfx + " " + msg)

    CRLF     = chr(13) + chr(10)
    CRLFCRLF = CRLF + CRLF

    def _build_auth(auth_val: str, method: str, uri: str) -> str | None:
        """Identical to probe_rtsp_socket's _build_auth — RFC 2617 nonce
        reuse with per-method+uri response digest recompute. 2.2.9 — adds
        qop=auth handling per RFC 2617 §3.2.2 for cameras (e.g. Hikvision
        DS-2DE4A425IW) that emit qop="auth" in the challenge."""
        if auth_val.lower().startswith("digest"):
            realm_m = re.search(r'realm="([^"]*)"', auth_val)
            nonce_m = re.search(r'nonce="([^"]*)"', auth_val)
            if not (realm_m and nonce_m):
                return None
            realm, nonce = realm_m.group(1), nonce_m.group(1)
            qop_m = re.search(r'qop="?([^",]+)"?', auth_val)
            ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
            ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
            if qop_m:
                qop_val = qop_m.group(1).strip()
                qop = "auth" if "auth" in qop_val else qop_val.split(",")[0].strip()
                cnonce = os.urandom(8).hex()
                nc = "00000001"
                rsp = hashlib.md5(
                    f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
                ).hexdigest()
                return (f'Digest username="{username}", realm="{realm}", '
                        f'nonce="{nonce}", uri="{uri}", '
                        f'qop={qop}, nc={nc}, cnonce="{cnonce}", '
                        f'response="{rsp}"')
            rsp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            return (f'Digest username="{username}", realm="{realm}", '
                    f'nonce="{nonce}", uri="{uri}", response="{rsp}"')
        elif auth_val.lower().startswith("basic"):
            return "Basic " + base64.b64encode(
                f"{username}:{password}".encode()).decode()
        return None

    def _parse_track_url(sdp: str, base_url: str) -> str | None:
        """Identical to probe_rtsp_socket's _parse_track_url."""
        in_video        = False
        video_control   = None
        session_control = None
        for raw in sdp.split("\n"):
            line = raw.rstrip("\r").strip()
            if line.startswith("m="):
                if in_video:
                    break
                in_video = line.startswith("m=video")
            elif line.startswith("a=control:"):
                ctrl = line[len("a=control:"):].strip()
                if in_video:
                    video_control = ctrl
                else:
                    session_control = ctrl
        ctrl = video_control or session_control
        if not ctrl:
            return None
        if ctrl == "*":
            return base_url
        if ctrl.lower().startswith("rtsp://"):
            return ctrl
        return base_url.rstrip("/") + "/" + ctrl

    sock      = None
    next_cseq = 1
    # auth_val is captured from first 401 and reused across all paths on
    # this socket. Per RFC 2617, nonce reuse is allowed; we just recompute
    # the response digest for each method+uri pair.
    auth_val: str | None = None

    # 2.4.0-rc2.4: early-bail counter for consecutive same-realm 401s.
    # When auth is required at the server level (RFC 7235 §2.2 — realm
    # is server-scoped, not URL-scoped), every path on the same socket
    # will get the same 401 with the same realm. Walking 25-31 paths
    # to confirm what we already know wastes ~5s per port. After 5
    # consecutive same-realm 401s we bail and save state for the
    # Deep Re-Probe button. The counter resets on any non-401 response
    # OR a 401 with a DIFFERENT realm (rare but possible — some
    # firmwares scope auth per-resource).
    consecutive_same_realm_401s: int = 0
    early_bail_realm: str = ""
    EARLY_BAIL_THRESHOLD: int = 5
    bailed_early: bool = False
    paths_tried_count: int = 0

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)

        def roundtrip(method: str, cseq: int,
                      extra: dict | None = None,
                      uri: str = "") -> str:
            """Send RTSP request on the persistent sock, read full response
            (headers + body if Content-Length present). Returns response text.
            Raises socket exceptions if the connection dies — caller must
            decide whether to bail."""
            extra = extra or {}
            target = uri
            hdr = "".join(k + ": " + v + CRLF for k, v in extra.items())
            req = method + " " + target + " RTSP/1.0" + CRLF
            req += "CSeq: " + str(cseq) + CRLF + hdr + CRLF
            sock.sendall(req.encode())
            buf = b""
            while CRLFCRLF.encode() not in buf and len(buf) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            text = buf.decode("utf-8", errors="replace")
            cl = 0
            for line in text.split(CRLF):
                if line.lower().startswith("content-length:"):
                    try:
                        cl = int(line.split(":", 1)[1].strip())
                    except (ValueError, IndexError):
                        cl = 0
                    break
            if cl > 0:
                sep_idx   = text.find(CRLFCRLF)
                already   = (len(buf) - (sep_idx + 4)) if sep_idx >= 0 else 0
                remaining = max(0, cl - already)
                while remaining > 0:
                    chunk = sock.recv(min(remaining, 4096))
                    if not chunk:
                        break
                    buf       += chunk
                    remaining -= len(chunk)
            return buf.decode("utf-8", errors="replace")

        for path_idx, path in enumerate(paths):
            full_path = path + extra_query
            rtsp_url = f"rtsp://{host}:{port}{full_path}"
            _log(f"({path_idx+1}/{len(paths)}) trying {full_path}")

            # ── OPTIONS ──────────────────────────────────────────────
            try:
                resp = roundtrip("OPTIONS", next_cseq, uri=rtsp_url)
                next_cseq += 1
            except Exception as e:
                _log(f"OPTIONS exception → bailing single-socket walk: {e}")
                # 2.4.0-rc3.5 ACD: a mid-walk OPTIONS exception means the
                # camera RST/closed the socket before we got a response.
                # Record so that if it happens again on this IP within the
                # ACD window, the per-IP cooldown escalates.
                _record_rst_observation(host)
                return (None, looks_like_rtsp)  # socket dead
            # rc2.1: detect RTSP-server-ness. ANY response starting with
            # "RTSP/" — even 4xx/5xx — confirms the server speaks RTSP.
            # If no path ever produces an RTSP-formatted reply, the
            # orchestrator skips Layer 2 (wrong port / not an RTSP server).
            if resp.startswith("RTSP/"):
                looks_like_rtsp = True
                # rc2.1.1: capture Server header — first one found wins.
                # The Hipcam RealServer firmware family always returns
                # `Server: Hipcam RealServer/V1.0` even on auth-required
                # responses, so this fires for the Microseven case
                # (no mac_vendor available, but Server header reliably
                # identifies the firmware family).
                if not captured_server:
                    for _line in resp.split(CRLF):
                        if _line.lower().startswith("server:"):
                            captured_server = _line.split(":", 1)[1].strip()
                            _log(f"captured Server: {captured_server!r}")
                            break
            if "RTSP/1.0 2" not in resp:
                # 2.5.0-rc1.1: when OPTIONS returns 401 with WWW-
                # Authenticate AND we have credentials, don't skip the
                # path — capture the auth challenge into auth_val and
                # fall through to DESCRIBE so the existing DESCRIBE-401
                # retry-with-Digest path can authenticate. Without this
                # fall-through, DVRs/NVRs that enforce auth on OPTIONS
                # itself (Lorex/Dahua DVR-NVR family confirmed; some
                # Dahua IPC variants likely too) silently skip every
                # path during cred-auth because the walker treated
                # 401-on-OPTIONS as "path not present." Hikvision-
                # style firmware avoids the bug by allowing OPTIONS
                # without auth and only enforcing auth on DESCRIBE.
                _is_opts_401 = "RTSP/1.0 401" in resp
                _has_creds   = bool(username or password)
                if _is_opts_401 and _has_creds:
                    if not auth_val:
                        _opts_auth_line = next(
                            (l for l in resp.splitlines()
                             if l.lower().startswith("www-authenticate:")),
                            "")
                        auth_val = (
                            _opts_auth_line.split(":", 1)[-1].strip()
                            if _opts_auth_line else "")
                    if auth_val:
                        _log(f"OPTIONS → 401 with auth challenge captured "
                             f"— falling through to DESCRIBE for {path}")
                        # Don't `continue` — fall through to DESCRIBE.
                        # DESCRIBE will also 401, then auth-retry uses
                        # the auth_val we just captured.
                    else:
                        _log(f"OPTIONS → 401 with no WWW-Authenticate "
                             f"— skipping path")
                        consecutive_same_realm_401s = 0
                        continue
                else:
                    _log(f"OPTIONS → {resp.split(CRLF)[0].strip()!r} "
                         f"— skipping path")
                    # 2.4.0-rc2.4: reset early-bail counter — non-401
                    # response means this isn't a same-realm-401 streak.
                    consecutive_same_realm_401s = 0
                    # Path-level rejection: try next path, socket likely still alive
                    continue

            # ── DESCRIBE (with optional 401-retry) ───────────────────
            try:
                resp = roundtrip("DESCRIBE", next_cseq,
                                 {"Accept": "application/sdp"},
                                 uri=rtsp_url)
                next_cseq += 1
            except Exception as e:
                _log(f"DESCRIBE exception → bailing: {e}")
                return (None, looks_like_rtsp)
            if "RTSP/1.0 200" in resp:
                # 2.4.0-rc2.4: reset early-bail counter — got a 200,
                # streak of consecutive same-realm 401s is broken.
                consecutive_same_realm_401s = 0
                pass  # continue to SDP parse
            elif "401" in resp:
                # 2.4.0-rc2.4: extract realm for the early-bail
                # counter. Need to do this regardless of whether
                # collect_locked is enabled, because the counter
                # decision is independent.
                _401_realm = ""
                _auth_line_global = next(
                    (l for l in resp.splitlines()
                     if l.lower().startswith("www-authenticate:")), "")
                if _auth_line_global:
                    _auth_v_global = _auth_line_global.split(":", 1)[-1].strip()
                    _realm_m = re.search(r'realm="([^"]*)"', _auth_v_global)
                    _401_realm = _realm_m.group(1) if _realm_m else ""

                # 2.4.0-rc2.0: when collect_locked=True AND we already
                # found a working unauth URL, treat this 401 as a
                # locked-stream candidate. Filter by expected_realm so
                # we only surface streams that share the camera's
                # primary auth domain (avoids prompting for creds that
                # won't work). Captures realm from the WWW-Authenticate
                # header before falling through to the existing skip-
                # without-creds path.
                # 2.4.0-rc2.5: in deep_reprobe_mode, eagerly collect
                # locked candidates regardless of whether an unauth
                # working_url was found first. The original rc2.0
                # gating (`collect_locked and found_working_url`)
                # was designed for the discovery scan where surfacing
                # locked candidates only makes sense IF the user can
                # already see ONE stream and is being told there are
                # MORE that need creds. But Deep Re-Probe is an
                # explicit user-driven enumeration on a camera that
                # may have NO unauth streams (e.g. Hikvision);
                # in that case the user wants to see the locked
                # candidates anyway so they can enter creds and
                # unlock them.
                want_collect = collect_locked and not username and (
                    found_working_url or deep_reprobe_mode)
                if want_collect:
                    auth_line = next(
                        (l for l in resp.splitlines()
                         if l.lower().startswith("www-authenticate:")), "")
                    if auth_line:
                        auth_v = auth_line.split(":", 1)[-1].strip()
                        scheme = auth_v.split(None, 1)[0] if auth_v else ""
                        realm_m = re.search(r'realm="([^"]*)"', auth_v)
                        realm = realm_m.group(1) if realm_m else ""
                        # Same-realm filter: only surface 401s that match
                        # the camera's captured rtsp_auth_realm. If
                        # expected_realm is empty (caller didn't supply
                        # one), accept all realms — but the orchestrator
                        # only enables collect_locked when a realm WAS
                        # captured upstream, so this fallback is rare.
                        # 2.4.0-rc2.6: brand-recipe filter. When the
                        # caller has identified the camera's brand and
                        # supplied the brand's known RTSP path list
                        # (brand_recipe_paths), only surface 401s whose
                        # path matches the recipe. Without this filter,
                        # cameras like Hikvision that 401 ALL paths
                        # (including paths that aren't valid streams on
                        # the actual camera, e.g. /cam/realmonitor on a
                        # Hikvision DS-2DE) produce dozens of bogus
                        # locked candidates. With it, the count drops
                        # to the brand's actual stream paths (~6 for
                        # Hikvision DS-2). When brand_recipe_paths is
                        # None, no filter is applied (backward-compat
                        # for callers that don't know the brand).
                        path_matches_recipe = True
                        if brand_recipe_paths:
                            # Match by path-prefix to allow variations
                            # like /Streaming/Channels/101 vs /102/103
                            # all matching /Streaming/Channels/.
                            path_matches_recipe = any(
                                path.startswith(rp.rsplit("/", 1)[0]
                                               + "/")
                                or path == rp
                                or path.split("?")[0] == rp.split("?")[0]
                                for rp in brand_recipe_paths)
                        if (not expected_realm or realm == expected_realm) \
                                and path_matches_recipe:
                            locked_streams.append({
                                "path": path,
                                "realm": realm,
                                "scheme": scheme,
                            })
                            _log(f"LOCKED candidate: {path} (realm={realm!r})")
                        elif not path_matches_recipe:
                            _log(f"DESCRIBE 401 path {path!r} not in "
                                 f"brand recipe — not surfacing as "
                                 f"locked candidate")
                        else:
                            _log(f"DESCRIBE 401 different realm "
                                 f"(got {realm!r}, expected "
                                 f"{expected_realm!r}) — not surfacing")
                if not username:
                    _log(f"DESCRIBE → 401 (no creds) — skipping {path}")
                    # 2.4.0-rc2.4: early-bail counter. Track consecutive
                    # 401s that share the same realm. After N hits, we
                    # have high confidence that all remaining paths will
                    # also 401 with the same realm (auth is enforced at
                    # the server level per RFC 7235 §2.2). Bail out and
                    # save state so the Deep Re-Probe button can resume
                    # the walk on demand.
                    if early_bail_realm == "" and _401_realm:
                        early_bail_realm = _401_realm
                    if _401_realm and _401_realm == early_bail_realm:
                        consecutive_same_realm_401s += 1
                    else:
                        # Different realm OR no realm — reset counter
                        consecutive_same_realm_401s = 0
                        if _401_realm:
                            early_bail_realm = _401_realm
                            consecutive_same_realm_401s = 1
                    paths_tried_count = path_idx + 1
                    if consecutive_same_realm_401s >= EARLY_BAIL_THRESHOLD:
                        # 2.4.0-rc2.5: in deep_reprobe_mode the user
                        # explicitly wants to walk every path —
                        # don't bail out, just keep going so all
                        # remaining 401s get surfaced as locked
                        # candidates. The original rc2.4 bail logic
                        # is the right default for discovery scans
                        # (saves ~5s/camera) but wrong here.
                        if deep_reprobe_mode:
                            _log(f"early-bail threshold reached but "
                                 f"deep_reprobe_mode=True — continuing "
                                 f"to enumerate all locked candidates")
                            continue
                        # 2.4.0-rc2.5: log at INFO unconditionally
                        # (was using _log which downgrades to DEBUG
                        # for unlabeled walks — i.e. all scan-time
                        # walks, hiding evidence that the optimization
                        # was firing during normal scans).
                        log.info(pfx + f" Layer 1 early-bail: "
                                 f"{EARLY_BAIL_THRESHOLD} consecutive "
                                 f"401s with realm={early_bail_realm!r} "
                                 f"— remaining {len(paths) - paths_tried_count} "
                                 f"path(s) will likely also 401; saving "
                                 f"state for Deep Re-Probe")
                        bailed_early = True
                        break
                    continue
                # Capture auth_val from this 401 if we haven't already
                if not auth_val:
                    auth_line = next(
                        (l for l in resp.splitlines()
                         if l.lower().startswith("www-authenticate:")), "")
                    auth_val = auth_line.split(":", 1)[-1].strip() if auth_line else ""
                if not auth_val:
                    _log("DESCRIBE → 401 with no WWW-Authenticate — skipping")
                    continue
                auth_hdr = _build_auth(auth_val, "DESCRIBE", rtsp_url)
                if not auth_hdr:
                    _log(f"DESCRIBE → unparseable auth: {auth_val[:60]!r}")
                    continue
                try:
                    resp = roundtrip("DESCRIBE", next_cseq,
                                     {"Accept": "application/sdp",
                                      "Authorization": auth_hdr},
                                     uri=rtsp_url)
                    next_cseq += 1
                except Exception as e:
                    _log(f"DESCRIBE-auth exception → bailing: {e}")
                    return (None, looks_like_rtsp)
                if "RTSP/1.0 200" not in resp:
                    _log(f"DESCRIBE-auth → {resp.split(CRLF)[0].strip()!r}")
                    # 2.5.0-rc1.0: auth_attempt_lockout policy. For
                    # brands whose throttle is a per-IP failed-auth
                    # counter (Lorex/Dahua DVR-NVR family: 10 failed
                    # attempts then ~30 min lockout or until power-
                    # cycle), continuing the walk after a Digest-auth-
                    # rejected response burns additional attempts on
                    # credentials we already know are wrong. Stop the
                    # entire walk after the first auth rejection so
                    # the user surfaces a clean "credentials wrong"
                    # failure with one used attempt. Only fires when
                    # host_meta plumbed the throttle type through
                    # (find_rtsp_path does this when the brand entry
                    # is matched). Sets a flag on host_meta so caller
                    # (cred-auth) can surface a counter-aware message.
                    _walker_throttle = ""
                    if host_meta:
                        _walker_throttle = str(host_meta.get(
                            "walker_throttle_type", "") or "")
                    if _walker_throttle == "auth_attempt_lockout":
                        _log(f"auth_attempt_lockout brand — bailing walk "
                             f"after first auth rejection (preserves "
                             f"remaining attempts before camera lockout)")
                        if host_meta is not None:
                            host_meta["walker_auth_lockout_bailed"] = True
                        return (None, looks_like_rtsp)
                    continue
            else:
                _log(f"DESCRIBE → {resp.split(CRLF)[0].strip()!r} — skipping")
                # 2.4.0-rc2.4: reset early-bail counter — non-401
                # response means this isn't a same-realm-401 streak.
                consecutive_same_realm_401s = 0
                continue

            # ── Parse SDP for first m=video track URL ───────────────
            body_idx  = resp.find(CRLFCRLF)
            sdp_text  = resp[body_idx + 4:] if body_idx >= 0 else ""
            track_url = _parse_track_url(sdp_text, rtsp_url)
            if not track_url:
                _log(f"DESCRIBE 200 but SDP has no m=video — skipping {path}")
                continue

            # 2.5.0-rc1.0: stricter populated-channel test when the brand
            # entry's streaming_recipe directs us to use it. DVR/NVR
            # devices commonly return 200 OK with valid-looking SDP on
            # channels that have NO physical camera connected — the
            # `m=video` line is present but no `a=rtpmap` codec mapping
            # follows, indicating a virtual/empty stream slot. Without
            # this filter, the walker would surface the first 200-OK
            # channel as the working stream and the user would see a
            # blank or frozen card. host_meta-driven so unaffected
            # (single-camera, IP-camera) probes get the original behavior.
            _populated_test = ""
            if host_meta:
                _populated_test = str(host_meta.get(
                    "walker_populated_channel_test", "") or "")
            if _populated_test == "sdp_has_video_track":
                if not _sdp_has_video_track(sdp_text):
                    _log(f"DESCRIBE 200 but SDP failed populated-channel "
                         f"test (no real video codec rtpmap) — likely "
                         f"empty channel; skipping {path}")
                    continue

            # ── SETUP: TCP-interleaved first, UDP fallback ──────────
            transports = [
                ("RTP/AVP/TCP;unicast;interleaved=0-1",            "TCP"),
                ("RTP/AVP/UDP;unicast;client_port=50000-50001",    "UDP"),
            ]
            setup_ok = False
            session  = ""
            for transport_hdr, tlabel in transports:
                extra: dict[str, str] = {"Transport": transport_hdr}
                if auth_val:
                    ah = _build_auth(auth_val, "SETUP", track_url)
                    if ah:
                        extra["Authorization"] = ah
                try:
                    resp = roundtrip("SETUP", next_cseq, extra, uri=track_url)
                    next_cseq += 1
                except Exception as e:
                    _log(f"SETUP exception → bailing: {e}")
                    return (None, looks_like_rtsp)
                if "RTSP/1.0 200" in resp:
                    for line in resp.split(CRLF):
                        if line.lower().startswith("session:"):
                            session = (line.split(":", 1)[1]
                                           .strip().split(";")[0].strip())
                            break
                    _log(f"SETUP ({tlabel}) → 200 OK at {path}")
                    setup_ok = True
                    break
                _log(f"SETUP ({tlabel}) → {resp.split(CRLF)[0].strip()!r}")

            if not setup_ok:
                # SETUP failed for this path — the socket is still alive
                # (server replied with status), so we can try the next path.
                # No TEARDOWN needed since SETUP didn't succeed.
                continue

            # ── TEARDOWN — release session before returning ─────────
            td_extra: dict[str, str] = {}
            if session:
                td_extra["Session"] = session
            if auth_val:
                ah = _build_auth(auth_val, "TEARDOWN", track_url)
                if ah:
                    td_extra["Authorization"] = ah
            try:
                roundtrip("TEARDOWN", next_cseq, td_extra, uri=track_url)
                next_cseq += 1
            except Exception:
                pass  # TEARDOWN failure is non-fatal — socket close releases state

            # 2.4.0-rc2.0 (Layered Stream Discovery): if collect_locked
            # is on AND we haven't yet recorded the first working URL,
            # capture it and continue walking. Subsequent paths get
            # OPTIONS+DESCRIBE only; their SETUPs aren't run because
            # we've already proven a working stream and don't need to
            # waste another SETUP/TEARDOWN cycle to enumerate locked
            # candidates. For 2.4.0-rc2.0 the extension is conservative
            # — we only collect 401-locked paths from continued
            # DESCRIBEs after this point. SETUP-tested confirmation of
            # a *second* working unauth URL would be a future extension
            # (it isn't useful for current UX since we only need ONE
            # working URL per camera).
            if collect_locked and found_working_url is None:
                found_working_url = rtsp_url
                _log(f"continuing walk to collect locked candidates "
                     f"after first success: {rtsp_url}")
                continue
            # Normal mode (or second+ success in collect mode): bail
            return (rtsp_url, True)

        # Walked every path. In collect mode, return the first working
        # URL we found (could be None if nothing worked).
        if collect_locked and found_working_url:
            return (found_working_url, True)

        # Walked every path without finding a streamable track
        return (None, looks_like_rtsp)

    except Exception as e:
        _log(f"single-socket walk exception: {e}")
        return (None, looks_like_rtsp)
    finally:
        # rc2.1.1: persist captured Server header to host_meta on EVERY
        # return path (success, no-match, exception, socket-dead). The
        # orchestrator re-runs brand-id post-walk using this signal,
        # which is critical for cameras like the Microseven whose
        # mac_vendor isn't available (camera not in nmap_results due to
        # its own TCP rate-limit defeating the focused port scan).
        if host_meta is not None and captured_server:
            host_meta["server_header"] = captured_server
        # 2.4.0-rc2.0: persist locked_streams to host_meta. We always
        # write the list (even when empty) so downstream readers can
        # distinguish "feature ran, found nothing" from "feature didn't
        # run". The UI badge is shown only when len > 0 AND the camera
        # has no saved creds.
        if host_meta is not None and collect_locked:
            host_meta["locked_streams"] = locked_streams
        # 2.4.0-rc2.4: persist early-bail state. When Layer 1 bailed
        # after EARLY_BAIL_THRESHOLD consecutive same-realm 401s, save
        # what we tried + what's remaining onto host_meta so the
        # Deep Re-Probe button can resume the walk on demand without
        # re-walking what we already know will 401.
        if host_meta is not None and bailed_early:
            host_meta["early_bail_reason"] = "layer1_consecutive_401s"
            host_meta["early_bail_realm"] = early_bail_realm
            host_meta["early_bail_paths_tried"] = list(
                paths[:paths_tried_count])
            host_meta["early_bail_paths_remaining"] = list(
                paths[paths_tried_count:])
            host_meta["early_bail_at"] = datetime.datetime.utcnow().isoformat()
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def _validate_rtsp_urls_single_socket(
    host: str,
    port: int,
    urls: list[str],
    username: str = "",
    password: str = "",
    timeout: float = 6.0,
    host_meta: dict | None = None,
    label: str = "",
) -> dict:
    """2.3.0: Validate a list of FULL RTSP URLs over a SINGLE TCP socket.

    Sibling to _probe_rtsp_paths_single_socket but with two key
    differences:
      • Walks the ENTIRE list (no early return on first match) and
        returns a dict mapping each URL → bool (probe_ok).
      • Designed for AFTER cred-auth: callers already have working
        credentials from the user. We capture the auth challenge from
        the first 401 (if any) and reuse the nonce across all URLs
        per RFC 2617.

    Used by the cred-auth handler to validate all ONVIF profile stream
    URLs (and any STREAM_DB-supplemental URLs) over ONE TCP connection
    instead of opening N sockets in rapid sequence — which is what
    triggered the Microseven firmware-level lockout in rc2.x.

    Mechanics — same as the discovery walker:
      • Full Content-Length-bounded reads before next request (no
        pipelining — universal-safe per RFC 2326 §9.1).
      • OPTIONS → DESCRIBE (with optional 401 retry) → SETUP → TEARDOWN
        per URL. SETUP confirms the server can stream the track.
      • TCP-interleaved transport tried first, UDP fallback second.
      • Captures Server: header into host_meta on first response.

    Returns: dict {url: probe_ok}. URLs are validated in order; if the
    socket dies mid-way, all remaining URLs return False (caller can
    retry on a fresh socket if it cares — most callers don't, since
    the dead socket itself indicates a broken stream).

    Empty input → returns {}.
    """
    if not urls:
        return {}

    results: dict = {url: False for url in urls}
    captured_server: str = ""

    pfx = f"  [validate_rtsp_walk {label or host + ':' + str(port)}]"

    def _log(msg: str) -> None:
        if label:
            log.info(pfx + " " + msg)
        else:
            log.debug(pfx + " " + msg)

    CRLF     = chr(13) + chr(10)
    CRLFCRLF = CRLF + CRLF

    def _build_auth(auth_val: str, method: str, uri: str) -> str | None:
        """Identical to _probe_rtsp_paths_single_socket._build_auth — RFC
        2617 nonce reuse, qop=auth handling for Hikvision-style challenges."""
        if auth_val.lower().startswith("digest"):
            realm_m = re.search(r'realm="([^"]*)"', auth_val)
            nonce_m = re.search(r'nonce="([^"]*)"', auth_val)
            if not (realm_m and nonce_m):
                return None
            realm, nonce = realm_m.group(1), nonce_m.group(1)
            qop_m = re.search(r'qop="?([^",]+)"?', auth_val)
            ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
            ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
            if qop_m:
                qop_val = qop_m.group(1).strip()
                qop = "auth" if "auth" in qop_val else qop_val.split(",")[0].strip()
                cnonce = os.urandom(8).hex()
                nc = "00000001"
                rsp = hashlib.md5(
                    f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
                ).hexdigest()
                return (f'Digest username="{username}", realm="{realm}", '
                        f'nonce="{nonce}", uri="{uri}", '
                        f'qop={qop}, nc={nc}, cnonce="{cnonce}", '
                        f'response="{rsp}"')
            rsp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            return (f'Digest username="{username}", realm="{realm}", '
                    f'nonce="{nonce}", uri="{uri}", response="{rsp}"')
        elif auth_val.lower().startswith("basic"):
            return "Basic " + base64.b64encode(
                f"{username}:{password}".encode()).decode()
        return None

    def _parse_track_url(sdp: str, base_url: str) -> str | None:
        """Identical to _probe_rtsp_paths_single_socket._parse_track_url."""
        in_video        = False
        video_control   = None
        session_control = None
        for raw in sdp.split("\n"):
            line = raw.rstrip("\r").strip()
            if line.startswith("m="):
                if in_video:
                    break
                in_video = line.startswith("m=video")
            elif line.startswith("a=control:"):
                ctrl = line[len("a=control:"):].strip()
                if in_video:
                    video_control = ctrl
                else:
                    session_control = ctrl
        ctrl = video_control or session_control
        if not ctrl:
            return None
        if ctrl == "*":
            return base_url
        if ctrl.lower().startswith("rtsp://"):
            return ctrl
        return base_url.rstrip("/") + "/" + ctrl

    sock      = None
    next_cseq = 1
    auth_val: str | None = None

    # 2.3.2 defense-in-depth: warn if the URLs being walked don't actually
    # point to the host:port we're about to connect to. The 2.3.0+ caller
    # in api_set_credentials was passing a wrong port for two minor
    # versions before this was caught — this check makes the same class
    # of misuse visible immediately in any future caller's logs.
    if urls:
        try:
            first_parsed = urlparse(urls[0])
            url_host = first_parsed.hostname
            url_port = first_parsed.port or 554
            if url_host and url_host != host:
                log.warning(f"  [{label or 'validator'}] URL host "
                            f"{url_host!r} != socket host {host!r} — "
                            f"validator will likely fail (caller bug)")
            if url_port != port:
                log.warning(f"  [{label or 'validator'}] URL port "
                            f"{url_port} != socket port {port} — "
                            f"validator will likely fail (caller bug)")
        except (ValueError, AttributeError):
            pass  # malformed URL — surfaces below as parse/probe failure

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)

        def roundtrip(method: str, cseq: int,
                      extra: dict | None = None,
                      uri: str = "") -> str:
            extra = extra or {}
            hdr = "".join(k + ": " + v + CRLF for k, v in extra.items())
            req = method + " " + uri + " RTSP/1.0" + CRLF
            req += "CSeq: " + str(cseq) + CRLF + hdr + CRLF
            sock.sendall(req.encode())
            buf = b""
            while CRLFCRLF.encode() not in buf and len(buf) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            text = buf.decode("utf-8", errors="replace")
            cl = 0
            for line in text.split(CRLF):
                if line.lower().startswith("content-length:"):
                    try:
                        cl = int(line.split(":", 1)[1].strip())
                    except (ValueError, IndexError):
                        cl = 0
                    break
            if cl > 0:
                sep_idx   = text.find(CRLFCRLF)
                already   = (len(buf) - (sep_idx + 4)) if sep_idx >= 0 else 0
                remaining = max(0, cl - already)
                while remaining > 0:
                    chunk = sock.recv(min(remaining, 4096))
                    if not chunk:
                        break
                    buf       += chunk
                    remaining -= len(chunk)
            return buf.decode("utf-8", errors="replace")

        for url_idx, rtsp_url in enumerate(urls):
            _log(f"({url_idx+1}/{len(urls)}) validating {_strip_creds(rtsp_url)}")

            # ── OPTIONS ──────────────────────────────────────────────
            try:
                resp = roundtrip("OPTIONS", next_cseq, uri=rtsp_url)
                next_cseq += 1
            except Exception as e:
                _log(f"OPTIONS exception → bailing remaining: {e}")
                # 2.4.0-rc3.5 ACD: see _probe_rtsp_paths_single_socket.
                # 2.5.0-rc1.4: callers can suppress ACD recording via
                # `walker_skip_acd` on host_meta. Used by the channel
                # enumeration helper, which loops over URLs one socket
                # at a time on the Lorex/Dahua family — a firmware
                # quirk where the DVR closes the socket after each
                # full authenticated transaction. Without the suppress
                # flag, every per-URL walk that completes successfully
                # but hits a server-side close on the next OPTIONS
                # would record an RST event, and 2 events in <60s
                # trigger ACD escalation. That's spurious for the
                # known socket-close-per-URL behavior of this brand.
                _skip_acd = bool(host_meta and host_meta.get(
                    "walker_skip_acd"))
                if not _skip_acd:
                    _record_rst_observation(host)
                return results  # remaining urls stay False
            if resp.startswith("RTSP/") and not captured_server:
                for _line in resp.split(CRLF):
                    if _line.lower().startswith("server:"):
                        captured_server = _line.split(":", 1)[1].strip()
                        _log(f"captured Server: {captured_server!r}")
                        break
            if "RTSP/1.0 2" not in resp:
                # 2.5.0-rc1.2: same OPTIONS-401-fall-through fix that
                # _probe_rtsp_paths_single_socket got in 2.5.0-rc1.1.
                # When OPTIONS returns 401 with WWW-Authenticate AND we
                # have credentials, capture the auth challenge into
                # auth_val and DON'T skip — fall through to DESCRIBE
                # so the existing DESCRIBE-401-retry-with-Digest path
                # can authenticate. The discovery walker had this fix
                # in rc1.1 but the validate walker (used to confirm
                # sub-stream URLs after the discovery walker found a
                # working main stream) was missed. Symptom on the Lorex DVR
                # rc1.1 field log: db_probe walked sub-stream URLs,
                # OPTIONS-401'd on each, skipped — `stream_profiles
                # built 1 entry/entries (1 main + 0 sub + 0 validated
                # locked)` even though the sub-stream URLs were valid.
                _is_opts_401 = "RTSP/1.0 401" in resp
                _has_creds   = bool(username or password)
                if _is_opts_401 and _has_creds:
                    if not auth_val:
                        _opts_auth_line = next(
                            (l for l in resp.splitlines()
                             if l.lower().startswith("www-authenticate:")),
                            "")
                        auth_val = (
                            _opts_auth_line.split(":", 1)[-1].strip()
                            if _opts_auth_line else "")
                    if auth_val:
                        _log(f"OPTIONS → 401 with auth challenge captured "
                             f"— falling through to DESCRIBE for {_strip_creds(rtsp_url)}")
                        # Fall through to DESCRIBE; auth retry uses
                        # the captured auth_val.
                    else:
                        _log(f"OPTIONS → 401 with no WWW-Authenticate "
                             f"— skipping URL")
                        continue
                else:
                    _log(f"OPTIONS → {resp.split(CRLF)[0].strip()!r} "
                         f"— skipping URL")
                    continue

            # ── DESCRIBE (with optional 401 retry) ───────────────────
            try:
                # If we already captured auth_val from a prior URL's 401,
                # send it pre-emptively to avoid a second roundtrip.
                describe_extra: dict = {"Accept": "application/sdp"}
                if auth_val:
                    ah = _build_auth(auth_val, "DESCRIBE", rtsp_url)
                    if ah:
                        describe_extra["Authorization"] = ah
                resp = roundtrip("DESCRIBE", next_cseq, describe_extra,
                                 uri=rtsp_url)
                next_cseq += 1
            except Exception as e:
                _log(f"DESCRIBE exception → bailing: {e}")
                return results
            if "RTSP/1.0 200" in resp:
                pass
            elif "401" in resp:
                if not username:
                    _log(f"DESCRIBE → 401 (no creds) — skipping {_strip_creds(rtsp_url)}")
                    continue
                if not auth_val:
                    auth_line = next(
                        (l for l in resp.splitlines()
                         if l.lower().startswith("www-authenticate:")), "")
                    auth_val = auth_line.split(":", 1)[-1].strip() if auth_line else ""
                if not auth_val:
                    _log("DESCRIBE → 401 with no WWW-Authenticate — skipping")
                    continue
                auth_hdr = _build_auth(auth_val, "DESCRIBE", rtsp_url)
                if not auth_hdr:
                    _log(f"DESCRIBE → unparseable auth: {auth_val[:60]!r}")
                    continue
                try:
                    resp = roundtrip("DESCRIBE", next_cseq,
                                     {"Accept": "application/sdp",
                                      "Authorization": auth_hdr},
                                     uri=rtsp_url)
                    next_cseq += 1
                except Exception as e:
                    _log(f"DESCRIBE-auth exception → bailing: {e}")
                    return results
                if "RTSP/1.0 200" not in resp:
                    _log(f"DESCRIBE-auth → {resp.split(CRLF)[0].strip()!r}")
                    continue
            else:
                _log(f"DESCRIBE → {resp.split(CRLF)[0].strip()!r} — skipping")
                continue

            # ── Parse SDP for first m=video track URL ───────────────
            body_idx  = resp.find(CRLFCRLF)
            sdp_text  = resp[body_idx + 4:] if body_idx >= 0 else ""
            track_url = _parse_track_url(sdp_text, rtsp_url)
            if not track_url:
                _log(f"DESCRIBE 200 but SDP has no m=video — skipping {_strip_creds(rtsp_url)}")
                continue

            # 2.5.0-rc1.2: stricter populated-channel test, used by the
            # post-cred-auth channel-enumeration helper to filter out
            # virtual/empty DVR channel slots that return SDP with
            # `m=video` but no real codec rtpmap. Same heuristic the
            # discovery walker has had since 2.5.0-rc1.0; ported here
            # so post-auth multi-card surfacing on channel_iterate
            # brands skips empty channels rather than registering
            # cards for them. host_meta-driven so probes that don't
            # opt in (single-camera ONVIF profile validation) get the
            # original behaviour.
            _populated_test = ""
            if host_meta:
                _populated_test = str(host_meta.get(
                    "walker_populated_channel_test", "") or "")
            if _populated_test == "sdp_has_video_track":
                if not _sdp_has_video_track(sdp_text):
                    _log(f"DESCRIBE 200 but SDP failed populated-channel "
                         f"test (no real video codec rtpmap) — likely "
                         f"empty channel; skipping {rtsp_url}")
                    continue

            # ── SETUP: TCP-interleaved first, UDP fallback ──────────
            transports = [
                ("RTP/AVP/TCP;unicast;interleaved=0-1",            "TCP"),
                ("RTP/AVP/UDP;unicast;client_port=50000-50001",    "UDP"),
            ]
            setup_ok = False
            session  = ""
            for transport_hdr, tlabel in transports:
                extra: dict = {"Transport": transport_hdr}
                if auth_val:
                    ah = _build_auth(auth_val, "SETUP", track_url)
                    if ah:
                        extra["Authorization"] = ah
                try:
                    resp = roundtrip("SETUP", next_cseq, extra, uri=track_url)
                    next_cseq += 1
                except Exception as e:
                    _log(f"SETUP exception → bailing: {e}")
                    return results
                if "RTSP/1.0 200" in resp:
                    for line in resp.split(CRLF):
                        if line.lower().startswith("session:"):
                            session = (line.split(":", 1)[1]
                                           .strip().split(";")[0].strip())
                            break
                    _log(f"SETUP ({tlabel}) → 200 OK")
                    setup_ok = True
                    break
                _log(f"SETUP ({tlabel}) → {resp.split(CRLF)[0].strip()!r}")

            if not setup_ok:
                continue

            # ── TEARDOWN — release session before next URL ──────────
            td_extra: dict = {}
            if session:
                td_extra["Session"] = session
            if auth_val:
                ah = _build_auth(auth_val, "TEARDOWN", track_url)
                if ah:
                    td_extra["Authorization"] = ah
            try:
                roundtrip("TEARDOWN", next_cseq, td_extra, uri=track_url)
                next_cseq += 1
            except Exception:
                pass  # TEARDOWN failure is non-fatal

            results[rtsp_url] = True
            _log(f"  → probe_ok=True for {_strip_creds(rtsp_url)}")

        return results

    except Exception as e:
        _log(f"validate-all walk exception: {e}")
        return results
    finally:
        if host_meta is not None and captured_server:
            host_meta["server_header"] = captured_server
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def find_rtsp_path(ip: str, port: int,
                   username: str = "", password: str = "",
                   host_meta: dict | None = None) -> str | None:
    """rc2 Two-layer RTSP path probe with brand-aware short-circuits.

    Layer 1 — Single-socket walk through all paths (always tried first
    unless the brand has throttle_type='no_rtsp_support').

    Layer 2 — Multi-socket fallback with 5s delay between attempts and
    bail-after-10 consecutive failures. Skipped for brands with
    throttle_type='rate_limit_per_ip_tcp' (Hipcam/Microseven family) —
    multi-socket would be RST'd before it could complete.

    host_meta — optional camera dict (mac_vendor, hostname, page_title,
    etc.) used to identify the brand BEFORE the probe begins. Without it,
    the function falls back to brand-agnostic two-layer behavior.
    """
    # ── Brand identification — first pass (uses signals already in
    #    host_meta: mac_vendor, page_title, server_header, nmap_product,
    #    onvif_scopes, hostname, verdict_reason). rc2.1.1: this is a
    #    DIRECT call to _identify_camera_brand which mutates host_meta in
    #    place, writing host_meta["manufacturer"] when a brand is found.
    #    Caller (e.g. ONVIF post-scan loop) reads it back after we return.
    #    Replaces rc2's _get_brand_throttle_info() which made a dict copy
    #    and silently dropped the manufacturer assignment.
    brand_entry: dict | None = None
    throttle_type: str = ""
    if host_meta is not None:
        try:
            brand_entry = _identify_camera_brand(host_meta)
        except Exception as e:
            log.debug(f"  brand-id (pre-probe): {e}")
        if brand_entry:
            throttle_type = str(brand_entry.get("throttle_type", "") or "")
    brand_name = (brand_entry or {}).get("name", "")

    # Cloud-only brands: skip RTSP entirely
    if throttle_type == "no_rtsp_support":
        log.info(f"  RTSP skipped: {brand_name} does not support RTSP "
                 f"(throttle_type=no_rtsp_support)")
        return None

    # Adjust per-socket timeout for sleeping/slow-wakeup brands
    sock_timeout: float = 6.0
    if throttle_type == "session_time_cap":
        sock_timeout = 25.0  # Reolink battery-WiFi wake-up window
        log.info(f"  RTSP timeout extended to {sock_timeout}s "
                 f"({brand_name}, session_time_cap)")

    # Compose path priority — brand-specific paths from STREAM_DB first,
    # then the universal RTSP_PATHS, deduped while preserving order.
    db_paths: list[str] = []
    if host_meta is not None and brand_name:
        cam_with_brand = dict(host_meta)
        cam_with_brand["manufacturer"] = brand_name
        sdb = _match_stream_db(cam_with_brand)
        if sdb:
            db_paths = list(sdb.get("rtsp", []))

    # 2.5.0-rc1.0: streaming_recipe consumer. Brands with
    # `streaming_recipe.type == "channel_iterate"` (Lorex/Dahua DVR-NVR
    # family, Hikvision NVR, Uniview NVR, Dahua direct, Amcrest, etc.)
    # need DVR-channel-specific paths, not the universal single-camera
    # paths. Expand the recipe and prepend it so channel iteration
    # happens BEFORE generic fallbacks. The fallback paths from the
    # recipe (legacy firmware URLs) come last in the recipe list itself,
    # see _expand_channel_iterate_paths.
    recipe_paths: list[str] = []
    recipe = (brand_entry or {}).get("streaming_recipe") or {}
    if recipe.get("type") == "channel_iterate":
        recipe_paths = _expand_channel_iterate_paths(recipe, channel_cap=16)
        if recipe_paths:
            log.info(f"  RTSP path list: brand={brand_name} streaming_recipe "
                     f"channel_iterate expanded to {len(recipe_paths)} paths "
                     f"(channels capped at 16)")
            # Tell the walker this is a channel-iteration walk so it can
            # apply the populated-channel SDP heuristic and the
            # auth_attempt_lockout bail-on-first-failure policy if either
            # is configured on the brand entry.
            if host_meta is not None:
                host_meta["walker_streaming_recipe_active"] = True
                host_meta["walker_populated_channel_test"] = (
                    recipe.get("populated_channel_test", "")
                )
                host_meta["walker_throttle_type"] = throttle_type

    seen: set[str]   = set()
    ordered: list[str] = []
    for p in recipe_paths + db_paths + RTSP_PATHS:
        if p not in seen:
            seen.add(p)
            ordered.append(p)

    # ── Layer 1: single-socket walk ──────────────────────────────────
    label_for_log = f"{ip}:{port}"
    if brand_name:
        label_for_log += f" ({brand_name})"
    log.info(f"  RTSP probe: Layer 1 (single-socket walk, "
             f"{len(ordered)} paths) — {label_for_log}")

    # 2.4.0-rc2.0 (Layered Stream Discovery): enable locked-stream
    # collection when:
    #  • no credentials are supplied to this call (creds-already-known
    #    means cred-auth flow handles enumeration directly)
    #  • brand is NOT marked skip_layer2 — skip_layer2 brands have known
    #    fragile multi-attempt behavior (per-IP TCP rate-limit, lockout
    #    counters); we don't grind extra DESCRIBEs against them
    #  • host_meta has a captured rtsp_auth_realm — without one we
    #    can't filter by realm; safer to skip collection than surface
    #    locked streams that need different creds
    skip_layer2_brand = bool(
        brand_entry and brand_entry.get("skip_layer2", False)
    )
    captured_realm = ""
    if host_meta is not None:
        captured_realm = str(host_meta.get("rtsp_auth_realm", "") or "").strip()
    enable_locked_collect = bool(
        (not username)
        and (not skip_layer2_brand)
        and captured_realm
    )
    if enable_locked_collect:
        log.info(f"  RTSP probe: Layered Stream Discovery enabled "
                 f"(realm={captured_realm!r})")

    found, looks_like_rtsp = _probe_rtsp_paths_single_socket(
        ip, port, ordered, username, password,
        timeout=sock_timeout, label="", host_meta=host_meta,
        collect_locked=enable_locked_collect,
        expected_realm=captured_realm,
        # 2.4.0-rc2.6: when we have a brand recipe, pass its paths so
        # the walker can filter locked candidates to paths the brand
        # actually serves. db_paths was already computed above from
        # _match_stream_db. Empty list → no filter (backward-compat).
        brand_recipe_paths=db_paths,
    )
    # rc2.1.1: re-run brand identification after the walk. The walker
    # captured the RTSP Server: header into host_meta["server_header"],
    # which is a strong identification signal especially for cameras
    # whose mac_vendor was unavailable (e.g. Microseven — its TCP
    # rate-limit prevented inclusion in nmap_results, so OUI lookup
    # had nothing to feed). The Hipcam RealServer firmware family
    # always returns `Server: Hipcam RealServer/V1.0` which matches
    # the http_headers field in the Hipcam/Microseven CAMERA_DB entry.
    if host_meta is not None and not brand_name and host_meta.get("server_header"):
        try:
            re_id = _identify_camera_brand(host_meta, force=True)
            if re_id and re_id["name"] != "Generic IP Camera":
                brand_entry = re_id
                brand_name = re_id["name"]
                throttle_type = str(re_id.get("throttle_type", "") or "")
                log.info(f"  Brand identified post-walk: {brand_name} "
                         f"(via Server header: "
                         f"{host_meta.get('server_header','')!r})")
        except Exception as e:
            log.debug(f"  brand-id (post-walk): {e}")
    if found:
        log.info(f"  RTSP OK (Layer 1): {found}")
        return found

    # ── Layer 1 fallback: Axis Companion query-param retry ───────────
    # Only triggers when the brand DB explicitly says this camera needs
    # the query param. Cheap to attempt; one extra single-socket pass.
    if throttle_type == "requires_query_param":
        log.info(f"  RTSP probe: Layer 1 retry with Axis-Orig-Sw=true "
                 f"({brand_name}, requires_query_param)")
        found, lr2 = _probe_rtsp_paths_single_socket(
            ip, port, ordered, username, password,
            timeout=sock_timeout, extra_query="?Axis-Orig-Sw=true",
            label="", host_meta=host_meta,
        )
        looks_like_rtsp = looks_like_rtsp or lr2
        if found:
            log.info(f"  RTSP OK (Layer 1+query): {found}")
            return found

    # ── Layer 2 short-circuit: per-IP TCP rate-limit ─────────────────
    if throttle_type == "rate_limit_per_ip_tcp":
        log.info(f"  RTSP Layer 2 skipped: {brand_name} has per-IP TCP "
                 f"rate-limit (multi-socket would be RST'd)")
        return None

    # ── 2.4.0-rc2.1: Layer 2 short-circuit on skip_layer2 brands ─────
    # Brands with skip_layer2=True are documented as having fragile
    # multi-attempt behavior — typically lockout counters or per-stream
    # session caps that punish repeated DESCRIBE attempts. Lorex/Dahua
    # DVR-NVR family is the canonical example: Layer 2 grinds 50+ seconds
    # through 10 sockets × 5s sleep on a multi-channel DVR where the
    # right answer is "use the streaming_recipe with channel iteration"
    # (consumed in rc3.x), not "try more single-channel paths."
    # 2.4.0-rc2.9: also honor host_meta["host_skip_layer2"], propagated
    # by run_scan when an EARLIER port on this IP triggered the skip.
    # Lorex/Dahua case: port 554 IDs as "Lorex / Dahua DVR-NVR Family"
    # (skip_layer2: True), but port 80 IDs as plain "Lorex" (no
    # skip_layer2). Without inheritance, port 80 would run Layer 2
    # for ~45s wastefully on an IP we already know can't speak it.
    inherited_skip_layer2 = bool(
        host_meta and host_meta.get("host_skip_layer2"))
    if skip_layer2_brand or inherited_skip_layer2:
        if skip_layer2_brand:
            log.info(f"  RTSP Layer 2 skipped: {brand_name} marked "
                     f"skip_layer2 (use streaming_recipe / channel iteration)")
            # Mark host_meta so run_scan can propagate to alt ports
            if host_meta is not None:
                host_meta["brand_skip_layer2"] = True
        else:
            log.info(f"  RTSP Layer 2 skipped: {ip} inherited "
                     f"skip_layer2 from earlier port on this host")
        return None

    # ── 2.4.0-rc2.4: Layer 2 short-circuit on Layer 1 early-bail ────
    # If Layer 1 hit EARLY_BAIL_THRESHOLD consecutive 401s with the
    # same realm and bailed out (state was written to host_meta in
    # _probe_rtsp_paths_single_socket's finally block), Layer 2's
    # multi-socket walk will get the same 401 with the same realm on
    # every fresh socket — auth is enforced server-side, not socket-
    # side, per RFC 7235 §2.2. Skip Layer 2 immediately and let the
    # camera surface as needs_credentials. Users who want to verify
    # against firmware-quirk cases (5% chance Layer 2 reveals
    # something Layer 1 missed) can hit the per-card "Deep Re-Probe"
    # button which re-runs Layer 2 on demand AND resumes the Layer 1
    # walk on the unwalked remaining paths.
    if (host_meta is not None
            and host_meta.get("early_bail_reason") == "layer1_consecutive_401s"):
        realm = host_meta.get("early_bail_realm", "")
        log.info(f"  RTSP Layer 2 skipped: Layer 1 early-bailed after "
                 f"5 consecutive 401s with realm={realm!r} — fresh sockets "
                 f"won't change auth result. Use Deep Re-Probe button to "
                 f"override.")
        # Mark that we ALSO skipped Layer 2 so the Deep Re-Probe button
        # knows to run Layer 2 in addition to resuming Layer 1.
        host_meta["early_bail_reason"] = "layer1_then_layer2_skipped_401s"
        return None

    # ── rc2.1: Layer 2 fast-bail — host doesn't speak RTSP at all ───
    # If Layer 1 walked every path and got NO RTSP-formatted responses
    # (server is HTTP, raw TCP, or otherwise non-RTSP), Layer 2 will
    # waste 50+s grinding through 10 sockets × 5s sleep before its own
    # bail kicks in. Skip it. Common when a Hikvision NVR is probed on
    # port 80 (HTTP) instead of 554 (RTSP) by the cred-relogin flow.
    if not looks_like_rtsp:
        log.info(f"  RTSP Layer 2 skipped: {ip}:{port} did not respond "
                 f"with RTSP format on any path — likely wrong port or "
                 f"non-RTSP service")
        return None

    # ── Layer 2: multi-socket fallback with 5s delay + bail-after-10 ─
    log.info(f"  RTSP probe: Layer 2 (multi-socket fallback, "
             f"5s delay + bail-after-10) — {label_for_log}")
    consecutive_failures = 0
    for i, path in enumerate(ordered):
        if i > 0:
            time.sleep(5.0)   # cooldown between sockets

        url = f"rtsp://{ip}:{port}{path}"
        if probe_rtsp(url, username, password, timeout=sock_timeout):
            log.info(f"  RTSP OK (Layer 2): {url}")
            return url

        # Axis Companion retry-on-failure with query param
        if throttle_type == "requires_query_param":
            url_q = f"rtsp://{ip}:{port}{path}?Axis-Orig-Sw=true"
            if probe_rtsp(url_q, username, password, timeout=sock_timeout):
                log.info(f"  RTSP OK (Layer 2+query): {url_q}")
                return url_q

        consecutive_failures += 1
        if consecutive_failures >= 10:
            log.info(f"  RTSP Layer 2 bailing after "
                     f"{consecutive_failures} consecutive failures")
            break

    return None


def probe_mjpeg_http(ip: str, port: int, username: str = "",
                     password: str = "", timeout: int = 4) -> str | None:
    import urllib.request
    scheme = "https" if port in (443, 8443) else "http"
    auth   = (f"Basic {base64.b64encode(f'{username}:{password}'.encode()).decode()}"
              if username else None)
    for path in MJPEG_PATHS:
        url = f"{scheme}://{ip}:{port}{path}"
        try:
            req = urllib.request.Request(url)
            if auth:
                req.add_header("Authorization", auth)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ct = resp.headers.get("Content-Type", "")
                if any(k in ct.lower() for k in
                       ("multipart/x-mixed-replace", "image/jpeg", "mjpeg", "mjpg")):
                    log.info(f"  MJPEG OK: {url}")
                    return url
        except Exception:
            pass
    return None


def probe_hls(ip: str, port: int, username: str = "",
              password: str = "", timeout: int = 4) -> str | None:
    import urllib.request
    scheme = "https" if port in (443, 8443) else "http"
    auth   = (f"Basic {base64.b64encode(f'{username}:{password}'.encode()).decode()}"
              if username else None)
    for path in HLS_PATHS:
        url = f"{scheme}://{ip}:{port}{path}"
        try:
            req = urllib.request.Request(url)
            if auth:
                req.add_header("Authorization", auth)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ct   = resp.headers.get("Content-Type", "")
                body = resp.read(64).decode("utf-8", errors="replace")
                if ("m3u8" in ct.lower() or "mpegurl" in ct.lower() or
                        body.strip().startswith("#EXTM3U")):
                    log.info(f"  HLS OK: {url}")
                    return url
        except Exception:
            pass
    return None


def probe_rtmp(ip: str, port: int, timeout: int = 3) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            sock.sendall(b"\x03")
            sock.settimeout(timeout)
            data = sock.recv(4)
            return bool(data and data[0] in (0x03, 0x06))
    except Exception:
        return False


def probe_webrtc(ip: str, port: int, timeout: int = 4) -> str | None:
    import urllib.request, urllib.error
    scheme = "https" if port in (443, 8443) else "http"
    sdp_offer = (
        "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
        "m=video 9 UDP/TLS/RTP/SAVPF 96\r\nc=IN IP4 0.0.0.0\r\n"
        "a=sendrecv\r\na=rtpmap:96 H264/90000\r\n"
    )
    for path in WEBRTC_PATHS:
        url = f"{scheme}://{ip}:{port}{path}"
        try:
            req = urllib.request.Request(url, data=sdp_offer.encode(), method="POST")
            req.add_header("Content-Type", "application/sdp")
            req.add_header("Accept",       "application/sdp")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status in (200, 201) and "sdp" in resp.headers.get("Content-Type","").lower():
                    return url
        except urllib.error.HTTPError as e:
            if any(h in e.headers.get("Content-Type","").lower() for h in ("sdp","webrtc","ice")):
                return url
        except Exception:
            pass
    return None


def probe_ws_rtsp(ip: str, port: int, timeout: int = 4) -> str | None:
    scheme_ws = "wss" if port in (443, 8443) else "ws"
    key_b64   = "dGhlIHNhbXBsZSBub25jZQ=="
    for path in WS_RTSP_PATHS:
        try:
            with socket.create_connection((ip, port), timeout=timeout) as sock:
                hs = (
                    f"GET {path} HTTP/1.1\r\nHost: {ip}:{port}\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key_b64}\r\nSec-WebSocket-Version: 13\r\n"
                    "Sec-WebSocket-Protocol: rtsp\r\n\r\n"
                ).encode()
                sock.sendall(hs)
                sock.settimeout(timeout)
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = sock.recv(1024)
                    if not chunk:
                        break
                    buf += chunk
                # 2.4.0-rc2.3: tightened verification. Per RFC 6455 §4.1,
                # the server MUST include the selected subprotocol in
                # `Sec-WebSocket-Protocol: <protocol>` in its 101
                # response if it accepted that subprotocol. A server
                # that returns 101 + "websocket" but does NOT echo
                # `rtsp` as its subprotocol speaks WebSocket but NOT
                # RTSP-over-WebSocket — common false positive case is
                # the Microseven Hipcam family which has a WebSocket
                # endpoint on port 80 for its live web UI MJPEG feed
                # but doesn't tunnel RTSP through it.
                resp = buf.decode("utf-8", errors="replace")
                # Status line check
                first_line = resp.split("\r\n", 1)[0] if resp else ""
                if "101" not in first_line:
                    continue
                # Header parse — case-insensitive lookup
                headers_lower = resp.lower()
                if "upgrade: websocket" not in headers_lower:
                    continue
                # MUST echo our requested rtsp subprotocol — anchored to
                # the actual header so we don't false-positive on the
                # word "rtsp" appearing elsewhere in body/comments.
                # Match: "sec-websocket-protocol: ..." line containing rtsp
                import re as _re_ws
                m = _re_ws.search(
                    r'(?im)^\s*sec-websocket-protocol\s*:\s*([^\r\n]+)',
                    resp,
                )
                if not m:
                    continue
                accepted = m.group(1).lower()
                # Subprotocol value can be a comma list per RFC; tokens
                # are case-insensitive identifiers. Match exact token.
                tokens = [t.strip() for t in accepted.split(",")]
                if "rtsp" not in tokens:
                    continue
                return f"{scheme_ws}://{ip}:{port}{path}"
        except Exception:
            pass
    return None


def probe_rtsp_options(ip: str, port: int, timeout: int = 3) -> bool:
    """
    Send RTSP OPTIONS via raw TCP and check for an RTSP response header.
    Works even when auth is required — a 401 is still camera-positive.
    This is the fastest and most reliable camera litmus test.
    Routers, printers, NAS devices do NOT speak RTSP and will close the
    connection or return HTTP/garbage.
    """
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            request = (
                f"OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\n"
                f"CSeq: 1\r\n"
                f"User-Agent: AnyCam/1.0\r\n"
                f"\r\n"
            )
            sock.sendall(request.encode())
            sock.settimeout(timeout)
            response = sock.recv(256).decode("utf-8", errors="replace")
            # Any RTSP response = camera
            if response.startswith("RTSP/"):
                log.info(f"  RTSP OPTIONS confirm: {ip}:{port} -> {response.split(chr(13))[0]}")
                return True
    except Exception:
        pass
    return False
# Additional paths to try for identity probing beyond root /
_IDENTITY_PATHS = [
    "/", "/index.html", "/index.htm", "/login.htm", "/login.html",
    "/web/", "/web/index.html", "/cgi-bin/main-cgi", "/view/index.shtml",
    "/live", "/admin/",
]


def _make_ssl_ctx() -> ssl.SSLContext:

    """SSL context that ignores self-signed certificates (common on cameras/NVRs)."""
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx


def _rtsp_options_fingerprint(
    host: str,
    port: int = 554,
    *,
    timeout: float = 3.0,
    path: str = "/",
) -> dict:
    """Open one TCP socket to host:port, send a single RTSP OPTIONS
    request, read the response, and return a dict of captured fingerprint
    fields.

    Returns dict with keys:
        status:           int|None      e.g. 200, 401, 404
        looks_like_rtsp:  bool          True if any line started with "RTSP/"
        server_header:    str|None      value of Server: header
        auth_scheme:      str|None      "Digest" | "Basic" | None
        auth_realm:       str|None      realm from WWW-Authenticate
        auth_algorithm:   str|None      algorithm from WWW-Authenticate
        public_methods:   list[str]     parsed from Public: header
        cseq:             str|None      echoed CSeq
        raw_response:     str           full response text (truncated to 4 KB)
        elapsed_ms:       float         wall-clock time of round-trip
        error:            str|None      populated only on socket-level failure

    Failure semantics:
      - Connection refused / timeout / RST: returns dict with `error`
        populated, `looks_like_rtsp=False`, all other fields None or empty.
      - Server speaks something else (HTTP, FTP): `looks_like_rtsp=False`,
        `status=None`, `raw_response` captures whatever was received.
      - Server speaks RTSP but returns 4xx: `looks_like_rtsp=True`,
        `status` set, `auth_*` fields populated if challenge present.
      - Server speaks RTSP and returns 200: full population including
        `public_methods`.

    Implementation notes:
      - Single TCP open + close. No retries. ~3s timeout.
      - No User-Agent header sent — keeps the request minimal.
      - Reads up to 4 KB or until "\\r\\n\\r\\n" or socket close.
      - Parses headers via simple line-split + ":" partition.
      - Multi-line continuation headers (RFC 822 folding) collapsed onto
        the previous header before parsing.

    Risk profile:
      - Zero risk to Hipcam-family rate-limited hosts: this IS the first
        TCP open, no preceding probe to collide with.
      - Zero risk to Lorex/Dahua DVR auth-lockout hosts: OPTIONS doesn't
        authenticate, just receives the 401 challenge. No counter increment.

    Added in 2.4.0-rc1.0 per RTSP_OPTIONS_Fingerprint_Helper_Plan.md.
    """
    import socket as _sock
    import re as _re_local
    import time as _time_local

    result: dict = {
        "status": None,
        "looks_like_rtsp": False,
        "server_header": None,
        "auth_scheme": None,
        "auth_realm": None,
        "auth_algorithm": None,
        "public_methods": [],
        "cseq": None,
        "raw_response": "",
        "elapsed_ms": 0.0,
        "error": None,
    }

    # Build the request. Use 'rtsp://host:port/path' as the request URI per
    # RFC 2326 §10. CSeq is mandatory per spec. No User-Agent — keeps the
    # request minimal and avoids any User-Agent-based filtering some
    # servers might do (per user request 2026-05-03).
    request_uri = f"rtsp://{host}:{port}{path}"
    request_lines = [
        f"OPTIONS {request_uri} RTSP/1.0",
        "CSeq: 1",
        "",   # blank line terminating headers
        "",   # extra CRLF
    ]
    request_bytes = "\r\n".join(request_lines).encode("ascii", errors="replace")

    t0 = _time_local.monotonic()
    sock = None
    try:
        sock = _sock.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall(request_bytes)

        # Read up to 4 KB or until "\r\n\r\n" or socket close
        buf = b""
        max_bytes = 4096
        while len(buf) < max_bytes:
            try:
                chunk = sock.recv(min(1024, max_bytes - len(buf)))
            except _sock.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if b"\r\n\r\n" in buf:
                break

        result["elapsed_ms"] = (_time_local.monotonic() - t0) * 1000.0
        try:
            text = buf.decode("utf-8", errors="replace")
        except Exception:
            text = buf.decode("latin-1", errors="replace")
        result["raw_response"] = text[:4096]

    except (_sock.timeout, OSError) as exc:
        result["elapsed_ms"] = (_time_local.monotonic() - t0) * 1000.0
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    # ---- Parse the response ----
    text = result["raw_response"]
    if not text:
        result["error"] = "empty_response"
        return result

    # Split into lines on CRLF or LF
    lines = text.replace("\r\n", "\n").split("\n")

    # Status line: "RTSP/1.0 200 OK" or "HTTP/1.1 400 Bad Request" etc
    if lines:
        status_line = lines[0].strip()
        if status_line.startswith("RTSP/"):
            result["looks_like_rtsp"] = True
            # Parse status code
            parts = status_line.split(None, 2)
            if len(parts) >= 2:
                try:
                    result["status"] = int(parts[1])
                except ValueError:
                    pass
        # If it's not RTSP, we still capture the raw text but leave
        # looks_like_rtsp=False and status=None.

    # Collapse RFC 822 continuation lines (lines starting with whitespace
    # are continuations of the previous header)
    folded: list[str] = []
    for line in lines[1:]:
        if line.startswith((" ", "\t")) and folded:
            folded[-1] += " " + line.strip()
        else:
            folded.append(line)

    # Parse headers — simple ":"-split
    for line in folded:
        if not line.strip():
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key_l = key.strip().lower()
        value = value.strip()

        if key_l == "server":
            result["server_header"] = value
        elif key_l == "cseq":
            result["cseq"] = value
        elif key_l == "public":
            # Public: OPTIONS, DESCRIBE, SETUP, PLAY, ...
            methods = [m.strip().upper() for m in value.split(",") if m.strip()]
            result["public_methods"] = methods
        elif key_l == "www-authenticate":
            # "Digest realm=\"foo\", algorithm=MD5, nonce=..."
            # or "Basic realm=\"bar\""
            scheme_match = _re_local.match(r"^\s*(\w+)\s+", value)
            if scheme_match:
                result["auth_scheme"] = scheme_match.group(1)
            # realm="..." — handle escaped quotes inside
            realm_match = _re_local.search(
                r'realm\s*=\s*"((?:[^"\\]|\\.)*)"', value
            )
            if realm_match:
                # Unescape \" → "
                result["auth_realm"] = realm_match.group(1).replace('\\"', '"')
            # algorithm=MD5 (unquoted) or algorithm="SHA-256" (quoted)
            algo_match = _re_local.search(
                r'algorithm\s*=\s*("([^"]+)"|([\w\-]+))', value
            )
            if algo_match:
                result["auth_algorithm"] = algo_match.group(2) or algo_match.group(3)

    return result


# 3.0.0-rc1.3 (build plan B19): the line below was lost in 2.4.0-rc1.0, when
# _rtsp_options_fingerprint was added directly above it. The body stayed,
# unreachable, as the tail of that function, and both callers raised a name
# error. The body is unchanged from 2.3.x (117 lines, compared).
def probe_http_identity(ip: str, port: int, timeout: int = 5) -> dict:
    """
    Fetch HTTP pages from a device and extract identity info:
      - Page title, Server header
      - Manufacturer matched against CAMERA_DB
      - is_camera flag

    Key improvements over naive fetch:
      - SSL certificate errors ignored (cameras use self-signed certs)
      - Both http:// and https:// tried on every port
      - Multiple paths probed (/, /login.htm, /web/, etc.)
      - Script src attributes scanned — catches JS SPAs where the logo
        is an image but the manufacturer name appears in asset paths
        (e.g. Lorex's /flirLorex/js/... paths)
      - Up to 16KB of body read for better coverage
    """
    import urllib.request, urllib.error, re as _re, ssl as _ssl

    result = {
        "is_camera": False,
        "title": "", "server": "",
        "manufacturer": "", "notes": "", "raw_snippet": "",
    }

    GENERIC_CAM_BODY = [
        "camera", "ipcam", "webcam", "nvr", "dvr", "cctv",
        "onvif", "rtsp", "video", "stream", "live view",
        "network camera", "ip camera", "surveillance",
        "channel", "ptz", "pan tilt",
    ]

    ssl_ctx = _make_ssl_ctx()

    def _extract(body_bytes: bytes, headers_str: str, url: str) -> bool:
        """Returns True if a match was found (stop probing further paths)."""
        body = body_bytes.decode("utf-8", errors="replace")
        if not result["raw_snippet"]:
            result["raw_snippet"] = body[:500]

        # Page title
        if not result["title"]:
            m = _re.search(r"<title[^>]*>([^<]{1,120})</title>", body, _re.I)
            if m:
                result["title"] = m.group(1).strip()

        # Include script src paths — SPAs like Lorex put the manufacturer
        # name in asset paths (e.g. src="/flirLorex/js/...")
        # Extract all attribute values from the HTML
        attr_values = " ".join(_re.findall(r'(?:src|href|action)=["\']([^"\']{3,120})["\']',
                                           body, _re.I))

        combined = headers_str + " " + body[:16384] + " " + attr_values

        # CAMERA_DB match (rich, manufacturer-specific)
        entry = identify_manufacturer(combined)
        if entry and entry["name"] != "Generic IP Camera":
            result["manufacturer"] = entry["name"]
            result["notes"]        = entry["notes"]
            result["is_camera"]    = True
            log.info(f"  HTTP identity {url}: → {entry['name']}")
            return True

        # Generic camera keyword fallback
        combined_l = combined.lower()
        for kw in GENERIC_CAM_BODY:
            if kw in combined_l:
                result["is_camera"] = True
                if entry:
                    result["manufacturer"] = entry["name"]
                    result["notes"]        = entry["notes"]
                log.info(f"  HTTP camera keyword: {ip}:{port} ({kw})")
                return True

        return False

    def _fetch_and_extract(url: str) -> bool:
        """Fetch a URL (with SSL bypass) and run _extract. Returns True on match."""
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0 AnyCam/1.0")
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=ssl_ctx) as resp:
                server = resp.headers.get("Server", "")
                if not result["server"]:
                    result["server"] = server
                all_headers = str(resp.headers)
                body = resp.read(16384)
                return _extract(body, all_headers + " " + server, url)
        except urllib.error.HTTPError as e:
            # 401/403: headers may still identify the device
            server = e.headers.get("Server", "")
            if not result["server"]:
                result["server"] = server
            all_headers = str(e.headers)
            try:
                body = e.read(16384)
            except Exception:
                body = b""
            return _extract(body, all_headers + " " + server, url)
        except Exception:
            return False

    # Try both schemes; cameras often redirect http→https
    schemes = []
    if port in (443, 8443):
        schemes = ["https"]
    elif port in (80, 8080, 8000, 8888):
        schemes = ["http", "https"]
    else:
        schemes = ["http", "https"]

    for scheme in schemes:
        for path in _IDENTITY_PATHS:
            url = f"{scheme}://{ip}:{port}{path}"
            if _fetch_and_extract(url):
                return result  # found a match — stop

    return result


def probe_http_for_camera(ip: str, port: int, timeout: int = 4) -> bool:
    """Thin wrapper — returns True if probe_http_identity says is_camera."""
    return probe_http_identity(ip, port, timeout).get("is_camera", False)
# Quick probe path lists — shorter than the full probers, used only for
# the is_camera_positive gate check where speed matters more than coverage.
_QUICK_MJPEG_PATHS = ["/video", "/mjpeg", "/stream", "/mjpg/video.mjpg",
                      "/cgi-bin/mjpg/video.cgi", "/videostream.cgi"]
_QUICK_HLS_PATHS   = ["/index.m3u8", "/stream.m3u8", "/live.m3u8",
                      "/hls/stream.m3u8", "/live/stream.m3u8"]


def probe_mjpeg_quick(ip: str, port: int, timeout: int = 3) -> bool:
    """
    Check a handful of common MJPEG paths for multipart/x-mixed-replace
    or image/jpeg Content-Type.  Used as a fast gate check; the full
    probe_mjpeg_http() runs later if this passes.
    """
    import urllib.request
    scheme = "https" if port in (443, 8443) else "http"
    for path in _QUICK_MJPEG_PATHS:
        url = f"{scheme}://{ip}:{port}{path}"
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "AnyCam/1.0")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ct = resp.headers.get("Content-Type", "").lower()
                if any(k in ct for k in ("multipart/x-mixed-replace",
                                         "image/jpeg", "mjpeg", "mjpg")):
                    log.info(f"  MJPEG gate confirm: {ip}:{port}{path}")
                    return True
        except Exception:
            pass
    return False


def probe_hls_quick(ip: str, port: int, timeout: int = 3) -> bool:
    """
    Check a handful of common HLS paths for an M3U8 playlist response
    (#EXTM3U header or mpegurl Content-Type).  Used as a fast gate check.
    """
    import urllib.request
    scheme = "https" if port in (443, 8443) else "http"
    for path in _QUICK_HLS_PATHS:
        url = f"{scheme}://{ip}:{port}{path}"
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "AnyCam/1.0")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ct   = resp.headers.get("Content-Type", "").lower()
                body = resp.read(32).decode("utf-8", errors="replace")
                if ("mpegurl" in ct or "m3u8" in ct or
                        body.strip().startswith("#EXTM3U")):
                    log.info(f"  HLS gate confirm: {ip}:{port}{path}")
                    return True
        except Exception:
            pass
    return False


def is_camera_positive(ip: str, port: int, service: str, product: str,
                        verdict: str, onvif_ips: set, ssdp_cam_ips: set,
                        mdns_ips: set, mac_addr: str = "") -> bool:
    """
    Gate function: returns True only if at least one active probe or
    multicast discovery confirms this device is likely a camera.

    Probes run in priority order — fastest / most definitive first,
    slower / less certain probes only tried if earlier ones fail.

    Protocol coverage:
      RTSP    — raw OPTIONS handshake (~100ms, definitive, works through auth)
      RTMP    — C0 handshake byte check (~50ms, definitive)
      MJPEG   — Content-Type multipart/x-mixed-replace on common paths (~200ms)
      HLS     — #EXTM3U body or mpegurl Content-Type on common paths (~200ms)
      WebRTC  — WHEP POST + heuristic GET on signaling paths (~300ms)
      WS-RTSP — WebSocket upgrade with Sec-WebSocket-Protocol: rtsp (~200ms)
      HTTP    — full page body + header scan for camera strings (~500ms)
      ONVIF   — already confirmed by multicast Stage 1 (instant)
      SSDP    — already confirmed by multicast Stage 1 (instant)
      mDNS    — already confirmed by multicast Stage 1 (instant)
      DVR     — ports 37777/34567 assumed positive by definition
    """
    # ── 1. Multicast-confirmed (instant, already done in Stage 1) ──────────
    if ip in onvif_ips or ip in ssdp_cam_ips or ip in mdns_ips:
        return True

    # ── 1b. OUI camera-positive (MAC address manufacturer lookup) ─────────
    if mac_addr:
        oui_result = oui_is_camera(mac_addr)
        if oui_result is True:
            log.info(f"  OUI camera confirm: {ip} MAC {mac_addr} → {lookup_oui(mac_addr)}")
            return True
        if oui_result is False:
            log.info(f"  OUI non-camera reject: {ip} MAC {mac_addr} → {lookup_oui(mac_addr)}")
            return False

    # ── 1c. Respect nmap not_camera verdict ────────────────────────────────
    # If nmap's service/product banner identified this as a non-camera device
    # (printer, router, NAS, etc.) AND OUI didn't confirm it's a camera,
    # skip probing entirely.  A camera that somehow has a generic service
    # banner would still be found via ONVIF/SSDP/mDNS in Stage 1.
    if verdict == "not_camera":
        log.info(f"  nmap verdict reject: {ip}:{port} — not_camera verdict")
        return False

    # ── 2. RTSP OPTIONS — raw TCP, ~100ms, works through auth ─────────────
    if port in (554, 8554, 10554, 2020, 8765):
        if probe_rtsp_options(ip, port):
            return True
        # Fall through: camera may have broken RTSP but working web UI

    # ── 3. RTMP C0 handshake — ~50ms, definitively identifies RTMP server ─
    if port in (1935, 1936):
        if probe_rtmp(ip, port):
            log.info(f"  RTMP gate confirm: {ip}:{port}")
            return True

    # ── 4. DVR ports — Dahua (37777) and generic DVR (34567) ──────────────
    if port in (37777, 34567):
        return True

    # ── 5–8. HTTP-family probes (all run on HTTP/HTTPS ports) ──────────────
    if port in (80, 8080, 8000, 8888, 443, 8443):

        # 5. MJPEG Content-Type check — fast, definitive for MJPEG cameras
        if probe_mjpeg_quick(ip, port):
            return True

        # 6. HLS M3U8 check — fast, definitive for HLS cameras/NVRs
        if probe_hls_quick(ip, port):
            return True

        # 7. WebRTC WHEP probe — POST SDP offer, look for SDP answer or hints
        if probe_webrtc(ip, port):
            log.info(f"  WebRTC gate confirm: {ip}:{port}")
            return True

        # 8. HTTP body/header content scan — broadest net, catches web UIs
        if probe_http_for_camera(ip, port):
            return True

    # ── 9. WS-RTSP upgrade — try on any port not already covered ──────────
    #       go2rtc typically serves on 8554, mediamtx on 8888 or custom;
    #       we try after the port-specific checks above.
    if probe_ws_rtsp(ip, port):
        log.info(f"  WS-RTSP gate confirm: {ip}:{port}")
        return True

    # ── 10. nmap banner: check against CAMERA_DB (much richer than CAMERA_KEYWORDS)
    combined = (service + " " + product).lower()
    if combined.strip():
        if identify_manufacturer(combined) is not None:
            log.info(f"  nmap DB match: {ip}:{port} — {combined.strip()}")
            return True
        # Fallback to simple keyword list
        if any(k in combined for k in CAMERA_KEYWORDS):
            return True

    # ── 11. DB alias check against hostname ────────────────────────────────
    # Sometimes the device hostname itself contains a manufacturer name
    # (e.g. "lorex-nvr.local", "hikvision-123.lan")
    if any(k in combined for k in _DB_MANUFACTURERS):
        return True

    return False


def _onvif_soap(url: str, body: str,
                username: str = "", password: str = "", timeout: int = 6) -> str | None:
    import urllib.request
    security = ""
    if username:
        nonce_raw = os.urandom(16)
        nonce_b64 = base64.b64encode(nonce_raw).decode()
        created   = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        # RFC 2617 WS-Security PasswordDigest: SHA1(nonce_raw || created_utf8 || password_utf8)
        digest = base64.b64encode(
            hashlib.sha1(
                nonce_raw + created.encode("utf-8") + password.encode("utf-8")
            ).digest()
        ).decode()
        _wsse = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
        _wssu = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
        security = (
            f'<s:Header>'
            f'<Security xmlns="{_wsse}" '
            f'xmlns:wsu="{_wssu}" '
            f's:mustUnderstand="1">'
            f'<wsu:Timestamp wsu:Id="TS-1">'
            f'<wsu:Created>{created}</wsu:Created>'
            f'</wsu:Timestamp>'
            f'<UsernameToken wsu:Id="UT-1">'
            f'<Username>{username}</Username>'
            f'<Password Type="{_wsse}#PasswordDigest">{digest}</Password>'
            f'<Nonce EncodingType="{_wsse}#Base64Binary">{nonce_b64}</Nonce>'
            f'<wsu:Created>{created}</wsu:Created>'
            f'</UsernameToken>'
            f'</Security>'
            f'</s:Header>'
        )
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
        ' xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"{security}<s:Body>{body}</s:Body></s:Envelope>"
    )
    # Try SOAP 1.2 first (application/soap+xml), then fall back to SOAP 1.1
    # (text/xml) for cameras like Hikvision that return HTTP 400 on SOAP 1.2.
    for content_type in ("application/soap+xml; charset=utf-8",
                         "text/xml; charset=utf-8"):
        try:
            req = urllib.request.Request(url, envelope.encode("utf-8"), method="POST")
            req.add_header("Content-Type", content_type)
            req.add_header("SOAPAction", '""')
            req.add_header("User-Agent", f"AnyCam/{CURRENT_VERSION}")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            err_str = str(e)
            if "400" in err_str and content_type.startswith("application/soap"):
                log.debug(f"ONVIF SOAP ({url}): SOAP 1.2 → 400, retrying with SOAP 1.1")
                continue   # retry with text/xml
            log.debug(f"ONVIF SOAP ({url}): {e}")
            return None
    return None


def onvif_get_profiles(onvif_url: str, username: str, password: str) -> list[dict]:
    """
    Parse ONVIF GetProfiles response.
    Returns profile list with resolution, video codec, and audio codec taken
    directly from VideoEncoderConfiguration / AudioEncoderConfiguration in the
    XML.  These are always present regardless of stream codec, so they are
    used as authoritative sources — probing is only needed for actual FPS
    (which ONVIF's FrameRateLimit doesn't accurately reflect).
    """
    xml = _onvif_soap(onvif_url, "<trt:GetProfiles/>", username, password)
    if not xml:
        return []
    profiles = []
    enc_map  = {"H264": "h264", "H265": "hevc", "JPEG": "mjpeg",
                "H264E": "h264", "MPEG4": "mpeg4"}
    try:
        root = ET.fromstring(xml)
        ns   = {"trt": "http://www.onvif.org/ver10/media/wsdl",
                "tt":  "http://www.onvif.org/ver10/schema"}
        for p in root.findall(".//trt:Profiles", ns):
            token = p.get("token", "")
            name_el = p.find("tt:Name", ns)
            name = name_el.text if name_el is not None else token
            if not token:
                continue

            # ── Video resolution + codec ──────────────────────────────────────
            onvif_w   = None
            onvif_h   = None
            onvif_enc = None
            vec = p.find("tt:VideoEncoderConfiguration", ns)
            if vec is not None:
                res_el = vec.find("tt:Resolution", ns)
                if res_el is not None:
                    try:
                        onvif_w = int(res_el.findtext("tt:Width",  namespaces=ns) or 0) or None
                        onvif_h = int(res_el.findtext("tt:Height", namespaces=ns) or 0) or None
                    except (ValueError, TypeError):
                        pass
                enc_el = vec.find("tt:Encoding", ns)
                if enc_el is not None and enc_el.text:
                    onvif_enc = enc_map.get(enc_el.text.upper(), enc_el.text.lower())

            # ── Audio codec ───────────────────────────────────────────────────
            onvif_audio = None
            aec = p.find("tt:AudioEncoderConfiguration", ns)
            if aec is not None:
                aenc_el = aec.find("tt:Encoding", ns)
                if aenc_el is not None and aenc_el.text:
                    onvif_audio = aenc_el.text.lower()  # e.g. "g711", "aac", "g726"

            profiles.append({
                "token": token, "name": name,
                "onvif_width":    onvif_w,
                "onvif_height":   onvif_h,
                "onvif_encoding": onvif_enc,
                "onvif_audio":    onvif_audio,
            })
    except Exception as e:
        log.debug(f"GetProfiles parse: {e}")
    return profiles


def onvif_get_stream_uri(onvif_url: str, token: str,
                         username: str, password: str) -> str | None:
    body = (
        f"<trt:GetStreamUri>"
        f"<trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
        f"<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
        f"<trt:ProfileToken>{token}</trt:ProfileToken>"
        f"</trt:GetStreamUri>"
    )
    xml = _onvif_soap(onvif_url, body, username, password)
    if not xml:
        return None
    try:
        root   = ET.fromstring(xml)
        uri_el = root.find(".//{http://www.onvif.org/ver10/schema}Uri")
        return uri_el.text.strip() if uri_el is not None else None
    except Exception:
        return None


def onvif_get_snapshot_uri(onvif_url: str, token: str,
                            username: str, password: str) -> str | None:
    """
    Call ONVIF GetSnapshotUri for the given profile token.
    Returns the snapshot HTTP URL, or None if the camera does not support it.
    Used as a secondary source when STREAM_DB has no snap entry.
    """
    body = (
        f"<trt:GetSnapshotUri>"
        f"<trt:ProfileToken>{token}</trt:ProfileToken>"
        f"</trt:GetSnapshotUri>"
    )
    xml = _onvif_soap(onvif_url, body, username, password)
    if not xml:
        return None
    try:
        root   = ET.fromstring(xml)
        uri_el = root.find(".//{http://www.onvif.org/ver10/schema}Uri")
        uri    = uri_el.text.strip() if uri_el is not None else None
        if uri and uri.startswith("http"):
            return uri
        return None
    except Exception as exc:
        log.debug(f"onvif_get_snapshot_uri parse error: {exc}")
        return None


def _onvif_media_url(ip: str, port: int, xaddrs: str) -> str:
    """
    Build the ONVIF media service URL from the XAddrs field.

    XAddrs can contain multiple space-separated URLs (e.g. one IPv4 and one
    IPv6 link-local address).  We pick the best single URL:
      1. Prefer plain http:// URLs with IPv4 addresses (no IPv6 link-local)
      2. Avoid IPv6 link-local addresses (fe80::...)
      3. Fall back to whatever is first if nothing else qualifies

    Without this, cameras advertising both IPv4 and IPv6 addrs produce a
    malformed URL like:
      http://10.0.0.33/onvif/media http://[fe80::...]/onvif/media
    which causes SOAP requests to fail with 0 profiles returned.
    """
    if xaddrs:
        # Split on whitespace — camera may advertise multiple addrs in one element
        candidates = xaddrs.split()
        chosen = None
        for c in candidates:
            c = c.strip()
            if not c.startswith("http"):
                continue
            # Skip IPv6 link-local (fe80::) — unreliable from HA container
            if "fe80" in c.lower() or "[" in c:
                continue
            chosen = c
            break
        if chosen is None:
            # Fallback: take first http:// URL regardless of address type
            for c in candidates:
                c = c.strip()
                if c.startswith("http"):
                    chosen = c
                    break
        if chosen is None:
            chosen = candidates[0].strip() if candidates else xaddrs
        return chosen.rstrip("/").replace("device_service", "media").replace("Device", "Media")
    scheme = "https" if port in (443, 8443) else "http"
    return f"{scheme}://{ip}:{port}/onvif/media"
