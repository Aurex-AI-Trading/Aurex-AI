"""
Aurex AI Signature Strategy — Decision Engine  (Phase 10: Strategy Optimization)

Makes the final EXECUTE / CONDITIONAL / TIER3 / SKIP decision from the
confluence score, emitting a structured log block.

Three-tier decision system (Phase 10 thresholds):
  score >= tier1_thresh (default 80) -> EXECUTE     (1.00× lot)   Tier 1 A+
  score >= tier2_thresh (default 70) -> CONDITIONAL  (0.50× lot)  Tier 2 A
  score >= tier3_thresh (default 55) -> TIER3        (0.25× lot)  Tier 3 B
  score <  tier3_thresh              -> SKIP

NONE direction always maps to SKIP regardless of score.

Phase 10 minimum vote gate:
  Tier 1 requires ≥ 2 directional votes (trend + at least one other factor).
  Tier 2 requires ≥ 1 directional vote.
  A single lonely vote (e.g. only trend, nothing else) cannot produce a Tier-1 EXECUTE.
  This prevents single-factor setups from inflating past high-quality thresholds.

Log output:
  [DECISION ENGINE]
  symbol=EURUSD.Z  direction=BUY  score=82.0
  trend=22.5  liquidity=18.0  fvg=15.0  fib=12.0  ema=8.0  confirmation=9.0
  ob=12.0  breakout=4.0  votes=3B/0S  tier=1  decision=EXECUTE
  reason=score=82.0 >= tier1=80 votes=3
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
    symbol:             str,
    confluence:         ConfluenceResult,
    exec_thresh:        int = 80,
    cond_thresh:        int = 70,
    tier3_thresh:       int = 55,
    min_votes_tier1:    int = 2,
    min_votes_tier2:    int = 1,
) -> Decision:
    """
    Evaluate the confluence result and return an actionable Decision.

    Phase 10 additions:
    - Default thresholds raised (exec=80, cond=70, tier3=55).
    - Minimum vote gate: Tier 1 requires ≥ min_votes_tier1 directional votes.
      Prevents single-factor setups from reaching full-size execution.
    - Minimum vote for Tier 2: ≥ min_votes_tier2 vote required.

    Args:
        symbol:          Instrument name (for logging only).
        confluence:      Output of strategy.confluence.combine().
        exec_thresh:     Score for full-size execution (Tier 1).
        cond_thresh:     Score for half-size execution (Tier 2).
        tier3_thresh:    Score for quarter-size fallback execution (Tier 3).
        min_votes_tier1: Minimum directional votes for Tier 1 (default 2).
        min_votes_tier2: Minimum directional votes for Tier 2 (default 1).

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

    # Resolve the vote count for the winning direction
    winning_votes = confluence.buy_votes if direction == "BUY" else confluence.sell_votes

    if score >= exec_thresh:
        # Phase 10: minimum vote gate for Tier 1
        if winning_votes < min_votes_tier1:
            action, tier, size_mult = "CONDITIONAL", 2, 0.50
            reason = (
                f"score={score:.1f}>={exec_thresh} BUT votes={winning_votes}<{min_votes_tier1} "
                f"→ demoted_to_tier2 [SINGLE_FACTOR_GUARD]"
            )
            log.warning(
                "[DECISION ENGINE] %s TIER1→TIER2 demotion: score=%.1f qualifies but "
                "only %d/%d votes — single-factor setups blocked from Tier 1",
                symbol, score, winning_votes, min_votes_tier1,
            )
        else:
            action, tier, size_mult = "EXECUTE",     1, 1.00
            reason = f"score={score:.1f} >= tier1={exec_thresh} votes={winning_votes}"

    elif score >= cond_thresh:
        # Phase 10: minimum vote gate for Tier 2
        if winning_votes < min_votes_tier2:
            action, tier, size_mult = "SKIP", 0, 0.00
            reason = (
                f"score={score:.1f} in tier2 but votes={winning_votes}<{min_votes_tier2} "
                f"→ SKIP [NO_VOTE_GUARD]"
            )
        else:
            action, tier, size_mult = "CONDITIONAL", 2, 0.50
            reason = f"score={score:.1f} in tier2 [{cond_thresh}–{exec_thresh}) votes={winning_votes}"

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
