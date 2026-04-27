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
from urllib.parse import urlparse

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

CURRENT_VERSION = "2.2.5"  # must match config.yaml

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
        "match": ["microseven", "m7d", "m7b", "m7t"],
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



# IPs/cam-ids the user has explicitly dismissed (loaded from disk)
BLACKLIST: set = set()


def _snap_state(camera_id: str) -> dict:
    """Return (and lazily create) the snapshot state dict for a camera."""
    if camera_id not in _SNAP:
        _SNAP[camera_id] = {
            "frame":             None,
            "frame_time":        0.0,
            "frame_count":       0,
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
    "/", "/stream", "/stream1", "/stream2", "/live", "/live/ch00_0",
    "/live/main", "/h264", "/h264/ch1/main/av_stream", "/video",
    "/video1", "/cam", "/cam/realmonitor?channel=1&subtype=0",
    "/Streaming/Channels/101", "/Streaming/Channels/1",
    "/av0_0", "/av0_1", "/11", "/12", "/MediaInput/h264",
    "/ch0_unicast.sdp", "/onvif1", "/profile1/media.smp",
    "/channel1", "/mpeg4/media.amp",
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
    "printer", "print server", "jetdirect", "brother", "epson", "canon printer",
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
        "notes": "Hikvision IP camera or NVR/DVR",
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
        "notes": "Dahua Technology IP camera, NVR/DVR or IMOU device",
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
        "notes": "Lorex (FLIR) NVR/DVR or IP camera",
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
        "notes": "Reolink IP camera or NVR",
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
        "notes": "Axis Communications IP camera or encoder",
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
        "notes": "Hanwha Vision (formerly Samsung Techwin) — Wisenet series",
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
        "notes": "Amcrest IP camera or NVR (Dahua-based OEM)",
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
        "notes": "Uniview (UNV) IP camera or NVR",
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
        "notes": "Vivotek IP camera or NVR",
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
        "notes": "Foscam IP camera",
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
        "notes": "Annke IP camera or NVR/DVR (Hikvision-based OEM)",
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
        "notes": "Swann security camera or NVR/DVR",
    },
    {
        "name": "TP-Link Tapo / Kasa",
        "aliases": ["tapo", "kasa", "tp-link"],
        "http_titles": ["tapo", "kasa", "tp-link"],
        "http_body":   ["tapo", "tp-link tapo", "kasa camera"],
        "http_headers":["tp-link", "tapo"],
        "nmap_products":["tp-link", "tapo"],
        "onvif_scopes": ["tapo", "tp-link"],
        "default_ports": [554, 80, 2020],
        "notes": "TP-Link Tapo / Kasa smart camera",
    },    {
        "name": "Night Owl",
        "aliases": ["night owl", "nightowl"],
        "http_titles": ["night owl"],
        "http_body":   ["night owl", "nightowl security"],
        "http_headers":["night owl"],
        "nmap_products":["night owl"],
        "onvif_scopes": ["nightowl"],
        "default_ports": [554, 80, 34567],
        "notes": "Night Owl security camera or NVR/DVR",
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
        "notes": "Google Nest / Dropcam IP camera",
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
        "notes": "Ring doorbell or security camera (Amazon)",
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
        "notes": "Wyze IP camera",
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
        "notes": "Eufy (Anker) IP camera",
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
        "notes": "Arlo wireless IP camera",
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
        "notes": "Verkada cloud-managed IP camera",
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
        "notes": "Q-See consumer DVR/NVR or IP camera",
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
        "notes": "LaView IP camera or NVR",
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
        "notes": "Zosi budget security camera or NVR/DVR",
    },
    {
        "name": "Sricam / Srihome",
        "aliases": ["sricam", "srihome"],
        "http_titles": ["sricam", "srihome"],
        "http_body":   ["sricam", "srihome", "sricam.com"],
        "http_headers":["sricam", "srihome"],
        "nmap_products":["sricam", "srihome"],
        "onvif_scopes": ["sricam", "srihome"],
        "default_ports": [554, 80, 8080],
        "notes": "Sricam / Srihome budget IP camera",
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
        "notes": "Vstarcam budget WiFi IP camera",
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
        "notes": "Wansview budget IP camera",
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
        "notes": "Tenvis IP camera",
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
]

# Build fast lookup structures from the DB
_DB_MANUFACTURERS: set[str] = set()   # all lowercase match strings for is_camera_positive
_DB_ENTRIES_BY_KEY: dict[str, dict] = {}  # pattern → db_entry for identify_manufacturer

for _entry in CAMERA_DB:
    for _field in ("http_titles", "http_body", "http_headers",
                   "nmap_products", "onvif_scopes", "aliases"):
        for _kw in _entry.get(_field, []):
            _k = _kw.lower()
            _DB_MANUFACTURERS.add(_k)
            _DB_ENTRIES_BY_KEY[_k] = _entry


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
    Best match = entry with the most keyword hits.
    Short keywords (< 6 chars) require word-boundary matching to avoid false
    positives (e.g. 'acti' matching 'interactive' on HP printer pages).
    """
    text_l = text.lower()
    scores: dict[str, int] = {}
    for kw, entry in _DB_ENTRIES_BY_KEY.items():
        if _kw_matches(kw, text_l):
            name = entry["name"]
            scores[name] = scores.get(name, 0) + 1
    if not scores:
        return None
    best_name = max(scores, key=lambda n: scores[n])
    for entry in CAMERA_DB:
        if entry["name"] == best_name:
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

    ports = [p["port"] for p in nmap_info.get("open_ports", [])]
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
                results.append({"ip": ip, "name": name,
                                 "xaddrs": xaddrs[0].strip() if xaddrs else ""})
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

def focused_nmap_scan(host_list: list[str]) -> list[dict]:
    """
    Scan only known-live hosts on the top 1000 most common ports.
    Using nmap --top-ports 1000 covers all standard camera ports plus
    thousands of other well-known ports, catching cameras on non-standard
    ports and identifying devices by service banner even without a camera
    protocol.  Because hosts are pre-confirmed alive via ARP, no timeout
    waste on dead IPs.
    Typical time: 30-90 seconds for 20 hosts.
    """
    if not host_list:
        return []
    log.info(f"Focused scan: {len(host_list)} host(s), top 1000 ports")
    try:
        r = subprocess.run(
            ["nmap", "-sV", "--open", "--top-ports", "1000",
             "--host-timeout", "30s", "-T4", "-oX", "-"] + host_list,
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
    Verify an RTSP stream is accessible.  Returns True if DESCRIBE
    succeeds (with or without auth).  Never touches ffprobe.
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

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)

        CRLF = chr(13) + chr(10)
        CRLFCRLF = (chr(13) + chr(10)) * 2

        def roundtrip(method: str, cseq: int, extra: dict | None = None) -> str:
            extra = extra or {}  # safe: new dict each call, never mutates caller's default
            hdr = "".join(k + ": " + v + CRLF for k, v in extra.items())
            req = method + " " + rtsp_url + " RTSP/1.0" + CRLF
            req += "CSeq: " + str(cseq) + CRLF + hdr + CRLF
            sock.sendall(req.encode())
            buf = b""
            while CRLFCRLF.encode() not in buf and len(buf) < 32768:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            return buf.decode("utf-8", errors="replace")

        resp = roundtrip("OPTIONS", 1)
        status_line = resp.split(chr(13))[0].strip()
        if "RTSP/1.0 2" not in resp:
            _log(f"OPTIONS → {status_line!r} (not 2xx — giving up)")
            return False
        _log(f"OPTIONS → OK")

        # DESCRIBE — may trigger 401
        resp = roundtrip("DESCRIBE", 2, {"Accept": "application/sdp"})
        status_line = resp.split(chr(13))[0].strip()
        if "RTSP/1.0 200" in resp:
            _log("DESCRIBE → 200 OK (no auth required)")
            return True
        if "401" not in resp or not username:
            ok = "RTSP/1.0 2" in resp
            _log(f"DESCRIBE → {status_line!r} (no 401; result={ok})")
            return ok

        # Parse WWW-Authenticate
        auth_line = next(
            (l for l in resp.splitlines() if l.lower().startswith("www-authenticate:")), "")
        auth_val  = auth_line.split(":", 1)[-1].strip()

        if auth_val.lower().startswith("digest"):
            realm_m = re.search(r'realm="([^"]*)"', auth_val)
            nonce_m = re.search(r'nonce="([^"]*)"', auth_val)
            if not (realm_m and nonce_m):
                _log(f"DESCRIBE → 401 Digest but no realm/nonce in: {auth_val[:80]!r}")
                return False
            realm, nonce = realm_m.group(1), nonce_m.group(1)
            _log(f"DESCRIBE → 401 Digest (realm={realm!r}, nonce={nonce[:8]!r}...)")
            ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
            ha2 = hashlib.md5(f"DESCRIBE:{rtsp_url}".encode()).hexdigest()
            rsp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            auth = (f'Digest username="{username}", realm="{realm}", '
                    f'nonce="{nonce}", uri="{rtsp_url}", response="{rsp}"')
        elif auth_val.lower().startswith("basic"):
            import base64 as _b64
            _log("DESCRIBE → 401 Basic")
            auth = "Basic " + _b64.b64encode(f"{username}:{password}".encode()).decode()
        else:
            _log(f"DESCRIBE → 401 unknown auth method: {auth_val[:60]!r} — giving up")
            return False

        resp = roundtrip("DESCRIBE", 3,
                         {"Accept": "application/sdp", "Authorization": auth})
        ok = "RTSP/1.0 200" in resp
        status_line = resp.split(chr(13))[0].strip()
        _log(f"DESCRIBE (authenticated) → {'200 OK' if ok else status_line!r}")
        return ok

    except Exception as e:
        _log(f"exception: {e}")
        return False
    finally:
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


def find_rtsp_path(ip: str, port: int,
                   username: str = "", password: str = "") -> str | None:
    for path in RTSP_PATHS:
        url = f"rtsp://{ip}:{port}{path}"
        if probe_rtsp(url, username, password):
            log.info(f"  RTSP OK: {url}")
            return url
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
                if "101" in buf.decode("utf-8", errors="replace") and \
                        b"websocket" in buf.lower():
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


def probe_http_identity(ip: str, port: int, timeout: int = 5) -> dict:
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
            log.info(f"  HTTP identity: {ip}:{port}{url} → {entry['name']}")
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

    # When Adaptive Quality is disabled: single tier — top profile, uncapped.
    # The user gets maximum quality with no stepping at all.
    if not CFG_ADAPTIVE_QUALITY:
        return [(0, None)]

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
    # EXCEPTION: when native_res=True (enhanced view) AND RTSP is available,
    # bypass http_snap_loop so the ffmpeg pipeline runs — this makes the
    # Resolution/FPS controls work and gives real video instead of 1fps polling.
    _has_rtsp = bool(camera.get("stream_url"))
    if camera.get("http_snap_url") and not (native_res and _has_rtsp):
        await http_snap_loop(camera_id, camera)
        return

    state        = _snap_state(camera_id)
    stream_codec = camera.get("stream_codec", "").lower()
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
            # Use the profile's URL directly (already has credentials stripped;
            # build_authenticated_url re-adds them from camera["credentials"]).
            prof_url_key = prof.get("_url_key", "stream_url")
            tier_url     = build_authenticated_url(cam_now, url_key=prof_url_key)
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

        return await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "warning",
            *transport_args,
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

            state["restart_count"] += 1

            # Exponential backoff when ffmpeg keeps dying with 0 frames
            # (e.g. wrong codec, bad URL, camera rejecting connection).
            # 0-frame failures: 2s, 4s, 8s, 16s, 32s (cap at 32s).
            # Normal failures (got some frames): always 2s.
            if frames == 0:
                streak = state.get("zero_frame_streak", 0) + 1
                state["zero_frame_streak"] = streak
                backoff = min(2 ** min(streak - 1, 4), 32)

                # After 3 consecutive 0-frame failures, try flipping the
                # RTSP transport.  Many cheap/generic ONVIF cameras (Sricam,
                # Microseven, etc.) accept the TCP SETUP but reply with UDP —
                # ffmpeg calls this "Nonmatching transport in server reply"
                # which surfaces as "Invalid data found when processing input".
                # Flipping to UDP fixes this class of camera entirely.
                # We try TCP→UDP first; if UDP also fails 3 more times we
                # flip back to TCP (so the backoff loop still applies).
                if streak == 3 and not native_res:
                    cam_now = CAMERAS.get(camera_id, {})
                    cur_transport = cam_now.get("preferred_transport", "tcp")
                    if cur_transport == "tcp":
                        log.warning(f"SNAP [{camera_id}]: 3 consecutive 0-frame failures "
                                    f"with TCP — switching to UDP transport (camera may "
                                    f"not support TCP RTSP)")
                        CAMERAS[camera_id]["preferred_transport"] = "udp"
                        state["zero_frame_streak"] = 0   # fresh count for UDP
                    elif cur_transport == "udp":
                        log.warning(f"SNAP [{camera_id}]: 3 consecutive 0-frame failures "
                                    f"with UDP also — reverting to TCP")
                        CAMERAS[camera_id]["preferred_transport"] = "tcp"
                        state["zero_frame_streak"] = 0

                # After 3 consecutive 0-frame failures in native_res (enhanced
                # view) mode, fall back to http_snap_loop if the camera has one.
                # This handles cameras like the Microseven whose RTSP is
                # stored but non-functional — ffmpeg keeps crashing, wasting CPU.
                if streak == 3 and native_res:
                    cam_now = CAMERAS.get(camera_id, camera)
                    if cam_now.get("http_snap_url"):
                        log.warning(
                            f"SNAP [{camera_id}]: 3 consecutive 0-frame failures in "
                            f"enhanced view — RTSP non-functional, falling back to "
                            f"HTTP snap loop for this focus session"
                        )
                        await http_snap_loop(camera_id, cam_now)
                        return

                # After 5 consecutive 0-frame failures on the main stream,
                # it may be a codec mismatch (e.g. ONVIF says h264, camera sends hevc).
                # Clear the stored codec so snap_loop lets ffmpeg auto-detect next restart.
                if streak == 5 and not native_res:
                    cam_now = CAMERAS.get(camera_id, {})
                    if cam_now.get("stream_codec"):
                        log.warning(f"SNAP [{camera_id}]: 5 consecutive 0-frame failures — "
                                    f"clearing stored codec {cam_now['stream_codec']!r} "
                                    f"so ffmpeg can auto-detect on next attempt")
                        CAMERAS[camera_id]["stream_codec"] = None
                        profs = CAMERAS[camera_id].get("stream_profiles") or []
                        if profs:
                            profs[0]["stream_codec"] = None
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

                # frames == 0 is always a failure regardless of run duration
                # (e.g. 30-second ffmpeg read timeout with zero frames decoded).
                # Previously this was counted as "stable" because run_dur >= 8s.
                # Skip adaptive stepping entirely if the user manually pinned the tier
                if ada.get("manual_override"):
                    fast_death       = False
                    restart_overflow = False
                else:
                    # frames == 0 is always a failure regardless of run duration
                    # (e.g. 30-second ffmpeg read timeout with zero frames decoded).
                    # Previously this was counted as "stable" because run_dur >= 8s.
                    fast_death = (frames == 0 or
                                  (run_dur < _ADAPTIVE_UNSTABLE_S and
                                   frames  < _ADAPTIVE_UNSTABLE_FR))

                # Count restarts at locked tier
                if locked:
                    ada["restarts_since_lock"] = ada.get("restarts_since_lock", 0) + 1
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
        state["proc"] = None
        # Only clear state["task"] if it still points to this task.
        # If handle_snapshot already started a new card-view loop while this
        # focus task was winding down, state["task"] now points to that newer
        # loop — clearing it unconditionally would cause handle_snapshot to
        # start yet another loop (the root cause of the duplicate-loop bug).
        if state.get("task") is asyncio.current_task():
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
            step_res = "?"
            step_fps = "?"
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
                                         "X-Frame-Count": str(state.get("frame_count", 0)),
                                         "X-Step-Res":    step_res,
                                         "X-Step-FPS":    step_fps})
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
    ada["tier_idx"]            = best_idx
    ada["locked"]              = True
    ada["restarts_since_lock"] = 0
    ada["manual_override"]     = True
    ada["ladder"]              = ladder
    log.info(f"Focus [{camera_id}]: manual tier [{best_idx}] "
             f"profile[{prof_idx}] fps={fps_val}")
    return web.json_response({"status": "ok", "tier_idx": best_idx,
                              "profile_idx": prof_idx, "fps": fps_val})


async def handle_focus_profiles(request: web.Request) -> web.Response:
    """GET /snap/focus/profiles — return the stream profiles for the focused
    camera, so the JS can populate the resolution dropdown."""
    camera_id = _FOCUSED_CAMERA
    if not camera_id:
        return web.json_response([])
    camera = CAMERAS.get(camera_id)
    if not camera:
        return web.json_response([])
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
    return web.json_response(result)


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
    s["has_credentials"]  = bool(s.get("credentials"))
    s["upgrade_missing"]  = bool(s.get("upgrade_missing"))
    s["has_sub_stream"]   = bool(s.get("sub_stream_url"))
    # Ensure identity fields always present
    for f in ("manufacturer", "device_notes", "page_title", "server_header",
              "mac_addr", "mac_vendor"):
        s.setdefault(f, "")
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
    """Return the best-matching STREAM_DB entry for a camera, or None."""
    haystack = " ".join([
        camera.get("name", ""),
        camera.get("vendor", ""),
        camera.get("model", ""),
        camera.get("verdict_reason", ""),
        camera.get("hostname", ""),
    ]).lower()
    best_slug, best_len = None, 0
    for slug, entry in STREAM_DB.items():
        for kw in entry["match"]:
            if kw in haystack and len(kw) > best_len:
                best_slug, best_len = slug, len(kw)
    return STREAM_DB[best_slug] if best_slug else None


def _match_stream_db_slug(camera: dict) -> str | None:
    """Return the STREAM_DB slug that matched, or None."""
    haystack = " ".join([
        camera.get("name", ""),
        camera.get("vendor", ""),
        camera.get("model", ""),
        camera.get("verdict_reason", ""),
        camera.get("hostname", ""),
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
    """
    loop = asyncio.get_event_loop()
    results = []
    rtsp_port = db_entry.get("port", 554)

    cred_pfx = ""
    if creds:
        try:
            from urllib.parse import quote as _q
            u, pw = decrypt_creds(creds)
            _SAFE = "!$&'()*+,;=~-._"
            cred_pfx = f"{_q(u, safe=_SAFE)}:{_q(pw, safe=_SAFE)}@"
        except Exception:
            pass

    for path in db_entry.get("rtsp", []):
        url = f"rtsp://{cred_pfx}{ip}:{rtsp_port}{path}"
        bare = f"rtsp://{ip}:{rtsp_port}{path}"
        if bare in existing_urls or url in existing_urls:
            continue
        try:
            ok = await loop.run_in_executor(
                _THREAD_POOL, probe_rtsp, url, "", "", 4, f"db_probe:{ip}{path}")
            if ok:
                det = await probe_stream_details(url, "RTSP")
                results.append({"url": url, **det})
        except Exception:
            pass
    return results


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
        profiles  = await loop.run_in_executor(
            _THREAD_POOL, onvif_get_profiles, media_url, username, password)
        log.info(f"  ONVIF profiles found: {len(profiles)} — {[p['name'] for p in profiles]}")
        if profiles:
            enc_creds = encrypt_creds(username, password)
            # ── Collect all streams from ONVIF profiles ───────────────────────
            stream_candidates = []   # list of {url, width, height, codec, token, name}
            for prof in profiles:
                stream_url = await loop.run_in_executor(
                    _THREAD_POOL, onvif_get_stream_uri, media_url, prof["token"], username, password)
                log.info(f"  Profile '{prof['name']}' stream_url: {stream_url}")
                if not stream_url:
                    continue
                ok = await loop.run_in_executor(
                    _THREAD_POOL, probe_rtsp, stream_url, username, password,
                    6, f"{camera_id}/{prof['name']}")
                log.info(f"  probe_rtsp OK: {ok}")
                if not ok:
                    log.warning(f"  probe_rtsp returned False for '{prof['name']}' "
                                f"— including anyway (ONVIF confirmed creds)")
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

            if db_entry:
                existing = {c["url"] for c in stream_candidates}
                db_streams = await _probe_db_streams(ip, port, enc_creds,
                                                     db_entry, existing)
                if db_streams:
                    log.info(f"  DB probe found {len(db_streams)} extra stream(s)")
                    for s in db_streams:
                        stream_candidates.append({**s, "token": "db_probe",
                                                  "name": "DB stream"})

            if stream_candidates:
                # ── Rank by resolution: highest first, lowest last ─────────────
                def _res(c) -> int:

                    return (c.get("stream_width") or 0) * (c.get("stream_height") or 0)
                stream_candidates.sort(key=_res, reverse=True)
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
                    **extra_urls,
                    **main_details,
                }
                CAMERAS.pop(camera_id, None)
                save_cameras()
                cam = CAMERAS.get(cid)
                if cam:
                    # Run ffprobe to detect the real codec — ONVIF often reports
                    # "h264" when the camera actually streams HEVC.
                    cam_url = build_authenticated_url(cam) or ""
                    async def _fix_codec(cam_id=cid, auth_url=cam_url) -> None:

                        await asyncio.sleep(1.0)
                        det = await probe_stream_details(auth_url, "RTSP")
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
                    asyncio.create_task(_fix_codec())

                return web.json_response({"status": "ok", "channels": 1})
            log.warning(f"  ONVIF: profiles found but no streams resolved — falling back to direct RTSP")
        else:
            log.warning(f"  ONVIF: no profiles returned (auth failed or device unreachable)")

    # RTSP direct — for ONVIF cards always try port 554 in addition to stored port
    if proto in ("RTSP", "DVR", "ONVIF"):
        # Try stored port first
        log.info(f"  Trying direct RTSP on {ip}:{port}")
        url = await loop.run_in_executor(
            _THREAD_POOL, find_rtsp_path, ip, port, username, password)
        # For ONVIF cards the stored port is often 80; always also try 554
        if not url and port != 554:
            log.info(f"  Trying direct RTSP on {ip}:554 (standard RTSP port)")
            url = await loop.run_in_executor(
                _THREAD_POOL, find_rtsp_path, ip, 554, username, password)
        if not url and camera.get("xaddrs"):
            parsed = urlparse(camera["xaddrs"])
            rtsp_port = parsed.port or 554
            log.info(f"  Trying RTSP via xaddrs {ip}:{rtsp_port}")
            url = await loop.run_in_executor(
                _THREAD_POOL, find_rtsp_path, parsed.hostname or ip,
                rtsp_port, username, password)
        log.info(f"  RTSP result: {_strip_creds(url) if url else 'None'}")
    elif proto == "MJPEG":
        url = await loop.run_in_executor(
            _THREAD_POOL, probe_mjpeg_http, ip, port, username, password)
    elif proto == "HLS":
        url = await loop.run_in_executor(
            _THREAD_POOL, probe_hls, ip, port, username, password)

    if not url:
        log.warning(f"Credential attempt FAILED for {camera_id} — no working stream found")
        return web.json_response({"error": "Could not connect with those credentials."}, status=401)

    details = await probe_stream_details(url, proto)
    enc_creds = encrypt_creds(username, password)

    # ── Silent DB probe for additional streams on non-ONVIF cameras ──────────
    sub_url = None
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
        if db_streams:
            # Rank with main stream, pick lowest-res as sub
            all_s = [{"url": url, **details}] + db_streams
            def _res2(c) -> int:

                return (c.get("stream_width") or 0) * (c.get("stream_height") or 0)
            all_s.sort(key=_res2, reverse=True)
            url     = all_s[0]["url"]
            details = {k: v for k, v in all_s[0].items() if k != "url"}
            sub_url = all_s[-1]["url"] if len(all_s) > 1 else None
            if sub_url:
                log.info(f"  DB probe found sub stream: {_strip_creds(sub_url)}")

    camera.update(credentials=enc_creds,
                  stream_url=url, sub_stream_url=sub_url,
                  requires_credentials=False,
                  status="ready", user_saved=True,
                  http_snap_url=http_snap_url,
                  http_snap_auth_mode=http_snap_auth_mode,
                  **details)
    save_cameras()
    log.info(f"Credentials accepted for {camera_id}: {_strip_creds(url)}")
    return web.json_response({"status": "ok", "stream_url": _strip_creds(url), **details})


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

function stopAllSnaps() {
  Object.keys(_snapTimers).forEach(stopSnap);
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

function storNavForward() {
  if (_storHistIdx >= _storHistory.length - 1) return;
  _storHistIdx++;
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
let _focusWarnOK  = {};   // camId → bool (user acknowledged warning this session)

function _estimateCpuPct(cam) {
  // Rough heuristic: hevc cost based on pixels × fps relative to Pi 4 capacity
  const codec = (cam.stream_codec || '').toLowerCase();
  const w     = cam.stream_width  || 1280;
  const h     = cam.stream_height || 720;
  const fps   = cam.stream_fps    || 10;
  const isH   = codec === 'hevc' || codec === 'h265';
  // Pi 4 baseline: hevc 1920×1080×30 ≈ 50% of 4 cores = 200% cpu equiv
  const basePx = 1920 * 1080 * 30;
  const thisPx = w * h * fps * (isH ? 2.5 : 1.0);   // hevc costs ~2.5× h264
  return Math.round((thisPx / basePx) * 50);
}

async function openFocus(camId) {
  const cam = cameras.find(c => c.id === camId);
  if (!cam || cam.status !== 'ready') return;

  // CPU warning if this stream will likely overload the Pi
  const pct = _estimateCpuPct(cam);
  if (pct > 80 && !_focusWarnOK[camId]) {
    const fps = cam.stream_fps || '?';
    const res = (cam.stream_width || '?') + 'x' + (cam.stream_height || '?');
    const warnEl = document.getElementById('focus-warning');
    document.getElementById('focus-warn-text').textContent =
      'This view runs at maximum quality: ' + fps + ' fps @ ' + res
      + '. Estimated CPU load: ~' + pct + '%. This may overload your system.';
    // Show overlay first, then show warning
    document.getElementById('focus-overlay').style.display = 'flex';
    warnEl.style.display = 'flex';
    // OK button handler
    const okBtn = warnEl.querySelector('button');
    okBtn.onclick = () => {
      _focusWarnOK[camId] = true;
      warnEl.style.display = 'none';
      _startFocusPoll(camId, cam);
    };
    return;
  }

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
  const name   = cam.name || cam.ip;

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

  // Info bar format:
  //   Name — Actual Feed: WxH · X fps  [Adapted Quality: WxH · fps]
  // "Actual Feed"     = measured from actual received JPEG + frame count.
  // "Adapted Quality" = current adaptive ladder tier from server headers.
  //                     Only shown when adaptive_quality config is on OR
  //                     the user has manually picked a resolution/fps tier.
  let _manualTierActive = false;
  function _updateInfoBar() {
    const realRes  = _liveRes  || '…';
    const realFps  = _liveFps  !== null ? _liveFps + ' fps' : 'measuring…';
    const stepRes  = _stepRes  || '…';
    const stepFpsS = _stepFps  !== null
      ? (_stepFps === 'uncapped' ? 'uncapped' : _stepFps + ' fps')
      : '…';
    const showAdapted = _manualTierActive
      || (typeof CFG_ADAPTIVE_QUALITY !== 'undefined' && CFG_ADAPTIVE_QUALITY);
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
        if (stepRes) _stepRes = stepRes;
        if (stepFps) _stepFps = stepFps;
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
let _focusProfiles = [];   // [{idx, label, width, height, codec}]
let _focusCurProf  = 0;    // currently selected profile index

async function _loadFocusProfiles() {
  try {
    const r = await fetch(BASE + '/snap/focus/profiles');
    if (!r.ok) return;
    const profiles = await r.json();
    if (!profiles || profiles.length === 0) return;  // keep placeholder if empty
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
    // Default to first (highest-res) profile
    sel.value = '0';
    _focusCurProf = 0;
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
  }, 2500);
}



/* ── Camera grid ───────────────────────────────────────────────────────────── */
function renderGrid() {
  const grid  = document.getElementById('cam-grid');
  const empty = document.getElementById('empty-state');
  const count = document.getElementById('cam-count');
  count.textContent = '';   // device count shown in scan status bar — not duplicated here
  empty.style.display = cameras.length ? 'none' : '';

  const existingIds = new Set([...grid.querySelectorAll('.camera-card')].map(c => c.dataset.id));
  const newIds      = new Set(cameras.map(c => c.id));
  existingIds.forEach(id => { if (!newIds.has(id)) grid.querySelector('[data-id="' + id + '"]')?.remove(); });

  cameras.forEach(cam => {
    if (existingIds.has(cam.id)) updateCard(cam);
    else                         grid.appendChild(buildCard(cam));
  });
  grid.querySelectorAll('video[data-hls]').forEach(v => { if (!v._hls) initHls(v); });
  initSnaps();   // start polling for any newly added data-snap images
}

function buildCard(cam) {
  const d = document.createElement('div');
  d.className = 'camera-card' + (cam.verdict === 'uncertain' || cam.verdict === 'not_camera' ? ' uncertain' : '');
  d.dataset.id = cam.id;
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
  return 'dot-error';
}

function protoBadge(proto) {
  const [bg, fg] = PROTO_CLR[proto] || ['2a2a2a', 'aaaaaa'];
  return '<span class="badge" style="background:#' + bg + ';color:#' + fg + '">'
       + (PROTO_ICONS[proto] || '') + ' ' + proto + '</span>';
}

/* onerror helper — avoids embedding quotes in the generated HTML string */
function imgError(img) {
  img.style.display = 'none';
  if (img.nextElementSibling) img.nextElementSibling.style.display = 'flex';
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
  return testBtn + clearBtn + notCamBtn + recBtn + webBtn
       + '<button class="btn btn-danger btn-sm" onclick="deleteCamera(\'' + cam.id + '\')">Remove</button>';
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

function cardHTML(cam) {
  const name     = esc(cam.name || cam.hostname || cam.ip);
  const onvifBdg = cam.onvif
    ? '<span class="badge" style="background:#2d1e4a;color:#c09eff">ONVIF</span>' : '';
  const credBdg  = cam.has_credentials
    ? '<span class="badge" style="background:#1e2d1e;color:#6fcf97">🔐</span>' : '';
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

  return '<div class="feed-wrap">' + feedHTML(cam) + '</div>'
    + '<div class="card-info">'
    + '<div class="status-dot ' + dotClass(cam) + '"></div>'
    + '<span class="card-name" title="' + name + '"'
    + ' onclick="openRename(\'' + cam.id + '\',\'' + name.replace(/'/g, "\\'") + '\')">'
    + name + '</span></div>'
    + '<div class="badges">' + protoBadge(cam.protocol)
    + '<span class="badge" style="background:#1e2d1e;color:#6fcf97">:' + cam.port + '</span>'
    + '<span class="badge" style="background:#2d2020;color:#e88">' + cam.ip + '</span>'
    + onvifBdg + credBdg + uncBdg + upgradeBdg + hevcPlusBdg + '</div>'
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
  e.textContent = 'Verifying…'; e.classList.add('visible');
  try {
    const r = await fetch(BASE + '/api/credentials', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({camera_id: cid, username: u, password: p})
    });
    const d = await r.json();
    if (r.ok) { e.classList.remove('visible'); await loadCameras(); }
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

def build_authenticated_url(camera: dict, url_key: str = "stream_url") -> str | None:
    """Return stream URL with credentials embedded, or None if no URL.

    Credentials are percent-encoded per RFC 3986 §3.2.1 so that special
    characters in passwords (e.g. '!' '?' '@' '#' '%') don't corrupt the URL.
    The safe set matches characters that RTSP/HTTP stacks accept raw in the
    userinfo component without confusion.
    """
    url = camera.get(url_key) or camera.get("stream_url")
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
        if cam_id in CAMERAS and not CAMERAS[cam_id].get("hevc_plus_warning"):
            CAMERAS[cam_id]["hevc_plus_warning"] = True
            log.warning(f"Camera {cam_id}: H.265+ (Hikvision proprietary) detected — "
                        f"fix: camera UI → Video → Encoding → change H.265+ to H.265")




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
                            verdict: str, reason: str, loop) -> dict | None:
    """
    Probe a single host:port and return a camera dict if a stream is found,
    or None if nothing reachable. Uses saved credentials from prev if available.
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

    def base(proto: str, url: str, status: str, display: str = "proxy") -> dict:
        return {
            "id": cid, "ip": ip, "hostname": hostname, "port": port,
            "protocol": proto, "stream_url": url,
            "requires_credentials": False, "credentials": None,
            "name": prev_name, "status": status,
            "user_saved": bool(prev), "display": display,
            "verdict": verdict, "verdict_reason": reason,
        }

    if initial_protocol in ("RTSP", "DVR"):
        url = await loop.run_in_executor(_THREAD_POOL, find_rtsp_path, ip, port, "", "")
        if url:
            return base("RTSP", url, "ready")
        if saved_u:
            url = await loop.run_in_executor(_THREAD_POOL, find_rtsp_path, ip, port, saved_u, saved_p)
            if url:
                cam = base("RTSP", url, "ready")
                cam["credentials"] = prev_creds
                return cam
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
            return base("MJPEG", url, "ready", "mjpeg")
        if saved_u:
            url = await loop.run_in_executor(_THREAD_POOL, probe_mjpeg_http, ip, port, saved_u, saved_p)
            if url:
                cam = base("MJPEG", url, "ready", "mjpeg")
                cam["credentials"] = prev_creds
                return cam

        url = await loop.run_in_executor(_THREAD_POOL, probe_hls, ip, port, "", "")
        if url:
            return base("HLS", url, "ready", "hls")
        if saved_u:
            url = await loop.run_in_executor(_THREAD_POOL, probe_hls, ip, port, saved_u, saved_p)
            if url:
                cam = base("HLS", url, "ready", "hls")
                cam["credentials"] = prev_creds
                return cam

        url = await loop.run_in_executor(_THREAD_POOL, find_rtsp_path, ip, port, "", "")
        if url:
            return base("RTSP", url, "ready")

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

        if verdict in ("camera", "uncertain"):
            cam = base("HTTP", "", "needs_credentials")
            cam["requires_credentials"] = True
            return cam

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
            for port_info in host.get("open_ports", []):
                port = port_info["port"]
                cid  = f"{ip}_{port}"
                if cid in BLACKLIST:
                    continue
                initial = _initial_protocol(port, port_info.get("service", ""),
                                             port_info.get("product", ""))
                prev = saved.get(cid, {})
                cam  = await _probe_host_port(ip, port, hostname, initial,
                                              prev, verdict, reason, loop)
                if cam:
                    CAMERAS[cam["id"]] = cam

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
                    initial = _initial_protocol(port, port_info.get("service", ""),
                                                 port_info.get("product", ""))
                    prev = saved.get(cid, {})
                    cam  = await _probe_host_port(ip, port, hostname, initial,
                                                  prev, verdict, reason, loop)
                    if cam:
                        CAMERAS[cam["id"]] = cam

        # Merge multicast-only ONVIF cameras not found by nmap
        for onvif in onvif_results:
            ip = onvif["ip"]
            if ip == gateway or ip in BLACKLIST:
                continue
            existing = [c for c in CAMERAS.values() if c["ip"] == ip]
            if existing:
                for cam in existing:
                    cam["onvif"]  = True
                    cam["xaddrs"] = onvif.get("xaddrs", cam.get("xaddrs", ""))
            else:
                cid  = f"{ip}_onvif"
                prev = saved.get(cid, {})
                CAMERAS[cid] = {
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
                }

        # Merge multicast-only SSDP cameras
        for ssdp in ssdp_results:
            if not ssdp.get("is_camera"):
                continue
            ip = ssdp["ip"]
            if ip == gateway or ip in BLACKLIST:
                continue
            if not any(c["ip"] == ip for c in CAMERAS.values()):
                cid  = f"{ip}_ssdp"
                prev = saved.get(cid, {})
                CAMERAS[cid] = {
                    "id": cid, "ip": ip, "hostname": ssdp.get("name", ip),
                    "port": 80, "protocol": "HTTP",
                    "stream_url": "", "requires_credentials": True,
                    "credentials": prev.get("credentials"),
                    "name": prev.get("name", ssdp.get("name", ip)),
                    "status": "needs_credentials",
                    "user_saved": bool(prev), "display": "proxy",
                    "verdict": "camera", "verdict_reason": "SSDP/UPnP discovered",
                }

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
.nc-share-note{{font-size:.7rem;color:var(--text-dim);margin-top:5px;line-height:1.4}}"""

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
  <h1 onclick="switchView('cameras')" title="System Stability — See Logs" style="cursor:pointer">
    <svg id="status-cam-icon" width="27" height="27" viewBox="0 0 24 24"
         fill="none" stroke="#43a047" stroke-width="2"
         style="cursor:pointer;flex-shrink:0;vertical-align:middle"
         title="System Stability — click to view logs"
         onclick="openHALog();event.stopPropagation()">
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
      <button class="focus-auto-btn" onclick="focusResetAuto()" title="Let the system adapt automatically">Auto</button>
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
    app.router.add_get(   "/",                                    handle_index)
    app.router.add_get(   "/api/cameras",                         api_cameras)
    app.router.add_get(   "/api/scan/status",                     api_scan_status)
    app.router.add_post(  "/api/scan",                            api_scan)
    app.router.add_post(  "/api/scan/cancel",                     api_scan_cancel)
    app.router.add_post(  "/api/credentials",                     api_set_credentials)
    app.router.add_delete("/api/cameras/{camera_id}/credentials", api_clear_credentials)
    app.router.add_post(  "/api/cameras/{camera_id}/name",        api_rename_camera)
    app.router.add_post(  "/api/cameras/{camera_id}/confirm",     api_confirm_camera)
    app.router.add_post(  "/api/cameras/{camera_id}/not_camera",  api_not_camera)
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

    # ── Graceful shutdown handler (SigRev-2 Item 5) ───────────────────────────
    # Registered with aiohttp's lifecycle so it fires on runner.cleanup().
    # Order: cancel snap tasks → SIGTERM ffmpeg → 3s wait → SIGKILL → persist.
    app.on_shutdown.append(_on_shutdown)

    return app


async def _on_shutdown(app: web.Application) -> None:
    """
    Graceful shutdown: called by aiohttp runner on SIGTERM / runner.cleanup().

    1. Cancel all active snap_loop asyncio tasks.
    2. SIGTERM all live ffmpeg child processes; wait up to 3 s; SIGKILL stragglers.
    3. Persist final frame timestamps to cameras.json (last-seen-time per cam).
    4. Shutdown thread pool.
    """
    log.info("AnyCam shutting down — cleaning up…")

    # ── 1. Cancel snap tasks ─────────────────────────────────────────────────
    tasks_cancelled = 0
    for cam_id, state in list(_SNAP.items()):
        task = state.get("task")
        if task and not task.done():
            task.cancel()
            tasks_cancelled += 1
    if tasks_cancelled:
        # Give cancelled tasks a moment to catch CancelledError
        await asyncio.gather(*[
            state["task"] for state in _SNAP.values()
            if state.get("task") and not state["task"].done()
        ], return_exceptions=True)
        log.info(f"  Cancelled {tasks_cancelled} snap task(s)")

    # ── 2. Terminate ffmpeg child processes ───────────────────────────────────
    procs_to_kill: list[tuple[str, object]] = []
    for cam_id, state in _SNAP.items():
        proc = state.get("proc")
        if proc is not None and proc.returncode is None:
            procs_to_kill.append((cam_id, proc))

    for cam_id, proc in procs_to_kill:
        try:
            proc.terminate()   # SIGTERM
            log.debug(f"  SIGTERM → ffmpeg [{cam_id}]")
        except Exception:
            pass

    if procs_to_kill:
        # Wait up to 3 seconds for all processes to exit
        deadline = asyncio.get_event_loop().time() + 3.0
        for cam_id, proc in procs_to_kill:
            remaining = max(0.0, deadline - asyncio.get_event_loop().time())
            try:
                await asyncio.wait_for(proc.wait(), timeout=remaining)
            except (asyncio.TimeoutError, Exception):
                pass

        # SIGKILL any that are still alive
        killed = 0
        for cam_id, proc in procs_to_kill:
            if proc.returncode is None:
                try:
                    proc.kill()
                    killed += 1
                    log.debug(f"  SIGKILL → ffmpeg [{cam_id}] (did not exit in 3 s)")
                except Exception:
                    pass
        log.info(
            f"  Terminated {len(procs_to_kill)} ffmpeg process(es)"
            + (f", force-killed {killed}" if killed else "")
        )

    # ── 3. Persist final frame timestamps ────────────────────────────────────
    # Store last frame time (as Unix epoch) per camera so the UI can show
    # when footage was last seen after a restart.
    changed = False
    for cam_id, state in _SNAP.items():
        ft = state.get("frame_time", 0.0)
        if ft and ft > 0:
            cam = CAMERAS.get(cam_id)
            if cam is not None:
                # frame_time is monotonic; convert to wall clock
                wall = time.time() - (asyncio.get_event_loop().time() - ft)
                cam["last_frame_wall"] = round(wall, 1)
                changed = True
    if changed:
        save_cameras()
        log.info("  Persisted frame timestamps to cameras.json")

    # ── 4. Shut down thread pool ──────────────────────────────────────────────
    _THREAD_POOL.shutdown(wait=False)
    log.info("AnyCam shutdown complete")


class _DockerIPFilter(logging.Filter):
    """Suppress aiohttp access log entries from Docker bridge IPs (172.x.x.x)."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        # Drop lines where the client IP starts with 172. (Docker/HA Supervisor)
        # Format: "172.30.32.2 [date] "METHOD /path..." status size ..."
        return not (msg.startswith("172.") or
                    msg.startswith('"172.') or
                    " 172." in msg[:20])


async def _probe_hw_decoders() -> None:
    """
    Probe hardware decoder availability once at startup (SigRev-2 Item 6).

    Strategy: device-file check + ffmpeg compiled-decoder list.
    No encode/decode round-trip — total probe time <200ms vs ~10s previously.

      v4l2m2m: check /dev/video* exists AND decoder appears in ffmpeg -decoders
      vaapi:   check /dev/dri/renderD* exists AND decoder appears in ffmpeg -decoders

    Logs:
      HW decoders available: hevc_v4l2m2m, h264_v4l2m2m
      <decoder>: unavailable (<reason>)
    """
    import glob as _glob

    log.info("Probing hardware decoder availability...")

    # Candidates: (decoder_name, device_glob, friendly_reason)
    candidates = [
        ("hevc_v4l2m2m", "/dev/video*",       "v4l2m2m device"),
        ("h264_v4l2m2m", "/dev/video*",       "v4l2m2m device"),
        ("hevc_vaapi",   "/dev/dri/renderD*", "VAAPI render device"),
        ("h264_vaapi",   "/dev/dri/renderD*", "VAAPI render device"),
    ]

    # Get ffmpeg compiled-decoder list once (fast, <100ms)
    decoder_list = ""
    try:
        list_proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-decoders",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        list_out, _ = await asyncio.wait_for(list_proc.communicate(), timeout=5)
        decoder_list = list_out.decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning(f"Could not query ffmpeg decoder list: {exc} — assuming no HW decoders")
        for dec, _, _ in candidates:
            _HW_UNAVAILABLE.add(dec)
        log.info("HW decoders available: none")
        return

    available = []
    for dec, dev_glob, dev_label in candidates:
        # Check 1: compiled into ffmpeg?
        if dec not in decoder_list:
            _HW_UNAVAILABLE.add(dec)
            log.info(f"  {dec}: unavailable (not compiled into ffmpeg)")
            continue

        # Check 2: required device file present?
        if not _glob.glob(dev_glob):
            _HW_UNAVAILABLE.add(dec)
            log.info(f"  {dec}: unavailable ({dev_label} not found)")
            continue

        available.append(dec)

    if available:
        log.info(f"HW decoders available: {', '.join(available)}")
    else:
        log.info("HW decoders available: none (software decode will be used)")


async def main() -> None:

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

    # ── Signal handling: SIGTERM / SIGINT → graceful shutdown ─────────────────
    # When HA stops the container it sends SIGTERM. We catch it, stop the
    # event-loop sentinel, and let runner.cleanup() trigger _on_shutdown.
    _stop_event = asyncio.Event()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _stop_event.set)
        except NotImplementedError:
            pass   # Windows — signals not supported on all platforms

    await _stop_event.wait()   # blocks until SIGTERM/SIGINT

    log.info("Shutdown signal received — starting graceful shutdown")
    await runner.cleanup()     # triggers app.on_shutdown → _on_shutdown

    # Thread pool is shut down inside _on_shutdown, but guard against
    # cases where the signal fires before runner is fully set up
    _THREAD_POOL.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())