"""Buffer offline SQLite — HU-DEVICE-003 AC-001/AC-005.

Tabla ``pending_events`` (ADR-005 + ADR-004):

- ``id`` TEXT PK = ``event_id`` UUID v7 (clave idempotencia global)
- ``event_json`` TEXT NOT NULL (evento serializado)
- ``evidence_path`` TEXT NULL (RELATIVO portable ``media/<id>.jpg``; los
  absolutos legacy ``C:/.../data/media/<id>.jpg`` se migran al leer/escribir)
- ``status`` TEXT PENDING/SENDING/ACKED/FAILED
- ``retries`` INTEGER DEFAULT 0
- ``created_at`` / ``updated_at`` ISO UTC

Solo stdlib (``sqlite3``) para no acoplar el arranque a ``aiosqlite``.
Wrappers async vía ``run_in_executor`` para no bloquear el loop.
"""
from __future__ import annotations
import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)

STATUS_PENDING = "PENDING"
STATUS_SENDING = "SENDING"
STATUS_ACKED = "ACKED"
STATUS_FAILED = "FAILED"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_events (
    id TEXT PRIMARY KEY,
    event_json TEXT NOT NULL,
    evidence_path TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    retries INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status_created ON pending_events(status, created_at);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventBuffer:
    """Buffer persistente de eventos pendientes de sync."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        # Migra filas legacy con path absoluto → relativo (portable).
        try:
            self.migrate_absolute_evidence_paths()
        except Exception:
            pass

    # -- schema ------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        try:
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
        except OSError as e:
            logger.error("No se pudo inicializar buffer SQLite %s: %s", self.db_path, e)
            raise

    # -- writes (sync, testeables) -----------------------------------
    def enqueue(self, event_id: str, event_json: dict | str,
                evidence_path: Optional[str] = None) -> bool:
        """INSERT OR IGNORE por idempotencia local. Retorna True si insertó.

        ``evidence_path`` siempre se normaliza a relativo ``media/<id>.jpg``
        antes de persistir (portable entre máquinas/usuarios).
        """
        payload = event_json if isinstance(event_json, str) else json.dumps(event_json, ensure_ascii=False)
        now = _utcnow_iso()
        evidence_path = self.normalize_evidence_path(evidence_path)
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO pending_events"
                    " (id, event_json, evidence_path, status, retries, created_at, updated_at)"
                    " VALUES (?, ?, ?, 'PENDING', 0, ?, ?)",
                    (event_id, payload, evidence_path, now, now),
                )
                return cur.rowcount == 1
        except OSError as e:
            logger.error("Buffer enqueue fallido (%s): %s", event_id, e)
            return False

    @staticmethod
    def normalize_evidence_path(stored: Optional[str]) -> Optional[str]:
        """Convierte absoluto legacy o variantes a ``media/<file>.jpg``."""
        try:
            from app.capture.evidence import to_relative_evidence_path
            return to_relative_evidence_path(None, stored)
        except Exception:
            pass
        if not stored:
            return None
        s = str(stored).strip().replace("\\", "/")
        if not s:
            return None
        name = s.rsplit("/", 1)[-1]
        return f"media/{name}" if name else None

    def migrate_absolute_evidence_paths(self) -> int:
        """Reescribe filas con path absoluto → relativo. Retorna nº migradas."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, evidence_path FROM pending_events"
                    " WHERE evidence_path IS NOT NULL").fetchall()
                migrated = 0
                for r in rows:
                    raw = r["evidence_path"]
                    if not raw:
                        continue
                    s = str(raw)
                    # Absoluto Windows/POSIX o con separador de dirs → migrar.
                    is_abs = ("/" in s or "\\" in s) and s != self.normalize_evidence_path(s)
                    # Casos: C:\..., C:/..., /var/..., data\media\...
                    if is_abs or Path(s).is_absolute():
                        rel = self.normalize_evidence_path(s)
                        if rel and rel != s:
                            conn.execute(
                                "UPDATE pending_events SET evidence_path=?, updated_at=? WHERE id=?",
                                (rel, _utcnow_iso(), r["id"]),
                            )
                            migrated += 1
                return migrated
        except OSError as e:
            logger.debug("migrate_absolute_evidence_paths fallido: %s", e)
            return 0

    def mark_sending(self, ids: list[str]) -> None:
        if not ids:
            return
        now = _utcnow_iso()
        try:
            with self._connect() as conn:
                conn.executemany(
                    "UPDATE pending_events SET status='SENDING', updated_at=? WHERE id=?",
                    [(now, i) for i in ids],
                )
        except OSError as e:
            logger.debug("mark_sending fallido: %s", e)

    def mark_acknowledged(self, ids: list[str]) -> int:
        """DELETE inmediato tras ACK 201 (acked + duplicate). Retorna borrados."""
        if not ids:
            return 0
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    f"DELETE FROM pending_events WHERE id IN ({','.join('?' for _ in ids)})",
                    ids,
                )
                return cur.rowcount or 0
        except OSError as e:
            logger.debug("mark_acknowledged fallido: %s", e)
            return 0

    def mark_failed(self, ids: list[str], max_retries: int = 10) -> None:
        """Incrementa retries; >= max_retries pasa a FAILED (retención 7d)."""
        if not ids:
            return
        now = _utcnow_iso()
        try:
            with self._connect() as conn:
                for eid in ids:
                    row = conn.execute(
                        "SELECT retries FROM pending_events WHERE id=?", (eid,)).fetchone()
                    if row is None:
                        continue
                    retries = int(row["retries"] or 0) + 1
                    status = STATUS_FAILED if retries >= max_retries else STATUS_PENDING
                    conn.execute(
                        "UPDATE pending_events SET retries=?, status=?, updated_at=? WHERE id=?",
                        (retries, status, now, eid),
                    )
        except OSError as e:
            logger.debug("mark_failed fallido: %s", e)

    def purge_acknowledged(self) -> int:
        """Limpieza defensiva de残留 ACKED (normalmente ya se hizo DELETE)."""
        try:
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM pending_events WHERE status='ACKED'")
                return cur.rowcount or 0
        except OSError:
            return 0

    def purge_old_failed(self, retention_days: int = 7) -> int:
        """Retención 7d para fallidos (AC-005). Solo toca FAILED, nunca PENDING."""
        try:
            cutoff = (datetime.now(timezone.utc).timestamp() - retention_days * 86400)
            cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
            with self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM pending_events WHERE status='FAILED' AND created_at < ?",
                    (cutoff_iso,),
                )
                return cur.rowcount or 0
        except OSError as e:
            logger.debug("purge_old_failed fallido: %s", e)
            return 0

    def purge_old_evidence_files(self, media_dir: Path, retention_days: int = 7) -> int:
        """Borra JPGs huérfanos/antiguos en data/media (retención evidencia 7d).

        Compara por NOMBRE de archivo (robusto a paths relativos nuevos y
        absolutos legacy): ``media/ev.jpg`` y ``C:/.../media/ev.jpg`` cuentan
        como el mismo.
        """
        removed = 0
        try:
            if not media_dir.exists():
                return 0
            cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400
            # 1) huérfanos: archivo sin fila en buffer (match por nombre,
            # válido para relativos nuevos y absolutos legacy).
            with self._connect() as conn:
                rows = conn.execute("SELECT evidence_path FROM pending_events"
                                    " WHERE evidence_path IS NOT NULL").fetchall()
            referenced = {str(r["evidence_path"]) for r in rows if r["evidence_path"]}
            referenced_names = {str(r["evidence_path"]).replace("\\", "/").rsplit("/", 1)[-1]
                                for r in rows if r["evidence_path"]}
            for jpg in media_dir.glob("*.jpg"):
                try:
                    is_ref = jpg.name in referenced_names or str(jpg) in referenced
                    old = jpg.stat().st_mtime < cutoff
                    if (not is_ref) or old:
                        # Solo borra huérfanos, o referenciados viejos de FAILED ya purgados
                        if not is_ref or old:
                            jpg.unlink(missing_ok=True)
                            removed += 1
                except OSError:
                    continue
        except OSError as e:
            logger.debug("purge_old_evidence_files fallido: %s", e)
        return removed

    # -- reads ---------------------------------------------------------
    def fetch_batch(self, limit: int = 100) -> list[dict[str, Any]]:
        """Lote PENDING más antiguos (ORDER BY created_at ASC). Excluye FAILED."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, event_json, evidence_path, retries, created_at"
                    " FROM pending_events WHERE status='PENDING'"
                    " ORDER BY created_at ASC LIMIT ?",
                    (max(1, min(100, int(limit))),),
                ).fetchall()
        except OSError as e:
            logger.debug("fetch_batch fallido: %s", e)
            return []
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                payload = json.loads(r["event_json"])
            except (ValueError, TypeError):
                payload = {"event_id": r["id"], "_raw": str(r["event_json"])}
            out.append({
                "id": r["id"],
                "event": payload,
                "evidence_path": r["evidence_path"],
                "retries": int(r["retries"] or 0),
                "created_at": r["created_at"],
            })
        return out

    def count_pending(self) -> int:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM pending_events WHERE status IN ('PENDING','SENDING')"
                ).fetchone()
                return int(row["n"] or 0) if row else 0
        except OSError:
            return 0

    def get_retry_delay_sec(self, retries: int, base_sec: int = 60,
                            max_sec: int = 3600) -> int:
        """Backoff exponencial 1m/2m/4m...max 1h (sin jitter aquí; jitter en sync)."""
        try:
            return min(max_sec, base_sec * (2 ** max(0, int(retries))))
        except (ValueError, OverflowError):
            return max_sec

    # -- async wrappers (no bloquean el loop) ---------------------------
    async def aenqueue(self, *args: Any, **kwargs: Any) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.enqueue(*args, **kwargs))

    async def afetch_batch(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.fetch_batch(*args, **kwargs))

    async def acount_pending(self) -> int:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.count_pending)
