"""``AGG_REQUIRE_KEY_FOR_READS`` gate (plan §6.2 P0-5 + 6.7).

The flag now defaults to True (enforced); the conftest ``_keyless_reads_
baseline`` fixture pins it back to False for the suite, so the "flag off"
cases below test the emergency-rollback path. Flag FALSE: keyless reads
pass (WARN-logged), a present key is still validated. Flag TRUE: keyless →
401; valid key → 200; infra probes stay keyless forever.
"""

from __future__ import annotations

import pytest

from aggrigator.config import get_settings
from aggrigator.main import app
from tests.integration.factories import make_tenant_user

pytestmark = pytest.mark.asyncio

_READ_PATHS = ["/v1/sports", "/v1/leagues", "/v1/bookmakers", "/v1/events"]


@pytest.fixture
def _flag_on():
    base = get_settings()
    patched = base.model_copy(update={"require_key_for_reads": True})
    app.dependency_overrides[get_settings] = lambda: patched
    yield
    app.dependency_overrides.pop(get_settings, None)


async def test_flag_off_keyless_reads_pass(client) -> None:
    for path in _READ_PATHS:
        assert (await client.get(path)).status_code == 200, path


async def test_flag_off_invalid_key_still_fails_closed(client) -> None:
    """A present-but-bad key must 401 even during the transition —
    otherwise a misconfigured MDProject only breaks at flag-flip time."""
    r = await client.get(
        "/v1/events", headers={"X-Aggrigator-Tenant-Key": "agg_test_bogusbogus"},
    )
    assert r.status_code == 401


async def test_flag_on_keyless_reads_401(client, _flag_on) -> None:
    for path in _READ_PATHS:
        assert (await client.get(path)).status_code == 401, path


async def test_flag_on_valid_key_passes(client, session, _flag_on) -> None:
    _, raw = await make_tenant_user(session, email="reader@example.com")
    for path in _READ_PATHS:
        r = await client.get(path, headers={"X-Aggrigator-Tenant-Key": raw})
        assert r.status_code == 200, path


async def test_flag_on_infra_probes_stay_keyless(client, _flag_on) -> None:
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/readyz")).status_code == 200
    assert (await client.get("/robots.txt")).status_code == 200
