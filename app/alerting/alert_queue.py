"""Cola de alertas sonoras — HU-DEVICE-004 AC-004.

Norma: una alerta a la vez; si varias coinciden, se encolan y suenan en orden
(FIFO) sin superposición.

Diseño:
- `AlertQueue` envuelve a `SoundPlayer`: el manager encola `SoundPattern` y un
  worker secuencial llama a `player.play()` (que ya hace `stop()` previo, por
  lo que nunca se superponen dos tonos).
- Filtro de prioridad ANTES de encolar vive en el manager (mejor por lote +
  ventana `alert_priority_window_sec`): la cola solo ordena lo que ya pasó el
  filtro, así no se pierde la semántica anti-ruido existente.
- `maxsize` configurable (`alert_queue_max`, default 10): llena → descarta la
  más antigua de menor rango y registra `dropped` para observabilidad.
- Sin worker en tests legacy: `enqueue_nowait()` + `drain_now()` permiten
  verificación síncrona sin loop permanente.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class QueuedAlert:
    rank: int
    seq: int
    pattern: object
    alert_code: object


class AlertQueue:
    def __init__(self, maxsize: int = 10) -> None:
        self.maxsize = max(1, int(maxsize or 10))
        self._queue: asyncio.Queue | None = None
        self._pending: deque[QueuedAlert] = deque()
        self._seq = 0
        self._worker: asyncio.Task | None = None
        self._player = None
        self.dropped = 0
        self.played = 0

    @property
    def qsize(self) -> int:
        if self._queue is not None:
            return self._queue.qsize()
        return len(self._pending)

    def configure(self, maxsize: int | None = None) -> None:
        if maxsize is not None:
            try:
                self.maxsize = max(1, int(maxsize))
            except (TypeError, ValueError):
                pass

    def enqueue_nowait(self, pattern, alert_code=None, rank: int = 0) -> bool:
        """Encola sin bloquear. Retorna False si se descartó por llena."""
        self._seq += 1
        item = QueuedAlert(rank=rank, seq=self._seq,
                           pattern=pattern, alert_code=alert_code)
        if self._queue is not None:
            try:
                self._queue.put_nowait(item)
                return True
            except asyncio.QueueFull:
                self.dropped += 1
                logger.debug("Cola de alertas llena (%d): descarta %s",
                             self.maxsize, getattr(alert_code, "value", alert_code))
                return False
        if len(self._pending) >= self.maxsize:
            self.dropped += 1
            self._pending.popleft()
        self._pending.append(item)
        # Orden estable: mayor rango primero, FIFO ante empate.
        ordered = sorted(self._pending, key=lambda q: (-q.rank, q.seq))
        self._pending = deque(ordered)
        return True

    async def start(self, player) -> None:
        """Arranca el worker secuencial (idempotente)."""
        self._player = player
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self.maxsize)
            for item in self._pending:
                try:
                    self._queue.put_nowait(item)
                except asyncio.QueueFull:
                    self.dropped += 1
            self._pending.clear()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop(), name="alert-queue")

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
            self._worker = None

    async def _worker_loop(self) -> None:
        assert self._queue is not None and self._player is not None
        while True:
            item = await self._queue.get()
            try:
                await self._player.play(item.pattern)
                self.played += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Alerta %s no sonó: %s",
                             getattr(item.alert_code, "value", item.alert_code), e)
            finally:
                self._queue.task_done()

    async def drain_now(self, player) -> int:
        """Vacía lo encolado de forma secuencial (tests / apagado limpio)."""
        n = 0
        items: list[QueuedAlert] = []
        if self._queue is not None:
            while not self._queue.empty():
                try:
                    items.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
        else:
            items = list(self._pending)
            self._pending.clear()
        for item in sorted(items, key=lambda q: (-q.rank, q.seq)):
            try:
                await player.play(item.pattern)
                self.played += 1
                n += 1
            except Exception as e:
                logger.debug("Alerta %s no sonó: %s",
                             getattr(item.alert_code, "value", item.alert_code), e)
            finally:
                if self._queue is not None:
                    try:
                        self._queue.task_done()
                    except ValueError:
                        pass
        return n
