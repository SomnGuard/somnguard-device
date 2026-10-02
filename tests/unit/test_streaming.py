"""HU-DEVICE-005 iteración 1: estado, auto-stop, bitrate y JPEG (sin hardware/red)."""
from app.streaming.frames import encode_live_frame
from app.streaming.session import BITRATE_MAX_KBPS, BITRATE_MIN_KBPS, StreamManager, StreamState


def test_wants_view_transitions_to_live():
    m = StreamManager()
    assert m.state is StreamState.IDLE
    assert m.wants_view("sess-1") is True
    assert m.is_live
    assert m.session_id == "sess-1"


def test_second_wants_view_only_refreshes():
    m = StreamManager()
    assert m.wants_view("sess-1") is True
    assert m.wants_view("sess-1") is False
    assert m.is_live


def test_disabled_never_goes_live():
    m = StreamManager(enabled=False)
    assert m.wants_view("sess-1") is False
    assert not m.is_live


def test_auto_stop_after_timeout():
    m = StreamManager(auto_stop_sec=30.0)
    m.wants_view("sess-1")
    assert m.tick(m.last_viewer_seen_monotonic + 29.9) is False
    assert m.is_live
    assert m.tick(m.last_viewer_seen_monotonic + 30.0) is True
    assert not m.is_live
    assert m.session_id is None


def test_stop_idempotent():
    m = StreamManager()
    assert m.stop() is False
    m.wants_view("sess-1")
    assert m.stop() is True
    assert m.stop() is False


def test_adapt_bitrate_bounds():
    m = StreamManager()
    assert m.adapt_bitrate(2.0) == BITRATE_MIN_KBPS
    assert m.adapt_bitrate(0.1) == BITRATE_MAX_KBPS
    assert m.adapt_bitrate(0.8) == 1000


def test_adapt_bitrate_drives_encoder_qos():
    m = StreamManager()
    m.adapt_bitrate(0.1)
    assert (m.qos_level, m.qos_quality, m.qos_fps) == (0, 55, 8.0)
    m.adapt_bitrate(0.8)
    assert (m.qos_level, m.qos_quality, m.qos_fps) == (1, 45, 6.0)
    m.adapt_bitrate(2.0)
    assert (m.qos_level, m.qos_quality, m.qos_fps) == (2, 35, 4.0)


def test_encode_live_frame_none():
    assert encode_live_frame(None) is None


def test_encode_live_frame_resizes():
    np = __import__("numpy")
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame[100:200, 100:200] = 255
    jpg = encode_live_frame(frame)
    assert jpg is not None
    assert jpg[:2] == b"\xff\xd8"
