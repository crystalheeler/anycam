"""Audit report for AnyCam 2.6.5, in the shared 2.6.x format (audit_lib.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (BP_INTRO, BP_SCOPE, DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, h2, mono, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph


def story():
    s = [Paragraph("AnyCam 2.6.5 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - Enhanced View changes - tag 2.6.5" % DATE, S_SUB),
         p("2.6.5 restores motion detection on the Lorex channels and makes it "
           "work with no viewer (build plan B1, C2). It changes Enhanced View in "
           "four ways CrystalHeeler asked for on 2026-09-29: slow-starting cameras play "
           "live, landscape on a phone fills the screen, the Resolution, Frame Rate "
           "and Auto menus are gone, and a loading message replaces the blank or "
           "gray screen. It closes B14, and the zip folder no longer carries the "
           "version (F8). camera_discovery.py against 2.6.4: 386 lines added, 328 "
           "removed. A companion release, Oak-D camera add-on 2.4.2, fixes the "
           "slow-starting stream at its source.")]

    s += [h1("Investigation before code"),
          h2("Motion detection stopped working (B1)"),
          p("CrystalHeeler, 2026-09-29, on 2.6.4: ch7 of the Lorex DVR was armed and no clip "
            "was written. His log shows every Lorex card polling HTTP snapshots "
            "(\"http frame\" lines). Motion detection was called only on the ffmpeg "
            "path (_motion_on_frame did not exist; the check sat inline in "
            "snap_loop). The HTTP loop has had no motion check in any version, "
            "checked back to the archived 2.5.0."),
          p("CrystalHeeler said it had worked on 2026-09-28, on the Lorex cameras only. "
            "Log1 (that date) settles it: ch7's card was on HTTP polling, and ffmpeg "
            "frames appear only while ch7 was open in the classic Enhanced View. "
            "Motion worked there, and only there. 2.6.4 made live view the default, "
            "so the classic view stopped running and so did motion detection: a "
            "regression from our change, as rule 8 predicts. The same mechanism "
            "explains B1: leaving the view ended the only loop that could stop a "
            "recording."),
          p("I first told CrystalHeeler this was not a regression, reasoning from the HTTP "
            "loop's history alone. Log1 corrected it. CrystalHeeler also recalled an earlier "
            "discussion, and found it: during the 2.6.3 build (2026-09-29 03:04) I "
            "noted that the snapshot loop stops 30 s after the last poll, so armed "
            "cameras would lose motion recording, and fixed it for live view only: "
            "_focus_set_go2rtc starts the thumbnail loop when motion is armed, and "
            "the card poller keeps it alive while the page is open. That fix assumed "
            "the thumbnail loop checks motion, which is true only on the ffmpeg path; "
            "on the Lorex channels it kept alive an HTTP loop that never checked. My "
            "first search missed it because it read only my replies, not my "
            "reasoning blocks. 2.6.5 keeps the 2.6.3 behaviour and extends it."),
          h2("The Oak-D H.264 camera (B13)"),
          p("CrystalHeeler saw this camera drop to poor quality on the phone, with the "
            "Connecting to RTSP stream message. The 2026-09-29 log from test "
            "system B (2.6.3) gave the cause. Every new connection to this stream was "
            "timed from start to first frame:"),
          table([["Result", "Connections", "Time to first frame"],
                 ["Produced a frame", "12", "19 s shortest, 23 s median, 28 s longest"],
                 ["Gave up first", "13", "at 12 s (live view) or 30 s (classic)"]],
                [1.6 * inch, 1.1 * inch, 4.0 * inch]),
          p("The phone session at 12:19: live view was not tried, because a failure "
            "at 07:42 was remembered until the page reloaded, and the HA app kept "
            "the page open all day. The classic view then stopped the card's working "
            "stream (frame 38,450) and opened a new connection, which timed out "
            "after 30 s with no frames. The poor quality was the card thumbnail "
            "left in the buffer."),
          p("go2rtc logged no errors. The source of the stream is the Oak-D camera "
            "add-on (version 2.4.1, on this PC at <project folder>). "
            "Its ffmpeg command runs libx264 with no -g option:"),
          mono("-c:v libx264 -preset ultrafast -tune zerolatency -b:v 1000k"),
          p("x264's default is one keyframe every 250 frames. A player cannot draw "
            "until the first keyframe, so a long interval gives every viewer a long "
            "start. The fix belongs in that add-on and needs CrystalHeeler's order."),
          h2("No changelog in Home Assistant"),
          p("CrystalHeeler's folder listing showed CHANGELOG.md present in "
            "/addons/camera_discovery-2.6.4/. Supervisor source gives the cause: on "
            "a store reload it refreshes each add-on's file checks before it reads "
            "the new folder location (supervisor/store/__init__.py, reload()), so "
            "the check runs against the deleted 2.6.3 folder and caches no "
            "changelog. The versioned folder name (CLAUDE.md rule 3) triggers it. "
            "No AnyCam code is involved; the workaround and the choice of lasting "
            "fix are in the changelog and build plan F8."),
          h2("Landscape: what the page can do"),
          p("Home Assistant's app panel (home-assistant/frontend, "
            "src/panels/app/ha-panel-app.ts) accepts a subscribe-properties message "
            "with kioskMode from the add-on page and hides its title bar; the "
            "unsubscribe message restores it. Present since the January 2026 app "
            "panel. The panel's iframe has no allow=\"fullscreen\", so the browser "
            "Fullscreen API is not available.")]

    s += [h1("Changes"),
          table([["Change", "Detail"],
                 ["Live view waits 30 s", "Up from 12 s, CrystalHeeler's choice. Covers the "
                  "28 s longest start measured."],
                 ["Live view retries", "Only a codec or mode error is remembered for "
                  "the page's life (Firefox and H.265, C10). A timeout, a dropped "
                  "connection or an error that names no mode is not."],
                 ["Landscape full screen", "Touch screens only: (orientation: "
                  "landscape) and (pointer: coarse). Hides the bottom bar and sends the "
                  "kiosk request; portrait or close sends the release. The message "
                  "targets the page's own origin."],
                 ["Menus removed", "Resolution, Frame Rate and Auto, their JS "
                  "(focusPickRes, focusPickFps, focusResetAuto, _loadFocusProfiles, "
                  "_applyFocusTier, _go2rtcSwitchProfile) and CSS. Classic button and "
                  "stream information kept. \"Decoded on this device\" text removed."],
                 ["Old manual tier cleared", "The server kept a manual tier between "
                  "sessions and the page can no longer clear it. handle_focus_set now "
                  "drops manual_override on entry. Learned adaptive locks stay."],
                 ["Loading message", "\"Loading feed, please wait…\" until the first "
                  "frame, both engines. Live player poster set to a 1x1 transparent GIF "
                  "(the Android WebView gray play icon). Classic hides the thumbnail "
                  "until the server's new X-Focus-Frames header is above 0."],
                 ["Connecting status", "Before the first frame it shows as the "
                  "loading message's second line instead of the warning box."],
                 ["Cards (B14)", "\"Loading feed, please wait…\" while starting; "
                  "\"Stream unavailable\" only after 90 s of errors, up from 3 errors "
                  "(about 16 s)."]],
                [1.7 * inch, 5.0 * inch]),
          h2("Motion detection"),
          table([["Change", "Detail"],
                 ["Both paths", "_motion_on_frame is called from the ffmpeg loop and "
                  "the HTTP snapshot loop."],
                 ["No viewer needed", "All four idle stops skip an armed camera."],
                 ["Keeper", "_motion_keeper, every 10 s: restarts a dead loop for an "
                  "armed camera (not during the classic view, which runs its own), "
                  "stops a recording after cooldown plus padding whether or not frames "
                  "arrive (B1), and clears a recording whose ffmpeg exited."],
                 ["Persistence", "/data/motion.json holds the armed list; restored "
                  "at start for cameras that still exist."],
                 ["Record button", "Field test 2026-09-30: the server restored ch7 "
                  "after both restarts, but the card said Record, and a click at "
                  "07:20:45 disarmed it. The page never read the server's state, and "
                  "the button flipped it blindly. Now: GET /api/motion for all cameras "
                  "at load and every 3 s; the button sends the state asked for; no "
                  "body keeps the old flip."],
                 ["Recording stderr", "Drained, so a full 64 KB pipe cannot block "
                  "ffmpeg mid-recording."],
                 ["False triggers", "The comparison frame resets on every ffmpeg "
                  "launch and HTTP loop start: a resolution change read as motion."],
                 ["Throttle", "An armed camera is not paused while another camera is "
                  "focused in live view (no Pi decode). The classic view still pauses "
                  "others."],
                 ["Cost", "Each armed Lorex channel polls a snapshot every 1.8 s "
                  "(measured) at all times."]],
                [1.7 * inch, 5.0 * inch]),
          h2("Packaging (F8, CrystalHeeler's order)"),
          p("The zip keeps its versioned name; the folder inside is always "
            "local_camera_discovery. CLAUDE.md rule 3 is changed with the reason "
            "written in. The first update into the new folder can still show no "
            "changelog once; the changelog gives the one-time touch command."),
          h2("Oak-D camera add-on 2.4.2 (separate project)"),
          p("On CrystalHeeler's order: -g set to the FPS option, one keyframe per second. "
            "Committed and tagged v2.4.2 in <project folder>; "
            "packaged as oak_camera_app-2.4.2.zip with folder oak_camera_app, the "
            "name on the Pi. Syntax-checked only; no Oak-D tests exist."),
          h2("Held back: fix 1c"),
          p("Proposed and approved: keep the card's stream until the classic view's "
            "new stream delivers. Not built. It needs two connections to one camera "
            "at once, through per-camera state (_SNAP) that holds one process and "
            "one task. Rate-limited cameras such as the Microseven refuse a second "
            "connection. Build plan B15, to discuss.")]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "All five gates; 24 function "
                  "contracts; go2rtc API host check."],
                 ["Python integration", verdict("PASS"), "120 of 120, up from 81. "
                  "Section G: manual tier dropped on entry, learned lock kept, "
                  "X-Focus-Frames 0 then 1, 8 page checks. Section H (motion): "
                  "detection and stop, keeper stops a recording with no frames, "
                  "restarts a loop, clears a dead recording, respects the classic view; "
                  "toggle saves, restart restores; and the real HTTP snapshot loop "
                  "against a local server keeps polling with no viewer, runs every "
                  "frame through detection, starts a recording on a size jump, and "
                  "stops once disarmed and idle. H6, through a real server: set-state "
                  "is idempotent, the all-cameras map, the no-body flip."],
                 ["JavaScript behaviour", verdict("PASS"), "75 of 75, up from 46. Real "
                  "vendored player class. New: blank poster, 30 s watchdog, a first "
                  "frame at 23 s plays, what is and is not remembered, landscape kiosk "
                  "messages and origin, and the classic loading gate driven through "
                  "the real openFocus, poll loop and closeFocus. Section R: cards start "
                  "with the server's motion state, the button asks for the opposite of "
                  "what it shows, and changes from another device redraw the card."],
                 ["Page build", verdict("PASS"), "node --check on the full page script."],
                 ["Visual check", verdict("PASS"), "Built page in a browser at 390x760 "
                  "and 760x360 touch emulation: loading message centred; landscape "
                  "picture fills the height, keeps 16:9, black side bars, X visible, "
                  "bar hidden. The kiosk request needs a Home Assistant parent frame: "
                  "covered by the JS test only."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [PageBreak(),
          h1("Best-practices compliance"),
          p(BP_INTRO), p(BP_SCOPE),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST plus duplicate-def scan", verdict("PASS"),
                  "161 top-level definitions, no duplicates."],
                 ["1.2 Syntax errors", "compile() to bytecode", verdict("PASS"), "Gate 1."],
                 ["1.4 Async/await", "focus_frame_base; keeper; file I/O",
                  verdict("PASS"), "The cancelled task cannot add to frame_count "
                  "again. The keeper is kept in _MOTION_TASK and cancelled first at "
                  "shutdown. The toggle writes motion.json through asyncio.to_thread."],
                 ["1.9 Exceptions", "Keeper and persistence", verdict("PASS"),
                  "One camera's error cannot stop the keeper; load catches OSError "
                  "and ValueError only."],
                 ["1.5 Dead code", "Removed page controls", verdict("NOTED"),
                  "Seven JS functions removed; the inline motion block replaced by "
                  "one helper. The server endpoints /snap/focus/tier "
                  "and /snap/focus/profiles now have no caller; kept this release, "
                  "build plan E8."],
                 ["1.5 Line length", "Measured every added line", verdict("NOTED"),
                  "27 of 386 added lines exceed 79 characters: 14 Python (80 to 99), "
                  "13 JS and CSS inside the page strings, matching the file's "
                  "existing style."],
                 ["Part 2 JavaScript", "No await in forEach; listener fallbacks",
                  verdict("PASS"), "matchMedia change listener with addListener "
                  "fallback; postMessage in try/catch."],
                 ["Part 3 HTML", "Markup", verdict("PASS"),
                  "Loading element added; focus image alt emptied because the "
                  "loading text now describes the state."],
                 ["Part 4 CSS", "100dvh with 100vh fallback", verdict("PASS"),
                  "Landscape height uses both, as Part 4 asks."],
                 ["5.1 f-string braces", "Rendered CSS checked", verdict("PASS"),
                  "Integration test asserts single braces in the built page."],
                 ["Outside Parts 1-5: security", "Cross-frame message",
                  verdict("PASS"), "postMessage targets window.location.origin, not *. "
                  "The page sends only the two documented HA message types."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets([
              "<b>The Oak-D camera needs about 20 s to start</b> until Oak-D "
              "2.4.2 is installed (B13).",
              "<b>Recording length</b> is one file per motion event (C12).",
              "<b>Light changes trigger recordings</b> (C13): 06:21 on 2026-09-30, a "
              "9 s clip at the night-to-day switch. JPEG-size method unchanged.",
              "<b>Opening the classic view stops the card stream</b> (B15).",
              "Unchanged: Firefox and H.265 (C10); Skip Non-Reference Frames (B3); "
              "hardware decode falls back to software (B11); a motion recording may "
              "not stop (B1)."])]

    s += [h1("Not verified"),
          *bullets([
              "<b>Kiosk mode on the real HA app.</b> The message format is from HA's "
              "source and the JS test; the phone test is A12.",
              "<b>The image was not built here.</b> The Dockerfile is unchanged from "
              "2.6.4.",
              "<b>The changelog workaround</b> is inferred from Supervisor source.",
              "<b>Motion on real cameras.</b> The loop and keeper ran against a local "
              "test server; a real clip on a Lorex channel is the A12 check."])]

    s += [h1("Acceptance"),
          *bullets([
              "Arm a Lorex channel, close the page, make motion: a clip appears in "
              "/media/anycam and ends about 10 s after the motion stops. The channel "
              "is still armed after a restart.",
              "HA app, Enhanced View, landscape: picture fills the screen, AnyCam title "
              "bar and bottom bar hidden, red X visible. Portrait restores both.",
              "No Resolution, Frame Rate or Auto controls; Classic button present.",
              "\"Loading feed, please wait…\" instead of the gray play icon.",
              "The Oak-D camera plays live after about 20 s in Chrome, including a "
              "second open in the same session.",
              "Cards show \"Loading feed, please wait…\" while starting."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["camera_discovery.py", "Enhanced View JS, markup and CSS; card "
                  "placeholder and error timing; handle_focus_set clears manual tiers "
                  "and records a frame base; X-Focus-Frames header; motion helpers, "
                  "keeper, persistence, idle and throttle changes. Version 2.6.5."],
                 ["config.yaml", "Version 2.6.5."],
                 ["CHANGELOG.md", "2.6.5 entry."],
                 ["CLAUDE.md", "Rule 3: fixed folder name local_camera_discovery, "
                  "with the reason."],
                 ["docs/BUILD_PLAN.md, .html", "A12, B13, C1, C3 updated; B1, B7, B14, "
                  "C2, F8 done; B15, C13, E8, F9 added."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("2.6.5", story(), "anycam_2_6_5_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
