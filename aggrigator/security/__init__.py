"""Security primitives — argon2 passwords, JWT, API keys, webhook signing.

Each module here is pure (no DB, no FastAPI, no settings reads at import time).
The HTTP layer composes these with sessions in ``aggrigator.deps``.
"""
