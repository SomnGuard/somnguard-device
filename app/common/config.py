from __future__ import annotations
import os
import json
from pathlib import Path
from typing import Any
from .models import DeviceConfig, SoundPattern, AlertCode


DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "device.default.json"

# Claves remotas aceptadas en GET /devices/{id}/config (HU-API-005 / RF-TEL-05).
# Todo lo desconocido se ignora (compatibilidad hacia adelante).
REMOTE_CONFIG_KEYS = frozenset({
    "sensitivity",
    "camera_resolution",
    "camera_fps",
    "sync_interval_sec",
    "heartbeat_interval_sec",
    "buffer_limit_mb",
    "retention_days",
    "detection_thresholds",
    "sound_patterns",
    "volume_scale",
})


def load_default_config() -> DeviceConfig:
    with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return _parse_config(data)


PROJECT_ROOT = Path(__file__).parent.parent.parent


def load_dotenv(env_file: Path | None = None) -> Path | None:
    """Carga un archivo ``.env`` (formato ``KEY=valor``) al entorno, sin dependencias.

    Busca en el directorio de trabajo y luego en la raíz del proyecto.
    Las variables ya exportadas en el sistema **no** se sobrescriben (mandan
    sobre el archivo). Retorna la ruta cargada o ``None`` si no hay archivo.
    """
    candidates = [env_file] if env_file else [Path.cwd() / ".env", PROJECT_ROOT / ".env"]
    for candidate in candidates:
        if candidate and candidate.is_file():
            _parse_dotenv(candidate)
            return candidate
    return None


def _parse_dotenv(path: Path) -> None:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if not key or not key.replace("_", "").isalnum():
            continue
        os.environ.setdefault(key, value)


def load_env_config() -> dict[str, Any]:
    """Lee configuración operativa desde entorno (AC-006/AC-007).

    - ``SOMNGUARD_PROVISION_TOKEN``: X-Provision-Token para self-register.
      Solo vive en memoria: jamás se persiste ni se loguea.
    - ``SOMNGUARD_SERIAL_NUMBER`` / ``SOMNGUARD_FIRMWARE_VERSION``:
      overrides para laboratorio (por defecto se leen del hardware/software).
    - ``SOMNGUARD_API_URL``: base del backend (se normaliza, sin /api/v1).

    Antes de leer, carga el archivo ``.env`` (cwd o raíz del proyecto) si
    existe; las variables del sistema tienen prioridad sobre el archivo.
    """
    load_dotenv()
    api_url = os.getenv("SOMNGUARD_API_URL", "http://localhost:8000").strip().rstrip("/")
    return {
        "api_url": api_url,
        "api_key": os.getenv("SOMNGUARD_API_KEY", ""),
        "device_id": os.getenv("SOMNGUARD_DEVICE_ID", ""),
        "provision_token": os.getenv("SOMNGUARD_PROVISION_TOKEN", ""),
        "serial_number_override": os.getenv("SOMNGUARD_SERIAL_NUMBER", ""),
        "firmware_version_override": os.getenv("SOMNGUARD_FIRMWARE_VERSION", ""),
        "log_level": os.getenv("SOMNGUARD_LOG_LEVEL", "INFO"),
        "data_dir": Path(os.getenv("SOMNGUARD_DATA_DIR", "data")),
        "backend_timeout_sec": float(os.getenv("SOMNGUARD_BACKEND_TIMEOUT_SEC", "10")),
    }


def api_base_url(env_config: dict[str, Any] | None = None) -> str:
    env = env_config or load_env_config()
    return str(env.get("api_url", "http://localhost:8000")).rstrip("/")


def redact_secrets(data: dict[str, Any]) -> dict[str, Any]:
    """Devuelve copia del dict con secretos ofuscados para logs."""
    redacted = dict(data)
    for key in ("api_key", "apiKey", "X-API-Key", "provision_token",
                "X-Provision-Token", "claim_code", "claimCode"):
        if key in redacted and redacted[key]:
            redacted[key] = "***"
    return redacted


def merge_configs(base: DeviceConfig, override: dict[str, Any]) -> DeviceConfig:
    if not override:
        return base

    # Solo claves conocidas (ignora resto para compatibilidad con API futura).
    override = {k: v for k, v in override.items() if k in REMOTE_CONFIG_KEYS}

    config_dict = {
        "sensitivity": override.get("sensitivity", base.sensitivity),
        "camera_resolution": tuple(override.get("camera_resolution", base.camera_resolution)),
        "camera_fps": override.get("camera_fps", base.camera_fps),
        "sync_interval_sec": override.get("sync_interval_sec", base.sync_interval_sec),
        "heartbeat_interval_sec": override.get("heartbeat_interval_sec", base.heartbeat_interval_sec),
        "buffer_limit_mb": override.get("buffer_limit_mb", base.buffer_limit_mb),
        "retention_days": override.get("retention_days", base.retention_days),
        "detection_thresholds": override.get("detection_thresholds", base.detection_thresholds),
        "volume_scale": override.get("volume_scale", base.volume_scale),
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
        volume_scale=float(data.get("volume_scale", 1.0)),
    )


def apply_remote_config(base: DeviceConfig, remote: dict[str, Any] | None) -> tuple[DeviceConfig, bool]:
    """Aplica device_config remota sobre la base (AC-005).

    Acepta tanto el objeto plano como envolventes habituales
    (``{"config": {...}}`` o ``{"data": {...}}``). Valida tipos básicos y
    rangos; ante payload inválido conserva la base.

    Retorna ``(config_resultante, cambió)``.
    """
    if not remote:
        return base, False
    if isinstance(remote, dict):
        for wrapper in ("config", "data", "device_config"):
            inner = remote.get(wrapper)
            if isinstance(inner, dict) and any(k in REMOTE_CONFIG_KEYS for k in inner):
                remote = inner
                break
    if not isinstance(remote, dict):
        return base, False
    try:
        merged = merge_configs(base, remote)
        merged = _sanitize_config(merged)
    except (ValueError, TypeError):
        return base, False
    changed = merged != base
    return merged, changed


def _sanitize_config(config: DeviceConfig) -> DeviceConfig:
    """Coacciona rangos seguros sin lanzar (nunca rompe el arranque)."""
    try:
        w, h = int(config.camera_resolution[0]), int(config.camera_resolution[1])
        if w <= 0 or h <= 0:
            raise ValueError("resolución inválida")
        config.camera_resolution = (w, h)
    except (ValueError, TypeError, IndexError):
        config.camera_resolution = (1280, 720)
    config.camera_fps = min(60, max(1, int(config.camera_fps or 30)))
    config.sync_interval_sec = min(3600, max(5, int(config.sync_interval_sec or 30)))
    config.heartbeat_interval_sec = min(600, max(5, int(config.heartbeat_interval_sec or 30)))
    try:
        config.volume_scale = min(1.0, max(0.0, float(config.volume_scale)))
    except (ValueError, TypeError):
        config.volume_scale = 1.0
    if not isinstance(config.detection_thresholds, dict):
        config.detection_thresholds = {}
    return config


def load_local_override(base: DeviceConfig, data_dir: Path | None = None) -> DeviceConfig:
    """Fusiona override local opcional (<data_dir>/device_config.override.json).

    Permite ajuste en campo sin backend. Si no existe o es inválido, usa base.
    """
    directory = Path(data_dir) if data_dir else Path("data")
    override_path = directory / "device_config.override.json"
    if not override_path.exists():
        return base
    try:
        with open(override_path, "r", encoding="utf-8") as f:
            override = json.load(f)
        merged, _ = apply_remote_config(base, override)
        return merged
    except (OSError, ValueError):
        return base


def get_sound_pattern(config: DeviceConfig, alert_code: AlertCode) -> SoundPattern:
    return config.sound_patterns.get(alert_code, SoundPattern(
        alert_code=alert_code,
        frequency_hz=800,
        duration_sec=0.5,
        repetitions=1,
        volume=0.5,
    ))