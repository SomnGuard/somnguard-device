"""Pipeline de visión edge — HU-DEVICE-001 AC-005/AC-006.

Orquesta en un solo `process(frame)`:
  captura -> downscale -> landmarks -> FOV/obstrucción (Detector existente)
  -> somnolencia (EAR/MAR/cabeceo) -> distracción (gaze/phone/movimiento)
  -> cinturón (tri-estado, sin falsos positivos) -> eventos + métricas.

Presupuestos normativos:
  RNF-1.2 captura->validación <=2s  (`frame_budget_sec`)
  RNF-1.3 confirmación->alarma <=1s (el manager encola y suena en background;
        aquí se mide `validated_at` para que el manager lo cumpla).

Fallback multi-modelo (AC-006): cada sub-modelo se carga independiente;
si falla, `models_status[name]=error` y se emite EV-SYS-02/AS-09 con
cooldown 30s (mismo patrón que HU-DEVICE-002 para FaceLandmarker). Sin
rostro/obstrucción el resto se salta (ahorra CPU en RPi).

Uso:
    pipe = VisionPipeline(thresholds)
    await pipe.load_models()          # en init (manager._init_detector)
    result = await pipe.process(frame, capture_time=t0)
    result.events, result.metrics
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    events: list[dict] = field(default_factory=list)
    face_present: bool = False
    fov_ok: bool = False
    metrics: dict = field(default_factory=dict)


class VisionPipeline:
    def __init__(self, thresholds: dict[str, Any] | None = None,
                 max_width: int = 640):
        self.thresholds = dict(thresholds or {})
        self.max_width = max_width
        self.frame_budget = float(self.thresholds.get("frame_budget_sec", 2.0))
        self.models_status: dict[str, str] = {"face": "pending", "phone": "unavailable",
                                              "belt": "unavailable"}
        self._skip_next = 0
        self._last_as09 = -1e9
        # Sub-detectores (import diferido para no romper si falta cv2/mp).
        from app.analysis.detector import Detector
        from app.analysis.somnolence import SomnolenceDetector
        from app.analysis.distraction import DistractionDetector
        from app.analysis.seatbelt import SeatbeltDetector, assess_fov_for_belt
        self._assess_fov_for_belt = assess_fov_for_belt
        self.obstruction = Detector(self.thresholds)
        self.somnolence = SomnolenceDetector(self.thresholds)
        self.distraction = DistractionDetector(self.thresholds)
        self.seatbelt = SeatbeltDetector(self.thresholds)
        self.last_latency_sec = 0.0

    async def load_models(self) -> None:
        """Carga cada modelo por separado; el fallo de uno no tumba el resto."""
        try:
            await self.obstruction.load_model()
            self.models_status["face"] = "ok"
        except Exception as e:
            self.models_status["face"] = f"error: {e}"
            logger.error("Pipeline: modelo rostro no disponible (%s)", e)
        # Teléfono: ObjectDetector real si el .tflite existe, si no stub seguro.
        try:
            loop = asyncio.get_running_loop()
            ok = await loop.run_in_executor(None, self.distraction.phone.load)
            self.models_status["phone"] = "ok" if ok else "stub-sin-modelo"
            if not ok:
                logger.warning("Pipeline: sin modelo teléfono (%s); EV-DIS-01/02 solo por inject/HIL",
                               self.distraction.phone._load_error)
        except Exception as e:
            self.models_status["phone"] = f"error: {e}"
        # Cinturón: desactivado por flag hasta HW (no se intenta cargar).
        if not self.seatbelt.is_enabled:
            self.models_status["belt"] = "disabled-by-config"
        else:
            self.models_status["belt"] = "stub" if not self.seatbelt.vision.available else "ok"
        logger.info("Pipeline modelos: %s", self.models_status)

    def update_thresholds(self, th: dict[str, Any]) -> None:
        self.thresholds = dict(th or {})
        self.frame_budget = float(self.thresholds.get("frame_budget_sec", 2.0))
        for sub in (self.obstruction, self.somnolence, self.distraction, self.seatbelt):
            try:
                sub.update_thresholds(self.thresholds)
            except Exception:
                pass

    async def process(self, frame: np.ndarray | None,
                      capture_time: float | None = None) -> PipelineResult:
        t_start = time.monotonic()
        capture_time = t_start if capture_time is None else capture_time
        if frame is None or getattr(frame, "size", 0) == 0:
            return PipelineResult(events=[], metrics=self._metrics(capture_time, t_start, skipped=False))
        if self._skip_next > 0:
            self._skip_next -= 1
            return PipelineResult(events=[], metrics=self._metrics(capture_time, t_start, skipped=True))
        small, _scale = self._downscale(frame)
        events: list[dict] = []
        # 1) landmarks + FOV/obstrucción (puede emitir AS-09 5s/20s).
        try:
            obs = await self.obstruction.process(small)
            events += obs.events
            face_present, fov_ok, lm = obs.face_present, obs.fov_ok, obs.landmarks
        except Exception as e:
            logger.warning("Pipeline obstrucción falló: %s", e)
            events += self._fallback_as09(t_start, f"visión: {e}")
            return PipelineResult(events=events, metrics=self._metrics(capture_time, t_start, error=True))
        if not face_present or lm is None:
            return PipelineResult(events=events, face_present=face_present, fov_ok=fov_ok,
                                  metrics=self._metrics(capture_time, t_start))
        # 2) somnolencia desde landmarks.
        try:
            events += self.somnolence.process_landmarks(lm, t_start)
        except Exception as e:
            logger.debug("Somnolencia falló: %s", e)
        # 3) distracción: solo con FOV válido. Con cara parcial el pose es
        # ruidoso y el gaze/phone generan falsos positivos; en ese caso ya
        # hay eventos de obstrucción/FOV del paso 1.
        if fov_ok:
            try:
                gaze_off, _yaw, _pitch = self.distraction.gaze.from_landmarks(lm)
                phone_present, _conf = self.distraction.phone.detect(small)
                moving = self.distraction.movement_est.update(small)
                events += self.distraction.update(phone_present, gaze_off, moving, t_start,
                                                  gaze_yaw=_yaw, gaze_pitch=_pitch)
            except Exception as e:
                logger.debug("Distracción falló: %s", e)
        else:
            try:
                self.distraction.gaze.reset()
            except Exception:
                pass
        # 4) cinturón: solo si evaluable (modelo inyectado o FOV amplio).
        try:
            vis, corr, _c = self.seatbelt.vision.detect(small)
            if vis is None and self.seatbelt.vision.available is False:
                pass  # degradado silencioso: sin modelo no hay falsos AS-07
            else:
                events += self.seatbelt.update(vis, corr, t_start)
        except Exception as e:
            logger.debug("Cinturón falló: %s", e)
        result = PipelineResult(events=events, face_present=face_present, fov_ok=fov_ok,
                                metrics=self._metrics(capture_time, t_start))
        # Frame-skipping adaptativo: si excedimos presupuesto, saltar 1.
        if result.metrics.get("process_sec", 0) > self.frame_budget:
            self._skip_next = 1
            logger.warning("Pipeline %.2fs > presupuesto %.1fs: skip-next activado",
                           result.metrics["process_sec"], self.frame_budget)
        self.last_latency_sec = result.metrics.get("process_sec", 0.0)
        return result

    # -- internos ---------------------------------------------------------
    def _downscale(self, frame: np.ndarray) -> tuple[np.ndarray, float]:
        try:
            h, w = frame.shape[:2]
            if w <= self.max_width:
                return frame, 1.0
            scale = self.max_width / float(w)
            import cv2
            small = cv2.resize(frame, (self.max_width, int(h * scale)))
            return small, scale
        except Exception:
            return frame, 1.0

    def _metrics(self, capture: float, start: float, skipped: bool = False,
                 error: bool = False) -> dict:
        validated = time.monotonic()
        return {
            "capture_to_validated_sec": round(validated - capture, 4),
            "process_sec": round(validated - start, 4),
            "budget_sec": self.frame_budget,
            "within_budget": (validated - start) <= self.frame_budget,
            "skipped": skipped,
            "error": error,
            "validated_at": validated,
        }

    def _fallback_as09(self, now: float, msg: str) -> list[dict]:
        if (now - self._last_as09) < 30.0:
            return []
        self._last_as09 = now
        from app.analysis.severity import build_event_dict
        return [build_event_dict("EV-SYS-02", msg)]
