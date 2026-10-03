# AnyCam — Working Agreement

AnyCam is a Home Assistant addon (`local_camera_discovery`) that discovers
cameras on a network and presents them as a grid of live cards with an
Enhanced View for single-camera focus.

Since 3.0.0-rc1.0 the add-on is several Python files, listed in
`anycam_modules.py`: `camera_discovery.py` (entry point: settings, stores,
REST API, routing, start-up), `camera_db.py` (camera tables),
`page_script.py` (the page's JavaScript), `anycam_page.py` (the page
builder), `anycam_brand.py` (manufacturer database, keyword tables, brand
identification), `anycam_credentials.py` (password entry, DVR channel list,
Deep Re-Probe, manual add), `anycam_snap.py` (the snapshot loop),
`anycam_focus.py` (the Enhanced View engine and the focus state),
`anycam_motion.py` (motion detection, recording, night boost),
`anycam_go2rtc.py` (go2rtc), `anycam_probe.py` (stream probers and ONVIF
calls), `anycam_scan.py` (the network scan), `anycam_storage.py` (the
Storage tab), `anycam_host.py` (the link between them). A new file must be
added to `anycam_modules.py` and to the Dockerfile's COPY lines; the release
gate checks both. Package with `python package_release.py`.

**How the files connect (3.0.0-rc1.1).** `camera_discovery.py` is the entry
point and imports the others, so they must never import it back: a second
copy would load with its own cameras and state. A split file takes what it
needs from `camera_discovery.py` in two ways, both in `anycam_host.py`:

- `NEEDS`: a list of names at the top of the file, copied in once at
  start-up. Right for functions and for objects changed in place.
- `H.name`: read at the moment of use. Required for a value that
  `camera_discovery.py` replaces while it runs (anything it assigns under a
  `global` statement). Since 3.0.0-rc1.5 no file uses it: the focus state
  moved to `anycam_focus.py`, and the values the main file still replaces
  are read only by the main file.

The same rule applies between any two files: never `from X import name`
for a name that X assigns under a `global` statement; read `X.name`
(`anycam_go2rtc._GO2RTC_READY`, `anycam_focus._FOCUSED_CAMERA`). A value
that functions replace moves with those functions, into the file that owns
it.

**No import cycles (3.0.0-rc1.5).** A split file imports another split file
only if that file does not import it back, directly or through a third. When
a direct import would close a cycle, the name goes through the main file's
NEEDS instead (`anycam_focus.py` takes `_MOTION` that way, because
`anycam_motion.py` imports `anycam_focus`).

The release gate fails on a NEEDS name that is replaced at run time, and
`tests/check_names.py` fails on a name a function uses that is not there.
In the tests, `cd` is all the files seen as one namespace.

The rules below are **binding**. They were established over the life of the
project and are not suggestions. Read this file before doing anything.

---

## 1. Audit reports

Every release gets an audit report, saved as a PDF and surfaced to the user
proactively — without being asked. Each audit must include a
**best-practices compliance section** measured against
`docs/AnyCam_Coding_Best_Practices.md`.

This section was present through 2.3.2, silently dropped during the 2.4.x
cycle, and needs to stay in from now on.

## 2. Logs before code

When the user reports something broken, **ask for the relevant logs before
any coding or investigation.** Camera and system state too, where relevant.
No speculating and then coding.

If a log was supplied for one build but the bug is reported against a later
build, ask explicitly for the later build's log. Do not reason from the
older log.

## 3. Release packaging

The zip filename carries the full version string. The top-level folder
inside it is always `anycam`, with no version:

```
camera_discovery-3.0.1.zip
└── anycam/
```

Stage the source under that folder, then zip from the staging parent.
The working source directory itself keeps its own name and is not renamed.

**Why the folder has no version (changed 2026-09-29, CrystalHeeler's order).** Up to
2.6.4 the folder also carried the version. Home Assistant then showed "No
changelog found" after every update: on a store reload the Supervisor checks
for CHANGELOG.md at the add-on's *old* folder path before it reads the new
one (`supervisor/store/__init__.py`, `reload()`), and the old folder had
been deleted. A folder name that never changes avoids it. The version stays
visible in the zip name, `config.yaml` and the changelog.

**Why the folder is `anycam` (changed 2026-10-02, CrystalHeeler's order).**
Up to 3.0.0 it was `local_camera_discovery`. The add-on store repository
(github.com/crystalheeler/crystalheeler) holds AnyCam in `anycam/`, and the
zip now matches it. The Supervisor names a local add-on from the slug in
`config.yaml` (`local_` + `camera_discovery`), not from its folder
(`supervisor/store/data.py`), so the rename keeps the same add-on and its
saved cameras. The first update after the rename may show "No changelog
found" one time, for the reason above, and the old
`/addons/local_camera_discovery` folder must be deleted: two folders with
the same slug leave the Supervisor with only one of them.

## 4. Missing tools means stop

If a tool, connector, or permission is needed but unavailable, **stop and
say so.** Never silently fall back to a worse approach, and never work
around a missing tool without flagging it first.

## 5. Version naming — strict

Always use the **complete** version number. Every time. No exceptions.

Forbidden: `rc2`, `rc3.x`, `v2.1`, `2.6.x`, "the latest rc".
Required: `2.6.0-rc3.1`, `2.5.0-rc1.7`, `2.4.0-rc3.5`.

**Single digit per position.** After `rc2.9` comes `rc3.0`, never `rc2.10`.
After `2.2.9` comes `2.3.0`, never `2.2.10`.

This matters because the project has had collisions — there was both a
`2.2.8-rc2.5` and a `2.6.0-rc2.5`, and shorthand made them ambiguous.

## 6. Multi-question discipline

If you ask the user several questions in one turn, **wait for answers to all
of them** before acting. If they answer some and skip others, re-ask the
skipped ones explicitly. Never proceed to code on partial answers.

## 7. Pre-build consultation

Before starting any build: consult the open task list, and **confirm the
version number and scope with the user.** Never infer a version from prior
context. If a release displaces queued items, walk the list together and
renumber.

If the task list is missing from context, ask for it to be re-pasted.

## 8. Regressions are in our code first

When something that previously worked stops working, assume the regression
is in AnyCam's code — not the camera, the network, the OS, or a brand quirk.
Search prior conversation logs for the working behavior before treating an
external system as the cause.

## 9. No real names, locations or addresses

Never write a real name, a screen name, a location, an IP address, a
hostname, a MAC address or a device serial number of the owner's networks
into the repository: not in the changelog, the code, comments, tests,
documents, audit reports, commit messages or release notes. This is for
privacy, and because a user cannot know what a nickname for a private
network means.

- The owner is **CrystalHeeler**. Use no other name.
- The two test networks are **test system A** and **test system B**.
- Name a device by what it is: "the Lorex DVR", "the Hikvision PTZ", "the
  Microseven", "the Oak-D camera". Not by its address or part of it.
- Examples and tests use made-up addresses (`10.0.0.x`, `192.168.50.x`).
- A log line quoted in a document gets the same treatment before it is
  pasted.

---

## Coding standards

`docs/AnyCam_Coding_Best_Practices.md` is a binding 200+ item reference.
Structure:

- **Part 1 (1.1–1.18) — Python.** Truncated-function/AST checks, syntax and
  runtime errors, async/await, style, function design, mutability,
  conditionals, comprehensions, exception handling, resource management,
  logging, Pythonic idioms, type hints, class design, refactoring, stdlib
  first, testing.
- **Part 2 — JavaScript.** Async hygiene, no `await` in `.forEach()`, ASI.
- **Part 3 — HTML.**
- **Part 4 — CSS.** Note `/* */` not `//`; `100dvh` over `100vh` on mobile.
- **Part 5 — Combined/Polyglot.** Includes the errors-that-sneak-past-review
  table, notably CSS-in-Python f-string `{{` escaping.
- **Part 6 — Project Engineering.** Security (validate at the boundary,
  allowlists, no `shell=True` with untrusted input), dependency management,
  version control.

## Release gate

`verify_release.py` is an eight-gate check that must pass before packaging:

1. AST/compile clean for every module, no duplicate top-level definitions;
   `anycam_modules.py` matches the imports and the Dockerfile
2. Semantic function contracts satisfied
3. Best-practice audit clean (includes blocking file I/O in async code and
   unfilled page placeholders)
4. Version consistent across `camera_discovery.py` and `config.yaml`
5. `CHANGELOG.md` top entry matches the version being built
6. Settings agree across `run.sh`, `config.yaml` options and schema, and
   `translations/en.yaml` (2.6.6)
7. Every Dockerfile input exists where the build fetches it: apt pins,
   pip wheels, go2rtc digests, base image platforms (2.6.6; needs internet
   access, and fails without it)
8. The tests in `tests/` all pass (3.0.0-rc1.0; `python
   tests/run_tests.py`; needs Node.js for the page checks). They include
   an undefined-name check, which catches a function that was moved or
   deleted while something still uses it

---

## Test environment

**Home Assistant OS** on a Raspberry Pi 4 (aarch64). Hardware decode
via rpivid — the correct overlay line in `/boot/firmware/config.txt` is
`dtoverlay=rpivid-v4l2`, with the `-v4l2` suffix.

The real addresses of the test devices are not written in this repository
(rule 9). Logs show them; the session's private notes map them to the
names below.

**Test system A**
- TP-Link Tapo (OUI-only, card suppressed)
- Two UniFi devices (OUI-only, suppressed)
- Microseven Hipcam — HTTP-polled at ~1 fps historically; per-IP TCP
  rate limit, 5s brand cooldown; prone to firmware lockout
- Hikvision DS-2DE4A425IW-DE PTZ — HEVC main 2560x1440; sub-stream H.265
  704x480 at 20 fps (camera settings, 2026-10-02; older notes said MJPEG);
  ONVIF returns 0 profiles. AnyCam stores the streams it found at password
  entry, so a camera setting changed later is not seen until the password
  is entered again

**Test system B**
- Lorex/Dahua DVR-NVR (D861A8B-Z, 8ch), channels ch2–ch8 populated
- An H.264 stream on port 8765 from CrystalHeeler's own Oak-D camera
  add-on, not a third-party camera

## Known device behavior

- **Lorex/Dahua**: ONVIF returns zero profiles (service disabled in
  firmware). RTSP pattern `/cam/realmonitor?channel=N&subtype=M`. Digest
  realm is `Login to <32-hex>`. The DVR allocates virtual stream slots for
  all 8 channels regardless of physical camera presence. Login lockout at 10
  failed attempts. The main stream runs at about 7 fps (measured from
  recordings, 2026-10-01; the DVR's setting tops out at about 8 fps) — that
  ceiling is the DVR, not AnyCam. Main stream 3840x2160 H.265, keyframe
  every second; the sub-stream is at `subtype=1`.
- **Hipcam/Microseven**: per-IP TCP rate limiting. Two opens inside the 5s
  window draws an RST. Soft-degraded RTSP state (accepts TCP, returns
  garbage) is distinct from hard lockout (refuses TCP) — only a power cycle
  reliably clears it.
- **Pi 4 rpivid**: HEVC hardware warmup takes 2.5–7s at 4K. Software HEVC
  decode at 4K cannot produce a first frame faster than hardware warmup
  completes.

## Open issues and queued work

`docs/BUILD_PLAN.md` is the task list (rule 7), with a readable copy in
`docs/BUILD_PLAN.html`. It supersedes the lists that used to be here, most
of whose items have shipped (see its Done section). Standing reminders:

- `skip_nonref=true` breaks every H.264 launch: ffmpeg rejects `nonref`
  (build plan B3).
- The Enhanced View automatic-quality redesign **requires full discussion
  before any coding** (build plan C3).
- `STREAM_DB` → `CAMERA_DB` consolidation and runtime use of
  `default_ports` need a plan document first (build plan D2).
- The tests are in `tests/` since 3.0.0-rc1.0 (build plan E7). They cover
  the scan since 3.0.0-rc1.4, and password entry, the snapshot loop, the
  Enhanced View engine, brand identification and the page builder since
  3.0.0-rc1.5. Add tests before changing code they do not cover.

## Repository notes

The old release zips (`archive/`), the old chat transcripts and the audit
reports that held private items were removed on 2026-10-02, and the git
history was rewritten to remove names and addresses (rule 9). Commit
hashes from before that date no longer exist.
