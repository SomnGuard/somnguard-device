"""Cliente HTTP del backend (HU-DEVICE-002 AC-004/005/007).

Solo stdlib (``urllib``) para no acoplar el arranque a dependencias opcionales.
Contratos implementados (HU-API-006):

- ``POST /api/v1/devices/self-register`` — headers ``X-Provision-Token`` +
  ``Idempotency-Key``, body ``{serialNumber, firmwareVersion}``.
  ``201 {deviceId/device_id, status, apiKey/api_key, claimCode/claim_code}``
  (secretos una sola vez); reintento ``200`` sin secretos (ADR-010).
- ``POST /api/v1/devices/{id}/heartbeat`` — headers ``X-Device-ID`` +
  ``X-API-Key``, body ``{firmware_version, pending_count, free_disk_pct,
  uptime_s}``. Respuesta ``{deviceId, status, lastHeartbeatAt}``.
- ``GET /api/v1/devices/{id}/config`` — HU-API-005 (aún no implementada en el
  backend: un ``404`` se trata como "no disponible", sin romper el arranque).

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
CONFIG_ENDPOINT_AVAILABLE = True  # se desactiva tras el primer 404 (HU-API-005 pendiente)


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
        global CONFIG_ENDPOINT_AVAILABLE
        if not CONFIG_ENDPOINT_AVAILABLE:
            return None
        try:
            result = _do_request(
                "GET", self._url(f"/devices/{device_id}/config"),
                {"X-Device-ID": device_id, "X-API-Key": api_key},
                None,
                self.timeout,
            )
        except BackendError as e:
            if "404" in str(e):
                CONFIG_ENDPOINT_AVAILABLE = False
                logger.info("GET config aún no disponible en backend (HU-API-005 pendiente); se usa config local")
                return None
            raise
        if result.status == 404:
            CONFIG_ENDPOINT_AVAILABLE = False
            logger.info("GET config aún no disponible en backend (HU-API-005 pendiente); se usa config local")
            return None
        if result.status != 200:
            raise BackendError(f"config respondió {result.status}")
        return result.body

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
        }
