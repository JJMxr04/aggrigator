"""One-shot CLI: register (or rotate) a webhook subscription.

Usage:
    python -m aggrigator.scripts.register_webhook \\
        --url http://localhost:8000/sportgameodds/webhook \\
        --events event.finalized,event.live,event.voided \\
        --owner admin@example.com \\
        --description "MDProject portal receiver"

Behavior:
- Mints a fresh 32-byte URL-safe signing secret.
- Encrypts it with ``AGG_SECRET_ENCRYPTION_KEY`` (Fernet) and stores on the
  WebhookEndpoint row.
- If a row with the same ``(user_id, url)`` already exists, it is rotated
  (new secret + new events list) — matches the typical "I lost the secret,
  give me a new one" UX.
- Prints the raw secret to stdout. **Show only once.** Add it to MDProject's
  ``.env`` as ``AGGRIGATOR_WEBHOOK_SECRET=<that secret>``.

The receiver side stores the raw secret (it doesn't have the Fernet key) and
verifies HMAC-SHA256 on every request — see plan §4.2.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from aggrigator.config import get_settings
from aggrigator.db import session_scope
from aggrigator.models import User, WebhookEndpoint
from aggrigator.security.webhook_signing import encrypt_secret, generate_secret


async def register(
    *,
    url: str,
    events: list[str],
    owner_email: str,
    description: str,
    rotate_existing: bool,
) -> tuple[WebhookEndpoint, str]:
    """Returns ``(endpoint_row, raw_secret)``. Raw secret is shown once and
    must be persisted on the receiver side immediately."""
    async with session_scope() as session:
        owner = await session.scalar(select(User).where(User.email == owner_email))
        if owner is None:
            raise SystemExit(
                f"No user with email={owner_email!r}. "
                "Create one first (e.g. via /admin or signup) and re-run."
            )

        existing = await session.scalar(
            select(WebhookEndpoint).where(
                WebhookEndpoint.user_id == owner.id,
                WebhookEndpoint.url == url,
            )
        )

        raw_secret = generate_secret(num_bytes=32)
        ciphertext = encrypt_secret(raw_secret, key=get_settings().secret_encryption_key)

        if existing is not None:
            if not rotate_existing:
                raise SystemExit(
                    f"Endpoint already exists for (user={owner_email}, url={url}). "
                    "Re-run with --rotate to mint a new secret."
                )
            existing.secret_ciphertext = ciphertext
            existing.events = events
            existing.description = description
            existing.enabled = True
            row = existing
        else:
            row = WebhookEndpoint(
                user_id=owner.id,
                url=url,
                secret_ciphertext=ciphertext,
                events=events,
                description=description,
                enabled=True,
            )
            session.add(row)

        await session.flush()
        # Detach so the caller can read attributes after the session closes.
        endpoint_id = row.id
        endpoint_url = row.url
        endpoint_events = list(row.events or [])

    # Build a lightweight detached copy for stdout.
    detached = WebhookEndpoint(
        id=endpoint_id,
        url=endpoint_url,
        events=endpoint_events,
        secret_ciphertext="",
        description=description,
    )
    return detached, raw_secret


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--url", required=True,
        help="Receiver URL, e.g. http://localhost:8000/sportgameodds/webhook",
    )
    parser.add_argument(
        "--events", required=True,
        help="Comma-separated event names, e.g. event.finalized,event.live,event.voided",
    )
    parser.add_argument(
        "--owner", default="admin@example.com",
        help="Email of the User row to attach the endpoint to (default: admin@example.com)",
    )
    parser.add_argument(
        "--description", default="",
        help="Free-form description shown in /admin",
    )
    parser.add_argument(
        "--rotate", action="store_true",
        help="If an endpoint already exists for this (user, url), mint a new secret.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    events = [e.strip() for e in args.events.split(",") if e.strip()]
    if not events:
        print("error: --events must contain at least one name", file=sys.stderr)
        return 2

    endpoint, raw_secret = asyncio.run(register(
        url=args.url,
        events=events,
        owner_email=args.owner,
        description=args.description,
        rotate_existing=args.rotate,
    ))

    print()
    print("=" * 70)
    print("  Webhook endpoint ready")
    print("=" * 70)
    print(f"  endpoint_id : {endpoint.id}")
    print(f"  url         : {endpoint.url}")
    print(f"  events      : {', '.join(endpoint.events)}")
    print()
    print("  Secret (shown ONCE — paste into MDProject/.env):")
    print()
    print(f"    AGGRIGATOR_WEBHOOK_SECRET={raw_secret}")
    print()
    print("  Then restart Django so the new env value is picked up.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
