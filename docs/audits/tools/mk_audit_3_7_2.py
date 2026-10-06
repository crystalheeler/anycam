"""Audit report for AnyCam 3.7.2 — card fallbacks, last picture, frozen hardware picture, login hints.

    python docs/audits/tools/mk_audit_3_7_2.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.7.2 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - card fallbacks, last picture, frozen hardware picture, "
                   "login hints - tag 3.7.2" % DATE, S_SUB),
         p("Scope confirmed by CrystalHeeler on 2026-10-06: version 3.7.2 with B35, B36, B39, B30, "
           "B40 (15 s) and B37 (detect and fall back, plus a diagnostic). Left out: the C items, "
           "B6, B33, B32 (to test on 3.7.2 first), B29 (CrystalHeeler's test later) and B38 (the "
           "Oak-D add-on's session). B11 moved to Done: Protection mode was the cause, confirmed "
           "in the field. Not field-tested.")]

    s += [h1("Changes"),
          table([["#", "Cause (from the logs)", "Change"],
                 ["B35", "The Oak-D card in LibreWolf stayed black. Each live attempt closed after "
                  "1 to 2 s; the card's timer counted only while a socket was open, and VideoRTC "
                  "waits up to 15 s to reconnect, so the timer never ran out.",
                  "A card counts the time its player is trying, the reconnect wait included. A "
                  "hidden page, an open Enhanced View and a player paused off screen do not count."],
                 ["B36", "Classic view of the Oak-D: \"entering enhanced view\", then \"idle 664s - "
                  "stopping\". A live card asks for no pictures, so the request time was old.",
                  "Entering the classic view marks the camera as just asked for."],
                 ["B39", "CrystalHeeler: after more than 60 s away, a card is black for about 2 s.",
                  "The live player keeps its last picture as the video's poster when its stream "
                  "closes or starts again (at most 1280 wide). MJPEG cards already kept theirs."],
                 ["B30", "Test system C: four failed logins with the user name left empty; the grey "
                  "\"admin\" hint looked like a value.", "No placeholder in the user name and "
                  "password fields of the three login forms."],
                 ["B40", "The 30 s limit came from the Oak-D add-on, whose encoder set no keyframe "
                  "interval. Normal settings: a keyframe every 1 to 2 s; Hikvision H.265+: 8 to "
                  "12 s (Hikvision's H.265+ white paper).", "15 s. A timeout names the camera "
                  "settings to change, on screen and in the add-on log (new POST /api/live_fail)."],
                 ["B37", "Lorex channel 7, Protection mode off: hevc_drm, then 2,183 JPEGs of exactly "
                  "48,806 bytes (green).", "After 50 hardware pictures of one size, software until "
                  "AnyCam restarts, and a one-time picture test of four ways (software, hardware "
                  "as now, no format filter, drm_prime with hwdownload) with the kernel's CMA "
                  "memory; also /api/diagnostics/hwtest/&lt;camera&gt;."]],
                [0.5 * inch, 3.1 * inch, 3.1 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 17 files; build inputs checked "
                  "online."],
                 ["Python integration", verdict("PASS"), "603 of 603; section AK new (22 checks); "
                  "G4 now expects 15 s."],
                 ["JavaScript behaviour", verdict("PASS"), "176 of 176; B35, B39 and B40 checks "
                  "new; the watchdog section moved to 15 s."],
                 ["Not run", verdict("NOTED"), "No field test. The picture test ran only against "
                  "a stand-in ffmpeg: its hardware ways are unproven on a Pi. The poster capture "
                  "was not watched in a real browser."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>. Rows this change "
            "touches; the rest carry from the 3.7.0 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.3 Async", "Blocking calls", verdict("PASS"),
                  "/proc/meminfo is read through asyncio.to_thread."],
                 ["1.10 Exceptions", "Task cleanup", verdict("PASS"),
                  "asyncio.wait for the stderr reader: a reader cancelled before it ran no "
                  "longer reads as the loop cancelled."],
                 ["1.12 Logging", "Truth", verdict("PASS"),
                  "Live view fallbacks and frozen hardware pictures are now in the add-on log."],
                 ["1.18 Testing", "New tests", verdict("PASS"), "22 server and 9 page checks."],
                 ["6.1 Security", "Input at the boundary", verdict("PASS"),
                  "/api/live_fail takes known cameras and two places only; the reason is cut "
                  "to one line of 200 characters. The picture test strips passwords."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "Made-up addresses only."]],
                W_BP)]

    s += [h1("Known issues"),
          *bullets(["<b>B37:</b> the hardware picture is still wrong; the fix waits for the "
                    "\"HW TEST\" lines from a field log.",
                    "<b>B32:</b> the Microseven waits on \"Loading feed\" while its RTSP is "
                    "broken; to test on 3.7.2.",
                    "<b>B33:</b> one Amcrest camera can get two cards; under investigation.",
                    "<b>B29:</b> sound, waits for CrystalHeeler's test."])]

    s += [h1("Acceptance (field test)"),
          *bullets(["LibreWolf: the Oak-D card shows still pictures within 15 s.",
                    "Open the Oak-D's classic view after its live card: pictures start.",
                    "Leave the page for 2 minutes and come back: each card shows its last "
                    "picture until live video starts.",
                    "Protection mode off, Lorex channel 7 in the classic view: the log shows "
                    "the frozen-picture line, the view turns to software, and \"HW TEST\" lines "
                    "follow."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_snap.py", "Frozen hardware detection, the picture test and its route "
                  "handler; the stderr reader wait."],
                 ["anycam_focus.py", "B36: the request time on classic entry."],
                 ["anycam_go2rtc.py", "B40: /api/live_fail."],
                 ["page_script.py", "B35, B39, B40; B30 in the card's login form."],
                 ["anycam_page.py", "B30 in the two dialogs."],
                 ["camera_discovery.py", "Two routes; the decoder report lists frozen decoders "
                  "and picture tests; version 3.7.2."],
                 ["tests/", "Section AK; page checks."],
                 ["CHANGELOG.md, config.yaml, docs", "3.7.2; build plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.7.2", story(), "anycam_3_7_2_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
