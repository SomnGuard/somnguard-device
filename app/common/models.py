from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid


class DeviceState(str, Enum):
    REGISTRADO = "REGISTRADO"
    ASIGNADO = "ASIGNADO"
    ACTIVO = "ACTIVO"
    ESPERA = "ESPERA"
    OFFLINE = "OFFLINE"
    SUSPENDIDO = "SUSPENDIDO"
    RETIRADO = "RETIRADO"
    ERROR = "ERROR"


class AlertCode(str, Enum):
    AS_01 = "AS-01"
    AS_02 = "AS-02"
    AS_03 = "AS-03"
    AS_04 = "AS-04"
    AS_05 = "AS-05"
    AS_06 = "AS-06"
    AS_07 = "AS-07"
    AS_08 = "AS-08"
    AS_09 = "AS-09"


class EventCategory(str, Enum):
    SOMNOLENCIA = "SOMNOLENCIA"
    DISTRACCION = "DISTRACCION"
    CINTURON = "CINTURON"
    SISTEMA = "SISTEMA"


class Severity(str, Enum):
    LEVE = "LEVE"
    MODERADA = "MODERADA"
    SEVERA = "SEVERA"
    CRITICA = "CRITICA"
    INFO = "INFO"


class MediaType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"


@dataclass
class SoundPattern:
    alert_code: AlertCode
    frequency_hz: int
    duration_sec: float
    repetitions: int = 1
    interval_sec: float = 0.0
    loop: bool = False
    volume: float = 0.5


@dataclass
class DeviceConfig:
    sensitivity: str = "medium"
    camera_resolution: tuple[int, int] = (1280, 720)
    camera_fps: int = 30
    sound_patterns: dict[AlertCode, SoundPattern] = field(default_factory=dict)
    sync_interval_sec: int = 30
    heartbeat_interval_sec: int = 30
    buffer_limit_mb: int = 2048
    retention_days: int = 7
    detection_thresholds: dict = field(default_factory=dict)
    volume_scale: float = 1.0


@dataclass
class DeviceIdentity:
    serial_number: str
    firmware_version: str
    api_key_hash: str = ""
    api_key: Optional[str] = None
    device_id: Optional[str] = None
    state: DeviceState = DeviceState.REGISTRADO

    def has_credentials(self) -> bool:
        """True si hay device_id real (UUID backend) + api_key para operar."""
        return bool(self.device_id and not self.device_id.startswith("local-") and self.api_key)

    def is_local_mode(self) -> bool:
        return not self.has_credentials()


@dataclass
class Event:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    device_id: str = ""
    event_type_id: str = ""
    category: EventCategory = EventCategory.SISTEMA
    severity: Severity = Severity.INFO
    occurred_at: datetime = field(default_factory=datetime.utcnow)
    alert_code: Optional[AlertCode] = None
    payload: dict = field(default_factory=dict)
    is_offline_sync: bool = True
    synced_at: Optional[datetime] = None


@dataclass
class Evidence:
    evidence_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_id: str = ""
    media_type: MediaType = MediaType.IMAGE
    file_path: str = ""
    file_size_bytes: int = 0
    checksum_sha256: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class AlertLog:
    alert_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_id: str = ""
    alert_code: AlertCode = AlertCode.AS_08
    severity: Severity = Severity.INFO
    timestamp: datetime = field(default_factory=datetime.utcnow)
    message: str = ""


@dataclass
class HeartbeatPayload:
    """Body de POST /api/v1/devices/{id}/heartbeat (HU-API-006 AC-006).

    Campos exactos del contrato backend: firmware_version, pending_count,
    free_disk_pct, uptime_s. Los headers X-Device-ID + X-API-Key viajan aparte.
    """

    device_id: str
    firmware_version: str
    pending_count: int = 0
    free_disk_pct: int = 100
    uptime_s: int = 0

    def to_request_dict(self) -> dict:
        return {
            "firmware_version": self.firmware_version,
            "pending_count": max(0, int(self.pending_count)),
            "free_disk_pct": min(100, max(0, int(self.free_disk_pct))),
            "uptime_s": max(0, int(self.uptime_s)),
        }


@dataclass
class SelfRegisterResult:
    """Resultado de POST /devices/self-register (HU-API-006 AC-009 / ADR-010)."""

    device_id: str
    status: str
    api_key: Optional[str] = None
    claim_code: Optional[str] = None
    created: bool = False  # True si 201 (con secretos), False si 200 (reintento)


# Mapeo estado local (edge) <-> estado backend (HU-API-006 AC-004/AC-006).
# ESPERA es sub-estado local de presencia: frente al backend el device sigue ACTIVE.
# ERROR es solo local (fallo hardware irrecuperable en arranque).
BACKEND_STATUS_TO_LOCAL: dict[str, DeviceState] = {
    "DEVICE_REGISTERED": DeviceState.REGISTRADO,
    "DEVICE_ASSIGNED": DeviceState.ASIGNADO,
    "DEVICE_ACTIVE": DeviceState.ACTIVO,
    "DEVICE_OFFLINE": DeviceState.OFFLINE,
    "DEVICE_SUSPENDED": DeviceState.SUSPENDIDO,
    "DEVICE_RETIRED": DeviceState.RETIRADO,
}

LOCAL_STATUS_TO_BACKEND: dict[DeviceState, str] = {
    DeviceState.REGISTRADO: "DEVICE_REGISTERED",
    DeviceState.ASIGNADO: "DEVICE_ASSIGNED",
    DeviceState.ACTIVO: "DEVICE_ACTIVE",
    DeviceState.ESPERA: "DEVICE_ACTIVE",  # sub-estado local de presencia
    DeviceState.OFFLINE: "DEVICE_OFFLINE",
    DeviceState.SUSPENDIDO: "DEVICE_SUSPENDED",
    DeviceState.RETIRADO: "DEVICE_RETIRED",
}


@dataclass
class SyncResult:
    success: bool
    events_synced: int = 0
    events_failed: int = 0
    config_updated: bool = False
    new_config: Optional[DeviceConfig] = None
    error: Optional[str] = None