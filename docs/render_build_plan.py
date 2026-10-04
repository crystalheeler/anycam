#!/usr/bin/env python3
"""Render docs/BUILD_PLAN.md as a colour-coded HTML page.

BUILD_PLAN.md stays the single source of truth; this only presents it.
After editing the plan, run:

    python docs/render_build_plan.py [--out PATH]

Default output is docs/BUILD_PLAN.html, committed beside the Markdown.
Output is deterministic: the same Markdown always produces the same HTML,
so the HTML changes in git only when the plan does. Standard library only.
The parser is strict on purpose: an unknown status or a table row with the
wrong number of cells stops the render with the line number, instead of
silently dropping an item from the page.
"""
import argparse
import html
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "BUILD_PLAN.md"
DEFAULT_OUT = HERE / "BUILD_PLAN.html"

# Order here is display order for the filter chips and the summary.
STATUSES = {
    "You":     "Waiting on you",
    "Logs":    "Logs first",
    "Discuss": "Discuss first",
    "Ready":   "Ready to build",
    "Blocked": "Blocked",
    "Later":   "Later",
}


class PlanError(Exception):
    pass


def inline(md: str) -> str:
    """Convert the small Markdown subset the plan uses to escaped HTML."""
    codes: list[str] = []

    def stash(m: re.Match) -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    s = re.sub(r"`([^`]+)`", stash, md)
    s = html.escape(s, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    return re.sub(r"\x00(\d+)\x00",
                  lambda m: "<code>" + html.escape(codes[int(m.group(1))],
                                                   quote=False) + "</code>", s)


def links(rendered: str) -> str:
    """Make item IDs (B2, C10) in rendered text link to their cards."""
    return re.sub(r"(?<![\w#/\"-])([A-F]\d{1,2})\b(?![^<]*</a>)", r'<a href="#\1">\1</a>', rendered)


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse(text: str) -> dict:
    plan = {"title": "", "compiled": "", "purpose": "", "order": [],
            "order_title": "Recommended order", "order_notes": [], "phases": [],
            "phases_title": "", "phase_notes": [], "groups": [], "done": [], "not_ours": []}
    section = None
    group = None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if line.startswith("# "):
            plan["title"] = line[2:].strip()
            continue
        if line.startswith("## "):
            head = line[3:].strip()
            m = re.match(r"([A-Z])\. (.+)$", head)
            if m:
                group = {"letter": m.group(1), "title": m.group(2), "items": []}
                plan["groups"].append(group)
                section = "group"
            elif head.startswith(("Recommended order", "Next up")):
                section = "order"
                plan["order_title"] = head
            elif head.startswith("Phases"):
                section = "phases"
                plan["phases_title"] = head
            elif head.startswith("Done"):
                section = "done"
            elif head.startswith("Not AnyCam"):
                section = "not_ours"
            else:
                section = None
            continue
        if line.startswith("**Compiled:**"):
            plan["compiled"] = line.split("**", 2)[2].strip()
        elif line.startswith("**Purpose:**"):
            plan["purpose"] = line.split("**", 2)[2].strip()
        if not line or re.match(r"^\|[-| ]+\|$", line) or line == "---":
            continue

        if section == "order":
            m = re.match(r"\d+\. (.+)$", line)
            if m:
                plan["order"].append(m.group(1))
            else:
                plan["order_notes"].append(line)
        elif section == "phases":
            if line.startswith("|"):
                c = cells(line)
                if c[0] == "Version":
                    continue
                if len(c) != 3:
                    raise PlanError(f"line {n}: expected 3 cells, found {len(c)}")
                plan["phases"].append(c)
            else:
                plan["phase_notes"].append(line)
        elif section == "group" and line.startswith("|"):
            c = cells(line)
            if c[0] == "#":
                continue
            if len(c) != 5:
                raise PlanError(f"line {n}: expected 5 cells, found {len(c)}")
            ident, status, item, why, nxt = c
            if status not in STATUSES:
                raise PlanError(f"line {n}: unknown status '{status}' "
                                f"(allowed: {', '.join(STATUSES)})")
            priority = item.startswith("**") and item.endswith("**")
            group["items"].append({"id": ident, "status": status,
                                   "item": item.strip("*") if priority else item,
                                   "priority": priority, "why": why, "next": nxt})
        elif section == "done" and line.startswith("|"):
            c = cells(line)
            if c[0] == "Item":
                continue
            if len(c) != 3:
                raise PlanError(f"line {n}: expected 3 cells, found {len(c)}")
            plan["done"].append(c)
        elif section == "not_ours" and line.startswith("- "):
            plan["not_ours"].append(line[2:])
    if not plan["groups"]:
        raise PlanError("no item groups found")
    return plan


CSS = r"""
/* Layout: summary first (colour bar, filters, what is waiting on CrystalHeeler), then one
   section per group; each group keeps its colour from the test-card bars. */
:root{
  --bg:#EEF0F3; --surface:#FFFFFF; --surface-2:#E5E9EE;
  --ink:#1A1F27; --ink-2:#465060; --ink-3:#6A7483; --rule:#D0D6DE; --focus:#0F7387;
  --grp-a:#0F8599; --grp-b:#C0412E; --grp-c:#A23D98; --grp-d:#3A8540; --grp-e:#3D58C0; --grp-f:#9A6F00;
  --grp-ink:#FFFFFF;
  --st-you-bg:#FBE7C0; --st-you-fg:#7A4A00;
  --st-logs-bg:#ECE3FA; --st-logs-fg:#5A3A9C;
  --st-discuss-bg:#DCE8F8; --st-discuss-fg:#1F4C8A;
  --st-ready-bg:#DAEFDF; --st-ready-fg:#25613A;
  --st-blocked-bg:#E6E8EC; --st-blocked-fg:#4E555F;
  --st-later-fg:#6A7483;
  --f-ui:"Archivo",system-ui,-apple-system,"Segoe UI",sans-serif;
  --f-mono:"JetBrains Mono",ui-monospace,"Cascadia Mono",Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#0F131A; --surface:#161B23; --surface-2:#1D242E;
    --ink:#DFE4EA; --ink-2:#A6B0BD; --ink-3:#7D8896; --rule:#2B333E; --focus:#45C3D8;
    --grp-a:#45C3D8; --grp-b:#E4735E; --grp-c:#D67ACC; --grp-d:#6FBF83; --grp-e:#8A9DF2; --grp-f:#E3BD4A;
    --grp-ink:#0F131A;
    --st-you-bg:#3B2C10; --st-you-fg:#F2BA55;
    --st-logs-bg:#2A2140; --st-logs-fg:#BBA5F2;
    --st-discuss-bg:#16263A; --st-discuss-fg:#8FB9EF;
    --st-ready-bg:#152F1E; --st-ready-fg:#80CC94;
    --st-blocked-bg:#252A32; --st-blocked-fg:#A8B0BB;
    --st-later-fg:#7D8896;
    color-scheme:dark;
  }
}
:root[data-theme="dark"]{
  --bg:#0F131A; --surface:#161B23; --surface-2:#1D242E;
  --ink:#DFE4EA; --ink-2:#A6B0BD; --ink-3:#7D8896; --rule:#2B333E; --focus:#45C3D8;
  --grp-a:#45C3D8; --grp-b:#E4735E; --grp-c:#D67ACC; --grp-d:#6FBF83; --grp-e:#8A9DF2; --grp-f:#E3BD4A;
  --grp-ink:#0F131A;
  --st-you-bg:#3B2C10; --st-you-fg:#F2BA55;
  --st-logs-bg:#2A2140; --st-logs-fg:#BBA5F2;
  --st-discuss-bg:#16263A; --st-discuss-fg:#8FB9EF;
  --st-ready-bg:#152F1E; --st-ready-fg:#80CC94;
  --st-blocked-bg:#252A32; --st-blocked-fg:#A8B0BB;
  --st-later-fg:#7D8896;
  color-scheme:dark;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--f-ui);
  font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px;padding-block:40px 72px}
a{color:var(--focus);text-underline-offset:2px}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:3px}
code{font-family:var(--f-mono);font-size:.86em;background:var(--surface-2);
  padding:.08em .35em;border-radius:3px;overflow-wrap:anywhere}
strong{font-weight:700}

.g-a{--g:var(--grp-a)} .g-b{--g:var(--grp-b)} .g-c{--g:var(--grp-c)}
.g-d{--g:var(--grp-d)} .g-e{--g:var(--grp-e)} .g-f{--g:var(--grp-f)}

/* header */
.eyebrow{font-size:.72rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;
  color:var(--ink-3);margin:0 0 6px}
h1{font-size:clamp(1.9rem,4.2vw,2.7rem);line-height:1.08;letter-spacing:-.02em;
  margin:0 0 10px;text-wrap:balance}
.meta{color:var(--ink-2);margin:0;max-width:75ch}
.meta .mono{font-family:var(--f-mono);font-size:.82rem;color:var(--ink-3)}

/* the plan's colour bar: one segment per group, width = open items */
.bars{display:flex;gap:3px;margin:26px 0 8px;height:46px}
.bar{flex:var(--n) 1 0;min-width:44px;background:var(--g);color:var(--grp-ink);
  display:flex;flex-direction:column;justify-content:center;padding:0 10px;
  text-decoration:none;border-radius:3px;line-height:1.1}
.bar b{font-family:var(--f-mono);font-size:.95rem}
.bar span{font-size:.68rem;font-weight:600;opacity:.9;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.bars-cap{font-size:.78rem;color:var(--ink-3);margin:0 0 26px}

/* filters */
.filters{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 30px}
.chip{font:inherit;font-size:.82rem;font-weight:600;border:1px solid var(--rule);
  background:var(--surface);color:var(--ink-2);border-radius:99px;padding:6px 12px;
  cursor:pointer;display:inline-flex;gap:7px;align-items:center}
.chip .n{font-family:var(--f-mono);font-weight:700}
.chip[aria-pressed="true"]{border-color:var(--ink);color:var(--ink);box-shadow:inset 0 0 0 1px var(--ink)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--dc);flex:none}

/* status pills */
.pill{font-size:.7rem;font-weight:700;letter-spacing:.04em;text-transform:uppercase;
  padding:3px 9px;border-radius:99px;white-space:nowrap;background:var(--pb);color:var(--pf)}
.st-You{--pb:var(--st-you-bg);--pf:var(--st-you-fg);--dc:var(--st-you-fg)}
.st-Logs{--pb:var(--st-logs-bg);--pf:var(--st-logs-fg);--dc:var(--st-logs-fg)}
.st-Discuss{--pb:var(--st-discuss-bg);--pf:var(--st-discuss-fg);--dc:var(--st-discuss-fg)}
.st-Ready{--pb:var(--st-ready-bg);--pf:var(--st-ready-fg);--dc:var(--st-ready-fg)}
.st-Blocked{--pb:var(--st-blocked-bg);--pf:var(--st-blocked-fg);--dc:var(--st-blocked-fg)}
.st-Later{--pb:transparent;--pf:var(--st-later-fg);--dc:var(--st-later-fg)}
.pill.st-Later{box-shadow:inset 0 0 0 1px var(--rule)}

/* summary panels */
.top{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,1fr);gap:18px;margin-bottom:44px}
@media (max-width:860px){.top{grid-template-columns:minmax(0,1fr)}}
.panel{background:var(--surface);border:1px solid var(--rule);border-radius:6px;padding:18px 20px}
.panel h2{font-size:1.05rem;margin:0 0 12px;letter-spacing:-.005em}
.panel.you{background:var(--st-you-bg);border-color:transparent}
.panel.you h2{color:var(--st-you-fg)}
.you-list{list-style:none;margin:0;padding:0;display:grid;gap:12px}
.you-list li{display:grid;grid-template-columns:auto minmax(0,1fr);gap:4px 10px}
.you-list .t{font-weight:600;color:var(--ink)}
.you-list .t a{color:inherit;text-decoration:none}
.you-list .t a:hover{text-decoration:underline}
.you-list .x{grid-column:2;color:var(--ink-2);font-size:.88rem}
.order{margin:0;padding:0;list-style:none;counter-reset:o;display:grid;gap:10px}
.order li{counter-increment:o;display:grid;grid-template-columns:28px minmax(0,1fr);gap:8px;color:var(--ink-2)}
.order li::before{content:counter(o);font-family:var(--f-mono);font-weight:700;color:var(--ink);
  background:var(--surface-2);border-radius:4px;text-align:center;height:24px;line-height:24px}
.order-note{margin:12px 0 0;font-size:.85rem;color:var(--ink-3)}

/* groups */
section.group{margin:0 0 40px}
.ghead{border-top:6px solid var(--g);padding-top:12px;display:flex;align-items:baseline;
  gap:12px;margin-bottom:16px;flex-wrap:wrap}
.gl{font-family:var(--f-mono);font-weight:700;background:var(--g);color:var(--grp-ink);
  border-radius:4px;padding:1px 9px;font-size:1rem}
.ghead h2{margin:0;font-size:1.3rem;letter-spacing:-.01em}
.gcount{color:var(--ink-3);font-size:.85rem}
.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
@media (max-width:820px){.cards{grid-template-columns:minmax(0,1fr)}}
.card{background:var(--surface);border:1px solid var(--rule);border-radius:6px;
  padding:14px 16px 16px;display:flex;flex-direction:column;gap:8px;scroll-margin-top:16px}
.card:target{border-color:var(--g);box-shadow:0 0 0 2px var(--g)}
.ctop{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.id{font-family:var(--f-mono);font-weight:700;font-size:.8rem;color:var(--g);
  background:color-mix(in srgb,var(--g) 14%,var(--surface));border-radius:4px;padding:2px 7px}
.prio{font-size:.7rem;font-weight:700;color:var(--ink);letter-spacing:.04em;text-transform:uppercase}
.card h3{margin:0;font-size:1.02rem;line-height:1.35;text-wrap:balance}
.card dl{margin:0;display:grid;gap:6px}
.card dt{font-size:.68rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-3)}
.card dd{margin:0;color:var(--ink-2);font-size:.9rem;min-width:0}

/* done */
.done-wrap{overflow-x:auto;background:var(--surface);border:1px solid var(--rule);border-radius:6px}
table{border-collapse:collapse;width:100%;min-width:640px;font-size:.86rem}
th,td{text-align:left;vertical-align:top;padding:9px 14px;border-bottom:1px solid var(--rule)}
th{font-size:.68rem;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-3);background:var(--surface-2)}
td{color:var(--ink-2)} td:first-child{color:var(--ink);font-weight:600}
tr:last-child td{border-bottom:none}
.tick{color:var(--st-ready-fg);font-weight:700;margin-right:6px}
.aside{color:var(--ink-3);font-size:.88rem;margin:18px 0 0}
footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--rule);color:var(--ink-3);font-size:.8rem}
@media (prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
"""

JS = r"""
(() => {
  const chips = [...document.querySelectorAll('.chip')];
  const cards = [...document.querySelectorAll('.card')];
  const groups = [...document.querySelectorAll('section.group')];
  const extras = [...document.querySelectorAll('[data-all-only]')];
  function apply(f) {
    cards.forEach(c => { c.hidden = f !== 'all' && c.dataset.status !== f; });
    groups.forEach(g => { g.hidden = !g.querySelector('.card:not([hidden])'); });
    extras.forEach(e => { e.hidden = f !== 'all'; });
    chips.forEach(ch => ch.setAttribute('aria-pressed', String(ch.dataset.filter === f)));
  }
  chips.forEach(ch => ch.addEventListener('click', () => apply(ch.dataset.filter)));
})();
"""


def render(plan: dict) -> str:
    items = [i | {"group": g["letter"]} for g in plan["groups"] for i in g["items"]]
    counts = {s: sum(1 for i in items if i["status"] == s) for s in STATUSES}
    e = inline
    out = [
        '<title>AnyCam Build Plan</title>',
        '<link rel="preconnect" href="https://fonts.googleapis.com">',
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family='
        'Archivo:wght@500;600;700&family=JetBrains+Mono:wght@500;700&display=swap">',
        f"<style>{CSS}</style>",
        '<div class="wrap">',
        '<header>',
        '<p class="eyebrow">AnyCam · Home Assistant add-on</p>',
        f'<h1>{html.escape(plan["title"])}</h1>',
        f'<p class="meta">{e(plan["purpose"])}</p>',
        f'<p class="meta"><span class="mono">{e(plan["compiled"])} · '
        f'{len(items)} open · {len(plan["done"])} done</span></p>',
        '</header>',
    ]

    out.append('<nav class="bars" aria-label="Open items per group">')
    for g in plan["groups"]:
        n = len(g["items"])
        out.append(f'<a class="bar g-{g["letter"].lower()}" style="--n:{n}" '
                   f'href="#group-{g["letter"].lower()}" title="{html.escape(g["title"])}: {n} open">'
                   f'<b>{g["letter"]} · {n}</b><span>{html.escape(g["title"])}</span></a>')
    out.append('</nav><p class="bars-cap">Each bar is one group; its width is the number of open items.</p>')

    out.append('<div class="filters" role="group" aria-label="Filter by status">')
    out.append(f'<button type="button" class="chip" data-filter="all" aria-pressed="true">'
               f'All <span class="n">{len(items)}</span></button>')
    for s, label in STATUSES.items():
        if counts[s]:
            out.append(f'<button type="button" class="chip st-{s}" data-filter="{s}" aria-pressed="false">'
                       f'<span class="dot"></span>{label} <span class="n">{counts[s]}</span></button>')
    out.append('</div>')

    you = [i for i in items if i["status"] == "You"]
    out.append('<div class="top" data-all-only>')
    out.append('<div class="panel you"><h2>Waiting on you</h2><ul class="you-list">')
    for i in you:
        out.append(f'<li class="g-{i["group"].lower()}"><span class="id">{html.escape(i["id"])}</span>'
                   f'<span class="t"><a href="#{html.escape(i["id"])}">{e(i["item"])}</a></span>'
                   f'<span class="x">{e(i["next"])}</span></li>')
    out.append('</ul></div>')
    out.append(f'<div class="panel"><h2>{html.escape(plan["order_title"])}</h2><ol class="order">')
    for o in plan["order"]:
        out.append(f'<li><span>{links(e(o))}</span></li>')
    out.append('</ol>')
    for note in plan["order_notes"]:
        out.append(f'<p class="order-note">{e(note)}</p>')
    out.append('</div></div>')

    # 2026-10-03: the version phases, between the top panels and the groups.
    if plan["phases"]:
        out.append(f'<section data-all-only id="phases"><div class="ghead" style="--g:var(--st-you-fg)">'
                   f'<h2>{html.escape(plan["phases_title"])}</h2>'
                   f'<span class="gcount">{len(plan["phases"])} versions</span></div>')
        for note in plan["phase_notes"][:1]:
            out.append(f'<p class="order-note">{e(note)}</p>')
        out.append('<div class="done-wrap"><table><thead><tr><th>Version</th><th>Theme</th>'
                   '<th>Items</th></tr></thead><tbody>')
        for ver, theme, its in plan["phases"]:
            out.append(f'<tr><td><span class="mono">{e(ver)}</span></td><td>{e(theme)}</td>'
                       f'<td>{links(e(its))}</td></tr>')
        out.append('</tbody></table></div>')
        for note in plan["phase_notes"][1:]:
            out.append(f'<p class="aside">{links(e(note))}</p>')
        out.append('</section>')

    for g in plan["groups"]:
        gl = g["letter"].lower()
        out.append(f'<section class="group g-{gl}" id="group-{gl}">')
        out.append(f'<div class="ghead"><span class="gl">{g["letter"]}</span>'
                   f'<h2>{html.escape(g["title"])}</h2>'
                   f'<span class="gcount">{len(g["items"])} open</span></div><div class="cards">')
        for i in g["items"]:
            prio = '<span class="prio">Priority</span>' if i["priority"] else ""
            out.append(
                f'<article class="card" id="{html.escape(i["id"])}" data-status="{i["status"]}">'
                f'<div class="ctop"><span class="id">{html.escape(i["id"])}</span>'
                f'<span class="pill st-{i["status"]}">{STATUSES[i["status"]]}</span>{prio}</div>'
                f'<h3>{e(i["item"])}</h3>'
                f'<dl><dt>Why</dt><dd>{e(i["why"])}</dd>'
                f'<dt>Next step</dt><dd>{e(i["next"])}</dd></dl></article>')
        out.append('</div></section>')

    out.append(f'<section data-all-only id="done"><div class="ghead" style="--g:var(--st-ready-fg)">'
               f'<h2>Done</h2><span class="gcount">{len(plan["done"])} items off the older lists</span></div>')
    out.append('<div class="done-wrap"><table><thead><tr><th>Item</th><th>Where it was listed</th>'
               '<th>Evidence</th></tr></thead><tbody>')
    for item, where, ev in plan["done"]:
        out.append(f'<tr><td><span class="tick">✓</span>{e(item)}</td><td>{e(where)}</td><td>{e(ev)}</td></tr>')
    out.append('</tbody></table></div>')
    for n in plan["not_ours"]:
        out.append(f'<p class="aside"><strong>Not AnyCam work:</strong> {e(n)}</p>')
    out.append('</section>')

    # No generation date here: the plan's own date line (shown in the header)
    # is the date that matters, and a run date would make every render differ.
    out.append(f'<footer>Generated from <code>docs/BUILD_PLAN.md</code>. '
               f'To update: edit the Markdown, then run <code>python docs/render_build_plan.py</code>.</footer>')
    out.append(f'</div><script>{JS}</script>')
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    try:
        plan = parse(SRC.read_text(encoding="utf-8"))
    except PlanError as ex:
        print(f"BUILD_PLAN.md: {ex}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(plan), encoding="utf-8", newline="\n")
    n = sum(len(g["items"]) for g in plan["groups"])
    print(f"wrote {args.out}  ({n} open items in {len(plan['groups'])} groups, "
          f"{len(plan['done'])} done)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
