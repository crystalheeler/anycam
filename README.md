# AnyCam

A Home Assistant addon (`local_camera_discovery`) that discovers cameras on a
local network and presents them as a grid of live cards, with an Enhanced View
for single-camera focus, hardware-accelerated decode, and PTZ control.

## Repository layout

```
camera_discovery.py     Main application: entry point and logic
camera_db.py            Camera tables (brands, stream paths, known behaviour)
page_script.py          The page's JavaScript
anycam_motion.py        Motion detection, motion recording, night boost
anycam_go2rtc.py        go2rtc: supervisor, streams, live-view proxy
anycam_storage.py       The Storage tab
anycam_host.py          Connects the files above to camera_discovery.py
anycam_modules.py       The list of the files above
package_release.py      Builds _build/camera_discovery-<version>.zip
config.yaml             Addon manifest and options schema
run.sh                  Addon entrypoint; exports config as env vars
Dockerfile              Addon image (base image set here since 2.6.6)
translations/en.yaml    Option help text
verify_release.py       Eight-gate pre-packaging check
tests/                  Behaviour tests; `python tests/run_tests.py`
CHANGELOG.md            Full release history

docs/                   Plan documents, RTSP database, research
docs/audits/            Audit report PDFs, one per release
docs/legacy/            Pre-repository working documents
archive/                Release zips for milestone versions
```

## Release process

1. Confirm version number and scope before starting (see `CLAUDE.md`)
2. Make changes
3. `python verify_release.py` — all eight gates must pass
4. Stage under `camera_discovery-<version>/`, zip from the staging parent
5. Produce an audit report PDF including a best-practices compliance section
6. Tag, and create a Release if the version is a milestone

## History note

This repository was reconstructed from archived release zips in July 2026.
Commits before that date are backdated to their original build timestamps.
The window from 2.4.0-rc2.2 through 2.5.0-rc1.7 has no recoverable source;
see `docs/gap-2.4.0-to-2.5.0/` for the audit reports covering it.
