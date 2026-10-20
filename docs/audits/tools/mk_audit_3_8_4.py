"""Audit report for AnyCam 3.8.4 — B68 dialog text overlap, B69 drag out of a dialog.

    python docs/audits/tools/mk_audit_3_8_4.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.8.4 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - B68, B69 - tag 3.8.4 (local)" % DATE, S_SUB),
         p("A fix release with the scope and version CrystalHeeler named on 2026-10-20: two dialog bugs "
           "reported with a screenshot of the camera settings. Built and tagged locally; CrystalHeeler "
           "said not to push or publish yet.")]

    s += [h1("B68: text ran over the next line"),
          table([["Part", "3.8.4"],
                 ["Cause", "3.8.2 (B67, rule 10) capped each dialog's height and let it scroll. A dialog is a "
                  "flex column, and flex items shrink by default, so a tall dialog first squeezed its parts. "
                  "Measured in the browser pane on 3.8.3 at 1366x768 with five zones: the zone peaks line held "
                  "66 px of text in a 15 px box and covered \"Detection in zones only\"."],
                 ["Fix", ".modal>* { flex-shrink: 0 }: every part keeps its height; the dialog scrolls."],
                 ["Guard", "Release gate R10 fails without that rule; test AT1."],
                 ["Check", "After the fix, at 1366x768, 812x375 and 375x812: the peaks text ends 12 px above "
                  "the checkbox row; in all six dialogs no part's content is taller than its box; each "
                  "dialog stays inside the screen with its buttons in view."]],
                [1.2 * inch, 5.4 * inch])]

    s += [h1("B69: a drag out of a dialog closed it"),
          table([["Part", "3.8.4"],
                 ["Cause", "A click goes to the nearest element under both the press and the release. Pressing "
                  "in the rename field and releasing outside the dialog sent the click to the backdrop, "
                  "whose handler closes the dialog."],
                 ["Fix", "One capture-phase listener records where each press began; a click on a "
                  ".modal-backdrop whose press began elsewhere is stopped before the backdrop's handler. "
                  "It covers every dialog, not only Rename."],
                 ["Check", "Page tests B69; in the browser pane, Rename and the camera settings stay open "
                  "after a press in a field and a release on the backdrop, close on a press and release on "
                  "the backdrop, and close with Cancel."]],
                [1.2 * inch, 5.4 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; R10 with the new flex-shrink check."],
                 ["Python integration", verdict("PASS"), "773 of 773; new section AT."],
                 ["JavaScript behaviour", verdict("PASS"), "210 of 210; new B69 checks."],
                 ["Visual (rule 10)", verdict("PASS"), "Three screen sizes, measured in the browser pane."],
                 ["Field", verdict("NOTED"), "Not run yet."]],
                [1.5 * inch, 0.7 * inch, 4.4 * inch])]

    s += [h1("Not tested"),
          *bullets(["<b>The HA phone app.</b> Touch presses start the same pointer events, but the app "
                    "was not available here.",
                    "<b>A real mouse drag.</b> The browser pane run sent the pointer and click events "
                    "from a script."])]

    s += [h1("Best-practices compliance"),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["Rule 10", "Overlays fit", verdict("PASS"), "The B67 cap no longer squeezes content."],
                 ["8 (regressions)", "Our code first", verdict("PASS"), "B68 came from AnyCam's 3.8.2 styles."],
                 ["Part 2 JS", "No global side effects beyond the need", verdict("PASS"),
                  "Two listeners; only backdrop clicks are affected."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "No names or addresses."]],
                W_BP)]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_page.py", ".modal>* keeps its height."],
                 ["page_script.py", "The backdrop click guard."],
                 ["verify_release.py", "R10 checks the rule."],
                 ["tests/", "AT1; page checks B69."],
                 ["CHANGELOG.md, config.yaml, camera_discovery.py, docs", "3.8.4; build plan (B68, B69 done; "
                  "B70-B72 from CrystalHeeler's 3.8.2 log)."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.8.4", story(), "anycam_3_8_4_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
