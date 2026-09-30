# AnyCam Coding Best Practices
### Compiled from 200+ sources — Python · JavaScript · HTML · CSS · Combined/Polyglot

---

## How To Use This Document

This is a living reference, not a checklist to run through once. The issues in PART 1.1–1.5 and PARTS 2–5 are ranked within each section by how likely they are to cause **silent, hard-to-catch bugs** — the kind that pass `ast.parse()` and still make production behave wrong. Sections 1.6–1.18 and PART 6 (added May 2026) shift focus from "errors that sneak through" to **proactive engineering practices** — design choices that prevent classes of bug from arising in the first place.

---

## PART 1 — PYTHON

### 1.1 The Truncated Function Problem (Most Relevant to AnyCam)

**What it is:** A function is syntactically valid Python but logically incomplete — it ends before its cleanup or state-management code, because the code was edited or generated under memory/context pressure.

**Why `ast.parse()` doesn't catch it:** Python has no required minimum function body. A function that ends with `pass` or one assignment is perfectly valid syntax. `ast.parse()` is a spell-checker, not a proofreader.

**The fix — semantic contract checks:** After `ast.parse()`, walk the AST to verify that critical functions contain their expected structural elements. For AnyCam this means:

```python
import ast, sys

src = open('camera_discovery.py').read()
tree = ast.parse(src)

REQUIRED_CALLS = {
    'run_verification_scan': ['save_cameras', 'SCAN_STATE'],
    'http_snap_loop':        ['asyncio.sleep', '_snap_state'],
    'snap_loop':             ['_snap_state', 'asyncio'],
    'build_html':            ['return', 'INGRESS_PATH'],
}

for node in ast.walk(tree):
    if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
        if node.name in REQUIRED_CALLS:
            src_slice = ast.get_source_segment(src, node) or ''
            for required in REQUIRED_CALLS[node.name]:
                if required not in src_slice:
                    print(f'✗ {node.name}() is missing expected content: {required}')
                    sys.exit(1)
print('✓ Semantic contract checks passed')
```

**Sources:** Real Python (ast module), Python docs (ast.NodeVisitor), MDN semantic analysis, oligo.security/academy, testrigor.com/blog

---

### 1.2 Syntax Errors — The Classic Five

| Error | Cause | Detection |
|---|---|---|
| `SyntaxError: expected ':'` | Missing colon on `if`/`for`/`def`/`while`/`class` | `ast.parse()` |
| `IndentationError` | Mixed tabs and spaces, or wrong indent level | `ast.parse()` / Flake8 |
| `TabError` | Both tabs AND spaces in same file | `ast.parse()` |
| `SyntaxError: EOL while scanning string literal` | Unclosed string | `ast.parse()` |
| `SyntaxError: invalid syntax` (near keyword) | Misspelled keyword e.g. `impotr`, `whille`, `retrun` | `ast.parse()` |

**Rule:** Always use 4 spaces per indent level (PEP 8). Configure your editor to insert spaces on Tab. Never mix.

**Sources:** scrapingant.com, betterstack.com, realpython.com/invalid-syntax-python, stackify.com, oxylabs.io, crawlbase/medium, oxford.train.rse

---

### 1.3 Runtime Errors That Syntax Checks Miss

**NameError** — Variable or function name not found. Most common causes:
- Typo in variable name (Python is case-sensitive: `Camera` ≠ `camera`)
- Variable defined inside a function, used outside (scope error)
- Variable used before assignment
- Forgot quotes on a string: `print(hello)` vs `print("hello")`
**TypeError** — Wrong type for an operation. Common in AnyCam:
- Calling a method on `None`: `camera.get("snap_url").split("/")` fails if snap_url is None
- Passing wrong argument type to a function
- `str + int` without conversion
**AttributeError** — Accessing a property that doesn't exist. Use `dict.get()` instead of `dict[]` when key may be missing.

**KeyError** — Accessing a dict key that doesn't exist. Prefer `.get(key, default)`.

**Sources:** aguaclara.github.io, editorialge.com, betterstack.com, apxml.com

---

### 1.4 Async/Await Pitfalls (Critical for AnyCam)

AnyCam is an async application. These mistakes are silent — no error, just wrong behavior.

| Mistake | Symptom | Fix |
|---|---|---|
| Calling coroutine without `await` | Function silently does nothing; returns a coroutine object, not a value | Add `await` |
| `asyncio.run()` inside an existing event loop | `RuntimeError: cannot run nested event loop` | Use `asyncio.create_task()` instead |
| `time.sleep()` inside async function | Blocks the entire event loop; no other coroutine runs | Use `await asyncio.sleep()` |
| `requests.get()` inside async function | Synchronous HTTP blocks event loop | Use `aiohttp` |
| `open()` inside async function | Blocking file I/O | Use `aiofiles` or `loop.run_in_executor()` |
| `await` inside a loop sequentially | Operations run one-by-one instead of concurrently; 3× slower | Collect tasks, then `await asyncio.gather()` |
| Missing `CancelledError` handling | Task cancellation silently swallowed | `except asyncio.CancelledError: break/raise` |
| CPU-bound work in async | Blocks the event loop despite `async def` | Use `loop.run_in_executor()` with `ThreadPoolExecutor` |

**The `await`-in-loop anti-pattern (directly applicable to AnyCam snap loops):**
```python
# BAD — each request waits for the previous
for cam in cameras:
    result = await probe_rtsp(cam)

# GOOD — all run concurrently
results = await asyncio.gather(*[probe_rtsp(cam) for cam in cameras])
```

**Missing cleanup (the run_verification_scan bug):** Every async function that sets state must have a `try/finally` or a cleanup block that runs even on exception:
```python
async def my_loop():
    SCAN_STATE['running'] = True
    try:
        # ... work ...
    finally:
        SCAN_STATE['running'] = False  # ALWAYS runs, even on exception
        save_cameras()
```

**Sources:** Python docs (asyncio-dev, asyncio-task), shanechang.com, datacamp.com, realpython.com/async-io-python, betterstack.com, medium.com/@aarmanj08, discuss.python.org, paulnorvig.com

---

### 1.5 Style and Maintainability

- **PEP 8:** 4-space indent, 79-char line limit, `snake_case` for functions/variables, `UPPER_CASE` for constants, `CamelCase` for classes
- **Type hints:** Add return annotations to all functions. A function without `-> None` or `-> str` is harder to review and easier to truncate invisibly
- **Bare `except:`:** Always name the exception type. `except Exception:` is acceptable; `except:` swallows `KeyboardInterrupt` and `SystemExit`
- **Mutable default arguments:** `def f(x=[]):` is a classic bug — the list persists between calls. Use `def f(x=None): x = x or []`
- **Global mutable state:** Each global dict or list that's mutated from multiple coroutines is a race condition waiting to happen; prefer passing state explicitly
- **`f-string` with HTML/JS:** Never use raw f-strings to inject user input into HTML — use `html.escape()`. In AnyCam's case the values come from camera metadata, not user input, but the pattern matters
**Tools:** Pylint, Flake8, Black (formatter), Ruff (fast all-in-one), mypy (type checker), Pyright (type checker)

**Sources:** PEP 8 (python.org), PEP 501 (peps.python.org), realpython.com, scrapingant.com, editorialge.com

---

---

### 1.6 Function Design

Beyond naming and return-statement consistency, function-level design choices have a big impact on whether code stays maintainable.

- **Single, clear responsibility.** If you find yourself adding "and" to a function name (`fetch_and_parse_and_save`), consider splitting it. Smaller functions are easier to test and reuse.
- **Verb-first names.** Functions perform actions: `write_camera`, `fetch_rtsp_metadata`, `validate_credentials`. Avoid noun-only names like `data()` or `user_input()`.
- **Snake_case** (PEP 8); leading underscore for non-public helpers (`_probe_rtsp_paths_single_socket`).
- **Pure functions where possible.** A pure function depends only on its arguments and has no side effects (no I/O, no global mutation). Pure functions are easier to test and reason about. AnyCam example: `_parse_throttle_seconds` is pure; `find_rtsp_path` is not (network I/O, mutates state) — that's appropriate, but the boundary should be deliberate.
- **Limit parameter count.** More than ~5 positional parameters is a smell. Group related values into a dataclass or pass a dict.
- **Keyword-only arguments with sensible defaults** for optional parameters. Prevents call-site mistakes:
  ```python
  # WRONG
  def find_rtsp_path(ip, creds, timeout, retries, fast_bail):
      ...
  # call site is unreadable:
  result = find_rtsp_path("10.0.0.22", c, 5, 3, True)

  # RIGHT
  def find_rtsp_path(ip, creds, *, timeout=5, retries=3, fast_bail=True):
      ...
  result = find_rtsp_path("10.0.0.22", c, fast_bail=True)
  ```
- **No hidden expensive work.** A function called `get_config()` that hits the network is a trap. If it does network I/O, name it `fetch_config()` or `load_config_from_server()`.
### 1.7 Object Mutability and Mutable Defaults

The classic mutable-default-argument bug is silent and easy to miss:

```python
# WRONG — list persists across calls
def add_path(path, paths=[]):
    paths.append(path)
    return paths

print(add_path("a"))  # ['a']
print(add_path("b"))  # ['a', 'b']  ← surprise!

# RIGHT — None sentinel, fresh list each call
def add_path(path, paths=None):
    if paths is None:
        paths = []
    paths.append(path)
    return paths
```

Other mutability rules that matter for AnyCam:

- **Prefer immutable types** (tuple, frozenset, frozen dataclass) for stable records. Immutable values can be shared safely between coroutines without copy.
- **Be explicit when sharing mutable state across coroutines.** AnyCam's `CAMERAS`, `SCAN_STATE`, `_snap_state`, and `_THROTTLE_TRACK` dicts are all globally mutated from multiple coroutines. Document the ownership and the synchronization assumption (or add an asyncio.Lock if one is needed).
- **Right data structure for the job:** list (ordered, growable), dict (keyed lookup, ~O(1)), set (uniqueness/membership tests), tuple (fixed-size record), frozenset/frozendict (immutable variants).
### 1.8 Conditionals and Control-Flow Patterns

**Guard clauses** flatten the happy path. Compare:

```python
# WRONG — pyramid of doom
def process(camera):
    if camera:
        if camera.get('manufacturer'):
            if camera['manufacturer'] == 'Hipcam/Microseven':
                return handle_hipcam(camera)
    return None

# RIGHT — early exits, flat happy path
def process(camera):
    if not camera:
        return None
    if not camera.get('manufacturer'):
        return None
    if camera['manufacturer'] != 'Hipcam/Microseven':
        return None
    return handle_hipcam(camera)
```

- **Truthy check for empty sequences** (`if not paths:` — both PEP 8 and Real Python agree).
- **Explicit `is None` for None checks.** `if value is None:` not `if not value:`, because `0`, `""`, `[]` are all falsy and you almost certainly mean only `None`.
- **Dict dispatch over long elif chains.** When branching on a single value, a lookup table is clearer and easier to extend than a chain of `elif` clauses:
  ```python
  # WRONG
  if codec == 'h264':
      return handle_h264(...)
  elif codec == 'h265':
      return handle_h265(...)
  elif codec == 'mjpeg':
      return handle_mjpeg(...)

  # RIGHT
  CODEC_HANDLERS = {'h264': handle_h264, 'h265': handle_h265, 'mjpeg': handle_mjpeg}
  return CODEC_HANDLERS[codec](...)
  ```
- **Walrus operator** (`:=`) is fine when it eliminates a duplicated computation, e.g. `if (m := re.search(pat, s)):`. Skip it when it makes the line harder to read.
- **`match` statement (3.10+)** for branching on data shape — cleaner than nested if/isinstance chains.
### 1.9 Loops, Comprehensions, and Generator Expressions

**Iterate directly over the iterable**, not by index:
```python
# WRONG
for i in range(len(cameras)):
    print(cameras[i])
# RIGHT
for cam in cameras:
    print(cam)
```

- **`enumerate()` when you need both index and value:** `for i, cam in enumerate(cameras):`
- **`zip()` for parallel iteration:** `for url, codec in zip(urls, codecs):`
- **Avoid string concatenation in loops** — `s += part` is O(n²) in CPython. Use `''.join(parts)` or `io.StringIO`.
**Comprehensions for transformations:**
```python
# Transformation
authed = [c for c in cameras if c.get('credentials')]

# Generator expression for one-pass / large data — doesn't materialize
total_fps = sum(c.fps for c in cameras if c.active)
```

- **Keep comprehensions flat.** Multiple `for` clauses + multiple `if` filters → switch to a regular for loop, it'll be clearer.
- **No side effects in comprehensions.** They're for computing values, not for printing or mutating state. Use a regular loop if you need side effects.
- **Generator expressions are single-use.** Once consumed, exhausted. If you need to iterate twice, materialize to a list first.
### 1.10 Exception Handling Philosophy

Beyond the mechanical PEP 8 rules (specific excepts, no bare `except:`, inherit from Exception), there's a design dimension:

- **Fail fast.** Raise as soon as you detect bad state — don't let it propagate.
- **Raise low, catch high.** Low-level helpers raise specific exceptions; the top-level handler (HTTP route, async task) catches them and decides how to surface to the user. AnyCam's `find_rtsp_path` and `probe_rtsp_socket` raise/return exceptional results; the request handlers in `api_set_credentials` etc. translate those into HTTP responses. Keep this separation.
- **EAFP > LBYL** (Easier to Ask Forgiveness than Permission, vs Look Before You Leap):
  ```python
  # LBYL — race condition window between check and use
  if os.path.exists(path):
      with open(path) as f:
          ...
  # EAFP — atomic, Pythonic
  try:
      with open(path) as f:
          ...
  except FileNotFoundError:
      ...
  ```
- **Catch the narrowest exception you can handle.** `except Exception:` masks bugs you didn't anticipate. AnyCam has many `except Exception:` blocks — most are appropriate (top-level catchalls in long-running coroutines that must not die), but each one is worth scrutinizing.
- **Don't use exceptions for routine control flow.** Exceptions signal errors. Branching is `if/else`.
- **Custom exceptions for domain errors.** `HipcamRateLimitedError`, `OnvifAuthRequired`. Inherit from Exception.
- **`raise NewError(...) from original`** preserves the original traceback. Don't swallow it silently.
- **`logger.exception(...)`** inside except blocks captures the stack trace into the log. Or pass `exc_info=True` to any logger call.
### 1.11 Resource Management with Context Managers

Always use `with` for resources that need setup + teardown: files, sockets, locks, subprocesses, sessions, locks.

- **Keep resource lifetimes short.** Acquire inside the function that uses the resource; release before returning. Don't open a socket at module level and hope it gets closed.
- **Custom context managers** for any setup/teardown pair you write more than once:
  ```python
  from contextlib import contextmanager

  @contextmanager
  def rtsp_socket(host, port, timeout=5):
      sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
      sock.settimeout(timeout)
      sock.connect((host, port))
      try:
          yield sock
      finally:
          sock.close()

  # Usage — guaranteed cleanup even on exception
  with rtsp_socket(ip, 554) as s:
      s.send(options_request)
      response = s.recv(4096)
  ```
- **`contextlib.ExitStack`** for dynamically composing multiple context managers (e.g. opening N files based on runtime input).
- **Don't leak resources across module boundaries.** Returning an open socket or file handle from a low-level helper makes it the caller's job to close — easy to forget. Prefer returning the data the caller actually wants.
- **AnyCam-specific:** `snap_loop` and `http_snap_loop` manage ffmpeg subprocess lifetimes via try/finally. These are good candidates for promotion to custom context managers in a future refactor — would centralize the cleanup logic and reduce the chance of a leak in a new code path.
### 1.12 Logging

- **Use stdlib `logging`, not `print()`.** Logging levels let you control verbosity at runtime; print can't be filtered, redirected to a file, or formatted consistently.
- **Module-level logger with `__name__`:**
  ```python
  import logging
  log = logging.getLogger(__name__)
  ```
  This names log records after their module, so you can tune verbosity per-module via config.
- **Use the right level:** DEBUG for noisy diagnostics, INFO for normal operations, WARNING for unexpected-but-handled, ERROR for failures, CRITICAL for unrecoverable. Consistent levels make log filtering useful.
- **Configure once near the entry point** (in `if __name__ == '__main__':`, a CLI main, or a dedicated `logging_config.py`). Library/module code should NEVER call `basicConfig()` or add global handlers — that's the application's job.
- **Include useful context:** identifiers, paths, response codes — but **never credentials or PII.** AnyCam's `_strip_creds` helper handles URLs in JSON payloads; the same discipline should apply to log messages. If you log a URL that contains creds, strip them first.
- **`logger.exception(...)` inside except blocks** captures the stack trace automatically. Or `logger.error(..., exc_info=True)`. This is invaluable for production debugging.
- **No structured-logging hygiene mistakes:** don't pre-format with f-strings if you can help it. `log.info("found %d cameras", n)` lets the logging system decide whether to format; `log.info(f"found {n} cameras")` always formats, even when the level is filtered out.
### 1.13 Pythonic Idioms

A non-exhaustive catalog of idioms that make Python code feel native:

- **Context managers for resources** (covered in 1.11) — `with` over manual try/finally.
- **Comprehensions and generator expressions** (covered in 1.9) — over manual append-loops.
- **`enumerate()`, `zip()`, `any()`, `all()`, `sum()`** — often replace 5-line loops with a one-liner.
- **Unpacking:** `a, b = b, a` for swap; `first, *rest = lst` for variable destructuring; `*args, **kwargs` for forwarding.
- **f-strings** for formatting (Python 3.6+) — over `.format()` or `%` interpolation.
- **`if __name__ == '__main__':`** for script entry points.
- **Duck typing** where reasonable: instead of `if isinstance(x, list): x.append(...)`, just call `x.append(...)` and let TypeError happen if x doesn't support it. Less rigid; works with any list-like type. (Note: this complements rather than overrides PEP 8's "use isinstance() not type() comparisons" — that rule is about HOW to type-check, this is about WHETHER.)
- **`pathlib.Path`** over `os.path` string manipulation: `(Path('/tmp') / 'snap.jpg').read_bytes()`.
- **`dict.get(key, default)`** over try/except KeyError when missing keys are normal.
- **`collections.Counter`, `collections.defaultdict`, `itertools.chain`** — reach for stdlib helpers before rolling your own.
- **`functools.lru_cache`** for memoization of pure functions.
### 1.14 Type Checking Workflow

AnyCam currently uses no type hints. Adding them gradually would catch a class of bugs that even semantic-contract checks miss.

- **Add hints gradually.** Start with public APIs and high-traffic functions; expand as you touch existing code. Don't try to annotate everything in one pass.
- **Use a static type checker** (mypy, pyright, or pyre) as part of development. Catches type errors before runtime. AnyCam's `verify_release.py` could grow a type-check gate alongside the existing five.
- **Prefer precise types over `Any`.** `Any` opts out of type checking — bugs slip through.
- **Model structured data explicitly:**
  ```python
  from typing import TypedDict, Optional

  class CameraRecord(TypedDict, total=False):
      id: str
      ip: str
      manufacturer: str
      stream_url: str
      stream_codec: str
      stream_width: int
      stream_height: int
      stream_fps: float
      credentials: Optional[bytes]   # encrypted blob or None
      # ... etc
  ```
  Right now AnyCam's CAMERAS records are plain dicts with ~50 unenforced keys. A TypedDict would catch typos (`cam['mainufacturer']`) and missing required fields at type-check time.
- **`Protocol`** when you need duck typing with type-checker support (e.g. "anything with a `.read()` method").
- **Keep type hints in sync with behavior.** Stale types lie. When you change a function's behavior, update its annotation in the same commit.
- **`# type: ignore[error-code]`** and `typing.cast()` are escape hatches. Use them sparingly with a comment explaining why.
### 1.15 Class Design

AnyCam currently uses dict-based records and module-level functions, with very few classes. That's not wrong — it's a deliberate tradeoff that keeps the code procedural and inspectable. But if a future refactor introduces classes, these principles apply:

- **Single responsibility per class.** Each class models one concept. If a class accumulates many methods doing different things, split it.
- **Composition > inheritance.** Use inheritance only for unambiguous "is-a" relationships. For most reuse, build small classes that delegate to each other. (Note: PEP 8 has a thoughtful "Designing for Inheritance" section if you do go down that path — including `__` name-mangling for subclass-API protection. Both perspectives are legitimate.)
- **Dataclasses (`@dataclass`)** for value-object-style classes that mostly hold data. Saves boilerplate `__init__`, `__repr__`, `__eq__`. Combine with `frozen=True` for immutability.
  ```python
  from dataclasses import dataclass

  @dataclass(frozen=True)
  class CameraIdentity:
      ip: str
      manufacturer: str
      mac_vendor: str
      page_title: str
  ```
- **Properties** to add behavior to attributes without breaking the API:
  ```python
  @dataclass
  class Camera:
      stream_url: str

      @property
      def is_authenticated(self) -> bool:
          return '@' in self.stream_url and ':' in self.stream_url
  ```
- **Special methods (`__str__`, `__repr__`, `__len__`, `__iter__`)** where they aid usability.
- **Be cautious with very small one-method classes** — a regular function in a module is often simpler than `class Foo: def do_thing(self): ...`.
### 1.16 Refactoring and Optimization

**Refactoring:**
- **Small steps.** Rename one thing, extract one helper, simplify one loop. Don't refactor and add features in the same commit.
- **Tests guide refactors.** AnyCam's `verify_release.py` semantic contracts already serve this role at the function-symbol level. Expanding to behavioral tests (snap_loop runs N restarts, find_rtsp_path returns expected URL for known camera fixture) would let larger refactors happen with confidence.
- **Watch for code smells:** functions over ~50 lines, deeply nested conditionals (>3 levels), repeated code fragments (extract a helper), classes that "know too much" (split responsibilities), magic numbers (extract named constants).
**Optimization:**
- **Don't optimize prematurely.** Readability and correctness first.
- **Profile before optimizing.** `cProfile`, `timeit`, py-spy, or similar identify hot paths. Don't guess. Most slow code isn't slow where you think it is.
- **Algorithmic improvements > clever micro-tweaks.** Switching from O(n²) to O(n log n) wins; replacing `+=` with `''.join()` matters for big string builds; rewriting a list comprehension as a manual loop "for speed" almost never matters for AnyCam-scale workloads (tens of cameras, hundreds of paths).
### 1.17 Standard Library First

Reach for the stdlib before installing a third-party package or rolling your own. Python's stdlib is huge and battle-tested.

Common AnyCam-relevant stdlib modules:
- **`pathlib.Path`** for filesystem paths (over `os.path` string manipulation)
- **`collections.Counter`, `defaultdict`, `OrderedDict`, `deque`** — common data structures done right
- **`itertools.chain`, `groupby`, `islice`** — efficient iteration patterns
- **`functools.lru_cache`, `partial`, `reduce`** — function composition and memoization
- **`contextlib.contextmanager`, `ExitStack`, `suppress`** — see 1.11
- **`secrets.compare_digest`, `secrets.token_urlsafe`** — security-critical comparisons and token generation (see 6.1)
- **`urllib.parse.urlparse`, `quote`, `urljoin`** — URL handling
- **`json`** — serialization (safer than pickle for untrusted input)
- **`hashlib`** — cryptographic digests (already used by AnyCam's qop-aware Digest auth)
- **`socket`, `ssl`** — low-level networking
- **`asyncio`** — event loop, tasks, queues, locks (already core to AnyCam)
- **`subprocess.run`** with argument lists for shelling out (never `shell=True` with untrusted input)
If a stdlib module solves your problem, prefer it. Each third-party dep is a maintenance liability — pinned version, security advisories, possible Python-version incompatibilities, abandonment risk.

### 1.18 Testing

AnyCam's `verify_release.py` already provides syntax-and-contract checks at release time. This section is about the next layer up: behavioral tests that catch logic bugs.

- **Small, focused tests.** Each test exercises one behavior. A test that "verifies the whole scan flow" is useful but should be one of many — most tests should be unit-level.
- **pytest fixtures** for shared setup (fake camera dicts, mock RTSP server, mock ffmpeg). Avoids duplicate setup code across tests.
- **pytest parametrize** for the same logic against multiple inputs. AnyCam's CAMERA_DB brand-id matching is a natural fit:
  ```python
  @pytest.mark.parametrize("server,title,expected", [
      ("Hipcam RealServer/V1.0", "Microseven", "Hipcam/Microseven"),
      ("nginx", "Hikvision", "Hikvision"),
      ("Boa/0.94", "", "Generic"),
  ])
  def test_brand_identification(server, title, expected):
      result = _identify_camera_brand({"server_header": server, "page_title": title})
      assert result["name"] == expected
  ```
- **Run tests frequently.** Pre-commit hook + CI = caught before merge.
- **Reasonable coverage, not 100%.** Focus on the paths that matter: credential handling, brand-id, throttle pacing, snap_loop restart logic, the qop-aware Digest formula. Skip trivial getters and one-line helpers.
- **Fast and deterministic.** No real network, no real files (use `tmp_path` fixture), no random. Mock external deps.
- **Mock external dependencies.** AnyCam's external deps are mostly network — ONVIF SOAP, RTSP, HTTP, ffmpeg. Each can be mocked. `unittest.mock.patch`, `aioresponses` for aiohttp, dedicated RTSP test servers for integration.
- **Complement unit tests with a small number of integration tests** against a real test camera (or `mediamtx` / `rtsp-simple-server` in Docker) for end-to-end confidence.
## PART 2 — JAVASCRIPT

### 2.1 The Three Core Error Types

**SyntaxError** — Caught before execution; file won't run at all.
- Missing closing `)`, `}`, `]`
- Using `;` instead of `,` in object literals: `{prop: 'a'; prop2: 'b'}` → use `,`
- Unterminated string literal
- Using a reserved keyword as a variable name
**ReferenceError** — Variable/function not found.
- Typo in name: `userName` vs `username` — JS is case-sensitive
- Using `let`/`const` variable before declaration (temporal dead zone)
- Variable declared inside a function, accessed outside (scope)
**TypeError** — Right variable, wrong type.
- `null.property` or `undefined.property` — most common JS crash in the wild
- Calling something that isn't a function: `let x = 5; x()`
- Using array method on non-array: `obj.map(...)` → `obj` must be an array
- `typeof` vs `instanceof` — use the right check
**Sources:** digitalocean.com, raygun.com, w3schools.com/js/js_mistakes, saad-minhas.com, fullstackfoundations.com, dev.to/__khojiakbar__, pixelfreestudio.com, linkedin.com/Dinesh-Rawat

---

### 2.2 Async/Await Pitfalls in JavaScript (Directly Relevant to AnyCam's Frontend)

| Mistake | Symptom | Fix |
|---|---|---|
| Not wrapping `await` in `try/catch` | Silent failure, undefined result, unhandled rejection | `try { await fetch(...) } catch(e) { ... }` |
| `await` inside `.forEach()` | forEach ignores returned Promises; each await is silently abandoned | Use `for...of` loop or `Promise.all()` |
| `await` inside a `for` loop sequentially | Each iteration waits; wastes time when operations are independent | Collect promises, then `await Promise.all(promises)` |
| `async` function with no `await` | Function is pointlessly async; ESLint's `require-await` flags this | Remove `async` or add actual awaited operation |
| `new Promise(async (resolve, reject) => {})` | If async function throws, error is lost and promise never rejects | Never pass async function to Promise constructor |
| Breaking a `return` statement across lines | JS auto-inserts semicolon; function returns `undefined` silently | Keep `return` and its value on the same line |
| `return await promise` (unnecessary) | Adds an extra microtask tick for no benefit | Return the promise directly: `return promise` |

**The `return` line-break silent bug (w3schools documented):**
```javascript
// WRONG — JavaScript inserts semicolon after return
function getData() {
  return
    { value: 42 }; // This is dead code — function returns undefined!
}

// RIGHT
function getData() {
  return { value: 42 };
}
```

**Sources:** maximorlov.com, dev.to/somedood, freecodecamp.org, eslint.org (no-await-in-loop, require-await), blog.poespas.me, tutorialreference.com, moldstud.com

---

### 2.3 Common Typos and Silent Bugs

- **`==` vs `===`:** `==` does type coercion (`"1" == 1` is `true`). Always use `===` unless you specifically need coercion
- **`null` vs `undefined`:** Both are falsy but distinct. `typeof null === 'object'` (historical bug in JS). Use `=== null` or `=== undefined` explicitly, or optional chaining: `obj?.prop`
- **Array vs Object confusion:** Arrays have `.map()`, `.filter()`, `.forEach()`. Objects don't. Check with `Array.isArray(x)` before calling array methods
- **`var` hoisting:** `var` declarations are hoisted to function scope. Use `const`/`let` exclusively in modern code
- **Named index on array:** `arr["name"] = "x"` silently converts the array to an object; `arr.length` then returns 0
- **`//` is not a CSS comment in JS:** CSS uses `/* */`. If CSS appears in a JS string/template literal, `//` comments break it
**Sources:** w3schools.com, raygun.com, ccodelearner.com, dev.to errors article

---

### 2.4 DOM and Event Handling

- Always pass the event object explicitly: `button.onclick = function(event) {...}`, not `function() { console.log(event) }`
- Use `addEventListener` over `onclick =` for multiple listeners
- Use optional chaining before DOM access: `document.getElementById('foo')?.value`
- Clean up event listeners when components are removed (memory leaks in SPAs)
---

## PART 3 — HTML

### 3.1 The Silent Permissiveness Problem

HTML browsers parse permissively — they auto-correct mistakes and render something even from invalid markup. **This is dangerous because it hides errors.** The page looks right in Chrome but breaks in Firefox or Safari because each browser's error-recovery differs.

**Always validate** with the W3C Nu HTML Checker after major changes.

---

### 3.2 The Ten Most Common HTML Errors

| # | Error | Impact | Fix |
|---|---|---|---|
| 1 | Missing `<!DOCTYPE html>` | Browser enters "quirks mode" — layout and CSS behave unpredictably | Add `<!DOCTYPE html>` as very first line |
| 2 | Unclosed tags | Downstream elements get absorbed into the unclosed element; layout breaks | Type opening and closing tags before adding content |
| 3 | Wrong tag nesting order | `<b><i>text</b></i>` → should be `<b><i>text</i></b>` | Close tags in reverse order of opening |
| 4 | Missing `alt` attribute on `<img>` | Accessibility failure; screen readers say nothing; SEO penalty | Always `<img src="..." alt="description">` |
| 5 | Misspelled tag name | `<ttile>` → browser ignores it silently | Spell tags correctly; linter catches this |
| 6 | Missing closing `/` in closing tag | `<p>Text<p>` creates nested `<p>` instead of closing | `<p>Text</p>` |
| 7 | Attributes without quotes | `<div id=main>` is technically valid but breaks on spaces and special chars | Always quote attributes: `id="main"` |
| 8 | Using deprecated tags | `<font>`, `<center>`, `<marquee>`, `<blink>` — not HTML5 | Use CSS equivalents |
| 9 | Multiple `<h1>` tags | Breaks document outline; SEO and screen reader impact | One `<h1>` per page; use `<h2>`–`<h6>` for hierarchy |
| 10 | `<div>` for everything | `<div class="header">` instead of `<header>`; hurts SEO and accessibility | Use semantic tags: `<header>`, `<nav>`, `<main>`, `<article>`, `<section>`, `<footer>` |

**Sources:** djamware.com, purecode.ai, patchmycode.com, dev.to/ofodile, medium.com/@techwisenow, learntube.ai, nestify.io, theserverside.com, iu.pressbooks.pub, medium.com/@wewillcode

---

### 3.3 HTML Embedded in Python (The AnyCam Pattern)

AnyCam generates HTML as Python triple-quoted strings. Specific risks:

- **Python f-string braces in CSS:** `{` and `}` in CSS inside an f-string must be doubled: `{{` and `}}`. Missing this causes `KeyError` or malformed CSS
- **Quote collision:** HTML attributes use `"`, Python f-strings use `"` — use `\"` or switch to `'` for HTML attributes inside f-strings
- **Newline in JS strings inside Python strings:** A bare newline inside a JS string literal inside a Python triple-quoted string is a JS syntax error. Use `String.fromCharCode(10)` or `\n` in a JS template literal
- **XSS from camera metadata:** Camera names come from ONVIF and could contain `<script>` or `"` characters. Escape with `html.escape()` before inserting into HTML
**Sources:** ssojet.com, mojoauth.com, bomberbot.com, dev.to/fosres (XSS framework), peps.python.org/pep-0501

---

## PART 4 — CSS

### 4.1 The Cascade and Specificity — Root of Most CSS Bugs

**The cascade:** When two rules target the same element, the one with higher *specificity* wins, not the one that appears later (unless specificity is equal).

**Specificity ranking** (highest to lowest):
1. `!important` (nuclear option — avoid)
2. Inline styles (`style="..."`)
3. ID selectors (`#myId`)
4. Class selectors (`.myClass`), attribute selectors, pseudo-classes
5. Element selectors (`div`, `p`), pseudo-elements
**Common bug:** You add a class rule but an older ID rule overrides it. You add `!important` to fix it. Now you've started a specificity war that only escalates.

**Fix:** Use class selectors almost exclusively. Reserve IDs for JS hooks, not styling. Never use `!important` except to override third-party library styles, and comment why.

**Sources:** MDN (developer.mozilla.org/specificity, handling_conflicts), dev.to/kingsley_uwandu, painlesscss.com, dev.to/umarsiddique010

---

### 4.2 The Most Common CSS Typos That Silently Do Nothing

CSS is uniquely dangerous for typos: **an invalid property is silently ignored** and the cascade falls through to the next matching rule. You see the wrong style but get no error.

| Typo | What You Meant | What Happens |
|---|---|---|
| `display: flexbox` | `display: flex` | Silently ignored; element uses default display |
| `linear-gradient(...)` without `background:` | CSS gradient background | Property has no selector; silently ignored |
| `font-size: 16pc` | `font-size: 16px` | `pc` = picas (print unit); renders at wrong size |
| `// This is a comment` | `/* This is a comment */` | `//` is NOT valid CSS; treated as an invalid property |
| `position: relative; top: left` | `top: 0; left: 0` | `top: left` is invalid; silently ignored |
| `color: drak-blue` | `color: dark-blue` or `color: darkblue` | Misspelled; silently ignored; falls back up the cascade |
| `border-radius` on `outline` in Safari | Rounded outline | Safari doesn't support this combination; no error, just no rounding |

**Sources:** css-tricks.com (My Dumbest CSS Mistakes), dev.to/umarsiddique010, painlesscss.com

---

### 4.3 CSS Units — Which to Use When

| Unit | What It's Relative To | Use For | Avoid For |
|---|---|---|---|
| `px` | Fixed (absolute) | Borders, box-shadows, images, breakpoints | Font sizes (ignores user browser preferences) |
| `rem` | Root `<html>` font size (default 16px) | Font sizes, spacing, padding/margin | — |
| `em` | Parent element font size (compounds!) | Component-level padding relative to its own text | Global spacing (compounds unexpectedly) |
| `%` | Parent element dimension | Fluid widths, fluid containers | Heights (parent must have explicit height) |
| `vh` / `vw` | Viewport height / width | Full-screen hero sections | Mobile (100vh includes browser chrome; jumps on scroll) |
| `dvh` | Dynamic viewport height | Full-screen mobile sections (replaces `vh` on mobile) | Older browsers (needs fallback) |
| `ch` | Width of "0" character | Readable prose line length (`max-width: 65ch`) | Pixel-precise layouts |

**The mobile 100vh bug:** On iOS Safari, `100vh` includes the address bar. When the user scrolls and the bar hides, the page jumps. Fix: use `100dvh` with `100vh` as fallback.

**Common mixing mistake:** Using `px` for padding but `rem` for font size causes inconsistent scaling when users change their browser font size preference.

**Sources:** blog.saeloun.com, fastutil.app, freecodecamp.org, stoman.me, pixelconverter.com, w3schools.com/css_units, frontendtools.tech, toolbox-kit.com, supercharged.design, css-units guide

---

### 4.4 Flexbox and Grid Pitfalls

**Flexbox:**
- `flex: 1` = `flex-grow: 1, flex-shrink: 1, flex-basis: 0%` — often what you want for equal columns
- **Implicit `min-width: auto`:** Flex items won't shrink below their content size by default, causing overflow with long text. Fix: `min-width: 0` on the flex item
- **`justify-content` vs `align-items`:** `justify` = main axis (row direction by default); `align` = cross axis. Mixing these up is the #1 flexbox frustration
- `gap` is now supported everywhere and is better than margin hacks for spacing
**Grid:**
- `visual order ≠ tab order` — `order` property and `grid-column`/`grid-row` placement reorder things visually but not for keyboard/screen reader navigation
- `will-change: grid-template-columns` — only add when actually animating; uses extra memory
**Z-index:**
- z-index only works on positioned elements (`position: relative/absolute/fixed/sticky`)
- Properties that create new stacking contexts: `transform`, `opacity < 1`, `position: fixed` — these reset z-index context unexpectedly
- Debugging tip: open browser DevTools → Layers panel to see stacking contexts
**Sources:** dev.to/thebitforge, medium.com/@ss-tech (z-index), dev.to/umarsiddique010, css-tricks.com

---

## PART 5 — COMBINED / POLYGLOT CODE (Python + JS + HTML + CSS)

### 5.1 The Escaping Context Problem

Each language has different escaping rules, and they must be applied in the right context. In AnyCam, Python generates an HTML page that contains embedded CSS and JavaScript. This creates four nested escaping contexts:

```
Python f-string → HTML string → CSS block → values
Python f-string → HTML string → <script> block → JS strings → DOM values
```

**Rules:**
1. **Python → HTML:** Python `{` in f-strings that are meant to be CSS/JS must be doubled: `{{` `}}`
2. **HTML → JS:** JS strings inside HTML must escape `"` as `\"` or use different quote style
3. **JS → DOM:** Use `textContent` not `innerHTML` when inserting dynamic values; `innerHTML` executes script tags
4. **Python → JS string literals:** A bare newline in a Python triple-quoted string that contains a JS string is a JS `SyntaxError`. Use `String.fromCharCode(10)` or `\\n`
5. **CSS comments:** `/* comment */` — not `//`. Using `//` inside a CSS block in a Python string introduces a subtle validity error
**The `//` CSS comment bug (appeared in AnyCam's history):**
```python
# WRONG — // is not valid CSS
css = """
.element {
    // This is not a CSS comment!
    color: red;
}
"""

# RIGHT
css = """
.element {
    /* This is a valid CSS comment */
    color: red;
}
"""
```

**Sources:** ssojet.com, mojoauth.com, bomberbot.com, ssojet.com/compare-escaping, mdn/template-literals, peps.python.org/pep-0501, dev.to/fosres

---

### 5.2 Function Completeness Across All Languages

In all four languages, a syntactically valid construct can be logically incomplete:

| Language | What's Valid But Incomplete |
|---|---|
| Python | `async def f(): x = 1` — valid syntax, missing cleanup/return |
| JavaScript | `async function f() {}` — valid, missing `await`, returns undefined |
| HTML | `<div id="foo">` with no closing tag — browser auto-closes, maybe wrong |
| CSS | `selector { color: red` — missing `}` — entire subsequent stylesheet may break |

**AnyCam release checklist (proposed addition):**

```
1. ast.parse() — syntax check                              [existing]
2. Semantic contract checks — key functions contain         [NEW — add this]
   expected calls (save_cameras, SCAN_STATE, etc.)
3. Version matches in config.yaml and CURRENT_VERSION       [existing]
4. Version present in zip config.yaml                       [existing]
5. Changelog top entry matches version                      [existing]
6. JS/CSS blocks: no '//' comments in CSS sections          [NEW — add this]
7. All triple-quoted HTML/JS/CSS: no bare newlines inside   [NEW — add this]
   JS string literals
```

---

### 5.3 Tools Recommended for Each Language

| Language | Linter / Formatter | Type Checker | In-editor |
|---|---|---|---|
| Python | Ruff (fast, all-in-one), Flake8, Black | mypy, Pyright | Pylance (VS Code) |
| JavaScript | ESLint (+ `no-await-in-loop`, `require-await`) | TypeScript / JSDoc | ESLint VS Code extension |
| HTML | HTMLHint, Nu HTML Checker (W3C) | — | HTMLHint VS Code extension |
| CSS | Stylelint | — | Stylelint VS Code extension |
| All | SonarQube (CI), Semgrep (custom rules) | — | SonarLint |

---

### 5.4 Quick Reference — The Errors Most Likely to Sneak Past Review

| Risk | Language | Why It's Sneaky |
|---|---|---|
| Truncated function body | Python | `ast.parse()` passes; function silently does nothing |
| Missing `await` | Python / JS | No error; returns a coroutine/Promise object instead of value |
| CSS typo (`flexbox`, `//`) | CSS | Silently ignored; falls back to cascade |
| `time.sleep()` in async | Python | Blocks entire event loop; no error, just everything freezes |
| `return` on its own line | JavaScript | Auto-semicolon insertion; returns `undefined` silently |
| `!important` overuse | CSS | Starts specificity war; overrides become impossible |
| Missing `<!DOCTYPE html>` | HTML | Browser quirks mode; layout differs across browsers |
| Unclosed HTML tag | HTML | Browser auto-closes wrong; layout corruption |
| `100vh` on mobile | CSS | Address bar causes jump; `100dvh` is the modern fix |
| `await` in `.forEach()` | JavaScript | Promises silently abandoned; looks like it works |
| Wrong CSS comment syntax | CSS (in Python) | `//` instead of `/* */` — invalid CSS, silently ignored |
| `{{` brace escape in f-strings | Python→HTML | Missing doubling causes `KeyError` or broken CSS |

---

---

## PART 6 — PROJECT ENGINEERING PRACTICE

This part covers project-level concerns that sit outside any one language: security, dependency management, and version-control discipline.

### 6.1 Security

AnyCam handles user credentials, processes untrusted network input from cameras and ONVIF servers, and runs `ffmpeg` subprocesses against URLs derived from those inputs. Security is a real concern, not a theoretical one.

**Validate untrusted input at the boundary.** Camera names from ONVIF, RTSP `Server:` headers, ONVIF SOAP responses, page `<title>` tags — all untrusted. They could contain anything (control characters, HTML/JS, very long strings, malformed encodings). Sanitize before logging, persisting, or rendering.

- **Allowlists over denylists** when possible. AnyCam currently filters dangerous chars in camera names; an explicit allowlist (alphanumeric + space + a small punctuation set) is safer than trying to ban every dangerous char.
- **Length limits everywhere.** Cap string fields at reasonable maxima (camera name 200 chars, RTSP URL 500 chars, log line 2000 chars). Unbounded input is a DoS surface.
- **Escape before rendering in HTML.** AnyCam's UI is generated from Python triple-quoted f-strings. Camera metadata flows into the HTML — `html.escape()` it first. PART 3.3 of this document already covers this.
**Subprocess safety.**
- **Never `shell=True` with untrusted input.** AnyCam already uses `subprocess` with argument lists for ffmpeg — keep doing that. Never construct ffmpeg command strings via string concatenation that includes camera-derived URLs or metadata.
- **`subprocess.run([...], shell=False)`** is the safe pattern. Only use `shell=True` with fully-static command strings you control.
**Credential handling.**
- **Use `secrets.compare_digest()`** for comparing tokens, hashes, or any security-relevant byte string. `==` is timing-attack vulnerable.
- **Generate tokens with `secrets.token_urlsafe()` or `secrets.token_bytes()`**, never `random.*` (which is not cryptographically secure).
- **Encrypt at rest, never plaintext.** AnyCam uses `cryptography.Fernet` for credential persistence — keep it.
- **Never log credentials.** AnyCam's `_strip_creds` helper exists for URL strings. Extend its use to any new place that handles URLs, including log messages. The recent rc2.x credential-leak fix in `_safe_cam` is the kind of audit that should happen periodically.
- **The Fernet key itself stays out of the repo.** It's stored separately and gitignored. Confirmed.
**Deserialization.**
- **Avoid pickling untrusted data.** `pickle.loads()` can execute arbitrary code from a malicious payload. AnyCam's persistence is JSON, which is safe. Keep it that way.
- **JSON for any persisted state from the network.**
**Supply chain.**
- **Pin dependency versions.** Reproducible builds + protection against malicious upstream updates.
- **Watch for security advisories** on `cryptography`, `aiohttp`, etc. Run `pip-audit` periodically against requirements.txt.
- **Minimal dependency surface.** Each unused dep removed is one less supply-chain risk. AnyCam's deps are already minimal — preserve that discipline.
### 6.2 Dependency Management

AnyCam's dependencies are intentionally minimal: `aiohttp`, `cryptography`, plus the stdlib. Recommendations for keeping them that way:

- **Each new third-party dep is a deliberate decision.** Each one adds: install time, attack surface, future maintenance burden, version-conflict risk, abandonment risk. The bar for adding a dependency should be "stdlib genuinely cannot solve this."
- **Evaluate before installing.** Check the PyPI page, repo activity, last release date, supported Python versions, open-issue count, maintainer count. Avoid abandoned packages.
- **Pin versions** in your dependency manifest. AnyCam's Dockerfile/requirements should pin specific versions for reproducible builds. Floating ranges (`aiohttp>=3.0`) lead to drift.
- **Document why a dep exists.** A short comment near the import or in the dependency manifest explaining why a package is used helps future maintainers (including future-you) decide whether it can be removed.
- **Remove unused deps regularly.** If you stop using something, uninstall it. `pip-autoremove`, manual review at release time.
### 6.3 Version Control Practices

- **Small, self-contained commits.** Each commit does one thing — one bugfix, one feature, one rename. AnyCam's release rhythm with its rc2.1 → rc2.1.1 → rc2.2 → rc2.3 → ... cadence already reflects this discipline; extending it to in-progress work (between releases) would help further.
- **Clear commit messages.** Subject line summarizes WHAT; body explains WHY when not obvious. AnyCam's CHANGELOG entries are essentially commit-message material — keeping that level of explanation in actual commit messages would let `git log` tell the same story.
- **Branch for non-trivial work.** Even solo, a feature branch lets you keep `main` stable while exploring a refactor.
- **Tag releases with annotated tags.** `git tag -a 2.2.9 -m "..."`. AnyCam's version-bump-then-zip workflow is effectively informal tagging; formalizing it adds release markers visible in `git log` and Github releases.
- **Run automated checks on every change.** AnyCam's `verify_release.py` is the pre-release gate. Wiring it as a git pre-commit hook (or CI step on push) would prevent broken commits from ever landing.
- **`.gitignore` generated artifacts, caches, and local config.** Already in place — `__pycache__/`, `.venv/`, `*.pyc`, the Fernet key file.
- **Never commit secrets.** AnyCam's encrypted-cred-store + Fernet-key approach is correct. The Fernet key itself stays out of the repo — that's the linchpin.
- **Sync frequently.** If the project lives on multiple machines (laptop, Pi, dev VM), keep them in sync. Small frequent syncs avoid divergent histories that need painful merges.
- **Document the workflow.** Even for a solo project, writing down "how I release AnyCam" (the script of: `verify_release.py` → bump versions → CHANGELOG entry → versioned zip → audit PDF → present_files) protects against forgotten steps after a long break.
## Sources Summary

**200+ sources consulted, including:**

- Real Python (realpython.com) — Python syntax errors, asyncio, f-strings; and the full **Python Best Practices** reference set (realpython.com/ref/best-practices/) covering classes, code formatting, code testing, comments, comprehensions, concurrency, conditionals, constants, dependency management, distribution, docstrings, documentation, exception handling, functions, generator expressions, imports, logging, loops, object mutability, optimization, project layout, public-API surface, Pythonic code, refactoring, resource management, security, standard library, third-party libraries, type checking, variables, version control, and virtual environments — 33 individual sub-pages consulted
- Python official docs (docs.python.org) — asyncio-dev, asyncio-task, ast module
- MDN Web Docs (developer.mozilla.org) — CSS specificity, cascade, template literals, JS errors
- W3Schools — Python errors, JS mistakes, CSS units, JS conventions
- ScrapingAnt, BetterStack, Stackify, Oxylabs, Crawlbase/Medium — Python common errors
- DigitalOcean, Raygun, PixelFreeStudio, Saad-Minhas — JS error types and debugging
- ESLint official (eslint.org) — no-await-in-loop, require-await rules
- Djamware, PurePurpose.ai, PatchMyCode, Dev.to (multiple authors) — HTML mistakes
- CSS-Tricks, Painless CSS, Dev.to (umarsiddique, thebitforge) — CSS mistakes and pitfalls
- FastUtil, PixelConverter, FrontendTools, Saeloun, Webflow/Supercharged — CSS units
- SSOJet, MojoAuth, Bomberbot, Dev.to/fosres — Python↔HTML escaping and XSS
- shanechang.com, DataCamp, Paul Norvig, Medium/@aarmanj08 — async best practices
- Wikipedia, oligo.security, testrigor.com, debugg.ai, codeant.ai — static analysis tools
- GitHub (analysis-tools-dev, lukehutch) — static analysis tool catalogs
- ACM SIGSOFT 2024, emergentmind.com — academic static analysis research
- Plus 150+ additional blog posts, forum answers, and documentation pages
*Compiled April 2026, expanded May 2026 with engineering best-practice additions (sections 1.6–1.18 and PART 6) sourced from Real Python's Python Best Practices reference. Original PART 1.1–1.5 and PARTS 2–5 preserved as-is.*
