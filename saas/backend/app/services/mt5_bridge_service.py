"""
Aurex AI SaaS — Multi-User MT5 Bridge Service

Architecture:
  - Singleton service, one instance for the entire FastAPI process
  - One MT5Bridge per user_id — fully isolated, independent sessions
  - Async session management with auto-reconnect
  - Background health monitor loops reconnect on dead sessions
  - Thread-safe session dictionary via asyncio.Lock

Design notes:
  - Wraps the existing aurex_ai.core.mt5_bridge.MT5Bridge
  - All blocking MT5 calls run in asyncio.to_thread() to avoid blocking the event loop
  - Sessions survive across API requests (held in-memory, keyed by user_id)
  - Designed for VPS/container: no local MT5 terminal required (MetaTrader5 lib connects remotely)
"""
from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import UUID

log = logging.getLogger("aurex.saas.mt5_bridge")

# Attempt to import the existing MT5Bridge from parent project
_PARENT_ROOT = Path(__file__).resolve().parents[5]  # c:/Projects/AUREX_AI
sys.path.insert(0, str(_PARENT_ROOT))

try:
    from aurex_ai.core.mt5_bridge import MT5Bridge as _CoreBridge
    _HAS_MT5 = True
except ImportError:
    _HAS_MT5 = False
    _CoreBridge = None  # type: ignore
    log.warning("aurex_ai.core.mt5_bridge not importable — MT5 sessions will be simulated")


# ── Per-user session ──────────────────────────────────────────────────────────

@dataclass
class MT5UserSession:
    user_id: str
    account_number: int
    server: str
    connected: bool = False
    connected_at: Optional[datetime] = None
    last_heartbeat: Optional[datetime] = None
    last_error: Optional[str] = None
    reconnect_attempts: int = 0
    bridge: Any = field(default=None, repr=False)
    _reconnect_task: Optional[asyncio.Task] = field(default=None, repr=False)

    def is_healthy(self) -> bool:
        if not self.connected or not self.last_heartbeat:
            return False
        age = (datetime.now(timezone.utc) - self.last_heartbeat).total_seconds()
        return age < 120  # Dead if no heartbeat for 2 minutes

    def to_dict(self) -> dict:
        return {
            "user_id":          self.user_id,
            "account_number":   self.account_number,
            "server":           self.server,
            "connected":        self.connected,
            "connected_at":     self.connected_at.isoformat() if self.connected_at else None,
            "last_heartbeat":   self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "last_error":       self.last_error,
            "reconnect_attempts": self.reconnect_attempts,
        }


# ── Singleton service ─────────────────────────────────────────────────────────

class MT5BridgeService:
    """
    Multi-user MT5 connection manager.

    Usage:
        service = MT5BridgeService.get_instance()
        await service.start()

        ok = await service.connect_user(user_id, account, password, server)
        bridge = service.get_user_bridge(user_id)
        await service.disconnect_user(user_id)

        await service.stop()
    """
    _instance: Optional["MT5BridgeService"] = None

    def __init__(self) -> None:
        self._sessions: Dict[str, MT5UserSession] = {}
        self._lock = asyncio.Lock()
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False

    @classmethod
    def get_instance(cls) -> "MT5BridgeService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._monitor_task = asyncio.create_task(
            self._health_monitor(), name="mt5_health_monitor"
        )
        log.info("MT5BridgeService started")

    async def stop(self) -> None:
        self._running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        async with self._lock:
            for uid in list(self._sessions.keys()):
                await self._disconnect_user_locked(uid)
        log.info("MT5BridgeService stopped — all sessions closed")

    # ── Connect / disconnect ──────────────────────────────────────────────────

    async def connect_user(
        self,
        user_id: str,
        account_number: int,
        password: str,
        server: str,
    ) -> tuple[bool, Optional[str]]:
        """
        Establish an MT5 session for user_id.
        Returns (success, error_message).
        If the user already has a session it is replaced.
        """
        async with self._lock:
            # Tear down any existing session first
            if user_id in self._sessions:
                await self._disconnect_user_locked(user_id)

            session = MT5UserSession(
                user_id=user_id,
                account_number=account_number,
                server=server,
            )

            if not _HAS_MT5:
                # Simulation mode: accept any credentials
                session.connected = True
                session.connected_at = datetime.now(timezone.utc)
                session.last_heartbeat = datetime.now(timezone.utc)
                self._sessions[user_id] = session
                log.info("MT5 simulation session created for user %s", user_id)
                return True, None

            try:
                bridge = _CoreBridge(
                    account=account_number,
                    password=password,
                    server=server,
                    dry_run=False,
                )
                ok = await asyncio.to_thread(bridge.connect)
                if ok:
                    session.bridge = bridge
                    session.connected = True
                    session.connected_at = datetime.now(timezone.utc)
                    session.last_heartbeat = datetime.now(timezone.utc)
                    self._sessions[user_id] = session
                    log.info("MT5 session opened: user=%s account=%d server=%s",
                             user_id, account_number, server)
                    return True, None
                else:
                    err = "MT5 connection rejected — check account number, password and server"
                    session.last_error = err
                    log.warning("MT5 connect failed: user=%s error=%s", user_id, err)
                    return False, err

            except Exception as exc:
                err = str(exc)
                log.error("MT5 connect exception: user=%s error=%s", user_id, err, exc_info=True)
                return False, err

    async def disconnect_user(self, user_id: str) -> None:
        async with self._lock:
            await self._disconnect_user_locked(user_id)

    async def _disconnect_user_locked(self, user_id: str) -> None:
        """Must be called with self._lock held."""
        session = self._sessions.pop(user_id, None)
        if session and session.bridge and _HAS_MT5:
            try:
                await asyncio.to_thread(session.bridge.disconnect)
            except Exception:
                pass
        if session:
            log.info("MT5 session closed: user=%s", user_id)

    # ── Bridge / account info access ──────────────────────────────────────────

    def get_user_bridge(self, user_id: str) -> Optional[Any]:
        """Return the raw MT5Bridge for a connected user, or None."""
        session = self._sessions.get(user_id)
        return session.bridge if session and session.connected else None

    def get_user_session(self, user_id: str) -> Optional[MT5UserSession]:
        return self._sessions.get(user_id)

    def is_connected(self, user_id: str) -> bool:
        session = self._sessions.get(user_id)
        return bool(session and session.connected)

    async def get_account_info(self, user_id: str) -> Optional[dict]:
        """Fetch live account info for a user's MT5 session."""
        session = self._sessions.get(user_id)
        if not session or not session.connected:
            return None

        if not _HAS_MT5 or not session.bridge:
            return {"balance": 10000.0, "equity": 10000.0, "currency": "USD", "leverage": 100}

        try:
            info = await asyncio.to_thread(session.bridge.get_account_info_raw)
            if info:
                return {
                    "balance":  info.balance,
                    "equity":   info.equity,
                    "currency": info.currency,
                    "leverage": info.leverage,
                }
        except Exception as exc:
            log.warning("get_account_info failed: user=%s error=%s", user_id, exc)
        return None

    async def get_all_statuses(self) -> Dict[str, dict]:
        """Return status dict for every active session (admin view)."""
        statuses = {}
        for uid, session in self._sessions.items():
            info = await self.get_account_info(uid)
            entry = session.to_dict()
            if info:
                entry["balance"] = info.get("balance")
                entry["equity"] = info.get("equity")
            statuses[uid] = entry
        return statuses

    # ── Background health monitor ─────────────────────────────────────────────

    async def _health_monitor(self) -> None:
        """
        Background task:
          1. Sends heartbeat pings to all sessions every 60s
          2. Triggers reconnect for unhealthy sessions
        """
        from app.core.config import settings
        interval = settings.MT5_HEALTH_CHECK_INTERVAL_SECONDS

        while self._running:
            await asyncio.sleep(interval)
            unhealthy = []

            async with self._lock:
                for uid, session in list(self._sessions.items()):
                    if not session.connected:
                        continue
                    try:
                        if _HAS_MT5 and session.bridge:
                            alive = await asyncio.to_thread(
                                lambda b=session.bridge: b.get_account_info_raw() is not None
                            )
                        else:
                            alive = True  # simulation always alive
                        if alive:
                            session.last_heartbeat = datetime.now(timezone.utc)
                            session.reconnect_attempts = 0
                        else:
                            session.connected = False
                            unhealthy.append(uid)
                    except Exception as exc:
                        log.warning("Health check failed: user=%s error=%s", uid, exc)
                        session.connected = False
                        session.last_error = str(exc)
                        unhealthy.append(uid)

            for uid in unhealthy:
                log.warning("Session unhealthy — scheduling reconnect: user=%s", uid)
                asyncio.create_task(self._reconnect_user(uid))

    async def _reconnect_user(self, user_id: str) -> None:
        """Attempt to reconnect a dead session using stored bridge credentials."""
        from app.core.config import settings

        session = self._sessions.get(user_id)
        if not session:
            return

        max_attempts = settings.MT5_MAX_RECONNECT_ATTEMPTS
        delay = settings.MT5_RECONNECT_INTERVAL_SECONDS

        for attempt in range(1, max_attempts + 1):
            if not self._running:
                return
            if self.is_connected(user_id):
                return  # Already reconnected by another path

            log.info("Reconnect attempt %d/%d: user=%s", attempt, max_attempts, user_id)
            session.reconnect_attempts = attempt

            if not _HAS_MT5 or not session.bridge:
                session.connected = True
                session.last_heartbeat = datetime.now(timezone.utc)
                return

            try:
                ok = await asyncio.to_thread(session.bridge.connect)
                if ok:
                    session.connected = True
                    session.last_heartbeat = datetime.now(timezone.utc)
                    session.reconnect_attempts = 0
                    log.info("Reconnect successful: user=%s", user_id)
                    return
            except Exception as exc:
                session.last_error = str(exc)
                log.warning("Reconnect attempt failed: user=%s attempt=%d error=%s",
                            user_id, attempt, exc)

            await asyncio.sleep(delay * attempt)  # exponential backoff

        log.error("All reconnect attempts exhausted: user=%s", user_id)
        session.last_error = "Max reconnect attempts reached — manual reconnect required"
