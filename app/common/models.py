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


@dataclass
class DeviceIdentity:
    serial_number: str
    api_key_hash: str
    firmware_version: str
    device_id: Optional[str] = None
    state: DeviceState = DeviceState.REGISTRADO


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
    device_id: str
    firmware_version: str
    state: DeviceState
    last_event_id: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class SyncResult:
    success: bool
    events_synced: int = 0
    events_failed: int = 0
    config_updated: bool = False
    new_config: Optional[DeviceConfig] = None
    error: Optional[str] = None