"""Password entry: the camera password endpoints, the stream database probe, the DVR channel list.

Moved out of camera_discovery.py in 3.0.0-rc1.5 (build plan E1); the function
bodies are unchanged.

This file cannot import camera_discovery.py (see anycam_host.py). The names in
NEEDS are set on this module at start-up; H reads a camera_discovery.py
value at the moment of use.
"""
import asyncio
import datetime
import logging
import re
import time
from aiohttp import web
from urllib.parse import urlparse, quote

from anycam_host import H
from anycam_brand import (
    _identify_camera_brand,
)
from anycam_probe import (
    _extract_channel_from_rtsp_url, _onvif_media_url, _probe_rtsp_paths_single_socket, _validate_rtsp_urls_single_socket,
    find_rtsp_path, onvif_get_profiles, onvif_get_snapshot_uri, onvif_get_stream_uri,
    probe_hls, probe_mjpeg_http, probe_rtmp, probe_rtsp,
)
from anycam_snap import (
    _http_digest_header, snap_loop,
)
from camera_db import (
    STREAM_DB,
)

log = logging.getLogger("anycam")

# Taken from camera_discovery.py at start-up (anycam_host.bind).
NEEDS = (
    'CAMERAS', 'RTSP_PATHS', '_THREAD_POOL', '_brand_throttle_seconds',
    '_snap_last_access', '_snap_state', '_strip_creds',
    '_throttle_wait_if_needed', 'build_authenticated_url', 'decrypt_creds', 'encrypt_creds',
    'probe_stream_details', 'save_cameras',
)


def _match_stream_db(camera: dict) -> dict | None:
    """Return the best-matching STREAM_DB entry for a camera, or None.

    rc2: includes mac_vendor (OUI lookup result) in the haystack so that
    cameras identified by MAC address alone — before any HTTP/RTSP probe —
    can be matched to their vendor recipe. This is critical for cameras
    like Microseven where ONVIF returns no useful vendor info but the
    OUI lookup gives "Microseven Inc"."""
    haystack = " ".join([
        camera.get("name", ""),
        camera.get("vendor", ""),
        camera.get("model", ""),
        camera.get("verdict_reason", ""),
        camera.get("hostname", ""),
        camera.get("mac_vendor", ""),       # rc2: OUI vendor name
        camera.get("manufacturer", ""),     # rc2: previously-identified brand
        camera.get("page_title", ""),       # rc2: HTTP page title
        camera.get("server_header", ""),    # rc2: HTTP/RTSP Server header
        camera.get("nmap_product", ""),     # rc2: nmap service banner
        camera.get("onvif_scopes", ""),     # rc2.1.1: WS-Discovery scopes
    ]).lower()
    best_slug, best_len = None, 0
    for slug, entry in STREAM_DB.items():
        for kw in entry["match"]:
            if kw in haystack and len(kw) > best_len:
                best_slug, best_len = slug, len(kw)
    return STREAM_DB[best_slug] if best_slug else None


def _match_stream_db_slug(camera: dict) -> str | None:
    """Return the STREAM_DB slug that matched, or None.

    rc2: includes mac_vendor + manufacturer + page_title + server_header +
    nmap_product in the haystack — see _match_stream_db for rationale."""
    haystack = " ".join([
        camera.get("name", ""),
        camera.get("vendor", ""),
        camera.get("model", ""),
        camera.get("verdict_reason", ""),
        camera.get("hostname", ""),
        camera.get("mac_vendor", ""),       # rc2: OUI vendor name
        camera.get("manufacturer", ""),     # rc2: previously-identified brand
        camera.get("page_title", ""),       # rc2: HTTP page title
        camera.get("server_header", ""),    # rc2: HTTP/RTSP Server header
        camera.get("nmap_product", ""),     # rc2: nmap service banner
        camera.get("onvif_scopes", ""),     # rc2.1.1: WS-Discovery scopes
    ]).lower()
    best_slug, best_len = None, 0
    for slug, entry in STREAM_DB.items():
        for kw in entry["match"]:
            if kw in haystack and len(kw) > best_len:
                best_slug, best_len = slug, len(kw)
    return best_slug


async def _probe_db_streams(ip: str, port: int, creds: str | None,
                             db_entry: dict,
                             existing_urls: set[str],
                             camera: dict | None = None,
                             max_new: int = 0) -> list[dict]:
    """
    Probe RTSP paths from a STREAM_DB entry.
    Returns list of {url, width, height, codec} dicts for responding paths,
    skipping any URLs already in existing_urls.

    2.3.0: All paths now validated through ONE TCP socket via
    _validate_rtsp_urls_single_socket instead of opening a fresh socket
    per path. Eliminates the multi-socket pressure that triggered
    Hipcam-family lockout in rc2.x. ffprobe is still per-URL (its own
    subprocess opens its own socket) so we pace it for rate_limit brands.
    """
    loop = asyncio.get_event_loop()
    results: list = []
    rtsp_port = db_entry.get("port", 554)

    cred_pfx = ""
    username = ""
    password = ""
    if creds:
        try:
            from urllib.parse import quote as _q
            username, password = decrypt_creds(creds)
            _SAFE = "!$&'()*+,;=~-._"
            cred_pfx = f"{_q(username, safe=_SAFE)}:{_q(password, safe=_SAFE)}@"
        except Exception:
            pass

    # Build URL list, deduping against caller's existing_urls set
    urls_to_validate: list = []
    bare_for_url:     dict = {}   # cred-bearing URL → bare URL (for de-dup)
    for path in db_entry.get("rtsp", []):
        url  = f"rtsp://{cred_pfx}{ip}:{rtsp_port}{path}"
        bare = f"rtsp://{ip}:{rtsp_port}{path}"
        if bare in existing_urls or url in existing_urls:
            continue
        urls_to_validate.append(url)
        bare_for_url[url] = bare

    if not urls_to_validate:
        return results

    # 2.3.0: throttle awareness — even though we use one TCP socket for
    # validation now, ffprobe per match still opens its own socket, so
    # we register the validation TCP open with the tracker.
    #
    # 3.0.1 (B24): the cooldown comes from the camera's brand, as everywhere
    # else in password entry. Before, it was read from db_entry, a STREAM_DB
    # entry, which never has throttle_type (that is in CAMERA_DB), so this
    # check and every ffprobe after it ran without the wait.
    throttle_s = _brand_throttle_seconds(camera) if camera else 0.0
    if throttle_s > 0:
        await _throttle_wait_if_needed(ip, throttle_s, "db_probe validation")

    url_results = await loop.run_in_executor(
        _THREAD_POOL, _validate_rtsp_urls_single_socket,
        ip, rtsp_port, urls_to_validate,
        username, password, 4.0, None,
        f"db_probe:{ip}")

    for url in urls_to_validate:
        if not url_results.get(url, False):
            continue
        if max_new and len(results) >= max_new:      # 3.8.0-rc1.0 (B54): 0/1/2 rule
            break
        try:
            if throttle_s > 0:
                await _throttle_wait_if_needed(ip, throttle_s,
                                               f"db_probe ffprobe {_strip_creds(url)}")
            det = await probe_stream_details(url, "RTSP")
            results.append({"url": url, **det})
        except Exception:
            pass
    return results

# ── 3.8.0-rc1.0 (B54): the login rule's message ────────────────────────────
LOGIN_REJECTED_TEXT = "Password rejected or camera locked."


async def _lock_hint(camera: dict) -> str:
    """When the camera itself says it is locked, its unlock time.

    Only Hikvision is asked, because it answers without a login: its
    /ISAPI/Security/userCheck reply carries lockStatus and unlockTime. Reolink,
    Tapo and Foscam report a lock only to a request that carries the password,
    which would add one more failed login, so they are not asked.
    """
    brand = (camera.get("manufacturer") or "").lower()
    ip = camera.get("ip", "")
    if "hikvision" not in brand or not ip:
        return ""
    import aiohttp
    try:
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://{ip}/ISAPI/Security/userCheck") as resp:
                body = await resp.text(errors="replace")
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return ""
    if not re.search(r"<lockStatus>\s*lock", body, re.IGNORECASE):
        return ""
    m = re.search(r"<unlockTime>\s*(\d+)", body)
    return (f" The camera says it is locked for {m.group(1)} more seconds."
            if m else " The camera says it is locked.")


# 2.5.0-rc1.2: post-cred-auth channel enumeration on channel_iterate
# brands. Tracks which (camera_id) we've already enumerated so a
# repeated cred-auth click doesn't re-enumerate the same DVR.
_DVR_ENUM_DONE: set = set()


# ── 3.5.0 (D1): the channel count the DVR reports ──────────────────────────
# Before 3.5.0 AnyCam walked channels 1 to 16 on every Lorex/Dahua DVR: an
# 8-channel DVR got 7 needless probes, and a 32-channel NVR lost half its
# cameras. Dahua's HTTP API (also on Lorex, which is Dahua inside) answers
# the number of video inputs. Both answers are read, and the larger counts
# (an NVR reports its IP channels as remote inputs).
DVR_CHANNEL_PATHS = ("/cgi-bin/devVideoInput.cgi?action=getCollect",
                     "/cgi-bin/magicBox.cgi?action=getProductDefinition&name=MaxRemoteInputChannels")
DVR_CHANNEL_DEFAULT = 16       # when the DVR does not say
DVR_CHANNEL_MAX = 256          # a larger answer is not believed


def _dvr_parse_count(text: str) -> int | None:
    """The number in "result=8" or "table.MaxRemoteInputChannels=32"."""
    m = re.search(r"(?im)^\s*(?:result|table\.MaxRemoteInputChannels)\s*=\s*(\d+)\s*$", text or "")
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= DVR_CHANNEL_MAX else None


async def _dvr_channel_count(cam: dict, username: str, password: str) -> int | None:
    """Ask the DVR how many channels it has; None when it does not answer."""
    import aiohttp
    host = urlparse(cam.get("http_snap_url") or "").netloc.split("@")[-1] or cam.get("ip", "")
    counts = []
    timeout = aiohttp.ClientTimeout(total=5)
    try:
        async with aiohttp.ClientSession(timeout=timeout,
                                         connector=aiohttp.TCPConnector(ssl=False)) as session:
            for path in DVR_CHANNEL_PATHS:
                url = f"http://{host}{path}"
                try:
                    async with session.get(url, auth=aiohttp.BasicAuth(username, password)) as resp:
                        status, www, body = (resp.status, resp.headers.get("WWW-Authenticate", ""),
                                             await resp.text(errors="replace"))
                    if status == 401 and www.startswith("Digest"):
                        hdr = _http_digest_header(www, "GET", path, username, password)
                        async with session.get(url, headers={"Authorization": hdr}) as resp:
                            status, body = resp.status, await resp.text(errors="replace")
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as ex:
                    log.debug(f"  channel count: {path.split('?')[0]} failed: {ex}")
                    continue
                n = _dvr_parse_count(body) if status == 200 else None
                if n:
                    counts.append(n)
    except (aiohttp.ClientError, OSError) as ex:
        log.debug(f"  channel count: {ex}")
    return max(counts) if counts else None


async def _enumerate_dvr_channels_after_auth(camera_id: str) -> None:
    """2.5.0-rc1.2: when cred-auth succeeds on a `channel_iterate`
    brand (Lorex/Dahua DVR-NVR Family etc.), walk the remaining
    channels in a background task and register each populated channel
    as its own camera card.

    Architectural rationale: a DVR exposes 1..N virtual stream slots,
    and a populated slot maps to a physical camera channel. Surfacing
    each populated channel as a separate card was the original 2026-
    05-02 plan's acceptance criterion #1 (`returns N populated
    channels (N = number of cameras physically connected, ≥5)`). 2.5.0-
    rc1.0 deferred this; 2.5.0-rc1.2 lands it.

    Throttle safety: brand has `auth_attempt_lockout` semantics — but
    SUCCESSFUL auth doesn't burn counter attempts, only failed auth
    does. The credentials we use here have already been validated by
    the cred-auth flow that called us, so walking remaining channels
    is unconstrained by the lockout counter. The validate walker
    captures the auth challenge from the first 401 and reuses the
    nonce for subsequent URLs (RFC 2617).

    Empty-channel filtering: the validate walker now (rc1.2) honors
    `walker_populated_channel_test` on host_meta. We pass
    `sdp_has_video_track` so virtual slots that return SDP without a
    real video codec rtpmap get skipped instead of registered as
    cards.

    Idempotency: tracked via `_DVR_ENUM_DONE`. Re-clicking save
    credentials on an already-enumerated DVR is a no-op (the existing
    cards remain).

    Per-card snap URL: built from the streaming_recipe's
    `snap_url_template` (added 2.5.0-rc1.2) so each channel card's
    thumbnail polls the channel-specific snapshot endpoint
    (`http://IP/cgi-bin/snapshot.cgi?channel=N` for Dahua-family).
    Without per-channel URLs, all cards would poll the same default
    snapshot and show the same image.
    """
    if camera_id in _DVR_ENUM_DONE:
        return

    cam = CAMERAS.get(camera_id)
    if not cam:
        # 2.5.0-rc1.8: signal done so the new JS poll loop in submitCreds()
        # exits cleanly when the camera was deleted between cred-auth and
        # task scheduling. Without this, /api/dvr_enum/status/{id} would
        # return done=False forever (camera_id never enters the set), and
        # the poll would only exit on its 12s hard cap.
        _DVR_ENUM_DONE.add(camera_id)
        return

    brand_entry = _identify_camera_brand(cam)
    if not brand_entry:
        _DVR_ENUM_DONE.add(camera_id)  # rc1.8: terminate poll loop
        return
    recipe = (brand_entry or {}).get("streaming_recipe") or {}
    if recipe.get("type") != "channel_iterate":
        _DVR_ENUM_DONE.add(camera_id)  # rc1.8: terminate poll loop
        return

    primary_url = cam.get("stream_url", "")
    primary_ch  = _extract_channel_from_rtsp_url(primary_url)
    if not primary_ch:
        log.debug(f"  channel enumeration: could not extract primary "
                  f"channel from {_strip_creds(primary_url)} — aborting")
        _DVR_ENUM_DONE.add(camera_id)  # rc1.8: terminate poll loop
        return

    creds = cam.get("credentials")
    if not creds:
        _DVR_ENUM_DONE.add(camera_id)  # rc1.8: terminate poll loop
        return
    # 2.5.0-rc1.3: bug-fix on rc1.2. The cam["credentials"] field is a
    # Fernet-encrypted JSON string, not a dict — decrypt it the same way
    # the rest of the cred-aware codepaths do (see line ~8690 in
    # _validate_rtsp_walk). rc1.2 assumed a dict shape and crashed:
    # `AttributeError: 'str' object has no attribute 'get'` at the line
    # where we tried `creds.get("username", "")`. The crash happened
    # silently in the fire-and-forget task, so cred-auth still
    # succeeded (single card surfaced as before) but no enumeration
    # ever ran.
    try:
        username, password = decrypt_creds(creds)
    except Exception as e:
        log.debug(f"  channel enumeration: decrypt_creds failed: {e}")
        _DVR_ENUM_DONE.add(camera_id)  # rc1.8: terminate poll loop
        return
    if not (username and password):
        _DVR_ENUM_DONE.add(camera_id)  # rc1.8: terminate poll loop
        return

    ip   = cam.get("ip", "")
    port = cam.get("port", 554)

    # Build candidate URLs for OTHER channels. Main stream only here —
    # we want to know "which channels have cameras," not validate every
    # sub-stream variant. Sub-streams for each populated channel can be
    # discovered later by the per-card cred-auth flow.
    template = recipe.get("path_template", "")
    main_subtype = recipe.get("subtype_main", 0)
    # 3.5.0 (D1): the DVR says how many channels it has
    reported = await _dvr_channel_count(cam, username, password)
    if reported:
        channel_cap = reported
        raw_channels = list(range(1, reported + 1))
        log.info(f"  Channel enumeration: the DVR reports {reported} channel(s)")
    else:
        channel_cap = DVR_CHANNEL_DEFAULT
        raw_channels = recipe.get("channels") or list(range(1, DVR_CHANNEL_DEFAULT + 1))
        log.info(f"  Channel enumeration: the DVR did not report its channel count — "
                 f"walking channels 1 to {DVR_CHANNEL_DEFAULT}")
    cam["dvr_channels"] = reported
    candidate_paths: list[str] = []
    for ch in raw_channels:
        if ch > channel_cap:
            continue
        if str(ch) == primary_ch:
            continue
        try:
            candidate_paths.append(template.format(ch=ch, st=main_subtype))
        except (KeyError, IndexError):
            continue

    if not candidate_paths:
        _DVR_ENUM_DONE.add(camera_id)
        return

    candidate_urls = [
        f"rtsp://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{ip}:{port}{p}"
        for p in candidate_paths
    ]

    log.info(f"  Channel enumeration starting for {camera_id}: walking "
             f"{len(candidate_urls)} other channel paths "
             f"(brand={brand_entry.get('name', '?')})")

    # 2.5.0-rc1.4: walk URLs one socket at a time. Firmware on this
    # brand closes the TCP connection after each full authenticated
    # transaction (OPTIONS+DESCRIBE+SETUP+TEARDOWN), so the validate
    # walker's normal single-socket-multi-URL pattern bails out on the
    # second URL with `OPTIONS exception → ConnectionResetError`. The
    # 2.5.0-rc1.3 field log captured this exactly: channel=2 succeeded
    # cleanly, channel=3 OPTIONS hit RST, walker bailed remaining 13
    # URLs. Per-URL invocation gives each URL a fresh socket. ~300ms
    # per URL × 15 URLs ≈ 5s total wallclock — acceptable for a
    # background task. Each call sets walker_skip_acd so the closed-
    # socket-after-success isn't logged as an ACD-relevant RST event.
    enum_meta = {
        "walker_throttle_type":           "auth_attempt_lockout",
        "walker_populated_channel_test":  recipe.get(
            "populated_channel_test", "sdp_has_video_track"),
        "walker_skip_acd":                True,
    }
    loop = asyncio.get_event_loop()
    walk_result: dict[str, bool] = {}
    for i, url in enumerate(candidate_urls, 1):
        try:
            one_result = await loop.run_in_executor(
                _THREAD_POOL,
                _validate_rtsp_urls_single_socket,
                ip, port, [url], username, password, 6.0,
                enum_meta,
                f"channel-enum:{camera_id}({i}/{len(candidate_urls)})",
            )
        except Exception as e:
            log.debug(f"  channel enum walker URL {i} raised: {e}")
            one_result = {url: False}
        if isinstance(one_result, dict):
            walk_result.update(one_result)
        # Tiny breath between URLs to be polite to the DVR's RTSP
        # subsystem and let any lingering server-side socket cleanup
        # finish before the next OPTIONS opens a new connection.
        await asyncio.sleep(0.1)

    snap_template = recipe.get("snap_url_template", "")
    populated_channels: list[str] = []
    new_cards: list[str] = []

    # 3.8.0-rc1.0 (B54): each populated channel is its own camera, so the
    # 0/1/2 rule applies per channel: its sub-stream is checked too, one
    # connection per URL as above. The parent channel gets the same check
    # when the password step found no sub-stream for it.
    sub_ok: dict[str, str] = {}
    sub_st = recipe.get("subtype_sub")
    if sub_st is not None:
        sub_channels = [_extract_channel_from_rtsp_url(u) for u, ok in walk_result.items() if ok]
        if not cam.get("sub_stream_url"):
            sub_channels.append(primary_ch)
        for ch in [c for c in sub_channels if c]:
            try:
                sub_path = template.format(ch=int(ch), st=sub_st)
            except (KeyError, IndexError, ValueError):
                continue
            sub_url = (f"rtsp://{quote(username, safe='')}:{quote(password, safe='')}"
                       f"@{ip}:{port}{sub_path}")
            try:
                one = await loop.run_in_executor(
                    _THREAD_POOL, _validate_rtsp_urls_single_socket,
                    ip, port, [sub_url], username, password, 6.0,
                    enum_meta, f"channel-enum:{camera_id}(ch{ch} sub)")
            except Exception as e:
                log.debug(f"  channel enum sub-stream ch{ch} raised: {e}")
                one = {}
            if isinstance(one, dict) and one.get(sub_url):
                sub_ok[ch] = _strip_creds(sub_url)
            await asyncio.sleep(0.1)
        log.info(f"  Channel enumeration: sub-streams found for channel(s) "
                 f"{sorted(sub_ok, key=lambda c: int(c) if c.isdigit() else 999)}")
        if sub_ok.get(primary_ch) and not cam.get("sub_stream_url"):
            cam["sub_stream_url"] = sub_ok[primary_ch]

    for url, probe_ok in walk_result.items():
        if not probe_ok:
            continue
        ch = _extract_channel_from_rtsp_url(url)
        if not ch:
            continue
        populated_channels.append(ch)
        new_id = f"{ip}_{port}_ch{ch}"
        if new_id in CAMERAS:
            continue
        # Build per-channel snap URL from template if available
        per_channel_snap = ""
        if snap_template:
            try:
                per_channel_snap = snap_template.format(ip=ip, ch=ch)
            except (KeyError, IndexError):
                per_channel_snap = ""
        # Clone the parent card structure, override channel-specific
        # fields. Strip credentials from the visible URL — the real
        # creds live in cam["credentials"] and get re-attached at
        # stream-fetch time by the snap_loop.
        new_cam = dict(cam)
        new_cam.update({
            "id":             new_id,
            "stream_url":     _strip_creds(url),
            "sub_stream_url": sub_ok.get(ch),          # 3.8.0-rc1.0 (B54)
            "channel":        ch,
            "name":           f"{brand_entry.get('name', 'DVR')} ch{ch}",
            "status":         "ready",
            "user_saved":     True,
            "requires_credentials": False,
            "http_snap_url":  per_channel_snap or cam.get("http_snap_url", ""),
            # Children inherit parent credentials
            "credentials":    cam.get("credentials"),
            "stream_profiles": [],   # rebuilt on first focus
            "locked_streams":  [],
            "additional_streams": [],
            "_dvr_parent_id":  camera_id,
        })
        CAMERAS[new_id] = new_cam
        new_cards.append(new_id)

        # 2.5.0-rc1.5: explicitly start the snap_loop for each new card.
        # Without this, newly-registered cards live in the CAMERAS dict
        # and surface in /api/cameras but never get a thumbnail polled
        # because the snap_loop kickoff path normally runs from
        # handle_snapshot (UI requests thumbnail → snap_loop starts).
        # Symptom on rc1.4: enumeration registered 7 channels but the
        # UI showed nothing — saw it in the field log as zero `SNAP
        # [192.168.50.217_554_chN]: starting background process` lines
        # firing after `Channel enumeration complete`. Mirroring the
        # post-restart-load behaviour where each saved card kicks off
        # its own snap_loop on startup.
        try:
            authed_url = build_authenticated_url(new_cam)
            if authed_url:
                _snap_last_access[new_id] = time.monotonic()
                state = _snap_state(new_id)
                if state.get("task") is None or state["task"].done():
                    log.info(f"  SNAP [{new_id}]: kicking off initial "
                             f"thumbnail loop after channel enumeration")
                    state["task"] = asyncio.create_task(
                        snap_loop(new_id, authed_url, new_cam))
        except Exception as _snap_e:
            log.debug(f"  channel-enum: snap kickoff for {new_id} "
                      f"raised: {_snap_e}")

    # Update the parent card's name to reflect the primary channel
    # (e.g. "Lorex / Dahua DVR-NVR Family ch1") so all DVR cards share
    # the same naming convention.
    if cam.get("name", "").lower() in (
            "general", "ip camera", "network camera", brand_entry.get(
                "name", "").lower()):
        cam["name"] = f"{brand_entry.get('name', 'DVR')} ch{primary_ch}"
    cam["channel"] = primary_ch
    cam["_dvr_parent_id"] = camera_id   # parent is its own parent
    cam["_dvr_populated_channels"] = sorted(
        set(populated_channels + [primary_ch]),
        key=lambda c: int(c) if c.isdigit() else 999)

    _DVR_ENUM_DONE.add(camera_id)
    save_cameras()
    log.info(f"  Channel enumeration complete for {camera_id}: "
             f"{len(new_cards)} new card(s) registered "
             f"(populated channels: {cam['_dvr_populated_channels']})")


async def api_dvr_enum_status(request: web.Request) -> web.Response:
    """2.5.0-rc1.8: lightweight polling endpoint that lets the post-cred-
    auth UI flow detect channel-enumeration completion deterministically
    instead of waiting a fixed wallclock budget.

    Replaces the 2.5.0-rc1.6 fixed +8s setTimeout(loadCameras) reload
    with a poll-until-done pattern. On CrystalHeeler's 7-channel Lorex the
    enumeration completed in ~3s (rc1.7 test log 10:51:16 → 10:51:19),
    so the old fixed budget cost ~5s of dead time before cards
    appeared. This endpoint shaves that by letting the UI react to
    actual completion.

    Returns:
      done                  — bool. True iff camera_id is in
                              _DVR_ENUM_DONE. The set is now populated
                              on every exit path of
                              _enumerate_dvr_channels_after_auth (rc1.8
                              backend hardening), so this flag flips
                              true within bounded time regardless of
                              outcome.
      populated_channels    — list[str]. Channels with real cameras
                              attached, populated from the parent cam's
                              _dvr_populated_channels field after a
                              successful run. Empty for non-success
                              exits (camera deleted, decrypt fail, etc.)
                              and during the in-progress window.

    Pattern matches /api/scan/status, /api/pscan/status, /snap/status:
    a tiny GET with a JSON body that the JS polls on a setInterval.
    """
    cid = request.match_info.get("camera_id", "")
    cam = CAMERAS.get(cid)
    populated: list = []
    if cam:
        populated = list(cam.get("_dvr_populated_channels") or [])
    return web.json_response({
        "done": cid in _DVR_ENUM_DONE,
        "populated_channels": populated,
    })


async def api_set_credentials(request: web.Request) -> web.Response:

    try:
        data      = await request.json()
        camera_id = data.get("camera_id", "")
        username  = data.get("username", "").strip()
        password  = data.get("password", "")
        # 2.6.0-rc3.0 Item 4 — track whether this credential submission
        # came in through the "🔒 N Locked Streams" badge modal or
        # through a generic Login button. When True, we clear
        # camera.locked_streams after auth (the user explicitly
        # walked through the modal, the badge has served its purpose
        # and the validated subset is now in additional_streams).
        # When False, we PRESERVE the locked_streams list so the badge
        # persists — the user logged in via some other path and may
        # still want to review locked candidates later. Pre-rc3.0
        # behavior was to clear unconditionally, which nuked the badge
        # for users who logged in via the regular Login button.
        from_locked_modal = bool(data.get("from_locked_streams_modal", False))
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    camera = CAMERAS.get(camera_id)
    if not camera:
        return web.json_response({"error": "Camera not found"}, status=404)

    proto = camera.get("protocol", "RTSP")
    ip    = camera["ip"]
    port  = camera.get("port", 554)
    loop  = asyncio.get_event_loop()
    url   = None

    log.info(f"Credential attempt: {camera_id} proto={proto} ip={ip}:{port} "
             f"onvif={camera.get('onvif')} xaddrs={camera.get('xaddrs','')[:40]}")

    if proto in ("ONVIF",) or camera.get("onvif"):
        media_url = _onvif_media_url(ip, port, camera.get("xaddrs",""))
        log.info(f"  ONVIF media URL: {media_url}")

        # 2.3.0: brand throttle awareness — surface a status text the UI
        # polls so users see "Authenticating (Camera rate-limited, ~30
        # seconds)" instead of a silent 25s pause for Hipcam-family
        # cameras. The throttle pacing itself happens before each ffprobe
        # call below; the cred-auth handler shows the status while it works.
        throttle_s = _brand_throttle_seconds(camera)
        if throttle_s > 0:
            camera["status"] = "authenticating_throttled"
            camera["status_text"] = (f"Authenticating "
                                     f"(Camera rate-limited, ~30 seconds)")
            log.info(f"  Brand has rate_limit_per_ip_tcp ({throttle_s:.0f}s) — "
                     f"using single-socket validation + throttled ffprobe")

        profiles  = await loop.run_in_executor(
            _THREAD_POOL, onvif_get_profiles, media_url, username, password)
        log.info(f"  ONVIF profiles found: {len(profiles)} — {[p['name'] for p in profiles]}")
        if profiles:
            enc_creds = encrypt_creds(username, password)

            # ── 2.3.0: Collect all stream URLs first, then validate them
            # all over a SINGLE TCP socket. Replaces the per-profile
            # probe_rtsp loop (one TCP per profile) that triggered Hipcam
            # firmware-level lockout in rc2.x.
            profile_urls = []   # list of (prof, stream_url)
            for prof in profiles:
                stream_url = await loop.run_in_executor(
                    _THREAD_POOL, onvif_get_stream_uri, media_url, prof["token"], username, password)
                log.info(f"  Profile '{prof['name']}' stream_url: {stream_url}")
                if stream_url:
                    profile_urls.append((prof, stream_url))

            url_results: dict = {}
            if profile_urls:
                # 2.3.2: Parse the actual RTSP port from the first profile
                # URL. The camera's stored `port` attribute is the
                # DISCOVERY port (often 80 for HTTP-discovered cams like
                # the Hipcam/Microseven family), NOT the RTSP port. The
                # 2.3.0+ validator was being passed `port` directly, so on
                # those cameras it was opening TCP to :80 — the HTTP admin
                # server — and sending RTSP-shaped requests there. The
                # HTTP server replied 'HTTP/1.1 400 Bad Request' to the
                # first request and closed the connection, leaving the
                # second request with an empty response. The validator
                # logged 0/2 OK every time, but the "ONVIF confirmed creds
                # → including anyway" fallback masked the symptom so
                # streams still came up via ffprobe. Confirmed empirically
                # on Microseven via direct port-554 RTSP walks (Test
                # C: 2/2 URLs OK in 1.6s through one socket after a
                # 4-SOAP burst with 1s gap).
                #
                # Defensive parse: ONVIF responses are untrusted input;
                # urlparse can raise ValueError on malformed port. Fall
                # back to 554 (RTSP default) on any parse failure rather
                # than letting the exception bubble up and break the
                # whole cred-auth flow.
                _, first_stream_url = profile_urls[0]
                try:
                    rtsp_port = urlparse(first_stream_url).port or 554
                except (ValueError, AttributeError) as ex:
                    log.warning(f"  Could not parse RTSP port from "
                                f"{first_stream_url!r} ({ex}) "
                                f"— falling back to 554")
                    rtsp_port = 554
                if rtsp_port != port:
                    log.info(f"  Validator using RTSP port {rtsp_port} "
                             f"(parsed from profile URL; camera-stored "
                             f"port was {port})")
                url_results = await loop.run_in_executor(
                    _THREAD_POOL, _validate_rtsp_urls_single_socket,
                    ip, rtsp_port, [u for _, u in profile_urls],
                    username, password, 6.0, None,
                    f"{camera_id}/onvif-profiles")
                log.info(f"  Single-socket profile validation: "
                         f"{sum(1 for v in url_results.values() if v)}/"
                         f"{len(profile_urls)} OK")

            # ── Build stream_candidates from validated results ────────────────
            stream_candidates = []   # list of {url, width, height, codec, token, name}
            for prof, stream_url in profile_urls:
                ok = url_results.get(stream_url, False)
                if not ok:
                    log.warning(f"  validate returned False for '{prof['name']}' "
                                f"— including anyway (ONVIF confirmed creds)")
                # 2.3.0: pace ffprobe (it opens its own RTSP socket per call,
                # not covered by the single-socket validation above).
                if throttle_s > 0:
                    await _throttle_wait_if_needed(ip, throttle_s,
                                                   f"ffprobe '{prof['name']}'")
                det = await probe_stream_details(stream_url, "RTSP")

                # ── Resolution: highest pixel area wins ───────────────────────
                # Both probe and ONVIF can be wrong — buggy firmware tends to
                # report *lower* or zero values, not inflated ones, so the source
                # reporting the larger pixel area is almost certainly more correct.
                probe_w  = det.get("stream_width")  or 0
                probe_h  = det.get("stream_height") or 0
                onvif_w  = prof.get("onvif_width")  or 0
                onvif_h  = prof.get("onvif_height") or 0
                if (onvif_w * onvif_h) >= (probe_w * probe_h) and onvif_w:
                    det["stream_width"]  = onvif_w
                    det["stream_height"] = onvif_h
                    res_src = "onvif"
                else:
                    res_src = "probe" if probe_w else "none"

                # ── Codec: highest capability wins ────────────────────────────
                # Rank: hevc > h264 > mjpeg > mpeg4 > anything else.
                # Again, wrong firmware tends to report a lesser codec (e.g.
                # H264 for an HEVC stream), so the higher-ranked source wins.
                _CODEC_RANK = {"hevc": 4, "h265": 4, "h264": 3,
                               "mjpeg": 2, "jpeg": 2, "mpeg4": 1}
                probe_codec = det.get("stream_codec") or ""
                onvif_codec = prof.get("onvif_encoding") or ""
                probe_rank  = _CODEC_RANK.get(probe_codec.lower(), 0)
                onvif_rank  = _CODEC_RANK.get(onvif_codec.lower(), 0)
                if onvif_rank > probe_rank and onvif_codec:
                    det["stream_codec"] = onvif_codec
                    codec_src = "onvif"
                elif probe_codec:
                    codec_src = "probe"
                else:
                    codec_src = "none"

                # ── Audio: ONVIF wins (probe rarely detects audio correctly) ──
                if prof.get("onvif_audio"):
                    det["stream_audio"] = prof["onvif_audio"]

                log.info(f"  Profile '{prof['name']}': "
                         f"{det.get('stream_width')}x{det.get('stream_height')} [{res_src}] "
                         f"{det.get('stream_codec','?')} [{codec_src}] "
                         f"{det.get('stream_fps','?')}fps "
                         f"audio={det.get('stream_audio','none')}")
                stream_candidates.append({
                    "url": stream_url, "token": prof["token"],
                    "name": prof["name"], "probe_ok": ok, **det,
                })

            # ── Silent DB probe: find additional streams not visible pre-login ─
            db_entry  = _match_stream_db(camera)
            db_slug   = _match_stream_db_slug(camera)

            # ── HTTP snapshot URL: DB first, ONVIF GetSnapshotUri as fallback ─
            http_snap_url       = None
            http_snap_auth_mode = "basic"
            if db_entry and db_entry.get("snap"):
                http_snap_url = f"http://{ip}{db_entry['snap']}"
                if db_slug == "reolink":
                    http_snap_auth_mode = "query_params"
                log.info(f"  HTTP snap URL (DB): {http_snap_url}")
            else:
                # Secondary: try ONVIF GetSnapshotUri on the first profile token
                if profiles:
                    _first_token = profiles[0]["token"]
                    _snap_uri = await loop.run_in_executor(
                        _THREAD_POOL, onvif_get_snapshot_uri,
                        media_url, _first_token, username, password)
                    if _snap_uri:
                        http_snap_url = _snap_uri
                        log.info(f"  HTTP snap URL (ONVIF GetSnapshotUri): {http_snap_url}")
                    else:
                        log.debug(f"  HTTP snap URL: not available (DB=None, ONVIF=None)")

            # ── 2.3.0: Modified db_probe skip rule (CrystalHeeler's design) ────────
            # Skip db_probe if we already have ≥2 stream URL coverage:
            #   • ≥2 ONVIF profiles probed OK (typical case: main + sub), OR
            #   • ≥1 ONVIF profile + a pre-existing unauth stream from scan
            #     (this fires when user manually opens cred dialog on an
            #     already-streaming card to add auth'd profiles).
            # Otherwise run db_probe as the safety net for finding streams
            # ONVIF didn't expose. Replaces the old "always run" behavior.
            onvif_working    = sum(1 for c in stream_candidates if c.get("probe_ok"))
            have_unauth      = bool(camera.get("rtsp_probe_ok") and camera.get("stream_url"))
            have_main_n_sub  = (onvif_working >= 2) or (onvif_working >= 1 and have_unauth)

            if db_entry and not have_main_n_sub:
                # 3.8.0-rc1.0 (B54): the 0/1/2 rule. One stream from ONVIF:
                # the brand paths add at most one more (no full walk).
                existing = {c["url"] for c in stream_candidates}
                db_streams = await _probe_db_streams(ip, port, enc_creds,
                                                     db_entry, existing, camera,
                                                     max_new=1 if onvif_working == 1 else 0)
                if db_streams:
                    log.info(f"  DB probe found {len(db_streams)} extra stream(s)")
                    for s in db_streams:
                        stream_candidates.append({**s, "token": "db_probe",
                                                  "name": "DB stream"})
            elif db_entry and have_main_n_sub:
                log.info(f"  Skipping db_probe — main+sub coverage achieved "
                         f"(onvif_working={onvif_working}, "
                         f"unauth_stream={have_unauth})")

            if stream_candidates:
                # ── Rank by resolution: highest first, lowest last ─────────────
                def _res(c: dict) -> int:

                    return (c.get("stream_width") or 0) * (c.get("stream_height") or 0)
                stream_candidates.sort(key=_res, reverse=True)

                # ── Dedupe by (width, height, codec) ───────────────────────────
                # Many ONVIF cameras (Hikvision, Dahua) expose 4+ profiles where
                # several are identical resolution/codec but differ only in name
                # or token. Showing all of them in the focus-view Resolution
                # dropdown is noise — keep only the first (highest-priority,
                # already sort-ordered) URL for each unique combo.
                seen   = set()
                deduped = []
                for c in stream_candidates:
                    key = (c.get("stream_width"),
                           c.get("stream_height"),
                           (c.get("stream_codec") or "").lower())
                    if key in seen:
                        log.info(f"  Profile dedupe: dropping duplicate "
                                 f"{key[0]}x{key[1]} {key[2] or '?'} "
                                 f"({c.get('name', '?')}, {_strip_creds(c.get('url',''))})")
                        continue
                    seen.add(key)
                    deduped.append(c)
                stream_candidates = deduped

                main_s = stream_candidates[0]
                # Only use a sub-stream that actually passed probe_rtsp.
                # If the second profile failed probe (e.g. "Connection reset"),
                # thumbnail polling would hammer a broken URL indefinitely.
                ok_subs = [c for c in stream_candidates[1:] if c.get("probe_ok")]
                sub_s   = ok_subs[-1] if ok_subs else None

                # Build stream_profiles: all candidates in resolution order,
                # each tagged with a _url_key so the adaptive ladder can look up
                # the right URL via build_authenticated_url().
                # profile[0] = highest res (main), profile[-1] = lowest res (sub).
                stream_profiles = []
                for i, cand in enumerate(stream_candidates):
                    url_key = ("stream_url" if i == 0
                               else ("sub_stream_url" if i == len(stream_candidates) - 1
                                     else f"stream_profile_{i}_url"))
                    stream_profiles.append({
                        "url":          cand["url"],
                        "_url_key":     url_key,
                        "stream_width":  cand.get("stream_width"),
                        "stream_height": cand.get("stream_height"),
                        "stream_codec":  cand.get("stream_codec"),
                        "stream_fps":    cand.get("stream_fps"),
                        "stream_audio":  cand.get("stream_audio"),
                        "rtsp_probe_ok": bool(cand.get("probe_ok")),
                    })

                log.info(f"  Profiles ranked by resolution:")
                for i, p in enumerate(stream_profiles):
                    log.info(f"    [{i}] {_strip_creds(p['url'])} "
                             f"({p.get('stream_width')}x{p.get('stream_height')}) "
                             f"{p.get('stream_codec','?')}")

                main_token = main_s.get("token", profiles[0]["token"])
                cid = f"{ip}_onvif_{main_token}"
                main_details = {k: v for k, v in main_s.items()
                                if k not in ("url", "token", "name")}

                # Build extra-profile URL keys for the CAMERAS dict so
                # build_authenticated_url can look them up by key.
                extra_urls = {}
                for i, cand in enumerate(stream_candidates):
                    if i == 0:
                        pass   # main → stream_url (added below via main_s["url"])
                    elif i == len(stream_candidates) - 1:
                        pass   # last → sub_stream_url (added below)
                    else:
                        extra_urls[f"stream_profile_{i}_url"] = cand["url"]

                # Always reset hevc_plus_warning on (re-)discovery so stale flags
                # from previous sessions or wrong stream URLs don't carry forward.
                # _drain_stderr will re-set it at runtime if ffmpeg actually sees
                # "Multi-layer HEVC coding is not implemented" on this stream.
                # rc2.2: preserve identification fields from the OLD camera
                # record across this dict-replacement. Without this, anything
                # the scan stage identified (manufacturer via mac_vendor /
                # ONVIF scopes / HTTP probe / RTSP Server header) is silently
                # wiped when the user enters credentials and the camera
                # transitions to a profile-based id (e.g. _onvif → _onvif_<token>).
                _id_preserve = {
                    k: camera.get(k, "")
                    for k in ("manufacturer", "mac_addr", "mac_vendor",
                              "page_title", "server_header", "onvif_scopes",
                              "device_notes",
                              # 2.4.0-rc1.0: RTSP fingerprint fields are
                              # captured pre-auth; preserve across the
                              # cred-auth dict replacement so the Identity
                              # panel shows them after the user authenticates.
                              "rtsp_server_header", "rtsp_auth_realm",
                              "rtsp_auth_scheme", "rtsp_public_methods",
                              # 2.4.0-rc2.0: locked_streams persists across
                              # cred-auth too — the user just supplied creds
                              # for some of them, but other locked candidates
                              # may remain (different sub-streams, etc.).
                              # The UI will hide the badge once creds are
                              # saved (camera.user_saved=True), but the data
                              # stays for diagnostic / debugging visibility.
                              "locked_streams")
                    if camera.get(k)
                }
                CAMERAS[cid] = {
                    "id": cid, "ip": ip,
                    "hostname": camera.get("hostname", ip),
                    "port": port, "protocol": "RTSP", "onvif": True,
                    "stream_url":     main_s["url"],
                    "sub_stream_url": sub_s["url"] if sub_s else None,
                    "stream_profiles": stream_profiles,
                    "requires_credentials": False, "credentials": enc_creds,
                    "name": camera.get("name", ip),
                    "status": "ready", "display": "proxy", "user_saved": True,
                    "verdict": "camera", "verdict_reason": "ONVIF profile",
                    "hevc_plus_warning": False,  # reset; _drain_stderr re-sets if needed
                    "http_snap_url":       http_snap_url,
                    "http_snap_auth_mode": http_snap_auth_mode,
                    "rtsp_probe_ok":       bool(main_s.get("probe_ok")),
                    **_id_preserve,
                    **extra_urls,
                    **main_details,
                }
                CAMERAS.pop(camera_id, None)
                # rc2.2: re-run brand identification on the new record
                # one last time. The dict-replacement above retained
                # any preserved manufacturer via _id_preserve, but if
                # NONE was identified pre-cred (e.g. the Microseven was unable to
                # identify because nmap-rate-limit blocked it AND the
                # rc2.2 HTTP probe was racing against the user entering
                # creds) this gives one more chance using all current
                # signals on the record.
                _new = CAMERAS[cid]
                if not _new.get("manufacturer"):
                    try:
                        _re_id = _identify_camera_brand(_new, force=True)
                        if _re_id and _re_id["name"] != "Generic IP Camera":
                            log.info(f"  Brand identified post-cred-auth: "
                                     f"{_re_id['name']}")
                    except Exception as e:
                        log.debug(f"  brand-id (post-cred-auth ONVIF): {e}")
                save_cameras()
                cam = CAMERAS.get(cid)
                if cam:
                    # Run ffprobe to detect the real codec — ONVIF often reports
                    # "h264" when the camera actually streams HEVC.
                    cam_url = build_authenticated_url(cam) or ""
                    async def _fix_codec(cam_id: str = cid, auth_url: str = cam_url,
                                         cam_ip: str = ip, throttle_s: float = throttle_s) -> None:

                        # rc2.6: retry with backoff. The original single-shot
                        # 1s sleep + ffprobe could fail when the camera was
                        # still in a per-IP TCP rate-limit cooldown from the
                        # cred-auth probe sequence (Hipcam family: 5s+ window
                        # between TCP opens). When ffprobe failed, real_codec
                        # came back empty, the if-check below was False, and
                        # the codec stayed at the wrong ONVIF-reported value
                        # — leaving snap_loop using the wrong stream_codec for
                        # the rest of the session, which propagates into
                        # downstream behavior (UI labels, hw_decoder selection,
                        # focus ladder construction).
                        #
                        # Retry up to 3 attempts with 5s backoff. Stops on
                        # first success. Logs each retry so the failure mode
                        # is observable in logs even if the corrections never
                        # succeeds (e.g. camera firmware doesn't expose the
                        # stream to ffprobe).
                        #
                        # 2.4.0-rc3.5 Leak E fix: first attempt now honors the
                        # per-IP TCP throttle. Previously hard-coded to 1s,
                        # which for Hipcam-family cameras (5s rate_limit_per_ip_tcp)
                        # could land inside the cooldown window from the
                        # immediately preceding cred-auth ffprobe at line 8498
                        # — silently failing the codec correction probe even
                        # though all the retry-spacing pacing was correct.
                        # Now uses _throttle_wait_if_needed so we honor the
                        # cross-sequence tracker, not just per-iteration
                        # spacing within this loop.
                        det = {}
                        for attempt in range(3):
                            if attempt == 0:
                                if throttle_s > 0:
                                    await _throttle_wait_if_needed(
                                        cam_ip, throttle_s,
                                        f"ffprobe codec correction "
                                        f"(initial)")
                                else:
                                    await asyncio.sleep(1.0)
                            else:
                                await asyncio.sleep(5.0)
                            det = await probe_stream_details(auth_url, "RTSP")
                            if det.get("stream_codec"):
                                if attempt > 0:
                                    log.info(f"  Codec correction probe succeeded "
                                             f"on retry {attempt + 1}/3")
                                break
                            if attempt < 2:
                                log.info(f"  Codec correction probe attempt "
                                         f"{attempt + 1}/3 returned no codec — "
                                         f"retrying in 5s")
                        real_codec = (det.get("stream_codec") or "").lower()
                        stored = (CAMERAS.get(cam_id, {}).get("stream_codec") or "").lower()
                        if real_codec and real_codec != stored:
                            log.info(f"  Codec correction: ONVIF reported {stored!r} "
                                     f"but ffprobe detected {real_codec!r}")
                            if cam_id in CAMERAS:
                                CAMERAS[cam_id]["stream_codec"] = real_codec
                                profs = CAMERAS[cam_id].get("stream_profiles") or []
                                if profs:
                                    profs[0]["stream_codec"] = real_codec
                                save_cameras()
                        elif not real_codec:
                            log.warning(f"  Codec correction: all 3 ffprobe attempts "
                                        f"failed for {cam_id} — stream_codec stays "
                                        f"as {stored!r} (may be wrong if ONVIF "
                                        f"misreported)")
                    asyncio.create_task(_fix_codec())

                return web.json_response({"status": "ok", "channels": 1})
            log.warning(f"  ONVIF: profiles found but no streams resolved — falling back to direct RTSP")
        else:
            log.warning(f"  ONVIF: no profiles returned (auth failed or device unreachable)")

    # RTSP direct — for ONVIF cards always try port 554 in addition to stored port
    if proto in ("RTSP", "DVR", "ONVIF"):
        # 2.3.0: brand throttle awareness for non-ONVIF cred-auth.
        # find_rtsp_path already does single-socket walking (Layer 1) +
        # brand-aware Layer 2 short-circuit, so no socket pressure here,
        # but we still surface the status text + pace ffprobe below.
        throttle_s = _brand_throttle_seconds(camera)
        if throttle_s > 0 and not camera.get("status_text"):
            camera["status"] = "authenticating_throttled"
            camera["status_text"] = (f"Authenticating "
                                     f"(Camera rate-limited, ~30 seconds)")
            log.info(f"  Brand has rate_limit_per_ip_tcp ({throttle_s:.0f}s) — "
                     f"pacing ffprobe and db_probe")
        # 3.8.0-rc1.0 (B54): the 0/1/2 rule. The brand's paths first, on
        # port 554, then the card's port, then an XAddrs port; the full walk
        # only when the brand paths found nothing. Each walk stops at 2
        # streams, and at the first login the camera rejects.
        targets: list[tuple[str, int]] = []
        if port != 554:
            targets.append((ip, 554))
        targets.append((ip, port))
        if camera.get("xaddrs"):
            parsed = urlparse(camera["xaddrs"])
            x_port = parsed.port or 554
            if (parsed.hostname or ip, x_port) not in targets:
                targets.append((parsed.hostname or ip, x_port))
        wmeta = dict(camera)
        for phase in ("brand", "universal"):
            for host_t, port_t in targets:
                log.info(f"  Trying RTSP on {host_t}:{port_t} ({phase} paths)")
                url = await loop.run_in_executor(
                    _THREAD_POOL, lambda: find_rtsp_path(host_t, port_t, username, password,
                                                         wmeta, want_streams=2, path_set=phase))
                if url or wmeta.get("login_rejected"):
                    break
            if url or wmeta.get("login_rejected"):
                break
        for k in ("manufacturer", "server_header"):
            if wmeta.get(k) and not camera.get(k):
                camera[k] = wmeta[k]
        walk_found = [u for u in (wmeta.get("walk_found") or []) if u != url]
        log.info(f"  RTSP result: {_strip_creds(url) if url else 'None'}"
                 + (f" (+{len(walk_found)} more)" if walk_found else ""))
    elif proto == "MJPEG":
        url = await loop.run_in_executor(
            _THREAD_POOL, probe_mjpeg_http, ip, port, username, password)
    elif proto == "HLS":
        url = await loop.run_in_executor(
            _THREAD_POOL, probe_hls, ip, port, username, password)

    if not url:
        rejected = proto in ("RTSP", "DVR", "ONVIF") and bool(wmeta.get("login_rejected"))
        log.warning(f"Credential attempt FAILED for {camera_id} — "
                    + ("the camera rejected the login (password wrong, or the camera is locked)"
                       if rejected else "no working stream found"))
        # 2.4.0-rc2.1 — Issue 4: restore needs_credentials state before
        # returning 401. The handler entry mutated camera["status"] to
        # "authenticating_throttled" (rate_limit_per_ip_tcp brands only)
        # and set status_text to inform the UI. Without this restore,
        # the next /api/cameras poll returns cam.status="authenticating_throttled",
        # credFormHTML's `cam.status !== 'needs_credentials'` check
        # fires false, and the card collapses with no login form —
        # leaving the user stranded with only a Remove button. Observed
        # on Microseven (rate_limit_per_ip_tcp) when wrong creds
        # were tried; not observed on Hikvision because Hikvision
        # is not throttled, so the entry status mutation never happened.
        if camera.get("status") == "authenticating_throttled":
            camera["status"] = "needs_credentials"
            camera.pop("status_text", None)
        if rejected:
            return web.json_response({"error": LOGIN_REJECTED_TEXT + await _lock_hint(camera)},
                                     status=401)
        return web.json_response({"error": "Could not connect with those credentials."}, status=401)

    # 2.3.0: pace ffprobe on rate_limit brands (it opens its own RTSP socket)
    _t_s = _brand_throttle_seconds(camera)
    if _t_s > 0:
        await _throttle_wait_if_needed(ip, _t_s, "non-ONVIF ffprobe")
    # 2.6.0-rc2.3 — bug #2 fix: find_rtsp_path returns a bare URL
    # without creds embedded. Calling probe_stream_details with a
    # credless URL hands ffprobe a stream that responds 401 to its
    # DESCRIBE, so ffprobe exits non-zero and probe_stream_details
    # silently returns {}. The downstream consequence is that the
    # main profile (stream_profiles[0]) gets stream_codec=None, which
    # then blocks HW decoder selection in snap_loop because it has
    # nothing to match against the candidate list. Pre-build an
    # authenticated URL using the creds that just passed find_rtsp_path
    # so ffprobe can actually fetch the stream. The locked-stream
    # branch at line ~9727 has been doing this all along (using
    # lurl_authed); this brings the main-URL probe to the same level.
    enc_for_probe = encrypt_creds(username, password)
    auth_probe_url = build_authenticated_url(
        dict(camera, credentials=enc_for_probe), url=url) or url
    details = await probe_stream_details(auth_probe_url, proto)
    enc_creds = encrypt_creds(username, password)

    # ── Silent DB probe for additional streams on non-ONVIF cameras ──────────
    sub_url = None
    sub_details: dict = {}   # 2.4.0-rc2.9: codec/res/fps for sub_url
                             # entry in stream_profiles, populated by
                             # the db_streams branch when a sub is
                             # picked. Defaults empty.
    db_entry  = _match_stream_db(camera)
    db_slug   = _match_stream_db_slug(camera)

    # ── HTTP snapshot URL: DB first, no ONVIF fallback on non-ONVIF path ──────
    http_snap_url       = None
    http_snap_auth_mode = "basic"
    if db_entry and db_entry.get("snap"):
        http_snap_url = f"http://{ip}{db_entry['snap']}"
        if db_slug == "reolink":
            http_snap_auth_mode = "query_params"
        log.info(f"  HTTP snap URL (DB): {http_snap_url}")
    else:
        log.debug(f"  HTTP snap URL: not available (no DB snap entry for this camera)")

    # 3.8.0-rc1.0 (B54): no second pass over the brand paths. The walk tried
    # them first and stopped at 2 streams: the second one is the sub-stream
    # (on a DVR, only when it is on the same channel as the first).
    if proto in ("RTSP", "DVR", "ONVIF"):
        for cand in walk_found:
            if (_extract_channel_from_rtsp_url(cand) or "") != (_extract_channel_from_rtsp_url(url) or ""):
                log.info(f"  second stream {_strip_creds(cand)} is another channel — not a sub-stream")
                continue
            sub_url = cand
            sub_auth = build_authenticated_url(dict(camera, credentials=enc_creds), url=cand) or cand
            if _t_s > 0:
                await _throttle_wait_if_needed(ip, _t_s, "sub-stream ffprobe")
            try:
                sub_details = await probe_stream_details(sub_auth, "RTSP")
            except Exception as e:
                log.debug(f"  probe_stream_details on the sub-stream: {e}")
            log.info(f"  Sub-stream from the walk: {_strip_creds(sub_url)}")
            break

    # 2.4.0-rc2.6: post-auth validation of locked-stream candidates
    # (Fix 3 + Fix 4 followup). After creds accepted, walk through
    # cam.locked_streams and validate each against the freshly-
    # authenticated camera. Successful ones get added to a new
    # additional_streams list on the camera record. Spurious
    # candidates (404s, RSTs, still-401-after-auth) get silently
    # dropped. This handles the case where the user enumerated 26
    # locked candidates via Deep Re-Probe but only ~6 are real
    # endpoints — the bogus ones disappear after auth.
    locked_in = camera.get("locked_streams", []) or []
    additional_streams: list[dict] = []
    if locked_in and proto in ("RTSP", "DVR"):
        log.info(f"  Validating {len(locked_in)} locked-stream "
                 f"candidate(s) post-auth")
        # Throttle-aware: respect brand cooldown between probes
        for idx, lk in enumerate(locked_in):
            lpath = lk.get("path", "")
            if not lpath:
                continue
            lurl_clear = f"rtsp://{ip}:{port}{lpath}"
            # Skip if it's already the primary or sub
            if lurl_clear == url or (sub_url and lurl_clear == sub_url):
                continue
            lurl_authed = build_authenticated_url({
                "stream_url": lurl_clear,
                "credentials": enc_creds,
                "ip": ip, "port": port, "protocol": "RTSP",
            }) or lurl_clear
            # Apply brand throttle cooldown if needed (Hikvision DS-2
            # has no cooldown, but Hipcam etc. do)
            if _t_s > 0:
                await _throttle_wait_if_needed(
                    ip, _t_s, f"locked-stream-validate {idx+1}")
            try:
                ok = await loop.run_in_executor(
                    _THREAD_POOL, probe_rtsp,
                    lurl_authed, "", "", 6.0)
                if ok:
                    log.info(f"  Locked-stream validated: "
                             f"{_strip_creds(lurl_authed)}")
                    # 2.4.0-rc2.8: capture resolution/codec for the
                    # validated stream so the focus-view dropdown can
                    # label it ("1920x1080 H264") instead of falling
                    # back to "Stream N". Brand throttle cooldown
                    # already applied above before probe_rtsp; we
                    # re-throttle here because probe_stream_details
                    # opens a fresh ffprobe TCP connection.
                    add_details: dict = {}
                    if _t_s > 0:
                        await _throttle_wait_if_needed(
                            ip, _t_s,
                            f"locked-stream-details {idx+1}")
                    try:
                        add_details = await probe_stream_details(
                            lurl_authed, "RTSP")
                    except Exception as ee:
                        log.debug(f"  probe_stream_details on "
                                  f"locked-stream {lpath}: {ee}")
                    additional_streams.append({
                        "path": lpath,
                        "url": lurl_clear,
                        "realm": lk.get("realm", ""),
                        "scheme": lk.get("scheme", ""),
                        # Resolution/codec fields for dropdown labels
                        "stream_width":  add_details.get("stream_width"),
                        "stream_height": add_details.get("stream_height"),
                        "stream_codec":  add_details.get("stream_codec"),
                        "stream_fps":    add_details.get("stream_fps"),
                    })
                else:
                    log.info(f"  Locked-stream NOT working "
                             f"(probably not a real endpoint): {lpath}")
            except Exception as e:
                log.debug(f"  Locked-stream validate exception "
                          f"({lpath}): {e}")
        log.info(f"  Locked-stream validation: "
                 f"{len(additional_streams)}/{len(locked_in)} "
                 f"validated as working")

    # 2.4.0-rc2.8: build stream_profiles explicitly. The non-ONVIF
    # cred-accept path used to leave stream_profiles empty, relying on
    # the dropdown's synth fallback at api_focus_get. That fallback
    # only built [main, sub] from stream_url/sub_stream_url and never
    # included additional_streams — so even when 3 locked candidates
    # were validated, the dropdown showed only 2 entries. Building
    # stream_profiles here means the canonical list is persisted on
    # the camera record and survives reload, the focus-view dropdown
    # gets all entries, and the snap_loop tier selection uses the
    # same source of truth as the UI.
    stream_profiles: list[dict] = []
    _seen_urls: set[str] = set()
    # Primary first
    if url and url not in _seen_urls:
        stream_profiles.append({
            "url":           url,
            "stream_width":  details.get("stream_width"),
            "stream_height": details.get("stream_height"),
            "stream_codec":  details.get("stream_codec"),
            "stream_fps":    details.get("stream_fps"),
        })
        _seen_urls.add(url)
    # DB-probed sub next
    if sub_url and sub_url not in _seen_urls:
        # 2.4.0-rc2.9: pull the captured details from sub_details
        # (populated up at the db_streams branch) instead of writing
        # None across the board.
        stream_profiles.append({
            "url":           sub_url,
            "stream_width":  sub_details.get("stream_width"),
            "stream_height": sub_details.get("stream_height"),
            "stream_codec":  sub_details.get("stream_codec"),
            "stream_fps":    sub_details.get("stream_fps"),
        })
        _seen_urls.add(sub_url)
    # Each validated locked-stream addition
    for add in additional_streams:
        au = add.get("url", "")
        if au and au not in _seen_urls:
            stream_profiles.append({
                "url":           au,
                "stream_width":  add.get("stream_width"),
                "stream_height": add.get("stream_height"),
                "stream_codec":  add.get("stream_codec"),
                "stream_fps":    add.get("stream_fps"),
            })
            _seen_urls.add(au)

    # 2.4.0-rc2.9: dedup stream_profiles on (codec, width, height).
    # When the same camera surfaces multiple URLs that resolve to
    # the same encoder/resolution combination — common on Hikvision
    # which exposes /Streaming/Channels/101 and /h.264/ch1/main/
    # av_stream and /Streaming/Channels/1 as three separate URLs all
    # backed by the same 2560x1440 HEVC encoder — the dropdown was
    # showing duplicates. First-discovery wins (pre-auth main →
    # DB-probed sub → validated locked candidates in walker order),
    # which gives users the canonical brand-recommended URL rather
    # than the alternate-form variant. Entries with None resolution
    # stay distinct (probe failed for some reason — better to keep
    # both than risk collapsing actually-different streams).
    _deduped: list[dict] = []
    _seen_keys: set = set()
    _dups_dropped = 0
    for sp in stream_profiles:
        w = sp.get("stream_width")
        h = sp.get("stream_height")
        c = sp.get("stream_codec")
        # Build dedup key. None values are preserved as-is and produce
        # a key that won't collide with concrete (w,h,codec) triples
        # OR with other None-bearing keys for different URLs — that
        # latter property is achieved by mixing the URL into the key
        # when any axis is None, ensuring "unknown" entries always
        # stay distinct.
        if w and h and c:
            key = ("known", c, w, h)
        else:
            # Any None → make key URL-unique so it can't collapse
            # against another unknown
            key = ("unknown", sp.get("url", ""))
        if key in _seen_keys:
            _dups_dropped += 1
            continue
        _seen_keys.add(key)
        _deduped.append(sp)
    if _dups_dropped:
        log.info(f"  stream_profiles dedup: dropped {_dups_dropped} "
                 f"duplicate entry/entries on (codec, width, height)")
    stream_profiles = _deduped
    log.info(f"  stream_profiles: built {len(stream_profiles)} "
             f"entry/entries (1 main + "
             f"{1 if sub_url else 0} sub + "
             f"{len(additional_streams)} validated locked)")

    camera.update(credentials=enc_creds,
                  stream_url=url, sub_stream_url=sub_url,
                  # 2.4.0-rc2.6: persist the validated additional
                  # streams. UI can offer them as alternative
                  # resolutions in the focus-view dropdown. Always
                  # written (may be empty list).
                  additional_streams=additional_streams,
                  # 2.4.0-rc2.8: persist stream_profiles built above
                  # so the focus-view dropdown reads the canonical
                  # list directly without needing the synth fallback.
                  stream_profiles=stream_profiles,
                  # 2.6.0-rc3.0 Item 4: only clear locked_streams if the
                  # user explicitly came in via the badge modal. The
                  # validated subset is in additional_streams either way
                  # (we always run post-auth validation above), so it's
                  # safe to keep the unvalidated list around. Users who
                  # logged in via the generic Login button keep the
                  # 🔒 badge visible and can review locked candidates
                  # later if they want.
                  locked_streams=([] if from_locked_modal
                                  else camera.get("locked_streams", []) or []),
                  requires_credentials=False,
                  status="ready", user_saved=True,
                  http_snap_url=http_snap_url,
                  http_snap_auth_mode=http_snap_auth_mode,
                  **details)
    # rc2.2: brand-id pass on the updated camera record. camera.update()
    # above merges fields onto the existing record (so manufacturer
    # survives if it was already set), but if pre-cred discovery never
    # identified the brand (no mac_vendor / no usable HTTP signals /
    # no captured RTSP server header), this catches the case where the
    # newly-stored stream_url + db_entry match would identify it now.
    if not camera.get("manufacturer"):
        try:
            _re_id = _identify_camera_brand(camera, force=True)
            if _re_id and _re_id["name"] != "Generic IP Camera":
                log.info(f"  Brand identified post-cred-auth: "
                         f"{_re_id['name']}")
        except Exception as e:
            log.debug(f"  brand-id (post-cred-auth RTSP): {e}")
    save_cameras()
    log.info(f"Credentials accepted for {camera_id}: {_strip_creds(url)}")
    # 2.5.0-rc1.2: kick off channel enumeration as a fire-and-forget
    # task. Channel-iterate brands (Lorex/Dahua DVR-NVR family) expose
    # multiple physical-camera channels under a single IP:port, and
    # the user's expectation (per the original 2026-05-02 plan) is
    # one card per populated channel. The task walks remaining
    # channels with the validated credentials and registers any
    # populated ones as additional CAMERAS entries. No-op for non-
    # channel_iterate brands. Doesn't block the cred-auth response —
    # the user gets confirmation immediately, additional cards
    # appear over the next few seconds as enumeration completes.
    #
    # 2.5.0-rc1.6: also set `dvr_enumeration_pending` on the response
    # so the UI knows to schedule a delayed loadCameras() refetch.
    # Without this hint, the UI's immediate post-cred-auth
    # loadCameras() runs BEFORE the background enumeration completes,
    # and there's no periodic /api/cameras poll, so the new cards
    # don't surface in the grid until the next user-initiated state
    # change. Field-confirmed in 2.5.0-rc1.5 log: cred-auth at 00:20:42,
    # 7 cards registered at 00:20:46, but UI didn't show them until
    # 00:22:06 when an unrelated periodic scan completed and that
    # scan's onComplete handler triggered loadCameras() as a side-
    # effect. Setting the flag bridges the gap deterministically.
    enum_pending = False
    try:
        _post_brand = _identify_camera_brand(camera) or {}
        _post_recipe = _post_brand.get("streaming_recipe") or {}
        if _post_recipe.get("type") == "channel_iterate":
            enum_pending = True
    except Exception:
        pass
    try:
        asyncio.create_task(
            _enumerate_dvr_channels_after_auth(camera_id))
    except Exception as e:
        log.debug(f"  channel-enum spawn: {e}")
    return web.json_response({"status": "ok",
                              "stream_url": _strip_creds(url),
                              "dvr_enumeration_pending": enum_pending,
                              **details})


# ── 3.1.0 (B25): read a camera's streams again with the saved password ──────
# AnyCam reads a camera's streams only when its password is entered, and saves
# them. After a change in the camera's settings the saved streams can be out
# of date: on 2026-10-02 the Hikvision PTZ card ran on snapshots until its
# password was entered again. Now AnyCam does that itself when a card cannot
# use what is saved. It runs the same password step (api_set_credentials),
# which paces rate-limited cameras, at most once every STREAM_REFRESH_MIN_S
# for each camera.
STREAM_REFRESH_MIN_S = 6 * 3600.0
_STREAM_REFRESH_AT: dict[str, float] = {}     # camera_id -> time.monotonic() of the last try


class _SavedPasswordRequest:
    """The one part of a request api_set_credentials reads: its JSON body."""

    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


async def _streams_refresh(camera_id: str, why: str) -> bool:
    """Read the camera's streams again with its saved password; True on success."""
    camera = CAMERAS.get(camera_id)
    if (not camera or not camera.get("credentials") or camera.get("_dvr_parent_id")
            or camera.get("display") in ("webrtc", "wsrtsp", "info", "appliance")):
        return False
    now = time.monotonic()
    last = _STREAM_REFRESH_AT.get(camera_id)
    if last is not None and now - last < STREAM_REFRESH_MIN_S:
        return False
    _STREAM_REFRESH_AT[camera_id] = now
    try:
        username, password = decrypt_creds(camera["credentials"])
    except Exception as exc:
        log.debug(f"Streams [{camera_id}]: saved password unreadable: {exc}")
        return False
    log.info(f"Streams [{camera_id}]: reading the streams again with the saved "
             f"password ({why})")
    resp = await api_set_credentials(_SavedPasswordRequest(
        {"camera_id": camera_id, "username": username, "password": password}))
    ok = resp.status == 200
    cam = CAMERAS.get(camera_id) or {}
    if ok:
        log.info(f"Streams [{camera_id}]: {len(cam.get('stream_profiles') or [])} "
                 f"stream(s) saved again")
    else:
        log.warning(f"Streams [{camera_id}]: the streams could not be read again "
                    f"(HTTP {resp.status}); the saved ones stay")
    return ok


async def api_clear_credentials(request: web.Request) -> web.Response:

    cid    = request.match_info["camera_id"]
    camera = CAMERAS.get(cid)
    if not camera:
        return web.json_response({"error": "Not found"}, status=404)
    camera.update(credentials=None,
                  stream_url=_strip_creds(camera.get("stream_url","")),
                  requires_credentials=True, status="needs_credentials")
    save_cameras()
    return web.json_response({"status": "ok"})


async def api_deep_reprobe(request: web.Request) -> web.Response:
    """User-invoked deep re-probe of a single camera card.

    3.8.0-rc1.0 (B54): one full walk of every path with no early stop: Layer 1
    keeps each 401 path as a locked stream, then Layer 2 runs (also on
    skip_layer2 brands; never on rate-limited ones, which reset a second
    connection). The 2.4.0-rc2.4 resume of an early-stopped walk is gone,
    because the scan no longer stops early on same-realm 401s.

    Concurrency: cam.deep_reprobe_in_progress flag suppresses
    overlapping invocations. Frontend disables the button while the
    handler is in flight.
    """
    cid = request.match_info["camera_id"]
    cam = CAMERAS.get(cid)
    if not cam:
        return web.json_response({"error": "Not found"}, status=404)

    if cam.get("deep_reprobe_in_progress"):
        return web.json_response(
            {"error": "Deep re-probe already in progress for this card"},
            status=409,
        )

    ip   = cam["ip"]
    port = cam["port"]
    cam["deep_reprobe_in_progress"] = True
    save_cameras()

    loop = asyncio.get_event_loop()
    log.info(f"Deep Re-Probe started: {cid}")

    # Build host_meta from camera record so brand-aware logic still
    # works inside the resumed walk
    host_meta = {
        "ip":             ip,
        "hostname":       cam.get("hostname", ip),
        "mac_addr":       cam.get("mac_addr", ""),
        "mac_vendor":     cam.get("mac_vendor", ""),
        "vendor":         cam.get("mac_vendor", ""),
        "manufacturer":   cam.get("manufacturer", ""),
        "page_title":     cam.get("page_title", ""),
        "server_header":  cam.get("server_header", ""),
        "rtsp_server_header": cam.get("rtsp_server_header", ""),
        "rtsp_auth_realm":    cam.get("rtsp_auth_realm", ""),
        "rtsp_auth_scheme":   cam.get("rtsp_auth_scheme", ""),
        "rtsp_public_methods": cam.get("rtsp_public_methods", ""),
        "onvif_scopes":   cam.get("onvif_scopes", ""),
    }

    found_url: str | None = None
    new_locked: list[dict] = []

    try:
        # 3.8.0-rc1.0 (B54): one full walk of every path, no early stop,
        # each 401 path kept as a locked stream, then Layer 2 (also on
        # skip_layer2 brands, never on rate-limited ones). The scan no longer
        # stops early on same-realm 401s, so there is no state to resume.
        log.info(f"  Deep Re-Probe: full walk, Layers 1 and 2")
        found_url = await loop.run_in_executor(
            _THREAD_POOL, lambda: find_rtsp_path(ip, port, "", "", host_meta, deep=True))
        new_locked = list(cam.get("locked_streams", []) or [])
        seen_paths = {l.get("path") for l in new_locked}
        for l in host_meta.get("locked_streams", []) or []:
            if l.get("path") not in seen_paths:
                new_locked.append(l)
                seen_paths.add(l.get("path"))

        # Update the camera record with the outcome
        if found_url:
            cam["stream_url"] = found_url
            cam["protocol"] = "RTSP"
            cam["status"] = "ready"
            cam["requires_credentials"] = False
            log.info(f"  Deep Re-Probe SUCCESS: {cid} → "
                     f"{_strip_creds(found_url)}")
        else:
            log.info(f"  Deep Re-Probe: no working stream found for "
                     f"{cid} (locked candidates: {len(new_locked)})")

        if new_locked:
            cam["locked_streams"] = new_locked

        # Clear early-bail state — we did the deep work, no more
        # skipped paths to resume.
        for k in ("early_bail_reason", "early_bail_realm",
                  "early_bail_paths_tried", "early_bail_paths_remaining",
                  "early_bail_at"):
            cam.pop(k, None)

        # Record outcome stats
        cam["deep_reprobe_attempts"] = int(
            cam.get("deep_reprobe_attempts", 0)) + 1
        cam["deep_reprobe_last_at"] = datetime.datetime.utcnow().isoformat()
        cam["deep_reprobe_last_outcome"] = (
            "ready" if found_url
            else (f"locked_streams:{len(new_locked)}"
                  if new_locked else "no_streams"))
    except Exception as e:
        log.error(f"Deep Re-Probe error for {cid}: {e}", exc_info=True)
        cam["deep_reprobe_last_outcome"] = f"error:{str(e)[:80]}"
    finally:
        cam["deep_reprobe_in_progress"] = False
        save_cameras()

    return web.json_response({
        "status":         "ok",
        "found_stream":   bool(found_url),
        "stream_url":     _strip_creds(found_url) if found_url else "",
        "locked_count":   len(new_locked),
        "outcome":        cam.get("deep_reprobe_last_outcome", ""),
    })


async def api_add_camera(request: web.Request) -> web.Response:

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    ip        = data.get("ip","").strip()
    port      = int(data.get("port", 554))
    protocol  = data.get("protocol","RTSP").upper()
    # 3.0.1 (B23): the scan names these "WebRTC" and "WS-RTSP" (display
    # "webrtc", "wsrtsp"). Upper case alone never matched the check below,
    # so a WebRTC camera added by hand always failed with 400.
    protocol  = {"WEBRTC": "WebRTC"}.get(protocol, protocol)
    name      = data.get("name","").strip() or f"{ip}:{port}"
    username  = data.get("username","").strip()
    password  = data.get("password","")
    rtsp_path = data.get("rtsp_path","").strip()

    if not ip:
        return web.json_response({"error": "IP is required"}, status=400)

    cid  = f"{ip}_{port}_manual"
    loop = asyncio.get_event_loop()
    url  = None

    if protocol in ("RTSP", "DVR", "ONVIF"):
        if rtsp_path:
            test_url = f"rtsp://{ip}:{port}{rtsp_path}"
            if await loop.run_in_executor(_THREAD_POOL, probe_rtsp, test_url, username, password):
                url = test_url
        if not url:
            url = await loop.run_in_executor(_THREAD_POOL, find_rtsp_path, ip, port, username, password)
    elif protocol == "MJPEG":
        url = await loop.run_in_executor(_THREAD_POOL, probe_mjpeg_http, ip, port, username, password)
    elif protocol == "HLS":
        url = await loop.run_in_executor(_THREAD_POOL, probe_hls, ip, port, username, password)
    elif protocol == "RTMP":
        if await loop.run_in_executor(_THREAD_POOL, probe_rtmp, ip, port):
            url = f"rtmp://{ip}:{port}/live/stream"

    if not url and protocol not in ("WebRTC", "WS-RTSP"):
        return web.json_response(
            {"error": f"Could not connect to {ip}:{port} via {protocol}. "
                      "Check IP, port, protocol and credentials."}, status=400)

    CAMERAS[cid] = {
        "id": cid, "ip": ip, "hostname": ip, "port": port,
        "protocol": protocol, "stream_url": url or "",
        "requires_credentials": False,
        "credentials": encrypt_creds(username, password) if (username and url) else None,
        "name": name, "status": "ready" if url else "info",
        "display": "proxy" if url else {"WebRTC": "webrtc", "WS-RTSP": "wsrtsp"}.get(
            protocol, protocol.lower()),
        "user_saved": True, "verdict": "camera", "verdict_reason": "Manually added",
    }
    save_cameras()
    return web.json_response({"status": "ok", "camera_id": cid})
