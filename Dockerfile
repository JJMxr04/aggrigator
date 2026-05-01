# syntax=docker/dockerfile:1.6
#
# Aggregator container — used by Coolify (Application type: Dockerfile).
# One image, two roles:
#   - web:    gunicorn + uvicorn workers (the FastAPI app)
#   - worker: arq (the background queue runner — webhook delivery, crons)
# Coolify deploys each role as its own service, both pointing at this image
# but overriding CMD. Same code, same deps, distinct processes.

# ---------- builder ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build deps for asyncpg / cryptography wheels on slim base.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first for better layer caching — only re-runs when
# pyproject changes, not on every code edit.
COPY pyproject.toml ./
RUN pip install --upgrade pip \
    && pip install --prefix=/install . \
    && pip install --prefix=/install gunicorn

# ---------- runtime ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Runtime libs only (libpq for psycopg2, ca-certs for HTTPS to SGO).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --no-create-home --shell /bin/false app

# Pull installed packages from builder stage.
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy code last so source edits don't bust the deps layer.
COPY --chown=app:app . /app

# Strip dev-only files at build time so the image stays small.
RUN rm -rf tests/ .pytest_cache/ .git/ .vscode/ aggrigator-plan/ 2>/dev/null || true \
    && chmod +x /app/docker-entrypoint.sh

USER app

# FastAPI binds to ${PORT:-8001}. Railway injects $PORT; Coolify and
# docker-compose default to 8001 via the entrypoint fallback.
EXPOSE 8001

# HEALTHCHECK shell-form so ${PORT} expands at runtime. Coolify and Railway
# both gate rolling deploys on this.
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8001}/healthz" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]

# Default CMD = web. Worker service overrides via Coolify's "Custom CMD".
CMD ["web"]
