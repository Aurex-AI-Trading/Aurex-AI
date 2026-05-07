"""
Aurex AI Signature Strategy — Trade Executor

Bridges the decision + risk layers to the MT5 bridge.

Responsibilities:
  - Accept a validated Decision and RiskResult
  - Submit the order via MT5Bridge (or simulate in DRY_RUN)
  - Return a structured TradeResult
  - Log the full trade record at WARNING level for auditability

DRY_RUN mode:
  No orders reach MT5.  The bridge returns a synthetic ticket so the rest of
  the pipeline (cooldown arming, analytics, Telegram alerts) runs identically
  to live mode.  This is intentional: the only difference between DRY_RUN and
  LIVE is whether MT5 receives the order.

Error handling:
  Exceptions from the bridge are caught and wrapped in TradeResult(success=False).
  The caller (main.py) decides whether to log, skip, or halt.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from aurex_ai.core.mt5_bridge import MT5Bridge, OrderResult, get_mt5_time
from aurex_ai.execution.decision_engine import Decision
from aurex_ai.execution.risk_manager import RiskResult
from aurex_ai.core.logger import get_logger

log = get_logger("execution.executor")

# Hard symbol whitelist — only symbols declared in settings.yaml reach MT5.
# Populated at startup by _load_allowed_symbols(); never mutated after that.
# Includes both bare and suffixed forms so validation passes regardless of
# how callers format the symbol (EURUSD or EURUSD.Z both allowed).
ALLOWED_SYMBOLS: frozenset = frozenset()   # replaced by _load_allowed_symbols()


def _load_allowed_symbols(cfg=None) -> frozenset:
    """
    Build the executor whitelist from config.

    Reads cfg.symbols (list of broker symbols, e.g. ["EURUSD.Z", ...]) and
    cfg.broker_suffix (e.g. ".Z") to construct a frozenset that accepts both
    bare and suffixed forms.  Falls back to hardcoded HFM Premium defaults if
    cfg is not supplied.

    Call once at startup:
        from aurex_ai.execution.trade_executor import _load_allowed_symbols
        _load_allowed_symbols(cfg)
    """
    global ALLOWED_SYMBOLS
    from aurex_ai.core.symbol_mapper import expand_allowed_set, BASE_SYMBOLS
    try:
        broker_symbols = list(cfg.symbols) if cfg else []
        suffix = str(getattr(cfg, "broker_suffix", ".Z") or "") if cfg else ".Z"
    except Exception:
        broker_symbols = []
        suffix = ".Z"

    if broker_symbols:
        from aurex_ai.core.symbol_mapper import get_base_symbols
        bases = get_base_symbols(broker_symbols)
    else:
        bases = BASE_SYMBOLS

    ALLOWED_SYMBOLS = expand_allowed_set(bases, suffix)
    log.info(
        "[EXECUTOR] Allowed symbols whitelist: %s",
        sorted(ALLOWED_SYMBOLS),
    )
    return ALLOWED_SYMBOLS


@dataclass
class TradeResult:
    success:         bool
    ticket:          int
    symbol:          str
    direction:       str
    executed_price:  float
    stop_loss:       float
    take_profit:     float
    lot_size:        float
    sl_pips:         float
    rr_ratio:        float
    risk_amount:     float
    dry_run:         bool
    signal_id:       str
    opened_at:       str   # ISO-8601 UTC
    error:           str = ""

    @classmethod
    def failed(
        cls, symbol: str, direction: str, error: str, dry_run: bool, signal_id: str = ""
    ) -> "TradeResult":
        return cls(
            success=False, ticket=0, symbol=symbol, direction=direction,
            executed_price=0.0, stop_loss=0.0, take_profit=0.0, lot_size=0.0,
            sl_pips=0.0, rr_ratio=0.0, risk_amount=0.0,
            dry_run=dry_run, signal_id=signal_id,
            opened_at=get_mt5_time().isoformat(),
            error=error,
        )


# ── Public API ────────────────────────────────────────────────────────────────

async def execute(
    symbol:     str,
    decision:   Decision,
    risk:       RiskResult,
    bridge:     MT5Bridge,
    signal_id:  str               = "",
    comment:    str               = "Aurex-Sig",
    now_utc:    Optional[datetime] = None,
) -> TradeResult:
    """
    Submit a trade to MT5 (or simulate in DRY_RUN).

    Args:
        symbol:    Instrument name.
        decision:  Validated Decision (action must be EXECUTE or CONDITIONAL).
        risk:      RiskResult with SL, TP, and lot size.
        bridge:    Initialised MT5Bridge.
        signal_id: Optional unique ID for audit logging.
        comment:   MT5 order comment (max 31 chars).

    Returns:
        TradeResult.  Check .success before acting on the ticket.
    """
    if decision.action == "SKIP" or not risk.allowed:
        return TradeResult.failed(
            symbol, decision.direction,
            error=f"not_executable: action={decision.action} risk_allowed={risk.allowed}",
            dry_run=bridge.dry_run, signal_id=signal_id,
        )

    # Hard symbol whitelist — only symbols declared in settings.yaml.
    # Accepts both bare (EURUSD) and broker-suffixed (EURUSD.Z) forms.
    # Any other symbol is a misconfiguration or injection; block unconditionally.
    if symbol.upper() not in ALLOWED_SYMBOLS:
        log.error(
            "[EXEC BLOCKED] %s — symbol not in whitelist %s (config or pipeline error)",
            symbol, sorted(ALLOWED_SYMBOLS),
        )
        return TradeResult.failed(
            symbol, decision.direction,
            error=f"symbol_not_allowed: {symbol} not in {sorted(ALLOWED_SYMBOLS)}",
            dry_run=bridge.dry_run, signal_id=signal_id,
        )

    # Hard guard: confidence must be set by the caller before execute() is called.
    # decision.confidence defaults to 0.0, so 0 here means the pipeline forgot to
    # populate it — a logic error we never want silently reaching MT5.
    if decision.confidence <= 0:
        log.error(
            "[EXEC BLOCKED] %s %s — Decision.confidence=%.1f (pipeline bug: set before calling execute)",
            symbol, decision.direction, decision.confidence,
        )
        return TradeResult.failed(
            symbol, decision.direction,
            error=f"confidence_not_set: confidence={decision.confidence}",
            dry_run=bridge.dry_run, signal_id=signal_id,
        )

    try:
        order: OrderResult = await asyncio.to_thread(
            bridge.execute_order,
            symbol      = symbol,
            direction   = decision.direction,
            lot_size    = risk.lot_size,
            stop_loss   = risk.stop_loss,
            take_profit = risk.take_profit,
            comment     = comment[:31],
        )
    except Exception as exc:
        log.error("execute: unexpected exception for %s %s: %s",
                  symbol, decision.direction, exc, exc_info=True)
        return TradeResult.failed(
            symbol, decision.direction, str(exc), bridge.dry_run, signal_id,
        )

    if not order.success:
        log.error(
            "execute: order rejected %s %s | retcode=%d error=%s",
            symbol, decision.direction, order.retcode, order.error,
        )
        return TradeResult.failed(
            symbol, decision.direction,
            f"retcode={order.retcode} {order.error}",
            bridge.dry_run, signal_id,
        )

    opened_at = (now_utc or get_mt5_time()).isoformat()
    mode      = "DRY_RUN" if bridge.dry_run else "LIVE"

    log.warning(
        "TRADE OPENED (%s) | %s %s | ticket=%d lots=%.2f "
        "entry=%.5f sl=%.5f tp=%.5f rr=%.2fR risk=$%.2f",
        mode, symbol, decision.direction,
        order.ticket, risk.lot_size,
        risk.entry, risk.stop_loss, risk.take_profit,
        risk.rr_ratio, risk.risk_amount,
        extra={
            "event":        "TRADE_OPENED",
            "mode":         mode,
            "symbol":       symbol,
            "direction":    decision.direction,
            "ticket":       order.ticket,
            "lots":         risk.lot_size,
            "entry":        risk.entry,
            "sl":           risk.stop_loss,
            "tp":           risk.take_profit,
            "rr":           risk.rr_ratio,
            "risk_usd":     risk.risk_amount,
            "score":        decision.score,
            "signal_id":    signal_id,
            "opened_at":    opened_at,
            "dry_run":      bridge.dry_run,
        },
    )

    return TradeResult(
        success        = True,
        ticket         = order.ticket,
        symbol         = symbol,
        direction      = decision.direction,
        executed_price = order.executed_price or risk.entry,
        stop_loss      = risk.stop_loss,
        take_profit    = risk.take_profit,
        lot_size       = risk.lot_size,
        sl_pips        = risk.sl_pips,
        rr_ratio       = risk.rr_ratio,
        risk_amount    = risk.risk_amount,
        dry_run        = bridge.dry_run,
        signal_id      = signal_id,
        opened_at      = opened_at,
    )
