"""Static configuration for the smart-shelf restocking system."""

from pathlib import Path

# Project paths
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TRAIN_CSV = DATA_DIR / "train.csv"
MODEL_PATH = BASE_DIR / "model.pkl"
DB_PATH = BASE_DIR / "events.db"

# Serial connection (override via environment or edit before deployment)
SERIAL_PORT = "/dev/tty.usbserial-0001"  # macOS/Linux; use COM3 on Windows
BAUD_RATE = 115200
SERIAL_TIMEOUT = 1.0

# Slot → SKU mapping (static, deliberate simplification)
SLOT_MAP: dict[int, dict[str, str]] = {
    1: {"sku": "SKU-A", "name": "Product A"},
    2: {"sku": "SKU-B", "name": "Product B"},
    3: {"sku": "SKU-C", "name": "Product C"},
    4: {"sku": "SKU-D", "name": "Product D"},
}

# Inventory / reorder tunables
LEAD_TIME_DAYS = 7
Z_SCORE = 1.65  # ~95% service level
INITIAL_STOCK_PER_SLOT: dict[int, int] = {slot: 50 for slot in SLOT_MAP}
REORDER_LOOKBACK_DAYS = 30  # window for observed demand std-dev

# Model training
TRAIN_TEST_SPLIT = 0.2
RANDOM_STATE = 42
