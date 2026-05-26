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
    # PORT defaults to 8001 (matches Dockerfile EXPOSE). Override via env
    # if Coolify or docker-compose maps a different port.
    #
    # ``--forwarded-allow-ips=*`` makes gunicorn trust X-Forwarded-* headers
    # from Coolify's Traefik. Without it the app sees the proxy IP as the
    # client and ``request.url.scheme`` as ``http`` — which breaks
    # rate-limit keying on real client IP and session cookies' ``secure``
    # flag. Trusting "*" is safe because Coolify's edge is the only thing
    # that can reach the container; external clients never bypass it.
    exec gunicorn aggrigator.main:app \
        --worker-class uvicorn.workers.UvicornWorker \
        --workers "${AGG_WEB_WORKERS:-4}" \
        --bind "0.0.0.0:${PORT:-8001}" \
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
    # job delivery (no polling), so small concurrency is plenty for this
    # app's volume.
    exec procrastinate -a aggrigator.workers.app.app worker \
        --concurrency "${AGG_WORKER_CONCURRENCY:-2}"
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
