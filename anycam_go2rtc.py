"""go2rtc: the supervisor, stream registration, the WebSocket proxy and the card stream.

Moved out of camera_discovery.py in 3.0.0-rc1.2 (build plan E1); the function
bodies are unchanged. The focus engine (_focus_set_go2rtc, api_go2rtc_focus)
stays in camera_discovery.py, because it replaces values that file owns.

This file cannot import camera_discovery.py (see anycam_host.py). The names in
NEEDS are set on this module at start-up. camera_discovery.py reads this
module's own run-time values as anycam_go2rtc.NAME, never through an import
of the name: an imported name would keep the value from start-up.
"""
import aiohttp
import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
from aiohttp import web
from pathlib import Path
from urllib.parse import quote, unquote, urlparse, urlsplit

from anycam_probe import _validate_rtsp_urls_single_socket   # 3.7.4 (B6)

log = logging.getLogger("anycam")

# Taken from camera_discovery.py at start-up (anycam_host.bind).
NEEDS = (
    'CAMERAS', 'CARD_MAX_WIDTH', '_ANSI_ESCAPE_RE', '_THREAD_POOL',
    '_brand_throttle_seconds', '_dahua_sub_stream', '_identify_camera_brand',
    '_match_stream_db', '_mjpeg_source',
    '_streams_refresh',
    '_strip_creds', '_throttle_wait_if_needed', 'build_authenticated_url',
)
# 3.1.0 (B25): a card that cannot play live for one of these reasons may
# have out-of-date saved streams, so AnyCam reads them again.
_REFRESH_REASONS = ("no stream small enough", "no stream that can play live",
                    "cannot play as live video", "no stream URL")
GO2RTC_BIN             = Path("/usr/local/bin/go2rtc")
GO2RTC_API_HOST        = "127.0.0.1"
# Non-default ports. 1984 and 8555 are go2rtc's defaults, and another add-on
# on a host-networked HAOS box (Frigate, the go2rtc add-on) may hold them.
GO2RTC_API_PORT        = 28984
GO2RTC_WEBRTC_PORT     = 28555
# 3.3.0 (C4): go2rtc's RTSP server, for AnyCam's own ffmpeg jobs. It listens
# on 127.0.0.1 only, with a password that is new at each start (approved by
# CrystalHeeler, 2026-10-04). 3.7.4: go2rtc 1.9.14 does not ask programs on
# 127.0.0.1 for that password (internal/rtsp: the check is skipped for a
# loopback address), and this add-on uses the host's network, so other
# programs on the Pi can read the video through it (build plan B48). The
# ffmpeg copy of B47 relies on that skip to publish into go2rtc.
GO2RTC_RTSP_PORT       = 28554
GO2RTC_RTSP_USER       = "anycam"
_GO2RTC_RTSP_PASS      = secrets.token_urlsafe(24)
# 3.7.4 (B47): go2rtc's API asks every caller for a password that is new at
# each start, programs on 127.0.0.1 included (local_auth). The API can add
# stream sources, and with the ffmpeg module a source runs ffmpeg with its
# own arguments inside this full_access add-on; without the password any
# program on the Pi's network could do that, and read every camera's
# address and password from /api/streams.
GO2RTC_API_USER        = "anycam"
_GO2RTC_API_PASS       = secrets.token_urlsafe(24)
# A camera whose stream fails this many times in a row through go2rtc is
# opened directly by ffmpeg again, as before 3.3.0, until the add-on restarts.
RELAY_FAIL_LIMIT       = 3
_RELAY_FAILS: dict[str, int] = {}
GO2RTC_PLAYER_JS       = Path("/www/video-rtc.js")
GO2RTC_READY_TIMEOUT_S = 10.0
_GO2RTC_PROC: asyncio.subprocess.Process | None = None
_GO2RTC_READY = False
# Streams registered in the CURRENT go2rtc process, name -> source URL.
# Cleared whenever go2rtc restarts, because its streams live in memory only.
# Also the proxy allowlist: handle_go2rtc_ws forwards only these names.
_GO2RTC_STREAMS: dict[str, str] = {}
_GO2RTC_STREAM_CAM: dict[str, str] = {}   # stream name -> camera_id
_GO2RTC_PLAYER_BYTES: bytes | None = None


def _go2rtc_config() -> str:
    """Return go2rtc's config as inline JSON. JSON is valid YAML.

    The leading "{" makes go2rtc parse this as raw config, not a file path,
    which is what keeps camera passwords off disk. Every key is
    load-bearing; see the security model above.
    """
    return json.dumps({
        # 3.7.4 (B47): ffmpeg, for the copy source of _go2rtc_camera_src.
        # Sources come only from AnyCam (_go2rtc_register); the browser can
        # name only streams AnyCam registered (handle_go2rtc_ws).
        "app":    {"modules": ["api", "ws", "rtsp", "webrtc", "mp4", "ffmpeg"]},
        "ffmpeg": {"bin": "ffmpeg"},
        "api":    {"listen": f"{GO2RTC_API_HOST}:{GO2RTC_API_PORT}",
                   "username": GO2RTC_API_USER, "password": _GO2RTC_API_PASS,
                   "local_auth": True},
        "rtsp":   {"listen": f"{GO2RTC_API_HOST}:{GO2RTC_RTSP_PORT}",
                   "username": GO2RTC_RTSP_USER, "password": _GO2RTC_RTSP_PASS},
        "webrtc": {"listen": f":{GO2RTC_WEBRTC_PORT}"},
        # warn keeps routine per-request lines out of the addon log. Source
        # URLs can still appear in a warning; _go2rtc_log_pump strips creds.
        "log":    {"level": "warn"},
    }, separators=(",", ":"))


def _go2rtc_api_url(path: str) -> str:
    return f"http://{GO2RTC_API_HOST}:{GO2RTC_API_PORT}{path}"


def _go2rtc_session(**kw) -> aiohttp.ClientSession:
    """3.7.4 (B47): a client session that gives go2rtc's API its password."""
    return aiohttp.ClientSession(auth=aiohttp.BasicAuth(GO2RTC_API_USER, _GO2RTC_API_PASS), **kw)


async def _go2rtc_log_pump(proc: asyncio.subprocess.Process) -> None:
    """Forward go2rtc output into the addon log, credentials stripped."""
    if proc.stdout is None:
        return
    while True:
        line = await proc.stdout.readline()
        if not line:
            return
        text = _ANSI_ESCAPE_RE.sub("", line.decode("utf-8", "replace")).strip()
        if text:
            log.warning(f"go2rtc: {_strip_creds(text)}")


async def _go2rtc_wait_ready(proc: asyncio.subprocess.Process) -> bool:
    """Poll go2rtc's API until it answers, the process exits, or time runs out."""
    deadline = time.monotonic() + GO2RTC_READY_TIMEOUT_S
    timeout = aiohttp.ClientTimeout(total=2)
    async with _go2rtc_session(timeout=timeout) as session:
        while time.monotonic() < deadline:
            if proc.returncode is not None:
                return False
            try:
                async with session.get(_go2rtc_api_url("/api")) as resp:
                    if resp.status == 200:
                        return True
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(0.25)
    return False


async def _go2rtc_terminate(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM go2rtc, then SIGKILL it if it has not exited within 3 s."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=3)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        await proc.wait()
    except ProcessLookupError:
        pass


async def _go2rtc_supervisor() -> None:
    """Run go2rtc for the life of the addon and restart it when it exits.

    Backoff doubles from 2 s to a 60 s ceiling while go2rtc keeps dying
    early, and resets after any run longer than a minute. go2rtc exits at
    once when it cannot bind a port, so a clash with another add-on shows up
    here as a restart loop, with go2rtc's own bind error logged above it.
    """
    global _GO2RTC_PROC, _GO2RTC_READY
    if not GO2RTC_BIN.exists():
        log.warning(f"go2rtc: binary missing at {GO2RTC_BIN} — "
                    f"Enhanced View will use the classic JPEG path")
        return
    backoff = 2.0
    while True:
        _GO2RTC_STREAMS.clear()
        _GO2RTC_STREAM_CAM.clear()
        try:
            proc = await asyncio.create_subprocess_exec(
                str(GO2RTC_BIN), "-config", _go2rtc_config(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as ex:
            log.error(f"go2rtc: could not start ({ex}) — "
                      f"Enhanced View will use the classic JPEG path")
            return
        _GO2RTC_PROC = proc
        started = time.monotonic()
        pump = asyncio.create_task(_go2rtc_log_pump(proc))
        rc: int | None = None
        try:
            if await _go2rtc_wait_ready(proc):
                _GO2RTC_READY = True
                log.info(f"go2rtc: ready — API {GO2RTC_API_HOST}:"
                         f"{GO2RTC_API_PORT}, WebRTC :{GO2RTC_WEBRTC_PORT}")
            elif proc.returncode is None:
                log.warning(f"go2rtc: API did not answer within "
                            f"{GO2RTC_READY_TIMEOUT_S:.0f}s")
            rc = await proc.wait()
        except asyncio.CancelledError:
            await _go2rtc_terminate(proc)
            raise
        finally:
            _GO2RTC_READY = False
            _GO2RTC_PROC = None
            _GO2RTC_STREAMS.clear()
            _GO2RTC_STREAM_CAM.clear()
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        ran = time.monotonic() - started
        if ran > 60:
            backoff = 2.0
        log.warning(f"go2rtc: exited (rc={rc}) after {ran:.0f}s — "
                    f"restarting in {backoff:.0f}s")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60.0)


def _go2rtc_stream_name(camera_id: str, prof_idx: int, kind: str = "p") -> str:
    """Stable, URL-safe go2rtc stream name for one camera profile.

    The hash keeps two camera_ids that differ only in punctuation from
    collapsing onto the same name once the punctuation is replaced.
    kind "p" is an Enhanced View profile; "c" (2.6.6) is a card stream.
    """
    tag = re.sub(r"[^A-Za-z0-9]", "_", camera_id)[:40]
    digest = hashlib.sha1(camera_id.encode("utf-8")).hexdigest()[:8]
    return f"anycam_{tag}_{digest}_{kind}{prof_idx}"


def _go2rtc_shared_name(camera_id: str, src: str) -> str:
    """3.3.0 (C4): one go2rtc stream name for one camera source.

    Every user of the same source (a live card, Enhanced View, the snapshot
    loop, motion detection, the recording buffer) gets the same name, so
    go2rtc holds one connection to the camera for all of them. A name never
    changes its source, so no user's stream is swapped under it. The hash
    is of the address without its password: the name reaches the browser.
    """
    tag = re.sub(r"[^A-Za-z0-9]", "_", camera_id)[:40]
    digest = hashlib.sha1(f"{camera_id}\n{_strip_creds(src)}".encode("utf-8")).hexdigest()[:10]
    return f"anycam_{tag}_{digest}"


async def _go2rtc_relay(camera_id: str, url: str | None) -> str | None:
    """3.3.0 (C4): the address of go2rtc's copy of an RTSP stream, for ffmpeg.

    Returns the camera's own address when go2rtc cannot serve it: go2rtc not
    running, not an RTSP address, the stream not accepted, or the camera
    already failed RELAY_FAIL_LIMIT times through go2rtc.
    """
    if (not url or not url.lower().startswith(("rtsp://", "rtsps://")) or not _GO2RTC_READY
            or _RELAY_FAILS.get(camera_id, 0) >= RELAY_FAIL_LIMIT):
        return url
    src = _go2rtc_camera_src(CAMERAS.get(camera_id), url)   # 3.7.1 (B31), 3.7.4 (B47)
    name = _go2rtc_shared_name(camera_id, src)
    if not await _go2rtc_register(name, src, camera_id):
        return url
    return (f"rtsp://{GO2RTC_RTSP_USER}:{_GO2RTC_RTSP_PASS}@{GO2RTC_API_HOST}:"
            f"{GO2RTC_RTSP_PORT}/{name}")


def _go2rtc_relayed(url: str | None) -> bool:
    return f"@{GO2RTC_API_HOST}:{GO2RTC_RTSP_PORT}/" in (url or "")


def _go2rtc_relay_result(camera_id: str, url: str | None, ok: bool) -> None:
    """Count a run that read through go2rtc; after RELAY_FAIL_LIMIT failures, go direct."""
    if not _go2rtc_relayed(url):
        return
    if ok:
        _RELAY_FAILS.pop(camera_id, None)
        return
    n = _RELAY_FAILS.get(camera_id, 0) + 1
    _RELAY_FAILS[camera_id] = n
    if n == RELAY_FAIL_LIMIT:
        log.warning(f"go2rtc: {camera_id} failed {n} times in a row through go2rtc — "
                    f"AnyCam opens the camera directly from now on")


def _go2rtc_profile_source(camera: dict,
                           prof_idx: int) -> tuple[str | None, str, str]:
    """Resolve one camera profile to an authenticated RTSP URL for go2rtc.

    Returns (url, codec, reason). url is None when go2rtc cannot relay the
    profile, and reason says why, for the browser's toast.

    Profile lookup mirrors snap_loop's native_res branch and
    _build_focus_ladder, so both engines open the same stream for the same
    profile index and the Resolution dropdown means one thing.
    """
    if camera.get("display") in ("webrtc", "wsrtsp"):
        if prof_idx != 0:
            return None, "", f"profile {prof_idx} does not exist"
        return _go2rtc_native_source(camera)
    if camera.get("display") in ("info", "appliance"):
        return None, "", "this camera is not an RTSP stream"
    profiles = _go2rtc_profiles(camera)
    if not 0 <= prof_idx < len(profiles):
        return None, "", f"profile {prof_idx} does not exist"
    prof = profiles[prof_idx]
    raw = prof.get("url") or camera.get(prof.get("_url_key", "stream_url"))
    return _go2rtc_relay_url(camera, raw, (prof.get("stream_codec") or "").lower())


def _go2rtc_native_source(camera: dict) -> tuple[str | None, str, str]:
    """3.2.0 (C6): the go2rtc source for a WebRTC or RTSP-over-WebSocket camera.

    Before 3.2.0 these cameras had an information card only. go2rtc
    1.9.14 plays both with the modules already loaded: a WHEP source
    (webrtc:http://..., the POST-an-offer endpoint probe_webrtc finds) and
    RTSP with a WebSocket transport (rtsp://...#transport=ws://...). The
    RTSP path inside the WebSocket is the brand's first stream-table path,
    else "/".
    """
    display = camera.get("display")
    if display == "webrtc":
        sig = camera.get("signaling_url") or camera.get("stream_url") or ""
        if not sig.lower().startswith(("http://", "https://")):
            return None, "", "no WebRTC signalling address"
        return f"webrtc:{build_authenticated_url(camera, url=sig) or sig}", "", "ok"
    if display == "wsrtsp":
        ws = camera.get("ws_url") or camera.get("stream_url") or ""
        if not ws.lower().startswith(("ws://", "wss://")):
            return None, "", "no WebSocket address"
        entry = _match_stream_db(camera) or {}
        path = (entry.get("rtsp") or ["/"])[0]
        rtsp = f"rtsp://{camera.get('ip', '')}:{entry.get('port') or 554}{path}"
        return (_go2rtc_rtsp_src(f"{build_authenticated_url(camera, url=rtsp) or rtsp}#transport={ws}"),
                "", "ok")
    return None, "", "this camera is not a WebRTC or WebSocket stream"


def _go2rtc_profiles(camera: dict) -> list[dict]:
    """The camera's stream profiles, best quality first."""
    profiles = camera.get("stream_profiles") or []
    if not profiles:
        # Cameras discovered before stream_profiles existed, and DVR channel
        # cards, which start with none.
        profiles = [{"url": camera.get("stream_url", ""),
                     "stream_codec": camera.get("stream_codec"),
                     "stream_width": camera.get("stream_width")}]
        if camera.get("sub_stream_url"):
            profiles.append({"url": camera.get("sub_stream_url", ""),
                             "stream_codec": camera.get("sub_stream_codec"),
                             "stream_width": camera.get("sub_stream_width")})
    return profiles


def _go2rtc_rtsp_src(url: str) -> str:
    """3.7.1 (B31): an RTSP source for go2rtc, without two-way audio.

    go2rtc asks every RTSP camera for its ONVIF backchannel (two-way audio)
    unless the source ends in #backchannel=0; its README calls that option
    "important for some glitchy cameras". Two older Hikvision cameras reset
    go2rtc's connection about once a second (2026-10-06, about 900 resets),
    while AnyCam's own probes and ffprobe worked. AnyCam never sends audio
    to a camera, so every RTSP source carries it.
    """
    if url.lower().startswith(("rtsp://", "rtsps://")) and "#backchannel=" not in url:
        return url + "#backchannel=0"
    return url


# ── 3.7.4 (B47): an ffmpeg copy in front of go2rtc ─────────────────────────
# The Microseven (Hipcam firmware) never played live: go2rtc built the
# browser's video description from the camera's own parameter sets, and
# Chrome refused it ("coded size: [4,0]" with H.265, "Unrecognized video
# codec profile" with H.264), while ffmpeg read the same stream at
# 3840x2160. go2rtc issues 2361 and 2529 describe it; the documented cure is
# an ffmpeg copy source, which rebuilds the parameter sets without decoding.
# Only cameras that need it get it: the brand entry's live_ffmpeg_copy, or
# the camera's own live_ffmpeg_copy, which wins.
def _go2rtc_ffmpeg_copy(camera: dict | None) -> bool:
    """True when this camera's go2rtc stream goes through an ffmpeg copy."""
    if not camera:
        return False
    if "live_ffmpeg_copy" in camera:
        return bool(camera["live_ffmpeg_copy"])
    return bool((_identify_camera_brand(dict(camera)) or {}).get("live_ffmpeg_copy"))


def _go2rtc_camera_src(camera: dict | None, url: str) -> str:
    """The go2rtc source for an authenticated RTSP address of this camera."""
    if _go2rtc_ffmpeg_copy(camera) and url.lower().startswith(("rtsp://", "rtsps://")):
        return f"ffmpeg:{url}#video=copy#audio=copy"
    return _go2rtc_rtsp_src(url)


def _go2rtc_relay_url(camera: dict, raw: str | None,
                      codec: str) -> tuple[str | None, str, str]:
    """Check one RTSP URL can be relayed; return (auth url, codec, reason)."""
    if not raw:
        return None, codec, "no stream URL for this profile"
    if not raw.lower().startswith(("rtsp://", "rtsps://")):
        return None, codec, "this profile is not RTSP"
    # Passthrough only. Browsers play H.264 and H.265 over WebRTC or MSE;
    # nothing plays MJPEG or MPEG-4 Part 2 that way, and AnyCam never has
    # go2rtc transcode (its ffmpeg copy, B47, does not decode). Unknown codecs are let through:
    # the browser negotiates, and falls back if negotiation fails.
    if codec in ("mjpeg", "jpeg", "mpeg4", "mp4v"):
        return None, codec, f"{codec.upper()} cannot play as live video"
    url = build_authenticated_url(camera, url=raw)
    if not url:
        return None, codec, "no stream URL for this profile"
    return _go2rtc_camera_src(camera, url), codec, "ok"


async def _go2rtc_register(name: str, src: str, camera_id: str) -> bool:
    """Create or update one go2rtc stream. No-op if the source is unchanged."""
    if _GO2RTC_STREAMS.get(name) == src:
        return True
    from yarl import URL
    method = "PATCH" if name in _GO2RTC_STREAMS else "PUT"
    # Encode with safe="" so every reserved character is escaped.
    # build_authenticated_url leaves '&' and '+' raw in the password, since
    # both are legal in RTSP userinfo. Raw in a query string, Go's parser
    # would split the value at '&' and read '+' as a space, and go2rtc
    # would dial the camera with the wrong password.
    query = f"name={quote(name, safe='')}&src={quote(src, safe='')}"
    put_url = URL(_go2rtc_api_url(f"/api/streams?{query}"), encoded=True)
    get_url = URL(_go2rtc_api_url(f"/api/streams?src={quote(name, safe='')}"),
                  encoded=True)
    timeout = aiohttp.ClientTimeout(total=5)
    try:
        async with _go2rtc_session(timeout=timeout) as session:
            async with session.request(method, put_url) as resp:
                body = (await resp.text()).strip()
                # See CREDENTIALS above: with no config file, go2rtc has
                # already created the stream when it returns this 400.
                if resp.status != 200 and "config file disabled" not in body:
                    log.warning(f"go2rtc: {method} {name} failed — HTTP "
                                f"{resp.status}: {_strip_creds(body)[:160]}")
                    return False
            # A GET naming only the stream does not dial the camera; go2rtc
            # probes the source only when media parameters are present.
            async with session.get(get_url) as resp:
                if resp.status != 200:
                    log.warning(f"go2rtc: {name} missing after {method} "
                                f"(HTTP {resp.status})")
                    return False
    except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
        log.warning(f"go2rtc: {method} {name} failed: {ex}")
        return False
    _GO2RTC_STREAMS[name] = src
    _GO2RTC_STREAM_CAM[name] = camera_id
    return True


def _go2rtc_card_source(camera: dict, h265: bool = True,
                        wide: bool = False) -> tuple[str | None, str, str]:
    """Pick the stream a live card plays; return (auth url, codec, reason).

    3.0.1 (C10): h265=False skips H.265 streams, for a browser that cannot
    play them.
    3.1.0 (C19): wide=True (a computer, not a phone) plays the smallest
    stream even when it is wider than CARD_MAX_WIDTH. The Pi only passes
    the bytes; the computer decodes them.
    """
    if camera.get("display") in ("webrtc", "wsrtsp"):
        return _go2rtc_native_source(camera)
    if camera.get("display") in ("info", "appliance"):
        return None, "", "this camera is not an RTSP stream"
    best: tuple[int, str, str] | None = None     # (width, url, codec)
    skipped_h265 = False
    for prof in _go2rtc_profiles(camera):
        raw = prof.get("url") or camera.get(prof.get("_url_key", "stream_url"))
        url, codec, _ = _go2rtc_relay_url(camera, raw,
                                          (prof.get("stream_codec") or "").lower())
        if not url:
            continue
        if not h265 and codec in ("hevc", "h265"):
            skipped_h265 = True
            continue
        width = prof.get("stream_width") or 0
        if best is None or (width and (not best[0] or width < best[0])):
            best = (width, url, codec)
    if best is not None and best[0] > CARD_MAX_WIDTH:
        sub = _dahua_sub_stream(camera.get("stream_url") or "")
        if sub:
            # Codec unknown: the browser negotiates, and the card falls
            # back to snapshots if it cannot play it.
            url, codec, _ = _go2rtc_relay_url(camera, sub, "")
            if url:
                return url, codec, "ok"
        if wide:
            return best[1], best[2], "ok"
        return None, best[2], f"no stream small enough for a card ({best[0]} wide)"
    if best is None:
        if skipped_h265:
            return None, "hevc", "this browser cannot play H.265, and the camera has no other stream"
        return None, "", "no stream that can play live"
    return best[1], best[2], "ok"


# ── 3.7.4 (B6, B32): a camera whose RTSP is stuck ─────────────────────────
# 2026-10-06, the Microseven: stuck, it accepted connections and answered
# nothing, or reset each one, also for VLC with AnyCam stopped; only a power
# cycle cleared it. Every live and classic try then failed in turn (about
# 50 s to a picture) and kept connecting to it. While stuck, AnyCam opens
# no RTSP to the camera: cards and Enhanced View use its HTTP snapshots if
# it has them, the card says to power-cycle it, and one quiet check every
# RTSP_STUCK_RECHECK_S ends the state when the camera answers again.
RTSP_STUCK_SIGNS = ("connection reset by peer", "broken pipe", "i/o timeout",
                    "connection refused")
RTSP_STUCK_REPORTS = 2           # live view reports with a sign, within ...
RTSP_STUCK_WINDOW_S = 120.0
RTSP_STUCK_RECHECK_S = 300.0
RTSP_STUCK_REASON = "the camera's live stream is not answering; power-cycle the camera if this lasts"
_RTSP_STUCK: dict[str, dict] = {}            # camera_id -> {"since", "why"}
_RTSP_SIGNS_SEEN: dict[str, list[float]] = {}


def _rtsp_stuck(camera_id: str) -> bool:
    return camera_id in _RTSP_STUCK


def _rtsp_stuck_mark(camera_id: str, why: str) -> None:
    """Record that this camera's RTSP is stuck, and start its quiet check."""
    if camera_id in _RTSP_STUCK or camera_id not in CAMERAS:
        return
    _RTSP_STUCK[camera_id] = {"since": time.monotonic(), "why": why}
    log.warning(f"LIVE [{camera_id}]: the camera's live stream (RTSP) is not answering ({why}). "
                f"AnyCam stops connecting to it and shows its HTTP snapshots if it has them; "
                f"if this lasts, cut the camera's power for 10 s. Checked again every "
                f"{RTSP_STUCK_RECHECK_S / 60:.0f} min")
    asyncio.create_task(_rtsp_stuck_recheck(camera_id))


def _rtsp_stuck_sign(camera_id: str, reason: str) -> None:
    """Count a live view failure that shows the camera dropping connections."""
    if not any(sign in reason.lower() for sign in RTSP_STUCK_SIGNS):
        return
    now = time.monotonic()
    seen = [t for t in _RTSP_SIGNS_SEEN.get(camera_id, []) if now - t < RTSP_STUCK_WINDOW_S]
    seen.append(now)
    _RTSP_SIGNS_SEEN[camera_id] = seen
    if len(seen) >= RTSP_STUCK_REPORTS:
        _RTSP_SIGNS_SEEN.pop(camera_id, None)
        _rtsp_stuck_mark(camera_id, reason[:120])


async def _rtsp_answers(camera: dict) -> bool:
    """One RTSP request with the password, on one connection, inside the cooldown."""
    url = build_authenticated_url(camera) or ""
    plain = camera.get("stream_url") or ""
    parts = urlsplit(url)
    if not plain.lower().startswith(("rtsp://", "rtsps://")) or not parts.hostname:
        return False
    await _throttle_wait_if_needed(camera.get("ip", ""), _brand_throttle_seconds(camera),
                                   "RTSP stuck check")
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            _THREAD_POOL, _validate_rtsp_urls_single_socket, parts.hostname, parts.port or 554,
            [plain], unquote(parts.username or ""), unquote(parts.password or ""), 6.0, None,
            f"{camera.get('id', '')}/stuck-check")
    except Exception as ex:
        log.debug(f"LIVE [{camera.get('id', '')}]: stuck check failed: {ex}")
        return False
    return any(result.values())


async def _rtsp_stuck_recheck(camera_id: str) -> None:
    """Check a stuck camera every RTSP_STUCK_RECHECK_S until it answers."""
    while camera_id in _RTSP_STUCK:
        await asyncio.sleep(RTSP_STUCK_RECHECK_S)
        camera = CAMERAS.get(camera_id)
        if not camera:
            _RTSP_STUCK.pop(camera_id, None)
            return
        if await _rtsp_answers(camera):
            _RTSP_STUCK.pop(camera_id, None)
            log.info(f"LIVE [{camera_id}]: the camera's live stream answers again — live view is back on")
            return
        log.info(f"LIVE [{camera_id}]: the camera's live stream still does not answer")


# 3.7.2 (B40): the page reports when a card or Enhanced View gives up on
# live view, so the add-on log shows it (before, only the browser console).
LIVE_FAIL_WHERE = ("card", "enhanced view", "enhanced view retry")   # 3.7.3 (B45): the retry
SMART_CODEC_HINT = ("if this camera uses H.264+, H.265+ or Smart Codec, turn it off, or set "
                    "its I-frame interval equal to its frame rate")


def _live_fail_line(camera_id: str, where: str, reason: str) -> str:
    """The log line for one live view fallback."""
    reason = " ".join(str(reason).split())[:200]
    if where == "enhanced view retry":
        return f"LIVE [{camera_id}]: enhanced view tries WebRTC, video only: {reason}"
    then = "uses still pictures" if where == "card" else "uses the classic view"
    line = f"LIVE [{camera_id}]: {where} {then}: {reason}"
    if reason.startswith("no video within"):
        line += f" ({SMART_CODEC_HINT})"
    return line


async def api_live_fail(request: web.Request) -> web.Response:
    """POST /api/live_fail  body: {"camera_id", "where": one of LIVE_FAIL_WHERE, "reason"}."""
    try:
        body = await request.json()
    except (ValueError, aiohttp.ContentTypeError):
        return web.json_response({"error": "JSON body required"}, status=400)
    camera_id = str(body.get("camera_id", ""))
    where = body.get("where")
    if camera_id not in CAMERAS or where not in LIVE_FAIL_WHERE:
        return web.json_response({"error": "unknown camera or place"}, status=400)
    log.info(_live_fail_line(camera_id, where, body.get("reason", "")))
    _rtsp_stuck_sign(camera_id, str(body.get("reason", "")))     # 3.7.4 (B6, B32)
    return web.json_response({"status": "ok"})


async def api_go2rtc_card(request: web.Request) -> web.Response:
    """GET /api/go2rtc/card/{camera_id} — prepare a live card (2.6.6, C1).

    Like api_go2rtc_focus, but picks the stream itself (_go2rtc_card_source)
    and registers it under a card name, so a card and Enhanced View on the
    same camera are two go2rtc streams. Never dials the camera.
    """
    camera_id = request.match_info["camera_id"]
    camera = CAMERAS.get(camera_id)
    # 3.1.0 (C19): an MJPEG camera plays its own MJPEG stream, through
    # anycam_mjpeg.py; it does not need go2rtc.
    mjpeg = _mjpeg_source(camera) if camera else None
    if not _GO2RTC_READY:
        if mjpeg:
            return web.json_response(_mjpeg_card(camera_id))
        # retry: go2rtc may still be starting (it starts with the web server);
        # the card uses snapshots now and tries live again later.
        return web.json_response({"ok": False, "reason": "go2rtc is not running",
                                  "retry": True})
    if not camera:
        return web.json_response({"ok": False, "reason": "camera not found"},
                                 status=404)
    if _rtsp_stuck(camera_id):                 # 3.7.4 (B6, B32): no RTSP to it
        return web.json_response({"ok": False, "reason": RTSP_STUCK_REASON, "retry": True})
    src, codec, reason = _go2rtc_card_source(camera, h265=request.query.get("h265") != "0",
                                             wide=request.query.get("wide") == "1")
    if not src:
        if mjpeg:
            return web.json_response(_mjpeg_card(camera_id))
        if any(r in reason for r in _REFRESH_REASONS):
            asyncio.create_task(_streams_refresh(camera_id, f"card: {reason}"))
        return web.json_response({"ok": False, "reason": reason, "codec": codec})
    name = _go2rtc_shared_name(camera_id, src)     # 3.3.0 (C4): shared with the other users
    if not await _go2rtc_register(name, src, camera_id):
        return web.json_response({"ok": False,
                                  "reason": "go2rtc rejected the stream"})
    return web.json_response({"ok": True, "stream": name, "codec": codec})


def _mjpeg_card(camera_id: str) -> dict:
    """The answer for a card that plays the camera's MJPEG stream (3.1.0, C19)."""
    return {"ok": True, "kind": "mjpeg", "codec": "mjpeg",
            "url": f"/api/mjpeg/{quote(camera_id, safe='')}/ws"}


async def handle_go2rtc_ws(request: web.Request) -> web.StreamResponse:
    """GET /go2rtc/ws?src=<name> — relay the player's WebSocket to go2rtc.

    The only way into go2rtc from a browser: its API listens on 127.0.0.1,
    and HA ingress proxies only to this addon's port. Forwards /api/ws, and
    only for names in _GO2RTC_STREAMS. MSE video and WebRTC signalling both
    ride this one socket; WebRTC media itself goes direct to the WebRTC port.
    """
    name = request.query.get("src", "")
    camera_id = _GO2RTC_STREAM_CAM.get(name)
    if not (_GO2RTC_READY and camera_id and name in _GO2RTC_STREAMS):
        return web.Response(status=404, text="Unknown live stream")
    camera = CAMERAS.get(camera_id) or {}
    from yarl import URL

    client_ws = web.WebSocketResponse(heartbeat=30.0)
    await client_ws.prepare(request)

    # go2rtc dials the camera when this consumer connects upstream, so wait
    # out the brand cooldown first. The Microseven draws an RST for two opens
    # inside 5 s; without this wait, the thumbnail ffmpeg's last open and
    # go2rtc's first could land inside one window.
    throttle_s = _brand_throttle_seconds(camera) if camera else 0.0
    if throttle_s > 0:
        await _throttle_wait_if_needed(camera.get("ip", ""), throttle_s,
                                       f"go2rtc live view {camera_id}")

    upstream = URL(_go2rtc_api_url(f"/api/ws?src={quote(name, safe='')}"),
                   encoded=True)
    session = _go2rtc_session()
    try:
        try:
            # max_msg_size=0: one MSE fragment carrying a 4K HEVC keyframe can
            # exceed aiohttp's 4 MiB default, which would drop the socket.
            upstream_ws = await session.ws_connect(upstream, max_msg_size=0,
                                                   heartbeat=30.0)
        except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
            log.warning(f"go2rtc: live view for {camera_id} could not reach "
                        f"go2rtc: {ex}")
            # The browser treats an error that names no mode as fatal before
            # the first frame, and falls back to the classic view at once.
            await client_ws.send_json({"type": "error",
                                       "value": "anycam: go2rtc unreachable"})
            await client_ws.close()
            return client_ws

        async def _relay(reader: "web.WebSocketResponse | aiohttp.ClientWebSocketResponse",
                         writer: "web.WebSocketResponse | aiohttp.ClientWebSocketResponse") -> None:
            async for msg in reader:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await writer.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await writer.send_bytes(msg.data)
                else:   # CLOSE, CLOSING, CLOSED, ERROR
                    break

        log.info(f"go2rtc: live view opened for {camera_id} ({name})")
        up = asyncio.create_task(_relay(upstream_ws, client_ws))
        down = asyncio.create_task(_relay(client_ws, upstream_ws))
        try:
            await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (up, down):
                task.cancel()
            await asyncio.gather(up, down, return_exceptions=True)
            await upstream_ws.close()
        log.info(f"go2rtc: live view closed for {camera_id}")
    finally:
        await session.close()
        if not client_ws.closed:
            await client_ws.close()
    return client_ws


async def handle_go2rtc_player_js(request: web.Request) -> web.Response:
    """GET /go2rtc/video-rtc.js — the vendored go2rtc player module.

    Served from memory with an explicit JavaScript type: the browser loads it
    with import(), and ES modules refuse any other MIME type.
    """
    global _GO2RTC_PLAYER_BYTES
    if _GO2RTC_PLAYER_BYTES is None:
        # Read in the thread pool: file I/O would otherwise block the event
        # loop (best practices §1.4). Once per process, then served from memory.
        loop = asyncio.get_running_loop()
        try:
            _GO2RTC_PLAYER_BYTES = await loop.run_in_executor(
                _THREAD_POOL, GO2RTC_PLAYER_JS.read_bytes)
        except OSError:
            return web.Response(status=404, text="go2rtc player not installed")
    return web.Response(body=_GO2RTC_PLAYER_BYTES,
                        content_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})
