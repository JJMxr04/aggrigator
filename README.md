# Aggrigator

FastAPI service that fronts the SportsGameOdds API for MDProject. See the plan at `../aggrigator-plan/plan/plan.md`.

## Quick start (local dev — Homebrew)

```bash
cd /Users/joem/dev/PBL/aggrigator
python3.12 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
```

### Postgres 18 + Redis (Homebrew)

```bash
brew install postgresql@18 redis

# Switch Postgres 18 to port 5434 so it doesn't fight any other Postgres
# you might already have on 5432.
echo "port = 5434" >> /opt/homebrew/var/postgresql@18/postgresql.conf

brew services start postgresql@18
brew services start redis

# Create the aggregator role + database on the running cluster.
/opt/homebrew/opt/postgresql@18/bin/createuser -p 5434 -s aggrigator
/opt/homebrew/opt/postgresql@18/bin/psql -p 5434 -d postgres \
  -c "ALTER USER aggrigator WITH PASSWORD 'aggrigator';"
/opt/homebrew/opt/postgresql@18/bin/createdb -p 5434 -O aggrigator aggrigator
```

The defaults in `aggrigator/config.py` and `.env.example` already point at `localhost:5434` (Postgres) and `localhost:6379` (Redis). Copy `.env.example` to `.env` if you want to override anything.

### Run migrations

```bash
alembic upgrade head
```

### Run the API

```bash
uvicorn aggrigator.main:app --reload --port 8001
# then GET http://localhost:8001/healthz
```

## Running the test suite

**Unit tests** — no DB or network needed:

```bash
pytest tests/unit
```

The parity test (`tests/parity/test_normalize_parity.py`) imports MDProject's `core.event.odds.sgo_normalize` directly and asserts it produces the same `EventSpec` as ours on every captured SGO event. Both project trees must exist on disk (they do by default in `/Users/joem/dev/PBL/`).

**Integration tests** — auto-skip when no DB is configured. With the Homebrew setup above already running:

```bash
AGG_TEST_DATABASE_URL=postgresql+asyncpg://aggrigator:aggrigator@localhost:5434/aggrigator \
  pytest tests/integration
```

Each integration test wipes the aggregator-owned tables, then runs through an `httpx.AsyncClient` against the real ASGI app.

## Alternative: Docker

The bundled `docker-compose.yml` is for CI / clean-machine setup / the moments when you want to nuke everything and rebuild against a known image. It uses **port 5433 (Postgres)** and **port 6380 (Redis)** so it can coexist with the Homebrew services above.

```bash
docker compose up -d postgres redis
AGG_DATABASE_URL=postgresql+asyncpg://aggrigator:aggrigator@localhost:5433/aggrigator \
AGG_REDIS_URL=redis://localhost:6380/0 \
  alembic upgrade head
```

## Phases shipped so far

- **Phase 0** — domain models with parity to MDProject (Sport, League, Team, Event, Bookmaker, BookmakerSelection, Market, Selection, OddsQuote), pure-port of normalize/converters/taxonomy, FixtureSGOClient, parity test.
- **Phase 1** — auth foundation: `auth_user`, `auth_api_key`, `auth_refresh_token`, `client_app` tables; Argon2 passwords, JWT access+refresh, Stripe-style API keys; `/v1/auth/{register,login,refresh,logout,me}` and `/v1/keys` CRUD.

Still to come: read endpoints (`/v1/events`, `/v1/markets`, …), webhook delivery + signing, ARQ workers, SQLAdmin, observability, MDProject side of the cutover.
