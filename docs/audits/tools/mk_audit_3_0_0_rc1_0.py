"""Audit report for AnyCam 3.0.0-rc1.0 — code structure release.

    python docs/audits/tools/mk_audit_3_0_0_rc1_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph


def story():
    s = [Paragraph("AnyCam 3.0.0-rc1.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - tests in the repository, module split stage 1, "
                   "log redaction, dead endpoints - tag 3.0.0-rc1.0" % DATE, S_SUB),
         p("3.0.0-rc1.0 is a code structure release: AnyCam's behaviour is meant to "
           "stay the same. Scope confirmed by CrystalHeeler on 2026-10-02: build plan E7, E6, "
           "E8, E4 and E1; E2, E3 and E5 dropped; version 3.0.0-rc1.0; stop and ask on "
           "problems. Built: E7, E6, E8, E4 and stage 1 of E1. Not built: E1 stages 2 "
           "and 3, which wait for the field test of this build.")]

    s += [h1("What changed"),
          table([["Item", "Change", "Evidence"],
                 ["E7 tests", "tests/: test_server.py (289 checks), test_page.mjs (110), "
                  "check_names.py, run_tests.py. Gate 8 runs them.",
                  "Run time 21 s. MOTION_NIGHT_BOOST changed to 14 on purpose: the gate "
                  "failed with 3 named checks; restored: passed."],
                 ["E6 old stream endpoint", "handle_stream and its route removed, 137 "
                  "lines.", "No caller in the page or the tests. No function became "
                  "unused through the removal."],
                 ["E8 manual tiers", "handle_focus_set_tier, handle_focus_profiles, "
                  "their routes, tier_change_kill, manual_override removed, 274 lines.",
                  "manual_override was set only by the removed handler and is not "
                  "saved to disk, so the two conditions that read it reduce to the "
                  "Adaptive Quality setting alone."],
                 ["E4 log redaction", "One filter on every log handler removes "
                  "user:password@ and password-style query values. _strip_creds is "
                  "bounded to the URL's host part.",
                  "10 checks: AnyCam lines, %-style arguments, a library logger, the "
                  "page's log panel, a password that contains '@'."],
                 ["E1 stage 1", "camera_db.py (STREAM_DB, CAMERA_DB; 1,939 lines) and "
                  "page_script.py (2,782 lines). camera_discovery.py: 18,198 lines in "
                  "2.6.8, 13,125 now.",
                  "Old and new loaded side by side: both tables equal (76 brands, 40 "
                  "stream entries), the script equal (124,783 characters), the built "
                  "page identical (163,326 characters), the same module names. The "
                  "packaged zip imports from its own folder."]],
                [1.2 * inch, 2.9 * inch, 2.6 * inch])]

    s += [h1("Defect found: probe_http_identity (build plan B19)"),
          p("The new undefined-name check reads every function's symbol table and "
            "reports each global name that no module defines. On its first run it "
            "reported four, all one defect."),
          table([["Fact", "Detail"],
                 ["Cause", "Commit 2.4.0-rc1.0 added _rtsp_options_fingerprint directly "
                  "above probe_http_identity and lost the line 'def "
                  "probe_http_identity(ip, port, timeout)'. The body remains, after a "
                  "return statement, unreachable. Present in every release since."],
                 ["Effect 1", "run_scan's HTTP identity step raises a name error. A "
                  "broad except catches it and logs at DEBUG. The step runs only for a "
                  "device that answers ONVIF discovery and has no card from the port "
                  "scan. CORRECTED 2026-10-02: this report first said 'on every "
                  "device'. CrystalHeeler's logs from both systems hold 8 scans and 2,849 DEBUG "
                  "lines; the step ran 0 times."],
                 ["Effect 2", "None. probe_http_for_camera calls the missing function, "
                  "but its only caller, is_camera_positive, has no caller. CORRECTED "
                  "2026-10-02: this report first said step 8 of the camera check "
                  "fails."],
                 ["Possible link", "Build plan B2, one or two cameras not discovered: "
                  "only if such a camera answers ONVIF discovery with no open scanned "
                  "port. Not shown."],
                 ["This build", "Not fixed. Restoring the line changes scan results, and "
                  "this release is meant to change no behaviour. The four names are "
                  "listed as known in tests/check_names.py; any new one fails the "
                  "gate. CrystalHeeler decides (B19)."]],
                [1.2 * inch, 5.5 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates. Gate 1 compiles each of "
                  "the 3 files, checks anycam_modules.py against the imports and the "
                  "Dockerfile (a removed COPY entry failed it, on purpose), 209 "
                  "top-level definitions, no duplicates. 48 contracts. Gate 8 runs the "
                  "tests."],
                 ["Python integration", verdict("PASS"), "289 of 289; 11 new (10 "
                  "redaction, 1 for the removed endpoints)."],
                 ["JavaScript behaviour", verdict("PASS"), "110 of 110, from the page as "
                  "built from page_script.py."],
                 ["Undefined names", verdict("NOTED"), "0 new, 4 known (B19)."],
                 ["Not run", verdict("NOTED"), "No Docker on the build PC: the image was "
                  "not built, so the new COPY line is checked by the gate's text check "
                  "and by importing the unpacked zip only. No test covers the scan, the "
                  "password entry path or most of the snapshot loop, where E6 and E8 "
                  "removed code."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [PageBreak(),
          h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, Parts 1 to "
            "6, as CLAUDE.md rule 1 requires."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST, duplicates, contracts, undefined names",
                  verdict("FAIL"), "probe_http_identity has been truncated since "
                  "2.4.0-rc1.0 (B19). Found by this release's new check; five earlier "
                  "audits passed this row without it."],
                 ["1.2 Syntax", "compile() per file", verdict("PASS"), "Gate 1."],
                 ["1.4 Async/await", "Blocking work on the loop", verdict("PASS"),
                  "Gate 3 clean. The log filter adds two regular expressions per log "
                  "line, each guarded by a character test."],
                 ["1.5 Style: line length", "Measured added lines", verdict("NOTED"),
                  "New code under 100 characters; moved code unchanged."],
                 ["1.6 Function design", "File size", verdict("NOTED"),
                  "Main file 13,125 lines, down 28%. snap_loop is still about 1,050 "
                  "lines; stages 2 and 3 are open (E1)."],
                 ["1.7 Mutability", "Shared state", verdict("NOTED"),
                  "Unchanged: 66 module-level state objects. Stage 2 needs one shared "
                  "state module."],
                 ["1.10 Exceptions", "Catches", verdict("NOTED"),
                  "The broad except around the HTTP identity step would hide B19's "
                  "name error at DEBUG level. The log filter catches only TypeError "
                  "and ValueError."],
                 ["1.12 Logging", "Credentials", verdict("PASS"),
                  "E4: every handler filtered; tested."],
                 ["1.16 Refactoring", "Dead code", verdict("PASS"),
                  "411 lines of unreachable endpoints and flags removed. One unused "
                  "function remains from before, handle_storage_page."],
                 ["1.17 Stdlib first", "New tools", verdict("PASS"),
                  "The undefined-name check uses symtable; pyflakes is not installed "
                  "and was not added."],
                 ["1.18 Testing", "Tests in the repository", verdict("PASS"),
                  "E7. Coverage gaps stated in tests/README.md."],
                 ["2.x JavaScript", "Page script", verdict("PASS"),
                  "Moved, not edited; byte-identical."],
                 ["5.x Polyglot", "Placeholders", verdict("PASS"),
                  "Gate 3 P7 reads the joined files, so it still sees the script's "
                  "placeholders and build_html's replacements."],
                 ["6.1 Security", "Attack surface, logs", verdict("PASS"),
                  "Three endpoints removed, one of which started a process per request. "
                  "Credentials removed from logs."],
                 ["6.2 Dependencies", "None added", verdict("PASS"), "Gate 7."],
                 ["6.3 Version control", "Commits and gate", verdict("PASS"),
                  "One commit per item, the gate run before each."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets([
              "<b>B19:</b> the HTTP identity check does not run. CrystalHeeler decides.",
              "<b>E1 stages 2 and 3</b> are open. Stage 3 needs new tests first.",
              "The Microseven on the test system A needs a power cycle (B6).",
              "The Lorex DVR beeping: cause unknown; waiting for the test system B log and the "
              "DVR's event entry.",
              "Skip Non-Reference Frames breaks H.264 cameras (B3)."])]

    s += [h1("Acceptance (field test A14)"),
          *bullets([
              "The image builds and the add-on starts; the page loads.",
              "A scan finds the same cameras as 2.6.8.",
              "Live view, cards, recording and night boost work as in 2.6.8.",
              "The classic view still steps its quality on its own.",
              "A log taken after entering a camera password shows no password."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["camera_discovery.py", "Tables and script moved out; three endpoints "
                  "removed; log filter. Version 3.0.0-rc1.0."],
                 ["camera_db.py, page_script.py, anycam_modules.py", "New."],
                 ["tests/", "New: 6 files, 2,612 lines."],
                 ["verify_release.py", "Gate 8; several files; module list checks; 2 new "
                  "contracts."],
                 ["package_release.py", "New: the packager, in the repository."],
                 ["Dockerfile", "COPY for the two new modules."],
                 ["docs", "Build plan, CLAUDE.md, README; audit tools in "
                  "docs/audits/tools."],
                 ["CHANGELOG.md", "3.0.0-rc1.0 entry."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.0.0-rc1.0", story(), "anycam_3_0_0_rc1_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
