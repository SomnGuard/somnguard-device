"""AC-005: carga de device_config al iniciar y tras cada sync."""
import json
import os

from app.common.config import (
    apply_remote_config,
    load_cached_remote,
    load_default_config,
    load_dotenv,
    load_env_config,
    load_local_override,
    merge_configs,
)
from app.common.models import AlertCode, DeviceConfig


def test_default_config_has_nine_patterns_and_thresholds():
    config = load_default_config()
    assert len(config.sound_patterns) == 9
    for code in AlertCode:
        assert code in config.sound_patterns
    assert config.heartbeat_interval_sec == 30
    assert "fov_min_face_ratio" in config.detection_thresholds


def test_remote_applies_thresholds_patterns_and_volume():
    base = load_default_config()
    remote = {
        "detection_thresholds": {"perclos_threshold": 0.30, "nuevo_umbral": 1},
        "heartbeat_interval_sec": 45,
        "volume_scale": 0.5,
        "sound_patterns": {
            "AS-01": {"frequency_hz": 700, "duration_sec": 0.6, "repetitions": 2,
                      "interval_sec": 0.1, "loop": False, "volume": 0.9},
        },
    }
    merged, changed = apply_remote_config(base, remote)
    assert changed is True
    assert merged.detection_thresholds["perclos_threshold"] == 0.30
    assert merged.heartbeat_interval_sec == 45
    assert merged.volume_scale == 0.5
    assert merged.sound_patterns[AlertCode.AS_01].frequency_hz == 700
    # Lo no mencionado se conserva.
    assert merged.camera_fps == base.camera_fps
    assert merged.sound_patterns[AlertCode.AS_09] == base.sound_patterns[AlertCode.AS_09]


def test_remote_ignores_unknown_keys_and_wrappers():
    base = load_default_config()
    merged, changed = apply_remote_config(base, {
        "config": {"heartbeat_interval_sec": 31, "clave_futura": "x"},
    })
    assert changed is True
    assert merged.heartbeat_interval_sec == 31

    merged2, changed2 = apply_remote_config(base, {"solo_desconocido": 1})
    assert changed2 is False
    assert merged2 == base


def test_remote_invalid_keeps_base():
    base = load_default_config()
    assert apply_remote_config(base, None) == (base, False)
    assert apply_remote_config(base, "no-un-dict") == (base, False)
    merged, changed = apply_remote_config(base, {"heartbeat_interval_sec": -5})
    assert changed is False or merged.heartbeat_interval_sec >= 5  # sanitizado


def test_local_override_file(tmp_path):
    base = DeviceConfig()
    assert load_local_override(base, tmp_path) == base
    override_path = tmp_path / "device_config.override.json"
    override_path.write_text(json.dumps({"heartbeat_interval_sec": 33}), encoding="utf-8")
    merged = load_local_override(base, tmp_path)
    assert merged.heartbeat_interval_sec == 33


def test_merge_does_not_mutate_base():
    base = load_default_config()
    before = dict(base.sound_patterns)
    merge_configs(base, {"sound_patterns": {
        "AS-02": {"frequency_hz": 1, "duration_sec": 1, "repetitions": 1}}})
    assert base.sound_patterns == before


def _clear_somnguard_env(monkeypatch):
    for var in ("SOMNGUARD_API_URL", "SOMNGUARD_API_KEY", "SOMNGUARD_DEVICE_ID",
                "SOMNGUARD_PROVISION_TOKEN", "SOMNGUARD_SERIAL_NUMBER",
                "SOMNGUARD_FIRMWARE_VERSION", "SOMNGUARD_DATA_DIR",
                "SOMNGUARD_LOG_LEVEL", "SOMNGUARD_BACKEND_TIMEOUT_SEC"):
        monkeypatch.delenv(var, raising=False)


def test_dotenv_file_is_loaded(monkeypatch, tmp_path):
    _clear_somnguard_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "# comentario\n"
        "SOMNGUARD_API_URL=http://localhost:8080\n"
        "SOMNGUARD_PROVISION_TOKEN='tok-123'\n"
        'SOMNGUARD_LOG_LEVEL="DEBUG"\n',
        encoding="utf-8",
    )
    assert load_dotenv() == tmp_path / ".env"
    env = load_env_config()
    assert env["api_url"] == "http://localhost:8080"
    assert env["provision_token"] == "tok-123"
    assert env["log_level"] == "DEBUG"


def test_system_env_wins_over_dotenv(monkeypatch, tmp_path):
    _clear_somnguard_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SOMNGUARD_API_URL=http://file:1\n", encoding="utf-8")
    monkeypatch.setenv("SOMNGUARD_API_URL", "http://system:2")
    load_dotenv()
    assert load_env_config()["api_url"] == "http://system:2"
    assert os.environ["SOMNGUARD_API_URL"] == "http://system:2"


def test_dotenv_missing_returns_none(monkeypatch, tmp_path):
    _clear_somnguard_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert load_dotenv(tmp_path / "no-existe.env") is None


def test_cached_remote_missing_returns_base(tmp_path):
    base = load_default_config()
    assert load_cached_remote(base, tmp_path) == base


def test_cached_remote_invalid_returns_base(tmp_path):
    base = load_default_config()
    (tmp_path / "device_config.cache.json").write_text("no-json{{{", encoding="utf-8")
    assert load_cached_remote(base, tmp_path) == base


def test_cached_remote_restores_last_pull(tmp_path):
    base = load_default_config()
    remote = {
        "pulled_at": "2026-09-15T00:00:00+00:00",
        "config": {
            "heartbeat_interval_sec": 45,
            "sound_patterns": {
                "AS-01": {"frequency_hz": 700, "duration_sec": 0.6, "repetitions": 2,
                          "interval_sec": 0.1, "loop": False, "volume": 0.9},
            },
        },
    }
    (tmp_path / "device_config.cache.json").write_text(json.dumps(remote), encoding="utf-8")
    restored = load_cached_remote(base, tmp_path)
    assert restored.heartbeat_interval_sec == 45
    assert restored.sound_patterns[AlertCode.AS_01].frequency_hz == 700


def test_remote_empty_detection_thresholds_keeps_base():
    # Regresión falsa obstrucción/AS-09 al arrancar: la API envía
    # "detection_thresholds": {} (sin overrides) y no debe borrar la base.
    base = load_default_config()
    assert base.detection_thresholds.get("fov_min_face_ratio") == 0.02
    merged, _ = apply_remote_config(base, {
        "detection_thresholds": {},
        "sound_patterns": {},
    })
    assert merged.detection_thresholds == base.detection_thresholds
    assert merged.detection_thresholds.get("fov_min_face_ratio") == 0.02


def test_remote_partial_detection_thresholds_merge():
    base = load_default_config()
    merged, changed = apply_remote_config(base, {
        "detection_thresholds": {"perclos_threshold": 0.30, "nuevo_umbral": 1},
    })
    assert changed is True
    assert merged.detection_thresholds["perclos_threshold"] == 0.30
    assert merged.detection_thresholds["nuevo_umbral"] == 1
    # El resto de la base se conserva (fusión, no reemplazo).
    assert merged.detection_thresholds["fov_min_face_ratio"] == base.detection_thresholds["fov_min_face_ratio"]


def test_boot_order_default_cache_override(tmp_path):
    base = load_default_config()
    (tmp_path / "device_config.cache.json").write_text(json.dumps({
        "pulled_at": "2026-09-15T00:00:00+00:00",
        "config": {"heartbeat_interval_sec": 45},
    }), encoding="utf-8")
    (tmp_path / "device_config.override.json").write_text(
        json.dumps({"heartbeat_interval_sec": 33}), encoding="utf-8")
    # default -> caché -> override: el override gana (ajuste en campo).
    assert load_local_override(load_cached_remote(base, tmp_path), tmp_path).heartbeat_interval_sec == 33
    # Sin override, la caché sobrevive al reinicio.
    (tmp_path / "device_config.override.json").unlink()
    assert load_local_override(load_cached_remote(base, tmp_path), tmp_path).heartbeat_interval_sec == 45
