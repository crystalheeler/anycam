# Camera database merge plan (build plan D2)

Status: plan written, tests added and merge done in 3.5.0-rc1.0 (2026-10-04), in that order, as
CrystalHeeler decided on 2026-10-04 ("Write the plan, add tests, then merge"). Not field-tested.

## Before 3.5.0

`camera_db.py` held two tables about the same brands:

| Table | Shape | Holds | Read by |
|---|---|---|---|
| `STREAM_DB` | dict, 40 entries, keyed by a slug (`"hikvision"`) | `match` keywords, `rtsp` paths, `mjpeg` path, `snap` path, RTSP `port` | `_match_stream_db`, `_match_stream_db_slug` (password entry, Deep Re-Probe, the RTSP walker, snapshot URLs) |
| `CAMERA_DB` | list, 76 entries, by display name (`"Hikvision"`) | recognition (titles, headers, nmap banners, ONVIF scopes, aliases), `default_ports`, throttle and request behaviour, the DVR recipe, notes | brand identification, throttling, the scan |

Two problems:

1. **Two places for one brand.** A new brand, or a corrected stream path, needed two edits in two
   tables with different keys. The 3.0.0-rc1.5 tests found one result of this: password entry read
   `throttle_type` from a `STREAM_DB` entry, but that field is in `CAMERA_DB` (B24, fixed in
   3.0.1-rc1.0).
2. **`default_ports` was not used at run time.** The scan's port list was written by hand from the
   `CAMERA_DB` ports in 2.4.0-rc2.1. A brand added later with a new port was not scanned on it.

## The merge

1. **One table.** Each `CAMERA_DB` brand gets a `"streams"` list holding its former `STREAM_DB`
   entries. A brand can hold more than one: Hikvision holds `hikvision` and `ezviz` (EZVIZ is
   Hikvision's consumer brand), Dahua holds `dahua` and `imou` (Imou is Dahua's). 38 brands hold
   the 40 entries.
2. **`STREAM_DB` is built, not written.** `_build_stream_db` in `camera_db.py` makes the same dict
   from the `"streams"` lists at start-up. Every reader stays unchanged.
3. **Order kept.** `_match_stream_db` takes the longest matching keyword; between two equally long
   ones, the first entry wins. Each stream entry keeps its place as `"rank"`, and `STREAM_DB` is
   built in rank order, so a tie goes the same way as before.
4. **Ports from the table.** `anycam_scan.CAMERA_RELEVANT_PORTS` is built at start-up from the
   classifier ports, the documented extra ports (the hand-written list, kept), every brand's
   `default_ports` and every stream entry's `port`. On 2026-10-04 every brand port was already in
   the list, so the scan uses the same 54 ports; a brand added later is scanned on its ports.

The data was moved by a script, not by hand: it checked that every slug maps to an existing brand,
wrote each entry's fields unchanged, and compared the built `STREAM_DB` with the old one.

## Tests (section AF in `tests/test_server.py`)

| Check | What it proves |
|---|---|
| AF4 | The built `STREAM_DB` is byte for byte the 3.4.0 table (SHA-256 of its JSON); the paths are written only on the brands; ranks 0 to 39, each once |
| AF5 | Every keyword of every entry still finds an entry, and the named ones find their own (`ezviz`, `imou`, `dh-ipc` to `dahua`) |
| AF6 | The scan's port list is the same 54 ports, classifier ports first; a brand added with a new port is scanned on it |

The tests that pinned password entry, Deep Re-Probe and the stream table before the merge (sections
Z and Y) pass unchanged after it.

## Not changed

- No brand's recognition fields, throttle values or recipe.
- No new brand; EZVIZ and Imou still have no recognition entry of their own.
- The readers of `STREAM_DB` keep their code.
