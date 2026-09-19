"""HU-DEVICE-003 integración manager: enqueue, pending_count, connectivity."""
import asyncio
from types import SimpleNamespace

from app.common.models import DeviceState


def _mgr_with_buffer(monkeypatch, tmp_path, make_manager):
    import app.device.manager as manager_mod
    from app.storage.buffer import EventBuffer
    mgr = make_manager(monkeypatch, tmp_path, state=DeviceState.ACTIVO,
                       device_id="dev-1", api_key="k")
    mgr.ctx.current_state = DeviceState.ACTIVO
    mgr.ctx.buffer = EventBuffer(tmp_path / "db" / "t.db")
    return mgr, manager_mod


def test_handle_detection_enqueues_with_uuid_and_telemetry_shape(
        monkeypatch, tmp_path):
    import sys
    sys.path.insert(0, "tests/unit")
    from test_manager_states import make_manager
    mgr, _ = _mgr_with_buffer(monkeypatch, tmp_path, make_manager)

    async def scenario():
        res = SimpleNamespace(
            events=[{"event_type_id": "EV-SOM-02", "alert_code": "AS-02",
                     "severity": "MODERADA", "message": "cierre"}],
            face_present=True, fov_ok=True, metrics={})
        await mgr._handle_detection_result(res, frame=None)
        await asyncio.sleep(0.05)
        assert mgr.ctx.buffer.count_pending() == 1
        batch = mgr.ctx.buffer.fetch_batch()
        ev = batch[0]["event"]
        assert ev["event_id"]  # UUID generado
        assert ev["event_type"] == "EV-SOM-02"
        assert ev["severity"] == "MODERADA"
        assert "has_evidence" in ev

    asyncio.run(scenario())


def test_priority_suppressed_sound_still_buffers(monkeypatch, tmp_path):
    import sys
    sys.path.insert(0, "tests/unit")
    from test_manager_states import make_manager
    mgr, _ = _mgr_with_buffer(monkeypatch, tmp_path, make_manager)

    async def scenario():
        big = SimpleNamespace(
            events=[{"event_type_id": "EV-SOM-05", "alert_code": "AS-04",
                     "severity": "CRITICA", "message": "micro"}],
            face_present=True, fov_ok=True, metrics={})
        small = SimpleNamespace(
            events=[{"event_type_id": "EV-SOM-02", "alert_code": "AS-02",
                     "severity": "MODERADA", "message": "cierre"}],
            face_present=True, fov_ok=True, metrics={})
        await mgr._handle_detection_result(big, frame=None)
        await asyncio.sleep(0.05)
        await mgr._handle_detection_result(small, frame=None)  # sonido suprimido
        await asyncio.sleep(0.05)
        assert mgr.ctx.buffer.count_pending() == 2  # ambos en buffer igual

    asyncio.run(scenario())


def test_heartbeat_reports_real_pending(monkeypatch, tmp_path):
    import sys
    sys.path.insert(0, "tests/unit")
    from test_manager_states import make_manager
    mgr, _ = _mgr_with_buffer(monkeypatch, tmp_path, make_manager)
    mgr.ctx.buffer.enqueue("x", {"event_id": "x"}, None)
    mgr.ctx.buffer.enqueue("y", {"event_id": "y"}, None)
    payload = mgr._heartbeat_payload().to_request_dict()
    assert payload["pending_count"] == 2
