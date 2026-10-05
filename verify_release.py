#!/usr/bin/env python3
"""
AnyCam release verification script.
Run before every release: python3 verify_release.py

Checks (in order):
  1. Python syntax  (ast.parse + compile)
  2. Semantic contracts — key functions contain required identifiers
  3. Best-practice audit — mutable defaults, blocking I/O in async, CSS // comments,
     unfilled page placeholders
  4. Version consistency — CURRENT_VERSION matches config.yaml
  5. Changelog — top entry matches CURRENT_VERSION
  6. Settings — run.sh, config.yaml options/schema and translations agree (2.6.6)
  7. Build inputs — every Dockerfile pin exists where the build fetches it
     (2.6.6; needs internet access)

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

ROOT     = pathlib.Path(__file__).parent
src_path = ROOT / "camera_discovery.py"
cfg_path = ROOT / "config.yaml"
cl_path  = ROOT / "CHANGELOG.md"

# 3.0.0-rc1.0 (build plan E1): the add-on is several files, listed in
# anycam_modules.py. Each is compiled on its own; every other check reads
# them joined together, so a function is found whichever file holds it.
sys.path.insert(0, str(ROOT))
from anycam_modules import MODULES
_sources = {m: (ROOT / m).read_text(encoding="utf-8") for m in MODULES}
src   = "\n".join(_sources[m] for m in MODULES)
lines = src.splitlines()


def where(lineno: int) -> str:
    """file:line for a line number in the joined source."""
    for m in MODULES:
        n = _sources[m].count("\n") + 1
        if lineno <= n:
            return f"{m}:{lineno}"
        lineno -= n
    return f"line {lineno}"

# ── 1. Syntax ─────────────────────────────────────────────────────────────────
print("\n[1/8] Syntax check")
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
    for _m in MODULES:
        compile(_sources[_m], str(ROOT / _m), "exec")
    ok(f"compile() passed for {len(MODULES)} file(s): {', '.join(MODULES)}")
except SyntaxError as e:
    fail(f"compile-time SyntaxError: {e}")
    sys.exit(1)

# 3.0.0-rc1.0: the module list must match what the code imports and what the
# image copies. A file missing from the Dockerfile fails only on the device.
_local = {p.name for p in ROOT.glob("*.py")}
_imported = {"camera_discovery.py"}
for _m in MODULES:
    for _n in ast.walk(ast.parse(_sources[_m])):
        _names = ([a.name for a in _n.names] if isinstance(_n, ast.Import)
                  else [_n.module] if isinstance(_n, ast.ImportFrom) and _n.module else [])
        _imported |= {f"{x.split('.')[0]}.py" for x in _names} & _local
if _imported == set(MODULES):
    ok("anycam_modules.py matches the imports")
else:
    fail(f"anycam_modules.py lists {sorted(MODULES)} but the code imports {sorted(_imported)}")
# 3.0.0-rc1.1: a split module takes names from camera_discovery.py through
# its NEEDS list, copied once at start-up (anycam_host.bind). A copied name
# must exist, and must not be one that a function replaces with `global`:
# the copy would go stale. Such a value has to be read through H.
_main_tree = ast.parse(_sources["camera_discovery.py"])
_main_top = set()
for _n in _main_tree.body:
    if isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        _main_top.add(_n.name)
    elif isinstance(_n, (ast.Assign, ast.AnnAssign)):
        for _t in (_n.targets if isinstance(_n, ast.Assign) else [_n.target]):
            _main_top |= {e.id for e in ast.walk(_t) if isinstance(e, ast.Name)}
    elif isinstance(_n, (ast.Import, ast.ImportFrom)):
        _main_top |= {(a.asname or a.name).split(".")[0] for a in _n.names}
_replaced = {x for _n in ast.walk(_main_tree) if isinstance(_n, ast.Global) for x in _n.names}
_needs_ok, _needs_total = True, 0
for _m in MODULES[1:]:
    for _n in ast.parse(_sources[_m]).body:
        if (isinstance(_n, ast.Assign) and isinstance(_n.targets[0], ast.Name)
                and _n.targets[0].id == "NEEDS"):
            _names = ast.literal_eval(_n.value)
            _needs_total += len(_names)
            for _x in _names:
                if _x not in _main_top:
                    fail(f"{_m}: NEEDS name {_x!r} is not defined in camera_discovery.py")
                    _needs_ok = False
                elif _x in _replaced:
                    fail(f"{_m}: NEEDS name {_x!r} is replaced at run time; read it through H")
                    _needs_ok = False
    for _x in set(re.findall(r"(?<![\w.])H\.([A-Za-z_]\w*)", _sources[_m])):
        if _x not in _main_top:
            fail(f"{_m}: H.{_x} is not defined in camera_discovery.py")
            _needs_ok = False
# 3.0.0-rc1.2: the same rule between any two files. `from X import name`
# copies the value once; if X replaces that name with `global` while it
# runs, the importer keeps the old value. It must read X.name.
_replaced_in = {_m[:-3]: {x for _n in ast.walk(ast.parse(_sources[_m]))
                          if isinstance(_n, ast.Global) for x in _n.names} for _m in MODULES}
for _m in MODULES:
    for _n in ast.walk(ast.parse(_sources[_m])):
        if isinstance(_n, ast.ImportFrom) and _n.module in _replaced_in:
            for _a in _n.names:
                if _a.name in _replaced_in[_n.module]:
                    fail(f"{_m}: 'from {_n.module} import {_a.name}' copies a value that "
                         f"{_n.module}.py replaces at run time; read {_n.module}.{_a.name}")
                    _needs_ok = False
if _needs_ok:
    ok(f"{_needs_total} names taken from camera_discovery.py exist and are never replaced; "
       f"no file imports a name that its owner replaces")
_docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
_uncopied = [m for m in MODULES if not re.search(rf"^COPY\s+.*\b{re.escape(m)}\b", _docker, re.M)]
if _uncopied:
    fail(f"Dockerfile does not COPY: {_uncopied}")
else:
    ok("Dockerfile copies every module")

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
print("\n[2/8] Semantic contract checks")
CONTRACTS = {
    "run_verification_scan": ["save_cameras", "SCAN_STATE", "run_scan"],
    "http_snap_loop":        ["asyncio.sleep", "_snap_state", "TCPConnector"],
    "snap_loop":             ["_snap_state", "asyncio",
                              # 2.6.7: the classic view honours the escalated cooldown
                              "_acd_active(_snap_ip)", "ACD_ESCALATED_COOLDOWN",
                              "_throttle_wait_if_needed(_snap_ip"],
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
                              # 3.3.0 (C4): the RTSP server on 127.0.0.1, with a password
                              '"rtsp":   {"listen": f"{GO2RTC_API_HOST}:{GO2RTC_RTSP_PORT}"',
                              '"username": GO2RTC_RTSP_USER, "password": _GO2RTC_RTSP_PASS',
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
    # 2.6.6: motion detection. Both frame paths must feed the detector; the
    # detector must cancel brightness and contrast and tell light by spread;
    # a recording must be split, and started and stopped once each.
    "_motion_on_frame":      ["MOTION_COMPARE_S", "_motion_thumb", "_motion_feed",
                              'ms.get("detector_live")'],
    # 2.6.7: light hold, single-picture rejection, stream clock.
    "_motion_decide":        ["_motion_judge", "MOTION_LIGHT_HOLD_S", "MOTION_CONFIRM_S",
                              "MOTION_INSTANT_PCT", 'ms["recording"]', "repeat"],
    "_cam_file_tag":         ['camera.get("channel")'],
    # 3.0.0-rc1.0 (E4): credentials never reach a log line.
    "_redact":               ["_CRED_USERINFO", "_CRED_QUERY"],
    "_strip_creds":          ["[^/"],
    "_motion_feed":          ["MOTION_REF_S", "_motion_decide", "_start_recording", "stream_t",
                              "_motion_tick"],
    "_motion_tick":          ["_stop_recording", "_motion_quiet"],
    # 2.6.6: live-stream detection, and the pre-roll buffer recordings start from.
    "_motion_detector":      ["MOTION_DETECT_FPS", "rawvideo", "_motion_feed",
                              '"detector_live"', "_stop_proc",
                              "format=yuv420p", "_motion_night_observe"],
    "_motion_buffer":        ['"-c:v", "copy"', "_TsBuffer()", "rec_queue",
                              "_motion_write", "_stop_proc"],
    # 3.4.0 (C17): the comparison moved to _motion_cells, which also flags each cell
    "_motion_cells":         ["MOTION_PIXEL_DELTA", "MOTION_REGIONS", "/ sa", "/ sc"],
    "_motion_diff":          ["_motion_cells(prev, curr)"],
    "_motion_judge":         ["MOTION_LIGHT_FRACTION", "_motion_area_now(camera_id)"],
    # 2.6.7: night boost (C15).
    "_motion_area_now":      ["_motion_cfg(camera_id)", "_motion_boost(camera_id)"],
    "_motion_area_pct":      ["MOTION_AREA_FLOOR"],
    "_motion_night_observe": ["NIGHT_HOLD_S", "NIGHT_CHROMA_MAX", "DAY_CHROMA_MIN",
                              "NIGHT_GAP_S"],
    "_night_expectation_check": ["NIGHT_WINDOW_S", "_NIGHT_CHECKED", "_ha_notify",
                                 "observed_since"],
    "_ha_api":               ["SUPERVISOR_TOKEN", "http://supervisor/core/api/"],
    "_ha_location_refresh":  ['"config"', "HA_LOC_REFRESH_S", "_ha_location_plausible",
                              '_HA_LOC_STATE["mismatch"]'],
    "_ha_location_plausible": ["HA_LOC_MAX_LON_DIFF", "_tz_std_offset_h"],
    # 2.6.6: per-camera settings; the global override wins when it is on.
    "_motion_cfg":           ["CFG_MOTION_GLOBAL", "_MOTION_CFG", "MOTION_DEFAULTS"],
    "_motion_validate":      ["MOTION_PATH_ROOT", "MOTION_CLIP_CHOICES"],
    "_start_recording":      ['"segment"', "_motion_cfg(camera_id)", "_part%02d.mp4",
                              "_cam_file_tag(camera)",
                              "buf.preroll()", 'ms["rec_queue"]',
                              'ms["recording"] = True', "_drain_stderr"],
    "_stop_recording":       ['ms.get("stopping")', 'ms["stopping"] = True'],
    # 2.6.6: live cards stay within the width a phone can decode.
    "_go2rtc_card_source":   ["CARD_MAX_WIDTH", "_go2rtc_relay_url", "_dahua_sub_stream"],
    # 2.6.6 (B9): never signal an ffmpeg that is already exiting.
    "_stop_proc":            ["exited_grace", "proc.returncode is None"],
}
# Frame paths that must call the motion detector (checked below).
MOTION_CALLERS = ("snap_loop", "http_snap_loop")
all_ok = True
found_contracts = set()
for node in ast.walk(tree):
    # 2.6.6: before 2.6.5 motion was checked on the ffmpeg path only, and the
    # Lorex channels, which poll HTTP snapshots, recorded nothing.
    if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name in MOTION_CALLERS:
        if "_motion_on_frame(" not in (ast.get_source_segment(src, node) or ""):
            fail(f"{node.name}() no longer calls _motion_on_frame — motion detection would miss that path")
            all_ok = False
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
print("\n[2b/8] rc2 CAMERA_DB throttle field validation")

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
print("\n[3/8] Best-practice audit")
audit_ok = True

# P6: mutable default arguments (skip inner closures — they're intentional)
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for default in node.args.defaults + node.args.kw_defaults:
            if default and isinstance(default, (ast.List, ast.Dict, ast.Set)):
                fail(f"P6 Mutable default arg in {node.name}() at {where(default.lineno)}")
                audit_ok = False

# P4: blocking file I/O in async functions (top-level only, not closures).
# 2.6.6 (build plan F2): pathlib's read_text/read_bytes/write_text/write_bytes
# block the event loop exactly like open(), and the 2.6.3 audit found one by
# hand that this check had missed.
_BLOCKING_PATH_IO = {"read_text", "read_bytes", "write_text", "write_bytes"}
for node in ast.walk(tree):
    if isinstance(node, ast.AsyncFunctionDef) and node.col_offset == 0:
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func = child.func
                what = None
                if isinstance(func, ast.Name) and func.id == "open":
                    what = "open()"
                elif isinstance(func, ast.Attribute) and func.attr in _BLOCKING_PATH_IO:
                    what = f".{func.attr}()"
                if what:
                    # Check it's not already wrapped in an executor on the same line
                    line_txt = lines[child.lineno - 1] if child.lineno <= len(lines) else ""
                    if not any(w in line_txt for w in ("run_in_executor", "to_thread", "lambda")):
                        fail(f"P4 Blocking {what} in async def {node.name}() at {where(child.lineno)}")
                        audit_ok = False

# P7: every ___NAME___ placeholder in the page script is filled by build_html.
# 2.6.6 (build plan B5): ___COMMUNITY___ never was, so the page read the
# placeholder text itself as a configured community endpoint.
_placeholders = set(re.findall(r"___[A-Z_]+___", src))
_filled = set(re.findall(r"\.replace\(\s*'(___[A-Z_]+___)'", src))
for _ph in sorted(_placeholders - _filled):
    fail(f"P7 Page placeholder {_ph} is never replaced in build_html")
    audit_ok = False

# J3: // comments in CSS sections
css_start = next((i for i, l in enumerate(lines) if "css = f\"\"\"" in l or "<style>" in l), None)
if css_start:
    for i in range(css_start, min(css_start + 800, len(lines))):
        if lines[i].strip().startswith("//") and "http" not in lines[i]:
            fail(f"J3 CSS '//' comment at {where(i+1)} — use /* */ instead")
            audit_ok = False

# C1: display: flexbox typo
if "display:flexbox" in src.replace(" ", "") or "display: flexbox" in src:
    fail("C1 CSS typo 'display: flexbox' found — should be 'display: flex'")
    audit_ok = False

if audit_ok:
    ok("No best-practice violations found")

# ── 4. Version consistency ────────────────────────────────────────────────────
print("\n[4/8] Version consistency")
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
print("\n[5/8] Changelog check")
if cv_match:
    version = cv_match.group(1)
    cl_text = cl_path.read_text(encoding="utf-8")
    if cl_text.startswith(f"## {version}"):
        ok(f"CHANGELOG.md top entry matches {version}")
    else:
        top = cl_text.splitlines()[0] if cl_text.strip() else "(empty)"
        fail(f"CHANGELOG.md top entry '{top}' does not match version {version}")

# ── 6. Settings consistency (2.6.6, build plan F7) ──────────────────────────
# run.sh hands each setting to the program with bashio::config. For a setting
# config.yaml does not define, bashio returns the text "null", which the code
# reads as off: removing the Live View option in 2.6.4 nearly shipped exactly
# that. The four lists must name the same settings.
print("\n[6/8] Settings consistency (run.sh, config.yaml, translations)")


def _yaml_block_keys(text: str, block: str) -> set:
    """Keys indented two spaces under a top-level 'block:' line."""
    keys, inside = set(), False
    for ln in text.splitlines():
        if re.match(rf"^{block}:\s*$", ln):
            inside = True
            continue
        if inside:
            if ln and not ln.startswith(" "):
                break
            m = re.match(r"^  ([a-z0-9_]+):", ln)
            if m:
                keys.add(m.group(1))
    return keys


_root = pathlib.Path(__file__).parent
_run_reads = set(re.findall(r"bashio::config '([a-z0-9_]+)'",
                            (_root / "run.sh").read_text(encoding="utf-8")))
_opts   = _yaml_block_keys(_cfg_text, "options")
_schema = _yaml_block_keys(_cfg_text, "schema")
_trans  = _yaml_block_keys((_root / "translations" / "en.yaml").read_text(encoding="utf-8"),
                           "configuration")
settings_ok = True
for k in sorted(_run_reads - _opts):
    fail(f"F7 run.sh reads '{k}', which config.yaml does not define (bashio would return null)")
    settings_ok = False
for k in sorted(_opts - _run_reads):
    fail(f"F7 config.yaml defines '{k}', which run.sh never reads")
    settings_ok = False
for k in sorted(_opts ^ _schema):
    fail(f"F7 '{k}' is in only one of config.yaml options and schema")
    settings_ok = False
for k in sorted(_opts - _trans):
    fail(f"F7 option '{k}' has no entry in translations/en.yaml")
    settings_ok = False
for k in sorted(_trans - _opts):
    fail(f"F7 translations/en.yaml describes '{k}', which config.yaml does not define")
    settings_ok = False
if settings_ok:
    ok(f"{len(_opts)} settings agree across run.sh, config.yaml options and "
       f"schema, and translations")

# ── 7. Build inputs (2.6.6, build plan F1) ───────────────────────────────────
# 2.6.1 passed every check above and still could not build: an apt pin had
# rotated out of the archive. Every pinned input the Dockerfile fetches is
# looked up where the build will fetch it. Needs internet access; with none,
# this gate fails rather than pass unchecked.
print("\n[7/8] Build inputs (network)")
import json as _json
import urllib.error
import urllib.request

_docker = (_root / "Dockerfile").read_text(encoding="utf-8")


def _get(url: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "anycam-verify-release",
                                               **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def _check_build_inputs() -> bool:
    good = True
    # apt pins: name=version inside the RUN block
    apt = re.findall(r"^\s+([a-z0-9][a-z0-9.+-]*)=([0-9][^\s\\]*)\s*\\?$", _docker, re.MULTILINE)
    if not apt:
        fail("F1 found no apt pins in the Dockerfile — the parser needs updating")
        return False
    names = "+".join(n for n, _ in apt)
    madison = _get("https://qa.debian.org/madison.php?package=" + names +
                   "&table=debian&s=bookworm,bookworm-updates,bookworm-security"
                   "&a=all,amd64,arm64&text=on").decode()
    for name, ver in apt:
        archs = set()
        for row in madison.splitlines():
            cols = [c.strip() for c in row.split("|")]
            if len(cols) == 4 and cols[0] == name and cols[1] == ver:
                archs |= {a.strip() for a in cols[3].split(",")}
        if "all" in archs or {"amd64", "arm64"} <= archs:
            ok(f"apt {name}={ver} is in Debian bookworm")
        else:
            fail(f"F1 apt {name}={ver} not found in Debian bookworm for amd64 and arm64")
            good = False
    # pip pins: a wheel for CPython 3.11 on manylinux aarch64 and x86_64
    for name, ver in re.findall(r"^\s+([A-Za-z0-9_.-]+)==([0-9][^\s\\]*)", _docker, re.MULTILINE):
        files = [u["filename"] for u in
                 _json.loads(_get(f"https://pypi.org/pypi/{name}/{ver}/json"))["urls"]]
        def has(arch: str) -> bool:
            return any(f.endswith(".whl") and "manylinux" in f and f"_{arch}" in f
                       and re.search(r"-(cp311-cp311|cp3\d+-abi3|py3-none)-", f)
                       for f in files)
        # 3.6.0: a pure-Python wheel (asyncssh) installs on every platform
        if any(f.endswith("-py3-none-any.whl") for f in files) or (has("aarch64") and has("x86_64")):
            ok(f"pip {name}=={ver} has CPython 3.11 wheels for aarch64 and x86_64")
        else:
            fail(f"F1 pip {name}=={ver}: no CPython 3.11 manylinux wheel for both "
                 f"aarch64 and x86_64")
            good = False
    # go2rtc: the pinned digests match the release assets
    ver = re.search(r"^ARG GO2RTC_VERSION=(\S+)", _docker, re.MULTILINE).group(1)
    rel = _json.loads(_get(f"https://api.github.com/repos/AlexxIT/go2rtc/releases/tags/{ver}"))
    digests = {a["name"]: (a.get("digest") or "") for a in rel["assets"]}
    for arch in ("ARM64", "AMD64"):
        pinned = re.search(rf"^ARG GO2RTC_SHA256_{arch}=(\S+)", _docker, re.MULTILINE).group(1)
        if digests.get(f"go2rtc_linux_{arch.lower()}") == f"sha256:{pinned}":
            ok(f"go2rtc {ver} linux_{arch.lower()} digest matches the release")
        else:
            fail(f"F1 go2rtc {ver} linux_{arch.lower()} digest differs from the release")
            good = False
    # base image: the multi-arch index has both platforms
    base = re.search(r"^ARG BUILD_FROM=(\S+)", _docker, re.MULTILINE)
    if not base:
        fail("F1 Dockerfile has no default BUILD_FROM (build plan F4)")
        return False
    m = re.match(r"ghcr\.io/([^:]+):(\S+)", base.group(1))
    token = _json.loads(_get(f"https://ghcr.io/token?scope=repository:{m.group(1)}:pull"))["token"]
    index = _json.loads(_get(
        f"https://ghcr.io/v2/{m.group(1)}/manifests/{m.group(2)}",
        {"Authorization": f"Bearer {token}",
         "Accept": "application/vnd.oci.image.index.v1+json, "
                   "application/vnd.docker.distribution.manifest.list.v2+json"}))
    plats = {(p["platform"]["os"], p["platform"]["architecture"])
             for p in index.get("manifests", []) if "platform" in p}
    if {("linux", "arm64"), ("linux", "amd64")} <= plats:
        ok(f"base image {base.group(1)} has linux/arm64 and linux/amd64")
    else:
        fail(f"F1 base image {base.group(1)} lacks linux/arm64 or linux/amd64")
        good = False
    return good


try:
    _check_build_inputs()
except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as e:
    fail(f"F1 could not check the build inputs ({e}) — needs internet access")

# ── 8. Tests ─────────────────────────────────────────────────────────────────
# 3.0.0-rc1.0 (build plan E7): the behaviour tests live in tests/ and must
# pass before a release. They need Node.js for the page checks; a missing
# Node.js fails the gate (CLAUDE.md rule 4: a missing tool means stop).
print("\n[8/8] Tests (tests/run_tests.py)")
import subprocess as _subprocess
_tests = pathlib.Path(__file__).parent / "tests" / "run_tests.py"
if not _tests.exists():
    fail("E7 tests/run_tests.py is missing")
else:
    try:
        _run = _subprocess.run([sys.executable, str(_tests)], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=1500)
        _lines = [l.strip() for l in (_run.stdout + _run.stderr).splitlines() if l.strip()]
        for _l in _lines:
            if _l.endswith("passed"):
                ok(_l)
        if _run.returncode != 0:
            for _l in _lines:
                if "FAIL" in _l or "Traceback" in _l or "Error" in _l:
                    print(f"    {_l}")
            fail("E7 the tests did not all pass")
    except _subprocess.TimeoutExpired:
        fail("E7 the tests did not finish in 25 minutes")

# ── Result ────────────────────────────────────────────────────────────────────
print()
if FAIL:
    print(f"❌  {len(FAIL)} check(s) FAILED — do not release")
    sys.exit(1)
else:
    print("✅  All checks passed — safe to package")
    sys.exit(0)
