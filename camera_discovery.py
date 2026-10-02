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
from page_script import PAGE_SCRIPT as _JS
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

# Currently focused camera for full-screen enhanced view.
# When set, all other snap_loops throttle to 1fps; focused loop runs native res.
_FOCUSED_CAMERA: str | None = None
# 2.6.3 — which engine serves the current focus session: "legacy" (the
# native-res ffmpeg snap_loop) or "go2rtc" (passthrough, no snap_loop for
# the focused camera). None when no camera is focused. handle_focus_clear
# reads it, because the two engines leave different state to tear down.
_FOCUS_ENGINE: str | None = None

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



# ── Adaptive fps state for focus/native_res mode ───────────────────────────────
# Persists across focus sessions so the camera remembers its best stable fps.
# Tiers ordered fastest→slowest. None = no fps limit (full native rate).
_ADAPTIVE_FPS_MAX:       int   = 30    # start fps cap (steps down by 1 each restart)
_ADAPTIVE_UNSTABLE_S:    float = 8.0   # run shorter than this with few frames = unstable
_ADAPTIVE_UNSTABLE_FR:   int   = 15    # fewer frames than this = unstable
_ADAPTIVE_RESTART_LIMIT: int   = 1     # restarts at locked tier before stepping down
_FOCUS_ADAPTIVE:         dict  = {}    # camera_id → {tier_idx, locked, run_start, ladder,
                                       #               restarts_since_lock}


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


async def _hw_preheater(
    camera_id: str,
    state: dict,
    hw_label: str,
    timeout_s: float = 30.0,
) -> None:
    """2.6.0-rc3.0 Items 2+3 — Fast Stream Start background task.

    When CFG_FAST_STREAM_START is on AND a HW decoder is selected AND
    we're in Enhanced View (native_res=True), snap_loop launches a
    second ffmpeg (HW) in parallel with the primary (SW) one. The
    SW proc serves frames immediately (~1s); this task watches the
    HW proc's stdout, waits for the first complete JPEG (proves the
    HW decoder warmed up and the camera is delivering valid data),
    then signals the main loop to swap state["proc"] from the SW
    to the HW proc.

    State protocol (all keys read/written under state):
      state["proc_hw"]: the HW ffmpeg proc (set by snap_loop before
        creating this task).
      state["hw_ready"]: set True by us when first HW JPEG seen.
      state["hw_preheater_failed"]: set True if HW died before
        producing a frame, or the timeout fired.
      state["hw_swapped"]: set True by the main loop when it has
        consumed our signal and swapped procs. We then continue
        draining the HW proc's stdout briefly (to prevent pipe-fill
        before the main loop takes over) and exit.

    The timeout (30s) is much longer than the rc2.5 single-proc HW
    warmup (10s). Reason: fast_stream_start is a "best effort"
    upgrade — if HW takes longer than usual, we just stay on SW
    for the session instead of disrupting the user's view. The
    rc2.5 10s timeout exists to bound the "I'm watching a frozen
    thumbnail" window; with fast_stream_start that window is gone
    (SW serves frames during warmup), so HW slowness is invisible.
    """
    proc_hw = state.get("proc_hw")
    if not proc_hw or not proc_hw.stdout:
        log.debug(f"SNAP [{camera_id}]: hw preheater — no proc_hw, exiting")
        state["hw_preheater_failed"] = True
        return
    started = time.monotonic()
    SOI     = bytes([0xFF, 0xD8])
    EOI     = bytes([0xFF, 0xD9])
    buf     = b""
    try:
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= timeout_s:
                log.warning(f"SNAP [{camera_id}]: hw preheater timeout "
                            f"({timeout_s:.0f}s) — staying SW for this session")
                state["hw_preheater_failed"] = True
                try: proc_hw.kill()
                except Exception: pass
                return
            try:
                chunk = await asyncio.wait_for(
                    proc_hw.stdout.read(65536), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if not chunk:
                rc = proc_hw.returncode
                log.info(f"SNAP [{camera_id}]: hw preheater EOF "
                         f"(rc={rc}, elapsed={elapsed:.1f}s) — HW proc died "
                         f"before producing a frame, staying SW")
                state["hw_preheater_failed"] = True
                return
            buf += chunk
            # Look for complete JPEG. Once seen, signal hw_ready and
            # continue draining to prevent pipe-fill until main loop swaps.
            if not state.get("hw_ready"):
                s = buf.find(SOI)
                e = buf.find(EOI, s + 2) if s >= 0 else -1
                if s >= 0 and e > s:
                    log.info(f"SNAP [{camera_id}]: hw preheater first frame "
                             f"in {elapsed:.1f}s ({hw_label}) — ready for swap")
                    state["hw_ready"] = True
                    state["hw_preheat_elapsed"] = elapsed
                    # Drain mode: keep reading & discarding until the main
                    # loop swaps procs (consumes our signal). After swap,
                    # main loop owns proc_hw and we exit.
                    buf = b""  # reset to avoid unbounded growth in drain mode
            else:
                # We've signalled hw_ready — drain quickly until swap.
                buf = b""
                if state.get("hw_swapped"):
                    log.debug(f"SNAP [{camera_id}]: hw preheater — main loop "
                              f"swapped procs, exiting")
                    return
    except asyncio.CancelledError:
        # Owner cancelled us (e.g., tier change, focus leave, snap_loop end).
        # Don't kill proc_hw here — the caller is responsible for that since
        # they cancelled us. (Cancellation might happen AFTER the swap, in
        # which case main loop owns proc_hw and we shouldn't kill it.)
        raise
    except Exception as ex:
        log.warning(f"SNAP [{camera_id}]: hw preheater error: {ex}")
        state["hw_preheater_failed"] = True
        try: proc_hw.kill()
        except Exception: pass


def _kill_hw_preheater(state: dict) -> None:
    """2.6.0-rc3.0 Items 2+3 — clean up any active HW preheater state.

    Called from:
      - snap_loop outer restart loop, before launching a new proc pair
        (clears stale state from a prior iteration that exited)
      - handle_focus_clear (focus-leave), same
      - snap_loop end-of-function cleanup

    Idempotent — safe to call when no preheater is running.
    """
    t = state.pop("hw_preheater_task", None)
    if t and not t.done():
        try: t.cancel()
        except Exception: pass
    proc_hw = state.pop("proc_hw", None)
    if proc_hw is not None:
        try: proc_hw.kill()
        except Exception: pass
    state.pop("hw_ready", None)
    state.pop("hw_swapped", None)
    state.pop("hw_preheater_failed", None)
    state.pop("hw_preheat_elapsed", None)


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

    # Normal (card) output filters — low_fps_mode adjusts fps inside _launch_snap
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
        Respects CFG_ options: thread limiting, skip_nonref, low_fps_mode.
        In focus/native_res mode, Low FPS Mode and Limit Threads are bypassed
        so the user gets full quality regardless of config settings.

        2.6.0-rc2.3: hw_args is the candidate's ffmpeg_args field copied
        verbatim from _HW_DECODER_CANDIDATES — either a [-c:v <decoder>]
        pair for decoder-name candidates (h264_v4l2m2m, *_vaapi if used
        as -c:v) or a [-hwaccel <name> -c:v <codec>] quad for hwaccel
        candidates (hevc_drm). Caller does the candidate lookup; this
        function just splices the args in.
        """
        hw_args     = list(hw_args) if hw_args else []
        # In focus mode: lift thread cap and nonref-skip for full quality,
        # even if CFG_LIMIT_THREADS / CFG_SKIP_NONREF are enabled in config.
        thread_args = [] if native_res else (["-threads", "2"] if CFG_LIMIT_THREADS else [])
        skip_args   = [] if native_res else (["-skip_frame", "nonref"] if CFG_SKIP_NONREF else [])
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
        elif CFG_LOW_FPS and is_hevc:
            # Low-fps mode for HEVC — 2fps output (decode cost unchanged,
            # encode/pipe cost drastically reduced)
            vf_used   = out_vf.replace(f"fps={8 if stream_w < 3840 else 4}", "fps=2")
            fps_label = "low-fps"
        else:
            vf_used   = out_vf
            fps_label = "normal"

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
            *skip_args,
            *hw_args,
            "-i", ffmpeg_url,
            "-an", "-vf", vf_used,
            *thread_args,
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
            if native_res and _FOCUSED_CAMERA != camera_id:
                log.info(f"SNAP [{camera_id}]: focus cleared — exiting native-res task")
                return

            # 2.6.7: every ffmpeg start waits out the camera's cooldown,
            # as every other connection to it does. On 2026-10-01 the first
            # start came 1 s after go2rtc's attempt, and five more 5 s apart,
            # while a 30 s cooldown was in force for the Microseven.
            if _snap_throttle_s > 0 or _acd_active(_snap_ip):
                await _throttle_wait_if_needed(_snap_ip, _snap_throttle_s,
                                               f"ffmpeg {camera_id}")
                if native_res and _FOCUSED_CAMERA != camera_id:
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
            if (_FOCUSED_CAMERA and _FOCUSED_CAMERA != camera_id
                    and not (_motion_uses_snapshots(camera_id) and _FOCUS_ENGINE == "go2rtc")):
                await asyncio.sleep(1.0)   # ~1fps while another cam is focused
                continue

            # Stagger polling start times across cameras to spread CPU spikes.
            if CFG_STAGGER_POLL and state["frame_count"] == 0:
                idx = list(_SNAP.keys()).index(camera_id) if camera_id in _SNAP else 0
                await asyncio.sleep(idx * 0.04)   # 40ms offset per camera

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
                        hw_label = cand_label
                        hw_args  = list(cand_args)
                        break
                if not hw_label:
                    log.debug(f"SNAP [{camera_id}]: no hw decoder available "
                              f"for codec={stream_codec}, using software")

            # 2.6.0-rc3.0 Items 2+3 — Fast Stream Start dual-proc path.
            # Clean up any preheater state from a prior outer-loop iteration
            # (e.g. previous tier change left a HW proc + task around).
            _kill_hw_preheater(state)
            # 2.6.1 — Tier 1 item 3: gate Fast Stream Start by resolution and
            # codec. At 3840x2160 HEVC the SW proc cannot produce a first frame
            # before rpivid finishes warming up (2.5-7s), so the parallel
            # decode spends CPU on frames that never render, and the second
            # RTSP session draws `RTP bad cseq` warnings. Below 4K, and for
            # h264 at any resolution, the dual-proc path still wins.
            # Width comes from the ladder's active profile, not from the camera
            # record, because a step-down may already have moved off 4K.
            _fs_w = stream_w
            if native_res:
                _fs_ada    = _FOCUS_ADAPTIVE.get(camera_id, {})
                _fs_ladder = _fs_ada.get("ladder") or []
                if _fs_ladder:
                    _fs_ti = min(_fs_ada.get("tier_idx", 0),
                                 len(_fs_ladder) - 1)
                    _fs_pi, _ = _fs_ladder[_fs_ti]
                    _fs_profs = camera.get("stream_profiles") or []
                    if 0 <= _fs_pi < len(_fs_profs):
                        _fs_prof = _fs_profs[_fs_pi]
                        _fs_w = _fs_prof.get("stream_width") or stream_w
            _fs_blocked = bool(is_hevc and (_fs_w or 0) >= 3840)
            _fast_start_active = (CFG_FAST_STREAM_START and native_res
                                  and bool(hw_label)
                                  and not _fs_blocked)
            if _fs_blocked and CFG_FAST_STREAM_START and native_res and hw_label:
                log.info(f"SNAP [{camera_id}]: fast_stream_start suppressed — "
                         f"width={_fs_w} HEVC is at or above the 4K gate; "
                         f"software decode cannot beat rpivid warmup here")
            if _fast_start_active:
                # Launch SW first (it's the active proc; main loop reads it).
                # Launch HW second (preheater task watches it for first frame).
                # Both ffmpegs target the same RTSP URL — most cameras tolerate
                # two concurrent sessions for the few seconds of HW warmup; if
                # the camera rejects the second session, the HW preheater will
                # see proc_hw die quickly and stay-SW gracefully.
                log.info(f"SNAP [{camera_id}]: fast_stream_start ON — "
                         f"launching SW for immediate frame + {hw_label} "
                         f"preheating in background")
                proc       = await _launch_snap(hw_args=[], hw_label="",
                                                native_res=native_res)
                proc_hw    = await _launch_snap(hw_args=hw_args, hw_label=hw_label,
                                                native_res=native_res)
                state["proc"]    = proc
                state["proc_hw"] = proc_hw
                state["hw_ready"]    = False
                state["hw_swapped"]  = False
                state["hw_preheater_failed"] = False
                state["hw_preheater_task"] = asyncio.create_task(
                    _hw_preheater(camera_id, state, hw_label))
                # The main read loop reads SW frames. hw_tried=False because
                # the active proc IS the SW proc — we're not "trying HW and
                # waiting to see if it works" in the rc2.5 sense. HW happens
                # in the preheater. hw_started_at also stays None to keep the
                # rc2.5 timeout/EOF fallback diagnostics for the SW proc
                # behaving correctly (no spurious "hw EOF" logs for SW exits).
                hw_tried       = False
                hw_started_at  = None
            else:
                proc     = await _launch_snap(hw_args=hw_args, hw_label=hw_label,
                                              native_res=native_res)
                state["proc"] = proc
                hw_tried       = bool(hw_label)
                hw_started_at  = time.monotonic() if hw_tried else None
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
            # hw_tried and hw_started_at were set above conditionally based
            # on whether fast_stream_start is active. They control the rc2.5
            # HW-EOF-fallback logic in the read loop below. In fast_stream
            # mode, the active proc IS SW, so hw_tried=False suppresses the
            # spurious "hw EOF" treatment of SW exits.

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

                        # 2.6.0-rc3.0 Items 2+3 — Fast Stream Start swap.
                        # If the HW preheater task signalled hw_ready (HW
                        # produced its first JPEG, decoder is warm and the
                        # camera is delivering valid HW-decodable data),
                        # atomically swap state["proc"] from the SW proc to
                        # the HW proc. The user sees no disruption — last SW
                        # frame is followed by first HW frame, both at the
                        # same vf output dimensions. From this point forward
                        # the main read loop reads HW frames and SW is dead.
                        if state.get("hw_ready") and not state.get("hw_swapped"):
                            sw_proc = state["proc"]
                            hw_proc = state.get("proc_hw")
                            if hw_proc is not None:
                                elapsed = state.get("hw_preheat_elapsed", 0.0)
                                log.info(f"SNAP [{camera_id}]: hw upgrade "
                                         f"complete — swapping SW→{hw_label} "
                                         f"(preheat took {elapsed:.1f}s)")
                                try: sw_proc.kill()
                                except Exception: pass
                                state["proc"]       = hw_proc
                                state["proc_hw"]    = None
                                state["hw_swapped"] = True
                                # Local proc var: subsequent reads happen
                                # against the new (HW) proc.
                                proc = hw_proc
                                # Reset frame buffer — old buf is SW byte
                                # stream, possibly mid-JPEG; HW stream starts
                                # fresh. buf.find(SOI) below will skip any
                                # garbage to first HW frame.
                                buf  = b""
                                # Replace stderr drain task with one bound
                                # to the HW proc (the old one was on SW).
                                stderr_t.cancel()
                                stderr_t = asyncio.create_task(
                                    _drain_stderr(proc, f"SNAP:{camera_id}"))

                        # Native-res focus task: exit the inner loop the moment
                        # focus is cleared so the task terminates quickly without
                        # waiting for task.cancel() to be delivered.
                        if native_res and _FOCUSED_CAMERA != camera_id:
                            break

                        if frames == 1 or frames % 50 == 0:
                            poll_ago = time.monotonic() -                                        _snap_last_access.get(camera_id, time.monotonic())
                            log.info(f"SNAP [{camera_id}]: frame {frames} "
                                     f"— {len(frame)} bytes "
                                     f"(last poll {poll_ago:.1f}s ago)")

                        _motion_on_frame(camera_id, frame)

            except asyncio.CancelledError:
                log.info(f"SNAP [{camera_id}]: task cancelled")
                raise
            except Exception as ex:
                log.warning(f"SNAP [{camera_id}]: inner exception: {ex}")
            finally:
                stderr_t.cancel()
                # 2.6.6 (B9): most exits here follow EOF; see _stop_proc.
                await _stop_proc(proc, exited_grace=0.5, timeout=3)
                try: await asyncio.wait_for(stderr_t, timeout=2)
                except Exception: pass

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
            else:
                backoff = 2
                state["zero_frame_streak"] = 0

            # ── Adaptive fps for focus/native_res mode ────────────────────────
            # Step-down only: never step back up once stable.
            # ── Adaptive fps/res controller for focus/native_res mode ─────────
            # Only run when this camera still has focus — prevents post-exit
            # step-downs from firing after focus was cleared while ffmpeg was
            # still running.
            if native_res and _FOCUSED_CAMERA == camera_id:
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
        """
        Build an HTTP Digest Authorization header value.
        Handles the qop=auth case (most cameras) and the simpler no-qop case.
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
    if camera.get("display") in ("webrtc", "wsrtsp", "info"):
        return web.Response(status=400, text="Not streamable")

    # ── Focus mode guard ──────────────────────────────────────────────────────
    # When this camera is in enhanced/focus view, do NOT start a competing
    # sub-stream task. The focus snap_loop manages its own lifecycle (incl.
    # the 2s restart delay). Just serve the last buffered frame so the JS
    # poller gets something without triggering a 480p task that would stomp
    # all over the adaptive quality controller.
    if _FOCUSED_CAMERA == camera_id:
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
    src, codec, reason = _go2rtc_profile_source(camera, prof_idx)
    if not src:
        return web.json_response({"ok": False, "reason": reason, "codec": codec})
    name = _go2rtc_stream_name(camera_id, prof_idx)
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
        state["task"] = asyncio.create_task(
            snap_loop(camera_id, url, camera, native_res=True))
    return web.json_response({"status": "ok", "focused": camera_id})


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
            # 2.6.0-rc3.0 Items 2+3 — tear down HW preheater + HW proc if
            # fast_stream_start was active for this focus session. Without
            # this, the HW preheater task keeps running after focus-leave
            # and may signal hw_ready into a snap_loop that already exited,
            # leaking the proc_hw subprocess.
            _kill_hw_preheater(state)
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

HTML = None   # built once on first request

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






def build_html() -> str:
    js_code = _JS.replace('___BASE___', INGRESS_PATH)
    js_code = js_code.replace('___UNRESTRICTED___',
                               'true' if CFG_UNRESTRICTED_BROWSER else 'false')
    js_code = js_code.replace('___ADAPTIVE_QUALITY___',
                               'true' if CFG_ADAPTIVE_QUALITY else 'false')
    # 2.6.6 (B5): never filled before, so the literal placeholder read as a
    # configured endpoint: "Share with community" showed ticked and did
    # nothing. A JSON string literal, with < escaped for the <script> block.
    js_code = js_code.replace('___COMMUNITY___',
                               json.dumps(COMMUNITY_ENDPOINT).replace('<', '\\u003c'))
    # CSS uses {{ }} for literal braces in Python f-string
    css = f"""\
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#111318;--surface:#1e2029;--surface2:#272a35;--border:#2e3140;
  --primary:#5b8af5;--primary-dim:#3a5dbf;--green:#4caf7d;--yellow:#f5b942;
  --red:#e05c5c;--blue:#5bc4f5;--orange:#f5944a;--text:#e4e6f0;--text-dim:#8a8fa8;
  --radius:12px;--card-w:320px;
}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;min-height:100vh;display:flex;flex-direction:column}}
header{{background:var(--surface);border-bottom:1px solid var(--border);
        padding:10px 18px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;
        position:sticky;top:0;z-index:10}}
header h1{{font-size:1rem;font-weight:700;display:flex;align-items:center;gap:8px;margin-right:auto}}
.btn{{padding:6px 14px;border-radius:8px;border:none;cursor:pointer;font-size:.8rem;
      font-weight:600;transition:opacity .15s,background .15s;white-space:nowrap}}
.btn:disabled{{opacity:.4;cursor:default}}
.btn-primary{{background:var(--primary);color:#fff}}
.btn-primary:not(:disabled):hover{{background:var(--primary-dim)}}
.btn-secondary{{background:var(--surface2);color:var(--text);border:1px solid var(--border)}}
.btn-secondary:hover,.btn-secondary.active{{background:var(--primary);color:#fff;border-color:var(--primary)}}
.btn-danger{{background:var(--red);color:#fff}}
.btn-ghost{{background:transparent;color:var(--text-dim);border:1px solid var(--border)}}
.btn-ghost:hover{{color:var(--text)}}
.btn-sm{{padding:4px 10px;font-size:.74rem}}
.sweep-toggle{{display:flex;align-items:center;gap:5px;font-size:.78rem;color:var(--text-dim);cursor:pointer;white-space:nowrap}}
.sweep-toggle input{{accent-color:var(--primary);cursor:pointer}}
#status-bar{{padding:6px 18px;font-size:.78rem;color:var(--text-dim);
             display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--border);min-height:30px}}
.stage-badge{{background:var(--primary);color:#fff;border-radius:4px;padding:1px 7px;
              font-size:.7rem;font-weight:700;white-space:nowrap}}
.progress-track{{flex:1;height:3px;background:var(--surface2);border-radius:2px;overflow:hidden;max-width:180px}}
.progress-fill{{height:100%;background:var(--primary);border-radius:2px;transition:width .4s ease;width:0%}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
.scanning .progress-fill{{animation:pulse 1.2s infinite}}
.view{{display:none;flex:1;overflow:auto}}
.view.active{{display:flex;flex-direction:column}}
#cam-grid{{padding:18px;display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--card-w),1fr));gap:16px;align-content:start;align-items:start}}
#empty-state{{grid-column:1/-1;text-align:center;padding:60px 20px;color:var(--text-dim)}}
#empty-state svg{{opacity:.2;display:block;margin:0 auto 14px}}
.camera-card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
              overflow:hidden;display:flex;flex-direction:column;transition:box-shadow .2s}}
.camera-card:hover{{box-shadow:0 4px 20px rgba(0,0,0,.5)}}
.camera-card.uncertain{{border-color:rgba(245,148,74,.4)}}
.feed-wrap{{position:relative;width:100%;aspect-ratio:16/9;background:#000;
            display:flex;align-items:center;justify-content:center;overflow:hidden}}
.feed-wrap img,.feed-wrap video{{width:100%;height:100%;object-fit:cover;display:block}}
/* 2.6.6 live cards: the player sits over the placeholder until it plays */
.feed-wrap anycam-video{{position:absolute;inset:0;display:block;transition:opacity .3s;cursor:pointer}}
.feed-placeholder{{display:flex;flex-direction:column;align-items:center;gap:6px;
                   color:var(--text-dim);font-size:.78rem;text-align:center;padding:10px}}
.feed-placeholder svg{{opacity:.3}}
.info-overlay{{position:absolute;inset:0;background:rgba(10,12,18,.88);
               display:flex;flex-direction:column;align-items:center;
               justify-content:center;gap:8px;padding:14px;text-align:center}}
.info-overlay .pi{{font-size:1.8rem}}
.info-overlay p{{font-size:.75rem;color:var(--text-dim);line-height:1.4}}
.info-overlay a{{color:var(--primary);font-size:.76rem}}
.info-overlay code{{font-size:.68rem;color:var(--text-dim);word-break:break-all;background:var(--surface2);padding:3px 6px;border-radius:4px}}
.card-info{{padding:10px 12px 5px;display:flex;align-items:flex-start;gap:8px}}
.card-cog{{margin-left:auto;flex-shrink:0;background:transparent;border:none;color:var(--text-dim);cursor:pointer;padding:2px;line-height:0;border-radius:6px}}
.card-cog:hover{{color:var(--text);background:var(--surface2)}}
.card-name{{font-size:.86rem;font-weight:600;flex:0 1 auto;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}}
.card-info-btn{{flex-shrink:0;background:transparent;border:none;color:var(--text-dim);cursor:pointer;padding:1px;line-height:0;border-radius:50%;margin-top:1px}}
.card-info-btn:hover,.card-info-btn.open{{color:var(--primary)}}
.card-lock{{margin-left:auto;display:inline-flex;align-items:center}}
.card-name:hover{{color:var(--primary)}}
.badges{{padding:0 12px 8px;display:flex;flex-wrap:wrap;gap:4px}}
.badge{{font-size:.68rem;font-weight:600;padding:2px 6px;border-radius:20px}}
.lock-badge{{background:transparent !important;padding:0 !important;display:inline-flex;align-items:center;line-height:1}}
.status-dot{{width:8px;height:8px;border-radius:50%;flex-shrink:0;margin-top:5px}}
.dot-ready{{background:var(--green);box-shadow:0 0 5px var(--green)}}
.dot-warning{{background:var(--yellow);box-shadow:0 0 5px var(--yellow)}}
.dot-info{{background:var(--blue);box-shadow:0 0 5px var(--blue)}}
.dot-uncertain{{background:var(--orange);box-shadow:0 0 5px var(--orange)}}
.dot-error{{background:var(--red);box-shadow:0 0 5px var(--red)}}
.dot-upgrade{{background:var(--orange);box-shadow:0 0 8px var(--orange);animation:pulse 2s infinite}}
.scan-timer-badge{{font-size:.74rem;color:var(--primary);font-weight:700;white-space:nowrap}}
.arp-host-section{{padding:14px 18px 0;display:flex;flex-direction:column;gap:6px}}
.arp-section-label{{font-size:.75rem;font-weight:600;color:var(--text-dim);letter-spacing:.04em}}
.arp-host-list{{background:var(--surface);border:1px solid var(--border);border-radius:8px;
               padding:10px;display:flex;flex-direction:column;gap:3px;max-height:220px;overflow-y:auto}}
.arp-empty{{font-size:.78rem;color:var(--text-dim);padding:4px 0}}
.arp-select-row{{display:flex;align-items:center;gap:6px;padding-bottom:6px;
                border-bottom:1px solid var(--border);margin-bottom:4px}}
.arp-row{{display:flex;align-items:center;gap:8px;padding:3px 4px;border-radius:5px;
         cursor:pointer;font-size:.82rem}}
.arp-row:hover{{background:var(--surface2)}}
.arp-row input{{accent-color:var(--primary);cursor:pointer;flex-shrink:0}}
.arp-ip{{font-family:monospace;font-weight:600;color:var(--primary)}}
.arp-host{{color:var(--text-dim)}}
.id-section{{margin:0 12px 8px;border:1px solid var(--border);border-radius:8px;overflow:hidden}}
.id-table{{width:100%;border-collapse:collapse;font-size:.74rem}}
.id-table td{{padding:4px 10px;border-top:1px solid var(--border);vertical-align:top}}
.id-table tr:first-child td{{border-top:none}}
.id-key{{color:var(--text-dim);font-weight:600;white-space:nowrap;width:90px}}
.cred-form{{margin:0 12px 10px;background:var(--surface2);border:1px solid var(--border);
            border-radius:8px;padding:10px;display:flex;flex-direction:column;gap:6px}}
.cred-form label{{font-size:.7rem;color:var(--text-dim);font-weight:600;letter-spacing:.04em}}
.cred-form input{{width:100%;background:var(--bg);border:1px solid var(--border);
                  border-radius:6px;color:var(--text);font-size:.82rem;padding:5px 8px;outline:none}}
.cred-form input:focus{{border-color:var(--primary)}}
.cred-row{{display:flex;gap:5px}}
.cred-row .btn{{flex:1}}
.cred-error{{font-size:.71rem;color:var(--red);display:none}}
.cred-error.visible{{display:block}}
.card-actions{{padding:0 12px 10px;display:flex;gap:4px;flex-wrap:wrap;align-items:center}}
.card-actions .btn-sm{{padding:3px 7px;font-size:.72rem}}
#pscan-view{{padding:16px 18px;gap:12px}}
.pscan-header{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:4px}}
.pscan-header .pscan-ctl{{margin-left:auto}}
.pscan-ctl{{display:flex;gap:6px;flex-wrap:wrap}}
.pscan-top{{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end}}
.pscan-top input{{background:var(--surface);border:1px solid var(--border);color:var(--text);
                  border-radius:8px;padding:7px 12px;font-size:.88rem;outline:none;
                  flex:1;min-width:200px;max-width:400px}}
.pscan-top input:focus{{border-color:var(--primary)}}
#pscan-msg{{font-size:.78rem;color:var(--text-dim);padding:2px 0;line-height:1.5}}
.live-ports-box{{background:var(--surface);border:1px solid var(--border);
               border-radius:8px;padding:10px 12px;max-height:220px;overflow-y:auto;
               display:flex;flex-direction:column;gap:3px;margin-bottom:8px;
               font-family:monospace;font-size:.82rem}}
.live-port-row{{display:flex;align-items:center;gap:6px;padding:1px 0}}
.live-port-row .pnum{{color:var(--primary);font-weight:700;min-width:52px}}
.live-proto{{color:var(--text-dim)}}
.section-header td{{background:var(--surface2);font-size:.73rem;font-weight:700;
                     color:var(--text-dim);letter-spacing:.04em;padding:6px 10px;
                     border-bottom:1px solid var(--border)}}
.other-header{{cursor:pointer}}
.other-header:hover td{{background:var(--border)}}
.port-table{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;flex:1}}
.port-table table{{width:100%;border-collapse:collapse;font-size:.8rem}}
.port-table th{{background:var(--surface2);padding:7px 10px;text-align:left;font-size:.72rem;color:var(--text-dim);font-weight:600;letter-spacing:.04em;border-bottom:1px solid var(--border)}}
.port-table td{{padding:7px 10px;border-bottom:1px solid var(--border);vertical-align:top}}
.port-table tr:last-child td{{border-bottom:none}}
.port-table tr:hover td{{background:var(--surface2)}}
.pnum{{font-weight:700;color:var(--primary);font-family:monospace}}
.psvc{{color:var(--green);font-weight:600}}
.pscripts{{font-family:monospace;font-size:.7rem;color:var(--text-dim);white-space:pre-wrap;word-break:break-all;max-height:80px;overflow:auto}}
#add-view{{padding:18px;max-width:520px}}
#add-view h2{{font-size:.95rem;font-weight:600;margin-bottom:14px}}
.fgrid{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}
.fgrid .full{{grid-column:1/-1}}
/* ── Record button variants ── */
.btn-rec-off{{background:#2a2a2a;color:#888;border:1px solid #444}}
.btn-rec-on{{background:#1e3a1e;color:#6fcf97;border:1px solid #2d5a2d}}
.btn-rec-active{{background:#4a1a1a;color:#ff6b6b;border:1px solid #8b2020;animation:rec-pulse 1.2s ease-in-out infinite}}
@keyframes rec-pulse{{0%,100%{{opacity:1}}50%{{opacity:.6}}}}
/* ── Storage view ── */
#storage-view{{padding:20px}}
#disk-bar-wrap{{flex:1}}
#disk-label{{font-size:.82rem;color:var(--text-dim);display:block;margin-bottom:6px}}
#disk-bar-track{{height:8px;background:#2a2a2a;border-radius:4px;overflow:hidden}}
#disk-bar-fill{{height:100%;border-radius:4px;transition:width .4s,background .4s}}
.stor-files{{padding:8px}}
.btn-xs{{padding:3px 8px;font-size:.72rem}}
/* ── Focus overlay ── */
#focus-overlay{{position:fixed;inset:0;background:#000;z-index:9000;display:flex;flex-direction:column;align-items:stretch;padding:0}}
#focus-img{{width:100vw;height:calc(100vh - 52px - 44px);height:calc(100dvh - 52px - 44px);object-fit:contain;display:block;margin:44px 0 0 0}}
/* 2.6.3 go2rtc live view: same box as #focus-img, swapped in its place */
#focus-video{{width:100vw;height:calc(100vh - 52px - 44px);height:calc(100dvh - 52px - 44px);margin:44px 0 0 0;background:#000}}
#focus-video anycam-video{{display:block;width:100%;height:100%}}
#focus-video video{{object-fit:contain;background:#000}}
.focus-engine-btn{{background:#1e1e2e;border:1px solid #555;color:#aaa;border-radius:6px;padding:3px 8px;font-size:.72rem;cursor:pointer;height:26px}}
.focus-engine-btn:hover{{border-color:#4a9eff;color:#4a9eff}}
#focus-bar{{position:absolute;bottom:0;left:0;right:0;height:52px;background:rgba(0,0,0,.85);display:flex;align-items:center;justify-content:space-between;padding:0 16px;gap:12px;z-index:9001;border-top:1px solid #333}}
#focus-info{{font-size:.78rem;color:#aaa;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}}
#focus-controls{{display:flex;align-items:center;gap:12px;flex-shrink:0}}
#focus-close{{position:absolute;top:6px;right:14px;background:transparent;border:2.5px solid #e03;color:#e03;font-size:1rem;font-weight:bold;width:32px;height:32px;border-radius:50%;cursor:pointer;z-index:9002;line-height:1;display:flex;align-items:center;justify-content:center}}
#focus-close:hover{{background:#e03;color:#fff}}
#focus-loading{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:9001;color:#ddd;font-size:1rem;text-align:center;pointer-events:none}}
#focus-loading-note{{color:#999;font-size:.8rem;margin-top:8px}}
/* 2.6.5: landscape on a touch screen, set by _focusLandscapeSync */
#focus-overlay.focus-landscape #focus-bar{{display:none}}
#focus-overlay.focus-landscape #focus-img,#focus-overlay.focus-landscape #focus-video{{height:100vh;height:100dvh;margin:0}}
#focus-warning{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);background:#1a1a1a;border:1px solid var(--orange);border-radius:10px;padding:24px;max-width:480px;text-align:center;z-index:9003;display:flex;flex-direction:column;gap:14px;align-items:center}}
#focus-warn-text{{color:#f5b942;font-size:.9rem;line-height:1.5}}
/* ── Toast ── */
#toast{{position:fixed;bottom:28px;left:50%;transform:translateX(-50%);padding:10px 22px;border-radius:24px;color:#fff;font-size:.85rem;z-index:9100;pointer-events:none;transition:opacity .3s}}
/* ── Storage view layout ── */
#storage-view{{padding:16px 18px}}
#storage-topbar{{display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap}}
/* ── Explorer pane (white box with nav inside) ── */
#stor-explorer{{background:#fff;border:1px solid #d0d0d0;border-radius:4px;overflow:hidden;color:#000}}
#stor-nav-bar{{display:flex;align-items:center;gap:4px;padding:6px 8px;background:#f3f3f3;border-bottom:1px solid #d0d0d0}}
#stor-nav-bar button{{background:none;border:1px solid transparent;color:#333;border-radius:3px;width:28px;height:24px;cursor:pointer;font-size:.85rem;line-height:1;transition:background .1s;flex-shrink:0}}
#stor-nav-bar button:disabled{{opacity:.35;cursor:default}}
#stor-nav-bar button:not(:disabled):hover{{background:#e0e0e0;border-color:#c0c0c0}}
#stor-breadcrumb{{flex:1;background:#fff;border:1px solid #c0c0c0;border-radius:2px;padding:3px 8px;font-size:.82rem;color:#111;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-left:4px;display:flex;align-items:center;gap:0;cursor:text}}
#stor-breadcrumb:hover{{border-color:#888}}
#stor-path-input{{width:100%;border:none;outline:none;font-size:.82rem;color:#111;background:transparent;padding:0}}
#stor-path-root{{color:#0066cc;cursor:pointer;border-radius:2px;padding:1px 3px}}
#stor-path-root:hover{{background:#e8f0fe}}
#stor-path-folder{{color:#111;font-weight:600}}
/* ── Column headers ── */
#stor-col-headers{{display:grid;grid-template-columns:1fr 180px 130px 90px 80px;gap:0;padding:5px 8px;background:#f3f3f3;border-bottom:1px solid #d0d0d0;font-size:.78rem;font-weight:600;color:#333}}
#stor-col-headers span{{cursor:pointer;user-select:none;padding:2px 4px;border-radius:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
#stor-col-headers span:hover{{background:#e0e0e0}}
#stor-col-headers .stor-col-acts{{cursor:default}}
#stor-col-headers .stor-col-acts:hover{{background:none}}
/* ── File/folder rows ── */
#storage-list{{max-height:calc(100vh - 260px);max-height:calc(100dvh - 260px);overflow-y:auto}}
.stor-row{{display:grid;grid-template-columns:1fr 180px 130px 90px 80px;gap:0;padding:3px 8px;font-size:.82rem;color:#111;align-items:center;border-bottom:1px solid #f0f0f0;cursor:default}}
.stor-row:hover{{background:#cce8ff}}
.stor-row:last-child{{border-bottom:none}}
.stor-row-name{{display:flex;align-items:center;gap:6px;overflow:hidden}}
.stor-row-name-text{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.stor-row-name-text.editable:hover{{color:#0066cc;cursor:pointer;text-decoration:underline}}
.stor-row-date,.stor-row-type,.stor-row-size{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#444;font-size:.78rem}}
.stor-row-acts{{display:flex;gap:4px;justify-content:flex-end;opacity:0}}
.stor-row:hover .stor-row-acts{{opacity:1}}
.stor-row-folder{{font-weight:500}}
.stor-empty-msg{{text-align:center;padding:60px 20px;color:#888;font-size:.9rem}}
/* ── Logs view ── */
/* ── header h1 cursor ── */
header h1{{cursor:pointer}}
.field label{{display:block;font-size:.7rem;color:var(--text-dim);font-weight:600;letter-spacing:.04em;margin-bottom:3px}}
.field input,.field select{{width:100%;background:var(--surface);border:1px solid var(--border);
  color:var(--text);border-radius:8px;padding:7px 10px;font-size:.84rem;outline:none}}
.field input:focus,.field select:focus{{border-color:var(--primary)}}
.field select option{{background:var(--surface2)}}
#add-error{{font-size:.78rem;color:var(--red);display:none;padding:6px 0}}
.modal-backdrop{{position:fixed;inset:0;background:rgba(0,0,0,.7);display:none;align-items:center;justify-content:center;z-index:100}}
.modal-backdrop.open{{display:flex}}
.modal{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
        padding:20px;width:min(360px,90vw);display:flex;flex-direction:column;gap:12px}}
.modal h3{{font-size:.92rem;font-weight:600}}
.modal input{{width:100%;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:.86rem;padding:7px 10px;outline:none}}
.modal input:focus{{border-color:var(--primary)}}
.modal-btns{{display:flex;gap:8px;justify-content:flex-end}}
/* 2.6.6 camera settings (cog menu) */
.cs-modal{{width:min(440px,94vw)}}
.cs-section{{font-size:.7rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--text-dim);margin-top:4px}}
.cs-label{{display:flex;justify-content:space-between;font-size:.84rem}}
.cs-val{{font-weight:700;color:var(--primary);min-width:2.5em;text-align:right}}
.cs-slider{{position:relative}}
.cs-modal .cs-slider input[type=range]{{width:100%;padding:0;border:none;background:transparent;accent-color:var(--primary);cursor:pointer}}
.cs-mode{{font-size:12px;color:var(--text-dim);margin-top:4px}}
.cs-mode.cs-night{{color:var(--blue)}}
.cs-night-note{{font-size:12px;color:var(--orange);margin-top:4px}}
.cs-peak-mark{{position:absolute;top:-2px;width:3px;height:22px;margin-left:-1px;background:var(--orange);border-radius:2px;pointer-events:none}}
.cs-ends{{display:flex;justify-content:space-between;font-size:.7rem;color:var(--text-dim);margin-top:-6px}}
.cs-peak{{font-size:.78rem;color:var(--orange);min-height:1.2em}}
.cs-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}
.cs-modal label{{display:flex;flex-direction:column;gap:4px;font-size:.76rem;color:var(--text-dim)}}
.cs-modal select{{background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:.86rem;padding:7px 8px}}
.cs-help{{font-size:.72rem;color:var(--text-dim);line-height:1.45}}
.cs-global{{font-size:.78rem;color:var(--yellow);border:1px solid var(--yellow);border-radius:8px;padding:8px 10px}}
.cs-error{{font-size:.78rem;color:var(--red);min-height:1em}}
.cs-modal input:disabled,.cs-modal select:disabled{{opacity:.45;cursor:not-allowed}}
.nc-modal-inner{{width:min(460px,94vw)}}
.nc-subtitle{{font-size:.82rem;color:var(--text-dim);margin-top:-4px}}
.nc-reasons{{display:flex;flex-wrap:wrap;gap:7px}}
.nc-reason-btn{{background:var(--surface2);border:1px solid var(--border);color:var(--text);
                border-radius:8px;padding:7px 12px;font-size:.78rem;cursor:pointer;
                transition:background .15s,border-color .15s;white-space:nowrap}}
.nc-reason-btn:hover{{background:var(--surface);border-color:var(--primary)}}
.nc-reason-btn.active{{background:var(--red);border-color:var(--red);color:#fff;font-weight:600}}
.nc-detail-row input{{width:100%;background:var(--bg);border:1px solid var(--border);
                      border-radius:8px;color:var(--text);font-size:.82rem;padding:7px 10px;outline:none}}
.nc-detail-row input:focus{{border-color:var(--primary)}}
.nc-share-row{{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px}}
.nc-share-label{{display:flex;align-items:flex-start;gap:8px;font-size:.8rem;cursor:pointer}}
.nc-share-label input{{flex-shrink:0;margin-top:2px;accent-color:var(--primary)}}
.nc-share-note{{font-size:.7rem;color:var(--text-dim);margin-top:5px;line-height:1.4}}
/* 2.4.0-rc2.0: Locked Streams modal — surfaces 401-locked RTSP paths
   the path walker found while collecting locked candidates. Same
   visual treatment as the notCam modal but with a credential-entry
   pane embedded. */
.locked-modal-inner{{width:min(520px,94vw)}}
.locked-subtitle{{font-size:.82rem;color:var(--text-dim);margin-top:-4px;line-height:1.5}}
.locked-list{{background:var(--surface2);border:1px solid var(--border);border-radius:8px;
              padding:8px 10px;max-height:180px;overflow-y:auto;font-family:monospace;
              font-size:.78rem;display:flex;flex-direction:column;gap:6px}}
.locked-row{{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px;
             padding:4px 6px;border-radius:5px;background:var(--bg)}}
.locked-row code{{color:var(--primary);background:transparent;font-size:.78rem}}
.locked-meta{{font-size:.68rem;color:var(--text-dim);font-family:monospace}}
.locked-cred-form{{display:flex;flex-direction:column;gap:6px;
                   background:var(--surface2);border:1px solid var(--border);
                   border-radius:8px;padding:10px}}
.locked-cred-form label{{font-size:.7rem;color:var(--text-dim);font-weight:600;letter-spacing:.04em}}
.locked-cred-form input{{width:100%;background:var(--bg);border:1px solid var(--border);
                         border-radius:8px;color:var(--text);font-size:.82rem;padding:7px 10px;outline:none}}
.locked-cred-form input:focus{{border-color:var(--primary)}}
.badge.locked-streams-badge:hover{{background:#3a2e54 !important}}"""

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AnyCam</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.7/dist/hls.min.js"></script>
<style>
""" + css + """
</style>
</head>
<body>

<header>
  <h1 onclick="switchView('cameras')" title="Click for Home" style="cursor:pointer">
    <svg id="status-cam-icon" width="27" height="27" viewBox="0 0 24 24"
         fill="none" stroke="#43a047" stroke-width="2"
         style="cursor:pointer;flex-shrink:0;vertical-align:middle"
         onclick="openHALog();event.stopPropagation()">
      <title>System Stability &mdash; See Logs</title>
      <path d="M15 10l4.553-2.069A1 1 0 0121 8.87v6.26a1 1 0 01-1.447.9L15 14"/>
      <rect x="1" y="7" width="14" height="10" rx="2" ry="2"/>
    </svg>
    <span style="margin-left:10px">AnyCam &mdash; Home</span>
  </h1>
  <span id="cam-count" style="color:var(--text-dim);font-size:.78rem"></span>
  <label class="sweep-toggle" title="Scan ports 0-10000 on live hosts that don't respond to camera ports">
    <input type="checkbox" id="broad-sweep">
    <span>Deeper Scan<br><small style="font-weight:400;opacity:.6;font-size:.68rem">Ports 1&#x2013;10,000</small></span>
  </label>
  <button class="btn btn-primary"   id="scan-btn"  onclick="startScan()">&#x1F50D; Scan Network</button>
  <button class="btn btn-secondary" id="pscan-btn"    onclick="switchView('pscan')">&#x1F50E; Port Scan</button>
  <button class="btn btn-secondary" id="add-btn"      onclick="switchView('add')">&#x2795; Connect Camera</button>
  <button class="btn btn-secondary" id="storage-btn"  onclick="switchView('storage')">&#x1F4BE; Storage</button>

</header>

<div id="status-bar">
  <span id="stage-badge" class="stage-badge" style="display:none"></span>
  <span id="status-msg">Idle &#x2014; click Scan Network to start.</span>
  <div class="progress-track" id="progress-track" style="display:none">
    <div class="progress-fill" id="progress-fill"></div>
  </div>
  <span id="scan-timer" style="display:none;font-size:.74rem;color:var(--primary);font-weight:600;white-space:nowrap"></span>
  <button id="scan-cancel-btn" onclick="cancelScan()"
          style="display:none;margin-left:auto;padding:3px 12px;font-size:.75rem;
                 background:#8b2020;color:#fff;border:none;border-radius:6px;cursor:pointer">
    &#x2715; Cancel
  </button>
</div>

<div class="view active" id="cameras-view">
  <div id="cam-grid">
    <div id="empty-state">
      <svg width="60" height="60" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M15 10l4.553-2.069A1 1 0 0121 8.87v6.26a1 1 0 01-1.447.9L15 14"/>
        <rect x="1" y="7" width="14" height="10" rx="2" ry="2"/>
      </svg>
      <p id="empty-idle">No cameras found.<br>Click <strong>Scan Network</strong> to discover cameras on your subnet.</p>
      <p id="empty-scanning" style="display:none">No Cameras Found Yet</p>
    </div>
  </div>
</div>

<div class="view" id="pscan-view">
  <!-- Back button upper left + controls -->
  <div class="pscan-header">
    <button class="btn btn-ghost btn-sm" onclick="switchView('cameras')">&#x2190; Back to Cameras</button>
    <div class="pscan-ctl">
      <button class="btn btn-primary"   id="ps-start"  onclick="startPortScan()">&#x25B6; Scan All Ports</button>
      <button class="btn btn-secondary" id="ps-pause"  onclick="togglePause()" style="display:none">&#x23F8; Pause</button>
      <button class="btn btn-danger"    id="ps-cancel" onclick="cancelPortScan()" style="display:none">&#x2715; Cancel</button>
    </div>
  </div>
  <!-- ARP-discovered hosts with checkboxes -->
  <div class="arp-host-section">
    <div class="arp-section-label">&#x1F4E1; Discovered hosts <span style="color:var(--text-dim);font-weight:400">(check any to include in port scan)</span></div>
    <div id="arp-host-list" class="arp-host-list">
      <span class="arp-empty">No hosts yet — run a network scan first.</span>
    </div>
  </div>
  <!-- Manual IP input -->
  <div class="pscan-top">
    <input type="text" id="pscan-ip" placeholder="Or enter an IP address manually (e.g. 192.168.1.100)"
           onkeydown="if(event.key==='Enter')startPortScan()"/>
  </div>
  <div id="pscan-msg" style="padding:6px 2px;font-size:.8rem;color:var(--text-dim)">Select hosts above and/or enter an IP, then click Scan All Ports.<br><small style="opacity:.7">Note: navigating away will not cancel an in-progress scan.</small></div>
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:4px">
    <div class="progress-track" id="ps-prog-track" style="display:none;flex:1;max-width:none">
      <div class="progress-fill" id="ps-prog-fill"></div>
    </div>
    <span id="ps-timer" style="display:none;font-size:.74rem;color:var(--primary);font-weight:600;white-space:nowrap"></span>
  </div>
  <!-- Live port discovery feed — shown during scan, replaced by table when done -->
  <div id="live-ports-box" class="live-ports-box" style="display:none"></div>
  <div class="port-table" id="port-table" style="display:none">
    <table>
      <thead><tr><th>Port</th><th>Proto</th><th>Service</th><th>Product / Version</th><th>Script Output</th></tr></thead>
      <tbody id="port-tbody"></tbody>
    </table>
  </div>
</div>

<div class="view" id="add-view">
  <div style="margin-bottom:14px">
    <button class="btn btn-ghost btn-sm" onclick="switchView('cameras')">&#x2190; Back to Cameras</button>
  </div>
  <h2>Connect Known Camera</h2>
  <div class="fgrid">
    <div class="field full"><label>CAMERA NAME</label><input type="text" id="add-name" placeholder="e.g. Front Door"/></div>
    <div class="field"><label>IP ADDRESS</label><input type="text" id="add-ip" placeholder="192.168.1.100"/></div>
    <div class="field"><label>PORT</label><input type="number" id="add-port" value="554" min="1" max="65535"/></div>
    <div class="field"><label>PROTOCOL</label>
      <select id="add-proto" onchange="onProtoChange()">
        <option value="RTSP">RTSP</option><option value="ONVIF">ONVIF</option>
        <option value="MJPEG">HTTP MJPEG</option><option value="HLS">HLS</option>
        <option value="RTMP">RTMP</option><option value="WebRTC">WebRTC</option>
        <option value="WS-RTSP">WS-RTSP</option>
      </select>
    </div>
    <div class="field full" id="path-field">
      <label>STREAM PATH <span style="font-weight:400;color:var(--text-dim)">(optional)</span></label>
      <input type="text" id="add-path" placeholder="/stream  or  /cam/realmonitor?channel=1"/>
    </div>
    <div class="field"><label>USERNAME <span style="font-weight:400;color:var(--text-dim)">(optional)</span></label>
      <input type="text" id="add-user" autocomplete="username" placeholder="admin"/></div>
    <div class="field"><label>PASSWORD <span style="font-weight:400;color:var(--text-dim)">(optional)</span></label>
      <input type="password" id="add-pass" autocomplete="current-password" placeholder="&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;"/></div>
  </div>
  <div id="add-error"></div>
  <div style="display:flex;gap:10px;margin-top:12px">
    <button class="btn btn-primary" id="add-btn2" onclick="submitAddCamera()">Connect</button>
  </div>
</div>

<div class="modal-backdrop" id="rename-modal">
  <div class="modal">
    <h3>Rename Camera</h3>
    <input type="text" id="rename-input" placeholder="Camera name"/>
    <div class="modal-btns">
      <button class="btn btn-ghost btn-sm" onclick="closeRename()">Cancel</button>
      <button class="btn btn-primary btn-sm" onclick="submitRename()">Save</button>
    </div>
  </div>
</div>

<!-- 2.6.6: per-camera settings, opened from the cog on each card -->
<div class="modal-backdrop" id="cam-settings-modal" onclick="if(event.target.id==='cam-settings-modal')closeCamSettings()">
  <div class="modal cs-modal">
    <h3 id="cs-title">Settings</h3>
    <div class="cs-global" id="cs-global" style="display:none">Global recording settings are on in the
      Configuration tab, so they apply to every camera. Turn them off there to set each camera here.</div>
    <div class="cs-section">Motion</div>
    <div class="cs-label"><span>Sensitivity</span><span class="cs-val" id="cs-level-val">63</span></div>
    <div class="cs-slider">
      <input type="range" id="cs-level" min="1" max="100" step="1" oninput="csLevelShow()">
      <div class="cs-peak-mark" id="cs-peak-mark" style="display:none" title="Biggest recent movement"></div>
    </div>
    <div class="cs-ends"><span>Less sensitive</span><span>More sensitive</span></div>
    <div class="cs-peak" id="cs-peak"></div>
    <div class="cs-mode" id="cs-mode"></div>
    <div class="cs-night-note" id="cs-night-note" style="display:none"></div>
    <div class="cs-section">Recording</div>
    <div class="cs-grid">
      <label>Cooldown (s)<input type="number" id="cs-cooldown" min="1" max="300"></label>
      <label>Tail (s)<input type="number" id="cs-tail" min="0" max="30"></label>
      <label>File length<select id="cs-clip"></select></label>
    </div>
    <label class="cs-full">Recording folder<input type="text" id="cs-path" spellcheck="false"></label>
    <div class="cs-help">Under /media. For a Samba or NFS share, add it in Home Assistant
      (Settings, System, Storage, Add network storage, usage Media); it appears as
      /media/&lt;name&gt;. SFTP and FTP upload are planned for a later version.</div>
    <div class="cs-error" id="cs-error"></div>
    <div class="modal-btns">
      <button class="btn btn-ghost btn-sm" id="cs-reset" onclick="resetCamSettings()" style="margin-right:auto">Defaults</button>
      <button class="btn btn-ghost btn-sm" onclick="closeCamSettings()">Cancel</button>
      <button class="btn btn-primary btn-sm" id="cs-save" onclick="saveCamSettings()">Save</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="nc-modal" onclick="if(event.target.id==='nc-modal')closeNotCamModal()">
  <div class="modal nc-modal-inner">
    <h3>&#x1F6AB; Not a Camera</h3>
    <p class="nc-subtitle">What kind of device is <strong id="nc-device-label"></strong>?</p>
    <div class="nc-reasons">
      <button class="nc-reason-btn" data-reason="printer"    onclick="selectReason(this)">&#x1F5A8; Printer</button>
      <button class="nc-reason-btn" data-reason="router"     onclick="selectReason(this)">&#x1F310; Router / Firewall</button>
      <button class="nc-reason-btn" data-reason="nas"        onclick="selectReason(this)">&#x1F4BE; NAS / Storage</button>
      <button class="nc-reason-btn" data-reason="computer"   onclick="selectReason(this)">&#x1F4BB; Computer</button>
      <button class="nc-reason-btn" data-reason="tv"         onclick="selectReason(this)">&#x1F4FA; Smart TV</button>
      <button class="nc-reason-btn" data-reason="iot"        onclick="selectReason(this)">&#x1F4F1; IoT Device</button>
      <button class="nc-reason-btn" data-reason="unknown"    onclick="selectReason(this)">&#x2753; Not sure</button>
    </div>
    <div class="nc-detail-row">
      <input type="text" id="nc-detail" placeholder="Optional: any extra detail (e.g. model name)" maxlength="200"/>
    </div>
    <div class="nc-share-row" id="nc-share-row">
      <label class="nc-share-label">
        <input type="checkbox" id="nc-share">
        <span>Share this anonymously to help improve AnyCam for everyone</span>
      </label>
      <p class="nc-share-note">No IP addresses are sent — only device type, OUI, open ports, and service banners.</p>
    </div>
    <div class="modal-btns">
      <button class="btn btn-ghost btn-sm" onclick="closeNotCamModal()">Cancel</button>
      <button class="btn btn-danger btn-sm" onclick="submitNotCam()">Confirm — Not a Camera</button>
    </div>
  </div>
</div>

<!-- 2.4.0-rc2.0: Layered Stream Discovery — locked streams modal.
     Surfaces 401-locked RTSP paths the path walker found while probing
     this camera. User enters credentials; the existing cred-auth flow
     attempts to authenticate against ALL paths (working unauth + locked),
     so successful auth automatically unlocks any of these that share the
     same auth domain (which they all do — same-realm filter ensures it). -->
<div class="modal-backdrop" id="locked-modal" onclick="if(event.target.id==='locked-modal')closeLockedStreams()">
  <div class="modal locked-modal-inner">
    <h3>&#x1F512; Locked Streams</h3>
    <p class="locked-subtitle">
      AnyCam found <strong id="locked-count"></strong> additional stream(s) on
      <strong id="locked-device-label"></strong> that require credentials.
      Enter the camera's username and password to unlock them.
    </p>
    <div class="locked-list" id="locked-list"></div>
    <div class="locked-cred-form">
      <label>USERNAME</label>
      <input type="text" id="locked-user" placeholder="admin" autocomplete="username">
      <label>PASSWORD</label>
      <input type="password" id="locked-pass" placeholder="&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;"
        autocomplete="current-password"
        onkeydown="if(event.key==='Enter')submitLockedCreds()">
      <div class="cred-error" id="locked-err"></div>
    </div>
    <div class="modal-btns">
      <button class="btn btn-ghost btn-sm" onclick="closeLockedStreams()">Cancel</button>
      <button class="btn btn-primary btn-sm" onclick="submitLockedCreds()">Unlock</button>
    </div>
  </div>
</div>

<script>
""" + js_code + """
</script>
<!-- ── Storage view ───────────────────────────────────────────────────────── -->
<div class="view" id="storage-view">
  <div id="storage-topbar">
    <button class="btn btn-ghost btn-sm" onclick="switchView('cameras')">&#x2190; Back to Cameras</button>
    <div id="disk-bar-wrap">
      <span id="disk-label">Loading...</span>
      <div id="disk-bar-track"><div id="disk-bar-fill"></div></div>
    </div>
    <button class="btn btn-secondary btn-sm" onclick="loadStorage()">&#x21BB; Refresh</button>
  </div>
  <!-- Explorer pane: nav bar + column headers + file list all inside white box -->
  <div id="stor-explorer">
    <div id="stor-nav-bar">
      <button id="stor-back-btn" onclick="storNavBack()" title="Back" disabled>&#x2190;</button>
      <button id="stor-up-btn"   onclick="storNavUp()"   title="Up"   disabled>&#x2191;</button>
      <div id="stor-breadcrumb" onclick="if(!event.target.closest('#stor-path-root'))_pathBarEdit()">
        <span id="stor-path-root" onclick="_storNavTo(null);event.stopPropagation()"
              style="cursor:pointer;padding:2px 6px;border-radius:3px;color:#0066cc"
              title="/media/anycam">/media/anycam</span>
        <span id="stor-path-sep" style="display:none;color:#888;padding:0 2px">&rsaquo;</span>
        <span id="stor-path-folder" style="font-weight:600;color:#111;padding:2px 4px"></span>
      </div>
    </div>
    <div id="stor-col-headers">
      <span class="stor-col-name"  onclick="storSort('name')"  id="sorth-name">Name &#x25B2;</span>
      <span class="stor-col-date"  onclick="storSort('date')"  id="sorth-date">Date Modified</span>
      <span class="stor-col-type"  onclick="storSort('type')"  id="sorth-type">Type</span>
      <span class="stor-col-size"  onclick="storSort('size')"  id="sorth-size">Size</span>
      <span class="stor-col-acts"></span>
    </div>
    <div id="storage-list"></div>
  </div>
</div>



<!-- ── Focus / full-screen enhanced view overlay ──────────────────────────── -->
<div id="focus-overlay" style="display:none">
  <button id="focus-close" onclick="closeFocus()" title="Exit enhanced view (Esc)">&#x2715;</button>
  <div id="focus-warning" style="display:none">
    <span id="focus-warn-text"></span>
    <button onclick="document.getElementById('focus-warning').style.display='none'">OK</button>
  </div>
  <img id="focus-img" alt="">
  <div id="focus-video" style="display:none"></div>
  <div id="focus-loading" style="display:none">
    <div>Loading feed, please wait…</div>
    <div id="focus-loading-note"></div>
  </div>
  <div id="focus-bar">
    <div id="focus-info">Loading…</div>
    <div id="focus-controls">
      <div id="focus-classic-grp" style="display:none">
        <button class="focus-engine-btn" onclick="focusUseClassic()" title="Compare against the classic view for this session">Classic</button>
      </div>
    </div>
  </div>
</div>

<!-- ── Toast notification ─────────────────────────────────────────────────── -->
<div id="toast" style="display:none"></div>

</body>
</html>"""


async def handle_index(request: web.Request) -> web.Response:
    global HTML
    if HTML is None:
        HTML = build_html()
    return web.Response(text=HTML, content_type="text/html")


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
anycam_host.bind(globals(), anycam_motion, anycam_storage, anycam_go2rtc, anycam_probe, anycam_scan, anycam_brand)

if __name__ == "__main__":
    asyncio.run(main())