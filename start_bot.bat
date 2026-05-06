@echo off

cd /d C:\Projects\Aurex-AI

REM Wait for MT5 to load
timeout /t 30

REM Activate environment
call C:\Projects\Aurex-AI\venv\Scripts\activate.bat

REM Start MT5 manually
start "" "C:\Program Files\MetaTrader 5\terminal64.exe"

REM Wait again to ensure MT5 connects
timeout /t 20

:loop
python -m aurex_ai.main
timeout /t 10
goto loop