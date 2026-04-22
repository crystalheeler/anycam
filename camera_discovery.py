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
import datetime
import hashlib
import ipaddress
import json
import logging
import os
import re
import signal
import socket
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
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
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

CURRENT_VERSION = "1.7.0"  # must match config.yaml

INGRESS_PATH = os.environ.get("INGRESS_PATH", "").rstrip("/")
PORT         = int(os.environ.get("INGRESS_PORT", 8099))
GO2RTC_PORT      = 1984
GO2RTC_RTSP_PORT = 8554
GO2RTC_API       = f"http://127.0.0.1:{GO2RTC_PORT}"

# ── HA add-on configuration options (set in the HA UI Config tab) ─────────────
# Read from env vars set by the HA supervisor from config.yaml options.
# Defaults mirror the config.yaml defaults so the server works without HA too.
CFG_LOW_FPS        = os.environ.get("LOW_FPS_MODE",   "true").lower()  == "true"
CFG_SKIP_NONREF    = os.environ.get("SKIP_NONREF",    "false").lower() == "true"
CFG_LIMIT_THREADS  = os.environ.get("LIMIT_THREADS",  "true").lower()  == "true"
CFG_STAGGER_POLL   = os.environ.get("STAGGER_POLLING","false").lower() == "true"
CFG_HW_DECODE      = os.environ.get("HW_DECODE",      "false").lower() == "true"
CFG_RECORDINGS     = os.environ.get("RECORDINGS_PATH", "/media/anycam")
CFG_MOTION_SENS    = int(os.environ.get("MOTION_SENSITIVITY",       "15"))
CFG_MOTION_COOL    = int(os.environ.get("MOTION_COOLDOWN_SECS",     "10"))
CFG_MOTION_PAD     = int(os.environ.get("MOTION_CLIP_PADDING_SECS", "3"))

MEDIA_DIR = Path(CFG_RECORDINGS)

# Currently focused camera for full-screen enhanced view.
# When set, all other snap_loops throttle to 1fps; focused loop runs native res.
_FOCUSED_CAMERA: str | None = None

# Hardware decoder names unavailable on this system (detected at runtime).
# When v4l2m2m reports "Could not find a valid device", the decoder name
# is added here so future stream requests skip hw decode immediately.
_HW_UNAVAILABLE: set = set()

# Per-camera snapshot state for the background ffmpeg processes that feed
# handle_snapshot.  Key = camera_id.
# Each value dict: frame(bytes|None), frame_time(float), frame_count(int),
#                  proc(Process|None), task(Task|None), restart_count(int)
_SNAP: dict = {}

# Timestamp of most recent handle_snapshot call per camera.
# snap_loop uses this to detect idle (>30s) and stop automatically.
_snap_last_access: dict = {}


def _snap_state(camera_id: str) -> dict:
    """Return (and lazily create) the snapshot state dict for a camera."""
    if camera_id not in _SNAP:
        _SNAP[camera_id] = {
            "frame":         None,
            "frame_time":    0.0,
            "frame_count":   0,
            "proc":          None,
            "task":          None,
            "restart_count": 0,
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


def load_oui_db():
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


async def refresh_oui_db():
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

        def _download():
            req = urllib.request.Request(OUI_CSV_URL)
            req.add_header("User-Agent", "AnyCam/1.0")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")

        raw = await loop.run_in_executor(None, _download)

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

def load_cameras():
    if not CAMS_FILE.exists():
        return
    try:
        for cam in json.loads(CAMS_FILE.read_text()):
            CAMERAS[cam["id"]] = cam
        log.info(f"Loaded {len(CAMERAS)} camera(s)")
    except Exception as e:
        log.warning(f"Load cameras: {e}")

def save_cameras():
    DATA_DIR.mkdir(exist_ok=True)
    safe = []
    for cam in CAMERAS.values():
        s = dict(cam)
        if s.get("credentials") and s.get("stream_url"):
            s["stream_url"] = _strip_creds(s["stream_url"])
        safe.append(s)
    CAMS_FILE.write_text(json.dumps(safe, indent=2))

def load_blacklist():
    if not BLACKLIST_FILE.exists():
        return
    try:
        BLACKLIST.update(json.loads(BLACKLIST_FILE.read_text()))
    except Exception:
        pass

def save_blacklist():
    DATA_DIR.mkdir(exist_ok=True)
    BLACKLIST_FILE.write_text(json.dumps(list(BLACKLIST)))

# In-memory feedback store: cid → rich fingerprint record
FEEDBACK: dict = {}

def load_feedback():
    if not FEEDBACK_FILE.exists():
        return
    try:
        FEEDBACK.update(json.loads(FEEDBACK_FILE.read_text()))
        log.info(f"Loaded {len(FEEDBACK)} feedback record(s)")
    except Exception as e:
        log.warning(f"Feedback load: {e}")

def save_feedback():
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

async def submit_to_community(record: dict):
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

def save_runtime(data: dict):
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

        def roundtrip(method: str, cseq: int, extra: dict = {}) -> str:
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


def _make_ssl_ctx():
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
        digest    = base64.b64encode(
            hashlib.sha1(nonce_raw + created.encode() + password.encode()).digest()
        ).decode()
        security = (
            '<s:Header><Security xmlns="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-wssecurity-secext-1.0.xsd"><UsernameToken>'
            f'<Username>{username}</Username>'
            f'<Password Type="...#PasswordDigest">{digest}</Password>'
            f'<Nonce EncodingType="...#Base64Binary">{nonce_b64}</Nonce>'
            f'<Created xmlns="...wssecurity-utility-1.0.xsd">{created}</Created>'
            '</UsernameToken></Security></s:Header>'
        )
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
        ' xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"{security}<s:Body>{body}</s:Body></s:Envelope>"
    )
    try:
        req = urllib.request.Request(url, envelope.encode(), method="POST")
        req.add_header("Content-Type", "application/soap+xml; charset=utf-8")
        req.add_header("SOAPAction", "")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"ONVIF SOAP ({url}): {e}")
        return None


def onvif_get_profiles(onvif_url: str, username: str, password: str) -> list[dict]:
    xml = _onvif_soap(onvif_url, "<trt:GetProfiles/>", username, password)
    if not xml:
        return []
    profiles = []
    try:
        root = ET.fromstring(xml)
        ns   = {"trt": "http://www.onvif.org/ver10/media/wsdl",
                "tt":  "http://www.onvif.org/ver10/schema"}
        for p in root.findall(".//trt:Profiles", ns):
            token = p.get("token", "")
            name_el = p.find("tt:Name", ns)
            name = name_el.text if name_el is not None else token
            if token:
                profiles.append({"token": token, "name": name})
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


def _onvif_media_url(ip: str, port: int, xaddrs: str) -> str:
    if xaddrs:
        return xaddrs.rstrip("/").replace("device_service", "media").replace("Device", "Media")
    scheme = "https" if port in (443, 8443) else "http"
    return f"{scheme}://{ip}:{port}/onvif/media"

# ─────────────────────────────────────────────────────────────────────────────
# Full-range port scanner (user-initiated, separate from camera scan)
# ─────────────────────────────────────────────────────────────────────────────

async def run_port_scan(ip: str):
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
                with open(xml_path) as f:
                    xml_text = f.read()
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

async def run_verification_scan():
    """
    Post-upgrade verification scan.
    Runs after saved cameras are loaded.

    For each saved camera:
      - Probe it with is_camera_positive() + the appropriate stream prober
      - If still reachable → keep as-is, status unchanged
      - If unreachable    → mark as "unverified_after_upgrade" so the user
                            can decide whether to keep or remove it

    After verifying saved cameras, runs a fresh full scan to discover
    any new cameras that upgraded detection capabilities might now find.
    """
    SCAN_STATE.update(
        running=True, progress=0, stage=1,
        stage_label="Post-upgrade verification",
        message="Post-upgrade: verifying previously saved cameras…"
    )
    loop = asyncio.get_event_loop()
    log.info(f"Post-upgrade verification scan (was: {load_runtime().get('version','?')} → now: {CURRENT_VERSION})")

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

        # Quick probe appropriate to the protocol
        if proto in ("RTSP", "DVR", "ONVIF"):
            url = cam.get("stream_url","")
            if url:
                found = await loop.run_in_executor(None, probe_rtsp, url, u, p)
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
                found = await loop.run_in_executor(None, probe_rtsp_options, ip, port)
        elif proto == "MJPEG":
            result = await loop.run_in_executor(None, probe_mjpeg_quick, ip, port)
            found = bool(result)
        elif proto == "HLS":
            result = await loop.run_in_executor(None, probe_hls_quick, ip, port)
            found = bool(result)
        elif proto == "RTMP":
            found = await loop.run_in_executor(None, probe_rtmp, ip, port)
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
            # Clear any stale unverified flag from a previous upgrade
            cam.pop("upgrade_missing", None)
            log.info(f"  Verified OK: {cam.get('name', ip)}")
        else:
            now_missing.append(cid)
            cam["upgrade_missing"] = True
            cam["upgrade_missing_version"] = CURRENT_VERSION
            # Don't delete — let the user decide
            log.info(f"  Not found after upgrade: {cam.get('name', ip)} @ {ip}:{port}")

    save_cameras()   # persist any newly-probed stream_codec fields
    if now_missing:
        log.info(f"Verification: {len(still_present)} OK, {len(now_missing)} not found")
        SCAN_STATE["message"] = (
            f"Verification: {len(still_present)} camera(s) OK, "
            f"{len(now_missing)} not responding — see cards for details."
        )
    else:
        log.info(f"Verification: all {len(still_present)} camera(s) still present")

    save_cameras()

    # ── Run a full fresh scan for new discoveries ────────────────────────
    # This is essential after upgrades: improved detection code may now
    # find cameras that previous versions missed entirely.
    SCAN_STATE.update(
        progress=20, stage=1,
        stage_label="Post-upgrade: full network scan",
        message=(
            f"Post-upgrade verification complete "
            f"({len(still_present)} verified OK, {len(now_missing)} not responding. "
            f"Now scanning subnet for newly discoverable cameras…"
        )
    )
    log.info("Post-upgrade: starting full subnet scan for new cameras")
    await run_scan()   # run_scan sets running=False when done


async def probe_stream_details(url: str, proto: str,
                               cid: str = "") -> dict:
    """
    Get stream codec/resolution/FPS details by registering with go2rtc
    and querying its streams API.  Falls back to ffprobe if go2rtc is
    unavailable.  Returns a flat dict of stream_* fields.
    """
    # Register with go2rtc so it connects and learns the stream details
    _tmp_name = cid or ("_probe_" + hashlib.md5(url.encode()).hexdigest()[:8])
    registered = await go2rtc_add(_tmp_name, url)

    if registered:
        # Give go2rtc a moment to connect and probe the stream
        await asyncio.sleep(2.0)
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(f"{GO2RTC_API}/api/streams",
                                    timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        streams_data = await r.json()
                        info = streams_data.get(_tmp_name, {})
                        # go2rtc reports producers with track info
                        result = _parse_go2rtc_stream_info(info)
                        if result:
                            log.info(f"  Stream details (go2rtc): {result}")
                            if not cid:
                                await go2rtc_remove(_tmp_name)
                            return result
        except Exception as e:
            log.debug(f"probe_stream_details go2rtc query: {e}")
        if not cid:
            await go2rtc_remove(_tmp_name)

    # Fallback: ffprobe (works even if go2rtc unavailable)
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
        data    = json.loads(out.decode("utf-8", errors="replace"))
        streams = data.get("streams", [])
        video   = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio   = next((s for s in streams if s.get("codec_type") == "audio"), None)
        result  = {}
        if video:
            fps_str = video.get("avg_frame_rate", "0/1")
            try:
                n, d = fps_str.split("/")
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
        log.info(f"  Stream details (ffprobe fallback): {result}")
        return result
    except Exception as e:
        log.debug(f"probe_stream_details ffprobe: {e}")
        return {}


def _parse_go2rtc_stream_info(info: dict) -> dict:
    """Extract stream_* fields from go2rtc's /api/streams entry."""
    result = {}
    producers = info.get("producers", [])
    for prod in producers:
        for track in prod.get("tracks", []):
            kind = track.get("kind", "")
            codec = (track.get("codec") or "").lower()
            if kind == "video" and codec and "stream_codec" not in result:
                result["stream_codec"] = codec.replace("h264", "h264").replace("h265", "hevc")
                w = track.get("width")
                h = track.get("height")
                fps = track.get("fps")
                if w: result["stream_width"]  = w
                if h: result["stream_height"] = h
                if fps: result["stream_fps"]  = fps
            elif kind == "audio" and codec and "stream_audio" not in result:
                result["stream_audio"] = codec
    return result


async def run_scan():
    _scan_start = time.time()

    # _total_estimate: running estimate of total scan duration in seconds.
    # Starts from last scan or 240s. Only ever decreases as we get more info.
    _runtime        = load_runtime()
    _last_dur       = _runtime.get("last_scan_duration", 0)
    _total_estimate = int(_last_dur) if _last_dur > 10 else 240

    SCAN_STATE.update(running=True, progress=0, stage=1,
                      stage_label="Stage 1/4 — Live host & multicast discovery",
                      message="ARP scan + ONVIF/SSDP/mDNS discovery running…",
                      started_at=_scan_start, elapsed=0.0,
                      eta=_total_estimate)
    loop = asyncio.get_event_loop()

    subnet  = await loop.run_in_executor(None, get_local_subnet)
    gateway = await loop.run_in_executor(None, get_default_gateway)
    log.info(f"Subnet: {subnet}  Gateway: {gateway}")

    # ── Stage 1: All 4 discovery methods in parallel ──────────────────────────
    arp_t   = loop.run_in_executor(None, discover_live_hosts,  subnet)
    onvif_t = loop.run_in_executor(None, onvif_discover,       5)
    ssdp_t  = loop.run_in_executor(None, ssdp_discover,        5)
    mdns_t  = loop.run_in_executor(None, mdns_discover,        5)

    arp_hosts, onvif_results, ssdp_results, mdns_results = await asyncio.gather(
        arp_t, onvif_t, ssdp_t, mdns_t
    )

    onvif_ips = {r["ip"] for r in onvif_results}
    ssdp_cam_ips = {r["ip"] for r in ssdp_results if r.get("is_camera")}
    mdns_ips = {r["ip"] for r in mdns_results}
    multicast_ips = onvif_ips | ssdp_cam_ips | mdns_ips

    all_live = (arp_hosts | multicast_ips) - BLACKLIST
    if gateway:
        all_live.discard(gateway)

    # Remove Docker/internal bridge IPs (172.x.x.x) — these are HA Supervisor
    # internal network addresses, not real LAN devices with cameras.
    all_live = {ip for ip in all_live
                if not ip.startswith("172.")
                and not ip.startswith("169.254.")}  # also skip APIPA

    disc_summary = []
    if arp_hosts:   disc_summary.append(f"{len(arp_hosts)} via ARP")
    if onvif_ips:   disc_summary.append(f"{len(onvif_ips)} via ONVIF")
    if ssdp_cam_ips: disc_summary.append(f"{len(ssdp_cam_ips)} via SSDP")
    if mdns_ips:    disc_summary.append(f"{len(mdns_ips)} via mDNS")
    disc_str = ", ".join(disc_summary) or "none"
    log.info(f"Live hosts: {len(all_live)} ({disc_str})")
    _s1_elapsed = time.time() - _scan_start
    # Refine total estimate using host count; Stage 1 ~ 10% of work.
    # Estimate: ~10s per host for focused scan + probing is a reasonable heuristic.
    _host_based  = _s1_elapsed + len(all_live) * 10
    _total_estimate = max(_total_estimate, _host_based)  # never go higher if host count is small
    _total_estimate = min(_total_estimate, _host_based * 2)  # cap at 2x host-based
    SCAN_STATE.update(
        message=f"{len(all_live)} live host(s) found ({disc_str}). Starting port scan…",
        elapsed=round(_s1_elapsed, 1),
        eta=max(0, int(_total_estimate - _s1_elapsed)))

    # Populate ARP_HOSTS for Port Scan UI — resolve hostnames from nmap results later;
    # for now, store IPs sorted numerically with empty hostname to be filled in
    ARP_HOSTS.clear()
    for ip in sorted(all_live, key=lambda x: [int(p) for p in x.split('.')]):
        ARP_HOSTS.append({"ip": ip, "hostname": ""})

    # ── Stage 2: Focused camera port scan on live hosts only ─────────────────
    SCAN_STATE.update(progress=25, stage=2,
                      stage_label="Stage 2/4 — Camera port scan",
                      message=f"Scanning camera ports on {len(all_live)} live host(s)…",
                      elapsed=round(time.time() - _scan_start, 1))

    nmap_results = await loop.run_in_executor(
        None, focused_nmap_scan, sorted(all_live))

    responding_ips = {h["ip"] for h in nmap_results}

    # ── Stage 3: Stream probing ───────────────────────────────────────────────
    _s2_elapsed = time.time() - _scan_start
    # Stage 2 complete ≈ 55% of work done; extrapolate total from actual elapsed.
    # Use the larger of: extrapolated-from-elapsed OR responding-hosts * 15s remaining.
    _extrap_total = _s2_elapsed / 0.55
    _host_remain  = len(nmap_results) * 15
    _total_estimate = min(_total_estimate, max(_extrap_total, _s2_elapsed + _host_remain))
    SCAN_STATE.update(progress=55, stage=3,
                      stage_label="Stage 3/4 — Stream probing",
                      message=f"Probing {len(nmap_results)} responding host(s)…",
                      elapsed=round(_s2_elapsed, 1),
                      eta=max(0, int(_total_estimate - _s2_elapsed)))

    # Update ARP_HOSTS hostnames from nmap results
    nmap_hostnames = {h["ip"]: h["hostname"] for h in nmap_results}
    for entry in ARP_HOSTS:
        if entry["ip"] in nmap_hostnames:
            entry["hostname"] = nmap_hostnames[entry["ip"]]

    # Remove our own service port from nmap results so AnyCam itself
    # never creates a camera card (port 8099 on the Pi serves our own UI).
    # We still probe the Pi for OAK Camera on port 8765.
    local_ip = next((e["ip"] for e in ARP_HOSTS if e["ip"] == get_local_ip()), "")
    for host in nmap_results:
        if host["ip"] == local_ip:
            host["open_ports"] = [
                p for p in host["open_ports"]
                if p["port"] != PORT   # skip AnyCam's own ingress port
            ]

    saved = {cid: c for cid, c in CAMERAS.items() if c.get("user_saved")}
    CAMERAS.clear()
    CAMERAS.update(saved)

    total = max(len(nmap_results), 1)
    for idx, host in enumerate(nmap_results):
        ip, hostname = host["ip"], host["hostname"]
        _elapsed_now = time.time() - _scan_start
        _eta_remain  = max(0, int(_total_estimate - _elapsed_now))
        SCAN_STATE.update(
            progress=55 + int(25 * idx / total),
            message=f"Probing {ip} ({idx+1}/{len(nmap_results)})…",
            elapsed=round(_elapsed_now, 1),
            eta=_eta_remain)

        verdict, reason = classify_device(host)
        for port_info in host["open_ports"]:
            port = port_info["port"]
            cid  = f"{ip}_{port}"
            if cid in BLACKLIST:
                continue
            initial = _initial_protocol(port, port_info["service"], port_info["product"])
            prev    = saved.get(cid, {})
            cam     = await _probe_host_port(ip, port, hostname, initial,
                                             prev, verdict, reason, loop,
                                             onvif_ips=onvif_ips,
                                             ssdp_cam_ips=ssdp_cam_ips,
                                             mdns_ips=mdns_ips,
                                             mac_addr=host.get("mac_addr",""),
                                             mac_vendor=host.get("mac_vendor",""))
            if cam:
                CAMERAS[cam["id"]] = cam

    # ── Stage 4 (optional): Deeper Scan on silent live hosts ──────────────────
    silent = sorted(all_live - responding_ips)
    if SCAN_OPTIONS.get("broad_sweep") and silent:
        SCAN_STATE.update(progress=82, stage=4,
                          stage_label="Stage 4/4 — Deeper Scan (0–10000)",
                          message=f"Deeper Scan on {len(silent)} unresponsive host(s)…")
        broad_results = await loop.run_in_executor(
            None, broad_nmap_scan, silent)
        for host in broad_results:
            ip, hostname = host["ip"], host["hostname"]
            verdict, reason = classify_device(host)
            for port_info in host["open_ports"]:
                port = port_info["port"]
                cid  = f"{ip}_{port}"
                if cid in BLACKLIST:
                    continue
                initial = _initial_protocol(port, port_info["service"], port_info["product"])
                prev    = saved.get(cid, {})
                cam     = await _probe_host_port(ip, port, hostname, initial,
                                                 prev, verdict, reason, loop,
                                                 onvif_ips=onvif_ips,
                                                 ssdp_cam_ips=ssdp_cam_ips,
                                                 mdns_ips=mdns_ips,
                                                 mac_addr=host.get("mac_addr",""),
                                                 mac_vendor=host.get("mac_vendor",""))
                if cam:
                    CAMERAS[cam["id"]] = cam

    # ── Merge multicast-only results ──────────────────────────────────────────
    for onvif in onvif_results:
        ip = onvif["ip"]
        if ip == gateway or ip in BLACKLIST:
            continue
        existing = [c for c in CAMERAS.values() if c["ip"] == ip]
        if existing:
            # Run HTTP identity probe if none of the existing cards identified
            # the manufacturer yet (e.g. device was found on port 554 only,
            # so no HTTP probing happened during _probe_host_port)
            if not any(c.get("manufacturer") for c in existing):
                identity = {}
                for _p in (80, 443, 8080, 8888, 8090, 34567):
                    try:
                        identity = await asyncio.wait_for(
                            loop.run_in_executor(None, probe_http_identity, ip, _p),
                            timeout=12
                        )
                    except asyncio.TimeoutError:
                        log.debug(f"  ONVIF HTTP probe timeout: {ip}:{_p}")
                        identity = {}
                    if identity.get("manufacturer"):
                        log.info(f"  ONVIF merge HTTP identity: {ip}:{_p} → {identity['manufacturer']}")
                        break
                    elif identity.get("is_camera"):
                        log.info(f"  ONVIF merge HTTP camera keyword: {ip}:{_p}")
                        break
                else:
                    log.info(f"  ONVIF merge: no HTTP identity found for {ip}")
                if identity.get("manufacturer"):
                    for cam in existing:
                        cam.setdefault("manufacturer", identity.get("manufacturer",""))
                        cam.setdefault("device_notes", identity.get("notes",""))
                        cam.setdefault("page_title",   identity.get("title",""))
                        cam.setdefault("server_header",identity.get("server",""))
                        # Auto-upgrade name if still using hostname default
                        if (cam["manufacturer"] and
                                cam.get("name") in (ip, onvif.get("name",""), "")):
                            cam["name"] = f"{cam['manufacturer']} ({ip})"
            for cam in existing:
                cam["onvif"]  = True
                cam["xaddrs"] = onvif.get("xaddrs", cam.get("xaddrs", ""))
                if cam["protocol"] == "HTTP":
                    cam["protocol"] = "ONVIF"
        else:
            cid  = f"{ip}_onvif"
            prev = saved.get(cid, {})
            # Try to identify manufacturer via HTTP on common ports
            identity = {}
            for _p in (80, 8080, 443):
                identity = await loop.run_in_executor(
                    None, probe_http_identity, ip, _p)
                if identity.get("manufacturer"):
                    break
            display_name = prev.get("name", onvif["name"])
            if identity.get("manufacturer") and display_name == onvif["name"]:
                display_name = f"{identity['manufacturer']} ({ip})"
            CAMERAS[cid] = {
                "id": cid, "ip": ip, "hostname": onvif["name"],
                "port": 80, "protocol": "ONVIF",
                "stream_url": prev.get("stream_url",""),
                "requires_credentials": True,
                "credentials": prev.get("credentials"),
                "name": display_name,
                "xaddrs": onvif.get("xaddrs",""),
                "status": "needs_credentials",
                "onvif": True, "user_saved": bool(prev), "display": "proxy",
                "verdict": "camera", "verdict_reason": "ONVIF discovered",
                "manufacturer": identity.get("manufacturer", ""),
                "device_notes": identity.get("notes", ""),
                "page_title":   identity.get("title", ""),
                "server_header":identity.get("server", ""),
            }

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

    # ── IP-level deduplication ───────────────────────────────────────────────
    # An IP is "confirmed camera" if ANY card on it meets ONE of:
    #   a) status=ready with a non-HTTP protocol (streaming)
    #   b) manufacturer is identified (even if still needs_credentials)
    #   c) protocol is a specific camera proto (RTSP, MJPEG, HLS, RTMP, etc.)
    #   d) verdict == "camera" from multicast discovery (ONVIF/SSDP/mDNS)
    # Once confirmed, suppress sibling cards that are HTTP-only /
    # needs_credentials with no manufacturer and no camera protocol.
    CAMERA_PROTOS = {"RTSP", "MJPEG", "HLS", "RTMP", "WebRTC", "WS-RTSP",
                     "DVR", "ONVIF"}

    def _is_confirmed(cam: dict) -> bool:
        if cam.get("status") == "ready" and cam.get("protocol") not in ("HTTP","UNKNOWN"):
            return True
        if cam.get("manufacturer"):
            return True
        if cam.get("protocol") in CAMERA_PROTOS:
            return True
        if cam.get("verdict_reason","").startswith("ONVIF") or            cam.get("verdict_reason","").startswith("SSDP") or            cam.get("verdict_reason","").startswith("mDNS"):
            return True
        return False

    confirmed_ips = {cam["ip"] for cam in CAMERAS.values() if _is_confirmed(cam)}

    def _is_noise(cam: dict) -> bool:
        """True if this card adds no value when a better card exists for the IP."""
        if cam.get("user_saved"):
            return False
        if cam.get("protocol") in CAMERA_PROTOS:
            return False
        if cam.get("manufacturer"):
            return False
        if cam.get("status") in ("ready", "info"):
            return False
        return cam.get("protocol") in ("HTTP", "UNKNOWN")

    suppressed = [
        cid for cid, cam in CAMERAS.items()
        if cam["ip"] in confirmed_ips and _is_noise(cam)
    ]
    for cid in suppressed:
        log.info(f"  Suppressed noise card: {CAMERAS[cid]['ip']}:{CAMERAS[cid].get('port')} "
                 f"— better card exists for this IP")
        del CAMERAS[cid]
    if suppressed:
        log.info(f"  Suppressed {len(suppressed)} HTTP-only noise card(s)")

    save_cameras()
    ready = sum(1 for c in CAMERAS.values() if c["status"] == "ready")
    elapsed_secs = round(time.time() - _scan_start)
    elapsed_str  = f"{elapsed_secs // 60}:{elapsed_secs % 60:02d}"
    completion_msg = f"Scan complete — {len(CAMERAS)} device(s), {ready} streaming."
    SCAN_STATE.update(
        running=False, progress=100, stage=0, stage_label="",
        message=completion_msg, elapsed=elapsed_secs, eta=0)
    log.info(f"{completion_msg} Completed in {elapsed_str}.")
    _rt = load_runtime(); _rt["last_scan_duration"] = elapsed_secs; save_runtime(_rt)

    # Persist actual duration so next scan can use it as the initial ETA
    _runtime_save = load_runtime()
    _runtime_save["last_scan_duration"] = elapsed_secs
    save_runtime(_runtime_save)


# Wrap run_scan in a safety shell that catches exceptions and always
# resets the running flag so the UI doesn't get stuck indefinitely
_run_scan_inner = run_scan
async def run_scan():
    try:
        await _run_scan_inner()
    except Exception as e:
        import traceback
        log.error(f"run_scan crashed: {e}")
        log.error(traceback.format_exc())
        SCAN_STATE.update(
            running=False, progress=100, stage=0, stage_label="",
            message=f"Scan error: {e}. Check logs for details.",
        )


async def _probe_host_port(ip, port, hostname, initial_protocol,
                           prev, verdict, reason, loop,
                           onvif_ips=None, ssdp_cam_ips=None,
                           mdns_ips=None,
                           mac_addr="", mac_vendor="") -> dict | None:
    cid = f"{ip}_{port}"
    prev_creds = prev.get("credentials")
    prev_name  = prev.get("name", hostname)
    saved_u = saved_p = ""
    if prev_creds:
        try:
            saved_u, saved_p = decrypt_creds(prev_creds)
        except Exception:
            pass

    # ── Active camera gate ────────────────────────────────────────────────
    # Skip this port entirely unless at least one active probe confirms
    # it looks like a camera.  User-saved cameras bypass this check so
    # they always appear after being manually confirmed.
    if not bool(prev):
        camera_confirmed = await loop.run_in_executor(
            None, is_camera_positive,
            ip, port, initial_protocol, "",
            verdict,
            onvif_ips or set(),
            ssdp_cam_ips or set(),
            mdns_ips or set(),
            mac_addr,
        )
        if not camera_confirmed:
            log.info(f"  Skipping {ip}:{port} — no camera-positive signal")
            return None

    # ── Collect identity info from HTTP if available ─────────────────────
    identity = {}
    if port in (80, 8080, 8000, 8888, 443, 8443):
        try:
            identity = await loop.run_in_executor(
                None, probe_http_identity, ip, port)
        except Exception:
            pass
    # Also try to identify from nmap banner via DB
    if not identity.get("manufacturer"):
        nmap_text = f"{initial_protocol}"
        entry = identify_manufacturer(nmap_text) if nmap_text.strip() else None
        if entry:
            identity.setdefault("manufacturer", entry["name"])
            identity.setdefault("notes", entry["notes"])

    # ── OUI camera-positive signal ──────────────────────────────────────
    oui_cam = oui_is_camera(mac_addr) if mac_addr else None

    def base(proto, url, status, display="proxy"):
        cam = {
            "id": cid, "ip": ip, "hostname": hostname, "port": port,
            "protocol": proto, "stream_url": url,
            "requires_credentials": False, "credentials": None,
            "name": prev_name, "status": status,
            "user_saved": bool(prev), "display": display,
            "verdict": verdict, "verdict_reason": reason,
            "manufacturer": identity.get("manufacturer", ""),
            "device_notes": identity.get("notes", ""),
            "page_title":   identity.get("title", ""),
            "server_header":identity.get("server", ""),
            "mac_addr":     mac_addr,
            "mac_vendor":   mac_vendor,
        }
        # Use OUI vendor to fill manufacturer if HTTP identity didn't find one
        if not cam["manufacturer"] and mac_vendor and oui_cam is True:
            cam["manufacturer"] = mac_vendor
        # Upgrade display name if still default
        if cam["manufacturer"] and cam["name"] == hostname:
            cam["name"] = f"{cam['manufacturer']} ({ip})"
        return cam

    if initial_protocol in ("RTSP", "DVR"):
        url = await loop.run_in_executor(None, find_rtsp_path, ip, port)
        if url:
            return base("RTSP", url, "ready")
        if saved_u:
            url = await loop.run_in_executor(None, find_rtsp_path, ip, port, saved_u, saved_p)
            if url:
                cam = base("RTSP", url, "ready")
                cam["credentials"] = prev_creds
                return cam
        cam = base("RTSP", "", "needs_credentials")
        cam["requires_credentials"] = True
        return cam

    if initial_protocol == "RTMP" or port in (1935, 1936):
        ok = await loop.run_in_executor(None, probe_rtmp, ip, port)
        if ok:
            return base("RTMP", f"rtmp://{ip}:{port}/live/stream", "ready")

    if initial_protocol in ("HTTP", "ONVIF", "UNKNOWN"):
        url = await loop.run_in_executor(None, probe_mjpeg_http, ip, port)
        if url:
            return base("MJPEG", url, "ready")
        if saved_u:
            url = await loop.run_in_executor(None, probe_mjpeg_http, ip, port, saved_u, saved_p)
            if url:
                cam = base("MJPEG", url, "ready"); cam["credentials"] = prev_creds; return cam

        url = await loop.run_in_executor(None, probe_hls, ip, port)
        if url:
            return base("HLS", url, "ready", "hls")
        if saved_u:
            url = await loop.run_in_executor(None, probe_hls, ip, port, saved_u, saved_p)
            if url:
                cam = base("HLS", url, "ready", "hls"); cam["credentials"] = prev_creds; return cam

        url = await loop.run_in_executor(None, find_rtsp_path, ip, port)
        if url:
            return base("RTSP", url, "ready")

        wrtc = await loop.run_in_executor(None, probe_webrtc, ip, port)
        if wrtc:
            cam = base("WebRTC", wrtc, "info", "webrtc")
            cam["info"] = "WebRTC signaling detected. Direct browser negotiation required."
            cam["signaling_url"] = wrtc
            return cam

        ws = await loop.run_in_executor(None, probe_ws_rtsp, ip, port)
        if ws:
            cam = base("WS-RTSP", ws, "info", "wsrtsp")
            cam["info"] = "WS-RTSP endpoint detected. Full playback planned for a future release."
            cam["ws_url"] = ws
            return cam

        # Always return a card for any device with open ports.
        # Suppression only happens via explicit user blacklist ("Not a Camera" button).
        # Automatic not_camera verdict just sets the badge — user decides.
        cam = base("HTTP", "", "needs_credentials")
        cam["requires_credentials"] = True
        return cam

    return None

# ─────────────────────────────────────────────────────────────────────────────
# Stream / snapshot handlers
# ─────────────────────────────────────────────────────────────────────────────

def build_authenticated_url(camera: dict) -> str | None:
    url = camera.get("stream_url", "")
    if not url:
        return None
    creds = camera.get("credentials")
    if creds:
        try:
            from urllib.parse import quote as _quote
            u, p = decrypt_creds(creds)
            # URL-encode only characters that break URL parsing.
            # Per RFC 3986 userinfo, sub-delims like ! $ & ' ( ) * + , ; = are
            # VALID unencoded — only @ : / ? # [ ] must be encoded.
            # Many RTSP servers don't decode %-encoded credentials, so encoding
            # ! as %21 causes authentication to fail with the wrong password.
            _USERINFO_SAFE = "!$&'()*+,;=~-._"
            u_enc = _quote(u, safe=_USERINFO_SAFE)
            p_enc = _quote(p, safe=_USERINFO_SAFE)
            proto, rest = url.split("://", 1)
            rest = re.sub(r"^[^@]+@", "", rest)   # strip any existing creds
            url  = f"{proto}://{u_enc}:{p_enc}@{rest}"
        except Exception as e:
            log.warning(f"Cred decrypt: {e}")
    return url


# ─────────────────────────────────────────────────────────────────────────
# go2rtc integration
# go2rtc runs on port 1984, handles all camera protocols/codecs/auth.
# We register each camera URL and proxy its MJPEG output to the browser.
# ─────────────────────────────────────────────────────────────────────────
_go2rtc_streams: set = set()


async def go2rtc_add(cid: str, url: str) -> bool:
    """Register a stream source in go2rtc."""
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.put(
                f"{GO2RTC_API}/api/streams",
                params={"name": cid},
                data=url,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                ok = r.status < 300
                if ok:
                    _go2rtc_streams.add(cid)
                    log.info(f"go2rtc: registered {cid}")
                else:
                    body = await r.text()
                    log.warning(f"go2rtc register {cid} -> {r.status}: {body[:80]}")
                return ok
    except Exception as e:
        log.warning(f"go2rtc register {cid} failed: {e}")
        return False


async def go2rtc_remove(cid: str):
    _go2rtc_streams.discard(cid)
    try:
        async with aiohttp.ClientSession() as sess:
            await sess.delete(f"{GO2RTC_API}/api/streams",
                              params={"name": cid},
                              timeout=aiohttp.ClientTimeout(total=3))
    except Exception:
        pass


def go2rtc_source(camera: dict, url: str) -> str:
    """
    Build the go2rtc source URL.
    HEVC cameras use go2rtc's ffmpeg integration to transcode HEVC→H.264
    internally, so the local RTSP restream on port 8554 delivers H.264.
    ffmpeg (used later for MJPEG) can then handle H.264 easily.
    H.264 or unknown cameras are registered directly — go2rtc re-streams as-is.
    """
    codec = (camera.get("stream_codec") or "").lower()
    w     = camera.get("stream_width") or 0
    if codec in ("hevc", "h265"):
        # go2rtc ffmpeg wrapper: decode HEVC, re-encode to H.264 at reduced res/fps
        if w >= 3840:
            params = "video=h264&width=480&height=270&fps=4"
        else:
            params = "video=h264&width=640&height=360&fps=8"
        return f"ffmpeg:{url}#{params}"
    # H.264 or unknown: let go2rtc handle natively; ffmpeg will decode H.264 easily
    return url


async def go2rtc_ensure(cid: str, url: str, camera: dict = {}) -> bool:
    if cid not in _go2rtc_streams:
        src = go2rtc_source(camera, url) if camera else url
        return await go2rtc_add(cid, src)
    return True


async def go2rtc_register_all():
    """Register all ready cameras with go2rtc on startup using correct source URLs."""
    for cam in list(CAMERAS.values()):
        if cam.get("status") == "ready":
            url = build_authenticated_url(cam)
            if url:
                src = go2rtc_source(cam, url)
                ffwrap = src.startswith("ffmpeg:")
                log.info(f"go2rtc: registering {cam['id']} "
                         f"({'ffmpeg-wrapped' if ffwrap else 'direct'}, "
                         f"codec={cam.get('stream_codec') or '?'})")
                await go2rtc_add(cam["id"], src)


async def _drain_stderr(proc: object, label: str) -> None:
    """
    Module-level stderr drain coroutine — shared by handle_stream and snap_loop.

    Reads ffmpeg's stderr line-by-line throughout the process lifetime so the
    OS pipe buffer never fills (which would deadlock ffmpeg).  Filters out the
    cosmetic 'deprecated pixel format' swscaler warning that fires on every
    new scale context when using yuvj420p.  On exit, logs all collected lines
    as a single WARNING and auto-detects v4l2m2m unavailability.

    label examples:
      "10.0.0.33_onvif"          — from handle_stream
      "SNAP:10.0.0.33_onvif"     — from snap_loop
    """
    lines = []
    try:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").rstrip()
            # Filter cosmetic swscaler deprecation warning — unavoidable with
            # yuvj420p on this ffmpeg build, not an error, very spammy.
            if decoded and "deprecated pixel format" not in decoded:
                lines.append(decoded)
    except asyncio.CancelledError:
        # On cancellation, grab any remaining buffered bytes before exiting.
        try:
            remaining = await proc.stderr.read(8192)
            for ln in remaining.decode("utf-8", errors="replace").splitlines():
                if ln.strip() and "deprecated pixel format" not in ln:
                    lines.append(ln.strip())
        except Exception:
            pass
    except Exception:
        pass
    if lines:
        joined = " | ".join(lines)[:600]
        log.warning(f"Stream {label} ffmpeg stderr: {joined}")
        # Auto-detect v4l2m2m hardware decoder unavailability.
        for hw in ("hevc_v4l2m2m", "h264_v4l2m2m"):
            if hw in joined and "Could not find a valid device" in joined:
                _HW_UNAVAILABLE.add(hw)
                log.info(f"Marked {hw} as unavailable on this system")


async def handle_stream(request: web.Request) -> web.StreamResponse:
    """
    Direct streaming: ffmpeg connects straight to the camera RTSP URL and
    outputs MJPEG to stdout.  Frames are parsed from the raw byte stream
    using JPEG SOI (0xFF 0xD8) and EOI (0xFF 0xD9) markers and forwarded
    as multipart/x-mixed-replace to the browser.

    Hardware decode is attempted first (v4l2m2m on ARM); on empty output or
    timeout it immediately retries with software decode.  A concurrent asyncio
    task drains stderr so the pipe never fills and deadlocks ffmpeg.
    """
    camera_id = request.match_info["camera_id"]
    camera    = CAMERAS.get(camera_id)
    if not camera:
        return web.Response(status=404)
    if camera.get("display") in ("webrtc", "wsrtsp", "info"):
        return web.Response(status=400, text="Not proxy-streamable")

    url = build_authenticated_url(camera)
    if not url:
        return web.Response(status=503)

    proto        = camera.get("protocol", "RTSP")
    stream_codec = camera.get("stream_codec", "").lower()
    stream_w     = camera.get("stream_width") or 0
    is_hevc      = stream_codec in ("hevc", "h265")

    # Scale + fps limits: 4K HEVC -> 480p@4fps, other HEVC -> 640p@8fps, else 640p@10fps
    # yuv420p in the vf chain; -color_range 2 tells the mjpeg encoder to use full range.
    if is_hevc and stream_w >= 3840:
        out_vf = "fps=4,scale=480:-2,format=yuvj420p"
    elif is_hevc:
        out_vf = "fps=8,scale=640:-2,format=yuvj420p"
    else:
        out_vf = "fps=10,scale=640:-2,format=yuvj420p"

    safe_url = _strip_creds(url)
    log.info(f"Stream [{proto}] {camera_id} -> {safe_url} "
             f"codec={stream_codec or '?'} vf={out_vf}")

    response = web.StreamResponse(headers={
        "Content-Type":        "multipart/x-mixed-replace; boundary=frame",
        "Cache-Control":       "no-cache",
        "Pragma":              "no-cache",
        "Connection":          "keep-alive",
        "X-Accel-Buffering":   "no",   # tell nginx (HA ingress proxy) not to buffer
        "X-Content-Type-Options": "nosniff",
    })
    await response.prepare(request)

    # JPEG frame delimiters defined as runtime byte objects — zero escape ambiguity.
    SOI  = bytes([0xFF, 0xD8])   # Start Of Image
    EOI  = bytes([0xFF, 0xD9])   # End Of Image
    CRLF = bytes([0x0D, 0x0A])   # Carriage Return + Line Feed

    async def _launch(hw_dec=""):
        """Spawn ffmpeg pointing directly at the camera RTSP URL."""
        hw_args = ["-c:v", hw_dec] if hw_dec else []
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-timeout", "8000000",
            *hw_args,
            "-i", url,
            "-an",
            "-vf", out_vf,
            "-vcodec", "mjpeg",
            "-pix_fmt", "yuvj420p",  # mjpeg encoder requires full-range yuvj420p
            "-q:v", "5",
            "-f", "image2pipe",
            "pipe:1",
        ]
        log.info(f"Stream {camera_id} ffmpeg cmd: " +
                 " ".join(a if "://" not in a else _strip_creds(a) for a in cmd))
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    # _drain_stderr is a module-level coroutine — see below handle_stream

    # Try hardware decode first; on ARM this is hevc_v4l2m2m / h264_v4l2m2m.
    # Skip hw decode immediately if we already know it's unavailable on this system.
    _wanted_hw = "hevc_v4l2m2m" if is_hevc else ("h264_v4l2m2m" if stream_codec == "h264" else "")
    hw_dec     = "" if (_wanted_hw in _HW_UNAVAILABLE) else _wanted_hw
    if _wanted_hw and hw_dec == "":
        log.info(f"Stream {camera_id}: skipping {_wanted_hw} (known unavailable), using sw decode")
    proc     = await _launch(hw_dec)
    stderr_t = asyncio.create_task(_drain_stderr(proc, camera_id))
    buf      = b""
    frames   = 0
    hw_tried = bool(hw_dec)

    try:
        while True:
            # Use a short timeout on the very first read when hw decode is active.
            timeout = 3.0 if (hw_tried and frames == 0) else 30.0
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(65536), timeout=timeout)
            except asyncio.TimeoutError:
                if hw_tried and frames == 0:
                    try: proc.kill()
                    except Exception: pass
                    try: await asyncio.wait_for(proc.wait(), timeout=2)
                    except Exception: pass
                    log.info(f"Stream {camera_id}: hw decode timeout, retrying software")
                    proc     = await _launch()
                    stderr_t = asyncio.create_task(_drain_stderr(proc, camera_id))
                    hw_tried = False
                    continue
                log.warning(f"Stream {camera_id}: 30s read timeout ({frames} frames sent)")
                break

            if not chunk:
                # EOF from ffmpeg stdout — process has exited
                rc = proc.returncode
                if hw_tried and frames == 0:
                    try: proc.kill()
                    except Exception: pass
                    try: await asyncio.wait_for(proc.wait(), timeout=2)
                    except Exception: pass
                    log.info(f"Stream {camera_id}: hw decode EOF (rc={rc}), retrying software")
                    proc     = await _launch()
                    stderr_t = asyncio.create_task(_drain_stderr(proc, camera_id))
                    hw_tried = False
                    continue
                # Software (or no-hw) exit — always log so we know what happened
                log.warning(f"Stream {camera_id}: ffmpeg EOF after {frames} frames (rc={rc})")
                break

            buf += chunk

            # Safety cap: discard if corrupted data accumulates without a frame.
            if len(buf) > 4_000_000:
                log.warning(f"Stream {camera_id}: buf overflow ({len(buf)} bytes), discarding")
                buf = b""
                continue

            # Extract all complete JPEG frames from the accumulated buffer.
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
                frame = buf[s : e + 2]
                buf   = buf[e + 2:]
                frames += 1
                try:
                    hdr = (
                        b"--frame" + CRLF
                        + b"Content-Type: image/jpeg" + CRLF
                        + b"Content-Length: " + str(len(frame)).encode() + CRLF
                        + CRLF
                    )
                    await response.write(hdr + frame + CRLF)
                    if frames == 1 or frames % 100 == 0:
                        log.info(f"Stream {camera_id}: sent frame {frames} "
                                 f"({len(frame)} bytes)")
                except (ConnectionResetError, ConnectionAbortedError):
                    log.info(f"Stream {camera_id}: client disconnected after {frames} frames")
                    return response

    except Exception as ex:
        log.warning(f"Stream {camera_id}: exception: {ex}")
    finally:
        # Kill ffmpeg first, then wait for stderr drain to flush its collected lines.
        try: proc.kill()
        except Exception: pass
        try: await asyncio.wait_for(proc.wait(), timeout=3)
        except Exception: pass
        # Give stderr drain task up to 2s to flush whatever it collected.
        try: await asyncio.wait_for(stderr_t, timeout=2)
        except Exception: stderr_t.cancel()
    return response

async def handle_stream_test(request: web.Request) -> web.Response:
    """
    Diagnostic endpoint: checks stream connectivity and returns codec/resolution.
    Uses the pure Python RTSP socket probe first (fast, no subprocesses),
    then queries go2rtc for detailed stream info.
    """
    camera_id = request.match_info["camera_id"]
    camera    = CAMERAS.get(camera_id)
    if not camera:
        return web.json_response({"error": "Camera not found"}, status=404)

    url = build_authenticated_url(camera)
    if not url:
        return web.json_response({"error": "No stream URL configured"}, status=400)

    proto = camera.get("protocol", "RTSP")

    # Fast connectivity check via pure Python socket probe
    from urllib.parse import urlparse as _up
    p    = _up(url)
    host = p.hostname or camera.get("ip","")
    port = p.port or 554
    path = p.path or "/"
    u    = p.username or ""
    pw   = p.password or ""

    connected = await asyncio.get_event_loop().run_in_executor(
        None, probe_rtsp_socket, host, port, path, u, pw, 8.0)

    if not connected:
        result = {"success": False, "url": _strip_creds(url),
                  "error": "RTSP DESCRIBE failed — check credentials and network"}
        log.info(f"Stream test [{camera_id}]: {result}")
        return web.json_response(result)

    # Get codec info from stored details or go2rtc
    result = {
        "success": True,
        "url":     _strip_creds(url),
        "codec":   camera.get("stream_codec") or "?",
        "width":   camera.get("stream_width"),
        "height":  camera.get("stream_height"),
        "fps":     camera.get("stream_fps"),
    }

    # If codec not known, try go2rtc (may already be registered)
    if not result["codec"] or result["codec"] == "?":
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(f"{GO2RTC_API}/api/streams",
                                    timeout=aiohttp.ClientTimeout(total=3)) as r:
                    if r.status == 200:
                        info = (await r.json()).get(camera_id, {})
                        g2 = _parse_go2rtc_stream_info(info)
                        if g2:
                            result.update({
                                "codec":  g2.get("stream_codec", result["codec"]),
                                "width":  g2.get("stream_width",  result["width"]),
                                "height": g2.get("stream_height", result["height"]),
                                "fps":    g2.get("stream_fps",    result["fps"]),
                            })
        except Exception:
            pass

    log.info(f"Stream test [{camera_id}]: {result}")
    return web.json_response(result)


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

    async def _launch_snap(hw_dec: str = "", native_res: bool = False):
        """Launch ffmpeg for snapshot polling.
        native_res=True: use camera's native resolution/fps (for focus view).
        Respects CFG_ options: thread limiting, skip_nonref, low_fps_mode.
        """
        hw_args     = ["-c:v", hw_dec] if hw_dec else []
        thread_args = ["-threads", "2"] if CFG_LIMIT_THREADS else []
        skip_args   = ["-skip_frame", "nonref"] if CFG_SKIP_NONREF else []
        hw_label    = f"hw:{hw_dec}" if hw_dec else "sw"

        if native_res:
            # Focus mode: run at camera's native resolution and fps
            native_vf = f"format=yuvj420p"
            vf_used   = native_vf
            fps_label = f"native ({stream_fps or '?'}fps, {stream_w or '?'}px)"
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
        return await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "warning",
            "-rtsp_transport", "tcp", "-timeout", "8000000",
            *skip_args,
            *hw_args,
            "-i", url,
            "-an", "-vf", vf_used,
            *thread_args,
            "-vcodec", "mjpeg", "-pix_fmt", "yuvj420p",
            "-q:v", "5", "-f", "image2pipe", "pipe:1",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    try:
        while True:   # outer restart loop
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
            wanted_hw = ("hevc_v4l2m2m" if is_hevc else
                         ("h264_v4l2m2m" if stream_codec == "h264" else ""))
            if not CFG_HW_DECODE:
                hw_dec = ""
            elif wanted_hw in _HW_UNAVAILABLE:
                hw_dec = ""
                log.info(f"SNAP [{camera_id}]: skipping {wanted_hw} (known unavailable)")
            else:
                hw_dec = wanted_hw

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
            log.info(f"SNAP [{camera_id}]: restarting in 2s "
                     f"(#{state['restart_count']})")
            await asyncio.sleep(2)

    finally:
        state["proc"] = None
        state["task"] = None
        log.info(f"SNAP [{camera_id}]: loop done")


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

    url = build_authenticated_url(camera)
    if not url:
        return web.Response(status=503, text="No stream URL")

    # Record access time so snap_loop knows we're still watching
    _snap_last_access[camera_id] = time.monotonic()

    state = _snap_state(camera_id)

    # Start background snap process if not already running
    if state.get("task") is None or state["task"].done():
        log.info(f"SNAP [{camera_id}]: starting background process "
                 f"(codec={camera.get('stream_codec') or '?'}, "
                 f"res={camera.get('stream_width') or '?'}px)")
        state["task"] = asyncio.create_task(snap_loop(camera_id, url, camera))

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
    # Ensure snap_loop is running for this camera in native-res mode
    camera = CAMERAS[camera_id]
    url    = build_authenticated_url(camera)
    if url:
        state = _snap_state(camera_id)
        if state.get("task") is None or state["task"].done():
            state["task"] = asyncio.create_task(
                snap_loop(camera_id, url, camera, native_res=True))
    return web.json_response({"status": "ok", "focused": camera_id})


async def handle_focus_clear(request: web.Request) -> web.Response:
    """DELETE /snap/focus — exit full-screen focus mode."""
    global _FOCUSED_CAMERA
    log.info(f"Focus: leaving enhanced view (was: {_FOCUSED_CAMERA})")
    _FOCUSED_CAMERA = None
    return web.json_response({"status": "ok"})


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
    """Derive a short filesystem-safe folder name from the camera display name."""
    name = camera.get("name", "") or camera.get("ip", "unknown")
    # Strip common useless prefixes
    for prefix in ("Generic IP Camera", "Generic", "Unknown Camera", "Unknown"):
        if name.startswith(prefix):
            name = name[len(prefix):].lstrip(" ()")
    # Strip parenthetical IP: "(10.0.0.22)"
    import re as _re
    name = _re.sub(r"\(\d+\.\d+\.\d+\.\d+\)", "", name)
    # Replace em-dash profile separator
    name = name.replace(" — ", "_").replace("—", "_")
    # Keep only alphanumeric + underscore
    name = _re.sub(r"[^a-zA-Z0-9_]", "_", name)
    name = _re.sub(r"_+", "_", name).strip("_").lower()
    if not name:
        name = camera.get("ip", "camera").replace(".", "_")
    # Ensure uniqueness via IP last octet suffix if name is generic
    if len(name) < 4 or name in ("main", "sub", "stream"):
        ip = camera.get("ip", "")
        suffix = ip.split(".")[-1] if ip else ""
        if suffix:
            name = f"{name}_{suffix}"
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
    s["has_credentials"]  = bool(s.get("credentials"))
    s["upgrade_missing"]  = bool(s.get("upgrade_missing"))
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


async def api_cameras(request):
    return web.json_response([_safe_cam(c) for c in CAMERAS.values()])

async def api_scan(request):
    if SCAN_STATE["running"]:
        return web.json_response({"error": "Scan already running"}, status=409)
    try:
        data = await request.json()
        SCAN_OPTIONS["broad_sweep"] = bool(data.get("broad_sweep", False))
    except Exception:
        pass
    asyncio.create_task(run_scan())
    return web.json_response({"status": "started"})

async def api_scan_status(request):
    return web.json_response(SCAN_STATE)


async def api_set_credentials(request):
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
            None, onvif_get_profiles, media_url, username, password)
        log.info(f"  ONVIF profiles found: {len(profiles)} — {[p['name'] for p in profiles]}")
        if profiles:
            enc_creds = encrypt_creds(username, password)
            created   = []
            for prof in profiles:
                stream_url = await loop.run_in_executor(
                    None, onvif_get_stream_uri, media_url, prof["token"], username, password)
                log.info(f"  Profile '{prof['name']}' stream_url: {stream_url}")
                if not stream_url:
                    continue
                ok = await loop.run_in_executor(
                    None, probe_rtsp, stream_url, username, password,
                    6, f"{camera_id}/{prof['name']}")
                log.info(f"  probe_rtsp OK: {ok}")
                if not ok:
                    # ONVIF SOAP already confirmed credentials are valid —
                    # create the card anyway and let streaming reveal any issue
                    log.warning(f"  probe_rtsp returned False for ONVIF profile "
                                f"'{prof['name']}' — creating card anyway "
                                f"(ONVIF SOAP confirmed credentials)")
                details = await probe_stream_details(stream_url, "RTSP")
                cid = f"{ip}_onvif_{prof['token']}"
                CAMERAS[cid] = {
                    "id": cid, "ip": ip,
                    "hostname": camera.get("hostname", ip),
                    "port": port, "protocol": "RTSP", "onvif": True,
                    "stream_url": stream_url,
                    "requires_credentials": False, "credentials": enc_creds,
                    "name": f"{camera.get('name', ip)} — {prof['name']}",
                    "status": "ready", "display": "proxy", "user_saved": True,
                    "verdict": "camera", "verdict_reason": "ONVIF profile",
                    **details,
                }
                created.append(cid)
            if created:
                CAMERAS.pop(camera_id, None)
                save_cameras()
                # Register new ONVIF profile cards with go2rtc
                for cid in created:
                    cam = CAMERAS.get(cid)
                    if cam:
                        cam_url = build_authenticated_url(cam) or ""
                        asyncio.create_task(
                            go2rtc_add(cid, go2rtc_source(cam, cam_url)))
                return web.json_response({"status": "ok", "channels": len(created)})
            log.warning(f"  ONVIF: profiles found but no streams probed OK — falling back to direct RTSP")
        else:
            log.warning(f"  ONVIF: no profiles returned (auth failed or device unreachable)")

    # RTSP direct — for ONVIF cards always try port 554 in addition to stored port
    if proto in ("RTSP", "DVR", "ONVIF"):
        # Try stored port first
        log.info(f"  Trying direct RTSP on {ip}:{port}")
        url = await loop.run_in_executor(
            None, find_rtsp_path, ip, port, username, password)
        # For ONVIF cards the stored port is often 80; always also try 554
        if not url and port != 554:
            log.info(f"  Trying direct RTSP on {ip}:554 (standard RTSP port)")
            url = await loop.run_in_executor(
                None, find_rtsp_path, ip, 554, username, password)
        if not url and camera.get("xaddrs"):
            parsed = urlparse(camera["xaddrs"])
            rtsp_port = parsed.port or 554
            log.info(f"  Trying RTSP via xaddrs {ip}:{rtsp_port}")
            url = await loop.run_in_executor(
                None, find_rtsp_path, parsed.hostname or ip,
                rtsp_port, username, password)
        log.info(f"  RTSP result: {_strip_creds(url) if url else 'None'}")
    elif proto == "MJPEG":
        url = await loop.run_in_executor(
            None, probe_mjpeg_http, ip, port, username, password)
    elif proto == "HLS":
        url = await loop.run_in_executor(
            None, probe_hls, ip, port, username, password)

    if not url:
        log.warning(f"Credential attempt FAILED for {camera_id} — no working stream found")
        return web.json_response({"error": "Could not connect with those credentials."}, status=401)

    details = await probe_stream_details(url, proto)
    camera.update(credentials=encrypt_creds(username, password),
                  stream_url=url, requires_credentials=False,
                  status="ready", user_saved=True, **details)
    save_cameras()
    log.info(f"Credentials accepted for {camera_id}: {_strip_creds(url)}")
    # Register with go2rtc (best-effort, don't block the response)
    _cam_url = build_authenticated_url(camera) or url
    asyncio.create_task(go2rtc_add(camera_id, go2rtc_source(camera, _cam_url)))
    return web.json_response({"status": "ok", "stream_url": _strip_creds(url), **details})


async def api_clear_credentials(request):
    cid    = request.match_info["camera_id"]
    camera = CAMERAS.get(cid)
    if not camera:
        return web.json_response({"error": "Not found"}, status=404)
    camera.update(credentials=None,
                  stream_url=_strip_creds(camera.get("stream_url","")),
                  requires_credentials=True, status="needs_credentials")
    save_cameras()
    return web.json_response({"status": "ok"})

async def api_rename_camera(request):
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

async def api_delete_camera(request):
    cid = request.match_info["camera_id"]
    CAMERAS.pop(cid, None)
    save_cameras()
    asyncio.create_task(go2rtc_remove(cid))
    return web.json_response({"status": "ok"})

async def api_confirm_camera(request):
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


async def api_not_camera(request):
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

async def api_add_camera(request):
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
            if await loop.run_in_executor(None, probe_rtsp, test_url, username, password):
                url = test_url
        if not url:
            url = await loop.run_in_executor(None, find_rtsp_path, ip, port, username, password)
    elif protocol == "MJPEG":
        url = await loop.run_in_executor(None, probe_mjpeg_http, ip, port, username, password)
    elif protocol == "HLS":
        url = await loop.run_in_executor(None, probe_hls, ip, port, username, password)
    elif protocol == "RTMP":
        if await loop.run_in_executor(None, probe_rtmp, ip, port):
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


async def api_arp_hosts(request):
    """Return the last ARP-discovered host list for the Port Scan UI."""
    return web.json_response(ARP_HOSTS)


async def api_pscan_start(request):
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


async def run_batch_port_scan():
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

async def api_pscan_status(request):
    # Add current elapsed so JS can compute drift between polls
    resp = dict(PSCAN)
    if resp.get("scan_start") and resp.get("running"):
        resp["elapsed"] = round(time.time() - resp["scan_start"], 1)
    resp.pop("live_ports", None)  # send separately to avoid huge payload
    resp["live_ports"] = PSCAN.get("live_ports", [])
    return web.json_response(resp)

async def api_pscan_cancel(request):
    pid = PSCAN.get("proc_pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    PSCAN.update(running=False, paused=False, message="Cancelled.", proc_pid=None)
    return web.json_response({"status": "ok"})

async def api_pscan_pause(request):
    pid = PSCAN.get("proc_pid")
    if pid and PSCAN["running"] and not PSCAN["paused"]:
        try:
            os.kill(pid, signal.SIGSTOP)
            PSCAN["paused"]  = True
            PSCAN["message"] = f"Paused — {len(PSCAN['results'])} port(s) found so far."
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"status": "ok"})

async def api_pscan_resume(request):
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

/* Called after renderGrid() to start polling for all visible snap cameras */
function initSnaps() {
  document.querySelectorAll('[data-snap]').forEach(img => {
    startSnap(img.dataset.snap);
  });
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
  _focusCamId = camId;
  // Tell server: enter focus mode (other cams throttle, this cam goes native res)
  await fetch(BASE + '/snap/focus/' + camId, {method: 'POST'}).catch(() => {});

  const img     = document.getElementById('focus-img');
  const infoEl  = document.getElementById('focus-info');
  const fps     = cam.stream_fps    || '?';
  const res     = (cam.stream_width || '?') + 'x' + (cam.stream_height || '?');
  const codec   = (cam.stream_codec || '?').toUpperCase();
  infoEl.textContent = (cam.name || cam.ip) + ' — ' + res + ' · ' + fps + ' fps · ' + codec;

  // Fast polling: 60ms intervals (~16fps attempt at native res)
  const poll = () => {
    if (_focusCamId !== camId) return;
    const loader  = new Image();
    loader.onload = () => { img.src = loader.src; };
    loader.src    = BASE + '/snapshot/' + camId + '?t=' + Date.now() + '&focus=1';
    _focusTimer   = setTimeout(poll, 60);
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
}

// Close focus on Escape key
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && _focusCamId) closeFocus();
});

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

function renderStorage(d) {
  const disk  = d.disk || {};
  const pct   = disk.pct_used || 0;
  const label = document.getElementById('disk-label');
  const fill  = document.getElementById('disk-bar-fill');
  label.textContent = 'Storage: ' + pct + '% used — '
    + (disk.free_gb || 0) + ' GB free of ' + (disk.total_gb || 0) + ' GB';
  fill.style.width = Math.min(pct, 100) + '%';
  fill.style.background = pct > 90 ? 'var(--red)' : pct > 70 ? 'var(--orange)' : 'var(--primary)';

  const list = document.getElementById('storage-list');
  if (!d.folders || !d.folders.length) {
    list.innerHTML = '<p style="color:var(--text-dim);padding:24px">No recordings yet. Enable motion detection on a camera to start recording.</p>';
    return;
  }

  list.innerHTML = d.folders.map(folder => `
    <div class="stor-folder" id="sf-${esc(folder.folder)}"
         ondragover="e=>e.preventDefault()"
         ondrop="storDrop(event, '${esc(folder.folder)}')">
      <div class="stor-folder-hdr">
        <span class="stor-folder-name" title="Click to rename"
              ondblclick="storRenameFolder('${esc(folder.folder)}')">${esc(folder.folder)}</span>
        <span class="stor-folder-meta">${folder.count} clip${folder.count !== 1 ? 's' : ''} · ${folder.size_mb} MB</span>
      </div>
      <div class="stor-files">
        ${folder.files.length ? folder.files.map(f => `
          <div class="stor-file" draggable="true"
               ondragstart="storDragStart(event,'${esc(folder.folder + '/' + f.name)}','${esc(folder.folder)}')">
            <span class="stor-file-icon">🎬</span>
            <span class="stor-file-name" title="Double-click to rename"
                  ondblclick="storRenameFile('${esc(folder.folder + '/' + f.name)}', '${esc(f.name)}')">${esc(f.name)}</span>
            <span class="stor-file-size">${f.size_mb} MB</span>
            <span class="stor-file-date">${new Date(f.mtime * 1000).toLocaleString()}</span>
            <a class="btn btn-ghost btn-xs" href="${BASE}/api/storage/download?path=${encodeURIComponent(folder.folder + '/' + f.name)}" download>&#x2B07;</a>
            <button class="btn btn-danger btn-xs" onclick="storDeleteFile('${esc(folder.folder + '/' + f.name)}')">&#x1F5D1;</button>
          </div>`).join('')
        : '<span class="stor-empty">No recordings</span>'}
      </div>
    </div>`).join('');
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
  return testBtn + clearBtn + notCamBtn + recBtn
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

  return '<div class="feed-wrap">' + feedHTML(cam) + '</div>'
    + '<div class="card-info">'
    + '<div class="status-dot ' + dotClass(cam) + '"></div>'
    + '<span class="card-name" title="' + name + '"'
    + ' onclick="openRename(\'' + cam.id + '\',\'' + name.replace(/'/g, "\\'") + '\')">'
    + name + '</span></div>'
    + '<div class="badges">' + protoBadge(cam.protocol)
    + '<span class="badge" style="background:#1e2d1e;color:#6fcf97">:' + cam.port + '</span>'
    + '<span class="badge" style="background:#2d2020;color:#e88">' + cam.ip + '</span>'
    + onvifBdg + credBdg + uncBdg + upgradeBdg + '</div>'
    + '<span class="badge" style="background:#3a2a10;color:var(--orange)">' 
      + (cam.upgrade_missing ? '⚠ Not found after upgrade' : '') + '</span>'
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


def build_html() -> str:
    js_code = _JS.replace('___BASE___', INGRESS_PATH)
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
#storage-header{{display:flex;align-items:center;gap:16px;margin-bottom:20px;padding:14px 18px;background:var(--card-bg);border-radius:10px;border:1px solid var(--border)}}
#disk-bar-wrap{{flex:1}}
#disk-label{{font-size:.82rem;color:var(--text-dim);display:block;margin-bottom:6px}}
#disk-bar-track{{height:8px;background:#2a2a2a;border-radius:4px;overflow:hidden}}
#disk-bar-fill{{height:100%;border-radius:4px;transition:width .4s,background .4s}}
#storage-list{{display:flex;flex-direction:column;gap:14px}}
.stor-folder{{background:var(--card-bg);border:2px solid var(--border);border-radius:10px;overflow:hidden;transition:border-color .2s}}
.stor-folder[ondragover]{{border-color:var(--primary)}}
.stor-folder-hdr{{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:#1a1a1a;border-bottom:1px solid var(--border)}}
.stor-folder-name{{font-weight:600;cursor:pointer;color:var(--text)}}
.stor-folder-name:hover{{color:var(--primary)}}
.stor-folder-meta{{font-size:.78rem;color:var(--text-dim)}}
.stor-files{{padding:8px}}
.stor-file{{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:6px;cursor:grab;transition:background .15s}}
.stor-file:hover{{background:#1e2a1e}}
.stor-file-icon{{font-size:1.1rem;flex-shrink:0}}
.stor-file-name{{flex:1;font-size:.84rem;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}}
.stor-file-name:hover{{color:var(--primary)}}
.stor-file-size{{font-size:.74rem;color:var(--text-dim);white-space:nowrap}}
.stor-file-date{{font-size:.74rem;color:var(--text-dim);white-space:nowrap}}
.stor-empty{{font-size:.8rem;color:var(--text-dim);padding:8px 10px;display:block}}
.btn-xs{{padding:3px 8px;font-size:.72rem}}
/* ── Focus overlay ── */
#focus-overlay{{position:fixed;inset:0;background:#000;z-index:9000;display:flex;flex-direction:column;align-items:center;justify-content:center}}
#focus-close{{position:absolute;top:16px;right:20px;background:#222;border:1px solid #444;color:#fff;font-size:1.4rem;width:40px;height:40px;border-radius:50%;cursor:pointer;z-index:9001;line-height:1}}
#focus-close:hover{{background:#444}}
#focus-img{{max-width:100%;max-height:calc(100vh - 60px);object-fit:contain;display:block}}
#focus-info{{position:absolute;bottom:12px;left:50%;transform:translateX(-50%);font-size:.8rem;color:#aaa;background:rgba(0,0,0,.6);padding:4px 12px;border-radius:20px;white-space:nowrap}}
#focus-warning{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);background:#1a1a1a;border:1px solid var(--orange);border-radius:10px;padding:24px;max-width:480px;text-align:center;z-index:9002;display:flex;flex-direction:column;gap:14px;align-items:center}}
#focus-warn-text{{color:#f5b942;font-size:.9rem;line-height:1.5}}
/* ── Toast ── */
#toast{{position:fixed;bottom:28px;left:50%;transform:translateX(-50%);padding:10px 22px;border-radius:24px;color:#fff;font-size:.85rem;z-index:9100;pointer-events:none;transition:opacity .3s}}
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
  <h1>
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M15 10l4.553-2.069A1 1 0 0121 8.87v6.26a1 1 0 01-1.447.9L15 14"/>
      <rect x="1" y="7" width="14" height="10" rx="2" ry="2"/>
    </svg>
    AnyCam
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
    <button class="btn btn-ghost" onclick="switchView('cameras')">&#x2190; Back</button>
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
  <div id="storage-header">
    <div id="disk-bar-wrap">
      <span id="disk-label">Loading storage info...</span>
      <div id="disk-bar-track"><div id="disk-bar-fill"></div></div>
    </div>
    <button class="btn btn-secondary btn-sm" onclick="loadStorage()">&#x21BB; Refresh</button>
  </div>
  <div id="storage-list"></div>
</div>

<!-- ── Focus / full-screen enhanced view overlay ──────────────────────────── -->
<div id="focus-overlay" style="display:none">
  <button id="focus-close" onclick="closeFocus()" title="Exit enhanced view">&#x2715;</button>
  <div id="focus-warning" style="display:none">
    <span id="focus-warn-text"></span>
    <button onclick="document.getElementById('focus-warning').style.display='none'">OK</button>
  </div>
  <img id="focus-img" alt="Enhanced view">
  <div id="focus-info"></div>
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

def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get(   "/",                                    handle_index)
    app.router.add_get(   "/api/cameras",                         api_cameras)
    app.router.add_get(   "/api/scan/status",                     api_scan_status)
    app.router.add_post(  "/api/scan",                            api_scan)
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
    app.router.add_post(  "/snap/focus/{camera_id}",              handle_focus_set)
    app.router.add_delete("/snap/focus",                          handle_focus_clear)
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


class _DockerIPFilter(logging.Filter):
    """Suppress aiohttp access log entries from Docker bridge IPs (172.x.x.x)."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        # Drop lines where the client IP starts with 172. (Docker/HA Supervisor)
        # Format: "172.30.32.2 [date] "METHOD /path..." status size ..."
        return not (msg.startswith("172.") or
                    msg.startswith('"172.') or
                    " 172." in msg[:20])


async def main():
    load_cameras()
    load_blacklist()
    load_feedback()
    load_oui_db()   # Load cached OUI DB synchronously (fast, from disk)

    # Suppress Docker bridge IP entries from the aiohttp access log
    _access_log = logging.getLogger("aiohttp.access")
    _access_log.addFilter(_DockerIPFilter())

    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"AnyCam on :{PORT}  ingress='{INGRESS_PATH}'")
    # Register all saved cameras with go2rtc on startup
    asyncio.get_event_loop().create_task(go2rtc_register_all())

    startup_mode = get_startup_mode()
    log.info(f"Startup mode: {startup_mode}")

    if startup_mode == "new_install":
        log.info("New install — starting initial scan")
        asyncio.create_task(run_scan())

    elif startup_mode == "post_upgrade":
        prev = load_runtime().get("version", "unknown")
        log.info(f"Post-upgrade ({prev} → {CURRENT_VERSION}) — running verification scan")
        asyncio.create_task(run_verification_scan())

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

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
