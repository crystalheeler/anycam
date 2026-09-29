# AnyCam Build Plan

**Compiled:** 28 September 2026, at 2.6.3 (tagged locally, not yet pushed).
**Purpose:** the open task list that CLAUDE.md rule 7 says to consult before
every build. Update it as items close.

**Sources swept:** CLAUDE.md; session memory; this project's chat history;
`docs/legacy/MoreToDo.txt`; the four transcripts in `docs/legacy/*.docx`;
the plan documents in `docs/`; every deferred, known-issue and out-of-scope
note in `CHANGELOG.md`; the 2.6.x audit reports; and the source itself.
Each item was checked against the current code. Items that turned out to be
done are listed at the end, so they can come off the older lists.

No version numbers are assigned here. Rule 7: version and scope are
confirmed with CrystalHeeler at build time.

---

## Recommended order

1. **Field-test 2.6.3** (group A). Everything in group C depends on whether
   live view survives Home Assistant ingress.
2. **Motion detection** (B1). Reported broken, and it blocks live cards.
3. **Missing cameras** (B2).
4. **The three small correctness bugs** (B3, B4, B5).
5. **Live cards, then the automatic-quality discussion** (C1 to C3).
6. **Tests into the repo** (E7) before **the module split** (E1): the tests
   are the safety net the split needs.

---

## A. Prove 2.6.3 in the field

| # | Item | Why / source | Next step |
|---|---|---|---|
| A1 | Field-test live view | 2.6.3 audit: the real go2rtc binary, a real browser and HA ingress were never tested | Install; work the acceptance list in `docs/audits/anycam_2_6_3_audit_report.pdf`. Start with the H.264 camera at 192.168.50.73:8765 |
| A2 | Ingress WebSocket longevity | Largest single unknown. Ingress cut multipart after 10-24 frames in 1.6.0 | Leave one camera live for 10+ minutes. Stays live = MSE through ingress is viable |
| A3 | Promote `go2rtc_live_view` to default ON | Off by default until A1 and A2 pass | Flip the default in the release after a clean field test |
| A4 | Tier 1 checks never exercised | 2.6.1 audit | Look for the `fast_stream_start suppressed` line on the 4K Hikvision; a Microseven re-auth with no RST; then try `low_latency` ON |
| A5 | Decide `low_latency` | Behind an option since 2.6.1 | Default ON if A4 is clean, otherwise remove the option |
| A6 | The tens-of-seconds freezes | Open since 2026-09-26. Smart codec was already off, so keyframe scarcity is ruled out | Re-check after A1. Live view removes three of the four candidate causes. If freezes remain, logs first (rule 2) |
| A7 | Hikvision I-frame interval | CrystalHeeler to confirm the setting | Report the value. Target 1-2x the frame rate |
| A8 | Push 2.6.3 | 3 commits and the 2.6.3 tag are local only | On CrystalHeeler's order |

## B. Bugs

| # | Item | Why / source | Next step |
|---|---|---|---|
| B1 | **Motion detection does not record** | Reported 2026-09-28: either not detecting or not storing | Logs first (rule 2). Note: detection runs inside the thumbnail `snap_loop`, which exits 30 s after the last snapshot poll |
| B2 | **One or two cameras not discovered** | Reported 2026-09-28 | Logs first: scan log, plus IP and brand of each missing camera |
| B3 | `skip_nonref=true` breaks every h264 launch | CLAUDE.md open issue. ffmpeg rejects `nonref` as a `skip_frame` value. Still wired at `camera_discovery.py` line ~7080 | Remove the option, or change the value (`noref` / `bidir`) and field-test |
| B4 | Camera names written unescaped into HTML | Found in 2.6.3: the classic Enhanced View info bar puts `displayName(cam)` into `innerHTML`. Names are editable through the rename API | Escape it, then audit every `innerHTML` write that carries camera data |
| B5 | "Share with community" checkbox is ticked and does nothing | Found in 2.6.3: `build_html` never replaces `___COMMUNITY___`, so the browser sees a truthy string while the server has no endpoint | Add the replacement in `build_html` |
| B6 | Microseven RTSP "Invalid data found" | Regression from 2.3.x to 2.4.x, deferred since 2.4.0-rc3.3. Status today unknown | Confirm whether it still happens. If so: control run with plain ffmpeg, then bisect |
| B7 | Resolution dropdown rapid flicker | Latent, deferred at CrystalHeeler's request in 2.2.8-rc2.6 | Confirm it still happens, then decide |
| B8 | Scan progress bar jumps and is inaccurate | `MoreToDo.txt`; changelog 2.2.9 and 2.3.0 | Base progress on completed work rather than elapsed time |
| B9 | "Unknown child process pid X" warning | asyncio race, cosmetic (changelog 2.2.9) | Low priority |
| B10 | "Cannot connect to host 172.30.32.1:8099" for ~3 s at each start | 2.6.2 Supervisor log. Ingress knocks before the web server binds | Cosmetic. Start the web server before the slow startup steps |

## C. Video pipeline

| # | Item | Why / source | Next step |
|---|---|---|---|
| C1 | **Live video in cards** | CrystalHeeler approved 2026-09-28. Blocked on B1 and C2 | Use each camera's sub-stream: N live 4K HEVC decodes would overload the viewing device. The Hikvision sub-stream is MJPEG, so that card stays on snapshots |
| C2 | Motion detection independent of snapshot polling | Live cards stop the polling that keeps the thumbnail loop, and so motion detection, alive | Keep an armed camera's loop alive regardless of polls. May fold into B1 |
| C3 | **Automatic quality, and retiring the FPS toggle** | CLAUDE.md: the Enhanced View step-down redesign "requires full discussion before any coding." CrystalHeeler wants the FPS toggle gone in favour of automatic behaviour | **Correction to the 2026-09-26 research:** WebRTC congestion control cannot help passthrough; it needs an encoder to adjust, and go2rtc relays the stream unchanged. Automatic quality in live view means switching camera streams (main to sub) on stall. Discuss before coding |
| C4 | Thumbnails, motion and recording through go2rtc's local re-stream | CrystalHeeler's long-term interest from April 2026 ("Fix B", Technique 3). One camera session instead of several; go2rtc also copes with non-compliant RTSP transport | Enable go2rtc's RTSP server on 127.0.0.1 only; point `snap_loop` and recording at it. Most useful for the rate-limited Microseven |
| C5 | Audio in live view | 2.6.3 plays video only | Add `audio` to the player's media list; check codec support per browser |
| C6 | Cameras that speak WebRTC (WHEP) or RTSP-over-WebSocket | Detected since 1.x but shown as info cards only | go2rtc can play both natively |
| C7 | Classic view JPEG quality `q:v 2` | Very large frames at 4K. Bytes against picture quality | Moot if live view becomes the default (A3) |
| C8 | ZeroTier tuning | Deferred by CrystalHeeler 2026-09-26 | Run `zerotier-cli peers` and look for `RELAY`; review the 2800-byte MTU |
| C9 | Pi 4 `dtoverlay=rpivid-v4l2` check or toggle | Deferred since 2.2.8 | Lower priority now: live view does no decode on the Pi |

## D. Discovery and device support

| # | Item | Why / source | Next step |
|---|---|---|---|
| D1 | Lorex / Dahua family: remaining pieces | Channel enumeration, per-channel cards and realm handling shipped in 2.5.0. Still open: no empirical SDP from a known-empty channel, and a hard-coded 16-channel cap | Watch for phantom or missing channel cards; make the cap a per-entry setting |
| D2 | `STREAM_DB` to `CAMERA_DB` consolidation, and runtime use of `default_ports` | CLAUDE.md queued work | Scope a plan document first |
| D3 | Drag-to-reorder cards | Changelog 2.4.0-rc3.3: foundation laid by `_stableCardKey` | Feature; schedule when wanted |

## E. Code health

| # | Item | Why / source | Next step |
|---|---|---|---|
| E1 | Split the ~16,000-line file into modules | The 2.0.7 transcript called it "the single change with the largest positive impact"; confirmed feasible under HAOS then. The file has more than doubled since | After E7. Suggested modules: scan, stream, onvif, go2rtc, storage, web handlers, config |
| E2 | Global mutable variables refactor | CLAUDE.md queued (SigRev-3 Issue 2). 2.6.3 added 7 more, following the existing pattern | Fold into E1 |
| E3 | One long-lived `aiohttp.ClientSession` | aiohttp FAQ; the 2.0.7 list. 2.6.3 added 3 per-call sessions | Create one at startup, close it on shutdown |
| E4 | Strip credentials from every log line | 2.0.7 list: `_strip_creds` is applied in some places, not all | Audit every log call that can carry a URL |
| E5 | Complete type hints | 2.0.7 list | Incremental; do it alongside E1 |
| E6 | Remove the `/stream/{camera_id}` multipart endpoint | Unused since 1.6.0 | Delete the route and its handler |
| E7 | **Behavioural tests in the repo** | CLAUDE.md lists them as "aspirational". 2.6.3 produced 79 Python and 46 JS tests that live only in a session scratchpad | Move them into `tests/` and run them from the release gate |

## F. Release engineering

| # | Item | Why / source | Next step |
|---|---|---|---|
| F1 | A gate that checks the Dockerfile | 2.6.1 passed all five gates and could not build. Offered 2026-09-27 and declined for 2.6.2 | Query each package archive for every pinned version at release time |
| F2 | Gate P4 misses blocking reads other than `open()` | Found by hand in 2.6.3 (`read_bytes()` in an async handler) | Extend the check to `read_bytes`, `read_text` and `write_*` |
| F3 | `build.yaml` is deprecated | Supervisor warns on every build | Move the build parameters into the Dockerfile |
| F4 | Docker warning `InvalidDefaultArgInFrom` | `ARG BUILD_FROM` has no default | Give it a default base image |
| F5 | 20 of 25 audit "PDFs" are ZIP page bundles | Found 2026-09-27; their content is intact | Convert them to real PDFs |
| F6 | CLAUDE.md is out of date | Its queued work and open issues list items that have shipped (see "Done" below). It describes a Part 6 of the best-practices document that does not exist | CrystalHeeler to decide whether Part 6 was lost or never written; then refresh both lists |

---

## Done: take these off the older lists

| Item | Where it was listed | Evidence |
|---|---|---|
| Promote a stable rc to 2.6.0 final | CLAUDE.md | 2.6.0 tagged; 2.6.2 published 2026-09-28 |
| `fast_stream_start` harmful at 4K HEVC | CLAUDE.md | Gated by resolution and codec in 2.6.1 |
| ONVIF SOAP calls ignore the throttle | CLAUDE.md | `_rerun_onvif_auth` waits on the cooldown since 2.6.1 |
| Bundle rpi-ffmpeg | CLAUDE.md | Raspberry Pi Foundation ffmpeg through the apt origin pin since 2.6.0-rc2.0; `8:5.1.9-0+deb12u1+rpt1` confirmed on the Pi 2026-09-28 |
| RTSP OPTIONS fingerprint helper | CLAUDE.md, `MoreToDo.txt` | `_rtsp_options_fingerprint` exists and feeds `host_meta` |
| Layered Stream Discovery / "View Locked Streams" | `MoreToDo.txt`, changelog | Deep Re-Probe, `additional_streams`, `openLockedStreams` |
| Lorex multi-channel cards | Changelog 2.5.0-rc1.0 | Fixed in 2.5.0-rc1.7: eight distinct cards |
| Hikvision EOF after ~30 frames | `MoreToDo.txt` | Marked DONE there |
| Dead JS `_estimateCpuPct`, `_focusWarnOK` | `MoreToDo.txt`, changelog | No longer in the source |
| "Waiting for camera (throttled)" indicator | Throttle Q&A transcript: CrystalHeeler asked for it | Cards show "Authenticating (Camera rate-limited, ~30 seconds)…" with a warning dot |
| Microseven Enhanced View re-entry bug | Changelog 2.4.0-rc3.1 | Fixed as Bug A in 2.4.0-rc3.3 |
| Graceful shutdown, bounded thread pool, backoff jitter, AST duplicate check | 2.0.7 transcript | All present |
| Thumbnails via ONVIF snapshot URIs and brand snapshot paths | Feed-fix transcript (Techniques 4 and 9) | `onvif_get_snapshot_uri` in use; snapshot paths in `STREAM_DB` |
| Throttle-Aware Probe Pacing | Plan document | Superseded by 2.3.0 |
| Live view without decoding on the Pi (Tier 2) | 2026-09-26 research | 2.6.3, behind `go2rtc_live_view` |

## Not AnyCam work

- Hikvision auto-tracking lost after a firmware update
  (`docs/legacy/HikvisionInfo.docx`). A camera firmware question, not
  software in this repository.
