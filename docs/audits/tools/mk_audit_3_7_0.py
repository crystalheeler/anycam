"""Audit report for AnyCam 3.7.0 — the release of 3.7.0-rc2.0.

    python docs/audits/tools/mk_audit_3_7_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.7.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - release of 3.7.0-rc2.0 - tag 3.7.0" % DATE, S_SUB),
         p("Scope confirmed by CrystalHeeler on 2026-10-06: 3.7.0 is exactly the code of "
           "3.7.0-rc2.0, which CrystalHeeler installed and accepted on 2026-10-05 (\"I'm happy with "
           "the results of my testing\"). Only the version number and the changelog change. "
           "CrystalHeeler ordered the build, the push and the publish in one message.")]

    s += [h1("What 3.7.0 holds"),
          table([["Release candidate", "Items", "Audit"],
                 ["3.0.1-rc1.0", "B2, C10, B20, B23, B24, B26, B12, B8, F10, C11 (B3), C20",
                  "anycam_3_0_1_rc1_0"],
                 ["3.1.0-rc1.0", "B25, C19, D3", "anycam_3_1_0_rc1_0"],
                 ["3.2.0-rc1.0", "C5, C6", "anycam_3_2_0_rc1_0"],
                 ["3.3.0-rc1.0", "C4, B15", "anycam_3_3_0_rc1_0"],
                 ["3.4.0-rc1.0", "C17", "anycam_3_4_0_rc1_0"],
                 ["3.5.0-rc1.0", "D1, D2", "anycam_3_5_0_rc1_0"],
                 ["3.6.0-rc1.0", "C14", "anycam_3_6_0_rc1_0"],
                 ["3.7.0-rc1.0", "B11 and C9 diagnostics", "anycam_3_7_0_rc1_0"],
                 ["3.7.0-rc2.0", "B28, C22, C24, D4", "anycam_3_7_0_rc2_0"]],
                [1.3 * inch, 3.9 * inch, 1.5 * inch]),
          p("Each release candidate's audit holds its design, security review, tests and "
            "best-practices table; this report does not repeat them.")]

    s += [h1("Field test"),
          table([["Build", "Who, when", "Result"],
                 ["3.7.0-rc1.0", "CrystalHeeler, 2026-10-05, test system B, phone and computer",
                  "Working; zone drawing \"works perfectly\"; 7 findings, entered as B27, B28, "
                  "C22 to C25, D4."],
                 ["3.7.0-rc2.0", "CrystalHeeler, 2026-10-05", "Installed (uninstall and install; "
                  "Home Assistant's Update button stayed grey, with no request reaching the "
                  "Supervisor) and accepted."]],
                [1.2 * inch, 2.4 * inch, 3.1 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 17 files; build inputs checked "
                  "online."],
                 ["Python integration", verdict("PASS"), "572 of 572."],
                 ["JavaScript behaviour", verdict("PASS"), "166 of 166."],
                 ["Privacy history check", verdict("PASS"), "Every reachable commit, tag and file "
                  "checked before the push."],
                 ["Not run", verdict("NOTED"), "Test system A (the Hikvision PTZ, the Microseven) "
                  "is not named in the field test. Not used in the field: Remote Storage to a "
                  "real server, WebRTC and RTSP-over-WebSocket cameras (none on either system), "
                  "sound. Not tested at all: the Microseven (locked, B6)."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, Parts 1 to 6. The code "
            "is that of 3.7.0-rc2.0; see the release candidate audits for each row. This release "
            "changes only the version number and the changelog."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 to 1.3", "AST, compile, undefined names", verdict("PASS"), "17 files."],
                 ["1.18 Testing", "Suites", verdict("PASS"), "572 server, 166 page checks."],
                 ["6.2 Dependencies", "Pins", verdict("PASS"), "Checked online by the gate."],
                 ["6.3 Version control", "Version in all places", verdict("PASS"),
                  "camera_discovery.py, config.yaml, the changelog heading, the tag."],
                 ["Rule 9 (privacy)", "History check", verdict("PASS"), "0 problems."]],
                W_BP)]

    s += [h1("Known issues"),
          *bullets([
              "<b>B11:</b> hardware decode falls back to software: the add-on may not open any "
              "decoder device (\"Operation not permitted\"). The start-up report's overlay "
              "warning is false: the decoder is named rpi-hevc-dec on this kernel.",
              "<b>B27:</b> Enhanced View is black for a few seconds after a return to the page.",
              "<b>C23:</b> the Storage tab hides the file names on a phone held upright.",
              "<b>C25:</b> a better on/off design for Remote Storage.",
              "<b>B6:</b> the Microseven is not tested."])]

    s += [h1("Files changed from 3.7.0-rc2.0"),
          table([["File", "Change"],
                 ["camera_discovery.py, config.yaml", "Version 3.7.0."],
                 ["CHANGELOG.md", "The 3.7.0 entry."],
                 ["docs/BUILD_PLAN.md, .html", "Current release 3.7.0."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.7.0", story(), "anycam_3_7_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
