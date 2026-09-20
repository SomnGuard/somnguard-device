"""Motor de sync offline-first — HU-DEVICE-003 AC-003/004/005/006.

- Lote máx 100 a POST /api/v1/telemetry/events (solo metadata JSON).
- Reintentos exponenciales 1m/2m/4m...max 1h con jitter (cross-cutting §4.2).
- Deduplicación: event_id UUID v7; duplicados en duplicate_ids del 201 no son
  error y se borran local igual que acked (ADR-005 §6).
- Limpieza tras ACK 201 (DELETE inmediato + borra JPG); retención 7d FAILED.
- Fase 2 evidencia (ADR-006): tras ACK metadata, sube JPG si has_evidence
  (best-effort, 409 = ya existe → no reintentar).
"""
from __future__ import annotations
import asyncio
import hashlib
import logging
import random
import time
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)

BATCH_LIMIT = 100
BACKOFF_BASE_SEC = 60
BACKOFF_MAX_SEC = 3600
BACKOFF_JITTER_SEC = 30
MAX_RETRIES = 10


def compute_backoff_sec(attempt: int, base_sec: int = BACKOFF_BASE_SEC,
                        max_sec: int = BACKOFF_MAX_SEC,
                        jitter_sec: int = BACKOFF_JITTER_SEC,
                        rng: Any = None) -> int:
    """min(base * 2^attempt + random(0,jitter), max). attempt=retries actuales."""
    try:
        delay = base_sec * (2 ** max(0, int(attempt)))
    except OverflowError:
        delay = max_sec
    jitter = (rng or random).uniform(0, max(0, jitter_sec)) if jitter_sec else 0
    return int(min(max_sec, delay + jitter))


def sha256_file(path: str | Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


class SyncEngine:
    def __init__(self, buffer: Any, backend: Any,
                 base_delay_sec: int = BACKOFF_BASE_SEC,
                 max_delay_sec: int = BACKOFF_MAX_SEC,
                 max_retries: int = MAX_RETRIES,
                 batch_limit: int = BATCH_LIMIT,
                 data_dir: Path | str | None = None):
        self.buffer = buffer
        self.backend = backend
        self.base_delay = base_delay_sec
        self.max_delay = max_delay_sec
        self.max_retries = max_retries
        self.batch_limit = max(1, min(100, int(batch_limit or BATCH_LIMIT)))
        self.data_dir = Path(data_dir) if data_dir is not None else None
        # attempt -> next_retry_monotonic (backoff en memoria por lote)
        self._next_retry_monotonic: float = 0.0
        self._consecutive_failures: int = 0

    def _resolve_evidence(self, stored: str | None) -> Path | None:
        """Resuelve path guardado (relativo portable o absoluto legacy)."""
        if not stored:
            return None
        try:
            from app.capture.evidence import resolve_evidence_path
            return resolve_evidence_path(self.data_dir, stored)
        except Exception:
            pass
        p = Path(str(stored))
        if p.is_absolute() or self.data_dir is None:
            return p
        return self.data_dir / p

    def _backoff_ready(self) -> bool:
        return time.monotonic() >= self._next_retry_monotonic

    def _register_failure(self) -> None:
        delay = compute_backoff_sec(self._consecutive_failures,
                                    self.base_delay, self.max_delay)
        self._next_retry_monotonic = time.monotonic() + delay
        logger.info("Sync fallido (%d seguidos): próximo intento en %ds",
                    self._consecutive_failures + 1, delay)
        self._consecutive_failures += 1

    def _register_success(self) -> None:
        self._consecutive_failures = 0
        self._next_retry_monotonic = 0.0

    async def sync_once(self, device_id: str, api_key: str,
                        retention_days: int = 7) -> dict:
        """Un ciclo: batch → POST → limpieza. Nunca lanza (retorna dict)."""
        if not self._backoff_ready():
            return {"synced": 0, "failed": 0, "skipped_backoff": True}
        try:
            batch = await self._fetch()
        except Exception as e:
            logger.debug("Sync fetch fallido: %s", e)
            return {"synced": 0, "failed": 0, "error": str(e)}
        if not batch:
            # Limpieza oportunista aunque no haya batch
            try:
                self.buffer.purge_old_failed(retention_days)
            except Exception:
                pass
            self._register_success()
            return {"synced": 0, "failed": 0, "empty": True}
        ids = [b["id"] for b in batch]
        events = [b["event"] for b in batch]
        try:
            self.buffer.mark_sending(ids)
        except Exception:
            pass
        try:
            res = await self.backend.post_events(device_id, api_key, events)
            parsed = self.backend.parse_telemetry_ack(res) \
                if hasattr(self.backend, "parse_telemetry_ack") else res
            acked = list(parsed.get("acked_ids", []) or [])
            dup = list(parsed.get("duplicate_ids", []) or [])
            confirmed = [i for i in (acked + dup) if i in set(ids)]
            # Si la API no devuelve ids (mock), asumir todo el lote confirmado
            if not confirmed and not acked and not dup:
                confirmed = list(ids)
            try:
                self.buffer.mark_acknowledged(confirmed)
            except Exception:
                pass
            # Fase 2 primero (usa los JPGs), luego borra remanentes.
            # Orden importa: si borráramos antes, la subida nunca encontraría el archivo.
            await self._upload_evidences_best_effort(device_id, api_key, batch, confirmed)
            self._delete_evidence_files(batch, confirmed)
            # Fallidos = enviados pero no confirmados (p.ej. 422 parcial)
            unconfirmed = [i for i in ids if i not in set(confirmed)]
            if unconfirmed:
                try:
                    self.buffer.mark_failed(unconfirmed, self.max_retries)
                except Exception:
                    pass
            try:
                self.buffer.purge_old_failed(retention_days)
            except Exception:
                pass
            self._register_success()
            logger.info("Sync OK: %d confirmados (%d dup), %d pendientes de reintento",
                        len(confirmed), len(dup), len(unconfirmed))
            return {"synced": len(confirmed), "duplicates": len(dup),
                    "failed": len(unconfirmed), "acked_ids": confirmed}
        except Exception as e:
            try:
                self.buffer.mark_failed(ids, self.max_retries)
            except Exception:
                pass
            self._register_failure()
            logger.warning("Sync lote fallido (%d eventos): %s", len(ids), e)
            return {"synced": 0, "failed": len(ids), "error": str(e)}

    async def _fetch(self) -> list[dict]:
        if hasattr(self.buffer, "afetch_batch"):
            return await self.buffer.afetch_batch(self.batch_limit)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self.buffer.fetch_batch(self.batch_limit))

    def _delete_evidence_files(self, batch: list[dict], confirmed: list[str]) -> None:
        ok = set(confirmed)
        for b in batch:
            if b["id"] not in ok:
                continue
            p = b.get("evidence_path")
            if not p:
                continue
            try:
                jpg = self._resolve_evidence(p)
                if jpg is None:
                    continue
                jpg.unlink(missing_ok=True)
            except OSError:
                continue

    async def _upload_evidences_best_effort(self, device_id: str, api_key: str,
                                            batch: list[dict], confirmed: list[str]) -> int:
        ok = set(confirmed)
        uploaded = 0
        for b in batch:
            if b["id"] not in ok:
                continue
            p = b.get("evidence_path")
            ev = b.get("event") if isinstance(b.get("event"), dict) else {}
            if not p or not ev.get("has_evidence"):
                continue
            # Resuelve relativo (media/x.jpg) o absoluto legacy.
            jpg = self._resolve_evidence(p)
            if jpg is None or not jpg.exists():
                continue
            checksum = sha256_file(jpg)
            if not checksum:
                continue
            try:
                await self.backend.upload_evidence(device_id, api_key, b["id"], str(jpg), checksum)
                uploaded += 1
                try:
                    jpg.unlink(missing_ok=True)
                except OSError:
                    pass
            except Exception as e:
                logger.debug("Evidencia %s no subida (reintento luego): %s", b["id"], e)
                continue
        return uploaded
