"""Regenerate AnyCam audit report PDFs for 2.6.0, 2.6.1 and 2.6.2.

Supersedes the 26 Sep 2026 revisions of the 2.6.0 and 2.6.1 reports.

Correction applied: those revisions cited compliance rows against
"Part 6 - Project Engineering" of docs/AnyCam_Coding_Best_Practices.md.
That part does not exist. The document contains Parts 1 to 5 and a
Sources Summary. CLAUDE.md describes a Part 6, so the working agreement
and the document disagree. The checks behind those rows were performed
and passed; only the attribution was wrong. They are relabelled here as
sitting outside the documented standards.

ASCII only in body text. ReportLab's built-in Helvetica uses
WinAnsiEncoding and renders missing glyphs as black boxes.
"""
import datetime
import pathlib

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (PageBreak, Paragraph, SimpleDocTemplate,
                                Table, TableStyle)

OUT_DIR = pathlib.Path(__file__).resolve().parent.parent      # docs/audits
DATE = datetime.date.today().strftime("%d %B %Y")

INK = colors.HexColor("#1A1F27")
DIM = colors.HexColor("#5C6672")
RULE = colors.HexColor("#C6CDD6")
BAND = colors.HexColor("#EEF1F4")
ACCENT = colors.HexColor("#0F5F70")
PASS_C = colors.HexColor("#2F6B41")
WARN_C = colors.HexColor("#8A5510")
CRIT_C = colors.HexColor("#9C3423")

_ss = getSampleStyleSheet()
S_TITLE = ParagraphStyle("t", parent=_ss["Title"], fontName="Helvetica-Bold",
                         fontSize=20, leading=24, textColor=INK,
                         alignment=TA_LEFT, spaceAfter=2)
S_SUB = ParagraphStyle("st", parent=_ss["Normal"], fontName="Helvetica",
                       fontSize=9, leading=12, textColor=DIM, spaceAfter=14)
S_H1 = ParagraphStyle("h1", parent=_ss["Heading1"], fontName="Helvetica-Bold",
                      fontSize=13, leading=16, textColor=ACCENT,
                      spaceBefore=16, spaceAfter=6)
S_H2 = ParagraphStyle("h2", parent=_ss["Heading2"], fontName="Helvetica-Bold",
                      fontSize=10.5, leading=13, textColor=INK,
                      spaceBefore=10, spaceAfter=4)
S_BODY = ParagraphStyle("b", parent=_ss["Normal"], fontName="Helvetica",
                        fontSize=9.5, leading=13.5, textColor=INK,
                        spaceAfter=7)
S_CELL = ParagraphStyle("c", parent=S_BODY, fontSize=8.5, leading=11.5,
                        spaceAfter=0)
S_CELLB = ParagraphStyle("cb", parent=S_CELL, fontName="Helvetica-Bold")
S_MONO = ParagraphStyle("m", parent=S_BODY, fontName="Courier", fontSize=8.5,
                        leading=11.5)
S_CORR = ParagraphStyle("corr", parent=S_BODY, fontSize=9, leading=12.5,
                        textColor=CRIT_C, leftIndent=10, borderPadding=0)


def h1(t):
    return Paragraph(t, S_H1)


def h2(t):
    return Paragraph(t, S_H2)


def p(t):
    return Paragraph(t, S_BODY)


def mono(t):
    return Paragraph(t, S_MONO)


def bullets(items):
    return [Paragraph("&bull;&nbsp;&nbsp;" + i, S_BODY) for i in items]


def table(rows, widths, header=True):
    data = []
    for i, row in enumerate(rows):
        st = S_CELLB if (header and i == 0) else S_CELL
        data.append([Paragraph(str(c), st) for c in row])
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    cmds = [("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE)]
    if header:
        cmds += [("BACKGROUND", (0, 0), (-1, 0), BAND),
                 ("LINEBELOW", (0, 0), (-1, 0), 0.8, ACCENT)]
    t.setStyle(TableStyle(cmds))
    return t


def verdict(w):
    c = {"PASS": PASS_C, "NOTED": WARN_C}.get(w, CRIT_C)
    return '<font color="%s"><b>%s</b></font>' % (c.hexval(), w)


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(DIM)
    canvas.drawString(0.9 * inch, 0.55 * inch, doc.anycam_footer)
    canvas.drawRightString(LETTER[0] - 0.9 * inch, 0.55 * inch,
                           "Page %d" % doc.page)
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.4)
    canvas.line(0.9 * inch, 0.75 * inch, LETTER[0] - 0.9 * inch, 0.75 * inch)
    canvas.restoreState()


def build(version, story, filename):
    path = OUT_DIR / filename
    doc = SimpleDocTemplate(
        str(path), pagesize=LETTER,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        topMargin=0.85 * inch, bottomMargin=0.9 * inch,
        title="AnyCam %s Audit Report" % version,
        author="AnyCam release audit",
        subject="Release audit for AnyCam %s" % version)
    doc.anycam_footer = "AnyCam %s Audit Report - %s" % (version, DATE)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return path


BP_INTRO = (
    "Measured against <i>docs/AnyCam_Coding_Best_Practices.md</i>. CLAUDE.md "
    "rule 1 requires this section in every audit. It was present through "
    "2.3.2 and dropped during the 2.4.x cycle. It is restored and stays.")

BP_SCOPE = (
    "<b>Scope note.</b> That document contains Parts 1 to 5 and a Sources "
    "Summary. It has no Part 6. CLAUDE.md describes a "
    "\"Part 6 - Project Engineering\" covering security, dependency "
    "management and version control, so the working agreement and the "
    "document disagree. Rows below marked <i>outside Parts 1-5</i> were "
    "checked against ordinary engineering practice, not against a written "
    "AnyCam standard.")

CORRECTION = (
    "<b>Correction, %s.</b> This report supersedes the 26 September 2026 "
    "revision. That revision attributed compliance rows to "
    "\"Part 6 - Project Engineering\" of the best-practices document. No "
    "such part exists. The checks were performed and passed; the "
    "attribution was wrong. The affected rows are relabelled below and the "
    "scope note is new. No finding changed." % DATE)

W_BP = [1.45 * inch, 2.45 * inch, 0.62 * inch, 2.18 * inch]
W_FILES = [1.8 * inch, 4.9 * inch]


def story_260():
    s = [Paragraph("AnyCam 2.6.0 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - promotion build - tag 2.6.0" % DATE,
                   S_SUB),
         Paragraph(CORRECTION, S_CORR),
         p("2.6.0 promotes 2.6.0-rc3.1 to final. It is a version-string "
           "change and nothing else. No behaviour changed in this build."),
         h1("Scope"),
         p("A byte diff against the rc3.1 commit confirms two changed lines "
           "of code, both version strings, plus one prepended CHANGELOG "
           "entry."),
         table([["Check", "Result"],
                ["Changed lines in camera_discovery.py", "1 (CURRENT_VERSION)"],
                ["Changed lines in config.yaml", "1 (version)"],
                ["Changed lines in CHANGELOG.md", "33 inserted, 0 removed"],
                ["Functional code paths touched", "None"]],
               [4.0 * inch, 2.7 * inch]),
         h1("Why promote now"),
         p("rc3.1 completed field testing on the Raspberry Pi 4 / HAOS "
           "target. An earlier objection, that promotion would ship two "
           "known-broken options, was withdrawn on inspection: skip_nonref "
           "and fast_stream_start both already default to false, so a "
           "default install enables neither."),
         h1("Release gate"),
         table([["Gate", "Result", "Detail"],
                ["1. Syntax", verdict("PASS"),
                 "ast.parse and compile clean; 140 top-level definitions, no "
                 "duplicates"],
                ["2. Semantic contracts", verdict("PASS"),
                 "All 18 function contracts satisfied"],
                ["2b. CAMERA_DB throttle", verdict("PASS"),
                 "throttle_type values in enum; data fields paired with "
                 "_confidence siblings"],
                ["3. Best-practice audit", verdict("PASS"), "No violations"],
                ["4. Version consistency", verdict("PASS"),
                 "CURRENT_VERSION matches config.yaml at 2.6.0"],
                ["5. Changelog", verdict("PASS"), "Top entry matches 2.6.0"]],
               [1.5 * inch, 0.75 * inch, 4.45 * inch]),
         PageBreak(),
         h1("Best-practices compliance"),
         p(BP_INTRO), p(BP_SCOPE),
         table([["Standard", "Check applied", "Result", "Note"],
                ["1.1 Truncated function", "AST plus duplicate-def scan",
                 verdict("PASS"), "Gate 1. No function bodies edited."],
                ["1.2 Syntax errors", "compile() to bytecode",
                 verdict("PASS"), "Gate 1."],
                ["1.4 Async/await", "No async code changed", verdict("PASS"),
                 "Not applicable."],
                ["1.5 Style, PEP 8", "Naming of the changed constant",
                 verdict("PASS"), "CURRENT_VERSION stays UPPER_CASE."],
                ["Parts 2 to 4", "No JS, HTML or CSS changed",
                 verdict("PASS"), "Not applicable."],
                ["5.4 Sneaky errors", "Whole diff against the table",
                 verdict("PASS"),
                 "A two-line version diff cannot hit any row."],
                ["Outside Parts 1-5: packaging",
                 "Dependency and packaging review", verdict("PASS"),
                 "No new dependencies. Dockerfile unchanged from rc3.1. Not "
                 "covered by a written AnyCam standard."]],
               W_BP),
         h1("Packaging"),
         p("Packaged per CLAUDE.md rule 3. Archive name and top-level folder "
           "both carry the full version string."),
         mono("camera_discovery-2.6.0.zip<br/>"
              "&nbsp;&nbsp;camera_discovery-2.6.0/"),
         p("Ten entries, forward-slash separators, verified explicitly. A "
           "first attempt through .NET ZipFile.CreateFromDirectory on "
           "Windows wrote backslash separators, which would not extract on "
           "the Linux HAOS target. Rebuilt with Python zipfile and checked."),
         h1("Superseded by"),
         p("This build could not be installed after September 2026. The "
           "Dockerfile it carries pins ffmpeg to a version both Debian and "
           "the Raspberry Pi archive have since deleted. See the 2.6.2 "
           "audit. 2.6.0 was never affected at the time it was cut, and no "
           "AnyCam code is at fault."),
         h1("Files changed"),
         table([["File", "Change"],
                ["camera_discovery.py", "CURRENT_VERSION to 2.6.0"],
                ["config.yaml", "version to 2.6.0"],
                ["CHANGELOG.md", "Prepended the 2.6.0 entry"]], W_FILES)]
    return s


def story_261():
    s = [Paragraph("AnyCam 2.6.1 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - Tier 1 live-feed work - tag 2.6.1"
                   % DATE, S_SUB),
         Paragraph(CORRECTION, S_CORR),
         Paragraph("<b>Status: superseded by 2.6.2 and never installable.</b> "
                   "The image build fails on a stale ffmpeg apt pin carried "
                   "since 2.6.0-rc2.3. No code in this release is at fault. "
                   "The code changes below are all carried forward into "
                   "2.6.2 unaltered.", S_CORR),
         p("2.6.1 carries Tier 1 of the live-feed quality work: three of "
           "four planned items, plus a repair to the release gate."),
         h1("Item 1 - low-latency ffmpeg flags"),
         p("_launch_snap now always passes -fflags +nobuffer and -flags "
           "low_delay. Both are standard for live RTSP and neither affects "
           "stream detection."),
         h2("The merge defect this avoided"),
         p("ffmpeg accepts a single -fflags value. The existing code added "
           "-fflags +discardcorrupt conditionally for cameras with H.265+ "
           "history. A second -fflags argument would have silently dropped "
           "+discardcorrupt on exactly those cameras. The new code appends "
           "to one string."),
         h2("Held back deliberately"),
         p("-probesize 32, -analyzeduration 0 and -reorder_queue_size 1 sit "
           "behind a new low_latency option, default off. A small probe size "
           "can stop codec identification; a one-packet reorder queue "
           "removes the RTSP jitter buffer."),
         h1("Item 3 - Fast Stream Start gated"),
         p("Suppressed at 3840x2160 HEVC, where software decode cannot beat "
           "rpivid warmup (2.5 to 7 seconds), so the parallel decode spent "
           "CPU on frames that never rendered and opened a second RTSP "
           "session. Width is read from the ladder's active profile, not the "
           "camera record, because a step-down may already have moved off "
           "4K. Still applies below 4K and to h264."),
         h1("Item 4 - ONVIF SOAP honours the brand cooldown"),
         p("_onvif_soap is synchronous and opens up to two TCP connections "
           "per call, because cameras answering HTTP 400 to SOAP 1.2 get a "
           "SOAP 1.1 retry. It never consulted the throttle, so a re-auth "
           "fired 1 + N calls back to back inside the cooldown window. "
           "_rerun_onvif_auth now awaits _throttle_wait_if_needed before "
           "GetProfiles and before each per-profile GetStreamUri. Closes the "
           "latent issue in CLAUDE.md."),
         h1("Release gate - Windows portability"),
         p("verify_release.py could not run on Windows. Three read_text() "
           "calls omitted encoding and hit cp1252 on UTF-8 source; ok() and "
           "fail() printed U+2713 and U+2717, which cp1252 cannot encode. "
           "Both fixed. Invisible on the Pi because Linux defaults to UTF-8."),
         h1("Item 2 - deferred, not implemented"),
         p("Replacing per-frame polling with a persistent "
           "multipart/x-mixed-replace response was not implemented. The "
           "docstring on handle_snapshot records that per-frame polling was "
           "deliberate, because Home Assistant ingress nginx terminates "
           "long-lived multipart streams early. Per CLAUDE.md rule 8 that "
           "prior finding outranks a fresh assumption. The finding may be "
           "stale, since ingress_stream: true is set; the next step is a "
           "throwaway test, not a rewrite."),
         PageBreak(),
         h1("Best-practices compliance"),
         p(BP_INTRO), p(BP_SCOPE),
         table([["Standard", "Check applied", "Result", "Note"],
                ["1.1 Truncated function", "AST plus duplicate-def scan",
                 verdict("PASS"), "140 top-level definitions, no duplicates."],
                ["1.2 Syntax errors", "compile() to bytecode",
                 verdict("PASS"), "Gate 1."],
                ["1.4 Missing await", "Every added call reviewed",
                 verdict("PASS"),
                 "Both added _throttle_wait_if_needed calls are awaited."],
                ["1.4 Blocking I/O in async",
                 "Checked for time.sleep, requests, open", verdict("PASS"),
                 "None added. The sync SOAP helper stays in "
                 "run_in_executor."],
                ["1.4 await in loop",
                 "Flagged the new await inside for prof in profiles",
                 verdict("NOTED"),
                 "Intentional. Serialising is the point of the fix; "
                 "asyncio.gather would defeat the cooldown. Throttle value "
                 "computed once outside the loop."],
                ["1.5 Naming", "PEP 8 on all new identifiers",
                 verdict("PASS"),
                 "CFG_LOW_LATENCY UPPER_CASE; locals snake_case."],
                ["1.5 Line length", "Measured every added line",
                 verdict("NOTED"),
                 "9 added lines over 79 chars against a 6.0 percent existing "
                 "baseline. Two self-inflicted ones shortened before tagging."],
                ["1.5 Bare except", "Scanned the diff", verdict("PASS"),
                 "The one added handler names AttributeError and OSError."],
                ["1.5 Mutable defaults", "Reviewed new signatures",
                 verdict("PASS"), "No new function signatures."],
                ["Parts 2 to 4", "No JS, HTML or CSS changed",
                 verdict("PASS"),
                 "Deferred Item 2 would have touched JS."],
                ["5.1 Escaping context", "No f-string HTML or CSS added",
                 verdict("PASS"), "Not applicable."],
                ["5.4 Sneaky errors", "Diff against every row",
                 verdict("PASS"),
                 "The -fflags merge defect is the row this diff could have "
                 "hit. Avoided by design."],
                ["Outside Parts 1-5: security",
                 "Reviewed for shell=True and unvalidated input",
                 verdict("PASS"),
                 "create_subprocess_exec takes an argument list, not a shell "
                 "string. Not covered by a written AnyCam standard."],
                ["Outside Parts 1-5: dependencies",
                 "Reviewed new runtime dependencies", verdict("PASS"),
                 "None added. Note this check does not cover apt pin "
                 "staleness, which is what made this release unbuildable."]],
               W_BP),
         h1("What this audit missed"),
         p("This release passed all five gates and every row above, and "
           "still could not be installed. The reason is recorded here so the "
           "gap is not repeated."),
         p("The Dockerfile pinned ffmpeg to an exact version that both "
           "Debian and the Raspberry Pi archive had already deleted. No "
           "release gate inspects the Dockerfile, and the best-practices "
           "document has no standard covering dependency pinning. The "
           "dependency row above checked only whether new dependencies were "
           "introduced, which is the wrong question."),
         h1("Files changed"),
         table([["File", "Change"],
                ["camera_discovery.py",
                 "Added CFG_LOW_LATENCY. Merged +nobuffer into the single "
                 "-fflags value, added -flags low_delay, probe flags behind "
                 "the new option. Gated fast_stream_start at 4K HEVC. Added "
                 "two throttle waits in _rerun_onvif_auth. Version to 2.6.1."],
                ["config.yaml", "Added low_latency option and schema entry."],
                ["run.sh", "Exported LOW_LATENCY and logged it."],
                ["translations/en.yaml",
                 "Added Low Latency Probe description; noted the 4K HEVC "
                 "limit on Fast Stream Start."],
                ["verify_release.py",
                 "encoding=utf-8 on three read_text calls; UTF-8 stdout."],
                ["CHANGELOG.md", "Prepended the 2.6.1 entry"]], W_FILES)]
    return s


def story_262():
    s = [Paragraph("AnyCam 2.6.2 Audit Report", S_TITLE),
         Paragraph("Release audit - %s - build fix - tag 2.6.2" % DATE, S_SUB),
         p("2.6.2 restores the ability to build the image. 2.6.1 could not "
           "be installed at all. No AnyCam code changed. The only edited "
           "files are the Dockerfile and the two version strings."),
         h1("What failed"),
         p("Reported by the user against 2.6.1. Per CLAUDE.md rule 2 the "
           "Supervisor log was obtained before any investigation. The build "
           "stopped at Dockerfile step 3:"),
         mono("E: Version '8:5.1.8-0+deb12u1+rpt1' for 'ffmpeg' was not "
              "found"),
         p("Four consecutive attempts in the log, all identical, all at the "
           "same step. The failure is in the apt resolve, before any AnyCam "
           "file is copied into the image."),
         h1("Root cause"),
         p("The pin did not rot. The archive moved. Debian and the Raspberry "
           "Pi archive each keep only the current version of a package, so "
           "the 5.1.9 security update deleted the 5.1.8 version the "
           "Dockerfile pinned to."),
         p("Both archives were queried directly to establish this rather "
           "than inferred:"),
         table([["Branch", "Pinned", "Archive holds now"],
                ["aarch64, rpios", "8:5.1.8-0+deb12u1+rpt1",
                 "8:5.1.9-0+deb12u1+rpt1"],
                ["amd64, Debian", "7:5.1.8-0+deb12u1",
                 "7:5.1.9-0+deb12u1"]],
               [1.6 * inch, 2.55 * inch, 2.55 * inch]),
         p("The amd64 pin was stale by the same point release and would have "
           "failed on its next build. The apt preferences origin string was "
           "also checked live and still matches: the archive still publishes "
           "Origin: Raspberry Pi Foundation."),
         h1("Fix"),
         p("ffmpeg is installed with no version constraint and is now the "
           "single exception to the pin-everything rule set in "
           "2.6.0-rc2.3. An exact pin against these archives has a shelf "
           "life of months, and the failure lands on the user at install "
           "time rather than on the project at build time."),
         p("ffmpeg is still constrained, by source instead of version. The "
           "apt preferences file written in step 1 gives ffmpeg and every "
           "libav, libsw and libpostproc sibling Pin-Priority 990 against "
           "o=Raspberry Pi Foundation, and everything else from that origin "
           "Pin-Priority 1. On aarch64 that forces the rpios build, because "
           "990 beats Debian's default of 500, and the rpios build carries "
           "the Pi patches needed for rpivid. On amd64 the rpios source is "
           "never registered, so ffmpeg resolves to Debian. Version floats "
           "inside the bookworm suite, which bounds it to 5.1.x."),
         p("Every other package keeps its exact pin. Those come from Debian "
           "bookworm, now frozen at oldstable, and do not rotate the same "
           "way."),
         p("Because the version floats, the Dockerfile now echoes what apt "
           "resolved, so each build log records the ffmpeg it received."),
         PageBreak(),
         h1("Best-practices compliance"),
         p(BP_INTRO), p(BP_SCOPE),
         table([["Standard", "Check applied", "Result", "Note"],
                ["1.1 Truncated function", "AST plus duplicate-def scan",
                 verdict("PASS"),
                 "Gate 1. No Python changed in this release."],
                ["1.2 Syntax errors", "compile() to bytecode",
                 verdict("PASS"), "Gate 1."],
                ["1.4 Async/await", "No async code changed", verdict("PASS"),
                 "Not applicable."],
                ["1.5 Style, PEP 8", "Only the version constant changed",
                 verdict("PASS"), "CURRENT_VERSION stays UPPER_CASE."],
                ["Parts 2 to 4", "No JS, HTML or CSS changed",
                 verdict("PASS"), "Not applicable."],
                ["5.4 Sneaky errors", "Whole diff against the table",
                 verdict("PASS"),
                 "One candidate row existed. The echo originally used "
                 "${Version} inside a RUN, where the build log shows the "
                 "shell expanding ${...}. Single quotes would have protected "
                 "it, but the construct was replaced with dpkg-query -W to "
                 "remove the ambiguity."],
                ["Outside Parts 1-5: dependency pinning",
                 "Both archives queried live for every pinned version",
                 verdict("NOTED"),
                 "This is the check that did not exist and would have caught "
                 "the 2.6.1 failure. It was performed by hand here. It is "
                 "not automated and not written into any standard."],
                ["Outside Parts 1-5: reproducibility",
                 "Reviewed the effect of an unpinned package",
                 verdict("NOTED"),
                 "The image is no longer byte-reproducible with respect to "
                 "ffmpeg. Accepted deliberately, with the resolved version "
                 "logged at build time as the mitigation."]],
               W_BP),
         h1("Why no gate caught this"),
         p("2.6.1 passed all five release gates and still could not build. "
           "Two gaps, both left open here rather than fixed silently:"),
         *bullets([
             "No gate inspects the Dockerfile. verify_release.py checks "
             "syntax, semantic contracts, the best-practice audit, version "
             "consistency and the changelog. An apt pin is outside all five.",
             "The best-practices document has no standard covering "
             "dependency management. CLAUDE.md describes a Part 6 that "
             "covers it, but that part is not present in the document. The "
             "working agreement and the document disagree, and that "
             "discrepancy is itself unresolved."]),
         p("A sixth gate that queries each archive for every pinned version "
           "would have caught this before packaging. It was offered and not "
           "selected for this release. It needs network access at release "
           "time, which no current gate requires."),
         h1("Not verified"),
         p("Stated plainly so it is not mistaken for tested work. Per "
           "CLAUDE.md rule 4 a missing tool is reported, not worked around."),
         *bullets([
             "<b>The image was not built.</b> No Docker and no aarch64 "
             "builder exists on the Windows workstation. The fix is reasoned "
             "from the live archive metadata and from apt priority rules, "
             "not from a successful build. Installing this release on the Pi "
             "is the first real test.",
             "<b>The resolved ffmpeg version is unconfirmed.</b> It should "
             "be 8:5.1.9-0+deb12u1+rpt1 on the Pi. Check the new "
             "'ffmpeg resolved to' line in the build log.",
             "<b>The Tier 1 changes carried from 2.6.1 remain untested.</b> "
             "They have never run, because 2.6.1 never installed. The "
             "acceptance list from that audit still applies in full."]),
         h1("Acceptance"),
         *bullets([
             "The image builds and the addon starts on HAOS at "
             "172.16.0.35:8123.",
             "The build log carries 'ffmpeg resolved to:' followed by "
             "8:5.1.9-0+deb12u1+rpt1 or later, from the rpt archive.",
             "Hardware decode still works. The rpt suffix confirms the Pi "
             "patched build; an unsuffixed Debian version on aarch64 would "
             "mean the origin pin failed and rpivid will not engage.",
             "The Configuration tab and startup log both report 2.6.2.",
             "Then the full 2.6.1 acceptance list, which has never run."]),
         h1("Files changed"),
         table([["File", "Change"],
                ["Dockerfile",
                 "Removed the FFMPEG_PIN branch and the exact ffmpeg version "
                 "on both arches. ffmpeg installed unpinned, constrained by "
                 "the step-1 origin preference. Added an echo of the "
                 "resolved version. Rewrote the pin-policy comment to record "
                 "why ffmpeg is the exception. Corrected a stale header "
                 "comment that still named ffmpeg=8:5.1.3-1+rpt4."],
                ["camera_discovery.py", "CURRENT_VERSION to 2.6.2"],
                ["config.yaml", "version to 2.6.2"],
                ["CHANGELOG.md", "Prepended the 2.6.2 entry"]], W_FILES)]
    return s


if __name__ == "__main__":
    out = [build("2.6.0", story_260(), "anycam_2_6_0_audit_report.pdf"),
           build("2.6.1", story_261(), "anycam_2_6_1_audit_report.pdf"),
           build("2.6.2", story_262(), "anycam_2_6_2_audit_report.pdf")]
    for path in out:
        print("wrote %-38s %6.1f KB" % (path.name,
                                        path.stat().st_size / 1024))
