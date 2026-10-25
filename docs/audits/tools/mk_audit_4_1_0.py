"""Audit report for AnyCam 4.1.0 — D6 (one Scans menu) and D7 (Back to Cameras in the status bar).

    python docs/audits/tools/mk_audit_4_1_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 4.1.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - D6 and D7, with 4.0.0 - tag 4.1.0" % DATE, S_SUB),
         p("The last step of CrystalHeeler's unattended order of 2026-10-25: 3.9.0, 4.0.0 (C32, audited in its own "
           "report), 4.1.0, then push and publish 4.1.0. D6 and D7 follow CrystalHeeler's mock-up and answers of "
           "2026-10-25 (docs/BUILD_PLAN.md).")]

    s += [h1("The changes"),
          table([["Item", "Change"],
                 ["D6 header", "Port Scan is Scans; the Deeper Scan box and Scan Network leave the header."],
                 ["D6 chosen hosts", "Deeper Scan (nmap -p1-10000) and Scan All Ports (-p-), grey until a host is "
                  "checked or an address typed. A Deeper Scan runs as a queue, then gives each host's open ports to "
                  "the scan's own camera probes (the stage-4 code, now _probe_broad_hosts), so a camera gets its "
                  "card; the port table stays."],
                 ["D6 rescan", "Rescan Entire System with its Deeper Scan box (the old stage 4). The scan counts its "
                  "new cameras (SCAN_STATE new_cameras); at the first one the page shows the cameras, only from the "
                  "Scans menu."],
                 ["D6 Cancel", "Cancel now stops the rest of a queue of hosts, and no cameras are looked for."],
                 ["D7", "One Back to Cameras button at the left of the status bar on every menu but the cameras, an "
                  "accent outline and tint; the per-menu rows are gone; the bar wraps."]],
                [1.3 * inch, 5.3 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; contracts for api_pscan_start, run_batch_port_scan "
                  "and deeper_scan_cards."],
                 ["Python integration", verdict("PASS"), "841 of 841; new section AX (10 checks); T1 updated for the "
                  "empty-page text."],
                 ["JavaScript behaviour", verdict("PASS"), "225 of 225; 4 new D6 and D7 checks."],
                 ["Visual (rule 10)", verdict("PASS"), "The built page in a browser at 1366x768, 375x812 and 812x375: "
                  "the Scans menu, the status bar wrapping, Storage and Connect Camera, the Cog panel with the "
                  "engine row and the warning on a phone."],
                 ["Field", verdict("NOTED"), "Not run: built unattended."]],
                [1.5 * inch, 0.7 * inch, 4.4 * inch])]

    s += [h1("Not tested"),
          *bullets(["<b>A real Deeper Scan.</b> nmap and the camera probes were replaced by fakes in the tests; "
                    "the change reuses the scan's stage-4 code unchanged.",
                    "<b>The jump to the cameras</b> during a real rescan with a new camera on the network.",
                    "<b>The object engine (4.0.0) live</b>: see its report."])]

    s += [h1("Best-practices compliance"),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["Rule 7", "Version and scope confirmed", verdict("PASS"), "CrystalHeeler: 4.1.0 = the UI items."],
                 ["Rule 10", "Overlays and bars fit every screen", verdict("PASS"), "Seen at the three sizes."],
                 ["6 Security", "Validate at the boundary", verdict("PASS"), "Scan mode from an allowlist."],
                 ["P1 Python", "No duplicated code", verdict("PASS"), "Stage 4 and Deeper Scan share one function."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "Made-up addresses in tests and the page check."]],
                W_BP)]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_page.py, page_script.py", "The Scans menu, the status-bar button, texts."],
                 ["anycam_scan.py", "_probe_broad_hosts, deeper_scan_cards, PSCAN_PORTS."],
                 ["camera_discovery.py", "The scan mode, the queue and Cancel, the new-camera count; 4.1.0."],
                 ["verify_release.py, tests/", "Contracts; section AX; page checks."],
                 ["CHANGELOG.md, config.yaml, docs", "4.1.0; the plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("4.1.0", story(), "anycam_4_1_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
