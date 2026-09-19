"""HU-DEVICE-001 IT5: pipeline (AC-005/AC-006) + manager no bloqueante."""
import asyncio
import numpy as np

from app.analysis.pipeline import VisionPipeline


class _NoFace:
    async def process(self, frame):
        from app.analysis.detector import DetectionResult
        return DetectionResult(events=[], face_present=False, fov_ok=False, landmarks=None)

    async def load_model(self):
        pass

    def update_thresholds(self, th):
        pass


def _frame():
    return np.zeros((240, 320, 3), dtype=np.uint8)


def test_pipeline_no_face_no_crash_and_metrics():
    async def go():
        p = VisionPipeline({})
        p.obstruction._landmark_detector = _NoFace()
        # Evitar cargar modelo real: stub ya puesto vía _landmark_detector
        orig = p.obstruction.process

        async def stub_process(frame):
            return await _NoFace().process(frame)
        p.obstruction.process = stub_process
        r = await p.process(_frame())
        assert r.events == []
        assert r.metrics["within_budget"] is True
        assert r.metrics["capture_to_validated_sec"] < 2.0
    asyncio.run(go())


def test_pipeline_latency_budget():
    async def go():
        p = VisionPipeline({"frame_budget_sec": 2.0})
        p.obstruction._landmark_detector = _NoFace()

        async def slow(frame):
            await asyncio.sleep(0.01)
            return await _NoFace().process(frame)
        p.obstruction.process = slow
        import time
        t0 = time.monotonic()
        r = await p.process(_frame(), capture_time=t0)
        assert r.metrics["process_sec"] < 2.0
    asyncio.run(go())


def test_pipeline_fallback_as09_on_subfailure():
    async def go():
        p = VisionPipeline({})
        async def boom(frame):
            raise RuntimeError("modelo roto")
        p.obstruction.process = boom
        r1 = await p.process(_frame())
        assert any(e["alert_code"] == "AS-09" for e in r1.events)
        r2 = await p.process(_frame())  # cooldown 30s: no repite
        assert r2.events == []
    asyncio.run(go())


def test_manager_handle_result_nonblocking():
    import app.device.manager as mm
    from app.common.models import DeviceConfig, DeviceIdentity, DeviceState
    from app.monitoring.presence import PresenceMonitor
    import time as _t

    class FakeSound:
        def __init__(self):
            self.played = []

        async def play(self, pattern):
            self.played.append(pattern)

        def update_config(self, c):
            pass

    async def go():
        mgr = mm.DeviceManager.__new__(mm.DeviceManager)
        from app.device.manager import DeviceContext
        mgr.ctx = DeviceContext(identity=DeviceIdentity(serial_number="S", firmware_version="F"),
                                config=DeviceConfig(), env_config={},
                                presence=PresenceMonitor(), sound_player=FakeSound(),
                                boot_start_monotonic=_t.monotonic(),
                                current_state=DeviceState.ACTIVO)
        mgr._shutdown = asyncio.Event()
        mgr._tasks = []
        from app.analysis.detector import DetectionResult
        res = DetectionResult(events=[{"alert_code": "AS-01", "message": "x",
                                       "event_type_id": "EV-SOM-01"}],
                              face_present=True, fov_ok=True)
        await mgr._handle_detection_result(res)
        await asyncio.sleep(0.05)  # background task suena sin bloquear
        assert len(mgr.ctx.sound_player.played) == 1
    asyncio.run(go())
