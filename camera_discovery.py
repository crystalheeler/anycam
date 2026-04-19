#!/usr/bin/env python3
"""
AnyCam — Home Assistant Add-on
Scans the local subnet for IP cameras across 6 protocols:
  RTSP · ONVIF · HTTP MJPEG · HLS · RTMP · WebRTC · RTSP-over-WebSocket
"""

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web
from cryptography.fernet import Fernet

log = logging.getLogger("cam_discovery")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

# ─────────────────────────────────────────────────────────────────────────────
# Paths & config
# ─────────────────────────────────────────────────────────────────────────────

DATA_DIR  = Path("/data")
KEY_FILE  = DATA_DIR / "secret.key"
CAMS_FILE = DATA_DIR / "cameras.json"

INGRESS_PATH = os.environ.get("INGRESS_PATH", "").rstrip("/")
PORT         = int(os.environ.get("INGRESS_PORT", 8099))

# ─────────────────────────────────────────────────────────────────────────────
# State
# ─────────────────────────────────────────────────────────────────────────────

CAMERAS    = {}
SCAN_STATE = {"running": False, "progress": 0, "message": "Idle. Click Scan to begin."}
_FERNET    = None

# ─────────────────────────────────────────────────────────────────────────────
# Protocol constants
# ─────────────────────────────────────────────────────────────────────────────

CAMERA_PORTS = [
    554, 8554, 10554,          # RTSP
    1935, 1936,                # RTMP
    80, 8080, 8000, 8888,      # HTTP (MJPEG / HLS / ONVIF)
    443, 8443,                 # HTTPS
    2020, 37777, 34567,        # DVR / proprietary
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

# WebRTC WHEP (WebRTC-HTTP Egress Protocol) and signaling paths
WEBRTC_PATHS = [
    "/whep", "/webrtc", "/api/webrtc", "/webrtc/offer",
    "/api/whep", "/offer", "/api/offer", "/webrtc/session",
    "/live/webrtc", "/stream/webrtc",
]

# RTSP-over-WebSocket paths (go2rtc, mediamtx, etc.)
WS_RTSP_PATHS = [
    "/api/ws", "/ws", "/stream/ws", "/live/ws",
    "/ws/stream", "/websocket", "/stream",
]


# ─────────────────────────────────────────────────────────────────────────────
# Encryption
# ─────────────────────────────────────────────────────────────────────────────

def get_fernet() -> Fernet:
    global _FERNET
    if _FERNET:
        return _FERNET
    DATA_DIR.mkdir(exist_ok=True)
    if KEY_FILE.exists():
        key = KEY_FILE.read_bytes()
    else:
        key = Fernet.generate_key()
        KEY_FILE.write_bytes(key)
        KEY_FILE.chmod(0o600)
        log.info("Generated new encryption key")
    _FERNET = Fernet(key)
    return _FERNET


def encrypt_creds(username: str, password: str) -> str:
    data = json.dumps({"u": username, "p": password}).encode()
    return get_fernet().encrypt(data).decode()


def decrypt_creds(token: str) -> tuple[str, str]:
    data = json.loads(get_fernet().decrypt(token.encode()).decode())
    return data["u"], data["p"]


# ─────────────────────────────────────────────────────────────────────────────
# Persistent camera store
# ─────────────────────────────────────────────────────────────────────────────

def load_cameras():
    if not CAMS_FILE.exists():
        return
    try:
        for cam in json.loads(CAMS_FILE.read_text()):
            CAMERAS[cam["id"]] = cam
        log.info(f"Loaded {len(CAMERAS)} saved camera(s)")
    except Exception as e:
        log.warning(f"Failed to load cameras: {e}")


def save_cameras():
    DATA_DIR.mkdir(exist_ok=True)
    safe = []
    for cam in CAMERAS.values():
        s = dict(cam)
        if s.get("credentials") and s.get("stream_url"):
            s["stream_url"] = _strip_creds_from_url(s["stream_url"])
        safe.append(s)
    CAMS_FILE.write_text(json.dumps(safe, indent=2))


def _strip_creds_from_url(url: str) -> str:
    if not url:
        return url
    return re.sub(r"(://)[^@]+@", r"\1", url)


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
        log.warning(f"Subnet detection error: {e}")
    return "192.168.1.0/24"


# ─────────────────────────────────────────────────────────────────────────────
# Protocol probers
# ─────────────────────────────────────────────────────────────────────────────

# ── RTSP ─────────────────────────────────────────────────────────────────────

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
    except (subprocess.TimeoutExpired, Exception):
        return False


def find_rtsp_path(ip: str, port: int,
                   username: str = "", password: str = "") -> str | None:
    for path in RTSP_PATHS:
        url = f"rtsp://{ip}:{port}{path}"
        if probe_rtsp(url, username, password):
            log.info(f"  RTSP OK: {url}")
            return url
    return None


# ── HTTP MJPEG ────────────────────────────────────────────────────────────────

def probe_mjpeg_http(ip: str, port: int, username: str = "",
                     password: str = "", timeout: int = 4) -> str | None:
    """
    Try MJPEG paths via HTTP GET.  Accepts Content-Type of
    multipart/x-mixed-replace or image/jpeg (looping snapshot endpoints).
    Returns the working URL or None.
    """
    import urllib.request
    import urllib.error
    import base64

    scheme = "https" if port in (443, 8443) else "http"
    auth_header = None
    if username:
        cred = base64.b64encode(f"{username}:{password}".encode()).decode()
        auth_header = f"Basic {cred}"

    for path in MJPEG_PATHS:
        url = f"{scheme}://{ip}:{port}{path}"
        try:
            req = urllib.request.Request(url)
            if auth_header:
                req.add_header("Authorization", auth_header)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ct = resp.headers.get("Content-Type", "")
                if any(k in ct.lower() for k in
                       ("multipart/x-mixed-replace", "image/jpeg",
                        "image/jpg", "mjpeg", "mjpg")):
                    log.info(f"  MJPEG OK: {url}")
                    return url
        except Exception:
            pass
    return None


# ── HLS ───────────────────────────────────────────────────────────────────────

def probe_hls(ip: str, port: int, username: str = "",
              password: str = "", timeout: int = 4) -> str | None:
    """
    Try HLS paths, looking for an M3U8 playlist (Content-Type or
    #EXTM3U body prefix).  Returns the working URL or None.
    """
    import urllib.request
    import urllib.error
    import base64

    scheme = "https" if port in (443, 8443) else "http"
    auth_header = None
    if username:
        cred = base64.b64encode(f"{username}:{password}".encode()).decode()
        auth_header = f"Basic {cred}"

    for path in HLS_PATHS:
        url = f"{scheme}://{ip}:{port}{path}"
        try:
            req = urllib.request.Request(url)
            if auth_header:
                req.add_header("Authorization", auth_header)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ct         = resp.headers.get("Content-Type", "")
                body_start = resp.read(64).decode("utf-8", errors="replace")
                if ("m3u8" in ct.lower() or "mpegurl" in ct.lower() or
                        body_start.strip().startswith("#EXTM3U")):
                    log.info(f"  HLS OK: {url}")
                    return url
        except Exception:
            pass
    return None


# ── RTMP ──────────────────────────────────────────────────────────────────────

def probe_rtmp(ip: str, port: int, timeout: int = 3) -> bool:
    """
    TCP connect and verify the RTMP server handshake byte.
    RTMP S0 is 0x03 (plain) or 0x06 (RTMPE).
    """
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            sock.sendall(b"\x03")          # C0 handshake
            sock.settimeout(timeout)
            data = sock.recv(4)
            if data and data[0] in (0x03, 0x06):
                log.info(f"  RTMP OK: {ip}:{port}")
                return True
    except Exception:
        pass
    return False


def build_rtmp_url(ip: str, port: int) -> str:
    # Generic live stream path — most RTMP servers use /live/stream
    return f"rtmp://{ip}:{port}/live/stream"


# ── WebRTC ────────────────────────────────────────────────────────────────────

def probe_webrtc(ip: str, port: int, timeout: int = 4) -> str | None:
    """
    Best-effort WebRTC signaling detection.

    Strategy 1 — WHEP (WebRTC-HTTP Egress Protocol, draft-ietf-wish-whep):
      POST a minimal SDP offer with Content-Type: application/sdp and look
      for a 200/201 with an SDP answer in the response body.

    Strategy 2 — heuristic GET:
      Look for "webrtc", "ice", "sdp", "whep" in the response Content-Type,
      Server header, or first 256 bytes of body.

    We do NOT complete the full ICE/DTLS exchange — just flag the endpoint.
    Real streaming via WebRTC requires a dedicated client-side implementation.
    Returns the detected URL path or None.
    """
    import urllib.request
    import urllib.error

    scheme = "https" if port in (443, 8443) else "http"

    # Minimal SDP offer for WHEP probing
    sdp_offer = (
        "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\n"
        "t=0 0\r\na=group:BUNDLE 0\r\n"
        "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
        "c=IN IP4 0.0.0.0\r\na=sendrecv\r\n"
        "a=rtpmap:96 H264/90000\r\n"
    )

    for path in WEBRTC_PATHS:
        url = f"{scheme}://{ip}:{port}{path}"

        # Strategy 1: WHEP POST
        try:
            req = urllib.request.Request(url, data=sdp_offer.encode(), method="POST")
            req.add_header("Content-Type", "application/sdp")
            req.add_header("Accept",       "application/sdp")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ct = resp.headers.get("Content-Type", "")
                if resp.status in (200, 201) and "sdp" in ct.lower():
                    log.info(f"  WebRTC WHEP: {url}")
                    return url
        except urllib.error.HTTPError as e:
            ct = e.headers.get("Content-Type", "")
            if any(h in ct.lower() for h in ("sdp", "webrtc", "ice")):
                log.info(f"  WebRTC signaling (HTTP {e.code}): {url}")
                return url
        except Exception:
            pass

        # Strategy 2: heuristic GET
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "application/json, application/sdp, */*")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ct   = resp.headers.get("Content-Type", "")
                srv  = resp.headers.get("Server", "")
                body = resp.read(256).decode("utf-8", errors="replace")
                if any(m in (ct + srv + body).lower()
                       for m in ("webrtc", "ice", "sdp", "whep", "offer")):
                    log.info(f"  WebRTC heuristic GET: {url}")
                    return url
        except Exception:
            pass

    return None


# ── RTSP-over-WebSocket ───────────────────────────────────────────────────────

def probe_ws_rtsp(ip: str, port: int, timeout: int = 4) -> str | None:
    """
    Attempt a raw WebSocket upgrade with Sec-WebSocket-Protocol: rtsp.
    A 101 Switching Protocols response confirms the endpoint exists.

    Compatible with: go2rtc (/api/ws), mediamtx WebSocket output,
    and similar RTSP-over-WebSocket implementations.

    We stop after the upgrade — no RTSP DESCRIBE or RTP frames are
    exchanged.  Full WS-RTSP playback support is planned for a future
    release; for now the card shows the detected endpoint URL.
    """
    scheme_ws = "wss" if port in (443, 8443) else "ws"
    key_b64   = "dGhlIHNhbXBsZSBub25jZQ=="   # canonical test key from RFC 6455

    for path in WS_RTSP_PATHS:
        try:
            with socket.create_connection((ip, port), timeout=timeout) as sock:
                handshake = (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: {ip}:{port}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key_b64}\r\n"
                    "Sec-WebSocket-Version: 13\r\n"
                    "Sec-WebSocket-Protocol: rtsp\r\n"
                    "\r\n"
                ).encode()
                sock.sendall(handshake)
                sock.settimeout(timeout)
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = sock.recv(1024)
                    if not chunk:
                        break
                    buf += chunk
                resp_text = buf.decode("utf-8", errors="replace")
                if "101" in resp_text and "websocket" in resp_text.lower():
                    ws_url = f"{scheme_ws}://{ip}:{port}{path}"
                    log.info(f"  WS-RTSP: {ws_url}")
                    return ws_url
        except Exception:
            pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
# ONVIF WS-Discovery
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
    results = []
    seen    = set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        sock.settimeout(timeout)
        msg = _WS_PROBE.replace("{mid}", str(uuid.uuid4())).encode()
        sock.sendto(msg, ("239.255.255.250", 3702))
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
                results.append({
                    "ip":     ip,
                    "name":   name,
                    "xaddrs": xaddrs[0].strip() if xaddrs else "",
                })
                log.info(f"ONVIF: {name} @ {ip}")
            except socket.timeout:
                break
        sock.close()
    except Exception as e:
        log.warning(f"WS-Discovery error: {e}")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# nmap scan
# ─────────────────────────────────────────────────────────────────────────────

def nmap_scan(subnet: str) -> list[dict]:
    ports_str = ",".join(str(p) for p in CAMERA_PORTS)
    log.info(f"nmap {subnet} ports {ports_str}")
    try:
        r = subprocess.run(
            ["nmap", "-sV", "--open", "-p", ports_str,
             "--host-timeout", "30s", "-T4", "-oX", "-", subnet],
            capture_output=True, text=True, timeout=360,
        )
        hosts = _parse_nmap_xml(r.stdout)
        log.info(f"nmap: {len(hosts)} host(s) with open ports")
        return hosts
    except subprocess.TimeoutExpired:
        log.warning("nmap timed out")
        return []
    except Exception as e:
        log.warning(f"nmap error: {e}")
        return []


def _parse_nmap_xml(xml_text: str) -> list[dict]:
    results = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return results
    for host in root.findall("host"):
        status = host.find("status")
        if status is None or status.get("state") != "up":
            continue
        addr_el = host.find("address[@addrtype='ipv4']")
        if addr_el is None:
            continue
        ip = addr_el.get("addr")
        hn = host.find("hostnames/hostname")
        hostname   = hn.get("name", ip) if hn is not None else ip
        open_ports = []
        for p in host.findall("ports/port"):
            st = p.find("state")
            if st is None or st.get("state") != "open":
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
    combined = (service + " " + product).lower()
    if port in (554, 8554, 10554, 2020):
        return "RTSP"
    if port in (1935, 1936):
        return "RTMP"
    if port in (80, 8080, 8000, 8888, 443, 8443):
        if "rtsp" in combined or "camera" in combined:
            return "RTSP"
        return "HTTP"
    if port in (37777, 34567):
        return "DVR"
    return service.upper() or "UNKNOWN"


# ─────────────────────────────────────────────────────────────────────────────
# Scan orchestration
# ─────────────────────────────────────────────────────────────────────────────

async def run_scan():
    SCAN_STATE.update(running=True, progress=0,
                      message="Detecting local subnet…")
    loop = asyncio.get_event_loop()

    subnet = await loop.run_in_executor(None, get_local_subnet)
    log.info(f"Subnet: {subnet}")
    SCAN_STATE.update(progress=5,
                      message=f"Scanning {subnet} — may take 60–90 seconds…")

    nmap_task  = loop.run_in_executor(None, nmap_scan, subnet)
    onvif_task = loop.run_in_executor(None, onvif_discover, 5)
    nmap_results, onvif_results = await asyncio.gather(nmap_task, onvif_task)

    SCAN_STATE.update(progress=55,
                      message=f"Found {len(nmap_results)} host(s). Probing streams…")

    saved = {cid: c for cid, c in CAMERAS.items() if c.get("user_saved")}
    CAMERAS.clear()
    CAMERAS.update(saved)

    total = max(len(nmap_results), 1)
    for idx, host in enumerate(nmap_results):
        ip, hostname = host["ip"], host["hostname"]
        SCAN_STATE.update(progress=55 + int(35 * idx / total),
                          message=f"Probing {ip}…")
        for port_info in host["open_ports"]:
            port    = port_info["port"]
            initial = _initial_protocol(port, port_info["service"],
                                        port_info["product"])
            prev    = saved.get(f"{ip}_{port}", {})
            cam     = await _probe_host_port(ip, port, hostname, initial, prev, loop)
            if cam:
                CAMERAS[cam["id"]] = cam

    # ── Merge ONVIF ───────────────────────────────────────────────────────────
    for onvif in onvif_results:
        ip       = onvif["ip"]
        existing = [c for c in CAMERAS.values() if c["ip"] == ip]
        if existing:
            for cam in existing:
                cam["onvif"] = True
                if onvif.get("xaddrs"):
                    cam["xaddrs"] = onvif["xaddrs"]
                if cam["protocol"] == "HTTP":
                    cam["protocol"] = "ONVIF"
        else:
            cid  = f"{ip}_onvif"
            prev = saved.get(cid, {})
            CAMERAS[cid] = {
                "id": cid, "ip": ip, "hostname": onvif["name"],
                "port": 80, "protocol": "ONVIF",
                "stream_url":           prev.get("stream_url", ""),
                "requires_credentials": True,
                "credentials":          prev.get("credentials"),
                "name":                 prev.get("name", onvif["name"]),
                "xaddrs":               onvif.get("xaddrs", ""),
                "status":               "needs_credentials",
                "onvif": True, "user_saved": bool(prev), "display": "proxy",
            }

    save_cameras()
    ready = sum(1 for c in CAMERAS.values() if c["status"] == "ready")
    SCAN_STATE.update(
        running=False, progress=100,
        message=(f"Scan complete — {len(CAMERAS)} camera(s) found, "
                 f"{ready} ready.")
    )
    log.info(SCAN_STATE["message"])


async def _probe_host_port(ip, port, hostname, initial_protocol,
                           prev, loop) -> dict | None:
    """
    Try all relevant protocols for a given host:port.
    Returns a camera dict for the first protocol that responds,
    or a needs_credentials placeholder for RTSP/DVR/HTTP.
    """
    cid        = f"{ip}_{port}"
    prev_creds = prev.get("credentials")
    prev_name  = prev.get("name", hostname)
    saved_u = saved_p = ""
    if prev_creds:
        try:
            saved_u, saved_p = decrypt_creds(prev_creds)
        except Exception:
            pass

    def base(proto, url, status, display="proxy"):
        return {
            "id": cid, "ip": ip, "hostname": hostname, "port": port,
            "protocol": proto, "stream_url": url,
            "requires_credentials": False, "credentials": None,
            "name": prev_name, "status": status,
            "user_saved": bool(prev), "display": display,
        }

    # ── RTSP / DVR ────────────────────────────────────────────────────────────
    if initial_protocol in ("RTSP", "DVR"):
        url = await loop.run_in_executor(None, find_rtsp_path, ip, port)
        if url:
            return base("RTSP", url, "ready")
        if saved_u:
            url = await loop.run_in_executor(
                None, find_rtsp_path, ip, port, saved_u, saved_p)
            if url:
                cam = base("RTSP", url, "ready")
                cam["credentials"] = prev_creds
                return cam
        cam = base("RTSP", "", "needs_credentials")
        cam["requires_credentials"] = True
        return cam

    # ── RTMP ──────────────────────────────────────────────────────────────────
    if initial_protocol == "RTMP" or port in (1935, 1936):
        ok = await loop.run_in_executor(None, probe_rtmp, ip, port)
        if ok:
            return base("RTMP", build_rtmp_url(ip, port), "ready")

    # ── HTTP — try MJPEG → HLS → RTSP-on-HTTP → WebRTC → WS-RTSP ────────────
    if initial_protocol in ("HTTP", "ONVIF", "UNKNOWN"):

        # MJPEG (no creds)
        url = await loop.run_in_executor(None, probe_mjpeg_http, ip, port)
        if url:
            return base("MJPEG", url, "ready")
        # MJPEG (saved creds)
        if saved_u:
            url = await loop.run_in_executor(
                None, probe_mjpeg_http, ip, port, saved_u, saved_p)
            if url:
                cam = base("MJPEG", url, "ready")
                cam["credentials"] = prev_creds
                return cam

        # HLS (no creds)
        url = await loop.run_in_executor(None, probe_hls, ip, port)
        if url:
            return base("HLS", url, "ready", "hls")
        # HLS (saved creds)
        if saved_u:
            url = await loop.run_in_executor(
                None, probe_hls, ip, port, saved_u, saved_p)
            if url:
                cam = base("HLS", url, "ready", "hls")
                cam["credentials"] = prev_creds
                return cam

        # RTSP on an HTTP port (some cameras)
        url = await loop.run_in_executor(None, find_rtsp_path, ip, port)
        if url:
            return base("RTSP", url, "ready")

        # WebRTC — detection only, no proxy stream
        wrtc = await loop.run_in_executor(None, probe_webrtc, ip, port)
        if wrtc:
            cam = base("WebRTC", wrtc, "info", "webrtc")
            cam["info"] = (
                "WebRTC signaling endpoint detected. "
                "Direct browser-to-camera negotiation is required — "
                "this protocol cannot be proxied generically. "
                "Click the link below to open the signaling endpoint directly."
            )
            cam["signaling_url"] = wrtc
            return cam

        # WS-RTSP — detection only, no proxy stream (yet)
        ws = await loop.run_in_executor(None, probe_ws_rtsp, ip, port)
        if ws:
            cam = base("WS-RTSP", ws, "info", "wsrtsp")
            cam["info"] = (
                "RTSP-over-WebSocket endpoint detected (go2rtc / mediamtx style). "
                "In-browser WS-RTSP playback is planned for a future release. "
                "Use go2rtc or a compatible player to connect directly."
            )
            cam["ws_url"] = ws
            return cam

        # HTTP device found but no recognisable stream — prompt for creds
        cam = base("HTTP", "", "needs_credentials")
        cam["requires_credentials"] = True
        return cam

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Stream URL builder (injects decrypted credentials)
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
            log.warning(f"Cred decrypt error: {e}")
    return url


# ─────────────────────────────────────────────────────────────────────────────
# Stream handler — RTSP / RTMP / MJPEG / HLS all proxied to MJPEG for browser
# ─────────────────────────────────────────────────────────────────────────────

async def handle_stream(request: web.Request) -> web.StreamResponse:
    camera_id = request.match_info["camera_id"]
    camera    = CAMERAS.get(camera_id)
    if not camera:
        return web.Response(status=404, text="Camera not found")
    if camera.get("display") in ("webrtc", "wsrtsp", "info"):
        return web.Response(status=400, text="Protocol not proxy-streamable")

    url = build_authenticated_url(camera)
    if not url:
        return web.Response(status=503, text="No stream URL")

    proto       = camera.get("protocol", "RTSP")
    input_flags = []
    if proto in ("RTSP", "DVR"):
        input_flags = ["-rtsp_transport", "tcp"]
    elif proto == "HLS":
        input_flags = ["-re"]

    response = web.StreamResponse(headers={
        "Content-Type":  "multipart/x-mixed-replace; boundary=frame",
        "Cache-Control": "no-cache",
        "Pragma":        "no-cache",
        "Connection":    "keep-alive",
    })
    await response.prepare(request)

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error",
        *input_flags, "-i", url,
        "-vf", "fps=10,scale=640:-2",
        "-q:v", "5", "-f", "mjpeg", "pipe:1",
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
        log.warning(f"Stream timeout: {camera_id}")
    except Exception as ex:
        log.warning(f"Stream error [{camera_id}]: {ex}")
    finally:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        log.info(f"Stream ended: {camera_id}")

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
            "ffmpeg", "-loglevel", "error",
            *extra, "-i", url,
            "-vframes", "1", "-q:v", "3", "-f", "image2", "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
        if stdout:
            return web.Response(body=stdout, content_type="image/jpeg")
    except Exception as ex:
        log.warning(f"Snapshot error: {ex}")
    return web.Response(status=503)


# ─────────────────────────────────────────────────────────────────────────────
# REST API
# ─────────────────────────────────────────────────────────────────────────────

def _safe_cam(cam: dict) -> dict:
    s = dict(cam)
    if s.get("stream_url"):
        s["stream_url"] = _strip_creds_from_url(s["stream_url"])
    s["has_credentials"] = bool(s.get("credentials"))
    s.pop("credentials", None)
    return s


async def api_cameras(request):
    return web.json_response([_safe_cam(c) for c in CAMERAS.values()])

async def api_scan(request):
    if SCAN_STATE["running"]:
        return web.json_response({"error": "Scan already running"}, status=409)
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

    if proto in ("RTSP", "DVR", "ONVIF"):
        url = await loop.run_in_executor(
            None, find_rtsp_path, camera["ip"], camera["port"], username, password)
        if not url and camera.get("xaddrs"):
            parsed = urlparse(camera["xaddrs"])
            url = await loop.run_in_executor(
                None, find_rtsp_path,
                parsed.hostname or camera["ip"],
                parsed.port or 554, username, password)
    elif proto == "MJPEG":
        url = await loop.run_in_executor(
            None, probe_mjpeg_http, camera["ip"], camera["port"], username, password)
    elif proto == "HLS":
        url = await loop.run_in_executor(
            None, probe_hls, camera["ip"], camera["port"], username, password)

    if not url:
        return web.json_response(
            {"error": "Could not connect with those credentials. "
                      "Check username/password and try again."},
            status=401)

    camera.update(
        credentials          = encrypt_creds(username, password),
        stream_url           = url,
        requires_credentials = False,
        status               = "ready",
        user_saved           = True,
    )
    save_cameras()
    return web.json_response({"status": "ok",
                               "stream_url": _strip_creds_from_url(url)})


async def api_clear_credentials(request):
    cid    = request.match_info["camera_id"]
    camera = CAMERAS.get(cid)
    if not camera:
        return web.json_response({"error": "Not found"}, status=404)
    camera.update(
        credentials          = None,
        stream_url           = _strip_creds_from_url(camera.get("stream_url", "")),
        requires_credentials = True,
        status               = "needs_credentials",
    )
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


# ─────────────────────────────────────────────────────────────────────────────
# Main HTML
# ─────────────────────────────────────────────────────────────────────────────

def build_html() -> str:
    base = INGRESS_PATH
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AnyCam</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.7/dist/hls.min.js"></script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#111318;--surface:#1e2029;--surface2:#272a35;--border:#2e3140;
  --primary:#5b8af5;--primary-dim:#3a5dbf;--green:#4caf7d;--yellow:#f5b942;
  --red:#e05c5c;--blue:#5bc4f5;--text:#e4e6f0;--text-dim:#8a8fa8;
  --radius:12px;--card-w:320px;
}}
body{{background:var(--bg);color:var(--text);
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;min-height:100vh}}
header{{background:var(--surface);border-bottom:1px solid var(--border);
        padding:16px 24px;display:flex;align-items:center;gap:16px;
        flex-wrap:wrap;position:sticky;top:0;z-index:10}}
header h1{{font-size:1.1rem;font-weight:600;display:flex;align-items:center;gap:10px;flex:1}}
.btn{{padding:8px 18px;border-radius:8px;border:none;cursor:pointer;font-size:.875rem;
      font-weight:600;transition:opacity .15s,background .15s;white-space:nowrap}}
.btn:disabled{{opacity:.4;cursor:default}}
.btn-primary{{background:var(--primary);color:#fff}}
.btn-primary:not(:disabled):hover{{background:var(--primary-dim)}}
.btn-danger{{background:var(--red);color:#fff}}
.btn-ghost{{background:var(--surface2);color:var(--text)}}
.btn-sm{{padding:5px 12px;font-size:.8rem}}
#status-bar{{padding:10px 24px;font-size:.82rem;color:var(--text-dim);
             display:flex;align-items:center;gap:12px;border-bottom:1px solid var(--border)}}
.progress-track{{flex:1;height:4px;background:var(--surface2);border-radius:2px;
                 overflow:hidden;max-width:240px}}
.progress-fill{{height:100%;background:var(--primary);border-radius:2px;
                transition:width .4s ease;width:0%}}
main{{padding:24px;display:grid;
      grid-template-columns:repeat(auto-fill,minmax(var(--card-w),1fr));gap:20px}}
#empty-state{{grid-column:1/-1;text-align:center;padding:80px 24px;color:var(--text-dim)}}
#empty-state svg{{opacity:.25;display:block;margin:0 auto 16px}}
.camera-card{{background:var(--surface);border:1px solid var(--border);
              border-radius:var(--radius);overflow:hidden;display:flex;
              flex-direction:column;transition:box-shadow .2s}}
.camera-card:hover{{box-shadow:0 4px 24px rgba(0,0,0,.5)}}
.feed-wrap{{position:relative;width:100%;aspect-ratio:16/9;background:#000;
            display:flex;align-items:center;justify-content:center;overflow:hidden}}
.feed-wrap img,.feed-wrap video{{width:100%;height:100%;object-fit:cover;display:block}}
.feed-placeholder{{display:flex;flex-direction:column;align-items:center;gap:8px;
                   color:var(--text-dim);font-size:.82rem;text-align:center;padding:12px}}
.feed-placeholder svg{{opacity:.3}}
.info-overlay{{position:absolute;inset:0;background:rgba(10,12,18,.88);
               display:flex;flex-direction:column;align-items:center;
               justify-content:center;gap:10px;padding:16px;text-align:center}}
.info-overlay .proto-icon{{font-size:2rem}}
.info-overlay p{{font-size:.78rem;color:var(--text-dim);line-height:1.5}}
.info-overlay a{{color:var(--primary);font-size:.8rem}}
.info-overlay code{{font-size:.7rem;color:var(--text-dim);word-break:break-all;
                    background:var(--surface2);padding:4px 8px;border-radius:4px}}
.card-info{{padding:12px 14px 8px;display:flex;align-items:flex-start;gap:10px}}
.card-info .name{{font-size:.9rem;font-weight:600;flex:1;white-space:nowrap;
                  overflow:hidden;text-overflow:ellipsis;cursor:pointer}}
.card-info .name:hover{{color:var(--primary)}}
.badges{{padding:0 14px 10px;display:flex;flex-wrap:wrap;gap:6px}}
.badge{{font-size:.72rem;font-weight:600;padding:2px 8px;border-radius:20px;letter-spacing:.03em}}
.status-dot{{width:9px;height:9px;border-radius:50%;flex-shrink:0;margin-top:4px}}
.dot-ready{{background:var(--green);box-shadow:0 0 6px var(--green)}}
.dot-warning{{background:var(--yellow);box-shadow:0 0 6px var(--yellow)}}
.dot-info{{background:var(--blue);box-shadow:0 0 6px var(--blue)}}
.dot-error{{background:var(--red);box-shadow:0 0 6px var(--red)}}
.cred-form{{margin:0 14px 12px;background:var(--surface2);border:1px solid var(--border);
            border-radius:8px;padding:12px;display:flex;flex-direction:column;gap:8px}}
.cred-form label{{font-size:.75rem;color:var(--text-dim);font-weight:600;letter-spacing:.04em}}
.cred-form input{{width:100%;background:var(--bg);border:1px solid var(--border);
                  border-radius:6px;color:var(--text);font-size:.875rem;
                  padding:6px 10px;outline:none;transition:border-color .15s}}
.cred-form input:focus{{border-color:var(--primary)}}
.cred-row{{display:flex;gap:6px}}
.cred-row .btn{{flex:1}}
.cred-error{{font-size:.75rem;color:var(--red);display:none}}
.cred-error.visible{{display:block}}
.card-actions{{padding:0 14px 12px;display:flex;gap:6px;margin-top:auto}}
.modal-backdrop{{position:fixed;inset:0;background:rgba(0,0,0,.7);
                 display:none;align-items:center;justify-content:center;z-index:100}}
.modal-backdrop.open{{display:flex}}
.modal{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
        padding:24px;width:min(400px,90vw);display:flex;flex-direction:column;gap:14px}}
.modal h3{{font-size:1rem;font-weight:600}}
.modal input{{width:100%;background:var(--bg);border:1px solid var(--border);
              border-radius:8px;color:var(--text);font-size:.9rem;padding:8px 12px;outline:none}}
.modal input:focus{{border-color:var(--primary)}}
.modal .modal-btns{{display:flex;gap:10px;justify-content:flex-end}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
.scanning .progress-fill{{animation:pulse 1.2s infinite}}
</style>
</head>
<body>

<header>
  <h1>
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M15 10l4.553-2.069A1 1 0 0121 8.87v6.26a1 1 0 01-1.447.9L15 14"/>
      <rect x="1" y="7" width="14" height="10" rx="2" ry="2"/>
    </svg>
    AnyCam
  </h1>
  <span id="cam-count" style="color:var(--text-dim);font-size:.85rem"></span>
  <button class="btn btn-primary" id="scan-btn" onclick="startScan()">🔍 Scan Network</button>
</header>

<div id="status-bar">
  <span id="status-msg">Idle — click Scan Network to discover cameras.</span>
  <div class="progress-track" id="progress-track" style="display:none">
    <div class="progress-fill" id="progress-fill"></div>
  </div>
</div>

<main id="cam-grid">
  <div id="empty-state">
    <svg width="72" height="72" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
      <path d="M15 10l4.553-2.069A1 1 0 0121 8.87v6.26a1 1 0 01-1.447.9L15 14"/>
      <rect x="1" y="7" width="14" height="10" rx="2" ry="2"/>
    </svg>
    <p>No cameras found yet.<br>Click <strong>Scan Network</strong> to discover cameras on your subnet.</p>
  </div>
</main>

<div class="modal-backdrop" id="rename-modal">
  <div class="modal">
    <h3>Rename Camera</h3>
    <input type="text" id="rename-input" placeholder="Camera name"/>
    <div class="modal-btns">
      <button class="btn btn-ghost" onclick="closeRename()">Cancel</button>
      <button class="btn btn-primary" onclick="submitRename()">Save</button>
    </div>
  </div>
</div>

<script>
const BASE = '{base}';
const PROTO_ICONS   = {{RTSP:'📹',ONVIF:'🔭',MJPEG:'🖼️',HLS:'📡',RTMP:'📺',WebRTC:'🔗','WS-RTSP':'🔌',HTTP:'🌐',DVR:'💾'}};
const PROTO_COLORS  = {{
  RTSP:['1e3a5f','79b8ff'],ONVIF:['2d1e4a','c09eff'],MJPEG:['1e3a30','79ffcd'],
  HLS:['3a2e1e','ffb879'],RTMP:['3a1e1e','ff7979'],WebRTC:['1e2d3a','79d4ff'],
  'WS-RTSP':['2d3a1e','b8ff79'],HTTP:['2a2a2a','aaaaaa'],DVR:['3a1e3a','ff79ff']
}};

let cameras=[], pollTimer=null, renameCameraId=null;

async function startScan(){{
  await fetch(BASE+'/api/scan',{{method:'POST'}});
  document.getElementById('scan-btn').disabled=true;
  document.getElementById('progress-track').style.display='';
  document.getElementById('status-bar').classList.add('scanning');
  pollStatus();
}}

async function pollStatus(){{
  clearTimeout(pollTimer);
  try{{
    const s=await(await fetch(BASE+'/api/scan/status')).json();
    document.getElementById('status-msg').textContent=s.message;
    document.getElementById('progress-fill').style.width=s.progress+'%';
    if(s.running){{pollTimer=setTimeout(pollStatus,1500);}}
    else{{
      document.getElementById('scan-btn').disabled=false;
      document.getElementById('status-bar').classList.remove('scanning');
      if(s.progress>=100){{
        document.getElementById('progress-track').style.display='none';
        await loadCameras();
      }}
    }}
  }}catch(e){{pollTimer=setTimeout(pollStatus,3000);}}
}}

async function loadCameras(){{
  cameras=await(await fetch(BASE+'/api/cameras')).json();
  renderGrid();
}}

function renderGrid(){{
  const grid=document.getElementById('cam-grid');
  const empty=document.getElementById('empty-state');
  document.getElementById('cam-count').textContent=
    cameras.length?cameras.length+' camera'+(cameras.length!==1?'s':'')+' found':'';
  empty.style.display=cameras.length?'none':'';
  const existingIds=new Set([...grid.querySelectorAll('.camera-card')].map(c=>c.dataset.id));
  const newIds=new Set(cameras.map(c=>c.id));
  existingIds.forEach(id=>{{if(!newIds.has(id))grid.querySelector(`[data-id="${{id}}"]`)?.remove();}});
  cameras.forEach(cam=>{{
    if(existingIds.has(cam.id)) updateCard(cam);
    else grid.appendChild(buildCard(cam));
  }});
  grid.querySelectorAll('video[data-hls-url]').forEach(v=>{{if(!v._hlsInit)initHls(v);}});
}}

function buildCard(cam){{
  const d=document.createElement('div');
  d.className='camera-card';d.dataset.id=cam.id;
  d.innerHTML=cardHTML(cam);return d;
}}
function updateCard(cam){{
  const c=document.querySelector(`[data-id="${{cam.id}}"]`);
  if(c)c.innerHTML=cardHTML(cam);
}}

function dotClass(cam){{
  if(cam.status==='ready') return 'dot-ready';
  if(cam.status==='info')  return 'dot-info';
  if(cam.status==='needs_credentials') return 'dot-warning';
  return 'dot-error';
}}

function protoBadge(proto){{
  const [bg,fg]=(PROTO_COLORS[proto]||['2a2a2a','aaaaaa']);
  return `<span class="badge" style="background:#${{bg}};color:#${{fg}}">${{PROTO_ICONS[proto]||''}} ${{proto}}</span>`;
}}

function feedHTML(cam){{
  const d=cam.display||'proxy';
  if(d==='proxy'&&cam.status==='ready')
    return `<img class="live" src="${{BASE}}/stream/${{cam.id}}" alt="Live feed"
              onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
            <div class="feed-placeholder" style="display:none">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <path d="M15 10l4.553-2.069A1 1 0 0121 8.87v6.26a1 1 0 01-1.447.9L15 14"/>
                <rect x="1" y="7" width="14" height="10" rx="2" ry="2"/>
              </svg><span>Stream unavailable</span></div>`;
  if(d==='hls'&&cam.status==='ready')
    return `<video data-hls-url="${{esc(cam.stream_url)}}" autoplay muted playsinline></video>`;
  if(d==='webrtc')
    return `<div class="info-overlay">
      <div class="proto-icon">🔗</div><strong>WebRTC Detected</strong>
      <p>${{esc(cam.info||'')}}</p>
      <a href="${{esc(cam.signaling_url||'#')}}" target="_blank" rel="noopener">Open signaling endpoint ↗</a>
    </div>`;
  if(d==='wsrtsp')
    return `<div class="info-overlay">
      <div class="proto-icon">🔌</div><strong>WS-RTSP Detected</strong>
      <p>${{esc(cam.info||'')}}</p>
      <code>${{esc(cam.ws_url||'')}}</code>
    </div>`;
  const icon=cam.status==='needs_credentials'
    ?`<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
        <path d="M7 11V7a5 5 0 0110 0v4"/>
      </svg><span>Credentials required</span>`
    :`<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M15 10l4.553-2.069A1 1 0 0121 8.87v6.26a1 1 0 01-1.447.9L15 14"/>
        <rect x="1" y="7" width="14" height="10" rx="2" ry="2"/>
      </svg><span>No stream detected</span>`;
  return `<div class="feed-placeholder">${{icon}}</div>`;
}}

function credFormHTML(cam){{
  if(cam.status!=='needs_credentials'||['webrtc','wsrtsp'].includes(cam.display)) return '';
  return `<div class="cred-form">
    <label>USERNAME</label>
    <input type="text" id="u_${{cam.id}}" placeholder="admin" autocomplete="username">
    <label>PASSWORD</label>
    <input type="password" id="p_${{cam.id}}" placeholder="••••••••"
           autocomplete="current-password"
           onkeydown="if(event.key==='Enter')submitCreds('${{cam.id}}')">
    <div class="cred-error" id="err_${{cam.id}}"></div>
    <div class="cred-row">
      <button class="btn btn-primary btn-sm" onclick="submitCreds('${{cam.id}}')">Connect</button>
    </div>
  </div>`;
}}

function cardHTML(cam){{
  const name=esc(cam.name||cam.hostname||cam.ip);
  const onvifBdg=cam.onvif?`<span class="badge" style="background:#2d1e4a;color:#c09eff">ONVIF</span>`:'';
  const credBdg=cam.has_credentials?`<span class="badge" style="background:#1e2d1e;color:#6fcf97">🔐 Stored</span>`:'';
  const clearBtn=cam.has_credentials?`<button class="btn btn-ghost btn-sm" onclick="clearCreds('${{cam.id}}')">Clear Creds</button>`:'';
  return `
    <div class="feed-wrap">${{feedHTML(cam)}}</div>
    <div class="card-info">
      <div class="status-dot ${{dotClass(cam)}}"></div>
      <span class="name" title="${{name}}" onclick="openRename('${{cam.id}}','${{name}}')">${{name}}</span>
    </div>
    <div class="badges">
      ${{protoBadge(cam.protocol)}}
      <span class="badge" style="background:#1e2d1e;color:#6fcf97">:${{cam.port}}</span>
      <span class="badge" style="background:#2d2020;color:#e88">${{cam.ip}}</span>
      ${{onvifBdg}}${{credBdg}}
    </div>
    ${{credFormHTML(cam)}}
    <div class="card-actions">
      ${{clearBtn}}
      <button class="btn btn-danger btn-sm" onclick="deleteCamera('${{cam.id}}')">Remove</button>
    </div>`;
}}

function initHls(video){{
  video._hlsInit=true;
  const src=video.dataset.hlsUrl;
  if(!src) return;
  if(Hls.isSupported()){{
    const hls=new Hls();
    hls.loadSource(src);hls.attachMedia(video);
    hls.on(Hls.Events.ERROR,(_,data)=>{{
      if(data.fatal){{
        video.style.display='none';
        const ph=document.createElement('div');
        ph.className='feed-placeholder';ph.innerHTML='<span>HLS stream error</span>';
        video.parentNode.appendChild(ph);
      }}
    }});
  }}else if(video.canPlayType('application/vnd.apple.mpegurl')){{
    video.src=src;
  }}
}}

async function submitCreds(cameraId){{
  const u=document.getElementById('u_'+cameraId)?.value.trim()||'';
  const p=document.getElementById('p_'+cameraId)?.value||'';
  const errEl=document.getElementById('err_'+cameraId);
  errEl.textContent='Verifying credentials…';errEl.classList.add('visible');
  try{{
    const r=await fetch(BASE+'/api/credentials',{{
      method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{camera_id:cameraId,username:u,password:p}})
    }});
    const data=await r.json();
    if(r.ok){{errEl.classList.remove('visible');await loadCameras();}}
    else errEl.textContent=data.error||'Connection failed.';
  }}catch(e){{errEl.textContent='Network error. Please retry.';}}
}}

async function clearCreds(cid){{
  if(!confirm('Clear stored credentials for this camera?'))return;
  await fetch(BASE+'/api/cameras/'+cid+'/credentials',{{method:'DELETE'}});
  await loadCameras();
}}

async function deleteCamera(cid){{
  if(!confirm('Remove this camera from the list?'))return;
  await fetch(BASE+'/api/cameras/'+cid,{{method:'DELETE'}});
  cameras=cameras.filter(c=>c.id!==cid);renderGrid();
}}

function openRename(cid,name){{
  renameCameraId=cid;
  document.getElementById('rename-input').value=name;
  document.getElementById('rename-modal').classList.add('open');
  setTimeout(()=>document.getElementById('rename-input').focus(),50);
}}
function closeRename(){{
  document.getElementById('rename-modal').classList.remove('open');
  renameCameraId=null;
}}
async function submitRename(){{
  const name=document.getElementById('rename-input').value.trim();
  if(!name||!renameCameraId){{closeRename();return;}}
  await fetch(BASE+'/api/cameras/'+renameCameraId+'/name',{{
    method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{name}})
  }});
  closeRename();await loadCameras();
}}
document.getElementById('rename-modal').addEventListener('click',e=>{{
  if(e.target.id==='rename-modal')closeRename();
}});
document.getElementById('rename-input').addEventListener('keydown',e=>{{
  if(e.key==='Enter')submitRename();
  if(e.key==='Escape')closeRename();
}});

const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
                       .replace(/>/g,'&gt;').replace(/"/g,'&quot;');

(async()=>{{
  await loadCameras();
  const s=await(await fetch(BASE+'/api/scan/status')).json();
  if(s.running){{
    document.getElementById('scan-btn').disabled=true;
    document.getElementById('progress-track').style.display='';
    document.getElementById('status-bar').classList.add('scanning');
    pollStatus();
  }}
}})();
</script>
</body>
</html>"""


async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=build_html(), content_type="text/html")


# ─────────────────────────────────────────────────────────────────────────────
# Routing
# ─────────────────────────────────────────────────────────────────────────────

def make_app() -> web.Application:
    app = web.Application()
    p   = INGRESS_PATH

    # Register the index for both slash and no-slash variants.
    # HA ingress sidebar clicks arrive WITHOUT a trailing slash; internal
    # links arrive WITH one.  aiohttp treats these as different paths.
    app.router.add_get(p or "/",  handle_index)
    if p:
        app.router.add_get(p + "/", handle_index)

    app.router.add_get(    p + "/api/cameras",                         api_cameras)
    app.router.add_get(    p + "/api/scan/status",                     api_scan_status)
    app.router.add_post(   p + "/api/scan",                            api_scan)
    app.router.add_post(   p + "/api/credentials",                     api_set_credentials)
    app.router.add_delete( p + "/api/cameras/{camera_id}/credentials", api_clear_credentials)
    app.router.add_post(   p + "/api/cameras/{camera_id}/name",        api_rename_camera)
    app.router.add_delete( p + "/api/cameras/{camera_id}",             api_delete_camera)
    app.router.add_get(    p + "/stream/{camera_id}",                  handle_stream)
    app.router.add_get(    p + "/snapshot/{camera_id}",                handle_snapshot)
    return app


async def main():
    load_cameras()
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
