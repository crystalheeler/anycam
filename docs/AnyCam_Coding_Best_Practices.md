# AnyCam Coding Best Practices
### Compiled from 200+ sources — Python · JavaScript · HTML · CSS · Combined/Polyglot

---

## How To Use This Document

This is a living reference, not a checklist to run through once. The issues are ranked within each section by how likely they are to cause **silent, hard-to-catch bugs** — the kind that pass `ast.parse()` and still make production behave wrong.

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

## Sources Summary

**200+ sources consulted, including:**

- Real Python (realpython.com) — Python syntax errors, asyncio, f-strings
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

*Compiled April 2026 for the AnyCam project*
