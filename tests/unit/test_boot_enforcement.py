"""Unit tests for main._enforce_prod_secrets — fail-closed boot checks.

The function only reads attributes off the settings object, so a
SimpleNamespace stands in for Settings — no env-var monkeypatching, and
each test mutates exactly one field from a known-good prod baseline.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aggrigator.main import (
    _DEFAULT_JWT_SECRET,
    InsecureProductionConfig,
    _enforce_prod_secrets,
)


def _good_prod() -> SimpleNamespace:
    """A prod config that passes every check."""
    return SimpleNamespace(
        env="prod",
        jwt_secret="a" * 64,
        session_secret="b" * 64,
        webhook_secret="c" * 64,
        mdproject_url="https://mdproject.example.com",
        paradise_secret="d" * 64,
        test_mode=False,
        allowed_hosts_list=["aggrigator.example.com"],
    )


def test_good_prod_config_boots() -> None:
    _enforce_prod_secrets(_good_prod())  # no raise


def test_non_prod_env_skips_all_checks() -> None:
    s = _good_prod()
    s.env = "dev"
    s.jwt_secret = _DEFAULT_JWT_SECRET
    s.session_secret = ""
    s.test_mode = True
    s.allowed_hosts_list = []
    s.paradise_secret = ""
    _enforce_prod_secrets(s)  # no raise


@pytest.mark.parametrize("env", ["prod", "production"])
def test_both_prod_spellings_enforced(env: str) -> None:
    s = _good_prod()
    s.env = env
    s.jwt_secret = _DEFAULT_JWT_SECRET
    with pytest.raises(InsecureProductionConfig):
        _enforce_prod_secrets(s)


@pytest.mark.parametrize(
    ("field", "value", "needle"),
    [
        ("jwt_secret", _DEFAULT_JWT_SECRET, "AGG_JWT_SECRET"),
        ("session_secret", "", "AGG_SESSION_SECRET is unset"),
        ("webhook_secret", "", "AGG_WEBHOOK_SECRET"),
        ("mdproject_url", "", "AGG_MDPROJECT_URL"),
        ("paradise_secret", "", "AGG_PARADISE_SECRET"),
        ("test_mode", True, "AGG_TEST_MODE"),
        ("allowed_hosts_list", [], "AGG_ALLOWED_HOSTS"),
    ],
)
def test_each_misconfig_is_fatal_and_named(field, value, needle) -> None:
    s = _good_prod()
    setattr(s, field, value)
    with pytest.raises(InsecureProductionConfig, match=needle):
        _enforce_prod_secrets(s)


def test_session_secret_equal_to_jwt_secret_is_fatal() -> None:
    s = _good_prod()
    s.session_secret = s.jwt_secret
    with pytest.raises(InsecureProductionConfig, match="equals AGG_JWT_SECRET"):
        _enforce_prod_secrets(s)


def test_failure_message_lists_every_problem_at_once() -> None:
    """Operators should see ALL misconfigs in one boot loop, not one per
    redeploy."""
    s = _good_prod()
    s.session_secret = ""
    s.test_mode = True
    s.allowed_hosts_list = []
    with pytest.raises(InsecureProductionConfig) as exc_info:
        _enforce_prod_secrets(s)
    msg = str(exc_info.value)
    assert "AGG_SESSION_SECRET" in msg
    assert "AGG_TEST_MODE" in msg
    assert "AGG_ALLOWED_HOSTS" in msg


def test_resolved_session_secret_property() -> None:
    """Config-side companion: dedicated value wins; dev fallback otherwise."""
    from aggrigator.config import Settings

    s = Settings(AGG_SESSION_SECRET="dedicated", AGG_JWT_SECRET="jwt")
    assert s.resolved_session_secret == "dedicated"
    s = Settings(AGG_SESSION_SECRET="", AGG_JWT_SECRET="jwt")
    assert s.resolved_session_secret == "jwt"
