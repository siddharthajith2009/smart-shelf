"""Regression tests for the smart-shelf backend.

Covers the bugs fixed in the backend pass:
  * serial parsing / event handling
  * fetch_events returning dicts on an init_db() connection (dashboard path)
  * reorder-point math incl. zero-filled daily std and the historical fallback
  * API endpoint contracts, incl. graceful 503 when the model is missing
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import api
import reorder_logic as rl
import serial_listener as sl
from train_model import MODEL_PATH

requires_model = pytest.mark.skipif(
    not MODEL_PATH.exists(), reason="model.pkl not trained (run: python train_model.py)"
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _memdb():
    """Fresh in-memory DB via the canonical factory (dashboard code path)."""
    conn = sl.init_db(":memory:")
    conn.execute("DELETE FROM events")
    conn.commit()
    return conn


def _day_ts(days_ago: int) -> float:
    """Epoch seconds for noon UTC `days_ago` days back (stable calendar date)."""
    noon = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    return (noon - timedelta(days=days_ago)).timestamp()


# --------------------------------------------------------------------------- #
# serial parsing / ingestion
# --------------------------------------------------------------------------- #
def test_parse_event_valid():
    assert sl.parse_event("SLOT:1,TS:1782933249.5") == (1, 1782933249.5)
    assert sl.parse_event("SLOT:2,TS:1700000000") == (2, 1700000000.0)


@pytest.mark.parametrize(
    "line", ["", "   ", "garbage", "SLOT:x,TS:1", "SLOT:1 TS:1", "SLOT:1,TS:abc", "SLOT:1,TS:"]
)
def test_parse_event_invalid(line):
    assert sl.parse_event(line) is None


def test_handle_line_known_unknown_and_junk():
    conn = _memdb()
    assert sl.handle_line(conn, "SLOT:1,TS:1782933249") is True  # known slot -> logged
    assert sl.handle_line(conn, "SLOT:99,TS:1782933249") is False  # unknown slot -> skipped
    assert sl.handle_line(conn, "junk") is False  # unparseable -> skipped
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# #1 regression: fetch_events must return dicts on an init_db() connection
# (the dashboard uses init_db() directly, without api.get_db()'s row_factory)
# --------------------------------------------------------------------------- #
def test_fetch_events_returns_dicts_on_init_db_conn():
    conn = _memdb()
    sl.log_event(conn, 1, "SKU-A", 1782933249.5)
    events = rl.fetch_events(conn, limit=10)
    assert isinstance(events[0], dict)
    assert events == [{"id": 1, "slot_id": 1, "sku": "SKU-A", "timestamp": 1782933249.5}]


def test_fetch_events_empty_and_ordering():
    conn = _memdb()
    assert rl.fetch_events(conn) == []
    sl.log_event(conn, 1, "SKU-A", _day_ts(2))
    sl.log_event(conn, 1, "SKU-A", _day_ts(0))  # newer
    events = rl.fetch_events(conn, limit=10)
    assert [e["timestamp"] for e in events] == sorted(
        [e["timestamp"] for e in events], reverse=True
    )  # DESC by timestamp


# --------------------------------------------------------------------------- #
# reorder logic
# --------------------------------------------------------------------------- #
def test_count_removals_by_slot_respects_window():
    conn = _memdb()
    for ago in (0, 5, 29):  # inside 30-day window
        sl.log_event(conn, 1, "SKU-A", _day_ts(ago))
    for ago in (40, 60):  # outside window
        sl.log_event(conn, 1, "SKU-A", _day_ts(ago))
    assert rl.count_removals_by_slot(conn) == {1: 3}


def test_current_stock_never_negative():
    conn = _memdb()
    for _ in range(4):
        sl.log_event(conn, 1, "SKU-A", _day_ts(0))
    assert rl.current_stock_for_slot(conn, 1, initial_stock=50) == (46, 4)
    assert rl.current_stock_for_slot(conn, 1, initial_stock=2) == (0, 4)  # clamped at 0


def test_observed_std_empty_and_single_day():
    conn = _memdb()
    assert rl.observed_daily_std_by_sku(conn) == {}
    for _ in range(5):
        sl.log_event(conn, 1, "SKU-A", _day_ts(0))
    # one day of history -> 0.0 so compute_reorder falls back to historical std
    assert rl.observed_daily_std_by_sku(conn) == {"SKU-A": 0.0}


def test_observed_std_zero_fills_quiet_days():
    conn = _memdb()
    sl.log_event(conn, 1, "SKU-A", _day_ts(4))  # 1 removal, 4 days ago
    for _ in range(3):
        sl.log_event(conn, 1, "SKU-A", _day_ts(0))  # 3 removals today
    series = [1, 0, 0, 0, 3]  # quiet days count as zero demand
    mean = sum(series) / len(series)
    expected = math.sqrt(sum((x - mean) ** 2 for x in series) / (len(series) - 1))
    assert rl.observed_daily_std_by_sku(conn)["SKU-A"] == pytest.approx(expected)


def test_compute_reorder_falls_back_to_historical_std(monkeypatch):
    monkeypatch.setattr(rl, "predict_daily_demand", lambda *a, **k: 10.0)
    artifact = {
        "models": {"SKU-A": {"avg_daily_sales": 10.0, "std_daily_sales": 2.0}},
        "metrics": {},
        "trained_at": "x",
    }
    conn = _memdb()  # no events -> observed std 0 -> hist std 2.0
    res = rl.compute_reorder(1, artifact=artifact, conn=conn)
    assert res.std_daily_demand == pytest.approx(2.0)
    assert res.safety_stock == pytest.approx(round(1.65 * 2.0 * math.sqrt(7), 2))
    assert res.reorder_point == pytest.approx(round(10.0 * 7 + res.safety_stock, 2))
    assert res.current_stock == 50 and res.needs_restock is True


def test_compute_reorder_prefers_observed_std(monkeypatch):
    monkeypatch.setattr(rl, "predict_daily_demand", lambda *a, **k: 10.0)
    artifact = {
        "models": {"SKU-A": {"avg_daily_sales": 10.0, "std_daily_sales": 2.0}},
        "metrics": {},
        "trained_at": "x",
    }
    conn = _memdb()
    sl.log_event(conn, 1, "SKU-A", _day_ts(4))
    for _ in range(3):
        sl.log_event(conn, 1, "SKU-A", _day_ts(0))
    res = rl.compute_reorder(1, artifact=artifact, conn=conn)
    assert res.std_daily_demand == pytest.approx(1.3, abs=0.05)  # observed, not hist 2.0


# --------------------------------------------------------------------------- #
# API endpoints
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch, tmp_path):
    # isolate the DB so tests never touch the real events.db
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "events.db")
    return TestClient(api.app)


def test_health_and_config(client):
    h = client.get("/api/health")
    assert h.status_code == 200
    body = h.json()
    assert body["status"] == "ok" and body["event_count"] == 0
    assert isinstance(body["model_loaded"], bool)

    c = client.get("/api/config").json()
    assert c["lead_time_days"] == 7 and c["z_score"] == 1.65
    assert "1" in c["slots"]


def test_events_endpoint_roundtrip(client):
    assert client.get("/api/events").json() == {"events": []}
    # write through the same (temp) DB the API reads
    conn = sl.init_db(str(api.DB_PATH))
    sl.log_event(conn, 1, "SKU-A", 1782933249.5)
    conn.close()
    events = client.get("/api/events?limit=100").json()["events"]
    assert len(events) == 1
    assert events[0]["sku"] == "SKU-A"
    expected = datetime.fromtimestamp(1782933249.5, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    assert events[0]["time"] == expected  # UTC-formatted, not local time


def test_slots_returns_503_when_model_missing(client, monkeypatch, tmp_path):
    monkeypatch.setattr(api, "MODEL_PATH", tmp_path / "nope.pkl")
    assert client.get("/api/slots").status_code == 503


@requires_model
def test_slots_ok(client):
    r = client.get("/api/slots")
    assert r.status_code == 200
    slot = r.json()["slots"][0]
    for key in ("slot_id", "sku", "current_stock", "reorder_point", "needs_restock", "reorder_math"):
        assert key in slot
    assert isinstance(slot["needs_restock"], bool)


@requires_model
def test_demand_ok_and_unknown_sku(client):
    ok = client.get("/api/demand/SKU-A")
    assert ok.status_code == 200
    body = ok.json()
    assert body["sku"] == "SKU-A"
    assert len(body["predicted"]) == 14 and body["historical"]
    assert client.get("/api/demand/SKU-NOPE").status_code == 404


def test_skus_endpoint(client):
    body = client.get("/api/skus").json()
    assert "SKU-A" in body["skus"]
    assert isinstance(body["metrics"], dict)
