"""Audit report for AnyCam 3.8.1 — pre-warm (C26) and the card after Enhanced View (B61).

    python docs/audits/tools/mk_audit_3_8_1.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.8.1 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - pre-warm + connection gate + B61 - tag 3.8.1" % DATE, S_SUB),
         p("CrystalHeeler reported on 2026-10-13 and 2026-10-15 that cards and Enhanced View take "
           "5-10 s to open, and that a card takes about 19 s to come back after Enhanced View. Scope, "
           "agreed 2026-10-15: pre-warm through go2rtc's own preload for the main and sub stream of "
           "every camera (C26), a connection gate for rate-limited cameras, and B61. Sub-stream-first "
           "is not in scope.")]

    s += [h1("Design"),
          table([["Part", "What it does"],
                 ["Pre-warm (C26)", "Every 30 s AnyCam makes go2rtc's preloads match the cameras: the main "
                  "stream and the card stream of each RTSP camera (PUT /api/preload). go2rtc's preload "
                  "consumer only counts bytes, so nothing is decoded. Not preloaded: cameras that need a "
                  "password, information and appliance cards, stuck cameras (B6), and a camera AnyCam has "
                  "seen resetting connections (ACD) that is not behind the gate."],
                 ["Watchdog", "A preloaded stream whose received bytes do not grow for 2 passes (about "
                  "60 s) has its camera's preloads deleted, so go2rtc stops redialing it; it is tried again "
                  "after 10 minutes."],
                 ["Connection gate", "go2rtc redials a dropped source after 1 s, five times, with no "
                  "setting (internal/streams/producer.go, unchanged in go2rtc's main branch). For brands "
                  "with throttle_type rate_limit_per_ip_tcp (Hipcam/Microseven, Sricam, Vstarcam, Wansview, "
                  "Tenvis), go2rtc's source is anycam_gate.py: it takes the camera's turn from a record "
                  "under a file lock, sleeps until then, and replaces itself with go2rtc's own ffmpeg copy "
                  "command (os.execvp). AnyCam's own openings take turns from the same record, and an ACD "
                  "escalation (30 s) is written into it."],
                 ["B61", "The card's ffmpeg reads go2rtc's copy; for a gated camera that open goes to "
                  "127.0.0.1, so the 5 s brand wait and back-off floor no longer apply to it. The live view "
                  "proxy's own 5 s wait is removed: the gate waits at the real open."],
                 ["Setting", "prewarm (default on), in config.yaml, run.sh and translations/en.yaml."]],
                [1.5 * inch, 5.1 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates. Contracts moved: the brand cooldown "
                  "before go2rtc dials a rate-limited camera is now pinned on the go2rtc source and the "
                  "gate (_go2rtc_camera_src, _go2rtc_gated_src, gate_main), not on the proxy."],
                 ["Python integration", verdict("PASS"), "711 of 711. New section AQ: the gate's turns and "
                  "escalation, the gated source split as go2rtc 1.9.14 splits it, the shared record, which "
                  "streams are preloaded, passes against a fake go2rtc (add, stall, pause, return, remove), "
                  "B61 in the snapshot loop, the setting. AM1 updated for the gated source."],
                 ["JavaScript behaviour", verdict("PASS"), "193 of 193."],
                 ["Gate as a process", verdict("PASS"), "Run as a real process on the build PC with a "
                  "harmless command: the second run waited its turn (1.55 s against 0.12 s), and the "
                  "command it was given ran."],
                 ["Field", verdict("NOTED"), "Not run yet; see the next section."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Not tested"),
          *bullets(["<b>go2rtc running the gate on the Pi.</b> No Linux system or go2rtc binary on the "
                    "build PC. The source string is checked against go2rtc's own parsing rules (cut at "
                    "'#', QuoteSplit, {output}), not against go2rtc itself.",
                    "<b>The file lock.</b> fcntl exists only on Linux; on the build PC the lock is skipped "
                    "and the tests run in one process.",
                    "<b>Pre-warm against the real cameras and the DVR.</b> The DVR would hold up to 14 "
                    "connections; its limit is not known.",
                    "<b>The B61 time on a phone.</b> The 19 s gap is expected to shrink to the keyframe wait "
                    "plus the ffmpeg start; to be measured in the field."])]

    s += [h1("Best-practices compliance"),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.10 Exceptions", "Narrow excepts", verdict("PASS"),
                  "The pass loop catches aiohttp, timeout and JSON errors only."],
                 ["1.3 Async", "No blocking I/O on the loop", verdict("PASS"),
                  "The gate record is read with asyncio.to_thread."],
                 ["1.12 Logging", "Truth", verdict("PASS"),
                  "Pre-warm on, off and paused are logged per camera, with the reason."],
                 ["6 Security", "No new exposure", verdict("PASS"), "Sources still come only from AnyCam; "
                  "the camera address reaches the command line as go2rtc's own ffmpeg copy already did."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "Made-up addresses only."]],
                W_BP)]

    s += [h1("Known issues"),
          *bullets(["<b>Phone live view (B62):</b> where WebRTC does not connect, MSE can fall behind; "
                    "under investigation.",
                    "<b>Extra connections:</b> each camera holds one or two all the time; the prewarm "
                    "setting turns them off.",
                    "<b>Microseven:</b> a camera in a locked state needs a power cycle to recover."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_gate.py (new)", "The connection gate: shared record, escalation, go2rtc command."],
                 ["anycam_go2rtc.py", "Gated source, pre-warm passes and watchdog, supervisor start/stop, "
                  "no wait in the proxy, _go2rtc_will_relay."],
                 ["camera_discovery.py", "Turns from the gate's record; ACD escalation written to it; "
                  "CFG_PREWARM; 3.8.1."],
                 ["anycam_snap.py", "B61: no brand wait or floor for a gated camera read through go2rtc."],
                 ["config.yaml, run.sh, translations/en.yaml", "The prewarm setting."],
                 ["anycam_modules.py, Dockerfile", "anycam_gate.py added."],
                 ["verify_release.py", "Contracts moved to the go2rtc source and the gate."],
                 ["tests/", "Section AQ; AM1 updated."],
                 ["CHANGELOG.md, CLAUDE.md, docs", "3.8.1; build plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.8.1", story(), "anycam_3_8_1_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
