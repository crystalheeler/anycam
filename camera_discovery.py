#!/usr/bin/env python3
"""
AnyCam — Home Assistant Add-on  v1.1.4
4-stage intelligent camera discovery:
  Stage 1 — ARP scan (live hosts only) + ONVIF/SSDP/mDNS multicast
  Stage 2 — Focused camera port scan on live hosts
  Stage 3 — Stream probing (RTSP/MJPEG/HLS/RTMP/WebRTC/WS-RTSP)
  Stage 4 — Optional broad sweep (0-10000) on unresponsive live hosts
"""

import asyncio
import base64
import concurrent.futures
import datetime
import hashlib
import ipaddress
import json
import logging
import os
import random
import re
import signal
import socket
import ssl
import struct
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
import io
import collections
from pathlib import Path
from urllib.parse import urlparse, quote

from aiohttp import web
import aiohttp
from cryptography.fernet import Fernet
from PIL import Image   # 2.6.6: pixel-comparison motion detection

# 3.0.0-rc1.0 (build plan E1, stage 1): the camera tables and the page's
# script live in their own files. anycam_modules.py lists every file.
from camera_db import CAMERA_DB, STREAM_DB
# 3.0.0-rc1.1 (E1, stage 2): motion, recording, night boost; the Storage tab.
import anycam_host
import anycam_motion
from anycam_motion import (
    _MOTION, _motion_keeper, _motion_load, _motion_on_frame,
    _motion_reset_prev, _motion_uses_snapshots, api_motion_all, api_motion_settings,
    api_motion_status, api_motion_toggle,
)
# 3.0.0-rc1.2 (E1): go2rtc.
import anycam_go2rtc
from anycam_go2rtc import (
    _go2rtc_profile_source, _go2rtc_register, _go2rtc_stream_name, _go2rtc_supervisor,
    api_go2rtc_card, handle_go2rtc_player_js, handle_go2rtc_ws,
)
# 3.0.0-rc1.3 (E1): anycam_probe.
import anycam_probe
from anycam_probe import (
    _extract_channel_from_rtsp_url, _onvif_media_url, _probe_rtsp_paths_single_socket, _rtsp_options_fingerprint,
    _validate_rtsp_urls_single_socket, find_rtsp_path, onvif_get_profiles, onvif_get_snapshot_uri,
    onvif_get_stream_uri, probe_hls, probe_hls_quick, probe_http_identity,
    probe_mjpeg_http, probe_mjpeg_quick, probe_rtmp, probe_rtsp,
    probe_rtsp_options, probe_webrtc, probe_ws_rtsp,
)
# 3.0.0-rc1.3 (E1): anycam_scan.
import anycam_scan
from anycam_scan import (
    run_port_scan, run_scan, run_verification_scan,
)
# 3.0.0-rc1.5 (E1): anycam_brand.
import anycam_brand
from anycam_brand import (
    _identify_camera_brand, load_oui_db, refresh_oui_db,
)
# 3.0.0-rc1.5 (E1): anycam_page.
import anycam_page
from anycam_page import (
    handle_index,
)
# 3.0.0-rc1.5 (E1): anycam_focus.
import anycam_focus
from anycam_focus import (
    api_go2rtc_focus, handle_focus_clear, handle_focus_set,
)
# 3.0.0-rc1.5 (E1): anycam_snap.
import anycam_snap
from anycam_snap import (
    _drain_stderr, _kill_hw_preheater, _stop_proc, api_logs,
    handle_snap_status, handle_snapshot, snap_loop,
)
import anycam_storage
from anycam_storage import (
    api_storage_delete, api_storage_download, api_storage_list, api_storage_move,
    api_storage_rename,
)

log = logging.getLogger("anycam")
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
# Log level is set from the HA Config tab via four boolean toggles:
# LOG_DEBUG, LOG_INFO, LOG_WARNING, LOG_ERROR.
# A custom filter passes only the levels that are enabled.
# Library loggers stay at WARNING regardless.
class _LevelFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self._allowed: set[int] = set()
        self._refresh()

    def _refresh(self) -> None:
        self._allowed = set()
        if os.environ.get("LOG_DEBUG",   "false").lower() == "true": self._allowed.add(logging.DEBUG)
        if os.environ.get("LOG_INFO",    "true").lower()  == "true": self._allowed.add(logging.INFO)
        if os.environ.get("LOG_WARNING", "true").lower()  == "true": self._allowed.add(logging.WARNING)
        if os.environ.get("LOG_ERROR",   "true").lower()  == "true": self._allowed.add(logging.ERROR)
        self._allowed.add(logging.CRITICAL)  # always pass CRITICAL

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno in self._allowed

_level_filter = _LevelFilter()
log.addFilter(_level_filter)


# 3.0.0-rc1.0 (build plan E4): no credentials in any log line. Call sites
# strip them where they know a URL carries them; this filter sits on the
# handlers, so it also covers what they miss: library messages, exception
# text, and lines copied from ffmpeg and go2rtc. Logs get sent for support.
_CRED_USERINFO = re.compile(r"(?<=://)[^/\s]*@")
_CRED_QUERY = re.compile(
    r"(?i)([?&;](?:user|usr|username|login|pass|pwd|passwd|password|token|auth)=)[^&\s\"'<>|]*")


def _redact(text: str) -> str:
    """Remove user:password@ and password-style query values from text."""
    if "@" in text:
        text = _CRED_USERINFO.sub("***@", text)
    if "=" in text:
        text = _CRED_QUERY.sub(r"\1***", text)
    return text


class _CredentialFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):      # bad format arguments: leave as is
            return True
        clean = _redact(message)
        if clean != message:
            record.msg, record.args = clean, None
        return True


_credential_filter = _CredentialFilter()
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_credential_filter)
log.setLevel(logging.DEBUG)   # pass all to the filter; filter decides what shows
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logging.getLogger("aiohttp.server").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)


class _DockerIPFilter(logging.Filter):
    """Suppress aiohttp access-log noise from Docker bridge and HA supervisor IPs.

    The Docker bridge (172.x.x.x) and the HA ingress proxy (127.0.0.1) make
    frequent internal requests that clutter the log with lines like:
      'GET /snapshot/cam_id 200 ...'
    We keep user-facing access log lines but drop the internal noise.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not (
            msg.startswith("172.")
            or msg.startswith('"172.')
            or " 172." in msg[:20]
        )

# ─────────────────────────────────────────────────────────────────────────────
# Paths & runtime config
# ─────────────────────────────────────────────────────────────────────────────

DATA_DIR       = Path("/data")
KEY_FILE       = DATA_DIR / "secret.key"
CAMS_FILE      = DATA_DIR / "cameras.json"
MOTION_FILE    = DATA_DIR / "motion.json"   # 2.6.5: armed cameras, kept across restarts
BLACKLIST_FILE = DATA_DIR / "blacklist.json"
RUNTIME_FILE   = DATA_DIR / "runtime.json"
OUI_CACHE_FILE  = DATA_DIR / "oui_cache.json"
FEEDBACK_FILE   = DATA_DIR / "not_camera_feedback.json"

# IEEE OUI CSV download URL (official source, ~37k entries, refreshed periodically)
OUI_CSV_URL      = "https://standards-oui.ieee.org/oui/oui.csv"
OUI_MAX_AGE_DAYS = 30  # re-download once a month

# Community verdicts endpoint — leave empty to disable sharing.
# When a community AnyCam server exists, set this URL and shared
# fingerprints will be submitted automatically.
COMMUNITY_ENDPOINT = os.environ.get("ANYCAM_COMMUNITY_URL", "")

CURRENT_VERSION = "3.0.0-rc1.5"  # must match config.yaml

INGRESS_PATH = os.environ.get("INGRESS_PATH", "").rstrip("/")
PORT         = int(os.environ.get("INGRESS_PORT", 8099))
# go2rtc removed — snap_loop connects directly to cameras

# ── HA add-on configuration options (set in the HA UI Config tab) ─────────────
# Read from env vars set by the HA supervisor from config.yaml options.
# Defaults mirror the config.yaml defaults so the server works without HA too.
CFG_LOW_FPS              = os.environ.get("LOW_FPS_MODE",   "false").lower() == "true"
CFG_SKIP_NONREF          = os.environ.get("SKIP_NONREF",    "false").lower() == "true"
CFG_LIMIT_THREADS        = os.environ.get("LIMIT_THREADS",  "false").lower() == "true"
CFG_STAGGER_POLL         = os.environ.get("STAGGER_POLLING","false").lower() == "true"
CFG_HW_DECODE            = os.environ.get("HW_DECODE",      "false").lower() == "true"
CFG_ADAPTIVE_QUALITY     = os.environ.get("ADAPTIVE_QUALITY","false").lower() == "true"
# 2.6.0-rc3.0 Items 2+3 — when ON, snap_loop in Enhanced View (or after
# a tier change) launches BOTH an SW ffmpeg (for fast first frame, ~1s)
# and an HW ffmpeg (warming up in background, ~5-6s). As soon as HW
# produces its first frame, the active proc atomically swaps from SW to
# HW. Without this, entering Enhanced View leaves the frozen card
# thumbnail visible for the full 5-10s of HW warmup.
CFG_FAST_STREAM_START    = os.environ.get("FAST_STREAM_START", "false").lower() == "true"
# 2.6.1 — when ON, _launch_snap adds the aggressive probe-reduction flags
# (-probesize 32, -analyzeduration 0, -reorder_queue_size 1) on top of the
# unconditional -fflags +nobuffer and -flags low_delay. These three cut
# connect latency but carry real risk: a tiny probesize can defeat codec
# detection on cameras that describe themselves slowly, and a 1-packet
# reorder queue removes the RTSP jitter buffer. Off by default until the
# Pi 4 / HAOS target has a field test.
CFG_LOW_LATENCY          = os.environ.get("LOW_LATENCY", "false").lower() == "true"
CFG_RECORDINGS           = os.environ.get("RECORDINGS_PATH", "/media/anycam")
# 2.6.6: 1 (least sensitive) to 100 (most), shown to the user as a plain
# scale. It maps to the share of the picture that must change, from 74%
# down to 1%, on a log curve so the steps are finer at the sensitive end.
# 63 maps to 5.0%, the default CrystalHeeler chose. Above 75% a change counts as
# light, not motion (MOTION_LIGHT_FRACTION), hence the 74% ceiling.
def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return min(hi, max(lo, int(os.environ.get(name, default))))
    except ValueError:
        return default


CFG_MOTION_LEVEL         = _env_int("MOTION_DETECT_LEVEL", 63, 1, 100)
# 2.6.6: new file every N seconds while motion continues (build plan C12).
CFG_MOTION_CLIP_S        = {"10s": 10, "20s": 20, "30s": 30, "1min": 60,
                            "2min": 120, "5min": 300}.get(
                                os.environ.get("MOTION_CLIP_LENGTH", "30s"), 30)
CFG_MOTION_COOL          = _env_int("MOTION_COOLDOWN_SECS", 5, 1, 300)
CFG_MOTION_PAD           = _env_int("MOTION_CLIP_PADDING_SECS", 3, 0, 30)
# 2.6.6: the recording settings above apply only when this is on; then they
# replace every camera's own settings (CrystalHeeler, 2026-09-30).
CFG_MOTION_GLOBAL        = os.environ.get("GLOBAL_RECORDING_SETTINGS", "false").lower() == "true"
CFG_UNRESTRICTED_BROWSER = os.environ.get("UNRESTRICTED_STORAGE_BROWSER", "false").lower() == "true"
CFG_LOG_DEBUG            = os.environ.get("LOG_DEBUG",   "false").lower() == "true"
CFG_LOG_INFO             = os.environ.get("LOG_INFO",    "true").lower()  == "true"
CFG_LOG_WARNING          = os.environ.get("LOG_WARNING", "true").lower()  == "true"
CFG_LOG_ERROR            = os.environ.get("LOG_ERROR",   "true").lower()  == "true"

MEDIA_DIR = Path(CFG_RECORDINGS)


# Hardware decoder names unavailable on this system (detected at runtime).
# When v4l2m2m reports "Could not find a valid device", the decoder name
# is added here so future stream requests skip hw decode immediately.
_HW_UNAVAILABLE: set = set()
# 2.6.6 (B10): set once _probe_hw_decoders has finished or skipped. The web
# server now starts before the probe, so a snapshot request can arrive first.
_HW_PROBED = asyncio.Event()

# 2.4.0-rc3.0: ordered list of (label, codec, ffmpeg_args) candidates
# that _probe_hw_decoders tries at startup, and that snap_loop iterates
# when selecting a hardware decoder for a given stream. Module-level so
# both the probe and snap_loop see the same identifiers — previously the
# probe defined this as a local list and snap_loop referenced
# _HW_DECODER_CANDIDATES expecting it to be a global, causing NameError
# the first time a stream tried to launch with hw_decode toggled on.
# The bug had been latent since 2.2.5 because the probe always added
# every candidate to _HW_UNAVAILABLE on systems without HW decode, so
# the for loop in snap_loop iterated over an empty list (which would
# itself NameError in CPython but apparently never fired in practice
# until rc3.0).
#
# 2.6.0-rc2.3: structure changed from (decoder_name, codec) to
# (label, codec, ffmpeg_args). Reason: rpios's ffmpeg exposes Pi 4 / 5
# HEVC HW decode through the v4l2-request stateless API, but does NOT
# expose it as a standalone decoder name. There is no `hevc_v4l2request`
# in `ffmpeg -decoders` on rpios builds. The v4l2-request HEVC path is
# reached via `-hwaccel drm -c:v hevc` instead — i.e. as a hwaccel, not
# a decoder. Earlier rcs added imaginary `hevc_v4l2request` /
# `h264_v4l2request` entries based on forum posts and never verified
# them against an actual `-decoders` listing; those entries are now gone.
#
# Verified live-decode of the Hikvision main stream (2560x1440 HEVC
# Main) on CrystalHeeler's Pi 4 with rpivid loaded: ffmpeg loads
# "Hwaccel V4L2 HEVC stateless V4; devices: /dev/media0,/dev/video19;
# buffers: src DMABuf, dst DMABuf; swfmt=rpi4_8" and decodes 6 of 9
# packets cleanly with no software fallback. That's the proof of the
# `-hwaccel drm` path. h264_v4l2m2m via bcm2835-codec at /dev/video10
# continues to handle H264 on Pi 4/5 the way it always has.
#
# Order = preference. Within each codec the rpi-specific path goes
# first, then vaapi as a fallback for amd64 builds with passthrough.
# All candidates gated on CFG_HW_DECODE — see _probe_hw_decoders for
# the toggle-respecting guard.
_HW_DECODER_CANDIDATES: list[tuple[str, str, list[str]]] = [
    ("hevc_drm",     "hevc", ["-hwaccel", "drm",   "-c:v", "hevc"]),
    ("h264_v4l2m2m", "h264", [                     "-c:v", "h264_v4l2m2m"]),
    ("hevc_vaapi",   "hevc", ["-hwaccel", "vaapi", "-c:v", "hevc"]),
    ("h264_vaapi",   "h264", ["-hwaccel", "vaapi", "-c:v", "h264"]),
]

# ── Shared thread pool for all run_in_executor calls ─────────────────────────
# Using a named, bounded pool instead of None (default) gives us:
#   1. Explicit max_workers cap — prevents unbounded thread creation on scan
#   2. Named threads for easier debugging (anycam-N in stack traces)
#   3. Clean shutdown lifecycle via _THREAD_POOL.shutdown()
# 12 workers: above the Pi 4 default (8) but appropriate for I/O-bound probes.
_THREAD_POOL: concurrent.futures.ThreadPoolExecutor = (
    concurrent.futures.ThreadPoolExecutor(
        max_workers=12,
        thread_name_prefix="anycam",
    )
)

# Circular log buffer — last 200 WARNING/ERROR entries for the status dot.
# Structure: [{"level": "warning"|"error", "msg": str, "t": float}, ...]
_LOG_BUFFER: list = []

class _BufHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:

        if record.levelno >= logging.WARNING:
            _LOG_BUFFER.append({
                "level": "error" if record.levelno >= logging.ERROR else "warning",
                "msg":   self.format(record),
                "t":     record.created,
            })
            if len(_LOG_BUFFER) > 200:
                _LOG_BUFFER.pop(0)

_buf_handler = _BufHandler()
_buf_handler.addFilter(_credential_filter)      # E4: the page's log panel too
_buf_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                             datefmt="%H:%M:%S"))
logging.getLogger().addHandler(_buf_handler)

# Per-camera snapshot state for the background ffmpeg processes that feed
# handle_snapshot.  Key = camera_id.
# Each value dict: frame(bytes|None), frame_time(float), frame_count(int),
#                  proc(Process|None), task(Task|None), restart_count(int)
_SNAP: dict = {}

# Timestamp of most recent handle_snapshot call per camera.
# snap_loop uses this to detect idle (>30s) and stop automatically.
_snap_last_access: dict = {}


# ─── 2.3.0: Throttle-aware probe pacing (Hipcam family rate-limit) ───────
#
# Some camera firmware (Hipcam RealServer family — Microseven, Sricam,
# Vstarcam, Wansview-old, Tenvis) rejects multiple TCP opens from the
# same source IP within a short window (~5s). Sustained violation
# escalates to firmware-level RTSP lockout requiring power-cycle.
# CAMERA_DB has throttle metadata already — this infra USES it at runtime.
#
# Strategy: cross-sequence runtime tracker keyed by IP. Any code path
# that's about to open a new TCP socket against a throttled IP first
# calls _throttle_wait_if_needed(ip, throttle_s), which sleeps the
# remainder of the cooldown window before returning. The 2.3.0 cred-auth
# refactor moves probing onto a single TCP socket per camera per phase,
# which mostly eliminates the issue at the source — but ffprobe and
# snap_loop ffmpeg restarts STILL open their own sockets, so the tracker
# remains as a runtime safety net.
_THROTTLE_TRACK: dict = {}   # ip → last RTSP TCP-open timestamp

# 2.4.0-rc3.5 Aggressive Cooldown Detection (ACD).
# Defense-in-depth on top of brand-static throttle data: when a TCP
# probe to an IP fails with a RST or broken pipe, record the timestamp.
# If 2+ such failures land in a 60-second window, we infer the camera's
# real-world cooldown is stricter than what CAMERA_DB documents (or the
# camera is in an escalated firmware lockout from prior pressure), and
# we escalate the per-IP cooldown to a fixed 30s for the next 5 minutes.
# This is the safety net the original Throttle-Aware Probe Pacing Plan
# suggested but didn't ship — added here because the rc3.4 Microseven
# field test surfaced a leak (alt-port Layer 1 walks) that the static
# pacing didn't catch in time.
#
# Records are pruned on every observation so this dict stays tiny in
# practice (typically empty; one entry per actively-misbehaving IP for
# the duration of the cooldown).
_RST_OBSERVED:  dict = {}    # ip → list[timestamp] of recent RSTs/RST-likes
_ACD_ESCALATED: dict = {}    # ip → timestamp until which 30s cooldown applies

ACD_RST_WINDOW_S       = 60.0    # observation window
ACD_RST_THRESHOLD      = 2       # RSTs within window to trigger escalation
ACD_ESCALATED_COOLDOWN = 30.0    # cooldown applied while escalated
ACD_ESCALATION_TTL_S   = 300.0   # how long an escalation lasts


def _record_rst_observation(ip: str) -> None:
    """Record a RST/broken-pipe observation for `ip`. Prunes entries
    older than ACD_RST_WINDOW_S. If the remaining count meets the
    threshold, sets _ACD_ESCALATED[ip] for ACD_ESCALATION_TTL_S so
    subsequent _throttle_wait_if_needed calls apply the escalated
    cooldown. Idempotent and cheap; safe to call from sync or async
    code paths."""
    if not ip:
        return
    now = time.monotonic()
    obs = _RST_OBSERVED.get(ip, [])
    # prune
    obs = [t for t in obs if (now - t) < ACD_RST_WINDOW_S]
    obs.append(now)
    _RST_OBSERVED[ip] = obs
    if len(obs) >= ACD_RST_THRESHOLD:
        already = _ACD_ESCALATED.get(ip, 0.0) > now
        _ACD_ESCALATED[ip] = now + ACD_ESCALATION_TTL_S
        if not already:
            log.warning(
                f"  ACD: {ip} produced {len(obs)} RST/broken-pipe "
                f"events in <{ACD_RST_WINDOW_S:.0f}s — escalating per-IP "
                f"cooldown to {ACD_ESCALATED_COOLDOWN:.0f}s for "
                f"{ACD_ESCALATION_TTL_S:.0f}s")


def _acd_active(ip: str) -> bool:
    """True while the escalated cooldown is in force for this address."""
    return bool(ip) and _ACD_ESCALATED.get(ip, 0.0) > time.monotonic()


# ── 2.5.0-rc1.0: streaming_recipe consumer infrastructure ────────────
# Two helpers used by find_rtsp_path's path-list builder and by the
# single-socket walker's SDP-parsing branch when a brand entry's
# streaming_recipe directs us to walk DVR/NVR channels rather than
# the universal RTSP_PATHS list.









def _parse_throttle_seconds(amount_str: str) -> float:
    """Extract seconds from a CAMERA_DB throttle_amount string. Returns
    0.0 if no parseable value. All current rate_limit_per_ip_tcp entries
    match the '~Ns' or 'Ns' pattern — see camera_discovery.CAMERA_DB
    entries for Hipcam, Sricam, Vstarcam, Wansview, Tenvis."""
    if not amount_str:
        return 0.0
    m = re.search(r"~?\s*(\d+)\s*s", amount_str)
    return float(m.group(1)) if m else 0.0


def _brand_throttle_seconds(camera: dict) -> float:
    """Return the cooldown seconds for this camera's brand, or 0.0 if
    the camera isn't subject to a per-IP TCP rate-limit. Looks up the
    CAMERA_DB entry via _identify_camera_brand and parses throttle_amount.
    Default 5.0 for rate_limit_per_ip_tcp brands when amount fails to
    parse — the documented Hipcam window is 5s and erring on the safe
    side costs nothing."""
    if not camera:
        return 0.0
    entry = _identify_camera_brand(dict(camera))
    if not entry:
        return 0.0
    if entry.get("throttle_type") != "rate_limit_per_ip_tcp":
        return 0.0
    secs = _parse_throttle_seconds(entry.get("throttle_amount", ""))
    return secs if secs > 0 else 5.0


async def _throttle_wait_if_needed(ip: str, throttle_s: float,
                                    log_label: str = "") -> None:
    """If the IP is in cooldown, sleep until it clears. Updates the
    last-open timestamp before returning so the NEXT caller waits from
    THIS call's TCP-open moment. Cheap no-op when throttle_s <= 0 AND
    no ACD escalation is active.

    2.4.0-rc3.5: also honors _ACD_ESCALATED — if an IP has produced
    enough RSTs to trip Aggressive Cooldown Detection, we use the
    escalated cooldown (max of brand-static and ACD value) regardless
    of what throttle_s the caller passed. This means even brands with
    no documented throttle get protection if the camera is observed
    to be misbehaving."""
    now = time.monotonic()
    # ACD: if escalated, override caller-supplied throttle_s with the
    # max of (caller value, escalated value) for the duration of the
    # escalation. Once the TTL elapses, _ACD_ESCALATED entry is stale
    # and ignored (we don't actively prune; next call past the TTL
    # simply sees the escalation_until timestamp in the past).
    if ip and _ACD_ESCALATED.get(ip, 0.0) > now:
        if throttle_s < ACD_ESCALATED_COOLDOWN:
            throttle_s = ACD_ESCALATED_COOLDOWN
    if throttle_s <= 0:
        return
    last = _THROTTLE_TRACK.get(ip, 0.0)
    elapsed = now - last
    if last and elapsed < throttle_s:
        wait_s = throttle_s - elapsed
        if log_label:
            log.info(f"  Throttle wait {wait_s:.1f}s for {ip} "
                     f"(brand cooldown ~{throttle_s:.0f}s): {log_label}")
        await asyncio.sleep(wait_s)
    _THROTTLE_TRACK[ip] = time.monotonic()


# IPs/cam-ids the user has explicitly dismissed (loaded from disk)
BLACKLIST: set = set()


def _snap_state(camera_id: str) -> dict:
    """Return (and lazily create) the snapshot state dict for a camera."""
    if camera_id not in _SNAP:
        _SNAP[camera_id] = {
            "frame":             None,
            "frame_time":        0.0,
            "frame_count":       0,
            # 2.4.0-rc3.4 Bug 2 fix: per-ffmpeg-run counter (resets on each
            # subprocess launch in snap_loop). Used by handle_snapshot's
            # X-Stream-Status logic; init here so any read before the first
            # ffmpeg launch sees 0 (interpreted correctly as "no frames yet
            # this run").
            "current_run_frames": 0,
            "proc":              None,
            "task":              None,
            "restart_count":     0,
            "zero_frame_streak": 0,
        }
    return _SNAP[camera_id]

# ─────────────────────────────────────────────────────────────────────────────
# State
# ─────────────────────────────────────────────────────────────────────────────

CAMERAS    = {}
BLACKLIST  = set()
SCAN_STATE = {"running": False, "progress": 0, "message": "Idle. Click Scan to begin.",
               "stage": 0, "stage_label": "",
               "started_at": 0.0, "elapsed": 0.0, "eta": ""}
SCAN_OPTIONS = {"broad_sweep": False}
_FERNET    = None

PSCAN = {
    "running":    False, "paused": False, "ip": "",
    "progress":   0, "message": "", "results": [], "proc_pid": None,
    "live_ports": [],    # ports found so far during active scan
    "scan_start": 0.0,   # timestamp scan began
    "eta":        0,     # seconds remaining (from nmap --stats-every)
    "percent":    0.0,   # % done (from nmap)
}

PSCAN_QUEUE: list[str] = []  # IPs queued for sequential batch scan

# ─────────────────────────────────────────────────────────────────────────────
# Protocol constants
# ─────────────────────────────────────────────────────────────────────────────

CAMERA_PORTS = [
    554, 8554, 10554,
    1935, 1936,
    80, 8080, 8000, 8888,
    443, 8443,
    2020, 37777, 34567,
    8765,
]

RTSP_PATHS = [
    "/stream", "/stream1", "/stream2", "/live", "/live/ch00_0",
    "/live/main", "/h264", "/h264/ch1/main/av_stream", "/video",
    "/video1", "/cam", "/cam/realmonitor?channel=1&subtype=0",
    "/Streaming/Channels/101", "/Streaming/Channels/1",
    "/av0_0", "/av0_1", "/11", "/12", "/MediaInput/h264",
    "/ch0_unicast.sdp", "/onvif1", "/profile1/media.smp",
    "/channel1", "/mpeg4/media.amp",
    "/",   # bare root tried last — many cameras 200-OK DESCRIBE here
           # but reject SETUP because no real track lives at root
]

















# ─────────────────────────────────────────────────────────────────────────────
# OUI (MAC address) database
# ─────────────────────────────────────────────────────────────────────────────














# ─────────────────────────────────────────────────────────────────────────────
# Encryption
# ─────────────────────────────────────────────────────────────────────────────

def get_fernet() -> Fernet:
    global _FERNET
    if _FERNET:
        return _FERNET
    DATA_DIR.mkdir(exist_ok=True)
    key = KEY_FILE.read_bytes() if KEY_FILE.exists() else Fernet.generate_key()
    if not KEY_FILE.exists():
        KEY_FILE.write_bytes(key)
        KEY_FILE.chmod(0o600)
    _FERNET = Fernet(key)
    return _FERNET

def encrypt_creds(u: str, p: str) -> str:
    return get_fernet().encrypt(json.dumps({"u": u, "p": p}).encode()).decode()

def decrypt_creds(token: str) -> tuple[str, str]:
    d = json.loads(get_fernet().decrypt(token.encode()).decode())
    return d["u"], d["p"]

# ─────────────────────────────────────────────────────────────────────────────
# Persistent stores
# ─────────────────────────────────────────────────────────────────────────────

def load_cameras() -> None:

    if not CAMS_FILE.exists():
        return
    try:
        loaded = json.loads(CAMS_FILE.read_text())
        for cam in loaded:
            # Migration: remove sub_stream_url == stream_url (pointless duplicate)
            if cam.get("sub_stream_url") and cam.get("sub_stream_url") == cam.get("stream_url"):
                cam["sub_stream_url"] = None
            CAMERAS[cam["id"]] = cam
        log.info(f"Loaded {len(CAMERAS)} camera(s)")
    except Exception as e:
        log.warning(f"Load cameras: {e}")

def save_cameras() -> None:

    DATA_DIR.mkdir(exist_ok=True)
    safe = []
    for cam in CAMERAS.values():
        s = dict(cam)
        if s.get("credentials") and s.get("stream_url"):
            s["stream_url"] = _strip_creds(s["stream_url"])
        safe.append(s)
    CAMS_FILE.write_text(json.dumps(safe, indent=2))


def _publish_scan_card(cam: dict) -> None:
    """2.4.0-rc2.6: Route a scan-time-discovered card through the
    pending buffer if a scan is in progress, otherwise into CAMERAS
    directly.

    During run_scan, anycam_scan.PENDING_CAMERAS is set to a fresh dict at scan
    start. Newly-discovered cards accumulate there during the scan
    and only get flushed to CAMERAS after the dedup pass — preventing
    the UI from rendering transient duplicate cards (Microseven-as-
    4-cards, Hikvision-as-5-cards) that would later be collapsed by
    dedup. The user previously had a 40-80s window where they could
    click on cards that were about to disappear, which broke
    cred-entry flows mid-attempt.

    Outside of a scan (anycam_scan.PENDING_CAMERAS is None), this is a no-op
    pass-through: cards go into CAMERAS as before. Callers that
    aren't in run_scan (manual-add, cred-attempt, etc.) still write
    directly to CAMERAS — only the scan-time discovery sites should
    use this helper.
    """
    if anycam_scan.PENDING_CAMERAS is not None:
        anycam_scan.PENDING_CAMERAS[cam["id"]] = cam
    else:
        CAMERAS[cam["id"]] = cam


def load_blacklist() -> None:

    if not BLACKLIST_FILE.exists():
        return
    try:
        BLACKLIST.update(json.loads(BLACKLIST_FILE.read_text()))
    except Exception:
        pass

def save_blacklist() -> None:

    DATA_DIR.mkdir(exist_ok=True)
    BLACKLIST_FILE.write_text(json.dumps(list(BLACKLIST)))

# In-memory feedback store: cid → rich fingerprint record
FEEDBACK: dict = {}

def load_feedback() -> None:

    if not FEEDBACK_FILE.exists():
        return
    try:
        FEEDBACK.update(json.loads(FEEDBACK_FILE.read_text()))
        log.info(f"Loaded {len(FEEDBACK)} feedback record(s)")
    except Exception as e:
        log.warning(f"Feedback load: {e}")

def save_feedback() -> None:

    DATA_DIR.mkdir(exist_ok=True)
    FEEDBACK_FILE.write_text(json.dumps(FEEDBACK, indent=2))


def _matches_feedback_fingerprint(host: dict, port: int) -> tuple[bool, str]:
    """2.4.0-rc2.4: Check whether a candidate scan host matches any
    "Not a Camera" feedback record from past scans, conservatively.

    Returns (skip, reason). skip=True means the host should be excluded
    from the current scan because we have a high-confidence fingerprint
    match indicating the user previously rejected this device pattern.

    `host` should be a focused-scan result dict with mac_addr/mac_vendor/
    nmap_product fields populated. `port` is the open port being checked.

    Conservative match rule: skip ONLY when a stored record has
        same OUI (first 3 MAC octets) AND
        (same nmap_product OR same port) AND
        the user marked share=True OR set a specific reason_type
            (router/printer/nas/etc. — not "unknown")
    Same OUI alone is NOT enough — preserves the "TP-Link switch on .12
    + Tapo camera on .15" case (both share OUI but only the switch was
    rejected). Same OUI + same product/port indicates very likely the
    same model device on a different IP, which is the case where we
    want to suppress.

    On match, returns the reason from the stored record so the scan log
    can surface why the host was skipped — observable, not silent.
    """
    if not FEEDBACK:
        return (False, "")

    mac_addr = (host.get("mac_addr") or "").upper()
    if not mac_addr or len(mac_addr) < 8:
        return (False, "")
    candidate_oui = mac_addr[:8]  # "XX:XX:XX"
    candidate_product = (host.get("nmap_product") or "").lower()

    for cid, record in FEEDBACK.items():
        fp = record.get("fingerprint", {})
        rec_oui = (fp.get("oui") or "").upper()
        if not rec_oui or rec_oui != candidate_oui:
            continue
        rec_product = (fp.get("nmap_product") or "").lower()
        rec_port = fp.get("port")
        rec_reason = record.get("reason_type", "")

        # Reject "unknown" reason — too weak; user might have clicked
        # Not-a-Camera before knowing what it was.
        if rec_reason in ("", "unknown"):
            continue

        # Conservative match: same OUI + (same product OR same port)
        product_matches = bool(
            candidate_product and rec_product
            and candidate_product == rec_product)
        port_matches = bool(rec_port and rec_port == port)

        if product_matches or port_matches:
            why = (f"OUI {candidate_oui} + "
                   + ("product match" if product_matches
                      else f"port {port} match")
                   + f"; reason={rec_reason}")
            return (True, why)

    return (False, "")

def build_fingerprint(cam: dict) -> dict:
    """
    Build a shareable device fingerprint from a camera dict.
    Contains NO IP addresses or personally identifying information —
    only hardware/service signatures useful for pattern matching.
    """
    return {
        "oui":          cam.get("mac_addr","")[:8].upper(),  # first 3 octets only
        "mac_vendor":   cam.get("mac_vendor",""),
        "port":         cam.get("port"),
        "protocol":     cam.get("protocol",""),
        "service":      cam.get("server_header",""),
        "page_title":   cam.get("page_title",""),
        "manufacturer": cam.get("manufacturer",""),
        # 2.4.0-rc1.0: RTSP fingerprint fields captured by
        # _rtsp_options_fingerprint. Server-baked metadata, no PII.
        "rtsp_server":  cam.get("rtsp_server_header",""),
        "rtsp_realm":   cam.get("rtsp_auth_realm",""),
    }

async def submit_to_community(record: dict) -> None:

    """
    Fire-and-forget submission to the community endpoint.
    Silently fails if the endpoint is unavailable or not configured.
    """
    if not COMMUNITY_ENDPOINT:
        return
    import urllib.request, urllib.error
    try:
        body = json.dumps(record).encode()
        req  = urllib.request.Request(
            COMMUNITY_ENDPOINT + "/api/v1/report",
            data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", f"AnyCam/{CURRENT_VERSION}")
        with urllib.request.urlopen(req, timeout=8) as resp:
            log.info(f"Community report submitted: {resp.status}")
    except Exception as e:
        log.debug(f"Community submit failed (non-fatal): {e}")


def load_runtime() -> dict:
    """Load persisted runtime state (last run version, etc.)."""
    if not RUNTIME_FILE.exists():
        return {}
    try:
        return json.loads(RUNTIME_FILE.read_text())
    except Exception:
        return {}

def save_runtime(data: dict) -> None:

    DATA_DIR.mkdir(exist_ok=True)
    RUNTIME_FILE.write_text(json.dumps(data, indent=2))

def get_startup_mode() -> str:
    """
    Determine what kind of startup this is.

    Returns:
      "new_install"   — no saved cameras and no prior version recorded
      "routine"       — same version as last run (reboot / HA restart)
      "post_upgrade"  — version differs from last run
    """
    runtime = load_runtime()
    last_version = runtime.get("version")

    if last_version is None:
        # First ever run — could be new install or pre-1.1.8 upgrade
        if CAMS_FILE.exists():
            # Cameras were saved by an older version that didn't write runtime.json
            return "post_upgrade"
        return "new_install"

    if last_version == CURRENT_VERSION:
        return "routine"

    return "post_upgrade"

def _strip_creds(url: str) -> str:
    """Remove user:password@ from every URL in the text.

    3.0.0-rc1.0 (E4): bounded to the URL's own host part. The old pattern
    ran to the first "@" anywhere, so it kept the tail of a password that
    holds "@", and in multi-line ffmpeg text it could remove unrelated
    text up to a later "@".
    """
    return re.sub(r"(://)[^/\s]*@", r"\1", url) if url else url

# ─────────────────────────────────────────────────────────────────────────────
# Network helpers
# ─────────────────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────────────
# False-positive classifier
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1a — ARP ping scan (finds live hosts without full port scan)
# ─────────────────────────────────────────────────────────────────────────────




# ─────────────────────────────────────────────────────────────────────────────
# Stage 1b — SSDP / UPnP discovery
# ─────────────────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────────────
# Stage 1c — mDNS / Bonjour discovery
# ─────────────────────────────────────────────────────────────────────────────




# ─────────────────────────────────────────────────────────────────────────────
# Stage 1d — ONVIF WS-Discovery (already present, kept here for completeness)
# ─────────────────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — nmap scans
# ─────────────────────────────────────────────────────────────────────────────










# ─────────────────────────────────────────────────────────────────────────────
# Protocol probers
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────
# Pure-Python RTSP probe — no ffprobe dependency, no probesize limits,
# no URL-encoding workarounds.  Implements RFC 2326 (RTSP) OPTIONS +
# DESCRIBE with Digest and Basic auth negotiation.
# ─────────────────────────────────────────────────────────────────────────



















# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# Active camera-positive probes
# ─────────────────────────────────────────────────────────────────────────────





















# ONVIF SOAP (multi-stream NVR support)
# ─────────────────────────────────────────────────────────────────────────────










# ─────────────────────────────────────────────────────────────────────────────
# Full-range port scanner (user-initiated, separate from camera scan)
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Main scan orchestration — 4-stage pipeline
# ─────────────────────────────────────────────────────────────────────────────



_FOCUS_ADAPTIVE:         dict  = {}    # camera_id → {tier_idx, locked, run_start, ladder,
                                       #               restarts_since_lock}



















# ─────────────────────────────────────────────────────────────────────────────
# Focus view endpoints
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# go2rtc live view (2.6.3, Tier 2)
# ─────────────────────────────────────────────────────────────────────────────
#
# Why this exists. The classic Enhanced View decodes the stream on the Pi,
# re-encodes it to MJPEG, and serves one JPEG per HTTP request. A Pi 4 cannot
# do that at 3840x2160 HEVC. go2rtc instead passes the camera's H.264 or
# H.265 through untouched, over WebRTC or MSE, and the viewing device decodes
# it with its own hardware. The Pi only moves bytes.
#
# SECURITY MODEL — read before changing anything in this block.
# go2rtc's own documentation warns that anyone who reaches its API can add an
# `exec:` source and run commands on the host. This addon runs with
# host_network and full_access, so a default go2rtc would expose that API to
# the LAN and to the ZeroTier network. Four independent controls:
#   1. The API listens on 127.0.0.1 only. Browsers never reach it directly.
#   2. Only the api, ws, rtsp, webrtc and mp4 modules load (see go2rtc
#      main.go: every named module is skipped unless listed). exec, echo,
#      expr and ffmpeg never initialise, so no command-running source exists
#      even for a local caller. Leaving out ffmpeg also enforces zero
#      transcode: a codec the browser cannot play produces an error and the
#      browser falls back, instead of go2rtc quietly burning Pi CPU.
#   3. go2rtc's RTSP server is off (listen ""). go2rtc registers its RTSP
#      *client* before it checks that value, so reading cameras still works.
#   4. The browser reaches go2rtc only through handle_go2rtc_ws, which
#      forwards /api/ws for stream names AnyCam registered itself.
# verify_release.py carries contracts on _go2rtc_config and
# handle_go2rtc_ws so that none of these controls can be dropped silently.
#
# CREDENTIALS. Config is passed inline (`-config {json}`), so go2rtc has no
# config file. Otherwise PUT /api/streams writes each stream's source URL —
# camera password included — into that file in plaintext. With no file,
# go2rtc creates the stream in memory and then answers HTTP 400 "config file
# disabled" for the persist step. _go2rtc_register treats that exact answer
# as success and confirms the stream exists with a GET.
#
# Only the WebRTC media port is reachable from the network. go2rtc's docs
# note it carries only encrypted media for sessions negotiated through the
# API, and that API is local-only here.


_GO2RTC_TASK: asyncio.Task | None = None
_MOTION_TASK: asyncio.Task | None = None   # 2.6.5: _motion_keeper
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
























# ── 2.6.6: live cards (build plan C1) ──────────────────────────────────────
# Cards play the camera's smallest stream. A phone decoding seven 3840-wide
# H.265 streams at once would stall, so a card whose smallest known stream
# is wider than CARD_MAX_WIDTH stays on snapshots.
CARD_MAX_WIDTH = 1920


def _dahua_sub_stream(url: str) -> str | None:
    """The sub-stream of a Dahua/Lorex main-stream URL, or None.

    DVR channel cards know only /cam/realmonitor?channel=N&subtype=0 (3840
    wide on the Lorex DVR); the DVR serves the sub-stream at subtype=1.
    """
    if "/cam/realmonitor" not in url:
        return None
    sub, n = re.subn(r"([?&]subtype=)0(?=&|$)", r"\g<1>1", url)
    return sub if n else None


















# ─────────────────────────────────────────────────────────────────────────────
# Storage browser
# ─────────────────────────────────────────────────────────────────────────────

async def handle_storage_page(request: web.Request) -> web.Response:
    """GET /storage — redirect to main app; storage is a JS view within the SPA."""
    raise web.HTTPFound(INGRESS_PATH + "/")


# ─────────────────────────────────────────────────────────────────────────────
# REST API
# ─────────────────────────────────────────────────────────────────────────────

def _safe_cam(cam: dict) -> dict:
    s = dict(cam)
    if s.get("stream_url"):
        s["stream_url"] = _strip_creds(s["stream_url"])
    if s.get("sub_stream_url"):
        s["sub_stream_url"] = _strip_creds(s["sub_stream_url"])
    # 2.2.9 — strip embedded creds from per-profile URL fields too. Without
    # this, GET /api/cameras leaked username:password in stream_profiles[].url
    # and stream_profile_N_url top-level keys (middle profiles only — main is
    # stored in stream_url and tail is in sub_stream_url, both already stripped
    # above). The leak only surfaced after a camera was authenticated, since
    # cred-less cards have no stream_profiles populated yet.
    if isinstance(s.get("stream_profiles"), list):
        clean_profiles = []
        for prof in s["stream_profiles"]:
            if isinstance(prof, dict):
                pcopy = dict(prof)
                if pcopy.get("url"):
                    pcopy["url"] = _strip_creds(pcopy["url"])
                clean_profiles.append(pcopy)
            else:
                clean_profiles.append(prof)
        s["stream_profiles"] = clean_profiles
    for k in list(s.keys()):
        if k.startswith("stream_profile_") and k.endswith("_url") and s.get(k):
            s[k] = _strip_creds(s[k])
    s["has_credentials"]  = bool(s.get("credentials"))
    s["upgrade_missing"]  = bool(s.get("upgrade_missing"))
    s["has_sub_stream"]   = bool(s.get("sub_stream_url"))
    # Ensure identity fields always present
    for f in ("manufacturer", "device_notes", "page_title", "server_header",
              "mac_addr", "mac_vendor",
              # 2.4.0-rc1.0: RTSP OPTIONS fingerprint fields. Distinct from
              # the HTTP-layer server_header / page_title above — these are
              # captured from the RTSP layer at port 554. None contain PII;
              # they're server-baked metadata (e.g. realm "IP Camera(NN)").
              "rtsp_server_header", "rtsp_auth_realm", "rtsp_auth_scheme",
              "rtsp_public_methods"):
        s.setdefault(f, "")
    # 2.4.0-rc2.0 (Layered Stream Discovery): list of paths returning 401
    # on the same auth realm during the path-walker run. Populated by
    # the path walker, surfaced as a "View Locked Streams (N)" badge in
    # the UI when len > 0 AND no creds are saved. Default empty list.
    s.setdefault("locked_streams", [])
    # Stream technical details (populated after credentials are accepted)
    for f in ("stream_codec", "stream_audio", "stream_profile"):
        s.setdefault(f, "")
    for f in ("stream_width", "stream_height"):
        s.setdefault(f, None)
    s.setdefault("stream_fps", None)
    s.pop("credentials", None)
    return s


async def api_cameras(request: web.Request) -> web.Response:

    return web.json_response([_safe_cam(c) for c in CAMERAS.values()])

async def api_scan(request: web.Request) -> web.Response:

    if SCAN_STATE["running"]:
        return web.json_response({"error": "Scan already running"}, status=409)
    try:
        data = await request.json()
        SCAN_OPTIONS["broad_sweep"] = bool(data.get("broad_sweep", False))
    except Exception:
        pass
    asyncio.create_task(run_scan())
    return web.json_response({"status": "started"})

async def api_scan_status(request: web.Request) -> web.Response:

    return web.json_response(SCAN_STATE)


async def api_scan_cancel(request: web.Request) -> web.Response:

    """POST /api/scan/cancel — request graceful abort of running scan."""
    if not SCAN_STATE["running"]:
        return web.json_response({"error": "No scan running"}, status=400)
    anycam_scan.SCAN_CANCELLED = True       # 3.0.0-rc1.5 (B22): the flag the scan reads
    log.info("Scan cancel requested by user")
    SCAN_STATE.update(message="Cancelling scan…")
    return web.json_response({"status": "cancelling"})


def _match_stream_db(camera: dict) -> dict | None:
    """Return the best-matching STREAM_DB entry for a camera, or None.

    rc2: includes mac_vendor (OUI lookup result) in the haystack so that
    cameras identified by MAC address alone — before any HTTP/RTSP probe —
    can be matched to their vendor recipe. This is critical for cameras
    like Microseven where ONVIF returns no useful vendor info but the
    OUI lookup gives "Microseven Inc"."""
    haystack = " ".join([
        camera.get("name", ""),
        camera.get("vendor", ""),
        camera.get("model", ""),
        camera.get("verdict_reason", ""),
        camera.get("hostname", ""),
        camera.get("mac_vendor", ""),       # rc2: OUI vendor name
        camera.get("manufacturer", ""),     # rc2: previously-identified brand
        camera.get("page_title", ""),       # rc2: HTTP page title
        camera.get("server_header", ""),    # rc2: HTTP/RTSP Server header
        camera.get("nmap_product", ""),     # rc2: nmap service banner
        camera.get("onvif_scopes", ""),     # rc2.1.1: WS-Discovery scopes
    ]).lower()
    best_slug, best_len = None, 0
    for slug, entry in STREAM_DB.items():
        for kw in entry["match"]:
            if kw in haystack and len(kw) > best_len:
                best_slug, best_len = slug, len(kw)
    return STREAM_DB[best_slug] if best_slug else None


def _match_stream_db_slug(camera: dict) -> str | None:
    """Return the STREAM_DB slug that matched, or None.

    rc2: includes mac_vendor + manufacturer + page_title + server_header +
    nmap_product in the haystack — see _match_stream_db for rationale."""
    haystack = " ".join([
        camera.get("name", ""),
        camera.get("vendor", ""),
        camera.get("model", ""),
        camera.get("verdict_reason", ""),
        camera.get("hostname", ""),
        camera.get("mac_vendor", ""),       # rc2: OUI vendor name
        camera.get("manufacturer", ""),     # rc2: previously-identified brand
        camera.get("page_title", ""),       # rc2: HTTP page title
        camera.get("server_header", ""),    # rc2: HTTP/RTSP Server header
        camera.get("nmap_product", ""),     # rc2: nmap service banner
        camera.get("onvif_scopes", ""),     # rc2.1.1: WS-Discovery scopes
    ]).lower()
    best_slug, best_len = None, 0
    for slug, entry in STREAM_DB.items():
        for kw in entry["match"]:
            if kw in haystack and len(kw) > best_len:
                best_slug, best_len = slug, len(kw)
    return best_slug


async def _probe_db_streams(ip: str, port: int, creds: str | None,
                             db_entry: dict,
                             existing_urls: set[str]) -> list[dict]:
    """
    Probe RTSP paths from a STREAM_DB entry.
    Returns list of {url, width, height, codec} dicts for responding paths,
    skipping any URLs already in existing_urls.

    2.3.0: All paths now validated through ONE TCP socket via
    _validate_rtsp_urls_single_socket instead of opening a fresh socket
    per path. Eliminates the multi-socket pressure that triggered
    Hipcam-family lockout in rc2.x. ffprobe is still per-URL (its own
    subprocess opens its own socket) so we pace it for rate_limit brands.
    """
    loop = asyncio.get_event_loop()
    results: list = []
    rtsp_port = db_entry.get("port", 554)

    cred_pfx = ""
    username = ""
    password = ""
    if creds:
        try:
            from urllib.parse import quote as _q
            username, password = decrypt_creds(creds)
            _SAFE = "!$&'()*+,;=~-._"
            cred_pfx = f"{_q(username, safe=_SAFE)}:{_q(password, safe=_SAFE)}@"
        except Exception:
            pass

    # Build URL list, deduping against caller's existing_urls set
    urls_to_validate: list = []
    bare_for_url:     dict = {}   # cred-bearing URL → bare URL (for de-dup)
    for path in db_entry.get("rtsp", []):
        url  = f"rtsp://{cred_pfx}{ip}:{rtsp_port}{path}"
        bare = f"rtsp://{ip}:{rtsp_port}{path}"
        if bare in existing_urls or url in existing_urls:
            continue
        urls_to_validate.append(url)
        bare_for_url[url] = bare

    if not urls_to_validate:
        return results

    # 2.3.0: throttle awareness — even though we use one TCP socket for
    # validation now, ffprobe per match still opens its own socket, so
    # we register the validation TCP open with the tracker.
    throttle_s = 0.0
    # Build a minimal camera-like dict for brand lookup.  db_entry has
    # all CAMERA_DB metadata already, so we can use it directly.
    if db_entry.get("throttle_type") == "rate_limit_per_ip_tcp":
        throttle_s = _parse_throttle_seconds(
            db_entry.get("throttle_amount", "")) or 5.0
        await _throttle_wait_if_needed(ip, throttle_s, "db_probe validation")

    url_results = await loop.run_in_executor(
        _THREAD_POOL, _validate_rtsp_urls_single_socket,
        ip, rtsp_port, urls_to_validate,
        username, password, 4.0, None,
        f"db_probe:{ip}")

    for url in urls_to_validate:
        if not url_results.get(url, False):
            continue
        try:
            if throttle_s > 0:
                await _throttle_wait_if_needed(ip, throttle_s,
                                               f"db_probe ffprobe {url}")
            det = await probe_stream_details(url, "RTSP")
            results.append({"url": url, **det})
        except Exception:
            pass
    return results


# 2.5.0-rc1.2: post-cred-auth channel enumeration on channel_iterate
# brands. Tracks which (camera_id) we've already enumerated so a
# repeated cred-auth click doesn't re-enumerate the same DVR.
_DVR_ENUM_DONE: set = set()


async def _enumerate_dvr_channels_after_auth(camera_id: str) -> None:
    """2.5.0-rc1.2: when cred-auth succeeds on a `channel_iterate`
    brand (Lorex/Dahua DVR-NVR Family etc.), walk the remaining
    channels in a background task and register each populated channel
    as its own camera card.

    Architectural rationale: a DVR exposes 1..N virtual stream slots,
    and a populated slot maps to a physical camera channel. Surfacing
    each populated channel as a separate card was the original 2026-
    05-02 plan's acceptance criterion #1 (`returns N populated
    channels (N = number of cameras physically connected, ≥5)`). 2.5.0-
    rc1.0 deferred this; 2.5.0-rc1.2 lands it.

    Throttle safety: brand has `auth_attempt_lockout` semantics — but
    SUCCESSFUL auth doesn't burn counter attempts, only failed auth
    does. The credentials we use here have already been validated by
    the cred-auth flow that called us, so walking remaining channels
    is unconstrained by the lockout counter. The validate walker
    captures the auth challenge from the first 401 and reuses the
    nonce for subsequent URLs (RFC 2617).

    Empty-channel filtering: the validate walker now (rc1.2) honors
    `walker_populated_channel_test` on host_meta. We pass
    `sdp_has_video_track` so virtual slots that return SDP without a
    real video codec rtpmap get skipped instead of registered as
    cards.

    Idempotency: tracked via `_DVR_ENUM_DONE`. Re-clicking save
    credentials on an already-enumerated DVR is a no-op (the existing
    cards remain).

    Per-card snap URL: built from the streaming_recipe's
    `snap_url_template` (added 2.5.0-rc1.2) so each channel card's
    thumbnail polls the channel-specific snapshot endpoint
    (`http://IP/cgi-bin/snapshot.cgi?channel=N` for Dahua-family).
    Without per-channel URLs, all cards would poll the same default
    snapshot and show the same image.
    """
    if camera_id in _DVR_ENUM_DONE:
        return

    cam = CAMERAS.get(camera_id)
    if not cam:
        # 2.5.0-rc1.8: signal done so the new JS poll loop in submitCreds()
        # exits cleanly when the camera was deleted between cred-auth and
        # task scheduling. Without this, /api/dvr_enum/status/{id} would
        # return done=False forever (camera_id never enters the set), and
        # the poll would only exit on its 12s hard cap.
        _DVR_ENUM_DONE.add(camera_id)
        return

    brand_entry = _identify_camera_brand(cam)
    if not brand_entry:
        _DVR_ENUM_DONE.add(camera_id)  # rc1.8: terminate poll loop
        return
    recipe = (brand_entry or {}).get("streaming_recipe") or {}
    if recipe.get("type") != "channel_iterate":
        _DVR_ENUM_DONE.add(camera_id)  # rc1.8: terminate poll loop
        return

    primary_url = cam.get("stream_url", "")
    primary_ch  = _extract_channel_from_rtsp_url(primary_url)
    if not primary_ch:
        log.debug(f"  channel enumeration: could not extract primary "
                  f"channel from {_strip_creds(primary_url)} — aborting")
        _DVR_ENUM_DONE.add(camera_id)  # rc1.8: terminate poll loop
        return

    creds = cam.get("credentials")
    if not creds:
        _DVR_ENUM_DONE.add(camera_id)  # rc1.8: terminate poll loop
        return
    # 2.5.0-rc1.3: bug-fix on rc1.2. The cam["credentials"] field is a
    # Fernet-encrypted JSON string, not a dict — decrypt it the same way
    # the rest of the cred-aware codepaths do (see line ~8690 in
    # _validate_rtsp_walk). rc1.2 assumed a dict shape and crashed:
    # `AttributeError: 'str' object has no attribute 'get'` at the line
    # where we tried `creds.get("username", "")`. The crash happened
    # silently in the fire-and-forget task, so cred-auth still
    # succeeded (single card surfaced as before) but no enumeration
    # ever ran.
    try:
        username, password = decrypt_creds(creds)
    except Exception as e:
        log.debug(f"  channel enumeration: decrypt_creds failed: {e}")
        _DVR_ENUM_DONE.add(camera_id)  # rc1.8: terminate poll loop
        return
    if not (username and password):
        _DVR_ENUM_DONE.add(camera_id)  # rc1.8: terminate poll loop
        return

    ip   = cam.get("ip", "")
    port = cam.get("port", 554)

    # Build candidate URLs for OTHER channels. Main stream only here —
    # we want to know "which channels have cameras," not validate every
    # sub-stream variant. Sub-streams for each populated channel can be
    # discovered later by the per-card cred-auth flow.
    template = recipe.get("path_template", "")
    raw_channels = recipe.get("channels") or list(range(1, 17))
    main_subtype = recipe.get("subtype_main", 0)
    channel_cap  = 16
    candidate_paths: list[str] = []
    for ch in raw_channels:
        if ch > channel_cap:
            continue
        if str(ch) == primary_ch:
            continue
        try:
            candidate_paths.append(template.format(ch=ch, st=main_subtype))
        except (KeyError, IndexError):
            continue

    if not candidate_paths:
        _DVR_ENUM_DONE.add(camera_id)
        return

    candidate_urls = [
        f"rtsp://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{ip}:{port}{p}"
        for p in candidate_paths
    ]

    log.info(f"  Channel enumeration starting for {camera_id}: walking "
             f"{len(candidate_urls)} other channel paths "
             f"(brand={brand_entry.get('name', '?')})")

    # 2.5.0-rc1.4: walk URLs one socket at a time. Firmware on this
    # brand closes the TCP connection after each full authenticated
    # transaction (OPTIONS+DESCRIBE+SETUP+TEARDOWN), so the validate
    # walker's normal single-socket-multi-URL pattern bails out on the
    # second URL with `OPTIONS exception → ConnectionResetError`. The
    # 2.5.0-rc1.3 field log captured this exactly: channel=2 succeeded
    # cleanly, channel=3 OPTIONS hit RST, walker bailed remaining 13
    # URLs. Per-URL invocation gives each URL a fresh socket. ~300ms
    # per URL × 15 URLs ≈ 5s total wallclock — acceptable for a
    # background task. Each call sets walker_skip_acd so the closed-
    # socket-after-success isn't logged as an ACD-relevant RST event.
    enum_meta = {
        "walker_throttle_type":           "auth_attempt_lockout",
        "walker_populated_channel_test":  recipe.get(
            "populated_channel_test", "sdp_has_video_track"),
        "walker_skip_acd":                True,
    }
    loop = asyncio.get_event_loop()
    walk_result: dict[str, bool] = {}
    for i, url in enumerate(candidate_urls, 1):
        try:
            one_result = await loop.run_in_executor(
                _THREAD_POOL,
                _validate_rtsp_urls_single_socket,
                ip, port, [url], username, password, 6.0,
                enum_meta,
                f"channel-enum:{camera_id}({i}/{len(candidate_urls)})",
            )
        except Exception as e:
            log.debug(f"  channel enum walker URL {i} raised: {e}")
            one_result = {url: False}
        if isinstance(one_result, dict):
            walk_result.update(one_result)
        # Tiny breath between URLs to be polite to the DVR's RTSP
        # subsystem and let any lingering server-side socket cleanup
        # finish before the next OPTIONS opens a new connection.
        await asyncio.sleep(0.1)

    snap_template = recipe.get("snap_url_template", "")
    populated_channels: list[str] = []
    new_cards: list[str] = []

    for url, probe_ok in walk_result.items():
        if not probe_ok:
            continue
        ch = _extract_channel_from_rtsp_url(url)
        if not ch:
            continue
        populated_channels.append(ch)
        new_id = f"{ip}_{port}_ch{ch}"
        if new_id in CAMERAS:
            continue
        # Build per-channel snap URL from template if available
        per_channel_snap = ""
        if snap_template:
            try:
                per_channel_snap = snap_template.format(ip=ip, ch=ch)
            except (KeyError, IndexError):
                per_channel_snap = ""
        # Clone the parent card structure, override channel-specific
        # fields. Strip credentials from the visible URL — the real
        # creds live in cam["credentials"] and get re-attached at
        # stream-fetch time by the snap_loop.
        new_cam = dict(cam)
        new_cam.update({
            "id":             new_id,
            "stream_url":     _strip_creds(url),
            "channel":        ch,
            "name":           f"{brand_entry.get('name', 'DVR')} ch{ch}",
            "status":         "ready",
            "user_saved":     True,
            "requires_credentials": False,
            "http_snap_url":  per_channel_snap or cam.get("http_snap_url", ""),
            # Children inherit parent credentials
            "credentials":    cam.get("credentials"),
            "stream_profiles": [],   # rebuilt on first focus
            "locked_streams":  [],
            "additional_streams": [],
            "_dvr_parent_id":  camera_id,
        })
        CAMERAS[new_id] = new_cam
        new_cards.append(new_id)

        # 2.5.0-rc1.5: explicitly start the snap_loop for each new card.
        # Without this, newly-registered cards live in the CAMERAS dict
        # and surface in /api/cameras but never get a thumbnail polled
        # because the snap_loop kickoff path normally runs from
        # handle_snapshot (UI requests thumbnail → snap_loop starts).
        # Symptom on rc1.4: enumeration registered 7 channels but the
        # UI showed nothing — saw it in the field log as zero `SNAP
        # [192.168.50.217_554_chN]: starting background process` lines
        # firing after `Channel enumeration complete`. Mirroring the
        # post-restart-load behaviour where each saved card kicks off
        # its own snap_loop on startup.
        try:
            authed_url = build_authenticated_url(new_cam)
            if authed_url:
                _snap_last_access[new_id] = time.monotonic()
                state = _snap_state(new_id)
                if state.get("task") is None or state["task"].done():
                    log.info(f"  SNAP [{new_id}]: kicking off initial "
                             f"thumbnail loop after channel enumeration")
                    state["task"] = asyncio.create_task(
                        snap_loop(new_id, authed_url, new_cam))
        except Exception as _snap_e:
            log.debug(f"  channel-enum: snap kickoff for {new_id} "
                      f"raised: {_snap_e}")

    # Update the parent card's name to reflect the primary channel
    # (e.g. "Lorex / Dahua DVR-NVR Family ch1") so all DVR cards share
    # the same naming convention.
    if cam.get("name", "").lower() in (
            "general", "ip camera", "network camera", brand_entry.get(
                "name", "").lower()):
        cam["name"] = f"{brand_entry.get('name', 'DVR')} ch{primary_ch}"
    cam["channel"] = primary_ch
    cam["_dvr_parent_id"] = camera_id   # parent is its own parent
    cam["_dvr_populated_channels"] = sorted(
        set(populated_channels + [primary_ch]),
        key=lambda c: int(c) if c.isdigit() else 999)

    _DVR_ENUM_DONE.add(camera_id)
    save_cameras()
    log.info(f"  Channel enumeration complete for {camera_id}: "
             f"{len(new_cards)} new card(s) registered "
             f"(populated channels: {cam['_dvr_populated_channels']})")


async def api_dvr_enum_status(request: web.Request) -> web.Response:
    """2.5.0-rc1.8: lightweight polling endpoint that lets the post-cred-
    auth UI flow detect channel-enumeration completion deterministically
    instead of waiting a fixed wallclock budget.

    Replaces the 2.5.0-rc1.6 fixed +8s setTimeout(loadCameras) reload
    with a poll-until-done pattern. On CrystalHeeler's 7-channel Lorex the
    enumeration completed in ~3s (rc1.7 test log 10:51:16 → 10:51:19),
    so the old fixed budget cost ~5s of dead time before cards
    appeared. This endpoint shaves that by letting the UI react to
    actual completion.

    Returns:
      done                  — bool. True iff camera_id is in
                              _DVR_ENUM_DONE. The set is now populated
                              on every exit path of
                              _enumerate_dvr_channels_after_auth (rc1.8
                              backend hardening), so this flag flips
                              true within bounded time regardless of
                              outcome.
      populated_channels    — list[str]. Channels with real cameras
                              attached, populated from the parent cam's
                              _dvr_populated_channels field after a
                              successful run. Empty for non-success
                              exits (camera deleted, decrypt fail, etc.)
                              and during the in-progress window.

    Pattern matches /api/scan/status, /api/pscan/status, /snap/status:
    a tiny GET with a JSON body that the JS polls on a setInterval.
    """
    cid = request.match_info.get("camera_id", "")
    cam = CAMERAS.get(cid)
    populated: list = []
    if cam:
        populated = list(cam.get("_dvr_populated_channels") or [])
    return web.json_response({
        "done": cid in _DVR_ENUM_DONE,
        "populated_channels": populated,
    })


async def api_set_credentials(request: web.Request) -> web.Response:

    try:
        data      = await request.json()
        camera_id = data.get("camera_id", "")
        username  = data.get("username", "").strip()
        password  = data.get("password", "")
        # 2.6.0-rc3.0 Item 4 — track whether this credential submission
        # came in through the "🔒 N Locked Streams" badge modal or
        # through a generic Login button. When True, we clear
        # camera.locked_streams after auth (the user explicitly
        # walked through the modal, the badge has served its purpose
        # and the validated subset is now in additional_streams).
        # When False, we PRESERVE the locked_streams list so the badge
        # persists — the user logged in via some other path and may
        # still want to review locked candidates later. Pre-rc3.0
        # behavior was to clear unconditionally, which nuked the badge
        # for users who logged in via the regular Login button.
        from_locked_modal = bool(data.get("from_locked_streams_modal", False))
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    camera = CAMERAS.get(camera_id)
    if not camera:
        return web.json_response({"error": "Camera not found"}, status=404)

    proto = camera.get("protocol", "RTSP")
    ip    = camera["ip"]
    port  = camera.get("port", 554)
    loop  = asyncio.get_event_loop()
    url   = None

    log.info(f"Credential attempt: {camera_id} proto={proto} ip={ip}:{port} "
             f"onvif={camera.get('onvif')} xaddrs={camera.get('xaddrs','')[:40]}")

    if proto in ("ONVIF",) or camera.get("onvif"):
        media_url = _onvif_media_url(ip, port, camera.get("xaddrs",""))
        log.info(f"  ONVIF media URL: {media_url}")

        # 2.3.0: brand throttle awareness — surface a status text the UI
        # polls so users see "Authenticating (Camera rate-limited, ~30
        # seconds)" instead of a silent 25s pause for Hipcam-family
        # cameras. The throttle pacing itself happens before each ffprobe
        # call below; the cred-auth handler shows the status while it works.
        throttle_s = _brand_throttle_seconds(camera)
        if throttle_s > 0:
            camera["status"] = "authenticating_throttled"
            camera["status_text"] = (f"Authenticating "
                                     f"(Camera rate-limited, ~30 seconds)")
            log.info(f"  Brand has rate_limit_per_ip_tcp ({throttle_s:.0f}s) — "
                     f"using single-socket validation + throttled ffprobe")

        profiles  = await loop.run_in_executor(
            _THREAD_POOL, onvif_get_profiles, media_url, username, password)
        log.info(f"  ONVIF profiles found: {len(profiles)} — {[p['name'] for p in profiles]}")
        if profiles:
            enc_creds = encrypt_creds(username, password)

            # ── 2.3.0: Collect all stream URLs first, then validate them
            # all over a SINGLE TCP socket. Replaces the per-profile
            # probe_rtsp loop (one TCP per profile) that triggered Hipcam
            # firmware-level lockout in rc2.x.
            profile_urls = []   # list of (prof, stream_url)
            for prof in profiles:
                stream_url = await loop.run_in_executor(
                    _THREAD_POOL, onvif_get_stream_uri, media_url, prof["token"], username, password)
                log.info(f"  Profile '{prof['name']}' stream_url: {stream_url}")
                if stream_url:
                    profile_urls.append((prof, stream_url))

            url_results: dict = {}
            if profile_urls:
                # 2.3.2: Parse the actual RTSP port from the first profile
                # URL. The camera's stored `port` attribute is the
                # DISCOVERY port (often 80 for HTTP-discovered cams like
                # the Hipcam/Microseven family), NOT the RTSP port. The
                # 2.3.0+ validator was being passed `port` directly, so on
                # those cameras it was opening TCP to :80 — the HTTP admin
                # server — and sending RTSP-shaped requests there. The
                # HTTP server replied 'HTTP/1.1 400 Bad Request' to the
                # first request and closed the connection, leaving the
                # second request with an empty response. The validator
                # logged 0/2 OK every time, but the "ONVIF confirmed creds
                # → including anyway" fallback masked the symptom so
                # streams still came up via ffprobe. Confirmed empirically
                # on Microseven via direct port-554 RTSP walks (Test
                # C: 2/2 URLs OK in 1.6s through one socket after a
                # 4-SOAP burst with 1s gap).
                #
                # Defensive parse: ONVIF responses are untrusted input;
                # urlparse can raise ValueError on malformed port. Fall
                # back to 554 (RTSP default) on any parse failure rather
                # than letting the exception bubble up and break the
                # whole cred-auth flow.
                _, first_stream_url = profile_urls[0]
                try:
                    rtsp_port = urlparse(first_stream_url).port or 554
                except (ValueError, AttributeError) as ex:
                    log.warning(f"  Could not parse RTSP port from "
                                f"{first_stream_url!r} ({ex}) "
                                f"— falling back to 554")
                    rtsp_port = 554
                if rtsp_port != port:
                    log.info(f"  Validator using RTSP port {rtsp_port} "
                             f"(parsed from profile URL; camera-stored "
                             f"port was {port})")
                url_results = await loop.run_in_executor(
                    _THREAD_POOL, _validate_rtsp_urls_single_socket,
                    ip, rtsp_port, [u for _, u in profile_urls],
                    username, password, 6.0, None,
                    f"{camera_id}/onvif-profiles")
                log.info(f"  Single-socket profile validation: "
                         f"{sum(1 for v in url_results.values() if v)}/"
                         f"{len(profile_urls)} OK")

            # ── Build stream_candidates from validated results ────────────────
            stream_candidates = []   # list of {url, width, height, codec, token, name}
            for prof, stream_url in profile_urls:
                ok = url_results.get(stream_url, False)
                if not ok:
                    log.warning(f"  validate returned False for '{prof['name']}' "
                                f"— including anyway (ONVIF confirmed creds)")
                # 2.3.0: pace ffprobe (it opens its own RTSP socket per call,
                # not covered by the single-socket validation above).
                if throttle_s > 0:
                    await _throttle_wait_if_needed(ip, throttle_s,
                                                   f"ffprobe '{prof['name']}'")
                det = await probe_stream_details(stream_url, "RTSP")

                # ── Resolution: highest pixel area wins ───────────────────────
                # Both probe and ONVIF can be wrong — buggy firmware tends to
                # report *lower* or zero values, not inflated ones, so the source
                # reporting the larger pixel area is almost certainly more correct.
                probe_w  = det.get("stream_width")  or 0
                probe_h  = det.get("stream_height") or 0
                onvif_w  = prof.get("onvif_width")  or 0
                onvif_h  = prof.get("onvif_height") or 0
                if (onvif_w * onvif_h) >= (probe_w * probe_h) and onvif_w:
                    det["stream_width"]  = onvif_w
                    det["stream_height"] = onvif_h
                    res_src = "onvif"
                else:
                    res_src = "probe" if probe_w else "none"

                # ── Codec: highest capability wins ────────────────────────────
                # Rank: hevc > h264 > mjpeg > mpeg4 > anything else.
                # Again, wrong firmware tends to report a lesser codec (e.g.
                # H264 for an HEVC stream), so the higher-ranked source wins.
                _CODEC_RANK = {"hevc": 4, "h265": 4, "h264": 3,
                               "mjpeg": 2, "jpeg": 2, "mpeg4": 1}
                probe_codec = det.get("stream_codec") or ""
                onvif_codec = prof.get("onvif_encoding") or ""
                probe_rank  = _CODEC_RANK.get(probe_codec.lower(), 0)
                onvif_rank  = _CODEC_RANK.get(onvif_codec.lower(), 0)
                if onvif_rank > probe_rank and onvif_codec:
                    det["stream_codec"] = onvif_codec
                    codec_src = "onvif"
                elif probe_codec:
                    codec_src = "probe"
                else:
                    codec_src = "none"

                # ── Audio: ONVIF wins (probe rarely detects audio correctly) ──
                if prof.get("onvif_audio"):
                    det["stream_audio"] = prof["onvif_audio"]

                log.info(f"  Profile '{prof['name']}': "
                         f"{det.get('stream_width')}x{det.get('stream_height')} [{res_src}] "
                         f"{det.get('stream_codec','?')} [{codec_src}] "
                         f"{det.get('stream_fps','?')}fps "
                         f"audio={det.get('stream_audio','none')}")
                stream_candidates.append({
                    "url": stream_url, "token": prof["token"],
                    "name": prof["name"], "probe_ok": ok, **det,
                })

            # ── Silent DB probe: find additional streams not visible pre-login ─
            db_entry  = _match_stream_db(camera)
            db_slug   = _match_stream_db_slug(camera)

            # ── HTTP snapshot URL: DB first, ONVIF GetSnapshotUri as fallback ─
            http_snap_url       = None
            http_snap_auth_mode = "basic"
            if db_entry and db_entry.get("snap"):
                http_snap_url = f"http://{ip}{db_entry['snap']}"
                if db_slug == "reolink":
                    http_snap_auth_mode = "query_params"
                log.info(f"  HTTP snap URL (DB): {http_snap_url}")
            else:
                # Secondary: try ONVIF GetSnapshotUri on the first profile token
                if profiles:
                    _first_token = profiles[0]["token"]
                    _snap_uri = await loop.run_in_executor(
                        _THREAD_POOL, onvif_get_snapshot_uri,
                        media_url, _first_token, username, password)
                    if _snap_uri:
                        http_snap_url = _snap_uri
                        log.info(f"  HTTP snap URL (ONVIF GetSnapshotUri): {http_snap_url}")
                    else:
                        log.debug(f"  HTTP snap URL: not available (DB=None, ONVIF=None)")

            # ── 2.3.0: Modified db_probe skip rule (CrystalHeeler's design) ────────
            # Skip db_probe if we already have ≥2 stream URL coverage:
            #   • ≥2 ONVIF profiles probed OK (typical case: main + sub), OR
            #   • ≥1 ONVIF profile + a pre-existing unauth stream from scan
            #     (this fires when user manually opens cred dialog on an
            #     already-streaming card to add auth'd profiles).
            # Otherwise run db_probe as the safety net for finding streams
            # ONVIF didn't expose. Replaces the old "always run" behavior.
            onvif_working    = sum(1 for c in stream_candidates if c.get("probe_ok"))
            have_unauth      = bool(camera.get("rtsp_probe_ok") and camera.get("stream_url"))
            have_main_n_sub  = (onvif_working >= 2) or (onvif_working >= 1 and have_unauth)

            if db_entry and not have_main_n_sub:
                existing = {c["url"] for c in stream_candidates}
                db_streams = await _probe_db_streams(ip, port, enc_creds,
                                                     db_entry, existing)
                if db_streams:
                    log.info(f"  DB probe found {len(db_streams)} extra stream(s)")
                    for s in db_streams:
                        stream_candidates.append({**s, "token": "db_probe",
                                                  "name": "DB stream"})
            elif db_entry and have_main_n_sub:
                log.info(f"  Skipping db_probe — main+sub coverage achieved "
                         f"(onvif_working={onvif_working}, "
                         f"unauth_stream={have_unauth})")

            if stream_candidates:
                # ── Rank by resolution: highest first, lowest last ─────────────
                def _res(c: dict) -> int:

                    return (c.get("stream_width") or 0) * (c.get("stream_height") or 0)
                stream_candidates.sort(key=_res, reverse=True)

                # ── Dedupe by (width, height, codec) ───────────────────────────
                # Many ONVIF cameras (Hikvision, Dahua) expose 4+ profiles where
                # several are identical resolution/codec but differ only in name
                # or token. Showing all of them in the focus-view Resolution
                # dropdown is noise — keep only the first (highest-priority,
                # already sort-ordered) URL for each unique combo.
                seen   = set()
                deduped = []
                for c in stream_candidates:
                    key = (c.get("stream_width"),
                           c.get("stream_height"),
                           (c.get("stream_codec") or "").lower())
                    if key in seen:
                        log.info(f"  Profile dedupe: dropping duplicate "
                                 f"{key[0]}x{key[1]} {key[2] or '?'} "
                                 f"({c.get('name', '?')}, {_strip_creds(c.get('url',''))})")
                        continue
                    seen.add(key)
                    deduped.append(c)
                stream_candidates = deduped

                main_s = stream_candidates[0]
                # Only use a sub-stream that actually passed probe_rtsp.
                # If the second profile failed probe (e.g. "Connection reset"),
                # thumbnail polling would hammer a broken URL indefinitely.
                ok_subs = [c for c in stream_candidates[1:] if c.get("probe_ok")]
                sub_s   = ok_subs[-1] if ok_subs else None

                # Build stream_profiles: all candidates in resolution order,
                # each tagged with a _url_key so the adaptive ladder can look up
                # the right URL via build_authenticated_url().
                # profile[0] = highest res (main), profile[-1] = lowest res (sub).
                stream_profiles = []
                for i, cand in enumerate(stream_candidates):
                    url_key = ("stream_url" if i == 0
                               else ("sub_stream_url" if i == len(stream_candidates) - 1
                                     else f"stream_profile_{i}_url"))
                    stream_profiles.append({
                        "url":          cand["url"],
                        "_url_key":     url_key,
                        "stream_width":  cand.get("stream_width"),
                        "stream_height": cand.get("stream_height"),
                        "stream_codec":  cand.get("stream_codec"),
                        "stream_fps":    cand.get("stream_fps"),
                        "stream_audio":  cand.get("stream_audio"),
                        "rtsp_probe_ok": bool(cand.get("probe_ok")),
                    })

                log.info(f"  Profiles ranked by resolution:")
                for i, p in enumerate(stream_profiles):
                    log.info(f"    [{i}] {_strip_creds(p['url'])} "
                             f"({p.get('stream_width')}x{p.get('stream_height')}) "
                             f"{p.get('stream_codec','?')}")

                main_token = main_s.get("token", profiles[0]["token"])
                cid = f"{ip}_onvif_{main_token}"
                main_details = {k: v for k, v in main_s.items()
                                if k not in ("url", "token", "name")}

                # Build extra-profile URL keys for the CAMERAS dict so
                # build_authenticated_url can look them up by key.
                extra_urls = {}
                for i, cand in enumerate(stream_candidates):
                    if i == 0:
                        pass   # main → stream_url (added below via main_s["url"])
                    elif i == len(stream_candidates) - 1:
                        pass   # last → sub_stream_url (added below)
                    else:
                        extra_urls[f"stream_profile_{i}_url"] = cand["url"]

                # Always reset hevc_plus_warning on (re-)discovery so stale flags
                # from previous sessions or wrong stream URLs don't carry forward.
                # _drain_stderr will re-set it at runtime if ffmpeg actually sees
                # "Multi-layer HEVC coding is not implemented" on this stream.
                # rc2.2: preserve identification fields from the OLD camera
                # record across this dict-replacement. Without this, anything
                # the scan stage identified (manufacturer via mac_vendor /
                # ONVIF scopes / HTTP probe / RTSP Server header) is silently
                # wiped when the user enters credentials and the camera
                # transitions to a profile-based id (e.g. _onvif → _onvif_<token>).
                _id_preserve = {
                    k: camera.get(k, "")
                    for k in ("manufacturer", "mac_addr", "mac_vendor",
                              "page_title", "server_header", "onvif_scopes",
                              "device_notes",
                              # 2.4.0-rc1.0: RTSP fingerprint fields are
                              # captured pre-auth; preserve across the
                              # cred-auth dict replacement so the Identity
                              # panel shows them after the user authenticates.
                              "rtsp_server_header", "rtsp_auth_realm",
                              "rtsp_auth_scheme", "rtsp_public_methods",
                              # 2.4.0-rc2.0: locked_streams persists across
                              # cred-auth too — the user just supplied creds
                              # for some of them, but other locked candidates
                              # may remain (different sub-streams, etc.).
                              # The UI will hide the badge once creds are
                              # saved (camera.user_saved=True), but the data
                              # stays for diagnostic / debugging visibility.
                              "locked_streams")
                    if camera.get(k)
                }
                CAMERAS[cid] = {
                    "id": cid, "ip": ip,
                    "hostname": camera.get("hostname", ip),
                    "port": port, "protocol": "RTSP", "onvif": True,
                    "stream_url":     main_s["url"],
                    "sub_stream_url": sub_s["url"] if sub_s else None,
                    "stream_profiles": stream_profiles,
                    "requires_credentials": False, "credentials": enc_creds,
                    "name": camera.get("name", ip),
                    "status": "ready", "display": "proxy", "user_saved": True,
                    "verdict": "camera", "verdict_reason": "ONVIF profile",
                    "hevc_plus_warning": False,  # reset; _drain_stderr re-sets if needed
                    "http_snap_url":       http_snap_url,
                    "http_snap_auth_mode": http_snap_auth_mode,
                    "rtsp_probe_ok":       bool(main_s.get("probe_ok")),
                    **_id_preserve,
                    **extra_urls,
                    **main_details,
                }
                CAMERAS.pop(camera_id, None)
                # rc2.2: re-run brand identification on the new record
                # one last time. The dict-replacement above retained
                # any preserved manufacturer via _id_preserve, but if
                # NONE was identified pre-cred (e.g. the Microseven was unable to
                # identify because nmap-rate-limit blocked it AND the
                # rc2.2 HTTP probe was racing against the user entering
                # creds) this gives one more chance using all current
                # signals on the record.
                _new = CAMERAS[cid]
                if not _new.get("manufacturer"):
                    try:
                        _re_id = _identify_camera_brand(_new, force=True)
                        if _re_id and _re_id["name"] != "Generic IP Camera":
                            log.info(f"  Brand identified post-cred-auth: "
                                     f"{_re_id['name']}")
                    except Exception as e:
                        log.debug(f"  brand-id (post-cred-auth ONVIF): {e}")
                save_cameras()
                cam = CAMERAS.get(cid)
                if cam:
                    # Run ffprobe to detect the real codec — ONVIF often reports
                    # "h264" when the camera actually streams HEVC.
                    cam_url = build_authenticated_url(cam) or ""
                    async def _fix_codec(cam_id: str = cid, auth_url: str = cam_url,
                                         cam_ip: str = ip, throttle_s: float = throttle_s) -> None:

                        # rc2.6: retry with backoff. The original single-shot
                        # 1s sleep + ffprobe could fail when the camera was
                        # still in a per-IP TCP rate-limit cooldown from the
                        # cred-auth probe sequence (Hipcam family: 5s+ window
                        # between TCP opens). When ffprobe failed, real_codec
                        # came back empty, the if-check below was False, and
                        # the codec stayed at the wrong ONVIF-reported value
                        # — leaving snap_loop using the wrong stream_codec for
                        # the rest of the session, which propagates into
                        # downstream behavior (UI labels, hw_decoder selection,
                        # focus ladder construction).
                        #
                        # Retry up to 3 attempts with 5s backoff. Stops on
                        # first success. Logs each retry so the failure mode
                        # is observable in logs even if the corrections never
                        # succeeds (e.g. camera firmware doesn't expose the
                        # stream to ffprobe).
                        #
                        # 2.4.0-rc3.5 Leak E fix: first attempt now honors the
                        # per-IP TCP throttle. Previously hard-coded to 1s,
                        # which for Hipcam-family cameras (5s rate_limit_per_ip_tcp)
                        # could land inside the cooldown window from the
                        # immediately preceding cred-auth ffprobe at line 8498
                        # — silently failing the codec correction probe even
                        # though all the retry-spacing pacing was correct.
                        # Now uses _throttle_wait_if_needed so we honor the
                        # cross-sequence tracker, not just per-iteration
                        # spacing within this loop.
                        det = {}
                        for attempt in range(3):
                            if attempt == 0:
                                if throttle_s > 0:
                                    await _throttle_wait_if_needed(
                                        cam_ip, throttle_s,
                                        f"ffprobe codec correction "
                                        f"(initial)")
                                else:
                                    await asyncio.sleep(1.0)
                            else:
                                await asyncio.sleep(5.0)
                            det = await probe_stream_details(auth_url, "RTSP")
                            if det.get("stream_codec"):
                                if attempt > 0:
                                    log.info(f"  Codec correction probe succeeded "
                                             f"on retry {attempt + 1}/3")
                                break
                            if attempt < 2:
                                log.info(f"  Codec correction probe attempt "
                                         f"{attempt + 1}/3 returned no codec — "
                                         f"retrying in 5s")
                        real_codec = (det.get("stream_codec") or "").lower()
                        stored = (CAMERAS.get(cam_id, {}).get("stream_codec") or "").lower()
                        if real_codec and real_codec != stored:
                            log.info(f"  Codec correction: ONVIF reported {stored!r} "
                                     f"but ffprobe detected {real_codec!r}")
                            if cam_id in CAMERAS:
                                CAMERAS[cam_id]["stream_codec"] = real_codec
                                profs = CAMERAS[cam_id].get("stream_profiles") or []
                                if profs:
                                    profs[0]["stream_codec"] = real_codec
                                save_cameras()
                        elif not real_codec:
                            log.warning(f"  Codec correction: all 3 ffprobe attempts "
                                        f"failed for {cam_id} — stream_codec stays "
                                        f"as {stored!r} (may be wrong if ONVIF "
                                        f"misreported)")
                    asyncio.create_task(_fix_codec())

                return web.json_response({"status": "ok", "channels": 1})
            log.warning(f"  ONVIF: profiles found but no streams resolved — falling back to direct RTSP")
        else:
            log.warning(f"  ONVIF: no profiles returned (auth failed or device unreachable)")

    # RTSP direct — for ONVIF cards always try port 554 in addition to stored port
    if proto in ("RTSP", "DVR", "ONVIF"):
        # 2.3.0: brand throttle awareness for non-ONVIF cred-auth.
        # find_rtsp_path already does single-socket walking (Layer 1) +
        # brand-aware Layer 2 short-circuit, so no socket pressure here,
        # but we still surface the status text + pace ffprobe below.
        throttle_s = _brand_throttle_seconds(camera)
        if throttle_s > 0 and not camera.get("status_text"):
            camera["status"] = "authenticating_throttled"
            camera["status_text"] = (f"Authenticating "
                                     f"(Camera rate-limited, ~30 seconds)")
            log.info(f"  Brand has rate_limit_per_ip_tcp ({throttle_s:.0f}s) — "
                     f"pacing ffprobe and db_probe")
        # rc2.1: try standard RTSP port 554 BEFORE stored port when they
        # differ. Prevents the Hikvision-NVR-probed-on-port-80 case where
        # find_rtsp_path Layer 1 would burn 30-45s grinding paths against
        # the HTTP/ONVIF port before falling through to 554. The Layer 2
        # fast-bail ALSO covers this (skips when Layer 1 sees no RTSP
        # responses), but trying 554 first short-circuits even Layer 1.
        url = None
        if port != 554:
            log.info(f"  Trying direct RTSP on {ip}:554 (standard RTSP port, "
                     f"before stored port {port})")
            url = await loop.run_in_executor(
                _THREAD_POOL, find_rtsp_path, ip, 554, username, password, camera)
        if not url:
            log.info(f"  Trying direct RTSP on {ip}:{port}")
            url = await loop.run_in_executor(
                _THREAD_POOL, find_rtsp_path, ip, port, username, password, camera)
        if not url and camera.get("xaddrs"):
            parsed = urlparse(camera["xaddrs"])
            rtsp_port = parsed.port or 554
            if rtsp_port != 554 and rtsp_port != port:
                log.info(f"  Trying RTSP via xaddrs {ip}:{rtsp_port}")
                url = await loop.run_in_executor(
                    _THREAD_POOL, find_rtsp_path, parsed.hostname or ip,
                    rtsp_port, username, password, camera)
        log.info(f"  RTSP result: {_strip_creds(url) if url else 'None'}")
    elif proto == "MJPEG":
        url = await loop.run_in_executor(
            _THREAD_POOL, probe_mjpeg_http, ip, port, username, password)
    elif proto == "HLS":
        url = await loop.run_in_executor(
            _THREAD_POOL, probe_hls, ip, port, username, password)

    if not url:
        log.warning(f"Credential attempt FAILED for {camera_id} — no working stream found")
        # 2.4.0-rc2.1 — Issue 4: restore needs_credentials state before
        # returning 401. The handler entry mutated camera["status"] to
        # "authenticating_throttled" (rate_limit_per_ip_tcp brands only)
        # and set status_text to inform the UI. Without this restore,
        # the next /api/cameras poll returns cam.status="authenticating_throttled",
        # credFormHTML's `cam.status !== 'needs_credentials'` check
        # fires false, and the card collapses with no login form —
        # leaving the user stranded with only a Remove button. Observed
        # on Microseven (rate_limit_per_ip_tcp) when wrong creds
        # were tried; not observed on Hikvision because Hikvision
        # is not throttled, so the entry status mutation never happened.
        if camera.get("status") == "authenticating_throttled":
            camera["status"] = "needs_credentials"
            camera.pop("status_text", None)
        return web.json_response({"error": "Could not connect with those credentials."}, status=401)

    # 2.3.0: pace ffprobe on rate_limit brands (it opens its own RTSP socket)
    _t_s = _brand_throttle_seconds(camera)
    if _t_s > 0:
        await _throttle_wait_if_needed(ip, _t_s, "non-ONVIF ffprobe")
    # 2.6.0-rc2.3 — bug #2 fix: find_rtsp_path returns a bare URL
    # without creds embedded. Calling probe_stream_details with a
    # credless URL hands ffprobe a stream that responds 401 to its
    # DESCRIBE, so ffprobe exits non-zero and probe_stream_details
    # silently returns {}. The downstream consequence is that the
    # main profile (stream_profiles[0]) gets stream_codec=None, which
    # then blocks HW decoder selection in snap_loop because it has
    # nothing to match against the candidate list. Pre-build an
    # authenticated URL using the creds that just passed find_rtsp_path
    # so ffprobe can actually fetch the stream. The locked-stream
    # branch at line ~9727 has been doing this all along (using
    # lurl_authed); this brings the main-URL probe to the same level.
    enc_for_probe = encrypt_creds(username, password)
    auth_probe_url = build_authenticated_url(
        dict(camera, credentials=enc_for_probe), url=url) or url
    details = await probe_stream_details(auth_probe_url, proto)
    enc_creds = encrypt_creds(username, password)

    # ── Silent DB probe for additional streams on non-ONVIF cameras ──────────
    sub_url = None
    sub_details: dict = {}   # 2.4.0-rc2.9: codec/res/fps for sub_url
                             # entry in stream_profiles, populated by
                             # the db_streams branch when a sub is
                             # picked. Defaults empty.
    db_entry  = _match_stream_db(camera)
    db_slug   = _match_stream_db_slug(camera)

    # ── HTTP snapshot URL: DB first, no ONVIF fallback on non-ONVIF path ──────
    http_snap_url       = None
    http_snap_auth_mode = "basic"
    if db_entry and db_entry.get("snap"):
        http_snap_url = f"http://{ip}{db_entry['snap']}"
        if db_slug == "reolink":
            http_snap_auth_mode = "query_params"
        log.info(f"  HTTP snap URL (DB): {http_snap_url}")
    else:
        log.debug(f"  HTTP snap URL: not available (no DB snap entry for this camera)")

    if db_entry and proto in ("RTSP", "DVR"):
        db_streams = await _probe_db_streams(ip, port, enc_creds,
                                             db_entry, {url})
        # 2.4.0-rc2.6: PRESERVE the main stream URL we found pre-cred-
        # auth. Previously this code sorted ALL candidates (main + DB-
        # probed sub-streams) by resolution and replaced `url` with
        # whichever came out on top — which on Hikvision DS-2DE
        # produced incorrect results: probe_stream_details on
        # /Streaming/Channels/101 returned 704x480 (sub-stream
        # resolution), then DB probe on /102 returned the same data,
        # sorting was unstable, and /102 ended up as primary while
        # /101 (the actual 4MP main stream) became the "sub". Then
        # snap_loop streamed the wrong URL at the wrong resolution.
        # The fix: the pre-cred main URL is locked as primary. DB-
        # probed streams are PURE ADDITIONS to the profile list, never
        # replacements. The picked sub_url is the lowest-resolution of
        # the DB-probed additions only.
        primary_url = url
        primary_details = dict(details)
        # 2.4.0-rc2.9: capture sub_url's probed details (codec/res/fps)
        # so they can be carried into stream_profiles. rc2.6/rc2.7/rc2.8
        # all hardcoded sub_url's stream_profiles entry to None across
        # the board, which is why "Stream 2" showed in the dropdown
        # instead of "704x480 MJPEG" — the DB probe had captured the
        # data, but it was being thrown away at this step.
        # (sub_details is hoisted above to function scope to handle the
        # case where this if-block doesn't execute at all.)
        if db_streams:
            # All DB-probed candidates become alternative profiles;
            # never replace primary. Sub-stream picked from these.
            additions = list(db_streams)
            # Lowest-res addition becomes sub (for adaptive snap_loop)
            additions.sort(key=lambda c:
                (c.get("stream_width") or 0) * (c.get("stream_height") or 0))
            if additions:
                _sub_pick = additions[0]
                sub_url = _sub_pick["url"]
                # Strip the "url" key so what remains is the pure
                # details dict (codec/width/height/fps/profile).
                sub_details = {k: v for k, v in _sub_pick.items()
                               if k != "url"}
            if sub_url and sub_url != primary_url:
                log.info(f"  DB probe found sub stream: "
                         f"{_strip_creds(sub_url)}")
            elif sub_url == primary_url:
                # Defensive: if DB probe returned the SAME URL as
                # primary, don't double-count it as sub.
                sub_url = None
                sub_details = {}
        # Restore primary
        url = primary_url
        details = primary_details

    # 2.4.0-rc2.6: post-auth validation of locked-stream candidates
    # (Fix 3 + Fix 4 followup). After creds accepted, walk through
    # cam.locked_streams and validate each against the freshly-
    # authenticated camera. Successful ones get added to a new
    # additional_streams list on the camera record. Spurious
    # candidates (404s, RSTs, still-401-after-auth) get silently
    # dropped. This handles the case where the user enumerated 26
    # locked candidates via Deep Re-Probe but only ~6 are real
    # endpoints — the bogus ones disappear after auth.
    locked_in = camera.get("locked_streams", []) or []
    additional_streams: list[dict] = []
    if locked_in and proto in ("RTSP", "DVR"):
        log.info(f"  Validating {len(locked_in)} locked-stream "
                 f"candidate(s) post-auth")
        # Throttle-aware: respect brand cooldown between probes
        for idx, lk in enumerate(locked_in):
            lpath = lk.get("path", "")
            if not lpath:
                continue
            lurl_clear = f"rtsp://{ip}:{port}{lpath}"
            # Skip if it's already the primary or sub
            if lurl_clear == url or (sub_url and lurl_clear == sub_url):
                continue
            lurl_authed = build_authenticated_url({
                "stream_url": lurl_clear,
                "credentials": enc_creds,
                "ip": ip, "port": port, "protocol": "RTSP",
            }) or lurl_clear
            # Apply brand throttle cooldown if needed (Hikvision DS-2
            # has no cooldown, but Hipcam etc. do)
            if _t_s > 0:
                await _throttle_wait_if_needed(
                    ip, _t_s, f"locked-stream-validate {idx+1}")
            try:
                ok = await loop.run_in_executor(
                    _THREAD_POOL, probe_rtsp,
                    lurl_authed, "", "", 6.0)
                if ok:
                    log.info(f"  Locked-stream validated: "
                             f"{_strip_creds(lurl_authed)}")
                    # 2.4.0-rc2.8: capture resolution/codec for the
                    # validated stream so the focus-view dropdown can
                    # label it ("1920x1080 H264") instead of falling
                    # back to "Stream N". Brand throttle cooldown
                    # already applied above before probe_rtsp; we
                    # re-throttle here because probe_stream_details
                    # opens a fresh ffprobe TCP connection.
                    add_details: dict = {}
                    if _t_s > 0:
                        await _throttle_wait_if_needed(
                            ip, _t_s,
                            f"locked-stream-details {idx+1}")
                    try:
                        add_details = await probe_stream_details(
                            lurl_authed, "RTSP")
                    except Exception as ee:
                        log.debug(f"  probe_stream_details on "
                                  f"locked-stream {lpath}: {ee}")
                    additional_streams.append({
                        "path": lpath,
                        "url": lurl_clear,
                        "realm": lk.get("realm", ""),
                        "scheme": lk.get("scheme", ""),
                        # Resolution/codec fields for dropdown labels
                        "stream_width":  add_details.get("stream_width"),
                        "stream_height": add_details.get("stream_height"),
                        "stream_codec":  add_details.get("stream_codec"),
                        "stream_fps":    add_details.get("stream_fps"),
                    })
                else:
                    log.info(f"  Locked-stream NOT working "
                             f"(probably not a real endpoint): {lpath}")
            except Exception as e:
                log.debug(f"  Locked-stream validate exception "
                          f"({lpath}): {e}")
        log.info(f"  Locked-stream validation: "
                 f"{len(additional_streams)}/{len(locked_in)} "
                 f"validated as working")

    # 2.4.0-rc2.8: build stream_profiles explicitly. The non-ONVIF
    # cred-accept path used to leave stream_profiles empty, relying on
    # the dropdown's synth fallback at api_focus_get. That fallback
    # only built [main, sub] from stream_url/sub_stream_url and never
    # included additional_streams — so even when 3 locked candidates
    # were validated, the dropdown showed only 2 entries. Building
    # stream_profiles here means the canonical list is persisted on
    # the camera record and survives reload, the focus-view dropdown
    # gets all entries, and the snap_loop tier selection uses the
    # same source of truth as the UI.
    stream_profiles: list[dict] = []
    _seen_urls: set[str] = set()
    # Primary first
    if url and url not in _seen_urls:
        stream_profiles.append({
            "url":           url,
            "stream_width":  details.get("stream_width"),
            "stream_height": details.get("stream_height"),
            "stream_codec":  details.get("stream_codec"),
            "stream_fps":    details.get("stream_fps"),
        })
        _seen_urls.add(url)
    # DB-probed sub next
    if sub_url and sub_url not in _seen_urls:
        # 2.4.0-rc2.9: pull the captured details from sub_details
        # (populated up at the db_streams branch) instead of writing
        # None across the board.
        stream_profiles.append({
            "url":           sub_url,
            "stream_width":  sub_details.get("stream_width"),
            "stream_height": sub_details.get("stream_height"),
            "stream_codec":  sub_details.get("stream_codec"),
            "stream_fps":    sub_details.get("stream_fps"),
        })
        _seen_urls.add(sub_url)
    # Each validated locked-stream addition
    for add in additional_streams:
        au = add.get("url", "")
        if au and au not in _seen_urls:
            stream_profiles.append({
                "url":           au,
                "stream_width":  add.get("stream_width"),
                "stream_height": add.get("stream_height"),
                "stream_codec":  add.get("stream_codec"),
                "stream_fps":    add.get("stream_fps"),
            })
            _seen_urls.add(au)

    # 2.4.0-rc2.9: dedup stream_profiles on (codec, width, height).
    # When the same camera surfaces multiple URLs that resolve to
    # the same encoder/resolution combination — common on Hikvision
    # which exposes /Streaming/Channels/101 and /h.264/ch1/main/
    # av_stream and /Streaming/Channels/1 as three separate URLs all
    # backed by the same 2560x1440 HEVC encoder — the dropdown was
    # showing duplicates. First-discovery wins (pre-auth main →
    # DB-probed sub → validated locked candidates in walker order),
    # which gives users the canonical brand-recommended URL rather
    # than the alternate-form variant. Entries with None resolution
    # stay distinct (probe failed for some reason — better to keep
    # both than risk collapsing actually-different streams).
    _deduped: list[dict] = []
    _seen_keys: set = set()
    _dups_dropped = 0
    for sp in stream_profiles:
        w = sp.get("stream_width")
        h = sp.get("stream_height")
        c = sp.get("stream_codec")
        # Build dedup key. None values are preserved as-is and produce
        # a key that won't collide with concrete (w,h,codec) triples
        # OR with other None-bearing keys for different URLs — that
        # latter property is achieved by mixing the URL into the key
        # when any axis is None, ensuring "unknown" entries always
        # stay distinct.
        if w and h and c:
            key = ("known", c, w, h)
        else:
            # Any None → make key URL-unique so it can't collapse
            # against another unknown
            key = ("unknown", sp.get("url", ""))
        if key in _seen_keys:
            _dups_dropped += 1
            continue
        _seen_keys.add(key)
        _deduped.append(sp)
    if _dups_dropped:
        log.info(f"  stream_profiles dedup: dropped {_dups_dropped} "
                 f"duplicate entry/entries on (codec, width, height)")
    stream_profiles = _deduped
    log.info(f"  stream_profiles: built {len(stream_profiles)} "
             f"entry/entries (1 main + "
             f"{1 if sub_url else 0} sub + "
             f"{len(additional_streams)} validated locked)")

    camera.update(credentials=enc_creds,
                  stream_url=url, sub_stream_url=sub_url,
                  # 2.4.0-rc2.6: persist the validated additional
                  # streams. UI can offer them as alternative
                  # resolutions in the focus-view dropdown. Always
                  # written (may be empty list).
                  additional_streams=additional_streams,
                  # 2.4.0-rc2.8: persist stream_profiles built above
                  # so the focus-view dropdown reads the canonical
                  # list directly without needing the synth fallback.
                  stream_profiles=stream_profiles,
                  # 2.6.0-rc3.0 Item 4: only clear locked_streams if the
                  # user explicitly came in via the badge modal. The
                  # validated subset is in additional_streams either way
                  # (we always run post-auth validation above), so it's
                  # safe to keep the unvalidated list around. Users who
                  # logged in via the generic Login button keep the
                  # 🔒 badge visible and can review locked candidates
                  # later if they want.
                  locked_streams=([] if from_locked_modal
                                  else camera.get("locked_streams", []) or []),
                  requires_credentials=False,
                  status="ready", user_saved=True,
                  http_snap_url=http_snap_url,
                  http_snap_auth_mode=http_snap_auth_mode,
                  **details)
    # rc2.2: brand-id pass on the updated camera record. camera.update()
    # above merges fields onto the existing record (so manufacturer
    # survives if it was already set), but if pre-cred discovery never
    # identified the brand (no mac_vendor / no usable HTTP signals /
    # no captured RTSP server header), this catches the case where the
    # newly-stored stream_url + db_entry match would identify it now.
    if not camera.get("manufacturer"):
        try:
            _re_id = _identify_camera_brand(camera, force=True)
            if _re_id and _re_id["name"] != "Generic IP Camera":
                log.info(f"  Brand identified post-cred-auth: "
                         f"{_re_id['name']}")
        except Exception as e:
            log.debug(f"  brand-id (post-cred-auth RTSP): {e}")
    save_cameras()
    log.info(f"Credentials accepted for {camera_id}: {_strip_creds(url)}")
    # 2.5.0-rc1.2: kick off channel enumeration as a fire-and-forget
    # task. Channel-iterate brands (Lorex/Dahua DVR-NVR family) expose
    # multiple physical-camera channels under a single IP:port, and
    # the user's expectation (per the original 2026-05-02 plan) is
    # one card per populated channel. The task walks remaining
    # channels with the validated credentials and registers any
    # populated ones as additional CAMERAS entries. No-op for non-
    # channel_iterate brands. Doesn't block the cred-auth response —
    # the user gets confirmation immediately, additional cards
    # appear over the next few seconds as enumeration completes.
    #
    # 2.5.0-rc1.6: also set `dvr_enumeration_pending` on the response
    # so the UI knows to schedule a delayed loadCameras() refetch.
    # Without this hint, the UI's immediate post-cred-auth
    # loadCameras() runs BEFORE the background enumeration completes,
    # and there's no periodic /api/cameras poll, so the new cards
    # don't surface in the grid until the next user-initiated state
    # change. Field-confirmed in 2.5.0-rc1.5 log: cred-auth at 00:20:42,
    # 7 cards registered at 00:20:46, but UI didn't show them until
    # 00:22:06 when an unrelated periodic scan completed and that
    # scan's onComplete handler triggered loadCameras() as a side-
    # effect. Setting the flag bridges the gap deterministically.
    enum_pending = False
    try:
        _post_brand = _identify_camera_brand(camera) or {}
        _post_recipe = _post_brand.get("streaming_recipe") or {}
        if _post_recipe.get("type") == "channel_iterate":
            enum_pending = True
    except Exception:
        pass
    try:
        asyncio.create_task(
            _enumerate_dvr_channels_after_auth(camera_id))
    except Exception as e:
        log.debug(f"  channel-enum spawn: {e}")
    return web.json_response({"status": "ok",
                              "stream_url": _strip_creds(url),
                              "dvr_enumeration_pending": enum_pending,
                              **details})


async def api_clear_credentials(request: web.Request) -> web.Response:

    cid    = request.match_info["camera_id"]
    camera = CAMERAS.get(cid)
    if not camera:
        return web.json_response({"error": "Not found"}, status=404)
    camera.update(credentials=None,
                  stream_url=_strip_creds(camera.get("stream_url","")),
                  requires_credentials=True, status="needs_credentials")
    save_cameras()
    return web.json_response({"status": "ok"})

async def api_rename_camera(request: web.Request) -> web.Response:

    cid = request.match_info["camera_id"]
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if cid in CAMERAS:
        CAMERAS[cid]["name"]       = data.get("name", CAMERAS[cid]["name"])
        CAMERAS[cid]["user_saved"] = True
        save_cameras()
    return web.json_response({"status": "ok"})

async def api_delete_camera(request: web.Request) -> web.Response:

    cid = request.match_info["camera_id"]
    CAMERAS.pop(cid, None)
    save_cameras()
    return web.json_response({"status": "ok"})

async def api_confirm_camera(request: web.Request) -> web.Response:

    """User confirmed a post-upgrade missing camera — clear the flag."""
    cid    = request.match_info["camera_id"]
    camera = CAMERAS.get(cid)
    if not camera:
        return web.json_response({"error": "Not found"}, status=404)
    camera.pop("upgrade_missing", None)
    camera.pop("upgrade_missing_version", None)
    camera["user_saved"] = True
    save_cameras()
    return web.json_response({"status": "ok"})


async def api_not_camera(request: web.Request) -> web.Response:

    cid = request.match_info["camera_id"]
    cam = CAMERAS.get(cid)
    if not cam:
        return web.json_response({"error": "Not found"}, status=404)

    reason_type   = "unknown"
    reason_detail = ""
    share         = False
    try:
        data          = await request.json()
        reason_type   = data.get("reason_type", "unknown")
        reason_detail = data.get("reason_detail", "").strip()[:200]
        share         = bool(data.get("share", False))
    except Exception:
        pass

    BLACKLIST.add(cam["ip"])
    BLACKLIST.add(cid)

    fingerprint = build_fingerprint(cam)
    record = {
        "cid":           cid,
        "reason_type":   reason_type,
        "reason_detail": reason_detail,
        "fingerprint":   fingerprint,
        "share":         share,
        "added_at":      datetime.datetime.utcnow().isoformat(),
        "version":       CURRENT_VERSION,
    }
    FEEDBACK[cid] = record
    save_feedback()

    log.info(f"Not-a-camera: {cid} | reason={reason_type}"
             + (f" | {reason_detail}" if reason_detail else "")
             + (" | share=yes" if share else ""))

    CAMERAS.pop(cid, None)
    save_cameras()
    save_blacklist()

    if share and COMMUNITY_ENDPOINT:
        asyncio.create_task(submit_to_community(record))

    return web.json_response({"status": "ok"})


# ─────────────────────────────────────────────────────────────────────────
# 2.4.0-rc2.4: Deep Re-Probe handler
# ─────────────────────────────────────────────────────────────────────────
async def api_deep_reprobe(request: web.Request) -> web.Response:
    """User-invoked deep re-probe of a single camera card.

    Resumes Layer 1 from where rc2.4's early-bail left off, then runs
    the full Layer 2 multi-socket walk if it was skipped. Designed as
    the escape hatch for the rc2.4 aggressive Layer 1/Layer 2 skip
    heuristics: when we early-bail Layer 1 after 5 consecutive same-
    realm 401s and skip Layer 2, the user may legitimately want to
    verify that no firmware-quirk path exists. This handler delivers
    that verification on demand.

    State machine (driven by cam.early_bail_reason):
      • "layer1_consecutive_401s" — Layer 1 bailed early but Layer 2
        was still attempted and bailed normally. Resume Layer 1 on
        cam.early_bail_paths_remaining.
      • "layer1_then_layer2_skipped_401s" — Layer 1 bailed early AND
        Layer 2 was skipped (rc2.4 default). Resume Layer 1 on
        remaining paths, THEN run full Layer 2 walk.
      • unset/missing — no early bail recorded. Run a fresh full
        Layer 1 + Layer 2 walk.

    Staleness check: if cam.early_bail_at is older than 30 minutes,
    discard the saved state and run a fresh full probe instead. Avoids
    resuming a walk against a host that's been rebooted, repurposed,
    or replaced since the original scan.

    Concurrency: cam.deep_reprobe_in_progress flag suppresses
    overlapping invocations. Frontend disables the button while the
    handler is in flight.
    """
    cid = request.match_info["camera_id"]
    cam = CAMERAS.get(cid)
    if not cam:
        return web.json_response({"error": "Not found"}, status=404)

    if cam.get("deep_reprobe_in_progress"):
        return web.json_response(
            {"error": "Deep re-probe already in progress for this card"},
            status=409,
        )

    ip   = cam["ip"]
    port = cam["port"]
    bail_reason     = cam.get("early_bail_reason", "")
    paths_remaining = cam.get("early_bail_paths_remaining", []) or []
    bail_at         = cam.get("early_bail_at", "")

    # Staleness — 30 minutes
    stale = False
    if bail_at:
        try:
            bail_dt = datetime.datetime.fromisoformat(bail_at)
            age_s = (datetime.datetime.utcnow() - bail_dt).total_seconds()
            if age_s > 1800:
                stale = True
                log.info(f"  Deep Re-Probe: cached bail state for {cid} "
                         f"is stale ({age_s:.0f}s old) — running fresh "
                         f"full probe instead of resuming")
        except Exception:
            stale = True

    cam["deep_reprobe_in_progress"] = True
    save_cameras()

    loop = asyncio.get_event_loop()
    log.info(f"Deep Re-Probe started: {cid} (reason={bail_reason!r}, "
             f"remaining={len(paths_remaining)} paths, stale={stale})")

    # Build host_meta from camera record so brand-aware logic still
    # works inside the resumed walk
    host_meta = {
        "ip":             ip,
        "hostname":       cam.get("hostname", ip),
        "mac_addr":       cam.get("mac_addr", ""),
        "mac_vendor":     cam.get("mac_vendor", ""),
        "vendor":         cam.get("mac_vendor", ""),
        "manufacturer":   cam.get("manufacturer", ""),
        "page_title":     cam.get("page_title", ""),
        "server_header":  cam.get("server_header", ""),
        "rtsp_server_header": cam.get("rtsp_server_header", ""),
        "rtsp_auth_realm":    cam.get("rtsp_auth_realm", ""),
        "rtsp_auth_scheme":   cam.get("rtsp_auth_scheme", ""),
        "rtsp_public_methods": cam.get("rtsp_public_methods", ""),
        "onvif_scopes":   cam.get("onvif_scopes", ""),
    }

    found_url: str | None = None
    new_locked: list[dict] = []

    try:
        # Decide: resume Layer 1, run Layer 2, or both. If the cached
        # state is stale OR no early-bail was recorded, do a fresh full
        # find_rtsp_path call (which itself runs Layer 1 + Layer 2).
        run_resume_layer1 = (
            not stale
            and bail_reason in ("layer1_consecutive_401s",
                                "layer1_then_layer2_skipped_401s")
            and bool(paths_remaining)
        )
        # 2.4.0-rc2.9: REVERTING rc2.8's Fix 5. rc2.8 dropped the rc2.5
        # expansion that forced Stage B (Layer 2) to run during Deep Re-
        # Probe on skip_layer2 brands. Reasoning at the time was that
        # skip_layer2 brands "guaranteed" failure on Layer 2. But the
        # user explicitly wanted Deep Re-Probe to be the catch-everything
        # safety net — overriding ALL skip flags including skip_layer2 —
        # specifically for the rare firmware-quirk cases where Layer 2's
        # fresh-socket-per-path semantics could reveal something Layer 1
        # missed. Three documented scenarios:
        #   1. Sub-stream paths that only respond on a fresh socket
        #      (some cheap firmwares have buggy session state where
        #      the second DESCRIBE on a single socket returns garbage,
        #      but a fresh socket returns clean 200/401).
        #   2. Servers that close the socket after first 401 (older
        #      Foscam-family clones); Layer 1 walk prematurely ends,
        #      Layer 2 re-opens and continues.
        #   3. Token-bucket throttles that reset between sockets —
        #      single-socket walk hits the throttle, multi-socket walk
        #      with 5s cooldown doesn't.
        # The 45s Layer 2 wait on Hikvision Deep Re-Probe is the
        # accepted cost of this safety net. User-requested behavior.
        brand_has_skip_layer2 = False
        try:
            _be = _identify_camera_brand(host_meta)
            brand_has_skip_layer2 = bool(
                _be and _be.get("skip_layer2", False))
        except Exception:
            pass
        run_layer2_followup = (
            not stale
            and (bail_reason == "layer1_then_layer2_skipped_401s"
                 or (bail_reason == "layer1_consecutive_401s"
                     and brand_has_skip_layer2))
        )
        if not run_resume_layer1 and not run_layer2_followup:
            # Fresh full probe — let find_rtsp_path do its thing
            log.info(f"  Deep Re-Probe stage: full fresh probe (Layer 1 + "
                     f"Layer 2)")
            # Clear any stale early-bail state so the new walk doesn't
            # re-trigger the rc2.4 short-circuits with old data.
            for k in ("early_bail_reason", "early_bail_realm",
                      "early_bail_paths_tried", "early_bail_paths_remaining",
                      "early_bail_at"):
                host_meta.pop(k, None)
            found_url = await loop.run_in_executor(
                _THREAD_POOL, find_rtsp_path, ip, port, "", "", host_meta)
            new_locked = host_meta.get("locked_streams", []) or []
        else:
            # Stage A: resume Layer 1 walk on the unwalked paths.
            # Run with collect_locked=True so 401s on the remaining
            # paths surface as locked-stream candidates the user can
            # later unlock by entering credentials.
            if run_resume_layer1:
                log.info(f"  Deep Re-Probe Stage A: resume Layer 1 "
                         f"({len(paths_remaining)} paths remaining)")
                expected_realm = host_meta.get("rtsp_auth_realm", "") or ""
                # 2.4.0-rc2.6: identify brand recipe to filter locked
                # candidates by paths the brand actually serves. Without
                # this, Hikvision (and similar 401-everything cameras)
                # produce 26+ bogus locked candidates that include
                # paths from completely different brand recipes
                # (Foscam, Dahua, etc.). With it, only paths matching
                # the brand's known stream URLs surface as candidates.
                _stage_a_brand_paths: list[str] = []
                try:
                    _stage_a_be = _identify_camera_brand(host_meta)
                    if _stage_a_be:
                        _cam_with_brand = dict(host_meta)
                        _cam_with_brand["manufacturer"] = _stage_a_be.get(
                            "name", "")
                        _stage_a_sdb = _match_stream_db(_cam_with_brand)
                        if _stage_a_sdb:
                            _stage_a_brand_paths = list(
                                _stage_a_sdb.get("rtsp", []))
                except Exception:
                    pass
                # 2.4.0-rc2.5: deep_reprobe_mode=True disables the
                # early-bail counter (we want a full walk) and
                # bypasses the found_working_url gate on locked-
                # stream collection (so 401s get surfaced as
                # candidates even when no unauth stream exists).
                found_a, _looks_rtsp = await loop.run_in_executor(
                    _THREAD_POOL, _probe_rtsp_paths_single_socket,
                    ip, port, paths_remaining, "", "",
                    6.0, "", f"deep-reprobe:{cid}", host_meta,
                    True, expected_realm, True,  # deep_reprobe_mode
                    _stage_a_brand_paths,  # brand_recipe_paths
                )
                if found_a:
                    found_url = found_a
                    log.info(f"  Deep Re-Probe Stage A → "
                             f"found working stream: {found_a}")
                # Pick up any locked streams Stage A captured
                stage_a_locked = host_meta.get("locked_streams", []) or []
                # Merge with whatever was on the camera before
                existing_locked = cam.get("locked_streams", []) or []
                seen_paths = {l.get("path") for l in existing_locked}
                for l in stage_a_locked:
                    if l.get("path") not in seen_paths:
                        existing_locked.append(l)
                        seen_paths.add(l.get("path"))
                new_locked = existing_locked
                log.info(f"  Deep Re-Probe Stage A: found "
                         f"{len(stage_a_locked)} locked candidate(s) "
                         f"on resumed paths")

            # Stage B: full Layer 2 multi-socket walk if it was
            # skipped during the original scan. Even if Stage A
            # already found a working stream, we still skip Stage B
            # in that case (no need to grind).
            if run_layer2_followup and not found_url:
                log.info(f"  Deep Re-Probe Stage B: running Layer 2 "
                         f"(skipped during original scan)")
                # Build the full ordered path list the same way
                # find_rtsp_path does, so Layer 2 walks the canonical
                # candidate set — not just the remaining-from-Stage-A
                # list.
                brand_entry: dict | None = None
                try:
                    brand_entry = _identify_camera_brand(host_meta)
                except Exception:
                    pass
                brand_name = (brand_entry or {}).get("name", "")
                db_paths_b: list[str] = []
                if brand_name:
                    cam_with_brand = dict(host_meta)
                    cam_with_brand["manufacturer"] = brand_name
                    sdb = _match_stream_db(cam_with_brand)
                    if sdb:
                        db_paths_b = list(sdb.get("rtsp", []))
                seen_b: set[str] = set()
                ordered_b: list[str] = []
                for p in db_paths_b + RTSP_PATHS:
                    if p not in seen_b:
                        seen_b.add(p)
                        ordered_b.append(p)
                # Inline a Layer-2-only walk (5s cooldown, bail-after-10)
                def _layer2_only() -> str | None:
                    consec = 0
                    for i, path in enumerate(ordered_b):
                        if i > 0:
                            time.sleep(5.0)
                        url2 = f"rtsp://{ip}:{port}{path}"
                        if probe_rtsp(url2, "", "", timeout=6.0):
                            log.info(f"  RTSP OK (Deep Re-Probe Layer 2): "
                                     f"{url2}")
                            return url2
                        consec += 1
                        if consec >= 10:
                            log.info(f"  Deep Re-Probe Layer 2 bailing "
                                     f"after {consec} consecutive failures")
                            break
                    return None
                found_b = await loop.run_in_executor(
                    _THREAD_POOL, _layer2_only)
                if found_b:
                    found_url = found_b

        # Update the camera record with the outcome
        if found_url:
            cam["stream_url"] = found_url
            cam["protocol"] = "RTSP"
            cam["status"] = "ready"
            cam["requires_credentials"] = False
            log.info(f"  Deep Re-Probe SUCCESS: {cid} → "
                     f"{_strip_creds(found_url)}")
        else:
            log.info(f"  Deep Re-Probe: no working stream found for "
                     f"{cid} (locked candidates: {len(new_locked)})")

        if new_locked:
            cam["locked_streams"] = new_locked

        # Clear early-bail state — we did the deep work, no more
        # skipped paths to resume.
        for k in ("early_bail_reason", "early_bail_realm",
                  "early_bail_paths_tried", "early_bail_paths_remaining",
                  "early_bail_at"):
            cam.pop(k, None)

        # Record outcome stats
        cam["deep_reprobe_attempts"] = int(
            cam.get("deep_reprobe_attempts", 0)) + 1
        cam["deep_reprobe_last_at"] = datetime.datetime.utcnow().isoformat()
        cam["deep_reprobe_last_outcome"] = (
            "ready" if found_url
            else (f"locked_streams:{len(new_locked)}"
                  if new_locked else "no_streams"))
    except Exception as e:
        log.error(f"Deep Re-Probe error for {cid}: {e}", exc_info=True)
        cam["deep_reprobe_last_outcome"] = f"error:{str(e)[:80]}"
    finally:
        cam["deep_reprobe_in_progress"] = False
        save_cameras()

    return web.json_response({
        "status":         "ok",
        "found_stream":   bool(found_url),
        "stream_url":     _strip_creds(found_url) if found_url else "",
        "locked_count":   len(new_locked),
        "outcome":        cam.get("deep_reprobe_last_outcome", ""),
    })


async def api_add_camera(request: web.Request) -> web.Response:

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    ip        = data.get("ip","").strip()
    port      = int(data.get("port", 554))
    protocol  = data.get("protocol","RTSP").upper()
    name      = data.get("name","").strip() or f"{ip}:{port}"
    username  = data.get("username","").strip()
    password  = data.get("password","")
    rtsp_path = data.get("rtsp_path","").strip()

    if not ip:
        return web.json_response({"error": "IP is required"}, status=400)

    cid  = f"{ip}_{port}_manual"
    loop = asyncio.get_event_loop()
    url  = None

    if protocol in ("RTSP", "DVR", "ONVIF"):
        if rtsp_path:
            test_url = f"rtsp://{ip}:{port}{rtsp_path}"
            if await loop.run_in_executor(_THREAD_POOL, probe_rtsp, test_url, username, password):
                url = test_url
        if not url:
            url = await loop.run_in_executor(_THREAD_POOL, find_rtsp_path, ip, port, username, password)
    elif protocol == "MJPEG":
        url = await loop.run_in_executor(_THREAD_POOL, probe_mjpeg_http, ip, port, username, password)
    elif protocol == "HLS":
        url = await loop.run_in_executor(_THREAD_POOL, probe_hls, ip, port, username, password)
    elif protocol == "RTMP":
        if await loop.run_in_executor(_THREAD_POOL, probe_rtmp, ip, port):
            url = f"rtmp://{ip}:{port}/live/stream"

    if not url and protocol not in ("WebRTC", "WS-RTSP"):
        return web.json_response(
            {"error": f"Could not connect to {ip}:{port} via {protocol}. "
                      "Check IP, port, protocol and credentials."}, status=400)

    CAMERAS[cid] = {
        "id": cid, "ip": ip, "hostname": ip, "port": port,
        "protocol": protocol, "stream_url": url or "",
        "requires_credentials": False,
        "credentials": encrypt_creds(username, password) if (username and url) else None,
        "name": name, "status": "ready" if url else "info",
        "display": "proxy" if url else protocol.lower(),
        "user_saved": True, "verdict": "camera", "verdict_reason": "Manually added",
    }
    save_cameras()
    return web.json_response({"status": "ok", "camera_id": cid})


async def api_arp_hosts(request: web.Request) -> web.Response:

    """Return the last ARP-discovered host list for the Port Scan UI."""
    return web.json_response(anycam_scan.ARP_HOSTS)


async def api_pscan_start(request: web.Request) -> web.Response:

    try:
        data = await request.json()
        ip   = data.get("ip","").strip()
        ips  = data.get("ips", [])  # batch: list of IPs
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    if PSCAN["running"]:
        return web.json_response({"error": "Scan already running"}, status=409)

    if ips:
        # Batch mode: queue all IPs, scan sequentially
        PSCAN_QUEUE.clear()
        PSCAN_QUEUE.extend([i.strip() for i in ips if i.strip()])
        if not PSCAN_QUEUE:
            return web.json_response({"error": "No valid IPs"}, status=400)
        asyncio.create_task(run_batch_port_scan())
        return web.json_response({"status": "started", "count": len(PSCAN_QUEUE)})

    if not ip:
        return web.json_response({"error": "IP required"}, status=400)
    asyncio.create_task(run_port_scan(ip))
    return web.json_response({"status": "started"})


async def run_batch_port_scan() -> None:

    """Run port scans sequentially for all IPs in PSCAN_QUEUE."""
    total = len(PSCAN_QUEUE)
    all_results = []
    for idx, ip in enumerate(list(PSCAN_QUEUE)):
        PSCAN.update(
            running=True, ip=ip, progress=int(100 * idx / total),
            message=f"Scanning {ip} ({idx+1}/{total})…",
        )
        await run_port_scan(ip)
        # Prefix each result with the IP it came from
        for r in PSCAN["results"]:
            r["scanned_ip"] = ip
        all_results.extend(PSCAN["results"])
    PSCAN.update(
        running=False, progress=100, results=all_results,
        message=f"Batch scan complete — {total} host(s), {len(all_results)} open port(s) total.",
        ip="",
    )
    PSCAN_QUEUE.clear()

async def api_pscan_status(request: web.Request) -> web.Response:

    # Add current elapsed so JS can compute drift between polls
    resp = dict(PSCAN)
    if resp.get("scan_start") and resp.get("running"):
        resp["elapsed"] = round(time.time() - resp["scan_start"], 1)
    resp.pop("live_ports", None)  # send separately to avoid huge payload
    resp["live_ports"] = PSCAN.get("live_ports", [])
    return web.json_response(resp)

async def api_pscan_cancel(request: web.Request) -> web.Response:

    pid = PSCAN.get("proc_pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    PSCAN.update(running=False, paused=False, message="Cancelled.", proc_pid=None)
    return web.json_response({"status": "ok"})

async def api_pscan_pause(request: web.Request) -> web.Response:

    pid = PSCAN.get("proc_pid")
    if pid and PSCAN["running"] and not PSCAN["paused"]:
        try:
            os.kill(pid, signal.SIGSTOP)
            PSCAN["paused"]  = True
            PSCAN["message"] = f"Paused — {len(PSCAN['results'])} port(s) found so far."
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"status": "ok"})

async def api_pscan_resume(request: web.Request) -> web.Response:

    pid = PSCAN.get("proc_pid")
    if pid and PSCAN["paused"]:
        try:
            os.kill(pid, signal.SIGCONT)
            PSCAN["paused"]  = False
            PSCAN["message"] = f"Resumed scan of {PSCAN['ip']}…"
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"status": "ok"})

# ─────────────────────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Core streaming / scan functions
# ─────────────────────────────────────────────────────────────────────────────

def build_authenticated_url(camera: dict, url_key: str = "stream_url",
                            url: str | None = None) -> str | None:
    """Return stream URL with credentials embedded, or None if no URL.

    Lookup order:
      1. If `url=` is passed, use that string directly (preferred for
         adaptive-tier launch, where the profile entry stores the URL).
      2. Otherwise, look up `camera[url_key]`. NO fallback to stream_url
         when an explicit non-default url_key is requested — silently
         substituting the main stream's URL when sub_stream_url is None
         caused manual tier changes to launch ffmpeg with the wrong URL
         (rc2.4 Microseven sub-stream probe failure → sub_stream_url=None
         → profile[1] launched on /11 instead of /12; "1280x720" label
         on actual 4K stream).

    Credentials are percent-encoded per RFC 3986 §3.2.1 so that special
    characters in passwords (e.g. '!' '?' '@' '#' '%') don't corrupt the
    URL. The safe set matches characters that RTSP/HTTP stacks accept
    raw in the userinfo component without confusion.
    """
    if url is None:
        url = camera.get(url_key)
    if not url:
        return None
    creds = camera.get("credentials")
    if creds:
        try:
            from urllib.parse import quote as _q
            u, p = decrypt_creds(creds)
            # RFC 3986 userinfo safe chars (never need encoding in user:pass)
            _SAFE = "!$&'()*+,;=-._~"
            u_enc = _q(u, safe=_SAFE)
            p_enc = _q(p, safe=_SAFE)
            proto, rest = url.split("://", 1)
            rest = re.sub(r"^[^@]+@", "", rest)   # strip any existing creds
            url  = f"{proto}://{u_enc}:{p_enc}@{rest}"
        except Exception as ex:
            log.warning(f"build_authenticated_url decrypt: {ex}")
    return url


async def probe_stream_details(url: str, proto: str) -> dict:
    """
    Run ffprobe on a confirmed stream URL to extract codec, resolution,
    FPS, and audio info.  Called after credentials are accepted.
    Returns a flat dict of stream_* fields, empty on failure.
    """
    extra = ["-rtsp_transport", "tcp"] if proto in ("RTSP", "DVR", "ONVIF") else []
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", *extra,
            "-show_streams", "-print_format", "json", "-i", url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
        if proc.returncode != 0:
            return {}
        streams = json.loads(out.decode("utf-8", errors="replace")).get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        result: dict = {}
        if video:
            try:
                n, d = video.get("avg_frame_rate", "0/1").split("/")
                fps = round(int(n) / int(d), 1) if int(d) else 0
            except Exception:
                fps = 0
            result.update({
                "stream_codec":   video.get("codec_name", ""),
                "stream_width":   video.get("width"),
                "stream_height":  video.get("height"),
                "stream_fps":     fps,
                "stream_profile": video.get("profile", ""),
            })
        if audio:
            result["stream_audio"] = audio.get("codec_name", "")
        log.info(f"  probe_stream_details: {result}")
        return result
    except Exception as ex:
        log.debug(f"probe_stream_details: {ex}")
        return {}














# ─────────────────────────────────────────────────────────────────────────────
# Routing
# ─────────────────────────────────────────────────────────────────────────────

async def api_set_log_level(request: web.Request) -> web.Response:
    """
    POST /api/log_level   body: {"level": "DEBUG"|"INFO"|"WARNING"|"ERROR"}
    Adjusts the anycam logger level at runtime without restart.
    Updates the environment variable and refreshes the _LevelFilter.
    """
    try:
        body      = await request.json()
        level_str = str(body.get("level", "")).upper()
        if level_str not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            return web.json_response(
                {"error": f"Unknown level '{level_str}'. Use DEBUG/INFO/WARNING/ERROR."},
                status=400
            )
        # Toggle the single named level on; leave others as configured by HA
        env_key = f"LOG_{level_str}"
        os.environ[env_key] = "true"
        _level_filter._refresh()
        log.info(f"Log level {level_str} enabled at runtime")
        return web.json_response({"status": "ok", "level": level_str})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=400)


def make_app() -> web.Application:
    app = web.Application()
    # Graceful shutdown — fired by runner.cleanup() in main() when SIGTERM
    # or SIGINT sets _STOP_EVENT.
    app.on_shutdown.append(_on_shutdown)
    app.router.add_get(   "/",                                    handle_index)
    app.router.add_get(   "/api/cameras",                         api_cameras)
    app.router.add_get(   "/api/scan/status",                     api_scan_status)
    app.router.add_post(  "/api/scan",                            api_scan)
    app.router.add_post(  "/api/scan/cancel",                     api_scan_cancel)
    app.router.add_post(  "/api/credentials",                     api_set_credentials)
    app.router.add_get(   "/api/dvr_enum/status/{camera_id}",     api_dvr_enum_status)
    app.router.add_delete("/api/cameras/{camera_id}/credentials", api_clear_credentials)
    app.router.add_post(  "/api/cameras/{camera_id}/name",        api_rename_camera)
    app.router.add_post(  "/api/cameras/{camera_id}/confirm",     api_confirm_camera)
    app.router.add_post(  "/api/cameras/{camera_id}/not_camera",  api_not_camera)
    app.router.add_post(  "/api/cameras/{camera_id}/deep_reprobe", api_deep_reprobe)
    app.router.add_delete("/api/cameras/{camera_id}",             api_delete_camera)
    app.router.add_post(  "/api/cameras/add",                     api_add_camera)
    app.router.add_get(   "/snapshot/{camera_id}",                handle_snapshot)
    app.router.add_get(   "/snap/status",                         handle_snap_status)
    # 2.6.3 — Tier 2 go2rtc live view. Each handler answers "not available"
    # itself when go2rtc is not running.
    app.router.add_get(   "/api/go2rtc/focus/{camera_id}",        api_go2rtc_focus)
    app.router.add_get(   "/api/go2rtc/card/{camera_id}",         api_go2rtc_card)
    app.router.add_get(   "/go2rtc/ws",                           handle_go2rtc_ws)
    app.router.add_get(   "/go2rtc/video-rtc.js",                 handle_go2rtc_player_js)
    app.router.add_post(  "/api/log_level",                        api_set_log_level)
    app.router.add_get(   "/api/logs",                            api_logs)
    app.router.add_post(  "/snap/focus/{camera_id}",              handle_focus_set)
    app.router.add_delete("/snap/focus",                          handle_focus_clear)
    app.router.add_post(  "/api/cameras/{camera_id}/motion",      api_motion_toggle)
    app.router.add_get(   "/api/cameras/{camera_id}/motion",      api_motion_status)
    app.router.add_get(   "/api/motion",                          api_motion_all)
    app.router.add_route("*", "/api/cameras/{camera_id}/motion/settings", api_motion_settings)
    app.router.add_get(   "/api/storage",                         api_storage_list)
    app.router.add_post(  "/api/storage/rename",                  api_storage_rename)
    app.router.add_post(  "/api/storage/move",                    api_storage_move)
    app.router.add_delete("/api/storage/file",                    api_storage_delete)
    app.router.add_get(   "/api/storage/download",                api_storage_download)
    app.router.add_get(   "/api/arp_hosts",                        api_arp_hosts)
    app.router.add_post(  "/api/pscan/start",                     api_pscan_start)
    app.router.add_get(   "/api/pscan/status",                    api_pscan_status)
    app.router.add_post(  "/api/pscan/cancel",                    api_pscan_cancel)
    app.router.add_post(  "/api/pscan/pause",                     api_pscan_pause)
    app.router.add_post(  "/api/pscan/resume",                    api_pscan_resume)

    return app


async def _probe_hw_decoders() -> None:
    """
    Probe hardware decoder availability once at startup.

    2.6.0-rc2.3: structurally rewritten. The candidate list is now
    (label, codec, ffmpeg_args) triples — see the comment block at the
    _HW_DECODER_CANDIDATES definition. Each candidate is either:

      • hwaccel-style — args contains "-hwaccel <name> -c:v <codec>"
        (e.g. hevc_drm uses "-hwaccel drm -c:v hevc"). Probed by
        verifying that ffmpeg's -hwaccels list contains <name> AND, for
        the drm hwaccel specifically, that rpivid is loaded
        (/dev/video19 + /dev/media0 both present). No synthetic decode
        — initial proof was an end-to-end live test against the
        Hikvision camera on CrystalHeeler's Pi 4 in 2.6.0-rc2.1's debug
        cycle. If a real stream fails at runtime, snap_loop's existing
        per-stream hw-fallback handler catches it and adds the label to
        _HW_UNAVAILABLE.

      • decoder-style — args contains "-c:v <name>" only (no hwaccel),
        e.g. h264_v4l2m2m. Probed by encoding a small H264/HEVC test
        clip and decoding it via the candidate's args. 2.6.0-rc2.3 adds
        -pix_fmt yuv420p + -profile:v baseline to the encode step so
        the test clip uses a profile bcm2835-codec accepts; the rc2.1
        regression where h264_v4l2m2m showed unavailable on Pi 4 was
        libx264 defaulting to High 4:4:4 Predictive (profile 244) which
        the HW decoder rejects.

    CFG_HW_DECODE gate: when the toggle is off, the entire probe skips.
    No ffmpeg subprocesses launched, no candidates marked
    available/unavailable, snap_loop and the live MJPEG endpoint both
    fall through to software decode via their own gates. The toggle is
    the single switch.

    Logs:
      Hardware decode disabled by config — skipping probe   (toggle off)
      Probing hardware decoder availability...              (toggle on)
        <label>: available (<how>)
        <label>: unavailable (<reason>)
      HW decoders available: <comma list> | No hardware decoders available
    """
    if not CFG_HW_DECODE:
        log.info("Hardware decode disabled by config — skipping probe")
        _HW_PROBED.set()
        return

    log.info("Probing hardware decoder availability...")

    # Static queries up-front: ffmpeg -decoders and ffmpeg -hwaccels.
    # Each candidate is then dispatched to the right test based on
    # whether its args use -hwaccel or only -c:v.
    decoder_list = ""
    hwaccel_list = ""
    try:
        ld = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-decoders",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(ld.communicate(), timeout=10)
        decoder_list = out.decode("utf-8", errors="replace")
    except Exception:
        pass
    try:
        lh = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-hwaccels",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(lh.communicate(), timeout=10)
        hwaccel_list = out.decode("utf-8", errors="replace")
    except Exception:
        pass

    available: list[str] = []
    for label, codec, args in _HW_DECODER_CANDIDATES:
        is_hwaccel = "-hwaccel" in args

        if is_hwaccel:
            hwaccel_idx  = args.index("-hwaccel")
            hwaccel_name = args[hwaccel_idx + 1]
            in_hwaccels = bool(re.search(
                rf"^\s*{re.escape(hwaccel_name)}\s*$",
                hwaccel_list, re.MULTILINE))
            if not in_hwaccels:
                _HW_UNAVAILABLE.add(label)
                reason = (f"ffmpeg does not list '{hwaccel_name}' as a "
                          f"hwaccel — needs ffmpeg built with the "
                          f"matching --enable-* flag (e.g. --enable-libdrm "
                          f"for drm, --enable-vaapi for vaapi)")
                log.info(f"  {label}: unavailable ({reason})")
                continue
            # drm hwaccel needs rpivid kernel module loaded on Pi 4/5.
            # Without it, ffmpeg accepts -hwaccel drm at parse time but
            # the actual decoder open fails at first packet — better to
            # catch that here than waste a snap_loop launch on it.
            if hwaccel_name == "drm":
                rpivid_loaded = (os.path.exists("/dev/video19")
                                 and os.path.exists("/dev/media0"))
                if not rpivid_loaded:
                    _HW_UNAVAILABLE.add(label)
                    reason = ("rpivid not loaded — /dev/video19 or "
                              "/dev/media0 missing. Add 'dtoverlay="
                              "rpivid-v4l2' to /boot/firmware/config.txt "
                              "and reboot the Pi.")
                    log.info(f"  {label}: unavailable ({reason})")
                    continue
            available.append(label)
            log.info(f"  {label}: available (via -hwaccel {hwaccel_name})")
            continue

        # decoder-style: -c:v <name> only.
        try:
            decoder_name = args[args.index("-c:v") + 1]
        except (ValueError, IndexError):
            _HW_UNAVAILABLE.add(label)
            log.info(f"  {label}: unavailable (malformed candidate args)")
            continue

        in_decoders = bool(re.search(rf"\b{re.escape(decoder_name)}\b",
                                      decoder_list))
        if not in_decoders:
            _HW_UNAVAILABLE.add(label)
            log.info(f"  {label}: unavailable "
                     f"(decoder '{decoder_name}' not in ffmpeg -decoders)")
            continue

        try:
            # Encode a tiny test clip with conservative profile so common
            # HW decoders (bcm2835-codec, generic vaapi) accept it. The
            # rc2.1 regression that prompted this: libx264 defaulted to
            # High 4:4:4 Predictive on the simple test pattern, which
            # bcm2835-codec rejected with rc=1.
            prof_args = (["-profile:v", "baseline"] if codec == "h264"
                         else ["-profile:v", "main"])
            enc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=black:s=64x64:d=0.2",
                "-c:v", ("libx264" if codec == "h264" else "libx265"),
                "-pix_fmt", "yuv420p",
                *prof_args,
                "-f", "matroska", "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            encoded, _ = await asyncio.wait_for(enc.communicate(), timeout=10)
            if not encoded:
                _HW_UNAVAILABLE.add(label)
                log.info(f"  {label}: unavailable (encode failed)")
                continue

            dec_proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                *args,
                "-i", "pipe:0",
                "-f", "null", "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                dec_proc.communicate(input=encoded), timeout=10)
            stderr_s = stderr.decode("utf-8", errors="replace")

            if (dec_proc.returncode == 0
                    and "not compiled" not in stderr_s
                    and "Could not find" not in stderr_s
                    and "Invalid" not in stderr_s):
                available.append(label)
                log.info(f"  {label}: available (synthetic decode passed)")
            else:
                _HW_UNAVAILABLE.add(label)
                reason = ("not compiled into ffmpeg"
                          if "not compiled" in stderr_s
                          else ("device not found"
                                if "Could not find" in stderr_s
                                else f"rc={dec_proc.returncode}"))
                log.info(f"  {label}: unavailable ({reason})")
        except asyncio.TimeoutError:
            _HW_UNAVAILABLE.add(label)
            log.info(f"  {label}: unavailable (probe timed out)")
        except Exception as e:
            _HW_UNAVAILABLE.add(label)
            log.info(f"  {label}: unavailable ({e})")

    if available:
        log.info(f"HW decoders available: {', '.join(available)}")
    else:
        log.info("No hardware decoders available — using software decode")


# ─────────────────────────────────────────────────────────────────────────────
# Graceful shutdown
# ─────────────────────────────────────────────────────────────────────────────
# Module-level Event so signal handlers (registered in main()) can flip it.
# When set, main() falls through to runner.cleanup() which fires the
# app.on_shutdown chain (registered in make_app()).
_STOP_EVENT: asyncio.Event | None = None


async def _on_shutdown(app: web.Application) -> None:
    """Graceful shutdown handler — registered via app.on_shutdown.append().

    Sequence:
      1. Persist last_frame_wall to cameras.json so the UI can show
         "last seen N minutes ago" after the next startup.
      2. Cancel all running snap_loop asyncio tasks.
      3. SIGTERM all live ffmpeg child processes (both snap_loop and
         motion-recording), wait up to 3s, then SIGKILL stragglers.
      4. Shut down _THREAD_POOL with cancel_futures=True so queued
         nmap/probe_rtsp jobs don't block exit.

    Errors in any single step are logged but do not stop the rest of
    the sequence — best-effort cleanup is the priority.
    """
    log.info("Graceful shutdown initiated...")

    # ── 1. Persist last_frame_wall ─────────────────────────────────────────
    # frame_time is monotonic (resets every process start). Convert to
    # wall-clock by computing how long ago the last frame arrived and
    # subtracting that from time.time().
    now_mono     = time.monotonic()
    now_wall     = time.time()
    saved_count  = 0
    for cam_id, state in _SNAP.items():
        ft = state.get("frame_time") or 0.0
        if ft > 0 and cam_id in CAMERAS:
            elapsed = now_mono - ft
            if elapsed >= 0:
                CAMERAS[cam_id]["last_frame_wall"] = now_wall - elapsed
                saved_count += 1
    if saved_count:
        try:
            save_cameras()
            log.info(f"  Persisted last_frame_wall for {saved_count} camera(s)")
        except Exception as ex:
            log.warning(f"  Could not save last_frame_wall: {ex}")

    # ── 2. Cancel snap_loop tasks ──────────────────────────────────────────
    # 2.6.5: the motion keeper first, or it would restart them.
    if _MOTION_TASK is not None and not _MOTION_TASK.done():
        _MOTION_TASK.cancel()
    cancelled_tasks = 0
    for state in _SNAP.values():
        task = state.get("task")
        if task and not task.done():
            task.cancel()
            cancelled_tasks += 1

    # ── 2b. Stop go2rtc (2.6.3) ────────────────────────────────────────────
    # Cancelling the supervisor makes it SIGTERM go2rtc (SIGKILL after 3 s)
    # and stops it restarting. The direct kill below catches the case where
    # the supervisor is wedged and misses its 5 s window.
    if _GO2RTC_TASK is not None and not _GO2RTC_TASK.done():
        _GO2RTC_TASK.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(_GO2RTC_TASK, return_exceptions=True), timeout=5)
        except asyncio.TimeoutError:
            log.warning("go2rtc: supervisor did not stop within 5s")
    if anycam_go2rtc._GO2RTC_PROC is not None and anycam_go2rtc._GO2RTC_PROC.returncode is None:
        try:
            anycam_go2rtc._GO2RTC_PROC.kill()
        except ProcessLookupError:
            pass

    # ── 3. SIGTERM ffmpeg children, wait 3s, SIGKILL survivors ─────────────
    procs_to_kill = []
    for state in _SNAP.values():
        proc = state.get("proc")
        if proc and proc.returncode is None:
            procs_to_kill.append(proc)
    for ms in _MOTION.values():
        proc = ms.get("proc")
        if proc and proc.returncode is None:
            procs_to_kill.append(proc)

    if procs_to_kill:
        log.info(f"  SIGTERM-ing {len(procs_to_kill)} ffmpeg child(ren)...")
        for proc in procs_to_kill:
            try:
                proc.terminate()
            except Exception:
                pass
        # Wait up to 3 s total — divide remaining budget across processes
        deadline = time.monotonic() + 3.0
        for proc in procs_to_kill:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(proc.wait(), timeout=remaining)
            except (asyncio.TimeoutError, Exception):
                pass
        # SIGKILL anything that didn't exit
        survivors = [p for p in procs_to_kill if p.returncode is None]
        if survivors:
            log.warning(f"  SIGKILL-ing {len(survivors)} ffmpeg process(es) "
                        f"that did not exit within 3s")
            for proc in survivors:
                try:
                    proc.kill()
                except Exception:
                    pass
            for proc in survivors:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except Exception:
                    pass

    # Brief settle time so cancelled tasks finish their CancelledError handlers
    if cancelled_tasks:
        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

    # ── 4. Shut down thread pool ───────────────────────────────────────────
    # cancel_futures=True drops queued (not-yet-started) work so we don't
    # block waiting for nmap/probe_rtsp jobs that are still in the queue.
    try:
        _THREAD_POOL.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        # Python <3.9 doesn't have cancel_futures kwarg
        _THREAD_POOL.shutdown(wait=False)

    log.info(f"Graceful shutdown complete "
             f"(cancelled {cancelled_tasks} task(s), "
             f"killed {len(procs_to_kill)} ffmpeg child(ren))")


async def main() -> None:
    global _STOP_EVENT, _GO2RTC_TASK, _MOTION_TASK

    load_cameras()
    load_blacklist()
    load_feedback()
    load_oui_db()   # Load cached OUI DB synchronously (fast, from disk)

    # Suppress Docker bridge IP entries from the aiohttp access log
    _access_log = logging.getLogger("aiohttp.access")
    _access_log.addFilter(_DockerIPFilter())

    # ── go2rtc live view (2.6.3, Tier 2; always on since 2.6.4) ───────────────
    # Supervised for the life of the addon; see _go2rtc_supervisor. Started
    # before the web server so it is usually ready by the first page load;
    # a card that asks before it is ready retries (api_go2rtc_card).
    # There is no option to turn it off: when go2rtc is missing or not
    # running, or a browser cannot play a stream, Enhanced View falls back to
    # the classic JPEG path per camera, which is the same result the option
    # used to give.
    _GO2RTC_TASK = asyncio.create_task(_go2rtc_supervisor())

    # ── Motion detection (2.6.5) ──────────────────────────────────────────────
    # Re-arm the cameras armed before this restart; the keeper starts their
    # loops within MOTION_KEEPER_S, with or without a viewer.
    _motion_load()
    _MOTION_TASK = asyncio.create_task(_motion_keeper())

    # ── Graceful shutdown plumbing ────────────────────────────────────────────
    # _STOP_EVENT is set by SIGTERM/SIGINT handlers below. main() blocks on it,
    # then runner.cleanup() fires the on_shutdown chain (incl. _on_shutdown).
    _STOP_EVENT = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _STOP_EVENT.set)
        except (NotImplementedError, RuntimeError):
            # Some platforms (Windows) don't support add_signal_handler.
            # On those, the process will die without graceful cleanup.
            pass

    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"AnyCam on :{PORT}  ingress='{INGRESS_PATH}'")

    # ── Hardware decoder availability probe ───────────────────────────────────
    # Run once at startup. Checks which hw decoders ffmpeg was compiled with
    # AND which devices are actually accessible (full_access: true exposes all
    # host devices; on non-Pi hardware the v4l2m2m devices simply won't exist).
    # Populates _HW_UNAVAILABLE so snap_loop never tries an unavailable decoder.
    # 2.6.6 (B10): after the web server starts, not before. The probe takes
    # about 2 s, and Home Assistant's ingress proxy logged "Cannot connect to
    # host 172.30.32.1:8099" until the server listened. snap_loop waits for
    # _HW_PROBED before choosing a decoder.
    try:
        await _probe_hw_decoders()
    finally:
        _HW_PROBED.set()
    # Register all saved cameras on startup
    startup_mode = get_startup_mode()
    log.info(f"Startup mode: {startup_mode}")

    if startup_mode == "new_install":
        log.info("New install — starting initial scan")
        asyncio.create_task(run_scan())

    elif startup_mode == "post_upgrade":
        prev = load_runtime().get("version", "unknown")
        log.info(f"Post-upgrade ({prev} → {CURRENT_VERSION}) — running verification scan")
        asyncio.create_task(run_verification_scan(prev_version=prev))

    else:  # "routine"
        log.info(f"Routine restart (v{CURRENT_VERSION}) — loaded {len(CAMERAS)} saved camera(s)")

    # Record the current version so next startup can compare
    # Also save last scan duration so we can use it for future ETA estimates
    _runtime_data = {"version": CURRENT_VERSION}
    if "last_scan_duration" in load_runtime():
        _runtime_data["last_scan_duration"] = load_runtime()["last_scan_duration"]
    save_runtime(_runtime_data)

    # Background: download/refresh IEEE OUI database (non-blocking)
    asyncio.create_task(refresh_oui_db())

    # Block until SIGTERM/SIGINT flips the stop event, then run cleanup.
    # runner.cleanup() invokes app.on_shutdown handlers (incl. _on_shutdown)
    # which kills ffmpeg children, persists last_frame_wall, etc.
    await _STOP_EVENT.wait()
    log.info("Shutdown signal received — running cleanup...")
    await runner.cleanup()


# 3.0.0-rc1.1: give the other modules the names they take from this file.
anycam_host.bind(globals(), anycam_motion, anycam_storage, anycam_go2rtc, anycam_probe, anycam_scan, anycam_brand, anycam_page, anycam_focus, anycam_snap)

if __name__ == "__main__":
    asyncio.run(main())