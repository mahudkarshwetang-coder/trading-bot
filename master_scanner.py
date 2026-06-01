import argparse
import time

from config import (
    BROKER_SYNC_MARK_MISSING_SIGNALS,
    CATEGORY_MIN_SCORE,
    ENERGY_MIN_SCORE,
    SCAN_EXTENDED_HOURS,
    SCAN_GLOBAL_OVERNIGHT,
    SCANNER_INTERVAL_SECONDS,
)
from market_session import get_market_session, now_market_time


def run_macro():
    from macro_scanner import run_macro_scan

    run_macro_scan()


def run_fundamental():
    from fundamental_scanner import run_fundamental_scan

    run_fundamental_scan()


def run_energy_targets():
    from energy_target_scanner import run_energy_target_scan

    run_energy_target_scan()


def run_category_targets():
    from category_target_scanner import run_category_target_scan

    return bool(run_category_target_scan())


def run_earnings():
    from earnings_radar import run_earnings_scan

    run_earnings_scan()


def run_radar():
    from radar import run_radar_scan

    run_radar_scan()


def run_sentiment():
    from sentiment_scanner import run_nlp_scan

    run_nlp_scan()


def run_technical():
    from tech_scanner import run_market_scan

    run_market_scan()


def run_llm():
    from llm_scanner import run_llm_scan

    run_llm_scan()


def run_preflight():
    from health_check import main as health_main

    try:
        health_main()
    except SystemExit as exc:
        return int(exc.code or 0) == 0
    return True


def run_context(dry_run=False):
    from context_enrichment import sync_context

    sync_context(dry_run=dry_run)
    return True


def run_broker_snapshot(dry_run=False, mark_missing_signals=BROKER_SYNC_MARK_MISSING_SIGNALS):
    from broker_sync import connect_to_ibkr, sync_once
    from config import get_supabase_client

    supabase = get_supabase_client()
    ib = connect_to_ibkr()
    try:
        sync_once(
            ib,
            supabase,
            mark_signals=mark_missing_signals,
            dry_run=dry_run,
        )
    finally:
        ib.disconnect()
    return True


def run_journal():
    from signal_journal import sync_journal, update_outcomes

    update_outcomes()
    sync_journal()
    return True


def run_post_trade_review(dry_run=False):
    from post_trade_review import run_post_trade_review

    return run_post_trade_review(dry_run=dry_run)


def run_briefing(dry_run=False):
    from daily_market_briefing import run_daily_market_briefing

    return run_daily_market_briefing(session="auto", dry_run=dry_run)


def run_energy_universe(dry_run=False):
    from energy_universe_builder import DEFAULT_LIMIT, build_energy_universe

    build_energy_universe(
        limit=DEFAULT_LIMIT,
        min_score=ENERGY_MIN_SCORE,
        dry_run=dry_run,
    )
    return True


def run_category_universe(dry_run=False):
    from category_universe_builder import CATEGORY_UNIVERSE_LIMIT, build_category_universe

    build_category_universe(
        limit=CATEGORY_UNIVERSE_LIMIT,
        min_score=CATEGORY_MIN_SCORE,
        dry_run=dry_run,
    )
    return True


SCANNERS = {
    "macro": run_macro,
    "fundamental": run_fundamental,
    "categories": run_category_targets,
    "energy": run_energy_targets,
    "earnings": run_earnings,
    "radar": run_radar,
    "sentiment": run_sentiment,
    "technical": run_technical,
    "llm": run_llm,
}

PHASES = {
    "premarket": ["macro", "categories", "earnings"],
    "fundamental_premarket": ["macro", "fundamental", "earnings"],
    "category_premarket": ["macro", "categories", "earnings"],
    "energy_premarket": ["macro", "energy", "earnings"],
    "intraday": ["radar", "sentiment", "technical", "llm"],
    "energy_full": ["macro", "energy", "earnings", "radar", "sentiment", "technical", "llm"],
    "full": ["macro", "fundamental", "earnings", "radar", "sentiment", "technical", "llm"],
}

OPERATIONS = {
    "preflight": run_preflight,
    "context": run_context,
    "broker-sync": run_broker_snapshot,
    "journal": run_journal,
    "post-trade-review": run_post_trade_review,
    "briefing": run_briefing,
    "energy-universe": run_energy_universe,
    "category-universe": run_category_universe,
}

WORKFLOWS = {
    "training-cycle": ["categories", "intraday", "context", "broker-sync", "journal"],
    "daily-cycle": ["preflight", "category-universe", "premarket", "intraday", "context", "broker-sync", "journal"],
    "review-cycle": ["journal", "post-trade-review", "briefing"],
    "energy-cycle": ["preflight", "energy_premarket", "intraday", "context", "broker-sync", "journal"],
    "category-cycle": ["preflight", "category-universe", "category_premarket", "intraday", "context", "broker-sync", "journal"],
}


def is_market_open(now):
    """Backward-compatible regular-session check."""
    return get_market_session(now).is_regular


def run_scanner(name):
    scanner = SCANNERS[name]
    print(f"\n--- Starting scanner: {name} ---")
    try:
        result = scanner()
        if result is False:
            print(f"--- Scanner returned failure: {name} ---")
            return False
        print(f"--- Scanner complete: {name} ---")
        return True
    except Exception as exc:
        print(f"Scanner failed: {name}: {exc}")
        return False


def run_phase(phase):
    print(f"\n[ALPHA ENGINE] Running phase: {phase}")
    results = []
    for scanner_name in PHASES[phase]:
        results.append(run_scanner(scanner_name))

    failures = results.count(False)
    if failures:
        print(f"[ALPHA ENGINE] Phase {phase} completed with {failures} failure(s).")
        return False

    print(f"[ALPHA ENGINE] Phase {phase} completed successfully.")
    return True


def run_operation(name, dry_run=False, mark_missing_signals=BROKER_SYNC_MARK_MISSING_SIGNALS):
    print(f"\n[ALPHA ENGINE] Running operation: {name}")
    try:
        if name in {"context", "post-trade-review", "briefing", "energy-universe", "category-universe"}:
            result = OPERATIONS[name](dry_run=dry_run)
        elif name == "broker-sync":
            result = OPERATIONS[name](
                dry_run=dry_run,
                mark_missing_signals=mark_missing_signals,
            )
        else:
            result = OPERATIONS[name]()
    except Exception as exc:
        print(f"[ALPHA ENGINE] Operation failed: {name}: {exc}")
        return False

    if result is False:
        print(f"[ALPHA ENGINE] Operation returned failure: {name}")
        return False

    print(f"[ALPHA ENGINE] Operation complete: {name}")
    return True


def run_target(target, dry_run=False, mark_missing_signals=BROKER_SYNC_MARK_MISSING_SIGNALS):
    if target in PHASES:
        return run_phase(target)
    if target in SCANNERS:
        return run_scanner(target)
    if target in OPERATIONS:
        return run_operation(target, dry_run=dry_run, mark_missing_signals=mark_missing_signals)
    if target in WORKFLOWS:
        return run_workflow(target, dry_run=dry_run, mark_missing_signals=mark_missing_signals)
    raise ValueError(f"Unknown target: {target}")


def run_workflow(name, dry_run=False, mark_missing_signals=BROKER_SYNC_MARK_MISSING_SIGNALS):
    print(f"\n[ALPHA ENGINE] Running workflow: {name}")
    failures = 0
    for target in WORKFLOWS[name]:
        success = run_target(
            target,
            dry_run=dry_run,
            mark_missing_signals=mark_missing_signals,
        )
        if not success:
            failures += 1
            if target == "preflight":
                print("[ALPHA ENGINE] Preflight failed; stopping workflow.")
                break

    if failures:
        print(f"[ALPHA ENGINE] Workflow {name} completed with {failures} failure(s).")
        return False

    print(f"[ALPHA ENGINE] Workflow {name} completed successfully.")
    return True


def run_pre_market_sweep():
    """Runs once before the market opens to establish the daily baseline."""
    return run_phase("premarket")


def run_intraday_pulse():
    """Runs during market hours to catch live price action."""
    return run_phase("intraday")


def run_intraday_training_cycle():
    """Runs scanner pulse plus supporting training jobs."""
    return run_workflow("training-cycle")


def master_engine():
    print("[ALPHA ENGINE] Master Autonomous Controller Online.")
    pre_market_done = False
    category_universe_date = None

    while True:
        now = now_market_time()
        session = get_market_session(now)

        if session.name == "premarket":
            if not pre_market_done and now.weekday() < 5:
                if category_universe_date != now.date():
                    run_operation("category-universe")
                    category_universe_date = now.date()
                run_pre_market_sweep()
                pre_market_done = True
            else:
                print(f"[{now.strftime('%I:%M %p')}] Waiting for opening bell...")
                time.sleep(60)

            if SCAN_EXTENDED_HOURS:
                run_intraday_training_cycle()
                cooldown_minutes = max(1, SCANNER_INTERVAL_SECONDS // 60)
                print(f"\n[{now.strftime('%I:%M %p')}] Premarket loop complete. Cooling down for {cooldown_minutes} minutes.")
                time.sleep(max(60, SCANNER_INTERVAL_SECONDS))

        elif (
            session.is_regular
            or (SCAN_EXTENDED_HOURS and session.is_extended)
            or (SCAN_GLOBAL_OVERNIGHT and session.is_global_overnight)
        ):
            print(f"[{now.strftime('%I:%M %p')}] Market session: {session.name}")
            run_intraday_training_cycle()
            cooldown_minutes = max(1, SCANNER_INTERVAL_SECONDS // 60)
            print(f"\n[{now.strftime('%I:%M %p')}] Loop complete. Cooling down for {cooldown_minutes} minutes.")
            time.sleep(max(60, SCANNER_INTERVAL_SECONDS))

        else:
            if now.hour >= 16 and pre_market_done:
                print("\nMarket closed. Resetting for tomorrow.")
                pre_market_done = False

            print(f"[{now.strftime('%I:%M %p')}] Market is closed. Engine sleeping...")
            time.sleep(900)


def parse_args():
    choices = ["engine", *PHASES.keys(), *SCANNERS.keys(), *OPERATIONS.keys(), *WORKFLOWS.keys()]
    parser = argparse.ArgumentParser(description="Master scanner orchestrator.")
    parser.add_argument(
        "target",
        nargs="?",
        default="engine",
        choices=choices,
        help="Run the full scheduler, a phase, or a single scanner.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use dry-run mode for support operations that can write to Supabase.",
    )
    parser.add_argument(
        "--mark-missing-signals",
        action="store_true",
        default=BROKER_SYNC_MARK_MISSING_SIGNALS,
        help="Let broker-sync mark executed signals missing from IBKR as closed_external.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.target == "engine":
        master_engine()
    else:
        success = run_target(
            args.target,
            dry_run=args.dry_run,
            mark_missing_signals=args.mark_missing_signals,
        )
        raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
