"""Run every AnyCam test: the server checks, then the page checks.

    python tests/run_tests.py

The page checks need Node.js. The page's script is taken from the real
page, as camera_discovery.build_html() builds it. Exit code 0 = all passed.
The release gate (verify_release.py, gate 8) runs this file.
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent


def _tail(text: str, pattern: str) -> list[str]:
    return [line for line in text.splitlines() if re.search(pattern, line)]


def run_server() -> bool:
    print("== server checks (tests/test_server.py)", flush=True)
    r = subprocess.run([sys.executable, str(HERE / "test_server.py")], cwd=REPO,
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONUTF8": "1"}, timeout=900)
    for line in _tail(r.stdout + r.stderr, r"FAIL|passed$|Traceback|Error:"):
        print("  " + line.strip())
    return r.returncode == 0


def build_page_js(out: Path) -> None:
    os.environ.setdefault("INGRESS_PATH", "/api/hassio_ingress/TOKEN")
    sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location("cd", REPO / "camera_discovery.py")
    cd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cd)
    html = cd.build_html()
    out.write_text(re.search(r"<script>\n(.*?)\n</script>", html, re.S).group(1),
                   encoding="utf-8")


def run_page() -> bool:
    print("== page checks (tests/test_page.mjs)", flush=True)
    node = shutil.which("node")
    if not node:
        print("  FAIL  Node.js is not installed; the page checks cannot run")
        return False
    with tempfile.TemporaryDirectory(prefix="anycam-page-") as tmp:
        page_js = Path(tmp) / "page.js"
        build_page_js(page_js)
        syntax = subprocess.run([node, "--check", str(page_js)], capture_output=True, text=True)
        if syntax.returncode:
            print("  FAIL  the page script does not parse:\n" + syntax.stderr[:2000])
            return False
        r = subprocess.run([node, str(HERE / "test_page.mjs"), str(page_js),
                            str(REPO / "www" / "video-rtc.js")],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=300)
    for line in _tail(r.stdout + r.stderr, r"FAIL|passed$|Error"):
        print("  " + line.strip())
    return r.returncode == 0


def run_names() -> bool:
    print("== undefined names (tests/check_names.py)", flush=True)
    r = subprocess.run([sys.executable, str(HERE / "check_names.py")], cwd=REPO,
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONUTF8": "1"}, timeout=300)
    for line in _tail(r.stdout + r.stderr, r"FAIL|names:|Traceback|Error:"):
        print("  " + line.strip())
    return r.returncode == 0


if __name__ == "__main__":
    results = [run_names(), run_server(), run_page()]
    print("ALL TESTS PASSED" if all(results) else "TESTS FAILED")
    sys.exit(0 if all(results) else 1)
