"""Audit report for AnyCam 4.1.1 — C33 (recording names) and B87 (shaking shadows).

    python docs/audits/tools/mk-audit-4-1-1.py

From 4.1.1 on, new file names use dashes, never underscores (CrystalHeeler, 2026-10-26).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 4.1.1 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - C33 and B87 - tag 4.1.1" % DATE, S_SUB),
         p("Scope confirmed by CrystalHeeler on 2026-10-26: C33, B87 and B88, then push and publish. B88 was built, "
           "replayed and taken out (below). The 172 system's log under 4.1.0 showed no problem first: the upgrade "
           "check passed for both cameras and live view connected in 0.4 s.")]

    s += [h1("The changes"),
          table([["Item", "Change"],
                 ["C33", "Recording names: <camera>-<zone>-<date>-<time>, parts -part01, -part02; dashes only, never "
                  "underscores (CrystalHeeler, 2026-10-26). The Storage tab reads the zone from old (_partNN) and new "
                  "(-partNN) names; older files keep their names."],
                 ["B87", "The object engine confirms an object when its net travel, first place to last, is 3% of the "
                  "picture's width and at least half of all it moved (OBJ_MIN_STRAIGHT 0.5). A shadow shaking out and "
                  "back (25 Oct 19:40:38, 26 Oct 07:26:59) moves a lot and gets nowhere."]],
                [1.1 * inch, 5.5 * inch])]

    s += [h1("B88, taken out"),
          p("The headlight test inside the slow-movement check (one-sided, scattered changes against the 5 s picture) "
            "was replayed on 535 recordings: it stopped 2 of 13 headlight files on driveway-left and lost 7 people, "
            "among them a person walking up to the back-yard camera at night and people coming out of the front door. "
            "Two positions of one person 5 s apart look scattered to that test. The object engine also records 9 of "
            "the 13: they are cars driving on the street inside the gravel zone and the zones over the neighbours' yards. B88 "
            "is back to Discuss; the fix is zones clear of the street.")]

    s += [h1("Evidence: the replay"),
          table([["Recordings (535)", "4.1.0", "4.1.1"],
                 ["People, the cat, vehicles arriving", "all recorded", "all recorded"],
                 ["Shaking shadows on the patio camera (4)", "3", "2"],
                 ["Nothing visible", "-", "1 fewer"],
                 ["Headlights (driveway-left, 13)", "11", "11"]],
                [3.0 * inch, 1.8 * inch, 1.8 * inch]),
          p("The add-on's _motion_feed replayed every recording of the 4.1.0 field night (patio, driveway-left, back "
            "yard: 126) and the 409 of 20-25 Oct, with the settings copy of 25 Oct 15:27.")]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; a contract for track (net travel, straightness); "
                  "the recording contract now requires the dash names."],
                 ["Python integration", verdict("PASS"), "846 of 846; new section AY; L14, AG10, AR6, AU8 and O10 "
                  "updated for the dash names."],
                 ["JavaScript behaviour", verdict("PASS"), "225 of 225; no page change."],
                 ["Recording replay", verdict("PASS"), "535 recordings, 4.1.0 against 4.1.1, and four variants to "
                  "find B88's losses."],
                 ["Field", verdict("NOTED"), "Not run yet."]],
                [1.5 * inch, 0.7 * inch, 4.4 * inch])]

    s += [h1("Not tested"),
          *bullets(["<b>The new names on the Pi:</b> ffmpeg's segment muxer with -part%02d, and the rename of a lone "
                    "-part01 file (tests use stand-ins).",
                    "<b>The Storage tab</b> with old and new names side by side, on a phone.",
                    "<b>B87 live.</b> The replay decoded files with OpenCV."])]

    s += [h1("Best-practices compliance"),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["Rule 2", "Logs before code", verdict("PASS"), "Both field logs and 244 recordings read first."],
                 ["Rule 7", "Version and scope confirmed", verdict("PASS"), "CrystalHeeler, 2026-10-26; B88 left "
                  "out after the replay."],
                 ["File names", "Dashes, never underscores", verdict("PASS"), "Recordings and this report; Python "
                  "modules that are imported keep underscores."],
                 ["P17 Testing", "Tests with the change", verdict("PASS"), "Section AY; the replay."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "No addresses or names."]],
                W_BP)]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_motion.py, anycam_zones.py", "C33: the dash names; reading old and new."],
                 ["anycam_objects.py", "B87: net travel and straightness."],
                 ["verify_release.py, tests/", "Contracts; section AY; tests for the names."],
                 ["CHANGELOG.md, config.yaml, camera_discovery.py, docs", "4.1.1; the plan (C34, D8 added)."]],
                W_FILES)]
    return s


if __name__ == "__main__":
    path = build("4.1.1", story(), "anycam-4.1.1-audit-report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
