"""Audit report for AnyCam 3.2.0-rc1.0 — sound in live view, WebRTC and RTSP-over-WebSocket cameras.

    python docs/audits/tools/mk_audit_3_2_0_rc1_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.2.0-rc1.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - sound in live view, WebRTC and RTSP-over-WebSocket "
                   "cameras - tag 3.2.0-rc1.0" % DATE, S_SUB),
         p("Scope: phase 3.2.0 of the build plan (live view): C5 and C6, with CrystalHeeler's "
           "decisions of 2026-10-04: C5 muted by default with an unmute button; C6 built "
           "without further input. C3 was dropped on 2026-10-04. Built in the untested run "
           "on top of 3.1.0-rc1.0. This build is not field-tested.")]

    s += [h1("1. Sound in Enhanced View (C5)"),
          *bullets([
              "The live player asks for media 'video,audio'. VideoRTC tells go2rtc which "
              "audio codecs this browser plays (MediaSource.isTypeSupported for MSE; the "
              "WebRTC offer), so go2rtc sends sound only in a codec the browser plays. Sound "
              "the browser cannot play is left out; the video is not affected.",
              "The video starts muted: browsers block sound that starts on its own. A Sound "
              "button in the Enhanced View bar turns it on; a click is the gesture browsers "
              "need. If the browser still refuses, the video stays muted.",
              "The button is grey before the first frame and when the stream has no sound "
              "(no audio codec in the MSE codec list, no live audio track on WebRTC). It is "
              "redrawn only when its state changes, not every second.",
              "Cards stay video only: no sound from a grid of cards, and less data."])]

    s += [h1("2. WebRTC and RTSP-over-WebSocket cameras (C6)"),
          p("Before 3.2.0 both had an information card. go2rtc 1.9.14 plays both with the "
            "modules AnyCam already loads (api, ws, rtsp, webrtc, mp4), so the go2rtc security "
            "model is unchanged. Source formats from go2rtc's README for v1.9.14."),
          table([["Camera", "go2rtc source"],
                 ["WebRTC (WHEP: an HTTP address that takes an SDP offer, which probe_webrtc "
                  "finds)", "webrtc:http://user:pass@ip:port/path"],
                 ["RTSP over WebSocket", "rtsp://user:pass@ip:554/<brand's first stream "
                  "path, else />#transport=ws://ip:port/path"]],
                [3.2 * inch, 3.5 * inch]),
          p("Both play in the card and in Enhanced View (profile 0 only). These cameras have "
            "no still pictures, so a card whose live view fails shows the reason instead of "
            "polling snapshots.")]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 15 files; build inputs "
                  "checked online."],
                 ["Python integration", verdict("PASS"), "503 of 503; 9 new in section AC."],
                 ["JavaScript behaviour", verdict("PASS"), "139 of 139; new section C5; the "
                  "mount check now expects video and sound."],
                 ["Undefined names", verdict("PASS"), "15 files, 0."],
                 ["Not run", verdict("NOTED"), "No field test (CrystalHeeler's order). The "
                  "image was not built. No test system has a camera known to send sound, "
                  "a WebRTC camera, or an RTSP-over-WebSocket "
                  "camera; go2rtc was a stand-in in the tests."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, Parts 1 to 6. "
            "Rows this change touches; the rest carry from the 3.1.0-rc1.0 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST, contracts, undefined names",
                  verdict("PASS"), "15 files."],
                 ["1.18 Testing", "New tests", verdict("PASS"), "9 server and 7 page checks."],
                 ["2.x JavaScript", "Promises, DOM", verdict("PASS"),
                  "play() refusal caught; the button is redrawn only on a change."],
                 ["6.1 Security", "go2rtc modules", verdict("PASS"),
                  "No module added; the passwords go into the source URL, percent-encoded, "
                  "never to the page."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"),
                  "Tests use made-up addresses only."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets([
              "Cards play no sound.",
              "An RTSP-over-WebSocket camera of an unknown brand gets the root stream path.",
              "<b>B6:</b> the Microseven waits until CrystalHeeler unlocks it.",
              "<b>C21:</b> the classic engine stays as the fallback."])]

    s += [h1("Acceptance (field test)"),
          *bullets([
              "Enhanced View on a camera with a microphone: the Sound button turns the sound "
              "on and off; on a camera without one, the button is grey.",
              "Chrome and Firefox on Windows both play the video as in 3.1.0-rc1.0.",
              "A WebRTC or RTSP-over-WebSocket camera, when one is available, plays in its "
              "card and in Enhanced View."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_go2rtc.py", "_go2rtc_native_source; card and Enhanced View use it."],
                 ["page_script.py", "Sound button; video and sound; WebRTC and WS-RTSP cards."],
                 ["camera_discovery.py, config.yaml", "Version 3.2.0-rc1.0."],
                 ["tests/", "Section AC; page section C5."],
                 ["CHANGELOG.md, docs", "3.2.0-rc1.0; build plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.2.0-rc1.0", story(), "anycam_3_2_0_rc1_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
