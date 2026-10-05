"""The manufacturer (OUI) database, the keyword tables and brand identification.

Moved out of camera_discovery.py in 3.0.0-rc1.5 (build plan E1); the function
bodies are unchanged.

This file cannot import camera_discovery.py (see anycam_host.py). The names in
NEEDS are set on this module at start-up; H reads a camera_discovery.py
value at the moment of use.
"""
import asyncio
import io
import json
import logging
import re as _re_mod
import time

from anycam_host import H
from camera_db import (
    CAMERA_DB,
)

log = logging.getLogger("anycam")

# Taken from camera_discovery.py at start-up (anycam_host.bind).
NEEDS = (
    'DATA_DIR', 'OUI_CACHE_FILE', 'OUI_CSV_URL', 'OUI_MAX_AGE_DAYS',
    '_THREAD_POOL',
)

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


# 3.0.1 (B2): cameras built into household appliances. Their video goes
# through the maker's cloud and app, and most open no local port, so the
# scan finds them only by their MAC address. A key is a MAC prefix of any
# length: the iENSO camera-module block is an MA-M block (28 bits).
APPLIANCE_CAMERA_PREFIXES: dict[str, tuple[str, str]] = {
    "04:A1:6F:1": ("Appliance camera (iENSO module)", "the appliance maker's app"),
    "00:AE:F7":   ("Dreame robot vacuum", "the Dreamehome app"),
}


def appliance_camera(mac: str) -> tuple[str, str] | None:
    """(name, app) when the MAC belongs to an appliance camera, else None."""
    m = (mac or "").upper().replace("-", ":")
    for prefix, info in APPLIANCE_CAMERA_PREFIXES.items():
        if m.startswith(prefix):
            return info
    return None


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
        # 2.6.6: off the event loop; the file holds about 40,000 entries.
        # Found by the extended P4 check in verify_release.py (F2).
        await asyncio.to_thread(OUI_CACHE_FILE.write_text, json.dumps(oui_map))
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
