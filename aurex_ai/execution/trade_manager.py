"""
Aurex AI — Trade Manager

Post-trade management: break-even SL, partial take-profit, and dynamic
trailing stop.  Runs at the end of each scan cycle in run_live().

Rules:
  At +1R profit:
    1. Close partial_tp_ratio of the position (default 50%)
    2. Move SL to entry_price (break-even)          → [TRAIL] BE activated

  At +1.5R profit (requires BE to be set first):
    Trail SL = current_price − ATR(14) × 1.0 (BUY)
               current_price + ATR(14) × 1.0 (SELL)
    Fires every scan cycle; SL only ever moves in the favorable direction.
                                                     → [TRAIL] SL moved to X

  At +2R profit:
    Trail SL = current_price − ATR(14) × 0.5 (BUY)  (tighter)
               current_price + ATR(14) × 0.5 (SELL)
                                                     → [TRAIL] SL moved to X

All actions execute at most once per ticket per direction (BE/partial).
Trailing fires every cycle as long as profit >= threshold and the new
SL level improves on the existing one.

State is in-memory only; resets on process restart (conservative: positions
opened in a previous session will not be re-managed).
"""
from __future__ import annotations

from typing import Dict, List, Set

from aurex_ai.core.logger import get_logger

log = get_logger("execution.trade_manager")

# M15 timeframe integer — matches data_feed.TF_M15
_TF_M15 = 15


def _ema_simple(values: List[float], period: int) -> List[float]:
    """Compute EMA without importing strategy modules (avoids circular imports)."""
    if len(values) < period:
        return []
    k   = 2.0 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


class TradeManager:
    """
    Stateful trade manager.  Instantiate once in run_live() and call
    manage() after every scan cycle.

    Phase 5 additions:
      - Runner momentum monitoring: detects EMA9 slope reversal and ATR exhaustion
        on winning runners.  Switches to ultra-tight trail (0.30×) when decaying.
      - [RUNNER ACTIVE] tag when trail fires normally.
      - [MOMENTUM WEAKENING] / [TREND EXHAUSTION] / [SMART EXIT] when decay detected.
      - [BREAKEVEN ACTIVE] / [PROFIT LOCKED] on first BE application.
    """

    def __init__(self) -> None:
        self._be_done:       Set[int]        = set()   # tickets where BE SL was applied
        self._partial_done:  Set[int]        = set()   # tickets where partial TP was executed
        self._original_risk: Dict[int, float] = {}     # original SL distance per ticket
        self._momentum_weak: Set[int]        = set()   # tickets with weakening momentum

    # ── Main entry point ──────────────────────────────────────────────────────

    def manage(self, bridge: object, cfg: object) -> None:
        """
        Check all open positions and apply BE / partial-TP / trail where due.
        Safe to call every scan cycle — BE/partial fire once; trail fires
        every cycle as long as price keeps improving.
        """
        tm              = getattr(cfg, "trade_management", None)
        be_enabled      = bool(getattr(tm, "breakeven_enabled",  True))
        partial_enabled = bool(getattr(tm, "partial_tp_enabled", True))
        partial_ratio   = float(getattr(tm, "partial_tp_ratio",  0.5))
        be_rr           = float(getattr(tm, "breakeven_rr",      1.0))
        trail_1r5_rr    = float(getattr(tm, "trail_1r5_rr",      1.5))
        trail_2r_rr     = float(getattr(tm, "trail_2r_rr",       2.0))
        trail_mult_1    = float(getattr(tm, "trail_atr_mult_1",  1.0))
        trail_mult_2    = float(getattr(tm, "trail_atr_mult_2",  0.4))
        # Phase 5: ultra-tight trail when momentum is decaying
        momentum_trail  = float(getattr(tm, "momentum_decay_trail_mult", 0.30))
        momentum_check  = bool(getattr(tm, "momentum_check_enabled",     True))

        if not be_enabled and not partial_enabled:
            return

        positions = bridge.get_open_positions()
        if not positions:
            return

        for pos in positions:
            ticket    = pos["ticket"]
            symbol    = pos["symbol"]
            direction = pos["type"]      # "BUY" | "SELL"
            entry     = pos["price_open"]
            sl        = pos["sl"]
            tp        = pos["tp"]

            if sl == 0.0:
                continue   # no SL set — cannot compute 1R

            raw_risk = abs(entry - sl)

            # Store original risk distance on first encounter (before BE moves SL
            # to entry and makes raw_risk zero on subsequent cycles).
            if raw_risk > 0 and ticket not in self._original_risk:
                self._original_risk[ticket] = raw_risk

            risk_dist = self._original_risk.get(ticket, raw_risk)
            if risk_dist <= 0:
                continue

            # Current price (conservative side for each direction)
            try:
                bid, ask = bridge.get_tick_raw(symbol)
                current  = bid if direction == "BUY" else ask
            except Exception as exc:
                log.debug("[TRADE MGMT] %s: tick error — %s", symbol, exc)
                continue

            profit_dist = (current - entry) if direction == "BUY" else (entry - current)
            profit_r    = profit_dist / risk_dist if risk_dist > 0 else 0.0

            # ── +1R: partial TP + break-even ─────────────────────────────────
            if profit_dist >= risk_dist * be_rr:
                log.debug(
                    "[TRADE MGMT CHECK] %s ticket=%d profit_r=%.2fR trigger=%.1fR",
                    symbol, ticket, profit_r, be_rr,
                )

                if partial_enabled and ticket not in self._partial_done:
                    self._do_partial_close(bridge, pos, partial_ratio)

                if be_enabled and ticket not in self._be_done:
                    self._do_breakeven(bridge, pos)

            # ── +1.5R / +2R: dynamic trailing stop ───────────────────────────
            # Trailing requires BE to be active — SL must not still be below entry.
            if ticket in self._be_done and profit_dist >= risk_dist * trail_1r5_rr:
                atr_mult = trail_mult_2 if profit_dist >= risk_dist * trail_2r_rr else trail_mult_1

                # Phase 5: Runner momentum check — detect EMA slope reversal / ATR exhaustion.
                # Switch to ultra-tight trail when momentum decays to lock in profits early.
                if momentum_check:
                    _weak, _weak_reason = self._check_runner_momentum(
                        bridge, symbol, direction,
                    )
                    if _weak:
                        atr_mult = momentum_trail  # 0.30× → ultra-tight trail
                        if ticket not in self._momentum_weak:
                            self._momentum_weak.add(ticket)
                            log.warning(
                                "[MOMENTUM WEAKENING] %s ticket=%d profit=%.2fR | %s "
                                "→ trail=%.2f× [SMART EXIT] [REVERSAL RISK]",
                                symbol, ticket, profit_r, _weak_reason, atr_mult,
                            )
                    else:
                        if ticket in self._momentum_weak:
                            self._momentum_weak.discard(ticket)
                            log.info(
                                "[RUNNER ACTIVE] %s ticket=%d profit=%.2fR | momentum "
                                "recovering [CONTINUATION BIAS]",
                                symbol, ticket, profit_r,
                            )
                        else:
                            log.info(
                                "[RUNNER ACTIVE] %s ticket=%d profit=%.2fR | "
                                "momentum intact trail=%.2f× [PROFIT LOCKED]",
                                symbol, ticket, profit_r, atr_mult,
                            )

                self._do_trail(bridge, pos, current, atr_mult)

    # ── Break-even ────────────────────────────────────────────────────────────

    def _do_breakeven(self, bridge: object, pos: dict) -> None:
        ticket    = pos["ticket"]
        symbol    = pos["symbol"]
        entry     = pos["price_open"]
        direction = pos["type"]
        sl        = pos["sl"]
        tp        = pos["tp"]

        # Guard: never move SL in the wrong direction
        if direction == "BUY"  and sl >= entry:
            self._be_done.add(ticket)
            return
        if direction == "SELL" and sl <= entry:
            self._be_done.add(ticket)
            return

        ok = bridge.modify_sl(ticket, new_sl=entry, new_tp=tp)
        if ok:
            self._be_done.add(ticket)
            log.warning(
                "[BREAKEVEN ACTIVE] [PROFIT LOCKED] %s ticket=%d sl=%.5f → entry=%.5f "
                "| risk eliminated [PARTIAL TP]",
                symbol, ticket, sl, entry,
            )
        else:
            log.error("[TRAIL] BE failed | %s ticket=%d — will retry next cycle", symbol, ticket)

    # ── Dynamic trailing stop ─────────────────────────────────────────────────

    def _do_trail(
        self,
        bridge:    object,
        pos:       dict,
        current:   float,
        atr_mult:  float,
    ) -> None:
        """
        Compute candidate trail SL = current ± ATR × atr_mult and move SL
        only if the candidate improves the existing level.
        """
        ticket    = pos["ticket"]
        symbol    = pos["symbol"]
        direction = pos["type"]
        sl        = pos["sl"]
        tp        = pos["tp"]

        atr = self._get_atr(bridge, symbol)
        if atr <= 0:
            log.debug("[TRAIL] %s ticket=%d — ATR unavailable, skipping", symbol, ticket)
            return

        trail_dist   = atr * atr_mult
        if direction == "BUY":
            candidate_sl = round(current - trail_dist, 5)
            if candidate_sl <= sl:
                return   # trail would not improve current SL
        else:
            candidate_sl = round(current + trail_dist, 5)
            if candidate_sl >= sl:
                return   # trail would not improve current SL

        ok = bridge.modify_sl(ticket, new_sl=candidate_sl, new_tp=tp)
        if ok:
            log.warning(
                "[TRAIL] SL moved to %.5f | %s ticket=%d dir=%s current=%.5f atr=%.5f mult=%.1f",
                candidate_sl, symbol, ticket, direction, current, atr, atr_mult,
            )
        else:
            log.error("[TRAIL] SL move failed | %s ticket=%d", symbol, ticket)

    # ── ATR helper ────────────────────────────────────────────────────────────

    def _get_atr(self, bridge: object, symbol: str, period: int = 14) -> float:
        """Compute ATR(period) from fresh M15 candles via the bridge."""
        try:
            raw = bridge.get_candles_raw(symbol, _TF_M15, period + 2)
        except Exception as exc:
            log.debug("[TRAIL] %s ATR fetch error: %s", symbol, exc)
            return 0.0

        if len(raw) < period + 1:
            return 0.0

        trs = []
        for i in range(-period, 0):
            c          = raw[i]
            prev_close = raw[i - 1]["close"]
            trs.append(max(
                c["high"] - c["low"],
                abs(c["high"] - prev_close),
                abs(c["low"]  - prev_close),
            ))
        return sum(trs) / len(trs)

    # ── Partial take-profit ───────────────────────────────────────────────────

    def _do_partial_close(self, bridge: object, pos: dict, ratio: float) -> None:
        ticket = pos["ticket"]
        symbol = pos["symbol"]
        volume = pos["volume"]

        try:
            info      = bridge.get_symbol_info(symbol)
            vol_min   = float(info.get("volume_min",  0.01))
            vol_step  = float(info.get("volume_step", 0.01))
        except Exception:
            vol_min  = 0.01
            vol_step = 0.01

        # Snap to nearest valid step
        raw          = volume * ratio
        vol_to_close = round(round(raw / vol_step) * vol_step, 2)
        vol_to_close = max(vol_min, vol_to_close)

        # Ensure remaining volume stays at or above min lot
        if vol_to_close >= volume or (volume - vol_to_close) < vol_min:
            log.debug(
                "[PARTIAL TP] %s ticket=%d — skipping (would leave %.2f lots, min=%.2f)",
                symbol, ticket, volume - vol_to_close, vol_min,
            )
            self._partial_done.add(ticket)
            return

        ok = bridge.partial_close(ticket, vol_to_close)
        if ok:
            self._partial_done.add(ticket)
            log.warning(
                "[PARTIAL TP OK] %s ticket=%d closed %.0f%% (%.2f lots of %.2f)",
                symbol, ticket, ratio * 100, vol_to_close, volume,
            )
        else:
            log.error("[PARTIAL TP FAILED] %s ticket=%d — will retry next cycle", symbol, ticket)

    # ── Runner momentum detection (Phase 5) ───────────────────────────────────

    def _check_runner_momentum(
        self,
        bridge:    object,
        symbol:    str,
        direction: str,
    ) -> tuple[bool, str]:
        """
        Detect momentum decay in a running winner.

        Algorithm:
          1. EMA9 slope: compute EMA9 over last 12 M15 closes. If the 3-bar slope
             is flattening or reversing against the trade direction → weakening.
          2. ATR compression: if average range of last 3 bars is < 55% of the
             average range 5-8 bars ago → momentum exhausting.

        Returns:
          (is_weakening: bool, reason: str)
        """
        try:
            raw = bridge.get_candles_raw(symbol, _TF_M15, 14)
        except Exception as exc:
            log.debug("[MOMENTUM] %s candle fetch error: %s", symbol, exc)
            return False, "data_error"

        if len(raw) < 10:
            return False, "insufficient_data"

        closes = [c["close"] for c in raw]
        highs  = [c["high"]  for c in raw]
        lows   = [c["low"]   for c in raw]

        # EMA9 slope check — 3-bar slope of the EMA9 line
        ema9 = _ema_simple(closes, 9)
        if len(ema9) < 4:
            return False, "ema_insufficient"

        slope = ema9[-1] - ema9[-3]   # 2-bar slope of EMA9

        if direction == "BUY"  and slope < 0.0:
            return True, f"ema9_declining slope={slope:.6f} [TREND EXHAUSTION]"
        if direction == "SELL" and slope > 0.0:
            return True, f"ema9_rising slope={slope:.6f} [TREND EXHAUSTION]"

        # ATR compression check — recent bars much smaller than prior bars
        n = len(raw)
        recent_range = sum(highs[i] - lows[i] for i in range(n - 3, n)) / 3
        prior_range  = sum(highs[i] - lows[i] for i in range(n - 8, n - 5)) / 3

        if prior_range > 0 and recent_range < prior_range * 0.50:
            return True, (
                f"atr_compressing recent={recent_range:.5f} prior={prior_range:.5f} "
                f"[TREND EXHAUSTION]"
            )

        return False, "momentum_intact"

    # ── Housekeeping ──────────────────────────────────────────────────────────

    def was_breakeven(self, ticket: int) -> bool:
        """Return True if the BE SL move was applied to this ticket."""
        return ticket in self._be_done

    def was_partially_closed(self, ticket: int) -> bool:
        """Return True if partial TP was applied to this ticket."""
        return ticket in self._partial_done

    def clear_closed(self, open_tickets: Set[int]) -> None:
        """Discard tracking state for tickets that are no longer open."""
        self._be_done        &= open_tickets
        self._partial_done   &= open_tickets
        self._momentum_weak  &= open_tickets
        self._original_risk   = {t: v for t, v in self._original_risk.items()
                                 if t in open_tickets}
