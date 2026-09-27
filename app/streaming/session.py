"""Estado de streaming en vivo (HU-DEVICE-005 AC-002/003/004).

Solo si device ACTIVO y asignado (lo valida el llamador/manager).
Auto-stop 30s sin viewer o por comando stop. Bitrate adaptativo
500kbps-2Mbps según estimación de red (fase 1: heurística por latencia poll).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class StreamState(str, Enum):
    IDLE = "IDLE"
    LIVE = "LIVE"


BITRATE_MIN_KBPS = 500
BITRATE_MAX_KBPS = 2000
AUTO_STOP_SEC = 30.0


@dataclass
class StreamManager:
    width: int = 640
    height: int = 480
    fps: int = 15
    bitrate_kbps: int = 1000
    auto_stop_sec: float = AUTO_STOP_SEC
    enabled: bool = True
    state: StreamState = StreamState.IDLE
    session_id: str | None = None
    last_viewer_seen_monotonic: float = field(default_factory=time.monotonic)

    def wants_view(self, session_id: str) -> bool:
        """El backend avisa que hay viewer (poll GET /session o WS fase 2).

        Retorna True si se pasa a LIVE. Solo en IDLE.
        """
        if not self.enabled or not session_id:
            return False
        if self.state is StreamState.LIVE:
            self.last_viewer_seen_monotonic = time.monotonic()
            return False
        self.state = StreamState.LIVE
        self.session_id = session_id
        self.last_viewer_seen_monotonic = time.monotonic()
        return True

    def heartbeat_viewer(self) -> None:
        if self.state is StreamState.LIVE:
            self.last_viewer_seen_monotonic = time.monotonic()

    def stop(self) -> bool:
        """Comando stop. Retorna True si estaba LIVE."""
        if self.state is StreamState.IDLE:
            return False
        self.state = StreamState.IDLE
        self.session_id = None
        return True

    def tick(self, now_monotonic: float | None = None) -> bool:
        """Auto-stop tras auto_stop_sec sin viewer. Retorna True si cortó."""
        if self.state is StreamState.IDLE:
            return False
        now = now_monotonic if now_monotonic is not None else time.monotonic()
        if now - self.last_viewer_seen_monotonic >= self.auto_stop_sec:
            self.stop()
            return True
        return False

    @property
    def is_live(self) -> bool:
        return self.state is StreamState.LIVE

    def adapt_bitrate(self, poll_latency_sec: float) -> int:
        """Heurística fase 1: red lenta -> baja bitrate, red rápida -> sube.

        Límites AC-002: min 500kbps, max 2Mbps.
        """
        if poll_latency_sec >= 1.5:
            self.bitrate_kbps = BITRATE_MIN_KBPS
        elif poll_latency_sec >= 0.6:
            self.bitrate_kbps = 1000
        else:
            self.bitrate_kbps = BITRATE_MAX_KBPS
        return self.bitrate_kbps
