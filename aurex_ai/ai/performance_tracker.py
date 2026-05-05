"""
Aurex AI — Performance Tracker

Tracks win rates and outcomes across multiple dimensions:
  - Per symbol
  - Per strategy tier (1, 2, 3)
  - Per session (asian, london, newyork, overlap)
  - Per setup type (FVG+OB, FVG+Sweep, OB+Sweep, Breakout, etc.)

Data is persisted to a JSON file so it survives process restarts.
Used by the learning engine to inform dynamic weight adjustments.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from aurex_ai.core.mt5_bridge import get_mt5_time

from aurex_ai.core.logger import get_logger

log = get_logger("ai.performance_tracker")

_DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "performance.json"


@dataclass
class PerfBucket:
    trades:      int   = 0
    wins:        int   = 0
    total_pnl:   float = 0.0
    total_rr:    float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades > 0 else 0.0

    @property
    def avg_rr(self) -> float:
        return self.total_rr / self.trades if self.trades > 0 else 0.0

    def record(self, won: bool, pnl: float = 0.0, rr: float = 0.0) -> None:
        self.trades += 1
        if won:
            self.wins += 1
        self.total_pnl += pnl
        self.total_rr  += rr


@dataclass
class PerformanceData:
    symbols:   Dict[str, PerfBucket]  = field(default_factory=dict)
    tiers:     Dict[str, PerfBucket]  = field(default_factory=dict)
    sessions:  Dict[str, PerfBucket]  = field(default_factory=dict)
    setups:    Dict[str, PerfBucket]  = field(default_factory=dict)
    overall:   PerfBucket             = field(default_factory=PerfBucket)
    last_updated: str                 = ""


def _get_session(utc_hour: int) -> str:
    """Return session name for a given UTC hour."""
    if 7 <= utc_hour < 13:
        return "london"
    elif 13 <= utc_hour < 16:
        return "overlap"
    elif 16 <= utc_hour < 22:
        return "newyork"
    else:
        return "asian"


class PerformanceTracker:
    """
    Thread-safe performance tracker. Singleton per process.

    Usage:
        tracker = PerformanceTracker.get_instance()
        tracker.record_trade(
            symbol="EURUSD", tier=1, setup_type="FVG+OB",
            won=True, pnl=45.0, rr=2.1,
        )
        wr = tracker.get_win_rate("symbol", "EURUSD")
    """
    _instance: Optional["PerformanceTracker"] = None

    def __init__(self, data_path: Path = _DEFAULT_PATH) -> None:
        self._path = data_path
        self._lock = threading.Lock()
        self._data = self._load()

    @classmethod
    def get_instance(cls, data_path: Path = _DEFAULT_PATH) -> "PerformanceTracker":
        if cls._instance is None:
            cls._instance = cls(data_path)
        return cls._instance

    # ── Record outcomes ───────────────────────────────────────────────────────

    def record_trade(
        self,
        symbol:     str,
        tier:       int,
        setup_type: str,
        won:        bool,
        pnl:        float = 0.0,
        rr:         float = 0.0,
        utc_hour:   Optional[int] = None,
    ) -> None:
        if utc_hour is None:
            utc_hour = get_mt5_time().hour
        session = _get_session(utc_hour)

        with self._lock:
            # Ensure buckets exist
            if symbol not in self._data.symbols:
                self._data.symbols[symbol] = PerfBucket()
            tier_key = str(tier)
            if tier_key not in self._data.tiers:
                self._data.tiers[tier_key] = PerfBucket()
            if session not in self._data.sessions:
                self._data.sessions[session] = PerfBucket()
            if setup_type not in self._data.setups:
                self._data.setups[setup_type] = PerfBucket()

            self._data.symbols[symbol].record(won, pnl, rr)
            self._data.tiers[tier_key].record(won, pnl, rr)
            self._data.sessions[session].record(won, pnl, rr)
            self._data.setups[setup_type].record(won, pnl, rr)
            self._data.overall.record(won, pnl, rr)
            self._data.last_updated = get_mt5_time().isoformat()

        self._save()

        log.info(
            "[LEARNING] trade recorded: %s tier=%d setup=%s won=%s pnl=%.2f rr=%.2f session=%s",
            symbol, tier, setup_type, won, pnl, rr, session,
        )

    # ── Query win rates ───────────────────────────────────────────────────────

    def get_win_rate(self, dimension: str, key: str) -> float:
        """
        Get win rate for a specific dimension/key.
        dimension: "symbol" | "tier" | "session" | "setup"
        """
        with self._lock:
            if dimension == "symbol":
                bucket = self._data.symbols.get(key)
            elif dimension == "tier":
                bucket = self._data.tiers.get(str(key))
            elif dimension == "session":
                bucket = self._data.sessions.get(key)
            elif dimension == "setup":
                bucket = self._data.setups.get(key)
            else:
                return 0.5
        return bucket.win_rate if bucket else 0.5

    def get_trade_count(self, dimension: str, key: str) -> int:
        with self._lock:
            if dimension == "symbol":
                bucket = self._data.symbols.get(key)
            elif dimension == "tier":
                bucket = self._data.tiers.get(str(key))
            elif dimension == "session":
                bucket = self._data.sessions.get(key)
            else:
                return 0
        return bucket.trades if bucket else 0

    def get_overall_stats(self) -> dict:
        with self._lock:
            o = self._data.overall
            return {
                "total_trades": o.trades,
                "wins":         o.wins,
                "win_rate":     round(o.win_rate, 3),
                "total_pnl":    round(o.total_pnl, 2),
                "avg_rr":       round(o.avg_rr, 2),
                "by_symbol":    {k: {"trades": v.trades, "win_rate": round(v.win_rate, 3)} for k, v in self._data.symbols.items()},
                "by_tier":      {k: {"trades": v.trades, "win_rate": round(v.win_rate, 3)} for k, v in self._data.tiers.items()},
                "by_session":   {k: {"trades": v.trades, "win_rate": round(v.win_rate, 3)} for k, v in self._data.sessions.items()},
                "by_setup":     {k: {"trades": v.trades, "win_rate": round(v.win_rate, 3)} for k, v in self._data.setups.items()},
            }

    def get_symbol_win_rate(self, symbol: str, min_trades: int = 10) -> Optional[float]:
        """Returns win rate only if we have enough data, otherwise None."""
        with self._lock:
            bucket = self._data.symbols.get(symbol)
        if bucket and bucket.trades >= min_trades:
            return bucket.win_rate
        return None

    def should_reduce_frequency(self, symbol: str, threshold: float = 0.40) -> bool:
        """Return True if symbol win rate is below threshold with enough data."""
        wr = self.get_symbol_win_rate(symbol)
        return wr is not None and wr < threshold

    def is_session_favorable(self, utc_hour: int, threshold: float = 0.55) -> bool:
        session = _get_session(utc_hour)
        wr = self.get_win_rate("session", session)
        count = self.get_trade_count("session", session)
        return count < 10 or wr >= threshold  # always trade if not enough data

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> PerformanceData:
        if self._path.exists():
            try:
                with self._path.open("r", encoding="utf-8") as f:
                    raw = json.load(f)

                data = PerformanceData()
                for sym, v in raw.get("symbols", {}).items():
                    data.symbols[sym] = PerfBucket(**v)
                for k, v in raw.get("tiers", {}).items():
                    data.tiers[k] = PerfBucket(**v)
                for k, v in raw.get("sessions", {}).items():
                    data.sessions[k] = PerfBucket(**v)
                for k, v in raw.get("setups", {}).items():
                    data.setups[k] = PerfBucket(**v)
                if "overall" in raw:
                    data.overall = PerfBucket(**raw["overall"])
                data.last_updated = raw.get("last_updated", "")
                return data
            except Exception as exc:
                log.warning("Failed to load performance data: %s — starting fresh", exc)

        return PerformanceData()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            payload = {
                "symbols":      {k: asdict(v) for k, v in self._data.symbols.items()},
                "tiers":        {k: asdict(v) for k, v in self._data.tiers.items()},
                "sessions":     {k: asdict(v) for k, v in self._data.sessions.items()},
                "setups":       {k: asdict(v) for k, v in self._data.setups.items()},
                "overall":      asdict(self._data.overall),
                "last_updated": self._data.last_updated,
            }
        try:
            with self._path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as exc:
            log.error("Failed to save performance data: %s", exc)
