# Deploying the Aggregator to Coolify

Step-by-step guide for getting the aggregator running on Coolify
(self-hosted, behind Traefik). Assumes you already have a Coolify server
reachable from the public internet and — when you're ready to go
public — a domain pointed at it.

## What we're deploying

| Service        | Type                        | Purpose                                       |
| -------------- | --------------------------- | --------------------------------------------- |
| `agg-postgres` | Coolify resource (Postgres) | persistent DB + task queue (Procrastinate)    |
| `agg-web`      | Application (Dockerfile)    | gunicorn + uvicorn workers (the FastAPI app)  |
| `agg-worker`   | Application (Dockerfile)    | Procrastinate worker (background jobs + cron) |

`agg-web` and `agg-worker` are built from the **same Dockerfile in this
repo** — they just override `CMD`. Coolify lets you do this by creating
two Applications that point at the same Git source with different start
commands.

No Redis. The task queue, periodic scheduler, and per-cron debounce lock
all live in Postgres (Procrastinate uses LISTEN/NOTIFY for push delivery
and row-level locks for cooperation).

---

## 1. Create the Postgres resource

In Coolify → **Resources → New Resource → Postgres** (version 18).

Note two things from the resource page once it's up:

- The **internal** connection URL (something like
  `postgres://USER:PASS@HOST:5432/DBNAME`). Use the internal hostname,
  not the public one — both apps reach the DB over Coolify's internal
  network.
- That same URL gets pasted twice into the apps below with two different
  driver prefixes:
  - `postgresql+asyncpg://…` → `AGG_DATABASE_URL` (used by the app)
  - `postgresql+psycopg2://…` → `AGG_DATABASE_URL_SYNC` (used by Alembic)

Procrastinate creates its task-queue tables inside this same Postgres on
first `alembic upgrade head` — no separate broker / Redis to provision.

---

## 2. Create the `agg-web` Application

In Coolify → **Applications → New Application**:

- **Source**: this repo (Git URL), branch `coolify`.
- **Build pack**: `Dockerfile` (auto-detects the `Dockerfile` in repo root).
- **Port**: `8001` (matches `EXPOSE` in the Dockerfile).
- **Health check path**: `/healthz` (the Dockerfile already has a
  `HEALTHCHECK` directive; Coolify also probes this URL).
- **Custom CMD**: **leave empty.** The Dockerfile's `CMD ["web"]` is what
  triggers `docker-entrypoint.sh web` — Alembic-then-gunicorn.

### Environment variables for `agg-web`

Paste the block below into the **Environment variables** tab. Generate
fresh secrets first:

```sh
openssl rand -hex 32   # AGG_JWT_SECRET
openssl rand -hex 32   # AGG_SESSION_SECRET
openssl rand -hex 32   # AGG_WEBHOOK_SECRET   (will pair with MDProject later)
openssl rand -hex 32   # AGG_PARADISE_SECRET  (will pair with MDProject later)
```

```
# --- core ---
AGG_ENV=prod
AGG_DEBUG=false
AGG_LOG_LEVEL=INFO
AGG_TEST_MODE=false                                  # MUST be false in prod (gates /ops/data-reset)

# --- database (use the INTERNAL Postgres host from Coolify) ---
AGG_DATABASE_URL=postgresql+asyncpg://USER:PASS@PG_HOST:5432/DBNAME
AGG_DATABASE_URL_SYNC=postgresql+psycopg2://USER:PASS@PG_HOST:5432/DBNAME

# --- odds provider (sole upstream) ---
ODDSAPI_API_KEY=<your odds-api.io key>
AGG_ODDSAPI_THROTTLE_PCT=80
AGG_ODDSAPI_MAX_RETRIES=3

# --- auth ---
AGG_JWT_SECRET=<openssl rand -hex 32>
AGG_SESSION_SECRET=<openssl rand -hex 32>
AGG_JWT_ACCESS_TTL_SECONDS=900
AGG_JWT_REFRESH_TTL_SECONDS=1209600

# --- outbound webhook to MDProject (DEFERRED until MDProject also on Coolify) ---
AGG_WEBHOOK_TARGET_URL=                              # leave empty; dispatcher logs+skips
AGG_WEBHOOK_SECRET=<openssl rand -hex 32>            # generate now, share with MDProject when wiring
AGG_PARADISE_SECRET=<openssl rand -hex 32>           # same — generate now, share later

# --- CORS / host allowlist (DEFERRED for first private deploy) ---
AGG_CORS_ORIGINS=                                    # set once MDProject domain is known; no wildcards
AGG_ALLOWED_HOSTS=                                   # SET BEFORE PUBLIC EXPOSURE (see §6)
AGG_DOCS_ENABLED=false

# --- ingest window ---
AGG_INGEST_WINDOW_DAYS_AHEAD=7
AGG_INGEST_WINDOW_DAYS_BEHIND=2
AGG_INGEST_INCLUDE_ALT_LINES=false
AGG_INGEST_ODD_IDS=
AGG_INGEST_BOOKMAKER_ID=
AGG_INGEST_SKIP_NEW_TERMINAL=true

# --- vacuum ---
AGG_VACUUM_DAYS=3
AGG_VACUUM_BATCH_SIZE=1000

# --- lifecycle watchdog ---
AGG_LIFECYCLE_STALE_GRACE_HOURS=12
AGG_LIFECYCLE_AUTO_VOID_HOURS=0
AGG_LIFECYCLE_DISAPPEARED_GRACE_HOURS=6
AGG_LIFECYCLE_DISAPPEARED_VOID_HOURS=12

# --- analytics gate (pair with MDProject's matching flag) ---
ANALYTICS_FREE_FOR_ALL=1

# --- gunicorn knobs ---
AGG_WEB_WORKERS=4
AGG_WEB_TIMEOUT=30

# --- profiler (opt-in; leave off unless actively profiling) ---
AGG_PROFILER_ENABLED=false
AGG_PROFILER_TOKEN=
```

> **Security caveat.** With `AGG_ALLOWED_HOSTS` empty, Starlette's
> TrustedHostMiddleware is skipped — any `Host` header is accepted.
> Fine while the URL is private; **must** be set before sharing the
> domain. See §6 pre-flight.

Hit **Deploy.** The container will:

1. Run `alembic upgrade head` (idempotent — safe on every boot).
2. Start gunicorn on `0.0.0.0:8001`.
3. Coolify's Traefik routes whichever domain you assigned to `agg-web:8001`.

---

## 3. Create the `agg-worker` Application

In Coolify → **Applications → New Application** (point at the **same**
Git source / branch, **same** Dockerfile):

- **Port**: leave empty (no ingress; worker doesn't serve HTTP).
- **Custom CMD**: `worker`
- **Health check**: disable (no HTTP listener to probe).
- **Public domain**: none.

### Environment variables for `agg-worker`

Paste **the same env-var block as `agg-web`** with two exceptions:

- The web-only knobs (`AGG_WEB_WORKERS`, `AGG_WEB_TIMEOUT`,
  `AGG_DOCS_ENABLED`, CORS / allowed-hosts) are no-ops here — pasting
  them is harmless but you can omit.
- Use the same `ANALYTICS_FREE_FOR_ALL=1`, the same secrets, the same
  Postgres URLs.

Hit **Deploy.** Worker logs should show:

```
[entrypoint] starting procrastinate worker...
```

followed by Procrastinate registering the periodic schedule
(`@app.periodic(cron=...)` decorators on each task).

---

## 4. Post-deploy: create the admin user

The DB starts empty. SSH into the running `agg-web` container (Coolify →
app → **Terminal**) and run the bundled script — it's idempotent:

```sh
# Auto-generated password (printed once to stdout — copy it immediately):
python scripts/make_admin.py admin@yourdomain.com

# …or supply your own (must be ≥ 16 chars):
python scripts/make_admin.py admin@yourdomain.com 'a-strong-password-16-or-more'
```

If the user already exists, the script promotes them to `role=admin` and
reactivates them; passing no password leaves the existing password alone.

Then sign in at `https://<agg-domain>/admin` and rotate via the user
edit page if you used the auto-generated default.

---

## 5. Bootstrap the events catalog

Trigger the seed + first ingest from
`https://<agg-domain>/ops/crons` (SQLAdmin auth required). Click
**full_refresh → Run**. This walks odds-api.io, populates
`core_event_event` + `core_market` + `core_selection`, and sets up the
league refresh cadence.

---

## 6. Pre-flight checklist

### First boot (private)

- [ ] `https://<agg-domain>/healthz` returns `{"ok": true, "version": "..."}`.
- [ ] `https://<agg-domain>/robots.txt` returns `User-agent: *\nDisallow: /`.
- [ ] Every response carries `X-Robots-Tag: noindex, nofollow, noarchive`.
- [ ] `https://<agg-domain>/admin` requires login.
- [ ] `https://<agg-domain>/ops/data-reset` returns **403**
      (`AGG_TEST_MODE=false` in prod).
- [ ] `https://<agg-domain>/v1/sports` returns **401** without an API key.
- [ ] Worker logs show `Starting worker for ...` and the cron schedule.
- [ ] `full_refresh` cron run succeeds (check
      `/ops/crons/full_refresh/history`).

### Before exposing the domain publicly (required)

- [ ] `AGG_ALLOWED_HOSTS` set to the exact public hostname(s) the service
      answers on. Empty means Starlette accepts any Host header — fine
      while private, not after.
- [ ] `AGG_CORS_ORIGINS` set to the MDProject origin (and any other
      portal). No wildcards.
- [ ] Once MDProject is also migrated to Coolify: set
      `AGG_WEBHOOK_TARGET_URL` to MDProject's `/sportgameodds/webhook`,
      then run `scripts/register_webhook.py` from the `agg-web`
      terminal and paste the resulting secret into MDProject's
      `AGGRIGATOR_WEBHOOK_SECRET` env var.

---

## 7. Coolify-specific knobs

- **Migrations**: run inside the web container's entrypoint on every
  boot (`docker-entrypoint.sh web` → `alembic upgrade head`). Coolify
  has no equivalent of Railway's `preDeployCommand`, so we rely on this
  in-entrypoint step. Idempotent; multiple boots are safe.
- **Persistent storage**: only Postgres needs a volume. Coolify's
  Postgres resource handles that automatically.
- **Build cache**: Coolify caches Docker layers per Application. Hit
  **Force rebuild** if you need a clean image.
- **Logs**: Coolify aggregates stdout/stderr under each app's **Logs**
  tab.
- **Rolling deploys**: Coolify uses the Dockerfile's HEALTHCHECK — old
  container stays up until the new one reports healthy on `/healthz`.
- **Horizontal scale**: web is stateless; the worker can scale to
  multiple replicas safely (Procrastinate uses row-level locks on
  `procrastinate_periodic_defers` to cooperate). At this app's volume
  there's no reason to scale either above 1.

---

## 8. Troubleshooting

**`connection refused` on the DB:** the worker may have started before
Postgres was ready. Coolify will restart it; check the worker's logs.

**Webhook deliveries failing with 401:** MDProject's
`AGGRIGATOR_WEBHOOK_SECRET` doesn't match what's stored on the endpoint.
Re-run `scripts/register_webhook.py --rotate` and update MDProject's env.

**`alembic` errors on deploy:** likely a migration race if you scaled
web replicas above 1. Set replicas=1, redeploy, then scale back up.
Migrations are idempotent — re-running is safe.

**Coolify reports unhealthy after deploy:** check the **Logs** tab. Most
common culprit is a missing or wrong `AGG_DATABASE_URL` (or asyncpg vs
psycopg2 driver prefix swapped between the two URL vars).

**`AGG_TEST_MODE=true` slipped into prod:** flip it back to `false` and
redeploy. While it's on, `/ops/data-reset` is reachable — anyone who
can authenticate to ops can wipe the database.
