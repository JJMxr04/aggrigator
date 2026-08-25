# Aggrigator

FastAPI service that owns sports odds/events data for MDProject. It ingests from odds-api.io (live odds/scores) and TheSportsDB (historical), normalizes both into a shared domain model, and exposes the result to MDProject over a keyed HTTP API plus outbound webhooks. MDProject never calls odds-api.io or TheSportsDB directly — aggrigator is the sole upstream client.

## Architecture at a glance

- `aggrigator/models/` — SQLAlchemy domain models (Sport, League, Team, Event, Bookmaker, Market, Selection, OddsQuote, Tenant, ...)
- `aggrigator/ingest/` — odds-api.io + TheSportsDB clients, normalization, translation, lifecycle/reconciliation for cancelled/suspended events, league catalog
- `aggrigator/api/` — versioned HTTP surface MDProject reads from: events, teams, selections, analytics, bets, internal/service routes
- `aggrigator/webhooks/` — outbound delivery + HMAC signing of state-change events to MDProject
- `aggrigator/queries/` — audited read layer; raw SQL is guarded by an AST check (`tests/unit/test_sql_guard.py`)
- `aggrigator/security/` — API keys, Argon2 password hashing, rate limiting, request body size caps
- `aggrigator/workers/` — Procrastinate task definitions (ingest cycles, settlement, webhook delivery)
- `aggrigator/observability/` — Prometheus metrics
- `aggrigator/ops/` — admin console (HTMX-driven cron run history, manual triggers)
- `aggrigator/analytics/` — derived analytics (e.g. the soccer model)
- `aggrigator/schemas/` — Pydantic request/response schemas
- `aggrigator/deps.py` — FastAPI dependencies (auth, tenant resolution)
- `aggrigator/config.py` — pydantic-settings config
- `alembic/` — schema migrations, the source of truth for the DB schema

There's no Redis dependency for the core system — Procrastinate (task queue, periodic schedule, per-cron advisory locks) lives entirely in Postgres. Redis is used for one thing: rate-limit counters (`aggrigator/security/rate_limit.py`), shared across the 4 uvicorn workers via `AGG_RATELIMIT_STORAGE_URL`. It defaults to in-process memory (fine for local dev/tests, advisory-only under multiple workers); `docker-compose.yml`'s `web` service points it at the bundled `redis` container, and prod should point it at the shared Coolify Redis.

## Local dev setup (Homebrew)

```bash
cd /Users/joem/dev/PBL/aggrigator
python3.12 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
```

### Postgres 18 (Homebrew)

```bash
brew install postgresql@18

# Switch Postgres 18 to port 5434 so it doesn't fight any other Postgres
# you might already have on 5432.
echo "port = 5434" >> /opt/homebrew/var/postgresql@18/postgresql.conf

brew services start postgresql@18

# Create the aggregator role + database on the running cluster.
/opt/homebrew/opt/postgresql@18/bin/createuser -p 5434 -s aggrigator
/opt/homebrew/opt/postgresql@18/bin/psql -p 5434 -d postgres \
  -c "ALTER USER aggrigator WITH PASSWORD 'aggrigator';"
/opt/homebrew/opt/postgresql@18/bin/createdb -p 5434 -O aggrigator aggrigator
```

The defaults in `aggrigator/config.py` and `.env.example` already point at `localhost:5434`. Copy `.env.example` to `.env` if you want to override anything.

### Run migrations

```bash
alembic upgrade head
```

### Run the API

```bash
uvicorn aggrigator.main:app --reload --port 3000
# then GET http://localhost:3000/healthz
```

## Running the test suite

**Unit tests** — no DB or network needed:

```bash
pytest tests/unit
```

**Integration tests** — auto-skip when no DB is configured. With the Homebrew setup above already running:

```bash
AGG_TEST_DATABASE_URL=postgresql+asyncpg://aggrigator:aggrigator@localhost:5434/aggrigator_test \
  pytest tests/integration
```

⚠️ Each integration test **TRUNCATEs the aggregator-owned tables** in whatever database this URL points at — always use the dedicated `aggrigator_test` database, never the dev `aggrigator` one (pointing it there wipes your local ingested data; only the registry loader + crons bring it back). One-time setup:

```bash
psql -p 5434 -U aggrigator -d postgres -c 'CREATE DATABASE aggrigator_test OWNER aggrigator'
AGG_DATABASE_URL_SYNC=postgresql+psycopg2://aggrigator:aggrigator@localhost:5434/aggrigator_test \
  alembic upgrade head
```

Tests run through an `httpx.AsyncClient` against the real ASGI app.

There is no parity test suite currently — `tests/parity/` (which cross-checked aggrigator's normalize path against MDProject's `core.event.odds.sgo_normalize`) was removed during a rework; the directory is now empty.

## Alternative: Docker

The bundled `docker-compose.yml` is for CI / clean-machine setup / the moments when you want to nuke everything and rebuild against a known image. It uses **port 5433 (Postgres)** so it can coexist with the Homebrew service above.

```bash
docker compose up -d postgres
AGG_DATABASE_URL=postgresql+asyncpg://aggrigator:aggrigator@localhost:5433/aggrigator \
  alembic upgrade head
```

## Deploying

See [`COOLIFY.md`](./COOLIFY.md) for the full deploy runbook.
