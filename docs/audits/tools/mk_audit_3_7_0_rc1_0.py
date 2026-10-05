"""Audit report for AnyCam 3.7.0-rc1.0 — hardware decode diagnostics.

    python docs/audits/tools/mk_audit_3_7_0_rc1_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.7.0-rc1.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - hardware decode diagnostics - tag 3.7.0-rc1.0" % DATE,
                   S_SUB),
         p("Scope: phase 3.7.0 of the build plan: B11 and C9, diagnostics only. CrystalHeeler, "
           "2026-10-04: build the diagnostics; the fix needs the logs (CLAUDE.md rule 2). The "
           "last phase of the untested run, on top of 3.6.0-rc1.0. This build is not "
           "field-tested.")]

    s += [h1("1. What the diagnostics show"),
          table([["Part", "Design"],
                 ["Start-up report", "Every /dev/video*, /dev/media* and /dev/dri/renderD* "
                  "device inside the add-on: its V4L2 name from /sys/class/video4linux, and "
                  "whether it opens for reading and writing (else the system's reason, such as "
                  "\"Operation not permitted\"). Logged even with Hardware Decode off."],
                 ["C9", "No device named rpivid: a warning names the line dtoverlay=rpivid-v4l2 "
                  "for /boot/firmware/config.txt. A toggle is not possible from inside the "
                  "add-on: it cannot write the boot partition."],
                 ["B11", "A device that does not open is named, with the note that hardware "
                  "decode on it falls back to software."],
                 ["The first-frame line", "ffmpeg's error lines are read while it runs. A line "
                  "that says a hardware decoder did not open (a device or hwaccel subject with a "
                  "failure word) is kept for that camera's current start. The first picture is "
                  "then logged as \"decoded in SOFTWARE\" with ffmpeg's line, not as \"hw first "
                  "frame\"."],
                 ["/api/diagnostics/hw", "The report, the Hardware Decode setting, the decoder "
                  "candidates, those found unavailable, and each camera's software fallback."]],
                [1.4 * inch, 5.3 * inch]),
          p("Why this first: the field log behind B11 showed ffmpeg failing to open /dev/media0 "
            "to /dev/media3 with \"Operation not permitted\" while AnyCam logged hardware "
            "decode. The cause can be the add-on's device access, a Home Assistant OS change, "
            "or the ffmpeg version; the report separates these in one start-up log.")]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 17 files; build inputs "
                  "checked online."],
                 ["Python integration", verdict("PASS"), "568 of 568; 8 new: AH1 (the real "
                  "snapshot loop with a fake ffmpeg that falls back), AH2 to AH5."],
                 ["JavaScript behaviour", verdict("PASS"), "158 of 158, unchanged."],
                 ["Undefined names", verdict("PASS"), "17 files, 0."],
                 ["Not run", verdict("NOTED"), "No field test (CrystalHeeler's order). The "
                  "image was not built. The device report ran against stand-in devices, not a "
                  "Pi; the ffmpeg failure lines are from the B11 field log and ffmpeg's source "
                  "wording, not from this build."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, Parts 1 to 6. "
            "Rows this change touches; the rest carry from the 3.6.0-rc1.0 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST, contracts, undefined names",
                  verdict("PASS"), "17 files."],
                 ["1.4 Async", "Blocking I/O", verdict("PASS"),
                  "The device report runs in a thread."],
                 ["1.12 Logging", "Truth", verdict("PASS"),
                  "The log no longer claims hardware decode that did not run."],
                 ["1.18 Testing", "New tests", verdict("PASS"), "8 server checks."],
                 ["6.1 Security", "Devices", verdict("PASS"),
                  "Each device is opened and closed at once, without reading or writing; the "
                  "ffmpeg line is cleaned of passwords."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "No addresses or names."]],
                W_BP)]

    s += [h1("The untested run, complete"),
          table([["Release candidate", "Items", "Server / page checks"],
                 ["3.0.1-rc1.0", "B2, C10, B20, B23, B24, B26, B12, B8, F10, C11 (B3), C20", "471 / 118"],
                 ["3.1.0-rc1.0", "B25, C19, D3", "494 / 132"],
                 ["3.2.0-rc1.0", "C5, C6", "503 / 139"],
                 ["3.3.0-rc1.0", "C4, B15", "513 / 139"],
                 ["3.4.0-rc1.0", "C17", "532 / 158"],
                 ["3.5.0-rc1.0", "D1, D2", "542 / 158"],
                 ["3.6.0-rc1.0", "C14", "560 / 158"],
                 ["3.7.0-rc1.0", "B11, C9 (diagnostics)", "568 / 158"]],
                [1.4 * inch, 3.8 * inch, 1.5 * inch]),
          p("Each is its own commit, tag and zip; none is pushed or published.")]

    s += [h1("Known issues carried forward"),
          *bullets([
              "<b>B11:</b> hardware decode can still fall back to software; the fix waits for "
              "the 3.7.0-rc1.0 start-up log.",
              "<b>B6:</b> the Microseven waits until CrystalHeeler unlocks it.",
              "<b>C21:</b> the classic engine stays as the fallback."])]

    s += [h1("Acceptance (field test)"),
          *bullets([
              "The start-up log has one \"HW device\" line for each decoder device, and says "
              "whether each opens.",
              "Enhanced View on the 4K DVR channel in the classic view: the log says either "
              "\"hw first frame\" or \"decoded in SOFTWARE\" with a reason.",
              "/api/diagnostics/hw answers in the browser through Home Assistant."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["camera_discovery.py", "_hw_device_report, _hw_report_log, "
                  "api_diagnostics_hw; the probe logs the report; version 3.7.0-rc1.0."],
                 ["anycam_snap.py", "_hw_fallback_line; the stderr reader keeps the line; the "
                  "first-frame log."],
                 ["tests/", "AH1 in the snapshot loop section; section AH."],
                 ["CHANGELOG.md, config.yaml, docs", "3.7.0-rc1.0; build plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.7.0-rc1.0", story(), "anycam_3_7_0_rc1_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
