"""Audit report for AnyCam 3.7.4 — Microseven live view, stuck cameras, no hardware HEVC, go2rtc API password.

    python docs/audits/tools/mk_audit_3_7_4.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.7.4 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - Microseven live view, stuck cameras, no hardware HEVC, "
                   "go2rtc API password - tag 3.7.4" % DATE, S_SUB),
         p("Scope confirmed by CrystalHeeler on 2026-10-06 and 2026-10-07: version 3.7.4 with every "
           "open B item except B29 and B33: B47 (design A), B46, B6 and B32 as one feature, B37 option "
           "A. Added during the build, with the reason below: go2rtc's API password for local "
           "programs. Not field-tested; built, not pushed.")]

    s += [h1("Changes"),
          table([["#", "Cause (from the field tests)", "Change"],
                 ["B47", "Chrome's media log for the Microseven: \"coded size: [4,0]\" (H.265) and "
                  "\"Unrecognized video codec profile\" (H.264): go2rtc built an invalid MSE init "
                  "segment; ffmpeg read the same stream at 3840x2160 (go2rtc issues 2361, 2529).",
                  "go2rtc's ffmpeg module; the Hipcam/Microseven brand entry sets live_ffmpeg_copy: "
                  "source ffmpeg:&lt;rtsp&gt;#video=copy#audio=copy. Other cameras unchanged."],
                 ["API", "Checking design A: go2rtc skips its API password for 127.0.0.1, and AnyCam "
                  "uses the host network, so any program on the Pi could add an ffmpeg source (running "
                  "ffmpeg with its own arguments in this full_access add-on) or read every camera "
                  "password from /api/streams.", "api.username/password, new at each start, with "
                  "local_auth: true; AnyCam's sessions send it. Pinned in the release gate."],
                 ["B6, B32", "The stuck Microseven accepted connections and answered nothing; each "
                  "live and classic try failed in turn (about 50 s) and kept connecting.", "Stuck after "
                  "the classic view gives up on RTSP, or two live failures with a dropped connection "
                  "in 2 min: no RTSP, HTTP snapshots, a \"Power-cycle camera\" badge, a check every "
                  "5 min inside the cooldown."],
                 ["B37", "Green on every camera and size on both systems.", "hevc_drm is never picked "
                  "automatically; the picture test still runs it by hand."],
                 ["B46", "The Microseven: a WebRTC retry with no stream at all.", "Retry only when "
                  "the session reached MSE."]],
                [0.6 * inch, 3.05 * inch, 3.05 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; the go2rtc contract now pins the "
                  "module list with ffmpeg and the API password."],
                 ["Python integration", verdict("PASS"), "638 of 638; section AM new (21 checks); "
                  "two go2rtc config checks and AJ4 updated."],
                 ["JavaScript behaviour", verdict("PASS"), "191 of 191; B46 new."],
                 ["Not run", verdict("NOTED"), "No field test: the ffmpeg copy has not met the "
                  "Microseven; the stuck state and its check have not met a stuck camera; go2rtc's "
                  "local_auth ran only against a stand-in server."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>. Rows this change "
            "touches; the rest carry from the 3.7.0 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["6.1 Security", "Least privilege", verdict("PASS"),
                  "ffmpeg loaded only with the API password required from every caller; the "
                  "browser can still name only streams AnyCam registered."],
                 ["6.1 Security", "Secrets", verdict("PASS"),
                  "Camera passwords stay percent-encoded inside the ffmpeg source; logs strip them."],
                 ["1.3 Async", "Blocking calls", verdict("PASS"),
                  "The stuck check runs the socket validator in the thread pool."],
                 ["1.12 Logging", "Truth", verdict("PASS"),
                  "\"hevc_drm: not used\" with the reason; stuck and recovered lines."],
                 ["1.18 Testing", "New tests", verdict("PASS"), "21 server checks, 1 page check."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"),
                  "Made-up addresses; no device serials."]],
                W_BP)]

    s += [h1("Known issues"),
          *bullets(["<b>B48:</b> go2rtc's RTSP server does not ask local programs for its password; "
                    "other programs on the Pi can read camera video. Decision open.",
                    "<b>B6:</b> the day-long check with no new lock-out waits for the camera.",
                    "<b>B33:</b> one Amcrest camera can get two cards; under investigation."])]

    s += [h1("Acceptance (field test)"),
          *bullets(["Update in place (no uninstall): the Microseven plays live in Chrome and LibreWolf.",
                    "The start-up log says \"hevc_drm: not used\".",
                    "A Lorex channel in LibreWolf's classic view: no green start.",
                    "A stuck Microseven: \"Power-cycle camera\" badge and still pictures; live view "
                    "back within 5 minutes after a power cycle."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_go2rtc.py", "ffmpeg module, copy source, API password, stuck state."],
                 ["camera_db.py", "live_ffmpeg_copy on the Hipcam/Microseven entry."],
                 ["anycam_snap.py", "HTTP snapshots while stuck; marks stuck."],
                 ["anycam_focus.py", "No live view while stuck."],
                 ["camera_discovery.py", "HW_NOT_AUTOMATIC; rtsp_stuck in the camera list; 3.7.4."],
                 ["page_script.py", "B46; the \"Power-cycle camera\" badge."],
                 ["verify_release.py", "The go2rtc security contract."],
                 ["tests/", "Section AM; page check; updated config checks."],
                 ["CHANGELOG.md, config.yaml, docs", "3.7.4; build plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.7.4", story(), "anycam_3_7_4_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
