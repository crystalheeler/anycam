"""3.8.1 (C26): the connection gate for cameras that limit how often they are opened.

A Hipcam-family camera (Microseven, Sricam, Vstarcam, Wansview, Tenvis:
throttle_type "rate_limit_per_ip_tcp" in CAMERA_DB) resets a connection
opened within about 5 s of the previous one, and repeated resets have left
the Microseven stuck until a power cycle (B6, B53). Since 3.8.1 go2rtc keeps
these cameras connected (pre-warm, C26), and go2rtc redials a dropped source
after 1 s, five times, with no setting to change that (go2rtc 1.9.14,
internal/streams/producer.go). So AnyCam keeps one record per camera address
of when it may next be opened, and every opener takes a turn from it:

  * AnyCam itself, through _throttle_wait_if_needed (camera_discovery.py);
  * go2rtc, which for these cameras runs this file as its source command:

        python3 /anycam_gate.py <address> <cooldown s> ffmpeg ... {output}

    The script waits for its turn, then replaces itself with ffmpeg
    (os.execvp), so go2rtc's stop signal reaches ffmpeg directly. go2rtc
    starts its retries one at a time, so no two opens can fall inside the
    cooldown, however fast go2rtc retries.

A turn is reserved under a file lock: the caller writes the moment it will
open, then sleeps until then, so two callers can never pick the same moment.
Times are time.monotonic(), which on Linux is one clock for every process.
Standard library only; this file imports nothing from AnyCam.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

try:
    import fcntl            # Linux, the add-on's platform
except ImportError:         # Windows, where only the tests run
    fcntl = None

GATE_DIR = Path(tempfile.gettempdir()) / "anycam_gate"
MAX_WAIT_S = 45.0           # go2rtc waits 60 s for a gated source (starttimeout)
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


def reserve(ip: str, cooldown_s: float) -> float:
    """Take the next turn to open the camera at `ip`; return the seconds to wait for it."""
    now = time.monotonic()
    out = {}

    def change(rec: dict) -> None:
        gap = cooldown_for(rec, cooldown_s, now)
        last = float(rec.get("next", 0.0))
        turn = now if not last else max(now, last + gap)
        turn = min(turn, now + MAX_WAIT_S)
        rec["next"] = turn
        out["wait"] = turn - now
    _update(ip, change)
    return max(0.0, out.get("wait", 0.0))


def escalate(ip: str, cooldown_s: float, ttl_s: float) -> None:
    """Make every opener use `cooldown_s` for this address for the next `ttl_s` seconds."""
    until = time.monotonic() + ttl_s

    def change(rec: dict) -> None:
        rec["escalated_until"] = until
        rec["escalated_cooldown"] = cooldown_s
    _update(ip, change)


def gate_main(argv: list[str]) -> int:
    """go2rtc's source command: wait for the turn, then become the command."""
    if len(argv) < 3:
        print("usage: anycam_gate.py <address> <cooldown s> <command> [args...]", file=sys.stderr)
        return 2
    ip, cooldown, command = argv[0], float(argv[1]), argv[2:]
    wait = reserve(ip, cooldown)
    if wait > 0:
        time.sleep(wait)
    os.execvp(command[0], command)
    return 1                # not reached: execvp replaces this process


if __name__ == "__main__":
    sys.exit(gate_main(sys.argv[1:]))
