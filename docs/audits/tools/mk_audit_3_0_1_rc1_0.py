"""Audit report for AnyCam 3.0.1-rc1.0 — appliance cameras, H.265 in the browser, late pictures.

    python docs/audits/tools/mk_audit_3_0_1_rc1_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.0.1-rc1.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - appliance cameras, H.265 in the browser, late "
                   "pictures - tag 3.0.1-rc1.0" % DATE, S_SUB),
         p("Scope: phase 3.0.1 of the build plan, agreed with CrystalHeeler on 2026-10-03, in "
           "this order: B2, C10, B20, B23, B24, B26, B12, B8, F10, C11 (also closes B3), C20. "
           "CrystalHeeler ordered the phases 3.0.1 to 3.7.0 built one after another without a "
           "field test between them, each as its own commit, tag and zip, so that an earlier "
           "build can be installed when a later one fails. This build is not field-tested.")]

    s += [h1("1. Cameras in appliances (B2)"),
          p("Two cameras on test system B never got a card: a litter box camera and a robot "
            "vacuum. Both opened no camera port to the scan. Their makers' MAC blocks "
            "identify them."),
          table([["MAC block", "Card name", "Where the video plays"],
                 ["04:A1:6F:1 (28 bits, iENSO)", "Appliance camera (iENSO module)",
                  "the appliance maker's app"],
                 ["00:AE:F7 (Dreame)", "Dreame robot vacuum", "the Dreamehome app"]],
                [2.2 * inch, 2.4 * inch, 2.1 * inch]),
          *bullets([
              "The scan keeps each live device's MAC address from the ARP sweep "
              "(LIVE_HOST_MACS), and the port scan's MAC as a second source.",
              "Each live device with no camera port is named in the log, with its MAC and "
              "maker. Before, it was dropped without a line.",
              "An appliance camera with no card gets an information card (display "
              "'appliance', status 'info'). One that asks for a login keeps the usual login "
              "card, with a note.",
              "AnyCam does not try the appliances' default passwords or known flaws."])]

    s += [h1("2. H.265 and the browser (C10)"),
          p("Firefox plays H.265 only on Windows, through Windows itself: it needs Microsoft's "
            "HEVC Video Extensions and a graphics chip that decodes H.265. A page cannot "
            "install a codec. The page now asks the browser "
            "(MediaSource.isTypeSupported with an hvc1 and an hev1 codec string)."),
          table([["Case", "What happens"],
                 ["Enhanced View, H.265 stream, browser plays H.265", "As before."],
                 ["... browser cannot play H.265, camera has an H.264 stream",
                  "Live view plays the H.264 stream, with a note."],
                 ["... no H.264 stream", "The classic view, with a message: HEVC Video "
                  "Extensions for Firefox on Windows, else Chrome or Edge."],
                 ["Live card", "The page adds ?h265=0; the server skips H.265 streams "
                  "(_go2rtc_card_source(h265=False)), or answers why none is left."],
                 ["A live-view error", "Readable text; go2rtc's text stays in the console."]],
                [3.0 * inch, 3.7 * inch])]

    s += [h1("3. Late pictures and insects (B20)"),
          table([["Part", "Cause", "Change"],
                 ["Cards minutes late", "The Pi decodes 3840x2160 H.265 in software slower "
                  "than the DVR sends it, and works through a backlog.",
                  "A 4K H.265 card decodes keyframes only (-skip_frame nokey): about one "
                  "picture a second, always current."],
                 ["Old picture on a card", "The snapshot endpoint served the last picture at "
                  "any age.", "A picture older than 10 s is not served (503); the card says "
                  "'Loading feed'."],
                 ["Log flood", "A failing stream was retried every 10 s with a warning each "
                  "time.", "10, 20, 40, 80, 160, then 300 s; one warning, then debug lines, "
                  "and one line at recovery."],
                 ["Insect echo", "On the snapshot path a picture compared with an insect "
                  "picture showed a change.", "A picture must also differ from the picture "
                  "before the reference."]],
                [1.3 * inch, 2.6 * inch, 2.8 * inch]),
          p("Limit: a one-picture insect on the snapshot path still records, as in 3.0.0. "
            "The live stream path, which most cameras use, already needs two changed "
            "pictures.")]

    s += [h1("4. The other items"),
          table([["#", "Change"],
                 ["B23", "Manual add: the protocol name is restored to 'WebRTC' after it is "
                  "made upper case. WS-RTSP cards get display 'wsrtsp'."],
                 ["B24", "The stream-table check paces by the camera's brand entry "
                  "(_brand_throttle_seconds), not by a key no STREAM_DB entry has. The pacing "
                  "label holds no password."],
                 ["B26", "The log link asks /api/self for the add-on's ID, from the "
                  "Supervisor's /addons/self/info, cached; 'local_camera_discovery' when "
                  "there is no Supervisor."],
                 ["B12", "VAAPI is used only when the render device exists and ffmpeg opens "
                  "it (a 0.1 s test encode), tested once per start."],
                 ["B8", "Progress is measured in work done per stage (hosts, ports, ONVIF "
                  "devices), weighted by the last scan's stage times. The page gets elapsed "
                  "time and time left."],
                 ["F10", "On the first start AnyCam sets ingress_panel and auto_update through "
                  "/addons/self/options, once. A failed call sets no marker."],
                 ["C11, B3", "Five settings removed with their code: Low FPS, Skip "
                  "Non-Reference Frames (invalid 'nonref' value, B3), Limit Threads, Stagger "
                  "Poll, Fast Stream Start (and the hardware preheater it ran). The Supervisor "
                  "ignores saved values for removed options."],
                 ["C20", "The Classic button in Enhanced View and its code removed. The "
                  "classic view still starts when live view cannot play."]],
                [0.8 * inch, 5.9 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 14 files; build inputs "
                  "checked online."],
                 ["Python integration", verdict("PASS"), "471 of 471; 23 new in section AA, "
                  "and updates in X4, Y2, Y3, Y9, Z4, Z9 and G4."],
                 ["JavaScript behaviour", verdict("PASS"), "118 of 118, with a C10 section "
                  "and the C20 check."],
                 ["Undefined names", verdict("PASS"), "14 files, 0."],
                 ["Not run", verdict("NOTED"), "No field test (CrystalHeeler's order for "
                  "this run). The image was not built. ffmpeg, the Supervisor, the scan tools "
                  "and the browser are stand-ins in the tests; -skip_frame nokey and the "
                  "VAAPI test encode did not run on a Pi."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, Parts 1 to 6. "
            "Rows this change touches; the rest carry from the 3.0.0-rc1.5 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST, contracts, undefined names",
                  verdict("PASS"), "14 files."],
                 ["1.3 Runtime errors", "Names", verdict("PASS"),
                  "B23 and B24 fixed; removed names gone from every NEEDS list."],
                 ["1.4 Async", "Subprocess and sleep", verdict("PASS"),
                  "The VAAPI test and the Supervisor calls are async; no blocking I/O."],
                 ["1.10 Exceptions", "Failure paths", verdict("PASS"),
                  "Supervisor errors return None; F10 retries at the next start."],
                 ["1.12 Logging", "Volume", verdict("PASS"),
                  "One warning per failing stream instead of one per retry."],
                 ["1.18 Testing", "New tests", verdict("PASS"), "23 server and 7 page checks."],
                 ["2.x JavaScript", "Async, feature detection", verdict("PASS"),
                  "isTypeSupported in try/catch; result cached."],
                 ["6.1 Security", "Credentials", verdict("PASS"),
                  "Pacing label strips the password; no default passwords tried on "
                  "appliances."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"),
                  "Tests use made-up addresses and MACs; MAC blocks are makers' public "
                  "prefixes."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets([
              "<b>B25:</b> a camera's streams are read only at password entry (3.1.0).",
              "<b>B6:</b> the Microseven waits until CrystalHeeler unlocks it.",
              "<b>B20:</b> a one-picture insect on the snapshot path still records."])]

    s += [h1("Acceptance (field test)"),
          *bullets([
              "Test system B: the litter box camera and the robot vacuum have information "
              "cards; the log names each skipped device.",
              "Firefox on Windows without HEVC Video Extensions: the Hikvision PTZ plays its "
              "H.264 stream, or shows the message.",
              "Test system B: 4K channel cards stay current (under 10 s behind).",
              "The scan bar moves steadily and shows time left.",
              "The log link opens the add-on's log page.",
              "After the update, Show in Sidebar and Auto update are on."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_scan.py", "B2 (MACs, dropped-host log, information cards), B8."],
                 ["anycam_brand.py", "B2: appliance MAC blocks."],
                 ["anycam_snap.py", "B20 keyframes only and the 10 s limit; C11 removals."],
                 ["anycam_motion.py", "B20 back-off and second look."],
                 ["anycam_go2rtc.py", "C10: card source without H.265."],
                 ["anycam_credentials.py", "B23, B24."],
                 ["anycam_focus.py, anycam_page.py", "C11, C20 removals."],
                 ["camera_discovery.py", "B26, F10, B12; C11 options; version 3.0.1-rc1.0."],
                 ["page_script.py", "C10, B26, C20, B2 card."],
                 ["config.yaml, run.sh, translations/en.yaml", "C11 options removed; version."],
                 ["tests/", "Section AA; updated checks; a MediaSource fake."],
                 ["CHANGELOG.md, docs", "3.0.1-rc1.0; build plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.0.1-rc1.0", story(), "anycam_3_0_1_rc1_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
