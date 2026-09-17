"""HU-DEVICE-001 IT4: cinturón AC-004. Sin hardware."""
from app.analysis.seatbelt import SeatbeltDetector, assess_fov_for_belt


def _enabled():
    return SeatbeltDetector({"belt_enabled": True})


def test_disabled_by_default():
    d = SeatbeltDetector({})
    assert d.is_enabled is False
    assert d.update(False, None, 1000.0) == []
    assert d.update(False, None, 1020.0) == []  # desactivado: jamás dispara


def test_not_evaluable_without_model():
    d = _enabled()
    assert d.update(None, None, 1000.0) == []
    assert d.update(None, None, 1020.0) == []  # nunca dispara sin torso/modelo


def test_cin01_after_10s_and_repeats():
    d = _enabled()
    assert d.update(False, None, 1000.0) == []
    evs = d.update(False, None, 1010.5)
    assert [e["event_type_id"] for e in evs] == ["EV-CIN-01"]
    assert evs[0]["alert_code"] == "AS-07"
    assert d.update(False, None, 1012.0) == []  # intermitente c/5s
    evs = d.update(False, None, 1015.6)
    assert any(e["event_type_id"] == "EV-CIN-01" for e in evs)
    # visible resetea
    assert d.update(True, True, 1016.0) == []


def test_cin02_bad_position():
    d = _enabled()
    d.update(True, False, 2000.0)
    evs = d.update(True, False, 2010.5)
    assert [e["event_type_id"] for e in evs] == ["EV-CIN-02"]


def test_fov_heuristic():
    assert assess_fov_for_belt(None, (720, 1280)) is False
    assert assess_fov_for_belt((100, 100, 200, 400), (720, 1280)) is False  # 55% alto
    assert assess_fov_for_belt((100, 100, 200, 150), (720, 1280)) is True  # 20% alto
