"""The network scan: live-host and multicast discovery, the port scan, and the scan itself.

Moved out of camera_discovery.py in 3.0.0-rc1.3 (build plan E1); the function
bodies are unchanged.

This file cannot import camera_discovery.py (see anycam_host.py). The names in
NEEDS are set on this module at start-up; H reads a camera_discovery.py
value at the moment of use.
"""
import asyncio
import ipaddress
import json
import logging
import re
import socket
import struct
import subprocess
import time
import uuid
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from anycam_host import H
from camera_db import CAMERA_DB       # 3.5.0 (D2): the scan's port list
from anycam_brand import (
    CAMERA_KEYWORDS, NON_CAMERA_KEYWORDS, appliance_camera, lookup_oui,
)
from anycam_probe import (
    _onvif_media_url, _rtsp_options_fingerprint, _validate_rtsp_urls_single_socket,
    find_rtsp_path, onvif_get_profiles, onvif_probe_no_login,
    onvif_get_stream_uri, probe_hls, probe_hls_quick,
    probe_mjpeg_http, probe_mjpeg_quick, probe_rtmp, probe_rtsp,
    probe_rtsp_options, probe_webrtc, probe_ws_rtsp,
)

log = logging.getLogger("anycam")

# 3.0.0-rc1.5 (build plan B22): the scan's cancel flag. The Cancel endpoint
# sets anycam_scan.SCAN_CANCELLED. Before, it set a different name in
# camera_discovery.py, which the scan never read, so Cancel did nothing.
SCAN_CANCELLED = False

# Taken from camera_discovery.py at start-up (anycam_host.bind).
NEEDS = (
    'BLACKLIST', 'CAMERAS', 'CURRENT_VERSION', 'PORT',
    'PSCAN', 'SCAN_OPTIONS', 'SCAN_STATE', '_ACD_ESCALATED',
    '_RST_OBSERVED', '_THREAD_POOL', '_brand_throttle_seconds', '_identify_camera_brand',
    '_matches_feedback_fingerprint', '_publish_scan_card', '_strip_creds', '_throttle_wait_if_needed',
    'build_authenticated_url', 'decrypt_creds', 'encrypt_creds', 'load_runtime',
    'probe_stream_details', 'save_cameras', 'save_runtime',
    'REMOVED_CAMERAS', 'save_removed',
)
# 2.4.0-rc2.6: Pending-flush card creation buffer. During a scan, new
# cards discovered are routed here instead of CAMERAS so they don't
# render to the UI mid-scan. After dedup runs at end of scan, survivors
# flush into CAMERAS in one batch. None when no scan is in progress;
# scan-time card writers should call _publish_scan_card(cam) which
# handles the routing.
PENDING_CAMERAS: dict | None = None
# 3.0.1 (B2): the MAC address of each live host from the last ARP scan,
# so a host with no open camera port can still be named in the log.
LIVE_HOST_MACS: dict[str, str] = {}
# Last ARP-discovered hosts — populated by run_scan(), consumed by Port Scan UI
ARP_HOSTS: list[dict] = []   # [{ip, hostname}, ...]


def get_local_subnet() -> str:
    """The network to scan, from the default route's interface.

    3.7.1 (B34): the route command is tried twice (5 s, then 15 s). If it
    still gives nothing, the add-on's own address with a /24 mask is used.
    Before, AnyCam fell back to a fixed 192.168.1.0/24 and scanned a
    network the cameras were not on (2026-10-06, test system C). An empty
    string means the network is unknown; the scan stops and says so.
    """
    for timeout in (5, 15):
        try:
            r = subprocess.run(["ip", "route", "show", "default"],
                               capture_output=True, text=True, timeout=timeout)
            m = re.search(r"dev\s+(\S+)", r.stdout)
            if m:
                r2 = subprocess.run(["ip", "addr", "show", m.group(1)],
                                    capture_output=True, text=True, timeout=timeout)
                m2 = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+/\d+)", r2.stdout)
                if m2:
                    return str(ipaddress.ip_interface(m2.group(1)).network)
        except Exception as e:
            log.warning(f"Subnet: {e}")
    own = _own_ipv4()
    if own:
        net = str(ipaddress.ip_interface(f"{own}/24").network)
        log.warning(f"Subnet: the route check gave no answer — scanning {net}, "
                    f"from the add-on's own address {own}")
        return net
    log.error("Subnet: AnyCam could not find its network")
    return ""


def _own_ipv4() -> str | None:
    """The address the add-on uses toward other networks; no packet is sent."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))        # TEST-NET-1: routing only, nothing sent
            addr = s.getsockname()[0]
    except OSError:
        return None
    return None if addr.startswith(("127.", "0.")) else addr


def get_default_gateway() -> str | None:
    try:
        r = subprocess.run(["ip", "route", "show", "default"],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r"via\s+(\d+\.\d+\.\d+\.\d+)", r.stdout)
        return m.group(1) if m else None
    except Exception:
        return None


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
    LIVE_HOST_MACS.clear()
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
                    mac = host.find("address[@addrtype='mac']")
                    if mac is not None and mac.get("addr"):
                        LIVE_HOST_MACS[addr.get("addr")] = mac.get("addr")
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
# 3.5.0 (D2): the list below is (b), the documented ports, plus the
# classifier ports; (a) is now read from CAMERA_DB at start-up, so a brand
# added there is scanned on its ports without a change here. On 2026-10-04
# every CAMERA_DB port was already in this list, so the result is the same
# 54 ports.
_PORTS_DOCUMENTED: list[int] = [
    # ── Classifier-only ports (rc2.2) ────────────────────────────────
    # These ports are NOT camera ports — they're scanned so the verdict
    # logic has signals to REJECT non-camera devices that happen to
    # expose a web UI on 80/443/8080. Without these, an HP printer or
    # NAS with only a web admin page falls through as a "camera
    # candidate" and gets RTSP-probed unnecessarily. rc1.0's top-1000
    # scan caught these incidentally; rc2.1's narrow port list lost
    # them, surfacing a regression where an HP printer (gSOAP
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
_PORTS_CLASSIFIER = (22, 631, 9100)
CAMERA_RELEVANT_PORTS: list[int] = list(_PORTS_CLASSIFIER) + sorted(
    (set(_PORTS_DOCUMENTED)
     | {p for e in CAMERA_DB for p in e.get("default_ports", [])}
     | {s["port"] for e in CAMERA_DB for s in e.get("streams", [])})
    - set(_PORTS_CLASSIFIER))


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

    # 2.6.1 — Tier 1 item 4: honour the brand cooldown on the ONVIF SOAP path.
    # _onvif_soap is synchronous and opens up to two TCP connections per call
    # (SOAP 1.2, then the 1.1 fallback for cameras that answer 400). It never
    # consulted the throttle, so a re-auth against a rate-limited brand fired
    # 1 + N calls back to back — N being the profile count — and every one of
    # them landed inside the cooldown window. The wait belongs here, in the
    # async caller, because _throttle_wait_if_needed cannot be awaited from
    # inside the sync SOAP helper.
    _onvif_throttle_s = _brand_throttle_seconds(camera)

    log.info(f"  [{camera_id}] ONVIF re-auth: media_url={media_url}")
    await _throttle_wait_if_needed(ip, _onvif_throttle_s,
                                   f"onvif re-auth GetProfiles {camera_id}")
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
        # One GetStreamUri per profile — each one needs its own cooldown wait.
        await _throttle_wait_if_needed(ip, _onvif_throttle_s,
                                       f"onvif re-auth GetStreamUri {camera_id}")
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
    def _res(c: dict) -> int:

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


# ── 3.7.5-rc2.0 (B53): no probing of cameras AnyCam already has ──────────
# Both times the Microseven got stuck, AnyCam had just walked 28 RTSP paths
# on it at start-up or rescan (2026-10-06 19:13, 2026-10-07 20:01). A scan
# now leaves a host alone when a saved camera on that address has a working
# stream, and gives a removed camera's card back from REMOVED_CAMERAS. For
# every brand. Removing a card makes its camera scannable again.
def _scan_known_camera(ip: str, saved: dict) -> dict | None:
    """A saved camera on this address with a working stream, or None."""
    for c in saved.values():
        if (c.get("ip") == ip and c.get("stream_url")
                and (c.get("credentials") or c.get("rtsp_probe_ok") or c.get("status") == "ready")):
            return c
    return None


def _scan_restore_removed(ip: str, saved: dict) -> list[dict]:
    """Give back the cards of removed cameras on this address, without probing."""
    rem = [c for c in REMOVED_CAMERAS.values() if c.get("ip") == ip]
    if not rem:
        return []
    if any(c.get("ip") == ip for c in saved.values()):
        # The user set this camera up again; the old copy is not needed.
        for c in rem:
            REMOVED_CAMERAS.pop(c["id"], None)
        save_removed()
        return []
    out = []
    for c in rem:
        card = json.loads(json.dumps(c))
        _publish_scan_card(card)
        out.append(card)
    return out


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
                # 3.7.5-rc2.0 (B53): inside the brand's cooldown, like every other
                # connection (2026-10-07: 0 s after the ONVIF calls).
                await _throttle_wait_if_needed(ip, _brand_throttle_seconds(cam),
                                               f"verify {cam.get('name', ip)}")
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


async def _probe_host_port(ip: str, port: int, hostname: str,
                            initial_protocol: str, prev: dict,
                            verdict: str, reason: str, loop: asyncio.AbstractEventLoop,
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
        # 3.8.0-rc1.0 (B54): a scan walk stops at the first 401, no Layer 2
        url = await loop.run_in_executor(
            _THREAD_POOL, lambda: find_rtsp_path(ip, port, "", "", host_meta, scan=True))
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
                _THREAD_POOL, lambda: find_rtsp_path(ip, port, saved_u, saved_p,
                                                     host_meta, scan=True))
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
                _THREAD_POOL, lambda: find_rtsp_path(ip, port, "", "", host_meta, scan=True))
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


# 3.0.1 (B8): the progress bar follows the work done, not fixed jumps
# (0, 25, 55, then per host to 80, 82, 100), and the page gets the elapsed
# time and an estimate of the time left, which it showed as 0:00 before.
# Each stage's share comes from how long it took in the last scan.
SCAN_STAGE_DEFAULT_S = (8.0, 14.0, 10.0, 60.0)    # discovery, port scan, probing, deeper scan


class _ScanProgress:
    """Progress and time left of one scan, from the last scan's stage times."""

    def __init__(self, broad: bool) -> None:
        saved = load_runtime().get("scan_stage_s") or []
        self.expect = [float(saved[i]) if i < len(saved) and saved[i] else SCAN_STAGE_DEFAULT_S[i]
                       for i in range(4)]
        if not broad:
            self.expect[3] = 0.0
        self.t0 = time.time()
        self.stage = 0
        self.stage_t0 = self.t0
        self.took = [0.0, 0.0, 0.0, 0.0]

    def start(self, stage: int) -> None:
        """Begin stage 1 to 4."""
        now = time.time()
        if self.stage:
            self.took[self.stage - 1] = now - self.stage_t0
        self.stage, self.stage_t0 = stage, now
        self.update(0.0)

    def update(self, done: float) -> None:
        """Set the share of the current stage that is done (0 to 1)."""
        now, i = time.time(), self.stage - 1
        done = min(max(done, 0.0), 1.0)
        total = sum(self.expect) or 1.0
        before = sum(self.expect[:i])
        pct = int(100 * (before + done * self.expect[i]) / total)
        in_stage = now - self.stage_t0
        left_here = (self.expect[i] * (1 - done) if done
                     else max(self.expect[i] - in_stage, 0.0))
        SCAN_STATE.update(progress=min(pct, 99), started_at=self.t0, elapsed=now - self.t0,
                          eta=int(left_here + sum(self.expect[i + 1:])))

    def finish(self, save: bool) -> None:
        """End the scan; keep the stage times for the next estimate."""
        now = time.time()
        if self.stage:
            self.took[self.stage - 1] = now - self.stage_t0
        SCAN_STATE.update(elapsed=now - self.t0, eta=0)
        if save:
            rt = load_runtime()
            rt["scan_stage_s"] = [round(t, 1) if t else self.expect[k] for k, t in enumerate(self.took)]
            save_runtime(rt)


def _onvif_card(ip: str, onvif: dict, meta: dict, prev: dict, reason: str) -> dict:
    """3.8.0-rc1.0 (B54): the "needs password" card of an ONVIF camera."""
    return {
        "id": f"{ip}_onvif", "ip": ip, "hostname": onvif.get("name", ip),
        "port": 80, "protocol": "ONVIF",
        "stream_url": prev.get("stream_url", ""),
        "requires_credentials": True,
        "credentials": prev.get("credentials"),
        "name": prev.get("name", onvif.get("name", ip)),
        "xaddrs": onvif.get("xaddrs", ""),
        "status": "needs_credentials",
        "onvif": True, "user_saved": bool(prev), "display": "proxy",
        "verdict": "camera", "verdict_reason": reason,
        "manufacturer":  meta.get("manufacturer", ""),
        "mac_addr":      meta.get("mac_addr", ""),
        "mac_vendor":    meta.get("mac_vendor", ""),
        "onvif_scopes":  onvif.get("onvif_scopes", ""),
        "locked_streams": [],
    }


async def _scan_onvif_first(ip: str, onvif: dict, meta: dict, prev: dict,
                            loop: asyncio.AbstractEventLoop) -> dict | None:
    """3.8.0-rc1.0 (B54): ask a device that answered WS-Discovery for its
    ONVIF profiles, with no login, before any other probe.

    A login prompt identifies an ONVIF camera that needs a password: the card
    asks for it, and the host gets no RTSP walk and no other probe. Profiles
    without a login give a ready card when a stream plays without one. None
    when ONVIF did not answer: the scan goes on as before.
    """
    try:
        _identify_camera_brand(meta)
    except Exception as e:
        log.debug(f"  brand-id (ONVIF first) {ip}: {e}")
    media_url = _onvif_media_url(ip, 80, onvif.get("xaddrs", ""))
    answer, profiles = await loop.run_in_executor(_THREAD_POOL, onvif_probe_no_login, media_url)
    if answer == "login":
        log.info(f"  ONVIF {ip}: asks for a login — card needs a password; no RTSP walk, "
                 f"its other ports are not probed")
        return _onvif_card(ip, onvif, meta, prev, "ONVIF asks for a login")
    if answer != "profiles":
        return None
    urls: list[str] = []
    for prof in profiles:
        uri = await loop.run_in_executor(_THREAD_POOL, onvif_get_stream_uri,
                                         media_url, prof["token"], "", "")
        if uri and uri not in urls:
            urls.append(uri)
    if urls:
        try:
            rtsp_port = urlparse(urls[0]).port or 554
        except ValueError:
            rtsp_port = 554
        await _throttle_wait_if_needed(ip, _brand_throttle_seconds(meta), f"ONVIF streams {ip}")
        ok = await loop.run_in_executor(_THREAD_POOL, _validate_rtsp_urls_single_socket,
                                        ip, rtsp_port, urls, "", "", 6.0, None,
                                        f"{ip}/onvif-no-login")
        good = [u for u in urls if ok.get(u)]
        if good:
            log.info(f"  ONVIF {ip}: {len(profiles)} profile(s) without a login, "
                     f"{len(good)} stream(s) play — card ready")
            return {
                "id": f"{ip}_onvif", "ip": ip, "hostname": onvif.get("name", ip),
                "port": rtsp_port, "protocol": "RTSP",
                "stream_url": good[0],
                "sub_stream_url": good[-1] if len(good) > 1 else None,
                "requires_credentials": False, "credentials": None,
                "name": prev.get("name", onvif.get("name", ip)),
                "xaddrs": onvif.get("xaddrs", ""),
                "status": "ready", "rtsp_probe_ok": True,
                "onvif": True, "user_saved": bool(prev), "display": "proxy",
                "verdict": "camera", "verdict_reason": "ONVIF, no login",
                "manufacturer": meta.get("manufacturer", ""),
                "mac_addr": meta.get("mac_addr", ""), "mac_vendor": meta.get("mac_vendor", ""),
                "onvif_scopes": onvif.get("onvif_scopes", ""), "locked_streams": [],
            }
    log.info(f"  ONVIF {ip}: profiles without a login, but no stream plays without one "
             f"— card needs a password")
    return _onvif_card(ip, onvif, meta, prev, "ONVIF discovered")


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
    prog = _ScanProgress(bool(SCAN_OPTIONS.get("broad_sweep")))
    prog.start(1)
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
        if not subnet:      # 3.7.1 (B34): never guess a network
            raise RuntimeError("AnyCam could not find its network; try the scan again")
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

        prog.start(2)
        SCAN_STATE.update(stage=2,
                          stage_label="Stage 2/4 — Camera port scan",
                          message=f"Stage 2/4 — Scanning camera ports on {len(all_live)} live host(s)…")

        if SCAN_CANCELLED:
            SCAN_STATE.update(message="Scan cancelled")
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

        # 3.0.1 (B2): name every live host that gets no card from the port
        # scan. Before, these were dropped without a log line, so a missing
        # camera could not be found in the log.
        for dip in sorted(all_live - responding_ips,
                          key=lambda a: tuple(int(x) for x in a.split(".")) if a.count(".") == 3 else (999,)):
            dmac = LIVE_HOST_MACS.get(dip, "")
            dmaker = (lookup_oui(dmac) if dmac else "") or "maker unknown"
            log.info(f"  No camera port open on {dip} (MAC {dmac or 'not seen'}, {dmaker})")

        prog.start(3)
        SCAN_STATE.update(stage=3,
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

        # 3.0.1 (B8): one unit per probed host and per ONVIF-only device
        units3 = max(len(nmap_results) + len(onvif_results), 1)
        onvif_by_ip = {r["ip"]: r for r in onvif_results}     # 3.8.0-rc1.0 (B54)
        for idx, host in enumerate(nmap_results):
            if SCAN_CANCELLED:
                break
            ip, hostname = host["ip"], host.get("hostname", host["ip"])
            prog.update(idx / units3)
            SCAN_STATE.update(
                message=f"Stage 3/4 — Probing {ip} ({idx+1}/{len(nmap_results)})…")

            verdict, reason = classify_device(host)
            # 3.7.5-rc2.0 (B53): a camera AnyCam already has is not probed
            known = _scan_known_camera(ip, saved)
            if known:
                log.info(f"  {ip}: known camera '{known.get('name', ip)}' — not probed "
                         f"(remove its card to scan it again)")
                continue
            restored = _scan_restore_removed(ip, saved)
            if restored:
                log.info(f"  {ip}: removed camera '{restored[0].get('name', ip)}' — card given back "
                         f"from its kept details, not probed")
                continue
            # 3.8.0-rc1.0 (B54): ONVIF first, with no login. An answer
            # identifies the camera, and its ports get no probes.
            onvif_hit = onvif_by_ip.get(ip)
            if onvif_hit:
                ometa = {"ip": ip, "hostname": onvif_hit.get("name", hostname),
                         "name": onvif_hit.get("name", ""),
                         "mac_addr": host.get("mac_addr", ""),
                         "mac_vendor": host.get("mac_vendor", ""),
                         "vendor": host.get("mac_vendor", ""),
                         "onvif_scopes": onvif_hit.get("onvif_scopes", ""),
                         "verdict_reason": reason}
                ocard = await _scan_onvif_first(ip, onvif_hit, ometa,
                                                saved.get(f"{ip}_onvif", {}), loop)
                if ocard:
                    _publish_scan_card(ocard)
                    continue
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
            for pidx, port_info in enumerate(open_ports_sorted):
                if SCAN_CANCELLED:      # 3.0.0-rc1.5 (B22): stop between ports too
                    break
                prog.update((idx + pidx / max(len(open_ports_sorted), 1)) / units3)
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
                # 3.8.0-rc1.0 (B54): a 401 or a working stream on this port
                # identified the camera; its other ports get no probes.
                if host_meta.get("rtsp_needs_password") or (
                        cam and cam.get("protocol") == "RTSP" and cam.get("status") == "ready"):
                    rest = len(open_ports_sorted) - pidx - 1
                    if rest:
                        log.info(f"  {ip}: identified on port {port} — its other "
                                 f"{rest} port(s) are not probed")
                    break
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
            prog.start(4)
            SCAN_STATE.update(stage=4,
                              stage_label="Stage 4/4 — Deeper Scan",
                              message=f"Stage 4/4 — Deeper scan on {len(silent)} unresponsive host(s)…")
            broad_results = await loop.run_in_executor(_THREAD_POOL, broad_nmap_scan, silent)
            for host in broad_results:
                if SCAN_CANCELLED:
                    break
                ip, hostname = host["ip"], host.get("hostname", host["ip"])
                verdict, reason = classify_device(host)
                if _scan_known_camera(ip, saved) or _scan_restore_removed(ip, saved):
                    continue            # 3.7.5-rc2.0 (B53)
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
                    # 3.8.0-rc1.0 (B54): identified; its other ports get no probes
                    if host_meta.get("rtsp_needs_password") or (
                            cam and cam.get("protocol") == "RTSP" and cam.get("status") == "ready"):
                        log.info(f"  {ip}: identified on port {port} — its other ports are not probed")
                        break

        # 3.0.1 (B2): an appliance camera (by its MAC) gets an information
        # card. If the probing above already gave it a card (a login form,
        # for example), that card stays and only gets a note.
        if not SCAN_CANCELLED:
            macs = dict(LIVE_HOST_MACS)
            macs.update({h["ip"]: h.get("mac_addr", "") for h in nmap_results if h.get("mac_addr")})
            for aip, amac in sorted(macs.items()):
                appl = appliance_camera(amac)
                if not appl or aip in BLACKLIST:
                    continue
                aname, app = appl
                note = f"Camera built into an appliance; its video is reachable only through {app}."
                have = [c for c in list(CAMERAS.values()) + list((PENDING_CAMERAS or {}).values())
                        if c.get("ip") == aip]
                if have:
                    for c in have:
                        if note not in (c.get("device_notes") or ""):
                            c["device_notes"] = ((c.get("device_notes") or "") + " " + note).strip()
                    continue
                log.info(f"  Appliance camera: {aip} ({aname}, MAC {amac}) — information card")
                _publish_scan_card({
                    "id": f"{aip}_appliance", "ip": aip, "hostname": aip, "port": 0,
                    "protocol": "Appliance", "stream_url": "",
                    "requires_credentials": False, "credentials": None,
                    "name": aname, "status": "info", "display": "appliance",
                    "user_saved": False, "verdict": "camera",
                    "verdict_reason": "Appliance camera, recognised by its MAC address",
                    "mac_addr": amac, "mac_vendor": lookup_oui(amac) or aname,
                    "manufacturer": aname, "info": note, "device_notes": note,
                })

        # Merge multicast-only ONVIF cameras not found by nmap
        for oidx, onvif in enumerate(onvif_results):
            if SCAN_CANCELLED:          # 3.0.0-rc1.5 (B22): no new probing after Cancel
                break
            prog.update((len(nmap_results) + oidx) / units3)
            ip = onvif["ip"]
            if ip == gateway or ip in BLACKLIST:
                continue
            # 2.4.0-rc2.6: also check PENDING_CAMERAS — cards just
            # discovered this scan haven't flushed to CAMERAS yet,
            # but they DO exist for the purposes of ONVIF dup-check.
            _all_cams = list(CAMERAS.values()) + list(
                (PENDING_CAMERAS or {}).values())
            existing = [c for c in _all_cams if c["ip"] == ip]
            if not existing and _scan_restore_removed(ip, saved):
                continue            # 3.7.5-rc2.0 (B53): given back, not probed
            if existing:
                for cam in existing:
                    cam["onvif"]  = True
                    cam["xaddrs"] = onvif.get("xaddrs", cam.get("xaddrs", ""))
            else:
                cid  = f"{ip}_onvif"
                prev = saved.get(cid, {})
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
                # 3.8.0-rc1.0 (B54): ONVIF with no login, in place of the
                # web page on port 80, the RTSP check and the walk on 554.
                ocard = await _scan_onvif_first(ip, onvif, onvif_meta, prev, loop)
                if not ocard:
                    log.info(f"  ONVIF {ip}: no answer to GetProfiles — card needs a password")
                    ocard = _onvif_card(ip, onvif, onvif_meta, prev, "ONVIF discovered")
                _publish_scan_card(ocard)

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
        prog.finish(save=not SCAN_CANCELLED)
        ready = sum(1 for c in CAMERAS.values() if c.get("status") == "ready")
        SCAN_STATE.update(
            running=False, progress=100, stage=0, stage_label="",
            message=(f"Scan cancelled — {len(CAMERAS)} device(s), {ready} streaming."
                     if SCAN_CANCELLED else
                     f"Scan complete — {len(CAMERAS)} device(s), {ready} streaming."),
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
