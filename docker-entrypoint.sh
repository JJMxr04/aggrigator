#!/bin/sh
# Aggregator container entrypoint. Switches behavior on the first arg so
# Coolify can deploy the same image as both the web service and the
# background worker by overriding ``Custom CMD``.
#
#   web    — apply DB migrations then start gunicorn (FastAPI)
#   worker — start arq (consumes the queue + cron schedule)
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
    exec gunicorn aggrigator.main:app \
        --worker-class uvicorn.workers.UvicornWorker \
        --workers "${AGG_WEB_WORKERS:-4}" \
        --bind 0.0.0.0:8001 \
        --timeout "${AGG_WEB_TIMEOUT:-30}" \
        --graceful-timeout 30 \
        --access-logfile - \
        --error-logfile -
    ;;

  worker)
    echo "[entrypoint] starting arq worker..."
    exec arq aggrigator.workers.settings.WorkerSettings
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
