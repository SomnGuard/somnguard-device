"""Tabla canónica EV -> (categoría, severidad, alerta) — HU-DEVICE-001 AC-002.

Fuente normativa: `somnguard-docs/docs/06-data-architecture/data-dictionary.md`
seed Apéndice 2 SRS (EV-SOM-01..EV-SYS-06) + SRS §RF-4.1-4.3/RF-5.1-5.2.

AC-002 exige: leve AS-01 / moderada AS-02 / severa AS-03 / crítica AS-04
para somnolencia. Distracción (AS-05/AS-06) y cinturón (AS-07) usan
INFO de base y MODERADA en versión prolongada (escalamiento HU-DEVICE-004).
"""
from __future__ import annotations

from app.common.models import AlertCode, EventCategory, Severity


EVENT_TABLE: dict[str, dict] = {
    # Somnolencia — AC-001/AC-002
    "EV-SOM-01": {"name": "Parpadeo anómalo", "category": EventCategory.SOMNOLENCIA,
                  "severity": Severity.LEVE, "alert_code": AlertCode.AS_01},
    "EV-SOM-02": {"name": "Cierre prolongado ojos", "category": EventCategory.SOMNOLENCIA,
                  "severity": Severity.MODERADA, "alert_code": AlertCode.AS_02},
    "EV-SOM-03": {"name": "Bostezo detectado", "category": EventCategory.SOMNOLENCIA,
                  "severity": Severity.MODERADA, "alert_code": AlertCode.AS_02},
    "EV-SOM-04": {"name": "Cabeceo/inclinación", "category": EventCategory.SOMNOLENCIA,
                  "severity": Severity.SEVERA, "alert_code": AlertCode.AS_03},
    "EV-SOM-05": {"name": "Microsueño detectado", "category": EventCategory.SOMNOLENCIA,
                  "severity": Severity.CRITICA, "alert_code": AlertCode.AS_04},
    # Distracción — AC-003
    "EV-DIS-01": {"name": "Uso teléfono móvil", "category": EventCategory.DISTRACCION,
                  "severity": Severity.INFO, "alert_code": AlertCode.AS_05},
    "EV-DIS-02": {"name": "Uso prolongado teléfono", "category": EventCategory.DISTRACCION,
                  "severity": Severity.MODERADA, "alert_code": AlertCode.AS_05},
    "EV-DIS-03": {"name": "Mirada fuera de la vía", "category": EventCategory.DISTRACCION,
                  "severity": Severity.INFO, "alert_code": AlertCode.AS_06},
    "EV-DIS-04": {"name": "Mirada prolongada fuera", "category": EventCategory.DISTRACCION,
                  "severity": Severity.MODERADA, "alert_code": AlertCode.AS_06},
    "EV-DIS-05": {"name": "Movimiento anómalo", "category": EventCategory.DISTRACCION,
                  "severity": Severity.INFO, "alert_code": AlertCode.AS_05},
    # Cinturón — AC-004
    "EV-CIN-01": {"name": "Cinturón no detectado", "category": EventCategory.CINTURON,
                  "severity": Severity.INFO, "alert_code": AlertCode.AS_07},
    "EV-CIN-02": {"name": "Cinturón mal colocado", "category": EventCategory.CINTURON,
                  "severity": Severity.INFO, "alert_code": AlertCode.AS_07},
    # Sistema — AC-006 / HU-DEVICE-002 (compat)
    "EV-SYS-01": {"name": "Inicialización exitosa", "category": EventCategory.SISTEMA,
                  "severity": Severity.INFO, "alert_code": AlertCode.AS_08},
    "EV-SYS-02": {"name": "Error cámara/obstrucción", "category": EventCategory.SISTEMA,
                  "severity": Severity.INFO, "alert_code": AlertCode.AS_09},
    "EV-SYS-03": {"name": "Rostro no detectado", "category": EventCategory.SISTEMA,
                  "severity": Severity.INFO, "alert_code": AlertCode.AS_09},
    "EV-SYS-04": {"name": "Conectividad perdida", "category": EventCategory.SISTEMA,
                  "severity": Severity.INFO, "alert_code": AlertCode.AS_09},
    "EV-SYS-05": {"name": "Conectividad restaurada", "category": EventCategory.SISTEMA,
                  "severity": Severity.INFO, "alert_code": AlertCode.AS_08},
    "EV-SYS-06": {"name": "Almacenamiento casi lleno", "category": EventCategory.SISTEMA,
                  "severity": Severity.MODERADA, "alert_code": AlertCode.AS_09},
}


def severity_for_event(event_type_id: str) -> Severity:
    entry = EVENT_TABLE.get(event_type_id)
    return entry["severity"] if entry else Severity.INFO


def alert_for_event(event_type_id: str) -> AlertCode:
    entry = EVENT_TABLE.get(event_type_id)
    return entry["alert_code"] if entry else AlertCode.AS_09


def category_for_event(event_type_id: str) -> EventCategory:
    entry = EVENT_TABLE.get(event_type_id)
    return entry["category"] if entry else EventCategory.SISTEMA


def build_event_dict(event_type_id: str, message: str = "", extra: dict | None = None) -> dict:
    """Construye el dict de evento que emite el pipeline/detectores.

    Incluye category/severity/alert_code canónicos + message. `extra` se
    fusiona (p.ej. métricas EAR/MAR/PERCLOS para evidencia HU-DEVICE-003).
    """
    entry = EVENT_TABLE.get(event_type_id, {})
    data: dict = {
        "event_type_id": event_type_id,
        "category": entry.get("category", EventCategory.SISTEMA).value
        if hasattr(entry.get("category", None), "value") else str(entry.get("category", "SISTEMA")),
        "severity": entry.get("severity", Severity.INFO).value
        if hasattr(entry.get("severity", None), "value") else str(entry.get("severity", "INFO")),
        "alert_code": entry.get("alert_code", AlertCode.AS_09).value
        if hasattr(entry.get("alert_code", None), "value") else str(entry.get("alert_code", "AS-09")),
        "message": message or entry.get("name", event_type_id),
    }
    if extra:
        data.update(extra)
    return data
