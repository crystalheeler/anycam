## 2.6.7

Night boost for motion detection, and insects and infrared switches no longer start recordings.

### Changes & improvements

- **Night boost.** When a camera switches to infrared, its motion sensitivity rises by 15 (a camera set to 80 works at 95), and returns when colour returns.
- **Sunrise and sunset check.** A camera that has not switched within 1 hour of sunset or sunrise is reported in the log, in its cog panel and as a Home Assistant notification.
- **Cog panel shows the mode:** "Night mode (IR): sensitivity +15" or "Day mode".
- **Recording names start with the camera:** `LorexCH4_20261001_053358.mp4`.
- **New permission: Home Assistant API.** Used to read the home location and to create the notification. Home Assistant asks you to accept it on update.

### Bugs fixed

- **Insects near the lens started recordings** (11 of 22 on one camera overnight). A recording now needs a second changed picture within 0.5 s, or one picture with 3% or more changed.
- **An infrared-colour switch started a recording.** After a light change, nothing counts for 2 s.
- **One change counted two or three times** when pictures arrived in a burst. Each picture is now compared with the one exactly 1 s earlier in the stream.

### Known issues

- The night and day colour thresholds are estimates; the tuning log line shows the colour value for checking.
- A fast animal in view for under 0.5 s and under 3% of the picture is not recorded.
- Several insects at once can still start a recording.
- Snapshot-only cameras still record on one changed picture.
- Skip Non-Reference Frames breaks H.264 cameras.

## 2.6.6

Motion detection now compares the pictures themselves, so it sees people
on the Lorex channels. Each camera has its own recording settings, opened
from a cog on its card, with a sensitivity slider and a live reading of
how strongly movement registers. Long motion events are split into files
of a length you choose. Camera cards play live video. Also: four bug
fixes and a stronger release check.

### Motion detection compares pictures (build plan B16, C13)

2.6.5 compared JPEG file sizes. On 2026-09-30 the Lorex channels' loops ran
all day but recorded nothing after 08:24: a person barely changes the size
of a 9 to 10 KB Lorex snapshot. The same method recorded a clip at 06:21
when a camera switched from night to day mode.

- **How it works now.** Each frame is shrunk to a 64 x 48 grey picture and
  compared with the picture from about a second before, cell by cell.
  Before comparing, both pictures are brought to the same brightness and
  contrast, so a cloud, an exposure change or a night-mode switch changes
  nothing.
- **Light changes.** The picture is split into 16 areas. A change that
  reaches more than 75% of them is treated as a light change: it is logged
  with its size and not recorded. A person changes a few neighbouring
  areas.
- **Test results** on synthetic pictures: a 30% brightness step, a 40%
  contrast step and a night-mode switch all measured 0% changed. A block
  the size of a person covering 4.7% of the picture measured 5.0%.
- **Cost:** about 0.4 ms per comparison on a desktop PC, 4 comparisons a
  second per armed camera; expect several times that on a Raspberry Pi 4.
- **New dependency:** Pillow 12.3.0, which decodes the JPEGs.

### Motion: live detection and a 3-second pre-roll

CrystalHeeler's field test on 2026-10-01: recordings caught only the end of each
event, even at sensitivity 80 to 90. Two causes:

- Detection looked at snapshots, which the Lorex channels deliver every
  1.9 s, so it noticed motion late.
- The recording connected to the camera only after motion was found, then
  had to wait for a keyframe, so the start of the event was gone.

An armed camera now runs two pipelines, the usual video-recorder split:

- **Detection** watches the camera's smallest stream (the Lorex DVR's
  sub-stream, `subtype=1`), decoded to 64 x 48 grey pictures, 4 a second.
  Cameras with no small stream (only a stream wider than 1920) and cameras
  without RTSP keep using snapshots. Snapshots also stand in while the live
  stream starts or after it drops.
- **Recording** copies the camera's main stream, unchanged, into memory,
  always holding the last few seconds from a keyframe. When motion starts,
  the file begins at a keyframe at least **3 seconds before the motion**,
  for every event on every camera. A camera's keyframe spacing can make
  the pre-roll a little longer than 3 s.

Recordings are a direct copy of the main stream, the same full-quality
stream Enhanced View plays: no re-encoding of the video. Audio, when the
camera sends any, is converted to AAC, because the G.711 audio DVRs often
send fits neither MPEG-TS nor MP4. H.265 files carry the `hvc1` tag that
Chrome and Apple players expect.

**Cost per armed camera:** two extra camera connections held open (the
small stream and the main stream); a light decode of the small stream; and
3 s or more of the main stream in memory, a few megabytes. For a DVR, each
armed channel adds two connections to the DVR.

### Each camera has its own recording settings (cog on the card)

A cog at the top right of each card's lower half opens that camera's
settings:

- **Sensitivity**: a slider from 1 (left, least sensitive) to 100 (right,
  most sensitive). Its value shows as it moves. The default, 63, records
  when about 5% of the picture changes. A person walking through counts
  roughly twice their own size, because both where they were and where
  they are now change.
- **Live reading** under the slider, for an armed camera: the lowest
  setting that would have recorded the biggest movement of the last
  minute or two, also marked on the slider in orange. Walk past the
  camera and move the slider to the marker or beyond.
- **Cooldown** (seconds with no motion before recording stops), **Tail**
  (extra seconds after that), **File length**, and **Recording folder**.
- **Defaults** returns the camera to the standard values: sensitivity 63,
  cooldown 5 s, tail 3 s, file length 30 s, folder `/media/anycam`.

The Configuration tab's Motion Sensitivity was a number box: Home
Assistant draws every whole-number add-on setting that way and cannot show
a slider there. A setting of 5, which looked like "5%", needed 62% of the
picture to change, and nothing recorded on 2026-09-30.

**Recording folder.** Any folder under `/media`, the only part of the
system the add-on can write to. Each camera still gets its own sub-folder
inside it. For a Samba or NFS share, add it in Home Assistant (Settings,
System, Storage, Add network storage, usage Media); it appears as
`/media/<name>`. SFTP and FTP upload are planned for 2.6.7. The Storage
tab opens on the global recordings folder; cameras that record elsewhere
show in Home Assistant's Media browser, or in the Storage tab with
Unrestricted Storage Browser on.

**File length** (build plan C12): 10s, 20s, 30s, 1min, 2min or 5min.
While motion continues, a new file starts this often. An event shorter
than this is one file with a plain name; only an event that runs longer is
split, and its parts are numbered:

```
motion_20260930_081151.mp4          ← a short event: one file
motion_20260930_093505_part01.mp4   ← a long event, first part
motion_20260930_093505_part02.mp4   ← its continuation
```

ffmpeg numbers every file while it records, because it cannot know an
event will end early; when a recording stops with a single file, AnyCam
renames it without the suffix.

A file can only end on a keyframe, so lengths are approximate: within
about 1 s on the Oak-D camera and 3 s on the Hikvision. Recording now also
starts and stops exactly once per event; 2.6.5 logged "recording stopped"
two or three times per clip.

### Global recording settings (Configuration tab)

The Configuration tab keeps five recording settings, now named Global
Recordings Path, Global Motion Sensitivity, Global Recording Length,
Global Motion Cooldown and Global Recording Tail. A new switch, **Use
Global Recording Settings**, is off by default. While it is off, those
five are ignored and each camera uses its own settings. When it is on,
they apply to every camera, and each camera's cog menu shows them
read-only. Home Assistant cannot grey out add-on settings, so the five
stay editable in the Configuration tab while the switch is off.

`motion_sensitivity` is replaced by `motion_detect_level`. After
updating, the Supervisor logs one harmless warning, because the old
setting is still saved:

```
Option 'motion_sensitivity' does not exist in the schema for AnyCam
```

### Tuning line in the log

Once a minute, each armed camera logs the biggest change it saw, for
example:

```
Motion [192.168.50.217_554_ch7]: largest change in the last 60 s: 12.3% of the picture, would record at sensitivity 43 or higher (set to 63, 5.0%; 31 comparisons)
```

At INFO when something moved, at DEBUG for a still scene, so a quiet night
does not fill the log.

### Cards

- An info icon (ⓘ) right after the camera's name opens the Identity
  panel in the same place the Identity box used to, pushing the buttons
  down; click it again to close it. The panel stays open when the card
  refreshes.
- Protocol, IP address and port moved from the badges into the Identity
  list, always at the top.
- The lock moved to the right end of the button row, the card's
  bottom-right corner.
- Cards are as tall as their own content. Before, every card in a row
  stretched to the tallest one, which left an empty band above the
  buttons of the shorter cards. The buttons are slightly tighter, so the
  fullest card's row (Clear Creds, Not a Camera, Record, web page,
  Remove and the lock) fits on one line from 413 px wide.
- The Test Stream button and its server endpoint are removed.

### Live video in the camera cards (build plan C1)

Cards play live through go2rtc, using each camera's smallest stream:

- The Lorex channels play the DVR's sub-stream. AnyCam knows only their
  3840-wide main stream, so it asks the DVR for the same channel with
  `subtype=1`.
- A camera whose smallest stream is wider than 1920 stays on snapshots, so
  a phone never decodes several 4K streams at once. The Hikvision is one:
  its main stream is 2560 wide and its sub-stream is MJPEG, which cannot
  play live.
- Only the cards on screen play. Cards pause when the page is in the
  background, and while Enhanced View is open.
- A card that cannot play live uses snapshots, as before: for the rest of
  the page visit when the browser cannot play the codec (Firefox with
  H.265), and for 5 minutes after a connection that shows no video in 30 s.
- Motion detection does not depend on the cards, since 2.6.5.

### Bug fixes

- **B4: camera names and IDs in the page.** Thirteen card buttons pasted
  the camera's ID, IP or name between quotes in their click handlers. A
  camera ID can carry an ONVIF token from the camera itself, and names are
  editable, so a crafted value could break out and run script. They are
  now encoded properly, and the classic Enhanced View info bar escapes the
  name.
- **B5: "Share with community"** showed ticked and did nothing. A build
  placeholder was never filled, and its own text read as a configured
  address. The box is now hidden while no community server exists.
- **B9: "Unknown child process pid N, will report returncode 255".**
  AnyCam killed an ffmpeg that had already exited; Python's kill() checks
  the process first, and that check collected it before asyncio could. It
  now waits briefly for an exiting ffmpeg before killing it. Harmless, but
  no longer logged.
- **B10: "Cannot connect to host 172.30.32.1:8099"** for about 3 s at
  start. The web server now starts before the 2-second hardware-decoder
  check.
- **Manufacturer database** (found by the new release check): its 40,000
  entries were written to disk on the event loop, pausing everything else;
  now written on a separate thread.
- **Unrestricted Storage Browser** had no description in the Configuration
  tab (found by the new release check).

### Build and release (build plan F1 to F7)

- `build.yaml` is gone. The Supervisor warned on every build that it is
  deprecated. The Dockerfile now names the base image itself:
  `ghcr.io/home-assistant/base-debian:bookworm`, a multi-arch image.
- `verify_release.py` has seven gates, up from five:
  - Gate 6 checks that `run.sh`, `config.yaml` and the translations name
    the same settings.
  - Gate 7 checks every Dockerfile input where the build will fetch it:
    the Debian package versions, the pip wheels, the go2rtc digests and the
    base image. It needs internet access.
  - The best-practice gate also catches pathlib file I/O in async code and
    page placeholders that are never filled.
  - 7 new function contracts cover motion detection, live cards and the
    ffmpeg stop.
- The 20 older audit reports that were ZIP files are now real PDFs.
- The full coding best-practices document (sections 1.6 to 1.18 and
  Part 6) is restored; it had been lost from the repository.

### After updating

The folder inside the zip is still `local_camera_discovery`, so the
changelog shows without extra steps. The Supervisor may log the warning
about `motion_sensitivity` once. Every camera starts on the standard
recording settings; set each one from its cog, or turn on Use Global
Recording Settings.

### Known issues carried forward

- **Not checked yet: choppy recordings.** CrystalHeeler, 2026-10-01: recordings
  play in steps of about a second. A recording copies the main stream, so
  this should not happen; the cause is not known yet and needs a recorded
  file to examine. 2.6.6 records through the new buffer, so test again
  with this build.
- **Not tested before release: the new ffmpeg commands.** No ffmpeg on
  the PC this was built on, so the detection, buffer and writer commands
  were checked against the ffmpeg 5.1 documentation and tested with
  stand-in processes, not run. If motion does not start, the add-on log
  shows ffmpeg's error on the `DET:`, `BUF:` or `REC:` lines.
- **Not tested before release: file splitting.** No ffmpeg on the PC this
  was built on, so the segment options were checked against the ffmpeg 5.1
  documentation only. If recordings fail to start, the add-on log shows
  ffmpeg's error on the `REC:` line.
- **Not tested before release: the image build.** The base image changed.
  The release check confirms it exists for both processor types, but no
  Docker build ran here.
- Opening the classic view still stops the card's stream (build plan B15).
- Skip Non-Reference Frames breaks every H.264 camera (build plan B3).

## 2.6.5

Motion detection works on the Lorex channels again and no longer needs a
viewer. Enhanced View: slow-starting cameras now play live, landscape on a
phone fills the screen, the manual quality menus are gone, and a loading
message replaces the blank or gray screen while a feed starts. The folder
inside the zip no longer carries the version.

### Motion detection (build plan B1)

Motion detection compared frames only on the ffmpeg thumbnail path. The
Lorex channels poll HTTP snapshots for their cards, so on those cameras
motion was checked only while a channel was open in the classic Enhanced
View, which runs ffmpeg. That is how it worked on 2026-09-28. 2.6.4 made
live view the default, so the classic view stopped running and motion
detection on the Lorex channels stopped with it.

- Motion is now checked on both paths, HTTP snapshots included.
- An armed camera keeps its loop running with no one watching. Before,
  every loop stopped 30 s after the last page poll, and detection stopped
  with it.
- A background check every 10 s restarts the loop of an armed camera that
  has stopped, and ends a recording once the cooldown and padding have
  passed, whether or not frames arrive. Before, a recording ended only
  when a new frame arrived, so leaving the view left it running: the
  "recording never stops" report.
- The armed state is saved in `/data/motion.json` and restored at start.
  Before, every restart or update silently disarmed every camera.
- The Record button shows the server's state. The page never asked for
  it: after any page load every card showed "Record", even on an armed
  camera, and clicking it then disarmed the camera, because the button
  flipped the state blindly (test system B log, 2026-09-30 07:20:45). The page now
  reads every camera's state at load and every 3 s, and the button asks
  for the state you click for.
- A recording whose ffmpeg exits on its own is marked stopped, so the next
  motion starts a new one. The recording's error output is now read, so
  a full pipe cannot stall it.
- The comparison starts afresh whenever the stream changes resolution
  (card to Enhanced View, or an adaptive step), which read as motion.
- An armed camera keeps detecting while you watch another camera in live
  view. The classic view still pauses other cameras for CPU.

Cost: each armed Lorex channel polls one HTTP snapshot every 1.8 s (measured),
all the time. Each armed ffmpeg camera keeps one thumbnail decode running.

### Slow-starting cameras (build plan B13)

The 2026-09-29 log from the test system B showed that every new connection to
the H.264 camera at 192.168.50.73:8765 took 19 to 28 s to deliver its first
frame. A player cannot draw anything until the stream's first keyframe
arrives, so a stream with keyframes far apart starts slowly for every
viewer.

- Live view now waits 30 s for the first frame, up from 12 s. In 2.6.4 it
  gave up on this camera every time.
- Live view now tries again every time you open Enhanced View. In 2.6.4 one
  failure kept a camera on the classic view until the page was reloaded,
  and the Home Assistant app keeps the page open for days. A browser that
  cannot play the codec (Firefox with H.265) is still remembered, so it
  does not show the red message on every open.

### Landscape on a phone or tablet

In Enhanced View, turn a touch-screen device to landscape. The picture
fills the screen, keeps its aspect ratio, and has black bars at the edges.
The bottom bar hides, and Home Assistant hides its "AnyCam" title bar
through the kiosk-mode request its app panel accepts from an add-on page.
The red X stays. Turn back to portrait, or close the view, and everything
returns. Desktop browsers do not change.

The phone's own status bar (clock, battery) stays: a web page cannot hide
it, and Home Assistant's frame does not allow the browser's full-screen
mode.

### Menus removed

The Resolution, Frame Rate and Auto controls are gone from Enhanced View.
The Classic button and the stream information stay. The "decoded on this
device, not the Pi" text is gone.

The classic view still adapts its quality on its own when the Adaptive
Quality option is on. A resolution or frame rate chosen on an older page
was remembered by the server, with no way left to clear it, so AnyCam now
clears it each time Enhanced View opens.

### Loading message

- Enhanced View shows "Loading feed, please wait…" until the first frame.
  In the Home Assistant app on Android the live player showed a large gray
  play icon instead.
- The classic view hides the card's small thumbnail until the full stream
  delivers. In 2.6.4 the thumbnail looked like a poor-quality feed. The
  server reports frames produced since the view opened in a new
  `X-Focus-Frames` response header.
- Before the first frame, "Still connecting to the camera" shows under the
  loading message instead of the separate "Connecting to RTSP stream" box.

### Camera cards (build plan B14)

Cards say "Loading feed, please wait…" while a stream starts, in place of
"Connecting...". A card says "Stream unavailable" only after 90 s of
failed requests. In 2.6.4 it said so after 3 failed requests, about 16 s,
while the stream was still starting.

### The folder inside the zip is now `local_camera_discovery`

Up to 2.6.4 the folder carried the version (`camera_discovery-2.6.4`), and
Home Assistant showed "No changelog found" after each update. On a store
reload the Supervisor checks for `CHANGELOG.md` at the add-on's old folder
path before it reads the new one, and the old folder was already deleted.
The zip name still carries the version.

This update is the last one with a new folder name, so the message can
show once more. After installing, run this once in the Terminal & SSH
add-on, then check for updates again:

```
touch /addons/local_camera_discovery/CHANGELOG.md
```

### Known issues carried forward

- The camera at 192.168.50.73:8765 needs about 20 s to start until the
  Oak-D camera add-on 2.4.2 is installed. That release sets one keyframe
  per second; x264's default was one every 250 frames.
- Opening the classic view still stops the card's stream and opens a new
  full-resolution connection.
- Recordings are single files per motion event. Fixed-length pieces
  (30 s, 1, 2 or 5 min) are build plan C12.
- Motion detection compares JPEG file sizes, so a sudden light change
  counts as motion. Seen 2026-09-30 06:21 on Lorex ch7: a 9 s clip when
  the snapshot size stepped from 8,773 to about 10,000 bytes at dawn,
  most likely the camera's night-to-day switch. Build plan C13.

## 2.6.4

Live view is now always on. The **Live View (Enhanced View)** option is
removed from the Configuration tab.

### Why

2.6.3 shipped live view behind an option, off by default, until it had a
field test. It has now passed one on both Raspberry Pi 4 / HAOS systems
(2026-09-28 and 2026-09-29):

- A Lorex DVR's 3840x2160 H.265 channels and the Hikvision's 2560x1440
  H.265 stream played live in Chrome.
- A live feed stayed up for more than 10 minutes through Home Assistant's
  ingress proxy, with no freezes.
- In the same browser, on the same camera, the classic view bogged down
  while live view was smooth. The Pi's decode-and-JPEG pipeline, not the
  browser, was the cause of the poor feeds.

### What changes

- Enhanced View always tries live view first. go2rtc starts with the
  add-on every time.
- A camera still falls back to the classic view on its own when live view
  cannot play it: go2rtc is not running, the browser cannot play the
  codec, or no video arrives within 12 s. That is the same result turning
  the option off used to give, so nothing is lost by removing it.
- The **Classic** button in Enhanced View still switches to the classic
  view for the current session.

### After updating

Home Assistant keeps the old setting in its saved options until the
add-on's options are next saved, and logs one warning at start:

```
Option 'go2rtc_live_view' does not exist in the schema for AnyCam
```

It is harmless. The Supervisor drops the unknown setting and starts the
add-on normally.

### Removed with the option

`run.sh` no longer reads `go2rtc_live_view`. This had to change in the same
release: for a setting that no longer exists, `bashio::config` returns the
text `null`, which the old code would have read as "off", quietly
disabling live view.

### Known issues carried forward

- Firefox and LibreWolf cannot play H.265 live. They show a red "Live view
  unavailable" message once per H.265 camera per page load, then use the
  classic view. With the option gone this can no longer be switched off.
  A browser check that skips straight to classic with a plain message is
  planned (build plan C10).
- A camera whose stream takes longer than 12 s to deliver its first
  complete frame falls back to the classic view with the same red message.
  Seen on the H.264 camera at 192.168.50.73:8765 (build plan B13).

## 2.6.3

Two changes. Enhanced View can now play a camera live through a bundled
go2rtc, with no decode on the Pi (Tier 2). The addon also logs its
installed ffmpeg version at every start.

### Live view through go2rtc — option `go2rtc_live_view`, default OFF

**Why.** The classic Enhanced View decodes each stream on the Pi,
re-encodes it to MJPEG, and serves one JPEG per HTTP request. A Pi 4
cannot sustain that at 3840x2160 HEVC. go2rtc passes the camera's H.264
or H.265 through unchanged, over WebRTC or MSE, and the viewing device
decodes it with its own hardware. The Pi only moves bytes.

**What changes when the option is ON.**

- go2rtc v1.9.14 is bundled in the image and supervised by AnyCam. If it
  exits, AnyCam restarts it with backoff from 2 s to 60 s.
- Opening Enhanced View tries live view first. The player runs MSE and
  WebRTC in parallel, plays MSE first, and switches to WebRTC when WebRTC
  wins go2rtc's codec comparison.
- The browser falls back to the classic view on its own when, before the
  first frame: 12 s pass with no video; the socket closes unexpectedly
  twice; or every attempted mode reports an error. A camera that falls
  back uses the classic view until the page is reloaded. After the first
  frame there is no automatic fallback, because the transport is proven.
- A new **Classic** button switches to the classic view for comparison.
  Reopening the camera returns to live view.
- **Resolution** switches the camera profile go2rtc relays. **Frame Rate**
  and **Auto** are disabled in live view: a passed-through stream runs at
  the camera's own settings.
- The info bar shows the live mode (MSE or WebRTC), the resolution, and
  the frame rate decoded on the viewing device.

**What plays live.** H.264 in every current browser. H.265 in Chrome or
Edge 136 and later, and Safari. Firefox cannot play H.265 this way and
falls back to classic. MJPEG profiles, such as the Hikvision sub-stream,
always use classic.

**Motion detection.** Motion detection runs inside the thumbnail
`snap_loop`, and that loop exits 30 s after the last snapshot poll. On
entering live view:

- Motion armed: the thumbnail loop keeps running, and AnyCam starts it if
  it was stopped, the same way `handle_snapshot` does. This costs a second
  RTSP session, but motion recording keeps working.
- Motion off: the thumbnail loop stops, so go2rtc holds the only RTSP
  session and the Pi decodes nothing for that camera.

**Security.** go2rtc's documentation warns that anyone who reaches its API
can add an `exec:` source and run commands on the host. This addon runs
with `host_network` and `full_access`, so a default go2rtc would expose
that API to the LAN and to ZeroTier. Four independent controls:

1. go2rtc's API listens on `127.0.0.1:28984` only.
2. Only the `api`, `ws`, `rtsp`, `webrtc` and `mp4` modules load. `exec`,
   `echo`, `expr` and `ffmpeg` never start, so no command-running source
   exists. Leaving out `ffmpeg` also enforces zero transcode.
3. go2rtc's RTSP server is off. Its RTSP client, which reads the cameras,
   still works.
4. Browsers reach go2rtc only through AnyCam's `/go2rtc/ws` proxy, which
   forwards `/api/ws` and only for stream names AnyCam registered.

Camera passwords stay off disk. Config is passed inline, so go2rtc has no
config file to write stream URLs into. The proxy also waits out each
brand's connection cooldown before go2rtc dials the camera.

**Network.** WebRTC video uses port 28555, TCP and UDP, on the host.
Non-default ports avoid a clash with Frigate or the go2rtc add-on.

**Build.** The Dockerfile downloads the pinned go2rtc binary, verifies it
against the SHA-256 digest GitHub publishes for the v1.9.14 release, and
runs `go2rtc -version`, so a corrupt or wrong-architecture binary fails
the build. The browser player, `www/video-rtc.js`, is vendored unmodified
from the same go2rtc tag under its MIT licence.

**With the option OFF**, Enhanced View behaves exactly as in 2.6.2. Cards
are unchanged in both cases and still use snapshots. Live cards are
deferred until motion detection is fixed, because live cards would stop
the snapshot polling that keeps motion detection alive.

### Release gate

`verify_release.py` now pins each go2rtc security control. Six new
function contracts cover the module allowlist, the disabled RTSP server,
inline config, the proxy allowlist, credential handling, and the motion
guard. A separate check requires `GO2RTC_API_HOST` to be `127.0.0.1`.
Each was tested by breaking it on purpose: adding `exec` to the modules,
binding the API to `0.0.0.0`, and removing the proxy allowlist each fail
the gate.

A contracted function that no longer exists now fails the gate. Before,
deleting a function outright skipped its contract and passed.

### ffmpeg version logging

`run.sh` logs the installed ffmpeg version on every start. Since 2.6.2 the
version floats, and the Supervisor shows build output only when a build
fails. On aarch64 it also warns when the version lacks the `+rpt` suffix,
because rpivid hardware decode needs the Raspberry Pi build and the
failure is otherwise silent while `hw_decode` is off.

### Tested before release, and what was not

Run locally against the release source:

- 79 Python checks. The real AnyCam handlers ran against a fake go2rtc
  that reproduces go2rtc v1.9.14's behaviour, each behaviour confirmed in
  go2rtc's source. The supervisor ran a real subprocess. Covered:
  registration, the password encoding, the proxy both ways including a
  5 MiB frame, the allowlist, the go2rtc-down path, the focus engine with
  motion on and off, restart, and shutdown.
- 46 JavaScript checks. The fallback logic ran against the real vendored
  go2rtc player class with a fake clock.

Not tested: the real go2rtc binary, a real camera, a real browser, or a
long-lived WebSocket through Home Assistant ingress. The install on the
Pi is the first real test. Ingress killed multipart streams after 10 to
24 frames in 1.6.0; if it treats WebSockets the same way, the player falls
back to classic after two closes.

### Field results carried from 2.6.2

2.6.2 installed and ran on the Raspberry Pi 4 / HAOS target on
2026-09-28. `dpkg-query` inside the container returned
`8:5.1.9-0+deb12u1+rpt1`, confirming that the origin pin selected the
Raspberry Pi build.

## 2.6.2

Build fix. 2.6.1 could not be installed at all. No functional change to
AnyCam itself: the only edited files are the Dockerfile and the two
version strings.

### What failed

The Docker image build stopped at Dockerfile step 3 with:

```
E: Version '8:5.1.8-0+deb12u1+rpt1' for 'ffmpeg' was not found
```

The pin did not rot. The archive moved. Debian and the Raspberry Pi
archive each keep only the current version of a package, so the 5.1.9
security update deleted the 5.1.8 version this Dockerfile pinned to.

Both branches were stale by the same point release:

| Branch | Pinned | Archive holds now |
|---|---|---|
| aarch64 (rpios) | `8:5.1.8-0+deb12u1+rpt1` | `8:5.1.9-0+deb12u1+rpt1` |
| amd64 (Debian) | `7:5.1.8-0+deb12u1` | `7:5.1.9-0+deb12u1` |

The amd64 pin would have failed on its next build for the same reason.

### Fix

ffmpeg is now installed with no version constraint. It is the single
exception to the pin-everything rule set in 2.6.0-rc2.3. Do not restore
an exact ffmpeg pin: an exact pin against these archives has a shelf
life measured in months, and the failure lands on the user at install
time, not on us at build time.

ffmpeg is still constrained by source. The apt preferences file written
in Dockerfile step 1 gives ffmpeg and every `libav*`, `libsw*` and
`libpostproc*` sibling Pin-Priority 990 against
`o=Raspberry Pi Foundation`, and everything else from that origin
Pin-Priority 1. On aarch64 that forces the rpios build, because 990
beats Debian's default of 500, and the rpios build carries the Pi
patches needed for rpivid. On amd64 the rpios source is never
registered, so ffmpeg resolves to Debian. The version now floats inside
the bookworm suite, which bounds it to the 5.1.x series.

Every other package keeps its exact pin. Those come from Debian
bookworm, which is frozen at oldstable, so they do not rotate the same
way.

### Build logs now record the ffmpeg version

Because the version floats, the Dockerfile echoes what apt actually
resolved:

```
==> ffmpeg resolved to: <version>
```

Check that line first when diagnosing any decode behaviour that differs
between builds.

### Known gap, not fixed here

No release gate inspects the Dockerfile. `verify_release.py` checks
syntax, contracts, the best-practice audit, version consistency and the
changelog. None of them would have caught a stale apt pin, which is why
2.6.1 passed all five gates and still could not build.

## 2.6.1

Tier 1 of the live-feed quality work. Three of the four planned items
landed. One item is deferred with cause. This release also repairs the
release gate, which could not run on Windows.

### Item 1 — low-latency ffmpeg flags

`_launch_snap` now always passes `-fflags +nobuffer` and `-flags
low_delay`. Both are standard for live RTSP. Neither affects stream
detection.

`+nobuffer` merges with the existing `+discardcorrupt` instead of
replacing it. ffmpeg accepts one `-fflags` value, so the second flag must
join the same string. A separate `-fflags` argument would have silently
dropped `+discardcorrupt` on every camera that needs it.

The aggressive flags sit behind a new `low_latency` option, default OFF:
`-probesize 32`, `-analyzeduration 0`, `-reorder_queue_size 1`. A small
probe size can stop ffmpeg identifying the codec. A 1-packet reorder queue
removes the RTSP jitter buffer. Both need a field test on the Pi 4 target
before they become default.

### Item 3 — Fast Stream Start now gated by resolution and codec

`fast_stream_start` no longer applies at 3840x2160 HEVC. Software decode
cannot produce a first frame before rpivid finishes warming up at that
resolution, so the parallel decode spent CPU on frames that never rendered
and opened a second RTSP session. AnyCam now suppresses the dual-proc path
and logs the reason.

The gate reads width from the adaptive ladder's active profile, not from
the camera record. A step-down may already have moved the session off 4K,
in which case the dual-proc path is still correct.

The option still applies below 4K and to h264 at any resolution.

### Item 4 — ONVIF SOAP calls honour the brand cooldown

`_rerun_onvif_auth` now awaits `_throttle_wait_if_needed` before
`GetProfiles` and before each per-profile `GetStreamUri`.

`_onvif_soap` is synchronous and opens up to two TCP connections per call,
because cameras that answer HTTP 400 to SOAP 1.2 get a SOAP 1.1 retry. The
function never consulted the throttle. A re-auth against a rate-limited
brand fired 1 + N calls back to back, and every one landed inside the
cooldown window. The wait goes in the async caller, because the sync helper
cannot await.

This closes the latent issue recorded in CLAUDE.md.

### Release gate — Windows portability

`verify_release.py` could not run on Windows. Two separate faults:

- Three `read_text()` calls omitted `encoding`, so Python used the cp1252
  locale default and raised `UnicodeDecodeError` on `camera_discovery.py`.
- `ok()` and `fail()` print U+2713 and U+2717, which cp1252 cannot encode,
  so stdout raised `UnicodeEncodeError`.

Both are fixed. The gate now runs on Windows and Linux with no environment
override.

### Item 2 — deferred, not implemented

The plan called for replacing per-frame HTTP polling with one persistent
`multipart/x-mixed-replace` response. This is deferred.

`handle_snapshot` documents that per-frame polling was a deliberate choice,
because Home Assistant ingress nginx terminates long-lived multipart
streams early. The plan contradicted a recorded prior result. Shipping it
would have risked a regression on the exact path that works today.

`ingress_stream: true` is set, and Home Assistant documents ingress support
for streaming and WebSockets, so the earlier failure may predate that flag.
The next step is a throwaway test against ingress, not a rewrite of the
working path.

### Not changed

- Card view still polls. See Item 2.
- Enhanced View still starts at the highest profile. Defaulting it to the
  sub-stream would cut the quality this work exists to raise. The adaptive
  ladder already steps down when a stream proves unstable.
- `skip_nonref` is unchanged and still defaults to OFF. ffmpeg still
  rejects `nonref` as a `skip_frame` value.
- JPEG quality is unchanged at `q:v 2` in focus mode. Raising the number
  would cut bytes per frame, but it lowers picture quality, so it needs a
  decision first.

## 2.6.0

First stable release of the 2.6.0 line. This release promotes
`2.6.0-rc3.1` to final. The source is identical to rc3.1 except for the
version strings in `camera_discovery.py` and `config.yaml`.

### Why this build

rc3.1 completed field testing on the Raspberry Pi 4 / HAOS target.
Items 2 and 3 (Fast Stream Start) and Item 4 (Locked Streams badge
persistence) stayed in. Item 1 (RTSP in card view) was reverted after
the rc3.0 field test.

### Carried forward without change

Two options remain opt-in. Both default to `false`, so this release does
not enable either one.

- `skip_nonref` — ffmpeg rejects `nonref` as a `skip_frame` value.
  Enabling this option stops every h264 launch.
- `fast_stream_start` — at 3840x2160 HEVC, software decode cannot beat
  rpivid hardware warmup. The option spends CPU on a decode that never
  renders. It also opens a second RTSP session.

Both options are scheduled for 2.6.1.

### Known limitation

Live feed quality at 3840x2160 HEVC is limited by the pipeline, not by
the network. The pipeline decodes each stream on the Pi, re-encodes it to
MJPEG, then sends one JPEG per HTTP request. 2.6.1 addresses the low-risk
part of this. A remux-only media path is the full fix.

## 2.6.0-rc3.1

**Item 1 (RTSP in card view) reverted per user request after rc3.0
field test.** Items 2+3 (Fast Stream Start) and Item 4 (Locked
Streams badge persistence) retained.

### What was reverted (Item 1)

- `_prefer_ffmpeg` gate restored to pre-rc3.0 rule:
  `(native_res AND _has_rtsp) OR (_has_rtsp AND _rtsp_probe_ok)`.
  Card view for cameras without rtsp_probe_ok (Lorex/Dahua DVR
  channels via channel-enum, etc.) routes back to http_snap_loop.
- Card-mode URL selection block removed — the rc3.0 logic that
  picked sub-stream URL when CFG_MAIN_STREAM_CARDS=false is gone.
- `out_vf` codec-dependent rules restored to pre-rc3.0 values:
  4K HEVC → fps=4 / scale=480, 1080p HEVC → fps=8 / scale=640,
  h264 → fps=10 / scale=640. Uniform fps=20 from rc3.0 reverted.
- low_fps_mode replace target restored to
  `fps={8 if w<3840 else 4}`.
- `CFG_MAIN_STREAM_CARDS` global removed.
- `main_stream_cards` option removed from config.yaml options +
  schema, translations/en.yaml, run.sh export, and run.sh "Config:"
  log line.

### What was retained from rc3.0

**Item 4 — Locked Streams badge persistence.**
- Backend api_set_credentials parses `from_locked_streams_modal`
  flag from request body; `locked_streams=[]` clear is conditional
  on it.
- Frontend submitLockedCreds tags the badge-modal POST with
  `from_locked_streams_modal: true`. submitCreds (regular Login
  button) leaves it absent.
- Cards keep the 🔒 badge when user logs in via a non-badge entry
  point.

**Items 2+3 — Fast Stream Start.**
- `CFG_FAST_STREAM_START` global + `fast_stream_start` config
  option + translations + run.sh export retained.
- `_hw_preheater` async helper retained.
- `_kill_hw_preheater` cleanup helper retained.
- snap_loop dual-proc launch path (SW for first frame, HW for
  background warmup) retained.
- Atomic swap logic in main read loop retained.
- HW preheater teardown in tier-change and focus-leave paths
  retained.

### Rationale

rc3.0 field test surfaced issues with Item 1's card-mode RTSP
behavior. Without a log to diagnose precisely, the safe move was
a surgical revert of just Item 1 — keep the architecturally-
independent Items 2, 3, and 4 in place, restore card view to
pre-rc3.0 behavior.

### Carried over from 2.6.0-rc3.0

Item 4 + Items 2+3 changes (see rc3.0 entry for full detail).

### Carried over from 2.6.0-rc2.6

The tier_change_kill flag gates the HW EOF fallback so intentional
tier-change kills no longer misclassify as HW failures. rc3.0/rc3.1
extends this by also tearing down the HW preheater on tier change.

All rc2.5 fixes (10s HW timeout, per-focus-session counter reset,
time-to-first-frame diagnostic). All rc2.4 fixes. All rc2.3 fixes.
All rc2.2 HW decode plumbing. All version pins.



**Four items, all interrelated.** Item 1 (RTSP in cards) lays
architectural foundation for items 2-3 (Fast Stream Start), since
the SW-fast / HW-upgrade dual-proc path can only swap atomically
when both procs run the same RTSP pipeline. Item 4 (Locked Streams
badge persistence) is a small unrelated cleanup folded in to share
the field-test cycle.

### Item 1 — RTSP in card view (replaces HTTP polling)

**Previous behavior:** card thumbnails used http_snap_loop, polling
the camera's HTTP JPEG endpoint at ~1 fps. Result: cards felt stale,
cached frames, ~1 fps refresh. When entering Enhanced View the
frontend kept displaying the last HTTP snapshot until snap_loop's
ffmpeg pipeline produced its first frame.

**New behavior:** card thumbnails run ffmpeg against the RTSP
stream when stream_url is populated. The `_prefer_ffmpeg` gate
was previously `(_has_rtsp AND _rtsp_probe_ok) OR (native_res AND
_has_rtsp)`. The `_rtsp_probe_ok` requirement was excluding
Lorex/Dahua DVR channels — channel-enum sets stream_url for each
populated channel but doesn't propagate rtsp_probe_ok per-channel,
so all 7 DVR channels were routing to http_snap_loop. New gate:
any populated stream_url is sufficient. http_snap_loop remains
the fallback after the existing 3-failure streak trigger.

**Main vs sub stream choice (new config option):**

`main_stream_cards` (default OFF). When OFF, cards use each
camera's sub-stream profile (typically ~640×480) — light on CPU,
network, and the rpivid HW decoder context budget. When ON, cards
use the main stream (full native resolution, downscaled to 640px
wide for display). The toggle help text warns about Pi 4 saturation
risk when ON with many HEVC main streams.

**Video filter changes:**

Old card-mode vf: `fps=4,scale=480:-2,format=yuvj420p` (4K HEVC) /
`fps=8,scale=640:-2,format=yuvj420p` (1080p HEVC) / `fps=10,scale=
640:-2,format=yuvj420p` (h264). Three different rates depending on
codec and resolution.

New card-mode vf: `fps=20,format=yuvj420p` (sub-stream mode — no
scale filter, sub is already small) or `fps=20,scale=640:-2,
format=yuvj420p` (main-stream mode or no-sub-available). Uniform
20 fps target; cards feel live, not stuttery.

low_fps_mode replace target updated from
`fps={8 if w<3840 else 4}` to `fps=20`.

### Items 2 + 3 — Fast Stream Start (Enhanced View + tier changes)

**Previous behavior:** entering Enhanced View or changing the
FPS/Resolution dropdown triggered the rc2.5/rc2.6 single-proc HW
launch. While HW warmed up (5-6s typical for rpivid HEVC at
1440p/4K, plus 1s outer-loop backoff), the frontend kept showing
the previous card thumbnail frozen. Net user-visible delay:
10-15s before the new feed appeared.

**New behavior (new config option):**

`fast_stream_start` (default OFF). When OFF, behavior identical
to rc2.6 (single-proc, HW first, 5-10s frozen thumbnail while
warming up). When ON, snap_loop launches TWO ffmpeg processes
in parallel:

  - `proc` (SW): the active proc, decoding in software. Produces
    first frame in ~1s. Main read loop reads from this; frontend
    sees a fresh frame immediately.
  - `proc_hw` (HW): warming up in background. A new
    `_hw_preheater` task reads its stdout, watches for the first
    complete JPEG, sets `state["hw_ready"]=True`.

After each successful SW frame parse in the main loop, the loop
checks `state["hw_ready"]`. When True: atomic swap — kill SW
proc, set `state["proc"] = proc_hw`, reset frame buffer, replace
the stderr drain task. The user sees the last SW frame followed
by the first HW frame at the same vf output dimensions. No visual
disruption.

The HW preheater has a 30s timeout (much longer than rc2.5's 10s
single-proc HW timeout). Reason: fast_stream_start is a best-effort
upgrade — if HW is slow, we stay on SW for the session instead of
disrupting the user's view. The rc2.5 10s exists to bound the
"frozen thumbnail" window; with fast_stream_start that window is
gone (SW is serving frames), so HW slowness is invisible.

**Edge cases handled:**

- HW preheater fails before producing a frame (rpivid context
  exhausted, decoder error, camera rejected second RTSP session):
  preheater sets `hw_preheater_failed`, exits cleanly. Main loop
  continues on SW for the session. Logs the failure with elapsed
  time and rc.
- HW preheater timeout (30s): kills proc_hw, sets the failed flag,
  exits. Same fallthrough.
- Tier change during HW warmup window: handle_focus_set_tier sets
  tier_change_kill on the active SW proc AND calls
  _kill_hw_preheater() to tear down the HW preheater + proc_hw.
  Outer loop launches fresh SW+HW pair for new tier.
- Focus leave during HW warmup: handle_focus_clear's existing
  focus_leave_kill flag fires on the active SW proc; same
  _kill_hw_preheater() cleanup added to that path.
- Outer-loop restart for any other reason: _kill_hw_preheater()
  called at the top of every launch iteration to clean up state
  from a prior aborted iteration.

**New helpers:**

`_hw_preheater(camera_id, state, hw_label, timeout_s=30)`: async
background task. Reads proc_hw.stdout in 1s polls, watches for
first complete JPEG (SOI…EOI), sets hw_ready. After signal,
drains until main loop sets hw_swapped.

`_kill_hw_preheater(state)`: idempotent cleanup. Cancels task,
kills proc_hw, pops all preheater-related state keys. Called
from outer loop, tier change, focus leave, and snap_loop end.

**State protocol additions (under `state` dict):**

  state["proc_hw"]               — HW ffmpeg proc (Popen)
  state["hw_preheater_task"]     — asyncio.Task running _hw_preheater
  state["hw_ready"]              — bool, set True on first HW JPEG
  state["hw_swapped"]            — bool, set True when main loop swapped
  state["hw_preheater_failed"]   — bool, set True on failure/timeout
  state["hw_preheat_elapsed"]    — float, time to first HW JPEG (logged)

### Item 4 — Locked Streams badge persistence

**Previous behavior:** any cred-POST to /api/credentials that
succeeded cleared `camera.locked_streams=[]` at the end of
api_set_credentials, regardless of which UI entry point invoked
it. Users who entered creds via a card's regular Login button (not
the 🔒 badge modal) would have the 🔒 badge disappear even
though they never went through the badge flow.

**New behavior:**

Frontend tags Locked Streams modal cred POSTs with
`from_locked_streams_modal: true` in the request body. The
regular Login button (submitCreds at line 12383-area) leaves
this flag absent (defaults to false server-side).

Backend in api_set_credentials parses the new field. At the
camera.update() site that previously did `locked_streams=[]`,
the clear is now conditional: only fires when
from_locked_streams_modal is True. Otherwise the original
locked_streams list is preserved.

Net result: user enters creds via card Login button → cred
validates → cards stream → 🔒 badge still showing. Bonus: this
also fixes the "wrong creds typed via Login button" case where
the badge would be lost before the user even had a chance to
review locked candidates.

Note: additional_streams population (the validated subset of
locked candidates) runs unchanged in both paths — the
post-auth validation walks camera.locked_streams against the
new creds whether or not the user came in via the badge modal.
So entering creds via Login button still correctly populates
additional_streams; the badge just additionally persists.

### Config changes (config.yaml, run.sh, translations)

Two new options added to config.yaml options + schema:
  - main_stream_cards (bool, default false)
  - fast_stream_start (bool, default false)

run.sh exports each as the corresponding env var. The post-startup
"Config:" log line splits onto a new third line:
  Config: main_stream_cards=... fast_stream_start=...

translations/en.yaml gets help text for both options, including
the Pi 4 saturation warning on main_stream_cards.

### Risks

- **fast_stream_start runs two ffmpeg procs against the same RTSP
  URL for the warmup window.** Most cameras tolerate it; some
  (especially older ONVIF/hi3516 boards) may reject the second
  session. _hw_preheater detects this via proc_hw EOF before
  first JPEG, sets hw_preheater_failed, stays SW. No crash, but
  user won't get HW for that session. Subsequent focus enters
  retry fresh.
- **main_stream_cards with many HEVC streams could exhaust rpivid
  context budget.** rpivid concurrent decode budget is bounded
  (~4-8 contexts on Pi 4). With 6+ 4K HEVC main streams active
  simultaneously, expect frame drops on some cards. Toggle help
  text warns about this.
- **fps=20 card baseline is much higher than the previous 4-10
  range.** mjpeg encode CPU cost scales with output frame rate.
  On Pi 4 with many cards visible, the mjpeg encoder may not
  keep up — would manifest as cards displaying their FPS lower
  than 20 (which is fine; ffmpeg's vf=fps just sets the target,
  it doesn't generate frames the source can't deliver).
- **HW upgrade swap is mid-session, on a different ffmpeg proc.**
  If the camera's RTSP stream has any state that's session-bound
  (sequence numbers, PTS continuity), the swap could be visible
  as a brief flicker or frame ordering glitch. Most cameras
  reset cleanly on a new RTSP SETUP; if not, the swap is just
  visually-unclean but functionally fine.
- **Item 4 fix is conservative.** Preserving locked_streams when
  user logs in via Login button means cameras successfully
  streaming may still show the 🔒 badge. Acceptable — clicking
  the badge then shows the user the locked candidates list, and
  they can dismiss/review at their pace.

### Carried over from 2.6.0-rc2.6

The tier_change_kill flag (rc2.6) gates the HW EOF fallback so
intentional tier-change kills no longer misclassify as HW
failures. rc3.0 extends this by also tearing down the HW
preheater on tier change.

All rc2.5 fixes (10s HW timeout, per-focus-session counter reset,
time-to-first-frame diagnostic). All rc2.4 fixes. All rc2.3 fixes.
All rc2.2 HW decode plumbing. All version pins.



**One fix: tier-change kill mid-HW-warmup no longer misclassifies
as a HW failure.** Regression exposed by rc2.5's elapsed-time
diagnostics; would have shipped silent without that diagnostic.

### The bug

rc2.5 field-test log on the Lorex DVR ch2:

```
20:25:28 ffmpeg starting (codec=hevc, hw:hevc_drm, ...)
20:25:30 Focus [ch2]: manual tier [21] profile[0] fps=10
20:25:30 Focus [ch2]: killed ffmpeg to apply manual tier change
20:25:30 hw EOF (rc=None, elapsed=2.2s) → sw    ← spurious
20:25:30 ffmpeg starting (codec=hevc, sw, ...)  ← new tier in SW
```

The user changed the tier 2.2s after HW launched — before HW had
finished warmup (typical warmup observed: 4.5-6.3s). The
`proc.kill()` from `handle_focus_set_tier` produced an EOF on
stdout that looked identical to ffmpeg dying on its own. snap_loop's
EOF branch saw `hw_tried=True, frames=0` and entered the
HW-EOF-fallback path: counted the failure against the per-session
HW skip budget AND silently relaunched in SW.

The new tier ended up running in SW even though HW would have
worked. The user could not tell — the FPS/profile values were
correct, just the decode path was wrong. Visible symptom was just
"FPS feels low / CPU pegged" with no log line saying anything went
wrong. rc2.5's `elapsed=X.Xs` field is what made this visible;
real HW failures hit either the 10s timeout cap or die in under
1s, so 2.2s right after a tier-change log line was the smoking
gun.

### The fix

`handle_focus_set_tier` sets `state["tier_change_kill"] = True`
right before `proc.kill()`. snap_loop's EOF branch checks this
flag *first*, before the HW EOF fallback. If set:

- Pop the flag (one-shot consume).
- Log "ffmpeg killed by tier change after N frames (rc=R) —
  outer loop will relaunch with new tier".
- `break` out of the inner read loop.

The outer restart loop then picks up the new tier from
`_FOCUS_ADAPTIVE` and launches it through the normal `hw_label`
selection path. HW decode gets a fresh attempt for the new tier,
just like the first launch did.

Mirrors the existing `focus_leave_kill` flag pattern — same
mechanism, different intent: `focus_leave_kill` means "user left
Enhanced View, snap_loop should exit entirely"; `tier_change_kill`
means "tier changed, keep running with the new tier".

### Defensive flag clearing

If `proc.kill()` raises (because proc was already dead), the
except branch pops the flag so it doesn't linger. Without that
cleanup, a stale `tier_change_kill=True` could consume a
later, unrelated EOF and log misleadingly.

### Edge case: races

If ffmpeg dies on its own at the exact same instant the user
changes the tier, the flag is set but the EOF is from the
natural death, not the kill. We'd log "killed by tier change"
instead of the natural EOF warning. Slight misleading log line,
no functional difference — outer loop relaunches either way.
Acceptable.

### What this doesn't fix

The frames > 0 case of `focus_leave_kill` is handled correctly
by the existing check below the HW EOF fallback. The frames == 0
case of `focus_leave_kill` (focus-leave during HW warmup) is
technically also broken — it would enter the HW EOF fallback,
spuriously bump the session counter, then the SW relaunch's
first-frame `_FOCUSED_CAMERA != camera_id` check would break
out of the inner loop, and the outer focus_leave_kill consumer
would pop the flag and return cleanly. User-visible result: a
brief spurious SW launch attempt that immediately exits. Not
moving this fix to rc2.6 because field-test logs don't show it
firing in practice (users typically watch for some seconds
before leaving, so frames > 0 by the time focus-leave kills
ffmpeg). If a future log shows it, we'd add focus_leave_kill to
the rc2.6 gate alongside tier_change_kill.

### CFG_HW_DECODE gating

Preserved. The flag-check fires before the HW EOF fallback
regardless of HW state, so it works in both HW-on and HW-off
modes. With HW off, the existing behavior was: tier change kill
→ EOF → fall through to "ffmpeg EOF after N frames" log →
break → outer relaunch with new tier. With rc2.6 + HW off, the
tier_change_kill log fires instead of the EOF warning. Same
net behavior, cleaner log.

### Risks

- **Outer-loop relaunch adds ~1s of latency** between tier
  change and new tier launching (the zero_frame_streak backoff
  starts at 1s). rc2.5 behavior was: in-loop SW relaunch
  immediately. rc2.6: outer loop restart, 1s backoff, then HW
  launch. Trade 1s of latency for HW decode on the new tier.
  Win.
- **If the flag set fails between flag-set and proc.kill()**
  (unlikely — that's two adjacent statements), the flag would
  linger and consume the next EOF. The kill exception handler
  pops the flag defensively, but doesn't help if some other
  exception interleaves. Worst case the next natural EOF gets
  misattributed once. Same misleading-log situation as the
  race case above.

### Carried over from 2.6.0-rc2.5

All three rc2.5 fixes (10s HW timeout, per-focus-session counter
reset, time-to-first-frame diagnostic logging). All rc2.4 fixes.
All rc2.3 fixes. All rc2.2 HW decode plumbing. All version pins.



**Three fixes giving hevc_drm a fair shot at engaging.** The rc2.4
field-test logs on two systems (Hikvision + Microseven on
one, Lorex DVR-NVR family on the other) confirmed rc2.4's
fixes worked structurally but exposed a second-order issue: the HW
path almost never gets to produce a frame before being timed out.
rc2.5 addresses that.

### Fix 1 — HW first-frame timeout extended from 3s to 10s

Every "hw decode timeout → sw" log line in the rc2.4 field tests
hit at exactly the 3s mark:

```
12:55:13 ffmpeg starting (codec=hevc, hw:hevc_drm, ...)
12:55:16 hw decode timeout → sw     (Δ=3s)

12:55:45 ffmpeg starting (codec=hevc, hw:hevc_drm, ...)
12:55:48 hw decode timeout → sw     (Δ=3s)

12:55:56 ffmpeg starting (codec=hevc, hw:hevc_drm, ...)
12:55:59 hw decode timeout → sw     (Δ=3s)
```

3s is too aggressive for rpivid's first-frame warmup at 4K HEVC,
especially after a kill+restart. The kernel V4L2 video device needs
to be reacquired, the decoder context rebuilt, the first keyframe
awaited (which depends on the camera's keyframe interval, often
1-2s on its own). 3s catches it mid-warmup, kills it, falls back
to SW. We never find out if rpivid would have produced frames at
5s or 8s.

The one case in the rc2.4 logs where hevc_drm DID produce a frame
was the Hikvision's first cred-auth entry — 19:48:06 launch,
19:48:08 first frame, 2s warmup. Faster than the 3s timeout, just
barely. Every subsequent attempt (after a kill+restart from
manual tier change or focus re-enter) timed out at 3s. So 3s is
right on the edge — works for cold-start, fails for kill+restart.

**Fix:** raise the HW first-frame timeout from 3s to 10s. Gives
honest HW a chance to warm up; truly broken HW still falls back,
just 7s later. Bounded either way.

### Fix 2 — Per-session HW skip counter reset on focus-enter

The rc2.4 log on the Lorex DVR ch4 showed the failure counter
leaking across focus sessions:

```
19:44:21 Focus: entering enhanced view for 192.168.50.217_554_ch4
19:44:24 hw decode timeout → sw                       (failure 1)
...
19:44:43 hw decode timeout → sw                       (failure 2)
...
19:45:09 Focus: leaving enhanced view
...
19:50:07 Focus: entering enhanced view for 192.168.50.217_554_ch4
19:50:10 hw decode timeout → sw                       (failure 3)
19:50:10 hevc_drm failed 3 times this session — skipping
```

That's not one session — the user left at 19:45:09 and came back
4+ minutes later at 19:50:07. The "session" log message was a lie
because `state["hw_session_fails"]` lives in the `state` dict
keyed by `camera_id`, which persists across snap_loop invocations.
Once 3 failures accumulate by any combination of focus sessions,
HW is gated out for the camera's lifetime.

The user's mental model: "I'll close Enhanced View, come back,
maybe rpivid will work this time." rc2.4's behavior: "Counter
remembers your last attempt; one more strike and HW is dead for
the camera's whole lifetime."

**Fix:** in handle_focus_set, before starting the new snap_loop
task, pop `state["hw_session_fails"]` and `state["hw_session_skip"]`.
Now "this session" actually means what the log says: one focus
entry. Every Enhanced View entry gets a fresh shot at HW.

### Fix 3 — Time-to-first-frame logged for HW launches

To tune the timeout intelligently going forward instead of
guessing, rc2.5 logs the elapsed time for HW launches:

- On first frame produced after a HW launch:
  `hw first frame in X.Xs (hevc_drm)` — proves HW worked,
  shows warmup time for this stream.
- On HW timeout fallback:
  `hw decode timeout (elapsed=X.Xs) → sw` — was it at the 10s
  cap or earlier? Earlier means ffmpeg actually died (not slow
  warmup).
- On HW EOF fallback:
  `hw EOF (rc=N, elapsed=X.Xs) → sw` — same context, lets us
  distinguish "produced no output, exited fast" from "ran a
  while then died".

Diagnostic feed for future tuning decisions. If every camera
consistently shows 4-5s, we know 10s is right; if they're all
<2s, we could tighten back; if some need 12s+, we'd know to
raise it.

### Expected behavior on the Hikvision and Lorex DVR after rc2.5

- First focus-enter: HW gets a real 10s window to produce frames.
  If the Hikvision's actual warmup is ~5s post-kill+restart (a plausible
  guess given the rc2.4 data point of 2s on cold start), HW now
  engages. Log will show `hw first frame in X.Xs (hevc_drm)`.
- Re-entering Enhanced View on the same camera: fresh HW retry
  budget. If the previous session burned 1-2 failures, those
  don't count against the next session.
- Manual tier change inside Enhanced View: ffmpeg gets killed,
  next launch retries HW (per rc2.4) with a 10s warmup window
  (per rc2.5). The pattern that consistently failed in rc2.4
  ("user picks 10fps → ffmpeg killed → HW retry times out at 3s
  → SW") should now produce `hw first frame` for the new tier.

### What this does NOT change

- The hard ceiling of ~10-15 fps at 4K HEVC even with rpivid
  engaged. Pi 4 rpivid + ffmpeg pipeline can sustain that range,
  not 30 fps. If the user picks 30fps and SW can't keep up,
  they'll still see drops — but the goal of 15-20fps stable is
  achievable when HW is engaging properly.
- The "every kill+restart costs a full warmup" cost. Manual tier
  changes still kill ffmpeg and restart. The new 10s warmup
  window means each restart costs up to 10s of "no frames" before
  decoder reaches steady state. User-visible: a brief pause when
  changing FPS/resolution in Enhanced View. Same UX as before
  rc2.4, just with more headroom for HW to actually engage.

### CFG_HW_DECODE gating

All three fixes preserve the toggle.

- Fix 1 (timeout extension): lives inside the `if hw_tried and
  frames == 0` branches, which only fire when HW was launched.
  Toggle off → no HW → no timeout extension matters.
- Fix 2 (session reset): pops state keys that only exist if rc2.4
  HW counters were populated, which requires HW to have been
  attempted. Toggle off → keys never set → pop is no-op.
- Fix 3 (timing logs): `hw_started_at` is None when hw_tried is
  False, so logs gracefully fall back to the rc2.4 format with
  no elapsed time. Toggle off → no HW → no timing log.

### Risks

- **10s warmup window doubles worst-case time to first frame on
  genuine HW failures.** If the user has a camera where rpivid
  truly doesn't work (some HEVC profile rpivid can't decode), they
  now wait 10s instead of 3s before SW fallback. Acceptable cost
  — better than the rc2.4 cost of "looks like HW works for some
  streams but never gets to prove it".
- **Per-focus-session reset of HW counters could let a genuinely
  broken HW path retry forever** if the user keeps re-entering
  Enhanced View. Each entry burns 3 × 10s = 30s of failed warmups
  before settling into SW. Cost is bounded per focus session.
  If a user reports "Enhanced View takes 30s to show first
  frame", we'd want to know — that's the diagnostic the timing
  logs from fix #3 give us.
- **Timing log adds INFO-level chatter on every HW launch.** Two
  new INFO lines per ffmpeg run (hw first frame on success, or
  the existing-but-enriched timeout/EOF lines on fallback). Logs
  grow slightly. Worth it for the data.

### Carried over from 2.6.0-rc2.4

Fix 1 (per-session HW skip after 3 consecutive 0-frame failures,
no global _HW_UNAVAILABLE add), fix 2 (HW→SW fallback preserves
native_res), fix 3 (manual tier honors prof_idx not in ladder).
All rc2.3 fixes. All rc2.2 HW decode plumbing. All version pins.



**Three fixes built on the rc2.3 field test on the Hikvision.**
The rc2.3 fixes worked — `hw:hevc_drm` engaged, decoded 91 frames
cleanly through the rpivid path. But the test also exposed three
follow-on issues. rc2.4 addresses them.

### Fix 1 — HW decoder permanently disqualified after one transient failure

The rc2.3 log on the Hikvision showed the smoking gun in 18 seconds:

```
10:16:15 SNAP: ffmpeg starting (codec=hevc, hw:hevc_drm, ...)
10:16:24 SNAP: frame 50 — 1434889 bytes
10:16:31 ffmpeg stderr: corrupt decoded frame in stream 0
10:16:31 SNAP: ffmpeg EOF after 91 frames (rc=0)
10:16:33 SNAP: ffmpeg starting (codec=hevc, hw:hevc_drm, ...)
10:16:36 SNAP: hw decode timeout → sw
```

First HW launch produced 91 frames cleanly through hevc_drm before
hitting one corrupt frame and exiting. The retry hit a 3-second
0-frame timeout — and the old code at that point added `hevc_drm`
to the global `_HW_UNAVAILABLE` set, permanently disabling it
until addon restart. Every subsequent launch in the entire log
(including session 2 after the user deleted + rescanned the
camera) shows `no hw decoder available for codec=hevc, using
software`.

A one-off timeout doesn't mean the hardware doesn't work. It can
mean the camera was between keyframes after the kill+restart, or
a brief network hiccup, or any transient runtime issue.
`_HW_UNAVAILABLE` should only carry init-level "this hardware
doesn't exist" disqualifications (vaapi connection failure on a
Pi 4, v4l2m2m synthetic decode failure, the "Could not find a
valid device" stderr match in `_drain_stderr`).

**Fix:** the timeout and EOF runtime fallback paths in snap_loop
no longer touch `_HW_UNAVAILABLE`. Instead they increment a
per-snap_loop-session counter (`state["hw_session_fails"]`) for
the failing decoder. After 3 consecutive 0-frame HW failures of
the same label in this session, the label gets added to
`state["hw_session_skip"]` — used by the HW selection block at
line ~7045 alongside `_HW_UNAVAILABLE` to skip candidates. Both
sets are checked; only the global one persists across snap_loop
sessions.

This means: on the next focus enter, on the next thumbnail
polling cycle, on a camera-delete-and-rescan, HW gets retried
fresh. The Hikvision's transient timeout no longer cascades into
"software-only for the rest of the addon process".

### Fix 2 — HW→SW fallback dropped Enhanced View to thumbnail quality

The rc2.3 log also showed the HW→SW fallback path losing
Enhanced View's native_res setting:

```
10:17:41 SNAP: ffmpeg starting (codec=hevc, hw:hevc_vaapi, adaptive:uncapped profile[2] (2560x1440), ...)
10:17:43 vaapi failed
10:17:43 SNAP: hw EOF (rc=None) → sw
10:17:43 SNAP: ffmpeg starting (codec=hevc, sw, normal, vf=fps=8,scale=640:-2,format=yuvj420p)
```

The pre-fallback launch was Enhanced View at 2560×1440 uncapped.
The post-fallback launch dropped to thumbnail vf
(`fps=8,scale=640:-2,...`). Both ran in the same snap_loop
instance with `native_res=True` in scope, but the fallback called
`_launch_snap()` with no args, so `native_res` defaulted to False
and `_launch_snap` picked the thumbnail vf branch.

That's why image 1 of the rc2.3 field-test screenshots showed
`Actual Feed: 640x360 · 7 fps` while the dropdown said "Stream
1": Enhanced View was actually serving thumbnail-quality output
because of the silent fallback path losing native_res.

**Fix:** both runtime fallback launches (timeout and EOF) now
pass `native_res=native_res`, propagating Enhanced View's mode
into the SW fallback. Mid-session HW→SW transitions preserve
focus-mode resolution and vf settings.

### Fix 3 — Manual tier selection silently launched profile[0] when prof_idx wasn't in ladder

The same field-test log also showed:

```
10:18:30 Focus: manual tier [0] profile[1] fps=None
10:18:32 SNAP: ffmpeg starting (codec=hevc, sw, adaptive:uncapped profile[0] (2560x?), ...)
```

User picked profile[1] in the dropdown. Server logged
`manual tier [0] profile[1]`. ffmpeg launched profile[0]
(2560x1440), not profile[1] (704x480). Manual override was a UI
lie — dropdown showed the user's selection, server returned
status:ok, ffmpeg ignored it.

Mechanism: the handler at line ~8180 walks the ladder looking
for a (prof_idx, fps_val) match. If no match, `best_idx` stays
at its default (0), pointing to ladder[0] = (profile[0],
uncapped). The handler then sets `ada["tier_idx"] = best_idx`
and snap_loop launches whatever ladder[0] points to.

In the rc2.3 field test session 1, the camera was loaded from
cameras.json built by an earlier rc; the dropdown's source of
truth and the ladder's source of truth had diverged. The
dropdown showed profile[1] (704x480 HEVC), but the ladder built
fresh in handle_focus_set_tier didn't have profile[1]. Match
failed → silent fallback to profile[0].

**Fix:** when the loop completes without finding a match,
append a one-off `(prof_idx, fps_val)` entry to the ladder and
use that as `best_idx`. Honors the user's explicit selection
even when the dropdown and ladder source-of-truth diverge. Logs
a warning so we can diagnose if/when the divergence happens
again — it shouldn't post-rc2.3, but the defensive code stops
the silent failure regardless.

### What this means for the Hikvision

The Hikvision still has whatever bitstream-side behavior makes the
rpivid path occasionally produce a "corrupt decoded frame" on
the first run. rc2.4 doesn't claim to fix that — that's a
separate camera-firmware-vs-decoder question. What rc2.4
guarantees is that one-off rpivid hiccups don't cascade into
"SW-only for the rest of the addon process". HW gets retried.

If hevc_drm fails 3 launches in a row in one snap_loop session,
fine — that session goes SW. But re-entering Enhanced View, or
re-authenticating the camera, gets a fresh state and another
shot at HW. That's the intended recovery model.

### CFG_HW_DECODE gating

All three fixes preserve the toggle:

- Fix 1: lives inside the existing HW timeout/EOF fallback paths
  (which only fire when HW was tried), and the per-session skip
  set is consulted in the HW selection block which is already
  gated by `if CFG_HW_DECODE:`.
- Fix 2: the SW fallback launch happens inside an HW path; `if
  CFG_HW_DECODE:` is False means HW is never selected, so the
  fallback never runs.
- Fix 3: completely independent of HW decode. Fixes a UI-vs-
  server contract issue in the manual tier handler.

### Risks

- **Per-session HW skip after 3 failures.** If HW genuinely
  doesn't work for a particular camera (not a transient — actual
  bitstream incompatibility with rpivid), this gates HW out for
  the session after 3 bad launches. Same end-state as the rc2.3
  permanent disqualification, but reaches it 3x slower. Reaches
  it nonetheless. If a user has a camera that stresses the HW
  path on every launch, they'll see 3 HW attempts before the
  session settles into SW. Acceptable cost for the recovery
  benefit.
- **Fix 3 logs a warning.** If the divergence between dropdown
  and ladder is a real ongoing issue we haven't fully traced
  (rc2.3 session 1 was reproduced on a stale cameras.json from
  rc2.2 — but there may be other paths to it), the warning will
  appear on every manual tier change. Watch for it; if it fires
  repeatedly outside the rc2.2-cameras.json scenario, there's a
  deeper bug to chase.
- **Fix 3 honors user selection unconditionally.** If the user
  somehow selects a `prof_idx` that's truly invalid (no such
  profile exists in stream_profiles at all), the one-off ladder
  entry will cause _launch_snap to look up `profiles[prof_idx]`
  and get an empty dict — same failure mode as if best_idx had
  pointed there originally. Empty profile dict → `prof_url`
  evaluates to falsy → `tier_url` falls back to
  `build_authenticated_url(cam_now)` → main stream URL. Better
  than the current silent profile[0] launch, but worth noting.

### Carried over from 2.6.0-rc2.3

Bug #1 (snap_loop reads camera-level codec) and bug #2
(probe_stream_details called with credless URL) fixes from
rc2.3. All HW decode plumbing from rc2.2. All version pins.



**Two bug fixes that finally let `hevc_drm` engage on the Hikvision
in Enhanced View.** rc2.2 landed the HW decode plumbing structurally —
probe shows `hevc_drm: available`, the candidate list is correct,
snap_loop has the right gating — but the path from "stream is HEVC"
to "snap_loop launches with `-hwaccel drm`" had two breaks. rc2.3 fixes
both.

### Bug 1 — snap_loop reads camera-level codec, not active profile's

snap_loop captures `stream_codec` once at entry from
`camera.get("stream_codec")`, then never re-reads it when the active
profile changes during focus-mode tier switching. The HW selection
block uses this stale value. Reading the rc2.2 test log:

```
SNAP [10.0.0.33_554]: adaptive focus — switching to profile[2] for tier 62
SNAP [10.0.0.33_554]: no hw decoder available for codec=, using software
SNAP [10.0.0.33_554]: ffmpeg starting (codec=?, sw, ... profile[2] (2560x1440), ...)
```

`profile[2]` carries `stream_codec='hevc'` from the locked-stream
probe. snap_loop picked profile[2]'s width/height correctly
(2560×1440) but read codec from camera-level (empty), so the HW
candidate iteration matched nothing and fell through to software.

**Fix:** in the outer snap_loop body, just before the HW selection
block, override the local `stream_codec` and `is_hevc` based on the
active profile when `native_res` is True. The reassignment feeds
`_launch_snap` by closure, so the "ffmpeg starting" log line also
reflects the right codec.

### Bug 2 — probe_stream_details called with credless URL

`find_rtsp_path` returns a bare URL without creds embedded.
`probe_stream_details(url, proto)` at line 9598 hands ffprobe a
credless URL, ffprobe gets 401 from auth-required servers and exits
non-zero, `probe_stream_details` silently returns `{}`. The downstream
consequence: `stream_profiles[0]` (the main profile) gets
`stream_codec=None`, propagating to `camera['stream_codec']=None` for
the main URL.

The locked-stream branch at line ~9728 has been doing this correctly
all along — it uses `lurl_authed` (creds embedded) and gets a
populated codec result. That's why the rc2.2 log shows codec=hevc
for the locked candidates but codec=? for the main `/Streaming/
Channels/101`.

**Fix:** pre-build an authenticated URL using the creds that just
passed `find_rtsp_path`, then pass that to `probe_stream_details`.
Same mechanism the locked-stream branch uses, applied to the main URL.

### Net behavior on the Hikvision after rc2.3

- Cred-auth runs `probe_stream_details` against an authed
  `/Streaming/Channels/101` → ffprobe succeeds → `details` populated
  with `{'stream_codec': 'hevc', 'stream_width': 2560, ...}`.
- `stream_profiles[0]` carries `stream_codec='hevc'`.
- `camera['stream_codec']` is set from `details.stream_codec` → 'hevc'.
- snap_loop's outer-body capture at line 6863 reads `'hevc'`.
- HW selection at line ~7045 picks `hevc_drm` candidate, sets
  `hw_args = ['-hwaccel', 'drm', '-c:v', 'hevc']`.
- `_launch_snap` splices those args into the ffmpeg command line.
- ffmpeg stderr shows "Hwaccel V4L2 HEVC stateless V4; devices:
  /dev/media0,/dev/video19; ..." — same line as the manual `docker
  exec` test from the rc2.1 debug cycle.
- Decoder keeps up at 2560×1440 30fps. No falling-behind. No POC
  reference errors. No half-rendered frames.

### Net behavior in Enhanced View when manually picking a profile

- User picks profile[2] (or any HEVC profile) from the dropdown.
- snap_loop re-enters with `native_res=True`.
- The new code at line ~7037 inspects `_FOCUS_ADAPTIVE[camera_id].
  ladder` for the active profile index, looks up its `stream_codec`,
  overrides local `stream_codec` if profile-level codec is set.
- HW selection picks `hevc_drm`. snap_loop launches with HW decode.

This applies to any profile in the dropdown that carries a codec
field — not just the Hikvision's specific main URL. So if a future camera
has main=H264 + sub=HEVC, switching to the sub-profile in Enhanced
View will pick `hevc_drm`.

### CFG_HW_DECODE gating

Both fixes preserve the toggle:

- Bug #1 fix: lives inside the existing `if CFG_HW_DECODE:` block.
  When the toggle is off, the codec re-derivation runs but doesn't
  matter because the HW selection that consumes it is gated.
- Bug #2 fix: not gated. Populating `stream_codec` correctly is a
  general improvement — it also drives the `is_hevc` vf filter
  selection in the non-focus path (line ~6870) and the resolution/
  codec labels shown in the dropdown UI. None of those are HW-decode
  specific.

Toggle off → no probe runs → no HW decoders ever invoked → unchanged
software-decode behavior. Same gating story as rc2.2.

### Risks

- **Hikvision will likely still show pixelation in extended
  Enhanced View even with HW decode engaged**, because we haven't
  yet untangled that camera's specific bitstream quirks from the
  generic SW-too-slow story. If `hevc_drm` engages and the artifact
  persists, we have direct evidence the artifact is bitstream-side
  and rc2.4 work would target that — not HW decode. If the artifact
  is gone, we know it was SW-too-slow. Either outcome is informative.
- **Bug #2 fix changes the auth flow for a piece of code that runs
  every cred-auth.** The new authed-URL construction uses the same
  `build_authenticated_url` mechanism the locked-stream branch already
  uses — well-tested code path — but applied earlier in the flow. If
  any non-Hikvision brand has a quirk where ffprobe behaves
  differently on an authed vs unauthed URL, that surfaces here. Low
  risk — ffprobe is well-behaved on standard RTSP — but worth
  watching the first few cred-auth runs after install.

### Carried over from 2.6.0-rc2.2

All HW decode plumbing (hevc_drm + h264_v4l2m2m + vaapi candidates,
new `_launch_snap` signature, _probe_hw_decoders dispatch, all 8
strict pins). All 2.5.0 cycle work.



**Pi 4 / Pi 5 HEVC HW decode for real this time.** Lights up the
HEVC hardware path that 2.6.0-rc1.0 through rc2.1 had been chasing.
Plus locks in the rc2.1 discovery pins so all 8 dependency versions
are now strict.

### What rc2.1 told us

The rc2.1 install succeeded but the addon log said
`hevc_v4l2request: unavailable (not compiled into ffmpeg)` and
`h264_v4l2m2m: unavailable (rc=1)`. Two separate issues, both in the
addon's probe code rather than in the rpios ffmpeg build:

1. **`hevc_v4l2request` was never a real decoder name.** Confirmed
   by running `ffmpeg -decoders` inside the running rc2.1 container —
   no v4l2request entries anywhere. rpios's ffmpeg DOES have v4l2-
   request HEVC support (configure log shows
   `--enable-v4l2-request --enable-libdrm --enable-libudev`), it just
   exposes it as a `-hwaccel` named `drm`, not as a `-c:v` decoder.
   The right invocation pattern is `-hwaccel drm -c:v hevc`. End-to-
   end live-decode test against the Hikvision camera confirmed:
   ffmpeg loads "Hwaccel V4L2 HEVC stateless V4; devices:
   /dev/media0,/dev/video19; buffers: src DMABuf, dst DMABuf;
   swfmt=rpi4_8" and decodes 6 of 9 packets cleanly with no software
   fallback.

2. **`h264_v4l2m2m` regression** was the synthetic probe being too
   strict. libx264 defaulted to High 4:4:4 Predictive (profile 244)
   on the small test pattern, which bcm2835-codec rejects. Real-world
   H264 streams use Main/High and decode fine; the probe just needed
   `-pix_fmt yuv420p -profile:v baseline` to force a profile the HW
   decoder accepts.

### Code changes

**`_HW_DECODER_CANDIDATES` restructured** from `(decoder_name, codec)`
tuples to `(label, codec, ffmpeg_args)` triples. The `ffmpeg_args` is
either `["-c:v", "<decoder>"]` (decoder-style) or `["-hwaccel",
"<name>", "-c:v", "<codec>"]` (hwaccel-style). New list, in
preference order:

```
hevc_drm     hevc  -hwaccel drm   -c:v hevc           # Pi 4/5 rpivid
h264_v4l2m2m h264                  -c:v h264_v4l2m2m  # Pi 4/5 bcm2835
hevc_vaapi   hevc  -hwaccel vaapi -c:v hevc           # generic vaapi
h264_vaapi   h264  -hwaccel vaapi -c:v h264           # generic vaapi
```

The phantom `hevc_v4l2request` and `h264_v4l2request` entries are
gone. They were never real decoder names in any ffmpeg.

**`_probe_hw_decoders` rewritten.** Dispatches each candidate to
hwaccel-style or decoder-style probe based on whether `-hwaccel` is
in its args. Hwaccel-style: static check that ffmpeg's `-hwaccels`
lists the hwaccel name, plus (for `drm` specifically) that rpivid
is loaded (`/dev/video19` + `/dev/media0` both present). Decoder-
style: synthetic encode + decode test, with the rc2.3 fix of
`-pix_fmt yuv420p -profile:v baseline/main` so the test clip uses a
profile bcm2835-codec accepts.

**`_launch_snap` signature changed** from `hw_dec: str` to
`hw_args: list[str], hw_label: str`. Caller looks up the candidate's
ffmpeg_args and label and passes them through; `_launch_snap`
splices the args into the ffmpeg command line. Lets hwaccel-style
invocations work alongside decoder-style ones without per-call
branching.

**snap_loop selection logic + live MJPEG endpoint** both updated to
the new candidate structure. Both still gate on `CFG_HW_DECODE`:
when the toggle is off, the candidate iteration is skipped entirely
and ffmpeg launches with no HW args (software-only). Existing
runtime fallback path also intact: if a HW decode launch produces no
frames within 3 seconds OR the ffmpeg process EOFs immediately, the
candidate's label gets added to `_HW_UNAVAILABLE` and the next
launch goes to software.

**`probe_stream_details` auto-disable list** updated: dropped the
v4l2request entries, added `hevc_drm`. Stream-launch failures with
"Could not find a valid device" still get auto-recorded.

### CFG_HW_DECODE gating audit

Per CrystalHeeler's directive that all HEVC HW decode work must activate
ONLY when the Hardware Decoding toggle is ON (and be benign when
OFF), every HW code path is gated:

- `_probe_hw_decoders`: returns immediately when toggle off, logging
  `"Hardware decode disabled by config — skipping probe"`.
- `snap_loop` selection: `if CFG_HW_DECODE` guard before the
  candidate iteration. When off, `hw_args` stays `[]` and ffmpeg
  launches without HW flags.
- Live MJPEG endpoint: same `if CFG_HW_DECODE and stream_codec in
  (...)` guard. Same software-only result when off.
- `probe_stream_details` auto-disable: doesn't gate on the toggle,
  but only mutates `_HW_UNAVAILABLE` when ffmpeg's stderr says a
  specific decoder failed — no HW decoder ever gets invoked, so
  there's no behavior to gate.
- `_HW_DECODER_CANDIDATES` constant: just a list, no execution.

When `CFG_HW_DECODE` is off, the changes in this rc are silent: no
ffmpeg subprocesses launched for probing, `_HW_UNAVAILABLE` stays
empty, every camera streams via software decode the same way it
always has.

### Pin lock-in (deferred from rc2.2)

All 8 dependency versions now strict in the Dockerfile. Captured
from the rc2.1 install on CrystalHeeler's Pi 4 (the system with rpivid
loaded) via `dpkg-query -W` and `pip3 freeze`:

```
python3=3.11.2-1+b1
python3-pip=23.0.1+dfsg-1
nmap=7.93+dfsg1-1
net-tools=2.10-0.1+deb12u2
iproute2=6.1.0-3
ffmpeg=8:5.1.8-0+deb12u1+rpt1   (aarch64 / rpios)
ffmpeg=7:5.1.8-0+deb12u1        (amd64 / Debian)
aiohttp==3.13.5
cryptography==48.0.0
```

The discovery RUN block from rc2.1 (`==> [2.6.0-rc2.1 discovery]
...`) is removed — it has served its purpose.

### Carried over from 2.6.0-rc2.1

apt-pinning structure (rpios as secondary apt source via signed-by
keyring + Pin-Priority overrides for ffmpeg + libav* only),
all 2.5.0 cycle work, all 2.4.x carry-over.

### Risks (rc2.2)

- **First-stream decode of a real HEVC camera not yet tested through
  the addon's snap_loop.** The end-to-end test we ran was a manual
  `docker exec ... ffmpeg -hwaccel drm -c:v hevc -i rtsp://...` from
  the terminal. snap_loop's launch path is similar but not identical
  — it uses subprocess piping, an output filter graph (scale,
  format), and runs in an asyncio context. If snap_loop's invocation
  hits some quirk the manual test didn't, the failure mode is graceful
  (3-second timeout → add `hevc_drm` to `_HW_UNAVAILABLE` → fall to
  software for that stream).
- **Pin drift.** The 8 strict pins reflect what's available right now
  in Debian Bookworm and rpios. If any rotates between this rc and a
  fresh build attempt, apt or pip fails loudly with `E: Version not
  found` and we bump in rc2.3.



**Discovery build to fix two stale pins from 2.6.0-rc2.0.** rc2.0
failed at the `apt-get install` step because two pinned versions
were not available in the configured apt sources:

```
E: Version '23.0.1+dfsg-1+deb12u1' for 'python3-pip' was not found
E: Version '8:5.1.3-1+rpt4' for 'ffmpeg' was not found
```

The other four apt pins (python3, nmap, net-tools, iproute2) and
both pip pins (aiohttp, cryptography) never got exercised because
apt aborted before reaching them.

### What rc2.1 does differently

- **Drops version pins on python3-pip and ffmpeg only.** apt now
  picks the candidate version for each (latest from the repo it's
  routed to via apt-preferences). The four working apt pins from
  rc2.0 stay strict.
- **Adds a post-install discovery RUN step** that runs:
  - `dpkg-query -W -f='${Package}=${Version}\n' python3 python3-pip
    nmap net-tools iproute2 ffmpeg`
  - `pip3 freeze | grep -E '^(aiohttp|cryptography)=='`
  Both outputs go to the build log. After rc2.1 builds successfully
  on CrystalHeeler's host, the build-log lines starting with `==>
  [2.6.0-rc2.1 discovery]` give us the exact installed versions for
  every dependency.
- **2.6.0-rc2.2 will lock in those discovered versions.** All 8
  pins (6 apt + 2 pip) become strict again. The discovery RUN step
  gets removed since it has served its purpose.

### Why discovery instead of better-guessing

The "fail-loud-on-drift" pinning contract from rc2.0 is correct in
principle: known versions, fail-loud on drift. But "drift" isn't
the right word for this case — the pins were wrong on day one,
not after time-drift. Best-effort version lookups via web search
have proven unreliable: rpios's ffmpeg has rotated through several
+rpt suffixes since the forum posts I was reading, and Debian's
python3-pip apparently never had a +deb12u1 point-release suffix at
all. Trying a second round of guesses risks a third failed build
without recovering signal. Discovery from the actual build is the
disciplined move.

### Pinning baseline that survives into rc2.2

Definitely correct (held in rc2.1 unchanged):

- python3=3.11.2-1+b1
- nmap=7.93+dfsg1-1
- net-tools=2.10-0.1+deb12u2
- iproute2=6.1.0-3
- aiohttp==3.13.5
- cryptography==48.0.0

To be discovered in rc2.1's build log, then locked in rc2.2:

- python3-pip
- ffmpeg (rpios on aarch64 / Debian on amd64)

### Carried over

Everything from rc2.0 except the two stale pins: rpios apt source
+ apt-preferences pinning, all camera_discovery.py changes (extended
_HW_DECODER_CANDIDATES, _probe_hw_decoders static-check probe path,
live MJPEG endpoint iteration, probe_stream_details auto-disable,
Pi-4 diagnostic refinement), all 2.5.0 cycle work (Lorex/Dahua DVR-
NVR family, poll-until-done channel-enum, credential leak fix), all
2.4.x carry-over.



**Pi 4 / Pi 5 HEVC hardware decode via apt-pinned rpios ffmpeg + full
dependency version pinning across the whole image.** Supersedes
2.6.0-rc1.0 (which proposed compile-from-source). Total scope: a
Dockerfile rewrite that adds `archive.raspberrypi.com/debian` as a
secondary apt source for ffmpeg only, and pins every apt + pip
package the addon installs to a specific version.

### What changed since 2.6.0-rc1.0

The 2.6.0-rc1.0 zip never got installed on a host — discussion
revealed the compile-from-source approach was wrong (would have
spent 30-45 min of Pi 4 CPU on every install) and that the rpios
.deb-direct approach would have failed dependency resolution
because rpios's ffmpeg has hard `=`-pinned deps on rpios's own
patched libav* packages. The clean path is to register
archive.raspberrypi.com as an apt source with apt-pinning that
permits only ffmpeg and the libav* family from there, and let apt
resolve the dep set the way it's designed to.

### Dockerfile architecture

aarch64 builds register `archive.raspberrypi.com/debian/ bookworm
main` as a secondary apt source via signed-by keyring. apt-
preferences in `/etc/apt/preferences.d/00-raspi-ffmpeg` set the
default Pin-Priority for anything from rpios to 1 (effectively
"never use") and override that to 990 for `ffmpeg` and the libav*
glob. Net effect: every package on the system continues to come
from Debian's repos by default, and only the explicit ffmpeg +
libav* family is allowed to come from rpios.

amd64 builds skip the rpios apt source entirely. There's no rpivid
hardware on x86, so the v4l2-request patches are irrelevant. amd64
ffmpeg stays from Debian.

### Pinned versions (rc2.0 baseline)

Debian Bookworm:
- python3=3.11.2-1+b1
- python3-pip=23.0.1+dfsg-1+deb12u1
- nmap=7.93+dfsg1-1
- net-tools=2.10-0.1+deb12u2
- iproute2=6.1.0-3
- ffmpeg=7:5.1.8-0+deb12u1 (amd64 only)

Raspberry Pi OS (aarch64 only):
- ffmpeg=8:5.1.3-1+rpt4 (transitively pins libavcodec59,
  libavformat59, libavfilter8, libavdevice59, libavutil57,
  libswscale6, libswresample4, libpostproc56 via the rpios
  ffmpeg package's own =-pinned Depends declarations)

PyPI:
- aiohttp==3.13.5
- cryptography==48.0.0

If any of these versions has rotated out of its source repo by
build time, apt or pip will fail loudly. The pinning contract is
"known versions, fail-loud on drift" — we bump in a follow-up rc.

### Base image NOT yet SHA-pinned

`build.yaml` still references the HA base image by tag
(`ghcr.io/home-assistant/aarch64-base-debian:bookworm` and the
amd64 equivalent), not by SHA digest. SHA-pinning requires
capturing the current digest from a build manifest, which is
deferred to 2.6.0-rc2.1 once we have a successful first build of
2.6.0-rc2.0 to pull the digest from. Tag-pinning still gets us
the right OS and apt source list; it just doesn't lock the image
identity at the registry layer.

### Reverted from 2.6.0-rc1.0

- Multi-stage rpi-ffmpeg compile-from-source — gone, replaced by
  apt source.
- run.sh PATH-shadow of `/opt/rpi-ffmpeg/bin` — gone, no longer
  needed since rpios ffmpeg installs to /usr/bin/ffmpeg via apt
  exactly like Debian's would.

### Carried over from 2.6.0-rc1.0

The camera_discovery.py code changes from 2.6.0-rc1.0 stay in
place — they're correct and architecture-neutral:

- `_HW_DECODER_CANDIDATES` list extended with `hevc_v4l2request`
  and `h264_v4l2request` ahead of the v4l2m2m entries.
- `_probe_hw_decoders` static-check probe path for v4l2request
  decoders (verifies `ffmpeg -decoders` lists the name and rpivid
  is loaded at /dev/video19 + /dev/media0).
- Live MJPEG endpoint hw selection iterates `_HW_DECODER_CANDIDATES`
  in preference order instead of hardcoding v4l2m2m.
- `probe_stream_details` auto-disable list extended to cover
  v4l2request decoders.
- Pi-4-aware diagnostic message updated.

### Carried over from 2.5.0

All of the 2.5.0 cycle (Lorex/Dahua DVR-NVR family support,
poll-until-done channel-enum, credential leak fix in
`_validate_rtsp_urls_single_socket`) ships unchanged.

### Known risks (rc2.0 — first attempt at apt-pinned multi-source build)

- **Pinned versions may have rotated.** Debian point-releases bump
  package versions; the +rpt4 suffix on rpios ffmpeg may have
  advanced to +rpt5 or beyond. If any pin fails at build time, we
  bump to current in 2.6.0-rc2.1. (No reproduction-log loop here —
  the addon log will show the apt failure cleanly on first install.)
- **archive.raspberrypi.com signing key URL.** The Dockerfile
  fetches the key from `https://archive.raspberrypi.com/debian/
  raspberrypi.gpg.key`. If rpios rotates the key URL, the curl step
  fails and the build aborts before any package gets installed.
- **First-time installation cost.** Adding rpios as an apt source
  triggers a one-time ~15-second apt-update + signing-key download
  on the first build of this version. Subsequent builds use Docker
  layer cache.



### What changed

**Dockerfile.** Now conditionally compiles jc-kynesim/rpi-ffmpeg
release/6.1 from source on aarch64 builds, installing the result at
`/opt/rpi-ffmpeg/`. Configure flags include `--enable-v4l2-request
--enable-libdrm --enable-libudev`, which give ffmpeg the
v4l2-request stateless API support that rpivid (Pi 4 / Pi 5 HEVC
silicon at `/dev/video19`) requires. Build-toolchain (build-essential,
pkg-config, libdrm-dev, libudev-dev, nasm, yasm, git) is installed
inside the same RUN layer as the compile, then the source tree is
cleaned but the toolchain stays — image-size optimization deferred
until the build proves stable. amd64 builds skip the entire compile
block and use system /usr/bin/ffmpeg unchanged (no rpivid hardware
on x86 to drive).

**run.sh.** Prepends `/opt/rpi-ffmpeg/bin` to PATH so plain
`ffmpeg` invocations across camera_discovery.py — _probe_hw_decoders,
snap_loop, the live MJPEG endpoint, probe_stream_details, snap_url
fallback paths — pick up the bundled binary automatically on aarch64.
On amd64 the directory doesn't exist and PATH lookup falls through
to /usr/bin/ffmpeg unchanged, so this export is a no-op on x86. No
Python-side branching needed.

**_HW_DECODER_CANDIDATES** (already added in earlier 2.6.0-rc1.0
work, listed here for completeness). Two new candidates ahead of the
existing v4l2m2m entries:

- `hevc_v4l2request` (codec=hevc) — primary HEVC HW decode path on
  Pi 4 / Pi 5 with rpivid loaded.
- `h264_v4l2request` (codec=h264) — H264 alternative; less critical
  since `h264_v4l2m2m` already works on Pi 4 via bcm2835-codec, but
  including it lets the probe pick up systems where v4l2request is
  the cleaner path.

Order is preference: v4l2request first, then v4l2m2m, then vaapi.
snap_loop and the live MJPEG endpoint both iterate this list and
pick the first match for the stream codec that isn't in
`_HW_UNAVAILABLE`.

**Live MJPEG endpoint hw selection.** Was hardcoded to
`hevc_v4l2m2m` / `h264_v4l2m2m`, which on Pi 4 always fell through
to software for HEVC because `hevc_v4l2m2m` is permanently in
`_HW_UNAVAILABLE` there. Now iterates `_HW_DECODER_CANDIDATES` in
preference order (matching snap_loop's selection at line ~7028), so
when rpivid is loaded and rpi-ffmpeg is available, HEVC live view
uses `hevc_v4l2request`.

**probe_stream_details auto-disable.** Runtime "Could not find a
valid device" detection now covers the v4l2request decoders too,
not just v4l2m2m and vaapi. Without this, a failed
`hevc_v4l2request` decode would leave the decoder out of
`_HW_UNAVAILABLE` and snap_loop would retry it on every snapshot.

**_probe_hw_decoders** (already added in earlier 2.6.0-rc1.0 work,
listed here for completeness). v4l2request decoders use a static-
check probe path: `ffmpeg -decoders` lists the decoder name AND
`/dev/video19` + `/dev/media0` are present (rpivid loaded). Both
true → decoder marked available. The old synthetic-decode probe used
for v4l2m2m / vaapi candidates would mis-fail here because v4l2-
request decoders are picky about input format (NAL alignment,
parameter set placement) and a libx265-encoded 16x16 test clip
trips them in ways that don't reflect real-world stream decoding.

**Pi-4 diagnostic refinement** (already added in earlier 2.6.0-rc1.0
work). The `hevc_v4l2m2m unavailable on Pi 4` message no longer
points at a future release ("Will be fixed in 2.6.0..."). It now
explains whether `hevc_v4l2request` was probed available alongside,
and if so, says HEVC HW decode is working via the v4l2request path.
If `hevc_v4l2request` was also unavailable, the message points at
the rpivid loading state and ffmpeg build, not at AnyCam.

### Known risks (rc1.0 — first attempt at compile-from-source)

- **Build time on Pi 4 host.** First `ha addon update` after this
  release will spend ~30-45 minutes compiling rpi-ffmpeg before the
  addon starts. Subsequent updates without Dockerfile changes will
  use Docker's layer cache and skip the recompile.
- **Dockerfile may fail on first build.** rpi-ffmpeg's configure
  occasionally finds a missing-pkg-config-package on minor base-
  image variants. If the first build fails, the addon log will
  show the configure error from inside the compile RUN; check there
  before assuming the issue is downstream.
- **Image bloat.** aarch64 image gains ~50MB binary plus ~150MB of
  build toolchain that we don't purge. amd64 image unchanged.
- **No live test yet.** CrystalHeeler hasn't run `ha addon update` to a
  build with this Dockerfile yet — the rpi-ffmpeg compile path is
  exercised here for the first time. Carry expectation that the
  next round of testing may reveal compile-side issues that take
  one or more rc bumps to resolve.

### Carried over from 2.5.0

All of the 2.5.0 cycle (Lorex/Dahua DVR-NVR family support,
poll-until-done channel-enum, credential leak fix in
`_validate_rtsp_urls_single_socket`) ships in 2.6.0-rc2.0 unchanged.


## 2.5.0

**Roll-up of the 2.5.0-rc1.0 → 2.5.0-rc1.9 cycle.** No code changes
beyond the version bump from 2.5.0-rc1.9. This entry summarizes the
shipped feature set for the 2.5.0 minor.

### Headline feature: Lorex/Dahua DVR-NVR family support

Cred-auth on a `channel_iterate` brand (Lorex DVR/NVR series, Dahua
direct, Amcrest rebrands) now spawns a background channel-enumeration
walk that registers each populated DVR channel as its own camera card
with its own per-channel snapshot URL
(`http://IP/cgi-bin/snapshot.cgi?channel=N`). Empty channel slots
(those returning 404 or 403 on DESCRIBE) are filtered out. On CrystalHeeler's
8-channel D861A8B-Z, all 8 populated channels surface as distinct
cards within ~6-9 seconds of clicking Connect, each with its own
live thumbnail.

### Cycle map

- **2.5.0-rc1.0 → 2.5.0-rc1.5** — initial channel-enumeration backend
  (validate-walker per-URL socket cycle, snap_url_template field on
  CAMERA_DB streaming_recipe, idempotent _DVR_ENUM_DONE guard, parent
  card renaming to `<brand> chN`).
- **2.5.0-rc1.6** — +8s loadCameras() refetch after cred-auth to
  surface enumerated cards in the UI without waiting for the next
  user-initiated state change.
- **2.5.0-rc1.7** — frontend `_stableCardKey` channel-suffix fix.
  Before this, all 8 DVR channel cards on the same ip:port collided
  on a single DOM card during in-place renderGrid updates. With this
  fix, each channel card gets a distinct `ip:port#chN` key.
- **2.5.0-rc1.8** — replace fixed +8s setTimeout with a poll-until-
  done loop against new `/api/dvr_enum/status/{camera_id}` endpoint.
  Backend hardening: every exit path of the channel-enum helper now
  adds to `_DVR_ENUM_DONE` so the JS poll loop terminates within
  bounded time regardless of outcome. Saved ~5 seconds of dead time.
- **2.5.0-rc1.9** — credential leak fix in
  `_validate_rtsp_urls_single_socket`. Five log emitters were
  interpolating creds-bearing RTSP URLs verbatim; all now use
  `_strip_creds()`.

### Carried over from 2.4.x

All 2.4.0 fixes remain in place: throttle-aware probe pacing,
aggressive cooldown detection, single-socket cred-auth refactor,
RST-class lockout safety nets, focus-view transport-flip + http_snap
fallback, fixed-position cards on login, dead JS cleanup, stable
stream profiles dedup.

## 2.5.0-rc1.9

**Security fix on 2.5.0-rc1.8.** Five log emitters in
`_validate_rtsp_urls_single_socket` were interpolating credential-
bearing RTSP URLs verbatim, leaking the username and password (both
raw and URL-encoded forms) into the addon log on every cred-auth
flow and every channel-enumeration walk. Fixed by wrapping each
interpolation with the existing `_strip_creds()` helper.

### Bug

CrystalHeeler's 2.5.0-rc1.8 test log shows the leak in two places:

- After credentials accepted on the Lorex, the `validate_rtsp_walk
  db_probe` lines logged the full creds-bearing URL when validating
  the sub-stream:
  `validating rtsp://admin:examplepass@192.168.50.217:554/...`
- The follow-up `validate_rtsp_walk channel-enum` walk for each of
  the 15 other channels logged the URL-encoded form (URL constructed
  via `quote()` for the channel-enum helper):
  `validating rtsp://admin:examplepass@192.168.50.217:554/...`

Both forms exposed creds. URL-encoding doesn't help — `%21` is just
the `!` character percent-encoded, trivially reversible.

### Fix

Five `_log(f"...{rtsp_url}...")` calls in
`_validate_rtsp_urls_single_socket` (Python lines 4891, 4950, 4981,
5016, 5088) now interpolate `_strip_creds(rtsp_url)` instead.
`_strip_creds` is the existing helper at line 3202:

    def _strip_creds(url: str) -> str:
        return re.sub(r"(://)[^@]+@", r"\1", url) if url else url

The regex matches `://` followed by anything-but-`@` followed by `@`,
replacing with bare `://`. Handles both raw (`admin:pass!`) and URL-
encoded (`admin:pass%21`) credential forms because the `[^@]+` class
doesn't care about the inner content — it just consumes everything
between `://` and `@`.

The five emitters:
- `(N/M) validating {rtsp_url}` (line 4891)
- `OPTIONS → 401 with auth challenge captured — falling through to
  DESCRIBE for {rtsp_url}` (line 4950)
- `DESCRIBE → 401 (no creds) — skipping {rtsp_url}` (line 4981)
- `DESCRIBE 200 but SDP has no m=video — skipping {rtsp_url}` (line
  5016)
- `→ probe_ok=True for {rtsp_url}` (line 5088)

Verification: a post-fix `grep -nE '_log\(f"[^"]*\{rtsp_url\}'`
returns zero matches in `_validate_rtsp_urls_single_socket`. The
sibling walker `_probe_rtsp_paths_single_socket` (line ~4335) was
already safe — it logs only the relative `{path}`, never a full URL.

### Scope

This fix only touches log output. No behavioral changes to RTSP
probing, validation, channel enumeration, or any other flow. The
fix is in five places that all live in the same function. Search
of the entire file confirms no other `_log` / `log.info` /
`log.debug` / `log.warning` calls in the codebase interpolate a
creds-bearing URL — `probe_rtsp_socket`'s `track_url` (line 3972)
derives from a creds-free `rtsp_url = f"rtsp://{host}:{port}{path}"`
and so doesn't need stripping.

### Acceptance

1. Repeat the 2.5.0-rc1.8 cred-auth flow on the Lorex.
2. Inspect the addon log for the `validate_rtsp_walk db_probe` and
   `validate_rtsp_walk channel-enum` lines after `Credentials accepted`.
3. URLs in those lines should appear as
   `rtsp://192.168.50.217:554/cam/realmonitor?channel=N&subtype=M`
   — no `admin:...@` segment present in any form.
4. All other 2.5.0-rc1.8 timing and behavior preserved (fast card
   surfacing, all 8 channels rendering, no idle-out at 30s).

## 2.5.0-rc1.8

**One-feature bumpfix on 2.5.0-rc1.7.** Replaces the fixed +8s
setTimeout(loadCameras) reload added in 2.5.0-rc1.6 with a poll-until-
done loop against a new lightweight backend status endpoint. On
CrystalHeeler's 7-channel Lorex in the 2.5.0-rc1.7 test log, channel
enumeration actually completed at +3.0s after cred-auth (10:51:16 →
10:51:19), but the UI waited the full 8s before refetching — costing
~5s of dead time before cards appeared. 2.5.0-rc1.8 makes the UI
react to actual completion via three coordinated changes.

### Changes

**1. New `/api/dvr_enum/status/{camera_id}` endpoint.** Lightweight GET
returning `{done: bool, populated_channels: [...]}`. `done` mirrors
`camera_id in _DVR_ENUM_DONE`. Pattern matches the existing
`/api/scan/status`, `/api/pscan/status`, `/snap/status` polling
endpoints — no new transport mechanism, no SSE/websocket
infrastructure added.

**2. Backend hardening: every exit path of
`_enumerate_dvr_channels_after_auth` now adds to `_DVR_ENUM_DONE`.**
Before 2.5.0-rc1.8, six early-return paths (camera deleted between
spawn and execution, brand identification miss, recipe type mismatch,
primary channel extraction failure, missing credentials, decrypt
failure, empty post-decrypt creds) bailed out without setting the
flag. With the new poll loop in the UI, any of those silent exits
would have caused the JS to poll forever (until its 12s hard cap).
Each early-return now calls `_DVR_ENUM_DONE.add(camera_id)` before
returning, making the contract explicit: after spawn, the camera_id
WILL appear in `_DVR_ENUM_DONE` within bounded time, regardless of
outcome. Six lines added.

**3. Frontend: `submitCreds()` poll loop replaces fixed setTimeout.**
After cred-auth response carries `dvr_enumeration_pending: true`, the
UI polls the new status endpoint every 500ms. On `done=true`, calls
`loadCameras()` once and exits. Hard-capped at 24 polls (12s, +50%
headroom over the old fixed budget). If the cap is hit without
`done=true`, falls through to `loadCameras()` anyway — worst case
matches 2.5.0-rc1.7 behavior, so no regression possible. Network
blips during polling are caught and silently retried until cap.

### Code delta
- `camera_discovery.py`:
  - `_enumerate_dvr_channels_after_auth`: +6 lines (one
    `_DVR_ENUM_DONE.add(camera_id)` before each of 6 early returns
    plus an explanatory comment on the first occurrence).
  - New `api_dvr_enum_status` async handler: +35 lines (most of which
    is docstring documenting the rationale).
  - New route registration: +1 line.
  - `submitCreds()` JS: replaces 14-line fixed-setTimeout block with
    35-line poll-loop block (most expansion is commentary; net
    behavioral diff is the polling pattern).
  - CURRENT_VERSION → 2.5.0-rc1.8.
- `config.yaml`: version → 2.5.0-rc1.8.
- `CHANGELOG.md`: this entry.

### Acceptance
1. Lorex fresh-install cred-auth: cards should appear ~4-5s
   sooner than 2.5.0-rc1.7 — log timestamps for `Channel enumeration
   complete` plus ~500ms polling latency should match the wallclock
   when the cards visibly appear in the UI.
2. All 8 cards continue producing frames — none idle out at 30s
   (2.5.0-rc1.7 fix preserved).
3. Non-DVR cards (Microseven, generic ONVIF): polling loop never
   starts because `dvr_enumeration_pending=false` — behaviour
   identical to 2.5.0-rc1.7.
4. Hard-cap fallback path: if for any reason `done` never flips true
   within 12s, `loadCameras()` still fires once at +12s, matching
   2.5.0-rc1.7's worst-case behaviour.

### Validation procedure
1. Install 2.5.0-rc1.8 on the Pi.
2. Click into Lorex card, enter admin credentials, click Connect.
3. In the addon log, find the `Credentials accepted for ...` line
   and the `Channel enumeration complete for ...` line. Note the
   delta — should be ~3s on this network.
4. Watch the UI: cards should appear within ~500ms of the `Channel
   enumeration complete` log line (one polling interval).
5. Total wallclock from clicking Connect to seeing all 8 cards
   should be ~6-9s (vs. ~13-15s under 2.5.0-rc1.7).
6. Verify all 8 cards continue thumbnail polling without idle-out at
   30s (2.5.0-rc1.7 stable_key fix still working).

## 2.5.0-rc1.7

**One-fix bumpfix on 2.5.0-rc1.6.** Fixes a frontend renderGrid
collision bug exposed by the rc1.6 +8s loadCameras() reload that
shipped in rc1.6: all 8 DVR channel cards on the same ip:port
collapsed into a single DOM card showing whichever channel was last
in the iteration order, with the others ghosting in CAMERAS but
never rendered.

### What 2.5.0-rc1.6 confirmed working

- rpi-ffmpeg version-promise string update — log line at startup
  reads `Will be fixed in 2.6.0 by bundling rpi-ffmpeg.`
- Channel enumeration backend still works exactly as in rc1.5 — all
  7 channels probed cleanly via per-URL walker, all 7 snap_loops
  kicked off via the rc1.5 fix, all 8 cards in CAMERAS by t+3s
  after cred-auth.

### What was still broken

Field log shows the rc1.6 +8s reload firing as designed, but the
result on screen is a single Lorex card showing channel 8's feed
plus the homeassistant card — not 8 Lorex cards. The log's snap-
loop activity confirms it:

```
08:39:55 [INFO] SNAP [192.168.50.217_554_ch{2..8}]: kicking off ...
08:39:55 [INFO] SNAP [192.168.50.217_554_ch{2..8}]: http starting → ...
08:40:26 [INFO] SNAP [192.168.50.217_554_ch{2..7}]: idle 31s — stopping
08:40:30 [INFO] SNAP [192.168.50.217_554]: idle 30s — stopping
08:40:58 [DEBUG] SNAP [192.168.50.217_554_ch8]: http frame 50 — 7712 bytes
```

Only ch8 keeps producing frames. Parent + ch2-ch7 idle out at 30s
because the UI never accesses them. ch8 is the lone Lorex card the
UI is rendering and polling.

### Root cause: stable_key collision in renderGrid

`_stableCardKey(cam)` returned `cam.ip + ':' + cam.port`, which is
identical for all 8 DVR channel cards (they share the same
ip:port). The renderGrid update path uses this key to find the
existing DOM card across re-renders so login-induced ID changes
don't reset card position (the rc3.3 fix). With 8 cards collapsing
onto one key:

1. Pre-cred-auth render: 1 DOM card (parent only).
2. Cred-auth + enumeration: 7 new cards added to CAMERAS, parent +
   ch2-ch8 = 8 entries.
3. +8s reload calls renderGrid with 8 cameras, all sharing one
   stable_key.
4. cardsByKey is built once at the top of renderGrid and contains a
   single entry pointing to the existing parent DOM card.
5. Iteration 1 (parent): cardsByKey.get(key) → existing card.
   Update in place.
6. Iteration 2 (ch2): cardsByKey.get(key) → SAME card (the map
   isn't updated mid-loop). Update overrides with ch2's HTML.
7. Iterations 3-8: each overwrites the same DOM card with its own
   HTML.
8. Final: ONE DOM card with ch8's HTML (last iteration wins). The
   new-card-append branch is never taken because the cardsByKey
   lookup always hits.

Result: only ch8's [data-snap] element exists in the DOM, only ch8
is polled by initSnaps, only ch8 stays alive. The parent and
ch2-ch7 idle out at 30s without ever being visible.

This also explains the earlier rc1.5 observation CrystalHeeler reported as
"went back in and it was displaying all the feeds" — a hard reload
starts with an empty cardsByKey, and even with the key collision
every iteration falls through to the new-card-append branch
because the lookup returns null on an empty map. So a fresh page
load works; the bug only manifests on in-place updates after the
DOM already has a card on the colliding key.

### Fix

`_stableCardKey` now appends a `#chN` suffix when `cam.channel` is
set:

```js
if (cam.ip) {
  let key = cam.ip + ':' + (cam.port || '');
  if (cam.channel) key += '#ch' + cam.channel;
  return key;
}
```

Each DVR channel card gets a distinct key (`192.168.50.217:554#ch1`,
`...#ch2`, …, `...#ch8`). Non-DVR cards have no `channel` field, so
they keep plain `ip:port` — the rc3.3 fixed-position-on-login
behaviour is preserved unchanged.

### First-render-after-cred-auth edge case

The parent card's first render (post-cred-auth, pre-enum) has no
`channel` field, so its stable_key is `ip:port`. After the +8s
reload, the parent now has `channel = primary_ch` set by the
enumeration helper, so its stable_key becomes `ip:port#chN`. These
don't match. But the renderGrid lookup falls back to cardsById
(matching by exact `cam.id`):

```js
let card = cardsByKey.get(key) || cardsById.get(cam.id);
```

The parent's `cam.id` (`192.168.50.217_554`) is unchanged across the
two renders, so cardsById finds it. Update in place. New ch2-ch8
cards have unique cardsByKey entries (none in DOM yet) and unique
cam.ids (none in DOM yet) — both maps miss → new-card-append
branch hits → all 7 appended.

End state: 8 distinct DOM cards, all with distinct stable_keys, all
with [data-snap] elements, all polled by initSnaps.

### Acceptance
1. Lorex fresh-install cred-auth: 8 distinct cards visible in
   the UI within ~6-10s of clicking Save Credentials, each with its
   own thumbnail.
2. All 8 cards continue producing frames — none idle out at the 30s
   mark.
3. Non-DVR cards (Microseven, generic ONVIF) still position-stable
   across login-induced ID changes (rc3.3 behaviour preserved).

## 2.5.0-rc1.6

**Three-fix bumpfix on 2.5.0-rc1.5.** Targets the UI lag observed in
the 2.5.0-rc1.5 field log between cred-auth completing and the new
DVR channel cards becoming visible in the grid, plus updates the
rpi-ffmpeg version-promise strings now that work has shifted to
2.6.0. Also reverifies channel enumeration is still firing
correctly (2.5.0-rc1.5 fix holding).

### What 2.5.0-rc1.5 confirmed working

- All 7 channel-enum snap_loops kicked off correctly:
  `SNAP [192.168.50.217_554_chN]: kicking off initial thumbnail loop
  after channel enumeration` for ch2 through ch8.
- Each card immediately started polling its per-channel snapshot URL
  (`http://192.168.50.217/cgi-bin/snapshot.cgi?channel=N`).
- 8 cards rendered correctly in the UI for the Lorex DVR once the UI
  caught up.

### What was still wrong: UI didn't catch up promptly

Field log timing:
- 00:20:42 — `Credentials accepted` (cred-auth POST returns 200)
- 00:20:46 — Channel enum completes, 7 new cards in CAMERAS, all
  snap_loops started
- 00:21:16 — All 7 new snap_loops idle out (UI hasn't requested
  thumbnails yet — it doesn't know the cards exist)
- 00:22:06 — Periodic scan completes (~80s later); its onComplete
  side-effect happens to call `loadCameras()` and the UI finally
  surfaces the new cards

Root cause is in the UI cred-auth flow at the embedded JS:
```js
if (r.ok) { e.classList.remove('visible'); await loadCameras(); }
```

The post-cred-auth `loadCameras()` runs immediately, but the
channel enumeration is a fire-and-forget background task that hasn't
completed yet. There's no periodic `/api/cameras` poll, so the UI
remains stale until the next user-initiated state change (or, as
in the field log, an unrelated scan happens to complete and trigger
a refresh side-effect).

### Fix 1 — Backend signals enumeration-pending on cred-auth response

`api_set_credentials` now checks the brand's streaming_recipe type
after kicking off the enumeration task. If the brand is
`channel_iterate`, the response includes `dvr_enumeration_pending:
true`. Non-channel-iterate brands get `false`. Other response fields
unchanged.

### Fix 2 — UI schedules delayed refetch when flag set

The cred-auth success handler now reads the new flag. When set, it
schedules a second `loadCameras()` 8 seconds after the immediate one.
8 seconds covers the worst-case enumeration wallclock budget (15
URLs × ~300ms walker time + 100ms politeness sleeps + snap_loop
kickoffs) with comfortable headroom. Non-DVR cred-auth flows skip
the extra fetch entirely so there's no behaviour change for them.

### Fix 3 — Update rpi-ffmpeg version-promise strings to 2.6.0

The "Will be fixed in 2.5.0 by bundling rpi-ffmpeg" diagnostic
message and its two preceding code comments referenced 2.5.0 but
the rpi-ffmpeg bundling work has been shifted to 2.6.0 so the
addon's own messaging would have been stale at 2.5.0 ship. Updated
all three references (one user-visible string, two dev comments).

### Out of scope for this rc

The delete-card lag CrystalHeeler mentioned ("they didn't disappear until I
left the UI and came back") couldn't be reproduced from the rc1.5
log alone. The `deleteCamera()` JS function already filters local
state and re-renders, so a delete should be instant. If it persists
in rc1.6 testing, send a log of the delete actions and we'll
diagnose separately.

### Acceptance
1. Lorex fresh-install cred-auth: cards begin appearing within
   ~6–10 seconds of clicking Save Credentials, no manual refresh
   needed.
2. Non-DVR cred-auth (e.g. Microseven, generic ONVIF): no behaviour
   change. Single card surfaces immediately as before.
3. Hardware-decoder log line on Pi 4 reads `Will be fixed in 2.6.0
   by bundling rpi-ffmpeg.` (not 2.5.0).
4. Channel enumeration still works as it did in 2.5.0-rc1.5 (no
   regression on the per-URL walker, snap_loop kickoff, or
   populated_channel_test).

## 2.5.0-rc1.5

**One-fix bumpfix on 2.5.0-rc1.4.** Newly-registered cards from the
channel enumeration helper now explicitly kick off their own
snap_loops at registration time, so they appear in the UI with
working thumbnails immediately.

### What 2.5.0-rc1.4 confirmed working

The 2.5.0-rc1.4 field log was a near-complete success:

- Per-URL socket invocation worked perfectly. Each of the 15 channel
  probes ran on its own fresh TCP socket. No mid-walk RST bails.
- All 8 physical channels (1–8) responded with probe_ok=True. SDP-
  has-video-track populated_channel_test passed all of them.
- Channels 9–14 returned RTSP 403 Forbidden, channels 15–16 RTSP 404
  Not Found — correctly filtered out before card registration.
- ACD escalation didn't fire on the Lorex DVR during enumeration (Fix 2
  from rc1.4 holding).
- Final enumeration log: `7 new card(s) registered (populated
  channels: ['1', '2', '3', '4', '5', '6', '7', '8'])`.

### What was still broken

Despite 7 new cards being correctly registered in CAMERAS and saved
to disk, the UI didn't show them. The field log diagnoses this:
after the `Channel enumeration complete` line at 00:07:13, only the
parent's snap_loop continues running. No `SNAP [192.168.50.217_554_
chN]: starting background process` lines fire for any of the new
cards. By contrast, when 2.5.0-rc1.3's saved ch2 card was loaded
during post-upgrade verification at 00:03:38, it kicked off its own
snap_loop immediately — that's the path that makes a card visible.

The root cause is architectural: the snap_loop kickoff in
`handle_snapshot` (line 7916) only runs when the UI requests a
thumbnail. For cards loaded from disk on startup, the verification
scan triggers thumbnail requests across the saved set, which
implicitly starts each card's snap_loop. For cards registered mid-
session via channel enumeration, no such trigger fires — the cards
exist in `CAMERAS` and surface in `/api/cameras`, but their snap
loops never start, and the UI's render pipeline (which expects
thumbnails to be available before treating a card as "live") shows
nothing.

### Fix

After registering each new card in `_enumerate_dvr_channels_after_
auth`, explicitly kick off the snap_loop using the same pattern
`handle_snapshot` uses:

```python
authed_url = build_authenticated_url(new_cam)
if authed_url:
    _snap_last_access[new_id] = time.monotonic()
    state = _snap_state(new_id)
    if state.get("task") is None or state["task"].done():
        log.info(f"  SNAP [{new_id}]: kicking off initial thumbnail "
                 f"loop after channel enumeration")
        state["task"] = asyncio.create_task(
            snap_loop(new_id, authed_url, new_cam))
```

Wrapped in try/except so a snap-loop kickoff failure on one card
doesn't block enumeration of the others. ~12 LoC inside the
existing for-loop.

### Acceptance
1. Lorex D861A8B-Z: cred-auth → channel enumeration walks 15
   channels per-URL, registers populated ones (rc1.4 behaviour).
2. Each registered card immediately logs `SNAP [192.168.50.217_554_
   chN]: kicking off initial thumbnail loop after channel
   enumeration` followed by `SNAP [...]: http starting → http://
   192.168.50.217/cgi-bin/snapshot.cgi?channel=N`.
3. UI shows N+1 cards (parent + N new) within seconds of cred-auth
   complete, each with its own thumbnail polling its own per-channel
   snapshot URL.

## 2.5.0-rc1.4

**Two-fix bumpfix on 2.5.0-rc1.3.** Fixes a Lorex/Dahua firmware
behavior the 2.5.0-rc1.3 field log exposed: the DVR closes the TCP
socket after each full authenticated RTSP transaction, breaking the
single-socket-multi-URL pattern the channel enumeration helper was
using.

### What the 2.5.0-rc1.3 log showed
```
23:55:21 [INFO]   Channel enumeration starting for 192.168.50.217_554:
                  walking 15 other channel paths
23:55:21 [INFO]   [validate_rtsp_walk channel-enum:...] (1/15) ...channel=2&subtype=0
23:55:21 [INFO]   [validate_rtsp_walk channel-enum:...] SETUP (TCP) → 200 OK
23:55:21 [INFO]   [validate_rtsp_walk channel-enum:...]   → probe_ok=True
23:55:21 [INFO]   [validate_rtsp_walk channel-enum:...] (2/15) validating ...channel=3&subtype=0
23:55:25 [INFO]   [validate_rtsp_walk channel-enum:...] OPTIONS exception
                  → bailing remaining: [Errno 104] Connection reset by peer
23:55:25 [WARNING]  ACD: 192.168.50.217 produced 2 RST/broken-pipe events
                    in <60s — escalating per-IP cooldown to 30s for 300s
23:55:25 [INFO]   Channel enumeration complete: 1 new card(s) registered
                  (populated channels: ['1', '2'])
```

Channel 2 succeeded (probe_ok=True). On the very next URL (channel=3)
the validate walker got `Connection reset by peer` on OPTIONS and
bailed all remaining 13 URLs. Then ACD escalated cooldown.

### Root cause: Lorex/Dahua firmware behavior
The Lorex DVR closes the TCP socket after each completed
authenticated RTSP transaction (OPTIONS+DESCRIBE+SETUP+TEARDOWN).
Verified by the same log: the unauthenticated discovery walker at
23:49:55 walked all 57 paths in a single socket (each path got just
OPTIONS-401-skip; no full transactions, socket stayed alive). It was
only the authenticated transactions during the channel enumeration
that triggered the per-URL socket close.

This is a firmware quirk specific to this brand; Hikvision, Axis,
generic ONVIF, and Hipcam-family cameras all hold the socket open
across multiple authenticated transactions, which is why the single-
socket-multi-URL validate walker has worked for them since rc2.x.

### Fix 1 — Per-URL socket invocation in channel enumeration helper

The channel enumeration helper now loops over candidate URLs and
calls `_validate_rtsp_urls_single_socket` once per URL with a single-
element list. Each call gets a fresh TCP socket. ~300ms per URL × 15
URLs ≈ 5s total wallclock — acceptable for a background task that
runs after cred-auth has already returned to the user. Adds a
`asyncio.sleep(0.1)` between URLs as a politeness gap for the DVR's
RTSP subsystem to finish server-side socket cleanup before the next
OPTIONS connects.

### Fix 2 — Honor `walker_skip_acd` flag in validate walker

`_validate_rtsp_urls_single_socket` now checks `host_meta["walker_skip_acd"]`
before recording RST events to the ACD (Aggressive Cooldown
Detection) escalation system. The channel enumeration helper passes
this flag so per-URL walks that complete successfully but happen to
race with a server-side socket close don't pollute ACD with
spurious RST events. Other walker callers (discovery, db_probe)
default to ACD-recording behavior — unchanged.

Without this fix, fix 1 alone would have caused ACD to escalate
cooldown after every 2 URLs (since every URL hits a server close on
the OPTIONS for the next URL), which would in turn slow down all
other probes against this IP.

### Acceptance
1. Lorex D861A8B-Z at 192.168.50.217: cred-auth succeeds on channel 1
   as before. Within ~5–8s after `Credentials accepted`:
   `Channel enumeration starting ... walking 15 other channel paths`
   followed by per-URL probes labeled `channel-enum:...(N/15)`.
2. Each per-URL walker call should complete its OPTIONS+DESCRIBE+
   SETUP+TEARDOWN in ~200–400ms. probe_ok=True on each populated
   channel.
3. Empty/unconnected channels filtered by populated_channel_test
   should log `DESCRIBE 200 but SDP failed populated-channel test`
   or DESCRIBE 404 (DVRs that don't allocate the slot at all).
4. No ACD escalation on the Lorex DVR from these per-URL walks.
5. Final log: `Channel enumeration complete: M new card(s)
   registered (populated channels: ['1', ...])` where M is the
   number of populated channels minus the parent (channel 1).
6. UI should show M+1 cards for the Lorex DVR.

### Validation procedure
1. Confirm the Lorex DVR is not in lockout (power-cycle if needed).
2. Install 2.5.0-rc1.4 (full restart, not soft reload).
3. Click into the Lorex card, enter correct admin credentials.
4. Watch the log for the per-URL channel-enum probes.
5. Compare populated channel list against your physical camera
   layout (CrystalHeeler's layout intentionally not shared per his request).

## 2.5.0-rc1.3

**One-line bumpfix on 2.5.0-rc1.2.** Fixes the `AttributeError` crash
in the channel-enumeration helper that silently failed in the fire-
and-forget task in 2.5.0-rc1.2.

### The bug

2.5.0-rc1.2 field log:
```
23:13:15 [ERROR] Task exception was never retrieved
future: <Task finished name='Task-296' coro=<_enumerate_dvr_channels_after_auth() ...>
exception=AttributeError("'str' object has no attribute 'get'")>
Traceback (most recent call last):
  File "/camera_discovery.py", line 8807, in _enumerate_dvr_channels_after_auth
    username = creds.get("username", "")
               ^^^^^^^^^
AttributeError: 'str' object has no attribute 'get'
```

I assumed `cam["credentials"]` was a dict shaped like `{"username":
..., "password": ...}`. It isn't — it's a Fernet-encrypted JSON
string. The codebase's pattern (see line ~8690 in `_validate_rtsp_walk`)
is `username, password = decrypt_creds(creds)`. The bug manifested
silently in the fire-and-forget task: cred-auth itself still
succeeded (single card surfaced as expected), but no enumeration
ever ran, so the user saw the same single-card behavior 2.5.0-rc1.0
shipped with.

Lesson recorded for myself: when introducing new code that reads
existing camera-record fields, search for at least one prior consumer
of the same field to confirm the shape before assuming. Per the
standing regression rule, the bug was in our code first — and in
this case our brand-new code, so the field where it manifested
(silent task failure) was the only data point I needed.

### Fix

Replace `creds.get(...)` with `decrypt_creds(creds)` and handle
decrypt failure with a debug log + early return. ~5 LoC change in
`_enumerate_dvr_channels_after_auth`.

### Confirmed by 2.5.0-rc1.2 log
- The OPTIONS-401 fall-through fix in the validate walker (Fix 1
  from rc1.2) is working — log shows `OPTIONS → 401 with auth
  challenge captured — falling through to DESCRIBE` followed by
  `DESCRIBE → 'RTSP/1.0 404 Not Found' — skipping` (the 404 is
  expected for `subtype=1` on a Lorex DVR — that variant doesn't
  exist on this firmware). So Fix 1 is now confirmed in the field.
- The enumeration TASK was successfully spawned from
  `api_set_credentials` — the traceback proves the spawn worked.
  Just the body of the task crashed.
- Idempotency guard (`_DVR_ENUM_DONE`) was never reached because
  the crash happened before that point.

### Validation procedure
1. Install 2.5.0-rc1.3.
2. Restart the addon to clear the in-memory `_DVR_ENUM_DONE` set
   (the rc1.2 crash didn't add the camera_id to the set, so the
   restart isn't strictly required — but cleaner test).
3. Click into the Lorex card, enter correct admin credentials.
4. Within ~10s after `Credentials accepted`, log should show:
   `Channel enumeration starting for 192.168.50.217_554: walking N
    other channel paths`,
   per-URL walker output, and finally
   `Channel enumeration complete for 192.168.50.217_554: M new
    card(s) registered (populated channels: ['1', '4', '5', ...])`.
5. UI should show M+1 cards for the Lorex DVR.

## 2.5.0-rc1.2

**Three-fix bumpfix on 2.5.0-rc1.1.** Two bug fixes plus the
architectural piece (multi-channel multi-card surfacing) that was
deferred from 2.5.0-rc1.0 and that the original 2026-05-02 plan called
for. After this build, a Lorex/Dahua DVR with N populated camera
channels surfaces as N separate camera cards instead of one.

### Fix 1 — OPTIONS-401 fall-through in `_validate_rtsp_urls_single_socket`

Same OPTIONS-401-handling bug that the discovery walker had until
2.5.0-rc1.1, now also lurking in the validate walker. The 2.5.0-rc1.1
field log proved it:
```
[validate_rtsp_walk db_probe:192.168.50.217] OPTIONS → 'RTSP/1.0 401 Unauthorized' — skipping URL
[validate_rtsp_walk db_probe:192.168.50.217] OPTIONS → 'RTSP/1.0 401 Unauthorized' — skipping URL
stream_profiles: built 1 entry/entries (1 main + 0 sub + 0 validated locked)
```
The two sub-stream URLs being validated were valid; the walker just
couldn't authenticate against them on OPTIONS and skipped. Same fix:
when OPTIONS returns 401 with WWW-Authenticate AND credentials are
present, capture the auth challenge into `auth_val` and fall through
to DESCRIBE so the existing 401-retry logic uses the captured nonce.
~10 LoC, mirror of the discovery walker fix. My oversight in 2.5.0-
rc1.1 — should have grep'd both walkers.

### Fix 2 — Multi-channel multi-card surfacing on channel_iterate brands

Architectural piece. After cred-auth succeeds on a brand whose
streaming_recipe is `channel_iterate`, a fire-and-forget background
task walks the remaining channels with the validated credentials and
registers each populated channel as its own camera card. Empty/
virtual slots get filtered by the populated_channel_test SDP heuristic
(`sdp_has_video_track`). The user gets cred-auth confirmation
immediately; additional cards appear over the next few seconds as
the enumeration completes.

Throttle safety: brands with `auth_attempt_lockout` (Lorex/Dahua DVR-
NVR family) only count FAILED auth attempts toward the lockout
counter. The credentials we use here have already been validated by
the cred-auth flow that called us, so walking remaining channels with
them is unconstrained — no risk of camera lockout from the
enumeration. The validate walker captures auth on the first 401 and
reuses the nonce per RFC 2617 across all URLs in a single TCP socket.

Card identity: new cards use ID format `{ip}_{port}_ch{N}`. The
parent card is renamed to include `chN` to match. Per-channel
snapshot URLs are built from a new optional `snap_url_template` field
on the streaming_recipe (added to the Lorex/Dahua entry as
`http://{ip}/cgi-bin/snapshot.cgi?channel={ch}` — the Dahua snapshot
endpoint accepts `?channel=N` and returns that physical channel's
still image).

Idempotency: a `_DVR_ENUM_DONE` set guards against re-enumerating
the same DVR if cred-auth is clicked again.

### Fix 3 — populated-channel SDP heuristic in `_validate_rtsp_urls_single_socket`

Companion to Fix 2. The validate walker now honors
`walker_populated_channel_test` on host_meta — same hook the
discovery walker has had since 2.5.0-rc1.0. Without this, the channel-
enumeration helper would register cards for empty DVR slots that
return a 200 OK SDP with `m=video` but no real codec rtpmap.

### Helpers added
- `_extract_channel_from_rtsp_url(url)` — pulls `channel=N` query
  value out of a Dahua-format RTSP URL. Tolerant of `?` vs `&`
  separator and case.
- `_enumerate_dvr_channels_after_auth(camera_id)` — async helper
  spawned from `api_set_credentials`. Walks remaining channels,
  registers populated ones as new cards.
- `_DVR_ENUM_DONE` set — module-level idempotency guard.

### CAMERA_DB update
Lorex/Dahua DVR-NVR Family entry's `streaming_recipe` gains
`snap_url_template: "http://{ip}/cgi-bin/snapshot.cgi?channel={ch}"`.

### Acceptance
1. Lorex D861A8B-Z at 192.168.50.217: cred-auth succeeds on channel
   1 (or whichever channel returns first 200 OK after auth). Within
   ~10s after `Credentials accepted`, additional cards appear for
   each other populated channel. Empty channels do not get cards.
2. Each card's thumbnail polls its own `/cgi-bin/snapshot.cgi?channel=N`
   URL.
3. Each card's RTSP stream URL is the channel-specific
   `/cam/realmonitor?channel=N&subtype=0`.
4. Non-channel_iterate brands (Hikvision, Hipcam, etc.): unchanged.
5. Re-clicking save credentials on an already-enumerated DVR is a
   no-op.

### Validation procedure
1. Power-cycle the Lorex DVR if it's in lockout. Confirm RTSP works in
   VLC against the camera directly.
2. Install 2.5.0-rc1.2.
3. Click into the Lorex card, enter correct admin credentials.
4. `Credentials accepted` log line should fire within ~10s.
5. Within another ~10s, log should show:
   `Channel enumeration starting for 192.168.50.217_554: walking N
    other channel paths (brand=Lorex / Dahua DVR-NVR Family)`,
   followed by per-URL validate walker output, and finally
   `Channel enumeration complete for 192.168.50.217_554: M new
    card(s) registered (populated channels: ['1', '4', '5', ...])`.
6. UI should show M+1 cards for the Lorex DVR, one per populated
   channel.

## 2.5.0-rc1.1

**Bumpfix on 2.5.0-rc1.0.** Fixes the discovery-walker bug that left
the Lorex D861A8B-Z (and any DVR/NVR firmware that enforces auth on
RTSP OPTIONS itself) silently failing cred-auth even with correct
credentials. Plus a tighten on `_expand_channel_iterate_paths` to
filter unexpanded `{...}` placeholders out of the probe list.

### The bug

Field test of 2.5.0-rc1.0 against the Lorex DVR (2026-05-07 10:00 log)
showed every Lorex DVR cred-auth attempt walking all 59 paths and
returning `OPTIONS → 'RTSP/1.0 401 Unauthorized' — skipping path` for
every single one. RTSP result: None. Credential attempt FAILED.

Root cause is in `_probe_rtsp_paths_single_socket` at line 4274. When
OPTIONS returns any non-2xx response, the walker logs "skipping path"
and `continue`s to the next path — without considering that 401 with
`WWW-Authenticate` means "this path exists, it just needs auth." The
walker has full Digest-auth retry logic but it lives on the DESCRIBE
step, which the walker never reaches because OPTIONS already shoved
it to `continue`.

This bug has been latent since the walker existed. Hikvision-style
firmware (which allows OPTIONS without auth and only enforces auth on
DESCRIBE) avoided it. Hipcam-family avoided it too because cred-auth
goes through ONVIF, not this walker. Lorex/Dahua DVR-NVR family with
ONVIF disabled (the Lorex DVR's configuration) hits the bug head-on.

### Fix 1: OPTIONS-401 fall-through to DESCRIBE

When OPTIONS returns 401 with WWW-Authenticate AND the walker has
credentials, capture the auth challenge into `auth_val` and DON'T
skip the path. Fall through to DESCRIBE. DESCRIBE will also 401
(same auth requirement), and the existing DESCRIBE-401-retry-with-
Digest path then runs to completion: builds Digest auth header from
captured `auth_val`, retries DESCRIBE with Authorization, gets 200
OK, parses SDP, finds m=video track, returns the working URL.

When OPTIONS returns 401 with NO WWW-Authenticate header (rare —
malformed firmware), or the walker has no credentials, the existing
"skip path" behavior is preserved. ~10 LoC, contained to the OPTIONS
handler.

### Fix 2: filter unexpanded `{...}` placeholders

`_expand_channel_iterate_paths` was emitting recipe `fallback_paths`
verbatim. Some fallback_paths in CAMERA_DB use `{ch}`/`{st}`
placeholders for channel/subtype substitution — for example
`/h264/ch{ch}/main/av_stream` (legacy Dahua firmware) and
`/live/ch{ch}/main` (very old firmware). The helper was emitting
these as literal strings, and the walker was probing the literal
`/h264/ch{ch}/main/av_stream` against the camera, which the camera
responds 401 to (path can never match). Adds a filter at the end of
expansion: any path containing `{` or `}` is dropped from the
result. ~3 LoC. Future improvement (out of scope here): properly
expand placeholders in fallback_paths the same way path_template is
expanded, so the legacy-firmware fallbacks get real channel coverage.

### Acceptance
1. Lorex D861A8B-Z at 192.168.50.217 cred-auth with correct credentials:
   walker should find a working URL on the first populated channel.
   Log shows: `OPTIONS → 401 with auth challenge captured — falling
   through to DESCRIBE for /cam/realmonitor?channel=N&subtype=0`,
   then `DESCRIBE-auth → 200 OK`, then `RTSP OK (Layer 1)`.
2. Lorex D861A8B-Z with WRONG credentials: still bails on first
   auth-retry-rejection per 2.5.0-rc1.0's auth_attempt_lockout
   policy. One used attempt against the camera's 10-attempt
   counter.
3. Hikvision cred-auth: behavior unchanged (OPTIONS still
   returns 200 OK on Hikvision, falls through to DESCRIBE-401-retry,
   same path as before).
4. Hipcam cred-auth: behavior unchanged (goes through ONVIF,
   doesn't hit this walker path).
5. The 34 channel-iterate paths logged for the Lorex DVR should no longer
   include literal `/h264/ch{ch}/main/av_stream` or `/live/ch{ch}/main`
   entries. Total paths from a 16-channel recipe: 32 channel × 2
   subtype paths + however many fallback paths have NO placeholders.

### Validation procedure
1. Install 2.5.0-rc1.1.
2. Click into the Lorex card, enter correct admin credentials.
3. Cred-auth should complete in <10s.
4. Card should display a working stream from the first populated
   channel.
5. Scan log should NOT contain literal `{ch}` substrings in any
   probe_rtsp_walk path entries.

## 2.5.0-rc1.0

**First rc on the 2.5.0 line.** Lorex/Dahua DVR Family Support — the
consumer side of work that began in 2.4.0-rc2.0 (data scaffolding) and
the Plan-2 RTSP OPTIONS Fingerprint Helper. Closes the discovery gap
that left the Lorex D861A8B-Z stuck at "Verifying..." for 90s before
"Could not connect" on AnyCam 2.4.x.

### What was already in place before 2.5.0-rc1.0
- CAMERA_DB entry for "Lorex / Dahua DVR-NVR Family" (added 2.4.0-rc2.0)
  with full streaming_recipe (type=channel_iterate, channels 1..16,
  subtypes [0,1], fallback paths), throttle_type=auth_attempt_lockout,
  skip_layer2=True, rtsp_realm_regex matching the canonical Dahua
  realm pattern `^Login to [0-9a-f]{32}$`, default_ports covering 554,
  80, 35000, 37777, 443, 8000.
- Three sibling DVR entries also seeded with streaming_recipe (Hikvision
  NVR, Uniview NVR, Amcrest direct).
- _rtsp_options_fingerprint helper + brand-id rtsp_realm_regex matching.
- skip_layer2 enforcement in find_rtsp_path (already done at line 5104).

### What 2.5.0-rc1.0 adds (the consumer side)
- **streaming_recipe expansion**: find_rtsp_path now reads brand_entry
  for streaming_recipe.type == "channel_iterate" and prepends the
  expanded channel-iterated paths (16 channels × 2 subtypes + recipe
  fallback paths = 34 paths) to the universal RTSP_PATHS list. Walked
  through the existing single-socket walker — no new sockets opened.
- **_sdp_has_video_track populated-channel filter**: stricter SDP
  heuristic for DVR/NVR devices that return 200 OK with valid-looking
  SDP on every channel slot regardless of whether a physical camera
  is connected. Returns True only when SDP contains m=video AND at
  least one a=rtpmap mapping to a real video codec (H.264/H.265/HEVC/
  MPEG4). Wired into the walker via host_meta["walker_populated_channel
  _test"] so single-camera/IP-camera probes are unaffected.
- **auth_attempt_lockout policy**: brands with throttle_type=
  auth_attempt_lockout (Lorex/Dahua DVR-NVR family) now bail the entire
  walk on the FIRST DESCRIBE-with-auth rejection, rather than burning
  additional lockout-counter attempts on credentials we already know
  are wrong. Only fires when the walker is invoked WITH credentials AND
  brand-id has matched — discovery scans (no creds) are unaffected.
  Surfaces a clean "credentials wrong, 1 of 10 attempts used" failure
  to the user.

### Helpers added
- `_sdp_has_video_track(sdp_body)` — module-level
- `_expand_channel_iterate_paths(recipe, channel_cap=16)` — module-level
- `_SDP_VIDEO_CODEC_RE` regex — module-level

### Acceptance
1. Lorex D861A8B-Z at 192.168.50.217: cred-auth completes in <10s
   instead of 90s, returns the first populated channel as the working
   stream URL.
2. Lorex D861A8B-Z: card title displays "Lorex / Dahua DVR-NVR Family".
3. Lorex D861A8B-Z with wrong password: cred-auth fails in <2s with
   `walker_auth_lockout_bailed=True` flag set on host_meta (preserves
   the remaining 9 of 10 attempts before camera lockout).
4. Hikvision: behavior unchanged (different brand entry, no
   channel_iterate recipe matches its brand-id).
5. Hipcam: behavior unchanged (different throttle type
   `rate_limit_per_ip_tcp`, different code path).
6. Layer 2 not invoked on the Lorex DVR (already enforced at line 5104).

### Deferred to a follow-up release
- Multi-channel-multi-card surfacing. Currently a Lorex DVR with N
  populated channels surfaces as ONE camera card showing the FIRST
  populated channel's stream. Surfacing all N channels as N separate
  cards requires architectural changes to the scanner's per-IP-port
  card output and is outside this rc's scope.
- Empirical SDP shape data from a known-empty Lorex channel. User
  opted to ship the populated-channel heuristic blind on the rationale
  that we will always be guessing for untested hardware. If field test
  on the Lorex DVR surfaces phantom or missing cards, a 2.5.0-rc1.1 bumpfix
  refines either the regex or the m=video substring check.
- NVR channel ranges >16. Cap is hardcoded to 16 in this rc; per-entry
  override is a future enhancement.

### Validation procedure
1. With AnyCam 2.5.0-rc1.0 running and a Lorex D861A8B-Z (or any
   Dahua-family DVR) reachable, scan the network. The DVR's IP should
   surface as a card titled "Lorex / Dahua DVR-NVR Family".
2. The scan log should show:
   `RTSP path list: brand=Lorex / Dahua DVR-NVR Family streaming_recipe
    channel_iterate expanded to 34 paths (channels capped at 16)`.
3. Click into the card, enter correct credentials. Cred-auth should
   complete in <10s and return a working stream URL pointing at the
   first populated channel (e.g. /cam/realmonitor?channel=3&subtype=0
   if the camera is connected to channel 3).
4. Click into the card again, enter WRONG credentials. Cred-auth
   should fail in <2s with one used attempt against the camera's
   10-attempt lockout counter.

## 2.4.0

**Final release of the 2.4.0 line.** Code is identical to 2.4.0-rc4.0
(field-validated on the secondary network on 2026-05-07; deferred the Microseven
re-test was waived by user given clean primary-network behavior). All
rc entries below preserved for traceability.

### Headline themes across the 2.4.0 series
- **Throttle-aware probing infrastructure (rc2.x)**: single-socket
  cred-auth refactor, brand-static rate_limit_per_ip_tcp data, cross-
  sequence pacing, snap_loop ffmpeg-restart cadence floor.
- **Microseven/Hipcam-family field hardening (rc3.x – rc4.0)**: closed
  the four leaks (E, F, G, plus the rc4.0 G-tightening) that allowed
  multi-socket hammering during scan, added Aggressive Cooldown
  Detection (ACD) as a runtime safety net, and corrected two snap_loop
  edge cases (transport-flip dead-end on persisted UDP, X-Stream-Status
  using lifetime frame count instead of per-run).
- **Adaptive enhanced-view UX (rc3.1 – rc3.3)**: env-var pipeline +
  CFG_ADAPTIVE_QUALITY gating, resolution dropdown grey-out when only
  one choice exists, fixed camera-card position, dead JS cleanup,
  Connecting/Switching toasts, focus_leave_kill flag fix.
- **Adaptive snapMode oscillation fix (rc3.2)**.

### Known limitation deferred to a later release
- Microseven re-test was deferred at user's request; primary-network
  validation against unaffected cameras passed cleanly. If field
  reports surface 2.4.0 issues against Hipcam-family hardware
  specifically, follow-up addressed in a 2.4.x patch.

## 2.4.0-rc4.0

**Single-fix bumpfix on 2.4.0-rc3.5.** Tightens the Leak G gate
(alt-port Layer 1 skip) so it fires for cameras that are already in
firmware-level RTSP lockout when the scan begins, not just for healthy
cameras whose canonical port confirmed speaker status.

### The gap that surfaced in 2.4.0-rc3.5 field testing
The 2.4.0-rc3.5 Leak G fix set `host_skip_layer1_alt` only when
`host_has_rtsp_speaker == True`, which itself required the canonical
port (554) to respond with RTSP-formatted headers. For a healthy
Microseven that's the right behavior. For a Microseven already in
firmware lockout from a prior scan (or a prior build's hammering),
the canonical port RSTs on path 1, never confirms speaker status, the
flag stays False, and `_probe_host_port`'s HTTP branch falls through
into `find_rtsp_path` for every alt port — exactly the multi-socket
hammering the gate was supposed to prevent. The 2026-05-07 0018 field
log captured this: 4 Layer 1 walks against a locked Microseven despite ACD
correctly escalating, because ACD slowed the walks down but the gate
let them happen at all.

### The fix
Broaden the gate's truth condition. `host_skip_layer1_alt` now also
fires when ALL three conditions hold:
1. The port is non-canonical (alt port — not 554/8554/10554)
2. Brand-id identifies the host as a `rate_limit_per_ip_tcp` brand
   (Hipcam, Sricam, Vstarcam, Wansview-old, Tenvis)
3. AND at least one lockout signal is present:
   - `_RST_OBSERVED[ip]` has any entry (a prior port's walker bailed
     on RST/broken-pipe), OR
   - `_ACD_ESCALATED[ip]` is currently active

Brand-id is run inline at the host_meta build site using
`_identify_camera_brand` against the same fields the existing brand
pre-probe uses (mac_vendor + nmap_product + hostname). Cheap.

### Why this won't false-positive
- Healthy cameras of any brand: existing speaker-confirmed branch
  fires; new branch's RST signal is absent.
- Brand-throttled cameras with no RSTs yet: new branch's RST signal
  absent → falls through to existing speaker-confirmed branch.
- Non-throttled brands (Hikvision, Dahua, Axis, Hanwha, etc.): new
  branch's brand-throttled check fails; existing behavior unchanged.

### Validation procedure
1. Power-cycle the Microseven, wait for VLC to confirm RTSP works against
   the camera directly.
2. Install 2.4.0-rc4.0. First scan should walk the canonical port
   once, then for each alt port log:
   `RTSP fall-through skipped: 10.0.0.22:80 — canonical RTSP port
   already established speaker status (no alt-port Layer 1 walk
   needed)` — same as a healthy 2.4.0-rc3.5 scan would.
3. Without recovering the camera, attempt a second install (or
   restart the addon). The canonical port will RST. The new gate
   should fire on alt ports with the new log line:
   `Alt-port Layer 1 walk pre-skipped: 10.0.0.22:80 — brand=Microseven
   is rate_limit_per_ip_tcp AND lockout signals present (RST
   observed=True, ACD active=False) — canonical port never confirmed
   speaker but camera is misbehaving; further walks would extend the
   lockout`.
4. Total Layer 1 socket opens against a locked Microseven should be 1 (just
   the canonical port that surfaces the RST), not 4.

## 2.4.0-rc3.5

**Four-fix bumpfix on 2.4.0-rc3.4.** Closes the two leaks (E and F) that
were explicitly out-of-scope when the original Throttle-Aware Probe
Pacing Plan shipped in 2.3.0, plus a newly-discovered seventh leak (G)
that surfaced when the Microseven was hammered into firmware-level
lockout during back-to-back rc2.0/rc3.4/rc2.0 install testing, plus
Aggressive Cooldown Detection (ACD) as a runtime safety net for any
RST-class behavior that the brand-static throttle data fails to predict.

### Background — why the Microseven was getting hammered in rc3.x

The original Throttle-Aware Probe Pacing Plan was filed during 2.2.8-rc2.6
debugging and identified six leaks (A–F). Plan items A, B, C, D shipped
in 2.3.0 as part of the single-socket cred-auth refactor. Items E and F
were explicitly deferred. A seventh leak — alt-port Layer 1 walks
multiplying TCP socket opens during scan — was not in the original plan
because it didn't exist as a problem at the time: rc2.0 had a separate
nmap intersection bug (`-p` AND'd with `--top-ports` instead of unioned)
that silently shrunk the focused scan to ~4 ports per host, so alt
ports 80/443/8080 weren't being discovered for the Microseven. rc2.1
fixed the nmap bug (correct fix), exposing the latent multi-socket
issue in the alt-port probe loop.

### Bug fixes
- **Leak E — `_fix_codec` first attempt was hard-coded 1.0s**: the
  cross-sequence pacing for retries (5.0s) was correct in rc2.6, but
  the FIRST attempt's sleep was hard-coded to 1.0 unconditionally,
  ignoring `_THROTTLE_TRACK[ip]`. For Hipcam-family cameras whose
  preceding cred-auth ffprobe at line 8498 already left an entry in
  `_THROTTLE_TRACK`, the codec correction probe would land inside the
  cooldown window, fail with no codec, and the misreported codec would
  persist. Now uses `_throttle_wait_if_needed(ip, throttle_s, ...)`
  for attempt 0; retries unchanged.

- **Leak F — `handle_stream_test` had no throttle gate**: the Test
  Stream button fires a single ffprobe — fine for one click, hazardous
  if the user double-clicks or clicks while a scan is running on the
  same IP. Added `_throttle_wait_if_needed` before the ffprobe call.
  User-visible cost: up to ~throttle_s (5s for Hipcam) for affected
  cameras only; zero impact otherwise.

- **Leak G — alt-port Layer 1 skip-gate had a hole**: rc2.4 added an
  optimization: once a canonical RTSP port (554/8554/10554) confirms
  the host speaks RTSP, alt ports skip the Layer 1 path walk. The gate
  fires correctly when `_initial_protocol` returns "RTSP" for the alt
  port — but for cameras whose alt-port nmap banner doesn't contain
  "rtsp"/"camera" (the common case for Hipcam-family on port 80, where
  nmap sees the GoAhead web admin and classifies as plain "http"),
  `initial` is already "HTTP", the gate's `initial == "RTSP"` condition
  is False, no downgrade fires, and `_probe_host_port`'s HTTP branch
  falls through to `find_rtsp_path` anyway — opening a fresh TCP socket
  per alt port. For the Microseven on a populated network, this added
  3 unnecessary Layer 1 walks per scan (4 sockets in 31s observed in
  the rc3.4 field log) against a camera with a 5s per-IP TCP rate-limit,
  enough to push it into a firmware-level lockout that persisted
  through addon uninstall and required a power-cycle. Plumbed a new
  `host_skip_layer1_alt` flag through `host_meta` (parallel to the
  existing `host_skip_layer2`) and gated the HTTP-branch
  `find_rtsp_path` call on it for non-canonical ports.

### New feature
- **Aggressive Cooldown Detection (ACD)**: per-IP RST observation
  tracker that escalates the cooldown to 30s for 5 minutes when 2+
  RSTs/broken-pipes are observed within a 60s window. Defense-in-depth
  for cameras whose actual rate-limit is stricter than CAMERA_DB
  documents, or for cameras already in firmware-escalated lockout from
  prior pressure (e.g. a previous build's hammering). Hooks into both
  the `_probe_rtsp_paths_single_socket` and
  `_validate_rtsp_urls_single_socket` exception handlers — the two
  places where mid-walk RSTs surface — and applies via the existing
  `_throttle_wait_if_needed` mechanism so all cred-auth, ffprobe, and
  snap_loop call sites benefit automatically. Logs a warning on the
  first escalation per IP so the behavior is observable.

### Validation procedure
1. Install 2.4.0-rc3.5 on a system with a Microseven (or any
   Hipcam-family) camera that's no longer in firmware lockout (verify
   by checking RTSP works in VLC against the camera directly first).
2. During scan, log should show the canonical port walked once, then
   for each alt port: `RTSP fall-through skipped: 10.x.x.x:80 —
   canonical RTSP port already established speaker status (no alt-port
   Layer 1 walk needed)`. No further Layer 1 walks against the same IP.
3. After cred-auth, codec correction should succeed on attempt 1 (no
   "all 3 ffprobe attempts failed" warning) because Leak E now waits
   the proper cooldown.
4. Click Test Stream rapidly twice in a row — second click should log
   `Throttle wait Xs for 10.x.x.x ... stream test ffprobe`.
5. Optional ACD validation: run the scan against an artificially
   misbehaving camera (or one currently in lockout) — after 2 RSTs in
   <60s, log should warn `ACD: 10.x.x.x produced 2 RST/broken-pipe
   events in <60s — escalating per-IP cooldown to 30s for 300s`.

## 2.4.0-rc3.4

**Two-fix bumpfix on 2.4.0-rc3.3.** Both surfaced from a Microseven
focus-session test (rc3.3 install): user re-entered focus on a camera whose
RTSP had previously failed and been flipped to UDP, expected to see the
http_snap fallback engage as in the first session, instead saw a frozen
frame for the entire 28-minute test across two re-focuses with no JS
toast and no transport flip in the log. Devtools captures showed
`X-Stream-Status: ok` on every snapshot during the freeze. Both fixes
below are safety-net hardening — they make the freeze recoverable and
visible — but the underlying cause (ffmpeg getting "Invalid data found
when processing input" from the Microseven RTSP every time) is a separate
regression scoped for the next build.

### Bug fixes
- **Bug 1 — transport flip dead-end on persisted UDP in focus mode**:
  `preferred_transport` is stored in `CAMERAS[camera_id]` and persisted
  to disk, so once a prior session flipped a camera to UDP the value
  survives across focus exits, idle stops, and addon restarts. The
  transport-flip block in `snap_loop` had two arms — `cur_transport == "tcp"`
  (any mode) and `cur_transport == "udp" and not native_res` (thumbnail
  mode only) — but no arm for `cur_transport == "udp" and native_res`.
  When that case occurred, the block fell through silently:
  `transport_flip_fired` stayed False, which gated the http_snap
  fallback below at `state.get("transport_flip_fired")`, so the
  fallback also never fired and ffmpeg restart-looped indefinitely.
  Added the missing branch: revert to TCP for this focus session and
  arm `transport_flip_fired=True` so a subsequent 3-streak triggers
  the http_snap fallback as designed.

- **Bug 2 — X-Stream-Status used lifetime frame_count instead of
  per-run count**: `state["frame_count"]` accumulates across every
  snap_loop call for a camera_id, including prior http_snap_loop
  sessions and thumbnail polling. Once a camera had ever produced any
  frame at all, the gate `not state.get("frame_count", 0)` evaluated
  False forever, so X-Stream-Status fell through to `"ok"` even when
  ffmpeg had been dead the entire current focus session. Result: the
  rc3.3 Bug B fix (Connecting / Switching transport toasts) couldn't
  fire — the JS was correctly reading the header but the server was
  reporting healthy. Added a per-ffmpeg-launch counter
  `state["current_run_frames"]` (reset to 0 at each `_launch_snap`,
  incremented per parsed frame) and gated X-Stream-Status on that
  instead. Status now correctly reflects "current ffmpeg has produced
  zero frames AND the streak is non-zero" → connecting/switching.

### Known issues carried into next build
- Microseven RTSP returns `Invalid data found when processing input`
  on every connect attempt across both /11 (main) and /12 (sub) profiles
  on both TCP and UDP transports. ffmpeg has produced literally zero
  frames for this camera in the rc3.3 test log. This is a regression
  from earlier rc2.x builds where the Microseven was producing real frames
  (clean 218-frame run observed earlier). Investigation scope for the
  next build: walk CHANGELOG between last-known-good and rc3.3 for
  RTSP-related changes, compare ffmpeg command lines, run vanilla
  ffmpeg against the Microseven outside the pipeline as a control.

## 2.4.0-rc3.3
**Four-fix bumpfix on 2.4.0-rc3.2. (1) Bug A: stale focus_leave_kill
flag killed snap_loop on first ffmpeg failure of a fresh focus
session — the bug behind CrystalHeeler's "menu greyed out and clicks did
nothing" symptom. (2) Bug B: visible Connecting / Switching transport
toasts during the retry-cycle window so the focus view no longer
appears mysteriously frozen. (3) Camera Cards Fixed Position: cards
no longer swap on login. (4) Dead JS Cleanup: removed three
confirmed-unused functions.**

### Issues fixed

**1. Bug A — stale `focus_leave_kill` flag killed snap_loop on
focus re-entry after an HTTP-snap fallback session.**

`handle_focus_clear` (line 7643) sets `state["focus_leave_kill"] = True`
when the user leaves enhanced view. The flag is meant to be consumed
by `snap_loop`'s main RTSP path at line 6817 — when ffmpeg's read
loop hits EOF after a focus-leave, the restart logic checks this
flag and bails out of restarting (returning to thumbnail polling).

The `state` dict lives in `_SNAP[camera_id]` and **persists across
snap_loop instances**. The flag is set per-camera, so when the user
leaves a focus session that ended in HTTP-snap fallback, the flag
gets set but never consumed — because `snap_loop` is no longer in
its main RTSP loop, it's awaiting `http_snap_loop` at line 6966.
Neither `http_snap_loop` nor task cancellation pops the flag.

Result: `focus_leave_kill` stays `True` in `_SNAP[camera_id]` after
any session that ended in HTTP-snap fallback. On the **next** focus
entry on that camera, the first ffmpeg failure (very common with the
Microseven /11 RTSP regression we're investigating separately as
task `c`) hits the EOF branch at line 6713, sees the still-stale
flag, logs `"ffmpeg killed by focus-leave"` (wrong — the user just
entered, didn't leave), and the restart-skip at line 6817 returns
from snap_loop entirely. Result: snap_loop is dead, the dropdown
greys out (or worse, doesn't), and dropdown clicks become no-ops
because there's no live loop to receive them.

This matches CrystalHeeler's log timeline at 15:07:11 → 15:08:43 in his
2.4.0-rc3.2 test exactly:
- Previous session ended in http_snap fallback at 14:00:50
- CrystalHeeler left at 14:01:54 (flag set, never consumed)
- Next focus at 15:08:43 saw the stale flag and committed suicide
  on the first ffmpeg failure
- 15:08:59, 15:09:09 manual tier dropdown clicks went nowhere

**Fix:** `handle_focus_enter` (line 7549) now clears any stale
`focus_leave_kill` flag on entry. Belt-and-suspenders: the
HTTP-snap fallback site at line 6966 also clears the flag
immediately after `http_snap_loop` returns, so even if the user
exits/re-enters in a tight window, the flag won't leak forward.
With both clears in place, the flag is guaranteed to be consumed
exactly once per `handle_focus_clear` call, which is its design.

**2. Bug B — frozen focus view during the retry-cycle window
before fallback engages.**

When ffmpeg keeps crashing with 0 frames (e.g. the Microseven
"Invalid data found" symptom), the focus view shows the last
cached frame for 15-30s before either RTSP recovers or the
HTTP-snap fallback engages. There was no visible status during
this window — no spinner, no "connecting…" message, nothing.
CrystalHeeler's exact observation from the 2.4.0-rc3.2 test: "completely
frozen the entire time I was in the view, regardless of what
I changed in the dropdown."

**Fix:** new `X-Stream-Status` response header from `handle_snapshot`
exposes the retry phase to JS. Possible values:
  - `ok` — normal (frames flowing or fresh start, no toast)
  - `connecting` — ffmpeg has crashed at least once, still trying TCP
  - `switching_transport` — TCP failed 3×, now trying UDP transport
  - `http_fallback` — HTTP-snap mode (handled separately by the
    existing httpFallback toast from 2.4.0-rc3.2)

JS reads the header, tracks transitions in new state var
`_lastStreamStatus`, and surfaces messages via the existing
`focus-warning` toast element (the same one used by the
4K-too-demanding case in 2.4.0-rc2 and the HTTP-snap toast in
2.4.0-rc3.2). The toast text is "Connecting to RTSP stream…" or
"Switching transport (TCP → UDP) — camera does not support TCP
RTSP" depending on whether the transport flip has fired.

The toast logic carefully avoids stomping on higher-priority
messages — if the 4K-too-demanding or HTTP-snap toast is already
displayed, the connecting-status toast doesn't override it. When
the stream recovers (`streamStatus === 'ok'`), our toast hides
itself but leaves other toasts alone.

**3. Camera Cards Fixed Position — cards no longer swap on
login.**

Root cause: when a user logs into a camera, the camera's `id`
changes (e.g. `10.0.0.22_onvif` → `10.0.0.22_onvif_MainStreamProfileToken`
because the ONVIF profile token gets appended after auth). The
prior `renderGrid` logic matched cards by ID alone — the old ID
disappeared from the cameras array, the corresponding DOM card
was removed, and the new ID's card was appended at the end of
the grid. Visible to users as a "swap" since the old card vanished
and the new one appeared in a different slot.

**Fix:** match cards by a stable `_stableCardKey(cam)` derived from
the camera's `ip:port` instead of just `id`. This survives the
login-induced ID change because IP and port don't change. When an
existing card's ID has changed (because the same IP:port now
carries a profile-tokened ID), `renderGrid` updates the card's
`dataset.id` in place and re-renders its content — DOM position
is naturally preserved because we never remove-and-re-append.

Each card now also has a `dataset.stableKey` attribute set on
creation in `buildCard`, used as the primary lookup index in
`renderGrid`'s map. The fallback to `dataset.id`-based matching
covers the legacy edge where an old card was rendered before the
attribute was introduced (first render after upgrade).

A future feature for user-driven card reordering (drag-to-reorder)
will be straightforward to add on top of this: capture the DOM
order of `[data-stable-key]` values, persist in `localStorage`,
and replay as a sort comparator at the top of `renderGrid`. The
inline comment in the rewrite calls this out for future reference.

**4. Dead JS Cleanup — three confirmed-unused functions removed.**

A two-pass scan (word-boundary regex for definitions, full-substring
grep for any reference including HTML attribute strings like
`onerror="..."`) identified three JS functions that appear only
at their definition site:

  - `imgError(img)` — labeled "onerror helper — avoids embedding
    quotes in the generated HTML string" but the string-literal
    callers had been refactored away in some earlier release
    without removing the helper. Truly orphaned.
  - `stopAllSnaps()` — wrapper around `Object.keys(_snapTimers).forEach(stopSnap)`.
    Never called.
  - `storNavForward()` — storage-browser forward-history navigator.
    Apparently the storage-browser UI never grew a forward button
    (only back/up/up).

The substring scan matters because the regex-only scan wouldn't
catch HTML-attribute references like `<img onerror="imgError(this)">`
embedded in template literals — both passes must come up empty
before deletion is safe. All three did.

Functions called exactly once (56 of them per the same scan) were
NOT touched — those would be inlining candidates, which is a
refactoring decision separate from dead-code removal.

### Files changed

- `camera_discovery.py`:
  - line 7549 area: `handle_focus_enter` clears stale
    `focus_leave_kill` (Bug A primary fix)
  - line 6966 area: belt-and-suspenders clear after `http_snap_loop`
    returns (Bug A defensive fix)
  - line 7436 area: `handle_snapshot` emits new `X-Stream-Status`
    header (Bug B server-side)
  - line 10366 area: new JS state `_lastStreamStatus` + transition
    handler in the focus poll (Bug B JS-side)
  - line 10808 area: `renderGrid` rewritten with `_stableCardKey`
    for position preservation (Camera Cards)
  - 3 unused JS functions deleted (Dead JS Cleanup)
- `config.yaml`: version bump
- `CHANGELOG.md`: this entry

### Live-test plan

1. **Bug A repro path.** Log into Microseven, enter focus
   view, wait for HTTP-snap fallback to engage (~15-30s), exit
   focus, immediately re-enter focus. Pre-fix behavior: snap_loop
   committed suicide on first ffmpeg failure, dropdown clicks were
   no-ops. Post-fix: focus session runs normally, transport flip
   may fire again (still going through the TCP→UDP cycle since
   that's separate work `c`), but dropdown stays responsive.
2. **Bug B retry-cycle visibility.** Microseven focus entry should
   show "Connecting to RTSP stream…" toast within 1-2s of the
   first ffmpeg failure. After 3 TCP failures, toast updates to
   "Switching transport (TCP → UDP) — camera does not support
   TCP RTSP". After UDP also fails 3×, toast switches to the
   2.4.0-rc3.2 message "Live RTSP stream unavailable — showing
   periodic snapshots from this camera".
3. **Camera Cards Fixed Position.** Discover cameras, note their
   positions in the grid. Log into one of them. Card with the
   newly-logged-in camera should NOT swap positions — should
   stay in its original slot, just update content.
4. **Dead JS regression check.** App should load and operate
   normally — no `ReferenceError: imgError is not defined`,
   `stopAllSnaps is not defined`, or `storNavForward is not
   defined` in the browser console. Storage browser back/up
   navigation should still work (those are separate functions
   that were not touched).
5. **All 2.4.0-rc3.2 fixes intact.** Transport flip still fires
   in focus mode, HTTP-snap fallback toast still appears,
   `adaptive_quality=true` default still applies for new installs.

### Out of scope (deferred)

- **Microseven /11 RTSP root-cause investigation (task `c`).**
  The "Invalid data found when processing input" error itself
  is a regression from 2.3.x → 2.4.x — CrystalHeeler's older-version
  install will produce the comparison data we need. Not
  addressed in this build.
- **Future drag-to-reorder of cards.** Foundation is laid by
  this build's `_stableCardKey` work; the actual UI feature
  is a future release.
## 2.4.0-rc3.2
**Three-fix bumpfix on 2.4.0-rc3.1, all stemming from yesterday's
Microseven enhanced-view investigation. (1) RTSP transport flip now
runs in focus mode, not just thumbnail mode — fixing the canonical
victim (Microseven and class-mates: Sricam, generic ONVIF) where
the camera accepts TCP SETUP but replies with UDP, surfacing as
"Invalid data found when processing input" and 3 immediate ffmpeg
crashes per session. (2) HTTP-snap fallback in focus mode now shows
a visible status toast explaining why the resolution dropdown just
greyed out — the existing hover-tooltip wasn't discoverable. (3)
adaptive_quality default flipped from false to true in config.yaml,
matching what the translations description has always claimed
("Default ON") and restoring the auto-step-down behavior that the
2.4.0-rc3.1 gating fix surfaced as missing.**

### Issues fixed

**1. Microseven `/11` RTSP `Invalid data` crash — transport flip
extended to focus mode.**

The "Invalid data found when processing input" ffmpeg error on
Microseven `/11` has been happening every focus session for at
least two test cycles. It's documented in our own code comments
(line 6894-6899): a class of cheap/generic ONVIF cameras (Sricam,
Microseven, etc.) accept the TCP SETUP request but reply with UDP
in the Transport header, which ffmpeg surfaces as "Nonmatching
transport in server reply" → "Invalid data found when processing
input". The fix is to flip `preferred_transport` to UDP after 3
consecutive 0-frame TCP failures.

That fix existed in `snap_loop`'s zero-frame handler — but it was
gated on `not native_res`, meaning it only ran in thumbnail mode.
For the Microseven specifically:
  • Thumbnail mode uses `http_snap_url` (`/tmpfs/snap.jpg`) so
    RTSP is never exercised → transport flip never fires there.
  • Focus mode tries RTSP, fails 3 times on TCP, but the 3-strike
    threshold immediately triggered the HTTP-snap fallback at
    line 6929 — bypassing the transport flip entirely.

Net: `preferred_transport` stayed `"tcp"` forever, every focus
session crashed identically, and the dropdown silently disabled
when fallback engaged. The log evidence is unambiguous — three
focus sessions across two test runs (yesterday's 12:18 and today's
13:05/13:11) all show:
```
ffmpeg starting (codec=h264, hw:h264_v4l2m2m, ...)
ffmpeg stderr: rtsp://10.0.0.22:554/11: Invalid data found when processing input
ffmpeg EOF after 0 frames
[2x more identical attempts]
3 consecutive 0-frame failures in enhanced view — RTSP non-functional, falling back to HTTP snap loop
```

The 2.4.0-rc3.2 fix:
  • Removes the `not native_res` gate from the transport-flip block,
    so the flip runs in BOTH thumbnail and focus modes.
  • Keeps the `not native_res` gate on the UDP→TCP revert branch —
    thumbnail mode still cycles TCP↔UDP for cameras that fail both,
    while focus mode falls through to the HTTP-snap fallback after
    UDP also fails.
  • Adds `state.get("transport_flip_fired")` as a precondition to
    the HTTP-snap fallback. Now the fallback only fires AFTER both
    transports have been tried.
  • Resets the local `streak` variable to 0 inside the flip block
    so the HTTP-snap fallback doesn't fire on the same iteration
    (subtle: the fallback reads `streak` (local), the flip resets
    `state["zero_frame_streak"]` (state's copy)).

For the Microseven specifically, expected behavior post-fix:
  1. Enter focus view. ffmpeg launches with `-rtsp_transport tcp`,
     fails with "Invalid data" — same as before.
  2. After 3 such failures, log warns "switching to UDP transport
     (camera may not support TCP RTSP)" and `preferred_transport`
     persists as `"udp"`.
  3. Next ffmpeg launch uses `-rtsp_transport udp`. If Microseven
     speaks UDP RTSP, video starts and the focus session works
     normally with all dropdown controls live.
  4. Subsequent sessions on this camera start at UDP and skip the
     TCP failure cycle entirely.
  5. If UDP also fails 3 times (camera speaks neither correctly),
     fall through to HTTP-snap fallback — same end state as
     pre-2.4.0-rc3.2, but reached only after exhausting both
     transports.

This is the canonical fix for "rate_limit_per_ip_tcp" branded
cameras (Microseven, Hipcam, Sricam, etc.) that don't fully
implement TCP RTSP. It does NOT pre-emptively switch to UDP for
those brands — every camera still gets TCP-first because TCP RTSP
is the more common, more reliable transport when supported.
Trial-and-error per camera, but only the first focus session pays
the cost; subsequent sessions inherit the learned transport.

**2. Visible toast when HTTP-snap fallback engages.**

The existing `httpFallback` JS block (lines ~10371-10380) sets
`sel.disabled = true`, `sel.title = 'Stream switching unavailable
— RTSP not accessible on this camera'`, and `g.style.opacity =
0.4` on the resolution/fps dropdowns. The disabled state and 0.4
opacity ARE visible — the camera dropdown does grey out. But the
explanation lives in a hover-tooltip on a disabled control, which
most users never discover.

CrystalHeeler's exact observation from yesterday's debugging: "the menu
greyed out... I left enhanced view, then went back in and tried
again... but it didn't seem to function either."

The fix re-uses the existing `focus-warning` toast element (used
by the "4K too demanding" auto-step-down case) and adds a snap-
mode-transition detector. New JS state var `_lastSnapMode`
tracks the last `X-Snap-Mode` header value seen. When the value
transitions from `rtsp` to `http`, the toast shows "Live RTSP
stream unavailable — showing periodic snapshots from this camera"
and stays visible until either focus is exited or the snap_mode
transitions back to RTSP.

Unlike the 4K-too-demanding toast which auto-hides after 5 seconds,
this one persists for the full HTTP-snap session — the underlying
condition persists, so the message should too. The toast is hidden
on transition back to RTSP (defensive — in 2.4.0-rc3.2 the focus
session never transitions back, but a future build might enable
RTSP retries).

The `clearTimeout(_focus4kWarnTimer)` call inside the toast-show
block prevents the 4K warning's auto-hide timer from accidentally
hiding our HTTP-snap toast if both fired in the same session.

**3. adaptive_quality default flipped from false to true.**

Discovered via 2.4.0-rc3.1's investigation: the `translations/en.yaml`
description has always said "Default ON" while config.yaml had the
default as `false`. This was masked pre-2.4.0-rc3.1 because run.sh
never exported the env var anyway, and a Block B gating bug in
snap_loop happened to enable auto-step-down via the repeated-restart
path even when CFG_ADAPTIVE_QUALITY was False. 2.4.0-rc3.1 fixed
both bugs but as a side-effect made every install start with
adaptive auto-stepping disabled (since `false` was the config
default). 2.4.0-rc3.2 flips the config default to match the
description. New installs get auto-step-down on by default; existing
installs keep whatever value is saved in their /data/options.json.

CrystalHeeler's note from session: "It [adaptive_quality] has never worked.
I haven't brought it up because other things are more important
but I have never seen the feed auto-step down to a lower
resolution... This is probably because I failed to properly define
what 'unstable' means..." That's tracked as the 2.4.0-rc7.0
redesign work — separate from this default flip. The flip restores
the toggle's *intended* behavior; whether the underlying detection
actually fires usefully is the rc7.0 question.

### Files changed

- `camera_discovery.py`:
  - Lines ~6894-6943: removed `not native_res` from transport-flip
    block, kept it on the UDP→TCP revert sub-branch, added
    `state.get("transport_flip_fired")` precondition to HTTP-snap
    fallback, added `streak = 0` reset inside flip block
  - Lines ~10316: new JS state var `_lastSnapMode`
  - Lines ~10366: snap-mode transition detector that surfaces the
    focus-warning toast on rtsp→http and clears it on http→rtsp
- `config.yaml`: `adaptive_quality: false` → `true`; version bump
- `CHANGELOG.md`: this entry

### Live-test plan

1. **Microseven focus session.** Toggle hw_decode and
   adaptive_quality both ON. Enter enhanced view. First 3 ffmpeg
   launches will still fail with "Invalid data" (TCP). Watch for
   log line: `3 consecutive 0-frame failures with TCP — switching
   to UDP transport`. Next ffmpeg launch should use
   `-rtsp_transport udp`. If video starts: success, controls stay
   live, no greyout, no toast.
2. **Microseven follow-up session.** Exit enhanced view, re-enter.
   Should start immediately with UDP (preferred_transport persisted)
   — no TCP failure cycle this time.
3. **Microseven HTTP-snap fallback path.** If UDP also fails (e.g.
   camera doesn't speak UDP either), after 3 UDP failures the
   HTTP-snap fallback should fire with the toast: "Live RTSP stream
   unavailable — showing periodic snapshots from this camera".
   Resolution dropdown greyed AND now has visible explanation.
4. **Hikvision regression check.** Enter creds + enhanced view.
   Should work as before — Hikvision speaks TCP fine, transport
   flip never fires, no toast.
5. **Adaptive quality default check.** New install (or wipe
   /data/options.json): startup log should show
   `adaptive_quality=true`. Existing installs: whatever was saved
   stays saved.
6. **All 2.4.0-rc3.1 fixes intact.** Three Config: log lines on
   startup, hw_decode actually engages, accurate Pi 4 rpivid
   diagnostic.
## 2.4.0-rc3.1
**Two-fix bumpfix on rc3.0 surfacing a long-latent infrastructure bug
that has masked every config toggle since the first one was added.
Found while investigating why hw_decode=True in the Configuration tab
wasn't actually engaging hardware decoding even though h264_v4l2m2m
probed as available at startup. Root cause: run.sh never read
/data/options.json or exported any of the user-configurable options
as environment variables, so every CFG_* in camera_discovery.py has
been falling through to its hardcoded default since the day it was
introduced. Fix #1 rewrites run.sh to use `bashio::config` for every
option in config.yaml. Fix #2 corrects an unrelated CFG_ADAPTIVE_QUALITY
gating asymmetry exposed by today's investigation.**

### Issues fixed

**1. run.sh never exported config options — every UI toggle was a
no-op since the first one was introduced.**

The end-to-end flow for HA addon configuration is:

1. User flips a toggle in the addon's Configuration tab
2. HA Supervisor writes the new value to /data/options.json
3. The addon's run.sh reads /data/options.json (typically via bashio)
4. run.sh exports the value as an environment variable
5. The addon process (camera_discovery.py) reads the env var and
   acts on it

Step 3-4 was missing entirely. Pre-rc3.1 run.sh was 9 lines, all of
them dealing with ingress wiring:

```bash
#!/usr/bin/with-contenv bashio
export INGRESS_PATH=$(bashio::addon.ingress_entry)
export INGRESS_PORT=8099
bashio::log.info "Camera Discovery starting on port ${INGRESS_PORT}"
bashio::log.info "Ingress path: ${INGRESS_PATH}"
exec python3 /camera_discovery.py
```

Meanwhile camera_discovery.py lines 120-134 had been doing:

```python
CFG_HW_DECODE = os.environ.get("HW_DECODE", "false").lower() == "true"
CFG_LOW_FPS   = os.environ.get("LOW_FPS_MODE", "false").lower() == "true"
# ... and 12 more options
```

`os.environ.get("HW_DECODE", "false")` returned `"false"` every time
because `HW_DECODE` was never in the environment. So `CFG_HW_DECODE`
was always False, regardless of toggle state in the UI. Same for
LOW_FPS_MODE, SKIP_NONREF, LIMIT_THREADS, STAGGER_POLLING,
ADAPTIVE_QUALITY, RECORDINGS_PATH (always defaulted to /media/anycam
even if the user changed it), MOTION_SENSITIVITY, MOTION_COOLDOWN_SECS,
MOTION_CLIP_PADDING_SECS, UNRESTRICTED_STORAGE_BROWSER, and the four
LOG_* toggles. **Every toggle in the addon Configuration tab was a
no-op.**

The bug stayed hidden for so long because:

  • CrystalHeeler's primary HW-decode test target was the Hikvision
    streaming HEVC, where hevc_v4l2m2m always probed unavailable on
    Pi 4 anyway (the rpivid + stateless-API issue uncovered yesterday).
    So even with a working toggle, ffmpeg would have stayed in
    software for HEVC.

  • The defaults are sensible — most toggles default to false, which
    is also the "safe / don't do anything special" behavior. Users
    who never toggled anything got default behavior either way.

  • Several toggles' "off" behavior is what most users want anyway
    (Skip Non-Ref Frames, Limit Threads, Stagger Polling all default
    off and most setups don't need them).

  • CrystalHeeler mentioned in this session that early on he had toggled
    these on and off and "couldn't quite see a behavioral difference"
    — that observation was correct; the differences were never
    being applied.

Today's investigation was the first real test of hw_decode on an H.264
camera (Microseven) where h264_v4l2m2m IS available and would
have engaged. The log showed `ffmpeg starting (codec=h264, sw, ...)`
on every launch despite the toggle being on — which traced back to
the run.sh export hole.

The fix rewrites run.sh to read every option from /data/options.json
via `bashio::config 'option_name'` and export it as the env var the
Python expects. bashio returns "true"/"false" strings for bool
options, which is what the Python's `.lower() == "true"` check
already expects, so no Python-side changes are needed. The fix also
adds three log lines on startup that dump the resolved config
values, so future debugging can confirm at a glance whether the
env-var pipeline is intact.

After this fix, every existing CFG_* gate in camera_discovery.py
starts working for the first time. Users who had toggles on but
weren't seeing the corresponding behavior should suddenly see it.
This means rc3.1 carries some unintentional behavior changes — not
new code, but newly-active code. Specifically: anyone who has Low
FPS Mode on with HEVC streams will get 2fps output (was getting full
fps). Anyone with Limit Threads on will see ffmpeg capped at 2 threads
(was uncapped). Anyone with Skip Non-Ref Frames on will see slight
choppiness in HEVC streams (was decoding all frames). Anyone with
Stagger Polling on with 4+ cameras will see staggered ffmpeg starts
(was simultaneous). Anyone with Adaptive Quality on with focus view
will see auto-stepping during stream instability (was manual-only).
Anyone whose Recordings Path differs from the default will see
recordings actually go to that path. Etc.

These behavior changes are the toggles WORKING, not regressions.
Worth being aware of post-rc3.1 to interpret any "the system is
behaving differently than yesterday" observations.

**2. CFG_ADAPTIVE_QUALITY gating asymmetry at line 7019.**

Found while investigating Fix 1 — looking at why today's log showed
adaptive focus stepping down even on what would have been a "toggle
off" install (because of Fix 1's bug, every install was effectively
"toggle off" for adaptive quality).

The adaptive-stepping logic in snap_loop's focus mode has two paths:

  • **fast-death path** (Block A, lines 6996-7007): the just-launched
    ffmpeg crashes within _ADAPTIVE_UNSTABLE_S seconds with fewer
    than _ADAPTIVE_UNSTABLE_FR frames. Block A correctly gates this
    on `manual_override or not CFG_ADAPTIVE_QUALITY`.

  • **repeated-restart path** (Block B, line 7019): a stream that
    locked at a tier has restarted enough times since lock to warrant
    stepping down. Block B was checking only `manual_override`,
    not CFG_ADAPTIVE_QUALITY — so even with Adaptive Quality
    disabled, the system would auto-step after repeated restarts
    at a locked tier. Inconsistent with Block A and inconsistent
    with the toggle's documented behavior.

rc3.1 makes Block B's gate identical to Block A's:

```python
if ada.get("manual_override") or not CFG_ADAPTIVE_QUALITY:
    restart_overflow = False
else:
    restart_overflow = (locked and
                        ada.get("restarts_since_lock", 0) >= _ADAPTIVE_RESTART_LIMIT)
```

Now both paths respect the toggle uniformly.

This bug was masked by Fix 1 — CFG_ADAPTIVE_QUALITY was always False
anyway, but Block B's then-buggy gating happened to NOT bail out
because it didn't check CFG_ADAPTIVE_QUALITY. So adaptive WOULD step
down on repeated restarts, even though the toggle was off. With Fix 1
landing, this asymmetry would have started actively misbehaving:
users with Adaptive Quality OFF would see repeated-restart
auto-stepping anyway. Fix 2 prevents that.

### What's still on the deferred list (not in rc3.1)

- **rc4.0**: Lorex/Dahua DVR Family Support (channel iteration
  consuming streaming_recipe data added in rc2.0)
- **2.5.0-rc1.0**: bundle rpi-ffmpeg so HEVC HW decode works on Pi 4
- The Microseven enhanced-view re-entry bug observed in today's log
  (snap_loop dying mid-session, dropdown clicks become no-ops) is a
  separate issue that needs its own investigation; not in rc3.1.

### Files changed

- `run.sh`: complete rewrite — 9 lines to 52 lines. Now reads every
  option in config.yaml from /data/options.json via bashio::config
  and exports as env var. Logs resolved config values on startup.
- `camera_discovery.py` (line ~7019): Block B gating now matches
  Block A's pattern with the CFG_ADAPTIVE_QUALITY check.
- `config.yaml`: version bump
- `CHANGELOG.md`: this entry

### Live-test plan

1. AnyCam startup log should show three new "Config:" lines listing
   the values of every toggle. The hw_decode line should specifically
   say `hw_decode=true` matching what's in your Configuration tab.
2. With hw_decode on AND a running snap_loop on the Microseven
   (which streams H.264, h264_v4l2m2m available): ffmpeg launch line
   should now say `hw:h264_v4l2m2m` instead of `sw`. CPU on the Pi
   during streaming should drop noticeably.
3. Toggle hw_decode OFF in the Configuration tab and restart the
   addon. Startup log should show `hw_decode=false`. Subsequent
   ffmpeg launches should go back to `sw`.
4. With Adaptive Quality OFF: enter focus view on the Microseven, let the
   stream restart 2+ times (e.g. by switching profiles or letting it
   crash naturally). Should NOT see "stepping down to..." log lines.
   With Adaptive Quality ON: same scenario should produce step-down.
5. Sanity check: change Recordings Path in Configuration tab to
   something non-default like /media/anycam-test, restart. Startup
   log should show that value. (Don't actually need to test motion
   recording — just confirming the env var pipeline.)
6. All rc3.0 fixes still in place: NameError-free hw_decode toggle,
   accurate Pi 4 rpivid diagnostic on startup.
## 2.4.0-rc3.0
**Two-fix follow-up to rc2.9, both targeting the hw_decode toggle path.
(1) Hoists `_HW_DECODER_CANDIDATES` to module scope, fixing a latent
NameError that would have triggered the moment any HW decoder probed
as available AND a stream actually tried to use it. (2) Replaces the
misleading "device not found" probe message with an accurate Pi 4
rpivid diagnostic that names the real cause (ffmpeg lacks v4l2-request
support, not a kernel device problem). The full rpivid HEVC HW decode
fix — bundling rpi-ffmpeg in the addon Docker image — is targeted for
2.5.0; this release just clears the diagnostic noise so future
debugging on the rpivid front lands accurately.**

### Issues fixed

**1. _HW_DECODER_CANDIDATES NameError fix.**

snap_loop at line ~6615 has been referencing `_HW_DECODER_CANDIDATES`
as a module-level constant since back in the 2.2.5 timeframe, but the
name was only ever defined as a local list inside `_probe_hw_decoders`.
The CHANGELOG entry from that era acknowledged the bug and reverted to
2.2.4, with a note that "SigRev-2 will be re-implemented correctly in
a future release after Decoding-Rev is resolved." That re-implementation
landed but the missing module-level constant was never added back.

The bug stayed dormant because every Pi 4 system AnyCam ships on hits
this code path:
  1. `_probe_hw_decoders` runs, fails on every HEVC/VAAPI candidate,
     succeeds only on h264_v4l2m2m.
  2. User runs with hw_decode=False (the default), snap_loop's
     `if CFG_HW_DECODE:` branch is never entered, NameError is never
     reached.
  3. User toggles hw_decode=True, but most users have HEVC streams
     so codec_lower is "hevc" and the loop body iterates looking
     for "hevc" in decoder names.

The moment a user with hw_decode=True streamed an H264 camera, the
NameError would have fired on first ffmpeg launch. It just never
quite got triggered because CrystalHeeler's primary test camera (Hikvision
the Hikvision) is HEVC and HEVC HW decode never worked anyway (see fix #2).

rc3.0 hoists `_HW_DECODER_CANDIDATES` to a proper module-level
constant near `_HW_UNAVAILABLE` (line ~470), with a long comment
explaining the historical context, the order semantics (v4l2m2m
preferred over vaapi), and the Pi 4 HEVC caveat. Both
`_probe_hw_decoders` and snap_loop now reference the same list.

**2. Misleading probe message replaced with accurate Pi 4 rpivid
diagnostic.**

For roughly 6 months the probe has been logging:
  hevc_v4l2m2m: unavailable (device not found)
on every Pi 4 startup, regardless of whether rpivid was loaded or
not. That message is technically true (ffmpeg's stderr does say
"Could not find a valid device") but it sent everyone — including
this assistant when helping CrystalHeeler — down the wrong rabbit hole:
chasing dtoverlay configurations, kernel module loading, container
device passthrough, and other things that ARE WORKING CORRECTLY.

The actual cause is much narrower. Pi 4 has TWO separate hardware
decoder pathways:
  • bcm2835-codec — exposes /dev/video10/11/12, implements stateful
    V4L2 m2m API, supports H264/MPEG/VP8/VP9/VC1 but NOT HEVC.
  • rpivid — exposes /dev/video19, implements stateless V4L2 request
    API, supports HEVC only.

ffmpeg's `hevc_v4l2m2m` decoder is a wrapper around the stateful
m2m API. It iterates V4L2 m2m devices looking for one that exposes
HEVC. On Pi 4 there's no such device — bcm2835-codec doesn't do
HEVC, rpivid uses the wrong API. So `hevc_v4l2m2m` will literally
never find a valid device on Pi 4, regardless of dtoverlay.

To actually USE rpivid HEVC HW decode, ffmpeg must be built with
`--enable-v4l2-request` and the stream must be opened with `-hwaccel
drm` against the stateless API. The ffmpeg shipped in this addon's
Docker image (Alpine ffmpeg) is NOT compiled with v4l2-request
support — `ffmpeg -h decoder=hevc` lists supported HW devices as
"cuda vaapi vdpau" only, no drm/v4l2request.

rc3.0 detects rpivid presence (both `/dev/video19` and `/dev/media0`
exist) at the start of the probe. When `hevc_v4l2m2m` then fails
with "Could not find" stderr, the diagnostic message reads:

  hevc_v4l2m2m: unavailable (rpivid present at /dev/video19 but
  bundled ffmpeg lacks v4l2-request support — stateful m2m API
  doesn't expose HEVC on Pi 4. Will be fixed in 2.5.0 by bundling
  rpi-ffmpeg.)

instead of the old generic "device not found." Future diagnostic
sessions land on the actual problem instantly.

The old "device not found" message is preserved for cases where
rpivid is NOT present (Intel/AMD/x86 systems, Pi 4 without the
dtoverlay, etc.) — those genuinely are device-not-found situations.

### What's NOT in rc3.0

The actual fix — bundling rpi-ffmpeg so HEVC HW decode works on Pi 4
— is targeted for 2.5.0. That requires modifying the addon's
Dockerfile to either compile a custom ffmpeg with --enable-v4l2-
request --enable-libdrm or pull in jc-kynesim's rpi-ffmpeg fork.
Both are real projects with image-size and build-time implications.
Not appropriate for an rc bumpfix.

Until 2.5.0 ships with that change, the practical workaround for
Pi 4 users with HEVC streams is to switch the camera's encoding to
H.264 in the camera's web UI. h264_v4l2m2m is fully functional on
Pi 4 and will hardware-decode H.264 streams cleanly.

### Files changed

- `camera_discovery.py`:
  - line ~470: new module-level `_HW_DECODER_CANDIDATES` constant
    with historical context
  - `_probe_hw_decoders` (line ~13507): now uses the module-level
    constant instead of a local list; rpivid presence detection
    via `/dev/video19` + `/dev/media0`; targeted Pi 4 diagnostic
    message when hevc_v4l2m2m fails on a system with rpivid loaded
- `config.yaml`: version bump
- `CHANGELOG.md`: this entry

### Live-test plan

1. AnyCam startup log shows the new accurate message:
   `hevc_v4l2m2m: unavailable (rpivid present ... will be fixed in
   2.5.0 ...)` instead of the old `device not found`.
2. h264_v4l2m2m still probes as available on Pi 4 (regression check).
3. Toggle hw_decode=True, navigate to an H264 camera, focus-view
   should NOT NameError on snap_loop launch (Fix 1 verification).
   ffmpeg should start with `codec=h264, hw=h264_v4l2m2m`.
4. All rc2.9 fixes still in place: dropdown dedup, sub-stream
   labels, Lorex skip_layer2 inheritance, Stage B safety net.
## 2.4.0-rc2.9
**Five-fix follow-up to rc2.8. (a) Reverts rc2.8's Fix 5 — Stage B
during Deep Re-Probe runs again on skip_layer2 brands, restoring the
catch-everything safety net the user explicitly designed for rare
firmware-quirk cases. (b) DB-probed sub_url's captured codec/res/fps
now flow through to its stream_profiles entry instead of being
hardcoded None — fixes "Stream 2" appearing in the dropdown when the
DB probe had successfully captured "704x480 MJPEG". (c) Layer 1-
discovered unauth streams are now probed with ffprobe so their
stream_profiles entry shows real resolution + codec. (d) Lorex/Dahua
DVR-NVR Family alt-port skip_layer2 inheritance — port 80 no longer
runs Layer 2 for 45s on a host where port 554 already identified
the brand as skip_layer2. (e) NEW: stream_profiles dedup on (codec,
width, height) — Hikvision's three working URLs that all resolve to
the same 2560x1440 HEVC encoder collapse to one dropdown entry; same
for any other duplicates.**

### Issues fixed

**a. REVERT rc2.8's Fix 5 — Stage B during Deep Re-Probe respects
user's catch-everything intent.**

rc2.8 dropped the rc2.5 expansion that forced Stage B (Layer 2) to
run during Deep Re-Probe on skip_layer2 brands, on the reasoning
that skip_layer2 brands "guaranteed" Layer 2 failure. The user
clarified that Deep Re-Probe was intentionally designed as the
catch-everything button — overriding ALL skip flags including
skip_layer2 — specifically to catch rare firmware-quirk cases that
the documented brand entries can't predict:

  • Sub-stream paths that only respond on a fresh socket. Some
    cheap firmwares have buggy session state where the second
    DESCRIBE on a single socket returns garbage instead of clean
    200/401, but a fresh socket returns clean. Layer 1's single-
    socket walk would skip these; Layer 2's fresh-socket-per-path
    catches them.
  • Servers that close the socket after first 401. Older Foscam-
    family clones do this — Layer 1 walk prematurely ends, Layer 2
    re-opens and continues.
  • Token-bucket rate limits that reset between sockets. Some
    cameras throttle requests-per-socket but not requests-per-IP —
    single-socket walks hit the throttle, multi-socket walks (with
    5s cooldown) don't.

The 45s Layer 2 wait on Hikvision Deep Re-Probe is the accepted
cost of this safety net. rc2.9 restores the rc2.7 trigger logic.

**b. sub_url's stream_profiles entry now carries DB-probed details.**

The rc2.6/rc2.7/rc2.8 stream_profiles building code hardcoded sub_
url's entry to None across all four fields (width, height, codec,
fps) — even though the DB probe at api_set_credentials had
successfully captured them. That's why CrystalHeeler's Hikvision dropdown
showed "Stream 2" instead of "704x480 MJPEG" for the
/Streaming/Channels/102 entry: the data was being thrown away in
the building step.

rc2.9 captures sub_details = {k: v for k, v in additions[0].items()
if k != "url"} when sub_url is picked from db_streams, then injects
those captured fields into the stream_profiles entry for sub_url.

**c. Layer 1-discovered unauth streams probe ffprobe at discovery
time.**

When a camera doesn't require credentials (e.g. CrystalHeeler's HA camera
at 192.168.50.73:8765 serving rtsp://.../stream open), the Layer 1
walk in find_rtsp_path returns the working URL but never probes
its codec/resolution. The cred-accept flow (which DOES probe) never
runs for unauth cameras. So cam.stream_width / stream_codec stayed
None, and the dropdown synth fallback fell through to "Stream 1".

rc2.9 adds an _enrich_with_details async helper inside _probe_host_
port. After find_rtsp_path / probe_mjpeg_http / probe_hls returns a
working URL, the helper runs probe_stream_details and merges the
captured codec/res/fps onto the camera dict. Best-effort: if the
probe fails (timeout, RST, weird codec), we just don't have
enrichment and the dropdown stays at the numbered fallback. Cost:
~3s per discovered unauth stream.

**d. Lorex/Dahua alt-port skip_layer2 inheritance.**

CrystalHeeler's Lorex port 554 IDs as "Lorex / Dahua DVR-NVR Family"
(skip_layer2: True), so Layer 2 correctly skips on 554. But port
80 IDs as plain "Lorex" (a different STREAM_DB row, no skip_
layer2), so Layer 2 was running for ~45s on port 80 — wasting time
on a host we'd already identified as can't-speak-Layer-2.

rc2.9 introduces host_has_skip_layer2 in run_scan, parallel to the
existing host_has_rtsp_speaker per-IP flag. When find_rtsp_path's
skip_layer2 short-circuit fires, it sets host_meta["brand_skip_
layer2"] = True. run_scan picks that up after the port's probe and
sets host_has_skip_layer2 = True for the rest of this IP's port
loop. Subsequent ports' host_meta gets host_skip_layer2 = True
propagated, and find_rtsp_path's skip_layer2 check honors that
flag in addition to the per-port brand match.

Logged as "skip_layer2 inherited for {ip} — alt ports will also
skip Layer 2 walks" once per IP when the inheritance kicks in, and
"RTSP Layer 2 skipped: {ip} inherited skip_layer2 from earlier
port on this host" on each subsequent port that benefits.

**e. Stream profiles dedup on (codec, width, height).**

Hikvision DS-2DE typically exposes its main 2560x1440 HEVC stream
via THREE different URLs that all backend to the same encoder:

  • /Streaming/Channels/101  (canonical Hikvision path)
  • /h.264/ch1/main/av_stream  (legacy Hikvision/Foscam variant)
  • /Streaming/Channels/1  (Hikvision short form)

In rc2.8 with fix b+c applied, all three would have shown up in
the dropdown labeled identically as "2560x1440 HEVC" — confusing.

rc2.9 adds a dedup pass after stream_profiles is fully built. Key
is (codec, width, height); first occurrence wins. First-discovery
order is the existing array order: pre-auth Layer 1 main → DB-
probed sub → validated locked candidates (in walker order). For
Hikvision: /101 wins over /h.264/ch1/main/av_stream and
/Streaming/Channels/1.

Probe-failure entries (any of width, height, codec is None) stay
distinct — better to keep both than risk collapsing genuinely-
different-but-unprobed streams. Logged as "stream_profiles dedup:
dropped N duplicate entry/entries on (codec, width, height)" when
any drops occur.

### Files changed

- `camera_discovery.py`:
  - `api_deep_reprobe` (line ~9139): Stage B trigger restored to
    rc2.7 form
  - `api_set_credentials` (line ~8716): sub_details captured from
    db_streams pick + used in stream_profiles
  - `_probe_host_port` (line ~11859): _enrich_with_details async
    helper + applied to all unauth-discovery success paths
  - `find_rtsp_path` (line ~4892): honors host_meta["host_skip_
    layer2"] in addition to per-port brand match; sets host_meta
    ["brand_skip_layer2"] when triggered
  - `run_scan` (line ~12148): host_has_skip_layer2 tracker added,
    propagated via host_meta["host_skip_layer2"]
  - `api_set_credentials` (line ~8893): stream_profiles dedup pass
- `config.yaml`: version bump
- `CHANGELOG.md`: this entry

### Live-test plan

1. Hikvision (10.1.1 net) — dropdown after cred-auth shows
   exactly 2 entries: "2560x1440 HEVC" + "704x480 MJPEG" (was 5 in
   rc2.8 with three duplicates collapsed).
2. Hikvision — Deep Re-Probe runs Stage A AND Stage B, taking
   ~50s total instead of <1s. Stage B will fail but the safety net
   is engaged.
3. Lorex (192.168.1 net) — first scan after install: port 80
   no longer runs Layer 2; "skip_layer2 inherited" log entry
   appears once after port 554 finishes.
4. HA camera 192.168.50.73:8765 — focus-view dropdown shows actual
   resolution + codec instead of "Stream 1".
5. Microseven cred-auth still works; dropdown shows the 2
   ONVIF-reported profiles correctly.
## 2.4.0-rc2.8
**Five-fix follow-up to rc2.7 — first build with rc2.6 features
actually exercised in production. Fixes (1) Deep Re-Probe button
visibility (only when there are skipped paths to resume; vanishes
after completion), (2) toast duration 2.5s → 7s so users can read
completion messages, (3) focus-view dropdown shows ALL stream
profiles (main + sub + every validated locked-stream candidate)
instead of just main + sub — was the most visible regression in
rc2.7's first live test on Hikvision, (4) Debug Log toggle
defaults to on, and (5) Deep Re-Probe Stage B respects skip_layer2
(saves ~45s per Hikvision Deep Re-Probe).**

### Issues fixed

**1. Deep Re-Probe button visibility.** The rc2.6 cardActions JS had
three button states: in-progress (yellow + spinner), skipped-paths
(yellow), and a subdued ghost "Deep Re-Probe" fallback for cards
where `early_bail_reason` was empty. The third state was misleading
for two reasons:

  • After a successful Deep Re-Probe, the backend clears
    `early_bail_reason` to indicate the deep work is done. With the
    rc2.6 cardActions, this caused the button to revert from the
    loud yellow "(running)" state to the subdued ghost — making it
    look like the operation had been undone.
  • Cards that NEVER had skipped paths (e.g. Lorex/Dahua DVR family,
    which has `skip_layer2: True` and walks Layer 1 cleanly with no
    early-bail) showed the subdued ghost button by default. Clicking
    it launched a "fresh full probe" that had no extra capability
    beyond the original scan, so the user always saw "no streams
    found" with no actionable next step.

rc2.8 changes the rule: the button is visible IFF
`cam.early_bail_reason` is set (or `deep_reprobe_in_progress` is
True for the running state). After Deep Re-Probe completes, the
backend clears `early_bail_reason` and the button vanishes — clean
indication that the work is done. Cards that never had skipped
paths get no button at all.

The in-progress state requires `early_bail_reason` to remain
visible, so the moment Deep Re-Probe finishes and clears the flag,
the button disappears in the same render. No flicker between
"running" and "subdued."

**2. Toast duration 2.5s → 7s.** `showToast()` had a 2500ms
auto-dismiss. Users reported the Deep Re-Probe completion toast
("🔒 N locked stream(s) found", "no streams found", etc.) was
disappearing before they could read it. 7000ms gives enough time
to read a one-line message comfortably without lingering long
enough to feel obstructive. Affects all toast messages, not just
Deep Re-Probe.

**3. Focus-view dropdown shows ALL stream profiles.** rc2.6's Fix
3 (post-auth validation of locked candidates → `additional_streams`)
and Fix 4 (preserve pre-auth main URL) worked correctly at the
backend — Hikvision's 3 validated locked candidates were stored
on the camera record. But the focus-view dropdown showed only 2
entries ("Stream 1", "Stream 2") instead of 5.

Root cause: there are TWO synth fallbacks for `stream_profiles`
in the codebase:
  • Line 6312: used by snap_loop tier selection (backend). rc2.6
    wired `additional_streams` into this one.
  • Line 7651: used by `api_focus_get` to build the dropdown
    (frontend). Reads only `stream_url` + `sub_stream_url`, not
    `additional_streams`.

The dropdown JS hits the line 7651 path. Since the non-ONVIF
cred-accept path doesn't populate `cam.stream_profiles` directly,
the dropdown synth fallback fired — and it didn't know about
`additional_streams`.

rc2.8 fixes this two ways for defense in depth:

  (a) **Backend:** `api_set_credentials` non-ONVIF path now builds
      `cam.stream_profiles` explicitly with all entries (main +
      DB-probed sub + each validated `additional_streams` entry)
      and persists it via `save_cameras()`. Dropdown reads the
      canonical list directly. Includes a one-shot
      `probe_stream_details` call per validated locked-stream so
      they get real resolution/codec captured (not just URL).
      Brand throttle cooldown applies between probes.
  (b) **Frontend:** the line 7651 synth fallback also includes
      `additional_streams` entries. Covers cameras saved by
      earlier builds (rc2.6/rc2.7) that have `additional_streams`
      populated without `stream_profiles` — they get the right
      dropdown on reload without needing re-auth.

Cost: on Hikvision DS-2 with 3 validated candidates, +~9s at
cred-accept time for `probe_stream_details` calls. On rate-
limited brands (Hipcam-family, 5s cooldown), +~24s. Acceptable
trade for correct dropdown labels and proper snap-loop tier
selection.

**4. Debug Log toggle defaults to on.** `config.yaml`:
`log_debug: false` → `log_debug: true`. New installs and existing
installs that haven't customized the toggle will now log DEBUG-
level entries by default. Useful for diagnosing edge cases without
requiring users to opt in.

**5. Deep Re-Probe Stage B respects `skip_layer2`.** The rc2.5
build expanded the Stage B trigger to also fire when the brand
has `skip_layer2: True` AND the bail reason was
`layer1_consecutive_401s`. The reasoning was: "Hikvision/Lorex
silently skipped Layer 2 during the original scan, so Deep Re-
Probe should give it another shot." This was wrong.

`skip_layer2` isn't an optimization marker — it's a documented
brand property:
  • Hikvision DS-2 RSTs multi-socket fanout connections; Layer 2
    walk produces 10 consecutive failures, then bails.
  • Lorex/Dahua DVR-NVRs use `auth_attempt_lockout` throttle —
    every Layer 2 socket counts as a failed auth attempt
    (max 10 before account lockout for 30 min).

In neither case does Layer 2 produce useful information. The
user's 10.1.1Hikvision Deep Re-Probe log showed exactly this:

```
09:55:30 Deep Re-Probe Stage B: running Layer 2 (skipped during
         original scan)
...
09:56:15 Deep Re-Probe Layer 2 bailing after 10 consecutive
         failures
```

45 seconds wasted for guaranteed failure. Stage A (resume Layer 1
with `deep_reprobe_mode=True` and brand-recipe filter) is what
actually does the work — surfacing locked candidates from the
unwalked paths. That stage is unchanged.

For brands WITHOUT `skip_layer2`, Stage B still triggers on
`bail_reason == "layer1_then_layer2_skipped_401s"` exactly as it
did pre-rc2.5 — that case is genuinely informative because it
means Layer 2 was throttle-skipped, not skip_layer2-skipped.

### Files changed

- `camera_discovery.py`:
  - `cardActions()` JS (line ~10632): three-state Deep Re-Probe
    button → two-state, gated on `early_bail_reason ||
    deep_reprobe_in_progress`
  - `showToast()` (line ~10417): 2500ms → 7000ms
  - `api_set_credentials` non-ONVIF (line ~8770-8895): added
    per-candidate `probe_stream_details` after validation; explicit
    `stream_profiles` build before `camera.update()`
  - `api_focus_get` synth fallback (line ~7651): includes
    `additional_streams` entries
  - `api_deep_reprobe` (line ~9043): `run_layer2_followup` simplified
    to drop the rc2.5 skip_layer2 expansion
- `config.yaml`: version bump + `log_debug: true`
- `CHANGELOG.md`: this entry

### Live-test plan

1. Add-on starts cleanly (no SyntaxError loop, both gates pass).
2. Hikvision — after Deep Re-Probe completes, the yellow
   "Deep Re-Probe (skipped paths)" button DISAPPEARS rather than
   reverting to subdued.
3. Hikvision — Deep Re-Probe completes faster (no 45s Layer 2
   wait); toast readable for 7 seconds.
4. Hikvision — after cred-auth via Locked Streams modal, the
   focus-view resolution dropdown shows 5 entries:
     • `/Streaming/Channels/101` (main, 4MP H.265+ — may show as
       "Stream 1" if ffprobe can't read H.265+)
     • `/Streaming/Channels/102` (sub)
     • `/h.264/ch1/main/av_stream` (validated locked candidate)
     • `/h.264/ch1/sub/av_stream` (validated locked candidate)
     • `/Streaming/Channels/1` (validated locked candidate)
5. Lorex — card shows NO Deep Re-Probe button (was previously
   showing subdued ghost). Cred-auth still requires manual entry
   via the regular cred form.
6. Microseven — cred-auth still works (regression check).
7. Add-on logs show DEBUG entries by default without you toggling
   anything in the UI.
## 2.4.0-rc2.7
**Hotfix for rc2.6 install crash. The rc2.6 build had a fatal
SyntaxError that prevented the add-on from starting on any HAOS
system: `name 'PENDING_CAMERAS' is used prior to global declaration`
inside `run_scan`. Cause was a duplicate `global PENDING_CAMERAS`
declaration in the function's `finally` block — Python only allows
ONE `global` declaration per name per function, and it must appear
BEFORE the name is referenced. The first declaration at scan start
(line ~11869) was correct; the second one in `finally` (line ~12438)
broke the entire module's bytecode compilation. Removed the
duplicate.**

**Also upgraded the release gate. The gate's syntax check used
`ast.parse()`, which does NOT catch this class of error — global-
declared-after-use is detected by the bytecode compiler, not the
parser. Added a `compile()` step alongside `ast.parse()` so the same
crash can never slip through gate verification again.**

### Why rc2.6 shipped broken

The rc2.6 release passed all 5 gates locally and packaged cleanly.
The bug only surfaces at module load time, which gate 1 (`ast.parse`)
doesn't simulate. Gates 2-5 work on the AST tree, not the compiled
bytecode, so they couldn't see it either. The first time the bug
manifested was when HAOS tried to import the module on CrystalHeeler's
system — a 5-second crash loop with the SyntaxError shown.

This is a real gap in our gate. Adding `compile(src, path, 'exec')`
catches this and other compile-time-only errors like `nonlocal`
binding errors, duplicate kwargs in calls, and any future variant
of "scoping declared after use." Cost: ~one extra second at gate
time.

### What changed

**1. The duplicate `global PENDING_CAMERAS` removed.** Inside
`run_scan` (line ~12438, the `finally` block). The first declaration
at scan start (line ~11869) covers both assignments; Python's
scoping rules mean one declaration per name per function is enough,
and putting a second one after the name has been referenced inline
elsewhere in the same function is a hard error.

**2. `verify_release.py` gate 1 now also runs `compile()`.** Catches
the rc2.6 bug pattern and others. Two-step: `ast.parse()` first
(faster, gives parse-grammar errors clearly), then `compile()` (a
bit slower, gives the real "module loadable" check). Either failing
exits with non-zero.

### Sanity check

The dedup logic, pending-flush behavior, brand-recipe filter, post-
auth validation, main-stream preservation, and Deep Re-Probe button
styling are all the same as rc2.6 — none of those code paths ever
ran in production because rc2.6 crashed at import. rc2.7 is rc2.6
minus the broken `global` declaration plus the upgraded gate. All
the rc2.6 changelog content remains accurate; this entry just
patches the crash.

### Files changed

- `camera_discovery.py`: removed duplicate `global PENDING_CAMERAS`
  in `run_scan`'s `finally` block (1-line removal)
- `verify_release.py`: gate 1 also runs `compile()` to catch global-
  after-use and similar compile-time errors
- `config.yaml`: version bump
- `CHANGELOG.md`: this entry

### Live-test plan

This is rc2.6's plan, since rc2.6 itself never ran:

1. Install rc2.7 — add-on starts cleanly (no SyntaxError loop)
2. Rescan 10.1.1.x — Microseven and Hikvision should NOT
   show transient duplicate cards mid-scan (Fix 1 from rc2.6)
3. Click Deep Re-Probe on the Hikvision: loud yellow button "⏳ Deep Re-Probe
   (running)", no neighbor reflow (Fix 5)
4. Toast on completion: ~6 locked candidates, not 26 (Fix 2)
5. Locked Streams modal contents: only Hikvision-recipe paths
6. Enter creds: validation pass runs, bogus drop, valid go to
   `additional_streams` (Fix 3)
7. Focus-view dropdown shows MULTIPLE resolutions including 4MP
   H.264 main (Fix 4)
8. Stream plays at native 4MP: snap_loop uses /Streaming/Channels/101
9. Cred-auth on the Microseven still works (regression check)
## 2.4.0-rc2.6
**Live-test follow-up build addressing 5 quirks CrystalHeeler observed in
rc2.5: (1) Pending-flush card creation. New cards discovered during
a scan accumulate in a server-side buffer instead of rendering to
the UI piecemeal — preventing the user from clicking on cards that
are about to be deduped away. (2) Brand-recipe filter on locked-
stream candidates. Hikvision used to surface 26 spurious "locked
candidates" (every path 401s, regardless of whether it's a real
endpoint). Now filtered to ~6 paths matching the brand's known
recipe. (3) Post-auth validation of locked-stream candidates.
After credentials accepted, walks each candidate to confirm it's
real; bogus ones (404, RST, still-401) get silently dropped. (4)
Cred-accept flow preserves the pre-auth main stream. The rc2.5 DB-
sub-stream pickup was ranking ALL candidates by resolution and
sometimes replacing /Streaming/Channels/101 (4MP main) with /102
(704x480 sub) as the primary URL. Now /101 is locked as primary
and /102+ are pure additions. (5) Loud Deep Re-Probe button styling
with min-width to prevent button reflow.**

### Issues fixed

**1. Pending-flush card creation.** rc2.5 wrote new cards directly
to CAMERAS as they were discovered. With rc2.4/2.5's faster scans,
a multi-port host (Hipcam at the Microseven with 4 open ports, Hikvision at
the Hikvision with 5 open ports) would render as 3-5 separate cards mid-scan
before the dedup pass collapsed them. The user had a 40-80 second
window where they could click "Enter creds" on a card that was
about to disappear, breaking the cred-entry flow.

rc2.6 introduces `PENDING_CAMERAS: dict | None`. During a scan, all
new card writes route through `_publish_scan_card(cam)` which puts
them in PENDING_CAMERAS instead of CAMERAS. The dedup pass operates
on the union of CAMERAS (user-saved cards preserved at scan start)
and PENDING_CAMERAS (newly-discovered). After dedup, surviving
PENDING cards flush into CAMERAS atomically — UI sees them all
appear in one render after dedup.

User-saved cards (from previous scans) stay visible during the new
scan unchanged. Only NEW cards from the in-progress scan are held
back. PENDING_CAMERAS is cleared in run_scan's finally block on
both success and error paths.

**2. Brand-recipe filter on locked-stream candidates.** rc2.5's
Deep Re-Probe Stage A surfaced 26 locked candidates on CrystalHeeler's the Hikvision
Hikvision. The walker's collect_locked logic appends ANY path that
returns 401 with the expected realm — but Hikvision DS-2 returns
401 with realm `IP Camera(F0818)` for ALL paths, including ones
that aren't real endpoints (`/cam/realmonitor` is Dahua, `/stream`
is generic Foscam, etc.).

rc2.6 adds a `brand_recipe_paths: list[str] | None` parameter to
`_probe_rtsp_paths_single_socket`. When the caller has identified
the brand (find_rtsp_path / api_deep_reprobe), it passes the
brand's known RTSP path list from STREAM_DB. The walker filters
locked candidates by path-prefix match against the recipe before
appending. When `brand_recipe_paths` is None, no filter is applied
(backward-compat). Logs `"DESCRIBE 401 path X not in brand recipe
— not surfacing as locked candidate"` for filtered-out paths.

Wired in two places: `find_rtsp_path` passes its already-computed
`db_paths`; `api_deep_reprobe` Stage A re-runs `_match_stream_db`
on host_meta and passes the resulting `rtsp` list. Hikvision's
locked-stream count drops from 26 to ~6 (the actual STREAM_DB
entries: `/Streaming/Channels/101..103`, `/ISAPI/Streaming/...`,
`/h.264/ch1/main/av_stream`, `/h.264/ch1/sub/av_stream`).

**3. Post-auth validation of locked-stream candidates.** Even
after Fix 2's recipe filter, surfaced candidates may not all be
working endpoints (e.g. firmware doesn't expose all DB paths).
rc2.6 adds a validation pass at cred-accept time: for each entry
in `cam.locked_streams`, build the authenticated URL and run
`probe_rtsp` (with brand-throttle cooldown if applicable). The
ones that return SETUP-OK get added to `cam.additional_streams`
as a list of `{path, url, realm, scheme}` dicts. The ones that
RST/404/still-401 get silently dropped. After validation,
`locked_streams` is cleared (the validated subset is in
`additional_streams`).

Slow on cameras with brand throttle (e.g. Hipcam: 5s cooldown ×
6 candidates = 30s extra at cred-accept). Hikvision DS-2 has no
documented cooldown so the validation is fast (<10s).

**4. Cred-accept preserves pre-auth main stream.** rc2.5 had a
latent bug in the non-ONVIF cred-accept path: after the user
entered creds, the DB-sub-stream pickup at line 8682-8696 ranked
ALL streams (main + DB-probed) by resolution descending and picked
`all_s[0]["url"]` as the new primary. On Hikvision DS-2, this
went wrong because:
  • probe_stream_details on /Streaming/Channels/101 returned bogus
    704x480 mjpeg data (ffprobe couldn't parse the 4MP H.264
    stream cleanly — possibly because Hikvision returned a sub-
    stream-style profile in DESCRIBE)
  • DB probe validated /102 with similar low-res data
  • The sort was unstable; /102 ended up at index 0
  • /102 (the sub) became `url`; /101 (the main) became `sub_url`
  • snap_loop streamed /102 at 704x480 mjpeg, the focus-view
    dropdown showed only "704x480 MJPEG" + "Stream 2"

rc2.6 changes the algorithm: the pre-auth main URL `url` is LOCKED
as primary regardless of what DB-probe returns. DB-probed streams
are pure ADDITIONS — they can never replace primary. The lowest-
resolution DB-probed addition becomes `sub_url` (for adaptive
focus-view step-down). All other DB additions go into the profile
list (they were already going there in stream_profiles synthesis).

This also makes Fix 3's `additional_streams` from locked-stream
validation behave correctly: they show up in the focus-view
resolution dropdown alongside the main, sub, and any DB-probed
streams, never replacing the main.

**5. Deep Re-Probe button feedback.** Three sub-fixes:
  • In-progress state was `btn-ghost btn-sm` with text "⏳
    Probing…" — nearly invisible against the dark card. Now uses
    the same yellow-accent styling as the skipped-paths state with
    "⏳ Deep Re-Probe (running)" label and 0.85 opacity to
    indicate disabled.
  • All three button states (idle, skipped-paths, running) now
    have `min-width: 200px; text-align: center` so the button
    width is constant regardless of label. The neighboring buttons
    no longer reflow when state changes.
  • Tooltip text expanded to make the operation clearer.

(CrystalHeeler explicitly opted OUT of a status line below the button — the
loud button itself is sufficient feedback.)

### Files changed

- `camera_discovery.py`:
  - `PENDING_CAMERAS: dict | None` global (line ~607)
  - `_publish_scan_card()` helper (line ~2772)
  - 5 scan-time card-write sites rerouted through helper
  - `run_scan` initializes/clears PENDING_CAMERAS
  - Dedup pass operates on combined CAMERAS+PENDING_CAMERAS, then
    flushes survivors
  - `_probe_rtsp_paths_single_socket`: new `brand_recipe_paths`
    parameter, locked-stream filter at append (line ~3835, ~4150)
  - `find_rtsp_path`: passes `db_paths` to walker (line ~4827)
  - `api_deep_reprobe` Stage A: identifies brand, passes recipe
    paths (line ~8956)
  - `api_set_credentials` non-ONVIF cred-accept path: preserves
    primary URL, validates locked candidates, populates
    `additional_streams` (line ~8682)
  - Stream-profiles synth fallback: includes
    `additional_streams` entries (line ~6312)
  - `cardActions()` JS: loud reprobe button styling + min-width
    (line ~10630)
- `config.yaml`: version bump
- `CHANGELOG.md`: this entry

### Live-test plan

1. Rescan 10.1.1.x net. During the scan, Microseven should NOT
   show 4 separate cards mid-scan — should appear as ONE card after
   the dedup pass at end. Same for Hikvision (one card, not 5).
2. Hikvision card after scan: Deep Re-Probe button visible
   with yellow accent + "(skipped paths)" suffix.
3. Click Deep Re-Probe on the Hikvision. Button immediately changes to "⏳
   Deep Re-Probe (running)" — clearly visible (loud yellow), at
   the SAME button width as before (no neighbor reflow).
4. Toast on completion: should report ~6 locked candidates (down
   from 26 in rc2.5) — those matching Hikvision recipe paths.
5. Click "Locked Streams" badge on the Hikvision, enter creds. Cred-accept
   flow should:
   - Keep `/Streaming/Channels/101` as primary (4MP main)
   - Validate the ~6 locked candidates; bogus ones drop, valid
     ones go to `additional_streams`
   - Focus-view resolution dropdown shows MULTIPLE entries (not
     just "704x480 MJPEG" + "Stream 2")
6. Cred-auth on Microseven still works (regression).
## 2.4.0-rc2.5
**Triage build on top of rc2.4 live-test findings. Four fixes addressing
gaps the rc2.4 logs revealed: (1) Fix C trigger (alt-port RTSP skip)
was silently broken on Hikvision DS-2 cameras because their OPTIONS
fingerprint returns 200 OK with no Server header and no realm — the
speaker-detect code only checked those headers. Now also accepts the
fingerprint helper's own `looks_like_rtsp` flag. (2) Layer 1 early-
bail log line was DEBUG-only on unlabeled walks (= every scan-time
walk), so we couldn't verify Fix B was firing. Now logs at INFO
unconditionally. (3) Deep Re-Probe Stage A re-bailed after 5 paths
(same early-bail counter) AND only collected locked candidates if an
unauth working URL had been found first. New `deep_reprobe_mode=True`
parameter to the walker disables both. (4) Deep Re-Probe Stage B
(full Layer 2 walk) didn't trigger when the original scan's Layer 2
was skipped via brand `skip_layer2: True` flag (Hikvision/Lorex/
Dahua case) — the bail reason captured was `layer1_consecutive_401s`,
not `layer1_then_layer2_skipped_401s`. Now also triggers Stage B when
the camera's brand has `skip_layer2: True`.**

### Why rc2.5 exists

CrystalHeeler's rc2.4 live test confirmed all 7 fixes worked structurally
(false positives gone, button rendered, state-resume mechanics
correct, the Hikvision PTZ skip_layer2 firing) but speed gain was less than
estimated and the Deep Re-Probe button "ran almost instantly" with no
useful output. The 116s total on the 10.1.1.x net (down from 167s)
left ~25-40s of unexplained Hikvision multi-port walking that
shouldn't have happened, and Stage A returned 0 locked candidates
when 31 should have been visible.

Tracing the rc2.4 log surfaced 4 distinct issues, each fixed below.

### What changed

**1. Fix C trigger broken on Hikvision DS-2 (alt-port RTSP skip).**
The rc2.4 fingerprint pre-probe on the Hikvision:554 logged "status=200,
server=None, realm=None, elapsed=3ms" — RTSP/1.0 200 OK with no
Server header and no realm. The speaker-detect code in the per-port
loop required `rtsp_server_header OR rtsp_auth_realm OR
rtsp_public_methods` to set `host_has_rtsp_speaker = True`. All
three were empty, so the optimization didn't fire. the Hikvision:80, the Hikvision:443,
the Hikvision:8000, the Hikvision:8443 each ran the full 31-path Layer 1 walk despite
the Hikvision:554 having already proven the host speaks RTSP.

The fingerprint helper's own `looks_like_rtsp` heuristic correctly
identifies status-200-with-RTSP-status-line as evidence the host
speaks RTSP. rc2.5 plumbs this through as
`host_meta["rtsp_speaker_confirmed"]` and includes it in the
speaker-detect check. Expected gain: ~25s on the Hikvision alone, similar on
any other Hikvision-DS-2 family camera with multiple admin ports.

Also adds an INFO log line "RTSP speaker confirmed for <ip> — alt
ports will skip Layer 1 path walk" so users can see the optimization
firing.

**2. Layer 1 early-bail logging at INFO unconditionally.** The
`_log()` helper inside the walker logs at INFO when called with a
non-empty `label` argument and DEBUG when label is empty. All scan-
time walks pass empty label (because Layer 1 + Layer 2 happen
multiple times per scan and per-walk INFO would flood the log).
Result: the rc2.4 log showed early-bail messages ONLY for the
deep-reprobe walk (which has label="deep-reprobe:..."), making it
impossible to verify the optimization was firing during normal
scans.

rc2.5 routes the early-bail line directly through `log.info(pfx +
...)` regardless of label. It's a per-camera-port event (fires at
most once per Layer 1 walk), low-volume, and high-value for
verifying the optimization. Other internal walker logs (per-path
attempts, per-method responses) still respect the label-driven
DEBUG/INFO split.

**3. Deep Re-Probe Stage A: deep_reprobe_mode parameter.** Two
issues with the rc2.4 Stage A behavior:

  - The early-bail counter fired during the resume walk too. On
    the Hikvision case the cached `early_bail_paths_remaining` had 26
    paths; Stage A walked 5, hit the early-bail threshold, and
    bailed. Logically correct (the remaining 21 will also 401)
    but it makes the button feel like it did nothing.
  - The locked-stream collection is gated by `found_working_url`
    — only fires AFTER an unauth stream has been found. That makes
    sense for the original scan (where surfacing locked candidates
    only matters if there's an existing visible stream) but is
    wrong for Deep Re-Probe on a camera like the Hikvision where every
    stream needs auth.

rc2.5 adds a `deep_reprobe_mode: bool = False` parameter to
`_probe_rtsp_paths_single_socket`. When True, it (a) suppresses the
early-bail counter (forces full walk) and (b) bypasses the
`found_working_url` gate on locked-stream collection. Used only by
the `api_deep_reprobe` Stage A call, doesn't affect any scan-time
behavior.

Expected behavior change on the Hikvision: Stage A will now walk all 26
remaining paths (~5s wall-clock at ~200ms/path on Hikvision) and
surface ~20-25 locked-stream candidates as the visible 🔒 badge
on the card. User can then enter creds to unlock them.

**4. Deep Re-Probe Stage B: trigger expanded for skip_layer2
brands.** The Stage B (full Layer 2 walk) trigger only checked
`bail_reason == "layer1_then_layer2_skipped_401s"`. But that bail
reason is only set by the Layer 2 short-circuit code path that
fires when Layer 1 returned only 401s. When a brand has
`skip_layer2: True` (Hikvision, Lorex/Dahua), find_rtsp_path
short-circuits Layer 2 BEFORE reaching the consecutive-401-skip
code, so the bail reason captured is `layer1_consecutive_401s`
only. Result: clicking Deep Re-Probe on a Hikvision camera ran
Stage A but silently skipped Stage B — exactly what the user
clicked the button to verify.

rc2.5 expands the Stage B trigger: ALSO run Stage B when
`bail_reason == "layer1_consecutive_401s"` AND the camera's brand
has `skip_layer2: True`. Done by calling `_identify_camera_brand()`
on the host_meta we built from the camera record, then checking
the matched entry's `skip_layer2` field.

Expected behavior change on the Hikvision: clicking Deep Re-Probe will now
run Stage A (~5s) followed by Stage B (~50s, 10 sockets × 5s
cooldown × 401s before bail). Total ~55s. The user gets the locked
candidates from Stage A AND verification that Layer 2 doesn't
reveal a firmware-quirk path.

### Files changed

- `camera_discovery.py`:
  - `_probe_rtsp_paths_single_socket`: new `deep_reprobe_mode`
    parameter, suppresses early-bail and bypasses
    `found_working_url` gate when True (line ~3801)
  - Layer 1 early-bail log line: now logs at INFO unconditionally
    (line ~4143)
  - Per-host scan loop: speaker-detect also accepts
    `rtsp_speaker_confirmed` flag (line ~11734)
  - Fingerprint pre-probe: persists `rtsp_speaker_confirmed` flag
    when `looks_like_rtsp` is True (line ~11366)
  - `api_deep_reprobe`: passes `deep_reprobe_mode=True` to Stage A,
    expanded Stage B trigger to include skip_layer2 brands
    (line ~8842)
- `config.yaml`: version bump
- `CHANGELOG.md`: this entry

### Live-test plan

1. 10.1.1.x net rescan: total time should drop from rc2.4's 116s.
   New "RTSP speaker confirmed for 10.0.0.33 — alt ports will skip
   Layer 1 path walk" log line should appear after the Hikvision:554
   fingerprint succeeds.
2. the Hikvision alt ports (.80, .443, .8000, .8443) should each show NO
   "RTSP probe: Layer 1 (single-socket walk, 31 paths)" line —
   skipped via the new flag. Each port should drop from ~5-12s to
   <1s.
3. Layer 1 early-bail should now log at INFO unconditionally on the
   the Hikvision:554 walk (will see the line during the scan, not just during
   deep-reprobe).
4. Click Deep Re-Probe on the Hikvision needs_credentials card. Expected
   sequence:
   - Stage A logs walking all 26 remaining paths (no early-bail
     this time)
   - Stage A surfaces ~20-25 locked candidates (visible as 🔒 badge
     on the card after refresh)
   - Stage B kicks in and runs Layer 2 walk (~50s)
   - Toast reports "🔒 N locked stream(s) found"
5. Cred-auth on the Hikvision still works (regression).

## 2.4.0-rc2.4
**Performance and false-positive build on top of rc2.3. Seven fixes
plus the new Deep Re-Probe button. (1) Tightens the verdict gate so
brand-id from MAC OUI alone no longer creates a card without a
service-level corroborating signal — kills the rc2.3 .12/.13/.14
false positives (TP-Link switch + UniFi APs). (2) Wires up the
FEEDBACK store consumer at scan time — clicking "Not a Camera" now
suppresses pattern-similar devices on other IPs, not just the exact
IP+port. (3) Adds `skip_layer2: True` to the single-camera Hikvision
entry, eliminating the 45s Layer 2 grind on CrystalHeeler's the Hikvision PTZ. (4)
Layer 1 early-bails after 5 consecutive same-realm 401s and saves
state for resume. (5) Layer 2 short-circuits when Layer 1 early-
bailed (fresh sockets won't change auth result per RFC 7235 §2.2).
(6) Per-host port reordering: canonical RTSP ports (554/8554/10554)
probed first; once RTSP-speaker confirmed, alt-ports skip RTSP
probing. (7) Deep Re-Probe button — escape hatch for the aggressive
Layer 1/2 skip heuristics with state-resume from where rc2.4 left
off.**

### Performance impact (estimated)

- test system B: 84s → ~25-30s
- test system A: 167s → ~30-40s

The rc2.4 perf wins come from killing Layer 2 grinding on auth-
required cameras (the Hikvision PTZ alone burned 45s in rc2.3),
early-bailing Layer 1 after we've established the camera needs auth,
and avoiding redundant RTSP probing on alt ports of an IP whose
canonical RTSP port we've already confirmed. The trade-off is small:
a ~5% chance Layer 2's multi-socket walk would have revealed a
firmware-quirk path that single-socket Layer 1 missed. The Deep Re-
Probe button is the user-controlled escape hatch for that case.

### What changed

**1. Verdict gate corroboration (Issue from rc2.3 live test).**
rc2.3's `verdict=="uncertain" + has_brand` accepted any brand-id
match including OUI-only matches. Surfaced 3 false positives on
CrystalHeeler's test system A: TP-Link Tapo / Kasa OUI matched a TP-Link switch
on .12; Ubiquiti UniFi OUI matched two UniFi APs on .13/.14. The
problem is that camera-vendor OUIs are shared across the same
vendor's networking gear (switches, routers, APs).

rc2.4 requires brand-id to be CORROBORATED by at least one service-
level signal beyond OUI:
  • ONVIF scope present (only cameras speak ONVIF)
  • RTSP fingerprint captured (host speaks RTSP)
  • Brand keyword in page_title (camera UI, not switch admin)
  • Brand keyword in server_header (HTTP server identifies as camera)
  • Brand keyword in nmap_product banner

OUI-only matches with NONE of the above no longer create cards.
Logged at INFO level: "Card suppressed: <ip>:<port> brand=<name>
from OUI alone, no service-level corroboration".

**2. FEEDBACK store consumer (Issue from rc2.3 live test).** The
"Not a Camera" button has stored rich fingerprints to
`/data/not_camera_feedback.json` since rc2.0, but no scan-time
consumer was wired up. Per-IP suppression worked via BLACKLIST; the
fingerprint data sat unused.

rc2.4 adds `_matches_feedback_fingerprint(host, port)` checked at
the start of every per-port scan iteration (main + broad-sweep
loops). Conservative match rule:
  • Same OUI (first 3 MAC octets) AND
  • (Same nmap_product OR same port) AND
  • reason_type is set to a specific value (router/printer/nas/
    switch/etc.) — not blank or "unknown"

Same OUI alone is NOT enough — preserves cases like "TP-Link switch
on .12 + Tapo camera on .15" where both share OUI but only the
switch was rejected. ONVIF-discovered hosts are NEVER suppressed
via this check (only cameras speak ONVIF; ONVIF response trumps any
past Not-a-Camera click).

Logged on match: "FEEDBACK fingerprint match: skipping <ip>:<port>
— OUI <oui> + product/port match; reason=<type>".

**3. `skip_layer2: True` on single-camera Hikvision entry.** The
`the Hikvision` Hikvision DS-2DE4A425IW PTZ requires auth — Layer 1 returned
401-with-same-realm on every attempted path, then Layer 2 ran and
also got 401 on every fresh socket before bail-after-10 fired (45s
wasted). With this flag, Layer 2 short-circuits immediately. The
NVR variants of Hikvision already had this flag for the same reason.

**4. Layer 1 early-bail on consecutive same-realm 401s.** Per RFC
7235 §2.2, auth realm is server-scoped, not URL-scoped. After 5
consecutive same-realm 401s on a single socket walk, all remaining
paths will also 401 with the same realm. rc2.4 detects this streak,
bails out, and saves state to `host_meta`:
  • `early_bail_reason = "layer1_consecutive_401s"`
  • `early_bail_realm = "<the realm>"`
  • `early_bail_paths_tried = [first 5 paths]`
  • `early_bail_paths_remaining = [unwalked paths from index 5+]`
  • `early_bail_at = ISO timestamp`

Saves ~4-5s per camera-port (avoids walking ~25 more paths × ~200ms
each). State is later read by the Deep Re-Probe handler for resume.

**5. Layer 2 short-circuit on Layer 1 early-bail.** When Layer 1
bailed early on consecutive same-realm 401s, Layer 2's multi-socket
walk will produce the same 401s on the same realm — fresh sockets
don't change a server-side auth check. rc2.4 skips Layer 2 in this
case and updates the bail reason to `layer1_then_layer2_skipped_401s`
so the Deep Re-Probe handler knows to also run Layer 2 if the user
opts in. Saves ~50s per such camera (the bail-after-10 grind).

**6. Per-host port reordering + alt-port RTSP skip.** Previously the
per-port scan loop iterated `host.open_ports` in nmap order — often
80, 443 first, then 554. For hosts with admin pages on 80/443/8080
AND RTSP on 554, that meant burning ~5-15s per HTTP-only port
walking RTSP paths against a port that doesn't speak RTSP, BEFORE
ever getting to 554.

rc2.4 sorts open ports so canonical RTSP ports (554, 8554, 10554)
go first. Once a canonical port confirms RTSP-speaker status (via
rtsp_server_header / rtsp_auth_realm / rtsp_public_methods), the
loop downgrades RTSP-marked initial-protocol on subsequent non-
canonical ports to "HTTP", causing those ports to skip the full
Layer 1 path walk and use HTTP/MJPEG/HLS probing only.

**7. Deep Re-Probe button (per-card escape hatch).** Available on
every needs_credentials and not_camera-pending card. Highlighted
with yellow accent + "(skipped paths)" suffix when the card has
`early_bail_reason` set, indicating rc2.4 fast-skipped some paths
during the original scan and the user can recover them on demand.

Backend: new `api_deep_reprobe` endpoint at
`POST /api/cameras/{cid}/deep_reprobe`. State machine driven by
`cam.early_bail_reason`:
  • `layer1_consecutive_401s` → resume Layer 1 on
    `cam.early_bail_paths_remaining`
  • `layer1_then_layer2_skipped_401s` → resume Layer 1, then run
    full Layer 2 multi-socket walk (5s cooldown, bail-after-10)
  • unset/missing → run a fresh full Layer 1 + Layer 2

Stage A (resume Layer 1) runs with `collect_locked=True` so 401s on
remaining paths surface as locked-stream candidates the user can
unlock by entering credentials (existing rc2.0 Layered Stream
Discovery pathway). Stage B inlines the Layer 2 walk pattern (5s
cooldown, bail-after-10) over the FULL canonical path list.

Staleness check: if `cam.early_bail_at` is older than 30 minutes,
the saved state is discarded and a fresh full probe runs instead.
Concurrency: `cam.deep_reprobe_in_progress` flag suppresses
overlapping invocations; frontend disables the button while the
handler is in flight.

Outcome stats persisted on the camera record:
  • `deep_reprobe_attempts` — count
  • `deep_reprobe_last_at` — ISO timestamp
  • `deep_reprobe_last_outcome` — "ready" / "locked_streams:N" /
    "no_streams" / "error:<msg>"

Frontend: button rendered in `cardActions()` between Clear Creds
and Not a Camera buttons. Click handler `deepReprobe()` posts to
the endpoint, shows a toast with the outcome ("✅ working stream
found", "🔒 N locked stream(s) found", "no streams found"), then
reloads the camera list so the new state renders.

### Files changed

- `camera_discovery.py`:
  - Single-camera Hikvision CAMERA_DB entry: +`skip_layer2: True`
    (line ~713)
  - `_probe_rtsp_paths_single_socket`: early-bail counter + state
    save (line ~3801)
  - `find_rtsp_path`: Layer 2 short-circuit on Layer 1 early-bail
    (line ~4793)
  - `_probe_host_port::base()`: persist early_bail_* fields onto
    cam record (line ~11071)
  - Stage 3 main scan loop: port reordering, alt-port RTSP skip,
    FEEDBACK fingerprint check (line ~11338)
  - Stage 4 broad-sweep loop: FEEDBACK fingerprint check
  - Verdict gate: corroboration check (line ~11150)
  - `_matches_feedback_fingerprint()`: new helper (line ~2809)
  - `api_deep_reprobe`: new handler (line ~8713)
  - JS `cardActions()`: Deep Re-Probe button
  - JS `deepReprobe()`: click handler
- `config.yaml`: version bump
- `CHANGELOG.md`: this entry

### Live-test plan

- test system B rescan: total time should drop noticeably from 84s.
  Lorex still skipped Layer 2 via brand flag.
- test system A rescan:
  - Total time should drop substantially from 167s
  - Hikvision PTZ: Layer 2 skipped via new `skip_layer2: True`
    flag; total the Hikvision time drops from ~76s to ~10-15s
  - .12 TP-Link switch / .13 .14 UniFi APs: NO cards (verdict gate
    now requires service-level corroboration beyond OUI)
- Deep Re-Probe button visible on the Hikvision card (after rc2.4 rescan,
  early_bail_reason will be set since Layer 1 will fast-bail then
  Layer 2 will skip). Click button → spinner → expected outcome:
  Stage A finds locked candidates on remaining ~25 paths, Stage B
  runs Layer 2 walk and bails at 10 failures, card updates with
  locked-stream badge showing the candidates.
- Cred-auth on the Microseven / the Hikvision still works (regression check)
- "Not a Camera" → rescan: clicking "Not a Camera" on a host then
  rescanning should produce the FEEDBACK match log line and not
  re-create a card for the same OUI+product on a different IP.

## 2.4.0-rc2.3
**Bug-fix release on top of rc2.2 addressing four issues from rc2.2
live testing on CrystalHeeler's two networks. (1) `_isGenericCamName` extended
to recognize auto-discovered hostnames (IPs, reverse-DNS suffixes,
MAC-/serial-derived strings, mDNS local-domain names) as generic so
the identified manufacturer wins display. (2) Per-IP card dedup —
multi-port hosts now produce ONE card per device for non-streaming
HTTP admin pages, while preserving multi-stream camera setups. (3)
WS-RTSP probe tightened to require RFC 6455 subprotocol echo,
eliminating the Microseven false-positive. (4) Stricter HTTP-only
fall-through — suppresses cred-prompt cards for hosts with no
camera-positive signals (no brand match, no RTSP fingerprint),
keeping the scan output clean while preserving the existing "Add
Camera Manually" path for exotic devices.**

### Why rc2.3 exists

rc2.2 shipped four targeted fixes from rc2.1 live testing. rc2.2 live
testing on both networks produced clean perf and correct brand-id
(Lorex no longer grinds Layer 2; Hikvision PTZ now correctly
identifies as "Hikvision" not "Hikvision NVR"; focused scan completes
in ~4-12s vs ~73s) BUT surfaced four UX issues from the now-faster /
broader scan output:

  • Card titles displaying hostnames instead of identified manufacturers
    (e.g. "D861A8.lan" instead of "Lorex / Dahua DVR-NVR Family")
  • Multiple cards per device when a host has multiple open ports
    (e.g. HP printer with 80/443/8080 → 3 "Credentials required" cards)
  • False-positive WS-RTSP card on a Microseven Hipcam (the camera has
    a WebSocket endpoint for its web UI MJPEG feed, not RTSP-over-WS)
  • Generic web-admin devices (printers without IPP exposed, IoT hubs,
    NAS appliances) creating cred-prompt cards even when the verdict
    pipeline had no positive camera signals

These are all consequences of rc2.2 actually finding everything that
was open on each host (where rc2.1's `-sV` timeout was masking most of
it). rc2.3 surfaces the right cards with the right titles and keeps
the false-positive count near zero.

### What changed

**1. `_isGenericCamName` JS regex extended (Issue 1).** The
hostname-as-name fallback at line ~10758 (`prev_name = prev.get("name",
hostname)`) returns the hostname for fresh discoveries. Frontend
displayName logic was correctly trying to swap to manufacturer when
name looked generic, but the existing regex only matched fixed strings
like "ip camera"/"network camera"/"general"/etc. — it did NOT
recognize auto-discovered hostname patterns as generic. rc2.3 adds
matchers for:
  • Bare IPv4 ("10.0.0.13" → generic)
  • All-hex MAC-derived hostnames ("D861A8.lan" → generic
    because "D861A8" is the leftmost label)
  • Serial-derived all-uppercase identifiers ("SN0123456789-
    ABCDEF012345" → generic)
  • Common reverse-DNS suffixes (".attlocal.net", ".local", ".lan",
    ".home", ".localdomain", ".hsd1.*.comcast.net", ".fios-router.home")
  • mDNS-style 3+-label FQDNs with short lowercase first label
    ("tplink.my.house" → generic)

User-given names like "Front Door Camera" or "Driveway" don't match
any of these and remain user-displayed.

**2. Per-IP card dedup at scan completion (Issue 2).** New post-scan
pass before `save_cameras()` groups cards by IP and resolves duplicates
using a conservative rule:
  • Always keep "ready" cards (working streams)
  • Always keep "user_saved" cards (user has interacted with them)
  • Always keep cards with `stream_url` populated
  • If best card on an IP is streaming, suppress all
    needs-credentials and info cards on that IP (we already have a
    working stream)
  • Otherwise keep the highest-priority-protocol card per IP, suppress
    weaker-protocol HTTP siblings

Protocol priority: RTSP > ONVIF > DVR > MJPEG > HLS > RTMP > WS-RTSP >
WebRTC > HTTP. Multi-stream cameras with legitimate multi-port stream
endpoints (e.g. RTSP main on 554 + RTSP sub on a different path) are
preserved because the rule only suppresses HTTP siblings of stream
protocols, never stream-protocol siblings of stream protocols.

Logs the suppressed-card count and IDs (truncated to first 6) so the
behavior is observable.

**3. WS-RTSP probe RFC 6455 verification (Issue 3).** Previously the
probe sent a WebSocket Upgrade with `Sec-WebSocket-Protocol: rtsp` and
checked the response only for "101" status and "websocket" in the
body. Per RFC 6455 §4.1 the server MUST echo the selected subprotocol
in `Sec-WebSocket-Protocol: <token>` if it accepted that subprotocol.
A server returning 101 + "websocket" but NOT echoing `rtsp` speaks
WebSocket but NOT RTSP-over-WebSocket — exactly the Microseven Hipcam
case (its port-80 WebSocket endpoint is for the live web UI MJPEG
feed). rc2.3 parses the response headers, looks for the
`Sec-WebSocket-Protocol` line, splits on comma per RFC, and only
returns a positive match if `rtsp` is in the accepted-subprotocol list
as an exact token (case-insensitive).

**4. Stricter HTTP-only fall-through (Issue 4).** Previously
`_probe_host_port` created a "needs_credentials" HTTP card for any
host with `verdict in ("camera", "uncertain")` after all stream
probes failed. After rc2.2's faster scan that flooded the UI with
cards for printers/NAS/IoT hubs whose web admin pages happened to be
on probe-list ports. rc2.3 keeps `verdict=="camera"` (high-confidence
keyword positive) creating cards as before, but for `verdict==
"uncertain"` ALSO requires at least one of:
  • Brand identified from MAC OUI / ONVIF scope / page title / server
    header (set in host_meta.manufacturer by the upstream pre-probe)
  • RTSP fingerprint pre-probe captured a server header / auth realm /
    public methods (host speaks RTSP even if path-walk failed)

If neither holds, the card is suppressed. Users with truly exotic
cameras Claude doesn't have a brand entry for can still reach them
via the existing "Add Camera Manually" entry point (api_add_camera
+ pscan-ip input field) or via the broad-sweep option.

### Files changed

- `camera_discovery.py`:
  - `_isGenericCamName` JS function: 5 new pattern matchers for
    hostname-derived names (line ~9920)
  - `probe_ws_rtsp`: RFC 6455 subprotocol-echo verification
    (line ~4762)
  - `_probe_host_port`: stricter `verdict=="uncertain"` gate
    (line ~10983)
  - `run_scan`: per-IP card dedup pass before save_cameras
    (line ~11409)
- `config.yaml`: version bump
- `CHANGELOG.md`: this entry

### Live-test target

Re-run scan on:
- test system B (Lorex DVR + an HP printer + OAK-D HAOS feed +
  .235 unknown device): expect ONE card per IP for non-streaming
  hosts (printer collapses from 3 cards → 1 or 0); Lorex card title
  reads "Lorex / Dahua DVR-NVR Family" not the hostname; HAOS RTSP
  feed at the Oak-D camera:8765 still works.
- test system A (Microseven + Hikvision PTZ + .12/.13/.14 unifi/tplink):
  expect Microseven shown as 1-2 cards (not 4) with manufacturer name
  not hostname; no WS-RTSP card on the Microseven; Hikvision shows brand
  name; UniFi Protect cards have manufacturer name displayed.

## 2.4.0-rc2.2
**Bug-fix release on top of rc2.1 addressing four issues from rc2.1
live testing on CrystalHeeler's two networks. (1) Drops `-sV` from the focused
nmap scan and lowers `--host-timeout` from 30s to 15s — empirically
measured ~24× faster on CrystalHeeler's test system B AND fixes a regression
where `-sV`'s per-port latency was timing out slow devices entirely
(the Lorex DVR was being dropped from scan output). (2) Adds
classifier-only ports 22 (SSH), 631 (IPP), 9100 (raw print) so the
verdict logic can reject printers and SSH-only devices that rc2.1's
narrow port list lost the ability to filter — fixes the another device HP
printer false-positive. (3) Sets `skip_layer2: True` on the
Lorex/Dahua DVR-NVR Family CAMERA_DB entry — wires up the rc2.1
short-circuit code that was already merged but had no data-side
companion, so the Lorex DVR Lorex was still grinding 50s through Layer 2.
(4) Refactors `_DB_ENTRIES_BY_KEY` from `dict[str, dict]` to
`dict[str, list[dict]]` and adds an NVR-family tie-breaker so single
cameras with shared brand keywords no longer get misclassified as
NVRs — fixes CrystalHeeler's Hikvision DS-2DE4A425IW-DE PTZ being tagged
as "Hikvision NVR".**

### What changed

**1. `focused_nmap_scan` — drop `-sV`, lower `--host-timeout` to 15s
(Issue A from rc2.1 live test).** Live measurement on CrystalHeeler's 192
network with the rc2.1 51-port list:

```
V1: -sV --host-timeout 30s (rc2.1 setting):  36.2s for 3 hosts
                                              Lorex DVR TIMED OUT
                                              (dropped from output entirely)
V3: -sV --version-light --host-timeout 30s:  34.6s, Lorex still dropped
V5: NO -sV (pure SYN) --host-timeout 15s:    1.5s, Lorex found cleanly
                                              (80, 554, 35000)
```

Why `-sV` was hurting: nmap runs sequential service-banner probes
per open port. On slow/throttled devices (Lorex DVR with auth-attempt
lockout, Microseven Hipcam with rate_limit_per_ip_tcp) the per-port
probe latency stacks up past `--host-timeout`'s 30s budget. When the
budget expires nmap drops the ENTIRE host including already-confirmed
open ports — they never reach our parser. This was the root cause of
the rc2.1 "Focused scan: 0 host(s) responded" regression on test system A
 and the partial-host-coverage on the test system B.

What rc2.2 keeps without `-sV`:
  • Open-port list (the actual goal)
  • `mac_vendor` from ARP — strongest classifier (HP printer correctly
    tagged via OUI; Lorex Technology correctly tagged via OUI)
  • `service` field from `/etc/services` lookup ("http", "rtsp",
    "https") — sufficient for `_initial_protocol()` routing

What we lose: `nmap_product` field (e.g., "gSOAP 2.7"). It was one of
~10 haystack signals in `_identify_camera_brand`; mac_vendor + ONVIF
discovery + HTTP page-title probe + RTSP fingerprint downstream more
than compensate. Lost coverage on real-world devices tested: zero.

**2. Add classifier-only ports 22, 631, 9100 (Issue C).** These ports
are NOT camera ports — they're scanned so the verdict logic has
signals to REJECT non-camera devices that happen to expose a web UI
on 80/443/8080. rc1.0's top-1000 scan caught these incidentally; the
rc2.1 narrow port list lost them, surfacing a regression where the
an HP printer (gSOAP 2.7 web admin on 80/443/8080) re-appeared as
a camera. With rc2.2:

  • Port 631 (IPP) or 9100 (raw print) → unconditional `not_camera`
    verdict regardless of HTTP signals
  • Port 22 (SSH) → `not_camera` verdict only when no camera-typical
    HTTP ports are also open (cameras occasionally have SSH for
    service mode; IoT device / NAS / managed switches typically
    only have SSH + a non-camera HTTP UI)

`classify_device()` updated with port-based fallback after the existing
keyword-based check. `NON_CAMERA_KEYWORDS` updated to include "ipp"
for cases where service text already identifies the protocol. Cost in
scan time: ~50ms total at 14 hosts (3 extra ports × SYN probe each).

**3. `skip_layer2: True` on Lorex/Dahua DVR-NVR Family entry (Issue B).**
rc2.1 added the *consumer* code (the short-circuit at line 4500-4510
in `find_rtsp_path`) but no CAMERA_DB entry was actually flagged with
`skip_layer2: True`, so the check evaluated False on every brand
including Lorex/Dahua. Added the flag with HIGH confidence — the
Lorex/Dahua entry already has all the metadata that says "this is a
multi-channel DVR, do not grind Layer 2": `streaming_recipe` with
channel iteration, `throttle_type: auth_attempt_lockout`, and
`rtsp_realm_regex` matching the Dahua-family realm. With rc2.2, the
the Lorex DVR Lorex Layer 2 short-circuits in <100ms instead of grinding 50s.

**4. `_DB_ENTRIES_BY_KEY` dict→list refactor + NVR tie-breaker
(Issue D).** Two compounding bugs caused CrystalHeeler's Hikvision
DS-2DE4A425IW-DE PTZ to misclassify as "Hikvision NVR":

  • **Architecture bug:** `_DB_ENTRIES_BY_KEY: dict[str, dict]` —
    when a keyword appeared in MULTIPLE entries, the dict overwrote
    and only the LAST entry written got credit. The Hikvision
    (single) entry at line 713 and the Hikvision NVR entry at line
    2046 both list "hikvision" as a keyword. List-order processing
    meant `_DB_ENTRIES_BY_KEY["hikvision"]` ended up pointing to
    Hikvision NVR. Single Hikvision cameras lost every haystack hit
    on the bare word "hikvision" to the NVR entry.

  • **Data bug:** Hikvision NVR's `onvif_scopes` field included
    `"onvif://www.onvif.org/Profile/Streaming"` — the standard ONVIF
    Profile S spec identifier returned by EVERY Profile-S-compliant
    ONVIF device on the planet. Per ONVIF Core Spec it carries zero
    brand-specific signal. Including it scored Hikvision NVR +1 for
    any ONVIF haystack (which is most haystacks).

Combined effect for the Hikvision PTZ: scored "Hikvision NVR=2, Hikvision=1"
→ NVR wrongly won. With rc2.2 fixes: "Hikvision=4, Hikvision NVR=0"
→ correctly identified as single Hikvision camera.

The refactor changes the lookup type to `dict[str, list[dict]]` so
keywords can score all entries that legitimately claim them (e.g.,
both Hikvision and Hikvision NVR get +1 from "hikvision"). Two
additional safeguards:

  • Per-keyword entry deduplication: a keyword that appears in
    MULTIPLE fields of the same entry (e.g., "hikvision" in 6 fields
    of the Hikvision entry) counts as ONE keyword match for that
    entry, not six. Otherwise scoring would skew massively toward
    entries that repeat keywords across fields.

  • NVR-family tie-breaker: when top score is shared by entries that
    include and exclude NVR-family designation, prefer non-NVR. The
    NVR entries are the SUPERSET case (they need extra signals like
    series-specific keywords — DS-77xxx, Turbo HD, app-webs/, RLN8 —
    to win). A bare brand name like just "hikvision" or "reolink"
    should default to the single-camera entry, not the NVR.

Audit completed across all 76 CAMERA_DB entries: only ONE truly
generic onvif_scope was found and removed (Hikvision NVR's
Profile/Streaming). Other shared keywords across entries
(hikvision/hikvision, reolink/reolink, swann/swann, etc.) are all
legitimate brand signals, properly handled by the dict→list refactor.

**5. Yellow dot for `authenticating_throttled` UI state (small
follow-up).** `authenticating_throttled` is a transient state during
the rate-limited auth window (~30s on `rate_limit_per_ip_tcp` brands
like Microseven). Previously rendered as 'dot-error' (red) which
suggested failure even though authentication was still in progress.
Now renders as 'dot-warning' (yellow) like other transient states.
CrystalHeeler observed this during rc2.1 testing: "saw the same failure for
about 3-5 seconds and then it corrected, the indicator dot went from
red to green."

### Files changed

- `camera_discovery.py`:
  - `CAMERA_RELEVANT_PORTS` +3 classifier ports (22, 631, 9100)
  - `focused_nmap_scan` rewritten without `-sV`, `--host-timeout` 30s→15s
  - `NON_CAMERA_KEYWORDS` +1 entry ("ipp")
  - `classify_device` port-based not_camera fallback added
  - `_DB_ENTRIES_BY_KEY` type refactor + dedup
  - `identify_manufacturer` rewritten with NVR tie-breaker
  - Lorex/Dahua DVR-NVR Family CAMERA_DB entry +`skip_layer2: True`
  - Hikvision NVR CAMERA_DB entry: removed generic ONVIF Profile/Streaming
  - `dotClass` JS function: yellow for `authenticating_throttled`
- `config.yaml`: version bump
- `CHANGELOG.md`: this entry

### Live-test target

Re-run scan on:
- test system B (Lorex DVR + an HP printer + an IoT device):
  expect Lorex Layer 2 to skip <100ms; the printer rejected as
  not_camera (port 631 or 9100 detected); focused scan completes in
  ~3-5s instead of ~73s.
- test system A (Microseven + Hikvision PTZ): focused scan now
  returns hosts (was 0 in rc2.1); the Hikvision card title shows "Hikvision"
  not "Hikvision NVR"; cred-auth on the Microseven still works; dot color during
  the rate-limited auth window is yellow not red.

## 2.4.0-rc2.1
**Bug-fix release on top of rc2.0. (1) Fixes a regression in the rc2.0
focused nmap scan where the brand-port augmentation silently shrunk the
scan to ~4 ports because nmap's `-p` and `--top-ports` flags intersect
rather than union. (2) Replaces the entire approach with an explicit
camera-relevant port list of 51 ports, ~19× faster than `--top-ports
1000` while catching every brand documented in CAMERA_DB plus generic
alt-HTTP/HTTPS ports used as common practice across manufacturers. (3)
Fixes 3 UI/state bugs from rc2.0 live testing: Lorex DVRs displaying as
"General" on the card, cred-auth failure collapsing the card with no
login form on rate-limited brands, and Lorex grinding 50s through Layer
2 instead of short-circuiting on `skip_layer2`.**

### What changed

**1. `focused_nmap_scan` redesign — explicit camera port list (Issue 2,
regression from rc2.0).** Replaces `--top-ports 1000 -p <6 brand ports>`
with `-p <51 explicit camera-relevant ports>`. rc2.0's approach silently
shrunk the scan because nmap intersects `-p` with `--top-ports`: of the
6 augmented ports, only 4 happened to be in the top-1000 list, so the
final scan probed 4 ports per host instead of the intended 1006. The
An IoT device (test system B) was discovered in rc1.0 baseline
on ports 80/443/8080/8291 plus 80/443/8888, but disappeared entirely in
rc2.0 because none of the 4 actually-scanned ports happened to be open
on those hosts. Empirically confirmed on CrystalHeeler's HAOS via direct nmap
invocation: `nmap --top-ports 1000 -p <list>` returns 4 ports;
`nmap --top-ports 1000` alone returns 1000; `nmap -p <list>` alone
returns the full list. nmap GitHub issue #447 documents this since 2016.

The new list (`CAMERA_RELEVANT_PORTS`, defined directly above
`focused_nmap_scan`) is the union of (a) CAMERA_DB `default_ports`
across all 78 entries (22 ports), and (b) ports documented by
manufacturer/VMS-vendor/industry sources NOT yet represented in
CAMERA_DB (29 ports). Research foundation: 80+ authoritative sources
including Hikvision-official Network Port List PDF (10554 alt-RTSP,
9010/9020 Ezviz), Bosch knowledge-base (1756/1757/1758 RCP+),
Hanwha Vision America KB (4520-4524 SUNAPI), Reolink official KB
(1935 RTMP, 9000 basic service), Pelco Developer Network
(49152-49156 Endura svc-tcp), help.ui.com Required Ports Reference
(7442/7443/7444/7446/7550 UniFi Protect), support.networkoptix.com
(7001 Nx Witness), Blue Iris HouseLogic docs (81 default web), plus
generic alt-HTTP/HTTPS ports (8081, 8082, 8443, etc.) confirmed across
multiple manufacturers as common-practice deployment.

Trade-off: an exotic camera on a truly weird port (e.g., 12345) will
be missed by the focused scan. In practice this is vanishingly rare —
camera firmware almost always picks ports in HTTP-adjacent or
RTSP-adjacent ranges. Devices on weird ports that ARE on the network
still appear in the ARP-discovered live-hosts list; they just have no
service info attached. For users with confirmed-exotic cameras, the
broad scan (0-10000) remains available.

Speedup: typical ~5-10s for 14 hosts vs ~80s in rc2.0/rc1.0 — a real
UX improvement on every scan.

**2. UI fix — generic ONVIF Name="General" (Issue 1).** The Lorex DVR
on the Lorex DVR (test system B) was displaying the card title as "General" — the
literal Name field returned by the device's ONVIF GetDeviceInformation
SOAP call. Backend brand-id correctly identified it as Lorex/Dahua
DVR-NVR Family (via mac_vendor or page-title signals), but the
`_isGenericCamName` regex didn't recognize "General" as a generic name
worth swapping out for the brand-identified manufacturer. Added
"general" and "generic" to the regex anchor list so the card now
displays "Lorex / Dahua DVR-NVR Family" instead. Anchored regex still
preserves user-given names that happen to contain "general" (e.g.
"General Office Camera").

**3. UI fix — cred-auth failure collapse on rate-limited brands (Issue
4).** When the user submitted wrong credentials for the Microseven
(rate_limit_per_ip_tcp throttle_type), the card collapsed to a no-form
state showing only a Remove button — leaving the user no path to
re-enter credentials without first deleting and re-discovering the
camera. Root cause: `api_set_credentials` mutates `camera["status"]` to
`"authenticating_throttled"` on entry (to surface the rate-limit
warning text in the UI during the ~30s auth window), but the failure
return path didn't restore the status. Next /api/cameras poll returned
`status="authenticating_throttled"`, `credFormHTML`'s `cam.status !==
'needs_credentials'` check fired false, and the form rendered empty.
Fixed by restoring `status="needs_credentials"` and clearing
`status_text` in the failure-return path. Only affected
rate_limit_per_ip_tcp brands (Microseven Hipcam, Sricam, Vstarcam,
Wansview, Tenvis families) since non-throttled brands never had the
status mutation applied — explaining why the Hikvision didn't show
the same symptom.

**4. Performance fix — Layer 2 short-circuit on `skip_layer2` brands
(bonus).** Brands marked `skip_layer2: True` in CAMERA_DB (currently
the Lorex/Dahua DVR-NVR Family) had Layer 2 grinding 50+ seconds
through 10 sockets × 5s sleep before bail-after-10 fired — the wrong
behavior for multi-channel DVRs where the right answer is "use the
streaming_recipe with channel iteration" (consumed in rc3.x). Added
a Layer 2 short-circuit alongside the existing
`rate_limit_per_ip_tcp` short-circuit, with log line distinguishing
the two skip reasons. Lorex DVR live-tested: Layer 2 now skipped
in <100ms instead of 50s+.

### Research foundation (rc2.1 port list)

80+ authoritative sources spanning manufacturer-official
documentation, support knowledge bases, VMS vendor docs, and industry
deployment guides. Source quality bar: only manufacturer
documentation, VMS vendor official KBs, industry standards bodies,
and trade publications — explicitly excluded individual forum posts
of the "I always use port X because reasons" variety. Full
bibliography in `/home/claude/research_2_4_0_rc2_1/findings_so_far.md`
and the rc2.1 audit report.

### Files changed

- `camera_discovery.py`: +1 constant (`CAMERA_RELEVANT_PORTS`), 4 fixes
  (focused_nmap_scan body, _isGenericCamName regex, api_set_credentials
  failure path, find_rtsp_path Layer 2 short-circuit)
- `config.yaml`: version bump
- `CHANGELOG.md`: this entry

### Live-test target

Re-run scan on:
- test system B (Lorex DVR + an IoT device):
  expect Lorex card to show as "Lorex / Dahua DVR-NVR Family", the
  IoT device to reappear in scan results, Lorex Layer 2 to skip <100ms.
- test system A (Microseven + Hikvision): submit wrong creds to
  the Microseven, confirm form remains visible with error text instead of
  collapsing.

## 2.4.0-rc2.0
**Adds Layered Stream Discovery (continue path-walker after first success
to surface 401-locked stream candidates), syncs spreadsheet v9 →
CAMERA_DB (8 new NVR/DVR family entries with `streaming_recipe`,
plus the existing Lorex/Dahua family backfilled with the same field),
and augments the focused nmap port list with 6 brand-specific ports
not in nmap's top-1000 (UniFi Protect 7441/7447, GeoVision 4550/8765,
Dahua-variant 34567/35000). Code-side feature work is the path-walker
mod + UI badge/modal; the CAMERA_DB additions are pure data (consumed
in rc3.x).**

### What changed

**1. Layered Stream Discovery — `_probe_rtsp_paths_single_socket` extended
with `collect_locked` + `expected_realm` parameters.** When `collect_locked=True`,
the walker no longer bails on the first working unauthenticated URL.
Instead it captures the first working URL and continues walking remaining
paths in the candidate list, examining each subsequent DESCRIBE 401
response and adding the path to a locked-stream-candidates list when
the response's realm matches `expected_realm`. The first working URL is
still returned as the function's primary `found` value; the locked list
is persisted to `host_meta["locked_streams"]` (alongside the existing
`server_header` capture) so the orchestrator and UI can read it back.

**2. `find_rtsp_path` opt-in to locked-stream collection.** Enabled when
all three conditions hold: (a) no credentials supplied to this call —
authenticated camera traffic is handled by the cred-auth flow which
enumerates streams directly; (b) brand is NOT marked `skip_layer2` —
fragile-multi-attempt brands (per-IP TCP rate-limit, lockout counters)
get one careful walk only, no extra DESCRIBEs after first success; (c)
`host_meta["rtsp_auth_realm"]` was captured by the rc1.0 OPTIONS
fingerprint helper — without a realm to match against we'd surface
locked streams that may need different credentials than the camera's
primary auth domain.

**3. Camera record lifecycle plumbing.** `_safe_cam` defaults
`locked_streams` to `[]`. `_id_preserve` (the cred-auth dict-replacement
preserve-list) carries `locked_streams` through credential acceptance.
Both record-build sites (`_probe_host_port::base()` and the ONVIF
post-scan record build at the unauth-RTSP and needs-credentials
branches) copy the field from the in-flight `onvif_meta`/`host_meta`
dict onto the persisted camera record. UI receives the list as
`cam.locked_streams`.

**4. UI: "🔒 N Locked Stream(s)" badge + modal.** When `cam.locked_streams`
is non-empty AND the camera has no stored credentials, a clickable
purple badge appears in the camera card's badge row. Clicking the
badge opens a modal listing each locked path with its realm and auth
scheme, plus a credential entry form (username + password). Submitting
credentials reuses the existing `/api/credentials` endpoint — the
server-side cred-auth flow re-attempts path-walking with the supplied
credentials, automatically authenticating against any same-realm
locked paths since they're already in the path candidate list. After
success the badge disappears (camera now has `has_credentials=true`)
and the unlocked streams populate the standard `stream_url` /
`sub_stream_url` fields.

**5. CAMERA_DB sync from spreadsheet v9.** 8 new NVR/DVR family entries
added with `streaming_recipe` populated:
  - **Hikvision NVR:** `/Streaming/Channels/{ch}{st:02d}` (1-based ch, st=01 main / 02 sub)
  - **Hanwha NVR:** `/LiveChannel/{ch}/media.smp/profile={st}` (**0-based** ch, **port 558** not 554)
  - **Uniview NVR:** `/unicast/c{ch}/s{st}/live` (1-based, s=0 main / s=1 sub)
  - **Reolink NVR:** `/Preview_{ch:02d}_{st}` where st="main"/"sub" (1-based RTSP, **0-based snap CGI** → `snap_channel_offset=-1`)
  - **Vivotek NVR:** `/Media/Live/Normal?camera=C_{ch}&streamindex={st}`
  - **Amcrest NVR:** Dahua-OEM, `/cam/realmonitor?channel={ch}&subtype={st}`
  - **Swann NVR:** Hikvision-OEM (newer), Raysharp fallback `/ch{ch:02d}/{st}`
  - **ANNKE NVR:** Hikvision-OEM, Dahua fallback `/cam/realmonitor?channel={ch}&subtype=0`

The existing Lorex/Dahua DVR-NVR Family entry is backfilled with the same
`streaming_recipe` field to make the format consistent across all 9
NVR/DVR families (was previously only documented in the spreadsheet's
Streaming Recipe column for that one row).

**Brands deliberately left without `streaming_recipe` (HIGH-confidence rule):**
Honeywell, FLIR Commercial (mixed OEM); EZVIZ, Wyze, Ring, Nest, Arlo,
Blink (cloud-only); Verkada (cloud-only Vivotek hardware).

**6. Focused nmap scan augmented with 6 brand-specific ports.**
`focused_nmap_scan()` now passes `-p 4550,7441,7447,8765,34567,35000`
alongside `--top-ports 1000`. These cover GeoVision (4550, 8765), UniFi
Protect (7441, 7447), and Dahua-variant admin ports (34567, 35000) that
aren't in nmap's standard top-1000 list. nmap deduplicates the union,
so the only cost is 6 additional port probes per host. Existing nmap
flags (`-sV --open --host-timeout 30s -T4 -oX -`) unchanged.

**7. CAMERA_DB hits 76 entries** (was 68 in rc1.0). All 5 release gates
pass with all confidence-paired correctly. `verify_release.py`
`DATA_FIELDS` extended to recognize `streaming_recipe` as an optional
data field with `streaming_recipe_confidence` sibling.

### Research foundation

17 manufacturers researched across 63+ unique sources (manufacturer-
official documentation, support knowledge bases, third-party VMS
docs, user forums, GitHub discussions). All HIGH-confidence findings
only — anywhere data was mixed across firmwares/OEMs the entry was
deliberately left without a recipe rather than guess. Full
bibliography in `Layered_Stream_Discovery_Plan.md` and
`AnyCam_RTSP_Stream_URL_Database_v9.xlsx` Legend changelog.

### Live test expectations

- **Microseven Hipcam:** unauth URL still works (existing behavior).
  No `rtsp_auth_realm` captured (Hipcam returns `realm=None` per rc1.0
  test), so `enable_locked_collect` is False — no continued walk, no
  extra grinding against the rate-limited camera. Identical behavior
  to rc1.0.
- **Hikvision DS-2DE4A425IW:** unauth URL works on 101. With realm
  captured (`"IP Camera(...)"`) and brand `skip_layer2=False`, continued
  walk runs; if the camera's RTSP server emits 401 on /102, /103,
  channel-zero, or other DB paths (with same-realm), they'll surface as
  locked candidates. UI badge shows count, modal lists them, user can
  enter creds to unlock.
- **Lorex DVR:** brand identified by realm regex (rc1.0). Brand has
  `skip_layer2=True`, so `enable_locked_collect=False` — no continued
  walk. Lorex DVRs use channel iteration which is rc3.x territory; for
  rc2.0 we deliberately don't grind against the lockout-prone DVR.

### Non-goals (deferred to later builds)

- **Brand-recipe path discovery during initial scan** — consuming
  `streaming_recipe` to short-circuit Layer 1's generic path walk.
  Deferred to rc3.x where the Lorex/Dahua DVR Family work needs it
  for channel iteration.
- **Channel iteration logic** — DVR/NVR channel-by-channel probing,
  populated_channel_test SDP parsing. rc3.x.
- **STREAM_DB → CAMERA_DB consolidation.** rc4.x.
- **Runtime consumption of `default_ports`** — per-camera port preference.
  rc3.x+.

---

## 2.4.0-rc1.0
**Adds the `_rtsp_options_fingerprint` helper that opens a single TCP
socket, sends one RTSP OPTIONS request, and captures Server header,
auth realm, auth scheme, and Public methods. Wires this fingerprint
into the discovery flow at two sites and extends the brand-id pipeline
to score on `rtsp_realm_regex`. Pure additive infrastructure — no
behavioral changes to existing camera handling.**

### What changed

**1. New helper: `_rtsp_options_fingerprint(host, port=554, timeout=3.0)`.**
Opens one TCP socket, sends an RTSP OPTIONS request with no User-Agent
(keeps the request minimal — avoids any User-Agent-based filtering some
servers might do), reads up to 4KB of response or until `\r\n\r\n`,
parses the status line + WWW-Authenticate + Server + Public headers.
Returns a dict with `status`, `looks_like_rtsp`, `server_header`,
`auth_scheme`, `auth_realm`, `auth_algorithm`, `public_methods`,
`cseq`, `raw_response`, `elapsed_ms`, `error`. Failure-tolerant:
timeouts, connection refused, non-RTSP servers, multi-line continuation
headers all handled without raising.

**2. Wired into the discovery flow at two sites.** First, in the ONVIF
post-scan path, right after `probe_http_identity` runs — populates
`onvif_meta` with `server_header`, `auth_realm`, `auth_scheme`, and
`rtsp_public_methods` when the fingerprint succeeds. Second, in
`_probe_host_port` for non-ONVIF discoveries (nmap-only, mDNS) — same
field-population, before path-walking starts.

**3. Brand-id pipeline now scores on `rtsp_realm_regex`.** When a
CAMERA_DB entry has the new optional `rtsp_realm_regex` field (regex
pattern matched against the captured `auth_realm`), `_identify_camera_brand`
treats a match as a HIGH-confidence signal — realm strings are
server-baked and not user-customizable. The Lorex/Dahua DVR-NVR Family
entry now has `rtsp_realm_regex: r"^Login to [0-9a-f]{32}$"` populated.

**4. New camera-record fields.** `rtsp_server_header`, `rtsp_auth_realm`,
`rtsp_auth_scheme`, `rtsp_public_methods` added to the camera dict and
to the safe-camera redaction allowlist. The Identity panel displays
`RTSP Auth Realm` and `RTSP Server` when populated.

### Cost / risk

- One extra TCP open per host during ONVIF post-scan. ~3s timeout,
  expected <0.05s for a responsive host. <1s added to the typical
  15-host scan on a healthy network.
- Zero risk to throttled hosts (Hipcam family): the fingerprint probe
  IS the first TCP open, no preceding probe to collide with.
- Zero risk to lockout-throttled hosts (Lorex/Dahua DVR family):
  OPTIONS doesn't authenticate, just gets the 401 challenge. No counter
  increment.

### Files modified

| `camera_discovery.py` | New `_rtsp_options_fingerprint` helper |
| `camera_discovery.py` | Wire into ONVIF post-scan + non-ONVIF flow |
| `camera_discovery.py` | Extend `_identify_camera_brand` for rtsp_realm_regex |
| `camera_discovery.py` | New camera record fields + redaction list |
| `camera_discovery.py` | UI: Identity panel new fields |
| `config.yaml` | version 2.3.2 → 2.4.0-rc1.0 |

---

## 2.3.2
**Fixes the actual root cause of the Microseven validator-400 bug — the
validator was connecting to the wrong port — and reverts the 2.3.1
hotfix that was working around the symptom without addressing it.**

### What changed

**1. Validator now uses the actual RTSP port from the profile URL.**
The 2.3.0 single-socket validator at `_validate_rtsp_urls_single_socket`
opens its TCP socket via `socket.create_connection((host, port))` where
`port` is passed in by the caller. In `api_set_credentials` line 6798
(2.3.1), this argument was the camera's stored `port` attribute —
which is the **discovery port**, not the RTSP port. For Hipcam/Microseven
and other HTTP-discovered cameras, that's port 80, not 554.

The validator was therefore opening TCP to `10.0.0.22:80` (the camera's
HTTP admin server) and sending the request:

    OPTIONS rtsp://10.0.0.22:554/11 RTSP/1.0
    CSeq: 1

Port 80's HTTP server saw `OPTIONS` (a valid HTTP method) followed by
`RTSP/1.0` (an invalid HTTP version) and responded with the textbook
HTTP rejection: `HTTP/1.1 400 Bad Request`. The HTTP/1.0 connection
then closed, so the second URL on the same (now-dead) socket got an
empty response. The validator dutifully logged `0/2 OK` every time, but
the existing fallback ("ONVIF confirmed creds → including anyway")
masked the symptom — streams came up via ffprobe, so nothing visible
to the user was broken.

This bug has existed since 2.3.0. The validator has **never** worked on
HTTP-discovered cameras, including the Microseven we've been
debugging for weeks.

Confirmed empirically via terminal-side tests on the live the Microseven:
  * **Test A** (1 SOAP → 1s → OPTIONS to :554) — `RTSP/1.0 200 OK`
  * **Test B** (4 SOAPs → 1s → OPTIONS to :554) — `RTSP/1.0 200 OK`
  * **Test C** (4 SOAPs → 1s → full validator-style multi-URL walk
    with Digest auth nonce reuse, all on ONE socket to :554) —
    **2/2 URLs validated successfully in 1.6 seconds**.

**Fix:** parse the actual RTSP port from the first profile URL via
`urlparse(profile_urls[0][1]).port` and pass that to the validator
instead of the camera's stored discovery port. The parse is wrapped in
try/except for ValueError/AttributeError (ONVIF responses are
untrusted input, malformed URLs fall back to the RTSP default 554).
A diagnostic log line surfaces when the parsed port differs from the
stored port — making the fix's effect visible in install logs.

**Also added a defense-in-depth check inside `_validate_rtsp_urls_single_socket` itself:**
the validator now warns if the host or port parsed from the first URL
doesn't match the host:port it was passed for the socket connection.
This catches the same class of caller bug at the validator boundary —
any future caller misconfiguring the connection target gets an
immediate WARNING in the logs rather than silent 0/N validation
results masked by fallback paths.

**2. Reverted the 2.3.1 throttle hotfix.**
The hotfix added `_THROTTLE_TRACK[ip] = time.monotonic()` after each
ONVIF SOAP call (port 8080) plus a `_throttle_wait_if_needed` call
before the validator, on the theory that SOAP TCP opens on :8080 were
racing the validator's TCP open on :554 inside the per-IP rate-limit
window. With the actual root cause known (port bug), the hotfix's
premise is wrong:
  * The 2.3.0 validator was never opening TCP to :554 in cred-auth on
    Hipcam-family cameras — it was opening TCP to :80. The race the
    hotfix tried to prevent didn't exist.
  * The hotfix added a 5-second `_throttle_wait_if_needed` delay before
    the validator that fixed nothing (the validator still got 400
    every single time) but degraded downstream timing — particularly
    visible in the install log: codec correction failed all 3 attempts
    on the Microseven, ffmpeg launched with the wrong codec (h264 vs hevc),
    Enhanced View dropped to HTTP-snap fallback, resolution greyed out.
  * Empirically, Test C confirms that 4 rapid SOAPs to :8080 followed
    by 1 second of wall-clock time then a full multi-URL validator
    walk on :554 succeeds completely. No pre-validator wait needed.

Removing the hotfix restores 2.3.0's faster cred-auth timing on
Hipcam-family cams while the port fix above gives us actual working
validator output (2/2 OK instead of 0/2 OK with fallback).

**3. Kept all other 2.3.1 changes:** focus-leave fast shutdown
(`proc.kill()` direct in `handle_focus_clear` + `focus_leave_kill`
flag) and H.265+ false-alarm badge clearing on clean run
(`hevc_plus_noise_confirmed` flag). Both worked correctly in
production observation.

### Files modified

| File | Change |
|---|---|
| `camera_discovery.py` | Port fix + revert 3 hotfix sites (~30 LoC net subtractive) |
| `config.yaml` | version 2.3.1 → 2.3.2 |
| `CHANGELOG.md` | This entry |

### Test plan

After install, delete + rescan the Microseven + enter creds. Expected log lines:

    [validate_rtsp_walk 10.0.0.22_onvif/onvif-profiles] (1/2) validating rtsp://10.0.0.22:554/11
    [validate_rtsp_walk 10.0.0.22_onvif/onvif-profiles] OPTIONS → OK
    [validate_rtsp_walk 10.0.0.22_onvif/onvif-profiles] DESCRIBE → 401 (Digest)
    [validate_rtsp_walk 10.0.0.22_onvif/onvif-profiles] DESCRIBE → 200 OK
    [validate_rtsp_walk 10.0.0.22_onvif/onvif-profiles] SETUP → 200 OK
    [validate_rtsp_walk 10.0.0.22_onvif/onvif-profiles] (2/2) validating rtsp://10.0.0.22:554/12
    [validate_rtsp_walk 10.0.0.22_onvif/onvif-profiles] (auth reused)
    ...
    Single-socket profile validation: 2/2 OK

Replacing the previous `0/2 OK` + fallback warnings. Total cred-auth
time should also be ~5 seconds shorter than 2.3.1.

---


**Three small fixes layered on 2.3.0: validator throttle plumbing, fast
focus-leave shutdown, and H.265+ false-alarm badge clearing.**

### What changed

**1. Validator hotfix — throttle tracker updated after each ONVIF SOAP call.**
On the Microseven (and other Hipcam-family cameras with `rate_limit_per_ip_tcp`),
the 2.3.0 validator was reliably failing on its first OPTIONS to port 554
with a malformed `'HTTP/1.1 400 Bad Request'` response. The fallback
("ONVIF confirmed creds → include anyway") covered the symptom so streams
ended up working, but the validator's job was being undermined.

Live diagnostic on the Microseven reproduced 2-for-2: each cred-auth, the validator
opened TCP to :554 within milliseconds of the last ONVIF SOAP call to
:8080. Hipcam's per-IP throttle counts SOAP TCP opens on :8080 the same
as RTSP TCP opens on :554 — to the camera, the validator's TCP open was
inside the cooldown window and got served by the wrong protocol handler.

Fix: `_THROTTLE_TRACK[ip] = time.monotonic()` after each ONVIF SOAP call
in `api_set_credentials` so the validator's `_throttle_wait_if_needed`
sees the most recent activity and waits the remainder of the cooldown
before opening on :554. ~10 lines added. No behavior change for any
brand without `rate_limit_per_ip_tcp`.

**2. Tight focus-leave shutdown — kill ffmpeg directly on Enhanced View exit.**
On the Hikvision, leaving Enhanced View was logging
`ffmpeg EOF after N frames (rc=None)` 12+ seconds after the leave event.
Diagnostic on a 60-second focus session showed: focus-leave fired
`task.cancel()` at t=64s, but ffmpeg kept producing frames for another
12s before naturally dying — visible in the log as a frame-counter that
kept advancing past the cancel call. The cancellation was never being
delivered to a sleeping await because every chunk read returned data
quickly and synchronous frame parsing dominated the loop.

The 12-second lag was visible in the UI as a laggy card return after
Enhanced View — during that window ffmpeg was still running (consuming
camera bandwidth) and the snap_loop couldn't switch back to thumbnail
mode, then there was an extra 2-second restart backoff.

Fix in `handle_focus_clear`: in addition to `task.cancel()`, directly
`proc.kill()` the ffmpeg subprocess via `state["proc"]`. Set a one-shot
`state["focus_leave_kill"]=True` flag. snap_loop's EOF branch checks it,
logs cleanly ("ffmpeg killed by focus-leave after N frames" at INFO,
not WARNING), and the post-finally code returns instead of restarting —
the next `handle_snapshot` poll spawns a fresh thumbnail-mode snap_loop
with `native_res=False`. New lag: <1 second.

**3. H.265+ red badge clears on clean-run evidence.**
The Hikvision was permanently flagged with the red H.265+ warning
badge despite NOT being on H.265+. Root cause: ffmpeg's "Multi-layer HEVC
coding is not implemented" stderr line is a known false positive on some
Hikvision streams (camera UI shows H.265, ffmpeg complains anyway).
2.3.0 had three pieces of the right behavior: detect the warning, set
`needs_fflags_discardcorrupt` for defensive workaround, and after 10
clean runs clear the discardcorrupt flag. But the clean-run logic
**never cleared `hevc_plus_warning`** — the badge flag — so once set, it
stayed forever.

Fix:
- New persistent flag `hevc_plus_noise_confirmed`. Set when a clean run
  (≥50 frames) shows the stream actually decodes — direct evidence the
  ffmpeg warning was noise, not a real codec failure.
- `_drain_stderr` now respects `hevc_plus_noise_confirmed`: if True,
  skip re-setting `hevc_plus_warning` even when the noisy stderr fires
  again. The defensive `-fflags +discardcorrupt` workaround still
  applies (harmless on a clean stream).
- In snap_loop's post-iteration code, after any run with frames ≥ 50,
  if `hevc_plus_warning` is set, clear it AND set `hevc_plus_noise_confirmed=True`.
  Persisted via `save_cameras()`.

For the Hikvision: badge will clear automatically the next time it racks up
50 frames in a single ffmpeg cycle (typically ~5 seconds in Enhanced
View, longer in thumbnail mode). For real H.265+ cameras: the badge
appears once, stays until a clean run proves the stream works
(unlikely if the camera really is on H.265+ — those usually fail to
decode entirely). Logs are unchanged.

### Files modified

| File | Change |
|---|---|
| `camera_discovery.py` | All three fixes (~50 lines net additive) |
| `config.yaml` | version 2.3.0 → 2.3.1 |
| `CHANGELOG.md` | This entry |

---


## 2.3.0
**Single-socket cred-auth refactor + throttle-aware runtime pacing.**
Eliminates the firmware-level RTSP lockout that hit the Microseven
in rc2.x by removing the multi-socket pressure inside `api_set_credentials`
and adding runtime safety nets where TCP opens still happen (ffprobe,
snap_loop ffmpeg restarts). Also implements user-designed db_probe skip
rule: skip db_probe entirely when ONVIF cred-auth already returned
≥2 working streams (or ≥1 ONVIF + a pre-existing unauthenticated stream).

### What changed

**1. New validate-all single-socket walker.**
`_validate_rtsp_urls_single_socket(host, port, urls, ...)` is a sibling
to the existing `_probe_rtsp_paths_single_socket`. Difference: walks the
full URL list and returns `dict[url, probe_ok]` instead of stopping on
the first match. Designed for AFTER cred-auth, where we have known URLs
and just need per-URL validation. Reuses the same RFC 2326 §9.1
sequential request-response mechanics, the same RFC 2617 nonce reuse
across requests on one socket, and the same Server-header capture.

**2. ONVIF cred-auth path refactored to use the new walker.**
The `for prof in profiles` loop in `api_set_credentials` no longer calls
`probe_rtsp` per profile (one TCP per profile). Instead: collect all
ONVIF profile stream URLs, pass them to the new walker as one batch
over ONE TCP socket, then iterate profiles using the validation results
dict. For a typical Hipcam camera with 2 profiles, this drops the TCP
open count from 2 (one per profile) to 1 (one for all profiles
together) — well inside the 5s rate-limit window.

**3. `_probe_db_streams` refactored to use the new walker.**
Same change: all DB-listed paths now validated through one socket
instead of one socket per path. Drops 3 sockets to 1 for typical
3-path STREAM_DB recipes.

**4. User-designed db_probe skip rule.**
After ONVIF profile validation, db_probe is skipped entirely if we
already have main+sub coverage:
  * ≥2 ONVIF profiles probed OK (typical case for 2-profile cameras), OR
  * ≥1 ONVIF profile + a pre-existing unauthenticated stream (fires
    when user manually opens cred dialog on already-streaming card to
    add auth'd profiles).
Otherwise db_probe runs as the safety net for cameras whose firmware
exposes streams ONVIF doesn't return. New log line:
`Skipping db_probe — main+sub coverage achieved (onvif_working=2, unauth_stream=False)`.
This rule, suggested by CrystalHeeler, is more universal than the original
plan's brand-specific skip — it skips db_probe for *every* camera that
doesn't need it, not just rate-limited brands.

**5. ffprobe pacing for rate_limit brands.**
ffprobe (`probe_stream_details`) opens its own RTSP socket per call —
not covered by the single-socket validator. For `rate_limit_per_ip_tcp`
brands, `_throttle_wait_if_needed(ip, throttle_s)` is called before each
`probe_stream_details` invocation. Tracker is keyed by IP, so even if
multiple threads/coroutines race, each TCP open respects the cooldown
window.

**6. snap_loop ffmpeg backoff floored at brand throttle window.**
Restart backoff was `min(2 ** min(streak-1, 4), 32)` (sequence:
2s, 4s, 8s, 16s, 32s). For Hipcam-family brands the early values
(2s, 4s) are inside the documented 5s rate-limit window. Now floored
at the brand's `throttle_amount` parsed value, so the sequence
becomes `5s, 5s, 8s, 16s, 32s` for those brands. Other brands see
no change. New log line at snap_loop init when this fires:
`SNAP [<id>]: rate_limit_per_ip_tcp brand — flooring ffmpeg backoff at 5s`.

**7. Cross-sequence runtime tracker.**
New module-level `_THROTTLE_TRACK: dict[ip, float]` mapping each IP to
the timestamp of its most recent RTSP TCP-open from any code path. The
`_throttle_wait_if_needed(ip, throttle_s, log_label)` helper checks the
tracker and sleeps the remainder of the cooldown window before
returning. Universal safety net for any code path that opens a fresh
TCP — keeps independent code paths (cred-auth, ffprobe, snap_loop
restart) from accidentally violating the per-IP rate limit even when
their individual logic looks fine in isolation.

**8. UI status text for throttled cred-auth.**
The `submitCreds` JS handler now reads the camera's `manufacturer` field
and shows `Authenticating (Camera rate-limited, ~30 seconds)…` instead
of `Verifying…` when the brand matches the Hipcam family pattern. Tells
users the longer cred-auth time is expected, not a hang. Server-side
sets `camera["status_text"]` to the same string so any future UI surface
that polls camera state has access to the same message.

### Behavior changes for affected brands

For a Hipcam-family camera (Microseven, Sricam, Vstarcam, Wansview-old,
Tenvis):
  * rc2.x cred-auth: 8 TCP opens in ~3 seconds → all fail with RST/400,
    camera enters firmware lockout requiring power-cycle.
  * 2.3.0 cred-auth: 1 TCP open for ONVIF validation + 2 ffprobe sockets
    paced at 5s apart = ~12s total, all succeed, no lockout.

For all other brands (~95% of the camera market): no behavior change
beyond a tiny per-call function lookup overhead. Brand throttle lookup
returns 0 immediately for `throttle_type` other than `rate_limit_per_ip_tcp`.

### What didn't change

* `RTSP_PATHS` list (2.2.9 rollback rule still applies).
* `CAMERA_DB` throttle field schema.
* `STREAM_DB` recipes.
* Single-socket walker for the SCAN phase
  (`_probe_rtsp_paths_single_socket`).
* `find_rtsp_path` orchestrator and its Layer 1/2 short-circuits.
* HTTP MJPEG / HLS / WebRTC / WS-RTSP probes.
* HTTP identity flow.
* `probe_rtsp` and `probe_rtsp_socket` (still used by `find_rtsp_path`
  Layer 2 fallback and by external callers that probe a single URL).

### Known latent issues carried forward (unchanged)

* `_estimateCpuPct` and `_focusWarnOK` orphaned dead code in JS (since
  2.2.7).
* Scan progress bar jumpy/inaccurate.
* Pi 4 dtoverlay auto-toggle flow — still deferred.
* Layered Stream Discovery — filed as separate future feature where
  unauth-stream-found cards continue scanning in background to surface
  a "View Locked Streams" badge.

---

## 2.2.9-rc1
**Hotfix for a crash introduced by 2.2.9's codec-clear path.** `snap_loop`
crashed with `AttributeError: 'NoneType' object has no attribute 'lower'`
on the Microseven after the camera record's `stream_codec` was set
to `None` by the runtime codec-clear logic and that `None` got persisted
into `cameras.json` (against the comment's claim of "runtime override only").
The crash happened *before* any RTSP socket opened, so the camera looked
unreachable when in fact the snap_loop never made it to the network.

### Bugs fixed

**1. `stream_codec` value of `None` crashed `snap_loop`.** Line 4582 used
`camera.get("stream_codec", "").lower()`, where the default `""` only
fires when the key is missing — not when its value is explicitly `None`.
Fixed to `(camera.get("stream_codec") or "").lower()`, which handles
both missing and `None`. This is the unblock for any existing
`cameras.json` already containing the bad value, since the persisted
state survives a fresh install.

**2. Codec-clear path wrote `None` instead of `""`.** Lines 5013/5016
in the `streak >= 5` codec-clear branch set both `CAMERAS[id]["stream_codec"]`
and `profs[0]["stream_codec"]` to `None`. The accompanying comment
("Don't save_cameras here — this is a runtime override only") is not
enforced by anything; any subsequent `save_cameras()` call (scan
completion, periodic save, shutdown) persists the `None`. Changed the
sentinel to `""` — same falsy behavior at every read site, but safe
under `.lower()` if it ever gets read before the next clear.

### What didn't change

* RTSP_PATHS list (still 31 paths) — the post-2.2.9 rollback rule still applies.
* CAMERA_DB throttle data, STREAM_DB recipes.
* qop-aware `_build_auth` from 2.2.9 (Hikvision fix).
* `_safe_cam` credential-leak fix from 2.2.9.
* All single-socket Layer 1 walker, Layer 2 fallback, brand-aware
  short-circuits, identification work from rc2.x.
* Throttle-Aware Probe Pacing — still deferred. The `None` crash is
  unrelated to throttle pacing; this hotfix just stops the crash so the
  underlying pacing question can be tackled cleanly in a future release.

## 2.2.9
**Three small, contained fixes that don't touch the streaming or scan
paths.** First general release after the rc2.x bugfix cycle.

### Bugs fixed

**1. Digest auth ignored qop="auth" challenges (RFC 2617 non-compliance).**
The Hikvision camera (DS-2DE4A425IW, realm "IP Camera(F0818)") was
intermittently emitting Digest challenges with `qop="auth"` and `stale="FALSE"`.
Historical captures of the same camera's challenge in past sessions showed
the no-qop form (`Digest realm="IP Camera(F0818)", nonce="..."`), so the
qop-required form is something the firmware switched to mid-rc2.x debugging
— probably a state-machine change after repeated probing, or a quiet
firmware tick. Either way, our `_build_auth` always used the no-qop response
formula `MD5(HA1:nonce:HA2)`, which is non-compliant when the server's
challenge advertises qop. The Hikvision rejected every cred-auth attempt, with
all 31 paths failing in the Layer 1 single-socket walk.

Fix: parse the `qop=` field out of the WWW-Authenticate header. When
present (qop="auth"), generate a fresh 16-hex-char cnonce per request,
set nc=00000001, and use the qop-aware response formula
`MD5(HA1:nonce:nc:cnonce:qop:HA2)` with `qop`, `cnonce`, `nc` included
in the Authorization header. When absent (the Microseven Hipcam, older Hikvision
firmware, every other camera in our dataset), the existing no-qop path
is preserved unchanged. Applied at both `_build_auth` sites — the one
inside `probe_rtsp_socket` and the one inside `_probe_rtsp_paths_single_socket`.

**2. Credential leak in `GET /api/cameras` per-profile URLs.** The
`_safe_cam` helper that sanitizes camera records before serializing them
to the API stripped credentials from `stream_url` and `sub_stream_url`
but not from the per-profile fields populated after authentication:
`stream_profiles[i].url` (every profile entry), and the top-level
`stream_profile_N_url` keys for middle profiles (1..N-2 when there are
3+ profiles). For an authenticated camera with 2+ ONVIF profiles, an
HTTP GET against `/api/cameras` returned URLs of the form
`rtsp://username:password@10.0.0.22:554/12` embedded inside the
`stream_profiles` array. The leak only surfaced post-cred-auth, since
unauthenticated cards have no `stream_profiles` populated.

Fix: `_safe_cam` now recurses into `stream_profiles[]`, calling
`_strip_creds` on each entry's `url` field; and walks all top-level keys
matching `stream_profile_*_url`, stripping creds from each. Both kinds of
leak were observed live by inspecting the `/api/cameras` response in the
browser during rc2 debugging.

**3. Dead JS removed from frontend.** Two leftover identifiers from
removed features (predate 2.2.7) had been carried forward across every
release without any references: a `_focusWarnOK = {}` dict declaration
(1 line) and an `_estimateCpuPct` function (12 lines). Removing both
saves a small amount of bandwidth per page load. No behavior change.

### Verification

All five `verify_release.py` gates pass: ast.parse, semantic contracts
(now including `cnonce` in both auth-aware functions and `_safe_cam`'s
new symbols), CAMERA_DB throttle/confidence-field validation, best-practice
audit, version consistency, changelog top-entry match. Smoke tests for
the qop-aware Digest formula validated against RFC 2617 §3.2.2 worked
example output.

### What didn't change

- RTSP_PATHS list (same 31 paths) — the post-2.2.9 rollback rule still applies
- CAMERA_DB throttle data, STREAM_DB recipes, single-socket Layer 1 walker
- Layer 2 multi-socket fallback (5s spacing + bail-after-10)
- Any of the existing snap_loop, http_snap_loop, or codec-fix machinery
- ONVIF SOAP, HTTP probe, MJPEG/HLS/WebRTC/WS-RTSP probes
- Cred-auth UI, scan UI, focus/enhanced view UI

### Known latent issues carried forward

- Pi 4 dtoverlay HEVC HW decode killed permanently — incompatible with
  HA add-on s6-overlay init (host_pid: true crashes container with
  "s6-overlay-suexec: fatal: can only run as pid 1"). Software decode
  remains for HEVC.
- "Unknown child process pid X" asyncio race — cosmetic, no impact
- the Microseven 4K HEVC software-decode instability — out of scope
- Scan progress bar jumpy/inaccurate — out of scope
- Throttle-Aware Probe Pacing scoped and planned but deferred — see
  `Throttle_Aware_Probe_Pacing_Plan.md` in the project root

## 2.2.8-rc2.6
**Bugfix release on rc2.5.** Three fixes prompted by live observation that
the Microseven got stuck in a 16s-backoff failure loop after a manual
tier change to 720p, with the http_snap safety-net never firing to gracefully
degrade.

### Bugs fixed

**1. Codec correction was single-shot and could fail silently.** The
`_fix_codec` task spawned at cred-auth completion did one `await
asyncio.sleep(1.0)` then a single `probe_stream_details` ffprobe call.
For cameras with per-IP TCP rate-limit (Hipcam family — 5s+ cooldown
between TCP opens), that 1-second wait was inside the rate-limit
window after cred-auth's probe sequence. ffprobe got RST'd, returned
empty, the if-check `if real_codec and real_codec != stored:` was
False, and stream_codec stayed at the wrong ONVIF-reported value
(usually "h264" when the camera actually streams HEVC) for the
remainder of the session. The wrong codec then propagated into UI
labels (Resolution dropdown shows "H264" on a HEVC camera), focus
ladder construction, and hardware-decoder selection.

Fix: retry up to 3 attempts with 5-second backoff. Stops on first
success. Logs failed attempts so the failure is observable in logs
even if all retries fail (e.g. camera firmware never exposes the
stream to ffprobe). Final attempt's failure is logged at WARNING level.

**2. Snap_loop's persistent-failure codec clear was non-native only.**
The `streak == 5` codec-clear safety net at line 4928 only fired in
non-native (background thumbnail) mode. The reasoning at the time was
"native_res mode falls back to http_snap at streak==3, so the codec
clear isn't needed there." But that reasoning assumed the codec clear
would have fired in non-native mode FIRST (before user entered Enhanced
view). Two cases broke this assumption:
- User enters Enhanced view immediately after cred-auth before any
  background failures. Codec is wrong, never cleared, native_res
  fails repeatedly.
- The streak counter inherited across sessions could skip past the
  trigger value (see Fix 3 below).

Fix: lifted the `not native_res` restriction. Codec clear now fires
in BOTH modes. A wrong codec is a wrong codec regardless of which
loop type is running — the self-heal should be available in both.

**3. Streak counter persisted across snap_loop sessions.** This was
the highest-impact bug. `state["zero_frame_streak"]` lives in the
`_SNAP[camera_id]` dict, which is intentionally persistent across
snap_loop calls (proc/task tracking depends on that). But the streak
counter is per-failure-tracking-session — it should reset when a new
snap_loop call starts.

What happened: the Microseven went into a fail loop in non-native mode after
cred-auth, hit streak >= 5 (codec cleared), eventually self-healed
on /11, frames flowed, streak reset to 0. User entered Enhanced view.
Background snap_loop cancelled, native_res snap_loop spawned. New
session inherited zero_frame_streak from previous session — which
had been bumped to elevated values during the earlier failures. When
user picked 720p, /12 started failing immediately (probably Hipcam
rate-limit on the rapid /11→/12 TCP transition, possibly /12 just
non-functional on this firmware). Each /12 failure incremented
streak: 6, 7, 8, ... never landing on the equality-match value of 3
that would have triggered the http_snap fallback. Result: stuck in
an indefinite 16-second-backoff loop, no http fallback, dropdowns
remained enabled but the stream was frozen on the last 4K frame from
before the kill.

Confirmed in live log: at 15:47–15:49 the Microseven was at restart #130–139
with `restarting in 16s` on every iteration, all firing `ffmpeg
starting profile[1] (1280x720)` followed by `Invalid data found ...
EOF after 0 frames` within ~1 second — and zero `falling back to
HTTP snap loop` log lines.

Fix (three-part):
- Reset `state["zero_frame_streak"]` to 0 at snap_loop entry. Each
  call is now a fresh failure-tracking session.
- Reset three new "fired" flags at entry: `transport_flip_fired`,
  `http_snap_fired`, `codec_clear_fired`. Each safety-net trigger
  consults these to fire at most once per session.
- Change all three streak triggers from `streak == N` equality to
  `streak >= N AND not state.fired_flag`. Defensive against any
  scenario where streak skips past the threshold value (e.g. if a
  future change to the increment path bumps by more than 1).

### What didn't change

- All rc2.5 fixes (build_authenticated_url url= parameter, snap_loop
  adaptive launch using prof.url, cardPort badge from stream_url).
- All earlier fixes through rc2.4.
- No CAMERA_DB or schema changes. No `verify_release.py` contract
  changes. No version-symbol renames.
- The Microseven `/12` sub-stream is currently non-functional in
  this user's environment (ffmpeg fails immediately on every TCP
  open even with 16-second backoffs between attempts). This is
  either Hipcam rate-limit operating below TCP layer or a firmware
  variant where /12 just doesn't work. rc2.6 doesn't fix that —
  it only ensures the system gracefully degrades to http_snap when
  /12 fails repeatedly, instead of getting stuck. With rc2.6, the
  user picking 720p on a camera with broken /12 will see: stream
  pause briefly → after ~3 failed attempts (~5–10 seconds) → switch
  to HTTP snap mode → dropdowns disable with the existing tooltip
  ("Stream switching unavailable — RTSP not accessible on this
  camera"). That's the intended graceful-degradation path.
- The dropdown rapid-flicker bug (latent, deferred at user request).
- 4K HEVC software-decode instability on the Microseven (separate camera/decoder
  issue).

## 2.2.8-rc2.5
**Bugfix release on rc2.4.** Two fixes prompted by live testing on the Microseven
Microseven where (1) manual switch back to 720p showed "Adapted Quality:
1280x720" but Actual Feed stayed at 3840x2160, and (2) the card port badge
displayed `:80` (the camera's HTTP identification port) instead of `:554`
(the RTSP stream port).

### Bugs fixed

**1. Manual tier change relaunches ffmpeg with the wrong URL when sub-stream
probe failed at credential time (primary fix).** rc2.4's proc-race fix made
the kill path fire correctly on every manual tier change — but uncovered a
second bug downstream. The relaunch was using the wrong URL.

Reproduction in rc2.4 log at 12:49:48–12:49:50:
```
12:49:48 manual tier [31] profile[1] fps=None
12:49:48 killed ffmpeg to apply manual tier change   (rc2.4 fix working)
12:49:50 ffmpeg starting (codec=hevc, sw, adaptive:uncapped profile[1] (1280x720), ...)
12:49:50 ffmpeg stderr: rtsp://10.0.0.22:554/11: Invalid data found
                                              ^^ profile[0]'s URL, not profile[1]'s
```

The label said "profile[1] (1280x720)" but ffmpeg connected to `/11` (the
4K main stream). After the relaunch, frame sizes were 2.3MB (4K JPEG
signature), not the ~340KB seen on a real 720p run.

**Root cause** — two compounding gaps:

(a) `build_authenticated_url(camera, url_key="sub_stream_url")` had a silent
    fallback: `url = camera.get(url_key) or camera.get("stream_url")`. When
    `camera["sub_stream_url"]` was None, the `or` returned the main stream
    URL. The caller had no way to detect the substitution — it got back
    "a URL" and used it.

(b) The cred-auth path at line 6268 only sets `sub_stream_url` from sub-
    stream candidates that *passed probe_rtsp*:
    ```
    ok_subs = [c for c in stream_candidates[1:] if c.get("probe_ok")]
    sub_s   = ok_subs[-1] if ok_subs else None
    ```
    For the Microseven, the first probe (MainStream, /11) opened a TCP
    socket, then the second probe (SecondStream, /12) tried within ~5s and
    got hit by the documented Hipcam per-IP TCP rate-limit:
    ```
    12:46:54 [probe_rtsp ... SecondStreamProfile] exception:
             [Errno 104] Connection reset by peer
    12:46:54 probe_rtsp OK: False
    ```
    Stochastic — the same probe sequence had succeeded on rc2.3 in an
    earlier session. So `sub_s = None`, `sub_stream_url = None`, and the
    silent-fallback substitution kicked in on every adaptive launch
    targeting profile[1].

Note that `stream_profiles[1]` was always populated correctly with
`url = "rtsp://10.0.0.22:554/12"` (line 6281 stores the URL alongside
the `_url_key` indirection). The information was there; the lookup path
just didn't use it.

**Fix:**

- `build_authenticated_url`: drop the silent fallback. Add an optional
  `url=` parameter so callers can pass a URL string directly. With
  `url_key` alone, an explicit non-default key with a missing value now
  returns `None` instead of substituting `stream_url`.
- snap_loop's adaptive launch: pull the URL from `prof["url"]` directly
  (it's already stored alongside `_url_key`), passing it via the new
  `url=` parameter. The `camera[url_key]` indirection is now a fallback
  for legacy/synthesised profile entries that may lack `url`.

After the fix, even if `sub_stream_url` is None due to transient probe
failure at cred-auth time, the manual tier change to profile[1] uses
the URL stored in stream_profiles[1] and ffmpeg gets the correct `/12`.

**2. Card port badge shows feed source port, not identification port.**
The card's port badge displayed `cam.port`, which for ONVIF-discovered
cameras is hardcoded to 80 (line 9111 — the HTTP/identification port
where ONVIF discovery happens). For an RTSP camera streaming on 554,
the badge said `:80` — misleading because the actual feed comes from
`:554` per `cam.stream_url`.

Fix: new `cardPort(cam)` JS helper that parses the port out of
`cam.stream_url` and falls back to `cam.port` only when no `stream_url`
is set (e.g. cameras still in `needs_credentials` state). Card badge
now reflects where the feed actually comes from.

### What didn't change

- Probe behavior at cred-auth — still excludes failed sub-stream
  candidates from `sub_stream_url` (the field used by the background
  thumbnail loop). The thumbnail loop wants a probe-confirmed URL
  because it polls continuously; firing ffmpeg at a broken URL there
  would loop indefinitely. Adaptive/manual mode is different — it's
  user-initiated and self-recovers via the EOF restart path, so
  trying the unverified URL is the correct trade-off.
- No CAMERA_DB or schema changes.
- `verify_release.py` contract is unchanged (no symbol renames).
- The dropdown rapid-flicker bug remains latent at user's request.
- 4K HEVC decode instability on the Microseven unchanged — separate camera/decoder
  issue, expected to improve with rc3 hardware decode.

## 2.2.8-rc2.4
**Bugfix release on rc2.3.** Two fixes prompted by live observation that
manual tier changes back to 4K silently no-op'd and the 4K-fallback toast
fired on user-driven dropdown changes.

### Bugs fixed

**1. Manual tier change silently no-ops (the primary fix).** Race
condition in `snap_loop`'s outer `finally` block. When Enhanced view
re-entry cancelled an existing background snap_loop task and started a
fresh native-res task, the OLD task's outer `finally` ran AFTER the NEW
task had already set `state["proc"] = new_proc`. The old task's cleanup
unconditionally cleared `state["proc"] = None`, clobbering the new
task's reference. Subsequent `handle_focus_set_tier` calls (manual
Resolution / FPS dropdown changes) read `state.get("proc")` to find
ffmpeg and kill it for relaunch at the new profile/fps — but the read
returned `None`, so the kill was skipped, ada was updated, and ffmpeg
kept running the previous profile indefinitely. From the user's
perspective, picking 4K from the dropdown looked correct but the stream
stayed at 720p forever.

Reproduction in rc2.3 log (11:55:38 → 11:55:49): re-entered Enhanced
view at 11:55:38, server resumed at preserved tier 31 (profile[1]
720p), task race nuked `state["proc"]`. User picked 4K at 11:55:49,
saw the `Focus: manual tier [0] profile[0]` log line but no
`killed ffmpeg to apply manual tier change` line. ffmpeg kept running
720p (frames stayed at ~360KB) for the next 4 minutes.

Fix: apply the same conditional-clear pattern that already protects
`state["task"]` to `state["proc"]`. Both fields are now only cleared
when the exiting task is `asyncio.current_task()` — preventing a
zombie cleanup from corrupting a successor's state. The same protection
was added for `state["task"]` in an earlier release for the analogous
duplicate-loop bug; missing the same fix on `state["proc"]` was an
oversight.

**2. 4K-fallback toast fires on manual dropdown changes.** The
"4K too demanding for this hardware — falling back to secondary
stream" toast was triggered whenever the `X-Step-Res` header changed
from a 4K-class resolution to anything smaller — without distinguishing
server-driven auto-degrade from user-driven manual change. When the
user clicked the Resolution dropdown to pick 720p, the toast popped up
incorrectly implying the system was overloaded, when in fact the user
was driving the change.

Fix: gate the toast on `!_manualTierActive`. When the user has
manually pinned a tier (via the Resolution / FPS dropdowns), the toast
is suppressed. The genuine auto-degrade case (adaptive controller
stepping down due to fast-death / repeated EOF when the user has NOT
touched the controls) still surfaces the toast as before.

### What didn't change

- No changes to the adaptive controller's decision logic.
  `manual_override` already correctly bypassed `fast_death` and
  `restart_overflow` checks (lines 4960, 4976) — the "locking" the
  user observed was actually the proc-race blocking the relaunch, not
  the adaptive logic ignoring `manual_override`.
- No changes to `handle_focus_set_tier` itself. The kill path was
  correct; the bug was upstream in `state["proc"]` getting nulled out
  before the kill ran.
- No CAMERA_DB or schema changes.
- The latent dropdown-flicker bug (poll loop unconditionally setting
  `sel.disabled` / `sel.title` / `opacity` every 60ms — surfaces when
  `snapMode` oscillates between rtsp and http) remains unfixed at
  user's request — flagged for rc3+ work.
- The "Unknown child process pid X" cosmetic asyncio warning remains.
- 4K HEVC software-decode instability remains — expected to improve
  with rc3 hardware decode.

## 2.2.8-rc2.3
**Targeted UI consistency fix** — addresses a UI lie where the Resolution
and FPS dropdowns in Enhanced view show stale defaults after re-entering
Enhanced view on a camera with a previously-set manual tier override.

### Bugs fixed

**1. Dropdown-state-sync (the main fix).** Server-side adaptive-tier state
(`_FOCUS_ADAPTIVE[camera_id]["manual_override"]` + `tier_idx`) is preserved
across Enhanced view sessions, but the JS dropdown state was not. When a
user closed Enhanced view and re-opened it, `_startFocusPoll` reset
`_focusCurProf = 0` and the dropdowns initialized to their first option —
while the server immediately resumed streaming the previously-selected
manual tier. Result: Resolution dropdown displayed e.g. "3840x2160 HEVC"
while the actual stream was "1280x720" (visible in the info bar). The
"Adapted Quality" indicator also failed to appear until the user manually
changed a dropdown.

Fix in two parts:

  a. Server: `GET /snap/focus/profiles` response shape changed from a
     bare profile array to an object that includes both the profiles and
     the server's current tier state:

       {
         "profiles":     [{"idx": 0, "label": "...", ...}, ...],
         "current_tier": {"manual_override": bool,
                          "profile_idx":     int,
                          "fps":             int|null}
       }

     `current_tier` is null when no tier has locked yet (camera just
     entered focus mode). The endpoint reads `_FOCUS_ADAPTIVE[camera_id]`
     and indexes its ladder by `tier_idx` to derive the current
     `(profile_idx, fps)` pair.

  b. JS: `_loadFocusProfiles` now handles both response shapes (defensive
     against split deployments and stale browser caches across upgrades).
     When `current_tier` is present, it seeds the Resolution dropdown
     from `profile_idx`, the FPS dropdown from `fps` (null → "uncapped"),
     and sets `_manualTierActive` from `manual_override` so the "Adapted
     Quality" indicator surfaces immediately on Enhanced view open.

**2. Malformed `HTTP identity:` log line.** Format string at the
HTTP-identity DB-match log call concatenated `{port}` directly to `{url}`,
where `url` is already a full URL (e.g. `http://10.0.0.22:80/`),
producing log lines like:

    HTTP identity: 10.0.0.22:80http://10.0.0.22:80/ → Hipcam/Microseven

Cosmetic-only — no other code path read this log line, the HTTP-identity
return value and side-effects were correct. Fixed to:

    HTTP identity http://10.0.0.22:80/: → Hipcam/Microseven

### Investigation results carried forward

- "Unknown child process pid X, will report returncode 255" warning
  remains. Confirmed cosmetic via code review: comes from Python's
  asyncio `MultiLoopChildWatcher._do_waitpid` race between SIGCHLD
  delivery and explicit `proc.wait()` after PIPE-attached stdout EOF.
  No zombie accumulation (the warning means the process was already
  reaped, not that it wasn't). snap_loop control flow uses `frames`
  count, not `proc.returncode`, so the synthesized `rc=255` has no
  functional effect. Future cleanup: `asyncio.set_child_watcher(
  asyncio.PidfdChildWatcher())` at app startup (Python 3.9+ Linux).

- the Microseven 4K HEVC ffmpeg EOF every ~30 frames remains. Camera-firmware /
  software-decode-cost issue; expected to improve with rc3 hardware
  decode work.

## 2.2.8-rc2.2
**Empirically-driven fix** — replaces rc2.1.1's speculation-based work.
Used Claude in Chrome to inspect the live rc2.1 install on the user's
Pi, then probed the Microseven directly via the HA terminal:

```
RTSP OPTIONS: Server: Hipcam RealServer/V1.0
HTTP HEAD /:  Server: Hipcam
HTTP body:    <title>Microseven Cameras works with Amazon Alexa</title>
              <meta name="keywords" content="Microseven Surveillance...">
```

Three confirmed identification signals — any one alone matches the
Hipcam/Microseven CAMERA_DB entry. The CAMERA_DB tuning from rc2 was
correct; the problem was elsewhere in the flow.

### Bugs fixed

**1. Cred-auth wipes identification fields.** When a user enters
credentials and the camera transitions from `<ip>_onvif` →
`<ip>_onvif_<profileToken>`, the cred-auth handler at line ~6269
does a complete `CAMERAS[cid] = {...}` dict-replacement that
silently drops `manufacturer`/`mac_*`/`page_title`/`server_header`
fields. This affected ALL cameras that needed credentials, including
the Hikvision (which rc2.1's brand-id correctly identified as
"Hikvision" during scan but lost during cred-acceptance). Fixed by
spreading a `_id_preserve` dict from the OLD record into the new
dict literal.

**2. The Microseven has no usable identification signals during scan.**
- Not in nmap_results (own per-IP TCP rate-limit defeats the focused
  port sweep), so no mac_vendor.
- ONVIF Scopes capture from rc2.1.1 didn't trigger because the Microseven's
  WS-Discovery response is sparse.
- Walker's RTSP Server header capture never fired — the unauth-RTSP
  shortcut hits 401 immediately on auth-required cameras and the
  walker doesn't get to the response-reading stage that captures
  Server. (rc2.1.1's walker change is still useful for OTHER scenarios
  — see "what didn't change" below.)

Fixed by adding an HTTP identity probe in the ONVIF post-scan loop,
called BEFORE find_rtsp_path. probe_http_identity already exists and
already runs identify_manufacturer against the response — its results
just weren't being wired in for ONVIF-discovered cameras. The function
captures Server header, page title, and runs CAMERA_DB matching on
the body. For the Microseven this matches via `Server: Hipcam` (CAMERA_DB
http_headers keyword), `<title>Microseven...</title>` (CAMERA_DB
aliases keyword), or both.

**3. Brand-id never re-runs in the cred-auth handler.** Even with
fix #1 preserving fields, if the pre-cred discovery stage didn't
identify the brand (rare but possible — slow ONVIF responses or
HTTP timeout), the cred-auth handler wouldn't try again with the
newly-acquired stream URL or other signals. Added re-id passes
after both the ONVIF profile path's dict-replace (line ~6302)
and the RTSP fallback's `camera.update()` (line ~6427). New log
line on hit: `Brand identified post-cred-auth: Hipcam/Microseven`.

### What didn't change

- rc2.1.1's walker `Server:` header capture is preserved. It doesn't
  help the Microseven's auth-required scenario specifically (the walker
  runs in find_rtsp_path discovery flow, not cred-auth flow), but
  it provides defense-in-depth for cameras with HTTP disabled,
  HTTP-behind-auth, RTSP-only firmware, or cross-VLAN configs where
  HTTP doesn't reach AnyCam.
- All earlier rc2 work (CAMERA_DB throttle fields, STREAM_DB recipes,
  single-socket Layer 1 walker, brand-aware short-circuits).
- All earlier rc2.1 work (Layer 2 fast-bail, cred-relogin reorder,
  `displayName` JS helper).
- All earlier rc2.1.1 work (direct `_identify_camera_brand` call,
  walker's Server-header capture, ONVIF Scopes capture).

### Smoke tests (all 5 pass)

- HTTP body+title combined identifies Microseven (matches the Microseven HTTP)
- RTSP `Server: Hipcam RealServer/V1.0` identifies Microseven (matches the Microseven RTSP)
- HTTP `Server: Hipcam` short form identifies Microseven (matches the Microseven HEAD)
- page_title alone identifies Microseven (defense-in-depth)
- `_id_preserve` dict captures non-empty fields, skips empties

Critical: tests 1-3 use the EXACT byte strings the Microseven returns in
production (validated empirically via the HA terminal). Not speculation.

### What this means at runtime

For the Microseven, `[INFO] HTTP identity 10.0.0.22:
'Hipcam/Microseven' (server='Hipcam', title='Microseven Cameras works
with Amazon Alexa')` should appear during the ONVIF post-scan loop,
BEFORE the RTSP probe. The card title displays "Hipcam/Microseven"
instead of "IPCAM" both before AND after cred-auth.

For the Hikvision, identification was already happening during
scan (rc2.1) but getting wiped during cred-auth. The card title
should now display "HIKVISION DS-2DE4A425IW-DE" (ONVIF name) AND
the Identity panel should show Manufacturer: Hikvision (preserved
through cred-acceptance).

---

## 2.2.8-rc2.1.1
The Microseven still showed as "IPCAM" after rc2.1. Live test
exposed two problems my smoke tests missed:

1. The Microseven wasn't in `nmap_results` at all. Its own per-IP TCP rate-limit
   defeats nmap's port-sweep — only the Hikvision responded to the
   focused scan. So `scan_meta_by_ip` had no entry for the Microseven and the
   rc2.1 `mac_vendor` plumbing silently did nothing.

2. Even if the Microseven had been in `nmap_results`, `_get_brand_throttle_info`
   in `find_rtsp_path` made a `dict(host_meta)` COPY before calling
   `_identify_camera_brand`. Brand-id wrote `manufacturer` to the copy,
   which was then discarded. The rc2.1 ONVIF wiring read
   `onvif_meta["manufacturer"]` after `find_rtsp_path` returned, but
   that field was never actually populated.

### Fixes

- `find_rtsp_path` now calls `_identify_camera_brand(host_meta)` directly
  (replacing the rc2 `_get_brand_throttle_info` indirection that made
  a dict copy). The mutation is in place — caller reads it back after
  return, as originally intended.

- `_probe_rtsp_paths_single_socket` accepts an optional `host_meta`
  parameter and captures the RTSP `Server:` header from the first
  response that includes one. Written to `host_meta["server_header"]`
  in a `finally` block so it persists on every return path (success,
  no-match, exception, socket-dead). The Hipcam RealServer firmware
  family always returns `Server: Hipcam RealServer/V1.0` even on auth-
  required responses, which directly matches the `http_headers`
  keywords in the Hipcam/Microseven CAMERA_DB entry.

- `find_rtsp_path` runs a SECOND brand-identification pass after the
  Layer 1 walker returns. If the first pass didn't identify anything
  (no `mac_vendor` available), the post-walk pass uses the newly
  captured `server_header`. This is the path that catches the
  Microseven case — its OUI may not register under "Microseven" in
  the IEEE database, but its RTSP server header reliably does.

- `onvif_discover` now captures the FULL ONVIF Scopes list from the
  WS-Discovery response (joined into a space-separated string), not
  just the `name` scope. Many ONVIF cameras populate Scopes with
  manufacturer/hardware/model identifiers
  (e.g. `onvif://www.onvif.org/manufacturer/Microseven`), which match
  CAMERA_DB `aliases` and `onvif_scopes` keywords.

- The new `onvif_scopes` field flows through:
  - `onvif_discover` → result dict
  - ONVIF post-scan loop → `onvif_meta`
  - `_identify_camera_brand` haystack
  - `_match_stream_db` and `_match_stream_db_slug` haystacks
  - persisted on `CAMERAS[cid]["server_header"]` so it shows in the
    Identity panel for diagnosis

### Smoke tests added (all pass)

- ONVIF scopes string alone → identifies Microseven
- RTSP Server header alone → identifies Hipcam family
- STREAM_DB recipe matches via either signal alone
- Walker correctly writes `server_header` to `host_meta` (or leaves
  empty when the host is unreachable)

### What this means at runtime

For the Microseven (the test camera that motivated this release):
- ONVIF discovery captures the full Scopes string (likely contains
  manufacturer/Microseven info, identifying it before any RTSP probe).
- Even if Scopes are sparse, the Layer 1 RTSP walker captures
  `Server: Hipcam RealServer/V1.0` from the OPTIONS or DESCRIBE
  response on the first path attempted.
- The post-walk brand-id pass identifies it as Hipcam/Microseven.
- The card title displays "Hipcam/Microseven" instead of "IPCAM".

### Code paths that don't change

- The CAMERA_DB structure and STREAM_DB recipes from rc2 — unchanged.
- `probe_rtsp_socket` (Layer 2's strict per-path probe) — unchanged.
- Layer 2 itself — unchanged.
- The `RTSP_PATHS` list — unchanged (2.2.9 rollback rule still applies).
- The rc2.1 fixes (Layer 2 fast-bail, cred-relogin reorder, `displayName`
  helper) — unchanged.

---

## 2.2.8-rc2.1
Bug-fix release for rc2. Four issues surfaced during the Microseven /
Hikvision live test:

1. The Microseven card still rendered as "IPCAM" (the generic ONVIF name)
   because the brand identification we added in rc2 only ran in
   `_probe_host_port`. The Microseven actually flowed through the ONVIF
   unauth-RTSP shortcut path, which never received the mac_vendor signal
   from the focused-scan stage and never persisted the manufacturer back
   to the camera record.

2. The Hikvision cred-relogin took 30-45s before recovering. The flow
   tried direct RTSP on the stored ONVIF/HTTP port (80) before falling
   through to the standard RTSP port (554). Since port 80 is HTTP and
   doesn't speak RTSP, Layer 1 walked all paths fast (each got an HTTP
   400 reply, not an RTSP reply), then Layer 2 ground through 10 sockets
   × 5s sleep before its bail-after-10 finally fired.

3. The `_probe_rtsp_paths_single_socket` walker had no way to tell its
   caller "this server isn't speaking RTSP at all" vs "this server is
   speaking RTSP but no path matched." Both cases returned `None`,
   leaving the orchestrator to spend Layer 2 retrying anyway.

4. Card titles for cameras whose ONVIF returned a generic name (IPCAM,
   Network Camera, Camera, ONVIF Device, Webcam, Video Server) had no
   way to display the more useful brand string from CAMERA_DB.

### Fixes

- `_probe_rtsp_paths_single_socket` now returns a tuple
  `(url_or_none, looks_like_rtsp_server)`. The second element is True if
  any response from the host started with `RTSP/` (any status, even 4xx),
  False if no RTSP-formatted reply was ever received. `find_rtsp_path`
  uses this to fast-bail Layer 2 when Layer 1 confirmed the server isn't
  RTSP. The two callers (Layer 1 normal walk, Layer 1 Axis-Companion
  query-param retry) both unpack the tuple. Failure paths preserve the
  current `looks_like_rtsp` state at the moment of the exception.

- The cred-relogin flow now tries port 554 BEFORE the stored port when
  they differ. Most cameras run RTSP on 554 and ONVIF/HTTP on 80; for
  cameras where the stored port is 80 (the ONVIF discovery port), this
  saves the round-trip through port 80 entirely. The Layer 2 fast-bail
  also covers this case as a safety net.

- The focused-scan stage now builds a `scan_meta_by_ip` lookup
  (`mac_addr`, `mac_vendor`, `nmap_product` per IP) immediately after
  `nmap_results` is populated. The downstream ONVIF unauth-RTSP shortcut
  uses this lookup to populate `onvif_meta` with the mac_vendor signal,
  so brand identification fires for ONVIF-discovered cameras whose ONVIF
  returns 0 profiles (the Microseven case). After `find_rtsp_path`
  returns, the manufacturer that `_identify_camera_brand` wrote to
  `onvif_meta` in place is captured and persisted to the resulting
  CAMERAS record (both the unauth-RTSP-success path and the
  needs-credentials path).

- `_probe_host_port`'s `base()` builder now copies `manufacturer`,
  `mac_addr`, and `mac_vendor` from `host_meta` onto the returned cam
  dict. Without this, the brand identification result was being silently
  lost when `host_meta` went out of scope at end of probe. The fix
  applies to all flows through `_probe_host_port` (focused-scan,
  broad-sweep, and any other call site that passes `host_meta`).

- Frontend: new `displayName(cam)` helper prefers `cam.manufacturer`
  over `cam.name` when the latter matches a generic-name regex
  (`IPCAM`, `IP Camera`, `Network Camera`, `Camera`, `ONVIF Device`,
  `Webcam`, `Video Server` — anchored full-string match). User-given
  names that happen to contain "Camera" (e.g. "Front Porch Camera") are
  NOT treated as generic. Used in `cardHTML` (main grid) and
  `openFocus` (enhanced view).

### What this means at runtime

- The Microseven now displays as "Hipcam/Microseven" in the card
  title instead of "IPCAM".
- The Hikvision cred-relogin finds the stream within ~5-10 seconds
  instead of 30-45 seconds.
- Any camera whose discovery flowed through the ONVIF unauth-RTSP
  shortcut now gets the mac_vendor / manufacturer / mac_addr fields
  populated on its camera record.

### Code paths that don't change

- The CAMERA_DB structure, throttle fields, and STREAM_DB extensions
  from rc2 are unchanged.
- `probe_rtsp_socket` (the strict per-path probe used by Layer 2) is
  unchanged. Layer 2 itself is unchanged behaviorally — only the entry
  condition into Layer 2 changed.
- The 2.2.9 RTSP_PATHS rollback rule still applies: list itself
  unchanged, brand-specific paths from STREAM_DB still prepended
  per-probe.

---

## 2.2.8-rc2
This release candidate adds a single-socket two-layer RTSP path-walking probe,
brand-aware throttle short-circuits driven by the new CAMERA_DB throttle
fields, and the long-standing OUI fix that wires mac_vendor into the
brand-identification pipeline. Built on top of rc1 (the persistent
-fflags +discardcorrupt change ships unchanged in rc2).

NOTE: rc2 is the second half of Decoding-Rev. The Pi 4 hardware HEVC decode
path (Item A) is deferred to rc3 — a UI flow needs CrystalHeeler's network access
to /boot/config.txt before that can ship.

### find_rtsp_path refactor — single-socket two-layer probe

Previous behavior (rc1 and prior): for each path in RTSP_PATHS, open a fresh
TCP socket, probe, close. ~24 sockets per camera. This breaks against the
Microseven — Hipcam RealServer firmware enforces a per-IP TCP rate-limit
(~5s cooldown) on RTSP port 554. Second connection from the same source IP
inside the cooldown window is RST'd at the TCP layer before any RTSP message
exchange. The first path's probe always succeeded; everything after failed
silently with connection-refused. Multi-socket fallbacks compounded this by
hammering the rate-limited port harder. RFC 2326 §9.1 explicitly permits
multiple RTSP requests over a single persistent TCP connection (servers MUST
queue and respond in order), so the universal-safe probe is a single socket.

New behavior (rc2):

- Layer 1: open ONE TCP socket to host:port. For each candidate path, send
  OPTIONS → read full response → DESCRIBE → read full response (including
  Content-Length-bounded SDP body) → parse SDP for first m=video track →
  SETUP (TCP-interleaved first, UDP fallback) → on 200, send TEARDOWN. On
  first successful SETUP, return the URL. Never pipelines (always reads each
  response in full before sending the next request) so the probe is safe
  against every documented camera firmware. Auth (Digest/Basic) is captured
  from the first 401 and reused across paths on the same socket — RFC 2617
  permits nonce reuse with per-method+uri response-digest recompute. CSeq
  increments monotonically across paths on the same socket.

- Layer 2: only invoked when Layer 1 finds nothing AND the brand does NOT
  have throttle_type=rate_limit_per_ip_tcp. Original per-path-per-socket
  behavior, but with `time.sleep(5)` between path attempts and bail-after-10
  on consecutive socket failures. The 10-failure cap is multi-socket-only —
  Layer 1's single-socket walker has its own short-circuits (bails on
  socket-death; per-path 4xx/5xx skips to the next path).

- Brand-aware short-circuits driven by CAMERA_DB throttle_type:
  - `no_rtsp_support` (Eufy, Arlo, Ring, Nest, Verkada): probe returns
    None immediately, no socket opened.
  - `rate_limit_per_ip_tcp` (Hipcam family): Layer 1 only, Layer 2 entirely
    skipped — multi-socket would be RST'd before completing.
  - `session_time_cap` (Reolink battery-WiFi): socket timeout extended from
    6s to 25s to accommodate camera wake-up window.
  - `requires_query_param` (Axis Companion line): Layer 1 retried with
    `?Axis-Orig-Sw=true` appended to all paths if first pass fails;
    Layer 2 retries each path with the query param on top of the standard
    URL form.

### CAMERA_DB additions — 55th brand and 8 new optional throttle fields

- New 55th brand entry: "Hipcam/Microseven" — covers Microseven, Sricam,
  Vstarcam, Wansview-old (W2/W3), Tenvis, and many cheap Chinese baby
  monitor / IPCAM rebrands that ship the Hipcam RealServer firmware family.
  Detection patterns include server header `Hipcam RealServer/V1.0` and
  `HiIpcam/V100R003 VodServer/1.0.0`, RTSP URL paths /11 /12, and digest
  auth realm "Hipcam RealServer".

- 8 new optional fields per CAMERA_DB entry: throttle_type,
  throttle_type_confidence, throttle_amount, throttle_amount_confidence,
  throttle_notes, throttle_notes_confidence, request_behaviors,
  request_behaviors_confidence. Confidence values are HIGH / MED / LOW.
  Fields are absent (not in the dict) for brands with no research findings
  rather than placeholder "none" values, so a missing field always means
  "unknown" rather than "confirmed-none".

- Throttle fields populated on ~28 brands: Hikvision, Dahua, Lorex, Reolink,
  Axis, Hanwha, Amcrest, Vivotek, Foscam, Annke, Swann, TP-Link Tapo,
  Night Owl, Nest, Ring, Wyze, Eufy, Arlo, Verkada, Q-See, LaView, Zosi,
  Sricam, Vstarcam, Wansview, Tenvis, Uniview, plus the new
  Hipcam/Microseven entry. Existing notes / default_ports updated where
  research surfaced corrections (Foscam port 88 added; TP-Link Tapo
  ONVIF port 2020 added; Reolink URL pattern documented; Hanwha multi-
  sensor URL pattern documented; Axis session timeout documented; Vivotek
  10-user limit documented; Wansview cloud-only firmware documented; etc.)

### OUI bug fix — mac_vendor wired into brand identification

For every camera identified before rc2, mac_vendor was captured from nmap
ARP scan results and stored in cam["mac_vendor"], displayed in the UI
tooltip, and... never used as a brand-detection signal. Brand-identification
relied entirely on HTTP page titles, ONVIF scopes, and nmap product banners.
For Microseven cameras (where ONVIF returns no profiles and HTTP probes
require auth) this meant brand was never identified — the camera fell
through to "Generic IP Camera" with no throttle metadata.

rc2 fix:

- New helper `_identify_camera_brand(cam, force=False)` runs identification
  against ALL available signals (mac_vendor + manufacturer + page_title +
  server_header + nmap_product + hostname + verdict_reason + onvif vendor)
  and writes the result to cam["manufacturer"] in place. Skips overwrite if
  manufacturer already set unless force=True.

- Existing `_match_stream_db` and `_match_stream_db_slug` haystacks
  extended with mac_vendor + manufacturer + page_title + server_header +
  nmap_product so STREAM_DB recipes match even when ONVIF/HTTP yield
  nothing (Microseven case).

- `_probe_host_port` now accepts an optional `host_meta` dict and runs
  brand identification BEFORE any RTSP probe begins. Focused-scan and
  broad-scan call sites assemble host_meta from the nmap host dict
  (mac_vendor, hostname, nmap_product) and pass it through. Manual-add
  flows default to host_meta=None — the probe still works, just without
  brand-aware short-circuits.

### STREAM_DB tweak

- `microseven` entry's `match` list extended to include `hipcam`,
  `hiipcam`, `hipcam realserver`, `hiipcam/v100r003` so OUI vendors that
  resolve to "Hipcam Industries" (some Microseven OUIs register under this
  name) match the same recipe.

### Code paths that don't change

- probe_rtsp_socket (the strict per-path probe) — UNTOUCHED. Layer 2 still
  uses it as-is. Layer 1 has its own walker that mirrors its strict-mode
  logic but on a persistent socket.
- All download / OUI-cache / nmap scan logic — UNTOUCHED.
- HTTP MJPEG / HLS / WebRTC / WS-RTSP probes — UNTOUCHED.
- ONVIF discovery — UNTOUCHED. Adds host_meta passthrough on the unauth-
  RTSP shortcut path so Microseven-via-ONVIF benefits from brand short-
  circuits there too.

---

## 2.2.8-rc1
This is a release candidate for testing the first half of Decoding-Rev (Item B1).
Decoding-Rev is split into two RC drops; rc2 will follow with the Pi 4 hardware
HEVC decode path (Item A) once rc1 is confirmed stable.

NOTE: The 2.2.9 release was rolled back because the RTSP_PATHS reorder broke
the unauth-RTSP shortcut on the Microseven and the Hikvision fallback
flow. rc1 is built on the 2.2.8 baseline (path order unchanged from 2.2.8).

- Item B1 — Persistent -fflags +discardcorrupt for H.265+ cameras.
  When _drain_stderr detects "Multi-layer HEVC coding is not implemented"
  in ffmpeg stderr (the Hikvision H.265+ proprietary-codec error), it now
  sets needs_fflags_discardcorrupt=True on the camera dict and persists
  this to cameras.json via save_cameras(). All future ffmpeg launches for
  that camera (both snap_loop's thumbnail pipeline and handle_stream's
  live MJPEG endpoint) prepend "-fflags +discardcorrupt" to the input
  args, telling the demuxer to drop corrupt packets instead of failing
  the whole pipeline. Most H.265+ streams remain partially decodable, so
  this turns "ffmpeg dies after 0 frames" into "ffmpeg produces video
  with occasional drops" — usable rather than broken.
- Auto-clear: After 10 consecutive ffmpeg runs producing ≥50 frames each
  (a "clean run"), the flag is removed and the next launch runs unflagged.
  This handles the case where the camera firmware is upgraded mid-life
  to fix the H.265+ bug — we don't keep the workaround forever once the
  underlying problem is gone. Partial runs that died early don't count
  as clean (those are exactly the runs the flag is supposed to be
  helping), preventing premature flag-clears. If the H.265+ pattern
  reappears on an unflagged run, _drain_stderr immediately re-sets the
  flag.
- The flag and clean-runs counter both persist across AnyCam restarts.

## 2.2.8
- RTSP probe: strict SETUP validation. probe_rtsp_socket now does a full
  OPTIONS → DESCRIBE → SDP-parse → SETUP → TEARDOWN sequence and only
  returns True if SETUP succeeds. The previous DESCRIBE-only probe was
  giving false positives on cameras that 200-OK DESCRIBE on a generic
  path but reject SETUP because no real track lives there (Microseven
  with RTSP authentication disabled was the discovery case — the camera
  acknowledged DESCRIBE on / but ffmpeg's SETUP returned 400 Bad Request
  causing infinite snap_loop restarts).
  SETUP transport is tried in order: TCP-interleaved first (matches what
  ffmpeg defaults to), UDP fallback for cameras that only accept UDP RTP.
  Track URL is extracted from the first m=video block's a=control: line,
  with support for absolute URLs, relative URLs, and "*" (which means
  use the base RTSP URL).
- RTSP path order: "/" moved from index 0 to the end of RTSP_PATHS so
  specific stream paths (/stream, /h264, /11, /Streaming/Channels/101,
  etc.) are tried before falling back to the bare root. Combined with
  the strict probe, this catches a working specific path before wasting
  a SETUP round-trip on the false-positive root.
- SigRev-2 Item 5 — Graceful shutdown handler:
  - _on_shutdown(app) registered via app.on_shutdown.append in make_app(),
    invoked when runner.cleanup() runs.
  - SIGTERM and SIGINT handlers in main() flip a module-level _STOP_EVENT.
    main() blocks on the event instead of asyncio.Event().wait(), then
    runs runner.cleanup() to fire the on_shutdown chain.
  - _on_shutdown sequence:
    1. Persist last_frame_wall (Unix epoch) to cameras.json so the UI
       can show "last seen N minutes ago" after the next restart.
       Computed by converting each camera's monotonic frame_time to
       wall-clock at shutdown time.
    2. Cancel all running snap_loop asyncio tasks.
    3. SIGTERM all live ffmpeg child processes (snap_loop and motion
       recording), wait up to 3 s, then SIGKILL any that haven't exited.
    4. _THREAD_POOL.shutdown(wait=False, cancel_futures=True) so queued
       nmap/probe_rtsp jobs don't block exit.
  - On platforms without add_signal_handler support (Windows), shutdown
    handler installation is skipped silently and the process behaves as
    before this change.
- verify_release.py: added _on_shutdown to semantic contracts (contracts
  count: 7 → 8). main() and probe_rtsp_socket contracts extended to
  pin the new graceful-shutdown plumbing and SETUP-validation behavior
  respectively.

## 2.2.7
- Card view: prefer RTSP via ffmpeg when probe_rtsp confirmed the stream
  works at credential-set time, instead of routing through http_snap_loop
  (which polls at ~1 fps and feels stale). http_snap_loop is still the
  fallback after 3 consecutive RTSP failures.
- Enhanced view: manual Resolution/Frame Rate selections that cross a
  profile or fps boundary now kill the running ffmpeg process so the outer
  restart loop relaunches with the new URL/vf filter. Previously the
  dropdown changed but the actual stream stayed the same.
- Enhanced view: dynamic toast "4K too demanding for this hardware —
  falling back to secondary stream" appears for 5 seconds when the adaptive
  controller steps down from a 4K-class profile (≥3840 wide). Replaces the
  upfront CPU-estimate pre-warning.
- Enhanced view: removed the upfront CPU-estimate pre-warning that fired
  when opening focus on a high-bitrate camera. The adaptive controller and
  the new dynamic 4K-fallback toast already cover this case.
- Profile dropdown: dedupe by (width, height, codec). Cameras like
  Hikvision often expose 4 ONVIF profiles for 2 unique streams; the
  dropdown now collapses these to one entry per unique combo.
- Initial scan: try unauthenticated RTSP on port 554 for ONVIF-only
  multicast-discovered cameras before defaulting to needs_credentials.
  Cameras with "RTSP Authentication" disabled (e.g. Microseven) now come
  up green/ready immediately, no credential prompt.
- Lock badge: replaced the 🔐 emoji credential indicator with an SVG lock
  icon. Yellow key on yellow lock for unauthenticated cameras, green key
  on yellow lock when credentials are stored.
- Header tooltips: "AnyCam — Home" text now reads "Click for Home"; the
  camera-icon SVG retains "System Stability — See Logs" via a proper SVG
  <title> child element (the previous title="" attribute was a latent bug
  — title attributes don't work on SVG elements).
- Card metadata row: removed duplicate purple "ONVIF" badge at the
  far-right of the row (the protocol badge already shows it).
- Card metadata row: red IP address now appears before green port number,
  consistent across all card types (RTSP, ONVIF, HTTP, MJPEG, HLS).
- Enhanced view: "Auto" button is now wrapped in a focus-ctrl-group so it
  aligns vertically with the Resolution and Frame Rate dropdowns above
  their labels.
## 2.2.6
- Enhanced view: fix Resolution and Frame Rate controls having no effect —
  root cause was _build_focus_ladder() returning a single-rung ladder
  [(0, None)] when CFG_ADAPTIVE_QUALITY was False (the default); every
  manual dropdown selection searched a one-rung ladder and always mapped
  back to tier 0 (uncapped); fixed by always building the full ladder —
  CFG_ADAPTIVE_QUALITY now only controls whether the system AUTO-STEPS
  down the ladder, not whether the ladder exists for manual use
- Enhanced view: gate adaptive auto-stepping on CFG_ADAPTIVE_QUALITY —
  previously the system would still step down tiers on stream instability
  even when Adaptive Quality was disabled; now combined with manual_override
  check so neither manual pins nor disabled-adaptive-quality allow stepping
- Enhanced view: add X-Snap-Mode response header ('rtsp' or 'http') so JS
  knows when the enhanced view has fallen back to http_snap_loop
- Enhanced view: disable Resolution and Frame Rate controls (greyed out,
  tooltip: 'Stream switching unavailable — RTSP not accessible on this
  camera') when in http_snap fallback mode — avoids misleading users on
  cameras like Microseven whose RTSP is non-functional; both profiles
  still visible in the dropdown so users can see what streams exist
- Enhanced view: hide Adapted Quality from info bar when in http mode —
  ladder tier selection is meaningless when http_snap_loop is serving frames
- http_snap_loop fallback: set/clear http_snap_active flag in snap state so
  handle_snapshot can report the correct X-Snap-Mode header

## 2.2.5
- Enhanced view: fix JS scope bug where _manualTierActive was declared inside
  the _startFocusPoll() closure but written by top-level focusPickRes() and
  focusPickFps() functions — those functions could not access the closure
  variable, so _updateInfoBar() always read it as false and never showed
  'Adapted Quality'; fixed by moving _manualTierActive to module scope
  alongside _focusProfiles and _focusCurProf
- Enhanced view: fix manual tier being overridden by adaptive restart logic —
  when a camera stream crashes repeatedly (e.g. Hikvision HEVC firmware bug),
  restarts_since_lock incremented past _ADAPTIVE_RESTART_LIMIT and triggered
  a tier step-down even when the user had manually pinned a specific tier;
  fixed by checking manual_override before computing restart_overflow, making
  manual pins stable across any number of ffmpeg crashes
- Confirmed via Chrome browser inspection: Resolution shows 'Stream 1' for
  Hikvision because ONVIF GetProfiles returns 0 (firmware bug) so no
  stream metadata is available; this is addressed in Decoding-Rev

## 2.2.4-rc2
- Fix: _DockerIPFilter class was lost during the 2.2.5→2.2.4 revert, causing
  NameError crash at startup before any cameras loaded; restored class definition
  alongside _LevelFilter where it belongs
- verify_release.py: added main() to semantic contracts, requiring _DockerIPFilter,
  _probe_hw_decoders, and get_startup_mode to be present — prevents this class
  of missing-definition crash from shipping again

## 2.2.4-rc1
Release candidate naming convention introduced: when reverting or re-releasing
a version with fixes, -rc1, -rc2, etc. are appended to distinguish the release
candidate from the original stable release.

This release is a corrected re-release of 2.2.4. The original 2.2.4 (SigRev-1
bundle) was followed by 2.2.5 (SigRev-2) which introduced two regressions:
  - hevc_v4l2m2m false positive: new simplified HW probe used /dev/video* for
    both h264 and hevc v4l2m2m decoders; on Pi 4 the h264 device exists but
    the hevc device (rpivid) does not without dtoverlay — causing hevc_v4l2m2m
    to be incorrectly listed as available, then ffmpeg to stall for 10-15s
    trying to initialize a non-existent hardware decoder
  - _HW_DECODER_CANDIDATES NameError: snap_loop referenced this as a global
    but it was only a local inside _probe_hw_decoders()
Reverted to 2.2.4 (SigRev-1) as base and re-packaged as 2.2.4-rc1 to
distinguish from the original stable 2.2.4.

Content is identical to the original 2.2.4 (SigRev-1 bundle). SigRev-2 will
be re-implemented correctly in a future release after Decoding-Rev is resolved.

## 2.2.4
SigRev-1 bundle (Items 1, 2, 3 — Items 4 and 7 were completed in 2.2.2/2.2.3):

- SigRev-1 Item 1 — Backoff jitter: the ffmpeg restart backoff sleep in snap_loop
  now applies ±10% random jitter (random.uniform(0.9, 1.1)) so multiple cameras
  that fail simultaneously don't all restart in lockstep and hammer the Pi together;
  added import random to module imports

- SigRev-1 Item 2 — Type hints: all 38 functions missing return annotations are now
  annotated (-> None, -> web.Response, -> ssl.SSLContext, -> str, -> int, etc.);
  added import ssl and import concurrent.futures to module imports for type accuracy

- SigRev-1 Item 3 — Thread pool cap: replaced all 44 run_in_executor(None, ...) calls
  with a named, bounded ThreadPoolExecutor(_THREAD_POOL, max_workers=12,
  thread_name_prefix='anycam'); prevents unbounded thread creation during full scans;
  12 workers chosen for I/O-bound network probing on a 4-core Pi; pool is shut down
  cleanly via _THREAD_POOL.shutdown(wait=False) when main() exits

Best-practice audit (required on this release):
  ✓ P1 Semantic contracts (6 functions verified)
  ✓ P2 No bare except clauses
  ✓ P3 No time.sleep() in async
  ✓ P4 No blocking open() in async
  ✓ P5 All functions annotated (0 missing — new)
  ✓ P6 No mutable default arguments
  ✓ J1-J4 JS checks clean
  ✓ H1-H2 HTML checks clean
  ✓ C1-C2 CSS typo / !important clean
  ✓ C3 Added dvh fallback to #storage-list max-height (body min-height
       left as-is: min-height:100vh is not affected by address-bar jump)
  ✓ PL1 No bare newlines in JS string literals (String.fromCharCode not needed)

## 2.2.3
- Bug fix: H.265+ warning badge on Hikvision card was a stale flag from
  cameras.json persisting across upgrades; verification scan RTSP-probe-fallback
  path now clears hevc_plus_warning=False whenever a camera verifies OK (was only
  cleared in the ONVIF re-auth path, which the Hikvision never reaches due to the SOAP 400)
- Bug fix: 'Stream unavailable' on card after browser tab backgrounded for 30+s;
  browser throttles/drops pending image requests while tab is hidden; those
  failures were accumulating in _snapErrors before the server could respond;
  added visibilitychange listener that resets all _snapErrors to 0 when the tab
  becomes visible again
- Bug fix: credentials exposed in logs — ffmpeg includes the full authenticated
  RTSP URL in its error messages; _drain_stderr was logging raw stderr without
  applying _strip_creds(); password now stripped before logging (SigRev-1 item 4)
- Bug fix: enhanced view for Microseven (broken RTSP) was looping ffmpeg
  crashes indefinitely instead of falling back to http_snap_loop; after 3
  consecutive 0-frame failures in native_res (enhanced view) mode, if the camera
  has an http_snap_url, snap_loop now falls back to http_snap_loop for the focus
  session; restores 2.1.9 behavior for cameras with broken RTSP
- All four fixes validated against best practices ruleset and semantic contracts

## 2.2.2
- Best-practice audit (first full audit against AnyCam Coding Best Practices doc):
  - P4 fixed: blocking open() in async def run_port_scan() replaced with
    loop.run_in_executor(None, lambda: open(xml_path).read()) so nmap XML
    parsing no longer holds the event loop
  - P6 fixed: mutable default dict {} in roundtrip() closure replaced with
    None default and extra = extra or {} guard inside the function
  - C3 fixed: #focus-img now has height:calc(100dvh - 52px - 44px) fallback
    after 100vh so mobile browsers do not jump when address bar hides/shows
- Added verify_release.py: standalone release verification script replacing the
  ad-hoc inline checks; runs 5 stages: syntax, semantic contracts, best-practice
  audit, version consistency, changelog; exits 1 on any failure
  - Semantic contract checks: 6 critical functions verified to contain their
    required identifiers (run_verification_scan, http_snap_loop, snap_loop,
    build_html, make_app, api_set_credentials)
  - Best-practice audit in script: mutable defaults, blocking open() in async,
    CSS // comments, display:flexbox typo
- Best practices ruleset (AnyCam_Coding_Best_Practices.md) established as
  permanent audit standard for all future releases

## 2.2.1
- run_verification_scan: was truncated — missing save_cameras(), SCAN_STATE
  cleanup, and the follow-up fresh network scan entirely; this caused the
  UI to show "Re-authenticating ONVIF… estimated 0:00 remaining" forever
  because SCAN_STATE["running"] was never set to False; fixed by adding
  proper cleanup block with save_cameras(), status update, await run_scan(),
  and a try/finally error fallback
- run_verification_scan: add HTTP snap URL as a third verification path —
  after RTSP probe fails, if camera has http_snap_url, does a quick GET;
  a 200 or 401 response confirms the camera is reachable; fixes Microseven
  the Microseven being incorrectly marked "Not found after upgrade"
- run_verification_scan: fix "was" version logging cosmetic bug — was reading
  load_runtime() after save_runtime() had already written the new version,
  so it always showed "was: current → now: current"; fixed by passing
  prev_version as a parameter from the call site
- http_snap_loop: persist upgraded https:// snap URL to cameras.json when
  redirect detected, so subsequent restarts use https:// directly without
  needing the redirect probe on every startup
- Note: not a 2.2.0 regression; truncation was pre-existing and not previously
  triggered because all prior installs were fresh installs skipping verification

## 2.2.1
- run_verification_scan: add HTTP snap URL as a third verification path —
  after RTSP probe fails, if camera has http_snap_url stored, does a quick
  GET to that URL; a 200 or 401 response both confirm the camera is reachable
  (401 = auth required = camera alive); fixes cameras like the Microseven
  being incorrectly marked 'Not found after upgrade' when their RTSP is broken
  but HTTP snap works fine
- run_verification_scan: fix cosmetic 'was' version logging bug — the log line
  was reading load_runtime() after save_runtime() had already written the new
  version, so it always showed 'was: 2.2.x → now: 2.2.x'; now passes
  prev_version as a parameter from the call site where it is read before
  save_runtime() is called
- Note: the 'Not found after upgrade' issue was pre-existing and not a 2.2.0
  regression; it was never triggered because all previous installs were fresh
  installs (which skip verification) rather than upgrades (Check for Updates)

## 2.2.0
- Enhanced view #1: video feed now has a 44px top margin so the X close button
  sits above the video rather than overlapping it; bottom black bar unchanged
- Enhanced view #2: Resolution and Frame Rate dropdowns are smaller and now
  have their labels ("Resolution", "Frame Rate") displayed beneath them in
  small text; FPS options renamed to include unit ("30 FPS", "15 FPS", etc.)
- Enhanced view #3: "Real Feed" renamed to "Actual Feed"
- Enhanced view #4: "Stepped Feed" renamed to "Adapted Quality"; only shown
  when adaptive_quality config toggle is on OR when the user has manually
  picked from the Resolution/Frame Rate dropdowns; _manualTierActive flag
  cleared when Auto button is pressed; CFG_ADAPTIVE_QUALITY injected from
  Python into JS at build time
- Enhanced view #5+#6: snap_loop now uses the ffmpeg pipeline (not
  http_snap_loop) when native_res=True (enhanced view) and the camera has
  a working stream_url (RTSP); fixes Resolution/FPS controls having no
  effect and fixes the 1fps ceiling — cameras with working RTSP now get real
  video in enhanced view; cameras with only HTTP snap (e.g. the Microseven) still use
  http_snap_loop in enhanced view as RTSP is not available on that camera
- Bumped to 2.2.0 (first double-digit minor version)

## 2.1.9
- ONVIF SOAP WS-Security: expanded all abbreviated namespace URIs (were using
  '...' placeholders causing malformed XML); added wsu:Timestamp block; added
  SOAP 1.1 fallback (text/xml) after SOAP 1.2 returns HTTP 400 — fixes
  Hikvision GetProfiles returning 400 Bad Request
- http_snap_loop: added one-time redirect probe (Options 1+2) — sends initial
  request with allow_redirects=False, detects http→https redirect, follows
  manually so Authorization header survives to final URL; upgrades stored
  snap_url to https:// permanently for the loop lifetime
- http_snap_loop: ffmpeg fallback (Option 4) — after 60 consecutive failures
  with no frame ever received, clears http_snap_url and exits; next
  handle_snapshot call will restart snap_loop on the ffmpeg path using the
  confirmed working RTSP URL
- Log level: replaced single LOG_LEVEL dropdown with four independent boolean
  toggles (Log Debug, Log Info, Log Warning, Log Error) in HA Config tab;
  uses _LevelFilter class to pass only enabled levels; all four read from
  environment at startup; LOG_DEBUG default off, LOG_INFO/WARNING/ERROR on
- config.yaml: all boolean options now default to false (low_fps_mode,
  limit_threads, adaptive_quality changed from true); added boot: auto so
  Start on Boot defaults enabled; log_level dropdown replaced by four booleans
- translations/en.yaml: added entries for log_debug/log_info/log_warning/
  log_error with descriptive text (Most detailed / Standard / Somewhat
  detailed / Least detailed); removed log_level entry
- System status icon tooltip: changed from "Home" to "System Stability — See Logs"

## 2.1.9
- Log level now configured in the HA add-on Configuration tab (log_level option:
  DEBUG / INFO / WARNING / ERROR, default INFO); takes effect on next add-on
  restart; reads LOG_LEVEL env var alongside all other CFG_ options at startup
- Removed the log level dropdown that was incorrectly placed in the AnyCam web
  UI header; POST /api/log_level endpoint retained for internal use
- config.yaml: added log_level option (default INFO) and schema entry
  list(DEBUG|INFO|WARNING|ERROR)
- Bumped version to 2.1.9 (was held at 2.1.8 in error across multiple releases)

## 2.1.8
- http_snap_loop: add HTTP Digest auth support — two-step flow: sends Basic auth
  first, detects WWW-Authenticate: Digest response, computes RFC 2617 MD5 Digest
  response and retries; fixes Hikvision ISAPI snapshot endpoint which rejects
  Basic auth (consistent with RTSP Digest challenge already seen in probing)
- http_snap_loop: reduce log flood — consecutive 401/error warnings now logged
  on the 1st failure and every 30th thereafter instead of every second
- UI: add Log Level dropdown to the header bar (DEBUG / INFO / WARNING / ERROR);
  calls POST /api/log_level and takes effect immediately without restart; shows
  toast confirmation on change
- api_set_log_level: new handler for POST /api/log_level; adjusts anycam logger
  level at runtime; library loggers (aiohttp, asyncio) remain at WARNING
- http_snap_loop: use TCPConnector(ssl=False) so cameras that redirect HTTP→HTTPS with a self-signed certificate (e.g. Hikvision on newer firmware) are served correctly; previously every snap attempt failed with SSLCertVerificationError
- STREAM_DB: populated snap URLs for microseven (/tmpfs/snap.jpg, confirmed
  from official ha.ivanfm.com source) and sricam/ipcam/generic (/tmpfs/snap.jpg,
  same hi3510/hi3516 chipset); Reolink snap set to CGI endpoint with URL-param
  auth; 13 new manufacturers added (Wansview, Eufy, Vstarcam, Honeywell,
  Arecont, Swann, FLIR, Digital Watchdog, Ubiquiti UniFi, Hiseeu, Sony,
  IQinVision, Verint); 200+ sources researched
- api_set_credentials: reads snap from STREAM_DB immediately after
  _match_stream_db() — no network call, pure dict lookup — and stores it as
  http_snap_url in the camera dict; also stores http_snap_auth_mode (basic or
  query_params for Reolink) and runs ONVIF GetSnapshotUri as secondary source
  when DB has no snap entry
- onvif_get_snapshot_uri(): new helper calling ONVIF GetSnapshotUri SOAP
  action; used only as fallback when db_entry is None or snap is None
- http_snap_loop(): new async function replacing the ffmpeg snap loop for
  cameras with a confirmed HTTP snapshot URL; polls at ~1 fps via aiohttp,
  writes JPEG bytes to _SNAP[camera_id]["frame"] — exactly the same buffer
  that handle_snapshot reads, so card view machinery is untouched; idle timeout
  30 s; supports both HTTP Basic auth (default) and URL-param auth (Reolink)
- snap_loop: routes to http_snap_loop when camera["http_snap_url"] is set,
  otherwise runs existing ffmpeg loop unchanged
- Logging: changed root logger level to DEBUG so all log.debug() calls are
  now visible (ONVIF parse errors, probe detail, SSDP/mDNS failures, snap
  frame counts, etc.); aiohttp, aiohttp.access, aiohttp.server, and asyncio
  library loggers suppressed to WARNING to avoid library noise drowning out
  camera events

## 2.1.7
- Fix A: RTSP transport auto-detection for non-compliant cameras. Many cheap/
  generic ONVIF cameras (Microseven, Sricam, etc.) accept the TCP RTSP SETUP
  request but reply with UDP in the Transport header. ffmpeg logs this as
  'Nonmatching transport in server reply' which surfaces as 'Invalid data found
  when processing input' — the exact error seen on camera 10.0.0.22 since
  version 2.1.0. After 3 consecutive 0-frame failures with TCP transport,
  snap_loop now automatically switches to UDP and resets the streak counter.
  If UDP also fails 3 times, it reverts to TCP. The preferred_transport is
  stored in the camera dict at runtime (not persisted to cameras.json — it
  rediscovers the right transport each addon start, which takes under a minute).
- Audit due at 2.1.9 (every other release per project rules, issues 2-10).

## 2.1.6
- Removed go2rtc entirely. go2rtc was introduced in v1.4.1 as a streaming
  backbone but was never wired to serve frames — snap_loop and handle_stream
  already use ffmpeg directly. go2rtc was running as a sidecar process that
  held the camera's single RTSP connection indefinitely, preventing snap_loop's
  ffmpeg from connecting and producing "Invalid data found when processing
  input" on cameras with a one-session limit (confirmed: 10.1.1the Microseven).
  The camera was working in v1.8.0–v1.8.3 precisely because go2rtc was
  absent in that session — snap_loop was the only RTSP client.
- Removed from Dockerfile: go2rtc binary download, BUILD_ARCH arg, wget
- Removed from run.sh: go2rtc config write, background start, readiness poll
- Removed from Python: go2rtc_add, go2rtc_remove, go2rtc_source,
  go2rtc_register_all functions; GO2RTC_PORT/RTSP_PORT/API constants;
  _go2rtc_streams global; all call sites at credential entry, camera
  deletion, ONVIF re-auth, and startup
- Retained: _fix_codec background task (runs ffprobe after ONVIF credential
  entry to detect real codec when ONVIF misreports it, e.g. "h264" vs hevc)

## 2.1.5
- Fix (definitive): Card view thumbnail polling now always uses stream_url
  (the main RTSP stream), never sub_stream_url. This is the root cause of the
  persistent the Microseven camera "Stream Unavailable" issue. History:
    - v1.7.7 introduced sub_stream_url usage for card thumbnails (CPU saving)
    - v1.8.7 implemented proper ONVIF multi-profile discovery, which for the
      first time populated sub_stream_url on the Microseven camera with the MJPEG
      sub-stream at /12
    - handle_snapshot then tried to use /12 for thumbnails — this failed because
      the sub-stream uses different codec/transport than the main stream
    - The camera had been working in card view before v1.8.7 because it was
      discovered via the db_probe path which never set sub_stream_url, so
      handle_snapshot fell through to stream_url (/11)
  sub_stream_url is now exclusively for the enhanced view adaptive focus ladder.
  The thumbnail loop already runs at fps=10,scale=640:-2, so the main stream
  is perfectly fine for card view on any hardware.

## 2.1.4
- Fix: ONVIF sub_stream_url is now always saved from the lowest-resolution ONVIF
  profile regardless of whether probe_rtsp succeeded. ONVIF authentication already
  confirms the URL and credentials are valid — "Connection reset by peer" from
  probe_rtsp just means the camera was busy during probing, not that the URL is
  broken. The v2.1.1 probe_ok filter was too aggressive and left cameras like
  10.0.0.22 with no sub_stream_url, forcing snap_loop to hammer an inaccessible
  4K main stream instead of using the manageable 1280x720 sub-stream.
- Fix: After accepting ONVIF credentials, AnyCam now runs ffprobe on the real
  stream 2 seconds later to detect the actual codec. ONVIF often reports "h264"
  when the camera is actually streaming HEVC. If ffprobe disagrees with ONVIF,
  the stored stream_codec is corrected so snap_loop builds the right ffmpeg
  pipeline. This runs as a background task so it does not block the response.
- Fix: After 5 consecutive 0-frame failures in snap_loop, the stored stream_codec
  is cleared at runtime so ffmpeg can auto-detect the codec on the next attempt.
  This catches cases where the codec correction task hasn't run yet or failed.
- Fix: build_authenticated_url now properly percent-encodes credentials per
  RFC 3986 §3.2.1 so special characters in passwords don't corrupt the URL.
  Uses urllib.parse.quote with the standard userinfo safe character set.

## 2.1.3
- Fix: fps display in enhanced view no longer flashes to 0 during the 2-second
  restart gap when H.265+ or other streams crash and restart. A 4-second grace
  period now holds the last known fps; after 4s with no new frames it shows
  measuring... instead of 0 fps.
- Includes all fixes from 2.1.2 (sub-stream fallback, Resolution/FPS dropdown
  appearance, _loadFocusProfiles empty-guard, startup migration).

## 2.1.2
- Fix: snap_loop now detects persistent sub-stream failures. After 5 consecutive
  0-frame restarts while polling the sub_stream_url, it permanently falls back
  to stream_url (main stream) and clears sub_stream_url from the camera record.
  This resolves the Microseven camera hammering /12 indefinitely when that stream
  rejects connections with "Invalid data found when processing input".
- Fix: load_cameras() startup migration now clears sub_stream_url entries that
  are identical to stream_url (degenerate duplicate saved by old code).
- Fix: _loadFocusProfiles() no longer clears the Resolution select's placeholder
  option when the server returns an empty profiles list — the "Resolution…"
  placeholder is preserved until real profile data is loaded.
- Fix: handle_focus_profiles produces meaningful dropdown labels even for cameras
  without full stream metadata (codec, resolution). Falls back to "Stream N"
  labeling instead of "Profile N".
- Fix: focus-select dropdowns now use a custom SVG down-arrow instead of the
  browser native dropdown indicator, which renders as ▲ in HA's ingress webview
  instead of the expected ▼. Selects now have appearance:none and explicit
  padding-right to accommodate the inline SVG arrow.

## 2.1.1
- Fix: AnyCam no longer creates a camera card for the HA Supervisor's Docker
  bridge IP (172.30.32.1) which mDNS was incorrectly discovering as a camera.
  All 172.x.x.x and 169.254.x.x addresses are now filtered out of the live
  host list before port scanning begins.
- Fix: AnyCam no longer probes its own ingress port (8099) on the Pi's local
  IP as a potential camera. _probe_host_port now skips port==PORT on local IP.
- Fix: Port Scan tab now shows all discovered live hosts after a network scan.
  ARP_HOSTS was defined but never populated — run_scan now fills it from nmap
  results plus any silent live hosts immediately after the focused port scan.
- Fix: sub_stream_url is now only stored when probe_rtsp actually succeeded
  for that ONVIF profile. Previously, a profile that returned "Connection reset
  by peer" was still saved as sub_stream_url, causing handle_snapshot to hammer
  a broken URL indefinitely (seen on 10.0.0.22 SecondStreamProfile /12).
- Fix: enhanced view bottom bar, Resolution/FPS dropdowns, red X close button,
  Auto button, and focus controls JS (focusPickRes, focusPickFps, focusResetAuto,
  _loadFocusProfiles) all confirmed at v2.0.5 state from session transcript v10.

## 2.1.0
- Restored all core functions lost in the v2.0.6 duplicate-section removal:
  build_authenticated_url, go2rtc_add, go2rtc_remove, go2rtc_source,
  go2rtc_register_all, probe_stream_details, _drain_stderr,
  _try_hevc_plus_fallback, run_scan, _probe_host_port, handle_stream,
  handle_stream_test — the app could start but crashed on any scan,
  credential entry, or stream operation without these
- Reconstructed from session transcripts to match original implementations:
  go2rtc_add uses correct PUT API (params=name, data=url body); go2rtc_source
  is sync and builds ffmpeg:// wrapper URLs for HEVC cameras; run_scan uses
  the real _probe_host_port helper for per-port probing; build_authenticated_url
  uses the "credentials" field (not "creds") consistent with api_set_credentials
- Added _go2rtc_streams tracking set and BLACKLIST global (loaded from disk)
- probe_stream_details uses ffprobe only (simpler and more reliable than
  going through go2rtc's stream API)
- handle_stream: real MJPEG proxy via ffmpeg pipe with hw decode support,
  proper _drain_stderr integration, and CRLF boundary headers
- _probe_host_port: real per-port probing helper supporting RTSP, MJPEG,
  HLS, RTMP, WebRTC, WS-RTSP with saved credential re-use
- _try_hevc_plus_fallback: uses "credentials" field, tries sub_stream_url
  then Hikvision path-convention sub-stream mapping
- run_scan: real 4-stage implementation preserving user-saved cameras,
  merging multicast-only ONVIF/SSDP results, and excluding BLACKLIST entries

## 2.0.7
- Fix: NameError crash on startup — _probe_hw_decoders() was only defined
  in the duplicate code section that was removed in v2.0.6. The function
  probes v4l2m2m and vaapi decoder availability at startup and populates
  _HW_UNAVAILABLE so snap_loop skips unavailable decoders. Re-implemented
  as a proper module-level async function before main().

## 2.0.6
- Post-upgrade scan now re-runs the full ONVIF authentication flow for any
  saved ONVIF camera that has stored credentials. Previously the verification
  scan only re-probed stream reachability; credential-dependent fixes (such
  as the v2.0.4 XAddrs dual-URL fix for the Hikvision PTZ) only took effect
  if the user manually re-entered credentials via Connect Camera. Now on
  every version update, ONVIF cameras with stored credentials are silently
  re-authenticated, stream profiles are refreshed, and stream_url /
  sub_stream_url / stream_profiles are all updated in-place. If re-auth
  fails (e.g. camera unreachable), the camera falls back to the existing
  basic RTSP probe and is marked unverified_after_upgrade as before.

## 2.0.5
- Fix: enhanced view adaptive ladder now correctly steps down when the stream
  produces 0 frames regardless of run duration. Previously, when ffmpeg hit
  its 30-second read timeout with zero frames decoded, run_dur ≈ 30s caused
  fast_death = (30s < 8s AND 0 < 15) = False, locking the tier as "stable"
  and displaying "0 fps" indefinitely. Now frames==0 is always treated as
  unstable and forces a step-down regardless of how long ffmpeg ran.
- Add: Resolution and FPS dropdowns in enhanced view bottom bar. The
  Resolution dropdown is populated from the camera's discovered stream
  profiles (e.g. "3840x2160 HEVC", "1280x720 H264"). The FPS dropdown
  offers Uncapped / 30 / 20 / 15 / 10 / 5 / 2 / 1 fps. Selecting from
  either locks the adaptive controller to that tier immediately (manual
  override). The Auto button clears the override and resumes adaptive
  stepping. A new server endpoint POST /snap/focus/tier accepts
  {profile_idx, fps} and GET /snap/focus/profiles returns profile metadata
  for the currently focused camera.
- Style: X close button in enhanced view is now a red circle (border: 2.5px
  solid #e03) positioned top-right at 10px/14px, styled to hover-fill red.
  No longer overlaps the camera's built-in OSD timestamp.
- Style: enhanced view info and controls moved to a fixed 52px bottom bar
  (dark background, border-top) keeping them separate from the video feed.
  Info text (camera name, real/stepped feed stats) on the left; dropdowns
  and Auto button on the right.

## 2.0.4
- Critical fix: ONVIF credential flow now works correctly for cameras that
  advertise multiple XAddrs in WS-Discovery (both IPv4 and IPv6 link-local).
  Previously, _onvif_media_url() joined all addresses into one space-separated
  string and passed the entire garbage value to the SOAP request, causing
  onvif_get_profiles() to return 0 profiles even with correct credentials.
  Now the function splits XAddrs on whitespace, skips IPv6 link-local (fe80::)
  addresses, and picks the first valid IPv4 http:// address. This fixes the
  Hikvision DS-2DE4A425IW-DE (10.0.0.33) which advertises both addresses.

## 2.0.3

### Bug fixes
- **Duplicate snap_loop after focus exit (root cause fix)**: `snap_loop`'s `finally` block was unconditionally setting `state["task"] = None` when any loop exited. When the lingering focus task finally wound down after being cancelled, it would overwrite the new card-view loop that `handle_snapshot` had already started — causing `handle_snapshot` to start yet another loop. Fixed by only clearing `state["task"]` when it still points to the current task, leaving any newer loop untouched.
- **Sub-stream codec mismatch**: when `handle_snapshot` starts a snap_loop against the sub-stream URL, it now passes a `snap_camera` dict with the sub-stream's codec/resolution instead of the main stream's. Without this, `snap_loop` would configure ffmpeg for e.g. H.264 while decoding an MJPEG sub-stream, causing "Invalid data found" errors.
- **Exponential backoff on 0-frame failures**: when ffmpeg repeatedly dies with 0 frames decoded (bad URL, wrong codec, camera rejecting connection), the restart delay now backs off exponentially: 2s → 4s → 8s → 16s → 32s (capped). Normal failures that produced at least one frame still restart at 2s. This prevents continuous hammering of a broken sub-stream URL.

## 2.0.2

### Improvement
- **Adaptive Quality toggle**: new config option (default ON) controls the adaptive profile/fps ladder in enhanced view. When OFF, enhanced view always uses the highest quality camera profile at uncapped fps with no stepping. When ON (default), the ladder steps through real camera profiles and fps tiers to find a stable setting.

## 2.0.1

### Bug fix
- **Hardware decode: use best available decoder for codec, not hardcoded Pi names** — `snap_loop` was hardcoding `hevc_v4l2m2m` / `h264_v4l2m2m` as the only hw decoder candidates. On Intel/AMD hardware, `hevc_vaapi` / `h264_vaapi` would be detected as available by `_probe_hw_decoders()` but never actually used. Now selects the best available decoder from `_HW_DECODER_CANDIDATES` (v4l2m2m preferred over vaapi) based on stream codec and probe results.

## 2.0.0

### Improvements
- **Multi-profile stream ladder**: ONVIF discovery now stores all discovered profiles as `stream_profiles` (sorted highest→lowest resolution). The adaptive focus ladder steps through real camera profiles instead of fake ffmpeg post-decode scaling. Switching to a lower profile genuinely reduces CPU decode pressure because the camera transmits fewer pixels over the network. All `scale=WxH` ffmpeg filter steps removed.
- **Hardware decode**: added `full_access: true` to config.yaml so the container can access all host hardware devices. At startup, `_probe_hw_decoders()` checks each candidate decoder (hevc_v4l2m2m, h264_v4l2m2m, hevc_vaapi, h264_vaapi) against both device file existence and ffmpeg compiled support, pre-populating `_HW_UNAVAILABLE` so snap_loop never attempts unavailable decoders. On non-Pi hardware, Pi-specific devices simply won't exist and are silently skipped.

### Notes
- Pi 4/5 hardware HEVC decode requires `dtoverlay=rpivid-v4l2` in `/boot/firmware/config.txt` and a reboot. Once set, the add-on will detect and use `hevc_v4l2m2m` automatically.
- Cameras discovered before v1.9.3 will not have `stream_profiles` stored — the ladder falls back to `stream_url`/`sub_stream_url` for those until re-discovered.

## 1.9.2

### Improvement
- **Resolution and codec detection: highest-wins arbitration** — when both probe (`probe_stream_details`) and ONVIF (`GetProfiles` XML) report values, whichever is "higher" is used. Buggy firmware tends to underreport (e.g. H264 for an HEVC stream, or 0x0 when probing fails), never overreport, so the higher value is almost always correct.
  - Resolution: highest pixel area wins (probe_w×probe_h vs onvif_w×onvif_h).
  - Codec: ranked by capability (hevc > h264 > mjpeg > mpeg4), higher rank wins.
  - Audio: ONVIF wins unconditionally (probe rarely detects audio correctly).
  - FPS: probe wins unconditionally (ONVIF's FrameRateLimit is a ceiling, not measured).
  - Log now shows `[probe]` or `[onvif]` source tag for resolution and codec per profile.

## 1.9.1

### Bug fixes
- **Post-exit step-down**: adaptive controller was firing after focus was already cleared (ffmpeg EOF fired after task.cancel() landed late). Guarded the entire step-down block with `_FOCUSED_CAMERA == camera_id` so it's a no-op once focus exits.
- **Stale restart count on re-entry**: `restarts_since_lock` was preserved across focus sessions, causing a premature step-down immediately on re-entering focus. Reset to 0 (along with `run_start`) in `handle_focus_set`.
- **Codec detection**: reverted codec to probe-first, ONVIF encoding as fallback only. ONVIF's `VideoEncoderConfiguration/Encoding` is unreliable on some Hikvision cameras (reports H264 for HEVC bitstreams). Probe reads the actual bitstream and is authoritative for codec.

### Improvements
- **Enhanced view info bar**: split into two sections — `Real Feed: WxH · fps` (measured from actual received frames) and `Stepped Feed: WxH · fps` (current adaptive ladder tier from server headers `X-Step-Res` / `X-Step-FPS`). Removed codec name and "full quality" text.
- **Uncapped tier restored**: each resolution block in the adaptive ladder now starts with an uncapped fps tier before stepping down through 30→1fps.

## 1.9.0

### Improvement
- **Adaptive ladder: fps-first per resolution block, every integer 30→1**: Each resolution block descends by 1fps per restart (30fps → 29fps → ... → 1fps) before the resolution drops. For a 4K camera with intermediate scales and a sub-stream, the ladder is:
  - 4K native: 30fps → 29fps → ... → 1fps  (30 tiers)
  - 1920px scaled: 30fps → ... → 1fps       (30 tiers, if native ≥ 2880px)
  - 1280px scaled: 30fps → ... → 1fps       (30 tiers, if native ≥ 1920px)
  - sub-stream: 30fps → ... → 1fps          (30 tiers)
  Each tier change requires exactly 1 restart at the current locked tier. Previous design had sparse fps steps and a resolution-first ordering that didn't match intended behavior.

## 1.8.9

### Bug fixes
- **Adaptive ladder: resolution-first ordering** — the quality ladder was fps-first (all 11 fps tiers at native res, then all 11 at 1920px, etc.), so stepping down always reduced fps at the same resolution before ever trying a lower resolution. Getting from 4K native to 1920px scaled required stepping through 11 fps tiers × 3 restarts = 33 restarts. The ladder is now resolution-first: for each fps tier, all available resolutions are tried before moving to the next fps tier. A single step-down now moves to the next lower resolution at the same fps, matching the expected behavior.
- **Adaptive restart limit reduced to 1** — with resolution-first ordering, a single restart at a locked tier is sufficient signal to try the next lower resolution. Limit of 3 was designed for fps-stepping (where you wanted confidence before giving up), not resolution-stepping (where each failure is immediate evidence the current quality doesn't work).
- **H.265+ flag reset on re-discovery** — `hevc_plus_warning` is now always reset to False when a camera is re-discovered, so stale flags from previous sessions or from when the wrong stream URL was stored don't carry forward. The flag is still set at runtime by `_drain_stderr` if ffmpeg genuinely encounters a multi-layer HEVC bitstream. Note: some Hikvision firmware versions output multi-layer HEVC bitstream even when H.265+ is set to OFF in the camera UI — this is a firmware bug, not a detection bug.

## 1.8.8

### Improvement
- **ONVIF as authoritative source for resolution, video codec, and audio codec**: `onvif_get_profiles` now also parses `AudioEncoderConfiguration/Encoding` from the GetProfiles XML. All three fields (resolution, video codec, audio codec) are now applied unconditionally from ONVIF data — probing via `probe_stream_details` (go2rtc/ffprobe) only contributes measured FPS, which ONVIF's `FrameRateLimit` ceiling doesn't accurately reflect. This removes the previous inconsistency where codec was "ONVIF if probe fails, else probe."

## 1.8.7

### Improvement
- **ONVIF profile selection: authoritative resolution source** — `probe_stream_details` (go2rtc/ffprobe) was the sole source of truth for resolution+codec when building the stream candidate list, but it frequently fails for H.265 streams, returning 0×0 and causing those streams to sort below lower-res streams that probe successfully. ONVIF's `GetProfiles` response already contains `VideoEncoderConfiguration/Resolution` and `Encoding` — this data is always present and never fails. Changed strategy: ONVIF is now the authoritative source for resolution and codec; probing is still used for actual FPS (which ONVIF's `FrameRateLimit` doesn't accurately reflect) and audio codec. This ensures H.265 main streams sort correctly above MJPEG sub-streams regardless of probe success.

## 1.8.6

### Bug fix
- **ONVIF profile resolution — wrong stream selected as main**: `probe_stream_details` often fails for H.265 streams (codec negotiation timeout), returning 0×0. When sorting ONVIF profiles by detected resolution, those streams sank to the bottom, causing a lower-resolution sub-stream that probed cleanly (e.g. MJPEG 704×480) to be selected as `stream_url`. Fix: `onvif_get_profiles` now also parses `VideoEncoderConfiguration/Resolution` and `Encoding` directly from the ONVIF XML — this data is always present and reliable. When `probe_stream_details` returns no resolution, the ONVIF-reported value is used instead, ensuring the sort is correct and the main high-res H.265 stream is properly selected as `stream_url`. Codec is also seeded from the ONVIF encoding field when probing fails.

## 1.8.5

### Improvements
- **Adaptive controller: restart-count instability** — added second instability signal alongside the existing fast-death check. If a locked tier restarts ≥3 times (regardless of individual run duration/frame count), the tier is considered unreliable and the controller unlocks and steps down. This catches cameras that drop RTSP connections on a regular cycle (e.g. every 20s) — previously these would lock as "stable" immediately and stay there forever.
- **Adaptive controller: intermediate resolution steps** — the quality ladder now includes scaled variants of the main stream (1920px-wide, 1280px-wide) inserted between the native-res fps tiers and the sub-stream. These are only added when the camera's native width is ≥1.5× the scale target (so never upscales). For a 4K camera this gives 4K→1920px→1280px→sub-stream instead of jumping straight from 4K to 480p.
- **Focus status bar: no more "0 fps"** — the JS FPS window now distinguishes three states: `X fps` (frames flowing), `buffering…` (had frames before, currently reconnecting), `connecting…` (waiting for very first frame). Previously any 1-second window with no new frames would show "0 fps".

## 1.8.4

### Bug fixes
- **Focus exit race — duplicate snap_loop tasks**: `task.cancel()` alone is unreliable when ffmpeg is streaming 4K data at high throughput — the event loop can't deliver `CancelledError` until a `read()` suspends, which may never happen while the pipe is full. Fixed by having `snap_loop` poll `_FOCUSED_CAMERA` itself: an explicit check at the top of the outer restart loop and after each frame parsed in the inner loop causes the native-res task to exit cleanly within one frame of focus being cleared. This eliminates the duplicate "idle 30s — stopping" entries and 4K frames arriving after focus exit seen in the log.
- **Idle timeout during focus (freeze bug)**: `_snap_last_access[camera_id]` was updated *after* the focus guard early-return in `handle_snapshot`, so the native-res snap_loop's idle timer never got reset while in focus mode. Loop died after 30s → screen froze. Fixed by updating `_snap_last_access` before the early return.
- **FPS display accuracy**: Focus status bar was measuring frame *delivery* rate (~16fps from 60ms polling) rather than actual frame *production* rate from ffmpeg. Switched focus poller from `Image()` to `fetch()` so the `X-Frame-Count` response header can be read; only increments the fps counter when the server-side frame count actually changes. Displayed fps now reflects true ffmpeg output rate (e.g. 3fps for 4K HEVC).

## 1.8.3

### Bug fix
- **Critical crash fix**: `UnboundLocalError: cannot access local variable 'url'` — when I added `url = effective_url` inside the `if native_res:` block of `_launch_snap`, Python treated `url` as a local variable for the entire function, making the outer closure variable inaccessible in the non-native_res branches. Fixed by using a separate `ffmpeg_url` variable instead of reassigning `url`.

## 1.8.2

### Focus view adaptive quality — complete rewrite

**Bug fixes:**
- **480p flickering eliminated**: handle_snapshot now returns the last buffered frame during focus mode instead of starting a competing 480p sub-stream task; the 2s restart gap no longer causes the quality controller to fight itself
- **FPS going "up" was an illusion**: the JS was measuring frame delivery rate from whichever task happened to be running — when the 480p task kicked in it delivered frames fast, making it look like fps increased; this is now impossible since only one task runs

**New adaptive quality ladder:**
- Ladder is now (resolution × fps) pairs, exhausting all fps tiers at the highest resolution before dropping to sub-stream resolution
- Tier order: main_stream @ [uncapped, 30, 20, 15, 10, 8, 5, 4, 3, 2, 1fps] → sub_stream @ [same fps sequence]
- Step-down only: never steps back up once stable
- Locks permanently when a stable tier is found; if a locked tier becomes unstable, unlocks and continues stepping down from there
- Per-camera memory: tier_idx and locked state persist across focus sessions within the same addon run

## 1.8.1

### Adaptive FPS — step-up removed
- **Step-down only**: adaptive fps controller now strictly steps down when a tier is unstable and locks there for the session — step-up logic removed entirely
- Once a stable fps tier is found, the controller stays there permanently until the user closes and re-opens enhanced view

## 1.8.0

### Adaptive FPS for enhanced/focus view
- **Adaptive fps controller**: focus/native_res mode now uses a self-tuning fps ladder `[uncapped, 10, 8, 5, 4, 3, 2]` to find the highest stable fps the hardware can sustain at full resolution
- **Step-down on instability**: if ffmpeg exits in under 8 seconds with fewer than 15 frames, the controller steps down one tier (slower fps) and relaunches — repeats until stable
- **Step-up on stability**: if a run lasts 45+ seconds, the controller steps up one tier (faster fps) to try recovering quality — drops back immediately if that tier is also unstable
- **Per-camera memory**: each camera remembers its best stable fps tier across focus sessions within the same addon run, so it converges faster on re-entry
- **Run_start reset on focus exit**: adaptive state cleans up cleanly when leaving focus view; tier is preserved but timing resets for next session

## 1.7.9

### Enhanced view improvements
- **Live resolution display**: status bar now shows actual decoded pixel dimensions from each JPEG frame (loader.naturalWidth × naturalHeight) rather than stored metadata
- **Live FPS display**: status bar counts real frame loads per second using a rolling 1-second window — updates every second with the true displayed frame rate
- **Status bar placeholder**: shows "loading…" on open then switches to live measurements after the first second
- **Fix: focus view was serving wrong stream**: handle_focus_set now always cancels the existing thumbnail snap_loop before starting a new native-res main-stream task — previously a running sub-stream task would block the high-quality task from starting
- **Higher JPEG quality in focus mode**: q:v lowered from 5 to 2 for focus/native_res frames (sharper output, less compression grain at 4K)
- **Config limits bypassed in focus mode**: Low FPS Mode and Limit Threads are now properly ignored when entering enhanced view; restored immediately on exit (snap_loop task cancelled on focus_clear)

## 1.7.8

### Bug fixes
- **Log link no longer duplicates HA sidebar**: clicking the status icon now navigates the top-level browser tab (window.top) instead of the ingress iframe, so the HA shell does not render inside itself
- **Status icon moved to left of title**: the green camera icon is now the leftmost element of the header h1, acting as both logo and live status indicator; the separate white camera SVG is removed
- **AnyCam — Home** added to header title text
- **Full quality in enhanced view**: when clicking a card to open focus/enhanced view, Low FPS Mode and Limit Threads config settings are bypassed for that stream so ffmpeg runs uncapped — full resolution, full fps, no thread limit

## 1.7.7

### Multi-stream handling
- **Single card per IP**: ONVIF cameras with multiple stream profiles (main + sub) now collapse into one card instead of creating separate cards per profile
- **Thumbnail uses sub-stream**: Card thumbnail polling uses the lowest-resolution stream for lower CPU/bandwidth; click-to-focus view switches to the full main stream
- **H.265+ badge suppressed**: If a working lower-res alternate stream is found, the H.265+ warning badge is automatically replaced with the green fallback badge

### Stream database
- **Embedded RTSP/MJPEG database**: Excel stream URL database converted to compact Python dict (STREAM_DB, 27 manufacturers) embedded in the addon for zero-I/O lookup
- **Post-login silent probe**: After credentials are accepted, silently probes manufacturer-specific alternate stream paths to discover streams not advertised pre-login
- **Manufacturer detection**: Heuristic matching on camera name/vendor/model to select the right URL patterns for probing

### Storage browser
- **Editable path bar**: Click anywhere on the breadcrumb bar to edit the path directly; Enter/blur navigates there; invalid paths show a popup then restore the previous valid path
- **Up arrow one level**: Up button now navigates one directory level up (not just to root); greyed at the ceiling (/media/anycam in scoped mode, / in unrestricted mode)
- **Unrestricted browser toggle**: New config option `unrestricted_storage_browser` (default: false) allows navigating the full filesystem instead of just /media/anycam

### Status indicator
- **Camera icon replaces dot**: The status indicator is now a camera SVG icon (50% larger, same shape as the AnyCam logo) with no fill — only the stroke colour changes
- **AnyCam text shifted right**: ~26px extra spacing between the logo icon and the AnyCam text for cleaner separation  
- **Same-tab log navigation**: Clicking the status icon now navigates in the same tab to Settings → Apps → AnyCam → Log (correct HA path), not a new tab

# AnyCam — Changelog

## 1.7.6
- Critical fix: storage view was rendering the OLD card-based layout instead
  of the Windows Explorer grid because two renderStorage() functions existed
  in the JS — the second (old) definition overrode the first (new) one.
  Old renderStorage removed; only the correct one calling _renderStorageView()
  remains.
- Critical fix: initSnaps() was defined twice; duplicate stub removed.
- Storage folder rows: entire row is now single-clickable to navigate into
  the folder (not just the name text); double-click on name still renames.
  Row cursor set to pointer to signal clickability.
- Storage file rows: single-click on filename now triggers a browser download
  of the MP4/MKV clip directly; double-click on filename still opens rename
  prompt. Download icon (⬇) in the actions column still works as before.
- Storage path breadcrumb now correctly shows /media/anycam (blue, clickable
  back to root) with › folder-name (bold) when inside a folder, and plain
  /media/anycam at root. _updateNavButtons() drives the display correctly.
- Forward button remains absent from nav bar (Windows Explorer style).

## 1.7.5
- Connect Camera page: Back button now matches all other pages — 'Back to
  Cameras' with btn-ghost btn-sm class, positioned at the top of the view
  before the form heading, identical to Port Scan and Storage pages
- Storage file browser nav bar: removed Forward button (Windows Explorer
  style only shows Back and Up); replaced path text box with a clickable
  breadcrumb: /media/anycam (blue, clickable → root) › folder-name (bold)
- Storage folder rows: type column now shows 'File folder' to match
  Windows Explorer; folder icon sized consistently at 1rem

## 1.7.4
- H.265+ automatic fallback for Hikvision (and compatible) cameras:
  When snap_loop detects the "Multi-layer HEVC coding is not implemented"
  ffmpeg error (set as camera["hevc_plus_warning"] = True by _drain_stderr),
  it now probes a prioritised list of alternate RTSP URLs on the FIRST
  restart after the flag appears:
    1. /Streaming/Channels/102 — Hikvision sub-stream (standard H.265,
       lower resolution, almost always decodable by ffmpeg)
    2. /Streaming/Channels/101?videoCodecType=H.264 — requests server-side
       transcode to H.264 (firmware-dependent, works on many models)
    3. /ISAPI/Streaming/channels/102 — ISAPI path sub-stream variant
    4. /ISAPI/Streaming/channels/101?videoCodecType=H.264 — ISAPI transcode
  Each candidate is probed with a raw RTSP OPTIONS socket call (4s timeout).
  The first URL that returns RTSP/1.0 200 is adopted; snap_loop switches to
  it, updates camera["stream_url"] in CAMERAS and saves to cameras.json,
  clears hevc_plus_warning, and sets hevc_plus_fallback_active = True.
  On subsequent addon restarts the camera loads from the fallback URL
  directly — no further probing needed.
  If no fallback URL responds, snap_loop continues with the original stream
  and -err_detect ignore_err extending stream life as before.
- Card badge updated: when hevc_plus_warning clears and fallback is active,
  the red "⚠ H.265+" badge is replaced by a green "✓ H.265+ fallback" badge
  confirming the automatic switch succeeded.
- New function _try_hevc_plus_fallback(camera_id, camera, orig_url) encapsulates
  all probe logic; easily extensible to other manufacturers using similar
  proprietary SVC codec extensions.

## 1.7.3
- Status dot moved inside <h1> tag, right after AnyCam text, so it appears
  immediately next to the logo as shown in screenshot (not as a separate
  sibling element after the h1)
- Status dot onclick now opens the real HA addon log page
  (/hassio/addon/camera_discovery/logs) in a new tab; event.stopPropagation()
  prevents the h1 click-home handler from also firing
- Storage view redesigned as Windows Explorer style:
  - Separate dark nav bar removed; nav controls (← → ↑ + path breadcrumb)
    now embedded inside the white explorer pane as its top toolbar
  - White (#fff) background for the file pane with light grey column headers
  - Four columns: Name | Date Modified | Type | Size — sortable by clicking
    any header, with ascending/descending arrow indicator
  - Folders listed at top, files below; hover highlight (#cce8ff, Windows blue)
  - Action buttons (⬇ download, 🗑 delete) appear on row hover only
  - Double-click any name to rename inline; folder rows navigate on single click
  - Drag-and-drop file move built with DOM event listeners (no quote collisions)
  - Empty state watermark centered in the white pane
- H.265+ warning badge: amber ⚠ H.265+ badge appears on Hikvision camera cards
  when the stream is detected as multi-layer HEVC (Hikvision proprietary codec);
  tooltip explains the fix (change H.265+ → H.265 in camera web UI)
- Removed duplicate "Not found after upgrade" badge that was rendering twice

## 1.7.2
- Remove (Option X) labels from Config tab toggle names
- Status dot colors: yellow (#f9c700) for warnings, red (#e53935) for errors,
  green (#43a047) for all clear — previously used orange for warnings
- Status dot click: opens actual HA addon log page at /hassio/addon/camera_discovery/logs
  in a new tab, rather than a custom in-app log view
- Removed custom System Log view and renderLogView JS — HA's built-in log is better
- Camera web page button: 🌐 button on each ready camera card; opens a modal offering
  to open the camera's IP in a new tab (always available) or in Firefox (if installed);
  Firefox detection probes /hassio/addon/firefox; if not installed, shows a Get It
  prompt linking to /hassio/store with a note to add mincka/ha-addons repository;
  if installed, opens the Firefox ingress panel in a new tab with a toast reminding
  the user to navigate to the camera IP manually (Firefox ingress does not accept
  URL injection from external callers)

## 1.7.1
- Fixes and improvements following 1.7.0 testing:

- Fix scan cancel logic: _SCAN_CANCELLED check at stage 1→2 was misplaced
  inside a nested if block causing IndentationError; fixed to check at top
  of each stage boundary as intended
- Add _LOG_BUFFER + _BufHandler: in-memory circular buffer of last 200
  WARNING/ERROR log entries; enables status dot to read health without
  requiring any external log access
- Status dot: green/amber/red circle next to AnyCam logo in header; polls
  /api/logs every 5s; amber = recent warnings, red = recent errors, green
  = all clear; click dot to open log view; title "System Stability"
- Logs view: new view showing recent warning/error entries in monochrome
  terminal style; accessible via status dot click or logs view
- AnyCam logo: clickable (→ cameras view) with title "Home"
- Scan cancel button: red × Cancel button appears in status bar while scan
  is running; POST /api/scan/cancel requests graceful abort at stage
  boundaries (between stages 1→2 and 3→4)
- Storage view: path navigation bar with Back / Forward / Up buttons and
  breadcrumb path display; root view shows camera folders as grid cards;
  folder view shows files as list; double-click name to rename
- Storage view: Back to Cameras button added
- H.265+ detection: when Hikvision (or other) camera streams in multi-layer
  HEVC (H.265+), _drain_stderr detects the ffmpeg "Multi-layer HEVC coding
  is not implemented" message and sets camera["hevc_plus_warning"] = True;
  Fix: camera web UI → Video → Encoding → change H.265+ to H.265
- -err_detect ignore_err added to snap_loop ffmpeg command; extends stream
  life before crash when camera sends malformed/partial HEVC frames
- Folder naming uses dashes not underscores (e.g. mainStreamProfile →
  main-stream-profile, hikvision-ds2de4a425iw-de)
- Focus view: image now fills full viewport (100vw × calc(100vh-50px)) with
  object-fit:contain; previously only occupied a small portion of screen
- Config descriptions added for all options A-F in HA Config tab

## 1.7.0
- Performance toggles (A-F) in HA App Configuration tab:
  A: low_fps_mode (default ON) — reduces HEVC output to 2fps while ffmpeg
     still decodes at source rate; dramatically cuts CPU encode/pipe cost;
     recommended for multi-camera Pi setups
  B: skip_nonref (default OFF) — adds -skip_frame nonref to ffmpeg input;
     skips B/P frames during HEVC decode (~40% CPU reduction, slightly choppy)
  C: limit_threads (default ON) — adds -threads 2 per ffmpeg process; prevents
     any single stream from monopolizing all 4 Pi cores
  D: stagger_polling (default OFF) — offsets each camera's snap_loop start by
     40ms * camera_index to spread CPU spikes across time
  F: hw_decode (default OFF) — enables hevc_v4l2m2m / h264_v4l2m2m hardware
     decoders; works on native Pi installs with V4L2 device access; not
     available in HA Docker containers; when enabled, failed decoders are
     NOT permanently marked unavailable (retried each stream start)
  Also: recordings_path, motion_sensitivity, motion_cooldown_secs,
        motion_clip_padding_secs configurable from the HA Config tab

- Motion detection + recording: toggle per-camera with the ⏺ Record button
  on each card; uses JPEG size comparison (fast, zero extra subprocess);
  when motion detected, spawns ffmpeg -c copy directly from camera RTSP URL
  for full-quality stream-copy recording (near-zero CPU); saves to
  /media/anycam/{camera_folder}/ as motion_YYYYMMDD_HHMMSS.mp4; stops
  recording after motion_cooldown_secs + motion_clip_padding_secs of
  no motion; button shows ⏺ Record (off) / ⏺ Armed (on, no motion) /
  ⏺ REC (active, pulsing red); motion state polled every 3s

- Click-to-focus enhanced view: click any live camera feed to enter
  full-screen mode; server sets _FOCUSED_CAMERA global, all other
  snap_loops throttle to 1fps, focused camera runs at native camera
  resolution/fps; CPU warning toast shown if estimated load >80% of Pi 4
  capacity (based on codec×pixels×fps heuristic); press Escape or X to exit;
  focus endpoints: POST /snap/focus/{id}, DELETE /snap/focus

- Built-in storage browser (Storage button in header):
  Shows disk usage bar (total/used/free/percent) for /media/anycam;
  lists per-camera folders with clip count and total size; shows each
  recording with size, date, download button, delete button; supports
  drag-and-drop to move clips between camera folders; double-click any
  filename or folder name to rename inline; folder names derived from
  camera display name (strips "Generic IP Camera", "(IP)", truncates to
  30 chars, filesystem-safe); storage endpoints: GET /api/storage,
  POST /api/storage/rename, POST /api/storage/move,
  DELETE /api/storage/file, GET /api/storage/download

- Removed duplicate device count from header (scan status bar already shows it)
- Renamed "Broad sweep" to "Deeper Scan" throughout UI and log messages
- Added Storage button to header bar (between Connect Camera and right edge)
- Toast notification system for storage actions (move, delete, rename)
- /snap/status endpoint updated with focus state info

## 1.6.1
- Critical fix: _drain_stderr was defined as a nested function inside
  handle_stream, making it invisible to snap_loop; every snap_loop call
  crashed immediately with NameError on the first _drain_stderr call,
  causing rapid restart loops and zero frames delivered; promoted
  _drain_stderr to module-level so both handle_stream and snap_loop
  can call it; this is the only thing preventing camera feeds from showing

## 1.6.0
- Architectural fix: replace long-lived multipart stream with snapshot polling.
  The browser now calls GET /snapshot/{id}?t=... every 125ms via JS setInterval.
  Each request is a normal short HTTP round-trip that HA's nginx ingress proxy
  handles correctly. Previously, one long-lived multipart/x-mixed-replace
  connection was used; nginx terminates these after a short burst (typically
  10-24 frames, ~1-3 seconds), causing "client disconnected" and blank cards.
  The /stream/{id} endpoint is kept but no longer used for live display.

- New background snap_loop task per camera: one persistent ffmpeg process runs
  per camera in the background, continuously decoding HEVC/H.264 and storing
  the latest JPEG frame in _SNAP[camera_id]['frame']. handle_snapshot returns
  whatever is in the buffer instantly — no ffmpeg startup cost per request.
  snap_loop auto-restarts on ffmpeg exit/crash (2s delay). Stops automatically
  after 30 seconds of no handle_snapshot calls (idle shutdown).

- New GET /snap/status endpoint: returns JSON health info for all active
  snapshot processes — pid, frame_count, frame_bytes, frame_age_s,
  last_poll_s, restarts. Open in browser to debug without reading logs.

- probe_rtsp_socket verbose label logging: pass label=camera_id/profile_name
  to get INFO-level logging of every RTSP round-trip (OPTIONS → result,
  DESCRIBE → 401 Digest realm/nonce, DESCRIBE authenticated → result).
  Previously all steps were silent on success and debug-only on failure.
  api_set_credentials now passes label so every probe step is visible in log.

- ONVIF credential flow: probe_rtsp no longer blocks card creation.
  When ONVIF GetProfiles SOAP returns profiles (proving credentials are valid),
  profile cards are created even if probe_rtsp returns False for the stream URL.
  A warning is logged explaining this. The probe_rtsp call is kept for
  informational logging but no longer gates card creation. Previously, probe_rtsp
  returning False (e.g. due to camera quirks in Digest auth negotiation, or
  rate-limiting after repeated debug sessions) would silently discard all
  profile cards even with correct credentials.

- Snap debug logging (server-side):
    SNAP [id]: starting background process (codec=..., res=...px)
    SNAP [id]: ffmpeg starting (codec=..., hw=hw:hevc_v4l2m2m OR sw, vf=...)
    SNAP [id]: frame N — X bytes (last poll Y.Zs ago)   [every 50 frames]
    SNAP [id]: hw decode timeout/EOF → sw               [hw fallback]
    SNAP [id]: ffmpeg EOF after N frames (rc=N)          [unexpected exit]
    SNAP [id]: 30s read timeout after N frames           [ffmpeg stalled]
    SNAP [id]: idle Ns — stopping                        [idle shutdown]
    SNAP [id]: restarting in 2s (#N)                     [before restart]
    SNAP [id]: loop done                                 [final exit]

- Snap debug logging (browser-side, open DevTools Console):
    [AnyCam] snapshot error #N for camera_id            [on failed frame fetch]
    After 3 consecutive errors: placeholder shown with "Stream unavailable"
    Error backoff: 500ms for errors 1-5, 2s for errors 6+

- probe_rtsp_socket verbose logging (when label supplied):
    [probe_rtsp id/profile] OPTIONS → OK
    [probe_rtsp id/profile] DESCRIBE → 401 Digest (realm=..., nonce=...)
    [probe_rtsp id/profile] DESCRIBE (authenticated) → 200 OK / error

## 1.5.4
- Revert pixel format to yuvj420p: this ffmpeg build's mjpeg encoder
  explicitly rejects yuv420p ('Incompatible pixel format') and only accepts
  yuvj420p (full-range JPEG format); -pix_fmt yuvj420p now set explicitly
  and -color_range 2 removed (redundant with yuvj420p)
- Filter swscaler deprecation lines from stderr log: the 'deprecated pixel
  format' warning is cosmetic-only and unavoidable with yuvj420p on this
  ffmpeg build; filtered in _drain_stderr so it no longer spams the HA log
- Add frame-sent logging: logs 'sent frame 1 (N bytes)' on the first frame
  and every 100 frames thereafter, confirming data is flowing from ffmpeg
  stdout through response.write() to the HTTP layer; also logs 'client
  disconnected after N frames' on ConnectionResetError/Aborted
- Note: 10.0.0.22 RTSP probe returning False is likely camera-side rate
  limiting from repeated connection attempts during debugging; re-entering
  credentials after a brief wait should restore it

## 1.5.3
- Fix swscaler deprecated pixel format warning: adding -pix_fmt yuv420p
  tells the mjpeg encoder to accept yuv420p directly (ffmpeg 5.0+),
  bypassing the internal conversion that triggered the swscaler warning

## 1.5.2
- Add X-Accel-Buffering: no response header: HA ingress is an nginx proxy;
  without this header nginx buffers the entire multipart/x-mixed-replace
  stream in memory before forwarding it to the browser, so the browser
  receives zero frames until the stream ends — this is the primary cause
  of the 'no live feed' symptom despite ffmpeg producing frames correctly
- Track hardware decoder availability at runtime: when hevc_v4l2m2m or
  h264_v4l2m2m reports 'Could not find a valid device', the decoder name
  is added to _HW_UNAVAILABLE (module-level set); subsequent stream
  requests skip hw decode immediately instead of wasting 3 seconds per
  attempt on a guaranteed failure
- Fix deprecated pixel format warning: change format=yuvj420p to
  format=yuv420p in all vf chains and add -color_range 2 to the ffmpeg
  command; yuvj420p is deprecated in ffmpeg 5+ and triggered a swscaler
  warning on every new scale context, filling the stderr pipe with noise

## 1.5.1
- Add diagnostic logging to handle_stream: log the ffmpeg command (creds
  stripped), the sanitized RTSP URL, ffmpeg exit code, and a warning on
  every ffmpeg EOF so silent failures are now visible in the HA log
- Fix stderr drain in finally block: previously stderr_t was cancelled
  before it could flush collected lines when ffmpeg crashed quickly; now
  we kill ffmpeg first, wait for it to exit, then await stderr_t (up to
  2s) so all ffmpeg error output is always captured and logged
- Handle asyncio.CancelledError in _drain_stderr: on cancellation, attempt
  one final read of any remaining buffered stderr bytes before exiting

## 1.5.0
- Remove go2rtc from streaming hot path (Option B architectural fix)
  ffmpeg now connects directly to the authenticated camera RTSP URL and
  outputs MJPEG frames to stdout; the go2rtc RTSP restream layer
  (localhost:8554) is no longer used for streaming, eliminating the
  phantom-registration bug where go2rtc created sourceless empty streams
  because the PUT /api/streams endpoint reads the source URL from the
  'src' query parameter, not the request body as our code incorrectly sent
- Remove double-transcode for HEVC cameras: previous pipeline decoded
  HEVC inside go2rtc then re-encoded to H.264, then ffmpeg decoded H.264
  again to produce MJPEG; new pipeline decodes HEVC once directly to MJPEG
- Add -nostdin flag and stdin=DEVNULL: prevents ffmpeg from inheriting the
  Python process stdin, which could cause unexpected blocking
- Add -an flag (no audio): drops audio stream entirely, saves CPU and
  prevents audio codec warnings from filling the stderr pipe
- Switch output format from -f mjpeg to -f image2pipe: both produce raw
  concatenated JPEGs but image2pipe is the documented format for pipe output
- Fix pixel format in vf chain: format=yuv420p -> format=yuvj420p
  The JPEG full-range variant avoids a deprecation warning ffmpeg emits
  when the mjpeg encoder receives limited-range yuv420p; the warning was
  silently filling the stderr pipe and could contribute to stderr deadlock
- Fix stderr deadlock: stderr is now drained by a concurrent asyncio Task
  (_drain_stderr) that reads stderr line-by-line throughout the stream;
  previously stderr was only read in the finally block after streaming
  ended, so any stderr output during streaming could fill the 64KB OS pipe
  buffer, blocking ffmpeg from writing to stdout, causing 30s timeouts
- Define SOI/EOI/CRLF as bytes([...]) literals in handle_stream: removes
  all escape sequence ambiguity that caused the broken frame-parser bug
- Add 4MB buf overflow cap: if buf exceeds 4MB without a complete JPEG
  frame, the buffer is discarded; this prevents unbounded memory growth
  when a camera sends corrupted or non-JPEG data
- Improve buf handling: bytes before SOI are trimmed immediately instead
  of carrying them through the next read loop iteration
- go2rtc is retained and still runs at startup for stream probing
  (the _probe_ code path), but is no longer required for viewing streams

## 1.4.4
- Critical fix: JPEG frame parser was broken — SOI (0xff 0xd8) and EOI (0xff 0xd9)
  markers were stored in source as double-escaped strings (\xff\xd8) meaning
  Python searched for literal 8-char ASCII sequences instead of 2-byte JPEG
  markers; no frame boundaries were ever found so the stream read loop ran until
  30-second timeout with zero frames delivered to the browser; fixed via binary
  replacement ensuring file has single-escaped ÿØ (4-char escape sequence
  that Python compiles to bytes 0xFF 0xD8 at runtime)
- HEVC cameras now registered with go2rtc using ffmpeg wrapper source:
  go2rtc_source() returns 'ffmpeg:rtsp://...#video=h264&width=640&fps=8' for
  HEVC streams; go2rtc uses its configured ffmpeg binary to transcode HEVC→H.264
  internally; the local RTSP restream (port 8554) then delivers H.264; our
  ffmpeg process reads H.264 from 127.0.0.1:8554 and easily converts to MJPEG
  For 4K HEVC: 480x270 @ 4fps; for 1080p/720p HEVC: 640x360 @ 8fps
- go2rtc_source() used consistently everywhere: handle_stream, go2rtc_register_all,
  ONVIF profile card registration, and credential save all pass camera dict so the
  correct source URL (direct or ffmpeg-wrapped) is used from the start
- Startup log now shows whether each camera is registered as direct or ffmpeg-wrapped
  and what codec is stored; Stream log shows go2rtc mode and source resolution

## 1.4.3
- Critical fix: added 'import aiohttp' to module imports; go2rtc_add/remove
  functions use aiohttp.ClientSession() but the module only had
  'from aiohttp import web' — every go2rtc API call failed with
  'NameError: name aiohttp is not defined', causing all streams to show
  unavailable even after credentials were accepted
- Critical fix: probe_rtsp_socket roundtrip() function was sending malformed
  RTSP requests because the CRLF separator used escaped backslash-r-backslash-n
  literal characters instead of actual carriage-return + line-feed bytes;
  rewrote using chr(13)+chr(10) which is unambiguous regardless of Python
  string escaping; the camera was receiving an invalid request, sending nothing
  back, and probe_rtsp_socket was timing out (taking the full 6 seconds) before
  returning False — causing all credential attempts to fail

## 1.4.2
- probe_stream_details: now queries go2rtc's /api/streams after registering
  the stream, then falls back to ffprobe only if go2rtc reports no track info;
  this avoids ffprobe entirely for cameras already known to go2rtc
- handle_stream_test (Test Stream button): replaced ffprobe with pure Python
  RTSP socket probe for connectivity check; queries go2rtc for codec/resolution
  details if not already stored on the camera; no subprocess needed
- handle_snapshot: replaced ffmpeg one-frame grab with go2rtc's /{id}.jpg
  snapshot endpoint; go2rtc handles the frame extraction natively
- api_delete_camera: now removes camera from go2rtc on deletion
- probe_stream_details: accepts optional cid param so caller-supplied stream
  name is used for go2rtc registration rather than a temp hash name

## 1.4.1
- Architecture change: replaced custom ffmpeg/ffprobe pipeline with go2rtc
  go2rtc is a purpose-built Go binary used by Frigate and HA's camera stack;
  it handles RTSP/ONVIF/HLS/RTMP, all codecs (HEVC/H.264/etc), auth (Basic/
  Digest/ONVIF), and reconnection natively; no more URL encoding fights,
  probesize limits, or hardware decode guesswork
  go2rtc runs as a sidecar on port 1984; streams are registered via its REST
  API and MJPEG output is proxied through the AnyCam server to the browser
  go2rtc binary auto-downloaded for the correct architecture in Dockerfile
  (arm64/armv6/amd64/386) using the BUILD_ARCH build arg
- Architecture change: replaced ffprobe stream verification with a pure Python
  RTSP socket probe (probe_rtsp_socket); sends RTSP OPTIONS + DESCRIBE with
  proper Digest and Basic auth negotiation via raw TCP socket; no external
  processes, no probesize limits, no URL encoding issues, works with any codec
  Credentials are never embedded in a URL string — passed as separate strings
  and encoded only into the RTSP Authorization header where needed
- go2rtc stream lifecycle: cameras are registered with go2rtc on startup,
  when credentials are accepted (both ONVIF profile cards and direct RTSP),
  and removed when cameras are deleted
- run.sh: go2rtc started before Python server with API readiness check

## 1.4.0
- probe_rtsp: removed -analyzeduration 1000000 -probesize 200000 flags that
  were causing RTSP verification to fail for high-res HEVC cameras; a single
  4K HEVC IDR frame can easily exceed the 200KB probesize limit, causing
  ffprobe to exit with error even on a valid stream; removed limits let ffprobe
  use its defaults (5MB / 5s) which are sufficient for any camera
- probe_rtsp: timeout increased from 4s to 8s (subprocess timeout 11s) to
  give HEVC streams time to deliver their first IDR frame
- probe_rtsp: stderr now logged as WARNING with redacted URL when ffprobe
  returns non-zero; exact ffprobe error message now visible in the log so
  future failures will explain themselves rather than just showing False
- probe_rtsp: credentials URL-encoded with the correct safe set (sub-delims
  kept literal, only @ : / ? # encoded) — consistent with build_authenticated_url

## 1.3.9
- Added full diagnostic logging to api_set_credentials — every step now logs
  at INFO/WARNING so credential failures are visible in the log:
  which protocol path is taken, ONVIF media URL, how many profiles returned,
  each profile's stream URL, probe_rtsp result per profile, which RTSP ports
  were tried, final success/failure outcome
- RTSP port 554 fallback for ONVIF cards: ONVIF cards store port 80 (the web
  UI port) but RTSP is always on port 554; if ONVIF SOAP fails and falls back
  to direct RTSP probing, we now always also try port 554 explicitly, not just
  the stored port; this fixes 'Could not connect' for cameras where ONVIF auth
  fails but direct RTSP works fine
- Also tries xaddrs port from ONVIF discovery as a third fallback

## 1.3.8
- URL credential encoding fix: the previous fix encoded ! as %21 but many
  RTSP servers (including Hikvision-based cameras) do not percent-decode
  credentials before authentication — they compare %21 literally against the
  stored password, causing authentication to fail; per RFC 3986, the userinfo
  component allows sub-delimiters (! $ & ' ( ) * + , ; =) unencoded; the
  safe set is now set to these sub-delimiters plus unreserved chars, so only
  truly URL-breaking characters are encoded: @ : / ? # [ ]
  Result: 'fuckyou!' stays as 'fuckyou!' in the URL (correct) while a
  password like 'p@ss:w0rd' becomes 'p%40ss%3Aw0rd' (necessary to avoid
  breaking the user:pass@host URL structure)

## 1.3.7
- Critical URL encoding fix: build_authenticated_url() now percent-encodes
  both username and password using urllib.parse.quote() before inserting
  into the RTSP/HTTP URL; special characters like ! @ : # ? in passwords
  were being passed raw, causing ffmpeg to fail with 'Invalid data found
  when processing input' — e.g. password 'abc!def' is now encoded as
  'abc%21def' in the URL string; this fix affects all stream protocols
- Replaced -pix_fmt yuvj420p flag with format=yuv420p inside the -vf
  filter chain; having -pix_fmt as a separate flag was conflicting with
  how the HEVC software decoder outputs frames and causing swscaler to
  run an extra conversion; moving it into the filter chain as
  'fps=N,scale=W:-2,format=yuv420p' is the correct approach

## 1.3.6
- Added -pix_fmt yuvj420p to ffmpeg output — yuvj420p is the JPEG full-range
  YUV pixel format that MJPEG encoding expects natively; without it, ffmpeg runs
  swscaler to convert pixel formats on every frame which is slower and was
  causing 'deprecated pixel format' warnings; forcing it eliminates the extra
  conversion step and ensures frames flush correctly
- Replaced blocking 4-second early-fail check for hardware decode with a fast
  2-second first-read timeout — the old approach spent 4 seconds doing nothing
  before switching to software, giving the HA ingress proxy time to drop the
  connection before the first MJPEG frame arrived; now if hardware produces no
  data in 2 seconds, we immediately kill it and retry software; if software
  produces no data in 30 seconds, we give up; total wait before first frame
  is now at most 2-3 seconds instead of potentially 10+ seconds

## 1.3.5
- Critical fix: replaced -stimeout with -timeout in ffmpeg command;
  -stimeout was deprecated and removed in newer ffmpeg versions — it caused
  ffmpeg to exit immediately with 'Unrecognized option stimeout / Option not found',
  producing zero frames and instant stream failure; -timeout is the correct
  option for modern ffmpeg RTSP connection timeout
- Critical fix: verification scan codec probe now uses build_authenticated_url()
  to get the credential-embedded URL for ffprobe; previously it used the bare
  saved stream_url which has credentials stripped by save_cameras(), causing
  ffprobe to get a 401 Unauthorized and return an empty dict, leaving
  stream_codec empty and hardware decode never triggered

## 1.3.4
- Critical bug fix: NameError 'hw_flags is not defined' crashed every stream
  request; hw_flags was renamed to hw_flags_inner inside _make_proc_args during
  a refactor but the _launch_ffmpeg(hw_flags) call site was not updated; streams
  always showed 'Stream unavailable' because of this exception
- Verification scan codec probing: fixed indentation error that prevented
  probe_stream_details() from running during the post-upgrade verification scan;
  codec info now correctly stored for all cameras missing stream_codec on startup;
  save_cameras() called after verification so codec data persists across restarts

## 1.3.3
- Critical fix: stream codec (HEVC/H.264) now populated during the post-upgrade
  verification scan for cameras that were saved before 1.3.2 was installed;
  previously 'codec=?' meant hardware decode was never attempted even though
  the camera supports it; now probe_stream_details() is called during
  verification when a camera has a stream_url but no stream_codec stored
- 4K HEVC handling: streams from sources >=3840px wide now use fps=4,scale=480:-2
  output filter (versus fps=8,scale=640:-2 for 1080p HEVC and fps=10,scale=640:-2
  for H.264); reduces the decode + re-encode workload to a level a Pi 4 can sustain
- Hardware decode early-fail detection: for HEVC/H.264 streams where a hardware
  decoder (hevc_v4l2m2m or h264_v4l2m2m) is attempted, a 4-second window checks
  if ffmpeg exits immediately with an error code; if so, stderr is logged and
  ffmpeg is restarted immediately with software decode (no waiting 30s)
- Timeout log message now includes frames_sent count, codec, and source resolution;
  zero frames + HEVC explicitly noted as likely hw decode availability issue
- ffmpeg stderr always drained and logged in finally block (not just on TimeoutError)

## 1.3.2
- HEVC/H.265 streaming support without changing camera settings:
  handle_stream now reads the stored stream_codec and selects the appropriate
  hardware decoder — hevc_v4l2m2m (Raspberry Pi 4 VideoCore VI) for H.265,
  h264_v4l2m2m for H.264; these use the Pi GPU rather than the CPU
  If hardware decode produces no frames, automatically retries with software
  decode (ffmpeg selects the native hevc/h264 software decoder)
  Output FPS reduced from 10 to 8 for HEVC streams to ease Pi CPU load
- Stream details auto-detected after credentials accepted:
  probe_stream_details() runs ffprobe on the confirmed stream URL and stores
  stream_codec, stream_width, stream_height, stream_fps, stream_profile,
  and stream_audio on the camera dict; called from both the RTSP/MJPEG/HLS
  credential path and the ONVIF profile card creation path
- Stream details shown in Identity section:
  Video codec row: e.g. 'HEVC (High)' or 'H264 (High)'
  Resolution row: e.g. '1920x1080  @  30 fps'
  Audio codec row: e.g. 'AAC' (if audio track present)
- _safe_cam API response now includes stream_codec, stream_width, stream_height,
  stream_fps, stream_profile, stream_audio fields

## 1.3.1
- Stream diagnostics: ffmpeg stderr is now ALWAYS captured and logged as WARNING
  when the stream ends (previously only logged on timeout); this makes silent
  failures visible — e.g. 'H.265 decoder not found' or 'RTSP auth failed'
- Stream log: stream URL logged at INFO level (credentials redacted) so you can
  see exactly what ffmpeg is connecting to
- ONVIF protocol now correctly gets -rtsp_transport tcp and -allowed_media_types video
  flags in ffmpeg (was only applied to RTSP and DVR); fixes Hikvision PTZ cameras
  that kept stream_url as ONVIF protocol after credential submission
- ffmpeg -stimeout 8000000 (8s connection timeout) and 30s per-frame read timeout
- New: GET /stream/{id}/test diagnostic endpoint — runs ffprobe on the stream URL
  and returns JSON with codec, resolution, FPS; 'Test Stream' button on each ready
  camera card opens an alert with this info, or the error message if unreachable
  Use this to diagnose 'Stream unavailable': if ffprobe succeeds but live view
  fails, the issue is ffmpeg decoding (usually H.265 on a Pi that only supports
  H.264 in software) — fix by setting the camera to H.264 720p
- Port scan timer: shows 'X:XX elapsed' at start (before nmap gives ETA),
  then 'estimated X:XX remaining' once nmap sends its first timing update,
  then 'Completed in X:XX' when done — now matches main scan timer behavior
- Port scan timer: uses .scan-timer-badge CSS class for consistent blue styling
- ETA unified: _total_estimate variable persists across all stages and only
  ever decreases; per-host updates use the same reference point (scan_start)

## 1.3.0
- ETA fix: eliminated per-stage ETA resets — a single _total_estimate variable
  is maintained from scan start and only ever refined downward; each stage
  updates the estimate based on new information but the countdown never jumps
  back up; eta is always computed as max(0, total_estimate - elapsed_since_start)
  Stage 1: refines using actual S1 time + live host count (10s/host heuristic)
  Stage 2: refines using actual S1+S2 elapsed / 0.55 (stage 2 = ~55% of work),
  floored at responding_hosts * 15s; takes the smaller of old vs new estimate
  Per-host: continuously updated using same total_estimate reference
- ONVIF stream fix: handle_stream now includes 'ONVIF' in the protocol list
  that gets -rtsp_transport tcp; Hikvision and many other cameras require TCP
  transport for RTSP — without it ffmpeg uses UDP which is often dropped
- Added -allowed_media_types video to RTSP/ONVIF/DVR ffmpeg flags to avoid
  stalling on audio-only streams before video frames arrive
- Added -stimeout 8000000 (8 seconds) RTSP connection timeout to ffmpeg so
  unreachable streams fail fast rather than hanging until the 30s read timeout
- ffmpeg stderr now captured and logged as WARNING on stream timeout so future
  stream failures are visible in the log for diagnosis
- Per-frame read timeout increased from 15s to 30s to accommodate cameras that
  take longer to start sending frames after connection is established

## 1.2.9
- Removed duplicate 'Completed in X:XX' from grey status bar message;
  completion time now shown only in the blue timer element to the right

## 1.2.8
- Not a Camera: replaced bare confirm() dialog with a proper modal:
  Shows device name/manufacturer prominently
  7 quick-select device type buttons: Printer, Router/Firewall, NAS/Storage,
  Computer, Smart TV, IoT Device, Not sure — selected button turns red
  Free-text detail field for optional extra info (model name, notes, etc.)
  Share anonymously checkbox — when checked, a device fingerprint is submitted
  to the configured community endpoint (no IP addresses, only OUI, device type,
  open ports, service banners, and page title)
  Keyboard: Escape closes the modal without blacklisting
- Feedback storage: each Not a Camera action now stored in /data/not_camera_feedback.json
  Record includes: cid, reason_type, reason_detail, fingerprint (OUI + vendor +
  port + protocol + service + page_title + manufacturer), share flag, timestamp, version
  Feedback persists across restarts and survives upgrades
- Community sharing architecture: ANYCAM_COMMUNITY_URL env var configures a community
  endpoint; when set and user checks Share, fingerprint is POSTed to /api/v1/report;
  silently ignored if endpoint unavailable; no endpoint configured by default
- build_fingerprint() helper extracts shareable device signatures from camera dicts
- submit_to_community() is a fire-and-forget async task (never blocks the UI)
- Broad sweep subtext: removed duplicate; label now shows Ports 1-10,000 cleanly

## 1.2.7
- Port scanner: ETA countdown shown from the moment scanning starts:
  Uses last_port_scan_duration from runtime.json as the initial estimate;
  updates every ~10 seconds using nmap's own timing output (--stats-every 10s);
  counts down smoothly client-side between polls accounting for poll drift;
  saves actual elapsed time to runtime.json on completion for next scan
- Port scanner: checkbox state now persists across view switches using a
  persistent _selectedIPs Set; selections survive navigating to Cameras and back;
  'Select all' / 'Clear all' also update the persistent set correctly
- Port scanner: live port discovery feed shown during scan:
  nmap -v flag emits 'Discovered open port X/tcp on Y' lines as ports are found;
  these are streamed line-by-line from stdout and added to PSCAN['live_ports'];
  a scrolling box (max 220px, auto-scrolls to bottom) shows ports as found;
  message updates to 'Scanning X... N open port(s) found' in real time;
  only open/active ports are shown, never closed or filtered ones
- Port scanner: live discovery box replaced by full results table when done;
  nmap XML now written to a temp file instead of stdout so we can stream
  the verbose text output for live discovery simultaneously
- Port scanner: ETA and live box both cleared when navigating back to page;
  completed results are restored from PSCAN state when returning to Port Scan
- api_pscan_status now includes elapsed (computed server-side from scan_start)
  so JS can accurately compute time drift between polls for smooth countdown

## 1.2.6
- ETA estimate shown from the very first second of scanning:
  On first-ever scan: initial estimate is 4 minutes (240s) for a typical /24 network
  On subsequent scans: uses actual duration from the most recent completed scan
  (stored as last_scan_duration in /data/runtime.json) as the starting estimate
- ETA progressively refined at each stage:
  After Stage 1 (ARP complete): refined using actual S1 time + host count
  formula (S1_elapsed * 10, floored at S1 + hosts * 8s)
  After Stage 2 (nmap complete): refined using S1+S2 elapsed / 0.55 to project
  100%, also floored at responding_hosts * 15s
  Per-host in Stage 3: updated continuously as each host is probed
- ETA display: 'estimated X:XX remaining' while running, counting down smoothly
  client-side between 1.5s polls; jumps are expected as estimates are refined
- Actual scan duration saved to runtime.json on completion for next scan's ETA
- ETA label changed from '~X:XX remaining' to 'estimated X:XX remaining'

## 1.2.5
- Critical bug fix: NameError crash in is_camera_positive() — 'reason' was
  referenced in the not_camera verdict log line but is not a parameter of that
  function; this caused run_scan() to crash immediately when any device with a
  not_camera verdict was encountered (e.g. a printer), leaving SCAN_STATE
  running=True forever and the UI stuck in an infinite polling loop
- Added safety wrapper around run_scan(): any uncaught exception now sets
  running=False with an error message so the UI never gets permanently stuck
- Stage label removed from status message text — stage is shown only in the
  purple badge; message now shows just what is happening (e.g. 'Probing
  192.168.50.3 (1/4)...') without the 'Stage 3/4 —' prefix duplication
- Scan timer changed from elapsed to ETA countdown:
  Early stages show 'X:XX elapsed'; once Stage 3 begins, an ETA is calculated
  by extrapolating from Stage 1+2 time (25% of work) to 100% and showing
  '~X:XX remaining' that counts down; completion shows 'Completed in X:XX'
- Docker IP access log filtering: a logging.Filter on aiohttp.access suppresses
  all log entries from 172.x.x.x addresses (HA Supervisor Docker bridge proxy);
  these requests are still served normally, just not logged
- Broad sweep checkbox now shows 'Ports 1–10,000' as small subtext below label
- Lorex probe also runs in ONVIF-only else branch (previous fix) — timeouts
  per-port are now explicit with asyncio.wait_for

## 1.2.4
- Lorex NVR identification improvements:
  Added 'flirlorex' and 'flir lorex' to all Lorex DB detection patterns;
  ONVIF-only device HTTP probe now tries ports 80, 443, 8080, 8888, 8090, 34567
  (NVRs often use non-standard web ports); added asyncio timeout and per-port
  logging so failures are visible in the log
- Docker/internal IP exclusion: hosts with 172.x.x.x or 169.254.x.x addresses
  are now excluded from the live host list; these are HA Supervisor Docker bridge
  IPs, not real LAN devices — this fixes the 'homeassistant' card from the
  Docker gateway appearing in scan results
- ACTi false positive fix (HP printer misidentified):
  identify_manufacturer() now uses word-boundary matching for keywords shorter
  than 6 characters; short strings like 'acti' no longer match as substrings
  inside words like 'interactive' or 'active' on unrelated device pages
- ACTi false positive fix (part 2): is_camera_positive() now respects the
  nmap not_camera verdict (printer, router, NAS, etc.) as an early rejection,
  skipping all protocol probing for devices nmap identified as non-cameras;
  OUI camera confirmation still overrides this if MAC OUI is a known camera maker
- Scan timing: SCAN_STATE now tracks started_at timestamp and elapsed seconds
- Elapsed timer shown in status bar to the right of the progress bar:
  'X:XX elapsed' while running, 'Completed in X:XX' when done
- Completion log message now includes elapsed time:
  'Scan complete — N device(s), N streaming. Completed in X:XX.'

Broad sweep reminder: enables Stage 4 — after the focused top-1000-port scan,
any live hosts that did not respond get scanned on ports 0-10,000 to catch
cameras on very non-standard ports. Adds 5-20 minutes depending on silent hosts.

## 1.2.3
- Deduplication fix: confirmed_ips now includes any card where manufacturer
  is identified OR protocol is a camera type OR verdict_reason starts with
  ONVIF/SSDP/mDNS — not just status=ready with non-HTTP protocol; this
  correctly suppresses the 192.168.50.210:80 and :443 noise cards when the
  iENSO was identified on port 8888 but still needs credentials
- AnyCam self-exclusion: nmap results for the local Pi IP have our own
  ingress port (8099) removed before probing; prevents AnyCam's own UI
  from appearing as a camera card (our page contains 'camera' everywhere)
- Lorex/ONVIF identity fix: when ONVIF merges into an existing nmap-found
  entry (e.g. device found on port 554 but ONVIF confirmed), HTTP identity
  probing now runs on ports 80/443/8080 if manufacturer is still empty;
  this allows Lorex NVRs found on RTSP to get HTTP-probed and identified
- Port Scanner: navigating away no longer cancels the scan; returning to
  Port Scan view resumes status polling automatically if scan is running
- Port Scanner: results split into two sections:
  'Likely camera-related' (camera ports + camera service keywords) shown
  fully expanded; 'Other ports' collapsed with click-to-expand toggle
- Port Scanner: 'Back to Cameras' button moved to upper left of the view
- Port Scanner: hint text added noting that navigating away won't cancel

## 1.2.2
- MAC address OUI lookup added to device identification pipeline:
  nmap reports MAC addresses and vendor names from its built-in OUI DB
  during ARP-based LAN scans; these are now extracted from nmap XML and
  stored on every camera card
- Full IEEE OUI database (~37,000 entries) downloaded from
  standards-oui.ieee.org on first startup and cached to /data/oui_cache.json;
  refreshed automatically when cache is older than 30 days; download runs
  as a background async task and never blocks the scan
- lookup_oui(mac): returns vendor name from full IEEE DB, or embedded fallback
- oui_is_camera(mac): returns True/False/None based on OUI vendor matching
  against CAMERA_DB aliases (camera) or NON_CAMERA_KEYWORDS (non-camera)
- is_camera_positive() now checks OUI immediately after multicast signals;
  a camera-manufacturer OUI is a definitive positive; a non-camera OUI
  (Cisco, Apple, HP, Ubiquiti, Synology, etc.) causes early rejection
  before any slower probes are attempted
- _parse_nmap_xml() extracts address[@addrtype=mac] addr and vendor attributes;
  supplements nmap vendor with full IEEE DB when nmap does not identify it
- _probe_host_port() and base() carry mac_addr and mac_vendor through to the
  camera dict; OUI vendor used to fill manufacturer field if HTTP probe
  did not identify one
- Identity card section now shows MAC / OUI row: address + vendor name in
  parentheses (e.g. "1C:C3:16:xx:xx:xx  (Hangzhou Hikvision Digital...")
- Embedded curated OUI sets: ~30 known camera-manufacturer OUI prefixes
  and ~60 known non-camera OUI prefixes (Cisco, Juniper, MikroTik, Ubiquiti,
  HP, Dell, Apple, Netgear, ASUS, Brother, Epson, Synology, QNAP)

## 1.2.1
- Lorex NVR detection fix: probe_http_identity now extracts script/link src
  attribute values from HTML (not just body text) — catches SPAs like Lorex
  where the manufacturer name appears only in JavaScript asset paths
  (e.g. src="/flirLorex/js/desktop/...") not as visible page text
- probe_http_identity now ignores SSL certificate errors (cameras and NVRs
  almost always use self-signed certs); previously a certificate error
  silently returned empty identity with no manufacturer detected
- probe_http_identity now tries multiple paths per host: /, /index.html,
  /login.htm, /login.html, /web/, /web/index.html, /cgi-bin/main-cgi,
  /view/index.shtml, /live, /admin/ — stops at first manufacturer match
- probe_http_identity now tries both http:// and https:// on every port
- Body read increased from 8KB to 16KB for better SPA coverage
- Focused nmap scan changed from fixed 15-port camera list to
  --top-ports 1000 (nmap's curated most-common-1000 ports list);
  covers all camera protocols plus thousands of other ports, catching
  cameras on non-standard ports identified by manufacturer name in banner
- IP-level deduplication: after all probing, if an IP already has a
  confirmed streaming camera card (status=ready, non-HTTP protocol),
  any sibling cards on that same IP that are HTTP-only/needs_credentials
  are automatically suppressed — prevents noise cards like serial-number
  hostnames appearing alongside confirmed camera cards
- Port Scanner: ARP-discovered hosts now listed above the IP input with
  checkboxes — IP and hostname shown; Select all / Clear buttons available
- Port Scanner: batch mode — check any number of hosts and click
  Scan All Ports to scan them sequentially; results table shows a Host
  column when more than one IP was scanned; results accumulate across all
  hosts in the batch
- ARP_HOSTS global stores last scan results; /api/arp_hosts endpoint
  returns them; Port Scanner auto-refreshes the list when opened

## 1.2.0
- Expanded CAMERA_DB from 33 to 53 entries (54 total minus 1 duplicate):
  Added: Tiandy, IndigoVision, Q-See, LaView, Zosi, Sricam/Srihome, Vstarcam,
  Wansview, Tenvis, Instar, Luma Surveillance (SnapAV), Speco Technologies, Oncam,
  Illustra (Johnson Controls), Milesight, Sunell, TVT Digital, Kedacom,
  VideoIQ (Avigilon), Samsung standalone (pre-Hanwha)
- Removed duplicate Reolink entry

## 1.1.9
- Camera manufacturer/model database (CAMERA_DB): 33 entries covering all major
  manufacturers — Hikvision, Dahua, Lorex, Reolink, Axis, Hanwha/Samsung, Amcrest,
  Uniview, Vivotek, Bosch, Pelco, Sony, Panasonic/i-PRO, Avigilon, FLIR, Mobotix,
  ACTi, GeoVision, Foscam, Annke, Swann, TP-Link Tapo, Night Owl, iENSO, Digital
  Watchdog, March Networks, Nest/Google, Ring, Wyze, Eufy/Anker, Arlo, Verkada, Luxonis
- Each DB entry contains signature patterns for: HTTP page titles, page body text,
  HTTP response headers, nmap service/product fields, and ONVIF WS-Discovery scope strings
- identify_manufacturer(): scores all DB entries against a text blob and returns the
  best-matching entry; used during probing and nmap banner analysis
- probe_http_identity(): replaces probe_http_for_camera() — fetches the HTTP root
  page (up to 8KB), extracts page title, Server header, and runs DB matching; returns
  structured dict with manufacturer, notes, title, server, is_camera, raw_snippet
- probe_http_for_camera() now a thin wrapper around probe_http_identity()
- is_camera_positive() now checks nmap banners against CAMERA_DB (not just keyword list)
  and also checks device hostnames against DB alias strings
- _probe_host_port() now calls probe_http_identity() on HTTP ports and attaches identity
  fields (manufacturer, device_notes, page_title, server_header) to every camera dict
- Camera display name auto-upgraded: if manufacturer is identified and the name is still
  the IP-derived default, the name is set to "Manufacturer (ip)" (e.g. "Lorex (192.168.50.3)")
- ONVIF-only devices (detected by multicast but not port scan) now also get HTTP identity
  probing on ports 80, 8080, 443
- Identity section on each camera card: collapsible "🔍 Identity" section showing
  Manufacturer, Page title, Server header, Hostname, and Notes rows
- Post-upgrade scan clarification: status message now explicitly says "scanning subnet
  for newly discoverable cameras" when the full scan runs after verification

## 1.1.8
- Version-aware startup logic — three distinct startup modes:
  new_install: no saved cameras and no prior version recorded → auto-scan as before
  routine: same version as last run (HAOS reboot, addon restart) → load cameras silently, no scan
  post_upgrade: version differs from last run → load cameras, then run verification scan + fresh full scan
- Post-upgrade verification scan:
  Each saved camera is probed individually using its own protocol (RTSP probe, MJPEG quick-check, etc.)
  Cameras still responding → kept as-is, upgrade_missing flag cleared
  Cameras not responding → marked upgrade_missing=True (kept in list, not deleted automatically)
  After verifying saved cameras, a full fresh scan runs to discover new cameras the upgraded
  detection code may now find
- Cameras marked upgrade_missing show:
  Pulsing orange status dot (distinct from yellow needs_credentials and red error)
  Orange badge: ⚠ Not found after upgrade
  Feed area shows explanatory overlay: was present before upgrade but did not respond;
  may be offline, removed, or a false positive from the previous version
  Two decision buttons: Keep (may be offline) clears the flag and restores normal card behaviour;
  Remove deletes permanently
- New /api/cameras/{id}/confirm endpoint: clears upgrade_missing flag
- Version persisted in /data/runtime.json on every startup so next run can compare
- Pre-1.1.8 upgrades handled: if runtime.json does not exist but cameras.json does,
  startup mode is treated as post_upgrade (covers users upgrading from any previous version)
- 172.30.32.x log entries clarification: these are the HA Supervisor Docker bridge IPs,
  not external clients — all browser requests are proxied through the Supervisor

## 1.1.7
- Extended is_camera_positive gate to cover all 7 supported protocols:
  RTMP: wired existing probe_rtmp() C0 handshake byte check into the gate
    (ports 1935/1936, ~50ms, definitively identifies RTMP server)
  MJPEG: new probe_mjpeg_quick() checks 6 common paths for
    Content-Type: multipart/x-mixed-replace or image/jpeg (~200ms)
  HLS: new probe_hls_quick() checks 5 common paths for #EXTM3U body
    or mpegurl Content-Type (~200ms)
  WebRTC: wired existing probe_webrtc() WHEP POST + heuristic GET
    into the gate for HTTP/HTTPS ports (~300ms)
  WS-RTSP: wired existing probe_ws_rtsp() WebSocket upgrade handshake
    (Sec-WebSocket-Protocol: rtsp) into the gate for any port (~200ms)
  RTSP/HTTP probes unchanged from 1.1.6
  ONVIF/SSDP/mDNS still confirmed instantly via Stage 1 multicast
- Probe order optimised: fastest and most definitive probes run first
  (RTMP handshake 50ms, RTSP OPTIONS 100ms) before slower HTTP probes
- WS-RTSP probe runs last on any port not covered by earlier checks
  (covers go2rtc on 8554, mediamtx on custom ports, etc.)

## 1.1.6
- Replaced passive keyword-matching filter with active camera-positive probing:
  Each device now only gets a card if at least one active test confirms it speaks
  camera protocols. Tests run in priority order (fastest first):
  1. Multicast-confirmed (ONVIF/SSDP/mDNS) — already done in Stage 1, instant
  2. RTSP OPTIONS handshake: sends a raw OPTIONS request to RTSP ports; any
     RTSP/1.0 response (including 401 Unauthorized) is camera-positive. Routers
     and printers do not speak RTSP and close the connection or return garbage.
  3. HTTP content probe: fetches the root page and scans the first 4KB of body
     and response headers for camera-specific strings (camera, onvif, rtsp,
     hikvision, dahua, stream, nvr, dvr, etc.). Much more reliable than nmap
     banner matching because it reads actual page content.
  4. DVR port assumption: ports 37777 (Dahua) and 34567 are camera-positive by
     definition.
  5. nmap keyword fallback: only if all above probes fail.
- User-saved cameras always appear regardless of probe result (they were already
  manually confirmed by the user).
- Result: only devices that actually speak camera protocols show as cards;
  routers, printers, NAS, and other non-camera devices are silently skipped.

## 1.1.5
- Fixed JavaScript not executing at all (buttons dead, cameras not showing despite being loaded):
  Root cause was Python f-string multi-level escaping — Python processes ''' → ''' in the f-string
  before JavaScript ever sees it, causing a JS SyntaxError that silently killed the entire script block.
  Fix: moved ALL JavaScript to a module-level raw string (_JS = r"""...""") so braces, backslashes,
  and quotes are all literal. Only BASE is injected via str.replace(). Verified clean with Node.js --check.
- Fixed onerror quote conflict: replaced inline onerror attribute string with imgError(this) helper function
  that lives in the main JS scope, eliminating nested quote problems entirely.
- ARP scan fallback: if ARP returns fewer than 3 hosts (can happen without raw socket privileges in Docker),
  automatically supplements with ICMP ping scan before proceeding to port scan.
- Local machine IP always added to live host list so OAK Camera addon RTSP on port 8765 is always scanned.
- Removed not_camera auto-suppression: all live devices with open ports now get a card regardless of
  classify_device() verdict. Only explicit user action (Not a Camera button) blacklists a device.
  This prevents real cameras with generic HTTP banners from being silently hidden.
- Better Stage 1 completion message: shows per-method discovery counts
  (e.g. "23 via ARP, 1 via ONVIF, 2 via SSDP").

## 1.1.4
- Replaced single-pass nmap with a 4-stage intelligent scan pipeline:
  Stage 1: ARP ping scan (nmap -sn -PR) finds live hosts in 3-8 seconds, eliminating timeout waste on dead IPs; ONVIF WS-Discovery, SSDP/UPnP, and mDNS/Bonjour all run in parallel
  Stage 2: Focused camera port scan runs only on confirmed-live hosts (not the full /24), cutting scan time dramatically
  Stage 3: Stream probing unchanged — RTSP path probe, MJPEG, HLS, RTMP, WebRTC, WS-RTSP
  Stage 4 (optional): Broad sweep of ports 0-10000 on live hosts that did not respond to camera ports — enabled via Broad sweep checkbox in the header
- Added SSDP/UPnP discovery (M-SEARCH on 239.255.255.250:1900) — detects cameras that announce via UPnP
- Added mDNS/Bonjour discovery (224.0.0.251:5353) — queries _rtsp._tcp, _onvif._tcp, _camera._tcp, _nvr._tcp; gracefully handles port-in-use (avahi) by falling back to send-only mode
- Stage indicator badge shown in status bar during scans (Stage 1/4, Stage 2/4, etc.)
- Broad sweep toggle checkbox added to header (passes broad_sweep flag in scan POST body)
- Scan start now accepts JSON body with broad_sweep option; no separate options endpoint needed
- HTML caching: build_html() called once and cached to avoid regenerating on every page load

## 1.1.3
- Added port 8765 to scan list — this is where the OAK Camera addon serves RTSP via mediamtx, so AnyCam can now find the Luxonis camera
- False-positive filtering: default gateway IP is now automatically skipped (routers are never cameras)
- False-positive filtering: nmap product/service banners checked against known non-camera keywords (router, printer, NAS, etc.); matched devices shown with orange ⚠ Unverified badge
- False-positive filtering: "Not a Camera" button on each card permanently blacklists the device by IP (stored in /data/blacklist.json, survives restarts and rescans)
- ONVIF multi-stream (NVR) support: when credentials are submitted for an ONVIF device, GetProfiles + GetStreamUri called via SOAP/WS-Security to enumerate all camera channels; each channel gets its own card
- Port Scanner: new "Port Scan" button opens a full-range scan (all 65535 ports) of any IP with -sV -sC -A flags for maximum detail; results shown in a table with port, service, version, and script output
- Port scanner supports Pause (SIGSTOP) and Cancel (SIGTERM) controls
- "Connect Known Camera" button opens a manual add form with fields for IP, port, protocol (all 7 supported), optional stream path, and credentials; validates by probing before saving
- UI now has 3 views switchable via header buttons: Camera Grid, Port Scan, Connect Known Camera
- Uncertain devices shown with orange dot and ⚠ Unverified badge; still visible so user can verify or dismiss
- "Not a Camera" button shown on uncertain and credential-required cards

## 1.1.2
- Fixed 404: HA ingress proxy strips the /api/hassio_ingress/TOKEN prefix before
  forwarding to the addon, so routes must be registered at bare paths (/, /api/cameras,
  etc.). INGRESS_PATH is now only used as the JavaScript BASE for browser fetch() calls.

## 1.1.1
- Fixed 404 on sidebar click: register index route for both /path and /path/ variants
  (HA ingress arrives without trailing slash; aiohttp treats slash vs no-slash as distinct routes)

## 1.1.0
- Added HTTP MJPEG detection: probes 17 common paths, checks Content-Type for multipart/x-mixed-replace or image/jpeg; streams via ffmpeg proxy
- Added HLS detection: probes 11 common .m3u8 paths, verifies #EXTM3U body or mpegurl Content-Type; plays via hls.js in browser (native Safari fallback)
- Added RTMP detection: TCP connect to ports 1935/1936, verifies handshake byte (0x03/0x06 S0); streams via ffmpeg proxy
- Added WebRTC detection (preliminary): probes WHEP endpoints via SDP POST and heuristic GET; shows info card with signaling URL — full in-browser negotiation not yet implemented
- Added RTSP-over-WebSocket detection (preliminary): raw WebSocket upgrade probe with Sec-WebSocket-Protocol: rtsp; shows info card with ws:// URL — full WS-RTSP playback planned for a future release
- Extended nmap port list to include 1935, 1936 (RTMP) and 8888 (common MJPEG/HLS)
- Protocol-aware ffmpeg input flags: RTSP uses -rtsp_transport tcp, HLS uses -re (native rate)
- Colour-coded protocol badges in UI: each protocol has its own distinct colour
- Protocol icons added to badges: 📹 RTSP, 🔭 ONVIF, 🖼️ MJPEG, 📡 HLS, 📺 RTMP, 🔗 WebRTC, 🔌 WS-RTSP
- WebRTC and WS-RTSP cards show blue status dot (info) rather than yellow (needs_credentials)
- Info cards for WebRTC/WS-RTSP include explanatory text and direct link/URL to the detected endpoint
- Credential verification now protocol-aware: MJPEG uses HTTP Basic auth probe, HLS uses M3U8 path probe, RTSP/ONVIF use ffprobe

## 1.0.0
- Initial release
- nmap scan on all 10 common camera ports (554, 8554, 80, 8080, 443, 8443, 2020, 37777, 34567, 10554)
- ONVIF WS-Discovery multicast probe (UDP 239.255.255.250:3702) runs in parallel with nmap
- Automatic RTSP path probing across 25 common paths per discovered device
- Fernet-encrypted credential storage in /data/cameras.json (key in /data/secret.key)
- Credentials verified against live stream before being saved
- Card grid UI with live MJPEG feeds via per-camera ffmpeg processes
- Credential prompt with password masking for cameras requiring authentication
- Auto-connect on startup using saved credentials from previous sessions
- Camera renaming (click camera name)
- Remove camera from list
- Clear stored credentials per camera
- ONVIF badge shown for ONVIF-confirmed devices
- Progress bar with live status messages during scan
- Auto-scan triggered on first launch if no saved cameras exist
- Appears in HA left sidebar as "AnyCam"
