"""Audit report for AnyCam 3.8.0 — stable release of the 3.8.0-rc line.

    python docs/audits/tools/mk_audit_3_8_0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from audit_lib import (DATE, S_SUB, S_TITLE, W_BP, W_FILES,
                       build, bullets, h1, p, table, verdict)
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph


def story():
    s = [Paragraph("AnyCam 3.8.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - stable 3.8.0 - tag 3.8.0" % DATE, S_SUB),
         p("Stable release of the 3.8.0-rc1 to 3.8.0-rc3 line. The code is identical to "
           "3.8.0-rc3; only the version string changes (3.8.0-rc3 to 3.8.0). CrystalHeeler "
           "field-tested rc3 on both test systems on 2026-10-11 (Chrome and the mobile Home "
           "Assistant app) and reported no bugs, with a faster initial scan. Scope: the version "
           "bump only.")]

    s += [h1("What 3.8.0 delivers over 3.7.5"),
          table([["Area", "Change"],
                 ["Scan redesign (B54)", "ONVIF once with no login first, then brand paths; the scan "
                  "stops at the first login prompt and leaves an identified camera's other ports "
                  "alone. Faster, quieter scans."],
                 ["Non-camera skip (B55)", "The scan skips a host with a printer port open, or whose "
                  "MAC maker is a known non-camera brand; multicast hosts are exempt."],
                 ["Appliance login", "Appliance-camera cards keep their notes and add a login prompt "
                  "(RTSP walk, then MJPEG and HLS on the web ports), with a clear message when "
                  "nothing is reachable."],
                 ["Password entry", "Up to two streams by the 0/1/2 rule; stop at the first rejected "
                  "login; a sub-stream per DVR channel."],
                 ["Fixes", "Login no longer lost on reset-prone cameras; DVR channels enumerate from "
                  "the RTSP realm; slow login re-syncs; zone drawing works from the Cog menu; "
                  "appliance notes wording corrected."]],
                [1.6 * inch, 5.0 * inch])]

    s += [h1("Field verification (rc3, 2026-10-11)"),
          *bullets(["<b>B55:</b> the printer host was logged skipped (\"printer port open — not a "
                    "camera, not probed\").",
                    "<b>Appliance login:</b> the path was exercised and reported \"no local camera "
                    "reachable\" cleanly — the designed outcome for an appliance with no local "
                    "stream.",
                    "<b>DVR:</b> eight devices found, six streaming; channels enumerated and "
                    "recording.",
                    "<b>No errors or tracebacks</b> in either field log; remaining warnings are "
                    "camera-side (Smart Codec still picture, Hipcam lockout) or cosmetic (an HA "
                    "base-image deprecation notice)."])]

    s += [h1("Tests run"),
          table([["Suite", "Result", "Scope"],
                 ["Release gate", verdict("PASS"), "Eight gates at 3.8.0."],
                 ["Python integration", verdict("PASS"), "679 of 679."],
                 ["JavaScript behaviour", verdict("PASS"), "193 of 193."],
                 ["Field", verdict("PASS"), "rc3 field-tested on both systems, Chrome and the mobile "
                  "HA app; no bugs reported."]],
                [1.5 * inch, 0.7 * inch, 4.5 * inch])]

    s += [h1("Best-practices compliance"),
          table([["Standard", "Check applied", "Result", "Note"],
                 ["5 (versioning)", "Full version string", verdict("PASS"),
                  "3.8.0 in code, config and changelog."],
                 ["8 (regressions)", "Our code first", verdict("PASS"),
                  "All 3.8.0 regressions against 3.7.5 were fixed within the rc line."],
                 ["1.18 Testing", "Coverage", verdict("PASS"), "Full suite green; rc3 field-tested."],
                 ["Rule 9 (privacy)", "Release text", verdict("PASS"), "No real names or addresses in "
                  "the changelog or this audit."]],
                W_BP)]

    s += [h1("Known issues"),
          *bullets(["<b>Smart Codec:</b> a camera using H.264+/H.265+ may show a still picture until "
                    "Smart Codec is off or the I-frame interval equals the frame rate.",
                    "<b>Hipcam/Microseven:</b> a locked camera needs a power cycle; untested on an "
                    "unlocked Microseven camera.",
                    "<b>OPTIONS-401 scan stop:</b> a camera that answers 401 at OPTIONS is stopped at "
                    "DESCRIBE, not yet at the first OPTIONS 401. Harmless."])]

    s += [h1("Files changed"),
          table([["File", "Change"],
                 ["config.yaml, camera_discovery.py", "Version 3.8.0-rc3 to 3.8.0."],
                 ["CHANGELOG.md", "Consolidated 3.8.0 entry."],
                 ["docs", "Build plan; this audit."]], W_FILES)]
    return s


if __name__ == "__main__":
    path = build("3.8.0", story(), "anycam_3_8_0_audit_report.pdf")
    print("wrote", path.name, round(path.stat().st_size / 1024, 1), "KB")
