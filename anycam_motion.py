"""Motion detection, motion recording and night boost.

Moved out of camera_discovery.py in 3.0.0-rc1.1 (build plan E1, stage 2);
the function bodies are unchanged.

This file cannot import camera_discovery.py: that file imports this one, and
it runs as the program's entry point. The names listed in NEEDS are set on
this module by anycam_host.bind() when camera_discovery.py has loaded. H reads
a camera_discovery.py value at the moment of use, for the values that file
replaces while it runs.
"""
import aiohttp
import asyncio
import collections
import datetime
import io
import json
import logging
import os
import time
from PIL import Image
from aiohttp import web
from pathlib import Path

from anycam_host import H
import anycam_focus
from anycam_go2rtc import _go2rtc_profiles      # 3.0.0-rc1.2: it moved there
from anycam_go2rtc import _go2rtc_relay, _go2rtc_relay_result     # 3.3.0 (C4)
import anycam_zones                                                 # 3.4.0 (C17)
import anycam_upload                                                # 3.6.0 (C14)
import anycam_backup                                                # 3.8.3 (C27)

log = logging.getLogger("anycam")

# Taken from camera_discovery.py at start-up (anycam_host.bind).
NEEDS = (
    'CAMERAS', 'CARD_MAX_WIDTH', 'CFG_MOTION_CLIP_S', 'CFG_MOTION_COOL',
    'CFG_MOTION_CALIBRATION', 'CFG_MOTION_GLOBAL', 'CFG_MOTION_LEVEL', 'CFG_MOTION_PAD',
    'CFG_RECORDINGS', 'CURRENT_VERSION',
    'DATA_DIR', 'MEDIA_DIR', 'MOTION_FILE', '_brand_throttle_seconds',
    '_dahua_sub_stream', '_drain_stderr', '_snap_last_access',
    '_snap_state', '_stop_proc', '_throttle_wait_if_needed', 'build_authenticated_url',
    'snap_loop', '_card_key',
)
# Per-camera motion state
_MOTION: dict = {}   # camera_id → {enabled, recording, last_motion, proc, clip_path}


def _motion_state(camera_id: str) -> dict:
    if camera_id not in _MOTION:
        _MOTION[camera_id] = {
            "enabled":      False,
            "recording":    False,
            "last_motion":  0.0,
            "proc":         None,
            "clip_path":    None,
            "refs":         collections.deque(),   # 2.6.6: (time, picture) to compare with
        }
    return _MOTION[camera_id]
# ── 2.6.5: motion detection that does not depend on a viewer ────────────
# Before 2.6.5 motion ran only on the ffmpeg thumbnail path, and every loop
# stopped 30 s after the last page poll. The Lorex channels poll HTTP
# snapshots, so they detected motion only while open in the classic
# Enhanced View; once 2.6.4 made live view the default, never. A recording
# was stopped only when a frame arrived, so leaving the view left it
# running (build plan B1). Now: both loops call _motion_on_frame, an armed
# camera's loop does not idle out, and _motion_keeper restarts dead loops
# and ends recordings on time whether or not frames arrive.
MOTION_KEEPER_S = 10


def _motion_armed(camera_id: str) -> bool:
    ms = _MOTION.get(camera_id)
    return bool(ms and ms["enabled"])


def _motion_on_frame(camera_id: str, frame: bytes) -> None:
    """A snapshot or thumbnail JPEG for motion detection (fallback path).

    2.6.6: an armed camera normally watches its live stream instead
    (_motion_detector); this path runs only while that is not delivering,
    or for cameras with no RTSP stream.
    """
    ms = _MOTION.get(camera_id)
    if not (ms and ms["enabled"]) or ms.get("detector_live"):
        return
    now_m = time.monotonic()
    # Decode at most once per MOTION_COMPARE_S; thumbnail ffmpeg loops can
    # deliver 10 frames a second.
    if now_m - ms.get("snap_cmp_t", 0.0) < MOTION_COMPARE_S:
        _motion_tick(camera_id, ms, now_m)
        return
    ms["snap_cmp_t"] = now_m
    chroma = _motion_jpeg_chroma(frame)
    if chroma is not None:
        _motion_night_observe(camera_id, chroma, now_m)
    thumb = _motion_thumb(frame, _motion_grid(camera_id))
    if thumb is None:
        _motion_tick(camera_id, ms, now_m)
        return
    _motion_feed(camera_id, thumb, now_m)


def _motion_feed(camera_id: str, thumb: tuple[bytes, float, float], now_m: float,
                 stream_t: float | None = None) -> None:
    """Compare one picture with the one MOTION_REF_S before; act on the result.

    Comparing with a picture about a second old, rather than the previous
    frame, keeps a slow walker visible at 4 frames a second.

    2.6.7: stream_t is the picture's place in the live stream (picture
    count / MOTION_DETECT_FPS), so the reference is exactly 4 pictures back
    however the pictures arrive. With arrival times, a burst of pictures all
    compared with the same reference: an insect in that reference showed
    as two or three changes in a row (log, 01:48:17: 1.1%, 1.1%, 1.1%).
    On the live stream a recording then needs a second changed picture
    within MOTION_CONFIRM_S, or one picture with MOTION_INSTANT_PCT.
    """
    ms = _MOTION.get(camera_id)
    if not (ms and ms["enabled"]):
        return
    live = stream_t is not None
    t = stream_t if live else now_m
    refs = ms.setdefault("refs", collections.deque())
    if refs and len(refs[-1][1][0]) != len(thumb[0]):
        _motion_reset_prev(camera_id)      # 3.4.0: the grid changed with the zones
    while len(refs) > 1 and t - refs[1][0] >= MOTION_REF_S - 1e-6:
        refs.popleft()
    ref = refs[0][1] if refs and t - refs[0][0] >= MOTION_REF_S - 1e-6 else None
    refs.append((t, thumb))
    # 3.0.1 (B20): on the snapshot path (about one picture a second) an
    # insect near the lens changes one picture and is gone by the next, and
    # recorded there. A picture must also differ from the picture before
    # the reference.
    hist = ms.setdefault("snap_hist", collections.deque(maxlen=2))
    older = hist[0] if (not live and len(hist) == 2) else None
    if not live:
        hist.append(thumb)
    moved = (ref is not None and _motion_decide(camera_id, ms, ref, thumb, t, live)
             and (live or ms["recording"] or _motion_second_look(camera_id, older, thumb)))
    # 3.4.0 (C17, answer 11): slow movement inside a zone
    if moved:
        ms.pop("cal_slow", None)          # 3.9.0 (B74): the fast checks started it
    elif anycam_zones.has_zones(camera_id):
        moved = _motion_slow_look(camera_id, ms, thumb, t)
        if moved and ms["recording"]:
            _motion_note_rec_zones(ms, [ms.get("judge_zone")])      # 3.9.0 (C31)
    if moved:
        ms["last_motion"] = now_m
        if not ms["recording"]:
            if live:
                if ms.get("hold") is None:
                    _motion_hold_open(ms, t, now_m)      # 3.9.0-rc2.0 (B85)
            else:
                _motion_begin(camera_id)
    if live and ms.get("hold") is not None:
        _motion_hold_check(camera_id, ms, t, now_m, moved)
    if not moved:
        _motion_tick(camera_id, ms, now_m)


def _motion_begin(camera_id: str) -> None:
    """Start a recording (the ffmpeg writer runs as its own task)."""
    camera = CAMERAS.get(camera_id)
    cam_url = build_authenticated_url(camera) if camera else None
    if cam_url:
        asyncio.create_task(_start_recording(camera_id, camera, cam_url))


# ── 3.9.0-rc2.0 (B85): look a little longer before a recording starts ────────
# CrystalHeeler's first field night of 3.9.0-rc1.0 (24-25 Oct): 4 headlight
# files started on the faint first edge of the light, a moment before the light
# pause saw it; 2 started on the first, half-switched picture of the dawn
# switch from infrared to colour, which stayed under the 75% light rule; 9 were
# the back yard's motion-sensitive light switching on or off. Each showed what
# it was within the next second. A live start now waits MOTION_HOLD_S; the
# pre-roll grows by the wait, so the file still begins before the movement.
MOTION_HOLD_S = 1.25            # a live start waits this long, then records ...
COLOUR_SWITCH_STEP = 1.0        # ... unless the colour moved this much (infrared <-> colour:
                                #     1.9-2.2 in a second; other recordings at most 0.6),
LIGHT_STEP_SHARE = 0.90         # ... or a change this one-sided (brighter or darker) ...
LIGHT_STEP_QUIET = 2            # ... stopped at once: this many still comparisons (a lamp)


def _motion_hold_open(ms: dict, t: float, now_m: float) -> None:
    """A recording is due; note what started it and wait MOTION_HOLD_S."""
    colours = list(ms.get("colour_hist") or ())
    before = colours[-8:-2] or colours[-1:]
    pct, share, mean, fill = ms.get("cmp", (0.0, 0.5, 0.0, 1.0))
    one_sided = (abs(mean) >= LIGHT_SWEEP_DELTA
                 and (share >= LIGHT_STEP_SHARE or share <= 1.0 - LIGHT_STEP_SHARE))
    ms["hold"] = {"t": t, "m": now_m, "zone": ms.get("judge_zone"), "slow": ms.get("cal_slow"),
                  "colour": (sum(before) / len(before)) if before else None,
                  "one_sided": one_sided, "still": 0}


def _motion_hold_cancel(camera_id: str, ms: dict, why: str) -> None:
    ms["hold"] = None
    ms.pop("cal_slow", None)
    log.info(f"Motion [{camera_id}]: not recorded — {why}")
    _cal_reason(ms, why)


def _motion_hold_check(camera_id: str, ms: dict, t: float, now_m: float, moved: bool) -> None:
    """Each live picture during the wait: cancel the start, or make it."""
    hold = ms["hold"]
    if ms["recording"]:
        ms["hold"] = None
        return
    if t <= hold["t"]:
        return
    if ms.get("lp"):
        _motion_hold_cancel(camera_id, ms, "a passing light followed (headlights)")
        return
    if ms.get("light_t", float("-inf")) >= hold["t"]:
        _motion_hold_cancel(camera_id, ms, "a light change followed")
        return
    colours = ms.get("colour_hist") or ()
    if hold["colour"] is not None and colours and _motion_colour_switch(hold["colour"], colours[-1]):
        _motion_hold_cancel(camera_id, ms, "the camera switched between infrared and colour")
        return
    hold["still"] = 0 if moved else hold["still"] + 1
    if t - hold["t"] < MOTION_HOLD_S - 1e-6:
        return
    if hold["one_sided"] and hold["still"] >= LIGHT_STEP_QUIET:
        _motion_hold_cancel(camera_id, ms, "a light switched on or off (a one-sided change "
                            "that stopped at once)")
        return
    ms["hold"] = None
    ms["judge_zone"] = hold["zone"]
    if hold["slow"] is not None:
        ms["cal_slow"] = hold["slow"]
    ms["held_s"] = now_m - hold["m"]
    _motion_begin(camera_id)


def _motion_colour_switch(before: float, after: float) -> bool:
    """A jump of COLOUR_SWITCH_STEP with one side black-and-white: infrared <-> colour.

    By day both sides are colour, so a person in a bright coat close to the
    camera does not count.
    """
    return abs(after - before) >= COLOUR_SWITCH_STEP and min(before, after) <= NIGHT_CHROMA_MAX


def _motion_colour(camera_id: str, chroma: float, now_m: float, frames: int) -> None:
    """3.9.0-rc2.0 (B85): each live picture's colour; night mode reads it once a second.

    A jump of COLOUR_SWITCH_STEP is the camera switching between infrared and
    colour: a light change, even when it stays under the 75% rule (the back
    yard and the front yard at dawn on 25 Oct). The slow check then drops its
    older pictures, which are from before the switch.
    """
    ms = _MOTION.get(camera_id)
    if ms is not None:
        hist = ms.setdefault("colour_hist", collections.deque(maxlen=12))
        before = list(hist)[-6:-1]
        hist.append(chroma)
        if before and _motion_colour_switch(sum(before) / len(before), chroma):
            t = frames / MOTION_DETECT_FPS
            if t - ms.get("colour_switch_t", float("-inf")) > MOTION_LIGHT_HOLD_S:
                log.info(f"Motion [{camera_id}]: the camera switched between infrared and colour "
                         f"(colour {sum(before) / len(before):.1f} → {chroma:.1f}) — treated as a "
                         f"light change, not recorded")
            ms["colour_switch_t"] = ms["light_t"] = t
            ms["light_until"] = max(ms.get("light_until", float("-inf")), t + MOTION_LIGHT_HOLD_S)
            ms["hits"] = []
            _motion_slow_forget(ms)
    if frames % MOTION_DETECT_FPS == 1:
        _motion_night_observe(camera_id, chroma, now_m)


def _motion_decide(camera_id: str, ms: dict, ref: tuple, thumb: tuple,
                   t: float, live: bool) -> bool:
    """Judge one comparison; apply the light hold and, live, the confirmation."""
    verdict, pct = _motion_judge(camera_id, ref, thumb)
    # ffmpeg repeats a picture when the stream stalls; a repeat is not news.
    repeat = live and (thumb[0] == ms.get("last_cur") or ref[0] == ms.get("last_ref"))
    ms["last_cur"], ms["last_ref"] = thumb[0], ref[0]
    hits = ms.setdefault("hits", [])
    if hits and t - hits[-1][0] > MOTION_CONFIRM_S + 1e-6:
        if len(hits) == 1:
            # 1 s on, that picture is the reference: the same change again.
            ms["echo_of"] = hits[0][2]
            ms["peak_single"] = ms.get("peak_single", 0) + 1
            log.info(f"Motion [{camera_id}]: {hits[0][1]:.1f}% changed in one picture "
                     f"only — not recorded (most often an insect near the lens)")
            _cal_reason(ms, "one picture only (often an insect near the lens)")
            # 3.9.0 (B78): nor may the slow-movement check use that picture later
            ms.setdefault("slow_taint", collections.deque(maxlen=8)).append(hits[0][0])
        hits.clear()
    if verdict == "light":
        ms["light_until"] = t + MOTION_LIGHT_HOLD_S
        ms["light_t"] = t                # 3.9.0-rc2.0 (B85): a waiting start is cancelled
        hits.clear()
        _motion_slow_forget(ms)          # 3.9.0 (B73)
        return False
    if _motion_light_pause(camera_id, ms, t):      # 3.9.0 (C30)
        hits.clear()
        return False
    # One big change records one picture later, unless that picture shows a
    # light change: the first, half-switched picture of an infrared switch
    # changed 24% of ch7 with only 69% spread (06:20:35).
    # 3.4.0 (answer 9): not inside a zone, where 3% is one or two cells
    # 3.9.0-rc2.0 (B85): not when that picture was a lit lawn (headlights on the
    # driveway, 3 files on 25 Oct); and its hit counts twice, so the check
    # above no longer calls a recorded picture "one picture only".
    if (live and not ms["recording"] and hits and hits[0][1] >= MOTION_INSTANT_PCT
            and hits[0][3] is None and not _motion_sweep(hits[0][4])):
        if len(hits) == 1:
            hits.append(hits[0])
        return True
    if verdict != "motion":
        return False
    if t < ms.get("light_until", float("-inf")):
        log.debug(f"Motion [{camera_id}]: {pct:.1f}% changed within "
                  f"{MOTION_LIGHT_HOLD_S:.0f} s of a light change — not recorded")
        _cal_reason(ms, f"within {MOTION_LIGHT_HOLD_S:.0f} s of a light change")
        return False
    if not live or ms["recording"]:
        return True
    if repeat:
        ms["peak_repeat"] = ms.get("peak_repeat", 0) + 1
        _cal_reason(ms, "a repeated picture (the stream stalled)")
        return False
    if ref[0] == ms.get("echo_of"):
        _cal_reason(ms, "the same change again (one picture only)")
        return False
    # 3.4.0 (answer 9): the second changed picture counts in the same zone
    zone = ms.get("judge_zone")
    hits.append((t, pct, thumb[0], zone, ms.get("cmp")))
    return sum(1 for h in hits if h[3] == zone) >= 2


# ── 3.9.0 (C30): passing lights (vehicle headlights) ─────────────────────────
# CrystalHeeler, 2026-10-24: headlights made most night recordings (14 of 25 on
# 2026-10-22/23). A car's light reaches several zones at once, 1-10% of the
# picture, far under the 75% light rule. Measured on 326 recordings of
# 2026-10-20 to 24 (docs/Recording_Review_2026-10-24.md, sections 7-8): when a
# headlight arrives, 90-100% of the changed cells get brighter, by +32 to +46
# grey levels, out of a quiet picture; a walking person or a cat is about half
# brighter, half darker. Such a change pauses detection until the picture
# settles. A darker-only change never starts a pause (a person in dark clothes
# walking in looks like that), but continues one while the beam leaves.
LIGHT_SWEEP_MIN_PCT   = 0.5    # % of the picture that changed
LIGHT_SWEEP_SHARE     = 0.90   # of the changed cells, this share brighter (or darker)
LIGHT_SWEEP_DELTA     = 25.0   # by at least this many grey levels on average
LIGHT_QUIET_BEFORE_S  = 1.5    # the picture before it was quiet for this long:
LIGHT_QUIET_BEFORE_PCT = 1.5   #   nothing over this % changed, unless ...
LIGHT_GROWING_SHARE   = 0.75   #   ... it was itself this much brighter (a light growing)
LIGHT_SWEEP_FILL_MAX  = 0.35   # changed cells / their bounding box: a lit lawn is scattered
                               # (at most 0.25 at the start of 71 headlight pauses), a
                               # person or an animal is one solid shape (2026-10-24)
LIGHT_QUIET_PCT       = 0.3    # two comparisons under this end the pause
# 3.9.0-rc2.0 (B84): 4 s and 6 s ended the pause while the light still crossed
# the yard (about 10 s); 20 of 29 headlight files on 25 Oct started then.
# Replayed on 409 recordings: 8 s and 12 s leave none of them, and no person,
# cat or door opening is lost (docs/Recording_Review_2026-10-24.md, section 10).
LIGHT_PAUSE_MAX_S     = 8.0    # a pause lasts at most this long
LIGHT_TAIL_S          = 3.0    # a one-sided change this soon after a pause continues it
LIGHT_PAUSE_TOTAL_S   = 12.0   # one passing light pauses at most this long in all
LIGHT_PREROLL_GAP_S   = 1.5    # a recording this soon after a pause starts before it


def _motion_light_dir(prev: tuple, curr: tuple, flags: bytes) -> tuple[float, float, float]:
    """(share of the changed cells that got brighter, their mean grey-level change,
    their share of the box around them)."""
    pa, ca = prev[0], curr[0]
    idx = [i for i, f in enumerate(flags) if f]
    if not idx:
        return 0.5, 0.0, 0.0
    deltas = [ca[i] - pa[i] for i in idx]
    gw = _grid_of(len(flags))[0]
    xs, ys = [i % gw for i in idx], [i // gw for i in idx]
    box = (max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1)
    return (sum(1 for d in deltas if d > 0) / len(deltas), sum(deltas) / len(deltas),
            len(idx) / box)


def _motion_sweep(cmp: tuple | None) -> bool:
    """3.9.0-rc2.0 (B85): a comparison that looks like a light on a lawn (C30's test)."""
    if not cmp:
        return False
    pct, share, mean, fill = cmp
    return (pct >= LIGHT_SWEEP_MIN_PCT and mean >= LIGHT_SWEEP_DELTA
            and share >= LIGHT_SWEEP_SHARE and fill <= LIGHT_SWEEP_FILL_MAX)


def _motion_light_pause(camera_id: str, ms: dict, t: float) -> bool:
    """True while a passing light pauses detection (C30); starts and ends pauses."""
    pct, share, mean, fill = ms.get("cmp", (0.0, 0.5, 0.0, 1.0))
    hist = ms.setdefault("lp_hist", collections.deque(maxlen=16))
    lp, chroma = ms.get("lp"), ms.get("chroma")
    paused = False
    if lp:
        if (lp["chroma0"] is not None and lp["chroma0"] <= NIGHT_CHROMA_MAX
                and chroma is not None and chroma >= DAY_CHROMA_MIN):
            # The camera turned from infrared to colour: a light was switched on
            # (the porch light, 2026-10-20 and 21), and a person most often follows.
            # The camera switches about 2 s after the light comes on, so the pause
            # had begun; the next recording starts before it (lp_last).
            ms["lp"] = None
            ms["lp_last"] = {"end": t, "first": lp["first"], "start_m": lp["start_m"],
                             "end_m": time.monotonic()}
            log.info(f"Motion [{camera_id}]: the camera turned to colour — a light was "
                     f"switched on, not a passing one; detection goes on")
        else:
            lp["quiet"] = lp["quiet"] + 1 if pct < LIGHT_QUIET_PCT else 0
            if (lp["quiet"] >= 2 or t - lp["t0"] >= LIGHT_PAUSE_MAX_S
                    or t - lp["first"] >= LIGHT_PAUSE_TOTAL_S):
                ms["lp"] = None
                ms["lp_last"] = {"end": t, "first": lp["first"], "start_m": lp["start_m"],
                                 "end_m": time.monotonic()}
                log.debug(f"Motion [{camera_id}]: passing light over after "
                          f"{t - lp['first']:.1f} s; detection goes on")
            else:
                paused = True
    else:
        strong = pct >= LIGHT_SWEEP_MIN_PCT and abs(mean) >= LIGHT_SWEEP_DELTA
        bright = strong and mean > 0 and share >= LIGHT_SWEEP_SHARE and fill <= LIGHT_SWEEP_FILL_MAX
        dark = strong and mean < 0 and share <= 1.0 - LIGHT_SWEEP_SHARE
        before = [x for x in hist if t - LIGHT_QUIET_BEFORE_S - 1e-6 <= x[0] < t]
        # a light that grows is scattered too; a solid bright shape is something moving
        quiet_before = all(x[1] < LIGHT_QUIET_BEFORE_PCT
                           or (x[2] >= LIGHT_GROWING_SHARE and x[3] <= LIGHT_SWEEP_FILL_MAX)
                           for x in before)
        last = ms.get("lp_last")
        colour_now = bool(ms.get("night")) and chroma is not None and chroma >= DAY_CHROMA_MIN
        start = None
        if bright and quiet_before and not colour_now:
            start = {"t0": t, "first": t, "start_m": time.monotonic()}
            what = (f"a passing light ({pct:.1f}% of the picture, {share:.0%} of it brighter "
                    f"by {mean:+.0f})")
        elif ((dark or bright) and last and 0 <= t - last["end"] <= LIGHT_TAIL_S
                and t - last["first"] < LIGHT_PAUSE_TOTAL_S):
            # 3.9.0-rc2.0 (B84): the light still moving (brighter) continues it too,
            # with no quiet picture before it: the light itself was before it
            start = {"t0": t, "first": last["first"], "start_m": last["start_m"]}
            what = "the passing light leaving" if dark else "the passing light still moving"
        if start:
            ms["lp"] = {**start, "quiet": 0, "chroma0": chroma}
            _motion_slow_forget(ms)
            log.info(f"Motion [{camera_id}]: {what} — detection paused until the picture settles")
            paused = True
    hist.append((t, pct, share, fill))
    if paused:
        # also keeps the slow-movement check out (it honours light_until)
        ms["light_until"] = max(ms.get("light_until", float("-inf")), t + 0.6)
        _cal_reason(ms, "a passing light (headlights)")
    return paused


def _motion_light_preroll(ms: dict, now_m: float) -> float:
    """3.9.0 (C30): seconds of pre-roll; a recording right after a pause starts before it.

    The front door's glass pauses detection like a light, then a person steps
    out (2026-10-23 20:26:49): the file then holds the door opening too.
    """
    held = ms.get("held_s", 0.0)            # 3.9.0-rc2.0 (B85): the start waited this long
    last = ms.get("lp_last")
    if last and now_m - held - last["end_m"] <= LIGHT_PREROLL_GAP_S:
        return min(MOTION_PREROLL_S + (now_m - last["start_m"]), MOTION_BUF_KEEP_S)
    return min(MOTION_PREROLL_S + held, MOTION_BUF_KEEP_S)


def _motion_slow_forget(ms: dict) -> None:
    """3.9.0 (B73): after a light change the slow check compares only newer pictures.

    Its reference is ZONE_SLOW_S old, longer than the light hold, so at dusk it
    compared with a picture from before the infrared switch and recorded it.
    """
    ms["slow_refs"] = collections.deque()
    ms["slow_runs"] = {}


def _motion_second_look(camera_id: str, older: tuple | None, thumb: tuple) -> bool:
    """3.0.1 (B20): True when the picture also differs from an older one."""
    if older is None or len(older[0]) != len(thumb[0]):
        return False
    if anycam_zones.has_zones(camera_id):
        # 3.4.0: the same zone must pass against the older picture too
        flags, _frac, spread = _motion_cells(older, thumb)
        zone = (_MOTION.get(camera_id) or {}).get("judge_zone")
        if spread <= MOTION_LIGHT_FRACTION and any(
                n == zone and p >= need for n, p, need in _motion_zone_results(camera_id, flags)):
            return True
    else:
        frac, spread = _motion_diff(older, thumb)
        if spread <= MOTION_LIGHT_FRACTION and frac * 100.0 >= _motion_area_now(camera_id):
            return True
    log.info(f"Motion [{camera_id}]: changed against the last picture only — not recorded "
             f"(most often an insect near the lens)")
    _cal_reason(_MOTION.get(camera_id) or {}, "changed against the last picture only")
    return False


SLOW_TAINT_S = 0.6     # 3.9.0 (B78): pictures this close to a rejected insect picture


def _motion_slow_look(camera_id: str, ms: dict, thumb: tuple, t: float) -> bool:
    """3.4.0 (C17, answer 11): a zone also compares with the picture ZONE_SLOW_S old.

    A garage door takes several seconds to open, so it changes little from
    one second to the next. The comparison counts only when the same zone
    passes its level on two pictures in a row, so a passing insect does not.
    """
    # 3.9.0-rc2.0 (B84): during a light pause or hold, and 0.6 s after, no picture
    # is kept; 5 s after a car passed it compared the scene with a lit picture
    # (14 headlight files on 25 Oct)
    if t < ms.get("light_until", float("-inf")) or ms.get("lp"):
        _motion_slow_forget(ms)
        return False
    slow = ms.setdefault("slow_refs", collections.deque())
    age = anycam_zones.ZONE_SLOW_S - 1e-6
    while len(slow) > 1 and t - slow[1][0] >= age:
        slow.popleft()
    old = slow[0][1] if slow and t - slow[0][0] >= age else None
    slow.append((t, thumb))
    runs = ms.setdefault("slow_runs", {})
    # 3.9.0 (B78): a picture the fast checks rejected as an insect is no reference
    old_t = slow[0][0] if slow else None
    tainted = old_t is not None and any(abs(old_t - x) <= SLOW_TAINT_S for x in ms.get("slow_taint", ()))
    if old is None or len(old[0]) != len(thumb[0]) or tainted:
        runs.clear()
        return False
    flags, _frac, spread = _motion_cells(old, thumb)
    if spread > MOTION_LIGHT_FRACTION:
        runs.clear()
        return False
    passed = {n for n, p, need in _motion_zone_results(camera_id, flags)
              if n is not None and p >= need}
    for name in [n for n in runs if n not in passed]:
        del runs[name]
    for name in passed:
        runs[name] = runs.get(name, 0) + 1
    winner = next((n for n in sorted(passed) if runs[n] >= 2), None)
    if winner is None:
        return False
    runs.clear()
    ms["judge_zone"] = winner
    if not ms["recording"]:                                 # 3.9.0 (B74): it starts this one
        ms["cal_slow"] = next(((n, p, need) for n, p, need in _motion_zone_results(camera_id, flags)
                               if n == winner), None)
    _motion_check_log(ms, ("slow", winner), f"Motion [{camera_id}]: zone \"{winner}\" changed "
                      f"over {anycam_zones.ZONE_SLOW_S:.0f} s on two pictures in a row (slow movement)")
    return True


def _motion_tick(camera_id: str, ms: dict, now_m: float) -> None:
    """No motion in this frame: stop a recording once quiet long enough."""
    if ms["recording"] and _motion_quiet(camera_id, ms, now_m):
        asyncio.create_task(_stop_recording(camera_id))


def _motion_reset_prev(camera_id: str) -> None:
    ms = _MOTION.get(camera_id)
    if ms:
        ms.setdefault("refs", collections.deque()).clear()
        ms["snap_cmp_t"] = 0.0
        # 2.6.7: the live and snapshot paths keep different clocks.
        ms["hits"], ms["light_until"] = [], float("-inf")
        ms["last_cur"] = ms["last_ref"] = ms["echo_of"] = None
        ms.setdefault("snap_hist", collections.deque(maxlen=2)).clear()
        ms.setdefault("slow_refs", collections.deque()).clear()     # 3.4.0
        ms["slow_runs"] = {}
        # 3.9.0-rc2.0 (B84): "passing light over after -284.0 s" (25 Oct 22:45:56):
        # a pause from the last run's clock went on in the new one
        ms["lp"] = ms["lp_last"] = ms["hold"] = None
        ms.setdefault("lp_hist", collections.deque(maxlen=16)).clear()
        ms.pop("light_t", None)
# ── 2.6.6: pixel comparison (build plan B16, C13) ──────────────────────────
# 2.6.5 compared JPEG file sizes. A person barely changes the size of a
# 9-10 KB Lorex snapshot, so nothing was recorded all day on 2026-09-30,
# while the night-to-day switch at 06:21 did trigger. Now each frame is
# shrunk to a MOTION_GRID greyscale picture and compared pixel by pixel,
# after cancelling any change in overall brightness and contrast.
MOTION_GRID = (64, 48)          # 3,072 cells; a person at 20 m covers several
MOTION_PIXEL_DELTA = 24         # of 255: smaller differences are sensor noise
MOTION_LIGHT_FRACTION = 0.75    # change spread over more of the picture = light
MOTION_REGIONS = (4, 4)         # the picture in 16 areas, for MOTION_LIGHT_FRACTION
MOTION_REGION_CHANGED = 0.10    # an area counts as changed at 10% of its cells
MOTION_COMPARE_S = 1.0          # snapshot path: at most one decode per second
MOTION_REF_S = 1.0              # compare each picture with the one this long before
# 2.6.7 (CrystalHeeler's overnight test, 2026-10-01). 11 of 22 ch4 recordings were
# insects: one blurred streak, lit by the infrared, in a single picture.
# Two infrared-colour switches on ch7 were recorded although each was
# logged as a light change: a switch spreads over about a second of
# comparisons, and some of them stay under the 75% light rule.
MOTION_CONFIRM_S = 0.5          # live stream: a 2nd changed picture within this
MOTION_INSTANT_PCT = 3.0        # one picture this changed records (0.25 s later)
MOTION_LIGHT_HOLD_S = 2.0       # after a light change, nothing counts for this


def _motion_grid(camera_id: str) -> tuple[int, int]:
    """3.4.0 (C17, answer 8): a camera with zones is judged on a finer grid."""
    return anycam_zones.ZONE_GRID if anycam_zones.has_zones(camera_id) else MOTION_GRID


def _grid_of(cells: int) -> tuple[int, int]:
    gw, gh = anycam_zones.ZONE_GRID
    return anycam_zones.ZONE_GRID if cells == gw * gh else MOTION_GRID


def _motion_thumb(jpeg: bytes, grid: tuple[int, int] = MOTION_GRID) -> tuple[bytes, float, float] | None:
    """Decode a JPEG to MOTION_GRID greyscale; return (pixels, mean, spread).

    spread is the standard deviation, floored at 1 for a flat picture.

    draft() lets the JPEG decoder skip to a 1/2 to 1/8 scale, so a frame
    costs about a millisecond. None when the bytes do not decode.
    """
    try:
        img = Image.open(io.BytesIO(jpeg))
        img.draft("L", (grid[0] * 2, grid[1] * 2))
        pixels = img.convert("L").resize(grid, Image.Resampling.BOX).tobytes()
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    return _motion_thumb_gray(pixels)


def _motion_thumb_gray(pixels: bytes) -> tuple[bytes, float, float]:
    """(pixels, mean, spread) for a MOTION_GRID greyscale picture."""
    mean = sum(pixels) / len(pixels)
    spread = (sum((v - mean) ** 2 for v in pixels) / len(pixels)) ** 0.5
    return pixels, mean, max(spread, 1.0)


def _motion_diff(prev: tuple[bytes, float, float],
                 curr: tuple[bytes, float, float]) -> tuple[float, float]:
    """Return (changed, spread) for two MOTION_GRID pictures.

    changed: share of cells that differ by more than MOTION_PIXEL_DELTA.
    spread:  share of the MOTION_REGIONS areas with MOTION_REGION_CHANGED
             or more of their cells changed.

    Both pictures are first normalised to the same brightness and contrast
    (each cell measured from its picture's mean, in units of its spread),
    so a cloud or an exposure change that moves the whole picture together
    changes nothing. That normalisation also means even an unrelated
    picture shows only about half its cells changed, so a light change is
    told apart by spread, not by amount: a person changes a few
    neighbouring areas, a night-to-day switch changes all of them.
    """
    _flags, changed, spread = _motion_cells(prev, curr)
    return changed, spread


def _motion_cells(prev: tuple[bytes, float, float],
                  curr: tuple[bytes, float, float]) -> tuple[bytearray, float, float]:
    """(flags, changed, spread): _motion_diff, plus one flag byte per changed cell.

    3.4.0 (C17): the zones count their own cells from the flags. The grid
    is taken from the picture size: 64 x 48, or 128 x 96 with zones.
    """
    (pa, ma, sa), (ca, mc, sc) = prev, curr
    gw, gh = _grid_of(len(ca))
    rx, ry = MOTION_REGIONS
    # MOTION_PIXEL_DELTA is in grey levels at the previous picture's contrast.
    limit = MOTION_PIXEL_DELTA / sa
    per_region = [0] * (rx * ry)
    flags = bytearray(len(ca))
    changed = 0
    for i, (a, c) in enumerate(zip(pa, ca)):
        if abs((c - mc) / sc - (a - ma) / sa) > limit:
            changed += 1
            flags[i] = 1
            y, x = divmod(i, gw)
            per_region[(y * ry // gh) * rx + (x * rx // gw)] += 1
    region_cells = (gw // rx) * (gh // ry)
    busy = sum(1 for n in per_region if n >= MOTION_REGION_CHANGED * region_cells)
    return flags, changed / len(ca), busy / len(per_region)


# 3.9.0 (B70): the per-check lines were 62-66% of a 10,000-line log export
# (about 4 a second during movement). Each kind now logs at DEBUG when a
# movement starts (quiet for CHECK_LOG_GAP_S before) and then at most once
# every CHECK_LOG_EVERY_S while it lasts; the calibration log gives one line
# per movement. DEBUG alone was not enough: log_debug is on by default.
CHECK_LOG_GAP_S = 1.0
CHECK_LOG_EVERY_S = 5.0


def _motion_check_log(ms: dict, key: object, text: str, now_m: float | None = None) -> None:
    """Log a per-check line at the start of a movement, then at most every CHECK_LOG_EVERY_S."""
    now_m = time.monotonic() if now_m is None else now_m
    seen = ms.setdefault("check_log", {})
    st = seen.get(key)
    if st is None or now_m - st[1] > CHECK_LOG_GAP_S or now_m - st[0] >= CHECK_LOG_EVERY_S:
        log.debug(text)
        seen[key] = [now_m, now_m]
    else:
        st[1] = now_m


def _motion_zone_results(camera_id: str, flags: bytes) -> list[tuple[str | None, float, float]]:
    """Each zone, and outside the zones, as (name, % changed, % needed)."""
    boost = _motion_boost(camera_id)
    return anycam_zones.evaluate(
        camera_id, flags, _grid_of(len(flags)), _motion_area_now(camera_id),
        lambda level: _motion_area_pct(level + boost))


def _motion_judge(camera_id: str, prev: tuple[bytes, float, float],
                  curr: tuple[bytes, float, float]) -> tuple[str, float]:
    """("motion" | "light" | "", % changed); logs for tuning."""
    flags, frac, spread = _motion_cells(prev, curr)
    pct = frac * 100.0
    ms = _MOTION.get(camera_id) or {}
    ms["judge_zone"] = None
    ms["cmp"] = (pct, *_motion_light_dir(prev, curr, flags))      # 3.9.0 (C30)
    _motion_note_peak(camera_id, pct, spread > MOTION_LIGHT_FRACTION)
    if _cal_on():                                       # 3.8.2: the calibration log
        zoned = anycam_zones.has_zones(camera_id)
        _cal_track(camera_id, ms,
                   _motion_zone_results(camera_id, flags) if zoned
                   else [(None, pct, _motion_area_now(camera_id))],
                   zoned, spread > MOTION_LIGHT_FRACTION)
    # 3.4.0 (answer 10): the light rule stays on the whole picture
    if spread > MOTION_LIGHT_FRACTION:
        log.info(f"Motion [{camera_id}]: change across {spread:.0%} of the "
                 f"picture ({pct:.0f}% of it changed) — treated as a light "
                 f"change, not recorded")
        return "light", pct
    area = _motion_area_now(camera_id)
    if anycam_zones.has_zones(camera_id):
        # 3.4.0 (C17): each zone on its own cells, outside the zones on the rest
        results = _motion_zone_results(camera_id, flags)
        _motion_note_zone_peaks(ms, results)
        if ms.get("recording"):
            _motion_note_rec_zones(ms, [n for n, p, need in results if p >= need])   # 3.9.0 (C31)
        hit = anycam_zones.best_pass(results)
        if hit is None:
            return "", max((p for _n, p, _need in results), default=0.0)
        name, zpct = hit
        ms["judge_zone"] = name
        need = next(nd for n, _p, nd in results if n == name)
        _motion_check_log(ms, ("zone", name), f"Motion [{camera_id}]: {zpct:.1f}% of "
                          f"{_zone_label(name)} changed (threshold {need:.1f}%)")
        return "motion", zpct
    if pct >= area:
        _motion_check_log(ms, ("picture",), f"Motion [{camera_id}]: {pct:.1f}% of the "
                          f"picture changed (threshold {area:.1f}%)")
        return "motion", pct
    if pct >= area / 2:
        log.debug(f"Motion [{camera_id}]: {pct:.1f}% changed, under the "
                  f"{area:.1f}% threshold")
    return "", pct
# ── 3.8.2: the motion calibration log ──────────────────────────────────────
# CrystalHeeler, 2026-10-20: to set each zone so cats and armadillos record at
# night and birds do not, one line per movement per zone, written when the
# movement ends: when it started (date and time), how long it lasted, its
# peak, the zone's threshold, and what happened (recorded with the file name,
# or why not). A movement counts from a quarter of the zone's threshold, so a
# near miss shows too. Setting motion_calibration_log, off by default.
CAL_FLOOR_SHARE   = 0.25     # a movement counts from this share of the threshold ...
CAL_FLOOR_MIN_PCT = 0.3      # ... and never below this, so sensor noise stays out
CAL_END_S         = 1.0      # below the floor this long ends the movement
CAL_MAX_PER_MIN   = 20       # lines per camera per minute; the rest are counted


def _cal_on() -> bool:
    return bool(CFG_MOTION_CALIBRATION)


def _cal_label(name: str | None, zoned: bool) -> str:
    if not zoned:
        return "the picture"
    return f'zone "{name}"' if name is not None else "outside the zones"


def _cal_track(camera_id: str, ms: dict, results: list, zoned: bool, light: bool,
               now_m: float | None = None) -> None:
    """Feed one check: (zone, % changed, % needed) for each zone, or the whole picture."""
    now_m = time.monotonic() if now_m is None else now_m
    events = ms.setdefault("cal", {})
    for name, pct, need in results:
        floor = max(need * CAL_FLOOR_SHARE, CAL_FLOOR_MIN_PCT)
        if pct < floor:
            continue
        ev = events.get(name)
        if ev is None:
            ev = events[name] = {"wall": time.time(), "start": now_m, "peak": 0.0, "need": need,
                                 "during": bool(ms.get("recording")), "outcome": None,
                                 "reason": None, "label": _cal_label(name, zoned)}
        ev["last"] = now_m
        if pct > ev["peak"]:
            ev["peak"], ev["need"] = pct, need
        if light:
            ev["reason"] = ev["reason"] or "light change"
    _cal_flush(camera_id, ms, now_m)


def _cal_reason(ms: dict, reason: str) -> None:
    """Why a check that passed did not record; kept for the open movements."""
    for ev in (ms.get("cal") or {}).values():
        if ev.get("outcome") is None:
            ev["reason"] = reason


def _cal_recorded(ms: dict, zone: str | None, base: str) -> None:
    """A recording started: the movement that started it gets the file name.

    3.9.0 (B74): a recording the slow-movement check started has often no open
    movement for its zone (the fast checks stayed below the floor), so it got
    no line. It gets one now, marked as slow movement.
    """
    slow = ms.pop("cal_slow", None)          # (zone, % changed, % needed) from _motion_slow_look
    events = ms.setdefault("cal", {})
    if slow is not None and slow[0] == zone and zone not in events:
        now_m = time.monotonic()
        events[zone] = {"wall": time.time(), "start": now_m, "last": now_m, "peak": slow[1],
                        "need": slow[2], "during": False, "outcome": None, "reason": None,
                        "label": _cal_label(zone, True)}
    for name, ev in events.items():
        if ev.get("outcome") is None:
            if name == zone:
                how = " (slow movement, over 5 s)" if slow is not None and slow[0] == zone else ""
                ev["outcome"] = f"recorded{how} → {base}"
            else:
                ev["outcome"] = "during a recording"


def _cal_outcome(ev: dict) -> str:
    if ev.get("outcome"):
        return ev["outcome"]
    if ev.get("during"):
        return "during a recording"
    if ev["peak"] < ev["need"]:
        return "not recorded: below the threshold"
    return f"not recorded: {ev.get('reason') or 'passed the threshold, not confirmed'}"


def _cal_pcts(peak: float, need: float) -> tuple[str, str]:
    """3.9.0 (B72): "peak 1.5% (records at 1.5%) — not recorded" read as a contradiction."""
    a, b = f"{peak:.1f}", f"{need:.1f}"
    return (f"{peak:.2f}", f"{need:.2f}") if a == b else (a, b)


def _cal_flush(camera_id: str, ms: dict, now_m: float, force: bool = False) -> None:
    """Log the movements that ended (below the floor for CAL_END_S, or force)."""
    events = ms.get("cal") or {}
    for name in [n for n, ev in events.items() if force or now_m - ev["last"] >= CAL_END_S]:
        ev = events.pop(name)
        minute = int(time.time() // 60)
        if ms.get("cal_minute") != minute:
            if ms.get("cal_dropped"):
                log.info(f"Motion [{camera_id}]: calibration: {ms['cal_dropped']} more "
                         f"movement(s) in the last minute not logged (limit {CAL_MAX_PER_MIN})")
            ms["cal_minute"], ms["cal_lines"], ms["cal_dropped"] = minute, 0, 0
        if ms["cal_lines"] >= CAL_MAX_PER_MIN:
            ms["cal_dropped"] += 1
            continue
        ms["cal_lines"] += 1
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ev["wall"]))
        peak, need = _cal_pcts(ev["peak"], ev["need"])
        log.info(f"Motion [{camera_id}]: {when} {ev['label']}: movement "
                 f"{max(0.0, ev['last'] - ev['start']):.1f} s, peak {peak}% "
                 f"(records at {need}%) — {_cal_outcome(ev)}")


# ── 2.6.6: tuning line ──────────────────────────────────────────────────────
# Once a minute, each armed camera logs the largest change it saw, so one
# walk past the camera shows where to set the sensitivity. CrystalHeeler, 2026-09-30:
# a setting of 5 needed 62% of the picture and nothing recorded, with no
# log line to show how close a walk-past came.
MOTION_PEAK_REPORT_S = 60


def _motion_note_peak(camera_id: str, pct: float, light: bool) -> None:
    ms = _MOTION.get(camera_id)
    if not ms:
        return
    ms["peak_n"] = ms.get("peak_n", 0) + 1
    if light:
        ms["peak_light"] = ms.get("peak_light", 0) + 1
    elif pct > ms.get("peak_pct", 0.0):
        ms["peak_pct"] = pct


def _zone_label(name: str | None) -> str:
    return f'zone "{name}"' if name is not None else "the picture outside the zones"


def _motion_note_zone_peaks(ms: dict, results: list) -> None:
    """3.4.0 (answer 14): the largest change in each zone and outside them."""
    peaks = ms.setdefault("zone_peaks", {})
    for name, pct, need in results:
        old = peaks.get(name)
        if old is None or pct > old[0]:
            peaks[name] = (pct, need)


def _motion_note_rec_zones(ms: dict, names: list) -> None:
    """3.9.0 (C31): the zones that passed during a recording, in the order they did.

    A person who walked through four zones made one file named after the
    first; the other three zones had detected the person too, but nothing
    showed it. The Storage tab now lists them all.
    """
    seen = ms.setdefault("rec_zone_list", [])
    for name in names:
        if name is not None and name not in seen:
            seen.append(name)


def _motion_zone_peak_text(peaks: dict) -> str:
    return ", ".join(
        f"{n if n is not None else 'outside'}: peak {p:.1f}% (records at {need:.1f}%)"
        for n, (p, need) in sorted(peaks.items(), key=lambda kv: (kv[0] is None, kv[0] or "")))


def _motion_report_peak(camera_id: str, ms: dict, now_m: float) -> None:
    """Log and reset the minute's largest change (called by the keeper)."""
    start = ms.setdefault("peak_since", now_m)
    if now_m - start < MOTION_PEAK_REPORT_S:
        return
    n, pct, light = ms.get("peak_n", 0), ms.get("peak_pct", 0.0), ms.get("peak_light", 0)
    single, repeat = ms.get("peak_single", 0), ms.get("peak_repeat", 0)
    ms["peak_since"], ms["peak_n"], ms["peak_pct"], ms["peak_light"] = now_m, 0, 0.0, 0
    ms["peak_single"] = ms["peak_repeat"] = 0
    zone_peaks = ms.pop("zone_peaks", None)
    if zone_peaks:
        ms["last_zone_peaks"] = zone_peaks
        # 3.9.0 (B70): INFO only when a zone saw movement, DEBUG for a still minute
        moved = any(p >= CAL_FLOOR_MIN_PCT for p, _need in zone_peaks.values())
        (log.info if moved else log.debug)(
            f"Motion [{camera_id}]: zones in the last {MOTION_PEAK_REPORT_S} s: "
            f"{_motion_zone_peak_text(zone_peaks)}")
    if not n:
        return
    ms["last_peak_pct"] = pct      # for the settings panel's live readout
    cfg = _motion_cfg(camera_id)
    extra = f"; {light} light change(s) ignored" if light else ""
    extra += f"; {single} single-picture change(s) ignored" if single else ""
    extra += f"; {repeat} repeated picture(s)" if repeat else ""
    boost = _motion_boost(camera_id)
    needed = _motion_level_for_pct(pct, boost)
    at = (f"would record at sensitivity {needed} or higher" if needed <= 100
          else "too small to record at any sensitivity")
    mode = (f"night +{boost}" if boost else "day")
    chroma = ms.get("chroma")
    colour = f", colour {chroma:.1f}" if chroma is not None else ""
    # INFO when something moved, DEBUG for a still scene, so a quiet night
    # does not fill the log.
    (log.info if pct >= 0.5 or light or single else log.debug)(
        f"Motion [{camera_id}]: largest change in the last {MOTION_PEAK_REPORT_S} s: "
        f"{pct:.1f}% of the picture, {at} (set to {cfg['level']}, {mode}, "
        f"{_motion_area_now(camera_id):.2f}%{colour}; {n} comparisons{extra})")


def _motion_finish_files(camera_id: str, ms: dict) -> None:
    """Name a finished recording's files and log them.

    2.6.6 (CrystalHeeler, 2026-10-01): the _partNN suffix only when an event ran
    past the file length and was split. ffmpeg's segment muxer must number
    every file while it records, because it cannot know the event will end
    early, so a lone <camera>_<date>_<time>_part01.mp4 is renamed here to
    <camera>_<date>_<time>.mp4 once ffmpeg has stopped.
    """
    base, clip = ms.get("clip_base"), ms.get("clip_path")
    if not (base and clip):
        return
    # 3.9.0 (C31): the Storage tab shows every zone the recording passed through
    zones = ms.get("rec_zone_list") or []
    if zones and anycam_zones.REC_ZONES.get(base) != ", ".join(zones):
        anycam_zones.REC_ZONES[base] = ", ".join(zones)
        ms["rec_zones_dirty"] = True
    also = f" (zones: {', '.join(zones)})" if len(zones) > 1 else ""
    parts = sorted(clip.parent.glob(f"{base}_part*.mp4"))
    if len(parts) == 1:
        single = clip.parent / f"{base}.mp4"
        try:
            parts[0].rename(single)
            ms["clip_path"] = single
        except OSError as ex:
            log.warning(f"Motion [{camera_id}]: could not rename {parts[0].name}: {ex}")
            single = parts[0]
        log.info(f"Motion [{camera_id}]: recording stopped → {single}{also}")
        parts = [single]
    elif parts:
        log.info(f"Motion [{camera_id}]: recording stopped → {len(parts)} files, "
                 f"{parts[0].name} to {parts[-1].name}{also}")
    else:
        log.warning(f"Motion [{camera_id}]: recording stopped, but no file was "
                    f"written in {clip.parent} — see the REC: lines above")
    # 3.6.0 (C14): upload the finished files, if the camera has a destination
    anycam_upload.enqueue(camera_id, parts)


def _motion_quiet(camera_id: str, ms: dict, now_m: float) -> bool:
    """True once the camera's cooldown plus tail have passed with no motion."""
    cfg = _motion_cfg(camera_id)
    return now_m - ms.get("last_motion", 0) > cfg["cooldown"] + cfg["tail"]
# ── 2.6.6: per-camera recording settings ───────────────────────────────────
# Each camera has its own sensitivity, cooldown, tail, file length and
# folder, set from the cog on its card (CrystalHeeler, 2026-09-30). When the
# Configuration tab's global_recording_settings is on, its values replace
# every camera's. Stored in MOTION_FILE beside the armed list.
MOTION_DEFAULTS = {"level": 63, "cooldown": 5, "tail": 3, "clip": "30s",
                   "path": "/media/anycam"}
MOTION_CLIP_CHOICES = {"10s": 10, "20s": 20, "30s": 30, "1min": 60,
                       "2min": 120, "5min": 300}
MOTION_PATH_ROOT = "/media"     # the add-on maps only /media (config.yaml)
_MOTION_CFG: dict = {}          # camera_id -> settings that differ from defaults


def _motion_area_pct(level: int) -> float:
    """Sensitivity -> share of the picture: 74% at 1 down to 1% at 100 (log).

    2.6.7: the night boost can take a camera past 100; the curve continues
    down to MOTION_AREA_FLOOR, about 121.
    """
    return max(MOTION_AREA_FLOOR, 74.0 * (1.0 / 74.0) ** ((level - 1) / 99.0))


def _motion_level_needed(pct: float) -> float:
    """The effective sensitivity at which a change of pct% just records."""
    import math
    if pct <= 0:
        return math.inf
    if pct >= 74.0:
        return 1.0
    return 1 + 99 * math.log(74.0 / pct) / math.log(74.0)


def _motion_level_for_pct(pct: float, boost: int = 0) -> int:
    """Lowest slider setting that records a change of pct%; 101 = none does.

    boost: the night boost in force; the slider value is what the user set,
    so the boost comes off the effective level.
    """
    import math
    need = _motion_level_needed(pct)
    if pct < MOTION_AREA_FLOOR:
        return 101
    return max(1, min(101, math.ceil(need - boost - 1e-9)))


def _motion_cfg(camera_id: str) -> dict:
    """The settings in force for one camera."""
    if CFG_MOTION_GLOBAL:
        cfg = {"level": CFG_MOTION_LEVEL, "cooldown": CFG_MOTION_COOL,
               "tail": CFG_MOTION_PAD, "path": CFG_RECORDINGS,
               "clip": next((k for k, v in MOTION_CLIP_CHOICES.items()
                             if v == CFG_MOTION_CLIP_S), "30s")}
    else:
        cfg = {**MOTION_DEFAULTS, **_MOTION_CFG.get(camera_id, {})}
    cfg["area_pct"] = _motion_area_pct(cfg["level"])
    cfg["clip_s"] = MOTION_CLIP_CHOICES[cfg["clip"]]
    return cfg


def _motion_validate(data: dict) -> tuple[dict, list[str]]:
    """Check one camera's settings from the page; return (clean, errors)."""
    clean, errors = {}, []
    for key, lo, hi, label in (("level", 1, 100, "Sensitivity"),
                               ("cooldown", 1, 300, "Cooldown"),
                               ("tail", 0, 30, "Tail")):
        try:
            v = int(data.get(key, MOTION_DEFAULTS[key]))
        except (TypeError, ValueError):
            errors.append(f"{label} must be a whole number")
            continue
        if not lo <= v <= hi:
            errors.append(f"{label} must be {lo} to {hi}")
        clean[key] = v
    clip = str(data.get("clip", MOTION_DEFAULTS["clip"]))
    if clip not in MOTION_CLIP_CHOICES:
        errors.append("Recording length must be one of " + ", ".join(MOTION_CLIP_CHOICES))
    clean["clip"] = clip
    raw = str(data.get("path", MOTION_DEFAULTS["path"])).strip()
    # posixpath: these are paths inside the Linux container on any host.
    import posixpath
    path = posixpath.normpath(raw) if raw else ""
    if not path.startswith("/") or not (path == MOTION_PATH_ROOT
                                        or path.startswith(MOTION_PATH_ROOT + "/")):
        errors.append(f"Recording folder must be under {MOTION_PATH_ROOT}")
    clean["path"] = path
    return clean, errors


def _motion_settings_payload(camera_id: str) -> dict:
    ms = _MOTION.get(camera_id) or {}
    peak = max(ms.get("peak_pct", 0.0), ms.get("last_peak_pct", 0.0))
    cfg = _motion_cfg(camera_id)
    return {
        "settings": {k: cfg[k] for k in MOTION_DEFAULTS},
        "custom":   camera_id in _MOTION_CFG,
        "global":   CFG_MOTION_GLOBAL,
        "defaults": MOTION_DEFAULTS,
        "clip_choices": list(MOTION_CLIP_CHOICES),
        "armed":    bool(ms.get("enabled")),
        # The lowest sensitivity that would have recorded the biggest
        # movement in the last minute or two; None before any comparison.
        "peak_level": _motion_level_for_pct(peak, _motion_boost(camera_id)) if peak > 0 else None,
        "night":      bool(ms.get("night")),
        "night_boost": MOTION_NIGHT_BOOST,
        "night_note": HA_LOC_NOTE if _HA_LOC_STATE["mismatch"] else ms.get("night_note"),
        **_motion_zones_payload(camera_id),
    }


def _motion_zones_payload(camera_id: str) -> dict:
    """3.4.0 (C17): the camera's zones, and what each saw in the last minute."""
    ms = _MOTION.get(camera_id) or {}
    cfg = anycam_zones.zone_cfg(camera_id)
    peaks = ms.get("zone_peaks") or ms.get("last_zone_peaks") or {}
    return {
        "zones": [{**z, "cells": len(anycam_zones.polygon_cells(z["points"], anycam_zones.ZONE_GRID))
                   if z.get("closed") else 0} for z in cfg["zones"]],
        "zones_only": cfg["zones_only"],
        "zone_max": anycam_zones.ZONE_MAX,
        "zone_min_cells": anycam_zones.ZONE_MIN_CELLS,
        "zone_peaks": [{"name": n, "peak": round(p, 2), "need": round(need, 2)}
                       for n, (p, need) in peaks.items()],
        "recording_zone": ms.get("rec_zone") if ms.get("recording") else None,
    }


async def api_motion_zones(request: web.Request) -> web.Response:
    """GET/POST /api/cameras/{camera_id}/motion/zones (3.4.0, C17).

    POST replaces all of the camera's zones. Zones belong to the camera,
    also while the global recording settings are on.
    """
    camera_id = request.match_info["camera_id"]
    if camera_id not in CAMERAS:
        return web.json_response({"error": "Camera not found"}, status=404)
    if request.method == "POST":
        try:
            data = await request.json()
        except ValueError:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        clean, errors = anycam_zones.validate(data)
        if errors:
            return web.json_response({"error": "; ".join(errors)}, status=400)
        anycam_zones.set_zones(camera_id, clean)
        names = ", ".join(z["name"] + ("" if z["closed"] else " (open)") for z in clean["zones"])
        log.info(f"Motion [{camera_id}]: zones saved — {len(clean['zones'])} zone(s)"
                 + (f": {names}" if names else "")
                 + f"; detection in zones only {'on' if clean['zones_only'] else 'off'}")
        try:
            await asyncio.to_thread(_motion_save)
        except OSError as ex:
            log.warning(f"Motion: could not save {MOTION_FILE}: {ex}")
        await _motion_restart_detector(camera_id)
    return web.json_response(_motion_zones_payload(camera_id))


async def _motion_restart_detector(camera_id: str) -> None:
    """The grid follows the zones, so the detector starts again (the buffer keeps running)."""
    ms = _MOTION.get(camera_id)
    if not ms:
        return
    task = ms.pop("det_task", None)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    _motion_reset_prev(camera_id)
    if ms.get("enabled"):
        _motion_ensure_pipelines(camera_id)


async def api_motion_settings(request: web.Request) -> web.Response:
    """GET/POST/DELETE /api/cameras/{camera_id}/motion/settings (2.6.6).

    POST saves the camera's own settings; DELETE returns it to defaults.
    Both are refused while the global settings are on, because they would
    not apply.
    """
    camera_id = request.match_info["camera_id"]
    if camera_id not in CAMERAS:
        return web.json_response({"error": "Camera not found"}, status=404)
    if request.method != "GET":
        if CFG_MOTION_GLOBAL:
            return web.json_response(
                {"error": "Global recording settings are on in the Configuration tab"},
                status=409)
        if request.method == "DELETE":
            _MOTION_CFG.pop(camera_id, None)
        else:
            try:
                data = await request.json()
            except ValueError:
                return web.json_response({"error": "Invalid JSON"}, status=400)
            clean, errors = _motion_validate(data if isinstance(data, dict) else {})
            if errors:
                return web.json_response({"error": "; ".join(errors)}, status=400)
            custom = {k: v for k, v in clean.items() if v != MOTION_DEFAULTS[k]}
            if custom:
                _MOTION_CFG[camera_id] = custom
            else:
                _MOTION_CFG.pop(camera_id, None)
        log.info(f"Motion [{camera_id}]: settings saved — "
                 + ", ".join(f"{k}={v}" for k, v in _motion_cfg(camera_id).items()
                             if k in MOTION_DEFAULTS))
        try:
            await asyncio.to_thread(_motion_save)
        except OSError as ex:
            log.warning(f"Motion: could not save {MOTION_FILE}: {ex}")
    return web.json_response(_motion_settings_payload(camera_id))


def _motion_ensure_loop(camera_id: str) -> None:
    """Start the thumbnail loop for an armed camera if it is not running."""
    camera = CAMERAS.get(camera_id)
    if not camera or camera.get("display") in ("webrtc", "wsrtsp", "info", "appliance"):
        return
    # The classic Enhanced View runs its own native-res loop for this
    # camera, and motion runs inside it.
    if anycam_focus._FOCUSED_CAMERA == camera_id and anycam_focus._FOCUS_ENGINE == "legacy":
        return
    state = _snap_state(camera_id)
    task = state.get("task")
    if task and not task.done():
        return
    url = build_authenticated_url(camera)
    if not url:
        return
    _snap_last_access[camera_id] = time.monotonic()
    state["task"] = asyncio.create_task(snap_loop(camera_id, url, camera))
    log.info(f"Motion [{camera_id}]: started the thumbnail loop — "
             f"motion detection is armed")
# ── 2.6.6: live-stream motion pipelines ─────────────────────────────────────
# CrystalHeeler, 2026-10-01: recordings missed the start of each event, and
# sensitivity 80-90 still reacted late. Detection compared snapshots, which
# the Lorex channels deliver every 1.9 s, and the recording connected to the
# camera only after motion was found. Now an armed camera runs two ffmpeg
# pipelines, the usual video-recorder split:
#   detector: its smallest stream, decoded to MOTION_GRID grey at
#             MOTION_DETECT_FPS frames a second (_motion_detector)
#   buffer:   its main stream, copied unchanged into MPEG-TS and held in
#             memory from a keyframe at least MOTION_PREROLL_S old
#             (_motion_buffer, _TsBuffer)
# On motion, a writer ffmpeg receives the buffer and then the live packets
# (_start_recording), so every file starts before the motion did.
MOTION_PREROLL_S = 3.0          # every recording starts at least this early
MOTION_DETECT_FPS = 4
MOTION_BUF_MAX_S = 20.0         # memory bound if keyframes are far apart
# 3.9.0 (C30): a recording right after a headlight pause starts before the
# pause, so the buffer keeps that much more: 3.9.0-rc2.0, with the 12 s pause
# and the 1.25 s look, 17.25 s (about 17 MB at 8 Mbit/s per armed camera).
MOTION_BUF_KEEP_S = MOTION_PREROLL_S + LIGHT_PAUSE_TOTAL_S + 1.0 + MOTION_HOLD_S   # 3.9.0-rc2.0
MOTION_PIPE_RETRY_S = 10.0
# 3.0.1 (B20): a stream that keeps failing (the DVR's sub-stream answered
# 404 1,970 times in 86 minutes) is retried at 10 s, 20 s, 40 s ... up to
# 5 min, with one warning at the first failure and one line at recovery.
MOTION_PIPE_RETRY_MAX_S = 300.0
TS_PACKET = 188
TS_VIDEO_PID = 0x100            # -mpegts_start_pid in _motion_buffer
TS_PMT_PID = 0x1000             # ffmpeg's default PMT PID


class _TsBuffer:
    """The recording stream's MPEG-TS packets, grouped by keyframe.

    Keeps the newest keyframe that is at least MOTION_PREROLL_S old, and
    everything after it: enough to start a file before the motion, and no
    more. ffmpeg marks each video keyframe with the random-access flag in
    the adaptation field of its first packet.
    """

    def __init__(self) -> None:
        self.rest = b""
        self.gops: collections.deque = collections.deque()   # [start time, bytearray]
        self.psi: dict[int, bytes] = {}                       # latest PAT and PMT
        self.now = 0.0                                        # time of the last feed

    def feed(self, data: bytes, now: float) -> bytes:
        """Add stream bytes; return the complete packets among them."""
        data = self.rest + data
        n = len(data) // TS_PACKET * TS_PACKET
        self.rest = data[n:]
        out = data[:n]
        mv = memoryview(out)
        for i in range(0, n, TS_PACKET):
            pkt = mv[i:i + TS_PACKET]
            if pkt[0] != 0x47:
                continue
            pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
            if pid in (0, TS_PMT_PID):
                self.psi[pid] = bytes(pkt)
            elif (pid == TS_VIDEO_PID and pkt[1] & 0x40 and pkt[3] & 0x20
                    and pkt[4] > 0 and pkt[5] & 0x40):
                self.gops.append([now, bytearray()])
            if self.gops:
                self.gops[-1][1] += pkt
        self.now = now
        self._trim(now)
        return out

    def _trim(self, now: float) -> None:
        while len(self.gops) > 1 and now - self.gops[1][0] >= MOTION_BUF_KEEP_S:
            self.gops.popleft()
        while len(self.gops) > 1 and now - self.gops[0][0] > MOTION_BUF_MAX_S:
            self.gops.popleft()

    def _first(self, want_s: float) -> int:
        """The newest keyframe at least want_s old, else the oldest kept."""
        for i in range(len(self.gops) - 1, -1, -1):
            if self.now - self.gops[i][0] >= want_s:
                return i
        return 0

    def preroll(self, want_s: float = MOTION_PREROLL_S) -> bytes:
        """PAT, PMT, then everything from the keyframe at least want_s old on."""
        if not self.gops:
            return b""
        head = b"".join(self.psi[pid] for pid in (0, TS_PMT_PID) if pid in self.psi)
        return head + b"".join(bytes(g[1]) for g in list(self.gops)[self._first(want_s):])

    def preroll_seconds(self, now: float, want_s: float = MOTION_PREROLL_S) -> float:
        return now - self.gops[self._first(want_s)][0] if self.gops else 0.0


def _motion_detect_source(camera: dict) -> str | None:
    """The stream detection decodes: the smallest RTSP stream, any codec.

    Like _go2rtc_card_source, but MJPEG is fine here (ffmpeg decodes it).
    None when only a stream wider than CARD_MAX_WIDTH is known: decoding a
    4K stream continuously would load the Pi, so that camera keeps the
    snapshot path.
    """
    if camera.get("display") in ("webrtc", "wsrtsp", "info", "appliance"):
        return None
    best: tuple[int, str] | None = None
    for prof in _go2rtc_profiles(camera):
        raw = prof.get("url") or camera.get(prof.get("_url_key", "stream_url"))
        if not raw or not raw.lower().startswith(("rtsp://", "rtsps://")):
            continue
        url = build_authenticated_url(camera, url=raw)
        if not url:
            continue
        width = prof.get("stream_width") or 0
        if best is None or (width and (not best[0] or width < best[0])):
            best = (width, url)
    if best is not None and best[0] > CARD_MAX_WIDTH:
        sub = _dahua_sub_stream(camera.get("stream_url") or "")
        return build_authenticated_url(camera, url=sub) if sub else None
    return best[1] if best else None


def _motion_record_source(camera: dict) -> str | None:
    """The stream recordings copy: the main stream, full quality."""
    raw = camera.get("stream_url") or ""
    if not raw.lower().startswith(("rtsp://", "rtsps://")):
        return None
    return build_authenticated_url(camera)


async def _motion_throttle(camera: dict) -> None:
    """Respect the brand's per-IP connection cooldown (Microseven)."""
    secs = _brand_throttle_seconds(camera)
    if secs > 0:
        await _throttle_wait_if_needed(camera.get("ip", ""), secs, "motion")


async def _motion_detector(camera_id: str, url: str) -> None:
    """Watch the camera's small stream; feed each frame to _motion_feed."""
    ms = _motion_state(camera_id)
    w, h = _motion_grid(camera_id)     # 3.4.0: 128 x 96 with zones
    luma = w * h
    # 2.6.7: yuv420p, so each frame also carries its colour planes (a
    # quarter of the luma size each); night mode is read from those.
    size = luma + 2 * (luma // 4)
    delay, fails = MOTION_PIPE_RETRY_S, 0
    while _motion_armed(camera_id):
        await _motion_throttle(CAMERAS.get(camera_id) or {})
        proc = None
        frames = 0
        src = await _go2rtc_relay(camera_id, url)     # 3.3.0 (C4)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-loglevel", "error",
                "-rtsp_transport", "tcp", "-timeout", "8000000",
                "-i", src, "-an",
                "-vf", f"fps={MOTION_DETECT_FPS},scale={w}:{h}:flags=area,format=yuv420p",
                "-f", "rawvideo", "pipe:1",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            err_t = asyncio.create_task(_drain_stderr(proc, f"DET:{camera_id}"))
            frames = 0
            try:
                while _motion_armed(camera_id):
                    px = await asyncio.wait_for(proc.stdout.readexactly(size), timeout=30)
                    frames += 1
                    if frames == 1:
                        ms["detector_live"] = True
                        _motion_reset_prev(camera_id)
                        if fails:
                            log.info(f"Motion [{camera_id}]: live detection back after "
                                     f"{fails} failed start(s)")
                        delay, fails = MOTION_PIPE_RETRY_S, 0
                        log.info(f"Motion [{camera_id}]: watching the live stream, "
                                 f"{MOTION_DETECT_FPS} frames a second")
                    now_m = time.monotonic()
                    # 3.9.0-rc2.0 (B85): colour of every picture; night mode once a second
                    _motion_colour(camera_id, _motion_chroma(px[luma:]), now_m, frames)
                    _motion_feed(camera_id, _motion_thumb_gray(px[:luma]), now_m,
                                 stream_t=frames / MOTION_DETECT_FPS)
            finally:
                err_t.cancel()
        except asyncio.IncompleteReadError:
            pass
        except asyncio.TimeoutError:
            log.warning(f"Motion [{camera_id}]: no detection frame in 30 s")
        except OSError as ex:
            log.warning(f"Motion [{camera_id}]: could not start detection ffmpeg: {ex}")
        finally:
            ms["detector_live"] = False
            _go2rtc_relay_result(camera_id, src, frames > 0)
            _motion_reset_prev(camera_id)      # 2.6.7: the stream clock ends here
            if proc is not None:
                await _stop_proc(proc, exited_grace=0.5)
        if _motion_armed(camera_id):
            fails += 1
            if fails == 1:
                log.warning(f"Motion [{camera_id}]: live detection stopped — snapshots "
                            f"meanwhile, retrying in {delay:.0f} s, then less often")
            else:
                log.debug(f"Motion [{camera_id}]: live detection retry {fails} "
                          f"in {delay:.0f} s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, MOTION_PIPE_RETRY_MAX_S)


async def _motion_buffer(camera_id: str, url: str) -> None:
    """Hold the main stream's last few seconds; feed an active recording."""
    ms = _motion_state(camera_id)
    delay, fails = MOTION_PIPE_RETRY_S, 0
    while _motion_armed(camera_id):
        await _motion_throttle(CAMERAS.get(camera_id) or {})
        proc = None
        buf = _TsBuffer()
        src = await _go2rtc_relay(camera_id, url)     # 3.3.0 (C4)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-loglevel", "error",
                "-rtsp_transport", "tcp", "-timeout", "8000000",
                "-fflags", "+genpts", "-i", src,
                "-map", "0:v:0", "-map", "0:a:0?",
                # Video copied unchanged. Audio to AAC: DVRs often send G.711,
                # which neither MPEG-TS nor MP4 can carry.
                "-c:v", "copy", "-c:a", "aac", "-b:a", "64k",
                "-f", "mpegts", "-mpegts_start_pid", str(TS_VIDEO_PID), "pipe:1",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            err_t = asyncio.create_task(_drain_stderr(proc, f"BUF:{camera_id}"))
            ms["tsbuf"] = buf
            try:
                while _motion_armed(camera_id):
                    data = await asyncio.wait_for(proc.stdout.read(65536), timeout=30)
                    if not data:
                        break
                    pkts = buf.feed(data, time.monotonic())
                    if not ms.get("buffer_live") and buf.gops:
                        ms["buffer_live"] = True
                        if fails:
                            log.info(f"Motion [{camera_id}]: recording stream back after "
                                     f"{fails} failed start(s)")
                        delay, fails = MOTION_PIPE_RETRY_S, 0
                        log.info(f"Motion [{camera_id}]: holding the last "
                                 f"{MOTION_PREROLL_S:.0f} s of the main stream for recordings")
                    queue = ms.get("rec_queue")
                    if queue is not None:
                        queue += pkts
                    elif (writer := ms.get("writer")) is not None and pkts:
                        await _motion_write(camera_id, ms, writer, pkts)
            finally:
                err_t.cancel()
        except asyncio.TimeoutError:
            log.warning(f"Motion [{camera_id}]: no recording-stream data in 30 s")
        except OSError as ex:
            log.warning(f"Motion [{camera_id}]: could not start buffer ffmpeg: {ex}")
        finally:
            _go2rtc_relay_result(camera_id, src, bool(buf.gops))
            ms["buffer_live"] = False
            ms["tsbuf"] = None
            if proc is not None:
                await _stop_proc(proc, exited_grace=0.5)
        if _motion_armed(camera_id):
            fails += 1
            if fails == 1:
                log.warning(f"Motion [{camera_id}]: recording stream stopped — retrying "
                            f"in {delay:.0f} s, then less often")
            else:
                log.debug(f"Motion [{camera_id}]: recording stream retry {fails} "
                          f"in {delay:.0f} s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, MOTION_PIPE_RETRY_MAX_S)


async def _motion_write(camera_id: str, ms: dict, writer: asyncio.subprocess.Process,
                        pkts: bytes) -> None:
    """Pass packets to the recording writer; drop it if it has died."""
    try:
        writer.stdin.write(pkts)
        await asyncio.wait_for(writer.stdin.drain(), timeout=5)
    except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError) as ex:
        log.warning(f"Motion [{camera_id}]: recording writer stopped taking data ({ex!r})")
        if ms.get("writer") is writer:
            ms["writer"] = None


def _motion_ensure_pipelines(camera_id: str) -> None:
    """Start an armed camera's detector and buffer tasks if not running."""
    ms = _motion_state(camera_id)
    camera = CAMERAS.get(camera_id)
    if not camera:
        return
    for key, source, factory in (
            ("det_task", _motion_detect_source, _motion_detector),
            ("buf_task", _motion_record_source, _motion_buffer)):
        task = ms.get(key)
        if task and not task.done():
            continue
        url = source(camera)
        if url:
            ms[key] = asyncio.create_task(factory(camera_id, url))


async def _motion_stop_pipelines(camera_id: str) -> None:
    """Disarmed or shutting down: stop the detector and buffer."""
    ms = _MOTION.get(camera_id) or {}
    tasks = [t for t in (ms.pop("det_task", None), ms.pop("buf_task", None))
             if t and not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
# ── 2.6.7: night boost (build plan C15) ────────────────────────────────────
# CrystalHeeler, 2026-10-01: at night under IR a distant walker changed 0.6-0.7% of
# the picture for 6-8 s, under the 1% that even sensitivity 100 needs.
# When a camera's picture turns black-and-white (IR), its setting shifts up
# by MOTION_NIGHT_BOOST, past 100 if need be, down to MOTION_AREA_FLOOR; in
# daytime the slider means exactly what it says. The colour check runs all
# the time (a storm can switch a camera to IR at 3 pm). Home Assistant's
# home location gives sunrise and sunset, so a camera that does not switch
# within NIGHT_WINDOW_S of either is reported: log, cog panel, and a Home
# Assistant notification.
MOTION_NIGHT_BOOST = 15
MOTION_AREA_FLOOR = 0.4        # % of the picture: about 4x the still-night median
NIGHT_CHROMA_MAX = 2.5         # colour at or under this: black-and-white
DAY_CHROMA_MIN = 5.0           # colour at or over this: day; between: no change
NIGHT_HOLD_S = 30              # a new mode must hold this long
NIGHT_WINDOW_S = 3600          # +/- around sunrise and sunset
NIGHT_GAP_S = 120              # unseen longer than this: not watched through
_HA_LOCATION: dict = {}        # latitude, longitude from Home Assistant
_HA_LOC_STATE = {"next": 0.0, "mismatch": False}   # when to ask again; C16
# 2.6.8 (C16). CrystalHeeler, 2026-10-02: Home Assistant still had its default
# location, Amsterdam, with a time zone 7 hours away, so AnyCam reported "still
# in night mode an hour after sunrise (00:42)". A time zone covers about
# 15 degrees of longitude for each hour from UTC; a location further than
# this from its time zone's longitude is wrong (3.5 h allows for China).
HA_LOC_MAX_LON_DIFF = 52.5
HA_LOC_REFRESH_S = 6 * 3600
HA_LOC_NOTE = ("Home Assistant's home location does not match its time zone. Set "
               "the location (Settings, System, General) to get the sunrise and "
               "sunset check.")
_NIGHT_CHECKED: dict = {}      # (camera, kind, event time) -> event time, checked


def _motion_boost(camera_id: str) -> int:
    ms = _MOTION.get(camera_id) or {}
    return MOTION_NIGHT_BOOST if ms.get("night") else 0


def _motion_area_now(camera_id: str) -> float:
    """The threshold in force: the camera's setting, plus the night boost."""
    return _motion_area_pct(_motion_cfg(camera_id)["level"] + _motion_boost(camera_id))


def _motion_chroma(uv: bytes) -> float:
    """Mean distance of the colour planes from neutral (128): 0 = grey."""
    return sum(abs(v - 128) for v in uv) / max(len(uv), 1)


def _motion_jpeg_chroma(jpeg: bytes) -> float | None:
    """The same colour measure for a JPEG (snapshot path)."""
    try:
        img = Image.open(io.BytesIO(jpeg))
        img.draft("YCbCr", (64, 48))
        ycc = img.convert("YCbCr").resize((32, 24), Image.Resampling.BOX)
        _, cb, cr = ycc.split()
        return _motion_chroma(cb.tobytes() + cr.tobytes())
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def _motion_night_observe(camera_id: str, chroma: float, now_m: float) -> None:
    """Track day or night from the picture's colour, with NIGHT_HOLD_S hysteresis."""
    ms = _MOTION.get(camera_id)
    if not ms:
        return
    ms["chroma"] = chroma
    now = time.time()
    if now - ms.get("observed_last", 0) > NIGHT_GAP_S:
        ms["observed_since"] = now      # a gap: the window must be watched again
    ms["observed_last"] = now
    night = bool(ms.get("night"))
    want = True if chroma <= NIGHT_CHROMA_MAX else False if chroma >= DAY_CHROMA_MIN else None
    if want is None or want == night:
        ms["night_cand_t"] = None
        return
    if ms.get("night_cand") is not want or not ms.get("night_cand_t"):
        ms["night_cand"], ms["night_cand_t"] = want, now_m
        return
    if now_m - ms["night_cand_t"] < NIGHT_HOLD_S:
        return
    ms["night"], ms["night_cand_t"] = want, None
    ms["night_switched_at"] = time.time()
    ms["night_note"] = None
    level = _motion_cfg(camera_id)["level"]
    when = ("" if not _HA_LOCATION or _near_sun_event(time.time())
            else " — outside the usual time (dark weather, or lights)")
    if want:
        log.info(f"Motion [{camera_id}]: night (IR, black-and-white) — sensitivity "
                 f"{level} → {level + MOTION_NIGHT_BOOST} "
                 f"({_motion_area_now(camera_id):.2f}% of the picture){when}")
    else:
        log.info(f"Motion [{camera_id}]: day (colour) — sensitivity back to {level}{when}")


def _sun_events_utc(lat: float, lon: float, day: "datetime.date") -> dict:
    """Sunrise and sunset for one UTC date, as epoch seconds (None if none).

    The almanac algorithm (US Naval Observatory, "Almanac for Computers",
    1990), accurate to a minute or two; zenith 90.833 degrees (refraction and
    the sun's radius). Standard library only.
    """
    import math
    n = day.timetuple().tm_yday
    lng_hour = lon / 15.0
    out = {}
    for kind, hour in (("sunrise", 6), ("sunset", 18)):
        t = n + (hour - lng_hour) / 24
        m = 0.9856 * t - 3.289
        sl = (m + 1.916 * math.sin(math.radians(m)) + 0.020 * math.sin(math.radians(2 * m))
              + 282.634) % 360
        ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(sl)))) % 360
        ra = (ra + (math.floor(sl / 90) * 90 - math.floor(ra / 90) * 90)) / 15
        sin_dec = 0.39782 * math.sin(math.radians(sl))
        cos_dec = math.cos(math.asin(sin_dec))
        cos_h = ((math.cos(math.radians(90.833)) - sin_dec * math.sin(math.radians(lat)))
                 / (cos_dec * math.cos(math.radians(lat))))
        if not -1 <= cos_h <= 1:
            out[kind] = None            # polar day or night
            continue
        h = (360 - math.degrees(math.acos(cos_h))) if kind == "sunrise" else math.degrees(math.acos(cos_h))
        ut = (h / 15 + ra - 0.06571 * t - 6.622 - lng_hour) % 24
        midnight = datetime.datetime(day.year, day.month, day.day,
                                     tzinfo=datetime.timezone.utc).timestamp()
        out[kind] = midnight + ut * 3600
    return out


def _sun_events_around(now: float) -> list[tuple[str, float]]:
    """Sunrises and sunsets from yesterday to tomorrow (UTC), sorted."""
    if not _HA_LOCATION:
        return []
    today = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date()
    ev = []
    for d in (-1, 0, 1):
        for kind, ts in _sun_events_utc(_HA_LOCATION["latitude"], _HA_LOCATION["longitude"],
                                        today + datetime.timedelta(days=d)).items():
            if ts is not None:
                ev.append((kind, ts))
    return sorted(ev, key=lambda e: e[1])


def _near_sun_event(now: float) -> tuple[str, float] | None:
    """The sunrise or sunset within NIGHT_WINDOW_S of now, if any."""
    return next(((k, ts) for k, ts in _sun_events_around(now)
                 if abs(now - ts) <= NIGHT_WINDOW_S), None)


async def _night_expectation_check(camera_id: str, ms: dict, now: float) -> None:
    """At the end of each window, report a camera that did not switch.

    Decided once per camera and event, at the first keeper pass after the
    window ends, and only for a camera watched all through the window.
    """
    for key in [k for k, ts in _NIGHT_CHECKED.items() if now - ts > 2 * 86400]:
        del _NIGHT_CHECKED[key]
    for kind, ts in _sun_events_around(now):
        end = ts + NIGHT_WINDOW_S
        key = (camera_id, kind, int(ts))
        if not end <= now < end + NIGHT_WINDOW_S or key in _NIGHT_CHECKED:
            continue
        _NIGHT_CHECKED[key] = ts
        if (ms.get("observed_since", now) > ts - NIGHT_WINDOW_S
                or now - ms.get("observed_last", 0) > NIGHT_GAP_S):
            continue
        expect_night = kind == "sunset"
        if bool(ms.get("night")) == expect_night:
            ms["night_note"] = None
            continue
        at = time.strftime("%H:%M", time.localtime(ts))
        if expect_night:
            msg = (f"No switch to night mode (IR) around sunset ({at}). The area may be "
                   f"lit, the camera may keep colour at night, or something is wrong. "
                   f"Night boost stays off.")
        else:
            msg = (f"Still in night mode (IR) an hour after sunrise ({at}). The camera "
                   f"may be forced to black-and-white, or something is wrong. Night "
                   f"boost stays on until it shows colour.")
        ms["night_note"] = msg
        name = (CAMERAS.get(camera_id) or {}).get("name") or camera_id
        log.warning(f"Motion [{camera_id}]: {msg}")
        await _ha_notify(f"AnyCam: {name}", msg, f"anycam_night_{camera_id}_{kind}")


async def _ha_api(method: str, path: str, payload: dict | None = None) -> dict | list | None:
    """Call Home Assistant's REST API through the Supervisor (homeassistant_api)."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        return None
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, f"http://supervisor/core/api/{path}",
                                   headers={"Authorization": f"Bearer {token}"},
                                   json=payload) as resp:
            if resp.status != 200:
                log.warning(f"Home Assistant API {path}: HTTP {resp.status}")
                return None
            return await resp.json(content_type=None)


def _tz_std_offset_h(tz_name: str | None) -> float:
    """The time zone's standard (winter) offset from UTC, in hours."""
    try:
        import zoneinfo
        now = datetime.datetime.now(zoneinfo.ZoneInfo(tz_name))
        return (now.utcoffset() - (now.dst() or datetime.timedelta())).total_seconds() / 3600
    except Exception:        # no name, an unknown name, or no time zone data
        return -time.timezone / 3600      # the add-on's own zone, set by the Supervisor


def _ha_location_plausible(lon: float, tz_name: str | None) -> bool:
    """False when the longitude is too far from the time zone's own."""
    expected = 15.0 * _tz_std_offset_h(tz_name)
    return abs((lon - expected + 180) % 360 - 180) <= HA_LOC_MAX_LON_DIFF


async def _ha_location_refresh() -> None:
    """Fetch Home Assistant's home location; retry every 5 min, refresh every 6 h."""
    if time.monotonic() < _HA_LOC_STATE["next"]:
        return
    _HA_LOC_STATE["next"] = time.monotonic() + 300
    try:
        cfg = await _ha_api("GET", "config")
    except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
        log.debug(f"Night: Home Assistant location not available yet: {ex}")
        return
    if not cfg or cfg.get("latitude") is None or cfg.get("longitude") is None:
        return
    _HA_LOC_STATE["next"] = time.monotonic() + HA_LOC_REFRESH_S
    lat, lon = float(cfg["latitude"]), float(cfg["longitude"])
    if not _ha_location_plausible(lon, cfg.get("time_zone")):
        if not _HA_LOC_STATE["mismatch"]:
            log.warning(f"Night: Home Assistant's home location ({lat:.1f}, {lon:.1f}) does "
                        f"not match its time zone ({cfg.get('time_zone')}) — the sunrise "
                        f"and sunset check is off. Set the location in Settings, System, "
                        f"General. Night boost itself is not affected.")
        _HA_LOC_STATE["mismatch"] = True
        _HA_LOCATION.clear()
        return
    first = not _HA_LOCATION
    _HA_LOC_STATE["mismatch"] = False
    _HA_LOCATION.update(latitude=lat, longitude=lon)
    if first:
        ev = [f"{k} {time.strftime('%H:%M', time.localtime(ts))}"
              for k, ts in _sun_events_around(time.time())
              if abs(ts - time.time()) < 86400]
        log.info("Night: Home Assistant location received; next " + ", ".join(ev[:4]))


async def _ha_notify(title: str, message: str, notification_id: str) -> None:
    """A persistent notification in Home Assistant."""
    try:
        await _ha_api("POST", "services/persistent_notification/create",
                      {"title": title, "message": message,
                       "notification_id": notification_id})
    except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
        log.warning(f"Home Assistant notification failed: {ex}")


def _motion_uses_snapshots(camera_id: str) -> bool:
    """Armed, and the snapshot path is its detector right now."""
    ms = _MOTION.get(camera_id)
    return bool(ms and ms["enabled"] and not ms.get("detector_live"))


async def _motion_keeper() -> None:
    """Every MOTION_KEEPER_S: keep armed cameras watched, end recordings."""
    while True:
        await asyncio.sleep(MOTION_KEEPER_S)
        await _ha_location_refresh()
        now_m = time.monotonic()
        for camera_id, ms in list(_MOTION.items()):
            try:
                proc = ms.get("proc")
                if ms["recording"] and proc is not None and proc.returncode is not None:
                    log.warning(f"Motion [{camera_id}]: recording ffmpeg exited "
                                f"(rc={proc.returncode}) → {ms.get('clip_path')}")
                    _motion_finish_files(camera_id, ms)
                    await _rec_zones_save_if_dirty(ms)
                    ms["recording"] = False
                    ms["proc"] = None
                elif ms["recording"] and (not ms["enabled"] or _motion_quiet(camera_id, ms, now_m)):
                    await _stop_recording(camera_id)
                if ms["enabled"]:
                    _motion_ensure_pipelines(camera_id)
                    if not ms.get("detector_live"):
                        _motion_ensure_loop(camera_id)
                    _motion_report_peak(camera_id, ms, now_m)
                    if ms.get("cal"):
                        _cal_flush(camera_id, ms, now_m)   # 3.8.2: checks may have stopped
                    await _night_expectation_check(camera_id, ms, time.time())
                elif ms.get("det_task") or ms.get("buf_task"):
                    await _motion_stop_pipelines(camera_id)
            except Exception as ex:
                log.warning(f"Motion [{camera_id}]: keeper error: {ex}")


def _motion_load() -> None:
    """Re-arm the cameras that were armed before the last restart."""
    if not MOTION_FILE.exists():
        return
    try:
        armed = json.loads(MOTION_FILE.read_text(encoding="utf-8")).get("armed", [])
    except (OSError, ValueError) as ex:
        log.warning(f"Motion: could not read {MOTION_FILE}: {ex}")
        return
    for camera_id in armed:
        if camera_id in CAMERAS:
            _motion_state(camera_id)["enabled"] = True
            log.info(f"Motion [{camera_id}]: armed (restored)")
    try:
        saved = json.loads(MOTION_FILE.read_text(encoding="utf-8")).get("cameras", {})
    except (OSError, ValueError):
        saved = {}
    for camera_id, raw in (saved.items() if isinstance(saved, dict) else []):
        clean, errors = _motion_validate({**MOTION_DEFAULTS, **(raw or {})})
        if camera_id in CAMERAS and not errors:
            _MOTION_CFG[camera_id] = {k: v for k, v in clean.items()
                                      if v != MOTION_DEFAULTS[k]}
    # 3.4.0 (C17, answer 17): zones in the same file
    try:
        zones = json.loads(MOTION_FILE.read_text(encoding="utf-8")).get("zones", {})
    except (OSError, ValueError):
        zones = {}
    for camera_id, raw in (zones.items() if isinstance(zones, dict) else []):
        clean, errors = anycam_zones.validate(raw)
        if camera_id in CAMERAS and not errors:
            anycam_zones.set_zones(camera_id, clean)
        elif errors:
            log.warning(f"Motion [{camera_id}]: saved zones not used: {'; '.join(errors)}")
    anycam_zones.REC_ZONES.update(_rec_zones_read())
    if CFG_MOTION_GLOBAL:
        log.info("Motion: global recording settings are on — they apply to "
                 "every camera")


def _motion_save() -> None:
    armed = sorted(cid for cid, ms in _MOTION.items() if ms["enabled"])
    DATA_DIR.mkdir(exist_ok=True)
    MOTION_FILE.write_text(json.dumps({"armed": armed, "cameras": _MOTION_CFG,
                                       "zones": anycam_zones.ZONES}),
                           encoding="utf-8")
    # 3.8.3 (C27): and a copy under the recordings folder, which a reinstall keeps
    try:
        path = anycam_backup.write_copy(_settings_copy_dir(), _settings_entries(), CURRENT_VERSION)
    except OSError as ex:
        log.warning(f"Motion: could not write a copy of the motion and zone settings: {ex}")
    else:
        if path:
            log.debug(f"Motion: settings copy written: {path.name}")


# ── 3.8.3 (C27): save and restore the motion and zone settings ──────────────
SETTINGS_COPY_DIR: Path | None = None     # None: MEDIA_DIR/settings (the tests set it)


def _settings_copy_dir() -> Path:
    return SETTINGS_COPY_DIR or (MEDIA_DIR / anycam_backup.COPY_FOLDER)


def _settings_entry(camera_id: str, cam: dict) -> dict | None:
    """One camera in a copy; None when it has nothing to keep (defaults, no zones, not armed)."""
    custom = dict(_MOTION_CFG.get(camera_id) or {})
    zones = anycam_zones.ZONES.get(camera_id)
    armed = bool((_MOTION.get(camera_id) or {}).get("enabled"))
    if not (custom or zones or armed):
        return None
    return {"key": _card_key(cam), "id": camera_id,
            "mac": anycam_backup.normal_mac(cam.get("mac_addr")),
            "channel": str(cam.get("channel") or ""), "name": cam.get("name") or "",
            "brand": cam.get("manufacturer") or "",
            "width": cam.get("stream_width"), "height": cam.get("stream_height"),
            "settings": custom, "zones": anycam_zones.zone_cfg(camera_id) if zones else None,
            "armed": armed}


def _settings_entries() -> list[dict]:
    entries = [_settings_entry(cid, cam) for cid, cam in list(CAMERAS.items())]
    return sorted((e for e in entries if e), key=lambda e: e["key"])


async def api_motion_copy(request: web.Request) -> web.Response:
    """GET /api/motion/copy — a copy of every camera's motion and zone settings, to download."""
    now = datetime.datetime.now()
    body = json.dumps(anycam_backup.build_copy(_settings_entries(), CURRENT_VERSION, now), indent=1)
    log.info("Motion: settings copy downloaded")
    return web.Response(text=body, content_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="{anycam_backup.copy_name(now)}"'})


def _restore_copies(data: dict) -> tuple[list[dict], str, str | None]:
    """The copies the restore reads: (copies, where, error).

    data["source"]: "default" (the copy folder), "pi" with data["folder"], or
    "device" with data["copies"] = [{"name", "text"}] read by the page.
    """
    source = data.get("source") or "default"
    if source == "default":
        folder = _settings_copy_dir()
        return anycam_backup.read_folder(folder), str(folder), None
    if source == "pi":
        folder = anycam_backup.pi_folder(data.get("folder"))
        if folder is None:
            return [], "", ("The folder must be a folder under "
                            + " or ".join(anycam_backup.RESTORE_ROOTS))
        return anycam_backup.read_folder(folder), str(folder), None
    if source == "device":
        sent = data.get("copies")
        if not isinstance(sent, list) or len(sent) > anycam_backup.COPY_READ_MAX:
            return [], "", "copies must be a list"
        copies = [c for c in (anycam_backup.parse_copy(f.get("text"), str(f.get("name") or "")[:120])
                              for f in sent if isinstance(f, dict)) if c]
        return copies, "the folder on this device", None
    return [], "", "Unknown source"


def _restore_cameras() -> list[dict]:
    return [{"id": cid, "key": _card_key(cam), "mac": cam.get("mac_addr"),
             "channel": cam.get("channel"), "name": cam.get("name") or cid,
             "width": cam.get("stream_width"), "height": cam.get("stream_height")}
            for cid, cam in list(CAMERAS.items())]


async def _restore_apply(camera_id: str, entry: dict) -> list[str]:
    """Apply one camera's saved settings, zones and armed state; return the problems."""
    problems = []
    clean, errors = _motion_validate({**MOTION_DEFAULTS, **(entry.get("settings") or {})})
    if errors:
        problems.append("settings not used: " + "; ".join(errors))
    else:
        custom = {k: v for k, v in clean.items() if v != MOTION_DEFAULTS[k]}
        if custom:
            _MOTION_CFG[camera_id] = custom
        else:
            _MOTION_CFG.pop(camera_id, None)
    zclean, zerrors = (anycam_zones.validate(entry["zones"]) if entry.get("zones")
                       else ({}, []))
    if zerrors:
        problems.append("zones not used: " + "; ".join(zerrors))
    else:
        anycam_zones.set_zones(camera_id, zclean or None)
    ms = _motion_state(camera_id)
    want = bool(entry.get("armed"))
    if ms["enabled"] and not want:
        ms["enabled"] = False
        await _stop_recording(camera_id)
        await _motion_stop_pipelines(camera_id)
    elif want and not ms["enabled"]:
        ms["enabled"] = True
    await _motion_restart_detector(camera_id)     # new zones, and starts an armed camera
    if ms["enabled"]:
        _motion_ensure_loop(camera_id)
    return problems


async def api_motion_restore(request: web.Request) -> web.Response:
    """POST /api/motion/restore — find saved settings for each camera, and restore them.

    Without "apply" it only reports what it found. With "apply": [camera ids] it
    restores those cameras from the same source, saves and restarts motion.
    """
    try:
        data = await request.json()
    except ValueError:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"error": "Invalid request"}, status=400)
    copies, where, error = await asyncio.to_thread(_restore_copies, data)
    if error:
        return web.json_response({"error": error}, status=400)
    cams = _restore_cameras()
    found = anycam_backup.match(cams, copies)
    apply = data.get("apply")
    if apply is not None:
        if not isinstance(apply, list) or not all(isinstance(c, str) for c in apply):
            return web.json_response({"error": "apply must be a list of camera ids"}, status=400)
        done, problems = [], {}
        for camera_id in dict.fromkeys(apply):
            hit = found.get(camera_id)
            if not hit or camera_id not in CAMERAS:
                continue
            issues = await _restore_apply(camera_id, hit["entry"])
            done.append(camera_id)
            if issues:
                problems[camera_id] = issues
            log.info(f"Motion [{camera_id}]: settings restored from {hit['source'] or where} "
                     f"(saved {hit['saved']:%Y-%m-%d %H:%M}, matched by "
                     f"{'card' if hit['by'] == 'key' else 'MAC address'})"
                     + (f" — {'; '.join(issues)}" if issues else ""))
        if done:
            try:
                await asyncio.to_thread(_motion_save)
            except OSError as ex:
                log.warning(f"Motion: could not save {MOTION_FILE}: {ex}")
        return web.json_response({"restored": done, "problems": problems})
    rows = []
    for cam in cams:
        hit = found.get(cam["id"])
        entry = hit["entry"] if hit else {}
        rows.append({
            "id": cam["id"], "name": cam["name"], "found": bool(hit),
            "saved": hit["saved"].isoformat(timespec="minutes") if hit else None,
            "by": hit["by"] if hit else None,
            "aspect_changed": bool(hit) and anycam_backup.aspect_changed(
                entry, cam["width"], cam["height"]),
            "zones": len((entry.get("zones") or {}).get("zones") or []),
            "custom": bool(entry.get("settings")), "armed": bool(entry.get("armed")),
        })
    log.info(f"Motion: restore looked in {where}: {len(copies)} cop{'y' if len(copies) == 1 else 'ies'}, "
             f"saved settings for {len(found)} of {len(cams)} camera(s)")
    return web.json_response({"where": where, "copies": len(copies), "cameras": rows})


def _rec_zones_file() -> Path:
    return DATA_DIR / "recording_zones.json"


def _rec_zones_read() -> dict:
    try:
        data = json.loads(_rec_zones_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


async def _rec_zones_save_if_dirty(ms: dict) -> None:
    """3.9.0 (C31): write recording_zones.json when a finished recording changed it."""
    if ms.pop("rec_zones_dirty", False):
        try:
            await asyncio.to_thread(_rec_zones_save)
        except OSError as ex:
            log.warning(f"Motion: could not save {_rec_zones_file()}: {ex}")


def _rec_zones_save() -> None:
    """3.4.0 (answer 15): which zone started each recording, for the Storage tab."""
    keep = dict(list(anycam_zones.REC_ZONES.items())[-anycam_zones.REC_ZONES_MAX:])
    anycam_zones.REC_ZONES.clear()
    anycam_zones.REC_ZONES.update(keep)
    DATA_DIR.mkdir(exist_ok=True)
    _rec_zones_file().write_text(json.dumps(keep), encoding="utf-8")


def _cam_folder_name(camera: dict) -> str:
    """Derive a short filesystem-safe folder name from the camera display name.
    Uses dashes (not underscores) per naming convention."""
    import re as _re
    name = camera.get("name", "") or camera.get("ip", "unknown")
    for prefix in ("Generic IP Camera", "Generic", "Unknown Camera", "Unknown"):
        if name.startswith(prefix):
            name = name[len(prefix):].lstrip(" ()")
    name = _re.sub(r"\(\d+\.\d+\.\d+\.\d+\)", "", name)
    name = name.replace(" — ", "-").replace("—", "-")
    name = _re.sub(r"[^a-zA-Z0-9-]", "-", name)
    name = _re.sub(r"-+", "-", name).strip("-").lower()
    if not name:
        name = camera.get("ip", "camera").replace(".", "-")
    if len(name) < 4 or name in ("main", "sub", "stream"):
        ip = camera.get("ip", "")
        suffix = ip.split(".")[-1] if ip else ""
        if suffix:
            name = f"{name}-{suffix}"
    return name[:30]


def _cam_file_tag(camera: dict) -> str:
    """Short camera tag that starts each recording's name.

    2.6.7 (CrystalHeeler, 2026-10-01): the camera in the file name, kept short.
    The first real word of the camera's name, then the DVR channel or the
    last part of the IP address: LorexCH4, Hikvision33, Camera73.
    """
    import re as _re
    skip = {"generic", "unknown", "ip", "camera", "cam"}
    words = _re.findall(r"[A-Za-z0-9]+", camera.get("name") or "")
    word = next((w for w in words if w.lower() not in skip and not w.isdigit()), "Camera")
    word = (word[:1].upper() + word[1:])[:12]
    if camera.get("channel"):
        return f"{word}CH{camera['channel']}"
    ip = camera.get("ip") or ""
    return word + (ip.rsplit(".", 1)[-1] if ip.count(".") == 3 else "")


def _zone_file_tag(zone: str) -> str:
    """3.8.2: the zone that started a recording, at the front of its file name.

    CrystalHeeler, 2026-10-20, changing answer 15 of docs/Detection_Zones_Plan.md
    (which kept zones out of file names because free-text names make long names
    with unsafe characters): the name is cut to letters, digits and hyphens, at
    most 24 characters.
    """
    import re as _re
    tag = _re.sub(r"[^A-Za-z0-9-]+", "-", zone or "").strip("-")[:24].strip("-")
    return tag or "Zone"


async def _ensure_cam_dir(camera: dict, base: Path | None = None) -> Path:
    """Create and return the recording directory for a camera.

    2.6.6: base is the camera's own recording folder; MEDIA_DIR otherwise.
    """
    folder = _cam_folder_name(camera)
    path   = (base or MEDIA_DIR) / folder
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _start_recording(camera_id: str, camera: dict, url: str) -> None:
    """Start an ffmpeg recording subprocess for this camera (stream-copy, full quality).

    2.6.6: the segment muxer starts a new file every clip_s seconds
    while motion continues (build plan C12). Files are
    <camera>_<date>_<time>_part01.mp4, part02, ...: a new event gets a new
    date and time; a higher part number is a continuation. 2.6.7: <camera>
    is _cam_file_tag, for example LorexCH4. 3.8.2: a recording a zone
    started begins with that zone, <zone>_<camera>_<date>_<time>, for example
    FrontYard-Left_LorexCH7_20261020_101500.mp4. Each file ends
    on the first keyframe after the interval, so lengths are approximate.
    """
    ms = _motion_state(camera_id)
    if ms["recording"]:
        return  # already recording, or starting
    # 2.6.6: claim the slot before the first await, so a second motion
    # frame arriving meanwhile cannot start a second ffmpeg.
    ms["recording"] = True
    cfg      = _motion_cfg(camera_id)
    cam_dir  = await _ensure_cam_dir(camera, Path(cfg["path"]))
    ts       = time.strftime("%Y%m%d_%H%M%S")
    zone     = ms.get("judge_zone")
    # 3.4.0 (answer 15): the zone goes into the log and the Storage tab. 3.8.2
    # (CrystalHeeler, 2026-10-20): and at the front of the file name.
    prefix   = f"{_zone_file_tag(zone)}_" if zone is not None else ""
    base     = f"{prefix}{_cam_file_tag(camera)}_{ts}"
    clip     = cam_dir / f"{base}_part01.mp4"
    ms["clip_path"] = clip
    ms["clip_base"] = base
    ms["rec_zone"] = zone
    ms["rec_zone_list"] = [zone] if zone is not None else []     # 3.9.0 (C31)
    if _cal_on():
        _cal_recorded(ms, zone, base)                   # 3.8.2: the calibration log
    if zone is not None:
        log.info(f"Motion [{camera_id}]: recording started by zone \"{zone}\"")
        anycam_zones.REC_ZONES[base] = zone
        try:
            await asyncio.to_thread(_rec_zones_save)
        except OSError as ex:
            log.warning(f"Motion: could not save {_rec_zones_file()}: {ex}")
    seg_args = ["-f", "segment", "-segment_time", str(cfg["clip_s"]),
                "-segment_start_number", "1", "-reset_timestamps", "1",
                "-segment_format", "mp4",
                "-segment_format_options", "movflags=+faststart",
                str(cam_dir / f"{base}_part%02d.mp4")]
    # H.265 in MP4 needs the hvc1 tag for Chrome and Apple players.
    tag = (["-tag:v", "hvc1"]
           if (camera.get("stream_codec") or "").lower() in ("hevc", "h265") else [])
    buf = ms.get("tsbuf")
    if buf is not None and ms.get("buffer_live"):
        # 2.6.6: start from the buffer — the file begins at a keyframe at
        # least MOTION_PREROLL_S before the motion. Packets that arrive
        # while the writer starts are queued, then handed over in order.
        want_s = _motion_light_preroll(ms, time.monotonic())       # 3.9.0 (C30)
        held = ms.pop("held_s", 0.0)
        ms["rec_queue"] = bytearray(buf.preroll(want_s))
        pre_s = buf.preroll_seconds(time.monotonic(), want_s)
        light = (", which includes a passing light"
                 if want_s > MOTION_PREROLL_S + held + 1e-6 else "")
        log.info(f"Motion [{camera_id}]: recording started → {clip} (from "
                 f"{pre_s:.1f} s before the motion{light}; a new file every "
                 f"{cfg['clip_s']} s while motion continues)")
        try:
            writer = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-loglevel", "warning",
                "-f", "mpegts", "-i", "pipe:0", "-map", "0", "-c", "copy", *tag,
                *seg_args,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
        except OSError as ex:
            ms["rec_queue"] = None
            ms["recording"] = False
            log.warning(f"Motion [{camera_id}]: failed to start recording: {ex}")
            return
        asyncio.create_task(_drain_stderr(writer, f"REC:{camera_id}"))
        queued = ms.pop("rec_queue", None) or b""
        ms["rec_queue"] = None
        ms["proc"] = writer
        ms["writer"] = writer
        await _motion_write(camera_id, ms, writer, bytes(queued))
        return
    log.info(f"Motion [{camera_id}]: recording started → {clip} (no pre-roll: the "
             f"recording stream is not running yet; a new file every "
             f"{cfg['clip_s']} s while motion continues)")
    try:
        src = await _go2rtc_relay(camera_id, url)     # 3.3.0 (C4)
        ms["proc"] = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "warning",
            "-rtsp_transport", "tcp", "-timeout", "8000000",
            "-fflags", "+genpts",   # 2.6.6: Lorex packets arrive without timestamps
            "-i", src,
            "-c", "copy",   # stream-copy: no decode/encode — nearly zero CPU
            *tag, *seg_args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        # 2.6.5: drain stderr. An undrained pipe fills at 64 KB of warnings
        # and ffmpeg then blocks, stalling the recording.
        asyncio.create_task(_drain_stderr(ms["proc"], f"REC:{camera_id}"))
    except Exception as ex:
        ms["recording"] = False
        log.warning(f"Motion [{camera_id}]: failed to start recording: {ex}")


async def _stop_recording(camera_id: str) -> None:
    """Gracefully stop the recording ffmpeg process."""
    ms = _motion_state(camera_id)
    # 2.6.6: one stop only. The keeper and the frame path could both call
    # this for the same recording; the log showed "recording stopped" two
    # or three times per clip (build plan B16).
    if not ms["recording"] or ms.get("stopping"):
        return
    ms["stopping"] = True
    try:
        writer = ms.pop("writer", None)
        if writer is not None and writer.returncode is None:
            # 2.6.6: end of input makes ffmpeg finish the file cleanly.
            try:
                writer.stdin.close()
                await asyncio.wait_for(writer.wait(), timeout=15)
            except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError):
                try:
                    writer.kill()
                except ProcessLookupError:
                    pass
        proc = ms.get("proc")
        if proc and proc is not writer and proc.returncode is None:
            try:
                proc.terminate()   # ffmpeg finishes the current file on SIGTERM
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        _motion_finish_files(camera_id, ms)
        await _rec_zones_save_if_dirty(ms)
    finally:
        ms["recording"] = False
        ms["proc"]      = None
        ms["stopping"]  = False


async def api_motion_toggle(request: web.Request) -> web.Response:
    """POST /api/cameras/{camera_id}/motion — toggle motion detection on/off."""
    camera_id = request.match_info["camera_id"]
    camera    = CAMERAS.get(camera_id)
    if not camera:
        return web.json_response({"error": "Not found"}, status=404)
    ms = _motion_state(camera_id)
    # 2.6.5: the page sends the state the user asked for. A blind flip
    # turned "Record" on a camera the page wrongly showed as off into a
    # disarm (test system B log, 2026-09-30 07:20:45). No body keeps the old flip.
    want = None
    if request.can_read_body:
        try:
            want = (await request.json()).get("enabled")
        except (ValueError, AttributeError):
            want = None
    ms["enabled"] = (not ms["enabled"]) if want is None else bool(want)
    _motion_reset_prev(camera_id)
    if ms["enabled"]:
        _motion_ensure_pipelines(camera_id)
        _motion_ensure_loop(camera_id)      # snapshots until the live stream delivers
    else:
        await _stop_recording(camera_id)
        await _motion_stop_pipelines(camera_id)
    log.info(f"Motion [{camera_id}]: {'enabled' if ms['enabled'] else 'disabled'}")
    try:
        await asyncio.to_thread(_motion_save)
    except OSError as ex:
        log.warning(f"Motion: could not save {MOTION_FILE}: {ex}")
    return web.json_response({"motion_enabled": ms["enabled"]})


async def api_motion_all(request: web.Request) -> web.Response:
    """GET /api/motion — motion state of every armed or recording camera.

    2.6.5: the page loads this at start and every 3 s. Before, it never
    asked, so every card showed "Record" after a page load even when the
    server had the camera armed.
    """
    return web.json_response({
        cid: {"enabled": ms["enabled"], "recording": ms["recording"],
              # 3.4.0 (answer 16): the zone that started the recording
              **({"zone": ms["rec_zone"]} if ms["recording"] and ms.get("rec_zone") else {})}
        for cid, ms in _MOTION.items() if ms["enabled"] or ms["recording"]
    })


async def api_motion_status(request: web.Request) -> web.Response:
    """GET /api/cameras/{camera_id}/motion — current motion detection state."""
    camera_id = request.match_info["camera_id"]
    ms = _motion_state(camera_id)
    return web.json_response({
        "motion_enabled": ms["enabled"],
        "recording":      ms["recording"],
        "clip_path":      str(ms["clip_path"]) if ms.get("clip_path") else None,
    })
