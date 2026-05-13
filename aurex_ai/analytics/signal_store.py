"""
Aurex AI — Signal Intelligence Store  (Phase 12)

Tracks ALL strategy signals — executed and rejected.
Resolves hypothetical outcomes for rejected signals using live price.

Enables:
  1. Filter effectiveness analysis (false-negatives / false-positives)
  2. Confidence-band analytics (near-threshold signals)
  3. Rejection-reason breakdown (which gates fire most often)
  4. Missed-opportunity tracking (good signals blocked by non-quality gates)

DB: analytics/data/signals.db (separate from trades.db for isolation)

Logs emitted
------------
  [SIGNAL TRACKED]      — every signal logged
  [SIGNAL RESOLVED]     — hypothetical outcome determined
  [FILTER EFFECTIVENESS]— periodic analytics summary
  [CONFIDENCE BAND]     — near-threshold win-rate analysis
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from aurex_ai.core.logger import get_logger

log = get_logger("analytics.signal_store")

_DB_PATH = Path(__file__).parent / "data" / "signals.db"

# Confidence bands relative to execution threshold
BAND_BELOW = "BELOW"   # score < threshold - _NEAR_MARGIN (clearly too weak)
BAND_NEAR  = "NEAR"    # threshold - _NEAR_MARGIN <= score < threshold (borderline)
BAND_ABOVE = "ABOVE"   # score >= threshold (executed or blocked by non-score gate)
_NEAR_MARGIN = 10      # pts below threshold that qualify as "near"

# Hypothetical outcome states
HYP_WIN     = "WIN"
HYP_LOSS    = "LOSS"
HYP_OPEN    = "OPEN"
HYP_EXPIRED = "EXPIRED"

# Signal resolution window — rejected signals older than this are expired
_RESOLUTION_HOURS = 4

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS signals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol            TEXT NOT NULL,
    direction         TEXT NOT NULL,
    detected_at       TEXT NOT NULL,
    utc_hour          INTEGER DEFAULT 0,
    session           TEXT DEFAULT '',
    setup_type        TEXT DEFAULT '',
    market_state      TEXT DEFAULT '',
    signal_score      REAL DEFAULT 0,
    confidence        REAL DEFAULT 0,
    core_score        REAL DEFAULT 0,
    quality_grade     TEXT DEFAULT '',
    atr_pips          REAL DEFAULT 0,
    spread_pips       REAL DEFAULT 0,
    trend_strength    REAL DEFAULT 0,
    trend_score       REAL DEFAULT 0,
    ob_score          REAL DEFAULT 0,
    fvg_score         REAL DEFAULT 0,
    liquidity_score   REAL DEFAULT 0,
    executed          INTEGER DEFAULT 0,
    trade_ticket      INTEGER,
    rejection_reason  TEXT DEFAULT '',
    pipeline_stage    TEXT DEFAULT '',
    confidence_band   TEXT DEFAULT 'BELOW',
    band_distance_pts REAL DEFAULT 0,
    strategy_version  TEXT DEFAULT 'AI_FORWARD_V2',
    hyp_outcome       TEXT DEFAULT 'OPEN',
    hyp_rr            REAL DEFAULT 0,
    hyp_mae           REAL DEFAULT 0,
    hyp_mfe           REAL DEFAULT 0,
    hyp_entry_price   REAL DEFAULT 0,
    hyp_sl_price      REAL DEFAULT 0,
    hyp_tp_price      REAL DEFAULT 0,
    hyp_resolved_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sig_symbol   ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_sig_detected ON signals(detected_at);
CREATE INDEX IF NOT EXISTS idx_sig_executed ON signals(executed);
CREATE INDEX IF NOT EXISTS idx_sig_outcome  ON signals(hyp_outcome);
CREATE INDEX IF NOT EXISTS idx_sig_band     ON signals(confidence_band);
CREATE INDEX IF NOT EXISTS idx_sig_quality  ON signals(quality_grade);
"""

# Columns added after initial schema — applied via ALTER TABLE to existing DBs
_SCHEMA_MIGRATIONS = [
    "ALTER TABLE signals ADD COLUMN core_score      REAL DEFAULT 0",
    "ALTER TABLE signals ADD COLUMN quality_grade   TEXT DEFAULT ''",
    "ALTER TABLE signals ADD COLUMN trend_score     REAL DEFAULT 0",
    "ALTER TABLE signals ADD COLUMN ob_score        REAL DEFAULT 0",
    "ALTER TABLE signals ADD COLUMN fvg_score       REAL DEFAULT 0",
    "ALTER TABLE signals ADD COLUMN liquidity_score REAL DEFAULT 0",
    "ALTER TABLE signals ADD COLUMN pipeline_stage  TEXT DEFAULT ''",
    "ALTER TABLE signals ADD COLUMN spread_pips     REAL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_sig_quality ON signals(quality_grade)",
]


@dataclass
class SignalRecord:
    symbol:           str
    direction:        str
    signal_score:     float
    confidence:       float
    utc_hour:         int
    session:          str   = ""
    setup_type:       str   = ""
    market_state:     str   = ""
    atr_pips:         float = 0.0
    spread_pips:      float = 0.0
    trend_strength:   float = 0.0
    core_score:       float = 0.0
    quality_grade:    str   = ""
    trend_score:      float = 0.0
    ob_score:         float = 0.0
    fvg_score:        float = 0.0
    liquidity_score:  float = 0.0
    executed:         bool  = False
    trade_ticket:     Optional[int] = None
    rejection_reason: str   = ""
    pipeline_stage:   str   = ""
    confidence_band:  str   = BAND_BELOW
    band_distance_pts: float = 0.0
    strategy_version: str   = "AI_FORWARD_V2"
    hyp_entry_price:  float = 0.0
    hyp_sl_price:     float = 0.0
    hyp_tp_price:     float = 0.0


def compute_band(
    score: float,
    threshold: float,
    margin: int = _NEAR_MARGIN,
) -> Tuple[str, float]:
    """Return (band, distance_pts) for a signal score vs its threshold."""
    if score >= threshold:
        return BAND_ABOVE, round(score - threshold, 1)
    if score >= threshold - margin:
        return BAND_NEAR, round(threshold - score, 1)
    return BAND_BELOW, round(threshold - score, 1)


class SignalStore:
    """
    Singleton SQLite store for all strategy signals.

    Usage:
        store = SignalStore.get_instance()
        sid   = store.log_signal(record)
        store.mark_rejected(sid, "LOW_CONFIDENCE")
        store.mark_executed(sid, ticket=12345)
        store.resolve_hypotheticals(get_price_fn)
    """
    _instance: Optional["SignalStore"] = None

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self._lock    = threading.Lock()
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @classmethod
    def get_instance(cls) -> "SignalStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            for stmt in _CREATE_SQL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    conn.execute(stmt)
            # Apply schema migrations for existing DBs (ignore "duplicate column" errors)
            for migration in _SCHEMA_MIGRATIONS:
                try:
                    conn.execute(migration)
                except Exception:
                    pass

    # ── Write operations ──────────────────────────────────────────────────────

    def log_signal(self, record: SignalRecord) -> int:
        """
        Persist a new signal. Returns the inserted row ID.
        Initially hyp_outcome='OPEN'; resolved on subsequent scans.
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        initial_outcome = HYP_WIN if record.executed else HYP_OPEN
        with self._lock, self._connect() as conn:
            cur = conn.execute("""
                INSERT INTO signals (
                    symbol, direction, detected_at, utc_hour, session,
                    setup_type, market_state, signal_score, confidence,
                    core_score, quality_grade,
                    atr_pips, spread_pips, trend_strength,
                    trend_score, ob_score, fvg_score, liquidity_score,
                    executed, trade_ticket, rejection_reason, pipeline_stage,
                    confidence_band, band_distance_pts, strategy_version,
                    hyp_outcome, hyp_entry_price, hyp_sl_price, hyp_tp_price
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                record.symbol, record.direction, now, record.utc_hour,
                record.session, record.setup_type, record.market_state,
                record.signal_score, record.confidence,
                record.core_score, record.quality_grade,
                record.atr_pips, record.spread_pips, record.trend_strength,
                record.trend_score, record.ob_score, record.fvg_score, record.liquidity_score,
                1 if record.executed else 0,
                record.trade_ticket, record.rejection_reason, record.pipeline_stage,
                record.confidence_band, record.band_distance_pts,
                record.strategy_version,
                initial_outcome,
                record.hyp_entry_price, record.hyp_sl_price, record.hyp_tp_price,
            ))
            sid = int(cur.lastrowid)

        log.info(
            "[SIGNAL TRACKED] id=%d %s %s score=%.1f conf=%.0f band=%s executed=%s %s",
            sid, record.symbol, record.direction, record.signal_score,
            record.confidence, record.confidence_band,
            "YES" if record.executed else "NO",
            f"reason={record.rejection_reason}" if record.rejection_reason else "",
        )
        return sid

    def mark_executed(self, signal_id: int, ticket: int) -> None:
        """Update signal as executed; outcome resolved via actual trade close."""
        if not signal_id:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE signals SET executed=1, trade_ticket=? WHERE id=?",
                (ticket, signal_id),
            )

    def mark_rejected(self, signal_id: int, reason: str) -> None:
        """Record rejection reason on an already-logged signal."""
        if not signal_id:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE signals SET rejection_reason=? WHERE id=?",
                (reason, signal_id),
            )

    def update_executed_outcome(self, ticket: int, won: bool, rr: float) -> None:
        """Called when a real trade closes — resolve the linked signal outcome."""
        outcome = HYP_WIN if won else HYP_LOSS
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE signals
                   SET hyp_outcome=?, hyp_rr=?, hyp_resolved_at=?
                   WHERE trade_ticket=? AND executed=1""",
                (outcome, round(rr, 3), now, ticket),
            )

    # ── Hypothetical outcome resolution ───────────────────────────────────────

    def resolve_hypotheticals(self, get_price_fn) -> int:
        """
        Check all OPEN rejected signals against current price.

        `get_price_fn(symbol) -> (bid, ask)` — pass bridge.get_bid_ask or similar.
        Signals older than _RESOLUTION_HOURS are marked EXPIRED.

        Returns count of signals resolved this call.
        """
        now     = datetime.now(timezone.utc)
        cutoff  = (now - timedelta(hours=_RESOLUTION_HOURS)).isoformat(timespec="seconds")
        resolved = 0

        with self._lock:
            with self._connect() as conn:
                pending = conn.execute("""
                    SELECT id, symbol, direction,
                           hyp_entry_price, hyp_sl_price, hyp_tp_price, detected_at
                    FROM   signals
                    WHERE  executed = 0
                      AND  hyp_outcome = 'OPEN'
                      AND  hyp_entry_price > 0
                      AND  hyp_sl_price    > 0
                      AND  hyp_tp_price    > 0
                """).fetchall()

                for row in pending:
                    sid       = row["id"]
                    symbol    = row["symbol"]
                    direction = row["direction"]
                    entry     = row["hyp_entry_price"]
                    sl        = row["hyp_sl_price"]
                    tp        = row["hyp_tp_price"]
                    detected  = row["detected_at"]

                    if detected < cutoff:
                        conn.execute(
                            "UPDATE signals SET hyp_outcome=? WHERE id=?",
                            (HYP_EXPIRED, sid),
                        )
                        resolved += 1
                        continue

                    try:
                        bid, ask = get_price_fn(symbol)
                        current  = (bid + ask) / 2.0
                    except Exception:
                        continue

                    outcome = None
                    rr      = 0.0

                    if direction == "BUY":
                        mfe = max(0.0, current - entry)
                        mae = max(0.0, entry   - current)
                        if current >= tp:
                            outcome = HYP_WIN
                            rr      = abs(tp - entry) / max(abs(entry - sl), 1e-8)
                        elif current <= sl:
                            outcome = HYP_LOSS
                            rr      = -1.0
                        else:
                            conn.execute(
                                "UPDATE signals SET hyp_mfe=max(hyp_mfe,?), hyp_mae=max(hyp_mae,?) WHERE id=?",
                                (round(mfe, 5), round(mae, 5), sid),
                            )
                    else:  # SELL
                        mfe = max(0.0, entry   - current)
                        mae = max(0.0, current - entry)
                        if current <= tp:
                            outcome = HYP_WIN
                            rr      = abs(entry - tp) / max(abs(sl - entry), 1e-8)
                        elif current >= sl:
                            outcome = HYP_LOSS
                            rr      = -1.0
                        else:
                            conn.execute(
                                "UPDATE signals SET hyp_mfe=max(hyp_mfe,?), hyp_mae=max(hyp_mae,?) WHERE id=?",
                                (round(mfe, 5), round(mae, 5), sid),
                            )

                    if outcome is not None:
                        now_str = now.isoformat(timespec="seconds")
                        conn.execute(
                            """UPDATE signals
                               SET hyp_outcome=?, hyp_rr=?, hyp_resolved_at=?
                               WHERE id=?""",
                            (outcome, round(rr, 3), now_str, sid),
                        )
                        log.info(
                            "[SIGNAL RESOLVED] id=%d %s %s → %s rr=%.2f",
                            sid, symbol, direction, outcome, rr,
                        )
                        resolved += 1

        return resolved

    # ── Analytics queries ─────────────────────────────────────────────────────

    def get_filter_effectiveness(self, window: int = 200) -> Dict:
        """
        Compute filter effectiveness over the last `window` resolved signals.

        Returns:
            exec_win_rate      — real executed win rate
            false_negative_rate — fraction of rejected signals that would have WON
            false_positive_rate — fraction of executed signals that LOST
            missed_opportunities — count of rejected-would-win signals
            successful_rejections — count of rejected-would-lose signals
        """
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT executed, rejection_reason, hyp_outcome, confidence_band
                FROM   signals
                WHERE  hyp_outcome NOT IN ('OPEN')
                ORDER  BY detected_at DESC
                LIMIT  ?
            """, (window,)).fetchall()

        if not rows:
            return {}

        total      = len(rows)
        executed   = [r for r in rows if r["executed"]]
        rejected   = [r for r in rows if not r["executed"]]
        exec_wins  = [r for r in executed if r["hyp_outcome"] == HYP_WIN]
        exec_losses= [r for r in executed if r["hyp_outcome"] == HYP_LOSS]
        rej_win    = [r for r in rejected if r["hyp_outcome"] == HYP_WIN]
        rej_lose   = [r for r in rejected if r["hyp_outcome"] == HYP_LOSS]

        near_band  = [r for r in rejected if r["confidence_band"] == BAND_NEAR]
        near_wins  = [r for r in near_band if r["hyp_outcome"] == HYP_WIN]

        reason_counts: Dict[str, int] = {}
        for r in rejected:
            key = r["rejection_reason"] or "UNKNOWN"
            reason_counts[key] = reason_counts.get(key, 0) + 1

        return {
            "total_signals":         total,
            "executed_count":        len(executed),
            "rejected_count":        len(rejected),
            "exec_win_rate":         len(exec_wins)  / max(len(executed), 1),
            "false_negative_rate":   len(rej_win)    / max(len(rejected), 1),
            "false_positive_rate":   len(exec_losses)/ max(len(executed), 1),
            "missed_opportunities":  len(rej_win),
            "successful_rejections": len(rej_lose),
            "near_band_count":       len(near_band),
            "near_band_win_rate":    len(near_wins)  / max(len(near_band), 1),
            "rejection_reasons":     reason_counts,
        }

    def get_confidence_band_stats(self) -> List[Dict]:
        """Per-band win rate for threshold calibration intelligence."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT confidence_band,
                       COUNT(*) AS n,
                       SUM(CASE WHEN hyp_outcome='WIN' THEN 1 ELSE 0 END) AS wins,
                       AVG(hyp_rr) AS avg_rr
                FROM   signals
                WHERE  hyp_outcome IN ('WIN','LOSS')
                GROUP  BY confidence_band
            """).fetchall()
        return [
            {
                "band":     r["confidence_band"],
                "n":        r["n"],
                "win_rate": r["wins"] / r["n"] if r["n"] else 0.0,
                "avg_rr":   round(r["avg_rr"] or 0.0, 3),
            }
            for r in rows
        ]

    def get_rejection_breakdown(self) -> Dict[str, Dict]:
        """False-negative rate per rejection reason."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT rejection_reason,
                       COUNT(*) AS n,
                       SUM(CASE WHEN hyp_outcome='WIN' THEN 1 ELSE 0 END) AS wins
                FROM   signals
                WHERE  executed = 0
                  AND  hyp_outcome IN ('WIN','LOSS')
                GROUP  BY rejection_reason
                ORDER  BY n DESC
            """).fetchall()
        return {
            r["rejection_reason"]: {
                "n":             r["n"],
                "would_have_won": r["wins"],
                "fn_rate":        r["wins"] / r["n"] if r["n"] else 0.0,
            }
            for r in rows
        }

    def get_setup_expectancy(self) -> List[Dict]:
        """Win rate and avg RR per setup type — executed trades only."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT setup_type,
                       COUNT(*) AS n,
                       SUM(CASE WHEN hyp_outcome='WIN' THEN 1 ELSE 0 END) AS wins,
                       AVG(hyp_rr) AS avg_rr
                FROM   signals
                WHERE  executed = 1
                  AND  hyp_outcome IN ('WIN','LOSS')
                GROUP  BY setup_type
                ORDER  BY n DESC
            """).fetchall()
        return [
            {
                "setup_type": r["setup_type"],
                "n":          r["n"],
                "win_rate":   r["wins"] / r["n"] if r["n"] else 0.0,
                "avg_rr":     round(r["avg_rr"] or 0.0, 3),
            }
            for r in rows
        ]

    def total_signals(self, executed_only: bool = False) -> int:
        where = "WHERE executed=1" if executed_only else ""
        with self._connect() as conn:
            return conn.execute(
                f"SELECT COUNT(*) FROM signals {where}"
            ).fetchone()[0]
