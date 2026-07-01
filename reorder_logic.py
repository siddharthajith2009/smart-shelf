"""Reorder-point calculations using model forecasts and live event counts."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

from config import (
    DB_PATH,
    INITIAL_STOCK_PER_SLOT,
    LEAD_TIME_DAYS,
    REORDER_LOOKBACK_DAYS,
    SLOT_MAP,
    Z_SCORE,
)
from train_model import load_artifact, predict_daily_demand


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
    conn = sqlite3.connect(db_path or str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


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
    since = (datetime.utcnow() - timedelta(days=lookback_days)).timestamp()
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
    """Std-dev of daily removal counts per SKU over the lookback window."""
    since = (datetime.utcnow() - timedelta(days=lookback_days)).timestamp()
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

    by_sku: dict[str, list[int]] = {}
    for sku, _day, removals in rows:
        by_sku.setdefault(sku, []).append(int(removals))

    result: dict[str, float] = {}
    for sku, daily_counts in by_sku.items():
        if len(daily_counts) > 1:
            mean = sum(daily_counts) / len(daily_counts)
            var = sum((x - mean) ** 2 for x in daily_counts) / (len(daily_counts) - 1)
            result[sku] = math.sqrt(var)
        elif len(daily_counts) == 1:
            result[sku] = float(daily_counts[0])
        else:
            result[sku] = 0.0
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

        predicted = predict_daily_demand(artifact, sku)
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
        needs_restock = current_stock <= reorder_point
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
