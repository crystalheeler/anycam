"""Live MJPEG cards (3.1.0, build plan C19).

Before 3.1.0 a camera that serves MJPEG only had a still-picture card: the
page asked for one picture at a time, and the add-on fetched one picture at a
time from the camera. Now the add-on holds one connection to the camera's
own MJPEG stream and passes each JPEG on, unchanged, to every card that
shows that camera. Nothing is decoded on the Pi.

The pictures go to the page over a WebSocket, not as one long multipart
HTTP response: Home Assistant's ingress proxy ends a long multipart response
(see feedHTML in page_script.py), and it carries the go2rtc live view's
WebSocket without trouble.

This file cannot import camera_discovery.py (see anycam_host.py). The names
in NEEDS are set on this module at start-up.
"""
import aiohttp
import asyncio
import logging
import time
from aiohttp import web

from anycam_snap import _http_digest_header

log = logging.getLogger("anycam")

# Taken from camera_discovery.py at start-up (anycam_host.bind).
NEEDS = (
    'CAMERAS', '_brand_throttle_seconds', '_strip_creds', '_throttle_wait_if_needed',
    'decrypt_creds',
)

MJPEG_IDLE_S      = 5.0             # the camera connection closes this long after the last card
MJPEG_RETRY_S     = (2.0, 30.0)     # first and longest wait before a new connection
MJPEG_NO_FRAME_S  = 15.0            # a card is told when no picture came for this long
MJPEG_MAX_FRAME   = 4 * 1024 * 1024 # a "picture" larger than this is not one
_MJPEG_CODECS     = ("mjpeg", "jpeg")
_NOT_A_STREAM     = ("webrtc", "wsrtsp", "info", "appliance")


def _mjpeg_source(camera: dict) -> str | None:
    """The camera's HTTP MJPEG stream, without a password; None if it has none.

    The smallest stream of known width comes first: a card is small.
    """
    if camera.get("display") in _NOT_A_STREAM:
        return None
    found: list[tuple[int, str]] = []
    for prof in camera.get("stream_profiles") or []:
        if (prof.get("stream_codec") or "").lower() in _MJPEG_CODECS:
            found.append((prof.get("stream_width") or 0, prof.get("url") or ""))
    if (camera.get("protocol") or "").upper() == "MJPEG":
        found.append((camera.get("stream_width") or 0, camera.get("stream_url") or ""))
    if (camera.get("sub_stream_codec") or "").lower() in _MJPEG_CODECS:
        found.append((camera.get("sub_stream_width") or 0, camera.get("sub_stream_url") or ""))
    found = [(w, u) for w, u in found if u.lower().startswith(("http://", "https://"))]
    if not found:
        return None
    found.sort(key=lambda f: (f[0] == 0, f[0]))
    return _strip_creds(found[0][1])


def _jpeg_end(buf: bytearray, start: int) -> int:
    """Index after the JPEG that starts at buf[start]; -1 incomplete, -2 not a JPEG.

    Walks the JPEG's segments up to the picture data, so an end marker
    inside a segment (an EXIF thumbnail) does not cut the picture short. In
    the picture data itself an end marker can only be the real end.
    """
    i, n = start + 2, len(buf)
    while True:
        if i + 4 > n:
            return -1
        if buf[i] != 0xFF:
            return -2
        marker = buf[i + 1]
        if marker == 0xFF:              # fill byte
            i += 1
            continue
        if marker == 0xD9:              # end of picture
            return i + 2
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2
            continue
        length = (buf[i + 2] << 8) | buf[i + 3]
        if marker == 0xDA:              # start of picture data
            end = buf.find(b"\xff\xd9", i + 2 + length)
            return -1 if end < 0 else end + 2
        i += 2 + length


class _JpegSplitter:
    """Cut an MJPEG byte stream into JPEG pictures.

    It finds the pictures themselves, not the multipart boundaries, so a
    camera that gets the boundary or the part headers wrong still works.
    """

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out: list[bytes] = []
        while True:
            start = self.buf.find(b"\xff\xd8\xff")
            if start < 0:
                del self.buf[:max(len(self.buf) - 2, 0)]
                return out
            end = _jpeg_end(self.buf, start)
            if end == -1:
                del self.buf[:start]
                if len(self.buf) > MJPEG_MAX_FRAME:
                    self.buf.clear()
                return out
            if end == -2:
                del self.buf[:start + 2]
                continue
            out.append(bytes(self.buf[start:end]))
            del self.buf[:end]


class _MjpegHub:
    """One camera connection, shared by every card that shows the camera."""

    def __init__(self, camera_id: str) -> None:
        self.camera_id = camera_id
        self.frame: bytes | None = None
        self.seq = 0
        self.viewers = 0
        self.idle_since = time.monotonic()
        self.failed = False
        self.reason = ""
        self.task: asyncio.Task | None = None
        self.changed = asyncio.Event()

    def publish(self, frame: bytes | None) -> None:
        if frame is not None:
            self.frame = frame
            self.seq += 1
        event, self.changed = self.changed, asyncio.Event()
        event.set()

    def idle(self) -> bool:
        return self.viewers == 0 and time.monotonic() - self.idle_since >= MJPEG_IDLE_S


_HUBS: dict[str, _MjpegHub] = {}


async def _mjpeg_read(hub: _MjpegHub, camera: dict, url: str) -> int:
    """One connection to the camera; return the pictures it gave, or -1 to stop.

    -1 means trying again cannot help: a refused password, a missing page, or
    a URL that sends a single picture instead of a stream.
    """
    cid = hub.camera_id
    throttle_s = _brand_throttle_seconds(camera)
    if throttle_s > 0:
        await _throttle_wait_if_needed(camera.get("ip", ""), throttle_s, f"MJPEG card {cid}")
    u = p = ""
    if camera.get("credentials"):
        try:
            u, p = decrypt_creds(camera["credentials"])
        except Exception as exc:
            log.debug(f"MJPEG [{cid}]: saved password unreadable: {exc}")
    frames = 0
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=10)
    # ssl=False: cameras use self-signed certificates (see http_snap_loop).
    async with aiohttp.ClientSession(timeout=timeout,
                                     connector=aiohttp.TCPConnector(ssl=False)) as session:
        try:
            resp = await session.get(url, auth=aiohttp.BasicAuth(u, p) if u else None)
            www_auth = resp.headers.get("WWW-Authenticate", "")
            if resp.status == 401 and u and www_auth.startswith("Digest"):
                resp.release()
                rest = url.split("//", 1)[1]
                uri = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
                resp = await session.get(url, headers={
                    "Authorization": _http_digest_header(www_auth, "GET", uri, u, p)})
            async with resp:
                if resp.status != 200:
                    hub.reason = f"the camera answered HTTP {resp.status}"
                    log.warning(f"MJPEG [{cid}]: {hub.reason} for {url}")
                    return -1 if resp.status in (401, 403, 404) else 0
                if "multipart" not in resp.headers.get("Content-Type", "").lower():
                    hub.reason = "the camera sends single pictures, not a stream"
                    log.warning(f"MJPEG [{cid}]: {hub.reason} ({url})")
                    return -1
                log.info(f"MJPEG [{cid}]: live card stream open ({url})")
                splitter = _JpegSplitter()
                async for chunk in resp.content.iter_any():
                    for jpeg in splitter.feed(chunk):
                        frames += 1
                        hub.publish(jpeg)
                    if hub.idle():
                        break
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            hub.reason = f"the connection failed: {exc}"
            log.warning(f"MJPEG [{cid}]: {hub.reason}")
    log.info(f"MJPEG [{cid}]: live card stream closed after {frames} picture(s)")
    return frames


async def _mjpeg_pump(hub: _MjpegHub) -> None:
    """Keep the camera connection while any card shows it."""
    delay = MJPEG_RETRY_S[0]
    try:
        while not hub.idle():
            camera = CAMERAS.get(hub.camera_id)
            url = _mjpeg_source(camera) if camera else None
            if not url:
                hub.reason = "the camera has no MJPEG stream"
                hub.failed = True
                break
            got = await _mjpeg_read(hub, camera, url)
            if got < 0:
                hub.failed = True
                break
            if got > 0:
                delay = MJPEG_RETRY_S[0]
            if hub.idle():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, MJPEG_RETRY_S[1])
    finally:
        # No await from the last idle check to here: a card that joins
        # before it keeps this hub, one that joins after finds a new one.
        if _HUBS.get(hub.camera_id) is hub:
            del _HUBS[hub.camera_id]
        hub.task = None
        hub.publish(None)       # wake the cards, so they see failed


async def handle_mjpeg_ws(request: web.Request) -> web.StreamResponse:
    """GET /api/mjpeg/{camera_id}/ws — the camera's MJPEG pictures, one message each.

    A text message starting "error:" ends the stream; the card then shows
    still pictures.
    """
    camera_id = request.match_info["camera_id"]
    camera = CAMERAS.get(camera_id)
    if not camera or not _mjpeg_source(camera):
        return web.Response(status=404, text="No MJPEG stream for this camera")
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)
    hub = _HUBS.get(camera_id)
    if hub is None:
        hub = _HUBS[camera_id] = _MjpegHub(camera_id)
    hub.viewers += 1
    if hub.task is None:
        hub.task = asyncio.create_task(_mjpeg_pump(hub))

    async def _client() -> None:
        async for _msg in ws:          # the page sends nothing; this sees the close
            pass
    client = asyncio.create_task(_client())
    sent = 0
    try:
        while not client.done():
            if hub.seq != sent and hub.frame is not None:
                sent = hub.seq
                await ws.send_bytes(hub.frame)
                continue
            if hub.failed:
                await ws.send_str("error: " + (hub.reason or "the stream stopped"))
                break
            waiter = asyncio.ensure_future(hub.changed.wait())
            done, _ = await asyncio.wait({waiter, client}, timeout=MJPEG_NO_FRAME_S,
                                         return_when=asyncio.FIRST_COMPLETED)
            waiter.cancel()
            if not done:
                await ws.send_str("error: no picture from the camera in "
                                  f"{MJPEG_NO_FRAME_S:.0f} s")
                break
    except (ConnectionResetError, aiohttp.ClientError):
        pass
    finally:
        client.cancel()
        hub.viewers -= 1
        if hub.viewers == 0:
            hub.idle_since = time.monotonic()
        if not ws.closed:
            await ws.close()
    return ws
