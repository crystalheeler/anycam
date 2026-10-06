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
    api_motion_status, api_motion_toggle, api_motion_zones,
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
    _drain_stderr, _stop_proc, api_logs,
    handle_snap_status, handle_snapshot, snap_loop,
)
# 3.0.0-rc1.5 (E1): anycam_credentials.
import anycam_credentials
from anycam_credentials import (
    _match_stream_db, _streams_refresh, api_add_camera, api_clear_credentials,
    api_deep_reprobe, api_dvr_enum_status, api_set_credentials,
)
# 3.6.0 (C14): recording upload.
import anycam_upload
from anycam_upload import api_upload_settings, api_upload_test, upload_load, upload_worker
# 3.1.0 (C19): live MJPEG cards.
import anycam_mjpeg
from anycam_mjpeg import _mjpeg_source, handle_mjpeg_ws
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

CURRENT_VERSION = "3.7.1"  # must match config.yaml

INGRESS_PATH = os.environ.get("INGRESS_PATH", "").rstrip("/")
PORT         = int(os.environ.get("INGRESS_PORT", 8099))
# go2rtc removed — snap_loop connects directly to cameras

# ── HA add-on configuration options (set in the HA UI Config tab) ─────────────
# Read from env vars set by the HA supervisor from config.yaml options.
# Defaults mirror the config.yaml defaults so the server works without HA too.
CFG_HW_DECODE            = os.environ.get("HW_DECODE",      "false").lower() == "true"
CFG_ADAPTIVE_QUALITY     = os.environ.get("ADAPTIVE_QUALITY","false").lower() == "true"
# 3.0.1 (C11): Skip Non-Reference Frames, Fast Stream Start, Stagger
# Polling, Low FPS Mode and Limit Threads are gone (CrystalHeeler,
# 2026-10-03: the cards should run at the fullest rate possible).
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


# Enhanced View adaptive quality, per camera: anycam_snap.py steps it,
# anycam_focus.py resets it.
_FOCUS_ADAPTIVE:         dict  = {}    # camera_id → {tier_idx, locked, run_start, ladder,
                                       #               restarts_since_lock}


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
#   3. go2rtc's RTSP server listens on 127.0.0.1 only and asks for a
#      password made new at each start (3.3.0, C4; before, it was off).
#      AnyCam's ffmpeg jobs read the cameras through it, so go2rtc holds one
#      connection per camera stream; no other program can use it without
#      the password, and nothing outside the Pi can reach it.
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


# ── 3.1.0 (D3): one card order for every viewer ─────────────────────────────
# The page sends the order after a card is dragged; the add-on keeps it in
# runtime.json and returns the cameras in that order. A card is named by
# _card_key, the same key as _stableCardKey in page_script.py: the address
# and port survive a password entry, which changes a camera's id.
CARD_ORDER_MAX = 500            # keys kept; more cards than this are not expected
CARD_KEY_MAX_LEN = 200


def _card_key(cam: dict) -> str:
    """The card's lasting name: ip:port, plus #chN for a DVR channel."""
    if cam.get("ip"):
        key = f"{cam['ip']}:{cam.get('port') or ''}"
        if cam.get("channel"):
            key += f"#ch{cam['channel']}"
        return key
    return f"id:{cam.get('id', '')}"


_CARD_ORDER: list[str] | None = None     # read from runtime.json at first use


def _card_order() -> list[str]:
    global _CARD_ORDER
    if _CARD_ORDER is None:
        _CARD_ORDER = list(load_runtime().get("card_order") or [])
    return _CARD_ORDER


def _cards_in_order(cams: list[dict]) -> list[dict]:
    """The cameras in the saved card order; cards not in it keep their place at the end."""
    order = _card_order()
    if not order:
        return cams
    rank = {key: i for i, key in enumerate(order)}
    last = len(order)
    return [c for _, c in sorted(enumerate(cams),
                                 key=lambda ic: (rank.get(_card_key(ic[1]), last + ic[0]),))]


async def api_card_order(request: web.Request) -> web.Response:
    """GET or POST /api/card_order — the saved card order, a list of card keys."""
    global _CARD_ORDER
    if request.method == "POST":
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        order = data.get("order") if isinstance(data, dict) else None
        if (not isinstance(order, list) or len(order) > CARD_ORDER_MAX
                or not all(isinstance(k, str) and 0 < len(k) <= CARD_KEY_MAX_LEN for k in order)):
            return web.json_response({"error": "order must be a list of card keys"}, status=400)
        _CARD_ORDER = list(dict.fromkeys(order))         # duplicates dropped, order kept
        rt = load_runtime()
        rt["card_order"] = _CARD_ORDER
        save_runtime(rt)
        log.info(f"Card order saved ({len(_CARD_ORDER)} cards)")
    return web.json_response({"order": _card_order()})


async def api_cameras(request: web.Request) -> web.Response:

    return web.json_response([_safe_cam(c) for c in _cards_in_order(list(CAMERAS.values()))])

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


# ── 3.0.1: the add-on's own Supervisor entry (F10, B26) ──────────────────────
# Every add-on may read and change its own entry, /addons/self/..., without
# hassio_api: the path is on the Supervisor's bypass list
# (supervisor/api/middleware/security.py).
_SELF_SLUG: str | None = None    # local_camera_discovery, or <repository>_camera_discovery


async def _supervisor_self(method: str, path: str,
                           payload: dict | None = None) -> dict | None:
    """Call the Supervisor's /addons/self/<path>; return its data, or None."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.request(method, f"http://supervisor/addons/self/{path}",
                                       headers={"Authorization": f"Bearer {token}"},
                                       json=payload) as resp:
                if resp.status != 200:
                    log.warning(f"Supervisor addons/self/{path}: HTTP {resp.status}")
                    return None
                body = await resp.json(content_type=None)
                return (body.get("data") or {}) if isinstance(body, dict) else {}
    except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
        log.warning(f"Supervisor addons/self/{path}: {ex}")
        return None


async def _store_defaults_once() -> None:
    """3.0.1 (F10): switch on Show in Sidebar and Auto update, once.

    The add-on manifest cannot set either: the Supervisor keeps them as user
    settings with default off. Done once per installation and recorded in
    runtime.json, so a later change by the user holds. A failure is retried
    at the next start.
    """
    if load_runtime().get("store_defaults_set"):
        return
    if await _supervisor_self("POST", "options",
                              {"ingress_panel": True, "auto_update": True}) is None:
        return
    runtime = load_runtime()
    runtime["store_defaults_set"] = CURRENT_VERSION
    save_runtime(runtime)
    log.info("Home Assistant: Show in Sidebar and Auto update switched on "
             "(one time; a later change in Home Assistant holds)")


async def api_self(request: web.Request) -> web.Response:
    """GET /api/self — this add-on's Home Assistant ID, for the log link (B26).

    The ID is local_camera_discovery for a copy in /addons, and
    <repository>_camera_discovery for a copy from an add-on store
    (supervisor/store/data.py), so the page cannot know it in advance.
    """
    global _SELF_SLUG
    if _SELF_SLUG is None:
        info = await _supervisor_self("GET", "info")
        if info and info.get("slug"):
            _SELF_SLUG = info["slug"]
    return web.json_response({"slug": _SELF_SLUG or "local_camera_discovery"})


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
    app.router.add_get(   "/api/self",                            api_self)
    app.router.add_get(   "/api/diagnostics/hw",                  api_diagnostics_hw)
    app.router.add_route("*", "/api/card_order",                     api_card_order)
    app.router.add_route("*", "/api/upload/settings",                api_upload_settings)
    app.router.add_post(  "/api/upload/test",                        api_upload_test)
    app.router.add_get(   "/api/mjpeg/{camera_id}/ws",            handle_mjpeg_ws)
    app.router.add_post(  "/snap/focus/{camera_id}",              handle_focus_set)
    app.router.add_delete("/snap/focus",                          handle_focus_clear)
    app.router.add_post(  "/api/cameras/{camera_id}/motion",      api_motion_toggle)
    app.router.add_get(   "/api/cameras/{camera_id}/motion",      api_motion_status)
    app.router.add_get(   "/api/motion",                          api_motion_all)
    app.router.add_route("*", "/api/cameras/{camera_id}/motion/settings", api_motion_settings)
    app.router.add_route("*", "/api/cameras/{camera_id}/motion/zones", api_motion_zones)
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


VAAPI_DEVICE = "/dev/dri/renderD128"


async def _vaapi_usable() -> tuple[bool, str]:
    """3.0.1 (B12): can ffmpeg open a VAAPI device? Return (yes, reason if not).

    ffmpeg listing "vaapi" under -hwaccels shows only that it was built with
    VAAPI. A Raspberry Pi 4 has a render node but no VAAPI driver, so the
    old check reported hevc_vaapi and h264_vaapi available there.
    """
    if not os.path.exists(VAAPI_DEVICE):
        return False, f"no {VAAPI_DEVICE}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-init_hw_device", f"vaapi=va:{VAAPI_DEVICE}",
            "-f", "lavfi", "-i", "color=black:s=64x64:d=0.1", "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (OSError, asyncio.TimeoutError) as ex:
        return False, f"ffmpeg could not test VAAPI: {ex}"
    if proc.returncode != 0:
        lines = (err or b"").decode("utf-8", "replace").strip().splitlines()
        return False, (f"no VAAPI driver opens {VAAPI_DEVICE}"
                       + (f" ({lines[-1][:120]})" if lines else ""))
    return True, ""


# ── 3.7.0 (B11, C9): which decoder devices the add-on can use ───────────────
# Logged at start-up, and answered by /api/diagnostics/hw, so a log shows at
# once whether the Pi's decoders exist (C9: the rpivid overlay) and whether
# the add-on may open them (B11). The fix for B11 waits for CrystalHeeler's
# logs (CLAUDE.md rule 2); this only reports.
HW_DEVICE_GLOBS = ("/dev/video*", "/dev/media*", "/dev/dri/renderD*")
_HW_REPORT: dict = {}


def _hw_device_report() -> dict:
    """Each decoder device: its name, and whether it opens for reading and writing."""
    import glob
    devices = []
    for pattern in HW_DEVICE_GLOBS:
        for dev in sorted(glob.glob(pattern)):
            name = ""
            sysname = Path("/sys/class/video4linux") / Path(dev).name / "name"
            if dev.startswith("/dev/video"):
                try:
                    name = sysname.read_text(encoding="utf-8").strip()
                except OSError:
                    pass
            try:
                fd = os.open(dev, os.O_RDWR | getattr(os, "O_NONBLOCK", 0))
                os.close(fd)
                opens = "yes"
            except OSError as ex:
                opens = ex.strerror or str(ex)
            devices.append({"device": dev, "name": name, "opens": opens})
    names = " ".join(d["name"].lower() for d in devices)
    report = {
        "devices": devices,
        # the HEVC decoder (C9). 3.7.1 (B11): newer Raspberry Pi kernels name
        # it rpi-hevc-dec, not rpivid; 3.7.0 then warned of a missing overlay.
        "rpivid": "rpivid" in names or "rpi-hevc-dec" in names,
        "bcm2835_codec": "bcm2835-codec-decode" in names,    # the H.264 decoder
        "blocked": [d["device"] for d in devices if d["opens"] != "yes"],
    }
    notes = []
    if not devices:
        notes.append("no decoder device is visible inside the add-on")
    elif not report["rpivid"] and any(d["device"].startswith("/dev/video") for d in devices):
        notes.append("no rpivid HEVC decoder: on a Pi 4, add dtoverlay=rpivid-v4l2 to "
                     "/boot/firmware/config.txt and restart the Pi")
    if report["blocked"]:
        # 3.7.1 (B11): confirmed 2026-10-06 on test system B: with the
        # add-on's Protection mode off, every device opens.
        notes.append("the add-on may not open " + ", ".join(report["blocked"])
                     + "; hardware decode on these falls back to software. To allow it, "
                     "turn off Protection mode on AnyCam's Info page and restart AnyCam")
    report["notes"] = notes
    return report


def _hw_report_log(report: dict) -> None:
    for d in report["devices"]:
        log.info(f"  HW device {d['device']}" + (f" ({d['name']})" if d["name"] else "")
                 + (": opens" if d["opens"] == "yes" else f": cannot open — {d['opens']}"))
    for note in report["notes"]:
        log.warning(f"  HW decode: {note}")


async def api_diagnostics_hw(request: web.Request) -> web.Response:
    """GET /api/diagnostics/hw (3.7.0) — devices, decoder results, software fallbacks."""
    report = _HW_REPORT or await asyncio.to_thread(_hw_device_report)
    return web.json_response({
        **report,
        "hw_decode_setting": CFG_HW_DECODE,
        "unavailable": sorted(_HW_UNAVAILABLE),
        "candidates": [label for label, _c, _a in _HW_DECODER_CANDIDATES],
        "software_fallback": dict(anycam_snap._HW_FALLBACK),
    })


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
    # 3.7.0 (B11, C9): the devices first, also with hardware decode off
    _HW_REPORT.clear()
    _HW_REPORT.update(await asyncio.to_thread(_hw_device_report))
    _hw_report_log(_HW_REPORT)
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
    vaapi_state: tuple[bool, str] | None = None     # 3.0.1 (B12): tested once
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
                # 3.7.1 (B11): present is not enough; the add-on must also
                # be allowed to open them, or ffmpeg decodes in software.
                blocked = [d for d in ("/dev/video19", "/dev/media0")
                           if d in _HW_REPORT.get("blocked", [])]
                if rpivid_loaded and blocked:
                    _HW_UNAVAILABLE.add(label)
                    log.info(f"  {label}: unavailable (the add-on may not open "
                             f"{', '.join(blocked)}: Protection mode is on)")
                    continue
                if not rpivid_loaded:
                    _HW_UNAVAILABLE.add(label)
                    reason = ("rpivid not loaded — /dev/video19 or "
                              "/dev/media0 missing. Add 'dtoverlay="
                              "rpivid-v4l2' to /boot/firmware/config.txt "
                              "and reboot the Pi.")
                    log.info(f"  {label}: unavailable ({reason})")
                    continue
            if hwaccel_name == "vaapi":
                if vaapi_state is None:
                    vaapi_state = await _vaapi_usable()
                if not vaapi_state[0]:
                    _HW_UNAVAILABLE.add(label)
                    log.info(f"  {label}: unavailable ({vaapi_state[1]})")
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
    # 3.6.0 (C14): recordings waiting to upload, then the uploader
    upload_load()
    asyncio.create_task(upload_worker())

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
    # 3.0.1: keep the F10 marker too, or the defaults would be set again.
    _runtime_data = {k: v for k, v in load_runtime().items()
                     if k in ("last_scan_duration", "store_defaults_set")}
    _runtime_data["version"] = CURRENT_VERSION
    save_runtime(_runtime_data)
    asyncio.create_task(_store_defaults_once())      # 3.0.1 (F10)

    # Background: download/refresh IEEE OUI database (non-blocking)
    asyncio.create_task(refresh_oui_db())

    # Block until SIGTERM/SIGINT flips the stop event, then run cleanup.
    # runner.cleanup() invokes app.on_shutdown handlers (incl. _on_shutdown)
    # which kills ffmpeg children, persists last_frame_wall, etc.
    await _STOP_EVENT.wait()
    log.info("Shutdown signal received — running cleanup...")
    await runner.cleanup()


# 3.0.0-rc1.1: give the other modules the names they take from this file.
anycam_host.bind(globals(), anycam_motion, anycam_storage, anycam_go2rtc, anycam_probe, anycam_scan, anycam_brand, anycam_page, anycam_focus, anycam_snap, anycam_credentials, anycam_mjpeg, anycam_upload)

if __name__ == "__main__":
    asyncio.run(main())