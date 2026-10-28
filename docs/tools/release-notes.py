"""Build a GitHub release note: the version's CHANGELOG.md entry, then the Install section.

    python docs/tools/release-notes.py 4.1.2 > notes.md
    gh release create 4.1.2 _build/anycam-4.1.2.zip --title 4.1.2 --notes-file notes.md

The Install section is docs/release-install-section.md, word for word, on every
release (CrystalHeeler, 2026-10-28). It was lost from 3.8.0-rc3 to 4.1.2 because
the notes were copied from the changelog alone.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def changelog_entry(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^## %s\s*$(.*?)(?=^## |\Z)" % re.escape(version), text, re.M | re.S)
    if not m:
        sys.exit("CHANGELOG.md has no entry for " + version)
    return m.group(1).strip()


def notes(body: str) -> str:
    install = (ROOT / "docs" / "release-install-section.md").read_text(encoding="utf-8").strip()
    return body.rstrip() + "\n\n" + install + "\n"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: release-notes.py <version>")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(notes(changelog_entry(sys.argv[1])))
