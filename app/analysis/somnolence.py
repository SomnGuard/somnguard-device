"""Detector de somnolencia — HU-DEVICE-001 AC-001/AC-002.

Eventos (Apéndice 2 SRS + literatura DMS Euro NCAP/NHTSA/PERCLOS):
  EV-SOM-01 parpadeo anómalo  blink>25 o <5/min en 15s, o cierre
            lento aislado 0.5-2s (duración anormal)               -> AS-01 LEVE
  EV-SOM-02 cierre ojos >2s                                    -> AS-02 MODERADA
  EV-SOM-03 bostezo prolongado ≥3s (cada uno alerta; el conteo en
            ventana queda como evidencia acumulativa)            -> AS-02 MODERADA
  EV-SOM-04 inclinación relativa ≥15° por >3s (neutro auto-cero) -> AS-03 SEVERA
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
    TiltEstimator,
    estimate_head_pose,
    eye_aspect_ratio,
    is_eye_closed,
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
        self._yawn_open_frames = 0
        self._yawn_total_frames = 0
        # Tras abortar por duración máxima (habla encadenada) se exige
        # cierre real (MAR < close) antes de permitir un episodio nuevo.
        # Sin esto, habla de 10s abortaba a los 6s y re-empezaba a los 6.1s,
        # y el 2º tramo sí contaba como bostezo al cerrar.
        self._yawn_needs_close = False
        self._tilted = False
        self._tilt_start = 0.0
        self._last_emit: dict[str, float] = {}
        # Latch por episodio: evita re-emitir mientras la condición sigue activa.
        self._latched: set[str] = set()
        # Suavizado EMA de señales crudas (anti-jitter de landmarks con
        # movimiento/luz mala): un spike de 1 frame no abre/cierra episodios.
        self._ear_s: float | None = None
        self._mar_s: float | None = None
        # Tilt relativo al neutro frontal (ver TiltEstimator): el solvePnP
        # trae sesgo ~±180° en pitch y el gate absoluto lo dejaba en 0.
        self.tilt_est = TiltEstimator()
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
        # Cierre lento aislado ≥0.5s (blink normal 0.1-0.4s) -> SOM-01 LEVE.
        self.blink_slow = float(self._get("blink_slow_sec", 0.5))
        self.blink_window = float(self._get("blink_window_sec", self._get("window_sec", 15)))
        self.eye_min = float(self._get("eye_closed_min_sec", 2.0))
        self.eye_critical = float(self._get("eye_closed_critical_sec", 3.0))
        self.yawn_min = int(self._get("yawn_count_min", 1))
        self.yawn_window = float(self._get("yawn_window_min", 5)) * 60.0
        self.yawn_open = float(self._get("yawn_mar_threshold", 0.85))
        # Histéresis: se abre con open_thr pero solo se cierra bajo close_thr
        # (evita que el habla, MAR fluctuante 0.4-0.65, cuente como bostezo).
        self.yawn_close = max(0.1, self.yawn_open - float(self._get("yawn_close_margin", 0.25)))
        # Pico mínimo: el habla fuerte sostiene 0.75-0.85 pero rara vez pica
        # ≥1.0; el bostezo real casi siempre estira al máximo (1.0-1.3).
        self.yawn_peak_min = float(self._get("yawn_peak_mar_min", 1.0))
        # Bostezo prolongado ≥3s = indicador directo de fatiga (PLOS
        # LDIE-FDNet: la separación normal/fatiga se estabiliza en ~3s).
        # El conteo en ventana queda como evidencia acumulativa, no como
        # condición para la primera alerta.
        self.yawn_min_dur = float(self._get("yawn_min_duration_sec", 3.0))
        # Habla/risa encadenada puede sostener MAR alto 10s+: si el episodio
        # supera el máximo ya no es bostezo -> se aborta sin contar.
        self.yawn_max_dur = float(self._get("yawn_max_duration_sec", 6.0))
        # Fracción mínima de frames por encima de open dentro del episodio.
        # El habla oscila (abre/cierra por sílabas) y da ratio ~0.3-0.5;
        # el bostezo real sostiene apertura y da ratio ~0.8-1.0.
        # Solo se exige con >=5 frames (con muestreo disperso/tests se omite).
        self.yawn_min_open_ratio = float(self._get("yawn_min_open_ratio", 0.6))
        # Dos bostezos reales están separados; sin cooldown una frase larga
        # con pausas contaba como 2+ bostezos.
        self.yawn_cooldown = float(self._get("yawn_cooldown_sec", 30.0))
        self.yawn_mar = self.yawn_open  # alias compat (lectores externos)
        self._last_yawn_counted = -1e9
        self.tilt_deg = float(self._get("head_tilt_deg_min", 15))
        self.tilt_dur = float(self._get("head_tilt_duration_sec", 3.0))
        self.pose_yaw_limit = float(self._get("pose_yaw_limit_deg", 80.0))
        self.pose_pitch_limit = float(self._get("pose_pitch_limit_deg", 65.0))
        try:
            tilt_calib = int(self._get("tilt_calib_samples",
                                       self._get("gaze_calib_samples", 60)))
        except (TypeError, ValueError):
            tilt_calib = 60
        self.tilt_est.calib_samples = max(0, tilt_calib)
        self.tilt_est.smooth_alpha = min(1.0, max(0.05, float(
            self._get("gaze_smooth_alpha", 0.35))))
        self.tilt_est.yaw_limit = self.pose_yaw_limit
        self.tilt_est.pitch_limit = self.pose_pitch_limit
        # Sin reset: igual que gaze, se conserva neutro/calibración en curso.
        self.ear_thr = float(self._get("ear_closed_threshold", 0.2))
        self.perclos_thr = float(t.get("perclos_threshold", 0.25))
        self.cooldown = float(t.get("event_cooldown_sec", 10.0))
        self._perclos.window_sec = float(self._get("perclos_window_sec", 60.0))
        # EMA: 1.0 = sin suavizado (solo crudo); 0.6 default absorbe spikes
        # de 1-2 frames sin matar picos reales de 2.5s+. Rango 0.2-1.0.
        try:
            self.smooth_alpha = min(1.0, max(0.2, float(self._get("signal_smooth_alpha", 0.6))))
        except (TypeError, ValueError):
            self.smooth_alpha = 0.6

    # -- API principal --------------------------------------------------
    def update(self, ear: float, mar: float, tilt: float, now: float | None = None) -> list[dict]:
        now = self._now() if now is None else float(now)
        events: list[dict] = []
        # Suavizado: decisiones sobre EMA, no sobre el frame crudo.
        a = self.smooth_alpha
        self._ear_s = float(ear) if self._ear_s is None else (1 - a) * self._ear_s + a * float(ear)
        self._mar_s = float(mar) if self._mar_s is None else (1 - a) * self._mar_s + a * float(mar)
        ear_s, mar_s = self._ear_s, self._mar_s
        closed = is_eye_closed(ear_s, self.ear_thr)
        yawning_open = mar_s >= self.yawn_open
        yawning_closed = mar_s < self.yawn_close
        perclos = self._perclos.update(closed, now)

        # --- parpadeo: transición abierto->cerrado abre episodio, cerrado->abierto lo cierra
        if closed and not self._eye_closed:
            self._eye_closed = True
            self._eye_closed_start = now
        elif not closed and self._eye_closed:
            self._eye_closed = False
            dur = now - self._eye_closed_start
            if 0.05 <= dur <= 0.5:
                self._blinks.append(now)
            elif self.blink_slow <= dur < self.eye_min:
                # Parpadeo lento: cierre 0.5-2s aislado (duración anormal
                # respecto al blink normal 0.1-0.4s) -> LEVE. Con cooldown
                # 10s, sin latch permanente.
                ev = self._maybe_emit("EV-SOM-01", now,
                                      f"Parpadeo lento: {dur:.1f}s",
                                      {"closure_sec": round(dur, 2),
                                       "perclos": round(perclos, 3)})
                if ev:
                    events.append(ev)
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
                                      {"closure_sec": round(dur, 2), "ear": round(ear_s, 3)})
                if ev:
                    events.append(ev)

        # --- bostezo: MAR >= open_thr sostenido >= min_dur cuenta 1.
        # Cierre con histéresis (close_thr < open_thr) + cooldown entre
        # bostezos + ratio de apertura + duración máxima: el habla
        # (picos cortos, MAR~0.65, oscilante) y el ruido de landmarks
        # con movimiento ya no cuentan.
        counted_now = False
        # Si venimos de un abort por max_dur, solo se desbloquea con cierre.
        if yawning_closed:
            self._yawn_needs_close = False
        if yawning_open and not self._yawning and not self._yawn_needs_close:
            self._yawning = True
            self._yawn_start = now
            self._yawn_peak = mar_s
            self._yawn_open_frames = 1
            self._yawn_total_frames = 1
        elif self._yawning:
            self._yawn_peak = max(self._yawn_peak, mar_s)
            self._yawn_total_frames += 1
            if yawning_open:
                self._yawn_open_frames += 1
            # Episodio eterno (habla encadenada / boca entreabierta por
            # ruido): abortar sin contar, exige cierre real antes de reabrir.
            if (now - self._yawn_start) > self.yawn_max_dur:
                self._yawning = False
                self._yawn_open_frames = 0
                self._yawn_total_frames = 0
                self._yawn_needs_close = True
            elif yawning_closed:
                self._yawning = False
                dur = now - self._yawn_start
                open_ratio = (self._yawn_open_frames / self._yawn_total_frames
                              if self._yawn_total_frames else 0.0)
                # Con pocas muestras (tests / fps bajo) no se exige ratio:
                # no hay evidencia de oscilación tipo habla.
                ratio_ok = (open_ratio >= self.yawn_min_open_ratio
                            if self._yawn_total_frames >= 5 else True)
                # EPS flotante: a 10fps dur suma 0.1s y puede dar 2.4999 en
                # el borde exacto de min_dur (la evaluación es una sola vez
                # al cerrar, no reintenta al frame siguiente como ojos).
                if (dur + 1e-6 >= self.yawn_min_dur and dur <= self.yawn_max_dur + 1e-6
                        and ratio_ok
                        and self._yawn_peak + 1e-6 >= self.yawn_peak_min
                        and (now - self._last_yawn_counted) >= self.yawn_cooldown):
                    self._yawns.append(now)
                    self._last_yawn_counted = now
                    counted_now = True
                self._yawn_open_frames = 0
                self._yawn_total_frames = 0
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
                                   {"yawn_count": len(self._yawns), "mar": round(mar_s, 3)})
            if ev:
                events.append(ev)

        # --- cabeceo EV-SOM-04
        # Bostezar echa la cabeza atrás (tilt 20-26° medido en campo): no es
        # cabeceo de somnolencia. Mientras el bostezo está activo no se
        # acumula tilt; al terminar, un tilt real requiere sus 3s completos.
        tilted = tilt >= self.tilt_deg and not self._yawning
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
            now_f = self._now() if now is None else float(now)
            kp = get_key_points(lm.landmarks)
            ear_l = eye_aspect_ratio(kp["left_eye"])
            ear_r = eye_aspect_ratio(kp["right_eye"])
            ear = (ear_l + ear_r) / 2.0
            mar = mouth_aspect_ratio(kp["mouth"])
            pitch, yaw, _ = estimate_head_pose(lm.landmarks, lm.image_shape)
            # Tilt relativo al neutro frontal auto-calibrado (TiltEstimator):
            # el pitch crudo trae sesgo ~+170° y el gate absoluto lo dejaba
            # siempre en 0 (EV-SOM-04/05 muertos en campo).
            tilt = self.tilt_est.update(yaw, pitch, now_f)
            return self.update(ear, mar, tilt, now_f)
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

    def debug_state(self, now: float | None = None) -> dict:
        """Estado interno temporizado para monitor/log sin alertas.

        Muestra cuánto falta para cada evento (lo que pedías: "algunos es
        por tiempo"). Todo en segundos salvo ratios. No dispara nada.
        """
        now = self._now() if now is None else float(now)
        eye_dur = (now - self._eye_closed_start) if self._eye_closed else 0.0
        yawn_dur = (now - self._yawn_start) if self._yawning else 0.0
        tilt_dur = (now - self._tilt_start) if self._tilted else 0.0
        open_ratio = (self._yawn_open_frames / self._yawn_total_frames
                      if self._yawn_total_frames else 0.0)
        cutoff = now - self.blink_window
        n_blinks = sum(1 for t in self._blinks if t >= cutoff)
        rate = n_blinks * (60.0 / self.blink_window) if self.blink_window > 0 else 0.0
        wcut = now - self.yawn_window
        n_yawns = sum(1 for t in self._yawns if t >= wcut)
        return {
            "eye_closed": self._eye_closed,
            "eye_dur_sec": round(eye_dur, 2),
            "eye_need_sec": self.eye_min,
            "eye_critical_sec": self.eye_critical,
            "yawning": self._yawning,
            "yawn_dur_sec": round(yawn_dur, 2),
            "yawn_need_sec": self.yawn_min_dur,
            "yawn_max_sec": self.yawn_max_dur,
            "yawn_peak": round(self._yawn_peak, 3),
            "yawn_peak_need": self.yawn_peak_min,
            "yawn_open_ratio": round(open_ratio, 2),
            "yawn_ratio_need": self.yawn_min_open_ratio,
            "yawn_blocked_until_close": self._yawn_needs_close,
            "yawns_in_window": n_yawns,
            "yawns_need": self.yawn_min,
            "yawn_window_min": round(self.yawn_window / 60.0, 1),
            "blinks_in_window": n_blinks,
            "blink_rate_per_min": round(rate, 1),
            "blink_need_max": self.blink_max,
            "blink_need_min": self.blink_min,
            "perclos": round(self._perclos.value, 3),
            "perclos_need": self.perclos_thr,
            "tilted": self._tilted,
            "tilt_dur_sec": round(tilt_dur, 2),
            "tilt_need_deg": self.tilt_deg,
            "tilt_need_sec": self.tilt_dur,
        }

    def reset(self) -> None:
        self._blinks.clear()
        self._yawns.clear()
        self._eye_closed = False
        self._yawning = False
        self._yawn_peak = 0.0
        self._yawn_open_frames = 0
        self._yawn_total_frames = 0
        self._yawn_needs_close = False
        self._ear_s = None
        self._mar_s = None
        self._tilted = False
        try:
            self.tilt_est.reset()
        except Exception:
            pass
        self._latched.clear()
        self._last_emit.clear()
        self._last_yawn_counted = -1e9
