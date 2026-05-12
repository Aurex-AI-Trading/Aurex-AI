"""
Aurex AI — News Guard  (Phase 5: Trade Quality)

Lightweight news event protection without external API dependency.
Identifies high-impact economic release windows from deterministic UTC patterns.

Covered events:
  NFP        — First Friday of each month, 13:30 UTC  (±window minutes)
  FOMC risk  — Every Wednesday, 17:45–20:00 UTC       (approximate — not every Wed)
  Rollover   — 22:00–23:59 UTC  (outside session gate anyway; secondary guard)

Configuration (settings.yaml news: section):
  avoid_nfp:         true    — block NFP window (nfp_window_pre + nfp_window_post)
  nfp_window_pre:    15      — minutes before 13:30 UTC to start protection
  nfp_window_post:   30      — minutes after 13:30 UTC before resuming
  avoid_fomc:        true    — reduce size on all Wednesdays 17:45-20:00 UTC
  fomc_size_mult:    0.50    — lot multiplier during FOMC risk window
  news_size_mult:    0.50    — generic fallback size multiplier for reduced windows

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


# ── Public API ────────────────────────────────────────────────────────────────

def check_news(cfg: object, now_utc: datetime) -> NewsGuardResult:
    """
    Check if now_utc falls within a high-impact news protection window.

    Returns NewsGuardResult with safe=False when execution should be blocked,
    or safe=True with size_mult < 1.0 when size should be reduced.
    """
    news         = getattr(cfg, "news", None)
    avoid_nfp    = bool(getattr(news, "avoid_nfp",      True))
    avoid_fomc   = bool(getattr(news, "avoid_fomc",     True))
    nfp_pre      = int(getattr(news,  "nfp_window_pre",  15))
    nfp_post     = int(getattr(news,  "nfp_window_post", 30))
    fomc_mult    = float(getattr(news, "fomc_size_mult",  0.50))
    news_mult    = float(getattr(news, "news_size_mult",  0.50))

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
                reason = (
                    f"NFP {phase}-release | 13:30 UTC ±{nfp_pre}/{nfp_post} min"
                )
                log.warning(
                    "[NEWS FILTER] [HIGH IMPACT EVENT] [TRADING PAUSED FOR NEWS] "
                    "NFP %s window | %s",
                    phase, reason,
                )
                return NewsGuardResult(
                    safe       = False,
                    size_mult  = 0.0,
                    event_type = "NFP",
                    reason     = reason,
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
                safe       = True,
                size_mult  = fomc_mult,
                event_type = "FOMC_RISK",
                reason     = reason,
            )

    return NewsGuardResult(
        safe=True, size_mult=1.0, event_type="CLEAR", reason="clear",
    )
