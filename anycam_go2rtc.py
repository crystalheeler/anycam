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
import time
from aiohttp import web
from pathlib import Path
from urllib.parse import urlparse, quote

log = logging.getLogger("anycam")

# Taken from camera_discovery.py at start-up (anycam_host.bind).
NEEDS = (
    'CAMERAS', 'CARD_MAX_WIDTH', '_ANSI_ESCAPE_RE', '_THREAD_POOL',
    '_brand_throttle_seconds', '_dahua_sub_stream', '_strip_creds', '_throttle_wait_if_needed',
    'build_authenticated_url',
)
GO2RTC_BIN             = Path("/usr/local/bin/go2rtc")
GO2RTC_API_HOST        = "127.0.0.1"
# Non-default ports. 1984 and 8555 are go2rtc's defaults, and another add-on
# on a host-networked HAOS box (Frigate, the go2rtc add-on) may hold them.
GO2RTC_API_PORT        = 28984
GO2RTC_WEBRTC_PORT     = 28555
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
        "app":    {"modules": ["api", "ws", "rtsp", "webrtc", "mp4"]},
        "api":    {"listen": f"{GO2RTC_API_HOST}:{GO2RTC_API_PORT}"},
        "rtsp":   {"listen": ""},
        "webrtc": {"listen": f":{GO2RTC_WEBRTC_PORT}"},
        # warn keeps routine per-request lines out of the addon log. Source
        # URLs can still appear in a warning; _go2rtc_log_pump strips creds.
        "log":    {"level": "warn"},
    }, separators=(",", ":"))


def _go2rtc_api_url(path: str) -> str:
    return f"http://{GO2RTC_API_HOST}:{GO2RTC_API_PORT}{path}"


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
    async with aiohttp.ClientSession(timeout=timeout) as session:
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


def _go2rtc_profile_source(camera: dict,
                           prof_idx: int) -> tuple[str | None, str, str]:
    """Resolve one camera profile to an authenticated RTSP URL for go2rtc.

    Returns (url, codec, reason). url is None when go2rtc cannot relay the
    profile, and reason says why, for the browser's toast.

    Profile lookup mirrors snap_loop's native_res branch and
    _build_focus_ladder, so both engines open the same stream for the same
    profile index and the Resolution dropdown means one thing.
    """
    if camera.get("display") in ("webrtc", "wsrtsp", "info", "appliance"):
        return None, "", "this camera is not an RTSP stream"
    profiles = _go2rtc_profiles(camera)
    if not 0 <= prof_idx < len(profiles):
        return None, "", f"profile {prof_idx} does not exist"
    prof = profiles[prof_idx]
    raw = prof.get("url") or camera.get(prof.get("_url_key", "stream_url"))
    return _go2rtc_relay_url(camera, raw, (prof.get("stream_codec") or "").lower())


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


def _go2rtc_relay_url(camera: dict, raw: str | None,
                      codec: str) -> tuple[str | None, str, str]:
    """Check one RTSP URL can be relayed; return (auth url, codec, reason)."""
    if not raw:
        return None, codec, "no stream URL for this profile"
    if not raw.lower().startswith(("rtsp://", "rtsps://")):
        return None, codec, "this profile is not RTSP"
    # Passthrough only. Browsers play H.264 and H.265 over WebRTC or MSE;
    # nothing plays MJPEG or MPEG-4 Part 2 that way, and ffmpeg is not
    # loaded in go2rtc to transcode them. Unknown codecs are let through:
    # the browser negotiates, and falls back if negotiation fails.
    if codec in ("mjpeg", "jpeg", "mpeg4", "mp4v"):
        return None, codec, f"{codec.upper()} cannot play as live video"
    url = build_authenticated_url(camera, url=raw)
    if not url:
        return None, codec, "no stream URL for this profile"
    return url, codec, "ok"


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
        async with aiohttp.ClientSession(timeout=timeout) as session:
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


def _go2rtc_card_source(camera: dict, h265: bool = True) -> tuple[str | None, str, str]:
    """Pick the stream a live card plays; return (auth url, codec, reason).

    3.0.1 (C10): h265=False skips H.265 streams, for a browser that cannot
    play them.
    """
    if camera.get("display") in ("webrtc", "wsrtsp", "info", "appliance"):
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
        return None, best[2], f"no stream small enough for a card ({best[0]} wide)"
    if best is None:
        if skipped_h265:
            return None, "hevc", "this browser cannot play H.265, and the camera has no other stream"
        return None, "", "no stream that can play live"
    return best[1], best[2], "ok"


async def api_go2rtc_card(request: web.Request) -> web.Response:
    """GET /api/go2rtc/card/{camera_id} — prepare a live card (2.6.6, C1).

    Like api_go2rtc_focus, but picks the stream itself (_go2rtc_card_source)
    and registers it under a card name, so a card and Enhanced View on the
    same camera are two go2rtc streams. Never dials the camera.
    """
    camera_id = request.match_info["camera_id"]
    if not _GO2RTC_READY:
        # retry: go2rtc may still be starting (it starts with the web server);
        # the card uses snapshots now and tries live again later.
        return web.json_response({"ok": False, "reason": "go2rtc is not running",
                                  "retry": True})
    camera = CAMERAS.get(camera_id)
    if not camera:
        return web.json_response({"ok": False, "reason": "camera not found"},
                                 status=404)
    src, codec, reason = _go2rtc_card_source(camera, h265=request.query.get("h265") != "0")
    if not src:
        return web.json_response({"ok": False, "reason": reason, "codec": codec})
    name = _go2rtc_stream_name(camera_id, 0, kind="c")
    if not await _go2rtc_register(name, src, camera_id):
        return web.json_response({"ok": False,
                                  "reason": "go2rtc rejected the stream"})
    return web.json_response({"ok": True, "stream": name, "codec": codec})


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
    session = aiohttp.ClientSession()
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
