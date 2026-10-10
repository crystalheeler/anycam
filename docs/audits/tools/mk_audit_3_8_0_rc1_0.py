"""Audit report for AnyCam 3.8.0-rc1.0 — B54, a smaller, gentler scan.

    python docs/audits/tools/mk_audit_3_8_0_rc1_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.8.0-rc1.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - B54 - tag 3.8.0-rc1.0" % DATE, S_SUB),
         p("Scope confirmed by CrystalHeeler on 2026-10-10: B54 only, in a new minor version. "
           "The design was agreed point by point on 2026-10-09 and drawn in "
           "docs/B54_Flow_Comparison.html (right side). Four research notes support it: "
           "docs/B54-Port-Probe-Research.md and docs/B54-Followup-Research.md. Not field-tested.")]

    s += [h1("What B54 changes"),
          table([["Area", "Before", "Now"],
                 ["Scan, ONVIF", "ONVIF stream paths read only at password entry; the ONVIF "
                  "pass fetched a web page, an RTSP OPTIONS and a no-login walk on 554",
                  "A device that answered WS-Discovery gets one GetProfiles with no login. A "
                  "login prompt gives a \"needs password\" card and no further probing; profiles "
                  "give a ready card when a stream plays without a login."],
                 ["Scan, RTSP walk", "Brand paths, then the universal list, in one pass; the "
                  "walk stopped only after 5 same-realm 401s (which the Microseven never sent)",
                  "Brand paths first; the first 401 stops the walk (a password is needed); the "
                  "full list only for a camera with no ONVIF and no brand match."],
                 ["Scan, other ports", "Each extra web port drew 17 MJPEG, 11 HLS, 9 WebRTC and "
                  "7 WebSocket requests", "Once ONVIF or a brand path has identified the camera, "
                  "its other ports are not probed."],
                 ["Scan, Layer 2", "A multi-socket walk ran when Layer 1 heard RTSP but found "
                  "nothing", "No Layer 2 at scan time (no documented camera needs it); Layer 1 "
                  "reopens one connection on \"Connection: close\", reads replies strictly, and "
                  "sends a User-Agent."],
                 ["Password entry", "ONVIF, then always a brand-path pass after the walk",
                  "The 0/1/2 rule: 2 streams from ONVIF stop; 1 checks the brand paths; 0 runs "
                  "the full walk. Each walk stops at 2 streams."],
                 ["Login attempts", "Every 401 path got a login attempt (a typo could lock a "
                  "camera out)", "The walk stops at the first rejected login; after one success "
                  "a later 401 skips that path, at most 2. The card says \"Password rejected or "
                  "camera locked\"."],
                 ["DVR channels", "Each channel's main stream only", "Each channel, and the "
                  "parent, also gets its sub-stream checked."],
                 ["Deep Re-Probe", "Resumed an early-stopped walk, sometimes Layer 2",
                  "One full walk of every path, Layers 1 and 2 (also on skip_layer2 brands)."],
                 ["Brand table", "Hikvision NVR lacked skip_layer2 its comment claimed; the Axis "
                  "retry hung on an unreachable throttle_type",
                  "Hikvision NVR gets skip_layer2; the Axis retry is its own field "
                  "(rtsp_query_retry) and runs."]],
                [1.1 * inch, 2.9 * inch, 2.7 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates."],
                 ["Python integration", verdict("PASS"), "673 of 673; new section AP (7 checks) "
                  "drives the walker against a fake RTSP server: scan 401-stop, the 0/1/2 rule, "
                  "the login rule, and the Connection: close reopen. Sections U and Z updated."],
                 ["JavaScript behaviour", verdict("PASS"), "193 of 193."],
                 ["Not run", verdict("NOTED"), "No field test. The new scan is untested against a "
                  "real camera; the Microseven path is untested on an unlocked Microseven camera."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>. Rows this change touches."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.12 Logging", "Truth", verdict("PASS"),
                  "Each stop logs its reason: \"password needed\", \"identified on port N\", "
                  "\"login rejected\"."],
                 ["6.1 Security", "No secrets at rest", verdict("PASS"),
                  "No password in a logged URL; the lock hint request sends no login."],
                 ["1.18 Testing", "New tests first", verdict("PASS"),
                  "Section AP added with the walker rewrite; U and Z updated in the same change."],
                 ["1.2 Runtime errors", "Socket handling", verdict("PASS"),
                  "The reopen caps at 3; the reply reader keeps leftover bytes apart."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "Made-up addresses only."]],
                W_BP)]

    s += [h1("Known issues"),
          *bullets(["<b>Microseven:</b> the new scan and the ffmpeg copy are untested on an "
                    "unlocked Microseven camera.",
                    "<b>B48:</b> go2rtc's local RTSP server does not ask local programs for a password.",
                    "<b>B33:</b> one Amcrest camera can get two cards.",
                    "<b>Later (B55–B59):</b> the MAC-first check and printer rule, Reolink FLV / "
                    "Synology / rpos, the ONVIF NVR split, the brand-table third stream, and the "
                    "Reolink hub timeout."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_probe.py", "The walker: scan 401-stop, the login rule, want_streams, "
                  "the Connection: close reopen, a strict reply reader, a User-Agent; "
                  "find_rtsp_path: scan/deep/want_streams/path_set, no Layer 2 at scan time, "
                  "rtsp_query_retry; onvif_probe_no_login."],
                 ["anycam_scan.py", "ONVIF first with no login; stop probing a host's other ports "
                  "once identified; the ONVIF pass uses the no-login GetProfiles."],
                 ["anycam_credentials.py", "The 0/1/2 rule; the sub-stream from the walk; the "
                  "login-rejected message and the lock hint; DVR sub-streams per channel; Deep "
                  "Re-Probe is one full walk."],
                 ["camera_db.py", "Hikvision NVR skip_layer2; Axis rtsp_query_retry."],
                 ["verify_release.py", "find_rtsp_path contract updated for B54."],
                 ["tests/", "Section AP; sections U and Z updated."],
                 ["CHANGELOG.md, config.yaml, docs", "3.8.0-rc1.0; build plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.8.0-rc1.0", story(), "anycam_3_8_0_rc1_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
