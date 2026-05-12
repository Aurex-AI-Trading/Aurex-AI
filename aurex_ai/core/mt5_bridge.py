"""
Aurex AI Signature Strategy — MT5 Bridge

Low-level wrapper around the MetaTrader5 Python library.

Responsibilities:
  - Connection management with exponential-backoff retry
  - Raw candle retrieval (numpy structured array -> passed through to DataFeed)
  - Order submission (BUY/SELL market orders with SL/TP)
  - Position and account queries
  - Graceful shutdown

All blocking MT5 calls are synchronous (MetaTrader5 library is not async).
The DataFeed wraps them in asyncio.to_thread() so the event loop never blocks.

DRY_RUN mode:
  - Connection is attempted but no orders are sent to MT5.
  - execute_order() returns a simulated result with a synthetic ticket number.

Non-Windows / import fallback:
  - If MetaTrader5 cannot be imported, the bridge operates in simulation mode.
    This allows unit testing and development on non-Windows machines.
"""
from __future__ import annotations

import datetime as _dt
import random
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from aurex_ai.core.logger import get_logger

log = get_logger("core.mt5_bridge")

# ── Optional MT5 import ───────────────────────────────────────────────────────
try:
    import MetaTrader5 as mt5
    _MT5_AVAILABLE = True
except ImportError:
    mt5 = None           # type: ignore[assignment]
    _MT5_AVAILABLE = False
    log.warning("MetaTrader5 not installed — bridge running in simulation mode")


# ── MT5 server time helper ────────────────────────────────────────────────────

_MAX_TICK_AGE_SECONDS   = 300   # tick older than 5 min = market closed or MT5 frozen
_TIME_WARN_THRESHOLD    = 30    # seconds — log [TIME WARNING] when VPS↔MT5 drift exceeds this
_TIME_CRITICAL_THRESHOLD = 120  # seconds — log [TIME CRITICAL] when drift exceeds this

# Set True when get_mt5_time() returned actual broker tick time; False = local UTC fallback.
_mt5_time_fresh: bool = False


def is_mt5_time_fresh() -> bool:
    """True if the most recent get_mt5_time() call returned verified MT5 broker time."""
    return _mt5_time_fresh


def log_time_sync_banner(symbol: str = "EURUSD.Z") -> float:
    """
    Log a startup time-sync validation banner comparing local PC time to MT5 broker time.

    Call once after bridge.connect() succeeds.  Visible at WARNING level so it
    appears in the console regardless of log verbosity.

    Returns:
        abs(delta_secs) — VPS↔MT5 drift in seconds (0.0 when MT5 unavailable).
        Caller should emit [TIME WARNING] / [TIME CRITICAL] based on thresholds.
    """
    local_utc  = _dt.datetime.now(_dt.timezone.utc)
    mt5_time   = get_mt5_time(symbol)
    fresh      = is_mt5_time_fresh()
    source     = "MT5 LIVE TICK" if fresh else "LOCAL UTC (MT5 tick unavailable)"

    # PC wall-clock in local timezone — human-readable display only, not used in trading
    local_wall      = _dt.datetime.now()
    tz_offset_secs  = (local_wall - local_utc.replace(tzinfo=None)).total_seconds()
    tz_sign         = "+" if tz_offset_secs >= 0 else "-"
    tz_hours        = int(abs(tz_offset_secs) // 3600)
    tz_mins         = int((abs(tz_offset_secs) % 3600) // 60)

    # Broker drift: difference between MT5 broker UTC and VPS UTC clock.
    # Near-zero when NTP is healthy and MT5 feed is live.
    # Positive  = VPS clock is BEHIND broker (clock was set too early).
    # Negative  = VPS clock is AHEAD  of broker (rare; clock was set too late).
    delta_secs = (mt5_time - local_utc).total_seconds()
    abs_delta  = abs(delta_secs)
    delta_sign = "+" if delta_secs >= 0 else "-"
    delta_str  = f"{delta_sign}{abs_delta:.1f}s"

    if abs_delta < _TIME_WARN_THRESHOLD:
        sync_status = "OK — in sync"
    elif abs_delta < _TIME_CRITICAL_THRESHOLD:
        sync_status = f"WARNING — {abs_delta:.0f}s drift"
    else:
        sync_status = f"CRITICAL — {abs_delta:.0f}s drift"

    log.warning(
        "\n"
        "  ┌─────────────────────────────────────────────┐\n"
        "  │           TIME SYNC VALIDATION               │\n"
        "  ├─────────────────────────────────────────────┤\n"
        "  │  PC local time  : %-27s│\n"
        "  │  PC UTC time    : %-27s│\n"
        "  │  MT5 time (UTC) : %-27s│\n"
        "  │  Broker drift   : %-27s│\n"
        "  │  Source         : %-27s│\n"
        "  │  NTP peers      : %-27s│\n"
        "  │  Status         : %-27s│\n"
        "  └─────────────────────────────────────────────┘",
        local_wall.strftime("%Y-%m-%d %H:%M:%S")
            + f" UTC{tz_sign}{tz_hours:02d}:{tz_mins:02d}",
        local_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        mt5_time.strftime("%Y-%m-%d %H:%M:%S UTC"),
        delta_str,
        source,
        "pool.ntp.org / time.google.com / time.nist.gov",
        sync_status + (" — trading uses MT5 time" if fresh else " — using local fallback"),
    )

    # Emit actionable tags for log monitoring / alerting
    if fresh and abs_delta >= _TIME_CRITICAL_THRESHOLD:
        log.critical(
            "[TIME CRITICAL] VPS clock drift %.0fs exceeds %ds threshold — "
            "trading safety risk. Run: w32tm /resync /force",
            abs_delta, _TIME_CRITICAL_THRESHOLD,
        )
    elif fresh and abs_delta >= _TIME_WARN_THRESHOLD:
        log.warning(
            "[TIME WARNING] VPS clock drift %.0fs detected — "
            "NTP resync recommended. Run: w32tm /resync /force",
            abs_delta,
        )

    return abs_delta if fresh else 0.0


def check_time_drift(symbol: str = "EURUSD.Z") -> float:
    """
    Lightweight periodic VPS↔MT5 clock drift check.

    Called every N scan cycles from the main trading loop.  Emits
    [TIME WARNING] / [TIME CRITICAL] tags without printing the full banner.
    Returns abs drift in seconds, or 0.0 when MT5 feed is unavailable.

    Thresholds (same as log_time_sync_banner):
        >= _TIME_WARN_THRESHOLD    (30s)  → [TIME WARNING]
        >= _TIME_CRITICAL_THRESHOLD(120s) → [TIME CRITICAL]
    """
    local_utc = _dt.datetime.now(_dt.timezone.utc)
    mt5_time  = get_mt5_time(symbol)

    if not is_mt5_time_fresh():
        return 0.0   # MT5 feed unavailable — cannot measure drift reliably

    drift = abs((mt5_time - local_utc).total_seconds())

    if drift >= _TIME_CRITICAL_THRESHOLD:
        log.critical(
            "[TIME CRITICAL] Periodic drift check: VPS clock %.0fs out of sync with MT5. "
            "Fix: w32tm /resync /force. Trading continues on MT5 broker time.",
            drift,
        )
    elif drift >= _TIME_WARN_THRESHOLD:
        log.warning(
            "[TIME WARNING] Periodic drift check: VPS clock %.0fs drift detected. "
            "NTP should self-correct within the next poll interval.",
            drift,
        )
    else:
        log.debug("[TIME SYNC] Periodic drift check: %.1fs — OK", drift)

    return drift


def get_mt5_time(symbol: str = "EURUSD.Z") -> _dt.datetime:
    """
    Return the current MT5 broker server time as a UTC-aware datetime.

    Source: mt5.symbol_info_tick(symbol).time — Unix epoch from the broker feed.
    Falls back to local UTC time when MT5 is unavailable, not connected, or when
    the last tick is stale (> _MAX_TICK_AGE_SECONDS old — market closed / feed frozen).

    Check is_mt5_time_fresh() after calling to know which source was used.
    """
    global _mt5_time_fresh
    if _MT5_AVAILABLE:
        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick is not None and tick.time > 0:
                server_dt = _dt.datetime.fromtimestamp(tick.time, tz=_dt.timezone.utc)
                age_secs  = (_dt.datetime.now(_dt.timezone.utc) - server_dt).total_seconds()
                if age_secs <= _MAX_TICK_AGE_SECONDS:
                    log.debug("[TIME] MT5 Server Time: %s", server_dt.strftime("%Y-%m-%d %H:%M:%S"))
                    _mt5_time_fresh = True
                    return server_dt
                log.warning("[TIME] MT5 tick stale (%.0fs old) — using local UTC", age_secs)
        except Exception:
            pass
    _mt5_time_fresh = False
    return _dt.datetime.now(_dt.timezone.utc)


# ── Filling-mode helpers ──────────────────────────────────────────────────────

# ORDER_FILLING_FOK=0  ORDER_FILLING_IOC=1  ORDER_FILLING_RETURN=2  (stable MT5 values)
_FILL_MODE_NAMES: Dict[int, str] = {0: "FOK", 1: "IOC", 2: "RETURN"}
_FILL_FALLBACK_MODES = [1, 0, 2]   # IOC → FOK → RETURN
_INVALID_FILL_RETCODE = 10030       # TRADE_RETCODE_INVALID_FILL


def _filling_from_bitmask(bitmask: int) -> int:
    """
    Convert symbol_info.filling_mode bitmask to an ORDER_FILLING_* integer.
      bit 0 (value 1) = FOK supported → ORDER_FILLING_FOK = 0
      bit 1 (value 2) = IOC supported → ORDER_FILLING_IOC = 1
      RETURN (2) is always the last-resort fallback.
    """
    if bitmask & 1:
        return 0   # FOK
    if bitmask & 2:
        return 1   # IOC
    return 2       # RETURN


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class OrderResult:
    success:        bool
    ticket:         int
    executed_price: float
    executed_volume: float
    stop_loss:      float
    take_profit:    float
    retcode:        int
    comment:        str
    error:          str = ""

    @classmethod
    def failed(cls, retcode: int, error: str) -> "OrderResult":
        return cls(
            success=False, ticket=0, executed_price=0.0, executed_volume=0.0,
            stop_loss=0.0, take_profit=0.0, retcode=retcode, comment="", error=error,
        )


@dataclass
class SymbolValidation:
    """Structured result from validate_for_trading()."""
    symbol:      str
    valid:       bool
    reason:      str       # "" on pass; human-readable failure reason
    trade_mode:  int       # raw MT5 trade_mode integer (4=FULL, 0=DISABLED, etc.)
    visible:     bool      # symbol visible in Market Watch
    market_open: bool      # tick fresh and prices valid
    spread_pips: float     # live spread in pips (0.0 in dry-run / sim)

    @classmethod
    def ok(cls, symbol: str, trade_mode: int, spread: float) -> "SymbolValidation":
        return cls(
            symbol=symbol, valid=True, reason="",
            trade_mode=trade_mode, visible=True,
            market_open=True, spread_pips=spread,
        )

    @classmethod
    def fail(cls, symbol: str, reason: str, trade_mode: int = -1) -> "SymbolValidation":
        return cls(
            symbol=symbol, valid=False, reason=reason,
            trade_mode=trade_mode, visible=False,
            market_open=False, spread_pips=0.0,
        )


# ── MT5Bridge ─────────────────────────────────────────────────────────────────

class MT5Bridge:
    """
    Synchronous MT5 API wrapper with retry logic.

    Designed to be instantiated once and shared across the application.
    Call connect() at startup and disconnect() at shutdown.
    """

    def __init__(
        self,
        path:        str   = "",
        account:     int   = 0,
        password:    str   = "",
        server:      str   = "",
        magic:       int   = 202500,
        deviation:   int   = 20,
        timeout:     int   = 10,
        max_retries: int   = 5,
        retry_delay: float = 2.0,
        dry_run:     bool  = True,
    ) -> None:
        self.path        = path
        self.account     = account
        self.password    = password
        self.server      = server
        self.magic       = magic
        self.deviation   = deviation
        self.timeout     = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.dry_run     = dry_run
        self._connected  = False
        self._sim_ticket = 90000   # counter for dry-run tickets
        # Timed cooldown per symbol after receiving retcode=10018 (market closed).
        self._closed_until: Dict[str, float] = {}
        # Timed cooldown per symbol when trade_mode=DISABLED and symbol_select retry fails.
        # Prevents hammering the broker with repeated validation attempts on a dead symbol.
        # Cleared on next successful validation.
        self._disabled_until: Dict[str, float] = {}

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """
        Initialise MT5 terminal and log in to the account.
        Retries up to max_retries times with exponential back-off.
        Returns True on success.
        """
        if not _MT5_AVAILABLE:
            log.warning("MT5 not available — running in simulation mode")
            self._connected = True
            return True

        for attempt in range(1, self.max_retries + 1):
            try:
                kwargs: Dict[str, Any] = {}
                if self.path:
                    kwargs["path"] = self.path
                if self.account:
                    kwargs["login"]    = self.account
                    kwargs["password"] = self.password
                    kwargs["server"]   = self.server

                if not mt5.initialize(**kwargs):
                    err = mt5.last_error()
                    log.warning("MT5 init failed attempt %d/%d: %s", attempt, self.max_retries, err)
                else:
                    info = mt5.account_info()
                    if info is None:
                        log.warning("MT5 account_info() returned None on attempt %d", attempt)
                    else:
                        self._connected = True
                        log.info(
                            "MT5 connected | account=%d server=%s balance=%.2f %s",
                            info.login, info.server, info.balance, info.currency,
                        )
                        return True
            except Exception as exc:
                log.error("MT5 connect exception attempt %d: %s", attempt, exc)

            delay = self.retry_delay * (2 ** (attempt - 1))
            log.info("Retrying MT5 connect in %.1f s …", delay)
            time.sleep(delay)

        log.error("MT5 connection failed after %d attempts", self.max_retries)
        return False

    def disconnect(self) -> None:
        if _MT5_AVAILABLE and self._connected:
            mt5.shutdown()
            self._connected = False
            log.info("MT5 disconnected")

    @property
    def connected(self) -> bool:
        return self._connected

    # ── Data queries ──────────────────────────────────────────────────────────

    def get_candles_raw(self, symbol: str, timeframe: int, count: int) -> List[Dict]:
        """
        Return last `count` closed bars as a list of dicts with keys:
        time, open, high, low, close, tick_volume.
        """
        if not _MT5_AVAILABLE or not self._connected:
            return self._simulate_candles(symbol, count)

        self._ensure_symbol(symbol)
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count + 1)
        if rates is None or len(rates) == 0:
            log.warning("get_candles_raw: no data for %s tf=%d", symbol, timeframe)
            return []
        # Drop the last (potentially incomplete) bar
        return [
            {
                "time":        int(r["time"]),
                "open":        float(r["open"]),
                "high":        float(r["high"]),
                "low":         float(r["low"]),
                "close":       float(r["close"]),
                "tick_volume": int(r["tick_volume"]),
            }
            for r in rates[:-1]
        ]

    def get_account_info_raw(self) -> Any:
        """Return the raw mt5.AccountInfo named tuple (or simulation object)."""
        if not _MT5_AVAILABLE or not self._connected:
            return _SimAccountInfo()
        info = mt5.account_info()
        if info is None:
            raise RuntimeError(f"MT5 account_info() returned None: {mt5.last_error()}")
        return info

    def get_tick_raw(self, symbol: str) -> Tuple[float, float]:
        """Return (bid, ask) for symbol."""
        if not _MT5_AVAILABLE or not self._connected:
            return 1.1000, 1.1001
        self._ensure_symbol(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"No tick data for {symbol}: {mt5.last_error()}")
        return tick.bid, tick.ask

    def is_market_open(self, symbol: str, direction: str = "") -> tuple[bool, str]:
        """
        Return (True, "") when the market is open and accepts new orders.
        Return (False, reason) when it is not safe to submit an order.

        Three-layer check (each catches a different failure mode):
          1. trade_mode — broker-side suspension, close-only windows, directional
                          restrictions (LONGONLY / SHORTONLY).
          2. ask/bid > 0 — basic price sanity; catches null-tick states.
          3. tick.time freshness (≤60 s) — detects daily breaks and weekend
                          closure where MT5 returns the last cached tick with
                          valid prices but trade_mode may not yet reflect "closed".

        In DRY_RUN or simulation mode, always returns (True, "") so strategy
        testing is never blocked by market-hours logic.
        """
        if not _MT5_AVAILABLE or not self._connected or self.dry_run:
            return True, ""

        # ── Layer 0: cooldown from a previous 10018 retcode ──────────────────
        # This is the most reliable signal: MT5 itself told us the market was
        # closed.  Broker metadata (trade_mode, tick freshness) can lie — MT5's
        # own rejection cannot.
        _blocked_until = self._closed_until.get(symbol, 0.0)
        if _blocked_until > time.time():
            _remaining = int(_blocked_until - time.time())
            return False, f"market_known_closed(retry_in_{_remaining}s)"
        elif symbol in self._closed_until:
            del self._closed_until[symbol]   # cooldown expired — clean up

        self._ensure_symbol(symbol)

        # ── Layer 1: broker trade_mode ────────────────────────────────────────
        info = mt5.symbol_info(symbol)
        if info is None:
            return False, "symbol_info_none"

        # SYMBOL_TRADE_MODE_FULL = 4  (all operations allowed)
        # SYMBOL_TRADE_MODE_CLOSEONLY = 3  (only closing existing positions)
        # SYMBOL_TRADE_MODE_LONGONLY  = 1  (only BUY orders)
        # SYMBOL_TRADE_MODE_SHORTONLY = 2  (only SELL orders)
        # SYMBOL_TRADE_MODE_DISABLED  = 0  (no trading at all)
        mode = info.trade_mode
        if mode == 0:
            return False, f"trade_mode=DISABLED({mode})"
        if mode == 3:
            return False, f"trade_mode=CLOSEONLY({mode})"
        if mode == 1 and direction.upper() == "SELL":
            return False, f"trade_mode=LONGONLY({mode})_direction=SELL"
        if mode == 2 and direction.upper() == "BUY":
            return False, f"trade_mode=SHORTONLY({mode})_direction=BUY"

        # ── Layer 2 + 3: live tick with fresh timestamp ───────────────────────
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return False, "tick_none"
        if tick.ask <= 0 or tick.bid <= 0:
            return False, f"tick_invalid(ask={tick.ask} bid={tick.bid})"

        tick_age = time.time() - tick.time
        if tick_age > 60:
            return False, f"tick_stale({tick_age:.0f}s_old)"

        return True, ""

    def validate_for_trading(
        self,
        symbol:   str,
        direction: str   = "",
        pip_size:  float = 0.0001,
    ) -> "SymbolValidation":
        """
        Full pre-trade symbol validation with auto-recovery.

        Checks (in order):
          1. Disabled-symbol cooldown — skip if still in penalty window.
          2. symbol_select(True) — ensure symbol is visible in Market Watch.
          3. trade_mode — if DISABLED(0), retry symbol_select once, then arm cooldown.
          4. Directional mode restrictions (LONGONLY / SHORTONLY).
          5. Tick freshness and bid/ask sanity.
          6. Spread calculation.

        Logs [MT5 VALIDATION] block on every call.
        Returns SymbolValidation.valid=True only when all checks pass.
        """
        if not _MT5_AVAILABLE or not self._connected or self.dry_run:
            return SymbolValidation.ok(symbol, trade_mode=4, spread=0.0)

        # ── Disabled-symbol cooldown ──────────────────────────────────────────
        _blocked = self._disabled_until.get(symbol, 0.0)
        if _blocked > time.time():
            _remaining = int(_blocked - time.time())
            reason = f"symbol_disabled_cooldown(retry_in_{_remaining}s)"
            log.warning(
                "[MT5 VALIDATION] symbol=%-8s  status=BLOCKED  reason=%s",
                symbol, reason,
            )
            return SymbolValidation.fail(symbol, reason, trade_mode=0)
        elif symbol in self._disabled_until:
            del self._disabled_until[symbol]   # cooldown expired

        # ── Step 1: ensure symbol is visible in Market Watch ─────────────────
        _visible = False
        try:
            _visible = mt5.symbol_select(symbol, True)
        except Exception as exc:
            log.warning("[MT5 VALIDATION] symbol_select(%s) raised: %s", symbol, exc)

        if not _visible:
            reason = "symbol_select_failed"
            log.warning(
                "[MT5 VALIDATION] symbol=%-8s  visible=False  status=FAIL  reason=%s",
                symbol, reason,
            )
            return SymbolValidation.fail(symbol, reason)

        # ── Step 2: symbol_info and trade_mode ───────────────────────────────
        info = mt5.symbol_info(symbol)
        if info is None:
            reason = "symbol_info_none"
            log.warning(
                "[MT5 VALIDATION] symbol=%-8s  visible=True  trade_mode=?  status=FAIL  reason=%s",
                symbol, reason,
            )
            return SymbolValidation.fail(symbol, reason)

        trade_mode = info.trade_mode

        # trade_mode=DISABLED(0): attempt auto-recovery via symbol_select, then cooldown.
        if trade_mode == 0:
            log.warning(
                "[MT5 VALIDATION] symbol=%-8s  trade_mode=DISABLED(0) — attempting symbol_select recovery",
                symbol,
            )
            try:
                mt5.symbol_select(symbol, True)
            except Exception:
                pass
            info2 = mt5.symbol_info(symbol)
            trade_mode = info2.trade_mode if info2 is not None else 0
            if trade_mode == 0:
                # Still disabled — arm 30-minute cooldown so we don't hammer every cycle.
                self._disabled_until[symbol] = time.time() + 1800
                reason = f"trade_mode=DISABLED(0)_recovery_failed(cooldown=1800s)"
                log.warning(
                    "[MT5 VALIDATION] symbol=%-8s  trade_mode=DISABLED  status=FAIL"
                    "  reason=%s  cooldown=1800s",
                    symbol, reason,
                )
                return SymbolValidation.fail(symbol, reason, trade_mode=0)
            log.warning(
                "[MT5 VALIDATION] symbol=%-8s  recovery=OK  trade_mode=%d", symbol, trade_mode,
            )

        # Directional trade_mode restrictions
        if trade_mode == 3:
            reason = f"trade_mode=CLOSEONLY({trade_mode})"
            log.warning(
                "[MT5 VALIDATION] symbol=%-8s  trade_mode=%d  status=FAIL  reason=%s",
                symbol, trade_mode, reason,
            )
            return SymbolValidation.fail(symbol, reason, trade_mode=trade_mode)
        if trade_mode == 1 and direction.upper() == "SELL":
            reason = f"trade_mode=LONGONLY({trade_mode})_direction=SELL"
            log.warning(
                "[MT5 VALIDATION] symbol=%-8s  trade_mode=%d  direction=%s  status=FAIL  reason=%s",
                symbol, trade_mode, direction, reason,
            )
            return SymbolValidation.fail(symbol, reason, trade_mode=trade_mode)
        if trade_mode == 2 and direction.upper() == "BUY":
            reason = f"trade_mode=SHORTONLY({trade_mode})_direction=BUY"
            log.warning(
                "[MT5 VALIDATION] symbol=%-8s  trade_mode=%d  direction=%s  status=FAIL  reason=%s",
                symbol, trade_mode, direction, reason,
            )
            return SymbolValidation.fail(symbol, reason, trade_mode=trade_mode)

        # ── Step 3: live tick ────────────────────────────────────────────────
        tick = mt5.symbol_info_tick(symbol)
        if tick is None or tick.ask <= 0 or tick.bid <= 0:
            reason = "tick_invalid_or_none"
            log.warning(
                "[MT5 VALIDATION] symbol=%-8s  trade_mode=%d  tick=invalid  status=FAIL",
                symbol, trade_mode,
            )
            return SymbolValidation.fail(symbol, reason, trade_mode=trade_mode)

        tick_age = time.time() - tick.time
        if tick_age > 60:
            reason = f"tick_stale({tick_age:.0f}s)"
            log.warning(
                "[MT5 VALIDATION] symbol=%-8s  trade_mode=%d  tick_age=%.0fs  status=FAIL",
                symbol, trade_mode, tick_age,
            )
            return SymbolValidation.fail(symbol, reason, trade_mode=trade_mode)

        # ── Step 4: spread ───────────────────────────────────────────────────
        spread_pips = round((tick.ask - tick.bid) / pip_size, 1) if pip_size > 0 else 0.0

        # ── All checks passed ────────────────────────────────────────────────
        log.warning(
            "[MT5 VALIDATION] symbol=%-8s  visible=True  trade_mode=%d  "
            "market_open=True  spread=%.1f  status=PASS",
            symbol, trade_mode, spread_pips,
        )
        result = SymbolValidation.ok(symbol, trade_mode=trade_mode, spread=spread_pips)
        result.visible = True
        return result

    def get_symbol_info(self, symbol: str) -> Dict[str, Any]:
        """Return symbol metadata (point, digits, trade_contract_size, etc.).

        Lookup order in simulation mode:
            1. Exact match  (e.g. "EURUSD.Z")
            2. Base symbol  (e.g. "EURUSD" — for no-suffix brokers)
            3. DEFAULT      (safe fallback for unsupported symbols)
        """
        if not _MT5_AVAILABLE or not self._connected:
            sym_up = symbol.upper()
            if sym_up in _SIM_SYMBOL_INFO:
                return _SIM_SYMBOL_INFO[sym_up]
            # Try stripping broker suffix for backward compat
            from aurex_ai.core.symbol_mapper import strip_suffix
            base_up = strip_suffix(sym_up).upper()
            return _SIM_SYMBOL_INFO.get(base_up, _SIM_SYMBOL_INFO["DEFAULT"])
        self._ensure_symbol(symbol)
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"symbol_info({symbol}) returned None")
        return {
            "point":               info.point,
            "digits":              info.digits,
            "trade_contract_size": info.trade_contract_size,
            "volume_min":          info.volume_min,
            "volume_max":          info.volume_max,
            "volume_step":         info.volume_step,
            "trade_tick_value":    info.trade_tick_value,
            "trade_tick_size":     info.trade_tick_size,
        }

    def get_open_positions(self, symbol: Optional[str] = None) -> List[Dict]:
        if not _MT5_AVAILABLE or not self._connected:
            return []
        positions = (
            mt5.positions_get(symbol=symbol) if symbol
            else mt5.positions_get()
        )
        if positions is None:
            return []
        return [
            {
                "ticket":   p.ticket,
                "symbol":   p.symbol,
                "type":     "BUY" if p.type == 0 else "SELL",
                "volume":   p.volume,
                "price_open": p.price_open,
                "sl":       p.sl,
                "tp":       p.tp,
                "profit":   p.profit,
                "magic":    p.magic,
            }
            for p in positions
            if p.magic == self.magic
        ]

    # ── Order execution ───────────────────────────────────────────────────────

    def execute_order(
        self,
        symbol:      str,
        direction:   str,    # "BUY" | "SELL"
        lot_size:    float,
        stop_loss:   float,
        take_profit: float,
        comment:     str = "Aurex-Sig",
    ) -> OrderResult:
        """
        Send a market order to MT5.

        DRY_RUN mode: logs the order and returns a synthetic success result.
        LIVE mode:    sends via mt5.order_send().
          - Uses broker-supported filling mode from symbol_info.filling_mode.
          - On retcode=10030 (INVALID_FILL), retries with IOC -> FOK -> RETURN
            until one succeeds or all are exhausted.
          - On transient errors (requote, timeout, price-off), retries up to
            max_retries within the same filling mode.
        """
        if self.dry_run or not _MT5_AVAILABLE or not self._connected:
            return self._dry_run_order(symbol, direction, lot_size, stop_loss, take_profit)

        # ── Hard block: market must be open before calling mt5.order_send() ──
        # This is the final defence. Upper layers (scan_symbol, fallback, stack
        # pipelines) should catch closed markets first, but broker metadata can
        # return false-positives.  This guarantee means mt5.order_send() is
        # NEVER reached when the market is known to be closed.
        _guard_open, _guard_reason = self.is_market_open(symbol, direction)
        if not _guard_open:
            log.error(
                "[HARD BLOCK] %s %s — market closed, aborting send | reason=%s",
                symbol, direction, _guard_reason,
            )
            return OrderResult.failed(10018, f"hard_block:{_guard_reason}")

        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        bid, ask   = self.get_tick_raw(symbol)
        price      = ask if direction == "BUY" else bid

        sym_info     = mt5.symbol_info(symbol)
        default_fill = _filling_from_bitmask(sym_info.filling_mode if sym_info else 0)
        fill_sequence = [default_fill] + [m for m in _FILL_FALLBACK_MODES if m != default_fill]

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       symbol,
            "volume":       round(lot_size, 2),
            "type":         order_type,
            "price":        price,
            "sl":           round(stop_loss, 5),
            "tp":           round(take_profit, 5),
            "deviation":    self.deviation,
            "magic":        self.magic,
            "comment":      comment,
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": default_fill,
        }

        _retriable = {
            mt5.TRADE_RETCODE_REQUOTE,
            mt5.TRADE_RETCODE_PRICE_CHANGED,
            mt5.TRADE_RETCODE_PRICE_OFF,
            mt5.TRADE_RETCODE_TIMEOUT,
        }

        for fill_mode in fill_sequence:
            request["type_filling"] = fill_mode
            fill_name = _FILL_MODE_NAMES.get(fill_mode, str(fill_mode))

            for attempt in range(1, self.max_retries + 1):
                if attempt > 1:
                    bid, ask = self.get_tick_raw(symbol)
                    request["price"] = ask if direction == "BUY" else bid

                result = mt5.order_send(request)

                if result is None:
                    log.error(
                        "order_send returned None attempt %d/%d fill=%s: %s",
                        attempt, self.max_retries, fill_name, mt5.last_error(),
                    )
                    time.sleep(self.retry_delay)
                    continue

                if result.retcode == mt5.TRADE_RETCODE_DONE:
                    log.warning(
                        "LIVE TRADE SENT | %s %s | lot=%.2f sl=%.5f tp=%.5f "
                        "ticket=%d price=%.5f filling_mode=%s",
                        symbol, direction, result.volume,
                        stop_loss, take_profit, result.order,
                        result.price, fill_name,
                    )
                    return OrderResult(
                        success=True, ticket=result.order,
                        executed_price=result.price, executed_volume=result.volume,
                        stop_loss=stop_loss, take_profit=take_profit,
                        retcode=result.retcode, comment=result.comment,
                    )

                if result.retcode == _INVALID_FILL_RETCODE:
                    log.warning(
                        "MT5 ORDER FAILED | %s | retcode=%d filling_mode=%s — trying fallback",
                        symbol, result.retcode, fill_name,
                    )
                    break  # try next filling mode

                # Market closed: record cooldown so future cycles are blocked
                # immediately at is_market_open() without reaching MT5 at all.
                if result.retcode == 10018:
                    self._closed_until[symbol] = time.time() + 300   # 5-min block
                    log.error(
                        "[MARKET CLOSED] %s retcode=10018 — blocking symbol for 300s",
                        symbol,
                    )
                    return OrderResult.failed(result.retcode, result.comment)

                if result.retcode in _retriable and attempt < self.max_retries:
                    log.warning(
                        "order_send retcode=%d attempt %d/%d fill=%s — retrying",
                        result.retcode, attempt, self.max_retries, fill_name,
                    )
                    time.sleep(self.retry_delay)
                    continue

                log.error(
                    "MT5 ORDER FAILED | %s | retcode=%d filling_mode=%s comment=%s",
                    symbol, result.retcode, fill_name, result.comment,
                )
                return OrderResult.failed(result.retcode, result.comment)

            else:
                log.error(
                    "MT5 ORDER FAILED | %s | max retries exceeded filling_mode=%s",
                    symbol, fill_name,
                )
                return OrderResult.failed(-1, "max_retries_exceeded")

        log.error("MT5 ORDER FAILED | %s | retcode=10030 filling_mode=all_tried", symbol)
        return OrderResult.failed(_INVALID_FILL_RETCODE, "invalid_fill_all_modes")

    def get_closed_deals(self, tickets: set, lookback_days: int = 30) -> List[Dict]:
        """
        Return one aggregated deal dict per position ticket.

        Multiple OUT deals for the same position_id (partial close + full close)
        are collapsed into one record with profits summed and volumes summed.
        Falls back to empty list on simulation or if MT5 unavailable.
        """
        if not _MT5_AVAILABLE or not self._connected or not tickets:
            return []
        now       = get_mt5_time()
        date_from = now - _dt.timedelta(days=lookback_days)
        date_to   = now + _dt.timedelta(hours=1)
        deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            return []
        _OUT = 1   # mt5.DEAL_ENTRY_OUT

        # Group by position_id and sum all OUT deal profits (partial + full close)
        grouped: Dict[int, Dict] = {}
        for d in deals:
            if d.position_id not in tickets:
                continue
            if d.magic != self.magic:
                continue
            if d.entry != _OUT:
                continue
            pos_id = d.position_id
            if pos_id not in grouped:
                grouped[pos_id] = {
                    "ticket":    pos_id,
                    "symbol":    d.symbol,
                    "profit":    0.0,
                    "direction": "BUY" if d.type == 0 else "SELL",
                    "volume":    0.0,
                    "magic":     d.magic,
                }
            grouped[pos_id]["profit"] += d.profit
            grouped[pos_id]["volume"] += d.volume

        return list(grouped.values())

    def modify_sl(self, ticket: int, new_sl: float, new_tp: Optional[float] = None) -> bool:
        """Move SL (and optionally TP) on an open position.  Returns True on success."""
        if self.dry_run or not _MT5_AVAILABLE or not self._connected:
            log.info("DRY_RUN modify_sl ticket=%d new_sl=%.5f", ticket, new_sl)
            return True
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            log.warning("modify_sl: ticket %d not found", ticket)
            return False
        pos = positions[0]
        tp  = round(new_tp, 5) if new_tp is not None else pos.tp
        req = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "symbol":   pos.symbol,
            "position": ticket,
            "sl":       round(new_sl, 5),
            "tp":       tp,
        }
        result = mt5.order_send(req)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            return True
        log.error("modify_sl failed | ticket=%d retcode=%s",
                  ticket, getattr(result, "retcode", "None"))
        return False

    def partial_close(self, ticket: int, volume: float) -> bool:
        """Close a partial volume of an open position.  Returns True on success."""
        if self.dry_run or not _MT5_AVAILABLE or not self._connected:
            log.info("DRY_RUN partial_close ticket=%d volume=%.2f", ticket, volume)
            return True
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            log.warning("partial_close: ticket %d not found", ticket)
            return False
        pos        = positions[0]
        close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
        bid, ask   = self.get_tick_raw(pos.symbol)
        price      = bid if pos.type == 0 else ask

        sym_info     = mt5.symbol_info(pos.symbol)
        default_fill = _filling_from_bitmask(sym_info.filling_mode if sym_info else 0)
        fill_sequence = [default_fill] + [m for m in _FILL_FALLBACK_MODES if m != default_fill]

        req = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       pos.symbol,
            "volume":       round(volume, 2),
            "type":         close_type,
            "position":     ticket,
            "price":        price,
            "deviation":    self.deviation,
            "magic":        self.magic,
            "comment":      "Aurex-PartialTP",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": default_fill,
        }
        for fill_mode in fill_sequence:
            req["type_filling"] = fill_mode
            fill_name = _FILL_MODE_NAMES.get(fill_mode, str(fill_mode))
            result = mt5.order_send(req)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info("partial_close done | ticket=%d volume=%.2f fill=%s", ticket, volume, fill_name)
                return True
            if result and result.retcode == _INVALID_FILL_RETCODE:
                log.debug("partial_close fill=%s rejected, trying fallback", fill_name)
                continue
            log.error("partial_close failed | ticket=%d retcode=%s fill=%s",
                      ticket, getattr(result, "retcode", "None"), fill_name)
            return False
        log.error("partial_close failed | ticket=%d all fill modes rejected", ticket)
        return False

    def close_position(self, ticket: int) -> bool:
        """Market-close a position by ticket.  Returns True on success."""
        if self.dry_run or not _MT5_AVAILABLE or not self._connected:
            log.info("DRY_RUN close_position ticket=%d", ticket)
            return True
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            log.warning("close_position: ticket %d not found", ticket)
            return False
        pos = positions[0]
        close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
        bid, ask = self.get_tick_raw(pos.symbol)
        price = bid if pos.type == 0 else ask
        req = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       pos.symbol,
            "volume":       pos.volume,
            "type":         close_type,
            "position":     ticket,
            "price":        price,
            "deviation":    self.deviation,
            "magic":        self.magic,
            "comment":      "Aurex-close",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(req)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info("position closed | ticket=%d", ticket)
            return True
        log.error("close_position failed | ticket=%d retcode=%s",
                  ticket, getattr(result, "retcode", "None"))
        return False

    # ── Internals ─────────────────────────────────────────────────────────────

    def _ensure_symbol(self, symbol: str) -> None:
        if not _MT5_AVAILABLE:
            return
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"symbol_select({symbol}) failed: {mt5.last_error()}")

    def _dry_run_order(
        self,
        symbol:     str,
        direction:  str,
        lot_size:   float,
        stop_loss:  float,
        take_profit: float,
    ) -> OrderResult:
        self._sim_ticket += 1
        log.warning(
            "DRY_RUN ORDER | %s %s | lots=%.2f sl=%.5f tp=%.5f ticket=%d",
            symbol, direction, lot_size, stop_loss, take_profit, self._sim_ticket,
        )
        return OrderResult(
            success=True, ticket=self._sim_ticket,
            executed_price=0.0,   # filled at market on next tick
            executed_volume=lot_size,
            stop_loss=stop_loss, take_profit=take_profit,
            retcode=10009, comment="dry_run",
        )

    @staticmethod
    def _simulate_candles(symbol: str, count: int) -> List[Dict]:
        """Return synthetic candles for use when MT5 is unavailable."""
        candles = []
        price  = 1.1000
        t      = int(time.time()) - count * 900
        for _ in range(count):
            o = price
            h = o + random.uniform(0.0001, 0.0020)
            l = o - random.uniform(0.0001, 0.0020)
            c = random.uniform(l, h)
            candles.append({"time": t, "open": o, "high": h, "low": l, "close": c, "tick_volume": 100})
            price = c
            t    += 900
        return candles


# ── Simulation helpers ────────────────────────────────────────────────────────

class _SimAccountInfo:
    login      = 0
    server     = "simulation"
    balance    = 100_000.0
    equity     = 100_000.0
    margin     = 0.0
    margin_free= 100_000.0
    currency   = "ZAR"   # matches live account currency; used only when MT5 is unavailable
    leverage   = 100


# Simulation symbol specs — keyed by HFM Premium broker names (.Z suffix).
# get_symbol_info() falls back through: exact match → base match → DEFAULT.
# This makes the bridge work correctly for both suffixed and bare-symbol brokers.
_SIM_SYMBOL_INFO: Dict[str, Dict] = {
    "DEFAULT": {
        "point": 0.00001, "digits": 5, "trade_contract_size": 100_000,
        "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
        "trade_tick_value": 1.0, "trade_tick_size": 0.00001,
    },
    # ── HFM Premium (.Z suffix) ───────────────────────────────
    "EURUSD.Z": {
        "point": 0.00001, "digits": 5, "trade_contract_size": 100_000,
        "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
        "trade_tick_value": 1.0, "trade_tick_size": 0.00001,
    },
    "GBPUSD.Z": {
        "point": 0.00001, "digits": 5, "trade_contract_size": 100_000,
        "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
        "trade_tick_value": 1.0, "trade_tick_size": 0.00001,
    },
    "USDJPY.Z": {
        "point": 0.001,   "digits": 3, "trade_contract_size": 100_000,
        "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
        "trade_tick_value": 0.91, "trade_tick_size": 0.001,
    },
    # ── Bare symbols (no-suffix brokers / backward compatibility) ─
    "EURUSD": {
        "point": 0.00001, "digits": 5, "trade_contract_size": 100_000,
        "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
        "trade_tick_value": 1.0, "trade_tick_size": 0.00001,
    },
    "GBPUSD": {
        "point": 0.00001, "digits": 5, "trade_contract_size": 100_000,
        "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
        "trade_tick_value": 1.0, "trade_tick_size": 0.00001,
    },
    "USDJPY": {
        "point": 0.001,   "digits": 3, "trade_contract_size": 100_000,
        "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
        "trade_tick_value": 0.91, "trade_tick_size": 0.001,
    },
}
