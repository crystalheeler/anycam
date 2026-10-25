"""Audit report for AnyCam 3.9.0 — the stable release of 3.9.0-rc1.0 and 3.9.0-rc2.0.

    python docs/audits/tools/mk_audit_3_9_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.9.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - stable release - tag 3.9.0" % DATE, S_SUB),
         p("3.9.0 is the code of 3.9.0-rc2.0 with the version changed; CrystalHeeler ordered it on 2026-10-25. "
           "Home Assistant could not install 3.9.0-rc2.0 over 3.9.0-rc1.0 (F11: its version library cannot read "
           "\"rc2.0\"); a final version reads as newer than any rc, so 3.9.0 installs from the update dialog."),
         p("The items of the line are audited in the 3.9.0-rc1.0 and 3.9.0-rc2.0 reports: B68, B69, B79, B72, "
           "B70, B74, B71, B73, B81, B78, B80, C31, B58, B63, B62 (first steps), C30, B84 and B85.")]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates, the same 63 contracts as 3.9.0-rc2.0."],
                 ["Python integration", verdict("PASS"), "812 of 812."],
                 ["JavaScript behaviour", verdict("PASS"), "217 of 217."],
                 ["Recording replay", verdict("PASS"), "From 3.9.0-rc2.0: 409 recordings, unchanged code."],
                 ["Field", verdict("NOTED"), "3.9.0-rc1.0 ran one night on Test System B; 3.9.0-rc2.0 was not "
                  "installed in the field (F11)."]],
                [1.5 * inch, 0.7 * inch, 4.4 * inch])]

    s += [h1("Not tested"),
          *bullets(["<b>B84 and B85 in the field.</b> 3.9.0-rc2.0 could not be installed from the update dialog; "
                    "3.9.0 is its first field run.",
                    "<b>The Pi's memory</b> with the 17.25 s recording buffer."])]

    s += [h1("Best-practices compliance"),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["Rule 5", "Full version numbers", verdict("PASS"), "3.9.0 in config.yaml, the code and the "
                  "changelog heading."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "No names or addresses."]],
                W_BP)]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["camera_discovery.py, config.yaml", "3.9.0."],
                 ["CHANGELOG.md", "The 3.9.0 entry."],
                 ["docs", "Build plan; this report."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.9.0", story(), "anycam_3_9_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
