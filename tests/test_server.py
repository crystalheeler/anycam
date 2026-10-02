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
cd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cd)

import aiohttp
from aiohttp import web, WSMsgType
from aiohttp.test_utils import make_mocked_request
from cryptography.fernet import Fernet

cd._FERNET = Fernet(Fernet.generate_key())   # never touch /data/secret.key


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
    check("config: RTSP server disabled", conf["rtsp"]["listen"] == "")
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
    check("G4 Classic button kept", "focusUseClassic()" in html and 'id="focus-classic-grp"' in html)
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
    check("L13 test-stream endpoint removed", "handle_stream_test" not in src and "/test\"" not in src)

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
    check("Q4 cog help no longer promises SFTP for 2.6.8",
          "planned for a later version" in src and "planned for 2.6.8" not in src)


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


# ── F. supervisor with a real subprocess ─────────────────────────────────────
FAKE_BIN = Path(__file__).resolve().parent / "fake_go2rtc.py"
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
