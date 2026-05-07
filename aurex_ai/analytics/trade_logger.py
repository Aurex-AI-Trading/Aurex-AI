"""
Aurex AI — Persistent Trade Logger

Records every trade open and close to SQLite (falls back to JSON if sqlite3
is unavailable).  Provides a queryable history for the analytics engine and
the score-component-based weight adjuster.

DB location: aurex_ai/analytics/data/trades.db
JSON fallback: aurex_ai/analytics/data/trades_log.json

Schema (trades table):
  ticket           INTEGER PRIMARY KEY  — MT5 ticket number
  signal_id        TEXT                 — "{symbol}-{scan_num}"
  symbol           TEXT
  direction        TEXT                 — BUY | SELL
  tier             INTEGER              — 1 | 2 | 3
  setup_type       TEXT
  confidence       REAL
  total_score      REAL
  trend_score      REAL
  liquidity_score  REAL
  fvg_score        REAL
  fib_score        REAL
  ema_score        REAL
  confirmation_score REAL
  structure_score  REAL
  ob_score         REAL
  breakout_score   REAL
  entry_price      REAL
  stop_loss        REAL
  take_profit      REAL
  lot_size         REAL
  risk_reward      REAL
  atr_pips         REAL
  utc_hour         INTEGER
  opened_at        TEXT                 — ISO-8601
  closed_at        TEXT                 — ISO-8601 (NULL while open)
  result           TEXT                 — OPEN | WIN | LOSS | BREAKEVEN
  pips             REAL
  profit_usd       REAL
  duration_minutes INTEGER
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from aurex_ai.core.logger import get_logger

log = get_logger("analytics.trade_logger")

_DB_PATH   = Path(__file__).resolve().parent / "data" / "trades.db"
_JSON_PATH = Path(__file__).resolve().parent / "data" / "trades_log.json"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    ticket             INTEGER PRIMARY KEY,
    signal_id          TEXT,
    symbol             TEXT,
    direction          TEXT,
    tier               INTEGER,
    setup_type         TEXT,
    confidence         REAL,
    total_score        REAL,
    trend_score        REAL,
    liquidity_score    REAL,
    fvg_score          REAL,
    fib_score          REAL,
    ema_score          REAL,
    confirmation_score REAL,
    structure_score    REAL,
    ob_score           REAL,
    breakout_score     REAL,
    entry_price        REAL,
    stop_loss          REAL,
    take_profit        REAL,
    lot_size           REAL,
    risk_reward        REAL,
    atr_pips           REAL,
    utc_hour           INTEGER,
    opened_at          TEXT,
    closed_at          TEXT,
    result             TEXT    DEFAULT 'OPEN',
    pips               REAL    DEFAULT 0.0,
    profit_usd         REAL    DEFAULT 0.0,
    duration_minutes   INTEGER DEFAULT 0
)
"""


@dataclass
class TradeRecord:
    ticket:             int
    signal_id:          str
    symbol:             str
    direction:          str
    tier:               int
    setup_type:         str
    confidence:         float
    total_score:        float
    trend_score:        float        = 0.0
    liquidity_score:    float        = 0.0
    fvg_score:          float        = 0.0
    fib_score:          float        = 0.0
    ema_score:          float        = 0.0
    confirmation_score: float        = 0.0
    structure_score:    float        = 0.0
    ob_score:           float        = 0.0
    breakout_score:     float        = 0.0
    entry_price:        float        = 0.0
    stop_loss:          float        = 0.0
    take_profit:        float        = 0.0
    lot_size:           float        = 0.0
    risk_reward:        float        = 0.0
    atr_pips:           float        = 0.0
    utc_hour:           int          = 0
    opened_at:          str          = ""
    closed_at:          str          = ""
    result:             str          = "OPEN"
    pips:               float        = 0.0
    profit_usd:         float        = 0.0
    duration_minutes:   int          = 0


class TradeLogger:
    """
    Thread-safe singleton trade logger.

    Usage:
        tl = TradeLogger.get_instance()
        tl.log_open(ticket=12345, signal_id="EURUSD.Z-7", ...)
        tl.log_close(ticket=12345, result="WIN", profit_usd=42.5, pips=28.0)
    """

    _instance: Optional["TradeLogger"] = None

    def __init__(self) -> None:
        self._lock       = threading.Lock()
        self._use_sqlite = self._init_db()

    @classmethod
    def get_instance(cls) -> "TradeLogger":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Open a new trade ─────────────────────────────────────────────────────

    def log_open(
        self,
        ticket:             int,
        signal_id:          str,
        symbol:             str,
        direction:          str,
        tier:               int,
        setup_type:         str,
        confidence:         float,
        total_score:        float,
        trend_score:        float        = 0.0,
        liquidity_score:    float        = 0.0,
        fvg_score:          float        = 0.0,
        fib_score:          float        = 0.0,
        ema_score:          float        = 0.0,
        confirmation_score: float        = 0.0,
        structure_score:    float        = 0.0,
        ob_score:           float        = 0.0,
        breakout_score:     float        = 0.0,
        entry_price:        float        = 0.0,
        stop_loss:          float        = 0.0,
        take_profit:        float        = 0.0,
        lot_size:           float        = 0.0,
        risk_reward:        float        = 0.0,
        atr_pips:           float        = 0.0,
        utc_hour:           int          = 0,
        opened_at:          Optional[str] = None,
    ) -> None:
        rec = TradeRecord(
            ticket             = ticket,
            signal_id          = signal_id,
            symbol             = symbol,
            direction          = direction,
            tier               = tier,
            setup_type         = setup_type,
            confidence         = round(confidence,         2),
            total_score        = round(total_score,        2),
            trend_score        = round(trend_score,        2),
            liquidity_score    = round(liquidity_score,    2),
            fvg_score          = round(fvg_score,          2),
            fib_score          = round(fib_score,          2),
            ema_score          = round(ema_score,          2),
            confirmation_score = round(confirmation_score, 2),
            structure_score    = round(structure_score,    2),
            ob_score           = round(ob_score,           2),
            breakout_score     = round(breakout_score,     2),
            entry_price        = round(entry_price,        5),
            stop_loss          = round(stop_loss,          5),
            take_profit        = round(take_profit,        5),
            lot_size           = round(lot_size,           2),
            risk_reward        = round(risk_reward,        2),
            atr_pips           = round(atr_pips,           1),
            utc_hour           = utc_hour,
            opened_at          = opened_at or _now_iso(),
            result             = "OPEN",
        )
        try:
            if self._use_sqlite:
                self._sqlite_insert(rec)
            else:
                self._json_insert(rec)
            log.info(
                "[TRADE LOG] OPEN ticket=%d %s %s tier=%d score=%.1f conf=%.0f",
                ticket, symbol, direction, tier, total_score, confidence,
            )
        except Exception as exc:
            log.error("[TRADE LOG] log_open failed: %s", exc)

    # ── Close an existing trade ───────────────────────────────────────────────

    def log_close(
        self,
        ticket:           int,
        result:           str,           # "WIN" | "LOSS" | "BREAKEVEN"
        profit_usd:       float  = 0.0,
        pips:             float  = 0.0,
        duration_minutes: int    = 0,
        closed_at:        Optional[str] = None,
    ) -> None:
        ts = closed_at or _now_iso()
        try:
            if self._use_sqlite:
                self._sqlite_close(ticket, result, profit_usd, pips, duration_minutes, ts)
            else:
                self._json_close(ticket, result, profit_usd, pips, duration_minutes, ts)
            log.info(
                "[TRADE LOG] CLOSE ticket=%d result=%s profit=%.2f pips=%.1f dur=%dm",
                ticket, result, profit_usd, pips, duration_minutes,
            )
        except Exception as exc:
            log.error("[TRADE LOG] log_close failed: %s", exc)

    # ── Query helpers ─────────────────────────────────────────────────────────

    def get_closed_trades(self, limit: int = 50) -> List[Dict]:
        """Return the most recent `limit` closed trades as dicts, newest first."""
        try:
            if self._use_sqlite:
                return self._sqlite_query(
                    "SELECT * FROM trades WHERE result != 'OPEN' "
                    "ORDER BY closed_at DESC LIMIT ?",
                    (limit,),
                )
            return self._json_query_closed(limit)
        except Exception as exc:
            log.error("[TRADE LOG] get_closed_trades failed: %s", exc)
            return []

    def get_open_trades(self) -> List[Dict]:
        """Return all currently open trades."""
        try:
            if self._use_sqlite:
                return self._sqlite_query(
                    "SELECT * FROM trades WHERE result = 'OPEN' ORDER BY opened_at DESC",
                    (),
                )
            return self._json_query_open()
        except Exception as exc:
            log.error("[TRADE LOG] get_open_trades failed: %s", exc)
            return []

    def get_all_trades(self, limit: int = 200) -> List[Dict]:
        """Return most recent trades of any status."""
        try:
            if self._use_sqlite:
                return self._sqlite_query(
                    "SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?",
                    (limit,),
                )
            return self._json_query_all(limit)
        except Exception as exc:
            log.error("[TRADE LOG] get_all_trades failed: %s", exc)
            return []

    def total_closed(self) -> int:
        try:
            if self._use_sqlite:
                rows = self._sqlite_query(
                    "SELECT COUNT(*) AS n FROM trades WHERE result != 'OPEN'", ()
                )
                return int(rows[0]["n"]) if rows else 0
            return len(self._json_query_closed(9999))
        except Exception:
            return 0

    # ── SQLite backend ────────────────────────────────────────────────────────

    def _init_db(self) -> bool:
        try:
            _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(_DB_PATH)) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(_CREATE_SQL)
                conn.commit()
            return True
        except Exception as exc:
            log.warning("[TRADE LOG] SQLite init failed (%s) — using JSON fallback", exc)
            _JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
            return False

    def _sqlite_insert(self, rec: TradeRecord) -> None:
        d = asdict(rec)
        cols   = ", ".join(d.keys())
        placeholders = ", ".join("?" * len(d))
        sql    = f"INSERT OR IGNORE INTO trades ({cols}) VALUES ({placeholders})"
        with self._lock:
            with sqlite3.connect(str(_DB_PATH)) as conn:
                conn.execute(sql, list(d.values()))
                conn.commit()

    def _sqlite_close(
        self,
        ticket: int, result: str,
        profit: float, pips: float,
        duration: int, ts: str,
    ) -> None:
        sql = (
            "UPDATE trades SET result=?, profit_usd=?, pips=?, "
            "duration_minutes=?, closed_at=? WHERE ticket=?"
        )
        with self._lock:
            with sqlite3.connect(str(_DB_PATH)) as conn:
                conn.execute(sql, (result, round(profit, 2), round(pips, 1), duration, ts, ticket))
                conn.commit()

    def _sqlite_query(self, sql: str, params: tuple) -> List[Dict]:
        with self._lock:
            with sqlite3.connect(str(_DB_PATH)) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(sql, params)
                return [dict(row) for row in cursor.fetchall()]

    # ── JSON fallback backend ─────────────────────────────────────────────────

    def _json_load(self) -> List[Dict]:
        if _JSON_PATH.exists():
            try:
                return json.loads(_JSON_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    def _json_save(self, records: List[Dict]) -> None:
        try:
            _JSON_PATH.write_text(json.dumps(records, indent=2), encoding="utf-8")
        except Exception as exc:
            log.error("[TRADE LOG] JSON save failed: %s", exc)

    def _json_insert(self, rec: TradeRecord) -> None:
        with self._lock:
            records = self._json_load()
            records.append(asdict(rec))
            # Keep most recent 500 entries
            if len(records) > 500:
                records = records[-500:]
            self._json_save(records)

    def _json_close(
        self, ticket: int, result: str,
        profit: float, pips: float, duration: int, ts: str,
    ) -> None:
        with self._lock:
            records = self._json_load()
            for r in records:
                if r.get("ticket") == ticket:
                    r["result"]           = result
                    r["profit_usd"]       = round(profit, 2)
                    r["pips"]             = round(pips, 1)
                    r["duration_minutes"] = duration
                    r["closed_at"]        = ts
                    break
            self._json_save(records)

    def _json_query_closed(self, limit: int) -> List[Dict]:
        records = self._json_load()
        closed  = [r for r in records if r.get("result", "OPEN") != "OPEN"]
        return sorted(closed, key=lambda r: r.get("closed_at", ""), reverse=True)[:limit]

    def _json_query_open(self) -> List[Dict]:
        records = self._json_load()
        return [r for r in records if r.get("result", "OPEN") == "OPEN"]

    def _json_query_all(self, limit: int) -> List[Dict]:
        records = self._json_load()
        return sorted(records, key=lambda r: r.get("opened_at", ""), reverse=True)[:limit]


# ── Module-level helpers ──────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
