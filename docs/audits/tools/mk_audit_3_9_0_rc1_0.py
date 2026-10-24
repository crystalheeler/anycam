"""Audit report for AnyCam 3.9.0-rc1.0 — B68, B69 and the 14 Ready tickets of 2026-10-24.

    python docs/audits/tools/mk_audit_3_9_0_rc1_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.9.0-rc1.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - 16 items - tag 3.9.0-rc1.0 (local)" % DATE, S_SUB),
         p("A release candidate with the scope CrystalHeeler confirmed on 2026-10-24: B68 and B69 from 3.8.4 "
           "(never published on its own) and every Ready ticket: B79, B72, B70, B74, B71, B73, B81, B78, B80, "
           "C31, B58, B63, B62 and C30. The recording work follows the review of CrystalHeeler's recordings "
           "(docs/Recording_Review_2026-10-24.md).")]

    s += [h1("C30: the headlight pause"),
          table([["Part", "3.9.0-rc1.0"],
                 ["Start", "One comparison (each picture against the one 1 s older): 0.5% or more of the picture "
                  "changed, 90% or more of the changed cells brighter by +25 grey levels or more on average, "
                  "the changed cells at most 35% of the box around them (a lit lawn is scattered, a person is "
                  "solid), and the 1.5 s before quiet (nothing over 1.5% unless it was itself a scattered "
                  "brightening)."],
                 ["During", "No recording starts; the slow-movement check drops its older pictures; the "
                  "calibration log gives the reason \"a passing light\"."],
                 ["End", "Two quiet comparisons (under 0.3%), or 4 s; a darker-only change within 3 s after "
                  "continues it, at most 6 s in all; the camera turning from infrared to colour ends it (a light "
                  "switched on: the porch light)."],
                 ["Pre-roll", "A recording that starts within 1.5 s after a pause starts at the pause's beginning "
                  "less the 3 s pre-roll; the recording buffer keeps 10 s instead of 3 s (about 10 MB at "
                  "8 Mbit/s per armed camera)."],
                 ["Evidence", "The add-on's own functions replayed on 334 recordings of 2026-10-20 to 24: 73 of "
                  "113 night recordings paused at their trigger (headlights, cars on the street, the "
                  "day-to-night switch); by day 4 of 221 (a dusk headlight, a cloud, two porch lights ended by "
                  "the colour switch). The cat, the man, the boy, the insect and the IR-switch clip start no "
                  "pause."],
                 ["Changed during the build", "The test suite's bright solid block (a person) started a pause; "
                  "the 35% box limit was added (checked on the 334 recordings: 2 small, solid street lights "
                  "lost, 71 kept). A solid bright shape no longer counts as a light growing."]],
                [1.3 * inch, 5.3 * inch])]

    s += [h1("The other items"),
          table([["Item", "Change"],
                 ["B79", "Start-up keeps every runtime.json entry; only the version changes (the card order "
                  "was dropped at every start)."],
                 ["B70", "Per-check zone, picture and slow-movement lines: DEBUG, at a movement's start, then at "
                  "most every 5 s; the once-a-minute zone line at INFO only when a zone saw movement. DEBUG alone "
                  "was not enough: log_debug is on by default."],
                 ["B72", "Two decimals when peak and threshold round alike; a WebRTC set-up close is DEBUG "
                  "(\"set up; WebRTC plays on\"); a pre-warm failure names its error type."],
                 ["B71", "LIVE-DIAG: global addresses become public:<port> (Python ipaddress is_global); "
                  "private, VPN and link-local stay. A bug found by the test: addresses followed by a comma "
                  "were not masked; fixed before release."],
                 ["B73, B78", "The slow-movement check drops its references at a light change, and never uses "
                  "a picture within 0.6 s of one the fast checks rejected as an insect."],
                 ["B74", "A slow-movement start gets its calibration line: \"recorded (slow movement, over "
                  "5 s) → file\"."],
                 ["C31", "Every zone that passed during a recording, in order, in recording_zones.json, the "
                  "Storage tab (ellipsis, full list on hover) and the \"recording stopped\" line."],
                 ["B80", "After 3 failures through go2rtc: direct, then go2rtc's copy again once the pre-warm "
                  "pass saw new bytes, waiting 1, 2, 4, then 10 min; a running picture loop switches back."],
                 ["B58", "When main and sub differ in one number and the brand table lists the next path, "
                  "password entry checks it with one paced request."],
                 ["B63", "The brand matcher reads the RTSP server name; the scan's ONVIF request and RTSP check "
                  "take turns 5 s apart."],
                 ["B81, B62", "A card player does not connect while Enhanced View is open (VideoRTC's own "
                  "restarts included); 20 s after playing, one LIVE-PLAY report per camera and place every "
                  "5 min (WebRTC inbound-rtp numbers, or MSE quality and lag)."],
                 ["B68, B69", "From 3.8.4: dialog parts keep their height; a drag out of a dialog does not "
                  "close it."]],
                [1.1 * inch, 5.5 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 58 contracts, new ones for C30, B71 and B79, "
                  "the pre-roll contract updated."],
                 ["Python integration", verdict("PASS"), "798 of 798; new section AU (25 checks); AE3 and M1 "
                  "updated for B70 and the 10 s buffer."],
                 ["JavaScript behaviour", verdict("PASS"), "217 of 217; B81, B62 and C31 checks."],
                 ["Recording replay", verdict("PASS"), "C30 through the add-on's code on 334 recordings."],
                 ["Visual (rule 10)", verdict("PASS"), "The Storage tab's zone list at 1366x768 and 812x375: "
                  "inside the name column, ellipsis, full list on hover. At 375x812 the name column is 0 px, "
                  "the known C23 fault."],
                 ["Field", verdict("NOTED"), "Not run yet: an rc for field testing."]],
                [1.5 * inch, 0.7 * inch, 4.4 * inch])]

    s += [h1("Not tested"),
          *bullets(["<b>C30 live.</b> The replay used recorded files (OpenCV decoding, 7 pictures a second); "
                    "the add-on decodes the live sub-stream with ffmpeg. A night of field use is the test.",
                    "<b>B80's switch back</b> on a real fall-back; B58 on a camera with a third stream; B63's "
                    "5 s spacing on the Microseven's scan.",
                    "<b>B81 and the LIVE-PLAY report</b> in the HA app on the phone.",
                    "<b>The 10 s buffer's memory</b> on the Pi with several armed cameras."])]

    s += [h1("Best-practices compliance"),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["Rule 2", "Logs before code", verdict("PASS"), "Every item came from field logs or videos."],
                 ["6 Security", "Validate at the boundary", verdict("PASS"),
                  "LIVE-PLAY takes numbers only; public addresses masked."],
                 ["P4 Async", "No blocking I/O on the loop", verdict("PASS"),
                  "recording_zones.json written in a thread."],
                 ["Rule 10", "Overlays fit", verdict("PASS"), "Zone label capped with an ellipsis."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "Made-up and documentation addresses in tests."]],
                W_BP)]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_motion.py", "C30, B70, B72, B73, B74, B78, C31; the 10 s buffer."],
                 ["anycam_go2rtc.py", "B71, B72, B80, the LIVE-PLAY line."],
                 ["anycam_snap.py", "B80's switch back."],
                 ["anycam_credentials.py, anycam_brand.py, anycam_scan.py", "B58, B63."],
                 ["camera_discovery.py", "B79; 3.9.0-rc1.0."],
                 ["page_script.py, anycam_page.py", "B81, B62, C31."],
                 ["verify_release.py, tests/", "Contracts; section AU, page checks."],
                 ["CHANGELOG.md, config.yaml, docs", "3.9.0-rc1.0 (3.8.4 folded in); build plan; recording "
                  "review."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.9.0-rc1.0", story(), "anycam_3_9_0_rc1_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
