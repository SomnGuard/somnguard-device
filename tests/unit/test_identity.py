"""AC-006/AC-007: serial/fw, provision token solo en memoria, persistencia segura."""
import json
import os

import app.device.identity as identity_mod
from app.device.identity import (
    load_or_create_identity,
    save_credentials,
    stable_idempotency_key,
)


def _clean_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SOMNGUARD_DATA_DIR", str(tmp_path))
    for var in ("SOMNGUARD_SERIAL_NUMBER", "SOMNGUARD_FIRMWARE_VERSION",
                "SOMNGUARD_API_KEY", "SOMNGUARD_DEVICE_ID",
                "SOMNGUARD_PROVISION_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def test_serial_stable_across_boots_and_firmware_refresh(monkeypatch, tmp_path):
    _clean_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SOMNGUARD_SERIAL_NUMBER", "LAB-123")
    monkeypatch.setenv("SOMNGUARD_FIRMWARE_VERSION", "9.9.9-test")
    first = load_or_create_identity()
    assert first.serial_number == "LAB-123"
    assert first.firmware_version == "9.9.9-test"
    assert first.has_credentials() is False

    # Segundo arranque: mismo serial aunque cambie el fallback de hardware.
    monkeypatch.setattr(identity_mod, "_read_cpu_serial", lambda: "OTRO-SERIAL")
    monkeypatch.setenv("SOMNGUARD_FIRMWARE_VERSION", "9.9.10-test")
    second = load_or_create_identity()
    assert second.serial_number == "LAB-123"
    assert second.firmware_version == "9.9.10-test"  # fw siempre fresca


def test_credentials_persisted_securely_without_token(monkeypatch, tmp_path):
    _clean_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SOMNGUARD_SERIAL_NUMBER", "SER-1")
    ident = load_or_create_identity()
    save_credentials(ident, "11111111-2222-3333-4444-555555555555", "super-secret-key")

    data_file = tmp_path / "device_identity.json"
    assert data_file.exists()
    raw = json.loads(data_file.read_text(encoding="utf-8"))
    assert raw["device_id"] == "11111111-2222-3333-4444-555555555555"
    assert raw["api_key"] == "super-secret-key"
    assert "provision" not in raw and "token" not in " ".join(raw.keys())
    assert set(raw.keys()) == {"serial_number", "firmware_version", "api_key_hash",
                               "api_key", "device_id", "state"}

    reloaded = load_or_create_identity()
    assert reloaded.has_credentials() is True
    assert reloaded.api_key == "super-secret-key"


def test_volatile_states_reset_on_boot(monkeypatch, tmp_path):
    _clean_env(monkeypatch, tmp_path)
    from app.common.models import DeviceState
    from app.device.identity import save_identity
    ident = load_or_create_identity()
    ident.state = DeviceState.ACTIVO
    save_identity(ident)
    assert load_or_create_identity().state == DeviceState.REGISTRADO


def test_idempotency_key_stable_per_serial():
    assert stable_idempotency_key("ABC") == stable_idempotency_key("ABC")
    assert stable_idempotency_key("ABC") != stable_idempotency_key("ABD")
    assert stable_idempotency_key("ABC").startswith("self-register-")


def test_provision_token_from_env_not_persisted(monkeypatch, tmp_path):
    _clean_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SOMNGUARD_PROVISION_TOKEN", "tok-secreto")
    from app.common.config import load_env_config
    env = load_env_config()
    assert env["provision_token"] == "tok-secreto"
    load_or_create_identity()
    content = (tmp_path / "device_identity.json").read_text(encoding="utf-8")
    assert "tok-secreto" not in content
    assert "SOMNGUARD_PROVISION_TOKEN" not in content
    assert "SOMNGUARD_PROVISION_TOKEN" in os.environ  # sigue solo en memoria
