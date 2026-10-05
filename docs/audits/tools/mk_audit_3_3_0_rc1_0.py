"""Audit report for AnyCam 3.3.0-rc1.0 — one camera connection through go2rtc.

    python docs/audits/tools/mk_audit_3_3_0_rc1_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.3.0-rc1.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - one camera connection through go2rtc - tag "
                   "3.3.0-rc1.0" % DATE, S_SUB),
         p("Scope: phase 3.3.0 of the build plan: C4 and B15. CrystalHeeler approved C4 on "
           "2026-10-04 with this security condition: go2rtc's RTSP server on, listening on "
           "127.0.0.1 only, with a random password set at each start. Built in the untested "
           "run on top of 3.2.0-rc1.0. This build is not field-tested.")]

    s += [h1("1. Security review of the RTSP server"),
          table([["Control", "Before 3.3.0", "3.3.0"],
                 ["RTSP server", "Off (listen \"\")", "127.0.0.1:28554 only"],
                 ["Login", "-", "User anycam, a 24-byte random password from secrets, new "
                  "at each add-on start; never written to disk or sent to the page"],
                 ["Reach", "-", "Nothing outside the Pi: loopback only. On the Pi, a program "
                  "needs the password, which exists only in the add-on's memory and in its "
                  "ffmpeg arguments"],
                 ["Modules", "api, ws, rtsp, webrtc, mp4", "Unchanged: no exec, echo, expr or "
                  "ffmpeg"],
                 ["API", "127.0.0.1 only", "Unchanged"],
                 ["Release gate", "Pins listen \"\"", "Pins the 127.0.0.1 listen address and "
                  "the user and password keys"]],
                [1.2 * inch, 1.8 * inch, 3.7 * inch]),
          p("Remaining exposure: a process on the Pi that can read the add-on's process "
            "arguments can read the password. Such a process can already read the camera "
            "passwords in the same arguments, as before 3.3.0. Log lines pass the password "
            "filter, which removes the user and password from every URL.")]

    s += [h1("2. Who reads through go2rtc"),
          table([["User", "Before", "3.3.0"],
                 ["Live card, Enhanced View live", "go2rtc", "go2rtc, same stream names as "
                  "the users below for the same source"],
                 ["Card pictures and classic view (snapshot loop)", "Camera", "go2rtc's copy, "
                  "always over TCP"],
                 ["Motion detector (small stream)", "Camera", "go2rtc's copy"],
                 ["Recording buffer, and a recording without it (main stream)", "Camera",
                  "go2rtc's copy"],
                 ["HTTP snapshots, the H.265+ fallback, probes", "Camera", "Camera (not RTSP "
                  "streams held open)"]],
                [2.6 * inch, 0.8 * inch, 3.3 * inch]),
          *bullets([
              "One go2rtc stream per source: _go2rtc_shared_name hashes the camera id and the "
              "address without its password. A name never changes its source, so no user's "
              "stream is swapped under it, and the name that reaches the browser carries no "
              "password information.",
              "go2rtc dials the camera when the first user connects and holds one connection "
              "for all of them.",
              "Way back: a run with no picture or data counts against the camera; 3 in a row "
              "and AnyCam opens the camera directly again until the add-on restarts. A good "
              "run clears the count. go2rtc not running, or a stream it does not accept: "
              "direct at once."])]

    s += [h1("3. B15"),
          p("B15 was held back in 2.6.5 because keeping the card's stream while the classic "
            "view starts needed two camera connections, which rate-limited cameras and DVRs "
            "refuse. With C4 the classic view's ffmpeg reads go2rtc's copy: no second camera "
            "connection, and the camera stream stays open in go2rtc while the live card or "
            "motion detection use it. The snapshot loop itself still restarts at full "
            "resolution, as before.")]

    s += [h1("4. Recording"),
          p("The build plan named go2rtc's MP4 output for recordings. The recording buffer "
            "ffmpeg stays instead: it holds the 3 s before the motion, which go2rtc's MP4 "
            "output does not. It now reads go2rtc's copy, so the goal of C4, one camera "
            "connection, holds.")]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 15 files; build inputs "
                  "checked online; the go2rtc contract updated."],
                 ["Python integration", verdict("PASS"), "513 of 513; 10 new in section AD "
                  "against the fake go2rtc; the config check now expects the RTSP server."],
                 ["JavaScript behaviour", verdict("PASS"), "139 of 139, unchanged."],
                 ["Undefined names", verdict("PASS"), "15 files, 0."],
                 ["Not run", verdict("NOTED"), "No field test (CrystalHeeler's order). The "
                  "image was not built. A real go2rtc serving RTSP to ffmpeg did not run: "
                  "the tests use a fake go2rtc and a fake ffmpeg."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, Parts 1 to 6. "
            "Rows this change touches; the rest carry from the 3.2.0-rc1.0 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST, contracts, undefined names",
                  verdict("PASS"), "15 files; the undefined-name check caught one slip."],
                 ["1.10 Exceptions", "Failure paths", verdict("PASS"),
                  "A refused stream or go2rtc down means a direct connection."],
                 ["1.18 Testing", "New tests", verdict("PASS"), "10 server checks."],
                 ["6.1 Security", "New listener", verdict("PASS"),
                  "Loopback only, random password per start, pinned by the gate."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"),
                  "Tests use made-up addresses only."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets([
              "Recordings keep their buffer ffmpeg, now fed by go2rtc.",
              "<b>B6:</b> the Microseven waits until CrystalHeeler unlocks it.",
              "<b>C21:</b> the classic engine stays as the fallback."])]

    s += [h1("Acceptance (field test)"),
          *bullets([
              "The log shows no ffmpeg reading a camera address directly while go2rtc runs "
              "(only 127.0.0.1:28554).",
              "Test system B: an armed DVR channel records, with the 3 s before the motion; "
              "the classic view opens while the card plays.",
              "Test system A: the Hikvision PTZ card, Enhanced View and motion together.",
              "From another computer, port 28554 on the Pi does not answer."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_go2rtc.py", "RTSP server config; _go2rtc_shared_name, _go2rtc_relay, "
                  "_go2rtc_relay_result; the card uses the shared name."],
                 ["anycam_focus.py", "Enhanced View uses the shared name."],
                 ["anycam_snap.py, anycam_motion.py", "ffmpeg reads through _go2rtc_relay."],
                 ["camera_discovery.py", "Security model note; version 3.3.0-rc1.0."],
                 ["verify_release.py", "The go2rtc contract."],
                 ["tests/", "Section AD; the config check; go2rtc off after its tests."],
                 ["CHANGELOG.md, config.yaml, docs", "3.3.0-rc1.0; build plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.3.0-rc1.0", story(), "anycam_3_3_0_rc1_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
