"""The Storage tab: list, download, rename, move and delete recordings.

Moved out of camera_discovery.py in 3.0.0-rc1.1 (build plan E1, stage 2);
the function bodies are unchanged.

This file cannot import camera_discovery.py: that file imports this one, and
it runs as the program's entry point. The names listed in NEEDS are set on
this module by anycam_host.bind() when camera_discovery.py has loaded. H reads
a camera_discovery.py value at the moment of use, for the values that file
replaces while it runs.
"""
import logging
from aiohttp import web
from pathlib import Path

from anycam_host import H
import anycam_zones          # 3.4.0 (C17): the zone that started a recording

log = logging.getLogger("anycam")

# Taken from camera_discovery.py at start-up (anycam_host.bind).
NEEDS = (
    'MEDIA_DIR',
)


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
                    "zone":     anycam_zones.rec_zone_for(f.name),   # 3.4.0 (C17)
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
