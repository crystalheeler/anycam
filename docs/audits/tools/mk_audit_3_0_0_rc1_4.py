"""Audit report for AnyCam 3.0.0-rc1.4 — empty page text, scan tests, the scan's move.

    python docs/audits/tools/mk_audit_3_0_0_rc1_4.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.0.0-rc1.4 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - empty page text, scan tests, scan split - tag "
                   "3.0.0-rc1.4" % DATE, S_SUB),
         p("Scope confirmed by CrystalHeeler on 2026-10-02: three things in one build. "
           "(1) Build plan B21, the empty page text. (2) Tests for the scan. (3) The "
           "scan's move to its own files, build plan E1. Item 1 changes what the page "
           "shows. Items 2 and 3 change no behaviour.")]

    s += [h1("1. Empty page text (B21)"),
          table([["State", "Text"],
                 ["No cameras, a scan is running", "No Cameras Found Yet"],
                 ["No cameras, no scan running", "No cameras found. Click Scan Network to "
                  "discover cameras on your subnet. (unchanged)"]],
                [2.4 * inch, 4.3 * inch]),
          p("The scan status poll, which already shows and hides the Cancel button, "
            "sets the text. 6 checks: 2 on the page markup, 4 on the script.")]

    s += [h1("2. Tests for the scan"),
          p("The scan had no tests. 25 checks now run the real run_scan and "
            "_probe_host_port. Stand-ins replace only the functions that touch the "
            "network: ARP, multicast discovery, nmap, and the single-port probers."),
          table([["Checks", "What they pin"],
                 ["U1-U2", "Every live host goes to the port scan, sorted; the gateway, "
                  "Docker and link-local addresses do not. The RTSP port is probed "
                  "first; a blacklisted port is skipped; after the RTSP port answers, "
                  "the host's other ports skip the RTSP path walk."],
                 ["U3-U4", "Saved cameras stay, unsaved old cards go. One card for each "
                  "device. A device found by the port scan and by ONVIF keeps one card."],
                 ["U5", "A device found by ONVIF only: a password card; its brand read "
                  "from its web page (the function restored in 3.0.0-rc1.3)."],
                 ["U6-U7", "End state: not running, 100%, counts, one save. A failure "
                  "inside the scan is reported and the scan is not left running."],
                 ["U8-U12", "The port prober: RTSP open, RTSP needing a password, MJPEG, "
                  "HLS, nothing found, not a camera, RTMP; the order of the probers."],
                 ["U13", "The status and cancel endpoints."]],
                [0.9 * inch, 5.8 * inch])]

    s += [h1("Defect found while writing the tests (build plan B22)"),
          p("The Cancel endpoint sets a flag named _SCAN_CANCELLED. The scan reads "
            "SCAN_CANCELLED, a different name that the scan creates itself. The "
            "status line changes to 'Cancelling scan…' and the scan runs to its end. "
            "Not fixed in this build: the scope was fixed at three items, and the fix "
            "changes behaviour. No test pins the broken behaviour.")]

    s += [h1("3. The move"),
          table([["File", "Content", "Lines"],
                 ["anycam_probe.py", "26 functions, 8 tables: the RTSP probe and path "
                  "walks, MJPEG, HLS, RTMP, WebRTC, the RTSP fingerprint, the HTTP "
                  "identity check, the ONVIF calls", "2,657"],
                 ["anycam_scan.py", "18 functions, 5 tables and state objects: subnet "
                  "and gateway, ARP, SSDP, mDNS and ONVIF discovery, nmap, the port "
                  "prober, the scan, the verification scan, the port scanner", "2,040"],
                 ["camera_discovery.py", "11,303 lines in 3.0.0-rc1.3", "6,780"]],
                [1.5 * inch, 4.2 * inch, 1.0 * inch]),
          table([["Link", "Detail"],
                 ["anycam_scan.py to anycam_probe.py", "Direct import of 15 prober "
                  "functions. anycam_probe.py imports nothing from anycam_scan.py."],
                 ["Values replaced at run time", "ARP_HOSTS and PENDING_CAMERAS moved "
                  "with run_scan, the only function that replaces them. "
                  "camera_discovery.py reads anycam_scan.ARP_HOSTS (1 place) and "
                  "anycam_scan.PENDING_CAMERAS (4 places)."],
                 ["Left in the main file", "The manufacturer database (OUI). The "
                  "probers and the scan both use it; moving it with the scan would "
                  "make the two new files import each other."],
                 ["NEEDS lists", "67 names across all split files, checked by the gate."]],
                [2.0 * inch, 4.7 * inch])]

    s += [h1("Evidence that the move changed nothing"),
          table([["Check", "Result"],
                 ["Every definition, text for text", "363 before, 363 after; 0 missing, "
                  "0 changed, apart from the anycam_scan. prefix on the two values "
                  "above. 175 in the main file, 34 in anycam_probe.py, 23 in "
                  "anycam_scan.py."],
                 ["Built page and routes", "Same page hash; the same 56 routes."],
                 ["The scan tests", "Written and passing before the move; passing "
                  "unchanged after it."],
                 ["Packaged zip", "Imported from its own folder."]],
                [1.9 * inch, 4.8 * inch]),
          p("The first attempt at the second move stopped half way, on a Dockerfile "
            "pattern in the split tool. The repository was reset to the last commit "
            "and both moves were run again with the corrected tool.")]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 9 files compiled; module "
                  "list matches the imports and the Dockerfile; 212 top-level "
                  "definitions; 48 contracts; 67 NEEDS names."],
                 ["Python integration", verdict("PASS"), "321 of 321, 27 new."],
                 ["JavaScript behaviour", verdict("PASS"), "114 of 114, 4 new."],
                 ["Undefined names", verdict("PASS"), "9 files, 0."],
                 ["Not run", verdict("NOTED"), "No scan ran on a real network from the "
                  "build PC, and the image was not built. The scan tests use stand-ins "
                  "for the probers, so the probers' own network code is not tested. The "
                  "field test (A18) is the first real scan with the moved code."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, Parts 1 to "
            "6. Rows this change touches; the rest carry from the 3.0.0-rc1.3 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST, contracts, undefined names",
                  verdict("PASS"), "9 files."],
                 ["1.2 Syntax", "compile() per file", verdict("PASS"), "9 files."],
                 ["1.3 Runtime errors", "Names", verdict("FAIL"),
                  "B22: the cancel flag has two names. Found, recorded, not fixed."],
                 ["1.6 Function design", "File size", verdict("NOTED"),
                  "Main file 6,780 lines, 63% under 2.6.8. run_scan (714 lines) and "
                  "_probe_rtsp_paths_single_socket (657) moved whole."],
                 ["1.7 Mutability", "Values replaced at run time", verdict("PASS"),
                  "One owner file for each; the gate enforces it."],
                 ["1.16 Refactoring", "Behaviour kept", verdict("PASS"),
                  "Text comparison of 363 definitions; page hash; routes; scan tests "
                  "before and after."],
                 ["1.18 Testing", "New tests", verdict("PASS"),
                  "25 for the scan, written before the move."],
                 ["2.x JavaScript", "New function", verdict("PASS"),
                  "_setEmptyText: no error when the elements are absent; tested."],
                 ["6.3 Version control", "Commits and gate", verdict("PASS"),
                  "Three commits, one for each item; the gate run before the last."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"),
                  "Tests and documents use made-up addresses only."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets([
              "<b>B22:</b> the Cancel button does not stop a scan.",
              "<b>B20</b> (late pictures, insects on the fallback): right after 3.0.0.",
              "<b>C17</b> (detection zones): design discussion pending.",
              "<b>E1:</b> password entry, the snapshot loop, the Enhanced View engine "
              "and the manufacturer database are still in the main file.",
              "Skip Non-Reference Frames breaks H.264 cameras (B3)."])]

    s += [h1("Acceptance (field test A18)"),
          *bullets([
              "The add-on starts; the start-up scan finds the same cameras as before.",
              "During a scan with an empty page: 'No Cameras Found Yet', and no "
              "'Click Scan Network' line.",
              "Entering a camera password works (that path calls the moved probers).",
              "The log has no line with 'not defined' or 'has not run yet'."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["camera_discovery.py", "Scan and probers moved out; empty page "
                  "markup. Version 3.0.0-rc1.4."],
                 ["anycam_probe.py, anycam_scan.py", "New."],
                 ["page_script.py", "_setEmptyText."],
                 ["anycam_modules.py, Dockerfile", "Two more files."],
                 ["tests/", "27 server checks and 4 page checks."],
                 ["docs, CLAUDE.md, README", "Module list; build plan."],
                 ["CHANGELOG.md", "3.0.0-rc1.4 entry."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.0.0-rc1.4", story(), "anycam_3_0_0_rc1_4_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
