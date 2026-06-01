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
FIXED_ORDER_QUANTITY=100
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
IBKR_CLIENT_ID=10
IBKR_SYNC_CLIENT_ID=11
IBKR_CONTEXT_CLIENT_ID=12
MAX_DRAWDOWN_PCT=2.0
MARKET_TIMEZONE=US/Eastern
PREMARKET_OPEN=04:00
REGULAR_MARKET_OPEN=09:30
REGULAR_MARKET_CLOSE=16:00
AFTER_HOURS_CLOSE=20:00
SCAN_EXTENDED_HOURS=true
ALLOW_EXTENDED_HOURS_TRADING=false
STOP_OUTSIDE_RTH=false
GLOBAL_OVERNIGHT_OPEN=20:00
GLOBAL_OVERNIGHT_CLOSE=04:00
SCAN_GLOBAL_OVERNIGHT=true
SCAN_SUNDAY_NIGHT=true
ALLOW_GLOBAL_OVERNIGHT_TRADING=false
BROKER_SYNC_INTERVAL_SECONDS=30
BROKER_SYNC_MARK_MISSING_SIGNALS=false
OLLAMA_URL=http://localhost:11434/api/generate
OLLAMA_MODEL=qwen2.5-coder:7b
SIGNAL_QUALITY_FILTER_ENABLED=true
SIGNAL_QUALITY_MIN_SCORE=65
SIGNAL_QUALITY_TIMEOUT_SECONDS=90
SIGNAL_QUALITY_FAIL_OPEN=true
SCANNER_INTERVAL_SECONDS=600
SIGNAL_COOLDOWN_MINUTES=240
SIGNAL_JOURNAL_PATH=data/signal_journal.csv
SIGNAL_CONTEXT_LIMIT=50
CATEGORY_UNIVERSE_LIMIT=7000
CATEGORY_MIN_SCORE=65
CATEGORY_TARGET_LIMIT=60
CATEGORY_TARGETS_PER_CATEGORY=15
TECH_NEWS_TICKERS=AAPL,MSFT,NVDA,AMD,AVGO,AMZN,META,GOOGL,TSLA,ORCL,CRM,ADBE,NFLX,INTC,QCOM,MU,IBM,TSM,ASML,ARM,PLTR,SMCI,NOW,SNOW,PANW,CRWD,DDOG,NET
TECH_NEWS_INTERVAL_SECONDS=180
TECH_NEWS_MAX_WORKERS=8
TECH_NEWS_LIMIT_PER_TICKER=5
TECH_NEWS_OUTPUT_PATH=data/tech_news_feed.jsonl
TECH_NEWS_LLM_ENABLED=true
TECH_NEWS_LLM_MAX_ITEMS=20
TECH_NEWS_LLM_TIMEOUT_SECONDS=90
MASSIVE_API_KEY=your_massive_api_key
```

`SCANNER_INTERVAL_SECONDS` controls the market-hours recommendation loop. The default is 600 seconds, or 10 minutes. `SIGNAL_COOLDOWN_MINUTES` defaults to 240 minutes, or 4 hours, so repeated ticker/action/channel ideas do not keep refilling the iPad queue.

Extended-hours scanning is enabled by default with `SCAN_EXTENDED_HOURS=true`, covering `PREMARKET_OPEN` through `AFTER_HOURS_CLOSE` on weekdays. Global overnight scanning is also enabled by default with `SCAN_GLOBAL_OVERNIGHT=true`, covering `GLOBAL_OVERNIGHT_OPEN` through `GLOBAL_OVERNIGHT_CLOSE` Sunday night through Friday morning. This is intended for futures/FX/Asia-sensitive market movement and signal tracking.

Live extended-hours order routing remains disabled unless both `DRY_RUN=false` and `ALLOW_EXTENDED_HOURS_TRADING=true` are set. Live global overnight routing is even more conservative and also requires `ALLOW_GLOBAL_OVERNIGHT_TRADING=true`. `FIXED_ORDER_QUANTITY=100` keeps every routed order at 100 shares during training; set it to `0` later to return to dynamic sizing. `STOP_OUTSIDE_RTH=false` keeps stop orders regular-hours only by default; be careful changing this because stop behavior can differ by order type and venue outside regular hours.

## Main Scripts

- `master_scanner.py`: single scanner gateway and market-hours orchestrator.
- `macro_scanner.py`: writes daily sector context into `strategy_vault`.
- `fundamental_scanner.py`: creates the daily target list and updates Supabase watchlist.
- `category_universe_builder.py`: scans the broad Yahoo ticker universe and classifies tickers into dynamic themes.
- `category_target_scanner.py`: picks the strongest category targets and updates the active watchlist.
- `tech_news_monitor.py`: standalone tech-stock headline monitor for running beside the scanner.
- `energy_universe_builder.py`: builds a broad NYSE/Nasdaq energy stock universe for traditional, clean, transition, and futuristic energy themes.
- `radar.py`: updates the volatility watchlist from Yahoo trending symbols.
- `earnings_radar.py`: updates the earnings watchlist from Yahoo's calendar.
- `tech_scanner.py`: creates RSI-based pending signals.
- `sentiment_scanner.py`: creates simple lexicon-based pending signals.
- `llm_scanner.py`: creates LLM/RAG-informed signals from headlines and strategy rules.
- `signal_quality_filter.py`: asks local Qwen to approve or block news/LLM signals before they enter Supabase.
- `context_enrichment.py`: enriches pending/approved signals with IBKR position, quote, SEC filing, and macro context for the iPad Execution Terminal.
- `signal_journal.py`: tracks accepted scanner signals and scores forward returns for training.
- `main.py`: IBKR/Supabase execution bridge.
- `broker_sync.py`: reads live IBKR paper positions and syncs them to Supabase for the iPad Ledger.

## Dynamic Category Universe

The default master flow is now category-first rather than fixed-stock-first. The broad category universe scans the Yahoo ticker list, classifies NYSE/Nasdaq equities into themes, and stores matches in `public.category_universe`.

Initial categories include:

- Energy
- Logistics
- Infrastructure
- Materials

Run this SQL once in Supabase:

```sql
-- supabase/category_universe.sql
```

Run a local dry run:

```powershell
python category_universe_builder.py --dry-run --limit 250
```

Run the broad daily universe scan:

```powershell
python category_universe_builder.py --limit 7000
```

Select dynamic daily targets from the category universe:

```powershell
python category_target_scanner.py
```

Or use the master:

```powershell
python master_scanner.py category-universe
python master_scanner.py premarket
```

`daily-cycle` runs this category universe flow automatically before scanning. Fixed test stocks are not required for normal scanner operation.

## Tech News Monitor

`tech_news_monitor.py` is a separate news-only process. It does not place orders or create trade signals. It watches a configurable tech-stock ticker list, prints fresh Yahoo Finance headlines, tags likely impact areas, deduplicates across scans, and appends new items to `data/tech_news_feed.jsonl`.

When `TECH_NEWS_LLM_ENABLED=true`, fresh headline batches are also sent to the local Ollama model configured by `OLLAMA_URL` and `OLLAMA_MODEL`. The saved JSONL records include Qwen's conservative sentiment, impact score, urgency score, short summary, why-it-matters note, and suggested action (`watch`, `investigate`, or `alert`).

Run one scan:

```powershell
python tech_news_monitor.py --once
```

Run continuously in a separate PowerShell window:

```powershell
python tech_news_monitor.py
```

Customize the list without editing code:

```powershell
python tech_news_monitor.py --tickers NVDA,AMD,MSFT,AAPL,AVGO --interval 120
```

Run without local LLM analysis:

```powershell
python tech_news_monitor.py --no-llm
```

## Energy Universe Tracking

The energy universe layer tracks a broad NYSE/Nasdaq stock set across traditional energy, electric utilities, renewable power, solar, wind, nuclear/uranium, hydrogen/fuel cells, battery storage, EV charging, grid/electrification, carbon capture, and critical minerals.

This is intentionally broader than the official Energy sector because many clean-energy and energy-transition companies are classified as Utilities, Industrials, Technology, or Basic Materials.

Run this SQL once in Supabase:

```sql
-- supabase/energy_universe.sql
```

Then run a local dry run:

```powershell
python energy_universe_builder.py --dry-run --limit 250
```

If the dry run looks reasonable, write matches to Supabase:

```powershell
python energy_universe_builder.py --limit 1000
```

Useful options:

```powershell
python energy_universe_builder.py --limit 2000 --min-score 70
python energy_universe_builder.py --dry-run --limit 500 --min-score 60
```

The builder writes to `public.energy_universe` and does not create market signals or route orders. It is a universe/classification layer only. Scanner integration happens in a later phase.

## Recommendation Noise Control

During regular, configured extended, and configured global overnight market sessions, `master_scanner.py` runs the intraday scanner pulse every `SCANNER_INTERVAL_SECONDS`. Duplicate suppression blocks:

- Any matching ticker/action/channel signal that is still `pending`.
- Any matching ticker/action/channel signal that is already `approved`.
- Any matching ticker/action/channel signal created inside `SIGNAL_COOLDOWN_MINUTES`.
- LLM and sentiment signals whose headline context has not changed.

Before `sentiment_scanner.py` or `llm_scanner.py` inserts a news-driven signal, `signal_quality_filter.py` asks local Qwen whether the catalyst is material, whether it may already be priced in, whether it is company-specific or broader, and whether the scanner confidence is justified. Signals below `SIGNAL_QUALITY_MIN_SCORE` are blocked before they reach the iPad queue. `SIGNAL_QUALITY_FAIL_OPEN=true` keeps scanners running if Ollama is temporarily unavailable; set it to `false` for stricter behavior.

## Signal Journal

Accepted scanner signals are recorded locally in `data/signal_journal.csv` through `signal_utils.py`. This journal is ignored by Git because it is training/runtime data.

Run this SQL once in Supabase to make journal analytics available to the iPad Metrics tab:

```sql
-- supabase/signal_journal.sql
```

The journal tracks:

- Signal metadata: ticker, action, channel, confidence, status, timestamp.
- Context fields when available: signal price, RSI, SMA, RVOL, bid, ask, memo excerpt.
- Forward outcomes: 15 minutes, 1 hour, 1 day, and 5 days.

Update forward returns after signals have aged:

```powershell
python signal_journal.py update
```

Sync the local journal to Supabase without recalculating outcomes:

```powershell
python signal_journal.py sync
```

Summarize performance by scanner channel:

```powershell
python signal_journal.py summary --horizon 1h
python signal_journal.py summary --horizon 1d
```

This measures signal quality only. It does not require iPad auto-execution, does not connect to IBKR, and does not place orders.

## No-Short Safety

This bot is configured as long-only by default. When auto-execute is enabled, LLM `BUY` signals can be routed for execution, but LLM `SELL` signals remain pending for manual review.

The execution bridge also performs a final IBKR position check before routing any approved `SELL`. If IBKR does not report a positive long position for that ticker, the signal is marked `blocked_no_position` and no order is sent.

## Execution Context Enrichment

The iPad Execution Terminal can show a richer decision context beside each pending signal. Run this SQL once in Supabase:

```sql
-- supabase/signal_context.sql
```

Then refresh context from the Windows PC:

```powershell
python context_enrichment.py
```

Or double-click:

```powershell
context_enrichment_run.bat
```

This script is best-effort. It uses IBKR for current held quantity/no-short validation, Massive/Yahoo for quote context, SEC EDGAR for filing risk, and SPY/QQQ/VIX for a lightweight macro regime. If one source is unavailable, the rest of the context still syncs. To reduce free-plan API noise, repeated tickers are cached inside each run and Massive is skipped for the rest of the run after a rate-limit response. Set `MASSIVE_PREV_CLOSE_ENABLED=false` to rely on Yahoo quotes only.

## Analytics Event History

The iPad Metrics tab can read execution outcome events from Supabase. Run this SQL once to enable that table:

```sql
-- supabase/trade_events.sql
```

After the table exists, `main.py` will write best-effort events when approved signals are routed, dry-run, or fail routing. If the table is not installed yet, execution still continues and the Metrics tab falls back to signal and broker-position analytics.

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

Run the broad daily universe scan and pre-market target generation:

```powershell
python master_scanner.py category-universe
python master_scanner.py premarket
```

Or run the all-in daily flow:

```powershell
python master_scanner.py daily-cycle
```

Run pre-market target generation only:

```powershell
python master_scanner.py premarket
```

Run scanner passes:

```powershell
python master_scanner.py intraday
```

Run the full training support cycle:

```powershell
python master_scanner.py training-cycle
```

This runs:

```text
intraday scanners
context enrichment
broker position sync
signal journal update/sync
```

Run a full daily cycle:

```powershell
python master_scanner.py daily-cycle
```

This runs:

```text
health preflight
category universe scan
premarket scanners
intraday scanners
context enrichment
broker position sync
signal journal update/sync
```

Support jobs can also be run through the master:

```powershell
python master_scanner.py preflight
python master_scanner.py context
python master_scanner.py broker-sync
python master_scanner.py journal
python master_scanner.py category-universe --dry-run
python master_scanner.py energy-universe --dry-run
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

To allow regular-hours live order placement later, set `DRY_RUN=false` in `.env` and restart `main.py`. To allow live extended-hours routing too, also set `ALLOW_EXTENDED_HOURS_TRADING=true`. To allow global overnight live routing, also set `ALLOW_GLOBAL_OVERNIGHT_TRADING=true`.

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
