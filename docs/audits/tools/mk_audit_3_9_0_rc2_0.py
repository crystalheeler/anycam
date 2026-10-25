"""Audit report for AnyCam 3.9.0-rc2.0 — B84 and B85, from the first field night of 3.9.0-rc1.0.

    python docs/audits/tools/mk_audit_3_9_0_rc2_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.9.0-rc2.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - 2 items - tag 3.9.0-rc2.0" % DATE, S_SUB),
         p("A release candidate with the scope CrystalHeeler confirmed on 2026-10-25: B84 and B85. The first "
           "field night of 3.9.0-rc1.0 (24-25 Oct, Test System B) recorded 29 headlight files, 9 of the "
           "motion-sensitive light by the hot tub and 2 of the dawn switch, although the headlight pause started "
           "at the right moment (docs/Recording_Review_2026-10-24.md, sections 9 and 10).")]

    s += [h1("What the field night showed"),
          table([["Cause", "Files", "Fix"],
                 ["The pause ended (4 s, 6 s in all) while the light still crossed the yard; the slow-movement "
                  "check had kept lit pictures from the pause", "20", "B84"],
                 ["A zone passed on the light's faint first edge, before the pause saw it", "4", "B85"],
                 ["One lit picture recorded at once (the one-picture rule)", "3", "B85"],
                 ["No pause at all", "2", "B84 (longer pause), B85"],
                 ["The dawn infrared-to-colour switch under the 75% light rule", "2", "B85"],
                 ["The motion-sensitive light switching on or off", "9", "B85"],
                 ["Insects in the infrared, several pictures in a row", "10", "Not in scope (B86)"]],
                [4.2 * inch, 0.6 * inch, 1.8 * inch])]

    s += [h1("The changes"),
          table([["Item", "Change"],
                 ["B84", "A light pause lasts up to 8 s (12 s in all); a one-sided change within 3 s after a pause "
                  "continues it with no quiet picture before it; the slow-movement check keeps no picture during "
                  "a pause or for 0.6 s after; a detector restart clears the pause (the log had shown \"passing "
                  "light over after -284.0 s\")."],
                 ["B85", "A live start waits 1.25 s (MOTION_HOLD_S); the pre-roll grows by the wait. It is "
                  "cancelled by a light pause, a light change, the camera switching between infrared and colour "
                  "(colour jump 1.0 or more, one side 2.5 or less; every picture's colour is now kept), or a "
                  "change 90% one-sided that stopped at once (two still comparisons). A colour switch also counts "
                  "as a light change outside the wait. The one-picture rule ignores a lit lawn, and its hit counts "
                  "twice, so the log no longer says \"one picture only\" for a picture it recorded."],
                 ["Buffer", "MOTION_BUF_KEEP_S = 3 + 12 + 1 + 1.25 = 17.25 s (was 10 s): about 17 MB at "
                  "8 Mbit/s per armed camera; MOTION_BUF_MAX_S stays 20 s."]],
                [1.1 * inch, 5.5 * inch])]

    s += [h1("Evidence: the replay"),
          p("409 recordings (every first file of 20-24 Oct on the patio camera, and the whole night of 24-25 "
            "Oct) were decoded once to the detector's pictures (4 a second, 128x96 or 64x48 grey, each picture's "
            "colour) and fed through the add-on's own _motion_feed, with each camera's zones and sensitivity from "
            "the settings copy of 24 Oct 22:42. Starts and stops were caught where the add-on hands them to "
            "asyncio."),
          table([["Night of 24-25 Oct", "3.9.0-rc1.0", "3.9.0-rc2.0"],
                 ["Headlights (29 files)", "22", "0"],
                 ["Motion-sensitive light (8)", "4", "0"],
                 ["Dawn switch (2)", "1", "0"],
                 ["A person (3)", "3", "3"],
                 ["Insects (9)", "9", "9"]],
                [3.0 * inch, 1.8 * inch, 1.8 * inch]),
          p("All 409: by day 214 of 215 still record (the one lost is a dusk headlight); at night 36 of the 127 "
            "that 3.9.0-rc1.0 records. All 91 lost were viewed: headlights, the porch light, the dusk and dawn "
            "switches, an insect. The cat, the person at the door, both door openings and the moth still record. "
            "Pause lengths 4/6 s, 6/10 s and 8/12 s left 2, 1 and 0 of the 29 headlight files; 8/12 s lost no "
            "other file.")]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 63 contracts, new ones for _motion_begin, "
                  "_motion_hold_check, _motion_colour, _motion_colour_switch and _motion_slow_look; the feed and "
                  "detector contracts updated."],
                 ["Python integration", verdict("PASS"), "812 of 812; new section AV (14 checks); M1 updated "
                  "for the 17.25 s buffer."],
                 ["JavaScript behaviour", verdict("PASS"), "217 of 217; no page change."],
                 ["Recording replay", verdict("PASS"), "409 recordings, whole motion path, old against new."],
                 ["Field", verdict("NOTED"), "Not run yet: an rc for field testing."]],
                [1.5 * inch, 0.7 * inch, 4.4 * inch])]

    s += [h1("Not tested"),
          *bullets(["<b>The live detector.</b> The replay decoded files with OpenCV; the add-on decodes the "
                    "live sub-stream with ffmpeg. Each file starts 3-8 s before its trigger, so the replay misses "
                    "what came before.",
                    "<b>The Pi's memory</b> with the 17.25 s buffer on several armed cameras.",
                    "<b>Dusk on 3.9.0-rc1.0 or rc2.0.</b> 3.9.0-rc1.0 started after dark on 24 Oct; the dusk "
                    "switch was replayed from 20-24 Oct files only.",
                    "<b>The rabbit</b> that switched the light on was not found in the 9 lamp files; an animal "
                    "that keeps moving records in the tests (AV2)."])]

    s += [h1("Best-practices compliance"),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["Rule 2", "Logs before code", verdict("PASS"), "From the field log of 25 Oct and its 54 videos."],
                 ["Rule 8", "Regression first in our code", verdict("PASS"),
                  "The causes were in AnyCam: pause length, slow-check references, the one-picture rule."],
                 ["P1 Python", "No duplicate definitions; small functions", verdict("PASS"),
                  "The wait is four functions; gate 1 clean."],
                 ["P4 Async", "No blocking I/O on the loop", verdict("PASS"),
                  "The colour of each picture is a sum over 6,144 bytes; no I/O added."],
                 ["P17 Testing", "Tests before the change ships", verdict("PASS"),
                  "Section AV; the replay of 409 recordings."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "Cameras named by role; no addresses."]],
                W_BP)]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_motion.py", "B84, B85: the wait, the colour switch, the pause, the slow check, the "
                  "one-picture rule, the 17.25 s buffer."],
                 ["camera_discovery.py, config.yaml", "3.9.0-rc2.0."],
                 ["verify_release.py, tests/test_server.py", "Contracts; section AV; M1."],
                 ["CHANGELOG.md, docs", "3.9.0-rc2.0; build plan; recording review sections 9 and 10."]],
                W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.9.0-rc2.0", story(), "anycam_3_9_0_rc2_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
