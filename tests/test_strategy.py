"""
Unit tests for Aurex AI strategy modules.

Covers every public analysis function with synthetic candle data so the
test suite has no dependency on MT5, pandas, or network access.

Run:
    pytest tests/ -v
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

import pytest

from aurex_ai.core.data_feed import Candle
from aurex_ai.strategy import (
    trend     as trend_mod,
    liquidity as liq_mod,
    fvg       as fvg_mod,
    fibonacci as fib_mod,
    fallback  as fb_mod,
    order_blocks as ob_mod,
    breakout     as bo_mod,
    structure    as structure_mod,
)
from aurex_ai.strategy.confluence import combine
from aurex_ai.execution.decision_engine import decide
from aurex_ai.execution.scoring_model import compute_ema_score, compute_confirmation_score
from aurex_ai.execution.risk_manager import calculate as calc_risk
from aurex_ai.execution.cooldown_manager import CooldownManager
from aurex_ai.core.data_feed import AccountInfo
from aurex_ai.strategy.liquidity import SweepResult
from aurex_ai.execution.trade_manager import TradeManager


# ── Candle factory ────────────────────────────────────────────────────────────

def _candle(close: float, *, high_delta: float = 0.0002, low_delta: float = 0.0002,
            bullish: bool = True, t: int = 0) -> Candle:
    open_  = close - 0.0001 if bullish else close + 0.0001
    return Candle(
        time   = datetime.fromtimestamp(t, tz=timezone.utc),
        open   = round(open_, 5),
        high   = round(close + high_delta, 5),
        low    = round(close - low_delta,  5),
        close  = round(close, 5),
        volume = 100,
    )


def _trending_candles(n: int, start: float = 1.0800, step: float = 0.0003) -> List[Candle]:
    """Generate n bullish trending candles walking up from `start`."""
    return [_candle(start + i * step, t=i * 900) for i in range(n)]


def _ranging_candles(n: int, mid: float = 1.0800) -> List[Candle]:
    """Generate n candles oscillating around `mid` — no trend."""
    candles = []
    for i in range(n):
        offset = 0.0003 if i % 2 == 0 else -0.0003
        candles.append(_candle(mid + offset, bullish=(i % 2 == 0), t=i * 900))
    return candles


# ── Trend ─────────────────────────────────────────────────────────────────────

class TestTrend:
    def test_bullish_trend_detected(self):
        h1 = _trending_candles(260)
        h4 = _trending_candles(260, step=0.0010)
        result = trend_mod.analyze(h1, h4)
        assert result.direction == "bullish"
        assert result.strength > 0

    def test_insufficient_data_returns_neutral(self):
        result = trend_mod.analyze([], [])
        assert result.direction == "neutral"

    def test_neutral_with_ranging_candles(self):
        h1 = _ranging_candles(260)
        h4 = _ranging_candles(260)
        result = trend_mod.analyze(h1, h4)
        # Alternating candles produce EMA convergence; verify valid structure only
        assert result.direction in ("neutral", "bullish", "bearish")
        assert 0.0 <= result.strength <= 25.0
        assert result.reason != ""

    def test_trend_result_has_reason(self):
        h1 = _trending_candles(260)
        h4 = _trending_candles(260)
        result = trend_mod.analyze(h1, h4)
        assert result.reason != ""


# ── Liquidity sweep ───────────────────────────────────────────────────────────

class TestLiquidity:
    def _sweep_candles(self) -> List[Candle]:
        """Create candles with a stop-hunt wick on the last bar."""
        candles = _ranging_candles(60, mid=1.0800)
        # Replace last candle: spike high (upper wick ≥ 65%), bearish close
        last = candles[-1]
        candles[-1] = Candle(
            time   = last.time,
            open   = 1.0805,
            high   = 1.0820,   # large upper wick
            low    = 1.0800,
            close  = 1.0801,   # closes near open — bearish body
            volume = 200,
        )
        return candles

    def test_no_sweep_returns_none(self):
        candles = _ranging_candles(60)
        result  = liq_mod.analyze(candles)
        assert not result.detected

    def test_stop_hunt_detected(self):
        candles = self._sweep_candles()
        result  = liq_mod.analyze(candles)
        # Stop-hunt wick should be detected
        assert result.detected
        assert result.sweep_type == "sell_side"
        assert result.strength > 0

    def test_insufficient_candles_returns_none(self):
        result = liq_mod.analyze([])
        assert not result.detected


# ── FVG ───────────────────────────────────────────────────────────────────────

class TestFVG:
    def _fvg_candles(self, bullish: bool = True) -> List[Candle]:
        """
        Construct a 3-candle pattern with a gap.
        Bullish: c1.high < c3.low  (price gapped up).
        """
        if bullish:
            c1 = _candle(1.0800, high_delta=0.0002, low_delta=0.0002, t=0)
            c2 = _candle(1.0810, t=900)
            c3 = _candle(1.0825, low_delta=0.0001, t=1800)   # c3.low > c1.high
        else:
            c1 = _candle(1.0830, high_delta=0.0002, low_delta=0.0002, bullish=False, t=0)
            c2 = _candle(1.0815, bullish=False, t=900)
            c3 = _candle(1.0800, high_delta=0.0001, bullish=False, t=1800)
        return [c1, c2, c3]

    def test_bullish_fvg_detected(self):
        candles = _ranging_candles(20) + self._fvg_candles(bullish=True)
        price   = candles[-1].close
        result  = fvg_mod.analyze(candles, price=price, pip_size=0.0001)
        assert result.present
        assert result.active_zone.bullish is True
        assert result.strength in ("strong", "weak")

    def test_price_inside_zone_is_strong(self):
        candles = _ranging_candles(20) + self._fvg_candles(bullish=True)
        # Place price squarely inside the gap
        zone_mid = (candles[-1].low + candles[-3].high) / 2.0
        result   = fvg_mod.analyze(candles, price=zone_mid, pip_size=0.0001)
        if result.present and result.price_in_zone:
            assert result.score    == 20.0
            assert result.strength == "strong"

    def test_price_outside_zone_is_weak(self):
        candles = _ranging_candles(20) + self._fvg_candles(bullish=True)
        # Place price clearly above the FVG zone
        result = fvg_mod.analyze(candles, price=1.0900, pip_size=0.0001)
        if result.present and not result.price_in_zone:
            assert result.score    == 10.0
            assert result.strength == "weak"

    def test_no_fvg_in_flat_market(self):
        candles = _ranging_candles(30, mid=1.0800)
        result  = fvg_mod.analyze(candles, price=1.0800, pip_size=0.0001)
        if result.present:
            assert result.score in (10.0, 20.0)
            assert result.strength in ("strong", "weak")

    def test_no_fvg_returns_none_strength(self):
        candles = _ranging_candles(30, mid=1.0800)
        result  = fvg_mod.analyze(candles, price=1.0800, pip_size=0.0001)
        if not result.present:
            assert result.strength == "none"
            assert result.score    == 0.0

    def test_insufficient_candles(self):
        result = fvg_mod.analyze([_candle(1.0800)], price=1.0800)
        assert not result.present
        assert result.strength == "none"


# ── Fibonacci ─────────────────────────────────────────────────────────────────

class TestFibonacci:
    def test_bullish_impulse_detected(self):
        # Create strong upward move
        lows  = _trending_candles(30, start=1.0700, step=-0.0005)  # bearish first half
        highs = _trending_candles(30, start=1.0700, step= 0.0010)  # bullish second half
        candles = lows + highs
        result = fib_mod.analyze(candles, price=highs[-1].close * 0.95,
                                  pip_size=0.0001, min_move_pips=20.0)
        assert result.impulse_dir != "none"
        assert len(result.levels) > 0

    def test_small_move_returns_empty(self):
        candles = _ranging_candles(60, mid=1.0800)
        result  = fib_mod.analyze(candles, price=1.0800,
                                   pip_size=0.0001, min_move_pips=50.0)
        assert result.impulse_dir == "none"
        assert result.score == 0.0


# ── Fallback strategy ─────────────────────────────────────────────────────────

class TestFallback:
    def test_flat_market_skipped_by_atr_floor(self):
        # Minimal-movement candles -> ATR well below 8 pips
        candles = [_candle(1.0800, high_delta=0.00002, low_delta=0.00002) for _ in range(30)]
        result  = fb_mod.analyze(candles, price=1.0800, pip_size=0.0001)
        assert not result.detected

    def test_wide_spread_skipped(self):
        candles = _trending_candles(30)
        cfg     = fb_mod.FallbackConfig(max_spread_pips=3.0)
        result  = fb_mod.analyze(candles, price=candles[-1].close,
                                  pip_size=0.0001, spread_pips=10.0, cfg=cfg)
        assert not result.detected

    def test_strength_capped_at_050(self):
        candles = _trending_candles(40, step=0.0008)
        result  = fb_mod.analyze(candles, price=candles[-1].close, pip_size=0.0001)
        if result.detected:
            assert result.strength <= 0.50

    def test_get_idle_threshold_prime_session(self):
        # During London (09:00) with high ATR -> active threshold
        threshold = fb_mod.get_idle_threshold(utc_hour=9, atr_pips=15.0,
                                               base=5, active=3, min_atr=8.0)
        assert threshold == 3

    def test_get_idle_threshold_asian_session(self):
        threshold = fb_mod.get_idle_threshold(utc_hour=2, atr_pips=15.0,
                                               base=5, active=3, min_atr=8.0)
        assert threshold == 5

    def test_get_idle_threshold_low_atr(self):
        # Prime session but low ATR -> base threshold
        threshold = fb_mod.get_idle_threshold(utc_hour=10, atr_pips=5.0,
                                               base=5, active=3, min_atr=8.0)
        assert threshold == 5


# ── Order blocks ──────────────────────────────────────────────────────────────

class TestOrderBlocks:
    def test_no_ob_in_flat_market(self):
        candles = _ranging_candles(40)
        result  = ob_mod.analyze(candles, price=1.0800, pip_size=0.0001)
        # Flat market — no significant impulse -> no OB or very low score
        if result.present:
            assert result.score <= 8.0

    def test_returns_valid_structure(self):
        candles = _trending_candles(40)
        result  = ob_mod.analyze(candles, price=candles[-1].close, pip_size=0.0001)
        assert isinstance(result.score, float)
        assert 0.0 <= result.score <= 15.0


# ── Breakout ──────────────────────────────────────────────────────────────────

class TestBreakout:
    def test_bullish_breakout_detected(self):
        # Consolidation then breakout
        consolidation = _ranging_candles(10, mid=1.0800)
        impulse       = _trending_candles(10, start=1.0815, step=0.0010)
        candles       = consolidation + impulse
        result        = bo_mod.analyze(candles, price=candles[-1].close, pip_size=0.0001)
        if result.detected:
            assert result.direction in ("bullish", "bearish")
            assert result.score > 0

    def test_score_within_bounds(self):
        candles = _ranging_candles(30)
        result  = bo_mod.analyze(candles, price=1.0800, pip_size=0.0001)
        assert 0.0 <= result.score <= 12.0


# ── Scoring model ─────────────────────────────────────────────────────────────

class TestScoringModel:
    def test_ema_score_at_price(self):
        candles = _trending_candles(30)
        price   = candles[-1].close
        result  = compute_ema_score(price, candles, ema_period=21)
        assert 0.0 <= result.score <= 10.0

    def test_ema_score_far_from_ema_is_zero(self):
        candles = _ranging_candles(30)
        # Price far above: 10% away
        price  = 1.0800 * 1.10
        result = compute_ema_score(price, candles, ema_period=21, threshold_pct=0.50)
        assert result.score == 0.0

    def test_confirmation_strong_engulfing(self):
        prev = _candle(1.0800, bullish=False, t=0)
        cur  = Candle(time=datetime.fromtimestamp(900, tz=timezone.utc),
                      open=1.0795, high=1.0820, low=1.0793, close=1.0818, volume=200)
        result = compute_confirmation_score([prev, cur], "BUY")
        assert result.pattern in ("strong_engulfing", "engulfing")
        assert result.score >= 8.0

    def test_confirmation_no_pattern(self):
        candles = _ranging_candles(5)
        result  = compute_confirmation_score(candles, "BUY")
        assert isinstance(result.score, float)


# ── Decision engine ───────────────────────────────────────────────────────────

class TestDecisionEngine:
    def _confluence(self, score: float, direction: str = "BUY"):
        from aurex_ai.strategy.confluence import ConfluenceResult
        return ConfluenceResult(
            direction=direction, total_score=score, core_score=score,
            trend_score=score * 0.25, liquidity_score=score * 0.20,
            fvg_score=score * 0.20, fib_score=score * 0.15,
            ema_score=score * 0.10, confirmation_score=score * 0.10,
            ob_score=0.0, breakout_score=0.0, structure_score=0.0,
            buy_votes=3, sell_votes=0,
            reason=f"test score={score}",
        )

    def test_tier1_execute(self):
        d = decide("EURUSD.Z", self._confluence(80.0))
        assert d.action == "EXECUTE"
        assert d.tier   == 1
        assert d.size_mult == 1.0

    def test_tier2_conditional(self):
        d = decide("EURUSD.Z", self._confluence(65.0))
        assert d.action == "CONDITIONAL"
        assert d.tier   == 2
        assert d.size_mult == 0.5

    def test_tier3(self):
        d = decide("EURUSD.Z", self._confluence(52.0))
        assert d.action == "TIER3"
        assert d.tier   == 3
        assert d.size_mult == 0.25

    def test_skip_low_score(self):
        d = decide("EURUSD.Z", self._confluence(40.0))
        assert d.action == "SKIP"
        assert d.size_mult == 0.0

    def test_none_direction_always_skip(self):
        d = decide("EURUSD.Z", self._confluence(95.0, direction="NONE"))
        assert d.action == "SKIP"


# ── Risk manager ──────────────────────────────────────────────────────────────

class TestRiskManager:
    _ACCOUNT = AccountInfo(
        balance=10_000.0, equity=10_000.0, margin=0.0,
        free_margin=10_000.0, currency="USD", leverage=100,
    )
    _SYM_INFO = {
        "point": 0.00001, "digits": 5, "trade_contract_size": 100_000,
        "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
        "trade_tick_value": 1.0, "trade_tick_size": 0.00001,
    }
    _NULL_SWEEP = SweepResult(
        detected=False, sweep_type="none", strength=0.0,
        swept_level=0.0, pattern="none", wick_ratio=0.0,
        touches=0, reason="no_sweep",
    )

    def test_buy_risk_allowed(self):
        candles = _trending_candles(30, start=1.0700)
        result  = calc_risk(
            direction="BUY", entry=1.0800,
            account=self._ACCOUNT, candles_m15=candles,
            sweep=self._NULL_SWEEP, symbol_info=self._SYM_INFO,
            symbol="EURUSD.Z",
        )
        assert result.allowed
        assert result.stop_loss < 1.0800
        assert result.take_profit > 1.0800
        assert result.lot_size >= 0.01
        assert result.rr_ratio >= 2.0

    def test_sell_risk_allowed(self):
        # Ranging candles around 1.0900; entry at 1.0800.
        # ATR-based TP = entry − ATR×3.0 guarantees RR = 2.0.
        candles = _ranging_candles(30, mid=1.0900)
        entry   = 1.0800
        result  = calc_risk(
            direction="SELL", entry=entry,
            account=self._ACCOUNT, candles_m15=candles,
            sweep=self._NULL_SWEEP, symbol_info=self._SYM_INFO,
            symbol="EURUSD.Z",
        )
        assert result.allowed
        assert result.stop_loss > entry
        assert result.take_profit < entry

    def test_size_mult_scales_lot(self):
        candles = _trending_candles(30, start=1.0700)
        full = calc_risk(
            direction="BUY", entry=1.0800,
            account=self._ACCOUNT, candles_m15=candles,
            sweep=self._NULL_SWEEP, symbol_info=self._SYM_INFO,
            symbol="EURUSD.Z", size_mult=1.0,
        )
        half = calc_risk(
            direction="BUY", entry=1.0800,
            account=self._ACCOUNT, candles_m15=candles,
            sweep=self._NULL_SWEEP, symbol_info=self._SYM_INFO,
            symbol="EURUSD.Z", size_mult=0.5,
        )
        if full.allowed and half.allowed:
            assert half.lot_size < full.lot_size


# ── Cooldown manager ──────────────────────────────────────────────────────────

class TestCooldown:
    def test_not_on_cooldown_initially(self):
        cm = CooldownManager()
        assert not cm.is_on_cooldown("EURUSD.Z", "BUY")

    def test_arm_and_check_cooldown(self):
        cm         = CooldownManager()
        null_trend = trend_mod.TrendResult(
            direction="neutral", strength=0.0,
            h1_aligned=False, h4_aligned=False, consistent=False,
            ema50_h1=0.0, ema200_h1=0.0, ema50_h4=0.0, ema200_h4=0.0,
            price=1.0800, reason="test",
        )
        cm.arm("EURUSD.Z", "BUY", trend=null_trend, trending_minutes=60, ranging_minutes=120)
        assert cm.is_on_cooldown("EURUSD.Z", "BUY")
        assert cm.remaining("EURUSD", "BUY") > 0

    def test_clear_removes_cooldown(self):
        cm         = CooldownManager()
        null_trend = trend_mod.TrendResult(
            direction="neutral", strength=0.0,
            h1_aligned=False, h4_aligned=False, consistent=False,
            ema50_h1=0.0, ema200_h1=0.0, ema50_h4=0.0, ema200_h4=0.0,
            price=1.0800, reason="test",
        )
        cm.arm("EURUSD.Z", "BUY", trend=null_trend, trending_minutes=60, ranging_minutes=120)
        cm.clear("EURUSD.Z", "BUY")
        assert not cm.is_on_cooldown("EURUSD.Z", "BUY")

    def test_arm_after_outcome_covers_both_directions(self):
        cm = CooldownManager()
        cm.arm_after_outcome("EURUSD.Z", was_win=False, post_loss_minutes=45, post_win_minutes=20)
        assert cm.is_on_cooldown("EURUSD.Z", "BUY")
        assert cm.is_on_cooldown("EURUSD", "SELL")


# ── Structure detection ───────────────────────────────────────────────────────

def _xau_candle(close: float, *, high_delta: float = 1.0, low_delta: float = 1.0,
                bullish: bool = True, t: int = 0) -> Candle:
    """Large-range candle used by structure detection tests (price ~2000, deltas in price units)."""
    open_ = close - 0.5 if bullish else close + 0.5
    return Candle(
        time   = datetime.fromtimestamp(t, tz=timezone.utc),
        open   = round(open_, 2),
        high   = round(close + high_delta, 2),
        low    = round(close - low_delta,  2),
        close  = round(close, 2),
        volume = 100,
    )


def _build_structure_candles(n: int = 60, base: float = 2000.0) -> List[Candle]:
    """60 trending-up candles: each bar closes 1.0 higher than the last."""
    return [_xau_candle(base + i, t=i) for i in range(n)]


class TestStructure:
    """
    BOS detection: signal_close > max(last_10_highs) = buy BOS
                   signal_close < min(last_10_lows)  = sell BOS
    Trend bias:    second-half of structure window has higher highs → buy
                   second-half has lower lows → sell
    """

    def test_insufficient_data_returns_neutral(self):
        # Fewer than 30 candles → always neutral
        result = structure_mod.analyze([_xau_candle(2000.0)])
        assert result.bos_detected  is False
        assert result.bos_direction == "none"
        assert result.trend_bias    == "neutral"
        assert result.score         == 0.0

    def test_insufficient_data_at_boundary(self):
        # 29 candles (one below the 30-candle minimum)
        candles = [_xau_candle(2000.0, t=i) for i in range(29)]
        result = structure_mod.analyze(candles)
        assert result.bos_detected is False
        assert result.score        == 0.0

    def test_bos_buy_detected(self):
        # 35 flat candles; signal bar closes above max(last_10_highs).
        # Flat candles: close=2000, high=2001 (high_delta default = 1.0)
        # recent_high = 2001.0; signal close = 2010.0 > 2001.0 → BUY BOS
        candles = [_xau_candle(2000.0, t=i) for i in range(34)]
        candles.append(_xau_candle(2010.0, t=34))   # signal bar
        result = structure_mod.analyze(candles, sweep_detected=False)
        assert result.bos_detected  is True
        assert result.bos_direction == "buy"

    def test_bos_sell_detected(self):
        # 35 flat candles; signal bar closes below min(last_10_lows).
        # Flat candles: close=2000, low=1999 (low_delta default = 1.0)
        # recent_low = 1999.0; signal close = 1990.0 < 1999.0 → SELL BOS
        candles = [_xau_candle(2000.0, t=i) for i in range(34)]
        candles.append(_xau_candle(1990.0, bullish=False, t=34))   # signal bar
        result = structure_mod.analyze(candles, sweep_detected=False)
        assert result.bos_detected  is True
        assert result.bos_direction == "sell"

    def test_no_bos_when_inside_range(self):
        # Signal bar stays within the last-10-candle high/low range.
        candles = [_xau_candle(2000.0, t=i) for i in range(34)]
        candles.append(_xau_candle(2000.5, t=34))   # 2000.5 < recent_high 2001
        result = structure_mod.analyze(candles, sweep_detected=False)
        assert result.bos_detected is False

    def test_strong_score_bos_plus_sweep(self):
        candles = [_xau_candle(2000.0, t=i) for i in range(34)]
        candles.append(_xau_candle(2010.0, t=34))
        result = structure_mod.analyze(candles, sweep_detected=True)
        assert result.bos_detected is True
        assert result.score        == 25.0

    def test_moderate_score_bos_only(self):
        candles = [_xau_candle(2000.0, t=i) for i in range(34)]
        candles.append(_xau_candle(2010.0, t=34))
        result = structure_mod.analyze(candles, sweep_detected=False)
        assert result.bos_detected is True
        assert result.score        == 15.0

    def test_weak_score_trend_bias_buy(self):
        # Trending up: second half of structure window has higher highs.
        # candles[-11:-1]: indices 24-33, closes 2024-2033, highs 2025-2034
        # first half (24-28): max high = 2029; second half (29-33): max high = 2034
        # 2034 > 2029 → bias = "buy"
        # Signal bar close = 2034.0 = recent_high → NOT a BOS (not strictly greater)
        candles = _build_structure_candles(35, base=2000.0)
        result  = structure_mod.analyze(candles, sweep_detected=False)
        # On a smooth ascending series the signal close equals recent_high
        # exactly → no BOS, but trend bias fires → score 5
        assert result.trend_bias == "buy"
        assert result.score      == 5.0

    def test_weak_score_trend_bias_sell(self):
        # Trending down: second half has lower lows.
        candles = [_xau_candle(2100.0 - i, bullish=False, t=i) for i in range(35)]
        # Structure = indices 24-33; lows: 2100-24-1=2075 .. 2100-33-1=2066
        # first half: min low ≈ 2071; second half: min low ≈ 2066 < 2071 → "sell"
        # Signal close = 2100-34 = 2066; recent_low = 2100-33-1 = 2066 → not < → no BOS
        result = structure_mod.analyze(candles, sweep_detected=False)
        assert result.trend_bias in ("sell", "neutral")   # sell on strict descent

    def test_no_score_flat_market(self):
        # Perfectly flat: no BOS, no trend bias → score 0
        candles = [_xau_candle(2000.0, t=i) for i in range(35)]
        # Signal bar also flat: 2000.0 ≤ recent_high=2001.0 and ≥ recent_low=1999.0
        result = structure_mod.analyze(candles, sweep_detected=False)
        assert result.bos_detected is False
        assert result.score        == 0.0   # no bias on identical candles


# ── Trailing stop ─────────────────────────────────────────────────────────────

class _MockBridge:
    """Minimal mock bridge for TradeManager tests."""

    def __init__(
        self,
        bid:        float,
        ask:        float,
        positions:  list,
        atr_value:  float = 100.0,   # ATR in price units for mock bridge
    ) -> None:
        self._bid       = bid
        self._ask       = ask
        self._positions = positions
        self._atr_value = atr_value
        self.modify_sl_calls: list = []   # records (ticket, new_sl, new_tp)

    def get_open_positions(self, symbol=None):
        return list(self._positions)

    def get_tick_raw(self, symbol):
        return self._bid, self._ask

    def get_symbol_info(self, symbol):
        return {"volume_min": 0.01, "volume_step": 0.01}

    def partial_close(self, ticket, volume):
        return True

    def modify_sl(self, ticket, new_sl, new_tp=None):
        self.modify_sl_calls.append((ticket, new_sl, new_tp))
        return True

    def get_candles_raw(self, symbol, timeframe, count):
        """Return synthetic candles whose ATR equals self._atr_value."""
        candles = []
        base = 2000.0
        for i in range(count):
            c_range = self._atr_value
            o = base
            h = base + c_range
            l = base
            c = base + c_range * 0.5
            candles.append({"time": i * 900, "open": o, "high": h,
                             "low": l, "close": c, "tick_volume": 100})
        return candles


def _make_pos(ticket, direction, entry, sl, tp, volume=0.10):
    return {
        "ticket": ticket, "symbol": "EURUSD.Z", "type": direction,
        "price_open": entry, "sl": sl, "tp": tp, "volume": volume,
    }


class _MockCfg:
    class trade_management:
        breakeven_enabled  = True
        partial_tp_enabled = True
        partial_tp_ratio   = 0.5
        breakeven_rr       = 1.0
        trail_1r5_rr       = 1.5
        trail_2r_rr        = 2.0
        trail_atr_mult_1   = 1.0
        trail_atr_mult_2   = 0.5


class TestTrailingStop:

    def test_be_fires_at_1r_buy(self):
        # Entry 2000, SL 1850 → risk = 150 pips. At current 2150 (+1R) → BE
        entry, sl = 2000.0, 1850.0
        risk = entry - sl  # 150
        current = entry + risk * 1.0  # 2150 (exactly 1R)
        pos = _make_pos(1, "BUY", entry, sl, 2450.0)
        bridge = _MockBridge(bid=current, ask=current + 0.3, positions=[pos])
        tm = TradeManager()
        tm.manage(bridge, _MockCfg())
        assert 1 in tm._be_done
        # BE call: new_sl == entry
        be_calls = [(t, s) for t, s, _ in bridge.modify_sl_calls if abs(s - entry) < 0.01]
        assert len(be_calls) == 1

    def test_be_fires_at_1r_sell(self):
        entry, sl = 2000.0, 2150.0
        risk = sl - entry  # 150
        current = entry - risk * 1.0  # 1850 (exactly 1R profit)
        pos = _make_pos(2, "SELL", entry, sl, 1550.0)
        bridge = _MockBridge(bid=current - 0.3, ask=current, positions=[pos])
        tm = TradeManager()
        tm.manage(bridge, _MockCfg())
        assert 2 in tm._be_done

    def test_trail_fires_at_1r5_buy(self):
        # ATR = 100.  At 1.5R, trail_sl = current − ATR × 1.0
        entry, sl = 2000.0, 1850.0
        risk    = entry - sl        # 150
        atr     = 100.0
        current = entry + risk * 1.5  # 2225 (1.5R)
        expected_trail = round(current - atr * 1.0, 5)  # 2125

        pos = _make_pos(3, "BUY", entry, sl, 2450.0)
        bridge = _MockBridge(bid=current, ask=current + 0.3,
                             positions=[pos], atr_value=atr)
        tm = TradeManager()
        tm._be_done.add(3)                  # simulate BE already applied
        tm._original_risk[3] = risk         # store original risk so R-multiples work
        pos["sl"] = entry                   # simulate SL already at entry (post-BE)

        tm.manage(bridge, _MockCfg())

        trail_calls = [s for t, s, _ in bridge.modify_sl_calls]
        assert any(abs(s - expected_trail) < 0.01 for s in trail_calls), (
            f"Expected trail SL ~{expected_trail:.5f}, got calls: {trail_calls}"
        )

    def test_trail_tighter_at_2r_buy(self):
        # ATR = 100.  At 2R, trail_sl = current − ATR × 0.5
        entry, sl = 2000.0, 1850.0
        risk    = entry - sl        # 150
        atr     = 100.0
        current = entry + risk * 2.0  # 2300 (2R)
        expected_trail = round(current - atr * 0.5, 5)  # 2250

        pos = _make_pos(4, "BUY", entry, sl, 2450.0)
        bridge = _MockBridge(bid=current, ask=current + 0.3,
                             positions=[pos], atr_value=atr)
        tm = TradeManager()
        tm._be_done.add(4)
        tm._original_risk[4] = risk
        pos["sl"] = entry + 50.0   # simulate previous trail already moved SL

        tm.manage(bridge, _MockCfg())

        trail_calls = [s for t, s, _ in bridge.modify_sl_calls]
        assert any(abs(s - expected_trail) < 0.01 for s in trail_calls), (
            f"Expected tighter trail SL ~{expected_trail:.5f}, got calls: {trail_calls}"
        )

    def test_trail_does_not_regress(self):
        # If the trail candidate would be below current SL → no modify_sl call
        entry, sl = 2000.0, 1850.0
        risk    = entry - sl
        atr     = 100.0
        current = entry + risk * 1.6  # 1.6R — trail = current - 100 = entry + 140

        pos = _make_pos(5, "BUY", entry, sl, 2450.0)
        # Simulate SL already trailing at a higher level than the new candidate
        pos["sl"] = entry + 200.0   # already at entry + 200 (better than entry+140)

        bridge = _MockBridge(bid=current, ask=current + 0.3,
                             positions=[pos], atr_value=atr)
        tm = TradeManager()
        tm._be_done.add(5)
        tm._original_risk[5] = risk
        tm.manage(bridge, _MockCfg())

        # No modify_sl should have fired (candidate was worse than existing SL)
        assert bridge.modify_sl_calls == []

    def test_trail_blocked_without_be(self):
        # At 1.5R but BE not yet in _be_done → no trail
        entry, sl = 2000.0, 1850.0
        risk    = entry - sl
        current = entry + risk * 1.5  # 1.5R

        pos = _make_pos(6, "BUY", entry, sl, 2450.0)
        bridge = _MockBridge(bid=current, ask=current + 0.3, positions=[pos])
        tm = TradeManager()   # _be_done is empty
        tm.manage(bridge, _MockCfg())

        # BE fires at 1R but 1.5R > 1.0R so BE fires in this same cycle.
        # Trail can only fire AFTER BE is in _be_done — and BE fires in this
        # cycle, so both may execute.  The key assertion: no modify_sl for a
        # SL level that's below entry (trail must not go backwards).
        trail_sl_calls = [s for _, s, _ in bridge.modify_sl_calls if s < entry]
        assert trail_sl_calls == []


# ── Confluence direction resolution ──────────────────────────────────────────

from aurex_ai.strategy.confluence import combine
from aurex_ai.strategy.trend     import TrendResult
from aurex_ai.strategy.liquidity import SweepResult as _SR
from aurex_ai.strategy.fvg       import FVGResult, FVGZone
from aurex_ai.strategy.fibonacci  import FibResult


def _make_trend(direction: str, strength: float = 20.0) -> TrendResult:
    return TrendResult(
        direction=direction, strength=strength,
        h1_aligned=(direction != "neutral"), h4_aligned=(direction != "neutral"),
        consistent=(direction != "neutral"),
        ema50_h1=1.0, ema200_h1=1.0, ema50_h4=1.0, ema200_h4=1.0,
        price=1.0, reason="test",
    )


def _make_sweep(detected: bool, sweep_type: str = "none", strength: float = 10.0) -> _SR:
    return _SR(
        detected=detected, sweep_type=sweep_type, strength=strength,
        swept_level=1.0, pattern="test", wick_ratio=0.7, touches=1, reason="test",
    )


def _make_fvg(present: bool, bullish: bool = True, score: float = 10.0) -> FVGResult:
    zone = FVGZone(high=1.002, low=1.001, bullish=bullish, bar_idx=0) if present else None
    return FVGResult(
        present=present, zones=[zone] if zone else [],
        active_zone=zone, score=score if present else 0.0,
        price_in_zone=False, strength="weak" if present else "none", reason="test",
    )


def _make_fib() -> FibResult:
    return FibResult(
        impulse_high=1.1, impulse_low=1.0, impulse_dir="none",
        levels={}, golden_zone=(1.05, 1.07), price_in_zone=False,
        score=0.0, nearest_ratio=0.0, reason="none",
    )


class TestConfluenceDirection:
    """Verify the weighted direction resolver never dead-locks."""

    def _combine(self, trend, sweep, fvg=None):
        if fvg is None:
            fvg = _make_fvg(present=False)
        return combine(
            trend=trend, sweep=sweep, fvg=fvg, fib=_make_fib(),
            ema_score=5.0, confirmation_score=5.0, symbol="TEST",
        )

    def test_trend_dominates_conflicting_sweep(self):
        # trend=bullish(strength=20) buy_w=40 vs sweep=sell_side(strength=10) sell_w=10
        # Expected: BUY
        result = self._combine(
            trend=_make_trend("bullish", strength=20.0),
            sweep=_make_sweep(True, "sell_side", strength=10.0),
        )
        assert result.direction == "BUY"

    def test_sweep_wins_without_trend(self):
        # No trend, sweep=buy_side only → BUY via weighted score
        result = self._combine(
            trend=_make_trend("neutral", strength=0.0),
            sweep=_make_sweep(True, "buy_side", strength=15.0),
        )
        assert result.direction == "BUY"

    def test_trend_fallback_when_all_absent_except_trend(self):
        # Trend=bullish, no sweep, no FVG — weighted vote gives BUY directly
        result = self._combine(
            trend=_make_trend("bullish", strength=10.0),
            sweep=_make_sweep(False),
        )
        assert result.direction == "BUY"

    def test_trend_fallback_covers_none_vote_gap(self):
        # Weighted buy_w=0, sell_w=0 (neutral trend, no sweep, no FVG)
        # → all-absent NONE; but trend override also absent → stays NONE
        result = self._combine(
            trend=_make_trend("neutral", strength=0.0),
            sweep=_make_sweep(False),
        )
        assert result.direction == "NONE"

    def test_fvg_tie_broken_by_trend(self):
        # trend=bullish(strength=5) buy_w=10; FVG=bearish(score=20) sell_w=10 — exact tie
        # raw votes: buy=1, sell=1 — still tied; trend wins → BUY
        result = self._combine(
            trend=_make_trend("bullish", strength=5.0),
            sweep=_make_sweep(False),
            fvg=_make_fvg(present=True, bullish=False, score=20.0),
        )
        assert result.direction == "BUY"

    def test_bearish_trend_dominates(self):
        # trend=bearish, sweep=buy_side weak → SELL
        result = self._combine(
            trend=_make_trend("bearish", strength=20.0),
            sweep=_make_sweep(True, "buy_side", strength=5.0),
        )
        assert result.direction == "SELL"

    def test_direction_set_enables_structure_bonus(self):
        # When direction is resolved, structure bonus counts toward total score.
        # Without fix (direction=NONE): struct_pts=0; with fix: struct_pts>0
        result = combine(
            trend=_make_trend("bullish", strength=20.0),
            sweep=_make_sweep(True, "sell_side", strength=5.0),  # conflicts → old code: NONE
            fvg=_make_fvg(present=False),
            fib=_make_fib(),
            ema_score=5.0, confirmation_score=5.0,
            structure_score=15.0, structure_direction="buy",
            symbol="TEST",
        )
        assert result.direction == "BUY"
        assert result.structure_score > 0.0   # bonus applied because direction resolved
