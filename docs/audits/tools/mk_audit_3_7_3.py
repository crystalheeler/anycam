"""Audit report for AnyCam 3.7.3 — real first frames, WebRTC retry, classic view pictures, request rate.

    python docs/audits/tools/mk_audit_3_7_3.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.7.3 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - real first frames, WebRTC retry, classic view pictures, "
                   "request rate - tag 3.7.3" % DATE, S_SUB),
         p("Scope confirmed by CrystalHeeler on 2026-10-06: version 3.7.3 with B41 (cards and "
           "Enhanced View), B42 (option A), B43, B44, B45 and B37's small-stream run. Built and "
           "pushed on CrystalHeeler's order; not published. Not field-tested.")]

    s += [h1("Changes"),
          table([["#", "Cause (from the field tests)", "Change"],
                 ["B41", "LibreWolf, Oak-D card (profile 244): played true, readyState 1, 0 frames, "
                  "width 1280, black for good. Firefox Enhanced View: \"Live (MSE): ... 0 fps\" for "
                  "90 s; the 'playing' event counted as a frame.", "_videoHasFrame: a width and "
                  "decoded frames (or readyState 2 or more), for cards and Enhanced View."],
                 ["B42", "Five still-picture cards asked every 125 ms (about 40 requests a second); "
                  "each channel gives about 0.8 pictures a second.", "?after=N holds the answer up to "
                  "2 s until a newer picture; the page fetches blobs and sends the number it shows."],
                 ["B43", "A fresh install's log named no version.", "\"AnyCam 3.7.3 on :8099\"."],
                 ["B44", "Classic view, channels 7 and 2: 1,550 and 1,850 identical JPEGs; ffmpeg "
                  "\"More than 1000 frames duplicated\".", "-fps_mode passthrough: each decoded "
                  "picture once."],
                 ["B45", "Firefox: the cards play H.265 over WebRTC (video only); Enhanced View asks "
                  "for sound, gets MSE, and shows nothing.", "No picture in 15 s and not on WebRTC: a "
                  "second player, WebRTC and video only, before the classic view."],
                 ["B37", "All three hardware ways failed (\"Decode fail\") with 127 and 211 MB of "
                  "CMA free.", "A fifth run: the hardware decoder on the smallest H.265 stream."]],
                [0.5 * inch, 3.1 * inch, 3.1 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 17 files; build inputs checked "
                  "online."],
                 ["Python integration", verdict("PASS"), "617 of 617; section AL new (14 checks)."],
                 ["JavaScript behaviour", verdict("PASS"), "190 of 190; B41, B45 and [T] (B42) new."],
                 ["Not run", verdict("NOTED"), "No field test. WebRTC in Firefox was inferred from "
                  "socket timing and VideoRTC's code, not measured. -fps_mode ran only against a "
                  "stand-in ffmpeg; the Pi's ffmpeg 5.1.9 documents the option."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>. Rows this change "
            "touches; the rest carry from the 3.7.0 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.3 Async", "Waits", verdict("PASS"),
                  "The hold sleeps 50 ms at a time and ends at 2 s."],
                 ["2 JavaScript", "Async hygiene", verdict("PASS"),
                  "A stopped snapshot run drops its answer (run number)."],
                 ["1.12 Logging", "Truth", verdict("PASS"),
                  "The version at start-up; the WebRTC retry has its own line."],
                 ["1.18 Testing", "New tests", verdict("PASS"), "14 server and 20 page checks."],
                 ["6.1 Security", "Input at the boundary", verdict("PASS"),
                  "after must be digits; /api/live_fail takes three places only."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"), "Made-up addresses only."]],
                W_BP)]

    s += [h1("Known issues"),
          *bullets(["<b>B37:</b> hardware HEVC decode fails on the Pi; software is used.",
                    "<b>B33:</b> one Amcrest camera can get two cards; under investigation.",
                    "<b>B32, B29:</b> wait for CrystalHeeler's tests."])]

    s += [h1("Acceptance (field test)"),
          *bullets(["LibreWolf, Oak-D add-on 3.0.0: the Oak-D card shows still pictures within 15 s.",
                    "Firefox: Enhanced View on a Lorex channel plays after the WebRTC retry.",
                    "LibreWolf: the classic view of a Lorex channel moves.",
                    "Browser console: about one request a second per still-picture card.",
                    "The start-up line names 3.7.3."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["page_script.py", "B41, B42 (startSnap), B45."],
                 ["anycam_snap.py", "B42 hold and header; B44; B37 small stream."],
                 ["anycam_go2rtc.py", "B45 log line."],
                 ["camera_discovery.py", "B43; version 3.7.3."],
                 ["tests/", "Section AL; page checks."],
                 ["CHANGELOG.md, config.yaml, docs", "3.7.3; build plan."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.7.3", story(), "anycam_3_7_3_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
