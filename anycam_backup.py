"""Copies of the motion and zone settings, and finding them again (3.8.3, C27).

Every save of the motion settings also writes a copy, anycam-motion-<date>-<time>.json,
into the copy folder (/media/anycam/settings by default), and the newest COPY_KEEP
copies are kept. The page can also download a copy. Restore reads a folder of copies
and finds, for each camera, the newest copy that holds it: by its card key
(ip:port#chN), or by its MAC address and channel when the address changed. The user
names a folder, never a file.

This file uses no other AnyCam file: anycam_motion.py builds the entries and applies
them, and passes the folders in.
"""
import datetime
import json
import os
from pathlib import Path

COPY_FORMAT = 1
COPY_PREFIX = "anycam-motion-"
COPY_FOLDER = "settings"          # under the recordings folder; the Storage tab skips it
COPY_KEEP = 20                    # CrystalHeeler, 2026-10-16
COPY_READ_MAX = 500               # copies read from one folder, newest first
COPY_BYTES_MAX = 2_000_000        # a bigger file is not a copy
RESTORE_ROOTS = ("/media", "/share")   # a folder on the Pi must be under one of these
ASPECT_TOLERANCE = 0.02           # a 2% change in width/height counts as a new picture shape


def copy_name(when: datetime.datetime) -> str:
    return f"{COPY_PREFIX}{when:%Y%m%d-%H%M%S}.json"


def normal_mac(mac: object) -> str:
    """aa:bb:cc:dd:ee:ff in lower case, or "" when it is not a MAC address."""
    hexes = "".join(ch for ch in str(mac or "").lower() if ch in "0123456789abcdef")
    if len(hexes) != 12:
        return ""
    return ":".join(hexes[i:i + 2] for i in range(0, 12, 2))


def build_copy(entries: list[dict], version: str, when: datetime.datetime) -> dict:
    return {"anycam_motion_copy": COPY_FORMAT, "version": version,
            "saved": when.isoformat(timespec="seconds"), "cameras": entries}


def _same_cameras(path: Path, entries: list[dict]) -> bool:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("cameras") == entries
    except (OSError, ValueError, AttributeError):
        return False


def _copies_in(folder: Path) -> list[Path]:
    """The copy files in a folder, newest name first (the name holds the date and time)."""
    try:
        files = [p for p in folder.iterdir()
                 if p.is_file() and p.name.startswith(COPY_PREFIX) and p.suffix == ".json"]
    except OSError:
        return []
    return sorted(files, key=lambda p: p.name, reverse=True)


def write_copy(folder: Path, entries: list[dict], version: str,
               when: datetime.datetime | None = None) -> Path | None:
    """Write a copy and keep the newest COPY_KEEP; None when nothing needed writing.

    No copy is written without any camera settings, or when the settings equal the
    newest copy's: a lost settings file then never pushes the good copies out.
    """
    if not entries:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    copies = _copies_in(folder)
    if copies and _same_cameras(copies[0], entries):
        return None
    when = when or datetime.datetime.now()
    path = folder / copy_name(when)
    n = 2
    while path.exists():
        path = folder / f"{COPY_PREFIX}{when:%Y%m%d-%H%M%S}-{n}.json"
        n += 1
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(build_copy(entries, version, when), indent=1), encoding="utf-8")
    os.replace(tmp, path)
    for old in _copies_in(folder)[COPY_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass
    return path


def parse_copy(text: str, name: str = "") -> dict | None:
    """A copy read from a file or sent by the page; None when it is not one."""
    if not isinstance(text, str) or len(text) > COPY_BYTES_MAX:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if (not isinstance(data, dict) or data.get("anycam_motion_copy") != COPY_FORMAT
            or not isinstance(data.get("cameras"), list)):
        return None
    try:
        saved = datetime.datetime.fromisoformat(str(data.get("saved")))
    except ValueError:
        return None
    cams = [c for c in data["cameras"] if isinstance(c, dict) and isinstance(c.get("key"), str)]
    return {"name": name, "saved": saved, "cameras": cams}


def read_folder(folder: Path) -> list[dict]:
    """The copies in a folder, newest first. Files that are not copies are skipped."""
    out = []
    for path in _copies_in(folder)[:COPY_READ_MAX]:
        try:
            if path.stat().st_size > COPY_BYTES_MAX:
                continue
            copy = parse_copy(path.read_text(encoding="utf-8"), path.name)
        except (OSError, UnicodeDecodeError):
            continue
        if copy:
            out.append(copy)
    return sorted(out, key=lambda c: c["saved"], reverse=True)


def pi_folder(raw: str, roots: tuple[str, ...] | None = None) -> Path | None:
    """The named folder on the Pi, or None when it is not a folder under RESTORE_ROOTS."""
    raw = str(raw or "").strip()
    if not raw:
        return None
    try:
        path = Path(raw).resolve()
    except (OSError, RuntimeError):
        return None
    for root in roots or RESTORE_ROOTS:
        try:
            path.relative_to(Path(root).resolve())
        except ValueError:
            continue
        return path if path.is_dir() else None
    return None


def _aspect(w: object, h: object) -> float | None:
    try:
        w, h = float(w), float(h)
    except (TypeError, ValueError):
        return None
    return w / h if w > 0 and h > 0 else None


def aspect_changed(entry: dict, width: object, height: object) -> bool:
    then, now = _aspect(entry.get("width"), entry.get("height")), _aspect(width, height)
    return bool(then and now and abs(then - now) / then > ASPECT_TOLERANCE)


def match(cameras: list[dict], copies: list[dict]) -> dict[str, dict]:
    """For each camera, the newest copy that holds it.

    cameras: {"id", "key", "mac", "channel"}. A card key match wins over a MAC
    match within one copy; across copies the newest wins. Returns
    camera id -> {"entry", "saved", "source", "by": "key" | "mac"}.
    """
    found: dict[str, dict] = {}
    for copy in sorted(copies, key=lambda c: c["saved"], reverse=True):
        by_key = {c["key"]: c for c in copy["cameras"]}
        by_mac = {}
        for c in copy["cameras"]:
            mac = normal_mac(c.get("mac"))
            if mac:
                by_mac.setdefault((mac, str(c.get("channel") or "")), c)
        for cam in cameras:
            if cam["id"] in found:
                continue
            entry, by = by_key.get(cam["key"]), "key"
            mac = normal_mac(cam.get("mac"))
            if entry is None and mac:
                entry, by = by_mac.get((mac, str(cam.get("channel") or ""))), "mac"
            if entry is not None:
                found[cam["id"]] = {"entry": entry, "saved": copy["saved"],
                                    "source": copy["name"], "by": by}
    return found
