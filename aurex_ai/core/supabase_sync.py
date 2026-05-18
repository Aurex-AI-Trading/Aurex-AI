"""
Aurex AI — Supabase Data Sync

Syncs live trading state (account, positions, trades, analytics, bot status)
from the local SQLite/MT5 state to Supabase so the web dashboard shows
real-time data.

Uses raw httpx REST calls against the Supabase PostgREST API.
(supabase-py cannot be installed — storage3 pulls pyiceberg which fails to build.)

Called once per scan cycle from run_live() via asyncio.ensure_future() so the
sync runs in a background thread and never blocks the trading loop.

Failure isolation
-----------------
  - All exceptions are caught — this module can never crash the trading loop.
  - [SUPABASE PATCH ERROR] warning emitted on 400/404 with full response body.
  - Per-table circuit breaker: any table that returns 4xx is skipped for
    _CIRCUIT_SKIP_CYCLES cycles, preventing error floods.
  - Closed-trade dedup: once a ticket is successfully pushed as closed it is
    never re-PATCHed (closed trades are immutable).
"""
from __future__ import annotations

import os
import time
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from aurex_ai.core.logger import get_logger

log = get_logger("core.supabase_sync")

# ── Load .env ─────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
except Exception:
    pass

try:
    import httpx as _httpx
    _HTTPX_OK = True
except ImportError:
    _httpx = None  # type: ignore[assignment]
    _HTTPX_OK = False

_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
_KEY = os.environ.get("SUPABASE_KEY", "")

# After this many consecutive 4xx errors on a table, skip it for _CIRCUIT_SKIP_CYCLES
_CIRCUIT_BREAK_THRESHOLD = 3
_CIRCUIT_SKIP_CYCLES      = 50


class SupabaseSync:
    """
    One instance lives for the lifetime of the trading session.
    Call sync_all(bridge, trade_logger) each scan cycle — best fired as
    asyncio.ensure_future(asyncio.to_thread(sync.sync_all, bridge, tl))
    so it never blocks the event loop.
    """

    def __init__(self) -> None:
        self._user_id: Optional[str] = None
        self._client:  Optional[Any] = None
        self._enabled = _HTTPX_OK and bool(_URL) and bool(_KEY)
        self._session_start = time.monotonic()   # for uptime_seconds calculation

        # ── Circuit breaker per table ─────────────────────────────────────────
        # Maps table name → sync_count value at which to re-enable it
        self._circuit_skip_until: Dict[str, int] = {}
        self._circuit_consec_err: Dict[str, int] = {}

        # ── Closed-trade dedup ────────────────────────────────────────────────
        # Tickets already confirmed-synced as closed (immutable — never re-push)
        self._synced_closed: Set[int] = set()

        # ── Performance counters ──────────────────────────────────────────────
        self._sync_count:       int = 0
        self._patch_ok:         int = 0
        self._patch_err:        int = 0
        self._insert_ok:        int = 0
        self._insert_err:       int = 0
        self._skipped_closed:   int = 0

        if not self._enabled:
            reason = (
                "httpx not installed" if not _HTTPX_OK else
                "SUPABASE_URL missing" if not _URL else
                "SUPABASE_KEY missing"
            )
            log.warning("[SYNC] Supabase sync disabled — %s", reason)
            return

        self._client = _httpx.Client(
            base_url=f"{_URL}/rest/v1/",
            headers={
                "apikey":        _KEY,
                "Authorization": f"Bearer {_KEY}",
                "Content-Type":  "application/json",
            },
            timeout=5.0,  # hard cap — never block longer than 5s per request
        )
        log.warning("[SYNC] Supabase sync ready | %s", _URL)

    # ── Public interface ──────────────────────────────────────────────────────

    def sync_all(self, bridge: Any, trade_logger: Any) -> None:
        """Full sync — call from asyncio.to_thread() to avoid blocking the event loop."""
        if not self._enabled:
            return
        self._sync_count += 1
        _t0 = time.monotonic()
        try:
            user_id = self._resolve_user_id(bridge)
            if not user_id:
                log.debug("[SYNC] No mt5_accounts row for this account — skipping")
                return
            self._sync_account(bridge, user_id)
            self._sync_positions(bridge, user_id)
            self._sync_bot_heartbeat(user_id)
            self._sync_trades(trade_logger, user_id)
            self._sync_daily_analytics(trade_logger, user_id)
            self._sync_ai_analytics(trade_logger, user_id)
            self._sync_sample_status(trade_logger, user_id)
            self._sync_signal_intelligence(trade_logger, user_id)
        except Exception as exc:
            log.debug("[SYNC] sync_all error: %s", exc)
        finally:
            dur_ms = round((time.monotonic() - _t0) * 1000)
            if self._sync_count % 20 == 0:
                log.warning(
                    "[SYNC PERFORMANCE] cycle=%d dur=%dms | "
                    "patch_ok=%d patch_err=%d insert_ok=%d insert_err=%d "
                    "closed_dedup_skipped=%d circuit_breaks=%s",
                    self._sync_count, dur_ms,
                    self._patch_ok, self._patch_err,
                    self._insert_ok, self._insert_err,
                    self._skipped_closed,
                    {t: v for t, v in self._circuit_skip_until.items()
                     if v > self._sync_count} or "none",
                )

    def on_shutdown(self) -> None:
        """Mark bot stopped and MT5 disconnected on clean shutdown."""
        if not self._enabled or not self._user_id:
            return
        try:
            self._patch("bot_instances", {"user_id": f"eq.{self._user_id}"}, {
                "status":     "stopped",
                "stopped_at": _now_iso(),
            })
            self._patch("mt5_accounts", {"user_id": f"eq.{self._user_id}"}, {
                "connected": False,
                "last_sync": _now_iso(),
            })
        except Exception as exc:
            log.debug("[SYNC] on_shutdown error: %s", exc)

    def close(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass

    # ── User ID resolution ────────────────────────────────────────────────────

    def _resolve_user_id(self, bridge: Any) -> Optional[str]:
        if self._user_id:
            return self._user_id
        try:
            info = bridge.get_account_info_raw()
            acct = str(int(getattr(info, "login", 0) or 0))
        except Exception:
            acct = str(getattr(bridge, "account", 0) or 0)
        if not acct or acct == "0":
            return None
        rows = self._get("mt5_accounts", {"account_number": f"eq.{acct}", "select": "user_id"})
        if rows:
            self._user_id = rows[0]["user_id"]
            log.warning("[SYNC] Linked MT5 account=%s to user_id=%s", acct, self._user_id)
        return self._user_id

    # ── Account ───────────────────────────────────────────────────────────────

    def _sync_account(self, bridge: Any, user_id: str) -> None:
        try:
            info = bridge.get_account_info_raw()
            self._patch("mt5_accounts", {"user_id": f"eq.{user_id}"}, {
                "connected":    True,
                "balance":      round(float(getattr(info, "balance",      0.0) or 0.0), 2),
                "equity":       round(float(getattr(info, "equity",       0.0) or 0.0), 2),
                "margin":       round(float(getattr(info, "margin",       0.0) or 0.0), 2),
                "free_margin":  round(float(getattr(info, "margin_free",  0.0) or 0.0), 2),
                "margin_level": round(float(getattr(info, "margin_level", 0.0) or 0.0), 4),
                "leverage":     int(getattr(info, "leverage", 100) or 100),
                "last_sync":    _now_iso(),
            })
        except Exception as exc:
            log.debug("[SYNC] _sync_account error: %s", exc)

    # ── Open positions ────────────────────────────────────────────────────────

    def _sync_positions(self, bridge: Any, user_id: str) -> None:
        try:
            positions = bridge.get_open_positions()
            self._delete("open_positions", {"user_id": f"eq.{user_id}"})
            for p in positions:
                ticket = p.get("ticket")
                if not ticket:
                    continue
                self._upsert("open_positions", {
                    "user_id":        user_id,
                    "mt5_ticket":     ticket,
                    "symbol":         p.get("symbol", ""),
                    "direction":      p.get("type", "BUY"),
                    "lot_size":       p.get("volume"),
                    "entry_price":    p.get("price_open"),
                    "sl":             p.get("sl"),
                    "tp":             p.get("tp"),
                    "unrealized_pnl": round(float(p.get("profit", 0.0) or 0.0), 2),
                    "opened_at":      _now_iso(),
                }, conflict="mt5_ticket")
        except Exception as exc:
            log.debug("[SYNC] _sync_positions error: %s", exc)

    # ── Bot heartbeat ─────────────────────────────────────────────────────────

    def _sync_bot_heartbeat(self, user_id: str) -> None:
        try:
            uptime_sec = int(time.monotonic() - self._session_start)
            self._upsert("bot_instances", {
                "user_id":        user_id,
                "status":         "running",
                "last_heartbeat": _now_iso(),
                "pid":            os.getpid(),
                "uptime_seconds": uptime_sec,
                "stopped_at":     None,   # clear stale stop timestamp from previous session
            }, conflict="user_id")
        except Exception as exc:
            log.debug("[SYNC] _sync_bot_heartbeat error: %s", exc)

    # ── Trades ────────────────────────────────────────────────────────────────

    def _sync_trades(self, trade_logger: Any, user_id: str) -> None:
        try:
            for t in trade_logger.get_open_trades():
                self._push_trade(t, user_id, status="open")
            for t in trade_logger.get_closed_trades(limit=50):
                self._push_trade(t, user_id, status="closed")
        except Exception as exc:
            log.debug("[SYNC] _sync_trades error: %s", exc)

    def _push_trade(self, t: Dict, user_id: str, status: str) -> None:
        ticket = t.get("ticket")
        if not ticket:
            return

        # Closed trades are immutable — once synced, never touch them again.
        # This eliminates the per-cycle PATCH storm on historical closed trades.
        if status == "closed" and ticket in self._synced_closed:
            self._skipped_closed += 1
            return

        tier_raw = t.get("tier")
        payload: Dict[str, Any] = {
            "user_id":      user_id,
            "mt5_ticket":   ticket,
            "symbol":       t.get("symbol", ""),
            "direction":    t.get("direction", "BUY"),
            "entry_price":  t.get("entry_price"),
            "sl":           t.get("stop_loss"),
            "tp":           t.get("take_profit"),
            "lot_size":     t.get("lot_size"),
            "status":       status,
            "rr_ratio":     t.get("risk_reward"),
            "score":        t.get("total_score"),
            "confidence":   t.get("confidence"),
            "tier":         f"T{tier_raw}" if tier_raw else None,
            "opened_at":    t.get("opened_at") or None,
            # NOTE: trade_source is NOT included — it is not in the Supabase
            # trades schema. Including it causes PGRST204 400 on every request.
        }
        if status == "closed":
            payload["pnl_zar"]   = round(float(t.get("profit_zar", 0.0) or 0.0), 2)
            payload["pnl_pips"]  = round(float(t.get("pips",        0.0) or 0.0), 2)
            payload["closed_at"] = t.get("closed_at") or None

        existing = self._get("trades", {
            "mt5_ticket": f"eq.{ticket}",
            "user_id":    f"eq.{user_id}",
            "select":     "id",
        })
        if existing:
            update = {k: v for k, v in payload.items() if k not in ("user_id", "mt5_ticket")}
            ok = self._patch("trades", {"mt5_ticket": f"eq.{ticket}", "user_id": f"eq.{user_id}"}, update)
        else:
            ok = self._insert("trades", payload)

        if ok and status == "closed":
            self._synced_closed.add(ticket)

    # ── Daily analytics ───────────────────────────────────────────────────────

    def _sync_daily_analytics(self, trade_logger: Any, user_id: str) -> None:
        try:
            if trade_logger.total_closed() == 0:
                return
            from aurex_ai.analytics.performance import PerformanceEngine
            report = PerformanceEngine.compute(trade_logger, window=200)
            wins   = max(0, int(round(report.total_closed * report.win_rate)))
            losses = report.total_closed - wins

            # Compute gross_profit / gross_loss / best_trade / worst_trade from raw trades
            _closed = trade_logger.get_closed_trades(limit=500)
            _pnls   = [float(t.get("profit_zar", 0.0) or 0.0) for t in _closed]
            _gross_profit = round(sum(p for p in _pnls if p > 0), 2)
            _gross_loss   = round(sum(p for p in _pnls if p < 0), 2)
            _best_trade   = round(max(_pnls, default=0.0), 2)
            _worst_trade  = round(min(_pnls, default=0.0), 2)

            self._upsert("daily_analytics", {
                "user_id":          user_id,
                "date":             datetime.now(timezone.utc).date().isoformat(),
                "win_rate":         round(report.win_rate, 4),
                "profit_factor":    round(max(0.0, report.profit_factor), 4),
                "total_trades":     report.total_closed,
                "winning_trades":   wins,
                "losing_trades":    losses,
                "gross_profit":     _gross_profit,
                "gross_loss":       _gross_loss,
                "total_pnl":        round(report.total_pnl_zar, 2),
                "max_drawdown_pct": round(abs(report.max_drawdown_pct), 4),
                "best_trade":       _best_trade,
                "worst_trade":      _worst_trade,
            }, conflict="user_id,date")
        except Exception as exc:
            log.debug("[SYNC] _sync_daily_analytics error: %s", exc)

    # ── AI analytics dashboard ────────────────────────────────────────────────

    def _sync_ai_analytics(self, trade_logger: Any, user_id: str) -> None:
        """
        Sync AI_FORWARD_V2 performance breakdown to Supabase.

        Table schema (create once in Supabase dashboard):
          CREATE TABLE IF NOT EXISTS ai_analytics_snapshot (
            user_id          UUID NOT NULL,
            snapshot_date    DATE NOT NULL,
            strategy_version TEXT NOT NULL DEFAULT 'AI_FORWARD_V2',
            trade_source     TEXT NOT NULL DEFAULT 'ALL',
            total_trades     INTEGER,
            win_rate         NUMERIC(6,4),
            profit_factor    NUMERIC(8,4),
            total_pnl        NUMERIC(12,2),
            max_dd_pct       NUMERIC(6,2),
            expectancy       NUMERIC(10,2),
            avg_rr           NUMERIC(6,3),
            best_session     TEXT,
            best_setup       TEXT,
            computed_at      TIMESTAMPTZ,
            PRIMARY KEY (user_id, snapshot_date, strategy_version, trade_source)
          );
        """
        try:
            from aurex_ai.analytics.performance import PerformanceEngine
            from aurex_ai.analytics.strategy_version import CURRENT_VERSION
            from aurex_ai.core.trade_source import AI_AUTO, MANUAL, HYBRID

            today = datetime.now(timezone.utc).date().isoformat()

            ai_report = PerformanceEngine.compute_ai_forward(trade_logger)
            self._upsert("ai_analytics_snapshot", {
                "user_id":          user_id,
                "snapshot_date":    today,
                "strategy_version": CURRENT_VERSION,
                "trade_source":     "AI_FORWARD",
                "total_trades":     ai_report.total_closed,
                "win_rate":         round(ai_report.win_rate,       4),
                "profit_factor":    round(max(0.0, ai_report.profit_factor), 4),
                "total_pnl":        round(ai_report.total_pnl_zar,  2),
                "max_dd_pct":       round(abs(ai_report.max_drawdown_pct), 4),
                "expectancy":       round(ai_report.expectancy,     2),
                "avg_rr":           round(ai_report.avg_rr,         3),
                "best_session":     ai_report.best_session or None,
                "best_setup":       ai_report.best_setup   or None,
                "computed_at":      _now_iso(),
            }, conflict="user_id,snapshot_date,strategy_version,trade_source")

            for src in (AI_AUTO, MANUAL, HYBRID):
                n = trade_logger.total_closed(sources=[src])
                if n == 0:
                    continue
                src_report = PerformanceEngine.compute(trade_logger, source_filter=[src], window=200)
                self._upsert("ai_analytics_snapshot", {
                    "user_id":          user_id,
                    "snapshot_date":    today,
                    "strategy_version": "ALL",
                    "trade_source":     src,
                    "total_trades":     src_report.total_closed,
                    "win_rate":         round(src_report.win_rate,       4),
                    "profit_factor":    round(max(0.0, src_report.profit_factor), 4),
                    "total_pnl":        round(src_report.total_pnl_zar,  2),
                    "max_dd_pct":       round(abs(src_report.max_drawdown_pct), 4),
                    "expectancy":       round(src_report.expectancy,     2),
                    "avg_rr":           round(src_report.avg_rr,         3),
                    "best_session":     src_report.best_session or None,
                    "best_setup":       src_report.best_setup   or None,
                    "computed_at":      _now_iso(),
                }, conflict="user_id,snapshot_date,strategy_version,trade_source")
        except Exception as exc:
            log.debug("[SYNC] _sync_ai_analytics error: %s", exc)

    def _sync_sample_status(self, trade_logger: Any, user_id: str) -> None:
        """
        Sync AI_FORWARD_V2 sample validity status to Supabase.

        Table schema (create once in Supabase dashboard):
          CREATE TABLE IF NOT EXISTS ai_sample_status (
            user_id          UUID NOT NULL PRIMARY KEY,
            strategy_version TEXT NOT NULL,
            sample_n         INTEGER,
            status           TEXT,
            confidence_pct   NUMERIC(5,1),
            min_valid_sample INTEGER DEFAULT 100,
            updated_at       TIMESTAMPTZ
          );
        """
        try:
            from aurex_ai.analytics.performance import PerformanceEngine
            from aurex_ai.analytics.strategy_version import MIN_VALID_SAMPLE
            ss = PerformanceEngine.get_sample_status(trade_logger)
            self._upsert("ai_sample_status", {
                "user_id":          user_id,
                "strategy_version": ss.strategy_version,
                "sample_n":         ss.n,
                "status":           ss.status,
                "confidence_pct":   ss.confidence_pct,
                "min_valid_sample": MIN_VALID_SAMPLE,
                "updated_at":       _now_iso(),
            }, conflict="user_id")
        except Exception as exc:
            log.debug("[SYNC] _sync_sample_status error: %s", exc)

    def _sync_signal_intelligence(self, trade_logger: Any, user_id: str) -> None:
        """
        Sync Phase 12 signal intelligence metrics to Supabase.

        Required table (create once):
        CREATE TABLE ai_signal_intelligence (
            user_id                TEXT,
            snapshot_date          DATE,
            total_signals          INT,
            executed_count         INT,
            rejected_count         INT,
            exec_win_rate          FLOAT,
            false_negative_rate    FLOAT,
            false_positive_rate    FLOAT,
            missed_opportunities   INT,
            successful_rejections  INT,
            near_band_count        INT,
            near_band_win_rate     FLOAT,
            paper_trade_count      INT,
            paper_win_rate         FLOAT,
            paper_expectancy       FLOAT,
            computed_at            TIMESTAMPTZ,
            PRIMARY KEY (user_id, snapshot_date)
        );
        """
        try:
            from aurex_ai.analytics.signal_store import SignalStore
            from aurex_ai.analytics.signal_telemetry import SignalTelemetry
            store       = SignalStore.get_instance()
            metrics     = store.get_filter_effectiveness(window=500)
            if not metrics or metrics.get("total_signals", 0) < 5:
                return

            paper_stats   = trade_logger.get_paper_trade_stats(window=200)
            funnel_stats  = SignalTelemetry.get_instance().get_funnel_stats()
            top_rej       = funnel_stats.get("top_rejections", {})
            top_rej_str   = "|".join(f"{k}:{v}" for k, v in list(top_rej.items())[:5])

            self._upsert("ai_signal_intelligence", {
                "user_id":               user_id,
                "snapshot_date":         datetime.now(timezone.utc).date().isoformat(),
                "total_signals":         metrics.get("total_signals", 0),
                "executed_count":        metrics.get("executed_count", 0),
                "rejected_count":        metrics.get("rejected_count", 0),
                "exec_win_rate":         round(metrics.get("exec_win_rate", 0.0), 4),
                "false_negative_rate":   round(metrics.get("false_negative_rate", 0.0), 4),
                "false_positive_rate":   round(metrics.get("false_positive_rate", 0.0), 4),
                "missed_opportunities":  metrics.get("missed_opportunities", 0),
                "successful_rejections": metrics.get("successful_rejections", 0),
                "near_band_count":       metrics.get("near_band_count", 0),
                "near_band_win_rate":    round(metrics.get("near_band_win_rate", 0.0), 4),
                "paper_trade_count":     paper_stats.get("n", 0),
                "paper_win_rate":        round(paper_stats.get("win_rate", 0.0), 4),
                "paper_expectancy":      round(paper_stats.get("expectancy", 0.0), 4),
                "funnel_total_scanned":  funnel_stats.get("total_scanned", 0),
                "funnel_total_executed": funnel_stats.get("total_executed", 0),
                "funnel_exec_rate":      round(funnel_stats.get("exec_rate", 0.0), 4),
                "funnel_near_misses":    funnel_stats.get("near_miss_count", 0),
                "top_rejection_reasons": top_rej_str,
                "computed_at":           _now_iso(),
            }, conflict="user_id,snapshot_date")
        except Exception as exc:
            log.debug("[SYNC] _sync_signal_intelligence error: %s", exc)

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _is_circuit_open(self, table: str) -> bool:
        """Return True if this table's circuit breaker is tripped (skip it)."""
        skip_until = self._circuit_skip_until.get(table, 0)
        return self._sync_count < skip_until

    def _record_table_error(self, table: str, status: int) -> None:
        """Increment error counter; trip circuit breaker after threshold."""
        n = self._circuit_consec_err.get(table, 0) + 1
        self._circuit_consec_err[table] = n
        if n >= _CIRCUIT_BREAK_THRESHOLD:
            skip_until = self._sync_count + _CIRCUIT_SKIP_CYCLES
            self._circuit_skip_until[table] = skip_until
            log.warning(
                "[SYNC] [CIRCUIT BREAKER] table=%s status=%d errors=%d — "
                "skipping for %d cycles (resumes at cycle=%d)",
                table, status, n, _CIRCUIT_SKIP_CYCLES, skip_until,
            )

    def _record_table_ok(self, table: str) -> None:
        self._circuit_consec_err[table] = 0

    def _get(self, table: str, params: Dict[str, str]) -> List[Dict]:
        if self._is_circuit_open(table):
            return []
        try:
            r = self._client.get(table, params=params)
            if r.status_code == 200:
                self._record_table_ok(table)
                return r.json()
            log.debug("[SYNC] GET %s status=%d", table, r.status_code)
            if r.status_code in (400, 404):
                self._record_table_error(table, r.status_code)
        except Exception as exc:
            log.debug("[SYNC] GET %s error: %s", table, exc)
        return []

    def _patch(self, table: str, filters: Dict[str, str], payload: Dict) -> bool:
        """Returns True on success, False on error."""
        if self._is_circuit_open(table):
            return False
        try:
            r = self._client.patch(table, params=filters, json=payload,
                                   headers={"Prefer": "return=minimal"})
            if r.status_code in (200, 204):
                self._patch_ok += 1
                self._record_table_ok(table)
                return True
            self._patch_err += 1
            self._record_table_error(table, r.status_code)
            log.warning(
                "[SUPABASE PATCH ERROR] table=%s status=%d | "
                "body=%s | payload_keys=%s | filters=%s",
                table, r.status_code,
                r.text[:300],
                list(payload.keys()),
                filters,
            )
        except Exception as exc:
            self._patch_err += 1
            log.debug("[SYNC] PATCH %s error: %s", table, exc)
        return False

    def _delete(self, table: str, filters: Dict[str, str]) -> None:
        if self._is_circuit_open(table):
            return
        try:
            r = self._client.delete(table, params=filters,
                                    headers={"Prefer": "return=minimal"})
            if r.status_code in (200, 204):
                self._record_table_ok(table)
            else:
                self._record_table_error(table, r.status_code)
                log.debug("[SYNC] DELETE %s status=%d body=%s",
                          table, r.status_code, r.text[:200])
        except Exception as exc:
            log.debug("[SYNC] DELETE %s error: %s", table, exc)

    def _insert(self, table: str, payload: Dict) -> bool:
        """Returns True on success, False on error."""
        if self._is_circuit_open(table):
            return False
        try:
            r = self._client.post(
                table,
                json=payload,
                headers={"Prefer": "return=minimal"},
            )
            if r.status_code in (200, 201, 204):
                self._insert_ok += 1
                self._record_table_ok(table)
                return True
            self._insert_err += 1
            self._record_table_error(table, r.status_code)
            log.warning(
                "[SUPABASE INSERT ERROR] table=%s status=%d | "
                "body=%s | payload_keys=%s",
                table, r.status_code,
                r.text[:300],
                list(payload.keys()),
            )
        except Exception as exc:
            self._insert_err += 1
            log.debug("[SYNC] INSERT %s error: %s", table, exc)
        return False

    def _upsert(self, table: str, payload: Dict, conflict: str = "id") -> bool:
        """Returns True on success, False on error."""
        if self._is_circuit_open(table):
            return False
        try:
            r = self._client.post(
                table,
                params={"on_conflict": conflict},
                json=payload,
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )
            if r.status_code in (200, 201, 204):
                self._insert_ok += 1
                self._record_table_ok(table)
                return True
            self._insert_err += 1
            self._record_table_error(table, r.status_code)
            log.warning(
                "[SUPABASE UPSERT ERROR] table=%s status=%d | "
                "body=%s | payload_keys=%s",
                table, r.status_code,
                r.text[:300],
                list(payload.keys()),
            )
        except Exception as exc:
            self._insert_err += 1
            log.debug("[SYNC] UPSERT %s error: %s", table, exc)
        return False


# ── Module helpers ────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
