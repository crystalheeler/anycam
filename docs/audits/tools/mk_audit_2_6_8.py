"""Audit report for AnyCam 2.6.8 — wrong home location check (C16)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 2.6.8 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - wrong home location check - tag 2.6.8" % DATE, S_SUB),
         p("2.6.8 carries one item, build plan C16. Scope confirmed by CrystalHeeler on "
           "2026-10-02: C16 only, the location re-read every 6 hours, push and "
           "publish when done. camera_discovery.py: 44 lines added, 7 removed.")]

    s += [h1("Evidence"),
          table([["Fact", "Value", "Source"],
                 ["Notification on the test system B", "'Still in night mode (IR) an hour "
                  "after sunrise (00:42)'", "CrystalHeeler's screenshot, 2026-10-02"],
                 ["Home Assistant home location", "Amsterdam (the default), time zone "
                  "Central", "CrystalHeeler's screenshot of Home information"],
                 ["Amsterdam sunrise, 2026-10-02", "05:42 UTC = 00:42 Central",
                  "AnyCam's own _sun_events_utc"],
                 ["Sunrise spread inside the Central zone, 21 December",
                  "06:41 (Pensacola) to 08:23 (New York): 1 h 42 min",
                  "Same function; why the time zone cannot replace the location"]],
                [2.3 * inch, 2.4 * inch, 2.0 * inch])]

    s += [h1("Design"),
          table([["Part", "Design"],
                 ["Rule", "A time zone's own longitude is 15 degrees for each hour of its "
                  "standard offset from UTC. A home location more than 52.5 degrees "
                  "(3.5 h) from it is wrong. 3.5 h accepts western China (75.9 E in "
                  "UTC+8) and western Spain."],
                 ["Time zone", "The time_zone name from Home Assistant's /api/config, "
                  "through Python's zoneinfo, standard time (summer time removed). If "
                  "the name is missing or unknown: the add-on's own zone, which the "
                  "Supervisor sets."],
                 ["When wrong", "No location is kept, so no sunrise or sunset events and "
                  "no notifications. One log warning. The cog panel's note line shows "
                  "the reason. Night boost itself works from the picture's colour and "
                  "is not affected."],
                 ["Refresh", "Every 6 hours (was 12); every 5 minutes until Home "
                  "Assistant answers."],
                 ["Also", "The cog's recording-folder help said SFTP and FTP were "
                  "planned for 2.6.8; now 'a later version' (C14 is parked)."]],
                [1.4 * inch, 5.3 * inch])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Seven gates, 46 contracts (1 new, 1 "
                  "updated), 19 settings in agreement, Dockerfile inputs online."],
                 ["Python integration", verdict("PASS"), "278 of 278, 13 new. Amsterdam "
                  "with a Central zone rejected; New York, Chicago, Amsterdam, Sydney, "
                  "Honolulu, western China, Spain, Fiji and Samoa accepted; standard "
                  "time used; unknown zone name; the refresh: check off, one warning, "
                  "cog note, 6 h; no second warning; corrected location turns it on."],
                 ["JavaScript behaviour", verdict("PASS"), "110 of 110. No page change "
                  "except one help sentence."],
                 ["Not run", verdict("NOTED"), "zoneinfo needs the time zone database "
                  "in the add-on image; it was not checked there (no Docker on the "
                  "build PC). If it is missing, the add-on's own zone is used. Not "
                  "tested against a real Home Assistant."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          p("Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>, Parts 1 to "
            "6. Only the rows this change touches are scored; the rest carry from the "
            "2.6.7 audit."),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["1.1 Truncated function", "AST, duplicates, 46 contracts",
                  verdict("PASS"), "No duplicates."],
                 ["1.2 Syntax", "compile()", verdict("PASS"), "Gate 1."],
                 ["1.6 Function design", "New helpers", verdict("PASS"),
                  "_tz_std_offset_h and _ha_location_plausible: one job each."],
                 ["1.7 Mutability", "Shared state", verdict("NOTED"),
                  "One more key in the module-level _HA_LOC_STATE dict."],
                 ["1.10 Exceptions", "Broad catch", verdict("NOTED"),
                  "_tz_std_offset_h catches Exception: zoneinfo raises several types "
                  "(missing database, bad key, bad type) and every one has the same "
                  "safe fallback."],
                 ["1.12 Logging", "New line", verdict("PASS"),
                  "One WARNING per mismatch, with the location, the zone and the fix."],
                 ["1.17 Stdlib first", "Time zones", verdict("PASS"),
                  "zoneinfo is in the standard library; no package added."],
                 ["1.18 Testing", "Behavioural tests", verdict("NOTED"),
                  "388 checks, still in the session scratchpad; E7 in 3.0.0-rc1.0 "
                  "moves them into the repository."],
                 ["6.1 Security", "Input from Home Assistant", verdict("PASS"),
                  "Latitude and longitude converted with float(); the zone name goes "
                  "only to zoneinfo, inside the catch."],
                 ["6.3 Version control", "Commits and gate", verdict("PASS"),
                  "One release commit; gate run before it; tag after."]],
                W_BP)]

    s += [h1("Known issues carried forward"),
          *bullets([
              "A location that is wrong but inside the right time zone is not detected.",
              "The night and day colour thresholds are estimates (A13).",
              "The Microseven on the test system A needs a power cycle (B6).",
              "The Lorex DVR beeping on the test system B: cause unknown; waiting for the "
              "add-on log and the DVR's event entry.",
              "Skip Non-Reference Frames breaks H.264 cameras (B3)."])]

    s += [h1("Acceptance"),
          *bullets([
              "With the Amsterdam default: the log shows 'does not match its time zone' "
              "once, the cog shows the note, and no sunrise or sunset notifications "
              "arrive.",
              "After setting the real location: within 6 hours (or after a restart) "
              "the log shows 'Home Assistant location received' with local sunrise "
              "and sunset times."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["camera_discovery.py", "Location check, 6 h refresh, cog note, help "
                  "text. Version 2.6.8."],
                 ["config.yaml", "Version 2.6.8."],
                 ["verify_release.py", "1 new contract, 1 updated."],
                 ["docs", "Build plan: C16 done, C14 parked, section E planned for "
                  "3.0.0-rc1.0."],
                 ["CHANGELOG.md", "2.6.8 entry, short format."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("2.6.8", story(), "anycam_2_6_8_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
