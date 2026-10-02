"""Audit report for AnyCam 3.0.0-rc1.5 — scan Cancel, tests and the last module moves.

    python docs/audits/tools/mk_audit_3_0_0_rc1_5.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.0.0-rc1.5 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - scan Cancel, tests, the last module moves - tag "
                   "3.0.0-rc1.5" % DATE, S_SUB),
         p("Scope confirmed by CrystalHeeler on 2026-10-02: (1) build plan B22, the scan's "
           "Cancel button. (2) Tests, then a move to its own file, for each part still in "
           "the main file: password entry, the snapshot loop, the Enhanced View engine, the "
           "manufacturer database, brand identification and the page builder (build plan "
           "E1). (3) The last type hints. Item 1 changes behaviour. Items 2 and 3 change "
           "no behaviour.")]

    s += [h1("1. The scan's Cancel button (B22)"),
          p("The Cancel endpoint set a flag named _SCAN_CANCELLED in camera_discovery.py. "
            "The scan read SCAN_CANCELLED in anycam_scan.py, a different name. Now there is "
            "one flag, anycam_scan.SCAN_CANCELLED, which the endpoint sets."),
          table([["Where the scan checks it", "What happens"],
                 ["Before the port scan (stage 2)", "Stops; the status says 'Scan cancelled'."],
                 ["Before each host, and before each port", "No more probing."],
                 ["Before each ONVIF-only device", "No more probing."],
                 ["At the end", "'Scan cancelled - N device(s), M streaming.' The flag is "
                  "cleared when the next scan starts."]],
                [2.6 * inch, 4.1 * inch]),
          p("5 checks (U14, U15): Cancel during the port probing stops it after the port in "
            "progress and probes no ONVIF-only device; Cancel during discovery runs no port "
            "scan; the next scan starts with the flag cleared.")]

    s += [h1("2. The last type hints"),
          p("30 functions had no type for an argument or the return value: the request "
            "argument of 19 web handlers, a log handler, an inner sort key and the background "
            "codec check in the main file, and 8 in anycam_host.py, anycam_go2rtc.py, anycam_motion.py and "
            "anycam_scan.py. Now every "
            "function in all 14 files is fully typed (an AST count: 0 missing). The code is "
            "unchanged apart from the annotations: each file's AST, with the annotations "
            "removed, is identical before and after.")]

    s += [h1("3. Tests for the parts that moved"),
          p("122 new checks in tests/test_server.py. Each section was written and passing "
            "before its code moved, and passing unchanged after. Stand-ins replace only what "
            "touches the network or a process: the ONVIF calls, the RTSP walkers, ffprobe, "
            "ffmpeg (a fake process that writes JPEGs) and the IEEE download. A local HTTP "
            "server plays a camera for the HTTP snapshot loop."),
          table([["Section", "Checks", "What they pin"],
                 ["V", "24", "OUI key forms; lookup order (downloaded table, then the built-in "
                  "camera and non-camera lists); camera or not; the cache read once; refresh "
                  "only when 30 days old, its parse and save, a failed download; brand scoring "
                  "(camera before NVR, word boundaries for short keywords); the RTSP realm "
                  "rule; the generic entry; the brand throttle."],
                 ["W", "7", "A complete page with every placeholder filled; the settings and "
                  "the community address in the script (< escaped); handle_index builds the "
                  "page once."],
                 ["X", "9", "Unknown camera; classic entry clears a stale leave flag and the "
                  "hardware failure count; the motion file reads the focus state (classic view: "
                  "no second loop; live view: the loop restarts); classic leave stops the "
                  "preheater and keeps the learned tier."],
                 ["Y", "42", "ffmpeg arguments for card view, the settings and Enhanced View; "
                  "JPEGs joined across reads; the idle stop; the transport flip after 3 empty "
                  "runs and the codec clear after 5; the focus-leave flag; HTTP snapshots or "
                  "ffmpeg; hardware decode and its software fallback; the quality ladder; the "
                  "preheater; ffmpeg's error output (password removed, H.265+ flags); the "
                  "H.265+ fallback; the snapshot endpoint in card and focus view; the status and "
                  "log endpoints; HTTP Digest and query-string logins."],
                 ["Z", "40", "Password entry by ONVIF (one-socket check at the RTSP port, "
                  "ranking, duplicate dropped, identity kept, codec corrected in the "
                  "background), by direct RTSP (port 554 first), for a rate-limited camera "
                  "(stream table, locked streams, pacing), a wrong password, MJPEG, a DVR (one "
                  "card per channel, the status endpoint); clear credentials; manual add; Deep "
                  "Re-Probe (fresh, resumed, second stage, stale); the stream table."]],
                [0.7 * inch, 0.75 * inch, 5.25 * inch])]

    s += [h1("Defects found while writing the tests"),
          p("Both recorded in the build plan, not fixed: the scope had no behaviour change for "
            "these parts. No check pins either defect."),
          table([["#", "Defect", "Effect"],
                 ["B23", "api_add_camera makes the protocol upper case, then compares it with "
                  "'WebRTC'.", "Adding a camera by hand with protocol WebRTC always answers "
                  "400 'Could not connect'. WS-RTSP works."],
                 ["B24", "_probe_db_streams paces a camera only if its STREAM_DB entry has "
                  "throttle_type. No STREAM_DB entry has that key; it is in CAMERA_DB.",
                  "On a rate-limited camera (the Microseven) the stream-table check and each "
                  "ffprobe after it run without the 5 s wait. Reached only on the direct-RTSP "
                  "path, or the ONVIF path with fewer than two working profiles."]],
                [0.6 * inch, 3.2 * inch, 2.9 * inch])]

    s += [h1("4. The move"),
          table([["File", "Content", "Lines"],
                 ["anycam_credentials.py", "Password entry, the stream table match and probe, "
                  "the DVR channel list and its status endpoint, clear credentials, Deep "
                  "Re-Probe, manual add", "1,710"],
                 ["anycam_snap.py", "snap_loop, http_snap_loop, the snapshot, status and log "
                  "endpoints, the hardware preheater, ffmpeg's error output, the H.265+ "
                  "fallback, the focus ladder", "1,881"],
                 ["anycam_focus.py", "The Enhanced View engine and the focus state "
                  "(_FOCUSED_CAMERA, _FOCUS_ENGINE)", "277"],
                 ["anycam_brand.py", "The OUI database, the keyword tables and the loop that "
                  "builds them, brand identification", "471"],
                 ["anycam_page.py", "build_html and handle_index", "642"],
                 ["camera_discovery.py", "Settings, stores, the REST API, routing, start-up. "
                  "6,780 lines in 3.0.0-rc1.4", "1,797"]],
                [1.6 * inch, 4.1 * inch, 1.0 * inch]),
          table([["Link", "Detail"],
                 ["The focus state", "Only the focus functions replace it, so it moved with "
                  "them. The main file, anycam_snap.py and anycam_motion.py read "
                  "anycam_focus._FOCUSED_CAMERA. No file reads through H any more."],
                 ["No import cycles", "A split file imports another only if that file does "
                  "not import it back, directly or through a third. Five names go through the "
                  "main file's NEEDS for that reason: _MOTION (anycam_focus.py), "
                  "_kill_hw_preheater (anycam_focus.py), _drain_stderr and _stop_proc "
                  "(anycam_motion.py), _match_stream_db (anycam_probe.py)."],
                 ["Settings", "The settings block (paths and options read from the "
                  "environment) stays in the main file; the moved code takes those names "
                  "through NEEDS."],
                 ["NEEDS lists", "117 names across all split files, checked by the gate."]],
                [1.6 * inch, 5.1 * inch]),
          p("The split tool (outside the repository) gained the rules above: no cycles, a "
            "moved value rewritten where other files read it, settings kept, a module-level "
            "loop moved with its tables, an import alias kept.")]

    s += [h1("Evidence that the move changed nothing"),
          table([["Check", "Result"],
                 ["Every definition, text for text, after each of the 5 moves",
                  "363 before, 363 after; 0 missing, 0 changed, apart from the anycam_focus. "
                  "prefix on the focus state. 124 in the main file."],
                 ["Built page and routes, after each move", "Same page hash; the same 56 routes."],
                 ["Tests, after each move", "448 of 448 server checks, 114 of 114 page checks."],
                 ["Main file tidy-up", "265 lines of empty banners, stale comments and blank "
                  "runs removed; the file's AST identical before and after."],
                 ["Packaged zip", "Imported from its own folder."]],
                [2.4 * inch, 4.3 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 14 files compiled; module "
                  "list matches the imports and the Dockerfile; 212 top-level definitions; 48 "
                  "contracts; 117 NEEDS names; build inputs checked online."],
                 ["Python integration", verdict("PASS"), "448 of 448, 122 new (and 5 for B22)."],
                 ["JavaScript behaviour", verdict("PASS"), "114 of 114."],
                 ["Undefined names", verdict("PASS"), "14 files, 0."],
                 ["Not run", verdict("NOTED"), "The image was not built and nothing ran on a "
                  "camera from the build PC. The tests use stand-ins for ffmpeg, ffprobe and "
                  "the probers. The field test (A19) is the first run of the moved code."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, Parts 1 to 6. "
            "Rows this change touches; the rest carry from the 3.0.0-rc1.4 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST, contracts, undefined names",
                  verdict("PASS"), "14 files."],
                 ["1.2 Syntax", "compile() per file", verdict("PASS"), "14 files."],
                 ["1.3 Runtime errors", "Names", verdict("PASS"),
                  "B22 fixed: one cancel flag."],
                 ["1.6 Function design", "File size", verdict("NOTED"),
                  "Main file 1,797 lines. snap_loop (1,048 lines) and api_set_credentials "
                  "(865) moved whole; splitting them is a behaviour risk and was not in scope."],
                 ["1.7 Mutability", "Values replaced at run time", verdict("PASS"),
                  "The focus state has one owner file; the gate enforces the import rule."],
                 ["1.14 Type checking", "AST count", verdict("PASS"), "0 functions missing a type."],
                 ["1.16 Refactoring", "Behaviour kept", verdict("PASS"),
                  "Text comparison of 363 definitions after each move; page hash; routes; "
                  "tests before and after."],
                 ["1.18 Testing", "New tests", verdict("PASS"),
                  "122 checks, written before the moves; they found B23 and B24."],
                 ["6.3 Version control", "Commits and gate", verdict("PASS"),
                  "One commit for each part; the gate run before the release commit."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"),
                  "Tests and documents use made-up addresses only."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets([
              "<b>B23:</b> adding a camera by hand with protocol WebRTC fails.",
              "<b>B24:</b> the stream-table check during password entry does not pace "
              "rate-limited cameras.",
              "<b>B20</b> (late pictures, insects on the fallback): right after 3.0.0.",
              "<b>C17</b> (detection zones): design discussion pending.",
              "Skip Non-Reference Frames breaks H.264 cameras (B3)."])]

    s += [h1("Acceptance (field test A19)"),
          *bullets([
              "The add-on starts; the start-up scan finds the same cameras as before.",
              "Cancel during a scan stops it, and the status says 'Scan cancelled'.",
              "A camera password is accepted; on test system B the DVR channel cards appear.",
              "Cards show pictures; Enhanced View opens in live and in classic view.",
              "Motion detection still records.",
              "The log has no line with 'not defined' or 'has not run yet'."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["camera_discovery.py", "Five parts moved out; the cancel flag; type hints; "
                  "empty banners removed. Version 3.0.0-rc1.5."],
                 ["anycam_credentials.py, anycam_snap.py, anycam_focus.py, anycam_brand.py, "
                  "anycam_page.py", "New."],
                 ["anycam_scan.py", "The cancel flag and its checks; imports from anycam_brand."],
                 ["anycam_motion.py", "Reads anycam_focus._FOCUSED_CAMERA; type hints."],
                 ["anycam_probe.py", "Imports from anycam_brand."],
                 ["anycam_host.py, anycam_go2rtc.py", "Type hints; docstring."],
                 ["anycam_modules.py, Dockerfile", "Five more files."],
                 ["tests/", "127 server checks; run_tests.py builds the page from anycam_page.py."],
                 ["docs, CLAUDE.md, README", "Module list, no-cycle rule; build plan."],
                 ["CHANGELOG.md, config.yaml", "3.0.0-rc1.5."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.0.0-rc1.5", story(), "anycam_3_0_0_rc1_5_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
