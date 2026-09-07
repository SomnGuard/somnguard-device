"""Identidad del dispositivo: serial, firmware y credenciales (AC-006/AC-007).

- ``serial_number``: override ``SOMNGUARD_SERIAL_NUMBER`` > serial CPU
  (``/proc/cpuinfo``, ``/sys/class/dmi/id/product_uuid``) > identificador
  estable persistido (no aleatorio en cada arranque).
- ``firmware_version``: override env > archivo ``VERSION`` > ``pyproject.toml``.
  Se refresca en cada arranque (el software pudo actualizarse).
- Credenciales operativas ``device_id + api_key``: se persisten en
  ``<data_dir>/device_identity.json`` con modo ``0o600`` (ADR-010: tras guardar,
  el device deja de usar el provision token). Escritura atómica.
- El provision token **nunca** se persiste ni se loguea: solo vive en memoria
  (variable de entorno ``SOMNGUARD_PROVISION_TOKEN``).
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
import platform
import socket
import tempfile
from pathlib import Path
from typing import Optional

from app.common.models import DeviceIdentity, DeviceState
from app.common.config import load_dotenv, load_env_config


logger = logging.getLogger(__name__)

IDENTITY_FILENAME = "device_identity.json"
PROJECT_ROOT = Path(__file__).parent.parent.parent

# Estados volátiles de runtime que NO deben restaurarse tras reinicio.
VOLATILE_STATES = frozenset({
    DeviceState.ACTIVO,
    DeviceState.ESPERA,
    DeviceState.OFFLINE,
    DeviceState.ERROR,
})


def data_dir() -> Path:
    env = load_env_config()
    configured = Path(env.get("data_dir", "data"))
    return configured if configured.is_absolute() else PROJECT_ROOT / configured


def identity_file() -> Path:
    return data_dir() / IDENTITY_FILENAME


def stable_idempotency_key(serial_number: str) -> str:
    """Clave estable por serial para reintentos de self-register (AC-007)."""
    digest = hashlib.sha256(serial_number.encode("utf-8")).hexdigest()[:16]
    return f"self-register-{digest}"


def _read_cpu_serial() -> Optional[str]:
    for candidate in ("/proc/cpuinfo", "/sys/class/dmi/id/product_uuid"):
        try:
            with open(candidate, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            if "cpuinfo" in candidate:
                for line in content.splitlines():
                    if line.startswith("Serial"):
                        serial = line.split(":", 1)[1].strip().strip("\x00")
                        if serial and serial != "0000000000000000":
                            return serial
            else:
                serial = content.strip().strip("\x00")
                if serial:
                    return serial
        except OSError:
            continue
    return None


def _stable_machine_fallback() -> str:
    """Fallback estable (persistido al primer arranque, no aleatorio ciego)."""
    node = platform.node() or socket.gethostname() or "somnguard"
    raw = f"{node}-{platform.machine()}".encode("utf-8", errors="ignore")
    return "dev-" + hashlib.sha256(raw).hexdigest()[:12]


def get_serial_number() -> str:
    override = (os.getenv("SOMNGUARD_SERIAL_NUMBER", "") or "").strip()
    if override:
        return override
    serial = _read_cpu_serial()
    return serial or _stable_machine_fallback()


def _read_pyproject_version() -> Optional[str]:
    try:
        with open(PROJECT_ROOT / "pyproject.toml", "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("version"):
                    _, _, value = stripped.partition("=")
                    return value.strip().strip("\"'")
    except OSError:
        pass
    return None


def get_firmware_version() -> str:
    override = (os.getenv("SOMNGUARD_FIRMWARE_VERSION", "") or "").strip()
    if override:
        return override
    for candidate in (PROJECT_ROOT / "VERSION", Path("VERSION")):
        try:
            version = candidate.read_text(encoding="utf-8").strip()
            if version:
                return version
        except OSError:
            continue
    return _read_pyproject_version() or "0.1.0-dev"


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _secure_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".identity-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            logger.debug("No se pudo aplicar chmod 600 a %s (plataforma sin soporte)", path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        except OSError:
            pass


def save_identity(identity: DeviceIdentity) -> None:
    _secure_write(identity_file(), {
        "serial_number": identity.serial_number,
        "firmware_version": identity.firmware_version,
        "api_key_hash": identity.api_key_hash or (hash_api_key(identity.api_key) if identity.api_key else ""),
        "api_key": identity.api_key or "",
        "device_id": identity.device_id,
        "state": identity.state.value,
    })


def _parse_state(value: object) -> DeviceState:
    try:
        state = DeviceState(str(value))
    except ValueError:
        return DeviceState.REGISTRADO
    # Los estados de runtime no sobreviven al reinicio: se re-derivan en boot.
    if state in VOLATILE_STATES:
        return DeviceState.REGISTRADO
    return state


def load_or_create_identity() -> DeviceIdentity:
    load_dotenv()  # el serial/fw del .env deben verse antes de leer el entorno
    env = load_env_config()
    serial = get_serial_number()
    firmware = get_firmware_version()
    env_api_key = (env.get("api_key", "") or "").strip()
    env_device_id = (env.get("device_id", "") or "").strip()

    path = identity_file()
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning("Identidad local ilegible (%s); se recrea sin credenciales", e)
            data = {}
        # El serial de laboratorio puede cambiar por env; el de hardware manda
        # salvo override explícito.
        persisted_serial = data.get("serial_number") or serial
        if os.getenv("SOMNGUARD_SERIAL_NUMBER"):
            persisted_serial = serial
        identity = DeviceIdentity(
            serial_number=persisted_serial,
            firmware_version=firmware,  # siempre fresca del software actual
            api_key_hash=data.get("api_key_hash", ""),
            api_key=data.get("api_key") or None,
            device_id=data.get("device_id"),
            state=_parse_state(data.get("state", DeviceState.REGISTRADO.value)),
        )
        changed = False
        # Migración: antes solo se guardaba el hash; si el env trae la key real,
        # se adopta para poder operar (una vez guardada, manda el archivo).
        if not identity.api_key and env_api_key:
            identity.api_key = env_api_key
            identity.api_key_hash = hash_api_key(env_api_key)
            if env_device_id and not identity.device_id:
                identity.device_id = env_device_id
            changed = True
        elif identity.api_key and not identity.api_key_hash:
            identity.api_key_hash = hash_api_key(identity.api_key)
            changed = True
        if identity.firmware_version != data.get("firmware_version"):
            changed = True
        if changed:
            save_identity(identity)
        return identity

    identity = DeviceIdentity(
        serial_number=serial,
        firmware_version=firmware,
        api_key_hash=hash_api_key(env_api_key) if env_api_key else "",
        api_key=env_api_key or None,
        device_id=env_device_id or None,
        state=DeviceState.REGISTRADO,
    )
    save_identity(identity)
    return identity


def save_credentials(identity: DeviceIdentity, device_id: str, api_key: str) -> DeviceIdentity:
    """Persiste device_id + api_key tras self-register 201 (AC-007).

    A partir de aquí el device opera con la API key y deja de usar el token.
    """
    identity.device_id = device_id
    identity.api_key = api_key
    identity.api_key_hash = hash_api_key(api_key)
    if identity.state == DeviceState.REGISTRADO:
        identity.state = DeviceState.ASIGNADO
    save_identity(identity)
    return identity


def update_device_id(identity: DeviceIdentity, device_id: str) -> DeviceIdentity:
    identity.device_id = device_id
    if identity.state == DeviceState.REGISTRADO:
        identity.state = DeviceState.ASIGNADO
    save_identity(identity)
    return identity


def update_state(identity: DeviceIdentity, state: DeviceState) -> DeviceIdentity:
    identity.state = state
    # Los estados volátiles no se persisten como estado estable: se guardan
    # igual por observabilidad local, pero _parse_state los resetea en boot.
    save_identity(identity)
    return identity
