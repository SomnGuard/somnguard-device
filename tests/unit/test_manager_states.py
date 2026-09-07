"""HU-DEVICE-002: estados, registro, heartbeat y alertas de arranque (sin hw/red)."""
import asyncio
import time

import pytest

import app.device.manager as manager_mod
from app.common.models import AlertCode, DeviceConfig, DeviceIdentity, DeviceState
from app.device.backend import BackendAuthError, BackendClient, BackendError
from app.device.manager import (
    BOOT_BUDGET_SEC,
    DeviceContext,
    DeviceManager,
)
from app.capture.camera import CameraError
from app.monitoring.presence import PresenceMonitor


class FakeSound:
    def __init__(self):
        self.played = []

    async def play(self, pattern):
        self.played.append(pattern)

    def update_config(self, config):
        pass


class FakeBackend(BackendClient):
    def __init__(self, heartbeat_result=None, heartbeat_error=None,
                 register_result=None, register_error=None):
        super().__init__("http://test")
        self.heartbeat_result = heartbeat_result or {"status": "DEVICE_ACTIVE"}
        self.heartbeat_error = heartbeat_error
        self.register_result = register_result
        self.register_error = register_error
        self.heartbeat_calls = []

    async def self_register(self, *args, **kwargs):
        if self.register_error:
            raise self.register_error
        return self.register_result

    async def heartbeat(self, device_id, api_key, payload):
        self.heartbeat_calls.append((device_id, payload))
        if self.heartbeat_error:
            raise self.heartbeat_error
        return self.heartbeat_result

    async def fetch_config(self, *args, **kwargs):
        return None


class FakeDetectorPaused:
    needs_detection_pause = True

    def update_thresholds(self, thresholds):
        pass


def make_manager(monkeypatch, tmp_path, state=DeviceState.REGISTRADO,
                 device_id=None, api_key=None, backend=None, token=""):
    monkeypatch.setenv("SOMNGUARD_DATA_DIR", str(tmp_path))
    identity = DeviceIdentity(serial_number="TEST-SER", firmware_version="0.2.0-test",
                              device_id=device_id, api_key=api_key, state=state)
    mgr = DeviceManager.__new__(DeviceManager)
    mgr.ctx = DeviceContext(
        identity=identity, config=DeviceConfig(),
        env_config={"api_url": "http://test", "provision_token": token,
                    "backend_timeout_sec": 10.0},
        backend=backend or FakeBackend(),
        boot_start_monotonic=time.monotonic(),
        presence=PresenceMonitor(face_absence_timeout_sec=30),
        sound_player=FakeSound(),
    )
    mgr.ctx.presence.on_state_change = mgr._on_presence_change
    mgr._shutdown = asyncio.Event()
    mgr._tasks = []
    return mgr


def run(coro):
    return asyncio.run(coro)


# -- AC-001 ---------------------------------------------------------------
def test_boot_budget_is_60s():
    assert BOOT_BUDGET_SEC == 60.0


def test_camera_ok_plays_as08(monkeypatch, tmp_path):
    class FakeCameraOK:
        def __init__(self, resolution=(1280, 720), fps=30):
            pass

        async def start(self):
            pass

    monkeypatch.setattr(manager_mod, "Camera", FakeCameraOK)
    mgr = make_manager(monkeypatch, tmp_path)
    run(mgr._init_camera())
    assert mgr.ctx.sound_player.played[0].alert_code == AlertCode.AS_08


def test_camera_fail_plays_as09_and_marks_error(monkeypatch, tmp_path):
    class FakeCameraFail:
        def __init__(self, resolution=(1280, 720), fps=30):
            pass

        async def start(self):
            raise CameraError("sin cámara")

    monkeypatch.setattr(manager_mod, "Camera", FakeCameraFail)
    mgr = make_manager(monkeypatch, tmp_path)
    with pytest.raises(CameraError):
        run(mgr._init_camera())
    assert mgr.ctx.sound_player.played[0].alert_code == AlertCode.AS_09
    assert mgr.ctx.current_state == DeviceState.ERROR


# -- AC-003 ---------------------------------------------------------------
def test_presence_drives_activo_espera(monkeypatch, tmp_path):
    async def scenario():
        mgr = make_manager(monkeypatch, tmp_path, state=DeviceState.ESPERA)
        mgr.ctx.current_state = DeviceState.ESPERA
        mgr.ctx.presence.update(True)
        await asyncio.sleep(0.05)
        assert mgr.ctx.current_state == DeviceState.ACTIVO
        # Simular 30s sin rostro: forzar el reloj del monitor.
        mgr.ctx.presence._last_face_seen_monotonic -= 31.0
        mgr.ctx.presence.update(False)
        await asyncio.sleep(0.05)
        assert mgr.ctx.current_state == DeviceState.ESPERA

    run(scenario())


def test_presence_never_leaves_offline(monkeypatch, tmp_path):
    async def scenario():
        mgr = make_manager(monkeypatch, tmp_path, state=DeviceState.OFFLINE)
        mgr.ctx.current_state = DeviceState.OFFLINE
        mgr.ctx.presence.update(True)
        await asyncio.sleep(0.05)
        assert mgr.ctx.current_state == DeviceState.OFFLINE

    run(scenario())


# -- AC-002 ---------------------------------------------------------------
def test_obstruction_pause_transitions_to_espera(monkeypatch, tmp_path):
    async def scenario():
        mgr = make_manager(monkeypatch, tmp_path, state=DeviceState.ACTIVO)
        mgr.ctx.current_state = DeviceState.ACTIVO
        mgr.ctx.detector = FakeDetectorPaused()
        await mgr._check_obstruction_pause()
        assert mgr.ctx.current_state == DeviceState.ESPERA
        assert mgr.ctx.sound_player.played[0].alert_code == AlertCode.AS_09

    run(scenario())


# -- AC-007 ---------------------------------------------------------------
def test_self_register_201_persists_and_assigns(monkeypatch, tmp_path):
    async def scenario():
        mgr = make_manager(monkeypatch, tmp_path)
        ok = await mgr._handle_self_register_result(True, {
            "deviceId": "11111111-2222-3333-4444-555555555555",
            "status": "DEVICE_REGISTERED", "apiKey": "key-abc", "claimCode": "X-Y-Z"})
        assert ok is True
        assert mgr.ctx.identity.device_id == "11111111-2222-3333-4444-555555555555"
        assert mgr.ctx.identity.api_key == "key-abc"
        assert mgr.ctx.current_state == DeviceState.ASIGNADO
        assert mgr.ctx.backend_available is True

    run(scenario())


def test_self_register_200_with_local_key_keeps_operating(monkeypatch, tmp_path):
    async def scenario():
        mgr = make_manager(monkeypatch, tmp_path, device_id="dev-1", api_key="key-1")
        ok = await mgr._handle_self_register_result(False, {
            "deviceId": "dev-1", "status": "DEVICE_REGISTERED",
            "apiKey": None, "claimCode": None})
        assert ok is True
        assert mgr.ctx.identity.api_key == "key-1"  # no se sobrescribe ni se pierde

    run(scenario())


def test_self_register_200_without_key_falls_back_local(monkeypatch, tmp_path):
    async def scenario():
        mgr = make_manager(monkeypatch, tmp_path)
        ok = await mgr._handle_self_register_result(False, {
            "deviceId": "dev-1", "status": "DEVICE_REGISTERED"})
        assert ok is False
        assert mgr.ctx.identity.device_id.startswith("local-")
        assert mgr.ctx.backend_available is False

    run(scenario())


# -- AC-004 ---------------------------------------------------------------
def test_heartbeat_payload_matches_api_contract(monkeypatch, tmp_path):
    mgr = make_manager(monkeypatch, tmp_path, device_id="dev-1", api_key="k")
    payload = mgr._heartbeat_payload().to_request_dict()
    assert set(payload) == {"firmware_version", "pending_count", "free_disk_pct", "uptime_s"}


def test_heartbeat_recovers_from_offline(monkeypatch, tmp_path):
    async def scenario():
        mgr = make_manager(monkeypatch, tmp_path, state=DeviceState.OFFLINE,
                           device_id="dev-1", api_key="k",
                           backend=FakeBackend(heartbeat_result={"status": "DEVICE_ACTIVE"}))
        mgr.ctx.current_state = DeviceState.OFFLINE
        mgr.ctx.pre_offline_state = DeviceState.ESPERA
        mgr.ctx.presence.mark_absent()
        await mgr._heartbeat_once()
        assert mgr.ctx.backend_available is True
        assert mgr.ctx.current_state == DeviceState.ESPERA
        device_id, payload = mgr.ctx.backend.heartbeat_calls[0]
        assert device_id == "dev-1"
        assert "uptime_s" in payload

    run(scenario())


def test_heartbeat_network_failure_goes_offline(monkeypatch, tmp_path):
    async def scenario():
        mgr = make_manager(monkeypatch, tmp_path, state=DeviceState.ACTIVO,
                           device_id="dev-1", api_key="k",
                           backend=FakeBackend(heartbeat_error=BackendError("red caída")))
        mgr.ctx.current_state = DeviceState.ACTIVO
        await mgr._heartbeat_once()
        assert mgr.ctx.backend_available is False
        assert mgr.ctx.current_state == DeviceState.OFFLINE

    run(scenario())


def test_heartbeat_401_keeps_state_and_reports(monkeypatch, tmp_path):
    async def scenario():
        mgr = make_manager(monkeypatch, tmp_path, state=DeviceState.ACTIVO,
                           device_id="dev-1", api_key="vieja",
                           backend=FakeBackend(heartbeat_error=BackendAuthError("401")))
        mgr.ctx.current_state = DeviceState.ACTIVO
        await mgr._heartbeat_once()
        assert mgr.ctx.current_state == DeviceState.ACTIVO  # no es caída de red

    run(scenario())


def test_backend_suspended_pauses_detection(monkeypatch, tmp_path):
    async def scenario():
        mgr = make_manager(monkeypatch, tmp_path, state=DeviceState.ACTIVO,
                           device_id="dev-1", api_key="k",
                           backend=FakeBackend(heartbeat_result={"status": "DEVICE_SUSPENDED"}))
        mgr.ctx.current_state = DeviceState.ACTIVO
        await mgr._heartbeat_once()
        assert mgr.ctx.current_state == DeviceState.SUSPENDIDO

    run(scenario())
