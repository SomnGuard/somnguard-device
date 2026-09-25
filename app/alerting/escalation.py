"""Escalamiento por persistencia — HU-DEVICE-004 AC-002.

Norma: evento persistente > 10s → siguiente nivel de severidad
(AS-01 → AS-02 → AS-03 → AS-04). Intervalo y reset configurables vía
detection_thresholds (escalation_interval_sec / escalation_reset_sec).

Diseño:
- Clave por grupo de riesgo: somnolencia (EV-SOM-*) comparte tracker para que
  la cadena AS-01→04 progrese aunque el detector rote subtipos; el resto de
  dominios (distracción, cinturón, sistema) escalan por event_type individual
  solo dentro de la cadena de somnolencia; fuera de ella no escalan (p.ej.
  AS-05 persistente repite AS-05, no salta a AS-06).
- Sin hilos: el manager llama a `escalate()` con el tiempo monotónico actual;
  el tracker es puro y testeable sin asyncio.
- Reset: si el grupo no aparece durante > escalation_reset_sec, se olvida
  (el conductor ya respondió / el riesgo cesó).
"""
from __future__ import annotations

import time

from app.common.models import AlertCode


# Cadena normativa de escalamiento (Apéndice 1: somnolencia leve→crítica).
ESCALATION_CHAIN: list[AlertCode] = [
    AlertCode.AS_01,
    AlertCode.AS_02,
    AlertCode.AS_03,
    AlertCode.AS_04,
]

# Severidad aparejada a cada peldaño (enum local HU-DEVICE-001).
CHAIN_SEVERITY: list[str] = ["LEVE", "MODERADA", "SEVERA", "CRITICA"]

DEFAULT_INTERVAL_SEC = 10.0
DEFAULT_RESET_SEC = 5.0


def _group_for(event_type_id: str) -> str:
    """Somnolencia comparte grupo; el resto escala por evento individual."""
    if event_type_id.startswith("EV-SOM-"):
        return "SOMNOLENCE"
    return event_type_id or "UNKNOWN"


class EscalationTracker:
    """Recuerda primera/última vista por grupo y calcula el peldaño vigente."""

    def __init__(self, interval_sec: float = DEFAULT_INTERVAL_SEC,
                 reset_sec: float = DEFAULT_RESET_SEC) -> None:
        self.interval_sec = interval_sec
        self.reset_sec = reset_sec
        self._first_seen: dict[str, float] = {}
        self._last_seen: dict[str, float] = {}

    def configure(self, interval_sec: float | None = None,
                  reset_sec: float | None = None) -> None:
        if interval_sec is not None:
            try:
                self.interval_sec = min(60.0, max(5.0, float(interval_sec)))
            except (TypeError, ValueError):
                pass
        if reset_sec is not None:
            try:
                self.reset_sec = min(30.0, max(1.0, float(reset_sec)))
            except (TypeError, ValueError):
                pass

    def reset(self, group: str | None = None) -> None:
        if group is None:
            self._first_seen.clear()
            self._last_seen.clear()
        else:
            self._first_seen.pop(group, None)
            self._last_seen.pop(group, None)

    def escalate(self, event_type_id: str, alert_code: AlertCode,
                 now_monotonic: float | None = None) -> tuple[AlertCode, str | None]:
        """Retorna (alert_code_vigente, severity_override_o_None).

        - Fuera de la cadena AS-01..AS-04 → sin escalamiento (mismo código, None).
        - Dentro de la cadena: peldaño = elapsed // interval, acotado al tope.
        - Gap > reset_sec → reinicia el cronómetro del grupo.
        """
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        if alert_code not in ESCALATION_CHAIN:
            return alert_code, None
        group = _group_for(event_type_id)
        last = self._last_seen.get(group)
        first = self._first_seen.get(group)
        if last is None or first is None or (now - last) > self.reset_sec:
            self._first_seen[group] = now
            self._last_seen[group] = now
            return alert_code, None
        self._last_seen[group] = now
        elapsed = now - self._first_seen[group]
        try:
            base_idx = ESCALATION_CHAIN.index(alert_code)
        except ValueError:
            return alert_code, None
        steps = int(elapsed // self.interval_sec)
        idx = min(len(ESCALATION_CHAIN) - 1, base_idx + steps)
        # No retrocede: si el detector ya subió solo, respeta el mayor.
        if idx <= base_idx:
            return alert_code, None
        escalated = ESCALATION_CHAIN[idx]
        return escalated, CHAIN_SEVERITY[idx]
