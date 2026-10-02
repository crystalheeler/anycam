# AnyCam Build Plan

**Compiled:** 28 September 2026; updated 29 September 2026. Current release: 2.6.7, published 2026-10-02. Next: 2.6.8 (C16 only), then 3.0.0-rc1.0 (section E).
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

1. **Field-test 2.6.7** (A13): night boost, the Home Assistant permission, the sunset check, insects, infrared switches, file names.
2. **Missing cameras** (B2). Logs first.
3. **The Microseven lockout** (B6): power-cycle it, then logs if it locks up again.
4. **The small correctness bugs**: B3, B12, and C10's plain message for Firefox, which matters more now that live view cannot be switched off.
5. **The automatic-behaviour discussion** (C11, C3).
6. **3.0.0-rc1.0** (CrystalHeeler, 2026-10-02): E7, then E6, E8 and E4, then the module split (E1) in three stages.

Hardware decode (B11) is parked for later, at CrystalHeeler's request.

---

## A. Field tests

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| A13 | You | **Field-test 2.6.7 night boost** | Built 2026-10-01 for C15, then insects and infrared switches after CrystalHeeler's overnight test | Overnight: count insect recordings (11 of 22 on ch4 with 2.6.6) and look for "changed in one picture only" lines; dawn and dusk: no recording from the switch; names start LorexCH4_. Accept the Home Assistant API permission when updating. After dark: the log line "night (IR, black-and-white)" per camera, the cog's "Night mode (IR): sensitivity +15", and the colour value in the tuning line (thresholds 2.5 and 5.0 are untested on real cameras). One hour after sunset: a notification only for a camera that did not switch |
| A12 | You | Finish the 2.6.6 field test | CrystalHeeler, 2026-10-01: everything fixed and working — live detection, 3 s pre-roll, file naming, card layout and behaviour. Sensitivity 90 on two Lorex channels gave better results with the pre-roll, "still not ideal" | CrystalHeeler testing 95 and 98, then report. Carried: the classic view hiding the card thumbnail; "Stream unavailable" after 90 s; 2.6.5 or later on the Hikvision (172) system |

## B. Bugs

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| B2 | Logs | **One or two cameras are not discovered** | CrystalHeeler, 2026-09-28 | Logs first: the scan log, plus the IP and brand of each missing camera |
| B3 | Ready | Skip Non-Reference Frames breaks every camera it touches | ffmpeg rejects `nonref`; its valid value is `noref`. Confirmed in Log1: every launch of the H.264 camera at 192.168.50.73:8765 fails. The "~40% less CPU" in its description never happened, because the setting never applied | Fix the value and field-test, or remove the option (see C11). Until then, turning it off restores that camera |
| B12 | Ready | VAAPI reported as available on a Pi 4 | Log1 startup. The Pi has no VAAPI device; the check only confirms ffmpeg was built with VAAPI | Check for a real device before reporting it available |
| B6 | You | **The Microseven is stuck in its rate-limit lockout** | CrystalHeeler, 2026-09-29: the Microseven at 10.0.0.22 is recognized and authenticates, but serves MJPEG only, with no RTSP stream. That matches the lockout CLAUDE.md describes, which only a power cycle reliably clears. It may be the same fault as the "Invalid data found" regression open since 2.4.0-rc3.3. It also blocks the last Tier 1 check: that the 2.6.1 ONVIF fix stops AnyCam opening connections inside the Microseven's 5 s cooldown | Power-cycle the camera. If RTSP comes back and stays, check the log for connection resets while re-entering its credentials. If it locks up again, something is still hitting it too often: send the log from the test system A (rule 2) |
| B15 | Discuss | Opening the classic view stops the card's working stream | test system B log, 12:19:35 on 2026-09-29: entering the classic view cancelled a card stream at frame 38,450 and opened a new full-resolution connection, which timed out after 30 s with no frames. Proposed for 2.6.5 as fix 1c and held back: keeping the old stream until the new one delivers needs two connections to one camera at once, through per-camera state that assumes one. Rate-limited cameras (Microseven) cannot take two connections | Discuss. Live view now covers this camera in Chrome, so the classic view is the fallback path only |
| B8 | Ready | Scan progress bar jumps and is inaccurate | `MoreToDo.txt`; changelog 2.2.9 and 2.3.0 | Base progress on work completed, not time elapsed |
| B11 | Later | **Hardware decode silently falls back to software** | Log1: ffmpeg cannot open the Pi's decoder devices (`/dev/media0` to `3`, "Operation not permitted"), then decodes in software, while AnyCam logs "hw first frame (hevc_drm)". Classic 4K runs at 8 fps; 2.6.0-rc2.5 measured 22 fps. The gray frames CrystalHeeler sees when the classic view bogs down fit this: ffmpeg's HEVC decoder fills any missing frame with mid-gray, and Log1 shows ch7 repeating one identical frame 400 times. Live view does no decoding on the Pi, which is why Chrome was clean | Parked by CrystalHeeler. When picked up: logs first, then find what changed since rc2.5 (HAOS or Supervisor update, ffmpeg 5.1.8 to 5.1.9, device permissions). Also make the log stop reporting hardware decode when it is not happening |

## C. Video and recording

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| C11 | Discuss | **Replace most or all of the option toggles with automatic behaviour** | CrystalHeeler, 2026-09-28. Eight toggles since 2.6.4 removed Live View, several broken or no longer relevant: Skip Non-Reference Frames never worked (B3), Fast Stream Start does nothing at 4K, and Live View makes several decode options irrelevant for the camera being watched. Low Latency Probe is the counter-example: it measurably helps the slow-starting H.264 camera (B13), so CrystalHeeler is keeping it for now. It is a candidate to switch on automatically for streams that start slowly | Separate discussion. For each toggle: remove it, make it automatic, or keep it. Pairs with C3 |
| C14 | Ready | **Upload recordings to SFTP or FTP** | CrystalHeeler, 2026-09-30: per-camera recording destination, including SCP, SFTP, FTP or another remote share. Samba and NFS already work in 2.6.6 through Home Assistant's network storage under /media. SCP is SFTP in current OpenSSH | Version to confirm (CrystalHeeler, 2026-10-02: 2.6.8 is C16 only; was 2.6.8). Upload each finished file, then delete the local copy; keep it and retry until the upload succeeds. SFTP needs asyncssh; FTP and FTPS are in Python's standard library. Passwords stored encrypted, like camera passwords |
| C16 | Ready | **Sunrise check: catch a wrong home location** | CrystalHeeler, 2026-10-02: Home Assistant's location was the Amsterdam default with a time zone 7 hours away, so AnyCam reported "still in night mode an hour after sunrise (00:42)". The time zone alone cannot give sunrise: across the Central zone it differs by 1 h 42 min in December | 2.6.8 (CrystalHeeler, 2026-10-02). When the location's longitude is more than 52.5 degrees (3.5 h) from the time zone's expected longitude (15 degrees per hour from UTC): no sunrise or sunset check, one log warning, and a cog note to set the home location. Re-read the location every 6 hours (now 12) |
| C3 | Discuss | Automatic quality in live view | CLAUDE.md: the Enhanced View step-down redesign "requires full discussion before any coding." 2.6.5 removed the Resolution, Frame Rate and Auto controls at CrystalHeeler's request. Correction to the 2026-09-26 research: WebRTC congestion control cannot adjust a stream that go2rtc passes through unchanged | In live view, automatic quality means switching between the camera's own streams (main to sub) when playback stalls. Discuss before coding |
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
| E7 | Ready | **Automated tests in the repo** | 265 server checks and 110 page checks exist only in a session scratch folder (2,293 lines), which CrystalHeeler ordered deleted after calibration | 3.0.0-rc1.0, first. Move them into `tests/`, remove the paths that point to the build PC, and run them from the release gate |
| E1 | Ready | Split the 18,162-line file into modules | The 2.0.7 review called this the single change with the largest positive impact. Evaluated 2026-10-02: 208 top-level definitions, 66 module-level state objects, 15 names replaced with `global`; no effect on speed. The tests do not cover the scan, the password path or most of the snapshot loop | 3.0.0-rc1.0, after E7, E6, E8 and E4. Three stages: (1) the camera database tables and the page, about 5,800 lines; (2) motion and night boost, go2rtc, storage; (3) scan, password entry, snapshot loop, after new tests. One shared state module (this replaces E2). Finish the last 27 type hints on the way (this replaces E5) |
| E4 | Ready | Strip credentials from every log line | 2.0.7 list. Evaluated 2026-10-02: 21 log calls carry a URL; 30 places already strip credentials | 3.0.0-rc1.0. Check each of the 21 |
| E6 | Ready | Remove the old `/stream/{camera_id}` endpoint | Unused since 1.6.0. Evaluated 2026-10-02: the route and a 117-line handler exist; nothing in the page calls it; it starts one ffmpeg per request | 3.0.0-rc1.0. Delete the route and its handler |
| E8 | Ready | Remove the manual-tier server endpoints | 2.6.5 removed the page controls. `/snap/focus/tier` and `/snap/focus/profiles` have no caller, and the snap loop still carries `tier_change_kill` handling | 3.0.0-rc1.0. Remove the routes, the handlers and the flag together; the classic view's automatic ladder must keep working |

## F. Release engineering

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| F9 | Ready | The packager lives outside the repo | `/tmp/mkzip.py` in Git Bash builds every release zip, including the rule 3 folder change in 2.6.5. A cleared temp folder loses it | Move it into the repo, for example `tools/mkzip.py`, and have the release notes name it |

---

## Done: take these off the older lists

| Item | Where it was listed | Evidence |
|---|---|---|
| Recordings looked choppy | This plan, B18 | CrystalHeeler, 2026-10-01: the stutter was VLC; Media Player Classic plays the same files well. The files hold the DVR's 7 fps with steady timing (measured) |
| Insects and infrared switches no longer record | CrystalHeeler, 2026-10-01 overnight test | 2.6.7: a second changed picture within 0.5 s (or one picture of 3% or more); reference by stream position, so no echoes; 2 s hold after a light change. Replay of 45 recordings: insects 11 of 12 never record, switches 0, real events kept. Field check in A13 |
| Classic view ignored a camera's escalated cooldown | CrystalHeeler's test system A log, 2026-10-01 23:22: 6 ffmpeg starts 5 s apart at the Microseven during a 30 s cooldown | 2.6.7: every ffmpeg start waits out the cooldown; a camera in cooldown goes to HTTP snapshots after one failed start. Not changed, CrystalHeeler's decision: the scan's fingerprint and path walk within 1 s |
| Recording names start with the camera | CrystalHeeler, 2026-10-01 | 2.6.7: LorexCH4_20261001_053358.mp4 (choice A) |
| Live video in the camera cards works in Chrome | This plan, B17 | CrystalHeeler, 2026-10-01: noticed working, on 2.6.6 |
| 2.6.7 pushed and published | CrystalHeeler, 2026-10-02 | Night boost, insects, infrared switches, file names, classic-view cooldown |
| Reduce shared global state (E2) | This plan | Dropped 2026-10-02 (CrystalHeeler agreed): rewriting 66 state objects is the riskiest change in section E and gives no user benefit. The split (E1) adds one shared state module |
| One shared HTTP client session (E3) | This plan | Dropped 2026-10-02 (CrystalHeeler agreed): 5 sessions, one per snapshot loop with its own connection settings and four rare calls; no measurable gain |
| Complete type hints (E5) | This plan | Dropped as a task 2026-10-02: 241 of 242 functions have a return type, 215 are fully typed; the last 27 are done in E1 |
| Night boost: +15 sensitivity under infrared, with a sunrise and sunset check | This plan, C15; CrystalHeeler, 2026-10-01 | 2.6.7: night read from the picture's colour (30 s hold); home location from Home Assistant; missed switches reported in the log, the cog panel and a Home Assistant notification. 40 new server tests, 4 new page tests. Field check in A13 |
| 2.6.6 field test: live detection, pre-roll, file names, cards | This plan, A12 | CrystalHeeler, 2026-10-01: "Everything is fixed and working very well." Pushed and published the same day |
| Motion from the live stream, with a 3-second pre-roll | CrystalHeeler, 2026-10-01 | 2.6.6: detection decodes the smallest stream at 4 frames a second; recordings start from an in-memory copy of the main stream, at a keyframe at least 3 s before the motion. Tested end to end with stand-in ffmpeg processes; field check in A12 |
| Per-camera recording settings from a cog on each card | CrystalHeeler, 2026-09-30 | 2.6.6: sensitivity slider (1 left, 100 right, value shown) with a live reading in slider units; cooldown, tail, file length, folder; defaults 63, 5 s, 3 s, 30 s, /media/anycam; global override switch in the Configuration tab |
| Identity shows protocol, IP and port first; Test Stream removed | CrystalHeeler, 2026-09-30 | 2.6.6 |
| Tuning line for motion sensitivity | CrystalHeeler, 2026-09-30 | 2.6.6: once a minute per armed camera, with the sensitivity that would have recorded |
| Motion detection that sees people; light changes ignored | This plan, B16, C13 | 2.6.6: pixel comparison after cancelling brightness and contrast; light told apart by spread over 16 areas. Synthetic tests: brightness, contrast and night-mode changes 0%, a person block 5.0%. New Motion Sensitivity setting (default 5% of the picture). Field check in A12 |
| Recording length: 10 s to 5 min | This plan, C12; CrystalHeeler, 2026-09-28 | 2.6.6: motion_clip_length, default 1min; files motion_<date>_<time>_partNN.mp4. Field check in A12 |
| Live video in the camera cards | This plan, C1 | 2.6.6: the smallest stream per camera; the Lorex DVR's subtype=1 sub-stream; wider than 1920 stays on snapshots. Field check in A12 |
| Camera values escaped in the page | This plan, B4 | 2.6.6: 13 click handlers encoded with jsArg(); the info bar and IP badge escaped |
| "Share with community" ticked and doing nothing | This plan, B5 | 2.6.6: the placeholder is filled at build time; a gate check now catches unfilled placeholders |
| "Unknown child process pid" warning | This plan, B9 | 2.6.6: an exiting ffmpeg is no longer signalled (Python's kill() collected it first) |
| "Cannot connect to host 172.30.32.1:8099" at start | This plan, B10 | 2.6.6: the web server starts before the hardware probe |
| Release checks: Dockerfile inputs, blocking I/O, settings agreement | This plan, F1, F2, F7 | 2.6.6: gates 6 and 7 added and gate 3 extended; each broken on purpose to confirm it fails. They found the OUI database written on the event loop and a missing option description, both fixed |
| build.yaml deprecated; base-image default | This plan, F3, F4 | 2.6.6: build.yaml removed; the Dockerfile defaults to the multi-arch ghcr.io/home-assistant/base-debian:bookworm |
| Old audit reports that were ZIP files | This plan, F5 | 2.6.6: 20 converted to real, searchable PDFs |
| CLAUDE.md out of date | This plan, F6 | 2026-09-30: best-practices document restored; CLAUDE.md lists point to this plan |
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
