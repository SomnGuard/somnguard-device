"""AC-003: Activo <-> Espera (sin rostro >30s / rostro detectado)."""
from app.monitoring.presence import PresenceMonitor


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_initial_face_sets_present_and_notifies(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr("app.monitoring.presence.time.monotonic", clock)
    seen = []
    m = PresenceMonitor(face_absence_timeout_sec=30)
    m.on_state_change = seen.append
    assert m.update(True) is True
    assert m.face_present is True
    assert seen == [True]


def test_absence_under_timeout_does_not_transition(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr("app.monitoring.presence.time.monotonic", clock)
    seen = []
    m = PresenceMonitor(face_absence_timeout_sec=30)
    m.on_state_change = seen.append
    m.update(True)
    clock.now += 29.9
    assert m.update(False) is None
    assert m.face_present is True
    assert seen == [True]


def test_absence_over_30s_transitions_to_espera(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr("app.monitoring.presence.time.monotonic", clock)
    seen = []
    m = PresenceMonitor(face_absence_timeout_sec=30)
    m.on_state_change = seen.append
    m.update(True)
    clock.now += 30.0
    assert m.update(False) is False
    assert m.face_present is False
    assert seen == [True, False]


def test_face_returns_to_activo_immediately(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr("app.monitoring.presence.time.monotonic", clock)
    m = PresenceMonitor(face_absence_timeout_sec=30)
    m.update(True)
    clock.now += 45.0
    m.update(False)
    assert m.face_present is False
    assert m.update(True) is True
    assert m.face_present is True


def test_never_seen_face_stays_absent_without_spurious_event(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr("app.monitoring.presence.time.monotonic", clock)
    seen = []
    m = PresenceMonitor(face_absence_timeout_sec=30)
    m.on_state_change = seen.append
    assert m.update(False) is None
    assert m.face_present is False
    assert seen == []
