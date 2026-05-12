"""
Aurex AI — Enterprise Telemetry & Heartbeat  (Phase 6)

Provides institutional-grade runtime monitoring:

  1. Cycle timing
       Records start/end of each scan cycle.  Emits [LATENCY WARNING] when
       a cycle exceeds the configured threshold.

  2. Memory monitoring
       Reads process RSS via psutil (if installed) or falls back to a no-op.
       Emits [VPS STATUS] when memory exceeds warn threshold.

  3. System health score  (0 – 100)
       Composite of: cycle latency quality, memory pressure, MT5 connectivity,
       and error rate.  Emitted with [ENTERPRISE HEARTBEAT] each heartbeat cycle.

  4. API/Supabase latency tracking
       Records round-trip times for Supabase sync calls.
       Emits [API STATUS] when latency degrades.

  5. Bot uptime tracking
       Seconds since process start, plus total scan cycles completed.

Logs emitted
------------
    [ENTERPRISE HEARTBEAT]  — full health summary every N cycles
    [SYSTEM HEALTH]         — when overall score changes significantly
    [VPS STATUS]            — memory / VPS resource usage
    [API STATUS]            — Supabase sync latency
    [LATENCY WARNING]       — slow cycle detected
    [RISK HEALTH]           — account risk state summary

No external dependencies required.  psutil is optional (graceful fallback).
Thread-safe.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, List, Optional

from aurex_ai.core.logger import get_logger

log = get_logger("core.telemetry")

try:
    import psutil as _psutil
    _PSUTIL_OK = True
except ImportError:
    _psutil    = None   # type: ignore[assignment]
    _PSUTIL_OK = False


@dataclass
class HealthSnapshot:
    """Point-in-time system health summary."""
    score:             int        # 0–100
    label:             str        # "EXCELLENT" | "GOOD" | "FAIR" | "POOR"
    cycle_count:       int
    uptime_sec:        float
    avg_cycle_sec:     float
    memory_mb:         float
    mt5_connected:     bool
    supabase_latency_ms: float
    error_count:       int
    computed_at:       str


class Telemetry:
    """
    Singleton telemetry coordinator.

    Usage:
        tel = Telemetry.get_instance(cfg)
        tel.cycle_start()
        # ... scan ...
        tel.cycle_end(mt5_ok=True, sync_latency_ms=45.0)
        if cycle_num % heartbeat_interval == 0:
            tel.emit_heartbeat(bridge, daily_state)
    """

    _instance: Optional["Telemetry"] = None

    def __init__(self, cfg=None) -> None:
        tel_cfg              = getattr(cfg, "telemetry", None) if cfg else None
        self._hb_interval    = int(getattr(tel_cfg,   "heartbeat_interval_cycles", 1))
        self._mem_warn_mb    = float(getattr(tel_cfg,  "memory_warn_mb",            500))
        self._cycle_warn_sec = float(getattr(tel_cfg,  "cycle_warn_sec",            60))
        self._health_window  = int(getattr(tel_cfg,   "health_score_window",        10))

        self._lock           = threading.Lock()
        self._start_time     = time.monotonic()
        self._cycle_count    = 0
        self._error_count    = 0
        self._cycle_start_ts: Optional[float] = None
        self._cycle_durations: Deque[float]   = deque(maxlen=self._health_window)
        self._api_latencies:   Deque[float]   = deque(maxlen=20)
        self._last_score:      int             = 100
        self._last_snapshot:   Optional[HealthSnapshot] = None

    @classmethod
    def get_instance(cls, cfg=None) -> "Telemetry":
        if cls._instance is None:
            cls._instance = cls(cfg)
        return cls._instance

    # ── Cycle timing ──────────────────────────────────────────────────────────

    def cycle_start(self) -> None:
        """Call at the very beginning of each scan cycle."""
        with self._lock:
            self._cycle_start_ts = time.monotonic()

    def cycle_end(
        self,
        mt5_ok:           bool  = True,
        sync_latency_ms:  float = 0.0,
        had_error:        bool  = False,
    ) -> float:
        """
        Call at the end of each scan cycle.
        Returns the cycle duration in seconds.
        """
        now = time.monotonic()
        with self._lock:
            start = self._cycle_start_ts or now
            duration = now - start
            self._cycle_count += 1
            self._cycle_durations.append(duration)
            if sync_latency_ms > 0:
                self._api_latencies.append(sync_latency_ms)
            if had_error:
                self._error_count += 1

        if duration > self._cycle_warn_sec:
            log.warning(
                "[LATENCY WARNING] [TELEMETRY] Slow scan cycle: %.1fs > threshold %.0fs",
                duration, self._cycle_warn_sec,
            )

        if sync_latency_ms > 3000:
            log.warning(
                "[API STATUS] Supabase sync slow: %.0f ms [LATENCY WARNING]",
                sync_latency_ms,
            )
        elif sync_latency_ms > 0:
            log.debug("[API STATUS] Supabase sync: %.0f ms", sync_latency_ms)

        return duration

    def record_error(self) -> None:
        with self._lock:
            self._error_count += 1

    # ── Heartbeat emission ────────────────────────────────────────────────────

    def emit_heartbeat(
        self,
        bridge,
        daily_state=None,
        cfg=None,
    ) -> HealthSnapshot:
        """
        Emit the [ENTERPRISE HEARTBEAT] log line and return a HealthSnapshot.

        Call once per heartbeat_interval_cycles from the main scan loop.
        """
        snap = self._build_snapshot(bridge)

        uptime_h = snap.uptime_sec / 3600
        mem_str  = f"{snap.memory_mb:.0f}MB" if snap.memory_mb > 0 else "n/a"

        log.warning(
            "[ENTERPRISE HEARTBEAT] [SYSTEM HEALTH] score=%d/100 (%s) | "
            "cycle=%d uptime=%.1fh avg_cycle=%.1fs | "
            "mem=%s mt5=%s api_latency=%.0fms errors=%d | "
            "[VPS STATUS] [API STATUS]",
            snap.score, snap.label,
            snap.cycle_count, uptime_h, snap.avg_cycle_sec,
            mem_str,
            "OK" if snap.mt5_connected else "DEGRADED",
            snap.supabase_latency_ms,
            snap.error_count,
        )

        # Memory pressure alert
        if snap.memory_mb > self._mem_warn_mb:
            log.warning(
                "[VPS STATUS] High memory usage: %.0f MB > threshold %.0f MB",
                snap.memory_mb, self._mem_warn_mb,
            )

        # MT5 connectivity alert
        if not snap.mt5_connected:
            log.warning(
                "[SYSTEM HEALTH] MT5 connection status: DEGRADED — check broker/VPS"
            )

        # Account risk state from daily_state
        if daily_state is not None:
            pnl     = getattr(daily_state, "daily_pnl_pct",   0.0)
            trades  = getattr(daily_state, "trades",           0)
            cons_l  = getattr(daily_state, "consecutive_losses", 0)
            log.info(
                "[RISK HEALTH] daily_pnl=%.2f%% trades=%d consec_losses=%d",
                pnl, trades, cons_l,
            )

        with self._lock:
            self._last_snapshot = snap
            self._last_score    = snap.score

        return snap

    # ── Internal snapshot builder ─────────────────────────────────────────────

    def _build_snapshot(self, bridge) -> HealthSnapshot:
        with self._lock:
            cycles   = self._cycle_count
            errors   = self._error_count
            durations= list(self._cycle_durations)
            latencies= list(self._api_latencies)

        uptime    = time.monotonic() - self._start_time
        avg_cycle = sum(durations) / len(durations) if durations else 0.0
        api_lat   = sum(latencies) / len(latencies) if latencies else 0.0

        # Memory
        mem_mb = 0.0
        if _PSUTIL_OK:
            try:
                import os
                proc   = _psutil.Process(os.getpid())
                mem_mb = proc.memory_info().rss / 1_048_576
            except Exception:
                pass

        # MT5 connectivity
        mt5_ok = True
        try:
            mt5_ok = bool(getattr(bridge, "connected", True))
        except Exception:
            pass

        # ── Score computation ──────────────────────────────────────────────
        # Latency score: 100 when avg_cycle < 15s, 0 when > 90s
        if avg_cycle < 15:
            latency_score = 100
        elif avg_cycle > 90:
            latency_score = 0
        else:
            latency_score = int(100 - (avg_cycle - 15) / 75 * 100)

        # Memory score: 100 when < 200 MB, 0 when > mem_warn_mb
        if mem_mb <= 0:
            mem_score = 100
        elif mem_mb < 200:
            mem_score = 100
        elif mem_mb >= self._mem_warn_mb:
            mem_score = 20
        else:
            mem_score = int(100 - (mem_mb - 200) / (self._mem_warn_mb - 200) * 80)

        # Error rate score: 100 when errors=0, 0 when errors >= 10
        error_score = max(0, 100 - errors * 10)

        # API score: 100 when < 500ms, 0 when > 5000ms
        if api_lat <= 0 or api_lat < 500:
            api_score = 100
        elif api_lat > 5000:
            api_score = 0
        else:
            api_score = int(100 - (api_lat - 500) / 4500 * 100)

        # MT5: binary
        mt5_score = 100 if mt5_ok else 0

        score = int(
            latency_score * 0.30 +
            mem_score     * 0.20 +
            error_score   * 0.20 +
            api_score     * 0.15 +
            mt5_score     * 0.15
        )
        score = max(0, min(100, score))

        label = (
            "EXCELLENT" if score >= 85 else
            "GOOD"      if score >= 70 else
            "FAIR"      if score >= 50 else
            "POOR"
        )

        return HealthSnapshot(
            score               = score,
            label               = label,
            cycle_count         = cycles,
            uptime_sec          = round(uptime, 1),
            avg_cycle_sec       = round(avg_cycle, 2),
            memory_mb           = round(mem_mb, 1),
            mt5_connected       = mt5_ok,
            supabase_latency_ms = round(api_lat, 1),
            error_count         = errors,
            computed_at         = datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def get_last_snapshot(self) -> Optional[HealthSnapshot]:
        with self._lock:
            return self._last_snapshot

    @property
    def cycle_count(self) -> int:
        with self._lock:
            return self._cycle_count

    @property
    def heartbeat_interval(self) -> int:
        return self._hb_interval
