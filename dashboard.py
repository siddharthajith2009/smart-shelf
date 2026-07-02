"""Streamlit dashboard for live shelf status, events, demand, and restock math."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from config import DB_PATH, INITIAL_STOCK_PER_SLOT, LEAD_TIME_DAYS, MODEL_PATH, SLOT_MAP, Z_SCORE
from reorder_logic import (
    evaluate_all_slots,
    fetch_events,
    format_reorder_math,
)
from serial_listener import init_db
from train_model import load_artifact, load_training_data, predict_daily_demand


st.set_page_config(page_title="Smart Shelf Dashboard", layout="wide")
st.title("Smart Shelf — Restock Dashboard")


@st.cache_resource
def get_model():
    if not MODEL_PATH.exists():
        st.warning("model.pkl not found — run `python train_model.py` first.")
        return None
    return load_artifact()


@st.cache_data(ttl=30)
def get_historical_sales() -> pd.DataFrame:
    try:
        df = load_training_data()
        daily = df.groupby(["date", "item"], as_index=False)["sales"].sum()
        return daily
    except Exception as exc:
        st.error(f"Could not load training data: {exc}")
        return pd.DataFrame(columns=["date", "item", "sales"])


def ensure_db() -> sqlite3.Connection:
    return init_db(str(DB_PATH))


def render_slot_status(conn: sqlite3.Connection, artifact: dict | None) -> None:
    st.subheader("Live Slot Status")
    if artifact is None:
        st.info("Train the model (`python train_model.py`) to see slot status.")
        return
    results = evaluate_all_slots(artifact=artifact, conn=conn)

    cols = st.columns(len(results) if results else 1)
    for col, r in zip(cols, results):
        with col:
            status = "RESTOCK" if r.needs_restock else "OK"
            st.metric(
                label=f"Slot {r.slot_id}: {r.name}",
                value=f"{r.current_stock} units",
                delta=f"ROP {r.reorder_point}",
            )
            st.caption(f"{r.sku} · {status}")


def render_event_log(conn: sqlite3.Connection) -> None:
    st.subheader("Event Log")
    events = fetch_events(conn, limit=100)
    if not events:
        st.info("No events yet. Start `serial_listener.py` and trigger the IR sensors.")
        return

    df = pd.DataFrame(events)
    df["time"] = df["timestamp"].apply(
        lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    )
    df = df[["id", "slot_id", "sku", "time"]].rename(
        columns={"id": "ID", "slot_id": "Slot", "sku": "SKU", "time": "Timestamp"}
    )
    st.dataframe(df, use_container_width=True, hide_index=True)


def render_demand_chart(artifact: dict | None) -> None:
    st.subheader("Demand Forecast — Historical vs Predicted")
    hist = get_historical_sales()
    if hist.empty or artifact is None:
        st.info("Train the model to see demand charts.")
        return

    sku_options = sorted(hist["item"].unique())
    selected_sku = st.selectbox("SKU", sku_options, key="demand_sku")

    sku_hist = hist[hist["item"] == selected_sku].sort_values("date")
    last_date = datetime.strptime(str(sku_hist["date"].iloc[-1])[:10], "%Y-%m-%d").replace(
        tzinfo=timezone.utc
    )
    future_dates = [last_date + timedelta(days=i) for i in range(1, 15)]
    predictions = [
        predict_daily_demand(artifact, selected_sku, d) for d in future_dates
    ]
    pred_df = pd.DataFrame(
        {"date": [d.strftime("%Y-%m-%d") for d in future_dates], "sales": predictions, "series": "Predicted"}
    )

    hist_plot = sku_hist.copy()
    hist_plot["date"] = hist_plot["date"].astype(str).str[:10]
    hist_plot["series"] = "Historical"
    combined = pd.concat(
        [
            hist_plot[["date", "sales", "series"]],
            pred_df[["date", "sales", "series"]],
        ],
        ignore_index=True,
    )

    st.line_chart(
        combined.pivot_table(index="date", columns="series", values="sales", aggfunc="sum"),
        use_container_width=True,
    )

    if selected_sku in artifact.get("metrics", {}):
        st.caption(f"Validation MAE: {artifact['metrics'][selected_sku]:.2f} units/day")


def render_restock_recommendations(conn: sqlite3.Connection, artifact: dict | None) -> None:
    st.subheader("Restock Recommendations")
    st.caption(
        f"Lead Time = {LEAD_TIME_DAYS} days · Z-score = {Z_SCORE} · "
        f"Initial stock per slot = {INITIAL_STOCK_PER_SLOT.get(1, 'N/A')}"
    )

    if artifact is None:
        st.info("Train the model (`python train_model.py`) to see restock recommendations.")
        return

    results = evaluate_all_slots(artifact=artifact, conn=conn)
    for r in results:
        flag = "⚠️ RESTOCK NEEDED" if r.needs_restock else "✅ Stock OK"
        with st.expander(f"Slot {r.slot_id} — {r.name} ({r.sku}) — {flag}", expanded=r.needs_restock):
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Inventory**")
                st.write(f"Current stock: **{r.current_stock}**")
                st.write(f"Removals (last 30d): **{r.removals_in_window}**")
                st.write(f"Predicted daily demand: **{r.predicted_daily_demand}**")
                if r.needs_restock:
                    st.error(f"Shortfall vs reorder point: **{r.shortfall:.1f}** units")
            with c2:
                st.markdown("**Reorder Point Math**")
                st.code(format_reorder_math(r), language=None)


def main() -> None:
    if st.sidebar.button("Refresh now"):
        st.cache_data.clear()
        st.rerun()

    st.sidebar.caption("Click **Refresh now** after new serial events.")

    conn = ensure_db()
    try:
        artifact = get_model()

        render_slot_status(conn, artifact)
        st.divider()

        left, right = st.columns([1, 1])
        with left:
            render_event_log(conn)
        with right:
            render_demand_chart(artifact)

        st.divider()
        render_restock_recommendations(conn, artifact)
    finally:
        # Streamlit reruns can raise mid-render (st.rerun, widget exceptions);
        # without this the SQLite connection leaks on every rerun.
        conn.close()


if __name__ == "__main__":
    main()
