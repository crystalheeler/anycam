"""Audit report for AnyCam 3.7.5 — the 3.7.5-rc2.0 code released.

    python docs/audits/tools/mk_audit_3_7_5.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.7.5 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - tag 3.7.5 - the 3.7.5-rc2.0 code released" % DATE, S_SUB),
         p("Scope confirmed by CrystalHeeler on 2026-10-10: 3.7.5 is the 3.7.5-rc2.0 code with only the "
           "version changed, released as it is, then pushed and published. It carries everything since "
           "the last published version, 3.7.0: 3.7.1 to 3.7.4, 3.7.5-rc1.0 and 3.7.5-rc2.0. Each of those "
           "builds has its own audit report in docs/audits/.")]

    s += [h1("Changes in this build"),
          table([["#", "Change"],
                 ["Version", "3.7.5-rc2.0 to 3.7.5 in camera_discovery.py, config.yaml and the CHANGELOG.md heading."],
                 ["Changelog", "One 3.7.5 entry summarizing the user-visible changes since 3.7.0."],
                 ["Build plan", "3.7.5 published; next build 3.8.0-rc1.0 with B54 only."],
                 ["Code", "No other change."]],
                [1.4 * inch, 5.3 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates, run before and after the version change."],
                 ["Python integration", verdict("PASS"), "666 of 666."],
                 ["JavaScript behaviour", verdict("PASS"), "193 of 193."],
                 ["Not run", verdict("NOTED"), "No field test of 3.7.5-rc2.0 or 3.7.5: CrystalHeeler chose to "
                  "release as it is. The Microseven ffmpeg copy is untested: the camera needs a power cycle "
                  "first. 3.7.5-rc1.0 was installed in the field (Quality Switch report, 2026-10-08)."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>. This build changes only the version "
            "and the changelog, so the code rows carry from the 3.7.5-rc1.0 and 3.7.5-rc2.0 audits."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["6.3 Version control", "Version in every place", verdict("PASS"),
                  "Gate 4 and gate 5: code, manifest and changelog agree on 3.7.5."],
                 ["6.2 Dependencies", "Build inputs exist", verdict("PASS"), "Gate 7: apt pins, wheels, go2rtc digests."],
                 ["1.18 Testing", "Full suite", verdict("PASS"), "666 server and 193 page checks."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "Changelog and notes name devices by role only."]],
                W_BP)]

    s += [h1("Known issues"),
          *bullets(["<b>Microseven:</b> stuck until power-cycled; then the ffmpeg copy gets its test.",
                    "<b>B48:</b> go2rtc's local RTSP server does not ask local programs for a password.",
                    "<b>B33:</b> one Amcrest camera can get two cards."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["camera_discovery.py, config.yaml", "Version 3.7.5."],
                 ["CHANGELOG.md", "3.7.5 entry."],
                 ["docs/BUILD_PLAN.md, docs/BUILD_PLAN.html", "Release state; next build."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.7.5", story(), "anycam_3_7_5_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
