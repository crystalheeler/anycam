"""Audit report for AnyCam 3.4.0-rc1.0 — detection zones.

    python docs/audits/tools/mk_audit_3_4_0_rc1_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.4.0-rc1.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - detection zones - tag 3.4.0-rc1.0" % DATE, S_SUB),
         p("Scope: phase 3.4.0 of the build plan: C17, detection zones, built to CrystalHeeler's "
           "12 requirements (2026-10-02) and the 23 answers approved on 2026-10-04, both in "
           "docs/Detection_Zones_Plan.md. Built in the untested run on top of 3.3.0-rc1.0. "
           "This build is not field-tested.")]

    s += [h1("1. The answers, as built"),
          table([["#", "Answer", "Built"],
                 ["1", "Close at the first point (12 px) or Enter; 3 points", "Yes"],
                 ["2", "Zone list; rename; delete with confirm; right-click or long press removes "
                  "a point; middle handle adds one; Undo/Backspace", "Yes"],
                 ["3", "Touch: tap; Finish and Close shape buttons; no zoom in the drawing area",
                  "Yes (touch-action: none)"],
                 ["4", "Overlaps: each zone on its own cells", "Yes"],
                 ["5", "Sensitivity in the window and the cog panel; Off masks", "Yes"],
                 ["6", "Crossing lines refused at close", "Yes, page and server"],
                 ["7", "Name asked at close; Zone N suggested; unique; 40 characters", "Yes"],
                 ["8", "128 x 96 with zones; warning under 24 cells; re-check whole-picture levels",
                  "Grid and warning yes; the re-check needs field data"],
                 ["9", "Second picture in the same zone; no one-picture 3% rule in a zone", "Yes"],
                 ["10", "Light rule on the whole picture only", "Yes"],
                 ["11", "5 s comparison, two pictures in a row", "Yes"],
                 ["12", "Night boost on each zone", "Yes"],
                 ["13", "Zones only: outside not checked", "Yes"],
                 ["14", "Tuning line and cog reading per zone and outside", "Yes"],
                 ["15", "Zone in the log and the Storage tab, not the file name",
                  "Yes (/data/recording_zones.json, newest 2,000)"],
                 ["16", "Show zones; recording zone marked", "Yes"],
                 ["17", "Zones in /data/motion.json", "Yes"],
                 ["18", "Pause for a still; snapshot cameras draw on their picture", "Yes"],
                 ["19", "Picture coordinates", "Yes"],
                 ["20", "PTZ: the cog panel says zones belong to one view", "Yes (for every camera)"],
                 ["21", "Zones button; Edit zones in the cog panel", "Yes"],
                 ["22", "Click outside starts a zone; click inside selects; + New zone; double "
                  "click stops drawing", "Yes"],
                 ["23", "Done saves; Cancel confirms; Esc stops drawing only", "Yes"]],
                [0.4 * inch, 4.3 * inch, 2.0 * inch])]

    s += [h1("2. Detection design"),
          *bullets([
              "anycam_zones.py holds the geometry (crossing test, point in polygon, cells), "
              "the checks of the page's input, and the judging; it imports no other AnyCam file.",
              "_motion_cells returns one flag per changed cell next to the whole-picture share "
              "and spread. Each zone counts its own flags; outside counts the cells in no closed "
              "zone. A zone's cells are built once per change, not per picture.",
              "The area that passed by the widest margin over its threshold is the trigger; "
              "its name goes to the confirmation rule, the second look on the snapshot path, "
              "the log and the recording.",
              "A change of zones starts the detector again (the grid follows the zones); the "
              "recording buffer keeps running.",
              "The page's own requirement 5 (a click goes on with an open zone) and answer 22 "
              "(a click outside starts a new zone) meet like this: with an open zone selected, a "
              "click continues it; otherwise a click outside starts a new zone."])]

    s += [h1("3. Also fixed"),
          p("A WebRTC or RTSP-over-WebSocket camera has status 'info'. openFocus opened only "
            "cameras with status 'ready', so in 3.2.0-rc1.0 those cameras played in the card "
            "but did not open Enhanced View. openFocus now also opens them.")]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 16 files; build inputs "
                  "checked online."],
                 ["Python integration", verdict("PASS"), "532 of 532; 19 new in section AE."],
                 ["JavaScript behaviour", verdict("PASS"), "158 of 158; 19 new in section "
                  "C17: geometry, the letterbox, clicks, closing, crossing, Esc, resume, Undo, 6 "
                  "zones, small zones, Off, Done, Cancel, double click."],
                 ["Undefined names", verdict("PASS"), "16 files, 0."],
                 ["Not run", verdict("NOTED"), "No field test (CrystalHeeler's order). The "
                  "image was not built. The drawing window was not seen in a browser: the page "
                  "tests run its code against stand-in elements, so its look, the drag on a "
                  "touch screen and Pause on a live video are not checked."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, Parts 1 to 6. "
            "Rows this change touches; the rest carry from the 3.3.0-rc1.0 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST, contracts, undefined names",
                  verdict("PASS"), "16 files."],
                 ["1.4 Async", "File writes", verdict("PASS"),
                  "motion.json and recording_zones.json are written in a thread."],
                 ["1.6 Function design", "New file", verdict("PASS"),
                  "Pure functions in anycam_zones.py, testable without the camera state."],
                 ["1.18 Testing", "New tests", verdict("PASS"), "19 server and 19 page checks."],
                 ["2.x JavaScript", "Events", verdict("PASS"),
                  "Pointer events with capture; handlers removed when the window closes."],
                 ["4.x CSS", "Small screens", verdict("PASS"),
                  "The zone panel becomes a bottom sheet under 700 px; 100dvh used."],
                 ["6.1 Security", "Input at the boundary", verdict("PASS"),
                  "Zones validated: count, names, points 0 to 1, levels, crossing lines; zone "
                  "names escaped wherever the page shows them."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"),
                  "Tests use made-up names and addresses only."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets([
              "Answer 8: the whole-picture levels at 128 x 96 are not checked yet; a camera with "
              "zones may need a new whole-picture setting.",
              "Zones belong to one view of a PTZ camera.",
              "<b>B6:</b> the Microseven waits until CrystalHeeler unlocks it.",
              "<b>C21:</b> the classic engine stays as the fallback."])]

    s += [h1("Acceptance (field test)"),
          *bullets([
              "Test system B, ch4: a zone around the far garage door; the door opening records, "
              "the still scene does not.",
              "The tuning line shows the zone's peak; the cog panel lists the zone.",
              "Drawing works with a mouse on a computer and with a finger on a phone.",
              "The recording's entry in the Storage tab names the zone."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_zones.py", "New."],
                 ["anycam_motion.py", "Grid, cells, zone judging, slow look, tuning, the zones "
                  "endpoint, storage."],
                 ["anycam_storage.py", "The zone of each recording."],
                 ["verify_release.py", "The comparison contract moved to _motion_cells."],
                 ["page_script.py, anycam_page.py", "The drawing window, Show zones, the cog "
                  "panel's zones; openFocus for WebRTC and WS-RTSP cameras."],
                 ["camera_discovery.py, anycam_modules.py, Dockerfile", "Route; the new file; "
                  "version 3.4.0-rc1.0."],
                 ["tests/", "Sections AE and C17."],
                 ["CHANGELOG.md, config.yaml, docs", "3.4.0-rc1.0; zones plan status; build "
                  "plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.4.0-rc1.0", story(), "anycam_3_4_0_rc1_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
