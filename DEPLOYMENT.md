# Deploying Website Fixer to Render

Everything needed to put this on the internet so people can play it from
ordinary browsers. Running the event once it is deployed is
[EVENT-OPERATIONS.md](EVENT-OPERATIONS.md); this file is only about getting
it there.

The architecture is three resources, and all three are required:

```
        ┌──────────────────────────────┐
        │  Web service (Daphne, ASGI)  │   HTTP + WebSocket, one service
        │  website-fixer               │
        └───────┬──────────────┬───────┘
                │              │
     DATABASE_URL              REDIS_URL
                │              │
   ┌────────────▼───┐   ┌──────▼──────────────┐
   │ Postgres       │   │ Key Value           │
   │ the event      │   │ the channel layer   │
   └────────────────┘   └─────────────────────┘
```

**Postgres is not optional.** The database *is* the event: every race, clock,
score, repair and reward lives in it and nothing lives in the web process.
Render's filesystem is ephemeral, so a SQLite file there is deleted on every
deploy and on every restart after an idle spin-down — silently, taking the
whole event with it. `settings.py` refuses to start that way rather than let
it happen.

**Key Value is what makes the scoreboard live across processes.** It carries
notifications only; every scoreboard is rebuilt from Postgres on connect and
on reconnect, so losing it degrades liveness and never correctness.

---

## 1. What you need first

* The repository pushed to GitHub/GitLab.
* A Render account.
* Nothing else. No custom domain — the `*.onrender.com` URL is enough.

## 2. Deploy

1. In Render, choose **New → Blueprint** and point it at this repository.
2. Render reads [`render.yaml`](render.yaml) and proposes three resources:
   `website-fixer` (web), `website-fixer-db` (Postgres), `website-fixer-kv`
   (Key Value). Read §7 on free instance types before approving.
3. Apply. Render generates `SECRET_KEY` and `WF_ADMIN_PASSWORD`, and wires
   `DATABASE_URL` and `REDIS_URL` across for you. **No secret is written into
   `render.yaml`, and none should ever be.**
4. Watch the build. It runs [`build.sh`](build.sh), which is deliberately
   loud: dependencies → `check --deploy` → `makemigrations --check` →
   `collectstatic` → `migrate` → `ensure_admin`. `set -o errexit` and
   `set -o pipefail` mean a failing migration fails the deploy instead of
   shipping an application whose schema does not match its code.
5. When the health check at `/healthz/` goes green, it is live.

## 3. Get the organiser password

Render generated it; you have never seen it. Read it once from the service's
**Environment** tab:

```
WF_ADMIN_PASSWORD
```

Sign in at `https://<your-service>.onrender.com/admin/` with:

```
Username: admin123
Password: <the generated value>
```

`admin123` is the login *name*, not a password — this model has no email
field, and `USERNAME_FIELD` is the participant name.

**Change it immediately** in `/admin/` if you would rather choose your own,
or set `WF_ADMIN_USERNAME` / `WF_ADMIN_PASSWORD` yourself before first deploy.

`ensure_admin` never prints the password, so it does not appear in build logs.
If `WF_ADMIN_PASSWORD` is missing it **refuses to create the account at all**
rather than inventing a guessable one — a failed deploy is recoverable, a
public organiser account is not.

## 4. Environment variables

Set by `render.yaml`, nothing to type:

| Variable | Source | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | `generateValue` | Signs session cookies. The app refuses to boot in production without it. |
| `DJANGO_DEBUG` | `"false"` | Declares production. Every guard keys off this — nothing is inferred from the platform. |
| `DATABASE_URL` | `fromDatabase` | Postgres. The app refuses to boot on SQLite in production. |
| `REDIS_URL` | `fromService` | Key Value. The app refuses to boot without it in production. |
| `WF_ADMIN_PASSWORD` | `generateValue` | Organiser account. `ensure_admin` refuses without it. |
| `PYTHON_VERSION` | `"3.11.9"` | Build runtime. |

Supplied by Render automatically:

| Variable | Purpose |
| --- | --- |
| `PORT` | The port Daphne binds. Used by `startCommand`. |
| `RENDER_EXTERNAL_HOSTNAME` | Becomes `ALLOWED_HOSTS` and the HTTPS `CSRF_TRUSTED_ORIGINS` entry — which is why neither is hardcoded, since the hostname does not exist until the service does. |

Optional, only if you need them:

| Variable | When |
| --- | --- |
| `DJANGO_ALLOWED_HOSTS` | Custom domain. Comma-separated. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Custom domain. Comma-separated, `https://` included. |
| `WF_ADMIN_USERNAME` | A different organiser login name. |
| `DJANGO_SSL_REDIRECT` | Set `false` only to run a production-shaped server over plain HTTP. |
| `WF_SINGLE_MACHINE` | Declares a one-box event on SQLite — see §8. Never set this on Render. |

**Never put a secret in `render.yaml`.** Use `generateValue`, `fromDatabase`,
`fromService`, or set it in the dashboard.

## 5. Static files

WhiteNoise serves them from the web service; there is no CDN or bucket to
configure. `build.sh` runs `collectstatic --no-input`, which gathers the game
CSS/JS and the Django admin assets into `staticfiles/` (139 files at the time
of writing). `staticfiles/` is generated at build time and is gitignored.

## 6. WebSockets

The scoreboard is a WebSocket at `/ws/scoreboard/`, served by the same service
as HTTP through `games/asgi.py`. This is why the start command is Daphne and
**must not** become Gunicorn or any other WSGI server.

The browser derives the scheme from the page it is on — `https:` gives
`wss://`, `http:` gives `ws://` — and always uses `window.location.host`. No
localhost URL is hardcoded, so the same build works on a laptop, on a LAN and
behind Render's TLS.

## 7. Free instance types — read before choosing

Verified against Render's documentation, and each of these has a real
consequence for an event:

* **Free web service** spins down after 15 minutes without inbound traffic and
  takes about a minute to spin back up. The first participant of the day, or
  the first after a quiet spell, waits through that. It does spin back up on a
  new WebSocket connection as well as an HTTP request. For a live event where
  people are standing at the machine, consider a paid instance for the day.
* **Free Postgres** is 1 GB and **expires 30 days after creation**, with a
  14-day grace period to upgrade before Render deletes it and all its data.
  Fine for a fest inside that window; not somewhere to leave results you want
  to keep. Export the CSV and take a backup regardless
  (EVENT-OPERATIONS.md §7).
* **Free Key Value** does not persist to disk — a restart loses its contents.
  That is harmless here: the channel layer holds only in-flight notifications,
  and every scoreboard rebuilds from Postgres on reconnect. One free instance
  per workspace.

To upgrade, change `plan:` in `render.yaml` (or use the dashboard) — the web
service and Key Value accept `starter`, `standard`, `pro`…, and Postgres
accepts `basic-256mb`, `basic-1gb`, and larger.

## 8. Running the event on one machine instead

Deploying is not the only option; EVENT-OPERATIONS.md describes running the
whole event from a single lab machine on SQLite, which is a perfectly good
choice for a room with no reliable internet. That path is still supported, but
it now has to say so:

```
DJANGO_DEBUG=false
WF_SINGLE_MACHINE=true
DJANGO_ALLOWED_HOSTS=192.168.1.20,fixer.local
DJANGO_SSL_REDIRECT=false
SECRET_KEY=<your own>
```

`WF_SINGLE_MACHINE=true` is what permits SQLite and the in-memory channel
layer with `DEBUG` off. It exists precisely so that *not* setting it makes a
hosted deployment fail loudly rather than quietly run on a disk that is about
to be erased. **Never set it on Render.**

## 9. After deploying — check these in order

Replace `<url>` with your `https://….onrender.com` address.

1. **Health** — `<url>/healthz/` returns `{"status": "ok", "database": true}`.
2. **Home page** — `<url>/` renders, styled. If it is unstyled, static files
   did not collect; check the build log.
3. **Console** — open DevTools on `<url>/`, confirm no 404s and no errors.
4. **Register** — `<url>/signup/` as a test participant on `PC-TEST`.
5. **The clock has not started** — open `/home/`, confirm the briefing shows
   START RACE and the timer is not running.
6. **Race** — press START RACE, drive, collect a repair, watch the NovaCloud
   preview change.
7. **Scoreboard** — in a second browser, sign in as the organiser and open
   `<url>/scoreboard/`. The connection pill must read **LIVE** (that is the
   WebSocket; if it says CONNECTING the socket is not getting through). Your
   test participant's row must update **without refreshing**.
8. **Projector** — `<url>/scoreboard/display/`.
9. **Finish** — complete the race and confirm the fixed website opens.
10. **Export** — `<url>/scoreboard/results.csv` downloads and contains the run.
11. **Restart** — restart the service from the Render dashboard, then reload
    the scoreboard. Every participant, score and completed state must still be
    there. This is the check that proves the database, not the process, is
    holding the event.
12. **Delete the test participant** — `/admin/`, or
    `python manage.py reset_race <name> --yes` from a shell.

## 10. Testing with friends

The deployed URL works from any browser on the internet; nothing extra is
needed. Two people should:

* register as **different participant names** — the participant is the
  identity, and one account is one attempt;
* be able to enter the **same PC number** if they want, and still get separate
  races and separate results;
* both appear on the organiser scoreboard at once, updating live.

Expect the free-tier spin-up delay (§7) on the first request after a quiet
period.

## 11. Production data

Production starts empty. The local `db.sqlite3` is development data and is
never copied anywhere: migrations build the schema, and nothing imports rows.
Do not load it into Postgres.

> **Housekeeping:** `db.sqlite3` is currently tracked in git and contains ~53
> development participants with password hashes. It cannot reach production —
> production uses `DATABASE_URL` and the app refuses to start on SQLite — but
> it does not belong in the repository. To drop it from version control while
> keeping your local copy:
>
> ```bash
> git rm --cached db.sqlite3
> git commit -m "Stop tracking the local development database"
> ```
>
> `.gitignore` already excludes it going forward.

## 12. Custom domain

Not required. If you add one later, point it at the service in Render's
dashboard, then set both:

```
DJANGO_ALLOWED_HOSTS=fixer.yourcollege.edu
DJANGO_CSRF_TRUSTED_ORIGINS=https://fixer.yourcollege.edu
```

Without the second, form posts over HTTPS are rejected.

## 13. Troubleshooting

| Symptom | Cause |
| --- | --- |
| Build fails: `SECRET_KEY must be set…` | `SECRET_KEY` missing from the service environment. |
| Build fails: `DATABASE_URL must point at PostgreSQL…` | The database is not attached, or the blueprint was edited. This guard is doing its job. |
| Build fails: `REDIS_URL must be set…` | The Key Value instance is not attached. |
| Build fails: `WF_ADMIN_PASSWORD is not set` | It is missing; add it (or let `generateValue` supply it) and redeploy. |
| Deploy never goes live | `/healthz/` is failing — usually the database. Check the service logs. |
| Page loads unstyled | `collectstatic` did not run or failed; check the build log. |
| Scoreboard stuck on CONNECTING | The WebSocket is not connecting. Confirm the start command is Daphne and that the page is on `https` (so the client uses `wss`). |
| Scoreboard connects but never updates | `REDIS_URL` is not set, and more than one instance is running. |
| `DisallowedHost` | Custom domain not in `DJANGO_ALLOWED_HOSTS`. |
| CSRF failure on a form | Custom domain not in `DJANGO_CSRF_TRUSTED_ORIGINS`. |
