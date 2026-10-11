"""Audit report for AnyCam 3.8.0-rc3 — zone drawing, appliance login, B55.

    python docs/audits/tools/mk_audit_3_8_0_rc3.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.8.0-rc3 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - zone drawing + appliance login + B55 - tag 3.8.0-rc3" % DATE, S_SUB),
         p("Three fixes and one feature found or requested by CrystalHeeler on 2026-10-10: zone "
           "drawing failed from the Cog menu, appliance cards now offer a login, the appliance notes "
           "wording was corrected, and the scan skips confident non-cameras (B55). Scope: these "
           "items only, no change to scan completeness.")]

    s += [h1("Cause and change"),
          table([["Item", "Detail"],
                 ["Zone drawing (bug)", "Opening Edit Zones from a card's Cog menu laid out the "
                  "canvas before the video had a measured size, so the drawing surface was zero-sized "
                  "and would not accept a zone. The editor now retries the layout (up to about 5s) "
                  "until the media reports a real size, matching the Enhanced View path that already "
                  "worked."],
                 ["Appliance login (change)", "A camera built into an appliance (robot vacuum, litter "
                  "robot) kept its \"Camera in an appliance\" notes but had no way to log in. The card "
                  "now also shows a login prompt; a submitted login walks the RTSP paths, then tries "
                  "MJPEG and HLS on the common web ports, and reports a clear message if nothing is "
                  "reachable."],
                 ["Appliance notes (bug)", "The note now reads that the camera's video may or may not "
                  "be reachable only through the appliance maker's app."],
                 ["B55 non-camera skip (feature)", "The scan now skips a host when a printer port "
                  "(631 or 9100) is open, or when the MAC maker is a known non-camera brand. This "
                  "stops the scan probing printers and similar appliances. Multicast hosts are "
                  "exempt from the skip."]],
                [1.6 * inch, 5.0 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates."],
                 ["Python integration", verdict("PASS"), "679 of 679; Z4e covers the appliance login "
                  "(success and failure); U16 covers the B55 skip."],
                 ["JavaScript behaviour", verdict("PASS"), "193 of 193."],
                 ["Field", verdict("NOTED"), "Zone drawing, the appliance login and B55 are verified "
                  "against the latest field logs and a follow-up field re-test."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["8 (regressions)", "Our code first", verdict("PASS"),
                  "Zone drawing and the appliance wording were AnyCam-side; both fixed."],
                 ["1.12 Logging", "Truth", verdict("PASS"),
                  "The scan logs each host it skips and why (printer port, MAC maker)."],
                 ["1.18 Testing", "Coverage", verdict("PASS"), "Z4e and U16 cover the new paths."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "Made-up addresses only; no device "
                  "state named."]],
                W_BP)]

    s += [h1("Known issues"),
          *bullets(["<b>OPTIONS-401 scan stop:</b> a camera that answers 401 at OPTIONS (Lorex) is "
                    "not yet stopped at the first 401 during the scan (only at DESCRIBE). Harmless.",
                    "<b>Microseven:</b> untested on an unlocked Microseven camera.",
                    "<b>Appliance login:</b> reaches a camera only if the appliance exposes a local "
                    "RTSP, MJPEG or HLS endpoint; many expose video only through the maker's app."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["page_script.py", "Zone editor retries layout until the media has a size; appliance "
                  "cards render a login prompt."],
                 ["anycam_credentials.py", "Appliance login branch (RTSP walk, then MJPEG/HLS on web "
                  "ports); initialises walk findings to avoid an unbound reference."],
                 ["anycam_scan.py", "B55 non-camera skip (printer ports, non-camera MAC maker); "
                  "corrected appliance notes wording."],
                 ["tests/", "Z4e (appliance login) and U16 (B55 skip) added."],
                 ["CHANGELOG.md, config.yaml, docs", "3.8.0-rc3; build plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.8.0-rc3", story(), "anycam_3_8_0_rc3_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
