"""
Aurex AI Signature Strategy — Top-Level Entry Point

Usage:
    python run.py                           # live / dry-run (from settings.yaml)
    python run.py --dry-run                 # force dry-run
    python run.py --live                    # force live orders
    python run.py --backtest                # walk-forward backtest
    python run.py --symbols EURUSD USDJPY GBPUSD  # restrict symbol list
    python run.py --config /path/to/settings.yaml

This file is a thin wrapper.  All logic lives in aurex_ai/main.py.
"""
from aurex_ai.main import main

if __name__ == "__main__":
    main()
