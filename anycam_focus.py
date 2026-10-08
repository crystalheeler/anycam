"""The Enhanced View engine: one camera in focus, through go2rtc or the snapshot path.

Moved out of camera_discovery.py in 3.0.0-rc1.5 (build plan E1); the function
bodies are unchanged.

This file cannot import camera_discovery.py (see anycam_host.py). The names in
NEEDS are set on this module at start-up; H reads a camera_discovery.py
value at the moment of use.
"""
import asyncio
import logging
import time
from aiohttp import web

from anycam_host import H
from anycam_go2rtc import (
    _go2rtc_profile_source, _go2rtc_register, _go2rtc_shared_name,
)
import anycam_go2rtc

log = logging.getLogger("anycam")

# Taken from camera_discovery.py at start-up (anycam_host.bind).
NEEDS = (
    'CAMERAS', '_FOCUS_ADAPTIVE', '_MOTION', '_SNAP',
    '_snap_last_access', '_snap_state', 'build_authenticated_url',
    'save_cameras', 'snap_loop',
)

# Currently focused camera for full-screen enhanced view.
# When set, all other snap_loops throttle to 1fps; focused loop runs native res.
_FOCUSED_CAMERA: str | None = None
# 2.6.3 — which engine serves the current focus session: "legacy" (the
# native-res ffmpeg snap_loop) or "go2rtc" (passthrough, no snap_loop for
# the focused camera). None when no camera is focused. handle_focus_clear
# reads it, because the two engines leave different state to tear down.
_FOCUS_ENGINE: str | None = None


async def api_go2rtc_focus(request: web.Request) -> web.Response:
    """GET /api/go2rtc/focus/{camera_id}?profile=N — prepare live view.

    Registers the profile's stream with go2rtc and returns its name, or
    {"ok": false, "reason": ...} so the browser takes the classic path.
    Never dials the camera: go2rtc connects only when the player's
    WebSocket arrives through handle_go2rtc_ws.
    """
    camera_id = request.match_info["camera_id"]
    if not anycam_go2rtc._GO2RTC_READY:
        return web.json_response({"ok": False, "reason": "go2rtc is not running"})
    camera = CAMERAS.get(camera_id)
    if not camera:
        return web.json_response({"ok": False, "reason": "camera not found"},
                                 status=404)
    try:
        prof_idx = int(request.query.get("profile", "0"))
    except ValueError:
        return web.json_response({"ok": False, "reason": "bad profile index"},
                                 status=400)
    if anycam_go2rtc._rtsp_stuck(camera_id):    # 3.7.4 (B6, B32): no RTSP to it
        return web.json_response({"ok": False, "reason": anycam_go2rtc.RTSP_STUCK_REASON})
    src, codec, reason = _go2rtc_profile_source(camera, prof_idx)
    if not src:
        return web.json_response({"ok": False, "reason": reason, "codec": codec})
    name = _go2rtc_shared_name(camera_id, src)     # 3.3.0 (C4): shared with the other users
    if not await _go2rtc_register(name, src, camera_id):
        return web.json_response({"ok": False,
                                  "reason": "go2rtc rejected the stream"})
    return web.json_response({"ok": True, "stream": name,
                              "profile": prof_idx, "codec": codec})


async def _focus_set_go2rtc(camera_id: str) -> web.Response:
    """Enter Enhanced View on the go2rtc engine.

    Records focus, so the card's polls for this camera stop spawning work and
    other cameras throttle as usual. Starts no native-res snap_loop: go2rtc
    serves the video, decoded by the viewing device.

    The thumbnail snap_loop is the one thing that must be decided here,
    because motion detection runs inside it (it compares consecutive JPEG
    frames). Since 2.6.5 an armed camera's loop never idles out, and
    _motion_keeper restarts it:
      * Motion armed: keep it running, and start it if it is not. Motion
        recording keeps working, at the cost of a second RTSP session and
        the thumbnail decode the grid view runs anyway.
      * Motion off: stop it, so go2rtc holds the only RTSP session and the
        Pi decodes nothing for this camera.
    """
    global _FOCUSED_CAMERA, _FOCUS_ENGINE
    _FOCUSED_CAMERA = camera_id
    _FOCUS_ENGINE = "go2rtc"
    camera = CAMERAS[camera_id]
    state = _SNAP.get(camera_id)
    ms = _MOTION.get(camera_id)
    # 2.6.6: only while the snapshot path is this camera's detector; with
    # live detection running, motion does not need the thumbnail loop.
    if ms and ms.get("enabled") and not ms.get("detector_live"):
        running = bool(state and state.get("task") and not state["task"].done())
        if not running:
            url = build_authenticated_url(camera)
            if url:
                _snap_last_access[camera_id] = time.monotonic()
                _snap_state(camera_id)["task"] = asyncio.create_task(
                    snap_loop(camera_id, url, camera))
        log.info(f"Focus: entering enhanced view for {camera_id} (engine: "
                 f"go2rtc) — motion detection is armed, so the thumbnail "
                 f"loop keeps running beside go2rtc")
    else:
        log.info(f"Focus: entering enhanced view for {camera_id} "
                 f"(engine: go2rtc)")
        task = state.get("task") if state else None
        if task and not task.done():
            proc = state.get("proc")
            if proc is not None and proc.returncode is None:
                # The documented clean exit: snap_loop's EOF branch sees the
                # flag, pops it, and returns without restarting.
                state["focus_leave_kill"] = True
                try:
                    proc.kill()
                    log.info(f"Focus: stopped thumbnail ffmpeg for "
                             f"{camera_id} — go2rtc holds the stream")
                except ProcessLookupError:
                    state.pop("focus_leave_kill", None)
            else:
                # No live ffmpeg to consume the flag (between restarts, or on
                # the HTTP snapshot path), so cancel and set no flag. An
                # unconsumed flag makes the NEXT thumbnail loop skip its first
                # restart: the 2.4.0-rc3.3 Bug A shape.
                task.cancel()
    return web.json_response({"status": "ok", "focused": camera_id,
                              "engine": "go2rtc"})


async def handle_focus_set(request: web.Request) -> web.Response:
    """POST /snap/focus/{camera_id} — enter full-screen focus mode.

    2.6.3: `?engine=go2rtc` enters focus on the go2rtc engine instead (see
    _focus_set_go2rtc). Without that parameter this path is unchanged from
    2.6.2 apart from recording its engine.
    """
    global _FOCUSED_CAMERA, _FOCUS_ENGINE
    camera_id = request.match_info["camera_id"]
    if camera_id not in CAMERAS:
        return web.json_response({"error": "Camera not found"}, status=404)
    if request.query.get("engine") == "go2rtc":
        return await _focus_set_go2rtc(camera_id)
    _FOCUSED_CAMERA = camera_id
    _FOCUS_ENGINE = "legacy"
    log.info(f"Focus: entering enhanced view for {camera_id}")
    # 2.4.0-rc3.3 Bug A fix: clear any stale focus_leave_kill flag from a
    # previous focus session. The flag is set by handle_focus_clear and
    # consumed by snap_loop's main RTSP path at line ~6817, but if the
    # previous session ended via http_snap fallback (where snap_loop is
    # awaiting http_snap_loop, not in its main read loop), the flag never
    # gets consumed and lingers in _SNAP[camera_id]. Then on the NEXT
    # focus entry, the first ffmpeg failure (e.g. on the Microseven's
    # initial TCP attempt) hits the EOF branch at line 6713, sees the
    # still-True flag, logs "killed by focus-leave" (wrong — the user
    # just entered, didn't leave), and the restart-skip at line 6817
    # returns from snap_loop entirely — leaving a dead loop while the
    # user's dropdown clicks go nowhere. Clearing the flag here on
    # every focus entry makes the next snap_loop start from a known
    # state regardless of how the previous session ended.
    state = _SNAP.get(camera_id)
    if state and state.pop("focus_leave_kill", None):
        log.debug(f"Focus: cleared stale focus_leave_kill flag for {camera_id} "
                  f"(previous session ended without consuming it — likely "
                  f"http_snap fallback path)")
    # 2.6.0-rc2.5 — fix #2: reset per-session HW failure counters on
    # focus-enter. rc2.4's `state["hw_session_fails"]` and
    # `state["hw_session_skip"]` are keyed by camera_id and persist
    # across snap_loop invocations for the same camera. Without this
    # reset, the field-test log on the Lorex DVR ch4 showed failure 1
    # at 19:44:24 and failure 2 at 19:44:43 in one focus session, then
    # failure 3 at 19:50:10 in a DIFFERENT focus session 5 minutes
    # later → log printed "hevc_drm failed 3 times this session —
    # skipping" but they were spread across two sessions. The user
    # explicitly re-entered Enhanced View expecting a fresh shot at
    # HW; rc2.4 was giving them a stale counter. Resetting here makes
    # "this session" actually mean what the log says: one focus entry.
    if state:
        # 3.7.5-rc1.0 (B51): the keyframes-only and sub-stream decisions are per session
        state.pop("classic_keyframes", None)
        state.pop("smooth_failed", None)
        if state.pop("hw_session_fails", None) or state.pop("hw_session_skip", None):
            log.debug(f"Focus: cleared HW session counters for {camera_id} "
                      f"— fresh shot at hardware decode")
    # Reset restarts_since_lock so a stale count from the previous focus session
    # doesn't immediately trigger a step-down on re-entry.
    ada = _FOCUS_ADAPTIVE.get(camera_id)
    if ada:
        ada["restarts_since_lock"] = 0
        ada["run_start"]           = None
    # Always cancel any existing task (likely a low-fps sub-stream thumbnail task)
    # and start a fresh native_res=True task on the main high-res stream_url.
    # Without this, a running thumbnail task would block the focus task from starting.
    camera = CAMERAS[camera_id]
    url    = build_authenticated_url(camera, url_key="stream_url")
    if url:
        state = _snap_state(camera_id)
        existing = state.get("task")
        if existing and not existing.done():
            existing.cancel()
            log.info(f"Focus: cancelled existing snap_loop for {camera_id} "
                     f"— starting native-res main-stream task")
        # 2.6.5: count frames from here, so the page can tell this
        # session's first frame from the card thumbnail still in the
        # buffer (X-Focus-Frames). The cancelled task cannot add to
        # frame_count: it stops at its next await.
        state["focus_frame_base"] = state.get("frame_count", 0)
        # 3.7.2 (B36): a live card asks for no pictures, so the last request
        # can be minutes old; the new loop's idle check then stopped it at
        # once ("idle 664s — stopping") and the view sat on "Loading".
        _snap_last_access[camera_id] = time.monotonic()
        state["task"] = asyncio.create_task(
            snap_loop(camera_id, url, camera, native_res=True))
    return web.json_response({"status": "ok", "focused": camera_id})


async def api_quality_switch(request: web.Request) -> web.Response:
    """POST /api/cameras/{camera_id}/quality_switch  body: {"smooth": bool} (3.7.5-rc1.0, B51).

    Smooth: the classic view plays the camera's sub-stream, smaller but at
    its full rate. Off: full size, keyframes only when the Pi falls behind.
    Saved per camera. A running classic view restarts in the new mode.
    """
    camera_id = request.match_info["camera_id"]
    camera = CAMERAS.get(camera_id)
    if not camera:
        return web.json_response({"error": "Camera not found"}, status=404)
    try:
        smooth = bool((await request.json()).get("smooth"))
    except (ValueError, AttributeError):
        return web.json_response({"error": "JSON body required"}, status=400)
    if smooth and not anycam_go2rtc._classic_sub_stream(camera):
        return web.json_response({"error": "this camera has no sub-stream"}, status=400)
    camera["classic_smooth"] = smooth
    save_cameras()
    log.info(f"Focus: Quality Switch {'on (sub-stream)' if smooth else 'off (full size)'} for {camera_id}")
    if _FOCUSED_CAMERA == camera_id and _FOCUS_ENGINE == "legacy":
        state = _snap_state(camera_id)
        state.pop("smooth_failed", None)
        state.pop("classic_keyframes", None)
        task = state.get("task")
        if task and not task.done():
            task.cancel()
        url = build_authenticated_url(camera, url_key="stream_url")
        if url:
            state["focus_frame_base"] = state.get("frame_count", 0)
            _snap_last_access[camera_id] = time.monotonic()
            state["task"] = asyncio.create_task(snap_loop(camera_id, url, camera, native_res=True))
    return web.json_response({"ok": True, "smooth": smooth})


async def handle_focus_clear(request: web.Request) -> web.Response:
    """DELETE /snap/focus — exit full-screen focus mode."""
    global _FOCUSED_CAMERA, _FOCUS_ENGINE
    prev = _FOCUSED_CAMERA
    engine = _FOCUS_ENGINE
    _FOCUS_ENGINE = None
    if engine == "go2rtc":
        log.info(f"Focus: leaving enhanced view (was: {prev}, engine: go2rtc)")
        _FOCUSED_CAMERA = None
        # Nothing to kill: this engine started no native-res loop, and the
        # browser has already closed its WebSocket. Drop any focus_leave_kill
        # flag the stopped thumbnail loop has not consumed yet, so the next
        # thumbnail loop restarts normally instead of hitting the
        # 2.4.0-rc3.3 Bug A shape. The legacy branch below must not run
        # here: it SETS that flag, and nothing would be left to consume it.
        state = _SNAP.get(prev) if prev else None
        if state:
            state.pop("focus_leave_kill", None)
        return web.json_response({"status": "ok"})
    log.info(f"Focus: leaving enhanced view (was: {prev})")
    _FOCUSED_CAMERA = None
    # Cancel the native-res snap_loop task so the next thumbnail poll
    # starts a fresh normal-quality task (CFG limits restored immediately).
    if prev:
        state = _SNAP.get(prev)
        if state:
            # 2.3.1: directly kill ffmpeg in addition to cancelling the task.
            # task.cancel() alone is cooperative — the task only sees the
            # cancellation at the next await checkpoint, but if ffmpeg keeps
            # producing chunks the read() keeps returning data successfully
            # and the cancellation never gets delivered cleanly. Result was a
            # 12+ second lag between focus-leave and snap_loop actually
            # exiting (observed on Hikvision: focus-leave at 14:58:14,
            # ffmpeg EOF only logged at 14:58:26). Killing the process
            # directly forces the next read() to return empty immediately,
            # the snap_loop drops into the EOF branch and unwinds in <1s.
            # The focus_leave_kill flag tells snap_loop's restart logic that
            # this exit was OUR doing — skip the restart; the next
            # handle_snapshot poll will spawn a fresh thumbnail task.
            state["focus_leave_kill"] = True
            proc = state.get("proc")
            if proc is not None:
                try:
                    proc.kill()
                    log.info(f"Focus: killed ffmpeg for {prev} — "
                             f"snap_loop will exit and thumbnail polling will restart")
                except Exception as ex:
                    log.debug(f"Focus: ffmpeg kill for {prev} failed "
                              f"(probably already dead): {ex}")
            task = state.get("task")
            if task and not task.done():
                task.cancel()
                log.info(f"Focus: cancelled native-res snap_loop for {prev} — "
                         f"thumbnail polling will restart at normal quality")
        # Reset run_start so the next focus session measures cleanly,
        # but preserve tier_idx and locked so it remembers the best stable setting.
        ada = _FOCUS_ADAPTIVE.get(prev)
        if ada:
            ada["run_start"] = None
    return web.json_response({"status": "ok"})
