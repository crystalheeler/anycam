#!/usr/bin/env python3
"""
AnyCam release verification script.
Run before every release: python3 verify_release.py

Checks (in order):
  1. Python syntax  (ast.parse + compile)
  2. Semantic contracts — key functions contain required identifiers
  3. Best-practice audit — mutable defaults, blocking I/O in async, CSS // comments
  4. Version consistency — CURRENT_VERSION matches config.yaml
  5. Changelog — top entry matches CURRENT_VERSION

Exit 0 = all checks passed.
Exit 1 = one or more checks failed (details printed).
"""
import ast, re, sys, zipfile, pathlib

# 2.6.1: force UTF-8 on stdout. The check marks are U+2713 / U+2717, which a
# Windows console cannot encode in its cp1252 default, so every ok() call
# raised UnicodeEncodeError and the gate could not run outside Linux. The
# source reads below pass encoding="utf-8" for the same reason — Python picks
# the locale encoding when the argument is omitted, and camera_discovery.py
# holds non-ASCII bytes.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass   # pre-3.7 or a stream that does not support reconfigure

FAIL = []

def fail(msg: str) -> None:
    FAIL.append(msg)
    print(f"  ✗ {msg}")

def ok(msg: str) -> None:
    print(f"  ✓ {msg}")

src_path = pathlib.Path(__file__).parent / "camera_discovery.py"
cfg_path = pathlib.Path(__file__).parent / "config.yaml"
cl_path  = pathlib.Path(__file__).parent / "CHANGELOG.md"

src   = src_path.read_text(encoding="utf-8")
lines = src.splitlines()

# ── 1. Syntax ─────────────────────────────────────────────────────────────────
print("\n[1/5] Syntax check")
try:
    tree = ast.parse(src)
    ok("ast.parse() passed")
except SyntaxError as e:
    fail(f"SyntaxError: {e}")
    sys.exit(1)

# 2.4.0-rc2.7: also run compile(). ast.parse() catches grammar errors but
# does NOT catch a category of compile-time errors including:
#   • `global X` declared after X is already used in the same function
#   • `nonlocal X` with no enclosing binding
#   • duplicate keyword args in a call
# Live install of rc2.6 crashed with "name 'PENDING_CAMERAS' is used
# prior to global declaration" because the gate only ran ast.parse() —
# that error is raised by the bytecode compiler, not the parser. Adding
# compile() means any future repro of this class lands at gate-time
# instead of HAOS-install-time.
try:
    compile(src, str(src_path), "exec")
    ok("compile() passed")
except SyntaxError as e:
    fail(f"compile-time SyntaxError: {e}")
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
    "make_app":              ["app.router", "web.Application", "_on_shutdown"],
    "api_set_credentials":   ["save_cameras", "CAMERAS"],
    "main":                  ["_DockerIPFilter", "_probe_hw_decoders",
                              "get_startup_mode", "_STOP_EVENT",
                              "runner.cleanup", "add_signal_handler"],
    "_on_shutdown":          ["_SNAP", "last_frame_wall", "save_cameras",
                              "_THREAD_POOL.shutdown", "terminate"],
    "probe_rtsp_socket":     ["DESCRIBE", "SETUP", "TEARDOWN",
                              "_parse_track_url", "m=video", "cnonce"],
    # 2.2.9 — _safe_cam strips creds from stream_profiles[].url and
    # stream_profile_N_url top-level keys (rc2.x leak)
    "_safe_cam":             ["_strip_creds", "stream_profiles",
                              "stream_profile_"],
    # rc2 — single-socket Layer 1 walker bounded by RFC 2326 §9.1 semantics
    # rc2.1 — additionally tracks looks_like_rtsp for Layer 2 fast-bail
    # rc2.1.1 — captures Server: header into host_meta for post-walk brand-id
    # 2.2.9 — _build_auth is qop-aware (RFC 2617 §3.2.2)
    "_probe_rtsp_paths_single_socket": ["DESCRIBE", "SETUP", "TEARDOWN",
                                         "next_cseq", "auth_val", "extra_query",
                                         "looks_like_rtsp", "captured_server",
                                         "host_meta", "cnonce"],
    # rc2 — find_rtsp_path orchestrator with brand-aware short-circuits
    # rc2.1 — adds Layer 2 fast-bail when looks_like_rtsp is False
    # rc2.1.1 — direct _identify_camera_brand call + post-walk re-id pass
    "find_rtsp_path":        ["_probe_rtsp_paths_single_socket",
                              "rate_limit_per_ip_tcp", "no_rtsp_support",
                              "session_time_cap", "requires_query_param",
                              "Layer 2", "looks_like_rtsp",
                              "_identify_camera_brand", "post-walk"],
    # rc2 — brand-id helper that wires mac_vendor into manufacturer detection
    "_identify_camera_brand": ["mac_vendor", "manufacturer",
                               "identify_manufacturer"],
    # 2.3.0 — sibling walker that validates a list of full URLs over one
    # TCP socket. Used by the cred-auth handler to replace per-profile
    # probe_rtsp loops that triggered Hipcam-family firmware lockout.
    "_validate_rtsp_urls_single_socket": ["DESCRIBE", "SETUP", "TEARDOWN",
                                           "next_cseq", "auth_val",
                                           "host_meta", "captured_server",
                                           "cnonce", "results"],
    # 2.3.0 — throttle helpers
    "_parse_throttle_seconds": ["amount_str"],
    "_brand_throttle_seconds": ["_identify_camera_brand",
                                 "rate_limit_per_ip_tcp"],
    "_throttle_wait_if_needed": ["_THROTTLE_TRACK", "asyncio.sleep",
                                  "monotonic"],
    # 2.3.0 — _probe_db_streams refactored to use single-socket walker
    "_probe_db_streams":     ["_validate_rtsp_urls_single_socket",
                              "_throttle_wait_if_needed"],
    # 2.6.3 — go2rtc security controls. go2rtc's API can add an `exec:`
    # source and run commands on the host, and this addon has full_access,
    # so each control below is pinned to fail the release if it is removed.
    # Exact module allowlist: adding exec, echo, expr or ffmpeg fails here.
    "_go2rtc_config":        ['{"modules": ["api", "ws", "rtsp", "webrtc", "mp4"]}',
                              '"rtsp":   {"listen": ""}',
                              "GO2RTC_API_HOST"],
    # Inline config: a file path would let go2rtc write camera passwords to disk.
    "_go2rtc_supervisor":    ['"-config", _go2rtc_config()'],
    # Proxy forwards /api/ws only, only for names AnyCam registered, and
    # honours the brand cooldown before go2rtc dials the camera.
    "handle_go2rtc_ws":      ["name in _GO2RTC_STREAMS", "/api/ws?src=",
                              "_throttle_wait_if_needed"],
    # Treats the no-config-file 400 as success; encodes the password-bearing
    # source so '&' and '+' survive Go's query parser.
    "_go2rtc_register":      ['"config file disabled"', "quote(src, safe='')"],
    # Motion detection runs inside the thumbnail snap_loop: keep it alive when
    # armed, and stop it through the flag path only when a live ffmpeg exists.
    "_focus_set_go2rtc":     ["_MOTION.get(camera_id)", 'ms.get("enabled")',
                              'state["focus_leave_kill"] = True'],
    # The go2rtc exit must drop an unconsumed flag (2.4.0-rc3.3 Bug A shape).
    "handle_focus_clear":    ["_FOCUS_ENGINE",
                              'state.pop("focus_leave_kill", None)'],
}
all_ok = True
found_contracts = set()
for node in ast.walk(tree):
    if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
        if node.name in CONTRACTS:
            found_contracts.add(node.name)
            segment = ast.get_source_segment(src, node) or ""
            for req in CONTRACTS[node.name]:
                if req not in segment:
                    fail(f"{node.name}() missing required content: '{req}'")
                    all_ok = False
# 2.6.3: a contracted function that no longer exists is a failure. Before
# this, deleting a function outright skipped its contract and passed.
for name in sorted(set(CONTRACTS) - found_contracts):
    fail(f"{name}() is under contract but was not found")
    all_ok = False
if all_ok:
    ok(f"All {len(CONTRACTS)} function contracts satisfied")

# 2.6.3: go2rtc's API address is a module constant, not inside a function,
# so the contract mechanism cannot see it. Check it directly.
_host = re.search(r'^GO2RTC_API_HOST\s*=\s*"([^"]+)"', src, re.MULTILINE)
if _host and _host.group(1) == "127.0.0.1":
    ok("go2rtc API bound to 127.0.0.1")
else:
    fail('GO2RTC_API_HOST must be "127.0.0.1" — anyone who reaches '
         "go2rtc's API can run commands on the host")

# ── 2b. rc2 CAMERA_DB structure contracts ─────────────────────────────────────
# These checks pull CAMERA_DB out of the AST (not by importing) so they don't
# need any runtime deps installed.
print("\n[2b/5] rc2 CAMERA_DB throttle field validation")

VALID_THROTTLE_TYPES = {
    "rate_limit_per_ip_tcp",
    "concurrent_user_cap",
    "concurrent_stream_cap",
    "session_time_cap",
    "restart_cooldown",
    "socket_close_after_play",
    "unique_profile_cap",
    "shared_session_id_required",
    "requires_query_param",
    "requires_custom_firmware",
    "unstable_rtsp",
    "no_rtsp_support",
    "auth_attempt_lockout",         # 2.4.0: Lorex/Dahua DVR-NVR family
                                    # (lockout after N failed Digest auth)
}
VALID_CONFIDENCE = {"HIGH", "MED", "LOW"}

# Pull the CAMERA_DB literal out of the AST — find the Assign node that
# binds CAMERA_DB to a list literal of dict literals.
camera_db_node = None
for node in ast.iter_child_nodes(tree):
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
            and node.target.id == "CAMERA_DB":
        camera_db_node = node.value
        break
    if isinstance(node, ast.Assign):
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id == "CAMERA_DB":
                camera_db_node = node.value
                break
        if camera_db_node:
            break

if not camera_db_node or not isinstance(camera_db_node, ast.List):
    fail("CAMERA_DB literal not found at module top level")
else:
    # Walk each entry and validate
    rc2_throttle_ok = True
    rc2_pair_ok = True
    entries_with_throttle = 0
    entries_total = len(camera_db_node.elts)

    DATA_FIELDS = ("throttle_type", "throttle_amount",
                   "throttle_notes", "request_behaviors",
                   # 2.4.0-rc1.0: rtsp_realm_regex is a new optional
                   # CAMERA_DB field used by _identify_camera_brand to
                   # match brands by RTSP Digest realm pattern. Optional
                   # like the others — only entries with high-confidence
                   # known realm patterns populate it (currently just
                   # the Lorex/Dahua DVR-NVR Family).
                   "rtsp_realm_regex",
                   # 2.4.0-rc2.0: streaming_recipe is a new optional
                   # CAMERA_DB field describing brand-specific path
                   # generation (channel iteration, profile selection,
                   # etc.) for multi-channel NVR/DVR boxes. Populated
                   # in rc2.0 for 9 NVR/DVR families; consumed in rc3.x.
                   "streaming_recipe",
                   # 2.4.0-rc2.2: skip_layer2 is a boolean flag set on
                   # multi-channel NVR/DVR families that should NOT use
                   # Layer 2 (multi-socket fallback) RTSP probing —
                   # they need streaming_recipe channel iteration
                   # instead. Consumer in find_rtsp_path was added in
                   # rc2.1; this field is the data-side companion that
                   # actually enables the short-circuit.
                   "skip_layer2")
    DATA_FIELD_SET = set(DATA_FIELDS)

    for idx, elt in enumerate(camera_db_node.elts):
        if not isinstance(elt, ast.Dict):
            continue
        # Build a {key: value_node} dict for this entry
        kv = {}
        for k, v in zip(elt.keys, elt.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                kv[k.value] = v

        entry_name = ""
        n_node = kv.get("name")
        if isinstance(n_node, ast.Constant) and isinstance(n_node.value, str):
            entry_name = n_node.value

        # Contract: throttle_type values must be in the valid enum
        tt_node = kv.get("throttle_type")
        if tt_node is not None:
            entries_with_throttle += 1
            if isinstance(tt_node, ast.Constant) and isinstance(tt_node.value, str):
                if tt_node.value not in VALID_THROTTLE_TYPES:
                    fail(f"rc2-throttle-fields-valid: '{entry_name}' has "
                         f"throttle_type='{tt_node.value}' not in valid enum")
                    rc2_throttle_ok = False
            else:
                fail(f"rc2-throttle-fields-valid: '{entry_name}' throttle_type "
                     f"is not a string literal")
                rc2_throttle_ok = False

        # Contract: every <field>_confidence has a matching <field>, and
        # every <field> in DATA_FIELDS has a matching <field>_confidence,
        # and every confidence value is HIGH/MED/LOW.
        for field in DATA_FIELDS:
            has_data = field in kv
            has_conf = (field + "_confidence") in kv
            if has_data and not has_conf:
                fail(f"rc2-confidence-fields-paired: '{entry_name}' has "
                     f"'{field}' but no '{field}_confidence'")
                rc2_pair_ok = False
            if has_conf and not has_data:
                fail(f"rc2-confidence-fields-paired: '{entry_name}' has "
                     f"'{field}_confidence' but no '{field}'")
                rc2_pair_ok = False
            if has_conf:
                cf_node = kv[field + "_confidence"]
                if isinstance(cf_node, ast.Constant) and isinstance(cf_node.value, str):
                    if cf_node.value not in VALID_CONFIDENCE:
                        fail(f"rc2-confidence-fields-paired: '{entry_name}' "
                             f"{field}_confidence='{cf_node.value}' not in "
                             f"{{HIGH,MED,LOW}}")
                        rc2_pair_ok = False

        # No stray confidence fields outside DATA_FIELDS
        for k in kv:
            if k.endswith("_confidence"):
                base = k[:-len("_confidence")]
                if base not in DATA_FIELD_SET:
                    fail(f"rc2-confidence-fields-paired: '{entry_name}' has "
                         f"unexpected '{k}' (no matching data field in "
                         f"{DATA_FIELDS})")
                    rc2_pair_ok = False

    if rc2_throttle_ok:
        ok(f"rc2-throttle-fields-valid: all throttle_type values in valid "
           f"enum ({entries_with_throttle} of {entries_total} entries have "
           f"throttle data)")
    if rc2_pair_ok:
        ok(f"rc2-confidence-fields-paired: every data field has its "
           f"_confidence sibling and vice versa, all confidence values "
           f"in {{HIGH,MED,LOW}}")

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
_cfg_text = cfg_path.read_text(encoding="utf-8")
cfg_match = re.search(r'^version:\s*"(.+?)"', _cfg_text, re.MULTILINE)
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
    cl_text = cl_path.read_text(encoding="utf-8")
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
