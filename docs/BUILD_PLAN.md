# AnyCam Build Plan

**Compiled:** 28 September 2026, at 2.6.3 (tagged locally, not yet pushed).
**Purpose:** the open task list that CLAUDE.md rule 7 says to consult before every build. Update it as items close.

**Sources swept:** CLAUDE.md; session memory; this project's chat history; `docs/legacy/MoreToDo.txt`; the four transcripts in `docs/legacy/*.docx`; the plan documents in `docs/`; every deferred, known-issue and out-of-scope note in `CHANGELOG.md`; the 2.6.x audit reports; field logs; and the source itself. Each item was checked against the current code. Items that turned out to be done are listed at the end, so they can come off the older lists.

No version numbers are assigned here. Rule 7: version and scope are confirmed with CrystalHeeler at build time.

A readable, colour-coded version is generated from this file by `docs/render_build_plan.py`.

---

## Status key

| Status | Meaning |
|---|---|
| You | Waiting on CrystalHeeler: a field test, a report, a decision, or an order |
| Logs | Needs logs before any investigation (CLAUDE.md rule 2) |
| Discuss | Needs a discussion before any code |
| Ready | Can be built when scheduled |
| Blocked | Waits on another item, named in "Next step" |
| Later | Parked by CrystalHeeler for later |

---

## Recommended order

1. **Field tests** (group A). Above all: install 2.6.3 on the Hikvision system (A9), leave a camera live for 10+ minutes (A2), and compare Classic against Live in Chrome (A10).
2. **Motion recording that never stops** (B1). Logs first.
3. **Missing cameras** (B2). Logs first.
4. **The small correctness bugs**: B3, B4, B5, B12.
5. **The automatic-behaviour discussion** (C11, C3), then **live cards** (C1).
6. **Tests into the repo** (E7), then **the module split** (E1). The tests are the safety net the split needs.

Hardware decode (B11) is parked for later, at CrystalHeeler's request.

---

## A. Field tests

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| A9 | You | **Install 2.6.3 on the Hikvision system and test live view** | The Hikvision DS-2DE4A425IW-DE at 10.0.0.33 is on the other HAOS system (172.16.0.35), which still runs an older build. CrystalHeeler asked to be reminded: do not let this one drop | Install 2.6.3, turn on Live View, open the Hikvision in Chrome. Its main stream is H.265 and should play live; its sub-stream is MJPEG and will always use the classic view |
| A2 | You | Does live view stay up through Home Assistant? | The biggest remaining unknown. Home Assistant's ingress proxy cut the old multipart streams after 10 to 24 frames in 1.6.0. Live view in Chrome played "perfectly" on 2026-09-28, but for how long was not recorded | Leave one camera live in Chrome for 10+ minutes. Still playing means live view through ingress is proven |
| A10 | You | Was the browser the problem all along? | CrystalHeeler's question, 2026-09-28. Prediction: no. Before 2.6.3, every browser received the same JPEG images, which Firefox and Chrome display identically; the Pi did the decoding. The browser only matters since live view, which hands the camera's H.265 to the browser | Quickest test: in 2.6.3, open a camera in Chrome and press **Classic**. That is the 2.6.2 pipeline in the same browser, on the same camera, at the same moment. Or reinstall 2.6.2 and use Chrome. If Classic in Chrome is as smooth as Live, the prediction is wrong |
| A11 | Blocked | Test live view on the H.264 camera at 192.168.50.73:8765 | H.264 plays live in every browser, Firefox included. Log1 shows its card polling for frames, so it is discovered, but every attempt fails on the Skip Non-Reference Frames setting, so the card shows nothing | Blocked on B3. With that setting off, the card should show video; then open it in Enhanced View |
| A7 | You | Hikvision I-frame interval | Faster start when opening a camera | Report the setting. Target: 1 to 2 times the frame rate |
| A4 | You | Tier 1 checks not yet exercised | 2.6.1 audit. The fast-stream-start suppression line was confirmed in Log1 on Lorex ch7 | Remaining: a Microseven re-auth with no connection reset (needs the test system A, see A9), and Low Latency Probe on and off (A5) |
| A5 | Blocked | Decide the Low Latency Probe option | Behind an option since 2.6.1. Its one-packet reorder queue drops packets that arrive out of order, and in H.265 a dropped packet can become a gray patch. It is on in CrystalHeeler's setup | Blocked on A11: test with it off and on once the H.264 camera works, then keep it or remove it |
| A6 | You | Tens-of-seconds freezes | The original complaint. None seen in live view on the first Chrome test | Watch for freezes over a longer live session. Freezes that happen only in the classic view point to the Pi decoder (B11) |
| A3 | Blocked | Turn Live View on by default | Off by default until proven | Blocked on A2 and A9 |
| A8 | You | Push 2.6.3 | 2.6.3 and later docs commits exist only locally | On CrystalHeeler's order |

## B. Bugs

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| B1 | Logs | **A motion recording may never stop** | CrystalHeeler, 2026-09-28: motion detection works and records, but once a recording starts it may not stop. Code fact: a recording is stopped automatically only inside the thumbnail loop, as each new frame arrives; the only other stop is disarming the camera. If that loop stops getting frames or exits, nothing ends the recording | Logs first: a log covering one full recording, from the motion that starts it to well past the cooldown |
| B2 | Logs | **One or two cameras are not discovered** | CrystalHeeler, 2026-09-28 | Logs first: the scan log, plus the IP and brand of each missing camera |
| B3 | Ready | Skip Non-Reference Frames breaks every camera it touches | ffmpeg rejects `nonref`; its valid value is `noref`. Confirmed in Log1: every launch of the H.264 camera at 192.168.50.73:8765 fails. The "~40% less CPU" in its description never happened, because the setting never applied | Fix the value and field-test, or remove the option (see C11). Until then, turning it off restores that camera |
| B4 | Ready | Camera names are written into the page unescaped | Found in 2.6.3: the classic Enhanced View info bar. Names are editable, so a crafted name could inject HTML | Escape it, then check every place the page inserts camera data |
| B5 | Ready | "Share with community" box is ticked and does nothing | Found in 2.6.3: a placeholder in the page is never filled in | Fill the placeholder when the page is built |
| B12 | Ready | VAAPI reported as available on a Pi 4 | Log1 startup. The Pi has no VAAPI device; the check only confirms ffmpeg was built with VAAPI | Check for a real device before reporting it available |
| B6 | Logs | Microseven "Invalid data found" | A regression from 2.3.x to 2.4.x, open since 2.4.0-rc3.3. Current status unknown | Confirm whether it still happens (test system A). If so, logs, a control run with plain ffmpeg, then compare builds |
| B7 | Later | Resolution dropdown flickers | Deferred at CrystalHeeler's request in 2.2.8-rc2.6 | Confirm it still happens |
| B8 | Ready | Scan progress bar jumps and is inaccurate | `MoreToDo.txt`; changelog 2.2.9 and 2.3.0 | Base progress on work completed, not time elapsed |
| B9 | Later | "Unknown child process pid" warning | Cosmetic (changelog 2.2.9) | Low priority |
| B10 | Later | "Cannot connect to host 172.30.32.1:8099" for about 3 s at start | 2.6.2 Supervisor log: Home Assistant connects before the web server is listening | Cosmetic. Start the web server before the slow startup steps |
| B11 | Later | **Hardware decode silently falls back to software** | Log1: ffmpeg cannot open the Pi's decoder devices (`/dev/media0` to `3`, "Operation not permitted"), then decodes in software, while AnyCam logs "hw first frame (hevc_drm)". Classic 4K runs at 8 fps; 2.6.0-rc2.5 measured 22 fps. The gray frames CrystalHeeler sees when the classic view bogs down fit this: ffmpeg's HEVC decoder fills any missing frame with mid-gray, and Log1 shows ch7 repeating one identical frame 400 times. Live view does no decoding on the Pi, which is why Chrome was clean | Parked by CrystalHeeler. When picked up: logs first, then find what changed since rc2.5 (HAOS or Supervisor update, ffmpeg 5.1.8 to 5.1.9, device permissions). Also make the log stop reporting hardware decode when it is not happening |

## C. Video and recording

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| C11 | Discuss | **Replace most or all of the option toggles with automatic behaviour** | CrystalHeeler, 2026-09-28. Nine toggles today, several broken or no longer relevant: Skip Non-Reference Frames never worked (B3), Fast Stream Start does nothing at 4K, and Live View makes several decode options irrelevant for the camera being watched | Separate discussion. For each toggle: remove it, make it automatic, or keep it. Pairs with C3 |
| C3 | Discuss | Automatic quality, and retiring the FPS control | CLAUDE.md: the Enhanced View step-down redesign "requires full discussion before any coding." Correction to the 2026-09-26 research: WebRTC congestion control cannot adjust a stream that go2rtc passes through unchanged | In live view, automatic quality means switching between the camera's own streams (main to sub) when playback stalls. Discuss before coding |
| C1 | Blocked | Live video in the camera cards | CrystalHeeler approved 2026-09-28. Motion detection works, so it no longer blocks this | Blocked on C2. Use each camera's sub-stream: many live 4K H.265 streams would overload the viewing device. The Hikvision sub-stream is MJPEG, so that card stays on snapshots |
| C2 | Ready | Motion detection that does not depend on snapshot polling | Motion detection runs inside the thumbnail loop, which stops 30 s after the last snapshot request. Live cards would stop those requests. Related to B1, where the same loop stops recordings | Keep an armed camera's loop running whether or not anyone is watching |
| C12 | Ready | Configurable recording length: 30 s, 1 min, 2 min, 5 min | CrystalHeeler, 2026-09-28 | New option. ffmpeg's segment muxer splits a recording into files with no re-encoding. It cuts at the first keyframe after each interval, so lengths are approximate |
| C10 | Ready | H.265 live view depends on the browser | Confirmed 2026-09-28: Chrome plays H.265 live; Firefox 156 and LibreWolf do not. go2rtc's own compatibility table lists Desktop Firefox as H.264-only for every live method, so this is not a security setting | Three changes. (1) Check what the browser can play before trying live view, and go straight to classic with a plain message instead of a red error. (2) Replace go2rtc's raw error text with something readable. (3) Optionally offer an H.264 sub-stream in live view, which trades resolution, so CrystalHeeler's decision |
| C4 | Discuss | Thumbnails, motion and recording through go2rtc's local stream | CrystalHeeler's long-term interest since April 2026 ("Fix B"). One camera connection instead of several, and go2rtc copes with cameras that break the RTSP rules | Discuss scope. Most useful for the rate-limited Microseven |
| C5 | Ready | Audio in live view | 2.6.3 plays video only | Add audio to the player; check codec support per browser |
| C6 | Later | Cameras that speak WebRTC or RTSP-over-WebSocket | Detected since 1.x, but shown as information cards only | go2rtc can play both |
| C7 | Later | Classic view JPEG quality | Very large frames at 4K | Moot once Live View is the default (A3) |
| C8 | Later | ZeroTier tuning | Deferred by CrystalHeeler 2026-09-26 | Check `zerotier-cli peers` for `RELAY`; review the 2800-byte MTU |
| C9 | Later | Pi 4 decoder overlay check or toggle | Deferred since 2.2.8 | Lower priority now that live view does no decoding on the Pi |

## D. Discovery and device support

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| D1 | Later | Lorex / Dahua: remaining pieces | Channel enumeration and per-channel cards shipped in 2.5.0. Still open: no sample of what a known-empty channel sends, and a fixed 16-channel limit | Watch for phantom or missing channel cards; make the limit a per-device setting |
| D2 | Discuss | Merge the two camera databases, and use each camera's default ports | CLAUDE.md queued work | Write a plan document first |
| D3 | Later | Drag to reorder cards | Changelog 2.4.0-rc3.3: the groundwork is in place | Build when wanted |

## E. Code health

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| E7 | Ready | **Automated tests in the repo** | CLAUDE.md calls them "aspirational". 2.6.3 produced 79 Python and 46 JavaScript tests, which live only in a session scratchpad | Move them into `tests/` and run them from the release gate |
| E1 | Blocked | Split the 16,000-line file into modules | The 2.0.7 review called this the single change with the largest positive impact; confirmed feasible under HAOS then. The file has more than doubled since | Blocked on E7. Suggested modules: scan, stream, onvif, go2rtc, storage, web handlers, config |
| E2 | Blocked | Reduce shared global state | CLAUDE.md queued (SigRev-3 Issue 2). 2.6.3 added 7 more, following the existing pattern | Blocked on E1: do it during the split |
| E3 | Ready | One shared HTTP client session | aiohttp's own guidance; on the 2.0.7 list. 2.6.3 added three more short-lived sessions | Create one at startup and close it at shutdown |
| E4 | Ready | Strip credentials from every log line | 2.0.7 list: applied in some places, not all | Check every log call that can carry a URL |
| E6 | Ready | Remove the old `/stream/{camera_id}` endpoint | Unused since 1.6.0 | Delete the route and its handler |
| E5 | Later | Complete type hints | 2.0.7 list | Do it alongside E1 |

## F. Release engineering

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| F6 | You | CLAUDE.md is out of date | Its queued-work and open-issue lists include items that have shipped (see Done). It describes a Part 6 of the best-practices document that does not exist | CrystalHeeler to say whether Part 6 was lost or never written; then refresh both lists |
| F1 | Ready | A release check for the Dockerfile | 2.6.1 passed every check and still could not build | Check each pinned package version against its archive at release time |
| F2 | Ready | The blocking-I/O check misses some calls | Found by hand in 2.6.3 | Extend it beyond `open()` to `read_bytes`, `read_text` and the write equivalents |
| F3 | Ready | `build.yaml` is deprecated | The Supervisor warns on every build | Move the build settings into the Dockerfile |
| F4 | Ready | Docker warning about the base-image argument | The argument has no default value | Give it a default |
| F5 | Ready | 20 of 25 older audit "PDFs" are ZIP bundles | Found 2026-09-27; their content is intact | Convert them to real PDFs |

---

## Done: take these off the older lists

| Item | Where it was listed | Evidence |
|---|---|---|
| Live view plays H.265 in Chrome | This plan, A1 | CrystalHeeler, 2026-09-28: Lorex DVR channels played "perfectly" in Chrome. Firefox and LibreWolf fall back to classic, as go2rtc's compatibility table predicts |
| Motion detection records | This plan, B1 (reported as not recording) | CrystalHeeler, 2026-09-28: it works |
| Promote a stable rc to 2.6.0 final | CLAUDE.md | 2.6.0 tagged; 2.6.2 published 2026-09-28 |
| Fast Stream Start harmful at 4K HEVC | CLAUDE.md | Gated by resolution and codec in 2.6.1; the suppression line appears in Log1 on Lorex ch7 |
| ONVIF SOAP calls ignore the throttle | CLAUDE.md | Fixed in 2.6.1 |
| Bundle the Raspberry Pi build of ffmpeg | CLAUDE.md | Pinned by origin since 2.6.0-rc2.0; `8:5.1.9-0+deb12u1+rpt1` confirmed on the Pi 2026-09-28 |
| RTSP OPTIONS fingerprint helper | CLAUDE.md, `MoreToDo.txt` | `_rtsp_options_fingerprint` exists and feeds `host_meta` |
| Layered Stream Discovery ("View Locked Streams") | `MoreToDo.txt`, changelog | Deep Re-Probe, `additional_streams`, `openLockedStreams` |
| Lorex multi-channel cards | Changelog 2.5.0-rc1.0 | Fixed in 2.5.0-rc1.7: eight distinct cards |
| Hikvision EOF after about 30 frames | `MoreToDo.txt` | Marked done there |
| Dead JavaScript functions `_estimateCpuPct`, `_focusWarnOK` | `MoreToDo.txt`, changelog | No longer in the source |
| "Waiting for camera (throttled)" indicator | Throttle Q&A transcript | Cards show "Authenticating (Camera rate-limited, ~30 seconds)" with a warning dot |
| Microseven Enhanced View re-entry bug | Changelog 2.4.0-rc3.1 | Fixed as Bug A in 2.4.0-rc3.3 |
| Graceful shutdown, bounded thread pool, backoff jitter, duplicate-definition check | 2.0.7 transcript | All present |
| Thumbnails from ONVIF snapshot URLs and brand snapshot paths | Feed-fix transcript (Techniques 4 and 9) | `onvif_get_snapshot_uri` in use; snapshot paths in `STREAM_DB` |
| Throttle-Aware Probe Pacing | Plan document | Superseded by 2.3.0 |
| Live view without decoding on the Pi (Tier 2) | 2026-09-26 research | 2.6.3, behind the Live View option |

## Not AnyCam work

- Hikvision auto-tracking lost after a firmware update (`docs/legacy/HikvisionInfo.docx`). A camera firmware question, not software in this repository.
