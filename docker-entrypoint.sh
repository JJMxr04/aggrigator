#!/bin/sh
# Aggregator container entrypoint. Switches behavior on the first arg so
# Coolify can deploy the same image as both the web service and the
# background worker by overriding ``Custom CMD``.
#
#   web    — apply DB migrations then start gunicorn (FastAPI)
#   worker — start the Procrastinate worker (task execution +
#            in-process periodic scheduler)
#   migrate-only — run alembic and exit (one-shot job)
#   anything else — exec it directly (debugging: e.g. ``sh``, ``python -i``)
set -e

ROLE="${1:-web}"

# Migrations run only on the web container so we don't race two services
# competing for the schema-migration lock. The worker container waits for
# the web container to be healthy (Coolify dependency / start-period).
case "$ROLE" in
  web)
    echo "[entrypoint] applying alembic migrations..."
    alembic upgrade head
    echo "[entrypoint] starting gunicorn (uvicorn workers)..."
    # 4 workers default, tunable via env. ``--access-logfile -`` sends
    # access logs to stdout for journald / docker logs.
    # PORT defaults to 3000 (matches Dockerfile EXPOSE and Coolify's
    # auto-injected PORT env var). Override via env if a different port
    # is needed.
    #
    # ``--forwarded-allow-ips=*`` makes gunicorn trust X-Forwarded-* headers
    # from Coolify's Traefik. Without it the app sees the proxy IP as the
    # client and ``request.url.scheme`` as ``http`` — which breaks
    # rate-limit keying on real client IP and session cookies' ``secure``
    # flag. Trusting "*" is safe because Coolify's edge is the only thing
    # that can reach the container; external clients never bypass it.
    #
    # --workers is INTENTIONALLY hardcoded (was AGG_WEB_WORKERS). Part of
    # the four app services' ~16 vCPU envelope on the 64 GB / 21 vCPU
    # Coolify host (the rest goes to MDProject's share, Matomo, GlitchTip,
    # Coolify, and the Postgres resources) — see COOLIFY.md §"Worker
    # sizing". These are uvicorn ASYNC workers, so 4 already gives high
    # per-worker concurrency, and this tier is mostly machine-to-machine
    # (MDProject calls it). Setting AGG_WEB_WORKERS in Coolify now has NO
    # effect; change the number here + redeploy.
    exec gunicorn aggrigator.main:app \
        --worker-class uvicorn.workers.UvicornWorker \
        --workers 4 \
        --bind "0.0.0.0:${PORT:-3000}" \
        --timeout "${AGG_WEB_TIMEOUT:-30}" \
        --graceful-timeout 30 \
        --forwarded-allow-ips="*" \
        --access-logfile - \
        --error-logfile -
    ;;

  worker)
    echo "[entrypoint] starting procrastinate worker..."
    # Migrations run on the web container; the worker waits for that to
    # finish via the dependency setting in docker-compose / Coolify. We do
    # NOT re-run them here — racing two services for the schema lock is
    # the leading cause of "migrate hangs forever" reports.
    #
    # --concurrency tunes how many jobs run in parallel within this
    # process. Procrastinate uses Postgres LISTEN/NOTIFY for push-based
    # job delivery (no polling). INTENTIONALLY hardcoded (was
    # AGG_WORKER_CONCURRENCY) — part of the apps' ~16 vCPU envelope, see
    # COOLIFY.md §"Worker sizing". Ingestion/settlement-backfill is heavy
    # but bursty, so 2 steady slots borrow idle host CPU when a burst hits.
    # Setting that env var in Coolify now has NO effect; change here +
    # redeploy.
    exec procrastinate -a aggrigator.workers.app.app worker \
        --concurrency 2
    ;;

  migrate-only)
    echo "[entrypoint] running alembic upgrade head and exiting..."
    exec alembic upgrade head
    ;;

  *)
    # Pass-through for ad-hoc debugging.
    exec "$@"
    ;;
esac
