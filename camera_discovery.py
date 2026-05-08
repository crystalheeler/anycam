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
from pathlib import Path
from urllib.parse import urlparse, quote

from aiohttp import web
import aiohttp
from cryptography.fernet import Fernet

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

CURRENT_VERSION = "2.5.0-rc1.9"  # must match config.yaml

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
CFG_RECORDINGS           = os.environ.get("RECORDINGS_PATH", "/media/anycam")
CFG_MOTION_SENS          = int(os.environ.get("MOTION_SENSITIVITY",       "15"))
CFG_MOTION_COOL          = int(os.environ.get("MOTION_COOLDOWN_SECS",     "10"))
CFG_MOTION_PAD           = int(os.environ.get("MOTION_CLIP_PADDING_SECS", "3"))
CFG_UNRESTRICTED_BROWSER = os.environ.get("UNRESTRICTED_STORAGE_BROWSER", "false").lower() == "true"
CFG_LOG_DEBUG            = os.environ.get("LOG_DEBUG",   "false").lower() == "true"
CFG_LOG_INFO             = os.environ.get("LOG_INFO",    "true").lower()  == "true"
CFG_LOG_WARNING          = os.environ.get("LOG_WARNING", "true").lower()  == "true"
CFG_LOG_ERROR            = os.environ.get("LOG_ERROR",   "true").lower()  == "true"

MEDIA_DIR = Path(CFG_RECORDINGS)

# ── Stream database — compact form of the RTSP/MJPEG URL database ─────────────
# Keyed by lowercase manufacturer slug.  Used for:
#   1. Post-login silent probe of alternate stream paths
#   2. Pre-login heuristic probing when ONVIF returns no profiles
#
# Fields:
#   match  — substrings to look for in camera name / vendor / model (lowercase)
#   rtsp   — RTSP path candidates to probe (ordered: most likely first)
#   mjpeg  — HTTP MJPEG stream path (None if not supported)
#   snap   — HTTP JPEG snapshot path (None if not available)
#   port   — default RTSP port (554 unless the brand uses something else)
STREAM_DB: dict = {
    # ── Professional / Enterprise ──────────────────────────────────────────────
    "hikvision": {
        "match": ["hikvision", "hikv", "ds-2", "ds-7", "ds-6", "isapi"],
        "rtsp":  ["/Streaming/Channels/101", "/Streaming/Channels/102",
                  "/Streaming/Channels/103",
                  "/ISAPI/Streaming/channels/101", "/ISAPI/Streaming/channels/102",
                  "/h.264/ch1/main/av_stream", "/h.264/ch1/sub/av_stream"],
        "mjpeg": "/ISAPI/Streaming/channels/102/httpPreview",
        "snap":  "/ISAPI/Streaming/channels/101/picture",
        "port":  554,
    },
    "ezviz": {
        "match": ["ezviz"],
        "rtsp":  ["/Streaming/Channels/101", "/Streaming/Channels/102"],
        "mjpeg": "/ISAPI/Streaming/channels/102/httpPreview",
        "snap":  "/ISAPI/Streaming/channels/101/picture",
        "port":  554,
    },
    "dahua": {
        "match": ["dahua", "dh-ipc", "dh-sd", "ipc-hfw", "ipc-hdw", "ipc-hdb",
                  "sd4", "sd5", "sd6", "hfw", "hdw"],
        "rtsp":  ["/cam/realmonitor?channel=1&subtype=0",
                  "/cam/realmonitor?channel=1&subtype=1",
                  "/cam/realmonitor?channel=1&subtype=2"],
        "mjpeg": "/cgi-bin/mjpg/video.cgi?channel=1&subtype=1",
        "snap":  "/cgi-bin/snapshot.cgi",
        "port":  554,
    },
    "imou": {
        "match": ["imou"],
        "rtsp":  ["/cam/realmonitor?channel=1&subtype=0",
                  "/cam/realmonitor?channel=1&subtype=1"],
        "mjpeg": "/cgi-bin/mjpg/video.cgi?channel=1&subtype=1",
        "snap":  "/cgi-bin/snapshot.cgi",
        "port":  554,
    },
    "amcrest": {
        "match": ["amcrest"],
        "rtsp":  ["/cam/realmonitor?channel=1&subtype=0",
                  "/cam/realmonitor?channel=1&subtype=1"],
        "mjpeg": "/cgi-bin/mjpg/video.cgi?channel=1&subtype=1",
        "snap":  "/cgi-bin/snapshot.cgi",
        "port":  554,
    },
    "axis": {
        "match": ["axis"],
        "rtsp":  ["/axis-media/media.amp", "/axis-media/media.amp?videocodec=h264",
                  "/axis-media/media.amp?videocodec=h265", "/mpeg4/media.amp"],
        "mjpeg": "/axis-cgi/mjpg/video.cgi",
        "snap":  "/axis-cgi/jpg/image.cgi",
        "port":  554,
    },
    "hanwha": {
        "match": ["hanwha", "wisenet", "samsung", "snv-", "xnv-", "qnv-", "pnv-",
                  "qnd-", "xnd-", "pnd-"],
        "rtsp":  ["/profile1/media.smp", "/profile2/media.smp",
                  "/profile10/media.smp", "/0/profile2/media.smp"],
        "mjpeg": "/stw-cgi/video.cgi?msubmenu=stream&action=view&Profile=1&CodecType=MJPEG&Resolution=800x450&FrameRate=15&CompressionLevel=10",
        "snap":  "/stw-cgi/image.cgi?msubmenu=snapshot&action=view",
        "port":  554,
    },
    "uniview": {
        "match": ["uniview", "unv", "ipc3", "ipc6", "ipc8"],
        "rtsp":  ["/media/video1", "/media/video2", "/media/video3"],
        "mjpeg": None,
        "snap":  "/images/snapshot.jpg",
        "port":  554,
    },
    "vivotek": {
        "match": ["vivotek", "vivo", "fd8", "fd9", "ip8", "ip9", "cc8", "ms8"],
        "rtsp":  ["/live1s1", "/live1s2", "/live.sdp", "/live2.sdp"],
        "mjpeg": "/video.mjpg",
        "snap":  "/cgi-bin/viewer/video.jpg",
        "port":  554,
    },
    "bosch": {
        "match": ["bosch", "ndc-", "nti-", "nbn-", "nbe-", "nuc-"],
        "rtsp":  ["/rtsp_tunnel", "/?inst=1", "/?inst=2"],
        "mjpeg": None,
        "snap":  "/snap.jpg",
        "port":  554,
    },
    "pelco": {
        "match": ["pelco", "sarix", "optera", "spectra"],
        "rtsp":  ["/stream1", "/stream2", "/?video"],
        "mjpeg": "/media/mjpeg",
        "snap":  "/media/jpeg",
        "port":  554,
    },
    "avigilon": {
        "match": ["avigilon"],
        "rtsp":  ["/defaultPrimary?streamType=u", "/defaultSecondary?streamType=u",
                  "/defaultPrimary-0?streamType=u", "/defaultPrimary-1?streamType=u"],
        "mjpeg": None,
        "snap":  None,   # generated in camera web UI per-stream
        "port":  554,
    },
    "mobotix": {
        "match": ["mobotix", "mx-", "mxfb"],
        "rtsp":  ["/mobotix.h264", "/stream/profile0", "/stream/profile1",
                  "/onvif/stream0/mobotix.mjpeg"],
        "mjpeg": "/cgi-bin/faststream.jpg?stream=MxPEG",
        "snap":  "/cgi-bin/faststream.jpg?stream=snapshot",
        "port":  554,
    },
    "geovision": {
        "match": ["geovision", "gv-", "geo-"],
        "rtsp":  ["/CH001.sdp", "/CH002.sdp", "/h264.sdp"],
        "mjpeg": "/mjpeg?cam=1",
        "snap":  "/PictureCatch.cgi?CH=1",
        "port":  8554,
    },
    "panasonic": {
        "match": ["panasonic", "wv-s", "wv-x", "wv-v", "wv-u", "wv-sc", "bl-c",
                  "i-pro"],
        "rtsp":  ["/MediaInput/h264", "/MediaInput/h264/stream_1/ch_1",
                  "/MediaInput/h264/stream_2/ch_1"],
        "mjpeg": "/nphMotionJpeg?Resolution=640x480&Quality=Standard",
        "snap":  "/SnapShotJPEG?Resolution=640x480&Quality=Clarity",
        "port":  554,
    },
    "acti": {
        "match": ["acti", "tcm-", "kce-", "e21", "e22", "e23", "e24", "e31"],
        "rtsp":  ["/track1", "/track2"],
        "mjpeg": None,
        "snap":  "/snapshot.jpg",
        "port":  554,
    },
    "tiandy": {
        "match": ["tiandy", "tc-c", "tc-h", "tc-r"],
        "rtsp":  ["/profile1", "/profile2"],
        "mjpeg": None,
        "snap":  None,
        "port":  554,
    },
    "honeywell": {
        "match": ["honeywell", "equip-", "hc3", "hp4", "hd4", "hb4"],
        "rtsp":  ["/Streaming/Channels/101", "/Streaming/Channels/102",
                  "/cam/realmonitor?channel=1&subtype=0"],
        "mjpeg": None,
        "snap":  "/ISAPI/Streaming/channels/101/picture",
        "port":  554,
    },
    "arecont": {
        "match": ["arecont", "av2", "av5", "av10", "av20"],
        "rtsp":  ["/h264.sdp", "/h264.sdp?res=full", "/h264.sdp1", "/h264.sdp2"],
        "mjpeg": "/mjpeg.cgi",
        "snap":  "/image.jpg",
        "port":  554,
    },
    "digital_watchdog": {
        "match": ["digital watchdog", "dw-", "dwc-"],
        "rtsp":  ["/1/stream1", "/1/stream2"],
        "mjpeg": None,
        "snap":  None,
        "port":  554,
    },
    "sony": {
        "match": ["sony", "snc-", "srg-", "srd-"],
        "rtsp":  ["/media/video1", "/media/video2"],
        "mjpeg": "/image?speed=1&size=3",
        "snap":  "/oneshotimage.jpg",
        "port":  554,
    },
    "iqinvision": {
        "match": ["iqinvision", "iqm", "iqe"],
        "rtsp":  ["/rtsp/now.mp4"],
        "mjpeg": None,
        "snap":  None,
        "port":  554,
    },
    "verint": {
        "match": ["verint"],
        "rtsp":  ["/live.sdp", "/live2.sdp", "/live3.sdp", "/live4.sdp"],
        "mjpeg": None,
        "snap":  None,
        "port":  554,
    },
    "ubiquiti": {
        "match": ["ubiquiti", "unifi", "uvc-"],
        "rtsp":  ["/{camera_id}"],   # served via UniFi Protect NVR
        "mjpeg": None,
        "snap":  None,
        "port":  7447,
    },
    # ── Consumer / Prosumer ────────────────────────────────────────────────────
    "reolink": {
        "match": ["reolink", "rlc-", "rlk-", "rlp-", "rln-"],
        "rtsp":  ["/h264Preview_01_main", "/h264Preview_01_sub",
                  "/Preview_01_main", "/Preview_01_sub",
                  "/h265Preview_01_main"],
        "mjpeg": None,
        # Snap requires credentials as URL params: &user={user}&password={pass}
        # http_snap_loop handles this via reolink_snap_auth flag
        "snap":  "/cgi-bin/api.cgi?cmd=Snap&channel=0&rs=AnyCam",
        "port":  554,
    },
    "foscam": {
        "match": ["foscam", "fi8", "fi9", "r2", "r4"],
        "rtsp":  ["/videoMain", "/videoSub"],
        "mjpeg": "/videostream.cgi",
        "snap":  "/cgi-bin/CGIProxy.fcgi?cmd=snapPicture2",
        "port":  88,
    },
    "tplink": {
        "match": ["tapo", "tp-link", "tplink", "c100", "c200", "c300", "c310",
                  "c320", "vigi"],
        "rtsp":  ["/stream1", "/stream2"],
        "mjpeg": None,
        "snap":  None,   # no HTTP snapshot endpoint — RTSP only
        "port":  554,
    },
    "annke": {
        "match": ["annke"],
        "rtsp":  ["/H264/ch1/main/av_stream", "/H264/ch1/sub/av_stream",
                  "/Streaming/channels/101", "/Streaming/channels/102"],
        "mjpeg": "/ISAPI/Streaming/channels/102/httpPreview",
        "snap":  "/ISAPI/Streaming/channels/101/picture",
        "port":  554,
    },
    "trendnet": {
        "match": ["trendnet", "tv-ip"],
        "rtsp":  ["/channel1", "/channel2"],
        "mjpeg": "/cgi/mjpg/mjpeg.cgi",
        "snap":  "/cgi-bin/video.jpg",
        "port":  554,
    },
    "dlink": {
        "match": ["d-link", "dlink", "dcs-"],
        "rtsp":  ["/play1.sdp", "/play2.sdp"],
        "mjpeg": "/video.cgi",
        "snap":  "/image.jpg",
        "port":  554,
    },
    "lorex": {
        "match": ["lorex"],
        "rtsp":  ["/cam/realmonitor?channel=1&subtype=0",
                  "/cam/realmonitor?channel=1&subtype=1",
                  "/ch01/0"],
        "mjpeg": None,
        "snap":  "/cgi-bin/snapshot.cgi",
        "port":  554,
    },
    "grandstream": {
        "match": ["grandstream", "gxv3"],
        "rtsp":  ["/0", "/1"],
        "mjpeg": None,
        "snap":  "/snapshot/view0.jpg",
        "port":  554,
    },
    "wansview": {
        "match": ["wansview", "ncm-", "ncb-", "w2", "w3", "w4", "w5", "w6",
                  "q5", "k1", "k2"],
        "rtsp":  ["/live/ch0", "/live/ch1", "/live/mpeg4"],
        "mjpeg": "/videostream.cgi",
        "snap":  "/mjpeg/snap.cgi?chn=0",
        "port":  554,
    },
    "eufy": {
        "match": ["eufy", "eufycam"],
        "rtsp":  ["/live0"],
        "mjpeg": None,
        "snap":  None,   # no HTTP snapshot — RTSP only (must enable in app)
        "port":  554,
    },
    "vstarcam": {
        "match": ["vstarcam", "c7", "c8", "c9"],
        "rtsp":  ["/udp/av0_0", "/udp/av0_1", "/tcp/av0_0", "/udp/av0_2"],
        "mjpeg": "/videostream.cgi",
        "snap":  None,
        "port":  554,
    },
    "swann": {
        "match": ["swann"],
        "rtsp":  ["/ch01/0", "/ch01/1",
                  "/Streaming/Channels/101",
                  "/cam/realmonitor?channel=1&subtype=0"],
        "mjpeg": None,
        "snap":  "/ISAPI/Streaming/channels/101/picture",
        "port":  554,
    },
    "hiseeu": {
        "match": ["hiseeu"],
        "rtsp":  ["/Streaming/Channels/101", "/Streaming/Channels/102",
                  "/cam/realmonitor?channel=1&subtype=0"],
        "mjpeg": None,
        "snap":  "/ISAPI/Streaming/channels/101/picture",
        "port":  554,
    },
    "flir": {
        "match": ["flir"],
        "rtsp":  ["/avc", "/avc/ch1"],
        "mjpeg": None,
        "snap":  None,
        "port":  554,
    },
    "sricam": {
        "match": ["sricam", "ipcam", "generic"],
        "rtsp":  ["/11", "/12", "/1", "/2", "/onvif1"],
        "mjpeg": "/videostream.cgi",
        "snap":  "/tmpfs/snap.jpg",
        "port":  554,
    },
    "microseven": {
        "match": ["microseven", "m7d", "m7b", "m7t", "hipcam", "hiipcam",
                  "hipcam realserver", "hiipcam/v100r003"],
        "rtsp":  ["/11", "/12", "/13", "/h264major", "/h264minor"],
        "mjpeg": "/auto.jpg",
        "snap":  "/tmpfs/snap.jpg",
        "port":  554,
    },
}

# Currently focused camera for full-screen enhanced view.
# When set, all other snap_loops throttle to 1fps; focused loop runs native res.
_FOCUSED_CAMERA: str | None = None

# Hardware decoder names unavailable on this system (detected at runtime).
# When v4l2m2m reports "Could not find a valid device", the decoder name
# is added here so future stream requests skip hw decode immediately.
_HW_UNAVAILABLE: set = set()

# 2.4.0-rc3.0: ordered list of (decoder_name, codec) candidates that
# _probe_hw_decoders tries at startup, and that snap_loop iterates when
# selecting a hardware decoder for a given stream. Module-level so both
# the probe and snap_loop see the same identifiers — previously the
# probe defined this as a local list and snap_loop referenced
# _HW_DECODER_CANDIDATES expecting it to be a global, causing NameError
# the first time a stream tried to launch with hw_decode toggled on.
# The bug had been latent since 2.2.5 because the probe always added
# every candidate to _HW_UNAVAILABLE on systems without HW decode, so
# the for loop in snap_loop iterated over an empty list (which would
# itself NameError in CPython but apparently never fired in practice
# until rc3.0). Order = preference: v4l2m2m (Pi/embedded) first, then
# vaapi (Intel/AMD GPU). Note that hevc_v4l2m2m is included for
# completeness but Pi 4 specifically does NOT expose stateful HEVC
# m2m — only stateless via rpivid (/dev/video19), which requires an
# ffmpeg build with --enable-v4l2-request that this addon doesn't
# currently bundle. See _probe_hw_decoders for rpivid detection
# and the more accurate "rpivid present but ffmpeg lacks v4l2-request"
# diagnostic message. Planned for 2.6.0: bundle a custom ffmpeg with
# rpivid support so HEVC HW decode actually works on Pi 4.
_HW_DECODER_CANDIDATES: list[tuple[str, str]] = [
    ("hevc_v4l2m2m", "hevc"),
    ("h264_v4l2m2m", "h264"),
    ("hevc_vaapi",   "hevc"),
    ("h264_vaapi",   "h264"),
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
    def emit(self, record) -> None:

        if record.levelno >= logging.WARNING:
            _LOG_BUFFER.append({
                "level": "error" if record.levelno >= logging.ERROR else "warning",
                "msg":   self.format(record),
                "t":     record.created,
            })
            if len(_LOG_BUFFER) > 200:
                _LOG_BUFFER.pop(0)

_buf_handler = _BufHandler()
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


# ── 2.5.0-rc1.0: streaming_recipe consumer infrastructure ────────────
# Two helpers used by find_rtsp_path's path-list builder and by the
# single-socket walker's SDP-parsing branch when a brand entry's
# streaming_recipe directs us to walk DVR/NVR channels rather than
# the universal RTSP_PATHS list.

# Codec match for the "sdp_has_video_track" populated-channel test.
# Pattern intentionally matches both H264 and H.264 etc. by treating
# the dot as an optional character via the regex below.
_SDP_VIDEO_CODEC_RE = re.compile(
    r"a=rtpmap:\d+\s+(H\.?264|H\.?265|HEVC|MPEG[- ]?4)",
    re.IGNORECASE,
)


def _sdp_has_video_track(sdp_body: str) -> bool:
    """2.5.0-rc1.0: stricter populated-channel heuristic for DVR/NVR
    devices that allocate virtual stream slots regardless of whether a
    physical camera is connected to that channel.

    Field-tested case (Lorex D861A8B-Z): an 8-channel DVR with 5
    cameras connected returned RTSP/1.0 200 OK on ALL 8 channel-
    iterated DESCRIBE requests. Without a populated-channel filter, we
    would surface the first 200-OK channel as the working stream — and
    that channel might be one of the empty 3.

    Returns True when SDP contains an `m=video` line AND at least one
    `a=rtpmap` mapping to a real video codec (H.264 / H.265 / HEVC /
    MPEG4). Returns False when SDP is empty, lacks `m=video`, or has
    `m=video` but no recognised codec rtpmap.

    The plan flagged this heuristic as "needs empirical confirmation
    against a known-empty channel before finalising"; user opted to
    ship blind on the rationale that we will always be guessing for
    untested hardware. If field test on the Lorex D861A8B-Z surfaces
    phantom or missing cards, the fix lands as a 2.5.0-rc1.1 bumpfix
    refining either this regex or the `m=video` substring check."""
    if not sdp_body or "m=video" not in sdp_body.lower():
        return False
    return bool(_SDP_VIDEO_CODEC_RE.search(sdp_body))


def _expand_channel_iterate_paths(recipe: dict,
                                   channel_cap: int = 16) -> list[str]:
    """2.5.0-rc1.0: expand a `streaming_recipe` of type
    `channel_iterate` into an ordered RTSP path list.

    Per-channel ordering is `(channel, subtype)` lexicographic:
    channel 1 main, channel 1 sub, channel 2 main, channel 2 sub, ...
    Main-before-sub within a channel is intentional — when a camera
    sits on channel 3 the user wants `channel=3&subtype=0` (main)
    discovered before `channel=3&subtype=1` (sub) so the main stream
    becomes the primary stream URL.

    `channel_cap` defaults to 16 (4ch / 8ch / 16ch DVRs and most
    consumer NVRs). NVRs with >16 physical channels would need a
    per-entry override (deferred to a later release; tracked in the
    Lorex/Dahua DVR Family Support Plan). Caps the recipe's `channels`
    list at the cap value, then appends `fallback_paths` verbatim.

    Returns [] for unrecognised recipe shape (wrong `type`, missing
    `path_template`) so callers can fall through to the universal path
    list without special-casing."""
    if recipe.get("type") != "channel_iterate":
        return []
    template = recipe.get("path_template", "")
    if not template:
        return []
    raw_channels = recipe.get("channels", list(range(1, 17)))
    channels    = [c for c in raw_channels if c <= channel_cap]
    subtypes    = recipe.get("subtypes", [0, 1])
    paths: list[str] = []
    for ch in channels:
        for st in subtypes:
            try:
                paths.append(template.format(ch=ch, st=st))
            except (KeyError, IndexError):
                # Malformed template — skip rather than raise; the
                # walker can still try fallback_paths below.
                continue
    paths.extend(recipe.get("fallback_paths", []))
    # 2.5.0-rc1.1: filter out paths that still contain unexpanded
    # `{...}` placeholders (e.g. recipe fallback_paths declared as
    # `/h264/ch{ch}/main/av_stream` for legacy Dahua firmware — these
    # were intended to iterate channels but the helper currently emits
    # them verbatim; without the filter the walker probes the literal
    # string and the camera responds 401 to a path that can never
    # match). Future improvement: expand placeholders in fallback_paths
    # the same way as path_template above; tracked separately. For now
    # this filter prevents noise probing without losing real-channel
    # coverage (which path_template-based paths above already provide).
    paths = [p for p in paths if "{" not in p and "}" not in p]
    return paths


def _extract_channel_from_rtsp_url(url: str) -> str:
    """2.5.0-rc1.2: pull the `channel=N` value out of a Dahua-format
    RTSP URL like `rtsp://.../cam/realmonitor?channel=3&subtype=0`.
    Returns the channel string (e.g. "3") or "" if not found.

    Used by the post-cred-auth channel enumeration helper to know
    which channel the cred-auth flow already established working,
    so we don't re-walk it during enumeration of the other channels.
    Tolerant of URL form: query may use `&` or `?` separator,
    case-insensitive on the param name."""
    if not url:
        return ""
    m = re.search(r"[?&]channel=(\d+)", url, re.IGNORECASE)
    return m.group(1) if m else ""


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
# 2.4.0-rc2.6: Pending-flush card creation buffer. During a scan, new
# cards discovered are routed here instead of CAMERAS so they don't
# render to the UI mid-scan. After dedup runs at end of scan, survivors
# flush into CAMERAS in one batch. None when no scan is in progress;
# scan-time card writers should call _publish_scan_card(cam) which
# handles the routing.
PENDING_CAMERAS: dict | None = None
BLACKLIST  = set()
_SCAN_CANCELLED = False   # set to True to request graceful scan abort
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

# Last ARP-discovered hosts — populated by run_scan(), consumed by Port Scan UI
ARP_HOSTS: list[dict] = []   # [{ip, hostname}, ...]
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

MJPEG_PATHS = [
    "/video", "/mjpeg", "/stream", "/stream.mjpeg", "/stream.jpg",
    "/image.mjpeg", "/mjpg/video.mjpg", "/cgi-bin/mjpg/video.cgi",
    "/videostream.cgi", "/mjpeg.cgi", "/video.cgi", "/live.jpg",
    "/snapshot.cgi?count=0", "/video0.mjpeg", "/.mjpg",
    "/cgi-bin/video.cgi", "/axis-cgi/mjpg/video.cgi",
]

HLS_PATHS = [
    "/index.m3u8", "/stream.m3u8", "/live/stream.m3u8",
    "/hls/stream.m3u8", "/hls/index.m3u8", "/live.m3u8",
    "/playlist.m3u8", "/channel1/index.m3u8", "/live/index.m3u8",
    "/streams/live.m3u8", "/hls/live/index.m3u8",
]

WEBRTC_PATHS = [
    "/whep", "/webrtc", "/api/webrtc", "/webrtc/offer",
    "/api/whep", "/offer", "/api/offer", "/live/webrtc", "/stream/webrtc",
]

WS_RTSP_PATHS = [
    "/api/ws", "/ws", "/stream/ws", "/live/ws",
    "/ws/stream", "/websocket", "/stream",
]

NON_CAMERA_KEYWORDS = [
    "router", "gateway", "firewall", "switch", "access point",
    "printer", "print server", "jetdirect", "ipp", "brother", "epson", "canon printer",
    "nas", "synology", "qnap", "drobo", "buffalo",
    "smart tv", "television", "blu-ray", "media player",
    "ups", "power management", "voip", "pbx", "phone",
    "thermostat", "hvac", "mikrotik", "ubiquiti", "edgerouter",
    "openwrt", "dd-wrt", "cisco", "juniper", "fortinet", "modem",
]

CAMERA_KEYWORDS = [
    "camera", "ipcam", "ipcamera", "cam", "dvr", "nvr", "cctv",
    "hikvision", "dahua", "reolink", "axis", "hanwha",
    "amcrest", "uniview", "vivotek", "bosch", "pelco",
    "rtsp", "onvif", "video server", "webcam",
]

# ─────────────────────────────────────────────────────────────────────────────
# Camera manufacturer / model database
#
# Each entry:
#   "name"     : canonical display name
#   "aliases"  : alternate spellings / brand families
#   "http_titles"  : substrings to match in HTML <title> (case-insensitive)
#   "http_body"    : substrings to match anywhere in page body (case-insensitive)
#   "http_headers" : substrings to match in any HTTP response header value
#   "nmap_products": substrings to match in nmap service product/version field
#   "onvif_scopes" : substrings to match in ONVIF WS-Discovery scope strings
#   "default_ports": hint ports commonly used by this manufacturer
#   "notes"        : human-readable notes shown in Identity section
# ─────────────────────────────────────────────────────────────────────────────

CAMERA_DB: list[dict] = [
    {
        "name": "Hikvision",
        "aliases": ["hik", "hikvision", "ds-2"],
        "http_titles": ["hikvision", "ds-2", "network camera", "ivms"],
        "http_body":   ["hikvision", "ivms-4200", "ds-2cd", "ds-2de", "hik-connect"],
        "http_headers":["hikvision", "webs/hikvision"],
        "nmap_products":["hikvision", "hikvision ip camera"],
        "onvif_scopes": ["hikvision"],
        "default_ports": [554, 8000, 80],
        "notes": ("Hikvision IP camera or NVR/DVR. URL pattern "
                  "/Streaming/Channels/N0X (101=ch1 main, 102=ch1 sub). "
                  "Multi-layer HEVC (H.265+) requires "
                  "`-fflags +discardcorrupt` to decode reliably. "
                  "Camera-level: no documented throttle. NVR-level: 128 "
                  "concurrent users (configurable, 0=unlimited)."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "128 (NVR-level, configurable, 0=unlim)",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Server-wide cap on NVR products. webSDK browser "
                           "plugin has separate 5-stream cap. Camera-level "
                           "products have no documented connection cap and "
                           "tolerate rapid sequential probes (verified on "
                           "DS-2DE4A425IW)."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("Standard RFC 2326. Digest auth "
                              "(realm typically \"IP Camera(<chipset>)\")."),
        "request_behaviors_confidence":"HIGH",
        # 2.4.0-rc2.4: skip_layer2: True. Hikvision single-camera units
        # (e.g. CrystalHeeler's Hikvision DS-2DE4A425IW PTZ) require auth — observed
        # consistently across all Layer 1 paths returning 401 with the
        # same realm. Layer 2's multi-socket walk produces the same
        # 401s on the same paths because realm/scheme are server-side,
        # not socket-side, per RFC 7235 §2.2. Live-tested: the Hikvision burned
        # ~45s through Layer 2 bail-after-10 before giving up. Setting
        # this flag short-circuits Layer 2 immediately when the camera
        # is in pre-creds discovery state. Users who want to verify
        # Layer 2 didn't miss a firmware-quirk path can click the
        # per-card "Deep Re-Probe" button (rc2.4) which runs Layer 2
        # on demand. NVR variants of Hikvision (Hikvision NVR entry)
        # already have this flag for the same reason.
        "skip_layer2":            True,
        "skip_layer2_confidence": "HIGH",
    },
    {
        "name": "Dahua",
        "aliases": ["dahua", "dhip", "dh-ipc", "imou"],
        "http_titles": ["dahua", "ipc", "nvr", "xvr", "imou"],
        "http_body":   ["dahua technology", "dahua", "imou", "lechange", "dh-ipc"],
        "http_headers":["dahua", "dh-"],
        "nmap_products":["dahua"],
        "onvif_scopes": ["dahua"],
        "default_ports": [37777, 80, 554],
        "notes": ("Dahua Technology IP camera, NVR/DVR or IMOU device. URL "
                  "pattern /cam/realmonitor?channel=N&subtype=M. Native DVR "
                  "protocol on port 37777. Camera-level: no documented "
                  "throttle. NVR-level: 128 concurrent users "
                  "(configurable, 0=unlimited)."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "128 (NVR-level, configurable, 0=unlim)",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Server-wide cap on NVR products. Camera-level "
                           "products have no documented connection cap."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": "Standard RFC 2326. Digest auth.",
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "Lorex",
        "aliases": ["lorex", "flir lorex", "flirlorex"],
        "http_titles": ["lorex", "lorex nvr", "lorex dvr", "flirlorex"],
        "http_body":   ["lorex", "lorextechnology", "lorex technology",
                        "flirlorex", "flir lorex"],
        "http_headers":["lorex", "flirlorex"],
        "nmap_products":["lorex"],
        "onvif_scopes": ["lorex"],
        "default_ports": [80, 443, 8080, 8888, 554, 34567],
        "notes": ("Lorex (FLIR-acquired, Dahua-OEM hardware) NVR/DVR or IP "
                  "camera. Inherits Dahua URL patterns and behavior. "
                  "FLIR→Dahua acquisition 2018."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "MED",
        "throttle_amount":          "128 (NVR-level, inherits Dahua)",
        "throttle_amount_confidence":"MED",
        "throttle_notes": "Inherits Dahua throttle behavior (Dahua-OEM hardware).",
        "throttle_notes_confidence":"MED",
        "request_behaviors": "Standard RFC 2326. Inherits Dahua RTSP/HTTP behavior.",
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "Reolink",
        "aliases": ["reolink"],
        "http_titles": ["reolink"],
        "http_body":   ["reolink", "reolink app"],
        "http_headers":["reolink"],
        "nmap_products":["reolink"],
        "onvif_scopes": ["reolink"],
        "default_ports": [554, 80, 8080, 9000],
        "notes": ("Reolink IP camera or NVR. URL pattern "
                  "/h264Preview_<ch>_<main|sub> (older) or "
                  "/Preview_<ch>_<main|sub> (newer). Some models require "
                  "beta firmware to expose RTSP/ONVIF. Battery-WiFi models "
                  "have 5-min preview cap then sleep+disconnect — needs "
                  "≥20s RTSP timeout. Bad cseq errors after fast restart "
                  "need 5+s pause; some models (C2/RLC-410/420) "
                  "auto-recover after ~30s, others (older RLC-423) require "
                  "camera reboot."),
        "throttle_type":            "restart_cooldown",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "5+s pause between fast restarts (or 30s self-clean / camera reboot)",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Stop/restart RTSP within ~1-2s causes RTP-Cseq "
                           "stream corruption that persists endlessly until "
                           "the cooldown elapses. Battery-WiFi models also "
                           "have a session_time_cap of 5 min preview before "
                           "sleep. 8MP+ models (TrackMix etc.) are reported "
                           "as unstable on RTSP — Neolink workaround often "
                           "required."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("Standard RFC 2326. Battery models need "
                              "extended socket timeout (≥20s) for wake-up. "
                              "Stop processes via stdin 'q' rather than "
                              "SIGKILL to allow clean TEARDOWN."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Axis",
        "aliases": ["axis communications", "axis network"],
        "http_titles": ["axis", "axis network camera", "axis video"],
        "http_body":   ["axis communications", "axis network camera", "axiscam"],
        "http_headers":["axis", "boa/"],
        "nmap_products":["axis network camera", "axis"],
        "onvif_scopes": ["axis"],
        "default_ports": [554, 80, 443],
        "notes": ("Axis Communications IP camera or encoder. URL "
                  "/axis-media/media.amp. GStreamer-based RTSP server. "
                  "Session timeout=60s default; OPTIONS keepalive every "
                  "30s recommended. Multi-session-on-one-TCP supported. "
                  "Companion line requires `Axis-Orig-Sw=true` query "
                  "param appended to RTSP URL — without it, returns 403. "
                  "Firmware 6.50+ returns 454 Session Not Found on session "
                  "timeout. ONVIF Profile S/G/T."),
        "throttle_type":            "unique_profile_cap",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "\"Too many viewers\" based on unique encoded profiles",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Cap is per UNIQUE stream profile, not per "
                           "client. If all clients request identical "
                           "settings, camera encodes once and serves all. "
                           "Companion variant returns 403 without "
                           "`Axis-Orig-Sw=true` query param — retry-on-403 "
                           "with that param appended works. Standards-strict "
                           "RFC 2326 implementation."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("RFC 2326 strict. Digest auth. Some models "
                              "(Companion) require `?Axis-Orig-Sw=true` "
                              "query param. SRTP/SRTCP supported on newer "
                              "firmware. RTSPS on rtsps:// URL."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Hanwha / Samsung Techwin",
        "aliases": ["hanwha", "samsung techwin", "wisenet", "qnv", "xnv"],
        "http_titles": ["wisenet", "hanwha", "samsung techwin", "snv-", "qnv-"],
        "http_body":   ["hanwha", "wisenet", "samsung techwin"],
        "http_headers":["hanwha", "wisenet"],
        "nmap_products":["hanwha", "wisenet", "samsung techwin"],
        "onvif_scopes": ["hanwha", "samsung"],
        "default_ports": [554, 80, 8080],
        "notes": ("Hanwha Vision (formerly Samsung Techwin) — Wisenet "
                  "series. URL /profile<N>/media.smp; multi-sensor models "
                  "use /<sensor#>/profile<N>/media.smp (e.g. "
                  "/0/profile2/media.smp). Ports 3702 and 49152 are "
                  "RESERVED (ONVIF discovery + RTP) — DO NOT use as "
                  "RTSP. Requires shared Session ID across all requests "
                  "from one client; mismatched IDs counted as separate "
                  "users."),
        "throttle_type":            "shared_session_id_required",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "All requests from one client must share Session ID",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("If a client opens multiple RTSP requests with "
                           "different Session IDs, the camera counts each "
                           "as a separate viewer (consuming concurrent-user "
                           "quota). Probe behavior is unaffected — our "
                           "probe never reuses Session IDs across paths "
                           "(each probe is independent). Documented for "
                           "future warning to users with NVR setups."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("Standard RFC 2326 with strict Session ID "
                              "tracking. Digest auth. Profile-G recording "
                              "and Profile-T analytics support."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Amcrest",
        "aliases": ["amcrest", "amcrest technologies"],
        "http_titles": ["amcrest", "amcrest ip"],
        "http_body":   ["amcrest", "amcrestsecurity"],
        "http_headers":["amcrest"],
        "nmap_products":["amcrest"],
        "onvif_scopes": ["amcrest"],
        "default_ports": [37777, 80, 554],
        "notes": ("Amcrest IP camera or NVR (Dahua-OEM hardware). "
                  "Inherits Dahua URL patterns and throttle behavior."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "MED",
        "throttle_amount":          "128 (NVR-level, inherits Dahua)",
        "throttle_amount_confidence":"MED",
        "throttle_notes": "Inherits Dahua throttle behavior (Dahua-OEM hardware).",
        "throttle_notes_confidence":"MED",
        "request_behaviors": "Standard RFC 2326. Inherits Dahua RTSP/HTTP behavior.",
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "Uniview (UNV)",
        "aliases": ["uniview", "unv", "univideo"],
        "http_titles": ["uniview", "unv", "network camera"],
        "http_body":   ["uniview", "univideo", "unv camera"],
        "http_headers":["uniview", "unv"],
        "nmap_products":["uniview", "unv"],
        "onvif_scopes": ["uniview"],
        "default_ports": [554, 80],
        "notes": ("Uniview (UNV) IP camera or NVR. URL pattern "
                  "/media/video<N>. H.265 buggy on many models — Camect "
                  "documentation explicitly recommends H.264 over H.265 "
                  "on UNV cameras. No documented connection-rate "
                  "throttle."),
        "request_behaviors": ("Standard RFC 2326. Recommend H.264 "
                              "transport over H.265 for stream stability."),
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "Vivotek",
        "aliases": ["vivotek"],
        "http_titles": ["vivotek", "network camera", "ip camera"],
        "http_body":   ["vivotek", "vvtk"],
        "http_headers":["vivotek", "vvtk-http"],
        "nmap_products":["vivotek"],
        "onvif_scopes": ["vivotek"],
        "default_ports": [554, 80, 8080],
        "notes": ("Vivotek IP camera or NVR. URL /live<N>s<M> or "
                  "/live.sdp. Documented 10-user concurrent limit (browser "
                  "+ VMS + NVR ALL count toward this). Server header: "
                  "\"Vivotek RtspServer\"."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "10 concurrent users (browser+VMS+NVR all count)",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Camera-level cap. Documented in Vivotek "
                           "support article. Does not affect rapid "
                           "sequential probes (we close socket between "
                           "paths, which releases the slot)."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("Standard RFC 2326. Server header "
                              "\"Vivotek RtspServer\" is identifying."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Bosch",
        "aliases": ["bosch security", "bosch camera", "autodome", "flexidome", "dinion"],
        "http_titles": ["bosch", "autodome", "flexidome", "dinion"],
        "http_body":   ["bosch security", "bosch camera", "dinion", "flexidome", "autodome"],
        "http_headers":["bosch"],
        "nmap_products":["bosch"],
        "onvif_scopes": ["bosch"],
        "default_ports": [554, 80, 443],
        "notes": "Bosch Security Systems IP camera",
    },
    {
        "name": "Pelco",
        "aliases": ["pelco", "sarix", "spectra", "optera"],
        "http_titles": ["pelco", "sarix", "spectra enhanced"],
        "http_body":   ["pelco", "sarix", "pelco.com"],
        "http_headers":["pelco"],
        "nmap_products":["pelco"],
        "onvif_scopes": ["pelco"],
        "default_ports": [554, 80],
        "notes": "Pelco IP camera (Motorola Solutions)",
    },
    {
        "name": "Sony",
        "aliases": ["sony ipela", "sony security", "snc-"],
        "http_titles": ["sony", "sony ipela", "snc-"],
        "http_body":   ["sony ipela", "sony security", "snc-rz", "snc-ep", "snc-vb"],
        "http_headers":["sony"],
        "nmap_products":["sony network camera", "sony ipela"],
        "onvif_scopes": ["sony"],
        "default_ports": [554, 80, 443],
        "notes": "Sony IPELA IP camera",
    },
    {
        "name": "Panasonic / i-PRO",
        "aliases": ["panasonic", "i-pro", "ipro", "wv-"],
        "http_titles": ["panasonic", "i-pro", "network camera", "wv-"],
        "http_body":   ["panasonic", "i-pro", "wv-sc", "wv-sf", "wv-sp"],
        "http_headers":["panasonic", "i-pro"],
        "nmap_products":["panasonic network camera", "i-pro"],
        "onvif_scopes": ["panasonic"],
        "default_ports": [554, 80, 443],
        "notes": "Panasonic / i-PRO IP camera",
    },
    {
        "name": "Avigilon",
        "aliases": ["avigilon", "motorola solutions"],
        "http_titles": ["avigilon"],
        "http_body":   ["avigilon", "avigilon corporation"],
        "http_headers":["avigilon"],
        "nmap_products":["avigilon"],
        "onvif_scopes": ["avigilon"],
        "default_ports": [554, 80, 443],
        "notes": "Avigilon (Motorola Solutions) IP camera or NVR",
    },
    {
        "name": "FLIR",
        "aliases": ["flir systems", "flir camera"],
        "http_titles": ["flir", "flir systems"],
        "http_body":   ["flir systems", "flir camera", "flir.com"],
        "http_headers":["flir"],
        "nmap_products":["flir"],
        "onvif_scopes": ["flir"],
        "default_ports": [554, 80, 443],
        "notes": "FLIR Systems thermal/optical camera",
    },
    {
        "name": "Mobotix",
        "aliases": ["mobotix"],
        "http_titles": ["mobotix", "mx-"],
        "http_body":   ["mobotix", "mx-q", "mx-s", "mobotix.com"],
        "http_headers":["mobotix", "mx-httpd"],
        "nmap_products":["mobotix"],
        "onvif_scopes": ["mobotix"],
        "default_ports": [554, 80, 443],
        "notes": "Mobotix IP camera",
    },
    {
        "name": "ACTi",
        "aliases": ["acti", "acti corporation"],
        "http_titles": ["acti"],
        "http_body":   ["acti corporation", "acti camera"],
        "http_headers":["acti"],
        "nmap_products":["acti"],
        "onvif_scopes": ["acti"],
        "default_ports": [554, 80, 443],
        "notes": "ACTi IP camera",
    },
    {
        "name": "GeoVision",
        "aliases": ["geovision", "gv-"],
        "http_titles": ["geovision", "gv-"],
        "http_body":   ["geovision", "geo vision", "gv-bx", "gv-ptz"],
        "http_headers":["geovision"],
        "nmap_products":["geovision"],
        "onvif_scopes": ["geovision"],
        "default_ports": [554, 80, 4550],
        "notes": "GeoVision IP camera or NVR",
    },
    {
        "name": "Foscam",
        "aliases": ["foscam"],
        "http_titles": ["foscam", "ip camera"],
        "http_body":   ["foscam", "foscam digital technologies"],
        "http_headers":["foscam"],
        "nmap_products":["foscam"],
        "onvif_scopes": ["foscam"],
        "default_ports": [554, 88, 80, 443],
        "notes": ("Foscam IP camera. URL paths /videoMain (high-res) and "
                  "/videoSub (low-res); audio at /audio. Some firmware "
                  "variants (FI9821P V3, FI98xx V3 series) use port 88 "
                  "for BOTH HTTP and RTSP, not the standard 554. Digest "
                  "auth required. ~4 concurrent connection cap reported "
                  "across community."),
        "throttle_type":            "concurrent_stream_cap",
        "throttle_type_confidence": "MED",
        "throttle_amount":          "~4 concurrent connections (community-reported)",
        "throttle_amount_confidence":"MED",
        "throttle_notes": ("Community-reported pattern across multiple "
                           "users. Does not affect rapid sequential "
                           "probes (we close socket between paths). "
                           "Some V3 firmware uses port 88 for both HTTP "
                           "and RTSP — try 88 if 554 connect fails."),
        "throttle_notes_confidence":"MED",
        "request_behaviors": ("Digest auth required. Some firmware "
                              "variants run RTSP on port 88 not 554."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Annke",
        "aliases": ["annke"],
        "http_titles": ["annke"],
        "http_body":   ["annke", "annke.com"],
        "http_headers":["annke"],
        "nmap_products":["annke"],
        "onvif_scopes": ["annke"],
        "default_ports": [554, 80, 8000],
        "notes": ("Annke IP camera or NVR/DVR (Hikvision OEM hardware "
                  "for IP line; Dahua-OEM for some DVR products). "
                  "Inherits upstream throttle behavior."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "MED",
        "throttle_amount":          "Inherits upstream (Hikvision/Dahua)",
        "throttle_amount_confidence":"MED",
        "throttle_notes": ("OEM rebrand — behavior inherits from upstream "
                           "manufacturer (Hikvision IP, Dahua DVR)."),
        "throttle_notes_confidence":"MED",
        "request_behaviors": "Inherits Hikvision/Dahua RTSP/HTTP behavior.",
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "Swann",
        "aliases": ["swann", "swann communications"],
        "http_titles": ["swann"],
        "http_body":   ["swann", "swann security", "swann communications"],
        "http_headers":["swann"],
        "nmap_products":["swann"],
        "onvif_scopes": ["swann"],
        "default_ports": [554, 80, 34567],
        "notes": ("Swann security camera or NVR/DVR. OEM rebrand — uses "
                  "Hikvision, Dahua, or Wansview hardware depending on "
                  "model. Behavior inherits from upstream manufacturer."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "LOW",
        "throttle_amount":          "Inherits upstream (Hikvision/Dahua/Wansview)",
        "throttle_amount_confidence":"LOW",
        "throttle_notes": ("OEM rebrand — varies by model. Some Swann "
                           "products are Wansview-OEM (Hipcam family) "
                           "and inherit that rate-limit behavior."),
        "throttle_notes_confidence":"MED",
        "request_behaviors": "Varies by hardware (Hikvision/Dahua/Wansview OEM).",
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "TP-Link Tapo / Kasa",
        "aliases": ["tapo", "kasa", "tp-link"],
        "http_titles": ["tapo", "kasa", "tp-link"],
        "http_body":   ["tapo", "tp-link tapo", "kasa camera"],
        "http_headers":["tp-link", "tapo"],
        "nmap_products":["tp-link", "tapo"],
        "onvif_scopes": ["tapo", "tp-link"],
        "default_ports": [554, 2020, 80],
        "notes": ("TP-Link Tapo / Kasa smart camera. URLs /stream1 "
                  "(main) and /stream2 (sub). ONVIF service runs on "
                  "port 2020 (NOT 80 or 8080). Battery-powered models "
                  "(C410, C420, C425, D230) do NOT support RTSP — "
                  "cloud-only by design. Tapo Care subscription occupies "
                  "1 main stream slot. Camera-account credentials are "
                  "separate from Tapo cloud login."),
        "throttle_type":            "concurrent_stream_cap",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "2 main + 2 sub streams max (4 total)",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Each RTSP/ONVIF connection occupies one "
                           "stream slot. Tapo Care subscription (cloud "
                           "recording) consumes 1 main slot. Battery "
                           "models (C410/C420/C425/D230) have no RTSP "
                           "at all. Reboot the camera to disconnect all "
                           "stream slots when stuck."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("ONVIF on port 2020 (NOT 80). Standard "
                              "RTSP on 554. Battery models — no RTSP "
                              "support."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Night Owl",
        "aliases": ["night owl", "nightowl"],
        "http_titles": ["night owl"],
        "http_body":   ["night owl", "nightowl security"],
        "http_headers":["night owl"],
        "nmap_products":["night owl"],
        "onvif_scopes": ["nightowl"],
        "default_ports": [554, 80, 34567],
        "notes": ("Night Owl security camera or NVR/DVR. OEM rebrand — "
                  "uses Hikvision or Dahua hardware. Native DVR protocol "
                  "on port 34567 (Hisilicon-based)."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "LOW",
        "throttle_amount":          "Inherits upstream (Hikvision/Dahua)",
        "throttle_amount_confidence":"LOW",
        "throttle_notes": "OEM rebrand — behavior inherits from upstream manufacturer.",
        "throttle_notes_confidence":"LOW",
        "request_behaviors": "Inherits Hikvision/Dahua RTSP/HTTP behavior.",
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "iENSO",
        "aliases": ["ienso", "ienso inc", "ienso camera"],
        "http_titles": ["ienso", "ienso camera", "ienso inc"],
        "http_body":   ["ienso", "ienso inc", "ienso.com", "made in canada"],
        "http_headers":["ienso"],
        "nmap_products":["ienso"],
        "onvif_scopes": ["ienso"],
        "default_ports": [554, 80, 8080],
        "notes": "iENSO embedded IP camera (Canada)",
    },
    {
        "name": "Digital Watchdog",
        "aliases": ["digital watchdog", "dw-"],
        "http_titles": ["digital watchdog", "dw megazip"],
        "http_body":   ["digital watchdog", "dwipnetwork", "dw.com"],
        "http_headers":["digital watchdog"],
        "nmap_products":["digital watchdog"],
        "onvif_scopes": ["digitalwatchdog"],
        "default_ports": [554, 80],
        "notes": "Digital Watchdog IP camera or NVR",
    },
    {
        "name": "March Networks",
        "aliases": ["march networks", "marchnetworks"],
        "http_titles": ["march networks"],
        "http_body":   ["march networks", "marchnetworks.com"],
        "http_headers":["march networks"],
        "nmap_products":["march networks"],
        "onvif_scopes": ["marchnetworks"],
        "default_ports": [554, 80],
        "notes": "March Networks IP camera or NVR",
    },
    {
        "name": "Nest / Google",
        "aliases": ["nest", "google nest"],
        "http_titles": ["nest", "dropcam"],
        "http_body":   ["nest labs", "google nest", "dropcam"],
        "http_headers":["nest"],
        "nmap_products":["nest", "dropcam"],
        "onvif_scopes": ["nest"],
        "default_ports": [554, 443, 80],
        "notes": ("Google Nest / Dropcam IP camera. CLOUD-ONLY by "
                  "design — no RTSP, ONVIF, or local stream API. Local "
                  "access requires reverse-engineered workarounds "
                  "(unsupported)."),
        "throttle_type":            "no_rtsp_support",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "n/a",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("No RTSP server present on device. Cloud-only "
                           "by manufacturer policy."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": "No local stream protocols supported.",
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Ring",
        "aliases": ["ring", "ring doorbell", "ring camera"],
        "http_titles": ["ring"],
        "http_body":   ["ring.com", "ring video", "ring doorbell"],
        "http_headers":["ring"],
        "nmap_products":["ring"],
        "onvif_scopes": ["ring"],
        "default_ports": [554, 443, 80],
        "notes": ("Ring doorbell or security camera (Amazon). CLOUD-ONLY "
                  "by design — no RTSP or ONVIF support."),
        "throttle_type":            "no_rtsp_support",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "n/a",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": "No RTSP server present on device. Cloud-only by Amazon design.",
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": "No local stream protocols supported.",
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Wyze",
        "aliases": ["wyze", "wyze cam"],
        "http_titles": ["wyze"],
        "http_body":   ["wyze", "wyzecam", "wyze cam"],
        "http_headers":["wyze"],
        "nmap_products":["wyze"],
        "onvif_scopes": ["wyze"],
        "default_ports": [554, 80],
        "notes": ("Wyze IP camera. RTSP NOT enabled by default — "
                  "requires flashing custom RTSP firmware (currently "
                  "in beta status; Wyze removed firmware files from "
                  "site). 3+ Wyze cameras on one network reportedly "
                  "causes instability."),
        "throttle_type":            "requires_custom_firmware",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "RTSP firmware aging/beta; not officially supported",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Wyze stock firmware has no RTSP. Beta RTSP "
                           "firmware exists but support is aging — "
                           "files removed from Wyze website. 3+ "
                           "cameras on same network → community-reported "
                           "instability."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("With custom firmware, standard RTSP. "
                              "Without it, no local stream protocols."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Eufy / Anker",
        "aliases": ["eufy", "anker", "eufysecurity"],
        "http_titles": ["eufy", "eufysecurity"],
        "http_body":   ["eufy", "eufysecurity", "anker innovations"],
        "http_headers":["eufy"],
        "nmap_products":["eufy"],
        "onvif_scopes": ["eufy"],
        "default_ports": [554, 80, 443],
        "notes": ("Eufy (Anker) IP camera. CLOUD-ONLY by design — most "
                  "models do not expose RTSP. Some HomeBase-paired "
                  "cameras can be enabled for local RTSP via the Eufy "
                  "app (URL /live0) but the feature is not universal."),
        "throttle_type":            "no_rtsp_support",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "n/a (most models)",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("No RTSP server on most models. HomeBase users "
                           "can opt in to local RTSP per-camera in the "
                           "Eufy mobile app, exposing /live0."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("Cloud-only by default. Optional local "
                              "RTSP /live0 on HomeBase-paired models."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Arlo",
        "aliases": ["arlo", "arlo technologies"],
        "http_titles": ["arlo"],
        "http_body":   ["arlo", "arlo technologies", "netgear arlo"],
        "http_headers":["arlo"],
        "nmap_products":["arlo"],
        "onvif_scopes": ["arlo"],
        "default_ports": [554, 443, 80],
        "notes": ("Arlo wireless IP camera. CLOUD-ONLY by design — no "
                  "RTSP or ONVIF support."),
        "throttle_type":            "no_rtsp_support",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "n/a",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": "No RTSP server on device. Cloud-only by Arlo design.",
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": "No local stream protocols supported.",
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Verkada",
        "aliases": ["verkada"],
        "http_titles": ["verkada"],
        "http_body":   ["verkada", "verkada command"],
        "http_headers":["verkada"],
        "nmap_products":["verkada"],
        "onvif_scopes": ["verkada"],
        "default_ports": [443, 80],
        "notes": ("Verkada cloud-managed IP camera. Hardware is "
                  "Vivotek-OEM. CLOUD-ONLY API — no local RTSP exposed "
                  "to end users."),
        "throttle_type":            "no_rtsp_support",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "n/a (cloud-only)",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Cloud-only by design. Underlying Vivotek "
                           "hardware is locked behind Verkada Command "
                           "platform — no direct RTSP access."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": "Cloud-only. No local stream protocols exposed.",
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Luxonis / OAK",
        "aliases": ["luxonis", "oak-d", "oak camera", "depthAI"],
        "http_titles": ["luxonis", "oak"],
        "http_body":   ["luxonis", "oak-d", "depthai", "oak camera"],
        "http_headers":["luxonis"],
        "nmap_products":["luxonis", "mediamtx", "oak"],
        "onvif_scopes": ["luxonis"],
        "default_ports": [8765, 554, 80],
        "notes": "Luxonis OAK-D depth/AI camera",
    },
    {
        "name": "Tiandy",
        "aliases": ["tiandy"],
        "http_titles": ["tiandy"],
        "http_body":   ["tiandy", "tiandy technologies", "tiandy.com"],
        "http_headers":["tiandy"],
        "nmap_products":["tiandy"],
        "onvif_scopes": ["tiandy"],
        "default_ports": [554, 80, 8000],
        "notes": "Tiandy Technologies IP camera or NVR",
    },
    {
        "name": "IndigoVision",
        "aliases": ["indigovision"],
        "http_titles": ["indigovision"],
        "http_body":   ["indigovision", "indigovision.com"],
        "http_headers":["indigovision"],
        "nmap_products":["indigovision"],
        "onvif_scopes": ["indigovision"],
        "default_ports": [554, 80, 443],
        "notes": "IndigoVision IP camera or NVR (Scotland)",
    },
    {
        "name": "Q-See",
        "aliases": ["q-see", "qsee"],
        "http_titles": ["q-see", "qsee"],
        "http_body":   ["q-see", "qsee", "q-see technologies"],
        "http_headers":["q-see"],
        "nmap_products":["q-see", "qsee"],
        "onvif_scopes": ["qsee"],
        "default_ports": [554, 80, 34567],
        "notes": ("Q-See consumer DVR/NVR or IP camera. OEM rebrand — "
                  "uses Hikvision or Dahua hardware. Native DVR protocol "
                  "on port 34567 (Hisilicon)."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "LOW",
        "throttle_amount":          "Inherits upstream (Hikvision/Dahua)",
        "throttle_amount_confidence":"LOW",
        "throttle_notes": "OEM rebrand — behavior inherits from upstream.",
        "throttle_notes_confidence":"LOW",
        "request_behaviors": "Inherits Hikvision/Dahua RTSP/HTTP behavior.",
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "LaView",
        "aliases": ["laview"],
        "http_titles": ["laview"],
        "http_body":   ["laview", "laview technology", "laview.us"],
        "http_headers":["laview"],
        "nmap_products":["laview"],
        "onvif_scopes": ["laview"],
        "default_ports": [554, 80, 8080],
        "notes": ("LaView IP camera or NVR. OEM rebrand — uses Hikvision "
                  "or Dahua hardware."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "LOW",
        "throttle_amount":          "Inherits upstream (Hikvision/Dahua)",
        "throttle_amount_confidence":"LOW",
        "throttle_notes": "OEM rebrand — behavior inherits from upstream.",
        "throttle_notes_confidence":"LOW",
        "request_behaviors": "Inherits Hikvision/Dahua RTSP/HTTP behavior.",
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "Zosi",
        "aliases": ["zosi"],
        "http_titles": ["zosi"],
        "http_body":   ["zosi", "zosi security", "zositechnology"],
        "http_headers":["zosi"],
        "nmap_products":["zosi"],
        "onvif_scopes": ["zosi"],
        "default_ports": [554, 80, 34567],
        "notes": ("Zosi budget security camera or NVR/DVR. OEM rebrand — "
                  "uses Hikvision or Dahua hardware. Native DVR on "
                  "port 34567 (Hisilicon)."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "LOW",
        "throttle_amount":          "Inherits upstream (Hikvision/Dahua)",
        "throttle_amount_confidence":"LOW",
        "throttle_notes": "OEM rebrand — behavior inherits from upstream.",
        "throttle_notes_confidence":"LOW",
        "request_behaviors": "Inherits Hikvision/Dahua RTSP/HTTP behavior.",
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "Sricam / Srihome",
        "aliases": ["sricam", "srihome"],
        "http_titles": ["sricam", "srihome"],
        "http_body":   ["sricam", "srihome", "sricam.com"],
        "http_headers":["sricam", "srihome", "rtspserver_0.0.0"],
        "nmap_products":["sricam", "srihome"],
        "onvif_scopes": ["sricam", "srihome"],
        "default_ports": [554, 80, 8080],
        "notes": ("Sricam / Srihome budget IP camera. Uses Hipcam "
                  "RealServer firmware family (see Hipcam/Microseven "
                  "entry for shared throttle behavior). URL path "
                  "/onvif2 also seen on some models. Server header: "
                  "\"RtspServer_0.0.0.2\"."),
        "throttle_type":            "rate_limit_per_ip_tcp",
        "throttle_type_confidence": "MED",
        "throttle_amount":          "~5s cooldown (inherits Hipcam family)",
        "throttle_amount_confidence":"MED",
        "throttle_notes": ("Hipcam RealServer firmware family — see "
                           "Hipcam/Microseven entry for full throttle "
                           "details and probe strategy."),
        "throttle_notes_confidence":"MED",
        "request_behaviors": ("Hipcam family — Digest auth (realm="
                              "\"Hipcam RealServer\"). URLs /11, /12, "
                              "/onvif2."),
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "Vstarcam",
        "aliases": ["vstarcam"],
        "http_titles": ["vstarcam"],
        "http_body":   ["vstarcam", "vstarcam.com"],
        "http_headers":["vstarcam"],
        "nmap_products":["vstarcam"],
        "onvif_scopes": ["vstarcam"],
        "default_ports": [554, 80, 8080],
        "notes": ("Vstarcam budget WiFi IP camera. Uses Hipcam "
                  "RealServer firmware family on most models — see "
                  "Hipcam/Microseven entry for shared throttle "
                  "behavior."),
        "throttle_type":            "rate_limit_per_ip_tcp",
        "throttle_type_confidence": "MED",
        "throttle_amount":          "~5s cooldown (inherits Hipcam family)",
        "throttle_amount_confidence":"MED",
        "throttle_notes": ("Hipcam RealServer firmware family — see "
                           "Hipcam/Microseven entry for full throttle "
                           "details."),
        "throttle_notes_confidence":"MED",
        "request_behaviors": ("Hipcam family. URLs /udp/av0_0, "
                              "/tcp/av0_0 also seen on some models."),
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "Wansview",
        "aliases": ["wansview"],
        "http_titles": ["wansview"],
        "http_body":   ["wansview", "wansview.com"],
        "http_headers":["wansview"],
        "nmap_products":["wansview"],
        "onvif_scopes": ["wansview"],
        "default_ports": [554, 80, 8080],
        "notes": ("Wansview budget IP camera. W2/W3 models use Hipcam "
                  "RealServer firmware family — URL /live/ch0. "
                  "WARNING: newer firmware versions are CLOUD-ONLY with "
                  "NO RTSP or ONVIF support (Wansview switched to "
                  "cloud-only on most products). Block firmware updates "
                  "via DNS to keep RTSP working on W2/W3."),
        "throttle_type":            "rate_limit_per_ip_tcp",
        "throttle_type_confidence": "MED",
        "throttle_amount":          "W2/W3: ~5s cooldown (Hipcam family); newer: no RTSP",
        "throttle_amount_confidence":"MED",
        "throttle_notes": ("W2/W3 inherit Hipcam family throttle. "
                           "Post-2019 firmware on most other models is "
                           "cloud-only with no RTSP support at all."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("W2/W3 (older FW): Hipcam family, URL "
                              "/live/ch0. Newer FW: cloud-only, no "
                              "RTSP."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Tenvis",
        "aliases": ["tenvis"],
        "http_titles": ["tenvis"],
        "http_body":   ["tenvis", "tenvis technology"],
        "http_headers":["tenvis"],
        "nmap_products":["tenvis"],
        "onvif_scopes": ["tenvis"],
        "default_ports": [554, 80, 8080],
        "notes": ("Tenvis IP camera. Uses Hipcam RealServer firmware "
                  "family on many models — see Hipcam/Microseven entry "
                  "for shared throttle behavior."),
        "throttle_type":            "rate_limit_per_ip_tcp",
        "throttle_type_confidence": "MED",
        "throttle_amount":          "~5s cooldown (inherits Hipcam family)",
        "throttle_amount_confidence":"MED",
        "throttle_notes": ("Hipcam RealServer firmware family — see "
                           "Hipcam/Microseven entry for full details."),
        "throttle_notes_confidence":"MED",
        "request_behaviors": "Hipcam family — Digest auth, URLs /11, /12.",
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "Instar",
        "aliases": ["instar"],
        "http_titles": ["instar"],
        "http_body":   ["instar", "instar gmbh", "instar.de"],
        "http_headers":["instar"],
        "nmap_products":["instar"],
        "onvif_scopes": ["instar"],
        "default_ports": [554, 80, 8080, 443],
        "notes": "Instar IP camera (Germany — popular in Europe)",
    },
    {
        "name": "Luma Surveillance",
        "aliases": ["luma surveillance", "luma", "snapav"],
        "http_titles": ["luma surveillance", "luma"],
        "http_body":   ["luma surveillance", "snapav", "luma.com"],
        "http_headers":["luma"],
        "nmap_products":["luma surveillance", "luma"],
        "onvif_scopes": ["luma"],
        "default_ports": [554, 80, 443],
        "notes": "Luma Surveillance IP camera or NVR (SnapAV) — Hikvision OEM",
    },
    {
        "name": "Speco Technologies",
        "aliases": ["speco", "speco technologies"],
        "http_titles": ["speco"],
        "http_body":   ["speco technologies", "speco", "specotech.com"],
        "http_headers":["speco"],
        "nmap_products":["speco"],
        "onvif_scopes": ["speco"],
        "default_ports": [554, 80, 443],
        "notes": "Speco Technologies IP camera or NVR",
    },
    {
        "name": "Oncam",
        "aliases": ["oncam", "oncam grandeye"],
        "http_titles": ["oncam", "grandeye"],
        "http_body":   ["oncam", "grandeye", "oncam.com"],
        "http_headers":["oncam"],
        "nmap_products":["oncam", "grandeye"],
        "onvif_scopes": ["oncam"],
        "default_ports": [554, 80, 443],
        "notes": "Oncam 360-degree fisheye IP camera",
    },
    {
        "name": "Illustra (Johnson Controls)",
        "aliases": ["illustra", "johnson controls", "tyco security"],
        "http_titles": ["illustra", "johnson controls"],
        "http_body":   ["illustra", "tyco security", "johnson controls"],
        "http_headers":["illustra", "tyco"],
        "nmap_products":["illustra", "johnson controls"],
        "onvif_scopes": ["illustra", "johnsoncontrols"],
        "default_ports": [554, 80, 443],
        "notes": "Illustra IP camera (Johnson Controls / Tyco Security)",
    },
    {
        "name": "Milesight",
        "aliases": ["milesight", "milesight iot"],
        "http_titles": ["milesight"],
        "http_body":   ["milesight", "milesight-iot.com"],
        "http_headers":["milesight"],
        "nmap_products":["milesight"],
        "onvif_scopes": ["milesight"],
        "default_ports": [554, 80, 8080, 443],
        "notes": "Milesight IP camera or NVR",
    },
    {
        "name": "Sunell",
        "aliases": ["sunell"],
        "http_titles": ["sunell"],
        "http_body":   ["sunell", "sunell technology", "sunell.com"],
        "http_headers":["sunell"],
        "nmap_products":["sunell"],
        "onvif_scopes": ["sunell"],
        "default_ports": [554, 80, 8000],
        "notes": "Sunell IP camera or NVR",
    },
    {
        "name": "TVT",
        "aliases": ["tvt", "tvt digital technology"],
        "http_titles": ["tvt", "tvt digital"],
        "http_body":   ["tvt digital", "tvt technology", "tvt-ip.com"],
        "http_headers":["tvt"],
        "nmap_products":["tvt"],
        "onvif_scopes": ["tvt"],
        "default_ports": [554, 80, 8000, 34567],
        "notes": "TVT Digital Technology IP camera or NVR (common OEM base)",
    },
    {
        "name": "Kedacom",
        "aliases": ["kedacom"],
        "http_titles": ["kedacom"],
        "http_body":   ["kedacom", "kedacom.com"],
        "http_headers":["kedacom"],
        "nmap_products":["kedacom"],
        "onvif_scopes": ["kedacom"],
        "default_ports": [554, 80, 443],
        "notes": "Kedacom IP camera or NVR",
    },
    {
        "name": "VideoIQ (Avigilon)",
        "aliases": ["videoiq"],
        "http_titles": ["videoiq"],
        "http_body":   ["videoiq", "videoiq.com"],
        "http_headers":["videoiq"],
        "nmap_products":["videoiq"],
        "onvif_scopes": ["videoiq"],
        "default_ports": [554, 80, 443],
        "notes": "VideoIQ analytics camera (absorbed by Avigilon/Motorola)",
    },
    {
        "name": "Samsung (standalone)",
        "aliases": ["samsung camera", "sno-", "snd-", "snh-"],
        "http_titles": ["samsung", "sno-", "snd-", "snh-"],
        "http_body":   ["samsung camera", "samsung techwin", "sno-", "snd-", "snh-"],
        "http_headers":["samsung"],
        "nmap_products":["samsung network camera"],
        "onvif_scopes": ["samsung"],
        "default_ports": [554, 80, 443],
        "notes": "Samsung standalone IP camera (pre-Hanwha rebranding)",
    },
    {
        "name": "Hipcam/Microseven",
        "aliases": ["microseven", "hipcam", "hiipcam", "m7d", "m7b", "m7t",
                    "srihome", "sricam", "vstarcam", "wansview"],
        "http_titles": ["microseven", "hipcam", "ipcam", "rtspserver"],
        "http_body":   ["microseven", "hipcam realserver", "rtspserver_0.0.0"],
        "http_headers":["hipcam realserver", "hipcam realserver/v1.0",
                        "hiipcam/v100r003", "vodserver/1.0.0",
                        "rtspserver_0.0.0.2", "rtspserver_0.0.0"],
        "nmap_products":["hipcam realserver", "hiipcam", "rtspserver_0.0.0"],
        "onvif_scopes": ["microseven", "hipcam"],
        "default_ports": [554, 80],
        "notes": "Hipcam RealServer firmware family — Microseven, Sricam, "
                 "Vstarcam, Wansview (W2/W3), Tenvis, many cheap Chinese "
                 "baby monitors / IPCAM rebrands. URL paths /11 (main) /12 "
                 "(sub) /13 /h264major /h264minor; HTTP snap at "
                 "/tmpfs/snap.jpg. Per-IP TCP rate-limit on RTSP port — "
                 "single-socket multi-method probing required.",
        # ─── rc2 throttle fields ───────────────────────────────────────
        "throttle_type":            "rate_limit_per_ip_tcp",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "~5s cooldown between TCP opens from same source IP",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("First TCP connection from a source IP always succeeds; "
                           "subsequent connections within ~5s are RST'd at the TCP layer. "
                           "Single-socket multi-method (OPTIONS+DESCRIBE+SETUP+TEARDOWN "
                           "on one socket) bypasses the throttle entirely. "
                           "CVE-2023-50685: malformed client_port in SETUP crashes the "
                           "RTSP service for ~45s. Tested on Microseven (4 browser "
                           "automation runs)."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("Digest auth required (realm=\"Hipcam RealServer\"); "
                              "rejects Basic auth. ONVIF returns no profiles for some "
                              "models — direct-RTSP probing required. Server header: "
                              "\"Hipcam RealServer/V1.0\" or "
                              "\"HiIpcam/V100R003 VodServer/1.0.0\"."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Honeywell",
        "aliases": ["honeywell", "hbt", "performance series", "hc30"],
        "http_titles": ["honeywell"],
        "http_body":   ["honeywell", "hbt"],
        "http_headers":["honeywell"],
        "nmap_products":["honeywell"],
        "onvif_scopes": ["honeywell"],
        "default_ports": [554, 80, 443],
        "notes": ("Honeywell IP camera — Performance Series HC30 line. "
                  "Mixed OEM (Hikvision or Dahua firmware depending on "
                  "year/line per SCW). Try Hikvision /Streaming/Channels/101 "
                  "and Dahua /cam/realmonitor first; fall back to ONVIF "
                  "discovery."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "LOW",
        "throttle_amount":          "Inherits upstream OEM",
        "throttle_amount_confidence":"LOW",
        "throttle_notes": ("OEM rebrand — behavior inherits from upstream "
                           "Hikvision or Dahua firmware (varies by year/line)."),
        "throttle_notes_confidence":"LOW",
        "request_behaviors": ("OEM-rebrand. Try Hikvision-style and "
                              "Dahua-style URL families before falling back "
                              "to ONVIF."),
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "Arecont Vision",
        "aliases": ["arecont", "arecont vision", "contera", "megavideo"],
        "http_titles": ["arecont", "contera"],
        "http_body":   ["arecont vision", "megavideo"],
        "http_headers":["arecont"],
        "nmap_products":["arecont"],
        "onvif_scopes": ["arecont"],
        "default_ports": [554, 80, 443],
        "notes": ("Arecont Vision IP camera (multi-megapixel, "
                  "multi-imager surround). Single-sensor URL pattern: "
                  "/h264.sdp?res=[half/full]&ssn=N&doublescan=0&fps=N. "
                  "Multi-sensor: /h264.sdp<sensor#> where sensor# is 1..4. "
                  "ssn must be unique per stream."),
        "throttle_type":            "concurrent_stream_cap",
        "throttle_type_confidence": "LOW",
        "throttle_amount":          "Camera-dependent — not all units handle multiple full-res sessions",
        "throttle_amount_confidence":"LOW",
        "throttle_notes": ("Per OpenEye/AvertX docs: not all Arecont cameras "
                           "can handle multiple full-resolution sessions. "
                           "Reduce to half-res or single session per sensor "
                           "on overload."),
        "throttle_notes_confidence":"MED",
        "request_behaviors": ("HTTP Basic auth. Multi-imager pattern uses "
                              "/h264.sdp<N> per sensor. Streaming recipe is "
                              "deterministic — Layer 2 grinding wasteful."),
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "IQinVision",
        "aliases": ["iqinvision", "iqeye"],
        "http_titles": ["iqinvision", "iqeye"],
        "http_body":   ["iqinvision", "iqeye"],
        "http_headers":["iqinvision"],
        "nmap_products":["iqinvision"],
        "onvif_scopes": ["iqinvision"],
        "default_ports": [554, 80],
        "notes": ("IQinVision IQeye IP camera (acquired by Vicon — older "
                  "line). ONVIF and PSIA compliant. Try generic /track1, "
                  "/track2 or ONVIF discovery."),
        "request_behaviors": ("Older PSIA-compliant line. ONVIF Profile S "
                              "on later firmware."),
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "OpenEye",
        "aliases": ["openeye", "ows", "apex", "owe"],
        "http_titles": ["openeye", "ows"],
        "http_body":   ["openeye", "openeye.net", "ows server"],
        "http_headers":["openeye"],
        "nmap_products":["openeye"],
        "onvif_scopes": ["openeye"],
        "default_ports": [554, 80, 443],
        "notes": ("OpenEye OWS Apex server / IP camera. Generic ONVIF + "
                  "RTSP /track1, /track2 paths."),
        "request_behaviors": ("OWS Apex server can ingest most third-party "
                              "cameras. Direct OpenEye cameras use ONVIF + "
                              "/track[N] paths."),
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "Verint",
        "aliases": ["verint", "nextiva", "s1700", "s1708"],
        "http_titles": ["verint", "nextiva"],
        "http_body":   ["verint", "nextiva", "video solutions"],
        "http_headers":["verint"],
        "nmap_products":["verint"],
        "onvif_scopes": ["verint"],
        "default_ports": [554, 80, 2543],
        "notes": ("Verint Nextiva enterprise camera/encoder. Older Nextiva "
                  "encoders (S1700/S1708) are RTP/UDP only on port 2543 "
                  "(no RTSP, telnet config). Newer Verint models are "
                  "ONVIF-compliant."),
        "request_behaviors": ("Older models lack RTSP entirely — RTP/UDP on "
                              "port 2543. Newer models ONVIF Profile S."),
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "Ubiquiti UniFi Protect",
        "aliases": ["ubiquiti", "unifi", "unifi protect", "ubnt", "g3", "g4", "g5"],
        "http_titles": ["unifi", "unifi protect"],
        "http_body":   ["ubiquiti", "unifi protect", "ui.com"],
        "http_headers":["ubnt", "ubiquiti"],
        "nmap_products":["ubiquiti", "unifi"],
        "onvif_scopes": [],
        "default_ports": [7447, 7441, 443, 80],
        "notes": ("Ubiquiti UniFi Protect cameras (G3/G4/G5/AI Port). RTSP "
                  "must be enabled per-camera in UniFi Protect controller "
                  "Settings -> Advanced. Camera ID is alphanumeric slug "
                  "from UniFi UI. Non-standard ports: 7447 (RTSP) / "
                  "7441 (RTSPS). Cameras do NOT speak ONVIF directly — "
                  "all access goes through the Protect controller."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "MED",
        "throttle_amount":          "Configurable in Protect controller",
        "throttle_amount_confidence":"MED",
        "throttle_notes": ("Bandwidth/session caps live in the Protect "
                           "controller, not the camera."),
        "throttle_notes_confidence":"MED",
        "request_behaviors": ("Streams via UniFi Protect controller — not "
                              "direct from camera. ONVIF discovery will "
                              "fail; use vendor's API or copy the camera_id "
                              "from Protect UI. Skip ONVIF + skip Layer 2."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Hiseeu",
        "aliases": ["hiseeu", "eseecloud", "esee"],
        "http_titles": ["hiseeu"],
        "http_body":   ["hiseeu", "eseecloud"],
        "http_headers":["hiseeu"],
        "nmap_products":["hiseeu"],
        "onvif_scopes": ["hiseeu"],
        "default_ports": [554, 80, 8554, 34567],
        "notes": ("Hiseeu wireless IP camera or NVR/gateway system. "
                  "Gateway-based: gateway exposes RTSP at port 80 with "
                  "/ch[N]_[feed].264 paths. Standalone cameras use "
                  "/onvif1 or generic /Streaming/Channels paths. "
                  "Inconsistent across models."),
        "request_behaviors": ("Wireless gateway-based system. URL pattern "
                              "varies — try /onvif1 and ONVIF discovery."),
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "Grandstream",
        "aliases": ["grandstream", "gxv", "gsc"],
        "http_titles": ["grandstream"],
        "http_body":   ["grandstream networks", "grandstream.com"],
        "http_headers":["grandstream"],
        "nmap_products":["grandstream"],
        "onvif_scopes": ["grandstream"],
        "default_ports": [554, 80, 443, 8000],
        "notes": ("Grandstream GXV/GSC IP camera. Standard ONVIF Profile "
                  "S/T. Multiple stream profiles configurable."),
        "request_behaviors": "Standard ONVIF. Multiple configurable stream profiles.",
        "request_behaviors_confidence":"LOW",
    },
    {
        "name": "TRENDnet",
        "aliases": ["trendnet", "tv-ip"],
        "http_titles": ["trendnet", "tv-ip"],
        "http_body":   ["trendnet", "trendnet.com"],
        "http_headers":["trendnet"],
        "nmap_products":["trendnet"],
        "onvif_scopes": ["trendnet"],
        "default_ports": [554, 80],
        "notes": ("TRENDnet TV-IP series IP camera. URL pattern varies by "
                  "model generation. Newer: /Streaming/Channels/101 "
                  "(Hikvision-style). Older: /h264 or /play1.sdp. "
                  "Some use /live/0/SUB."),
        "request_behaviors": ("URL pattern varies by model generation — "
                              "try Hik-style, then /play1.sdp, then "
                              "/h264, then ONVIF discovery."),
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "D-Link",
        "aliases": ["d-link", "dlink", "dcs-"],
        "http_titles": ["d-link", "dlink", "dcs-"],
        "http_body":   ["d-link", "dlink.com"],
        "http_headers":["d-link"],
        "nmap_products":["d-link", "dlink"],
        "onvif_scopes": ["dlink"],
        "default_ports": [554, 80],
        "notes": ("D-Link DCS-series IP camera. URL pattern varies by "
                  "model. Older DCS-9xx: /play1.sdp. DCS-8526LH: "
                  "/live/profile.0 (main), /live/profile.1 (sub). "
                  "Some use /onvif/profile.0 or /live/ch00_0."),
        "request_behaviors": ("URL pattern varies by model. Try "
                              "/play1.sdp, /live/profile.0, "
                              "/onvif/profile.0, then ONVIF discovery."),
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "SCW (Security Camera Warehouse)",
        "aliases": ["scw", "security camera warehouse", "getscw"],
        "http_titles": ["scw", "security camera warehouse"],
        "http_body":   ["getscw.com", "security camera warehouse"],
        "http_headers":["scw"],
        "nmap_products":["scw"],
        "onvif_scopes": ["scw"],
        "default_ports": [554, 80],
        "notes": ("SCW Hikvision-compatible firmware on most lines. Use "
                  "/Streaming/Channels/101 pattern. SCW-branded NVRs "
                  "follow Hikvision NVR conventions."),
        "throttle_type":            "concurrent_user_cap",
        "throttle_type_confidence": "LOW",
        "throttle_amount":          "Inherits Hikvision firmware",
        "throttle_amount_confidence":"LOW",
        "throttle_notes": "Hikvision OEM — inherits parent throttle behavior.",
        "throttle_notes_confidence":"MED",
        "request_behaviors": ("Hikvision OEM. Use /Streaming/Channels/N0X "
                              "pattern. ISAPI snapshot URL works."),
        "request_behaviors_confidence":"MED",
    },
    {
        "name": "Blink",
        "aliases": ["blink", "amazon blink", "blink mini", "blink outdoor"],
        "http_titles": ["blink"],
        "http_body":   ["blink", "amazon blink"],
        "http_headers":[],
        "nmap_products":["blink"],
        "onvif_scopes": [],
        "default_ports": [],
        "notes": ("Blink (Amazon) cloud-only camera. No local RTSP, no "
                  "local HTTP UI for streaming. Footage routes through "
                  "Amazon servers. Local discovery should mark these "
                  "as 'Not a Camera' since no controllable stream exists."),
        "throttle_type":            "no_rtsp_support",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "N/A — cloud only",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Cloud-only camera — no local RTSP path exists. "
                           "Sync Module 2 offers local storage but no RTSP."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("Cloud-only — no local stream access. Skip "
                              "ONVIF, skip Layer 2, skip all probes."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Lorex / Dahua DVR-NVR Family",
        "aliases": ["lorex dvr", "lorex nvr", "dahua dvr", "dahua nvr",
                    "amcrest dvr", "amcrest nvr",
                    "d861", "d862", "d863", "d871", "d841", "d881",
                    "n841", "n844", "n846", "n861", "n864", "n881", "n882",
                    "n910", "n920"],
        "http_titles": ["web service"],
        "http_body":   [],
        "http_headers":[],
        "nmap_products":[],
        "onvif_scopes": [],
        "default_ports": [554, 80, 35000, 37777, 443, 8000],
        "rtsp_realm_regex": r"^Login to [0-9a-f]{32}$",
        "rtsp_realm_regex_confidence": "HIGH",
        # 2.4.0-rc2.0: streaming_recipe added (data only; consumed in rc3.x).
        # Channel iteration for Dahua-format DVR/NVRs. populated_channel_test
        # uses SDP video-track presence to detect connected channels.
        "streaming_recipe": {
            "type": "channel_iterate",
            "path_template": "/cam/realmonitor?channel={ch}&subtype={st}",
            "channels": list(range(1, 33)),
            "channel_base": 1,
            "subtypes": [0, 1],
            "subtype_main": 0,
            "subtype_sub": 1,
            "fallback_paths": ["/h264/ch{ch}/main/av_stream", "/live/ch{ch}/main"],
            "populated_channel_test": "sdp_has_video_track",
            # 2.5.0-rc1.2: per-channel HTTP snapshot URL template. The
            # Dahua snapshot.cgi endpoint accepts ?channel=N to return
            # that specific physical channel's still image; without the
            # query param the DVR returns either the default channel
            # or a generic placeholder (~3.5 KB). Used by the channel
            # enumeration helper to build per-card snap URLs so each
            # channel's card thumbnail polls its own snapshot.
            "snap_url_template": "http://{ip}/cgi-bin/snapshot.cgi?channel={ch}",
        },
        "streaming_recipe_confidence": "HIGH",
        # 2.4.0-rc2.2 — skip_layer2: True. Multi-channel DVR/NVRs do
        # not benefit from Layer 2 fallback (multi-socket fanout) on a
        # single-IP target — the right path requires channel iteration
        # via streaming_recipe (consumed in rc3.x), not more retries on
        # a single-channel guess. Without this flag rc2.1's Layer 2
        # consumer falls through and grinds 50s on the Lorex DVR
        # before bail-after-10 fires; with the flag set, Layer 2 is
        # skipped immediately with a one-line log entry. Companion to
        # the rc2.1 code-side short-circuit at find_rtsp_path.
        "skip_layer2": True,
        "skip_layer2_confidence": "HIGH",
        "notes": ("Multi-channel DVR/NVR family — Lorex (post-Dahua-"
                  "acquisition), Dahua direct, Amcrest (rebrand). "
                  "Series: D861/862/863/871/841/881, N841/844/846/861/"
                  "864/881/882/910/920. Web admin shows page title 'WEB "
                  "SERVICE' before init. Default Dahua/Amcrest TCP/UDP "
                  "ports 37777/37778 are user-configurable (one observed "
                  "live unit had them on 35000/35001). RTSP Server: "
                  "header omitted. ONVIF disabled by default. URL pattern "
                  "/cam/realmonitor?channel={ch}&subtype={st} requires "
                  "channel iteration to find populated channels."),
        "throttle_type":            "auth_attempt_lockout",
        "throttle_type_confidence": "HIGH",
        "throttle_amount":          "10 failed auth attempts then ~30 min lockout (or until power cycle)",
        "throttle_amount_confidence":"HIGH",
        "throttle_notes": ("Lockout only counts FAILED auth (bad Digest "
                           "response). Successful auth followed by 200/404 "
                           "on subsequent paths does NOT increment the "
                           "counter. Channel iteration with valid creds is "
                           "unthrottled."),
        "throttle_notes_confidence":"HIGH",
        "request_behaviors": ("Digest auth only (no Basic). Realm pattern "
                              "'Login to <32-hex>'. Server header omitted "
                              "from RTSP responses. ONVIF disabled by "
                              "default — lives under Network -> Connection "
                              "or Network -> Advanced depending on firmware "
                              "minor build."),
        "request_behaviors_confidence":"HIGH",
    },
    {
        "name": "Generic IP Camera",
        "aliases": ["webcam", "ipcam", "network camera"],
        "http_titles": ["ip camera", "network camera", "webcam", "ipcam",
                        "video server", "live view", "camera login"],
        "http_body":   ["ip camera", "network camera", "video surveillance",
                        "live view", "ptz control"],
        "http_headers":[],
        "nmap_products":["ip camera", "network camera", "video server", "webcam"],
        "onvif_scopes": [],
        "default_ports": [554, 80, 8080],
        "notes": "Generic IP camera (manufacturer unidentified)",
    },
    # ============================================================================
    # 2.4.0-rc2.0: NVR/DVR family entries with streaming_recipe (channel iteration)
    # ============================================================================
    # These are SEPARATE entries from the IP camera entries above because (per
    # design) channel iteration only applies to multi-channel boxes. Single
    # IP cameras of the same brand keep their static-path entries.
    # streaming_recipe field consumed in 2.4.0-rc3.x (Lorex/Dahua DVR family
    # support build); populated here in rc2.0 as the data foundation.
    {
        "name": "Hikvision NVR",
        "aliases": ["hikvision nvr", "hikvision dvr", "hilook nvr", "hilook dvr",
                    "ds-7604", "ds-7608", "ds-7616", "ds-7708", "ds-7716",
                    "ds-7732", "ds-9632", "ds-77 hghi", "ds-77 huhi",
                    "ds-77 hqhi", "turbo hd"],
        "http_titles": ["hikvision", "hikvision-webs"],
        "http_body":   [],
        "http_headers":["app-webs/"],
        "nmap_products":[],
        # 2.4.0-rc2.2 — onvif_scopes emptied. Previously contained
        # "onvif://www.onvif.org/Profile/Streaming" which is the
        # standard ONVIF Profile S spec identifier returned by EVERY
        # Profile-S-compliant ONVIF device on the planet (IP cameras,
        # NVRs, encoders, all of them). Per ONVIF Core Spec it carries
        # zero brand-specific signal — it identifies the protocol, not
        # the manufacturer. Including it here was scoring +1 to
        # Hikvision NVR for any ONVIF haystack and contributed to
        # CrystalHeeler's Hikvision DS-2DE4A425IW-DE single PTZ camera
        # being misclassified as a Hikvision NVR.
        "onvif_scopes": [],
        "default_ports": [554, 80, 8000, 443],
        "notes": ("Hikvision multi-channel NVR/DVR. DS-7xxx series NVRs and "
                  "DS-77xxx HUHI/HQHI/HGHI Turbo HD DVRs. HiLook is a "
                  "Hikvision sub-brand using identical RTSP format. Some "
                  "firmwares require disabling stream encryption "
                  "(Configuration > Network > Advanced Settings > Stream "
                  "Encryption > OFF) — must be done from direct-connected "
                  "monitor."),
        "streaming_recipe": {
            "type": "channel_iterate",
            "path_template": "/Streaming/Channels/{ch}{st:02d}",
            "channels": list(range(1, 33)),
            "channel_base": 1,
            "subtypes": [1, 2],
            "subtype_main": 1,
            "subtype_sub": 2,
            "populated_channel_test": "sdp_has_video_track",
        },
        "streaming_recipe_confidence": "HIGH",
    },
    {
        "name": "Hanwha NVR / Wisenet NVR",
        "aliases": ["hanwha nvr", "wisenet nvr", "samsung nvr",
                    "qrn", "prn", "xrn", "hrx", "hrd", "srn"],
        "http_titles": ["wisenet", "samsung"],
        "http_body":   [],
        "http_headers":[],
        "nmap_products":[],
        "onvif_scopes": [],
        "default_ports": [558, 554, 80, 8080, 443],
        "notes": ("Hanwha (formerly Samsung Techwin) Wisenet NVRs. IMPORTANT: "
                  "Channels are 0-based (Channel 1 in UI = 0 in RTSP URL). "
                  "RTSP port is the LAST device port in the configured range, "
                  "default 558 (NOT 554). Profile selection via /stw-cgi/ "
                  "media.cgi CGI command. HRX series with newer firmware uses "
                  "NVR-style URLs."),
        "streaming_recipe": {
            "type": "channel_iterate",
            "path_template": "/LiveChannel/{ch}/media.smp/profile={st}",
            "channels": list(range(0, 32)),
            "channel_base": 0,
            "subtypes": [1, 2],
            "subtype_main": 1,
            "subtype_sub": 2,
            "default_port": 558,
            "populated_channel_test": "sdp_has_video_track",
        },
        "streaming_recipe_confidence": "HIGH",
    },
    {
        "name": "Uniview NVR (UNV)",
        "aliases": ["uniview nvr", "unv nvr", "nvr301", "nvr3", "nvr5",
                    "nvr8", "ezview nvr"],
        "http_titles": ["uniview", "unv"],
        "http_body":   [],
        "http_headers":[],
        "nmap_products":[],
        "onvif_scopes": [],
        "default_ports": [554, 80, 9090],
        "notes": ("Uniview (UNV) NVR301/3/5/8 series. Channel iteration: c1, "
                  "c2, c3, ... (1-based). Stream s0=main, s1=sub. Some firmwares "
                  "have s0/s1 swapped — fall back accordingly."),
        "streaming_recipe": {
            "type": "channel_iterate",
            "path_template": "/unicast/c{ch}/s{st}/live",
            "channels": list(range(1, 33)),
            "channel_base": 1,
            "subtypes": [0, 1],
            "subtype_main": 0,
            "subtype_sub": 1,
            "populated_channel_test": "sdp_has_video_track",
        },
        "streaming_recipe_confidence": "HIGH",
    },
    {
        "name": "Reolink NVR / Home Hub",
        "aliases": ["reolink nvr", "rln8", "rln16", "rln36", "home hub",
                    "reolink home hub"],
        "http_titles": ["reolink"],
        "http_body":   [],
        "http_headers":[],
        "nmap_products":[],
        "onvif_scopes": [],
        "default_ports": [554, 80, 8080, 9000],
        "notes": ("Reolink RLN8-410, RLN16-410, RLN36 NVRs and Home Hub. "
                  "Channel ch is zero-padded 2-digit (01, 02, ...). 1-based "
                  "in RTSP URLs but 0-based in CGI/snapshot URLs (snap_channel_"
                  "offset=-1). Use api.cgi?cmd=GetChannelstatus to query "
                  "populated channels. Same RTSP format as Reolink IP cameras "
                  "(single camera = ch=01 only)."),
        "streaming_recipe": {
            "type": "channel_iterate",
            "path_template": "/Preview_{ch:02d}_{st}",
            "channels": list(range(1, 17)),
            "channel_base": 1,
            "subtypes": ["main", "sub"],
            "subtype_main": "main",
            "subtype_sub": "sub",
            "snap_channel_offset": -1,
            "populated_channel_test": "sdp_has_video_track",
        },
        "streaming_recipe_confidence": "HIGH",
    },
    {
        "name": "Vivotek NVR (Linux-based)",
        "aliases": ["vivotek nvr", "nd8321", "nd8322", "nd9322", "nd9442"],
        "http_titles": ["vivotek"],
        "http_body":   [],
        "http_headers":[],
        "nmap_products":[],
        "onvif_scopes": [],
        "default_ports": [554, 80, 8080],
        "notes": ("Vivotek Linux-based NVR (ND8x21, ND8322P, ND9x42P, ND9x44P "
                  "series). Camera ID format C_<N> where N is channel number. "
                  "Different format from Vivotek IP cameras themselves "
                  "(which use /live.sdp or /media2/stream.sdp?profile=)."),
        "streaming_recipe": {
            "type": "channel_iterate",
            "path_template": "/Media/Live/Normal?camera=C_{ch}&streamindex={st}",
            "channels": list(range(1, 33)),
            "channel_base": 1,
            "subtypes": [1, 2],
            "subtype_main": 1,
            "subtype_sub": 2,
            "populated_channel_test": "sdp_has_video_track",
        },
        "streaming_recipe_confidence": "HIGH",
    },
    {
        "name": "Amcrest NVR",
        "aliases": ["amcrest nvr", "amcrest dvr", "nv4108", "nv4216", "nv5216",
                    "nv4108-hs"],
        "http_titles": ["amcrest"],
        "http_body":   [],
        "http_headers":[],
        "nmap_products":[],
        "onvif_scopes": [],
        "default_ports": [554, 37777, 80],
        "notes": ("Amcrest NVR (NV4108E, NV4216E, NV5216E series). Dahua-OEM. "
                  "Same RTSP recipe as Dahua. Some older Amcrest NVRs also "
                  "support Reolink-style h264Preview format as a fallback."),
        "streaming_recipe": {
            "type": "channel_iterate",
            "path_template": "/cam/realmonitor?channel={ch}&subtype={st}",
            "channels": list(range(1, 33)),
            "channel_base": 1,
            "subtypes": [0, 1],
            "subtype_main": 0,
            "subtype_sub": 1,
            "fallback_paths": ["/h264Preview_{ch:02d}_main"],
            "populated_channel_test": "sdp_has_video_track",
        },
        "streaming_recipe_confidence": "HIGH",
    },
    {
        "name": "Swann NVR / DVR",
        "aliases": ["swann nvr", "swann dvr", "nvr-7090", "nvr-8580",
                    "nvr8000", "dvr-1590", "dvr-4575", "dvr-4980", "swnvk"],
        "http_titles": ["swann"],
        "http_body":   [],
        "http_headers":[],
        "nmap_products":[],
        "onvif_scopes": [],
        "default_ports": [554, 80, 1085],
        "notes": ("Swann NVR/DVR mostly Hikvision-OEM (newer NVR-7090, "
                  "NVR-8580). Older models may be Raysharp-OEM with "
                  "/ch{NN}/{stream} format. Try Hikvision format first, "
                  "fall back to Raysharp."),
        "streaming_recipe": {
            "type": "channel_iterate",
            "path_template": "/Streaming/Channels/{ch}{st:02d}",
            "channels": list(range(1, 17)),
            "channel_base": 1,
            "subtypes": [1, 2],
            "subtype_main": 1,
            "subtype_sub": 2,
            "fallback_paths": ["/ch{ch:02d}/0", "/ch{ch:02d}/1"],
            "populated_channel_test": "sdp_has_video_track",
        },
        "streaming_recipe_confidence": "MED",
    },
    {
        "name": "ANNKE NVR / DVR",
        "aliases": ["annke nvr", "annke dvr", "h800", "h500", "n48pbb",
                    "n46pbb", "dn81r", "dn82r"],
        "http_titles": ["annke"],
        "http_body":   [],
        "http_headers":[],
        "nmap_products":[],
        "onvif_scopes": [],
        "default_ports": [554, 80, 8000],
        "notes": ("ANNKE NVR/DVR (H800, H500, N48PBB, N46PBB, DN81R, DN82R "
                  "series). Hikvision-OEM. Same recipe as Hikvision NVR. "
                  "Older ANNKE may use Dahua-OEM (cam/realmonitor) — fall "
                  "back if Hikvision format fails entirely."),
        "streaming_recipe": {
            "type": "channel_iterate",
            "path_template": "/Streaming/Channels/{ch}{st:02d}",
            "channels": list(range(1, 17)),
            "channel_base": 1,
            "subtypes": [1, 2],
            "subtype_main": 1,
            "subtype_sub": 2,
            "fallback_paths": ["/H264/ch{ch}/main/av_stream",
                               "/cam/realmonitor?channel={ch}&subtype=0"],
            "populated_channel_test": "sdp_has_video_track",
        },
        "streaming_recipe_confidence": "HIGH",
    },
]

# Build fast lookup structures from the DB
_DB_MANUFACTURERS: set[str] = set()   # all lowercase match strings for is_camera_positive
# 2.4.0-rc2.2 — multi-entry support per keyword. Previously this was
# `dict[str, dict]` which silently dropped all-but-the-last entry that
# claimed a given keyword. List-order processing meant later entries
# (e.g., Hikvision NVR at line 2046) clobbered earlier ones (Hikvision
# single camera at line 713) for shared keywords like "hikvision". A
# single Hikvision PTZ camera then lost every haystack hit on the bare
# word "hikvision" to the NVR entry, contributing to misclassification
# of CrystalHeeler's Hikvision DS-2DE4A425IW-DE PTZ as a Hikvision NVR. Storing all
# matching entries per keyword lets identify_manufacturer credit each
# legitimately-claiming entry, and the entry with the most distinct
# keyword hits across the haystack wins.
_DB_ENTRIES_BY_KEY: dict[str, list[dict]] = {}

for _entry in CAMERA_DB:
    for _field in ("http_titles", "http_body", "http_headers",
                   "nmap_products", "onvif_scopes", "aliases"):
        for _kw in _entry.get(_field, []):
            _k = _kw.lower()
            _DB_MANUFACTURERS.add(_k)
            # Each entry appears at most once per keyword bucket — a
            # keyword that occurs in MULTIPLE fields of the same entry
            # (e.g., "hikvision" in aliases, http_titles, http_body,
            # http_headers, nmap_products, AND onvif_scopes of the
            # Hikvision entry) counts as ONE keyword match for that
            # entry, not six. Otherwise scoring would be massively
            # skewed in favor of entries that repeat the same keyword
            # across many fields.
            _bucket = _DB_ENTRIES_BY_KEY.setdefault(_k, [])
            if _entry not in _bucket:
                _bucket.append(_entry)


import re as _re_mod

def _kw_matches(kw: str, text_l: str) -> bool:
    """
    Match a keyword against text.
    Short keywords (< 6 chars) require word-boundary match to prevent
    false positives from substring matches (e.g. 'acti' matching 'interactive').
    Long keywords use plain substring matching.
    """
    if len(kw) < 6:
        # Word boundary: kw must be preceded and followed by non-alphanumeric
        pattern = r'(?<![a-z0-9])' + _re_mod.escape(kw) + r'(?![a-z0-9])'
        return bool(_re_mod.search(pattern, text_l))
    return kw in text_l


def identify_manufacturer(text: str) -> dict | None:
    """
    Given a blob of text (HTTP body, nmap banner, etc.), return the best-matching
    CAMERA_DB entry, or None if no match found.

    2.4.0-rc2.2 — scoring rewritten to credit ALL entries that claim a
    matched keyword (previously only the last-written entry got credit
    due to dict overwrite). Tie-breaker: when the top score is shared
    by multiple entries, prefer non-NVR/DVR entries — multi-channel
    NVR entries are the SUPERSET case (need extra signals like
    series-specific keywords to win), and a single camera with only
    the bare brand name should default to the single-camera entry,
    not the NVR.

    Short keywords (< 6 chars) require word-boundary matching to avoid false
    positives (e.g. 'acti' matching 'interactive' on HP printer pages).
    """
    text_l = text.lower()
    # name -> count of distinct keywords that matched
    scores: dict[str, int] = {}
    # name -> the entry object (cached to avoid re-iterating CAMERA_DB)
    name_to_entry: dict[str, dict] = {}
    for kw, entries in _DB_ENTRIES_BY_KEY.items():
        if not _kw_matches(kw, text_l):
            continue
        for entry in entries:
            name = entry["name"]
            scores[name] = scores.get(name, 0) + 1
            name_to_entry[name] = entry
    if not scores:
        return None

    top_score = max(scores.values())
    tied = [n for n, s in scores.items() if s == top_score]
    if len(tied) == 1:
        return name_to_entry[tied[0]]

    # Tie-breaker: prefer entries that are NOT marked as multi-channel
    # NVR/DVR families. Without this, a haystack containing only the
    # bare brand name (e.g. just "hikvision" with no series identifier)
    # would tie 1-1 between Hikvision and Hikvision NVR, and CAMERA_DB
    # ordering alone would decide. NVR entries should win only when
    # they accumulate MORE distinct hits via series-specific keywords
    # (DS-77xxx, Turbo HD, app-webs/, etc.) — that's the right signal
    # for "this is actually an NVR, not just a camera of this brand."
    def _is_nvr_family(entry: dict) -> bool:
        n = entry.get("name", "").lower()
        return ("nvr" in n or "dvr" in n
                or "family" in n
                or entry.get("streaming_recipe", {}).get("type") == "channel_iterate")
    non_nvr_tied = [n for n in tied if not _is_nvr_family(name_to_entry[n])]
    if non_nvr_tied:
        # Among non-NVR tied entries, prefer earliest in CAMERA_DB list
        # order (which is the canonical/most-common entry for that brand).
        for entry in CAMERA_DB:
            if entry["name"] in non_nvr_tied:
                return entry
    # All tied entries are NVR-family — fall back to CAMERA_DB order
    for entry in CAMERA_DB:
        if entry["name"] in tied:
            return entry
    return None


def _identify_camera_brand(cam: dict, force: bool = False) -> dict | None:
    """rc2: Identify a camera's brand from ALL available signals (MAC OUI
    vendor, HTTP page title/server header, ONVIF vendor, hostname, nmap
    product banner) and write the result to cam["manufacturer"] in place.

    Returns the matched CAMERA_DB entry (containing throttle_type,
    request_behaviors, etc.) or None if no specific brand was identified.

    Critical for cameras whose ONVIF returns no usable vendor info — the
    OUI lookup field (cam["mac_vendor"]) often identifies them by their
    IEEE-registered manufacturer (e.g. "Microseven Inc"). Without this
    helper, such cameras were falling through to "Generic IP Camera" and
    missing their brand-specific throttle metadata.

    If cam already has a non-empty "manufacturer" field, returns the
    matching CAMERA_DB entry without overwriting (unless force=True).
    """
    if cam.get("manufacturer") and not force:
        # Brand already identified — return existing entry without overwriting
        return next(
            (e for e in CAMERA_DB if e["name"] == cam["manufacturer"]),
            None,
        )

    haystack = " ".join([
        str(cam.get("name", "") or ""),
        str(cam.get("vendor", "") or ""),
        str(cam.get("model", "") or ""),
        str(cam.get("hostname", "") or ""),
        str(cam.get("verdict_reason", "") or ""),
        str(cam.get("mac_vendor", "") or ""),       # OUI lookup result
        str(cam.get("page_title", "") or ""),
        str(cam.get("server_header", "") or ""),
        str(cam.get("nmap_product", "") or ""),
        # rc2.1.1: ONVIF scopes from WS-Discovery often contain
        # `onvif://www.onvif.org/manufacturer/<Brand>` or
        # `/hardware/<Model>` strings — primary identification signal
        # for cameras that aren't in nmap_results (no mac_vendor).
        str(cam.get("onvif_scopes", "") or ""),
    ]).strip()

    # 2.4.0-rc1.0: rtsp_realm_regex pre-pass.
    # If we have a captured RTSP auth realm (from _rtsp_options_fingerprint),
    # check it against any CAMERA_DB entries that define rtsp_realm_regex.
    # A regex match here is HIGH-confidence: realm strings are server-baked
    # and not user-customizable, so this overrides any weaker haystack
    # match (e.g. a substring hit on "web service" page title alone). The
    # primary motivation is the Lorex/Dahua DVR-NVR Family, whose
    # "Login to <32-hex>" realm is a stronger discriminator than the
    # generic "WEB SERVICE" HTTP title.
    rtsp_realm = str(cam.get("rtsp_auth_realm", "") or "").strip()
    if rtsp_realm:
        import re as _re_realm
        for entry in CAMERA_DB:
            pattern = entry.get("rtsp_realm_regex")
            if not pattern:
                continue
            try:
                if _re_realm.search(pattern, rtsp_realm):
                    cam["manufacturer"] = entry["name"]
                    return entry
            except _re_realm.error:
                # Bad regex in CAMERA_DB — log and skip; never raise to caller
                log.debug(
                    f"  bad rtsp_realm_regex on {entry['name']!r}: "
                    f"{pattern!r}"
                )
                continue

    if not haystack:
        return None

    entry = identify_manufacturer(haystack)
    if entry and entry["name"] != "Generic IP Camera":
        cam["manufacturer"] = entry["name"]
        return entry
    return None


# ─────────────────────────────────────────────────────────────────────────────
# OUI (MAC address) database
# ─────────────────────────────────────────────────────────────────────────────

# In-memory OUI lookup: "XX:XX:XX" (uppercase, colon-separated) → vendor string
_OUI_DB: dict[str, str] = {}
_OUI_DB_LOADED = False

# Curated embedded OUI entries for known camera and non-camera vendors.
# Used as fallback when the IEEE cache is unavailable, and to seed the
# camera/non-camera classification even before the full DB loads.
_CAMERA_OUI_VENDORS = {
    # Hikvision
    "1C:C3:16", "28:57:BE", "3C:E8:24", "44:19:B6", "48:EA:63",
    "4C:11:BF", "54:C4:15", "70:A7:41", "80:18:44", "84:EB:18",
    "A0:AC:1B", "B4:A3:82", "BC:AD:28", "C8:02:8F", "D8:69:73",
    "E8:EA:6A", "C8:C2:FA", "50:2A:8B", "D4:56:B0",
    # Dahua
    "70:62:B8", "90:02:A9", "98:03:D8", "A8:6B:7C",
    "C8:02:10", "E0:50:8B", "F4:AA:2C",
    # Axis Communications
    "00:40:8C", "AC:CC:8E", "B8:A4:4F", "F4:4D:30",
    # Hanwha / Samsung Techwin
    "00:09:18", "00:16:6C", "34:FC:EF",
    # Reolink
    "EC:71:DB", "DC:A6:32",
    # Amcrest / Dahua OEM
    "98:03:D8", "70:62:B8",
    # Mobotix
    "00:4A:E0",
    # ACTi
    "00:1F:9F",
    # Vivotek
    "00:02:D1",
    # GeoVision
    "00:13:E2",
    # Pelco
    "00:07:CB",
    # Bosch
    "00:04:63",
    # Sony (network cameras)
    "00:01:4A", "00:90:C6",
    # Panasonic
    "00:80:45", "04:B1:67",
    # Foscam
    "C4:D9:87", "E0:AE:5E",
    # TP-Link (Tapo cameras)
    "50:3E:AA", "98:DA:C4", "C0:06:C3",
    # Uniview (UNV)
    "E8:73:2E",
    # Lorex / FLIR
    "00:1C:F0", "C0:03:EF",
    # Milesight
    "2C:41:38",
    # Luxonis
    "44:A9:2C",
    # iENSO
    # (OUI not widely published — identified via HTTP)
}

# OUI prefixes of devices that are almost certainly NOT cameras
_NON_CAMERA_OUI_VENDORS: set[str] = {
    # Cisco Systems
    "00:00:0C", "00:01:42", "00:01:43", "00:01:96", "00:01:97",
    "00:03:6B", "00:03:E3", "00:0A:8A", "00:0E:38", "00:14:BF",
    "00:17:94", "00:19:E7", "00:1A:2F", "00:1B:2B", "00:1E:49",
    "00:1F:27", "00:21:A0", "00:22:BD", "00:23:AC", "00:24:14",
    "00:25:83", "00:26:0B", "00:27:0D", "00:60:2F",
    # Juniper Networks
    "00:05:85", "00:10:DB", "00:12:1E", "00:14:F6", "00:17:CB",
    "00:19:E2", "00:1F:12", "00:21:59", "00:23:9C", "00:24:DC",
    # MikroTik
    "00:0C:42", "2C:C8:1B", "4C:5E:0C", "6C:3B:6B", "74:4D:28",
    "8C:22:50", "B8:69:F4", "CC:2D:E0", "D4:CA:6D", "DC:2C:6E",
    "E4:8D:8C", "18:FD:74",
    # Ubiquiti Networks
    "00:15:6D", "00:27:22", "04:18:D6", "0C:80:63", "18:E8:29",
    "24:A4:3C", "44:D9:E7", "68:72:51", "80:2A:A8", "B4:FB:E4",
    "DC:9F:DB", "F0:9F:C2",
    # HP / Hewlett-Packard
    "00:01:E6", "00:02:A5", "00:0D:9D", "00:11:0A", "00:13:21",
    "00:17:08", "00:18:71", "00:1E:0B", "00:1F:29", "00:21:5A",
    "00:23:7D", "00:24:81", "00:25:B3", "00:26:55", "3C:D9:2B",
    # Dell
    "00:06:5B", "00:08:74", "00:0B:DB", "00:0F:1F", "00:11:43",
    "00:12:3F", "00:13:72", "00:14:22", "00:15:C5", "00:16:F0",
    "00:18:8B", "00:19:B9", "00:1A:A0", "00:1C:23", "00:1D:09",
    # Apple
    "00:03:93", "00:0A:27", "00:0A:95", "00:0D:93", "00:11:24",
    "00:14:51", "00:16:CB", "00:17:F2", "00:19:E3", "00:1B:63",
    "00:1C:B3", "00:1D:4F", "00:1E:52", "00:1E:C2", "00:1F:5B",
    "00:1F:F3", "00:21:E9", "00:22:41", "00:23:12", "00:23:32",
    "00:23:6C", "00:23:DF", "00:24:36", "00:25:00", "00:25:4B",
    "00:25:BC", "00:26:08", "00:26:4A", "00:26:B9", "00:26:BB",
    # Netgear
    "00:09:5B", "00:0F:B5", "00:14:6C", "00:18:4D", "00:1B:2F",
    "00:1E:2A", "00:1F:33", "00:22:3F", "00:24:B2", "00:26:F2",
    # ASUS
    "00:0C:6E", "00:11:2F", "00:13:D4", "00:15:F2", "00:17:31",
    "00:18:F3", "00:1A:92", "00:1B:FC", "00:1D:60", "00:1E:8C",
    "00:1F:C6", "00:22:15", "00:23:54", "00:24:8C", "00:26:18",
    # Brother (printers)
    "00:0C:29", "00:1B:A9", "00:80:77",
    # Epson (printers)
    "00:26:AB",
    # Synology (NAS)
    "00:11:32",
    # QNAP (NAS)
    "00:08:9B",
}


def _oui_key(mac: str) -> str:
    """Normalise a MAC address to XX:XX:XX uppercase OUI key."""
    mac = mac.upper().replace("-", ":").replace(".", ":")
    parts = mac.split(":")
    return ":".join(parts[:3]) if len(parts) >= 3 else ""


def load_oui_db() -> None:

    """
    Load the OUI database from /data/oui_cache.json into _OUI_DB.
    The cache is downloaded asynchronously by refresh_oui_db() on first run.
    """
    global _OUI_DB_LOADED
    if _OUI_DB_LOADED:
        return
    if OUI_CACHE_FILE.exists():
        try:
            _OUI_DB.update(json.loads(OUI_CACHE_FILE.read_text()))
            log.info(f"OUI DB loaded: {len(_OUI_DB)} entries")
        except Exception as e:
            log.warning(f"OUI cache load error: {e}")
    _OUI_DB_LOADED = True


async def refresh_oui_db() -> None:

    """
    Download the IEEE OUI CSV and cache it to /data/oui_cache.json.
    Runs once on startup if cache is missing or older than OUI_MAX_AGE_DAYS.
    Non-blocking — runs as a background task.
    """
    import csv, io, urllib.request

    needs_refresh = True
    if OUI_CACHE_FILE.exists():
        age_days = (time.time() - OUI_CACHE_FILE.stat().st_mtime) / 86400
        if age_days < OUI_MAX_AGE_DAYS:
            needs_refresh = False
            log.info(f"OUI cache is {age_days:.0f} days old — no refresh needed")

    if not needs_refresh:
        return

    log.info(f"Downloading IEEE OUI database from {OUI_CSV_URL}…")
    try:
        loop = asyncio.get_event_loop()

        def _download() -> str:

            req = urllib.request.Request(OUI_CSV_URL)
            req.add_header("User-Agent", "AnyCam/1.0")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")

        raw = await loop.run_in_executor(_THREAD_POOL, _download)

        # Parse CSV: Registry, Assignment (OUI hex), Organization Name, Address
        oui_map: dict[str, str] = {}
        reader = csv.reader(io.StringIO(raw))
        next(reader, None)  # skip header
        for row in reader:
            if len(row) < 3:
                continue
            assignment = row[1].strip().upper()  # e.g. "1CC316"
            org        = row[2].strip()
            if len(assignment) == 6:
                key = f"{assignment[0:2]}:{assignment[2:4]}:{assignment[4:6]}"
                oui_map[key] = org

        DATA_DIR.mkdir(exist_ok=True)
        OUI_CACHE_FILE.write_text(json.dumps(oui_map))
        _OUI_DB.update(oui_map)
        log.info(f"OUI DB refreshed: {len(oui_map)} entries cached")
    except Exception as e:
        log.warning(f"OUI DB download failed: {e} — will use embedded fallback")


def lookup_oui(mac: str) -> str:
    """
    Return the vendor name for a MAC address.
    Checks the full downloaded OUI DB first, then falls back to
    camera/non-camera embedded sets (returns prefix like 'Hikvision (OUI)').
    Returns empty string if unknown.
    """
    key = _oui_key(mac)
    if not key:
        return ""
    # Full downloaded DB
    if key in _OUI_DB:
        return _OUI_DB[key]
    # Curated embedded fallback label
    if key in _CAMERA_OUI_VENDORS:
        return "(known camera manufacturer)"
    if key in _NON_CAMERA_OUI_VENDORS:
        return "(known non-camera device)"
    return ""


def oui_is_camera(mac: str) -> bool | None:
    """
    Return True if OUI is a known camera manufacturer,
    False if a known non-camera device, None if unknown.
    """
    key = _oui_key(mac)
    if not key:
        return None
    # Check full DB vendor name against CAMERA_DB
    vendor = _OUI_DB.get(key, "").lower()
    if vendor:
        # Match against camera DB aliases
        for entry in CAMERA_DB:
            for alias in entry.get("aliases", []):
                if alias.lower() in vendor:
                    return True
        # Match against known non-camera keywords
        for kw in NON_CAMERA_KEYWORDS:
            if kw in vendor:
                return False
    # Curated embedded sets
    if key in _CAMERA_OUI_VENDORS:
        return True
    if key in _NON_CAMERA_OUI_VENDORS:
        return False
    return None

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

    During run_scan, PENDING_CAMERAS is set to a fresh dict at scan
    start. Newly-discovered cards accumulate there during the scan
    and only get flushed to CAMERAS after the dedup pass — preventing
    the UI from rendering transient duplicate cards (Microseven-as-
    4-cards, Hikvision-as-5-cards) that would later be collapsed by
    dedup. The user previously had a 40-80s window where they could
    click on cards that were about to disappear, which broke
    cred-entry flows mid-attempt.

    Outside of a scan (PENDING_CAMERAS is None), this is a no-op
    pass-through: cards go into CAMERAS as before. Callers that
    aren't in run_scan (manual-add, cred-attempt, etc.) still write
    directly to CAMERAS — only the scan-time discovery sites should
    use this helper.
    """
    if PENDING_CAMERAS is not None:
        PENDING_CAMERAS[cam["id"]] = cam
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
    return re.sub(r"(://)[^@]+@", r"\1", url) if url else url

# ─────────────────────────────────────────────────────────────────────────────
# Network helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_local_subnet() -> str:
    try:
        r = subprocess.run(["ip", "route", "show", "default"],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r"dev\s+(\S+)", r.stdout)
        if m:
            r2 = subprocess.run(["ip", "addr", "show", m.group(1)],
                                capture_output=True, text=True, timeout=5)
            m2 = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+/\d+)", r2.stdout)
            if m2:
                return str(ipaddress.ip_interface(m2.group(1)).network)
    except Exception as e:
        log.warning(f"Subnet: {e}")
    return "192.168.1.0/24"

def get_default_gateway() -> str | None:
    try:
        r = subprocess.run(["ip", "route", "show", "default"],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r"via\s+(\d+\.\d+\.\d+\.\d+)", r.stdout)
        return m.group(1) if m else None
    except Exception:
        return None

# ─────────────────────────────────────────────────────────────────────────────
# False-positive classifier
# ─────────────────────────────────────────────────────────────────────────────

def classify_device(nmap_info: dict) -> tuple[str, str]:
    all_text = " ".join([
        nmap_info.get("hostname", ""),
        " ".join(p.get("service", "") + " " + p.get("product", "")
                 for p in nmap_info.get("open_ports", [])),
    ]).lower()

    cam_hits = [k for k in CAMERA_KEYWORDS if k in all_text]
    if cam_hits:
        return "camera", f"Matched: {', '.join(cam_hits)}"

    non_hits = [k for k in NON_CAMERA_KEYWORDS if k in all_text]
    if non_hits:
        return "not_camera", f"Detected as: {', '.join(non_hits)}"

    # rc2.2 — Port-based not_camera classification.
    # Without -sV (rc2.2), nmap doesn't always tag print/SSH services in
    # the `service` text reliably across firmware variants. Fall back to
    # port presence: if a host exposes a print or SSH port AND no camera
    # keywords or RTSP ports were matched, it's not a camera. Cameras
    # virtually never expose 631 (IPP) or 9100 (raw print). SSH (22) is
    # weaker — some IP cameras have it open for service mode — so we
    # only fire the SSH-based reject when the host has NO HTTP-adjacent
    # ports that cameras typically expose.
    ports = [p["port"] for p in nmap_info.get("open_ports", [])]
    if any(p in (631, 9100) for p in ports):
        return "not_camera", "Print port detected (631/IPP or 9100/raw)"
    camera_http_ports = {80, 81, 88, 443, 554, 8000, 8080, 8081,
                         8082, 8443, 8554, 8765, 8888}
    if 22 in ports and not any(p in camera_http_ports for p in ports):
        return "not_camera", "SSH-only host (no camera ports)"

    if any(p in (554, 8554, 10554, 2020, 8765) for p in ports):
        return "camera", "RTSP port found"

    if all(p in (80, 443, 8080, 8443, 8000, 8888) for p in ports):
        return "uncertain", "HTTP only — no camera service identified"

    return "uncertain", "Unknown device type"

# ─────────────────────────────────────────────────────────────────────────────
# Stage 1a — ARP ping scan (finds live hosts without full port scan)
# ─────────────────────────────────────────────────────────────────────────────

def get_local_ip() -> str | None:
    """Return this machine's primary LAN IP."""
    try:
        # Connect a UDP socket to find the default route interface IP
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return None


def discover_live_hosts(subnet: str) -> set[str]:
    """
    Stage 1a: ARP ping scan to find live hosts without triggering port scan
    timeouts on dead IPs.  Falls back to ICMP ping if ARP returns nothing
    (can happen when nmap lacks raw-socket privileges in some containers).
    Always includes the local machine's own IP so the OAK Camera addon
    (RTSP on port 8765) is always scanned.
    """
    log.info(f"ARP ping scan: {subnet}")
    live = set()
    try:
        r = subprocess.run(
            ["nmap", "-sn", "-PR", "-T4", "--host-timeout", "8s", "-oX", "-", subnet],
            capture_output=True, text=True, timeout=60,
        )
        root = ET.fromstring(r.stdout)
        for host in root.findall("host"):
            st = host.find("status")
            if st is not None and st.get("state") == "up":
                addr = host.find("address[@addrtype='ipv4']")
                if addr is not None:
                    live.add(addr.get("addr"))
        log.info(f"ARP scan: {len(live)} live host(s)")
    except Exception as e:
        log.warning(f"ARP scan error: {e}")

    # Fallback: if ARP found very few hosts (< 3), supplement with ICMP ping scan
    if len(live) < 3:
        log.info("ARP returned few results — supplementing with ICMP ping scan")
        try:
            r = subprocess.run(
                ["nmap", "-sn", "-PE", "-T4", "--host-timeout", "8s", "-oX", "-", subnet],
                capture_output=True, text=True, timeout=90,
            )
            root = ET.fromstring(r.stdout)
            for host in root.findall("host"):
                st = host.find("status")
                if st is not None and st.get("state") == "up":
                    addr = host.find("address[@addrtype='ipv4']")
                    if addr is not None:
                        live.add(addr.get("addr"))
            log.info(f"After ICMP fallback: {len(live)} live host(s)")
        except Exception as e:
            log.warning(f"ICMP ping fallback error: {e}")

    # Always include this machine's own IP — the OAK Camera addon serves
    # RTSP on port 8765 on the Pi itself, which wouldn't be found otherwise.
    local_ip = get_local_ip()
    if local_ip:
        live.add(local_ip)
        log.info(f"Added local IP: {local_ip}")

    return live

# ─────────────────────────────────────────────────────────────────────────────
# Stage 1b — SSDP / UPnP discovery
# ─────────────────────────────────────────────────────────────────────────────

_SSDP_PROBE = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 3\r\n"
    "ST: ssdp:all\r\n"
    "\r\n"
)

def ssdp_discover(timeout: int = 5) -> list[dict]:
    """
    UPnP/SSDP M-SEARCH on 239.255.255.250:1900.
    Many IP cameras, NVRs and video encoders announce themselves via SSDP.
    Returns all responding devices, flagging likely cameras.
    """
    results, seen = [], set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        sock.sendto(_SSDP_PROBE.encode(), ("239.255.255.250", 1900))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
                ip = addr[0]
                if ip in seen:
                    continue
                seen.add(ip)
                text     = data.decode("utf-8", errors="replace")
                combined = text.lower()
                is_camera = any(k in combined for k in (
                    "camera", "ipcam", "nvr", "dvr", "onvif", "rtsp",
                    "hikvision", "dahua", "reolink", "axis", "amcrest",
                    "networkvideoserver", "networkcamera", "videoserver",
                ))
                server_m = re.search(r"server:\s*([^\r\n]+)", text, re.I)
                name     = server_m.group(1).strip() if server_m else ip
                results.append({"ip": ip, "name": name[:80], "is_camera": is_camera})
                log.info(f"  SSDP: {ip} — {name[:60]} {'[camera]' if is_camera else ''}")
            except socket.timeout:
                break
        sock.close()
    except Exception as e:
        log.debug(f"SSDP: {e}")
    return results

# ─────────────────────────────────────────────────────────────────────────────
# Stage 1c — mDNS / Bonjour discovery
# ─────────────────────────────────────────────────────────────────────────────

def _build_mdns_query(service: str) -> bytes:
    """Minimal DNS PTR query for mDNS (RFC 6762)."""
    header = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
    qname  = b""
    for label in service.encode().split(b"."):
        if label:
            qname += bytes([len(label)]) + label
    qname += b"\x00"
    footer = struct.pack(">HH", 12, 0x8001)   # PTR, multicast class
    return header + qname + footer


def mdns_discover(timeout: int = 5) -> list[dict]:
    """
    mDNS (Bonjour) discovery on 224.0.0.251:5353.
    Queries _rtsp._tcp, _onvif._tcp, _camera._tcp and passively collects
    any responses that reference camera-related service names.
    Falls back gracefully if port 5353 is already in use by avahi.
    """
    CAMERA_SERVICES = [
        "_rtsp._tcp.local", "_onvif._tcp.local",
        "_camera._tcp.local", "_nvr._tcp.local",
    ]
    CAMERA_BYTES = [b"_rtsp", b"_onvif", b"_camera", b"_nvr", b"camera", b"ipcam"]

    results, seen = [], set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        try:
            sock.bind(("", 5353))
        except OSError:
            # Port already bound (avahi) — use ephemeral port for sending only
            sock.bind(("", 0))

        mreq = struct.pack("4sL", socket.inet_aton("224.0.0.251"), socket.INADDR_ANY)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            pass

        for svc in CAMERA_SERVICES:
            try:
                sock.sendto(_build_mdns_query(svc), ("224.0.0.251", 5353))
            except Exception:
                pass

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
                ip = addr[0]
                if ip not in seen and any(cb in data for cb in CAMERA_BYTES):
                    seen.add(ip)
                    results.append({"ip": ip, "name": ip, "source": "mDNS"})
                    log.info(f"  mDNS camera: {ip}")
            except socket.timeout:
                break
        sock.close()
    except Exception as e:
        log.debug(f"mDNS: {e}")
    return results

# ─────────────────────────────────────────────────────────────────────────────
# Stage 1d — ONVIF WS-Discovery (already present, kept here for completeness)
# ─────────────────────────────────────────────────────────────────────────────

_WS_PROBE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"'
    ' xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
    ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
    ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    "<e:Header>"
    "<w:MessageID>uuid:{mid}</w:MessageID>"
    "<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
    "<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>"
    "</e:Header>"
    "<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>"
    "</e:Envelope>"
)

def onvif_discover(timeout: int = 5) -> list[dict]:
    results, seen = [], set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        sock.settimeout(timeout)
        sock.sendto(_WS_PROBE.replace("{mid}", str(uuid.uuid4())).encode(),
                    ("239.255.255.250", 3702))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
                ip = addr[0]
                if ip in seen:
                    continue
                seen.add(ip)
                text   = data.decode("utf-8", errors="replace")
                xaddrs = re.findall(r"<[^>]*XAddrs[^>]*>([^<]+)<", text)
                scopes = re.findall(r"<[^>]*Scopes[^>]*>([^<]+)<", text)
                m      = re.search(r"onvif://www\.onvif\.org/name/([^\s]+)",
                                   " ".join(scopes))
                name   = m.group(1).replace("%20", " ") if m else ip
                # rc2.1.1: capture the full scopes string (not just the
                # name). Many ONVIF cameras populate scopes with their
                # manufacturer/hardware/model identifiers, e.g.
                # `onvif://www.onvif.org/manufacturer/Microseven`. These
                # strings are fed into the brand-id haystack downstream
                # so the Hipcam/Microseven CAMERA_DB entry can match
                # against its `aliases` and `onvif_scopes` fields even
                # for cameras whose ONVIF Name field returns just "IPCAM".
                onvif_scopes_str = " ".join(scopes)
                results.append({"ip": ip, "name": name,
                                 "xaddrs": xaddrs[0].strip() if xaddrs else "",
                                 "onvif_scopes": onvif_scopes_str})
                log.info(f"  ONVIF: {name} @ {ip}")
            except socket.timeout:
                break
        sock.close()
    except Exception as e:
        log.debug(f"ONVIF WS-Discovery: {e}")
    return results

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — nmap scans
# ─────────────────────────────────────────────────────────────────────────────

# 2.4.0-rc2.1 — Camera-relevant TCP ports for focused_nmap_scan.
# Replaces the rc2.0 approach of "--top-ports 1000 + small augmentation"
# (which had a regression: nmap intersects -p with --top-ports rather
# than unioning, silently shrinking the scan to ~4 ports). Using -p
# with this explicit list scans ONLY camera-relevant ports — vastly
# faster than top-1000 (which wastes 95%+ of probes on non-camera
# services like SMTP, NFS, MySQL, X11) and catches every brand we know
# about plus generic alt-HTTP/HTTPS ports used as common practice.
#
# This list is the union of:
#   (a) CAMERA_DB default_ports across all 78 entries (22 ports), and
#   (b) ports documented by manufacturer/VMS-vendor/industry sources
#       NOT yet represented in CAMERA_DB default_ports (30 ports).
#
# Research foundation: 80+ authoritative sources (manufacturer-official
# documentation, support knowledge bases, VMS vendor docs). Full
# bibliography in the rc2.1 audit report.
#
# Trade-off: an "exotic camera on a truly weird port" (e.g., 12345)
# will be missed. In practice this is vanishingly rare — camera firmware
# almost always picks ports in HTTP-adjacent or RTSP-adjacent ranges.
# Devices on weird ports that ARE on the network still appear in the
# ARP-discovered live-hosts list; they just have no service info.
CAMERA_RELEVANT_PORTS: list[int] = [
    # ── Classifier-only ports (rc2.2) ────────────────────────────────
    # These ports are NOT camera ports — they're scanned so the verdict
    # logic has signals to REJECT non-camera devices that happen to
    # expose a web UI on 80/443/8080. Without these, an HP printer or
    # NAS with only a web admin page falls through as a "camera
    # candidate" and gets RTSP-probed unnecessarily. rc1.0's top-1000
    # scan caught these incidentally; rc2.1's narrow port list lost
    # them, surfacing a regression where the an HP printer (gSOAP
    # 2.7 web admin) re-appeared as a camera. Cost: 3 extra ports per
    # host = ~50ms total in our SYN-only scan.
    22,     # SSH — IoT device, NAS, embedded Linux non-cameras
    631,    # IPP printing — every modern network printer
    9100,   # Raw print (HP JetDirect) — virtually all network printers
    # ── Standard camera ports (in CAMERA_DB) ──────────────────────────
    80,     # HTTP — universal
    81,     # Blue Iris default web; common alt-HTTP for cameras
    86,     # Pelco RTP/RTSP-over-HTTP tunnel
    88,     # Foscam alt-HTTP
    443,    # HTTPS — universal
    554,    # RTSP — universal
    558,    # Hanwha NVR (newer)
    1085,   # Swann (legacy)
    1756,   # Bosch RCP+ (proprietary)
    1757,   # Bosch RCP+ (proprietary)
    1758,   # Bosch RCP+ (proprietary)
    1935,   # RTMP — Reolink, generic camera streaming
    2020,   # ACTi
    2543,   # Pelco/3xLogic
    4520,   # Hanwha SUNAPI device port range
    4521,   # Hanwha SUNAPI device port range
    4522,   # Hanwha SUNAPI device port range
    4523,   # Hanwha SUNAPI device port range
    4524,   # Hanwha SUNAPI device port range (final = RTSP for some NVRs)
    4550,   # GeoVision command port
    7001,   # Network Optix Nx Witness mediaserver
    7441,   # Ubiquiti UniFi Protect RTSP
    7442,   # Ubiquiti UniFi Protect NVR communications
    7443,   # Ubiquiti UniFi Protect HTTPS UI
    7444,   # Ubiquiti UniFi Protect camera firmware
    7446,   # Ubiquiti UniFi Protect web-media
    7447,   # Ubiquiti UniFi Protect SRTSP
    7550,   # Ubiquiti UniFi Protect streaming
    8000,   # Hikvision SDK; alt-HTTP for some
    8001,   # alt-HTTP range
    8080,   # Mobotix/Vivotek/generic alt-HTTP
    8081,   # Vivotek secondary HTTP; common alt
    8082,   # alt-HTTP range
    8443,   # alt-HTTPS — universal practice (was missing from CAMERA_DB)
    8554,   # Vivotek/GeoVision alt-RTSP
    8765,   # GeoVision alt
    8888,   # Foscam streaming
    8899,   # Foscam ONVIF
    9000,   # Reolink basic service port
    9010,   # Hikvision Ezviz command
    9020,   # Hikvision Ezviz live view
    9090,   # Uniview admin
    10554,  # Hikvision alt-RTSP
    34567,  # Dahua-variant admin (XMeye/H264DVR firmware)
    35000,  # Dahua-variant admin
    37777,  # Dahua TCP admin
    49152,  # Pelco Endura/non-Sarix; Axis UPnP
    49153,  # Pelco Endura svc-tcp range
    49154,  # Pelco Endura svc-tcp range
    49155,  # Pelco Endura svc-tcp range
    49156,  # Pelco Endura svc-tcp range
]


def focused_nmap_scan(host_list: list[str]) -> list[dict]:
    """
    Scan only known-live hosts on the camera-relevant TCP port set
    (CAMERA_RELEVANT_PORTS, currently 54 ports — see definition above
    for derivation, classifier-port rationale, and source bibliography).

    rc2.2 redesign — drops `-sV` (version detection) entirely and
    lowers `--host-timeout` from 30s to 15s. Empirically measured on
    CrystalHeeler's test system B: rc2.1's `-sV --host-timeout 30s` took 36s
    for 3 hosts AND timed out the Lorex DVR (dropped from output
    entirely). Pure SYN scan with `--host-timeout 15s` took 1.5s for
    the same 3 hosts AND found the Lorex's 80/554/35000 cleanly. ~24×
    faster, and more reliable on slower devices.

    Why `-sV` was hurting:
      • -sV runs sequential service-banner probes per open port. On
        slow/throttled devices (Lorex DVR, Microseven Hipcam) the per-
        port probe latency stacks up past --host-timeout's 30s budget.
        When the budget expires nmap DROPS THE ENTIRE HOST including
        already-confirmed open ports — they never reach our parser.
      • The `product` field that -sV adds (e.g., "gSOAP 2.7") is one
        of ~10 haystack signals in identify_manufacturer; mac_vendor
        + ONVIF + HTTP probe + RTSP probe downstream more than
        compensate. Lost coverage: zero on real-world devices tested.

    What we keep without -sV:
      • Open-port list (the actual goal of the scan)
      • mac_vendor (strongest classifier — HP, Lorex Technology, etc.)
      • service field from /etc/services lookup ("http", "rtsp",
        "https") — sufficient for _initial_protocol() routing

    Because hosts are pre-confirmed alive via ARP, no timeout waste on
    dead IPs. Typical time: 1-3 seconds for 14 hosts (rc1.0/rc2.0
    baseline was ~80s; rc2.1 was still ~75s due to -sV).
    """
    if not host_list:
        return []
    port_list = ",".join(str(p) for p in CAMERA_RELEVANT_PORTS)
    log.info(f"Focused scan: {len(host_list)} host(s), "
             f"{len(CAMERA_RELEVANT_PORTS)} camera-relevant ports")
    try:
        r = subprocess.run(
            ["nmap", "--open", "-p", port_list,
             "--host-timeout", "15s", "-T4", "-oX", "-"] + host_list,
            capture_output=True, text=True, timeout=360,
        )
        hosts = _parse_nmap_xml(r.stdout)
        log.info(f"Focused scan: {len(hosts)} host(s) responded")
        return hosts
    except Exception as e:
        log.warning(f"Focused nmap: {e}")
        return []


def broad_nmap_scan(host_list: list[str]) -> list[dict]:
    """
    Broader scan (ports 0-10000) on hosts that were alive but didn't
    respond to camera ports.  Only runs if user enables broad sweep.
    Typical time: 1-4 minutes depending on host count.
    """
    if not host_list:
        return []
    log.info(f"Broad scan (0-10000): {len(host_list)} host(s)")
    try:
        r = subprocess.run(
            ["nmap", "-sV", "--open", "-p", "0-10000",
             "--host-timeout", "90s", "-T4", "-oX", "-"] + host_list,
            capture_output=True, text=True, timeout=600,
        )
        hosts = _parse_nmap_xml(r.stdout)
        log.info(f"Broad scan: {len(hosts)} host(s) responded")
        return hosts
    except Exception as e:
        log.warning(f"Broad nmap: {e}")
        return []


def _parse_nmap_xml(xml_text: str) -> list[dict]:
    results = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return results
    for host in root.findall("host"):
        st = host.find("status")
        if st is None or st.get("state") != "up":
            continue
        addr_el = host.find("address[@addrtype='ipv4']")
        if addr_el is None:
            continue
        ip = addr_el.get("addr")
        hn = host.find("hostnames/hostname")
        hostname = hn.get("name", ip) if hn is not None else ip

        # Extract MAC address + nmap's built-in OUI vendor (ARP scan only)
        mac_el  = host.find("address[@addrtype='mac']")
        mac_addr   = mac_el.get("addr", "")   if mac_el is not None else ""
        mac_vendor = mac_el.get("vendor", "") if mac_el is not None else ""

        # Supplement nmap vendor with our full OUI DB if nmap didn't identify it
        if mac_addr and not mac_vendor:
            mac_vendor = lookup_oui(mac_addr)

        open_ports = []
        for p in host.findall("ports/port"):
            pst = p.find("state")
            if pst is None or pst.get("state") != "open":
                continue
            svc = p.find("service")
            open_ports.append({
                "port":    int(p.get("portid")),
                "service": svc.get("name", "")    if svc is not None else "",
                "product": svc.get("product", "") if svc is not None else "",
            })
        if open_ports:
            results.append({
                "ip": ip, "hostname": hostname, "open_ports": open_ports,
                "mac_addr": mac_addr, "mac_vendor": mac_vendor,
            })
    return results


def _initial_protocol(port: int, service: str, product: str) -> str:
    c = (service + " " + product).lower()
    if port in (554, 8554, 10554, 2020, 8765):
        return "RTSP"
    if port in (1935, 1936):
        return "RTMP"
    if port in (80, 8080, 8000, 8888, 443, 8443):
        return "RTSP" if ("rtsp" in c or "camera" in c) else "HTTP"
    if port in (37777, 34567):
        return "DVR"
    return service.upper() or "UNKNOWN"

# ─────────────────────────────────────────────────────────────────────────────
# Protocol probers
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────
# Pure-Python RTSP probe — no ffprobe dependency, no probesize limits,
# no URL-encoding workarounds.  Implements RFC 2326 (RTSP) OPTIONS +
# DESCRIBE with Digest and Basic auth negotiation.
# ─────────────────────────────────────────────────────────────────────────
def probe_rtsp_socket(host: str, port: int, path: str,
                      username: str = "", password: str = "",
                      timeout: float = 6.0,
                      label: str = "") -> bool:
    """
    Verify an RTSP stream is genuinely streamable.  Returns True only if
    OPTIONS + DESCRIBE succeed AND a SETUP round-trip on the first
    m=video track succeeds (TCP-interleaved first, UDP fallback).  This
    catches the common false-positive case where a camera 200-OKs DESCRIBE
    on a bare root path but rejects SETUP because no real track lives
    there (Microseven and similar firmware).

    Pass label="" for silent (debug-only) logging, or a non-empty
    string (e.g. camera_id/profile) for verbose INFO-level logging
    of each RTSP round-trip — useful when diagnosing credential failures.
    """
    rtsp_url = f"rtsp://{host}:{port}{path}"
    pfx = f"  [probe_rtsp {label or host + ':' + str(port) + path}]"

    def _log(msg: str) -> None:
        if label:
            log.info(pfx + " " + msg)
        else:
            log.debug(pfx + " " + msg)

    CRLF     = chr(13) + chr(10)
    CRLFCRLF = CRLF + CRLF

    def _build_auth(auth_val: str, method: str, uri: str) -> str | None:
        """Build an Authorization header value for the given method+uri,
        given the WWW-Authenticate value from a prior 401 response.

        2.2.9 — qop-aware per RFC 2617 §3.2.2. When the challenge carries
        qop="auth" (observed live on Hikvision DS-2DE4A425IW with realm
        "IP Camera(F0818)" — was previously emitting a no-qop challenge,
        switched to qop="auth" sometime during rc2.x debugging), the
        response digest formula changes to
            MD5(HA1:nonce:nc:cnonce:qop:HA2)
        and the Authorization header must include qop, cnonce, and nc.
        Falls back to the no-qop formula MD5(HA1:nonce:HA2) when qop is
        absent (Hipcam family, older Hikvision firmware, etc.).

        Reuses the server's nonce — RFC 2617 allows nonce reuse for
        subsequent requests in the same session, recomputing the response
        digest with the new method+uri in HA2 (and incrementing nc when
        qop is in play, though we only issue one qop-aware request per
        nonce here so nc=00000001 is correct)."""
        if auth_val.lower().startswith("digest"):
            realm_m = re.search(r'realm="([^"]*)"', auth_val)
            nonce_m = re.search(r'nonce="([^"]*)"', auth_val)
            if not (realm_m and nonce_m):
                return None
            realm, nonce = realm_m.group(1), nonce_m.group(1)
            qop_m = re.search(r'qop="?([^",]+)"?', auth_val)
            ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
            ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
            if qop_m:
                qop_val = qop_m.group(1).strip()
                # Pick "auth" when offered (most common); some servers send
                # "auth,auth-int" and we only do auth (no message-body integrity).
                qop = "auth" if "auth" in qop_val else qop_val.split(",")[0].strip()
                cnonce = os.urandom(8).hex()
                nc = "00000001"
                rsp = hashlib.md5(
                    f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
                ).hexdigest()
                return (f'Digest username="{username}", realm="{realm}", '
                        f'nonce="{nonce}", uri="{uri}", '
                        f'qop={qop}, nc={nc}, cnonce="{cnonce}", '
                        f'response="{rsp}"')
            rsp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            return (f'Digest username="{username}", realm="{realm}", '
                    f'nonce="{nonce}", uri="{uri}", response="{rsp}"')
        elif auth_val.lower().startswith("basic"):
            import base64 as _b64
            return "Basic " + _b64.b64encode(
                f"{username}:{password}".encode()).decode()
        return None

    def _parse_track_url(sdp: str, base_url: str) -> str | None:
        """Find first m=video block in the SDP, extract its a=control:
        value, and resolve it against base_url. Returns None if no
        m=video or no control attribute exists."""
        in_video        = False
        video_control   = None
        session_control = None
        for raw in sdp.split("\n"):
            line = raw.rstrip("\r").strip()
            if line.startswith("m="):
                if in_video:
                    break  # next media block — stop, video control is final
                in_video = line.startswith("m=video")
            elif line.startswith("a=control:"):
                ctrl = line[len("a=control:"):].strip()
                if in_video:
                    video_control = ctrl
                else:
                    session_control = ctrl
        ctrl = video_control or session_control
        if not ctrl:
            return None
        if ctrl == "*":
            return base_url
        if ctrl.lower().startswith("rtsp://"):
            return ctrl
        # Relative — append to base, ensuring exactly one separator
        return (base_url.rstrip("/") + "/" + ctrl)

    sock      = None
    next_cseq = 1
    auth_val  = None   # WWW-Authenticate value if any 401 was returned
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)

        def roundtrip(method: str, cseq: int,
                      extra: dict | None = None,
                      uri: str | None = None) -> str:
            """Send an RTSP request and read response (headers + body if
            Content-Length present). uri overrides the default rtsp_url
            (used for SETUP/TEARDOWN where the track URI differs)."""
            extra  = extra or {}   # safe: new dict each call
            target = uri or rtsp_url
            hdr    = "".join(k + ": " + v + CRLF for k, v in extra.items())
            req    = method + " " + target + " RTSP/1.0" + CRLF
            req   += "CSeq: " + str(cseq) + CRLF + hdr + CRLF
            sock.sendall(req.encode())
            buf = b""
            # Read until end-of-headers
            while CRLFCRLF.encode() not in buf and len(buf) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            text = buf.decode("utf-8", errors="replace")
            # If a Content-Length was advertised, read the body too
            cl = 0
            for line in text.split(CRLF):
                if line.lower().startswith("content-length:"):
                    try:
                        cl = int(line.split(":", 1)[1].strip())
                    except (ValueError, IndexError):
                        cl = 0
                    break
            if cl > 0:
                sep_idx   = text.find(CRLFCRLF)
                already   = (len(buf) - (sep_idx + 4)) if sep_idx >= 0 else 0
                remaining = max(0, cl - already)
                while remaining > 0:
                    chunk = sock.recv(min(remaining, 4096))
                    if not chunk:
                        break
                    buf       += chunk
                    remaining -= len(chunk)
            return buf.decode("utf-8", errors="replace")

        # ── OPTIONS ──────────────────────────────────────────────────────
        resp = roundtrip("OPTIONS", next_cseq); next_cseq += 1
        status_line = resp.split(CRLF)[0].strip()
        if "RTSP/1.0 2" not in resp:
            _log(f"OPTIONS → {status_line!r} (not 2xx — giving up)")
            return False
        _log("OPTIONS → OK")

        # ── DESCRIBE (with optional 401-retry) ───────────────────────────
        resp = roundtrip("DESCRIBE", next_cseq, {"Accept": "application/sdp"})
        next_cseq += 1
        status_line = resp.split(CRLF)[0].strip()
        if "RTSP/1.0 200" in resp:
            _log("DESCRIBE → 200 OK (no auth required)")
        elif "401" in resp:
            if not username:
                _log("DESCRIBE → 401 (no credentials provided — rejecting)")
                return False
            auth_line = next(
                (l for l in resp.splitlines()
                 if l.lower().startswith("www-authenticate:")), "")
            auth_val = auth_line.split(":", 1)[-1].strip() if auth_line else ""
            if not auth_val:
                _log("DESCRIBE → 401 with no WWW-Authenticate header")
                return False
            scheme = auth_val.split()[0] if auth_val else "?"
            _log(f"DESCRIBE → 401 ({scheme})")
            auth_hdr = _build_auth(auth_val, "DESCRIBE", rtsp_url)
            if not auth_hdr:
                _log(f"DESCRIBE → 401 unparseable auth: {auth_val[:60]!r}")
                return False
            resp = roundtrip("DESCRIBE", next_cseq,
                             {"Accept": "application/sdp",
                              "Authorization": auth_hdr})
            next_cseq += 1
            if "RTSP/1.0 200" not in resp:
                sl = resp.split(CRLF)[0].strip()
                _log(f"DESCRIBE (authenticated) → {sl!r}")
                return False
            _log("DESCRIBE (authenticated) → 200 OK")
        else:
            _log(f"DESCRIBE → {status_line!r} (not 200 / not 401 — rejecting)")
            return False

        # ── Parse SDP for first m=video track URL ────────────────────────
        body_idx  = resp.find(CRLFCRLF)
        sdp_text  = resp[body_idx + 4:] if body_idx >= 0 else ""
        track_url = _parse_track_url(sdp_text, rtsp_url)
        if not track_url:
            _log("DESCRIBE 200 but SDP has no m=video / a=control — rejecting")
            return False
        _log(f"SDP track URL: {track_url}")

        # ── SETUP: TCP-interleaved first, UDP fallback ───────────────────
        # Catches the false-positive case where DESCRIBE 200 OKs but the
        # camera has no real track at this path (rejects SETUP with 4xx).
        # We never bind UDP locally — server's 200 OK to SETUP is enough
        # to verify the stream is genuinely streamable.
        transports = [
            ("RTP/AVP/TCP;unicast;interleaved=0-1",            "TCP-interleaved"),
            ("RTP/AVP/UDP;unicast;client_port=50000-50001",    "UDP"),
        ]
        for transport_hdr, tlabel in transports:
            extra = {"Transport": transport_hdr}
            if auth_val:
                ah = _build_auth(auth_val, "SETUP", track_url)
                if ah:
                    extra["Authorization"] = ah
            resp = roundtrip("SETUP", next_cseq, extra, uri=track_url)
            next_cseq += 1
            sl = resp.split(CRLF)[0].strip()
            if "RTSP/1.0 200" in resp:
                # Extract Session header so TEARDOWN can release it
                session = ""
                for line in resp.split(CRLF):
                    if line.lower().startswith("session:"):
                        session = line.split(":", 1)[1].strip().split(";")[0].strip()
                        break
                _log(f"SETUP ({tlabel}) → 200 OK (session={session[:12]!r})")

                # ── TEARDOWN — release session cleanly ────────────────────
                td_extra: dict = {}
                if session:
                    td_extra["Session"] = session
                if auth_val:
                    ah = _build_auth(auth_val, "TEARDOWN", track_url)
                    if ah:
                        td_extra["Authorization"] = ah
                try:
                    roundtrip("TEARDOWN", next_cseq, td_extra, uri=track_url)
                    next_cseq += 1
                except Exception:
                    pass   # TEARDOWN failure is non-fatal — socket close
                           # also releases server-side state
                return True
            _log(f"SETUP ({tlabel}) → {sl!r}")

        return False

    except Exception as e:
        _log(f"exception: {e}")
        return False
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def probe_rtsp(url: str, username: str = "", password: str = "",
               timeout: int = 6, label: str = "") -> bool:
    """Thin wrapper — parses URL and delegates to probe_rtsp_socket.
    Pass label (e.g. camera_id/profile_name) to get verbose INFO-level logging."""
    try:
        from urllib.parse import urlparse
        p    = urlparse(url)
        host = p.hostname or ""
        port = p.port or 554
        path = p.path or "/"
        # Prefer caller-supplied credentials over any embedded in the URL
        u  = username or p.username or ""
        pw = password or p.password or ""
        return probe_rtsp_socket(host, port, path, u, pw,
                                 timeout=timeout, label=label)
    except Exception as e:
        log.debug(f"probe_rtsp: {e}")
        return False


def _probe_rtsp_paths_single_socket(
    host: str,
    port: int,
    paths: list[str],
    username: str = "",
    password: str = "",
    timeout: float = 6.0,
    extra_query: str = "",
    label: str = "",
    host_meta: dict | None = None,
    collect_locked: bool = False,
    expected_realm: str = "",
    deep_reprobe_mode: bool = False,
    brand_recipe_paths: list[str] | None = None,
) -> tuple[str | None, bool]:
    """rc2 Layer 1: Walk multiple RTSP paths through OPTIONS+DESCRIBE+
    SETUP+TEARDOWN on a SINGLE TCP socket. Returns a tuple
    (url_or_none, looks_like_rtsp_server).

    looks_like_rtsp_server is True if at least one response started with
    "RTSP/" — even if the status was 4xx/5xx, the server demonstrably
    speaks RTSP. False means we received NO RTSP-formatted responses
    (server is HTTP, raw TCP, or the path failed before any handshake).
    rc2.1 callers use this to fast-bail Layer 2 when the wrong port was
    probed (e.g. RTSP path-walk against port 80 of a Hikvision NVR).

    This is the universal-safe probe per RFC 2326 §9.1 (servers must
    queue per-socket requests in order; no documented camera firmware
    rejects sequential request-response on the same socket). Critical
    for cameras with per-IP TCP rate-limits (Hipcam/Microseven family
    — opening a second socket within ~5s of the first is RST'd, but
    walking paths on ONE socket bypasses the throttle entirely).

    Always reads the full response (headers + Content-Length-bounded
    body) before sending the next request — never pipelines.

    extra_query — appended to every path (e.g. "?Axis-Orig-Sw=true"
    for Axis Companion). Empty by default.

    rc2.1.1: host_meta — when provided, the walker captures the RTSP
    'Server:' header from the first response that includes one and
    writes it to host_meta["server_header"] in place. The orchestrator
    then re-runs brand identification post-walk so cameras that didn't
    identify from MAC OUI alone (Microseven — not in nmap_results,
    so no mac_vendor) can still be identified by their Hipcam RealServer
    server string.

    2.4.0-rc2.0 (Layered Stream Discovery): collect_locked — when True,
    AFTER finding a working unauthenticated URL the walker continues
    through remaining paths, collecting 401 responses whose realm
    matches expected_realm (or any 401 if expected_realm is empty).
    Each match is added to host_meta["locked_streams"] as a dict
    {"path": <path>, "realm": <realm>, "scheme": <scheme>}. The first
    working URL is still returned as the primary `found` value; the
    locked list surfaces in the UI as a "View Locked Streams (N)" badge
    so the user can supply credentials and unlock additional streams
    (sub-streams, third streams, audio-only feeds) that the unauth
    walk found but couldn't authenticate against.

    expected_realm — when non-empty, restricts locked-stream candidates
    to 401s whose realm matches. Prevents surfacing locked streams that
    need different credentials than the camera's primary auth domain.
    Captured upstream by the OPTIONS fingerprint helper (rc1.0) into
    host_meta["rtsp_auth_realm"]; the orchestrator passes that value
    here as expected_realm.
    """
    if not paths:
        return (None, False)

    # rc2.1: tracks whether ANY response from the server started with
    # "RTSP/" — used by the orchestrator to skip Layer 2 when we
    # confirmed the host doesn't speak RTSP at this port.
    looks_like_rtsp: bool = False

    # rc2.1.1: capture Server header from first response that has one.
    # Written back to host_meta at end so brand-id can re-run with it.
    captured_server: str = ""

    # 2.4.0-rc2.0 (Layered Stream Discovery): once a working URL is
    # found AND collect_locked=True, we keep walking and accumulate
    # 401-with-matching-realm into this list. Persisted to host_meta
    # at the finally block (alongside server_header) so the orchestrator
    # can surface them in the UI badge.
    locked_streams: list[dict] = []
    found_working_url: str | None = None

    pfx = f"  [probe_rtsp_walk {label or host + ':' + str(port)}]"

    def _log(msg: str) -> None:
        if label:
            log.info(pfx + " " + msg)
        else:
            log.debug(pfx + " " + msg)

    CRLF     = chr(13) + chr(10)
    CRLFCRLF = CRLF + CRLF

    def _build_auth(auth_val: str, method: str, uri: str) -> str | None:
        """Identical to probe_rtsp_socket's _build_auth — RFC 2617 nonce
        reuse with per-method+uri response digest recompute. 2.2.9 — adds
        qop=auth handling per RFC 2617 §3.2.2 for cameras (e.g. Hikvision
        DS-2DE4A425IW) that emit qop="auth" in the challenge."""
        if auth_val.lower().startswith("digest"):
            realm_m = re.search(r'realm="([^"]*)"', auth_val)
            nonce_m = re.search(r'nonce="([^"]*)"', auth_val)
            if not (realm_m and nonce_m):
                return None
            realm, nonce = realm_m.group(1), nonce_m.group(1)
            qop_m = re.search(r'qop="?([^",]+)"?', auth_val)
            ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
            ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
            if qop_m:
                qop_val = qop_m.group(1).strip()
                qop = "auth" if "auth" in qop_val else qop_val.split(",")[0].strip()
                cnonce = os.urandom(8).hex()
                nc = "00000001"
                rsp = hashlib.md5(
                    f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
                ).hexdigest()
                return (f'Digest username="{username}", realm="{realm}", '
                        f'nonce="{nonce}", uri="{uri}", '
                        f'qop={qop}, nc={nc}, cnonce="{cnonce}", '
                        f'response="{rsp}"')
            rsp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            return (f'Digest username="{username}", realm="{realm}", '
                    f'nonce="{nonce}", uri="{uri}", response="{rsp}"')
        elif auth_val.lower().startswith("basic"):
            return "Basic " + base64.b64encode(
                f"{username}:{password}".encode()).decode()
        return None

    def _parse_track_url(sdp: str, base_url: str) -> str | None:
        """Identical to probe_rtsp_socket's _parse_track_url."""
        in_video        = False
        video_control   = None
        session_control = None
        for raw in sdp.split("\n"):
            line = raw.rstrip("\r").strip()
            if line.startswith("m="):
                if in_video:
                    break
                in_video = line.startswith("m=video")
            elif line.startswith("a=control:"):
                ctrl = line[len("a=control:"):].strip()
                if in_video:
                    video_control = ctrl
                else:
                    session_control = ctrl
        ctrl = video_control or session_control
        if not ctrl:
            return None
        if ctrl == "*":
            return base_url
        if ctrl.lower().startswith("rtsp://"):
            return ctrl
        return base_url.rstrip("/") + "/" + ctrl

    sock      = None
    next_cseq = 1
    # auth_val is captured from first 401 and reused across all paths on
    # this socket. Per RFC 2617, nonce reuse is allowed; we just recompute
    # the response digest for each method+uri pair.
    auth_val: str | None = None

    # 2.4.0-rc2.4: early-bail counter for consecutive same-realm 401s.
    # When auth is required at the server level (RFC 7235 §2.2 — realm
    # is server-scoped, not URL-scoped), every path on the same socket
    # will get the same 401 with the same realm. Walking 25-31 paths
    # to confirm what we already know wastes ~5s per port. After 5
    # consecutive same-realm 401s we bail and save state for the
    # Deep Re-Probe button. The counter resets on any non-401 response
    # OR a 401 with a DIFFERENT realm (rare but possible — some
    # firmwares scope auth per-resource).
    consecutive_same_realm_401s: int = 0
    early_bail_realm: str = ""
    EARLY_BAIL_THRESHOLD: int = 5
    bailed_early: bool = False
    paths_tried_count: int = 0

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)

        def roundtrip(method: str, cseq: int,
                      extra: dict | None = None,
                      uri: str = "") -> str:
            """Send RTSP request on the persistent sock, read full response
            (headers + body if Content-Length present). Returns response text.
            Raises socket exceptions if the connection dies — caller must
            decide whether to bail."""
            extra = extra or {}
            target = uri
            hdr = "".join(k + ": " + v + CRLF for k, v in extra.items())
            req = method + " " + target + " RTSP/1.0" + CRLF
            req += "CSeq: " + str(cseq) + CRLF + hdr + CRLF
            sock.sendall(req.encode())
            buf = b""
            while CRLFCRLF.encode() not in buf and len(buf) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            text = buf.decode("utf-8", errors="replace")
            cl = 0
            for line in text.split(CRLF):
                if line.lower().startswith("content-length:"):
                    try:
                        cl = int(line.split(":", 1)[1].strip())
                    except (ValueError, IndexError):
                        cl = 0
                    break
            if cl > 0:
                sep_idx   = text.find(CRLFCRLF)
                already   = (len(buf) - (sep_idx + 4)) if sep_idx >= 0 else 0
                remaining = max(0, cl - already)
                while remaining > 0:
                    chunk = sock.recv(min(remaining, 4096))
                    if not chunk:
                        break
                    buf       += chunk
                    remaining -= len(chunk)
            return buf.decode("utf-8", errors="replace")

        for path_idx, path in enumerate(paths):
            full_path = path + extra_query
            rtsp_url = f"rtsp://{host}:{port}{full_path}"
            _log(f"({path_idx+1}/{len(paths)}) trying {full_path}")

            # ── OPTIONS ──────────────────────────────────────────────
            try:
                resp = roundtrip("OPTIONS", next_cseq, uri=rtsp_url)
                next_cseq += 1
            except Exception as e:
                _log(f"OPTIONS exception → bailing single-socket walk: {e}")
                # 2.4.0-rc3.5 ACD: a mid-walk OPTIONS exception means the
                # camera RST/closed the socket before we got a response.
                # Record so that if it happens again on this IP within the
                # ACD window, the per-IP cooldown escalates.
                _record_rst_observation(host)
                return (None, looks_like_rtsp)  # socket dead
            # rc2.1: detect RTSP-server-ness. ANY response starting with
            # "RTSP/" — even 4xx/5xx — confirms the server speaks RTSP.
            # If no path ever produces an RTSP-formatted reply, the
            # orchestrator skips Layer 2 (wrong port / not an RTSP server).
            if resp.startswith("RTSP/"):
                looks_like_rtsp = True
                # rc2.1.1: capture Server header — first one found wins.
                # The Hipcam RealServer firmware family always returns
                # `Server: Hipcam RealServer/V1.0` even on auth-required
                # responses, so this fires for the Microseven case
                # (no mac_vendor available, but Server header reliably
                # identifies the firmware family).
                if not captured_server:
                    for _line in resp.split(CRLF):
                        if _line.lower().startswith("server:"):
                            captured_server = _line.split(":", 1)[1].strip()
                            _log(f"captured Server: {captured_server!r}")
                            break
            if "RTSP/1.0 2" not in resp:
                # 2.5.0-rc1.1: when OPTIONS returns 401 with WWW-
                # Authenticate AND we have credentials, don't skip the
                # path — capture the auth challenge into auth_val and
                # fall through to DESCRIBE so the existing DESCRIBE-401
                # retry-with-Digest path can authenticate. Without this
                # fall-through, DVRs/NVRs that enforce auth on OPTIONS
                # itself (Lorex/Dahua DVR-NVR family confirmed; some
                # Dahua IPC variants likely too) silently skip every
                # path during cred-auth because the walker treated
                # 401-on-OPTIONS as "path not present." Hikvision-
                # style firmware avoids the bug by allowing OPTIONS
                # without auth and only enforcing auth on DESCRIBE.
                _is_opts_401 = "RTSP/1.0 401" in resp
                _has_creds   = bool(username or password)
                if _is_opts_401 and _has_creds:
                    if not auth_val:
                        _opts_auth_line = next(
                            (l for l in resp.splitlines()
                             if l.lower().startswith("www-authenticate:")),
                            "")
                        auth_val = (
                            _opts_auth_line.split(":", 1)[-1].strip()
                            if _opts_auth_line else "")
                    if auth_val:
                        _log(f"OPTIONS → 401 with auth challenge captured "
                             f"— falling through to DESCRIBE for {path}")
                        # Don't `continue` — fall through to DESCRIBE.
                        # DESCRIBE will also 401, then auth-retry uses
                        # the auth_val we just captured.
                    else:
                        _log(f"OPTIONS → 401 with no WWW-Authenticate "
                             f"— skipping path")
                        consecutive_same_realm_401s = 0
                        continue
                else:
                    _log(f"OPTIONS → {resp.split(CRLF)[0].strip()!r} "
                         f"— skipping path")
                    # 2.4.0-rc2.4: reset early-bail counter — non-401
                    # response means this isn't a same-realm-401 streak.
                    consecutive_same_realm_401s = 0
                    # Path-level rejection: try next path, socket likely still alive
                    continue

            # ── DESCRIBE (with optional 401-retry) ───────────────────
            try:
                resp = roundtrip("DESCRIBE", next_cseq,
                                 {"Accept": "application/sdp"},
                                 uri=rtsp_url)
                next_cseq += 1
            except Exception as e:
                _log(f"DESCRIBE exception → bailing: {e}")
                return (None, looks_like_rtsp)
            if "RTSP/1.0 200" in resp:
                # 2.4.0-rc2.4: reset early-bail counter — got a 200,
                # streak of consecutive same-realm 401s is broken.
                consecutive_same_realm_401s = 0
                pass  # continue to SDP parse
            elif "401" in resp:
                # 2.4.0-rc2.4: extract realm for the early-bail
                # counter. Need to do this regardless of whether
                # collect_locked is enabled, because the counter
                # decision is independent.
                _401_realm = ""
                _auth_line_global = next(
                    (l for l in resp.splitlines()
                     if l.lower().startswith("www-authenticate:")), "")
                if _auth_line_global:
                    _auth_v_global = _auth_line_global.split(":", 1)[-1].strip()
                    _realm_m = re.search(r'realm="([^"]*)"', _auth_v_global)
                    _401_realm = _realm_m.group(1) if _realm_m else ""

                # 2.4.0-rc2.0: when collect_locked=True AND we already
                # found a working unauth URL, treat this 401 as a
                # locked-stream candidate. Filter by expected_realm so
                # we only surface streams that share the camera's
                # primary auth domain (avoids prompting for creds that
                # won't work). Captures realm from the WWW-Authenticate
                # header before falling through to the existing skip-
                # without-creds path.
                # 2.4.0-rc2.5: in deep_reprobe_mode, eagerly collect
                # locked candidates regardless of whether an unauth
                # working_url was found first. The original rc2.0
                # gating (`collect_locked and found_working_url`)
                # was designed for the discovery scan where surfacing
                # locked candidates only makes sense IF the user can
                # already see ONE stream and is being told there are
                # MORE that need creds. But Deep Re-Probe is an
                # explicit user-driven enumeration on a camera that
                # may have NO unauth streams (e.g. Hikvision);
                # in that case the user wants to see the locked
                # candidates anyway so they can enter creds and
                # unlock them.
                want_collect = collect_locked and not username and (
                    found_working_url or deep_reprobe_mode)
                if want_collect:
                    auth_line = next(
                        (l for l in resp.splitlines()
                         if l.lower().startswith("www-authenticate:")), "")
                    if auth_line:
                        auth_v = auth_line.split(":", 1)[-1].strip()
                        scheme = auth_v.split(None, 1)[0] if auth_v else ""
                        realm_m = re.search(r'realm="([^"]*)"', auth_v)
                        realm = realm_m.group(1) if realm_m else ""
                        # Same-realm filter: only surface 401s that match
                        # the camera's captured rtsp_auth_realm. If
                        # expected_realm is empty (caller didn't supply
                        # one), accept all realms — but the orchestrator
                        # only enables collect_locked when a realm WAS
                        # captured upstream, so this fallback is rare.
                        # 2.4.0-rc2.6: brand-recipe filter. When the
                        # caller has identified the camera's brand and
                        # supplied the brand's known RTSP path list
                        # (brand_recipe_paths), only surface 401s whose
                        # path matches the recipe. Without this filter,
                        # cameras like Hikvision that 401 ALL paths
                        # (including paths that aren't valid streams on
                        # the actual camera, e.g. /cam/realmonitor on a
                        # Hikvision DS-2DE) produce dozens of bogus
                        # locked candidates. With it, the count drops
                        # to the brand's actual stream paths (~6 for
                        # Hikvision DS-2). When brand_recipe_paths is
                        # None, no filter is applied (backward-compat
                        # for callers that don't know the brand).
                        path_matches_recipe = True
                        if brand_recipe_paths:
                            # Match by path-prefix to allow variations
                            # like /Streaming/Channels/101 vs /102/103
                            # all matching /Streaming/Channels/.
                            path_matches_recipe = any(
                                path.startswith(rp.rsplit("/", 1)[0]
                                               + "/")
                                or path == rp
                                or path.split("?")[0] == rp.split("?")[0]
                                for rp in brand_recipe_paths)
                        if (not expected_realm or realm == expected_realm) \
                                and path_matches_recipe:
                            locked_streams.append({
                                "path": path,
                                "realm": realm,
                                "scheme": scheme,
                            })
                            _log(f"LOCKED candidate: {path} (realm={realm!r})")
                        elif not path_matches_recipe:
                            _log(f"DESCRIBE 401 path {path!r} not in "
                                 f"brand recipe — not surfacing as "
                                 f"locked candidate")
                        else:
                            _log(f"DESCRIBE 401 different realm "
                                 f"(got {realm!r}, expected "
                                 f"{expected_realm!r}) — not surfacing")
                if not username:
                    _log(f"DESCRIBE → 401 (no creds) — skipping {path}")
                    # 2.4.0-rc2.4: early-bail counter. Track consecutive
                    # 401s that share the same realm. After N hits, we
                    # have high confidence that all remaining paths will
                    # also 401 with the same realm (auth is enforced at
                    # the server level per RFC 7235 §2.2). Bail out and
                    # save state so the Deep Re-Probe button can resume
                    # the walk on demand.
                    if early_bail_realm == "" and _401_realm:
                        early_bail_realm = _401_realm
                    if _401_realm and _401_realm == early_bail_realm:
                        consecutive_same_realm_401s += 1
                    else:
                        # Different realm OR no realm — reset counter
                        consecutive_same_realm_401s = 0
                        if _401_realm:
                            early_bail_realm = _401_realm
                            consecutive_same_realm_401s = 1
                    paths_tried_count = path_idx + 1
                    if consecutive_same_realm_401s >= EARLY_BAIL_THRESHOLD:
                        # 2.4.0-rc2.5: in deep_reprobe_mode the user
                        # explicitly wants to walk every path —
                        # don't bail out, just keep going so all
                        # remaining 401s get surfaced as locked
                        # candidates. The original rc2.4 bail logic
                        # is the right default for discovery scans
                        # (saves ~5s/camera) but wrong here.
                        if deep_reprobe_mode:
                            _log(f"early-bail threshold reached but "
                                 f"deep_reprobe_mode=True — continuing "
                                 f"to enumerate all locked candidates")
                            continue
                        # 2.4.0-rc2.5: log at INFO unconditionally
                        # (was using _log which downgrades to DEBUG
                        # for unlabeled walks — i.e. all scan-time
                        # walks, hiding evidence that the optimization
                        # was firing during normal scans).
                        log.info(pfx + f" Layer 1 early-bail: "
                                 f"{EARLY_BAIL_THRESHOLD} consecutive "
                                 f"401s with realm={early_bail_realm!r} "
                                 f"— remaining {len(paths) - paths_tried_count} "
                                 f"path(s) will likely also 401; saving "
                                 f"state for Deep Re-Probe")
                        bailed_early = True
                        break
                    continue
                # Capture auth_val from this 401 if we haven't already
                if not auth_val:
                    auth_line = next(
                        (l for l in resp.splitlines()
                         if l.lower().startswith("www-authenticate:")), "")
                    auth_val = auth_line.split(":", 1)[-1].strip() if auth_line else ""
                if not auth_val:
                    _log("DESCRIBE → 401 with no WWW-Authenticate — skipping")
                    continue
                auth_hdr = _build_auth(auth_val, "DESCRIBE", rtsp_url)
                if not auth_hdr:
                    _log(f"DESCRIBE → unparseable auth: {auth_val[:60]!r}")
                    continue
                try:
                    resp = roundtrip("DESCRIBE", next_cseq,
                                     {"Accept": "application/sdp",
                                      "Authorization": auth_hdr},
                                     uri=rtsp_url)
                    next_cseq += 1
                except Exception as e:
                    _log(f"DESCRIBE-auth exception → bailing: {e}")
                    return (None, looks_like_rtsp)
                if "RTSP/1.0 200" not in resp:
                    _log(f"DESCRIBE-auth → {resp.split(CRLF)[0].strip()!r}")
                    # 2.5.0-rc1.0: auth_attempt_lockout policy. For
                    # brands whose throttle is a per-IP failed-auth
                    # counter (Lorex/Dahua DVR-NVR family: 10 failed
                    # attempts then ~30 min lockout or until power-
                    # cycle), continuing the walk after a Digest-auth-
                    # rejected response burns additional attempts on
                    # credentials we already know are wrong. Stop the
                    # entire walk after the first auth rejection so
                    # the user surfaces a clean "credentials wrong"
                    # failure with one used attempt. Only fires when
                    # host_meta plumbed the throttle type through
                    # (find_rtsp_path does this when the brand entry
                    # is matched). Sets a flag on host_meta so caller
                    # (cred-auth) can surface a counter-aware message.
                    _walker_throttle = ""
                    if host_meta:
                        _walker_throttle = str(host_meta.get(
                            "walker_throttle_type", "") or "")
                    if _walker_throttle == "auth_attempt_lockout":
                        _log(f"auth_attempt_lockout brand — bailing walk "
                             f"after first auth rejection (preserves "
                             f"remaining attempts before camera lockout)")
                        if host_meta is not None:
                            host_meta["walker_auth_lockout_bailed"] = True
                        return (None, looks_like_rtsp)
                    continue
            else:
                _log(f"DESCRIBE → {resp.split(CRLF)[0].strip()!r} — skipping")
                # 2.4.0-rc2.4: reset early-bail counter — non-401
                # response means this isn't a same-realm-401 streak.
                consecutive_same_realm_401s = 0
                continue

            # ── Parse SDP for first m=video track URL ───────────────
            body_idx  = resp.find(CRLFCRLF)
            sdp_text  = resp[body_idx + 4:] if body_idx >= 0 else ""
            track_url = _parse_track_url(sdp_text, rtsp_url)
            if not track_url:
                _log(f"DESCRIBE 200 but SDP has no m=video — skipping {path}")
                continue

            # 2.5.0-rc1.0: stricter populated-channel test when the brand
            # entry's streaming_recipe directs us to use it. DVR/NVR
            # devices commonly return 200 OK with valid-looking SDP on
            # channels that have NO physical camera connected — the
            # `m=video` line is present but no `a=rtpmap` codec mapping
            # follows, indicating a virtual/empty stream slot. Without
            # this filter, the walker would surface the first 200-OK
            # channel as the working stream and the user would see a
            # blank or frozen card. host_meta-driven so unaffected
            # (single-camera, IP-camera) probes get the original behavior.
            _populated_test = ""
            if host_meta:
                _populated_test = str(host_meta.get(
                    "walker_populated_channel_test", "") or "")
            if _populated_test == "sdp_has_video_track":
                if not _sdp_has_video_track(sdp_text):
                    _log(f"DESCRIBE 200 but SDP failed populated-channel "
                         f"test (no real video codec rtpmap) — likely "
                         f"empty channel; skipping {path}")
                    continue

            # ── SETUP: TCP-interleaved first, UDP fallback ──────────
            transports = [
                ("RTP/AVP/TCP;unicast;interleaved=0-1",            "TCP"),
                ("RTP/AVP/UDP;unicast;client_port=50000-50001",    "UDP"),
            ]
            setup_ok = False
            session  = ""
            for transport_hdr, tlabel in transports:
                extra: dict[str, str] = {"Transport": transport_hdr}
                if auth_val:
                    ah = _build_auth(auth_val, "SETUP", track_url)
                    if ah:
                        extra["Authorization"] = ah
                try:
                    resp = roundtrip("SETUP", next_cseq, extra, uri=track_url)
                    next_cseq += 1
                except Exception as e:
                    _log(f"SETUP exception → bailing: {e}")
                    return (None, looks_like_rtsp)
                if "RTSP/1.0 200" in resp:
                    for line in resp.split(CRLF):
                        if line.lower().startswith("session:"):
                            session = (line.split(":", 1)[1]
                                           .strip().split(";")[0].strip())
                            break
                    _log(f"SETUP ({tlabel}) → 200 OK at {path}")
                    setup_ok = True
                    break
                _log(f"SETUP ({tlabel}) → {resp.split(CRLF)[0].strip()!r}")

            if not setup_ok:
                # SETUP failed for this path — the socket is still alive
                # (server replied with status), so we can try the next path.
                # No TEARDOWN needed since SETUP didn't succeed.
                continue

            # ── TEARDOWN — release session before returning ─────────
            td_extra: dict[str, str] = {}
            if session:
                td_extra["Session"] = session
            if auth_val:
                ah = _build_auth(auth_val, "TEARDOWN", track_url)
                if ah:
                    td_extra["Authorization"] = ah
            try:
                roundtrip("TEARDOWN", next_cseq, td_extra, uri=track_url)
                next_cseq += 1
            except Exception:
                pass  # TEARDOWN failure is non-fatal — socket close releases state

            # 2.4.0-rc2.0 (Layered Stream Discovery): if collect_locked
            # is on AND we haven't yet recorded the first working URL,
            # capture it and continue walking. Subsequent paths get
            # OPTIONS+DESCRIBE only; their SETUPs aren't run because
            # we've already proven a working stream and don't need to
            # waste another SETUP/TEARDOWN cycle to enumerate locked
            # candidates. For 2.4.0-rc2.0 the extension is conservative
            # — we only collect 401-locked paths from continued
            # DESCRIBEs after this point. SETUP-tested confirmation of
            # a *second* working unauth URL would be a future extension
            # (it isn't useful for current UX since we only need ONE
            # working URL per camera).
            if collect_locked and found_working_url is None:
                found_working_url = rtsp_url
                _log(f"continuing walk to collect locked candidates "
                     f"after first success: {rtsp_url}")
                continue
            # Normal mode (or second+ success in collect mode): bail
            return (rtsp_url, True)

        # Walked every path. In collect mode, return the first working
        # URL we found (could be None if nothing worked).
        if collect_locked and found_working_url:
            return (found_working_url, True)

        # Walked every path without finding a streamable track
        return (None, looks_like_rtsp)

    except Exception as e:
        _log(f"single-socket walk exception: {e}")
        return (None, looks_like_rtsp)
    finally:
        # rc2.1.1: persist captured Server header to host_meta on EVERY
        # return path (success, no-match, exception, socket-dead). The
        # orchestrator re-runs brand-id post-walk using this signal,
        # which is critical for cameras like the Microseven whose
        # mac_vendor isn't available (camera not in nmap_results due to
        # its own TCP rate-limit defeating the focused port scan).
        if host_meta is not None and captured_server:
            host_meta["server_header"] = captured_server
        # 2.4.0-rc2.0: persist locked_streams to host_meta. We always
        # write the list (even when empty) so downstream readers can
        # distinguish "feature ran, found nothing" from "feature didn't
        # run". The UI badge is shown only when len > 0 AND the camera
        # has no saved creds.
        if host_meta is not None and collect_locked:
            host_meta["locked_streams"] = locked_streams
        # 2.4.0-rc2.4: persist early-bail state. When Layer 1 bailed
        # after EARLY_BAIL_THRESHOLD consecutive same-realm 401s, save
        # what we tried + what's remaining onto host_meta so the
        # Deep Re-Probe button can resume the walk on demand without
        # re-walking what we already know will 401.
        if host_meta is not None and bailed_early:
            host_meta["early_bail_reason"] = "layer1_consecutive_401s"
            host_meta["early_bail_realm"] = early_bail_realm
            host_meta["early_bail_paths_tried"] = list(
                paths[:paths_tried_count])
            host_meta["early_bail_paths_remaining"] = list(
                paths[paths_tried_count:])
            host_meta["early_bail_at"] = datetime.datetime.utcnow().isoformat()
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def _validate_rtsp_urls_single_socket(
    host: str,
    port: int,
    urls: list[str],
    username: str = "",
    password: str = "",
    timeout: float = 6.0,
    host_meta: dict | None = None,
    label: str = "",
) -> dict:
    """2.3.0: Validate a list of FULL RTSP URLs over a SINGLE TCP socket.

    Sibling to _probe_rtsp_paths_single_socket but with two key
    differences:
      • Walks the ENTIRE list (no early return on first match) and
        returns a dict mapping each URL → bool (probe_ok).
      • Designed for AFTER cred-auth: callers already have working
        credentials from the user. We capture the auth challenge from
        the first 401 (if any) and reuse the nonce across all URLs
        per RFC 2617.

    Used by the cred-auth handler to validate all ONVIF profile stream
    URLs (and any STREAM_DB-supplemental URLs) over ONE TCP connection
    instead of opening N sockets in rapid sequence — which is what
    triggered the Microseven firmware-level lockout in rc2.x.

    Mechanics — same as the discovery walker:
      • Full Content-Length-bounded reads before next request (no
        pipelining — universal-safe per RFC 2326 §9.1).
      • OPTIONS → DESCRIBE (with optional 401 retry) → SETUP → TEARDOWN
        per URL. SETUP confirms the server can stream the track.
      • TCP-interleaved transport tried first, UDP fallback second.
      • Captures Server: header into host_meta on first response.

    Returns: dict {url: probe_ok}. URLs are validated in order; if the
    socket dies mid-way, all remaining URLs return False (caller can
    retry on a fresh socket if it cares — most callers don't, since
    the dead socket itself indicates a broken stream).

    Empty input → returns {}.
    """
    if not urls:
        return {}

    results: dict = {url: False for url in urls}
    captured_server: str = ""

    pfx = f"  [validate_rtsp_walk {label or host + ':' + str(port)}]"

    def _log(msg: str) -> None:
        if label:
            log.info(pfx + " " + msg)
        else:
            log.debug(pfx + " " + msg)

    CRLF     = chr(13) + chr(10)
    CRLFCRLF = CRLF + CRLF

    def _build_auth(auth_val: str, method: str, uri: str) -> str | None:
        """Identical to _probe_rtsp_paths_single_socket._build_auth — RFC
        2617 nonce reuse, qop=auth handling for Hikvision-style challenges."""
        if auth_val.lower().startswith("digest"):
            realm_m = re.search(r'realm="([^"]*)"', auth_val)
            nonce_m = re.search(r'nonce="([^"]*)"', auth_val)
            if not (realm_m and nonce_m):
                return None
            realm, nonce = realm_m.group(1), nonce_m.group(1)
            qop_m = re.search(r'qop="?([^",]+)"?', auth_val)
            ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
            ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
            if qop_m:
                qop_val = qop_m.group(1).strip()
                qop = "auth" if "auth" in qop_val else qop_val.split(",")[0].strip()
                cnonce = os.urandom(8).hex()
                nc = "00000001"
                rsp = hashlib.md5(
                    f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
                ).hexdigest()
                return (f'Digest username="{username}", realm="{realm}", '
                        f'nonce="{nonce}", uri="{uri}", '
                        f'qop={qop}, nc={nc}, cnonce="{cnonce}", '
                        f'response="{rsp}"')
            rsp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            return (f'Digest username="{username}", realm="{realm}", '
                    f'nonce="{nonce}", uri="{uri}", response="{rsp}"')
        elif auth_val.lower().startswith("basic"):
            return "Basic " + base64.b64encode(
                f"{username}:{password}".encode()).decode()
        return None

    def _parse_track_url(sdp: str, base_url: str) -> str | None:
        """Identical to _probe_rtsp_paths_single_socket._parse_track_url."""
        in_video        = False
        video_control   = None
        session_control = None
        for raw in sdp.split("\n"):
            line = raw.rstrip("\r").strip()
            if line.startswith("m="):
                if in_video:
                    break
                in_video = line.startswith("m=video")
            elif line.startswith("a=control:"):
                ctrl = line[len("a=control:"):].strip()
                if in_video:
                    video_control = ctrl
                else:
                    session_control = ctrl
        ctrl = video_control or session_control
        if not ctrl:
            return None
        if ctrl == "*":
            return base_url
        if ctrl.lower().startswith("rtsp://"):
            return ctrl
        return base_url.rstrip("/") + "/" + ctrl

    sock      = None
    next_cseq = 1
    auth_val: str | None = None

    # 2.3.2 defense-in-depth: warn if the URLs being walked don't actually
    # point to the host:port we're about to connect to. The 2.3.0+ caller
    # in api_set_credentials was passing a wrong port for two minor
    # versions before this was caught — this check makes the same class
    # of misuse visible immediately in any future caller's logs.
    if urls:
        try:
            first_parsed = urlparse(urls[0])
            url_host = first_parsed.hostname
            url_port = first_parsed.port or 554
            if url_host and url_host != host:
                log.warning(f"  [{label or 'validator'}] URL host "
                            f"{url_host!r} != socket host {host!r} — "
                            f"validator will likely fail (caller bug)")
            if url_port != port:
                log.warning(f"  [{label or 'validator'}] URL port "
                            f"{url_port} != socket port {port} — "
                            f"validator will likely fail (caller bug)")
        except (ValueError, AttributeError):
            pass  # malformed URL — surfaces below as parse/probe failure

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)

        def roundtrip(method: str, cseq: int,
                      extra: dict | None = None,
                      uri: str = "") -> str:
            extra = extra or {}
            hdr = "".join(k + ": " + v + CRLF for k, v in extra.items())
            req = method + " " + uri + " RTSP/1.0" + CRLF
            req += "CSeq: " + str(cseq) + CRLF + hdr + CRLF
            sock.sendall(req.encode())
            buf = b""
            while CRLFCRLF.encode() not in buf and len(buf) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            text = buf.decode("utf-8", errors="replace")
            cl = 0
            for line in text.split(CRLF):
                if line.lower().startswith("content-length:"):
                    try:
                        cl = int(line.split(":", 1)[1].strip())
                    except (ValueError, IndexError):
                        cl = 0
                    break
            if cl > 0:
                sep_idx   = text.find(CRLFCRLF)
                already   = (len(buf) - (sep_idx + 4)) if sep_idx >= 0 else 0
                remaining = max(0, cl - already)
                while remaining > 0:
                    chunk = sock.recv(min(remaining, 4096))
                    if not chunk:
                        break
                    buf       += chunk
                    remaining -= len(chunk)
            return buf.decode("utf-8", errors="replace")

        for url_idx, rtsp_url in enumerate(urls):
            _log(f"({url_idx+1}/{len(urls)}) validating {_strip_creds(rtsp_url)}")

            # ── OPTIONS ──────────────────────────────────────────────
            try:
                resp = roundtrip("OPTIONS", next_cseq, uri=rtsp_url)
                next_cseq += 1
            except Exception as e:
                _log(f"OPTIONS exception → bailing remaining: {e}")
                # 2.4.0-rc3.5 ACD: see _probe_rtsp_paths_single_socket.
                # 2.5.0-rc1.4: callers can suppress ACD recording via
                # `walker_skip_acd` on host_meta. Used by the channel
                # enumeration helper, which loops over URLs one socket
                # at a time on the Lorex/Dahua family — a firmware
                # quirk where the DVR closes the socket after each
                # full authenticated transaction. Without the suppress
                # flag, every per-URL walk that completes successfully
                # but hits a server-side close on the next OPTIONS
                # would record an RST event, and 2 events in <60s
                # trigger ACD escalation. That's spurious for the
                # known socket-close-per-URL behavior of this brand.
                _skip_acd = bool(host_meta and host_meta.get(
                    "walker_skip_acd"))
                if not _skip_acd:
                    _record_rst_observation(host)
                return results  # remaining urls stay False
            if resp.startswith("RTSP/") and not captured_server:
                for _line in resp.split(CRLF):
                    if _line.lower().startswith("server:"):
                        captured_server = _line.split(":", 1)[1].strip()
                        _log(f"captured Server: {captured_server!r}")
                        break
            if "RTSP/1.0 2" not in resp:
                # 2.5.0-rc1.2: same OPTIONS-401-fall-through fix that
                # _probe_rtsp_paths_single_socket got in 2.5.0-rc1.1.
                # When OPTIONS returns 401 with WWW-Authenticate AND we
                # have credentials, capture the auth challenge into
                # auth_val and DON'T skip — fall through to DESCRIBE
                # so the existing DESCRIBE-401-retry-with-Digest path
                # can authenticate. The discovery walker had this fix
                # in rc1.1 but the validate walker (used to confirm
                # sub-stream URLs after the discovery walker found a
                # working main stream) was missed. Symptom on the Lorex DVR
                # rc1.1 field log: db_probe walked sub-stream URLs,
                # OPTIONS-401'd on each, skipped — `stream_profiles
                # built 1 entry/entries (1 main + 0 sub + 0 validated
                # locked)` even though the sub-stream URLs were valid.
                _is_opts_401 = "RTSP/1.0 401" in resp
                _has_creds   = bool(username or password)
                if _is_opts_401 and _has_creds:
                    if not auth_val:
                        _opts_auth_line = next(
                            (l for l in resp.splitlines()
                             if l.lower().startswith("www-authenticate:")),
                            "")
                        auth_val = (
                            _opts_auth_line.split(":", 1)[-1].strip()
                            if _opts_auth_line else "")
                    if auth_val:
                        _log(f"OPTIONS → 401 with auth challenge captured "
                             f"— falling through to DESCRIBE for {_strip_creds(rtsp_url)}")
                        # Fall through to DESCRIBE; auth retry uses
                        # the captured auth_val.
                    else:
                        _log(f"OPTIONS → 401 with no WWW-Authenticate "
                             f"— skipping URL")
                        continue
                else:
                    _log(f"OPTIONS → {resp.split(CRLF)[0].strip()!r} "
                         f"— skipping URL")
                    continue

            # ── DESCRIBE (with optional 401 retry) ───────────────────
            try:
                # If we already captured auth_val from a prior URL's 401,
                # send it pre-emptively to avoid a second roundtrip.
                describe_extra: dict = {"Accept": "application/sdp"}
                if auth_val:
                    ah = _build_auth(auth_val, "DESCRIBE", rtsp_url)
                    if ah:
                        describe_extra["Authorization"] = ah
                resp = roundtrip("DESCRIBE", next_cseq, describe_extra,
                                 uri=rtsp_url)
                next_cseq += 1
            except Exception as e:
                _log(f"DESCRIBE exception → bailing: {e}")
                return results
            if "RTSP/1.0 200" in resp:
                pass
            elif "401" in resp:
                if not username:
                    _log(f"DESCRIBE → 401 (no creds) — skipping {_strip_creds(rtsp_url)}")
                    continue
                if not auth_val:
                    auth_line = next(
                        (l for l in resp.splitlines()
                         if l.lower().startswith("www-authenticate:")), "")
                    auth_val = auth_line.split(":", 1)[-1].strip() if auth_line else ""
                if not auth_val:
                    _log("DESCRIBE → 401 with no WWW-Authenticate — skipping")
                    continue
                auth_hdr = _build_auth(auth_val, "DESCRIBE", rtsp_url)
                if not auth_hdr:
                    _log(f"DESCRIBE → unparseable auth: {auth_val[:60]!r}")
                    continue
                try:
                    resp = roundtrip("DESCRIBE", next_cseq,
                                     {"Accept": "application/sdp",
                                      "Authorization": auth_hdr},
                                     uri=rtsp_url)
                    next_cseq += 1
                except Exception as e:
                    _log(f"DESCRIBE-auth exception → bailing: {e}")
                    return results
                if "RTSP/1.0 200" not in resp:
                    _log(f"DESCRIBE-auth → {resp.split(CRLF)[0].strip()!r}")
                    continue
            else:
                _log(f"DESCRIBE → {resp.split(CRLF)[0].strip()!r} — skipping")
                continue

            # ── Parse SDP for first m=video track URL ───────────────
            body_idx  = resp.find(CRLFCRLF)
            sdp_text  = resp[body_idx + 4:] if body_idx >= 0 else ""
            track_url = _parse_track_url(sdp_text, rtsp_url)
            if not track_url:
                _log(f"DESCRIBE 200 but SDP has no m=video — skipping {_strip_creds(rtsp_url)}")
                continue

            # 2.5.0-rc1.2: stricter populated-channel test, used by the
            # post-cred-auth channel-enumeration helper to filter out
            # virtual/empty DVR channel slots that return SDP with
            # `m=video` but no real codec rtpmap. Same heuristic the
            # discovery walker has had since 2.5.0-rc1.0; ported here
            # so post-auth multi-card surfacing on channel_iterate
            # brands skips empty channels rather than registering
            # cards for them. host_meta-driven so probes that don't
            # opt in (single-camera ONVIF profile validation) get the
            # original behaviour.
            _populated_test = ""
            if host_meta:
                _populated_test = str(host_meta.get(
                    "walker_populated_channel_test", "") or "")
            if _populated_test == "sdp_has_video_track":
                if not _sdp_has_video_track(sdp_text):
                    _log(f"DESCRIBE 200 but SDP failed populated-channel "
                         f"test (no real video codec rtpmap) — likely "
                         f"empty channel; skipping {rtsp_url}")
                    continue

            # ── SETUP: TCP-interleaved first, UDP fallback ──────────
            transports = [
                ("RTP/AVP/TCP;unicast;interleaved=0-1",            "TCP"),
                ("RTP/AVP/UDP;unicast;client_port=50000-50001",    "UDP"),
            ]
            setup_ok = False
            session  = ""
            for transport_hdr, tlabel in transports:
                extra: dict = {"Transport": transport_hdr}
                if auth_val:
                    ah = _build_auth(auth_val, "SETUP", track_url)
                    if ah:
                        extra["Authorization"] = ah
                try:
                    resp = roundtrip("SETUP", next_cseq, extra, uri=track_url)
                    next_cseq += 1
                except Exception as e:
                    _log(f"SETUP exception → bailing: {e}")
                    return results
                if "RTSP/1.0 200" in resp:
                    for line in resp.split(CRLF):
                        if line.lower().startswith("session:"):
                            session = (line.split(":", 1)[1]
                                           .strip().split(";")[0].strip())
                            break
                    _log(f"SETUP ({tlabel}) → 200 OK")
                    setup_ok = True
                    break
                _log(f"SETUP ({tlabel}) → {resp.split(CRLF)[0].strip()!r}")

            if not setup_ok:
                continue

            # ── TEARDOWN — release session before next URL ──────────
            td_extra: dict = {}
            if session:
                td_extra["Session"] = session
            if auth_val:
                ah = _build_auth(auth_val, "TEARDOWN", track_url)
                if ah:
                    td_extra["Authorization"] = ah
            try:
                roundtrip("TEARDOWN", next_cseq, td_extra, uri=track_url)
                next_cseq += 1
            except Exception:
                pass  # TEARDOWN failure is non-fatal

            results[rtsp_url] = True
            _log(f"  → probe_ok=True for {_strip_creds(rtsp_url)}")

        return results

    except Exception as e:
        _log(f"validate-all walk exception: {e}")
        return results
    finally:
        if host_meta is not None and captured_server:
            host_meta["server_header"] = captured_server
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def find_rtsp_path(ip: str, port: int,
                   username: str = "", password: str = "",
                   host_meta: dict | None = None) -> str | None:
    """rc2 Two-layer RTSP path probe with brand-aware short-circuits.

    Layer 1 — Single-socket walk through all paths (always tried first
    unless the brand has throttle_type='no_rtsp_support').

    Layer 2 — Multi-socket fallback with 5s delay between attempts and
    bail-after-10 consecutive failures. Skipped for brands with
    throttle_type='rate_limit_per_ip_tcp' (Hipcam/Microseven family) —
    multi-socket would be RST'd before it could complete.

    host_meta — optional camera dict (mac_vendor, hostname, page_title,
    etc.) used to identify the brand BEFORE the probe begins. Without it,
    the function falls back to brand-agnostic two-layer behavior.
    """
    # ── Brand identification — first pass (uses signals already in
    #    host_meta: mac_vendor, page_title, server_header, nmap_product,
    #    onvif_scopes, hostname, verdict_reason). rc2.1.1: this is a
    #    DIRECT call to _identify_camera_brand which mutates host_meta in
    #    place, writing host_meta["manufacturer"] when a brand is found.
    #    Caller (e.g. ONVIF post-scan loop) reads it back after we return.
    #    Replaces rc2's _get_brand_throttle_info() which made a dict copy
    #    and silently dropped the manufacturer assignment.
    brand_entry: dict | None = None
    throttle_type: str = ""
    if host_meta is not None:
        try:
            brand_entry = _identify_camera_brand(host_meta)
        except Exception as e:
            log.debug(f"  brand-id (pre-probe): {e}")
        if brand_entry:
            throttle_type = str(brand_entry.get("throttle_type", "") or "")
    brand_name = (brand_entry or {}).get("name", "")

    # Cloud-only brands: skip RTSP entirely
    if throttle_type == "no_rtsp_support":
        log.info(f"  RTSP skipped: {brand_name} does not support RTSP "
                 f"(throttle_type=no_rtsp_support)")
        return None

    # Adjust per-socket timeout for sleeping/slow-wakeup brands
    sock_timeout: float = 6.0
    if throttle_type == "session_time_cap":
        sock_timeout = 25.0  # Reolink battery-WiFi wake-up window
        log.info(f"  RTSP timeout extended to {sock_timeout}s "
                 f"({brand_name}, session_time_cap)")

    # Compose path priority — brand-specific paths from STREAM_DB first,
    # then the universal RTSP_PATHS, deduped while preserving order.
    db_paths: list[str] = []
    if host_meta is not None and brand_name:
        cam_with_brand = dict(host_meta)
        cam_with_brand["manufacturer"] = brand_name
        sdb = _match_stream_db(cam_with_brand)
        if sdb:
            db_paths = list(sdb.get("rtsp", []))

    # 2.5.0-rc1.0: streaming_recipe consumer. Brands with
    # `streaming_recipe.type == "channel_iterate"` (Lorex/Dahua DVR-NVR
    # family, Hikvision NVR, Uniview NVR, Dahua direct, Amcrest, etc.)
    # need DVR-channel-specific paths, not the universal single-camera
    # paths. Expand the recipe and prepend it so channel iteration
    # happens BEFORE generic fallbacks. The fallback paths from the
    # recipe (legacy firmware URLs) come last in the recipe list itself,
    # see _expand_channel_iterate_paths.
    recipe_paths: list[str] = []
    recipe = (brand_entry or {}).get("streaming_recipe") or {}
    if recipe.get("type") == "channel_iterate":
        recipe_paths = _expand_channel_iterate_paths(recipe, channel_cap=16)
        if recipe_paths:
            log.info(f"  RTSP path list: brand={brand_name} streaming_recipe "
                     f"channel_iterate expanded to {len(recipe_paths)} paths "
                     f"(channels capped at 16)")
            # Tell the walker this is a channel-iteration walk so it can
            # apply the populated-channel SDP heuristic and the
            # auth_attempt_lockout bail-on-first-failure policy if either
            # is configured on the brand entry.
            if host_meta is not None:
                host_meta["walker_streaming_recipe_active"] = True
                host_meta["walker_populated_channel_test"] = (
                    recipe.get("populated_channel_test", "")
                )
                host_meta["walker_throttle_type"] = throttle_type

    seen: set[str]   = set()
    ordered: list[str] = []
    for p in recipe_paths + db_paths + RTSP_PATHS:
        if p not in seen:
            seen.add(p)
            ordered.append(p)

    # ── Layer 1: single-socket walk ──────────────────────────────────
    label_for_log = f"{ip}:{port}"
    if brand_name:
        label_for_log += f" ({brand_name})"
    log.info(f"  RTSP probe: Layer 1 (single-socket walk, "
             f"{len(ordered)} paths) — {label_for_log}")

    # 2.4.0-rc2.0 (Layered Stream Discovery): enable locked-stream
    # collection when:
    #  • no credentials are supplied to this call (creds-already-known
    #    means cred-auth flow handles enumeration directly)
    #  • brand is NOT marked skip_layer2 — skip_layer2 brands have known
    #    fragile multi-attempt behavior (per-IP TCP rate-limit, lockout
    #    counters); we don't grind extra DESCRIBEs against them
    #  • host_meta has a captured rtsp_auth_realm — without one we
    #    can't filter by realm; safer to skip collection than surface
    #    locked streams that need different creds
    skip_layer2_brand = bool(
        brand_entry and brand_entry.get("skip_layer2", False)
    )
    captured_realm = ""
    if host_meta is not None:
        captured_realm = str(host_meta.get("rtsp_auth_realm", "") or "").strip()
    enable_locked_collect = bool(
        (not username)
        and (not skip_layer2_brand)
        and captured_realm
    )
    if enable_locked_collect:
        log.info(f"  RTSP probe: Layered Stream Discovery enabled "
                 f"(realm={captured_realm!r})")

    found, looks_like_rtsp = _probe_rtsp_paths_single_socket(
        ip, port, ordered, username, password,
        timeout=sock_timeout, label="", host_meta=host_meta,
        collect_locked=enable_locked_collect,
        expected_realm=captured_realm,
        # 2.4.0-rc2.6: when we have a brand recipe, pass its paths so
        # the walker can filter locked candidates to paths the brand
        # actually serves. db_paths was already computed above from
        # _match_stream_db. Empty list → no filter (backward-compat).
        brand_recipe_paths=db_paths,
    )
    # rc2.1.1: re-run brand identification after the walk. The walker
    # captured the RTSP Server: header into host_meta["server_header"],
    # which is a strong identification signal especially for cameras
    # whose mac_vendor was unavailable (e.g. Microseven — its TCP
    # rate-limit prevented inclusion in nmap_results, so OUI lookup
    # had nothing to feed). The Hipcam RealServer firmware family
    # always returns `Server: Hipcam RealServer/V1.0` which matches
    # the http_headers field in the Hipcam/Microseven CAMERA_DB entry.
    if host_meta is not None and not brand_name and host_meta.get("server_header"):
        try:
            re_id = _identify_camera_brand(host_meta, force=True)
            if re_id and re_id["name"] != "Generic IP Camera":
                brand_entry = re_id
                brand_name = re_id["name"]
                throttle_type = str(re_id.get("throttle_type", "") or "")
                log.info(f"  Brand identified post-walk: {brand_name} "
                         f"(via Server header: "
                         f"{host_meta.get('server_header','')!r})")
        except Exception as e:
            log.debug(f"  brand-id (post-walk): {e}")
    if found:
        log.info(f"  RTSP OK (Layer 1): {found}")
        return found

    # ── Layer 1 fallback: Axis Companion query-param retry ───────────
    # Only triggers when the brand DB explicitly says this camera needs
    # the query param. Cheap to attempt; one extra single-socket pass.
    if throttle_type == "requires_query_param":
        log.info(f"  RTSP probe: Layer 1 retry with Axis-Orig-Sw=true "
                 f"({brand_name}, requires_query_param)")
        found, lr2 = _probe_rtsp_paths_single_socket(
            ip, port, ordered, username, password,
            timeout=sock_timeout, extra_query="?Axis-Orig-Sw=true",
            label="", host_meta=host_meta,
        )
        looks_like_rtsp = looks_like_rtsp or lr2
        if found:
            log.info(f"  RTSP OK (Layer 1+query): {found}")
            return found

    # ── Layer 2 short-circuit: per-IP TCP rate-limit ─────────────────
    if throttle_type == "rate_limit_per_ip_tcp":
        log.info(f"  RTSP Layer 2 skipped: {brand_name} has per-IP TCP "
                 f"rate-limit (multi-socket would be RST'd)")
        return None

    # ── 2.4.0-rc2.1: Layer 2 short-circuit on skip_layer2 brands ─────
    # Brands with skip_layer2=True are documented as having fragile
    # multi-attempt behavior — typically lockout counters or per-stream
    # session caps that punish repeated DESCRIBE attempts. Lorex/Dahua
    # DVR-NVR family is the canonical example: Layer 2 grinds 50+ seconds
    # through 10 sockets × 5s sleep on a multi-channel DVR where the
    # right answer is "use the streaming_recipe with channel iteration"
    # (consumed in rc3.x), not "try more single-channel paths."
    # 2.4.0-rc2.9: also honor host_meta["host_skip_layer2"], propagated
    # by run_scan when an EARLIER port on this IP triggered the skip.
    # Lorex/Dahua case: port 554 IDs as "Lorex / Dahua DVR-NVR Family"
    # (skip_layer2: True), but port 80 IDs as plain "Lorex" (no
    # skip_layer2). Without inheritance, port 80 would run Layer 2
    # for ~45s wastefully on an IP we already know can't speak it.
    inherited_skip_layer2 = bool(
        host_meta and host_meta.get("host_skip_layer2"))
    if skip_layer2_brand or inherited_skip_layer2:
        if skip_layer2_brand:
            log.info(f"  RTSP Layer 2 skipped: {brand_name} marked "
                     f"skip_layer2 (use streaming_recipe / channel iteration)")
            # Mark host_meta so run_scan can propagate to alt ports
            if host_meta is not None:
                host_meta["brand_skip_layer2"] = True
        else:
            log.info(f"  RTSP Layer 2 skipped: {ip} inherited "
                     f"skip_layer2 from earlier port on this host")
        return None

    # ── 2.4.0-rc2.4: Layer 2 short-circuit on Layer 1 early-bail ────
    # If Layer 1 hit EARLY_BAIL_THRESHOLD consecutive 401s with the
    # same realm and bailed out (state was written to host_meta in
    # _probe_rtsp_paths_single_socket's finally block), Layer 2's
    # multi-socket walk will get the same 401 with the same realm on
    # every fresh socket — auth is enforced server-side, not socket-
    # side, per RFC 7235 §2.2. Skip Layer 2 immediately and let the
    # camera surface as needs_credentials. Users who want to verify
    # against firmware-quirk cases (5% chance Layer 2 reveals
    # something Layer 1 missed) can hit the per-card "Deep Re-Probe"
    # button which re-runs Layer 2 on demand AND resumes the Layer 1
    # walk on the unwalked remaining paths.
    if (host_meta is not None
            and host_meta.get("early_bail_reason") == "layer1_consecutive_401s"):
        realm = host_meta.get("early_bail_realm", "")
        log.info(f"  RTSP Layer 2 skipped: Layer 1 early-bailed after "
                 f"5 consecutive 401s with realm={realm!r} — fresh sockets "
                 f"won't change auth result. Use Deep Re-Probe button to "
                 f"override.")
        # Mark that we ALSO skipped Layer 2 so the Deep Re-Probe button
        # knows to run Layer 2 in addition to resuming Layer 1.
        host_meta["early_bail_reason"] = "layer1_then_layer2_skipped_401s"
        return None

    # ── rc2.1: Layer 2 fast-bail — host doesn't speak RTSP at all ───
    # If Layer 1 walked every path and got NO RTSP-formatted responses
    # (server is HTTP, raw TCP, or otherwise non-RTSP), Layer 2 will
    # waste 50+s grinding through 10 sockets × 5s sleep before its own
    # bail kicks in. Skip it. Common when a Hikvision NVR is probed on
    # port 80 (HTTP) instead of 554 (RTSP) by the cred-relogin flow.
    if not looks_like_rtsp:
        log.info(f"  RTSP Layer 2 skipped: {ip}:{port} did not respond "
                 f"with RTSP format on any path — likely wrong port or "
                 f"non-RTSP service")
        return None

    # ── Layer 2: multi-socket fallback with 5s delay + bail-after-10 ─
    log.info(f"  RTSP probe: Layer 2 (multi-socket fallback, "
             f"5s delay + bail-after-10) — {label_for_log}")
    consecutive_failures = 0
    for i, path in enumerate(ordered):
        if i > 0:
            time.sleep(5.0)   # cooldown between sockets

        url = f"rtsp://{ip}:{port}{path}"
        if probe_rtsp(url, username, password, timeout=sock_timeout):
            log.info(f"  RTSP OK (Layer 2): {url}")
            return url

        # Axis Companion retry-on-failure with query param
        if throttle_type == "requires_query_param":
            url_q = f"rtsp://{ip}:{port}{path}?Axis-Orig-Sw=true"
            if probe_rtsp(url_q, username, password, timeout=sock_timeout):
                log.info(f"  RTSP OK (Layer 2+query): {url_q}")
                return url_q

        consecutive_failures += 1
        if consecutive_failures >= 10:
            log.info(f"  RTSP Layer 2 bailing after "
                     f"{consecutive_failures} consecutive failures")
            break

    return None


def probe_mjpeg_http(ip: str, port: int, username: str = "",
                     password: str = "", timeout: int = 4) -> str | None:
    import urllib.request
    scheme = "https" if port in (443, 8443) else "http"
    auth   = (f"Basic {base64.b64encode(f'{username}:{password}'.encode()).decode()}"
              if username else None)
    for path in MJPEG_PATHS:
        url = f"{scheme}://{ip}:{port}{path}"
        try:
            req = urllib.request.Request(url)
            if auth:
                req.add_header("Authorization", auth)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ct = resp.headers.get("Content-Type", "")
                if any(k in ct.lower() for k in
                       ("multipart/x-mixed-replace", "image/jpeg", "mjpeg", "mjpg")):
                    log.info(f"  MJPEG OK: {url}")
                    return url
        except Exception:
            pass
    return None


def probe_hls(ip: str, port: int, username: str = "",
              password: str = "", timeout: int = 4) -> str | None:
    import urllib.request
    scheme = "https" if port in (443, 8443) else "http"
    auth   = (f"Basic {base64.b64encode(f'{username}:{password}'.encode()).decode()}"
              if username else None)
    for path in HLS_PATHS:
        url = f"{scheme}://{ip}:{port}{path}"
        try:
            req = urllib.request.Request(url)
            if auth:
                req.add_header("Authorization", auth)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ct   = resp.headers.get("Content-Type", "")
                body = resp.read(64).decode("utf-8", errors="replace")
                if ("m3u8" in ct.lower() or "mpegurl" in ct.lower() or
                        body.strip().startswith("#EXTM3U")):
                    log.info(f"  HLS OK: {url}")
                    return url
        except Exception:
            pass
    return None


def probe_rtmp(ip: str, port: int, timeout: int = 3) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            sock.sendall(b"\x03")
            sock.settimeout(timeout)
            data = sock.recv(4)
            return bool(data and data[0] in (0x03, 0x06))
    except Exception:
        return False


def probe_webrtc(ip: str, port: int, timeout: int = 4) -> str | None:
    import urllib.request, urllib.error
    scheme = "https" if port in (443, 8443) else "http"
    sdp_offer = (
        "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
        "m=video 9 UDP/TLS/RTP/SAVPF 96\r\nc=IN IP4 0.0.0.0\r\n"
        "a=sendrecv\r\na=rtpmap:96 H264/90000\r\n"
    )
    for path in WEBRTC_PATHS:
        url = f"{scheme}://{ip}:{port}{path}"
        try:
            req = urllib.request.Request(url, data=sdp_offer.encode(), method="POST")
            req.add_header("Content-Type", "application/sdp")
            req.add_header("Accept",       "application/sdp")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status in (200, 201) and "sdp" in resp.headers.get("Content-Type","").lower():
                    return url
        except urllib.error.HTTPError as e:
            if any(h in e.headers.get("Content-Type","").lower() for h in ("sdp","webrtc","ice")):
                return url
        except Exception:
            pass
    return None


def probe_ws_rtsp(ip: str, port: int, timeout: int = 4) -> str | None:
    scheme_ws = "wss" if port in (443, 8443) else "ws"
    key_b64   = "dGhlIHNhbXBsZSBub25jZQ=="
    for path in WS_RTSP_PATHS:
        try:
            with socket.create_connection((ip, port), timeout=timeout) as sock:
                hs = (
                    f"GET {path} HTTP/1.1\r\nHost: {ip}:{port}\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key_b64}\r\nSec-WebSocket-Version: 13\r\n"
                    "Sec-WebSocket-Protocol: rtsp\r\n\r\n"
                ).encode()
                sock.sendall(hs)
                sock.settimeout(timeout)
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = sock.recv(1024)
                    if not chunk:
                        break
                    buf += chunk
                # 2.4.0-rc2.3: tightened verification. Per RFC 6455 §4.1,
                # the server MUST include the selected subprotocol in
                # `Sec-WebSocket-Protocol: <protocol>` in its 101
                # response if it accepted that subprotocol. A server
                # that returns 101 + "websocket" but does NOT echo
                # `rtsp` as its subprotocol speaks WebSocket but NOT
                # RTSP-over-WebSocket — common false positive case is
                # the Microseven Hipcam family which has a WebSocket
                # endpoint on port 80 for its live web UI MJPEG feed
                # but doesn't tunnel RTSP through it.
                resp = buf.decode("utf-8", errors="replace")
                # Status line check
                first_line = resp.split("\r\n", 1)[0] if resp else ""
                if "101" not in first_line:
                    continue
                # Header parse — case-insensitive lookup
                headers_lower = resp.lower()
                if "upgrade: websocket" not in headers_lower:
                    continue
                # MUST echo our requested rtsp subprotocol — anchored to
                # the actual header so we don't false-positive on the
                # word "rtsp" appearing elsewhere in body/comments.
                # Match: "sec-websocket-protocol: ..." line containing rtsp
                import re as _re_ws
                m = _re_ws.search(
                    r'(?im)^\s*sec-websocket-protocol\s*:\s*([^\r\n]+)',
                    resp,
                )
                if not m:
                    continue
                accepted = m.group(1).lower()
                # Subprotocol value can be a comma list per RFC; tokens
                # are case-insensitive identifiers. Match exact token.
                tokens = [t.strip() for t in accepted.split(",")]
                if "rtsp" not in tokens:
                    continue
                return f"{scheme_ws}://{ip}:{port}{path}"
        except Exception:
            pass
    return None

# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# Active camera-positive probes
# ─────────────────────────────────────────────────────────────────────────────

def probe_rtsp_options(ip: str, port: int, timeout: int = 3) -> bool:
    """
    Send RTSP OPTIONS via raw TCP and check for an RTSP response header.
    Works even when auth is required — a 401 is still camera-positive.
    This is the fastest and most reliable camera litmus test.
    Routers, printers, NAS devices do NOT speak RTSP and will close the
    connection or return HTTP/garbage.
    """
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            request = (
                f"OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\n"
                f"CSeq: 1\r\n"
                f"User-Agent: AnyCam/1.0\r\n"
                f"\r\n"
            )
            sock.sendall(request.encode())
            sock.settimeout(timeout)
            response = sock.recv(256).decode("utf-8", errors="replace")
            # Any RTSP response = camera
            if response.startswith("RTSP/"):
                log.info(f"  RTSP OPTIONS confirm: {ip}:{port} -> {response.split(chr(13))[0]}")
                return True
    except Exception:
        pass
    return False


# Additional paths to try for identity probing beyond root /
_IDENTITY_PATHS = [
    "/", "/index.html", "/index.htm", "/login.htm", "/login.html",
    "/web/", "/web/index.html", "/cgi-bin/main-cgi", "/view/index.shtml",
    "/live", "/admin/",
]


def _make_ssl_ctx() -> ssl.SSLContext:

    """SSL context that ignores self-signed certificates (common on cameras/NVRs)."""
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx


def _rtsp_options_fingerprint(
    host: str,
    port: int = 554,
    *,
    timeout: float = 3.0,
    path: str = "/",
) -> dict:
    """Open one TCP socket to host:port, send a single RTSP OPTIONS
    request, read the response, and return a dict of captured fingerprint
    fields.

    Returns dict with keys:
        status:           int|None      e.g. 200, 401, 404
        looks_like_rtsp:  bool          True if any line started with "RTSP/"
        server_header:    str|None      value of Server: header
        auth_scheme:      str|None      "Digest" | "Basic" | None
        auth_realm:       str|None      realm from WWW-Authenticate
        auth_algorithm:   str|None      algorithm from WWW-Authenticate
        public_methods:   list[str]     parsed from Public: header
        cseq:             str|None      echoed CSeq
        raw_response:     str           full response text (truncated to 4 KB)
        elapsed_ms:       float         wall-clock time of round-trip
        error:            str|None      populated only on socket-level failure

    Failure semantics:
      - Connection refused / timeout / RST: returns dict with `error`
        populated, `looks_like_rtsp=False`, all other fields None or empty.
      - Server speaks something else (HTTP, FTP): `looks_like_rtsp=False`,
        `status=None`, `raw_response` captures whatever was received.
      - Server speaks RTSP but returns 4xx: `looks_like_rtsp=True`,
        `status` set, `auth_*` fields populated if challenge present.
      - Server speaks RTSP and returns 200: full population including
        `public_methods`.

    Implementation notes:
      - Single TCP open + close. No retries. ~3s timeout.
      - No User-Agent header sent — keeps the request minimal.
      - Reads up to 4 KB or until "\\r\\n\\r\\n" or socket close.
      - Parses headers via simple line-split + ":" partition.
      - Multi-line continuation headers (RFC 822 folding) collapsed onto
        the previous header before parsing.

    Risk profile:
      - Zero risk to Hipcam-family rate-limited hosts: this IS the first
        TCP open, no preceding probe to collide with.
      - Zero risk to Lorex/Dahua DVR auth-lockout hosts: OPTIONS doesn't
        authenticate, just receives the 401 challenge. No counter increment.

    Added in 2.4.0-rc1.0 per RTSP_OPTIONS_Fingerprint_Helper_Plan.md.
    """
    import socket as _sock
    import re as _re_local
    import time as _time_local

    result: dict = {
        "status": None,
        "looks_like_rtsp": False,
        "server_header": None,
        "auth_scheme": None,
        "auth_realm": None,
        "auth_algorithm": None,
        "public_methods": [],
        "cseq": None,
        "raw_response": "",
        "elapsed_ms": 0.0,
        "error": None,
    }

    # Build the request. Use 'rtsp://host:port/path' as the request URI per
    # RFC 2326 §10. CSeq is mandatory per spec. No User-Agent — keeps the
    # request minimal and avoids any User-Agent-based filtering some
    # servers might do (per user request 2026-05-03).
    request_uri = f"rtsp://{host}:{port}{path}"
    request_lines = [
        f"OPTIONS {request_uri} RTSP/1.0",
        "CSeq: 1",
        "",   # blank line terminating headers
        "",   # extra CRLF
    ]
    request_bytes = "\r\n".join(request_lines).encode("ascii", errors="replace")

    t0 = _time_local.monotonic()
    sock = None
    try:
        sock = _sock.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall(request_bytes)

        # Read up to 4 KB or until "\r\n\r\n" or socket close
        buf = b""
        max_bytes = 4096
        while len(buf) < max_bytes:
            try:
                chunk = sock.recv(min(1024, max_bytes - len(buf)))
            except _sock.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if b"\r\n\r\n" in buf:
                break

        result["elapsed_ms"] = (_time_local.monotonic() - t0) * 1000.0
        try:
            text = buf.decode("utf-8", errors="replace")
        except Exception:
            text = buf.decode("latin-1", errors="replace")
        result["raw_response"] = text[:4096]

    except (_sock.timeout, OSError) as exc:
        result["elapsed_ms"] = (_time_local.monotonic() - t0) * 1000.0
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    # ---- Parse the response ----
    text = result["raw_response"]
    if not text:
        result["error"] = "empty_response"
        return result

    # Split into lines on CRLF or LF
    lines = text.replace("\r\n", "\n").split("\n")

    # Status line: "RTSP/1.0 200 OK" or "HTTP/1.1 400 Bad Request" etc
    if lines:
        status_line = lines[0].strip()
        if status_line.startswith("RTSP/"):
            result["looks_like_rtsp"] = True
            # Parse status code
            parts = status_line.split(None, 2)
            if len(parts) >= 2:
                try:
                    result["status"] = int(parts[1])
                except ValueError:
                    pass
        # If it's not RTSP, we still capture the raw text but leave
        # looks_like_rtsp=False and status=None.

    # Collapse RFC 822 continuation lines (lines starting with whitespace
    # are continuations of the previous header)
    folded: list[str] = []
    for line in lines[1:]:
        if line.startswith((" ", "\t")) and folded:
            folded[-1] += " " + line.strip()
        else:
            folded.append(line)

    # Parse headers — simple ":"-split
    for line in folded:
        if not line.strip():
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key_l = key.strip().lower()
        value = value.strip()

        if key_l == "server":
            result["server_header"] = value
        elif key_l == "cseq":
            result["cseq"] = value
        elif key_l == "public":
            # Public: OPTIONS, DESCRIBE, SETUP, PLAY, ...
            methods = [m.strip().upper() for m in value.split(",") if m.strip()]
            result["public_methods"] = methods
        elif key_l == "www-authenticate":
            # "Digest realm=\"foo\", algorithm=MD5, nonce=..."
            # or "Basic realm=\"bar\""
            scheme_match = _re_local.match(r"^\s*(\w+)\s+", value)
            if scheme_match:
                result["auth_scheme"] = scheme_match.group(1)
            # realm="..." — handle escaped quotes inside
            realm_match = _re_local.search(
                r'realm\s*=\s*"((?:[^"\\]|\\.)*)"', value
            )
            if realm_match:
                # Unescape \" → "
                result["auth_realm"] = realm_match.group(1).replace('\\"', '"')
            # algorithm=MD5 (unquoted) or algorithm="SHA-256" (quoted)
            algo_match = _re_local.search(
                r'algorithm\s*=\s*("([^"]+)"|([\w\-]+))', value
            )
            if algo_match:
                result["auth_algorithm"] = algo_match.group(2) or algo_match.group(3)

    return result



    """
    Fetch HTTP pages from a device and extract identity info:
      - Page title, Server header
      - Manufacturer matched against CAMERA_DB
      - is_camera flag

    Key improvements over naive fetch:
      - SSL certificate errors ignored (cameras use self-signed certs)
      - Both http:// and https:// tried on every port
      - Multiple paths probed (/, /login.htm, /web/, etc.)
      - Script src attributes scanned — catches JS SPAs where the logo
        is an image but the manufacturer name appears in asset paths
        (e.g. Lorex's /flirLorex/js/... paths)
      - Up to 16KB of body read for better coverage
    """
    import urllib.request, urllib.error, re as _re, ssl as _ssl

    result = {
        "is_camera": False,
        "title": "", "server": "",
        "manufacturer": "", "notes": "", "raw_snippet": "",
    }

    GENERIC_CAM_BODY = [
        "camera", "ipcam", "webcam", "nvr", "dvr", "cctv",
        "onvif", "rtsp", "video", "stream", "live view",
        "network camera", "ip camera", "surveillance",
        "channel", "ptz", "pan tilt",
    ]

    ssl_ctx = _make_ssl_ctx()

    def _extract(body_bytes: bytes, headers_str: str, url: str) -> bool:
        """Returns True if a match was found (stop probing further paths)."""
        body = body_bytes.decode("utf-8", errors="replace")
        if not result["raw_snippet"]:
            result["raw_snippet"] = body[:500]

        # Page title
        if not result["title"]:
            m = _re.search(r"<title[^>]*>([^<]{1,120})</title>", body, _re.I)
            if m:
                result["title"] = m.group(1).strip()

        # Include script src paths — SPAs like Lorex put the manufacturer
        # name in asset paths (e.g. src="/flirLorex/js/...")
        # Extract all attribute values from the HTML
        attr_values = " ".join(_re.findall(r'(?:src|href|action)=["\']([^"\']{3,120})["\']',
                                           body, _re.I))

        combined = headers_str + " " + body[:16384] + " " + attr_values

        # CAMERA_DB match (rich, manufacturer-specific)
        entry = identify_manufacturer(combined)
        if entry and entry["name"] != "Generic IP Camera":
            result["manufacturer"] = entry["name"]
            result["notes"]        = entry["notes"]
            result["is_camera"]    = True
            log.info(f"  HTTP identity {url}: → {entry['name']}")
            return True

        # Generic camera keyword fallback
        combined_l = combined.lower()
        for kw in GENERIC_CAM_BODY:
            if kw in combined_l:
                result["is_camera"] = True
                if entry:
                    result["manufacturer"] = entry["name"]
                    result["notes"]        = entry["notes"]
                log.info(f"  HTTP camera keyword: {ip}:{port} ({kw})")
                return True

        return False

    def _fetch_and_extract(url: str) -> bool:
        """Fetch a URL (with SSL bypass) and run _extract. Returns True on match."""
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0 AnyCam/1.0")
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=ssl_ctx) as resp:
                server = resp.headers.get("Server", "")
                if not result["server"]:
                    result["server"] = server
                all_headers = str(resp.headers)
                body = resp.read(16384)
                return _extract(body, all_headers + " " + server, url)
        except urllib.error.HTTPError as e:
            # 401/403: headers may still identify the device
            server = e.headers.get("Server", "")
            if not result["server"]:
                result["server"] = server
            all_headers = str(e.headers)
            try:
                body = e.read(16384)
            except Exception:
                body = b""
            return _extract(body, all_headers + " " + server, url)
        except Exception:
            return False

    # Try both schemes; cameras often redirect http→https
    schemes = []
    if port in (443, 8443):
        schemes = ["https"]
    elif port in (80, 8080, 8000, 8888):
        schemes = ["http", "https"]
    else:
        schemes = ["http", "https"]

    for scheme in schemes:
        for path in _IDENTITY_PATHS:
            url = f"{scheme}://{ip}:{port}{path}"
            if _fetch_and_extract(url):
                return result  # found a match — stop

    return result


def probe_http_for_camera(ip: str, port: int, timeout: int = 4) -> bool:
    """Thin wrapper — returns True if probe_http_identity says is_camera."""
    return probe_http_identity(ip, port, timeout).get("is_camera", False)


# Quick probe path lists — shorter than the full probers, used only for
# the is_camera_positive gate check where speed matters more than coverage.
_QUICK_MJPEG_PATHS = ["/video", "/mjpeg", "/stream", "/mjpg/video.mjpg",
                      "/cgi-bin/mjpg/video.cgi", "/videostream.cgi"]
_QUICK_HLS_PATHS   = ["/index.m3u8", "/stream.m3u8", "/live.m3u8",
                      "/hls/stream.m3u8", "/live/stream.m3u8"]


def probe_mjpeg_quick(ip: str, port: int, timeout: int = 3) -> bool:
    """
    Check a handful of common MJPEG paths for multipart/x-mixed-replace
    or image/jpeg Content-Type.  Used as a fast gate check; the full
    probe_mjpeg_http() runs later if this passes.
    """
    import urllib.request
    scheme = "https" if port in (443, 8443) else "http"
    for path in _QUICK_MJPEG_PATHS:
        url = f"{scheme}://{ip}:{port}{path}"
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "AnyCam/1.0")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ct = resp.headers.get("Content-Type", "").lower()
                if any(k in ct for k in ("multipart/x-mixed-replace",
                                         "image/jpeg", "mjpeg", "mjpg")):
                    log.info(f"  MJPEG gate confirm: {ip}:{port}{path}")
                    return True
        except Exception:
            pass
    return False


def probe_hls_quick(ip: str, port: int, timeout: int = 3) -> bool:
    """
    Check a handful of common HLS paths for an M3U8 playlist response
    (#EXTM3U header or mpegurl Content-Type).  Used as a fast gate check.
    """
    import urllib.request
    scheme = "https" if port in (443, 8443) else "http"
    for path in _QUICK_HLS_PATHS:
        url = f"{scheme}://{ip}:{port}{path}"
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "AnyCam/1.0")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ct   = resp.headers.get("Content-Type", "").lower()
                body = resp.read(32).decode("utf-8", errors="replace")
                if ("mpegurl" in ct or "m3u8" in ct or
                        body.strip().startswith("#EXTM3U")):
                    log.info(f"  HLS gate confirm: {ip}:{port}{path}")
                    return True
        except Exception:
            pass
    return False


def is_camera_positive(ip: str, port: int, service: str, product: str,
                        verdict: str, onvif_ips: set, ssdp_cam_ips: set,
                        mdns_ips: set, mac_addr: str = "") -> bool:
    """
    Gate function: returns True only if at least one active probe or
    multicast discovery confirms this device is likely a camera.

    Probes run in priority order — fastest / most definitive first,
    slower / less certain probes only tried if earlier ones fail.

    Protocol coverage:
      RTSP    — raw OPTIONS handshake (~100ms, definitive, works through auth)
      RTMP    — C0 handshake byte check (~50ms, definitive)
      MJPEG   — Content-Type multipart/x-mixed-replace on common paths (~200ms)
      HLS     — #EXTM3U body or mpegurl Content-Type on common paths (~200ms)
      WebRTC  — WHEP POST + heuristic GET on signaling paths (~300ms)
      WS-RTSP — WebSocket upgrade with Sec-WebSocket-Protocol: rtsp (~200ms)
      HTTP    — full page body + header scan for camera strings (~500ms)
      ONVIF   — already confirmed by multicast Stage 1 (instant)
      SSDP    — already confirmed by multicast Stage 1 (instant)
      mDNS    — already confirmed by multicast Stage 1 (instant)
      DVR     — ports 37777/34567 assumed positive by definition
    """
    # ── 1. Multicast-confirmed (instant, already done in Stage 1) ──────────
    if ip in onvif_ips or ip in ssdp_cam_ips or ip in mdns_ips:
        return True

    # ── 1b. OUI camera-positive (MAC address manufacturer lookup) ─────────
    if mac_addr:
        oui_result = oui_is_camera(mac_addr)
        if oui_result is True:
            log.info(f"  OUI camera confirm: {ip} MAC {mac_addr} → {lookup_oui(mac_addr)}")
            return True
        if oui_result is False:
            log.info(f"  OUI non-camera reject: {ip} MAC {mac_addr} → {lookup_oui(mac_addr)}")
            return False

    # ── 1c. Respect nmap not_camera verdict ────────────────────────────────
    # If nmap's service/product banner identified this as a non-camera device
    # (printer, router, NAS, etc.) AND OUI didn't confirm it's a camera,
    # skip probing entirely.  A camera that somehow has a generic service
    # banner would still be found via ONVIF/SSDP/mDNS in Stage 1.
    if verdict == "not_camera":
        log.info(f"  nmap verdict reject: {ip}:{port} — not_camera verdict")
        return False

    # ── 2. RTSP OPTIONS — raw TCP, ~100ms, works through auth ─────────────
    if port in (554, 8554, 10554, 2020, 8765):
        if probe_rtsp_options(ip, port):
            return True
        # Fall through: camera may have broken RTSP but working web UI

    # ── 3. RTMP C0 handshake — ~50ms, definitively identifies RTMP server ─
    if port in (1935, 1936):
        if probe_rtmp(ip, port):
            log.info(f"  RTMP gate confirm: {ip}:{port}")
            return True

    # ── 4. DVR ports — Dahua (37777) and generic DVR (34567) ──────────────
    if port in (37777, 34567):
        return True

    # ── 5–8. HTTP-family probes (all run on HTTP/HTTPS ports) ──────────────
    if port in (80, 8080, 8000, 8888, 443, 8443):

        # 5. MJPEG Content-Type check — fast, definitive for MJPEG cameras
        if probe_mjpeg_quick(ip, port):
            return True

        # 6. HLS M3U8 check — fast, definitive for HLS cameras/NVRs
        if probe_hls_quick(ip, port):
            return True

        # 7. WebRTC WHEP probe — POST SDP offer, look for SDP answer or hints
        if probe_webrtc(ip, port):
            log.info(f"  WebRTC gate confirm: {ip}:{port}")
            return True

        # 8. HTTP body/header content scan — broadest net, catches web UIs
        if probe_http_for_camera(ip, port):
            return True

    # ── 9. WS-RTSP upgrade — try on any port not already covered ──────────
    #       go2rtc typically serves on 8554, mediamtx on 8888 or custom;
    #       we try after the port-specific checks above.
    if probe_ws_rtsp(ip, port):
        log.info(f"  WS-RTSP gate confirm: {ip}:{port}")
        return True

    # ── 10. nmap banner: check against CAMERA_DB (much richer than CAMERA_KEYWORDS)
    combined = (service + " " + product).lower()
    if combined.strip():
        if identify_manufacturer(combined) is not None:
            log.info(f"  nmap DB match: {ip}:{port} — {combined.strip()}")
            return True
        # Fallback to simple keyword list
        if any(k in combined for k in CAMERA_KEYWORDS):
            return True

    # ── 11. DB alias check against hostname ────────────────────────────────
    # Sometimes the device hostname itself contains a manufacturer name
    # (e.g. "lorex-nvr.local", "hikvision-123.lan")
    if any(k in combined for k in _DB_MANUFACTURERS):
        return True

    return False


# ONVIF SOAP (multi-stream NVR support)
# ─────────────────────────────────────────────────────────────────────────────

def _onvif_soap(url: str, body: str,
                username: str = "", password: str = "", timeout: int = 6) -> str | None:
    import urllib.request
    security = ""
    if username:
        nonce_raw = os.urandom(16)
        nonce_b64 = base64.b64encode(nonce_raw).decode()
        created   = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        # RFC 2617 WS-Security PasswordDigest: SHA1(nonce_raw || created_utf8 || password_utf8)
        digest = base64.b64encode(
            hashlib.sha1(
                nonce_raw + created.encode("utf-8") + password.encode("utf-8")
            ).digest()
        ).decode()
        _wsse = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
        _wssu = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
        security = (
            f'<s:Header>'
            f'<Security xmlns="{_wsse}" '
            f'xmlns:wsu="{_wssu}" '
            f's:mustUnderstand="1">'
            f'<wsu:Timestamp wsu:Id="TS-1">'
            f'<wsu:Created>{created}</wsu:Created>'
            f'</wsu:Timestamp>'
            f'<UsernameToken wsu:Id="UT-1">'
            f'<Username>{username}</Username>'
            f'<Password Type="{_wsse}#PasswordDigest">{digest}</Password>'
            f'<Nonce EncodingType="{_wsse}#Base64Binary">{nonce_b64}</Nonce>'
            f'<wsu:Created>{created}</wsu:Created>'
            f'</UsernameToken>'
            f'</Security>'
            f'</s:Header>'
        )
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
        ' xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"{security}<s:Body>{body}</s:Body></s:Envelope>"
    )
    # Try SOAP 1.2 first (application/soap+xml), then fall back to SOAP 1.1
    # (text/xml) for cameras like Hikvision that return HTTP 400 on SOAP 1.2.
    for content_type in ("application/soap+xml; charset=utf-8",
                         "text/xml; charset=utf-8"):
        try:
            req = urllib.request.Request(url, envelope.encode("utf-8"), method="POST")
            req.add_header("Content-Type", content_type)
            req.add_header("SOAPAction", '""')
            req.add_header("User-Agent", f"AnyCam/{CURRENT_VERSION}")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            err_str = str(e)
            if "400" in err_str and content_type.startswith("application/soap"):
                log.debug(f"ONVIF SOAP ({url}): SOAP 1.2 → 400, retrying with SOAP 1.1")
                continue   # retry with text/xml
            log.debug(f"ONVIF SOAP ({url}): {e}")
            return None
    return None


def onvif_get_profiles(onvif_url: str, username: str, password: str) -> list[dict]:
    """
    Parse ONVIF GetProfiles response.
    Returns profile list with resolution, video codec, and audio codec taken
    directly from VideoEncoderConfiguration / AudioEncoderConfiguration in the
    XML.  These are always present regardless of stream codec, so they are
    used as authoritative sources — probing is only needed for actual FPS
    (which ONVIF's FrameRateLimit doesn't accurately reflect).
    """
    xml = _onvif_soap(onvif_url, "<trt:GetProfiles/>", username, password)
    if not xml:
        return []
    profiles = []
    enc_map  = {"H264": "h264", "H265": "hevc", "JPEG": "mjpeg",
                "H264E": "h264", "MPEG4": "mpeg4"}
    try:
        root = ET.fromstring(xml)
        ns   = {"trt": "http://www.onvif.org/ver10/media/wsdl",
                "tt":  "http://www.onvif.org/ver10/schema"}
        for p in root.findall(".//trt:Profiles", ns):
            token = p.get("token", "")
            name_el = p.find("tt:Name", ns)
            name = name_el.text if name_el is not None else token
            if not token:
                continue

            # ── Video resolution + codec ──────────────────────────────────────
            onvif_w   = None
            onvif_h   = None
            onvif_enc = None
            vec = p.find("tt:VideoEncoderConfiguration", ns)
            if vec is not None:
                res_el = vec.find("tt:Resolution", ns)
                if res_el is not None:
                    try:
                        onvif_w = int(res_el.findtext("tt:Width",  namespaces=ns) or 0) or None
                        onvif_h = int(res_el.findtext("tt:Height", namespaces=ns) or 0) or None
                    except (ValueError, TypeError):
                        pass
                enc_el = vec.find("tt:Encoding", ns)
                if enc_el is not None and enc_el.text:
                    onvif_enc = enc_map.get(enc_el.text.upper(), enc_el.text.lower())

            # ── Audio codec ───────────────────────────────────────────────────
            onvif_audio = None
            aec = p.find("tt:AudioEncoderConfiguration", ns)
            if aec is not None:
                aenc_el = aec.find("tt:Encoding", ns)
                if aenc_el is not None and aenc_el.text:
                    onvif_audio = aenc_el.text.lower()  # e.g. "g711", "aac", "g726"

            profiles.append({
                "token": token, "name": name,
                "onvif_width":    onvif_w,
                "onvif_height":   onvif_h,
                "onvif_encoding": onvif_enc,
                "onvif_audio":    onvif_audio,
            })
    except Exception as e:
        log.debug(f"GetProfiles parse: {e}")
    return profiles


def onvif_get_stream_uri(onvif_url: str, token: str,
                         username: str, password: str) -> str | None:
    body = (
        f"<trt:GetStreamUri>"
        f"<trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
        f"<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
        f"<trt:ProfileToken>{token}</trt:ProfileToken>"
        f"</trt:GetStreamUri>"
    )
    xml = _onvif_soap(onvif_url, body, username, password)
    if not xml:
        return None
    try:
        root   = ET.fromstring(xml)
        uri_el = root.find(".//{http://www.onvif.org/ver10/schema}Uri")
        return uri_el.text.strip() if uri_el is not None else None
    except Exception:
        return None


def onvif_get_snapshot_uri(onvif_url: str, token: str,
                            username: str, password: str) -> str | None:
    """
    Call ONVIF GetSnapshotUri for the given profile token.
    Returns the snapshot HTTP URL, or None if the camera does not support it.
    Used as a secondary source when STREAM_DB has no snap entry.
    """
    body = (
        f"<trt:GetSnapshotUri>"
        f"<trt:ProfileToken>{token}</trt:ProfileToken>"
        f"</trt:GetSnapshotUri>"
    )
    xml = _onvif_soap(onvif_url, body, username, password)
    if not xml:
        return None
    try:
        root   = ET.fromstring(xml)
        uri_el = root.find(".//{http://www.onvif.org/ver10/schema}Uri")
        uri    = uri_el.text.strip() if uri_el is not None else None
        if uri and uri.startswith("http"):
            return uri
        return None
    except Exception as exc:
        log.debug(f"onvif_get_snapshot_uri parse error: {exc}")
        return None


def _onvif_media_url(ip: str, port: int, xaddrs: str) -> str:
    """
    Build the ONVIF media service URL from the XAddrs field.

    XAddrs can contain multiple space-separated URLs (e.g. one IPv4 and one
    IPv6 link-local address).  We pick the best single URL:
      1. Prefer plain http:// URLs with IPv4 addresses (no IPv6 link-local)
      2. Avoid IPv6 link-local addresses (fe80::...)
      3. Fall back to whatever is first if nothing else qualifies

    Without this, cameras advertising both IPv4 and IPv6 addrs produce a
    malformed URL like:
      http://10.0.0.33/onvif/media http://[fe80::...]/onvif/media
    which causes SOAP requests to fail with 0 profiles returned.
    """
    if xaddrs:
        # Split on whitespace — camera may advertise multiple addrs in one element
        candidates = xaddrs.split()
        chosen = None
        for c in candidates:
            c = c.strip()
            if not c.startswith("http"):
                continue
            # Skip IPv6 link-local (fe80::) — unreliable from HA container
            if "fe80" in c.lower() or "[" in c:
                continue
            chosen = c
            break
        if chosen is None:
            # Fallback: take first http:// URL regardless of address type
            for c in candidates:
                c = c.strip()
                if c.startswith("http"):
                    chosen = c
                    break
        if chosen is None:
            chosen = candidates[0].strip() if candidates else xaddrs
        return chosen.rstrip("/").replace("device_service", "media").replace("Device", "Media")
    scheme = "https" if port in (443, 8443) else "http"
    return f"{scheme}://{ip}:{port}/onvif/media"

# ─────────────────────────────────────────────────────────────────────────────
# Full-range port scanner (user-initiated, separate from camera scan)
# ─────────────────────────────────────────────────────────────────────────────

async def run_port_scan(ip: str) -> None:

    """
    Full 65535-port scan with live discovery feed.

    Uses nmap -v so it emits 'Discovered open port X/tcp on Y' lines
    as ports are found, and --stats-every 10s for ETA lines.
    Results are written to a temp XML file; parsed for the final table.
    """
    import os as _os

    # Load initial ETA estimate from last port scan duration
    _runtime    = load_runtime()
    _last_p_dur = _runtime.get("last_port_scan_duration", 0)
    _init_eta   = int(_last_p_dur) if _last_p_dur > 10 else 300  # 5 min fallback

    PSCAN.update(
        running=True, paused=False, ip=ip, progress=2,
        message=f"Scanning all 65535 ports on {ip}…",
        results=[], live_ports=[], proc_pid=None,
        scan_start=time.time(), eta=_init_eta, percent=0.0,
    )

    xml_path = f"/tmp/anycam_pscan_{ip.replace('.','_')}.xml"

    try:
        proc = await asyncio.create_subprocess_exec(
            "nmap", "-sV", "-sC", "-A", "--open", "-p-",
            "-v", "--stats-every", "10s",
            "--host-timeout", "600s", "-T3",
            "-oX", xml_path, ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge stderr so we capture stats
        )
        PSCAN["proc_pid"] = proc.pid

        # Stream stdout line by line for live port discovery + ETA
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()

            # "Discovered open port 554/tcp on 192.168.50.3"
            m_port = re.search(r"Discovered open port (\d+)/(\w+)", line)
            if m_port:
                port_num = int(m_port.group(1))
                proto    = m_port.group(2)
                PSCAN["live_ports"].append({"port": port_num, "proto": proto})
                PSCAN["message"] = (
                    f"Scanning {ip}… {len(PSCAN['live_ports'])} open port(s) found")
                continue

            # "About 34.56% done; ETC: 13:45 (0:03:12 remaining)"
            m_pct = re.search(r"About ([\d.]+)% done", line)
            if m_pct:
                pct = float(m_pct.group(1))
                PSCAN["percent"]  = pct
                PSCAN["progress"] = max(2, min(95, int(pct)))

            m_eta = re.search(r"(\d+):(\d+):(\d+) remaining", line)
            if m_eta:
                h, m, s = int(m_eta.group(1)), int(m_eta.group(2)), int(m_eta.group(3))
                PSCAN["eta"] = h * 3600 + m * 60 + s

        await asyncio.wait_for(proc.wait(), timeout=30)

        # Parse the XML temp file for rich service details
        results = []
        if _os.path.exists(xml_path):
            try:
                # Use run_in_executor so this blocking file read doesn't hold
                # the event loop — nmap XML can be several KB on busy subnets.
                loop     = asyncio.get_event_loop()
                xml_text = await loop.run_in_executor(
                    _THREAD_POOL, lambda: open(xml_path).read()
                )
                root = ET.fromstring(xml_text)
                for host in root.findall("host"):
                    for port_el in host.findall("ports/port"):
                        pst = port_el.find("state")
                        if pst is None or pst.get("state") != "open":
                            continue
                        svc     = port_el.find("service")
                        scripts = {sc.get("id",""):sc.get("output","")
                                   for sc in port_el.findall("script")}
                        results.append({
                            "port":    int(port_el.get("portid")),
                            "proto":   port_el.get("protocol","tcp"),
                            "service": svc.get("name","")      if svc is not None else "",
                            "product": svc.get("product","")   if svc is not None else "",
                            "version": svc.get("version","")   if svc is not None else "",
                            "extra":   svc.get("extrainfo","") if svc is not None else "",
                            "scripts": scripts,
                        })
            except Exception as e:
                log.warning(f"Port scan XML parse: {e}")
            finally:
                try:
                    _os.unlink(xml_path)
                except Exception:
                    pass

        elapsed = round(time.time() - PSCAN["scan_start"])
        # Save for next scan's ETA
        _rt = load_runtime()
        _rt["last_port_scan_duration"] = elapsed
        save_runtime(_rt)

        PSCAN.update(
            running=False, progress=100, results=results,
            live_ports=[],   # clear — final table takes over
            eta=0, percent=100.0,
            message=f"Scan complete — {len(results)} open port(s) on {ip}. "
                    f"({elapsed//60}:{elapsed%60:02d})",
        )

    except asyncio.TimeoutError:
        PSCAN.update(running=False, progress=100, message="Scan timed out.", live_ports=[])
    except Exception as e:
        PSCAN.update(running=False, progress=100, message=f"Scan error: {e}", live_ports=[])
    finally:
        PSCAN["proc_pid"] = None

# ─────────────────────────────────────────────────────────────────────────────
# Main scan orchestration — 4-stage pipeline
# ─────────────────────────────────────────────────────────────────────────────

async def _rerun_onvif_auth(camera_id: str, camera: dict,
                             username: str, password: str) -> bool:
    """
    Re-run the full ONVIF credential flow for a saved camera.
    Updates stream_url, sub_stream_url, stream_profiles, and codec/res details
    in-place.  Returns True if at least one profile was resolved.

    Called by run_verification_scan() for ONVIF cameras with stored credentials
    so that fixes to the ONVIF layer (e.g. XAddrs URL parsing) take effect
    without requiring the user to manually re-enter credentials.
    """
    ip       = camera.get("ip", "")
    port     = camera.get("port", 554)
    loop     = asyncio.get_event_loop()
    xaddrs   = camera.get("xaddrs", "")
    media_url = _onvif_media_url(ip, port, xaddrs)

    log.info(f"  [{camera_id}] ONVIF re-auth: media_url={media_url}")
    profiles = await loop.run_in_executor(
        _THREAD_POOL, onvif_get_profiles, media_url, username, password)
    log.info(f"  [{camera_id}] ONVIF profiles found: {len(profiles)} "
             f"— {[p['name'] for p in profiles]}")
    if not profiles:
        log.warning(f"  [{camera_id}] ONVIF re-auth: no profiles returned")
        return False

    enc_creds = encrypt_creds(username, password)
    stream_candidates = []
    for prof in profiles:
        stream_url = await loop.run_in_executor(
            _THREAD_POOL, onvif_get_stream_uri, media_url, prof["token"], username, password)
        if not stream_url:
            continue
        det = await probe_stream_details(stream_url, "RTSP")

        # Best-source resolution (ONVIF vs probe — largest pixel area wins)
        probe_w  = det.get("stream_width")  or 0
        probe_h  = det.get("stream_height") or 0
        onvif_w  = prof.get("onvif_width")  or 0
        onvif_h  = prof.get("onvif_height") or 0
        if (onvif_w * onvif_h) >= (probe_w * probe_h) and onvif_w:
            det["stream_width"]  = onvif_w
            det["stream_height"] = onvif_h

        # Best-source codec (highest capability wins)
        _CODEC_RANK = {"hevc": 4, "h265": 4, "h264": 3, "mjpeg": 2, "mpeg4": 1}
        probe_codec = det.get("stream_codec") or ""
        onvif_codec = prof.get("onvif_encoding") or ""
        if _CODEC_RANK.get(onvif_codec.lower(), 0) > _CODEC_RANK.get(probe_codec.lower(), 0):
            det["stream_codec"] = onvif_codec
        if prof.get("onvif_audio"):
            det["stream_audio"] = prof["onvif_audio"]

        log.info(f"  [{camera_id}] Profile '{prof['name']}': "
                 f"{det.get('stream_width')}x{det.get('stream_height')} "
                 f"{det.get('stream_codec','?')} "
                 f"{det.get('stream_fps','?')}fps")
        stream_candidates.append({
            "url": stream_url, "token": prof["token"],
            "name": prof["name"], **det,
        })

    if not stream_candidates:
        return False

    # Rank by resolution descending
    def _res(c) -> int:

        return (c.get("stream_width") or 0) * (c.get("stream_height") or 0)
    stream_candidates.sort(key=_res, reverse=True)
    main_s = stream_candidates[0]
    sub_s  = stream_candidates[-1] if len(stream_candidates) > 1 else None

    stream_profiles = []
    for i, cand in enumerate(stream_candidates):
        url_key = ("stream_url" if i == 0
                   else ("sub_stream_url" if i == len(stream_candidates) - 1
                         else f"stream_profile_{i}_url"))
        stream_profiles.append({
            "url":           cand["url"],
            "_url_key":      url_key,
            "stream_width":  cand.get("stream_width"),
            "stream_height": cand.get("stream_height"),
            "stream_codec":  cand.get("stream_codec"),
            "stream_fps":    cand.get("stream_fps"),
            "stream_audio":  cand.get("stream_audio"),
        })

    main_token  = main_s.get("token", profiles[0]["token"])
    new_cid     = f"{ip}_onvif_{main_token}"
    main_details = {k: v for k, v in main_s.items() if k not in ("url", "token", "name")}
    extra_urls   = {}
    for i, cand in enumerate(stream_candidates):
        if 0 < i < len(stream_candidates) - 1:
            extra_urls[f"stream_profile_{i}_url"] = cand["url"]

    updated = {
        **camera,
        "id":              new_cid,
        "stream_url":      main_s["url"],
        "sub_stream_url":  sub_s["url"] if sub_s else None,
        "stream_profiles": stream_profiles,
        "credentials":     enc_creds,
        "status":          "ready",
        "hevc_plus_warning": False,
        **extra_urls,
        **main_details,
    }
    CAMERAS[new_cid] = updated
    if new_cid != camera_id:
        CAMERAS.pop(camera_id, None)
    cam_url = build_authenticated_url(updated) or ""
    log.info(f"  [{camera_id}] ONVIF re-auth OK — {len(stream_profiles)} profile(s) "
             f"updated, new id={new_cid}")
    return True


async def run_verification_scan(prev_version: str = "unknown") -> None:
    """
    Post-upgrade verification scan.
    Runs after saved cameras are loaded.

    For each saved camera:
      - If ONVIF with stored credentials: re-runs the full ONVIF auth flow so
        any fixes to profile parsing / XAddrs handling take effect immediately.
      - Probes reachability with the appropriate stream prober.
      - If RTSP probe fails and camera has an http_snap_url: tries HTTP snap
        URL (200 or 401 both confirm the camera is alive).
      - If unreachable → marks as "unverified_after_upgrade".

    After verifying saved cameras, runs a fresh full scan to discover
    any new cameras that upgraded detection capabilities might now find.
    """
    SCAN_STATE.update(
        running=True, progress=0, stage=1,
        stage_label="Post-upgrade verification",
        message="Post-upgrade: verifying previously saved cameras…"
    )
    loop = asyncio.get_event_loop()
    log.info(f"Post-upgrade verification scan ({prev_version} → {CURRENT_VERSION})")

    # ── Verify each saved camera ──────────────────────────────────────────
    still_present = []
    now_missing   = []

    for cid, cam in list(CAMERAS.items()):
        if not cam.get("user_saved"):
            continue
        ip   = cam.get("ip","")
        port = cam.get("port", 554)
        proto = cam.get("protocol","RTSP")
        SCAN_STATE["message"] = f"Verifying {cam.get('name', ip)}…"

        found = False
        creds = cam.get("credentials")
        u = p = ""
        if creds:
            try:
                u, p = decrypt_creds(creds)
            except Exception:
                pass

        # For ONVIF cameras with stored credentials, re-run the full ONVIF
        # auth flow so fixes to profile parsing / XAddrs handling take effect
        # immediately without requiring the user to re-enter credentials.
        if (proto == "ONVIF" or cam.get("onvif")) and creds and u:
            log.info(f"  Re-running ONVIF auth for {cam.get('name', ip)}")
            SCAN_STATE["message"] = f"Re-authenticating ONVIF: {cam.get('name', ip)}…"
            found = await _rerun_onvif_auth(cid, cam, u, p)
            if found:
                # _rerun_onvif_auth updated CAMERAS in-place — skip normal probe
                still_present.append(cid)
                cam.pop("upgrade_missing", None)
                log.info(f"  ONVIF re-auth verified OK: {cam.get('name', ip)}")
                continue
            # If ONVIF re-auth failed, fall through to basic RTSP probe below
            log.warning(f"  ONVIF re-auth failed for {cam.get('name', ip)} "
                        f"— falling back to basic RTSP probe")

        # Quick probe appropriate to the protocol
        if proto in ("RTSP", "DVR", "ONVIF"):
            url = cam.get("stream_url","")
            if url:
                found = await loop.run_in_executor(_THREAD_POOL, probe_rtsp, url, u, p)
                # Populate codec info using authenticated URL if not yet stored
                if not cam.get("stream_codec"):
                    _auth_url = build_authenticated_url(cam) or url
                    if _auth_url:
                        _details = await probe_stream_details(_auth_url, "RTSP")
                        if _details:
                            cam.update(_details)
                            log.info(f"  Stream details: "
                                     f"{cam.get('stream_codec','?')} "
                                     f"{cam.get('stream_width','?')}x{cam.get('stream_height','?')}")
            if not found:
                found = await loop.run_in_executor(_THREAD_POOL, probe_rtsp_options, ip, port)
        elif proto == "MJPEG":
            result = await loop.run_in_executor(_THREAD_POOL, probe_mjpeg_quick, ip, port)
            found = bool(result)
        elif proto == "HLS":
            result = await loop.run_in_executor(_THREAD_POOL, probe_hls_quick, ip, port)
            found = bool(result)
        elif proto == "RTMP":
            found = await loop.run_in_executor(_THREAD_POOL, probe_rtmp, ip, port)
        else:
            # WebRTC / WS-RTSP / HTTP — just check TCP reachability
            try:
                _, w = await asyncio.wait_for(
                    asyncio.open_connection(ip, port), timeout=3)
                w.close()
                found = True
            except Exception:
                found = False

        if found:
            still_present.append(cid)
            cam.pop("upgrade_missing", None)
            # Clear stale hevc_plus_warning — if the stream probes OK the camera
            # is not stuck on H.265+. _drain_stderr will re-set it if needed.
            cam["hevc_plus_warning"] = False
            log.info(f"  Verified OK: {cam.get('name', ip)}")
        else:
            # ── HTTP snap URL fallback verification ───────────────────────────
            # Cameras like the Microseven have broken RTSP but a working
            # HTTP snapshot endpoint.  If the RTSP probe failed and the camera
            # has a stored http_snap_url, do a quick connectivity check against
            # it.  A 200 or 401 response both confirm the camera is reachable
            # (401 just means we need to send auth — the camera is alive).
            http_snap = cam.get("http_snap_url")
            if http_snap and not found:
                try:
                    import urllib.request as _urlreq
                    _req = _urlreq.Request(http_snap, method="GET")
                    _req.add_header("User-Agent", f"AnyCam/{CURRENT_VERSION}")
                    try:
                        with _urlreq.urlopen(  # nosec — LAN only, ssl not relevant
                            _req, timeout=5,
                            context=__import__("ssl")._create_unverified_context()
                        ) as _r:
                            found = _r.status in (200, 401)
                    except Exception as _he:
                        # urllib raises HTTPError for 4xx — that still means alive
                        _code = getattr(_he, "code", None)
                        if _code in (401, 403):
                            found = True
                        else:
                            found = False
                    if found:
                        log.info(f"  Verified OK via HTTP snap: {cam.get('name', ip)}")
                    else:
                        log.warning(f"  HTTP snap probe also failed: {cam.get('name', ip)}")
                except Exception as _exc:
                    log.debug(f"  HTTP snap verify error: {_exc}")

            if found:
                still_present.append(cid)
                cam.pop("upgrade_missing", None)
                cam["hevc_plus_warning"] = False  # clear stale flag
                log.info(f"  Verified OK: {cam.get('name', ip)}")
            else:
                now_missing.append(cid)
                cam["upgrade_missing"] = True
            cam["upgrade_missing_version"] = CURRENT_VERSION

    # ── Save updated camera states ────────────────────────────────────────────
    save_cameras()
    log.info(
        f"Verification complete: {len(still_present)} present, "
        f"{len(now_missing)} missing"
    )
    if now_missing:
        for cid in now_missing:
            cam = CAMERAS.get(cid, {})
            log.warning(
                f"  Not found after upgrade: {cam.get('name', cid)} "
                f"({cam.get('ip','')})"
            )

    # ── Run a fresh network scan to pick up newly discoverable cameras ────────
    # Do this after the per-camera verification so the UI shows the verification
    # results before the full scan progress bar takes over.
    SCAN_STATE.update(
        running=True, progress=10, stage=2,
        stage_label="Post-upgrade verification",
        message="Verification done — running fresh network scan…"
    )
    try:
        await run_scan()
    except Exception as scan_exc:
        log.warning(f"Post-upgrade follow-up scan error: {scan_exc}")
        SCAN_STATE.update(
            running=False, progress=100,
            message=f"Verification complete ({len(still_present)} cameras OK"
                    + (f", {len(now_missing)} missing" if now_missing else "") + ")"
        )
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
    _has_rtsp        = bool(camera.get("stream_url"))
    _rtsp_probe_ok   = bool(camera.get("rtsp_probe_ok"))
    _prefer_ffmpeg   = (native_res and _has_rtsp) or (_has_rtsp and _rtsp_probe_ok)
    if camera.get("http_snap_url") and not _prefer_ffmpeg:
        await http_snap_loop(camera_id, camera)
        return

    state        = _snap_state(camera_id)

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
    if is_hevc and stream_w >= 3840:
        out_vf = "fps=4,scale=480:-2,format=yuvj420p"
    elif is_hevc:
        out_vf = "fps=8,scale=640:-2,format=yuvj420p"
    else:
        out_vf = "fps=10,scale=640:-2,format=yuvj420p"

    SOI = bytes([0xFF, 0xD8])
    EOI = bytes([0xFF, 0xD9])

    async def _launch_snap(hw_dec: str = "", native_res: bool = False) -> None:

        """Launch ffmpeg for snapshot polling.
        native_res=True: use camera's native resolution/fps (for focus view).
        Respects CFG_ options: thread limiting, skip_nonref, low_fps_mode.
        In focus/native_res mode, Low FPS Mode and Limit Threads are bypassed
        so the user gets full quality regardless of config settings.
        """
        hw_args     = ["-c:v", hw_dec] if hw_dec else []
        # In focus mode: lift thread cap and nonref-skip for full quality,
        # even if CFG_LIMIT_THREADS / CFG_SKIP_NONREF are enabled in config.
        thread_args = [] if native_res else (["-threads", "2"] if CFG_LIMIT_THREADS else [])
        skip_args   = [] if native_res else (["-skip_frame", "nonref"] if CFG_SKIP_NONREF else [])
        hw_label    = f"hw:{hw_dec}" if hw_dec else "sw"

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
        fflags_args = (["-fflags", "+discardcorrupt"]
                       if cam_for_transport.get("needs_fflags_discardcorrupt")
                       else [])

        return await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "warning",
            *transport_args,
            *fflags_args,
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

            # Idle check — stop if nobody has polled recently
            last   = _snap_last_access.get(camera_id, 0)
            idle_s = time.monotonic() - last
            if state["frame_count"] > 0 and idle_s > 30:
                log.info(f"SNAP [{camera_id}]: idle {idle_s:.0f}s — stopping")
                return

            # Focus-mode throttle: if another camera has focus, sleep most of
            # the time so the focused camera gets the CPU.
            if _FOCUSED_CAMERA and _FOCUSED_CAMERA != camera_id:
                await asyncio.sleep(1.0)   # ~1fps while another cam is focused
                continue

            # Stagger polling start times across cameras to spread CPU spikes.
            if CFG_STAGGER_POLL and state["frame_count"] == 0:
                idx = list(_SNAP.keys()).index(camera_id) if camera_id in _SNAP else 0
                await asyncio.sleep(idx * 0.04)   # 40ms offset per camera

            # Select decoder
            # Respect CFG_HW_DECODE toggle: if disabled, skip hw entirely.
            # Otherwise pick the best available decoder for this codec from
            # the candidates probed at startup — prefers v4l2m2m (Pi) then
            # vaapi (Intel/AMD), skipping anything in _HW_UNAVAILABLE.
            hw_dec = ""
            if CFG_HW_DECODE:
                codec_lower = (stream_codec or "").lower()
                for decoder, _ in _HW_DECODER_CANDIDATES:
                    if decoder in _HW_UNAVAILABLE:
                        continue
                    # Match decoder to stream codec
                    if codec_lower in ("hevc", "h265") and "hevc" in decoder:
                        hw_dec = decoder
                        break
                    if codec_lower == "h264" and "h264" in decoder:
                        hw_dec = decoder
                        break
                if not hw_dec:
                    log.debug(f"SNAP [{camera_id}]: no hw decoder available "
                              f"for codec={stream_codec}, using software")

            proc     = await _launch_snap(hw_dec, native_res=native_res)
            state["proc"] = proc
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
            hw_tried = bool(hw_dec)

            try:
                while True:
                    # Idle check inside the read loop
                    last   = _snap_last_access.get(camera_id, 0)
                    idle_s = time.monotonic() - last
                    if frames > 10 and idle_s > 30:
                        log.info(f"SNAP [{camera_id}]: idle {idle_s:.0f}s — stopping")
                        return

                    timeout = 3.0 if (hw_tried and frames == 0) else 30.0
                    try:
                        chunk = await asyncio.wait_for(
                            proc.stdout.read(65536), timeout=timeout)
                    except asyncio.TimeoutError:
                        if hw_tried and frames == 0:
                            try: proc.kill()
                            except Exception: pass
                            try: await asyncio.wait_for(proc.wait(), timeout=2)
                            except Exception: pass
                            log.info(f"SNAP [{camera_id}]: hw decode timeout → sw")
                            _HW_UNAVAILABLE.add(hw_dec)
                            proc     = await _launch_snap()
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
                            try: proc.kill()
                            except Exception: pass
                            try: await asyncio.wait_for(proc.wait(), timeout=2)
                            except Exception: pass
                            log.info(f"SNAP [{camera_id}]: hw EOF (rc={rc}) → sw")
                            _HW_UNAVAILABLE.add(hw_dec)
                            proc     = await _launch_snap()
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
                        hw_tried = False   # got a frame → hw decode worked
                        state["frame"]       = frame
                        state["frame_time"]  = time.monotonic()
                        state["frame_count"] += 1
                        # 2.4.0-rc3.4 Bug 2 fix: per-run counter, see launch site.
                        state["current_run_frames"] = state.get("current_run_frames", 0) + 1

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

                        # Motion detection — runs only when enabled for this camera.
                        # Uses JPEG size comparison: a scene with motion has more
                        # high-frequency content and compresses to a larger file.
                        ms = _MOTION.get(camera_id)
                        if ms and ms["enabled"]:
                            motion = _detect_motion(
                                ms.get("prev_frame"), frame, CFG_MOTION_SENS)
                            ms["prev_frame"] = frame
                            now_m = time.monotonic()
                            if motion:
                                ms["last_motion"] = now_m
                                if not ms["recording"]:
                                    cam_url = build_authenticated_url(
                                        CAMERAS.get(camera_id, {}))
                                    if cam_url:
                                        asyncio.create_task(
                                            _start_recording(camera_id,
                                                CAMERAS[camera_id], cam_url))
                            elif ms["recording"]:
                                # Stop after cooldown + padding with no motion
                                idle = now_m - ms.get("last_motion", 0)
                                if idle > CFG_MOTION_COOL + CFG_MOTION_PAD:
                                    asyncio.create_task(
                                        _stop_recording(camera_id))

            except asyncio.CancelledError:
                log.info(f"SNAP [{camera_id}]: task cancelled")
                raise
            except Exception as ex:
                log.warning(f"SNAP [{camera_id}]: inner exception: {ex}")
            finally:
                stderr_t.cancel()
                try: proc.kill()
                except Exception: pass
                try: await asyncio.wait_for(proc.wait(), timeout=3)
                except Exception: pass
                try: await asyncio.wait_for(stderr_t, timeout=2)
                except Exception: pass

            # Before restarting, check idle
            last   = _snap_last_access.get(camera_id, 0)
            idle_s = time.monotonic() - last
            if frames > 0 and idle_s > 30:
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
                if (streak >= 3 and native_res
                        and state.get("transport_flip_fired")
                        and not state.get("http_snap_fired")):
                    cam_now = CAMERAS.get(camera_id, camera)
                    if cam_now.get("http_snap_url"):
                        log.warning(
                            f"SNAP [{camera_id}]: 3 consecutive 0-frame failures in "
                            f"enhanced view — RTSP non-functional, falling back to "
                            f"HTTP snap loop for this focus session"
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

                # Skip adaptive stepping if:
                # (a) user manually pinned the tier (manual_override), or
                # (b) Adaptive Quality is disabled in config — in that case the
                #     ladder exists for manual use only; the system never auto-steps.
                if ada.get("manual_override") or not CFG_ADAPTIVE_QUALITY:
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
                # Never step down if the user manually pinned the tier —
                # the adaptive system must not override an explicit user choice
                # (e.g. HEVC firmware bug causes repeated crashes but user wants
                # this tier regardless).
                # 2.4.0-rc3.1: Block B parallels Block A's gating. Previously
                # this only checked manual_override and would set
                # restart_overflow=True even when CFG_ADAPTIVE_QUALITY was
                # off — meaning the system auto-stepped on repeated restarts
                # despite the user disabling Adaptive Quality. The fast-death
                # path in Block A correctly respected the toggle; this path
                # didn't. Now both paths use the same gate.
                if ada.get("manual_override") or not CFG_ADAPTIVE_QUALITY:
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
        # reference back to None on its way out. handle_focus_set_tier then
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
            if idle_s > 30 and state["frame"] is not None:
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

async def handle_focus_set(request: web.Request) -> web.Response:
    """POST /snap/focus/{camera_id} — enter full-screen focus mode."""
    global _FOCUSED_CAMERA
    camera_id = request.match_info["camera_id"]
    if camera_id not in CAMERAS:
        return web.json_response({"error": "Camera not found"}, status=404)
    _FOCUSED_CAMERA = camera_id
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
        state["task"] = asyncio.create_task(
            snap_loop(camera_id, url, camera, native_res=True))
    return web.json_response({"status": "ok", "focused": camera_id})


async def handle_focus_clear(request: web.Request) -> web.Response:
    """DELETE /snap/focus — exit full-screen focus mode."""
    global _FOCUSED_CAMERA
    prev = _FOCUSED_CAMERA
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


async def handle_focus_set_tier(request: web.Request) -> web.Response:
    """POST /snap/focus/tier — manually pin the enhanced view to a specific
    profile index and fps cap.  Adaptive stepping is paused while a manual
    override is active.  Send profile_idx=null to resume auto mode."""
    camera_id = _FOCUSED_CAMERA
    if not camera_id:
        return web.json_response({"error": "No camera in focus"}, status=400)
    try:
        data     = await request.json()
        prof_idx = data.get("profile_idx")   # int or None = reset
        fps_cap  = data.get("fps")            # int, None, or "uncapped"
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    camera = CAMERAS.get(camera_id)
    if not camera:
        return web.json_response({"error": "Camera not found"}, status=404)

    ladder = _build_focus_ladder(camera)

    if prof_idx is None:
        # Reset to automatic
        ada = _FOCUS_ADAPTIVE.get(camera_id, {})
        ada.pop("manual_override", None)
        ada["locked"]             = False
        ada["restarts_since_lock"] = 0
        ada["tier_idx"]           = 0
        _FOCUS_ADAPTIVE[camera_id] = ada
        log.info(f"Focus [{camera_id}]: manual override cleared")
        return web.json_response({"status": "ok", "mode": "auto"})

    fps_val = None if (fps_cap is None or fps_cap == "uncapped") else int(fps_cap)

    # Find the best matching tier
    best_idx = 0
    for i, (p, f) in enumerate(ladder):
        if p == prof_idx and f == fps_val:
            best_idx = i
            break
        if p == prof_idx:   # right profile, any fps — keep as fallback
            best_idx = i

    ada = _FOCUS_ADAPTIVE.setdefault(camera_id, {
        "tier_idx": 0, "locked": False,
        "restarts_since_lock": 0, "run_start": None,
        "ladder": ladder,
    })
    prev_tier_idx = ada.get("tier_idx", 0)
    ada["tier_idx"]            = best_idx
    ada["locked"]              = True
    ada["restarts_since_lock"] = 0
    ada["manual_override"]     = True
    ada["ladder"]              = ladder
    log.info(f"Focus [{camera_id}]: manual tier [{best_idx}] "
             f"profile[{prof_idx}] fps={fps_val}")

    # If the new tier crosses a profile or fps boundary, the running ffmpeg is
    # decoding the OLD profile/fps — kill it so snap_loop's outer restart loop
    # picks up the new ada state and relaunches with the new URL+vf filter.
    # Without this, the dropdown changes but the actual stream stays the same.
    if best_idx != prev_tier_idx:
        state  = _snap_state(camera_id)
        proc   = state.get("proc")
        if proc is not None:
            try:
                proc.kill()
                log.info(f"Focus [{camera_id}]: killed ffmpeg to apply manual tier change")
            except Exception as ex:
                log.debug(f"Focus [{camera_id}]: ffmpeg kill failed (probably already dead): {ex}")

    return web.json_response({"status": "ok", "tier_idx": best_idx,
                              "profile_idx": prof_idx, "fps": fps_val})


async def handle_focus_profiles(request: web.Request) -> web.Response:
    """GET /snap/focus/profiles — return the stream profiles for the focused
    camera, so the JS can populate the resolution dropdown.

    rc2.3: response shape changed from a bare profile array to an object
    that also includes the server's current adaptive-tier state. The JS
    uses `current_tier` to seed the Resolution / FPS dropdowns and the
    `_manualTierActive` flag so that the controls reflect what's actually
    streaming — not just the dropdowns' default `selected` option.

    Without this, when a user closes Enhanced view and re-opens it, the
    server may have a preserved `manual_override` (e.g. tier 31 = profile[1])
    while the dropdowns reset to profile[0]. The result is a UI lie:
    Resolution shows "3840x2160" while the actual stream is "1280x720".

    Response shape:
        {
          "profiles": [{"idx": 0, "label": "...", "width": ..., ...}, ...],
          "current_tier": {
              "manual_override": true|false,
              "profile_idx":     int,
              "fps":             int|null   # null == uncapped
          }
        }
    """
    camera_id = _FOCUSED_CAMERA
    if not camera_id:
        return web.json_response({"profiles": [], "current_tier": None})
    camera = CAMERAS.get(camera_id)
    if not camera:
        return web.json_response({"profiles": [], "current_tier": None})
    profiles = camera.get("stream_profiles") or []
    if not profiles:
        # Synthesise from legacy stream_url / sub_stream_url
        profiles = [{
            "_url_key": "stream_url",
            "stream_width":  camera.get("stream_width"),
            "stream_height": camera.get("stream_height"),
            "stream_codec":  camera.get("stream_codec"),
        }]
        if camera.get("sub_stream_url"):
            profiles.append({
                "_url_key": "sub_stream_url",
                "stream_width":  camera.get("sub_stream_width"),
                "stream_height": camera.get("sub_stream_height"),
                "stream_codec":  camera.get("sub_stream_codec"),
            })
        # 2.4.0-rc2.8: include validated locked-stream candidates
        # ("additional_streams") in the synth fallback. rc2.8's
        # api_set_credentials builds stream_profiles directly so this
        # fallback is rarely needed for fresh cred-accepts — but
        # cameras saved by earlier builds (rc2.6/rc2.7) have
        # additional_streams populated without stream_profiles. This
        # branch covers them so the dropdown shows all entries on
        # reload without requiring the user to re-auth.
        _seen_urls = {camera.get("stream_url", ""),
                      camera.get("sub_stream_url", "")}
        for add in (camera.get("additional_streams") or []):
            au = add.get("url", "")
            if au and au not in _seen_urls:
                profiles.append({
                    "url": au,  # explicit URL — dropdown uses this
                                # over _url_key when present
                    "stream_width":  add.get("stream_width"),
                    "stream_height": add.get("stream_height"),
                    "stream_codec":  add.get("stream_codec"),
                })
                _seen_urls.add(au)
    result = []
    for i, p in enumerate(profiles):
        w = p.get("stream_width")  or 0
        h = p.get("stream_height") or 0
        c = (p.get("stream_codec") or "").upper()
        if w and h and c:
            label = f"{w}x{h} {c}"
        elif w and h:
            label = f"{w}x{h}"
        elif c:
            label = f"Stream {i+1} ({c})"
        else:
            label = f"Stream {i+1}"
        result.append({"idx": i, "label": label, "width": w, "height": h,
                       "codec": c or "?"})

    # rc2.3: read the server's current adaptive-tier state so the JS can
    # seed the Resolution / FPS dropdowns to match what's actually streaming.
    # _FOCUS_ADAPTIVE may be empty (camera just entered focus and hasn't
    # locked yet) — return None in that case so the JS uses its defaults.
    ada = _FOCUS_ADAPTIVE.get(camera_id)
    current_tier = None
    if ada and ada.get("ladder"):
        ladder    = ada["ladder"]
        tier_idx  = ada.get("tier_idx", 0)
        if 0 <= tier_idx < len(ladder):
            prof_idx, fps_val = ladder[tier_idx]
            current_tier = {
                "manual_override": bool(ada.get("manual_override", False)),
                "profile_idx":     prof_idx,
                "fps":             fps_val,   # None == uncapped
            }

    return web.json_response({"profiles": result, "current_tier": current_tier})


# ─────────────────────────────────────────────────────────────────────────────
# Motion detection + recording
# ─────────────────────────────────────────────────────────────────────────────

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
            "prev_frame":   None,   # bytes of previous JPEG for comparison
        }
    return _MOTION[camera_id]


def _detect_motion(prev_jpeg: bytes, curr_jpeg: bytes, sensitivity: int) -> bool:
    """
    Fast motion detection by comparing JPEG file sizes.
    JPEG size is strongly correlated with image entropy — a scene with motion
    has more high-frequency content and compresses less.  Size difference
    > threshold% of the smaller size → motion detected.

    sensitivity: 1-100 (higher = more sensitive, triggers on smaller changes)
    threshold%  = (100 - sensitivity) / 10  → sensitivity=15 → 8.5% threshold
    """
    if not prev_jpeg or not curr_jpeg:
        return False
    small = min(len(prev_jpeg), len(curr_jpeg))
    diff  = abs(len(curr_jpeg) - len(prev_jpeg))
    threshold_pct = (101 - sensitivity) / 10.0   # sensitivity=15 → 8.6%
    return (diff / max(small, 1)) * 100 > threshold_pct


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

async def _ensure_cam_dir(camera: dict) -> Path:
    """Create and return the recording directory for a camera."""
    folder = _cam_folder_name(camera)
    path   = MEDIA_DIR / folder
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _start_recording(camera_id: str, camera: dict, url: str) -> None:
    """Start an ffmpeg recording subprocess for this camera (stream-copy, full quality)."""
    ms = _motion_state(camera_id)
    if ms["recording"] and ms["proc"] and ms["proc"].returncode is None:
        return  # already recording
    cam_dir  = await _ensure_cam_dir(camera)
    ts       = time.strftime("%Y%m%d_%H%M%S")
    clip     = cam_dir / f"motion_{ts}.mp4"
    ms["clip_path"] = clip
    log.info(f"Motion [{camera_id}]: recording started → {clip}")
    try:
        ms["proc"] = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "warning",
            "-rtsp_transport", "tcp", "-timeout", "8000000",
            "-i", url,
            "-c", "copy",   # stream-copy: no decode/encode — nearly zero CPU
            "-movflags", "+faststart",
            str(clip),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        ms["recording"] = True
    except Exception as ex:
        log.warning(f"Motion [{camera_id}]: failed to start recording: {ex}")


async def _stop_recording(camera_id: str) -> None:
    """Gracefully stop the recording ffmpeg process."""
    ms = _motion_state(camera_id)
    if not ms["recording"]:
        return
    proc = ms.get("proc")
    if proc and proc.returncode is None:
        try:
            proc.stdin  # just accessing it is harmless
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            try: proc.kill()
            except Exception: pass
    log.info(f"Motion [{camera_id}]: recording stopped → {ms.get('clip_path')}")
    ms["recording"] = False
    ms["proc"]      = None


async def api_motion_toggle(request: web.Request) -> web.Response:
    """POST /api/cameras/{camera_id}/motion — toggle motion detection on/off."""
    camera_id = request.match_info["camera_id"]
    camera    = CAMERAS.get(camera_id)
    if not camera:
        return web.json_response({"error": "Not found"}, status=404)
    ms = _motion_state(camera_id)
    ms["enabled"] = not ms["enabled"]
    if not ms["enabled"]:
        await _stop_recording(camera_id)
    log.info(f"Motion [{camera_id}]: {'enabled' if ms['enabled'] else 'disabled'}")
    return web.json_response({"motion_enabled": ms["enabled"]})


async def api_motion_status(request: web.Request) -> web.Response:
    """GET /api/cameras/{camera_id}/motion — current motion detection state."""
    camera_id = request.match_info["camera_id"]
    ms = _motion_state(camera_id)
    return web.json_response({
        "motion_enabled": ms["enabled"],
        "recording":      ms["recording"],
        "clip_path":      str(ms["clip_path"]) if ms.get("clip_path") else None,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Storage browser
# ─────────────────────────────────────────────────────────────────────────────

async def handle_storage_page(request: web.Request) -> web.Response:
    """GET /storage — redirect to main app; storage is a JS view within the SPA."""
    raise web.HTTPFound(INGRESS_PATH + "/")


async def api_storage_list(request: web.Request) -> web.Response:
    """GET /api/storage — list cameras/files in the recordings directory."""
    import shutil as _shutil
    try:
        du  = _shutil.disk_usage(str(MEDIA_DIR.parent if not MEDIA_DIR.exists()
                                     else MEDIA_DIR))
        pct = round(du.used / du.total * 100, 1) if du.total else 0
        disk = {
            "total_gb": round(du.total / 1e9, 1),
            "used_gb":  round(du.used  / 1e9, 1),
            "free_gb":  round(du.free  / 1e9, 1),
            "pct_used": pct,
        }
    except Exception:
        disk = {"total_gb": 0, "used_gb": 0, "free_gb": 0, "pct_used": 0}

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    folders = []
    for cam_dir in sorted(MEDIA_DIR.iterdir()):
        if not cam_dir.is_dir():
            continue
        files = []
        for f in sorted(cam_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file() and f.suffix in (".mp4", ".mkv", ".jpg", ".jpeg"):
                st = f.stat()
                files.append({
                    "name":     f.name,
                    "size_mb":  round(st.st_size / 1e6, 2),
                    "mtime":    int(st.st_mtime),
                    "path":     str(f.relative_to(MEDIA_DIR)),
                })
        folders.append({
            "folder":   cam_dir.name,
            "files":    files,
            "count":    len(files),
            "size_mb":  round(sum(f["size_mb"] for f in files), 1),
        })
    return web.json_response({"disk": disk, "folders": folders})


async def api_storage_rename(request: web.Request) -> web.Response:
    """POST /api/storage/rename — rename a file or folder."""
    try:
        data     = await request.json()
        old_rel  = Path(data["old_path"])
        new_name = data["new_name"].strip()
    except Exception:
        return web.json_response({"error": "Invalid request"}, status=400)
    if not new_name or "/" in new_name or chr(92) in new_name:
        return web.json_response({"error": "Invalid name"}, status=400)
    old_abs = MEDIA_DIR / old_rel
    new_abs = old_abs.parent / new_name
    if not old_abs.exists():
        return web.json_response({"error": "Not found"}, status=404)
    if new_abs.exists():
        return web.json_response({"error": "Name already exists"}, status=409)
    try:
        old_abs.rename(new_abs)
        log.info(f"Storage: renamed {old_abs} → {new_abs}")
        return web.json_response({"status": "ok"})
    except Exception as ex:
        return web.json_response({"error": str(ex)}, status=500)


async def api_storage_move(request: web.Request) -> web.Response:
    """POST /api/storage/move — move a file to a different camera folder."""
    import shutil as _shutil
    try:
        data       = await request.json()
        src_rel    = Path(data["src_path"])
        dst_folder = data["dst_folder"].strip()
    except Exception:
        return web.json_response({"error": "Invalid request"}, status=400)
    src_abs = MEDIA_DIR / src_rel
    dst_abs = MEDIA_DIR / dst_folder / src_abs.name
    if not src_abs.exists() or not src_abs.is_file():
        return web.json_response({"error": "Source not found"}, status=404)
    dst_abs.parent.mkdir(parents=True, exist_ok=True)
    try:
        _shutil.move(str(src_abs), str(dst_abs))
        log.info(f"Storage: moved {src_abs} → {dst_abs}")
        return web.json_response({"status": "ok"})
    except Exception as ex:
        return web.json_response({"error": str(ex)}, status=500)


async def api_storage_delete(request: web.Request) -> web.Response:
    """DELETE /api/storage/file — delete a recording file."""
    try:
        data    = await request.json()
        rel     = Path(data["path"])
    except Exception:
        return web.json_response({"error": "Invalid request"}, status=400)
    abs_path = MEDIA_DIR / rel
    # Safety: only allow deleting files inside MEDIA_DIR
    try:
        abs_path.resolve().relative_to(MEDIA_DIR.resolve())
    except ValueError:
        return web.json_response({"error": "Forbidden"}, status=403)
    if not abs_path.is_file():
        return web.json_response({"error": "Not found"}, status=404)
    try:
        abs_path.unlink()
        log.info(f"Storage: deleted {abs_path}")
        return web.json_response({"status": "ok"})
    except Exception as ex:
        return web.json_response({"error": str(ex)}, status=500)


async def api_storage_download(request: web.Request) -> web.Response:
    """GET /api/storage/download?path=folder/file.mp4"""
    rel  = request.rel_url.query.get("path", "")
    if not rel:
        return web.Response(status=400)
    abs_path = MEDIA_DIR / Path(rel)
    try:
        abs_path.resolve().relative_to(MEDIA_DIR.resolve())
    except ValueError:
        return web.Response(status=403)
    if not abs_path.is_file():
        return web.Response(status=404)
    return web.FileResponse(abs_path)


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


async def api_cameras(request) -> web.Response:

    return web.json_response([_safe_cam(c) for c in CAMERAS.values()])

async def api_scan(request) -> web.Response:

    if SCAN_STATE["running"]:
        return web.json_response({"error": "Scan already running"}, status=409)
    try:
        data = await request.json()
        SCAN_OPTIONS["broad_sweep"] = bool(data.get("broad_sweep", False))
    except Exception:
        pass
    asyncio.create_task(run_scan())
    return web.json_response({"status": "started"})

async def api_scan_status(request) -> web.Response:

    return web.json_response(SCAN_STATE)


async def api_scan_cancel(request) -> web.Response:

    """POST /api/scan/cancel — request graceful abort of running scan."""
    global _SCAN_CANCELLED
    if not SCAN_STATE["running"]:
        return web.json_response({"error": "No scan running"}, status=400)
    _SCAN_CANCELLED = True
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


async def api_dvr_enum_status(request) -> web.Response:
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


async def api_set_credentials(request) -> web.Response:

    try:
        data      = await request.json()
        camera_id = data.get("camera_id", "")
        username  = data.get("username", "").strip()
        password  = data.get("password", "")
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
                def _res(c) -> int:

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
                    async def _fix_codec(cam_id=cid, auth_url=cam_url,
                                         cam_ip=ip, throttle_s=throttle_s) -> None:

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
    details = await probe_stream_details(url, proto)
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
                  # Clear the unvalidated locked_streams list — the
                  # validated subset is now in additional_streams.
                  locked_streams=[],
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


async def api_clear_credentials(request) -> web.Response:

    cid    = request.match_info["camera_id"]
    camera = CAMERAS.get(cid)
    if not camera:
        return web.json_response({"error": "Not found"}, status=404)
    camera.update(credentials=None,
                  stream_url=_strip_creds(camera.get("stream_url","")),
                  requires_credentials=True, status="needs_credentials")
    save_cameras()
    return web.json_response({"status": "ok"})

async def api_rename_camera(request) -> web.Response:

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

async def api_delete_camera(request) -> web.Response:

    cid = request.match_info["camera_id"]
    CAMERAS.pop(cid, None)
    save_cameras()
    return web.json_response({"status": "ok"})

async def api_confirm_camera(request) -> web.Response:

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


async def api_not_camera(request) -> web.Response:

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
async def api_deep_reprobe(request) -> web.Response:
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


async def api_add_camera(request) -> web.Response:

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


async def api_arp_hosts(request) -> web.Response:

    """Return the last ARP-discovered host list for the Port Scan UI."""
    return web.json_response(ARP_HOSTS)


async def api_pscan_start(request) -> web.Response:

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

async def api_pscan_status(request) -> web.Response:

    # Add current elapsed so JS can compute drift between polls
    resp = dict(PSCAN)
    if resp.get("scan_start") and resp.get("running"):
        resp["elapsed"] = round(time.time() - resp["scan_start"], 1)
    resp.pop("live_ports", None)  # send separately to avoid huge payload
    resp["live_ports"] = PSCAN.get("live_ports", [])
    return web.json_response(resp)

async def api_pscan_cancel(request) -> web.Response:

    pid = PSCAN.get("proc_pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    PSCAN.update(running=False, paused=False, message="Cancelled.", proc_pid=None)
    return web.json_response({"status": "ok"})

async def api_pscan_pause(request) -> web.Response:

    pid = PSCAN.get("proc_pid")
    if pid and PSCAN["running"] and not PSCAN["paused"]:
        try:
            os.kill(pid, signal.SIGSTOP)
            PSCAN["paused"]  = True
            PSCAN["message"] = f"Paused — {len(PSCAN['results'])} port(s) found so far."
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"status": "ok"})

async def api_pscan_resume(request) -> web.Response:

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
# JavaScript — kept as a raw string so braces, backslashes and quotes are
# all literal.  Only BASE is injected via str.replace().
# ─────────────────────────────────────────────────────────────────────────────

_JS = r"""
const BASE = '___BASE___';
const CFG_UNRESTRICTED_BROWSER = ___UNRESTRICTED___;
const CFG_ADAPTIVE_QUALITY     = ___ADAPTIVE_QUALITY___;
const STORAGE_UNRESTRICTED = ___UNRESTRICTED___;
const PROTO_ICONS = {RTSP:'📹',ONVIF:'🔭',MJPEG:'🖼️',HLS:'📡',RTMP:'📺',WebRTC:'🔗','WS-RTSP':'🔌',HTTP:'🌐',DVR:'💾'};
const PROTO_CLR   = {
  RTSP:['1e3a5f','79b8ff'],ONVIF:['2d1e4a','c09eff'],MJPEG:['1e3a30','79ffcd'],
  HLS:['3a2e1e','ffb879'],RTMP:['3a1e1e','ff7979'],WebRTC:['1e2d3a','79d4ff'],
  'WS-RTSP':['2d3a1e','b8ff79'],HTTP:['2a2a2a','aaaaaa'],DVR:['3a1e3a','ff79ff']
};
const DPORT = {RTSP:554,ONVIF:80,MJPEG:80,HLS:80,RTMP:1935,WebRTC:443,'WS-RTSP':8554};

let cameras=[], pollT=null, pscanT=null, renameId=null, _paused=false;

/* ── View switching ────────────────────────────────────────────────────────── */
function switchView(v) {
  document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
  const viewMap = {cameras: 'cameras-view', pscan: 'pscan-view', add: 'add-view', storage: 'storage-view'};
  document.getElementById(viewMap[v] || 'cameras-view').classList.add('active');
  document.getElementById('pscan-btn').classList.toggle('active', v === 'pscan');
  document.getElementById('add-btn').classList.toggle('active', v === 'add');
  document.getElementById('storage-btn').classList.toggle('active', v === 'storage');
  if (v === 'storage') loadStorage();
  if (v === 'pscan') {
    loadArpHosts();
    fetch(BASE + '/api/pscan/status').then(r => r.json()).then(s => {
      if (s.running) {
        document.getElementById('ps-start').disabled = true;
        document.getElementById('ps-pause').style.display = '';
        document.getElementById('ps-cancel').style.display = '';
        document.getElementById('ps-prog-track').style.display = '';
        _paused = s.paused || false;
        document.getElementById('ps-pause').textContent = _paused ? '▶ Resume' : '⏸ Pause';
        pollPscan();
      } else if (s.results && s.results.length) {
        // Restore completed results
        renderPorts(s.results);
        const msgEl = document.getElementById('pscan-msg');
        if (s.message) msgEl.textContent = s.message;
      }
    });
  }
}

/* ── Camera scan ───────────────────────────────────────────────────────────── */
async function startScan() {
  if (!document.getElementById('cameras-view').classList.contains('active'))
    switchView('cameras');
  const broad = document.getElementById('broad-sweep').checked;
  const r = await fetch(BASE + '/api/scan', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({broad_sweep: broad})
  });
  if (!r.ok) { const d = await r.json(); alert('Scan error: ' + (d.error || r.status)); return; }
  document.getElementById('scan-btn').disabled = true;
  document.getElementById('progress-track').style.display = '';
  document.getElementById('status-bar').classList.add('scanning');
  pollScan();
}

async function pollScan() {
  clearTimeout(pollT);
  try {
    const s = await (await fetch(BASE + '/api/scan/status')).json();
    document.getElementById('status-msg').textContent = s.message;
    document.getElementById('progress-fill').style.width = s.progress + '%';
    const badge = document.getElementById('stage-badge');
    if (s.stage && s.stage > 0) {
      badge.textContent = s.stage_label || ('Stage ' + s.stage);
      badge.style.display = '';
    } else {
      badge.style.display = 'none';
    }
    // ETA countdown — server provides eta in seconds remaining
    // We adjust client-side for time since last poll to keep it smooth
    const timerEl = document.getElementById('scan-timer');
    if (s.running) {
      const serverEta    = s.eta || 0;
      const serverElap   = s.elapsed || 0;
      const clientElap   = s.started_at ? (Date.now()/1000) - s.started_at : serverElap;
      const secondsGone  = Math.max(0, clientElap - serverElap);
      const etaAdj       = Math.max(0, Math.round(serverEta - secondsGone));
      const etaM = Math.floor(etaAdj / 60), etaSec = etaAdj % 60;
      timerEl.textContent = 'estimated ' + etaM + ':' + String(etaSec).padStart(2, '0') + ' remaining';
      timerEl.style.display = '';
    } else if (s.elapsed) {
      const m = Math.floor(s.elapsed / 60), sec = Math.round(s.elapsed) % 60;
      timerEl.textContent = 'Completed in ' + m + ':' + String(sec).padStart(2, '0');
      timerEl.style.display = '';
    } else {
      timerEl.style.display = 'none';
    }
    _updateCancelBtn(s.running);
    if (s.running) {
      pollT = setTimeout(pollScan, 1500);
    } else {
      document.getElementById('scan-btn').disabled = false;
      document.getElementById('status-bar').classList.remove('scanning');
      document.getElementById('progress-track').style.display = 'none';
      badge.style.display = 'none';
      if (s.progress >= 100) await loadCameras();
    }
  } catch(e) {
    pollT = setTimeout(pollScan, 3000);
  }
}

async function loadCameras() {
  try {
    cameras = await (await fetch(BASE + '/api/cameras')).json();
    renderGrid();
  } catch(e) {
    console.error('loadCameras error:', e);
  }
}

/* ── Snapshot polling ──────────────────────────────────────────────────────── */
/* Instead of one long-lived multipart/x-mixed-replace stream (which HA's
   nginx ingress terminates after a short time), we poll /snapshot/{id}?t=...
   every 125ms.  Each request is a normal fast HTTP round-trip.
   Debug info is logged to the browser console (open DevTools → Console). */
const _snapTimers  = {};   // camId → setTimeout handle
const _snapErrors  = {};   // camId → consecutive error count

function startSnap(camId) {
  stopSnap(camId);
  _snapErrors[camId] = 0;
  const poll = () => {
    const display = document.querySelector('[data-snap="' + camId + '"]');
    if (!display) { stopSnap(camId); return; }   // card was removed
    const loader = new Image();
    loader.onload = () => {
      _snapErrors[camId] = 0;
      // Show img, hide placeholder
      display.src         = loader.src;
      display.style.display = '';
      const ph = document.getElementById('ph-' + camId);
      if (ph) ph.style.display = 'none';
      // Next poll: 125ms (~8fps) matches server vf=fps=8
      _snapTimers[camId] = setTimeout(poll, 125);
    };
    loader.onerror = () => {
      _snapErrors[camId] = (_snapErrors[camId] || 0) + 1;
      const errs = _snapErrors[camId];
      console.warn('[AnyCam] snapshot error #' + errs + ' for ' + camId);
      if (errs === 3) {
        // After 3 consecutive errors, show placeholder
        display.style.display = 'none';
        const ph = document.getElementById('ph-' + camId);
        if (ph) { ph.querySelector('span').textContent = 'Stream unavailable'; ph.style.display = 'flex'; }
      }
      // Back off: 500ms for first few errors, 2s after 5 errors
      _snapTimers[camId] = setTimeout(poll, errs > 5 ? 2000 : 500);
    };
    loader.src = BASE + '/snapshot/' + camId + '?t=' + Date.now();
  };
  poll();   // start immediately
}

function stopSnap(camId) {
  if (_snapTimers[camId]) { clearTimeout(_snapTimers[camId]); delete _snapTimers[camId]; }
}

/* ── Scan cancel ───────────────────────────────────────────────────────────── */
async function cancelScan() {
  await fetch(BASE + '/api/scan/cancel', {method: 'POST'}).catch(() => {});
}


/* ── Open HA addon log page ─────────────────────────────────────────────────── */
function openHALog() {
  // Navigate the top-level HA window (not this ingress iframe) to the addon log page.
  window.top.location.href = window.top.location.origin + '/config/app/local_camera_discovery/logs';
}

/* ── Camera page opener (Firefox addon or direct) ────────────────────────────── */
// Firefox addon slug — if installed, its ingress is at /api/hassio_ingress/...
// We detect it by trying the supervisor addon info endpoint via a known pattern.
// Since we don't have hassio_api, we probe the Firefox ingress panel URL.
const FIREFOX_SLUG = 'firefox';
let _firefoxIngress = null;   // cached ingress path once found

async function _detectFirefox() {
  // Try the HA frontend addon page to see if firefox panel exists
  try {
    const r = await fetch(window.location.origin + '/hassio/addon/' + FIREFOX_SLUG, {
      method: 'GET', redirect: 'follow',
    });
    // If we get a 200 (the HA page rendered), Firefox is installed
    return r.ok || r.status === 200;
  } catch(e) {
    return false;
  }
}

async function openCameraPage(ip) {
  const cameraUrl = 'http://' + ip;
  // Always show the modal with options
  _showBrowserModal(ip, cameraUrl);
}

async function _showBrowserModal(ip, cameraUrl) {
  document.getElementById('browser-modal')?.remove();
  const firefoxInstalled = await _detectFirefox();
  const modal = document.createElement('div');
  modal.id = 'browser-modal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9500;display:flex;align-items:center;justify-content:center';
  const ffLabel = firefoxInstalled ? '🦊 Open in Firefox' : '🦊 Get Firefox for HA';
  const inner = document.createElement('div');
  inner.style.cssText = 'background:var(--card-bg);border:1px solid var(--border);border-radius:12px;padding:28px 32px;max-width:420px;width:90%;text-align:center';
  inner.innerHTML = '<div style="font-size:1.5rem;margin-bottom:10px">🌐</div>'
    + '<div style="font-weight:600;margin-bottom:6px">Open Camera Page</div>'
    + '<div style="font-size:.82rem;color:var(--text-dim);margin-bottom:20px">' + esc(cameraUrl) + '</div>'
    + '<div style="display:flex;flex-direction:column;gap:10px">'
    + '<a class="btn btn-primary" id="bmNewTab" href="' + esc(cameraUrl) + '" target="_blank" rel="noopener">↗ Open in New Tab</a>'
    + '<button class="btn btn-secondary" id="bmFirefox">' + ffLabel + '</button>'
    + '<button class="btn btn-ghost" id="bmCancel">Cancel</button>'
    + '</div>';
  modal.appendChild(inner);
  document.body.appendChild(modal);
  const close = () => modal.remove();
  modal.querySelector('#bmNewTab').addEventListener('click', close);
  modal.querySelector('#bmCancel').addEventListener('click', close);
  modal.querySelector('#bmFirefox').addEventListener('click', () => {
    close();
    if (firefoxInstalled) _openInFirefox(ip);
    else _promptGetFirefox();
  });
  modal.addEventListener('click', e => { if (e.target === modal) close(); });
}

function _openInFirefox(ip) {
  // Navigate to the Firefox HA addon panel — user can then type the camera IP
  // (we cannot inject a URL into Firefox via ingress directly)
  const ffUrl = window.location.origin + '/hassio/ingress/' + FIREFOX_SLUG;
  const win = window.open(ffUrl, '_blank');
  // Show a toast telling the user to navigate to the IP
  setTimeout(() => showToast('Firefox opened — navigate to http://' + ip), 600);
}

function _promptGetFirefox() {
  document.getElementById('browser-modal')?.remove();
  const modal = document.createElement('div');
  modal.id = 'browser-modal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9500;display:flex;align-items:center;justify-content:center';
  const inner = document.createElement('div');
  inner.style.cssText = 'background:var(--card-bg);border:1px solid var(--border);border-radius:12px;padding:28px 32px;max-width:420px;width:90%;text-align:center';
  inner.innerHTML = '<div style="font-size:1.5rem;margin-bottom:10px">🦊</div>'
    + '<div style="font-weight:600;margin-bottom:8px">Firefox not installed</div>'
    + '<div style="font-size:.82rem;color:var(--text-dim);margin-bottom:20px">'
    + 'The Firefox add-on lets you browse camera pages inside Home Assistant. '
    + 'Add the repository from mincka/ha-addons and install Firefox.</div>'
    + '<div style="display:flex;gap:10px;justify-content:center">'
    + '<a class="btn btn-primary" id="ffGetIt" href="' + window.location.origin + '/hassio/store" target="_blank">Go Get It</a>'
    + '<button class="btn btn-ghost" id="ffCancel">Cancel</button>'
    + '</div>';
  modal.appendChild(inner);
  document.body.appendChild(modal);
  const close = () => modal.remove();
  modal.querySelector('#ffGetIt').addEventListener('click', close);
  modal.querySelector('#ffCancel').addEventListener('click', close);
  modal.addEventListener('click', e => { if (e.target === modal) close(); });
}



/* ── Status dot ────────────────────────────────────────────────────────────── */
let _lastLogTime = 0;
async function pollStatusDot() {
  try {
    const d   = await (await fetch(BASE + '/api/logs?since=' + _lastLogTime)).json();
    const icon = document.getElementById('status-cam-icon');
    if (!icon) return;
    if (d.status === 'error') {
      icon.style.stroke = '#e53935';   // red — errors
    } else if (d.status === 'warning') {
      icon.style.stroke = '#f9c700';   // yellow — warnings
    } else {
      icon.style.stroke = '#43a047';   // green — all clear
    }
    if (d.entries && d.entries.length) {
      _lastLogTime = d.entries[d.entries.length - 1].t;
    }
  } catch(e) {}
}
setInterval(pollStatusDot, 5000);
pollStatusDot();

/* ── Cancel button visibility ─────────────────────────────────────────────── */
/* Show the cancel button while scan is running */
const _origPollScan = typeof pollScan !== 'undefined' ? pollScan : null;
function _updateCancelBtn(running) {
  const btn = document.getElementById('scan-cancel-btn');
  if (btn) btn.style.display = running ? '' : 'none';
}

/* ── Log view ──────────────────────────────────────────────────────────────── */

/* ── Storage navigation state ─────────────────────────────────────────────── */
let _storHistory  = [null];   // null = root, string = folder name
let _storHistIdx  = 0;
let _storCurrent  = null;     // null = root view, string = folder name

function _storNavTo(folder) {
  // Trim forward history when navigating to a new place
  _storHistory = _storHistory.slice(0, _storHistIdx + 1);
  _storHistory.push(folder);
  _storHistIdx = _storHistory.length - 1;
  _storCurrent = folder;
  _renderStorageView();
}

function storNavBack() {
  if (_storHistIdx <= 0) return;
  _storHistIdx--;
  _storCurrent = _storHistory[_storHistIdx];
  _renderStorageView();
}

function storNavUp() {
  if (_storCurrent === null) return;   // already at root (ceiling)
  if (STORAGE_UNRESTRICTED) {
    // In unrestricted mode _storCurrent is a full path string
    const parts = _storCurrent.replace(/\/+$/, '').split('/');
    parts.pop();
    const parent = parts.join('/') || '/';
    _storNavTo(parent === '/' ? null : parent);
  } else {
    // Scoped mode: two levels max (null = /media/anycam root, string = subfolder)
    // Any subfolder is one level below root, so up always goes to root
    _storNavTo(null);
  }
}

/* Sort state */
let _storSortKey = 'name', _storSortAsc = true;

function storSort(key) {
  if (_storSortKey === key) _storSortAsc = !_storSortAsc;
  else { _storSortKey = key; _storSortAsc = key === 'name'; }
  // Update header arrows
  ['name','date','type','size'].forEach(k => {
    const el = document.getElementById('sorth-' + k);
    if (!el) return;
    const base = k.charAt(0).toUpperCase() + k.slice(1);
    const labels = {name:'Name',date:'Date Modified',type:'Type',size:'Size'};
    el.innerHTML = labels[k] + (k === _storSortKey ? (' ' + (_storSortAsc ? '&#x25B2;' : '&#x25BC;')) : '');
  });
  _renderStorageView();
}

function _updateNavButtons() {
  const back    = document.getElementById('stor-back-btn');
  const up      = document.getElementById('stor-up-btn');
  const sep     = document.getElementById('stor-path-sep');
  const folderEl= document.getElementById('stor-path-folder');
  if (!back) return;
  back.disabled = _storHistIdx <= 0;
  up.disabled   = _storCurrent === null;
  if (sep && folderEl) {
    if (_storCurrent === null) {
      sep.style.display    = 'none';
      folderEl.textContent = '';
    } else {
      sep.style.display    = 'inline';
      // In unrestricted mode show only the last path component
      folderEl.textContent = STORAGE_UNRESTRICTED
        ? (_storCurrent.replace(/\/+$/, '').split('/').filter(Boolean).pop() || '/')
        : _storCurrent;
    }
  }
}

/* ── Editable path bar ────────────────────────────────────────────────────── */
function _pathBarEdit() {
  const bc = document.getElementById('stor-breadcrumb');
  if (!bc || bc.querySelector('#stor-path-input')) return;  // already editing
  // Compute the current full display path
  const root = STORAGE_UNRESTRICTED ? '/' : '/media/anycam';
  let fullPath = root;
  if (_storCurrent !== null) {
    fullPath = STORAGE_UNRESTRICTED ? _storCurrent : (root + '/' + _storCurrent);
  }
  // Replace breadcrumb contents with an input
  bc.innerHTML = '';
  const inp = document.createElement('input');
  inp.id = 'stor-path-input';
  inp.value = fullPath;
  inp.style.cssText = 'width:100%;border:none;outline:none;font-size:.82rem;color:#111;background:transparent;padding:0';
  bc.appendChild(inp);
  inp.focus();
  // Place cursor at end
  inp.setSelectionRange(inp.value.length, inp.value.length);

  function _commit() {
    const val = inp.value.trim().replace(/\/+$/, '') || '/';
    _pathBarCommit(val);
  }
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); _commit(); }
    if (e.key === 'Escape') { _renderBreadcrumb(); }
  });
  inp.addEventListener('blur', _commit);
}

function _pathBarCommit(path) {
  // Validate the path against known data
  if (!_storageData) { _renderBreadcrumb(); return; }
  const root = STORAGE_UNRESTRICTED ? '/' : '/media/anycam';
  const rootNorm = root.replace(/\/+$/, '');
  const pathNorm = path.replace(/\/+$/, '') || '/';

  if (pathNorm === rootNorm || pathNorm === '/media/anycam' || pathNorm === '/') {
    // Navigating to the root ceiling
    _storNavTo(null);
    return;
  }

  if (STORAGE_UNRESTRICTED) {
    // Accept any path string in unrestricted mode — server will validate
    _storNavTo(pathNorm);
    return;
  }

  // Scoped mode: path must be /media/anycam or /media/anycam/<folder>
  if (!pathNorm.startsWith(rootNorm + '/')) {
    alert('Folder not found: ' + path + '\n\nBrowser is scoped to ' + root);
    _renderBreadcrumb();
    return;
  }
  const sub = pathNorm.slice(rootNorm.length + 1);
  const folders = (_storageData.folders || []).map(f => f.folder || f.name);
  if (!folders.includes(sub)) {
    alert('Folder not found: ' + path);
    _renderBreadcrumb();
    return;
  }
  _storNavTo(sub);
}

function _renderBreadcrumb() {
  const bc = document.getElementById('stor-breadcrumb');
  if (!bc) return;
  const rootLabel = STORAGE_UNRESTRICTED ? '/' : '/media/anycam';
  bc.innerHTML =
    '<span id="stor-path-root" onclick="_storNavTo(null)"'
    + ' style="cursor:pointer;padding:2px 6px;border-radius:3px;color:#0066cc"'
    + ' title="' + rootLabel + '">' + rootLabel + '</span>'
    + '<span id="stor-path-sep" style="display:none;color:#888;padding:0 2px">&rsaquo;</span>'
    + '<span id="stor-path-folder" style="font-weight:600;color:#111;padding:2px 4px"></span>';
  // Re-apply click-to-edit on the whole bar
  bc.onclick = e => { if (!e.target.closest('#stor-path-root')) _pathBarEdit(); };
  _updateNavButtons();
}


function renderStorage(d) {
  const disk = d.disk || {};
  const pct  = disk.pct_used || 0;
  const lbl  = document.getElementById('disk-label');
  const fill = document.getElementById('disk-bar-fill');
  if (lbl)  lbl.textContent = pct + '% used — ' + (disk.free_gb || 0) + ' GB free of ' + (disk.total_gb || 0) + ' GB';
  if (fill) {
    fill.style.width      = Math.min(pct, 100) + '%';
    fill.style.background = pct > 90 ? 'var(--red)' : pct > 70 ? 'var(--orange)' : 'var(--primary)';
  }
  _storHistory = [null]; _storHistIdx = 0; _storCurrent = null;
  _renderBreadcrumb();
  _renderStorageView();
}

function _sortRows(rows) {
  return rows.slice().sort((a, b) => {
    let av, bv;
    if (_storSortKey === 'name')      { av = a.name.toLowerCase(); bv = b.name.toLowerCase(); }
    else if (_storSortKey === 'date') { av = a.mtime || 0;         bv = b.mtime || 0; }
    else if (_storSortKey === 'size') { av = a.size_mb || 0;        bv = b.size_mb || 0; }
    else if (_storSortKey === 'type') { av = a._type || '';         bv = b._type || ''; }
    else { av = ''; bv = ''; }
    if (av < bv) return _storSortAsc ? -1 :  1;
    if (av > bv) return _storSortAsc ?  1 : -1;
    return 0;
  });
}

function _renderStorageView() {
  // Refresh breadcrumb text (don't interrupt if user is actively editing)
  if (!document.getElementById('stor-path-input')) _renderBreadcrumb();
  _updateNavButtons();
  const list = document.getElementById('storage-list');
  if (!list || !_storageData) return;
  const folders = _storageData.folders || [];

  if (_storCurrent === null) {
    // Root: show all camera folders as rows
    if (!folders.length) {
      list.innerHTML = '<div class="stor-empty-msg">📁<br><br>No recordings yet.<br>'
        + 'Enable motion detection on a camera card to start recording.</div>';
      return;
    }
    // Annotate for sorting
    const rows = folders.map(f => ({
      name: f.folder, _type: 'Folder', size_mb: f.size_mb, mtime: 0,
      count: f.count, _isFolder: true
    }));
    const folderFrag = document.createDocumentFragment();
    _sortRows(rows).forEach(f => {
      const row = document.createElement('div');
      row.className = 'stor-row stor-row-folder';
      row.innerHTML = '<div class="stor-row-name">'
        + '<span style="font-size:1rem;flex-shrink:0;margin-right:4px">📁</span>'
        + '<span class="stor-row-name-text editable">' + esc(f.name) + '</span></div>'
        + '<div class="stor-row-date"></div>'
        + '<div class="stor-row-type">File folder</div>'
        + '<div class="stor-row-size"></div>'
        + '<div class="stor-row-acts"><span style="font-size:.72rem;color:#888">'
        + f.count + ' clip' + (f.count !== 1 ? 's' : '') + '</span></div>';
      // Single-click anywhere on row → navigate into folder
      // Double-click on name → rename
      let _folderClickTimer = null;
      row.addEventListener('click', e => {
        if (e.detail === 2) return;   // let dblclick handle it
        clearTimeout(_folderClickTimer);
        _folderClickTimer = setTimeout(() => _storNavTo(f.name), 200);
      });
      row.querySelector('.stor-row-name-text').addEventListener('dblclick', e => {
        clearTimeout(_folderClickTimer);
        storRenameFolder(f.name);
      });
      row.style.cursor = 'pointer';
      row.addEventListener('dragover', e => e.preventDefault());
      row.addEventListener('drop',     e => storDrop(e, f.name));
      folderFrag.appendChild(row);
    });
    list.innerHTML = '';
    list.appendChild(folderFrag);
  } else {
    // Folder view: show files
    const folder = folders.find(f => f.folder === _storCurrent);
    const files  = folder ? folder.files : [];
    if (!files.length) {
      list.innerHTML = '<div class="stor-empty-msg">🎬<br><br>No recordings in this folder.</div>';
      return;
    }
    const rows = files.map(f => ({
      ...f, _type: f.name.endsWith('.mp4') ? 'MP4 Video' : f.name.endsWith('.mkv') ? 'MKV Video' : 'File',
      _isFolder: false
    }));
    const fileFrag = document.createDocumentFragment();
    _sortRows(rows).forEach(f => {
      const path = _storCurrent + '/' + f.name;
      const dt   = f.mtime ? new Date(f.mtime * 1000).toLocaleString() : '';
      const sz   = f.size_mb ? f.size_mb + ' MB' : '';
      const row  = document.createElement('div');
      row.className = 'stor-row';
      row.draggable = true;
      row.innerHTML = '<div class="stor-row-name"><span>🎬</span>'
        + '<span class="stor-row-name-text editable">' + esc(f.name) + '</span></div>'
        + '<div class="stor-row-date">' + dt + '</div>'
        + '<div class="stor-row-type">' + esc(f._type) + '</div>'
        + '<div class="stor-row-size">' + sz + '</div>'
        + '<div class="stor-row-acts">'
        + '<a class="stor-dl" href="' + BASE + '/api/storage/download?path='
        + encodeURIComponent(path) + '" download title="Download" style="font-size:.9rem;text-decoration:none">⬇</a>'
        + '&nbsp;<span class="stor-del" title="Delete" style="cursor:pointer;font-size:.9rem;color:#c00">🗑</span>'
        + '</div>';
      // Single-click on name → download; double-click → rename
      const fileNameEl = row.querySelector('.stor-row-name-text');
      let _fileClickTimer = null;
      fileNameEl.addEventListener('click', e => {
        if (e.detail === 2) return;
        clearTimeout(_fileClickTimer);
        _fileClickTimer = setTimeout(() => {
          const a = document.createElement('a');
          a.href = BASE + '/api/storage/download?path=' + encodeURIComponent(path);
          a.download = f.name;
          a.click();
        }, 200);
      });
      fileNameEl.addEventListener('dblclick', e => {
        clearTimeout(_fileClickTimer);
        storRenameFile(path, f.name);
      });
      fileNameEl.style.cursor = 'pointer';
      row.querySelector('.stor-del').addEventListener('click', () => storDeleteFile(path));
      row.addEventListener('dragstart', e => storDragStart(e, path, _storCurrent));
      fileFrag.appendChild(row);
    });
    list.innerHTML = '';
    list.appendChild(fileFrag);
  }
}

/* Called after renderGrid() to start polling for all visible snap cameras */
function initSnaps() {
  document.querySelectorAll('[data-snap]').forEach(img => {
    // Click-to-focus: open enhanced view on click
    img.style.cursor = 'pointer';
    img.onclick = () => openFocus(img.dataset.snap);
    startSnap(img.dataset.snap);
  });
}

/* ── Motion detection state (mirrored from server) ──────────────────────── */
const _motionEnabled = {};   // camId → bool
const _recording     = {};   // camId → bool

async function toggleMotion(camId) {
  try {
    const r = await fetch(BASE + '/api/cameras/' + camId + '/motion', {method: 'POST'});
    const d = await r.json();
    _motionEnabled[camId] = d.motion_enabled;
    _recording[camId]     = d.recording || false;
    // Rebuild just this card
    const cam = cameras.find(c => c.id === camId);
    if (cam) updateCard(cam);
  } catch(e) { console.error('toggleMotion error:', e); }
}

/* Poll motion status for all ready cameras every 3s */
function pollMotion() {
  cameras.filter(c => c.status === 'ready' && _motionEnabled[c.id]).forEach(async cam => {
    try {
      const d = await (await fetch(BASE + '/api/cameras/' + cam.id + '/motion')).json();
      const wasRec = _recording[cam.id];
      _recording[cam.id] = d.recording;
      if (wasRec !== d.recording) updateCard(cam);   // refresh button state
    } catch(e) {}
  });
}
setInterval(pollMotion, 3000);

/* ── Focus / enhanced view ───────────────────────────────────────────────── */
let _focusCamId   = null;
let _focusTimer   = null;
let _focus4kWarnTimer = null;   // auto-dismiss timer for the 4K-fallback toast

async function openFocus(camId) {
  const cam = cameras.find(c => c.id === camId);
  if (!cam || cam.status !== 'ready') return;

  document.getElementById('focus-overlay').style.display = 'flex';
  _startFocusPoll(camId, cam);
}

async function _startFocusPoll(camId, cam) {
  _focusCamId   = camId;
  _focusCurProf = 0;
  // Tell server: enter focus mode (other cams throttle, this cam goes native res)
  await fetch(BASE + '/snap/focus/' + camId, {method: 'POST'}).catch(() => {});
  // Load profile list for the resolution dropdown (async, non-blocking)
  _loadFocusProfiles();

  const img    = document.getElementById('focus-img');
  const infoEl = document.getElementById('focus-info');
  const codec  = (cam.stream_codec || '?').toUpperCase();
  const name   = displayName(cam);

  // Show placeholder until first real measurements arrive
  infoEl.textContent = name + ' — loading…';

  // Live measurement state.
  // fetch() is used instead of Image() so we can read X-Frame-Count response
  // header and only count frames that are actually new from ffmpeg.
  // Polling with Image() measures delivery rate (~16fps), not production rate.
  let _lastFrameCount = -1;   // server frame_count from last response
  let _newFrames      = 0;    // new frames seen in current 1-second window
  let _fpsWindowStart = performance.now();
  let _liveFps        = null;
  let _liveRes        = null;
  let _stepRes        = null;  // current ladder tier resolution from X-Step-Res header
  let _stepFps        = null;  // current ladder tier fps from X-Step-FPS header
  let _prevBlobUrl    = null;
  // 2.4.0-rc3.2: track snap_mode so we can show a one-time toast when
  // the server transitions RTSP → HTTP-snap fallback. Without this the
  // dropdown silently disables (the existing tooltip is hover-gated and
  // most users never see it). null = first response, no transition yet.
  let _lastSnapMode   = null;
  // 2.4.0-rc3.3 Bug B fix: track stream status so we show "Connecting…" /
  // "Switching transport…" toasts during the retry-cycle window where
  // ffmpeg is crashing. Without this, the focus view appears frozen.
  let _lastStreamStatus = null;

  // Info bar format:
  //   Name — Actual Feed: WxH · X fps  [Adapted Quality: WxH · fps]
  // "Actual Feed"     = measured from actual received JPEG + frame count.
  // "Adapted Quality" = current adaptive ladder tier from server headers.
  //                     Only shown when adaptive_quality config is on OR
  //                     the user has manually picked a resolution/fps tier.
  function _updateInfoBar() {
    const realRes  = _liveRes  || '…';
    const realFps  = _liveFps  !== null ? _liveFps + ' fps' : 'measuring…';
    const stepRes  = _stepRes  || '…';
    const stepFpsS = _stepFps  !== null
      ? (_stepFps === 'uncapped' ? 'uncapped' : _stepFps + ' fps')
      : '…';
    // Only show Adapted Quality when using ffmpeg (rtsp mode) — in http
    // fallback mode the controls are disabled so Adapted Quality is irrelevant.
    const httpMode    = document.querySelector('.focus-ctrl-group select')?.disabled || false;
    const showAdapted = !httpMode && (_manualTierActive
      || (typeof CFG_ADAPTIVE_QUALITY !== 'undefined' && CFG_ADAPTIVE_QUALITY));
    infoEl.innerHTML =
      name + ' — ' +
      '<b>Actual Feed:</b> ' + realRes + ' · ' + realFps +
      (showAdapted
        ? ' &nbsp;<b>Adapted Quality:</b> ' + stepRes + ' · ' + stepFpsS
        : '');
  }

  // Fetch-based polling at 60ms. X-Frame-Count tells us when a new frame
  // has actually been produced by ffmpeg vs the same buffered frame re-served.
  // X-Step-Res / X-Step-FPS carry the current adaptive ladder tier.
  const poll = () => {
    if (_focusCamId !== camId) return;
    fetch(BASE + '/snapshot/' + camId + '?t=' + Date.now() + '&focus=1')
      .then(resp => {
        if (!resp.ok) return null;
        const serverCount = parseInt(resp.headers.get('X-Frame-Count') || '-1');
        const stepRes     = resp.headers.get('X-Step-Res');
        const stepFps     = resp.headers.get('X-Step-FPS');
        const snapMode    = resp.headers.get('X-Snap-Mode') || 'rtsp';
        // 2.4.0-rc3.3 Bug B fix: X-Stream-Status surfaces the retry phase so
        // the user sees "Connecting…" or "Switching transport…" during the
        // 15-30s window where ffmpeg keeps crashing on 0-frame failures
        // before the http_snap fallback engages. Without this status, the
        // focus view appears frozen on the last cached frame with no
        // explanation. Possible values:
        //   ok                  — normal (frames flowing or fresh start, no toast)
        //   connecting          — ffmpeg has crashed at least once, still trying TCP
        //   switching_transport — TCP failed 3×, now trying UDP transport
        //   http_fallback       — both transports gave up, in http_snap mode
        //                         (handled separately via the existing httpFallback path)
        const streamStatus = resp.headers.get('X-Stream-Status') || 'ok';
        if (stepRes) {
          // Detect 4K → smaller fallback so we can surface a one-time message.
          // _stepRes is the previously seen tier resolution; if it was 4K-class
          // (≥3840 wide) and the new tier is smaller, the adaptive controller
          // just stepped down due to fast-death (CPU couldn't keep up with 4K).
          //
          // rc2.4: Only surface the toast when the step-down is *automatic*.
          // When _manualTierActive is true, the user is driving the resolution
          // change themselves and the popup is misleading — it implies the
          // system is auto-degrading when in fact the user just clicked a
          // smaller resolution from the dropdown. Gating on !_manualTierActive
          // suppresses the popup in that case while preserving it for the
          // genuine auto-degrade path (when the adaptive controller steps
          // down on its own due to repeated EOF / fast-death).
          if (_stepRes && _stepRes !== stepRes && !_manualTierActive) {
            const oldW = parseInt((_stepRes.split('x')[0]) || '0');
            const newW = parseInt((stepRes.split('x')[0])  || '0');
            if (oldW >= 3840 && newW > 0 && newW < oldW) {
              const warnEl = document.getElementById('focus-warning');
              const txt    = document.getElementById('focus-warn-text');
              if (warnEl && txt) {
                txt.textContent = '4K too demanding for this hardware — falling back to secondary stream';
                warnEl.style.display = 'flex';
                clearTimeout(_focus4kWarnTimer);
                _focus4kWarnTimer = setTimeout(() => {
                  warnEl.style.display = 'none';
                }, 5000);
              }
            }
          }
          _stepRes = stepRes;
        }
        if (stepFps) _stepFps = stepFps;
        // Disable resolution/fps controls when in http_snap fallback —
        // profile switching is impossible via HTTP snapshot endpoints.
        const httpFallback = (snapMode === 'http');
        // 2.4.0-rc3.2: surface the fallback transition as a visible toast
        // so the user knows WHY the dropdown just greyed out. The
        // existing hover-tooltip on the disabled select wasn't enough —
        // most users don't think to hover over a disabled control. We
        // reuse the focus-warning element that already exists for the
        // 4K-too-demanding case. Toast stays visible the whole time
        // we're in HTTP fallback; clears the moment RTSP comes back.
        if (_lastSnapMode !== null && _lastSnapMode !== snapMode) {
          const warnEl = document.getElementById('focus-warning');
          const txt    = document.getElementById('focus-warn-text');
          if (warnEl && txt) {
            if (httpFallback) {
              txt.textContent = 'Live RTSP stream unavailable — showing periodic snapshots from this camera';
              warnEl.style.display = 'flex';
              // Don't auto-hide; this state persists for the whole focus session
              clearTimeout(_focus4kWarnTimer);
            } else {
              // Transitioned back to RTSP — clear the warning if it's ours
              if (txt.textContent.indexOf('periodic snapshots') !== -1) {
                warnEl.style.display = 'none';
              }
            }
          }
        }
        _lastSnapMode = snapMode;
        // 2.4.0-rc3.3 Bug B fix: surface stream-status transitions so the
        // user sees what's happening during the retry phase. The
        // "connecting" and "switching_transport" states fire during the
        // 15-30s window between the first ffmpeg failure and either
        // recovery or HTTP-snap fallback — without this, the focus view
        // looks frozen on the last cached frame with no explanation.
        // Suppress when snapMode is already 'http' since the existing
        // httpFallback toast covers that state more specifically.
        if (_lastStreamStatus !== streamStatus && snapMode !== 'http') {
          const warnEl = document.getElementById('focus-warning');
          const txt    = document.getElementById('focus-warn-text');
          if (warnEl && txt) {
            // Only act on this status if our current message isn't already
            // a higher-priority one (4K-too-demanding, http_snap fallback).
            const curText = txt.textContent || '';
            const isOurStatus = (curText.indexOf('Connecting') !== -1 ||
                                 curText.indexOf('Switching transport') !== -1);
            const isFreshSlot = (warnEl.style.display !== 'flex' || isOurStatus);
            if (streamStatus === 'connecting' && isFreshSlot) {
              txt.textContent = 'Connecting to RTSP stream…';
              warnEl.style.display = 'flex';
              clearTimeout(_focus4kWarnTimer);  // we manage our own lifecycle
            } else if (streamStatus === 'switching_transport' && isFreshSlot) {
              txt.textContent = 'Switching transport (TCP → UDP) — camera does not support TCP RTSP';
              warnEl.style.display = 'flex';
              clearTimeout(_focus4kWarnTimer);
            } else if (streamStatus === 'ok' && isOurStatus) {
              // Clear our toast when stream recovers
              warnEl.style.display = 'none';
            }
          }
        }
        _lastStreamStatus = streamStatus;
        const ctrlGroups = document.querySelectorAll('.focus-ctrl-group');
        const autoBtn    = document.querySelector('.focus-auto-btn');
        ctrlGroups.forEach(g => {
          const sel = g.querySelector('select');
          if (sel) {
            sel.disabled = httpFallback;
            sel.title    = httpFallback
              ? 'Stream switching unavailable — RTSP not accessible on this camera'
              : '';
            g.style.opacity = httpFallback ? '0.4' : '1';
          }
        });
        if (autoBtn) {
          autoBtn.disabled = httpFallback;
          autoBtn.style.opacity = httpFallback ? '0.4' : '1';
        }
        return resp.blob().then(blob => ({ blob, serverCount }));
      })
      .then(result => {
        if (!result || _focusCamId !== camId) return;
        const { blob, serverCount } = result;
        const isNew = serverCount >= 0 && serverCount !== _lastFrameCount;
        if (isNew) {
          _lastFrameCount = serverCount;
          _newFrames++;
          const blobUrl = URL.createObjectURL(blob);
          if (_prevBlobUrl) URL.revokeObjectURL(_prevBlobUrl);
          _prevBlobUrl = blobUrl;
          img.onload = () => {
            if (img.naturalWidth && img.naturalHeight) {
              _liveRes = img.naturalWidth + 'x' + img.naturalHeight;
            }
          };
          img.src = blobUrl;
        }
        // Update FPS display once per second
        const now     = performance.now();
        const elapsed = (now - _fpsWindowStart) / 1000;
        if (elapsed >= 1.0) {
          const measuredFps = Math.round(_newFrames / elapsed);
          // Grace period: only zero out fps if no frames for >4 seconds.
          // During a 2s restart gap the fps would otherwise flash to 0.
          if (measuredFps > 0 || elapsed >= 4.0) {
            _liveFps = measuredFps > 0 ? measuredFps : null;  // null = show "…"
          }
          _newFrames      = 0;
          _fpsWindowStart = now;
          _updateInfoBar();
        }
      })
      .catch(() => {})
      .finally(() => {
        if (_focusCamId === camId) _focusTimer = setTimeout(poll, 60);
      });
  };
  poll();
}

async function closeFocus() {
  _focusCamId = null;
  clearTimeout(_focusTimer);
  clearTimeout(_focus4kWarnTimer);
  await fetch(BASE + '/snap/focus', {method: 'DELETE'}).catch(() => {});
  document.getElementById('focus-overlay').style.display = 'none';
  document.getElementById('focus-img').src = '';
  document.getElementById('focus-warning').style.display = 'none';
  // Reset dropdowns for next open
  const rs = document.getElementById('focus-res-sel');
  const fs = document.getElementById('focus-fps-sel');
  if (rs) rs.value = '';
  if (fs) fs.value = 'uncapped';
}

// Close focus on Escape key
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && _focusCamId) closeFocus();
});

// When the browser tab returns to focus after being backgrounded, the browser
// may have throttled or dropped pending image requests — resetting _snapErrors
// prevents those stale failures from triggering "Stream unavailable" on the card.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    Object.keys(_snapErrors).forEach(camId => { _snapErrors[camId] = 0; });
  }
});

/* ── Focus manual resolution/fps controls ─────────────────────────────── */
// Populated when openFocus() runs
let _focusProfiles    = [];     // [{idx, label, width, height, codec}]
let _focusCurProf     = 0;     // currently selected profile index
let _manualTierActive = false; // true when user has manually picked res/fps

async function _loadFocusProfiles() {
  try {
    const r = await fetch(BASE + '/snap/focus/profiles');
    if (!r.ok) return;
    const data = await r.json();

    // rc2.3: response is now an object {profiles, current_tier}.
    // Older shape was a bare array — handle both for safety even though
    // server and JS ship together (defensive against split deployments
    // and stale browser caches across upgrades).
    let profiles, currentTier;
    if (Array.isArray(data)) {
      profiles    = data;
      currentTier = null;
    } else {
      profiles    = data.profiles    || [];
      currentTier = data.current_tier || null;
    }
    if (profiles.length === 0) return;  // keep placeholder if empty

    _focusProfiles = profiles;
    const sel = document.getElementById('focus-res-sel');
    if (!sel) return;
    // Rebuild options from real profile data
    sel.innerHTML = '';
    _focusProfiles.forEach(p => {
      const opt = document.createElement('option');
      opt.value = String(p.idx);
      opt.textContent = p.label;
      sel.appendChild(opt);
    });

    // rc2.3: seed both dropdowns from the server's current adaptive-tier
    // state. Without this, re-entering enhanced view after a manual tier
    // change resets the dropdowns to profile[0]/uncapped while the server
    // is still streaming whatever the user last selected — the dropdowns
    // become a UI lie. When current_tier is null (server hasn't locked
    // a tier yet), fall through to the default "0 / uncapped".
    const fpsSel = document.getElementById('focus-fps-sel');
    if (currentTier && currentTier.profile_idx !== undefined) {
      // Resolution: only set if the profile_idx is actually in the dropdown.
      const profIdxStr = String(currentTier.profile_idx);
      const optExists  = _focusProfiles.some(p => String(p.idx) === profIdxStr);
      if (optExists) {
        sel.value      = profIdxStr;
        _focusCurProf  = currentTier.profile_idx;
      } else {
        sel.value      = '0';
        _focusCurProf  = 0;
      }
      // FPS: server returns null for uncapped, int for a cap.
      if (fpsSel) {
        const fpsVal = currentTier.fps;
        const newVal = (fpsVal === null || fpsVal === undefined)
                         ? 'uncapped' : String(fpsVal);
        // Only set if the option exists (defensive — in case the FPS
        // dropdown was changed in a future build to have fewer rungs).
        const fpsOptExists = Array.from(fpsSel.options)
                                  .some(o => o.value === newVal);
        fpsSel.value = fpsOptExists ? newVal : 'uncapped';
      }
      // If it's a manual override on the server, the user previously
      // pinned a tier — surface that immediately so "Adapted Quality"
      // shows up without waiting for the next dropdown change.
      _manualTierActive = !!currentTier.manual_override;
    } else {
      sel.value      = '0';
      _focusCurProf  = 0;
      if (fpsSel) fpsSel.value = 'uncapped';
      _manualTierActive = false;
    }
  } catch(e) {}
}

async function focusPickRes(val) {
  const profIdx = parseInt(val);
  if (isNaN(profIdx)) return;
  _focusCurProf = profIdx;
  _manualTierActive = true;
  const fps = document.getElementById('focus-fps-sel')?.value || 'uncapped';
  await _applyFocusTier(profIdx, fps === 'uncapped' ? null : parseInt(fps));
}

async function focusPickFps(val) {
  const fps = val === 'uncapped' ? null : parseInt(val);
  _manualTierActive = true;
  await _applyFocusTier(_focusCurProf, fps);
}

async function focusResetAuto() {
  await fetch(BASE + '/snap/focus/tier', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({profile_idx: null})
  }).catch(() => {});
  // Reset dropdowns and manual flag
  const rs = document.getElementById('focus-res-sel');
  const fs = document.getElementById('focus-fps-sel');
  if (rs && _focusProfiles.length > 0) rs.value = '0';
  if (fs) fs.value = 'uncapped';
  _focusCurProf = 0;
  _manualTierActive = false;
}

async function _applyFocusTier(profIdx, fps) {
  await fetch(BASE + '/snap/focus/tier', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({profile_idx: profIdx, fps: fps})
  }).catch(() => {});
}

/* ── Storage browser ─────────────────────────────────────────────────────── */
let _storageData    = null;
let _dragSrc        = null;   // {path, folder} of file being dragged

async function loadStorage() {
  document.getElementById('storage-list').innerHTML
    = '<p style="color:var(--text-dim);padding:24px">Loading...</p>';
  try {
    const d = await (await fetch(BASE + '/api/storage')).json();
    _storageData = d;
    renderStorage(d);
  } catch(e) {
    document.getElementById('storage-list').innerHTML
      = '<p style="color:var(--red)">Error loading storage: ' + esc(String(e)) + '</p>';
  }
}



function storDragStart(e, path, folder) {
  _dragSrc = {path, folder};
  e.dataTransfer.effectAllowed = 'move';
}

async function storDrop(e, dstFolder) {
  e.preventDefault();
  if (!_dragSrc || _dragSrc.folder === dstFolder) return;
  const r = await fetch(BASE + '/api/storage/move', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({src_path: _dragSrc.path, dst_folder: dstFolder})
  });
  if (r.ok) { showToast('Moved to ' + dstFolder); loadStorage(); }
  else showToast('Move failed', true);
  _dragSrc = null;
}

async function storDeleteFile(path) {
  if (!confirm('Delete ' + path.split('/').pop() + '?')) return;
  const r = await fetch(BASE + '/api/storage/file', {
    method: 'DELETE', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path})
  });
  if (r.ok) { showToast('Deleted'); loadStorage(); }
  else showToast('Delete failed', true);
}

function storRenameFile(path, oldName) {
  const newName = prompt('Rename file:', oldName);
  if (!newName || newName === oldName) return;
  fetch(BASE + '/api/storage/rename', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({old_path: path, new_name: newName})
  }).then(r => { if (r.ok) { showToast('Renamed'); loadStorage(); } else showToast('Rename failed', true); });
}

function storRenameFolder(folder) {
  const newName = prompt('Rename folder:', folder);
  if (!newName || newName === folder) return;
  fetch(BASE + '/api/storage/rename', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({old_path: folder, new_name: newName})
  }).then(r => { if (r.ok) { showToast('Renamed'); loadStorage(); } else showToast('Rename failed', true); });
}

/* ── Toast notification ──────────────────────────────────────────────────── */
let _toastTimer = null;
function showToast(msg, isError = false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.background  = isError ? 'var(--red)' : 'var(--primary)';
  t.style.display     = 'block';
  t.style.opacity     = '1';
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => {
    t.style.opacity = '0';
    setTimeout(() => { t.style.display = 'none'; }, 300);
  // 2.4.0-rc2.8: 2500ms → 7000ms. The Deep Re-Probe completion toast
  // ("🔒 N locked stream(s) found", "no streams found", etc.) was
  // disappearing before users could read it. 7s gives enough time
  // to read a one-line message comfortably without lingering long
  // enough to feel obstructive.
  }, 7000);
}



/* ── Camera grid ───────────────────────────────────────────────────────────── */
// 2.4.0-rc3.3 (Camera Cards Fixed Position): cards used to swap positions
// when a user logged into a camera. Root cause: login changes the camera's
// ID (e.g. "10.0.0.22_onvif" → "10.0.0.22_onvif_MainStreamProfileToken")
// and the prior renderGrid logic matched by ID alone. The old ID disappeared
// from the cameras array, the corresponding DOM card was removed, and the
// new ID's card got appendChild'd at the end — visible as a "swap" since
// the old card vanished and a new one appeared in a different slot.
//
// Fix: match cards by a stable key derived from the camera's IP:port,
// which doesn't change across login. When an existing card's ID has
// changed (because the same IP:port now has a profile-tokened ID), we
// update its dataset.id in place and re-render its content — DOM
// position is naturally preserved because we never remove-and-re-append.
//
// Future drag-to-reorder feature can persist a user-set order in
// localStorage by capturing the DOM order of [data-stable-key] values
// and replaying it as a sort comparator at the top of renderGrid.
function _stableCardKey(cam) {
  // ip:port survives login (which mutates id but not network identity).
  // Falls back to id-as-key if a camera somehow has no ip (synthetic
  // entries during testing / dev), preserving old behavior in that edge.
  //
  // 2.5.0-rc1.7: append a #chN suffix when cam.channel is set, so multi-
  // channel DVR cards (Lorex/Dahua family) all sharing one ip:port get
  // distinct stable_keys. Without this, all 8 channel cards collapse
  // onto the same key — renderGrid's cardsByKey lookup returns the same
  // DOM element on every iteration, the loop overwrites that one card's
  // content with each iteration's HTML, and the new-card-append path is
  // never reached. Field-confirmed in 2.5.0-rc1.6 log: cred-auth
  // registered 7 channel cards in CAMERAS, +8s reload's renderGrid
  // collapsed them into one DOM card showing ch8's content (last
  // iteration wins). Only ch8 was [data-snap]'d, only ch8 was polled,
  // ch2-ch7 + parent idled out at 30s without ever being rendered.
  // Non-DVR cards have no `channel` field so they keep plain ip:port —
  // the rc3.3 fixed-position-on-login behaviour is preserved.
  if (cam.ip) {
    let key = cam.ip + ':' + (cam.port || '');
    if (cam.channel) key += '#ch' + cam.channel;
    return key;
  }
  return 'id:' + cam.id;
}

function renderGrid() {
  const grid  = document.getElementById('cam-grid');
  const empty = document.getElementById('empty-state');
  const count = document.getElementById('cam-count');
  count.textContent = '';   // device count shown in scan status bar — not duplicated here
  empty.style.display = cameras.length ? 'none' : '';

  // Build maps from existing DOM cards. cardsByKey is the primary lookup
  // (matches across login ID changes); cardsById covers the legacy edge
  // where an old card was rendered before the dataset.stableKey attribute
  // was introduced (first render after upgrade).
  const cardsByKey = new Map();
  const cardsById  = new Map();
  [...grid.querySelectorAll('.camera-card')].forEach(c => {
    if (c.dataset.id)        cardsById.set(c.dataset.id, c);
    if (c.dataset.stableKey) cardsByKey.set(c.dataset.stableKey, c);
  });

  const seenKeys = new Set();
  cameras.forEach(cam => {
    const key = _stableCardKey(cam);
    seenKeys.add(key);
    // Prefer key match (handles login ID change). Fall back to id match
    // (handles first render or stable_key-less legacy cards).
    let card = cardsByKey.get(key) || cardsById.get(cam.id);
    if (card) {
      // Existing card — update in place, preserving DOM position.
      if (card.dataset.id !== cam.id) {
        // ID changed (typical: login added a profile token). Preserve
        // position, update DOM identity, stop the old snap loop.
        stopSnap(card.dataset.id);
        card.dataset.id = cam.id;
      }
      card.dataset.stableKey = key;
      // Mirror updateCard's logic without re-querying — we already have
      // the element in hand.
      stopSnap(cam.id);
      card.innerHTML = cardHTML(cam);
      // uncertain class needs to be re-applied since we may have a fresh
      // verdict from the server (e.g. brand identification just ran).
      const isUncertain = (cam.verdict === 'uncertain' || cam.verdict === 'not_camera');
      card.classList.toggle('uncertain', isUncertain);
    } else {
      // New camera — append at the end. Future cards land here too.
      const newCard = buildCard(cam);
      newCard.dataset.stableKey = key;
      grid.appendChild(newCard);
    }
  });

  // Remove DOM cards whose stable key is no longer in the cameras array
  // (camera was deleted, marked not-camera, etc.). Using stable_key here
  // means a login-induced ID change does NOT trigger a removal, which is
  // the whole point of this rewrite.
  [...grid.querySelectorAll('.camera-card')].forEach(c => {
    const k = c.dataset.stableKey || ('id:' + c.dataset.id);
    if (!seenKeys.has(k)) c.remove();
  });

  grid.querySelectorAll('video[data-hls]').forEach(v => { if (!v._hls) initHls(v); });
  initSnaps();   // start polling for any newly added data-snap images
}

function buildCard(cam) {
  const d = document.createElement('div');
  d.className = 'camera-card' + (cam.verdict === 'uncertain' || cam.verdict === 'not_camera' ? ' uncertain' : '');
  d.dataset.id = cam.id;
  // 2.4.0-rc3.3: stable_key set on creation so the ID-change-preserving
  // matcher in renderGrid finds this card on next update.
  d.dataset.stableKey = _stableCardKey(cam);
  d.innerHTML = cardHTML(cam);
  return d;
}
function updateCard(cam) {
  const c = document.querySelector('[data-id="' + cam.id + '"]');
  if (c) {
    stopSnap(cam.id);   // stop any existing snap loop for this card
    c.innerHTML = cardHTML(cam);
    initSnaps();        // restart if card now has a data-snap image
  }
}

function dotClass(cam) {
  if (cam.upgrade_missing)                 return 'dot-upgrade';
  if (cam.status === 'ready')              return 'dot-ready';
  if (cam.status === 'info')               return 'dot-info';
  if (cam.verdict === 'uncertain' ||
      cam.verdict === 'not_camera')        return 'dot-uncertain';
  if (cam.status === 'needs_credentials')  return 'dot-warning';
  // 2.4.0-rc2.2 — authenticating_throttled is a transient state during
  // the rate-limited auth window (~30s on rate_limit_per_ip_tcp brands).
  // Previously fell through to 'dot-error' (red), which was misleading
  // since the camera is mid-authentication, not failed. Now renders as
  // yellow (warning) like other transient/informational states.
  if (cam.status === 'authenticating_throttled')  return 'dot-warning';
  return 'dot-error';
}

function protoBadge(proto) {
  const [bg, fg] = PROTO_CLR[proto] || ['2a2a2a', 'aaaaaa'];
  return '<span class="badge" style="background:#' + bg + ';color:#' + fg + '">'
       + (PROTO_ICONS[proto] || '') + ' ' + proto + '</span>';
}

/* rc2.5: pick the most useful port to show in the card badge.
   cam.port is the camera's primary HTTP/identification port (set at ONVIF
   discovery, often 80) and may differ from where the actual feed comes
   from (RTSP on 554, custom HTTP-snap port, etc.). Prefer the port parsed
   out of stream_url when present so the badge reflects the feed source. */
function cardPort(cam) {
  const m = (cam.stream_url || '').match(/:\/\/[^\/]*?:(\d+)/);
  return m ? m[1] : cam.port;
}

function feedHTML(cam) {
  const d = cam.display || 'proxy';

  // Post-upgrade: camera was saved but not found after upgrade scan
  if (cam.upgrade_missing)
    return '<div class="feed-placeholder">'
         + '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">'
         + '<circle cx="12" cy="12" r="10"/><path d="M4.93 4.93l14.14 14.14"/></svg>'
         + '<span style="color:var(--orange)"><strong>Not found after upgrade</strong><br>'
         + '<small style="opacity:.75">Was present before upgrade but did not respond.<br>'
         + 'May be offline, removed, or a false positive from the previous version.</small></span></div>';

  if (d === 'proxy' && cam.status === 'ready')
    // Snapshot polling: JS calls /snapshot/{id}?t=... every 125ms via startSnap().
    // Each request is a normal short HTTP round-trip — nginx/ingress handles it
    // correctly unlike long-lived multipart streams which ingress terminates early.
    return '<img class="live" data-snap="' + esc(cam.id) + '" alt="Live" style="display:none">'
         + '<div class="feed-placeholder" id="ph-' + esc(cam.id) + '">'
         + '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">'
         + '<path d="M15 10l4.553-2.069A1 1 0 0121 8.87v6.26a1 1 0 01-1.447.9L15 14"/>'
         + '<rect x="1" y="7" width="14" height="10" rx="2" ry="2"/></svg>'
         + '<span>Connecting...</span></div>';

  if (d === 'hls' && cam.status === 'ready')
    return '<video data-hls="' + esc(cam.stream_url) + '" autoplay muted playsinline></video>';

  if (d === 'webrtc')
    return '<div class="info-overlay"><div class="pi">🔗</div><strong>WebRTC Detected</strong>'
         + '<p>' + esc(cam.info || '') + '</p>'
         + '<a href="' + esc(cam.signaling_url || '#') + '" target="_blank">Open endpoint ↗</a></div>';

  if (d === 'wsrtsp')
    return '<div class="info-overlay"><div class="pi">🔌</div><strong>WS-RTSP Detected</strong>'
         + '<p>' + esc(cam.info || '') + '</p>'
         + '<code>' + esc(cam.ws_url || '') + '</code></div>';

  if (cam.verdict === 'uncertain' || cam.verdict === 'not_camera')
    return '<div class="feed-placeholder">'
         + '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">'
         + '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>'
         + '<line x1="12" y1="16" x2="12.01" y2="16"/></svg>'
         + '<span>Unverified device<br><small>' + esc(cam.verdict_reason || '') + '</small></span></div>';

  return '<div class="feed-placeholder">'
       + '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">'
       + '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>'
       + '<path d="M7 11V7a5 5 0 0110 0v4"/></svg>'
       + '<span>Credentials required</span></div>';
}

function identityHTML(cam) {
  const rows = [];
  if (cam.manufacturer)   rows.push(['Manufacturer', cam.manufacturer]);
  if (cam.mac_addr) {
    const macLabel = cam.mac_addr + (cam.mac_vendor ? '  (' + cam.mac_vendor + ')' : '');
    rows.push(['MAC / OUI', macLabel]);
  }
  if (cam.page_title)     rows.push(['Page title',   cam.page_title]);
  if (cam.server_header)  rows.push(['Server',       cam.server_header]);
  // 2.4.0-rc1.0: RTSP-layer fingerprint fields, captured by the OPTIONS
  // pre-probe. Distinct from the HTTP-layer Server / Page title above.
  if (cam.rtsp_server_header) rows.push(['RTSP server', cam.rtsp_server_header]);
  if (cam.rtsp_auth_realm)    rows.push(['RTSP realm',  cam.rtsp_auth_realm]);
  if (cam.hostname && cam.hostname !== cam.ip) rows.push(['Hostname', cam.hostname]);
  if (cam.device_notes)   rows.push(['Notes',        cam.device_notes]);
  // Stream technical details (populated once credentials are accepted)
  if (cam.stream_codec) {
    const codec = cam.stream_codec.toUpperCase()
                + (cam.stream_profile ? ' (' + cam.stream_profile + ')' : '');
    rows.push(['Video codec', codec]);
  }
  if (cam.stream_width && cam.stream_height) {
    const res = cam.stream_width + 'x' + cam.stream_height
              + (cam.stream_fps ? '  @  ' + cam.stream_fps + ' fps' : '');
    rows.push(['Resolution', res]);
  }
  if (cam.stream_audio)   rows.push(['Audio codec',  cam.stream_audio.toUpperCase()]);
  if (!rows.length) return '';
  return '<details class="id-section"><summary>&#x1F50D; Identity</summary><table class="id-table">'
    + rows.map(([k,v]) => '<tr><td class="id-key">' + esc(k) + '</td><td>' + esc(v) + '</td></tr>').join('')
    + '</table></details>';
}

function credFormHTML(cam) {
  if (cam.status !== 'needs_credentials' || ['webrtc','wsrtsp'].includes(cam.display)) return '';
  return '<div class="cred-form">'
    + '<label>USERNAME</label>'
    + '<input type="text" id="u_' + cam.id + '" placeholder="admin" autocomplete="username">'
    + '<label>PASSWORD</label>'
    + '<input type="password" id="p_' + cam.id + '" placeholder="••••••••"'
    + ' autocomplete="current-password"'
    + ' onkeydown="if(event.key===\'Enter\')submitCreds(\'' + cam.id + '\')">'
    + '<div class="cred-error" id="err_' + cam.id + '"></div>'
    + '<div class="cred-row">'
    + '<button class="btn btn-primary btn-sm" onclick="submitCreds(\'' + cam.id + '\')">Connect</button>'
    + '</div></div>';
}

function cardActions(cam, clearBtn, notCamBtn) {
  if (cam.upgrade_missing) {
    return '<button class="btn btn-ghost btn-sm" onclick="confirmCamera(\'' + cam.id + '\')">Keep (may be offline)</button>'
         + '<button class="btn btn-danger btn-sm" onclick="deleteCamera(\'' + cam.id + '\')">Remove</button>';
  }
  const testBtn = (cam.status === 'ready' && ['proxy','hls'].includes(cam.display || 'proxy'))
    ? '<button class="btn btn-ghost btn-sm" onclick="testStream(event,\'' + cam.id + '\')" title="Test stream connectivity">Test Stream</button>'
    : '';
  const motOn = !!_motionEnabled[cam.id];
  const recOn = !!_recording[cam.id];
  const recBtn = (cam.status === 'ready' && ['proxy'].includes(cam.display || 'proxy'))
    ? '<button class="btn btn-sm ' + (recOn ? 'btn-rec-active' : (motOn ? 'btn-rec-on' : 'btn-rec-off'))
      + '" onclick="toggleMotion(\'' + cam.id + '\')" title="' + (motOn ? 'Motion recording on' : 'Enable motion recording') + '">'
      + (recOn ? '⏺ REC' : (motOn ? '⏺ Armed' : '⏺ Record')) + '</button>'
    : '';
  // Globe button: opens camera web page (via Firefox addon or new tab)
  const webBtn = (cam.status === 'ready' && cam.ip)
    ? '<button class="btn btn-ghost btn-sm" onclick="openCameraPage(\'' + cam.ip + '\')" title="Open camera web page">🌐</button>'
    : '';
  // 2.4.0-rc2.4: Deep Re-Probe button. Available on any card where
  // we may have skipped paths during the original scan (Layer 1 early-
  // bail OR Layer 2 skip via brand flag) AND on needs_credentials
  // cards in general (user might want to re-probe after camera reboot
  // or firmware change). Highlighted with yellow accent + extended
  // label when early_bail_reason is set, indicating we know we
  // skipped some scanning for this specific card.
  // 2.4.0-rc2.6: loud in-progress styling + min-width to prevent
  // button reflow. The rc2.5 version used `btn-ghost btn-sm` for
  // the in-progress state, which rendered as nearly-invisible faint
  // text against the dark card. The button label "Deep Re-Probe
  // (skipped paths)" is also significantly wider than "Deep Re-
  // Probe" alone, so the row reflowed when state changed. Now: all
  // three states use the SAME button width (min-width 200px), and
  // in-progress gets the same yellow accent + spinner emoji as the
  // skipped-paths state for high visibility.
  // 2.4.0-rc2.8: button is visible IFF cam.early_bail_reason is set —
  // i.e. there are actually skipped paths to resume. After Deep Re-
  // Probe completes the backend clears early_bail_reason (api_deep_
  // reprobe at line ~9189), so the button vanishes once the work is
  // done. Cards that never had skipped paths (e.g. Lorex/Dahua DVR-
  // family which has skip_layer2: True and walks Layer 1 cleanly) get
  // no button at all — there's nothing for it to do. The previous
  // rc2.6 design had a "subdued ghost" state for these cards which
  // was misleading: clicking it would launch a "fresh full probe"
  // that had no extra capability beyond what the original scan did,
  // so the user always got "no streams found" with no actionable
  // next step. The in-progress state also requires early_bail_reason
  // to remain visible — without that, the moment Deep Re-Probe
  // completes and clears the flag, the button disappears.
  let reprobeBtn = '';
  const _reprobeStyle = 'min-width:200px;text-align:center;';
  const _showReprobe = (cam.status === 'needs_credentials' || cam.verdict === 'not_camera')
                       && (cam.deep_reprobe_in_progress || cam.early_bail_reason);
  if (_showReprobe) {
    if (cam.deep_reprobe_in_progress) {
      reprobeBtn = '<button class="btn btn-sm" disabled '
        + 'style="' + _reprobeStyle
        + 'background:#3a2e1e;color:#f5b942;border:1px solid #f5b942;opacity:0.85" '
        + 'title="Deep re-probe in progress — walking the unwalked paths">'
        + '⏳ Deep Re-Probe (running)</button>';
    } else {
      reprobeBtn = '<button class="btn btn-sm" '
        + 'style="' + _reprobeStyle
        + 'background:#3a2e1e;color:#f5b942;border:1px solid #f5b942" '
        + 'onclick="deepReprobe(\'' + cam.id + '\')" '
        + 'title="Resume scan from where rc2.4 fast-skipped — walks the unwalked paths">'
        + '🔍 Deep Re-Probe (skipped paths)</button>';
    }
  }
  return testBtn + clearBtn + reprobeBtn + notCamBtn + recBtn + webBtn
       + '<button class="btn btn-danger btn-sm" onclick="deleteCamera(\'' + cam.id + '\')">Remove</button>';
}

// 2.4.0-rc2.4: Deep Re-Probe handler. Posts to api_deep_reprobe and
// progressively updates UI status. Backend handler is synchronous over
// HTTP — total wait is whatever Layer 1 resume + Layer 2 walk take
// (5-60s typical). We update the button to a spinner during the call,
// then refresh the camera list so the result renders.
async function deepReprobe(cid) {
  const cam = (cameras || []).find(c => c.id === cid);
  // Build a status-line message; keep the user informed about which
  // stages are running.
  const reasonText = cam && cam.early_bail_reason
    ? ' (resuming from skipped paths)' : '';
  showToast('Deep Re-Probe started' + reasonText + '…');
  // Optimistically flip the in-progress flag so the button disables
  if (cam) { cam.deep_reprobe_in_progress = true; renderGrid(); }
  try {
    const r = await fetch(BASE + '/api/cameras/' + cid + '/deep_reprobe',
                         { method: 'POST' });
    const d = await r.json();
    if (!r.ok) {
      showToast('Deep Re-Probe failed: ' + (d.error || r.statusText), true);
      return;
    }
    let msg = 'Deep Re-Probe complete: ';
    if (d.found_stream) msg += '✅ working stream found';
    else if (d.locked_count > 0) msg += '🔒 ' + d.locked_count + ' locked stream(s) found';
    else msg += 'no streams found';
    showToast(msg);
  } catch (e) {
    showToast('Deep Re-Probe error: ' + e, true);
  } finally {
    // Force refresh from server so cam.* fields (early_bail_reason,
    // locked_streams, status, deep_reprobe_attempts) reflect the new
    // backend state.
    try { await loadCameras(); } catch (_) {}
  }
}
async function testStream(ev, cid) {
  const btn = ev.target, orig = btn.textContent;
  btn.textContent = 'Testing...'; btn.disabled = true;
  try {
    const r = await fetch(BASE + '/stream/' + cid + '/test');
    const d = await r.json();
    const msg = d.success
      ? 'Stream OK  Codec:' + (d.codec||'?') + '  ' + (d.width||'?') + 'x' + (d.height||'?') + ' FPS:' + (d.fps||'?') + '\n\nIf live view fails, try H.264 720p on the camera.'
      : 'Stream test failed\n' + (d.error||'Unknown') + '\nURL: ' + (d.url||'');
    alert(msg);
  } catch(e) { alert('Test failed: ' + e); }
  finally { btn.textContent = orig; btn.disabled = false; }
}

/* rc2.1: generic-name detector. ONVIF often returns boilerplate names
   like "IPCAM" or "Network Camera" instead of a real model. When the
   camera record has a manufacturer (typically set from MAC OUI lookup),
   prefer that over the generic ONVIF default. Regex anchors the entire
   string so user-given names that happen to contain "Camera" (e.g.
   "Front Porch Camera") are NOT treated as generic. */
function _isGenericCamName(s) {
  if (!s) return true;
  // 2.4.0-rc2.1: added "general" — observed on Lorex/Dahua DVRs which
  // report ONVIF Name="General" by default. Without this, the card
  // displayed "General" (the generic ONVIF name) instead of the
  // brand-identified manufacturer ("Lorex / Dahua DVR-NVR Family").
  // "generic" included for parity (similar product lines).
  if (/^(ip\s*cam(era)?|network\s*camera|camera|onvif[\s_-]*(device|camera)?|webcam|video\s*server|general|generic)$/i.test(s.trim()))
    return true;

  // 2.4.0-rc2.3: also treat auto-discovered hostnames as generic so
  // displayName falls through to the identified manufacturer instead
  // of the hostname. This fixes a long-latent bug surfaced by rc2.2's
  // faster scan: prev_name defaults to hostname when no ONVIF name is
  // available, and hostnames like "D861A8.lan" or
  // "tplink.my.house" or just an IP weren't recognized as generic.
  // Patterns covered:
  //   • Bare IPv4 (e.g. "10.0.0.13")
  //   • Reverse-DNS / ISP-provided FQDNs (e.g. "*.attlocal.net",
  //     "*.lan", "*.local", "*.home", "*.localdomain", and any user-
  //     provided local DNS suffix that the user hasn't customized
  //     per-camera). Heuristic: anything containing a dot AND looking
  //     like a domain — bare DNS labels with no user formatting.
  //   • MAC-OUI / serial-derived hostnames (uppercase hex chunks like
  //     "D861A8", "00:00:5E:00:53:09", "SN0123456789-ABCDEF012345")
  //   • mDNS-style "*.my.house" / "*.<userdomain>" auto-publish names
  // User-given names like "Front Door Camera" or "Driveway" still fail
  // the regex and remain user-displayed.
  const t = s.trim();
  // Bare IPv4
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(t)) return true;
  // Serial-style hostname WITHOUT a dot (e.g. "SN0123456789-ABCDEF012345"
  // — printer/IoT devices that publish their MAC- or serial-derived
  // hostname directly without a domain). Heuristic: 6+ uppercase
  // alphanumerics, optionally hyphenated into multiple all-caps blocks.
  // User-given names (mixed case "Front Door") fail this anchor.
  if (/^[A-Z0-9]{6,}(-[A-Z0-9]+)+$/.test(t)) return true;
  // Bare hostname-as-FQDN: contains a dot AND the leftmost label looks
  // device-derived (all caps + hex/digits, or has multiple hex segments
  // separated by hyphens). User-given short names rarely look like this.
  if (t.indexOf('.') >= 0) {
    const left = t.split('.')[0];
    // Pure hex or alphanum-uppercase first label (e.g. "D861A8",
    // "SN0123456789-ABCDEF012345", "tplink", "homeassistant"-NO that's
    // mixed case - we explicitly want "device-derived" patterns)
    if (/^[A-F0-9]{4,}$/.test(left)) return true;             // pure hex
    if (/^[A-Z0-9]{6,}(-[A-Z0-9]+)*$/.test(left)) return true; // serial-style
    // Common reverse-DNS suffixes — names ending in these are
    // auto-derived, not user-named
    if (/\.(local|lan|home|localdomain|attlocal\.net|hsd1\.[a-z]+\.comcast\.net|fios-router\.home)$/i.test(t))
      return true;
    // mDNS/local broadcast pattern: any FQDN with 3+ labels where the
    // leftmost label is all-lowercase short device-name. This catches
    // "tplink.my.house", "homeassistant.local", etc. Conservative —
    // requires the host part to be a single short lowercase token.
    if (/^[a-z][a-z0-9]{2,15}\.[a-z0-9]+(\.[a-z0-9]+)+$/i.test(t))
      return true;
  }
  return false;
}

function displayName(cam) {
  if (cam.manufacturer && _isGenericCamName(cam.name || '')) {
    return cam.manufacturer;
  }
  return cam.name || cam.hostname || cam.ip;
}

function cardHTML(cam) {
  const name     = esc(displayName(cam));
  // Lock badge: yellow key when creds not stored, green key when stored.
  // Only shown for cameras that actually involve credentials — pure-public
  // streams (e.g. open MJPEG with no auth) get no badge.
  const _showLock = cam.has_credentials || cam.requires_credentials || cam.status === 'needs_credentials';
  const _keyFill  = cam.has_credentials ? '#3B6D11' : '#F2BD2A';
  const _keyStrk  = cam.has_credentials ? '#173404' : '#8B6F00';
  const _lockTtl  = cam.has_credentials ? 'Credentials stored' : 'Credentials required';
  const credBdg   = _showLock
    ? '<span class="badge lock-badge" title="' + _lockTtl + '">'
      + '<svg width="20" height="20" viewBox="0 0 24 24">'
      +   '<path d="M5 9V6a3 3 0 0 1 6 0v3" fill="none" stroke="#8B6F00" stroke-width="2" stroke-linecap="round"/>'
      +   '<rect x="2" y="9" width="12" height="10" rx="2" fill="#F2BD2A" stroke="#8B6F00" stroke-width="0.6"/>'
      +   '<circle cx="8" cy="13" r="1.1" fill="#5C4400"/>'
      +   '<rect x="7.4" y="13" width="1.2" height="3" fill="#5C4400"/>'
      +   '<circle cx="14.5" cy="7" r="2.85" fill="' + _keyFill + '" stroke="' + _keyStrk + '" stroke-width="0.7"/>'
      +   '<circle cx="14.5" cy="7" r="1.2" fill="#1a1a1a"/>'
      +   '<rect x="13.75" y="9.85" width="1.5" height="6.3" fill="' + _keyFill + '" stroke="' + _keyStrk + '" stroke-width="0.5"/>'
      +   '<rect x="15.25" y="13.5" width="2.5" height="1.05" fill="' + _keyFill + '" stroke="' + _keyStrk + '" stroke-width="0.3"/>'
      +   '<rect x="15.25" y="15" width="1.8" height="0.75" fill="' + _keyFill + '" stroke="' + _keyStrk + '" stroke-width="0.3"/>'
      + '</svg>'
      + '</span>'
    : '';
  const uncBdg   = (cam.verdict === 'uncertain' || cam.verdict === 'not_camera')
    ? '<span class="badge" style="background:#3a2e1e;color:#f5b942" title="'
      + esc(cam.verdict_reason || '') + '">⚠ Unverified</span>' : '';
  const clearBtn = cam.has_credentials
    ? '<button class="btn btn-ghost btn-sm" onclick="clearCreds(\'' + cam.id + '\')">Clear Creds</button>' : '';
  const notCamBtn =
    '<button class="btn btn-ghost btn-sm" onclick="markNotCamera(\'' + cam.id + '\')"'
    + ' title="Permanently hide — not a camera">🚫 Not a Camera</button>';
  const upgradeBdg = cam.upgrade_missing
    ? '<span class="badge" style="background:#3a2a10;color:var(--orange)">⚠ Not found after upgrade</span>' : '';
  const hevcPlusBdg = cam.hevc_plus_warning && !cam.has_sub_stream
    ? '<span class="badge" style="background:#3a1a1a;color:#ff7070" title="Camera streams H.265+ (Hikvision proprietary). Fix: camera web UI → Video → Encoding → change H.265+ to H.265">⚠ H.265+</span>'
    : cam.hevc_plus_fallback_active || (cam.hevc_plus_warning && cam.has_sub_stream)
    ? '<span class="badge" style="background:#1a3a1a;color:#6fcf97" title="H.265+ detected — switched to compatible sub-stream automatically">✓ H.265+ fallback</span>'
    : '';
  // 2.4.0-rc2.0 (Layered Stream Discovery): show "View Locked Streams (N)"
  // badge when the path-walker found additional 401-locked paths AND the
  // camera doesn't have stored credentials yet. Once credentials are
  // accepted, the badge disappears (the cred-auth flow handles those
  // streams, so surfacing them again would be confusing). Click opens
  // a modal listing the paths + realm with a credential entry prompt.
  const _locked = Array.isArray(cam.locked_streams) ? cam.locked_streams : [];
  const lockedBdg = (_locked.length > 0 && !cam.has_credentials)
    ? '<span class="badge locked-streams-badge" title="' + _locked.length
      + ' additional stream(s) found that require credentials"'
      + ' onclick="event.stopPropagation();openLockedStreams(\'' + cam.id + '\')"'
      + ' style="background:#2d2640;color:#b39ddb;cursor:pointer">'
      + '🔒 ' + _locked.length + ' Locked Stream' + (_locked.length === 1 ? '' : 's')
      + '</span>'
    : '';

  return '<div class="feed-wrap">' + feedHTML(cam) + '</div>'
    + '<div class="card-info">'
    + '<div class="status-dot ' + dotClass(cam) + '"></div>'
    + '<span class="card-name" title="' + name + '"'
    + ' onclick="openRename(\'' + cam.id + '\',\'' + name.replace(/'/g, "\\'") + '\')">'
    + name + '</span></div>'
    + '<div class="badges">' + protoBadge(cam.protocol)
    + '<span class="badge" style="background:#2d2020;color:#e88">' + cam.ip + '</span>'
    + '<span class="badge" style="background:#1e2d1e;color:#6fcf97">:' + cardPort(cam) + '</span>'
    + credBdg + uncBdg + upgradeBdg + hevcPlusBdg + lockedBdg + '</div>'
    + '</div>'
    + identityHTML(cam)
    + credFormHTML(cam)
    + '<div class="card-actions">' + cardActions(cam, clearBtn, notCamBtn)
    + '</div>';
}
/* ── HLS init ──────────────────────────────────────────────────────────────── */
function initHls(video) {
  video._hls = true;
  const src = video.dataset.hls;
  if (!src) return;
  if (Hls.isSupported()) {
    const h = new Hls();
    h.loadSource(src);
    h.attachMedia(video);
    h.on(Hls.Events.ERROR, (_, d) => { if (d.fatal) video.style.display = 'none'; });
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = src;
  }
}

/* ── Credentials ───────────────────────────────────────────────────────────── */
async function submitCreds(cid) {
  const u = document.getElementById('u_' + cid)?.value.trim() || '';
  const p = document.getElementById('p_' + cid)?.value || '';
  const e = document.getElementById('err_' + cid);
  /* 2.3.0: rate_limit_per_ip_tcp brands need single-socket validation +
     paced ffprobe, which makes cred-auth take ~25–30s. Tell the user
     that's expected so they don't think the UI is hung. */
  const cam = (typeof cameras !== 'undefined') ?
    cameras.find(c => c.id === cid) : null;
  const _throttledBrand = cam && (
    /^Hipcam|^Microseven|^Sricam|^Vstarcam|^Wansview|^Tenvis/i.test(cam.manufacturer || ''));
  e.textContent = _throttledBrand
    ? 'Authenticating (Camera rate-limited, ~30 seconds)…'
    : 'Verifying…';
  e.classList.add('visible');
  try {
    const r = await fetch(BASE + '/api/credentials', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({camera_id: cid, username: u, password: p})
    });
    const d = await r.json();
    if (r.ok) {
      e.classList.remove('visible');
      await loadCameras();
      // 2.5.0-rc1.6: channel-iterate brands (Lorex/Dahua DVR-NVR
      // family) spawn a background channel enumeration after cred-auth
      // returns 200. The first loadCameras() above runs before that
      // enumeration completes and sees only the parent card.
      //
      // 2.5.0-rc1.8: replaces the previous fixed setTimeout(loadCameras,
      // 8000) reload with a poll-until-done loop against the new
      // /api/dvr_enum/status/{camera_id} endpoint. The old fixed budget
      // had to assume worst-case wallclock (15 channels at ~300ms per
      // walker call plus politeness sleeps and snap_loop kickoffs);
      // the rc1.7 test log on CrystalHeeler's 7-channel Lorex showed
      // enumeration actually completing in ~3s, so the old fixed wait
      // cost ~5s of dead time before cards appeared. This poll wakes
      // the moment the backend signals done.
      //
      // Hard-cap at 12s (24 polls @ 500ms = +50% headroom over the old
      // fixed budget). If somehow done never flips true (backend bug
      // or unforeseen exception that bypasses the rc1.8 hardening),
      // the cap-fall-through calls loadCameras() anyway so the worst
      // case matches today's behavior — no regression.
      if (d.dvr_enumeration_pending) {
        let polls = 0;
        const MAX_POLLS = 24;        // 24 * 500ms = 12s hard cap
        const POLL_INTERVAL_MS = 500;
        const pollUrl = BASE + '/api/dvr_enum/status/' +
                        encodeURIComponent(cid);
        const tick = async () => {
          polls += 1;
          let done = false;
          try {
            const sr = await fetch(pollUrl);
            if (sr.ok) {
              const sd = await sr.json();
              done = !!sd.done;
            }
          } catch { /* network blip — keep polling until cap */ }
          if (done) {
            await loadCameras();
            return;
          }
          if (polls >= MAX_POLLS) {
            // Fall back to behaviour matching 2.5.0-rc1.7's fixed
            // setTimeout — refetch anyway so cards eventually surface
            // even on backend failure.
            await loadCameras();
            return;
          }
          setTimeout(tick, POLL_INTERVAL_MS);
        };
        setTimeout(tick, POLL_INTERVAL_MS);
      }
    }
    else e.textContent = d.error || 'Connection failed.';
  } catch { e.textContent = 'Network error.'; }
}

async function clearCreds(cid) {
  if (!confirm('Clear stored credentials for this camera?')) return;
  await fetch(BASE + '/api/cameras/' + cid + '/credentials', {method: 'DELETE'});
  await loadCameras();
}

async function deleteCamera(cid) {
  if (!confirm('Remove this camera?')) return;
  await fetch(BASE + '/api/cameras/' + cid, {method: 'DELETE'});
  cameras = cameras.filter(c => c.id !== cid);
  renderGrid();
}

async function confirmCamera(cid) {
  // User acknowledges camera may just be offline — clear the upgrade_missing flag
  await fetch(BASE + '/api/cameras/' + cid + '/confirm', {method: 'POST'});
  await loadCameras();
}

/* ── Not a Camera modal ────────────────────────────────────────────────────── */
let _notCamId = null;
const COMMUNITY_ENDPOINT = '___COMMUNITY___';

function openNotCamModal(cid) {
  _notCamId = cid;
  const cam = cameras.find(c => c.id === cid);
  const label = cam ? (cam.manufacturer || cam.name || cam.ip) : cid;
  document.getElementById('nc-device-label').textContent = label;
  document.querySelectorAll('.nc-reason-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('nc-detail').value = '';
  document.getElementById('nc-share').checked = !!COMMUNITY_ENDPOINT;
  document.getElementById('nc-share-row').style.display = COMMUNITY_ENDPOINT ? '' : 'none';
  document.getElementById('nc-modal').classList.add('open');
  setTimeout(() => document.querySelector('.nc-reason-btn[data-reason="unknown"]')?.focus(), 50);
}

function closeNotCamModal() {
  document.getElementById('nc-modal').classList.remove('open');
  _notCamId = null;
}

function selectReason(btn) {
  document.querySelectorAll('.nc-reason-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

async function submitNotCam() {
  if (!_notCamId) { closeNotCamModal(); return; }
  const activeBtn   = document.querySelector('.nc-reason-btn.active');
  const reasonType  = activeBtn ? activeBtn.dataset.reason : 'unknown';
  const reasonDetail = document.getElementById('nc-detail').value.trim();
  const share       = document.getElementById('nc-share').checked;
  await fetch(BASE + '/api/cameras/' + _notCamId + '/not_camera', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({reason_type: reasonType, reason_detail: reasonDetail, share})
  });
  cameras = cameras.filter(c => c.id !== _notCamId);
  closeNotCamModal();
  renderGrid();
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('nc-modal').classList.contains('open'))
    closeNotCamModal();
});

async function markNotCamera(cid) { openNotCamModal(cid); }

/* ── 2.4.0-rc2.0 Layered Stream Discovery: Locked Streams modal ─────────── */
let _lockedCid = null;

function openLockedStreams(cid) {
  _lockedCid = cid;
  const cam = cameras.find(c => c.id === cid);
  if (!cam) return;
  const label = displayName(cam);
  const locked = Array.isArray(cam.locked_streams) ? cam.locked_streams : [];
  document.getElementById('locked-device-label').textContent = label;
  document.getElementById('locked-count').textContent = locked.length;
  const list = document.getElementById('locked-list');
  list.innerHTML = locked.map(ls => {
    const path = esc(ls.path || '?');
    const realm = ls.realm ? ' (realm: ' + esc(ls.realm) + ')' : '';
    const scheme = ls.scheme ? ' [' + esc(ls.scheme) + ']' : '';
    return '<div class="locked-row"><code>' + path + '</code>'
         + '<span class="locked-meta">' + scheme + realm + '</span></div>';
  }).join('');
  document.getElementById('locked-user').value = '';
  document.getElementById('locked-pass').value = '';
  document.getElementById('locked-err').textContent = '';
  document.getElementById('locked-err').classList.remove('visible');
  document.getElementById('locked-modal').classList.add('open');
  setTimeout(() => document.getElementById('locked-user').focus(), 50);
}

function closeLockedStreams() {
  document.getElementById('locked-modal').classList.remove('open');
  _lockedCid = null;
}

async function submitLockedCreds() {
  if (!_lockedCid) { closeLockedStreams(); return; }
  const u = document.getElementById('locked-user').value.trim();
  const p = document.getElementById('locked-pass').value;
  const e = document.getElementById('locked-err');
  if (!u || !p) {
    e.textContent = 'Username and password are required.';
    e.classList.add('visible');
    return;
  }
  e.textContent = 'Authenticating…';
  e.classList.add('visible');
  try {
    /* Reuses the existing cred-auth endpoint. The server-side flow runs
       _identity-preserving cred-auth which retries the path walker WITH
       credentials supplied, so any same-realm 401-locked paths get
       authenticated automatically. After success, the camera record is
       rebuilt with stream_url(s) populated and the locked_streams list
       remains for diagnostic display (badge hides because user_saved). */
    const r = await fetch(BASE + '/api/credentials', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({camera_id: _lockedCid, username: u, password: p})
    });
    const d = await r.json();
    if (r.ok) {
      e.classList.remove('visible');
      closeLockedStreams();
      await loadCameras();
    } else {
      e.textContent = d.error || 'Authentication failed.';
    }
  } catch {
    e.textContent = 'Network error.';
  }
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('locked-modal').classList.contains('open'))
    closeLockedStreams();
});

/* ── Rename ────────────────────────────────────────────────────────────────── */
function openRename(cid, name) {
  renameId = cid;
  document.getElementById('rename-input').value = name;
  document.getElementById('rename-modal').classList.add('open');
  setTimeout(() => document.getElementById('rename-input').focus(), 50);
}
function closeRename() {
  document.getElementById('rename-modal').classList.remove('open');
  renameId = null;
}
async function submitRename() {
  const name = document.getElementById('rename-input').value.trim();
  if (!name || !renameId) { closeRename(); return; }
  await fetch(BASE + '/api/cameras/' + renameId + '/name', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name})
  });
  closeRename();
  await loadCameras();
}
document.getElementById('rename-modal').addEventListener('click',
  e => { if (e.target.id === 'rename-modal') closeRename(); });
document.getElementById('rename-input').addEventListener('keydown',
  e => { if (e.key === 'Enter') submitRename(); if (e.key === 'Escape') closeRename(); });

/* ── Port scanner ──────────────────────────────────────────────────────────── */
let _arpHosts = [];
let _selectedIPs = new Set();   // persists across view switches

async function loadArpHosts() {
  try {
    _arpHosts = await (await fetch(BASE + '/api/arp_hosts')).json();
    renderArpList();
  } catch(e) {
    console.warn('ARP hosts not available yet');
  }
}

function renderArpList() {
  const wrap = document.getElementById('arp-host-list');
  if (!_arpHosts.length) {
    wrap.innerHTML = '<span class="arp-empty">No hosts discovered yet — run a network scan first.</span>';
    return;
  }
  wrap.innerHTML = '<div class="arp-select-row">'
    + '<button class="btn btn-ghost btn-sm" onclick="arpSelectAll(true)">Select all</button>'
    + '<button class="btn btn-ghost btn-sm" onclick="arpSelectAll(false)">Clear all</button>'
    + '<span style="color:var(--text-dim);font-size:.75rem;margin-left:6px">'
    + _arpHosts.length + ' host' + (_arpHosts.length !== 1 ? 's' : '') + ' discovered</span>'
    + '</div>'
    + _arpHosts.map(h => {
        const checked = _selectedIPs.has(h.ip) ? ' checked' : '';
        return '<label class="arp-row">'
          + '<input type="checkbox" class="arp-cb" value="' + h.ip + '"'
          + checked + ' onchange="onCbChange(this)"> '
          + '<span class="arp-ip">' + h.ip + '</span>'
          + (h.hostname && h.hostname !== h.ip
              ? '<span class="arp-host"> — ' + esc(h.hostname) + '</span>' : '')
          + '</label>';
      }).join('');
}

function onCbChange(cb) {
  if (cb.checked) _selectedIPs.add(cb.value);
  else            _selectedIPs.delete(cb.value);
}

function arpSelectAll(val) {
  document.querySelectorAll('.arp-cb').forEach(cb => {
    cb.checked = val;
    if (val) _selectedIPs.add(cb.value);
    else     _selectedIPs.delete(cb.value);
  });
}

function getSelectedIPs() {
  const manual = document.getElementById('pscan-ip').value.trim();
  const combined = [...new Set([..._selectedIPs, ...(manual ? [manual] : [])])];
  return combined;
}

async function startPortScan() {
  const ips = getSelectedIPs();
  if (!ips.length) { alert('Select at least one host or enter an IP address.'); return; }

  const body = ips.length === 1 ? {ip: ips[0]} : {ips};
  const r = await fetch(BASE + '/api/pscan/start', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  if (!r.ok) { const d = await r.json(); alert(d.error || 'Scan failed'); return; }
  document.getElementById('ps-start').disabled = true;
  document.getElementById('ps-pause').style.display  = '';
  document.getElementById('ps-cancel').style.display = '';
  document.getElementById('ps-prog-track').style.display = '';
  document.getElementById('port-table').style.display = 'none';
  document.getElementById('port-tbody').innerHTML = '';
  document.getElementById('live-ports-box').innerHTML = '';
  document.getElementById('live-ports-box').style.display = 'none';
  _paused = false;
  pollPscan();
}

async function pollPscan() {
  clearTimeout(pscanT);
  try {
    const s = await (await fetch(BASE + '/api/pscan/status')).json();

    // Message + ETA
    const msgEl   = document.getElementById('pscan-msg');
    const timerEl = document.getElementById('ps-timer');
    msgEl.textContent = s.message;

    if (s.running && s.scan_start) {
      const clientElap = (Date.now()/1000) - s.scan_start;
      if (s.eta > 0) {
        const drift  = Math.max(0, clientElap - (s.elapsed || 0));
        const etaAdj = Math.max(0, Math.round(s.eta - drift));
        const etaM = Math.floor(etaAdj/60), etaSec = etaAdj % 60;
        timerEl.textContent = 'estimated ' + etaM + ':' + String(etaSec).padStart(2,'0') + ' remaining';
      } else {
        // No nmap ETA yet — show elapsed instead
        const m = Math.floor(clientElap/60), sec = Math.floor(clientElap) % 60;
        timerEl.textContent = m + ':' + String(sec).padStart(2,'0') + ' elapsed';
      }
      timerEl.style.display = '';
    } else if (!s.running && s.scan_start) {
      const elapsed = Math.round(s.elapsed || 0);
      const m = Math.floor(elapsed/60), sec = elapsed % 60;
      timerEl.textContent = 'Completed in ' + m + ':' + String(sec).padStart(2,'0');
      timerEl.style.display = '';
    } else {
      timerEl.style.display = 'none';
    }

    document.getElementById('ps-prog-fill').style.width = (s.progress || 0) + '%';

    // Live port discovery box (during scan)
    const liveBox = document.getElementById('live-ports-box');
    if (s.running && s.live_ports && s.live_ports.length) {
      liveBox.style.display = '';
      liveBox.innerHTML = s.live_ports.map(p =>
        '<div class="live-port-row">'
        + '<span class="pnum">' + p.port + '</span>'
        + '<span class="live-proto">/' + esc(p.proto) + '</span>'
        + '</div>'
      ).join('');
      liveBox.scrollTop = liveBox.scrollHeight;
    } else if (!s.running) {
      liveBox.style.display = 'none';
    }

    // Final results table (when done)
    if (!s.running && s.results && s.results.length) {
      renderPorts(s.results);
    }

    if (s.running) {
      pscanT = setTimeout(pollPscan, 2000);
    } else {
      document.getElementById('ps-start').disabled  = false;
      document.getElementById('ps-pause').style.display  = 'none';
      document.getElementById('ps-cancel').style.display = 'none';
    }
  } catch { pscanT = setTimeout(pollPscan, 3000); }
}

const CAM_PORTS   = new Set([554,8554,10554,1935,1936,2020,37777,34567,8765]);
const CAM_SERVICES = ['rtsp','onvif','rtmp','camera','ipcam','nvr','dvr','cctv',
                      'video server','webcam','dahua','hikvision','lorex','reolink',
                      'axis','amcrest','axis-cgi','mediamtx'];

function isCamPort(p) {
  if (CAM_PORTS.has(p.port)) return true;
  const combined = (p.service + ' ' + p.product).toLowerCase();
  return CAM_SERVICES.some(k => combined.includes(k));
}

function makePortRow(p, hasBatch) {
  const ver = [p.product, p.version, p.extra].filter(Boolean).join(' ');
  const scripts = Object.entries(p.scripts || {})
    .map(([k,v]) => '<b>' + esc(k) + '</b>: ' + esc(v)).join('\n');
  return '<tr>'
    + (hasBatch ? '<td class="arp-ip" style="white-space:nowrap">' + esc(p.scanned_ip || '') + '</td>' : '')
    + '<td class="pnum">' + p.port + '</td>'
    + '<td>' + esc(p.proto) + '</td>'
    + '<td class="psvc">' + esc(p.service) + '</td>'
    + '<td>' + esc(ver) + '</td>'
    + '<td>' + (scripts ? '<div class="pscripts">' + scripts + '</div>' : '—') + '</td>'
    + '</tr>';
}

function renderPorts(results) {
  if (!results.length) return;
  document.getElementById('port-table').style.display = '';
  const hasBatch = results.some(p => p.scanned_ip);
  const thead = document.querySelector('#port-table thead tr');
  if (hasBatch && !thead.querySelector('.batch-ip-col')) {
    const th = document.createElement('th');
    th.textContent = 'Host'; th.className = 'batch-ip-col';
    thead.insertBefore(th, thead.firstChild);
  } else if (!hasBatch) {
    const old = thead.querySelector('.batch-ip-col');
    if (old) old.remove();
  }

  const camPorts  = results.filter(isCamPort);
  const otherPorts = results.filter(p => !isCamPort(p));

  let html = '';
  if (camPorts.length) {
    html += '<tr class="section-header"><td colspan="99">&#x1F4F9; Likely camera-related ('
          + camPorts.length + ' port' + (camPorts.length !== 1 ? 's' : '') + ')</td></tr>';
    html += camPorts.map(p => makePortRow(p, hasBatch)).join('');
  }
  if (otherPorts.length) {
    html += '<tr class="section-header other-header" onclick="toggleOther(this)">'
          + '<td colspan="99">&#x25B6; Other ports ('
          + otherPorts.length + ') — click to show/hide</td></tr>';
    html += '<tbody class="other-ports" style="display:none">'
          + otherPorts.map(p => makePortRow(p, hasBatch)).join('')
          + '</tbody>';
  }
  document.getElementById('port-tbody').innerHTML = html;
}

function toggleOther(hdrRow) {
  const next = hdrRow.nextElementSibling;
  if (!next) return;
  const hidden = next.style.display === 'none';
  next.style.display = hidden ? '' : 'none';
  const arrow = hdrRow.querySelector('td');
  if (arrow) arrow.textContent = arrow.textContent.replace(hidden ? '▶' : '▼', hidden ? '▼' : '▶');
}

async function togglePause() {
  const btn = document.getElementById('ps-pause');
  if (!_paused) {
    await fetch(BASE + '/api/pscan/pause', {method: 'POST'});
    _paused = true; btn.textContent = '▶ Resume';
  } else {
    await fetch(BASE + '/api/pscan/resume', {method: 'POST'});
    _paused = false; btn.textContent = '⏸ Pause';
    pollPscan();
  }
}

async function cancelPortScan() {
  await fetch(BASE + '/api/pscan/cancel', {method: 'POST'});
  _paused = false;
  document.getElementById('ps-pause').textContent = '⏸ Pause';
  document.getElementById('ps-start').disabled = false;
  document.getElementById('ps-pause').style.display  = 'none';
  document.getElementById('ps-cancel').style.display = 'none';
}

/* ── Add camera ────────────────────────────────────────────────────────────── */
function onProtoChange() {
  const p = document.getElementById('add-proto').value;
  document.getElementById('add-port').value = DPORT[p] || 554;
  document.getElementById('path-field').style.display =
    ['RTSP','ONVIF','DVR'].includes(p) ? '' : 'none';
}

async function submitAddCamera() {
  const btn   = document.getElementById('add-btn2');
  const errEl = document.getElementById('add-error');
  errEl.style.display = 'none';
  btn.disabled = true; btn.textContent = 'Connecting…';
  try {
    const r = await fetch(BASE + '/api/cameras/add', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        name:      document.getElementById('add-name').value.trim(),
        ip:        document.getElementById('add-ip').value.trim(),
        port:      parseInt(document.getElementById('add-port').value) || 554,
        protocol:  document.getElementById('add-proto').value,
        rtsp_path: document.getElementById('add-path').value.trim(),
        username:  document.getElementById('add-user').value.trim(),
        password:  document.getElementById('add-pass').value,
      })
    });
    const d = await r.json();
    if (r.ok) { await loadCameras(); switchView('cameras'); }
    else { errEl.textContent = d.error || 'Connection failed.'; errEl.style.display = 'block'; }
  } catch { errEl.textContent = 'Network error.'; errEl.style.display = 'block'; }
  finally { btn.disabled = false; btn.textContent = 'Connect'; }
}

/* ── Helpers ───────────────────────────────────────────────────────────────── */
function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/* ── Init ──────────────────────────────────────────────────────────────────── */
(async () => {
  await loadCameras();
  const s = await (await fetch(BASE + '/api/scan/status')).json();
  if (s.running) {
    document.getElementById('scan-btn').disabled = true;
    document.getElementById('progress-track').style.display = '';
    document.getElementById('status-bar').classList.add('scanning');
    pollScan();
  }
})();
"""


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
    for hw in ("hevc_v4l2m2m", "h264_v4l2m2m", "hevc_vaapi", "h264_vaapi"):
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





async def _probe_host_port(ip: str, port: int, hostname: str,
                            initial_protocol: str, prev: dict,
                            verdict: str, reason: str, loop,
                            host_meta: dict | None = None) -> dict | None:
    """
    Probe a single host:port and return a camera dict if a stream is found,
    or None if nothing reachable. Uses saved credentials from prev if available.

    rc2: host_meta — optional dict containing mac_vendor, server_header,
    page_title, nmap_product, etc. captured during the scan stage.
    Passed through to find_rtsp_path so brand-aware probe short-circuits
    can fire (skip Layer 2 for Hipcam family, extend timeout for Reolink
    battery, append Axis Companion query param, return None for Eufy/Ring/
    Nest/Arlo/Verkada).
    """
    # Never treat our own ingress port as a camera — it's AnyCam's own web UI
    local_ip = get_local_ip()
    if local_ip and ip == local_ip and port == PORT:
        return None

    cid        = f"{ip}_{port}"
    prev_creds = prev.get("credentials")
    prev_name  = prev.get("name", hostname)
    saved_u = saved_p = ""
    if prev_creds:
        try:
            saved_u, saved_p = decrypt_creds(prev_creds)
        except Exception:
            pass

    # rc2: Run brand identification BEFORE any RTSP probe so the
    # find_rtsp_path orchestrator can apply throttle-aware short-circuits.
    # Sets host_meta["manufacturer"] in place if a brand is identified.
    #
    # 2.4.0-rc1.0: RTSP OPTIONS fingerprint runs first so brand-id has
    # access to the auth_realm and rtsp_server_header signals. Same
    # populate-then-identify pattern as Site A in the ONVIF post-scan
    # path. Skipped when port != 554 — the helper assumes RTSP service
    # on a known RTSP port; running it against port 80 is wasteful.
    if host_meta is not None:
        if port == 554:
            try:
                rtsp_fp = await loop.run_in_executor(
                    _THREAD_POOL, _rtsp_options_fingerprint, ip, 554)
                if rtsp_fp.get("server_header"):
                    host_meta["rtsp_server_header"] = rtsp_fp["server_header"]
                if rtsp_fp.get("auth_realm"):
                    host_meta["rtsp_auth_realm"] = rtsp_fp["auth_realm"]
                if rtsp_fp.get("auth_scheme"):
                    host_meta["rtsp_auth_scheme"] = rtsp_fp["auth_scheme"]
                if rtsp_fp.get("public_methods"):
                    host_meta["rtsp_public_methods"] = ",".join(
                        rtsp_fp["public_methods"])
                # 2.4.0-rc2.5: persist looks_like_rtsp flag from the
                # fingerprint pre-probe. Some RTSP servers (notably
                # Hikvision DS-2DE4A425IW) reply with status=200 to
                # OPTIONS but emit NO Server header and NO realm
                # (auth is challenged later, on DESCRIBE). Without
                # this flag the alt-port RTSP-skip optimization
                # (Fix C in rc2.4) couldn't fire on those cameras —
                # rtsp_server_header/rtsp_auth_realm/rtsp_public_methods
                # were all empty even though the host demonstrably
                # speaks RTSP. The fingerprint helper's own
                # `looks_like_rtsp` heuristic correctly identifies
                # this case (200 OK with RTSP/1.0 status line); we
                # just need to plumb it through.
                if rtsp_fp.get("looks_like_rtsp"):
                    host_meta["rtsp_speaker_confirmed"] = True
                if rtsp_fp.get("looks_like_rtsp"):
                    log.info(
                        f"  RTSP fingerprint {ip}: status="
                        f"{rtsp_fp.get('status')}, "
                        f"server={rtsp_fp.get('server_header','')!r}, "
                        f"realm={rtsp_fp.get('auth_realm','')!r}, "
                        f"elapsed={rtsp_fp.get('elapsed_ms',0):.0f}ms")
                elif rtsp_fp.get("error"):
                    log.debug(
                        f"  RTSP fingerprint {ip}: {rtsp_fp['error']}")
            except Exception as e:
                log.debug(f"  RTSP fingerprint probe failed for {ip}: {e}")

        try:
            _identify_camera_brand(host_meta)
        except Exception as e:
            log.debug(f"  brand-id pre-probe: {e}")

    def base(proto: str, url: str, status: str, display: str = "proxy") -> dict:
        d = {
            "id": cid, "ip": ip, "hostname": hostname, "port": port,
            "protocol": proto, "stream_url": url,
            "requires_credentials": False, "credentials": None,
            "name": prev_name, "status": status,
            "user_saved": bool(prev), "display": display,
            "verdict": verdict, "verdict_reason": reason,
        }
        # rc2.1: persist brand identity from host_meta onto the camera
        # record. _identify_camera_brand() set host_meta["manufacturer"]
        # in place during the pre-probe brand-id step above; without
        # this copy the identification result is silently lost when the
        # local host_meta dict goes out of scope, leaving cam["manufacturer"]
        # empty and the UI displaying just the generic ONVIF name.
        # rc2.1.1: server_header is also persisted — populated by the
        # walker if it captured a Server: line from any RTSP response.
        if host_meta:
            for k in ("manufacturer", "mac_addr", "mac_vendor",
                      "server_header",
                      # 2.4.0-rc1.0: RTSP fingerprint fields populated by
                      # the OPTIONS pre-probe at line ~10030.
                      "rtsp_server_header", "rtsp_auth_realm",
                      "rtsp_auth_scheme", "rtsp_public_methods",
                      # 2.4.0-rc2.4: early-bail state from
                      # _probe_rtsp_paths_single_socket. Persisted onto
                      # the camera record so the Deep Re-Probe button
                      # (api_deep_reprobe) can resume the walk on the
                      # remaining unwalked paths and run Layer 2 on
                      # demand.
                      "early_bail_reason", "early_bail_realm",
                      "early_bail_paths_tried",
                      "early_bail_paths_remaining",
                      "early_bail_at",
                      "page_title", "onvif_scopes"):
                v = host_meta.get(k, "")
                if v:
                    d[k] = v
            # 2.4.0-rc2.0 (Layered Stream Discovery): locked_streams is a
            # list and may legitimately be empty (= feature ran, found
            # nothing) — copy unconditionally when present in host_meta
            # so the camera record reflects "feature ran" status.
            if "locked_streams" in host_meta:
                d["locked_streams"] = host_meta["locked_streams"]
        return d

    async def _enrich_with_details(cam: dict, stream_url: str,
                                   proto: str) -> dict:
        """2.4.0-rc2.9: best-effort ffprobe of a discovered unauth
        stream URL. Merges captured codec/width/height/fps onto the
        camera dict so the focus-view dropdown can show a proper
        resolution label ("1920x1080 H264") instead of "Stream 1".
        Failure is harmless — without enrichment we just don't have
        the labels, but the camera still works. Cost is one ffprobe
        call (~3s typical, 12s max timeout) per discovered unauth
        stream — only fires for cameras that don't require creds, so
        most networks see this run zero or one times per scan."""
        try:
            d = await probe_stream_details(stream_url, proto)
            if d:
                cam.update(d)
        except Exception as e:
            log.debug(f"  probe_stream_details({stream_url!r}, "
                      f"{proto!r}): {e}")
        return cam

    if initial_protocol in ("RTSP", "DVR"):
        url = await loop.run_in_executor(
            _THREAD_POOL, find_rtsp_path, ip, port, "", "", host_meta)
        if url:
            # 2.4.0-rc2.9: probe stream details on the discovered URL so
            # the focus-view dropdown can label this entry as "1920x1080
            # H264" instead of falling back to "Stream 1". Best-effort —
            # if the probe fails (timeout, RST, weird codec), we just
            # don't have enrichment data and the dropdown stays at the
            # numbered fallback. Also benefits adaptive snap_loop which
            # uses stream_codec for decoder selection. Same treatment
            # applies below for saved-creds RTSP, MJPEG, and HLS.
            cam = base("RTSP", url, "ready")
            return await _enrich_with_details(cam, url, "RTSP")
        if saved_u:
            url = await loop.run_in_executor(
                _THREAD_POOL, find_rtsp_path, ip, port,
                saved_u, saved_p, host_meta)
            if url:
                cam = base("RTSP", url, "ready")
                cam["credentials"] = prev_creds
                return await _enrich_with_details(cam, url, "RTSP")
        cam = base("RTSP", "", "needs_credentials")
        cam["requires_credentials"] = True
        return cam

    if initial_protocol == "RTMP" or port in (1935, 1936):
        ok = await loop.run_in_executor(_THREAD_POOL, probe_rtmp, ip, port)
        if ok:
            return base("RTMP", f"rtmp://{ip}:{port}/live/stream", "ready")

    if initial_protocol in ("HTTP", "ONVIF", "UNKNOWN"):
        url = await loop.run_in_executor(_THREAD_POOL, probe_mjpeg_http, ip, port, "", "")
        if url:
            cam = base("MJPEG", url, "ready", "mjpeg")
            return await _enrich_with_details(cam, url, "MJPEG")
        if saved_u:
            url = await loop.run_in_executor(_THREAD_POOL, probe_mjpeg_http, ip, port, saved_u, saved_p)
            if url:
                cam = base("MJPEG", url, "ready", "mjpeg")
                cam["credentials"] = prev_creds
                return await _enrich_with_details(cam, url, "MJPEG")

        url = await loop.run_in_executor(_THREAD_POOL, probe_hls, ip, port, "", "")
        if url:
            cam = base("HLS", url, "ready", "hls")
            return await _enrich_with_details(cam, url, "HLS")
        if saved_u:
            url = await loop.run_in_executor(_THREAD_POOL, probe_hls, ip, port, saved_u, saved_p)
            if url:
                cam = base("HLS", url, "ready", "hls")
                cam["credentials"] = prev_creds
                return await _enrich_with_details(cam, url, "HLS")

        # 2.4.0-rc3.5 Leak G fix: skip the find_rtsp_path fall-through
        # call when the canonical RTSP port already established speaker
        # status for this host. Without this gate, an alt port whose
        # nmap banner doesn't contain "rtsp"/"camera" (so initial="HTTP"
        # from the start) bypasses the upstream skip-gate and ends up
        # here, opening a fresh TCP socket per alt port to walk RTSP
        # paths the camera already proved (on the canonical port) it
        # doesn't expose. For the Microseven on a populated network,
        # this added 3 unnecessary Layer 1 walks per scan against a
        # camera with a 5-second per-IP TCP rate-limit, which was
        # enough to push it into a firmware-level lockout. Other
        # protocols above (MJPEG, HLS) and below (WebRTC, WS-RTSP)
        # are unaffected — they're legitimately HTTP-port-bound and
        # don't multiply RTSP socket opens.
        if host_meta and host_meta.get("host_skip_layer1_alt"):
            log.info(f"  RTSP fall-through skipped: {ip}:{port} — "
                     f"canonical RTSP port already established speaker "
                     f"status (no alt-port Layer 1 walk needed)")
        else:
            url = await loop.run_in_executor(
                _THREAD_POOL, find_rtsp_path, ip, port, "", "", host_meta)
            if url:
                cam = base("RTSP", url, "ready")
                return await _enrich_with_details(cam, url, "RTSP")

        wrtc = await loop.run_in_executor(_THREAD_POOL, probe_webrtc, ip, port)
        if wrtc:
            cam = base("WebRTC", wrtc, "info", "webrtc")
            cam["info"] = "WebRTC signaling detected. Direct browser negotiation required."
            cam["signaling_url"] = wrtc
            return cam

        ws = await loop.run_in_executor(_THREAD_POOL, probe_ws_rtsp, ip, port)
        if ws:
            cam = base("WS-RTSP", ws, "info", "wsrtsp")
            cam["info"] = "WS-RTSP endpoint detected."
            cam["ws_url"] = ws
            return cam

        # 2.4.0-rc2.3: stricter HTTP-only fall-through. Previously this
        # 2.4.0-rc2.4: stricter verdict gate — OUI brand-id alone is
        # NOT sufficient evidence to create a cred-prompt card. Many
        # camera-vendor OUIs (TP-Link, Ubiquiti, Hanwha, Bosch, etc.)
        # are shared with the same vendor's networking gear (switches,
        # routers, access points). CrystalHeeler's test system A surfaced 3 false
        # positives in rc2.3: TP-Link Tapo / Kasa OUI matched a
        # TP-Link switch on .12; Ubiquiti UniFi OUI matched two UniFi
        # APs on .13/.14. None of those devices are cameras. The fix
        # is to require the brand match to be CORROBORATED by at
        # least one service-level signal — something only cameras
        # produce, not the vendor's other product lines:
        #   • ONVIF scope present (only cameras speak ONVIF)
        #   • RTSP fingerprint captured (host speaks RTSP)
        #   • Page title contains brand keyword (means it's the camera
        #     UI, not a switch/router admin page)
        #   • Server header contains brand keyword (HTTP server
        #     identified itself with the camera-software name)
        #   • Nmap product banner contains brand keyword (service
        #     fingerprint matched)
        # OUI-only matches with NONE of the above drop to verdict
        # suppressed → no card. Users with truly exotic cameras
        # Claude doesn't have a brand entry for can still reach
        # them via "Add Camera Manually" / pscan-ip input field.
        if verdict == "camera":
            cam = base("HTTP", "", "needs_credentials")
            cam["requires_credentials"] = True
            return cam
        if verdict == "uncertain" and host_meta:
            has_brand = bool(host_meta.get("manufacturer"))
            has_rtsp_speaker = bool(
                host_meta.get("rtsp_server_header")
                or host_meta.get("rtsp_auth_realm")
                or host_meta.get("rtsp_public_methods"))
            # Service-level corroboration check
            brand = (host_meta.get("manufacturer") or "").lower()
            page_title = (host_meta.get("page_title") or "").lower()
            srv_hdr = (host_meta.get("server_header") or "").lower()
            nmap_p = (host_meta.get("nmap_product") or "").lower()
            onvif_scopes = (host_meta.get("onvif_scopes") or "").lower()
            # Substring match: if a non-trivial brand-name token
            # appears in any service-level field, count as corroborated
            brand_tokens = [t for t in re.split(r'[\s/.\-]+', brand) if len(t) >= 4]
            has_brand_in_service = False
            for tok in brand_tokens:
                if (tok in page_title or tok in srv_hdr
                        or tok in nmap_p or tok in onvif_scopes):
                    has_brand_in_service = True
                    break
            has_onvif = bool(onvif_scopes)
            has_corroboration = (has_rtsp_speaker or has_onvif
                                 or has_brand_in_service)
            if has_brand and has_corroboration:
                cam = base("HTTP", "", "needs_credentials")
                cam["requires_credentials"] = True
                return cam
            if has_rtsp_speaker:
                # Speaks RTSP even without identified brand — keep card
                cam = base("HTTP", "", "needs_credentials")
                cam["requires_credentials"] = True
                return cam
            if has_brand and not has_corroboration:
                log.info(f"  Card suppressed: {ip}:{port} "
                         f"brand={host_meta.get('manufacturer')!r} from OUI "
                         f"alone, no service-level corroboration")

    return None



async def run_scan() -> None:
    """
    4-stage network camera discovery:
      Stage 1 — ARP + ONVIF/SSDP/mDNS multicast (parallel)
      Stage 2 — Focused nmap port scan on live hosts
      Stage 3 — Per-port stream probing
      Stage 4 — Optional broader sweep on silent live hosts
    """
    global SCAN_CANCELLED
    SCAN_CANCELLED = False

    SCAN_STATE.update(running=True, progress=0, stage=1,
                      stage_label="Stage 1/4 — Live host & multicast discovery",
                      message="Stage 1/4 — ARP scan + ONVIF/SSDP/mDNS discovery…")
    loop = asyncio.get_event_loop()

    # 2.4.0-rc2.6: pending-flush card buffer. New cards discovered
    # during this scan accumulate here instead of CAMERAS until the
    # dedup pass runs at the end. This avoids the ~40-80s window
    # where a multi-port host would render as 3-5 separate cards
    # before dedup collapses them — letting the user click "Enter
    # creds" on a card that's about to disappear.
    global PENDING_CAMERAS
    PENDING_CAMERAS = {}

    try:
        subnet  = await loop.run_in_executor(_THREAD_POOL, get_local_subnet)
        gateway = await loop.run_in_executor(_THREAD_POOL, get_default_gateway)
        log.info(f"Subnet: {subnet}  Gateway: {gateway}")

        arp_hosts, onvif_results, ssdp_results, mdns_results = await asyncio.gather(
            loop.run_in_executor(_THREAD_POOL, discover_live_hosts, subnet),
            loop.run_in_executor(_THREAD_POOL, onvif_discover, 5),
            loop.run_in_executor(_THREAD_POOL, ssdp_discover, 5),
            loop.run_in_executor(_THREAD_POOL, mdns_discover, 5),
        )

        multicast_ips = (
            {r["ip"] for r in onvif_results} |
            {r["ip"] for r in ssdp_results if r.get("is_camera")} |
            {r["ip"] for r in mdns_results}
        )
        all_live = (arp_hosts | multicast_ips) - BLACKLIST
        if gateway:
            all_live.discard(gateway)

        # Remove Docker/HA-internal bridge IPs and APIPA — never real camera targets.
        # 172.x.x.x = HA Supervisor Docker bridge (e.g. 172.30.32.1) found by mDNS.
        # 169.254.x.x = link-local/APIPA addresses.
        all_live = {ip for ip in all_live
                    if not ip.startswith("172.")
                    and not ip.startswith("169.254.")}
        log.info(f"Live: {len(all_live)} host(s) ({len(arp_hosts)} ARP, {len(multicast_ips)} multicast)")

        SCAN_STATE.update(progress=25, stage=2,
                          stage_label="Stage 2/4 — Camera port scan",
                          message=f"Stage 2/4 — Scanning camera ports on {len(all_live)} live host(s)…")

        if SCAN_CANCELLED:
            return

        nmap_results  = await loop.run_in_executor(_THREAD_POOL, focused_nmap_scan, sorted(all_live))
        responding_ips = {h["ip"] for h in nmap_results}

        # rc2.1: build a per-IP lookup of (mac_addr, mac_vendor, nmap_product)
        # so the ONVIF unauth-RTSP shortcut downstream can identify the
        # camera's brand from MAC OUI before its RTSP probe runs. Without
        # this, ONVIF-discovered cameras whose ONVIF returns 0 profiles
        # (e.g. Microseven) fell through to find_rtsp_path with no
        # host_meta, brand-id never fired, and the UI showed only the
        # generic ONVIF name "IPCAM" instead of "Hipcam/Microseven".
        scan_meta_by_ip: dict[str, dict] = {}
        for _h in nmap_results:
            _ports = _h.get("open_ports", [])
            scan_meta_by_ip[_h["ip"]] = {
                "mac_addr":   _h.get("mac_addr", ""),
                "mac_vendor": _h.get("mac_vendor", ""),
                "nmap_product": (_ports[0].get("product", "") if _ports else ""),
            }

        SCAN_STATE.update(progress=55, stage=3,
                          stage_label="Stage 3/4 — Stream probing",
                          message=f"Stage 3/4 — Probing {len(nmap_results)} responding host(s)…")

        # Populate ARP_HOSTS for Port Scan tab (all live hosts, not just camera ones)
        global ARP_HOSTS
        ARP_HOSTS = [{"ip": h["ip"], "hostname": h.get("hostname", h["ip"]),
                      "mac": h.get("mac", ""), "vendor": h.get("vendor", "")}
                     for h in nmap_results]
        nmap_ips = {h["ip"] for h in nmap_results}
        for silent_ip in sorted(all_live - nmap_ips):
            ARP_HOSTS.append({"ip": silent_ip, "hostname": silent_ip,
                               "mac": "", "vendor": ""})

        # Preserve cameras the user has already saved (creds, names, etc.)
        saved = {cid: c for cid, c in CAMERAS.items() if c.get("user_saved")}
        CAMERAS.clear()
        CAMERAS.update(saved)

        total = max(len(nmap_results), 1)
        for idx, host in enumerate(nmap_results):
            if SCAN_CANCELLED:
                break
            ip, hostname = host["ip"], host.get("hostname", host["ip"])
            SCAN_STATE.update(
                progress=55 + int(25 * idx / total),
                message=f"Stage 3/4 — Probing {ip} ({idx+1}/{len(nmap_results)})…")

            verdict, reason = classify_device(host)
            # 2.4.0-rc2.4: port-ordering optimization. Probe canonical
            # RTSP ports (554, 8554, 10554) FIRST — if any of them
            # establishes RTSP-speaker status (returns RTSP-format
            # response with realm/server header), subsequent HTTP-only
            # ports of the same IP can skip their full RTSP probe and
            # use a fast HTTP/MJPEG/HLS-only path. Saves the ~5-15s
            # per HTTP-only port that Layer 1 currently burns walking
            # paths against a port that won't speak RTSP.
            CANONICAL_RTSP_PORTS = {554, 8554, 10554}
            open_ports_sorted = sorted(
                host.get("open_ports", []),
                key=lambda p: (
                    0 if p["port"] in CANONICAL_RTSP_PORTS else 1,
                    p["port"],
                ),
            )
            # Per-IP "we already established RTSP-speaker status" flag
            # — reset per host so cross-host state doesn't leak.
            host_has_rtsp_speaker = False
            # 2.4.0-rc2.9: similar per-IP "we identified a brand with
            # skip_layer2: True on an earlier port" flag. Lorex/Dahua
            # DVR-NVR family is the canonical case: port 554 IDs as
            # the full DVR-NVR Family entry (skip_layer2: True), but
            # port 80 IDs as plain "Lorex" (different STREAM_DB row,
            # no skip_layer2). Without inheritance, port 80 would run
            # Layer 2 for ~45s wastefully on a brand we already know
            # can't speak Layer 2. Once any port on this IP triggers
            # the skip_layer2 short-circuit in find_rtsp_path, the
            # flag goes True and subsequent ports propagate it via
            # host_meta["host_skip_layer2"].
            host_has_skip_layer2 = False
            for port_info in open_ports_sorted:
                port = port_info["port"]
                cid  = f"{ip}_{port}"
                if cid in BLACKLIST:
                    continue
                # 2.4.0-rc2.4: FEEDBACK fingerprint check. If a past
                # "Not a Camera" click has a high-confidence
                # fingerprint match (same OUI + same product/port +
                # explicit reason_type), skip this candidate without
                # probing. Conservative — same OUI alone never skips.
                fb_skip, fb_reason = _matches_feedback_fingerprint(host, port)
                if fb_skip:
                    log.info(f"  FEEDBACK fingerprint match: skipping "
                             f"{ip}:{port} — {fb_reason}")
                    continue
                initial = _initial_protocol(port, port_info.get("service", ""),
                                             port_info.get("product", ""))
                # 2.4.0-rc2.4: if we already confirmed RTSP-speaker on
                # a canonical RTSP port for this IP, downgrade an
                # initial="RTSP" classification on a non-canonical
                # port to the HTTP/MJPEG/HLS path. The host won't
                # speak RTSP on its admin/HTTP ports — burning the
                # full 25-31 path Layer 1 walk is wasted.
                if (host_has_rtsp_speaker
                        and port not in CANONICAL_RTSP_PORTS
                        and initial == "RTSP"):
                    log.info(f"  RTSP probe skipped: {ip}:{port} — "
                             f"canonical RTSP port already established "
                             f"speaker status; treating as HTTP-only")
                    initial = "HTTP"
                prev = saved.get(cid, {})
                # 2.4.0-rc4.0 Leak G tightening: pre-compute lockout
                # signals so the host_skip_layer1_alt flag below can
                # widen its skip condition. The 2.4.0-rc3.5 gate fired
                # only when the canonical port had successfully confirmed
                # speaker status (host_has_rtsp_speaker=True) — which
                # works for healthy cameras but NOT for a camera already
                # in firmware-level RTSP lockout: the canonical port
                # RSTs on path 1, never confirms speaker status, the
                # flag stays False, and we proceed to walk the alt ports
                # and add insult to injury. Field log 2026-05-07 0018
                # showed exactly this — 4 walks against a locked Microseven
                # despite ACD escalation, because the gate didn't fire.
                #
                # New signal sources, all dict-key-cheap:
                #   1. brand-id says rate_limit_per_ip_tcp (the brand
                #      pre-probe in find_rtsp_path already populated
                #      mac_vendor/nmap_product, so _identify_camera_brand
                #      hits the same code path used elsewhere)
                #   2. _RST_OBSERVED has any timestamp for this IP (means
                #      a prior port's walker bailed on RST/broken-pipe)
                #   3. _ACD_ESCALATED is active for this IP (the 2.4.0-
                #      rc3.5 ACD escalation is in force)
                #
                # When brand-throttled AND (RST seen OR ACD active), we
                # treat the host as suspected-locked and skip alt-port
                # Layer 1 walks even without canonical-port confirmation.
                # No false positives for healthy cameras: brand-throttled
                # alone isn't enough; we need at least one RST signal.
                # No false positives for non-throttled brands: the brand
                # check filters them out so Hikvision/Dahua/Axis/etc.
                # get the existing behavior unchanged.
                _brand_entry = _identify_camera_brand({
                    "ip":            ip,
                    "hostname":      hostname,
                    "vendor":        host.get("mac_vendor", ""),
                    "mac_vendor":    host.get("mac_vendor", ""),
                    "nmap_product":  port_info.get("product", ""),
                    "verdict_reason": reason,
                })
                _brand_throttled = bool(
                    _brand_entry
                    and _brand_entry.get("throttle_type") == "rate_limit_per_ip_tcp"
                )
                _now = time.monotonic()
                _has_rst_signal = bool(_RST_OBSERVED.get(ip)) or (
                    _ACD_ESCALATED.get(ip, 0.0) > _now
                )
                _alt_skip_via_lockout = (
                    _brand_throttled
                    and _has_rst_signal
                    and port not in CANONICAL_RTSP_PORTS
                )
                if _alt_skip_via_lockout and not host_has_rtsp_speaker:
                    log.info(
                        f"  Alt-port Layer 1 walk pre-skipped: {ip}:{port} "
                        f"— brand={_brand_entry.get('name', '?')} is "
                        f"rate_limit_per_ip_tcp AND lockout signals present "
                        f"(RST observed={bool(_RST_OBSERVED.get(ip))}, "
                        f"ACD active={_ACD_ESCALATED.get(ip, 0.0) > _now}) "
                        f"— canonical port never confirmed speaker but "
                        f"camera is misbehaving; further walks would extend "
                        f"the lockout"
                    )
                # rc2: assemble a host_meta dict so brand identification
                # can run BEFORE the RTSP probe begins (mac_vendor + nmap
                # service banner + product feed into _identify_camera_brand)
                host_meta = {
                    "ip":            ip,
                    "hostname":      hostname,
                    "mac_addr":      host.get("mac_addr", ""),
                    "mac_vendor":    host.get("mac_vendor", ""),
                    "vendor":        host.get("mac_vendor", ""),
                    "nmap_product":  port_info.get("product", ""),
                    "verdict_reason": reason,
                    # 2.4.0-rc2.9: propagate skip_layer2 across ports
                    # for the same IP. False on the first port; True
                    # on subsequent ports after a skip_layer2 brand
                    # was identified upstream. Read by find_rtsp_path
                    # to apply the Layer 2 short-circuit even when
                    # the per-port brand match wouldn't fire it.
                    "host_skip_layer2": host_has_skip_layer2,
                    # 2.4.0-rc3.5 Leak G fix: parallel skip flag for
                    # Layer 1. The pre-existing gate above (lines 12552-
                    # 12558) handles the case where _initial_protocol
                    # returned "RTSP" for a non-canonical port — but
                    # when nmap classifies the alt port as plain HTTP
                    # (no "rtsp"/"camera" in the banner — the common
                    # case for Hipcam-family on port 80, where nmap
                    # just sees the GoAhead web admin), `initial` is
                    # already "HTTP", the gate's `initial == "RTSP"`
                    # condition is False, no downgrade fires, and
                    # _probe_host_port falls through into the HTTP
                    # branch which calls find_rtsp_path anyway. This
                    # flag lets _probe_host_port suppress that fall-
                    # through call when the canonical port has already
                    # established speaker status — closing the loophole
                    # without changing the existing behavior for
                    # initial="RTSP" alt ports.
                    # 2.4.0-rc4.0 tightening: also fire when the brand
                    # is throttled AND lockout signals (RST or ACD) are
                    # present, even if speaker status was never confirmed
                    # — see _alt_skip_via_lockout above for rationale.
                    "host_skip_layer1_alt": (
                        (host_has_rtsp_speaker
                         and port not in CANONICAL_RTSP_PORTS)
                        or _alt_skip_via_lockout
                    ),
                }
                cam  = await _probe_host_port(ip, port, hostname, initial,
                                              prev, verdict, reason, loop,
                                              host_meta=host_meta)
                if cam:
                    _publish_scan_card(cam)
                # 2.4.0-rc2.4: detect RTSP-speaker status from any of the
                # signals find_rtsp_path / fingerprint pre-probe wrote
                # to host_meta. If the host responded RTSP/-format on
                # this canonical port, all subsequent non-canonical
                # ports can skip RTSP probing.
                # 2.4.0-rc2.5: also accept rtsp_speaker_confirmed flag
                # set by the fingerprint pre-probe — covers cameras
                # like Hikvision DS-2DE that respond 200 OK to OPTIONS
                # without emitting Server or realm headers (auth
                # challenged later on DESCRIBE).
                if not host_has_rtsp_speaker:
                    if (host_meta.get("rtsp_server_header")
                            or host_meta.get("rtsp_auth_realm")
                            or host_meta.get("rtsp_public_methods")
                            or host_meta.get("rtsp_speaker_confirmed")):
                        host_has_rtsp_speaker = True
                        log.info(f"  RTSP speaker confirmed for {ip} — "
                                 f"alt ports will skip Layer 1 path walk")
                # 2.4.0-rc2.9: update host_has_skip_layer2 after the
                # port's probe. find_rtsp_path writes
                # host_meta["brand_skip_layer2"]=True when its skip-
                # layer2 short-circuit fires, so subsequent ports on
                # this IP can inherit the decision.
                if not host_has_skip_layer2:
                    if host_meta.get("brand_skip_layer2"):
                        host_has_skip_layer2 = True
                        log.info(f"  skip_layer2 inherited for {ip} — "
                                 f"alt ports will also skip Layer 2 walks")

        # Stage 4: optional broad sweep on silent live hosts
        silent = sorted(all_live - responding_ips)
        if SCAN_OPTIONS.get("broad_sweep") and silent and not SCAN_CANCELLED:
            SCAN_STATE.update(progress=82, stage=4,
                              stage_label="Stage 4/4 — Deeper Scan",
                              message=f"Stage 4/4 — Deeper scan on {len(silent)} unresponsive host(s)…")
            broad_results = await loop.run_in_executor(_THREAD_POOL, broad_nmap_scan, silent)
            for host in broad_results:
                if SCAN_CANCELLED:
                    break
                ip, hostname = host["ip"], host.get("hostname", host["ip"])
                verdict, reason = classify_device(host)
                for port_info in host.get("open_ports", []):
                    port = port_info["port"]
                    cid  = f"{ip}_{port}"
                    if cid in BLACKLIST:
                        continue
                    # 2.4.0-rc2.4: FEEDBACK fingerprint check (broad-sweep)
                    fb_skip, fb_reason = _matches_feedback_fingerprint(host, port)
                    if fb_skip:
                        log.info(f"  FEEDBACK fingerprint match: skipping "
                                 f"{ip}:{port} — {fb_reason}")
                        continue
                    initial = _initial_protocol(port, port_info.get("service", ""),
                                                 port_info.get("product", ""))
                    prev = saved.get(cid, {})
                    # rc2: pass host_meta for brand-aware probe short-circuits
                    host_meta = {
                        "ip":            ip,
                        "hostname":      hostname,
                        "mac_addr":      host.get("mac_addr", ""),
                        "mac_vendor":    host.get("mac_vendor", ""),
                        "vendor":        host.get("mac_vendor", ""),
                        "nmap_product":  port_info.get("product", ""),
                        "verdict_reason": reason,
                    }
                    cam  = await _probe_host_port(ip, port, hostname, initial,
                                                  prev, verdict, reason, loop,
                                                  host_meta=host_meta)
                    if cam:
                        _publish_scan_card(cam)

        # Merge multicast-only ONVIF cameras not found by nmap
        for onvif in onvif_results:
            ip = onvif["ip"]
            if ip == gateway or ip in BLACKLIST:
                continue
            # 2.4.0-rc2.6: also check PENDING_CAMERAS — cards just
            # discovered this scan haven't flushed to CAMERAS yet,
            # but they DO exist for the purposes of ONVIF dup-check.
            _all_cams = list(CAMERAS.values()) + list(
                (PENDING_CAMERAS or {}).values())
            existing = [c for c in _all_cams if c["ip"] == ip]
            if existing:
                for cam in existing:
                    cam["onvif"]  = True
                    cam["xaddrs"] = onvif.get("xaddrs", cam.get("xaddrs", ""))
            else:
                cid  = f"{ip}_onvif"
                prev = saved.get(cid, {})
                # Try unauthenticated RTSP first — some cameras (e.g. Microseven
                # with "RTSP Authentication" disabled) serve streams openly even
                # though their ONVIF/HTTP ports require auth. If unauth RTSP
                # works AND the user hasn't already saved creds, skip the cred
                # prompt entirely and create a ready card right away.
                unauth_url = ""
                # rc2.1: pull mac_vendor + nmap_product from the focused
                # scan results (built above into scan_meta_by_ip). This
                # gives the ONVIF flow the OUI vendor signal it needs to
                # identify Microseven/Hipcam-family cameras (whose ONVIF
                # often returns 0 profiles, forcing the path-walking
                # shortcut taken below).
                _scan_extra = scan_meta_by_ip.get(ip, {})
                onvif_meta = {
                    "ip": ip,
                    "hostname":     onvif.get("name", ip),
                    "name":         onvif.get("name", ""),
                    "xaddrs":       onvif.get("xaddrs", ""),
                    "mac_addr":     _scan_extra.get("mac_addr", ""),
                    "mac_vendor":   _scan_extra.get("mac_vendor", ""),
                    "vendor":       _scan_extra.get("mac_vendor", ""),
                    "nmap_product": _scan_extra.get("nmap_product", ""),
                    # rc2.1.1: ONVIF scopes string from WS-Discovery
                    # response — often contains manufacturer/hardware/
                    # model identifiers that brand-id uses to match
                    # CAMERA_DB entries. Critical for cameras whose
                    # mac_vendor isn't available (Microseven not in
                    # nmap_results due to its own TCP rate-limit).
                    "onvif_scopes": onvif.get("onvif_scopes", ""),
                }
                # rc2.2: HTTP identity probe before RTSP. Catches cameras
                # whose ONVIF returns only a generic name and whose MAC
                # OUI isn't available (Microseven case — empirically
                # verified to return `Server: Hipcam` HTTP header and
                # `<title>Microseven Cameras...</title>` page title, both
                # of which match the Hipcam/Microseven CAMERA_DB entry).
                # Runs on port 80 first, then xaddrs port if different.
                # Doesn't fail loudly if the camera has HTTP disabled —
                # rc2.1.1 walker Server-header capture is the fallback.
                try:
                    http_id = await loop.run_in_executor(
                        _THREAD_POOL, probe_http_identity, ip, 80, 4)
                    if http_id.get("server"):
                        onvif_meta["server_header"] = http_id["server"]
                    if http_id.get("title"):
                        onvif_meta["page_title"] = http_id["title"]
                    if http_id.get("manufacturer"):
                        # probe_http_identity already matched against
                        # CAMERA_DB — adopt directly rather than re-running
                        onvif_meta["manufacturer"] = http_id["manufacturer"]
                        log.info(f"  HTTP identity {ip}: "
                                 f"{http_id['manufacturer']!r} "
                                 f"(server={http_id.get('server','')!r}, "
                                 f"title={http_id.get('title','')!r})")
                    elif http_id.get("server") or http_id.get("title"):
                        log.info(f"  HTTP identity {ip}: unmatched "
                                 f"(server={http_id.get('server','')!r}, "
                                 f"title={http_id.get('title','')!r})")
                except Exception as e:
                    log.debug(f"  HTTP identity probe failed for {ip}: {e}")

                # 2.4.0-rc1.0: RTSP OPTIONS fingerprint.
                # One TCP open to port 554, one OPTIONS request, capture
                # the Server header, auth realm/scheme, and Public methods.
                # Populates onvif_meta so _identify_camera_brand can score
                # on rtsp_realm_regex, and the Identity panel can display
                # realm + RTSP server header.
                # Worst-case cost: ~3s timeout per dead host. Zero risk to
                # throttled/lockout-protected hosts (OPTIONS doesn't auth).
                try:
                    rtsp_fp = await loop.run_in_executor(
                        _THREAD_POOL, _rtsp_options_fingerprint, ip, 554)
                    if rtsp_fp.get("server_header"):
                        onvif_meta["rtsp_server_header"] = rtsp_fp["server_header"]
                    if rtsp_fp.get("auth_realm"):
                        onvif_meta["rtsp_auth_realm"] = rtsp_fp["auth_realm"]
                    if rtsp_fp.get("auth_scheme"):
                        onvif_meta["rtsp_auth_scheme"] = rtsp_fp["auth_scheme"]
                    if rtsp_fp.get("public_methods"):
                        onvif_meta["rtsp_public_methods"] = ",".join(
                            rtsp_fp["public_methods"])
                    if rtsp_fp.get("looks_like_rtsp"):
                        log.info(
                            f"  RTSP fingerprint {ip}: status="
                            f"{rtsp_fp.get('status')}, "
                            f"server={rtsp_fp.get('server_header','')!r}, "
                            f"realm={rtsp_fp.get('auth_realm','')!r}, "
                            f"elapsed={rtsp_fp.get('elapsed_ms',0):.0f}ms")
                    elif rtsp_fp.get("error"):
                        log.debug(
                            f"  RTSP fingerprint {ip}: {rtsp_fp['error']}")

                    # Re-run brand identification — _identify_camera_brand
                    # now has the realm available and may upgrade an earlier
                    # weak match (or identify a brand we missed entirely).
                    if onvif_meta.get("rtsp_auth_realm"):
                        _identify_camera_brand(onvif_meta, force=True)
                except Exception as e:
                    log.debug(f"  RTSP fingerprint probe failed for {ip}: {e}")

                if not prev.get("credentials"):
                    unauth_url = await loop.run_in_executor(
                        _THREAD_POOL, find_rtsp_path, ip, 554, "", "", onvif_meta)
                # rc2.1: find_rtsp_path → _identify_camera_brand mutated
                # onvif_meta in place, setting manufacturer if a brand
                # was identified. Capture it for the record below so the
                # UI shows the real brand (e.g. "Hipcam/Microseven")
                # instead of just the generic ONVIF name ("IPCAM").
                # rc2.1.1: also capture server_header — populated by the
                # walker mid-probe — for downstream display + diagnosis
                # in the camera Identity panel.
                # rc2.2: page_title also captured — set by HTTP probe above.
                _brand   = onvif_meta.get("manufacturer", "")
                _mac_v   = onvif_meta.get("mac_vendor", "")
                _mac_a   = onvif_meta.get("mac_addr", "")
                _srv_hdr = onvif_meta.get("server_header", "")
                _pg_ttl  = onvif_meta.get("page_title", "")
                # 2.4.0-rc1.0: RTSP-layer fingerprint fields (distinct from
                # HTTP-layer server_header / page_title above).
                _rtsp_srv  = onvif_meta.get("rtsp_server_header", "")
                _rtsp_rlm  = onvif_meta.get("rtsp_auth_realm", "")
                _rtsp_sch  = onvif_meta.get("rtsp_auth_scheme", "")
                _rtsp_pub  = onvif_meta.get("rtsp_public_methods", "")
                # 2.4.0-rc2.0: locked_streams from path walker
                _locked    = onvif_meta.get("locked_streams", [])
                if unauth_url:
                    log.info(f"  ONVIF {ip}: unauthenticated RTSP works "
                             f"({_strip_creds(unauth_url)}) — skipping cred prompt")
                    _publish_scan_card({
                        "id": cid, "ip": ip, "hostname": onvif["name"],
                        "port": 554, "protocol": "RTSP",
                        "stream_url": unauth_url,
                        "requires_credentials": False,
                        "credentials": None,
                        "name": prev.get("name", onvif["name"]),
                        "xaddrs": onvif.get("xaddrs", ""),
                        "status": "ready",
                        "rtsp_probe_ok": True,
                        "onvif": True, "user_saved": bool(prev), "display": "proxy",
                        "verdict": "camera", "verdict_reason": "ONVIF + unauth RTSP",
                        "manufacturer":  _brand,
                        "mac_addr":      _mac_a,
                        "mac_vendor":    _mac_v,
                        "server_header": _srv_hdr,
                        # rc2.2: persist page_title + onvif_scopes so
                        # the cred-auth dict-replacement at line ~6269
                        # can preserve them, and so they're visible in
                        # the camera Identity panel.
                        "page_title":    _pg_ttl,
                        "onvif_scopes":  onvif_meta.get("onvif_scopes", ""),
                        # 2.4.0-rc1.0: RTSP fingerprint fields
                        "rtsp_server_header":  _rtsp_srv,
                        "rtsp_auth_realm":     _rtsp_rlm,
                        "rtsp_auth_scheme":    _rtsp_sch,
                        "rtsp_public_methods": _rtsp_pub,
                        # 2.4.0-rc2.0: locked-stream candidates from
                        # the continued path walk after first success.
                        # UI shows badge + modal when len > 0 AND no
                        # creds saved. Always written (may be empty).
                        "locked_streams":      _locked,
                    })
                else:
                    _publish_scan_card({
                        "id": cid, "ip": ip, "hostname": onvif["name"],
                        "port": 80, "protocol": "ONVIF",
                        "stream_url": prev.get("stream_url", ""),
                        "requires_credentials": True,
                        "credentials": prev.get("credentials"),
                        "name": prev.get("name", onvif["name"]),
                        "xaddrs": onvif.get("xaddrs", ""),
                        "status": "needs_credentials",
                        "onvif": True, "user_saved": bool(prev), "display": "proxy",
                        "verdict": "camera", "verdict_reason": "ONVIF discovered",
                        "manufacturer":  _brand,
                        "mac_addr":      _mac_a,
                        "mac_vendor":    _mac_v,
                        "server_header": _srv_hdr,
                        # rc2.2: see comment above
                        "page_title":    _pg_ttl,
                        "onvif_scopes":  onvif_meta.get("onvif_scopes", ""),
                        # 2.4.0-rc1.0: RTSP fingerprint fields
                        "rtsp_server_header":  _rtsp_srv,
                        "rtsp_auth_realm":     _rtsp_rlm,
                        "rtsp_auth_scheme":    _rtsp_sch,
                        "rtsp_public_methods": _rtsp_pub,
                        # 2.4.0-rc2.0: locked-stream candidates (may be
                        # empty if path-walker didn't enable collection
                        # for this camera).
                        "locked_streams":      _locked,
                    })

        # Merge multicast-only SSDP cameras
        for ssdp in ssdp_results:
            if not ssdp.get("is_camera"):
                continue
            ip = ssdp["ip"]
            if ip == gateway or ip in BLACKLIST:
                continue
            # 2.4.0-rc2.6: also check PENDING_CAMERAS so we don't double-
            # add a card that was just published this scan but hasn't
            # flushed yet.
            _all_cams = list(CAMERAS.values()) + list(
                (PENDING_CAMERAS or {}).values())
            if not any(c["ip"] == ip for c in _all_cams):
                cid  = f"{ip}_ssdp"
                prev = saved.get(cid, {})
                _publish_scan_card({
                    "id": cid, "ip": ip, "hostname": ssdp.get("name", ip),
                    "port": 80, "protocol": "HTTP",
                    "stream_url": "", "requires_credentials": True,
                    "credentials": prev.get("credentials"),
                    "name": prev.get("name", ssdp.get("name", ip)),
                    "status": "needs_credentials",
                    "user_saved": bool(prev), "display": "proxy",
                    "verdict": "camera", "verdict_reason": "SSDP/UPnP discovered",
                })

        # 2.4.0-rc2.3: per-IP card dedup. rc2.2's faster scan now finds
        # all open ports on a host, which legitimately produces one card
        # per (IP, port) — but for printers/IoT/web-admin devices that
        # was creating 3+ cards per device (e.g. HP printer at 80/443/
        # 8080 → 3 "Credentials required" cards on the same printer).
        # Multi-stream cameras still need multiple cards (e.g. main +
        # sub stream on different paths), so the rule is conservative:
        #   • Keep all "ready" cards (working streams)
        #   • Keep all user-saved cards (user has interacted with them)
        #   • Keep all cards with stream_url populated
        #   • Among the remaining "needs_credentials"/"info" cards on
        #     the same IP, keep ONE — the highest-priority protocol.
        # If a "ready" card exists on an IP, all hint/needs-creds cards
        # on that IP are suppressed (we already have a working stream;
        # no need to prompt for creds on the HTTP admin port).
        _PROTO_RANK = {
            "RTSP":    100, "ONVIF":   95, "DVR":   90,
            "MJPEG":    80, "HLS":     75, "RTMP":  70,
            "WS-RTSP":  50, "WebRTC":  45,
            "HTTP":     20,
        }
        def _dedup_rank(c: dict) -> tuple:
            # Higher tuple = keep. Sort descending and pick first.
            return (
                1 if c.get("status") == "ready" else 0,
                1 if c.get("user_saved") else 0,
                1 if c.get("stream_url") else 0,
                _PROTO_RANK.get(c.get("protocol", ""), 0),
                # Tie-breaker: prefer lower port (554 < 8080) — usually
                # the manufacturer-default stream port is lower.
                -int(c.get("port", 65535)),
            )
        # 2.4.0-rc2.6: dedup operates on the union of CAMERAS (user-
        # saved cards preserved at scan start) and PENDING_CAMERAS
        # (newly-discovered cards from this scan, accumulated via
        # _publish_scan_card). After dedup, survivors are flushed
        # into CAMERAS and PENDING_CAMERAS is cleared.
        _all_cards: dict = {}
        _all_cards.update(CAMERAS)
        if PENDING_CAMERAS:
            _all_cards.update(PENDING_CAMERAS)
        by_ip: dict[str, list[dict]] = {}
        for c in _all_cards.values():
            by_ip.setdefault(c["ip"], []).append(c)
        suppressed_cids: list[str] = []
        for ip, group in by_ip.items():
            if len(group) <= 1:
                continue
            # Sort highest-priority first
            group_sorted = sorted(group, key=_dedup_rank, reverse=True)
            best = group_sorted[0]
            best_is_streaming = (best.get("status") == "ready"
                                 or bool(best.get("stream_url")))
            for c in group_sorted[1:]:
                # Always retain user-saved or already-ready cards
                if c.get("user_saved") or c.get("status") == "ready":
                    continue
                # If best is a working stream, suppress all
                # needs-credentials and info cards on this IP.
                if best_is_streaming and c.get("status") in (
                        "needs_credentials", "info"):
                    suppressed_cids.append(c["id"])
                    continue
                # Otherwise: keep best, suppress weaker-protocol HTTP
                # siblings on the same IP. Don't suppress siblings of
                # the same protocol family that might represent
                # legitimate multi-stream endpoints (RTSP main + sub).
                best_proto = best.get("protocol", "")
                this_proto = c.get("protocol", "")
                if (this_proto == "HTTP" and best_proto != "HTTP"
                        and c.get("status") in ("needs_credentials", "info")):
                    suppressed_cids.append(c["id"])
                    continue
                # Two HTTP needs-credentials cards on the same IP:
                # suppress the higher-port (lower-rank) one.
                if (this_proto == "HTTP" and best_proto == "HTTP"
                        and c.get("status") == "needs_credentials"):
                    suppressed_cids.append(c["id"])
        # 2.4.0-rc2.6: flush survivors. Suppressed cards drop on the
        # floor (they only ever existed in PENDING_CAMERAS, never
        # made it to the UI). Non-suppressed PENDING cards merge
        # into CAMERAS atomically — UI sees them all appear in one
        # render.
        if PENDING_CAMERAS:
            for cid, cam in PENDING_CAMERAS.items():
                if cid in suppressed_cids:
                    continue
                CAMERAS[cid] = cam
        # Also remove suppressed CAMERAS entries (the user-saved-vs-
        # newly-discovered conflict case).
        for cid in suppressed_cids:
            CAMERAS.pop(cid, None)
        if suppressed_cids:
            log.info(f"Card dedup: suppressed {len(suppressed_cids)} "
                     f"redundant card(s): "
                     f"{', '.join(suppressed_cids[:6])}"
                     f"{'…' if len(suppressed_cids) > 6 else ''}")

        save_cameras()
        ready = sum(1 for c in CAMERAS.values() if c.get("status") == "ready")
        SCAN_STATE.update(
            running=False, progress=100, stage=0, stage_label="",
            message=f"Scan complete — {len(CAMERAS)} device(s), {ready} streaming.",
        )
        log.info(SCAN_STATE["message"])

    except asyncio.CancelledError:
        SCAN_STATE.update(running=False, message="Scan cancelled")
    except Exception as ex:
        log.error(f"run_scan error: {ex}", exc_info=True)
        SCAN_STATE.update(running=False, message=f"Scan error: {ex}")
    finally:
        SCAN_CANCELLED = False
        SCAN_STATE["running"] = False
        # 2.4.0-rc2.6: clear pending buffer regardless of success/failure
        # — if the scan errored mid-flight, any partially-discovered
        # cards in PENDING get dropped on the floor (they wouldn't have
        # been deduped, may be incomplete). Better to lose them than
        # show them. (Note: the `global PENDING_CAMERAS` declaration
        # at scan start covers this assignment too — Python only
        # allows one `global` per name per function, and it must
        # appear BEFORE the name is used. Re-declaring here in the
        # finally block was the rc2.6 install crash bug.)
        PENDING_CAMERAS = None





async def handle_stream(request: web.Request) -> web.StreamResponse:
    """
    GET /stream/{camera_id} — live MJPEG stream via ffmpeg pipe.
    Frames are extracted from the camera's RTSP/MJPEG/HLS source and
    served as multipart/x-mixed-replace. The snap_loop (used for card
    thumbnails) is separate; this endpoint is for the focus/full view.
    """
    camera_id = request.match_info["camera_id"]
    camera    = CAMERAS.get(camera_id)
    if not camera:
        return web.Response(status=404)
    if camera.get("display") in ("webrtc", "wsrtsp", "info"):
        return web.Response(status=400, text="Not proxy-streamable")

    url = build_authenticated_url(camera)
    if not url:
        return web.Response(status=503, text="No stream URL")

    proto = camera.get("protocol", "RTSP")
    flags = (["-rtsp_transport", "tcp"] if proto in ("RTSP", "DVR", "ONVIF")
             else ["-re"] if proto == "HLS" else [])
    # rc1 (Item B1): -fflags +discardcorrupt for H.265+ cameras (see snap_loop
    # for full explanation). Same flag, same persistence, applied here too
    # so the live MJPEG endpoint also benefits.
    if camera.get("needs_fflags_discardcorrupt"):
        flags = flags + ["-fflags", "+discardcorrupt"]

    stream_codec = (camera.get("stream_codec") or "").lower()
    stream_w     = camera.get("stream_width") or 0
    is_hevc      = stream_codec in ("hevc", "h265")

    wanted_hw = ("hevc_v4l2m2m" if is_hevc
                 else "h264_v4l2m2m" if stream_codec == "h264" else "")
    hw_dec    = wanted_hw if (CFG_HW_DECODE and wanted_hw
                               and wanted_hw not in _HW_UNAVAILABLE) else ""
    hw_args     = ["-c:v", hw_dec] if hw_dec else []
    thread_args = ["-threads", "2"] if CFG_LIMIT_THREADS else []

    if is_hevc and stream_w >= 3840:
        vf = "fps=4,scale=640:-2,format=yuvj420p"
    elif is_hevc:
        vf = "fps=8,scale=640:-2,format=yuvj420p"
    else:
        vf = "fps=10,scale=640:-2,format=yuvj420p"

    response = web.StreamResponse(headers={
        "Content-Type":      "multipart/x-mixed-replace; boundary=frame",
        "Cache-Control":     "no-cache",
        "Pragma":            "no-cache",
        "Connection":        "keep-alive",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)

    proc = drain_t = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "warning",
            *flags, *hw_args,
            "-i", url,
            "-an", "-vf", vf, *thread_args,
            "-vcodec", "mjpeg", "-pix_fmt", "yuvj420p",
            "-q:v", "5", "-f", "mpjpeg", "-boundary_tag", "frame",
            "pipe:1",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        drain_t = asyncio.create_task(_drain_stderr(proc, camera_id))
        log.info(f"handle_stream [{camera_id}]: ffmpeg started")

        buf = b""
        SOI, EOI = bytes([0xFF, 0xD8]), bytes([0xFF, 0xD9])
        while True:
            chunk = await asyncio.wait_for(proc.stdout.read(65536), timeout=20)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 4_000_000:
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
                frame = buf[s:e + 2]
                buf   = buf[e + 2:]
                await response.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode()
                    + b"\r\n\r\n" + frame + b"\r\n"
                )

    except (asyncio.TimeoutError, ConnectionResetError, asyncio.CancelledError):
        pass
    except Exception as ex:
        log.debug(f"handle_stream [{camera_id}]: {ex}")
    finally:
        if drain_t:
            drain_t.cancel()
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:
                pass
        if drain_t:
            try:
                await asyncio.wait_for(drain_t, timeout=2)
            except Exception:
                pass

    return response




async def handle_stream_test(request: web.Request) -> web.Response:
    """
    GET /stream/{camera_id}/test — quick stream reachability check via ffprobe.
    Returns JSON with codec info or an error.
    """
    camera_id = request.match_info["camera_id"]
    camera    = CAMERAS.get(camera_id)
    if not camera:
        return web.json_response({"ok": False, "error": "Not found"}, status=404)

    url = build_authenticated_url(camera)
    if not url:
        return web.json_response({"ok": False, "error": "No stream URL"})

    proto = camera.get("protocol", "RTSP")
    extra = ["-rtsp_transport", "tcp"] if proto in ("RTSP", "DVR", "ONVIF") else []
    # 2.4.0-rc3.5 Leak F fix: handle_stream_test fires when the user clicks
    # the Test Stream button. Single ffprobe = single TCP open. Single click
    # is fine, but rapid double-clicks (or clicks while a scan is running on
    # the same IP) could land inside the per-IP cooldown for throttled
    # brands. Cross-sequence tracker handles this — if no recent activity,
    # throttle_wait_if_needed returns immediately; if recent, it waits the
    # right amount. User-perceptible cost: up to throttle_s (~5s for Hipcam)
    # for the affected camera; zero impact otherwise.
    throttle_s = _brand_throttle_seconds(camera)
    if throttle_s > 0:
        await _throttle_wait_if_needed(camera.get("ip", ""),
                                       throttle_s, "stream test ffprobe")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", *extra,
            "-show_entries", "stream=codec_type,codec_name,width,height",
            "-print_format", "json", "-i", url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        if proc.returncode == 0:
            streams = json.loads(out.decode("utf-8", errors="replace")).get("streams", [])
            video   = next((s for s in streams if s.get("codec_type") == "video"), None)
            return web.json_response({
                "ok":      bool(video),
                "streams": len(streams),
                "codec":   video.get("codec_name", "") if video else "",
                "width":   video.get("width") if video else None,
                "height":  video.get("height") if video else None,
            })
    except Exception as ex:
        log.debug(f"handle_stream_test {camera_id}: {ex}")

    return web.json_response({"ok": False, "error": "Probe failed"})



def build_html() -> str:
    js_code = _JS.replace('___BASE___', INGRESS_PATH)
    js_code = js_code.replace('___UNRESTRICTED___',
                               'true' if CFG_UNRESTRICTED_BROWSER else 'false')
    js_code = js_code.replace('___ADAPTIVE_QUALITY___',
                               'true' if CFG_ADAPTIVE_QUALITY else 'false')
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
#cam-grid{{padding:18px;display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--card-w),1fr));gap:16px;align-content:start}}
#empty-state{{grid-column:1/-1;text-align:center;padding:60px 20px;color:var(--text-dim)}}
#empty-state svg{{opacity:.2;display:block;margin:0 auto 14px}}
.camera-card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
              overflow:hidden;display:flex;flex-direction:column;transition:box-shadow .2s}}
.camera-card:hover{{box-shadow:0 4px 20px rgba(0,0,0,.5)}}
.camera-card.uncertain{{border-color:rgba(245,148,74,.4)}}
.feed-wrap{{position:relative;width:100%;aspect-ratio:16/9;background:#000;
            display:flex;align-items:center;justify-content:center;overflow:hidden}}
.feed-wrap img,.feed-wrap video{{width:100%;height:100%;object-fit:cover;display:block}}
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
.card-name{{font-size:.86rem;font-weight:600;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}}
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
.id-section summary{{padding:6px 10px;font-size:.72rem;font-weight:600;color:var(--text-dim);
                     cursor:pointer;list-style:none;user-select:none}}
.id-section summary:hover{{color:var(--text);background:var(--surface2)}}
.id-table{{width:100%;border-collapse:collapse;font-size:.74rem}}
.id-table td{{padding:4px 10px;border-top:1px solid var(--border);vertical-align:top}}
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
.card-actions{{padding:0 12px 10px;display:flex;gap:4px;margin-top:auto;flex-wrap:wrap}}
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
#focus-bar{{position:absolute;bottom:0;left:0;right:0;height:52px;background:rgba(0,0,0,.85);display:flex;align-items:center;justify-content:space-between;padding:0 16px;gap:12px;z-index:9001;border-top:1px solid #333}}
#focus-info{{font-size:.78rem;color:#aaa;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}}
#focus-controls{{display:flex;align-items:flex-end;gap:12px;flex-shrink:0}}
#focus-close{{position:absolute;top:6px;right:14px;background:transparent;border:2.5px solid #e03;color:#e03;font-size:1rem;font-weight:bold;width:32px;height:32px;border-radius:50%;cursor:pointer;z-index:9002;line-height:1;display:flex;align-items:center;justify-content:center}}
#focus-close:hover{{background:#e03;color:#fff}}
.focus-select{{appearance:none;-webkit-appearance:none;background:#1e1e2e url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23aaa'/%3E%3C/svg%3E") no-repeat right 6px center;background-size:9px 5px;border:1px solid #555;color:#e0e0e0;border-radius:6px;padding:3px 22px 3px 6px;font-size:.72rem;cursor:pointer;height:26px;min-width:68px}}
.focus-select:focus{{outline:none;border-color:#4a9eff}}
.focus-auto-btn{{background:#1e1e2e;border:1px solid #555;color:#aaa;border-radius:6px;padding:3px 8px;font-size:.72rem;cursor:pointer;height:26px}}
.focus-auto-btn:hover{{border-color:#4a9eff;color:#4a9eff}}
.focus-ctrl-group{{display:flex;flex-direction:column;align-items:center;gap:3px}}
.focus-ctrl-label{{font-size:.62rem;color:#777;white-space:nowrap;text-align:center;letter-spacing:.02em}}
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
      <p>No cameras found.<br>Click <strong>Scan Network</strong> to discover cameras on your subnet.</p>
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
  <img id="focus-img" alt="Enhanced view">
  <div id="focus-bar">
    <div id="focus-info">Loading…</div>
    <div id="focus-controls">
      <div class="focus-ctrl-group">
        <select id="focus-res-sel" class="focus-select" title="Resolution" onchange="focusPickRes(this.value)">
          <option value="">—</option>
        </select>
        <span class="focus-ctrl-label">Resolution</span>
      </div>
      <div class="focus-ctrl-group">
        <select id="focus-fps-sel" class="focus-select" title="Frame rate cap" onchange="focusPickFps(this.value)">
          <option value="uncapped">Uncapped</option>
          <option value="30">30 FPS</option>
          <option value="20">20 FPS</option>
          <option value="15">15 FPS</option>
          <option value="10">10 FPS</option>
          <option value="5">5 FPS</option>
          <option value="2">2 FPS</option>
          <option value="1">1 FPS</option>
        </select>
        <span class="focus-ctrl-label">Frame Rate</span>
      </div>
      <div class="focus-ctrl-group">
        <button class="focus-auto-btn" onclick="focusResetAuto()" title="Let the system adapt automatically">Auto</button>
        <span class="focus-ctrl-label" style="visibility:hidden">&middot;</span>
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
    app.router.add_get(   "/stream/{camera_id}",                  handle_stream)
    app.router.add_get(   "/stream/{camera_id}/test",             handle_stream_test)
    app.router.add_get(   "/snapshot/{camera_id}",                handle_snapshot)
    app.router.add_get(   "/snap/status",                         handle_snap_status)
    app.router.add_post(  "/api/log_level",                        api_set_log_level)
    app.router.add_get(   "/api/logs",                            api_logs)
    app.router.add_post(  "/snap/focus/{camera_id}",              handle_focus_set)
    app.router.add_delete("/snap/focus",                          handle_focus_clear)
    app.router.add_post(  "/snap/focus/tier",                     handle_focus_set_tier)
    app.router.add_get(   "/snap/focus/profiles",                 handle_focus_profiles)
    app.router.add_post(  "/api/cameras/{camera_id}/motion",      api_motion_toggle)
    app.router.add_get(   "/api/cameras/{camera_id}/motion",      api_motion_status)
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

    Tries to decode a 1-frame black H.264/HEVC stream with each v4l2m2m decoder.
    If ffmpeg exits with error (device not found, not compiled in, etc.), the
    decoder name is added to _HW_UNAVAILABLE so snap_loop never wastes 3 seconds
    trying it.

    Logs:
      HW decoders available: hevc_v4l2m2m, h264_v4l2m2m
      <decoder>: unavailable (<reason>)
    """
    log.info("Probing hardware decoder availability...")
    # 2.4.0-rc3.0: detect rpivid presence ahead of the loop so we can emit
    # the accurate Pi-4-specific diagnostic when hevc_v4l2m2m fails on a
    # system that DOES have the HEVC hardware available (just via the
    # wrong API for our bundled ffmpeg). Two signals: /dev/video19 (the
    # rpivid stateless decoder device created by dtoverlay=rpivid-v4l2)
    # and /dev/media0 (rpivid's media controller). Both being present
    # means rpivid is loaded; if hevc_v4l2m2m then fails, the cause is
    # the ffmpeg-side missing-v4l2-request-support, not a kernel-side
    # missing-device. Fixing this in 2.6.0 by bundling rpi-ffmpeg.
    rpivid_present = (os.path.exists("/dev/video19")
                      and os.path.exists("/dev/media0"))
    available = []
    for dec, codec in _HW_DECODER_CANDIDATES:
        try:
            # Encode a tiny test clip, then try to decode it with the hw decoder
            enc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=black:s=16x16:d=0.1",
                "-c:v", ("libx264" if codec == "h264" else "libx265"),
                "-f", "matroska", "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            encoded, _ = await asyncio.wait_for(enc.communicate(), timeout=10)
            if not encoded:
                _HW_UNAVAILABLE.add(dec)
                log.info(f"  {dec}: unavailable (encode failed)")
                continue

            dec_proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-c:v", dec,
                "-i", "pipe:0",
                "-f", "null", "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                dec_proc.communicate(input=encoded), timeout=10)
            stderr_s = stderr.decode("utf-8", errors="replace")

            if dec_proc.returncode == 0 and "not compiled" not in stderr_s and \
                    "Could not find" not in stderr_s and "Invalid" not in stderr_s:
                available.append(dec)
            else:
                _HW_UNAVAILABLE.add(dec)
                # 2.4.0-rc3.0: more accurate diagnostic when hevc_v4l2m2m
                # fails on a Pi 4 that has rpivid loaded. Previous message
                # ("device not found") was misleading — the device IS
                # there at /dev/video19, but ffmpeg's hevc_v4l2m2m decoder
                # uses the stateful V4L2 m2m API, while rpivid implements
                # the stateless V4L2 request API. Pi 4's bcm2835-codec
                # provides stateful m2m for H264/MPEG/VP8/VP9/VC1 but
                # NOT for HEVC — so hevc_v4l2m2m will literally never
                # find a valid device on a Pi 4, regardless of dtoverlay
                # config. To use rpivid HEVC decode, ffmpeg needs to be
                # built with --enable-v4l2-request and use -hwaccel drm
                # against the stateless API, neither of which the
                # bundled ffmpeg in this addon's Docker image supports
                # today. Planned for 2.6.0: bundle rpi-ffmpeg.
                if dec == "hevc_v4l2m2m" and rpivid_present and \
                        "Could not find" in stderr_s:
                    reason = ("rpivid present at /dev/video19 but bundled "
                              "ffmpeg lacks v4l2-request support — "
                              "stateful m2m API doesn't expose HEVC on "
                              "Pi 4. Will be fixed in 2.6.0 by bundling "
                              "rpi-ffmpeg.")
                else:
                    reason = ("not compiled into ffmpeg"
                              if "not compiled" in stderr_s
                              else ("device not found"
                                    if "Could not find" in stderr_s
                                    else f"rc={dec_proc.returncode}"))
                log.info(f"  {dec}: unavailable ({reason})")
        except asyncio.TimeoutError:
            _HW_UNAVAILABLE.add(dec)
            log.info(f"  {dec}: unavailable (probe timed out)")
        except Exception as e:
            _HW_UNAVAILABLE.add(dec)
            log.info(f"  {dec}: unavailable ({e})")

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
    cancelled_tasks = 0
    for state in _SNAP.values():
        task = state.get("task")
        if task and not task.done():
            task.cancel()
            cancelled_tasks += 1

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
    global _STOP_EVENT

    load_cameras()
    load_blacklist()
    load_feedback()
    load_oui_db()   # Load cached OUI DB synchronously (fast, from disk)

    # Suppress Docker bridge IP entries from the aiohttp access log
    _access_log = logging.getLogger("aiohttp.access")
    _access_log.addFilter(_DockerIPFilter())

    # ── Hardware decoder availability probe ───────────────────────────────────
    # Run once at startup. Checks which hw decoders ffmpeg was compiled with
    # AND which devices are actually accessible (full_access: true exposes all
    # host devices; on non-Pi hardware the v4l2m2m devices simply won't exist).
    # Populates _HW_UNAVAILABLE so snap_loop never tries an unavailable decoder.
    await _probe_hw_decoders()

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


if __name__ == "__main__":
    asyncio.run(main())