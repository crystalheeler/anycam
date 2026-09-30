# AnyCam Build Plan

**Compiled:** 28 September 2026; updated 29 September 2026. Current release: 2.6.5, committed and tagged locally, not pushed. 2.6.4 is the latest published release.
**Purpose:** the open task list that CLAUDE.md rule 7 says to consult before every build. Update it as items close.

**Sources swept:** CLAUDE.md; session memory; this project's chat history; `docs/legacy/MoreToDo.txt`; the four transcripts in `docs/legacy/*.docx`; the plan documents in `docs/`; every deferred, known-issue and out-of-scope note in `CHANGELOG.md`; the 2.6.x audit reports; field logs; and the source itself. Each item was checked against the current code. Items that turned out to be done are listed at the end, so they can come off the older lists.

No version numbers are assigned here. Rule 7: version and scope are confirmed with CrystalHeeler at build time.

A readable, colour-coded version, `docs/BUILD_PLAN.html`, is generated from this file by `docs/render_build_plan.py`. Regenerate it after every change to this file.

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

1. **Motion detection that misses real motion on the Lorex channels** (B16, C13): pixel comparison. Then finish the 2.6.5 field test (A12).
2. **Missing cameras** (B2). Logs first.
3. **The Microseven lockout** (B6): power-cycle it, then logs if it locks up again.
4. **The small correctness bugs**: B3, B4, B5, B12, and C10's plain message for Firefox, which matters more now that live view cannot be switched off.
5. **The automatic-behaviour discussion** (C11, C3), then **live cards** (C1) and **recording length** (C12).
6. **Tests into the repo** (E7), then **the module split** (E1). The tests are the safety net the split needs.

Hardware decode (B11) is parked for later, at CrystalHeeler's request.

---

## A. Field tests

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| A12 | You | Finish the 2.6.5 field test | CrystalHeeler, 2026-09-30, on the test system B: landscape, menus removed, loading message, the Oak-D camera live, card loading text, Record button, zip folder and changelog, and recordings that stop all confirmed good | Still open: the classic view hiding the card thumbnail until the full stream arrives; "Stream unavailable" after 90 s (not yet seen); install 2.6.5 on the Hikvision (172) system. The ffmpeg version line is confirmed in the 2026-09-30 log | 

## B. Bugs

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| B2 | Logs | **One or two cameras are not discovered** | CrystalHeeler, 2026-09-28 | Logs first: the scan log, plus the IP and brand of each missing camera |
| B3 | Ready | Skip Non-Reference Frames breaks every camera it touches | ffmpeg rejects `nonref`; its valid value is `noref`. Confirmed in Log1: every launch of the H.264 camera at 192.168.50.73:8765 fails. The "~40% less CPU" in its description never happened, because the setting never applied | Fix the value and field-test, or remove the option (see C11). Until then, turning it off restores that camera |
| B4 | Ready | Camera names are written into the page unescaped | Found in 2.6.3: the classic Enhanced View info bar. Names are editable, so a crafted name could inject HTML | Escape it, then check every place the page inserts camera data |
| B5 | Ready | "Share with community" box is ticked and does nothing | Found in 2.6.3: a placeholder in the page is never filled in | Fill the placeholder when the page is built |
| B12 | Ready | VAAPI reported as available on a Pi 4 | Log1 startup. The Pi has no VAAPI device; the check only confirms ffmpeg was built with VAAPI | Check for a real device before reporting it available |
| B6 | You | **The Microseven is stuck in its rate-limit lockout** | CrystalHeeler, 2026-09-29: the Microseven at 10.0.0.22 is recognized and authenticates, but serves MJPEG only, with no RTSP stream. That matches the lockout CLAUDE.md describes, which only a power cycle reliably clears. It may be the same fault as the "Invalid data found" regression open since 2.4.0-rc3.3. It also blocks the last Tier 1 check: that the 2.6.1 ONVIF fix stops AnyCam opening connections inside the Microseven's 5 s cooldown | Power-cycle the camera. If RTSP comes back and stays, check the log for connection resets while re-entering its credentials. If it locks up again, something is still hitting it too often: send the log from the test system A (rule 2) |
| B16 | Discuss | **Motion detection misses real motion on the Lorex channels** | CrystalHeeler, 2026-09-30, on 2.6.5: the Oak-D camera, ch4 and ch7 armed; the Oak-D camera recorded all day (23 clips), ch4 and ch7 did not after 08:24. Log (07:34 to 14:21): both Lorex loops ran all day, one snapshot every 1.9 s, no stops or restarts, so "stops when the app closes" is ruled out. Lorex snapshots are 9,000 to 10,500 bytes; after 08:26 they drift under 1% per 10 min. Sensitivity was 50 all day (log, 07:34 start), which triggers on a file-size change over 5.1%. A person in the picture changes a 10 KB JPEG by less than that, so the file-size method cannot see them. The 1 to 100 scale maps to a 10% to 0.1% size change, so "50" is less sensitive than it sounds. ch4's clips from 07:45 to 08:24 came while the sizes still moved with the morning light. The the Oak-D camera frames are 28 to 35 KB, and the Oak-D add-on draws boxes and labels on detected people, which moves the size a lot. Also seen: "recording stopped" logged 2 or 3 times per clip; the keeper and the frame path both stop the same recording | Pixel comparison (C13) is the fix for both. It needs an image library (Pillow) added to the image. CrystalHeeler to choose the version. Optional test first: raise Motion Sensitivity to 80 (a 2.1% threshold) to confirm. It applies to every camera, so the Oak-D camera and light changes will trigger more |
| B15 | Discuss | Opening the classic view stops the card's working stream | test system B log, 12:19:35 on 2026-09-29: entering the classic view cancelled a card stream at frame 38,450 and opened a new full-resolution connection, which timed out after 30 s with no frames. Proposed for 2.6.5 as fix 1c and held back: keeping the old stream until the new one delivers needs two connections to one camera at once, through per-camera state that assumes one. Rate-limited cameras (Microseven) cannot take two connections | Discuss. Live view now covers this camera in Chrome, so the classic view is the fallback path only |
| B8 | Ready | Scan progress bar jumps and is inaccurate | `MoreToDo.txt`; changelog 2.2.9 and 2.3.0 | Base progress on work completed, not time elapsed |
| B9 | Later | "Unknown child process pid" warning | Cosmetic (changelog 2.2.9) | Low priority |
| B10 | Later | "Cannot connect to host 172.30.32.1:8099" for about 3 s at start | 2.6.2 Supervisor log: Home Assistant connects before the web server is listening | Cosmetic. Start the web server before the slow startup steps |
| B11 | Later | **Hardware decode silently falls back to software** | Log1: ffmpeg cannot open the Pi's decoder devices (`/dev/media0` to `3`, "Operation not permitted"), then decodes in software, while AnyCam logs "hw first frame (hevc_drm)". Classic 4K runs at 8 fps; 2.6.0-rc2.5 measured 22 fps. The gray frames CrystalHeeler sees when the classic view bogs down fit this: ffmpeg's HEVC decoder fills any missing frame with mid-gray, and Log1 shows ch7 repeating one identical frame 400 times. Live view does no decoding on the Pi, which is why Chrome was clean | Parked by CrystalHeeler. When picked up: logs first, then find what changed since rc2.5 (HAOS or Supervisor update, ffmpeg 5.1.8 to 5.1.9, device permissions). Also make the log stop reporting hardware decode when it is not happening |

## C. Video and recording

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| C11 | Discuss | **Replace most or all of the option toggles with automatic behaviour** | CrystalHeeler, 2026-09-28. Eight toggles since 2.6.4 removed Live View, several broken or no longer relevant: Skip Non-Reference Frames never worked (B3), Fast Stream Start does nothing at 4K, and Live View makes several decode options irrelevant for the camera being watched. Low Latency Probe is the counter-example: it measurably helps the slow-starting H.264 camera (B13), so CrystalHeeler is keeping it for now. It is a candidate to switch on automatically for streams that start slowly | Separate discussion. For each toggle: remove it, make it automatic, or keep it. Pairs with C3 |
| C3 | Discuss | Automatic quality in live view | CLAUDE.md: the Enhanced View step-down redesign "requires full discussion before any coding." 2.6.5 removed the Resolution, Frame Rate and Auto controls at CrystalHeeler's request. Correction to the 2026-09-26 research: WebRTC congestion control cannot adjust a stream that go2rtc passes through unchanged | In live view, automatic quality means switching between the camera's own streams (main to sub) when playback stalls. Discuss before coding |
| C1 | Ready | Live video in the camera cards | CrystalHeeler approved 2026-09-28. The blocker, C2, closed in 2.6.5: an armed camera's motion detection no longer depends on the page polling its card | Use each camera's sub-stream: many live 4K H.265 streams would overload the viewing device. The Hikvision sub-stream is MJPEG, so that card stays on snapshots |
| C12 | Ready | Configurable recording length: 30 s, 1 min, 2 min, 5 min | CrystalHeeler, 2026-09-28 | New option. ffmpeg's segment muxer splits a recording into files with no re-encoding. It cuts at the first keyframe after each interval, so lengths are approximate |
| C13 | Later | **Light changes trigger motion recordings** | CrystalHeeler, 2026-09-30: a clip at 06:21 on Lorex ch7 with no motion in it. Log: snapshot size grew from 7,028 to 8,773 bytes between 06:05 and 06:20 as it got light, then stepped to about 10,000 bytes at 06:21, most likely the camera's night-to-day switch. Detection compares JPEG file sizes; at sensitivity 15, a change over 8.6% counts. No other false clip from 00:03 to 06:21 | CrystalHeeler, 2026-09-30: leave it for now and collect more clips; he leans toward pixel comparison later (compare the pictures, not file sizes: more accurate, more CPU on the Pi). Plan it as its own release |
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
| E8 | Ready | Remove the manual-tier server endpoints | 2.6.5 removed the page controls. `/snap/focus/tier` and `/snap/focus/profiles` have no caller now, and the snap loop still carries `tier_change_kill` handling for them | Remove the routes, the handlers and the flag together; the classic view's automatic ladder must keep working. Do it with C3 or C11 |
| E5 | Later | Complete type hints | 2.0.7 list | Do it alongside E1 |

## F. Release engineering

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| F6 | You | CLAUDE.md is out of date | Its queued-work and open-issue lists include items that have shipped (see Done). It describes a Part 6 of the best-practices document that does not exist | CrystalHeeler to say whether Part 6 was lost or never written; then refresh both lists |
| F7 | Ready | A release check that run.sh and config.yaml agree | 2.6.4 audit. run.sh passes each setting to the program; for a setting missing from config.yaml, bashio returns the text null, which the code reads as off. Removing the Live View option nearly shipped exactly that. Checked by hand in 2.6.4 | Fail the release gate when run.sh reads a setting config.yaml does not define, or config.yaml defines one run.sh never reads |
| F9 | Ready | The packager lives outside the repo | `/tmp/mkzip.py` in Git Bash builds every release zip, including the rule 3 folder change in 2.6.5. A cleared temp folder loses it | Move it into the repo, for example `tools/mkzip.py`, and have the release notes name it |
| F1 | Ready | A release check for the Dockerfile | 2.6.1 passed every check and still could not build | Check each pinned package version against its archive at release time |
| F2 | Ready | The blocking-I/O check misses some calls | Found by hand in 2.6.3 | Extend it beyond `open()` to `read_bytes`, `read_text` and the write equivalents |
| F3 | Ready | `build.yaml` is deprecated | The Supervisor warns on every build | Move the build settings into the Dockerfile |
| F4 | Ready | Docker warning about the base-image argument | The argument has no default value | Give it a default |
| F5 | Ready | 20 of 25 older audit "PDFs" are ZIP bundles | Found 2026-09-27; their content is intact | Convert them to real PDFs |

---

## Done: take these off the older lists

| Item | Where it was listed | Evidence |
|---|---|---|
| 2.6.5 Enhanced View: landscape full screen, menus removed, loading message | CrystalHeeler, 2026-09-29 | CrystalHeeler, 2026-09-30: all good on the HA app |
| The the Oak-D camera camera slow to start | This plan, B13 | CrystalHeeler, 2026-09-30: good, with AnyCam 2.6.5 and Oak-D 2.4.2 (one keyframe per second) |
| Cards say "Loading feed, please wait…" | This plan, B14 | CrystalHeeler, 2026-09-30: good. "Stream unavailable" after 90 s not yet seen |
| Record button shows the server's state | 2.6.5 field test | CrystalHeeler, 2026-09-30: good |
| Recordings stop | This plan, B1 | Log 2026-09-30: a 9 s clip at 06:21; CrystalHeeler: good |
| Changelog shows in HA; fixed zip folder | This plan, F8 | CrystalHeeler, 2026-09-30: good |
| Live View option removed from the Configuration tab | 2.6.4 | CrystalHeeler, 2026-09-30: good |
| Motion detection on the Lorex channels; recordings that never stop | This plan, B1; CrystalHeeler, 2026-09-29 ("no longer working" on 2.6.4) | 2.6.5. Cause: motion ran only on the ffmpeg path, which the Lorex channels used only in the classic Enhanced View; 2.6.4's live view removed that. Now both paths, no viewer needed, a 10 s keeper ends recordings, armed state saved. Field check in A12 |
| Motion detection that does not depend on snapshot polling | This plan, C2 | 2.6.5: an armed camera's loop no longer idles out, and the keeper restarts it |
| "No changelog found" after an update | This plan, F8 | 2.6.5, CrystalHeeler's order: the zip folder is always `local_camera_discovery`; CLAUDE.md rule 3 changed. One `touch` needed on the first update |
| Cards said "Stream unavailable" while starting | This plan, B14 | 2.6.5: cards say "Loading feed, please wait…" and switch to "Stream unavailable" only after 90 s of errors |
| Resolution dropdown flickers | This plan, B7 | 2.6.5 removed the dropdown |
| Enhanced View menus, landscape, loading message | CrystalHeeler, 2026-09-29 | 2.6.5, in code and tests. Field check in A12 |
| Live View on by default | This plan, A3 | 2.6.4, 2026-09-29: the option is removed and live view is always on; each camera still falls back to the classic view on its own |
| Live view on the Hikvision system | This plan, A9 | CrystalHeeler, 2026-09-29: 2.6.3 installed on the test system A; the Hikvision plays live and looks good |
| Was the browser the problem all along? No | This plan, A10 | CrystalHeeler, 2026-09-29: in Chrome, Classic bogs down and is not smooth while Live is smooth, on the same camera. The Pi's decode-and-JPEG pipeline was the cause, as predicted |
| Live view stays up through Home Assistant | This plan, A2 | CrystalHeeler, 2026-09-29: a live feed stayed up for 10+ minutes |
| Tens-of-seconds freezes | This plan, A6; the original complaint | CrystalHeeler, 2026-09-29: no freezes in live view |
| Low Latency Probe: keep it | This plan, A5 | CrystalHeeler, 2026-09-29: with it on, the H.264 camera's classic view reached 1280x720 after 10 to 15 s; with it off, it never did. No other camera changed |
| Tier 1 checks | This plan, A4 | Fast Stream Start suppression confirmed in Log1; Low Latency Probe tested (A5); the Microseven check moved to B6 because the camera is locked out |
| H.264 camera tested | This plan, A11 | CrystalHeeler, 2026-09-29: its card now shows video after 20 to 30 s; the test surfaced B13 |
| Hikvision I-frame interval | This plan, A7 | 60 at 20 fps, a keyframe every 3 s. Suggested 40, optional. The main stream is 2560x1440 H.265, not 4K, so Fast Stream Start's 4K gate does not apply to it |
| Live view plays H.265 in Chrome | This plan, A1 | CrystalHeeler, 2026-09-28: Lorex DVR channels played "perfectly" in Chrome. Firefox and LibreWolf fall back to classic, as go2rtc's compatibility table predicts |
| Motion detection records | This plan, B1 (reported as not recording) | CrystalHeeler, 2026-09-28: it works. Found 2026-09-29: on the Lorex channels only while open in the classic Enhanced View (Log1); fixed for all paths in 2.6.5 |
| Push and publish 2.6.3 | This plan, A8 | Pushed and published on GitHub on 2026-09-29, on CrystalHeeler's order |
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
