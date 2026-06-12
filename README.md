# Aggrigator

FastAPI service that fronts the SportsGameOdds API for MDProject. See the plan at `../aggrigator-plan/plan/plan.md`.

## Quick start (local dev — Homebrew)

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

The defaults in `aggrigator/config.py` and `.env.example` already point at `localhost:5434` (Postgres). Copy `.env.example` to `.env` if you want to override anything. There's no Redis to install — the Procrastinate task queue, periodic schedule, and per-cron advisory locks all live in Postgres.

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

The parity test (`tests/parity/test_normalize_parity.py`) imports MDProject's `core.event.odds.sgo_normalize` directly and asserts it produces the same `EventSpec` as ours on every captured SGO event. Both project trees must exist on disk (they do by default in `/Users/joem/dev/PBL/`).

**Integration tests** — auto-skip when no DB is configured. With the Homebrew setup above already running:

```bash
AGG_TEST_DATABASE_URL=postgresql+asyncpg://aggrigator:aggrigator@localhost:5434/aggrigator_test \
  pytest tests/integration
```

⚠️ Each integration test **TRUNCATEs the aggregator-owned tables** in whatever
database this URL points at — always use the dedicated ``aggrigator_test``
database, never the dev ``aggrigator`` one (pointing it there wipes your local
ingested data; only the registry loader + crons bring it back). One-time setup:

```bash
psql -p 5434 -U aggrigator -d postgres -c 'CREATE DATABASE aggrigator_test OWNER aggrigator'
AGG_DATABASE_URL_SYNC=postgresql+psycopg2://aggrigator:aggrigator@localhost:5434/aggrigator_test \
  alembic upgrade head
```

Tests run through an `httpx.AsyncClient` against the real ASGI app.

## Alternative: Docker

The bundled `docker-compose.yml` is for CI / clean-machine setup / the moments when you want to nuke everything and rebuild against a known image. It uses **port 5433 (Postgres)** so it can coexist with the Homebrew service above.

```bash
docker compose up -d postgres
AGG_DATABASE_URL=postgresql+asyncpg://aggrigator:aggrigator@localhost:5433/aggrigator \
  alembic upgrade head
```

## Phases shipped so far

- **Phase 0** — domain models with parity to MDProject (Sport, League, Team, Event, Bookmaker, BookmakerSelection, Market, Selection, OddsQuote), pure-port of normalize/converters/taxonomy, FixtureSGOClient, parity test.
- **Phase 1** — auth foundation: `auth_user`, `auth_api_key`, `auth_refresh_token`, `client_app` tables; Argon2 passwords, JWT access+refresh, Stripe-style API keys; `/v1/auth/{register,login,refresh,logout,me}` and `/v1/keys` CRUD.

Still to come: read endpoints (`/v1/events`, `/v1/markets`, …), webhook delivery + signing, Procrastinate workers, SQLAdmin, observability, MDProject side of the cutover.
