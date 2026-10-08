"""Audit report for AnyCam 3.7.5-rc1.0 — the ffmpeg copy for any camera, the Quality Switch.

    python docs/audits/tools/mk_audit_3_7_5_rc1_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.7.5-rc1.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - the ffmpeg copy for any camera, the Quality Switch "
                   "- tag 3.7.5-rc1.0" % DATE, S_SUB),
         p("Scope confirmed by CrystalHeeler on 2026-10-07: B49 (option A2), B50, B51 with a "
           "Quality Switch, as a release candidate in case the Microseven still fails. "
           "CrystalHeeler also asked that fixes work for other camera systems, not only these, "
           "so B47's ffmpeg copy and B51's sub-stream are found generally. Not field-tested.")]

    s += [h1("Changes"),
          table([["#", "Cause (from the 3.7.4 field test)", "Change"],
                 ["B49", "Every Microseven stream: \"unsupported scheme: exec:ffmpeg ...\". go2rtc "
                  "runs an ffmpeg: source through its exec module, which AnyCam did not load.",
                  "exec loaded, behind the API password required from every caller (3.7.4)."],
                 ["B47", "The copy was tied to one brand entry; another brand with the same "
                  "flaw would still fail.", "The page sends the browser's media error; a stream "
                  "description error switches that camera to the copy, saved, and live view "
                  "retries once."],
                 ["B50", "After go2rtc's failure, the Microseven was marked stuck (20:07:37).",
                  "No stuck mark while go2rtc failed for the camera; reasons naming go2rtc never "
                  "count."],
                 ["B51", "LibreWolf, Lorex channel 7 at 3840x2160: about 4.5 decoded pictures a "
                  "second of 7; grey pictures every 5 to 10 s.", "Keyframes only at full size "
                  "(rule for 4K H.265 in software, and a measured switch below 80% of the "
                  "stream's rate); a Quality Switch to the sub-stream when one is found."]],
                [0.6 * inch, 3.05 * inch, 3.05 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; the module list now pins exec."],
                 ["Python integration", verdict("PASS"), "656 of 656; section AN new (18 checks)."],
                 ["JavaScript behaviour", verdict("PASS"), "193 of 193; the repair flow new."],
                 ["Not run", verdict("NOTED"), "No field test: go2rtc's exec and ffmpeg sources ran "
                  "only against a stand-in; the browser error texts are Chrome's from the 2026-10-06 "
                  "media log, Firefox's are unknown; the sub-stream rules were not tried on cameras."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>. Rows this change "
            "touches; the rest carry from the 3.7.0 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["6.1 Security", "Least privilege", verdict("NOTED"),
                  "exec can run any command as a stream source; only callers with the API "
                  "password (AnyCam) can add sources. Accepted by CrystalHeeler (A2)."],
                 ["6.1 Security", "Input at the boundary", verdict("PASS"),
                  "/api/live_repair accepts a known camera and a description error only; "
                  "/quality_switch refuses a camera with no sub-stream."],
                 ["1.12 Logging", "Truth", verdict("PASS"),
                  "Repair, keyframes-only and sub-stream decisions are logged with reasons."],
                 ["1.18 Testing", "New tests", verdict("PASS"), "18 server and 2 page checks."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "Made-up addresses only."]],
                W_BP)]

    s += [h1("Known issues"),
          *bullets(["<b>B48:</b> go2rtc's local RTSP server does not ask local programs for a "
                    "password. Decision open.",
                    "<b>B33:</b> one Amcrest camera can get two cards; under investigation."])]

    s += [h1("Acceptance (field test)"),
          *bullets(["The Microseven plays live in Chrome and LibreWolf.",
                    "LibreWolf, Lorex channel 7, classic view: sharp full-size pictures, no grey, "
                    "\"keyframes only\" in the info bar; the Quality Switch appears and switches to "
                    "the sub-stream.",
                    "A camera whose stream description the browser rejects is repaired once, logged."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_go2rtc.py", "exec module; /api/live_repair; _classic_sub_stream; B50."],
                 ["anycam_snap.py", "Keyframes only, the rate check, the sub-stream mode, headers."],
                 ["anycam_focus.py", "/quality_switch; per-session resets."],
                 ["page_script.py", "Media error reports, the repair retry, the Quality Switch."],
                 ["anycam_page.py", "The switch's style."],
                 ["camera_discovery.py", "Routes; quality_switch in the camera list; version."],
                 ["verify_release.py", "Module list with exec."],
                 ["tests/", "Section AN; page checks; updated config checks."],
                 ["CHANGELOG.md, config.yaml, docs", "3.7.5-rc1.0; build plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.7.5-rc1.0", story(), "anycam_3_7_5_rc1_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
