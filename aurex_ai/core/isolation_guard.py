"""
Aurex AI Signature Strategy — Namespace Isolation Guard

Detects if any legacy HFM_AUTO_TRADER module has been imported into the
same Python process as aurex_ai.  If contamination is found, raises an
ImportError immediately so the problem is caught at startup, not at runtime.

This guard is called once from aurex_ai/__init__.py.

Forbidden namespaces (any module whose name contains these strings):
  - "mt5_bridge"           legacy package name
  - "hfm_auto_trader"      alternative legacy name
  - "hfm_mt5"              any legacy alias

Why this matters:
  Both projects use a module called "mt5_bridge" internally.  Without this
  guard, a sys.path misconfiguration could silently import the wrong bridge,
  leading to mismatched config objects, wrong magic numbers, and live orders
  on the wrong account.
"""
from __future__ import annotations

import sys
from typing import List

_FORBIDDEN: tuple[str, ...] = (
    "hfm_auto_trader",
    "hfm_mt5",
)

_LEGACY_MT5_BRIDGE_INDICATORS: tuple[str, ...] = (
    # If 'mt5_bridge' is loaded but NOT as 'aurex_ai.core.mt5_bridge',
    # it is the legacy package — flag it.
    "mt5_bridge.config",
    "mt5_bridge.executor",
    "mt5_bridge.signal_listener",
    "mt5_bridge.auto_trader",
    "mt5_bridge.connection",
    "mt5_bridge.risk_guard",
    "mt5_bridge.routers",
)


def assert_clean_namespace() -> None:
    """
    Scan sys.modules for any legacy HFM_AUTO_TRADER namespaces.

    Raises:
        ImportError: If a forbidden legacy module is already loaded.
    """
    contaminated: List[str] = []

    for module_name in list(sys.modules.keys()):
        lower = module_name.lower()

        # Catch explicit legacy package names
        if any(forbidden in lower for forbidden in _FORBIDDEN):
            contaminated.append(module_name)
            continue

        # Catch legacy mt5_bridge package (distinct from aurex_ai.core.mt5_bridge)
        if any(module_name.startswith(indicator) for indicator in _LEGACY_MT5_BRIDGE_INDICATORS):
            contaminated.append(module_name)

    if contaminated:
        raise ImportError(
            "\n"
            "╔══════════════════════════════════════════════════════════════╗\n"
            "║         AUREX AI — ISOLATION BREACH DETECTED                ║\n"
            "╠══════════════════════════════════════════════════════════════╣\n"
            "║  Legacy HFM_AUTO_TRADER modules are loaded in this process. ║\n"
            "║  aurex_ai MUST run in a completely separate Python process. ║\n"
            "║                                                              ║\n"
            "║  Contaminated modules:                                       ║\n"
            f"║    {', '.join(contaminated)!s:<58}║\n"
            "║                                                              ║\n"
            "║  Fix:                                                        ║\n"
            "║    1. Deactivate the HFM_AUTO_TRADER venv                   ║\n"
            "║    2. Activate the AUREX_AI venv                            ║\n"
            "║    3. Verify sys.path contains no HFM_AUTO_TRADER paths     ║\n"
            "╚══════════════════════════════════════════════════════════════╝"
        )
