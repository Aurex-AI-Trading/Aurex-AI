@echo off
REM ============================================================
REM  AUREX_AI — Virtual Environment Setup
REM  Run this once before first use.
REM  DO NOT activate HFM_AUTO_TRADER venv in the same shell.
REM ============================================================

echo [AUREX] Checking for HFM_AUTO_TRADER contamination...
python -c "import sys; bad=[p for p in sys.path if 'HFM_AUTO_TRADER' in p]; sys.exit(1) if bad else None" 2>nul
if errorlevel 1 (
    echo.
    echo ERROR: HFM_AUTO_TRADER detected in sys.path.
    echo        Deactivate that venv first, then re-run this script.
    echo.
    pause
    exit /b 1
)

echo [AUREX] Creating virtual environment...
python -m venv venv

echo [AUREX] Activating...
call venv\Scripts\activate.bat

echo [AUREX] Installing dependencies...
pip install --upgrade pip
pip install -r requirements.txt

echo.
echo [AUREX] Optional: install MetaTrader5 for live trading (Windows only):
echo         pip install "MetaTrader5>=5.0.45"
echo.
echo [AUREX] Setup complete.
echo [AUREX] To activate:  call venv\Scripts\activate.bat
echo [AUREX] Dry-run:      python run.py --dry-run
echo [AUREX] Backtest:     python run.py --backtest --symbols EURUSD
echo [AUREX] Live:         python run.py --live
echo.
pause
