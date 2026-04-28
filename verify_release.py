#!/usr/bin/env python3
"""
AnyCam release verification script.
Run before every release: python3 verify_release.py

Checks (in order):
  1. Python syntax  (ast.parse)
  2. Semantic contracts — key functions contain required identifiers
  3. Best-practice audit — mutable defaults, blocking I/O in async, CSS // comments
  4. Version consistency — CURRENT_VERSION matches config.yaml
  5. Changelog — top entry matches CURRENT_VERSION

Exit 0 = all checks passed.
Exit 1 = one or more checks failed (details printed).
"""
import ast, re, sys, zipfile, pathlib

FAIL = []

def fail(msg: str) -> None:
    FAIL.append(msg)
    print(f"  ✗ {msg}")

def ok(msg: str) -> None:
    print(f"  ✓ {msg}")

src_path = pathlib.Path(__file__).parent / "camera_discovery.py"
cfg_path = pathlib.Path(__file__).parent / "config.yaml"
cl_path  = pathlib.Path(__file__).parent / "CHANGELOG.md"

src   = src_path.read_text()
lines = src.splitlines()

# ── 1. Syntax ─────────────────────────────────────────────────────────────────
print("\n[1/5] Syntax check")
try:
    tree = ast.parse(src)
    ok("ast.parse() passed")
except SyntaxError as e:
    fail(f"SyntaxError: {e}")
    sys.exit(1)

# Duplicate top-level definitions
import collections
top_names = [n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
dupes = [k for k, v in collections.Counter(top_names).items() if v > 1]
if dupes:
    fail(f"Duplicate top-level definitions: {dupes}")
else:
    ok(f"{len(top_names)} top-level definitions, no duplicates")

# ── 2. Semantic contracts ─────────────────────────────────────────────────────
print("\n[2/5] Semantic contract checks")
CONTRACTS = {
    "run_verification_scan": ["save_cameras", "SCAN_STATE", "run_scan"],
    "http_snap_loop":        ["asyncio.sleep", "_snap_state", "TCPConnector"],
    "snap_loop":             ["_snap_state", "asyncio"],
    "build_html":            ["INGRESS_PATH"],
    "make_app":              ["app.router", "web.Application"],
    "api_set_credentials":   ["save_cameras", "CAMERAS"],
    "main":                  ["_DockerIPFilter", "_probe_hw_decoders", "get_startup_mode"],
}
all_ok = True
for node in ast.walk(tree):
    if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
        if node.name in CONTRACTS:
            segment = ast.get_source_segment(src, node) or ""
            for req in CONTRACTS[node.name]:
                if req not in segment:
                    fail(f"{node.name}() missing required content: '{req}'")
                    all_ok = False
if all_ok:
    ok(f"All {len(CONTRACTS)} function contracts satisfied")

# ── 3. Best-practice audit ────────────────────────────────────────────────────
print("\n[3/5] Best-practice audit")
audit_ok = True

# P6: mutable default arguments (skip inner closures — they're intentional)
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for default in node.args.defaults + node.args.kw_defaults:
            if default and isinstance(default, (ast.List, ast.Dict, ast.Set)):
                fail(f"P6 Mutable default arg in {node.name}() at line {default.lineno}")
                audit_ok = False

# P4: blocking open() in async functions (top-level only, not closures)
for node in ast.walk(tree):
    if isinstance(node, ast.AsyncFunctionDef) and node.col_offset == 0:
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Name) and func.id == "open":
                    # Check it's not already wrapped in run_in_executor on same line
                    line_txt = lines[child.lineno - 1] if child.lineno <= len(lines) else ""
                    if "run_in_executor" not in line_txt and "lambda" not in line_txt:
                        fail(f"P4 Blocking open() in async def {node.name}() at line {child.lineno}")
                        audit_ok = False

# J3: // comments in CSS sections
css_start = next((i for i, l in enumerate(lines) if "css = f\"\"\"" in l or "<style>" in l), None)
if css_start:
    for i in range(css_start, min(css_start + 800, len(lines))):
        if lines[i].strip().startswith("//") and "http" not in lines[i]:
            fail(f"J3 CSS '//' comment at line {i+1} — use /* */ instead")
            audit_ok = False

# C1: display: flexbox typo
if "display:flexbox" in src.replace(" ", "") or "display: flexbox" in src:
    fail("C1 CSS typo 'display: flexbox' found — should be 'display: flex'")
    audit_ok = False

if audit_ok:
    ok("No best-practice violations found")

# ── 4. Version consistency ────────────────────────────────────────────────────
print("\n[4/5] Version consistency")
cv_match = re.search(r'CURRENT_VERSION\s*=\s*"(.+?)"', src)
cfg_match = re.search(r'^version:\s*"(.+?)"', cfg_path.read_text(), re.MULTILINE)
if not cv_match:
    fail("CURRENT_VERSION not found in camera_discovery.py")
elif not cfg_match:
    fail("version: not found in config.yaml")
else:
    cv = cv_match.group(1)
    cfg_v = cfg_match.group(1)
    if cv == cfg_v:
        ok(f"Version consistent: {cv}")
    else:
        fail(f"Version mismatch: CURRENT_VERSION={cv} vs config.yaml={cfg_v}")

# ── 5. Changelog ─────────────────────────────────────────────────────────────
print("\n[5/5] Changelog check")
if cv_match:
    version = cv_match.group(1)
    cl_text = cl_path.read_text()
    if cl_text.startswith(f"## {version}"):
        ok(f"CHANGELOG.md top entry matches {version}")
    else:
        top = cl_text.splitlines()[0] if cl_text.strip() else "(empty)"
        fail(f"CHANGELOG.md top entry '{top}' does not match version {version}")

# ── Result ────────────────────────────────────────────────────────────────────
print()
if FAIL:
    print(f"❌  {len(FAIL)} check(s) FAILED — do not release")
    sys.exit(1)
else:
    print("✅  All checks passed — safe to package")
    sys.exit(0)
