"""Audit report for AnyCam 4.0.0 — C32, the object engine (and B75 with it).

    python docs/audits/tools/mk_audit_4_0_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 4.0.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - C32 and B75 - tag 4.0.0" % DATE, S_SUB),
         p("A major version on CrystalHeeler's order of 2026-10-25 (an unattended run: 3.9.0, 4.0.0, 4.1.0, then "
           "push and publish 4.1.0). C32 follows CrystalHeeler's decisions of 2026-10-24, without the classifier. "
           "The object engine is a per-camera choice; the classic engine stays the default, so an update changes "
           "nothing until a camera is switched. Design and evidence: docs/C32_Object_Engine.md.")]

    s += [h1("The object engine"),
          table([["Part", "4.0.0"],
                 ["Objects", "Changed cells up to 2 cells apart form one object (anycam_objects.find_objects): size, "
                  "box, feet (bottom middle), brighter share, fill."],
                 ["Where", "The zone at the feet, else the zone holding most cells, else outside; Off zones and, in "
                  "zones-only mode, outside do not count."],
                 ["Size", "obj_min_pct: 10% of the picture at sensitivity 1 to 0.05% at 100 (0.02% with the night "
                  "boost), per zone."],
                 ["Not bodies", "Lit surfaces (one-sided, +25 grey levels, fill 0.35 or less); at night insects "
                  "(one-sided, fill 0.55 or more, under 1% of the picture)."],
                 ["Tracking", "Confirmed after 3 pictures and 3% of the width of travel; unconfirmed tracks dropped "
                  "after 1 s unseen, confirmed after 3 s (B75)."],
                 ["Lights", "Tracks dropped at a light change, a colour switch and during a pause; a 3 s light hold "
                  "(classic 2 s). The 1.25 s look before recording (B85) applies."],
                 ["Settings", "MOTION_DEFAULTS engine = classic; validated; kept per camera in global mode (an "
                  "engine-only POST); in settings copies and restores."],
                 ["Answer 14", "_motion_detector_kind: live, snapshots or None; the Cog panel note and a warning sign "
                  "on the record button."]],
                [1.2 * inch, 5.4 * inch])]

    s += [h1("Evidence: the replay"),
          p("409 recordings through the add-on's _motion_feed with each camera's zones and sensitivity (settings "
            "copy of 24 Oct 22:42); classic = 3.9.0."),
          table([["Recordings", "Classic", "Objects"],
                 ["Headlights (29)", "0", "2"],
                 ["Insects (10)", "10", "8"],
                 ["Motion-sensitive light (8)", "0", "0"],
                 ["Dawn switch (2)", "0", "0"],
                 ["A person (6)", "6", "6"],
                 ["The cat (1)", "1", "1"],
                 ["By day, unlabelled (228)", "211", "217"],
                 ["At night, unlabelled (122)", "20", "21"]],
                [3.0 * inch, 1.8 * inch, 1.8 * inch]),
          p("The 15 unlabelled files where the engines disagree were viewed: classic only, 2 insects and 2 with "
            "nothing visible; objects only, 3 people far away that the classic engine missed, 2 headlights and 6 "
            "with nothing visible.")]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; contracts for _motion_objects, _motion_engine and "
                  "_motion_obj_place; the new file in anycam_modules.py and the Dockerfile."],
                 ["Python integration", verdict("PASS"), "831 of 831; new section AW (19 checks); L1 updated for "
                  "the engine setting."],
                 ["JavaScript behaviour", verdict("PASS"), "221 of 221; 4 new checks for the Cog panel and the "
                  "warning."],
                 ["Recording replay", verdict("PASS"), "409 recordings on both engines."],
                 ["Field", verdict("NOTED"), "Not run: built unattended."]],
                [1.5 * inch, 0.7 * inch, 4.4 * inch])]

    s += [h1("Not tested"),
          *bullets(["<b>The object engine live.</b> The replay decoded files with OpenCV; its time per picture on "
                    "the Pi with several cameras was not measured. On the build PC the grouping takes 0.7 ms with 1% of "
                    "the picture changed, 2.4 ms at 5% and 9.7 ms at 29% (the most it groups); the Pi 4 is a few "
                    "times slower, inside the 250 ms between pictures.",
                    "<b>People at night far from the camera</b> are not in the labelled set; a white cat dashing "
                    "past at night may count as an insect.",
                    "<b>The Cog panel's new rows</b> were checked by the page tests, not on a phone screen (rule 10)."])]

    s += [h1("Best-practices compliance"),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["Rule 2", "Logs and recordings before code", verdict("PASS"),
                  "Tuned on 409 recordings, labelled from viewing."],
                 ["Rule 7", "Version and scope confirmed", verdict("PASS"), "CrystalHeeler: 4.0.0 = C32, 2026-10-25."],
                 ["Imports", "No cycles; new file registered", verdict("PASS"),
                  "anycam_objects.py imports nothing from AnyCam."],
                 ["P17 Testing", "Tests with the change", verdict("PASS"), "Section AW; page checks."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "Made-up zone names in tests."]],
                W_BP)]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_objects.py (new)", "Objects, tracking, the size curve."],
                 ["anycam_motion.py", "The engine setting, the object path, insects, the light hold, the "
                  "reduced-detection report."],
                 ["anycam_zones.py", "zone_map."],
                 ["anycam_page.py, page_script.py", "The engine select, its help, the warnings."],
                 ["anycam_modules.py, Dockerfile, verify_release.py, tests/", "The new file; contracts; tests."],
                 ["CHANGELOG.md, config.yaml, camera_discovery.py, docs", "4.0.0; the design document; the plan."]],
                W_FILES)]
    return s


if __name__ == "__main__":
    path = build("4.0.0", story(), "anycam_4_0_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
