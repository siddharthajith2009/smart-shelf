"""Train per-SKU demand forecasting models from historical retail data."""

from __future__ import annotations

import pickle
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import train_test_split

from config import (
    DATA_DIR,
    MODEL_PATH,
    RANDOM_STATE,
    SLOT_MAP,
    TRAIN_CSV,
    TRAIN_TEST_SPLIT,
)


def _sku_list() -> list[str]:
    return [meta["sku"] for meta in SLOT_MAP.values()]


def generate_synthetic_train_csv(path: Path | None = None) -> pd.DataFrame:
    """Create a small realistic fallback dataset when train.csv is missing."""
    path = path or TRAIN_CSV
    path.parent.mkdir(parents=True, exist_ok=True)

    skus = _sku_list()
    stores = ["STORE-1", "STORE-2"]
    rng = np.random.default_rng(RANDOM_STATE)
    start = datetime(2024, 1, 1)
    rows: list[dict] = []

    base_demand = {"SKU-A": 12, "SKU-B": 8, "SKU-C": 15, "SKU-D": 6}
    for sku in skus:
        if sku not in base_demand:
            warnings.warn(f"No base_demand configured for {sku}; defaulting to 10")

    for day_offset in range(180):
        date = start + timedelta(days=day_offset)
        dow = date.weekday()
        weekend_boost = 1.25 if dow >= 5 else 1.0
        seasonal = 1.0 + 0.15 * np.sin(2 * np.pi * day_offset / 30)

        for store in stores:
            for sku in skus:
                base = base_demand.get(sku, 10)
                noise = rng.normal(0, base * 0.2)
                sales = max(0, int(round(base * weekend_boost * seasonal + noise)))
                rows.append(
                    {
                        "date": date.strftime("%Y-%m-%d"),
                        "store": store,
                        "item": sku,
                        "sales": sales,
                    }
                )

    df = pd.DataFrame(rows)
    df.to_csv(path, index=False)
    print(f"Generated synthetic training data at {path} ({len(df)} rows)")
    return df


def load_training_data() -> pd.DataFrame:
    if not TRAIN_CSV.exists():
        return generate_synthetic_train_csv()

    df = pd.read_csv(TRAIN_CSV)
    expected = {"date", "store", "item", "sales"}
    if not expected.issubset(df.columns):
        missing = expected - set(df.columns)
        raise ValueError(
            f"train.csv schema mismatch. Missing columns: {sorted(missing)}. "
            f"Expected: date, store, item, sales"
        )
    return df


def _parse_date_parts(date_str: str) -> tuple[int, int, int, int]:
    """Return day_of_week, day_of_month, month, week_of_year without pd.to_datetime."""
    dt = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
    return dt.weekday(), dt.day, dt.month, dt.isocalendar().week


def _engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    parts = df["date"].map(_parse_date_parts)
    return df.assign(
        day_of_week=parts.map(lambda p: p[0]),
        day_of_month=parts.map(lambda p: p[1]),
        month=parts.map(lambda p: p[2]),
        week_of_year=parts.map(lambda p: p[3]),
        date_key=df["date"].astype(str).str[:10],
    )


FEATURE_COLS = ["day_of_week", "day_of_month", "month", "week_of_year"]


def train_models(df: pd.DataFrame) -> dict:
    """Train one GradientBoostingRegressor per SKU."""
    featured = _engineer_features(df)
    skus = sorted(featured["item"].unique())
    models: dict[str, dict] = {}
    metrics: dict[str, float] = {}

    for sku in skus:
        sku_df = featured[featured["item"] == sku]
        if len(sku_df) < 10:
            continue

        # Aggregate daily sales across stores for demand forecasting
        daily = (
            sku_df.groupby("date_key", as_index=False)["sales"]
            .sum()
            .rename(columns={"date_key": "date"})
            .merge(
                sku_df.drop_duplicates("date_key")[["date_key", *FEATURE_COLS]].rename(
                    columns={"date_key": "date"}
                ),
                on="date",
                how="left",
            )
        )

        daily = daily.sort_values("date")
        # The raw-row guard above doesn't cover multi-store data collapsing to
        # too few unique dates after aggregation.
        if len(daily) < 5:
            continue
        X = daily[FEATURE_COLS]
        y = daily["sales"]

        # Chronological holdout: validate on the most recent dates. Random
        # shuffling would leak future days into training and report an
        # optimistic MAE for what is a forward-looking demand forecast.
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=TRAIN_TEST_SPLIT, shuffle=False
        )
        if len(X_train) == 0 or len(X_test) == 0:
            continue

        model = GradientBoostingRegressor(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.1,
            random_state=RANDOM_STATE,
        )
        model.fit(X_train, y_train)
        preds = model.predict(X_test)
        mae = mean_absolute_error(y_test, preds)
        metrics[sku] = float(mae)

        # A NaN std would poison the safety-stock math downstream (comparisons
        # against NaN are always False, so restock would never trigger).
        std_val = daily["sales"].std(ddof=1) if len(daily) > 1 else 0.0
        models[sku] = {
            "model": model,
            "feature_cols": FEATURE_COLS,
            "historical_daily": daily[["date", "sales"]].copy(),
            "avg_daily_sales": float(daily["sales"].mean()),
            "std_daily_sales": 0.0 if pd.isna(std_val) else float(std_val),
        }

    return {
        "models": models,
        "metrics": metrics,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "feature_cols": FEATURE_COLS,
    }


def save_artifact(artifact: dict, path: Path | None = None) -> Path:
    path = path or MODEL_PATH
    with open(path, "wb") as f:
        pickle.dump(artifact, f)
    print(f"Saved model artifact to {path}")
    return path


def load_artifact(path: Path | None = None) -> dict:
    path = path or MODEL_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"Model not found at {path}. Run: python train_model.py"
        )
    with open(path, "rb") as f:
        return pickle.load(f)


def predict_daily_demand(artifact: dict, sku: str, target_date: datetime | None = None) -> float:
    """Predict demand for a SKU on a given date."""
    if sku not in artifact["models"]:
        return 0.0

    target_date = target_date or datetime.now(timezone.utc)
    entry = artifact["models"][sku]
    model = entry["model"]

    row = pd.DataFrame(
        [
            {
                "day_of_week": target_date.weekday(),
                "day_of_month": target_date.day,
                "month": target_date.month,
                "week_of_year": target_date.isocalendar().week,
            }
        ]
    )
    return float(max(0.0, model.predict(row[FEATURE_COLS])[0]))


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df = load_training_data()
    print(f"Loaded {len(df)} training rows for items: {sorted(df['item'].unique())}")
    artifact = train_models(df)
    save_artifact(artifact)
    for sku, mae in artifact["metrics"].items():
        print(f"  {sku}: validation MAE = {mae:.2f}")


if __name__ == "__main__":
    main()
