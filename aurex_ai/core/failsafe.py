"""
Aurex AI — Failsafe & Recovery System  (Phase 6)

Production-grade protection against:

  1. Stale market data
       Detects when the same candle (same open time) is returned N times in
       a row for a symbol.  Indicates a frozen MT5 feed or dead broker connection.
       Action: log [FAILSAFE], skip execution for that symbol.

  2. Duplicate trade prevention
       Tracks ticket IDs submitted in the last N seconds.  Any attempt to
       submit the same ticket again is blocked with [DUPLICATE PREVENTED].
       Also detects rapid re-execution of the same symbol+direction within
       the duplicate_window_sec window.

  3. Execution lock
       Per-symbol mutex preventing two async tasks from executing the same
       symbol simultaneously.  Released with a timeout guard so locks never
       become permanent on exceptions.

  4. MT5 recovery detection
       Tracks consecutive MT5 errors.  After max_reconnect_attempts failures,
       emits [RECOVERY ACTIVE] and signals the caller to attempt reconnection.

All state is in-memory.  Thread-safe via threading.Lock.

Logs emitted
------------
    [FAILSAFE]           — stale data or lock violation detected
    [RECOVERY ACTIVE]    — MT5 reconnect threshold reached
    [DUPLICATE PREVENTED]— duplicate ticket or rapid re-signal blocked
    [MT5 RECOVERED]      — successful reconnection after failures
"""
from __future__ import annotations

import threading
import time
from enum import IntEnum
from typing import Dict, Optional, Tuple

from aurex_ai.core.logger import get_logger

log = get_logger("core.failsafe")


class StalenessLevel(IntEnum):
    """Graded staleness: FRESH → WARNING → DEGRADED → BLOCKED."""
    FRESH    = 0   # data is fresh; normal execution
    WARNING  = 1   # first detection; warn only, allow through
    DEGRADED = 2   # prolonged stale; warn + 0.5× size reduction
    BLOCKED  = 3   # true feed freeze; hard block execution


# Timeframe-aware stale thresholds (minutes).
# M15 candles persist 15 min naturally — don't flag until >20 min stale.
_TF_STALE_THRESHOLDS: Dict[int, Tuple[int, int, int]] = {
    15:  (20,  35,  50),    # M15: warn @20m  degrade @35m  block @50m
    60:  (75,  100, 130),   # H1:  warn @75m  degrade @100m block @130m
    240: (300, 360, 480),   # H4:  warn @5h   degrade @6h   block @8h
}


class StaleCandleDetector:
    """
    Time-based stale candle detection.

    Tracks wall-clock age of the current candle per (symbol, tf).
    Returns a StalenessLevel rather than a plain bool, enabling graded
    responses (warn → reduce size → hard block) instead of an immediate block.

    Rationale: M15 candles live for 15 minutes.  A count-based detector
    that fires after 3 identical readings (≈45 seconds) produces constant
    false positives on normal 15-second scan cycles.
    """

    def __init__(self, threshold: int = 3) -> None:
        # `threshold` kept for API compatibility; not used in time-based logic.
        self._lock  = threading.Lock()
        # (symbol, tf) → {open_time, first_seen, stale_hits}
        self._state: Dict[Tuple[str, int], dict] = {}

    def _thresholds(self, tf: int) -> Tuple[int, int, int]:
        if tf in _TF_STALE_THRESHOLDS:
            return _TF_STALE_THRESHOLDS[tf]
        # Unknown TF: 1.5×, 2.5×, 4× the candle duration in minutes
        return (int(tf * 1.5), int(tf * 2.5), int(tf * 4.0))

    def check(self, symbol: str, tf: int, candle_open_time: str) -> StalenessLevel:
        """
        Record candle_open_time and return the current StalenessLevel.

        FRESH    → candle is new or age is within normal timeframe duration.
        WARNING  → first stale detection; allow execution, emit [STALE CHECK].
        DEGRADED → prolonged stale; execution continues at 0.5× size.
        BLOCKED  → true feed freeze; caller must skip execution.
        """
        key = (symbol, tf)
        now = time.monotonic()
        warn_min, degrade_min, block_min = self._thresholds(tf)

        with self._lock:
            s = self._state.get(key)
            if s is None or s["open_time"] != candle_open_time:
                # New or first-seen candle — reset to fresh
                self._state[key] = {
                    "open_time":  candle_open_time,
                    "first_seen": now,
                    "stale_hits": 0,
                }
                return StalenessLevel.FRESH

            age_min = (now - s["first_seen"]) / 60.0

            if age_min >= block_min:
                s["stale_hits"] += 1
                level = StalenessLevel.BLOCKED
            elif age_min >= degrade_min:
                s["stale_hits"] += 1
                level = StalenessLevel.DEGRADED
            elif age_min >= warn_min:
                s["stale_hits"] += 1
                level = StalenessLevel.WARNING
            else:
                level = StalenessLevel.FRESH

        if level != StalenessLevel.FRESH:
            _thresh = (
                block_min   if level == StalenessLevel.BLOCKED  else
                degrade_min if level == StalenessLevel.DEGRADED else
                warn_min
            )
            log.warning(
                "[STALE CHECK] symbol=%s tf=%d age_minutes=%.1f threshold=%d status=%s",
                symbol, tf, age_min, _thresh, level.name,
            )

        return level

    def reset(self, symbol: str, tf: int) -> None:
        """Clear stale state for a symbol after data resumes."""
        key = (symbol, tf)
        with self._lock:
            self._state.pop(key, None)


class DuplicateTicketGuard:
    """
    Prevent the same trade ticket or same symbol+direction from being
    submitted twice within duplicate_window_sec seconds.
    """

    def __init__(self, window_sec: int = 30) -> None:
        self._window  = window_sec
        self._lock    = threading.Lock()
        # ticket_id → submission timestamp
        self._tickets: Dict[int, float]         = {}
        # (symbol, direction) → submission timestamp
        self._signals: Dict[Tuple[str, str], float] = {}

    def is_duplicate_ticket(self, ticket: int) -> bool:
        """Return True and log if this ticket was already submitted recently."""
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            if ticket in self._tickets:
                log.error(
                    "[DUPLICATE PREVENTED] [FAILSAFE] ticket=%d already submitted %.1fs ago",
                    ticket, now - self._tickets[ticket],
                )
                return True
        return False

    def register_ticket(self, ticket: int) -> None:
        """Record a new ticket submission."""
        with self._lock:
            self._tickets[ticket] = time.monotonic()

    def is_duplicate_signal(self, symbol: str, direction: str) -> bool:
        """Return True if this symbol+direction was already submitted within window_sec."""
        key = (symbol, direction)
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            ts = self._signals.get(key)
            if ts is not None and (now - ts) < self._window:
                log.warning(
                    "[DUPLICATE PREVENTED] [FAILSAFE] %s %s re-signal within %.0fs window",
                    symbol, direction, self._window,
                )
                return True
        return False

    def register_signal(self, symbol: str, direction: str) -> None:
        """Record that a trade was submitted for this symbol+direction."""
        key = (symbol, direction)
        with self._lock:
            self._signals[key] = time.monotonic()

    def _purge(self, now: float) -> None:
        """Expire old entries (called while holding lock)."""
        cutoff = now - self._window
        self._tickets = {k: v for k, v in self._tickets.items() if v >= cutoff}
        self._signals = {k: v for k, v in self._signals.items() if v >= cutoff}


class MT5RecoveryTracker:
    """
    Count consecutive MT5 errors and flag when reconnection is needed.
    Resets on any successful MT5 operation.
    """

    def __init__(self, max_attempts: int = 3) -> None:
        self._max      = max_attempts
        self._lock     = threading.Lock()
        self._errors   = 0
        self._in_recovery = False

    def record_error(self) -> bool:
        """
        Record an MT5 error.  Returns True when the reconnect threshold
        is reached and [RECOVERY ACTIVE] should be triggered.
        """
        with self._lock:
            self._errors += 1
            if self._errors >= self._max and not self._in_recovery:
                self._in_recovery = True
                log.warning(
                    "[RECOVERY ACTIVE] [FAILSAFE] MT5 errors=%d reached threshold=%d "
                    "— initiating reconnect",
                    self._errors, self._max,
                )
                return True
        return False

    def record_success(self) -> None:
        """Reset error counter on any successful MT5 operation."""
        with self._lock:
            if self._errors > 0 or self._in_recovery:
                if self._in_recovery:
                    log.warning(
                        "[MT5 RECOVERED] [FAILSAFE] MT5 reconnected after %d error(s)",
                        self._errors,
                    )
                self._errors      = 0
                self._in_recovery = False

    @property
    def in_recovery(self) -> bool:
        with self._lock:
            return self._in_recovery


class SymbolExecutionLock:
    """
    Per-symbol execution lock preventing concurrent order submission.
    Uses threading.Lock with a timeout to avoid permanent deadlocks.
    """

    def __init__(self, timeout_sec: float = 10.0) -> None:
        self._timeout = timeout_sec
        self._meta_lock = threading.Lock()
        self._locks: Dict[str, threading.Lock] = {}

    def _get_lock(self, symbol: str) -> threading.Lock:
        with self._meta_lock:
            if symbol not in self._locks:
                self._locks[symbol] = threading.Lock()
            return self._locks[symbol]

    def acquire(self, symbol: str) -> bool:
        """
        Try to acquire the execution lock for a symbol.
        Returns True on success, False if another execution is in progress.
        """
        lk = self._get_lock(symbol)
        acquired = lk.acquire(timeout=self._timeout)
        if not acquired:
            log.warning(
                "[FAILSAFE] Execution lock timeout for %s after %.0fs — "
                "skipping to prevent duplicate orders",
                symbol, self._timeout,
            )
        return acquired

    def release(self, symbol: str) -> None:
        """Release the execution lock for a symbol."""
        lk = self._get_lock(symbol)
        try:
            lk.release()
        except RuntimeError:
            pass   # already released — safe to ignore


class Failsafe:
    """
    Unified failsafe coordinator.  One instance lives for the session.

    Usage:
        fs = Failsafe.get_instance(cfg)

        # In scan loop:
        if fs.is_stale("XAUUSD", TF_M15, candles_m15[-1].time.isoformat()):
            continue   # skip this symbol this cycle

        if fs.is_duplicate("XAUUSD", "BUY"):
            continue

        if not fs.lock("XAUUSD"):
            continue
        try:
            result = await execute(...)
            fs.register_trade(result.ticket, "XAUUSD", "BUY")
        finally:
            fs.unlock("XAUUSD")

        fs.mt5_ok()   # call after any successful bridge call
    """

    _instance: Optional["Failsafe"] = None

    def __init__(self, cfg=None) -> None:
        fs_cfg      = getattr(cfg, "failsafe", None) if cfg else None
        threshold   = int(getattr(fs_cfg,   "stale_candle_threshold", 3))
        dup_window  = int(getattr(fs_cfg,   "duplicate_window_sec",  30))
        reconnects  = int(getattr(fs_cfg,   "mt5_reconnect_attempts",  3))
        lock_timeout= float(getattr(fs_cfg, "execution_lock_timeout", 10.0))

        self._stale    = StaleCandleDetector(threshold=threshold)
        self._dup      = DuplicateTicketGuard(window_sec=dup_window)
        self._mt5      = MT5RecoveryTracker(max_attempts=reconnects)
        self._exec_lock= SymbolExecutionLock(timeout_sec=lock_timeout)

    @classmethod
    def get_instance(cls, cfg=None) -> "Failsafe":
        if cls._instance is None:
            cls._instance = cls(cfg)
        return cls._instance

    def is_stale(self, symbol: str, tf: int, candle_open_iso: str) -> StalenessLevel:
        return self._stale.check(symbol, tf, candle_open_iso)

    def reset_stale(self, symbol: str, tf: int) -> None:
        self._stale.reset(symbol, tf)

    def is_duplicate(self, symbol: str, direction: str) -> bool:
        return self._dup.is_duplicate_signal(symbol, direction)

    def register_trade(self, ticket: int, symbol: str, direction: str) -> None:
        self._dup.register_ticket(ticket)
        self._dup.register_signal(symbol, direction)

    def lock(self, symbol: str) -> bool:
        return self._exec_lock.acquire(symbol)

    def unlock(self, symbol: str) -> None:
        self._exec_lock.release(symbol)

    def mt5_error(self) -> bool:
        """Returns True when reconnect threshold is reached."""
        return self._mt5.record_error()

    def mt5_ok(self) -> None:
        self._mt5.record_success()

    @property
    def mt5_in_recovery(self) -> bool:
        return self._mt5.in_recovery
