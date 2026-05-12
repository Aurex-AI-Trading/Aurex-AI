"""
Aurex AI — News Guard  (Phase 7: Institutional Upgrade)

Lightweight news event protection without external API dependency.
Identifies high-impact economic release windows from deterministic UTC patterns.

Covered events:
  NFP        — First Friday of each month, 13:30 UTC     (block ±pre/post minutes)
  FOMC risk  — Every Wednesday, 17:45–20:00 UTC          (reduce size — not every Wed)
  CPI        — Second Tuesday of each month, 13:30 UTC   (block ±pre/post minutes)
  Rate day   — Every Thursday, 11:00–13:30 UTC           (reduce size — ECB/BoE windows)
  Rollover   — 22:00–23:59 UTC  (outside session gate; secondary guard)

Configuration (settings.yaml news: section):
  avoid_nfp:           true    — block NFP window
  nfp_window_pre:      20      — minutes before 13:30 UTC to start protection
  nfp_window_post:     45      — minutes after 13:30 UTC before resuming
  avoid_fomc:          true    — reduce size Wednesdays 17:45-20:00 UTC
  fomc_size_mult:      0.50    — lot multiplier during FOMC risk window
  avoid_cpi:           true    — block CPI window (2nd Tuesday each month)
  cpi_window_pre:      20      — minutes before 13:30 UTC to start protection
  cpi_window_post:     45      — minutes after 13:30 UTC before resuming
  avoid_rate_day:      true    — reduce size on rate-decision Thursdays
  rate_day_size_mult:  0.60    — lot multiplier during rate-day window
  rate_day_start_hm:   660     — 11:00 UTC in minutes-since-midnight
  rate_day_end_hm:     810     — 13:30 UTC in minutes-since-midnight
  news_size_mult:      0.50    — generic fallback size multiplier

Usage:
    result = check_news(cfg, now_utc)
    if not result.safe:
        return None    # block execution
    _news_mult = result.size_mult
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from aurex_ai.core.logger import get_logger

log = get_logger("execution.news_guard")


@dataclass
class NewsGuardResult:
    safe:       bool   # True = safe to trade; False = block this scan
    size_mult:  float  # 1.0 = no change; < 1.0 = reduced size; 0.0 = full block
    event_type: str    # "NFP" | "FOMC_RISK" | "CLEAR"
    reason:     str


# ── Calendar helpers ──────────────────────────────────────────────────────────

def _first_friday(year: int, month: int) -> date:
    """Return the date of the first Friday in the given year/month."""
    for week in calendar.monthcalendar(year, month):
        if week[calendar.FRIDAY] != 0:
            return date(year, month, week[calendar.FRIDAY])
    raise ValueError(f"No Friday in {year}-{month}")   # should never happen


def _second_tuesday(year: int, month: int) -> date:
    """Return the date of the second Tuesday in the given year/month."""
    tuesdays = [
        week[calendar.TUESDAY]
        for week in calendar.monthcalendar(year, month)
        if week[calendar.TUESDAY] != 0
    ]
    return date(year, month, tuesdays[1])


# ── Public API ────────────────────────────────────────────────────────────────

def check_news(cfg: object, now_utc: datetime) -> NewsGuardResult:
    """
    Check if now_utc falls within a high-impact news protection window.

    Returns NewsGuardResult with safe=False when execution should be blocked,
    or safe=True with size_mult < 1.0 when size should be reduced.
    """
    news              = getattr(cfg, "news", None)
    avoid_nfp         = bool(getattr(news,  "avoid_nfp",           True))
    avoid_fomc        = bool(getattr(news,  "avoid_fomc",           True))
    avoid_cpi         = bool(getattr(news,  "avoid_cpi",            True))
    avoid_rate_day    = bool(getattr(news,  "avoid_rate_day",       True))
    nfp_pre           = int(getattr(news,   "nfp_window_pre",       20))
    nfp_post          = int(getattr(news,   "nfp_window_post",      45))
    cpi_pre           = int(getattr(news,   "cpi_window_pre",       20))
    cpi_post          = int(getattr(news,   "cpi_window_post",      45))
    fomc_mult         = float(getattr(news, "fomc_size_mult",       0.50))
    rate_day_mult     = float(getattr(news, "rate_day_size_mult",   0.60))
    rate_day_start_hm = int(getattr(news,   "rate_day_start_hm",   660))   # 11:00 UTC
    rate_day_end_hm   = int(getattr(news,   "rate_day_end_hm",     810))   # 13:30 UTC

    h       = now_utc.hour
    m       = now_utc.minute
    hm      = h * 60 + m           # minutes since midnight UTC
    weekday = now_utc.weekday()    # 0=Mon … 4=Fri … 6=Sun

    # ── NFP: First Friday of each month, 13:30 UTC ────────────────────────────
    if avoid_nfp and weekday == 4:   # Friday
        try:
            nfp_date = _first_friday(now_utc.year, now_utc.month)
        except ValueError:
            nfp_date = None

        if nfp_date is not None and now_utc.date() == nfp_date:
            nfp_hm   = 13 * 60 + 30
            pre_hm   = nfp_hm - nfp_pre
            post_hm  = nfp_hm + nfp_post
            if pre_hm <= hm <= post_hm:
                phase  = "PRE" if hm < nfp_hm else "POST"
                reason = f"NFP {phase}-release | 13:30 UTC ±{nfp_pre}/{nfp_post} min"
                log.warning(
                    "[NEWS FILTER] [HIGH IMPACT EVENT] [TRADING PAUSED FOR NEWS] "
                    "NFP %s window | %s",
                    phase, reason,
                )
                return NewsGuardResult(
                    safe=False, size_mult=0.0, event_type="NFP", reason=reason,
                )

    # ── CPI: Second Tuesday of each month, 13:30 UTC ─────────────────────────
    if avoid_cpi and weekday == 1:   # Tuesday
        try:
            cpi_date = _second_tuesday(now_utc.year, now_utc.month)
        except (IndexError, ValueError):
            cpi_date = None

        if cpi_date is not None and now_utc.date() == cpi_date:
            cpi_hm  = 13 * 60 + 30
            pre_hm  = cpi_hm - cpi_pre
            post_hm = cpi_hm + cpi_post
            if pre_hm <= hm <= post_hm:
                phase  = "PRE" if hm < cpi_hm else "POST"
                reason = f"CPI {phase}-release | 13:30 UTC ±{cpi_pre}/{cpi_post} min"
                log.warning(
                    "[NEWS FILTER] [HIGH IMPACT EVENT] [TRADING PAUSED FOR NEWS] "
                    "CPI %s window | %s",
                    phase, reason,
                )
                return NewsGuardResult(
                    safe=False, size_mult=0.0, event_type="CPI", reason=reason,
                )

    # ── FOMC risk window: Wednesdays 17:45–20:00 UTC ─────────────────────────
    # Not every Wednesday has an FOMC statement, but this window covers the
    # typical release time (18:00 ET = 22:00 UTC, or 14:00 ET = 19:00 UTC).
    # Conservative protection: reduce size on all Wednesdays during this window.
    if avoid_fomc and weekday == 2:   # Wednesday
        fomc_start = 17 * 60 + 45    # 17:45 UTC
        fomc_end   = 20 * 60         # 20:00 UTC
        if fomc_start <= hm < fomc_end:
            reason = "FOMC risk window | Wed 17:45-20:00 UTC"
            log.warning(
                "[NEWS FILTER] [HIGH IMPACT EVENT] FOMC risk window active | %s | "
                "size=%.0f%%",
                reason, fomc_mult * 100,
            )
            return NewsGuardResult(
                safe=True, size_mult=fomc_mult, event_type="FOMC_RISK", reason=reason,
            )

    # ── Rate day: Thursdays, ECB/BoE decision window 11:00–13:30 UTC ─────────
    # Covers ECB (13:15 UTC) and BoE (12:00 UTC) interest rate announcements.
    # Not every Thursday has a decision; conservative approach reduces size.
    if avoid_rate_day and weekday == 3:   # Thursday
        if rate_day_start_hm <= hm < rate_day_end_hm:
            reason = f"Rate-day window | Thu {rate_day_start_hm//60:02d}:{rate_day_start_hm%60:02d}-{rate_day_end_hm//60:02d}:{rate_day_end_hm%60:02d} UTC"
            log.warning(
                "[NEWS FILTER] [HIGH IMPACT EVENT] Rate-day window active | %s | "
                "size=%.0f%%",
                reason, rate_day_mult * 100,
            )
            return NewsGuardResult(
                safe=True, size_mult=rate_day_mult, event_type="RATE_DAY", reason=reason,
            )

    return NewsGuardResult(
        safe=True, size_mult=1.0, event_type="CLEAR", reason="clear",
    )
