"""Audit report for AnyCam 3.0.0-rc1.3 — probe_http_identity restored (B19).

    python docs/audits/tools/mk_audit_3_0_0_rc1_3.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.0.0-rc1.3 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - probe_http_identity restored - tag 3.0.0-rc1.3" % DATE, S_SUB),
         p("3.0.0-rc1.3 carries one change, build plan B19. Scope confirmed by CrystalHeeler "
           "on 2026-10-02: B19 alone, after 3.0.0-rc1.2 passed his field test on both "
           "systems; the unused is_camera_positive and probe_http_for_camera stay. "
           "camera_discovery.py: 6 lines added, 2 removed (the def line, a 4-line comment, blank lines).")]

    s += [h1("The defect and the fix"),
          table([["Fact", "Detail"],
                 ["Cause", "Commit 2.4.0-rc1.0 added _rtsp_options_fingerprint directly "
                  "above probe_http_identity and lost the line 'def "
                  "probe_http_identity(ip: str, port: int, timeout: int = 5) -> dict:'. "
                  "The 117-line body stayed as unreachable code after that function's "
                  "return."],
                 ["Fix", "The def line is back in the same place."],
                 ["Proof of identity", "The restored function's source equals the "
                  "function in the commit before 2.4.0-rc1.0, compared as text by the "
                  "restore script, which refuses to write otherwise."],
                 ["Callers", "run_scan, in the branch for a device that answers ONVIF "
                  "discovery and has no card from the port scan. probe_http_for_camera, "
                  "whose only caller is_camera_positive has no caller."],
                 ["Effect on the scan", "For such a device the scan now requests its web "
                  "page on port 80 (several paths, http then https, 4 s timeout each) "
                  "and takes the brand from the title or Server header."]],
                [1.3 * inch, 5.4 * inch])]

    s += [h1("Expected effect on the two test systems"),
          table([["Evidence", "Value"],
                 ["Logs of 2026-10-02, both systems", "8 scans, 2,849 DEBUG lines"],
                 ["Times the step ran (its failure logs at DEBUG)", "0"],
                 ["Reason", "Every ONVIF device there (10.0.0.22, 10.0.0.33, "
                  "192.168.50.217) already had a card from the port scan"],
                 ["Expectation", "The same devices and cards as the scans of 10:40"]],
                [3.3 * inch, 3.4 * inch]),
          p("Correction carried from the 3.0.0-rc1.0 audit: that report first said "
            "the step failed on every device and that a second step failed too. Both "
            "statements were wrong and were corrected the same day.")]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 7 files; 212 top-level "
                  "definitions, no duplicates; 48 contracts."],
                 ["Undefined names", verdict("PASS"), "7 files, 0 undefined names. The "
                  "list of known defects is empty for the first time."],
                 ["Python integration", verdict("PASS"), "294 of 294, 5 new: a local web "
                  "server with a Microseven title and a Hipcam Server header is "
                  "identified as Hipcam/Microseven; an ordinary page is not a camera; "
                  "the wrapper; a closed port; _rtsp_options_fingerprint now ends at "
                  "its return."],
                 ["JavaScript behaviour", verdict("PASS"), "110 of 110."],
                 ["Not run", verdict("NOTED"), "No scan was run on the build PC. No test "
                  "drives run_scan into the ONVIF-only branch; the scan has no tests "
                  "yet (E1). The field test (A17) compares the scan logs."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, Parts 1 to "
            "6. Rows this change touches; the rest carry from the 3.0.0-rc1.2 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST, contracts, undefined names",
                  verdict("PASS"), "B19 closed. This row failed in the three audits "
                  "before this one."],
                 ["1.2 Syntax", "compile() per file", verdict("PASS"), "7 files."],
                 ["1.10 Exceptions", "Broad catches", verdict("NOTED"),
                  "The restored function and its caller use 'except Exception'. That "
                  "is what hid the defect at DEBUG level. Not changed here: one change "
                  "per build."],
                 ["1.16 Refactoring", "Dead code", verdict("NOTED"),
                  "is_camera_positive and probe_http_for_camera are unused (about 150 "
                  "lines); kept at CrystalHeeler's request."],
                 ["1.18 Testing", "Regression test", verdict("PASS"),
                  "5 checks, with a real HTTP server on localhost."],
                 ["6.1 Security", "New outbound requests", verdict("NOTED"),
                  "HTTP GET to a discovered device's port 80, certificate checks off "
                  "(cameras use self-signed certificates), 16 KB read limit, timeout."],
                 ["6.3 Version control", "Commits and gate", verdict("PASS"),
                  "One commit; the gate run before it; tag after."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets([
              "<b>B20</b> (late pictures, sub-stream retries): right after 3.0.0.",
              "<b>C17</b> (detection zones): requirements saved, design discussion next.",
              "<b>E1:</b> the scan, password entry, the snapshot loop and the Enhanced "
              "View engine are still in the main file and have no tests.",
              "The Lorex DVR answers 404 for the sub-stream of its channels.",
              "Skip Non-Reference Frames breaks H.264 cameras (B3)."])]

    s += [h1("Acceptance (field test A17)"),
          *bullets([
              "After the update, the start-up scan finds the same devices and cards "
              "as on 2026-10-02 10:40, on both systems.",
              "No line with 'not defined' in the log.",
              "A line 'HTTP identity <ip>: ...' is new and means the step ran."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["camera_discovery.py", "The def line of probe_http_identity. Version "
                  "3.0.0-rc1.3."],
                 ["tests/", "5 new checks; the known-defects list emptied."],
                 ["docs", "Build plan; detection zones requirements "
                  "(Detection_Zones_Plan.md)."],
                 ["CHANGELOG.md", "3.0.0-rc1.3 entry; earlier entries corrected."]],
                W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.0.0-rc1.3", story(), "anycam_3_0_0_rc1_3_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
