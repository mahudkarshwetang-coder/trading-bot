# Trading Bot Training Setup

This project is a Python signal pipeline for scanning equities, writing trade ideas to Supabase, and optionally routing approved signals to Interactive Brokers.

## Safety Defaults

- `DRY_RUN` defaults to `true` in `config.py`.
- In dry-run mode, `main.py` calculates bracket orders but does not call `ib.placeOrder`.
- Test scripts are read-only by default. Use `--write` only when you intentionally want them to update Supabase rows.
- Supabase credentials should live only in `.env`.

## Required Services

- Supabase project with `bot_settings` and `market_signals` tables.
- IBKR TWS or Gateway for the execution bridge.
- Ollama running locally for `llm_scanner.py`.
- Internet access for Yahoo Finance, yfinance, and S&P 500 data.

## Environment Variables

Required:

```env
SUPABASE_URL=your_supabase_url
SUPABASE_KEY=your_supabase_key
```

Optional:

```env
DRY_RUN=true
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
IBKR_CLIENT_ID=10
IBKR_SYNC_CLIENT_ID=11
MAX_DRAWDOWN_PCT=2.0
BROKER_SYNC_INTERVAL_SECONDS=30
BROKER_SYNC_MARK_MISSING_SIGNALS=false
OLLAMA_URL=http://localhost:11434/api/generate
OLLAMA_MODEL=qwen2.5-coder:7b
SIGNAL_COOLDOWN_MINUTES=30
MASSIVE_API_KEY=your_massive_api_key
```

## Main Scripts

- `master_scanner.py`: single scanner gateway and market-hours orchestrator.
- `macro_scanner.py`: writes daily sector context into `strategy_vault`.
- `fundamental_scanner.py`: creates the daily target list and updates Supabase watchlist.
- `radar.py`: updates the volatility watchlist from Yahoo trending symbols.
- `earnings_radar.py`: updates the earnings watchlist from Yahoo's calendar.
- `tech_scanner.py`: creates RSI-based pending signals.
- `sentiment_scanner.py`: creates simple lexicon-based pending signals.
- `llm_scanner.py`: creates LLM/RAG-informed signals from headlines and strategy rules.
- `main.py`: IBKR/Supabase execution bridge.
- `broker_sync.py`: reads live IBKR paper positions and syncs them to Supabase for the iPad Ledger.

## Broker Position Sync

The iPad Ledger should use IBKR as the source of truth for active positions. Run this SQL once in Supabase before enabling the sync:

```sql
-- supabase/broker_positions.sql
```

Then run a read-only local test on the Windows PC while TWS or IB Gateway is open:

```powershell
python broker_sync.py --dry-run
```

To write the current IBKR paper positions to Supabase once:

```powershell
python broker_sync.py
```

To keep the broker snapshot fresh for the iPad app:

```powershell
python broker_sync.py --loop
```

Or double-click:

```powershell
broker_sync_run.bat
```

By default, `broker_sync.py` updates only the `broker_positions` table. To also mark stale `market_signals.status = executed` rows as `closed_external` when their tickers no longer exist in IBKR, either set `BROKER_SYNC_MARK_MISSING_SIGNALS=true` in `.env` or run:

```powershell
python broker_sync.py --mark-missing-signals
```

## Typical Training Flow

Install dependencies:

```powershell
pip install -r requirements.txt
```

Run a read-only preflight check:

```powershell
python health_check.py
```

Run pre-market context and target generation:

```powershell
python master_scanner.py premarket
```

Run scanner passes:

```powershell
python master_scanner.py intraday
```

Run the execution bridge in training mode:

```powershell
python main.py
```

Run a single scanner through the master:

```powershell
python master_scanner.py radar
python master_scanner.py llm
```

To allow live order placement later, set `DRY_RUN=false` in `.env` and restart `main.py`.

## Test Scripts

Read-only database check:

```powershell
python db_test.py
```

Write-mode database test:

```powershell
python db_test.py --write
```

Read-only IBKR bridge dry run:

```powershell
python ibkr_test.py
```

Write-mode IBKR bridge test:

```powershell
python ibkr_test.py --write
```

## Security Note

If this folder was shared or uploaded while Supabase keys were hardcoded in source files, rotate the Supabase key in the Supabase dashboard.
