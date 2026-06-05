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
FIXED_ORDER_QUANTITY=20
ENTRY_ORDER_TYPE=MKT
BRACKET_STOP_LOSS_PCT=2.0
BRACKET_TAKE_PROFIT_PCT=4.0
BUY_REENTRY_DIP_PCT=10.0
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
IBKR_CLIENT_ID=10
IBKR_SYNC_CLIENT_ID=11
IBKR_CONTEXT_CLIENT_ID=12
IBKR_NEWS_CLIENT_ID=13
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
SYNC_BROKER_AFTER_ORDER=true
SYNC_BROKER_AFTER_ORDER_DELAY_SECONDS=2
OLLAMA_URL=http://localhost:11434/api/chat
OLLAMA_MODEL=qwen3.5:latest
PERFORMANCE_GOVERNOR_ENABLED=true
PERFORMANCE_MODE=auto
PERFORMANCE_GAME_PROCESS_NAMES=cs2.exe,valorant.exe,fortniteclient-win64-shipping.exe,cod.exe,cod22-cod.exe,r5apex.exe,overwatch.exe,destiny2.exe,helldivers2.exe,thefinals.exe,gta5.exe,eldenring.exe,cyberpunk2077.exe,starfield.exe,minecraft.exe,robloxplayerbeta.exe
PERFORMANCE_BUSY_CPU_PCT=78
PERFORMANCE_LOW_MEMORY_GB=10
PERFORMANCE_PROFILE_TTL_SECONDS=30
PERFORMANCE_GAMING_KEEP_ALIVE=30s
PERFORMANCE_GAMING_TIMEOUT_SECONDS=30
PERFORMANCE_GAMING_MAX_NUM_PREDICT=64
PERFORMANCE_GAMING_MAX_BATCH_SIZE=1
PERFORMANCE_GAMING_MAX_LLM_ITEMS=2
PERFORMANCE_GAMING_MAX_WORKERS=1
PERFORMANCE_GAMING_MAX_LLM_TICKERS=5
PERFORMANCE_GAMING_INTERVAL_MULTIPLIER=6
PERFORMANCE_GAMING_CPU_BUDGET_PCT=10
PERFORMANCE_GAMING_THROTTLE_ENABLED=true
PERFORMANCE_GAMING_THROTTLE_MIN_SLEEP_SECONDS=2
PERFORMANCE_GAMING_THROTTLE_MAX_SLEEP_SECONDS=30
PERFORMANCE_GAMING_PROCESS_PRIORITY=BelowNormal
PERFORMANCE_GAMING_EXTERNAL_PRIORITY=BelowNormal
PERFORMANCE_GAMING_EXTERNAL_PROCESSES=ollama,llama-server
PERFORMANCE_RESTORE_PRIORITY_ON_NORMAL=true
PERFORMANCE_DEFER_TASKS=scanner:llm,operation:ticker-intel,operation:briefing,operation:post-trade-review,operation:category-universe,operation:energy-universe
LLM_METRICS_ENABLED=true
LLM_METRICS_PATH=data/llm_metrics.jsonl
LLM_SCANNER_TIMEOUT_SECONDS=90
LLM_SCANNER_RETRY_ATTEMPTS=2
LLM_SCANNER_RETRY_BACKOFF_SECONDS=2
LLM_SCANNER_NUM_PREDICT=180
LLM_SCANNER_KEEP_ALIVE=30m
LLM_SCANNER_RULES_MAX_CHARS=1800
LLM_SCANNER_MAX_TICKERS=0
LLM_SCANNER_MAX_CONSECUTIVE_FAILURES=8
SIGNAL_QUALITY_FILTER_ENABLED=true
SIGNAL_QUALITY_MIN_SCORE=65
SIGNAL_QUALITY_TIMEOUT_SECONDS=90
SIGNAL_QUALITY_FAIL_OPEN=true
SIGNAL_QUALITY_NUM_PREDICT=96
SIGNAL_QUALITY_RETRY_ATTEMPTS=2
SIGNAL_QUALITY_RETRY_BACKOFF_SECONDS=2
SIGNAL_QUALITY_KEEP_ALIVE=30m
EXECUTION_GATE_ENABLED=true
EXECUTION_GATE_MIN_SCORE=45
EXECUTION_GATE_FAIL_OPEN=false
EXECUTION_GATE_TIMEOUT_SECONDS=90
EXECUTION_GATE_NUM_PREDICT=140
EXECUTION_GATE_RETRY_ATTEMPTS=2
EXECUTION_GATE_RETRY_BACKOFF_SECONDS=2
EXECUTION_GATE_KEEP_ALIVE=30m
SCANNER_INTERVAL_SECONDS=600
SIGNAL_COOLDOWN_MINUTES=240
OPEN_SCANNER_ENABLED=true
OPEN_SCANNER_START=09:30
OPEN_SCANNER_END=10:15
OPEN_SCANNER_MINUTES_AFTER_OPEN=5
OPEN_SCANNER_MAX_TICKERS=135
OPEN_SCANNER_MIN_PRICE=5.0
OPEN_SCANNER_MIN_RVOL=1.2
OPEN_SCANNER_MIN_MOVE_PCT=1.0
OPEN_SCANNER_MIN_RECOVERY_PCT=0.45
OPEN_SCANNER_MIN_CONFIDENCE=62
OPEN_SCANNER_COOLDOWN_MINUTES=90
SIGNAL_JOURNAL_PATH=data/signal_journal.csv
SIGNAL_CONTEXT_LIMIT=50
CATEGORY_UNIVERSE_LIMIT=7000
CATEGORY_MIN_SCORE=65
CATEGORY_TARGET_LIMIT=135
CATEGORY_TARGETS_PER_CATEGORY=15
TECH_NEWS_TICKERS=AAPL,MSFT,NVDA,AMD,AVGO,AMZN,META,GOOGL,TSLA,ORCL,CRM,ADBE,NFLX,INTC,QCOM,MU,IBM,TSM,ASML,ARM,PLTR,SMCI,NOW,SNOW,PANW,CRWD,DDOG,NET
TECH_NEWS_INTERVAL_SECONDS=180
TECH_NEWS_MAX_WORKERS=8
TECH_NEWS_LIMIT_PER_TICKER=5
TECH_NEWS_OUTPUT_PATH=data/tech_news_feed.jsonl
TECH_NEWS_RELEVANCE_FILTER_ENABLED=true
TECH_NEWS_RELEVANCE_MIN_SCORE=2
TECH_NEWS_LLM_ENABLED=true
TECH_NEWS_LLM_MAX_ITEMS=20
TECH_NEWS_LLM_TIMEOUT_SECONDS=120
TECH_NEWS_LLM_BATCH_SIZE=3
TECH_NEWS_LLM_NUM_PREDICT=140
TECH_NEWS_LLM_NUM_PREDICT_PER_ITEM=120
TECH_NEWS_LLM_SINGLE_TIMEOUT_SECONDS=45
TECH_NEWS_LLM_SINGLE_NUM_PREDICT=96
TECH_NEWS_LLM_DEFAULT_RETRY_LIMIT=20
TECH_NEWS_LLM_RETRY_ATTEMPTS=2
TECH_NEWS_LLM_RETRY_BACKOFF_SECONDS=2
TECH_NEWS_LLM_KEEP_ALIVE=30m
TECH_NEWS_LLM_WARMUP=true
TICKER_INTEL_OUTPUT_PATH=data/ticker_intelligence.jsonl
TICKER_INTEL_NEWS_LIMIT=8
TICKER_INTEL_CHATTER_LIMIT=10
TICKER_INTEL_SIGNAL_LOOKBACK_DAYS=14
TICKER_INTEL_WATCHLIST_LIMIT=100
TICKER_INTEL_PUSH_SUPABASE=true
IBKR_NEWS_ENABLED=true
IBKR_NEWS_PROVIDERS=
IBKR_NEWS_LOOKBACK_MINUTES=30
IBKR_NEWS_INTERVAL_SECONDS=60
IBKR_NEWS_MAX_TICKERS=135
IBKR_NEWS_RESULTS_PER_TICKER=5
IBKR_NEWS_MIN_CONFIDENCE=62
IBKR_NEWS_MIN_DIRECTIONAL_SCORE=1
IBKR_NEWS_COOLDOWN_MINUTES=90
IBKR_NEWS_OUTPUT_PATH=data/ibkr_news_feed.jsonl
IBKR_NEWS_SEEN_PATH=data/ibkr_news_seen.json
IBKR_NEWS_PUSH_SIGNALS=true
MARKET_BRIEFING_LOOKBACK_HOURS=12
MARKET_BRIEFING_NEWS_LIMIT=30
MARKET_BRIEFING_SIGNAL_LIMIT=30
MARKET_BRIEFING_POSITION_LIMIT=20
MARKET_BRIEFING_CATEGORY_LIMIT=10
MARKET_BRIEFING_TIMEOUT_SECONDS=120
MARKET_BRIEFING_OUTPUT_PATH=data/daily_market_briefings.jsonl
MARKET_BRIEFING_PUSH_SUPABASE=true
POST_TRADE_REVIEW_LOOKBACK_DAYS=14
POST_TRADE_REVIEW_LIMIT=25
POST_TRADE_REVIEW_HORIZON=1d
POST_TRADE_REVIEW_TIMEOUT_SECONDS=120
POST_TRADE_REVIEW_OUTPUT_PATH=data/post_trade_reviews.jsonl
POST_TRADE_REVIEW_PUSH_SUPABASE=true
STRATEGY_OPTIMIZER_LOOKBACK_DAYS=14
STRATEGY_OPTIMIZER_HORIZON=1h
STRATEGY_OPTIMIZER_MIN_EVALUATED_SIGNALS=12
STRATEGY_OPTIMIZER_MIN_CHANNEL_SIGNALS=5
STRATEGY_OPTIMIZER_MIN_CONFIDENCE_FLOOR=0.55
STRATEGY_OPTIMIZER_MIN_CONFIDENCE_CEILING=0.95
STRATEGY_OPTIMIZER_MAX_STEP=0.03
STRATEGY_OPTIMIZER_OUTPUT_PATH=data/strategy_optimizer_runs.jsonl
STRATEGY_OPTIMIZER_PUSH_SUPABASE=true
STRATEGY_OPTIMIZER_AUTO_APPLY=false
MASSIVE_API_KEY=your_massive_api_key
```

If you upgrade Ollama models, update `OLLAMA_MODEL` in `.env` to the installed tag. The recommended day-to-day model is `qwen3.5:latest`; use `qwen3.6:latest` for intentional deep research runs when you are not gaming.

`LLM_METRICS_ENABLED=true` writes local Qwen/Ollama timing records to `data/llm_metrics.jsonl`. Use `python llm_metrics_report.py` after scanner or briefing runs to see average latency, p50/p95 latency, success rate, and retry pressure by script.

`PERFORMANCE_GOVERNOR_ENABLED=true` lets the bot downshift when Windows looks busy or a configured game process is running. In gaming/quiet mode, the bot behaves like it has a small CPU budget: `PERFORMANCE_GAMING_CPU_BUDGET_PCT=10` adds cooperative sleep between non-critical work, Qwen keep-alive drops to `30s`, tech-news LLM work shrinks to tiny single-item batches, the LLM scanner caps its ticker pass, and the master scheduler defers deep non-critical jobs such as ticker intelligence, briefings, post-trade reviews, and broad universe rebuilding. The governor also lowers this Python process and Ollama/llama-server to `BelowNormal` priority so Windows favors the game.

This is a best-effort 10% profile, not a hard OS resource jail. Python can throttle itself precisely, but Ollama and GPU scheduling are controlled by Windows and the AMD driver. For a true hard cap, use Windows power/process tools or a utility such as Process Lasso; the built-in governor is designed to slow the bot safely without breaking routing.

Check the current mode:

```powershell
python performance_status.py
```

If your game is not auto-detected, add its `.exe` name to `PERFORMANCE_GAME_PROCESS_NAMES`, or temporarily force quiet mode with `PERFORMANCE_MODE=gaming`. Set `PERFORMANCE_MODE=normal` to force full-speed bot behavior.

`SCANNER_INTERVAL_SECONDS` controls the market-hours recommendation loop. The default is 600 seconds, or 10 minutes. `SIGNAL_COOLDOWN_MINUTES` defaults to 240 minutes, or 4 hours, so repeated ticker/action/channel ideas do not keep refilling the iPad queue.

Extended-hours scanning is enabled by default with `SCAN_EXTENDED_HOURS=true`, covering `PREMARKET_OPEN` through `AFTER_HOURS_CLOSE` on weekdays. Global overnight scanning is also enabled by default with `SCAN_GLOBAL_OVERNIGHT=true`, covering `GLOBAL_OVERNIGHT_OPEN` through `GLOBAL_OVERNIGHT_CLOSE` Sunday night through Friday morning. This is intended for futures/FX/Asia-sensitive market movement and signal tracking.

Live extended-hours order routing remains disabled unless both `DRY_RUN=false` and `ALLOW_EXTENDED_HOURS_TRADING=true` are set. Live global overnight routing is even more conservative and also requires `ALLOW_GLOBAL_OVERNIGHT_TRADING=true`. `FIXED_ORDER_QUANTITY=20` keeps every routed order at 20 shares during training; set it to `0` later to return to dynamic sizing. `ENTRY_ORDER_TYPE=MKT` sends the parent bracket entry as a market order for immediate execution, while the child exits remain a take-profit limit and stop-loss stop. `BRACKET_STOP_LOSS_PCT=2.0` and `BRACKET_TAKE_PROFIT_PCT=4.0` control the bracket risk profile. `BUY_REENTRY_DIP_PCT=10.0` blocks repeat BUY routing in the same trading session unless price is at least 10% below the previous session BUY fill. `STOP_OUTSIDE_RTH=false` keeps stop orders regular-hours only by default; be careful changing this because stop behavior can differ by order type and venue outside regular hours.

## Main Scripts

- `master_scanner.py`: single scanner gateway and market-hours orchestrator.
- `macro_scanner.py`: writes daily sector context into `strategy_vault`.
- `fundamental_scanner.py`: creates the daily target list and updates Supabase watchlist.
- `category_universe_builder.py`: scans the broad Yahoo ticker universe and classifies tickers into dynamic themes.
- `category_target_scanner.py`: picks the strongest category targets and updates the active watchlist.
- `tech_news_monitor.py`: standalone tech-stock headline monitor for running beside the scanner.
- `ibkr_news_scanner.py`: watches IBKR/TWS API news providers for catalyst headlines and creates guarded `IBKR_NEWS` signals.
- `energy_universe_builder.py`: builds a broad NYSE/Nasdaq energy stock universe for traditional, clean, transition, and futuristic energy themes.
- `opening_momentum_scanner.py`: scans the daily target list shortly after 9:30 Eastern for opening dip reversals, strength continuation, and downside breakdowns.
- `radar.py`: updates the volatility watchlist from Yahoo trending symbols.
- `earnings_radar.py`: updates the earnings watchlist from Yahoo's calendar.
- `tech_scanner.py`: creates RSI-based pending signals.
- `sentiment_scanner.py`: creates simple lexicon-based pending signals.
- `llm_scanner.py`: creates LLM/RAG-informed signals from headlines and strategy rules.
- `signal_quality_filter.py`: asks local Qwen to approve or block news/LLM signals before they enter Supabase.
- `daily_market_briefing.py`: generates morning/evening Qwen briefings from category/news/signal/position/macro data.
- `post_trade_review.py`: asks Qwen to review closed paper trades and extract training lessons.
- `strategy_optimizer.py`: turns journal/trade feedback into cautious setting and channel recommendations.
- `context_enrichment.py`: enriches pending/approved signals with IBKR position, quote, SEC filing, and macro context for the iPad Execution Terminal.
- `ticker_intelligence.py`: builds a ticker-search intelligence snapshot (news, public chatter proxy, short interest, options tilt, SEC context, and local signal bias) for the iPad research view.
- `routing_status.py`: read-only routing report showing approved/pending/blocked signals, recent order events, and why nothing is routing.
- `signal_journal.py`: tracks accepted scanner signals and scores forward returns for training.
- `main.py`: IBKR/Supabase execution bridge.
- `broker_sync.py`: reads live IBKR paper positions and syncs them to Supabase for the iPad Ledger.

## Dynamic Category Universe

The default master flow is now category-first rather than fixed-stock-first. The broad category universe scans the Yahoo ticker list, classifies NYSE/Nasdaq equities into themes, and stores matches in `public.category_universe`.

Initial categories include:

- Nuclear Energy
- Data Center Power & Grid Infrastructure
- AI Chips
- Cybersecurity & AI Security
- Aerospace Defense & Security
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

By default this selects up to 15 tickers from each category, for a 135-ticker daily target list across the nine categories.

Or use the master:

```powershell
python master_scanner.py category-universe
python master_scanner.py premarket
```

`daily-cycle` runs this category universe flow automatically before scanning. Fixed test stocks are not required for normal scanner operation.

## Tech News Monitor

`tech_news_monitor.py` is a separate news-only process. It does not place orders or create trade signals. It watches a configurable tech-stock ticker list, prints fresh Yahoo Finance headlines, tags likely impact areas, deduplicates across scans and across duplicate cross-ticker story links, and appends new items to `data/tech_news_feed.jsonl`.

When `TECH_NEWS_LLM_ENABLED=true`, fresh headline batches are also sent to the local Ollama model configured by `OLLAMA_URL` and `OLLAMA_MODEL`. The saved JSONL records include Qwen's conservative sentiment, impact score, urgency score, short summary, why-it-matters note, and suggested action (`watch`, `investigate`, or `alert`).

A ticker relevance gate runs before Qwen scoring (default `TECH_NEWS_RELEVANCE_FILTER_ENABLED=true`) so weak ticker/headline matches are dropped before they consume LLM time.

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

Run with smaller Qwen batches (useful if your local model times out on large headline sets):

```powershell
python tech_news_monitor.py --once --llm-batch-size 3
```

Run with tighter generation limits plus single-headline retries (helpful on slower local CPUs):

```powershell
python tech_news_monitor.py --once --llm-batch-size 3 --llm-num-predict 140 --llm-single-timeout 45
```

Analyze more than the default 20 headlines in one run:

```powershell
python tech_news_monitor.py --once --llm-max-items 60 --llm-batch-size 1
```

Retry up to N default/empty LLM outputs (useful when local model occasionally returns blank JSON fields):

```powershell
python tech_news_monitor.py --once --llm-max-items 60 --llm-batch-size 1 --llm-default-retry-limit 20
```

Tune Ollama keep-alive and retry behavior from CLI when needed:

```powershell
python tech_news_monitor.py --once --llm-keep-alive 45m --llm-retry-attempts 3 --llm-retry-backoff 3
```

Tighten the relevance gate to filter more weak matches:

```powershell
python tech_news_monitor.py --once --relevance-min-score 4
```

Disable relevance filtering for debugging:

```powershell
python tech_news_monitor.py --once --no-relevance-filter
```

Summarize recent local Qwen performance:

```powershell
python llm_metrics_report.py --last 200
```

## IBKR News Scanner

`ibkr_news_scanner.py` connects to TWS/Gateway through the IBKR API and reads the news providers exposed to the API account. It saves fresh headlines to `data/ibkr_news_feed.jsonl` and can create guarded `IBKR_NEWS` signals from directional catalysts such as upgrades, downgrades, target changes, guidance changes, investigations, or major contract headlines.

IBKR API news availability is not identical to the TWS desktop news panel. IBKR requires API-specific news entitlements; by default many accounts expose `BRFG`, `BRFUPDN`, and `DJNL`, while other providers may require separate Account Management subscriptions.

Safe one-pass test:

```powershell
python ibkr_news_scanner.py --once --dry-run --lookback-minutes 240 --max-tickers 25
```

One live polling pass that can create `IBKR_NEWS` signals:

```powershell
python ibkr_news_scanner.py --once
```

Run continuously beside `main.py` and `master_scanner.py`:

```powershell
python ibkr_news_scanner.py
```

Run through the master gateway:

```powershell
python master_scanner.py ibkr-news
```

This scanner does not send orders directly. BUY signals enter the same iPad/main.py autonomous flow, SELL signals remain manual-review first, and all routed orders still face the Qwen execution gate.

## Daily Market Briefing

`daily_market_briefing.py` creates a structured morning/evening briefing with local Qwen. It summarizes:

- Category universe shifts (entrants/exits from the top category shortlist)
- Recent tech news highlights from `data/tech_news_feed.jsonl` and IBKR catalyst headlines from `data/ibkr_news_feed.jsonl`
- Open signal queue (`pending` and `approved`)
- Current broker positions from `broker_positions`
- Macro context from `strategy_vault/00_daily_macro_state.txt`

If Ollama is unavailable, the script falls back to a deterministic summary so the briefing pipeline still runs.

Run this SQL once in Supabase for iPad sync:

```sql
-- supabase/daily_market_briefings.sql
```

Generate morning briefing and push to Supabase:

```powershell
python daily_market_briefing.py --session morning
```

Generate evening briefing:

```powershell
python daily_market_briefing.py --session evening
```

Auto session by local market time:

```powershell
python daily_market_briefing.py --session auto
```

Preview without Supabase write:

```powershell
python daily_market_briefing.py --session morning --dry-run
```

You can also run through master:

```powershell
python master_scanner.py briefing
python master_scanner.py briefing --dry-run
```

## Post-Trade Journal Analysis

`post_trade_review.py` reviews completed paper trades with local Qwen. For each closed/closeable executed signal with journal outcomes, it analyzes:

- Signal reason
- Entry and exit proxy prices
- Directional PnL %
- What worked
- What failed
- Concrete training adjustments

Run this SQL once in Supabase:

```sql
-- supabase/post_trade_reviews.sql
```

Run local review pass:

```powershell
python post_trade_review.py
```

Preview without Supabase writes:

```powershell
python post_trade_review.py --dry-run
```

Master operation:

```powershell
python master_scanner.py post-trade-review
```

Support workflow:

```powershell
python master_scanner.py review-cycle
```

Reviews are appended locally to `data/post_trade_reviews.jsonl` and can sync to Supabase `post_trade_reviews` for iPad display and training dashboards.

## Strategy Optimizer

`strategy_optimizer.py` closes the feedback loop. It reads recent `signal_journal`, `trade_events`, `post_trade_reviews`, `broker_positions`, and category-universe context, then produces cautious recommendations for scanner/channel behavior and bot settings.

Run this SQL once if you want optimizer history in Supabase:

```sql
-- supabase/strategy_optimizer_runs.sql
```

Safe report only:

```powershell
python strategy_optimizer.py --dry-run
```

Write local history and Supabase optimizer history, but do not change settings:

```powershell
python strategy_optimizer.py
```

Apply only safe bot setting changes:

```powershell
python strategy_optimizer.py --apply
```

The first automatic setting is `bot_settings.min_confidence`. The optimizer respects `STRATEGY_OPTIMIZER_MIN_CONFIDENCE_FLOOR`, `STRATEGY_OPTIMIZER_MIN_CONFIDENCE_CEILING`, and `STRATEGY_OPTIMIZER_MAX_STEP`, so it changes slowly rather than overfitting to a small batch of paper trades. Channel/category recommendations are advisory until we add explicit channel-weight settings.

Master operation:

```powershell
python master_scanner.py strategy-optimizer --dry-run
python master_scanner.py review-cycle
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

When the continuous engine is running, `opening_momentum_scanner.py` is checked once per regular-session day inside the configured `OPEN_SCANNER_START`/`OPEN_SCANNER_END` window. It uses its own `OPEN_SCANNER_COOLDOWN_MINUTES` duplicate window because the opening auction is short-lived and should not be treated exactly like the slower all-day scanners.

Before `sentiment_scanner.py` or `llm_scanner.py` inserts a news-driven signal, `signal_quality_filter.py` asks local Qwen whether the catalyst is material, whether it may already be priced in, whether it is company-specific or broader, and whether the scanner confidence is justified. Signals below `SIGNAL_QUALITY_MIN_SCORE` are blocked before they reach the iPad queue. `SIGNAL_QUALITY_FAIL_OPEN=true` keeps scanners running if Ollama is temporarily unavailable; set it to `false` for stricter behavior.

Before `main.py` routes an approved signal to TWS, `execution_quality_gate.py` asks Qwen for a final execution decision using the signal memo, confidence, current price, price drift from signal, session type, bracket risk matrix, quantity, position state, and repeat-buy guard. `EXECUTION_GATE_MIN_SCORE=45` is the default paper-training minimum, another roughly 20% looser step from the previous threshold. `EXECUTION_GATE_FAIL_OPEN=false` is intentionally stricter: if Qwen is unavailable, the execution bridge blocks instead of sending an order.

Summarize Qwen performance after scanner or execution runs:

```powershell
python llm_metrics_report.py --last 200
```

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

## Ticker Intelligence Search Feed

The iPad app can use a single ticker-search table in Supabase (`public.ticker_intelligence`) to render a rich research card for any symbol:

- Market quote snapshot + key fundamentals
- Short interest metrics (shares short, % float, days-to-cover)
- Fresh Yahoo market headlines with quick sentiment scoring
- Public chatter proxy from finance-focused Reddit RSS
- Cached tech-news LLM context from `data/tech_news_feed.jsonl` (when present)
- Recent local scanner/signal bias and latest SEC filing risk snapshot

Run this SQL once in Supabase:

```sql
-- supabase/ticker_intelligence.sql
```

Build one ticker on demand:

```powershell
python ticker_intelligence.py --ticker NVDA
```

Build multiple tickers:

```powershell
python ticker_intelligence.py --tickers NVDA,AMD,AAPL,TSLA
```

Build from dynamic watchlists:

```powershell
python ticker_intelligence.py
```

Run without writing to Supabase:

```powershell
python ticker_intelligence.py --ticker NVDA --dry-run --no-push-supabase
```

`master_scanner.py ticker-intel` runs the same job, and `daily-cycle` now includes this step so the iPad search table stays fresh each day.

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

`main.py` also performs a best-effort broker snapshot after every live order is sent, so the iPad Ledger can manually refresh against fresh Supabase position data. This is enabled by default with `SYNC_BROKER_AFTER_ORDER=true`; `SYNC_BROKER_AFTER_ORDER_DELAY_SECONDS=2` gives IBKR a short moment to update positions before the snapshot runs.

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

Run the scanner engine:

```powershell
python master_scanner.py
```

On startup, the engine runs `daily-cycle` once per market date, records that marker in `data/master_startup_state.json`, and then enters the normal market-session loop controlled by `SCANNER_INTERVAL_SECONDS`. That startup cycle covers:

```text
health preflight
category universe scan
premarket scanners
intraday scanners
ticker intelligence sync
context enrichment
broker position sync
signal journal update/sync
```

Force the daily startup cycle again:

```powershell
python master_scanner.py --force-daily-startup
```

Run pre-market target generation only:

```powershell
python master_scanner.py premarket
```

Run scanner passes:

```powershell
python master_scanner.py opening-bell
python master_scanner.py intraday
```

`opening-bell` runs the dedicated 9:30 Eastern opening scanner. By default it waits five minutes after the regular open, scans the daily category targets until 10:15, and looks for opening dip reversals, opening strength continuation, and downside breakdowns. BUY signals enter the normal iPad/bridge flow and still face the Qwen execution gate before IBKR routing; SELL signals remain manual-review first.

Run it directly without writing signals, useful for testing outside the open:

```powershell
python opening_momentum_scanner.py --force --dry-run --max-tickers 25
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

This runs once and exits. Use plain `python master_scanner.py` for the continuously looping engine.

Support jobs can also be run through the master:

```powershell
python master_scanner.py preflight
python master_scanner.py context
python master_scanner.py broker-sync
python master_scanner.py journal
python master_scanner.py routing-status
python master_scanner.py category-universe --dry-run
python master_scanner.py energy-universe --dry-run
```

Run the routing report directly with a custom lookback:

```powershell
python routing_status.py --hours 6
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
