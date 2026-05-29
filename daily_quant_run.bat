@echo off
echo ===================================================
echo [ALPHA ENGINE] Initiating Master Scanner Operations...
echo ===================================================

cd "C:\Users\Shwetang\Trading Bot"

echo [1/2] Running premarket scanner phase through master_scanner.py...
python master_scanner.py premarket

echo.
echo [System] Allowing 5 seconds for ChromaDB RAG sync...
timeout /t 5 /nobreak > NUL

echo [2/2] Running LLM scanner through master_scanner.py...
python master_scanner.py llm

echo.
echo ===================================================
echo [ALPHA ENGINE] Daily Operations Complete.
echo ===================================================
exit
