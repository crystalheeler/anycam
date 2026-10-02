"""Audit report for AnyCam 3.0.0-rc1.1 — module split, stage 2.

    python docs/audits/tools/mk_audit_3_0_0_rc1_1.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.0.0-rc1.1 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - module split, stage 2 - tag 3.0.0-rc1.1" % DATE, S_SUB),
         p("3.0.0-rc1.1 is the second step of build plan E1. AnyCam's behaviour is "
           "meant to stay the same. CrystalHeeler, 2026-10-02: '3.0.0-rc1.0 is stable. Move to "
           "next phase.' Built: motion detection, motion recording, night boost and "
           "the Storage tab moved to their own files. Not built: go2rtc, the scan, the "
           "password entry path and the snapshot loop stay in the main file.")]

    s += [h1("What moved"),
          table([["File", "Content", "Lines"],
                 ["anycam_motion.py", "58 functions and classes, 37 constants and state "
                  "objects: detection, the pre-roll buffer, recording, per-camera "
                  "settings, night boost, the sunrise check, the Home Assistant calls, "
                  "the four motion endpoints", "1,354"],
                 ["anycam_storage.py", "The five Storage tab endpoints", "147"],
                 ["anycam_host.py", "The link between the files (new code, 44 lines)", "44"],
                 ["camera_discovery.py", "13,125 lines in 3.0.0-rc1.0", "11,667"]],
                [1.5 * inch, 4.2 * inch, 1.0 * inch])]

    s += [h1("Design: how the files connect"),
          p("camera_discovery.py is the entry point and imports the other files. They "
            "cannot import it back: Python would load a second copy of it, with its own "
            "cameras and state. The split files get what they need the other way:"),
          table([["Mechanism", "Use", "Count"],
                 ["NEEDS list", "Names copied onto the module once, at start-up, by "
                  "anycam_host.bind(). For functions (snap_loop, _stop_proc) and for "
                  "objects changed in place (CAMERAS).", "22 names"],
                 ["H.name", "Read from camera_discovery.py at the moment of use. For "
                  "values that file replaces while it runs.", "2: _FOCUSED_CAMERA, "
                  "_FOCUS_ENGINE, one line"],
                 ["Imports back", "camera_discovery.py imports the 15 names it calls "
                  "from the two new files.", "15 names"]],
                [1.2 * inch, 4.1 * inch, 1.4 * inch]),
          p("go2rtc did not move. Its functions assign _FOCUSED_CAMERA and "
            "_FOCUS_ENGINE under a global statement; in another file that statement "
            "would create a separate value. It needs a link that can write, which is "
            "a change in behaviour risk and was left out of this step.")]

    s += [h1("Evidence that nothing changed"),
          table([["Check", "Result"],
                 ["Every definition, text for text", "357 definitions in 3.0.0-rc1.0. "
                  "After the split: 0 missing, 0 changed, apart from the two H. reads. "
                  "257 in the main file, 95 in anycam_motion.py, 5 in "
                  "anycam_storage.py. The first run found 2 differences (a blank line "
                  "lost inside build_html and _rtsp_options_fingerprint by the split "
                  "script); both were restored and the check rerun."],
                 ["Built page", "Identical to 3.0.0-rc1.0's."],
                 ["Routes", "The same set of routes as 3.0.0-rc1.0."],
                 ["Shared state", "camera_discovery._MOTION is the same object as "
                  "anycam_motion._MOTION; H follows a replaced value."],
                 ["Packaged zip", "Imported from its own folder."]],
                [1.9 * inch, 4.8 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 6 files compiled; the "
                  "module list matches the imports and the Dockerfile; 211 top-level "
                  "definitions, no duplicates; 48 contracts. New: the 22 NEEDS names and "
                  "the H names exist in the main file and none is replaced at run time."],
                 ["Gate self-test", verdict("PASS"), "_FOCUSED_CAMERA added to NEEDS: the "
                  "gate failed. _stop_proc removed from NEEDS: the undefined-name check "
                  "failed and named both functions. Restored."],
                 ["Python integration", verdict("PASS"), "289 of 289, unchanged in "
                  "content. The tests see the files as one namespace: a stand-in is "
                  "written to every file that holds the name."],
                 ["JavaScript behaviour", verdict("PASS"), "110 of 110."],
                 ["Undefined names", verdict("NOTED"), "6 files, 0 new, 4 known (B19)."],
                 ["Not run", verdict("NOTED"), "The add-on was not started on the build "
                  "PC: it would scan this network. No Docker: the image was not built. "
                  "The field test (A15) is the first real run."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, Parts 1 to "
            "6. Rows this change touches; the rest carry from the 3.0.0-rc1.0 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST, contracts, undefined names",
                  verdict("FAIL"), "Unchanged: probe_http_identity (B19), fix planned "
                  "right after 3.0.0."],
                 ["1.2 Syntax", "compile() per file", verdict("PASS"), "6 files."],
                 ["1.6 Function design", "File size", verdict("NOTED"),
                  "Main file 11,667 lines, 36% under 2.6.8. snap_loop is unchanged."],
                 ["1.7 Mutability", "Shared state", verdict("NOTED"),
                  "State moved with its code; none added. NEEDS copies are safe only "
                  "for names never replaced, and the gate enforces that."],
                 ["1.14 Class design", "_Host", verdict("PASS"),
                  "Read-only by design: assignment raises, so a split file cannot "
                  "write a main-file value by accident."],
                 ["1.16 Refactoring", "Behaviour kept", verdict("PASS"),
                  "Text comparison of all 357 definitions."],
                 ["1.18 Testing", "Tests", verdict("NOTED"),
                  "No new behaviour, no new behaviour tests. Two new structural checks. "
                  "Coverage gaps unchanged."],
                 ["6.2 Dependencies", "None added", verdict("PASS"), "Gate 7."],
                 ["6.3 Version control", "Commits and gate", verdict("PASS"),
                  "One commit; the gate run before it; tag after."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets([
              "<b>B19</b> (HTTP identity check) and <b>B20</b> (late pictures): the "
              "build right after 3.0.0.",
              "<b>E1:</b> go2rtc, the scan, password entry and the snapshot loop are "
              "still in the main file.",
              "The Lorex DVR answers 404 for the sub-stream of channels 4 and 7.",
              "Skip Non-Reference Frames breaks H.264 cameras (B3)."])]

    s += [h1("Acceptance (field test A15)"),
          *bullets([
              "The image builds and the add-on starts.",
              "Arming a camera, the cog settings and the night-mode line work.",
              "A motion event records, with its pre-roll, to LorexCHn_ files.",
              "The Storage tab lists, downloads, renames and deletes.",
              "The log has no line with 'not defined' or 'has not run yet'."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["camera_discovery.py", "Motion and storage code moved out; imports "
                  "and the bind call added. Version 3.0.0-rc1.1."],
                 ["anycam_motion.py, anycam_storage.py, anycam_host.py", "New."],
                 ["anycam_modules.py, Dockerfile", "Three more files."],
                 ["verify_release.py", "NEEDS and H checks."],
                 ["tests/", "One-namespace view of the files; the name check covers "
                  "every file."],
                 ["docs, CLAUDE.md, README", "How the files connect; build plan."],
                 ["CHANGELOG.md", "3.0.0-rc1.1 entry."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.0.0-rc1.1", story(), "anycam_3_0_0_rc1_1_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
