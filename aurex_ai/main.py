"""
Aurex AI Signature Strategy — Main Entry Point

Run modes:
    python aurex_ai/main.py                          # live (dry_run from settings.yaml)
    python aurex_ai/main.py --dry-run                # force dry-run
    python aurex_ai/main.py --live                   # force live orders
    python aurex_ai/main.py --backtest               # walk-forward backtest
    python aurex_ai/main.py --symbols EURUSD USDJPY  # override symbol list
    python aurex_ai/main.py --config /path/to/settings.yaml

Or as a module:
    python -m aurex_ai.main
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import ctypes
import os
import signal
import sys
import tempfile
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from aurex_ai.config.loader import load_settings, Settings
from aurex_ai.core.logger import configure_logging, get_logger
from aurex_ai.core.data_feed import DataFeed, TF_M5, TF_H1, TF_H4, TF_M15
from aurex_ai.core.mt5_bridge import MT5Bridge, get_mt5_time, is_mt5_time_fresh, log_time_sync_banner

from aurex_ai.strategy import trend      as trend_mod
from aurex_ai.strategy import liquidity  as liq_mod
from aurex_ai.strategy import fvg        as fvg_mod
from aurex_ai.strategy import fibonacci  as fib_mod
from aurex_ai.strategy import confluence as conf_mod
from aurex_ai.strategy import order_blocks as ob_mod
from aurex_ai.strategy import breakout     as bo_mod
from aurex_ai.strategy import fallback     as fb_mod
from aurex_ai.strategy import structure    as structure_mod

from aurex_ai.execution.scoring_model    import compute_ema_score, compute_confirmation_score
from aurex_ai.execution.decision_engine  import decide, Decision
from aurex_ai.execution.risk_manager     import calculate as calc_risk
from aurex_ai.execution.cooldown_manager import CooldownManager
from aurex_ai.execution.trade_executor   import execute, TradeResult
from aurex_ai.execution.confidence_engine import (
    compute_confidence, MIN_TRADE_CONFIDENCE, get_confidence_threshold,
)
from aurex_ai.execution.trade_manager    import TradeManager
from aurex_ai.execution.reentry_tracker  import ReEntryTracker, ClosedTrade
from aurex_ai.execution.adaptive_limits    import get_adaptive_limit
from aurex_ai.execution.adaptive_execution import build_execution_factors

from aurex_ai.ai.optimizer        import optimize as ai_optimize, Optimizer
from aurex_ai.ai.learning_engine  import LearningEngine
from aurex_ai.ai.weight_adjuster  import FactorWeights

from aurex_ai.analytics.trade_logger import TradeLogger
from aurex_ai.analytics.performance  import PerformanceEngine

try:
    from aurex_ai.api.server import start_server as _api_start, is_paused as _api_is_paused
    _API_AVAILABLE = True
except ImportError:
    def _api_start(_b, **_kw): pass          # type: ignore[misc]
    def _api_is_paused() -> bool: return False  # type: ignore[misc]
    _API_AVAILABLE = False

log = get_logger("main")

_SHUTDOWN = asyncio.Event()


# ── Pip-size helper ───────────────────────────────────────────────────────────

MAX_FALLBACK_TRADES = 2   # default; overridden by cfg.fallback.max_daily_trades
MAX_CYCLE_TRADES    = 2   # hard cap on trades executed in a single scan cycle

# Per-symbol confidence scores from the previous cycle — used to rank symbols
# before the next scan so higher-quality setups are evaluated first.
_symbol_confidence_cache: Dict[str, float] = {}


def _pip_size(symbol: str) -> float:
    s = symbol.upper()
    if "JPY" in s:                return 0.01
    if "BTC" in s or "ETH" in s:  return 1.0
    if any(x in s for x in ("NAS", "US30", "US500", "SPX", "GER", "UK")): return 0.5
    return 0.0001


def _apply_sim_balance(account, cfg) -> "AccountInfo":
    """
    Replace account.balance with cfg.trading.sim_balance when sim_balance > 0.
    Equity is also overridden so downstream margin/drawdown logic stays consistent.
    MT5 execution itself is unaffected — only risk-calculation consumers see the override.
    """
    from aurex_ai.core.data_feed import AccountInfo  # local import avoids circular at module level
    sim_bal = float(getattr(getattr(cfg, "trading", None), "sim_balance", 0.0) or 0.0)
    if sim_bal > 0:
        return replace(account, balance=sim_bal, equity=sim_bal)
    return account


def _derive_setup_type(fvg_present: bool, ob_present: bool, sweep_detected: bool, breakout: bool) -> str:
    """Derive a human-readable setup label for performance tracking."""
    if fvg_present and ob_present:
        return "FVG+OB"
    if fvg_present and sweep_detected:
        return "FVG+Sweep"
    if ob_present and sweep_detected:
        return "OB+Sweep"
    if fvg_present:
        return "FVG"
    if ob_present:
        return "OB"
    if breakout:
        return "Breakout"
    if sweep_detected:
        return "Sweep"
    return "Trend"


def _log_rejected(
    symbol:     str,
    reason:     str,
    confidence: float = 0.0,
    rr:         float = 0.0,
    spread:     float = 0.0,
    atr:        float = 0.0,
) -> None:
    log.warning(
        "[BLOCKED] symbol=%s reason=%s | confidence=%.1f | rr=%.2f | spread=%.1f | atr=%.1f",
        symbol, reason, confidence, rr, spread, atr,
    )


def _check_entry_quality(
    direction:   str,
    candles_m15: list,
    atr_pips:    float,
    pip:         float,
    utc_hour:    int,
) -> Tuple[bool, str]:
    """Deprecated — replaced by build_execution_factors() adaptive sizing."""
    return True, ""


# ── Session-level state ───────────────────────────────────────────────────────

@dataclass
class _DailyState:
    """Per-UTC-day trade counters and P&L tracking."""
    date:                  date
    trades:                int   = 0
    gross_profit:          float = 0.0
    gross_loss:            float = 0.0
    start_balance:         float = 0.0
    fallback_trades_today: int   = 0   # resets each UTC day
    wins_today:            int   = 0
    losses_today:          int   = 0
    consecutive_wins:      int   = 0
    consecutive_losses:    int   = 0

    def reset(self, new_date: date, balance: float) -> None:
        self.date                  = new_date
        self.trades                = 0
        self.gross_profit          = 0.0
        self.gross_loss            = 0.0
        self.start_balance         = balance
        self.fallback_trades_today = 0
        self.wins_today            = 0
        self.losses_today          = 0
        self.consecutive_wins      = 0
        self.consecutive_losses    = 0

    @property
    def daily_loss_pct(self) -> float:
        """Absolute daily loss as a positive %.  Used for the drawdown halt check."""
        if self.start_balance <= 0:
            return 0.0
        net  = self.gross_profit + self.gross_loss
        loss = abs(min(0.0, net))
        return loss / self.start_balance * 100.0

    @property
    def daily_pnl_pct(self) -> float:
        """Signed net daily PnL as %.  Negative when losing.  Used by adaptive limits."""
        if self.start_balance <= 0:
            return 0.0
        return (self.gross_profit + self.gross_loss) / self.start_balance * 100.0


@dataclass
class _SessionState:
    """Lifetime counters for the current process run."""
    total_trades:     int   = 0
    wins:             int   = 0
    losses:           int   = 0
    gross_profit:     float = 0.0
    gross_loss:       float = 0.0
    open_tickets:     Set[int]         = field(default_factory=set)
    open_trade_meta:  Dict[int, dict]  = field(default_factory=dict)

    def record_trade(self, pnl: float) -> None:
        self.total_trades += 1
        if pnl >= 0:
            self.wins        += 1
            self.gross_profit += pnl
        else:
            self.losses      += 1
            self.gross_loss  += pnl


# ── Stack state ───────────────────────────────────────────────────────────────

@dataclass
class _StackState:
    """Tracks which symbols have been stacked this session (max 1 per symbol)."""
    _stacked: Set[str] = field(default_factory=set)

    def was_stacked(self, symbol: str) -> bool:
        return symbol in self._stacked

    def mark_stacked(self, symbol: str) -> None:
        self._stacked.add(symbol)


# ── Entry delay: pending confirmation signals (conservative mode) ─────────────

@dataclass
class _PendingSignal:
    """
    Signal validated by the full pipeline but not yet executed.
    Conservative mode waits for the NEXT M15 candle to close and confirm
    continuation before sending the order to MT5.
    """
    direction:          str
    signal_candle_time: datetime
    signal_high:        float
    signal_low:         float
    expires_at:         datetime


_pending_signals: Dict[str, _PendingSignal] = {}


# ── Fallback helpers ──────────────────────────────────────────────────────────

def _fb_config(cfg: "Settings") -> "fb_mod.FallbackConfig":
    """Build a FallbackConfig from settings.yaml [fallback] section."""
    raw = getattr(cfg, "fallback", None)
    return fb_mod.FallbackConfig(
        enabled            = bool(getattr(raw, "enabled",            True)),
        force_trigger      = bool(getattr(raw, "force_trigger",      False)),
        idle_cycles_normal = int( getattr(raw, "idle_cycles_normal", 5)),
        idle_cycles_active = int( getattr(raw, "idle_cycles_active", 3)),
        max_daily_trades   = int( getattr(raw, "max_daily_trades",   MAX_FALLBACK_TRADES)),
        min_atr_pips       = float(getattr(raw, "min_atr_pips",      8.0)),
        max_spread_pips    = float(getattr(raw, "max_spread_pips",   3.0)),
        min_strength       = float(getattr(raw, "min_strength",      0.35)),
    )


def _should_try_fallback(
    idle_cycles: int,
    daily:       "_DailyState",
    cfg:         "Settings",
    candles_m15: list,
    pip:         float,
    utc_hour:    int,
    symbol:      str = "",
) -> bool:
    """
    Return True if fallback conditions are met for this scan.

    Checks (in order):
      1. fallback.enabled
      2. Daily fallback trade limit not reached
      3. FORCE_FALLBACK bypasses idle threshold check
      4. idle_cycles >= dynamic threshold (session + ATR aware)
    """
    fb_cfg = _fb_config(cfg)

    if not fb_cfg.enabled:
        return False

    if daily.fallback_trades_today >= fb_cfg.max_daily_trades:
        return False

    if fb_cfg.force_trigger:
        log.info("[FALLBACK DEBUG] FORCE_FALLBACK=True — skipping idle threshold")
        return True

    atr_v     = fb_mod.calc_atr_pips(candles_m15, pip)
    threshold = fb_mod.get_idle_threshold(
        utc_hour,
        atr_v,
        base   = fb_cfg.idle_cycles_normal,
        active = fb_cfg.idle_cycles_active,
        min_atr = fb_cfg.min_atr_pips,
    )
    session   = fb_mod._session_name(utc_hour)
    triggered = idle_cycles >= threshold

    log.info(
        "[FALLBACK DEBUG] idle=%d threshold=%d session=%s atr=%.1f -> %s",
        idle_cycles, threshold, session, atr_v,
        "TRIGGER" if triggered else f"SKIP (need {threshold - idle_cycles} more cycle(s))",
    )
    return triggered


# ── Fallback pipeline ─────────────────────────────────────────────────────────

async def _fallback_pipeline(
    symbol:       str,
    candles_m15:  list,
    price:        float,
    pip:          float,
    feed:         "DataFeed",
    bridge:       "MT5Bridge",
    cooldown:     "CooldownManager",
    cfg:          "Settings",
    daily:        "_DailyState",
    session:      "_SessionState",
    engine:       "LearningEngine",
    signal_id:    str,
    current_time: datetime,
    idle_cycles:  int = 0,
) -> Optional["TradeResult"]:
    """
    Secondary strategy pipeline.  Activated after idle_cycles >= dynamic threshold.

    Execution flow:
      1. Log activation with idle count and daily fallback budget
      2. Fetch symbol_info to get current spread for pre-filter
      3. Run fb_mod.analyze() with ATR + spread filters
      4. Check directional cooldown
      5. calc_risk() -> execute() -> cooldown.arm() -> state update
    """
    fb_cfg = _fb_config(cfg)

    log.warning(
        "[FALLBACK] Activated for %s | idle=%d daily_fb=%d/%d",
        symbol, idle_cycles, daily.fallback_trades_today, fb_cfg.max_daily_trades,
    )

    # Fetch symbol info and current tick (needed for spread + risk calc)
    symbol_info = await asyncio.to_thread(bridge.get_symbol_info, symbol)

    spread_pips = 0.0
    try:
        bid, ask  = await asyncio.to_thread(bridge.get_tick_raw, symbol)
        if pip > 0 and ask > bid:
            spread_pips = round((ask - bid) / pip, 1)
    except Exception:
        spread_pips = 0.0

    atr_pips_fb = fb_mod.calc_atr_pips(candles_m15, pip)

    # Compute confidence BEFORE signal detection so every rejection path has the score.
    # candles_h1 is unavailable in the fallback context (not in the signature).
    confidence_fb = compute_confidence(
        symbol      = symbol,
        candles_m15 = candles_m15,
        candles_h1  = None,
        price       = price,
        atr_pips    = atr_pips_fb,
        pip         = pip,
    )

    log.warning(
        "[FALLBACK] %s | spread=%.1f pips atr=%.1f pips confidence=%d session=%s",
        symbol,
        spread_pips,
        atr_pips_fb,
        confidence_fb.total,
        fb_mod._session_name(current_time.hour),
    )

    # Sanity guard: degenerate candle data (all-zero prices, disconnected feed).
    # compute_confidence returns 0 only if every single component scores 0 simultaneously,
    # which cannot happen with valid live data.
    if confidence_fb.total <= 0:
        log.error(
            "[FALLBACK DATA ERROR] %s confidence=0 — degenerate candle data, skipping",
            symbol,
        )
        return None

    # Run fallback detection with pre-execution filters
    fb = fb_mod.analyze(
        candles_m15,
        price,
        symbol      = symbol,
        pip_size    = pip,
        spread_pips = spread_pips,
        cfg         = fb_cfg,
    )

    if not fb.detected:
        _fb_no_setup_reason = (
            "SPREAD_TOO_HIGH"    if spread_pips > fb_cfg.max_spread_pips else
            "VOLATILITY_TOO_LOW" if atr_pips_fb < fb_cfg.min_atr_pips   else
            "LOW_CONFIDENCE"
        )
        _log_rejected(symbol, _fb_no_setup_reason,
                      confidence=confidence_fb.total, spread=spread_pips, atr=atr_pips_fb)
        log.info("[FALLBACK] %s — no qualifying setup found (confidence=%d)",
                 symbol, confidence_fb.total)
        return None

    if fb.strength < fb_cfg.min_strength:
        log.warning(
            "[TRADE BLOCKED] weak_strength | symbol=%s strength=%.2f threshold=%.2f confidence=%d",
            symbol, fb.strength, fb_cfg.min_strength, confidence_fb.total,
        )
        return None

    direction = fb.direction

    log.warning(
        "[FALLBACK] %s %s | method=%s strength=%.2f confidence=%d | %s",
        symbol, direction, fb.method, fb.strength, confidence_fb.total, fb.reason,
    )

    # Check directional cooldown
    if cooldown.is_on_cooldown(symbol, direction):
        remaining = cooldown.remaining(symbol, direction)
        _log_rejected(symbol, "FILTER_CONFLICT", confidence=confidence_fb.total, spread=spread_pips, atr=atr_pips_fb)
        log.info(
            "[FALLBACK] %s %s — on cooldown (%.0f s remaining), skipping",
            symbol, direction, remaining,
        )
        return None

    # Risk calculation — ATR-derived SL so stop distance is proportional to volatility.
    # A synthetic SweepResult encodes the desired swept_level such that _sl_from_sweep
    # produces: sl = entry ± (ATR * 1.2).  Formula derivation:
    #   BUY:  swept_level - sl_buffer = entry - ATR*1.2  =>  swept_level = entry - ATR*1.2 + buffer
    #   SELL: swept_level + sl_buffer = entry + ATR*1.2  =>  swept_level = entry + ATR*1.2 - buffer
    account  = _apply_sim_balance(await feed.get_account_info(), cfg)
    atr_dist = atr_pips_fb * pip
    _buf     = cfg.risk.sl_buffer_pips * pip
    if direction == "BUY":
        atr_swept = liq_mod.SweepResult(
            detected=True, sweep_type="buy_side",
            swept_level=round(price - atr_dist * 1.2 + _buf, 5),
            strength=fb.strength, pattern="atr_fallback",
            wick_ratio=0.0, touches=0, reason="atr_based",
        )
    else:
        atr_swept = liq_mod.SweepResult(
            detected=True, sweep_type="sell_side",
            swept_level=round(price + atr_dist * 1.2 - _buf, 5),
            strength=fb.strength, pattern="atr_fallback",
            wick_ratio=0.0, touches=0, reason="atr_based",
        )

    log.info(
        "[FALLBACK] %s %s ATR SL target: %.5f (%.1f pips) | TP target: %.1f pips",
        symbol, direction,
        price - atr_dist * 1.2 if direction == "BUY" else price + atr_dist * 1.2,
        atr_pips_fb * 1.2,
        atr_pips_fb * 2.0,
    )

    # ── Tier filter — fallback is always Tier 3 ──────────────────────────────
    _min_tier_fb = int(getattr(cfg.risk, "min_execution_tier", 3))
    if _min_tier_fb < 3:
        log.warning(
            "[RISK FILTER] symbol=%s | tier=3 (fallback) → REJECTED "
            "(min_execution_tier=%d blocks Tier 3 / fallback setups)",
            symbol, _min_tier_fb,
        )
        _log_rejected(symbol, "TIER_BELOW_MINIMUM",
                      confidence=confidence_fb.total, spread=spread_pips, atr=atr_pips_fb)
        return None

    risk = calc_risk(
        direction           = direction,
        entry               = price,
        account             = account,
        candles_m15         = candles_m15,
        sweep               = atr_swept,
        symbol_info         = symbol_info,
        symbol              = symbol,
        risk_pct            = cfg.risk.risk_pct,
        min_rr              = cfg.risk.min_rr,
        tp_fixed_rr         = cfg.risk.tp_fixed_rr,
        sl_buffer_pips      = cfg.risk.sl_buffer_pips,
        size_mult           = 0.25,   # Tier 3 — quarter lot, always
        max_lot_size        = getattr(cfg.risk, "max_lot_size",        0.0),
        max_trade_risk_pct  = getattr(cfg.risk, "max_trade_risk_pct",  15.0),
        fixed_lot_size      = getattr(cfg.risk, "fixed_lot_size",      0.0),
        max_sl_pips         = getattr(cfg.risk, "max_sl_pips",         0.0),
        sl_reduction_mode   = False,   # fallback is Tier-3 minimum size — keep SL strict
    )

    if not risk.allowed:
        _fb_risk_reason = (
            "RR_TOO_LOW"        if "rr_too_low"     in risk.reason else
            "SL_TOO_LARGE"      if "sl_too_large"   in risk.reason else
            "RISK_TOO_HIGH"     if "risk_too_large" in risk.reason else
            "VOLATILITY_TOO_LOW" if "lot_size_zero" in risk.reason else
            "FILTER_CONFLICT"
        )
        _log_rejected(symbol, _fb_risk_reason, confidence=confidence_fb.total, rr=risk.rr_ratio, spread=spread_pips, atr=atr_pips_fb)
        log.warning("[FALLBACK ERROR] %s — risk rejected: %s", symbol, risk.reason)
        return None

    log.info(
        "[FALLBACK] %s %s — sending to execution | "
        "sl=%.1f pips tp=%.1f pips lot=%.2f rr=%.2f",
        symbol, direction,
        risk.sl_pips, risk.sl_pips * risk.rr_ratio,
        risk.lot_size, risk.rr_ratio,
    )

    # Build Tier-3 Decision for the executor.
    # confidence must be set explicitly — Decision.confidence defaults to 0.0.
    fallback_decision = Decision(
        action     = "TIER3",
        direction  = direction,
        score      = round(fb.strength * 100, 1),   # 0–50
        tier       = 3,
        size_mult  = 0.25,
        reason     = f"fallback/{fb.method}",
        confidence = float(confidence_fb.total),
    )

    _mkt_open_fb, _mkt_reason_fb = await asyncio.to_thread(
        bridge.is_market_open, symbol, direction
    )
    if not _mkt_open_fb:
        log.warning(
            "[MARKET CLOSED] %s %s — fallback skipped | reason=%s",
            symbol, direction, _mkt_reason_fb,
        )
        return None

    trade = await execute(
        symbol    = symbol,
        decision  = fallback_decision,
        risk      = risk,
        bridge    = bridge,
        signal_id = signal_id,
        comment   = "AurexFB",
        now_utc   = current_time,
    )

    if not trade.success:
        log.error(
            "[FALLBACK ERROR] %s %s — execution failed: %s",
            symbol, direction, trade.error,
        )
        return None

    # Arm directional cooldown (ranging duration — no trend info available)
    null_trend = trend_mod.TrendResult(
        direction  = "neutral", strength   = 0.0,
        h1_aligned = False,     h4_aligned = False, consistent = False,
        ema50_h1   = 0.0,       ema200_h1  = 0.0,
        ema50_h4   = 0.0,       ema200_h4  = 0.0,
        price      = price,     reason     = "fallback",
    )
    cooldown.arm(
        symbol,
        direction,
        trend            = null_trend,
        trending_minutes = cfg.cooldown.ranging_minutes,
        ranging_minutes  = cfg.cooldown.ranging_minutes,
    )

    # Update counters
    daily.fallback_trades_today += 1
    daily.trades                += 1
    session.total_trades        += 1
    session.open_tickets.add(trade.ticket)
    _opened_at_fb = current_time.isoformat()
    session.open_trade_meta[trade.ticket] = {
        "symbol":      symbol,
        "tier":        3,
        "setup_type":  f"Fallback-{fb.method}",
        "direction":   direction,
        "rr":          risk.rr_ratio,
        "score":       fallback_decision.score,
        "utc_hour":    current_time.hour,
        "entry_price": risk.entry,
        "stop_loss":   risk.stop_loss,
        "take_profit": risk.take_profit,
        "lot_size":    risk.lot_size,
        "opened_at":   _opened_at_fb,
        "signal_id":   signal_id,
        "confidence":  float(confidence_fb.total),
        "strength":    fb.strength,
        "atr_pips":    atr_pips_fb,
    }

    # ── Persist trade open to analytics log ──────────────────────────────────
    try:
        TradeLogger.get_instance().log_open(
            ticket      = trade.ticket,
            signal_id   = signal_id,
            symbol      = symbol,
            direction   = direction,
            tier        = 3,
            setup_type  = f"Fallback-{fb.method}",
            confidence  = float(confidence_fb.total),
            total_score = fallback_decision.score,
            entry_price = risk.entry,
            stop_loss   = risk.stop_loss,
            take_profit = risk.take_profit,
            lot_size    = risk.lot_size,
            risk_reward = risk.rr_ratio,
            atr_pips    = atr_pips_fb,
            utc_hour    = current_time.hour,
            opened_at   = _opened_at_fb,
        )
    except Exception as _tl_exc:
        log.debug("[TRADE LOG] fallback log_open error: %s", _tl_exc)

    log.warning(
        "[FALLBACK] Trade executed successfully: %s %s | "
        "method=%s strength=%.2f lot=%.2f rr=%.2f ticket=%d",
        symbol, direction, fb.method, fb.strength,
        risk.lot_size, risk.rr_ratio, trade.ticket,
    )

    return trade


# ── Trend-stack pipeline ─────────────────────────────────────────────────────

async def _stack_pipeline(
    symbol:       str,
    open_pos:     dict,
    feed:         "DataFeed",
    bridge:       "MT5Bridge",
    cooldown:     "CooldownManager",
    cfg:          "Settings",
    daily:        "_DailyState",
    session:      "_SessionState",
    engine:       "LearningEngine",
    signal_id:    str,
    current_time: datetime,
    trade_mgr:    "TradeManager",
    stack_state:  "_StackState",
) -> Optional["TradeResult"]:
    """
    Add a second position on a symbol whose first trade is already risk-free
    (SL at entry + partial TP done) and is showing a valid pullback to add.

    All six entry conditions must pass:
      1. Symbol not already stacked this session
      2. Daily trade limit not reached
      3. Total open positions < 2
      4. First trade SL == entry  (break-even applied)
      5. Partial TP executed on first trade
      6. Session 07–22 UTC
      7. ATR-normalised pullback in 0.2–0.4 range (not at extreme)
      8. Momentum candle: body >= 60%, direction matches
      9. Confidence >= 70
    """
    direction = open_pos["type"]   # "BUY" | "SELL"
    ticket    = open_pos["ticket"]
    entry     = open_pos["price_open"]
    sl        = open_pos["sl"]
    pip       = _pip_size(symbol)

    # ── Read stack config ─────────────────────────────────────────────────────
    _sc          = getattr(cfg, "stack", None)
    _enabled     = bool(getattr(_sc, "enabled",          True))
    _min_conf    = float(getattr(_sc, "min_confidence",  70.0))
    _pb_min_atr  = float(getattr(_sc, "pullback_min_atr", 0.2))
    _pb_max_atr  = float(getattr(_sc, "pullback_max_atr", 0.4))
    _body_ratio  = float(getattr(_sc, "body_ratio",       0.6))

    if not _enabled:
        return None

    log.info("[STACK CHECK] %s %s | ticket=%d", symbol, direction, ticket)

    # ── Guard: already stacked this session ──────────────────────────────────
    if stack_state.was_stacked(symbol):
        log.info("[STACK BLOCKED] %s | reason=already_stacked", symbol)
        return None

    # ── Guard: adaptive daily trade limit ────────────────────────────────────
    _st_adap_max, _st_adap_mode = get_adaptive_limit(
        daily.consecutive_losses, daily.consecutive_wins,
        daily.daily_pnl_pct, cfg.risk.max_daily_loss_pct,
        max_daily_trades=cfg.risk.max_daily_trades,
    )
    if _st_adap_mode == "drawdown_stop" or daily.trades >= _st_adap_max:
        log.info("[STACK BLOCKED] %s | reason=daily_limit", symbol)
        return None

    # ── Guard: open position cap (respects max_open_trades config) ──────────
    open_count   = len(bridge.get_open_positions())
    _stack_max   = int(getattr(cfg.risk, "max_open_trades", 2))
    # A stack adds a 2nd position on top of the existing 1; only allow if the
    # config ceiling is >= 2.  With max_open_trades=1, stacking is always blocked.
    if open_count >= _stack_max or _stack_max < 2:
        log.info(
            "[STACK BLOCKED] %s | reason=open_cap open=%d max_open=%d",
            symbol, open_count, _stack_max,
        )
        return None

    # ── Guard: break-even applied (SL == entry within 1 pip) ─────────────────
    if abs(sl - entry) > pip:
        log.info(
            "[STACK BLOCKED] %s | reason=no_breakeven sl=%.5f entry=%.5f",
            symbol, sl, entry,
        )
        return None

    # ── Guard: partial TP must be done ───────────────────────────────────────
    if not trade_mgr.was_partially_closed(ticket):
        log.info("[STACK BLOCKED] %s | reason=partial_tp_not_done", symbol)
        return None

    # ── Guard: session filter ─────────────────────────────────────────────────
    if not (7 <= current_time.hour < 22):
        log.info("[STACK BLOCKED] %s | reason=session_filter hour=%d", symbol, current_time.hour)
        return None

    # ── Guard: directional cooldown ───────────────────────────────────────────
    if cooldown.is_on_cooldown(symbol, direction):
        log.info(
            "[STACK BLOCKED] %s | reason=on_cooldown remaining=%.0fs",
            symbol, cooldown.remaining(symbol, direction),
        )
        return None

    # ── Candle data ───────────────────────────────────────────────────────────
    candles_m15 = await feed.get_candles(symbol, TF_M15, cfg.timeframes.m15_candles)
    candles_h1  = await feed.get_candles(symbol, TF_H1,  cfg.timeframes.h1_candles)

    if not candles_m15 or len(candles_m15) < 10:
        log.info("[STACK BLOCKED] %s | reason=insufficient_candles", symbol)
        return None

    atr_pips = fb_mod.calc_atr_pips(candles_m15, pip)
    atr_dist = atr_pips * pip
    last     = candles_m15[-1]

    # ── Confidence gate ───────────────────────────────────────────────────────
    confidence = compute_confidence(
        symbol      = symbol,
        candles_m15 = candles_m15,
        candles_h1  = candles_h1,
        price       = last.close,
        atr_pips    = atr_pips,
        pip         = pip,
    )
    if confidence.total < _min_conf:
        log.info(
            "[STACK BLOCKED] %s | reason=confidence_too_low conf=%.0f min=%.0f",
            symbol, confidence.total, _min_conf,
        )
        return None

    # ── Momentum confirmation ─────────────────────────────────────────────────
    candle_range = last.candle_range
    if candle_range > 0:
        body = last.body_size / candle_range
        bullish = last.close > last.open
        if body < _body_ratio:
            log.info("[STACK BLOCKED] %s | reason=weak_momentum body=%.2f", symbol, body)
            return None
        if direction == "BUY" and not bullish:
            log.info("[STACK BLOCKED] %s | reason=weak_momentum candle_bearish", symbol)
            return None
        if direction == "SELL" and bullish:
            log.info("[STACK BLOCKED] %s | reason=weak_momentum candle_bullish", symbol)
            return None

    # ── Pullback zone check (0.2–0.4 ATR from recent swing) ──────────────────
    lookback = candles_m15[-4:-1]   # 3 candles before current as swing reference
    if lookback:
        if direction == "BUY":
            swing_high = max(c.high for c in lookback)
            dist_from_swing = swing_high - last.open
            if dist_from_swing < _pb_min_atr * atr_dist:
                log.info(
                    "[STACK BLOCKED] %s | reason=pullback_too_shallow dist=%.5f min=%.5f",
                    symbol, dist_from_swing, _pb_min_atr * atr_dist,
                )
                return None
            if dist_from_swing > _pb_max_atr * atr_dist:
                log.info(
                    "[STACK BLOCKED] %s | reason=pullback_too_deep dist=%.5f max=%.5f",
                    symbol, dist_from_swing, _pb_max_atr * atr_dist,
                )
                return None
        else:  # SELL
            swing_low = min(c.low for c in lookback)
            dist_from_swing = last.open - swing_low
            if dist_from_swing < _pb_min_atr * atr_dist:
                log.info(
                    "[STACK BLOCKED] %s | reason=pullback_too_shallow dist=%.5f min=%.5f",
                    symbol, dist_from_swing, _pb_min_atr * atr_dist,
                )
                return None
            if dist_from_swing > _pb_max_atr * atr_dist:
                log.info(
                    "[STACK BLOCKED] %s | reason=pullback_too_deep dist=%.5f max=%.5f",
                    symbol, dist_from_swing, _pb_max_atr * atr_dist,
                )
                return None

    # ── Build SL sweep from 5-candle swing ───────────────────────────────────
    price        = last.close
    swing_candles = candles_m15[-6:-1]
    if direction == "BUY":
        swing_sl = min(c.low for c in swing_candles)
        stack_swept = liq_mod.SweepResult(
            detected=True, sweep_type="buy_side",
            swept_level=round(swing_sl, 5),
            strength=0.7, pattern="stack_swing_low",
            wick_ratio=0.0, touches=0, reason="stack_pullback",
        )
    else:
        swing_sl = max(c.high for c in swing_candles)
        stack_swept = liq_mod.SweepResult(
            detected=True, sweep_type="sell_side",
            swept_level=round(swing_sl, 5),
            strength=0.7, pattern="stack_swing_high",
            wick_ratio=0.0, touches=0, reason="stack_pullback",
        )

    # ── Risk calculation ──────────────────────────────────────────────────────
    account     = _apply_sim_balance(await feed.get_account_info(), cfg)
    symbol_info = await asyncio.to_thread(bridge.get_symbol_info, symbol)

    _stack_aggressive = getattr(getattr(cfg, "trading", None), "trading_mode", "aggressive") == "aggressive"
    risk = calc_risk(
        direction           = direction,
        entry               = price,
        account             = account,
        candles_m15         = candles_m15,
        sweep               = stack_swept,
        symbol_info         = symbol_info,
        symbol              = symbol,
        risk_pct            = cfg.risk.risk_pct,
        min_rr              = cfg.risk.min_rr,
        tp_fixed_rr         = cfg.risk.tp_fixed_rr,
        sl_buffer_pips      = cfg.risk.sl_buffer_pips,
        size_mult           = 0.5,   # half position — conservative stack sizing
        max_lot_size        = getattr(cfg.risk, "max_lot_size",        0.0),
        max_trade_risk_pct  = getattr(cfg.risk, "max_trade_risk_pct",  15.0),
        fixed_lot_size      = getattr(cfg.risk, "fixed_lot_size",      0.0),
        max_sl_pips         = getattr(cfg.risk, "max_sl_pips",         0.0),
        sl_reduction_mode   = _stack_aggressive,
    )

    if not risk.allowed:
        log.info(
            "[STACK BLOCKED] %s | reason=risk_rejected detail=%s rr=%.2f",
            symbol, risk.reason, risk.rr_ratio,
        )
        return None

    # ── Execute ───────────────────────────────────────────────────────────────
    stack_decision = Decision(
        action    = "CONDITIONAL",
        direction = direction,
        score     = float(confidence.total),
        tier      = 2,
        size_mult = 0.5,
        reason    = "stack_pullback",
    )
    stack_decision.confidence = float(confidence.total)

    _mkt_open_st, _mkt_reason_st = await asyncio.to_thread(
        bridge.is_market_open, symbol, direction
    )
    if not _mkt_open_st:
        log.warning(
            "[MARKET CLOSED] %s %s — stack skipped | reason=%s",
            symbol, direction, _mkt_reason_st,
        )
        return None

    trade = await execute(
        symbol    = symbol,
        decision  = stack_decision,
        risk      = risk,
        bridge    = bridge,
        signal_id = signal_id,
        comment   = "AurexStack",
        now_utc   = current_time,
    )

    if not trade.success:
        log.error("[STACK ERROR] %s %s — execution failed: %s", symbol, direction, trade.error)
        return None

    # ── Update state ──────────────────────────────────────────────────────────
    stack_state.mark_stacked(symbol)
    daily.trades         += 1
    session.total_trades += 1
    session.open_tickets.add(trade.ticket)
    session.open_trade_meta[trade.ticket] = {
        "symbol":      symbol,
        "tier":        2,
        "setup_type":  "Stack",
        "direction":   direction,
        "rr":          risk.rr_ratio,
        "score":       float(confidence.total),
        "utc_hour":    current_time.hour,
        "entry_price": risk.entry,
        "confidence":  float(confidence.total),
        "strength":    0.7,   # synthetic sweep used for stack entries
        "atr_pips":    atr_pips,
    }

    log.warning(
        "[STACK ENTRY] %s %s | conf=%.0f rr=%.2f lot=%.2f ticket=%d",
        symbol, direction, confidence.total, risk.rr_ratio, risk.lot_size, trade.ticket,
    )
    return trade


# ── Trading mode parameter resolver ──────────────────────────────────────────

_ATR_DEAD_MARKET = 3.0   # absolute floor — dead market in any mode (pips)

@dataclass
class _ModeParams:
    """
    Resolved per-mode execution parameters.

    Aggressive: looser gates, lower thresholds — designed for small accounts
                ($29–R500) where trade frequency is critical.
    Conservative: original production gates — use once scaling to larger capital.
    """
    name:               str
    min_confidence:     int
    min_atr_pips:       float
    atr_optimal_max:    float   # ATR above this = HIGH zone; 0 = disabled
    min_core_score:     float
    min_exec_composite: float
    min_execution_tier: int
    tier1:              int
    tier2:              int
    tier3:              int
    weights:            Optional[FactorWeights]   # None → use engine weights


def _resolve_mode_params(cfg: "Settings") -> _ModeParams:
    """
    Build mode-specific execution parameters from settings.yaml.

    Priority: modes.<mode>.<key> > strategy/risk/scoring default in settings.yaml.

    Aggressive mode weights are injected directly so the learning engine's
    adaptive weights (tuned for conservative parameters) do not corrupt the
    aggressive gate calibration.  Conservative mode leaves weights=None so the
    learning engine continues to self-tune.
    """
    mode_name = str(
        getattr(getattr(cfg, "trading", None), "trading_mode", "conservative")
    ).lower().strip()

    _modes    = getattr(cfg, "modes", None)
    _mc       = getattr(_modes, mode_name, None) if _modes else None

    def _mv(key: str, default):
        if _mc is not None:
            v = getattr(_mc, key, None)
            if v is not None:
                return v
        return default

    # Per-mode scoring weights (aggressive only — conservative uses engine.get_weights())
    _wc      = getattr(_mc, "weights", None) if _mc else None
    weights  = None
    if _wc is not None:
        weights = FactorWeights(
            trend        = int(getattr(_wc, "trend",        30)),
            liquidity    = int(getattr(_wc, "liquidity",    25)),
            fvg          = int(getattr(_wc, "fvg",          10)),
            fibonacci    = int(getattr(_wc, "fibonacci",    15)),
            ema          = int(getattr(_wc, "ema",          10)),
            confirmation = int(getattr(_wc, "confirmation", 10)),
        )

    # Scoring threshold defaults: read from settings.yaml scoring.thresholds first
    _sc  = getattr(cfg, "scoring", None)
    _th  = getattr(_sc, "thresholds", None) if _sc else None
    _t1  = int(getattr(_th, "tier1", 60)) if _th else 60
    _t2  = int(getattr(_th, "tier2", 50)) if _th else 50
    _t3  = int(getattr(_th, "tier3", 35)) if _th else 35
    _cmin = int(getattr(_sc, "min_confidence", 50) or 50) if _sc else 50

    return _ModeParams(
        name               = mode_name,
        min_confidence     = int(_mv("min_confidence",     _cmin)),
        min_atr_pips       = float(_mv("min_atr_pips",
                                float(getattr(cfg.strategy, "min_atr_pips", 10.0)))),
        atr_optimal_max    = float(_mv("atr_optimal_max_pips",
                                float(getattr(cfg.strategy, "atr_optimal_max_pips", 30.0)))),
        min_core_score     = float(_mv("min_core_score",
                                float(getattr(cfg.strategy, "min_core_score", 35.0)))),
        min_exec_composite = float(_mv("min_exec_composite",
                                float(getattr(cfg.strategy, "min_exec_composite", 0.40)))),
        min_execution_tier = int(_mv("min_execution_tier",
                                int(getattr(cfg.risk, "min_execution_tier", 2)))),
        tier1              = int(_mv("tier1", _t1)),
        tier2              = int(_mv("tier2", _t2)),
        tier3              = int(_mv("tier3", _t3)),
        weights            = weights,
    )


# ── Full strategy pipeline for one symbol ────────────────────────────────────

async def scan_symbol(
    symbol:           str,
    feed:             DataFeed,
    bridge:           MT5Bridge,
    cooldown:         CooldownManager,
    cfg:              Settings,
    daily:            _DailyState,
    session:          _SessionState,
    engine:           LearningEngine,
    idle_cycles:      int  = 0,
    signal_id:        str  = "",
    now_utc:          Optional[datetime] = None,
    reentry_tracker:  Optional[ReEntryTracker] = None,
) -> Optional[TradeResult]:
    """
    Run the full Aurex AI pipeline for a single symbol.

    Returns the TradeResult if a trade was opened, None otherwise.
    """
    current_time = now_utc if now_utc is not None else get_mt5_time()
    today        = current_time.date()

    # ── Trading mode resolution ───────────────────────────────────────────────
    # Reads once per symbol scan; all subsequent gates use mode params.
    mode = _resolve_mode_params(cfg)
    log.warning(
        "[RISK MODE] %s | mode=%s sl_reduction=%s"
        " | atr_floor=%.1f conf_min=%d tiers=%d/%d/%d tier_floor=%d",
        symbol, mode.name.upper(),
        "ADAPTIVE" if mode.name == "aggressive" else "HARD_REJECT",
        mode.min_atr_pips, mode.min_confidence,
        mode.tier1, mode.tier2, mode.tier3, mode.min_execution_tier,
    )

    # ── Daily state reset ─────────────────────────────────────────────────────
    if daily.date != today:
        account = _apply_sim_balance(await feed.get_account_info(), cfg)
        daily.reset(today, account.balance)
        log.info("Daily state reset: %s | balance=%.2f", today, account.balance)

    # ── Open position count ───────────────────────────────────────────────────
    if feed._backtest:
        open_count = len(session.open_tickets)
    else:
        open_count = len(bridge.get_open_positions())

    # ── Open position cap (global) ────────────────────────────────────────────
    if open_count >= cfg.risk.max_open_trades:
        log.warning("[TRADE BLOCKED] %s — max open trades reached (%d/%d)",
                    symbol, open_count, cfg.risk.max_open_trades)
        return None

    # ── Per-symbol cap ────────────────────────────────────────────────────────
    if not feed._backtest:
        _max_per_sym = int(getattr(cfg.risk, "max_open_per_symbol", 1))
        _sym_count   = len(bridge.get_open_positions(symbol=symbol))
        if _sym_count >= _max_per_sym:
            log.info("[TRADE BLOCKED] %s — symbol position cap reached (%d/%d)",
                     symbol, _sym_count, _max_per_sym)
            return None

    # ── Adaptive daily trade cap (skipped in HF mode) ───────────────────────
    _hf_mode = bool(getattr(getattr(cfg, "testing", None), "high_frequency_mode", False))
    if not _hf_mode:
        _adap_max, _adap_mode = get_adaptive_limit(
            daily.consecutive_losses, daily.consecutive_wins,
            daily.daily_pnl_pct, cfg.risk.max_daily_loss_pct,
            max_daily_trades=cfg.risk.max_daily_trades,
        )
        if _adap_mode == "drawdown_stop":
            log.warning(
                "[ADAPTIVE LIMIT] %s trading stopped due to drawdown pnl=%.2f%%",
                symbol, daily.daily_pnl_pct,
            )
            return None
        if daily.trades >= _adap_max:
            if open_count > 0:
                log.warning("[TRADE BLOCKED] %s — daily limit reached (%d/%d)",
                            symbol, daily.trades, _adap_max)
                return None
            log.warning(
                "[TRADE SLOT FREED] %s — daily limit reached but all positions closed, allowing replacement",
                symbol,
            )

    # ── Daily loss limit ──────────────────────────────────────────────────────
    if daily.daily_loss_pct >= cfg.risk.max_daily_loss_pct:
        log.warning(
            "Daily loss limit reached %.2f%% >= %.2f%% — halting trades for today",
            daily.daily_loss_pct, cfg.risk.max_daily_loss_pct,
        )
        return None

    _utc_hour = current_time.hour

    # ── Session hard block ────────────────────────────────────────────────────
    # Only trade during London + New York sessions (07:00–21:00 UTC).
    # Off-hours liquidity is institutional-grade absent: sweeps and FVGs formed
    # in Asian/dead sessions are retail traps, not smart money zones.
    # Fallback pipeline is intentionally also blocked — no exceptions.
    if not (7 <= _utc_hour < 21):
        log.info(
            "[BLOCKED] %s — outside trading session (UTC=%02d, require 07–21)",
            symbol, _utc_hour,
        )
        return None

    # ── Candle data ───────────────────────────────────────────────────────────
    candles_h4  = await feed.get_candles(symbol, TF_H4,  cfg.timeframes.h4_candles)
    candles_h1  = await feed.get_candles(symbol, TF_H1,  cfg.timeframes.h1_candles)
    candles_m15 = await feed.get_candles(symbol, TF_M15, cfg.timeframes.m15_candles)
    candles_m5  = await feed.get_candles(symbol, TF_M5,  cfg.timeframes.m5_candles)

    if not candles_h1 or not candles_m15:
        log.warning("[WARNING] No data for %s — skipping", symbol)
        return None

    if len(candles_h1) < 205 or len(candles_m15) < 10:
        log.warning("[WARNING] %s candle data insufficient H1=%d M15=%d (need H1≥205, M15≥10)",
                    symbol, len(candles_h1), len(candles_m15))
        return None

    # Staleness guard: newest M15 candle must be < 30 min old in live mode.
    # Older data means MT5 is frozen or the symbol feed has dropped.
    if not feed._backtest:
        _ct = current_time
        _lt = candles_m15[-1].time
        # Ensure both are timezone-aware for comparison
        if _ct.tzinfo is None:
            from datetime import timezone as _tz
            _ct = _ct.replace(tzinfo=_tz.utc)
        if _lt.tzinfo is None:
            from datetime import timezone as _tz
            _lt = _lt.replace(tzinfo=_tz.utc)
        _candle_age_mins = (_ct - _lt).total_seconds() / 60
        if _candle_age_mins > 30:
            log.warning(
                "[STALE DATA] %s newest M15 candle is %.0f min old — skipping scan",
                symbol, _candle_age_mins,
            )
            return None

    price    = candles_m15[-1].close
    pip      = _pip_size(symbol)
    atr_pips = fb_mod.calc_atr_pips(candles_m15, pip)

    log.info("[SYMBOL] %s price=%.5f atr=%.1f pips", symbol, price, atr_pips)

    # ── Entry delay: check pending confirmation signal (conservative only) ────
    # On the first pass, conservative mode stores a validated signal and waits.
    # On subsequent scans, when a new M15 candle has closed, this block checks
    # whether price continued in the signal direction before allowing execution.
    # Aggressive mode always executes immediately (_skip_entry_delay = True).
    _skip_entry_delay = True
    if mode.name == "conservative":
        _skip_entry_delay = False   # will store pending unless explicitly confirmed
        _ps = _pending_signals.get(symbol)
        if _ps is not None:
            # Normalize timestamps for safe comparison
            def _tz_safe(dt: datetime) -> datetime:
                return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
            _ps_candle_t = _tz_safe(candles_m15[-1].time)
            _ps_signal_t = _tz_safe(_ps.signal_candle_time)
            _ps_expires  = _tz_safe(_ps.expires_at)
            _cur_tz      = _tz_safe(current_time)

            if _cur_tz > _ps_expires:
                log.warning(
                    "[ENTRY REJECTED] %s — pending signal expired (stored at %s, now %s)",
                    symbol, _ps.signal_candle_time, current_time,
                )
                del _pending_signals[symbol]
                # Fall through: treat as fresh scan, store new pending at end

            elif _ps_candle_t <= _ps_signal_t:
                # Same candle still — wait for next close
                log.debug(
                    "[ENTRY DELAY] %s %s — same candle, waiting for close "
                    "(signal=%s candle=%s)",
                    symbol, _ps.direction,
                    _ps.signal_candle_time, candles_m15[-1].time,
                )
                return None

            else:
                # New candle closed — check continuation
                _nc = candles_m15[-1]
                _confirmed_buy  = (_ps.direction == "BUY"  and _nc.close > _ps.signal_high)
                _confirmed_sell = (_ps.direction == "SELL" and _nc.close < _ps.signal_low)

                if _confirmed_buy or _confirmed_sell:
                    log.warning(
                        "[ENTRY CONFIRMED] %s %s — close=%.5f confirms %s "
                        "(high=%.5f low=%.5f) | proceeding to execution",
                        symbol, _ps.direction, _nc.close, _ps.direction,
                        _ps.signal_high, _ps.signal_low,
                    )
                    del _pending_signals[symbol]
                    _skip_entry_delay = True   # confirmed — skip re-storing below
                else:
                    log.warning(
                        "[ENTRY REJECTED] %s %s — continuation failed "
                        "(close=%.5f signal_high=%.5f signal_low=%.5f)",
                        symbol, _ps.direction, _nc.close,
                        _ps.signal_high, _ps.signal_low,
                    )
                    del _pending_signals[symbol]
                    return None

    # ── Volatility bounds ─────────────────────────────────────────────────────
    _min_atr = mode.min_atr_pips                                  # mode-resolved floor
    _max_atr = float(getattr(cfg.strategy, "max_atr_pips", 0.0))

    if atr_pips <= 0:
        log.warning("[DATA ERROR] %s ATR=0 — corrupted price data, skipping", symbol)
        return None

    # Dead-market absolute floor — blocks all modes (truly no movement possible)
    if atr_pips < _ATR_DEAD_MARKET:
        log.warning(
            "[BLOCKED] symbol=%s reason=atr_dead_market | atr=%.1f < %.1f pips",
            symbol, atr_pips, _ATR_DEAD_MARKET,
        )
        _log_rejected(symbol, "ATR_TOO_LOW", atr=atr_pips)
        return None

    # Mode ATR floor: aggressive=5.0 pips, conservative=10.0 pips.
    # Below this floor the market is not generating enough movement for clean setups.
    if _min_atr > 0 and atr_pips < _min_atr:
        log.warning(
            "[BLOCKED] symbol=%s reason=atr_too_low | atr=%.1f < min=%.1f pips [MODE:%s]",
            symbol, atr_pips, _min_atr, mode.name.upper(),
        )
        _log_rejected(symbol, "ATR_TOO_LOW", atr=atr_pips)
        return None

    # ── ATR zone classification ───────────────────────────────────────────────
    # LOW  (< min_atr_pips): already handled above.
    # OPTIMAL: normal execution window.
    # HIGH (> atr_optimal_max): elevated volatility — conservative blocks, aggressive reduces.
    _atr_opt_max = mode.atr_optimal_max
    if _atr_opt_max > 0 and atr_pips > _atr_opt_max:
        _atr_zone = "HIGH"
        log.warning(
            "[ATR RANGE] %s atr=%.1f zone=HIGH optimal_max=%.0f | action=%s",
            symbol, atr_pips, _atr_opt_max,
            "BLOCK" if mode.name == "conservative" else "REDUCE_SIZE",
        )
        if mode.name == "conservative":
            _log_rejected(symbol, "ATR_TOO_HIGH", atr=atr_pips)
            return None
        # Aggressive: the existing compute_volatility_factor() in build_execution_factors()
        # already applies max_atr proportional reduction — no extra action needed here.
    else:
        _atr_zone = "OPTIMAL"
        log.info(
            "[ATR RANGE] %s atr=%.1f zone=OPTIMAL (floor=%.0f ceiling=%.0f)",
            symbol, atr_pips, _min_atr, _atr_opt_max if _atr_opt_max > 0 else 999,
        )

    # Evaluate fallback readiness once — used at several exit points below.
    # Suppressed when a pending confirmation signal already exists: we don't want
    # fallback to fire a contradictory direction while waiting for main-strategy
    # confirmation on the same symbol.
    _fallback_ready = (
        _should_try_fallback(
            idle_cycles, daily, cfg, candles_m15, pip, current_time.hour,
            symbol=symbol,
        )
        and symbol not in _pending_signals
    )

    # ── AI Confidence pre-filter ──────────────────────────────────────────────
    confidence = compute_confidence(
        symbol      = symbol,
        candles_m15 = candles_m15,
        candles_h1  = candles_h1,
        price       = price,
        atr_pips    = atr_pips,
        pip         = pip,
    )
    _symbol_confidence_cache[symbol] = float(confidence.total)

    _conf_min        = mode.min_confidence
    _conf_adaptive   = get_confidence_threshold(current_time.hour, atr_pips)
    _conf_threshold  = max(_conf_adaptive, _conf_min)

    # Confidence tier cap: even when the gate passes, borderline-confidence markets
    # cap the maximum eligible tier so full position is not placed in uncertain conditions.
    # confidence >= 60 → all tiers eligible (Tier 1 allowed)
    # confidence 50–59 → Tier 2 cap (0.50× max in dynamic mode) — valid setup, uncertain market
    _CONF_FULL_TIER = 60
    _conf_tier_cap  = 1 if confidence.total >= _CONF_FULL_TIER else 2

    log.warning(
        "\n[CONFIDENCE DEBUG] %s\n"
        "  score       = %d  (threshold=%d  adaptive=%d  min_conf=%d)\n"
        "  trend       = %d /25\n"
        "  momentum    = %d /20\n"
        "  structure   = %d /20\n"
        "  volatility  = %d /15  (atr=%.1f pips)\n"
        "  entry       = %d /20  (ema_proximity)\n"
        "  session     = utc_hour=%d\n"
        "  tier_cap    = %s  (score >= %d -> Tier1 eligible)\n"
        "  verdict     = %s",
        symbol,
        confidence.total, _conf_threshold, _conf_adaptive, _conf_min,
        confidence.trend,
        confidence.momentum,
        confidence.structure,
        confidence.volatility, atr_pips,
        confidence.entry,
        current_time.hour,
        f"Tier{_conf_tier_cap}",   # always shows the effective tier cap (1 or 2)
        _CONF_FULL_TIER,
        "PASS" if confidence.total >= _conf_threshold else f"REJECT (score {confidence.total} < threshold {_conf_threshold})",
    )

    if confidence.total < _conf_threshold:
        if _fallback_ready:
            return await _fallback_pipeline(
                symbol, candles_m15, price, pip,
                feed, bridge, cooldown, cfg, daily, session, engine,
                signal_id, current_time, idle_cycles=idle_cycles,
            )
        _log_rejected(symbol, "LOW_CONFIDENCE", confidence=confidence.total, atr=atr_pips)
        return None

    # ── Momentum pre-filter ───────────────────────────────────────────────────
    # Require non-zero 5-bar Rate-of-Change. momentum=0 means price has moved
    # < 0.02% over 75 minutes — the market is chopping in place. No valid
    # sweep, FVG, or structure setup is actionable in that condition.
    if confidence.momentum == 0:
        log.warning(
            "[BLOCKED] %s — zero momentum (5-bar ROC < 0.02%%) | "
            "confidence=%d atr=%.1f",
            symbol, confidence.total, atr_pips,
        )
        _log_rejected(symbol, "ZERO_MOMENTUM", confidence=confidence.total, atr=atr_pips)
        return None

    # ── Trend analysis ────────────────────────────────────────────────────────
    trend_result = trend_mod.analyze(
        candles_h1,
        candles_h4,
        ema_fast = cfg.strategy.ema_fast_period,
        ema_slow = cfg.strategy.ema_slow_period,
    )

    if trend_result.direction == "neutral":
        if _fallback_ready:
            return await _fallback_pipeline(
                symbol, candles_m15, price, pip,
                feed, bridge, cooldown, cfg, daily, session, engine,
                signal_id, current_time, idle_cycles=idle_cycles,
            )
        _log_rejected(symbol, "NEUTRAL_TREND", confidence=confidence.total, atr=atr_pips)
        log.debug("%s: neutral trend — skip", symbol)
        return None

    # ── H1/H4 multi-timeframe consistency gate ────────────────────────────────
    # trend_result.consistent = True when BOTH H1 EMA stack and H4 EMA align on the
    # same direction.  Inconsistency means the two frames disagree — we may be at a
    # higher-TF turning point where the risk of reversal is elevated.
    # aggressive: soft filter (30% size reduction, trade allowed)
    # conservative: hard block
    _mtf_h4_size_penalty = 1.0
    if not trend_result.consistent:
        _h1_bias = ("bull" if trend_result.h1_aligned and trend_result.direction == "bullish"
                    else "bear" if trend_result.h1_aligned else "flat")
        _h4_bias = ("bull" if trend_result.h4_aligned and trend_result.direction == "bullish"
                    else "bear" if trend_result.h4_aligned else "flat")
        if mode.name == "aggressive":
            _mtf_h4_size_penalty = 0.7
            log.warning(
                "[MTF WARNING] %s — H1/H4 inconsistent (H1=%s H4=%s), allowing with 30%% size reduction | %s",
                symbol, _h1_bias, _h4_bias, trend_result.reason,
            )
        else:
            log.warning(
                "[MTF BLOCK] %s — H1/H4 inconsistent (H1=%s H4=%s), blocking [conservative] | %s",
                symbol, _h1_bias, _h4_bias, trend_result.reason,
            )
            _log_rejected(symbol, "MTF_H1_H4_CONFLICT", confidence=confidence.total, atr=atr_pips)
            if _fallback_ready:
                return await _fallback_pipeline(
                    symbol, candles_m15, price, pip,
                    feed, bridge, cooldown, cfg, daily, session, engine,
                    signal_id, current_time, idle_cycles=idle_cycles,
                )
            return None

    # ── Multi-timeframe check: M5 must align with H1 direction ───────────────
    _h1_dir = trend_result.direction   # "bullish" | "bearish"
    _h1_sig = "BUY" if _h1_dir == "bullish" else "SELL"
    if candles_m5 and len(candles_m5) >= 22:
        _m5_closes = [c.close for c in candles_m5]
        _m5_ema20  = trend_mod.ema(_m5_closes, 20)
        _m5_dir    = "bullish" if _m5_closes[-1] > _m5_ema20[-1] else "bearish"
    else:
        _m5_dir = "neutral"
    _m5_sig = "BUY" if _m5_dir == "bullish" else ("SELL" if _m5_dir == "bearish" else "NEUTRAL")

    if _m5_dir == "neutral" or _m5_dir != _h1_dir:
        if mode.name == "conservative":
            log.warning(
                "[MTF BLOCK] %s H1=%s M5=%s — mismatch, blocking in conservative mode",
                symbol, _h1_sig, _m5_sig,
            )
            _log_rejected(symbol, "MTF_M5_CONFLICT", confidence=confidence.total, atr=atr_pips)
            return None
        log.warning("[MTF WARN] %s H1=%s M5=%s — mismatch, will reduce size", symbol, _h1_sig, _m5_sig)
    else:
        log.info("[MTF CHECK] %s H1=%s M5=%s → aligned", symbol, _h1_sig, _m5_sig)

    # ── Directional cooldown fast-path ────────────────────────────────────────
    hint_dir = "BUY" if trend_result.direction == "bullish" else "SELL"
    if cooldown.is_on_cooldown(symbol, hint_dir):
        if _fallback_ready:
            return await _fallback_pipeline(
                symbol, candles_m15, price, pip,
                feed, bridge, cooldown, cfg, daily, session, engine,
                signal_id, current_time, idle_cycles=idle_cycles,
            )
        _log_rejected(symbol, "FILTER_CONFLICT", confidence=confidence.total, atr=atr_pips)
        log.debug("%s %s: on cooldown (%.0f s remaining)",
                  symbol, hint_dir, cooldown.remaining(symbol, hint_dir))
        return None

    # ── Liquidity sweep ───────────────────────────────────────────────────────
    sweep_result = liq_mod.analyze(
        candles_m15,
        symbol         = symbol,
        lookback       = cfg.strategy.sweep_lookback,
        swing_window   = cfg.strategy.swing_window,
        equal_tol_pips = cfg.strategy.equal_tol_pips,
        min_wick_ratio = cfg.strategy.min_wick_ratio,
    )

    # ── Structure detection (BOS + trend bias) ────────────────────────────────
    struct_result = structure_mod.analyze(
        candles       = candles_m15,
        sweep_detected= sweep_result.detected,
        swing_window  = cfg.strategy.swing_window,
    )
    _struct_dir   = struct_result.bos_direction  # "buy" | "sell" | "none"
    _struct_score = struct_result.score

    # Prefer BOS direction; fall back to trend bias when BOS absent.
    if _struct_dir == "none" and struct_result.trend_bias != "neutral":
        _struct_dir = struct_result.trend_bias

    # Aggressive-mode continuation fallback: strong directional trend with no
    # BOS or bias still earns minimum structure credit (5 pts) so that trend-
    # following continuation trades are not penalised for the absence of a
    # fresh break.  Only applies when the trend module confirmed direction.
    if (mode.name == "aggressive" and _struct_score == 0
            and trend_result.direction != "neutral"
            and trend_result.strength >= 10):
        _cont_dir = "buy" if trend_result.direction == "bullish" else "sell"
        _struct_dir   = _cont_dir
        _struct_score = 5.0
        log.info(
            "[STRUCTURE FALLBACK] %s — trend-continuation credit: dir=%s score=5 "
            "(trend strength=%.0f, no BOS/bias detected)",
            symbol, _cont_dir, trend_result.strength,
        )

    log.warning(
        "[STRUCTURE] %s bos=%s(%s) bias=%s score=%.0f [MODE:%s] — %s",
        symbol,
        struct_result.bos_detected,
        _struct_dir,
        struct_result.trend_bias,
        _struct_score,
        mode.name.upper(),
        struct_result.reason,
    )

    # ── Fair Value Gap ────────────────────────────────────────────────────────
    fvg_result = fvg_mod.analyze(
        candles_m15,
        price         = price,
        symbol        = symbol,
        lookback      = cfg.strategy.fvg_lookback,
        min_size_pips = cfg.strategy.fvg_min_size_pips,
        pip_size      = pip,
    )

    # ── Fibonacci retracement ─────────────────────────────────────────────────
    fib_result = fib_mod.analyze(
        candles_m15,
        price         = price,
        symbol        = symbol,
        lookback      = cfg.strategy.fib_lookback,
        fib_ratios    = list(cfg.strategy.fib_ratios),
        golden_zone   = tuple(cfg.strategy.golden_zone),
        min_move_pips = cfg.strategy.fib_min_move_pips,
        pip_size      = pip,
    )

    # ── EMA entry score ───────────────────────────────────────────────────────
    ema_result = compute_ema_score(
        price         = price,
        candles_m15   = candles_m15,
        ema_period    = cfg.strategy.ema_entry_period,
        threshold_pct = cfg.strategy.ema_entry_threshold_pct,
    )

    # ── Candle confirmation score ─────────────────────────────────────────────
    conf_score = compute_confirmation_score(candles_m15, hint_dir)

    # ── Entry confirmation gate ───────────────────────────────────────────────
    # Conservative: engulfing or momentum candle required to enter.
    # Aggressive: absent pattern reduces size by 40%; weak pattern by 15%.
    # _entry_conf_factor is applied to combined_size_mult after risk calculation.
    log.warning(
        "[ENTRY CONFIRMATION] %s %s | pattern=%s score=%.1f",
        symbol, hint_dir, conf_score.pattern, conf_score.score,
    )
    _entry_conf_factor = 1.0
    if conf_score.score <= 0:
        if mode.name == "conservative":
            log.warning(
                "[BLOCKED] %s — no confirmation pattern (conservative mode)", symbol,
            )
            _log_rejected(symbol, "NO_ENTRY_CONFIRMATION",
                          confidence=confidence.total, atr=atr_pips)
            return None
        _entry_conf_factor = 0.60
    elif conf_score.score < 8.0:
        _entry_conf_factor = 0.85

    # ── Order Block analysis ──────────────────────────────────────────────────
    ob_result = ob_mod.analyze(
        candles_m15,
        price    = price,
        symbol   = symbol,
        pip_size = pip,
    )

    # ── Breakout / Retest analysis ────────────────────────────────────────────
    bo_result = bo_mod.analyze(
        candles_m15,
        price    = price,
        symbol   = symbol,
        pip_size = pip,
    )

    # ── Dynamic weights + thresholds from mode + learning engine ─────────────
    # Aggressive mode uses fixed mode-calibrated weights so the AI's adaptive
    # weights (tuned for conservative gates) don't corrupt aggressive execution.
    # Conservative mode lets the learning engine self-tune weights normally.
    weights = mode.weights if mode.weights is not None else engine.get_weights()

    # Thresholds: mode values are the base; learning engine applies ±3 adaptive nudge.
    thresholds = engine.get_thresholds(
        idle_cycles = idle_cycles,
        base_t1     = mode.tier1,
        base_t2     = mode.tier2,
        base_t3     = mode.tier3,
    )

    # ── Confluence ────────────────────────────────────────────────────────────
    confluence = conf_mod.combine(
        trend_result,
        sweep_result,
        fvg_result,
        fib_result,
        ema_score           = ema_result.score,
        confirmation_score  = conf_score.score,
        ob_score            = ob_result.score,
        ob_direction        = ob_result.ob_type,
        breakout_score      = bo_result.score,
        breakout_direction  = bo_result.direction,
        structure_score     = _struct_score,
        structure_direction = _struct_dir,
        weights             = weights,
        symbol              = symbol,
    )

    # ── Decision ──────────────────────────────────────────────────────────────
    decision = decide(
        symbol,
        confluence,
        exec_thresh  = thresholds.tier1,
        cond_thresh  = thresholds.tier2,
        tier3_thresh = thresholds.tier3,
    )
    decision.confidence = float(confidence.total)

    # Apply confidence tier cap: Tier1 requires confidence >= _CONF_FULL_TIER (45).
    # A strong setup in a borderline market runs as Tier2 (0.50× lot) instead of Tier1.
    if decision.tier == 1 and _conf_tier_cap > 1:
        _cap_action = {2: "CONDITIONAL", 3: "TIER3"}[_conf_tier_cap]
        _cap_mult   = {2: 0.50, 3: 0.25}[_conf_tier_cap]
        log.warning(
            "[CONFIDENCE CAP] %s confluence=Tier1 → %s (conf=%d < %d for full tier)",
            symbol, _cap_action, confidence.total, _CONF_FULL_TIER,
        )
        from aurex_ai.execution.decision_engine import Decision as _Decision
        decision = _Decision(
            action     = _cap_action,
            direction  = decision.direction,
            score      = decision.score,
            tier       = _conf_tier_cap,
            size_mult  = _cap_mult,
            reason     = decision.reason + f" [conf_cap: {confidence.total}<{_CONF_FULL_TIER}]",
            confidence = decision.confidence,
        )

    if decision.action == "SKIP":
        skip_reason = (
            "no_consensus" if "no_directional" in decision.reason else "low_score"
        )
        _rej_label = (
            "NO_DIRECTION_CONSENSUS" if skip_reason == "no_consensus"
            else "CONFLUENCE_SCORE_LOW"
        )
        _log_rejected(symbol, _rej_label, confidence=decision.confidence, atr=atr_pips)
        log.info("[SKIP] symbol=%s reason=%s score=%.1f", symbol, skip_reason, decision.score)
        if _fallback_ready:
            return await _fallback_pipeline(
                symbol, candles_m15, price, pip,
                feed, bridge, cooldown, cfg, daily, session, engine,
                signal_id, current_time, idle_cycles=idle_cycles,
            )
        return None

    direction = decision.direction

    # ── Core score gate ───────────────────────────────────────────────────────
    _min_core = mode.min_core_score
    if _min_core > 0 and confluence.core_score < _min_core:
        log.warning(
            "[BLOCKED] symbol=%s reason=core_score_too_low | core=%.1f < min=%.1f "
            "(bonuses=%.1f total=%.1f) [MODE:%s]",
            symbol, confluence.core_score, _min_core,
            confluence.total_score - confluence.core_score, confluence.total_score,
            mode.name.upper(),
        )
        _log_rejected(symbol, "CORE_SCORE_TOO_LOW",
                      confidence=decision.confidence, atr=atr_pips)
        return None

    # ── Tier filter ───────────────────────────────────────────────────────────
    _min_tier = mode.min_execution_tier
    if decision.tier > _min_tier:
        log.warning(
            "[RISK FILTER] symbol=%s | tier=%d | score=%.1f → REJECTED "
            "(tier%d below min_execution_tier=%d) [MODE:%s]",
            symbol, decision.tier, decision.score, decision.tier, _min_tier,
            mode.name.upper(),
        )
        _log_rejected(symbol, "TIER_BELOW_MINIMUM",
                      confidence=decision.confidence, atr=atr_pips)
        return None

    # ── Adaptive execution factors ────────────────────────────────────────────
    _pb_min_atr = float(getattr(cfg.strategy, "entry_pullback_min_atr", 0.20))
    _pb_max_atr = float(getattr(cfg.strategy, "entry_pullback_max_atr", 2.50))

    _exec = build_execution_factors(
        direction        = direction,
        candles_m5       = candles_m5 or candles_m15,
        atr_pips         = atr_pips,
        pip              = pip,
        utc_hour         = _utc_hour,
        h1_dir           = _h1_dir,
        m5_dir           = _m5_dir,
        min_atr_pips     = _min_atr,
        max_atr_pips     = _max_atr,
        pullback_min_atr = _pb_min_atr,
        pullback_max_atr = _pb_max_atr,
    )
    _exec.log_summary(symbol, direction, log)

    # ── Momentum enforcement gate ──────────────────────────────────────────────
    # _exec.momentum_factor encodes candle body quality:
    #   1.0 = body >= 40%, correct direction      (healthy momentum)
    #   0.7 = body 30–40%, correct direction       (acceptable)
    #   0.5 = body < 30% (doji), OR wrong direction close
    # Conservative: block doji / wrong-direction entries (factor < 0.70).
    # Aggressive:   size already reduced via composite; no additional hard block.
    _momentum_threshold = 0.70
    if _exec.momentum_factor < _momentum_threshold:
        _momentum_action = "BLOCK" if mode.name == "conservative" else "REDUCE"
        log.warning(
            "[MOMENTUM FILTER] %s %s | momentum_factor=%.2f threshold=%.2f action=%s",
            symbol, direction, _exec.momentum_factor, _momentum_threshold, _momentum_action,
        )
        if mode.name == "conservative":
            _log_rejected(symbol, "WEAK_MOMENTUM",
                          confidence=decision.confidence, atr=atr_pips)
            return None
    else:
        log.info(
            "[MOMENTUM FILTER] %s %s | momentum_factor=%.2f — OK",
            symbol, direction, _exec.momentum_factor,
        )

    # ── Trade quality classification ──────────────────────────────────────────
    # Composite already encodes session × volatility × momentum × pullback × MTF.
    # HIGH ≥0.80: all factors strong — full execution.
    # MEDIUM 0.50-0.80: some weakness — normal execution with size already adjusted.
    # LOW <0.50: multiple weak factors — conservative mode rejects; aggressive continues.
    _trade_quality = (
        "HIGH"   if _exec.composite >= 0.80 else
        "MEDIUM" if _exec.composite >= 0.50 else
        "LOW"
    )
    log.warning(
        "[TRADE QUALITY] %s %s | quality=%s composite=%.2f entry_conf=%.2f | "
        "session=%.2f momentum=%.2f pullback=%.2f mtf=%.2f",
        symbol, direction, _trade_quality, _exec.composite, _entry_conf_factor,
        _exec.session_factor, _exec.momentum_factor,
        _exec.pullback_factor, _exec.mtf_factor,
    )
    if _trade_quality == "LOW" and mode.name == "conservative":
        log.warning(
            "[BLOCKED] %s — LOW trade quality in conservative mode (composite=%.2f < 0.50)",
            symbol, _exec.composite,
        )
        _log_rejected(symbol, "TRADE_QUALITY_LOW",
                      confidence=decision.confidence, atr=atr_pips)
        return None

    # ── Execution composite gate (fixed-lot mode only) ────────────────────────
    # In fixed-lot mode the composite multiplier cannot reduce lot size, so it
    # would silently pass weak entries (doji, shallow pullback, MTF conflict,
    # off-session combinations).  Convert to a hard gate: composite < threshold
    # means entry quality is too poor to execute at any size.
    _fixed_lot_active  = float(getattr(cfg.risk, "fixed_lot_size", 0.0)) > 0
    _min_exec_comp     = mode.min_exec_composite
    if _fixed_lot_active and _min_exec_comp > 0 and _exec.composite < _min_exec_comp:
        log.warning(
            "[BLOCKED] symbol=%s reason=entry_quality_too_low | "
            "composite=%.2f < %.2f [MODE:%s] | factors: %s",
            symbol, _exec.composite, _min_exec_comp, mode.name.upper(),
            " | ".join(f"{k}={v}" for k, v in _exec.reasons.items()) or "none",
        )
        _log_rejected(symbol, "ENTRY_QUALITY_TOO_LOW",
                      confidence=decision.confidence, atr=atr_pips)
        return None

    # ── Post-decision cooldown check ──────────────────────────────────────────
    if cooldown.is_on_cooldown(symbol, direction):
        _log_rejected(symbol, "FILTER_CONFLICT", confidence=decision.confidence, atr=atr_pips)
        log.debug("%s %s: on cooldown (%.0f s remaining) after decision",
                  symbol, direction, cooldown.remaining(symbol, direction))
        return None

    # ── AI Pre-trade optimizer ────────────────────────────────────────────────
    opt = ai_optimize(
        symbol         = symbol,
        direction      = direction,
        sl_pips        = 20.0,           # placeholder — will be recalculated by risk manager
        tp_pips        = 45.0,           # placeholder — ratio preserved by risk manager
        lot_size       = 1.0,            # placeholder — actual lot from risk manager
        candles_m15    = candles_m15,
        utc_hour       = current_time.hour,
        pip_size       = pip,
        daily_loss_pct = daily.daily_loss_pct,
        max_daily_loss = cfg.risk.max_daily_loss_pct,
        tracker        = engine.get_tracker(),
    )

    # ── Session quality size modifier ────────────────────────────────────────
    # Reduce position size when the current session has historically underperformed
    # (only applies once we have ≥10 trades in that session — avoid early data bias).
    _session_mult = 1.0
    _tracker = engine.get_tracker()
    if not _tracker.is_session_favorable(current_time.hour, threshold=0.45):
        _session_mult = 0.75
        log.warning(
            "[SESSION FILTER] %s — unfavorable session, reducing size to %.0f%%",
            symbol, _session_mult * 100,
        )

    # ── Dynamic risk multiplier based on rolling win rate ────────────────────
    # Win rate > 60%: allow slight increase (+50% risk, capped at 1.5×)
    # Drawdown > 50% of daily limit: already handled by optimizer, but add extra guard
    _dyn_risk_mult = 1.0
    _stats = engine.get_tracker().get_overall_stats()
    _total = _stats.get("total_trades", 0)
    _wr    = _stats.get("win_rate", 0.5)
    if _total >= 20:
        if _wr >= 0.60:
            _dyn_risk_mult = 1.5    # performing well — scale up cautiously
        elif _wr < 0.40:
            _dyn_risk_mult = 0.5    # poor win rate — halve risk until it improves
    # Drawdown circuit-breaker: if daily loss > 1.5% override to 0.5×
    if daily.daily_loss_pct >= cfg.risk.max_daily_loss_pct * 0.75:
        _dyn_risk_mult = min(_dyn_risk_mult, 0.5)
        log.warning(
            "[DYNAMIC RISK] %s — drawdown %.1f%% near limit, risk at 50%%",
            symbol, daily.daily_loss_pct,
        )

    # Combine decision size multiplier with optimizer, session, dynamic risk, execution factors,
    # entry confirmation quality (_entry_conf_factor: 0.60/0.85/1.0), and MTF H1/H4 penalty
    # (_mtf_h4_size_penalty: 0.7 when H1/H4 disagree in aggressive mode, else 1.0).
    combined_size_mult = (
        decision.size_mult
        * opt.lot_mult
        * _session_mult
        * _dyn_risk_mult
        * _exec.composite
        * _entry_conf_factor
        * _mtf_h4_size_penalty
    )
    combined_size_mult = max(0.10, min(1.5, round(combined_size_mult, 2)))

    # ── Dynamic RR profile based on trade quality ────────────────────────────
    # LOW quality entries use tighter TP (1.5R) to bank smaller wins rather than
    # stretching for targets unlikely to be reached in marginal setups.
    _sl_atr_mult  = float(getattr(cfg.strategy, "sl_atr_mult", 1.5))
    _cfg_tp_mult  = float(getattr(cfg.strategy, "tp_atr_mult", 0.0))
    if _cfg_tp_mult > 0:
        _tp_atr_mult = _cfg_tp_mult
    else:
        _tp_atr_mult = {"HIGH": 3.75, "MEDIUM": 3.0, "LOW": 2.25}.get(_trade_quality, 3.0)
    _rr_target = round(_tp_atr_mult / _sl_atr_mult, 2) if _sl_atr_mult > 0 else 2.0
    log.warning(
        "[RR PROFILE] %s %s | quality=%s → sl×%.2f tp×%.2f rr_target=%.2fR",
        symbol, direction, _trade_quality, _sl_atr_mult, _tp_atr_mult, _rr_target,
    )

    # ── Risk calculation ──────────────────────────────────────────────────────
    account     = _apply_sim_balance(await feed.get_account_info(), cfg)
    symbol_info = await asyncio.to_thread(bridge.get_symbol_info, symbol)

    risk = calc_risk(
        direction           = direction,
        entry               = price,
        account             = account,
        candles_m15         = candles_m15,
        sweep               = sweep_result,
        symbol_info         = symbol_info,
        symbol              = symbol,
        risk_pct            = cfg.risk.risk_pct,
        min_rr              = cfg.risk.min_rr,
        tp_fixed_rr         = cfg.risk.tp_fixed_rr,
        sl_buffer_pips      = cfg.risk.sl_buffer_pips,
        size_mult           = combined_size_mult,
        max_lot_size        = getattr(cfg.risk, "max_lot_size",        0.0),
        max_trade_risk_pct  = getattr(cfg.risk, "max_trade_risk_pct",  15.0),
        fixed_lot_size      = getattr(cfg.risk, "fixed_lot_size",      0.0),
        max_sl_pips         = getattr(cfg.risk, "max_sl_pips",         0.0),
        sl_atr_mult         = _sl_atr_mult,
        tp_atr_mult         = _tp_atr_mult,
        sl_reduction_mode   = (mode.name == "aggressive"),
    )

    if not risk.allowed:
        _rej_reason = (
            "RR_TOO_LOW"         if "rr_too_low"     in risk.reason else
            "SL_TOO_LARGE"       if "sl_too_large"   in risk.reason else
            "RISK_TOO_HIGH"      if "risk_too_large" in risk.reason else
            "VOLATILITY_TOO_LOW" if "lot_size_zero"  in risk.reason else
            "FILTER_CONFLICT"
        )
        _log_rejected(symbol, _rej_reason, confidence=decision.confidence, rr=risk.rr_ratio, atr=atr_pips)
        log.info("%s %s: risk rejected — %s", symbol, direction, risk.reason)
        return None

    log.debug(
        "[RISK SIZE] %s lot=%.2f risk_pct=%.2f balance=%.2f sl=%.1fpips rr=%.2f",
        symbol, risk.lot_size, cfg.risk.risk_pct, account.balance, risk.sl_pips, risk.rr_ratio,
    )

    # Apply optimizer SL/TP multipliers to the actual computed levels
    if opt.sl_mult != 1.0:
        sl_dist     = abs(risk.entry - risk.stop_loss)
        tp_dist     = abs(risk.take_profit - risk.entry)
        new_sl_dist = sl_dist * opt.sl_mult
        new_tp_dist = tp_dist * opt.tp_mult

        if direction == "BUY":
            new_sl = round(risk.entry - new_sl_dist, 5)
            new_tp = round(risk.entry + new_tp_dist, 5)
        else:
            new_sl = round(risk.entry + new_sl_dist, 5)
            new_tp = round(risk.entry - new_tp_dist, 5)

        new_rr = round(new_tp_dist / new_sl_dist, 2) if new_sl_dist > 0 else 0.0
        if new_rr >= cfg.risk.min_rr:
            risk.stop_loss   = new_sl
            risk.take_profit = new_tp
            risk.sl_pips     = round(new_sl_dist / pip, 1)
            risk.rr_ratio    = new_rr

    # Entry quality is now fully handled by build_execution_factors() above —
    # no hard block here; size is already adjusted by _exec.composite.

    # ── Re-entry gate ────────────────────────────────────────────────────────
    _is_reentry = False
    if reentry_tracker is not None and reentry_tracker.is_candidate(symbol):
        _re_ok, _re_reason = reentry_tracker.evaluate(
            symbol        = symbol,
            direction     = direction,
            confidence    = float(confidence.total),
            atr_pips      = atr_pips,
            current_price = price,
            current_time  = current_time,
            pip           = pip,
        )
        if not _re_ok:
            return None    # [RE-ENTRY BLOCKED] already logged inside evaluate()
        _is_reentry = (_re_reason == "reentry")

    # ── Market-open guard ────────────────────────────────────────────────────
    _mkt_open, _mkt_reason = await asyncio.to_thread(
        bridge.is_market_open, symbol, direction
    )
    if not _mkt_open:
        log.warning(
            "[MARKET CLOSED] %s %s — skipping execution | reason=%s",
            symbol, direction, _mkt_reason,
        )
        return None

    # ── Spread filter — execution-cost adjusted RR ───────────────────────────
    # Only runs in live mode; skipped in dry_run to avoid stale tick data issues.
    # Effective RR accounts for spread eating TP and widening effective SL:
    #   eff_rr = (sl_pips × rr_ratio − spread) / (sl_pips + spread)
    _spread_pips = 0.0
    if not bridge.dry_run and risk.sl_pips > 0:
        try:
            _bid, _ask = await asyncio.to_thread(bridge.get_tick_raw, symbol)
            if _ask > _bid > 0 and pip > 0:
                _spread_pips = round((_ask - _bid) / pip, 1)
        except Exception:
            pass
    if _spread_pips > 0 and risk.sl_pips > 0:
        _eff_sl = risk.sl_pips + _spread_pips
        _eff_tp = risk.sl_pips * risk.rr_ratio - _spread_pips
        _eff_rr = round(_eff_tp / _eff_sl, 2) if _eff_sl > 0 else 0.0
        _max_spread = float(getattr(getattr(cfg, "fallback", None), "max_spread_pips", 3.0))
        _spread_action = "PASS"
        if _max_spread > 0 and _spread_pips > _max_spread:
            _spread_action = "REJECT_SPREAD"
        elif _eff_rr < cfg.risk.min_rr:
            _spread_action = "REJECT_RR"
        log.warning(
            "[SPREAD FILTER] %s %s | spread=%.1fpips sl=%.1f raw_rr=%.2f eff_rr=%.2f min_rr=%.2f | action=%s",
            symbol, direction, _spread_pips, risk.sl_pips, risk.rr_ratio, _eff_rr,
            cfg.risk.min_rr, _spread_action,
        )
        if _spread_action == "REJECT_SPREAD":
            _log_rejected(symbol, "SPREAD_TOO_HIGH", spread=_spread_pips, max=_max_spread)
            return None
        if _spread_action == "REJECT_RR":
            _log_rejected(symbol, "SPREAD_DEGRADES_RR", eff_rr=_eff_rr, min_rr=cfg.risk.min_rr)
            return None

    # ── Trade allowed — all gates passed ─────────────────────────────────────
    log.warning(
        "[TRADE ALLOWED] symbol=%s direction=%s | score=%.1f tier=%d conf=%.0f "
        "atr=%.1f rr=%.2f lot=%.2f | mode=%s | "
        "core=%.1f trend=%.1f liq=%.1f fvg=%.1f fib=%.1f ema=%.1f struct=%.1f",
        symbol, direction,
        confluence.total_score, decision.tier, confidence.total,
        atr_pips, risk.rr_ratio, risk.lot_size,
        mode.name.upper(),
        confluence.core_score,
        confluence.trend_score, confluence.liquidity_score,
        confluence.fvg_score, confluence.fib_score,
        confluence.ema_score, confluence.structure_score,
    )

    # ── Entry delay: store signal for conservative mode (first-pass) ─────────
    # All gates have passed.  In conservative mode, defer execution by one M15
    # candle to confirm momentum continuation before sending the order.
    # On the NEXT scan where this symbol has a matching pending signal and a new
    # candle has closed with continuation, _skip_entry_delay will be True and
    # execution proceeds normally (this block is skipped).
    if not _skip_entry_delay:
        _sig_candle = candles_m15[-1]
        _pending_signals[symbol] = _PendingSignal(
            direction          = direction,
            signal_candle_time = _sig_candle.time,
            signal_high        = _sig_candle.high,
            signal_low         = _sig_candle.low,
            expires_at         = current_time + timedelta(minutes=45),
        )
        log.warning(
            "[ENTRY DELAY] %s %s — signal stored, waiting for next-candle confirmation | "
            "high=%.5f low=%.5f score=%.1f tier=%d",
            symbol, direction,
            _sig_candle.high, _sig_candle.low,
            confluence.total_score, decision.tier,
        )
        return None

    # ── Execute trade ─────────────────────────────────────────────────────────
    trade = await execute(
        symbol    = symbol,
        decision  = decision,
        risk      = risk,
        bridge    = bridge,
        signal_id = signal_id,
        comment   = f"AurexT{decision.tier}",
        now_utc   = current_time,
    )

    if not trade.success:
        log.error("%s %s: trade failed — %s", symbol, direction, trade.error)
        return None

    # ── Post-execution ────────────────────────────────────────────────────────
    cooldown.arm(
        symbol,
        direction,
        trend            = trend_result,
        trending_minutes = cfg.cooldown.trending_minutes,
        ranging_minutes  = cfg.cooldown.ranging_minutes,
    )

    setup_type = _derive_setup_type(
        fvg_present    = fvg_result.present,
        ob_present     = ob_result.present,
        sweep_detected = sweep_result.detected,
        breakout       = bo_result.detected and bo_result.score >= 9.0,
    )

    daily.trades          += 1
    session.total_trades  += 1
    session.open_tickets.add(trade.ticket)
    session.open_trade_meta[trade.ticket] = {
        "symbol":             symbol,
        "tier":               decision.tier,
        "setup_type":         setup_type,
        "direction":          direction,
        "rr":                 risk.rr_ratio,
        "score":              decision.score,
        "utc_hour":           current_time.hour,
        "entry_price":        risk.entry,
        "stop_loss":          risk.stop_loss,
        "take_profit":        risk.take_profit,
        "lot_size":           risk.lot_size,
        "confidence":         float(confidence.total),
        "strength":           sweep_result.strength,
        "atr_pips":           atr_pips,
        # Component scores for learning engine
        "trend_score":        confluence.trend_score,
        "liquidity_score":    confluence.liquidity_score,
        "fvg_score":          confluence.fvg_score,
        "fib_score":          confluence.fib_score,
        "ema_score":          confluence.ema_score,
        "confirmation_score": confluence.confirmation_score,
        "structure_score":    confluence.structure_score,
        "ob_score":           confluence.ob_score,
        "breakout_score":     confluence.breakout_score,
        "opened_at":          current_time.isoformat(),
        "signal_id":          signal_id,
    }

    # ── Persist trade open to analytics log ──────────────────────────────────
    try:
        TradeLogger.get_instance().log_open(
            ticket             = trade.ticket,
            signal_id          = signal_id,
            symbol             = symbol,
            direction          = direction,
            tier               = decision.tier,
            setup_type         = setup_type,
            confidence         = float(confidence.total),
            total_score        = decision.score,
            trend_score        = confluence.trend_score,
            liquidity_score    = confluence.liquidity_score,
            fvg_score          = confluence.fvg_score,
            fib_score          = confluence.fib_score,
            ema_score          = confluence.ema_score,
            confirmation_score = confluence.confirmation_score,
            structure_score    = confluence.structure_score,
            ob_score           = confluence.ob_score,
            breakout_score     = confluence.breakout_score,
            entry_price        = risk.entry,
            stop_loss          = risk.stop_loss,
            take_profit        = risk.take_profit,
            lot_size           = risk.lot_size,
            risk_reward        = risk.rr_ratio,
            atr_pips           = atr_pips,
            utc_hour           = current_time.hour,
            opened_at          = current_time.isoformat(),
        )
    except Exception as _tl_exc:
        log.debug("[TRADE LOG] log_open error: %s", _tl_exc)

    if _is_reentry:
        log.warning("[RE-ENTRY EXECUTED] %s %s | ticket=%d", symbol, direction, trade.ticket)
        reentry_tracker.clear(symbol)   # type: ignore[union-attr]

    log.warning(
        "[LIVE MODE] symbol=%s direction=%s lot=%.2f risk=%.2f %s sl=%.1f pips "
        "rr=%.2f confidence=%.0f tier=%d score=%.1f setup=%s ticket=%d — decision=EXECUTED",
        symbol, direction, risk.lot_size, risk.risk_amount, account.currency, risk.sl_pips,
        risk.rr_ratio, float(confidence.total), decision.tier, decision.score,
        setup_type, trade.ticket,
    )

    return trade


# ── Startup reconciliation ────────────────────────────────────────────────────

def _reconcile_open_db_trades(bridge: MT5Bridge, trade_logger) -> int:
    """
    Close out any trade_logger records stuck as OPEN that MT5 no longer has open.

    Triggered once at startup to repair the process-restart state gap: when the
    bot restarts, session.open_tickets is empty so _check_live_outcomes never
    fires for trades that closed while the process was down.

    Returns the number of trades reconciled.
    """
    db_open = trade_logger.get_open_trades()
    if not db_open:
        return 0

    live_positions = bridge.get_open_positions()
    live_tickets   = {p["ticket"] for p in live_positions}
    stuck_tickets  = {t["ticket"] for t in db_open} - live_tickets

    if not stuck_tickets:
        return 0

    log.warning(
        "[RECONCILE] %d trade(s) stuck as OPEN in database — attempting MT5 outcome lookup",
        len(stuck_tickets),
    )

    deals      = bridge.get_closed_deals(stuck_tickets, lookback_days=7)
    deal_index = {d["ticket"]: d for d in deals}
    now_ts     = get_mt5_time().isoformat()
    count      = 0

    for trade in db_open:
        ticket = trade["ticket"]
        if ticket not in stuck_tickets:
            continue

        deal    = deal_index.get(ticket)
        profit  = deal["profit"] if deal else 0.0
        symbol  = trade.get("symbol", "UNKNOWN")
        lot     = trade.get("lot_size", 0.01) or 0.01
        pip_sz  = _pip_size_for_symbol(symbol)
        cs      = _contract_size_for_symbol(symbol)
        pips    = round(profit / (lot * cs * pip_sz) if pip_sz > 0 else 0.0, 1)
        result  = (
            "BREAKEVEN" if abs(profit) < 0.01
            else ("WIN" if profit > 0 else "LOSS")
        )

        trade_logger.log_close(
            ticket           = ticket,
            result           = result,
            profit_usd       = profit,
            pips             = pips,
            duration_minutes = 0,
            closed_at        = now_ts,
        )
        log.warning(
            "[RECONCILE] ticket=%d %s %s → %s profit=%.2f (deal_found=%s)",
            ticket, symbol, trade.get("direction", "?"), result, profit,
            str(deal is not None).lower(),
        )
        count += 1

    return count


# ── Live scan loop ─────────────────────────────────────────────────────────────

async def run_live(cfg: Settings, symbols: List[str], bridge: MT5Bridge) -> None:
    """
    Infinite async scan loop over all configured symbols.
    Exits on SIGINT/SIGTERM or when _SHUTDOWN is set.
    """
    feed            = DataFeed(bridge=bridge)
    cooldown        = CooldownManager()
    trade_mgr       = TradeManager()
    reentry_tracker = ReEntryTracker()
    stack_state     = _StackState()
    param_optimizer = Optimizer(cfg)
    daily           = _DailyState(date=get_mt5_time().date())
    session         = _SessionState()
    engine          = LearningEngine.get_instance()
    trade_logger    = TradeLogger.get_instance()
    interval = cfg.trading.scan_interval
    mode     = "DRY_RUN" if bridge.dry_run else "LIVE"

    log.warning(
        "=== Aurex AI LIVE (%s) | symbols=%d | interval=%ds | risk=%.1f%% ===",
        mode, len(symbols), interval, cfg.risk.risk_pct,
    )

    # ── Startup reconciliation ────────────────────────────────────────────────
    if not bridge.dry_run:
        # 1. Close out any trades stuck as OPEN in the database from a previous run
        _rec_count = _reconcile_open_db_trades(bridge, trade_logger)
        if _rec_count:
            log.warning("[STARTUP] Reconciled %d orphaned OPEN trade(s)", _rec_count)

        # 2. Seed session.open_tickets from live MT5 positions so _check_live_outcomes
        #    can detect closes for trades opened before this process started
        _live_now = bridge.get_open_positions()
        for _pos in _live_now:
            session.open_tickets.add(_pos["ticket"])
            if _pos["ticket"] not in session.open_trade_meta:
                session.open_trade_meta[_pos["ticket"]] = {
                    "symbol":      _pos["symbol"],
                    "direction":   _pos["type"],
                    "entry_price": _pos["price_open"],
                    "lot_size":    _pos["volume"],
                    "opened_at":   get_mt5_time().isoformat(),
                    "utc_hour":    get_mt5_time().hour,
                    "rr":          0.0,
                    "signal_type": "pre_restart",
                }
        if _live_now:
            log.warning(
                "[STARTUP] Seeded %d live position(s) into session: %s",
                len(_live_now), [p["ticket"] for p in _live_now],
            )

    _hf_mode = bool(getattr(getattr(cfg, "testing", None), "high_frequency_mode", False))

    # ── Risk config verification (read-only log — no overrides) ─────────────────
    log.warning(
        "[RISK CONFIG] risk_pct=%.2f%% max_open=%d max_open_per_sym=%d max_daily=%d symbols=%d",
        cfg.risk.risk_pct,
        cfg.risk.max_open_trades,
        int(getattr(cfg.risk, "max_open_per_symbol", 1)),
        cfg.risk.max_daily_trades,
        len(symbols),
    )
    _fb_enabled = bool(getattr(getattr(cfg, "fallback", None), "enabled", True))
    _min_conf   = int(getattr(getattr(cfg, "scoring", None), "min_confidence", 0) or 0)
    log.warning(
        "[RISK ACTIVE] risk=%.2f%% open_limit=%d daily_limit=%d min_conf=%d fallback=%s",
        cfg.risk.risk_pct, cfg.risk.max_open_trades, cfg.risk.max_daily_trades,
        _min_conf, str(_fb_enabled).lower(),
    )
    _tm         = getattr(cfg, "trade_management", None)
    _be_on      = bool(getattr(_tm, "breakeven_enabled",  True))
    _partial_on = bool(getattr(_tm, "partial_tp_enabled", True))
    _pt_ratio   = float(getattr(_tm, "partial_tp_ratio",  0.5))
    _be_rr      = float(getattr(_tm, "breakeven_rr",      1.0))
    log.warning(
        "[TRADE MGMT ACTIVE] BE=%s partial=%s ratio=%.2f rr=%.1f",
        str(_be_on).lower(), str(_partial_on).lower(), _pt_ratio, _be_rr,
    )

    log.warning(
        "[TEST MODE ACTIVE] max_daily_trades=%d max_open_trades=%d max_lot_size=%.2f hf_mode=%s",
        cfg.risk.max_daily_trades, cfg.risk.max_open_trades,
        getattr(cfg.risk, "max_lot_size", 0.0),
        str(_hf_mode).lower(),
    )

    # ── Manual override API ───────────────────────────────────────────────────
    if _API_AVAILABLE and not bridge.dry_run:
        _api_start(bridge)

    scan_num    = 0
    idle_cycles = 0

    while not _SHUTDOWN.is_set():
        scan_num    = scan_num + 1
        t_start     = asyncio.get_event_loop().time()
        server_time = get_mt5_time()
        now_ts      = server_time.strftime("%Y-%m-%d %H:%M:%S")

        if not is_mt5_time_fresh() and not bridge.dry_run:
            log.warning(
                "[TIME SYNC] MT5 time unavailable — using local UTC fallback (%s). "
                "New trades blocked until MT5 feed is live.",
                now_ts,
            )

        log.info("[TIME] MT5 Server Time: %s", now_ts)

        # Daily learning update (idempotent — fires once per UTC day)
        engine.run_daily_update()

        # Daily performance snapshot from persistent trade log
        try:
            if trade_logger.total_closed() > 0:
                _perf = PerformanceEngine.compute(trade_logger, window=50)
                log.warning(_perf.to_log_str())
        except Exception as _pe_exc:
            log.debug("[PERFORMANCE] snapshot error: %s", _pe_exc)

        log.info(
            "[HEARTBEAT] cycle=%d timestamp=%s symbols=%d trades_today=%d open=%d idle=%d",
            scan_num, now_ts, len(symbols), daily.trades, len(session.open_tickets), idle_cycles,
        )
        log.info("[SCAN START] cycle=%d symbols=%d", scan_num, len(symbols))

        # Rank symbols by their confidence score from the previous cycle.
        # First cycle all scores are 0 so order equals the configured list.
        ranked_symbols = sorted(
            symbols,
            key=lambda s: _symbol_confidence_cache.get(s, 0.0),
            reverse=True,
        )

        signals_generated = 0
        valid_count       = 0
        trades_this_cycle = 0

        _scan_paused = _api_is_paused()
        if _scan_paused:
            log.warning("[SYSTEM PAUSED] skipping symbol scan this cycle")

        # Open count snapshot for this cycle (used in daily-limit replacement check)
        _loop_open_count = (
            len(session.open_tickets) if feed._backtest
            else len(bridge.get_open_positions())
        )

        # Adaptive limit for this cycle — log mode changes at WARNING
        _cycle_adap_max, _cycle_adap_mode = get_adaptive_limit(
            daily.consecutive_losses, daily.consecutive_wins,
            daily.daily_pnl_pct, cfg.risk.max_daily_loss_pct,
            max_daily_trades=cfg.risk.max_daily_trades,
        )
        if _cycle_adap_mode == "drawdown_stop":
            log.warning(
                "[ADAPTIVE LIMIT] trading stopped due to drawdown | pnl=%.2f%% limit=%.1f%%",
                daily.daily_pnl_pct, cfg.risk.max_daily_loss_pct,
            )
        elif _cycle_adap_mode == "loss_reduction":
            log.warning(
                "[ADAPTIVE LIMIT] reduced due to losses | daily_limit=%d consec_losses=%d",
                _cycle_adap_max, daily.consecutive_losses,
            )
        elif _cycle_adap_mode == "win_increase":
            log.warning(
                "[ADAPTIVE LIMIT] increased due to wins | daily_limit=%d consec_wins=%d",
                _cycle_adap_max, daily.consecutive_wins,
            )

        for sym in ranked_symbols:
            if _scan_paused:
                continue

            # Hard cap: daily limit — skipped in HF mode (only open cap + loss halt enforced)
            if not _hf_mode and (
                _cycle_adap_mode == "drawdown_stop" or (
                    daily.trades >= _cycle_adap_max and _loop_open_count > 0
                )
            ):
                log.info(
                    "[TRADE LIMIT] Skipping %s — daily limit reached (%d/%d)",
                    sym, daily.trades, _cycle_adap_max,
                )
                valid_count += 1
                continue

            # Hard cap: per-cycle limit
            if trades_this_cycle >= MAX_CYCLE_TRADES:
                log.info(
                    "[TRADE LIMIT] Skipping %s — max trades reached (%d/cycle)",
                    sym, MAX_CYCLE_TRADES,
                )
                valid_count += 1
                continue

            # MT5 time sync guard — block new entries when broker feed is stale/unavailable
            if not bridge.dry_run and not is_mt5_time_fresh():
                log.info(
                    "[TIME SYNC] %s — skipping scan: MT5 feed stale, no new entries until feed recovers",
                    sym,
                )
                valid_count += 1
                continue

            cached_conf = _symbol_confidence_cache.get(sym, 0.0)
            log.info("[TOP PICK] Executing %s (confidence=%.0f)", sym, cached_conf)

            try:
                result = await scan_symbol(
                    symbol           = sym,
                    feed             = feed,
                    bridge           = bridge,
                    cooldown         = cooldown,
                    cfg              = cfg,
                    daily            = daily,
                    session          = session,
                    engine           = engine,
                    idle_cycles      = idle_cycles,
                    signal_id        = f"{sym}-{scan_num}",
                    now_utc          = server_time,
                    reentry_tracker  = reentry_tracker,
                )
                valid_count += 1
                if result is not None:
                    signals_generated += 1
                    trades_this_cycle += 1
            except Exception as exc:
                log.error("[ERROR] symbol=%s error=%s", sym, exc, exc_info=True)

        # Update idle cycle counter
        if signals_generated > 0:
            idle_cycles = 0
        else:
            idle_cycles += 1

        # Detect closed positions -> record P&L, update learning engine + cooldowns
        if not bridge.dry_run:
            _check_live_outcomes(
                bridge, session, engine, daily, cooldown, cfg,
                trade_mgr       = trade_mgr,
                reentry_tracker = reentry_tracker,
                current_time    = server_time,
                param_optimizer = param_optimizer,
            )

        # Post-trade management: break-even SL + partial TP
        trade_mgr.manage(bridge, cfg)
        trade_mgr.clear_closed(session.open_tickets)

        # Expire stale re-entry candidates
        reentry_tracker.expire_all(server_time)

        # Stack check — only runs when no fresh primary trade was taken this cycle
        if trades_this_cycle == 0 and not bridge.dry_run:
            try:
                _open_positions = bridge.get_open_positions()
                for _pos in _open_positions:
                    _sym = _pos["symbol"]
                    if _sym not in symbols:
                        continue
                    _stack_result = await _stack_pipeline(
                        symbol       = _sym,
                        open_pos     = _pos,
                        feed         = feed,
                        bridge       = bridge,
                        cooldown     = cooldown,
                        cfg          = cfg,
                        daily        = daily,
                        session      = session,
                        engine       = engine,
                        signal_id    = f"{_sym}-stack-{scan_num}",
                        current_time = server_time,
                        trade_mgr    = trade_mgr,
                        stack_state  = stack_state,
                    )
                    if _stack_result is not None:
                        break   # max 1 stack per cycle
            except Exception as exc:
                log.error("[STACK ERROR] unexpected: %s", exc, exc_info=True)

        elapsed = asyncio.get_event_loop().time() - t_start
        wait    = max(0.0, interval - elapsed)

        log.info(
            "[MT5 DATA] cycle=%d symbols_received=%d valid=%d",
            scan_num, len(symbols), valid_count,
        )
        log.info(
            "[SCAN END] cycle=%d duration=%.2fs signals=%d trades_today=%d idle=%d sleeping=%.1fs",
            scan_num, elapsed, signals_generated, daily.trades, idle_cycles, wait,
        )

        try:
            await asyncio.wait_for(_SHUTDOWN.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass

    log.warning(
        "Aurex AI shutdown | total_trades=%d wins=%d losses=%d",
        session.total_trades, session.wins, session.losses,
    )


def _check_live_outcomes(
    bridge:           MT5Bridge,
    session:          _SessionState,
    engine:           LearningEngine,
    daily:            _DailyState,
    cooldown:         CooldownManager,
    cfg:              Settings,
    trade_mgr:        Optional[TradeManager]      = None,
    reentry_tracker:  Optional[ReEntryTracker]    = None,
    current_time:     Optional[datetime]          = None,
    param_optimizer:  Optional[Optimizer]         = None,
) -> None:
    """
    Detect newly-closed positions, record P&L into the learning engine,
    update daily P&L state, and arm post-outcome cooldowns.
    Also feeds the re-entry tracker and parameter optimizer with trade data.
    """
    try:
        open_positions = bridge.get_open_positions()
        live_tickets   = {p["ticket"] for p in open_positions}
        closed_tickets = session.open_tickets - live_tickets

        if not closed_tickets:
            return

        deals      = bridge.get_closed_deals(closed_tickets)
        deal_index = {d["ticket"]: d for d in deals}

        for ticket in closed_tickets:
            meta = session.open_trade_meta.pop(ticket, None)
            session.open_tickets.discard(ticket)

            if not meta:
                continue

            deal   = deal_index.get(ticket)
            profit = deal["profit"] if deal else 0.0
            won    = profit > 0

            # Daily P&L tracking
            if profit >= 0:
                daily.gross_profit += profit
            else:
                daily.gross_loss   += profit

            # Consecutive win/loss streak tracking (feeds adaptive limit)
            if won:
                daily.wins_today         += 1
                daily.consecutive_wins   += 1
                daily.consecutive_losses  = 0
            else:
                daily.losses_today       += 1
                daily.consecutive_losses += 1
                daily.consecutive_wins    = 0

            # Record into learning engine for weight + threshold adaptation
            engine.record_outcome(
                symbol     = meta["symbol"],
                tier       = meta["tier"],
                setup_type = meta["setup_type"],
                won        = won,
                pnl        = profit,
                rr         = meta["rr"],
                utc_hour   = meta.get("utc_hour"),
            )

            # Arm post-outcome cooldown (extends/reduces based on win/loss)
            cooldown.arm_after_outcome(
                meta["symbol"],
                was_win           = won,
                post_loss_minutes = cfg.cooldown.post_loss_minutes,
                post_win_minutes  = cfg.cooldown.post_win_minutes,
            )

            session.record_trade(profit)

            # Exit classification — used by both re-entry tracker and optimizer
            _be_exit = trade_mgr.was_breakeven(ticket) if trade_mgr else False
            if _be_exit:
                _exit_reason = "BE"
            elif profit < 0:
                _exit_reason = "SL"
            else:
                _exit_reason = "TP"

            # Feed the re-entry tracker
            if reentry_tracker is not None and meta.get("entry_price"):
                reentry_tracker.record(ClosedTrade(
                    symbol      = meta["symbol"],
                    direction   = meta["direction"],
                    entry_price = meta["entry_price"],
                    exit_reason = _exit_reason,
                    closed_at   = current_time or get_mt5_time(),
                    pnl         = profit,
                ))

            # Feed the parameter optimizer
            if param_optimizer is not None:
                _opt_result = "be" if _exit_reason == "BE" else ("win" if won else "loss")
                param_optimizer.record(
                    symbol     = meta["symbol"],
                    direction  = meta["direction"],
                    confidence = meta.get("confidence", 0.0),
                    strength   = meta.get("strength",   0.0),
                    atr_pips   = meta.get("atr_pips",   0.0),
                    utc_hour   = meta.get("utc_hour",   12),
                    result     = _opt_result,
                    rr         = meta["rr"],
                    pnl        = profit,
                    timestamp  = (current_time or get_mt5_time()).isoformat(),
                )

            # ── Persist trade close to analytics log ──────────────────────
            _result_str = (
                "BREAKEVEN" if _exit_reason == "BE" else
                ("WIN"  if won else "LOSS")
            )
            _entry_price = meta.get("entry_price", 0.0)
            _pip_size      = _pip_size_for_symbol(meta["symbol"])
            _contract_size = _contract_size_for_symbol(meta["symbol"])
            _pips = round(
                (profit / (meta.get("lot_size", 0.01) * _contract_size * _pip_size))
                if _pip_size > 0 and meta.get("lot_size", 0) > 0 else 0.0,
                1,
            )
            _duration = 0
            _opened_at_str = meta.get("opened_at", "")
            if _opened_at_str and current_time:
                try:
                    from datetime import timezone as _tz
                    _ot = datetime.fromisoformat(_opened_at_str)
                    if _ot.tzinfo is None:
                        _ot = _ot.replace(tzinfo=_tz.utc)
                    _ct = current_time if current_time.tzinfo else current_time.replace(tzinfo=_tz.utc)
                    _duration = max(0, int((_ct - _ot).total_seconds() / 60))
                except Exception:
                    pass
            try:
                TradeLogger.get_instance().log_close(
                    ticket           = ticket,
                    result           = _result_str,
                    profit_usd       = round(profit, 2),
                    pips             = _pips,
                    duration_minutes = _duration,
                    closed_at        = (current_time or get_mt5_time()).isoformat(),
                )
            except Exception as _tl_exc:
                log.debug("[TRADE LOG] log_close error: %s", _tl_exc)

            # ── Trigger component-score weight update ──────────────────────
            try:
                engine._adjuster.update_from_trade_log(TradeLogger.get_instance())
            except Exception as _wa_exc:
                log.debug("[LEARNING] weight update error: %s", _wa_exc)

            log.info(
                "[LEARNING] Position closed: ticket=%d %s %s tier=%d setup=%s profit=%.2f %s",
                ticket, meta["symbol"], meta["direction"],
                meta["tier"], meta["setup_type"],
                profit, "WIN" if won else "LOSS",
            )

    except Exception as exc:
        log.debug("_check_live_outcomes: %s", exc)


def _pip_size_for_symbol(symbol: str) -> float:
    """Return pip size for a symbol — mirrors _pip_size() but callable from _check_live_outcomes."""
    s = symbol.upper()
    if "JPY" in s:                return 0.01
    if "BTC" in s or "ETH" in s:  return 1.0
    if any(x in s for x in ("NAS", "US30", "US500", "SPX", "GER", "UK")): return 0.5
    return 0.0001


def _contract_size_for_symbol(symbol: str) -> float:
    """Return the standard contract size (units per 1 lot) for position-to-pips conversion."""
    s = symbol.upper()
    if "BTC" in s or "ETH" in s:  return 1.0
    if any(x in s for x in ("NAS", "US30", "US500", "SPX", "GER", "UK")): return 1.0
    return 100_000.0   # standard forex lot


# ── Backtester ────────────────────────────────────────────────────────────────

@dataclass
class _BtTrade:
    """A single simulated open position in the backtest."""
    symbol:      str
    direction:   str
    entry:       float
    stop_loss:   float
    take_profit: float
    lot_size:    float
    sl_pips:     float
    rr:          float
    risk_usd:    float
    open_bar:    int
    ticket:      int
    score:       float
    tier:        int   = 1
    setup_type:  str   = "Trend"
    utc_hour:    int   = 12
    closed:      bool  = False
    outcome:     str   = "open"
    pnl:         float = 0.0


class Backtester:
    """
    Walk-forward backtester.

    Replays M15 candle data bar-by-bar per symbol. At each bar it:
    1. Advances the DataFeed cursor for M15/H1/H4 proportionally
    2. Checks all open simulated trades for SL/TP hit on the current bar
    3. Runs scan_symbol() with dry-run bridge to generate new signals
    4. Accumulates closed-trade P&L

    After all symbols are processed, prints the full performance report.

    CSV layout expected in data_dir:
        {data_dir}/{SYMBOL}_M15.csv
        {data_dir}/{SYMBOL}_H1.csv
        {data_dir}/{SYMBOL}_H4.csv

    CSV columns (case-insensitive):
        datetime (or time), open, high, low, close, volume (or tick_volume)
    """

    def __init__(self, cfg: Settings, symbols: List[str], data_dir: str) -> None:
        self._cfg     = cfg
        self._symbols = symbols

        bridge = MT5Bridge(dry_run=True)
        bridge._connected = True
        self._bridge = bridge

        _bt_ccy = str(getattr(getattr(cfg, "backtest", None), "account_currency", "ZAR") or "ZAR")
        self._feed     = DataFeed(bridge=bridge, backtest=True, data_dir=data_dir, bt_currency=_bt_ccy)
        self._cooldown = CooldownManager()
        self._daily    = _DailyState(date=date(2000, 1, 1))
        self._session  = _SessionState()
        self._engine   = LearningEngine.get_instance()

        self._open_trades:   List[_BtTrade] = []
        self._closed_trades: List[_BtTrade] = []

    def load_data(self) -> None:
        """Load CSV files for all symbols × timeframes."""
        loaded = 0
        for sym in self._symbols:
            for tf in ("H4", "H1", "M15"):
                try:
                    n = self._feed.load_csv(sym, tf)
                    log.info("backtest: loaded %d %s %s candles", n, sym, tf)
                    loaded += 1
                except FileNotFoundError:
                    log.warning("backtest: missing %s_%s.csv — symbol will be skipped", sym, tf)
                except Exception as exc:
                    log.error("backtest: failed to load %s_%s.csv: %s", sym, tf, exc)
        log.info("backtest: %d timeframe files loaded", loaded)

    def run(self) -> None:
        """Execute the full walk-forward backtest."""
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        cfg     = self._cfg
        min_m15 = cfg.timeframes.m15_candles + 10

        for sym in self._symbols:
            m15_bars = self._feed._bt_cache.get(sym, {}).get("M15", [])
            h1_bars  = self._feed._bt_cache.get(sym, {}).get("H1",  [])
            h4_bars  = self._feed._bt_cache.get(sym, {}).get("H4",  [])

            if len(m15_bars) < min_m15:
                log.warning("backtest: %s — only %d M15 bars (need %d) — skipping",
                            sym, len(m15_bars), min_m15)
                continue

            log.info("backtest: scanning %s | %d M15 bars", sym, len(m15_bars))

            for idx in range(min_m15, len(m15_bars)):
                current_bar = m15_bars[idx - 1]

                h1_cursor = min(len(h1_bars), max(1, idx // 4))
                h4_cursor = min(len(h4_bars), max(1, idx // 16))
                self._feed.set_backtest_cursor(sym, "M15", idx)
                self._feed.set_backtest_cursor(sym, "H1",  h1_cursor)
                self._feed.set_backtest_cursor(sym, "H4",  h4_cursor)

                self._check_open_trades(sym, current_bar)

                trade = await scan_symbol(
                    symbol      = sym,
                    feed        = self._feed,
                    bridge      = self._bridge,
                    cooldown    = self._cooldown,
                    cfg         = cfg,
                    daily       = self._daily,
                    session     = self._session,
                    engine      = self._engine,
                    idle_cycles = 0,
                    signal_id   = f"bt-{sym}-{idx}",
                    now_utc     = current_bar.time,
                )

                if trade and trade.success:
                    meta = self._session.open_trade_meta.get(trade.ticket, {})
                    self._open_trades.append(_BtTrade(
                        symbol      = sym,
                        direction   = trade.direction,
                        entry       = trade.executed_price or current_bar.close,
                        stop_loss   = trade.stop_loss,
                        take_profit = trade.take_profit,
                        lot_size    = trade.lot_size,
                        sl_pips     = trade.sl_pips,
                        rr          = trade.rr_ratio,
                        risk_usd    = trade.risk_amount,
                        open_bar    = idx,
                        ticket      = trade.ticket,
                        score       = meta.get("score", 0.0),
                        tier        = meta.get("tier", 1),
                        setup_type  = meta.get("setup_type", "Trend"),
                        utc_hour    = meta.get("utc_hour", 12),
                    ))

        # Force-close remaining open trades
        for t in self._open_trades:
            if not t.closed:
                t.outcome = "open_at_end"
                t.pnl     = 0.0
                t.closed  = True
                self._closed_trades.append(t)

        self._print_report()

    def _check_open_trades(self, symbol: str, bar) -> None:
        """
        Check open simulated trades for SL/TP hit on current bar.
        On a tie (both within bar), SL wins — conservative.
        """
        still_open: List[_BtTrade] = []
        for t in self._open_trades:
            if t.symbol != symbol or t.closed:
                still_open.append(t)
                continue

            hit_tp = hit_sl = False
            if t.direction == "BUY":
                hit_tp = bar.high >= t.take_profit
                hit_sl = bar.low  <= t.stop_loss
            else:
                hit_tp = bar.low  <= t.take_profit
                hit_sl = bar.high >= t.stop_loss

            if hit_sl:
                t.outcome = "loss"
                t.pnl     = -abs(t.risk_usd)
                t.closed  = True
                self._closed_trades.append(t)
                self._session.losses    += 1
                self._session.gross_loss += t.pnl
                self._daily.gross_loss   += t.pnl
                self._session.open_tickets.discard(t.ticket)
                self._cooldown.arm_after_outcome(
                    symbol, was_win=False,
                    post_loss_minutes = self._cfg.cooldown.post_loss_minutes,
                    post_win_minutes  = self._cfg.cooldown.post_win_minutes,
                )
                self._engine.record_outcome(
                    symbol=t.symbol, tier=t.tier, setup_type=t.setup_type,
                    won=False, pnl=t.pnl, rr=t.rr, utc_hour=t.utc_hour,
                )
            elif hit_tp:
                t.outcome = "win"
                t.pnl     = abs(t.risk_usd) * t.rr
                t.closed  = True
                self._closed_trades.append(t)
                self._session.wins         += 1
                self._session.gross_profit += t.pnl
                self._daily.gross_profit   += t.pnl
                self._session.open_tickets.discard(t.ticket)
                self._cooldown.arm_after_outcome(
                    symbol, was_win=True,
                    post_loss_minutes = self._cfg.cooldown.post_loss_minutes,
                    post_win_minutes  = self._cfg.cooldown.post_win_minutes,
                )
                self._engine.record_outcome(
                    symbol=t.symbol, tier=t.tier, setup_type=t.setup_type,
                    won=True, pnl=t.pnl, rr=t.rr, utc_hour=t.utc_hour,
                )
            else:
                still_open.append(t)

        self._open_trades = still_open

    def _print_report(self) -> None:
        trades = [t for t in self._closed_trades if t.outcome in ("win", "loss")]

        if not trades:
            log.warning("Backtest complete — no closed trades")
            return

        wins    = [t for t in trades if t.outcome == "win"]
        losses  = [t for t in trades if t.outcome == "loss"]
        total   = len(trades)
        win_pct = len(wins) / total * 100.0 if total else 0.0

        gross_profit  = sum(t.pnl for t in wins)
        gross_loss    = abs(sum(t.pnl for t in losses))
        net_pnl       = gross_profit - gross_loss
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        avg_win  = gross_profit / len(wins)   if wins   else 0.0
        avg_loss = gross_loss   / len(losses) if losses else 0.0
        avg_rr   = sum(t.rr for t in trades) / total if total else 0.0

        # Tier breakdown
        tier_counts = {1: 0, 2: 0, 3: 0}
        for t in trades:
            tier_counts[t.tier] = tier_counts.get(t.tier, 0) + 1

        # Max drawdown
        equity = peak = max_dd = 0.0
        for t in trades:
            equity += t.pnl
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd

        symbols_traded = len({t.symbol for t in trades})
        _cc = str(getattr(getattr(self._cfg, "backtest", None), "account_currency", "ZAR") or "ZAR")

        report = (
            "\n╔══════════════════════════════════════════════╗\n"
            "║     Aurex AI Signature Strategy              ║\n"
            "║          BACKTEST REPORT                     ║\n"
            "╠══════════════════════════════════════════════╣\n"
            f"║  Symbols traded:    {symbols_traded:>4}                     ║\n"
            f"║  Total trades:      {total:>4}                     ║\n"
            f"║  Wins:              {len(wins):>4}  ({win_pct:5.1f}%)          ║\n"
            f"║  Losses:            {len(losses):>4}                     ║\n"
            f"║  Win rate:          {win_pct:5.1f}%                   ║\n"
            f"║  Profit factor:     {profit_factor:6.2f}                   ║\n"
            f"║  Net P&L:    {net_pnl:>9.2f} {_cc}              ║\n"
            f"║  Gross profit:{gross_profit:>9.2f} {_cc}              ║\n"
            f"║  Gross loss:  {gross_loss:>9.2f} {_cc}              ║\n"
            f"║  Avg win:     {avg_win:>9.2f} {_cc}              ║\n"
            f"║  Avg loss:    {avg_loss:>9.2f} {_cc}              ║\n"
            f"║  Avg R:R ratio:     {avg_rr:5.2f}                   ║\n"
            f"║  Max drawdown:{max_dd:>9.2f} {_cc}              ║\n"
            f"║  Tier 1 trades:     {tier_counts.get(1,0):>4}                     ║\n"
            f"║  Tier 2 trades:     {tier_counts.get(2,0):>4}                     ║\n"
            f"║  Tier 3 trades:     {tier_counts.get(3,0):>4}                     ║\n"
            "╚══════════════════════════════════════════════╝"
        )
        log.warning(report)


# ── CLI argument parsing ──────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog        = "aurex_ai",
        description = "Aurex AI Signature Strategy — institutional FX/Gold trading engine",
        formatter_class = argparse.RawDescriptionHelpFormatter,
        epilog = (
            "Examples:\n"
            "  python aurex_ai/main.py --dry-run\n"
            "  python aurex_ai/main.py --live --symbols EURUSD USDJPY GBPUSD\n"
            "  python aurex_ai/main.py --backtest --data-dir ./data/backtest\n"
        ),
    )
    p.add_argument("--config", metavar="PATH",
        help="Path to settings.yaml (default: aurex_ai/config/settings.yaml)")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", default=None,
        help="Force dry-run mode (no real orders)")
    p.add_argument("--live", action="store_true", default=None,
        help="Force live trading mode")
    p.add_argument("--symbols", nargs="+", metavar="SYM",
        help="Limit symbol list (e.g. --symbols EURUSD USDJPY GBPUSD)")
    p.add_argument("--backtest", action="store_true",
        help="Walk-forward backtest using CSV data")
    p.add_argument("--data-dir", metavar="DIR",
        help="Directory containing CSV backtest files (default: ./data/backtest)")
    return p.parse_args()


# ── Signal handlers ───────────────────────────────────────────────────────────

def _install_shutdown_handlers() -> None:
    def _handler(signum, frame):  # noqa: ANN001
        log.warning("Signal %d received — initiating graceful shutdown", signum)
        try:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(_SHUTDOWN.set)
        except RuntimeError:
            _SHUTDOWN.set()

    try:
        signal.signal(signal.SIGINT,  _handler)
        signal.signal(signal.SIGTERM, _handler)
    except (AttributeError, OSError):
        pass


# ── Startup banner ────────────────────────────────────────────────────────────

def _print_banner(cfg: Settings, symbols: List[str], mode: str) -> None:
    engine     = LearningEngine.get_instance()
    thresholds = engine.get_thresholds()
    log.warning(
        "\n"
        "  ╔══════════════════════════════════════════════╗\n"
        "  ║        Aurex AI Signature Strategy v1.1.0    ║\n"
        "  ╚══════════════════════════════════════════════╝\n"
        "  Mode:               %s\n"
        "  Symbols:            %s\n"
        "  Scan interval:      %d s\n"
        "  Risk per trade:     %.1f%%\n"
        "  Min R:R:            %.1f\n"
        "  Tier 1 threshold:   %d pts (1.00× lot)\n"
        "  Tier 2 threshold:   %d pts (0.50× lot)\n"
        "  Tier 3 threshold:   %d pts (0.25× lot)\n"
        "  Max open trades:    %d\n"
        "  Max daily trades:   %d\n"
        "  Max daily loss:     %.1f%%\n",
        mode,
        ", ".join(symbols),
        cfg.trading.scan_interval,
        cfg.risk.risk_pct,
        cfg.risk.min_rr,
        thresholds.tier1,
        thresholds.tier2,
        thresholds.tier3,
        cfg.risk.max_open_trades,
        cfg.risk.max_daily_trades,
        cfg.risk.max_daily_loss_pct,
    )


# ── Single-instance protection ────────────────────────────────────────────────

_INSTANCE_LOCK_HANDLE = None   # kept alive for the full process lifetime


def _acquire_single_instance_lock() -> None:
    """
    Acquire a global single-instance lock before any trading logic runs.

    Windows (primary): kernel32 CreateMutexW in the Global\\ namespace.
    The OS holds the mutex for the entire process lifetime and releases it
    automatically on termination — even on crash.  No cleanup needed.

    Non-Windows (fallback): PID-validated lock file in the system temp dir.
    On process exit, atexit removes the file.  If the process crashes and the
    file is orphaned, the stale PID check prevents a false "already running".
    """
    global _INSTANCE_LOCK_HANDLE

    _MUTEX_NAME = "Global\\AurexAI_Bot_SingleInstance"
    _LOCK_FILE  = os.path.join(tempfile.gettempdir(), "aurex_ai.lock")

    # ── Windows: kernel-level named mutex ────────────────────────────────────
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        # Declare types so the call is safe on 64-bit Windows
        kernel32.CreateMutexW.restype  = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,   # lpMutexAttributes (NULL)
            ctypes.c_bool,     # bInitialOwner
            ctypes.c_wchar_p,  # lpName
        ]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.GetLastError.restype  = ctypes.c_ulong

        handle   = kernel32.CreateMutexW(None, True, _MUTEX_NAME)
        last_err = kernel32.GetLastError()

        _ERROR_ALREADY_EXISTS = 183

        if handle and last_err != _ERROR_ALREADY_EXISTS:
            # We created the mutex — we are the sole owner
            _INSTANCE_LOCK_HANDLE = handle
            print("[SYSTEM] Single instance lock acquired — bot running safely")
            return

        # Mutex already exists: another instance holds it
        if handle:
            kernel32.CloseHandle(handle)
        print("[SAFETY] Another Aurex AI instance is already running. Exiting.")
        sys.exit(0)

    # ── Non-Windows fallback: PID-validated lock file ─────────────────────────
    def _pid_alive(pid: int) -> bool:
        """Return True if the given process is still running."""
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    if os.path.exists(_LOCK_FILE):
        try:
            stored_pid = int(Path(_LOCK_FILE).read_text().strip())
            if stored_pid != os.getpid() and _pid_alive(stored_pid):
                print("[SAFETY] Another Aurex AI instance is already running. Exiting.")
                sys.exit(0)
        except (ValueError, OSError):
            pass   # corrupt or empty lock file — overwrite below

    try:
        Path(_LOCK_FILE).write_text(str(os.getpid()))
        _INSTANCE_LOCK_HANDLE = _LOCK_FILE
        atexit.register(lambda f=_LOCK_FILE: Path(f).unlink(missing_ok=True))
        print("[SYSTEM] Single instance lock acquired — bot running safely")
    except OSError as exc:
        # Lock file creation failed — warn and continue rather than blocking startup.
        # This should not happen on a normal VPS; the warning is enough to flag it.
        print(f"[WARNING] Could not create instance lock file: {exc} — proceeding without lock")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    # Must be the very first action — before config, logging, and MT5 connection.
    _acquire_single_instance_lock()

    args = _parse_args()
    cfg  = load_settings(path=args.config)

    if args.dry_run:
        cfg.trading.dry_run = True
    elif args.live:
        cfg.trading.dry_run = False

    symbols: List[str] = args.symbols if args.symbols else list(cfg.symbols)

    configure_logging(
        level        = cfg.logging.level,
        log_dir      = cfg.logging.dir,
        max_bytes    = cfg.logging.max_bytes,
        backup_count = cfg.logging.backup_count,
        debug        = getattr(cfg.logging, "debug", False),
    )

    if args.backtest:
        _print_banner(cfg, symbols, "BACKTEST")
        data_dir = args.data_dir or cfg.backtest.data_dir
        bt = Backtester(cfg, symbols, data_dir)
        bt.load_data()
        bt.run()
        return

    mode = "DRY_RUN" if cfg.trading.dry_run else "LIVE"
    _print_banner(cfg, symbols, mode)

    if mode == "LIVE":
        log.warning("!!! LIVE TRADING ACTIVE — real orders will be sent to MT5 !!!")

    bridge = MT5Bridge(
        path        = cfg.mt5.path,
        account     = cfg.mt5.account,
        password    = cfg.mt5.password,
        server      = cfg.mt5.server,
        magic       = cfg.mt5.magic,
        deviation   = cfg.mt5.deviation,
        timeout     = cfg.mt5.timeout,
        max_retries = cfg.mt5.max_retries,
        retry_delay = cfg.mt5.retry_delay,
        dry_run     = cfg.trading.dry_run,
    )

    if not bridge.connect():
        log.error("MT5 connection failed — aborting startup")
        sys.exit(1)

    # Time-sync validation: log local vs MT5 broker time once at startup.
    # All subsequent trading logic uses get_mt5_time() — this is for human verification only.
    log_time_sync_banner(symbol=symbols[0] if symbols else "EURUSD")

    log.warning(
        "[STARTUP] MT5 connected OK | symbols=%d | mode=%s | scan_interval=%ds "
        "| risk=%.1f%% | log_level=%s | debug=%s",
        len(symbols), mode, cfg.trading.scan_interval,
        cfg.risk.risk_pct, cfg.logging.level,
        getattr(cfg.logging, "debug", False),
    )
    log.warning("[SYMBOL CONFIG] ACTIVE SYMBOLS: %s", ", ".join(symbols))

    # ── Live account validation ───────────────────────────────────────────────
    _raw_acct  = bridge.get_account_info_raw()
    _live_bal  = float(getattr(_raw_acct, "balance",  0.0))
    _live_equ  = float(getattr(_raw_acct, "equity",   _live_bal))
    _live_ccy  = str(getattr(_raw_acct,   "currency", "")).strip()

    # FAILSAFE: currency must be detected before trading is allowed.
    # An empty currency field means MT5 account_info() returned corrupt data.
    if not _live_ccy:
        log.error(
            "Currency detection failed — trading halted. "
            "MT5 account_info().currency is empty or unavailable."
        )
        bridge.disconnect()
        sys.exit(1)

    log.warning("MT5 connected | balance=%.2f %s | equity=%.2f %s",
                _live_bal, _live_ccy, _live_equ, _live_ccy)

    _fixed_lot  = float(getattr(cfg.risk, "fixed_lot_size",      0.0))
    _max_sl     = float(getattr(cfg.risk, "max_sl_pips",         50.0))
    _max_rsk    = float(getattr(cfg.risk, "max_trade_risk_pct",  15.0))
    _fb_on      = bool(getattr(getattr(cfg, "fallback", None), "enabled", False))
    _sim_bal    = float(getattr(getattr(cfg, "trading", None), "sim_balance", 0.0) or 0.0)
    _daily_halt = _live_bal * (cfg.risk.max_daily_loss_pct / 100.0) if _live_bal > 0 else 0.0

    # Resolve mode params at startup for the banner
    _startup_mode = _resolve_mode_params(cfg)

    log.warning(
        "\n"
        "  ╔══════════════════════════════════════════════╗\n"
        "  ║       LIVE ACCOUNT MODE ACTIVE               ║\n"
        "  ╚══════════════════════════════════════════════╝\n"
        "  balance          = %.2f %s (real MT5 account)\n"
        "  equity           = %.2f %s\n"
        "  symbols          = %s\n"
        "  lot              = %s\n"
        "  max_open_trades  = %d trade at a time\n"
        "  max_daily_trades = %d trades per day\n"
        "  daily_loss_halt  = %.1f%% ≈ %.2f %s\n"
        "  max_sl_pips      = %.0f pips (wider SL trades rejected)\n"
        "  max_trade_risk   = %.1f%% (survivability gate)\n"
        "  fallback         = %s\n"
        "  sim_balance      = %s\n"
        "  ──────────────────────────────────────────────\n"
        "  TRADING MODE     = %s\n"
        "  min_confidence   = %d\n"
        "  min_atr_pips     = %.0f  (dead_market_floor=%.0f)\n"
        "  score_thresholds = T1≥%d / T2≥%d / T3≥%d\n"
        "  min_tier         = Tier %d+ (lower tiers BLOCKED)\n"
        "  min_core_score   = %.0f\n"
        "  exec_composite   = %.2f",
        _live_bal, _live_ccy, _live_equ, _live_ccy,
        ", ".join(symbols),
        f"{_fixed_lot:.2f} FIXED (dynamic disabled)" if _fixed_lot > 0 else "DYNAMIC (risk_pct=%.2f%%)" % cfg.risk.risk_pct,
        cfg.risk.max_open_trades,
        cfg.risk.max_daily_trades,
        cfg.risk.max_daily_loss_pct, _daily_halt, _live_ccy,
        _max_sl,
        _max_rsk,
        "DISABLED" if not _fb_on else "enabled",
        "DISABLED (real balance active)" if _sim_bal <= 0
            else f"ACTIVE — {_sim_bal:.2f} {_live_ccy} override",
        _startup_mode.name.upper(),
        _startup_mode.min_confidence,
        _startup_mode.min_atr_pips, _ATR_DEAD_MARKET,
        _startup_mode.tier1, _startup_mode.tier2, _startup_mode.tier3,
        _startup_mode.min_execution_tier,
        _startup_mode.min_core_score,
        _startup_mode.min_exec_composite,
    )

    # ── Config check: emit every risk-critical value so operators can verify ─────
    log.warning(
        "\n[CONFIG CHECK] Runtime values loaded from settings.yaml:\n"
        "  risk_pct             = %.4f%%\n"
        "  max_trade_risk_pct   = %.1f%%\n"
        "  fixed_lot_size       = %.2f  (%s)\n"
        "  max_sl_pips          = %.0f  (0=disabled)\n"
        "  max_lot_size         = %.2f\n"
        "  min_atr_pips (mode)  = %.1f\n"
        "  sl_reduction_mode    = %s\n"
        "  trading_mode         = %s\n"
        "  min_rr               = %.1f\n"
        "  max_daily_loss_pct   = %.1f%%",
        cfg.risk.risk_pct,
        _max_rsk,
        _fixed_lot,
        "FIXED lot — dynamic sizing OFF" if _fixed_lot > 0 else "dynamic sizing ON",
        _max_sl,
        getattr(cfg.risk, "max_lot_size", 0.05),
        _startup_mode.min_atr_pips,
        "ADAPTIVE (aggressive)" if _startup_mode.name == "aggressive" else "HARD_REJECT (conservative)",
        _startup_mode.name.upper(),
        cfg.risk.min_rr,
        cfg.risk.max_daily_loss_pct,
    )

    _install_shutdown_handlers()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(run_live(cfg, symbols, bridge))
    except KeyboardInterrupt:
        log.warning("KeyboardInterrupt — shutting down")
        _SHUTDOWN.set()
    finally:
        bridge.disconnect()
        loop.close()
        log.warning("Aurex AI stopped")


if __name__ == "__main__":
    main()
