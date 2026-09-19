"""Detección de cinturón — HU-DEVICE-001 AC-004.

  EV-CIN-01 no visible >10s desde inicio   -> AS-07 intermitente
  EV-CIN-02 mal colocado >10s              -> AS-07 intermitente

Supuesto HW documentado (riesgo conocido): la cámara frontal a rostro con
FOV estrecho NO ve torso/cinturón. Para no generar falsos AS-07, el detector
tiene 3 estados por frame: True / False / None (no evaluable).

- Sin modelo (`SeatbeltVisionAdapter` sin `.onnx`): `detect()` retorna
  (None, None, 0.0) -> `update()` NO emite (degradado silencioso).
- Con modelo o inyección de test (`inject()`): evalúa temporizadores 10s.
- Intermitencia: tras el primer disparo, re-emite cada `belt_repeat_sec`
  (default 5s) mientras la condición siga activa (el manager además usa el
  `sound_pattern AS-07 loop:true`).

`assess_fov_for_belt()` heurística sin modelo: si el rostro ocupa >40% del
frame o está centrado arriba sin torso visible, marca no-evaluable.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

import numpy as np

from app.analysis.severity import build_event_dict


class SeatbeltVisionAdapter:
    """Stub con interfaz lista para ONNX/YOLO torso+cinturón (IT4).

    `detect(frame)` -> (visible|None, correct|None, confidence).
    Hoy sin modelo: (None, None, 0.0). Usar `inject()` en tests/HIL.
    """

    def __init__(self, confidence_min: float = 0.7, model_path: str | None = None):
        self.confidence_min = confidence_min
        self.model_path = model_path
        self._model = None
        self._forced: Optional[tuple[Optional[bool], Optional[bool]]] = None

    def inject(self, visible: Optional[bool], correct: Optional[bool] = None) -> None:
        self._forced = (visible, correct)

    def detect(self, frame: np.ndarray | None) -> tuple[Optional[bool], Optional[bool], float]:
        if self._forced is not None:
            v, c = self._forced
            return v, c, (0.9 if v is not None else 0.0)
        return None, None, 0.0

    @property
    def available(self) -> bool:
        return self._model is not None or self._forced is not None


def assess_fov_for_belt(face_bbox: tuple | None, frame_shape: tuple[int, ...] | None) -> bool:
    """True si el encuadre permite evaluar cinturón (hay torso visible).

    Heurística: si no hay bbox/frame -> False. Si el rostro ocupa >40% del
    alto del frame (primer plano) -> False (no se ve torso). En otro caso
    True (FOV amplio, posible torso abajo).
    """
    if not face_bbox or not frame_shape:
        return False
    try:
        h = float(frame_shape[0])
        _, _, _, bh = face_bbox
        if h <= 0:
            return False
        return (float(bh) / h) < 0.40
    except Exception:
        return False


class SeatbeltDetector:
    def __init__(self, thresholds: dict[str, Any] | None = None,
                 now_fn: Callable[[], float] | None = None):
        self.thresholds = dict(thresholds or {})
        self._now = now_fn or time.monotonic
        self.vision = SeatbeltVisionAdapter(
            float(self.thresholds.get("belt_detection_confidence", 0.7)))
        self.update_thresholds(self.thresholds)
        self._absent_start: Optional[float] = None
        self._bad_start: Optional[float] = None
        self._last_emit: dict[str, float] = {}

    def update_thresholds(self, th: dict[str, Any]) -> None:
        self.thresholds = dict(th or {})
        t = self.thresholds
        self.enabled = str(t.get("belt_enabled", False)).lower() in ("1", "true", "yes", "on") \
            if not isinstance(t.get("belt_enabled", False), bool) else bool(t.get("belt_enabled", False))
        self.no_det_sec = float(t.get("belt_no_detection_sec", 10.0))
        self.bad_pos_sec = float(t.get("belt_bad_position_sec", 10.0))
        self.repeat_sec = float(t.get("belt_repeat_sec", 5.0))
        self.vision.confidence_min = float(t.get("belt_detection_confidence", 0.7))

    @property
    def is_enabled(self) -> bool:
        return bool(getattr(self, "enabled", False))

    def update(self, visible: Optional[bool], correct: Optional[bool] = None,
               now: float | None = None) -> list[dict]:
        """Tri-estado: None = no evaluable (sin modelo / sin torso) -> [].

        Si `belt_enabled=false` (default, pendiente HW): siempre [] aunque
        llegue `visible=False` — desactivado mientras se mejora.
        """
        if not getattr(self, "enabled", False):
            return []
        now = self._now() if now is None else float(now)
        events: list[dict] = []
        if visible is None:
            self._absent_start = None
            self._bad_start = None
            return events
        # --- EV-CIN-01: no visible
        if visible is False:
            if self._absent_start is None:
                self._absent_start = now
            dur = now - self._absent_start
            if dur >= self.no_det_sec and self._due("EV-CIN-01", now):
                events.append(build_event_dict("EV-CIN-01", f"Cinturón no detectado {dur:.0f}s",
                                               {"duration_sec": round(dur, 1)}))
            self._bad_start = None
        else:
            self._absent_start = None
            # --- EV-CIN-02: visible pero mal colocado
            if correct is False:
                if self._bad_start is None:
                    self._bad_start = now
                dur = now - self._bad_start
                if dur >= self.bad_pos_sec and self._due("EV-CIN-02", now):
                    events.append(build_event_dict("EV-CIN-02", f"Cinturón mal colocado {dur:.0f}s",
                                                   {"duration_sec": round(dur, 1)}))
            else:
                self._bad_start = None
        return events

    def _due(self, ev: str, now: float) -> bool:
        last = self._last_emit.get(ev, -1e9)
        if (now - last) >= self.repeat_sec:
            self._last_emit[ev] = now
            return True
        return False

    def reset(self) -> None:
        self._absent_start = None
        self._bad_start = None
        self._last_emit.clear()
