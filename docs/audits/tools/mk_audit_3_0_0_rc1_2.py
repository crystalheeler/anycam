"""Audit report for AnyCam 3.0.0-rc1.2 — module split: go2rtc.

    python docs/audits/tools/mk_audit_3_0_0_rc1_2.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.0.0-rc1.2 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - module split: go2rtc - tag 3.0.0-rc1.2" % DATE, S_SUB),
         p("3.0.0-rc1.2 is the third step of build plan E1. AnyCam's behaviour is "
           "meant to stay the same. Scope confirmed by CrystalHeeler on 2026-10-02: go2rtc "
           "only; B19 follows alone as 3.0.0-rc1.3; then tests for the scan and the "
           "scan's move. 3.0.0-rc1.1 passed his field test the same day.")]

    s += [h1("What moved"),
          table([["File", "Content", "Lines"],
                 ["anycam_go2rtc.py", "15 functions, 11 constants and state objects: the "
                  "configuration, the supervisor and its log pump, stream names and "
                  "registration, the relay URL, the card stream, the WebSocket proxy, "
                  "the player script endpoint", "434"],
                 ["camera_discovery.py", "11,667 lines in 3.0.0-rc1.1", "11,299"]],
                [1.5 * inch, 4.2 * inch, 1.0 * inch]),
          p("Stayed in the main file on purpose: _focus_set_go2rtc and "
            "api_go2rtc_focus. They are the Enhanced View engine: they assign "
            "_FOCUSED_CAMERA and _FOCUS_ENGINE under a global statement, and the main "
            "file owns those values. The split script refuses to move a function that "
            "would assign a main-file value. _GO2RTC_TASK also stayed: only main() "
            "uses it.")]

    s += [h1("Values replaced at run time"),
          table([["Value", "Owner now", "How the other file reads it"],
                 ["_GO2RTC_PROC, _GO2RTC_READY", "anycam_go2rtc.py (the supervisor "
                  "assigns them)", "camera_discovery.py reads "
                  "anycam_go2rtc._GO2RTC_PROC (3 places, shutdown) and "
                  "anycam_go2rtc._GO2RTC_READY (1 place, the focus engine)"],
                 ["_GO2RTC_PLAYER_BYTES", "anycam_go2rtc.py", "Used only inside it"],
                 ["_FOCUSED_CAMERA, _FOCUS_ENGINE", "camera_discovery.py", "The moved "
                  "go2rtc functions do not use them"]],
                [1.9 * inch, 2.0 * inch, 2.8 * inch]),
          p("anycam_motion.py took _go2rtc_profiles from the main file through NEEDS. "
            "That function moved, so anycam_motion.py now imports it from "
            "anycam_go2rtc.py directly. It is a function and is never replaced.")]

    s += [h1("Evidence that nothing changed"),
          table([["Check", "Result"],
                 ["Every definition, text for text", "362 definitions in 3.0.0-rc1.1 "
                  "(all 6 files). After: 0 missing, 0 new, 0 changed, apart from the "
                  "anycam_go2rtc. prefix on the two values above. 231 in the main "
                  "file, 26 in anycam_go2rtc.py."],
                 ["Built page", "Same SHA-256 as 3.0.0-rc1.1's, each built in its own "
                  "Python process."],
                 ["Routes", "The same 56 method and path pairs."],
                 ["Packaged zip", "Imported from its own folder."]],
                [1.9 * inch, 4.8 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 7 files compiled; module "
                  "list matches the imports and the Dockerfile; 211 top-level "
                  "definitions; 48 contracts; 30 NEEDS names checked. New: no file "
                  "imports a name that its owner assigns under global."],
                 ["Gate self-test", verdict("PASS"), "'from anycam_go2rtc import "
                  "_GO2RTC_READY' added to the main file: the new check failed and "
                  "named the line, and the behaviour tests failed too. Restored."],
                 ["Python integration", verdict("PASS"), "289 of 289. They include the "
                  "supervisor with a real subprocess, registration against a fake "
                  "go2rtc, and the WebSocket proxy end to end."],
                 ["JavaScript behaviour", verdict("PASS"), "110 of 110."],
                 ["Undefined names", verdict("NOTED"), "7 files, 0 new, 4 known (B19)."],
                 ["Not run", verdict("NOTED"), "The add-on was not started on the build "
                  "PC and the image was not built. No real go2rtc binary ran. The field "
                  "test (A16) is the first real run."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, Parts 1 to "
            "6. Rows this change touches; the rest carry from the 3.0.0-rc1.1 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST, contracts, undefined names",
                  verdict("FAIL"), "Unchanged: probe_http_identity (B19); the fix is "
                  "3.0.0-rc1.3."],
                 ["1.2 Syntax", "compile() per file", verdict("PASS"), "7 files."],
                 ["1.6 Function design", "File size", verdict("NOTED"),
                  "Main file 11,299 lines, 38% under 2.6.8."],
                 ["1.7 Mutability", "Values replaced at run time", verdict("PASS"),
                  "Each such value has one owner file; other files read owner.name; "
                  "the gate enforces it."],
                 ["1.16 Refactoring", "Behaviour kept", verdict("PASS"),
                  "Text comparison of all 362 definitions; page hash; routes."],
                 ["6.1 Security", "go2rtc API address", verdict("PASS"),
                  "The gate's check that GO2RTC_API_HOST is 127.0.0.1 reads the joined "
                  "files and still passes after the move."],
                 ["6.3 Version control", "Commits and gate", verdict("PASS"),
                  "One commit; the gate run before it; tag after."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets([
              "<b>B19</b> (HTTP identity check): 3.0.0-rc1.3, alone.",
              "<b>B20</b> (late pictures): right after 3.0.0.",
              "<b>E1:</b> the scan, password entry, the snapshot loop and the Enhanced "
              "View engine are still in the main file.",
              "The Lorex DVR answers 404 for the sub-stream of channels 4 and 7.",
              "Skip Non-Reference Frames breaks H.264 cameras (B3)."])]

    s += [h1("Acceptance (field test A16)"),
          *bullets([
              "The add-on starts and the log shows go2rtc ready.",
              "Enhanced View plays live; closing and reopening it works.",
              "Cards play live where they did in 3.0.0-rc1.1.",
              "The add-on stops cleanly on restart (no go2rtc process left).",
              "The log has no line with 'not defined' or 'has not run yet'."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["camera_discovery.py", "go2rtc code moved out; reads "
                  "anycam_go2rtc._GO2RTC_PROC and _GO2RTC_READY. Version 3.0.0-rc1.2."],
                 ["anycam_go2rtc.py", "New."],
                 ["anycam_motion.py", "Imports _go2rtc_profiles from anycam_go2rtc.py."],
                 ["anycam_modules.py, Dockerfile", "One more file."],
                 ["verify_release.py", "Check for imported names that the owner "
                  "replaces."],
                 ["docs, CLAUDE.md, README", "The rule for such values; build plan."],
                 ["CHANGELOG.md", "3.0.0-rc1.2 entry."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.0.0-rc1.2", story(), "anycam_3_0_0_rc1_2_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
