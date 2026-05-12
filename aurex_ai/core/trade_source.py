"""
Aurex AI — Trade Source Classification

Defines the canonical TradeSource enum and detection helpers that classify
every trade as AI_AUTO, MANUAL, HYBRID, PAPER_AI, or BACKTEST.

This is the foundation of the trade-attribution architecture.  Every component
that records, analyses, or learns from trades reads the source from here.

Definitions
-----------
  AI_AUTO  — fully autonomous Aurex AI execution (magic=202500, comment=Aurex-Sig).
              Default for all trades produced by the scan_symbol() pipeline.
  MANUAL   — user-opened position in MT5 with no Aurex magic number / comment,
              or a trade that appeared in MT5 without a corresponding signal_id
              in the trade logger.
  HYBRID   — AI-generated signal that was manually confirmed, lot-adjusted, or
              SL/TP-modified by the user before execution.
  PAPER_AI — simulated trade from the Phase 12 shadow engine; never sent to MT5.
              Uses live market data and identical strategy logic.  Stored for
              accelerated AI research without capital risk.
  BACKTEST — trade produced during walk-forward backtesting (dry_run + backtest flag).

Learning Weights  (Phase 12)
-----------------------------
  Live AI trades dominate learning; paper/shadow trades supplement at lower weight;
  manual trades NEVER influence AI weights.

  AI_AUTO  = 1.00 — full weight (production live trades)
  HYBRID   = 0.60 — partial weight (AI signal, human modified)
  BACKTEST = 0.50 — moderate weight (clean simulated fills)
  PAPER_AI = 0.35 — lower weight (live prices, simulated fills)
  REJECTED = 0.15 — minimal weight (hypothetical outcome only)
  MANUAL   = 0.00 — never learned from

Detection Logic
---------------
  1. Magic number (primary):  Aurex magic = 202500.  Any position with a different
     magic number is MANUAL.
  2. Comment (secondary):  Aurex stamps "Aurex-Sig" on all orders.  Positions with
     the Aurex magic but a non-Aurex comment are classified MANUAL (could be another
     EA sharing the same magic — very unlikely but safe to tag).
  3. Signal ID correlation:  Positions whose ticket appears in the trade_logger with
     a populated signal_id → AI_AUTO.  Tickets not in the trade_logger → MANUAL.
  4. HYBRID path:  Set explicitly by the caller when a human modifies an AI signal
     before execution.  Not auto-detected (requires explicit operator action).
  5. PAPER_AI path:  Set explicitly by the shadow engine; stored in paper_trades
     table, never in the live trades table.

Learning Isolation
------------------
  ONLY AI_AUTO, HYBRID, and PAPER_AI trades feed:
    - LearningEngine.record_outcome() (PAPER_AI at reduced weight)
    - PerformanceTracker.record_trade()
    - AdaptiveLearning.compute() windows
    - WeightAdjuster drift tracking

  MANUAL trades are recorded to trade_logger for audit and performance comparison
  but are NEVER used to train the AI system.

Tags emitted
------------
  [TRADE SOURCE]  [MANUAL DETECTED]  [AI AUTO]  [PAPER TRADE]
  [LEARNING BLOCKED — MANUAL]
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, Optional


# ── Canonical magic number emitted on every Aurex order ───────────────────────
AUREX_MAGIC: int = 202500

# ── Comment prefixes stamped by the execution layer ───────────────────────────
_AUREX_COMMENT_PREFIXES = ("aurex-sig", "aurex-close", "aurex-partialtp", "aurex")


class TradeSource(Enum):
    """Source classification for every trade record."""
    AI_AUTO  = "AI_AUTO"    # fully autonomous Aurex execution
    MANUAL   = "MANUAL"     # user-opened / external EA
    HYBRID   = "HYBRID"     # AI signal + manual modification
    PAPER_AI = "PAPER_AI"   # shadow engine simulated trade (Phase 12)
    BACKTEST = "BACKTEST"   # walk-forward backtest mode

    def is_learnable(self) -> bool:
        return self in (TradeSource.AI_AUTO, TradeSource.HYBRID, TradeSource.PAPER_AI)

    def learning_weight(self) -> float:
        return LEARNING_WEIGHTS.get(self.value, 0.0)

    def label(self) -> str:
        return self.value


# ── Convenience string constants (avoids .value everywhere) ───────────────────
AI_AUTO  = TradeSource.AI_AUTO.value
MANUAL   = TradeSource.MANUAL.value
HYBRID   = TradeSource.HYBRID.value
PAPER_AI = TradeSource.PAPER_AI.value
BACKTEST = TradeSource.BACKTEST.value

ALL_SOURCES: tuple = (AI_AUTO, MANUAL, HYBRID, PAPER_AI, BACKTEST)

# Primary live sources — feed full-weight learning
LIVE_LEARNABLE_SOURCES: frozenset = frozenset({AI_AUTO, HYBRID})

# All sources that contribute to learning (at varying weights)
LEARNABLE_SOURCES: frozenset = frozenset({AI_AUTO, HYBRID, PAPER_AI})

# ── Phase 12: Tiered learning weights per source ──────────────────────────────
# Live AI trades dominate; paper/shadow supplement at reduced weight.
# MANUAL = 0.00 — never learned from under any circumstances.
LEARNING_WEIGHTS: Dict[str, float] = {
    AI_AUTO:    1.00,
    HYBRID:     0.60,
    BACKTEST:   0.50,
    PAPER_AI:   0.35,
    "REJECTED": 0.15,   # hypothetical outcome signals (SignalStore)
    MANUAL:     0.00,
}


# ── Detection helpers ─────────────────────────────────────────────────────────

def classify_from_mt5_position(
    magic:   int,
    comment: str  = "",
    *,
    aurex_magic: int = AUREX_MAGIC,
) -> str:
    """
    Classify a position as AI_AUTO or MANUAL from its MT5 metadata.

    Called when scanning all MT5 positions to detect trades not opened by Aurex.

    Args:
        magic:       MT5 position magic number.
        comment:     MT5 order comment string.
        aurex_magic: Expected magic for Aurex orders (default 202500).

    Returns:
        TradeSource string ("AI_AUTO" or "MANUAL").
    """
    if magic != aurex_magic:
        return MANUAL

    comment_lower = (comment or "").lower().strip()
    if any(comment_lower.startswith(pfx) for pfx in _AUREX_COMMENT_PREFIXES):
        return AI_AUTO

    # Matching magic but unrecognised comment — tag as MANUAL to be safe.
    return MANUAL


def classify_from_signal_id(signal_id: Optional[str]) -> str:
    """
    Classify a historical trade from its signal_id.

    Aurex always sets a non-empty signal_id of the form "{SYMBOL}-{scan_num}".
    Trades without a signal_id were not generated by the pipeline.

    Args:
        signal_id: The signal_id column value from the trade logger.

    Returns:
        "AI_AUTO" if signal_id is populated, "MANUAL" otherwise.
    """
    if signal_id and signal_id.strip():
        return AI_AUTO
    return MANUAL


def is_learnable_source(source: str) -> bool:
    """Return True if the source string allows learning engine ingestion."""
    return source in LEARNABLE_SOURCES


def get_learning_weight(source: str) -> float:
    """Return the learning weight (0.0–1.0) for a given source string."""
    return LEARNING_WEIGHTS.get(source, 0.0)


def source_label(source: str) -> str:
    """Return a human-readable label for use in log tags."""
    return {
        AI_AUTO:  "[AI AUTO]",
        MANUAL:   "[MANUAL]",
        HYBRID:   "[HYBRID]",
        PAPER_AI: "[PAPER AI]",
        BACKTEST: "[BACKTEST]",
    }.get(source, f"[{source}]")
