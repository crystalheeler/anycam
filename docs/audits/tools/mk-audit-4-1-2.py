"""Audit report for AnyCam 4.1.2 — C34 (border while recording) and C35 (remote viewing).

    python docs/audits/tools/mk-audit-4-1-2.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 4.1.2 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - C34 and C35 - tag 4.1.2" % DATE, S_SUB),
         p("Scope confirmed by CrystalHeeler on 2026-10-26: C34 and C35 (both the per-device choice and the automatic "
           "switch). C35 answers B62: the phone's ZeroTier link to the Pi is RELAY (zerotier-cli peers on the Pi), it "
           "lost about a quarter of WebRTC's packets, and CrystalHeeler will not open firewall ports.")]
    s += [h1("The changes"),
          table([["Item", "Change"],
                 ["C35", "liveModeNow(): Automatic (WebRTC, or MSE for 12 h after a live view lost 5% of its packets or "
                  "froze 3 times in its first 20 s), WebRTC, or Remote (MSE). Kept in the browser (localStorage, every "
                  "access in try/catch: a blocked store means WebRTC). Cards and Enhanced View use it; a change restarts "
                  "the live views. MSE jumps to the live edge when more than 2 s behind (go2rtc's player only speeds up). "
                  "The choice is in Enhanced View's bar, also on a phone held upright."],
                 ["C34", "A red pulsing border on a recording camera's card and around Enhanced View, set by the 3 s "
                  "motion poll; still for reduced motion."]],
                [1.1 * inch, 5.5 * inch])]
    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates."],
                 ["Python integration", verdict("PASS"), "848 of 848; AY5 for the styles and the Live choice."],
                 ["JavaScript behaviour", verdict("PASS"), "230 of 230; C35 modes over time and with blocked storage; "
                  "the automatic thresholds; the live-edge jump; C34. B39 kept (the disconnect hook merged)."],
                 ["Visual (rule 10)", verdict("PASS"), "Enhanced View's bar at 375x812 (measured: all inside 375 px) and "
                  "812x375; the border at 1366x768. Found and fixed: the phone rule hid every label in the bar, so the "
                  "Live choice was hidden on a phone held upright."],
                 ["Field", verdict("NOTED"), "Not run."]],
                [1.5 * inch, 0.7 * inch, 4.4 * inch])]
    s += [h1("Not tested"),
          *bullets(["<b>MSE over the relayed ZeroTier link.</b> On 24 Oct the Microseven's 4K Enhanced View got no video "
                    "within 15 s over MSE on that link; cards (smaller streams) were not tried over MSE. The field test "
                    "decides whether Remote is steady there.",
                    "<b>The automatic switch on a real phone</b>, and the toast it shows.",
                    "<b>Safari and the HA app's web view</b> with MSE (ManagedMediaSource on iOS)."])]
    s += [h1("Best-practices compliance"),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["Rule 10", "Bars fit every screen", verdict("PASS"), "Measured on screen at the three sizes."],
                 ["Storage", "Browser storage never required", verdict("PASS"), "try/catch; WebRTC when blocked."],
                 ["Rule 7", "Version and scope confirmed", verdict("PASS"), "CrystalHeeler, 2026-10-26."],
                 ["File names", "Dashes", verdict("PASS"), "This report and its script."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "No names or addresses."]],
                W_BP)]
    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["page_script.py", "C35 mode, automatic switch, live-edge jump; C34 marks."],
                 ["anycam_page.py", "The Live choice; the border styles; the phone rule."],
                 ["tests/", "AY5; page checks."],
                 ["CHANGELOG.md, config.yaml, camera_discovery.py, docs", "4.1.2; the plan; the CEC research "
                  "(docs/CEC-and-TV-Research.md, .html, .pdf)."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("4.1.2", story(), "anycam-4.1.2-audit-report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
