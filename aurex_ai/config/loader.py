"""
Aurex AI Signature Strategy — Configuration Loader

Loads settings.yaml and exposes it via a dot-notation Settings object.
Environment variables override YAML values using the pattern:
    AUREX_<SECTION>_<KEY>=value   e.g.  AUREX_MT5_ACCOUNT=123456

Usage:
    from aurex_ai.config.loader import load_settings
    cfg = load_settings()          # auto-finds settings.yaml next to this file
    print(cfg.risk.risk_pct)       # 1.0
    print(cfg.mt5.account)         # from env or yaml
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

_DEFAULT_PATH = Path(__file__).resolve().parent / "settings.yaml"


# ── Dot-notation settings object ─────────────────────────────────────────────

class Settings:
    """
    Recursive dot-notation wrapper around a YAML dict.

    Examples:
        cfg.risk.risk_pct          -> 1.0
        cfg.scoring.weights.trend  -> 25
        cfg.get("mt5", "account", default=0)
    """

    def __init__(self, data: Dict[str, Any]) -> None:
        for key, val in data.items():
            setattr(
                self,
                key,
                Settings(val) if isinstance(val, dict) else val,
            )

    def get(self, *keys: str, default: Any = None) -> Any:
        obj: Any = self
        for key in keys:
            if hasattr(obj, key):
                obj = getattr(obj, key)
            else:
                return default
        return obj

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, val in self.__dict__.items():
            out[key] = val.to_dict() if isinstance(val, Settings) else val
        return out

    def __repr__(self) -> str:
        return f"Settings({self.to_dict()})"


# ── Env-var override ──────────────────────────────────────────────────────────

def _apply_env_overrides(data: Dict[str, Any], prefix: str = "AUREX") -> None:
    """
    Walk the nested dict and apply matching AUREX_SECTION_KEY env vars.
    Only overrides leaf values (not sub-dicts).
    Type coercion: tries int -> float -> bool -> str in that order.
    """
    for section_key, section_val in data.items():
        if isinstance(section_val, dict):
            for key, val in section_val.items():
                env_name = f"{prefix}_{section_key.upper()}_{key.upper()}"
                raw = os.environ.get(env_name)
                if raw is not None:
                    section_val[key] = _coerce(raw, type(val))
        else:
            env_name = f"{prefix}_{section_key.upper()}"
            raw = os.environ.get(env_name)
            if raw is not None:
                data[section_key] = _coerce(raw, type(section_val))


def _coerce(value: str, target_type: type) -> Any:
    if target_type is bool or isinstance(target_type, bool):
        return value.strip().lower() in ("true", "1", "yes", "on")
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


# ── Public loader ─────────────────────────────────────────────────────────────

_cache: Optional[Settings] = None


def load_settings(path: Optional[str] = None, reload: bool = False) -> Settings:
    """
    Load and cache the settings.yaml.

    Args:
        path:   Override path to settings.yaml.  Defaults to the file next to
                this module (aurex_ai/config/settings.yaml).
        reload: Force re-read from disk (clears cache).
    """
    global _cache
    if _cache is not None and not reload:
        return _cache

    yaml_path = Path(path) if path else _DEFAULT_PATH
    if not yaml_path.exists():
        raise FileNotFoundError(
            f"settings.yaml not found at {yaml_path}. "
            "Copy config/settings.yaml.example -> config/settings.yaml."
        )

    with yaml_path.open("r", encoding="utf-8") as fh:
        raw: Dict[str, Any] = yaml.safe_load(fh) or {}

    _apply_env_overrides(raw)
    _cache = Settings(raw)
    return _cache
