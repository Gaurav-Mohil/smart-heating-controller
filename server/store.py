"""SQLite-backed storage.

Everything the controller needs to remember lives in one small SQLite file:
settings and state as JSON documents in a key/value table, plus a few
append-only tables (activity log, daily usage, meter readings, reports).
"""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT PRIMARY KEY,
    kwh REAL NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meter_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kwh REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    period TEXT NOT NULL,
    fmt TEXT NOT NULL,
    action TEXT NOT NULL
);
"""

ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
WEEKDAYS = [0, 1, 2, 3, 4]

DEFAULTS = {
    "settings": {
        "location": {
            "name": "Fredericton, NB",
            "lat": 45.9636,
            "lon": -66.6431,
            "tz": "America/Moncton",
        },
        "rate_cents_per_kwh": None,
    },
    "control": {"mode": "auto", "manual_temp": 21, "away_temp": 18, "hold": None},
    "schedule": [
        {"id": "p1", "name": "Morning", "start": "06:30", "temp": 21, "days": ALL_DAYS},
        {"id": "p2", "name": "Away", "start": "08:00", "temp": 18, "days": WEEKDAYS},
        {"id": "p3", "name": "Evening", "start": "17:00", "temp": 21, "days": ALL_DAYS},
        {"id": "p4", "name": "Overnight", "start": "22:30", "temp": 19, "days": ALL_DAYS},
    ],
    "rules": {
        "preheat_suggestions": True,
        "auto_apply": False,
        "drop_threshold": 2.0,
        "window_hours": 5,
        "preheat_boost": 1,
    },
    "calibration": {
        "min_angle": 60,
        "max_angle": 120,
        # Example mapping only. Replace each point by measuring the real dial.
        "points": [
            {"temp": 18, "angle": 60, "measured": False},
            {"temp": 19, "angle": 70, "measured": False},
            {"temp": 20, "angle": 80, "measured": False},
            {"temp": 21, "angle": 90, "measured": False},
            {"temp": 22, "angle": 100, "measured": False},
            {"temp": 23, "angle": 110, "measured": False},
            {"temp": 24, "angle": 120, "measured": False},
        ],
    },
    "suggestions": {},  # suggestion id -> "accepted" | "dismissed"
    "events": [],  # accepted one-off preheats: {id, start, end, temp}
    "device": {},
    "command": None,  # one-shot command for the controller (e.g. servo test)
    "forecast_cache": None,
    "weather_daily": {},  # "YYYY-MM-DD" -> mean temperature
    "homeassistant": {"enabled": False, "url": "", "token": "", "entity_id": "", "last_pushed": None, "last_error": None},
    "runtime": {"last_decision": None, "last_target": None},
}


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---- key/value documents -------------------------------------------------
    def get(self, key: str):
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None:
            return copy.deepcopy(DEFAULTS.get(key))
        return json.loads(row["value"])

    def set(self, key: str, value) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
            self._conn.commit()

    def update(self, key: str, **changes):
        value = self.get(key) or {}
        value.update(changes)
        self.set(key, value)
        return value

    # ---- activity log ----------------------------------------------------------
    def log(self, kind: str, text: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO log (ts, kind, text) VALUES (?, ?, ?)", (time.time(), kind, text)
            )
            # Keep the log from growing forever.
            self._conn.execute("DELETE FROM log WHERE id <= (SELECT MAX(id) FROM log) - 500")
            self._conn.commit()

    def recent_log(self, limit: int = 12):
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, kind, text FROM log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- electricity usage -----------------------------------------------------
    def upsert_usage(self, day: str, kwh: float, source: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage (day, kwh, source) VALUES (?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET kwh = excluded.kwh, source = excluded.source",
                (day, kwh, source),
            )
            self._conn.commit()

    def usage_between(self, start: str, end: str):
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, kwh, source FROM usage WHERE day >= ? AND day <= ? ORDER BY day",
                (start, end),
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_usage_day(self):
        with self._lock:
            row = self._conn.execute("SELECT MAX(day) AS d FROM usage").fetchone()
        return row["d"] if row else None

    def add_meter_reading(self, ts: float, kwh: float) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO meter_readings (ts, kwh) VALUES (?, ?)", (ts, kwh))
            self._conn.commit()

    def last_meter_reading(self):
        with self._lock:
            row = self._conn.execute(
                "SELECT ts, kwh FROM meter_readings ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    # ---- reports -----------------------------------------------------------------
    def add_report(self, period: str, fmt: str, action: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO reports (ts, period, fmt, action) VALUES (?, ?, ?, ?)",
                (time.time(), period, fmt, action),
            )
            self._conn.commit()

    def recent_reports(self, limit: int = 10):
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, period, fmt, action FROM reports ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
