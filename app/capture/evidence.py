"""Evidencia local JPEG — HU-DEVICE-003 AC-007.

Al confirmar un evento con severidad >= MODERADA el device guarda 1 frame
JPEG (640px lado mayor, calidad 70) en ``<data_dir>/media/<event_id>.jpg``.

Si falla la captura/codificación, retorna None y el evento se guarda igual
sin evidencia (no bloquea). Solo stdlib + ``cv2`` si está disponible
(inyectable para tests sin hardware).
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)

EVIDENCE_MAX_SIDE_PX = 640
EVIDENCE_JPEG_QUALITY = 70
# Severidades que generan evidencia (AC-007: >= MODERADA).
EVIDENCE_SEVERITIES = frozenset({"MODERADA", "SEVERA", "CRITICA"})
# Prefijo relativo portable dentro de <data_dir> (nunca absoluto en DB).
EVIDENCE_MEDIA_DIRNAME = "media"


def needs_evidence(severity: str | None) -> bool:
    return str(severity or "").strip().upper() in EVIDENCE_SEVERITIES


def media_path(data_dir: Path | str, event_id: str) -> Path:
    """Ruta absoluta de escritura: <data_dir>/media/<event_id>.jpg."""
    return Path(data_dir) / EVIDENCE_MEDIA_DIRNAME / f"{event_id}.jpg"


def evidence_relpath(event_id: str) -> str:
    """Ruta relativa portable para guardar en DB: media/<event_id>.jpg."""
    return f"{EVIDENCE_MEDIA_DIRNAME}/{event_id}.jpg"


def to_relative_evidence_path(data_dir: Path | str | None,
                              stored: str | None) -> str | None:
    """Normaliza cualquier path legacy a relativo portable.

    - ``None``/vacío → None.
    - Ya relativo (``media/x.jpg`` o ``x.jpg``) → ``media/x.jpg``.
    - Absoluto (``C:/.../data/media/x.jpg``, ``/var/.../x.jpg``) → ``media/x.jpg``.
    """
    if not stored:
        return None
    s = str(stored).strip().replace("\\", "/")
    if not s:
        return None
    # Quita prefijos file:// y drive absolutos dejando el nombre.
    name = s.rsplit("/", 1)[-1]
    if not name:
        return None
    return f"{EVIDENCE_MEDIA_DIRNAME}/{name}"


def resolve_evidence_path(data_dir: Path | str | None,
                          stored: str | None) -> Path | None:
    """Resuelve un path guardado (relativo nuevo o absoluto legacy) a absoluto.

    Retorna None si no hay path. Si ``data_dir`` es None y el path es relativo,
    retorna el relativo como Path (el llamador decide).
    """
    if not stored:
        return None
    s = str(stored).strip()
    if not s:
        return None
    p = Path(s)
    try:
        if p.is_absolute():
            return p
    except Exception:
        return Path(s)
    if data_dir is None:
        return p
    # Relativo: media/x.jpg o x.jpg → <data_dir>/...
    rel = s.replace("\\", "/")
    if rel.startswith(f"{EVIDENCE_MEDIA_DIRNAME}/"):
        return Path(data_dir) / rel
    if "/" not in rel and "\\" not in s:
        return Path(data_dir) / EVIDENCE_MEDIA_DIRNAME / rel
    return Path(data_dir) / p


def save_event_frame(frame: Any, event_id: str, severity: str | None,
                     data_dir: Path | str, cv2_module: Any = None) -> Optional[str]:
    """Guarda 1 frame JPEG. Retorna path RELATIVO o None (nunca lanza).

    Retorna ``media/<event_id>.jpg`` (relativo a ``data_dir``, portable entre
    máquinas). El archivo se escribe en ``<data_dir>/media/<event_id>.jpg``.

    - Si ``severity`` < MODERADA → None (no se captura).
    - Si ``frame`` es None/vacío o sin ``cv2`` → None.
    """
    if not needs_evidence(severity):
        return None
    if frame is None or not event_id:
        return None
    try:
        cv2 = cv2_module
        if cv2 is None:
            try:
                import cv2 as _cv2  # type: ignore
                cv2 = _cv2
            except Exception:
                logger.debug("Evidencia omitida (%s): sin cv2", event_id)
                return None
        h, w = frame.shape[:2]
        if h <= 0 or w <= 0:
            return None
        scale = min(1.0, EVIDENCE_MAX_SIDE_PX / float(max(h, w)))
        small = frame
        if scale < 1.0:
            try:
                small = cv2.resize(frame, (int(w * scale), int(h * scale)))
            except Exception as e:
                logger.debug("Evidencia resize fallido (%s): %s", event_id, e)
                return None
        dest = media_path(data_dir, event_id)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.debug("Evidencia mkdir fallido (%s): %s", event_id, e)
            return None
        try:
            ok = cv2.imwrite(str(dest), small,
                             [int(cv2.IMWRITE_JPEG_QUALITY), int(EVIDENCE_JPEG_QUALITY)])
        except Exception as e:
            logger.debug("Evidencia imwrite fallido (%s): %s", event_id, e)
            return None
        if not ok:
            return None
        return evidence_relpath(event_id)
    except Exception as e:  # nunca bloquea el evento
        logger.debug("Evidencia omitida (%s): %s", event_id, e)
        return None
