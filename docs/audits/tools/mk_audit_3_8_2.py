"""Audit report for AnyCam 3.8.2 — B65 regression fix, B66, B67, B64 first step, B62 report.

    python docs/audits/tools/mk_audit_3_8_2.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.8.2 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - B65, B66, B67, B64 (first step), B62 report - tag 3.8.2" % DATE, S_SUB),
         p("A fix release for a 3.8.1 regression found in CrystalHeeler's log of 2026-10-16, with the "
           "scope CrystalHeeler confirmed on 2026-10-19: B65 (the regression), a fake-go2rtc check, a "
           "quieter pre-warm log, B66 (the log link in the HA app), the first step of B64, B67 (view "
           "cleanup, with the new rule 10) and the B62 WebRTC report.")]

    s += [h1("B65: the 3.8.1 regression"),
          p("3.8.1 sent the connection gate to go2rtc as an exec: source through go2rtc's API. go2rtc "
            "1.9.14 refuses an API source whose scheme is exec, echo or expr, or that holds a space "
            "(internal/streams/handlers.go, Validate), so every registration of the Microseven's streams "
            "failed (\"source from insecure producer\", 596 lines in one log). The Microseven lost go2rtc: "
            "its card showed pictures from AnyCam's own ffmpeg, Enhanced View used the classic engine, "
            "and the card's ffmpeg opened the camera directly. The tests missed it because the fake "
            "go2rtc accepted every source."),
          table([["Part", "3.8.2"],
                 ["Source", "Rate-limited cameras get go2rtc's own ffmpeg: copy source again (accepted by "
                  "the API). AnyCam records each one's cooldown in the gate's record."],
                 ["Gate", "AnyCam's inline go2rtc config sets go2rtc's ffmpeg program (ffmpeg.bin) to "
                  "anycam_gate.py, made executable in the Dockerfile. For an -i address with a cooldown "
                  "on record it takes the camera's turn, then os.execvp's the real ffmpeg with the same "
                  "arguments; every other run, including ffmpeg -version, passes straight through; a "
                  "broken record never stops ffmpeg."],
                 ["No squeezed turns", "go2rtc gives such a command 30 s to start. A turn further away "
                  "than 25 s is not taken: the gate exits without opening the camera and go2rtc retries "
                  "later. The 3.8.1 code capped the wait instead, which could give two queued openers the "
                  "same moment under a 30 s escalation; found while testing, fixed here."],
                 ["Tests", "The fake go2rtc now applies Validate; AR1 shows 3.8.1's source refused and "
                  "3.8.2's accepted."]],
                [1.4 * inch, 5.2 * inch])]

    s += [h1("The other items"),
          table([["Item", "Change"],
                 ["Quieter log", "A pre-warm stream go2rtc refuses is logged once (and once when it "
                  "starts), not every 30 s; the card's own registration still warns at once."],
                 ["B66", "The log link navigates inside the HA frontend as its navigate() does: pushState "
                  "on the HA window with the page it came from, then a location-changed event. A full "
                  "page load stays as the fallback. Inside the HA app the old full load opened the "
                  "phone's browser."],
                 ["B64, first step", "12 prefixes registered to Ubiquiti and TP-Link left the non-camera "
                  "list, which B55 uses to skip hosts unprobed."],
                 ["B67, rule 10", "Dialogs: height capped to the screen with their own scrolling, width "
                  "capped in vw, buttons sticky at the bottom. The Enhanced View bar: min-height, wraps, "
                  "safe-area padding, the camera name on its own row and the short Quality Switch line on "
                  "phones. The toast: max-width. The release gate checks these styles (R10) and names "
                  "every fixed overlay; it fails on 3.8.1's styles (dialog scroll, bar, toast)."],
                 ["B62 report", "The live player reads WebRTC's statistics and reports the outcome, the "
                  "candidates each side offered, the pair checks and the route used, once per camera, "
                  "place and outcome, to POST /api/live_diag, which checks every field and logs one "
                  "line (LIVE-DIAG)."]],
                [1.4 * inch, 5.2 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates, with the new R10 check and the contracts "
                  "moved to the go2rtc source, the go2rtc config and the gate."],
                 ["Python integration", verdict("PASS"), "729 of 729. New section AR; AQ and AM1 updated for "
                  "the 3.8.2 gate."],
                 ["JavaScript behaviour", verdict("PASS"), "201 of 201: B66 navigation and fallback, the B62 "
                  "summary."],
                 ["Visual (rule 10)", verdict("PASS"), "The built page in the browser pane at 375x812, "
                  "812x375 and 1366x768, the settings dialog with eight zones and a long title, the bar "
                  "with a long label, Sound and the Quality Switch: every box inside the screen, the "
                  "dialog scrolls, its buttons visible. Measured from the page; screenshots at phone size."],
                 ["Field", verdict("NOTED"), "Not run yet; see the next section."]],
                [1.5 * inch, 0.7 * inch, 4.4 * inch])]

    s += [h1("Not tested"),
          *bullets(["<b>go2rtc running the gate as its ffmpeg on the Pi.</b> No Linux system or go2rtc "
                    "here. The command line is checked against go2rtc's own parsing (the ffmpeg: redirect, "
                    "QuoteSplit) and the API's Validate, read from go2rtc's source, not run.",
                    "<b>The file lock and the executable bit.</b> Linux only; set by the Dockerfile.",
                    "<b>B66 inside the HA app.</b> The navigation is HA's own method, read from the "
                    "frontend's source; the app itself was not available here.",
                    "<b>The B62 report on a phone.</b> Its first field log is the test.",
                    "<b>An intermittent test.</b> Q2 (the home-location re-check timer, untouched by this "
                    "build) failed once in one run and passed in the next three; a timing check, to be "
                    "looked at separately."])]

    s += [h1("Best-practices compliance"),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["8 (regressions)", "Our code first", verdict("PASS"),
                  "B65 was AnyCam's; fixed, and the test fake now matches go2rtc."],
                 ["6 Security", "Validate at the boundary", verdict("PASS"),
                  "/api/live_diag checks camera, place and shape, and cuts every field."],
                 ["1.10 Exceptions", "Narrow excepts", verdict("PASS"),
                  "The gate catches OSError, ValueError, TypeError only, then runs ffmpeg."],
                 ["Rule 10", "Overlays fit", verdict("PASS"), "New rule; gate R10 and a visual check."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "Made-up addresses only."]],
                W_BP)]

    s += [h1("Known issues"),
          *bullets(["<b>Phone live view (B62):</b> where WebRTC does not connect, MSE can fall behind; "
                    "the new report will show why.",
                    "<b>Extra connections:</b> each camera holds one or two all the time; the prewarm "
                    "setting turns them off."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_gate.py", "Runs as go2rtc's ffmpeg; set_cooldown; no squeezed turns; shebang."],
                 ["anycam_go2rtc.py", "ffmpeg: source for gated cameras, cooldown record, ffmpeg.bin, quiet "
                  "registration, pre-warm failure logged once, /api/live_diag."],
                 ["anycam_brand.py", "12 prefixes off the non-camera list."],
                 ["page_script.py", "B66 navigation, B62 report."],
                 ["anycam_page.py", "B67 styles."],
                 ["camera_discovery.py, Dockerfile", "Route for /api/live_diag; chmod for the gate; 3.8.2."],
                 ["verify_release.py", "R10 check; contracts moved."],
                 ["tests/", "Fake go2rtc Validate; sections AQ (updated) and AR; page tests."],
                 ["CHANGELOG.md, config.yaml, CLAUDE.md, docs", "3.8.2; rule 10; build plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.8.2", story(), "anycam_3_8_2_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
