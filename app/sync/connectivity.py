"""Detección de conectividad — HU-DEVICE-003 AC-002.

HEAD a API cada 30s (``sync_interval_sec``); online → sync automático.
Separado del heartbeat con auth (ADR-005 §3): el healthcheck solo responde
¿hay internet? y no actualiza ``last_heartbeat_at``.
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Any, Callable, Optional


logger = logging.getLogger(__name__)


class ConnectivityMonitor:
    """Polling HEAD con callback OFFLINE→ONLINE (dispara sync + reset backoff)."""

    def __init__(self, backend: Any, interval_sec: int = 30,
                 on_online: Optional[Callable[[], Any]] = None):
        self.backend = backend
        self.interval_sec = max(5, int(interval_sec or 30))
        self.on_online = on_online
        self.online: Optional[bool] = None
        self.last_check_monotonic: float = 0.0

    async def check_once(self) -> bool:
        """Un chequeo HEAD. Retorna True=online. Dispara on_online al recuperar."""
        try:
            is_online = await self.backend.healthcheck()
        except Exception as e:
            logger.debug("Healthcheck fallido: %s", e)
            is_online = False
        prev = self.online
        self.online = bool(is_online)
        self.last_check_monotonic = time.monotonic()
        if self.online and prev is False and self.on_online is not None:
            try:
                res = self.on_online()
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.debug("on_online fallido: %s", e)
        return self.online

    def check_once_sync(self) -> bool:
        """Versión sync testeable (usa healthcheck_sync si existe)."""
        fn = getattr(self.backend, "healthcheck_sync", None)
        try:
            is_online = bool(fn()) if fn else False
        except Exception:
            is_online = False
        prev = self.online
        self.online = is_online
        self.last_check_monotonic = time.monotonic()
        if is_online and prev is False and self.on_online is not None:
            try:
                res = self.on_online()
                if asyncio.iscoroutine(res):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(res)
                    except RuntimeError:
                        pass
            except Exception:
                pass
        return is_online
