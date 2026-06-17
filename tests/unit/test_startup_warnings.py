"""_warn_on_misconfig surfaces survivable misconfigs at startup."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from aggrigator.main import _warn_on_misconfig


def _settings(**over) -> SimpleNamespace:
    base = {"public_base_url": "http://localhost:8001", "env": "dev", "odds_api_key": "k"}
    base.update(over)
    return SimpleNamespace(**base)


def test_warns_when_public_base_url_empty(caplog):
    with caplog.at_level(logging.WARNING):
        _warn_on_misconfig(_settings(public_base_url=""))
    assert "AGG_PUBLIC_BASE_URL is unset" in caplog.text


def test_no_warning_when_public_base_url_set(caplog):
    with caplog.at_level(logging.WARNING):
        _warn_on_misconfig(_settings(public_base_url="http://localhost:8001"))
    assert "AGG_PUBLIC_BASE_URL is unset" not in caplog.text


def test_public_base_warning_fires_in_dev_too(caplog):
    # Unlike the prod-only checks, this one must fire outside prod (the
    # exact env where the relative-URL trap bit us).
    with caplog.at_level(logging.WARNING):
        _warn_on_misconfig(_settings(public_base_url="", env="dev"))
    assert "AGG_PUBLIC_BASE_URL is unset" in caplog.text
