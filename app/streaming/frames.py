"""Frames JPEG para vivo (HU-DEVICE-005).

Reescala el frame de detección (1280x720) a 640x480 q70 sin mutar el original.
Retorna bytes o None si no hay frame. El publisher WebRTC (aiortc, fase 2)
consumirá estos bytes; hoy sirve para validar cámara compartida sin
cortar la detección.
"""
from __future__ import annotations

from typing import Any


def encode_live_frame(
    frame: Any, width: int = 640, height: int = 480, quality: int = 70
) -> bytes | None:
    if frame is None:
        return None
    try:
        import cv2
    except Exception:
        return None
    try:
        import numpy as np
    except Exception:
        return None
    try:
        if not hasattr(frame, "shape") or len(frame.shape) != 3:
            return None
        if frame.size == 0:
            return None
        resized = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", resized, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            return None
        return bytes(np.asarray(buf).tobytes())
    except Exception:
        return None
