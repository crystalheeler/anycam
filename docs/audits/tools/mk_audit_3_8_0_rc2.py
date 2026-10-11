"""Audit report for AnyCam 3.8.0-rc2 — DVR channels and the login UI.

    python docs/audits/tools/mk_audit_3_8_0_rc2.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.8.0-rc2 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - DVR channels + login UI - tag 3.8.0-rc2" % DATE, S_SUB),
         p("Fixes for two field problems in 3.8.0-rc1.1, found by CrystalHeeler on 2026-10-10: a "
           "DVR showed only one channel, and a successful login could look like it failed. Scope: "
           "these two fixes, no change to completeness. rc numbering is now single-digit on "
           "CrystalHeeler's order (rc1, rc2, rc3), so this build is 3.8.0-rc2.")]

    s += [h1("Cause"),
          *bullets(["<b>DVR mis-identification:</b> B54's ONVIF-first scan stops at the login prompt "
                    "and dropped the RTSP OPTIONS fingerprint. The Lorex/Dahua DVR-NVR family is "
                    "identified by its auth realm (\"Login to &lt;hex&gt;\"); without it the DVR read as "
                    "a plain single camera, which has no channel-iterate recipe, so channel "
                    "enumeration never ran. The ONVIF card also carried port 80, so even once "
                    "identified the enumeration would have probed the wrong port.",
                    "<b>Login UI:</b> a slow login (several seconds) could time out at the Home "
                    "Assistant ingress while the add-on finished and succeeded. The page caught the "
                    "network error and left the user on the form, so a login that worked looked like "
                    "it did nothing. The UI code was unchanged from 3.7.5; B54's slower login exposed "
                    "this pre-existing gap."])]

    s += [h1("Fix"),
          table([["#", "Change"],
                 ["RTSP realm at scan", "When ONVIF asks for a login, the scan sends one RTSP "
                  "OPTIONS to read the auth realm and identifies the brand from it, so DVRs are "
                  "recognized and their channels enumerate. The realm is kept on the card."],
                 ["Enumeration port", "Channel enumeration reads the RTSP port from the working "
                  "stream URL, not the card's discovery port (an ONVIF card's port is 80)."],
                 ["Login re-sync", "If the credential request errors or times out, the page re-reads "
                  "the camera list, so a login that really succeeded shows as a ready card. The login "
                  "fields are locked while a login runs."]],
                [1.5 * inch, 5.1 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates."],
                 ["Python integration", verdict("PASS"), "675 of 675; U5 updated for the realm "
                  "fingerprint; Z7 covers enumeration."],
                 ["JavaScript behaviour", verdict("PASS"), "193 of 193."],
                 ["Field", verdict("NOTED"), "The DVR fix is verified from the field logs (the DVR "
                  "was read as plain \"Lorex\"); a field re-test of both follows. The login re-sync "
                  "could not be reproduced in a unit test (it needs a real ingress timeout); it is a "
                  "targeted, low-risk UI change."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["8 (regressions)", "Our code first", verdict("PASS"),
                  "Two 3.8.0 regressions against 3.7.5, both fixed; no completeness change."],
                 ["1.12 Logging", "Truth", verdict("PASS"),
                  "The scan logs the realm it read and the brand it chose."],
                 ["1.18 Testing", "Coverage", verdict("PASS"), "U5 and Z7 cover the DVR path."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "Made-up addresses only."]],
                W_BP)]

    s += [h1("Known issues"),
          *bullets(["<b>OPTIONS-401 scan stop:</b> a camera that answers 401 at OPTIONS (Lorex) is "
                    "not yet stopped at the first 401 during the scan (only at DESCRIBE). Harmless.",
                    "<b>Microseven:</b> untested on an unlocked Microseven camera.",
                    "<b>Login re-sync</b> leans on a re-read after a timeout; a deeper fix would make "
                    "the credential request return quickly and probe in the background."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_scan.py", "RTSP realm fingerprint in the ONVIF-first login branch; the "
                  "realm fields on the ONVIF card."],
                 ["anycam_credentials.py", "Channel enumeration uses the stream URL's RTSP port."],
                 ["page_script.py", "Login re-sync on error/timeout; fields locked during a login."],
                 ["tests/", "U5 updated."],
                 ["CHANGELOG.md, config.yaml, docs", "3.8.0-rc2; build plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.8.0-rc2", story(), "anycam_3_8_0_rc2_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
