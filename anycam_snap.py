"""The snapshot loop: the ffmpeg and HTTP picture loops, the hardware preheater, the snapshot endpoints.

Moved out of camera_discovery.py in 3.0.0-rc1.5 (build plan E1); the function
bodies are unchanged.

This file cannot import camera_discovery.py (see anycam_host.py). The names in
NEEDS are set on this module at start-up; H reads a camera_discovery.py
value at the moment of use.
"""
import aiohttp
import asyncio
import hashlib
import logging
import os
import random
import re
import time
from aiohttp import web

from anycam_host import H
from anycam_motion import (
    _motion_on_frame, _motion_reset_prev, _motion_uses_snapshots,
)
from anycam_probe import (
    probe_rtsp,
)
import anycam_focus
from anycam_go2rtc import _go2rtc_relay, _go2rtc_relay_result, _go2rtc_relayed

log = logging.getLogger("anycam")

# Taken from camera_discovery.py at start-up (anycam_host.bind).
NEEDS = (
    'ACD_ESCALATED_COOLDOWN', 'CAMERAS', 'CFG_ADAPTIVE_QUALITY', 'CFG_HW_DECODE',
    'CFG_LOW_LATENCY', '_FOCUS_ADAPTIVE', '_HW_DECODER_CANDIDATES',
    '_HW_PROBED', '_HW_UNAVAILABLE', '_LOG_BUFFER', '_SNAP',
    '_THREAD_POOL', '_acd_active', '_brand_throttle_seconds', '_snap_last_access',
    '_snap_state', '_strip_creds', '_throttle_wait_if_needed', 'build_authenticated_url',
    '_streams_refresh', 'decrypt_creds', 'save_cameras',
)


# ── 3.7.0 (B11): say when hardware decode did not happen ───────────────────
# 2026-09 field log (B11): ffmpeg could not open the Pi's decoder devices
# (/dev/media0 to 3, "Operation not permitted"), decoded in software, and
# AnyCam still logged "hw first frame (hevc_drm)". ffmpeg prints why on its
# error output while it goes on in software; those lines are caught here
# as they arrive, so the first-frame line can tell the truth.
_HW_FALLBACK: dict[str, str] = {}    # camera_id -> the ffmpeg line that showed it
HW_FALLBACK_WORDS = ("operation not permitted", "permission denied",
                     "hwaccel initialisation returned error", "failed setup for format",
                     "could not find a valid device", "failed to open", "no such file")
HW_FALLBACK_SUBJECTS = ("/dev/media", "/dev/video", "/dev/dri", "hwaccel", "drm", "v4l2",
                        "vaapi", "rpivid")


def _hw_fallback_line(line: str) -> bool:
    """True for an ffmpeg line that says a hardware decoder did not open."""
    low = line.lower()
    return any(w in low for w in HW_FALLBACK_WORDS) and any(s in low for s in HW_FALLBACK_SUBJECTS)


# ── 3.7.2 (B37): a hardware decoder that gives one unchanging picture ──────
# 2026-10-06 field log, Protection mode off: hevc_drm gave its first frame in
# 4.9 s, then 2,183 JPEGs of exactly 48,806 bytes each: a green screen. A
# camera's sensor noise changes the JPEG size of every real picture, so
# HW_FROZEN_FRAMES equal sizes in a row mean the hardware output is wrong.
# The camera then decodes in software until AnyCam restarts, and
# _hw_picture_test runs once to find which conversion gives a real picture.
HW_FROZEN_FRAMES = 50
_HW_FROZEN: dict[str, str] = {}              # camera_id -> hw label that froze
_HW_TEST_RESULTS: dict[str, dict] = {}       # camera_id -> last _hw_picture_test report
HW_TEST_PIXELS = 8 * 8 * 3                   # one 8x8 RGB picture
HW_TEST_WORDS = ("hwaccel", "v4l2", "drm", "sand", "error", "fail", "invalid",
                 "cannot", "not supported", "impossible")


def _hw_frozen_step(same: int, last_len: int, frame_len: int) -> tuple[int, int, bool]:
    """Count equal JPEG sizes in a row. Returns (count, size, frozen)."""
    same = same + 1 if frame_len == last_len else 1
    return same, frame_len, same >= HW_FROZEN_FRAMES


def _hw_test_verdict(raw: bytes) -> dict:
    """Judge the 8x8 RGB pictures of one _hw_picture_test run."""
    n = len(raw) // HW_TEST_PIXELS
    if n == 0:
        return {"frames": 0, "verdict": "no picture"}
    pics = [raw[i * HW_TEST_PIXELS:(i + 1) * HW_TEST_PIXELS] for i in range(n)]
    mean = [round(sum(p[c::3][k] for p in pics for k in range(64)) / (64 * n)) for c in range(3)]
    changes = any(max(abs(a - b) for a, b in zip(p, pics[0])) > 2 for p in pics[1:])
    r, g, b = mean
    if g > 90 and r < 40 and b < 40:
        verdict = "green (empty picture)"
    elif n > 1 and not changes:
        verdict = "frozen (the same picture every time)"
    else:
        verdict = "picture ok"
    return {"frames": n, "mean_rgb": mean, "changes": changes, "verdict": verdict}


def _cma_info() -> str:
    """The kernel's contiguous memory, which the Pi's HEVC decoder allocates from."""
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            vals = {k: v.strip() for k, _, v in (ln.partition(":") for ln in f)
                    if k in ("CmaTotal", "CmaFree")}
    except OSError as ex:
        return f"unknown ({ex})"
    return ", ".join(f"{k} {v}" for k, v in vals.items()) or "not reported"


async def _hw_picture_test(camera_id: str, url: str, hw_args: list[str]) -> dict:
    """3.7.2 (B37): decode a few pictures each way and say which ways give a real one.

    Software is the reference. The hardware ways: as AnyCam runs it; with
    the frames kept in the decoder's own memory and downloaded by the
    hwdownload filter; and with no pixel-format filter before the scale.
    Each run reduces six pictures, half a second apart, to 8x8 pixels.
    """
    variants = [("software", [], "format=yuvj420p,"),
                ("hardware, as AnyCam runs it", hw_args, "format=yuvj420p,"),
                ("hardware, no pixel format filter", hw_args, "")]
    if hw_args[:2] == ["-hwaccel", "drm"]:
        variants.append(("hardware, drm_prime frames and hwdownload",
                         hw_args[:2] + ["-hwaccel_output_format", "drm_prime"] + hw_args[2:],
                         "hwdownload,format=yuv420p,"))
    report = {"camera": camera_id, "cma": await asyncio.to_thread(_cma_info), "runs": []}
    log.info(f"HW TEST [{camera_id}]: kernel memory for the decoder: {report['cma']}")
    for name, args, pre in variants:
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "verbose",
               "-rtsp_transport", "tcp", "-timeout", "8000000", *args, "-i", url, "-an",
               "-vf", f"{pre}fps=2,scale=8:8:flags=area,format=rgb24",
               "-frames:v", "6", "-f", "rawvideo", "pipe:1"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as ex:
            report["runs"].append({"way": name, "verdict": f"ffmpeg did not start: {ex}"})
            continue
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=25)
        except asyncio.TimeoutError:
            await _stop_proc(proc)
            out, err = b"", b"(no answer in 25 s)"
        notes = [_strip_creds(ln.strip())[:160]
                 for ln in (err or b"").decode("utf-8", "replace").splitlines()
                 if any(w in ln.lower() for w in HW_TEST_WORDS)][:6]
        run = {"way": name, **_hw_test_verdict(out or b""), "ffmpeg": notes}
        report["runs"].append(run)
        log.info(f"HW TEST [{camera_id}]: {name}: {run['verdict']} ({run['frames']} pictures"
                 + (f", mean colour {run['mean_rgb']}" if run.get("mean_rgb") else "") + ")"
                 + (f" ffmpeg: {' | '.join(notes)}" if notes else ""))
    _HW_TEST_RESULTS[camera_id] = report
    return report


async def api_diagnostics_hwtest(request: web.Request) -> web.Response:
    """GET /api/diagnostics/hwtest/{camera_id} (3.7.2, B37) — run _hw_picture_test now."""
    camera_id = request.match_info["camera_id"]
    camera = CAMERAS.get(camera_id)
    if not camera or not camera.get("stream_url"):
        return web.json_response({"error": "no such camera with a stream"}, status=404)
    codec = (camera.get("stream_codec") or "").lower()
    target = "hevc" if codec in ("hevc", "h265") else "h264" if codec == "h264" else ""
    cand = next((a for _l, c, a in _HW_DECODER_CANDIDATES if c == target), None)
    if cand is None:
        return web.json_response({"error": f"no hardware decoder for codec {codec or '?'}"},
                                 status=400)
    url = await _go2rtc_relay(camera_id, build_authenticated_url(camera))
    return web.json_response(await _hw_picture_test(camera_id, url, list(cand)))


def _http_digest_header(www_auth: str, method: str, uri: str, u: str, p: str) -> str:
    """Build an HTTP Digest Authorization header value.

    Handles the qop=auth case (most cameras) and the simpler no-qop case.
    3.1.0: moved out of http_snap_loop, for the live MJPEG cards
    (anycam_mjpeg.py); unchanged.
    """
    # Parse WWW-Authenticate: Digest realm="...", nonce="...", ...
    def _unquote(s: str) -> str:
        return s.strip().strip('"')

    params: dict[str, str] = {}
    for part in re.split(r',\s*(?=[a-zA-Z])', www_auth.replace("Digest ", "", 1)):
        if "=" in part:
            k, v = part.split("=", 1)
            params[k.strip()] = _unquote(v)

    realm  = params.get("realm", "")
    nonce  = params.get("nonce", "")
    qop    = params.get("qop", "")
    opaque = params.get("opaque", "")
    nc_hex = "00000001"
    cnonce = hashlib.md5(os.urandom(8)).hexdigest()[:8]

    ha1 = hashlib.md5(f"{u}:{realm}:{p}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()

    if "auth" in qop:
        resp_str = f"{ha1}:{nonce}:{nc_hex}:{cnonce}:auth:{ha2}"
    else:
        resp_str = f"{ha1}:{nonce}:{ha2}"

    response = hashlib.md5(resp_str.encode()).hexdigest()

    header = (
        f'Digest username="{u}", realm="{realm}", '
        f'nonce="{nonce}", uri="{uri}", response="{response}"'
    )
    if "auth" in qop:
        header += f', qop=auth, nc={nc_hex}, cnonce="{cnonce}"'
    if opaque:
        header += f', opaque="{opaque}"'
    return header

# ── Adaptive fps state for focus/native_res mode ───────────────────────────────
# Persists across focus sessions so the camera remembers its best stable fps.
# Tiers ordered fastest→slowest. None = no fps limit (full native rate).
_ADAPTIVE_FPS_MAX:       int   = 30    # start fps cap (steps down by 1 each restart)
_ADAPTIVE_UNSTABLE_S:    float = 8.0   # run shorter than this with few frames = unstable
_ADAPTIVE_UNSTABLE_FR:   int   = 15    # fewer frames than this = unstable
_ADAPTIVE_RESTART_LIMIT: int   = 1     # restarts at locked tier before stepping down


def _build_focus_ladder(camera: dict) -> list:
    """
    Build the ordered adaptive quality ladder for focus mode.
    Returns list of (profile_idx, fps) tuples, best quality first.

      profile_idx: index into camera["stream_profiles"] (0 = highest res)
      fps:         None (uncapped) or int fps cap

    Order is FPS-FIRST within each profile block:

      profile[0] @ uncapped → profile[0] @ 30fps → ... → profile[0] @ 1fps
      profile[1] @ uncapped → profile[1] @ 30fps → ... → profile[1] @ 1fps
      ...

    Each step-down reduces fps by 1 at the current profile. Only after
    exhausting all fps values (down to 1fps) does the camera profile drop.
    Switching to a lower-index profile genuinely reduces decode CPU because
    the camera sends fewer pixels over the network.

    Fake ffmpeg post-decode scaling (scale=WxH) is intentionally NOT used
    because it does NOT reduce CPU decode pressure.
    """
    profiles = camera.get("stream_profiles") or []
    if not profiles:
        # Fallback for cameras discovered before stream_profiles was added:
        # synthesise entries from stored stream_url / sub_stream_url.
        profiles = [{"url": camera.get("stream_url", ""),
                     "stream_width":  camera.get("stream_width"),
                     "stream_height": camera.get("stream_height"),
                     "stream_codec":  camera.get("stream_codec")}]
        if camera.get("sub_stream_url"):
            profiles.append({"url": camera.get("sub_stream_url", ""),
                              "stream_width":  camera.get("sub_stream_width"),
                              "stream_height": camera.get("sub_stream_height"),
                              "stream_codec":  camera.get("sub_stream_codec")})
        # 2.4.0-rc2.6: include validated locked-stream candidates as
        # additional profile entries. These were enumerated by Deep
        # Re-Probe and validated post-cred-auth (see api_set_credentials
        # in rc2.6). Each one is a real working stream on this camera —
        # the user can pick them as alternative resolutions in the
        # focus-view dropdown. Skip entries that duplicate the main
        # or sub URL.
        _seen_urls = {camera.get("stream_url", ""),
                      camera.get("sub_stream_url", "")}
        for add in (camera.get("additional_streams") or []):
            au = add.get("url", "")
            if au and au not in _seen_urls:
                profiles.append({
                    "url": au,
                    "stream_width":  None,
                    "stream_height": None,
                    "stream_codec":  None,
                    "_locked_origin": True,  # diagnostic flag
                })
                _seen_urls.add(au)

    # Always build the full ladder so manual Resolution/FPS controls have rungs
    # to snap to. CFG_ADAPTIVE_QUALITY only controls whether the system AUTO-STEPS
    # down the ladder — not whether the ladder exists for manual use.
    # (Previously, when CFG_ADAPTIVE_QUALITY was False, only [(0, None)] was
    # returned, making every manual dropdown selection silently ignored.)
    ladder = []
    for idx in range(len(profiles)):
        ladder.append((idx, None))                   # uncapped — always first
        for fps in range(_ADAPTIVE_FPS_MAX, 0, -1):
            ladder.append((idx, fps))

    return ladder




async def _stop_proc(proc: asyncio.subprocess.Process, *,
                     exited_grace: float = 0.0, timeout: float = 3.0) -> None:
    """Kill an ffmpeg if it is still running, then wait for it.

    2.6.6 (B9): Popen.send_signal() polls the child before signalling
    (Python 3.9+), and that poll collects a child that has already exited.
    asyncio's child watcher then finds no child and logs "Unknown child
    process pid N, will report returncode 255" (seen twice in the test system B logs,
    each right after an ffmpeg EOF). After EOF the process is exiting on
    its own, so give asyncio exited_grace seconds to collect it first.
    """
    if exited_grace and proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=exited_grace)
        except asyncio.TimeoutError:
            pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


async def snap_loop(camera_id: str, url: str, camera: dict, native_res: bool = False) -> None:
    """
    Background task: keeps ffmpeg running for one camera, continuously
    decoding and storing the latest JPEG frame in _SNAP[camera_id]['frame'].
    Restarts automatically on ffmpeg exit/error.
    Stops when no handle_snapshot call has been made in 30 seconds (idle).

    Debug logging:
      SNAP [id]: ffmpeg starting (codec=..., hw=..., vf=...)
      SNAP [id]: frame N — X bytes (last poll Ys ago)   [every 50 frames]
      SNAP [id]: hw timeout/EOF → sw                    [hw→sw fallback]
      SNAP [id]: ffmpeg EOF after N frames (rc=N)        [unexpected exit]
      SNAP [id]: 30s timeout after N frames              [no data from ffmpeg]
      SNAP [id]: idle Ns — stopping                      [idle shutdown]
      SNAP [id]: restarting in 2s (#N)                   [before each restart]
      SNAP [id]: loop done                               [final exit]
    """
    # ── Route to HTTP snapshot polling if a direct snap URL was stored ────────
    # http_snap_loop polls the camera's HTTP JPEG endpoint at ~1 fps, which is
    # far cheaper than running ffmpeg for cameras where RTSP is unreliable or
    # the snap URL is confirmed (Microseven, generic ONVIF/hi3516, Wansview…).
    # EXCEPTION 1: when native_res=True (enhanced view) AND RTSP is available,
    # bypass http_snap_loop so the ffmpeg pipeline runs — this makes the
    # Resolution/FPS controls work and gives real video instead of 1fps polling.
    # EXCEPTION 2 (card view): when probe_rtsp confirmed the RTSP stream works
    # at credential-set time, prefer ffmpeg over http_snap_loop. http_snap_loop
    # produces ~1 fps cached frames (stale-feeling); ffmpeg gives second-by-second
    # live video. http_snap_loop remains the fallback if ffmpeg fails 3× in a row
    # (handled lower in this function).
    #
    # 2.6.0-rc3.1: Item 1 reverted. rc3.0 had relaxed the card-mode gate to
    # `_has_rtsp` alone and added sub/main-stream URL selection for cards.
    # That change was reverted per user request (card RTSP behavior caused
    # issues in rc3.0 field test). Restored to pre-rc3.0 behavior.
    _has_rtsp        = bool(camera.get("stream_url"))
    _rtsp_probe_ok   = bool(camera.get("rtsp_probe_ok"))
    _prefer_ffmpeg   = (native_res and _has_rtsp) or (_has_rtsp and _rtsp_probe_ok)
    if camera.get("http_snap_url") and not _prefer_ffmpeg:
        await http_snap_loop(camera_id, camera)
        return

    state        = _snap_state(camera_id)

    # 2.6.6 (B10): the web server now starts before the hardware probe, so
    # a request can land first. Choose a decoder only once the probe is done.
    if not _HW_PROBED.is_set():
        try:
            await asyncio.wait_for(_HW_PROBED.wait(), timeout=15)
        except asyncio.TimeoutError:
            log.warning(f"SNAP [{camera_id}]: hardware probe still running after "
                        f"15 s — starting anyway")

    # rc2.6: Each snap_loop call is a fresh failure-tracking session.
    # zero_frame_streak persists in _SNAP[cid] across calls (which is needed
    # for proc/task tracking), but the streak counter should NOT inherit from
    # the previous session — that broke the streak==N equality triggers below
    # (transport flip at 3, http_snap fallback at 3 in native_res, codec clear
    # at 5 in non-native). Once streak got bumped past a threshold in one
    # session, the next snap_loop call started with that elevated value and
    # never went back through the equality-trigger value. Result on the Microseven
    # Microseven (rc2.5): stuck in failure loop with backoff=16s indefinitely
    # because streak was already >=5 when we entered, never went back through
    # 3 to fire the http_snap fallback. Reset on entry fixes that. The
    # *_fired flags accompany the streak: each safety-net trigger fires at
    # most once per session, and the flag prevents re-firing if streak ever
    # hits the threshold again later.
    state["zero_frame_streak"]  = 0
    state["transport_flip_fired"] = False
    state["http_snap_fired"]      = False
    state["codec_clear_fired"]    = False

    # 2.3.0: per-session throttle window for this camera's brand. Looked
    # up once here, then floor backoff at this value below so ffmpeg
    # restart cycles never violate the firmware's per-IP TCP rate-limit.
    _snap_throttle_s = _brand_throttle_seconds(camera)
    _snap_ip = camera.get("ip", "")
    if _snap_throttle_s > 0:
        log.info(f"SNAP [{camera_id}]: rate_limit_per_ip_tcp brand — "
                 f"flooring ffmpeg backoff at {_snap_throttle_s:.0f}s")

    # 2.2.9-rc1: `or ""` handles both missing key AND explicit None. The
    # codec-clear path below used to write None into the camera record; if
    # save_cameras() ran between the clear and shutdown, the None persisted
    # to cameras.json and was reloaded on next startup, crashing snap_loop
    # here with `AttributeError: 'NoneType' object has no attribute 'lower'`.
    stream_codec = (camera.get("stream_codec") or "").lower()
    stream_w     = camera.get("stream_width")  or 0
    stream_h     = camera.get("stream_height") or 0
    stream_fps   = camera.get("stream_fps")    or 0
    is_hevc      = stream_codec in ("hevc", "h265")

    # Normal (card) output filters. 3.0.1 (C11): Low FPS Mode is gone; cards
    # run at these caps.
    # 2.6.0-rc3.1: Item 1 reverted to pre-rc3.0 codec-dependent rules.
    # rc3.0 had unified to fps=20 with conditional scale based on sub vs main
    # stream; reverted alongside the rest of Item 1.
    if is_hevc and stream_w >= 3840:
        out_vf = "fps=4,scale=480:-2,format=yuvj420p"
    elif is_hevc:
        out_vf = "fps=8,scale=640:-2,format=yuvj420p"
    else:
        out_vf = "fps=10,scale=640:-2,format=yuvj420p"

    SOI = bytes([0xFF, 0xD8])
    EOI = bytes([0xFF, 0xD9])

    async def _launch_snap(
        hw_args: list[str] | None = None,
        hw_label: str = "",
        native_res: bool = False,
    ) -> None:

        """Launch ffmpeg for snapshot polling.
        native_res=True: use camera's native resolution/fps (for focus view).
        3.0.1 (C11): Skip Non-Reference Frames, Low FPS Mode and Limit Threads
        are gone with their options.

        2.6.0-rc2.3: hw_args is the candidate's ffmpeg_args field copied
        verbatim from _HW_DECODER_CANDIDATES — either a [-c:v <decoder>]
        pair for decoder-name candidates (h264_v4l2m2m, *_vaapi if used
        as -c:v) or a [-hwaccel <name> -c:v <codec>] quad for hwaccel
        candidates (hevc_drm). Caller does the candidate lookup; this
        function just splices the args in.
        """
        hw_args     = list(hw_args) if hw_args else []
        hw_label    = f"hw:{hw_label}" if hw_label else "sw"

        # ffmpeg_url: which URL to actually pass to ffmpeg.
        # For native_res/focus mode this may differ from the outer url variable.
        # Using a separate name avoids Python treating 'url' as local-only and
        # causing an UnboundLocalError in the non-native_res branches.
        ffmpeg_url = url

        if native_res:
            # Focus mode: use adaptive ladder to step through camera profiles + fps.
            cam_now  = CAMERAS.get(camera_id, camera)
            ada      = _FOCUS_ADAPTIVE.setdefault(camera_id, {
                "tier_idx": 0, "locked": False,
                "run_start": None,
                "ladder": _build_focus_ladder(cam_now),
                "restarts_since_lock": 0,
            })
            if not ada["ladder"]:
                ada["ladder"] = _build_focus_ladder(cam_now)
            ladder    = ada["ladder"]
            tier_idx  = min(ada["tier_idx"], len(ladder) - 1)
            prof_idx, tier_fps = ladder[tier_idx]
            profiles  = cam_now.get("stream_profiles") or []
            prof      = profiles[prof_idx] if prof_idx < len(profiles) else {}
            # rc2.5: use the URL stored in the profile entry directly. Falling
            # back to camera[url_key] is unreliable — when the cred-auth path
            # excludes a probe-failed sub-stream from `sub_s` (line ~6269), the
            # camera dict has sub_stream_url=None even though stream_profiles
            # still contains the failed candidate's URL. Without this, manual
            # tier changes to profile[1] silently launched on profile[0]'s URL
            # because build_authenticated_url's old `or stream_url` fallback
            # substituted the main stream URL.
            prof_url     = prof.get("url") or cam_now.get(prof.get("_url_key", "stream_url"))
            tier_url     = build_authenticated_url(cam_now, url=prof_url) if prof_url \
                           else build_authenticated_url(cam_now)
            if tier_url and tier_url != url:
                log.info(f"SNAP [{camera_id}]: adaptive focus — switching to "
                         f"profile[{prof_idx}] for tier {tier_idx}")
            ffmpeg_url = tier_url or url

            # vf filter: fps cap only — NO scale filter (post-decode scaling
            # doesn't reduce CPU; only using a lower camera profile does).
            prof_w    = prof.get("stream_width")  or stream_w or "?"
            prof_h    = prof.get("stream_height") or "?"
            res_label = f"{prof_w}x{prof_h}"
            if tier_fps is None:
                vf_used   = "format=yuvj420p"
                fps_label = f"adaptive:uncapped profile[{prof_idx}] ({res_label})"
            else:
                vf_used   = f"fps={tier_fps},format=yuvj420p"
                fps_label = f"adaptive:{tier_fps}fps profile[{prof_idx}] ({res_label})"
            ada["run_start"] = time.monotonic()
        elif is_hevc and stream_w >= 3840:
            # 3.0.1 (B20): the Pi decodes 3840x2160 H.265 in software slower
            # than the camera sends it (2026-10-02: 2 to 25 pictures a second
            # from a 7 a second stream, working through a backlog), so the
            # card and the motion fallback showed pictures minutes old.
            # Keyframes only: about one picture a second, always current.
            vf_used   = "scale=480:-2,format=yuvj420p"
            fps_label = "keyframes only"
        else:
            vf_used   = out_vf
            fps_label = "normal"
        key_args = ["-skip_frame", "nokey"] if fps_label == "keyframes only" else []
        # 3.3.0 (C4): read the camera through go2rtc, which holds one
        # connection for every user of this stream (B15: the classic view no
        # longer opens a second connection to the camera).
        ffmpeg_url = await _go2rtc_relay(camera_id, ffmpeg_url)
        state["ffmpeg_url_used"] = ffmpeg_url     # for _go2rtc_relay_result below

        log.info(f"SNAP [{camera_id}]: ffmpeg starting "
                 f"(codec={stream_codec or '?'}, {hw_label}, {fps_label}, vf={vf_used})")
        # JPEG quality: 1=best, 31=worst.
        # Focus/native_res: q:v 2 for maximum sharpness at full resolution.
        # Thumbnails: q:v 5 is a good balance of quality vs bandwidth.
        jpeg_q = "2" if native_res else "5"
        # Transport: default TCP for reliability, but some cameras (typically
        # cheap/generic ONVIF devices) accept the TCP SETUP request but reply
        # with UDP in the Transport header — ffmpeg raises "Nonmatching transport
        # in server reply" which surfaces as "Invalid data found when processing
        # input".  After repeated failures snap_loop marks the camera as needing
        # UDP; we honour that here.
        cam_for_transport = CAMERAS.get(camera_id, camera)
        pref_transport    = cam_for_transport.get("preferred_transport", "tcp")
        if _go2rtc_relayed(ffmpeg_url):
            pref_transport = "tcp"      # go2rtc's RTSP server speaks TCP
        transport_args    = ["-rtsp_transport", pref_transport, "-timeout", "8000000"]

        # rc1 (Item B1): -fflags +discardcorrupt on cameras with H.265+ history.
        # Set by _drain_stderr when it sees "Multi-layer HEVC coding is not
        # implemented" in ffmpeg stderr. Tells the demuxer to drop corrupt
        # packets instead of failing the entire decode pipeline. Cleared
        # automatically after 10 consecutive ≥50-frame runs (see below).
        # MUST come before -i (it's an input option).
        # 2.6.1 — Tier 1 item 1: low-latency demuxer and decoder flags.
        # +nobuffer stops the demuxer buffering the input before it emits
        # packets. low_delay tells the decoder not to hold frames for
        # reordering. Both are standard for live RTSP and neither affects
        # stream detection, so both are unconditional. They merge with the
        # existing +discardcorrupt rather than replacing it — ffmpeg takes one
        # -fflags value, so a second flag must be appended to the same string.
        _fflags = "+nobuffer"
        if cam_for_transport.get("needs_fflags_discardcorrupt"):
            _fflags += "+discardcorrupt"
        fflags_args = ["-fflags", _fflags]

        # The probe-reduction flags stay behind CFG_LOW_LATENCY. -probesize 32
        # and -analyzeduration 0 can make ffmpeg give up before it identifies
        # the codec, and -reorder_queue_size 1 drops the RTSP jitter buffer to
        # a single packet, which trades artifacts for latency on a lossy path.
        lowlat_args = ["-flags", "low_delay"]
        if CFG_LOW_LATENCY:
            lowlat_args += ["-probesize", "32",
                            "-analyzeduration", "0",
                            "-reorder_queue_size", "1"]

        return await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "warning",
            *transport_args,
            *fflags_args,
            *lowlat_args,
            "-err_detect", "ignore_err",   # tolerate partial HEVC decode errors
            *key_args,
            *hw_args,
            "-i", ffmpeg_url,
            "-an", "-vf", vf_used,
            "-vcodec", "mjpeg", "-pix_fmt", "yuvj420p",
            "-q:v", jpeg_q, "-f", "image2pipe", "pipe:1",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    try:
        while True:   # outer restart loop
            # Native-res focus task: exit if focus was cleared or switched.
            # task.cancel() alone isn't reliable when ffmpeg is writing rapidly
            # (CancelledError can't be delivered while reads complete immediately).
            # This check makes focus exit deterministic.
            if native_res and anycam_focus._FOCUSED_CAMERA != camera_id:
                log.info(f"SNAP [{camera_id}]: focus cleared — exiting native-res task")
                return

            # 2.6.7: every ffmpeg start waits out the camera's cooldown,
            # as every other connection to it does. On 2026-10-01 the first
            # start came 1 s after go2rtc's attempt, and five more 5 s apart,
            # while a 30 s cooldown was in force for the Microseven.
            if _snap_throttle_s > 0 or _acd_active(_snap_ip):
                await _throttle_wait_if_needed(_snap_ip, _snap_throttle_s,
                                               f"ffmpeg {camera_id}")
                if native_res and anycam_focus._FOCUSED_CAMERA != camera_id:
                    return

            # Idle check — stop if nobody has polled recently
            last   = _snap_last_access.get(camera_id, 0)
            idle_s = time.monotonic() - last
            if state["frame_count"] > 0 and idle_s > 30 and not _motion_uses_snapshots(camera_id):
                log.info(f"SNAP [{camera_id}]: idle {idle_s:.0f}s — stopping")
                return

            # Focus-mode throttle: if another camera has focus, sleep most of
            # the time so the focused camera gets the CPU.
            # 2.6.5: not for an armed camera while the focus is live view,
            # which decodes nothing on the Pi; the wait would blind motion
            # detection for as long as someone watches another camera.
            if (anycam_focus._FOCUSED_CAMERA and anycam_focus._FOCUSED_CAMERA != camera_id
                    and not (_motion_uses_snapshots(camera_id) and anycam_focus._FOCUS_ENGINE == "go2rtc")):
                await asyncio.sleep(1.0)   # ~1fps while another cam is focused
                continue

            # 2.6.0-rc2.3 — bug #1 fix: when in adaptive focus mode,
            # re-derive stream_codec / is_hevc from the active profile.
            # The camera-level stream_codec captured at line 6863 reflects
            # only profile[0] (or whatever was set when the camera was
            # first registered); focus-mode tier switching can pick
            # profile[1] / profile[2] with a potentially different codec
            # (a sub-stream is occasionally MJPEG even when main is HEVC,
            # though the more common case — and the one that motivates
            # this fix — is profile[0] having codec=None at all because
            # of a probe_stream_details miss on the main URL during
            # cred-auth, with profile[1] / profile[2] correctly carrying
            # codec=hevc from the locked-stream / DB-probe path). The
            # reassignment also feeds _launch_snap by closure, so the
            # "ffmpeg starting (codec=...)" log line reflects what's
            # actually being decoded.
            if native_res:
                ada_now = _FOCUS_ADAPTIVE.get(camera_id, {})
                ladder_now = ada_now.get("ladder") or []
                if ladder_now:
                    ti_now = min(ada_now.get("tier_idx", 0), len(ladder_now) - 1)
                    prof_idx_now, _ = ladder_now[ti_now]
                    profs_now = camera.get("stream_profiles") or []
                    if 0 <= prof_idx_now < len(profs_now):
                        prof_codec = (profs_now[prof_idx_now].get("stream_codec")
                                      or "").lower()
                        if prof_codec:
                            stream_codec = prof_codec
                            is_hevc = stream_codec in ("hevc", "h265")

            # Select decoder
            # Respect CFG_HW_DECODE toggle: if disabled, skip hw entirely.
            # When enabled, walk _HW_DECODER_CANDIDATES in preference order and
            # pick the first match for this stream's codec that isn't in
            # _HW_UNAVAILABLE. Each candidate is (label, codec, ffmpeg_args)
            # — see the comment block at the candidate-list definition for
            # how 2.6.0-rc2.3 restructured this around hwaccel-style entries.
            hw_label = ""
            hw_args: list[str] = []
            if CFG_HW_DECODE:
                codec_lower = (stream_codec or "").lower()
                target_codec = ("hevc" if codec_lower in ("hevc", "h265")
                                else "h264" if codec_lower == "h264"
                                else "")
                # 2.6.0-rc2.4 — fix #1: per-snap_loop-session HW skip set.
                # Populated by the timeout/EOF runtime fallback paths after
                # 3 consecutive 0-frame HW failures for the same hw_label.
                # Only affects this snap_loop session — next focus enter or
                # next thumbnail polling cycle gets a fresh `state` dict and
                # retries HW. Decouples per-camera transient runtime issues
                # from the global _HW_UNAVAILABLE init-level disqualifications.
                hw_skip_session = state.get("hw_session_skip") or set()
                if target_codec:
                    for cand_label, cand_codec, cand_args in _HW_DECODER_CANDIDATES:
                        if cand_codec != target_codec:
                            continue
                        if cand_label in _HW_UNAVAILABLE:
                            continue
                        if cand_label in hw_skip_session:
                            continue
                        if _HW_FROZEN.get(camera_id) == cand_label:
                            continue    # 3.7.2 (B37): its pictures froze
                        hw_label = cand_label
                        hw_args  = list(cand_args)
                        break
                if not hw_label:
                    log.debug(f"SNAP [{camera_id}]: no hw decoder available "
                              f"for codec={stream_codec}, using software")

            # 3.0.1 (C11): Fast Stream Start (a second, hardware ffmpeg
            # warming up beside a software one) is gone with its option.
            proc     = await _launch_snap(hw_args=hw_args, hw_label=hw_label,
                                          native_res=native_res)
            state["proc"] = proc
            hw_tried       = bool(hw_label)
            hw_started_at  = time.monotonic() if hw_tried else None
            hw_run         = hw_label        # 3.7.2 (B37): "" once in software
            hw_args_run    = hw_args
            same_n, last_len, frozen = 0, -1, False
            _HW_FALLBACK.pop(camera_id, None)     # 3.7.0 (B11): this launch only
            stderr_t = asyncio.create_task(_drain_stderr(proc, f"SNAP:{camera_id}"))
            buf      = b""
            frames   = 0
            # 2.4.0-rc3.4 Bug 2 fix: per-ffmpeg-run frame counter, reset on
            # every subprocess launch. Used by handle_snapshot to decide
            # X-Stream-Status — we need "has the CURRENT ffmpeg produced any
            # frame yet?", not the lifetime `state["frame_count"]` which
            # accumulates across all sessions (including prior http_snap_loop
            # successes) and so was always non-zero by the time a focus
            # re-entry retry happened, masking the true "ffmpeg dead, retrying"
            # state behind a misleading X-Stream-Status: ok.
            state["current_run_frames"] = 0
            # 2.6.5: a new ffmpeg may run at another resolution (card, focus,
            # adaptive tier). Comparing JPEG sizes across that change would
            # read as motion, so start the comparison afresh.
            _motion_reset_prev(camera_id)

            try:
                while True:
                    # Idle check inside the read loop
                    last   = _snap_last_access.get(camera_id, 0)
                    idle_s = time.monotonic() - last
                    if frames > 10 and idle_s > 30 and not _motion_uses_snapshots(camera_id):
                        log.info(f"SNAP [{camera_id}]: idle {idle_s:.0f}s — stopping")
                        return

                    # 2.6.0-rc2.5 — fix #1: extend HW first-frame timeout
                    # from 3s to 10s. rpivid takes longer than 3s to
                    # produce its first frame at 4K HEVC, especially after
                    # a kill+restart (kernel video device must be
                    # reacquired, decoder context rebuilt, first keyframe
                    # awaited). rc2.4's 3s cap was catching it mid-warmup
                    # and falling back to SW before HW ever got a chance.
                    # Field-test data: every "hw decode timeout → sw" in
                    # rc2.4 logs hit at exactly the 3s mark. 10s gives
                    # honest HW a chance; truly broken HW still falls
                    # back, just 7s later. Bounded either way.
                    timeout = 10.0 if (hw_tried and frames == 0) else 30.0
                    try:
                        chunk = await asyncio.wait_for(
                            proc.stdout.read(65536), timeout=timeout)
                    except asyncio.TimeoutError:
                        if hw_tried and frames == 0:
                            try: proc.kill()
                            except Exception: pass
                            try: await asyncio.wait_for(proc.wait(), timeout=2)
                            except Exception: pass
                            if hw_started_at is not None:
                                elapsed = time.monotonic() - hw_started_at
                                log.info(f"SNAP [{camera_id}]: hw decode "
                                         f"timeout (elapsed={elapsed:.1f}s) → sw")
                            else:
                                log.info(f"SNAP [{camera_id}]: hw decode timeout → sw")
                            # 2.6.0-rc2.4 — fix #1: don't permanently
                            # disqualify this decoder. A timeout on a single
                            # ffmpeg launch can be transient (camera between
                            # keyframes after a kill+restart, brief network
                            # hiccup) and doesn't mean the hardware doesn't
                            # work. Instead, count per-snap_loop-session
                            # 0-frame HW failures and only skip this label
                            # for the rest of THIS session after 3 in a row.
                            # _HW_UNAVAILABLE stays reserved for init-level
                            # "hardware doesn't exist" failures detected in
                            # _probe_hw_decoders or via the "Could not find
                            # a valid device" stderr match in _drain_stderr.
                            # 2.6.0-rc2.4 — fix #2: pass native_res to the
                            # SW fallback launch so Enhanced View preserves
                            # the focus vf (no scaling, full resolution)
                            # instead of dropping to thumbnail vf
                            # (fps=8,scale=640:-2,...). The old call passed
                            # no args, defaulted native_res=False, and made
                            # Enhanced View serve thumbnail-quality output
                            # whenever HW fell back to SW mid-session.
                            hw_fails = state.setdefault("hw_session_fails", {})
                            hw_fails[hw_label] = hw_fails.get(hw_label, 0) + 1
                            if hw_fails[hw_label] >= 3:
                                state.setdefault("hw_session_skip", set()
                                                 ).add(hw_label)
                                log.info(f"SNAP [{camera_id}]: {hw_label} "
                                         f"failed 3 times this session — "
                                         f"skipping for remainder of "
                                         f"snap_loop")
                            proc     = await _launch_snap(native_res=native_res)
                            state["proc"] = proc
                            stderr_t.cancel()
                            stderr_t = asyncio.create_task(
                                _drain_stderr(proc, f"SNAP:{camera_id}"))
                            buf      = b""
                            hw_tried = False
                            hw_run   = ""
                            continue
                        log.warning(f"SNAP [{camera_id}]: "
                                    f"30s read timeout after {frames} frames")
                        break

                    if not chunk:
                        rc = proc.returncode
                        if hw_tried and frames == 0:
                            # 2.6.6 (B9): EOF, so ffmpeg is exiting; see _stop_proc.
                            await _stop_proc(proc, exited_grace=1.0, timeout=2)
                            if hw_started_at is not None:
                                elapsed = time.monotonic() - hw_started_at
                                log.info(f"SNAP [{camera_id}]: hw EOF "
                                         f"(rc={rc}, elapsed={elapsed:.1f}s) → sw")
                            else:
                                log.info(f"SNAP [{camera_id}]: hw EOF (rc={rc}) → sw")
                            # 2.6.0-rc2.4 — same fix as the timeout branch
                            # above. See the comment block there for the
                            # full rationale; mirroring the logic so EOF and
                            # timeout paths stay parallel.
                            hw_fails = state.setdefault("hw_session_fails", {})
                            hw_fails[hw_label] = hw_fails.get(hw_label, 0) + 1
                            if hw_fails[hw_label] >= 3:
                                state.setdefault("hw_session_skip", set()
                                                 ).add(hw_label)
                                log.info(f"SNAP [{camera_id}]: {hw_label} "
                                         f"failed 3 times this session — "
                                         f"skipping for remainder of "
                                         f"snap_loop")
                            proc     = await _launch_snap(native_res=native_res)
                            state["proc"] = proc
                            stderr_t.cancel()
                            stderr_t = asyncio.create_task(
                                _drain_stderr(proc, f"SNAP:{camera_id}"))
                            buf      = b""
                            hw_tried = False
                            hw_run   = ""
                            continue
                        # 2.3.1: distinguish focus-leave kill (clean shutdown,
                        # we caused it) from natural EOF (ffmpeg actually died).
                        # The flag is set in handle_focus_clear right before the
                        # proc.kill() that triggers this EOF — log it differently
                        # so the warning channel doesn't carry a false alarm.
                        if state.get("focus_leave_kill"):
                            log.info(f"SNAP [{camera_id}]: ffmpeg killed "
                                     f"by focus-leave after {frames} frames "
                                     f"(rc={rc})")
                        else:
                            log.warning(f"SNAP [{camera_id}]: "
                                        f"ffmpeg EOF after {frames} frames (rc={rc})")
                        break

                    buf += chunk
                    if len(buf) > 4_000_000:
                        log.warning(f"SNAP [{camera_id}]: "
                                    f"buf overflow ({len(buf)} bytes) — discarding")
                        buf = b""
                        continue

                    while True:
                        s = buf.find(SOI)
                        if s < 0:
                            buf = b""
                            break
                        e = buf.find(EOI, s + 2)
                        if e < 0:
                            if s > 0:
                                buf = buf[s:]
                            break
                        frame    = buf[s : e + 2]
                        buf      = buf[e + 2:]
                        frames  += 1
                        # 2.6.0-rc2.5 — fix #3: log time to first HW
                        # frame. Proves HW worked end-to-end and shows
                        # how long rpivid warmup took for this stream.
                        # The diagnostic feeds future timeout tuning —
                        # if every camera consistently shows 4-5s, we
                        # know 10s is right; if they're all <2s, we
                        # could tighten back; if some need 12s+, we'd
                        # know to raise it.
                        if hw_tried and hw_started_at is not None:
                            hw_first_frame_s = time.monotonic() - hw_started_at
                            fallback = _HW_FALLBACK.get(camera_id)
                            if fallback:
                                # 3.7.0 (B11): ffmpeg went on in software
                                log.warning(f"SNAP [{camera_id}]: first frame in "
                                            f"{hw_first_frame_s:.1f}s, decoded in SOFTWARE: "
                                            f"the {hw_label} hardware decoder did not open "
                                            f"(ffmpeg: {fallback})")
                            else:
                                log.info(f"SNAP [{camera_id}]: hw first frame "
                                         f"in {hw_first_frame_s:.1f}s "
                                         f"({hw_label})")
                            hw_started_at = None
                        hw_tried = False   # got a frame → hw decode worked
                        state["frame"]       = frame
                        state["frame_time"]  = time.monotonic()
                        state["frame_count"] += 1
                        # 2.4.0-rc3.4 Bug 2 fix: per-run counter, see launch site.
                        state["current_run_frames"] = state.get("current_run_frames", 0) + 1

                        # Native-res focus task: exit the inner loop the moment
                        # focus is cleared so the task terminates quickly without
                        # waiting for task.cancel() to be delivered.
                        if native_res and anycam_focus._FOCUSED_CAMERA != camera_id:
                            break

                        if frames == 1 or frames % 50 == 0:
                            poll_ago = time.monotonic() -                                        _snap_last_access.get(camera_id, time.monotonic())
                            log.info(f"SNAP [{camera_id}]: frame {frames} "
                                     f"— {len(frame)} bytes "
                                     f"(last poll {poll_ago:.1f}s ago)")

                        _motion_on_frame(camera_id, frame)

                        if hw_run and camera_id not in _HW_FALLBACK:
                            same_n, last_len, frozen = _hw_frozen_step(same_n, last_len, len(frame))
                            if frozen:
                                break

                    # 3.7.2 (B37): the hardware gave one unchanging picture
                    if frozen:
                        log.warning(f"SNAP [{camera_id}]: {hw_run} gave {HW_FROZEN_FRAMES} "
                                    f"pictures in a row of exactly {last_len} bytes: the "
                                    f"hardware picture is wrong. Decoding this camera in "
                                    f"software until AnyCam restarts")
                        first_time = camera_id not in _HW_FROZEN
                        _HW_FROZEN[camera_id] = hw_run
                        await _stop_proc(proc, timeout=3)
                        if first_time:
                            asyncio.create_task(_hw_picture_test(
                                camera_id, state.get("ffmpeg_url_used") or url, list(hw_args_run)))
                        proc     = await _launch_snap(native_res=native_res)
                        state["proc"] = proc
                        stderr_t.cancel()
                        stderr_t = asyncio.create_task(_drain_stderr(proc, f"SNAP:{camera_id}"))
                        buf      = b""
                        hw_tried = False
                        hw_run   = ""
                        frozen   = False
                        frames   = 0          # a new ffmpeg: count its pictures
                        state["current_run_frames"] = 0
                        _motion_reset_prev(camera_id)

            except asyncio.CancelledError:
                log.info(f"SNAP [{camera_id}]: task cancelled")
                raise
            except Exception as ex:
                log.warning(f"SNAP [{camera_id}]: inner exception: {ex}")
            finally:
                stderr_t.cancel()
                # 2.6.6 (B9): most exits here follow EOF; see _stop_proc.
                await _stop_proc(proc, exited_grace=0.5, timeout=3)
                # 3.7.2: asyncio.wait, not wait_for: a reader cancelled before
                # it ran raises CancelledError, which read as this loop cancelled.
                await asyncio.wait({stderr_t}, timeout=2)

            # Before restarting, check idle
            last   = _snap_last_access.get(camera_id, 0)
            idle_s = time.monotonic() - last
            if frames > 0 and idle_s > 30 and not _motion_uses_snapshots(camera_id):
                log.info(f"SNAP [{camera_id}]: idle {idle_s:.0f}s after exit — not restarting")
                return

            # 2.3.1: If this exit was due to focus-leave (we killed ffmpeg in
            # handle_focus_clear), don't restart here — return to let the next
            # handle_snapshot poll spawn a fresh thumbnail-mode (native_res=False)
            # snap_loop. Without this, after the user leaves enhanced view we
            # would either restart at native_res=True (still high-res, wrong
            # mode) or run a stale loop. The flag is one-shot: consumed here
            # so future natural restarts behave normally.
            if state.get("focus_leave_kill"):
                state.pop("focus_leave_kill", None)
                log.info(f"SNAP [{camera_id}]: returning to thumbnail polling "
                         f"(focus-leave clean exit, {frames} frames)")
                return

            # rc1 (Item B1): track clean runs to eventually clear the
            # -fflags +discardcorrupt flag. A "clean run" is one that produced
            # at least 50 frames before exiting — partial runs that died early
            # don't count (those are exactly the runs the flag is supposed to
            # be helping). After 10 consecutive clean runs, clear the flag and
            # let the next ffmpeg launch run unflagged. If the camera firmware
            # was fixed (or the H.265+ pattern stops appearing), this lets us
            # automatically drop the workaround. _drain_stderr will re-set the
            # flag immediately if the pattern shows up again on the unflagged
            # run.
            cam_now = CAMERAS.get(camera_id)
            if cam_now and cam_now.get("needs_fflags_discardcorrupt"):
                if frames >= 50:
                    cam_now["clean_runs_since_fflags"] = (
                        cam_now.get("clean_runs_since_fflags", 0) + 1)
                    if cam_now["clean_runs_since_fflags"] >= 10:
                        cam_now.pop("needs_fflags_discardcorrupt", None)
                        cam_now.pop("clean_runs_since_fflags", None)
                        log.info(f"SNAP [{camera_id}]: 10 consecutive clean runs "
                                 f"— clearing -fflags +discardcorrupt")
                        try:
                            save_cameras()
                        except Exception as ex:
                            log.debug(f"SNAP [{camera_id}]: save_cameras after "
                                      f"fflags clear failed: {ex}")

            # 2.3.1: After a clean run (>=50 frames), clear the H.265+ red
            # badge if it was set. _drain_stderr fires the "Multi-layer HEVC"
            # warning whenever ffmpeg emits that stderr line — but on many
            # Hikvision streams that line appears even though the camera is
            # NOT actually on H.265+ (false positive in ffmpeg's codec
            # detection). If frames flowed cleanly, we have direct evidence
            # the stream is decodable, so the badge is misleading. Clear it
            # and set hevc_plus_noise_confirmed=True so future ffmpeg cycles
            # don't re-set the badge from the same stderr noise. The
            # -fflags +discardcorrupt workaround stays applied as defensive
            # cover (it's harmless on a clean stream).
            if cam_now and frames >= 50 and cam_now.get("hevc_plus_warning"):
                cam_now["hevc_plus_warning"] = False
                cam_now["hevc_plus_noise_confirmed"] = True
                log.info(f"SNAP [{camera_id}]: clean run ({frames} frames) "
                         f"— clearing H.265+ badge "
                         f"(ffmpeg's Multi-layer HEVC warning was a false alarm)")
                try:
                    save_cameras()
                except Exception as ex:
                    log.debug(f"SNAP [{camera_id}]: save_cameras after "
                              f"badge clear failed: {ex}")

            state["restart_count"] += 1

            # Exponential backoff when ffmpeg keeps dying with 0 frames
            # (e.g. wrong codec, bad URL, camera rejecting connection).
            # 0-frame failures: 1s, 2s, 4s, 8s, 16s, 32s (cap at 32s).
            # Normal failures (got some frames): always 2s.
            #
            # 2.3.0: For rate_limit_per_ip_tcp brands (Hipcam family),
            # floor the backoff at the brand's documented cooldown so
            # ffmpeg restart cycles never violate the per-IP TCP limit.
            # Without this, the early sequence (1s, 2s, 4s) is inside the
            # Hipcam 5s window and accumulates lockout pressure across
            # restarts.
            _go2rtc_relay_result(camera_id, state.get("ffmpeg_url_used"), frames > 0)
            if frames == 0:
                streak = state.get("zero_frame_streak", 0) + 1
                state["zero_frame_streak"] = streak
                _raw_backoff = min(2 ** min(streak - 1, 4), 32)
                if _snap_throttle_s > 0:
                    backoff = max(_snap_throttle_s, _raw_backoff)
                else:
                    backoff = _raw_backoff
                # 2.6.7: a camera that is resetting connections gets the
                # escalated cooldown between restarts, not the 5 s floor.
                _resetting = _acd_active(_snap_ip)
                if _resetting:
                    backoff = max(backoff, ACD_ESCALATED_COOLDOWN)

                # After 3 consecutive 0-frame failures, try flipping the
                # RTSP transport.  Many cheap/generic ONVIF cameras (Sricam,
                # Microseven, etc.) accept the TCP SETUP but reply with UDP —
                # ffmpeg calls this "Nonmatching transport in server reply"
                # which surfaces as "Invalid data found when processing input".
                # Flipping to UDP fixes this class of camera entirely.
                #
                # rc2.6: Use >= with fired-flag pattern so the trigger fires
                # at most once per session even if streak skips past 3.
                #
                # 2.4.0-rc3.2: removed the `not native_res` gate. Previously
                # the flip ran only in thumbnail mode, which meant focus
                # mode never benefited — and for the canonical victim
                # (Microseven), thumbnail mode uses http_snap_url and
                # never exercises RTSP at all, so the flip never fired
                # anywhere. Result: preferred_transport stayed "tcp"
                # forever even on cameras that only speak UDP RTSP, and
                # every focus session crashed 3 times before the
                # http_snap fallback below kicked in. Now the flip runs
                # in both modes; the http_snap fallback in focus mode
                # is gated on transport_flip_fired so we only give up
                # on RTSP after BOTH transports have failed.
                # The UDP→TCP revert branch keeps the `not native_res`
                # gate so thumbnail mode still cycles TCP↔UDP on
                # cameras that fail both, while focus mode falls
                # through to the http_snap fallback below.
                if (streak >= 3 and not state.get("transport_flip_fired")):
                    cam_now = CAMERAS.get(camera_id, {})
                    cur_transport = cam_now.get("preferred_transport", "tcp")
                    if cur_transport == "tcp":
                        log.warning(f"SNAP [{camera_id}]: 3 consecutive 0-frame failures "
                                    f"with TCP — switching to UDP transport (camera may "
                                    f"not support TCP RTSP)")
                        CAMERAS[camera_id]["preferred_transport"] = "udp"
                        state["zero_frame_streak"] = 0   # fresh count for UDP
                        state["transport_flip_fired"] = True
                        # Reset local streak too so the http_snap fallback
                        # block below doesn't fire on the same iteration —
                        # it reads `streak` (local) not state's copy.
                        streak = 0
                    elif cur_transport == "udp" and not native_res:
                        log.warning(f"SNAP [{camera_id}]: 3 consecutive 0-frame failures "
                                    f"with UDP also — reverting to TCP")
                        CAMERAS[camera_id]["preferred_transport"] = "tcp"
                        state["zero_frame_streak"] = 0
                        state["transport_flip_fired"] = True
                        streak = 0
                    elif cur_transport == "udp" and native_res:
                        # 2.4.0-rc3.4 Bug 1 fix: previous version had no branch
                        # for "currently UDP + focus mode". `preferred_transport`
                        # is persisted to disk in CAMERAS[cid], so once a prior
                        # session flipped it to UDP, every subsequent focus
                        # session entered with cur_transport="udp" and fell
                        # through both arms silently — transport_flip_fired
                        # stayed False forever, which gated the http_snap
                        # fallback below at `state.get("transport_flip_fired")`,
                        # so ffmpeg restart-looped indefinitely. Microseven
                        # users saw this as a frozen frame for the entire focus
                        # session (observed: 28+ minutes across two re-focuses
                        # in the 2.4.0-rc3.3 log). Now we revert to TCP and arm
                        # the fallback gate; if TCP also fails the next 3
                        # attempts, the http_snap fallback fires as designed.
                        log.warning(f"SNAP [{camera_id}]: 3 consecutive 0-frame failures "
                                    f"with UDP (persisted from earlier session) "
                                    f"— reverting to TCP for this focus session")
                        CAMERAS[camera_id]["preferred_transport"] = "tcp"
                        state["zero_frame_streak"] = 0
                        state["transport_flip_fired"] = True
                        streak = 0

                # After 3 consecutive 0-frame failures in native_res (enhanced
                # view) mode, fall back to http_snap_loop if the camera has one.
                # This handles cameras like the Microseven whose RTSP is
                # stored but non-functional — ffmpeg keeps crashing, wasting CPU.
                #
                # rc2.6: >= with fired-flag pattern (see above).
                #
                # 2.4.0-rc3.2: now requires transport_flip_fired so the
                # fallback only fires AFTER UDP has also failed. This is
                # the second half of the Microseven fix — we want to
                # exhaust both transports before giving up on RTSP.
                # 2.6.7: at once, without more RTSP attempts, when the
                # camera is resetting connections (escalated cooldown).
                if (native_res and not state.get("http_snap_fired")
                        and (_resetting or (streak >= 3
                                            and state.get("transport_flip_fired")))):
                    cam_now = CAMERAS.get(camera_id, camera)
                    if cam_now.get("http_snap_url"):
                        why = ("the camera is resetting RTSP connections" if _resetting
                               else "3 consecutive 0-frame failures in enhanced view")
                        log.warning(
                            f"SNAP [{camera_id}]: {why} — RTSP non-functional, "
                            f"falling back to HTTP snap loop for this focus session"
                        )
                        # Mark state so JS can disable resolution/fps controls
                        _snap_state(camera_id)["http_snap_active"] = True
                        state["http_snap_fired"] = True
                        await http_snap_loop(camera_id, cam_now)
                        _snap_state(camera_id)["http_snap_active"] = False
                        # 2.4.0-rc3.3 Bug A fix (defensive): if focus_leave_kill
                        # was set during the http_snap_loop session (e.g. user
                        # left focus while we were in HTTP-snap mode), clear it
                        # here so it doesn't leak to a subsequent focus entry.
                        # The handle_focus_enter clear is the primary fix; this
                        # is belt-and-suspenders for the case where the state
                        # dict gets read between handle_focus_clear setting the
                        # flag and the next handle_focus_enter clearing it.
                        state.pop("focus_leave_kill", None)
                        return

                # rc2.6: clear stored codec on persistent failure, regardless
                # of native_res. Was non-native only — but the same wrong-codec
                # issue affects focus mode too (a sub-stream can have a wrong
                # codec hint just like the main stream). Without this,
                # native_res sessions stayed stuck if the cred-auth codec
                # correction had failed (Hipcam rate-limit window), since the
                # http_snap fallback above (also new fix) at least lets the UI
                # degrade gracefully but does not retry RTSP at the corrected
                # codec.
                if (streak >= 5 and not state.get("codec_clear_fired")):
                    cam_now = CAMERAS.get(camera_id, {})
                    if cam_now.get("stream_codec"):
                        log.warning(f"SNAP [{camera_id}]: 5 consecutive 0-frame failures — "
                                    f"clearing stored codec {cam_now['stream_codec']!r} "
                                    f"so ffmpeg can auto-detect on next attempt")
                        # 2.2.9-rc1: write "" not None. Sentinel was None
                        # in 2.2.9, but if save_cameras() runs between the
                        # clear and shutdown the None persists to disk and
                        # crashes the next startup at the .lower() above.
                        # "" is falsy in the same places None was used and
                        # safe under .lower().
                        CAMERAS[camera_id]["stream_codec"] = ""
                        profs = CAMERAS[camera_id].get("stream_profiles") or []
                        if profs:
                            profs[0]["stream_codec"] = ""
                        state["codec_clear_fired"] = True
                        # Don't save_cameras here — this is a runtime override only
                    # 3.1.0 (B25): the saved streams may be out of date
                    # (the camera's settings changed). Read them again with
                    # the saved password; _streams_refresh limits how often.
                    asyncio.create_task(_streams_refresh(camera_id, "5 failed stream starts"))
            else:
                backoff = 2
                state["zero_frame_streak"] = 0

            # ── Adaptive fps for focus/native_res mode ────────────────────────
            # Step-down only: never step back up once stable.
            # ── Adaptive fps/res controller for focus/native_res mode ─────────
            # Only run when this camera still has focus — prevents post-exit
            # step-downs from firing after focus was cleared while ffmpeg was
            # still running.
            if native_res and anycam_focus._FOCUSED_CAMERA == camera_id:
                cam_now   = CAMERAS.get(camera_id, camera)
                ada       = _FOCUS_ADAPTIVE.setdefault(camera_id, {
                    "tier_idx": 0, "locked": False,
                    "run_start": None,
                    "ladder": _build_focus_ladder(cam_now),
                    "restarts_since_lock": 0,
                })
                ladder    = ada["ladder"]
                tier_idx  = ada["tier_idx"]
                run_start = ada.get("run_start") or time.monotonic()
                run_dur   = time.monotonic() - run_start
                locked    = ada.get("locked", False)

                # Skip adaptive stepping when Adaptive Quality is disabled in
                # config: the system never auto-steps then. (3.0.0-rc1.0: the
                # manual tier pin went with its endpoints, build plan E8.)
                if not CFG_ADAPTIVE_QUALITY:
                    fast_death       = False
                    restart_overflow = False
                else:
                    # frames == 0 is always a failure regardless of run duration
                    fast_death = (frames == 0 or
                                  (run_dur < _ADAPTIVE_UNSTABLE_S and
                                   frames  < _ADAPTIVE_UNSTABLE_FR))

                # Count restarts at locked tier
                if locked:
                    ada["restarts_since_lock"] = ada.get("restarts_since_lock", 0) + 1
                # 2.4.0-rc3.1: Block B parallels Block A's gating, so repeated
                # restarts never auto-step while Adaptive Quality is off.
                if not CFG_ADAPTIVE_QUALITY:
                    restart_overflow = False
                else:
                    restart_overflow = (locked and
                                        ada.get("restarts_since_lock", 0) >= _ADAPTIVE_RESTART_LIMIT)

                if fast_death or restart_overflow:
                    reason = (f"fast-death ({frames}fr in {run_dur:.1f}s)"
                              if fast_death else
                              f"repeated-restart ({ada['restarts_since_lock']}x at locked tier)")
                    if locked:
                        ada["locked"] = False
                        ada["restarts_since_lock"] = 0
                        log.info(f"SNAP [{camera_id}]: adaptive focus — "
                                 f"locked tier unstable ({reason}), stepping down")
                    if tier_idx < len(ladder) - 1:
                        ada["tier_idx"] += 1
                        new_prof_idx, new_fps = ladder[ada["tier_idx"]]
                        profiles   = cam_now.get("stream_profiles") or []
                        new_prof   = profiles[new_prof_idx] if new_prof_idx < len(profiles) else {}
                        new_res    = (f"{new_prof.get('stream_width')}x"
                                      f"{new_prof.get('stream_height')}")
                        log.info(
                            f"SNAP [{camera_id}]: adaptive focus — {reason}, "
                            f"stepping down to "
                            f"{'uncapped' if new_fps is None else str(new_fps)+'fps'}"
                            f" profile[{new_prof_idx}] ({new_res})"
                        )
                        new_url_key = new_prof.get("_url_key", "stream_url")
                        next_url    = build_authenticated_url(cam_now, url_key=new_url_key)
                        if next_url:
                            url = next_url
                    else:
                        log.warning(
                            f"SNAP [{camera_id}]: adaptive focus — reached end of "
                            f"quality ladder, staying at lowest tier"
                        )
                else:
                    # Run was stable — lock here, reset restart counter
                    if not locked:
                        locked_prof_idx, locked_fps = ladder[tier_idx]
                        profiles    = cam_now.get("stream_profiles") or []
                        locked_prof = profiles[locked_prof_idx] if locked_prof_idx < len(profiles) else {}
                        locked_res  = (f"{locked_prof.get('stream_width')}x"
                                       f"{locked_prof.get('stream_height')}")
                        log.info(
                            f"SNAP [{camera_id}]: adaptive focus — stable at "
                            f"{'uncapped' if locked_fps is None else str(locked_fps)+'fps'}"
                            f" profile[{locked_prof_idx}] ({locked_res}) — locking"
                        )
                        ada["locked"] = True
                        ada["restarts_since_lock"] = 0

            # ── H.265+ fallback: probe alternate URLs on first restart after flag ──
            # _drain_stderr sets camera["hevc_plus_warning"] = True when it detects
            # the "Multi-layer HEVC coding is not implemented" ffmpeg error message.
            # On the FIRST restart after that flag appears, probe sub-stream and
            # H.264-transcode URLs; if one responds, switch to it permanently.
            cam_dict = CAMERAS.get(camera_id, camera)
            if cam_dict.get("hevc_plus_warning") and state["restart_count"] == 1:
                log.info(f"SNAP [{camera_id}]: H.265+ detected — probing fallback URLs")
                fallback_url = await _try_hevc_plus_fallback(camera_id, cam_dict, url)
                if fallback_url and fallback_url != url:
                    log.info(f"SNAP [{camera_id}]: switching to fallback URL "
                             f"→ {_strip_creds(fallback_url)}")
                    # Patch the url variable for all subsequent loop iterations
                    url = fallback_url
                    # Also update the stored stream_url so it persists across restarts
                    cam_dict["stream_url"] = _strip_creds(fallback_url)
                    CAMERAS[camera_id] = cam_dict
                    save_cameras()
                    # Clear the warning now that we have a working alternative
                    cam_dict["hevc_plus_warning"] = False
                    cam_dict["hevc_plus_fallback_active"] = True

            log.info(f"SNAP [{camera_id}]: restarting in {backoff}s "
                     f"(#{state['restart_count']})")
            # ±10% jitter prevents multiple cameras from hammering resources
            # in lockstep after a shared network event (e.g. a brief outage).
            await asyncio.sleep(backoff * random.uniform(0.9, 1.1))

    finally:
        # rc2.4: Gate state["proc"] = None on current-task ownership to fix
        # the "manual tier change silently no-ops" bug. Without this guard, an
        # OLD snap_loop task whose cancellation finalises AFTER the NEW task
        # has already written `state["proc"] = new_proc` would overwrite that
        # reference back to None on its way out. A later kill of the process then
        # reads `state.get("proc")` → None → skips the kill path → the new
        # ffmpeg keeps running its old profile/fps forever despite the manual
        # tier change being recorded server-side.
        #
        # The same conditional already protects state["task"] below for the
        # analogous reason (clearing it unconditionally caused the duplicate-
        # loop bug that handle_snapshot's task-presence check was supposed to
        # prevent). Apply the same pattern to state["proc"].
        if state.get("task") is asyncio.current_task():
            state["proc"] = None
            state["task"] = None
        log.info(f"SNAP [{camera_id}]: loop done")


async def http_snap_loop(camera_id: str, camera: dict) -> None:
    """
    Background task: polls an HTTP snapshot URL at ~1 fps and stores the JPEG
    bytes in _SNAP[camera_id]['frame'] — the same buffer that handle_snapshot
    reads, so the card-view machinery is completely untouched.

    Stops after 30 s of idle (no handle_snapshot calls while a frame exists).

    Debug logging:
      SNAP [id]: http starting → <url>
      SNAP [id]: http frame N — X bytes               [every 50 frames]
      SNAP [id]: http status N                        [non-200 response]
      SNAP [id]: http error: <exc>                    [request failure]
      SNAP [id]: idle Ns — stopping                   [idle shutdown]
      SNAP [id]: http loop done                       [final exit]

    Auth modes (stored in camera['http_snap_auth_mode']):
      'basic'        — HTTP Basic/Digest auth (default for all cameras)
      'query_params' — credentials appended as &user=…&password=… in the URL
                       (Reolink CGI API requires this)
    """
    state     = _snap_state(camera_id)
    snap_url  = camera["http_snap_url"]
    creds     = camera.get("credentials")
    auth_mode = camera.get("http_snap_auth_mode", "basic")

    auth = None
    u = p = ""
    if creds:
        try:
            u, p = decrypt_creds(creds)
            if auth_mode == "basic":
                auth = aiohttp.BasicAuth(u, p)
        except Exception as exc:
            log.debug(f"SNAP [{camera_id}]: http_snap_loop: decrypt_creds failed: {exc}")

    log.info(f"SNAP [{camera_id}]: http starting → {snap_url}")
    _motion_reset_prev(camera_id)   # 2.6.5: see the same call in snap_loop

    timeout = aiohttp.ClientTimeout(total=5)
    # ssl=False: LAN cameras often present self-signed certs (Hikvision redirects
    # http→https with a self-signed cert).  The connection remains TLS-encrypted;
    # we skip cert *verification* only, which is appropriate on a trusted LAN.
    _connector = aiohttp.TCPConnector(ssl=False)

    def _make_digest_auth(www_auth: str, method: str, uri: str) -> str:
        return _http_digest_header(www_auth, method, uri, u, p)

    # Track consecutive error count to rate-limit log noise
    _err_count     = 0
    _err_logged_at = 0  # frame count when we last logged an error

    async with aiohttp.ClientSession(timeout=timeout, connector=_connector) as session:

        # ── Options 1+2: One-time redirect probe ──────────────────────────────
        # aiohttp strips the Authorization header when following http→https
        # redirects (different scheme), so Digest auth never reaches the camera.
        # Fix: probe with allow_redirects=False, detect the redirect ourselves,
        # follow it manually so the auth header survives to the final URL.
        # If the final URL is https://, upgrade snap_url permanently so every
        # subsequent request goes straight to https:// — no further redirects.
        if snap_url.startswith("http://") and auth and auth_mode == "basic":
            try:
                async with session.get(
                    snap_url, auth=auth, allow_redirects=False
                ) as _probe:
                    if _probe.status in (301, 302, 303, 307, 308):
                        _loc = _probe.headers.get("Location", "")
                        if _loc.startswith("https://") or _loc.startswith("http://"):
                            snap_url = _loc
                            camera["http_snap_url"] = snap_url
                            save_cameras()   # persist so next restart uses https:// directly
                            log.info(
                                f"SNAP [{camera_id}]: redirect detected → "
                                f"upgrading snap URL to {snap_url}"
                            )
            except Exception as _exc:
                log.debug(f"SNAP [{camera_id}]: redirect probe error: {_exc}")

        while True:
            # ── Idle check: stop if nothing has polled us in 30 s ────────────
            last   = _snap_last_access.get(camera_id, 0)
            idle_s = time.monotonic() - last
            if (idle_s > 30 and state["frame"] is not None
                    and not _motion_uses_snapshots(camera_id)):
                log.info(f"SNAP [{camera_id}]: idle {idle_s:.0f}s — stopping")
                break

            # ── Build request URL (Reolink needs creds in query params) ───────
            request_url = snap_url
            if auth_mode == "query_params" and u:
                request_url = f"{snap_url}&user={u}&password={p}"

            # Parse path for Digest uri field
            try:
                _parsed_path = request_url.split("//", 1)[1].split("/", 1)[1]
                _uri = "/" + _parsed_path
            except (IndexError, ValueError):
                _uri = "/"

            try:
                # ── Step 1: try with Basic auth (or no auth for query_params) ─
                headers: dict[str, str] = {}
                if auth and auth_mode == "basic":
                    # Send Basic auth first — many cameras accept it
                    async with session.get(
                        request_url, auth=auth, allow_redirects=True
                    ) as resp1:
                        status1 = resp1.status
                        www_auth = resp1.headers.get("WWW-Authenticate", "")

                    if status1 == 401 and www_auth.startswith("Digest") and u:
                        # ── Step 2: camera requires Digest auth — compute and retry
                        dig_header = _make_digest_auth(www_auth, "GET", _uri)
                        headers = {"Authorization": dig_header}
                        async with session.get(
                            request_url, headers=headers, allow_redirects=True
                        ) as resp2:
                            final_status = resp2.status
                            data = await resp2.read() if final_status == 200 else b""
                    elif status1 == 200:
                        async with session.get(
                            request_url, auth=auth, allow_redirects=True
                        ) as resp_ok:
                            final_status = resp_ok.status
                            data = await resp_ok.read() if final_status == 200 else b""
                    else:
                        final_status = status1
                        data = b""
                else:
                    # query_params or no credentials
                    async with session.get(
                        request_url, allow_redirects=True
                    ) as resp:
                        final_status = resp.status
                        data = await resp.read() if final_status == 200 else b""

                if final_status == 200:
                    if data and len(data) > 200:
                        state["frame"]       = data
                        state["frame_time"]  = time.monotonic()
                        state["frame_count"] = (state.get("frame_count") or 0) + 1
                        fc = state["frame_count"]
                        _err_count = 0
                        # 2.6.5: motion detection on this path too. Every
                        # Lorex channel runs here, and before 2.6.5 motion
                        # was only checked on the ffmpeg path.
                        _motion_on_frame(camera_id, data)
                        if fc % 50 == 0:
                            log.debug(
                                f"SNAP [{camera_id}]: http frame {fc} "
                                f"— {len(data)} bytes"
                            )
                    else:
                        log.debug(
                            f"SNAP [{camera_id}]: http got {len(data) if data else 0} "
                            f"bytes (too small, discarding)"
                        )
                else:
                    _err_count += 1
                    fc = state.get("frame_count") or 0
                    # Log first failure, then every 30th, to avoid log flood
                    if _err_count == 1 or (_err_count % 30 == 0):
                        log.warning(
                            f"SNAP [{camera_id}]: http status {final_status} "
                            f"(consecutive failures: {_err_count})"
                        )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                _err_count += 1
                if _err_count == 1 or (_err_count % 30 == 0):
                    log.warning(f"SNAP [{camera_id}]: http error: {exc} "
                                f"(consecutive failures: {_err_count})")

            # ── Option 4: ffmpeg fallback after 60 consecutive failures ────────
            # If HTTP snap has never produced a frame and has failed 60 times
            # (≈60 s), clear http_snap_url so snap_loop falls through to the
            # ffmpeg path on the next restart, using the confirmed RTSP URL.
            if _err_count >= 60 and state["frame"] is None:
                log.warning(
                    f"SNAP [{camera_id}]: {_err_count} consecutive HTTP snap "
                    f"failures with no frame — falling back to ffmpeg snap loop"
                )
                camera["http_snap_url"] = None
                break

            try:
                await asyncio.sleep(1.0)   # ~1 fps
            except asyncio.CancelledError:
                break

    if state.get("task") is asyncio.current_task():
        state["task"] = None
    log.info(f"SNAP [{camera_id}]: http loop done")


SNAP_MAX_AGE_S = 10.0      # 3.0.1 (B20): an older picture is not shown on a card


async def handle_snapshot(request: web.Request) -> web.Response:
    """
    Return the latest JPEG frame for a camera.
    Starts the background snap_loop if not already running.
    Called by the JS polling loop every ~125ms for live video display.

    The snap_loop runs persistently in the background, continuously decoding
    the camera stream and storing the latest frame.  This endpoint just
    returns whatever is in the buffer — each response is a fast,
    complete HTTP round-trip that HA's ingress proxy handles cleanly
    (unlike long-lived multipart streams which nginx terminates early).

    Debug logging (server side):
      SNAP [id]: starting background process       — on first call per camera
      SNAP [id]: waiting for first frame...         — before first frame arrives
      SNAP [id]: no frame available after 5s        — timeout on first frame
      SNAP [id]: serving stale frame (age=Ns)       — ffmpeg died, cached frame
    """
    camera_id = request.match_info["camera_id"]
    camera    = CAMERAS.get(camera_id)
    if not camera:
        return web.Response(status=404)
    if camera.get("display") in ("webrtc", "wsrtsp", "info", "appliance"):
        return web.Response(status=400, text="Not streamable")

    # ── Focus mode guard ──────────────────────────────────────────────────────
    # When this camera is in enhanced/focus view, do NOT start a competing
    # sub-stream task. The focus snap_loop manages its own lifecycle (incl.
    # the 2s restart delay). Just serve the last buffered frame so the JS
    # poller gets something without triggering a 480p task that would stomp
    # all over the adaptive quality controller.
    if anycam_focus._FOCUSED_CAMERA == camera_id:
        # Update last_access so snap_loop's idle timer doesn't fire during focus.
        # Without this, the loop would die after 30s since the early return skips
        # the normal _snap_last_access update below.
        _snap_last_access[camera_id] = time.monotonic()
        state = _snap_state(camera_id)
        frame = state.get("frame")
        if frame:
            # Build step-label headers so the JS info bar can show the current
            # ladder tier ("Adapted Quality") separately from measured real FPS.
            step_res  = "?"
            step_fps  = "?"
            # X-Snap-Mode tells JS whether frame is from ffmpeg (rtsp) or
            # http_snap_loop — controls are disabled in http mode since profile
            # switching is impossible via HTTP snapshot endpoints.
            snap_mode = "http" if state.get("http_snap_active") else "rtsp"
            # 2.4.0-rc3.3 Bug B fix: X-Stream-Status surfaces the retry phase
            # to JS so the user sees "Connecting…" / "Switching transport…"
            # instead of a frozen frame with no explanation. Streak >= 1 means
            # at least one ffmpeg has died with 0 frames since the last
            # successful frame; frames > 0 in current run means we're past
            # the connect phase. Ordering matters: if we're already in
            # http_snap mode, snap_mode handles the messaging via the
            # existing toast — only emit "connecting" when we're still
            # actively retrying RTSP.
            #
            # 2.4.0-rc3.4 Bug 2 fix: gate on current_run_frames (per-ffmpeg-launch
            # counter) instead of lifetime frame_count. frame_count accumulates
            # across the camera_id's whole lifetime — including prior http_snap
            # sessions that successfully produced frames before RTSP came back —
            # so by the time a focus re-entry was retrying ffmpeg with 0 frames,
            # frame_count was already large and the gate evaluated False, so
            # status fell through to "ok" and the JS toast never showed despite
            # ffmpeg being dead. current_run_frames resets to 0 on each ffmpeg
            # launch so the gate now reflects the actual current run.
            zfs = state.get("zero_frame_streak", 0)
            tff = state.get("transport_flip_fired", False)
            if snap_mode == "http":
                stream_status = "http_fallback"
            elif zfs >= 1 and not state.get("current_run_frames", 0):
                stream_status = "switching_transport" if tff else "connecting"
            else:
                stream_status = "ok"
            ada = _FOCUS_ADAPTIVE.get(camera_id)
            if ada and ada.get("ladder"):
                ladder   = ada["ladder"]
                tier_idx = min(ada.get("tier_idx", 0), len(ladder) - 1)
                prof_idx, t_fps = ladder[tier_idx]
                cam_now  = CAMERAS.get(camera_id, {})
                profiles = cam_now.get("stream_profiles") or []
                prof     = profiles[prof_idx] if prof_idx < len(profiles) else {}
                pw       = prof.get("stream_width")  or cam_now.get("stream_width")  or "?"
                ph       = prof.get("stream_height") or cam_now.get("stream_height") or "?"
                step_res = f"{pw}x{ph}"
                step_fps = "uncapped" if t_fps is None else str(t_fps)
            return web.Response(body=frame, content_type="image/jpeg",
                                headers={"Cache-Control": "no-cache",
                                         "X-Frame-Source": "focus",
                                         "X-Snap-Mode":     snap_mode,
                                         "X-Stream-Status": stream_status,
                                         "X-Frame-Count":   str(state.get("frame_count", 0)),
                                         "X-Focus-Frames":  str(state.get("frame_count", 0)
                                                               - state.get("focus_frame_base", 0)),
                                         "X-Step-Res":      step_res,
                                         "X-Step-FPS":      step_fps})
        return web.Response(status=204)  # no frame yet — JS will retry

    # Card view always uses the main stream_url for thumbnail polling.
    # sub_stream_url is reserved for the adaptive focus ladder (enhanced view).
    # Using sub_stream_url for thumbnails was causing "Invalid data found" errors
    # on cameras where the sub-stream has different codec/transport requirements
    # than the main stream (e.g. the Microseven: main=HEVC/RTSP, sub=MJPEG/HTTP).
    # The thumbnail loop already runs at fps=10,scale=640:-2 so CPU/bandwidth
    # is low regardless of which stream is used.
    url = build_authenticated_url(camera)
    snap_camera = camera
    if not url:
        return web.Response(status=503, text="No stream URL")

    # Record access time so snap_loop knows we're still watching
    _snap_last_access[camera_id] = time.monotonic()

    state = _snap_state(camera_id)

    # Start background snap process if not already running
    if state.get("task") is None or state["task"].done():
        log.info(f"SNAP [{camera_id}]: starting background process "
                 f"(codec={snap_camera.get('stream_codec') or '?'}, "
                 f"res={snap_camera.get('stream_width') or '?'}px)")
        state["task"] = asyncio.create_task(snap_loop(camera_id, url, snap_camera))

    # Wait up to 5s for the very first frame (subsequent calls return instantly)
    if state["frame"] is None:
        log.info(f"SNAP [{camera_id}]: waiting for first frame...")
        loop     = asyncio.get_event_loop()
        deadline = loop.time() + 5.0
        while state["frame"] is None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                log.info(f"SNAP [{camera_id}]: no frame available after 5s")
                return web.Response(status=503, text="No frame yet — starting up")
            await asyncio.sleep(0.05)

    frame = state["frame"]
    age   = time.monotonic() - state["frame_time"]
    # 3.0.1 (B20): a card showed events minutes after they happened. A
    # picture older than SNAP_MAX_AGE_S is not served; the card says
    # "Loading feed" until a current one arrives.
    if age > SNAP_MAX_AGE_S:
        log.debug(f"SNAP [{camera_id}]: picture {age:.0f} s old — not shown")
        return web.Response(status=503, text="Picture too old")
    if age > 3.0:
        log.info(f"SNAP [{camera_id}]: serving stale frame (age={age:.1f}s)")

    return web.Response(
        body=frame,
        content_type="image/jpeg",
        headers={
            "Cache-Control":    "no-cache, no-store, must-revalidate",
            "Pragma":           "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def api_logs(request: web.Request) -> web.Response:
    """GET /api/logs?since=T — recent warning/error entries from the in-memory buffer.
    Used by the JS status dot to determine system health color (green/amber/red)."""
    since   = float(request.rel_url.query.get("since", 0))
    entries = [e for e in _LOG_BUFFER if e["t"] > since]
    recent  = _LOG_BUFFER[-50:]
    has_err = any(e["level"] == "error"   for e in recent)
    has_wrn = any(e["level"] == "warning" for e in recent)
    status  = "error" if has_err else ("warning" if has_wrn else "ok")
    return web.json_response({"status": status, "entries": entries[-100:]})


async def handle_snap_status(request: web.Request) -> web.Response:
    """
    GET /snap/status — JSON health check for all active snapshot processes.
    Useful for debugging without reading logs.
    Example response:
      {"10.0.0.33_onvif": {"running": true, "pid": 1247, "frame_count": 350,
                           "frame_bytes": 6234, "frame_age_s": 0.08,
                           "last_poll_s": 0.1, "restarts": 0}}
    """
    now    = time.monotonic()
    result = {}
    for cid, state in _SNAP.items():
        proc   = state.get("proc")
        result[cid] = {
            "running":      proc is not None and proc.returncode is None,
            "pid":          proc.pid if proc else None,
            "frame_count":  state.get("frame_count", 0),
            "frame_bytes":  len(state["frame"]) if state.get("frame") else 0,
            "frame_age_s":  round(now - state["frame_time"], 2)
                            if state.get("frame_time") else None,
            "last_poll_s":  round(now - _snap_last_access[cid], 1)
                            if cid in _snap_last_access else None,
            "restarts":     state.get("restart_count", 0),
        }
    return web.json_response(result)


async def _drain_stderr(proc: object, label: str) -> None:
    """
    Drain ffmpeg stderr to prevent OS pipe buffer deadlock.
    Logs collected lines on exit and auto-detects:
      - v4l2m2m / vaapi hardware decoder unavailability
      - Hikvision H.265+ (Multi-layer HEVC) incompatibility
    """
    lines: list[str] = []
    try:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").rstrip()
            if decoded and "deprecated pixel format" not in decoded:
                lines.append(decoded)
                # 3.7.0 (B11): caught while ffmpeg runs, not only at its exit
                if label.startswith("SNAP:") and _hw_fallback_line(decoded):
                    _HW_FALLBACK.setdefault(label[5:].strip(), _strip_creds(decoded)[:200])
    except asyncio.CancelledError:
        try:
            remaining = await proc.stderr.read(8192)
            for ln in remaining.decode("utf-8", errors="replace").splitlines():
                if ln.strip() and "deprecated pixel format" not in ln:
                    lines.append(ln.strip())
        except Exception:
            pass
    except Exception:
        pass
    if not lines:
        return
    joined = " | ".join(lines)[:600]
    # Strip credentials from RTSP URLs before logging — ffmpeg includes the
    # full authenticated URL in its error messages (SigRev-1 item 4).
    joined = _strip_creds(joined)
    log.warning(f"Stream {label} ffmpeg stderr: {joined}")
    # 2.6.0-rc2.3: auto-disable list updated for the new candidate set.
    # hevc_drm is the rpi 4/5 HEVC path (via -hwaccel drm); when ffmpeg
    # can't init it, the stderr reads "Could not find a valid device"
    # the same way v4l2m2m and vaapi failures do, so the same matching
    # logic applies. v4l2request entries dropped — those decoder names
    # never existed.
    for hw in ("hevc_drm",
               "hevc_v4l2m2m", "h264_v4l2m2m",
               "hevc_vaapi",   "h264_vaapi"):
        if hw in joined and "Could not find a valid device" in joined:
            _HW_UNAVAILABLE.add(hw)
            log.info(f"Marked {hw} as unavailable on this system")
    if "Multi-layer HEVC" in joined:
        cam_id = label.replace("SNAP:", "").strip()
        if cam_id in CAMERAS:
            cam = CAMERAS[cam_id]
            # 2.3.1: If a prior clean run (>=50 frames) confirmed this is just
            # Hikvision's noisy stderr (not actual broken H.265+), skip setting
            # the badge. We still apply the -fflags +discardcorrupt workaround
            # below as defensive cover (it's harmless on a clean stream and
            # helpful if the stream ever does have real corruption).
            noise_confirmed = cam.get("hevc_plus_noise_confirmed", False)
            if not noise_confirmed:
                first_time = not cam.get("hevc_plus_warning")
                if first_time:
                    cam["hevc_plus_warning"] = True
                    log.warning(f"Camera {cam_id}: H.265+ (Hikvision proprietary) detected — "
                                f"fix: camera UI → Video → Encoding → change H.265+ to H.265")
            # rc1 (Item B1): enable -fflags +discardcorrupt for future ffmpeg
            # launches on this camera. ffmpeg's discardcorrupt flag drops frames
            # that fail decoding instead of bailing the entire process — most
            # Hikvision H.265+ streams remain partially decodable, so we get
            # video instead of nothing. Persists across restarts via
            # save_cameras() until 10 consecutive clean (≥50 frame) runs clear
            # the flag.
            if not cam.get("needs_fflags_discardcorrupt"):
                cam["needs_fflags_discardcorrupt"] = True
                cam["clean_runs_since_fflags"]    = 0
                log.info(f"Camera {cam_id}: H.265+ Multi-layer HEVC detected — "
                         f"enabling -fflags +discardcorrupt for future ffmpeg launches")
                try:
                    save_cameras()
                except Exception as ex:
                    log.debug(f"Camera {cam_id}: save_cameras after fflags set failed: {ex}")
            else:
                # Flag was already on but we just saw another error — reset
                # the clean-run counter so we don't prematurely clear the flag.
                cam["clean_runs_since_fflags"] = 0


async def _try_hevc_plus_fallback(camera_id: str, camera: dict,
                                   url: str) -> str | None:
    """
    Hikvision H.265+ cameras use a proprietary multi-layer codec that standard
    ffmpeg cannot decode. When detected, try a sub-stream fallback.
    Returns a working alternate URL, or None.
    """
    log.info(f"snap_loop [{camera_id}]: attempting H.265+ fallback")
    loop = asyncio.get_event_loop()

    def _try_probe(test_url: str) -> bool:
        u, p = "", ""
        try:
            creds = camera.get("credentials")
            if creds:
                u, p = decrypt_creds(creds)
        except Exception:
            pass
        return probe_rtsp(test_url, u, p, timeout=6, label=f"{camera_id}/h265plus")

    # 1. Explicit sub-stream URL stored on the camera dict
    sub_url = camera.get("sub_stream_url")
    if sub_url:
        auth_sub = build_authenticated_url(camera, "sub_stream_url")
        if auth_sub and await loop.run_in_executor(_THREAD_POOL, _try_probe, auth_sub):
            log.info(f"snap_loop [{camera_id}]: H.265+ → sub_stream_url")
            return auth_sub

    # 2. Hikvision path-convention sub-stream mapping
    from urllib.parse import urlparse as _up
    parsed = _up(url)
    sub_paths = {
        "/Streaming/Channels/101":       "/Streaming/Channels/102",
        "/Streaming/Channels/1":         "/Streaming/Channels/2",
        "/ISAPI/Streaming/channels/101": "/ISAPI/Streaming/channels/102",
        "/h264/ch1/main/av_stream":      "/h264/ch1/sub/av_stream",
        "/h265/ch1/main/av_stream":      "/h265/ch1/sub/av_stream",
    }
    sub_path = sub_paths.get(parsed.path)
    if sub_path:
        auth_main = build_authenticated_url(camera) or url
        alt_auth = auth_main.replace(parsed.path, sub_path, 1)
        if await loop.run_in_executor(_THREAD_POOL, _try_probe, alt_auth):
            log.info(f"snap_loop [{camera_id}]: H.265+ → path fallback {_strip_creds(alt_auth)}")
            return alt_auth

    log.warning(f"snap_loop [{camera_id}]: H.265+ fallback exhausted")
    return None
