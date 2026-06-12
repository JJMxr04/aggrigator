import os
from aggrigator.config import Settings

def test_loopback_merged_when_validation_on(monkeypatch):
    monkeypatch.setenv("COOLIFY_FQDN", "abc.sslip.io")
    s = Settings(AGG_ALLOWED_HOSTS="aggregator.warforaterra.com")
    assert s.allowed_hosts_list == [
        "aggregator.warforaterra.com", "abc.sslip.io", "127.0.0.1", "localhost",
    ]

def test_empty_stays_empty(monkeypatch):
    monkeypatch.delenv("COOLIFY_FQDN", raising=False)
    s = Settings(AGG_ALLOWED_HOSTS="")
    assert s.allowed_hosts_list == []
