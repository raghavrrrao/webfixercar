# Website Fixer

A timed **CSS debugging** game. There is one challenge — **CSS Challenge ·
NovaCloud** — which hands the player a finished
cloud-platform landing page ("NovaCloud") whose stylesheet shipped broken, and
gives them **30 minutes** to clear **14 CSS objectives**.

Nobody builds NovaCloud, and nobody edits its markup. `index.html` is
**read-only** — shown so the player can read the structure and work out which
selectors apply, never editable, and never accepted from the browser. The only
editable file is `style.css`.

Django 5 + Django Channels (ASGI). No frontend framework.

---

## Run it locally

```bash
cd website-fixer
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Open <http://127.0.0.1:8000/>. `daphne` is first in `INSTALLED_APPS`, so
`runserver` serves ASGI and the WebSocket works without any extra process.

To serve exactly the way production does:

```bash
python manage.py collectstatic --no-input
daphne -b 127.0.0.1 -p 8000 games.asgi:application
```

---

## How it fits together

| Area | Where |
| --- | --- |
| Challenge files (the site itself) | `first/challenge/novacloud/` |
| Round metadata + duration + fix table | `first/game_config.py` |
| Objective checking | `first/checks.py` |
| Views / API | `first/views.py`, `games/urls.py` |
| Presence store | `first/presence.py` |
| WebSocket consumer | `first/consumers.py`, `first/routing.py`, `games/asgi.py` |
| Game shell UI | `static/css/wf-*.css`, `static/js/wf-*.js`, `template/*.html` |

Flow: `/` (home) → `/signup/` or `/login/` → `/start/` (launch screen) →
`/home/` (briefing + broken site) → **START RACE** → the race → `/race-result/`
→ the fixed NovaCloud site, or a timeout that ends the attempt.

### The challenge is three files

```
first/challenge/novacloud/
  index.html      the finished page -- READ ONLY, never edited or submitted
  style.css       the broken stylesheet the player repairs
  solution.css    the organisers' gold standard
```

`index.html` is a **complete HTML document**, not a fragment. The preview
renders it as written and swaps its `<link rel="stylesheet">` for the player's
live CSS (`previewDocument()` in `static/js/wf-arena.js`).

`solution.css` is never served to a browser — `ArenaPageTests` asserts that the
answers do not appear in the arena HTML.

### Read-only markup

Three independent layers keep the markup fixed:

* the textarea carries `readonly` and `aria-readonly`, and is never bound to
  the editor's input/keydown handlers (`static/js/wf-arena.js`);
* `submission()` posts `{css}` only;
* `_read_submitted_css()` in `first/views.py` **ignores any `html` field**, and
  every check runs against `CHALLENGE_HTML` from disk. A forged `html`
  parameter can change neither the page nor the score — `ReadOnlyMarkupTests`
  proves it.

### 37 defects, 14 objectives

`style.css` ships with **37 deliberate defects**; only **14 are graded**. The
rest are visual noise: squashed pricing cards, a four-column footer, cropped
avatars. A player may fix them or ignore them, and the arena says so plainly.

`GRADED_FIXES` in `first/game_config.py` is the authoritative list of what is
scored — objective id to the `(broken, fixed)` edits that clear it. Grouped
edits count as one objective: the stats band needs its column count *and* its
alignment, the steps row its columns *and* its padding, the pricing row its gap
*and* the scale of the featured card. A half-finished group does not score
(`test_grouped_objectives_need_all_their_parts`). `apply_fixes()` raises if an
anchor is missing or ambiguous, so a drifted challenge file fails the suite
loudly rather than grading the wrong thing.

The whole round is **17 declarations across 14 objectives** — 17 changed lines
in a 1278-line stylesheet, about 960 characters of typing.

One defect from the organisers' `style.css` is deliberately **not** shipped:
`.hero__glow { position: static }` puts a 900×900 decorative div into normal
flow, opening a 900px void that pushes the hero headline from 715px to 1615px.
Four of the fourteen objectives live in that hero, so the defect hides the
round rather than decorating it.

To swap in a different site: replace the four files, update the two fix tables,
and point the checks in `first/checks.py` at the new selectors. Nothing in the
views, templates or JS knows what the challenge is.

### The objectives

14 objectives, **all CSS**. They are graded on *outcome*, not on text —
whitespace, property order, comments and equivalent answers (`flex` vs
`inline-flex`, `56px` vs `clamp(...)`, `repeat(3, 1fr)` vs `auto-fit`, a
literal `16px` vs the `--radius-md` token) all pass. Each objective carries a
description plus **three hints** that the player unlocks one at a time, each
narrowing the search:

| Level | Answers | Never contains |
| --- | --- | --- |
| 1 · the idea | what kind of CSS this is | a selector or a property |
| 2 · where to look | the rule(s) involved | — |
| 3 · which property | the property and which way it is wrong | the finished declaration |

`HintQualityTests` enforces that shape: hint 1 must not name a selector, hint 2
must point at a rule, hint 3 must name a property, and no hint may contain any
literal declaration from `GRADED_FIXES`. Backtick spans render as `<code>`
through the `code_spans` filter, which escapes before it marks safe.

Two objectives carry an interaction note. Measured at 1120px, while the 860px
breakpoint is still misfiring it overrides `.hero__container`'s column count
and hides `.navbar__menu` entirely, so fixing `css-hero-split` or
`css-nav-spacing` changes nothing on screen. Their hints say so without
revealing how to fix the breakpoint — a test asserts both the note and the
absence of `max-width` in them.

Two objectives interact, on purpose. The reversed breakpoint
(`@media (min-width: 860px)`) applies the entire phone stylesheet to desktop,
which overrides the hero's column count — so fixing `css-hero-split` shows no
visual change until `css-responsive` is fixed too. Both are graded
independently and the hint for the first one says so.

### JavaScript

Neither the page nor the round needs any. The supplied markup referenced a
`script.js` driving the theme toggle, the FAQ accordion, the mobile menu and a
stat counter; the preview sandbox blocks scripts, so the page was made to stand
on its own:

* the `<script>` tag is removed;
* the four headline statistics carry their real values in the markup rather
  than only in `data-count-to` attributes;
* `.faq-item__answer` opens to `max-height: 400px` in **both** stylesheets, so
  the answers are readable without an accordion — it is not a defect and not an
  objective;
* `.theme-toggle__icon--moon { display: none }` is correct in both stylesheets,
  so the decorative toggle shows one icon rather than two.

The stylesheet's `.reveal` rules are dead code: the markup never uses that class.

### Final preview

`[ View final preview ]` in the preview panel head opens the **finished**
NovaCloud page — `index.html` styled by `solution.css` — so the player can see
what they are aiming at and infer the CSS from the visual difference.

It is a reference, never a grading action. `final_preview()` in
`first/views.py` reads nothing from the player and writes nothing back: no
grading, no progress, no autosave, and deliberately no `start_challenge()`
call, so opening it never starts or extends a clock. The document is composed
server-side and loaded **by URL** into its own empty-`sandbox` iframe, so the
arena's own source never carries the answers and the gold-standard CSS is
scoped to that document.

That view is the only one exempted from the site-wide `X-Frame-Options: DENY`
(to `SAMEORIGIN`); without it the overlay renders an empty box. A test asserts
both headers.

Note that a participant with devtools can still inspect the rendered reference
page and read its computed styles. Anything rendered in the player's own
browser is readable there — the feature hides the answers from the game UI, not
from a determined inspector.

### Why the preview is scaled

`fitPreview()` in `static/js/wf-arena.js` renders the iframe at a fixed 1120px
virtual width and scales it down to the panel. Sizing the iframe to the panel
instead would put NovaCloud permanently inside its own 860px breakpoint and
hide the desktop-only mistakes. The **Phone** toggle switches to a 390px
virtual width.

### The 12 minute race timer

`GAME_DURATION_SECONDS = 12 * 60` lives in `first/game_config.py`.

`User.race_started_at` is stamped **once**, by the server, and only by
`POST /api/race/start/`. `User.start_challenge()` refuses to write it a second
time, so opening the briefing, reloading it, opening a second tab, logging out
and back in, or replaying the request all return the *running* attempt rather
than a fresh twelve minutes. Every remaining-time value is computed on the
server from that timestamp:

* the briefing ships the current state in `#wf-race-state`;
* the browser only *renders* a countdown from it, advances it with
  `performance.now()` (a monotonic clock, so changing the machine's time does
  nothing) and re-syncs with `GET /api/race/state/` every 5 seconds;
* `POST /api/race/progress/` and `POST /api/race/complete/` both re-read
  `user.race_status` and refuse the write once the attempt is over.

Refreshing, editing the countdown in devtools, or moving the system clock
changes nothing — the clock is a database timestamp.

### CSS isolation

The player's page is rendered **only** inside `<iframe id="wf-preview" sandbox>`
via `srcdoc` (`static/js/wf-arena.js`). An empty `sandbox` attribute means no
scripts, no forms, no same-origin access and no top-level navigation, so:

* player CSS applies to that document alone and can never reach the game shell;
* game-shell CSS (all `wf-` prefixed) never reaches the player's page, because
  it is a different document;
* player JavaScript does not run at all — the challenge is HTML + CSS;
* the preview cannot read cookies, the session, or the parent DOM.

Server side, player HTML/CSS is never rendered with `|safe`; it only appears
inside auto-escaped `<textarea>` elements and is parsed (never executed) by
`first/checks.py`.

### Live player count

`/ws/presence/` (`first/consumers.py`) is a Channels consumer joined to one
broadcast group. Presence is keyed by **Django session**, so one browser with
five tabs counts as one player. Each client pings every 20 seconds; entries
older than 55 seconds are swept, which covers dropped sockets, instance
restarts and closed laptops. The count is broadcast on every connect and
disconnect, and the browser reconnects with exponential backoff.

The client picks its scheme from the page (`https:` → `wss://`, otherwise
`ws://`) and uses `location.host`, so nothing about the environment is
hardcoded.

### Do I need Redis?

**No, not for a single instance** — which is what Render's free/starter web
service is. The default `InMemoryChannelLayer` plus the in-process presence
store are exact when one process serves everybody.

Set `REDIS_URL` **only if you scale to more than one instance**. Doing so
switches the channel layer to `channels_redis` *and* the presence store to a
Redis sorted set, so the count is shared. Nothing else changes.

---

## Deploying to Render

* **Build Command:** `./build.sh`
  (installs requirements, `collectstatic`, `migrate`)
* **Start Command:** `daphne -b 0.0.0.0 -p $PORT games.asgi:application`
* **Environment variables:**

  | Key | Value |
  | --- | --- |
  | `PYTHON_VERSION` | `3.11.9` |
  | `DJANGO_SECRET_KEY` | generate a value |
  | `DJANGO_DEBUG` | `false` |
  | `DATABASE_URL` | Postgres connection string (optional; SQLite otherwise) |
  | `REDIS_URL` | only when running 2+ instances |
  | `DJANGO_SSL_REDIRECT` | defaults to `true` when `DJANGO_DEBUG=false`; set `false` only to run a production-shaped server locally over plain HTTP |

`render.yaml` in this folder describes the same thing as a blueprint.

Gunicorn was removed from `requirements.txt`: it is WSGI-only and would kill
the WebSocket. Daphne serves both HTTP and WS.

---

## Running an event

Players register themselves at `/signup/` with their PC number. Accounts
created before this version already carry a `game_start_time`, so their clock
looks expired — clear that field in `/admin/` (or set `completed_at` back to
empty) to hand a player a fresh 30 minutes.

`python manage.py test` runs the regression suite (36 tests). It asserts that
the shipped page fails all 14 objectives, that both the gold standard *and* a
graded-fixes-only submission pass all 14, that each individual fix clears
exactly one objective and no others, that equivalent beginner answers are
accepted, that the FAQ icon's legitimate `rotate(45deg)` is never mistaken for
the console's, that the answers never reach the browser, that the clock cannot
be restarted or beaten, and that the presence socket counts one browser session
as one player however many tabs it opens.

## Testing the player count by hand

1. Start the server and open <http://127.0.0.1:8000/> — the badge reads
   `1 player online`.
2. Open a second **private/incognito** window on the same URL — both windows
   move to `2 players online` without a refresh.
3. Open a second tab in the *same* window — the count stays at 2 (same session).
4. Close the private window — the first window drops back to `1`.
5. Refresh either window — the count settles back to the same number; it does
   not climb.

Two normal windows of the same browser share a session cookie and therefore
count as one player. That is intentional; use a private window to simulate a
second person.

## Testing CSS isolation by hand

Paste this into the `style.css` tab in the arena:

```css
* { margin: 0 !important; padding: 0 !important; }
body { background: red !important; }
button { background: lime !important; font-size: 50px !important; }
header { display: none !important; }
```

The preview turns red and loses its header. The game bar, timer, buttons and
editor must not change at all.


## Website Fixer — current game flow

The competition now uses the existing Django project with a lightweight CSS-themed racing challenge:

1. Existing intro/auth flow remains in place.
2. The player sees the intentionally broken NovaCloud page.
3. Pressing **START RACE** starts a server-authoritative 12-minute timer.
4. The player drives through six CSS-themed obstacles.
5. Reaching the finish records time, obstacles, collisions and a score.
6. The result page shows congratulations and the official fixed NovaCloud website.
7. One overall winner is selected by the organizers from the recorded results.

The old CSS editor files and grading code are retained for compatibility with existing project data, but the active player flow is the racing challenge.

### What the server decides, and what it cannot

The browser is never believed. The server owns:

* **the clock** — `race_started_at` is written once, by `POST /api/race/start/`;
* **the state machine** — `NOT_STARTED → ACTIVE → COMPLETED | EXPIRED`, with
  both end states terminal (`User.race_status`);
* **obstacle progress** — `POST /api/race/progress/` accepts an obstacle only
  when it is the *next* one in course order and the course has had time to
  carry it to the car (`RACE_OBSTACLE_TIMES`, plus a few seconds of grace), so
  six clears cannot be produced faster than the course can be driven;
* **the finish line** — `POST /api/race/complete/` requires an active race,
  every obstacle already recorded, and at least `RACE_COURSE_SECONDS` elapsed;
* **the score** — `views.race_score()`, computed from the server's own
  recorded elapsed time, obstacle count and collision count. Nothing posted by
  the browser (`score`, `obstacles`, `collisions`, `elapsed`) is ever read.

Known limits, stated plainly. This is a browser game, so it is not
cheat-proof, and the goal is only that trivial devtools or curl manipulation
cannot produce a winning result:

* **Collisions are self-reported.** The server counts what the browser tells
  it and never lets the number go down, but a patched client can simply not
  report a hit. Collisions only ever *subtract* points, so the worst case is a
  participant claiming a cleaner run than they drove.
* **Obstacle clears are time-gated, not skill-verified.** The server proves
  the participant was present and that the course had time to reach each
  obstacle; it does not simulate the car, so it cannot prove they steered
  around it rather than through it.
* **The earliest possible finish is fixed.** Because the course scrolls at a
  fixed pace, every completed run takes about `RACE_COURSE_SECONDS`; the time
  component of the score separates a prompt finish from a stalled one, not one
  driver's line from another's.

Organisers compare the recorded runs in the Django admin and pick the one
overall winner. Nothing in the application claims a winner or ranks anybody.
 
