"""
Aurex AI Signature Strategy — Decision Engine

Makes the final EXECUTE / CONDITIONAL / TIER3 / SKIP decision from the
confluence score, emitting a structured log block.

Three-tier decision system:
  score >= tier1_thresh (default 75) -> EXECUTE   (1.00× lot)   Tier 1 A+
  score >= tier2_thresh (default 60) -> CONDITIONAL (0.50× lot)  Tier 2 A
  score >= tier3_thresh (default 50) -> TIER3     (0.25× lot)   Tier 3 B
  score <  tier3_thresh              -> SKIP

NONE direction always maps to SKIP regardless of score.

Log output:
  [DECISION ENGINE]
  symbol=EURUSD  direction=BUY  score=82.0
  trend=22.5  liquidity=18.0  fvg=15.0  fib=12.0  ema=8.0  confirmation=9.0
  ob=12.0  breakout=9.0
  votes=3B/0S  tier=1  decision=EXECUTE
  reason=score=82.0 >= tier1=75
"""
from __future__ import annotations

from dataclasses import dataclass

from aurex_ai.strategy.confluence import ConfluenceResult
from aurex_ai.core.logger import get_logger

log = get_logger("execution.decision")


@dataclass
class Decision:
    action:     str    # "EXECUTE" | "CONDITIONAL" | "TIER3" | "SKIP"
    direction:  str    # "BUY" | "SELL" | "NONE"
    score:      float  # confluence score 0-115
    tier:       int    # 1, 2, 3, or 0 for SKIP
    size_mult:  float  # 1.0 / 0.5 / 0.25 / 0.0
    reason:     str
    confidence: float = 0.0   # confidence engine score 0-100 (set by caller)


# ── Public API ────────────────────────────────────────────────────────────────

def decide(
    symbol:       str,
    confluence:   ConfluenceResult,
    exec_thresh:  int = 75,
    cond_thresh:  int = 60,
    tier3_thresh: int = 50,
) -> Decision:
    """
    Evaluate the confluence result and return an actionable Decision.

    Args:
        symbol:       Instrument name (for logging only).
        confluence:   Output of strategy.confluence.combine().
        exec_thresh:  Score for full-size execution (Tier 1).
        cond_thresh:  Score for half-size execution (Tier 2).
        tier3_thresh: Score for quarter-size fallback execution (Tier 3).

    Returns:
        Decision with action, direction, tier, size multiplier, and reason.
    """
    score     = confluence.total_score
    direction = confluence.direction

    if direction == "NONE":
        _log_decision(symbol, confluence, "SKIP", 0, "no_directional_consensus")
        return Decision(
            action="SKIP", direction="NONE",
            score=score, tier=0, size_mult=0.0,
            reason="no_directional_consensus",
        )

    if score >= exec_thresh:
        action, tier, size_mult = "EXECUTE",     1, 1.00
        reason = f"score={score:.1f} >= tier1={exec_thresh}"
    elif score >= cond_thresh:
        action, tier, size_mult = "CONDITIONAL", 2, 0.50
        reason = f"score={score:.1f} in tier2 [{cond_thresh}–{exec_thresh})"
    elif score >= tier3_thresh:
        action, tier, size_mult = "TIER3",       3, 0.25
        reason = f"score={score:.1f} in tier3 [{tier3_thresh}–{cond_thresh})"
    else:
        action, tier, size_mult = "SKIP",        0, 0.00
        reason = f"score={score:.1f} < tier3={tier3_thresh}"

    _log_decision(symbol, confluence, action, tier, reason)

    return Decision(
        action    = action,
        direction = direction,
        score     = score,
        tier      = tier,
        size_mult = size_mult,
        reason    = reason,
    )


# ── Structured log block ──────────────────────────────────────────────────────

def _log_decision(
    symbol:    str,
    c:         ConfluenceResult,
    action:    str,
    tier:      int,
    reason:    str,
) -> None:
    block = (
        "\n[DECISION ENGINE]\n"
        f"  symbol={symbol}  direction={c.direction}  score={c.total_score:.1f}\n"
        f"  trend={c.trend_score:.1f}  liquidity={c.liquidity_score:.1f}  "
        f"fvg={c.fvg_score:.1f}  fib={c.fib_score:.1f}  "
        f"ema={c.ema_score:.1f}  confirmation={c.confirmation_score:.1f}\n"
        f"  ob={c.ob_score:.1f}  breakout={c.breakout_score:.1f}\n"
        f"  votes={c.buy_votes}B/{c.sell_votes}S  tier={tier}  decision={action}\n"
        f"  reason={reason}"
    )

    if action == "EXECUTE":
        log.warning(block, extra={
            "event": "DECISION", "symbol": symbol,
            "direction": c.direction, "score": c.total_score,
            "tier": tier, "action": action,
        })
    elif action in ("CONDITIONAL", "TIER3"):
        log.info(block, extra={
            "event": "DECISION", "symbol": symbol,
            "action": action, "score": c.total_score,
            "direction": c.direction, "tier": tier,
        })
    else:
        log.info(
            "[DECISION ENGINE] symbol=%s direction=%s score=%.1f tier=%d decision=SKIP",
            symbol, c.direction, c.total_score, tier,
        )
        log.debug(block)
