"""Procrastinate App singleton — the task queue + periodic scheduler.

This is the standalone (non-Django) equivalent of MDProject's
``procrastinate.contrib.django.app``. One ``App`` instance is shared
between:

- the FastAPI web process (opens it in ``main:_lifespan`` so request
  handlers can defer jobs via ``task.defer_async()``), and
- the Procrastinate worker process (started by
  ``docker-entrypoint.sh worker`` as
  ``procrastinate -a aggrigator.workers.app.app worker``).

Both processes write to the same Postgres tables
(``procrastinate_jobs`` et al.), which Alembic installs in migration
``0015_procrastinate_schema.py``. There is no separate broker — the
worker listens for ``pg_notify`` events on inserts and picks up jobs
within milliseconds.

The connector is psycopg3 (the modern Postgres driver) — SQLAlchemy's
asyncpg engine elsewhere in the codebase is unaffected; the two drivers
coexist on the same database.
"""

from __future__ import annotations

import logging

from procrastinate import App, PsycopgConnector

from aggrigator.config import get_settings
from aggrigator.observability.sentry import init_sentry

logger = logging.getLogger(__name__)

# The worker process boots via ``procrastinate -a aggrigator.workers.app.app
# worker`` and never imports ``main`` — this module IS its entrypoint, so
# sentry is initialized here. The web process imports this module too;
# init_sentry guards against the double call.
init_sentry(get_settings())


def _conninfo_from_settings() -> str:
    """Strip the SQLAlchemy driver suffix from the sync URL.

    Settings store ``postgresql+psycopg2://...`` for Alembic, but
    psycopg3 wants the bare ``postgresql://...``. Same database, same
    credentials — just a different driver scheme that psycopg3 doesn't
    understand if left in place.
    """
    url = get_settings().database_url_sync
    for prefix in ("postgresql+psycopg2://", "postgresql+asyncpg://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


# Tasks discover the app via this attribute, so import order matters:
# every ``aggrigator/workers/tasks/*.py`` module that registers a task
# imports ``app`` from this module. The procrastinate CLI's
# ``--app aggrigator.workers.app.app`` flag walks the same path.
app = App(
    connector=PsycopgConnector(conninfo=_conninfo_from_settings()),
    # Force every task module to be imported when the worker starts so
    # ``@app.task`` / ``@app.periodic`` decorators run and the registries
    # populate before the worker begins polling. Without this, the worker
    # would start with an empty task list and silently drop every job.
    import_paths=[
        "aggrigator.workers.tasks.full_refresh",
        "aggrigator.workers.tasks.ingest",
        "aggrigator.workers.tasks.load_registry",
        "aggrigator.workers.tasks.logos",
        "aggrigator.workers.tasks.seed",
        "aggrigator.workers.tasks.settle",
        "aggrigator.workers.tasks.vacuum",
        "aggrigator.workers.tasks.watchdog",
        "aggrigator.workers.tasks.webhook_deliver",
    ],
)


def log_periodic_banner() -> None:
    """One-shot boot banner listing every registered periodic task.

    Mirrors MDProject's ``core/crons/apps.py:_log_periodic_banner``. Call
    once at worker startup so the very first lines of the deploy log
    make it obvious what the scheduler thinks it's responsible for. The
    FastAPI process doesn't call this — only ``main.py`` knows it's the
    web process, and the worker entrypoint script triggers the banner
    indirectly when ``procrastinate worker`` boots and registers tasks.
    """
    periodic_tasks = list(app.periodic_registry.periodic_tasks.values())
    lines = [
        "=" * 64,
        "[procrastinate-worker] booted — periodic tasks registered:",
    ]
    if not periodic_tasks:
        lines.append("  (no @app.periodic tasks discovered)")
    else:
        for pt in sorted(periodic_tasks, key=lambda p: p.task.name):
            lines.append(f"  - {pt.task.name:<48}  cron={pt.cron}")
    lines.append("=" * 64)
    for line in lines:
        logger.info(line)
