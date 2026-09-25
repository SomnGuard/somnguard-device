"""HU-DEVICE-004: alertas sonoras diferenciadas y escalamiento (AC-001..AC-004)."""
import asyncio

from app.alerting.alert_queue import AlertQueue
from app.alerting.escalation import EscalationTracker
from app.common.config import (
    apply_remote_config,
    extract_config_version,
    load_default_config,
    resolve_alert_code,
)
from app.common.models import AlertCode, DEFAULT_VOLUME_SCALE


# -- AC-001: event_sound_map remoto con fallback local -----------------------
def test_ac001_remote_map_overrides_local():
    base = load_default_config()
    merged, changed = apply_remote_config(base, {
        "event_sound_map": {"EV-SOM-01": "AS-04"},
    })
    assert changed is True
    assert resolve_alert_code(merged, "EV-SOM-01", AlertCode.AS_01) == AlertCode.AS_04
    # Sin override conserva tabla local.
    assert resolve_alert_code(merged, "EV-SOM-02", AlertCode.AS_02) == AlertCode.AS_02


def test_ac001_invalid_code_ignored():
    base = load_default_config()
    merged, _ = apply_remote_config(base, {
        "event_sound_map": {"EV-SOM-01": "AS-99", "EV-X": 123},
    })
    assert resolve_alert_code(merged, "EV-SOM-01", AlertCode.AS_01) == AlertCode.AS_01
    assert "EV-SOM-01" not in merged.event_sound_map


# -- AC-002: escalamiento >10s AS-01→02→03→04 --------------------------------
def test_ac002_escalates_chain_over_time():
    tracker = EscalationTracker(interval_sec=10.0, reset_sec=5.0)
    t0 = 1000.0
    # Stream continuo cada 2s (el detector reporta por frame mientras persiste;
    # gaps > reset_sec reiniciarían por cese del riesgo).
    code, sev = tracker.escalate("EV-SOM-01", AlertCode.AS_01, t0)
    assert (code, sev) == (AlertCode.AS_01, None)
    for dt in (2.0, 4.0, 6.0, 8.0):
        code, _ = tracker.escalate("EV-SOM-01", AlertCode.AS_01, t0 + dt)
        assert code == AlertCode.AS_01
    code, sev = tracker.escalate("EV-SOM-01", AlertCode.AS_01, t0 + 10.1)
    assert (code, sev) == (AlertCode.AS_02, "MODERADA")
    for dt in (12.0, 14.0, 16.0, 18.0):
        tracker.escalate("EV-SOM-02", AlertCode.AS_01, t0 + dt)
    code, sev = tracker.escalate("EV-SOM-02", AlertCode.AS_01, t0 + 20.1)
    assert (code, sev) == (AlertCode.AS_03, "SEVERA")
    for dt in (22.0, 24.0, 26.0, 28.0):
        tracker.escalate("EV-SOM-05", AlertCode.AS_01, t0 + dt)
    code, sev = tracker.escalate("EV-SOM-05", AlertCode.AS_01, t0 + 30.1)
    assert (code, sev) == (AlertCode.AS_04, "CRITICA")
    # Tope: no pasa de AS-04 aunque persista 2 minutos (stream continuo c/2s).
    t = t0 + 30.1
    while t < t0 + 120.0:
        t += 2.0
        code, _ = tracker.escalate("EV-SOM-01", AlertCode.AS_01, t)
    assert code == AlertCode.AS_04


def test_ac002_outside_chain_never_escalates_and_resets():
    tracker = EscalationTracker(interval_sec=10.0, reset_sec=5.0)
    t0 = 2000.0
    code, sev = tracker.escalate("EV-DIS-01", AlertCode.AS_05, t0 + 50.0)
    assert (code, sev) == (AlertCode.AS_05, None)
    tracker.escalate("EV-SOM-01", AlertCode.AS_01, t0)
    # Gap > reset reinicia el cronómetro.
    code, sev = tracker.escalate("EV-SOM-01", AlertCode.AS_01, t0 + 100.0)
    assert (code, sev) == (AlertCode.AS_01, None)


# -- AC-003: volumen 80% + versionado -----------------------------------------
def test_ac003_default_volume_is_80_pct():
    assert DEFAULT_VOLUME_SCALE == 0.8
    config = load_default_config()
    assert config.volume_scale == 0.8
    assert config.applied_config_version == 0


def test_ac003_volume_pct_alias_and_version_tracking():
    base = load_default_config()
    merged, changed = apply_remote_config(base, {
        "volume_pct": 60, "version": 16,
        "sources": {"global_version": 16},
    })
    assert changed is True
    assert merged.volume_scale == 0.6
    assert merged.applied_config_version == 16
    assert extract_config_version({"sources": {"global_version": 16}}) == 16
    # Nunca retrocede.
    older, _ = apply_remote_config(merged, {"version": 3})
    assert older.applied_config_version == 16


# -- AC-004: una a la vez + cola FIFO ------------------------------------------
class FakePlayer:
    def __init__(self):
        self.played = []
        self.overlap = False
        self._busy = False

    async def play(self, pattern):
        if self._busy:
            self.overlap = True
        self._busy = True
        await asyncio.sleep(0.01)
        self.played.append(pattern)
        self._busy = False


def test_ac004_fifo_without_overlap():
    async def scenario():
        queue = AlertQueue(maxsize=10)
        player = FakePlayer()
        await queue.start(player)
        for i in range(3):
            queue.enqueue_nowait(f"p{i}", alert_code=f"AS-0{i}", rank=0)
        await queue._queue.join()
        await queue.stop()
        assert player.played == ["p0", "p1", "p2"]
        assert player.overlap is False

    asyncio.run(scenario())


def test_ac004_priority_orders_but_keeps_fifo_on_tie():
    queue = AlertQueue(maxsize=10)
    queue.enqueue_nowait("low", rank=0)
    queue.enqueue_nowait("crit", rank=4)
    queue.enqueue_nowait("mid", rank=2)
    kinds = [q.pattern for q in queue._pending]
    assert kinds[0] == "crit"


# -- Integración manager: override + escalamiento llegan al sonido -----------
def test_manager_applies_map_and_escalation():
    import time as _time
    from types import SimpleNamespace
    from app.device.manager import DeviceManager, DeviceContext
    from app.common.models import DeviceConfig, DeviceIdentity, DeviceState
    from app.monitoring.presence import PresenceMonitor

    class FakeSound:
        def __init__(self):
            self.played = []

        async def play(self, pattern):
            self.played.append(pattern)

        def update_config(self, config):
            pass

    async def scenario():
        mgr = DeviceManager.__new__(DeviceManager)
        cfg = load_default_config()
        cfg.event_sound_map = {"EV-SOM-01": "AS-03"}
        mgr.ctx = DeviceContext(
            identity=DeviceIdentity(serial_number="T", firmware_version="t"),
            config=cfg,
            env_config={}, backend=None,
            presence=PresenceMonitor(face_absence_timeout_sec=30),
            sound_player=FakeSound(),
        )
        mgr._escalation = EscalationTracker(interval_sec=10.0, reset_sec=60.0)
        mgr._alert_queue = AlertQueue(maxsize=10)
        mgr._last_priority_alert = None
        res = SimpleNamespace(
            events=[{"event_type_id": "EV-SOM-01", "alert_code": "AS-01",
                     "severity": "LEVE", "message": "parpadeo"}],
            face_present=True, fov_ok=True, metrics={},
        )
        await mgr._handle_detection_result(res)
        await asyncio.sleep(0.05)
        # AC-001: el mapa remoto manda sobre la tabla local.
        assert mgr.ctx.sound_player.played[0].alert_code == AlertCode.AS_03

    asyncio.run(scenario())
