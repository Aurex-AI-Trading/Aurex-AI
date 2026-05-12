"""
Aurex AI — Structural Event Logger  (Phase 12)

Logs MTF structural events (OB, FVG, BOS, CHoCH, liquidity sweeps,
displacement candles) detected by the strategy engine — even when no
trade is executed.  Tracks subsequent price movement to build
event-level expectancy analytics.

DB: analytics/data/signals.db (structural_events table alongside signals)

This enables:
  - Setup expectancy per event type / session / regime
  - Directional accuracy of structural signals
  - Regime-level structural edge quantification

Logs emitted
------------
  [STRUCTURAL EVENT]        — every event logged
  [STRUCTURAL EXPECTANCY]   — periodic summary by event type
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from aurex_ai.core.logger import get_logger

log = get_logger("analytics.event_logger")

_DB_PATH = Path(__file__).parent / "data" / "signals.db"

# Canonical event type constants
EVT_OB           = "OB"
EVT_FVG          = "FVG"
EVT_BOS          = "BOS"
EVT_CHOCH        = "CHOCH"
EVT_LIQ_SWEEP    = "LIQ_SWEEP"
EVT_DISPLACEMENT = "DISPLACEMENT"
EVT_CONTINUATION = "CONTINUATION"

ALL_EVENT_TYPES = (EVT_OB, EVT_FVG, EVT_BOS, EVT_CHOCH, EVT_LIQ_SWEEP, EVT_DISPLACEMENT, EVT_CONTINUATION)

_RESOLUTION_HOURS = 6   # events older than this with no resolution are abandoned

_CREATE_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS structural_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol           TEXT NOT NULL,
    timeframe        TEXT DEFAULT 'M15',
    event_type       TEXT NOT NULL,
    direction        TEXT DEFAULT '',
    detected_at      TEXT NOT NULL,
    utc_hour         INTEGER DEFAULT 0,
    session          TEXT DEFAULT '',
    market_state     TEXT DEFAULT '',
    atr_pips         REAL DEFAULT 0,
    price_level      REAL DEFAULT 0,
    resolved_at      TEXT,
    result_pips      REAL,
    result_direction TEXT,
    was_correct      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_evt_symbol ON structural_events(symbol);
CREATE INDEX IF NOT EXISTS idx_evt_type   ON structural_events(event_type);
CREATE INDEX IF NOT EXISTS idx_evt_time   ON structural_events(detected_at);
"""


@dataclass
class StructuralEvent:
    """Payload for a single detected structural event."""
    symbol:       str
    event_type:   str          # EVT_* constant
    direction:    str          # "BUY" | "SELL" | ""
    utc_hour:     int
    timeframe:    str   = "M15"
    session:      str   = ""
    market_state: str   = ""
    atr_pips:     float = 0.0
    price_level:  float = 0.0


class EventLogger:
    """
    Singleton structural event logger.

    Usage:
        el  = EventLogger.get_instance()
        eid = el.log_event(StructuralEvent(...))
        el.resolve_event(eid, result_pips=15.0, result_direction="BUY")
        el.log_expectancy_summary()
    """
    _instance: Optional["EventLogger"] = None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @classmethod
    def get_instance(cls) -> "EventLogger":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(_DB_PATH), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            for stmt in _CREATE_EVENTS_SQL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    conn.execute(stmt)

    # ── Write ─────────────────────────────────────────────────────────────────

    def log_event(self, event: StructuralEvent) -> int:
        """Persist a detected structural event. Returns the row ID."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO structural_events (
                    symbol, timeframe, event_type, direction,
                    detected_at, utc_hour, session, market_state,
                    atr_pips, price_level
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                event.symbol, event.timeframe, event.event_type, event.direction,
                now, event.utc_hour, event.session, event.market_state,
                event.atr_pips, event.price_level,
            ))
            eid = int(cur.lastrowid)

        log.info(
            "[STRUCTURAL EVENT] id=%d %s %s %s dir=%s session=%s atr=%.1f",
            eid, event.event_type, event.symbol, event.timeframe,
            event.direction, event.session, event.atr_pips,
        )
        return eid

    def resolve_event(
        self,
        event_id:         int,
        result_pips:      float,
        result_direction: str,
    ) -> None:
        """Record the actual price outcome for a previously logged event."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT direction FROM structural_events WHERE id=?", (event_id,)
            ).fetchone()
            if not row:
                return
            was_correct = 1 if (row["direction"] == result_direction) else 0
            conn.execute("""
                UPDATE structural_events
                SET resolved_at=?, result_pips=?, result_direction=?, was_correct=?
                WHERE id=?
            """, (now, result_pips, result_direction, was_correct, event_id))

    # ── Expire old unresolved events ──────────────────────────────────────────

    def expire_old_events(self) -> int:
        """Remove resolution obligation for events older than _RESOLUTION_HOURS."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=_RESOLUTION_HOURS)
        ).isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            cur = conn.execute("""
                UPDATE structural_events
                SET resolved_at=?, result_pips=0, result_direction='EXPIRED', was_correct=0
                WHERE resolved_at IS NULL AND detected_at < ?
            """, (datetime.now(timezone.utc).isoformat(timespec="seconds"), cutoff))
            return cur.rowcount

    # ── Analytics ─────────────────────────────────────────────────────────────

    def get_expectancy(self, event_type: str, window: int = 100) -> Dict:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT result_pips, was_correct
                FROM   structural_events
                WHERE  event_type = ?
                  AND  result_pips IS NOT NULL
                  AND  result_direction != 'EXPIRED'
                ORDER  BY detected_at DESC
                LIMIT  ?
            """, (event_type, window)).fetchall()

        if not rows:
            return {"event_type": event_type, "n": 0, "accuracy": 0.0, "avg_pips": 0.0}

        n        = len(rows)
        correct  = sum(r["was_correct"] or 0 for r in rows)
        avg_pips = sum(r["result_pips"] or 0.0 for r in rows) / n
        return {
            "event_type": event_type,
            "n":          n,
            "accuracy":   round(correct / n, 3),
            "avg_pips":   round(avg_pips, 2),
        }

    def get_all_expectancies(self, window: int = 100) -> List[Dict]:
        return [self.get_expectancy(evt, window) for evt in ALL_EVENT_TYPES]

    def log_expectancy_summary(self, min_n: int = 5) -> None:
        """Emit [STRUCTURAL EXPECTANCY] logs for all event types with n ≥ min_n."""
        for evt_type in ALL_EVENT_TYPES:
            stats = self.get_expectancy(evt_type)
            if stats["n"] >= min_n:
                log.warning(
                    "[STRUCTURAL EXPECTANCY] %-14s | n=%d accuracy=%.1f%% avg_pips=%+.1f",
                    stats["event_type"], stats["n"],
                    stats["accuracy"] * 100, stats["avg_pips"],
                )
