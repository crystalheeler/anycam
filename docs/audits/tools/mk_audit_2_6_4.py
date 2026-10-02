"""Audit report for AnyCam 2.6.4, in the shared 2.6.x format (audit_lib.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (BP_INTRO, BP_SCOPE, DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, h2, mono, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph


def story():
    s = [Paragraph("AnyCam 2.6.4 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - live view always on - tag 2.6.4" % DATE, S_SUB),
         p("2.6.4 makes live view the default and only behaviour for Enhanced View "
           "and removes the Live View option. It is a small change: five files, "
           "75 lines added and 47 removed, most of it changelog. It completes "
           "build plan item A3.")]

    s += [h1("What 2.6.3 confirmed working"),
          p("Field evidence from CrystalHeeler on both Raspberry Pi 4 / HAOS systems."),
          table([["Component", "Evidence"],
                 ["Live H.265, Lorex DVR", "2026-09-28: 3840x2160 channels played smoothly "
                  "in Chrome on the test system B."],
                 ["Live H.265, Hikvision", "2026-09-29: 2.6.3 installed on the test system A; "
                  "the 2560x1440 stream plays live and looks good."],
                 ["Home Assistant ingress", "2026-09-29: a live feed stayed up for more than "
                  "10 minutes. This was the largest unknown in the 2.6.3 audit: ingress "
                  "cut multipart streams after 10 to 24 frames in 1.6.0."],
                 ["Freezes", "2026-09-29: none in live view."],
                 ["Root cause", "2026-09-29: in Chrome, on the same camera, Classic bogs "
                  "down while Live is smooth. The Pi's decode-and-JPEG pipeline was the "
                  "cause of the poor feeds, not the browser."],
                 ["Fallback", "2026-09-28: Firefox fell back to classic on H.265 within "
                  "1 s, as designed. go2rtc's table lists desktop Firefox as H.264-only."]],
                [1.6 * inch, 5.1 * inch])]

    s += [h1("Change"),
          h2("Live view is always on"),
          p("go2rtc now starts with the add-on on every install. The option, its "
            "environment variable and its checks are removed from the server and the "
            "page. Enhanced View always tries live view first."),
          p("Nothing is lost by removing the option. Its off setting sent every camera "
            "to the classic view. Each camera still falls back to the classic view on "
            "its own when go2rtc is not running, the browser cannot play the codec, or "
            "no video arrives within 12 s. The Classic button still switches views for "
            "a session."),
          h2("The trap this release had to avoid"),
          p("run.sh passes each setting to the program with bashio::config. For a "
            "setting that does not exist, bashio returns the text null (verified in "
            "bashio's lib/config.sh). Removing the option from config.yaml alone would "
            "have left run.sh exporting null, which the old code read as off: live view "
            "would have been silently disabled by the release meant to enable it. "
            "config.yaml, run.sh and the translation file change together, and a check "
            "confirmed every setting run.sh reads exists in config.yaml and the other "
            "way round."),
          h2("Upgrading a system that had the option saved"),
          p("Home Assistant keeps saved options until they are next saved. Supervisor "
            "source (supervisor/apps/options.py) logs a saved setting that is missing "
            "from the schema as a warning and drops it, then starts the add-on:"),
          mono("Option 'go2rtc_live_view' does not exist in the schema for AnyCam"),
          p("Harmless, and documented in the changelog so it is not mistaken for a "
            "fault.")]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "All five gates; 24 function "
                  "contracts; go2rtc API host check."],
                 ["Python integration", verdict("PASS"), "81 of 81, up from 79: three new "
                  "checks confirm the option is gone, no setting is present, and live "
                  "view is still offered. One old check that turned the option off was "
                  "removed with the option."],
                 ["JavaScript behaviour", verdict("PASS"), "46 of 46, run with no "
                  "CFG_GO2RTC defined, so any leftover reference would have thrown."],
                 ["Page build", verdict("PASS"), "Syntax check on the full page script; "
                  "no new unreplaced placeholder."],
                 ["Settings consistency", verdict("PASS"), "run.sh reads, config.yaml "
                  "options and config.yaml schema all match."],
                 ["Package", verdict("PASS"), "Zip byte-identical to tag 2.6.4; POSIX "
                  "paths; no CRLF; option absent from the packaged config and run.sh."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [PageBreak(),
          h1("Best-practices compliance"),
          p(BP_INTRO), p(BP_SCOPE),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST plus duplicate-def scan", verdict("PASS"),
                  "153 top-level definitions, no duplicates."],
                 ["1.2 Syntax errors", "compile() to bytecode", verdict("PASS"), "Gate 1."],
                 ["1.4 Async/await", "The startup change", verdict("PASS"),
                  "The supervisor task is created unconditionally, kept in "
                  "_GO2RTC_TASK and cancelled at shutdown, as before."],
                 ["1.5 Dead code", "Searched for leftovers", verdict("PASS"),
                  "No reference to the option remains in any shipped file; the JS test "
                  "would throw on one."],
                 ["1.5 Line length", "Measured every added line", verdict("NOTED"),
                  "2 of 15 added lines exceed 79 characters: a section banner comment "
                  "at 81, and an existing run.sh log line, now shorter than before."],
                 ["Part 2 JavaScript", "Removed guards and constant", verdict("PASS"),
                  "Syntax check and 46 behaviour checks pass."],
                 ["Parts 3 and 4", "No markup or CSS changed", verdict("PASS"),
                  "Not applicable."],
                 ["5.1 Escaping context", "Removed a page placeholder", verdict("PASS"),
                  "No new unreplaced placeholder in the built page."],
                 ["Outside Parts 1-5: security", "Exposure now present on every install",
                  verdict("NOTED"), "go2rtc now runs everywhere, where before it ran only "
                  "with the option on. Its four controls are unchanged and still pinned "
                  "by the gate: API on 127.0.0.1, no command-running modules, RTSP "
                  "server off, proxy allowlist. The WebRTC media port 28555 is now "
                  "always open."],
                 ["Outside Parts 1-5: configuration", "Settings consistency",
                  verdict("NOTED"), "Checked by hand this release. No gate checks it; "
                  "logged as build plan F7."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets([
              "<b>Firefox and LibreWolf</b> show a red Live view unavailable message once "
              "per H.265 camera per page load, then use the classic view. With the option "
              "gone this can no longer be switched off. Build plan C10.",
              "<b>Slow-starting streams</b> fall back after 12 s with the same message, "
              "seen on the Oak-D H.264 camera. Build plan B13, logs first.",
              "Unchanged from 2.6.3: Skip Non-Reference Frames breaks the cameras it "
              "applies to (B3); hardware decode falls back to software (B11); a motion "
              "recording may not stop (B1)."])]

    s += [h1("Not verified"),
          *bullets([
              "<b>The image was not built here.</b> The Dockerfile is unchanged from "
              "2.6.3, which built on both systems, so build risk is low.",
              "<b>The upgrade warning</b> is inferred from Supervisor source, not seen on "
              "a real upgrade."])]

    s += [h1("Acceptance"),
          *bullets([
              "The Configuration tab no longer shows Live View (Enhanced View).",
              "The startup log shows go2rtc: ready, with no option set.",
              "Enhanced View opens live in Chrome on both systems, as with 2.6.3 and the "
              "option on.",
              "At most one Supervisor warning about go2rtc_live_view after the upgrade."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["camera_discovery.py", "Removed CFG_GO2RTC, its checks in the focus API, "
                  "the proxy and startup, and its page constant and guards. go2rtc "
                  "supervised unconditionally. Version 2.6.4."],
                 ["config.yaml", "Removed the go2rtc_live_view option and schema entry. "
                  "Version 2.6.4."],
                 ["run.sh", "Stopped reading go2rtc_live_view; dropped it from the "
                  "startup config log line."],
                 ["translations/en.yaml", "Removed the Live View description."],
                 ["CHANGELOG.md", "2.6.4 entry."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("2.6.4", story(), "anycam_2_6_4_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
