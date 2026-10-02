# AnyCam tests

| File | What it checks |
|---|---|
| `test_server.py` | The real functions and handlers of every add-on file, against stand-ins for go2rtc, ffmpeg, cameras and Home Assistant (448 checks) |
| `test_page.mjs` | The real page script, in Node.js, with a fake clock and a stub page |
| `fake_go2rtc.py` | Stand-in for the go2rtc binary |
| `check_names.py` | Every function's global names exist in its module (catches a moved or deleted function) |
| `run_tests.py` | Runs all three; the release gate calls it |

Run everything:

```
python tests/run_tests.py
```

Needs Python with `aiohttp`, `cryptography` and `Pillow`, and Node.js. No
camera, ffmpeg or network.

In `test_server.py`, `cd` is every file of the add-on seen as one namespace:
reading finds a name in the file that defines it, and writing replaces it in
every file that holds it.

Since 3.0.0-rc1.5, sections V to Z cover the parts that left the main file
in that build: the manufacturer database and brand identification (V), the
page builder (W), the Enhanced View engine (X, with C, E and G), the snapshot
loop (Y) and password entry (Z). They were written and passing before each
move.

Not covered yet: the single probers themselves (the scan and password tests
replace them with stand-ins), and ffmpeg and ffprobe themselves.
