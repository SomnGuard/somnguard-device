"""Monitor de presencia: Activo <-> Espera (AC-003 / RF-EDGE-09).

Sin rostro durante ``face_absence_timeout_sec`` (default 30s) -> ausente
(Espera); rostro detectado -> presente de inmediato (Activo).

Usa reloj monotónico (inmune a cambios de hora del sistema) y notifica
transiciones vía callback. El callback puede ser sync o async (el manager
lo ejecuta de forma segura en el loop).
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Callable, Optional


logger = logging.getLogger(__name__)


class PresenceMonitor:
    def __init__(self, face_absence_timeout_sec: int = 30):
        self.timeout_sec = float(face_absence_timeout_sec)
        self._last_face_seen_monotonic: Optional[float] = None
        self._face_present = False
        self._initialized = False
        self.on_state_change: Optional[Callable[[bool], None]] = None

    def update(self, face_present: bool) -> Optional[bool]:
        """Procesa una observación. Retorna la transición (True/False) o None."""
        now = time.monotonic()
        if face_present:
            self._last_face_seen_monotonic = now
            if not self._face_present or not self._initialized:
                self._face_present = True
                self._initialized = True
                self._notify(True)
                return True
            self._initialized = True
            return None
        # Sin rostro: solo transiciona si antes había presencia y venció el timeout.
        self._initialized = True
        if self._face_present and self._last_face_seen_monotonic is not None:
            if now - self._last_face_seen_monotonic >= self.timeout_sec:
                self._face_present = False
                self._notify(False)
                return False
        return None

    def _notify(self, face_present: bool) -> None:
        if not self.on_state_change:
            return
        try:
            result = self.on_state_change(face_present)
            if asyncio.iscoroutine(result):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    logger.debug("Sin loop activo; se ignora callback async de presencia")
                    return
                loop.create_task(result)
        except RuntimeError as e:
            logger.debug("Callback de presencia sin loop: %s", e)
        except Exception as e:
            logger.warning("Error en callback de presencia: %s", e)

    def mark_present(self) -> None:
        self._last_face_seen_monotonic = time.monotonic()
        self._face_present = True
        self._initialized = True

    def mark_absent(self) -> None:
        self._face_present = False
        self._initialized = True

    @property
    def face_present(self) -> bool:
        return self._face_present

    @property
    def seconds_since_face(self) -> Optional[float]:
        if self._last_face_seen_monotonic is None:
            return None
        return time.monotonic() - self._last_face_seen_monotonic
