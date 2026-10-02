"""Audit report for AnyCam 2.6.6 — the first scored against Parts 1 to 6."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, h2, mono, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph

BP_INTRO_266 = (
    "Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, as CLAUDE.md "
    "rule 1 requires. <b>Scope change from 2.6.5:</b> the full document was "
    "restored on 2026-09-30 (sections 1.6 to 1.18 and Part 6 had been lost from "
    "the repository; CrystalHeeler found the May 2026 copy). This is the first audit "
    "since 2.3.2 scored against all six parts. Audits 2.6.0 to 2.6.5 marked "
    "such rows <i>outside Parts 1-5</i>.")


def story():
    s = [Paragraph("AnyCam 2.6.6 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - motion by pixels, live cards, release "
                   "checks - tag 2.6.6" % DATE, S_SUB),
         p("2.6.6 replaces file-size motion detection with pixel comparison, gives "
           "each camera its own recording settings from a cog on its card, plays "
           "live video in the camera cards, fixes four bugs (B4, B5, B9, B10), and completes "
           "release-engineering items F1 to F7. Scope confirmed by CrystalHeeler on "
           "2026-09-30, then extended the same day after his first field test "
           "(per-camera settings, global override, tuning line, card changes). "
           "camera_discovery.py: 937 lines added, 182 removed; verify_release.py: "
           "204 added, 11 removed.")]

    s += [h1("What 2.6.5 confirmed working"),
          table([["Item", "Evidence"],
                 ["Enhanced View", "CrystalHeeler, 2026-09-30: landscape full screen, menus "
                  "removed, loading message, the Oak-D camera camera live, all good."],
                 ["Cards, button, changelog", "Loading text, the Record button's "
                  "state, and the HA changelog after an update: good."],
                 ["Recordings stop", "Log 2026-09-30: a 9 s clip at 06:21."],
                 ["Motion loops without a viewer", "Log 07:34 to 14:21: both Lorex "
                  "loops ran all day, one snapshot every 1.9 s."],
                 ["Not working", "Motion on the Lorex channels: nothing recorded "
                  "after 08:24 at sensitivity 50 (a 5.1% file-size change). The "
                  "reason for 2.6.6's main change."]],
                [1.8 * inch, 4.9 * inch])]

    s += [h1("Motion detection: design and evidence"),
          p("File sizes cannot see a person in a 9 to 10 KB snapshot. Each frame is "
            "now shrunk to 64 x 48 grey cells and compared with the previous one at "
            "most once a second. Three design steps came from measurements, not "
            "assumptions:"),
          table([["Step", "What the first version measured", "Change"],
                 ["Brightness", "Cancelling only the mean: a 30% brighter picture "
                  "read as 7.4% changed, over the 5% default.",
                  "Normalise by mean and spread (contrast) as well: now 0%."],
                 ["Light rule", "After normalising, even an unrelated picture read "
                  "as only 48% changed, so a '75% changed' rule could never fire.",
                  "Measure spread instead: change in more than 75% of 16 areas is "
                  "light. Person block: 25%; inverted picture: 100%."],
                 ["Scale", "CrystalHeeler: 1 to 100 shown, 1% to 74% of the picture "
                  "hidden, default 5%.",
                  "Log mapping; 63 = 5.0%, finer steps at the sensitive end."]],
                [1.1 * inch, 3.0 * inch, 2.6 * inch]),
          table([["Synthetic test (704 x 480 scene)", "Changed", "Spread"],
                 ["New camera noise only", "0%", "0%"],
                 ["30% brighter / darker; 40% more contrast", "0%", "0%"],
                 ["Night-mode style (grey, darker)", "0%", "0%"],
                 ["Person block, 1.2% of the picture", "1.3%", "6%"],
                 ["Person block, 4.7%", "5.0%", "25%"],
                 ["Whole picture inverted", "72%", "100% (light)"]],
                [3.9 * inch, 1.3 * inch, 1.5 * inch]),
          p("Cost measured on the build PC: 0.9 ms to decode and 0.4 ms to compare "
            "one frame. A Raspberry Pi 4 is slower; at one comparison per second per "
            "armed camera the load stays small, but it runs on the event loop "
            "(see 1.4 below).")]

    s += [h1("First field test and what changed"),
          p("CrystalHeeler installed the first 2.6.6 build on 2026-09-30, armed two cameras "
            "and walked past one twice for at least 8 s. Nothing recorded. His "
            "Motion Sensitivity was 5; on the 1-100 scale that is 62% of the "
            "picture. The log showed both loops running and no near-miss line, "
            "because near misses were logged only above half the threshold."),
          p("He asked for a slider. Home Assistant cannot draw one for an add-on "
            "setting: its add-on configuration page maps every integer to a number "
            "box (supervisor-app-config.ts, mode \"box\"), and the Supervisor sends "
            "integer ranges as lengthMin/lengthMax, which that page ignores. So the "
            "slider moved into AnyCam's own page."),
          table([["Change", "Detail"],
                 ["Cog menu", "Per camera: sensitivity slider 1-100 (value shown as it "
                  "moves), live reading in slider units with an orange marker, "
                  "cooldown, tail, file length, folder under /media. Saved in "
                  "/data/motion.json. Defaults 63, 5 s, 3 s, 30 s, /media/anycam."],
                 ["Global override", "global_recording_settings (off): when on, the "
                  "five Global settings apply to every camera and the panel is "
                  "read-only; POST and DELETE return 409."],
                 ["Tuning line", "Once a minute per armed camera: largest change, the "
                  "sensitivity that would have recorded it, comparisons, light "
                  "changes. INFO when something moved, DEBUG otherwise."],
                 ["Folder validation", "posixpath.normpath, then must be /media or "
                  "below; '..' that climbs out is refused. Found by the Windows test "
                  "run: os.path.normpath rewrote '/media/x' with backslashes."],
                 ["Cards", "Protocol, IP and port at the top of Identity; Test "
                  "Stream button and /stream/{id}/test removed."],
                 ["Second round (2026-10-01)", "Identity opens from an info icon after "
                  "the name; lock at the end of the button row; cards keep their own "
                  "height (grid align-items:start), measured: no gap under the buttons, "
                  "fullest row fits from 413 px; a single-file recording is renamed "
                  "without _part01; stray closing div removed."]],
                [1.4 * inch, 5.3 * inch])]

    s += [h1("Third round: live detection and a 3 s pre-roll"),
          p("CrystalHeeler, 2026-10-01: recordings caught only the end of each event, even "
            "at sensitivity 80 to 90, and played choppily. Detection compared "
            "snapshots that the Lorex channels deliver every 1.9 s, and the recording "
            "connected to the camera only after motion was found, then waited for a "
            "keyframe. He asked for a fixed 3 s pre-roll on every event."),
          table([["Part", "Design"],
                 ["Detection", "_motion_detector: ffmpeg decodes the smallest RTSP "
                  "stream (Lorex subtype=1; MJPEG allowed) to 64 x 48 grey at 4 frames "
                  "a second; each frame is compared with the one 1 s before. Cameras "
                  "with only a stream wider than 1920, or no RTSP, keep snapshots."],
                 ["Buffer", "_motion_buffer: the main stream copied (video unchanged, "
                  "audio to AAC) into MPEG-TS; _TsBuffer keeps the newest keyframe at "
                  "least 3 s old and everything after it, found by the TS random-access "
                  "flag."],
                 ["Writer", "On motion: an ffmpeg reading MPEG-TS from its input gets "
                  "PAT, PMT and the buffered GOPs, then each live packet; packets that "
                  "arrive while it starts are queued and handed over in order. Closing "
                  "its input finishes the file. hvc1 tag for H.265."],
                 ["Cost per armed camera", "Two camera connections held open, a light "
                  "decode, a few MB of memory."]],
                [1.4 * inch, 5.3 * inch])]

    s += [h1("Other changes"),
          table([["Item", "Change"],
                 ["C12 recording length", "Segment muxer, a new file every 10 s to "
                  "5 min (default 1 min), motion_&lt;date&gt;_&lt;time&gt;_partNN.mp4. "
                  "Recording claims its slot before its first await and stops once."],
                 ["C1 live cards", "/api/go2rtc/card picks the smallest playable "
                  "stream; Dahua subtype=1 for DVR channels; over 1920 wide stays on "
                  "snapshots. One player per camera, moved into each redrawn card; "
                  "pauses off screen, in the background and during Enhanced View."],
                 ["B4", "13 onclick handlers encoded with jsArg (JSON, then HTML "
                  "escaping); esc() escapes single quotes; info bar and IP badge "
                  "escaped. The old card-name handler broke on a backslash."],
                 ["B5", "___COMMUNITY___ filled as a JSON literal with &lt; escaped."],
                 ["B9", "_stop_proc: wait for an exiting ffmpeg before kill(); Python "
                  "3.9+ kill() polls and so reaped it before asyncio's watcher "
                  "(CPython 3.11 source read)."],
                 ["B10", "go2rtc, then web server, then hardware probe; snap_loop "
                  "waits for _HW_PROBED; a card retries if go2rtc is starting."],
                 ["F1-F7", "Gates 6 and 7, gate 3 extended, 7 contracts; build.yaml "
                  "removed and BUILD_FROM defaulted; 20 bundles to PDFs; CLAUDE.md "
                  "refreshed."]],
                [1.4 * inch, 5.3 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Seven gates, 37 contracts, 19 "
                  "settings in agreement. Gate 7 "
                  "checked 5 apt pins, 3 pip pins, 2 go2rtc digests and the base "
                  "image online."],
                 ["Gate self-test", verdict("PASS"), "Broke six things in a copy: an "
                  "unknown setting, an unfilled placeholder, a stale apt pin, a wrong "
                  "digest, async write_bytes, and the HTTP motion call. Each failed "
                  "the gate."],
                 ["Python integration", verdict("PASS"), "204 of 204, up from 120. "
                  "Settings API (defaults, save, six refusals, persistence, Defaults, "
                  "global override), per-camera cooldown and tail, tuning line, file "
                  "renaming, card layout, the TS buffer (keyframe choice, split reads), "
                  "and the detector, buffer and writer end to end with stand-in "
                  "processes: pre-roll from before the motion, no packet lost or "
                  "repeated at the hand-over. Also: "
                  "detector (brightness, contrast, spread, scale), real HTTP loop "
                  "recording on a person, card stream choice for Lorex, Hikvision, "
                  "Oak-D, B5 placeholder, B9 stop, B10 order and probe wait."],
                 ["JavaScript behaviour", verdict("PASS"), "106 of 106, up from 75. "
                  "Cog panel reading and read-only state. Also: "
                  "New: hostile names through jsArg in a simulated browser; live "
                  "cards (attach, redraw, fallbacks, retry, watchdog, pause, prune)."],
                 ["Visual check", verdict("PASS"), "Card and cog panel rendered in a "
                  "browser with stand-in data: cog placement, Identity order, slider "
                  "drag 63 to 92 updating the value, marker at 72, read-only state."],
                 ["Not run (2)", verdict("NOTED"), "The detection, buffer and writer "
                  "ffmpeg commands were checked against the ffmpeg 5.1 documentation "
                  "and tested with stand-in processes only."],
                 ["Not run", verdict("NOTED"), "ffmpeg is not installed on the build "
                  "PC (rule 4): the segment command is checked against the ffmpeg 5.1 "
                  "documentation only. No Docker: the image was not built."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [PageBreak(),
          h1("Best-practices compliance"),
          p(BP_INTRO_266),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST, duplicates, 31 contracts",
                  verdict("PASS"), "178 top-level definitions, no duplicates."],
                 ["1.2 Syntax", "compile()", verdict("PASS"), "Gate 1."],
                 ["1.4 Async/await", "Blocking work on the loop", verdict("NOTED"),
                  "Gate 3 now also catches pathlib I/O and found the OUI write (fixed). "
                  "Motion decoding (about 1 ms per armed camera per second) runs on "
                  "the event loop by choice; move to a thread if the Pi shows lag."],
                 ["1.5 Style: line length", "Measured added lines", verdict("NOTED"),
                  "116 of 937 exceed 79 characters: 37 Python (up to 100), 79 in the "
                  "page script, markup and CSS strings."],
                 ["1.6 Function design", "New helpers", verdict("PASS"),
                  "Small single-purpose helpers (_motion_thumb, _motion_diff, "
                  "_motion_judge, _stop_proc, _go2rtc_card_source); keyword-only "
                  "grace and timeout in _stop_proc."],
                 ["1.7 Mutability", "Shared state", verdict("NOTED"),
                  "Motion state still lives in the global _MOTION dict, now with "
                  "stopping and clip_base keys; build plan E2."],
                 ["1.10 Exceptions", "Narrow catches", verdict("PASS"),
                  "_motion_thumb catches OSError, ValueError and "
                  "DecompressionBombError; _stop_proc catches ProcessLookupError and "
                  "TimeoutError; replaced two bare 'except Exception: pass' pairs."],
                 ["1.12 Logging", "New lines", verdict("PASS"),
                  "Light changes and triggers at INFO with their percentages; near "
                  "misses at DEBUG, for tuning."],
                 ["1.17 Stdlib first", "New dependency", verdict("NOTED"),
                  "Pillow added: the standard library cannot decode JPEG. See 6.2."],
                 ["1.18 Testing", "Behavioural tests", verdict("NOTED"),
                  "250 checks, still in the session scratchpad, not the repo "
                  "(build plan E7)."],
                 ["2.2 JS async", "No await in forEach", verdict("PASS"),
                  "Card players use per-camera promises; failures fall back."],
                 ["3.3 / 5.1 Escaping", "Camera data into HTML and JS", verdict("PASS"),
                  "B4: every onclick camera value through jsArg; simulated-browser "
                  "test with quote, backslash and tag payloads."],
                 ["4.3 CSS units", "Card player", verdict("PASS"),
                  "Absolute inset:0 over the 16:9 card; no new vh units."],
                 ["6.1 Security: paths", "Recording folder from the page", verdict("PASS"),
                  "Validated on the server: POSIX-normalised, must stay under /media; "
                  "levels, times and lengths range-checked; tested with six bad inputs."],
                 ["6.1 Security", "Boundary validation", verdict("PASS"),
                  "Camera-derived values encoded for their context (B4); the "
                  "community endpoint JSON-encoded with &lt; escaped (B5); card "
                  "streams go through the existing go2rtc allowlist; no shell."],
                 ["6.2 Dependencies", "Pillow 12.3.0", verdict("PASS"),
                  "Pinned; wheels for CPython 3.11 aarch64 and x86_64 confirmed by "
                  "gate 7; reason documented in the Dockerfile."],
                 ["6.3 Version control", "Commits and gate", verdict("PASS"),
                  "Nine build commits, one per stage (one restores a Dockerfile "
                  "edit a script missed), then the release commit; the gate ran "
                  "on each; tag after the final gate run."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets([
              "<b>File splitting and the image build are untested</b> (no ffmpeg or "
              "Docker on the build PC). First install is the test (A12).",
              "<b>Motion thresholds are tuned on synthetic pictures.</b> Real camera "
              "noise, rain and trees may need a different setting; the tuning line "
              "and the cog's live reading show where to set each camera.",
              "<b>Live cards fell back to snapshots in Chrome</b> on the first field "
              "test (B17); needs the browser console lines.",
              "<b>The Storage tab</b> opens on the global recordings folder; cameras "
              "recording elsewhere show in Home Assistant's Media browser.",
              "<b>Choppy recordings</b> (B18): cause unknown; needs a recorded file. "
              "The recording path changed in this round, so test again first.",
              "<b>Connections:</b> each armed DVR channel holds two more connections "
              "to the DVR; a DVR's connection limit is unknown.",
              "Opening the classic view still stops the card's stream (B15).",
              "Skip Non-Reference Frames breaks H.264 cameras (B3)."])]

    s += [h1("Acceptance"),
          *bullets([
              "The image builds on the Pi with the new base image.",
              "An armed Lorex channel records a person walking past, page closed.",
              "The cog opens each camera's settings; the slider value moves with the "
              "drag; the live reading appears after a walk-past; settings survive a "
              "restart; the global switch makes the panel read-only.",
              "A long event produces part01, part02 ... of about the chosen length.",
              "The log shows light changes ignored at dawn and dusk, with percentages.",
              "Cards play live; the Hikvision card stays on snapshots.",
              "No 'Cannot connect' and no 'Unknown child process' lines.",
              "One Supervisor warning about motion_sensitivity after the upgrade."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["camera_discovery.py", "Motion detection, recording, live cards, "
                  "B4, B5, B9, B10, OUI write. Version 2.6.6."],
                 ["verify_release.py", "Gates 6 and 7, gate 3 P4 and P7, 7 contracts."],
                 ["config.yaml, run.sh, translations/en.yaml",
                  "global_recording_settings added; motion_detect_level and "
                  "motion_clip_length replace motion_sensitivity; the five recording "
                  "settings renamed Global; defaults 30s and 5 s; storage-browser "
                  "description. Version 2.6.6."],
                 ["Dockerfile, build.yaml", "Pillow 12.3.0; BUILD_FROM default; "
                  "build.yaml deleted."],
                 ["docs", "Best-practices document restored; 20 audit PDFs rebuilt; "
                  "CLAUDE.md, README and build plan updated."],
                 ["CHANGELOG.md", "2.6.6 entry."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("2.6.6", story(), "anycam_2_6_6_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
