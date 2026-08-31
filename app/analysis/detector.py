from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from app.analysis.landmarks import FaceLandmarks, LandmarkDetector, get_key_points


logger = logging.getLogger(__name__)


@dataclass
class DetectionResult:
    events: list[dict]
    face_present: bool
    fov_ok: bool
    landmarks: Optional[FaceLandmarks] = None


class Detector:
    def __init__(self, thresholds: dict[str, Any]):
        self.thresholds = thresholds
        self._landmark_detector: Optional[LandmarkDetector] = None
        self._fov_min_face_ratio = thresholds.get("fov_min_face_ratio", 0.15)
        self._fov_max_face_ratio = thresholds.get("fov_max_face_ratio", 0.85)
        self._obstruction_landmark_threshold = thresholds.get("obstruction_landmark_threshold", 0.3)
        
        # Timer de obstrucción para escalamiento
        self._obstruction_start_time: Optional[float] = None
        self._last_alert_time: Optional[float] = None
        self._alert_stage = 0  # 0=ninguna, 1=5s, 2=20s
        
        # Estado previo para detectar transiciones
        self._prev_face_present: Optional[bool] = None
        self._prev_fov_ok: Optional[bool] = None
        self._prev_obstructed: Optional[bool] = None
        
        logger.info(f"Detector umbrales: fov_min={self._fov_min_face_ratio}, fov_max={self._fov_max_face_ratio}")

    async def load_model(self) -> None:
        self._landmark_detector = LandmarkDetector(
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
            max_num_faces=1,
        )
        logger.info("Detector inicializado con MediaPipe Face Mesh")

    def update_thresholds(self, thresholds: dict[str, Any]) -> None:
        self.thresholds = thresholds
        self._fov_min_face_ratio = thresholds.get("fov_min_face_ratio", 0.15)
        self._fov_max_face_ratio = thresholds.get("fov_max_face_ratio", 0.85)
        self._obstruction_landmark_threshold = thresholds.get("obstruction_landmark_threshold", 0.3)
        logger.debug("Umbrales actualizados")

    async def process(self, frame: np.ndarray) -> DetectionResult:
        if self._landmark_detector is None:
            await self.load_model()

        landmarks_result = await self._landmark_detector.process(frame)
        now = time.monotonic()

        if landmarks_result is None:
            logger.debug("Detector: No landmarks detected")
            face_present = False
            fov_ok = False
            obstructed = True
        else:
            face_present = True
            fov_ok = self._check_fov(landmarks_result)
            obstructed = self._check_obstruction(landmarks_result)

            logger.debug(f"Detector: face_present={face_present}, fov_ok={fov_ok}, obstructed={obstructed}, bbox={landmarks_result.bbox}")

        # Gestión de timer de obstrucción
        problem_now = not face_present or not fov_ok or obstructed
        events = []

        if problem_now:
            if self._obstruction_start_time is None:
                # Primera vez que detectamos problema
                self._obstruction_start_time = now
                self._last_alert_time = now
                self._alert_stage = 0
                logger.info("Obstrucción iniciada, timer started")
            
            elapsed = now - self._obstruction_start_time
            
            # Escalamiento: 5s -> primera alerta, 20s -> segunda alerta
            if self._alert_stage == 0 and elapsed >= 5.0:
                events.append({
                    "alert_code": "AS-09",
                    "event_type_id": "EV-SYS-02",
                    "message": "Campo visual obstruido (5s)",
                })
                self._alert_stage = 1
                self._last_alert_time = now
                logger.info(f"AS-09 escalamiento 1: {elapsed:.1f}s obstruido")
            elif self._alert_stage == 1 and elapsed >= 20.0:
                events.append({
                    "alert_code": "AS-09",
                    "event_type_id": "EV-SYS-02",
                    "message": "Campo visual obstruido (20s)",
                })
                self._alert_stage = 2
                self._last_alert_time = now
                logger.info(f"AS-09 escalamiento 2: {elapsed:.1f}s obstruido")
        else:
            # Todo OK - resetear timer
            if self._obstruction_start_time is not None:
                logger.info(f"Obstrucción resuelta tras {now - self._obstruction_start_time:.1f}s")
            self._obstruction_start_time = None
            self._last_alert_time = None
            self._alert_stage = 0

        # Actualizar estado previo para compatibilidad
        self._prev_face_present = face_present
        self._prev_fov_ok = fov_ok
        self._prev_obstructed = obstructed

        return DetectionResult(
            events=events,
            face_present=face_present,
            fov_ok=fov_ok and not obstructed,
            landmarks=landmarks_result,
        )

    async def detect_face_only(self, frame: np.ndarray) -> bool:
        if self._landmark_detector is None:
            await self.load_model()
        result = await self._landmark_detector.process(frame)
        return result is not None

    def _check_fov(self, landmarks: FaceLandmarks) -> bool:
        h, w = landmarks.image_shape
        _, _, bw, bh = landmarks.bbox
        face_area_ratio = (bw * bh) / (w * h)
        logger.info(f"FOV check: frame={w}x{h}, bbox={bw}x{bh}, ratio={face_area_ratio:.4f}, min={self._fov_min_face_ratio}, max={self._fov_max_face_ratio}, ok={self._fov_min_face_ratio <= face_area_ratio <= self._fov_max_face_ratio}")
        return self._fov_min_face_ratio <= face_area_ratio <= self._fov_max_face_ratio

    def _check_obstruction(self, landmarks: FaceLandmarks) -> bool:
        key_pts = get_key_points(landmarks.landmarks)
        visible_count = 0
        total_count = 0

        for region_pts in key_pts.values():
            total_count += len(region_pts)
            z_vals = region_pts[:, 2]
            visible_count += np.sum(z_vals < self._obstruction_landmark_threshold)

        visibility_ratio = visible_count / total_count if total_count > 0 else 0
        obstructed = visibility_ratio < 0.5
        logger.info(f"Obstruction check: visibility_ratio={visibility_ratio:.3f}, obstructed={obstructed}")
        return obstructed