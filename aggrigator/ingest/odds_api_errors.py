"""odds-api.io exception taxonomy.

Names mirror the official `odds-api-io` Python SDK so that swapping in the
SDK later (if it ever exposes response headers — see
aggrigator-plan/odds-api/python-sdk.md) is a one-line import change for
callers.

Reason we don't use the SDK today: it doesn't surface
``x-ratelimit-remaining`` from responses, and that header is the
cornerstone of our per-hour throttle pacer
(aggrigator-plan/odds-api/odds-api.md §6).
"""

from __future__ import annotations


class OddsApiError(Exception):
    """Base class for all odds-api.io adapter errors."""


class InvalidApiKeyError(OddsApiError):
    """HTTP 401 — bad or missing apiKey."""


class RateLimitExceededError(OddsApiError):
    """HTTP 429 — per-hour bucket exhausted. Quota pacer should have prevented this."""


class NotFoundError(OddsApiError):
    """HTTP 404 — event / league / sport doesn't exist."""


class ValidationError(OddsApiError):
    """HTTP 400 — bad query parameter."""


class SchemaError(OddsApiError):
    """Response payload missing a field we require to translate. Raised by
    the translator; caught by the adapter and surfaced as a per-event skip
    rather than a cycle-killing crash.
    """
