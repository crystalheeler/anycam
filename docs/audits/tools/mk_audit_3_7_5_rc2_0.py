"""Audit report for AnyCam 3.7.5-rc2.0 — start-up traffic and fragile cameras.

    python docs/audits/tools/mk_audit_3_7_5_rc2_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.7.5-rc2.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - start-up traffic and fragile cameras - tag 3.7.5-rc2.0"
                   % DATE, S_SUB),
         p("Scope confirmed by CrystalHeeler on 2026-10-08: B53 for every brand, with "
           "CrystalHeeler's design for removed cameras (scannable again, details kept so a scan "
           "gives the card back without probing), and the Quality Switch's failure notice. "
           "Not field-tested.")]

    s += [h1("Cause (from the 2026-10-08 log)"),
          p("AnyCam's one-connection RTSP check got \"Connection reset by peer\" at all 32 checks "
            "from 2026-10-07 20:12 to 22:47, also while nothing else contacted the camera: the "
            "Microseven was stuck. Both times it got stuck, AnyCam had just run its start-up or "
            "rescan checks on it: a 28-path RTSP walk (20 answers \"400 Bad Request\"), and on "
            "2026-10-07 ONVIF requests sent over HTTP to its RTSP port 554. In the 23 hours "
            "between, with no start-up scan and many live and classic tries, it stayed healthy.")]

    s += [h1("Changes"),
          table([["#", "Change"],
                 ["Known cameras", "Every scan pass skips a host where a saved camera has a working "
                  "stream (credentials, an unauthenticated probe, or status ready)."],
                 ["Removed cameras", "Remove keeps the card's details without its password in "
                  "removed_cameras.json; a scan that finds the address gives the card back "
                  "(needs a password) without probing; the copy is dropped once the camera is set "
                  "up again."],
                 ["ONVIF port", "_onvif_media_url never uses an RTSP or RTMP port; port 80 when no "
                  "XAddrs are saved."],
                 ["Cooldown", "The start-up RTSP check waits out the brand's cooldown."],
                 ["Quality Switch", "\"failed\" state: the toggle says the smoother stream gave no "
                  "picture."],
                 ["Correction", "AnyCam's probe already sends TEARDOWN after SETUP; the earlier "
                  "claim that it did not was wrong."]],
                [1.4 * inch, 5.3 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates."],
                 ["Python integration", verdict("PASS"), "666 of 666; section AO new (10 checks), "
                  "AO6 runs the real scan with stand-in network calls."],
                 ["JavaScript behaviour", verdict("PASS"), "193 of 193."],
                 ["Not run", verdict("NOTED"), "No field test; the Microseven needs a power cycle "
                  "first. Whether the scan was the only trigger is a strong pattern in two "
                  "events, not proof."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>. Rows this change "
            "touches; the rest carry from the 3.7.0 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["6.1 Security", "Secrets at rest", verdict("PASS"),
                  "The removed-cameras file holds no password and no password in any URL."],
                 ["1.10 Exceptions", "Corrupt file", verdict("PASS"),
                  "A bad removed_cameras.json is logged and ignored."],
                 ["1.12 Logging", "Truth", verdict("PASS"),
                  "\"known camera ... not probed\" and \"card given back ... not probed\" lines."],
                 ["1.18 Testing", "New tests", verdict("PASS"), "10 server checks."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "Made-up addresses only."]],
                W_BP)]

    s += [h1("Known issues"),
          *bullets(["<b>Microseven:</b> stuck until power-cycled; then the ffmpeg copy gets its test.",
                    "<b>B48:</b> go2rtc's local RTSP server does not ask local programs for a password.",
                    "<b>B33:</b> one Amcrest camera can get two cards."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_scan.py", "Known-camera skip, removed-camera restore, the cooldown."],
                 ["camera_discovery.py", "removed_cameras.json; Remove keeps the details; version."],
                 ["anycam_probe.py", "ONVIF never to an RTSP port."],
                 ["anycam_snap.py, page_script.py, anycam_page.py", "Quality Switch failure notice."],
                 ["tests/", "Section AO."],
                 ["CHANGELOG.md, config.yaml, docs", "3.7.5-rc2.0; build plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.7.5-rc2.0", story(), "anycam_3_7_5_rc2_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
