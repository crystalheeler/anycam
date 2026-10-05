"""Server-side behaviour tests for AnyCam.

Runs the REAL functions and handlers from camera_discovery.py against
stand-ins: a fake go2rtc that reproduces the behaviour read from go2rtc
v1.9.14's source, fake ffmpeg processes, and a fake Home Assistant API.
No camera, no ffmpeg and no network are needed.

Run:  python tests/test_server.py        (or tests/run_tests.py for everything)
Exit code 0 = every check passed.
"""
import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import sys
import textwrap
import time
from pathlib import Path
from urllib.parse import parse_qs

import tempfile

REPO = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="anycam-tests-"))   # files the tests write
os.environ["INGRESS_PATH"] = "/api/hassio_ingress/TESTTOKEN"

sys.path.insert(0, str(REPO))            # camera_discovery imports its sibling files
from anycam_modules import MODULES


def repo_source() -> str:
    """The add-on's Python source, every file joined."""
    return "\n".join((REPO / m).read_text(encoding="utf-8") for m in MODULES)


spec = importlib.util.spec_from_file_location("cd", REPO / "camera_discovery.py")
_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_main)
_PARTS = [_main] + [sys.modules[Path(m).stem] for m in MODULES[1:] if Path(m).stem in sys.modules]


class _AddOn:
    """The add-on's files seen as one namespace, as before the module split.

    Reading finds a name in whichever file defines it. Writing replaces it in
    every file that holds it, so a test's stand-in reaches every caller: a
    function that moved to another file looks the name up in that file.
    """

    def __getattr__(self, name):
        for part in _PARTS:
            if name in vars(part):
                return vars(part)[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        for part in [p for p in _PARTS if name in vars(p)] or [_main]:
            setattr(part, name, value)


cd = _AddOn()

import aiohttp
from aiohttp import web, WSMsgType
from aiohttp.test_utils import make_mocked_request
from cryptography.fernet import Fernet

cd._FERNET = Fernet(Fernet.generate_key())   # never touch /data/secret.key
# 3.0.1: the scan and the add-on's first-start settings write runtime.json;
# keep it in the scratch folder, never in /data (C:\data on Windows).
cd.DATA_DIR = SCRATCH
cd.RUNTIME_FILE = SCRATCH / "runtime.json"


def _scene_jpegs():
    """A textured 704x480 scene, and the same scene with a person-sized
    block covering 8% of it, as real JPEGs (the 2.6.6 detector decodes)."""
    import io as _io, random as _r
    from PIL import Image as _I, ImageDraw as _D
    _r.seed(3)
    img = _I.new("RGB", (704, 480), (90, 110, 80))
    d = _D.Draw(img)
    for _ in range(400):
        x, y = _r.randrange(704), _r.randrange(480); r = _r.randrange(5, 60)
        d.rectangle([x, y, x + r, y + r // 2], fill=tuple(_r.randrange(40, 220) for _ in range(3)))

    def enc(i):
        b = _io.BytesIO(); i.save(b, "JPEG", quality=45); return b.getvalue()
    busy = img.copy(); _D.Draw(busy).rectangle([300, 120, 400, 390], fill=(30, 30, 40))
    return enc(img), enc(busy)


SCENE, SCENE_PERSON = _scene_jpegs()

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


CAP = LogCapture()
cd.log.addHandler(CAP)
cd.log.setLevel(logging.DEBUG)

TRICKY_PASS = "p@ss&w+rd #50% x"
BIG = os.urandom(5 * 1024 * 1024)   # larger than aiohttp's 4 MiB default


def camera(**kw):
    base = {"id": "cam1", "ip": "10.0.0.33", "port": 554,
            "stream_codec": "hevc",
            "stream_url": "rtsp://10.0.0.33:554/Streaming/Channels/101",
            "credentials": cd.encrypt_creds("admin", TRICKY_PASS),
            "stream_profiles": [
                {"url": "rtsp://10.0.0.33:554/Streaming/Channels/101",
                 "stream_codec": "hevc"},
                {"url": "rtsp://10.0.0.33:554/Streaming/Channels/102",
                 "stream_codec": "mjpeg"},
            ]}
    base.update(kw)
    return base


# ── A. pure functions ─────────────────────────────────────────────────────────
def test_pure():
    print("\n[A] pure functions")
    raw = cd._go2rtc_config()
    conf = json.loads(raw)
    check("config is inline JSON (starts with '{')", raw.startswith("{"))
    check("config: exact module allowlist",
          conf["app"]["modules"] == ["api", "ws", "rtsp", "webrtc", "mp4"],
          str(conf["app"]["modules"]))
    check("config: exec/echo/expr/ffmpeg absent",
          not {"exec", "echo", "expr", "ffmpeg"} & set(conf["app"]["modules"]))
    check("config: API on 127.0.0.1:28984",
          conf["api"]["listen"] == "127.0.0.1:28984", conf["api"]["listen"])
    check("config: RTSP server on 127.0.0.1 only, with a password (3.3.0, C4)",
          conf["rtsp"]["listen"] == "127.0.0.1:28554" and conf["rtsp"]["username"] == "anycam"
          and len(conf["rtsp"]["password"]) >= 24, str(conf["rtsp"]))
    check("config: WebRTC on :28555", conf["webrtc"]["listen"] == ":28555")
    check("config: log level warn", conf["log"]["level"] == "warn")

    a = cd._go2rtc_stream_name("10.0.0.33:554", 0)
    b = cd._go2rtc_stream_name("10_1_1_33_554", 0)
    check("stream name deterministic", a == cd._go2rtc_stream_name("10.0.0.33:554", 0))
    check("stream name URL-safe", all(c.isalnum() or c == "_" for c in a), a)
    check("punctuation-only id difference does not collide", a != b, f"{a} vs {b}")

    url, codec, reason = cd._go2rtc_profile_source(camera(), 0)
    check("profile 0 (HEVC RTSP) eligible", url is not None and reason == "ok", reason)
    check("profile 0 URL carries credentials",
          url is not None and url.startswith("rtsp://admin:") and "@10.0.0.33" in url, str(url))
    url, codec, reason = cd._go2rtc_profile_source(camera(), 1)
    check("profile 1 (MJPEG) rejected", url is None and "MJPEG" in reason, reason)
    url, _, reason = cd._go2rtc_profile_source(camera(), 5)
    check("out-of-range profile rejected", url is None, reason)
    url, _, reason = cd._go2rtc_profile_source(camera(), -1)
    check("negative profile rejected", url is None, reason)
    url, _, reason = cd._go2rtc_profile_source(camera(display="webrtc"), 0)
    check("non-RTSP display type rejected", url is None, reason)
    url, _, reason = cd._go2rtc_profile_source(
        camera(stream_profiles=[{"url": "http://10.0.0.22/video.mjpg", "stream_codec": "h264"}]), 0)
    check("HTTP profile rejected", url is None, reason)
    legacy = camera(stream_profiles=[], stream_url="rtsp://10.0.0.73:8765/live",
                    sub_stream_url="rtsp://10.0.0.73:8765/sub", sub_stream_codec="h264")
    url0, _, r0 = cd._go2rtc_profile_source(legacy, 0)
    url1, _, r1 = cd._go2rtc_profile_source(legacy, 1)
    check("pre-stream_profiles camera: main resolves",
          url0 is not None and url0.endswith("@10.0.0.73:8765/live"), str(url0))
    check("pre-stream_profiles camera: sub resolves",
          url1 is not None and url1.endswith("@10.0.0.73:8765/sub"), str(url1))


# ── fake go2rtc ───────────────────────────────────────────────────────────────
class FakeGo2rtc:
    def __init__(self):
        self.puts, self.patches, self.gets = [], [], 0
        self.registered = {}
        self.ws_srcs = []
        self.ws_closed = asyncio.Event()
        self.put_status, self.put_body = 400, "config file disabled"
        self.runner = None

    async def api(self, request):
        return web.json_response({"version": "fake"})

    async def streams(self, request):
        # Go semantics: url.ParseQuery turns '+' into ' ', like parse_qs.
        # raw_query_string: the bytes on the wire. query_string is already
        # percent-decoded by yarl, and decoding it again double-decodes.
        raw = request.rel_url.raw_query_string
        q = {k: v[0] for k, v in parse_qs(raw).items()}
        if request.method == "PUT":
            self.puts.append((raw, q.get("name"), q.get("src")))
            self.registered[q["name"]] = q["src"]      # created before persisting
            return web.Response(status=self.put_status, text=self.put_body)
        if request.method == "PATCH":
            self.patches.append((q.get("name"), q.get("src")))
            self.registered[q["name"]] = q["src"]
            return web.Response(status=200)
        self.gets += 1
        return (web.json_response({}) if q.get("src") in self.registered
                else web.Response(status=404))

    async def ws(self, request):
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(request)
        self.ws_srcs.append(request.query.get("src"))
        await ws.send_str(json.dumps({"type": "mse", "value": "avc1.640029"}))
        await ws.send_bytes(BIG)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await ws.send_str(json.dumps({"type": "echo", "value": msg.data}))
        self.ws_closed.set()
        return ws

    async def start(self):
        app = web.Application()
        app.router.add_get("/api", self.api)
        app.router.add_route("*", "/api/streams", self.streams)
        app.router.add_get("/api/ws", self.ws)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 28984).start()

    async def stop(self):
        await self.runner.cleanup()


# ── B. registration against fake go2rtc ───────────────────────────────────────
async def test_register(fake):
    print("\n[B] stream registration")
    cd._GO2RTC_STREAMS.clear(); cd._GO2RTC_STREAM_CAM.clear()
    src, _, _ = cd._go2rtc_profile_source(camera(), 0)
    ok = await cd._go2rtc_register("s1", src, "cam1")
    check("register succeeds despite HTTP 400 'config file disabled'", ok)
    check("used PUT for a new stream", len(fake.puts) == 1)
    raw_q, name, got_src = fake.puts[-1]
    check("go2rtc receives the source URL byte-exact (password with @ & + # % space)",
          got_src == src, f"sent={src!r} got={got_src!r}")
    check("'&' in password did not split the query", "&" not in raw_q.split("&src=", 1)[1])
    naive = parse_qs("name=s1&src=" + src)
    check("control: an unencoded src WOULD be corrupted by Go's parser",
          naive.get("src", [""])[0] != src)
    check("verified with a GET after PUT", fake.gets >= 1)
    n_puts, n_gets = len(fake.puts), fake.gets
    ok = await cd._go2rtc_register("s1", src, "cam1")
    check("re-register with same source is a no-op",
          ok and len(fake.puts) == n_puts and fake.gets == n_gets)
    ok = await cd._go2rtc_register("s1", src + "?changed=1", "cam1")
    check("changed source uses PATCH", ok and len(fake.patches) == 1)
    check("allowlist + camera map updated",
          cd._GO2RTC_STREAMS.get("s1", "").endswith("changed=1")
          and cd._GO2RTC_STREAM_CAM.get("s1") == "cam1")
    fake.put_status, fake.put_body = 400, "streams: wrong source"
    ok = await cd._go2rtc_register("s2", "rtsp://bad", "cam1")
    check("a real 400 error is reported as failure", ok is False)
    check("failed stream not added to allowlist", "s2" not in cd._GO2RTC_STREAMS)
    fake.put_status, fake.put_body = 400, "config file disabled"


# ── C. api_go2rtc_focus ───────────────────────────────────────────────────────
async def test_focus_api(fake):
    print("\n[C] /api/go2rtc/focus")
    cd.CAMERAS.clear(); cd.CAMERAS["cam1"] = camera()
    cd._GO2RTC_READY = True

    async def call(path, cam_id):
        req = make_mocked_request("GET", path, match_info={"camera_id": cam_id})
        resp = await cd.api_go2rtc_focus(req)
        return resp.status, json.loads(resp.body)

    st, body = await call("/api/go2rtc/focus/cam1?profile=0", "cam1")
    check("eligible profile -> ok with stream name",
          st == 200 and body["ok"] and body["stream"].startswith("anycam_"), str(body))
    check("reported codec is hevc", body.get("codec") == "hevc")
    st, body = await call("/api/go2rtc/focus/cam1?profile=1", "cam1")
    check("MJPEG profile -> ok:false with reason",
          st == 200 and not body["ok"] and "MJPEG" in body["reason"], str(body))
    st, body = await call("/api/go2rtc/focus/nope?profile=0", "nope")
    check("unknown camera -> 404", st == 404 and not body["ok"])
    st, body = await call("/api/go2rtc/focus/cam1?profile=x", "cam1")
    check("non-integer profile -> 400", st == 400)
    cd._GO2RTC_READY = False
    st, body = await call("/api/go2rtc/focus/cam1?profile=0", "cam1")
    check("go2rtc not ready -> ok:false", not body["ok"] and "not running" in body["reason"])
    cd._GO2RTC_READY = True
    # 2.6.4: live view is always on. No option exists, and the API answers
    # with no environment setting at all.
    check("2.6.4: the Live View option is gone (no CFG_GO2RTC)", not hasattr(cd, "CFG_GO2RTC"))
    check("2.6.4: GO2RTC_LIVE_VIEW is not set in this test", "GO2RTC_LIVE_VIEW" not in os.environ)
    st, body = await call("/api/go2rtc/focus/cam1?profile=0", "cam1")
    check("2.6.4: live view offered with no option set", st == 200 and body["ok"], str(body))


# ── D. WebSocket proxy, end to end ────────────────────────────────────────────
async def start_proxy_app():
    app = web.Application()
    app.router.add_get("/go2rtc/ws", cd.handle_go2rtc_ws)
    app.router.add_get("/go2rtc/video-rtc.js", cd.handle_go2rtc_player_js)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


async def test_proxy(fake):
    print("\n[D] WebSocket proxy")
    cd.CAMERAS.clear(); cd.CAMERAS["cam1"] = camera()
    cd._GO2RTC_READY = True
    cd._GO2RTC_STREAMS.clear(); cd._GO2RTC_STREAM_CAM.clear()
    cd._GO2RTC_STREAMS["live1"] = "rtsp://x"; cd._GO2RTC_STREAM_CAM["live1"] = "cam1"
    runner, port = await start_proxy_app()
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(f"{base}/go2rtc/ws?src=live1", max_msg_size=0) as ws:
                m1 = await asyncio.wait_for(ws.receive(), 5)
                check("relays go2rtc's first JSON message",
                      m1.type == WSMsgType.TEXT and json.loads(m1.data)["type"] == "mse")
                m2 = await asyncio.wait_for(ws.receive(), 10)
                check("relays a 5 MiB binary frame intact (above aiohttp's 4 MiB default)",
                      m2.type == WSMsgType.BINARY
                      and hashlib.sha256(m2.data).digest() == hashlib.sha256(BIG).digest(),
                      f"type={m2.type} len={len(m2.data) if m2.type == WSMsgType.BINARY else '-'}")
                await ws.send_str('{"type":"webrtc/offer","value":"v=0"}')
                m3 = await asyncio.wait_for(ws.receive(), 5)
                check("relays browser -> go2rtc and the answer back",
                      m3.type == WSMsgType.TEXT
                      and json.loads(m3.data)["value"] == '{"type":"webrtc/offer","value":"v=0"}')
            check("proxy asked go2rtc for the right stream", fake.ws_srcs[-1] == "live1")
            await asyncio.wait_for(fake.ws_closed.wait(), 5)
            check("closing the browser socket closes the upstream socket", fake.ws_closed.is_set())

            async with s.get(f"{base}/go2rtc/ws?src=not_registered") as r:
                check("unregistered stream name -> 404, never proxied", r.status == 404)
            async with s.get(f"{base}/go2rtc/ws?src=") as r:
                check("empty stream name -> 404", r.status == 404)

            async with s.get(f"{base}/go2rtc/video-rtc.js") as r:
                check("player JS: 404 when not installed (no /www on this host)", r.status == 404)
            cd.GO2RTC_PLAYER_JS = REPO / "www" / "video-rtc.js"
            cd._GO2RTC_PLAYER_BYTES = None
            async with s.get(f"{base}/go2rtc/video-rtc.js") as r:
                body = await r.read()
                check("player JS served with a JavaScript MIME type",
                      r.status == 200 and r.content_type == "text/javascript", r.content_type)
                check("player JS body is the vendored file",
                      body == (REPO / "www" / "video-rtc.js").read_bytes())

        await fake.stop()   # go2rtc down
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(f"{base}/go2rtc/ws?src=live1") as ws:
                m = await asyncio.wait_for(ws.receive(), 5)
                err = json.loads(m.data) if m.type == WSMsgType.TEXT else {}
                check("go2rtc unreachable -> explicit error message to the browser",
                      err.get("type") == "error" and err.get("value", "").startswith("anycam:"),
                      str(err))
                m = await asyncio.wait_for(ws.receive(), 5)
                check("...then the socket closes",
                      m.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING))
    finally:
        await runner.cleanup()


# ── E. focus engine state machine ─────────────────────────────────────────────
class FakeProc:
    def __init__(self):
        self.returncode = None
        self.killed = False

    def kill(self):
        self.killed = True


async def test_focus_engine():
    print("\n[E] focus engine")
    started = []

    async def fake_snap_loop(camera_id, url, camera_, native_res=False):
        started.append((camera_id, native_res))
        await asyncio.sleep(1000)

    real_snap_loop = cd.snap_loop
    cd.snap_loop = fake_snap_loop
    try:
        cd.CAMERAS.clear(); cd.CAMERAS["cam1"] = camera()

        async def enter(engine="go2rtc"):
            q = "?engine=go2rtc" if engine == "go2rtc" else ""
            req = make_mocked_request("POST", "/snap/focus/cam1" + q,
                                      match_info={"camera_id": "cam1"})
            return await cd.handle_focus_set(req)

        async def leave():
            return await cd.handle_focus_clear(make_mocked_request("DELETE", "/snap/focus"))

        def reset():
            cd._SNAP.clear(); cd._MOTION.clear(); started.clear()
            cd._FOCUSED_CAMERA = None; cd._FOCUS_ENGINE = None

        # E1: motion off, live thumbnail ffmpeg
        reset()
        st = cd._snap_state("cam1")
        task = asyncio.create_task(asyncio.sleep(1000)); proc = FakeProc()
        st["task"], st["proc"] = task, proc
        await enter()
        check("E1 go2rtc enter: focus + engine recorded",
              cd._FOCUSED_CAMERA == "cam1" and cd._FOCUS_ENGINE == "go2rtc")
        check("E1 motion off: thumbnail ffmpeg killed", proc.killed)
        check("E1 motion off: flag set for the loop to consume",
              st.get("focus_leave_kill") is True)
        check("E1 task not cancelled (EOF path must consume the flag)", not task.cancelled())
        check("E1 no native-res loop started", not started)
        task.cancel()

        # E2: motion off, task alive but no live ffmpeg
        reset()
        st = cd._snap_state("cam1")
        task = asyncio.create_task(asyncio.sleep(1000))
        st["task"], st["proc"] = task, None
        await enter()
        await asyncio.sleep(0)
        check("E2 no live ffmpeg: task cancelled instead", task.cancelled())
        check("E2 no live ffmpeg: NO flag left behind (Bug A shape)",
              "focus_leave_kill" not in st)

        # E3: motion armed, loop running -> untouched
        reset()
        cd._MOTION["cam1"] = {"enabled": True}
        st = cd._snap_state("cam1")
        task = asyncio.create_task(asyncio.sleep(1000)); proc = FakeProc()
        st["task"], st["proc"] = task, proc
        await enter()
        check("E3 motion armed: thumbnail ffmpeg NOT killed", not proc.killed)
        check("E3 motion armed: no flag set", "focus_leave_kill" not in st)
        check("E3 motion armed: task untouched", not task.done())
        task.cancel()

        # E4: motion armed, loop NOT running -> started like handle_snapshot does
        reset()
        cd._MOTION["cam1"] = {"enabled": True}
        before = time.monotonic()
        await enter()
        await asyncio.sleep(0)
        st = cd._SNAP.get("cam1", {})
        check("E4 motion armed, loop stopped: thumbnail loop started",
              started == [("cam1", False)], str(started))
        check("E4 poll timestamp stamped so it is not idled out at once",
              cd._snap_last_access.get("cam1", 0) >= before)
        if st.get("task"):
            st["task"].cancel()

        # E5: leave after go2rtc -> nothing killed, stale flag dropped
        reset()
        await enter()
        st = cd._snap_state("cam1")
        st["focus_leave_kill"] = True             # simulate an unconsumed flag
        proc = FakeProc(); st["proc"] = proc
        await leave()
        check("E5 go2rtc leave: focus + engine cleared",
              cd._FOCUSED_CAMERA is None and cd._FOCUS_ENGINE is None)
        check("E5 go2rtc leave: unconsumed flag dropped", "focus_leave_kill" not in st)
        check("E5 go2rtc leave: nothing killed", not proc.killed)

        # E6: legacy path records its engine and still starts native-res
        reset()
        await enter(engine="legacy")
        await asyncio.sleep(0)
        check("E6 legacy enter: engine 'legacy'", cd._FOCUS_ENGINE == "legacy")
        check("E6 legacy enter: native-res loop started as before",
              started == [("cam1", True)], str(started))
        st = cd._SNAP.get("cam1", {})
        proc = FakeProc(); st["proc"] = proc
        await leave()
        check("E6 legacy leave: unchanged behaviour (sets flag, kills ffmpeg)",
              st.get("focus_leave_kill") is True and proc.killed)
        if st.get("task"):
            st["task"].cancel()
    finally:
        cd.snap_loop = real_snap_loop
        await asyncio.sleep(0)


# ── G. 2.6.5 Enhanced View changes (server side and page) ────────────────────
async def test_265():
    print("\n[G] 2.6.5 Enhanced View")

    async def fake_snap_loop(camera_id, url, camera_, native_res=False):
        await asyncio.sleep(1000)

    real_snap_loop = cd.snap_loop
    cd.snap_loop = fake_snap_loop
    try:
        cd.CAMERAS.clear(); cd.CAMERAS["cam1"] = camera()
        cd._SNAP.clear(); cd._MOTION.clear(); cd._FOCUS_ADAPTIVE.clear()
        cd._FOCUSED_CAMERA = None; cd._FOCUS_ENGINE = None
        st = cd._snap_state("cam1")
        st["frame"], st["frame_time"], st["frame_count"] = b"\xff\xd8thumb\xff\xd9", time.monotonic(), 500
        cd._FOCUS_ADAPTIVE["cam1"] = {"tier_idx": 3, "locked": True,
                                      "restarts_since_lock": 2, "run_start": 1.0}
        await cd.handle_focus_set(make_mocked_request(
            "POST", "/snap/focus/cam1", match_info={"camera_id": "cam1"}))
        ada = cd._FOCUS_ADAPTIVE["cam1"]
        check("G1 entry keeps a learned adaptive lock and resets its restart count",
              ada["tier_idx"] == 3 and ada["locked"] is True
              and ada["restarts_since_lock"] == 0 and ada["run_start"] is None, str(ada))
        _src = repo_source()
        check("G1 3.0.0-rc1.0: manual-tier endpoints, their flag and the old stream endpoint are gone",
              not any(x in _src for x in ("handle_focus_set_tier", "handle_focus_profiles",
                                          "/snap/focus/tier", "/snap/focus/profiles",
                                          "tier_change_kill", "manual_override",
                                          "def handle_stream(", '"/stream/{camera_id}"')))
        check("G1 frame base recorded at entry", st.get("focus_frame_base") == 500)

        async def snap_headers():
            r = await cd.handle_snapshot(make_mocked_request(
                "GET", "/snapshot/cam1?focus=1", match_info={"camera_id": "cam1"}))
            return r.status, r.headers
        status, h = await snap_headers()
        check("G2 thumbnail still in buffer: X-Focus-Frames 0",
              status == 200 and h.get("X-Focus-Frames") == "0", f"{status} {dict(h)}")
        st["frame_count"] = 501
        status, h = await snap_headers()
        check("G2 first frame of the session: X-Focus-Frames 1", h.get("X-Focus-Frames") == "1")
        if st.get("task"):
            st["task"].cancel()

        # A learned (non-manual) lock survives entry, as before.
        cd._FOCUSED_CAMERA = None
        cd._FOCUS_ADAPTIVE["cam1"] = {"tier_idx": 2, "locked": True,
                                      "restarts_since_lock": 1, "run_start": 1.0}
        await cd.handle_focus_set(make_mocked_request(
            "POST", "/snap/focus/cam1", match_info={"camera_id": "cam1"}))
        ada = cd._FOCUS_ADAPTIVE["cam1"]
        check("G3 learned adaptive lock kept", ada["tier_idx"] == 2 and ada["locked"] is True, str(ada))
        st = cd._SNAP["cam1"]
        if st.get("task"):
            st["task"].cancel()
    finally:
        cd.snap_loop = real_snap_loop
        cd._FOCUSED_CAMERA = None; cd._FOCUS_ENGINE = None
        await asyncio.sleep(0)

    html = cd.build_html()
    check("G4 Resolution, Frame Rate and Auto controls removed",
          not any(k in html for k in ("focus-res-sel", "focus-fps-sel", "focusResetAuto()",
                                      ">Auto<", "focus-ctrl-group")))
    check("G4 3.0.1 (C20): the Classic button is gone", "focusUseClassic" not in html
          and 'id="focus-classic-grp"' not in html and ">Classic<" not in html)
    check("G4 'decoded on this device' text removed", "decoded on this device" not in html)
    check("G4 loading message in the overlay",
          'id="focus-loading"' in html and "Loading feed, please wait" in html)
    check("G4 landscape CSS rendered with single braces",
          "#focus-overlay.focus-landscape #focus-bar{display:none}" in html)
    check("G4 card placeholder says Loading feed", "<span>Loading feed, please wait…</span>" in html
          and "<span>Connecting...</span>" not in html)
    check("G4 card: Stream unavailable only after 90 s", "const SNAP_UNAVAILABLE_MS = 90000;" in html)
    check("G4 live view waits 30 s", "const GO2RTC_FIRST_FRAME_MS = 30000;" in html)


# ── H. 2.6.5 motion detection ────────────────────────────────────────────────
async def test_motion():
    print("\n[H] 2.6.5 motion detection")
    started_rec, stopped_rec, loops = [], [], []

    async def fake_start(camera_id, camera_, url):
        started_rec.append(camera_id)
        cd._motion_state(camera_id)["recording"] = True

    async def fake_stop(camera_id):
        stopped_rec.append(camera_id)
        cd._motion_state(camera_id)["recording"] = False

    async def fake_snap_loop(camera_id, url, camera_, native_res=False):
        loops.append(camera_id)
        await asyncio.sleep(1000)

    real = (cd._start_recording, cd._stop_recording, cd.snap_loop, cd.MOTION_FILE)
    cd._start_recording, cd._stop_recording, cd.snap_loop = fake_start, fake_stop, fake_snap_loop
    cd.MOTION_FILE = SCRATCH / "motion_test.json"
    if cd.MOTION_FILE.exists():
        cd.MOTION_FILE.unlink()
    try:
        cd.CAMERAS.clear(); cd.CAMERAS["cam1"] = camera()
        cd._SNAP.clear(); cd._MOTION.clear()
        cd._FOCUSED_CAMERA = None; cd._FOCUS_ENGINE = None
        small, big = SCENE, SCENE_PERSON
        cd.MOTION_COMPARE_S = 0.0   # compare every frame in the test
        cd.MOTION_REF_S = 0.0

        # H1 detection on any frame source
        cd._motion_on_frame("cam1", small)
        check("H1 unarmed: nothing happens", not cd._MOTION)
        ms = cd._motion_state("cam1"); ms["enabled"] = True
        cd._motion_on_frame("cam1", small); cd._motion_on_frame("cam1", small)
        await asyncio.sleep(0)
        check("H1 armed, same picture: no recording", not started_rec)
        cd._motion_on_frame("cam1", big)
        await asyncio.sleep(0)
        check("H1 armed, a person covers 8% of the picture: recording starts", started_rec == ["cam1"])
        ms["last_motion"] = time.monotonic() - (cd.CFG_MOTION_COOL + cd.CFG_MOTION_PAD + 1)
        cd._motion_on_frame("cam1", big)
        await asyncio.sleep(0)
        check("H1 quiet past cooldown + padding: recording stops", stopped_rec == ["cam1"])
        cd._motion_reset_prev("cam1")
        check("H1 reset clears the comparison history", len(ms["refs"]) == 0)

        # H7 the detector itself (2.6.6)
        from PIL import ImageEnhance as _E, Image as _I
        import io as _io

        def enc(i):
            b = _io.BytesIO(); i.save(b, "JPEG", quality=45); return b.getvalue()
        base = _I.open(_io.BytesIO(SCENE)).convert("RGB")
        a = cd._motion_thumb(SCENE)
        f = lambda j: cd._motion_diff(a, cd._motion_thumb(j))[0]
        check("H7 same picture: 0% changed", f(SCENE) == 0.0)
        check("H7 30% brighter: under 1% changed (brightness cancelled)",
              f(enc(_E.Brightness(base).enhance(1.3))) < 0.01)
        check("H7 40% more contrast: under 1% changed", f(enc(_E.Contrast(base).enhance(1.4))) < 0.01)
        check("H7 person block: 6 to 10% changed", 0.06 <= f(SCENE_PERSON) <= 0.10, f"{f(SCENE_PERSON):.3f}")
        check("H7 not a JPEG: no crash, no picture", cd._motion_thumb(b"\xff\xd8garbage") is None)
        check("H7 scale: level 1 = 74%, 63 = 5.0%, 100 = 1%",
              [round(74 * (1 / 74) ** ((lv - 1) / 99), 1) for lv in (1, 63, 100)] == [74.0, 5.0, 1.0])
        import random as _rnd
        _g = _rnd.Random(5)
        px = bytes(_g.randrange(256) for _ in a[0])   # an unrelated picture
        m = sum(px) / len(px)
        fake = (px, m, (sum((v - m) ** 2 for v in px) / len(px)) ** 0.5)
        check("H7 unrelated picture: change spread over more than 75% of the picture",
              cd._motion_diff(a, fake)[1] > 0.75, str(cd._motion_diff(a, fake)))
        check("H7 person block: change spread over 25% or less",
              cd._motion_diff(a, cd._motion_thumb(SCENE_PERSON))[1] <= 0.25,
              str(cd._motion_diff(a, cd._motion_thumb(SCENE_PERSON))))
        check("H7 over 75% changed at once: a light change, not motion",
              cd._motion_judge("cam1", a, fake)[0] == "light")
        check("H7 person over the threshold: motion",
              cd._motion_judge("cam1", a, cd._motion_thumb(SCENE_PERSON))[0] == "motion")

        # H2 keeper
        started_rec.clear(); stopped_rec.clear(); loops.clear()
        ms["recording"] = True; ms["last_motion"] = 0.0
        real_sleep = asyncio.sleep

        async def one_pass(_):
            raise asyncio.CancelledError
        # Run exactly one keeper pass: first sleep returns, second cancels.
        calls = {"n": 0}

        async def sleep_once(t):
            calls["n"] += 1
            if calls["n"] > 1:
                raise asyncio.CancelledError
            await real_sleep(0)
        cd.asyncio.sleep = sleep_once
        try:
            await cd._motion_keeper()
        except asyncio.CancelledError:
            pass
        finally:
            cd.asyncio.sleep = real_sleep
        await real_sleep(0)
        check("H2 keeper stops a recording with no frames arriving (B1)", stopped_rec == ["cam1"])
        check("H2 keeper starts the loop of an armed camera", loops == ["cam1"])
        cd._SNAP["cam1"]["task"].cancel()

        class DeadProc:
            returncode = 1
        ms["recording"], ms["proc"] = True, DeadProc()
        calls["n"] = 0; cd.asyncio.sleep = sleep_once
        try:
            await cd._motion_keeper()
        except asyncio.CancelledError:
            pass
        finally:
            cd.asyncio.sleep = real_sleep
        check("H2 keeper clears a recording whose ffmpeg died", not ms["recording"] and ms["proc"] is None)
        await real_sleep(0)
        if cd._SNAP.get("cam1", {}).get("task"):
            cd._SNAP["cam1"]["task"].cancel()   # the loop this pass restarted

        loops.clear(); cd._SNAP.clear()
        cd._FOCUSED_CAMERA, cd._FOCUS_ENGINE = "cam1", "legacy"
        cd._motion_ensure_loop("cam1")
        check("H2 classic Enhanced View: no second loop", not loops)
        cd._FOCUSED_CAMERA, cd._FOCUS_ENGINE = "cam1", "go2rtc"
        cd._motion_ensure_loop("cam1")
        await real_sleep(0)
        check("H2 live view: loop started beside go2rtc", loops == ["cam1"],
              f"loops={loops} task={cd._SNAP.get('cam1', {}).get('task')}")
        cd._SNAP["cam1"]["task"].cancel()
        cd._FOCUSED_CAMERA = None; cd._FOCUS_ENGINE = None

        # H3 toggle + persistence
        cd._MOTION.clear(); cd._SNAP.clear(); loops.clear()

        async def toggle():
            r = await cd.api_motion_toggle(make_mocked_request(
                "POST", "/api/cameras/cam1/motion", match_info={"camera_id": "cam1"}))
            return json.loads(r.body)
        r = await toggle()
        await real_sleep(0)
        check("H3 arm: loop started at once", r["motion_enabled"] and loops == ["cam1"])
        check("H3 arm: saved", json.loads(cd.MOTION_FILE.read_text())["armed"] == ["cam1"])
        cd._MOTION.clear()
        cd._motion_load()
        check("H3 restart: armed state restored", cd._motion_armed("cam1"))
        cd._SNAP["cam1"]["task"].cancel()
        r = await toggle()
        check("H3 disarm: saved", not r["motion_enabled"]
              and json.loads(cd.MOTION_FILE.read_text())["armed"] == [])
        cd.MOTION_FILE.write_text('{"armed": ["gone_cam"]}')
        cd._MOTION.clear(); cd._motion_load()
        check("H3 restore skips a camera that no longer exists", not cd._MOTION)
    finally:
        cd._start_recording, cd._stop_recording, cd.snap_loop, cd.MOTION_FILE = real
        cd._FOCUSED_CAMERA = None; cd._FOCUS_ENGINE = None
        await asyncio.sleep(0)

    # H4 the real HTTP snapshot loop (the Lorex path), against a local server
    frames = [SCENE, SCENE, SCENE_PERSON, SCENE_PERSON]
    served = {"n": 0}
    cd.MOTION_COMPARE_S = 0.0
    cd.MOTION_REF_S = 0.0

    async def snap(request):
        body = frames[min(served["n"], len(frames) - 1)]
        served["n"] += 1
        return web.Response(body=body, content_type="image/jpeg")
    app = web.Application(); app.router.add_get("/snap.jpg", snap)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
    port = site._server.sockets[0].getsockname()[1]
    seen = []
    real_hook = cd._motion_on_frame
    cd._motion_on_frame = lambda cid, f: (seen.append(len(f)), real_hook(cid, f))
    real_start = cd._start_recording
    rec = []

    async def fake_start2(camera_id, camera_, url):
        rec.append(camera_id)
        cd._motion_state(camera_id)["recording"] = True
    cd._start_recording = fake_start2
    try:
        cd.CAMERAS.clear()
        cam = camera(); cam["id"] = "lorex"; cam["http_snap_url"] = f"http://127.0.0.1:{port}/snap.jpg"
        cam["credentials"] = None
        cd.CAMERAS["lorex"] = cam
        cd._SNAP.clear(); cd._MOTION.clear()
        cd._motion_state("lorex")["enabled"] = True
        cd._snap_last_access["lorex"] = time.monotonic() - 120   # nobody watching
        task = asyncio.create_task(cd.http_snap_loop("lorex", cam))
        await asyncio.sleep(4.5)
        check("H4 http loop: armed camera keeps polling with no viewer",
              not task.done() and served["n"] >= 4, f"served={served['n']} done={task.done()}")
        check("H4 http loop: every frame goes through motion detection", len(seen) >= 4, str(seen))
        await asyncio.sleep(0.1)
        check("H4 http loop: a person in the picture starts a recording (Lorex path)", rec == ["lorex"], str(rec))
        cd._motion_state("lorex")["enabled"] = False
        await asyncio.sleep(2.5)
        check("H4 http loop: disarmed and idle → stops", task.done())
        if not task.done():
            task.cancel()
    finally:
        cd._motion_on_frame = real_hook
        cd._start_recording = real_start
        await runner.cleanup()

    # H6 set-state toggle and the all-cameras endpoint, through a real server
    from aiohttp.test_utils import TestServer, TestClient
    real_loop = cd.snap_loop

    async def idle_loop(camera_id, url, camera_, native_res=False):
        await asyncio.sleep(1000)
    cd.snap_loop = idle_loop
    cd.MOTION_FILE = SCRATCH / "motion_test.json"
    app2 = web.Application()
    app2.router.add_post("/api/cameras/{camera_id}/motion", cd.api_motion_toggle)
    app2.router.add_get("/api/motion", cd.api_motion_all)
    client = TestClient(TestServer(app2)); await client.start_server()
    try:
        cd.CAMERAS.clear(); cd.CAMERAS["cam1"] = camera(); cd.CAMERAS["cam2"] = camera()
        cd._MOTION.clear(); cd._SNAP.clear()
        r = await (await client.get("/api/motion")).json()
        check("H6 nothing armed: empty map", r == {}, str(r))
        await client.post("/api/cameras/cam1/motion", json={"enabled": True})
        await client.post("/api/cameras/cam1/motion", json={"enabled": True})
        check("H6 'enabled: true' twice stays armed (no blind flip)", cd._motion_armed("cam1"))
        r = await (await client.get("/api/motion")).json()
        check("H6 all-cameras map shows cam1 armed, cam2 absent",
              r == {"cam1": {"enabled": True, "recording": False}}, str(r))
        await client.post("/api/cameras/cam1/motion", json={"enabled": False})
        check("H6 'enabled: false' disarms", not cd._motion_armed("cam1"))
        await client.post("/api/cameras/cam1/motion")
        check("H6 no body keeps the old flip", cd._motion_armed("cam1"))
        for st in cd._SNAP.values():
            if st.get("task"):
                st["task"].cancel()
    finally:
        await client.close()
        cd.snap_loop = real_loop
        cd.MOTION_FILE = real[3]

    src = repo_source()
    check("H5 recording ffmpeg stderr is drained", 'asyncio.create_task(_drain_stderr(ms["proc"], f"REC:{camera_id}"))' in src)
    check("H5 all four idle stops keep the loop while it is the motion detector",
          src.count("and not _motion_uses_snapshots(camera_id)") == 4,
          str(src.count("and not _motion_uses_snapshots(camera_id)")))


# ── I. 2.6.6 live cards: which stream a card plays ───────────────────────────
async def test_cards():
    print("\n[I] 2.6.6 live cards")
    from urllib.parse import unquote
    lorex = camera(id="lorex7", ip="192.168.50.217", stream_codec="hevc", stream_width=3840,
                   stream_url="rtsp://192.168.50.217:554/cam/realmonitor?channel=7&subtype=0",
                   stream_profiles=[])
    html = cd.build_html()
    check("B5 community placeholder filled: empty endpoint, box hidden",
          'const COMMUNITY_ENDPOINT = "";' in html and "___COMMUNITY___" not in html)
    import re as _re
    left = sorted(set(_re.findall(r"___[A-Z_]+___", html)))
    check("B5 no build placeholder left anywhere in the page", not left, str(left))
    cd.COMMUNITY_ENDPOINT = "https://x.test/</script><b>"
    try:
        check("B5 an endpoint cannot close the script block",
              "</script><b>" not in cd.build_html().split("<script>", 1)[1].rsplit("</script>", 1)[0])
    finally:
        cd.COMMUNITY_ENDPOINT = ""
    url, codec, reason = cd._go2rtc_card_source(lorex)
    check("I1 Lorex channel (only a 3840-wide main known): the DVR sub-stream",
          url and url.endswith("/cam/realmonitor?channel=7&subtype=1") and reason == "ok", str(url))
    check("I1 ... with the camera password added", url and TRICKY_PASS in unquote(url))
    hik = camera(stream_width=2560,
                 stream_profiles=[{"url": "rtsp://10.0.0.33:554/Streaming/Channels/101",
                                   "stream_codec": "hevc", "stream_width": 2560},
                                  {"url": "rtsp://10.0.0.33:554/Streaming/Channels/102",
                                   "stream_codec": "mjpeg", "stream_width": 704}])
    url, codec, reason = cd._go2rtc_card_source(hik)
    check("I2 Hikvision (2560 main, MJPEG sub): snapshots, with the reason",
          url is None and "2560 wide" in reason, reason)
    oak = camera(id="oak", ip="192.168.50.73", stream_codec="h264",
                 stream_url="rtsp://192.168.50.73:8765/stream", stream_width=1280, stream_profiles=[])
    url, codec, reason = cd._go2rtc_card_source(oak)
    check("I3 Oak-D (1280 H.264, one stream): plays its main stream",
          url and url.endswith("192.168.50.73:8765/stream") and codec == "h264", str(url))
    two = camera(stream_profiles=[{"url": "rtsp://10.0.0.9:554/main", "stream_codec": "h264", "stream_width": 3840},
                                  {"url": "rtsp://10.0.0.9:554/sub", "stream_codec": "h264", "stream_width": 640}])
    url, _, _ = cd._go2rtc_card_source(two)
    check("I4 main 3840 + sub 640: the sub-stream", url and url.endswith("/sub"), str(url))
    check("I5 card and Enhanced View use different go2rtc names",
          cd._go2rtc_stream_name("lorex7", 0, kind="c") != cd._go2rtc_stream_name("lorex7", 0)
          and cd._go2rtc_stream_name("lorex7", 0, kind="c").endswith("_c0"))
    check("I6 non-Dahua URL gets no invented sub-stream",
          cd._dahua_sub_stream("rtsp://10.0.0.33/Streaming/Channels/101") is None)
    ready = cd._GO2RTC_READY
    cd._GO2RTC_READY = False
    try:
        cd.CAMERAS.clear(); cd.CAMERAS["lorex7"] = lorex
        r = await cd.api_go2rtc_card(make_mocked_request(
            "GET", "/api/go2rtc/card/lorex7", match_info={"camera_id": "lorex7"}))
        check("I7 go2rtc not running: the card is told to use snapshots for now",
              json.loads(r.body) == {"ok": False, "reason": "go2rtc is not running", "retry": True})
    finally:
        cd._GO2RTC_READY = ready


# ── K. 2.6.6 tuning line ─────────────────────────────────────────────────────
async def test_tuning_line():
    print("\n[K] 2.6.6 tuning line")
    cd._MOTION.clear()
    ms = cd._motion_state("cam1"); ms["enabled"] = True
    CAP.lines.clear()
    t0 = 1000.0
    cd._motion_report_peak("cam1", ms, t0)            # starts the minute
    for pct in (0.4, 12.3, 3.0):
        cd._motion_note_peak("cam1", pct, False)
    cd._motion_note_peak("cam1", 90.0, True)          # a light change: not the peak
    cd._motion_report_peak("cam1", ms, t0 + 30)
    check("K1 nothing logged before the minute is up", not any("largest change" in l for l in CAP.lines))
    cd._motion_report_peak("cam1", ms, t0 + 61)
    line = next((l for l in CAP.lines if "largest change" in l), "")
    check("K2 minute report: largest change, threshold, comparisons, light count",
          "12.3% of the picture" in line and "would record at sensitivity" in line
          and "4 comparisons" in line and "1 light change(s) ignored" in line, line)
    check("K3 counters reset for the next minute", ms["peak_pct"] == 0.0 and ms["peak_n"] == 0)
    CAP.lines.clear()
    cd._motion_note_peak("cam1", 0.1, False)
    level = []
    real_debug, real_info = cd.log.debug, cd.log.info
    cd.log.debug = lambda m, *a, **k: level.append(("debug", m))
    cd.log.info = lambda m, *a, **k: level.append(("info", m))
    try:
        cd._motion_report_peak("cam1", ms, t0 + 125)
    finally:
        cd.log.debug, cd.log.info = real_debug, real_info
    check("K4 a still scene reports at DEBUG, not INFO", level and level[0][0] == "debug", str(level))
    check("K5 the keeper calls the report for armed cameras",
          "_motion_report_peak(camera_id, ms, now_m)" in repo_source())


# ── L. 2.6.6 per-camera recording settings ──────────────────────────────────
async def test_cam_settings():
    print("\n[L] 2.6.6 per-camera recording settings")
    from aiohttp.test_utils import TestServer, TestClient
    real = (cd.MOTION_FILE, cd.CFG_MOTION_GLOBAL)
    cd.MOTION_FILE = SCRATCH / "motion_test.json"
    if cd.MOTION_FILE.exists():
        cd.MOTION_FILE.unlink()
    cd.CFG_MOTION_GLOBAL = False
    cd._MOTION.clear(); cd._MOTION_CFG.clear()
    cd.CAMERAS.clear(); cd.CAMERAS["ch7"] = camera(id="ch7"); cd.CAMERAS["ch4"] = camera(id="ch4")
    app = web.Application()
    app.router.add_route("*", "/api/cameras/{camera_id}/motion/settings", cd.api_motion_settings)
    client = TestClient(TestServer(app)); await client.start_server()
    url = "/api/cameras/ch7/motion/settings"
    try:
        d = await (await client.get(url)).json()
        check("L1 defaults: 63, cooldown 5, tail 3, 30s, /media/anycam",
              d["settings"] == {"level": 63, "cooldown": 5, "tail": 3, "clip": "30s",
                                "path": "/media/anycam"} and not d["custom"], str(d["settings"]))
        r = await client.post(url, json={"level": 90, "cooldown": 8, "tail": 2, "clip": "1min",
                                         "path": "/media/driveway"})
        d = await r.json()
        check("L2 save: the camera's own values", r.status == 200 and d["custom"]
              and d["settings"]["level"] == 90 and d["settings"]["path"] == "/media/driveway")
        check("L2 other cameras keep the defaults", cd._motion_cfg("ch4")["level"] == 63)
        cfg = cd._motion_cfg("ch7")
        check("L3 effective: threshold from level 90, file length 60 s",
              abs(cfg["area_pct"] - 1.54) < 0.01 and cfg["clip_s"] == 60, str(cfg))
        for bad, why in (({"path": "/data/x"}, "outside /media"),
                         ({"path": "/media/../etc"}, "climbs out of /media"),
                         ({"level": 0}, "level 0"), ({"level": 101}, "level 101"),
                         ({"clip": "7s"}, "unknown length"), ({"cooldown": "x"}, "not a number")):
            r = await client.post(url, json={**cd.MOTION_DEFAULTS, **bad})
            check(f"L4 rejected: {why}", r.status == 400, str(r.status))
        check("L4 a rejected save changes nothing", cd._motion_cfg("ch7")["level"] == 90)
        saved = json.loads(cd.MOTION_FILE.read_text())
        check("L5 saved to motion.json", saved["cameras"]["ch7"]["path"] == "/media/driveway")
        cd._MOTION_CFG.clear(); cd._motion_load()
        check("L5 restored after a restart", cd._motion_cfg("ch7")["level"] == 90)
        d = await (await client.delete(url)).json()
        check("L6 Defaults button: back to defaults", not d["custom"] and d["settings"]["level"] == 63)
        cd.CFG_MOTION_GLOBAL = True
        cd._MOTION_CFG["ch7"] = {"level": 90}
        d = await (await client.get(url)).json()
        check("L7 global on: the Configuration tab's values win",
              d["global"] and d["settings"]["level"] == cd.CFG_MOTION_LEVEL
              and d["settings"]["path"] == cd.CFG_RECORDINGS)
        r = await client.post(url, json=cd.MOTION_DEFAULTS)
        check("L7 global on: saving a camera is refused", r.status == 409)
        cd.CFG_MOTION_GLOBAL = False
        # the live reading, in slider units
        ms = cd._motion_state("ch7"); ms["enabled"] = True
        ms["peak_pct"] = 3.0
        d = await (await client.get(url)).json()
        check("L8 live reading: a 3% movement records at 75 or higher",
              d["armed"] and d["peak_level"] == 75, str(d.get("peak_level")))
        check("L8 inverse of the scale", [cd._motion_level_for_pct(p) for p in (74, 5.0, 1.0, 0.5)]
              == [1, 63, 100, 101])
    finally:
        await client.close()
        cd.MOTION_FILE, cd.CFG_MOTION_GLOBAL = real
        cd._MOTION_CFG.clear()

    # cooldown and tail per camera
    cd._MOTION_CFG["ch7"] = {"cooldown": 20, "tail": 5}
    ms = cd._motion_state("ch7"); now = time.monotonic()
    ms["last_motion"] = now - 22
    check("L9 per-camera cooldown + tail: not quiet at 22 s (20 + 5)", not cd._motion_quiet("ch7", ms, now))
    ms["last_motion"] = now - 26
    check("L9 ... quiet at 26 s", cd._motion_quiet("ch7", ms, now))
    cd._MOTION_CFG.clear()
    src = repo_source()
    check("L10 recording uses the camera's folder and file length",
          'cam_dir  = await _ensure_cam_dir(camera, Path(cfg["path"]))' in src
          and '"-segment_time", str(cfg["clip_s"]),' in src)
    html = cd.build_html()
    check("L11 card: cog opens the settings; Test Stream gone",
          "openCamSettings(" in html and "card-cog" in html and "Test Stream" not in html
          and "testStream" not in html)
    check("L11 slider: 1 to 100, value shown as it moves",
          '<input type="range" id="cs-level" min="1" max="100" step="1" oninput="csLevelShow()">' in html
          and "Less sensitive" in html and "More sensitive" in html)
    check("L12 Identity starts with protocol, IP and port",
          "rows.push(['Protocol'" in html and html.index("rows.push(['Protocol'") < html.index("rows.push(['IP address'")
          < html.index("rows.push(['Port'") < html.index("rows.push(['Manufacturer'"))
    check("L13 test-stream endpoint removed",   # 3.6.0: /api/upload/test is the upload test, not it
          "handle_stream_test" not in src and "/test\"" not in src.replace('"/api/upload/test"', ""))

    # L14 file names: _partNN only for a split event
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        one = {"clip_base": "motion_20261001_120000", "clip_path": d / "motion_20261001_120000_part01.mp4"}
        (d / "motion_20261001_120000_part01.mp4").write_bytes(b"x")
        cd._motion_finish_files("ch7", one)
        check("L14 one file: renamed without the part suffix",
              (d / "motion_20261001_120000.mp4").exists()
              and not (d / "motion_20261001_120000_part01.mp4").exists()
              and one["clip_path"].name == "motion_20261001_120000.mp4")
        three = {"clip_base": "motion_20261001_130000", "clip_path": d / "motion_20261001_130000_part01.mp4"}
        for n in (1, 2, 3):
            (d / f"motion_20261001_130000_part0{n}.mp4").write_bytes(b"x")
        cd._motion_finish_files("ch7", three)
        check("L14 a split event keeps part01 to part03",
              all((d / f"motion_20261001_130000_part0{n}.mp4").exists() for n in (1, 2, 3))
              and not (d / "motion_20261001_130000.mp4").exists())
        CAP.lines.clear()
        cd._motion_finish_files("ch7", {"clip_base": "motion_x", "clip_path": d / "motion_x_part01.mp4"})
        check("L14 no file written: a warning, no crash", any("no file was written" in l for l in CAP.lines))
    html = cd.build_html()
    check("L15 Identity opens from an info icon after the name, not a <details> box",
          "toggleIdentity(" in html and "card-info-btn" in html and "<details class=\"id-section\"" not in html
          and html.index("'</span>'", html.index('class="card-name"')) < html.index("card-info-btn", html.index('class="card-name"')))
    check("L16 lock sits at the end of the button row",
          "'<span class=\"card-lock\">' + credBdg + '</span>'" in html
          and html.index("cardActions(cam, clearBtn, notCamBtn)") < html.index("card-lock\">' + credBdg"))
    check("L17 cards keep their own height (no stretched gaps)", "align-content:start;align-items:start}" in html)


# ── M. 2.6.6 live-stream detection and pre-roll ──────────────────────────────
def ts_pkt(pid, key=False, tag=0):
    """One 188-byte MPEG-TS packet; key marks a video keyframe start."""
    if key:
        head = bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x30, 1, 0x40])
    else:
        head = bytes([0x47, pid >> 8, pid & 0xFF, 0x10])
    body = tag.to_bytes(4, "big")
    return head + body + b"\xff" * (188 - len(head) - len(body))


def ts_tag(pkt):
    off = 6 if pkt[3] & 0x20 else 4
    return int.from_bytes(pkt[off:off + 4], "big")


async def test_preroll():
    print("\n[M] 2.6.6 live-stream detection and 3 s pre-roll")
    # M1 the buffer on its own
    real_pre = cd.MOTION_PREROLL_S
    buf = cd._TsBuffer()
    V, PAT, PMT = cd.TS_VIDEO_PID, 0, cd.TS_PMT_PID
    data = b""
    t = 0.0
    for sec in range(10):                       # a keyframe every 2 s, 1 packet per 0.5 s
        for q in range(4):
            key = (sec % 2 == 0 and q == 0)
            chunk = (ts_pkt(PAT, tag=sec) + ts_pkt(PMT, tag=sec)) if key else b""
            chunk += ts_pkt(V, key=key, tag=sec * 10 + q)
            # odd-sized pieces: packets must survive being split across reads
            for piece in (chunk[:50], chunk[50:]):
                buf.feed(piece, t)
            t += 0.5
    # now t = 20.0; keyframes at 0,4,8,12,16 (sec 0,2,4,6,8 at t = 2*sec)
    pre = buf.preroll()
    pkts = [pre[i:i + 188] for i in range(0, len(pre), 188)]
    first_video = next(p for p in pkts if ((p[1] & 0x1F) << 8 | p[2]) == V)
    check("M1 pre-roll starts with PAT and PMT", ((pkts[0][1] & 0x1F) << 8 | pkts[0][2]) == PAT
          and ((pkts[1][1] & 0x1F) << 8 | pkts[1][2]) == PMT)
    check("M1 ... then a keyframe", first_video[5] & 0x40 and first_video[3] & 0x20)
    check("M1 the newest keyframe at least 3 s old (t=16 at now 20)",
          ts_tag(first_video) == 80 and abs(buf.preroll_seconds(t) - 4.0) < 1e-6,
          f"tag={ts_tag(first_video)} pre={buf.preroll_seconds(t)}")
    check("M1 nothing older is kept", len(buf.gops) == 1)
    check("M1 every packet came through whole", len(pre) % 188 == 0 and not buf.rest)

    # M2 the pipeline, with stand-in ffmpeg processes
    cd.MOTION_PREROLL_S = 0.3
    real_ref, real_exec = cd.MOTION_REF_S, cd.asyncio.create_subprocess_exec
    cd.MOTION_REF_S = 0.05
    real_src = (cd._motion_detect_source, cd._motion_record_source)
    cd._motion_detect_source = lambda cam: "rtsp://det"
    cd._motion_record_source = lambda cam: "rtsp://main"
    sent_at, writers, feeders = {}, [], []
    phase = {"person": False}
    # 2.6.7: the detector reads yuv420p: grey picture, then colour planes.
    colour = bytes([148]) * (2 * 64 * 48 // 4)
    scene, person = (cd._motion_thumb(SCENE)[0] + colour,
                     cd._motion_thumb(SCENE_PERSON)[0] + colour)

    class Stdin:
        def __init__(self, proc):
            self.proc, self.data, self.closed = proc, bytearray(), False

        def write(self, b):
            self.data += b

        async def drain(self):
            pass

        def close(self):
            self.closed = True
            self.proc.returncode = 0
            self.proc.done.set()

    class Proc:
        def __init__(self):
            self.returncode, self.done = None, asyncio.Event()
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
            self.stderr.feed_eof()
            self.stdin = Stdin(self)

        def kill(self):
            if self.returncode is None:
                self.returncode = -9
                self.stdout.feed_eof()
                self.done.set()

        async def wait(self):
            await self.done.wait()
            return self.returncode

    async def feed_detector(proc):
        n = 0
        while proc.returncode is None:
            # 2.6.7: a real stream is never byte-identical picture to picture;
            # exact repeats are ignored as ffmpeg duplicates.
            f = bytearray(person if phase["person"] else scene)
            f[n % 3072] ^= 1
            n += 1
            proc.stdout.feed_data(bytes(f))
            await asyncio.sleep(0.02)

    async def feed_buffer(proc):
        n = 0
        while proc.returncode is None:
            key = n % 5 == 0                          # a keyframe every 0.1 s
            sent_at[n] = time.monotonic()
            chunk = (ts_pkt(PAT) + ts_pkt(PMT)) if key else b""
            proc.stdout.feed_data(chunk + ts_pkt(V, key=key, tag=n))
            n += 1
            await asyncio.sleep(0.02)

    async def fake_exec(*args, **kw):
        p = Proc()
        if "rawvideo" in args:
            feeders.append(asyncio.create_task(feed_detector(p)))
        elif "pipe:1" in args:
            feeders.append(asyncio.create_task(feed_buffer(p)))
        else:
            writers.append((args, p))
        return p
    cd.asyncio.create_subprocess_exec = fake_exec
    import tempfile
    tmp = tempfile.mkdtemp()
    try:
        cd.CAMERAS.clear(); cd.CAMERAS["cam9"] = camera(id="cam9", stream_codec="hevc")
        cd._MOTION.clear(); cd._MOTION_CFG.clear()
        cd._MOTION_CFG["cam9"] = {"cooldown": 1, "tail": 0, "path": tmp}
        ms = cd._motion_state("cam9"); ms["enabled"] = True
        cd._motion_ensure_pipelines("cam9")
        await asyncio.sleep(0.8)
        check("M2 detector watching the live stream", ms.get("detector_live") is True)
        check("M2 buffer holding the main stream", ms.get("buffer_live") is True)
        check("M2 snapshot loops not needed while live", not cd._motion_uses_snapshots("cam9"))
        phase["person"] = True
        await asyncio.sleep(0.4)
        trig = ms.get("last_motion")
        check("M3 a person in the live stream starts a recording", ms["recording"] and len(writers) == 1)
        args, w = writers[0]
        check("M3 writer copies the stream, tags H.265 as hvc1, splits into parts",
              "-c" in args and "copy" in args and "hvc1" in args and "segment" in args)
        got = bytes(w.stdin.data)
        pk = [got[i:i + 188] for i in range(0, len(got), 188)]
        vids = [p for p in pk if ((p[1] & 0x1F) << 8 | p[2]) == V]
        first = vids[0]
        first_t = sent_at[ts_tag(first)]
        check("M3 the file starts with PAT, PMT, then a keyframe",
              ((pk[0][1] & 0x1F) << 8 | pk[0][2]) == PAT and first[5] & 0x40)
        check("M3 ... from before the motion, by at least the pre-roll",
              trig - first_t >= cd.MOTION_PREROLL_S - 0.03, f"{trig - first_t:.3f}s")
        n_before = len(got)
        await asyncio.sleep(0.3)
        check("M3 live packets keep flowing into the recording", len(w.stdin.data) > n_before)
        tags = [ts_tag(p) for p in [bytes(w.stdin.data)[i:i + 188]
                for i in range(0, len(w.stdin.data), 188)]
                if ((p[1] & 0x1F) << 8 | p[2]) == V]
        check("M3 no packet lost or repeated at the hand-over",
              tags == list(range(tags[0], tags[0] + len(tags))), f"{tags[:3]}...{tags[-3:]}")
        phase["person"] = False
        await asyncio.sleep(1.6)
        check("M4 quiet past cooldown: the writer's input is closed, recording ends",
              w.stdin.closed and not ms["recording"] and ms.get("writer") is None)
        ms["enabled"] = False
        await cd._motion_stop_pipelines("cam9")
        check("M5 disarmed: detector and buffer stopped",
              not ms.get("det_task") and not ms.get("buf_task") and not ms.get("detector_live"))
    finally:
        for f in feeders:
            f.cancel()
        await asyncio.gather(*feeders, return_exceptions=True)
        cd.asyncio.create_subprocess_exec = real_exec
        cd._motion_detect_source, cd._motion_record_source = real_src
        cd.MOTION_PREROLL_S, cd.MOTION_REF_S = real_pre, real_ref
        cd._MOTION.clear(); cd._MOTION_CFG.clear()

    lorex = camera(id="l7", stream_width=3840, stream_profiles=[],
                   stream_url="rtsp://192.168.50.217:554/cam/realmonitor?channel=7&subtype=0")
    check("M6 Lorex: detection watches the subtype=1 sub-stream, recording copies subtype=0",
          cd._motion_detect_source(lorex).endswith("channel=7&subtype=1")
          and cd._motion_record_source(lorex).endswith("channel=7&subtype=0"))
    hik = camera(stream_width=2560, stream_profiles=[
        {"url": "rtsp://10.0.0.33:554/Streaming/Channels/101", "stream_codec": "hevc", "stream_width": 2560},
        {"url": "rtsp://10.0.0.33:554/Streaming/Channels/102", "stream_codec": "mjpeg", "stream_width": 704}])
    check("M6 Hikvision: detection may use the MJPEG sub-stream",
          cd._motion_detect_source(hik).endswith("/Channels/102"))


# ── J. 2.6.6 B9 and B10 ──────────────────────────────────────────────────────
async def test_b9_b10():
    print("\n[J] 2.6.6 B9 (child pid warning) and B10 (startup order)")

    class P:
        def __init__(self, exits_after):
            self.returncode, self.killed, self._t = None, False, exits_after

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            if self.returncode is None:
                await asyncio.sleep(self._t)
                self.returncode = 0
            return self.returncode

    exited = P(0.05)                 # ffmpeg after EOF: gone within 50 ms
    await cd._stop_proc(exited, exited_grace=1.0)
    check("B9 exiting ffmpeg is waited for, never signalled", not exited.killed and exited.returncode == 0)
    hung = P(100)                    # still running
    await cd._stop_proc(hung, exited_grace=0.1)
    check("B9 a running ffmpeg is still killed after the grace", hung.killed)
    live = P(100)
    await cd._stop_proc(live)
    check("B9 no grace: killed at once", live.killed)

    import inspect
    src = inspect.getsource(cd.main)
    check("B10 web server starts before the hardware probe",
          src.index("TCPSite(runner") < src.index("await _probe_hw_decoders()"))
    check("B10 go2rtc starts before the web server",
          src.index("_go2rtc_supervisor()") < src.index("TCPSite(runner"))
    check("B10 probe flag set even if the probe fails", "finally:\n        _HW_PROBED.set()" in src)

    launched = []
    real_exec = cd.asyncio.create_subprocess_exec

    async def fake_exec(*a, **k):
        launched.append(a[0])
        raise OSError("test: no ffmpeg")
    cd.asyncio.create_subprocess_exec = fake_exec
    cd._HW_PROBED.clear()
    cd.CAMERAS.clear(); cd.CAMERAS["cam1"] = camera()
    cd._SNAP.clear(); cd._snap_last_access["cam1"] = time.monotonic()
    try:
        task = asyncio.create_task(cd.snap_loop("cam1", "rtsp://x/1", cd.CAMERAS["cam1"]))
        await asyncio.sleep(0.3)
        check("B10 snap loop waits for the hardware probe", not launched and not task.done())
        cd._HW_PROBED.set()
        await asyncio.sleep(0.5)
        check("B10 ... then starts ffmpeg", "ffmpeg" in launched, str(launched))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        cd.asyncio.create_subprocess_exec = real_exec
        cd._HW_PROBED.set()

    ready = cd._GO2RTC_READY
    cd._GO2RTC_READY = False
    try:
        r = await cd.api_go2rtc_card(make_mocked_request(
            "GET", "/api/go2rtc/card/cam1", match_info={"camera_id": "cam1"}))
        check("B10 go2rtc still starting: the card is told to retry", json.loads(r.body).get("retry") is True)
    finally:
        cd._GO2RTC_READY = ready


# ── N. 2.6.7 night boost ─────────────────────────────────────────────────────
async def test_night():
    import datetime as dt
    print("\n[N] 2.6.7 night boost")
    utc = dt.timezone.utc

    def hm(ts):
        return dt.datetime.fromtimestamp(ts, utc).strftime("%H:%M")

    def near(ts, h, m, tol=4):
        t = dt.datetime.fromtimestamp(ts, utc)
        return abs((t.hour * 60 + t.minute) - (h * 60 + m)) <= tol

    d = dt.date(2026, 6, 21)
    ev = cd._sun_events_utc(51.5074, -0.1278, d)                 # London
    check("N1 London 2026-06-21: sunrise 03:43, sunset 20:21 UTC (+/- 4 min)",
          near(ev["sunrise"], 3, 43) and near(ev["sunset"], 20, 21),
          f"{hm(ev['sunrise'])} {hm(ev['sunset'])}")
    ev = cd._sun_events_utc(-33.8688, 151.2093, d)               # Sydney, winter
    check("N1 Sydney: sunset 06:54 UTC, sunrise 21:00 UTC (+/- 4 min)",
          near(ev["sunset"], 6, 54) and near(ev["sunrise"], 21, 0),
          f"{hm(ev['sunrise'])} {hm(ev['sunset'])}")
    ev = cd._sun_events_utc(39.74, -104.99, dt.date(2026, 12, 21))   # Denver, winter
    check("N1 Denver 2026-12-21: sunrise 14:19, sunset 23:39 UTC (+/- 4 min)",
          near(ev["sunrise"], 14, 19) and near(ev["sunset"], 23, 39),
          f"{hm(ev['sunrise'])} {hm(ev['sunset'])}")
    ev = cd._sun_events_utc(69.65, 18.96, d)                     # Tromso, midnight sun
    check("N1 polar day: no sunrise or sunset", ev == {"sunrise": None, "sunset": None}, str(ev))

    check("N2 colour measure: grey planes = 0", cd._motion_chroma(bytes([128]) * 1536) == 0)
    check("N2 colour measure: +-20 off neutral = 20",
          cd._motion_chroma(bytes([148, 108]) * 768) == 20)
    import io as _io
    from PIL import Image as _I
    b = _io.BytesIO(); _I.open(_io.BytesIO(SCENE)).convert("L").convert("RGB").save(b, "JPEG")
    grey, colourful = cd._motion_jpeg_chroma(b.getvalue()), cd._motion_jpeg_chroma(SCENE)
    check("N2 snapshot path: a black-and-white JPEG reads as night",
          grey is not None and grey <= cd.NIGHT_CHROMA_MAX, f"{grey}")
    check("N2 snapshot path: a colour JPEG reads as day",
          colourful is not None and colourful >= cd.DAY_CHROMA_MIN, f"{colourful}")
    check("N2 snapshot path: a broken JPEG gives None", cd._motion_jpeg_chroma(b"junk") is None)

    cd._MOTION.clear(); cd._MOTION_CFG.clear(); cd._HA_LOCATION.clear()
    cd._MOTION_CFG["n1"] = {"level": 80}
    ms = cd._motion_state("n1"); ms["enabled"] = True
    day_area = cd._motion_area_now("n1")
    for t in range(0, 30):
        cd._motion_night_observe("n1", 0.5, 100.0 + t)
    check("N3 black-and-white for 29 s: still day", not ms.get("night"))
    cd._motion_night_observe("n1", 9.0, 130.0)                  # one colour frame
    cd._motion_night_observe("n1", 0.5, 131.0)
    cd._motion_night_observe("n1", 0.5, 150.0)
    check("N3 a colour frame restarts the 30 s hold", not ms.get("night"))
    cd._motion_night_observe("n1", 0.5, 161.5)
    check("N3 black-and-white held 30 s: night", ms.get("night") is True)
    check("N4 night: level 80 works at 95",
          abs(cd._motion_area_now("n1") - cd._motion_area_pct(95)) < 1e-9
          and abs(day_area - cd._motion_area_pct(80)) < 1e-9,
          f"{day_area:.2f}% -> {cd._motion_area_now('n1'):.2f}%")
    for t in range(0, 40):
        cd._motion_night_observe("n1", 3.7, 200.0 + t)          # between the thresholds
    check("N3 colour between the thresholds: no change", ms.get("night") is True)
    cd._motion_night_observe("n1", 8.0, 300.0)
    cd._motion_night_observe("n1", 8.0, 331.0)
    check("N3 colour held 30 s: day again", ms.get("night") is False)

    check("N5 scale past 100 continues: 115 = about 0.52%",
          abs(cd._motion_area_pct(115) - 0.52) < 0.01, f"{cd._motion_area_pct(115):.3f}")
    check("N5 floor 0.4%", cd._motion_area_pct(300) == cd.MOTION_AREA_FLOOR)
    check("N5 day: 0.65% is too small at any setting", cd._motion_level_for_pct(0.65) == 101)
    check("N5 night: 0.65% records from slider 95 (slider units)",
          cd._motion_level_for_pct(0.65, 15) == 95, str(cd._motion_level_for_pct(0.65, 15)))
    check("N5 unchanged in daytime: 5% at 63",
          cd._motion_level_for_pct(5.0) == cd._motion_level_for_pct(5.0, 0) <= 63)
    check("N5 night: 0.3% is under the floor", cd._motion_level_for_pct(0.3, 15) == 101)

    # tuning line and settings payload at night
    ms["night"], ms["chroma"] = True, 0.4
    CAP.lines.clear()
    cd._motion_report_peak("n1", ms, 1000.0)
    cd._motion_note_peak("n1", 0.65, False)
    cd._motion_report_peak("n1", ms, 1061.0)
    line = next((l for l in CAP.lines if "largest change" in l), "")
    check("N6 tuning line: slider units, night +15, colour value",
          "sensitivity 95 or higher" in line and "night +15" in line and "colour 0.4" in line, line)
    pay = cd._motion_settings_payload("n1")
    check("N6 cog payload: night, boost, slider-unit peak",
          pay["night"] is True and pay["night_boost"] == 15 and pay["peak_level"] == 95,
          str({k: pay.get(k) for k in ("night", "night_boost", "peak_level")}))

    # the expectation windows
    sent = []

    async def fake_notify(title, message, nid):
        sent.append((title, message, nid))
    real_notify = cd._ha_notify
    cd._ha_notify = fake_notify
    try:
        cd._HA_LOCATION.update(latitude=51.5074, longitude=-0.1278)
        cd._NIGHT_CHECKED.clear()
        sunset = cd._sun_events_utc(51.5074, -0.1278, d)["sunset"]
        now = sunset + cd.NIGHT_WINDOW_S + 5
        ms["night"], ms["night_note"] = False, None
        ms["observed_since"], ms["observed_last"] = sunset - 2 * 3600, now - 1
        await cd._night_expectation_check("n1", ms, sunset)          # inside the window
        check("N7 nothing reported inside the window", not sent)
        CAP.lines.clear()
        await cd._night_expectation_check("n1", ms, now)
        check("N7 no IR an hour after sunset: HA notification", len(sent) == 1
              and "sunset" in sent[0][1] and sent[0][2] == "anycam_night_n1_sunset", str(sent))
        check("N7 ... a log warning and a cog note",
              any("No switch to night mode" in l for l in CAP.lines) and ms["night_note"])
        await cd._night_expectation_check("n1", ms, now + 10)
        check("N7 reported once per camera and event", len(sent) == 1)
        cd._NIGHT_CHECKED.clear(); ms["night"] = True
        await cd._night_expectation_check("n1", ms, now)
        check("N7 switched as expected: no report, note cleared",
              len(sent) == 1 and ms["night_note"] is None)
        cd._NIGHT_CHECKED.clear(); ms["night"] = False
        ms["observed_since"] = sunset - 10
        await cd._night_expectation_check("n1", ms, now)
        check("N7 not watched through the window: no report", len(sent) == 1)
        cd._NIGHT_CHECKED.clear()
        ms["observed_since"], ms["observed_last"] = sunset - 2 * 3600, now - 600
        await cd._night_expectation_check("n1", ms, now)
        check("N7 not watched lately (detector down): no report", len(sent) == 1)
        sunrise = cd._sun_events_utc(51.5074, -0.1278, d)["sunrise"]
        now = sunrise + cd.NIGHT_WINDOW_S + 5
        cd._NIGHT_CHECKED.clear(); ms["night"] = True
        ms["observed_since"], ms["observed_last"] = sunrise - 2 * 3600, now - 1
        await cd._night_expectation_check("n1", ms, now)
        check("N7 still IR an hour after sunrise: reported",
              len(sent) == 2 and "sunrise" in sent[1][1], str(sent[-1:]))
        # a switch outside the windows is an info line, not a warning
        real_near = cd._near_sun_event
        cd._near_sun_event = lambda now: None
        CAP.lines.clear(); ms["night"] = False; ms["night_cand_t"] = None
        cd._motion_night_observe("n1", 0.3, 500.0)
        cd._motion_night_observe("n1", 0.3, 531.0)
        cd._near_sun_event = real_near
        check("N8 switch outside the windows: logged as unusual",
              any("outside the usual time" in l for l in CAP.lines), str(CAP.lines[-2:]))
    finally:
        cd._ha_notify = real_notify

    # Home Assistant location: retry every 5 min, then twice a day
    answers = [None, {"latitude": 40.0, "longitude": -105.0, "time_zone": "America/Denver"}]
    calls = []

    async def fake_api(method, path, payload=None):
        calls.append((method, path)); return answers.pop(0)
    real_api = cd._ha_api
    cd._ha_api = fake_api
    try:
        cd._HA_LOCATION.clear(); cd._HA_LOC_STATE["next"] = 0.0
        await cd._ha_location_refresh()
        check("N9 no answer: retry in 5 min", not cd._HA_LOCATION
              and 290 < cd._HA_LOC_STATE["next"] - time.monotonic() <= 300)
        await cd._ha_location_refresh()
        check("N9 no second call inside the 5 min", len(calls) == 1)
        cd._HA_LOC_STATE["next"] = 0.0
        await cd._ha_location_refresh()
        check("N9 location received; next refresh in 6 h (2.6.8)",
              cd._HA_LOCATION == {"latitude": 40.0, "longitude": -105.0}
              and 5.9 * 3600 < cd._HA_LOC_STATE["next"] - time.monotonic() <= 6 * 3600
              and calls[-1] == ("GET", "config"))
    finally:
        cd._ha_api = real_api
    os.environ.pop("SUPERVISOR_TOKEN", None)
    check("N9 no Supervisor token: no call", await cd._ha_api("GET", "config") is None)

    src = repo_source()
    cfg = (REPO / "config.yaml").read_text(encoding="utf-8")
    check("N10 config: homeassistant_api true", "homeassistant_api: true" in cfg)
    check("N10 page: cog shows mode and note", 'id="cs-mode"' in src and 'id="cs-night-note"' in src)
    check("N10 detector reads 64x48 yuv420p (4608 bytes a frame)",
          "format=yuv420p" in src and "luma + 2 * (luma // 4)" in src)
    check("N10 keeper runs the check and the location refresh",
          "await _night_expectation_check(camera_id, ms, time.time())" in src
          and "await _ha_location_refresh()" in src)
    cd._MOTION.clear(); cd._MOTION_CFG.clear(); cd._HA_LOCATION.clear()


# ── O. 2.6.7 light hold, single pictures, recording names ───────────────────
async def test_confirm():
    import random as _r
    print("\n[O] 2.6.7 light hold, single-picture changes, recording names")
    real_ref = cd.MOTION_REF_S
    cd.MOTION_REF_S = 1.0          # sections H and M shorten it and leave it so
    W, H = cd.MOTION_GRID
    rnd = _r.Random(11)
    base = bytearray(rnd.randrange(60, 200) for _ in range(W * H))

    def pic(blobs=(), noise=0):
        """The scene, plus bright blocks (x, y, w, h) in grid cells."""
        b = bytearray(base)
        for x, y, w, h in blobs:
            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    b[yy * W + xx] = 255
        if noise:
            b[rnd.randrange(len(b))] ^= 1          # never byte-identical
        return cd._motion_thumb_gray(bytes(b))

    started = []

    async def fake_start(cid, cam, url):
        started.append(cid); cd._MOTION[cid]["recording"] = True
    real_start, real_url = cd._start_recording, cd.build_authenticated_url
    cd._start_recording = fake_start
    cd.build_authenticated_url = lambda cam: "rtsp://x"
    cd.CAMERAS.clear(); cd.CAMERAS["o1"] = {"id": "o1", "name": "Lorex / Dahua DVR/NVR Family ch4",
                                           "ip": "192.168.50.217", "channel": 4}
    level_1pct = 100                                  # 1% of the picture

    def fresh():
        cd._MOTION.clear(); cd._MOTION_CFG.clear(); started.clear()
        cd._MOTION_CFG["o1"] = {"level": level_1pct}
        ms = cd._motion_state("o1"); ms["enabled"] = True
        cd._motion_reset_prev("o1")
        return ms

    async def play(frames, burst=1):
        """Feed pictures as the live detector does; burst = how many arrive at once."""
        for k, f in enumerate(frames, start=1):
            cd._motion_feed("o1", f, 1000.0 + (k // burst) * 0.25 * burst, stream_t=k / 4)
            await asyncio.sleep(0)

    insect = [(10, 10, 20, 3)]                        # 60 cells = 2.0% of 3072
    quiet = lambda n: [pic(noise=1) for _ in range(n)]

    ms = fresh(); CAP.lines.clear()
    await play(quiet(8) + [pic(insect, 1)] + quiet(12))
    check("O1 an insect in one picture: not recorded", not started)
    singles = [l for l in CAP.lines if "in one picture only" in l]
    check("O1 ... logged once as a single-picture change (its echo 1 s later is silent)",
          len(singles) == 1, str(singles))
    for burst in (2, 3):
        ms = fresh()
        await play(quiet(8) + [pic(insect, 1)] + quiet(12), burst=burst)
        check(f"O2 pictures arriving {burst} at a time: still no echo, not recorded", not started)
    ms = fresh()
    await play(quiet(8) + [pic(insect, 1)] * 4 + quiet(8))     # one picture, sent 4 times
    check("O3 ffmpeg repeating the insect picture: not recorded", not started)

    # light hold: a whole-picture change, then the settling pictures after it
    def relit(gain, blob=()):
        b = bytes(min(255, int(v * gain) if i % 7 else 255 - v) for i, v in enumerate(base))
        bb = bytearray(b)
        for x, y, w, h in blob:
            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    bb[yy * W + xx] = 0
        bb[rnd.randrange(len(bb))] ^= 1
        return cd._motion_thumb_gray(bytes(bb))
    walk = [pic([(5 + 4 * k, 20, 4, 10)], 1) for k in range(12)]   # 40 cells, 1.3%, moving
    ms = fresh()
    await play(quiet(8) + walk)
    check("O4 a person moving across 2 pictures in a row: recorded", started == ["o1"])
    ms = fresh()
    await play(quiet(8) + [pic([(5, 5, 12, 10)], 1)] + quiet(8))
    check("O5 one picture 6% changed (a cat dashing past): recorded one picture later",
          started == ["o1"])
    ms = fresh()
    await play(quiet(8) + [pic([(5, 5, 12, 10)], 1), relit(1.0)] + [relit(1.0) for _ in range(12)])
    check("O5 a big change followed by a light change (half-switched picture): not recorded",
          not started)

    ms = fresh(); CAP.lines.clear()
    after = [relit(1.0, [(30, 30, 4, 3)]) for _ in range(3)] + [relit(1.0) for _ in range(12)]
    await play(quiet(8) + after)
    check("O6 a light change, then settling changes inside 2 s: not recorded",
          not started and any("light change" in l for l in CAP.lines))
    ms = fresh()
    await play(quiet(8) + [relit(1.0) for _ in range(11)]
               + [relit(1.0, [(5 + 4 * k, 20, 4, 10)]) for k in range(12)])
    check("O7 a person after the 2 s hold: recorded", started == ["o1"])

    # snapshot path (no stream clock): one comparison a second, no confirmation
    ms = fresh()
    for k, f in enumerate([pic(noise=1), pic(noise=1), pic(insect, 1)]):
        cd._motion_feed("o1", f, 5000.0 + k)
        await asyncio.sleep(0)
    check("O8 snapshot cameras: one comparison a second, records without confirmation",
          started == ["o1"])
    cd._start_recording, cd.build_authenticated_url = real_start, real_url

    check("O9 name tag: Lorex DVR channel -> LorexCH4",
          cd._cam_file_tag({"name": "Lorex / Dahua DVR/NVR Family ch4", "channel": 4, "ip": "192.168.50.217"}) == "LorexCH4")
    check("O9 name tag: Hikvision -> Hikvision33",
          cd._cam_file_tag({"name": "Hikvision DS-2DE4A425IW-DE", "ip": "10.0.0.33"}) == "Hikvision33")
    check("O9 name tag: generic camera -> Camera73",
          cd._cam_file_tag({"name": "Generic IP Camera (192.168.50.73)", "ip": "192.168.50.73"}) == "Camera73",
          cd._cam_file_tag({"name": "Generic IP Camera (192.168.50.73)", "ip": "192.168.50.73"}))
    check("O9 name tag: no name -> Camera",
          cd._cam_file_tag({}) == "Camera")
    src = repo_source()
    check("O10 recordings start with the tag", 'base     = f"{_cam_file_tag(camera)}_{ts}"' in src)
    check("O10 live detector passes the stream clock", "stream_t=frames / MOTION_DETECT_FPS" in src)
    cd._MOTION.clear(); cd._MOTION_CFG.clear(); cd.CAMERAS.clear()
    cd.MOTION_REF_S = real_ref


# ── P. 2.6.7 classic view and a camera that resets connections ──────────────
async def test_acd_classic():
    print("\n[P] 2.6.7 classic view honours the escalated cooldown")
    cid, ip = "p1", "10.9.9.22"
    cam = {"id": cid, "ip": ip, "name": "Hipcam/Microseven", "brand": "Hipcam/Microseven",
           "stream_url": f"rtsp://{ip}:554/11", "stream_codec": "h264",
           "http_snap_url": f"http://{ip}/tmpfs/snap.jpg"}
    starts, waits, fell_back = [], [], []

    class Proc:
        def __init__(self):
            self.returncode = None; self.pid = 4242
            self.stdout = asyncio.StreamReader(); self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader(); self.stderr.feed_eof()
        def kill(self): self.returncode = -9
        def terminate(self): self.returncode = -15
        def send_signal(self, s): self.returncode = -15
        async def wait(self):
            self.returncode = 1 if self.returncode is None else self.returncode
            return self.returncode

    async def fake_exec(*a, **k):
        starts.append(a); return Proc()

    async def fake_wait(ip_, secs, label=""):
        waits.append((ip_, secs, label))

    async def fake_http(cid_, cam_):
        fell_back.append(cid_); cd._FOCUSED_CAMERA = None      # user leaves the view

    real = (cd.asyncio.create_subprocess_exec, cd._throttle_wait_if_needed, cd.http_snap_loop,
            cd._FOCUSED_CAMERA)
    cd.asyncio.create_subprocess_exec = fake_exec
    cd._throttle_wait_if_needed = fake_wait
    cd.http_snap_loop = fake_http
    cd.CAMERAS.clear(); cd.CAMERAS[cid] = cam
    cd._FOCUSED_CAMERA = cid
    cd._snap_last_access[cid] = time.monotonic()
    cd._ACD_ESCALATED[ip] = time.monotonic() + 300
    cd._HW_PROBED.set()
    CAP.lines.clear()
    try:
        await asyncio.wait_for(cd.snap_loop(cid, cam["stream_url"], cam, native_res=True), timeout=20)
        check("P1 every ffmpeg start waits out the cooldown first",
              len(waits) >= 1 and waits[0][0] == ip, str(waits))
        check("P1 one ffmpeg start, not six", len(starts) == 1, f"{len(starts)} starts")
        check("P1 HTTP snapshots at once, with the reason logged",
              fell_back == [cid]
              and any("resetting RTSP connections" in l for l in CAP.lines), str(CAP.lines[-3:]))
    except asyncio.TimeoutError:
        check("P1 classic view finished", False, f"timed out; starts={len(starts)} waits={len(waits)}")
    finally:
        (cd.asyncio.create_subprocess_exec, cd._throttle_wait_if_needed, cd.http_snap_loop,
         cd._FOCUSED_CAMERA) = real
        cd._ACD_ESCALATED.pop(ip, None); cd.CAMERAS.clear()
    check("P2 no cooldown in force: _acd_active is False", cd._acd_active(ip) is False
          and cd._acd_active("") is False)


# ── Q. 2.6.8 a wrong Home Assistant home location (C16) ─────────────────────
async def test_location_check():
    print("\n[Q] 2.6.8 wrong home location")
    ok = cd._ha_location_plausible
    check("Q1 Amsterdam (the default) with a time zone 7 hours away: wrong",
          ok(4.89, "America/New_York") is False and ok(4.89, "America/Chicago") is False)
    check("Q1 real pairs accepted: New York, Chicago, Amsterdam, Sydney, Honolulu",
          all([ok(-97.1, "America/New_York"), ok(-87.6, "America/Chicago"),
               ok(4.89, "Europe/Amsterdam"), ok(151.2, "Australia/Sydney"),
               ok(-157.8, "Pacific/Honolulu")]))
    check("Q1 wide zones accepted: western China, Spain",
          ok(75.9, "Asia/Shanghai") and ok(-8.5, "Europe/Madrid"))
    check("Q1 the date line: Fiji (178 E) and Samoa (-172) accepted",
          ok(178.4, "Pacific/Fiji") and ok(-171.8, "Pacific/Apia"))
    check("Q1 standard time, not summer time: Chicago is -6 all year",
          cd._tz_std_offset_h("America/Chicago") == -6.0)
    check("Q1 unknown zone name: falls back to the add-on's own zone, no crash",
          isinstance(cd._tz_std_offset_h("Bogus/Zone"), float)
          and isinstance(cd._tz_std_offset_h(None), float))

    answers = [{"latitude": 52.37, "longitude": 4.89, "time_zone": "America/New_York"},
               {"latitude": 52.37, "longitude": 4.89, "time_zone": "America/New_York"},
               {"latitude": 49.9, "longitude": -97.1, "time_zone": "America/New_York"}]

    async def fake_api(method, path, payload=None):
        return answers.pop(0)
    real_api = cd._ha_api
    cd._ha_api = fake_api
    cd._MOTION.clear(); cd._MOTION_CFG.clear()
    ms = cd._motion_state("q1"); ms["enabled"] = True
    try:
        cd._HA_LOCATION.clear(); cd._HA_LOCATION.update(latitude=1.0, longitude=1.0)
        cd._HA_LOC_STATE.update(next=0.0, mismatch=False)
        CAP.lines.clear()
        await cd._ha_location_refresh()
        warn = [l for l in CAP.lines if "does not match its time zone" in l]
        check("Q2 wrong location: check off (no location kept), one warning",
              not cd._HA_LOCATION and cd._HA_LOC_STATE["mismatch"] and len(warn) == 1, str(warn))
        check("Q2 ... no sunrise or sunset events, so no notifications",
              cd._sun_events_around(time.time()) == [])
        check("Q2 ... the cog panel shows the note",
              cd._motion_settings_payload("q1")["night_note"] == cd.HA_LOC_NOTE)
        check("Q2 ... asked again in 6 h",
              5.9 * 3600 < cd._HA_LOC_STATE["next"] - time.monotonic() <= 6 * 3600)
        cd._HA_LOC_STATE["next"] = 0.0
        await cd._ha_location_refresh()
        check("Q2 still wrong 6 h later: no second warning",
              len([l for l in CAP.lines if "does not match its time zone" in l]) == 1)
        cd._HA_LOC_STATE["next"] = 0.0
        await cd._ha_location_refresh()
        check("Q3 location corrected: check on again, note gone",
              cd._HA_LOCATION == {"latitude": 49.9, "longitude": -97.1}
              and not cd._HA_LOC_STATE["mismatch"]
              and cd._motion_settings_payload("q1")["night_note"] is None
              and len(cd._sun_events_around(time.time())) >= 4)
    finally:
        cd._ha_api = real_api
        cd._HA_LOCATION.clear(); cd._HA_LOC_STATE.update(next=0.0, mismatch=False)
        cd._MOTION.clear()
    src = repo_source()
    check("Q4 cog help no longer promises SFTP for 2.6.8; since 3.6.0 it points to Upload",
          "SFTP, FTPS or FTP, use Upload" in src and "planned for 2.6.8" not in src)


# ── R. 3.0.0-rc1.0 no credentials in any log line (E4) ──────────────────────
async def test_redaction():
    print("\n[R] 3.0.0-rc1.0 credentials never reach the log")
    r = cd._redact
    check("R1 user:password@ removed",
          r("open rtsp://admin:hunter2@10.0.0.33:554/ch1 ok") == "open rtsp://***@10.0.0.33:554/ch1 ok")
    check("R1 a password with @ and : in it leaves nothing behind",
          "p@ss" not in r("rtsp://admin:p@ss:w0rd@10.0.0.33/x") and "w0rd" not in r("rtsp://admin:p@ss:w0rd@10.0.0.33/x"))
    check("R1 query credentials removed, the rest kept",
          r("GET http://10.0.0.22/snap.jpg?chn=1&user=admin&password=hunter2&q=5")
          == "GET http://10.0.0.22/snap.jpg?chn=1&user=***&password=***&q=5")
    check("R1 usr= and pwd= (Hipcam style) removed",
          r("http://10.0.0.22/tmpfs/snap.jpg?usr=admin&pwd=s3cret") == "http://10.0.0.22/tmpfs/snap.jpg?usr=***&pwd=***")
    check("R1 two URLs on one line, and text after them, handled separately",
          r("a rtsp://u:p@h1/x | b rtsp://h2/y mail me@example.com")
          == "a rtsp://***@h1/x | b rtsp://h2/y mail me@example.com")
    check("R1 nothing to remove: unchanged",
          r("Motion [192.168.50.217_554_ch4]: 1.3% changed (threshold 1.0%)")
          == "Motion [192.168.50.217_554_ch4]: 1.3% changed (threshold 1.0%)")
    check("R2 _strip_creds: bounded to the host part",
          cd._strip_creds("rtsp://admin:p@ss@10.0.0.33/ch1") == "rtsp://10.0.0.33/ch1"
          and cd._strip_creds("rtsp://10.0.0.33/ch1 | note me@example.com") == "rtsp://10.0.0.33/ch1 | note me@example.com")

    import io as _io
    import logging as _logging
    stream = _io.StringIO()
    handler = _logging.StreamHandler(stream)
    handler.addFilter(cd._credential_filter)
    root = _logging.getLogger()
    root.addHandler(handler)
    cd._LOG_BUFFER.clear()
    try:
        cd.log.warning("ffmpeg: rtsp://admin:hunter2@10.0.0.33:554/ch1: Invalid data")
        cd.log.warning("snap %s failed", "http://10.0.0.22/s.jpg?user=admin&password=hunter2")
        _logging.getLogger("aiohttp.client").warning("Cannot connect to rtsp://root:hunter2@10.0.0.9/")
    finally:
        root.removeHandler(handler)
    out = stream.getvalue()
    check("R3 the filter covers AnyCam's lines, %-style arguments and library loggers",
          "hunter2" not in out and out.count("***") == 4, out)
    check("R3 the page's log panel (_LOG_BUFFER) is covered too",
          cd._LOG_BUFFER and not any("hunter2" in e["msg"] for e in cd._LOG_BUFFER))
    check("R3 every root handler carries the filter",
          all(cd._credential_filter in h.filters for h in root.handlers
              if type(h).__name__ in ("StreamHandler", "_BufHandler")),
          str([(type(h).__name__, len(h.filters)) for h in root.handlers]))


# ── S. 3.0.0-rc1.3 probe_http_identity restored (B19) ───────────────────────
async def test_http_identity():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    print("\n[S] 3.0.0-rc1.3 HTTP identity check restored")

    def serve(server_header, body):
        class Handler(BaseHTTPRequestHandler):
            server_version, sys_version = server_header, ""

            def do_GET(self):
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    loop = asyncio.get_running_loop()
    cam = serve("Hipcam RealServer/V1.0", "<html><head><title>Microseven Cameras</title></head></html>")
    plain = serve("nginx", "<html><head><title>Welcome</title></head><body>It works.</body></html>")
    try:
        r = await loop.run_in_executor(None, cd.probe_http_identity, "127.0.0.1", cam.server_port, 2)
        check("S1 a Microseven web page: identified as a camera, brand from CAMERA_DB",
              r["is_camera"] is True and "Hipcam" in r["manufacturer"]
              and "Hipcam" in r["server"] and "Microseven" in r["title"], str(r)[:200])
        r2 = await loop.run_in_executor(None, cd.probe_http_identity, "127.0.0.1", plain.server_port, 2)
        check("S2 an ordinary web page: not a camera, no brand",
              r2["is_camera"] is False and r2["manufacturer"] == "" and r2["server"].startswith("nginx"),
              str(r2)[:200])
        check("S3 the wrapper probe_http_for_camera works again",
              cd.probe_http_for_camera("127.0.0.1", cam.server_port, 2) is True
              and cd.probe_http_for_camera("127.0.0.1", plain.server_port, 2) is False)
    finally:
        cam.shutdown(); plain.shutdown()
    r3 = await loop.run_in_executor(None, cd.probe_http_identity, "127.0.0.1", 9, 1)
    check("S4 nothing listening: an empty result, no error",
          r3["is_camera"] is False and r3["title"] == "" and r3["manufacturer"] == "", str(r3)[:120])
    import ast as _ast
    src = repo_source()
    fns = {n.name: n for n in _ast.parse(src).body
           if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))}
    fp = _ast.get_source_segment(src, fns["_rtsp_options_fingerprint"])
    check("S5 _rtsp_options_fingerprint ends at its return; no unreachable code after it",
          fp.rstrip().endswith("return result") and "Fetch HTTP pages" not in fp)


# ── T. 3.0.0-rc1.4 empty-page text (B21) ────────────────────────────────────
async def test_empty_text():
    print("\n[T] 3.0.0-rc1.4 empty-page text")
    html = cd.build_html()
    check("T1 the page has both texts; the scanning one starts hidden",
          '<p id="empty-idle">No cameras found.<br>Click <strong>Scan Network</strong>' in html
          and '<p id="empty-scanning" style="display:none">No Cameras Found Yet</p>' in html)
    check("T1 the scan status poll sets the text", "_setEmptyText(running);" in html)


# ── U. 3.0.0-rc1.4 the scan ──────────────────────────────────────────────────
# The scan had no tests. These run the REAL run_scan and _probe_host_port; only
# the functions that touch the network are stand-ins (ARP, multicast discovery,
# nmap, and the single-port probers).
class _Swap:
    """Replace add-on functions for a test, and put them back after."""

    def __init__(self, **stubs):
        self.stubs = stubs

    def __enter__(self):
        self.real = {k: getattr(cd, k) for k in self.stubs}
        for k, v in self.stubs.items():
            setattr(cd, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.real.items():
            setattr(cd, k, v)


async def test_scan():
    print("\n[U] 3.0.0-rc1.4 the scan")
    calls = {"probe": [], "nmap": [], "identity": [], "fp": [], "find": [], "saved": 0}

    def nmap(ips):
        calls["nmap"].append(list(ips))
        return [
            {"ip": "10.0.0.22", "hostname": "cam22", "mac_addr": "", "mac_vendor": "",
             "open_ports": [{"port": 554, "service": "rtsp", "product": ""}]},
            {"ip": "10.0.0.33", "hostname": "cam33", "mac_addr": "", "mac_vendor": "",
             "open_ports": [{"port": 80, "service": "http", "product": ""},
                            {"port": 554, "service": "rtsp", "product": ""},
                            {"port": 8000, "service": "http", "product": ""}]},
            {"ip": "10.0.0.50", "hostname": "printer", "mac_addr": "", "mac_vendor": "",
             "open_ports": [{"port": 80, "service": "http", "product": "HP printer httpd"}]},
        ]

    async def probe(ip, port, hostname, initial, prev, verdict, reason, loop, host_meta=None):
        calls["probe"].append((ip, port, initial, verdict, bool(host_meta.get("host_skip_layer1_alt"))))

        def card(proto, status, url=""):
            return {"id": f"{ip}_{port}", "ip": ip, "hostname": hostname, "port": port,
                    "protocol": proto, "stream_url": url, "name": hostname, "status": status,
                    "user_saved": False}
        if (ip, port) == ("10.0.0.22", 554):
            return card("RTSP", "ready", "rtsp://10.0.0.22:554/11")
        if (ip, port) == ("10.0.0.33", 554):
            host_meta["rtsp_speaker_confirmed"] = True
            return card("RTSP", "needs_credentials")
        if ip == "10.0.0.33":
            return card("HTTP", "needs_credentials")
        return None

    def identity(ip, port, timeout=5):
        calls["identity"].append((ip, port))
        return {"is_camera": True, "title": "", "server": "Hipcam RealServer/V1.0",
                "manufacturer": "Hipcam/Microseven", "notes": ""}

    def fingerprint(host, port):
        calls["fp"].append((host, port))
        return {"error": "refused", "looks_like_rtsp": False}

    def find(ip, port, username="", password="", host_meta=None):
        calls["find"].append((ip, port))
        return None

    def saved():
        calls["saved"] += 1

    world = _Swap(
        get_local_subnet=lambda: "10.0.0.0/26", get_default_gateway=lambda: "10.0.0.1",
        discover_live_hosts=lambda subnet: {"10.0.0.1", "10.0.0.22", "10.0.0.33", "10.0.0.50",
                                            "10.0.0.60", "172.30.32.1", "169.254.1.1"},
        onvif_discover=lambda timeout: [
            {"ip": "10.0.0.33", "name": "CAM33", "onvif_scopes": "",
             "xaddrs": "http://10.0.0.33/onvif/device_service"},
            {"ip": "10.0.0.70", "name": "IPCAM", "onvif_scopes": "",
             "xaddrs": "http://10.0.0.70:8080/onvif/device_service"}],
        ssdp_discover=lambda timeout: [], mdns_discover=lambda timeout: [],
        broad_nmap_scan=lambda ips: [], focused_nmap_scan=nmap, _probe_host_port=probe,
        probe_http_identity=identity, _rtsp_options_fingerprint=fingerprint,
        find_rtsp_path=find, save_cameras=saved)
    keep = (dict(cd.CAMERAS), set(cd.BLACKLIST), cd.SCAN_OPTIONS.get("broad_sweep"))
    cd.CAMERAS.clear(); cd.BLACKLIST.clear(); cd.BLACKLIST.add("10.0.0.33_8000")
    cd.CAMERAS["manual_1"] = {"id": "manual_1", "ip": "10.0.0.99", "user_saved": True,
                              "status": "ready", "protocol": "RTSP", "port": 554}
    cd.CAMERAS["stale_1"] = {"id": "stale_1", "ip": "10.0.0.98", "user_saved": False,
                             "status": "ready", "protocol": "RTSP", "port": 554}
    cd.SCAN_OPTIONS["broad_sweep"] = False
    try:
        with world:
            await cd.run_scan()
        cams = cd.CAMERAS
        check("U1 the port scan gets every live host, sorted; not the gateway, Docker or link-local addresses",
              calls["nmap"] == [["10.0.0.22", "10.0.0.33", "10.0.0.50", "10.0.0.60", "10.0.0.70"]],
              str(calls["nmap"]))
        check("U2 each open port is probed; the RTSP port first; a blacklisted port is skipped",
              [c[:2] for c in calls["probe"]] == [("10.0.0.22", 554), ("10.0.0.33", 554),
                                                  ("10.0.0.33", 80), ("10.0.0.50", 80)],
              str(calls["probe"]))
        check("U2 after the RTSP port answers, the host's other ports skip the RTSP path walk",
              calls["probe"][2] == ("10.0.0.33", 80, "HTTP", "camera", True)
              and calls["probe"][1][4] is False, str(calls["probe"][1:3]))
        check("U2 a printer is classified before it is probed",
              calls["probe"][3][3] == "not_camera", str(calls["probe"][3]))
        check("U3 result: saved cameras stay, unsaved old cards go, new cards arrive",
              sorted(cams) == ["10.0.0.22_554", "10.0.0.33_554", "10.0.0.70_onvif", "manual_1"],
              str(sorted(cams)))
        check("U3 one card per device: the HTTP card of a device with an RTSP card is dropped",
              "10.0.0.33_80" not in cams and cams["10.0.0.33_554"]["status"] == "needs_credentials")
        check("U4 a device found by both the port scan and ONVIF: its card gets the ONVIF address",
              cams["10.0.0.33_554"].get("onvif") is True
              and cams["10.0.0.33_554"]["xaddrs"] == "http://10.0.0.33/onvif/device_service")
        o = cams["10.0.0.70_onvif"]
        check("U5 a device found by ONVIF only: a card that asks for a password",
              o["protocol"] == "ONVIF" and o["status"] == "needs_credentials"
              and o["requires_credentials"] is True and o["verdict_reason"] == "ONVIF discovered")
        check("U5 ... its brand comes from its web page (probe_http_identity, restored in 3.0.0-rc1.3)",
              calls["identity"] == [("10.0.0.70", 80)] and o["manufacturer"] == "Hipcam/Microseven"
              and o["server_header"] == "Hipcam RealServer/V1.0", str(calls["identity"]))
        check("U5 ... then one RTSP fingerprint and one path search on port 554",
              calls["fp"] == [("10.0.0.70", 554)] and calls["find"] == [("10.0.0.70", 554)])
        st = cd.SCAN_STATE
        check("U6 the scan ends: not running, 100%, counts in the message, cameras saved once",
              st["running"] is False and st["progress"] == 100 and st["stage"] == 0
              and st["message"] == "Scan complete — 4 device(s), 2 streaming." and calls["saved"] == 1,
              str({k: st[k] for k in ("running", "progress", "message")}))
        check("U6 the host list for the port scanner: probed hosts, then silent ones",
              [h["ip"] for h in cd.ARP_HOSTS] == ["10.0.0.22", "10.0.0.33", "10.0.0.50",
                                                 "10.0.0.60", "10.0.0.70"])
        check("U6 the waiting list of new cards is empty again", cd.PENDING_CAMERAS is None)

        def boom(subnet):
            raise RuntimeError("no network")
        real_error = cd.log.error
        cd.log.error = lambda *a, **k: None          # the scan logs the failure with its trace
        try:
            with _Swap(get_local_subnet=lambda: "10.0.0.0/26", get_default_gateway=lambda: "10.0.0.1",
                       discover_live_hosts=boom, onvif_discover=lambda t: [], ssdp_discover=lambda t: [],
                       mdns_discover=lambda t: [], save_cameras=saved):
                await cd.run_scan()
        finally:
            cd.log.error = real_error
        check("U7 a failure inside the scan: reported in the status, the scan is not left running",
              cd.SCAN_STATE["running"] is False and cd.SCAN_STATE["message"] == "Scan error: no network"
              and cd.PENDING_CAMERAS is None, cd.SCAN_STATE["message"])
        check("U7 ... and the saved camera is still there", "manual_1" in cd.CAMERAS)
    finally:
        cd.CAMERAS.clear(); cd.CAMERAS.update(keep[0])
        cd.BLACKLIST.clear(); cd.BLACKLIST.update(keep[1])
        cd.SCAN_OPTIONS["broad_sweep"] = keep[2]
        cd.SCAN_STATE.update(running=False, progress=0, message="Idle. Click Scan to begin.")

    # ── the port prober ──────────────────────────────────────────────────────
    fp_ok = {"looks_like_rtsp": True, "status": 401, "server_header": "", "auth_realm": "Login",
             "auth_scheme": "Digest", "public_methods": ["OPTIONS", "DESCRIBE"], "elapsed_ms": 5}

    async def port(port_no, initial, verdict, w, ip="10.0.0.22"):
        order = []

        async def details(url, *a, **k):
            order.append("details")
            return {}
        meta = {"ip": ip, "hostname": "cam"}
        with _Swap(
                find_rtsp_path=lambda i, p, username="", password="", host_meta=None:
                    (order.append("find") or w.get("rtsp")),
                probe_rtmp=lambda i, p: (order.append("rtmp") or w.get("rtmp", False)),
                probe_mjpeg_http=lambda i, p, u, pw: (order.append("mjpeg") or w.get("mjpeg")),
                probe_hls=lambda i, p, u, pw: (order.append("hls") or w.get("hls")),
                probe_webrtc=lambda i, p: (order.append("webrtc") or w.get("webrtc")),
                probe_ws_rtsp=lambda i, p: (order.append("ws") or w.get("ws")),
                _rtsp_options_fingerprint=lambda h, p: (order.append("fp") or w.get("fp", {"error": "refused"})),
                get_local_ip=lambda: "10.0.0.24", probe_stream_details=details):
            cam = await cd._probe_host_port(ip, port_no, "cam", initial, {}, verdict, "",
                                            asyncio.get_running_loop(), host_meta=meta)
        return cam, order, meta

    cam, order, meta = await port(554, "RTSP", "camera", {"rtsp": "rtsp://10.0.0.22:554/11", "fp": fp_ok})
    check("U8 RTSP port, a stream opens without a password: a ready card",
          cam["id"] == "10.0.0.22_554" and cam["protocol"] == "RTSP" and cam["status"] == "ready"
          and cam["stream_url"] == "rtsp://10.0.0.22:554/11" and cam["requires_credentials"] is False
          and order == ["fp", "find", "details"], f"{cam and cam.get('status')} {order}")
    check("U8 ... the fingerprint marks the host as an RTSP speaker for its other ports",
          meta.get("rtsp_speaker_confirmed") is True and meta.get("rtsp_auth_realm") == "Login"
          and meta.get("rtsp_public_methods") == "OPTIONS,DESCRIBE", str(meta))
    cam, order, meta = await port(554, "RTSP", "camera", {"rtsp": None, "fp": fp_ok})
    check("U9 RTSP port, no stream without a password: a card that asks for one",
          cam["status"] == "needs_credentials" and cam["requires_credentials"] is True
          and cam["stream_url"] == "" and order == ["fp", "find"], f"{cam and cam.get('status')} {order}")
    cam, order, _ = await port(80, "HTTP", "camera", {"mjpeg": "http://10.0.0.22:80/video.mjpg"})
    check("U10 web port with an MJPEG stream: a ready MJPEG card, found first",
          cam["protocol"] == "MJPEG" and cam["status"] == "ready" and cam["display"] == "mjpeg"
          and order == ["mjpeg", "details"], f"{cam and cam.get('protocol')} {order}")
    cam, order, _ = await port(8080, "HTTP", "unknown", {"hls": "http://10.0.0.22:8080/live.m3u8"})
    check("U10 web port with an HLS stream: a ready HLS card",
          cam["protocol"] == "HLS" and cam["display"] == "hls" and order == ["mjpeg", "hls", "details"])
    cam, order, _ = await port(80, "HTTP", "camera", {})
    check("U11 web port of a camera, no stream found: every prober tried in order, then a password card",
          cam["protocol"] == "HTTP" and cam["status"] == "needs_credentials"
          and order == ["mjpeg", "hls", "find", "webrtc", "ws"], f"{cam and cam.get('status')} {order}")
    cam, order, _ = await port(80, "HTTP", "not_camera", {})
    check("U11 the same on a device that is not a camera: no card", cam is None, str(cam))
    cam, order, _ = await port(1935, "RTMP", "camera", {"rtmp": True})
    check("U12 RTMP port: a ready RTMP card", cam["protocol"] == "RTMP" and cam["status"] == "ready"
          and cam["stream_url"] == "rtmp://10.0.0.22:1935/live/stream" and order == ["rtmp"])

    # ── the scan API ─────────────────────────────────────────────────────────
    app = web.Application()
    app.router.add_post("/api/scan/cancel", cd.api_scan_cancel)
    app.router.add_get("/api/scan/status", cd.api_scan_status)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(base + "/api/scan/status") as r:
                body = await r.json()
            check("U13 status: the scan state as JSON", r.status == 200 and body["running"] is False
                  and "message" in body and "progress" in body, str(body)[:120])
            async with sess.post(base + "/api/scan/cancel") as r:
                check("U13 cancel with no scan running: refused", r.status == 400)
    finally:
        await runner.cleanup()

    # ── 3.0.0-rc1.5 (B22): Cancel stops the scan ────────────────────────────
    probed, identity_calls = [], []
    gate = asyncio.Event()

    async def slow_probe(ip, port, hostname, initial, prev, verdict, reason, loop, host_meta=None):
        probed.append((ip, port))
        if len(probed) == 1:
            gate.set()                         # the test presses Cancel now
            await asyncio.sleep(0.05)
        return None

    two_hosts = lambda ips: [
        {"ip": "10.0.0.22", "hostname": "a", "mac_addr": "", "mac_vendor": "",
         "open_ports": [{"port": 554, "service": "rtsp", "product": ""},
                        {"port": 80, "service": "http", "product": ""}]},
        {"ip": "10.0.0.33", "hostname": "b", "mac_addr": "", "mac_vendor": "",
         "open_ports": [{"port": 554, "service": "rtsp", "product": ""}]}]
    req = make_mocked_request("POST", "/api/scan/cancel")
    with _Swap(get_local_subnet=lambda: "10.0.0.0/26", get_default_gateway=lambda: "10.0.0.1",
               discover_live_hosts=lambda subnet: {"10.0.0.22", "10.0.0.33"},
               onvif_discover=lambda t: [{"ip": "10.0.0.70", "name": "X", "onvif_scopes": "",
                                          "xaddrs": "http://10.0.0.70/onvif/device_service"}],
               ssdp_discover=lambda t: [], mdns_discover=lambda t: [], broad_nmap_scan=lambda i: [],
               focused_nmap_scan=two_hosts, _probe_host_port=slow_probe,
               probe_http_identity=lambda *a, **k: identity_calls.append(a) or {},
               save_cameras=lambda: None):
        cd.SCAN_STATE["running"] = True        # api_scan sets this before it starts the task
        task = asyncio.create_task(cd.run_scan())
        await gate.wait()
        r = await cd.api_scan_cancel(req)
        await task
    check("U14 Cancel during the port probing: answered 'cancelling'",
          r.status == 200 and json.loads(r.body)["status"] == "cancelling")
    check("U14 ... no port is probed after it, and no ONVIF-only device",
          probed == [("10.0.0.22", 554)] and identity_calls == [], str(probed))
    check("U14 ... the scan ends and says it was cancelled",
          cd.SCAN_STATE["running"] is False
          and cd.SCAN_STATE["message"].startswith("Scan cancelled"), cd.SCAN_STATE["message"])
    check("U14 ... and the next scan starts with the flag cleared", cd.SCAN_CANCELLED is False)

    nmap_called = []

    def slow_arp(subnet):
        cd.SCAN_CANCELLED = True               # Cancel pressed during discovery
        return {"10.0.0.22"}
    with _Swap(get_local_subnet=lambda: "10.0.0.0/26", get_default_gateway=lambda: "10.0.0.1",
               discover_live_hosts=slow_arp, onvif_discover=lambda t: [], ssdp_discover=lambda t: [],
               mdns_discover=lambda t: [], focused_nmap_scan=lambda ips: nmap_called.append(ips) or [],
               save_cameras=lambda: None):
        await cd.run_scan()
    check("U15 Cancel during discovery: no port scan, and the status says cancelled",
          nmap_called == [] and cd.SCAN_STATE["message"] == "Scan cancelled"
          and cd.SCAN_STATE["running"] is False, cd.SCAN_STATE["message"])
    cd.SCAN_STATE.update(running=False, progress=0, message="Idle. Click Scan to begin.")


# ── V. 3.0.0-rc1.5 the manufacturer database and brand identification ───────
# Written before these functions left camera_discovery.py (build plan E1).
async def test_brand():
    print("\n[V] 3.0.0-rc1.5 manufacturer database and brand identification")
    check("V1 a MAC address in any form gives its OUI key",
          cd._oui_key("1c-c3-16-aa-bb-cc") == "1C:C3:16" and cd._oui_key("1C:C3:16:AA:BB:CC") == "1C:C3:16"
          and cd._oui_key("") == "" and cd._oui_key("1c:c3") == "")
    saved_db = dict(cd._OUI_DB)
    cd._OUI_DB.clear()
    cd._OUI_DB.update({"AA:00:01": "Microseven Inc", "AA:00:02": "Cisco Systems, Inc",
                       "AA:00:03": "Acme Widgets"})
    try:
        check("V2 lookup_oui: the downloaded table first",
              cd.lookup_oui("aa:00:01:12:34:56") == "Microseven Inc")
        check("V2 ... then the built-in camera and non-camera lists",
              cd.lookup_oui("1C:C3:16:00:00:01") == "(known camera manufacturer)"
              and cd.lookup_oui("00:00:0C:00:00:01") == "(known non-camera device)")
        check("V2 ... an unknown or bad address gives nothing",
              cd.lookup_oui("02:00:00:00:00:01") == "" and cd.lookup_oui("x") == "")
        check("V3 oui_is_camera: a vendor that matches a camera alias",
              cd.oui_is_camera("AA:00:01:00:00:00") is True)
        check("V3 ... a vendor with a non-camera word", cd.oui_is_camera("AA:00:02:00:00:00") is False)
        check("V3 ... the built-in lists when the vendor is unknown",
              cd.oui_is_camera("1C:C3:16:00:00:01") is True and cd.oui_is_camera("00:00:0C:00:00:01") is False)
        check("V3 ... nothing known: None",
              cd.oui_is_camera("AA:00:03:00:00:00") is None and cd.oui_is_camera("") is None)
    finally:
        cd._OUI_DB.clear(); cd._OUI_DB.update(saved_db)

    cache = SCRATCH / "oui_cache.json"
    cache.write_text(json.dumps({"AB:CD:EF": "Test Vendor"}))
    import urllib.request as _ur
    real = (cd.OUI_CACHE_FILE, cd.DATA_DIR, cd._OUI_DB_LOADED, dict(cd._OUI_DB), _ur.urlopen)
    cd.OUI_CACHE_FILE, cd.DATA_DIR, cd._OUI_DB_LOADED = cache, SCRATCH, False
    cd._OUI_DB.clear()
    fetched = []

    class _Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    csv_text = ("Registry,Assignment,Organization Name,Organization Address\n"
                "MA-L,0A0B0C,Example Cameras Ltd,Somewhere\nMA-L,bad,Short,x\nMA-L\n")
    _ur.urlopen = lambda req, timeout=30: (fetched.append(req.full_url), _Resp(csv_text.encode()))[1]
    try:
        cd.load_oui_db()
        check("V4 load_oui_db reads the cache file",
              cd._OUI_DB.get("AB:CD:EF") == "Test Vendor" and cd._OUI_DB_LOADED is True)
        cache.write_text(json.dumps({"AB:CD:EF": "Changed"}))
        cd.load_oui_db()
        check("V4 ... once only", cd._OUI_DB["AB:CD:EF"] == "Test Vendor")
        await cd.refresh_oui_db()
        check("V5 refresh_oui_db: a cache under 30 days old is not downloaded again", fetched == [])
        os.utime(cache, (time.time() - 40 * 86400,) * 2)
        await cd.refresh_oui_db()
        check("V5 ... an old cache is downloaded from the IEEE address, parsed and saved",
              fetched == [cd.OUI_CSV_URL]
              and json.loads(cache.read_text()) == {"0A:0B:0C": "Example Cameras Ltd"}
              and cd._OUI_DB.get("0A:0B:0C") == "Example Cameras Ltd", f"{fetched} {cache.read_text()[:80]}")
        os.utime(cache, (time.time() - 40 * 86400,) * 2)

        def broken(req, timeout=30):
            raise OSError("test: offline")
        _ur.urlopen = broken
        CAP.lines.clear()
        await cd.refresh_oui_db()
        check("V5 ... a failed download keeps the old cache and says so",
              json.loads(cache.read_text()) == {"0A:0B:0C": "Example Cameras Ltd"}
              and any("OUI DB download failed" in l for l in CAP.lines))
    finally:
        cd.OUI_CACHE_FILE, cd.DATA_DIR, cd._OUI_DB_LOADED = real[0], real[1], real[2]
        cd._OUI_DB.clear(); cd._OUI_DB.update(real[3])
        _ur.urlopen = real[4]

    check("V6 identify_manufacturer: the bare brand name picks the camera entry, not the NVR",
          (cd.identify_manufacturer("Hikvision web") or {}).get("name") == "Hikvision")
    check("V6 ... a series name picks the NVR entry",
          (cd.identify_manufacturer("hikvision DS-7608 turbo hd") or {}).get("name") == "Hikvision NVR")
    check("V6 ... a short keyword needs word boundaries",
          cd._kw_matches("acti", "interactive printer") is False and cd._kw_matches("acti", "acti camera") is True)
    check("V6 ... nothing known: None", cd.identify_manufacturer("hp laserjet") is None)
    check("V6 the keyword table holds each entry once per keyword",
          [e["name"] for e in cd._DB_ENTRIES_BY_KEY["hikvision"]] == ["Hikvision", "Hikvision NVR"]
          and "microseven" in cd._DB_MANUFACTURERS)

    cam = {"manufacturer": "Hikvision", "mac_vendor": "Microseven Inc"}
    e = cd._identify_camera_brand(cam)
    check("V7 _identify_camera_brand: a brand already set is kept",
          e["name"] == "Hikvision" and cam["manufacturer"] == "Hikvision")
    e = cd._identify_camera_brand(cam, force=True)
    check("V7 ... force=True identifies it again, from the OUI vendor",
          e["name"] == "Hipcam/Microseven" and cam["manufacturer"] == "Hipcam/Microseven")
    cam = {"rtsp_auth_realm": "Login to " + "0123456789abcdef" * 2, "page_title": "Hikvision"}
    check("V7 ... an RTSP realm match wins over the page text",
          cd._identify_camera_brand(cam)["name"] == "Lorex / Dahua DVR-NVR Family"
          and cam["manufacturer"] == "Lorex / Dahua DVR-NVR Family")
    cam = {"name": "webcam"}
    check("V7 ... the generic entry sets no brand",
          cd._identify_camera_brand(cam) is None and "manufacturer" not in cam, str(cam))
    check("V7 ... nothing to go on: None", cd._identify_camera_brand({}) is None)
    check("V7 the throttle comes from the brand",
          cd._brand_throttle_seconds({"mac_vendor": "Microseven Inc"}) == 5.0
          and cd._brand_throttle_seconds({"name": "Hikvision"}) == 0.0)


# ── W. 3.0.0-rc1.5 the page builder ─────────────────────────────────────────
async def test_page_builder():
    print("\n[W] 3.0.0-rc1.5 the page builder")
    html = cd.build_html()
    check("W1 a complete document", html.startswith("<!DOCTYPE html>") and html.rstrip().endswith("</html>"))
    check("W1 every placeholder filled",
          not any(p in html for p in ("___BASE___", "___UNRESTRICTED___",
                                      "___ADAPTIVE_QUALITY___", "___COMMUNITY___")))
    check("W1 the page script is in the page, with the ingress path",
          f"const BASE = '{os.environ['INGRESS_PATH']}';" in html
          and cd._JS[-300:] in html and cd._JS[2000:2300] in html)
    check("W1 CSS braces are single", "*,*::before,*::after{box-sizing:border-box" in html
          and "{{" not in html)
    real = (cd.CFG_UNRESTRICTED_BROWSER, cd.CFG_ADAPTIVE_QUALITY, cd.COMMUNITY_ENDPOINT, cd.HTML, cd.build_html)
    try:
        cd.CFG_UNRESTRICTED_BROWSER, cd.CFG_ADAPTIVE_QUALITY = True, True
        cd.COMMUNITY_ENDPOINT = "https://x.example/</script>"
        h2 = cd.build_html()
        check("W2 the settings reach the script",
              "const CFG_UNRESTRICTED_BROWSER = true;" in h2 and "const CFG_ADAPTIVE_QUALITY     = true;" in h2
              and "const STORAGE_UNRESTRICTED = true;" in h2)
        check("W2 the community address is a JSON string with < escaped",
              'const COMMUNITY_ENDPOINT = "https://x.example/\\u003c/script>";' in h2)
        builds = []

        def counting():
            builds.append(1)
            return "<html>built</html>"
        cd.build_html, cd.HTML = counting, None
        r1 = await cd.handle_index(make_mocked_request("GET", "/"))
        r2 = await cd.handle_index(make_mocked_request("GET", "/"))
        check("W3 handle_index builds the page once and serves it as HTML",
              builds == [1] and r1.text == r2.text == "<html>built</html>"
              and r1.content_type == "text/html")
    finally:
        (cd.CFG_UNRESTRICTED_BROWSER, cd.CFG_ADAPTIVE_QUALITY, cd.COMMUNITY_ENDPOINT,
         cd.HTML, cd.build_html) = real
        cd.HTML = None


# ── X. 3.0.0-rc1.5 the Enhanced View engine (more than C, E and G) ──────────
async def test_focus_more():
    print("\n[X] 3.0.0-rc1.5 Enhanced View engine")
    started = []

    async def fake_snap_loop(camera_id, url, camera_, native_res=False):
        started.append((camera_id, native_res))
        await asyncio.sleep(1000)
    with _Swap(snap_loop=fake_snap_loop):
        cd.CAMERAS.clear(); cd.CAMERAS["cam1"] = camera()
        cd._SNAP.clear(); cd._MOTION.clear(); cd._FOCUS_ADAPTIVE.clear()
        cd._FOCUSED_CAMERA = None; cd._FOCUS_ENGINE = None
        r = await cd.handle_focus_set(make_mocked_request(
            "POST", "/snap/focus/nope", match_info={"camera_id": "nope"}))
        check("X1 unknown camera: 404 and no focus", r.status == 404 and cd._FOCUSED_CAMERA is None)
        st = cd._snap_state("cam1")
        st.update(focus_leave_kill=True, hw_session_fails={"hevc_drm": 2})
        await cd.handle_focus_set(make_mocked_request(
            "POST", "/snap/focus/cam1", match_info={"camera_id": "cam1"}))
        await asyncio.sleep(0)
        check("X2 classic entry clears a stale leave flag and the hardware failure count",
              "focus_leave_kill" not in st and "hw_session_fails" not in st)
        check("X2 ... and starts the full-resolution loop on the main stream",
              started == [("cam1", True)] and st["task"] is not None)

        # The motion file reads the focus state, which this engine owns.
        cd._MOTION["cam1"] = {"enabled": True}
        st["task"].cancel(); await asyncio.sleep(0)
        started.clear()
        cd._motion_ensure_loop("cam1")
        check("X3 classic view on: motion starts no second loop", started == [])
        await cd.handle_focus_clear(make_mocked_request("DELETE", "/snap/focus"))
        await cd.handle_focus_set(make_mocked_request(
            "POST", "/snap/focus/cam1?engine=go2rtc", match_info={"camera_id": "cam1"}))
        await asyncio.sleep(0)
        cd._SNAP["cam1"]["task"].cancel(); await asyncio.sleep(0)
        started.clear()
        cd._motion_ensure_loop("cam1")
        await asyncio.sleep(0)
        check("X3 live view on: motion restarts the armed camera's thumbnail loop",
              started == [("cam1", False)], str(started))
        for s in cd._SNAP.values():
            if s.get("task"):
                s["task"].cancel()
        await cd.handle_focus_clear(make_mocked_request("DELETE", "/snap/focus"))
        cd._MOTION.clear()

        # Classic leave: the ffmpeg is killed, the learned tier stays.
        # (3.0.1, C11: the hardware preheater is gone with Fast Stream Start.)
        cd._SNAP.clear(); started.clear()
        await cd.handle_focus_set(make_mocked_request(
            "POST", "/snap/focus/cam1", match_info={"camera_id": "cam1"}))
        await asyncio.sleep(0)
        st = cd._SNAP["cam1"]
        ff = FakeProc(); st["proc"] = ff
        cd._FOCUS_ADAPTIVE["cam1"] = {"tier_idx": 4, "locked": True, "run_start": 9.0}
        await cd.handle_focus_clear(make_mocked_request("DELETE", "/snap/focus"))
        await asyncio.sleep(0); await asyncio.sleep(0)
        check("X4 classic leave: the full-resolution ffmpeg is killed, the leave flag set",
              ff.killed and st.get("focus_leave_kill") is True)
        ada = cd._FOCUS_ADAPTIVE["cam1"]
        check("X4 ... the learned tier kept, its run timer reset",
              ada["tier_idx"] == 4 and ada["locked"] is True and ada["run_start"] is None)
        check("X4 ... focus cleared and the loop cancelled",
              cd._FOCUSED_CAMERA is None and cd._FOCUS_ENGINE is None and st["task"].cancelled())
        r = await cd.handle_focus_clear(make_mocked_request("DELETE", "/snap/focus"))
        check("X5 leaving with nothing in focus is harmless", r.status == 200)


# ── Y. 3.0.0-rc1.5 the snapshot loop ────────────────────────────────────────
# The real snap_loop, http_snap_loop and handle_snapshot, with stand-in ffmpeg
# processes and a local HTTP camera. Written before the move (build plan E1).
class FakeFfmpeg:
    """A stand-in ffmpeg: writes its chunks to stdout, then ends."""

    def __init__(self, chunks=(), stderr=b"", rc=1):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        for c in chunks:
            self.stdout.feed_data(c)
        self.stdout.feed_eof()
        if stderr:
            self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.returncode, self.pid, self.killed, self._rc = None, 4321, False, rc

    def kill(self):
        self.killed, self.returncode = True, -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = self._rc
        return self.returncode


def jpeg(tag: bytes) -> bytes:
    return b"\xff\xd8" + tag + b"\xff\xd9"


def opt(args, flag):
    """The value after a flag in an ffmpeg argument list, or None."""
    return args[args.index(flag) + 1] if flag in args else None


async def run_snap(cid, cam, makers, native_res=False, timeout=15):
    """Run the real snap_loop. makers[i]() gives the i-th ffmpeg (the last
    one repeats). Returns the argument list of every ffmpeg start."""
    import random as _rnd
    launches, seen = [], []

    async def fake_exec(*a, **k):
        launches.append(a)
        return makers[min(len(launches), len(makers)) - 1]()
    real = (cd.asyncio.create_subprocess_exec, _rnd.uniform, cd._motion_on_frame)
    cd.asyncio.create_subprocess_exec = fake_exec
    _rnd.uniform = lambda a, b: 0.0                     # no waiting between restarts
    cd._motion_on_frame = lambda c, f: seen.append(f)
    cd._HW_PROBED.set()
    try:
        state = cd._snap_state(cid)
        task = asyncio.create_task(cd.snap_loop(cid, cd.build_authenticated_url(cam), cam,
                                                native_res=native_res))
        state["task"] = task
        await asyncio.wait_for(task, timeout=timeout)
    finally:
        cd.asyncio.create_subprocess_exec, _rnd.uniform, cd._motion_on_frame = real
    return launches, seen


async def test_snapshot_loop():
    print("\n[Y] 3.0.0-rc1.5 the snapshot loop")
    cd._FOCUSED_CAMERA = None; cd._FOCUS_ENGINE = None
    cd._MOTION.clear(); cd._SNAP.clear(); cd._FOCUS_ADAPTIVE.clear()
    cid = "cam1"
    nobody = lambda: cd._snap_last_access.__setitem__(cid, time.monotonic() - 100)

    # Y1 card view: arguments, frames, the idle stop
    cam = camera(stream_codec="h264", stream_width=1920)
    cd.CAMERAS.clear(); cd.CAMERAS[cid] = cam
    nobody()
    CAP.lines.clear()
    launches, seen = await run_snap(cid, cam, [
        lambda: FakeFfmpeg([b"junk" + jpeg(b"one")[:3], jpeg(b"one")[3:] + jpeg(b"two")])])
    a, st = launches[0], cd._SNAP[cid]
    check("Y1 card view: one ffmpeg, RTSP over TCP, the camera URL with its password",
          len(launches) == 1 and a[0] == "ffmpeg" and opt(a, "-rtsp_transport") == "tcp"
          and opt(a, "-i") == cd.build_authenticated_url(cam), str(a))
    check("Y1 ... thumbnail filter for H.264, JPEG quality 5, to a pipe",
          opt(a, "-vf") == "fps=10,scale=640:-2,format=yuvj420p" and opt(a, "-q:v") == "5"
          and a[-1] == "pipe:1" and opt(a, "-fflags") == "+nobuffer" and opt(a, "-flags") == "low_delay"
          and "-threads" not in a and "-skip_frame" not in a and "-probesize" not in a)
    check("Y1 ... JPEGs split across reads are joined; every frame is stored and checked for motion",
          st["frame"] == jpeg(b"two") and st["frame_count"] == 2 and st["current_run_frames"] == 2
          and seen == [jpeg(b"one"), jpeg(b"two")])
    check("Y1 ... nobody watching: no restart, and the loop clears its state",
          st["task"] is None and st["proc"] is None
          and any("after exit — not restarting" in l for l in CAP.lines))

    # Y2 the settings that change the arguments
    real_cfg = cd.CFG_LOW_LATENCY
    try:
        cd.CFG_LOW_LATENCY = True
        cam = camera(stream_codec="hevc", stream_width=3840, preferred_transport="udp",
                     needs_fflags_discardcorrupt=True)
        cd.CAMERAS[cid] = cam; cd._SNAP.clear(); nobody()
        launches, _ = await run_snap(cid, cam, [lambda: FakeFfmpeg([jpeg(b"a")])])
        a = launches[0]
        check("Y2 3.0.1 (B20): 4K H.265 card: keyframes only, 480 wide",
              opt(a, "-vf") == "scale=480:-2,format=yuvj420p" and opt(a, "-skip_frame") == "nokey"
              and a.index("-skip_frame") < a.index("-i"), str(a))
        check("Y2 low latency, UDP, discard corrupt",
              opt(a, "-probesize") == "32" and opt(a, "-rtsp_transport") == "udp"
              and opt(a, "-fflags") == "+nobuffer+discardcorrupt", str(a))
        check("Y2 3.0.1 (C11): no thread limit, and never the broken non-reference skip",
              "-threads" not in a and "nonref" not in a, str(a))
        cd.CFG_LOW_LATENCY = False
        cam = camera(stream_codec="hevc", stream_width=1920)
        cd.CAMERAS[cid] = cam; cd._SNAP.clear(); nobody()
        launches, _ = await run_snap(cid, cam, [lambda: FakeFfmpeg([jpeg(b"a")])])
        check("Y2 3.0.1 (C11): H.265 card at its own cap, 8 fps (Low FPS Mode is gone)",
              opt(launches[0], "-vf") == "fps=8,scale=640:-2,format=yuvj420p")
    finally:
        cd.CFG_LOW_LATENCY = real_cfg

    # Y3 Enhanced View (classic): the ladder's first rung, full quality
    try:
        cam = camera(stream_width=2560, stream_height=1440)
        cam["stream_profiles"][0].update(stream_width=2560, stream_height=1440)
        cd.CAMERAS[cid] = cam; cd._SNAP.clear(); cd._FOCUS_ADAPTIVE.clear(); nobody()
        cd._FOCUSED_CAMERA, cd._FOCUS_ENGINE = cid, "legacy"
        launches, _ = await run_snap(cid, cam, [lambda: FakeFfmpeg([jpeg(b"f")])], native_res=True)
        a = launches[0]
        check("Y3 classic view: profile 0 uncapped, no scaling, JPEG quality 2, no thread limit",
              opt(a, "-i") == cd.build_authenticated_url(cam, url=cam["stream_profiles"][0]["url"])
              and opt(a, "-vf") == "format=yuvj420p" and opt(a, "-q:v") == "2" and "-threads" not in a, str(a))
        ada = cd._FOCUS_ADAPTIVE[cid]
        check("Y3 ... the adaptive state is created with its ladder",
              ada["tier_idx"] == 0 and len(ada["ladder"]) == 62 and ada["run_start"] is not None)
    finally:
        cd._FOCUSED_CAMERA = cd._FOCUS_ENGINE = None

    # Y4 a camera that sends nothing: transport flip at 3, codec cleared at 5
    cam = camera(stream_codec="h264")
    cd.CAMERAS[cid] = cam; cd._SNAP.clear(); nobody()
    CAP.lines.clear()
    launches, _ = await run_snap(cid, cam, [FakeFfmpeg] * 8 + [lambda: FakeFfmpeg([jpeg(b"z")])])
    tr = [opt(a, "-rtsp_transport") for a in launches]
    check("Y4 three empty runs on TCP, then UDP, once per session",
          tr == ["tcp"] * 3 + ["udp"] * 6 and cam["preferred_transport"] == "udp", str(tr))
    check("Y4 five more empty runs clear the stored codec", cam["stream_codec"] == ""
          and cam["stream_profiles"][0]["stream_codec"] == "")
    check("Y4 restarts counted", cd._SNAP[cid]["restart_count"] == 8)

    # Y5 the focus-leave flag ends the loop without a restart
    cam = camera(stream_codec="h264")
    cd.CAMERAS[cid] = cam; cd._SNAP.clear()
    cd._snap_last_access[cid] = time.monotonic()
    cd._snap_state(cid)["focus_leave_kill"] = True
    launches, _ = await run_snap(cid, cam, [lambda: FakeFfmpeg([jpeg(b"k")])])
    check("Y5 focus-leave kill: no restart, flag consumed",
          len(launches) == 1 and "focus_leave_kill" not in cd._SNAP[cid])

    # Y6 which path: HTTP snapshots or ffmpeg
    http_calls = []

    async def fake_http(c, cam_):
        http_calls.append(c)
    with _Swap(http_snap_loop=fake_http):
        cam = camera(stream_codec="h264", http_snap_url="http://10.0.0.33/snap.jpg")
        cd.CAMERAS[cid] = cam; cd._SNAP.clear(); nobody()
        launches, _ = await run_snap(cid, cam, [lambda: FakeFfmpeg([jpeg(b"h")])])
        check("Y6 a snapshot URL and an unconfirmed RTSP stream: HTTP snapshots",
              http_calls == [cid] and launches == [])
        cam["rtsp_probe_ok"] = True; cd._SNAP.clear(); nobody()
        launches, _ = await run_snap(cid, cam, [lambda: FakeFfmpeg([jpeg(b"h")])])
        check("Y6 ... a confirmed RTSP stream: ffmpeg", http_calls == [cid] and len(launches) == 1)

    # Y7 hardware decode: tried first, software after a failed start
    real_hw = (cd.CFG_HW_DECODE, list(cd._HW_DECODER_CANDIDATES))
    cd.CFG_HW_DECODE = True
    try:
        cam = camera()
        cd.CAMERAS[cid] = cam; cd._SNAP.clear(); nobody()
        cd._HW_UNAVAILABLE.discard("hevc_drm")
        launches, _ = await run_snap(cid, cam, [FakeFfmpeg, lambda: FakeFfmpeg([jpeg(b"s")])])
        check("Y7 H.265 starts on the hardware decoder",
              opt(launches[0], "-hwaccel") == "drm"
              and launches[0].index("-hwaccel") < launches[0].index("-i"), str(launches[0]))
        check("Y7 ... no picture from it: software at once, failure counted",
              len(launches) == 2 and "-hwaccel" not in launches[1]
              and cd._SNAP[cid]["hw_session_fails"] == {"hevc_drm": 1})
    finally:
        cd.CFG_HW_DECODE = real_hw[0]

    # Y8 the quality ladder
    lad = cd._build_focus_ladder(camera())
    check("Y8 ladder: each profile uncapped, then 30 down to 1 fps",
          len(lad) == 62 and lad[0] == (0, None) and lad[1] == (0, 30) and lad[30] == (0, 1)
          and lad[31] == (1, None) and lad[-1] == (1, 1))
    old = {"stream_url": "rtsp://a/1", "sub_stream_url": "rtsp://a/2",
           "additional_streams": [{"url": "rtsp://a/3"}, {"url": "rtsp://a/1"}]}
    check("Y8 no profiles: built from the stream URLs, duplicates dropped",
          len(cd._build_focus_ladder(old)) == 93)

    # Y9 3.0.1 (C11): the removed options are gone from every file
    gone = ("low_fps_mode", "skip_nonref", "limit_threads", "stagger_polling", "fast_stream_start")
    texts = {f: (REPO / f).read_text(encoding="utf-8") for f in ("config.yaml", "run.sh", "translations/en.yaml")}
    check("Y9 3.0.1 (C11): the five removed options are in no manifest, start script or translation",
          not [(f, o) for f, t in texts.items() for o in gone if o in t])
    check("Y9 ... and their code is gone", not any(n in repo_source() for n in (
        "CFG_LOW_FPS", "CFG_SKIP_NONREF", "CFG_LIMIT_THREADS", "CFG_STAGGER_POLL",
        "CFG_FAST_STREAM_START", "_hw_preheater", "proc_hw")))

    # Y10 ffmpeg's error output
    cd.CAMERAS.clear(); cd.CAMERAS[cid] = camera()
    saves = []
    with _Swap(save_cameras=lambda: saves.append(1)):
        CAP.lines.clear()
        err = (b"deprecated pixel format used\n"
               b"[rtsp] rtsp://admin:hunter2@10.0.0.33/x: Multi-layer HEVC coding is not implemented\n")
        await cd._drain_stderr(FakeFfmpeg(stderr=err), f"SNAP:{cid}")
        line = next((l for l in CAP.lines if "ffmpeg stderr" in l), "")
        check("Y10 stderr logged without the password or the pixel-format noise",
              "Multi-layer HEVC" in line and "hunter2" not in line and "deprecated" not in line, line)
        c = cd.CAMERAS[cid]
        check("Y10 H.265+ seen: badge, discard-corrupt flag, saved",
              c.get("hevc_plus_warning") is True and c.get("needs_fflags_discardcorrupt") is True
              and c.get("clean_runs_since_fflags") == 0 and saves == [1])
        c.update(hevc_plus_warning=False, hevc_plus_noise_confirmed=True, clean_runs_since_fflags=4)
        await cd._drain_stderr(FakeFfmpeg(stderr=err), f"SNAP:{cid}")
        check("Y10 ... known noise: no badge, the clean-run count restarts",
              c["hevc_plus_warning"] is False and c["clean_runs_since_fflags"] == 0 and saves == [1])
        await cd._drain_stderr(FakeFfmpeg(stderr=b"hevc_drm: Could not find a valid device\n"), "SNAP:x")
        check("Y10 a missing hardware device is marked unavailable", "hevc_drm" in cd._HW_UNAVAILABLE)
        cd._HW_UNAVAILABLE.discard("hevc_drm")

    # Y11 the H.265+ fallback
    probed = []

    def fake_probe(url, u="", p="", timeout=6, label=""):
        probed.append((url, u, p))
        return url.endswith("/Streaming/Channels/102")
    with _Swap(probe_rtsp=fake_probe):
        cam = camera(sub_stream_url=None)
        got = await cd._try_hevc_plus_fallback(cid, cam, cam["stream_url"])
        check("Y11 H.265+ fallback: the Hikvision sub-stream path, with the password",
              got == cd.build_authenticated_url(cam).replace("/101", "/102")
              and probed[-1][1:] == ("admin", TRICKY_PASS), str(got))
        cam = camera(stream_url="rtsp://10.0.0.33:554/other")
        check("Y11 ... no known path: None",
              await cd._try_hevc_plus_fallback(cid, cam, cam["stream_url"]) is None)

    # Y12 handle_snapshot
    started = []

    async def frame_loop(camera_id, url, camera_, native_res=False):
        started.append(url)
        await asyncio.sleep(0.05)
        s = cd._snap_state(camera_id)
        s["frame"], s["frame_time"] = jpeg(b"card"), time.monotonic()
        await asyncio.sleep(1000)

    def snap_req(c):
        return make_mocked_request("GET", f"/snapshot/{c}", match_info={"camera_id": c})
    with _Swap(snap_loop=frame_loop):
        cd.CAMERAS.clear(); cd._SNAP.clear()
        cd.CAMERAS[cid] = camera()
        cd.CAMERAS["info"] = camera(id="info", display="info")
        cd.CAMERAS["nourl"] = camera(id="nourl", stream_url="")
        check("Y12 unknown camera 404, not streamable 400, no URL 503",
              (await cd.handle_snapshot(snap_req("nope"))).status == 404
              and (await cd.handle_snapshot(snap_req("info"))).status == 400
              and (await cd.handle_snapshot(snap_req("nourl"))).status == 503)
        r = await cd.handle_snapshot(snap_req(cid))
        check("Y12 first poll starts the loop and waits for its first picture",
              r.status == 200 and r.body == jpeg(b"card") and r.content_type == "image/jpeg"
              and started == [cd.build_authenticated_url(cd.CAMERAS[cid])]
              and r.headers["Cache-Control"] == "no-cache, no-store, must-revalidate")
        await cd.handle_snapshot(snap_req(cid))
        check("Y12 a running loop is not started twice", len(started) == 1)
        cd._SNAP[cid]["task"].cancel()

        st = cd._snap_state(cid)
        cd._FOCUSED_CAMERA = cid
        try:
            st["frame"] = None
            check("Y12 in focus, no picture yet: 204", (await cd.handle_snapshot(snap_req(cid))).status == 204)
            st.update(frame=jpeg(b"f"), frame_count=7, focus_frame_base=5, zero_frame_streak=1,
                      current_run_frames=0)
            cd._FOCUS_ADAPTIVE[cid] = {"tier_idx": 0, "ladder": cd._build_focus_ladder(cd.CAMERAS[cid])}
            cd.CAMERAS[cid]["stream_profiles"][0].update(stream_width=2560, stream_height=1440)
            h = (await cd.handle_snapshot(snap_req(cid))).headers
            check("Y12 in focus: frame source, step and frame counts in the headers",
                  h["X-Frame-Source"] == "focus" and h["X-Step-Res"] == "2560x1440"
                  and h["X-Step-FPS"] == "uncapped" and h["X-Focus-Frames"] == "2"
                  and h["X-Stream-Status"] == "connecting" and h["X-Snap-Mode"] == "rtsp", str(dict(h)))
            st["transport_flip_fired"] = True
            h = (await cd.handle_snapshot(snap_req(cid))).headers
            check("Y12 ... after the transport flip: switching_transport",
                  h["X-Stream-Status"] == "switching_transport")
            st["http_snap_active"] = True
            h = (await cd.handle_snapshot(snap_req(cid))).headers
            check("Y12 ... on HTTP snapshots: http_fallback", h["X-Stream-Status"] == "http_fallback"
                  and h["X-Snap-Mode"] == "http")
        finally:
            cd._FOCUSED_CAMERA = None
            cd._FOCUS_ADAPTIVE.clear()

    # Y13 status and log endpoints
    cd._SNAP.clear()
    st = cd._snap_state(cid)
    st.update(frame=b"12345", frame_time=time.monotonic(), frame_count=9, restart_count=2,
              proc=FakeProc())
    st["proc"].pid = 99
    body = json.loads((await cd.handle_snap_status(make_mocked_request("GET", "/snap/status"))).body)
    check("Y13 /snap/status: one entry per camera",
          body[cid]["running"] is True and body[cid]["pid"] == 99 and body[cid]["frame_count"] == 9
          and body[cid]["frame_bytes"] == 5 and body[cid]["restarts"] == 2, str(body))
    saved_buf = list(cd._LOG_BUFFER)
    cd._LOG_BUFFER.clear()
    cd._LOG_BUFFER.extend([{"level": "warning", "msg": "w", "t": 10.0},
                           {"level": "error", "msg": "e", "t": 20.0}])
    try:
        body = json.loads((await cd.api_logs(make_mocked_request("GET", "/api/logs?since=15"))).body)
        check("Y13 /api/logs: entries after 'since', worst level of the last 50",
              body["status"] == "error" and [e["msg"] for e in body["entries"]] == ["e"])
        cd._LOG_BUFFER[:] = [{"level": "warning", "msg": "w", "t": 10.0}]
        body = json.loads((await cd.api_logs(make_mocked_request("GET", "/api/logs"))).body)
        check("Y13 ... warnings only: warning", body["status"] == "warning" and len(body["entries"]) == 1)
    finally:
        cd._LOG_BUFFER[:] = saved_buf

    # Y14 http_snap_loop: Digest login and Reolink query-string login
    big = jpeg(b"P" * 400)
    hits = []

    async def digest(request):
        auth = request.headers.get("Authorization", "")
        hits.append(auth.split(" ", 1)[0])
        if not auth.startswith("Digest "):
            return web.Response(status=401, headers={
                "WWW-Authenticate": 'Digest realm="cam", nonce="n1", qop="auth", opaque="o1"'})
        f = dict(x.split("=", 1) for x in auth[7:].replace('"', "").split(", "))
        ha1 = hashlib.md5(f"admin:cam:{TRICKY_PASS}".encode()).hexdigest()
        ha2 = hashlib.md5(f"GET:{f['uri']}".encode()).hexdigest()
        want = hashlib.md5(f"{ha1}:n1:{f['nc']}:{f['cnonce']}:auth:{ha2}".encode()).hexdigest()
        ok = f["response"] == want and f["uri"] == "/snap.jpg" and f["opaque"] == "o1"
        return web.Response(body=big if ok else b"", status=200 if ok else 403)

    async def query(request):
        hits.append(dict(request.query))
        ok = request.query.get("user") == "admin" and request.query.get("password") == "pw1"
        return web.Response(body=big if ok else b"", status=200 if ok else 401)
    app = web.Application()
    app.router.add_get("/snap.jpg", digest)
    app.router.add_get("/cgi-bin/api.cgi", query)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        cd._SNAP.clear(); cd._MOTION.clear()
        cam = camera(http_snap_url=f"http://127.0.0.1:{port}/snap.jpg")
        cd.CAMERAS[cid] = cam; nobody()
        await asyncio.wait_for(cd.http_snap_loop(cid, cam), 10)
        check("Y14 Digest camera: Basic first, then a correct Digest answer; the picture stored",
              hits[-2:] == ["Basic", "Digest"] and cd._SNAP[cid]["frame"] == big, str(hits))
        hits.clear(); cd._SNAP.clear()
        cam = camera(http_snap_url=f"http://127.0.0.1:{port}/cgi-bin/api.cgi?cmd=Snap&channel=0",
                     http_snap_auth_mode="query_params", credentials=cd.encrypt_creds("admin", "pw1"))
        cd.CAMERAS[cid] = cam; nobody()
        await asyncio.wait_for(cd.http_snap_loop(cid, cam), 10)
        check("Y14 query-string camera: user and password in the address",
              hits and hits[0].get("cmd") == "Snap" and cd._SNAP[cid]["frame"] == big, str(hits))
    finally:
        await runner.cleanup()
        cd.CAMERAS.clear(); cd._SNAP.clear()


# ── Z. 3.0.0-rc1.5 password entry ───────────────────────────────────────────
# The real api_set_credentials and its helpers. Stand-ins replace only the
# functions that touch the network: ONVIF calls, the RTSP walkers, ffprobe.
class JsonReq:
    """A request whose body is the given value (an exception: unreadable)."""

    def __init__(self, data=None, match=None):
        self.data, self.match_info = data, match or {}

    async def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


async def test_credentials():
    print("\n[Z] 3.0.0-rc1.5 password entry")
    import re as _re
    calls = {"validate": [], "find": [], "probe": [], "waits": [], "onvif_snap": []}

    def validate(ip, port, urls, user, pw, timeout, meta=None, label=""):
        calls["validate"].append((port, list(urls), label))
        return {u: (bool(_re.search(r"channel=[23]&subtype=0", u)) if "realmonitor" in u
                    else not u.endswith(("/13", "/h264major", "/h264minor", "/11")))
                for u in urls}

    details = {}

    async def ffprobe(url, proto):
        calls["probe"].append(url)
        for key, d in details.items():
            if key in url:
                return dict(d)
        return {}

    async def wait(ip, secs, label=""):
        calls["waits"].append((secs, label))

    def find(ip, port, user, pw, cam=None):
        calls["find"].append((ip, port))
        return find.result.get(port)
    find.result = {}

    profiles = [{"token": "P1", "name": "main", "onvif_width": 2560, "onvif_height": 1440,
                 "onvif_encoding": "H264"},
                {"token": "P2", "name": "sub", "onvif_width": 640, "onvif_height": 360,
                 "onvif_encoding": "H264"},
                {"token": "P3", "name": "dup", "onvif_width": 640, "onvif_height": 360,
                 "onvif_encoding": "H264"}]
    stubs = dict(
        onvif_get_profiles=lambda media, u, p: [dict(x) for x in onvif_get_profiles.ret],
        onvif_get_stream_uri=lambda media, tok, u, p: f"rtsp://10.0.0.40:554/{tok}",
        onvif_get_snapshot_uri=lambda *a: calls["onvif_snap"].append(a) or "",
        _validate_rtsp_urls_single_socket=validate, probe_stream_details=ffprobe,
        _throttle_wait_if_needed=wait, find_rtsp_path=find, save_cameras=lambda: None,
        probe_mjpeg_http=lambda ip, port, u, p: f"http://{ip}:{port}/video",
        probe_hls=lambda ip, port, u, p: None,
        probe_rtsp=lambda url, u="", p="", timeout=6.0, label="": url.endswith("/13"),
        probe_rtmp=lambda ip, port: True)

    class onvif_get_profiles:
        ret = profiles

    async def set_creds(data):
        r = await cd.api_set_credentials(JsonReq(data))
        return r.status, json.loads(r.body)

    with _Swap(**stubs):
        cd.CAMERAS.clear()
        st, _ = await set_creds(ValueError("bad"))
        st2, _ = await set_creds({"camera_id": "nope", "username": "u", "password": "p"})
        check("Z1 unreadable body 400, unknown camera 404", st == 400 and st2 == 404)

        # Z2 ONVIF: profiles, one-socket check, ranking, the codec correction
        old_id = "10.0.0.40_onvif"
        cd.CAMERAS[old_id] = {"id": old_id, "ip": "10.0.0.40", "port": 80, "protocol": "ONVIF",
                              "onvif": True, "xaddrs": "http://10.0.0.40:80/onvif/device_service",
                              "name": "Front", "manufacturer": "Hikvision", "mac_addr": "02:00:00:00:00:40"}
        details.clear()
        details.update({"P2": {"stream_codec": "h264", "stream_width": 640, "stream_height": 360},
                        "P3": {"stream_codec": "h264", "stream_width": 640, "stream_height": 360},
                        "@10.0.0.40:554/P1": {"stream_codec": "hevc"}})
        st, body = await set_creds({"camera_id": old_id, "username": "admin", "password": TRICKY_PASS})
        new_id = "10.0.0.40_onvif_P1"
        c = cd.CAMERAS.get(new_id, {})
        check("Z2 ONVIF: accepted; the card moves to the main profile's id",
              st == 200 and body == {"status": "ok", "channels": 1} and old_id not in cd.CAMERAS and c, str(body))
        check("Z2 ... all profile addresses checked on one socket at the RTSP port, not port 80",
              calls["validate"][0][0] == 554 and len(calls["validate"][0][1]) == 3)
        check("Z2 ... largest first; the duplicate sub-stream dropped",
              c.get("stream_url") == "rtsp://10.0.0.40:554/P1" and c.get("sub_stream_url") == "rtsp://10.0.0.40:554/P2"
              and [p["_url_key"] for p in c.get("stream_profiles", [])] == ["stream_url", "sub_stream_url"])
        check("Z2 ... ONVIF size used where ffprobe had none; identity kept; password stored encrypted",
              c.get("stream_width") == 2560 and c.get("manufacturer") == "Hikvision"
              and c.get("mac_addr") == "02:00:00:00:00:40"
              and cd.decrypt_creds(c["credentials"]) == ("admin", TRICKY_PASS)
              and TRICKY_PASS not in json.dumps(c))
        check("Z2 ... snapshot address from the camera table, no ONVIF call for it",
              c.get("http_snap_url") == "http://10.0.0.40/ISAPI/Streaming/channels/101/picture"
              and calls["onvif_snap"] == [])
        check("Z2 ... before the correction: the codec ONVIF reported", c.get("stream_codec") == "H264")
        await asyncio.sleep(1.4)
        check("Z2 ... ffprobe with the password corrects it", c.get("stream_codec") == "hevc"
              and c["stream_profiles"][0]["stream_codec"] == "hevc", str(c.get("stream_codec")))

        # Z3 ONVIF with no profiles: direct RTSP, port 554 before the stored port
        onvif_get_profiles.ret = []
        cid = "10.0.0.41_onvif"
        cd.CAMERAS[cid] = {"id": cid, "ip": "10.0.0.41", "port": 80, "protocol": "ONVIF", "onvif": True,
                           "xaddrs": "http://10.0.0.41:80/onvif/device_service", "name": "Side"}
        calls["find"].clear()
        find.result = {554: "rtsp://10.0.0.41:554/Streaming/Channels/101"}
        details.clear(); details["10.0.0.41"] = {"stream_codec": "h264", "stream_width": 1920,
                                                 "stream_height": 1080}
        st, body = await set_creds({"camera_id": cid, "username": "admin", "password": "pw"})
        c = cd.CAMERAS[cid]
        check("Z3 no ONVIF profiles: direct RTSP on 554 first",
              st == 200 and calls["find"] == [("10.0.0.41", 554)], str(calls["find"]))
        check("Z3 ... card ready with one profile and the probed details",
              c["status"] == "ready" and c["stream_url"] == "rtsp://10.0.0.41:554/Streaming/Channels/101"
              and len(c["stream_profiles"]) == 1 and c["stream_width"] == 1920
              and body["stream_url"] == c["stream_url"] and body["dvr_enumeration_pending"] is False)
        check("Z3 ... ffprobe got the address with the password", "admin:pw@10.0.0.41" in calls["probe"][-1])

        # Z4 rate-limited camera: table probe, locked streams, pacing
        cid = "10.0.0.42_554"
        cd.CAMERAS[cid] = {"id": cid, "ip": "10.0.0.42", "port": 554, "protocol": "RTSP",
                           "name": "Garage", "mac_vendor": "Microseven Inc",
                           "locked_streams": [{"path": "/13", "realm": "r"}, {"path": "/11"}, {"path": "/14"}]}
        find.result = {554: "rtsp://10.0.0.42:554/11"}
        details.clear()
        details.update({"/12": {"stream_codec": "h264", "stream_width": 640, "stream_height": 352},
                        "/13": {"stream_codec": "h264", "stream_width": 1280, "stream_height": 720},
                        "/11": {"stream_codec": "hevc", "stream_width": 2560, "stream_height": 1440}})
        calls["waits"].clear(); calls["validate"].clear()
        st, body = await set_creds({"camera_id": cid, "username": "admin", "password": TRICKY_PASS})
        c = cd.CAMERAS[cid]
        check("Z4 rate-limited camera: the card says it is waiting, then ready",
              st == 200 and c["status"] == "ready" and "Camera rate-limited" in c.get("status_text", ""))
        labels = [l for _, l in calls["waits"]]
        check("Z4 ... the main ffprobe and each locked-stream check wait out the 5 s cooldown",
              all(s == 5.0 for s, _ in calls["waits"]) and labels[0] == "non-ONVIF ffprobe"
              and {"locked-stream-validate 1", "locked-stream-details 1",
                   "locked-stream-validate 3"} <= set(labels), str(calls["waits"]))
        check("Z4 3.0.1 (B24): the stream-table check and its ffprobe wait out the cooldown too",
              labels[1] == "db_probe validation"
              and any(l.startswith("db_probe ffprobe") and l.endswith("/12") for l in labels), str(labels))
        check("Z4 ... table paths checked with the password percent-encoded, the main path skipped",
              all("admin:p%40ss&w+rd%20%2350%25%20x@10.0.0.42:554/" in u for u in calls["validate"][0][1])
              and not any(u.endswith("/11") for u in calls["validate"][0][1]))
        check("Z4 ... main kept, the table's sub-stream added, one locked stream confirmed",
              c["stream_url"] == "rtsp://10.0.0.42:554/11" and c["sub_stream_url"].endswith("10.0.0.42:554/12")
              and [a["path"] for a in c["additional_streams"]] == ["/13"]
              and [p.get("stream_width") for p in c["stream_profiles"]] == [2560, 640, 1280])
        check("Z4 ... locked list kept when not entered from its badge",
              len(c["locked_streams"]) == 3)
        cd.CAMERAS[cid]["locked_streams"] = [{"path": "/14"}]
        st, _ = await set_creds({"camera_id": cid, "username": "admin", "password": TRICKY_PASS,
                                 "from_locked_streams_modal": True})
        check("Z4 ... entered from the badge: locked list cleared", cd.CAMERAS[cid]["locked_streams"] == [])

        # Z5 wrong password
        find.result = {}
        cd.CAMERAS[cid].update(status="needs_credentials", locked_streams=[])
        cd.CAMERAS[cid].pop("status_text", None)
        st, body = await set_creds({"camera_id": cid, "username": "admin", "password": "wrong"})
        check("Z5 wrong password: 401, and the card back to its login form",
              st == 401 and body["error"] == "Could not connect with those credentials."
              and cd.CAMERAS[cid]["status"] == "needs_credentials" and "status_text" not in cd.CAMERAS[cid])

        # Z6 MJPEG
        cd.CAMERAS["m"] = {"id": "m", "ip": "10.0.0.43", "port": 8080, "protocol": "MJPEG", "name": "m"}
        st, body = await set_creds({"camera_id": "m", "username": "u", "password": "p"})
        check("Z6 MJPEG camera: the HTTP stream found", st == 200
              and cd.CAMERAS["m"]["stream_url"] == "http://10.0.0.43:8080/video")

        # Z7 DVR: one card per populated channel, in the background
        started = []

        async def fake_snap_loop(camera_id, url, camera_, native_res=False):
            started.append(camera_id)
        cd.CAMERAS.clear(); cd._DVR_ENUM_DONE.clear()
        cid = "10.0.0.50_554"
        cd.CAMERAS[cid] = {"id": cid, "ip": "10.0.0.50", "port": 554, "protocol": "RTSP",
                           "name": "IP Camera", "rtsp_auth_realm": "Login to " + "ab" * 16}
        find.result = {554: "rtsp://10.0.0.50:554/cam/realmonitor?channel=1&subtype=0"}
        details.clear(); calls["validate"].clear()

        async def no_count(cam, u, p):     # 3.5.0 (D1): the DVR does not say
            return None
        with _Swap(snap_loop=fake_snap_loop, _dvr_channel_count=no_count):
            st, body = await set_creds({"camera_id": cid, "username": "admin", "password": "pw"})
            check("Z7 DVR: accepted, and the page told the channel list is coming",
                  st == 200 and body["dvr_enumeration_pending"] is True)
            req = JsonReq(match={"camera_id": cid})
            for _ in range(80):
                s = json.loads((await cd.api_dvr_enum_status(req)).body)
                if s["done"]:
                    break
                await asyncio.sleep(0.1)
            check("Z7 ... the status endpoint reports the populated channels",
                  s == {"done": True, "populated_channels": ["1", "2", "3"]}, str(s))
            enum_urls = [u for _, urls, label in calls["validate"] if label.startswith("channel-enum")
                         for u in urls]
            check("Z7 ... channels 2 to 16 walked, one address per socket",
                  len(enum_urls) == 15 and all(len(urls) == 1 for _, urls, l in calls["validate"]
                                               if l.startswith("channel-enum")))
            ch2 = cd.CAMERAS.get("10.0.0.50_554_ch2", {})
            check("Z7 ... a card per channel: its own snapshot, the parent's password, no password in the address",
                  ch2.get("http_snap_url") == "http://10.0.0.50/cgi-bin/snapshot.cgi?channel=2"
                  and ch2.get("credentials") == cd.CAMERAS[cid]["credentials"]
                  and ch2.get("stream_url") == "rtsp://10.0.0.50:554/cam/realmonitor?channel=2&subtype=0"
                  and ch2.get("name") == "Lorex / Dahua DVR-NVR Family ch2"
                  and "10.0.0.50_554_ch3" in cd.CAMERAS, str(ch2)[:200])
            check("Z7 ... each new card starts its thumbnail loop; the parent is renamed for its channel",
                  sorted(started) == ["10.0.0.50_554_ch2", "10.0.0.50_554_ch3"]
                  and cd.CAMERAS[cid]["name"] == "Lorex / Dahua DVR-NVR Family ch1", str(started))
            n = len(cd.CAMERAS)
            await cd._enumerate_dvr_channels_after_auth(cid)
            check("Z7 ... a second run does nothing", len(cd.CAMERAS) == n)
            await cd._enumerate_dvr_channels_after_auth("gone")
            check("Z7 ... a deleted camera is marked done", "gone" in cd._DVR_ENUM_DONE)

        # AF1 3.5.0 (D1): the channels the DVR reports, not 16
        async def four(cam, u, p):
            return 4
        for k in [k for k in cd.CAMERAS if "_ch" in k]:
            del cd.CAMERAS[k]
        cd._DVR_ENUM_DONE.clear(); calls["validate"].clear()
        with _Swap(snap_loop=fake_snap_loop, _dvr_channel_count=four):
            await cd._enumerate_dvr_channels_after_auth(cid)
        enum_urls = [u for _, urls, label in calls["validate"] if label.startswith("channel-enum")
                     for u in urls]
        check("AF1 D1: a DVR that reports 4 channels: channels 2 to 4 walked, not 2 to 16",
              len(enum_urls) == 3 and cd.CAMERAS[cid].get("dvr_channels") == 4, str(len(enum_urls)))

        # Z8 clear credentials
        r = await cd.api_clear_credentials(JsonReq(match={"camera_id": "nope"}))
        check("Z8 clear credentials: unknown camera 404", r.status == 404)
        cd.CAMERAS["x"] = {"id": "x", "credentials": "c", "stream_url": "rtsp://u:p@10.0.0.9/1"}
        await cd.api_clear_credentials(JsonReq(match={"camera_id": "x"}))
        check("Z8 ... password and address password gone, card asks again",
              cd.CAMERAS["x"] == {"id": "x", "credentials": None, "stream_url": "rtsp://10.0.0.9/1",
                                  "requires_credentials": True, "status": "needs_credentials"})

        # Z9 add a camera by hand
        add = lambda d: cd.api_add_camera(JsonReq(d))
        r1, r2 = await add(ValueError()), await add({"ip": " "})
        check("Z9 add camera: unreadable 400, no address 400", r1.status == 400 and r2.status == 400)
        r = await add({"ip": "10.0.0.60", "port": 554, "rtsp_path": "/13", "username": "u", "password": "p"})
        c = cd.CAMERAS.get("10.0.0.60_554_manual", {})
        check("Z9 ... a given RTSP path that answers: saved ready, password encrypted",
              json.loads(r.body)["camera_id"] == "10.0.0.60_554_manual" and c.get("status") == "ready"
              and c.get("stream_url") == "rtsp://10.0.0.60:554/13" and cd.decrypt_creds(c["credentials"]) == ("u", "p"))
        find.result = {}
        r = await add({"ip": "10.0.0.61", "port": 554})
        check("Z9 ... nothing answers: 400 with the reason", r.status == 400
              and "Could not connect to 10.0.0.61:554 via RTSP" in json.loads(r.body)["error"])
        await add({"ip": "10.0.0.62", "port": 1935, "protocol": "rtmp"})
        await add({"ip": "10.0.0.63", "port": 8443, "protocol": "ws-rtsp"})
        check("Z9 ... RTMP gets its default path; WS-RTSP is an information card",
              cd.CAMERAS["10.0.0.62_1935_manual"]["stream_url"] == "rtmp://10.0.0.62:1935/live/stream"
              and cd.CAMERAS["10.0.0.63_8443_manual"]["status"] == "info"
              and cd.CAMERAS["10.0.0.63_8443_manual"]["display"] == "wsrtsp")
        r = await add({"ip": "10.0.0.64", "port": 443, "protocol": "WebRTC"})
        c = cd.CAMERAS.get("10.0.0.64_443_manual", {})
        check("Z9 3.0.1 (B23): WebRTC added by hand is an information card, named as the scan names it",
              r.status == 200 and c.get("protocol") == "WebRTC" and c.get("display") == "webrtc"
              and c.get("status") == "info", f"{r.status} {c}")

        # Z10 Deep Re-Probe
        rp = lambda c: cd.api_deep_reprobe(JsonReq(match={"camera_id": c}))
        check("Z10 Deep Re-Probe: unknown camera 404", (await rp("nope")).status == 404)
        cd.CAMERAS["d"] = {"id": "d", "ip": "10.0.0.70", "port": 554, "deep_reprobe_in_progress": True}
        check("Z10 ... one at a time: 409", (await rp("d")).status == 409)

        def find_locked(ip, port, u, p, meta):
            meta["locked_streams"] = [{"path": "/a"}]
            return "rtsp://10.0.0.70:554/live"
        cd.CAMERAS["d"] = {"id": "d", "ip": "10.0.0.70", "port": 554, "early_bail_reason": "x"}
        with _Swap(find_rtsp_path=find_locked):
            body = json.loads((await rp("d")).body)
        c = cd.CAMERAS["d"]
        check("Z10 ... no saved walk: a fresh full probe; the card ready, the walk state cleared",
              body["found_stream"] and body["outcome"] == "ready" and c["status"] == "ready"
              and c["locked_streams"] == [{"path": "/a"}] and "early_bail_reason" not in c
              and c["deep_reprobe_in_progress"] is False and c["deep_reprobe_attempts"] == 1, str(body))
        resumed = []

        def walk(ip, port, paths, u, p, t, *rest):
            resumed.append(list(paths))
            rest[2]["locked_streams"] = [{"path": "/b"}]
            return None, True
        cd.CAMERAS["d"] = {"id": "d", "ip": "10.0.0.70", "port": 554, "locked_streams": [{"path": "/a"}],
                           "early_bail_reason": "layer1_consecutive_401s",
                           "early_bail_paths_remaining": ["/b", "/c"],
                           "early_bail_at": datetime_now_iso()}
        with _Swap(_probe_rtsp_paths_single_socket=walk):
            body = json.loads((await rp("d")).body)
        check("Z10 ... a recent early stop: the walk resumes on the paths left; locked streams merged",
              resumed == [["/b", "/c"]] and body["outcome"] == "locked_streams:2"
              and [l["path"] for l in cd.CAMERAS["d"]["locked_streams"]] == ["/a", "/b"], str(body))
        cd.CAMERAS["d"] = {"id": "d", "ip": "10.0.0.70", "port": 554,
                           "early_bail_reason": "layer1_then_layer2_skipped_401s",
                           "early_bail_paths_remaining": [], "early_bail_at": datetime_now_iso()}
        with _Swap(probe_rtsp=lambda url, u="", p="", timeout=6.0: True):
            body = json.loads((await rp("d")).body)
        check("Z10 ... the skipped second stage runs: first address that answers",
              body["found_stream"] and body["stream_url"] == "rtsp://10.0.0.70:554" + cd.RTSP_PATHS[0])
        cd.CAMERAS["d"] = {"id": "d", "ip": "10.0.0.70", "port": 554,
                           "early_bail_reason": "layer1_consecutive_401s",
                           "early_bail_paths_remaining": ["/b"], "early_bail_at": "2000-01-01T00:00:00"}
        calls["find"].clear()
        await rp("d")
        check("Z10 ... an old early stop: a fresh probe instead", calls["find"] == [("10.0.0.70", 554)])

    # Z11 the stream table
    check("Z11 stream table: the OUI vendor finds the recipe",
          cd._match_stream_db_slug({"mac_vendor": "Microseven Inc"}) == "microseven"
          and cd._match_stream_db({"mac_vendor": "Microseven Inc"})["snap"] == "/tmpfs/snap.jpg")
    check("Z11 ... the longest keyword wins; nothing: None",
          cd._match_stream_db_slug({"server_header": "Hipcam RealServer/V1.0"}) == "microseven"
          and cd._match_stream_db_slug({"name": "Hikvision DS-2DE"}) == "hikvision"
          and cd._match_stream_db_slug({}) is None and cd._match_stream_db({}) is None)
    cd.CAMERAS.clear(); cd._DVR_ENUM_DONE.clear()


def datetime_now_iso():
    import datetime as _dt
    return _dt.datetime.utcnow().isoformat()



# ── AA. 3.0.1 ────────────────────────────────────────────────────────────────
async def test_301():
    print("\n[AA] 3.0.1")
    # B2: appliance cameras by MAC prefix
    check("AA1 B2: the iENSO block (28 bits) and Dreame are appliance cameras",
          (cd.appliance_camera("04:a1:6f:1a:2b:3c") or ("",))[0] == "Appliance camera (iENSO module)"
          and (cd.appliance_camera("00-AE-F7-AA-BB-01") or ("",))[0] == "Dreame robot vacuum"
          and cd.appliance_camera("04:A1:6F:20:00:01") is None and cd.appliance_camera("") is None)

    # B2 in the scan: every dropped host logged; an information card, or a note on a login card
    def live(subnet):
        cd.LIVE_HOST_MACS.clear()
        cd.LIVE_HOST_MACS.update({"10.0.0.40": "04:A1:6F:1A:2B:3C", "10.0.0.41": "00:AE:F7:AA:BB:01",
                                  "10.0.0.42": "02:11:22:33:44:55", "10.0.0.44": "04:A1:6F:10:00:09"})
        return {"10.0.0.40", "10.0.0.41", "10.0.0.42", "10.0.0.43", "10.0.0.44"}

    def nmap(ips):
        return [{"ip": "10.0.0.43", "hostname": "cam", "mac_addr": "", "mac_vendor": "",
                 "open_ports": [{"port": 554, "service": "rtsp", "product": ""}]},
                {"ip": "10.0.0.44", "hostname": "appl", "mac_addr": "04:A1:6F:10:00:09", "mac_vendor": "",
                 "open_ports": [{"port": 80, "service": "http", "product": ""}]}]

    async def probe(ip, port, hostname, initial, prev, verdict, reason, loop, host_meta=None):
        status = "ready" if ip == "10.0.0.43" else "needs_credentials"
        return {"id": f"{ip}_{port}", "ip": ip, "hostname": hostname, "port": port, "protocol": "RTSP",
                "stream_url": f"rtsp://{ip}:{port}/x", "status": status, "display": "proxy",
                "name": hostname, "verdict": "camera", "verdict_reason": "test",
                "requires_credentials": status != "ready", "user_saved": False}
    cd.CAMERAS.clear()
    CAP.lines.clear()
    with _Swap(get_local_subnet=lambda: "10.0.0.0/26", get_default_gateway=lambda: "10.0.0.1",
               discover_live_hosts=live, onvif_discover=lambda t: [], ssdp_discover=lambda t: [],
               mdns_discover=lambda t: [], broad_nmap_scan=lambda i: [], focused_nmap_scan=nmap,
               _probe_host_port=probe, save_cameras=lambda: None):
        await cd.run_scan()
    dropped = [l for l in CAP.lines if "No camera port open on" in l]
    check("AA2 B2: each live host without a camera port is named in the log, with its MAC",
          len(dropped) == 3 and any("10.0.0.42 (MAC 02:11:22:33:44:55" in l for l in dropped), str(dropped))
    a40, a41 = cd.CAMERAS.get("10.0.0.40_appliance", {}), cd.CAMERAS.get("10.0.0.41_appliance", {})
    check("AA2 ... an appliance camera with no open port gets an information card",
          a40.get("display") == "appliance" and a40.get("status") == "info"
          and a41.get("name") == "Dreame robot vacuum" and "Dreamehome" in a41.get("info", "")
          and "10.0.0.42_appliance" not in cd.CAMERAS, str(a40))
    c44 = cd.CAMERAS.get("10.0.0.44_80", {})
    check("AA2 ... an appliance camera that asks for a login keeps its login card, with a note",
          c44.get("status") == "needs_credentials" and "appliance" in (c44.get("device_notes") or "")
          and "10.0.0.44_appliance" not in cd.CAMERAS, str(c44))
    st = dict(cd.SCAN_STATE)
    check("AA3 B8: the scan reports its elapsed time and no time left at the end",
          st["progress"] == 100 and st["eta"] == 0 and st["elapsed"] > 0 and st["started_at"] > 0, str(st))
    check("AA3 ... and keeps each stage's time for the next estimate",
          len(cd.load_runtime().get("scan_stage_s", [])) == 4)

    # B8: progress from work done
    saved_rt = []
    with _Swap(load_runtime=lambda: {"scan_stage_s": [10, 10, 10, 10]},
               save_runtime=lambda d: saved_rt.append(d)):
        pr = cd._ScanProgress(broad=False)
        pr.start(1); pr.update(0.5)
        p1 = cd.SCAN_STATE["progress"]
        pr.start(3); pr.update(0.5)
        p3, eta3 = cd.SCAN_STATE["progress"], cd.SCAN_STATE["eta"]
        pr.finish(save=True)
    check("AA3 B8: progress follows the work done, from the last scan's stage times",
          p1 == 16 and p3 == 83 and eta3 == 5, f"{p1} {p3} {eta3}")
    check("AA3 ... the deeper scan counts only when it runs; the times are saved",
          pr.expect[3] == 0.0 and saved_rt and len(saved_rt[0]["scan_stage_s"]) == 4)
    cd.SCAN_STATE.update(running=False, progress=0, message="Idle. Click Scan to begin.")

    # B12: VAAPI only with a device a VAAPI driver opens
    real = (cd.VAAPI_DEVICE, cd.asyncio.create_subprocess_exec)
    try:
        cd.VAAPI_DEVICE = str(SCRATCH / "no-such-render-node")
        ok, why = await cd._vaapi_usable()
        check("AA4 B12: no render device: VAAPI not available", ok is False and "no " in why, why)
        dev = SCRATCH / "renderD128"; dev.write_bytes(b"")
        cd.VAAPI_DEVICE = str(dev)

        class P:
            def __init__(self, rc, err):
                self.returncode, self._err = rc, err

            async def communicate(self):
                return b"", self._err

        async def no_driver(*a, **k):
            return P(1, b"[AVHWDeviceContext] Failed to initialise VAAPI connection: -1 (unknown libva error).")

        async def driver(*a, **k):
            return P(0, b"")
        cd.asyncio.create_subprocess_exec = no_driver
        ok, why = await cd._vaapi_usable()
        check("AA4 ... a device but no VAAPI driver (a Pi 4): not available, with ffmpeg's reason",
              ok is False and "Failed to initialise VAAPI" in why, why)
        cd.asyncio.create_subprocess_exec = driver
        ok, why = await cd._vaapi_usable()
        check("AA4 ... a working driver: available", ok is True)
    finally:
        cd.VAAPI_DEVICE, cd.asyncio.create_subprocess_exec = real

    # B20 (3): a card is never shown a picture older than 10 s
    async def idle_loop(camera_id, url, camera_, native_res=False):
        await asyncio.sleep(1000)
    cid = "camb20"
    with _Swap(snap_loop=idle_loop):
        cd.CAMERAS.clear(); cd._SNAP.clear(); cd.CAMERAS[cid] = camera(id=cid)
        st = cd._snap_state(cid)
        st["frame"], st["frame_time"] = jpeg(b"old"), time.monotonic() - 20
        req = make_mocked_request("GET", f"/snapshot/{cid}", match_info={"camera_id": cid})
        r_old = await cd.handle_snapshot(req)
        st["frame_time"] = time.monotonic() - 2
        r_new = await cd.handle_snapshot(req)
        check("AA5 B20: a picture 20 s old is not shown (503); a 2 s old one is",
              r_old.status == 503 and r_new.status == 200, f"{r_old.status} {r_new.status}")
        if st.get("task"):
            st["task"].cancel()

    # B20 (4): on the snapshot path, the echo of an insect is not recorded
    starts = []

    async def fake_start(camera_id, camera_, url):
        starts.append(camera_id)
    w, h = cd.MOTION_GRID
    import random as _r
    rnd = _r.Random(7)
    calm = bytes(rnd.randrange(60, 200) for _ in range(w * h))
    bug = bytearray(calm)
    for y in range(4, 20):
        for x in range(4, 20):
            bug[y * w + x] = 255 - calm[y * w + x]
    A, B = cd._motion_thumb_gray(calm), cd._motion_thumb_gray(bytes(bug))

    def run(hold_until=None):
        cd._MOTION.clear()
        ms = cd._motion_state(cid); ms["enabled"] = True
        cd._motion_reset_prev(cid)
        CAP.lines.clear(); starts.clear()
        for t, pic in ((0.0, A), (1.0, A), (2.0, A), (3.0, B), (4.0, A)):
            if hold_until and t == 3.0:
                ms["light_until"] = 1000.0 + hold_until
            cd._motion_feed(cid, pic, 1000.0 + t)
    with _Swap(_start_recording=fake_start):
        cd.CAMERAS.clear(); cd.CAMERAS[cid] = camera(id=cid)
        run()
        await asyncio.sleep(0)
        rec1 = list(starts)
        # the insect picture itself is not judged (a light hold); the next
        # picture differs from it, but not from the picture before it
        run(hold_until=3.5)
        await asyncio.sleep(0)
        rec2 = list(starts)
    check("AA6 B20: a picture that differs from both pictures before it records",
          rec1 == [cid], str(rec1))
    check("AA6 ... a picture that differs only from an insect picture does not",
          rec2 == [] and any("changed against the last picture only" in l for l in CAP.lines),
          str(CAP.lines[-4:]))
    cd._MOTION.clear()

    # B20 (2): the detector's retry backs off from 10 s to 5 min
    delays = []
    real_sleep = asyncio.sleep

    async def fake_sleep(d, *a, **k):
        if d >= 1:
            delays.append(d)
            if len(delays) >= 6:
                cd._motion_state(cid)["enabled"] = False
        await real_sleep(0)

    async def no_ffmpeg(*a, **k):
        raise OSError("test: no ffmpeg")
    real_exec = cd.asyncio.create_subprocess_exec
    cd.asyncio.create_subprocess_exec = no_ffmpeg
    cd.asyncio.sleep = fake_sleep
    CAP.lines.clear()
    try:
        cd._MOTION.clear(); cd._motion_state(cid)["enabled"] = True
        await asyncio.wait_for(cd._motion_detector(cid, "rtsp://10.0.0.33/x"), 10)
    finally:
        cd.asyncio.sleep = real_sleep
        cd.asyncio.create_subprocess_exec = real_exec
    check("AA7 B20: detector retries at 10, 20, 40, 80, 160, then 300 s",
          delays == [10.0, 20.0, 40.0, 80.0, 160.0, 300.0], str(delays))
    check("AA7 ... with one warning, not one per retry",
          sum("live detection stopped" in l for l in CAP.lines) == 1)
    cd._MOTION.clear()

    # B26: the add-on's own ID for the log link
    asked = []

    async def sup(method, path, payload=None):
        asked.append((method, path))
        return {"slug": "5c53de3b_camera_discovery"}
    with _Swap(_supervisor_self=sup):
        cd._SELF_SLUG = None
        r1 = json.loads((await cd.api_self(make_mocked_request("GET", "/api/self"))).body)
        r2 = json.loads((await cd.api_self(make_mocked_request("GET", "/api/self"))).body)
    check("AA8 B26: the log link gets the add-on's own ID from the Supervisor, once",
          r1["slug"] == r2["slug"] == "5c53de3b_camera_discovery" and asked == [("GET", "info")])

    async def sup_none(method, path, payload=None):
        return None
    with _Swap(_supervisor_self=sup_none):
        cd._SELF_SLUG = None
        r3 = json.loads((await cd.api_self(make_mocked_request("GET", "/api/self"))).body)
    check("AA8 ... no Supervisor: the local ID", r3["slug"] == "local_camera_discovery")
    cd._SELF_SLUG = None
    check("AA8 ... the page asks for it", "/api/self" in repo_source() and "fetch(BASE + '/api/self')" in cd._JS)

    # F10: Show in Sidebar and Auto update, once
    posted = []

    async def sup_ok(method, path, payload=None):
        posted.append((method, path, payload))
        return {}
    rt = cd.load_runtime(); rt.pop("store_defaults_set", None); cd.save_runtime(rt)
    with _Swap(_supervisor_self=sup_ok):
        await cd._store_defaults_once()
        await cd._store_defaults_once()
    check("AA9 F10: on the first start, Show in Sidebar and Auto update are switched on, once",
          posted == [("POST", "options", {"ingress_panel": True, "auto_update": True})]
          and cd.load_runtime().get("store_defaults_set"), str(posted))
    rt = cd.load_runtime(); rt.pop("store_defaults_set", None); cd.save_runtime(rt)
    with _Swap(_supervisor_self=sup_none):
        await cd._store_defaults_once()
    check("AA9 ... a failed call sets no marker, so the next start tries again",
          not cd.load_runtime().get("store_defaults_set"))

    # C10: a browser without H.265 gets another stream for a card
    camx = {"id": "cx", "ip": "10.0.0.35", "credentials": None, "stream_url": "rtsp://10.0.0.35/main",
            "stream_profiles": [{"url": "rtsp://10.0.0.35/main", "stream_codec": "hevc", "stream_width": 1280},
                                {"url": "rtsp://10.0.0.35/sub", "stream_codec": "h264", "stream_width": 1920}]}
    u1, c1, _ = cd._go2rtc_card_source(camx)
    u2, c2, _ = cd._go2rtc_card_source(camx, h265=False)
    check("AA10 C10: a card plays its smallest stream; without H.265, the H.264 one",
          u1.endswith("/main") and c1 == "hevc" and u2.endswith("/sub") and c2 == "h264", f"{u1} {u2}")
    camx["stream_profiles"] = camx["stream_profiles"][:1]
    u3, c3, why = cd._go2rtc_card_source(camx, h265=False)
    check("AA10 ... only H.265: no live card, and the reason says why",
          u3 is None and "cannot play H.265" in why, why)
    cd.CAMERAS.clear()



# ── AB. 3.1.0 ────────────────────────────────────────────────────────────────
def mjpg(body: bytes) -> bytes:
    """A small, well-formed JPEG: start, one APP0 segment, picture data, end."""
    return b"\xff\xd8" + b"\xff\xe0\x00\x04ab" + b"\xff\xda\x00\x02" + body + b"\xff\xd9"


async def test_310():
    print("\n[AB] 3.1.0")
    from aiohttp.test_utils import TestServer, TestClient

    # C19: a computer plays a wide stream live; a phone does not
    wide = camera(id="w1", stream_profiles=[{"url": "rtsp://10.0.0.33:554/main",
                                             "stream_codec": "h264", "stream_width": 2560}])
    u_phone, _, why = cd._go2rtc_card_source(wide)
    u_pc, c_pc, _ = cd._go2rtc_card_source(wide, wide=True)
    check("AB1 C19: a 2560-wide stream: still pictures on a phone, live on a computer",
          u_phone is None and "small enough" in why and u_pc and u_pc.endswith("/main") and c_pc == "h264")

    # C19: which cameras have an MJPEG stream for a live card
    m1 = camera(id="m1", protocol="MJPEG", stream_url="http://10.0.0.22:81/videostream.cgi",
                stream_profiles=[])
    m2 = camera(id="m2", sub_stream_url="http://admin:pw@10.0.0.22/mjpeg", sub_stream_codec="mjpeg",
                sub_stream_width=640)
    check("AB2 C19: an MJPEG camera's HTTP stream is found; an RTSP MJPEG profile is not",
          cd._mjpeg_source(m1) == "http://10.0.0.22:81/videostream.cgi"
          and cd._mjpeg_source(camera()) is None)
    check("AB2 ... a sub-stream URL loses its password",
          cd._mjpeg_source(m2) == "http://10.0.0.22/mjpeg")
    check("AB2 ... an information card has none",
          cd._mjpeg_source(dict(m1, display="appliance")) is None)

    async def no_refresh(camera_id, why):
        refreshed.append((camera_id, why))
        return False
    refreshed = []
    real_ready = cd.anycam_go2rtc._GO2RTC_READY
    try:
        with _Swap(_streams_refresh=no_refresh):
            cd.CAMERAS.clear(); cd.CAMERAS["m1"] = m1; cd.CAMERAS["w1"] = wide
            cd.anycam_go2rtc._GO2RTC_READY = False
            req = make_mocked_request("GET", "/api/go2rtc/card/m1", match_info={"camera_id": "m1"})
            r1 = json.loads((await cd.api_go2rtc_card(req)).body)
            cd.anycam_go2rtc._GO2RTC_READY = True
            req = make_mocked_request("GET", "/api/go2rtc/card/w1", match_info={"camera_id": "w1"})
            r2 = json.loads((await cd.api_go2rtc_card(req)).body)
            await asyncio.sleep(0)
    finally:
        cd.anycam_go2rtc._GO2RTC_READY = real_ready
    check("AB3 C19: an MJPEG card plays live through the add-on, also while go2rtc starts",
          r1.get("ok") and r1.get("kind") == "mjpeg" and r1.get("url") == "/api/mjpeg/m1/ws", str(r1))
    check("AB3 B25: a card that cannot use the saved streams has them read again",
          not r2.get("ok") and refreshed and refreshed[0][0] == "w1" and "small enough" in refreshed[0][1],
          str(refreshed))

    # C19: pictures cut from the byte stream
    a, b = mjpg(b"one"), mjpg(b"two")
    thumb = b"\xff\xd8\xff\xd9"                              # an EXIF thumbnail's markers
    exif = (b"\xff\xd8" + b"\xff\xe1" + (2 + len(thumb)).to_bytes(2, "big") + thumb
            + b"\xff\xda\x00\x02pic\xff\xd9")
    stream = (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + a + b"\r\n--frame\r\n\r\n" + exif
              + b"\r\n--frame\r\n\r\n" + b)
    sp = cd._JpegSplitter()
    got = []
    for i in range(0, len(stream), 7):                     # pieces cut anywhere
        got += sp.feed(stream[i:i + 7])
    check("AB4 C19: JPEGs are cut out of the stream across reads",
          got[0] == a and got[2] == b and len(got) == 3, str([len(x) for x in got]))
    check("AB4 ... an end marker inside a segment does not cut a picture short", got[1] == exif)

    # C19: the WebSocket relay, against a camera that serves MJPEG
    opened, auth_seen = [], []

    async def camera_mjpeg(request):
        auth_seen.append(request.headers.get("Authorization", ""))
        if not request.headers.get("Authorization", "").startswith("Basic "):
            return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="cam"'})
        opened.append(1)
        resp = web.StreamResponse(headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame"})
        await resp.prepare(request)
        try:
            for n in range(200):
                await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                                 + mjpg(b"f%03d" % n) + b"\r\n")
                await asyncio.sleep(0.02)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def camera_still(request):
        return web.Response(body=mjpg(b"x"), content_type="image/jpeg")
    cam_app = web.Application()
    cam_app.router.add_get("/video", camera_mjpeg)
    cam_app.router.add_get("/still", camera_still)
    cam_srv = TestServer(cam_app); await cam_srv.start_server()
    app = web.Application(); app.router.add_get("/api/mjpeg/{camera_id}/ws", cd.handle_mjpeg_ws)
    client = TestClient(TestServer(app)); await client.start_server()
    real_idle = cd.MJPEG_IDLE_S
    cd.MJPEG_IDLE_S = 0.2
    try:
        cd.CAMERAS.clear(); cd._HUBS.clear()
        cd.CAMERAS["mj"] = camera(id="mj", ip="127.0.0.1", protocol="MJPEG", stream_profiles=[],
                                  stream_url=f"http://127.0.0.1:{cam_srv.port}/video")
        ws1 = await client.ws_connect("/api/mjpeg/mj/ws")
        ws2 = await client.ws_connect("/api/mjpeg/mj/ws")
        m1a = await asyncio.wait_for(ws1.receive(), 5)
        m1b = await asyncio.wait_for(ws1.receive(), 5)
        m2a = await asyncio.wait_for(ws2.receive(), 5)
        check("AB5 C19: each card gets the camera's JPEGs, unchanged, one per message",
              m1a.type == aiohttp.WSMsgType.BINARY and m1a.data.startswith(b"\xff\xd8")
              and m1a.data.endswith(b"\xff\xd9") and m1a.data != m1b.data
              and m2a.type == aiohttp.WSMsgType.BINARY)
        check("AB5 ... two cards share one camera connection, with the saved password",
              len(opened) == 1 and auth_seen[-1].startswith("Basic "), f"{opened} {auth_seen}")
        await ws1.close(); await ws2.close()
        for _ in range(100):
            if "mj" not in cd._HUBS:
                break
            await asyncio.sleep(0.05)
        check("AB5 ... the camera connection closes after the last card", "mj" not in cd._HUBS)

        cd.CAMERAS["st"] = camera(id="st", ip="127.0.0.1", protocol="MJPEG", stream_profiles=[],
                                  stream_url=f"http://127.0.0.1:{cam_srv.port}/still")
        ws3 = await client.ws_connect("/api/mjpeg/st/ws")
        m3 = await asyncio.wait_for(ws3.receive(), 5)
        check("AB6 C19: a URL that sends one picture, not a stream: the card is told, in words",
              m3.type == aiohttp.WSMsgType.TEXT and m3.data.startswith("error:")
              and "single pictures" in m3.data, str(m3.data))
        await ws3.close()
        r404 = await client.get("/api/mjpeg/cam1/ws")
        check("AB6 ... a camera with no MJPEG stream: 404", r404.status == 404)
    finally:
        cd.MJPEG_IDLE_S = real_idle
        await client.close(); await cam_srv.close()
        cd.CAMERAS.clear()

    # B25: the saved password, the same password step, at most every 6 hours
    calls = []

    async def fake_set(request):
        calls.append(await request.json())
        return web.json_response({"status": "ok"})
    with _Swap(api_set_credentials=fake_set):
        cd._STREAM_REFRESH_AT.clear(); cd.CAMERAS.clear()
        cd.CAMERAS["cam1"] = camera()
        cd.CAMERAS["ch3"] = camera(id="ch3", _dvr_parent_id="dvr")
        cd.CAMERAS["nopw"] = camera(id="nopw", credentials=None)
        ok1 = await cd._streams_refresh("cam1", "test")
        ok2 = await cd._streams_refresh("cam1", "test")
        ok3 = await cd._streams_refresh("ch3", "test")
        ok4 = await cd._streams_refresh("nopw", "test")
        cd._STREAM_REFRESH_AT["cam1"] -= cd.STREAM_REFRESH_MIN_S + 1
        ok5 = await cd._streams_refresh("cam1", "test")
    check("AB7 B25: the streams are read again with the saved password",
          ok1 and calls and calls[0] == {"camera_id": "cam1", "username": "admin",
                                         "password": TRICKY_PASS}, str(calls[:1]))
    check("AB7 ... not again within 6 hours; then again",
          ok2 is False and ok5 is True and len(calls) == 2)
    check("AB7 ... not for a DVR channel card, or a camera with no saved password",
          ok3 is False and ok4 is False)

    async def fail_set(request):
        return web.json_response({"error": "Could not connect"}, status=401)
    with _Swap(api_set_credentials=fail_set):
        cd._STREAM_REFRESH_AT.clear()
        saved = list(cd.CAMERAS["cam1"]["stream_profiles"])
        ok6 = await cd._streams_refresh("cam1", "test")
    check("AB7 ... a failed try keeps the saved streams",
          ok6 is False and cd.CAMERAS["cam1"]["stream_profiles"] == saved)
    cd._STREAM_REFRESH_AT.clear(); cd.CAMERAS.clear()

    # D3: one card order for every viewer
    order_app = web.Application()
    order_app.router.add_route("*", "/api/card_order", cd.api_card_order)
    order_app.router.add_get("/api/cameras", cd.api_cameras)
    oc = TestClient(TestServer(order_app)); await oc.start_server()
    real_rt = cd.RUNTIME_FILE
    try:
        cd.RUNTIME_FILE = SCRATCH / "runtime_order.json"
        cd.RUNTIME_FILE.unlink(missing_ok=True)
        cd._CARD_ORDER = None
        cd.CAMERAS.clear()
        cd.CAMERAS["a"] = camera(id="a", ip="10.0.0.31")
        cd.CAMERAS["b"] = camera(id="b", ip="10.0.0.32")
        cd.CAMERAS["c"] = camera(id="c", ip="10.0.0.60", port=80, channel=3)
        cd.CAMERAS["d"] = camera(id="d", ip="10.0.0.34")
        check("AB8 D3: the card key matches the page's: ip:port, #chN for a DVR channel",
              cd._card_key(cd.CAMERAS["c"]) == "10.0.0.60:80#ch3"
              and cd._card_key({"id": "x"}) == "id:x")
        before = [c["id"] for c in await (await oc.get("/api/cameras")).json()]
        r = await oc.post("/api/card_order",
                          json={"order": ["10.0.0.60:80#ch3", "10.0.0.32:554", "10.0.0.60:80#ch3"]})
        saved = (await r.json())["order"]
        after = [c["id"] for c in await (await oc.get("/api/cameras")).json()]
        check("AB8 D3: no saved order: the cameras in their own order", before == ["a", "b", "c", "d"])
        check("AB8 ... a saved order (duplicates dropped) comes first; other cards follow in their order",
              saved == ["10.0.0.60:80#ch3", "10.0.0.32:554"] and after == ["c", "b", "a", "d"], str(after))
        cd._CARD_ORDER = None      # a restart reads it from runtime.json
        again = [c["id"] for c in await (await oc.get("/api/cameras")).json()]
        check("AB8 ... the order survives a restart", again == after)
        bad = [await oc.post("/api/card_order", json={"order": "x"}),
               await oc.post("/api/card_order", json={"order": [1, 2]}),
               await oc.post("/api/card_order", json={"order": ["k"] * 501}),
               await oc.post("/api/card_order", data=b"{")]
        check("AB8 ... a bad order is refused (400) and the saved one stays",
              all(x.status == 400 for x in bad)
              and (await (await oc.get("/api/card_order")).json())["order"] == saved)
    finally:
        cd.RUNTIME_FILE = real_rt
        cd._CARD_ORDER = None
        await oc.close()
        cd.CAMERAS.clear()
    check("AB9 D3: the page draws a drag handle and sends the order",
          "cardDragStart(event,this)" in cd._JS and "'/api/card_order'" in cd._JS
          and "_cardOrderApply(grid)" in cd._JS)



# ── AC. 3.2.0 ────────────────────────────────────────────────────────────────
async def test_320():
    print("\n[AC] 3.2.0")
    from urllib.parse import unquote
    wrtc = {"id": "w", "ip": "10.0.0.50", "port": 8889, "display": "webrtc", "status": "info",
            "protocol": "WebRTC", "signaling_url": "http://10.0.0.50:8889/cam/whep",
            "credentials": cd.encrypt_creds("admin", "p#ss")}
    src, _, _ = cd._go2rtc_card_source(wrtc)
    check("AC1 C6: a WebRTC camera plays through go2rtc's WHEP source, with its password",
          src == "webrtc:http://admin:p%23ss@10.0.0.50:8889/cam/whep", str(src))
    fsrc, _, _ = cd._go2rtc_profile_source(wrtc, 0)
    none1, _, why1 = cd._go2rtc_profile_source(wrtc, 1)
    check("AC1 ... Enhanced View plays it too; it has one stream",
          fsrc == src and none1 is None and "does not exist" in why1)
    ws = {"id": "s", "ip": "10.0.0.51", "port": 80, "display": "wsrtsp", "status": "info",
          "protocol": "WS-RTSP", "ws_url": "ws://10.0.0.51:80/rtspoverwebsocket",
          "manufacturer": "Hikvision", "name": "Hikvision"}
    with _Swap(_match_stream_db=lambda c: {"rtsp": ["/Streaming/Channels/101"], "port": 554}):
        wsrc, _, _ = cd._go2rtc_card_source(ws)
    with _Swap(_match_stream_db=lambda c: None):
        wsrc2, _, _ = cd._go2rtc_card_source(ws)
    check("AC2 C6: an RTSP-over-WebSocket camera: the brand's RTSP path, carried by the WebSocket",
          wsrc == "rtsp://10.0.0.51:554/Streaming/Channels/101#transport=ws://10.0.0.51:80/rtspoverwebsocket",
          str(wsrc))
    check("AC2 ... an unknown brand asks for the root path",
          wsrc2 == "rtsp://10.0.0.51:554/#transport=ws://10.0.0.51:80/rtspoverwebsocket", str(wsrc2))
    bad, _, why = cd._go2rtc_card_source(dict(wrtc, signaling_url="ftp://x"))
    check("AC3 C6: a bad signalling address is refused, with the reason",
          bad is None and "signalling" in why)
    info, _, _ = cd._go2rtc_card_source({"display": "appliance"})
    check("AC3 ... an information card still has no live source", info is None)
    registered = []

    async def reg(name, src_, cid):
        registered.append(src_)
        return True
    real_ready = cd.anycam_go2rtc._GO2RTC_READY
    try:
        cd.anycam_go2rtc._GO2RTC_READY = True
        with _Swap(_go2rtc_register=reg):
            cd.CAMERAS.clear(); cd.CAMERAS["w"] = wrtc
            req = make_mocked_request("GET", "/api/go2rtc/card/w", match_info={"camera_id": "w"})
            r = json.loads((await cd.api_go2rtc_card(req)).body)
    finally:
        cd.anycam_go2rtc._GO2RTC_READY = real_ready
        cd.CAMERAS.clear()
    check("AC4 C6: the card endpoint registers the WHEP source with go2rtc",
          r.get("ok") and registered and registered[0].startswith("webrtc:http://"), str(r))
    check("AC5 C6: the card draws a live player and no still-picture fallback",
          "data-nosnap" in cd._JS and "img.dataset.nosnap" in cd._JS)
    check("AC6 C5: Enhanced View asks for sound; cards stay video only",
          "el.media = 'video,audio'" in cd._JS and cd._JS.count("el.media = 'video';") == 1)



# ── AD. 3.3.0 ────────────────────────────────────────────────────────────────
async def test_330():
    print("\n[AD] 3.3.0")
    fake = FakeGo2rtc()
    await fake.start()
    real_ready = cd.anycam_go2rtc._GO2RTC_READY
    cid = "cam1"
    cam = camera()
    main = cd.build_authenticated_url(cam)
    try:
        cd.anycam_go2rtc._GO2RTC_READY = True
        cd._GO2RTC_STREAMS.clear(); cd._GO2RTC_STREAM_CAM.clear(); cd._RELAY_FAILS.clear()
        r1 = await cd._go2rtc_relay(cid, main)
        r2 = await cd._go2rtc_relay(cid, main)
        name = cd._go2rtc_shared_name(cid, main)
        check("AD1 C4: ffmpeg reads the camera through go2rtc's RTSP server on 127.0.0.1",
              r1 == f"rtsp://anycam:{cd._GO2RTC_RTSP_PASS}@127.0.0.1:28554/{name}", r1)
        check("AD1 ... two users of one stream: one go2rtc stream, so one camera connection",
              r2 == r1 and len(fake.puts) == 1 and fake.registered.get(name) == main)
        check("AD2 C4: the stream name holds no password, and never changes its source",
              TRICKY_PASS not in name and "admin" not in name
              and cd._go2rtc_shared_name(cid, main) == name
              and cd._go2rtc_shared_name(cid, main.replace("101", "102")) != name)
        other = await cd._go2rtc_relay("cam2", main)
        check("AD2 ... the same address on another camera card is another stream",
              other != r1)
        http = await cd._go2rtc_relay(cid, "http://10.0.0.33/snap.jpg")
        cd.anycam_go2rtc._GO2RTC_READY = False
        off = await cd._go2rtc_relay(cid, main)
        cd.anycam_go2rtc._GO2RTC_READY = True
        check("AD3 C4: not RTSP, or go2rtc not running: the camera's own address",
              http == "http://10.0.0.33/snap.jpg" and off == main)
        for _ in range(cd.RELAY_FAIL_LIMIT):
            cd._go2rtc_relay_result(cid, r1, False)
        direct = await cd._go2rtc_relay(cid, main)
        check("AD4 C4: 3 failed runs through go2rtc: ffmpeg opens the camera directly again",
              direct == main and any("opens the camera directly" in l for l in CAP.lines))
        cd._RELAY_FAILS.clear()
        cd._go2rtc_relay_result(cid, r1, False); cd._go2rtc_relay_result(cid, r1, True)
        cd._go2rtc_relay_result(cid, main, False)
        check("AD4 ... a good run clears the count; a direct run is not counted",
              cid not in cd._RELAY_FAILS)

        # the live card and the snapshot loop share the stream
        cd.CAMERAS.clear()
        card_cam = camera(id=cid, stream_profiles=[{"url": "rtsp://10.0.0.33:554/Streaming/Channels/102",
                                                    "stream_codec": "h264", "stream_width": 640}])
        cd.CAMERAS[cid] = card_cam
        req = make_mocked_request("GET", f"/api/go2rtc/card/{cid}", match_info={"camera_id": cid})
        card = json.loads((await cd.api_go2rtc_card(req)).body)
        sub = cd.build_authenticated_url(card_cam, url="rtsp://10.0.0.33:554/Streaming/Channels/102")
        relay_sub = await cd._go2rtc_relay(cid, sub)
        check("AD5 C4/B15: the live card and AnyCam's ffmpeg use one go2rtc stream",
              card.get("ok") and relay_sub.endswith("/" + card["stream"]), f"{card} {relay_sub}")

        # the motion detector reads through go2rtc
        captured = []
        real_exec, real_sleep = cd.asyncio.create_subprocess_exec, asyncio.sleep

        async def cap_exec(*a, **k):
            captured.append(a)
            raise OSError("test: stop here")

        async def stop_sleep(d, *a, **k):
            cd._motion_state(cid)["enabled"] = False
            await real_sleep(0)
        cd._MOTION.clear(); cd._motion_state(cid)["enabled"] = True
        cd.asyncio.create_subprocess_exec = cap_exec
        cd.asyncio.sleep = stop_sleep
        try:
            await asyncio.wait_for(cd._motion_detector(cid, sub), 10)
        finally:
            cd.asyncio.create_subprocess_exec = real_exec
            cd.asyncio.sleep = real_sleep
        args = list(captured[0]) if captured else []
        check("AD6 C4: motion detection's ffmpeg reads go2rtc's copy, over TCP",
              opt(args, "-i") == relay_sub and opt(args, "-rtsp_transport") == "tcp", str(args[:8]))
        check("AD6 ... and its failed start is counted", cd._RELAY_FAILS.get(cid) == 1)
    finally:
        cd.anycam_go2rtc._GO2RTC_READY = real_ready
        cd._GO2RTC_STREAMS.clear(); cd._GO2RTC_STREAM_CAM.clear(); cd._RELAY_FAILS.clear()
        cd.CAMERAS.clear(); cd._MOTION.clear()
        await fake.stop()



# ── AE. 3.4.0 ────────────────────────────────────────────────────────────────
async def test_340():
    print("\n[AE] 3.4.0")
    import random as _r
    Z = cd.anycam_zones
    sq = [[0.75, 0.0], [1.0, 0.0], [1.0, 0.25], [0.75, 0.25]]
    good = {"zones": [{"name": "Garage", "points": sq, "level": 50, "closed": True},
                      {"name": "Path", "points": [[0.1, 0.5], [0.2, 0.5]], "level": 30, "closed": False}],
            "zones_only": False}
    clean, errs = Z.validate(good)
    check("AE1 C17: a closed zone and an open one are accepted", not errs and len(clean["zones"]) == 2, str(errs))
    bad = [
        {"zones": [dict(good["zones"][0], name=f"Z{i}") for i in range(7)]},
        {"zones": [good["zones"][0], dict(good["zones"][0], name="garage")]},
        {"zones": [dict(good["zones"][0], points=[[0, 0], [1.5, 0], [0, 1]])]},
        {"zones": [dict(good["zones"][0], points=[[0, 0], [1, 1], [1, 0], [0, 1]])]},
        {"zones": [dict(good["zones"][0], level=101)]},
        {"zones": [dict(good["zones"][0], name="x" * 41)]},
        {"zones": [dict(good["zones"][0], points=[[0, 0], [1, 0]])]},
        {"zones": "no"},
    ]
    reasons = [Z.validate(b)[1] for b in bad]
    check("AE1 ... refused: 7 zones, a repeated name, a point off the picture, crossing lines, "
          "level 101, a long name, 2 points closed, not a list",
          all(reasons), str([bool(r) for r in reasons]))
    check("AE1 ... the crossing-lines message says what to do", "move a point" in reasons[3][0])
    check("AE2 C17: a quarter square in the corner covers 32 x 24 cells of 128 x 96",
          len(Z.polygon_cells(sq, Z.ZONE_GRID)) == 32 * 24)

    # judging: a 0.2% door in a zone, which the whole picture would never see
    gw, gh = Z.ZONE_GRID
    rnd = _r.Random(5)
    calm = bytes(rnd.randrange(60, 200) for _ in range(gw * gh))

    def changed(x0, y0, w, h):
        b = bytearray(calm)
        for y in range(y0, y0 + h):
            for x in range(x0, x0 + w):
                b[y * gw + x] = 255 - calm[y * gw + x]
        return cd._motion_thumb_gray(bytes(b))
    A = cd._motion_thumb_gray(calm)
    door = changed(100, 4, 16, 10)          # inside the zone: 160 cells, 21% of it, 1.3% of the picture
    door2 = changed(100, 4, 16, 11)         # the next picture: a little more of the door
    yard = changed(10, 50, 40, 30)          # outside: 1,200 cells, 10% of the picture
    cid = "cz"
    cd.CAMERAS.clear(); cd.CAMERAS[cid] = camera(id=cid)
    cd._MOTION.clear(); cd._MOTION_CFG.clear(); cd._motion_state(cid)["enabled"] = True
    Z.set_zones(cid, None)
    v0, p0 = cd._motion_judge(cid, A, door)
    Z.set_zones(cid, clean)
    v1, p1 = cd._motion_judge(cid, A, door)
    check("AE3 C17: without the zone the door is too small; with it, it is motion in the zone",
          v0 == "" and v1 == "motion" and cd._MOTION[cid]["judge_zone"] == "Garage" and p1 > 10,
          f"{v0} {v1} {p1:.1f}")
    check("AE3 ... the log names the zone", any('zone "Garage" changed' in l for l in CAP.lines))
    v2, _ = cd._motion_judge(cid, A, yard)
    Z.set_zones(cid, dict(clean, zones_only=True))
    v3, _ = cd._motion_judge(cid, A, yard)
    check("AE4 C17: outside the zones the camera's own level applies; with zones only, nothing there",
          v2 == "motion" and v3 == "", f"{v2} {v3}")
    Z.set_zones(cid, {"zones": [dict(clean["zones"][0], level=0)], "zones_only": False})
    v4, _ = cd._motion_judge(cid, A, door)
    check("AE5 C17: an Off zone never records and masks its cells (answer 5)", v4 == "")
    check("AE5 ... its cells are not part of outside either",
          len(Z.outside_cells(cid, Z.ZONE_GRID)) == gw * gh - 32 * 24)

    # answer 9: the second picture counts in the same zone, and no one-picture rule in a zone
    Z.set_zones(cid, clean)
    ms = cd._motion_state(cid); cd._motion_reset_prev(cid)
    first = cd._motion_decide(cid, ms, A, door, 10.0, True)
    same = cd._motion_decide(cid, ms, A, A, 10.25, True)
    check("AE6 C17: one changed picture in a zone does not record at once (answer 9)",
          first is False and same is False)
    cd._motion_reset_prev(cid)
    cd._motion_decide(cid, ms, A, door, 20.0, True)
    A2 = cd._motion_thumb_gray(bytes([calm[0] ^ 1]) + calm[1:])   # the next reference: not a repeat
    second = cd._motion_decide(cid, ms, A2, door2, 20.25, True)
    check("AE6 ... a second changed picture in the same zone records", second is True)

    # answer 11: slow movement
    cd._motion_reset_prev(cid)
    looks = [cd._motion_slow_look(cid, ms, A, float(t)) for t in range(6)]
    l6 = cd._motion_slow_look(cid, ms, door, 6.0)
    l7 = cd._motion_slow_look(cid, ms, door, 7.0)
    check("AE7 C17: a zone also compares with the picture 5 s old, on two pictures in a row",
          not any(looks) and l6 is False and l7 is True and ms["judge_zone"] == "Garage",
          f"{looks} {l6} {l7}")

    # answer 8: the finer grid
    check("AE8 C17: a camera with a zone is judged on 128 x 96, without one on 64 x 48",
          cd._motion_grid(cid) == (128, 96) and cd._motion_grid("nozones") == cd.MOTION_GRID)
    from PIL import Image as _Img
    import io as _io
    buf = _io.BytesIO(); _Img.new("L", (640, 480), 90).save(buf, "JPEG")
    th = cd._motion_thumb(buf.getvalue(), cd._motion_grid(cid))
    check("AE8 ... the snapshot path decodes to that grid", th and len(th[0]) == 128 * 96)

    # answer 14: the tuning line
    ms["peak_since"] = 0.0; ms["peak_n"] = 5; ms["peak_pct"] = 0.3
    ms["zone_peaks"] = {"Garage": (3.1, 2.0), None: (0.4, 5.0)}
    CAP.lines.clear()
    cd._motion_report_peak(cid, ms, 1000.0)
    check("AE9 C17: the tuning line has one entry for each zone and one for outside",
          any("Garage: peak 3.1% (records at 2.0%)" in l and "outside: peak 0.4%" in l for l in CAP.lines),
          str(CAP.lines[-2:]))

    # the endpoint, motion.json, and the Storage tab (answers 15, 17)
    real_mf, real_media = cd.MOTION_FILE, cd.MEDIA_DIR
    try:
        cd.MOTION_FILE = SCRATCH / "motion_zones.json"
        Z.set_zones(cid, None)
        req = make_mocked_request("POST", f"/api/cameras/{cid}/motion/zones", match_info={"camera_id": cid})
        async def body(): return {"zones": [{"name": "A", "points": [[0, 0], [1, 1], [1, 0], [0, 1]],
                                             "level": 5, "closed": True}]}
        req.json = body
        r_bad = await cd.api_motion_zones(req)
        async def body2(): return good
        req.json = body2
        r_ok = await cd.api_motion_zones(req)
        saved = json.loads(cd.MOTION_FILE.read_text(encoding="utf-8"))
        payload = json.loads(r_ok.body)
        check("AE10 C17: the endpoint refuses crossing lines (400) and saves good zones in motion.json",
              r_bad.status == 400 and r_ok.status == 200 and saved["zones"][cid]["zones"][0]["name"] == "Garage")
        check("AE10 ... it answers each zone's cells, for the small-zone warning",
              payload["zones"][0]["cells"] == 32 * 24 and payload["zones"][1]["cells"] == 0)
        Z.set_zones(cid, None)
        cd._motion_load()
        check("AE10 ... the zones come back after a restart", Z.has_zones(cid))
        cd.MEDIA_DIR = SCRATCH / "media_zones"
        (cd.MEDIA_DIR / "LorexCH4").mkdir(parents=True, exist_ok=True)
        (cd.MEDIA_DIR / "LorexCH4" / "LorexCH4_20261004_101010_part02.mp4").write_bytes(b"x")
        (cd.MEDIA_DIR / "LorexCH4" / "LorexCH4_20261004_111111.mp4").write_bytes(b"x")
        Z.REC_ZONES["LorexCH4_20261004_101010"] = "Garage"
        st = json.loads((await cd.api_storage_list(make_mocked_request("GET", "/api/storage"))).body)
        zones = {f["name"]: f["zone"] for f in st["folders"][0]["files"]}
        check("AE11 C17: the Storage tab shows the zone that started a recording (answer 15)",
              zones == {"LorexCH4_20261004_101010_part02.mp4": "Garage", "LorexCH4_20261004_111111.mp4": None},
              str(zones))
    finally:
        cd.MOTION_FILE, cd.MEDIA_DIR = real_mf, real_media
        Z.set_zones(cid, None); Z.REC_ZONES.clear()
        cd.CAMERAS.clear(); cd._MOTION.clear()



# ── AF. 3.5.0 ────────────────────────────────────────────────────────────────
async def test_350():
    print("\n[AF] 3.5.0")
    from aiohttp.test_utils import TestServer
    # D1: what a Dahua or Lorex DVR answers
    check("AF2 D1: the channel count is read from either answer",
          cd._dvr_parse_count("result=8\r\n") == 8
          and cd._dvr_parse_count("table.MaxRemoteInputChannels=32\n") == 32
          and cd._dvr_parse_count("Error\r\nBad Request!") is None
          and cd._dvr_parse_count("result=0") is None and cd._dvr_parse_count("result=999") is None)
    seen = []

    async def collect(request):
        seen.append((request.path, request.headers.get("Authorization", "")[:6]))
        if not request.headers.get("Authorization", "").startswith("Digest "):
            return web.Response(status=401, headers={
                "WWW-Authenticate": 'Digest realm="Login to x", qop="auth", nonce="n1", opaque="o"'})
        return web.Response(text="result=8\r\n" if "devVideoInput" in request.path_qs
                            else "table.MaxRemoteInputChannels=0\r\n")
    app = web.Application()
    app.router.add_get("/cgi-bin/devVideoInput.cgi", collect)
    app.router.add_get("/cgi-bin/magicBox.cgi", collect)
    srv = TestServer(app); await srv.start_server()
    try:
        cam = {"ip": "127.0.0.1", "http_snap_url": f"http://127.0.0.1:{srv.port}/cgi-bin/snapshot.cgi"}
        n = await cd._dvr_channel_count(cam, "admin", "pw")
    finally:
        await srv.close()
    check("AF3 D1: the DVR is asked with Digest login, and its 8 inputs are the count",
          n == 8 and ("/cgi-bin/devVideoInput.cgi", "Digest") in seen, f"{n} {seen}")
    n2 = await cd._dvr_channel_count({"ip": "127.0.0.1", "http_snap_url": "http://127.0.0.1:9/x"}, "a", "b")
    check("AF3 ... no answer: None, so AnyCam walks 16 channels", n2 is None)

    # D2: one database, the same lookups
    import hashlib as _h
    check("AF4 D2: STREAM_DB, built from CAMERA_DB, is byte for byte the table of 3.4.0",
          _h.sha256(json.dumps(cd.STREAM_DB).encode()).hexdigest()
          == "e14a73dedbc300b8dcdc6dab9a5f290ab5ed4d2b590149455acd4f40c178a8dc")
    import camera_db as _db
    src = Path(_db.__file__).read_text(encoding="utf-8")
    check("AF4 ... the stream paths are written once, on the brands", "STREAM_DB: dict = {" not in src
          and sum(len(e.get("streams", [])) for e in _db.CAMERA_DB) == len(cd.STREAM_DB) == 40)
    ranks = [s["rank"] for e in _db.CAMERA_DB for s in e.get("streams", [])]
    check("AF4 ... every stream entry has its own rank", sorted(ranks) == list(range(40)))
    picks = {kw: cd._match_stream_db_slug({"name": kw}) for e in cd.STREAM_DB.values() for kw in e["match"]}
    check("AF5 D2: each keyword still finds its stream entry",
          picks["hikvision"] == "hikvision" and picks["lorex"] == "lorex" and picks["ezviz"] == "ezviz"
          and picks["imou"] == "imou" and picks.get("dh-ipc") == "dahua" and None not in picks.values())
    check("AF6 D2: the scan's ports come from the database too: the same 54 ports",
          len(cd.CAMERA_RELEVANT_PORTS) == 54 and cd.CAMERA_RELEVANT_PORTS[:3] == [22, 631, 9100]
          and cd.CAMERA_RELEVANT_PORTS[3:] == sorted(cd.CAMERA_RELEVANT_PORTS[3:])
          and {p for e in _db.CAMERA_DB for p in e.get("default_ports", [])} <= set(cd.CAMERA_RELEVANT_PORTS))
    real = list(_db.CAMERA_DB)
    try:
        _db.CAMERA_DB.append({"name": "Test brand", "default_ports": [12345]})
        import importlib, anycam_scan as _sc
        ports = list(_sc._PORTS_CLASSIFIER) + sorted(
            (set(_sc._PORTS_DOCUMENTED) | {p for e in _db.CAMERA_DB for p in e.get("default_ports", [])})
            - set(_sc._PORTS_CLASSIFIER))
        check("AF6 ... a brand added with a new port is scanned on it", 12345 in ports)
    finally:
        _db.CAMERA_DB[:] = real



# ── AG. 3.6.0 ────────────────────────────────────────────────────────────────
async def test_360():
    print("\n[AG] 3.6.0")
    import types
    U = cd.anycam_upload
    real_data = cd.DATA_DIR
    cd.DATA_DIR = SCRATCH / "upload_data"
    cd.DATA_DIR.mkdir(exist_ok=True)
    U._STATE.update(global_=None)
    U._STATE.pop("global_", None)
    U._STATE.update({"global": None, "cameras": {}, "host_keys": {}})
    U._QUEUE.clear()
    try:
        t, e = U.validate_target({"protocol": "SFTP", "host": "nas.example", "username": "rec",
                                  "password": "s3cret", "path": "/anycam/"}, None)
        check("AG1 C14: a destination is checked; the password is stored encrypted",
              not e and t["port"] == 22 and t["path"] == "/anycam" and "s3cret" not in json.dumps(t)
              and cd.decrypt_creds(t["credentials"]) == ("rec", "s3cret") and t["delete_local"] is True, str(e))
        bad = [U.validate_target(d, None)[1] for d in (
            {"protocol": "scp", "host": "h", "username": "u", "password": "p"},
            {"protocol": "ftp", "host": "a b", "username": "u", "password": "p"},
            {"protocol": "ftp", "host": "h", "port": 70000, "username": "u", "password": "p"},
            {"protocol": "ftp", "host": "h", "username": "u", "password": "p", "path": "/a/../b"},
            {"protocol": "ftp", "host": "h", "username": "u"})]
        check("AG1 ... refused: an unknown protocol, a bad server, port, folder, no password",
              all(bad), str([bool(b) for b in bad]))
        t2, e2 = U.validate_target({"protocol": "sftp", "host": "nas.example", "username": "rec",
                                    "password": "", "path": "/x"}, t)
        check("AG1 ... an empty password field keeps the saved password", not e2 and t2["credentials"] == t["credentials"])
        check("AG2 C14: the page never gets the password",
              "credentials" not in U.public_target(t) and U.public_target(t)["has_password"] is True)

        # where each camera's recordings go
        cd.CAMERAS.clear()
        for cid in ("a", "b", "c"):
            cd.CAMERAS[cid] = camera(id=cid)
        U._STATE["global"] = t
        own = dict(t, host="other.example")
        U._STATE["cameras"] = {"b": {"mode": "own", "target": own}, "c": {"mode": "off"}}
        check("AG3 C14: global for a camera with no entry, its own, or none",
              U.target_for("a") is t and U.target_for("b") is own and U.target_for("c") is None)

        # the queue
        rec_dir = SCRATCH / "upload_media" / "LorexCH4"
        rec_dir.mkdir(parents=True, exist_ok=True)
        f1, f2 = rec_dir / "LorexCH4_20261004_101010.mp4", rec_dir / "LorexCH4_20261004_111111.mp4"
        f1.write_bytes(b"video1"); f2.write_bytes(b"video2")
        U.enqueue("c", [f1])
        U.enqueue("a", [f1]); U.enqueue("a", [f1])
        saved_q = json.loads(U._queue_file().read_text(encoding="utf-8"))
        check("AG4 C14: a finished recording is queued once; a camera set to off is not",
              len(U._QUEUE) == 1 and U._QUEUE[0]["camera_id"] == "a" and len(saved_q) == 1)
        U._QUEUE.clear(); U.upload_load()
        check("AG4 ... the queue survives a restart", len(U._QUEUE) == 1)

        # FTP and FTPS
        calls = []

        class FakeFTP:
            def __init__(self, timeout=None):
                calls.append(("new", type(self).__name__))
            def connect(self, host, port): calls.append(("connect", host, port))
            def login(self, u, p): calls.append(("login", u, p))
            def prot_p(self): calls.append(("prot_p",))
            def mkd(self, d):
                calls.append(("mkd", d))
                if d == "/anycam":
                    raise U.ftplib.error_perm("550 exists")
            def storbinary(self, cmd, fh): calls.append(("stor", cmd, fh.read()))
            def delete(self, f): raise U.ftplib.error_perm("550 no file")
            def rename(self, a, b): calls.append(("rename", a, b))
            def quit(self): calls.append(("quit",))
            def close(self): pass

        class FakeTLS(FakeFTP):
            pass
        real_ftp, real_tls = U.ftplib.FTP, U.ftplib.FTP_TLS
        U.ftplib.FTP, U.ftplib.FTP_TLS = FakeFTP, FakeTLS
        try:
            await U.upload_one(dict(t, protocol="ftp", port=21), f1)
            ftp_calls = list(calls); calls.clear()
            await U.upload_one(dict(t, protocol="ftps", port=21), f1)
        finally:
            U.ftplib.FTP, U.ftplib.FTP_TLS = real_ftp, real_tls
        check("AG5 C14: FTP: folders made, written as .part, then renamed into <folder>/<camera>/",
              ("mkd", "/anycam/LorexCH4") in ftp_calls
              and ("stor", "STOR /anycam/LorexCH4/LorexCH4_20261004_101010.mp4.part", b"video1") in ftp_calls
              and ("rename", "/anycam/LorexCH4/LorexCH4_20261004_101010.mp4.part",
                   "/anycam/LorexCH4/LorexCH4_20261004_101010.mp4") in ftp_calls
              and ("prot_p",) not in ftp_calls, str(ftp_calls))
        check("AG5 ... FTPS encrypts the data connection too",
              ("new", "FakeTLS") in calls and ("prot_p",) in calls)

        # SFTP, with a stand-in for asyncssh
        sftp_log, key = [], {"fp": "SHA256:aaaa"}

        class Key:
            def get_fingerprint(self): return key["fp"]

        class Sftp:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def makedirs(self, d, exist_ok=False): sftp_log.append(("makedirs", d))
            async def put(self, a, b): sftp_log.append(("put", Path(a).name, b))
            async def exists(self, p): return False
            async def remove(self, p): sftp_log.append(("remove", p))
            async def rename(self, a, b): sftp_log.append(("rename", a, b))

        class Conn:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            def get_server_host_key(self): return Key()
            def start_sftp_client(self): return Sftp()
        fake_ssh = types.ModuleType("asyncssh")
        fake_ssh.connect = lambda host, **kw: (sftp_log.append(("connect", host, kw.get("port"),
                                                                kw.get("username"))), Conn())[1]
        real_mod = sys.modules.get("asyncssh")
        sys.modules["asyncssh"] = fake_ssh
        try:
            U._STATE["host_keys"].clear()
            await U.upload_one(t, f1)
            first = dict(U._STATE["host_keys"])
            key["fp"] = "SHA256:bbbb"
            try:
                await U.upload_one(t, f1)
                changed = None
            except RuntimeError as ex:
                changed = str(ex)
        finally:
            if real_mod is None:
                sys.modules.pop("asyncssh", None)
            else:
                sys.modules["asyncssh"] = real_mod
        check("AG6 C14: SFTP: login, folders, .part then rename",
              ("connect", "nas.example", 22, "rec") in sftp_log
              and ("put", "LorexCH4_20261004_101010.mp4", "/anycam/LorexCH4/LorexCH4_20261004_101010.mp4.part") in sftp_log
              and ("rename", "/anycam/LorexCH4/LorexCH4_20261004_101010.mp4.part",
                   "/anycam/LorexCH4/LorexCH4_20261004_101010.mp4") in sftp_log, str(sftp_log))
        check("AG6 ... the server's key is saved at the first upload; a changed key stops the upload",
              first == {"nas.example:22": "SHA256:aaaa"} and changed and "key changed" in changed)

        # the worker: success deletes the local copy; failure keeps it and waits
        U._QUEUE.clear()
        U._QUEUE.append({"file": str(f1), "camera_id": "a", "tries": 0, "next": 0.0})
        U._QUEUE.append({"file": str(f2), "camera_id": "a", "tries": 0, "next": 0.0})
        outcomes = {"LorexCH4_20261004_101010.mp4": None, "LorexCH4_20261004_111111.mp4": "refused"}

        async def fake_upload(target, local):
            if outcomes[local.name]:
                raise OSError(outcomes[local.name])
        CAP.lines.clear()
        with _Swap(upload_one=fake_upload):
            await U._upload_due(); await U._upload_due()
            again = await U._upload_due()
        check("AG7 C14: an uploaded file is deleted here and leaves the queue",
              not f1.exists() and all(q["file"] != str(f1) for q in U._QUEUE)
              and any("uploaded to SFTP nas.example; local copy deleted" in l for l in CAP.lines))
        q2 = next(q for q in U._QUEUE if q["file"] == str(f2))
        check("AG7 ... a failed upload keeps the file and waits 60 s, doubling to 30 min",
              f2.exists() and q2["tries"] == 1 and 55 < q2["next"] - time.time() < 65 and again is False
              and sum("failed (refused)" in l for l in CAP.lines) == 1)

        # the endpoints
        U._STATE.update({"global": None, "cameras": {}})
        mk = lambda method, query="", body=None: _req(method, "/api/upload/settings" + query, body)
        U._STATE["host_keys"]["nas.example:21"] = "SHA256:old"
        r = await U.api_upload_settings(mk("POST", "", {"protocol": "ftps", "host": "nas.example",
                                                        "username": "rec", "password": "pw", "path": "/r"}))
        d = json.loads(r.body)
        check("AG8 C14: the global destination is saved; the answer has no password",
              r.status == 200 and d["global"]["protocol"] == "ftps" and d["global"]["port"] == 21
              and "pw" not in r.body.decode() and "credentials" not in r.body.decode()
              and json.loads(U._settings_file().read_text(encoding="utf-8"))["global"]["host"] == "nas.example")
        check("AG8 ... saving a destination again accepts its server's key anew",
              "nas.example:21" not in U._STATE["host_keys"])
        r_own = await U.api_upload_settings(mk("POST", "?camera_id=b", {"mode": "own", "target": {
            "protocol": "sftp", "host": "cam-nas.example", "username": "u", "password": "p"}}))
        r_off = await U.api_upload_settings(mk("POST", "?camera_id=c", {"mode": "off"}))
        r_bad = await U.api_upload_settings(mk("POST", "?camera_id=c", {"mode": "sometimes"}))
        r_404 = await U.api_upload_settings(mk("GET", "?camera_id=nope"))
        check("AG8 ... a camera's own destination, off, a bad mode (400), an unknown camera (404)",
              r_own.status == 200 and U.target_for("b")["host"] == "cam-nas.example"
              and r_off.status == 200 and U.target_for("c") is None and r_bad.status == 400
              and r_404.status == 404)

        async def ok_upload(target, local):
            return None
        with _Swap(upload_one=ok_upload):
            tr = json.loads((await U.api_upload_test(mk("POST"))).body)
        check("AG9 C14: Test uploads a small file to the destination",
              tr["ok"] is True and tr["remote"].startswith("/r/anycam_upload_test/anycam-test-"), str(tr))

        # the recorder hands finished files over
        U._QUEUE.clear()
        f3 = rec_dir / "LorexCH4_20261004_121212_part01.mp4"
        f3.write_bytes(b"v3")
        ms = {"clip_base": "LorexCH4_20261004_121212", "clip_path": f3}
        cd._motion_finish_files("a", ms)
        check("AG10 C14: a finished recording goes to the upload queue under its final name",
              [q["file"] for q in U._QUEUE] == [str(rec_dir / "LorexCH4_20261004_121212.mp4")])
    finally:
        cd.DATA_DIR = real_data
        U._STATE.update({"global": None, "cameras": {}, "host_keys": {}})
        U._QUEUE.clear()
        cd.CAMERAS.clear()


def _req(method, path, body=None):
    req = make_mocked_request(method, path)
    async def _json():
        return body
    req.json = _json
    return req


# ── F. supervisor with a real subprocess ─────────────────────────────────────
FAKE_BIN =Path(__file__).resolve().parent / "fake_go2rtc.py"
FAKE_BIN.write_text(textwrap.dedent('''
    import json, os, sys, threading, time
    from http.server import BaseHTTPRequestHandler, HTTPServer
    assert sys.argv[1] == "-config", sys.argv
    conf = json.loads(sys.argv[2])
    with open(os.environ["FAKE_RECORD"], "a") as f:
        f.write(sys.argv[2] + "\\n")
    host, port = conf["api"]["listen"].rsplit(":", 1)
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
        def log_message(self, *a):
            pass
    srv = HTTPServer((host, int(port)), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("\\x1b[33mWRN\\x1b[0m [rtsp] dial rtsp://admin:hunter2@10.0.0.33:554/ch1 refused", flush=True)
    time.sleep(float(os.environ.get("FAKE_LIFETIME", "1.5")))
    srv.shutdown()
    sys.exit(3)
'''))


async def test_supervisor():
    print("\n[F] supervisor (real subprocess)")
    record = SCRATCH / "fake_go2rtc_record.txt"
    record.write_text("")
    os.environ["FAKE_RECORD"] = str(record)
    os.environ["FAKE_LIFETIME"] = "1.5"
    real_exec = asyncio.create_subprocess_exec
    launches = []

    async def fake_exec(program, *args, **kw):
        if program == str(cd.GO2RTC_BIN):
            launches.append(args)
            return await real_exec(sys.executable, str(FAKE_BIN), *args, **kw)
        return await real_exec(program, *args, **kw)

    cd.GO2RTC_BIN = FAKE_BIN
    cd._GO2RTC_READY = False   # test D left it True
    asyncio.create_subprocess_exec = fake_exec
    CAP.lines.clear()
    cd._GO2RTC_STREAMS["stale"] = "x"
    task = asyncio.create_task(cd._go2rtc_supervisor())
    try:
        t0 = time.monotonic()
        while not cd._GO2RTC_READY and time.monotonic() - t0 < 10:
            await asyncio.sleep(0.05)
        check("go2rtc reported ready", cd._GO2RTC_READY)
        check("stale streams cleared on (re)start", "stale" not in cd._GO2RTC_STREAMS)
        check("launched with inline config (-config {json})",
              launches and launches[0][0] == "-config" and launches[0][1].startswith("{"))
        while cd._GO2RTC_READY and time.monotonic() - t0 < 10:
            await asyncio.sleep(0.05)
        check("exit detected -> not ready", not cd._GO2RTC_READY)
        while len(launches) < 2 and time.monotonic() - t0 < 12:
            await asyncio.sleep(0.05)
        check("restarted after exit (backoff)", len(launches) >= 2, f"launches={len(launches)}")
        logged = "\n".join(CAP.lines)
        check("go2rtc output forwarded to the addon log", "[rtsp] dial rtsp://" in logged)
        check("camera password stripped from forwarded log", "hunter2" not in logged)
        check("ANSI colour codes stripped", "\x1b" not in logged)
        check("exit logged with return code", "rc=3" in logged)
        while not cd._GO2RTC_READY and time.monotonic() - t0 < 14:
            await asyncio.sleep(0.05)
        proc = cd._GO2RTC_PROC
    finally:
        os.environ["FAKE_LIFETIME"] = "1.5"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        asyncio.create_subprocess_exec = real_exec
    check("cancel stops the supervisor", task.done())
    check("cancel terminates the running go2rtc", proc is None or proc.returncode is not None,
          f"rc={getattr(proc, 'returncode', None)}")
    check("state cleared after stop", not cd._GO2RTC_READY and cd._GO2RTC_PROC is None)


async def main():
    test_pure()
    fake = FakeGo2rtc()
    await fake.start()
    try:
        await test_register(fake)
        await test_focus_api(fake)
        await test_proxy(fake)          # stops the fake at the end
    finally:
        try:
            await fake.stop()
        except Exception:
            pass
    # The go2rtc tests above leave it True; the tests below run without
    # go2rtc unless they say otherwise (3.3.0: ffmpeg jobs read through it).
    cd._GO2RTC_READY = False
    await test_focus_engine()
    await test_265()
    await test_motion()
    await test_cards()
    await test_b9_b10()
    await test_tuning_line()
    await test_cam_settings()
    await test_preroll()
    await test_night()
    await test_confirm()
    await test_acd_classic()
    await test_location_check()
    await test_redaction()
    await test_http_identity()
    await test_empty_text()
    await test_scan()
    await test_brand()
    await test_page_builder()
    await test_focus_more()
    await test_snapshot_loop()
    await test_credentials()
    await test_301()
    await test_310()
    await test_320()
    await test_330()
    await test_340()
    await test_350()
    await test_360()
    os.environ["FAKE_LIFETIME"] = "1.5"
    await test_supervisor()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    for name, _, detail in failed:
        print(f"  FAILED: {name}  {detail}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
