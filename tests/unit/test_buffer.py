"""HU-DEVICE-003 AC-001/AC-005: buffer SQLite pending_events."""
import json
import time

from app.storage.buffer import EventBuffer


def test_enqueue_fetch_ack_cycle(tmp_path):
    buf = EventBuffer(tmp_path / "db" / "somnguard_local.db")
    assert buf.count_pending() == 0
    assert buf.enqueue("ev-1", {"event_id": "ev-1", "event_type": "EV-SOM-02"}, None) is True
    # Idempotente local: duplicado no inserta
    assert buf.enqueue("ev-1", {"event_id": "ev-1"}, None) is False
    assert buf.count_pending() == 1
    batch = buf.fetch_batch(limit=100)
    assert len(batch) == 1 and batch[0]["id"] == "ev-1"
    buf.mark_sending(["ev-1"])
    assert buf.count_pending() == 1  # SENDING sigue contando como pendiente
    assert buf.mark_acknowledged(["ev-1"]) == 1
    assert buf.count_pending() == 0


def test_failed_retention_only_failed(tmp_path):
    buf = EventBuffer(tmp_path / "t.db")
    buf.enqueue("a", {"event_id": "a"}, None)
    buf.enqueue("b", {"event_id": "b"}, None)
    buf.mark_failed(["a"], max_retries=10)
    # 1 retry -> sigue PENDING, no se purga aunque retention=0
    assert buf.purge_old_failed(retention_days=0) == 0
    # Forzar FAILED antiguo manipulando created_at
    import sqlite3
    with sqlite3.connect(str(tmp_path / "t.db")) as conn:
        conn.execute("UPDATE pending_events SET status='FAILED',"
                     " created_at='2000-01-01T00:00:00+00:00' WHERE id='a'")
    assert buf.purge_old_failed(retention_days=7) == 1
    assert buf.count_pending() == 1  # 'b' PENDING intacto


def test_batch_limit_and_order(tmp_path):
    buf = EventBuffer(tmp_path / "t.db")
    for i in range(5):
        buf.enqueue(f"ev-{i}", {"event_id": f"ev-{i}", "n": i}, None)
        time.sleep(0.01)
    batch = buf.fetch_batch(limit=2)
    assert [b["id"] for b in batch] == ["ev-0", "ev-1"]
    batch150 = buf.fetch_batch(limit=500)
    assert len(batch150) == 5  # clamp a 100, pero hay 5


def test_evidence_path_stored_relative_and_migrates_legacy(tmp_path):
    buf = EventBuffer(tmp_path / "t.db")
    # Absoluto legacy se normaliza al guardar
    buf.enqueue("abs-1", {"event_id": "abs-1"},
                "C:\\Users\\TEST\\Documents\\SomnGuard\\data\\media\\abs-1.jpg")
    batch = buf.fetch_batch()
    assert batch[0]["evidence_path"] == "media/abs-1.jpg"
    # Fila legacy insertada directo en SQL se migra al abrir el buffer
    import sqlite3
    with sqlite3.connect(str(tmp_path / "t.db")) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO pending_events"
            " (id, event_json, evidence_path, status, retries, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            ("legacy-1", '{"event_id":"legacy-1"}',
             "/home/pi/somnguard/data/media/legacy-1.jpg",
             "PENDING", 0, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
    buf2 = EventBuffer(tmp_path / "t.db")  # __init__ migra
    rows = {b["id"]: b["evidence_path"] for b in buf2.fetch_batch(limit=100)}
    assert rows["legacy-1"] == "media/legacy-1.jpg"
    assert rows["abs-1"] == "media/abs-1.jpg"
