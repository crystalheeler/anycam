"""Audit report for AnyCam 3.8.3 — C27 save and restore, D5 channel cards, B64 camera MAC list.

    python docs/audits/tools/mk_audit_3_8_3.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.8.3 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - C27, D5, B64 - tag 3.8.3" % DATE, S_SUB),
         p("A feature release with the scope CrystalHeeler confirmed on 2026-10-20: C27 (save and restore "
           "the motion and zone settings, designed with CrystalHeeler on 2026-10-16), D5 (a recorder's new "
           "channel cards next to it) and the rest of B64 (the camera MAC prefix list rebuilt from the "
           "IEEE registry).")]

    s += [h1("C27: save and restore the motion and zone settings"),
          table([["Decision (2026-10-16)", "3.8.3"],
                 ["1. Where the copy lives", "Both: every save of motion.json also writes "
                  "anycam-motion-<date>-<time>.json into <recordings>/settings (default /media/anycam/settings), "
                  "newest 20 kept; Save Motion & Zone Settings in the Storage tab downloads one "
                  "(GET /api/motion/copy). A copy with no camera settings, or equal to the newest, is not "
                  "written, so a lost settings file never pushes the good copies out. Written to a .tmp "
                  "file, then renamed. The Storage tab skips the folder."],
                 ["2. What it holds", "Per camera with anything to keep: card key, MAC, channel, name, brand, "
                  "picture size, its own settings (level, cooldown, tail, clip, folder), its zones with "
                  "detection in zones only, and the armed state."],
                 ["3. Restore", "Restore Motion & Zone Settings in the Storage tab. Sources: the settings "
                  "folder; a folder on the HA machine under /media or /share (resolved, then checked against "
                  "the two roots); a folder on the device through the browser's folder picker, where the page "
                  "sends at most the newest 20 copies under 900,000 characters (the add-on's request limit "
                  "is 1 MB). The user never picks a file."],
                 ["5. Matching", "By card key; else by MAC address and channel when the address changed; "
                  "the newest copy that holds the camera wins."],
                 ["6. The dialog", "Every camera with the date found, or \"no saved settings\"; notes a MAC "
                  "match; warns when the picture's width/height changed by more than 2% (zones are kept in "
                  "fractions of the picture). All, None, or any group."],
                 ["7. Apply", "Settings through the same check as the settings dialog, zones through the "
                  "zone check, then the armed state; motion restarts per camera; one save at the end. A "
                  "camera whose saved zones fail the check gets its settings and a reported problem."],
                 ["8. Folder pickers", "Offered where the browser has webkitdirectory; otherwise a note "
                  "points to a folder under /media or /share. The HA phone app has no folder picker in its "
                  "latest release."]],
                [1.6 * inch, 5.0 * inch])]

    s += [h1("D5 and B64"),
          table([["Item", "Change"],
                 ["D5", "After channel enumeration registers new cards, _card_order_after_parent puts them "
                  "right after the recorder's card in the saved order (runtime.json), in channel order; a "
                  "later channel goes after the recorder's existing channel cards; the recorder's key, which "
                  "gains #chN when its channel is set, is renamed in the order. With no saved order one "
                  "starts from the current order."],
                 ["B64 list", "_CAMERA_OUI_VENDORS rebuilt from the IEEE registry (Wireshark's manuf copy, "
                  "2026-10-20) by company: 165 blocks (MA-L, MA-M /28, MA-S /36) of 28 camera-only makers. "
                  "37 of the old 57 prefixes belonged to other companies (Apple, Dell, Intel, HP, Raspberry "
                  "Pi, D-Link, Huawei, Ubiquiti, LG, Texas Instruments and others); 44:A9:2C is a "
                  "Registration Authority prefix that 15 companies share, of which Luxonis holds one /28."],
                 ["B64 matching", "camera_oui_block matches 9, 7 and 6 hex digits, longest first. With the "
                  "downloaded table, which holds only 24-bit prefixes, a camera block inside a "
                  "Registration Authority prefix gets the camera label."],
                 ["B64 left out", "Makers that also make routers, phones or other network devices: Ubiquiti, "
                  "TP-Link, D-Link, Bosch, Sony, Panasonic, Samsung Electronics, Wyze, Ring, EZVIZ, Verkada, "
                  "Kedacom, Speco, Honeywell."],
                 ["B64 not findable", "No registry block: Foscam, Annke, Vstarcam, Wansview, Tenvis, Sricam, "
                  "Zosi, LaView, Hiseeu, Q-See, Pelco, Illustra, Oncam, IndigoVision, VideoIQ. The Hipcam "
                  "family, the Microseven too, uses its Wi-Fi module maker's prefix (Shenzhen Bilian, 42 "
                  "blocks, in many other devices)."],
                 ["B64 test", "tests/build_oui_extract.py makes the list and tests/data/oui_camera_extract.txt "
                  "(the registry lines); test AS10 checks every prefix's owner against its brand, both "
                  "ways."],
                 ["Live effect", "Small, as recorded in the build plan: the list is lookup_oui's fallback "
                  "label before the IEEE table loads, and oui_is_camera's True answer has no live caller."]],
                [1.4 * inch, 5.2 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates, with new contracts for the copy write, the "
                  "folder check and the card placement."],
                 ["Python integration", verdict("PASS"), "772 of 772. New section AS (AS1-AS12); the V2, V3 "
                  "and B55 scan checks use a real Hikvision prefix (the old 1C:C3:16 is Milesight IoT)."],
                 ["JavaScript behaviour", verdict("PASS"), "207 of 207: the copies sent from a device folder, "
                  "the dialog's list, the apply request."],
                 ["Visual (rule 10)", verdict("PASS"), "The built page in the browser pane at 375x812, "
                  "812x375 and 1366x768, the restore dialog with 14 cameras and a long name: inside the "
                  "screen, scrolls inside, buttons in view, no sideways scroll; the Storage bar wraps. The "
                  "check found the rows' checkbox above the name and the style was fixed."],
                 ["Field", verdict("NOTED"), "Not run yet; see the next section."]],
                [1.5 * inch, 0.7 * inch, 4.4 * inch])]

    s += [h1("Not tested"),
          *bullets(["<b>The copy folder on the Pi.</b> Writing under /media/anycam/settings and the rename "
                    "were run on Windows only.",
                    "<b>A folder on a phone.</b> The folder picker was not run in a phone browser; the HA "
                    "app has none in its latest release.",
                    "<b>Channel placement with a real recorder.</b> Run through the function and checked at "
                    "its call site, not through a full channel enumeration.",
                    "<b>Earlier field items.</b> The 3.8.2 gate through go2rtc, the B66 link in the HA app "
                    "and the calibration log are still untested in the field."])]

    s += [h1("Best-practices compliance"),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["6 Security", "Validate at the boundary", verdict("PASS"),
                  "Restore: source allowlist, folder roots, copy size and count, ids as strings; the "
                  "settings and zones go through the existing checks."],
                 ["1.11 Resources", "Atomic write", verdict("PASS"), "Copies are written to .tmp, then renamed."],
                 ["P4 Async", "No blocking I/O on the loop", verdict("PASS"),
                  "Folder reads and saves run in a thread."],
                 ["Rule 10", "Overlays fit", verdict("PASS"), "The new dialog uses the shared .modal styles; "
                  "visual check at three sizes."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"),
                  "Made-up addresses in tests; the registry extract is public data."]],
                W_BP)]

    s += [h1("Known issues"),
          *bullets(["<b>Brands without a MAC block</b> are still found by the camera's answers only.",
                    "<b>Phone live view (B62):</b> waiting for the LIVE-DIAG lines from the field."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_backup.py (new)", "Copy write, prune, read, folder check, matching."],
                 ["anycam_motion.py", "The copy at every save; /api/motion/copy and /api/motion/restore."],
                 ["anycam_page.py, page_script.py", "Two Storage-bar buttons, the restore dialog."],
                 ["anycam_storage.py", "Skips the settings folder."],
                 ["camera_discovery.py, anycam_credentials.py", "Routes; D5 card placement; 3.8.3."],
                 ["anycam_brand.py", "The rebuilt camera list; block matching."],
                 ["anycam_modules.py, Dockerfile, CLAUDE.md", "The new file."],
                 ["verify_release.py", "Four contracts."],
                 ["tests/", "Section AS, page checks, build_oui_extract.py, data/oui_camera_extract.txt."],
                 ["CHANGELOG.md, config.yaml, docs", "3.8.3; build plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.8.3", story(), "anycam_3_8_3_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
