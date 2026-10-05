"""Audit report for AnyCam 3.5.0-rc1.0 — DVR channel count, one camera database.

    python docs/audits/tools/mk_audit_3_5_0_rc1_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.5.0-rc1.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - DVR channel count, one camera database - tag "
                   "3.5.0-rc1.0" % DATE, S_SUB),
         p("Scope: phase 3.5.0 of the build plan: D1 (CrystalHeeler, 2026-10-04: automatic, the "
           "channel limit from what the DVR reports) and D2 (write the plan, add tests, then "
           "merge). Built in the untested run on top of 3.4.0-rc1.0. This build is not "
           "field-tested.")]

    s += [h1("1. D1: the DVR's channel count"),
          table([["Part", "Design"],
                 ["When", "After the password is accepted on a Lorex/Dahua DVR, before the "
                  "channel walk."],
                 ["Ask", "GET /cgi-bin/devVideoInput.cgi?action=getCollect (answer result=N) and "
                  "/cgi-bin/magicBox.cgi?action=getProductDefinition&name=MaxRemoteInputChannels "
                  "(answer table.MaxRemoteInputChannels=N), on the DVR's HTTP address. Basic login, "
                  "then Digest on a 401, with the password just accepted."],
                 ["Use", "The larger of the two answers from 1 to 256 sets the channels walked; "
                  "the count is saved on the card as dvr_channels."],
                 ["No answer", "16 channels, as before, with a log line that says so."]],
                [1.1 * inch, 5.6 * inch]),
          p("Before 3.5.0 every DVR was walked for channels 1 to 16: 7 needless probes on the "
            "8-channel Lorex DVR on test system B, and no cards past 16 on a larger NVR. The "
            "lockout counter of this brand counts failed logins only; the password used here "
            "has just been accepted.")]

    s += [h1("2. D2: one camera database"),
          p("The plan is docs/Camera_DB_Merge_Plan.md, written first. In short:"),
          *bullets([
              "Each CAMERA_DB brand holds its former STREAM_DB entries as \"streams\" (38 brands, "
              "40 entries; Hikvision also holds ezviz, Dahua also holds imou).",
              "STREAM_DB is built from them at start-up, in the old order (\"rank\"), so a tie "
              "between two equally long keywords goes the same way.",
              "The data was moved by a script that compared the built table with the old one: "
              "SHA-256 of its JSON e14a73de... before and after.",
              "The scan's port list is built from the database plus the documented extra ports; "
              "it is the same 54 ports today."])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates; 16 files; build inputs "
                  "checked online; the CAMERA_DB throttle field check."],
                 ["Python integration", verdict("PASS"), "542 of 542; 10 new (AF1 to AF6), "
                  "including a fake DVR that asks for Digest login. Z7 now says the DVR does "
                  "not report a count, and still walks 2 to 16."],
                 ["JavaScript behaviour", verdict("PASS"), "158 of 158, unchanged."],
                 ["Undefined names", verdict("PASS"), "16 files, 0."],
                 ["Not run", verdict("NOTED"), "No field test (CrystalHeeler's order). The "
                  "image was not built. The Lorex DVR's real answer to the two requests is not "
                  "known; the parsing follows Dahua's HTTP API format."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, Parts 1 to 6. "
            "Rows this change touches; the rest carry from the 3.4.0-rc1.0 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST, contracts, undefined names",
                  verdict("PASS"), "16 files."],
                 ["1.16 Refactoring", "Behaviour kept", verdict("PASS"),
                  "STREAM_DB byte for byte; port list equal; Z and Y tests unchanged."],
                 ["1.17 Standard library", "Data", verdict("PASS"),
                  "No new dependency; one source for each fact."],
                 ["1.18 Testing", "Tests first", verdict("PASS"),
                  "Plan, then tests, then the merge, as decided."],
                 ["6.1 Security", "DVR answer", verdict("PASS"),
                  "The count is bounded (1 to 256); the password goes only to the DVR."],
                 ["Rule 9 (privacy)", "New text", verdict("PASS"),
                  "Tests use made-up addresses only."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets([
              "D1: no sample yet of what a known-empty channel sends; such a channel can get a card.",
              "<b>B6:</b> the Microseven waits until CrystalHeeler unlocks it.",
              "<b>C21:</b> the classic engine stays as the fallback."])]

    s += [h1("Acceptance (field test)"),
          *bullets([
              "Test system B: after the password, the log says the DVR reports 8 channels, and "
              "only channels 2 to 8 are walked.",
              "The channel cards are the same as in 3.4.0-rc1.0.",
              "A scan finds the same cameras as before."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["camera_db.py", "Stream paths on the brands; the STREAM_DB builder."],
                 ["anycam_credentials.py", "_dvr_channel_count, _dvr_parse_count; the walk uses "
                  "the count."],
                 ["anycam_scan.py", "The port list built from the database."],
                 ["docs/Camera_DB_Merge_Plan.md", "New."],
                 ["tests/", "Section AF; Z7 with no reported count."],
                 ["CHANGELOG.md, config.yaml, CLAUDE.md, docs", "3.5.0-rc1.0; build plan."]],
                W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.5.0-rc1.0", story(), "anycam_3_5_0_rc1_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
