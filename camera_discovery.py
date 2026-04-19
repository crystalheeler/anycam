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

INGRESS_PATH = os.environ.get("INGRESS_PATH", "").rstrip("/")
PORT         = int(os.environ.get("INGRESS_PORT", 8099))

# ─────────────────────────────────────────────────────────────────────────────
# State
# ─────────────────────────────────────────────────────────────────────────────

CAMERAS    = {}
BLACKLIST  = set()
SCAN_STATE = {"running": False, "progress": 0, "message": "Idle. Click Scan to begin.",
               "stage": 0, "stage_label": ""}
SCAN_OPTIONS = {"broad_sweep": False}
_FERNET    = None

PSCAN = {
    "running": False, "paused": False, "ip": "",
    "progress": 0, "message": "", "results": [], "proc_pid": None,
}

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
    Scan only known-live hosts on the camera port list.
    Because hosts are pre-confirmed alive, no timeout waste on dead IPs.
    Typical time: 15-45 seconds for 50 hosts.
    """
    if not host_list:
        return []
    ports_str = ",".join(str(p) for p in CAMERA_PORTS)
    log.info(f"Focused scan: {len(host_list)} host(s), ports {ports_str}")
    try:
        r = subprocess.run(
            ["nmap", "-sV", "--open", "-p", ports_str,
             "--host-timeout", "20s", "-T4", "-oX", "-"] + host_list,
            capture_output=True, text=True, timeout=300,
        )
        hosts = _parse_nmap_xml(r.stdout)
        log.info(f"Focused scan: {len(hosts)} host(s) responded on camera ports")
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
        hostname   = hn.get("name", ip) if hn is not None else ip
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
            results.append({"ip": ip, "hostname": hostname, "open_ports": open_ports})
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

def probe_rtsp(url: str, username: str = "", password: str = "",
               timeout: int = 4) -> bool:
    probe_url = url
    if username:
        proto, rest = url.split("://", 1)
        rest = re.sub(r"^[^@]+@", "", rest)
        probe_url = f"{proto}://{username}:{password}@{rest}"
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
             "-analyzeduration", "1000000", "-probesize", "200000",
             "-i", probe_url],
            timeout=timeout + 3, capture_output=True,
        )
        return r.returncode == 0
    except Exception:
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


def probe_http_for_camera(ip: str, port: int, timeout: int = 4) -> bool:
    """
    Fetch the HTTP root page and scan response headers + body for
    camera-specific strings.  Much more reliable than nmap banner matching
    because we're reading actual page content, not a service fingerprint.
    """
    import urllib.request
    import urllib.error

    scheme = "https" if port in (443, 8443) else "http"
    url    = f"{scheme}://{ip}:{port}/"

    CAMERA_BODY_MARKERS = [
        b"camera", b"ipcam", b"webcam", b"nvr", b"dvr", b"cctv",
        b"onvif", b"rtsp", b"video", b"stream", b"live view",
        b"hikvision", b"dahua", b"reolink", b"axis", b"amcrest",
        b"network camera", b"ip camera", b"surveillance",
        b"channel", b"ptz", b"pan tilt",
    ]
    CAMERA_HEADER_MARKERS = [
        "camera", "ipcam", "nvr", "dvr", "onvif",
        "hikvision", "dahua", "reolink", "axis",
    ]

    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "AnyCam/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Check headers (Server, X-Application, etc.)
            all_headers = str(resp.headers).lower()
            for marker in CAMERA_HEADER_MARKERS:
                if marker in all_headers:
                    log.info(f"  HTTP header camera confirm: {ip}:{port} ({marker})")
                    return True
            # Check body (first 4KB)
            body = resp.read(4096).lower()
            for marker in CAMERA_BODY_MARKERS:
                if marker in body:
                    log.info(f"  HTTP body camera confirm: {ip}:{port} ({marker.decode()})")
                    return True
    except urllib.error.HTTPError as e:
        # 401/403 on a camera-like path is still informative — check headers
        all_headers = str(e.headers).lower()
        for marker in CAMERA_HEADER_MARKERS:
            if marker in all_headers:
                log.info(f"  HTTP 4xx header camera confirm: {ip}:{port} ({marker})")
                return True
    except Exception:
        pass
    return False


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
                        mdns_ips: set) -> bool:
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

    # ── 10. nmap keyword fallback — least reliable, last resort ────────────
    combined = (service + " " + product).lower()
    if any(k in combined for k in CAMERA_KEYWORDS):
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
    PSCAN.update(running=True, paused=False, ip=ip, progress=5,
                 message=f"Scanning all 65535 ports on {ip}…", results=[], proc_pid=None)
    try:
        proc = await asyncio.create_subprocess_exec(
            "nmap", "-sV", "-sC", "-A", "--open", "-p-",
            "--host-timeout", "600s", "-T3", "-oX", "-", ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        PSCAN["proc_pid"] = proc.pid
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=660)
        xml_text  = stdout.decode("utf-8", errors="replace")
        results = []
        try:
            root = ET.fromstring(xml_text)
            for host in root.findall("host"):
                for port_el in host.findall("ports/port"):
                    pst = port_el.find("state")
                    if pst is None or pst.get("state") != "open":
                        continue
                    svc     = port_el.find("service")
                    scripts = {sc.get("id",""): sc.get("output","")
                               for sc in port_el.findall("script")}
                    results.append({
                        "port":    int(port_el.get("portid")),
                        "proto":   port_el.get("protocol", "tcp"),
                        "service": svc.get("name","")    if svc is not None else "",
                        "product": svc.get("product","") if svc is not None else "",
                        "version": svc.get("version","") if svc is not None else "",
                        "extra":   svc.get("extrainfo","") if svc is not None else "",
                        "scripts": scripts,
                    })
        except Exception as e:
            log.warning(f"Port scan XML parse: {e}")
        PSCAN.update(running=False, progress=100, results=results,
                     message=f"Scan complete — {len(results)} open port(s) on {ip}.")
    except asyncio.TimeoutError:
        PSCAN.update(running=False, progress=100, message="Scan timed out.")
    except Exception as e:
        PSCAN.update(running=False, progress=100, message=f"Scan error: {e}")
    finally:
        PSCAN["proc_pid"] = None

# ─────────────────────────────────────────────────────────────────────────────
# Main scan orchestration — 4-stage pipeline
# ─────────────────────────────────────────────────────────────────────────────

async def run_scan():
    SCAN_STATE.update(running=True, progress=0, stage=1,
                      stage_label="Stage 1/4 — Live host & multicast discovery",
                      message="Stage 1/4 — ARP scan + ONVIF/SSDP/mDNS discovery…")
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

    disc_summary = []
    if arp_hosts:   disc_summary.append(f"{len(arp_hosts)} via ARP")
    if onvif_ips:   disc_summary.append(f"{len(onvif_ips)} via ONVIF")
    if ssdp_cam_ips: disc_summary.append(f"{len(ssdp_cam_ips)} via SSDP")
    if mdns_ips:    disc_summary.append(f"{len(mdns_ips)} via mDNS")
    disc_str = ", ".join(disc_summary) or "none"
    log.info(f"Live hosts: {len(all_live)} ({disc_str})")
    SCAN_STATE.update(message=f"Stage 1 complete — {len(all_live)} live host(s) found ({disc_str}). Starting port scan…")

    # ── Stage 2: Focused camera port scan on live hosts only ─────────────────
    SCAN_STATE.update(progress=25, stage=2,
                      stage_label="Stage 2/4 — Camera port scan",
                      message=f"Stage 2/4 — Scanning camera ports on {len(all_live)} live host(s)…")

    nmap_results = await loop.run_in_executor(
        None, focused_nmap_scan, sorted(all_live))

    responding_ips = {h["ip"] for h in nmap_results}

    # ── Stage 3: Stream probing ───────────────────────────────────────────────
    SCAN_STATE.update(progress=55, stage=3,
                      stage_label="Stage 3/4 — Stream probing",
                      message=f"Stage 3/4 — Probing {len(nmap_results)} responding host(s)…")

    saved = {cid: c for cid, c in CAMERAS.items() if c.get("user_saved")}
    CAMERAS.clear()
    CAMERAS.update(saved)

    total = max(len(nmap_results), 1)
    for idx, host in enumerate(nmap_results):
        ip, hostname = host["ip"], host["hostname"]
        SCAN_STATE.update(
            progress=55 + int(25 * idx / total),
            message=f"Stage 3/4 — Probing {ip} ({idx+1}/{len(nmap_results)})…")

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
                                             mdns_ips=mdns_ips)
            if cam:
                CAMERAS[cam["id"]] = cam

    # ── Stage 4 (optional): Broad sweep on silent live hosts ──────────────────
    silent = sorted(all_live - responding_ips)
    if SCAN_OPTIONS.get("broad_sweep") and silent:
        SCAN_STATE.update(progress=82, stage=4,
                          stage_label="Stage 4/4 — Broad sweep (0–10000)",
                          message=f"Stage 4/4 — Broad sweep on {len(silent)} unresponsive host(s)…")
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
                                                 mdns_ips=mdns_ips)
                if cam:
                    CAMERAS[cam["id"]] = cam

    # ── Merge multicast-only results ──────────────────────────────────────────
    for onvif in onvif_results:
        ip = onvif["ip"]
        if ip == gateway or ip in BLACKLIST:
            continue
        existing = [c for c in CAMERAS.values() if c["ip"] == ip]
        if existing:
            for cam in existing:
                cam["onvif"]  = True
                cam["xaddrs"] = onvif.get("xaddrs", cam.get("xaddrs", ""))
                if cam["protocol"] == "HTTP":
                    cam["protocol"] = "ONVIF"
        else:
            cid  = f"{ip}_onvif"
            prev = saved.get(cid, {})
            CAMERAS[cid] = {
                "id": cid, "ip": ip, "hostname": onvif["name"],
                "port": 80, "protocol": "ONVIF",
                "stream_url": prev.get("stream_url",""),
                "requires_credentials": True,
                "credentials": prev.get("credentials"),
                "name": prev.get("name", onvif["name"]),
                "xaddrs": onvif.get("xaddrs",""),
                "status": "needs_credentials",
                "onvif": True, "user_saved": bool(prev), "display": "proxy",
                "verdict": "camera", "verdict_reason": "ONVIF discovered",
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

    save_cameras()
    ready = sum(1 for c in CAMERAS.values() if c["status"] == "ready")
    SCAN_STATE.update(
        running=False, progress=100, stage=0, stage_label="",
        message=f"Scan complete — {len(CAMERAS)} device(s), {ready} streaming."
    )
    log.info(SCAN_STATE["message"])


async def _probe_host_port(ip, port, hostname, initial_protocol,
                           prev, verdict, reason, loop,
                           onvif_ips=None, ssdp_cam_ips=None,
                           mdns_ips=None) -> dict | None:
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
        )
        if not camera_confirmed:
            log.info(f"  Skipping {ip}:{port} — no camera-positive signal")
            return None

    def base(proto, url, status, display="proxy"):
        return {"id": cid, "ip": ip, "hostname": hostname, "port": port,
                "protocol": proto, "stream_url": url,
                "requires_credentials": False, "credentials": None,
                "name": prev_name, "status": status,
                "user_saved": bool(prev), "display": display,
                "verdict": verdict, "verdict_reason": reason}

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
            u, p = decrypt_creds(creds)
            proto, rest = url.split("://", 1)
            rest = re.sub(r"^[^@]+@", "", rest)
            url  = f"{proto}://{u}:{p}@{rest}"
        except Exception as e:
            log.warning(f"Cred decrypt: {e}")
    return url


async def handle_stream(request: web.Request) -> web.StreamResponse:
    camera_id = request.match_info["camera_id"]
    camera    = CAMERAS.get(camera_id)
    if not camera:
        return web.Response(status=404)
    if camera.get("display") in ("webrtc", "wsrtsp", "info"):
        return web.Response(status=400, text="Not proxy-streamable")
    url = build_authenticated_url(camera)
    if not url:
        return web.Response(status=503)

    proto = camera.get("protocol", "RTSP")
    flags = (["-rtsp_transport", "tcp"] if proto in ("RTSP", "DVR")
             else ["-re"] if proto == "HLS" else [])

    response = web.StreamResponse(headers={
        "Content-Type":  "multipart/x-mixed-replace; boundary=frame",
        "Cache-Control": "no-cache", "Pragma": "no-cache", "Connection": "keep-alive",
    })
    await response.prepare(request)

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error",
        *flags, "-i", url,
        "-vf", "fps=10,scale=640:-2", "-q:v", "5", "-f", "mjpeg", "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    log.info(f"Stream [{proto}] {camera_id}")
    buf = b""
    try:
        while True:
            chunk = await asyncio.wait_for(proc.stdout.read(16384), timeout=15)
            if not chunk:
                break
            buf += chunk
            while True:
                s = buf.find(b"\xff\xd8")
                if s < 0:
                    break
                e = buf.find(b"\xff\xd9", s + 2)
                if e < 0:
                    break
                frame = buf[s:e + 2]
                buf   = buf[e + 2:]
                try:
                    await response.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                        + frame + b"\r\n"
                    )
                except (ConnectionResetError, ConnectionAbortedError):
                    return response
    except asyncio.TimeoutError:
        pass
    except Exception as ex:
        log.warning(f"Stream error: {ex}")
    finally:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
    return response


async def handle_snapshot(request: web.Request) -> web.Response:
    camera_id = request.match_info["camera_id"]
    camera    = CAMERAS.get(camera_id)
    if not camera:
        return web.Response(status=404)
    url = build_authenticated_url(camera)
    if not url:
        return web.Response(status=503)
    proto = camera.get("protocol", "RTSP")
    extra = ["-rtsp_transport", "tcp"] if proto in ("RTSP", "DVR") else []
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", *extra, "-i", url,
            "-vframes", "1", "-q:v", "3", "-f", "image2", "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
        if stdout:
            return web.Response(body=stdout, content_type="image/jpeg")
    except Exception as ex:
        log.warning(f"Snapshot: {ex}")
    return web.Response(status=503)

# ─────────────────────────────────────────────────────────────────────────────
# REST API
# ─────────────────────────────────────────────────────────────────────────────

def _safe_cam(cam: dict) -> dict:
    s = dict(cam)
    if s.get("stream_url"):
        s["stream_url"] = _strip_creds(s["stream_url"])
    s["has_credentials"] = bool(s.get("credentials"))
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
    loop  = asyncio.get_event_loop()
    url   = None

    if proto in ("ONVIF",) or camera.get("onvif"):
        media_url = _onvif_media_url(camera["ip"], camera["port"], camera.get("xaddrs",""))
        profiles  = await loop.run_in_executor(
            None, onvif_get_profiles, media_url, username, password)
        if profiles:
            enc_creds = encrypt_creds(username, password)
            created   = []
            for prof in profiles:
                stream_url = await loop.run_in_executor(
                    None, onvif_get_stream_uri, media_url, prof["token"], username, password)
                if not stream_url:
                    continue
                ok = await loop.run_in_executor(None, probe_rtsp, stream_url, username, password)
                if not ok:
                    continue
                cid = f"{camera['ip']}_onvif_{prof['token']}"
                CAMERAS[cid] = {
                    "id": cid, "ip": camera["ip"],
                    "hostname": camera.get("hostname", camera["ip"]),
                    "port": camera["port"], "protocol": "RTSP", "onvif": True,
                    "stream_url": stream_url,
                    "requires_credentials": False, "credentials": enc_creds,
                    "name": f"{camera.get('name', camera['ip'])} — {prof['name']}",
                    "status": "ready", "display": "proxy", "user_saved": True,
                    "verdict": "camera", "verdict_reason": "ONVIF profile",
                }
                created.append(cid)
            if created:
                CAMERAS.pop(camera_id, None)
                save_cameras()
                return web.json_response({"status": "ok", "channels": len(created)})

    if proto in ("RTSP", "DVR", "ONVIF"):
        url = await loop.run_in_executor(
            None, find_rtsp_path, camera["ip"], camera["port"], username, password)
        if not url and camera.get("xaddrs"):
            parsed = urlparse(camera["xaddrs"])
            url = await loop.run_in_executor(
                None, find_rtsp_path, parsed.hostname or camera["ip"],
                parsed.port or 554, username, password)
    elif proto == "MJPEG":
        url = await loop.run_in_executor(
            None, probe_mjpeg_http, camera["ip"], camera["port"], username, password)
    elif proto == "HLS":
        url = await loop.run_in_executor(
            None, probe_hls, camera["ip"], camera["port"], username, password)

    if not url:
        return web.json_response({"error": "Could not connect with those credentials."}, status=401)

    camera.update(credentials=encrypt_creds(username, password),
                  stream_url=url, requires_credentials=False,
                  status="ready", user_saved=True)
    save_cameras()
    return web.json_response({"status": "ok", "stream_url": _strip_creds(url)})


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
    CAMERAS.pop(request.match_info["camera_id"], None)
    save_cameras()
    return web.json_response({"status": "ok"})

async def api_not_camera(request):
    cid = request.match_info["camera_id"]
    cam = CAMERAS.get(cid)
    if cam:
        BLACKLIST.add(cam["ip"])
        BLACKLIST.add(cid)
        CAMERAS.pop(cid, None)
        save_cameras()
        save_blacklist()
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


async def api_pscan_start(request):
    try:
        data = await request.json()
        ip   = data.get("ip","").strip()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not ip:
        return web.json_response({"error": "IP required"}, status=400)
    if PSCAN["running"]:
        return web.json_response({"error": "Scan already running"}, status=409)
    asyncio.create_task(run_port_scan(ip))
    return web.json_response({"status": "started"})

async def api_pscan_status(request):
    return web.json_response(PSCAN)

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
  document.getElementById(
    v === 'cameras' ? 'cameras-view' : v === 'pscan' ? 'pscan-view' : 'add-view'
  ).classList.add('active');
  document.getElementById('pscan-btn').classList.toggle('active', v === 'pscan');
  document.getElementById('add-btn').classList.toggle('active', v === 'add');
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

/* ── Camera grid ───────────────────────────────────────────────────────────── */
function renderGrid() {
  const grid  = document.getElementById('cam-grid');
  const empty = document.getElementById('empty-state');
  const count = document.getElementById('cam-count');
  count.textContent = cameras.length
    ? cameras.length + ' device' + (cameras.length !== 1 ? 's' : '') + ' found'
    : '';
  empty.style.display = cameras.length ? 'none' : '';

  const existingIds = new Set([...grid.querySelectorAll('.camera-card')].map(c => c.dataset.id));
  const newIds      = new Set(cameras.map(c => c.id));
  existingIds.forEach(id => { if (!newIds.has(id)) grid.querySelector('[data-id="' + id + '"]')?.remove(); });

  cameras.forEach(cam => {
    if (existingIds.has(cam.id)) updateCard(cam);
    else                         grid.appendChild(buildCard(cam));
  });
  grid.querySelectorAll('video[data-hls]').forEach(v => { if (!v._hls) initHls(v); });
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
  if (c) c.innerHTML = cardHTML(cam);
}

function dotClass(cam) {
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

  if (d === 'proxy' && cam.status === 'ready')
    return '<img class="live" src="' + BASE + '/stream/' + cam.id + '" alt="Live" onerror="imgError(this)">'
         + '<div class="feed-placeholder" style="display:none">'
         + '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">'
         + '<path d="M15 10l4.553-2.069A1 1 0 0121 8.87v6.26a1 1 0 01-1.447.9L15 14"/>'
         + '<rect x="1" y="7" width="14" height="10" rx="2" ry="2"/></svg>'
         + '<span>Stream unavailable</span></div>';

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

  return '<div class="feed-wrap">' + feedHTML(cam) + '</div>'
    + '<div class="card-info">'
    + '<div class="status-dot ' + dotClass(cam) + '"></div>'
    + '<span class="card-name" title="' + name + '"'
    + ' onclick="openRename(\'' + cam.id + '\',\'' + name.replace(/'/g, "\\'") + '\')">'
    + name + '</span></div>'
    + '<div class="badges">' + protoBadge(cam.protocol)
    + '<span class="badge" style="background:#1e2d1e;color:#6fcf97">:' + cam.port + '</span>'
    + '<span class="badge" style="background:#2d2020;color:#e88">' + cam.ip + '</span>'
    + onvifBdg + credBdg + uncBdg + '</div>'
    + credFormHTML(cam)
    + '<div class="card-actions">' + clearBtn + notCamBtn
    + '<button class="btn btn-danger btn-sm" onclick="deleteCamera(\'' + cam.id + '\')">Remove</button>'
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

async function markNotCamera(cid) {
  const cam = cameras.find(c => c.id === cid);
  const label = cam ? cam.ip : cid;
  if (!confirm('Mark ' + label + ' as "not a camera"?\nThis IP will be permanently hidden from future scans.'))
    return;
  await fetch(BASE + '/api/cameras/' + cid + '/not_camera', {method: 'POST'});
  cameras = cameras.filter(c => c.id !== cid);
  renderGrid();
}

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
async function startPortScan() {
  const ip = document.getElementById('pscan-ip').value.trim();
  if (!ip) { alert('Enter an IP address.'); return; }
  const r = await fetch(BASE + '/api/pscan/start', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ip})
  });
  if (!r.ok) { const d = await r.json(); alert(d.error || 'Scan failed'); return; }
  document.getElementById('ps-start').disabled = true;
  document.getElementById('ps-pause').style.display  = '';
  document.getElementById('ps-cancel').style.display = '';
  document.getElementById('ps-prog-track').style.display = '';
  document.getElementById('port-table').style.display = 'none';
  document.getElementById('port-tbody').innerHTML = '';
  _paused = false;
  pollPscan();
}

async function pollPscan() {
  clearTimeout(pscanT);
  try {
    const s = await (await fetch(BASE + '/api/pscan/status')).json();
    document.getElementById('pscan-msg').textContent = s.message;
    document.getElementById('ps-prog-fill').style.width = s.progress + '%';
    if (s.results && s.results.length) renderPorts(s.results);
    if (s.running) {
      pscanT = setTimeout(pollPscan, 2000);
    } else {
      document.getElementById('ps-start').disabled  = false;
      document.getElementById('ps-pause').style.display  = 'none';
      document.getElementById('ps-cancel').style.display = 'none';
    }
  } catch { pscanT = setTimeout(pollPscan, 3000); }
}

function renderPorts(results) {
  document.getElementById('port-table').style.display = '';
  document.getElementById('port-tbody').innerHTML = results.map(p => {
    const ver = [p.product, p.version, p.extra].filter(Boolean).join(' ');
    const scripts = Object.entries(p.scripts || {})
      .map(([k,v]) => '<b>' + esc(k) + '</b>: ' + esc(v)).join('\n');
    return '<tr>'
      + '<td class="pnum">' + p.port + '</td>'
      + '<td>' + esc(p.proto) + '</td>'
      + '<td class="psvc">' + esc(p.service) + '</td>'
      + '<td>' + esc(ver) + '</td>'
      + '<td>' + (scripts ? '<div class="pscripts">' + scripts + '</div>' : '—') + '</td>'
      + '</tr>';
  }).join('');
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
#pscan-view{{padding:18px;gap:14px}}
.pscan-top{{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end}}
.pscan-top input{{background:var(--surface);border:1px solid var(--border);color:var(--text);
                  border-radius:8px;padding:7px 12px;font-size:.88rem;outline:none;flex:1;min-width:160px}}
.pscan-top input:focus{{border-color:var(--primary)}}
.pscan-ctl{{display:flex;gap:6px;flex-wrap:wrap}}
#pscan-msg{{font-size:.8rem;color:var(--text-dim);padding:4px 0}}
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
.modal-btns{{display:flex;gap:8px;justify-content:flex-end}}"""

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
    <input type="checkbox" id="broad-sweep"> Broad sweep
  </label>
  <button class="btn btn-primary"   id="scan-btn"  onclick="startScan()">&#x1F50D; Scan Network</button>
  <button class="btn btn-secondary" id="pscan-btn" onclick="switchView('pscan')">&#x1F50E; Port Scan</button>
  <button class="btn btn-secondary" id="add-btn"   onclick="switchView('add')">&#x2795; Connect Camera</button>
</header>

<div id="status-bar">
  <span id="stage-badge" class="stage-badge" style="display:none"></span>
  <span id="status-msg">Idle &#x2014; click Scan Network to start.</span>
  <div class="progress-track" id="progress-track" style="display:none">
    <div class="progress-fill" id="progress-fill"></div>
  </div>
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
  <div class="pscan-top">
    <input type="text" id="pscan-ip" placeholder="IP address (e.g. 192.168.1.100)"
           onkeydown="if(event.key==='Enter')startPortScan()"/>
    <div class="pscan-ctl">
      <button class="btn btn-primary"   id="ps-start"  onclick="startPortScan()">&#x25B6; Scan All Ports</button>
      <button class="btn btn-secondary" id="ps-pause"  onclick="togglePause()" style="display:none">&#x23F8; Pause</button>
      <button class="btn btn-danger"    id="ps-cancel" onclick="cancelPortScan()" style="display:none">&#x2715; Cancel</button>
      <button class="btn btn-ghost btn-sm" onclick="switchView('cameras')">&#x2190; Cameras</button>
    </div>
  </div>
  <div id="pscan-msg" style="padding:6px 0;font-size:.8rem;color:var(--text-dim)">Enter an IP and click Scan All Ports.</div>
  <div class="progress-track" id="ps-prog-track" style="display:none;max-width:100%;margin-bottom:6px">
    <div class="progress-fill" id="ps-prog-fill"></div>
  </div>
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

<script>
""" + js_code + """
</script>
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
    app.router.add_post(  "/api/cameras/{camera_id}/not_camera",  api_not_camera)
    app.router.add_delete("/api/cameras/{camera_id}",             api_delete_camera)
    app.router.add_post(  "/api/cameras/add",                     api_add_camera)
    app.router.add_get(   "/stream/{camera_id}",                  handle_stream)
    app.router.add_get(   "/snapshot/{camera_id}",                handle_snapshot)
    app.router.add_post(  "/api/pscan/start",                     api_pscan_start)
    app.router.add_get(   "/api/pscan/status",                    api_pscan_status)
    app.router.add_post(  "/api/pscan/cancel",                    api_pscan_cancel)
    app.router.add_post(  "/api/pscan/pause",                     api_pscan_pause)
    app.router.add_post(  "/api/pscan/resume",                    api_pscan_resume)
    return app


async def main():
    load_cameras()
    load_blacklist()
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"AnyCam on :{PORT}  ingress='{INGRESS_PATH}'")
    if not CAMERAS:
        log.info("No saved cameras — auto-starting scan")
        asyncio.create_task(run_scan())
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
