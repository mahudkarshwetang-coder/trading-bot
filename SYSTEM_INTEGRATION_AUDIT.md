# Alpha Engine Integration Audit

Last reviewed: 2026-06-05

## System Shape

The project has four main layers:

1. `master_scanner.py` produces research, watchlists, signal context, journals, and daily support data.
2. `main.py` listens for approved Supabase signals and routes bracket orders through IBKR/TWS.
3. Supabase is the shared state layer between the Windows bot and SignalCenter.
4. SignalCenter is the iPad control/review surface for execution, ledger, metrics, holdings, research, and command settings.

## Current Integrated Flows

| Flow | Producer | Supabase/File State | iPad Surface | Status |
| --- | --- | --- | --- | --- |
| Execution queue | scanners -> `market_signals` | `market_signals` | Execution tab | Integrated |
| Auto/manual approvals | SignalCenter -> Supabase | `market_signals.status` | Execution tab | Integrated |
| IBKR routing | `main.py` | `market_signals`, `trade_events` | Execution + Metrics | Integrated |
| Broker ledger | `broker_sync.py`, `main.py` | `broker_positions` | Ledger tab | Integrated |
| Daily category universe | `category_universe_builder.py` | `category_universe` | Research + Control | Integrated |
| Daily targets | `category_target_scanner.py` | `bot_settings.watchlist`, `daily_targets.txt` | Research + scanner inputs | Integrated |
| Ticker dossiers | `ticker_intelligence.py` | `ticker_intelligence` | Research + Holdings | Integrated |
| Live Wealthsimple watch | SignalCenter + `ticker_intelligence.py` | `live_holdings`, `ticker_intelligence` | Holdings tab | Integrated |
| Signal context | `context_enrichment.py` | `signal_context` | Execution detail | Integrated |
| Signal journal | `signal_journal.py` | `signal_journal` | Metrics tab | Integrated |
| Trade events | `main.py` | `trade_events` | Metrics tab | Integrated |
| Daily briefing | `daily_market_briefing.py` | `daily_market_briefings` | Control tab | Integrated, manual workflow |
| Post-trade review | `post_trade_review.py` | `post_trade_reviews` | Metrics tab | Integrated, manual workflow |
| Strategy optimizer | `strategy_optimizer.py` | `strategy_optimizer_runs` | Control tab | Integrated, manual workflow |
| Repo sync | `repo_sync.py` | GitHub | Windows/Mac workflow | Integrated |

## Loose Ends

1. **Runtime settings duplication**
   - SignalCenter `BotRuntimeConfig.swift` mirrors important `.env` defaults such as quantity, stop loss, take profit, category limits, and gate thresholds.
   - Better target: publish runtime config to Supabase so the iPad displays actual bot settings, not hardcoded defaults.

2. **Manual workflow scheduling**
   - `review-cycle` exists but is not automatically scheduled by `master_scanner.py`.
   - Better target: run an evening review once after regular close: `journal`, `post-trade-review`, `strategy-optimizer`, and `briefing`.

3. **Daily startup timing**
   - `master_scanner.py` runs `daily-cycle` on market-date rollover and also runs a 05:30 category refresh.
   - Better target: make startup phases time-aware so heavy daily work happens at intentional times rather than immediately after midnight if the engine is left running.

4. **Macro context quality**
   - Fixed: `macro_scanner.py` now refuses to write all-invalid `nan%` macro states.
   - Remaining target: add a health check warning when `strategy_vault/00_daily_macro_state.txt` contains stale or invalid data.

5. **Supabase security posture**
   - Current app-facing tables use broad anon access for iPad convenience.
   - Better target: add app auth and tighten RLS before storing sensitive live portfolio details long term.

6. **Swift compile validation**
   - Windows cannot run `xcodebuild`.
   - Better target: use the Mac as final SignalCenter build verifier after every pushed iPad change.

## Recommended Next Integration Passes

1. Add a Supabase-backed `bot_runtime_config` or `bot_settings` expansion and have SignalCenter read actual risk/runtime settings.
2. Add an evening scheduled workflow to `master_scanner.py` for reviews, optimizer, and briefing.
3. Add a `system_status` table so the bot can publish last run timestamps, failures, and active mode for the iPad.
4. Add Health tab/status cards in SignalCenter for scanner freshness, TWS connection, Ollama model, Supabase tables, and latest errors.
5. Tighten Supabase RLS/auth for `live_holdings` and other writeable tables.
6. Add a Mac-side build check to the repo sync workflow or require one before pushing SignalCenter release changes.
