"""Audit report for AnyCam 3.7.0-rc2.0 — four page changes from the 3.7.0-rc1.0 field test.

    python docs/audits/tools/mk_audit_3_7_0_rc2_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.7.0-rc2.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - page changes from the field test - tag 3.7.0-rc2.0"
                   % DATE, S_SUB),
         p("Scope confirmed by CrystalHeeler on 2026-10-05: the four Ready items from the "
           "3.7.0-rc1.0 field test, B28, C22, C24 and D4, as 3.7.0-rc2.0. Page changes only; no "
           "server behaviour changes. Not field-tested.")]

    s += [h1("Changes"),
          table([["#", "Change"],
                 ["C22", "The zone window has a title bar: drag it to move the window, press ▾ to "
                  "fold the window to the title bar. The window stays on the screen. The place "
                  "and the fold are kept in the browser's own storage (localStorage, read and "
                  "written in try/catch), so each device keeps its own."],
                 ["D4", "While a card is dragged, a 4 px line in the gap before or after the "
                  "target card shows where it lands. Cards in one column (a phone held upright) "
                  "get a level line, judged by the upper or lower half of the card; side by side, "
                  "an upright line by the left or right half. The lit card side is gone."],
                 ["C24", "The page says Remote Storage for the buttons, the dialog title, the "
                  "help text and the messages. The add-on log keeps Upload."],
                 ["B28", "Cause: the rule .modal input {width:100%} also made the checkbox full "
                  "width. A checkbox in a dialog label now keeps its own size. The text is "
                  "shorter: \"Delete the local copy after upload\"."]],
                [0.7 * inch, 6.0 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 17 files."],
                 ["Python integration", verdict("PASS"), "572 of 572; 4 new in section AI; Q4 "
                  "now expects Remote Storage."],
                 ["JavaScript behaviour", verdict("PASS"), "166 of 166; new section C22 and "
                  "D4 checks in the card-order section."],
                 ["Not run", verdict("NOTED"), "No field test. The changes were not seen in a "
                  "browser: the page needs the add-on running to draw these parts."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>. Rows this change "
            "touches; the rest carry from the 3.7.0-rc1.0 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["2.x JavaScript", "Events", verdict("PASS"),
                  "Pointer capture; handlers removed at the end of a drag."],
                 ["4.x CSS", "Specificity", verdict("PASS"),
                  "The checkbox rule is scoped to dialog labels; touch-action none on the title "
                  "bar only."],
                 ["1.18 Testing", "New tests", verdict("PASS"), "4 server and 9 page checks."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "No names or addresses."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets(["<b>B27:</b> Enhanced View is black for a few seconds after a return to the "
                    "page; waits for a log.",
                    "<b>C23:</b> the Storage tab hides the file names on a phone held upright; "
                    "part of the revamp.",
                    "<b>C25:</b> a better on/off design for Remote Storage.",
                    "<b>B11:</b> hardware decode fix waits for the start-up log."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["page_script.py", "Zone window drag and fold; the drop line; Remote Storage text."],
                 ["anycam_page.py", "Title bar, fold button, drop line element, checkbox CSS, "
                  "Remote Storage text."],
                 ["tests/", "Section AI; page sections C22 and D4; Q4."],
                 ["CHANGELOG.md, config.yaml, camera_discovery.py, docs", "3.7.0-rc2.0; build plan."]],
                W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.7.0-rc2.0", story(), "anycam_3_7_0_rc2_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
