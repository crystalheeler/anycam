# AnyCam Build Plan

**Compiled:** 28 September 2026; updated 3 October 2026. Current release: 3.0.0, published 2026-10-02 (the code of 3.0.0-rc1.5). The release candidates 3.0.0-rc1.0 to 3.0.0-rc1.5 are pushed, not published, all field-tested. The history was rewritten on 2026-10-02 (privacy scrub); older commit hashes no longer exist.
**Purpose:** the open task list that CLAUDE.md rule 7 says to consult before every build. Update it as items close.

**Sources swept:** CLAUDE.md; session memory; this project's chat history; `docs/legacy/MoreToDo.txt`; the four transcripts in `docs/legacy/*.docx`; the plan documents in `docs/`; every deferred, known-issue and out-of-scope note in `CHANGELOG.md`; the 2.6.x audit reports; field logs; and the source itself. Each item was checked against the current code. Items that turned out to be done are listed at the end, so they can come off the older lists.

Since 2026-10-03 the open items are arranged into version phases (see Phases after 3.0.0). Rule 7 still applies: the version and scope are confirmed with CrystalHeeler before each build.

A readable, colour-coded version, `docs/BUILD_PLAN.html`, is generated from this file by `docs/render_build_plan.py`. Regenerate it after every change to this file.

---

## Next up: 3.0.1, in work order

Agreed with CrystalHeeler on 2026-10-03.

1. **B2, missing cameras.** Two cameras built into household appliances, on test system B; the make and model are not available. First step: the scan logs every live host with its MAC maker and open ports; then a test system B log identifies the appliances.
2. **C10, Firefox and H.265 live view.** (1) Check what the browser can play before trying live view; if it cannot play the stream, go straight to the classic view with a plain message, not a red error. (2) Replace go2rtc's raw error text with readable text. (3) Optionally offer an H.264 sub-stream in live view, which gives up resolution: CrystalHeeler decides at the start of C10.
3. **The rest of the phase:** B20, B23, B24, B26, B12, B8, F10, C11 (also closes B3), C20.

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

Agreed with CrystalHeeler on 2026-10-03. Each phase runs as 3.0.0 did: release candidates, a field test, then a publish. An item marked Discuss gets its design discussion at the start of its phase. Rule 7 still applies: the version and scope are confirmed before each build.

| Version | Theme | Items |
|---|---|---|
| 3.0.1 | Fixes, then the Firefox message | **B2 first** (CrystalHeeler, 2026-10-03: "the first thing we work on"), **C10 second** (CrystalHeeler, 2026-10-03), then B20, B23, B24, B26, B12, B8, F10, C11 (also closes B3), C20 |
| 3.1.0 | Cards | B25, C19, D3 |
| 3.2.0 | Live view | C5, C3, C6 |
| 3.3.0 | One connection per camera | C4, B15 |
| 3.4.0 | Detection zones | C17 |
| 3.5.0 | Discovery and DVRs | D1, D2 |
| 3.6.0 | Recording upload | C14 |
| 3.7.0 | Hardware decode | B11, C9 |

Outside the phases: B6 waits until CrystalHeeler unlocks the Microseven.

---

## A. Field tests

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|

## B. Bugs

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| B2 | Logs | **One or two cameras are not discovered** | CrystalHeeler, 2026-09-28. 2026-10-03, CrystalHeeler: the missing cameras are two cameras built into household appliances, one of them probably a robot vacuum. History: the 2.4.0-rc2.1 changelog records one of them on test system B, found by 2.4.0-rc1.0 on ports 80, 443, 8080, 8291 and 8888 and lost in 2.4.0-rc2.0 (the nmap port-list fault fixed in rc2.1). The 3.0.0-rc1.5 log of test system B: 16 live hosts, 15 port-scanned on 54 camera ports, 3 with an open port (the DVR, the Oak-D, one device that resets RTSP probes on 80, 443 and 8080). The other 12 hosts are dropped without a log line, so the log cannot show whether the appliance cameras were seen. CrystalHeeler, 2026-10-03: both are on test system B; the make and model are not available; neither camera is in Home Assistant; the addresses may follow | First item of 3.0.1 (CrystalHeeler, 2026-10-03). Identify the devices from the network, not the model: (1) the scan logs every live host with its MAC maker (from the IEEE OUI database AnyCam already loads), its open ports, and why it got no card. (2) The first 3.0.1 release candidate carries this; CrystalHeeler sends a test system B log after a scan. (3) For the appliance hosts in that log: check the maker's documents for a local stream (RTSP, ONVIF, HTTP) and widen the port check for them (2.4.0-rc1.0 saw 8291 and 8888). (4) Then decide: a card with live video, or, for a camera that streams only through the maker's app, an information card that says so |
| B3 | Ready | Skip Non-Reference Frames breaks every camera it touches | ffmpeg rejects `nonref`; its valid value is `noref`. Confirmed in Log1: every launch of the H.264 camera at 192.168.50.73:8765 fails. The "~40% less CPU" in its description never happened, because the setting never applied | Fix the value and field-test, or remove the option (see C11). Until then, turning it off restores that camera |
| B12 | Ready | VAAPI reported as available on a Pi 4 | Log1 startup. The Pi has no VAAPI device; the check only confirms ffmpeg was built with VAAPI | Check for a real device before reporting it available |
| B23 | Ready | **Adding a camera by hand with protocol WebRTC always fails** | Found 2026-10-02 by the 3.0.0-rc1.5 password-entry tests. `api_add_camera` makes the protocol upper case ("WEBRTC"), then compares it with "WebRTC", so no stream is found and the answer is 400 "Could not connect". WS-RTSP is not affected | Compare in one letter case, and add a check to tests Z9. Version to confirm |
| B24 | Ready | **The stream-table check during password entry does not pace rate-limited cameras** | Found 2026-10-02 by the 3.0.0-rc1.5 password-entry tests. `_probe_db_streams` waits out the brand cooldown only if its table entry has `throttle_type`, and no `STREAM_DB` entry has that key (it is in `CAMERA_DB`). So on a camera such as the Microseven, the one-socket check and each ffprobe after it run without the 5 s wait. Reached only when a rate-limited camera takes the direct-RTSP path, or the ONVIF path with fewer than two working profiles | Take the throttle from the camera's brand (`_brand_throttle_seconds`), as the rest of password entry does, and add a check to tests Z4. Version to confirm |
| B25 | Discuss | **A camera's saved streams go stale when its settings change** | CrystalHeeler, 2026-10-02. The Hikvision PTZ card ran on the ffmpeg snapshot loop (28 starts in the test system A log of 2026-10-02, 3.0.0-rc1.4), although the camera's sub-stream is H.265, 704x480 at 20 fps (camera settings screenshot), which the live-card rule accepts. AnyCam reads a camera's streams only at password entry and at Deep Re-Probe, and stores them. On 3.0.0-rc1.5 the password was entered again (19:23:35): the stream-table check found `/Streaming/Channels/102`, and the card played live from 19:23:38. Which saved value had blocked it (codec, width, or no sub-stream) cannot be known now | Choose the trigger, then build: for example, read the streams again with the saved password when a card or Enhanced View cannot use what is saved, and a "Refresh streams" action in the card's cog. Respect the brand cooldown (Microseven). No version: phases are planned after 3.0.0 |
| B26 | Ready | **The page's "open log" link fails for a store install** | Found 2026-10-02 while renaming the zip folder. `openHALog()` in `page_script.py` opens `/config/app/local_camera_discovery/logs`. That ID is right only for a copy in `/addons`: the Supervisor names an add-on `<repository>_<slug>` (`supervisor/store/data.py`), so a copy from the store repository (github.com/crystalheeler/crystalheeler) has another ID | Read the add-on's own ID from the Supervisor at start-up (`GET /addons/self/info`, no extra permission) and fill it into the page. No version: phases are planned after 3.0.0 |
| B20 | Ready | **A camera card shows a picture that is minutes old; recordings start late** | CrystalHeeler, 2026-10-02, 2.6.7, test system B, log 05:12-05:30. (1) The DVR answered 404 to the sub-stream of ch4 and ch7 183 times, so live detection was off and AnyCam fell back to decoding the 3840x2160 main stream with ffmpeg. (2) That loop delivered 2 to 25 frames a second from a 7 frames a second camera: it was working through a backlog, so its pictures were behind real time. The card and the motion detector both used those pictures. At about 05:22 the ch7 card showed events from 05:14 to 05:20 and its button showed REC (a recording started at 05:21:57). A recording copies the live main stream, so a recording started from a late picture does not hold the event. (3) Separate: when the page opened at 05:13:36, ch2, ch6 and ch8 were served pictures 410 s old | Approved by CrystalHeeler 2026-10-02, for the build right after 3.0.0; no version number yet (B19 moved to 3.0.0-rc1.3). (1) The fallback reads keyframes only: 1 picture a second, always current. (2) The retry after a failed sub-stream doubles from 10 s to 5 min, resets on success and on arm or disarm, and logs one warning at the first failure and one line at recovery. (3) A picture older than 10 s is not shown; the card says Loading. The 404 answers: CrystalHeeler changed the DVR's detection and alarm settings on 2026-10-02 to stop its beeping and will check the sub-stream setting (4) Added 2026-10-02: insects were still recorded overnight on 2.6.7. Every log of that day from 05:12 on shows live detection never running on test system B (1,786 "live detection stopped" lines, 0 "watching the live stream", 0 "changed in one picture only"). The single-picture rule and the stream clock exist only in live detection; the fallback records on one changed picture. Give the fallback the same protection: at 1 picture a second, record only when a picture differs from both of the two pictures before it (5) 2026-10-02 evening, 3.0.0-rc1.4: the DVR still answers 404 for the sub-stream, also to the password path (`subtype=1`), so the cause is the DVR setting; 1,970 "live detection stopped" lines in 86 minutes on system B. On system A the armed Microseven draws a detector and a recording-stream restart about every 30 s against its broken RTSP; the same back-off applies (6) 2026-10-02 evening: CrystalHeeler changed the DVR's sub-stream setting and re-enabled the streams; the channel cards now play live. The cause of the 404 answers is gone; the approved fixes (1) to (4) still stand for the next camera whose sub-stream fails |
| B6 | You | **Test the Microseven once it is unlocked, and make AnyCam robust to cameras that lock out** | CrystalHeeler, 2026-09-29: the Microseven at 10.0.0.22 is recognized and authenticates, but serves MJPEG only, with no RTSP stream. That matches the lockout CLAUDE.md describes, which only a power cycle reliably clears. It may be the same fault as the "Invalid data found" regression open since 2.4.0-rc3.3. It also blocks the last Tier 1 check: that the 2.6.1 ONVIF fix stops AnyCam opening connections inside the Microseven's 5 s cooldown 2026-10-03: the throttle research (`docs/anycam_rtsp_throttle_research_report.md`) found the connection-rate limit rare: the Hipcam RealServer family (Microseven, Sricam, Vstarcam, older Wansview, Tenvis) is the only documented family, 5 of the 76 entries in CAMERA_DB. The lockout that only a power cycle clears was seen on the Microseven; the research names no other camera with it. The nearest is CVE-2023-50685 in the same firmware family, a crash that recovers in about 45 s | Retitled by CrystalHeeler, 2026-10-03. When CrystalHeeler has unlocked the camera: field-test that it behaves as expected (scan, password entry with the 5 s pacing, card, Enhanced View, no new lockout over a day). Then review the code: detect the locked state (accepts the connection, returns no valid answer), stop connecting to it, and tell the user on the card to power-cycle the camera. No phase until it is unlocked |
| B15 | Discuss | Opening the classic view stops the card's working stream | test system B log, 12:19:35 on 2026-09-29: entering the classic view cancelled a card stream at frame 38,450 and opened a new full-resolution connection, which timed out after 30 s with no frames. Proposed for 2.6.5 as fix 1c and held back: keeping the old stream until the new one delivers needs two connections to one camera at once, through per-camera state that assumes one. Rate-limited cameras (Microseven) cannot take two connections | Discuss. Live view now covers this camera in Chrome, so the classic view is the fallback path only |
| B8 | Ready | Scan progress bar jumps and is inaccurate | `MoreToDo.txt`; changelog 2.2.9 and 2.3.0 | Base progress on work completed, not time elapsed |
| B11 | Later | **Hardware decode silently falls back to software** | Log1: ffmpeg cannot open the Pi's decoder devices (`/dev/media0` to `3`, "Operation not permitted"), then decodes in software, while AnyCam logs "hw first frame (hevc_drm)". Classic 4K runs at 8 fps; 2.6.0-rc2.5 measured 22 fps. The gray frames CrystalHeeler sees when the classic view bogs down fit this: ffmpeg's HEVC decoder fills any missing frame with mid-gray, and Log1 shows ch7 repeating one identical frame 400 times. Live view does no decoding on the Pi, which is why Chrome was clean | Parked by CrystalHeeler. When picked up: logs first, then find what changed since rc2.5 (HAOS or Supervisor update, ffmpeg 5.1.8 to 5.1.9, device permissions). Also make the log stop reporting hardware decode when it is not happening |

## C. Video and recording

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| C11 | Ready | **Replace most or all of the option toggles with automatic behaviour** | CrystalHeeler, 2026-09-28. Eight toggles since 2.6.4 removed Live View, several broken or no longer relevant: Skip Non-Reference Frames never worked (B3), Fast Stream Start does nothing at 4K, and Live View makes several decode options irrelevant for the camera being watched. Low Latency Probe is the counter-example: it measurably helps the slow-starting H.264 camera (B13), so CrystalHeeler is keeping it for now. It is a candidate to switch on automatically for streams that start slowly. 2026-10-02: every one of these toggles acts only on the snapshot loop (ffmpeg card pictures and the classic Enhanced View); none affects live view, live cards, the motion detector or recording. In the logs of 2026-10-02 only the Hikvision PTZ card on test system A runs that loop; test system B's cards all use HTTP snapshots. Low FPS Mode measured there: 2.0 pictures a second (2,049 in 1,028 s) against 9.8 (1,049 in 107 s) in runs where the stored codec was empty and the switch did not apply; it saves the scaling and JPEG encoding of the other frames, not the decode; no CPU figures in the logs. Limit Threads: `-threads 2` stands after `-i`, so it limits only the JPEG encoder, not the decoder; no measurable effect in the logs | CrystalHeeler, 2026-10-02, decided: remove Skip Non-Reference Frames, Fast Stream Start, Stagger Polling, Low FPS Mode and Limit Threads, each with its code ("In an ideal world, the cards would have the fullest FPS possible"); keep Hardware Decode until B11 (then automatic), Adaptive Quality until C3, Low Latency Probe until an automatic design. Without Low FPS Mode an H.265 card drawn by ffmpeg runs at the snapshot loop's own cap: 8 fps, 4 fps at 3840 wide, 10 fps for H.264; raising those caps is a separate decision. Before removing any option, check how the Supervisor treats a saved value for it (auto update skips a version whose options fail). No version: phases are planned after 3.0.0. Pairs with C3 |
| C17 | Discuss | **Detection zones: detect small, far objects** | CrystalHeeler, 2026-10-02: a garage door opening in the far corner of the ch4 picture did not record, and the tuning line said "too small to record at any sensitivity". Log 10:52-10:58: minute peaks 0.1% to 0.5% of the picture; the most sensitive daytime setting needs 1.0%. The door is about 0.2% of the picture (estimated from two screenshots), the same size as the still-scene peaks, so a lower floor for the whole picture would record noise. Also: the DVR's sub-stream answered 404, so detection ran on the fallback, about 42 comparisons a minute, not 240 (B20) | CrystalHeeler, 2026-10-02: yes. His requirements are saved in `docs/Detection_Zones_Plan.md`: a drawing window over the Enhanced View live feed; polygons from straight lines, click to set each anchor point, close the loop to make the zone active; a double click leaves drawing and keeps the lines without an active zone; anchor points can be moved; no line limit unless needed; at most 6 polygons for each camera; a name for each polygon; a sensitivity for each polygon; the camera's own sensitivity then applies outside the zones; a "Detection in zones only" switch. Next: the design discussion on the open points listed in that document. No version assigned |
| C20 | Ready | **Remove the Classic button from Enhanced View** | CrystalHeeler, 2026-10-03. The button opens the classic view by hand, to compare it with live view. The automatic fallback to the classic view stays: the page uses it when go2rtc is not ready, when the camera has no stream go2rtc can relay (the Microseven on HTTP snapshots), when the browser cannot play the stream (Firefox and H.265, C10), and when live view shows no picture within 30 s (`page_script.py`) | 3.0.1: remove the button and its function (`focusUseClassic`). Remove the classic engine itself (the native-resolution snapshot loop, the adaptive ladder, the hardware preheater, the Adaptive Quality toggle) only when no camera or browser needs the fallback any more: see C19, C10, C4 |
| C19 | Discuss | **Fewer snapshot cards** | CrystalHeeler, 2026-10-03. The ffmpeg snapshot loop decodes on the Pi for every card that cannot play live: a stream wider than 1,920, an MJPEG stream, H.265 in Firefox (C10), or saved streams that are out of date (B25: the Hikvision PTZ card ran on snapshots until its password was entered again) | Discuss with B25 and C10: make live play possible for more cards, so the snapshot loop runs only where live play cannot work. No version: phases are planned after 3.0.0 |
| C14 | Later | **Upload recordings to SFTP or FTP** | CrystalHeeler, 2026-09-30: per-camera recording destination, including SCP, SFTP, FTP or another remote share. Samba and NFS already work in 2.6.6 through Home Assistant's network storage under /media. SCP is SFTP in current OpenSSH | Parked (CrystalHeeler, 2026-10-02: "We'll get to it eventually"; do not propose it for a build). Upload each finished file, then delete the local copy; keep it and retry until the upload succeeds. SFTP needs asyncssh; FTP and FTPS are in Python's standard library. Passwords stored encrypted, like camera passwords |
| C3 | Discuss | Automatic quality in live view | CLAUDE.md: the Enhanced View step-down redesign "requires full discussion before any coding." 2.6.5 removed the Resolution, Frame Rate and Auto controls at CrystalHeeler's request. Correction to the 2026-09-26 research: WebRTC congestion control cannot adjust a stream that go2rtc passes through unchanged | In live view, automatic quality means switching between the camera's own streams (main to sub) when playback stalls. Discuss before coding |
| C10 | Ready | H.265 live view depends on the browser | Confirmed 2026-09-28: Chrome plays H.265 live; Firefox 156 and LibreWolf do not. go2rtc's own compatibility table lists Desktop Firefox as H.264-only for every live method, so this is not a security setting | Three changes. (1) Check what the browser can play before trying live view, and go straight to classic with a plain message instead of a red error. (2) Replace go2rtc's raw error text with something readable. (3) Optionally offer an H.264 sub-stream in live view, which trades resolution, so CrystalHeeler's decision. 3.0.1, second item after B2 (CrystalHeeler, 2026-10-03). Change (3) needs CrystalHeeler's decision at the start of the work |
| C4 | Discuss | **One camera connection through go2rtc: thumbnails, motion and recording read go2rtc's local stream** | CrystalHeeler's long-term interest since April 2026 ("Fix B"). One camera connection instead of several, and go2rtc copes with cameras that break the RTSP rules. 2026-10-03 (merged from C18): CrystalHeeler, 2026-10-03, asked why everything cannot run through go2rtc. go2rtc relays without decoding, so the motion detector and the picture paths need ffmpeg (see C19). But each consumer opens its own connection to the camera today: an armed camera can have up to 5 open at once (live card, Enhanced View, detector, recording buffer, snapshot loop). The Microseven resets connections and DVRs limit sessions. go2rtc's MP4 output (module `mp4`) is already loaded | Discuss, then design: (1) go2rtc holds the only connection to each camera stream; AnyCam's ffmpeg jobs read from go2rtc's RTSP server on 127.0.0.1. That server is off by design (security model in `anycam_go2rtc.py`), so a security review comes first. (2) Recording saves go2rtc's MP4 output to files without the buffer ffmpeg; the 3 s of pre-event video needs its own design. No version: phases are planned after 3.0.0 |
| C5 | Ready | Audio in live view | 2.6.3 plays video only | Add audio to the player; check codec support per browser |
| C6 | Later | Cameras that speak WebRTC or RTSP-over-WebSocket | Detected since 1.x, but shown as information cards only | go2rtc can play both |
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

## F. Release engineering

| # | Status | Item | Why / source | Next step |
|---|---|---|---|---|
| F10 | Ready | **Show in Sidebar and Auto update on by default** | CrystalHeeler, 2026-10-02. The add-on manifest cannot set them: the Supervisor stores both as user settings with default off (`apps/validate.py`, SCHEMA_APP_USER). An add-on may change its own options without `hassio_api` (`/addons/self/` is on the Supervisor's bypass list) | Approved 2026-10-02: on its first start AnyCam calls `POST /addons/self/options` with `ingress_panel` and `auto_update` true, once, and saves a marker in `/data`, so a later change by the user holds. Limits: on at first start, not at install; auto update waits 1 day after a new version. No version: phases are planned after 3.0.0 |

---

## Done: take these off the older lists

| Item | Where it was listed | Evidence |
|---|---|---|
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
