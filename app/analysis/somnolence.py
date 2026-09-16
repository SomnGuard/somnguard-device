"""Detector de somnolencia — HU-DEVICE-001 AC-001/AC-002.

Eventos (Apéndice 2 SRS):
  EV-SOM-01 parpadeo anómalo  blink>25 o <5/min en 15s        -> AS-01 LEVE
  EV-SOM-02 cierre ojos >2s                                    -> AS-02 MODERADA
  EV-SOM-03 >=2 bostezos /5min                                -> AS-02 MODERADA
  EV-SOM-04 inclinación >20° por >3s                          -> AS-03 SEVERA
  EV-SOM-05 ojos>3s + cabeceo simultáneo (microsueño)         -> AS-04 CRITICA

Diseño funcional sin hardware: `update(ear, mar, tilt_deg, now)` recibe
métricas ya calculadas (testeable con tiempo inyectado). `process_landmarks`
es el adaptador desde FaceLandmarks (EAR/MAR/pose vía ear_mar.py).

Anti-spam: cada evento tiene cooldown (default 10s, microsueño 15s) para no
inundar la cola de alertas; el reseteo de cierre/bostezo/cabeceo es por
transición (requiere reapertura/enderezado antes de re-disparar).
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Callable, Optional

from app.analysis.ear_mar import (
    PerclosTracker,
    estimate_head_pose,
    eye_aspect_ratio,
    head_tilt_deg,
    is_eye_closed,
    is_head_pose_plausible,
    mouth_aspect_ratio,
)
from app.analysis.landmarks import FaceLandmarks, get_key_points
from app.analysis.severity import build_event_dict


class SomnolenceDetector:
    def __init__(
        self,
        thresholds: dict[str, Any] | None = None,
        now_fn: Callable[[], float] | None = None,
    ):
        self.thresholds: dict[str, Any] = dict(thresholds or {})
        self._now: Callable[[], float] = now_fn or time.monotonic
        self._perclos = PerclosTracker(window_sec=float(self._get("perclos_window_sec", 60.0)))
        self._blinks: deque[float] = deque()
        self._yawns: deque[float] = deque()
        self._eye_closed = False
        self._eye_closed_start = 0.0
        self._yawning = False
        self._yawn_start = 0.0
        self._yawn_peak = 0.0
        self._tilted = False
        self._tilt_start = 0.0
        self._last_emit: dict[str, float] = {}
        # Latch por episodio: evita re-emitir mientras la condición sigue activa.
        self._latched: set[str] = set()
        self.update_thresholds(self.thresholds)

    # -- config ---------------------------------------------------------
    def _get(self, key: str, default: Any) -> Any:
        # Aliases legacy (config vieja) -> canónico.
        aliases = {
            "blink_rate_max": ("blink_rate_threshold",),
            "eye_closed_min_sec": ("eye_closure_duration_sec",),
            "yawn_mar_threshold": ("yawn_threshold",),
        }
        if key in self.thresholds:
            return self.thresholds[key]
        for old in aliases.get(key, ()):
            if old in self.thresholds:
                return self.thresholds[old]
        return default

    def update_thresholds(self, thresholds: dict[str, Any]) -> None:
        self.thresholds = dict(thresholds or {})
        t = self.thresholds
        self.blink_max = float(self._get("blink_rate_max", 25))
        self.blink_min = float(self._get("blink_rate_min", 5))
        self.blink_window = float(self._get("blink_window_sec", self._get("window_sec", 15)))
        self.eye_min = float(self._get("eye_closed_min_sec", 2.0))
        self.eye_critical = float(self._get("eye_closed_critical_sec", 3.0))
        self.yawn_min = int(self._get("yawn_count_min", 2))
        self.yawn_window = float(self._get("yawn_window_min", 5)) * 60.0
        self.yawn_open = float(self._get("yawn_mar_threshold", 0.75))
        # Histéresis: se abre con open_thr pero solo se cierra bajo close_thr
        # (evita que el habla, MAR fluctuante 0.4-0.65, cuente como bostezo).
        self.yawn_close = max(0.1, self.yawn_open - float(self._get("yawn_close_margin", 0.15)))
        # Pico mínimo: el habla fuerte sostiene 0.75-0.85 pero rara vez pica
        # ≥0.9; el bostezo real casi siempre estira al máximo (0.9-1.2).
        self.yawn_peak_min = float(self._get("yawn_peak_mar_min", 0.9))
        # Bostezo real dura 2-4s; hablar rara vez mantiene la boca abierta
        # de forma continua. 0.8s era demasiado corto (falsos positivos).
        self.yawn_min_dur = float(self._get("yawn_min_duration_sec", 2.0))
        # Dos bostezos reales están separados; sin cooldown una frase larga
        # con pausas contaba como 2+ bostezos.
        self.yawn_cooldown = float(self._get("yawn_cooldown_sec", 30.0))
        self.yawn_mar = self.yawn_open  # alias compat (lectores externos)
        self._last_yawn_counted = -1e9
        self.tilt_deg = float(self._get("head_tilt_deg_min", 20))
        self.tilt_dur = float(self._get("head_tilt_duration_sec", 3.0))
        self.pose_yaw_limit = float(self._get("pose_yaw_limit_deg", 80.0))
        self.pose_pitch_limit = float(self._get("pose_pitch_limit_deg", 65.0))
        self.ear_thr = float(self._get("ear_closed_threshold", 0.2))
        self.perclos_thr = float(t.get("perclos_threshold", 0.25))
        self.cooldown = float(t.get("event_cooldown_sec", 10.0))
        self._perclos.window_sec = float(self._get("perclos_window_sec", 60.0))

    # -- API principal --------------------------------------------------
    def update(self, ear: float, mar: float, tilt: float, now: float | None = None) -> list[dict]:
        now = self._now() if now is None else float(now)
        events: list[dict] = []
        closed = is_eye_closed(ear, self.ear_thr)
        yawning_open = mar >= self.yawn_open
        yawning_closed = mar < self.yawn_close
        perclos = self._perclos.update(closed, now)

        # --- parpadeo: transición abierto->cerrado abre episodio, cerrado->abierto lo cierra
        if closed and not self._eye_closed:
            self._eye_closed = True
            self._eye_closed_start = now
        elif not closed and self._eye_closed:
            self._eye_closed = False
            dur = now - self._eye_closed_start
            if 0.05 <= dur <= 0.6:
                self._blinks.append(now)
            self._latched.discard("EV-SOM-02")
            self._latched.discard("EV-SOM-05")
        # Purgar ventana blink
        cutoff = now - self.blink_window
        while self._blinks and self._blinks[0] < cutoff:
            self._blinks.popleft()
        rate_per_min = len(self._blinks) * (60.0 / self.blink_window) if self.blink_window > 0 else 0
        if len(self._blinks) >= 2 and (rate_per_min > self.blink_max or rate_per_min < self.blink_min):
            # Solo evaluar si la ventana está "llena" (>=60% del tiempo) para no
            # disparar con 2 blinks en 2s al arrancar.
            oldest = self._blinks[0] if self._blinks else now
            if (now - oldest) >= self.blink_window * 0.6:
                ev = self._maybe_emit("EV-SOM-01", now,
                                      f"Parpadeo anómalo: {len(self._blinks)} en {self.blink_window:.0f}s",
                                      {"blink_count": len(self._blinks), "rate_per_min": round(rate_per_min, 1),
                                       "perclos": round(perclos, 3)})
                if ev:
                    events.append(ev)
        elif perclos >= self.perclos_thr and (now - self._blinks[0] if self._blinks else now) >= 0:
            # PERCLOS alto con ventana con muestras suficientes -> somnolencia leve.
            if len(self._perclos._samples) >= 20:
                ev = self._maybe_emit("EV-SOM-01", now, f"PERCLOS alto: {perclos:.2f}",
                                      {"perclos": round(perclos, 3)})
                if ev:
                    events.append(ev)

        # --- cierre prolongado EV-SOM-02
        if closed and self._eye_closed:
            dur = now - self._eye_closed_start
            if dur >= self.eye_min:
                ev = self._maybe_emit("EV-SOM-02", now, f"Ojos cerrados {dur:.1f}s",
                                      {"closure_sec": round(dur, 2), "ear": round(ear, 3)})
                if ev:
                    events.append(ev)

        # --- bostezo: MAR >= open_thr sostenido >= min_dur cuenta 1.
        # Cierre con histéresis (close_thr < open_thr) + cooldown entre
        # bostezos: el habla (picos cortos ~1s, MAR~0.65) ya no cuenta.
        counted_now = False
        if yawning_open and not self._yawning:
            self._yawning = True
            self._yawn_start = now
            self._yawn_peak = mar
        elif self._yawning:
            self._yawn_peak = max(self._yawn_peak, mar)
            if yawning_closed:
                self._yawning = False
                dur = now - self._yawn_start
                if (dur >= self.yawn_min_dur
                        and self._yawn_peak >= self.yawn_peak_min
                        and (now - self._last_yawn_counted) >= self.yawn_cooldown):
                    self._yawns.append(now)
                    self._last_yawn_counted = now
                    counted_now = True
        wcut = now - self.yawn_window
        while self._yawns and self._yawns[0] < wcut:
            self._yawns.popleft()
        # Solo se (re)emite cuando SE CUENTA un bostezo nuevo, no con
        # cualquier movimiento de boca posterior (eso re-disparaba la alerta
        # cada 10s durante toda la ventana de 5 min: el spam reportado).
        if counted_now and len(self._yawns) >= self.yawn_min:
            self._latched.discard("EV-SOM-03")
            ev = self._maybe_emit("EV-SOM-03", now,
                                  f"{len(self._yawns)} bostezos en {self.yawn_window/60:.0f}min",
                                  {"yawn_count": len(self._yawns), "mar": round(mar, 3)})
            if ev:
                events.append(ev)

        # --- cabeceo EV-SOM-04
        tilted = tilt >= self.tilt_deg
        if tilted and not self._tilted:
            self._tilted = True
            self._tilt_start = now
        elif not tilted and self._tilted:
            self._tilted = False
            self._latched.discard("EV-SOM-04")
            self._latched.discard("EV-SOM-05")
        if tilted and self._tilted and (now - self._tilt_start) >= self.tilt_dur:
            ev = self._maybe_emit("EV-SOM-04", now, f"Cabeceo {tilt:.0f}° por {now-self._tilt_start:.1f}s",
                                  {"tilt_deg": round(tilt, 1), "duration_sec": round(now - self._tilt_start, 2)})
            if ev:
                events.append(ev)

        # --- microsueño EV-SOM-05: ojos>3s + cabeceo simultáneo
        if closed and self._eye_closed and tilted and self._tilted:
            eye_dur = now - self._eye_closed_start
            if eye_dur >= self.eye_critical:
                ev = self._maybe_emit("EV-SOM-05", now, f"Microsueño: ojos {eye_dur:.1f}s + cabeceo {tilt:.0f}°",
                                      {"closure_sec": round(eye_dur, 2), "tilt_deg": round(tilt, 1),
                                       "ear": round(ear, 3)}, cooldown=15.0)
                if ev:
                    events.append(ev)
        return events

    def process_landmarks(self, lm: FaceLandmarks | None, now: float | None = None) -> list[dict]:
        if lm is None:
            return []
        try:
            kp = get_key_points(lm.landmarks)
            ear_l = eye_aspect_ratio(kp["left_eye"])
            ear_r = eye_aspect_ratio(kp["right_eye"])
            ear = (ear_l + ear_r) / 2.0
            mar = mouth_aspect_ratio(kp["mouth"])
            pitch, yaw, _ = estimate_head_pose(lm.landmarks, lm.image_shape)
            # Flip de solvePnP (p.ej. pitch −179°) = evidencia inválida,
            # no cabeceo: tilt 0 en vez de ~179.
            if is_head_pose_plausible(pitch, yaw, self.pose_yaw_limit, self.pose_pitch_limit):
                tilt = head_tilt_deg(pitch, yaw)
            else:
                tilt = 0.0
            return self.update(ear, mar, tilt, now)
        except Exception:
            return []

    # -- helpers ----------------------------------------------------------
    def _maybe_emit(self, ev_id: str, now: float, msg: str, extra: dict, cooldown: float | None = None) -> dict | None:
        if ev_id in self._latched:
            return None
        cd = self.cooldown if cooldown is None else cooldown
        last = self._last_emit.get(ev_id, -1e9)
        if (now - last) < cd:
            return None
        self._last_emit[ev_id] = now
        self._latched.add(ev_id)
        # SOM-01 por blink/PERCLOS es re-evaluable (no latch permanente en ventana):
        if ev_id == "EV-SOM-01":
            self._latched.discard(ev_id)
        return build_event_dict(ev_id, msg, extra)

    def reset(self) -> None:
        self._blinks.clear()
        self._yawns.clear()
        self._eye_closed = False
        self._yawning = False
        self._yawn_peak = 0.0
        self._tilted = False
        self._latched.clear()
        self._last_emit.clear()
        self._last_yawn_counted = -1e9
