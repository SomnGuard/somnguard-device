from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional


logger = logging.getLogger(__name__)


class PresenceMonitor:
    def __init__(self, face_absence_timeout_sec: int = 30):
        self.timeout = timedelta(seconds=face_absence_timeout_sec)
        self._last_face_seen: Optional[datetime] = None
        self._face_present = False
        self._state = "UNKNOWN"
        self.on_state_change: Optional[Callable[[bool], None]] = None

    def update(self, face_present: bool) -> None:
        now = datetime.utcnow()
        if face_present:
            self._last_face_seen = now
            if not self._face_present:
                self._face_present = True
                self._notify(True)
        else:
            if self._face_present and self._last_face_seen:
                if now - self._last_face_seen > self.timeout:
                    self._face_present = False
                    self._notify(False)

    def _notify(self, face_present: bool) -> None:
        if self.on_state_change:
            self.on_state_change(face_present)

    @property
    def face_present(self) -> bool:
        return self._face_present