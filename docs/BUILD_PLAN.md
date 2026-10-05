# AnyCam Build Plan

**Compiled:** 28 September 2026; updated 4 October 2026. Current release: 3.0.0, published 2026-10-02 (the code of 3.0.0-rc1.5). Built, not field-tested, not pushed: 3.0.1-rc1.0, 3.1.0-rc1.0, 3.2.0-rc1.0, 3.3.0-rc1.0, 3.4.0-rc1.0, 3.5.0-rc1.0, 3.6.0-rc1.0, 3.7.0-rc1.0. The release candidates 3.0.0-rc1.0 to 3.0.0-rc1.5 are pushed, not published, all field-tested. The history was rewritten on 2026-10-02 (privacy scrub); older commit hashes no longer exist.
**Purpose:** the open task list that CLAUDE.md rule 7 says to consult before every build. Update it as items close.

**Sources swept:** CLAUDE.md; session memory; this project's chat history; `docs/legacy/MoreToDo.txt`; the four transcripts in `docs/legacy/*.docx`; the plan documents in `docs/`; every deferred, known-issue and out-of-scope note in `CHANGELOG.md`; the 2.6.x audit reports; field logs; and the source itself. Each item was checked against the current code. Items that turned out to be done are listed at the end, so they can come off the older lists.

Since 2026-10-03 the open items are arranged into version phases (see Phases after 3.0.0). Rule 7 still applies: the version and scope are confirmed with CrystalHeeler before each build.

A readable, colour-coded version, `docs/BUILD_PLAN.html`, is generated from this file by `docs/render_build_plan.py`. Regenerate it after every change to this file.

---

## Next up: field tests

All phases of the untested run CrystalHeeler ordered on 2026-10-04 are built: 3.0.1-rc1.0, 3.1.0-rc1.0, 3.2.0-rc1.0, 3.3.0-rc1.0, 3.4.0-rc1.0, 3.5.0-rc1.0, 3.6.0-rc1.0 and 3.7.0-rc1.0, each its own commit, tag and zip, none pushed. Each audit report lists its field test.

1. **Field-test 3.7.0-rc1.0;** on a fault, install an earlier release candidate to narrow it down.
2. **B11:** send the 3.7.0-rc1.0 start-up log (the "HW device" lines) for the hardware decode fix.

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

## Phases after 3.0.0

Agreed with CrystalHeeler on 2026-10-03. Mode, CrystalHeeler 2026-10-04: every phase is built untested, one after another, each as its own release candidate with its own local commit, tag and zip; nothing is pushed or published without an order. CrystalHeeler tests the latest and installs an earlier one to narrow down a fault. An item marked Discuss gets its design discussion at the start of its phase. Rule 7 still applies: the version and scope are confirmed before each build.

| Version | Theme | Items |
|---|---|---|
| 3.0.1 | Fixes, then the Firefox message. Built: 3.0.1-rc1.0, 2026-10-04, not field-tested | **B2 first** (CrystalHeeler, 2026-10-03: "the first thing we work on"), **C10 second** (CrystalHeeler, 2026-10-03), then B20, B23, B24, B26, B12, B8, F10, C11 (also closes B3), C20 |
| 3.1.0 | Cards. Built: 3.1.0-rc1.0, 2026-10-04, not field-tested | B25, C19, D3 |
| 3.2.0 | Live view. Built: 3.2.0-rc1.0, 2026-10-04, not field-tested | C5, C6 |
| 3.3.0 | One connection per camera. Built: 3.3.0-rc1.0, 2026-10-04, not field-tested | C4, B15 |
| 3.4.0 | Detection zones. Built: 3.4.0-rc1.0, 2026-10-04, not field-tested | C17 |
| 3.5.0 | Discovery and DVRs. Built: 3.5.0-rc1.0, 2026-10-04, not field-tested | D1, D2 |
| 3.6.0 | Recording upload. Built: 3.6.0-rc1.0, 2026-10-04, not field-tested | C14 |
| 3.7.0 | Hardware decode. Built: 3.7.0-rc1.0 (diagnostics), 2026-10-04, not field-tested | B11, C9 |

Outside the phases: B6 waits until CrystalHeeler unlocks the Microseven.

---

## A. Field tests

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|

## B. Bugs

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| B6 | You | **Test the Microseven once it is unlocked, and make AnyCam robust to cameras that lock out** | CrystalHeeler, 2026-09-29: the Microseven at 10.0.0.22 is recognized and authenticates, but serves MJPEG only, with no RTSP stream. That matches the lockout CLAUDE.md describes, which only a power cycle reliably clears. It may be the same fault as the "Invalid data found" regression open since 2.4.0-rc3.3. It also blocks the last Tier 1 check: that the 2.6.1 ONVIF fix stops AnyCam opening connections inside the Microseven's 5 s cooldown 2026-10-03: the throttle research (`docs/anycam_rtsp_throttle_research_report.md`) found the connection-rate limit rare: the Hipcam RealServer family (Microseven, Sricam, Vstarcam, older Wansview, Tenvis) is the only documented family, 5 of the 76 entries in CAMERA_DB. The lockout that only a power cycle clears was seen on the Microseven; the research names no other camera with it. The nearest is CVE-2023-50685 in the same firmware family, a crash that recovers in about 45 s | Retitled by CrystalHeeler, 2026-10-03. When CrystalHeeler has unlocked the camera: field-test that it behaves as expected (scan, password entry with the 5 s pacing, card, Enhanced View, no new lockout over a day). Then review the code: detect the locked state (accepts the connection, returns no valid answer), stop connecting to it, and tell the user on the card to power-cycle the camera. No phase until it is unlocked |
| B11 | Logs | **Hardware decode silently falls back to software** | Log1: ffmpeg cannot open the Pi's decoder devices (`/dev/media0` to `3`, "Operation not permitted"), then decodes in software, while AnyCam logs "hw first frame (hevc_drm)". Classic 4K runs at 8 fps; 2.6.0-rc2.5 measured 22 fps. The gray frames CrystalHeeler sees when the classic view bogs down fit this: ffmpeg's HEVC decoder fills any missing frame with mid-gray, and Log1 shows ch7 repeating one identical frame 400 times. Live view does no decoding on the Pi, which is why Chrome was clean | Parked by CrystalHeeler. When picked up: logs first, then find what changed since rc2.5 (HAOS or Supervisor update, ffmpeg 5.1.8 to 5.1.9, device permissions). Also make the log stop reporting hardware decode when it is not happening 3.7.0-rc1.0 (2026-10-04) adds the diagnostics: the start-up "HW device" lines, /api/diagnostics/hw, and a warning when a picture was decoded in software. Next: CrystalHeeler's 3.7.0-rc1.0 start-up log, then the fix |

## C. Video and recording

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| C21 | Later | **Remove the classic engine** | From C20 and C19. 3.0.1-rc1.0 removed the Classic button; 3.1.0-rc1.0 made MJPEG cameras and wide streams play live in cards. The classic engine (the native-resolution snapshot loop, the adaptive ladder, the Adaptive Quality toggle) is still the fallback for a camera or browser that cannot play live | Remove it only when no camera or browser needs the fallback any more, after C10, C19 and C4 are field-tested |

## D. Discovery and device support

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|

## E. Code health

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|

## F. Release engineering

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|

---

## Done: take these off the older lists

| Item | Where it was listed | Evidence |
|---|---|---|
| Pi 4 decoder overlay check (C9) | This plan, C9 | 3.7.0-rc1.0: the start-up device report says when no rpivid decoder is visible and names the line `dtoverlay=rpivid-v4l2` for /boot/firmware/config.txt. A toggle is not possible from inside the add-on (it cannot write the boot partition). Tests AH3, AH4. Not field-tested |
| Upload recordings by SFTP, FTPS or FTP (C14) | This plan, C14 | 3.6.0-rc1.0, CrystalHeeler's defaults of 2026-10-04: global plus per camera (global, own, off); upload when a recording stops, then delete the local copy (a box keeps it); retry 1 min doubling to 30 min, queue kept in /data; passwords encrypted, never sent to the page; SFTP server key saved at the first upload. New file `anycam_upload.py`; asyncssh 2.23.1 (the newest that accepts cryptography 48.0.0). Tests AG1 to AG10. Not field-tested |
| DVR channel limit from the DVR (D1) | This plan, D1 | 3.5.0-rc1.0: after the password, `devVideoInput.cgi?action=getCollect` and `MaxRemoteInputChannels` (Basic, then Digest); the larger count, 1 to 256, sets the channels walked; 16 when the DVR does not answer. Tests AF1 to AF3. Open from D1: a sample of what a known-empty channel sends. Not field-tested |
| One camera database; default ports used (D2) | This plan, D2; CLAUDE.md | 3.5.0-rc1.0, plan `docs/Camera_DB_Merge_Plan.md`: stream paths on the brands (`"streams"`, with `rank`), `STREAM_DB` built from them and byte for byte the old table; the scan's port list built from the database (same 54 ports). Tests AF4 to AF6 |
| Detection zones (C17) | This plan, C17 | 3.4.0-rc1.0, to the 23 answers in `docs/Detection_Zones_Plan.md`: drawing window over Enhanced View (Zones button, Edit zones in the cog panel), up to 6 polygons, own sensitivity with Off, zones only, 128 x 96 grid with zones, per-zone confirmation, slow comparison over 5 s, tuning line and cog reading per zone, Show zones with the recording's zone, zone in the log and the Storage tab, zones in motion.json. New file `anycam_zones.py`. Tests AE1 to AE11, page section C17. Open: answer 8's re-check of the whole-picture levels at 128 x 96 (needs field data). Not field-tested |
| One camera connection through go2rtc (C4) | This plan, C4 | 3.3.0-rc1.0: go2rtc's RTSP server on 127.0.0.1:28554 with a password new at each start (CrystalHeeler's approval, 2026-10-04); the snapshot loop, the classic view, the motion detector, the recording buffer and the direct recording read go2rtc's copy; one go2rtc stream per source, named from the address without its password, shared with the live card and Enhanced View; back to direct after 3 failed runs. The recording buffer stays, for the 3 s pre-event video. Tests AD1 to AD6; the release gate pins the RTSP listen address and password. Not field-tested |
| The classic view kept the card's camera stream (B15) | This plan, B15 | 3.3.0-rc1.0: the classic view's ffmpeg reads go2rtc's copy, so it opens no second camera connection, and go2rtc keeps the camera stream while the live card or motion detection use it. Test AD5. Not field-tested |
| Sound in live view (C5) | This plan, C5 | 3.2.0-rc1.0: Enhanced View asks go2rtc for video and audio; VideoRTC offers only the audio codecs the browser plays; muted at the start, a Sound button turns it on, grey when there is no playable sound. Cards stay video only. Page section C5, test AC6. Not field-tested |
| WebRTC and RTSP-over-WebSocket cameras play live (C6) | This plan, C6 | 3.2.0-rc1.0: go2rtc's WHEP source (`webrtc:http://...`) and RTSP with `#transport=ws://...`, both in go2rtc 1.9.14 with the modules already loaded; card and Enhanced View; no still-picture fallback. Tests AC1 to AC5. No test system has such a camera |
| Saved streams read again when out of date (B25) | This plan, B25 | 3.1.0-rc1.0: when a card cannot use the saved streams, or after 5 failed stream starts, AnyCam runs the password step again with the saved password, at most once every 6 hours for each camera; not for DVR channel cards. Tests AB3, AB7. Not field-tested |
| Fewer snapshot cards (C19) | This plan, C19 | 3.1.0-rc1.0: an HTTP MJPEG camera plays its stream live in the card through `anycam_mjpeg.py` (one camera connection, JPEGs over a WebSocket, no decoding); a stream wider than 1,920 plays live on a computer, still pictures on a phone. Tests AB1 to AB6, page section C19. RTSP MJPEG still shows pictures. Not field-tested |
| Drag to reorder cards (D3) | This plan, D3 | 3.1.0-rc1.0: a drag handle on each card; the order is saved by the add-on (`/api/card_order`, runtime.json) and `/api/cameras` returns the cameras in it. Tests AB8, AB9, page section D3. Not field-tested |
| Cameras in appliances get an information card (B2) | This plan, B2 | 3.0.1-rc1.0: the iENSO block and Dreame recognised by MAC; each dropped live host logged; information card, or a note on a login card. Tests AA1, AA2. Not field-tested |
| H.265 live view and the browser (C10) | This plan, C10 | 3.0.1-rc1.0: the page checks H.265 support; plays the H.264 stream, or a plain message; readable errors; cards ask for a stream without H.265. Tests AA10 and page section C10. Not field-tested |
| Late pictures, log flood, insect echo on the fallback (B20) | This plan, B20 | 3.0.1-rc1.0: keyframes only for 4K H.265; no picture older than 10 s; retry 10 s to 5 min with one warning; a second look on the snapshot path. Tests AA5 to AA7, Y2. Not field-tested |
| Manual add with WebRTC (B23); stream-table pacing (B24) | This plan, B23, B24 | 3.0.1-rc1.0. Tests Z9, Z4 |
| The log link for a store install (B26) | This plan, B26 | 3.0.1-rc1.0: `/api/self` from the Supervisor's `/addons/self/info`. Test AA8 |
| VAAPI on a Pi 4 (B12) | This plan, B12 | 3.0.1-rc1.0: a 0.1 s ffmpeg test before VAAPI is used. Test AA4 |
| Scan progress from work done (B8) | This plan, B8 | 3.0.1-rc1.0: per host, port and ONVIF device, weighted by the last scan's stage times; elapsed time and time left. Test AA3 |
| Show in Sidebar and Auto update on (F10) | This plan, F10 | 3.0.1-rc1.0: set once at the first start. Test AA9 |
| Five settings removed (C11); Skip Non-Reference Frames (B3) | This plan, C11, B3 | 3.0.1-rc1.0: Low FPS, Skip Non-Reference Frames, Limit Threads, Stagger Poll, Fast Stream Start removed with their code. Hardware Decode, Adaptive Quality and Low Latency Probe stay. Test Y9 |
| The Classic button in Enhanced View (C20) | This plan, C20 | 3.0.1-rc1.0: button and `focusUseClassic` removed; the automatic fallback stays. Test G4 and the page check. The classic engine's removal is noted in C19 |
| Recordings looked choppy | This plan, B18 | CrystalHeeler, 2026-10-01: the stutter was VLC; Media Player Classic plays the same files well. The files hold the DVR's 7 fps with steady timing (measured) |
| Insects and infrared switches no longer record | CrystalHeeler, 2026-10-01 overnight test | 2.6.7: a second changed picture within 0.5 s (or one picture of 3% or more); reference by stream position, so no echoes; 2 s hold after a light change. Replay of 45 recordings: insects 11 of 12 never record, switches 0, real events kept. Field check in A13 |
| Classic view ignored a camera's escalated cooldown | CrystalHeeler's test system A log, 2026-10-01 23:22: 6 ffmpeg starts 5 s apart at the Microseven during a 30 s cooldown | 2.6.7: every ffmpeg start waits out the cooldown; a camera in cooldown goes to HTTP snapshots after one failed start. Not changed, CrystalHeeler's decision: the scan's fingerprint and path walk within 1 s |
| Recording names start with the camera | CrystalHeeler, 2026-10-01 | 2.6.7: LorexCH4_20261001_053358.mp4 (choice A) |
| Live video in the camera cards works in Chrome | This plan, B17 | CrystalHeeler, 2026-10-01: noticed working, on 2.6.6 |
| The scan's Cancel button stops the scan (B22) | This plan, B22 | 3.0.0-rc1.5: one flag, `anycam_scan.SCAN_CANCELLED`; the scan checks it per stage, per host, per port and per ONVIF device, and ends with "Scan cancelled". Tests U14 and U15 |
| Tests for the rest of the add-on | This plan, E1 | 3.0.0-rc1.5: 122 checks, sections V to Z: brand identification and the OUI database, the page builder, the Enhanced View engine, the snapshot loop, password entry. Written and passing before each move. They found B23 and B24 |
| Module split: password entry, snapshot loop, Enhanced View engine, brand identification, page builder (E1) | This plan | 3.0.0-rc1.5: `anycam_credentials.py`, `anycam_snap.py`, `anycam_focus.py`, `anycam_brand.py`, `anycam_page.py`. All 363 definitions compared with the code before each move: none missing, none changed; page and routes identical. No import cycles |
| The last type hints | This plan, E5 | 3.0.0-rc1.5: 30 functions; every function in every file is now fully typed. The code is unchanged apart from the annotations (compared with the annotations removed) |
| The packager lives in the repository | This plan, F9 | `package_release.py` has been in the repository since 3.0.0-rc1.0; it built every release since, including 3.0.0 |
| Classic view JPEG quality (C7) | This plan | Dropped 2026-10-03 (CrystalHeeler): no longer needed; live view has been the default since 2.6.4, and the classic view is the fallback only |
| ZeroTier tuning (C8) | This plan | Dropped 2026-10-03 (CrystalHeeler): not needed; the item came from a misreading |
| Automatic quality in live view (C3) | This plan | Dropped 2026-10-04 (CrystalHeeler): in live view the Pi decodes nothing and the viewing devices play full resolution; the only remaining use was slow remote links, against the rule quality over bandwidth. The classic adaptive ladder and the Adaptive Quality toggle go with the classic engine (C20) |
| 3.0.0 published | CrystalHeeler, 2026-10-02 | The code of 3.0.0-rc1.5 with the version changed; GitHub Release 3.0.0 |
| 3.0.0-rc1.5 field test | This plan, A19 | CrystalHeeler, 2026-10-02: "rc1.5 testing is complete. it works", and Cancel is good. Logs from both systems: 0 "not defined", 0 "has not run yet", 0 tracebacks, 0 ERROR lines; password entry on both; the DVR's 7 channel cards; the PTZ card live after its password was entered again (B25) |
| Module split complete (E1) | This plan | 3.0.0-rc1.0 to 3.0.0-rc1.5: 14 files; the main file 1,797 lines, from 18,198. No further split planned |
| 3.0.0-rc1.4 field test | This plan, A18 | CrystalHeeler, 2026-10-02: working on both systems. Logs: start-up scans as before (system A 2 devices, system B 2 devices, 1 streaming); password entry on system B found the DVR stream and 7 channel cards; on system A the Hikvision password was accepted; no error lines |
| Empty page text during a scan (B21) | CrystalHeeler, 2026-10-02 | 3.0.0-rc1.4: "No Cameras Found Yet" while a scan runs; "Click Scan Network" only when no scan is running |
| Tests for the scan | This plan, E1 | 3.0.0-rc1.4: 25 checks with the real `run_scan` and `_probe_host_port` against a made-up network |
| Module split: the scan and the probers (E1) | This plan | 3.0.0-rc1.4: `anycam_scan.py` (18 functions) and `anycam_probe.py` (26 functions). All 363 definitions compared with the code before the move: none missing, none changed; page and routes identical |
| 2.6.6 and 2.6.7 field tests | This plan, A12, A13 | CrystalHeeler, 2026-10-02: done, nothing unexpected. Night boost needs no more work for now; detection zones (C17) will follow. Insects were still recorded overnight: see B20 |
| Privacy scrub of the repository and GitHub | CrystalHeeler, 2026-10-02 | No real names, network labels, addresses, hardware identifiers or places in any file, commit message, tag or release file. 48 binary files removed (old release zips, transcripts, 44 audit reports); the history rewritten (188 commits, 118 tags) and force-pushed; the 12 release zips and 3 release notes replaced. Verified on a fresh copy from GitHub: 0 items. Rule 9 in CLAUDE.md and the same rule in the global instructions. Open: GitHub still serves old commits by their hash until its own clean-up runs |
| 3.0.0-rc1.3 field test | This plan, A17 | CrystalHeeler, 2026-10-02: stable on both systems. Scan logs before and after compared: the same devices and cards on both; the only differences are the counts of other live hosts on each network. The restored step did not run (0 lines), as expected; no error lines. 3.0.0-rc1.2 and 3.0.0-rc1.3 pushed, not published |
| `probe_http_identity` restored (B19) | This plan | 3.0.0-rc1.3: the `def` line lost in 2.4.0-rc1.0 is back; the function is identical to 2.3.x (118 lines, compared). Reached only for a device that answers ONVIF discovery and has no card from the port scan. The unused `is_camera_positive` and `probe_http_for_camera` stay for now (CrystalHeeler) |
| 3.0.0-rc1.2 field test | This plan, A16 | CrystalHeeler, 2026-10-02: 3.0.0-rc1.2 on both systems, "they both appear to be stable". Logs from both: no error lines, go2rtc ready |
| 3.0.0-rc1.1 field test | This plan, A15 | CrystalHeeler, 2026-10-02: "everything seems to be working properly". 3.0.0-rc1.0 and 3.0.0-rc1.1 pushed, not published |
| Module split: go2rtc (E1) | This plan | 3.0.0-rc1.2: `anycam_go2rtc.py` (15 functions, 11 constants and state objects). The Enhanced View engine stays in the main file. All 362 definitions compared with 3.0.0-rc1.1: none missing, none changed; page and routes identical |
| 3.0.0-rc1.0 field test | This plan, A14 | CrystalHeeler, 2026-10-02: "3.0.0-rc1.0 is stable" |
| Module split, stage 2 (E1) | This plan | 3.0.0-rc1.1: `anycam_motion.py` (58 functions, 37 constants and state objects) and `anycam_storage.py` (5 functions); `anycam_host.py` links them to the main file. All 357 definitions compared with 3.0.0-rc1.0: none missing, none changed. Two new gate checks, each confirmed by a deliberate mistake |
| Automated tests in the repository (E7) | This plan | 3.0.0-rc1.0: `tests/` with 289 server checks, 110 page checks and an undefined-name check; gate 8 runs them (21 s); confirmed by breaking a value on purpose |
| Module split, stage 1 (E1) | This plan | 3.0.0-rc1.0: `camera_db.py` (1,939 lines) and `page_script.py` (2,782 lines); the gate, tests, Dockerfile and packager handle several files; `package_release.py` is in the repository |
| Credentials stripped from every log line (E4) | This plan | 3.0.0-rc1.0: one filter on the log handlers; `_strip_creds` bounded to the host part |
| Old `/stream/{camera_id}` endpoint removed (E6) | This plan | 3.0.0-rc1.0: 137 lines |
| Manual-tier endpoints removed (E8) | This plan | 3.0.0-rc1.0: two handlers, their routes, `tier_change_kill` and `manual_override` |
| Sunrise check: a wrong home location is detected | This plan, C16; CrystalHeeler, 2026-10-02 | 2.6.8: a location more than 52.5 degrees of longitude from its time zone turns the check off, with one log warning and a cog note; the location is re-read every 6 hours |
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
