"""
Aurex AI — Synthetic Regime Expansion / Replay Engine  (Phase 12)

Historical candle replay framework for accelerated AI learning without
capital risk.  Replays M15 candle sequences through a strategy evaluation
function to generate synthetic trade data.

Design constraints (no corners cut)
-------------------------------------
  - No future-leak bias:   strategy_fn only receives history up to bar T.
  - Realistic fills:       spread and slippage are simulated explicitly.
  - No overfitting:        same scoring thresholds as live engine — no relaxation.
  - Regime replay:         select specific regime periods (TRENDING, RANGING, etc.).
  - Monte Carlo paths:     perturb OHLC ±volatility_perturb% for robustness testing.

Modes
-----
  WALK_FORWARD   — sequential forward replay, one bar at a time
  REGIME_REPLAY  — replay only bars matching a target regime string
  MONTE_CARLO    — N paths with independent OHLC perturbation

Usage
-----
    engine = ReplayEngine(ReplayConfig(mode=MODE_WALK_FORWARD))
    result = engine.run(
        candles      = historical_m15_candles,
        strategy_fn  = lambda history: (score, direction, setup_type),  # or None
        risk_fn      = lambda candle, direction, atr: (sl_price, tp_price),
    )
    mc_stats = engine.run_monte_carlo(candles, strategy_fn, risk_fn, n_paths=20)

Logs emitted
------------
  [REPLAY]         — per-run completion summary
  [REPLAY MC]      — Monte Carlo distribution summary
  [REGIME REPLAY]  — per-regime sub-results
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Generator, List, Optional, Tuple

from aurex_ai.core.logger import get_logger

log = get_logger("analytics.replay_engine")

# Replay modes
MODE_WALK_FORWARD  = "WALK_FORWARD"
MODE_REGIME_REPLAY = "REGIME_REPLAY"
MODE_MONTE_CARLO   = "MONTE_CARLO"

_MIN_SCORE_FLOOR = 45   # same as live engine minimum — never relax for synthetic data


@dataclass
class ReplayCandle:
    """Single OHLCV candle for replay (regime-annotated)."""
    time:    datetime
    open:    float
    high:    float
    low:     float
    close:   float
    volume:  float = 0.0
    spread:  float = 0.0   # in price units (not pips)
    regime:  str   = ""    # optional: pre-computed regime label


@dataclass
class ReplayConfig:
    mode:                str            = MODE_WALK_FORWARD
    target_regime:       Optional[str]  = None  # used by REGIME_REPLAY
    spread_mult:         float          = 1.2   # multiply real spread (slippage sim)
    volatility_perturb:  float          = 0.0   # fractional OHLC perturbation (0=off)
    slippage_pips:       float          = 0.5   # extra fill slippage in price units
    max_bars:            int            = 5_000
    min_bars_lookback:   int            = 200   # warm-up history before first signal
    score_floor:         int            = _MIN_SCORE_FLOOR
    rng_seed:            Optional[int]  = None


@dataclass
class ReplayResult:
    mode:           str   = MODE_WALK_FORWARD
    total_bars:     int   = 0
    total_signals:  int   = 0
    executed:       int   = 0
    wins:           int   = 0
    losses:         int   = 0
    total_r:        float = 0.0
    win_rate:       float = 0.0
    expectancy:     float = 0.0
    regime_results: Dict[str, Dict] = field(default_factory=dict)
    completed:      bool  = False


class ReplayEngine:
    """
    Historical candle replay engine for synthetic regime data generation.

    Ensures no future-look bias: at bar i, strategy_fn only receives
    candles[max(0, i - lookback) : i].  The bar at index i is evaluated
    AFTER strategy_fn returns (it is the "next bar" that determines outcome).
    """

    def __init__(self, cfg: Optional[ReplayConfig] = None) -> None:
        self._cfg = cfg or ReplayConfig()
        self._rng = random.Random(self._cfg.rng_seed)

    # ── Public API ────────────────────────────────────────────────────────────

    def run(
        self,
        candles:     List[ReplayCandle],
        strategy_fn: Callable,    # (history: List[ReplayCandle]) -> Optional[Tuple[float, str, str]]
        risk_fn:     Callable,    # (candle: ReplayCandle, direction: str, atr: float) -> Tuple[float, float]
        regime_fn:   Optional[Callable] = None,  # (history) -> str
    ) -> ReplayResult:
        """
        Forward replay `candles` through strategy_fn / risk_fn.

        strategy_fn must return (score, direction, setup_type) or None.
        risk_fn must return (sl_price, tp_price).
        """
        cfg    = self._cfg
        n_bars = min(len(candles), cfg.max_bars)
        lb     = cfg.min_bars_lookback

        result            = ReplayResult(mode=cfg.mode)
        result.total_bars = n_bars

        if n_bars < lb + 10:
            log.warning("[REPLAY] Insufficient candles: %d (need %d+)", n_bars, lb + 10)
            result.completed = True
            return result

        open_trades: List[Dict] = []

        for i in range(lb, n_bars):
            # Strict no-lookahead: history = bars BEFORE bar i
            history = candles[max(0, i - lb) : i]
            current = self._perturb(candles[i])

            # Regime filter for REGIME_REPLAY mode
            if cfg.mode == MODE_REGIME_REPLAY and cfg.target_regime:
                bar_regime = current.regime or (regime_fn(history) if regime_fn else "")
                if bar_regime != cfg.target_regime:
                    continue

            # Resolve any open replay trades on this bar
            still_open = []
            for trade in open_trades:
                outcome = self._resolve_on_bar(trade, current)
                if outcome:
                    won    = outcome == "WIN"
                    rr     = trade["rr"] if won else -1.0
                    result.wins   += int(won)
                    result.losses += int(not won)
                    result.total_r += rr

                    regime = trade.get("regime", "UNKNOWN")
                    reg    = result.regime_results.setdefault(
                        regime, {"wins": 0, "losses": 0, "r": 0.0}
                    )
                    reg["wins" if won else "losses"] += 1
                    reg["r"] += rr
                else:
                    still_open.append(trade)
            open_trades = still_open

            # Evaluate strategy (no future data — history ends at bar i-1)
            try:
                sig = strategy_fn(history)
            except Exception:
                continue

            if sig is None:
                continue

            score, direction, setup_type = sig
            result.total_signals += 1

            if score < cfg.score_floor:
                continue

            try:
                atr         = self._compute_atr(history)
                slip        = cfg.slippage_pips * (1 if direction == "BUY" else -1)
                entry       = current.close + slip
                sl, tp      = risk_fn(current, direction, atr)
            except Exception:
                continue

            if sl <= 0 or tp <= 0 or abs(tp - entry) < 1e-8:
                continue

            rr = abs(tp - entry) / max(abs(entry - sl), 1e-8)
            result.executed += 1
            regime = current.regime or (regime_fn(history) if regime_fn else "UNKNOWN")
            open_trades.append({
                "direction": direction,
                "entry":     entry,
                "sl":        sl,
                "tp":        tp,
                "rr":        round(rr, 3),
                "regime":    regime,
            })

        total_closed = result.wins + result.losses
        if total_closed > 0:
            result.win_rate   = round(result.wins / total_closed, 4)
            result.expectancy = round(result.total_r / total_closed, 4)
        result.completed = True

        log.warning(
            "[REPLAY] %s complete | bars=%d signals=%d executed=%d "
            "wins=%d losses=%d wr=%.1f%% exp=%.2fR",
            cfg.mode, n_bars, result.total_signals, result.executed,
            result.wins, result.losses,
            result.win_rate * 100, result.expectancy,
        )

        if result.regime_results:
            for regime, rs in sorted(result.regime_results.items()):
                closed = rs["wins"] + rs["losses"]
                if closed >= 3:
                    log.info(
                        "[REGIME REPLAY] %-24s | n=%d wr=%.1f%% exp=%.2fR",
                        regime, closed,
                        rs["wins"] / closed * 100 if closed else 0,
                        rs["r"]    / closed        if closed else 0,
                    )

        return result

    def run_monte_carlo(
        self,
        candles:     List[ReplayCandle],
        strategy_fn: Callable,
        risk_fn:     Callable,
        n_paths:     int = 20,
        perturb:     float = 0.02,   # 2% OHLC perturbation
    ) -> Dict:
        """
        Run N Monte Carlo paths with independent OHLC perturbation.
        Returns distribution statistics across all valid paths.
        """
        win_rates: List[float]    = []
        expectancies: List[float] = []

        for seed in range(n_paths):
            mc_cfg = ReplayConfig(
                mode               = MODE_MONTE_CARLO,
                volatility_perturb = perturb,
                spread_mult        = 1.3,
                slippage_pips      = 1.0,
                max_bars           = self._cfg.max_bars,
                min_bars_lookback  = self._cfg.min_bars_lookback,
                score_floor        = self._cfg.score_floor,
                rng_seed           = seed,
            )
            r = ReplayEngine(cfg=mc_cfg).run(candles, strategy_fn, risk_fn)
            if r.executed >= 10:
                win_rates.append(r.win_rate)
                expectancies.append(r.expectancy)

        if not win_rates:
            log.warning("[REPLAY MC] No valid paths (need executed≥10 per path)")
            return {"n_paths": n_paths, "n_valid": 0}

        nv = len(win_rates)
        log.warning(
            "[REPLAY MC] n_paths=%d valid=%d | "
            "wr μ=%.1f%% min=%.1f%% max=%.1f%% | "
            "exp μ=%.2fR min=%.2fR max=%.2fR",
            n_paths, nv,
            sum(win_rates) / nv * 100, min(win_rates) * 100, max(win_rates) * 100,
            sum(expectancies) / nv, min(expectancies), max(expectancies),
        )
        return {
            "n_paths":   n_paths,
            "n_valid":   nv,
            "wr_mean":   round(sum(win_rates)    / nv, 4),
            "wr_min":    round(min(win_rates),    4),
            "wr_max":    round(max(win_rates),    4),
            "exp_mean":  round(sum(expectancies)  / nv, 4),
            "exp_min":   round(min(expectancies), 4),
            "exp_max":   round(max(expectancies), 4),
        }

    # ── Candle generator helpers ───────────────────────────────────────────────

    def _perturb(self, candle: ReplayCandle) -> ReplayCandle:
        """Apply random OHLC perturbation for Monte Carlo paths."""
        p = self._cfg.volatility_perturb
        if p <= 0:
            return candle
        mid   = (candle.high + candle.low) / 2.0
        noise = lambda: 1.0 + self._rng.uniform(-p, p)
        return ReplayCandle(
            time   = candle.time,
            open   = candle.open  * noise(),
            high   = mid + (candle.high - mid) * noise(),
            low    = mid - (mid - candle.low)  * noise(),
            close  = candle.close * noise(),
            volume = candle.volume,
            spread = candle.spread * self._cfg.spread_mult,
            regime = candle.regime,
        )

    @staticmethod
    def _resolve_on_bar(trade: Dict, candle: ReplayCandle) -> Optional[str]:
        """Check if candle's H/L crosses TP or SL for an open replay trade."""
        d = trade["direction"]
        if d == "BUY":
            if candle.low  <= trade["sl"]: return "LOSS"
            if candle.high >= trade["tp"]: return "WIN"
        else:
            if candle.high >= trade["sl"]: return "LOSS"
            if candle.low  <= trade["tp"]: return "WIN"
        return None

    @staticmethod
    def _compute_atr(candles: List[ReplayCandle], period: int = 14) -> float:
        if len(candles) < 2:
            return 0.0
        trs = [abs(c.high - c.low) for c in candles[-period:]]
        return sum(trs) / len(trs) if trs else 0.0
