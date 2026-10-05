"""Audit report for AnyCam 3.1.0-rc1.0 — live MJPEG and wide cards, stream refresh, card order.

    python docs/audits/tools/mk_audit_3_1_0_rc1_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.1.0-rc1.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - live MJPEG and wide cards, stream refresh, card "
                   "order - tag 3.1.0-rc1.0" % DATE, S_SUB),
         p("Scope: phase 3.1.0 of the build plan (cards): B25, C19, D3, with CrystalHeeler's "
           "decisions of 2026-10-04: B25 automatic; C19 as proposed (MJPEG live, wide streams "
           "live on computers, still pictures on phones); D3 one order for every viewer. "
           "Built in the untested run CrystalHeeler ordered on 2026-10-04, on top of "
           "3.0.1-rc1.0. This build is not field-tested.")]

    s += [h1("1. Live MJPEG cards (C19)"),
          p("New file anycam_mjpeg.py. A card for a camera with an HTTP MJPEG stream asks "
            "/api/go2rtc/card as before; the answer is kind 'mjpeg' and a WebSocket address. "
            "go2rtc is not involved, so this works while go2rtc starts."),
          table([["Part", "Design"],
                 ["One camera connection", "_MjpegHub: one connection per camera, shared by "
                  "every card that shows it; closed 5 s after the last card. Reconnects at 2 s, "
                  "doubling to 30 s. The brand cooldown (Microseven) applies before each "
                  "connection."],
                 ["Login", "Basic, then Digest on a 401. The Digest code moved out of "
                  "http_snap_loop to _http_digest_header, unchanged."],
                 ["Pictures", "_JpegSplitter cuts JPEGs from the bytes by walking each JPEG's "
                  "segments, so a wrong multipart boundary does not matter and an EXIF "
                  "thumbnail's end marker does not cut a picture short."],
                 ["To the page", "One WebSocket message per JPEG. Not a long multipart HTTP "
                  "response: the page builder notes that Home Assistant's ingress proxy ends "
                  "those; the go2rtc WebSocket passes ingress without trouble."],
                 ["In the page", "The card shows the newest picture. A picture that arrives "
                  "while one loads replaces the waiting one, so the card never falls behind. "
                  "Each blob is freed once shown. A hidden page or an open Enhanced View "
                  "closes the socket."],
                 ["Stop", "A refused password, a missing page or a single-picture URL stops "
                  "the stream with a text message; the card shows still pictures and tries "
                  "live again after 5 min."]],
                [1.5 * inch, 5.2 * inch])]

    s += [h1("2. Wide streams on computers (C19)"),
          p("_go2rtc_card_source(wide=True) plays the smallest stream even when it is wider "
            "than 1,920. The page sends wide=1 unless it runs on a phone "
            "(navigator.userAgentData.mobile, else the user agent). The Pi passes the bytes; "
            "the computer decodes them. The DVR sub-stream rule still comes first.")]

    s += [h1("3. Saved streams read again (B25)"),
          table([["Part", "Design"],
                 ["How", "_streams_refresh runs api_set_credentials with the saved password, "
                  "the same step as entering it by hand, which paces rate-limited cameras."],
                 ["When", "A card request that fails for a saved-stream reason (no stream small "
                  "enough, no stream that can play live, an MJPEG or MPEG-4 stream, no URL); "
                  "or 5 failed stream starts in the snapshot loop."],
                 ["Limits", "At most once every 6 hours for each camera. Not for a DVR channel "
                  "card, an information card, or a camera with no saved password."],
                 ["Failure", "The saved streams stay; one warning line."]],
                [1.2 * inch, 5.5 * inch])]

    s += [h1("4. Card order (D3)"),
          *bullets([
              "A drag handle at the left of each card's name; pointer events, so a mouse "
              "and a finger both work. The card moves once, at the drop: moving a card "
              "restarts its live stream.",
              "The page posts the order of card keys (ip:port, #chN for a DVR channel) to "
              "/api/card_order. The add-on checks it (a list of up to 500 strings of up to "
              "200 characters), drops duplicates and keeps it in runtime.json.",
              "/api/cameras returns the cameras in that order; cameras not in it follow in "
              "their own order. renderGrid puts the cards in that order, and moves nothing "
              "when they already are."])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 15 files; build inputs "
                  "checked online."],
                 ["Python integration", verdict("PASS"), "494 of 494; 23 new in section AB, "
                  "including a real local camera server for the MJPEG relay."],
                 ["JavaScript behaviour", verdict("PASS"), "132 of 132; new sections C19 "
                  "and D3."],
                 ["Undefined names", verdict("PASS"), "15 files, 0."],
                 ["Not run", verdict("NOTED"), "No field test (CrystalHeeler's order for "
                  "this run). The image was not built. Not checked: the MJPEG relay through "
                  "Home Assistant's ingress, a real MJPEG camera, the drag on a touch screen, "
                  "and a wide stream decoded by a real browser."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, Parts 1 to 6. "
            "Rows this change touches; the rest carry from the 3.0.1-rc1.0 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST, contracts, undefined names",
                  verdict("PASS"), "15 files."],
                 ["1.4 Async", "Tasks and waits", verdict("PASS"),
                  "Hub and viewer tasks cancelled on close; no blocking call in the relay."],
                 ["1.10 Exceptions", "Network errors", verdict("PASS"),
                  "Client errors caught at the connection; the card is told in words."],
                 ["1.11 Resources", "Connections, blobs", verdict("PASS"),
                  "One camera connection per camera; closed after the last card; blobs freed."],
                 ["1.16 Refactoring", "Digest helper", verdict("PASS"),
                  "Moved to module level unchanged; http_snap_loop calls it."],
                 ["1.18 Testing", "New tests", verdict("PASS"), "23 server and 14 page checks."],
                 ["2.x JavaScript", "Async, events", verdict("PASS"),
                  "Pointer capture; handlers removed at the drop; no await in forEach."],
                 ["6.1 Security", "Input at the boundary", verdict("PASS"),
                  "/api/card_order validates type, count and length. The MJPEG socket serves "
                  "only a camera that has an MJPEG stream; passwords never reach the page."],
                 ["No import cycles", "Imports", verdict("PASS"),
                  "anycam_go2rtc takes _mjpeg_source and _streams_refresh through the main "
                  "file's NEEDS."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"),
                  "Tests use made-up addresses only."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets([
              "An MJPEG stream over RTSP still shows still pictures (go2rtc has no MJPEG "
              "output loaded; adding it changes the go2rtc security model).",
              "Another viewer sees a new card order at the next page load.",
              "<b>B6:</b> the Microseven waits until CrystalHeeler unlocks it.",
              "<b>C21:</b> the classic engine stays as the fallback."])]

    s += [h1("Acceptance (field test)"),
          *bullets([
              "Test system A: the Microseven card (HTTP MJPEG) plays live, about as fast "
              "as the camera sends; one camera connection with two browsers open.",
              "Test system A: the Hikvision PTZ card plays live on a computer; on a phone it "
              "shows pictures.",
              "After a camera's stream settings change, its card plays live again within one "
              "card retry (5 min) without entering the password.",
              "A card dragged to a new place stays there after a reload, and another "
              "browser shows the same order."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["anycam_mjpeg.py", "New: the MJPEG hub, the JPEG splitter, the WebSocket."],
                 ["anycam_go2rtc.py", "wide=1; the MJPEG answer; the B25 trigger."],
                 ["anycam_credentials.py", "_streams_refresh."],
                 ["anycam_snap.py", "_http_digest_header; the B25 trigger at 5 failed starts."],
                 ["camera_discovery.py", "Card order; routes; imports; version 3.1.0-rc1.0."],
                 ["page_script.py, anycam_page.py", "MJPEG card, phone check, drag and order; CSS."],
                 ["anycam_modules.py, Dockerfile", "The new file."],
                 ["tests/", "Sections AB, C19, D3."],
                 ["CHANGELOG.md, config.yaml, docs", "3.1.0-rc1.0; module lists; build plan."]],
                W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.1.0-rc1.0", story(), "anycam_3_1_0_rc1_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
