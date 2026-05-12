"""
Aurex AI — Supabase Data Sync

Syncs live trading state (account, positions, trades, analytics, bot status)
from the local SQLite/MT5 state to Supabase so the web dashboard shows
real-time data.

Uses raw httpx REST calls against the Supabase PostgREST API.
(supabase-py cannot be installed — storage3 pulls pyiceberg which fails to build.)

Called once per scan cycle from run_live() via asyncio.to_thread().
All exceptions are caught so this module can never crash the trading loop.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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


class SupabaseSync:
    """
    One instance lives for the lifetime of the trading session.
    Call sync_all(bridge, trade_logger) each scan cycle.
    """

    def __init__(self) -> None:
        self._user_id: Optional[str] = None
        self._client:  Optional[Any] = None
        self._enabled = _HTTPX_OK and bool(_URL) and bool(_KEY)

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
            timeout=8.0,
        )
        log.info("[SYNC] Supabase sync ready | %s", _URL)

    # ── Public interface ──────────────────────────────────────────────────────

    def sync_all(self, bridge: Any, trade_logger: Any) -> None:
        """Full sync — call once per scan cycle."""
        if not self._enabled:
            return
        try:
            user_id = self._resolve_user_id(bridge)
            if not user_id:
                log.debug("[SYNC] No mt5_accounts row for account=%s — skipping", bridge.account)
                return
            self._sync_account(bridge, user_id)
            self._sync_positions(bridge, user_id)
            self._sync_bot_heartbeat(user_id)
            self._sync_trades(trade_logger, user_id)
            self._sync_daily_analytics(trade_logger, user_id)
        except Exception as exc:
            log.debug("[SYNC] sync_all error: %s", exc)

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
        # Prefer the live login number from MT5 account_info (works when the bridge
        # connects to an already-running terminal with account=0 in config).
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
            log.info("[SYNC] Linked MT5 account=%s to user_id=%s", acct, self._user_id)
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
            # Atomic replace: delete all user's rows then insert current state
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
            self._upsert("bot_instances", {
                "user_id":        user_id,
                "status":         "running",
                "last_heartbeat": _now_iso(),
                "pid":            os.getpid(),
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
            "trade_source": t.get("trade_source", "AI_AUTO"),
        }
        if status == "closed":
            payload["pnl_zar"]   = round(float(t.get("profit_zar", 0.0) or 0.0), 2)
            payload["pnl_pips"]  = round(float(t.get("pips",        0.0) or 0.0), 2)
            payload["closed_at"] = t.get("closed_at") or None
        # trades.mt5_ticket has no UNIQUE constraint — use check-then-insert/update
        existing = self._get("trades", {
            "mt5_ticket": f"eq.{ticket}",
            "user_id":    f"eq.{user_id}",
            "select":     "id",
        })
        if existing:
            update = {k: v for k, v in payload.items() if k not in ("user_id", "mt5_ticket")}
            self._patch("trades", {"mt5_ticket": f"eq.{ticket}", "user_id": f"eq.{user_id}"}, update)
        else:
            self._insert("trades", payload)

    # ── Daily analytics ───────────────────────────────────────────────────────

    def _sync_daily_analytics(self, trade_logger: Any, user_id: str) -> None:
        try:
            if trade_logger.total_closed() == 0:
                return
            from aurex_ai.analytics.performance import PerformanceEngine
            report = PerformanceEngine.compute(trade_logger, window=200)
            wins   = max(0, int(round(report.total_closed * report.win_rate)))
            losses = report.total_closed - wins
            self._upsert("daily_analytics", {
                "user_id":          user_id,
                "date":             datetime.now(timezone.utc).date().isoformat(),
                "win_rate":         round(report.win_rate, 4),
                "profit_factor":    round(max(0.0, report.profit_factor), 4),
                "total_trades":     report.total_closed,
                "winning_trades":   wins,
                "losing_trades":    losses,
                "gross_profit":     0.0,
                "gross_loss":       0.0,
                "total_pnl":        round(report.total_pnl_zar, 2),
                "max_drawdown_pct": round(abs(report.max_drawdown_pct), 4),
                "best_trade":       0.0,
                "worst_trade":      0.0,
            }, conflict="user_id,date")
        except Exception as exc:
            log.debug("[SYNC] _sync_daily_analytics error: %s", exc)

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _get(self, table: str, params: Dict[str, str]) -> List[Dict]:
        try:
            r = self._client.get(table, params=params)
            if r.status_code == 200:
                return r.json()
            log.debug("[SYNC] GET %s status=%d", table, r.status_code)
        except Exception as exc:
            log.debug("[SYNC] GET %s error: %s", table, exc)
        return []

    def _patch(self, table: str, filters: Dict[str, str], payload: Dict) -> None:
        try:
            r = self._client.patch(table, params=filters, json=payload,
                                   headers={"Prefer": "return=minimal"})
            if r.status_code not in (200, 204):
                log.debug("[SYNC] PATCH %s status=%d body=%s", table, r.status_code, r.text[:200])
        except Exception as exc:
            log.debug("[SYNC] PATCH %s error: %s", table, exc)

    def _delete(self, table: str, filters: Dict[str, str]) -> None:
        try:
            r = self._client.delete(table, params=filters,
                                    headers={"Prefer": "return=minimal"})
            if r.status_code not in (200, 204):
                log.debug("[SYNC] DELETE %s status=%d", table, r.status_code)
        except Exception as exc:
            log.debug("[SYNC] DELETE %s error: %s", table, exc)

    def _insert(self, table: str, payload: Dict) -> None:
        try:
            r = self._client.post(
                table,
                json=payload,
                headers={"Prefer": "return=minimal"},
            )
            if r.status_code not in (200, 201, 204):
                log.debug("[SYNC] INSERT %s status=%d body=%s", table, r.status_code, r.text[:200])
        except Exception as exc:
            log.debug("[SYNC] INSERT %s error: %s", table, exc)

    def _upsert(self, table: str, payload: Dict, conflict: str = "id") -> None:
        try:
            r = self._client.post(
                table,
                params={"on_conflict": conflict},
                json=payload,
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )
            if r.status_code not in (200, 201, 204):
                log.debug("[SYNC] UPSERT %s status=%d body=%s", table, r.status_code, r.text[:200])
        except Exception as exc:
            log.debug("[SYNC] UPSERT %s error: %s", table, exc)


# ── Module helpers ────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
