# AnyCam — Working Agreement

AnyCam is a Home Assistant addon (`local_camera_discovery`) that discovers
cameras on a network and presents them as a grid of live cards with an
Enhanced View for single-camera focus.

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
inside it is always `local_camera_discovery`, the add-on's slug, with no
version:

```
camera_discovery-2.6.5.zip
└── local_camera_discovery/
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

`verify_release.py` is a five-gate check that must pass before packaging:

1. AST/compile clean, no duplicate top-level definitions
2. Semantic function contracts satisfied
3. Best-practice audit clean
4. Version consistent across `camera_discovery.py` and `config.yaml`
5. `CHANGELOG.md` top entry matches the version being built

---

## Test environment

**HAOS** at `172.16.0.35:8123`, Raspberry Pi 4 (aarch64). Hardware decode
via rpivid — the correct overlay line in `/boot/firmware/config.txt` is
`dtoverlay=rpivid-v4l2`, with the `-v4l2` suffix.

**test system A** (`10.0.0.0/26`)
- `.12` TP-Link Tapo (OUI-only, card suppressed)
- `.13` / `.14` UniFi (OUI-only, suppressed)
- `.22` Microseven Hipcam — HTTP-polled at ~1 fps historically; per-IP TCP
  rate limit, 5s brand cooldown; prone to firmware lockout
- `.33` Hikvision DS-2DE4A425IW-DE PTZ — HEVC main, MJPEG sub, ONVIF
  returns 0 profiles

**test system B** (`192.168.1.0/24`)
- `.217` Lorex/Dahua DVR-NVR (D861A8B-Z, 8ch), channels ch2–ch8 populated
- `.73:8765` h264 camera

## Known device behavior

- **Lorex/Dahua**: ONVIF returns zero profiles (service disabled in
  firmware). RTSP pattern `/cam/realmonitor?channel=N&subtype=M`. Digest
  realm is `Login to <32-hex>`. The DVR allocates virtual stream slots for
  all 8 channels regardless of physical camera presence. Login lockout at 10
  failed attempts. Encoder caps around 5 fps — that ceiling is the DVR, not
  AnyCam.
- **Hipcam/Microseven**: per-IP TCP rate limiting. Two opens inside the 5s
  window draws an RST. Soft-degraded RTSP state (accepts TCP, returns
  garbage) is distinct from hard lockout (refuses TCP) — only a power cycle
  reliably clears it.
- **Pi 4 rpivid**: HEVC hardware warmup takes 2.5–7s at 4K. Software HEVC
  decode at 4K cannot produce a first frame faster than hardware warmup
  completes.

## Open issues carried into this repo

- `skip_nonref=true` breaks every h264 launch. ffmpeg rejects `nonref` as a
  value for `skip_frame`. Pre-existing, predates 2.6.0-rc2.5. Needs a
  different approach or removal of the option.
- `fast_stream_start=true` is actively harmful at 4K HEVC — software decode
  can't beat hardware warmup, so it burns CPU on a parallel decode that
  never renders, and opens a second RTSP session that triggers
  `RTP bad cseq` warnings. Should be gated by resolution and codec.
- ONVIF SOAP calls during credential auth do not appear to consult
  `_throttle_wait_if_needed`, so several TCP opens land inside a
  rate-limited brand's cooldown window. Latent, pre-existing.

## Queued work

- Promote a stable rc to **2.6.0** final once field issues resolve
- Runtime consumption of `default_ports`; `STREAM_DB` → `CAMERA_DB`
  consolidation
- Bundle rpi-ffmpeg for rpivid HEVC hardware decode (Dockerfile hw-decoder
  selection for Pi 4)
- Lorex/Dahua DVR family support (~170 LoC) — see
  `docs/Lorex_Dahua_DVR_Family_Support_Plan.md`
- RTSP OPTIONS fingerprinting as reusable core infrastructure — see
  `docs/RTSP_OPTIONS_Fingerprint_Helper_Plan.md`
- Enhanced View resolution auto-step-down redesign — **requires full
  discussion before any coding.** The current system steps within the same
  profile, which masks real quality changes.
- Return card view to live RTSP rather than HTTP snapshot (no version
  assigned)
- Global mutable variables refactor (SigRev-3 Issue 2)
- Behavioral tests (aspirational)

## Repository notes

`archive/` holds historical release zips that predate this repository.
Do not treat anything under it as current source. When searching history,
scope to `main`.
