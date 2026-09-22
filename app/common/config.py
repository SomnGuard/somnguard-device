from __future__ import annotations
import os
import json
from pathlib import Path
from typing import Any
from .models import DEFAULT_VOLUME_SCALE, DeviceConfig, SoundPattern, AlertCode


DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "device.default.json"

# Claves remotas aceptadas en GET /devices/{id}/config (HU-API-005 / RF-TEL-05
# + HU-DEVICE-004 AC-001/AC-003: event_sound_map del catálogo HU-API-004 y
# volume_pct alias de volume_scale según ADR-011).
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
    "volume_pct",
    "event_sound_map",
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


def _resolve_volume_scale(override: dict[str, Any], base_scale: float) -> float:
    """HU-DEVICE-004 AC-003: volume_pct (0-100) y volume_scale (0.0-1.0).

    La API (ADR-011) expone ambos; volume_pct prevalece si viene válido.
    Fuera de rango o inválido -> conserva base (el sanitize acota de todos modos).
    """
    pct = override.get("volume_pct", None)
    if pct is not None:
        try:
            pct_f = float(pct)
            if 0 <= pct_f <= 100:
                return pct_f / 100.0
        except (ValueError, TypeError):
            pass
    scale = override.get("volume_scale", None)
    if scale is not None:
        try:
            return float(scale)
        except (ValueError, TypeError):
            pass
    return base_scale


def _merge_event_sound_map(base_map: dict[str, Any] | None,
                           remote_map: Any) -> dict[str, str]:
    """HU-DEVICE-004 AC-001: fusiona override EV->AS del catálogo.

    Solo conserva entradas con código AS-XX válido; el resto se ignora
    (compatibilidad + robustez ante catálogo corrupto).
    """
    merged = dict(base_map or {})
    if isinstance(remote_map, dict):
        for ev_code, as_code in remote_map.items():
            if not isinstance(ev_code, str) or not isinstance(as_code, str):
                continue
            try:
                AlertCode(as_code.strip())
            except ValueError:
                continue
            merged[ev_code.strip()] = as_code.strip()
    return merged


def extract_config_version(remote: dict[str, Any] | None) -> int:
    """ADR-011: versión global desde GET /config (plano o en sources).

    La API (DeviceConfigResponse) entrega `version` plano +
    `sources{global_version,...}`. Retorna 0 si no hay versión válida.
    """
    if not isinstance(remote, dict):
        return 0
    for key in ("version", "global_version", "globalVersion"):
        try:
            v = int(remote.get(key))  # type: ignore[arg-type]
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    sources = remote.get("sources")
    if isinstance(sources, dict):
        for key in ("global_version", "globalVersion", "version"):
            try:
                v = int(sources.get(key))  # type: ignore[arg-type]
                if v > 0:
                    return v
            except (TypeError, ValueError):
                pass
    # Envolventes {"config": {...}} / {"data": {...}}.
    for wrapper in ("config", "data", "device_config"):
        inner = remote.get(wrapper)
        if isinstance(inner, dict):
            v = extract_config_version(inner)
            if v > 0:
                return v
    return 0


def resolve_alert_code(config: DeviceConfig, event_type_id: str,
                       default_code: AlertCode) -> AlertCode:
    """HU-DEVICE-004 AC-001: override remoto EV->AS con fallback local.

    Si `event_sound_map` trae el evento con un AS-XX válido, manda el catálogo
    (HU-API-004); si no, se conserva el código local de severity.EVENT_TABLE.
    """
    try:
        mapped = (config.event_sound_map or {}).get(event_type_id)
    except AttributeError:
        return default_code
    if isinstance(mapped, str):
        try:
            return AlertCode(mapped.strip())
        except ValueError:
            pass
    return default_code


def merge_configs(base: DeviceConfig, override: dict[str, Any]) -> DeviceConfig:
    if not override:
        return base

    # Desenvuelve {"config"/"data"/"device_config": {...}} si trae claves conocidas.
    if isinstance(override, dict):
        for wrapper in ("config", "data", "device_config"):
            inner = override.get(wrapper)
            if isinstance(inner, dict) and any(k in REMOTE_CONFIG_KEYS for k in inner):
                # Fusiona envolvente + plano (plano manda ante colisión).
                fused = dict(inner)
                for k, v in override.items():
                    if k not in ("config", "data", "device_config"):
                        fused[k] = v
                override = fused
                break

    # ADR-011: versión ANTES de filtrar (version/sources no están en REMOTE_KEYS).
    remote_version = extract_config_version(override)

    # Solo claves conocidas (ignora resto para compatibilidad con API futura).
    override = {k: v for k, v in override.items() if k in REMOTE_CONFIG_KEYS}

    # detection_thresholds se fusiona (no se reemplaza): la API (ADR-011) envía
    # {} cuando no hay overrides y un reemplazo borraría los umbrales de visión
    # del default (fov_min_face_ratio, etc.) dejando al Detector con fallbacks
    # más estrictos -> falsos "FOV deficiente"/AS-09 al arrancar.
    remote_thresholds = override.get("detection_thresholds", None)
    if isinstance(remote_thresholds, dict) and remote_thresholds:
        merged_thresholds = dict(base.detection_thresholds or {})
        merged_thresholds.update(remote_thresholds)
    else:
        merged_thresholds = base.detection_thresholds

    config_dict = {
        "sensitivity": override.get("sensitivity", base.sensitivity),
        "camera_resolution": tuple(override.get("camera_resolution", base.camera_resolution)),
        "camera_fps": override.get("camera_fps", base.camera_fps),
        "sync_interval_sec": override.get("sync_interval_sec", base.sync_interval_sec),
        "heartbeat_interval_sec": override.get("heartbeat_interval_sec", base.heartbeat_interval_sec),
        "buffer_limit_mb": override.get("buffer_limit_mb", base.buffer_limit_mb),
        "retention_days": override.get("retention_days", base.retention_days),
        "detection_thresholds": merged_thresholds,
        # HU-DEVICE-004 AC-003: volume_pct (API) prevalece sobre volume_scale.
        "volume_scale": _resolve_volume_scale(override, base.volume_scale),
        "sound_patterns": base.sound_patterns.copy(),
        # HU-DEVICE-004 AC-001: event_sound_map se fusiona (no reemplaza).
        "event_sound_map": _merge_event_sound_map(
            getattr(base, "event_sound_map", None), override.get("event_sound_map")),
        # ADR-011: conserva la mayor versión vista (nunca retrocede).
        "applied_config_version": max(
            int(getattr(base, "applied_config_version", 0) or 0),
            remote_version,
        ),
    }

    if "sound_patterns" in override:
        sp_override = override["sound_patterns"]
        if isinstance(sp_override, dict):
            for code_str, pattern_data in sp_override.items():
                if not isinstance(pattern_data, dict):
                    continue
                try:
                    alert_code = AlertCode(code_str)
                except ValueError:
                    continue
                # La API envía duration_sec/interval_sec ya convertidos, pero se
                # aceptan aliases duration_ms/interval_ms por robustez.
                try:
                    duration_sec = float(pattern_data.get(
                        "duration_sec",
                        float(pattern_data.get("duration_ms", 500)) / 1000.0
                        if "duration_ms" in pattern_data else 0.5))
                    interval_sec = float(pattern_data.get(
                        "interval_sec",
                        float(pattern_data.get("interval_ms", 0)) / 1000.0
                        if "interval_ms" in pattern_data else 0.0))
                except (ValueError, TypeError):
                    continue
                config_dict["sound_patterns"][alert_code] = SoundPattern(
                    alert_code=alert_code,
                    frequency_hz=int(pattern_data.get("frequency_hz", 800) or 800),
                    duration_sec=duration_sec,
                    repetitions=int(pattern_data.get("repetitions", 1) or 0),
                    interval_sec=interval_sec,
                    loop=bool(pattern_data.get(
                        "loop", int(pattern_data.get("repetitions", 1) or 1) == 0)),
                    volume=float(pattern_data.get("volume", 0.5) or 0.5),
                )

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

    # HU-DEVICE-004 AC-003: volume_pct alias + default 80% (ADR-011).
    try:
        if data.get("volume_pct") is not None:
            _vol = min(100.0, max(0.0, float(data.get("volume_pct")))) / 100.0
        else:
            _vol = float(data.get("volume_scale", DEFAULT_VOLUME_SCALE))
    except (ValueError, TypeError):
        _vol = DEFAULT_VOLUME_SCALE
    # HU-DEVICE-004 AC-001: event_sound_map local (default {} = tabla local).
    _esm = data.get("event_sound_map") if isinstance(
        data.get("event_sound_map"), dict) else {}
    _esm = _merge_event_sound_map(None, _esm)

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
        volume_scale=_vol,
        event_sound_map=_esm,
        applied_config_version=int(data.get("applied_config_version", 0) or 0),
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
        config.volume_scale = DEFAULT_VOLUME_SCALE
    # HU-DEVICE-004 AC-001/AC-003: sanea event_sound_map y versión.
    if not isinstance(getattr(config, "event_sound_map", None), dict):
        config.event_sound_map = {}
    else:
        config.event_sound_map = _merge_event_sound_map(None, config.event_sound_map)
    try:
        config.applied_config_version = max(0, int(
            getattr(config, "applied_config_version", 0) or 0))
    except (ValueError, TypeError):
        config.applied_config_version = 0
    if not isinstance(config.detection_thresholds, dict):
        config.detection_thresholds = {}
    th = config.detection_thresholds
    # Compat aliases legacy -> canónicos Apéndice 2 (HU-DEVICE-001 IT1).
    # No pisa si el canónico ya existe (el remoto/default nuevo manda).
    _aliases = {
        "blink_rate_threshold": ("blink_rate_max", 25),
        "eye_closure_duration_sec": ("eye_closed_min_sec", 2.0),
        "yawn_threshold": ("yawn_mar_threshold", 0.75),
        "head_nod_threshold": ("head_tilt_duration_sec", 3.0),
        "phone_detection_confidence": ("detection_confidence_min", 0.7),
        "gaze_deviation_threshold": ("gaze_deviation_deg", 30),
    }
    for old, (new, _fb) in _aliases.items():
        if old in th and new not in th:
            th[new] = th[old]

    def _clamp(key: str, lo: float, hi: float, fallback: float) -> None:
        try:
            v = float(th.get(key, fallback))
        except (ValueError, TypeError):
            v = fallback
        th[key] = min(hi, max(lo, v))

    _clamp("perclos_threshold", 0.05, 0.9, 0.25)
    _clamp("blink_rate_max", 1, 60, 25)
    _clamp("blink_rate_min", 0, 20, 5)
    _clamp("blink_slow_sec", 0.3, 2.0, 0.5)
    _clamp("eye_closed_min_sec", 0.5, 10, 2.0)
    _clamp("eye_closed_critical_sec", 1.0, 10, 3.0)
    _clamp("yawn_mar_threshold", 0.2, 1.5, 0.85)
    _clamp("yawn_peak_mar_min", 0.5, 1.5, 1.0)
    _clamp("yawn_close_margin", 0.05, 0.4, 0.25)
    _clamp("yawn_min_duration_sec", 0.5, 5.0, 3.0)
    _clamp("yawn_max_duration_sec", 2.0, 15.0, 6.0)
    _clamp("yawn_min_open_ratio", 0.1, 1.0, 0.6)
    _clamp("yawn_cooldown_sec", 5.0, 120.0, 30.0)
    _clamp("signal_smooth_alpha", 0.2, 1.0, 0.6)
    _clamp("head_tilt_deg_min", 5, 60, 15)
    _clamp("head_tilt_duration_sec", 1, 15, 3.0)
    _clamp("ear_closed_threshold", 0.05, 0.4, 0.2)
    _clamp("detection_confidence_min", 0.1, 0.99, 0.7)
    _clamp("belt_detection_confidence", 0.1, 0.99, 0.7)
    _clamp("phone_duration_sec", 0.5, 15, 2.0)
    _clamp("gaze_duration_sec", 0.5, 15, 2.0)
    _clamp("gaze_exit_deg", 5, 60, 22)
    _clamp("gaze_smooth_alpha", 0.05, 1.0, 0.35)
    _clamp("gaze_yaw_offset", -45, 45, 0.0)
    _clamp("gaze_pitch_offset", -45, 45, 0.0)
    _clamp("gaze_calib_samples", 0, 600, 60)
    _clamp("gaze_force_off_sec", 0.5, 5.0, 1.0)
    _clamp("tilt_calib_samples", 0, 600, 60)
    _clamp("pose_yaw_limit_deg", 30, 179, 80.0)
    _clamp("pose_pitch_limit_deg", 30, 179, 65.0)
    _clamp("prolonged_max_repeats", 1, 50, 5)
    _clamp("track_grace_sec", 0.0, 2.0, 0.4)
    _clamp("movement_diff_threshold", 1.0, 100.0, 25.0)
    _clamp("belt_no_detection_sec", 3, 60, 10.0)
    _clamp("frame_budget_sec", 0.5, 5.0, 2.0)
    _clamp("alert_priority_window_sec", 0.0, 30.0, 5.0)
    # HU-DEVICE-004 AC-002/AC-004: escalamiento y cola configurables.
    _clamp("escalation_interval_sec", 5.0, 60.0, 10.0)
    _clamp("escalation_reset_sec", 1.0, 30.0, 5.0)
    _clamp("alert_queue_max", 1, 50, 10)
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


def load_cached_remote(base: DeviceConfig, data_dir: Path | None = None) -> DeviceConfig:
    """Restaura la última config aplicada desde <data_dir>/device_config.cache.json.

    El manager la escribe en cada pull manual exitoso (`_cache_remote_config`).
    Sin esto, un reinicio volvía a `device.default.json` aunque la API tuviera
    `applied == global` (ok) y el heartbeat ya no pedía pull: el device quedaba
    obsoleto para siempre. Si no existe o es inválida, usa base.
    """
    directory = Path(data_dir) if data_dir else Path("data")
    cache_path = directory / "device_config.cache.json"
    if not cache_path.exists():
        return base
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except (OSError, ValueError):
        return base
    # Formato escrito por el manager: {"pulled_at": ..., "config": <respuesta API>}.
    # apply_remote_config ya desenvuelve "config"/"data"/"device_config".
    if isinstance(cached, dict) and isinstance(cached.get("config"), dict):
        cached = cached
    elif not isinstance(cached, dict):
        return base
    try:
        merged, _ = apply_remote_config(base, cached)
        return merged
    except (ValueError, TypeError):
        return base


def get_sound_pattern(config: DeviceConfig, alert_code: AlertCode) -> SoundPattern:
    return config.sound_patterns.get(alert_code, SoundPattern(
        alert_code=alert_code,
        frequency_hz=800,
        duration_sec=0.5,
        repetitions=1,
        volume=0.5,
    ))