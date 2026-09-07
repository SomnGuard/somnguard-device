"""AC-002: obstrucción/mala posición -> AS-09 (5s/20s) y pausa a 30s."""
import asyncio

from app.analysis.detector import (
    OBSTRUCTION_PAUSE_SEC,
    Detector,
)


class NoFaceLandmarker:
    """Simula cámara tapada / sin rostro: nunca hay landmarks."""

    async def process(self, frame):
        return None


def _detector():
    d = Detector({})
    d._landmark_detector = NoFaceLandmarker()
    return d


def _run(coro):
    return asyncio.run(coro)


def test_obstruction_escalates_5s_20s_then_pause(monkeypatch):
    import app.analysis.detector as det_mod

    now = [2000.0]
    monkeypatch.setattr(det_mod.time, "monotonic", lambda: now[0])
    d = _detector()
    frame = object()

    now[0] = 2000.0
    r = _run(d.process(frame))
    assert r.events == []  # t=0: timer recién iniciado

    now[0] = 2006.0
    r = _run(d.process(frame))
    assert [e["alert_code"] for e in r.events] == ["AS-09"]
    assert "5s" in r.events[0]["message"]

    now[0] = 2010.0
    r = _run(d.process(frame))
    assert r.events == []  # entre 5s y 20s no se repite

    now[0] = 2021.0
    r = _run(d.process(frame))
    assert [e["alert_code"] for e in r.events] == ["AS-09"]
    assert "20s" in r.events[0]["message"]

    assert OBSTRUCTION_PAUSE_SEC == 30.0
    assert d.needs_detection_pause is False
    now[0] = 2031.0
    r = _run(d.process(frame))
    assert r.events == []  # a 30s no hay 3er sonido: corresponde pausar
    assert d.needs_detection_pause is True
    assert d.obstruction_elapsed_sec >= 30.0


def test_recovery_resets_obstruction_timer(monkeypatch):
    import app.analysis.detector as det_mod

    now = [3000.0]
    monkeypatch.setattr(det_mod.time, "monotonic", lambda: now[0])
    d = _detector()
    frame = object()
    _run(d.process(frame))
    now[0] = 3006.0
    _run(d.process(frame))
    assert d._alert_stage == 1

    # Rostro OK: se resetea (fake con fov_ok mediante stub de métodos).
    async def ok_process(f):
        from app.analysis.detector import DetectionResult
        d.reset_obstruction()
        return DetectionResult(events=[], face_present=True, fov_ok=True)

    d.process = ok_process
    _run(d.process(frame))
    assert d.obstruction_elapsed_sec == 0.0
    assert d.needs_detection_pause is False
