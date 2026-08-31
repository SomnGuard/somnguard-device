from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional
import cv2
import numpy as np


logger = logging.getLogger(__name__)


class CameraError(Exception):
    pass


@dataclass
class CameraConfig:
    resolution: tuple[int, int] = (1280, 720)
    fps: int = 30


class Camera:
    def __init__(self, resolution: tuple[int, int] = (1280, 720), fps: int = 30):
        self.config = CameraConfig(resolution=resolution, fps=fps)
        self._cap: Optional[cv2.VideoCapture] = None
        self._running = False

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._cap = await loop.run_in_executor(None, self._open_camera)
        if not self._cap or not self._cap.isOpened():
            raise CameraError("No se pudo abrir la cámara")
        self._running = True
        await self._verify_camera()

    def _open_camera(self) -> cv2.VideoCapture:
        import sys
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_V4L2
        cap = cv2.VideoCapture(0, backend)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.resolution[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.resolution[1])
        cap.set(cv2.CAP_PROP_FPS, self.config.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    async def _verify_camera(self) -> None:
        for _ in range(5):
            frame = await self.read_frame()
            if frame is not None and frame.size > 0:
                logger.debug("Cámara verificada OK")
                return
            await asyncio.sleep(0.1)
        raise CameraError("Cámara no entrega frames válidos")

    async def read_frame(self) -> Optional[np.ndarray]:
        if not self._cap or not self._running:
            return None
        loop = asyncio.get_running_loop()
        ret, frame = await loop.run_in_executor(None, self._cap.read)
        if not ret or frame is None:
            return None
        return frame

    async def set_resolution(self, resolution: tuple[int, int]) -> None:
        if self._cap:
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, resolution[0])
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, resolution[1])
            self.config.resolution = resolution

    async def stop(self) -> None:
        self._running = False
        if self._cap:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._cap.release)
            self._cap = None