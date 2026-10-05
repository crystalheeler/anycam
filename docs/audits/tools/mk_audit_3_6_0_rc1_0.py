"""Audit report for AnyCam 3.6.0-rc1.0 — recording upload.

    python docs/audits/tools/mk_audit_3_6_0_rc1_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.6.0-rc1.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - recording upload by SFTP, FTPS or FTP - tag "
                   "3.6.0-rc1.0" % DATE, S_SUB),
         p("Scope: phase 3.6.0 of the build plan: C14, with CrystalHeeler's defaults of "
           "2026-10-04: SFTP, FTPS and FTP; per camera plus global; upload, delete the local copy, "
           "retry; passwords encrypted. Built in the untested run on top of 3.5.0-rc1.0. This "
           "build is not field-tested.")]

    s += [h1("1. Design"),
          table([["Part", "Design"],
                 ["Destinations", "A global one (Storage tab, Upload) and, for each camera, "
                  "global, its own, or off (camera settings, Upload). Kept in /data/upload.json."],
                 ["When", "When a recording stops: _motion_finish_files hands over the files "
                  "under their final names."],
                 ["Where", "<folder>/<camera folder>/<file>, the same layout as under /media."],
                 ["How", "Written as <file>.part, then renamed, so a reader never sees half a "
                  "file. SFTP by asyncssh; FTP and FTPS by Python's ftplib (FTPS encrypts the "
                  "data connection too, PROT P)."],
                 ["After", "The local copy is deleted unless the destination says to keep it."],
                 ["Failure", "The file stays; the next try is 1 min later, doubling to 30 min. "
                  "One warning per file, then debug lines. The queue is in "
                  "/data/upload_queue.json, so it survives a restart."],
                 ["Test", "Uploads a small text file to <folder>/anycam_upload_test/."]],
                [1.2 * inch, 5.5 * inch])]

    s += [h1("2. Security review"),
          table([["Risk", "Control"],
                 ["Passwords at rest", "Encrypted with the same key and code as camera "
                  "passwords (encrypt_creds)."],
                 ["Passwords to the page", "Never sent: the page gets has_password; an empty "
                  "password field keeps the saved one."],
                 ["SFTP server swapped", "Trust on first use: the server key's fingerprint is "
                  "saved at the first upload; a different key stops the uploads and the log "
                  "says so. Saving the destination again accepts the new key (for a server "
                  "that was replaced on purpose)."],
                 ["FTP in clear text", "Offered because CrystalHeeler chose it; the form warns "
                  "that FTP sends the password and the recordings unencrypted."],
                 ["Path tricks", "The folder must start with / and must not contain .. ; the "
                  "server name must have no spaces, slashes, @, ? or #."],
                 ["New dependency", "asyncssh 2.23.1 (with typing_extensions 4.16.0), pinned. "
                  "2.24.x needs cryptography 48.0.1; AnyCam pins 48.0.0, so the newest working "
                  "release was chosen instead of changing the encryption library in an untested "
                  "build. The release gate now accepts a pure-Python wheel."]],
                [1.6 * inch, 5.1 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 17 files; asyncssh and "
                  "typing_extensions found on PyPI as pure-Python wheels."],
                 ["Python integration", verdict("PASS"), "560 of 560; 18 new in section AG "
                  "with stand-ins for ftplib and asyncssh; L13 and Q4 updated for the new "
                  "route and help text."],
                 ["JavaScript behaviour", verdict("PASS"), "158 of 158, unchanged; the upload "
                  "form's 17 element ids all exist in the page."],
                 ["Undefined names", verdict("PASS"), "17 files, 0."],
                 ["Not run", verdict("NOTED"), "No field test (CrystalHeeler's order). The "
                  "image was not built, so asyncssh was not installed or imported for real. No "
                  "real SFTP, FTPS or FTP server was used. The upload form was not seen in a "
                  "browser."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, Parts 1 to 6. "
            "Rows this change touches; the rest carry from the 3.5.0-rc1.0 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST, contracts, undefined names",
                  verdict("PASS"), "17 files."],
                 ["1.4 Async", "Blocking I/O", verdict("PASS"),
                  "ftplib and file writes run in a thread; SFTP is async."],
                 ["1.10 Exceptions", "Failure paths", verdict("PASS"),
                  "Any upload failure keeps the file and schedules a retry."],
                 ["1.11 Resources", "Connections", verdict("PASS"),
                  "One connection per file, closed in finally or by the context manager."],
                 ["1.18 Testing", "New tests", verdict("PASS"), "18 server checks."],
                 ["6.1 Security", "Input, secrets", verdict("PASS"), "See section 2."],
                 ["6.2 Dependencies", "Pins", verdict("PASS"),
                  "Exact versions; checked online by the gate."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"),
                  "Tests use example.com-style names only."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets([
              "FTP is unencrypted by design.",
              "A recording uploads only after it stops.",
              "<b>B6:</b> the Microseven waits until CrystalHeeler unlocks it.",
              "<b>C21:</b> the classic engine stays as the fallback."])]

    s += [h1("Acceptance (field test)"),
          *bullets([
              "The add-on image builds with asyncssh, and starts.",
              "Test against an SFTP server: the test file arrives.",
              "A motion recording arrives on the server under <folder>/<camera>/, and the "
              "local copy is gone.",
              "With the server off, the recording stays and uploads when the server is back."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_upload.py", "New."],
                 ["anycam_motion.py", "Finished files go to the upload queue."],
                 ["camera_discovery.py", "Routes, start-up, version 3.6.0-rc1.0."],
                 ["page_script.py, anycam_page.py", "The upload form; Storage tab and camera "
                  "settings buttons."],
                 ["Dockerfile, verify_release.py", "asyncssh and typing_extensions; the gate "
                  "accepts a pure-Python wheel; the new file."],
                 ["anycam_modules.py, tests/", "The new file; section AG; L13, Q4."],
                 ["CHANGELOG.md, config.yaml, CLAUDE.md, README, docs", "3.6.0-rc1.0; build "
                  "plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.6.0-rc1.0", story(), "anycam_3_6_0_rc1_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
