"""HU-DEVICE-001 IT2: EAR/MAR + somnolencia (AC-001/AC-002). Sin hardware."""
import numpy as np

from app.analysis.ear_mar import TiltEstimator, eye_aspect_ratio, mouth_aspect_ratio, PerclosTracker
from app.analysis.somnolence import SomnolenceDetector
from app.analysis.severity import EVENT_TABLE


def _eye(open_: bool = True):
    # 6 pts: p1(0,0) p4(1,0); verticales grandes=abierto, chicos=cerrado
    h = 0.3 if open_ else 0.02
    return np.array([[0, 0], [0.2, h], [0.5, h], [1, 0], [0.5, -h], [0.2, -h]], dtype=float)


def test_ear_open_vs_closed():
    assert eye_aspect_ratio(_eye(True)) > 0.2
    assert eye_aspect_ratio(_eye(False)) < 0.2

def test_mar_open_vs_closed():
    assert mouth_aspect_ratio(_eye(True)) > mouth_aspect_ratio(_eye(False))


def test_mouth_uses_inner_lips():
    """Los puntos de boca deben medir la apertura INTERNA (13/14): con la
    boca muy abierta de frente el MAR tiene que pasar 0.7 holgado.
    (El set externo saturaba en ~0.6 y solo subía al girar la cabeza.)"""
    from app.analysis.landmarks import MOUTH_IDX, get_key_points

    assert {61, 291, 13, 14} <= set(MOUTH_IDX)

    def mar_with_opening(half_h: float) -> float:
        lm = np.zeros((468, 3), dtype=float)
        lm[61] = [0.0, 0.0, 0.0]
        lm[291] = [1.0, 0.0, 0.0]
        lm[13] = [0.5, half_h, 0.0]
        lm[14] = [0.5, -half_h, 0.0]
        lm[82] = [0.3, half_h, 0.0]
        lm[87] = [0.3, -half_h, 0.0]
        return mouth_aspect_ratio(get_key_points(lm)["mouth"])

    assert mar_with_opening(0.02) < 0.15  # cerrada
    assert mar_with_opening(0.40) > 0.7  # bien abierta de frente


def test_perclos_tracker():
    p = PerclosTracker(window_sec=10)
    assert p.update(True, 0.0) == 1.0
    assert p.update(False, 1.0) == 0.5


def test_blink_anomaly_emits_som01():
    d = SomnolenceDetector({"blink_rate_max": 25, "blink_rate_min": 5, "blink_window_sec": 15,
                             "perclos_threshold": 0.99, "ear_closed_threshold": 0.2})
    now = 1000.0
    # 6 blinks rápidos en ventana 15s -> rate = 6*4=24... necesitamos >25: 7 blinks
    for i in range(7):
        d.update(0.05, 0.2, 0.0, now)      # cierra
        now += 0.2
        d.update(0.35, 0.2, 0.0, now)      # abre (blink válido 0.2s)
        now += 1.0
    # forzar ventana llena: avanzar hasta 60% ventana desde primer blink
    evs = d.update(0.35, 0.2, 0.0, now + 5.0)
    codes = [e["event_type_id"] for e in evs]
    assert "EV-SOM-01" in codes
    assert evs[0]["alert_code"] == "AS-01"
    assert evs[0]["severity"] == "LEVE"


def test_eye_closure_2s_emits_som02():
    d = SomnolenceDetector({})
    evs = d.update(0.05, 0.2, 0.0, 2000.0)  # cierra
    assert evs == []
    evs = d.update(0.05, 0.2, 0.0, 2002.5)  # 2.5s cerrado
    assert [e["event_type_id"] for e in evs] == ["EV-SOM-02"]
    assert evs[0]["alert_code"] == "AS-02" and evs[0]["severity"] == "MODERADA"
    # latch: no re-emite mientras siga cerrado
    assert d.update(0.05, 0.2, 0.0, 2003.0) == []
    # reabre y recierra -> re-emite tras cooldown
    d.update(0.35, 0.2, 0.0, 2003.5)
    evs = d.update(0.05, 0.2, 0.0, 2004.0)
    evs = d.update(0.05, 0.2, 0.0, 2016.0)
    assert any(e["event_type_id"] == "EV-SOM-02" for e in evs)


def test_yawn_2x_emits_som03():
    d = SomnolenceDetector({"yawn_cooldown_sec": 5.0})  # cooldown corto para test
    now = 3000.0
    evs: list = []
    for _ in range(2):
        for _i in range(30):  # 3.0s a 10fps con MAR alto (bostezo típico)
            d.update(0.35, 1.0, 0.0, now)
            now += 0.1
        for _i in range(5):  # cierre: la EMA tarda ~2 frames en bajar de close
            evs += d.update(0.35, 0.2, 0.0, now)
            now += 0.1
        now += 6.0
    assert any(e["event_type_id"] == "EV-SOM-03" for e in evs)


def test_talking_does_not_count_as_yawn():
    """Regresión FP: hablar (MAR 0.65, aperturas ~1s) no debe contar."""
    d = SomnolenceDetector({})
    now = 6000.0
    evs_all = []
    for _ in range(3):
        d.update(0.35, 0.65, 0.0, now)
        now += 1.0
        evs_all += d.update(0.35, 0.2, 0.0, now)
        now += 60.0
    assert not any(e["event_type_id"] == "EV-SOM-03" for e in evs_all)


def test_short_opening_does_not_count():
    """Regresión FP: apertura 1.0s aunque con MAR alto no es bostezo (min 3.0s)."""
    d = SomnolenceDetector({"yawn_cooldown_sec": 0.0})
    now = 7000.0
    d.update(0.35, 1.0, 0.0, now)
    evs = d.update(0.35, 0.2, 0.0, now + 1.0)
    assert evs == []
    assert len(d._yawns) == 0


def test_loud_talking_without_peak_does_not_count():
    """Regresión FP: habla fuerte sostenida (MAR 0.8, 3s) sin pico ≥0.9 no cuenta."""
    d = SomnolenceDetector({"yawn_cooldown_sec": 0.0})
    now = 7500.0
    d.update(0.35, 0.8, 0.0, now)
    now += 3.0
    assert d.update(0.35, 0.2, 0.0, now) == []
    assert len(d._yawns) == 0


def test_yawn_cooldown_avoids_double_count():
    """Una frase larga con dos pausas no cuenta como 2 bostezos."""
    d = SomnolenceDetector({"yawn_cooldown_sec": 30.0})
    now = 8000.0
    for _ in range(2):
        for _i in range(30):  # 3.0s a 10fps
            d.update(0.35, 1.0, 0.0, now)
            now += 0.1
        for _i in range(5):
            d.update(0.35, 0.2, 0.0, now)
            now += 0.1
        now += 2.0  # solo 2s después (< cooldown 30s)
    assert len(d._yawns) == 1


def test_single_prolonged_yawn_alerts():
    """Un solo bostezo ≥3s ya alerta (PLOS: indicador directo, no solo conteo)."""
    d = SomnolenceDetector({"yawn_cooldown_sec": 5.0})
    now = 8100.0
    evs: list = []
    for _i in range(30):
        d.update(0.35, 1.0, 0.0, now)
        now += 0.1
    for _i in range(5):
        evs += d.update(0.35, 0.2, 0.0, now)
        now += 0.1
    assert any(e["event_type_id"] == "EV-SOM-03" for e in evs)


def test_slow_blink_emits_som01():
    """Cierre aislado ~0.7s (lento, no blink normal ni SOM-02) -> SOM-01 LEVE."""
    d = SomnolenceDetector({})
    now = 8200.0
    assert d.update(0.05, 0.2, 0.0, now) == []
    evs = d.update(0.35, 0.2, 0.0, now + 0.7)
    assert [e["event_type_id"] for e in evs] == ["EV-SOM-01"]
    assert evs[0]["alert_code"] == "AS-01"
    # Con cooldown: el siguiente inmediato no repite.
    assert d.update(0.05, 0.2, 0.0, now + 1.0) == []
    assert d.update(0.35, 0.2, 0.0, now + 1.7) == []


def test_single_frame_spike_does_not_open_episode():
    """Un spike de 1 frame (ruido de landmarks con movimiento) no abre episodio."""
    d = SomnolenceDetector({"yawn_cooldown_sec": 0.0})
    now = 8500.0
    for _i in range(10):  # base boca cerrada a 10fps
        d.update(0.35, 0.2, 0.0, now)
        now += 0.1
    d.update(0.35, 1.2, 0.0, now)  # spike aislado: EMA 0.8 < open 0.85
    now += 0.1
    assert d._yawning is False
    for _i in range(10):
        assert d.update(0.35, 0.2, 0.0, now) == []
        now += 0.1
    assert len(d._yawns) == 0


def test_oscillating_talk_does_not_count_dense():
    """Habla oscilante 0.9/0.7 a 10fps por 8s: la EMA la aplana bajo open."""
    d = SomnolenceDetector({"yawn_cooldown_sec": 0.0})
    now = 8600.0
    for i in range(80):
        d.update(0.35, 0.9 if i % 2 == 0 else 0.7, 0.0, now)
        now += 0.1
    for _i in range(5):
        d.update(0.35, 0.2, 0.0, now)
        now += 0.1
    assert len(d._yawns) == 0


def test_no_reemit_without_new_yawn():
    """Hablar tras 2 bostezos reales no re-dispara EV-SOM-03 (anti-spam)."""
    d = SomnolenceDetector({"yawn_cooldown_sec": 5.0})
    now = 9000.0
    evs = []
    for _ in range(2):
        for _i in range(30):  # 3.0s a 10fps con MAR alto (bostezo típico)
            d.update(0.35, 1.0, 0.0, now)
            now += 0.1
        for _i in range(5):
            evs += d.update(0.35, 0.2, 0.0, now)
            now += 0.1
        now += 6.0
    assert any(e["event_type_id"] == "EV-SOM-03" for e in evs)
    # Hablar después (MAR bajo o apertura corta): sin bostezo nuevo, silencio.
    d.update(0.35, 0.65, 0.0, now)
    now += 1.0
    assert d.update(0.35, 0.2, 0.0, now) == []
    d.update(0.35, 0.85, 0.0, now + 1.0)  # apertura alta pero corta (<2.0s)
    assert d.update(0.35, 0.2, 0.0, now + 2.0) == []


def test_degenerate_pose_is_not_head_tilt(monkeypatch):
    """Flip de solvePnP (pitch −179°) no debe disparar EV-SOM-04."""
    import numpy as np

    import app.analysis.somnolence as som_mod
    from app.analysis.landmarks import FaceLandmarks

    monkeypatch.setattr(som_mod, "estimate_head_pose", lambda *a: (-179.0, 2.0, 0.0))
    monkeypatch.setattr(som_mod, "eye_aspect_ratio", lambda pts: 0.35)
    monkeypatch.setattr(som_mod, "mouth_aspect_ratio", lambda pts: 0.2)
    lm = FaceLandmarks(landmarks=np.zeros((468, 3), dtype=float),
                       bbox=(0, 0, 10, 10), score=1.0, image_shape=(100, 100))
    d = SomnolenceDetector({})
    evs: list = []
    for i in range(10):
        evs += d.process_landmarks(lm, 11000.0 + i)
    assert not any(e["event_type_id"] == "EV-SOM-04" for e in evs)


def test_head_tilt_emits_som04_and_microsleep_som05():
    d = SomnolenceDetector({})
    d.update(0.35, 0.2, 25.0, 4000.0)
    evs = d.update(0.35, 0.2, 25.0, 4003.5)
    assert any(e["event_type_id"] == "EV-SOM-04" for e in evs)
    assert any(e["alert_code"] == "AS-03" for e in evs)
    # microsueño: ojos cerrados + tilt simultáneo >3s
    d2 = SomnolenceDetector({})
    d2.update(0.05, 0.2, 25.0, 5000.0)
    evs = d2.update(0.05, 0.2, 25.0, 5003.5)
    codes = [e["event_type_id"] for e in evs]
    assert "EV-SOM-05" in codes
    sev = {e["event_type_id"]: e["severity"] for e in evs}
    assert sev["EV-SOM-05"] == "CRITICA"


def test_tilt_suppressed_while_yawning():
    """Bostezar echa la cabeza atrás (tilt 20-26° en campo): no es SOM-04."""
    d = SomnolenceDetector({"tilt_calib_samples": 0})
    now = 23000.0
    d.update(0.35, 1.0, 0.0, now)  # abre boca -> bostezo activo
    evs: list = []
    for i in range(40):  # 4s con tilt alto DURANTE el bostezo
        now += 0.1
        evs += d.update(0.35, 1.0, 25.0, now)
    assert not any(e["event_type_id"] == "EV-SOM-04" for e in evs)
    # Al cerrar la boca, un tilt que sigue requiere sus 3s completos.
    for i in range(5):
        now += 0.1
        evs += d.update(0.35, 0.2, 25.0, now)
    assert not any(e["event_type_id"] == "EV-SOM-04" for e in evs)
    now += 3.5
    evs = d.update(0.35, 0.2, 25.0, now)
    assert any(e["event_type_id"] == "EV-SOM-04" for e in evs)


def test_severity_table_covers_ac002():
    assert EVENT_TABLE["EV-SOM-01"]["alert_code"].value == "AS-01"
    assert EVENT_TABLE["EV-SOM-04"]["severity"].value == "SEVERA"
    assert EVENT_TABLE["EV-SOM-05"]["severity"].value == "CRITICA"


def test_tilt_relative_to_biased_neutral():
    """Campo 2026-09-16: neutro (yaw 6, pitch 170). Un cabeceo real
    (pitch 170→140) debe medir ~30° aunque el valor absoluto sea 'raro'."""
    t = TiltEstimator(calib_samples=10)
    now = 20000.0
    for _ in range(10):
        assert t.update(6.0, 170.0, now) == 0.0  # calibrando
        now += 0.05
    assert t.calibrated is True
    for _ in range(30):
        t.update(6.0, 140.0, now)  # delta pitch −30
        now += 0.05
    assert t._tilt > 20.0


def test_tilt_flip_holds_then_decays():
    """Flip extremo conserva el tilt (no rompe el episodio) y decae a 0
    tras hold_sec sin pose válida."""
    t = TiltEstimator(calib_samples=4, hold_sec=3.0)
    now = 21000.0
    for _ in range(4):
        t.update(6.0, 170.0, now)
        now += 0.05
    for _ in range(20):
        t.update(6.0, 140.0, now)
        now += 0.05
    held = t._tilt
    assert held > 20.0
    assert t.update(124.0, -57.0, now) == held  # flip: conserva
    assert t.update(124.0, -57.0, now + 10.0) == 0.0  # tras hold: decae
    for _ in range(10):  # frontal de nuevo: vuelve a ~0 por EMA
        v = t.update(6.0, 170.0, now + 11.0)
    assert v < 5.0


def test_som04_end_to_end_with_biased_pose(monkeypatch):
    """EV-SOM-04 vía process_landmarks con el sesgo real de campo."""
    import app.analysis.somnolence as som_mod
    from app.analysis.landmarks import FaceLandmarks

    poses = [(170.0, 6.0)] * 70 + [(157.0, 45.0)] * 80  # (pitch, yaw)
    state = {"i": 0}

    def fake_pose(*a):
        p = poses[min(state["i"], len(poses) - 1)]
        state["i"] += 1
        return (p[0], p[1], 0.0)

    monkeypatch.setattr(som_mod, "estimate_head_pose", fake_pose)
    monkeypatch.setattr(som_mod, "eye_aspect_ratio", lambda pts: 0.35)
    monkeypatch.setattr(som_mod, "mouth_aspect_ratio", lambda pts: 0.2)
    lm = FaceLandmarks(landmarks=np.zeros((468, 3), dtype=float),
                       bbox=(0, 0, 10, 10), score=1.0, image_shape=(100, 100))
    d = SomnolenceDetector({"tilt_calib_samples": 70})
    evs: list = []
    now = 22000.0
    for _ in range(len(poses)):
        evs += d.process_landmarks(lm, now)
        now += 0.05
    assert any(e["event_type_id"] == "EV-SOM-04" for e in evs)
