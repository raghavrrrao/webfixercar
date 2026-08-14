# Running the event

Everything an organiser needs on the day, and everything that has to be
configured before it. The game itself is documented in `README.md`, and
putting it on the internet is [DEPLOYMENT.md](DEPLOYMENT.md); this file is
about operating it.

The one rule the whole system is built on:

> **The database is the state.** Nothing about a race lives in the web
> process. Restarting the server, closing the scoreboard, losing Redis or
> reconnecting a browser changes no participant's clock, score or reward.

---

## 1. Before the day: configuration

Everything is environment-driven. Nothing below is hardcoded, and no
credential is committed.

| Variable | When | What it is |
| --- | --- | --- |
| `DJANGO_DEBUG` | always | `false` for the event. Anything else is a development run. |
| `SECRET_KEY` | **required when `DJANGO_DEBUG=false`** | Signs session cookies. The server *refuses to boot* without it, because the development key is published in this repository and would let anybody forge an organiser session. (`DJANGO_SECRET_KEY` is still accepted.) |
| `WF_SINGLE_MACHINE` | **required for a one-box event** | Declares that this machine is running the whole event by itself, which is what permits SQLite and the in-memory channel layer with `DEBUG` off. Without it a production run insists on `DATABASE_URL` and `REDIS_URL` — see [DEPLOYMENT.md](DEPLOYMENT.md). |
| `DJANGO_ALLOWED_HOSTS` | **required when `DJANGO_DEBUG=false`** | Comma-separated hostnames this server answers to, e.g. `192.168.1.20,fixer.local`. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | HTTPS only | Comma-separated origins allowed to POST, e.g. `https://fixer.college.edu`. Not consulted over plain HTTP. |
| `DJANGO_SSL_REDIRECT` | HTTPS only | Defaults to on when `DEBUG` is off. Set `false` to run a production-shaped server over plain HTTP on a LAN. |
| `WF_ADMIN_PASSWORD` | **set it** | The organiser's `/admin/` password. Without it `ensure_admin` falls back to a password that is in this repository and prints a loud warning. |
| `WF_ADMIN_USERNAME` | optional | Defaults to `admin123`. |
| `DATABASE_URL` | see §2 | Postgres URL. Defaults to local SQLite. |
| `REDIS_URL` | see §3 | Only needed with more than one worker process. |

Generate a secret key with:

```
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

### Verifying the configuration

```
python manage.py check --deploy
```

Two warnings are expected and are **not** blockers:

* `security.W004` (HSTS) — deliberately off. Browsers cache HSTS for its full
  duration, which is painful to undo while a fest domain is still changing.
* `security.W008` (SSL redirect) — only appears if you deliberately set
  `DJANGO_SSL_REDIRECT=false` for a plain-HTTP LAN event.

Anything else is a real finding.

---

## 2. Database

**SQLite is the default and is fine for a lab event on one machine.** It is a
single file, `db.sqlite3`, and it is the entire event. A one-box event has to
declare itself with `WF_SINGLE_MACHINE=true`, which is what permits SQLite
with `DEBUG` off.

**A hosted deployment must use Postgres, and the application now enforces
that.** On a platform with an ephemeral filesystem the disk is wiped on every
deploy and on every restart after idle spin-down, which would destroy the file
and with it every participant, score and reward. A production run without
`DATABASE_URL` and without `WF_SINGLE_MACHINE` refuses to start rather than
let that happen quietly. See [DEPLOYMENT.md](DEPLOYMENT.md).

### Backup

Backup is **manual**. Nothing in this project takes one for you.

SQLite — while the server is running, and safe to run at any time:

```
python -c "import sqlite3,sys; src=sqlite3.connect('db.sqlite3'); dst=sqlite3.connect(sys.argv[1]); src.backup(dst); dst.close(); print('backed up to', sys.argv[1])" backup-2026-08-14-1830.sqlite3
```

Or simply copy `db.sqlite3` while the server is stopped.

Postgres:

```
pg_dump "$DATABASE_URL" > backup-2026-08-14-1830.sql
```

Take one **before the event starts** and one **after the last race finishes**,
plus the CSV export (§6). Keep them off the machine running the event.

---

## 3. Channel layer

The scoreboard's live updates travel over a Channels layer. It carries
notifications only — every scoreboard is rebuilt from the database on connect
and on reconnect — so losing it degrades liveness, never correctness.

**Development and a declared single-machine event: `InMemoryChannelLayer`.**
This is the default and needs no configuration. It is correct for `runserver`
and for one `daphne` process, and a production run may use it only with
`WF_SINGLE_MACHINE=true`.

**More than one worker or instance: set `REDIS_URL`.** The in-memory layer is
a dictionary inside one process, so with two workers an event handled by
worker A never reaches a scoreboard socket held by worker B. A hosted
production run without `REDIS_URL` now refuses to start.

This was measured, not assumed. Two `daphne` workers on one database, no
`REDIS_URL`:

```
scoreboard socket on worker B: 101 Switching Protocols
race started on worker A:      200 active
live race_update events reaching worker B: 0
after reconnecting to worker B, the snapshot holds: [... 'Worker A Player' ...]
```

Zero live events crossed; the reconnect snapshot was still completely correct,
because it came from the database. That is the exact failure mode `REDIS_URL`
removes.

With Redis:

```
REDIS_URL=redis://localhost:6379/0
```

Nothing else changes. `channels-redis` is already in `requirements.txt`. No
Redis credential is committed anywhere.

The same two-worker measurement, repeated with an external channel layer
configured, and both workers running the production settings:

```
worker A (:8501) healthy, worker B (:8502) healthy   -- one Postgres
scoreboard socket on worker B: 101 Switching Protocols
participant registers and races on worker A
live race_update events reaching worker B: 3
  race_started, race_progress, collision — all naming the participant
  who raced on worker A, carrying that participant's live state
```

Three events crossed where the in-memory layer delivered zero.

> **What that test used:** the channel-layer server was `fakeredis`'s
> Redis-protocol TCP server, not Redis itself — no Redis build was available
> for this machine. It is a real TCP listener speaking the real protocol, and
> the two Daphne workers were genuinely separate OS processes using the real
> `channels_redis` client, so it establishes that the architecture and the
> configuration path work across processes. It is *not* evidence about Redis's
> own implementation or about a hosted Key Value service. Re-run step 11 of
> DEPLOYMENT.md §9 after deploying to confirm it there.

---

## 4. HTTPS and WSS

The scoreboard and presence clients derive the socket scheme from the page:
`https:` gives `wss://`, `http:` gives `ws://`, and the host is always
`window.location.host`. No localhost URL is hardcoded anywhere, so the same
build works on a laptop, on a LAN and behind TLS.

Behind a TLS-terminating proxy, `SECURE_PROXY_SSL_HEADER` is already set for
`X-Forwarded-Proto`. Make sure the proxy forwards `Upgrade` and `Connection`
headers or WebSockets will not connect.

---

## 5. Start of event

1. **Back up** the current database (§2), even if it is empty.
2. Start the server:
   ```
   daphne -b 0.0.0.0 -p 8000 games.asgi:application
   ```
   (`python manage.py runserver` also serves WebSockets — `daphne` is first in
   `INSTALLED_APPS` — but use `daphne` for the real thing.)
3. Confirm the database: `python manage.py export_results` prints the current
   rows. An empty event prints just the header.
4. Confirm the channel layer: single worker means in-memory, and nothing to do.
5. Open the **organiser monitor** at `/scoreboard/` and sign in as the
   organiser account.
6. Open the **projector view** at `/scoreboard/display/` on the hall screen.
7. Confirm the connection indicator on the monitor reads `LIVE`. If it says
   `CONNECTING…` the WebSocket is not getting through — check the proxy.
8. **Run one test participant** end to end: register, START RACE, drive,
   finish, and confirm the fixed website opens.
9. Clear that test run: `python manage.py reset_race <that name> --yes`, or
   delete the account from `/admin/`.
10. Begin.

---

## 6. Between participants

1. The participant presses **EXIT** (logout). This is the only handover step.
2. The next participant registers with **their own name** and **the PC number
   on the machine**.
3. The same PC number is expected to repeat all day. It identifies the desk,
   never the person.
4. The new participant gets a fresh, unstarted twelve minutes.
5. Every previous run on that PC stays exactly as it was, and stays on the
   scoreboard.

If a participant forgets to log out, the next one can still sign in from the
login page; signing in replaces the session. Signed-in pages are served
`no-store`, so a Back press cannot redraw the previous participant's race or
result from the browser cache.

---

## 7. End of event

1. Stop new registrations (stop directing people to `/signup/`).
2. Let the races still running finish or run out.
3. Check the monitor: every row should read COMPLETED or TIME'S UP.
4. **Export the results**, from the browser or the shell:
   ```
   python manage.py export_results --settle --out results-2026-08-14.csv
   ```
   `--settle` records an entry for anyone who walked away mid-race. The
   organiser monitor has an **Export CSV** button that does the same thing.
5. **Back up the database** (§2).
6. Compare the COMPLETED runs and choose **one overall winner**.
7. Only after 4 and 5 are safely off the machine, reset if another event
   follows (§8).

### What the export contains

One row per **run**, in registration order. Three people who used PC-14 are
three rows.

`participant`, `pc_no`, `status`, `registered_at`, `started_at`,
`completed_at`, `elapsed_seconds`, `elapsed`, `repairs_collected`,
`repairs_total`, `repairs`, `penalties`, `score`, `score_max`,
`distance_metres`, `course_metres`, `section`, `reward_unlocked`.

It contains **no password, no password hash, no session key and no token**.

It names **no winner and no placings**. Only COMPLETED runs carry a score;
a timeout scores 0, which is what its own recorded entry says.

---

## 8. Resetting for another event

There is deliberately **no web button that deletes everything**. Resetting is a
shell operation, which means it needs access to the machine the event runs on.

**Export and back up first (§7). A reset is not undoable.**

### One participant

For a genuine judgement call — a PC died mid-race and the organiser decides to
give that person a rerun:

```
python manage.py reset_race Rahul --yes
```

The argument is the **participant**, because that is what an attempt belongs
to. A PC number is accepted only when exactly one person used it; passing a
shared one lists everybody on it and refuses rather than guessing:

```
$ python manage.py reset_race PC-14 --yes
CommandError: 'PC-14' is a PC number used by 3 participants, and an attempt
belongs to a person rather than a machine.
    Rahul  (completed, registered 2026-08-14 18:04)
    Priya  (active, registered 2026-08-14 18:19)
    Arjun  (expired, registered 2026-08-14 18:33)
Re-run with the participant name.
```

Without `--yes` it prints exactly what it is about to destroy and then refuses.

### A test participant

```
python manage.py reset_race Tester --new --pc PC-TEST
```

Creates an unstarted account (default password `pw-123456`, override with
`--password`).

### A whole new event

Two safe options, in order of preference:

1. **Start a new database.** Move `db.sqlite3` aside — it *is* the archive —
   then `python manage.py migrate` and `python manage.py ensure_admin`. The old
   event stays intact in the file you moved.
2. **Delete participants deliberately** from `/admin/`, using the *"Delete
   participant and ALL their competition data"* action. Django's confirmation
   page runs first. Deleting a participant cascades to their submission,
   stylesheet and hint history and touches nobody else.

### Telling test data apart

Test accounts are ordinary participants — the model has no test flag. Use a
naming convention the export makes obvious (`PC-TEST`, `Tester …`) and clear
them before the event opens. Historical records with migration-suffixed names
like `Alice (PC-A-1786569771)` are legacy data from development: leave them,
they break nothing, and delete them deliberately from `/admin/` if you want a
clean board.

---

## 9. Failure scenarios, and what actually happens

| # | Situation | Behaviour |
| --- | --- | --- |
| A | Player closes the browser | The race keeps running on server time. It is not paused and not reset. They log back in and resume with the time that has actually passed. |
| B | Player refreshes | Resumes. No new twelve minutes; repairs, distance and score are as they were. |
| C | Player loses Wi-Fi | Same as A. Progress reports stop, so the last accepted distance stands; the clock does not. |
| D | Scoreboard loses Wi-Fi | The board reconnects with backoff and rebuilds from a fresh database snapshot. No race is affected. |
| E | Server restarts | Everything survives — participants, timestamps, repairs, scores, completed and expired states, reward locks. Verified directly: state before and after a restart was identical. |
| F | Redis restarts | Live updates pause. Scoreboards reconnect and rebuild from the database. No race is affected. Irrelevant on a single worker, which uses no Redis. |
| G | PC handed to a new participant | See §6. New identity, fresh attempt, previous runs untouched. |
| H | Participant types the wrong PC number | Cosmetic. The PC number is metadata; it does not affect their race, their score or their reward. Fix it in `/admin/` if it matters for the record. |
| I | Participant attempts a second run | Refused, `409`. Logging out and in, another browser, another device, another PC number and calling the endpoint directly were all tested and all refused. |
| J | Two people try to use one account | They share one race — it is one identity and one attempt. Give the second person their own account. |
| K | Participant opens another participant's result | Refused. The result, the reward and the organiser views are all server-side authorised; there is no URL that hands one participant another's run. |
| L | Organiser refreshes the scoreboard | Fresh snapshot from the database. Changes nothing. |
| M | Projector browser refreshes | Same. It is read-only. |
| N | One player times out | Becomes EXPIRED, remaining 0, score settled at 0, reward stays locked, cannot restart, cannot complete. The board is told once. |
| O | Two players finish at almost the same time | Independent records. Neither can affect the other. |
| P | Two completion requests arrive at once | One completion, one result, one reward, one submission row. The loser gets `409` and the recorded score does not move. |

---

## 10. Quick reference

```
# run the event
daphne -b 0.0.0.0 -p 8000 games.asgi:application

# who is where
python manage.py export_results

# the judging file
python manage.py export_results --settle --out results.csv

# give one named person another attempt (deliberate)
python manage.py reset_race <participant> --yes

# a fresh test account
python manage.py reset_race <name> --new --pc PC-TEST

# the organiser account
python manage.py ensure_admin

# health
python manage.py check
python manage.py check --deploy
```

Organiser URLs (all organiser-only):

```
/scoreboard/                  live monitor
/scoreboard/display/          projector view
/scoreboard/<id>/             one participant's run
/scoreboard/<id>/site/        the website that run left behind
/scoreboard/results.csv       the judging export
/admin/                       Django admin
```
