import argparse
import time
from datetime import datetime

import pytz

from config import SCANNER_INTERVAL_SECONDS

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


def run_pre_market_sweep():
    """Runs once before the market opens to establish the daily baseline."""
    return run_phase("premarket")


def run_intraday_pulse():
    """Runs during market hours to catch live price action."""
    return run_phase("intraday")


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
            run_intraday_pulse()
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
    choices = ["engine", *PHASES.keys(), *SCANNERS.keys()]
    parser = argparse.ArgumentParser(description="Master scanner orchestrator.")
    parser.add_argument(
        "target",
        nargs="?",
        default="engine",
        choices=choices,
        help="Run the full scheduler, a phase, or a single scanner.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.target == "engine":
        master_engine()
    elif args.target in PHASES:
        success = run_phase(args.target)
        raise SystemExit(0 if success else 1)
    else:
        success = run_scanner(args.target)
        raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
