"""Pista de video aiortc desde el frame compartido (sin abrir la cámara dos veces)."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

import numpy as np


def make_camera_track(
    get_frame: Callable[[], Any],
    width: int = 640,
    height: int = 480,
    fps: float | Callable[[], float] = 10.0,
) -> Any:
    """Crea VideoStreamTrack que lee del frame compartido (o negro si no hay).

    Ritmo acotado: aiortc pediría ~30fps y el x264 por software se come el
    CPU de MediaPipe. ``fps`` acepta callable para QoS dinámica (AC-002).
    """
    from aiortc import VideoStreamTrack

    def _fps() -> float:
        try:
            v = fps() if callable(fps) else fps
            return max(1.0, min(15.0, float(v)))
        except Exception:
            return 10.0

    class SharedCameraTrack(VideoStreamTrack):
        kind = "video"

        def __init__(self) -> None:
            super().__init__()
            self._next_at = 0.0

        async def recv(self):  # type: ignore[no-untyped-def]
            import av

            min_interval = 1.0 / _fps()
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_at = max(now, self._next_at) + min_interval
            pts, time_base = await self.next_timestamp()
            try:
                frame = get_frame()
            except Exception:
                frame = None
            if frame is None or not hasattr(frame, "shape"):
                arr = np.zeros((height, width, 3), dtype=np.uint8)
            else:
                try:
                    import cv2

                    arr = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
                    if len(arr.shape) == 2:
                        arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
                except Exception:
                    arr = np.zeros((height, width, 3), dtype=np.uint8)
            vf = av.VideoFrame.from_ndarray(arr, format="bgr24")
            vf.pts = pts
            vf.time_base = time_base
            return vf

    return SharedCameraTrack()
