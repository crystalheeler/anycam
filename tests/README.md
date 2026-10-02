# AnyCam tests

| File | What it checks |
|---|---|
| `test_server.py` | The real functions and handlers in `camera_discovery.py`, against stand-ins for go2rtc, ffmpeg and Home Assistant |
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

Not covered yet: the network scan, the password entry path, and most of the
snapshot loop.
