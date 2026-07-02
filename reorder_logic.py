"""Reorder-point calculations using model forecasts and live event counts."""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

from config import (
    DB_PATH,
    INITIAL_STOCK_PER_SLOT,
    LEAD_TIME_DAYS,
    REORDER_LOOKBACK_DAYS,
    SLOT_MAP,
    Z_SCORE,
)
from serial_listener import init_db
from train_model import load_artifact, predict_daily_demand

logger = logging.getLogger(__name__)


@dataclass
class ReorderResult:
    slot_id: int
    sku: str
    name: str
    current_stock: int
    removals_in_window: int
    predicted_daily_demand: float
    avg_daily_sales: float
    std_daily_demand: float
    lead_time_days: float
    z_score: float
    safety_stock: float
    reorder_point: float
    needs_restock: bool
    shortfall: float

    def to_dict(self) -> dict:
        return asdict(self)


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    # Go through init_db so the events table exists even on a fresh database;
    # a raw sqlite3.connect would raise "no such table: events" on first query.
    return init_db(db_path or str(DB_PATH))


def fetch_events(
    conn: sqlite3.Connection,
    since_ts: float | None = None,
    limit: int | None = None,
) -> list[dict]:
    query = "SELECT id, slot_id, sku, timestamp FROM events"
    params: list = []
    if since_ts is not None:
        query += " WHERE timestamp >= ?"
        params.append(since_ts)
    query += " ORDER BY timestamp DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def count_removals_by_slot(
    conn: sqlite3.Connection, lookback_days: int = REORDER_LOOKBACK_DAYS
) -> dict[int, int]:
    since = time.time() - lookback_days * 86400
    rows = conn.execute(
        """
        SELECT slot_id, COUNT(*) AS cnt
        FROM events
        WHERE timestamp >= ?
        GROUP BY slot_id
        """,
        (since,),
    ).fetchall()
    return {int(r[0]): int(r[1]) for r in rows}


def observed_daily_std_by_sku(
    conn: sqlite3.Connection, lookback_days: int = REORDER_LOOKBACK_DAYS
) -> dict[str, float]:
    """Sample std-dev of *daily* removal counts per SKU over the lookback window.

    Every calendar day from a SKU's first observed event (within the window)
    through today is one sample — days with no removals count as 0 demand rather
    than being dropped, so quiet days don't inflate the mean or distort variance.
    Fewer than two days of history returns 0.0, which makes ``compute_reorder``
    fall back to the model's historical std instead of guessing from one point.
    """
    since = time.time() - lookback_days * 86400
    rows = conn.execute(
        """
        SELECT sku,
               date(timestamp, 'unixepoch') AS day,
               COUNT(*) AS removals
        FROM events
        WHERE timestamp >= ?
        GROUP BY sku, day
        """,
        (since,),
    ).fetchall()

    # sku -> {day (YYYY-MM-DD, UTC) -> removals}
    counts: dict[str, dict[str, int]] = {}
    for sku, day, removals in rows:
        counts.setdefault(sku, {})[day] = int(removals)

    today = datetime.now(timezone.utc).date()
    result: dict[str, float] = {}
    for sku, day_counts in counts.items():
        first_day = min(
            datetime.strptime(d, "%Y-%m-%d").date() for d in day_counts
        )
        span_days = (today - first_day).days + 1
        if span_days < 2:
            logger.debug(
                "SKU %s has only %d day(s) of observed history; "
                "falling back to the model's historical std",
                sku,
                span_days,
            )
            result[sku] = 0.0
            continue
        series = [
            day_counts.get((first_day + timedelta(days=i)).strftime("%Y-%m-%d"), 0)
            for i in range(span_days)
        ]
        mean = sum(series) / len(series)
        var = sum((x - mean) ** 2 for x in series) / (len(series) - 1)
        result[sku] = math.sqrt(var)
    return result


def current_stock_for_slot(
    conn: sqlite3.Connection,
    slot_id: int,
    initial_stock: int | None = None,
) -> tuple[int, int]:
    """Return (current_stock, total_removals_all_time)."""
    initial = initial_stock if initial_stock is not None else INITIAL_STOCK_PER_SLOT.get(slot_id, 0)
    row = conn.execute(
        "SELECT COUNT(*) FROM events WHERE slot_id = ?",
        (slot_id,),
    ).fetchone()
    removals = int(row[0]) if row else 0
    return max(0, initial - removals), removals


def compute_reorder(
    slot_id: int,
    artifact: dict | None = None,
    conn: sqlite3.Connection | None = None,
    lead_time_days: float = LEAD_TIME_DAYS,
    z_score: float = Z_SCORE,
) -> ReorderResult:
    """
    Reorder Point = (Avg Daily Sales × Lead Time) + Safety Stock
    Safety Stock = Z-score × StdDev(demand) × √(Lead Time)
    """
    meta = SLOT_MAP[slot_id]
    sku = meta["sku"]
    name = meta["name"]

    own_conn = conn is None
    if own_conn:
        conn = _connect()

    try:
        if artifact is None:
            artifact = load_artifact()

        predicted = predict_daily_demand(artifact, sku, datetime.now(timezone.utc))
        model_entry = artifact["models"].get(sku, {})
        avg_daily = float(model_entry.get("avg_daily_sales", predicted))
        hist_std = float(model_entry.get("std_daily_sales", 0.0))

        observed_std_map = observed_daily_std_by_sku(conn)
        observed_std = observed_std_map.get(sku, 0.0)
        std_daily = observed_std if observed_std > 0 else hist_std

        safety_stock = z_score * std_daily * math.sqrt(lead_time_days)
        reorder_point = (avg_daily * lead_time_days) + safety_stock

        current_stock, _ = current_stock_for_slot(conn, slot_id)
        removals_window = count_removals_by_slot(conn).get(slot_id, 0)
        needs_restock = current_stock < reorder_point
        shortfall = max(0.0, reorder_point - current_stock)

        return ReorderResult(
            slot_id=slot_id,
            sku=sku,
            name=name,
            current_stock=current_stock,
            removals_in_window=removals_window,
            predicted_daily_demand=round(predicted, 2),
            avg_daily_sales=round(avg_daily, 2),
            std_daily_demand=round(std_daily, 2),
            lead_time_days=lead_time_days,
            z_score=z_score,
            safety_stock=round(safety_stock, 2),
            reorder_point=round(reorder_point, 2),
            needs_restock=needs_restock,
            shortfall=round(shortfall, 2),
        )
    finally:
        if own_conn:
            conn.close()


def evaluate_all_slots(
    artifact: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[ReorderResult]:
    return [compute_reorder(slot_id, artifact=artifact, conn=conn) for slot_id in sorted(SLOT_MAP)]


def format_reorder_math(result: ReorderResult) -> str:
    lt = result.lead_time_days
    return (
        f"Safety Stock = Z × σ × √Lead Time\n"
        f"             = {result.z_score} × {result.std_daily_demand} × √{lt}\n"
        f"             = {result.safety_stock}\n\n"
        f"Reorder Point = (Avg Daily Sales × Lead Time) + Safety Stock\n"
        f"              = ({result.avg_daily_sales} × {lt}) + {result.safety_stock}\n"
        f"              = {result.reorder_point}\n\n"
        f"Current stock: {result.current_stock}\n"
        f"Needs restock: {'YES' if result.needs_restock else 'NO'}"
    )
