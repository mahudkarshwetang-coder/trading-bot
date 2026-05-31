import argparse
import time
from datetime import datetime

import pytz

from config import (
    BROKER_SYNC_MARK_MISSING_SIGNALS,
    ENERGY_MIN_SCORE,
    SCANNER_INTERVAL_SECONDS,
)

EST = pytz.timezone("US/Eastern")


def run_macro():
    from macro_scanner import run_macro_scan

    run_macro_scan()


def run_fundamental():
    from fundamental_scanner import run_fundamental_scan

    run_fundamental_scan()


def run_energy_targets():
    from energy_target_scanner import run_energy_target_scan

    run_energy_target_scan()


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


def run_energy_universe(dry_run=False):
    from energy_universe_builder import DEFAULT_LIMIT, build_energy_universe

    build_energy_universe(
        limit=DEFAULT_LIMIT,
        min_score=ENERGY_MIN_SCORE,
        dry_run=dry_run,
    )
    return True


SCANNERS = {
    "macro": run_macro,
    "fundamental": run_fundamental,
    "energy": run_energy_targets,
    "earnings": run_earnings,
    "radar": run_radar,
    "sentiment": run_sentiment,
    "technical": run_technical,
    "llm": run_llm,
}

PHASES = {
    "premarket": ["macro", "fundamental", "earnings"],
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
    "energy-universe": run_energy_universe,
}

WORKFLOWS = {
    "training-cycle": ["intraday", "context", "broker-sync", "journal"],
    "daily-cycle": ["preflight", "premarket", "intraday", "context", "broker-sync", "journal"],
    "energy-cycle": ["preflight", "energy_premarket", "intraday", "context", "broker-sync", "journal"],
}


def is_market_open(now):
    """Checks if the current time is between 9:30 AM and 4:00 PM EST on a weekday."""
    if now.weekday() >= 5:
        return False

    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)

    return market_open <= now <= market_close


def run_scanner(name):
    scanner = SCANNERS[name]
    print(f"\n--- Starting scanner: {name} ---")
    try:
        scanner()
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
        if name in {"context", "energy-universe"}:
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

    while True:
        now = datetime.now(EST)

        if 8 <= now.hour < 9 or (now.hour == 9 and now.minute < 30):
            if not pre_market_done and now.weekday() < 5:
                run_pre_market_sweep()
                pre_market_done = True
            else:
                print(f"[{now.strftime('%I:%M %p')}] Waiting for opening bell...")
                time.sleep(60)

        elif is_market_open(now):
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
