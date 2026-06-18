"""Keyed reads are enforced by default (hardcoded, not env-dependent).

The ``AGG_REQUIRE_KEY_FOR_READS`` flag still exists as an emergency-rollback
override, but its *default* is now True: a fresh deployment rejects keyless
reads to the keyed surface (events / teams / references / selections) without
any env var being set. Logo + infra probes remain keyless (separate routers).
"""

from __future__ import annotations

from aggrigator.config import Settings


def test_require_key_for_reads_defaults_to_true():
    # Read the declared field default directly — independent of env / .env so
    # the assertion is about the hardcoded baseline, not the local environment.
    assert Settings.model_fields["require_key_for_reads"].default is True
