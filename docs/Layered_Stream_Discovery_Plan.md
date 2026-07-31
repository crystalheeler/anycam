# Layered Stream Discovery + CAMERA_DB Sync Plan

**Status:** PROPOSED — awaiting CrystalHeeler's sign-off
**Target build:** AnyCam 2.4.0-rc2.0 (single combined release)
**Last updated:** 2026-05-03

## Goals

This release does three things in one shipping unit:

1. **Layered Stream Discovery (the original feature):** when a scan finds a working unauthenticated RTSP stream, don't bail early. Continue probing the remaining brand-known paths, collecting any 401 responses as "locked stream candidates." Surface these in the UI behind a "View Locked Streams" badge so the user can enter credentials and unlock additional streams (sub-streams, third streams, audio-only feeds, etc.) that they wouldn't otherwise have discovered.

2. **CAMERA_DB sync from spreadsheet → code:** sync all the brand-aware fields from the v8 spreadsheet that are not yet in code (`RTSP Main Stream`, `RTSP Sub Stream`, `RTSP Additional Streams`, `MJPEG URL`, `HTTP Snapshot URL`, `Streaming Recipe`, `Skip ONVIF`, `Skip Layer 2`, `RTSP Port`, `HTTP Port`). This is data-only — no consumption changes.

3. **Nmap port augmentation:** add the small set of camera-relevant TCP ports that aren't already in nmap top-1000 (likely just 6 ports). 5-line code change.

## Non-goals (deferred to rc3.x or later)

- **Brand-recipe path discovery during initial scan** — i.e. consuming `streaming_recipe` to short-circuit Layer 1's generic 25-31-path walk. Deferred to rc3.x where the Lorex/Dahua DVR Family work needs it for channel iteration anyway.
- **Channel iteration logic** — DVR/NVR channel-by-channel probing. Deferred to rc3.x.
- **STREAM_DB → CAMERA_DB consolidation** — deferred to rc4.x (the cleanup bundle).
- **`Default Ports` consultation at runtime** — we only USE `default_ports` for nmap augmentation in rc2.0; full per-camera port preference is rc3.x+.

## Concrete changes

### 1. Spreadsheet → CAMERA_DB sync (data-only)

**New CAMERA_DB fields** to be populated where applicable:
- `rtsp_main_stream` — string, the canonical RTSP path for the main stream
- `rtsp_sub_stream` — string, the canonical RTSP path for the sub stream
- `rtsp_additional_streams` — list of strings, third stream / audio / etc
- `mjpeg_url` — string, MJPEG-over-HTTP path (not consumed yet)
- `http_snap_url` — string, HTTP snapshot path (the scope of the rc4.x consolidation)
- `streaming_recipe` — dict, for NVR/DVR families with channel iteration (consumed in rc3.x, not rc2.0)
- `skip_onvif` — bool, default False, only set True with HIGH confidence
- `skip_layer2` — bool, default False, only set True with HIGH confidence
- `default_rtsp_port` — int, default 554
- `default_http_port` — int, default 80

**For rc2.x we ONLY consume `skip_layer2` at runtime** (the existing find_rtsp_path Layer 2 gate respects the brand metadata). The other fields are populated for use in rc3.x.

### 2. Nmap port augmentation

**Spreadsheet `Default Ports` union (across all brands):** 80, 88, 443, 554, 2020, 2543, 4550, 7441, 7447, 8000, 8080, 8554, 8765, 8888, 9000, 34567, 35000, 37777

**Likely NOT in nmap top-1000:** 4550, 7441, 7447, 8765, 34567, 35000

**Action:** in `focused_nmap_scan()`, change the nmap command from `--top-ports 1000` to `--top-ports 1000 -p 4550,7441,7447,8765,34567,35000` (the `-p` flag adds those specific ports on top of the top-1000 set, deduplicated by nmap). 5-line code change.

**Verification:** I'll run a real nmap query at code time to confirm which of those 6 are actually missing from the live top-1000 list (rather than relying on memory). The spec is "augment with the missing ones, not the union" — verified at build time.

### 3. Layered Stream Discovery (the actual feature)

**Modify `find_rtsp_path` (or its caller) to NOT bail early on first hit.** Currently the path-walker stops at the first 200-OK DESCRIBE response. After this change, the walker continues to walk the remaining paths in the candidate list. Any path that returns a 401 (auth-required) without an `Authorization` header in the request is recorded as a "locked stream candidate" with its realm and scheme.

**Locked stream candidates stored on the camera record:**
```python
camera["locked_streams"] = [
    {"path": "/Streaming/Channels/102", "realm": "IP Camera(C7)", "scheme": "Digest"},
    {"path": "/Streaming/Channels/103", "realm": "IP Camera(C7)", "scheme": "Digest"},
    ...
]
```

**Skip the continued walk entirely** if credentials are already saved for the camera (the existing cred-auth flow handles those). Skip also if `skip_layer2: True` for the brand AND we're past Layer 1 (don't grind multi-socket on brands where it's known to fail).

**UI:**
- New badge on camera card: "🔒 View Locked Streams (N)" — visible only when `locked_streams` is non-empty AND no creds are saved for the camera.
- Tapping the badge opens a modal listing the locked streams (path + realm shown).
- Modal includes a credential entry prompt (existing username/password fields).
- On credential entry, the existing cred-auth flow attempts the locked streams and adds successful ones to the camera's stream profiles. The user's credential machinery remains unchanged — Layered Stream Discovery just adds the locked-stream URLs as candidates for that machinery to try.

## Research summary — what was confirmed

I researched 17 camera manufacturers across 63+ unique sources (manufacturer-official documentation, support knowledge bases, third-party VMS docs, user forums, GitHub discussions). Below is what was confirmed at HIGH confidence.

### Manufacturers with documented streaming_recipe (NVR/DVR specifically)

| Brand | NVR Recipe | Channel Indexing | RTSP Port | Skip ONVIF | Skip Layer 2 |
|---|---|---|---|---|---|
| Hikvision NVR | `/Streaming/Channels/{ch}0{stream}` | 1-based | 554 | No | No |
| Lorex/Dahua DVR-NVR | `/cam/realmonitor?channel={ch}&subtype={sub}` | 1-based | 554 | Yes | Yes (lockout risk) |
| Hanwha NVR | `/LiveChannel/{ch}/media.smp/profile={n}` | **0-based** | **558** (last device port) | No | No |
| Uniview NVR | `/unicast/c{ch}/s{stream}/live` | 1-based | 554 | No | No |
| Reolink NVR | `/Preview_{ch:02d}_{stream}` | 1-based | 554 | No | No |
| Vivotek NVR | `/Media/Live/Normal?camera=C_{ch}&streamindex={n}` | 1-based | 554 | No | No |
| Amcrest NVR | (Dahua format) | 1-based | 554 | No | No |
| Swann NVR (Hikvision-OEM) | `/Streaming/Channels/{ch}0{stream}` | 1-based | 554 | No | No |
| ANNKE NVR (Hikvision-OEM) | `/Streaming/Channels/{ch}0{stream}` | 1-based | 554 | No | No |

### Manufacturers with documented IP camera streams (single channel, no recipe needed)

| Brand | Camera Path | Notes |
|---|---|---|
| Hikvision IPC | `/Streaming/Channels/101` (main), `/102` (sub) | Static |
| Dahua IPC | `/cam/realmonitor?channel=1&subtype=0` | Static when single-camera |
| Hanwha IPC | `/profile1/media.smp` (main), `/profile2/media.smp` (sub) | Static |
| Uniview IPC | `/media/video1` (main), `/media/video2` (sub) | Static |
| Reolink IPC | `/Preview_01_main`, `/Preview_01_sub` | Older: `/h264Preview_01_main` |
| Axis IPC | `/axis-media/media.amp` | Multi-sensor: `?camera={N}` |
| Vivotek IPC (older) | `/live.sdp`, `/live2.sdp` | Newer: `/media2/stream.sdp?profile={token}` |
| Foscam IPC | `/videoMain`, `/videoSub` | Port 88 OR 554 |
| ACTi IPC | `/track1` (single), `/track{N}` (multi) | Older firmware: port 7070 |
| Bosch IPC | `/` (root!), `/?inst=2` (sub), `/rtsp_tunnel` | Multi-channel: `/?line={N}` |
| Avigilon IPC | `/defaultPrimary?streamType=u` | Sub: `/defaultSecondary?streamType=u` |
| Pelco Sarix | `/stream1`, `/stream2` | HTTP tunneling supported |
| Amcrest IPC | `/cam/realmonitor?channel=1&subtype=0` | Dahua format |
| UniFi Protect | `rtsps://IP:7441/{camera_id}` or `rtsp://IP:7447/{camera_id}` | Camera ID required, can't iterate |

### Brands where streaming_recipe was deliberately LEFT BLANK (insufficient confidence)

- **Honeywell:** Mixed OEM (Hikvision in some product lines, Dahua in others). Multiple sources recommend "try Hikvision and Dahua URLs" but no single canonical pattern. Per HIGH-confidence-only rule: leave blank.
- **FLIR (commercial):** Same as Honeywell — Dahua-OEM in most lines, military-grade is locked anyway.
- **EZVIZ:** RTSP usually disabled at the factory. Even though it's Hikvision-OEM, can't reliably stream.
- **Wyze, Xiaomi, Ring, Nest, Arlo, Blink:** Cloud-only cameras, no RTSP.
- **Verkada:** Cloud-only (Vivotek hardware, but cloud-locked). They've publicly committed to "unlock RTSP if they go bankrupt" but until then, no recipe.

## Code changes summary

| File | Change | Lines |
|---|---|---|
| `camera_discovery.py` | Add new fields to ~30 CAMERA_DB entries | ~150 |
| `camera_discovery.py` | Modify `find_rtsp_path` to continue walking after first hit, collect 401 responses | ~40 |
| `camera_discovery.py` | New `_collect_locked_streams` helper | ~30 |
| `camera_discovery.py` | Add `locked_streams` field through `_safe_cam`, `_id_preserve`, record-build sites | ~20 |
| `camera_discovery.py` | UI: "View Locked Streams" badge + modal | ~80 |
| `camera_discovery.py` | Augment nmap port list | ~5 |
| `verify_release.py` | Allowlist new CAMERA_DB fields | ~10 |
| `CHANGELOG.md` | rc2.0 entry | ~50 |
| `config.yaml` | Version bump to 2.4.0-rc2.0 | 1 |

**Total: ~390 LoC, mostly data**

## New version of the spreadsheet (v9)

I'll generate a new spreadsheet with the new data populated:
- Streaming Recipe filled out for the 9 NVR/DVR families above (currently empty for most)
- Skip ONVIF filled out for the brands where confirmed (most are NO)
- Skip Layer 2 filled out for the brands where confirmed
- Confidence values populated for each new data field
- Legend updated to v9 changelog

The spreadsheet will be the source of truth, the code DB will be synced from it.

## Acceptance criteria

1. After scan, cameras with successful unauthenticated streams that have additional 401-locked paths show "View Locked Streams (N)" badge.
2. Tapping the badge surfaces the locked stream list with credential prompt.
3. After credential entry, locked streams successfully convert to active stream profiles (where credentials are valid).
4. Camera with already-saved credentials does NOT show the badge (we already have those streams).
5. Cameras with `skip_layer2: True` brand metadata don't get multi-socket grinding during the continued walk.
6. Nmap scan includes the augmented ports (verified by inspecting log of nmap command).
7. All existing functionality from rc1.0 preserved — RTSP OPTIONS Fingerprint still works, Lorex/Dahua DVR family still identified by realm.
8. All 5 release gates pass.
9. Live test against the Microseven Hipcam, Hikvision, Lorex DVR shows expected behavior.

## Open questions for CrystalHeeler before coding

1. **UI confirmation:** "View Locked Streams (N)" badge with modal — match your mental model? Or did you envision something different?

2. **Locked stream candidate selection:** When walking the brand-aware path list and finding a 401 on path X, do we add path X to `locked_streams`? Or only if the realm matches the auth realm we already captured (i.e. it's the same auth domain)? My recommendation: only same-realm 401s count, to avoid surfacing locked streams that need different credentials.

3. **Throttle awareness during continued walk:** the continued walk should respect existing throttle metadata (e.g. for Hipcam, observe the 5s cooldown between probes). My plan: yes, automatic — the existing `_throttle_wait` machinery still runs because we're using the same probe code path. Confirm OK?

4. **Skipping based on product line:** the v8 spreadsheet has the Lorex/Dahua DVR-NVR Family as a single row, but most other brands have separate IP camera and NVR rows. Should I add separate NVR rows for: Hikvision NVR, Hanwha NVR, Uniview NVR, Reolink NVR, Vivotek NVR, Amcrest NVR, Swann NVR, ANNKE NVR? My recommendation: yes, ~8 new rows.

5. **`streaming_recipe` field structure:** I've sketched it as a dict like `{"format": "/Streaming/Channels/{ch}0{stream}", "channels": "1-32", "channel_base": 1, "stream_main": 1, "stream_sub": 2}`. This is rc3.x consumption format — but we're populating it now in rc2.0. Sound right, or want a different structure?

## Source bibliography (for the audit PDF)

63+ unique sources across 17 manufacturers. Manufacturer-official sources marked (*).

- *Hikvision: supportusa.hikvision.com, securitycamcenter.com, videoexpertsgroup.com, ipcamtalk.com, techage.com, visioforge.com, use-ip.co.uk forum
- *Dahua: dahuawiki.com (x2), dahuatech.zendesk.com, monitoreal.com (PDF), ipcamlive.com, support.visiotechsecurity.com, help.angelcam.com, techage.com, getscw.com, videoexpertsgroup.com
- *Hanwha: support.hanwhavisionamerica.com (x2), support.hanwhavision.eu (x2), support.hanwhavision.com, hanwhavisionsupport.com, getscw.com, ipcamlive.com, verheles.com
- *Uniview: global.uniview.com (PDF), univiewtechnology.com (PDF), forums.developer.nvidia.com, securitysystemdepot.com, support.visiotechsecurity.com, knowledge.ic.plus, securitycamcenter.com, support.gigamedia.net, ipcamlive.com
- Honeywell: getscw.com, community.geniusvision.net, camlytics.com, ispyconnect.com, security.world, *prod-edam.honeywell.com (User Guide PDF), camera-sdk.com, cctv.supplies
- *Ubiquiti: webrtsp.com, hostifi.com, community.ui.com (x4)
- *Reolink: support.reolink.com (x2), reolink.com, community.reolink.com (x2), ipcamlive.com, github frigate, reolink.com (PDF)
- *Axis: developer.axis.com (x3), securitycamcenter.com, learncctv.com, openeye.net, getscw.com, ipcamlive.com, ipcamtalk.com, obsproject.com forum
- *Vivotek: vivotek.zendesk.com (x3), security.world, ispyconnect.com, getscw.com, ipcamlive.com, crestron.com (test report PDF), community.geniusvision.net
- *Foscam: foscam.com, securitycamcenter.com, cctv.supplies, ipcamlive.com, ispyconnect.com, ipcamtalk.com, hdashboards.app, camlytics.com
- *ACTi: acti.com (PDF), www2.acti.com, getscw.com, ipcamtalk.com, security.world, ispyconnect.com, avertx.com, scribd
- *Bosch: keenfinity-group.com, scribd RTSP guide, ipcamlive.com, smartvision.dev, ipcameramaster.com, ispyconnect.com, nsoft.vision, camera-sdk.com, camlytics.com, community.geniusvision.net
- *Avigilon: support.avigilon.com (x4), docs.avigilon.com, ipcamlive.com, getscw.com, nsoft.vision
- *Pelco: support.pelco.com (x3), pdn.pelco.com, getscw.com, cctv.supplies, ispyconnect.com, camera-sdk.com, camlytics.com
- *Amcrest: support.amcrest.com (x2), amcrest.com forum, visioforge.com, ipcamtalk.com, community.homehabit.app
- *Annke: help.annke.com (x2), ipvm.com, ispyconnect.com, camlytics.com
- *Swann: flynsarmy.com, ipcamtalk.com, getscw.com, ebenezertechs.com, support.actiontiles.com, ispyconnect.com, camera-sdk.com, manualslib (Swann manual), forum.monoclecam.com
