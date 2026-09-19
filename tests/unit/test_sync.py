"""HU-DEVICE-003 AC-002/003/004/005: sync lote, backoff, dedup, limpieza."""
import asyncio
import json

import app.device.backend as backend_mod
from app.device.backend import BackendClient
from app.storage.buffer import EventBuffer
from app.sync.engine import SyncEngine, compute_backoff_sec


class FakeResponse:
    def __init__(self, payload, status=200):
        self._raw = json.dumps(payload).encode()
        self.status = status
        self.length = len(self._raw)

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install(monkeypatch, handler):
    def fake_urlopen(req, timeout=None):
        return handler(req)
    monkeypatch.setattr(backend_mod.urllib.request, "urlopen", fake_urlopen)


def test_backoff_sequence():
    assert compute_backoff_sec(0, jitter_sec=0) == 60
    assert compute_backoff_sec(1, jitter_sec=0) == 120
    assert compute_backoff_sec(2, jitter_sec=0) == 240
    assert compute_backoff_sec(10, jitter_sec=0) == 3600  # cap 1h


def test_post_events_parses_duplicates(monkeypatch):
    def handler(req):
        assert req.full_url.endswith("/api/v1/telemetry/events")
        body = json.loads(req.data.decode())
        assert len(body["events"]) <= 100
        return FakeResponse({"acked_ids": ["a"], "duplicate_ids": ["b"]}, status=201)
    _install(monkeypatch, handler)
    res = BackendClient("http://t").post_events_sync("dev", "k",
                                                     [{"event_id": "a"}, {"event_id": "b"}])
    assert res == {"acked_ids": ["a"], "duplicate_ids": ["b"]}
    assert BackendClient.parse_telemetry_ack(res) == res


def test_healthcheck_true_false(monkeypatch):
    _install(monkeypatch, lambda req: FakeResponse({}, status=200))
    assert BackendClient("http://t").healthcheck_sync() is True

    import urllib.error
    def fail(req, timeout=None):
        raise urllib.error.URLError("caída")
    monkeypatch.setattr(backend_mod.urllib.request, "urlopen", fail)
    assert BackendClient("http://t").healthcheck_sync() is False


def test_sync_once_cleans_acked_and_duplicates(tmp_path):
    buf = EventBuffer(tmp_path / "t.db")
    buf.enqueue("a", {"event_id": "a", "event_type": "EV-SOM-02"}, None)
    buf.enqueue("b", {"event_id": "b", "event_type": "EV-SOM-03"}, None)

    class FakeBackend:
        async def post_events(self, device_id, api_key, events):
            assert len(events) == 2
            return {"acked_ids": ["a"], "duplicate_ids": ["b"]}

        def parse_telemetry_ack(self, res):
            return res

        async def upload_evidence(self, *a, **k):
            return {}

    eng = SyncEngine(buf, FakeBackend())
    res = asyncio.run(eng.sync_once("dev", "k"))
    assert res["synced"] == 2 and res["duplicates"] == 1
    assert buf.count_pending() == 0


def test_sync_failure_increments_retries_and_backoff(tmp_path):
    buf = EventBuffer(tmp_path / "t.db")
    buf.enqueue("a", {"event_id": "a"}, None)

    class FailBackend:
        async def post_events(self, *a, **k):
            raise backend_mod.BackendError("500")

    eng = SyncEngine(buf, FailBackend())
    r1 = asyncio.run(eng.sync_once("dev", "k"))
    assert r1["failed"] == 1
    assert buf.count_pending() == 1
    # Segundo intento inmediato bloqueado por backoff
    r2 = asyncio.run(eng.sync_once("dev", "k"))
    assert r2.get("skipped_backoff") is True


def test_sync_resolves_relative_evidence_and_deletes(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    jpg = media / "ev-rel.jpg"
    jpg.write_bytes(b"fake-jpg")
    buf = EventBuffer(tmp_path / "t.db")
    buf.enqueue("ev-rel", {"event_id": "ev-rel", "has_evidence": True}, "media/ev-rel.jpg")

    seen = {}

    class FakeBackend:
        async def post_events(self, device_id, api_key, events):
            return {"acked_ids": ["ev-rel"], "duplicate_ids": []}

        def parse_telemetry_ack(self, res):
            return res

        async def upload_evidence(self, device_id, api_key, event_id, path, checksum):
            seen["path"] = path
            assert event_id == "ev-rel"
            assert Path(path) == jpg  # resuelto a <data_dir>/media/...
            return {"evidence_id": "e1"}

    from pathlib import Path
    eng = SyncEngine(buf, FakeBackend(), data_dir=tmp_path)
    res = asyncio.run(eng.sync_once("dev", "k"))
    assert res["synced"] == 1
    assert buf.count_pending() == 0
    assert seen["path"] == str(jpg)
    assert not jpg.exists()  # limpieza tras ACK
