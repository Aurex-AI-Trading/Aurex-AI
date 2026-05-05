"""
Aurex AI Signature Strategy — Confluence Engine

Aggregates all module scores into a single 0–115 score and resolves
the trade direction through weighted voting.

Core weight allocation (sums to 100, dynamically adjusted by WeightAdjuster):
  trend        = 25   (TrendResult.strength)
  liquidity    = 20   (SweepResult.strength)
  fvg          = 20   (FVGResult.score)
  fibonacci    = 15   (FibResult.score)
  ema          = 10   (EMA proximity)
  confirmation = 10   (candle pattern)

Bonus factors (on top of core 100):
  order_block  = 0–15  (OBResult.score)
  breakout     = 0–12  (BreakoutResult.score)

Direction resolution (weighted voting):
  Trend:  strength × 2  — primary driver
  Sweep:  strength × 1  — standard confirmation
  FVG:    score    × 0.5 — secondary confirmation

  Direction = side with higher weighted score.
  Tie-break: raw vote count → trend direction → NONE (all signals absent).
  Trend fallback: if weighted vote yields NONE but trend is non-neutral, trend wins.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from aurex_ai.strategy.trend     import TrendResult
from aurex_ai.strategy.liquidity import SweepResult
from aurex_ai.strategy.fvg       import FVGResult
from aurex_ai.strategy.fibonacci  import FibResult
from aurex_ai.core.logger         import get_logger

log = get_logger("strategy.confluence")

# Default weight maximums (must match FactorWeights defaults)
_DEF = {
    "trend": 25, "liquidity": 20, "fvg": 20,
    "fibonacci": 15, "ema": 10, "confirmation": 10,
    "order_block": 15,
}


def _scale(raw: float, raw_max: float, weight: float) -> float:
    """Normalize raw (0–raw_max) to (0–weight) range."""
    if raw_max <= 0:
        return 0.0
    return max(0.0, min(float(weight), raw / raw_max * weight))


@dataclass
class ConfluenceResult:
    direction:          str    # "BUY" | "SELL" | "NONE"
    total_score:        float  # core (0–100) + bonuses (up to 27)
    core_score:         float  # 0–100 (trend+liquidity+fvg+fib+ema+confirmation)
    trend_score:        float
    liquidity_score:    float
    fvg_score:          float
    fib_score:          float
    ema_score:          float
    confirmation_score: float
    ob_score:           float  # 0–15 bonus
    breakout_score:     float  # 0–12 bonus
    structure_score:    float  # 0–25 bonus
    buy_votes:          int
    sell_votes:         int
    reason:             str


# ── Weighted direction resolution ────────────────────────────────────────────

def _resolve_direction(
    trend: TrendResult,
    sweep: SweepResult,
    fvg:   FVGResult,
) -> tuple[str, int, int]:
    """
    Resolve trade direction via weighted scoring.

    Weights:
      Trend  = strength × 2   (primary driver — range 0–50)
      Sweep  = strength × 1   (standard — range 0–20)
      FVG    = score × 0.5    (confirmation — range 0–10)

    Resolution order:
      1. Higher weighted score wins.
      2. Tie → raw vote count.
      3. Still tied → trend direction.
      4. All absent → NONE (no trade signal present).
    """
    buy_w  = 0.0
    sell_w = 0.0
    buy    = 0
    sell   = 0

    # Trend — highest weight (2×)
    if trend.direction == "bullish":
        buy_w += trend.strength * 2.0
        buy   += 1
    elif trend.direction == "bearish":
        sell_w += trend.strength * 2.0
        sell   += 1

    # Sweep — standard weight (1×)
    if sweep.detected:
        if sweep.sweep_type == "buy_side":
            buy_w += sweep.strength
            buy   += 1
        elif sweep.sweep_type == "sell_side":
            sell_w += sweep.strength
            sell   += 1

    # FVG — confirmation weight (0.5×)
    if fvg.present and fvg.active_zone is not None:
        if fvg.active_zone.bullish:
            buy_w += fvg.score * 0.5
            buy   += 1
        else:
            sell_w += fvg.score * 0.5
            sell   += 1

    # No signals at all
    if buy_w == 0.0 and sell_w == 0.0:
        return "NONE", buy, sell

    # Weighted winner
    if buy_w > sell_w:
        return "BUY", buy, sell
    if sell_w > buy_w:
        return "SELL", buy, sell

    # Exact weighted tie — raw vote count
    if buy > sell:
        return "BUY", buy, sell
    if sell > buy:
        return "SELL", buy, sell

    # True deadlock — trend as final tiebreaker
    if trend.direction == "bullish":
        return "BUY", buy, sell
    if trend.direction == "bearish":
        return "SELL", buy, sell

    return "NONE", buy, sell


# ── Public API ────────────────────────────────────────────────────────────────

def combine(
    trend:              TrendResult,
    sweep:              SweepResult,
    fvg:                FVGResult,
    fib:                FibResult,
    ema_score:          float,
    confirmation_score: float,
    ob_score:           float = 0.0,
    ob_direction:       str   = "none",
    breakout_score:     float = 0.0,
    breakout_direction: str   = "none",
    structure_score:    float = 0.0,
    structure_direction:str   = "none",
    weights=None,              # Optional[FactorWeights] — None -> use defaults
    symbol:             str   = "",
) -> ConfluenceResult:
    """
    Combine all module outputs into a single ConfluenceResult.

    Args:
        trend, sweep, fvg, fib:         Strategy module results.
        ema_score:                      0–10 EMA proximity score.
        confirmation_score:             0–10 candle confirmation score.
        ob_score:                       0–15 order block proximity bonus.
        ob_direction:                   "bullish" | "bearish" | "none" (for logging).
        breakout_score:                 0–12 breakout/retest bonus.
        breakout_direction:             "bullish" | "bearish" | "none" (for logging).
        weights:                        FactorWeights instance or None for defaults.
        symbol:                         Instrument name (used in [ANALYSIS] log).

    Returns:
        ConfluenceResult with direction, scores, and structured analysis log.
    """
    direction, buy_votes, sell_votes = _resolve_direction(trend, sweep, fvg)

    # Trend override: weighted vote can only return NONE when all signals are
    # absent (buy_w = sell_w = 0).  If trend is non-neutral, use it directly
    # so a valid directional setup is never silently dropped.
    if direction == "NONE" and trend.direction != "neutral":
        direction = "BUY" if trend.direction == "bullish" else "SELL"

    # Resolve dynamic weights (fall back to defaults if not provided)
    w = _DEF.copy()
    if weights is not None:
        w["trend"]        = weights.trend
        w["liquidity"]    = weights.liquidity
        w["fvg"]          = weights.fvg
        w["fibonacci"]    = weights.fibonacci
        w["ema"]          = weights.ema
        w["confirmation"] = weights.confirmation
        w["order_block"]  = weights.order_block

    # Scale each factor score proportionally to its dynamic weight
    t_score  = _scale(trend.strength,    _DEF["trend"],        w["trend"])
    l_score  = _scale(sweep.strength,    _DEF["liquidity"],    w["liquidity"])
    f_score  = _scale(fvg.score,         _DEF["fvg"],          w["fvg"])
    fi_score = _scale(fib.score,         _DEF["fibonacci"],    w["fibonacci"])
    e_score  = _scale(ema_score,         _DEF["ema"],          w["ema"])
    c_score  = _scale(confirmation_score,_DEF["confirmation"], w["confirmation"])

    core = t_score + l_score + f_score + fi_score + e_score + c_score

    # Bonus factors only count when they agree with the resolved direction.
    # A bearish OB (supply zone) must not inflate a BUY score, and vice versa.
    ob_aligned = (
        (direction == "BUY"  and ob_direction  == "bullish") or
        (direction == "SELL" and ob_direction  == "bearish")
    )
    bo_aligned = (
        (direction == "BUY"  and breakout_direction == "bullish") or
        (direction == "SELL" and breakout_direction == "bearish")
    )
    ob_pts = _scale(ob_score, _DEF["order_block"], w["order_block"]) if ob_aligned else 0.0
    bo_pts = max(0.0, min(12.0, breakout_score)) if bo_aligned else 0.0

    struct_aligned = (
        (direction == "BUY"  and structure_direction in ("buy",  "bullish")) or
        (direction == "SELL" and structure_direction in ("sell", "bearish"))
    )
    struct_pts = max(0.0, min(25.0, structure_score)) if struct_aligned else 0.0

    total  = core + ob_pts + bo_pts + struct_pts

    # ── [ANALYSIS] structured log ─────────────────────────────────────────────
    fvg_label    = "yes" if fvg.present else "no"
    sweep_label  = sweep.sweep_type if sweep.detected else "none"

    # Determine tier label for display (thresholds mirrored from settings.yaml)
    if direction != "NONE":
        if total >= 60:
            tier_label = "-> TRADE (Tier 1)"
        elif total >= 50:
            tier_label = "-> TRADE (Tier 2)"
        elif total >= 35:
            tier_label = "-> TRADE (Tier 3)"
        else:
            tier_label = "-> SKIP"
    else:
        tier_label = "-> SKIP (no consensus)"

    analysis = (
        f"\n[ANALYSIS] {symbol}\n"
        f"  Trend:        {trend.direction:<10}  +{t_score:5.1f} /{w['trend']}\n"
        f"  Liquidity:    {sweep_label:<10}  +{l_score:5.1f} /{w['liquidity']}\n"
        f"  FVG:          {fvg_label:<10}  +{f_score:5.1f} /{w['fvg']}\n"
        f"  Fibonacci:    {fib.impulse_dir:<10}  +{fi_score:5.1f} /{w['fibonacci']}\n"
        f"  EMA:                       +{e_score:5.1f} /{w['ema']}\n"
        f"  Confirmation:              +{c_score:5.1f} /{w['confirmation']}\n"
        f"  ─────────────────────────────────\n"
        f"  Core score:   {core:.1f}\n"
        f"  Order Block:  {ob_direction:<10}  +{ob_pts:5.1f} /{w['order_block']} [BONUS]\n"
        f"  Breakout:     {breakout_direction:<10}  +{bo_pts:5.1f} /12 [BONUS]\n"
        f"  Structure:    {structure_direction:<10}  +{struct_pts:5.1f} /25 [BONUS]\n"
        f"  ─────────────────────────────────\n"
        f"  TOTAL SCORE:  {total:.1f}  {tier_label}\n"
        f"  Votes:        {buy_votes}B / {sell_votes}S   Direction: {direction}"
    )
    log.info(analysis)

    reason = (
        f"dir={direction} score={total:.1f} votes=({buy_votes}B/{sell_votes}S) | "
        f"trend={trend.direction}({t_score:.1f}) "
        f"sweep={sweep_label}({l_score:.1f}) "
        f"fvg={fvg_label}({f_score:.1f}) "
        f"fib={fib.impulse_dir}({fi_score:.1f}) "
        f"ema({e_score:.1f}) conf({c_score:.1f}) "
        f"ob={ob_direction}({ob_pts:.1f}) bo({bo_pts:.1f}) "
        f"struct={structure_direction}({struct_pts:.1f})"
    )

    return ConfluenceResult(
        direction          = direction,
        total_score        = round(total,    1),
        core_score         = round(core,     1),
        trend_score        = round(t_score,  1),
        liquidity_score    = round(l_score,  1),
        fvg_score          = round(f_score,  1),
        fib_score          = round(fi_score, 1),
        ema_score          = round(e_score,  1),
        confirmation_score = round(c_score,  1),
        ob_score           = round(ob_pts,     1),
        breakout_score     = round(bo_pts,     1),
        structure_score    = round(struct_pts, 1),
        buy_votes          = buy_votes,
        sell_votes         = sell_votes,
        reason             = reason,
    )
