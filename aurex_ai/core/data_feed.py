"""
Aurex AI Signature Strategy — Data Feed Abstraction

Provides a uniform Candle model and a DataFeed class that sources candles
from either a live MT5 terminal or historical CSV files (backtest mode).

All strategy modules depend only on List[Candle] — they never touch MT5 or
pandas directly, making them trivially unit-testable.

Timeframe constants mirror MetaTrader5 values so they can be passed straight
through to the MT5 bridge without translation.
"""
from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

from aurex_ai.core.logger import get_logger

if TYPE_CHECKING:
    from aurex_ai.core.mt5_bridge import MT5Bridge

log = get_logger("core.data_feed")

# ── Timeframe constants ───────────────────────────────────────────────────────
# Values match MetaTrader5 TIMEFRAME_* constants.
TF_M1  = 1
TF_M5  = 5
TF_M15 = 15
TF_M30 = 30
TF_H1  = 16385   # mt5.TIMEFRAME_H1
TF_H4  = 16388   # mt5.TIMEFRAME_H4
TF_D1  = 16408   # mt5.TIMEFRAME_D1

TF_NAMES: Dict[int, str] = {
    TF_M1:  "M1",  TF_M5: "M5",  TF_M15: "M15", TF_M30: "M30",
    TF_H1:  "H1",  TF_H4: "H4",  TF_D1:  "D1",
}

# Reverse lookup used by CSV loader
_TF_MINUTES: Dict[str, int] = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440,
}


# ── Candle model ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Candle:
    time:   datetime
    open:   float
    high:   float
    low:    float
    close:  float
    volume: int
    symbol: str = ""
    tf:     str = ""

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def candle_range(self) -> float:
        return self.high - self.low

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open


# ── Account info model ────────────────────────────────────────────────────────

@dataclass
class AccountInfo:
    balance:   float
    equity:    float
    margin:    float
    free_margin: float
    currency:  str
    leverage:  int
    server:    str = ""


# ── DataFeed ──────────────────────────────────────────────────────────────────

class DataFeed:
    """
    Unified data access layer.

    In live mode  : delegates to MT5Bridge (asyncio.to_thread for blocking calls).
    In backtest mode: serves candles from pre-loaded CSV DataFrames.
    """

    def __init__(
        self,
        bridge:      Optional["MT5Bridge"] = None,
        backtest:    bool                  = False,
        data_dir:    Optional[str]         = None,
        bt_currency: str                   = "ZAR",
    ) -> None:
        self._bridge      = bridge
        self._backtest    = backtest
        self._data_dir    = Path(data_dir) if data_dir else None
        self._bt_currency = bt_currency   # account currency reported in backtest mode
        # backtest cache: symbol -> tf_name -> List[Candle]
        self._bt_cache: Dict[str, Dict[str, List[Candle]]] = {}
        # backtest cursor: symbol -> tf_name -> current index
        self._bt_cursor: Dict[str, Dict[str, int]] = {}

    # ── Live path ─────────────────────────────────────────────────────────────

    async def get_candles(
        self,
        symbol:    str,
        timeframe: int,
        count:     int,
    ) -> List[Candle]:
        """
        Fetch the last `count` closed candles for `symbol` on `timeframe`.

        Raises RuntimeError if bridge is not initialised in live mode.
        """
        if self._backtest:
            return self._bt_get_candles(symbol, timeframe, count)

        if self._bridge is None:
            raise RuntimeError("DataFeed.get_candles called in live mode but bridge is None")

        rows = await asyncio.to_thread(
            self._bridge.get_candles_raw, symbol, timeframe, count
        )
        tf_name = TF_NAMES.get(timeframe, str(timeframe))
        return [
            Candle(
                time   = datetime.fromtimestamp(r["time"], tz=timezone.utc),
                open   = float(r["open"]),
                high   = float(r["high"]),
                low    = float(r["low"]),
                close  = float(r["close"]),
                volume = int(r["tick_volume"]),
                symbol = symbol,
                tf     = tf_name,
            )
            for r in rows
        ]

    async def get_account_info(self) -> AccountInfo:
        if self._backtest:
            return AccountInfo(
                balance=100_000.0, equity=100_000.0,
                margin=0.0, free_margin=100_000.0,
                currency=self._bt_currency, leverage=100,
            )
        if self._bridge is None:
            raise RuntimeError("No bridge in live mode")
        raw = await asyncio.to_thread(self._bridge.get_account_info_raw)
        return AccountInfo(
            balance      = raw.balance,
            equity       = raw.equity,
            margin       = raw.margin,
            free_margin  = raw.margin_free,
            currency     = raw.currency,
            leverage     = raw.leverage,
            server       = raw.server,
        )

    async def get_tick(self, symbol: str) -> tuple[float, float]:
        """Return (bid, ask) for symbol."""
        if self._backtest:
            return 0.0, 0.0
        if self._bridge is None:
            raise RuntimeError("No bridge in live mode")
        return await asyncio.to_thread(self._bridge.get_tick_raw, symbol)

    # ── Backtest path ─────────────────────────────────────────────────────────

    def load_csv(self, symbol: str, tf_name: str, path: Optional[str] = None) -> int:
        """
        Load historical candles from a CSV file into the backtest cache.

        Expected CSV columns (case-insensitive):
            datetime, open, high, low, close, volume
        OR:
            time, open, high, low, close, tick_volume

        Returns the number of candles loaded.
        """
        if path is None:
            if self._data_dir is None:
                raise ValueError("data_dir must be set when loading CSVs without explicit path")
            path = str(self._data_dir / f"{symbol}_{tf_name}.csv")

        candles: List[Candle] = []
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                # Normalise all column names to lowercase so headers like "Open" or "OPEN" work
                norm = {k.lower().strip(): v for k, v in row.items()}
                dt_key  = "datetime" if "datetime" in norm else "time"
                vol_key = "volume"   if "volume"   in norm else "tick_volume"
                try:
                    raw_dt = norm.get(dt_key, norm.get("datetime", ""))
                    dt = datetime.fromisoformat(str(raw_dt).replace(" ", "T"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    candles.append(Candle(
                        time   = dt,
                        open   = float(norm["open"]),
                        high   = float(norm["high"]),
                        low    = float(norm["low"]),
                        close  = float(norm["close"]),
                        volume = int(float(norm.get(vol_key, 0))),
                        symbol = symbol,
                        tf     = tf_name,
                    ))
                except Exception as exc:
                    log.warning("CSV row parse error [%s %s]: %s", symbol, tf_name, exc)

        candles.sort(key=lambda c: c.time)
        self._bt_cache.setdefault(symbol, {})[tf_name] = candles
        self._bt_cursor.setdefault(symbol, {})[tf_name] = len(candles)
        log.info("backtest: loaded %d %s %s candles from %s", len(candles), symbol, tf_name, path)
        return len(candles)

    def set_backtest_cursor(self, symbol: str, tf_name: str, index: int) -> None:
        """Set the replay cursor so get_candles returns candles[:index]."""
        self._bt_cursor.setdefault(symbol, {})[tf_name] = index

    def _bt_get_candles(self, symbol: str, timeframe: int, count: int) -> List[Candle]:
        tf_name = TF_NAMES.get(timeframe, str(timeframe))
        sym_cache = self._bt_cache.get(symbol, {})
        all_candles = sym_cache.get(tf_name, [])
        if not all_candles:
            log.warning("backtest: no data for %s %s", symbol, tf_name)
            return []
        cursor = self._bt_cursor.get(symbol, {}).get(tf_name, len(all_candles))
        start  = max(0, cursor - count)
        return list(all_candles[start:cursor])
