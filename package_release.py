"""Package an AnyCam release per CLAUDE.md rule 3.

    python package_release.py

The zip name carries the full version; the single top-level folder is always
anycam. Entries use POSIX separators: HAOS extracts on Linux.
Run verify_release.py first. Writes _build/anycam-<version>.zip.
"""
import pathlib
import re
import sys
import zipfile

from anycam_modules import MODULES

ROOT = pathlib.Path(__file__).resolve().parent
TOP = "anycam"      # CLAUDE.md rule 3: fixed, no version
FILES = ["run.sh", *MODULES, "Dockerfile", "CHANGELOG.md", "config.yaml",
         "verify_release.py", "anycam_modules.py", "translations/en.yaml",
         "www/video-rtc.js"]


def main() -> int:
    version = re.search(r'^version:\s*"(.+?)"',
                        (ROOT / "config.yaml").read_text(encoding="utf-8"), re.MULTILINE).group(1)
    out = ROOT / "_build" / f"anycam-{version}.zip"
    out.parent.mkdir(exist_ok=True)
    if out.exists():
        out.unlink()
    dirs_added = set()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo(f"{TOP}/"), b"")
        for f in FILES:
            parent = f.rsplit("/", 1)[0] if "/" in f else None
            if parent and parent not in dirs_added:
                z.writestr(zipfile.ZipInfo(f"{TOP}/{parent}/"), b"")
                dirs_added.add(parent)
            z.write(ROOT / f, f"{TOP}/{f}")
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        bad = [n for n in names if "\\" in n]
        crlf = [n for n in names if not n.endswith("/") and b"\r\n" in z.read(n)]
    print(f"created {out.name}  {out.stat().st_size / 1024:.1f} KB  entries={len(names)}")
    for n in names:
        print("  ", n)
    print(f"backslash entries: {len(bad)}   files containing CRLF: {crlf or 'none'}")
    return 1 if bad or crlf else 0


if __name__ == "__main__":
    sys.exit(main())
