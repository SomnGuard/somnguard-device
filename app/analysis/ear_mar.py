"""Métricas faciales EAR / MAR / PERCLOS / head-pose — HU-DEVICE-001 AC-001.

Sin dependencias pesadas nuevas (solo numpy + opcional cv2 para solvePnP).
Todo testeable sin hardware: las funciones reciben arrays (N,2|3).
"""
from __future__ import annotations

import math
import statistics
import time
from collections import deque

import numpy as np


def _dist(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.linalg.norm(np.asarray(a, dtype=float) - np.asarray(b, dtype=float)))


def eye_aspect_ratio(eye_6pts: np.ndarray) -> float:
    """EAR = (||p2-p6|| + ||p3-p5||) / (2*||p1-p4||).

    `eye_6pts`: (6,2|3) en orden MediaPipe [esquina_ext, sup1, sup2,
    esquina_int, inf2, inf1] (p1..p6). Retorna 0.0 si degenerado.
    """
    pts = np.asarray(eye_6pts, dtype=float)
    if pts.shape[0] < 6:
        return 0.0
    p1, p2, p3, p4, p5, p6 = pts[:6, :2]
    horiz = _dist(p1, p4)
    if horiz < 1e-6:
        return 0.0
    return (_dist(p2, p6) + _dist(p3, p5)) / (2.0 * horiz)


def mouth_aspect_ratio(mouth_6pts: np.ndarray) -> float:
    """MAR con la misma fórmula genérica alto/ancho (bostezo si > umbral)."""
    pts = np.asarray(mouth_6pts, dtype=float)
    if pts.shape[0] < 6:
        return 0.0
    p1, p2, p3, p4, p5, p6 = pts[:6, :2]
    horiz = _dist(p1, p4)
    if horiz < 1e-6:
        return 0.0
    return (_dist(p2, p6) + _dist(p3, p5)) / (2.0 * horiz)


def is_eye_closed(ear: float, threshold: float = 0.2) -> bool:
    return ear < threshold


def is_yawning(mar: float, threshold: float = 0.85) -> bool:
    return mar > threshold


class PerclosTracker:
    """Proporción de tiempo con ojos cerrados en ventana deslizante."""

    def __init__(self, window_sec: float = 60.0):
        self.window_sec = max(1.0, float(window_sec))
        self._samples: deque[tuple[float, bool]] = deque()

    def update(self, closed: bool, now: float) -> float:
        self._samples.append((now, bool(closed)))
        cutoff = now - self.window_sec
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        if not self._samples:
            return 0.0
        closed_n = sum(1 for _, c in self._samples if c)
        return closed_n / len(self._samples)

    @property
    def value(self) -> float:
        if not self._samples:
            return 0.0
        closed_n = sum(1 for _, c in self._samples if c)
        return closed_n / len(self._samples)


# Modelo 3D canónico (mm aprox) para solvePnP: nariz, mentón,
# comisuras ojos, comisuras boca.
_MODEL_3D = np.array([
    (0.0, 0.0, 0.0),          # nariz
    (0.0, -63.6, -12.5),      # mentón
    (-42.0, 32.0, -26.0),     # ojo izq ext
    (42.0, 32.0, -26.0),      # ojo der ext
    (-28.0, -28.0, -24.0),    # boca izq
    (28.0, -28.0, -24.0),     # boca der
], dtype=float)


def estimate_head_pose(landmarks_468: np.ndarray, image_shape: tuple[int, int]) -> tuple[float, float, float]:
    """Estima (pitch, yaw, roll) en grados. Fallback (0,0,0) si no se puede.

    Usa 6 puntos 2D (nariz=1, mentón=152, ojos ext=33/263, boca=61/291)
    + solvePnP. Pitch>0 = cabeceo hacia abajo (útil para EV-SOM-04/05).
    Si cv2 no está disponible, usa heurística geométrica simple.
    """
    try:
        pts = np.asarray(landmarks_468, dtype=float)
        h, w = image_shape
        idx = [1, 152, 33, 263, 61, 291]
        if pts.shape[0] <= max(idx):
            return 0.0, 0.0, 0.0
        img_pts = np.array([[pts[i, 0] * w, pts[i, 1] * h] for i in idx], dtype=float)
        import cv2  # local: puede ser stub en tests
        focal = float(w)
        cam = np.array([[focal, 0, w / 2.0], [0, focal, h / 2.0], [0, 0, 1]], dtype=float)
        dist = np.zeros((4, 1), dtype=float)
        ok, rvec, _ = cv2.solvePnP(_MODEL_3D, img_pts, cam, dist, flags=cv2.SOLVEPNP_ITERATIVE)
        if not ok:
            raise ValueError("solvePnP falló")
        import math as _m
        rx, ry, rz = (float(v) for v in rvec.flatten()[:3])
        # Conversión aproximada a grados euler (suficiente para umbral >20°).
        pitch = -_m.degrees(rx)
        yaw = _m.degrees(ry)
        roll = _m.degrees(rz)
        # Normalizar a [-180, 180]
        pitch = ((pitch + 180) % 360) - 180
        yaw = ((yaw + 180) % 360) - 180
        return pitch, yaw, roll
    except Exception:
        return _heuristic_tilt(landmarks_468)


def _heuristic_tilt(landmarks_468: np.ndarray) -> tuple[float, float, float]:
    """Fallback sin cv2: inclinación por geometría nariz/ojos/boca (grados aprox)."""
    try:
        pts = np.asarray(landmarks_468, dtype=float)
        if pts.shape[0] < 300:
            return 0.0, 0.0, 0.0
        nose = pts[1, :2]
        eye_l = pts[33, :2]
        eye_r = pts[263, :2]
        mouth_l = pts[61, :2]
        mouth_r = pts[291, :2]
        eye_c = (eye_l + eye_r) / 2.0
        mouth_c = (mouth_l + mouth_r) / 2.0
        eye_w = float(np.linalg.norm(eye_l - eye_r)) + 1e-6
        # Cabeceo: la nariz baja respecto al eje ojos-boca cuando se inclina.
        axis_mid = (eye_c + mouth_c) / 2.0
        dy = float(nose[1] - axis_mid[1])
        pitch = float(np.degrees(np.arctan2(dy, eye_w)))
        # Yaw: asimetría horizontal nariz vs centro ojos.
        dx = float(nose[0] - eye_c[0])
        yaw = float(np.degrees(np.arctan2(dx, eye_w)))
        return pitch, yaw, 0.0
    except Exception:
        return 0.0, 0.0, 0.0


def head_tilt_deg(pitch: float, yaw: float = 0.0) -> float:
    """Magnitud de inclinación (para umbral >20°): combina pitch/yaw."""
    return math.sqrt(pitch * pitch + yaw * yaw)


# Límites de plausibilidad física (conductor frente a cámara).
# solvePnP con 6 puntos sufre ambigüedad flip: algún frame devuelve la
# solución "volteada" (p.ej. pitch −179°, yaw −157°) con la cara al frente.
# Más allá de estos límites los landmarks ya se degradan (ojo lejano ocluido),
# así que es evidencia inválida, no distracción.
POSE_PLAUSIBLE_YAW_DEG = 80.0
POSE_PLAUSIBLE_PITCH_DEG = 65.0


def is_head_pose_plausible(pitch: float, yaw: float,
                           yaw_limit: float = POSE_PLAUSIBLE_YAW_DEG,
                           pitch_limit: float = POSE_PLAUSIBLE_PITCH_DEG) -> bool:
    """False si la pose es físicamente imposible (flip de solvePnP)."""
    try:
        if not (math.isfinite(pitch) and math.isfinite(yaw)):
            return False
        return abs(float(yaw)) <= yaw_limit and abs(float(pitch)) <= pitch_limit
    except (TypeError, ValueError):
        return False


def wrap_deg(angle: float) -> float:
    """Diferencia angular a [-180, 180): absorbe el wrap ±180° del solvePnP."""
    try:
        return ((float(angle) + 180.0) % 360.0) - 180.0
    except (TypeError, ValueError):
        return 0.0


class TiltEstimator:
    """Inclinación de cabeza relativa al neutro frontal (auto-zero).

    Hallazgo de campo (logs 2026-09-16): el solvePnP de 6 puntos devuelve el
    pitch con un sesgo de ~±180° (cara de frente lee pitch≈+170° estable) y
    flips discretos en giros fuertes. Con umbral absoluto (>20°) y gate de
    plausibilidad absoluto, el tilt daba siempre 0: EV-SOM-04/05 y la mirada
    (mismo gate) quedaban muertos.

    Se mide desviación respecto al neutro calibrado (mediana de las primeras
    `calib_samples` muestras mirando al frente; misma filosofía que
    GazeEstimator). Deltas con wrap (170°→-170° = 0, no 340). Saltos mayores
    que los límites = flip de solvePnP: se conserva el último tilt y si el
    rechazo supera `hold_sec` se vuelve a 0 (tracker perdido).
    """

    def __init__(self, calib_samples: int = 60, smooth_alpha: float = 0.35,
                 yaw_limit: float = POSE_PLAUSIBLE_YAW_DEG,
                 pitch_limit: float = POSE_PLAUSIBLE_PITCH_DEG,
                 hold_sec: float = 3.0):
        self.calib_samples = max(0, int(calib_samples))
        self.smooth_alpha = min(1.0, max(0.05, float(smooth_alpha)))
        self.yaw_limit = float(yaw_limit)
        self.pitch_limit = float(pitch_limit)
        # Huecos de pose inválida menores que un episodio no lo cortan
        # (giro rápido que cruza la zona ciega del solvePnP).
        self.hold_sec = max(0.5, float(hold_sec))
        self._tilt = 0.0
        self._syaw: float | None = None
        self._spitch: float | None = None
        self._last_ok = 0.0
        self.reset()

    def reset(self) -> None:
        self._cal_yaw: list[float] = []
        self._cal_pitch: list[float] = []
        self._cal_attempts = 0
        self._base_yaw = 0.0
        self._base_pitch = 0.0
        self._calibrated = self.calib_samples <= 0
        self._tilt = 0.0
        self._syaw = None
        self._spitch = None
        self._last_ok = 0.0

    @property
    def calibrated(self) -> bool:
        return self._calibrated

    def relative(self) -> tuple[float, float]:
        """Desviación suavizada actual (yaw, pitch) respecto al neutro."""
        return (self._syaw if self._syaw is not None else 0.0,
                self._spitch if self._spitch is not None else 0.0)

    def neutral(self) -> tuple[float, float]:
        return self._base_yaw, self._base_pitch

    def update(self, yaw_deg: float, pitch_deg: float, now: float | None = None) -> float:
        now = time.monotonic() if now is None else float(now)
        try:
            yaw = float(yaw_deg)
            pitch = float(pitch_deg)
            finite = math.isfinite(yaw) and math.isfinite(pitch)
        except (TypeError, ValueError):
            finite = False
            yaw = pitch = 0.0
        if not finite:
            return self._hold(now)
        if not self._calibrated:
            self._cal_attempts += 1
            self._cal_yaw.append(yaw)
            self._cal_pitch.append(pitch)
            if (len(self._cal_yaw) >= self.calib_samples
                    or self._cal_attempts >= max(1, self.calib_samples * 3)):
                if self._cal_yaw:
                    self._base_yaw = float(statistics.median(self._cal_yaw))
                    self._base_pitch = float(statistics.median(self._cal_pitch))
                self._calibrated = True
                self._cal_yaw.clear()
                self._cal_pitch.clear()
            self._last_ok = now
            return 0.0
        dyaw = wrap_deg(yaw - self._base_yaw)
        dpitch = wrap_deg(pitch - self._base_pitch)
        if abs(dyaw) > self.yaw_limit or abs(dpitch) > self.pitch_limit:
            return self._hold(now)
        a = self.smooth_alpha
        self._syaw = dyaw if self._syaw is None else (1 - a) * self._syaw + a * dyaw
        self._spitch = dpitch if self._spitch is None else (1 - a) * self._spitch + a * dpitch
        self._tilt = math.hypot(self._syaw, self._spitch)
        self._last_ok = now
        return self._tilt

    def _hold(self, now: float) -> float:
        # Rechazo seguido largo = tracker perdido: decaer a 0 para no latchar
        # un tilt viejo eternamente (misma idea que la gracia de distracción).
        if self._last_ok and (now - self._last_ok) > self.hold_sec:
            self._tilt = 0.0
            self._syaw = None
            self._spitch = None
            return 0.0
        return self._tilt
