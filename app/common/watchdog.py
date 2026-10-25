"""Guardián del event loop (diagnóstico de paros totales).

Hilo daemon que mete un callback al loop con ``call_soon_threadsafe``: si el
loop no lo ejecuta en ``stall_after_sec``, está parado y se vuelca stacks con
faulthandler para ver qué lo bloquea. Solo loguea ante paro real.
"""
from __future__ import annotations

import asyncio
import faulthandler
import logging
import threading
import time

logger = logging.getLogger(__name__)


def start_loop_watchdog(check_interval_sec: float = 15.0, stall_after_sec: float = 45.0) -> threading.Event:
    """Arranca el guardián. Retorna evento para detenerlo (stop_evt.set())."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        stop_evt = threading.Event()
        stop_evt.set()
        return stop_evt
    stop_evt = threading.Event()

    def _loop_alive(timeout: float) -> bool:
        done = threading.Event()
        try:
            loop.call_soon_threadsafe(done.set)
        except RuntimeError:
            return False
        return done.wait(timeout)

    def _watch() -> None:
        stalled = False
        while not stop_evt.wait(check_interval_sec):
            if _loop_alive(stall_after_sec):
                if stalled:
                    logger.info("Event loop recuperado")
                    stalled = False
                continue
            if stop_evt.is_set():
                return
            logger.error(
                "Event loop sin avance %.0fs: vuelco de stacks para diagnóstico",
                stall_after_sec,
            )
            try:
                faulthandler.dump_traceback()
            except Exception as e:
                logger.error("No se pudo volcar stacks: %s", e)
            stalled = True

    t = threading.Thread(target=_watch, name="loop-watchdog", daemon=True)
    t.start()
    return stop_evt


async def stop_loop_watchdog(stop_evt: threading.Event) -> None:
    try:
        stop_evt.set()
    except Exception:
        pass
    await asyncio.sleep(0)
    _ = time.monotonic()
