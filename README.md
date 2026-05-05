# Aurex AI Signature Strategy

Institutional-grade autonomous trading engine for MetaTrader 5.

Multi-confluence signal generation: **EMA Trend + Liquidity Sweep + Fair Value Gap +
Fibonacci Retracement** — scored 0–100, decision-gated at 80/65, dynamically risk-managed.

> **This is an independent project.** It shares zero code with HFM_AUTO_TRADER.

---

## Architecture

```
AUREX_AI/
├── aurex_ai/                       # Core package
│   ├── config/
│   │   ├── settings.yaml           # Master config (all parameters)
│   │   └── loader.py               # Dot-notation Settings with env overrides
│   ├── core/
│   │   ├── logger.py               # JSON + colorised console logging
│   │   ├── data_feed.py            # Candle abstraction (live + backtest CSV)
│   │   ├── mt5_bridge.py           # MT5 connection, orders, retry logic
│   │   └── isolation_guard.py      # Startup check: zero legacy contamination
│   ├── strategy/
│   │   ├── trend.py                # H1/H4 EMA alignment → direction + strength
│   │   ├── liquidity.py            # Equal-highs/lows sweep detection
│   │   ├── fvg.py                  # Fair Value Gap detection + scoring
│   │   ├── fibonacci.py            # Retracement impulse + golden zone scoring
│   │   └── confluence.py           # Vote-based direction + 0-100 score
│   ├── execution/
│   │   ├── scoring_model.py        # EMA proximity + candle confirmation scores
│   │   ├── decision_engine.py      # EXECUTE / CONDITIONAL / SKIP decision
│   │   ├── risk_manager.py         # SL/TP placement + lot sizing
│   │   ├── cooldown_manager.py     # Adaptive post-trade cooldowns
│   │   └── trade_executor.py       # Async MT5 order submission
│   └── main.py                     # scan_symbol(), run_live(), Backtester
├── run.py                          # Top-level entry point
├── requirements.txt
├── .env.example
├── .gitignore
├── setup_venv.bat
├── logs/                           # Runtime logs (git-ignored)
└── data/
    └── backtest/                   # CSV historical data for backtesting
```

---

## Scoring Model

| Signal | Max Points | Source |
|---|---|---|
| EMA Trend (H1+H4) | 25 | `strategy/trend.py` |
| Liquidity Sweep | 20 | `strategy/liquidity.py` |
| Fair Value Gap | 20 | `strategy/fvg.py` |
| Fibonacci Retracement | 15 | `strategy/fibonacci.py` |
| EMA Entry Proximity | 10 | `execution/scoring_model.py` |
| Candle Confirmation | 10 | `execution/scoring_model.py` |
| **Total** | **100** | `strategy/confluence.py` |

**Decision thresholds** (configurable in `settings.yaml`):

| Score | Action | Lot size |
|---|---|---|
| ≥ 80 | `EXECUTE` | 100% |
| 65–79 | `CONDITIONAL` | 50% |
| < 65 | `SKIP` | — |

---

## Quick Start

### 1. Prerequisites
- Python 3.11+
- Windows (for live trading with MT5 terminal)
- MetaTrader 5 terminal installed (for live mode only)

### 2. Virtual environment

```bat
cd c:\Projects\AUREX_AI
setup_venv.bat
```

Or manually:
```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
# Windows live trading only:
pip install MetaTrader5>=5.0.45
```

### 3. Configure

Edit `aurex_ai/config/settings.yaml` — all parameters documented inline.

For MT5 credentials, use environment variables (never put credentials in yaml):

```bat
copy .env.example .env
```

Then edit `.env`:
```
AUREX_MT5_ACCOUNT=12345678
AUREX_MT5_PASSWORD=YourPassword
AUREX_MT5_SERVER=BrokerServer-Demo
AUREX_TRADING_DRY_RUN=true
```

### 4. Run

#### Dry-run (safe — no real orders)
```bat
venv\Scripts\activate
python run.py --dry-run
```

#### Specific symbols
```bat
python run.py --dry-run --symbols EURUSD XAUUSD
```

#### Walk-forward backtest

Place CSV files in `data/backtest/`:
- `EURUSD_M15.csv`, `EURUSD_H1.csv`, `EURUSD_H4.csv`
- Columns: `datetime, open, high, low, close, volume`

```bat
python run.py --backtest --symbols EURUSD GBPUSD
```

#### Live trading (real orders)
```bat
python run.py --live
```

---

## Configuration Reference

All settings live in `aurex_ai/config/settings.yaml`. Key sections:

```yaml
risk:
  risk_pct:          1.0    # % of balance per trade
  min_rr:            2.0    # minimum R:R to take a trade
  max_open_trades:   3      # simultaneous position cap
  max_daily_trades:  6      # daily trade cap
  max_daily_loss_pct: 3.0   # halt trading if exceeded

scoring:
  thresholds:
    execute:     80          # full-size execution
    conditional: 65          # half-size execution

trading:
  dry_run:       true        # always start with true
  scan_interval: 15          # seconds between symbol scans
```

Override any value via environment variable:
```
AUREX_RISK_RISK_PCT=1.5
AUREX_TRADING_DRY_RUN=false
```

---

## Running Tests

```bat
venv\Scripts\activate
pytest -v
```

---

## Isolation Guarantee

On every startup, `aurex_ai/__init__.py` calls `isolation_guard.assert_clean_namespace()`.

If any legacy `mt5_bridge` (HFM_AUTO_TRADER) module is detected in `sys.modules`,
the process raises `ImportError` immediately with a diagnostic message.

This prevents silent cross-contamination between projects if both are on the same machine.

---

## Key Differences vs HFM_AUTO_TRADER

| | HFM_AUTO_TRADER | AUREX_AI |
|---|---|---|
| Architecture | HTTP signal receiver | Autonomous scanner |
| Strategy | EMA + basic sweep scoring | 5-factor confluence (EMA+Sweep+FVG+Fib+Confirmation) |
| Config | `.env` + pydantic-settings | `settings.yaml` + dot-notation loader |
| Entry point | `uvicorn main:app --port 8001` | `python run.py` |
| HTTP server | Yes (FastAPI, port 8001) | No |
| Backtest mode | No | Yes (walk-forward CSV replay) |
| Isolation guard | No | Yes (startup namespace check) |
| Status | Stable / legacy | Active development |
