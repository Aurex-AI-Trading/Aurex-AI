"""
Aurex AI Signature Strategy — Structured Logger

Outputs:
  - Colourised human-readable lines  -> stdout
  - JSON structured lines            -> logs/aurex_ai.log  (rotating)
  - JSON structured lines            -> logs/latest.log    (current run, truncated on start)

Usage:
    from aurex_ai.core.logger import configure_logging, get_logger
    configure_logging(level="INFO", log_dir="./logs")
    log = get_logger("strategy.trend")
    log.info("trend detected: %s", direction)
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


# ── Formatters ────────────────────────────────────────────────────────────────

class _JsonFormatter(logging.Formatter):
    """Emit each record as a single JSON line — analytics-ready."""

    # Standard LogRecord instance attributes that should NOT be forwarded as extras
    _STANDARD = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:
        data: Dict[str, Any] = {
            "ts":      datetime.now(timezone.utc).isoformat(),
            "level":   record.levelname,
            "logger":  record.name,
            "message": record.getMessage(),
        }
        # Merge any extra= kwargs supplied by the caller (not standard fields)
        for key, val in record.__dict__.items():
            if key not in self._STANDARD and not key.startswith("_"):
                data[key] = val
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


class _ConsoleFormatter(logging.Formatter):
    _COLOURS = {
        "DEBUG":    "\033[36m",   # cyan
        "INFO":     "\033[32m",   # green
        "WARNING":  "\033[33m",   # yellow
        "ERROR":    "\033[31m",   # red
        "CRITICAL": "\033[35m",   # magenta
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        colour = self._COLOURS.get(record.levelname, "")
        ts     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return (
            f"{colour}[{ts}] [{record.levelname:<8}] "
            f"[{record.name}] {record.getMessage()}{self._RESET}"
        )


# ── Public API ────────────────────────────────────────────────────────────────

_configured = False


def configure_logging(
    level:        str  = "INFO",
    log_dir:      str  = "./logs",
    max_bytes:    int  = 10_485_760,
    backup_count: int  = 10,
    debug:        bool = False,
) -> None:
    """
    Configure the root logger once at startup.
    Safe to call multiple times — subsequent calls are no-ops.

    Args:
        level:        Logging level string (INFO, DEBUG, WARNING, etc.).
        log_dir:      Directory for log files (created if missing).
        max_bytes:    Max size per rotating log file (default 10 MB).
        backup_count: Number of rotating backup files to keep.
        debug:        If True, override level to DEBUG regardless of `level`.
    """
    global _configured
    if _configured:
        return
    _configured = True

    effective_level = "DEBUG" if debug else level

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, effective_level.upper(), logging.INFO))

    # Colourised console
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_ConsoleFormatter())
    root.addHandler(console)

    # Rotating JSON file — persists across runs
    rotating_h = logging.handlers.RotatingFileHandler(
        log_path / "aurex_ai.log",
        maxBytes    = max_bytes,
        backupCount = backup_count,
        encoding    = "utf-8",
    )
    rotating_h.setFormatter(_JsonFormatter())
    root.addHandler(rotating_h)

    # Latest-run JSON file — truncated on each startup for easy tail/grep
    latest_h = logging.FileHandler(
        log_path / "latest.log",
        mode     = "w",
        encoding = "utf-8",
    )
    latest_h.setFormatter(_JsonFormatter())
    root.addHandler(latest_h)


def get_logger(name: str) -> logging.Logger:
    """Return a named child of the 'aurex' root logger."""
    return logging.getLogger(f"aurex.{name}")
