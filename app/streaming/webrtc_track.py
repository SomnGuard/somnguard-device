"""Pista de video aiortc desde el frame compartido (sin abrir la cámara dos veces)."""
from __future__ import annotations

from typing import Any, Callable

import numpy as np


def make_camera_track(get_frame: Callable[[], Any], width: int = 640, height: int = 480) -> Any:
    """Crea VideoStreamTrack que lee del frame compartido (o negro si no hay)."""
    from aiortc import VideoStreamTrack

    class SharedCameraTrack(VideoStreamTrack):
        kind = "video"

        async def recv(self):  # type: ignore[no-untyped-def]
            import av

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
