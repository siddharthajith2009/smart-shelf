"""REST API and static frontend server for the smart-shelf dashboard."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(title="Smart Shelf API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    # Restrict to the local dashboard origins; widen deliberately if deployed.
    allow_origins=["http://localhost:3000", "http://localhost:8000", "http://127.0.0.1:8000"],
    allow_methods=["GET"],
    allow_headers=["*"],
    allow_credentials=False,
)


@contextmanager
def get_db():
    # DB_PATH is looked up from this module's globals at call time (not frozen at
    # import), so tests can monkeypatch api.DB_PATH to isolate the database.
    # init_db also sets row_factory = sqlite3.Row.
    conn = init_db(str(DB_PATH))
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
    if artifact is None:
        raise HTTPException(503, "Model not trained. Run: python train_model.py")
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
    with get_db() as conn:
        events = fetch_events(conn, limit=limit)
    for e in events:
        e["time"] = datetime.fromtimestamp(e["timestamp"], timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    return {"events": events}


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

    last_date = datetime.strptime(str(sku_hist["date"].iloc[-1])[:10], "%Y-%m-%d").replace(
        tzinfo=timezone.utc
    )
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
    except Exception as exc:
        logger.warning("Could not load training data for /api/skus: %s", exc)
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
    app.mount("/css", StaticFiles(directory=FRONTEND_DIR / "css"), name="css")
    app.mount("/js", StaticFiles(directory=FRONTEND_DIR / "js"), name="js")


def main() -> None:
    import uvicorn

    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
