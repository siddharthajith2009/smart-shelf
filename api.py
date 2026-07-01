"""REST API and static frontend server for the smart-shelf dashboard."""

from __future__ import annotations

import json
import sqlite3
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import (
    DB_PATH,
    INITIAL_STOCK_PER_SLOT,
    LEAD_TIME_DAYS,
    MODEL_PATH,
    SLOT_MAP,
    Z_SCORE,
)
from reorder_logic import evaluate_all_slots, fetch_events, format_reorder_math
from serial_listener import init_db
from train_model import load_artifact, load_training_data, predict_daily_demand

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "frontend"
DEBUG_LOG = BASE_DIR / ".cursor" / "debug-f8be37.log"


# #region agent log
def _dbg(hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "sessionId": "f8be37",
                        "hypothesisId": hypothesis_id,
                        "location": location,
                        "message": message,
                        "data": data or {},
                        "timestamp": int(time.time() * 1000),
                    }
                )
                + "\n"
            )
    except Exception:
        pass


# #endregion

app = FastAPI(title="Smart Shelf API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@contextmanager
def get_db():
    conn = init_db(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _load_model_safe() -> dict | None:
    if not MODEL_PATH.exists():
        return None
    try:
        return load_artifact()
    except Exception:
        return None


@app.get("/api/health")
def health():
    artifact = _load_model_safe()
    with get_db() as conn:
        event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return {
        "status": "ok",
        "model_loaded": artifact is not None,
        "event_count": int(event_count),
        "trained_at": artifact.get("trained_at") if artifact else None,
    }


@app.get("/api/config")
def get_config():
    return {
        "lead_time_days": LEAD_TIME_DAYS,
        "z_score": Z_SCORE,
        "initial_stock_per_slot": INITIAL_STOCK_PER_SLOT,
        "slots": {str(k): v for k, v in SLOT_MAP.items()},
    }


@app.get("/api/slots")
def get_slots():
    artifact = _load_model_safe()
    with get_db() as conn:
        results = evaluate_all_slots(artifact=artifact, conn=conn)
    return {
        "slots": [
            {
                **r.to_dict(),
                "reorder_math": format_reorder_math(r),
            }
            for r in results
        ]
    }


@app.get("/api/events")
def get_events(limit: int = 100):
    limit = max(1, min(limit, 500))
    # #region agent log
    _dbg("A", "api.py:get_events", "entry", {"limit": limit})
    # #endregion
    try:
        with get_db() as conn:
            # #region agent log
            _dbg(
                "A",
                "api.py:get_events",
                "conn_row_factory",
                {"row_factory": str(getattr(conn, "row_factory", None))},
            )
            # #endregion
            events = fetch_events(conn, limit=limit)
        for e in events:
            e["time"] = datetime.utcfromtimestamp(e["timestamp"]).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        # #region agent log
        _dbg("A", "api.py:get_events", "success", {"count": len(events)})
        # #endregion
        return {"events": events}
    except Exception as exc:
        # #region agent log
        _dbg(
            "A",
            "api.py:get_events",
            "error",
            {"type": type(exc).__name__, "message": str(exc), "trace": traceback.format_exc()},
        )
        # #endregion
        raise


@app.get("/api/demand/{sku}")
def get_demand(sku: str):
    artifact = _load_model_safe()
    if artifact is None:
        raise HTTPException(503, "Model not trained. Run: python train_model.py")

    try:
        df = load_training_data()
    except Exception as exc:
        raise HTTPException(500, f"Could not load training data: {exc}") from exc

    daily = df.groupby(["date", "item"], as_index=False)["sales"].sum()
    sku_hist = daily[daily["item"] == sku].sort_values("date")
    if sku_hist.empty:
        raise HTTPException(404, f"No historical data for {sku}")

    hist_rows = [
        {"date": str(row["date"])[:10], "sales": float(row["sales"]), "series": "historical"}
        for _, row in sku_hist.iterrows()
    ]

    last_date = datetime.strptime(str(sku_hist["date"].iloc[-1])[:10], "%Y-%m-%d")
    pred_rows = []
    for i in range(1, 15):
        d = last_date + timedelta(days=i)
        pred_rows.append(
            {
                "date": d.strftime("%Y-%m-%d"),
                "sales": round(predict_daily_demand(artifact, sku, d), 2),
                "series": "predicted",
            }
        )

    mae = artifact.get("metrics", {}).get(sku)
    return {
        "sku": sku,
        "mae": mae,
        "historical": hist_rows,
        "predicted": pred_rows,
    }


@app.get("/api/skus")
def get_skus():
    artifact = _load_model_safe()
    try:
        df = load_training_data()
        skus = sorted(df["item"].unique().tolist())
    except Exception:
        skus = [meta["sku"] for meta in SLOT_MAP.values()]
    return {
        "skus": skus,
        "metrics": artifact.get("metrics", {}) if artifact else {},
    }


@app.get("/")
def serve_index():
    index = FRONTEND_DIR / "index.html"
    if not index.exists():
        raise HTTPException(404, "Frontend not found")
    return FileResponse(index)


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
