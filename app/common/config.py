from __future__ import annotations
import os
import json
from pathlib import Path
from typing import Any
from .models import (
    DeviceConfig, SoundPattern, AlertCode, DeviceState,
    DeviceIdentity, EventCategory, Severity, MediaType
)


DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "device.default.json"


def load_default_config() -> DeviceConfig:
    with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return _parse_config(data)


def load_env_config() -> dict[str, Any]:
    return {
        "api_url": os.getenv("SOMNGUARD_API_URL", "http://localhost:8000"),
        "api_key": os.getenv("SOMNGUARD_API_KEY", ""),
        "device_id": os.getenv("SOMNGUARD_DEVICE_ID", ""),
        "log_level": os.getenv("SOMNGUARD_LOG_LEVEL", "INFO"),
        "data_dir": Path(os.getenv("SOMNGUARD_DATA_DIR", "data")),
    }


def merge_configs(base: DeviceConfig, override: dict[str, Any]) -> DeviceConfig:
    if not override:
        return base

    config_dict = {
        "sensitivity": override.get("sensitivity", base.sensitivity),
        "camera_resolution": tuple(override.get("camera_resolution", base.camera_resolution)),
        "camera_fps": override.get("camera_fps", base.camera_fps),
        "sync_interval_sec": override.get("sync_interval_sec", base.sync_interval_sec),
        "heartbeat_interval_sec": override.get("heartbeat_interval_sec", base.heartbeat_interval_sec),
        "buffer_limit_mb": override.get("buffer_limit_mb", base.buffer_limit_mb),
        "retention_days": override.get("retention_days", base.retention_days),
        "detection_thresholds": override.get("detection_thresholds", base.detection_thresholds),
        "sound_patterns": base.sound_patterns.copy(),
    }

    if "sound_patterns" in override:
        for code_str, pattern_data in override["sound_patterns"].items():
            try:
                alert_code = AlertCode(code_str)
                config_dict["sound_patterns"][alert_code] = SoundPattern(
                    alert_code=alert_code,
                    frequency_hz=pattern_data.get("frequency_hz", 800),
                    duration_sec=pattern_data.get("duration_sec", 0.5),
                    repetitions=pattern_data.get("repetitions", 1),
                    interval_sec=pattern_data.get("interval_sec", 0.0),
                    loop=pattern_data.get("loop", False),
                    volume=pattern_data.get("volume", 0.5),
                )
            except ValueError:
                pass

    return DeviceConfig(**config_dict)


def _parse_config(data: dict[str, Any]) -> DeviceConfig:
    sound_patterns = {}
    for code_str, pattern_data in data.get("sound_patterns", {}).items():
        try:
            alert_code = AlertCode(code_str)
            sound_patterns[alert_code] = SoundPattern(
                alert_code=alert_code,
                frequency_hz=pattern_data.get("frequency_hz", 800),
                duration_sec=pattern_data.get("duration_sec", 0.5),
                repetitions=pattern_data.get("repetitions", 1),
                interval_sec=pattern_data.get("interval_sec", 0.0),
                loop=pattern_data.get("loop", False),
                volume=pattern_data.get("volume", 0.5),
            )
        except ValueError:
            continue

    return DeviceConfig(
        sensitivity=data.get("sensitivity", "medium"),
        camera_resolution=tuple(data.get("camera_resolution", [1280, 720])),
        camera_fps=data.get("camera_fps", 30),
        sound_patterns=sound_patterns,
        sync_interval_sec=data.get("sync_interval_sec", 30),
        heartbeat_interval_sec=data.get("heartbeat_interval_sec", 30),
        buffer_limit_mb=data.get("buffer_limit_mb", 2048),
        retention_days=data.get("retention_days", 7),
        detection_thresholds=data.get("detection_thresholds", {}),
    )


def get_sound_pattern(config: DeviceConfig, alert_code: AlertCode) -> SoundPattern:
    return config.sound_patterns.get(alert_code, SoundPattern(
        alert_code=alert_code,
        frequency_hz=800,
        duration_sec=0.5,
        repetitions=1,
        volume=0.5,
    ))