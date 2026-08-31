from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from typing import Optional

from app.common.models import DeviceIdentity, DeviceState
from app.common.config import load_env_config


IDENTITY_FILE = Path("data") / "device_identity.json"


def _get_serial_number() -> str:
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if line.startswith("Serial"):
                    return line.split(":")[1].strip()
    except Exception:
        pass
    return "unknown-" + hashlib.md5(os.urandom(16)).hexdigest()[:12]


def _get_firmware_version() -> str:
    try:
        with open("VERSION", "r") as f:
            return f.read().strip()
    except Exception:
        return "0.1.0-dev"


def _hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()[:32]


def load_or_create_identity() -> DeviceIdentity:
    env = load_env_config()
    api_key = env.get("api_key", "")
    api_key_hash = _hash_api_key(api_key) if api_key else ""

    if IDENTITY_FILE.exists():
        with open(IDENTITY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        identity = DeviceIdentity(
            serial_number=data.get("serial_number", _get_serial_number()),
            api_key_hash=data.get("api_key_hash", api_key_hash),
            firmware_version=data.get("firmware_version", _get_firmware_version()),
            device_id=data.get("device_id"),
            state=DeviceState(data.get("state", DeviceState.REGISTRADO.value)),
        )
        if api_key_hash and identity.api_key_hash != api_key_hash:
            identity.api_key_hash = api_key_hash
            save_identity(identity)
        return identity

    identity = DeviceIdentity(
        serial_number=_get_serial_number(),
        api_key_hash=api_key_hash,
        firmware_version=_get_firmware_version(),
        state=DeviceState.REGISTRADO,
    )
    save_identity(identity)
    return identity


def save_identity(identity: DeviceIdentity) -> None:
    IDENTITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "serial_number": identity.serial_number,
        "api_key_hash": identity.api_key_hash,
        "firmware_version": identity.firmware_version,
        "device_id": identity.device_id,
        "state": identity.state.value,
    }
    with open(IDENTITY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def update_device_id(identity: DeviceIdentity, device_id: str) -> DeviceIdentity:
    identity.device_id = device_id
    identity.state = DeviceState.ASIGNADO
    save_identity(identity)
    return identity


def update_state(identity: DeviceIdentity, state: DeviceState) -> DeviceIdentity:
    identity.state = state
    save_identity(identity)
    return identity