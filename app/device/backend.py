"""Cliente HTTP del backend (HU-DEVICE-002 AC-004/005/007 + HU-DEVICE-003 sync).

Solo stdlib (``urllib``) para no acoplar el arranque a dependencias opcionales.
Contratos implementados (HU-API-006 + HU-API-005/ADR-011 + HU-API-007/ADR-005):

- ``POST /api/v1/devices/self-register`` — headers ``X-Provision-Token`` +
  ``Idempotency-Key``, body ``{serialNumber, firmwareVersion}``.
  ``201 {deviceId/device_id, status, apiKey/api_key, claimCode/claim_code}``
  (secretos una sola vez); reintento ``200`` sin secretos (ADR-010).
- ``POST /api/v1/devices/{id}/heartbeat`` — headers ``X-Device-ID`` +
  ``X-API-Key``, body ``{firmware_version, pending_count, free_disk_pct,
  uptime_s}``. Respuesta ``{deviceId, status, lastHeartbeatAt,
  configPending, configVersionAvailable}``.
- ``GET /api/v1/devices/{id}/config`` — HU-API-005/ADR-011 (config global
  versionada). Un ``404`` se trata como "no disponible esta vez" (se reintenta
  en el próximo pull, sin romper el arranque).
- ``HEAD /actuator/health`` (sin auth, sin prefijo /api/v1) — HU-DEVICE-003
  AC-002: healthcheck cada 30s. Solo responde ¿hay internet?
- ``POST /api/v1/telemetry/events`` — HU-DEVICE-003 AC-003/004 (HU-API-007):
  body ``{"events": [...]}`` lote máx 100 solo metadata JSON.
  ``201 {acked_ids[], duplicate_ids[]}`` (duplicados no son error).
- ``POST /api/v1/telemetry/events/{id}/evidence`` — HU-DEVICE-003 fase 2
  (ADR-006): multipart 1 JPG + ``checksum_sha256``. ``201`` ok,
  ``409`` ya existe (no reintentar), ``404`` sin evento.

Seguridad: ningún secreto se loguea (valores reemplazados por ``***``).
"""
from __future__ import annotations
import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional


logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
# Legado HU-API-005 pendiente: antes se desactivaba tras el primer 404.
# Se conserva por compatibilidad con tests, pero ya NO se desactiva:
# el endpoint existe (ADR-011) y un 404 solo significa "reintentar luego".
CONFIG_ENDPOINT_AVAILABLE = True


class BackendError(Exception):
    """Fallo de red/transporte contra el backend (reintentable)."""


class BackendAuthError(BackendError):
    """401/403: credencial inválida o sin permiso (no reintentar igual)."""


class BackendConflictError(BackendError):
    """409: serial duplicado u otro conflicto."""


@dataclass
class HttpResult:
    status: int
    body: dict[str, Any]


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: ("***" if "token" in k.lower() or "key" in k.lower() else v)
            for k, v in headers.items()}


def _do_request(method: str, url: str, headers: dict[str, str],
               payload: Optional[dict], timeout_sec: float) -> HttpResult:
    data = None
    body_headers = dict(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        body_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=body_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8") if resp.length != 0 else ""
            status = resp.status
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="ignore")
        except Exception:
            raw = ""
        status = e.code
        if status in (401, 403):
            raise BackendAuthError(f"backend {status}: {_safe_message(raw)}") from e
        if status == 409:
            raise BackendConflictError(f"backend 409: {_safe_message(raw)}") from e
        if 400 <= status < 500:
            raise BackendError(f"backend {status}: {_safe_message(raw)}") from e
        raise BackendError(f"backend {status}: {_safe_message(raw)}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BackendError(f"sin conexión al backend: {e}") from e
    try:
        body = json.loads(raw) if raw.strip() else {}
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {"data": body}
    return HttpResult(status=status, body=body)


def _safe_message(raw: str) -> str:
    """Mensaje de error sin secretos (nunca incluye headers ni bodies)."""
    text = (raw or "").strip().replace("\n", " ")
    return text[:200] if text else "sin detalle"


def _first(body: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in body and body[key] is not None:
            return body[key]
    data = body.get("data")
    if isinstance(data, dict):
        for key in keys:
            if key in data and data[key] is not None:
                return data[key]
    return None


class BackendClient:
    def __init__(self, api_url: str, timeout_sec: float = 10.0):
        self.base = api_url.rstrip("/")
        self.timeout = timeout_sec

    def _url(self, path: str) -> str:
        return f"{self.base}{API_PREFIX}{path}"

    # -- sync (testeable sin loop) -------------------------------------
    def self_register_sync(self, serial_number: str, firmware_version: str,
                           provision_token: str, idempotency_key: str) -> tuple[bool, dict]:
        """Retorna (created, body). created=True si 201 (trae secretos)."""
        result = _do_request(
            "POST", self._url("/devices/self-register"),
            {"X-Provision-Token": provision_token, "Idempotency-Key": idempotency_key},
            {"serialNumber": serial_number, "firmwareVersion": firmware_version},
            self.timeout,
        )
        if result.status not in (200, 201):
            raise BackendError(f"self-register respondió {result.status}")
        return result.status == 201, result.body

    def heartbeat_sync(self, device_id: str, api_key: str, payload: dict) -> dict:
        result = _do_request(
            "POST", self._url(f"/devices/{device_id}/heartbeat"),
            {"X-Device-ID": device_id, "X-API-Key": api_key},
            payload,
            self.timeout,
        )
        if result.status != 200:
            raise BackendError(f"heartbeat respondió {result.status}")
        return result.body

    def fetch_config_sync(self, device_id: str, api_key: str) -> Optional[dict]:
        try:
            result = _do_request(
                "GET", self._url(f"/devices/{device_id}/config"),
                {"X-Device-ID": device_id, "X-API-Key": api_key},
                None,
                self.timeout,
            )
        except BackendError as e:
            if "404" in str(e):
                logger.info("GET config devolvió 404 para %s; se usa config local y se reintentará",
                            device_id)
                return None
            raise
        if result.status == 404:
            logger.info("GET config devolvió 404 para %s; se usa config local y se reintentará",
                        device_id)
            return None
        if result.status != 200:
            raise BackendError(f"config respondió {result.status}")
        return result.body

    # -- HU-DEVICE-003: conectividad + telemetría ----------------------
    def healthcheck_sync(self, timeout_sec: float = 5.0) -> bool:
        """HEAD /actuator/health sin auth (AC-002). True=online, False=offline.

        Nunca lanza: cualquier error de red/timeout/5xx = offline.
        No usa API_PREFIX (es /actuator/health en raíz).
        """
        url = f"{self.base}/actuator/health"
        req = urllib.request.Request(url, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                return 200 <= resp.status < 400
        except urllib.error.HTTPError as e:
            return 200 <= e.code < 400
        except Exception:
            return False

    def post_events_sync(self, device_id: str, api_key: str,
                         events: list[dict]) -> dict:
        """POST /api/v1/telemetry/events lote máx 100 (AC-003/AC-004).

        Retorna ``{"acked_ids": [...], "duplicate_ids": [...]}``.
        Duplicados no son error (van en ``duplicate_ids`` del 201).
        """
        if not events:
            return {"acked_ids": [], "duplicate_ids": []}
        batch = events[:100]
        result = _do_request(
            "POST", self._url("/telemetry/events"),
            {"X-Device-ID": device_id, "X-API-Key": api_key},
            {"events": batch},
            self.timeout,
        )
        if result.status != 201:
            raise BackendError(f"telemetry/events respondió {result.status}")
        return {
            "acked_ids": _first(result.body, "acked_ids", "ackedIds") or [],
            "duplicate_ids": _first(result.body, "duplicate_ids", "duplicateIds") or [],
        }

    def upload_evidence_sync(self, device_id: str, api_key: str, event_id: str,
                             jpg_path: str, checksum_sha256: str) -> Optional[dict]:
        """POST /events/{id}/evidence multipart 1 JPG (fase 2, ADR-006).

        Retorna body del 201, o ``{"duplicate": True}`` si 409 (ya existe,
        no reintentar). ``404`` (sin evento) lanza BackendError.
        """
        import mimetypes
        import uuid as _uuid
        try:
            with open(jpg_path, "rb") as f:
                content = f.read()
        except OSError as e:
            raise BackendError(f"evidencia ilegible {jpg_path}: {e}") from e
        boundary = f"----SomnGuard{_uuid.uuid4().hex}"
        filename = f"{event_id}.jpg"
        ctype = mimetypes.guess_type(filename)[0] or "image/jpeg"
        CRLF = "\r\n"
        parts: list[bytes] = []
        parts.append(f"--{boundary}{CRLF}".encode())
        parts.append(
            f'Content-Disposition: form-data; name="file"; filename="{filename}"{CRLF}'.encode())
        parts.append(f"Content-Type: {ctype}{CRLF}{CRLF}".encode())
        parts.append(content + CRLF.encode())
        parts.append(f"--{boundary}{CRLF}".encode())
        parts.append(
            f'Content-Disposition: form-data; name="checksum_sha256"{CRLF}{CRLF}'.encode())
        parts.append((checksum_sha256 + CRLF).encode())
        parts.append(f"--{boundary}--{CRLF}".encode())
        body = b"".join(parts)
        headers = {
            "X-Device-ID": device_id,
            "X-API-Key": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        }
        req = urllib.request.Request(
            self._url(f"/telemetry/events/{event_id}/evidence"),
            data=body, headers=headers, method="POST")
        try:
            import json as _json
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8") if resp.length != 0 else ""
                status = resp.status
            try:
                parsed = _json.loads(raw) if raw.strip() else {}
            except ValueError:
                parsed = {}
            if status == 409:
                return {"duplicate": True}
            if status not in (200, 201):
                raise BackendError(f"evidence respondió {status}")
            return parsed if isinstance(parsed, dict) else {}
        except urllib.error.HTTPError as e:
            if e.code == 409:
                return {"duplicate": True}
            if e.code in (401, 403):
                raise BackendAuthError(f"evidence {e.code}") from e
            raise BackendError(f"evidence {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise BackendError(f"sin conexión al backend: {e}") from e

    # -- async wrappers (no bloquean el loop) ---------------------------
    async def self_register(self, *args: Any, **kwargs: Any) -> tuple[bool, dict]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.self_register_sync(*args, **kwargs))

    async def heartbeat(self, *args: Any, **kwargs: Any) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.heartbeat_sync(*args, **kwargs))

    async def fetch_config(self, *args: Any, **kwargs: Any) -> Optional[dict]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.fetch_config_sync(*args, **kwargs))

    async def healthcheck(self, *args: Any, **kwargs: Any) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.healthcheck_sync(*args, **kwargs))

    async def post_events(self, *args: Any, **kwargs: Any) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.post_events_sync(*args, **kwargs))

    async def upload_evidence(self, *args: Any, **kwargs: Any) -> Optional[dict]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.upload_evidence_sync(*args, **kwargs))

    # -- parsing ---------------------------------------------------------
    @staticmethod
    def parse_self_register(body: dict[str, Any], created: bool) -> dict:
        return {
            "device_id": _first(body, "deviceId", "device_id"),
            "status": _first(body, "status") or "DEVICE_REGISTERED",
            "api_key": _first(body, "apiKey", "api_key"),
            "claim_code": _first(body, "claimCode", "claim_code"),
            "created": created,
        }

    @staticmethod
    def parse_heartbeat(body: dict[str, Any]) -> dict:
        return {
            "device_id": _first(body, "deviceId", "device_id"),
            "status": _first(body, "status"),
            "last_heartbeat_at": _first(body, "lastHeartbeatAt", "last_heartbeat_at"),
            "config_pending": _first(body, "configPending", "config_pending", "pendingConfigUpdate", "pending_config_update"),
        }

    @staticmethod
    def parse_telemetry_ack(body: dict[str, Any]) -> dict:
        """Normaliza 201 {acked_ids[], duplicate_ids[]} (camelCase o snake_case)."""
        acked = _first(body, "acked_ids", "ackedIds") or []
        dup = _first(body, "duplicate_ids", "duplicateIds") or []
        return {
            "acked_ids": list(acked) if isinstance(acked, list) else [],
            "duplicate_ids": list(dup) if isinstance(dup, list) else [],
        }
