# Deploying the Aggregator to Coolify

This is a step-by-step guide for getting the aggregator running on Coolify
(self-hosted, behind Traefik). It assumes you already have a Coolify
server reachable from the public internet and a domain pointed at it.

## What we're deploying

| Service              | Type                        | Purpose                                        |
| -------------------- | --------------------------- | ---------------------------------------------- |
| `agg-postgres`       | Coolify resource (Postgres) | persistent DB + task queue (Procrastinate)     |
| `agg-web`            | Application (Dockerfile)    | gunicorn + uvicorn workers (the FastAPI app)   |
| `agg-worker`         | Application (Dockerfile)    | Procrastinate worker (background jobs + cron)  |

`agg-web` and `agg-worker` are built from the **same Dockerfile in this
repo** — they just override `CMD`. Coolify lets you do this by creating
two Applications that point at the same Git source with different start
commands.

---

## 1. Create the resources

In Coolify → **Resources**:

1. Click **New Resource → Postgres** (version 18). Note the generated
   connection URL — you'll paste it into the apps below.

Procrastinate creates its task-queue tables inside this same Postgres
on first `alembic upgrade head` — no separate broker / Redis service
to provision.

---

## 2. Create the web Application

In Coolify → **Applications → New Application**:

- **Source**: this repo (Git URL).
- **Build pack**: `Dockerfile` (auto-detects the `Dockerfile` in repo root).
- **Port**: `8001` (matches `EXPOSE` in the Dockerfile).
- **Health check path**: `/healthz` (already defined in the Dockerfile's
  `HEALTHCHECK`, but Coolify also reads this for its own probe).
- **Custom CMD**: leave empty (the entrypoint defaults to `web`).

Click **Environment variables** and paste these (replace placeholders):

```
AGG_ENV=prod
AGG_DEBUG=false
AGG_LOG_LEVEL=INFO
AGG_TEST_MODE=false                      # MUST be false in prod (gates data-reset)

AGG_DATABASE_URL=postgresql+asyncpg://<user>:<pass>@<coolify-pg-host>:5432/<db>
AGG_DATABASE_URL_SYNC=postgresql+psycopg2://<user>:<pass>@<coolify-pg-host>:5432/<db>

# Generate with: openssl rand -hex 32
AGG_JWT_SECRET=<64 hex chars>

# Generate with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
AGG_SECRET_ENCRYPTION_KEY=<44-char base64>

# Real SGO (override the dev defaults that point at the simulator)
SPORTSGAMEODDS_BASE_URL=https://api.sportsgameodds.com/v2
SPORTSGAMEODDS_API_KEY=<your real SGO key>

# Comma-separated origins of MDProject (and any other portal that talks
# to this aggregator). Wildcards are rejected.
AGG_CORS_ORIGINS=https://app.example.com

AGG_WEB_WORKERS=4
AGG_WEB_TIMEOUT=30
```

Hit **Deploy**. The container will:

1. Run `alembic upgrade head` against the Postgres resource (idempotent).
2. Start gunicorn on `0.0.0.0:8001`.
3. Coolify's Traefik routes the public domain you set on the app to
   `agg-web:8001`.

### After first deploy: create your admin user + API keys

The DB starts empty. SSH into the running container (Coolify exposes a
web shell under the app's **Terminal** tab) and run:

```sh
python -c "
from aggrigator.db import session_scope
from aggrigator.models.auth import User, UserRole
from aggrigator.security.passwords import hash_password
import asyncio, uuid
async def main():
    async with session_scope() as s:
        s.add(User(
            id=uuid.uuid4(),
            email='admin@yourdomain.com',
            password_hash=hash_password('CHANGE_ME'),
            role=UserRole.ADMIN,
            is_active=True,
        ))
asyncio.run(main())
"
```

Then sign in at `https://<your-aggregator-domain>/admin` and rotate the
password from `/admin/users/edit/<id>` — or wire your real auth flow.

---

## 3. Create the worker Application

In Coolify → **Applications → New Application** (point at the **same**
Git source, **same** Dockerfile):

- **Port**: leave empty (worker doesn't expose anything).
- **Custom CMD**: `worker`
- **Health check**: disable, or use a custom command if you want one.

Same env vars as `agg-web` (it shares the DB + secrets). The
Procrastinate worker loads the periodic schedule by importing every
task module — the registrations live as `@app.periodic(cron=...)`
decorators on each task function.

Hit **Deploy**. The worker has no public ingress — it just consumes the
queue.

---

## 4. Register the MDProject webhook endpoint

Once both apps are up, register MDProject's webhook receiver from inside
the running web container's terminal:

```sh
python -m aggrigator.scripts.register_webhook \
    --url https://<mdproject-domain>/sportgameodds/webhook \
    --events event.finalized,event.voided \
    --owner admin@yourdomain.com \
    --description "MDProject portal receiver"
```

The script prints a one-time secret. Copy it into MDProject's
`AGGRIGATOR_WEBHOOK_SECRET` env var (Coolify env vars on the
`mdproject-web` Application — see `MDProject/COOLIFY.md`).

---

## 5. Bootstrap the events catalog

Trigger the seed + first ingest from `https://<your-aggregator-domain>/ops/crons`
(SQLAdmin auth required). Click **full_refresh → Run**. This walks SGO,
populates `core_event_event` + `core_market` + `core_selection`, and
sets up the league refresh cadence.

---

## 6. Coolify-specific knobs

- **Persistent storage**: only Postgres needs a volume. Coolify's
  Postgres resource handles this automatically.
- **Build cache**: Coolify caches Docker layers per Application. If you
  need a clean rebuild, hit **Force rebuild**.
- **Logs**: Coolify aggregates stdout/stderr from both web + worker
  under the app's **Logs** tab.
- **Rolling deploys**: Coolify uses the Dockerfile's HEALTHCHECK — old
  container stays up until the new one reports healthy on `/healthz`.
- **Multi-region / horizontal scale**: bump replicas in the app's
  **Resource limits**. Web is stateless; the worker can scale to
  multiple replicas safely (Procrastinate's periodic scheduler uses
  row-level locks on `procrastinate_periodic_defers` to cooperate)
  but at this app's volume there's no reason to.

---

## 7. Pre-flight checklist

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

## 8. Troubleshooting

**`connection refused` on the DB:** the worker may have started before
Postgres was ready. Coolify will restart it; check the worker's logs.

**Webhook deliveries failing with 401:** MDProject's
`AGGRIGATOR_WEBHOOK_SECRET` doesn't match what's stored on the endpoint.
Re-run `register_webhook.py --rotate` and update MDProject's env.

**`alembic` errors on deploy:** likely a migration race if you scaled
web replicas above 1 before this section. Set replicas=1, redeploy,
then scale back up. Migrations are idempotent — re-running is safe.

**Coolify reports unhealthy after deploy:** check `docker logs`. Most
common culprit is missing `AGG_DATABASE_URL` or a Fernet key mismatch on
`AGG_SECRET_ENCRYPTION_KEY` (decryption of stored webhook secrets fails).
