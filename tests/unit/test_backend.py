"""AC-004/AC-007: contratos HTTP self-register / heartbeat / config (sin red real)."""
import io
import json
import urllib.error

import pytest

import app.device.backend as backend_mod
from app.device.backend import (
    BackendAuthError,
    BackendClient,
    BackendConflictError,
    BackendError,
    _redact_headers,
)


API = "http://backend-test:8000"


class FakeResponse:
    def __init__(self, payload, status=200):
        self._raw = json.dumps(payload).encode()
        self.status = status
        self.length = len(self._raw)

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_urlopen(monkeypatch, handler):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["body"] = req.data and json.loads(req.data.decode())
        return handler(captured)

    monkeypatch.setattr(backend_mod.urllib.request, "urlopen", fake_urlopen)
    return captured


def test_self_register_201_parses_secrets_once(monkeypatch):
    captured = _install_urlopen(monkeypatch, lambda c: FakeResponse({
        "deviceId": "11111111-2222-3333-4444-555555555555",
        "status": "DEVICE_REGISTERED",
        "apiKey": "key-una-sola-vez",
        "claimCode": "AAAA-BBBB-CCCC",
    }, status=201))
    client = BackendClient(API)
    created, body = client.self_register_sync("SER-1", "0.2.0", "tok", "idem-1")
    assert created is True
    assert captured["url"] == f"{API}/api/v1/devices/self-register"
    assert captured["method"] == "POST"
    assert captured["body"] == {"serialNumber": "SER-1", "firmwareVersion": "0.2.0"}
    parsed = BackendClient.parse_self_register(body, created)
    assert parsed == {"device_id": "11111111-2222-3333-4444-555555555555",
                      "status": "DEVICE_REGISTERED", "api_key": "key-una-sola-vez",
                      "claim_code": "AAAA-BBBB-CCCC", "created": True}


def test_self_register_200_retry_without_secrets(monkeypatch):
    _install_urlopen(monkeypatch, lambda c: FakeResponse({
        "deviceId": "11111111-2222-3333-4444-555555555555",
        "status": "DEVICE_REGISTERED",
        "apiKey": None,
        "claimCode": None,
    }, status=200))
    created, body = BackendClient(API).self_register_sync("SER-1", "0.2.0", "tok", "idem-1")
    assert created is False
    parsed = BackendClient.parse_self_register(body, created)
    assert parsed["api_key"] is None  # ADR-010: el reintento no reexpone la key
    assert parsed["device_id"] == "11111111-2222-3333-4444-555555555555"


def test_self_register_rejects_bad_token_without_leaking(monkeypatch):
    def handler(c):
        raise urllib.error.HTTPError(c["url"], 401, "Unauthorized", {}, io.BytesIO(b"{}"))

    _install_urlopen(monkeypatch, handler)
    with pytest.raises(BackendAuthError):
        BackendClient(API).self_register_sync("SER-1", "0.2.0", "tok-malo", "idem-1")


def test_self_register_duplicate_serial_conflict(monkeypatch):
    def handler(c):
        raise urllib.error.HTTPError(c["url"], 409, "Conflict", {}, io.BytesIO(b"{}"))

    _install_urlopen(monkeypatch, handler)
    with pytest.raises(BackendConflictError):
        BackendClient(API).self_register_sync("SER-1", "0.2.0", "otro-tok", "idem-2")


def test_heartbeat_sends_contract_and_headers(monkeypatch):
    captured = _install_urlopen(monkeypatch, lambda c: FakeResponse({
        "deviceId": "11111111-2222-3333-4444-555555555555",
        "status": "DEVICE_ACTIVE",
        "lastHeartbeatAt": "2026-09-07T00:00:00Z",
    }))
    body = BackendClient(API).heartbeat_sync(
        "11111111-2222-3333-4444-555555555555", "key",
        {"firmware_version": "0.2.0", "pending_count": 0, "free_disk_pct": 80, "uptime_s": 12})
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert captured["url"].endswith("/api/v1/devices/11111111-2222-3333-4444-555555555555/heartbeat")
    assert headers["x-device-id"] == "11111111-2222-3333-4444-555555555555"
    assert headers["x-api-key"] == "key"
    assert captured["body"] == {"firmware_version": "0.2.0", "pending_count": 0,
                                "free_disk_pct": 80, "uptime_s": 12}
    assert BackendClient.parse_heartbeat(body)["status"] == "DEVICE_ACTIVE"


def test_heartbeat_network_error_is_backend_error(monkeypatch):
    def handler(c):
        raise urllib.error.URLError("red caída")

    _install_urlopen(monkeypatch, handler)
    with pytest.raises(BackendError):
        BackendClient(API).heartbeat_sync("id", "key", {})


def test_fetch_config_404_means_not_available(monkeypatch):
    def handler(c):
        raise urllib.error.HTTPError(c["url"], 404, "Not Found", {}, io.BytesIO(b"{}"))

    _install_urlopen(monkeypatch, handler)
    backend_mod.CONFIG_ENDPOINT_AVAILABLE = True
    assert BackendClient(API).fetch_config_sync("id", "key") is None
    backend_mod.CONFIG_ENDPOINT_AVAILABLE = True  # restaurar para otros tests


def test_secrets_never_logged():
    headers = {"X-Provision-Token": "tok", "X-API-Key": "key", "Idempotency-Key": "idem",
               "Content-Type": "application/json"}
    redacted = _redact_headers(headers)
    assert redacted["X-Provision-Token"] == "***"
    assert redacted["X-API-Key"] == "***"
    assert redacted["Idempotency-Key"] == "***"  # también se ofusca por defecto
    assert redacted["Content-Type"] == "application/json"
