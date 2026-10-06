"""Audit report for AnyCam 3.7.1 — short absences, no two-way audio requests, decoder report, network.

    python docs/audits/tools/mk_audit_3_7_1.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.7.1 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - short absences, two-way audio, decoder report, network "
                   "- tag 3.7.1" % DATE, S_SUB),
         p("Scope confirmed by CrystalHeeler on 2026-10-06: version 3.7.1 with B27 (short "
           "absences only, 60 s), B31 (#backchannel=0 for every camera), B34, and the false "
           "overlay warning of B11. B33 was taken out on CrystalHeeler's order, to be "
           "investigated first. Not field-tested.")]

    s += [h1("Changes"),
          table([["#", "Cause (from the logs)", "Change"],
                 ["B27", "The live players close their streams 5 s after the page is hidden, "
                  "and a return waits for a new connection and keyframe: about 2 s of black "
                  "(CrystalHeeler: black, Chrome, seen since 3.7.0-rc1.0).",
                  "While the page is hidden, a player keeps its stream for 60 s "
                  "(LIVE_HIDDEN_GRACE_MS), in the cards, the MJPEG cards and Enhanced View. Off "
                  "screen and Enhanced View's pause of the cards keep 5 s."],
                 ["B31", "Two older Hikvision cameras on test system C reset go2rtc's "
                  "connection about once a second (about 900 resets) while AnyCam's probes and "
                  "ffprobe worked. go2rtc requests the ONVIF backchannel unless told not to; "
                  "its README: #backchannel=0 is \"important for some glitchy cameras\".",
                  "_go2rtc_rtsp_src adds #backchannel=0 to every RTSP source given to go2rtc: "
                  "cards, Enhanced View, the RTSP-over-WebSocket source and the relay for "
                  "AnyCam's ffmpeg. A direct address to ffmpeg never carries it."],
                 ["B34", "On test system C (3.0.0) `ip route` timed out; AnyCam scanned a "
                  "fixed 192.168.1.0/24.", "Retry at 15 s, then the add-on's own address "
                  "with a /24, logged; an unknown network stops the scan with a message."],
                 ["B11", "3.7.0's report looked for the name rpivid; newer kernels say "
                  "rpi-hevc-dec. With Protection mode off (test system B, 2026-10-06) all 19 "
                  "devices open.", "rpi-hevc-dec counts as the HEVC decoder; a blocked device "
                  "note names Protection mode; hevc_drm is unavailable when /dev/video19 or "
                  "/dev/media0 does not open."]],
                [0.5 * inch, 3.1 * inch, 3.1 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 17 files; build inputs checked "
                  "online."],
                 ["Python integration", verdict("PASS"), "581 of 581; 9 new in section AJ; 11 "
                  "source checks now expect #backchannel=0."],
                 ["JavaScript behaviour", verdict("PASS"), "166 of 166."],
                 ["Not run", verdict("NOTED"), "No field test. The B31 cause is the likely one, "
                  "from go2rtc's documentation, not yet confirmed on the cameras. The 60 s timer "
                  "was not watched in a real browser."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>. Rows this change "
            "touches; the rest carry from the 3.7.0 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.10 Exceptions", "Failure paths", verdict("PASS"),
                  "No guessed network; a clear scan message."],
                 ["1.12 Logging", "Truth", verdict("PASS"),
                  "No false overlay warning; the blocked-device note names the fix."],
                 ["1.18 Testing", "New tests", verdict("PASS"), "9 server checks."],
                 ["6.1 Security", "Requests to cameras", verdict("PASS"),
                  "AnyCam no longer asks cameras for a two-way audio channel it never used."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "Made-up addresses only."]],
                W_BP)]

    s += [h1("Known issues"),
          *bullets(["<b>B11:</b> hardware decode needs Protection mode off; whether to declare "
                    "the devices instead is open.",
                    "<b>B33:</b> one Amcrest camera can get two cards; under investigation.",
                    "<b>B32:</b> the Microseven waits about 30 s on \"Loading feed\".",
                    "<b>B29, B30:</b> sound, and an empty user name, wait for logs."])]

    s += [h1("Acceptance (field test)"),
          *bullets(["Switch to another program for 10 s and back: the cards are live at once.",
                    "Test system C: the two older Hikvision cameras play live and in the "
                    "classic view; the go2rtc \"connection reset by peer\" lines stop.",
                    "With Protection mode off: the start-up report has no overlay warning, and "
                    "the classic view logs \"hw first frame\"."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_go2rtc.py", "_go2rtc_rtsp_src and its uses."],
                 ["anycam_scan.py", "get_local_subnet, _own_ipv4; the scan stops on no network."],
                 ["camera_discovery.py", "The decoder report and the hevc_drm check; version "
                  "3.7.1."],
                 ["page_script.py", "The 60 s grace for hidden pages."],
                 ["tests/", "Section AJ; updated source checks."],
                 ["CHANGELOG.md, config.yaml, docs", "3.7.1; build plan (header and phases "
                  "rewritten)."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.7.1", story(), "anycam_3_7_1_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
