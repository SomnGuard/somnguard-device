import pytest

from app.common.config import load_env_config
from app.streaming.publisher import ws_url_for


def test_production_rejects_http_api_url(monkeypatch):
    monkeypatch.setenv("SOMNGUARD_ENV", "production")
    monkeypatch.setenv("SOMNGUARD_API_URL", "http://localhost:8080")

    with pytest.raises(ValueError, match="HTTPS"):
        load_env_config()


def test_production_rejects_ws_stream_url(monkeypatch):
    monkeypatch.setenv("SOMNGUARD_ENV", "production")
    monkeypatch.setenv("SOMNGUARD_WS_URL", "ws://localhost:8080/ws/stream")

    with pytest.raises(ValueError, match="WSS"):
        ws_url_for("https://api.somnguard.tech")


def test_production_accepts_wss_stream_url(monkeypatch):
    monkeypatch.setenv("SOMNGUARD_ENV", "production")
    monkeypatch.setenv("SOMNGUARD_WS_URL", "wss://api.somnguard.tech/ws/stream")

    assert ws_url_for("https://api.somnguard.tech") == "wss://api.somnguard.tech/ws/stream"


def test_production_derives_wss_from_https_api_url(monkeypatch):
    monkeypatch.setenv("SOMNGUARD_ENV", "production")
    monkeypatch.delenv("SOMNGUARD_WS_URL", raising=False)

    assert ws_url_for("https://api.somnguard.tech") == "wss://api.somnguard.tech/ws/stream"
