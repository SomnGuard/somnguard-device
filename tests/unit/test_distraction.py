"""HU-DEVICE-001 IT3: distracción AC-003. Sin hardware."""
from app.analysis.distraction import (
    DistractionDetector,
    GazeEstimator,
    MovementEstimator,
    PhoneDetector,
    is_gaze_off_axis,
)
import numpy as np


def test_gaze_threshold():
    assert is_gaze_off_axis(35, 0, 30) is True
    assert is_gaze_off_axis(10, 5, 30) is False


def test_phone_stub_safe_default():
    p = PhoneDetector()
    assert p.detect(None) == (False, 0.0)
    p.inject_present(True)
    assert p.detect(None)[0] is True


def test_phone_missing_model_is_safe():
    import numpy as np

    p = PhoneDetector(confidence_min=0.7, model_path="models/no-existe-test.tflite")
    assert p.load() is False
    assert p.available is False
    assert p.detect(np.zeros((64, 64, 3), dtype=np.uint8)) == (False, 0.0)


def test_movement_estimator_needs_two_frames():
    m = MovementEstimator(diff_threshold=1.0)
    f = np.zeros((64, 64, 3), dtype=np.uint8)
    assert m.update(f) is False  # primer frame: referencia
    assert m.update(f) is False  # sin cambio
    assert m.update(f + 100) is True  # cambio brusco


def test_phone_2s_emits_dis01_then_5s_dis02_repeat():
    d = DistractionDetector({})
    assert d.update(True, False, False, 1000.0) == []
    evs = d.update(True, False, False, 1002.5)
    assert [e["event_type_id"] for e in evs] == ["EV-DIS-01"]
    assert evs[0]["alert_code"] == "AS-05"
    # prolongado a 5s
    evs = d.update(True, False, False, 1005.5)
    assert any(e["event_type_id"] == "EV-DIS-02" for e in evs)
    # repite c/3s
    assert d.update(True, False, False, 1006.5) == []
    evs = d.update(True, False, False, 1008.6)
    assert any(e["event_type_id"] == "EV-DIS-02" for e in evs)
    # soltar resetea
    assert d.update(False, False, False, 1009.0) == []
    assert d.update(True, False, False, 1009.5) == []


def test_gaze_3s_dis03_5s_dis04():
    d = DistractionDetector({})
    d.update(False, True, False, 2000.0)
    evs = d.update(False, True, False, 2003.5)
    assert any(e["event_type_id"] == "EV-DIS-03" for e in evs)
    assert any(e["alert_code"] == "AS-06" for e in evs)
    evs = d.update(False, True, False, 2005.5)
    assert any(e["event_type_id"] == "EV-DIS-04" for e in evs)


def test_movement_3s_dis05():
    d = DistractionDetector({})
    d.update(False, False, True, 3000.0)
    evs = d.update(False, False, True, 3003.5)
    assert [e["event_type_id"] for e in evs] == ["EV-DIS-05"]


def test_gaze_single_spike_does_not_latch():
    """Regresión FP: un spike de un frame a 32° no latcha off (EMA+histéresis)."""
    g = GazeEstimator(deviation_deg=30.0, calib_samples=0)
    for y in (5.0, 8.0, 6.0, 7.0):
        assert g.update_raw(y) is False
    assert g.update_raw(32.0) is False  # spike aislado: suavizado lo absorbe
    assert g.update_raw(6.0) is False


def test_gaze_sustained_deviation_latches():
    """Desviación real sostenida sí latcha."""
    g = GazeEstimator(deviation_deg=30.0, smooth_alpha=1.0, calib_samples=0)  # sin suavizado
    assert g.update_raw(35.0) is True
    assert g.update_raw(35.0) is True
    # Histéresis: baja a 25° (entre exit 22° y enter 30°) y sigue off.
    assert g.update_raw(25.0) is True
    assert g.update_raw(10.0) is False


def test_gaze_calibration_absorbs_mount_bias():
    """Sesgo sostenido de montaje (yaw +35°) se vuelve neutro tras calibrar."""
    g = GazeEstimator(deviation_deg=30.0, smooth_alpha=1.0, calib_samples=10)
    for _ in range(10):
        assert g.update_raw(35.0) is False  # calibrando: nunca alerta
    assert g.calibrated is True
    # Misma lectura post-calibración: desviación relativa ~0 → no off.
    assert g.update_raw(35.0) is False
    assert g.update_raw(36.0) is False
    # Desviación real sobre el neutro sí dispara.
    assert g.update_raw(70.0) is True


def test_gaze_prolonged_capped_per_episode():
    """Un episodio no repite AS-06 eternamente: tope y silencio hasta re-centrar."""
    d = DistractionDetector({"prolonged_max_repeats": 3})
    t = 6000.0
    seen = []
    for i in range(30):
        seen += d.update(False, True, False, t)
        t += 3.0
    prol = [e for e in seen if e["event_type_id"] == "EV-DIS-04"]
    assert len(prol) == 3
    # Tras re-centrar (>gracia) el episodio se cierra y puede volver a alertar.
    d.update(False, False, False, t)
    d.update(False, False, False, t + 1.0)
    assert d.update(False, True, False, t + 1.5) == []


def test_gaze_brief_gap_does_not_split_episode():
    """Un frame suelto en eje no reinicia el temporizador (gracia 0.4s)."""
    d = DistractionDetector({})
    d.update(False, True, False, 4000.0)
    d.update(False, True, False, 4002.0)
    d.update(False, False, False, 4002.2)  # hueco 0.2s < gracia
    evs = d.update(False, True, False, 4003.5)
    assert any(e["event_type_id"] == "EV-DIS-03" for e in evs)


def test_gaze_long_gap_resets():
    d = DistractionDetector({})
    d.update(False, True, False, 5000.0)
    d.update(False, False, False, 5000.5)
    evs = d.update(False, False, False, 5001.5)  # hueco 1s > gracia: resetea
    assert evs == []
    assert d.update(False, True, False, 5002.0) == []  # episodio nuevo, sin duración


def test_degenerate_pose_ignored_by_gaze():
    """Secuencia del log de campo: flips (pitch −179°, yaw −157°) no latchan."""
    g = GazeEstimator(deviation_deg=30.0, calib_samples=0)
    for yaw, pitch in [(2, -179), (18, -179), (4, -178), (-157, -5), (3, 1), (2, -179)]:
        assert g.update_raw(yaw, pitch) is False


def test_degenerate_pose_skipped_in_calibration():
    """La basura no contamina la mediana del neutro."""
    g = GazeEstimator(deviation_deg=30.0, smooth_alpha=1.0, calib_samples=4)
    g.update_raw(35.0, 0.0)
    g.update_raw(2.0, -179.0)  # flip: se ignora, no consume muestra
    g.update_raw(35.0, 0.0)
    g.update_raw(36.0, 0.0)
    g.update_raw(34.0, 0.0)
    assert g.calibrated is True
    assert g._base_yaw == 35.0  # mediana de [35,35,36,34]
    assert g.update_raw(35.0, 0.0) is False
