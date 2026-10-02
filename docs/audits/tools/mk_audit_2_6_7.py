"""Audit report for AnyCam 2.6.7 — night boost (build plan C15)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph

BP_INTRO = (
    "Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, Parts 1 to 6, "
    "as CLAUDE.md rule 1 requires. Only the rows that 2.6.7's changes touch are "
    "scored; the rest carry from the 2.6.6 audit unchanged.")


def story():
    s = [Paragraph("AnyCam 2.6.7 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - night boost, insects, infrared switches, "
                   "file names - tag 2.6.7" % DATE, S_SUB),
         p("2.6.7 adds build plan C15: under infrared, each armed camera's motion "
           "sensitivity rises by 15 on its own, and a sunrise and sunset check "
           "reports cameras that do not switch. Scope confirmed by CrystalHeeler on "
           "2026-10-01: version 2.6.7 (SFTP and FTP moved to 2.6.8), +15 for every "
           "camera, no 'small changes must last' rule, night watched all the time, "
           "location from Home Assistant, missed switches reported three ways. "
           "A second round, the same day, after CrystalHeeler's overnight test on 2.6.6: "
           "insects and infrared-colour switches stop recording, and recording "
           "names start with the camera (CrystalHeeler: version 2.6.7, name choice A). "
           "Against 2.6.6: camera_discovery.py 392 lines added, 33 removed; "
           "verify_release.py 18 added, 3 removed.")]

    s += [h1("Evidence for the change"),
          table([["Measurement", "Value", "Source"],
                 ["Distant approach on ch4, before reaching 1.1%", "0.6-0.7% for 6-8 s",
                  "CrystalHeeler's log, 2026-10-01, 22:47:01-:07 and 22:49:33-:41"],
                 ["Still night scene, minute peaks", "median 0.1%, highest 0.7%",
                  "Same log, 22:27-23:03"],
                 ["Most sensitive setting before 2.6.7", "100 = 1.0%", "2.6.6 scale"],
                 ["Night setting 80 + 15 = 95", "1.24%", "2.6.7 scale"],
                 ["Night setting 100 + 15 = 115", "0.52%", "2.6.7 scale"],
                 ["Floor", "0.4%", "Never reached by 115; a safety bound"]],
                [2.6 * inch, 1.6 * inch, 2.5 * inch]),
          p("The 0.7% still-scene maximum is above 0.52%, so a camera at 100 by day "
            "can record a false event at night. CrystalHeeler chose +15 for every camera "
            "knowing this; each camera's slider still sets its own night level.")]

    s += [h1("Design"),
          table([["Part", "Design"],
                 ["Night signal", "The detector's ffmpeg output changed from grey to "
                  "YUV 4:2:0 (4,608 bytes per 64 x 48 frame). Once a second the colour "
                  "planes give a measure: mean distance from neutral. At or under 2.5 "
                  "for 30 s = night; at or over 5.0 for 30 s = day; between = no "
                  "change. Snapshot cameras: Pillow YCbCr on each JPEG."],
                 ["Boost", "_motion_area_now: the camera's level plus 15 at night, "
                  "into the same log scale, continued past 100, floor 0.4%."],
                 ["Readings", "Slider units at all times: _motion_level_for_pct takes "
                  "the boost off. The tuning line adds the mode and colour value."],
                 ["Clock", "Home location from GET /core/api/config through the "
                  "Supervisor: every 5 min until it answers, then every 12 h. "
                  "Sunrise and sunset from the US Naval Observatory almanac formula "
                  "(standard library only), for yesterday, today and tomorrow."],
                 ["Expectation check", "At the first keeper pass after sunset + 1 h "
                  "(or sunrise + 1 h): a camera watched through the whole window, "
                  "without a gap over 120 s, that is not in the expected mode is "
                  "reported once: log warning, cog note, and "
                  "persistent_notification.create in Home Assistant."],
                 ["Permission", "homeassistant_api: true. The Supervisor's Core proxy "
                  "refuses only hassio paths (CORE_API_DENY, read in the Supervisor "
                  "source), so both calls are allowed."]],
                [1.4 * inch, 5.3 * inch])]

    s += [h1("Second round: insects and infrared switches"),
          p("Evidence: CrystalHeeler's 6 recordings, the add-on log for 21:44 to 07:53, and "
            "the 45 recordings in the two camera folders, replayed with OpenCV "
            "4.13.0 on the build PC (it decodes H.265; ffmpeg is not installed)."),
          table([["Finding", "Evidence", "Change"],
                 ["Insects: one picture", "All 12 insect clips: one blurred streak in "
                  "1 or 2 pictures (0.14-0.28 s)", "Live stream: a 2nd changed picture "
                  "within 0.5 s, or one of 3% or more (recorded one picture later)"],
                 ["Echoes", "Log: one insect as 2-3 equal changes in a row (01:48:17, "
                  "1.1% x3). Burst arrival; reference chosen by arrival time",
                  "Reference = 4 pictures back in the stream; exact repeats ignored; "
                  "the insect's own echo 1 s later ignored"],
                 ["Switches", "06:20:35: comparisons at 69%, 75%, 31% spread; 06:22:22: "
                  "a half-switched picture at 19%", "2 s hold after a light change; a "
                  "light change cancels a waiting change"],
                 ["First fix too strict", "Requiring a change against both the 1 s and "
                  "2 s pictures dropped the cat and 4 porch clips", "Not used"],
                 ["Classic view ignored the 30 s cooldown", "test system A log, 23:22:38-23:23:07: 6 "
                  "ffmpeg starts 5 s apart at the Microseven while a 30 s cooldown was "
                  "in force", "Every ffmpeg start waits out the cooldown; a camera in "
                  "cooldown goes to HTTP snapshots after one failed start"],
                 ["3% rule vs switch", "Production replay: 06:20:35 recorded in 4 of 7 "
                  "(a 24% half-switched picture)", "Big change waits one picture for a "
                  "light change: 0 of 7"]],
                [1.3 * inch, 2.9 * inch, 2.5 * inch]),
          table([["Replay through the 2.6.7 code (setting 99, 7 offsets)", "Recorded"],
                 ["11 insect clips (ch4 10, ch7 1)", "0 of 7 each"],
                 ["1 insect clip (ch4 23:37:28)", "1 of 7"],
                 ["2 infrared-colour switches (ch7)", "0 of 7 each"],
                 ["Child on a bike, ch7 07:01:53 (log: 4 pictures in a row)", "7 of 7"],
                 ["Cat, ch4 03:08:48", "4 of 7"],
                 ["Person standing on the porch, ch7 06:59:10", "5 of 7"],
                 ["29 other real events", "7 of 7 each"]],
                [4.9 * inch, 1.8 * inch]),
          p("Limit: the replay uses the recorded main stream; the live detector "
            "watches the sub-stream. The two measured the bike clip differently "
            "(replay peak 1.11%, log 1.1-1.2% in 4 pictures in a row), so the "
            "field test (A13) decides.")]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Seven gates, 45 contracts (8 new, 1 extended for the cooldown fix: "
                  "the detector format, the boost, the hold, the expectation check, "
                  "the API call, the location refresh, the motion decision, the file "
                  "tag), 19 settings in agreement, Dockerfile inputs online."],
                 ["Python integration", verdict("PASS"), "265 of 265, 61 new. Classic view with a camera in cooldown: one ffmpeg "
                  "start, then HTTP snapshots. Second "
                  "round: insect in one picture, with pictures arriving 1, 2 or 3 at a "
                  "time, and repeated 4 times; a walker; a 6% single picture; a big "
                  "change then a light change; the 2 s hold; snapshot cameras; name "
                  "tags. The stand-in detector now varies each picture. Sunrise "
                  "and sunset for London (June), Sydney (June) and Denver (December) "
                  "within 4 minutes of published times; polar day gives none. Colour "
                  "measure on grey and colour JPEGs. The 30 s hold both ways and the "
                  "dead band. Scale past 100 and the floor. Tuning line and cog "
                  "payload in slider units. Expectation check: inside the window, "
                  "missed sunset, missed sunrise, one report only, note cleared, not "
                  "watched, detector down. Location retry and refresh. The live "
                  "detector end to end with YUV frames."],
                 ["JavaScript behaviour", verdict("PASS"), "110 of 110, 4 new: mode line "
                  "day and night, the note shown and hidden, nothing when disarmed."],
                 ["Not run", verdict("NOTED"), "ffmpeg is not installed on the build PC "
                  "(rule 4): the yuv420p frame size (64 x 48 x 1.5 = 4,608 bytes) is "
                  "from the format's definition, not a run. No real infrared picture "
                  "was measured: the 2.5 and 5.0 thresholds are estimates. No Docker: "
                  "the image was not built. The Supervisor calls were tested with a "
                  "stand-in, not a real Home Assistant."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [PageBreak(),
          h1("Best-practices compliance"),
          p(BP_INTRO),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST, duplicates, 43 contracts",
                  verdict("PASS"), "207 top-level functions and classes, no duplicates."],
                 ["1.2 Syntax", "compile()", verdict("PASS"), "Gate 1."],
                 ["1.4 Async/await", "Blocking work on the loop", verdict("NOTED"),
                  "Gate 3 clean. The colour measure (1,536 bytes once a second) runs "
                  "on the loop. The keeper awaits the Home Assistant calls, 10 s "
                  "timeout each: an unreachable Home Assistant delays one keeper pass "
                  "every 5 min by up to 10 s."],
                 ["1.5 Style: line length", "Measured added lines", verdict("NOTED"),
                  "45 of 392 added lines exceed 79 characters (up to 100)."],
                 ["1.6 Function design", "New helpers", verdict("PASS"),
                  "Ten small functions, each one job: _motion_boost, _motion_area_now, "
                  "_motion_chroma, _motion_jpeg_chroma, _motion_night_observe, "
                  "_sun_events_utc, _sun_events_around, _near_sun_event, "
                  "_night_expectation_check, _ha_api (with _ha_location_refresh and "
                  "_ha_notify), _motion_decide, _cam_file_tag. _motion_judge now "
                  "returns a verdict and the percentage."],
                 ["1.7 Mutability", "Shared state", verdict("NOTED"),
                  "Night state joins the global _MOTION dict; _NIGHT_CHECKED is pruned "
                  "after 2 days. Build plan E2."],
                 ["1.10 Exceptions", "Narrow catches", verdict("PASS"),
                  "aiohttp.ClientError and TimeoutError for Home Assistant calls; "
                  "OSError, ValueError, DecompressionBombError for JPEG colour."],
                 ["1.12 Logging", "New lines", verdict("PASS"),
                  "Mode switches at INFO with the level change; outside-window switches "
                  "marked; missed switches at WARNING; colour value in the tuning line."],
                 ["1.17 Stdlib first", "Sun position", verdict("PASS"),
                  "Standard-library math; no astronomy package added."],
                 ["1.18 Testing", "Behavioural tests", verdict("NOTED"),
                  "354 checks, still in the session scratchpad (build plan E7)."],
                 ["2.2 JS async", "Cog panel", verdict("PASS"),
                  "No new async code; the mode line fills from the existing poll."],
                 ["3.3 / 5.1 Escaping", "Note text into the page", verdict("PASS"),
                  "Set with textContent, not innerHTML."],
                 ["5.x CSS in f-string", "{{ }} escaping", verdict("PASS"),
                  "Three new rules, braces doubled; page renders (page tests)."],
                 ["6.1 Security", "New permission", verdict("NOTED"),
                  "homeassistant_api gives the add-on Home Assistant's REST API. Used "
                  "for two fixed paths only, no user input in either; token from the "
                  "environment, never logged."],
                 ["6.2 Dependencies", "None added", verdict("PASS"), "Gate 7."],
                 ["6.3 Version control", "Commits and gate", verdict("PASS"),
                  "One build commit and the release commit; gate run before each; "
                  "tag after the final run."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets([
              "<b>Colour thresholds are estimates.</b> Field check in A13 reads the "
              "colour value from the tuning line by day and by night.",
              "<b>A camera in colour at night under lights</b> gets no boost, by "
              "design; it is reported once a day after sunset.",
              "<b>False events at night:</b> the still-scene peak (0.7%) is above the "
              "boosted 100 (0.52%).",
              "<b>Fast animals</b> in view for under 0.5 s and under 3% of the picture "
              "are not recorded (the cat: 4 of 7 replays).",
              "<b>Several insects at once</b> can still start a recording.",
              "Opening the classic view still stops the card's stream (B15).",
              "Skip Non-Reference Frames breaks H.264 cameras (B3)."])]

    s += [h1("Acceptance"),
          *bullets([
              "Home Assistant asks for the API permission on update; the log shows "
              "'Home Assistant location received' with sunrise and sunset times.",
              "After dark, each armed camera logs 'night (IR, black-and-white)' and "
              "its cog says 'Night mode (IR): sensitivity +15'.",
              "A distant walker at night records at a setting that missed it in 2.6.6.",
              "One hour after sunset, no notification for cameras that switched.",
              "At dawn, cameras log 'day (colour)' and the setting returns.",
              "Overnight: far fewer insect recordings; 'changed in one picture only' "
              "lines instead; no recording from the dawn or dusk switch.",
              "Recordings named LorexCH4_&lt;date&gt;_&lt;time&gt;.mp4."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["camera_discovery.py", "Night boost, sun clock, Home Assistant calls, "
                  "cog mode line, light hold, single-picture rejection, stream-clock "
                  "reference, file tag. Version 2.6.7."],
                 ["config.yaml", "homeassistant_api: true. Version 2.6.7."],
                 ["verify_release.py", "8 new contracts; 4 updated."],
                 ["docs", "Build plan: C15 and B17 done, A13 field test; HTML "
                  "regenerated."],
                 ["CHANGELOG.md", "2.6.7 entry."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("2.6.7", story(), "anycam_2_6_7_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
