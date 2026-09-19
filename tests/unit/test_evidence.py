"""HU-DEVICE-003 AC-007: evidencia JPEG 640px/q70, >=MODERADA, no bloquea."""
import numpy as np

from app.capture.evidence import needs_evidence, save_event_frame


class FakeCV2:
    IMWRITE_JPEG_QUALITY = 1

    def __init__(self):
        self.saved = {}

    def resize(self, frame, size):
        w, h = size
        return np.zeros((h, w, 3), dtype=np.uint8)

    def imwrite(self, path, img, params=None):
        self.saved[path] = (img.shape, params)
        with open(path, "wb") as f:
            f.write(b"fake-jpg")
        return True


def test_severity_gate():
    assert needs_evidence("MODERADA") is True
    assert needs_evidence("SEVERA") is True
    assert needs_evidence("CRITICA") is True
    assert needs_evidence("LEVE") is False
    assert needs_evidence("INFO") is False
    assert needs_evidence(None) is False


def test_save_resizes_major_side_to_640(tmp_path):
    cv2 = FakeCV2()
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)  # lado mayor 1280 -> 640
    out = save_event_frame(frame, "ev-1", "MODERADA", tmp_path, cv2_module=cv2)
    assert out is not None and out.endswith("ev-1.jpg")
    saved_shape = list(cv2.saved.values())[0][0]
    assert max(saved_shape[0], saved_shape[1]) == 640


def test_no_evidence_below_moderada(tmp_path):
    cv2 = FakeCV2()
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    assert save_event_frame(frame, "ev-2", "LEVE", tmp_path, cv2_module=cv2) is None
    assert save_event_frame(frame, "ev-3", "INFO", tmp_path, cv2_module=cv2) is None
    assert cv2.saved == {}


def test_failure_never_raises(tmp_path):
    # frame None / cv2 ausente / resize roto -> None, no excepción
    assert save_event_frame(None, "ev-x", "SEVERA", tmp_path, cv2_module=FakeCV2()) is None

    class BrokenCV2(FakeCV2):
        def resize(self, frame, size):
            raise RuntimeError("boom")

    frame = np.zeros((800, 600, 3), dtype=np.uint8)
    assert save_event_frame(frame, "ev-y", "CRITICA", tmp_path,
                            cv2_module=BrokenCV2()) is None
