"""Detección de distracción — HU-DEVICE-001 AC-003.

  EV-DIS-01 teléfono en mano/rostro >2s              -> AS-05
  EV-DIS-02 uso prolongado >5s (repite c/3s)         -> AS-05
  EV-DIS-03 mirada fuera eje >3s                     -> AS-06
  EV-DIS-04 mirada prolongada >5s (repite c/3s)      -> AS-06
  EV-DIS-05 movimiento anómalo >3s                   -> AS-05

Funcional sin modelo pesado: `update()` recibe booleanos ya decididos
(testeable). Los adaptadores reales viven aquí mismo:

- `GazeEstimator`: yaw/pitch (de ear_mar.estimate_head_pose) -> off-axis.
- `PhoneDetector`: stub con interfaz lista para YOLO/ONNX (hoy heurística
  desactivada -> siempre (False, 0.0); el cableado real llega con el modelo).
- `MovementEstimator`: diff de frames downscaleado (sin dependencias nuevas).

El orquestador `DistractionDetector` gestiona temporizadores monotónicos,
latch por episodio y repetición c/3s para prolongados.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

import numpy as np

from app.analysis.ear_mar import estimate_head_pose
from app.analysis.landmarks import FaceLandmarks
from app.analysis.severity import build_event_dict


def is_gaze_off_axis(yaw_deg: float, pitch_deg: float = 0.0, threshold_deg: float = 30.0) -> bool:
    return abs(yaw_deg) >= threshold_deg or abs(pitch_deg) >= threshold_deg


class GazeEstimator:
    """Yaw/pitch suavizado con histéresis + auto-cero (anti falsos positivos).

    Dos fuentes de FP cubiertas:
    - Jitter frame a frame: EMA (alpha default 0.35) + histéresis
      (entra en 30°, sale en 22°); un spike aislado no latcha `off`.
    - Sesgo sostenido (cámara montada de lado, bias sistemático de solvePnP):
      durante `calib_samples` iniciales (default 90, se asume conductor al
      frente tras el chequeo inicial de rostro) se acumula la mediana como
      neutro y se resta. Más `yaw_offset`/`pitch_offset` manuales para un
      montaje lateral conocido. Tras calibrar, `off` mide desviación
      *relativa* al neutro, no absoluta.
    """

    def __init__(self, deviation_deg: float = 30.0, exit_deg: float | None = None,
                 smooth_alpha: float = 0.35, calib_samples: int = 60,
                 yaw_offset: float = 0.0, pitch_offset: float = 0.0,
                 yaw_limit: float = 80.0, pitch_limit: float = 65.0,
                 force_off_sec: float = 1.0):
        self.deviation_deg = deviation_deg
        self.exit_deg = exit_deg if exit_deg is not None else deviation_deg - 8.0
        self.smooth_alpha = min(1.0, max(0.05, smooth_alpha))
        self.calib_samples = max(0, int(calib_samples))
        self.yaw_offset = float(yaw_offset)
        self.pitch_offset = float(pitch_offset)
        self.yaw_limit = float(yaw_limit)
        self.pitch_limit = float(pitch_limit)
        # Zona ciega extrema: con rostro tracked pero pose rechazada de forma
        # sostenida (giro total, solvePnP en flip), se asume desvío y se
        # fuerza off. Sin esto, mirar muy al costado no latchaba nunca porque
        # los primeros frames ya eran flips (off nunca se activaba).
        self.force_off_sec = max(0.5, float(force_off_sec))
        self._syaw: float | None = None
        self._spitch: float | None = None
        self._off = False
        self._reject_since: float | None = None
        self._calib_yaw: list[float] = []
        self._calib_pitch: list[float] = []
        self._calib_attempts = 0
        self._base_yaw = 0.0
        self._base_pitch = 0.0
        self._calibrated = self.calib_samples <= 0

    @property
    def calibrated(self) -> bool:
        return self._calibrated

    def update_raw(self, yaw_deg: float, pitch_deg: float = 0.0,
                   now: float | None = None) -> bool:
        from app.analysis.ear_mar import wrap_deg
        import math
        import time as _time

        now = _time.monotonic() if now is None else float(now)
        try:
            yaw_raw = float(yaw_deg)
            pitch_raw = float(pitch_deg)
            if not (math.isfinite(yaw_raw) and math.isfinite(pitch_raw)):
                return self._rejected(now)
        except (TypeError, ValueError):
            return self._rejected(now)
        yaw = yaw_raw - self.yaw_offset
        pitch = pitch_raw - self.pitch_offset
        if not self._calibrated:
            self._calib_attempts += 1
            self._calib_yaw.append(yaw)
            self._calib_pitch.append(pitch)
            if (len(self._calib_yaw) >= self.calib_samples
                    or self._calib_attempts >= max(1, self.calib_samples * 3)):
                import statistics

                if self._calib_yaw:
                    self._base_yaw = float(statistics.median(self._calib_yaw))
                    self._base_pitch = float(statistics.median(self._calib_pitch))
                self._calibrated = True
                self._calib_yaw.clear()
                self._calib_pitch.clear()
            # Durante calibración no se alerta (neutro aún desconocido).
            # Al completarla se reinicia el suavizado: post-calibración guarda
            # DELTAS y sembrar la EMA con crudos fabricaría un desvío fantasma
            # (~110° por el sesgo de pitch) en los primeros frames.
            if self._calibrated:
                self._syaw, self._spitch = None, None
            else:
                self._syaw, self._spitch = yaw, pitch
            return False
        # Gate en DELTAS relativos (con wrap), no en valores absolutos: el
        # solvePnP trae sesgo ~±180° en pitch (frente lee +170°) y flips en
        # giros; el gate absoluto descartaba el 100% de los frames de campo.
        dyaw = wrap_deg(yaw - self._base_yaw)
        dpitch = wrap_deg(pitch - self._base_pitch)
        if abs(dyaw) > self.yaw_limit or abs(dpitch) > self.pitch_limit:
            # Frame degenerado (flip): se ignora sin tocar el suavizado;
            # se mantiene el estado previo (o se fuerza off si la zona
            # ciega se sostiene: giro extremo real, no glitch).
            return self._rejected(now)
        self._reject_since = None
        a = self.smooth_alpha
        self._syaw = dyaw if self._syaw is None else (1 - a) * self._syaw + a * dyaw
        self._spitch = dpitch if self._spitch is None else (1 - a) * self._spitch + a * dpitch
        rel_yaw = self._syaw
        rel_pitch = self._spitch
        mag = max(abs(rel_yaw), abs(rel_pitch))
        if not self._off and mag >= self.deviation_deg:
            self._off = True
        elif self._off and mag < self.exit_deg:
            self._off = False
        return self._off

    def _rejected(self, now: float) -> bool:
        if self._reject_since is None:
            self._reject_since = now
        elif (now - self._reject_since) >= self.force_off_sec:
            self._off = True
        return self._off

    def relative(self) -> tuple[float, float]:
        """Desviación relativa actual respecto al neutro. (0,0) si no hay datos."""
        sy = self._syaw if self._syaw is not None else 0.0
        sp = self._spitch if self._spitch is not None else 0.0
        if self._calibrated:
            # Post-calibración el suavizado ya guarda deltas.
            return sy, sp
        return sy - self._base_yaw, sp - self._base_pitch

    def from_landmarks(self, lm: FaceLandmarks | None,
                       now: float | None = None) -> tuple[bool, float, float]:
        """Retorna (off_axis, yaw, pitch). Sin rostro -> pausa (conserva neutro)."""
        if lm is None:
            # Pausa transitoria: se limpia el suavizado pero se conserva el
            # neutro calibrado (una pérdida breve de rostro no debe obligar
            # a recalibrar 60 muestras).
            self._syaw = None
            self._spitch = None
            self._off = False
            self._reject_since = None
            return False, 0.0, 0.0
        try:
            pitch, yaw, _ = estimate_head_pose(lm.landmarks, lm.image_shape)
            return self.update_raw(yaw, pitch, now), yaw, pitch
        except Exception:
            return False, 0.0, 0.0

    def reset(self) -> None:
        self._syaw = None
        self._spitch = None
        self._off = False
        self._reject_since = None
        self._calib_yaw.clear()
        self._calib_pitch.clear()
        self._calib_attempts = 0
        self._base_yaw = 0.0
        self._base_pitch = 0.0
        self._calibrated = self.calib_samples <= 0

    def reset_transient(self) -> None:
        """Pausa breve (FOV inválido / rostro perdido un frame).

        Limpia suavizado y estado off, pero CONSERVA neutro calibrado y
        progreso de calibración. El `reset()` completo solo se usa al
        cambio de conductor / reset manual; llamarlo en cada FOV malo
        impedía calibrar jamás (loop infinito sin alertas).
        """
        self._syaw = None
        self._spitch = None
        self._off = False
        self._reject_since = None


class PhoneDetector:
    """Detector de teléfono v1 con MediaPipe ObjectDetector (sin nuevas deps pip).

    - Si `models/phone_detector.tflite` existe (ver `scripts/download_models.py`
      y `models/README.md`): detecta clase `cell phone`/`mobile phone` con
      `confidence >= confidence_min` -> (True, conf).
    - Sin modelo: degradado seguro (False, 0.0), sin falsos positivos.
    - `inject_present()` fuerza resultado (tests / HIL sin modelo).
    - `load()` intenta abrir el modelo; fallo -> stub (AC-006, no tumba pipeline).
    """

    PHONE_LABELS = frozenset({"cell phone", "mobile phone", "cell_phone", "phone"})

    def __init__(self, confidence_min: float = 0.7, model_path: str | None = None):
        self.confidence_min = confidence_min
        self.model_path = model_path or "models/phone_detector.tflite"
        self._model = None
        self._forced: Optional[bool] = None
        self._load_error: Optional[str] = None

    def inject_present(self, present: bool | None) -> None:
        self._forced = present

    def load(self) -> bool:
        if self._model is not None:
            return True
        try:
            import os

            if not os.path.exists(self.model_path):
                self._load_error = f"modelo no encontrado: {self.model_path}"
                return False
            import mediapipe as mp

            opts = mp.tasks.vision.ObjectDetectorOptions(
                base_options=mp.tasks.BaseOptions(model_asset_path=self.model_path),
                max_results=5,
                score_threshold=self.confidence_min,
                running_mode=mp.tasks.vision.RunningMode.IMAGE,
            )
            self._model = mp.tasks.vision.ObjectDetector.create_from_options(opts)
            return True
        except Exception as e:
            self._load_error = str(e)
            self._model = None
            return False

    def detect(self, frame: np.ndarray | None) -> tuple[bool, float]:
        if self._forced is not None:
            return (self._forced, 0.9 if self._forced else 0.0)
        if frame is None or getattr(frame, "size", 0) == 0:
            return False, 0.0
        if self._model is None and not self.load():
            return False, 0.0
        try:
            import cv2
            import mediapipe as mp

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            res = self._model.detect(mp_image)
            best = 0.0
            for det in getattr(res, "detections", []) or []:
                for cat in getattr(det, "categories", []) or []:
                    name = (getattr(cat, "category_name", "") or "").strip().lower()
                    score = float(getattr(cat, "score", 0.0) or 0.0)
                    if name in self.PHONE_LABELS and score >= self.confidence_min:
                        best = max(best, score)
            return (best > 0.0, best)
        except Exception:
            return False, 0.0

    def close(self) -> None:
        try:
            if self._model is not None and hasattr(self._model, "close"):
                self._model.close()
        except Exception:
            pass
        self._model = None

    @property
    def available(self) -> bool:
        return self._model is not None or self._forced is not None


class MovementEstimator:
    """Movimiento anómalo por diferencia de frames (brusco/sostenido)."""

    def __init__(self, diff_threshold: float = 25.0):
        self.diff_threshold = float(diff_threshold)
        self._prev: Optional[np.ndarray] = None

    def update(self, frame: np.ndarray | None) -> bool:
        if frame is None or frame.size == 0:
            return False
        try:
            small = frame[::8, ::8]
            gray = small.mean(axis=2) if small.ndim == 3 else small.astype(float)
        except Exception:
            return False
        if self._prev is None or self._prev.shape != gray.shape:
            self._prev = gray.astype(float)
            return False
        diff = float(np.abs(gray.astype(float) - self._prev).mean())
        self._prev = gray.astype(float)
        return diff > self.diff_threshold

    def reset(self) -> None:
        self._prev = None


class DistractionDetector:
    def __init__(self, thresholds: dict[str, Any] | None = None,
                 now_fn: Callable[[], float] | None = None):
        self.thresholds = dict(thresholds or {})
        self._now = now_fn or time.monotonic
        self.phone = PhoneDetector(
            float(self.thresholds.get("detection_confidence_min", 0.7)),
            model_path=self.thresholds.get("phone_model_path", "models/phone_detector.tflite"),
        )
        self.gaze = GazeEstimator(
            float(self.thresholds.get("gaze_deviation_deg",
                                      self.thresholds.get("gaze_deviation_threshold", 30))),
            calib_samples=int(self.thresholds.get("gaze_calib_samples", 60)),
            yaw_offset=float(self.thresholds.get("gaze_yaw_offset", 0.0)),
            pitch_offset=float(self.thresholds.get("gaze_pitch_offset", 0.0)),
            yaw_limit=float(self.thresholds.get("pose_yaw_limit_deg", 80.0)),
            pitch_limit=float(self.thresholds.get("pose_pitch_limit_deg", 65.0)),
        )
        self.movement_est = MovementEstimator()
        self.update_thresholds(self.thresholds)
        self._phone_start: Optional[float] = None
        self._gaze_start: Optional[float] = None
        self._move_start: Optional[float] = None
        self._phone_gap_start: Optional[float] = None
        self._gaze_gap_start: Optional[float] = None
        self._phone_repeat_count = 0
        self._gaze_repeat_count = 0
        self._fired: set[str] = set()
        self._last_repeat: dict[str, float] = {}

    def update_thresholds(self, th: dict[str, Any]) -> None:
        self.thresholds = dict(th or {})
        t = self.thresholds
        self.phone_dur = float(t.get("phone_duration_sec", 2.0))
        self.phone_prolonged = float(t.get("phone_prolonged_sec", 5.0))
        # NHTSA/Klauer: >2s eyes-off-road ya multiplica el riesgo (x3.8);
        # >5s lo dispara (x8.9). DIS-03 a 2s, no 3s.
        self.gaze_dur = float(t.get("gaze_duration_sec", 2.0))
        self.gaze_prolonged = float(t.get("gaze_prolonged_sec", 5.0))
        self.move_dur = float(t.get("movement_duration_sec", 3.0))
        self.repeat_sec = float(t.get("phone_repeat_sec", t.get("gaze_repeat_sec", 3.0)))
        self.phone.confidence_min = float(t.get("detection_confidence_min", 0.7))
        if t.get("phone_model_path"):
            self.phone.model_path = str(t["phone_model_path"])
        self.gaze.deviation_deg = float(t.get("gaze_deviation_deg", t.get("gaze_deviation_threshold", 30)))
        self.gaze.exit_deg = float(t.get("gaze_exit_deg", self.gaze.deviation_deg - 8.0))
        self.gaze.smooth_alpha = min(1.0, max(0.05, float(t.get("gaze_smooth_alpha", 0.35))))
        self.gaze.calib_samples = max(0, int(t.get("gaze_calib_samples", 60)))
        self.gaze.yaw_offset = float(t.get("gaze_yaw_offset", 0.0))
        self.gaze.pitch_offset = float(t.get("gaze_pitch_offset", 0.0))
        self.gaze.yaw_limit = float(t.get("pose_yaw_limit_deg", 80.0))
        self.gaze.pitch_limit = float(t.get("pose_pitch_limit_deg", 65.0))
        try:
            self.gaze.force_off_sec = max(0.5, float(t.get("gaze_force_off_sec", 1.0)))
        except (TypeError, ValueError):
            self.gaze.force_off_sec = 1.0
        try:
            self.movement_est.diff_threshold = float(t.get("movement_diff_threshold", 25.0))
        except (TypeError, ValueError):
            self.movement_est.diff_threshold = 25.0
        # Gracia: un frame suelto en eje no corta un episodio real
        # (parpadeo del estimador); episodios falsos sostenidos siguen
        # requiriendo 3s/5s completos para disparar.
        self.track_grace_sec = float(t.get("track_grace_sec", 0.4))
        # Tope anti-spam: un episodio (p.ej. sesgo de montaje no calibrado)
        # no debe sonar cada 3s eternamente; tras N repeticiones se silencia
        # hasta que la mirada vuelva al eje.
        self.max_repeats = max(1, int(t.get("prolonged_max_repeats", 5)))

    def update(self, phone: bool, gaze_off: bool, moving: bool,
               now: float | None = None, gaze_yaw: float | None = None,
               gaze_pitch: float | None = None) -> list[dict]:
        now = self._now() if now is None else float(now)
        events: list[dict] = []
        events += self._track(phone, "_phone", now, self.phone_dur, self.phone_prolonged,
                              "EV-DIS-01", "EV-DIS-02",
                              lambda d: f"Teléfono en mano {d:.1f}s",
                              lambda d: f"Teléfono prolongado {d:.1f}s")
        gaze_tag = ""
        if gaze_yaw is not None or gaze_pitch is not None:
            gaze_tag = f" (yaw {gaze_yaw or 0.0:+.0f}° pitch {gaze_pitch or 0.0:+.0f}°)"
        events += self._track(gaze_off, "_gaze", now, self.gaze_dur, self.gaze_prolonged,
                              "EV-DIS-03", "EV-DIS-04",
                              lambda d: f"Mirada fuera de vía {d:.1f}s{gaze_tag}",
                              lambda d: f"Mirada prolongada fuera {d:.1f}s{gaze_tag}",
                              extra={"yaw_deg": gaze_yaw, "pitch_deg": gaze_pitch})
        # Movimiento anómalo: solo base (sin versión prolongada en norma).
        if moving:
            if self._move_start is None:
                self._move_start = now
            dur = now - self._move_start
            if dur >= self.move_dur and "EV-DIS-05" not in self._fired:
                self._fired.add("EV-DIS-05")
                events.append(build_event_dict("EV-DIS-05", f"Movimiento anómalo {dur:.1f}s",
                                               {"duration_sec": round(dur, 2)}))
        else:
            self._move_start = None
            self._fired.discard("EV-DIS-05")
        return events

    def _track(self, active: bool, slot: str, now: float, base_sec: float, prol_sec: float,
               base_ev: str, prol_ev: str, base_msg, prol_msg,
               extra: dict | None = None) -> list[dict]:
        start_attr = slot + "_start"
        gap_attr = slot + "_gap_start"
        count_attr = slot + "_repeat_count"
        start = getattr(self, start_attr)
        gap_start = getattr(self, gap_attr, None)
        grace = getattr(self, "track_grace_sec", 0.4)
        max_rep = getattr(self, "max_repeats", 5)
        out: list[dict] = []
        if active:
            setattr(self, gap_attr, None)
            if start is None:
                setattr(self, start_attr, now)
                setattr(self, count_attr, 0)
                start = now
            dur = now - start
            payload = {"duration_sec": round(dur, 2)}
            if extra:
                payload.update({k: v for k, v in extra.items() if v is not None})
            if dur >= base_sec and base_ev not in self._fired:
                self._fired.add(base_ev)
                out.append(build_event_dict(base_ev, base_msg(dur), dict(payload)))
            if dur >= prol_sec:
                count = getattr(self, count_attr, 0) or 0
                last = self._last_repeat.get(prol_ev, -1e9)
                first_prol = prol_ev not in self._fired
                if count < max_rep and (first_prol or (now - last) >= self.repeat_sec):
                    self._fired.add(prol_ev)
                    self._last_repeat[prol_ev] = now
                    setattr(self, count_attr, count + 1)
                    out.append(build_event_dict(prol_ev, prol_msg(dur), dict(payload)))
        else:
            # Gracia: hueco breve no resetea el episodio en curso.
            if start is not None:
                if gap_start is None:
                    setattr(self, gap_attr, now)
                elif (now - gap_start) > grace:
                    setattr(self, start_attr, None)
                    setattr(self, gap_attr, None)
                    setattr(self, count_attr, 0)
                    self._fired.discard(base_ev)
                    self._fired.discard(prol_ev)
            else:
                self._fired.discard(base_ev)
                self._fired.discard(prol_ev)
        return out

    def debug_state(self, now: float | None = None) -> dict:
        """Progreso temporizado hacia EV-DIS-01..05 (sin disparar nada)."""
        now = self._now() if now is None else float(now)

        def _dur(start: float | None) -> float:
            return round(now - start, 2) if start is not None else 0.0

        try:
            rel_yaw, rel_pitch = self.gaze.relative()
        except Exception:
            rel_yaw, rel_pitch = 0.0, 0.0
        return {
            "phone_active": self._phone_start is not None,
            "phone_dur_sec": _dur(self._phone_start),
            "phone_need_sec": self.phone_dur,
            "phone_prolonged_sec": self.phone_prolonged,
            "gaze_off": bool(getattr(self.gaze, "_off", False)),
            "gaze_calibrated": bool(getattr(self.gaze, "_calibrated", True)),
            "gaze_rel_yaw": round(float(rel_yaw), 1),
            "gaze_rel_pitch": round(float(rel_pitch), 1),
            "gaze_dur_sec": _dur(self._gaze_start),
            "gaze_need_sec": self.gaze_dur,
            "gaze_prolonged_sec": self.gaze_prolonged,
            "move_active": self._move_start is not None,
            "move_dur_sec": _dur(self._move_start),
            "move_need_sec": self.move_dur,
        }

    def reset(self) -> None:
        self._phone_start = self._gaze_start = self._move_start = None
        self._phone_gap_start = self._gaze_gap_start = None
        self._phone_repeat_count = self._gaze_repeat_count = 0
        self._fired.clear()
        self._last_repeat.clear()
        self.gaze.reset()
        self.movement_est.reset()
