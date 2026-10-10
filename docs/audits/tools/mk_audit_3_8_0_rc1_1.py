"""Audit report for AnyCam 3.8.0-rc1.1 — login hotfix for 3.8.0-rc1.0.

    python docs/audits/tools/mk_audit_3_8_0_rc1_1.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.8.0-rc1.1 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - login hotfix - tag 3.8.0-rc1.1" % DATE, S_SUB),
         p("A hotfix for a login regression in 3.8.0-rc1.0, found by CrystalHeeler in the field "
           "on 2026-10-10 (both test systems: no camera could log in). Scope: one bug fix plus "
           "its regression test. Found from the field logs.")]

    s += [h1("Cause"),
          p("B54 added the 0/1/2 rule: at password entry the walk looks for up to two streams "
            "(want_streams=2) on one connection. After the first stream succeeded, the walk kept "
            "going for a second. Cameras that close the connection after the first stream "
            "(Hikvision, Lorex and Dahua recorders, the Hipcam/Microseven family) then reset the "
            "next request; the walker's socket-death handlers returned None, throwing away the "
            "stream already found. Every login on those cameras failed with \"no working stream\". "
            "In 3.7.5 the walk returned on the first stream, so the second request never happened.")]

    s += [h1("Fix"),
          table([["#", "Change"],
                 ["Sub-stream kept", "When a camera drops the connection after the first stream, the sub-stream is now found on a fresh connection (as before B54), so the card still offers it."],
                 ["Keep the found stream", "The five socket-death returns in the single-socket "
                  "walker (OPTIONS, DESCRIBE, DESCRIBE-auth and SETUP exceptions, and the outer "
                  "handler) now return the first stream already found, not None. Looking for a "
                  "second stream is best-effort; a dropped connection no longer loses the first."],
                 ["Regression tests", "AP8: a camera serves one stream then drops the connection; the walk "
                  "must still return it. Z4d: the sub-stream then comes from a fresh-connection probe."]],
                [1.6 * inch, 5.1 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates."],
                 ["Python integration", verdict("PASS"), "675 of 675; AP8 and Z4d added."],
                 ["JavaScript behaviour", verdict("PASS"), "193 of 193."],
                 ["Field", verdict("NOTED"), "The failure was reproduced from the 2026-10-10 logs "
                  "(Hikvision and Lorex). The fix is verified by AP8; a field re-test follows."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.10 Exceptions", "No silent data loss", verdict("PASS"),
                  "Socket-death handlers preserve the found result."],
                 ["1.18 Testing", "Regression test first", verdict("PASS"), "AP8 covers the exact case."],
                 ["8 (regressions)", "Our code first", verdict("PASS"),
                  "A 3.8.0-rc1.0 regression against 3.7.5, fixed."]],
                W_BP)]

    s += [h1("Known issues"),
          *bullets(["<b>OPTIONS-401 scan stop:</b> a camera that answers 401 at OPTIONS (Lorex) is "
                    "not yet stopped at the first 401 during the scan (only at DESCRIBE). Harmless; "
                    "tracked for a later build.",
                    "<b>Microseven:</b> the new scan and the ffmpeg copy are untested on an unlocked "
                    "Microseven camera."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_probe.py", "Five socket-death returns keep the found stream."],
                 ["anycam_credentials.py", "Fresh-connection sub-stream probe when the walk found none."],
                 ["tests/test_server.py", "AP8 and Z4d."],
                 ["CHANGELOG.md, config.yaml, docs", "3.8.0-rc1.1; build plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.8.0-rc1.1", story(), "anycam_3_8_0_rc1_1_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
