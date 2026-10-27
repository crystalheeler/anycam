"""Turn a project Markdown report into a readable PDF (headings, paragraphs, lists, code, tables, links).

    python docs/tools/md-to-pdf.py docs/CEC-and-TV-Research.md docs/CEC-and-TV-Research.pdf
"""
import html
import re
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (KeepTogether, ListFlowable, ListItem, Paragraph, Preformatted,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

ss = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=ss["BodyText"], fontName="Helvetica", fontSize=10, leading=14,
                      spaceAfter=6, alignment=TA_LEFT)
H = {1: ParagraphStyle("h1", parent=ss["Title"], fontSize=19, leading=23, spaceAfter=10, alignment=TA_LEFT,
                       textColor=colors.HexColor("#123")),
     2: ParagraphStyle("h2", parent=ss["Heading2"], fontSize=14, leading=18, spaceBefore=12, spaceAfter=6,
                       textColor=colors.HexColor("#14527a")),
     3: ParagraphStyle("h3", parent=ss["Heading3"], fontSize=11.5, leading=15, spaceBefore=8, spaceAfter=4,
                       textColor=colors.HexColor("#1d3b52"))}
CELL = ParagraphStyle("cell", parent=BODY, fontSize=8, leading=10, spaceAfter=0)
CODE = ParagraphStyle("code", parent=ss["Code"], fontSize=8, leading=10, backColor=colors.HexColor("#f3f3f3"),
                      borderPadding=4, spaceAfter=8)


def inline(t: str) -> str:
    t = html.escape(t, quote=False)
    t = re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?!\w)", r"<i>\1</i>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<link href="\2" color="#0b5cad">\1</link>', t)
    t = re.sub(r"(?<![\"=>])(https?://[^\s<)]+)", r'<link href="\1" color="#0b5cad">\1</link>', t)
    return t


def build(src: Path, dst: Path) -> None:
    lines = src.read_text(encoding="utf-8").splitlines()
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            i += 1
            continue
        if ln.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            out.append(Preformatted("\n".join(lines[i + 1:j]), CODE))
            i = j + 1
            continue
        m = re.match(r"^(#{1,3})\s+(.*)", ln)
        if m:
            out.append(Paragraph(inline(m.group(2)), H[len(m.group(1))]))
            i += 1
            continue
        if ln.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            n = max(len(r) for r in rows)
            data = [[Paragraph(("<b>%s</b>" if k == 0 else "%s") % inline(c), CELL) for c in r + [""] * (n - len(r))]
                    for k, r in enumerate(rows)]
            t = Table(data, colWidths=[6.9 * inch / n] * n, repeatRows=1)
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bbb")),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef3")),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            out += [t, Spacer(1, 8)]
            continue
        if re.match(r"^\s*([-*]|\d+\.)\s+", ln):
            items, numbered = [], bool(re.match(r"^\s*\d+\.", ln))
            while i < len(lines) and re.match(r"^\s*([-*]|\d+\.)\s+", lines[i]):
                text = re.sub(r"^\s*([-*]|\d+\.)\s+", "", lines[i])
                indent = len(lines[i]) - len(lines[i].lstrip())
                i += 1
                while i < len(lines) and lines[i].strip() and not re.match(r"^\s*([-*]|\d+\.)\s+", lines[i]) \
                        and not lines[i].startswith(("#", "|", "```")):
                    text += " " + lines[i].strip()
                    i += 1
                st = ParagraphStyle("li", parent=BODY, spaceAfter=2, leftIndent=12 if indent else 0)
                items.append(ListItem(Paragraph(inline(text), st)))
            out.append(ListFlowable(items, bulletType="1" if numbered else "bullet", start="1" if numbered else None,
                                    leftIndent=16, bulletFontSize=8))
            out.append(Spacer(1, 4))
            continue
        para = [ln.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#|\||```|\s*([-*]|\d+\.)\s)", lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append(Paragraph(inline(" ".join(para)), BODY))

    def page(c, d):
        c.saveState()
        c.setFont("Helvetica", 8)
        c.setFillColor(colors.grey)
        c.drawString(0.8 * inch, 0.5 * inch, "AnyCam — HDMI-CEC and showing a camera on the TV")
        c.drawRightString(7.7 * inch, 0.5 * inch, "Page %d" % d.page)
        c.restoreState()
    doc = SimpleDocTemplate(str(dst), pagesize=letter, leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                            topMargin=0.75 * inch, bottomMargin=0.8 * inch,
                            title="HDMI-CEC and TV Research", author="CrystalHeeler")
    doc.build(out, onFirstPage=page, onLaterPages=page)


if __name__ == "__main__":
    build(Path(sys.argv[1]), Path(sys.argv[2]))
    print("wrote", sys.argv[2])
