"""Recording upload by SFTP, FTPS or FTP (3.6.0, build plan C14).

CrystalHeeler's decisions of 2026-10-04: SFTP, FTPS and FTP; a global
destination plus one for each camera; each finished recording is uploaded,
then the local copy is deleted; a failed upload is retried, and the file is
kept until it succeeds; passwords are stored encrypted, like camera
passwords.

Samba and NFS shares need nothing here: they already work through Home
Assistant's network storage under /media (2.6.6).

This file cannot import camera_discovery.py (see anycam_host.py). The names
in NEEDS are set on this module at start-up.
"""
import asyncio
import ftplib
import json
import logging
import posixpath
import time
from aiohttp import web
from pathlib import Path

log = logging.getLogger("anycam")

# Taken from camera_discovery.py at start-up (anycam_host.bind).
NEEDS = ('CAMERAS', 'DATA_DIR', 'decrypt_creds', 'encrypt_creds')

UPLOAD_PROTOCOLS = {"sftp": 22, "ftps": 21, "ftp": 21}     # protocol -> default port
UPLOAD_RETRY_S = (60.0, 1800.0)       # first and longest wait after a failed upload
UPLOAD_TIMEOUT_S = 30.0               # connect and each transfer step
UPLOAD_QUEUE_MAX = 5000
_STATE: dict = {"global": None, "cameras": {}, "host_keys": {}}
_QUEUE: list[dict] = []               # {"file", "camera_id", "tries", "next"}
_STATUS: dict = {"last_ok": None, "last_error": None, "uploaded": 0}
_WAKE: asyncio.Event | None = None


def _settings_file() -> Path:
    return DATA_DIR / "upload.json"


def _queue_file() -> Path:
    return DATA_DIR / "upload_queue.json"


def upload_load() -> None:
    """Read the destinations and the waiting uploads (start-up)."""
    for path, apply in ((_settings_file(), _load_settings), (_queue_file(), _load_queue)):
        try:
            apply(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as ex:
            log.warning(f"Upload: could not read {path}: {ex}")
    if _QUEUE:
        log.info(f"Upload: {len(_QUEUE)} recording(s) waiting to upload")


def _load_settings(data: dict) -> None:
    if isinstance(data, dict):
        _STATE["global"] = data.get("global")
        _STATE["cameras"] = data.get("cameras") or {}
        _STATE["host_keys"] = data.get("host_keys") or {}


def _load_queue(data: list) -> None:
    if isinstance(data, list):
        _QUEUE[:] = [q for q in data if isinstance(q, dict) and q.get("file")][:UPLOAD_QUEUE_MAX]


def _save_settings() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    _settings_file().write_text(json.dumps(_STATE), encoding="utf-8")


def _save_queue() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    _queue_file().write_text(json.dumps(_QUEUE), encoding="utf-8")


# ── destinations ─────────────────────────────────────────────────────────────
def validate_target(data: dict, old: dict | None) -> tuple[dict | None, list[str]]:
    """Check one destination from the page; return (clean, errors).

    The password is never sent back to the page; an empty password field
    keeps the saved one (old).
    """
    if not isinstance(data, dict):
        return None, ["destination must be an object"]
    errors = []
    proto = str(data.get("protocol", "sftp")).lower()
    if proto not in UPLOAD_PROTOCOLS:
        errors.append("Protocol must be SFTP, FTPS or FTP")
    host = str(data.get("host", "")).strip()
    if not host or len(host) > 253 or any(c in host for c in " /\\@?#"):
        errors.append("Server must be a host name or address")
    try:
        port = int(data.get("port") or UPLOAD_PROTOCOLS.get(proto, 22))
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        errors.append("Port must be 1 to 65535")
    user = str(data.get("username", "")).strip()
    if not user or len(user) > 128:
        errors.append("User name is needed")
    raw_path = str(data.get("path", "/")).strip() or "/"
    path = posixpath.normpath(raw_path)
    if not raw_path.startswith("/") or ".." in raw_path.split("/"):
        errors.append("Folder must start with / and must not contain ..")
    password = str(data.get("password", ""))
    if password:
        creds = encrypt_creds(user, password)
    elif old and old.get("credentials") and old.get("username") == user:
        creds = old["credentials"]
    else:
        creds = None
        errors.append("Password is needed")
    if errors:
        return None, errors
    return {"protocol": proto, "host": host, "port": port, "username": user, "path": path,
            "credentials": creds, "delete_local": bool(data.get("delete_local", True))}, []


def _forget_host_key(t: dict) -> None:
    """A destination saved again accepts the server's key anew (see _sftp_put)."""
    _STATE["host_keys"].pop(f"{t['host']}:{t['port']}", None)


def public_target(t: dict | None) -> dict | None:
    """A destination for the page: no password."""
    if not t:
        return None
    return {k: v for k, v in t.items() if k != "credentials"} | {"has_password": bool(t.get("credentials"))}


def target_for(camera_id: str) -> dict | None:
    """The destination for a camera: its own, the global one, or none.

    A camera's entry is {"mode": "global" | "own" | "off", "target": ...};
    a camera with no entry uses the global destination.
    """
    cam = _STATE["cameras"].get(camera_id) or {"mode": "global"}
    if cam.get("mode") == "off":
        return None
    if cam.get("mode") == "own":
        return cam.get("target")
    return _STATE["global"]


# ── the queue ────────────────────────────────────────────────────────────────
def enqueue(camera_id: str, files: list[Path]) -> None:
    """Finished recording files to upload (called by the recorder)."""
    if not target_for(camera_id):
        return
    known = {q["file"] for q in _QUEUE}
    added = [str(f) for f in files if str(f) not in known]
    if not added:
        return
    for f in added:
        _QUEUE.append({"file": f, "camera_id": camera_id, "tries": 0, "next": 0.0})
    del _QUEUE[:-UPLOAD_QUEUE_MAX]
    try:
        _save_queue()
    except OSError as ex:
        log.warning(f"Upload: could not save the queue: {ex}")
    log.info(f"Upload [{camera_id}]: {len(added)} file(s) queued")
    if _WAKE is not None:
        _WAKE.set()


def _remote_path(target: dict, local: Path) -> str:
    """<folder>/<camera folder>/<file>: the same layout as under /media."""
    return posixpath.join(target["path"], local.parent.name, local.name)


async def upload_one(target: dict, local: Path) -> None:
    """Upload one file; raise on failure. Written as <name>.part, then renamed."""
    user, password = decrypt_creds(target["credentials"])
    remote = _remote_path(target, local)
    if target["protocol"] == "sftp":
        await _sftp_put(target, user, password, local, remote)
    else:
        await asyncio.to_thread(_ftp_put, target, user, password, local, remote)


async def _sftp_put(target: dict, user: str, password: str, local: Path, remote: str) -> None:
    import asyncssh
    key_id = f"{target['host']}:{target['port']}"
    async with asyncssh.connect(target["host"], port=target["port"], username=user,
                                password=password, known_hosts=None,
                                connect_timeout=UPLOAD_TIMEOUT_S) as conn:
        # Trust on first use: the server's key is saved at the first upload;
        # a different key later stops the upload (another machine at that
        # address, or one in between).
        key = conn.get_server_host_key()
        seen = key.get_fingerprint() if key else ""
        saved = _STATE["host_keys"].get(key_id)
        if saved and seen != saved:
            raise RuntimeError(f"the server's key changed (was {saved}, now {seen}); "
                               f"if that is expected, save the destination again")
        if not saved and seen:
            _STATE["host_keys"][key_id] = seen
            await asyncio.to_thread(_save_settings)
            log.info(f"Upload: SFTP server {key_id} key saved: {seen}")
        async with conn.start_sftp_client() as sftp:
            await sftp.makedirs(posixpath.dirname(remote), exist_ok=True)
            part = remote + ".part"
            await sftp.put(str(local), part)
            if await sftp.exists(remote):
                await sftp.remove(remote)
            await sftp.rename(part, remote)


def _ftp_put(target: dict, user: str, password: str, local: Path, remote: str) -> None:
    ftp = ftplib.FTP_TLS(timeout=UPLOAD_TIMEOUT_S) if target["protocol"] == "ftps" \
        else ftplib.FTP(timeout=UPLOAD_TIMEOUT_S)
    try:
        ftp.connect(target["host"], target["port"])
        ftp.login(user, password)
        if isinstance(ftp, ftplib.FTP_TLS):
            ftp.prot_p()                       # the data connection encrypted too
        folder = ""
        for part in posixpath.dirname(remote).strip("/").split("/"):
            if not part:
                continue
            folder += "/" + part
            try:
                ftp.mkd(folder)
            except ftplib.error_perm:
                pass                           # it exists
        tmp = remote + ".part"
        with open(local, "rb") as fh:
            ftp.storbinary(f"STOR {tmp}", fh)
        try:
            ftp.delete(remote)
        except ftplib.error_perm:
            pass
        ftp.rename(tmp, remote)
    finally:
        try:
            ftp.quit()
        except ftplib.all_errors:              # OSError is one of them
            ftp.close()


async def _upload_due() -> bool:
    """Upload the first due file; True when one was tried."""
    now = time.time()
    item = next((q for q in _QUEUE if q.get("next", 0) <= now), None)
    if item is None:
        return False
    local, cid = Path(item["file"]), item.get("camera_id", "")
    target = target_for(cid)
    if not local.exists() or not target:
        _QUEUE.remove(item)
        log.info(f"Upload [{cid}]: {local.name} dropped from the queue "
                 + ("(the file is gone)" if not local.exists() else "(no destination any more)"))
        await asyncio.to_thread(_save_queue)
        return True
    try:
        await asyncio.wait_for(upload_one(target, local), timeout=UPLOAD_TIMEOUT_S * 20)
    except Exception as ex:                    # any failure: keep the file, try again later
        item["tries"] = item.get("tries", 0) + 1
        wait = min(UPLOAD_RETRY_S[0] * 2 ** (item["tries"] - 1), UPLOAD_RETRY_S[1])
        item["next"] = now + wait
        _STATUS["last_error"] = f"{local.name}: {ex}"
        (log.warning if item["tries"] == 1 else log.debug)(
            f"Upload [{cid}]: {local.name} to {target['protocol'].upper()} {target['host']} "
            f"failed ({ex}); the file is kept, next try in {wait:.0f} s")
        await asyncio.to_thread(_save_queue)
        return True
    _QUEUE.remove(item)
    _STATUS["last_ok"], _STATUS["uploaded"] = time.time(), _STATUS["uploaded"] + 1
    msg = f"Upload [{cid}]: {local.name} uploaded to {target['protocol'].upper()} {target['host']}"
    if target.get("delete_local", True):
        try:
            local.unlink()
            msg += "; local copy deleted"
        except OSError as ex:
            msg += f"; local copy kept ({ex})"
    log.info(msg)
    await asyncio.to_thread(_save_queue)
    return True


async def upload_worker() -> None:
    """Upload queued recordings, one at a time, for as long as AnyCam runs."""
    global _WAKE
    _WAKE = asyncio.Event()
    while True:
        try:
            if await _upload_due():
                continue
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            log.warning(f"Upload: worker error: {ex}")
        _WAKE.clear()
        due = [q.get("next", 0) for q in _QUEUE]
        wait = max(1.0, min(due) - time.time()) if due else 3600.0
        try:
            await asyncio.wait_for(_WAKE.wait(), timeout=min(wait, 3600.0))
        except asyncio.TimeoutError:
            pass


# ── the page ─────────────────────────────────────────────────────────────────
def _payload(camera_id: str | None) -> dict:
    cam = _STATE["cameras"].get(camera_id) if camera_id else None
    return {"global": public_target(_STATE["global"]),
            "camera": ({"mode": (cam or {}).get("mode", "global"),
                        "target": public_target((cam or {}).get("target"))} if camera_id else None),
            "protocols": UPLOAD_PROTOCOLS,
            "queue": len(_QUEUE), "status": _STATUS}


async def api_upload_settings(request: web.Request) -> web.Response:
    """GET/POST /api/upload/settings[?camera_id=...] — the global or a camera's destination."""
    camera_id = request.query.get("camera_id") or None
    if camera_id and camera_id not in CAMERAS:
        return web.json_response({"error": "Camera not found"}, status=404)
    if request.method == "POST":
        try:
            data = await request.json()
        except ValueError:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        data = data if isinstance(data, dict) else {}
        if camera_id:
            mode = str(data.get("mode", "global"))
            if mode not in ("global", "own", "off"):
                return web.json_response({"error": "mode must be global, own or off"}, status=400)
            entry = {"mode": mode}
            if mode == "own":
                old = (_STATE["cameras"].get(camera_id) or {}).get("target")
                clean, errors = validate_target(data.get("target") or {}, old)
                if errors:
                    return web.json_response({"error": "; ".join(errors)}, status=400)
                entry["target"] = clean
                _forget_host_key(clean)
            _STATE["cameras"][camera_id] = entry
            log.info(f"Upload [{camera_id}]: destination set to {mode}"
                     + (f" ({entry['target']['protocol'].upper()} {entry['target']['host']})"
                        if mode == "own" else ""))
        elif data.get("off"):
            _STATE["global"] = None
            log.info("Upload: global destination removed")
        else:
            clean, errors = validate_target(data, _STATE["global"])
            if errors:
                return web.json_response({"error": "; ".join(errors)}, status=400)
            _STATE["global"] = clean
            _forget_host_key(clean)
            log.info(f"Upload: global destination {clean['protocol'].upper()} {clean['host']}:"
                     f"{clean['port']}{clean['path']}")
        try:
            await asyncio.to_thread(_save_settings)
        except OSError as ex:
            log.warning(f"Upload: could not save {_settings_file()}: {ex}")
    return web.json_response(_payload(camera_id))


async def api_upload_test(request: web.Request) -> web.Response:
    """POST /api/upload/test[?camera_id=...] — upload a small test file.

    The test file stays on the server (in anycam_upload_test/); only the local
    copy is deleted.
    """
    camera_id = request.query.get("camera_id") or None
    target = target_for(camera_id) if camera_id else _STATE["global"]
    if not target:
        return web.json_response({"ok": False, "error": "No destination is set"})
    tmp = DATA_DIR / "anycam_upload_test" / f"anycam-test-{int(time.time())}.txt"
    try:
        await asyncio.to_thread(tmp.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(tmp.write_text, "AnyCam upload test. You can delete this file.\n")
        await asyncio.wait_for(upload_one(target, tmp), timeout=UPLOAD_TIMEOUT_S * 2)
    except Exception as ex:
        return web.json_response({"ok": False, "error": str(ex) or type(ex).__name__})
    finally:
        try:
            await asyncio.to_thread(tmp.unlink)
        except OSError:
            pass
    return web.json_response({"ok": True, "remote": _remote_path(target, tmp)})
