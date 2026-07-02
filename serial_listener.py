"""Listen for ESP32 shelf events over USB serial and log to SQLite."""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime, timezone

from config import BAUD_RATE, DB_PATH, SERIAL_PORT, SERIAL_TIMEOUT, SLOT_MAP

# Slot IDs must be positive integers (no zero, no leading zeros).
EVENT_PATTERN = re.compile(
    r"^SLOT:(?P<slot_id>[1-9]\d*),TS:(?P<timestamp>\d+(?:\.\d+)?)\s*$"
)


def init_db(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or str(DB_PATH)
    conn = sqlite3.connect(path, check_same_thread=False)
    # Name-keyed rows so every consumer (API + dashboard) can do dict(row);
    # reorder_logic.fetch_events relies on this.
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id INTEGER NOT NULL,
            sku TEXT NOT NULL,
            timestamp REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_slot_ts ON events(slot_id, timestamp)"
    )
    conn.commit()
    return conn


def parse_event(line: str) -> tuple[int, float] | None:
    line = line.strip()
    if not line:
        return None
    match = EVENT_PATTERN.match(line)
    if not match:
        print(f"[warn] Unrecognized line: {line!r}", file=sys.stderr)
        return None
    slot_id = int(match.group("slot_id"))
    ts = float(match.group("timestamp"))
    return slot_id, ts


def sku_for_slot(slot_id: int) -> str | None:
    meta = SLOT_MAP.get(slot_id)
    return meta["sku"] if meta else None


def log_event(conn: sqlite3.Connection, slot_id: int, sku: str, timestamp: float) -> None:
    conn.execute(
        "INSERT INTO events (slot_id, sku, timestamp) VALUES (?, ?, ?)",
        (slot_id, sku, timestamp),
    )
    conn.commit()


def handle_line(conn: sqlite3.Connection, line: str) -> bool:
    parsed = parse_event(line)
    if parsed is None:
        return False

    slot_id, ts = parsed
    sku = sku_for_slot(slot_id)
    if sku is None:
        print(f"[warn] Unknown slot_id={slot_id}, skipping", file=sys.stderr)
        return False

    log_event(conn, slot_id, sku, ts)
    when = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[event] slot={slot_id} sku={sku} ts={when}")
    return True


def listen(port: str | None = None, baud: int | None = None) -> None:
    # Imported lazily so init_db / parsing helpers can be used (by the API and
    # dashboard) on hosts that don't have pyserial or the hardware installed.
    import serial

    port = port or SERIAL_PORT
    baud = baud or BAUD_RATE
    conn = init_db()

    print(f"Opening serial port {port} @ {baud} baud …")
    print("Expected format: SLOT:<slot_id>,TS:<unix_timestamp>")
    print("Press Ctrl+C to stop.\n")

    try:
        with serial.Serial(port, baud, timeout=SERIAL_TIMEOUT) as ser:
            # Buffer raw bytes and only decode complete lines: decoding each
            # chunk separately corrupts multi-byte UTF-8 sequences that get
            # split across two reads.
            buffer = b""
            while True:
                chunk = ser.read(ser.in_waiting or 1)
                if not chunk:
                    continue
                buffer += chunk
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    handle_line(conn, raw_line.decode("utf-8", errors="replace"))
    except serial.SerialException as exc:
        print(f"[error] Serial connection failed: {exc}", file=sys.stderr)
        print(
            "Tip: update SERIAL_PORT in config.py or pass --port /dev/tty.usbmodem*",
            file=sys.stderr,
        )
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        conn.close()


def main() -> None:
    port = SERIAL_PORT
    if len(sys.argv) > 1:
        if sys.argv[1] in ("-h", "--help"):
            print("Usage: python serial_listener.py [PORT]")
            sys.exit(0)
        port = sys.argv[1]
    listen(port=port)


if __name__ == "__main__":
    main()
