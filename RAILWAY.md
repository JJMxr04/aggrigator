# Deploying the Aggregator to Railway

Step-by-step guide for getting the aggregator running on
[Railway](https://railway.app) via the GitHub connector, with Postgres
hosted on [Neon](https://neon.tech) (free tier) and Redis as a Railway
plugin.

This setup targets the **Hobby plan ($5/mo)** and typically runs at
~$5–10/mo all-in.

## What we're deploying

| Service        | Source                              | Purpose                                         |
| -------------- | ----------------------------------- | ----------------------------------------------- |
| Postgres       | Neon (external, free tier)          | persistent DB                                   |
| `agg-redis`    | Railway plugin (Redis)              | queue + rate limiter                            |
| `agg-web`      | GitHub repo, Dockerfile             | gunicorn + uvicorn workers (the FastAPI app)    |
| `agg-worker`   | Same GitHub repo, **same Dockerfile** | arq (background jobs + scheduled crons)       |

`agg-web` and `agg-worker` are built from the **same Dockerfile** — they
just override the start command. Railway lets you do this by creating
two services pointing at the same repo with different "Custom Start
Command" values.

---

## 0. Prerequisites

- A GitHub account with this repo pushed (`JJMxr04/aggrigator`).
- A Railway account (sign in with GitHub — quickest).
- A Neon account (sign up at neon.tech — free, no credit card).
- A real SportsGameOdds API key (or you can ship without it and add later).
- A custom domain for the aggregator (optional — Railway gives you a
  `*.up.railway.app` subdomain by default).

---

## 1. Create the Neon Postgres database

1. Sign in to [console.neon.tech](https://console.neon.tech).
2. **Create a new project** → name it `aggrigator-prod`. Region: pick
   one geographically close to Railway's region (Railway defaults to
   US-West / `us-west2`).
3. After creation Neon shows a **connection string** like:
   ```
   postgresql://user:password@ep-xyz-abc-12345678.us-west-2.aws.neon.tech/neondb?sslmode=require
   ```
   Copy this. You'll convert it into two URLs (async + sync) below.

The aggregator needs **two** DSNs — Alembic uses psycopg2 (sync) and the
app uses asyncpg (async). Same database, different drivers.

Given Neon's connection string `postgresql://USER:PASS@HOST/DB?sslmode=require`:

```
AGG_DATABASE_URL=postgresql+asyncpg://USER:PASS@HOST/DB?ssl=require
AGG_DATABASE_URL_SYNC=postgresql+psycopg2://USER:PASS@HOST/DB?sslmode=require
```

⚠️ Note the difference: **asyncpg uses `ssl=require`**, **psycopg2 uses
`sslmode=require`**. Don't mix them or you'll get connection errors at
startup.

---

## 2. Create the Railway project

1. In Railway → **New Project → Deploy from GitHub repo** → pick
   `JJMxr04/aggrigator`. This creates the project and the first
   service (`agg-web`).
2. Railway auto-detects the `Dockerfile` (and `railway.toml`) in the
   repo root.

---

## 3. Add the Redis plugin

In the project canvas → **+ New → Database → Add Redis**.

Railway provisions a Redis instance and exposes connection variables
(`REDIS_URL`, `REDISHOST`, etc.). You'll reference these from your app
services using Railway's variable-reference syntax.

---

## 4. Configure `agg-web` (the FastAPI service)

Click into the `agg-web` service → **Settings**:

- **Source**: GitHub repo `JJMxr04/aggrigator`, branch `main`.
- **Build**: Dockerfile (auto-detected from `railway.toml`).
- **Healthcheck path**: `/healthz` (already set in `railway.toml`).
- **Custom Start Command**: leave empty (`railway.toml` defaults to `web`).
- **Networking**: click **Generate Domain** — Railway gives you
  `agg-web-production.up.railway.app`. Use that, or attach a custom
  domain.

Then **Variables** → add these (click "Raw Editor" and paste):

```
AGG_ENV=prod
AGG_DEBUG=false
AGG_LOG_LEVEL=INFO
AGG_TEST_MODE=false

# Neon DSNs — see §1 for the URL-shape gotcha.
AGG_DATABASE_URL=postgresql+asyncpg://USER:PASS@HOST/DB?ssl=require
AGG_DATABASE_URL_SYNC=postgresql+psycopg2://USER:PASS@HOST/DB?sslmode=require

# Railway Redis — variable reference fills this in at deploy time.
AGG_REDIS_URL=${{Redis.REDIS_URL}}

# Generate with: openssl rand -hex 32
AGG_JWT_SECRET=<64 hex chars>

# Generate with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
AGG_SECRET_ENCRYPTION_KEY=<44-char base64>

# Real SGO (override the dev defaults that point at the simulator)
SPORTSGAMEODDS_BASE_URL=https://api.sportsgameodds.com/v2
SPORTSGAMEODDS_API_KEY=<your real SGO key>

# Comma-separated origins of any portal that talks to this aggregator.
# Wildcards are rejected.
AGG_CORS_ORIGINS=https://app.example.com

# Optional but recommended
AGG_SENTRY_DSN=https://<key>@sentry.io/<project>

AGG_WEB_WORKERS=4
AGG_WEB_TIMEOUT=30
```

Hit **Deploy**. Railway will:

1. Build the Docker image (cached after the first build).
2. Start the container — the entrypoint runs `alembic upgrade head`
   against Neon, then starts gunicorn on `0.0.0.0:$PORT` (Railway
   injects `$PORT`).
3. Route the public domain you set on the service through Railway's
   edge proxy to the container.

### After first deploy: create your admin user

The DB starts empty. Open the **Shell** tab on the `agg-web` service
in Railway and run:

```sh
python -m scripts.make_admin --email admin@yourdomain.com --password 'CHANGE_ME'
```

(See `scripts/make_admin.py` for the full flag set.) Then sign in at
`https://<your-aggregator-domain>/admin` and rotate the password from
`/admin/users/edit/<id>`.

---

## 5. Add the worker service

In the project canvas → **+ New → GitHub Repo** → pick the **same**
`JJMxr04/aggrigator` repo. This creates a second service from the same
source.

Rename it to `agg-worker`. Then in **Settings**:

- **Custom Start Command**: `/app/docker-entrypoint.sh worker`

  ⚠️ **Must be the full command, not just `worker`.** For
  Dockerfile-based services Railway's start command **replaces** the
  image's `ENTRYPOINT` rather than appending to it — setting
  `worker` alone makes Railway try to exec a `worker` binary that
  doesn't exist. Spelling out the entrypoint path runs our script with
  `worker` as `$1`, which the entrypoint's case statement handles.
- **Healthcheck path**: clear it (the worker doesn't expose HTTP).
- **Networking**: do not generate a domain — the worker has no public
  ingress.

In **Variables**, copy **all** the env vars from `agg-web`. Easiest
path: Railway has **Shared Variables** at the project level — define
the env vars once on the project and reference them from both services
with `${{shared.AGG_JWT_SECRET}}` etc. (Or just paste the raw editor
contents into both services; less elegant but works.)

Hit **Deploy**. The worker has no public ingress — it just consumes
the queue and runs the cron schedule from
`aggrigator.workers.settings.WorkerSettings`.

---

## 6. Bootstrap the events catalog

Trigger the seed + first ingest from
`https://<your-aggregator-domain>/ops/crons` (SQLAdmin auth required).
Click **full_refresh → Run**. This walks SGO, populates
`core_event_event` + `core_market` + `core_selection`, and sets up the
league refresh cadence.

---

## 7. (Optional) Register a webhook subscriber

If something downstream needs `event.finalized` / `event.voided`
deliveries, register it from the Railway **Shell** tab on `agg-web`:

```sh
python -m aggrigator.scripts.register_webhook \
    --url https://<subscriber-domain>/some/webhook \
    --events event.finalized,event.voided \
    --owner admin@yourdomain.com \
    --description "downstream subscriber"
```

The script prints a one-time secret. Paste it into the subscriber's
env vars.

---

## 8. Railway-specific knobs

- **Persistent storage**: not needed for the aggregator. Postgres lives
  on Neon, Redis on the Railway plugin.
- **Build cache**: Railway caches Docker layers per service. Force a
  clean rebuild with **Settings → Redeploy → Skip Cache**.
- **Logs**: Railway aggregates stdout/stderr per-service under each
  service's **Deployments → View Logs**.
- **Rolling deploys**: Railway honors the Dockerfile `HEALTHCHECK` (and
  the `healthcheckPath` from `railway.toml`) — old container stays up
  until the new one reports healthy on `/healthz`.
- **Worker singleton**: keep `agg-worker` at **1 replica**. ARQ's
  scheduler isn't HA — running two workers means two copies of every
  cron job.
- **Web replicas**: keep `agg-web` at 1 replica unless you split out
  Alembic migrations into a separate one-shot deploy job. Two web
  replicas can race on the migration lock at startup.
- **Internal networking**: Railway plugins (Redis) are reachable from
  app services via the project's private network — `${{Redis.REDIS_URL}}`
  resolves to a private hostname automatically.

---

## 9. Pre-flight checklist

Before you mark a deploy "done", confirm:

- [ ] `https://<agg-domain>/healthz` returns `{"ok": true, "version": "..."}`.
- [ ] `https://<agg-domain>/robots.txt` returns `User-agent: *\nDisallow: /`.
- [ ] Every response carries `X-Robots-Tag: noindex, nofollow, noarchive`.
- [ ] `https://<agg-domain>/admin` requires login.
- [ ] `https://<agg-domain>/ops/data-reset` returns **403**
      (`AGG_TEST_MODE=false` in prod).
- [ ] `https://<agg-domain>/v1/sports` returns 401 without an API key.
- [ ] Worker logs show `Starting worker for ...` and the cron schedule.
- [ ] `full_refresh` cron run succeeds (check `/ops/crons/full_refresh/history`).

---

## 10. Troubleshooting

**`connection refused` on the DB at startup**
→ Check that `AGG_DATABASE_URL` uses `?ssl=require` (asyncpg syntax) and
`AGG_DATABASE_URL_SYNC` uses `?sslmode=require` (psycopg2 syntax). Neon
enforces TLS — connections without it fail.

**`relation "<...>" does not exist` after first deploy**
→ Alembic didn't run, or ran against the wrong DB. The web entrypoint
runs `alembic upgrade head` on every start; check the deploy logs for
the `[entrypoint] applying alembic migrations...` line. If it's missing,
verify `AGG_DATABASE_URL_SYNC` is set.

**Healthcheck fails immediately after deploy**
→ Most common: the app is binding to a hardcoded port instead of
`$PORT`. Check that `docker-entrypoint.sh` was updated to use
`${PORT:-8001}` (this repo's entrypoint already does). Also check
`AGG_DATABASE_URL` — a bad DSN crashes uvicorn workers in a loop.

**`alembic` errors on deploy after working before**
→ Likely a migration race because you scaled `agg-web` above 1 replica.
Set replicas back to 1, redeploy. Migrations are idempotent — re-running
is safe.

**Neon free tier compute hours exhausted mid-month**
→ The free tier caps compute at ~190 hours/month per branch. With the
worker hitting Postgres on cron, scale-to-zero rarely engages. If you
hit the cap, either upgrade to Neon Launch ($19/mo), or migrate Postgres
to the Railway plugin (~$5/mo metered) by changing the two `AGG_DATABASE_URL*`
values.

**Worker can't reach Redis**
→ The worker's `AGG_REDIS_URL` must reference the same Redis plugin as
`agg-web`. If you used `${{Redis.REDIS_URL}}` in both, it's automatic.
If you typed the URL by hand, confirm the host matches.

**`SECURE_SSL_REDIRECT`-style infinite loop**
→ Doesn't apply (FastAPI side); but if you're behind Cloudflare in front
of Railway, set Cloudflare's SSL mode to **Full (strict)** so it talks
HTTPS to Railway, not HTTP.
