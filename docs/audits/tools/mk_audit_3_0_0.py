"""Audit report for AnyCam 3.0.0 — the release of the 3.0.0-rc1.x series.

    python docs/audits/tools/mk_audit_3_0_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.0.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - the 3.0.0-rc1.x series as a release - tag 3.0.0" % DATE,
                   S_SUB),
         p("CrystalHeeler, 2026-10-02: \"rc1.5 testing is complete. it works\", with the order to "
           "push and publish 3.0.0. 3.0.0 is the code of 3.0.0-rc1.5 with the version number "
           "changed in camera_discovery.py, config.yaml and CHANGELOG.md. No other line of code changed. "
           "Each release candidate has its own audit report; this report covers the series against "
           "2.6.8, the last published release.")]

    s += [h1("1. The series"),
          table([["Build", "Content", "Field test"],
                 ["3.0.0-rc1.0", "Tests in the repository and gate 8 (E7); three unused endpoints "
                  "removed (E6, E8); credentials removed from every log line (E4); camera tables and "
                  "page script in their own files", "A14: stable"],
                 ["3.0.0-rc1.1", "Motion, recording, night boost; the Storage tab", "A15: working"],
                 ["3.0.0-rc1.2", "go2rtc", "A16: stable on both systems"],
                 ["3.0.0-rc1.3", "probe_http_identity restored (B19)", "A17: scans unchanged"],
                 ["3.0.0-rc1.4", "Empty page text (B21); scan tests; the scan and the probers",
                  "A18: working on both systems"],
                 ["3.0.0-rc1.5", "Cancel fixed (B22); type hints complete; tests and moves for "
                  "password entry, the snapshot loop, the Enhanced View engine, brand "
                  "identification, the page builder", "A19: works on both systems"]],
                [1.0 * inch, 4.2 * inch, 1.5 * inch])]

    s += [h1("2. Against 2.6.8"),
          table([["Measure", "2.6.8", "3.0.0"],
                 ["Python files of the add-on", "1", "14"],
                 ["Lines in camera_discovery.py", "18,198", "1,797"],
                 ["Tests in the repository", "none", "448 server, 114 page, undefined-name check"],
                 ["Release gates", "7", "8 (gate 8 runs the tests)"],
                 ["Functions without full type hints", "27 (E5 count)", "0"],
                 ["Behaviour changes", "-", "Cancel works (B22); brand lookup from a web page works "
                  "(B19); passwords with @ fully removed from addresses; empty page text (B21); "
                  "three unused endpoints removed"]],
                [2.2 * inch, 1.5 * inch, 3.0 * inch]),
          p("Every move kept the moved code unchanged: after each one, all definitions were "
            "compared text for text with the code before it (357 to 363), and the built page and "
            "the routes were identical.")]

    s += [h1("3. Field evidence for 3.0.0-rc1.5"),
          table([["Check", "Result"],
                 ["Error lines", "0 'not defined', 0 'has not run yet', 0 tracebacks, 0 ERROR "
                  "lines in the logs from both systems."],
                 ["Password entry", "Accepted on both systems. The DVR's 7 channel cards were "
                  "registered in 3 s."],
                 ["Live cards", "The DVR channel cards after CrystalHeeler fixed the DVR's "
                  "sub-stream setting; the PTZ card after its password was entered again (B25)."],
                 ["Motion", "Live detection on the two armed DVR channels."],
                 ["Cancel", "Confirmed by CrystalHeeler."]],
                [1.6 * inch, 5.1 * inch])]

    s += [h1("Tests run for this release"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates, including the build inputs online."],
                 ["Python integration", verdict("PASS"), "448 of 448."],
                 ["JavaScript behaviour", verdict("PASS"), "114 of 114."],
                 ["Undefined names", verdict("PASS"), "14 files, 0."],
                 ["Packaged zip", verdict("PASS"), "Imported from its own folder."],
                 ["Not run", verdict("NOTED"), "No new field test: the code is that of 3.0.0-rc1.5, "
                  "field-tested as A19. The image was not built on the build PC."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, Parts 1 to 6. The code "
            "is that of 3.0.0-rc1.5; its audit holds the full table. Rows for this release:"),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1, 1.2 AST and syntax", "Gate 1", verdict("PASS"), "14 files."],
                 ["1.14 Type checking", "AST count", verdict("PASS"), "0 functions missing a type."],
                 ["1.18 Testing", "Gate 8", verdict("PASS"), "448 and 114 checks."],
                 ["6.3 Version control", "Version in every place", verdict("PASS"),
                  "Code, manifest and changelog heading in one commit; tag 3.0.0."],
                 ["Rule 9 (privacy)", "Release text", verdict("PASS"),
                  "Changelog, release notes and this report: no names, places or addresses."]],
                W_BP)]

    s += [h1("Known issues"),
          *bullets([
              "<b>B23:</b> adding a camera by hand with protocol WebRTC fails.",
              "<b>B24:</b> one password-entry step does not pace rate-limited cameras.",
              "<b>B25:</b> a camera's streams are read only at password entry; enter the password "
              "again after changing a camera's stream settings.",
              "<b>B20</b> (late pictures when a sub-stream fails): fixes approved, not built.",
              "Skip Non-Reference Frames breaks H.264 cameras (B3); the toggle is to be removed (C11)."])]

    s += [h1("Files changed from 3.0.0-rc1.5"),
          table([["File", "Change"],
                 ["camera_discovery.py, config.yaml", "Version 3.0.0."],
                 ["CHANGELOG.md", "The 3.0.0 entry."],
                 ["docs/BUILD_PLAN.md and .html", "A19 and E1 done; 3.0.0 is the current release."],
                 ["docs/audits/", "This report."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.0.0", story(), "anycam_3_0_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
