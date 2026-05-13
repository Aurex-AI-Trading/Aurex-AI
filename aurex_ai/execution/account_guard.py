"""
Aurex AI — Account Guard  (Phase 1: Risk Stabilization)

Hard account protection for small live accounts trading on MetaTrader 5.

Halt conditions (stop trading for the rest of the day):
  1. 3+ consecutive losses
  2. Daily equity drawdown >= 5%  (vs session-open equity)
  3. Total equity drawdown >= 12% (vs all-time peak equity)

Size reduction schedule (progressive, before halt):
  consecutive_losses == 2     →  50% lot size
  daily drawdown >= 2.5%      →  50% lot size  (50% of the 5% halt limit)
  normal                      →  100% lot size

Small account mode (balance < R2000 equivalent):
  - max 1 open trade at a time
  - max 0.01 lot on all forex pairs
  - max 0.01 lot on gold / indices

Lot multiplier hard cap:
  - No multiplier may exceed 1.5× (eliminates martingale / revenge scaling)

Persistence:
  - All state saved to JSON after every change
  - Restarts within the same trading day preserve streak and halt status
  - Daily counters reset automatically at UTC midnight
  - Peak equity persists indefinitely (session-independent high-water mark)

Logging tags:
  [RISK LOCK]              — trading is halted and this cycle is being skipped
  [RISK RESET]             — new day detected; daily counters cleared
  [CONSECUTIVE LOSS LIMIT] — halt triggered by streak
  [DAILY LOSS LIMIT]       — halt triggered by daily drawdown
  [MAX DD HIT]             — halt triggered by total drawdown from peak
  [TRADING PAUSED]         — generic halt confirmation
  [LOSS STREAK DETECTED]   — loss recorded, streak counter updated
  [COOLDOWN ACTIVE]        — 2-loss streak → size reduced to 50%
  [SAFE LOT CAP]           — lot multiplier capped at 1.5×
  [RECOVERY LIMITED]       — recovery multiplier reduced to safe level
  [RISK NORMALIZED]        — size reduced due to approaching drawdown limit
  [SMALL ACCOUNT MODE]     — balance below threshold; conservative limits applied
  [SAFE CAPITAL PROTECTION]— small account lot override applied
  [RISK STATUS]            — periodic telemetry heartbeat
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from aurex_ai.core.logger import get_logger

log = get_logger("execution.guard")

# ── Module-level defaults ──────────────────────────────────────────────────────

_CONSEC_LOSS_HALT    = 3        # halt after this many consecutive losses
_CONSEC_LOSS_REDUCE  = 2        # reduce size at this many consecutive losses
_DAILY_DD_HALT_PCT   = 5.0      # daily equity drawdown % → halt
_DAILY_DD_REDUCE_PCT = 2.5      # daily equity drawdown % → 50% size (50% of halt level)
_TOTAL_DD_HALT_PCT   = 12.0     # total drawdown from peak equity % → halt
_SMALL_ACCT_ZAR      = 2000.0   # ZAR balance threshold for small-account mode
_MAX_LOT_MULT        = 1.5      # hard cap on any lot multiplier
_STATE_FILE          = Path(__file__).resolve().parent.parent / "data" / "account_guard_state.json"

# ── Adaptive symbol thresholds ────────────────────────────────────────────────
# Balance-based symbol activation gates (Phase 13 — small account survivability).
_XAUUSD_MIN_BALANCE_ZAR    = 500.0   # Phase 14: lowered from 7000 ZAR; overridden by guard.xauusd_min_balance_zar in config
_GBPJPY_REDUCE_BALANCE_ZAR = 3000.0  # log GBPJPY reduced-allocation warning below this
_MICRO_ACCT_ZAR            = 1000.0  # micro-account tier: Tier 1 only, max 1 open trade


# ── Public data classes ───────────────────────────────────────────────────────

@dataclass
class GuardDecision:
    """Returned by check_cycle() — controls whether this scan cycle executes."""
    trading_allowed: bool    # False → skip all symbol scans this cycle
    size_mult:       float   # multiply into combined_size_mult (0.0=halt, 0.5=half, 1.0=full)
    halt_reason:     str     # non-empty when trading_allowed=False
    mode:            str     # "NORMAL" | "REDUCED" | "HALTED"


@dataclass
class SmallAccountLimits:
    """Lot/position caps enforced when balance is below the small-account threshold."""
    active:        bool
    max_open:      int     # maximum simultaneous open trades
    max_lot_forex: float   # hard lot cap for FX pairs
    max_lot_gold:  float   # hard lot cap for XAUUSD / gold
    micro_mode:    bool = False  # balance < 1000 ZAR: Tier 1 only, max 1 open


# ── Account Guard ─────────────────────────────────────────────────────────────

class AccountGuard:
    """
    Persistent, thread-safe account protection layer.

    Instantiate once at bot startup.  Pass into every scan cycle and trade-close
    handler.  The guard maintains its own state file so protection is preserved
    across process restarts within the same trading day.
    """

    def __init__(
        self,
        cfg=None,
        state_path: Optional[Path] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._path = Path(state_path) if state_path else _STATE_FILE
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # Thresholds — can be overridden via cfg.guard.*
        self.consec_halt    = _CONSEC_LOSS_HALT
        self.consec_reduce  = _CONSEC_LOSS_REDUCE
        self.daily_dd_halt  = _DAILY_DD_HALT_PCT
        self.daily_dd_reduce = _DAILY_DD_REDUCE_PCT
        self.total_dd_halt  = _TOTAL_DD_HALT_PCT
        self.small_acct_bal = _SMALL_ACCT_ZAR

        if cfg is not None:
            g = getattr(cfg, "guard", None)
            if g is not None:
                self.consec_halt     = int(getattr(g,   "consecutive_loss_halt",  self.consec_halt))
                self.consec_reduce   = int(getattr(g,   "consecutive_loss_reduce", self.consec_reduce))
                self.daily_dd_halt   = float(getattr(g, "daily_dd_halt_pct",       self.daily_dd_halt))
                self.daily_dd_reduce = float(getattr(g, "daily_dd_reduce_pct",     self.daily_dd_reduce))
                self.total_dd_halt   = float(getattr(g, "total_dd_halt_pct",       self.total_dd_halt))
                self.small_acct_bal  = float(getattr(g, "small_account_balance",   self.small_acct_bal))

        self._state = self._load_state()
        log.warning(
            "[ACCOUNT GUARD] Initialized | consec_halt=%d daily_dd=%.1f%% "
            "total_dd=%.1f%% small_acct=%.0f | peak_equity=%.2f state_date=%s",
            self.consec_halt, self.daily_dd_halt, self.total_dd_halt,
            self.small_acct_bal,
            self._state.get("peak_equity", 0.0),
            self._state.get("date", "none"),
        )

    # ── Primary scan-cycle entry point ────────────────────────────────────────

    def check_cycle(self, equity: float, balance: float, date_utc: date) -> GuardDecision:
        """
        Call once at the top of every scan cycle, before the symbol loop.

        Handles:
          - Daily state reset on new UTC date
          - Peak equity high-water mark update
          - Evaluation of all three halt conditions
          - Size reduction scheduling
        """
        with self._lock:
            self._maybe_reset_day(date_utc, equity, balance)
            return self._evaluate(equity, balance)

    # ── Trade outcome hook ────────────────────────────────────────────────────

    def record_trade_outcome(self, won: bool, pnl: float) -> None:
        """
        Call immediately after every trade closes.

        Updates the consecutive-loss counter.  A win resets the streak.
        State is persisted to disk after every call.
        """
        with self._lock:
            if won:
                prev = self._state["consecutive_losses"]
                self._state["consecutive_losses"] = 0
                if prev > 0:
                    log.warning(
                        "[RISK RESET] Win cleared loss streak | was=%d consecutive losses | pnl=+%.2f",
                        prev, pnl,
                    )
            else:
                self._state["consecutive_losses"] += 1
                self._state["daily_loss_count"]   += 1
                streak = self._state["consecutive_losses"]
                log.warning(
                    "[LOSS STREAK DETECTED] consecutive_losses=%d/%d | pnl=%.2f",
                    streak, self.consec_halt, pnl,
                )
                if streak >= self.consec_reduce:
                    log.warning(
                        "[COOLDOWN ACTIVE] %d consecutive losses — next trade size reduced to 50%%",
                        streak,
                    )
            self._save_state()

    # ── Size multiplier query ─────────────────────────────────────────────────

    def get_size_mult(self, equity: float = 0.0) -> float:
        """
        Return the current size multiplier to apply to combined_size_mult.

        1.0 = full size  |  0.5 = half size  |  0.0 = halted (no trades)
        """
        with self._lock:
            return self._size_mult_unlocked(equity)

    def _size_mult_unlocked(self, equity: float = 0.0) -> float:
        """Compute size multiplier. MUST be called while self._lock is already held."""
        s = self._state
        if s.get("halted"):
            return 0.0
        if s["consecutive_losses"] >= self.consec_reduce:
            return 0.5
        if equity > 0 and s["daily_start_equity"] > 0:
            dd = (s["daily_start_equity"] - equity) / s["daily_start_equity"] * 100.0
            if dd >= self.daily_dd_reduce:
                return 0.5
        return 1.0

    # ── Small account limits ─────────────────────────────────────────────────

    def get_small_account_limits(self, balance: float) -> SmallAccountLimits:
        """
        Return SmallAccountLimits.  active=True when balance < threshold.

        Phase 13 tiers:
          balance < 1000 ZAR  → micro_mode: max_open=1, Tier 1 only
          balance < 2000 ZAR  → small account: max_open=5, lot capped at 0.01

        Caller must apply max_lot_forex / max_lot_gold as a hard ceiling
        on the computed lot size for the respective instrument type.
        """
        micro  = balance > 0 and balance < _MICRO_ACCT_ZAR
        small  = balance > 0 and balance < self.small_acct_bal
        active = small or micro
        if micro:
            log.warning(
                "[MICRO ACCOUNT MODE] balance=%.2f ZAR < %.0f ZAR | "
                "Tier 1 only | max_open=1 | max_lot=0.01 | capital preservation priority",
                balance, _MICRO_ACCT_ZAR,
            )
        elif small:
            log.warning(
                "[SMALL ACCOUNT MODE] balance=%.2f < threshold=%.2f ZAR | "
                "max_open=5 max_lot_forex=0.01 max_lot_gold=0.01",
                balance, self.small_acct_bal,
            )
        return SmallAccountLimits(
            active        = active,
            max_open      = 1 if micro else 5,
            max_lot_forex = 0.01,
            max_lot_gold  = 0.01,
            micro_mode    = micro,
        )

    # ── Telemetry ─────────────────────────────────────────────────────────────

    def telemetry(self, equity: float, balance: float) -> str:
        """Build a structured [RISK STATUS] block for heartbeat logs."""
        with self._lock:
            s             = self._state
            start_eq      = s["daily_start_equity"]
            peak_eq       = s["peak_equity"]
            consec        = s["consecutive_losses"]
            halted        = s.get("halted", False)
            halt_reason   = s.get("halt_reason", "")
            daily_losses  = s.get("daily_loss_count", 0)

            daily_dd_pct = (
                (start_eq - equity) / start_eq * 100.0
                if start_eq > 0 else 0.0
            )
            total_dd_pct = (
                (peak_eq - equity) / peak_eq * 100.0
                if peak_eq > 0 else 0.0
            )
            exposure_pct = (
                max(0.0, (balance - equity) / balance * 100.0)
                if balance > 0 else 0.0
            )
            # Use _size_mult_unlocked() — self._lock is already held here.
            # Calling get_size_mult() here would deadlock (threading.Lock is not reentrant).
            size_mult  = self._size_mult_unlocked(equity)
            risk_mode  = "HALTED" if halted else ("REDUCED" if size_mult < 1.0 else "NORMAL")

        return (
            "\n[RISK STATUS]\n"
            f"  Equity:           {equity:.2f}\n"
            f"  Balance:          {balance:.2f}\n"
            f"  Exposure:         {exposure_pct:.2f}%\n"
            f"  Daily DD:         {daily_dd_pct:.2f}%  (limit={self.daily_dd_halt:.1f}%)\n"
            f"  Total DD (peak):  {total_dd_pct:.2f}%  (limit={self.total_dd_halt:.1f}%)\n"
            f"  Consec Losses:    {consec}/{self.consec_halt}\n"
            f"  Daily Losses:     {daily_losses}\n"
            f"  Size Mult:        {size_mult:.2f}\n"
            f"  Risk Mode:        {risk_mode}\n"
            f"  Halt Reason:      {halt_reason or 'none'}\n"
            f"  Lot Cap:          {_MAX_LOT_MULT:.1f}×"
        )

    # ── Internal: evaluation ──────────────────────────────────────────────────

    def _evaluate(self, equity: float, balance: float) -> GuardDecision:
        """Evaluate halt conditions. Must be called while holding self._lock."""
        s = self._state

        # Update current equity and peak
        s["last_equity"] = equity
        if equity > s.get("peak_equity", 0.0) and equity > 0:
            s["peak_equity"] = equity
        self._save_state()

        # ── Already halted for today ──────────────────────────────────────────
        if s.get("halted"):
            log.warning("[RISK LOCK] Trading halted for today: %s", s.get("halt_reason", "?"))
            return GuardDecision(
                trading_allowed=False,
                size_mult=0.0,
                halt_reason=s.get("halt_reason", "halted"),
                mode="HALTED",
            )

        consec   = s["consecutive_losses"]
        start_eq = s["daily_start_equity"]
        peak_eq  = s["peak_equity"]

        # ── Halt check 1: Consecutive losses ─────────────────────────────────
        if consec >= self.consec_halt:
            reason = f"consecutive_losses={consec}/{self.consec_halt}"
            self._halt(reason)
            log.warning(
                "[CONSECUTIVE LOSS LIMIT] [LOSS STREAK PAUSE] %d consecutive losses — "
                "trading paused until next UTC day",
                consec,
            )
            log.warning("[TRADING PAUSED] reason=%s", reason)
            return GuardDecision(
                trading_allowed=False, size_mult=0.0,
                halt_reason=reason, mode="HALTED",
            )

        # ── Halt check 2: Daily equity drawdown ───────────────────────────────
        if start_eq > 0:
            daily_dd = (start_eq - equity) / start_eq * 100.0
            if daily_dd >= self.daily_dd_halt:
                reason = (
                    f"daily_equity_dd={daily_dd:.2f}%>={self.daily_dd_halt:.1f}%"
                )
                self._halt(reason)
                log.warning(
                    "[DAILY LOSS LIMIT] [DAILY DRAWDOWN HIT] Daily drawdown %.2f%% >= %.1f%% limit — "
                    "trading paused until next UTC day",
                    daily_dd, self.daily_dd_halt,
                )
                log.warning("[TRADING PAUSED] reason=%s", reason)
                return GuardDecision(
                    trading_allowed=False, size_mult=0.0,
                    halt_reason=reason, mode="HALTED",
                )

        # ── Halt check 3: Total equity drawdown from peak ─────────────────────
        if peak_eq > 0:
            total_dd = (peak_eq - equity) / peak_eq * 100.0
            if total_dd >= self.total_dd_halt:
                reason = (
                    f"total_equity_dd={total_dd:.2f}%>={self.total_dd_halt:.1f}%"
                    f"_from_peak={peak_eq:.2f}"
                )
                self._halt(reason)
                log.warning(
                    "[MAX DD HIT] [EQUITY PROTECTION ACTIVATED] Total drawdown %.2f%% >= %.1f%% "
                    "from peak %.2f — trading paused until next UTC day",
                    total_dd, self.total_dd_halt, peak_eq,
                )
                log.warning("[TRADING PAUSED] reason=%s", reason)
                return GuardDecision(
                    trading_allowed=False, size_mult=0.0,
                    halt_reason=reason, mode="HALTED",
                )

        # ── Size reduction (progressive, not halted) ──────────────────────────
        size_mult = 1.0
        mode      = "NORMAL"

        if consec >= self.consec_reduce:
            size_mult = 0.5
            mode      = "REDUCED"
            log.warning(
                "[COOLDOWN ACTIVE] %d consecutive losses — lot size reduced to 50%%",
                consec,
            )

        if start_eq > 0:
            daily_dd = (start_eq - equity) / start_eq * 100.0
            if daily_dd >= self.daily_dd_reduce:
                if size_mult > 0.5:
                    size_mult = 0.5
                    mode      = "REDUCED"
                log.warning(
                    "[RISK NORMALIZED] Daily DD %.2f%% >= %.1f%% threshold — "
                    "lot size reduced to 50%%",
                    daily_dd, self.daily_dd_reduce,
                )

        return GuardDecision(
            trading_allowed=True,
            size_mult=size_mult,
            halt_reason="",
            mode=mode,
        )

    # ── Internal: daily reset ─────────────────────────────────────────────────

    def _maybe_reset_day(self, date_utc: date, equity: float, balance: float) -> None:
        """Reset daily counters when the UTC date changes."""
        today_str   = date_utc.isoformat()
        stored_date = self._state.get("date", "")

        if stored_date == today_str:
            return

        prev_equity = self._state.get("daily_start_equity", 0.0)
        log.warning(
            "[RISK RESET] New UTC trading day %s → %s | "
            "equity=%.2f balance=%.2f | prev_day_start_equity=%.2f",
            stored_date or "none", today_str,
            equity, balance, prev_equity,
        )

        self._state["date"]                = today_str
        self._state["daily_start_equity"]  = equity
        self._state["daily_start_balance"] = balance
        self._state["consecutive_losses"]  = 0
        self._state["daily_loss_count"]    = 0
        self._state["halted"]              = False
        self._state["halt_reason"]         = ""

        # Ensure peak equity is initialised if this is first run
        if self._state.get("peak_equity", 0.0) <= 0 and equity > 0:
            self._state["peak_equity"] = equity

        self._save_state()

    # ── Internal: halt ────────────────────────────────────────────────────────

    def _halt(self, reason: str) -> None:
        self._state["halted"]      = True
        self._state["halt_reason"] = reason
        self._save_state()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("version") == 1:
                    log.warning(
                        "[ACCOUNT GUARD] Loaded saved state | date=%s "
                        "peak_equity=%.2f consec_losses=%d halted=%s",
                        raw.get("date", "?"),
                        raw.get("peak_equity", 0.0),
                        raw.get("consecutive_losses", 0),
                        raw.get("halted", False),
                    )
                    return raw
        except Exception as exc:
            log.warning("[ACCOUNT GUARD] State load failed (%s) — starting fresh", exc)

        return {
            "version":             1,
            "date":                "",
            "peak_equity":         0.0,
            "daily_start_equity":  0.0,
            "daily_start_balance": 0.0,
            "last_equity":         0.0,
            "consecutive_losses":  0,
            "daily_loss_count":    0,
            "halted":              False,
            "halt_reason":         "",
        }

    def _save_state(self) -> None:
        try:
            self._path.write_text(
                json.dumps(self._state, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:
            log.error("[ACCOUNT GUARD] State save failed: %s", exc)


# ── Adaptive symbol activation ────────────────────────────────────────────────

def get_active_symbols(balance: float, configured_symbols: list, cfg=None) -> list:
    """
    Filter the configured symbol list based on account balance.

    Phase 14 rules:
      balance < guard.xauusd_min_balance_zar (default 500 ZAR) → XAUUSD removed
      balance < 3000 ZAR  → GBPJPY lot already capped at 0.01 by SmallAccountLimits;
                            a warning is emitted to flag reduced effective exposure
      balance >= threshold → all configured symbols allowed

    Logs [SMALL ACCOUNT PROTECTION] whenever a symbol is removed at startup.
    Returns the filtered list (never empty — falls back to full list if all removed).
    """
    from typing import List
    _guard_cfg = getattr(cfg, "guard", None) if cfg is not None else None
    _xau_threshold = float(
        getattr(_guard_cfg, "xauusd_min_balance_zar", _XAUUSD_MIN_BALANCE_ZAR)
        if _guard_cfg is not None else _XAUUSD_MIN_BALANCE_ZAR
    )

    active: List[str] = list(configured_symbols)
    removed: List[str] = []

    xauusd_syms = [s for s in active if "XAU" in s.upper() or "GOLD" in s.upper()]
    if xauusd_syms and balance < _xau_threshold:
        for sym in xauusd_syms:
            active.remove(sym)
            removed.append(sym)
        log.warning(
            "[SMALL ACCOUNT PROTECTION] XAUUSD DISABLED | balance=%.2f ZAR < %.0f ZAR threshold | "
            "gold ATR requires larger capital cushion. Re-enable when balance >= %.0f ZAR.",
            balance, _xau_threshold, _xau_threshold,
        )

    if removed:
        log.warning(
            "[SMALL ACCOUNT PROTECTION] Active symbols after balance filter: [%s] | "
            "Removed: [%s]",
            ", ".join(active), ", ".join(removed),
        )
    else:
        log.warning(
            "[SMALL ACCOUNT PROTECTION] Symbol filter passed | balance=%.2f ZAR | "
            "all %d configured symbols active",
            balance, len(active),
        )

    if balance < _GBPJPY_REDUCE_BALANCE_ZAR:
        gbpjpy_syms = [s for s in active if "GBPJPY" in s.upper()]
        if gbpjpy_syms:
            log.warning(
                "[SMALL ACCOUNT PROTECTION] GBPJPY reduced allocation | balance=%.2f ZAR < %.0f ZAR | "
                "lot capped at 0.01 by small-account limits. "
                "Consider removing GBPJPY if drawdown exceeds 5%% in a single session.",
                balance, _GBPJPY_REDUCE_BALANCE_ZAR,
            )

    if not active:
        log.error(
            "[SMALL ACCOUNT PROTECTION] All symbols were filtered out (balance=%.2f ZAR) — "
            "restoring full list to prevent trading halt.",
            balance,
        )
        return list(configured_symbols)

    return active


# ── Module-level lot cap utility ──────────────────────────────────────────────

def cap_lot_multiplier(lot_mult: float, context: str = "") -> float:
    """
    Hard cap on any lot size multiplier — eliminates martingale escalation,
    revenge sizing, and accidental compounding beyond safe exposure.

    Maximum allowed multiplier: 1.5×
    """
    if lot_mult > _MAX_LOT_MULT:
        log.warning(
            "[SAFE LOT CAP] lot_mult %.4f capped to %.2f%s — "
            "[RECOVERY LIMITED] excessive scaling blocked",
            lot_mult, _MAX_LOT_MULT,
            f" | context={context}" if context else "",
        )
        return _MAX_LOT_MULT
    return lot_mult
