@echo off
echo ===================================================
echo [ALPHA ENGINE] Starting IBKR Broker Position Sync...
echo ===================================================

cd "C:\Users\Shwetang\Trading Bot"

python broker_sync.py --loop

echo.
echo Broker sync stopped.
pause
