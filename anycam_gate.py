#!/usr/bin/env python3
"""The connection gate for cameras that limit how often they are opened.

3.8.1 (C26), reworked in 3.8.2 (B65).

A Hipcam-family camera (Microseven, Sricam, Vstarcam, Wansview, Tenvis:
throttle_type "rate_limit_per_ip_tcp" in CAMERA_DB) resets a connection
opened within about 5 s of the previous one, and repeated resets have left
the Microseven stuck until a power cycle (B6, B53). Since 3.8.1 go2rtc keeps
these cameras connected (pre-warm, C26), and go2rtc redials a dropped source
after 1 s, five times, with no setting to change that (go2rtc 1.9.14,
internal/streams/producer.go). So AnyCam keeps one record per camera address
of when it may next be opened, and every opener takes a turn from it:

  * AnyCam itself, through _throttle_wait_if_needed (camera_discovery.py);
  * go2rtc, whose ffmpeg program is this file (ffmpeg.bin in AnyCam's inline
    go2rtc config). go2rtc runs it with ffmpeg's arguments. When the -i
    address belongs to a camera AnyCam recorded a cooldown for
    (set_cooldown), it waits for that camera's turn; then it replaces itself
    with the real ffmpeg (os.execvp), with the same arguments, so go2rtc's
    stop signal reaches ffmpeg directly. Every other ffmpeg run passes
    straight through.

3.8.2 (B65): 3.8.1 sent the gate to go2rtc as an exec: source through its
API, and go2rtc refuses API sources whose scheme is exec, echo or expr or that
contain a space (internal/streams/handlers.go, Validate); the Microseven lost
go2rtc. These cameras now use go2rtc's own ffmpeg: copy source, which the API
accepts, and the gate sits where go2rtc starts ffmpeg.

A turn is reserved under a file lock: the caller writes the moment it will
open, then sleeps until then, so two callers can never pick the same moment.
Times are time.monotonic(), which on Linux is one clock for every process.
Standard library only; this file imports nothing from AnyCam.
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

try:
    import fcntl            # Linux, the add-on's platform
except ImportError:         # Windows, where only the tests run
    fcntl = None

GATE_DIR = Path(tempfile.gettempdir()) / "anycam_gate"
# go2rtc gives a command it runs for an ffmpeg: source 30 s to start
# (internal/exec, starttimeout), and the API cannot change that for this source.
# A turn further away than this is not taken: the gate exits without ffmpeg,
# go2rtc counts a failed start and retries later. Turns are never squeezed
# together to fit, which would put two opens inside one cooldown.
MAX_WAIT_S = 25.0
_MEMORY: dict[str, dict] = {}     # used only when the record file cannot be written


def _path(ip: str) -> Path:
    return GATE_DIR / (ip.replace(":", "_").replace("/", "_") or "unknown")


def _update(ip: str, change) -> dict:
    """Read the address's record, apply change(record), write it back, under a lock."""
    try:
        GATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_path(ip).with_suffix(".lock"), "a+") as lock:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                try:
                    rec = json.loads(_path(ip).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    rec = {}
                change(rec)
                tmp = _path(ip).with_suffix(".tmp")
                tmp.write_text(json.dumps(rec), encoding="utf-8")
                os.replace(tmp, _path(ip))
            finally:
                if fcntl:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        return rec
    except OSError:
        rec = _MEMORY.setdefault(ip, {})
        change(rec)
        return rec


def cooldown_for(rec: dict, cooldown_s: float, now: float) -> float:
    """The cooldown in force: the brand's, or a longer escalated one while it lasts."""
    if rec.get("escalated_until", 0.0) > now:
        return max(cooldown_s, float(rec.get("escalated_cooldown", 0.0)))
    return cooldown_s


def reserve(ip: str, cooldown_s: float, max_wait: float | None = None) -> float | None:
    """Take the next turn to open the camera at `ip`; return the seconds to wait for it.

    With max_wait, a turn further away than that is not taken and None comes
    back: the caller must not open the camera now.
    """
    now = time.monotonic()
    out: dict = {}

    def change(rec: dict) -> None:
        gap = cooldown_for(rec, cooldown_s, now)
        last = float(rec.get("next", 0.0))
        turn = now if not last else max(now, last + gap)
        if max_wait is not None and turn - now > max_wait:
            out["wait"] = None
            return
        rec["next"] = turn
        out["wait"] = max(0.0, turn - now)
    _update(ip, change)
    return out.get("wait", 0.0)


def escalate(ip: str, cooldown_s: float, ttl_s: float) -> None:
    """Make every opener use `cooldown_s` for this address for the next `ttl_s` seconds."""
    until = time.monotonic() + ttl_s

    def change(rec: dict) -> None:
        rec["escalated_until"] = until
        rec["escalated_cooldown"] = cooldown_s
    _update(ip, change)


def set_cooldown(ip: str, cooldown_s: float) -> None:
    """3.8.2 (B65): record the cooldown go2rtc's ffmpeg runs for this camera must keep."""
    def change(rec: dict) -> None:
        rec["cooldown"] = cooldown_s
    _update(ip, change)


def cooldown_of(ip: str) -> float:
    """The cooldown AnyCam recorded for this address, or 0 when it has none."""
    return float(_update(ip, lambda rec: None).get("cooldown", 0.0) or 0.0)


def ffmpeg_input_host(args: list[str]) -> str:
    """The host of the RTSP address after -i in ffmpeg's arguments, or ''."""
    for i, arg in enumerate(args[:-1]):
        if arg == "-i" and args[i + 1].lower().startswith(("rtsp://", "rtsps://")):
            return urlsplit(args[i + 1]).hostname or ""
    return ""


def real_ffmpeg() -> str:
    """The ffmpeg program this file stands in for."""
    return shutil.which("ffmpeg") or "/usr/bin/ffmpeg"


def gate_main(args: list[str]) -> int:
    """Run as go2rtc's ffmpeg: wait for a gated camera's turn, then become ffmpeg."""
    try:
        host = ffmpeg_input_host(args)
        cooldown = cooldown_of(host) if host else 0.0
        if cooldown > 0:
            wait = reserve(host, cooldown, MAX_WAIT_S)
            if wait is None:
                print(f"anycam_gate: {host} has no turn within {MAX_WAIT_S:.0f} s; "
                      f"not opening it now", file=sys.stderr)
                return 3    # go2rtc counts a failed start and retries later
            if wait > 0:
                time.sleep(wait)
    except (OSError, ValueError, TypeError):
        pass                # a broken record must never stop ffmpeg from running
    ffmpeg = real_ffmpeg()
    os.execvp(ffmpeg, [ffmpeg] + list(args))
    return 1                # not reached: execvp replaces this process


if __name__ == "__main__":
    sys.exit(gate_main(sys.argv[1:]))
