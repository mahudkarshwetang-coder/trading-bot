import argparse
import json
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from config import (
    BROKER_SYNC_MARK_MISSING_SIGNALS,
    CATEGORY_MIN_SCORE,
    ENERGY_MIN_SCORE,
    MASTER_DAILY_CATEGORY_REFRESH_TIME,
    MASTER_EVENING_REVIEW_TIME,
    OPEN_SCANNER_ENABLED,
    PURE_TRAINING_MONITOR_INTERVAL_SECONDS,
    PURE_TRAINING_REVIEW_TIME,
    SCAN_EXTENDED_HOURS,
    SCAN_GLOBAL_OVERNIGHT,
    SCANNER_INTERVAL_SECONDS,
)
from cycle_logger import log_cycle_event, log_cycle_summary, new_cycle_id, seconds_between, utc_now_iso
from market_session import get_market_session, now_market_time, parse_time
from performance_governor import (
    adjust_poll_interval,
    describe_performance_profile,
    gaming_budget_pause,
    print_compute_notice,
    should_defer_work,
)
from config import resolve_ollama_model
from system_status import publish_system_status

STARTUP_STATE_PATH = Path("data/master_startup_state.json")
STARTUP_WORKFLOW = "daily-cycle"
DAILY_CATEGORY_REFRESH_WORKFLOW = "daily-category-refresh"
EVENING_REVIEW_WORKFLOW = "review-cycle"
PURE_TRAINING_CYCLE_WORKFLOW = "pure-training-cycle"
PURE_TRAINING_REVIEW_WORKFLOW = "pure-training-review"
EXPERIMENTAL_CYCLE_WORKFLOW = "experimental-cycle"
EXPERIMENTAL_REVIEW_WORKFLOW = "experimental-review"
HEAVY_SCANNERS = {
    "llm": ("LLM scanner pass across the active target pool", "llm_scanner"),
}
HEAVY_OPERATIONS = {
    "category-universe": ("broad Yahoo category-universe rebuild", None),
    "ticker-intel": ("ticker intelligence dossier sync", "scanner"),
    "briefing": ("daily market briefing generation", "briefing"),
    "post-trade-review": ("Qwen post-trade review analysis", "post_trade_review"),
    "strategy-optimizer": ("strategy optimizer review", "deep"),
}

CURRENT_CYCLE_ID = None
CURRENT_CYCLE_STARTED_AT = None
CURRENT_CYCLE_TYPE = None
CURRENT_CYCLE_SESSION = None
CURRENT_CYCLE_STATS = None


def reset_cycle_stats():
    return {
        "workflows": [],
        "scanners": [],
        "operations": [],
        "phases": [],
        "errors": [],
    }


@contextmanager
def cycle_scope(cycle_type, session=None, metadata=None):
    global CURRENT_CYCLE_ID
    global CURRENT_CYCLE_STARTED_AT
    global CURRENT_CYCLE_TYPE
    global CURRENT_CYCLE_SESSION
    global CURRENT_CYCLE_STATS

    parent_id = CURRENT_CYCLE_ID
    parent_started_at = CURRENT_CYCLE_STARTED_AT
    parent_type = CURRENT_CYCLE_TYPE
    parent_session = CURRENT_CYCLE_SESSION
    parent_stats = CURRENT_CYCLE_STATS

    cycle_id = new_cycle_id(cycle_type)
    started_at = utc_now_iso()
    session_name = getattr(session, "name", None) or session
    CURRENT_CYCLE_ID = cycle_id
    CURRENT_CYCLE_STARTED_AT = started_at
    CURRENT_CYCLE_TYPE = cycle_type
    CURRENT_CYCLE_SESSION = session_name
    CURRENT_CYCLE_STATS = reset_cycle_stats()

    log_cycle_event(
        cycle_id,
        "cycle",
        "running",
        "master_scanner",
        market_session=session_name,
        mode=describe_performance_profile(),
        started_at=started_at,
        detail=f"Started {cycle_type} cycle.",
        metadata=metadata,
    )

    status = "success"
    error = None
    try:
        yield cycle_id
    except Exception as exc:
        status = "error"
        error = str(exc)
        if CURRENT_CYCLE_STATS is not None:
            CURRENT_CYCLE_STATS["errors"].append({"component": "master_scanner", "error": error})
        raise
    finally:
        finished_at = utc_now_iso()
        stats = CURRENT_CYCLE_STATS or reset_cycle_stats()
        if status == "success" and stats.get("errors"):
            status = "error"
        log_cycle_summary(
            cycle_id,
            cycle_type,
            status,
            started_at,
            finished_at=finished_at,
            market_session=session_name,
            mode=describe_performance_profile(),
            workflows=stats["workflows"],
            scanners=stats["scanners"],
            operations=stats["operations"],
            phases=stats["phases"],
            errors=stats["errors"],
            metadata=metadata,
        )
        log_cycle_event(
            cycle_id,
            "cycle",
            status,
            "master_scanner",
            market_session=session_name,
            mode=describe_performance_profile(),
            started_at=started_at,
            finished_at=finished_at,
            detail=f"Finished {cycle_type} cycle.",
            error=error,
            metadata=metadata,
        )

        CURRENT_CYCLE_ID = parent_id
        CURRENT_CYCLE_STARTED_AT = parent_started_at
        CURRENT_CYCLE_TYPE = parent_type
        CURRENT_CYCLE_SESSION = parent_session
        CURRENT_CYCLE_STATS = parent_stats


def active_cycle_id():
    return CURRENT_CYCLE_ID or "manual"


def active_cycle_session():
    return CURRENT_CYCLE_SESSION


def record_cycle_item(kind, name, status, started_at, finished_at=None, error=None, metadata=None):
    if CURRENT_CYCLE_STATS is None:
        return
    item = {
        "name": name,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": None,
        "error": error,
        "metadata": metadata or {},
    }
    if started_at and finished_at:
        item["duration_seconds"] = seconds_between(started_at, finished_at)
    CURRENT_CYCLE_STATS[kind].append(item)
    if error:
        CURRENT_CYCLE_STATS["errors"].append({"component": name, "error": error})


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


def run_ibkr_news():
    from ibkr_news_scanner import run_ibkr_news_scanner

    return run_ibkr_news_scanner(once=True)


def run_technical():
    from tech_scanner import run_market_scan

    run_market_scan()


def run_opening_momentum():
    from opening_momentum_scanner import run_opening_momentum_scan

    return run_opening_momentum_scan()


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


def run_strategy_optimizer(dry_run=False):
    from strategy_optimizer import run_strategy_optimizer

    return run_strategy_optimizer(dry_run=dry_run)


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


def run_ticker_intel(dry_run=False):
    from ticker_intelligence import sync_ticker_intelligence

    sync_ticker_intelligence(dry_run=dry_run)
    return True


def run_runtime_config(dry_run=False):
    from runtime_config_sync import sync_runtime_config

    sync_runtime_config(dry_run=dry_run)
    return True


def run_routing_status():
    from routing_status import main as routing_status_main

    routing_status_main([])
    return True


def run_pure_training(dry_run=False):
    from pure_training_mode import run_pure_training_mode

    return run_pure_training_mode(dry_run=dry_run)


def run_pure_training_monitor(dry_run=False):
    from pure_training_mode import run_pure_training_monitor

    return run_pure_training_monitor(dry_run=dry_run)


def run_pure_training_adapt(dry_run=False):
    from pure_training_adaptation import run_pure_training_adaptation

    return run_pure_training_adaptation(dry_run=dry_run)


SCANNERS = {
    "macro": run_macro,
    "fundamental": run_fundamental,
    "categories": run_category_targets,
    "energy": run_energy_targets,
    "earnings": run_earnings,
    "radar": run_radar,
    "ibkr-news": run_ibkr_news,
    "sentiment": run_sentiment,
    "technical": run_technical,
    "open": run_opening_momentum,
    "llm": run_llm,
}

PHASES = {
    "premarket": ["macro", "categories", "earnings"],
    "fundamental_premarket": ["macro", "fundamental", "earnings"],
    "category_premarket": ["macro", "categories", "earnings"],
    "energy_premarket": ["macro", "energy", "earnings"],
    "opening-bell": ["open"],
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
    "strategy-optimizer": run_strategy_optimizer,
    "briefing": run_briefing,
    "energy-universe": run_energy_universe,
    "category-universe": run_category_universe,
    "ticker-intel": run_ticker_intel,
    "runtime-config": run_runtime_config,
    "routing-status": run_routing_status,
    "experimental": run_pure_training,
    "experimental-monitor": run_pure_training_monitor,
    "experimental-adapt": run_pure_training_adapt,
    "pure-training": run_pure_training,
    "pure-training-monitor": run_pure_training_monitor,
    "pure-training-adapt": run_pure_training_adapt,
}

WORKFLOWS = {
    DAILY_CATEGORY_REFRESH_WORKFLOW: ["category-universe", "categories"],
    EXPERIMENTAL_CYCLE_WORKFLOW: [
        "preflight",
        "runtime-config",
        "category-universe",
        "categories",
        "premarket",
        "intraday",
        "ticker-intel",
        "context",
        "experimental",
        "experimental-monitor",
        "broker-sync",
        "journal",
        "briefing",
    ],
    EXPERIMENTAL_REVIEW_WORKFLOW: [
        "experimental-monitor",
        "journal",
        "post-trade-review",
        "strategy-optimizer",
        "experimental-adapt",
        "briefing",
    ],
    PURE_TRAINING_CYCLE_WORKFLOW: [
        "preflight",
        "runtime-config",
        "category-universe",
        "categories",
        "ticker-intel",
        "context",
        "pure-training",
        "pure-training-monitor",
        "broker-sync",
        "journal",
    ],
    PURE_TRAINING_REVIEW_WORKFLOW: [
        "pure-training-monitor",
        "journal",
        "post-trade-review",
        "strategy-optimizer",
        "pure-training-adapt",
        "briefing",
    ],
    "training-cycle": ["categories", "intraday", "context", "broker-sync", "journal"],
    "daily-cycle": [
        "preflight",
        "runtime-config",
        "category-universe",
        "premarket",
        "intraday",
        "ticker-intel",
        "context",
        "broker-sync",
        "journal",
    ],
    "review-cycle": ["journal", "post-trade-review", "strategy-optimizer", "briefing"],
    "energy-cycle": ["preflight", "energy_premarket", "intraday", "context", "broker-sync", "journal"],
    "category-cycle": [
        "preflight",
        "runtime-config",
        "category-universe",
        "category_premarket",
        "intraday",
        "context",
        "broker-sync",
        "journal",
    ],
}


def is_market_open(now):
    """Backward-compatible regular-session check."""
    return get_market_session(now).is_regular


def load_startup_state():
    try:
        with STARTUP_STATE_PATH.open("r", encoding="utf-8") as state_file:
            data = json.load(state_file)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"[ALPHA ENGINE] Could not read startup state: {exc}")
        return {}


def save_startup_state(state):
    try:
        STARTUP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with STARTUP_STATE_PATH.open("w", encoding="utf-8") as state_file:
            json.dump(state, state_file, indent=2)
    except Exception as exc:
        print(f"[ALPHA ENGINE] Could not write startup state: {exc}")


def market_date_string(now=None):
    current = now or now_market_time()
    return current.date().isoformat()


def sleep_with_heartbeat(seconds, detail, session=None):
    total_seconds = max(0, int(seconds))
    if total_seconds <= 0:
        return

    current = now_market_time()
    wake_at = current + timedelta(seconds=total_seconds)
    session_name = session.name if session else get_market_session(current).name
    print(
        f"[ALPHA ENGINE] Next wake around {wake_at.strftime('%I:%M:%S %p')} "
        f"({max(1, total_seconds // 60)} minute(s))."
    )

    deadline = time.monotonic() + total_seconds
    while True:
        remaining = max(0, int(deadline - time.monotonic()))
        if remaining <= 0:
            break

        publish_system_status(
            "master_scanner",
            "sleeping",
            detail=detail,
            market_session=session_name,
            mode=describe_performance_profile(),
            metadata={
                "next_wake_at": wake_at.isoformat(),
                "remaining_seconds": remaining,
            },
        )
        time.sleep(min(60, remaining))


def run_scanner(name):
    started_at = utc_now_iso()
    if should_defer_work("scanner", name):
        finished_at = utc_now_iso()
        record_cycle_item("scanners", name, "deferred", started_at, finished_at)
        log_cycle_event(
            active_cycle_id(),
            "scanner",
            "deferred",
            f"scanner:{name}",
            target=name,
            market_session=active_cycle_session(),
            mode=describe_performance_profile(),
            started_at=started_at,
            finished_at=finished_at,
            detail="Deferred by performance governor.",
        )
        publish_system_status(
            f"scanner:{name}",
            "deferred",
            detail="Deferred by performance governor.",
            mode=describe_performance_profile(),
        )
        print(
            f"\n[ALPHA ENGINE] Performance governor deferred scanner: {name} "
            f"({describe_performance_profile()})"
        )
        return True

    gaming_budget_pause(f"scanner:{name}", estimated_work_seconds=1.5)
    scanner = SCANNERS[name]
    print(f"\n--- Starting scanner: {name} ---")
    if name in HEAVY_SCANNERS:
        detail, model_role = HEAVY_SCANNERS[name]
        model = resolve_ollama_model(model_role) if model_role else None
        print_compute_notice(f"scanner:{name}", detail, model=model)
    publish_system_status(
        f"scanner:{name}",
        "running",
        detail=f"Running scanner {name}.",
        mode=describe_performance_profile(),
    )
    try:
        result = scanner()
        if result is False:
            finished_at = utc_now_iso()
            record_cycle_item("scanners", name, "error", started_at, finished_at, error="Scanner returned failure.")
            log_cycle_event(
                active_cycle_id(),
                "scanner",
                "error",
                f"scanner:{name}",
                target=name,
                market_session=active_cycle_session(),
                mode=describe_performance_profile(),
                started_at=started_at,
                finished_at=finished_at,
                detail=f"Scanner {name} returned failure.",
                error="Scanner returned failure.",
            )
            print(f"--- Scanner returned failure: {name} ---")
            publish_system_status(
                f"scanner:{name}",
                "error",
                detail=f"Scanner {name} returned failure.",
                mode=describe_performance_profile(),
            )
            return False
        finished_at = utc_now_iso()
        record_cycle_item("scanners", name, "success", started_at, finished_at)
        log_cycle_event(
            active_cycle_id(),
            "scanner",
            "success",
            f"scanner:{name}",
            target=name,
            market_session=active_cycle_session(),
            mode=describe_performance_profile(),
            started_at=started_at,
            finished_at=finished_at,
            detail=f"Scanner {name} completed.",
        )
        print(f"--- Scanner complete: {name} ---")
        publish_system_status(
            f"scanner:{name}",
            "success",
            detail=f"Scanner {name} completed.",
            mode=describe_performance_profile(),
        )
        return True
    except Exception as exc:
        finished_at = utc_now_iso()
        record_cycle_item("scanners", name, "error", started_at, finished_at, error=str(exc))
        log_cycle_event(
            active_cycle_id(),
            "scanner",
            "error",
            f"scanner:{name}",
            target=name,
            market_session=active_cycle_session(),
            mode=describe_performance_profile(),
            started_at=started_at,
            finished_at=finished_at,
            detail=f"Scanner {name} failed.",
            error=str(exc),
        )
        print(f"Scanner failed: {name}: {exc}")
        publish_system_status(
            f"scanner:{name}",
            "error",
            detail=f"Scanner {name} failed.",
            mode=describe_performance_profile(),
            error=str(exc),
        )
        return False


def run_phase(phase):
    started_at = utc_now_iso()
    print(f"\n[ALPHA ENGINE] Running phase: {phase}")
    publish_system_status(
        f"phase:{phase}",
        "running",
        detail=f"Running phase {phase}.",
        mode=describe_performance_profile(),
    )
    results = []
    for scanner_name in PHASES[phase]:
        results.append(run_scanner(scanner_name))

    failures = results.count(False)
    if failures:
        finished_at = utc_now_iso()
        record_cycle_item(
            "phases",
            phase,
            "error",
            started_at,
            finished_at,
            error=f"{failures} scanner failure(s).",
            metadata={"failures": failures},
        )
        log_cycle_event(
            active_cycle_id(),
            "phase",
            "error",
            f"phase:{phase}",
            target=phase,
            market_session=active_cycle_session(),
            mode=describe_performance_profile(),
            started_at=started_at,
            finished_at=finished_at,
            detail=f"Phase {phase} completed with {failures} failure(s).",
            error=f"{failures} scanner failure(s).",
            metadata={"failures": failures},
        )
        print(f"[ALPHA ENGINE] Phase {phase} completed with {failures} failure(s).")
        publish_system_status(
            f"phase:{phase}",
            "error",
            detail=f"Phase {phase} completed with {failures} failure(s).",
            mode=describe_performance_profile(),
            metadata={"failures": failures},
        )
        return False

    finished_at = utc_now_iso()
    record_cycle_item("phases", phase, "success", started_at, finished_at)
    log_cycle_event(
        active_cycle_id(),
        "phase",
        "success",
        f"phase:{phase}",
        target=phase,
        market_session=active_cycle_session(),
        mode=describe_performance_profile(),
        started_at=started_at,
        finished_at=finished_at,
        detail=f"Phase {phase} completed successfully.",
    )
    print(f"[ALPHA ENGINE] Phase {phase} completed successfully.")
    publish_system_status(
        f"phase:{phase}",
        "success",
        detail=f"Phase {phase} completed successfully.",
        mode=describe_performance_profile(),
    )
    return True


def run_operation(name, dry_run=False, mark_missing_signals=BROKER_SYNC_MARK_MISSING_SIGNALS):
    started_at = utc_now_iso()
    if should_defer_work("operation", name):
        finished_at = utc_now_iso()
        record_cycle_item("operations", name, "deferred", started_at, finished_at, metadata={"dry_run": dry_run})
        log_cycle_event(
            active_cycle_id(),
            "operation",
            "deferred",
            f"operation:{name}",
            target=name,
            market_session=active_cycle_session(),
            mode=describe_performance_profile(),
            started_at=started_at,
            finished_at=finished_at,
            detail="Deferred by performance governor.",
            metadata={"dry_run": dry_run},
        )
        publish_system_status(
            f"operation:{name}",
            "deferred",
            detail="Deferred by performance governor.",
            mode=describe_performance_profile(),
            metadata={"dry_run": dry_run},
        )
        print(
            f"\n[ALPHA ENGINE] Performance governor deferred operation: {name} "
            f"({describe_performance_profile()})"
        )
        return True

    gaming_budget_pause(
        f"operation:{name}",
        estimated_work_seconds=0.75,
        critical=name in {"broker-sync", "journal"},
    )
    print(f"\n[ALPHA ENGINE] Running operation: {name}")
    if name in HEAVY_OPERATIONS:
        detail, model_role = HEAVY_OPERATIONS[name]
        model = resolve_ollama_model(model_role) if model_role else None
        print_compute_notice(f"operation:{name}", detail, model=model)
    publish_system_status(
        f"operation:{name}",
        "running",
        detail=f"Running operation {name}.",
        mode=describe_performance_profile(),
        metadata={"dry_run": dry_run},
    )
    try:
        if name in {
            "context",
            "post-trade-review",
            "strategy-optimizer",
            "briefing",
            "energy-universe",
            "category-universe",
            "ticker-intel",
            "runtime-config",
            "pure-training",
            "pure-training-monitor",
            "pure-training-adapt",
        }:
            result = OPERATIONS[name](dry_run=dry_run)
        elif name == "broker-sync":
            result = OPERATIONS[name](
                dry_run=dry_run,
                mark_missing_signals=mark_missing_signals,
            )
        else:
            result = OPERATIONS[name]()
    except Exception as exc:
        finished_at = utc_now_iso()
        record_cycle_item(
            "operations",
            name,
            "error",
            started_at,
            finished_at,
            error=str(exc),
            metadata={"dry_run": dry_run},
        )
        log_cycle_event(
            active_cycle_id(),
            "operation",
            "error",
            f"operation:{name}",
            target=name,
            market_session=active_cycle_session(),
            mode=describe_performance_profile(),
            started_at=started_at,
            finished_at=finished_at,
            detail=f"Operation {name} failed.",
            error=str(exc),
            metadata={"dry_run": dry_run},
        )
        print(f"[ALPHA ENGINE] Operation failed: {name}: {exc}")
        publish_system_status(
            f"operation:{name}",
            "error",
            detail=f"Operation {name} failed.",
            mode=describe_performance_profile(),
            error=str(exc),
            metadata={"dry_run": dry_run},
        )
        return False

    if result is False:
        finished_at = utc_now_iso()
        record_cycle_item(
            "operations",
            name,
            "error",
            started_at,
            finished_at,
            error="Operation returned failure.",
            metadata={"dry_run": dry_run},
        )
        log_cycle_event(
            active_cycle_id(),
            "operation",
            "error",
            f"operation:{name}",
            target=name,
            market_session=active_cycle_session(),
            mode=describe_performance_profile(),
            started_at=started_at,
            finished_at=finished_at,
            detail=f"Operation {name} returned failure.",
            error="Operation returned failure.",
            metadata={"dry_run": dry_run},
        )
        print(f"[ALPHA ENGINE] Operation returned failure: {name}")
        publish_system_status(
            f"operation:{name}",
            "error",
            detail=f"Operation {name} returned failure.",
            mode=describe_performance_profile(),
            metadata={"dry_run": dry_run},
        )
        return False

    finished_at = utc_now_iso()
    record_cycle_item("operations", name, "success", started_at, finished_at, metadata={"dry_run": dry_run})
    log_cycle_event(
        active_cycle_id(),
        "operation",
        "success",
        f"operation:{name}",
        target=name,
        market_session=active_cycle_session(),
        mode=describe_performance_profile(),
        started_at=started_at,
        finished_at=finished_at,
        detail=f"Operation {name} completed.",
        metadata={"dry_run": dry_run},
    )
    print(f"[ALPHA ENGINE] Operation complete: {name}")
    publish_system_status(
        f"operation:{name}",
        "success",
        detail=f"Operation {name} completed.",
        mode=describe_performance_profile(),
        metadata={"dry_run": dry_run},
    )
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


def run_workflow(
    name,
    dry_run=False,
    mark_missing_signals=BROKER_SYNC_MARK_MISSING_SIGNALS,
    continue_on_failures=None,
):
    started_at = utc_now_iso()
    print(f"\n[ALPHA ENGINE] Running workflow: {name}")
    publish_system_status(
        f"workflow:{name}",
        "running",
        detail=f"Running workflow {name}.",
        mode=describe_performance_profile(),
    )
    continue_on_failures = set(continue_on_failures or [])
    failures = 0
    for target in WORKFLOWS[name]:
        success = run_target(
            target,
            dry_run=dry_run,
            mark_missing_signals=mark_missing_signals,
        )
        if not success:
            failures += 1
            if target in continue_on_failures:
                print(f"[ALPHA ENGINE] Continuing workflow after non-critical failure: {target}")
                continue
            if target == "preflight":
                print("[ALPHA ENGINE] Preflight failed; stopping workflow.")
                break

    if failures:
        finished_at = utc_now_iso()
        record_cycle_item(
            "workflows",
            name,
            "error",
            started_at,
            finished_at,
            error=f"{failures} target failure(s).",
            metadata={"failures": failures},
        )
        log_cycle_event(
            active_cycle_id(),
            "workflow",
            "error",
            f"workflow:{name}",
            target=name,
            market_session=active_cycle_session(),
            mode=describe_performance_profile(),
            started_at=started_at,
            finished_at=finished_at,
            detail=f"Workflow {name} completed with {failures} failure(s).",
            error=f"{failures} target failure(s).",
            metadata={"failures": failures},
        )
        print(f"[ALPHA ENGINE] Workflow {name} completed with {failures} failure(s).")
        publish_system_status(
            f"workflow:{name}",
            "error",
            detail=f"Workflow {name} completed with {failures} failure(s).",
            mode=describe_performance_profile(),
            metadata={"failures": failures},
        )
        return False

    finished_at = utc_now_iso()
    record_cycle_item("workflows", name, "success", started_at, finished_at)
    log_cycle_event(
        active_cycle_id(),
        "workflow",
        "success",
        f"workflow:{name}",
        target=name,
        market_session=active_cycle_session(),
        mode=describe_performance_profile(),
        started_at=started_at,
        finished_at=finished_at,
        detail=f"Workflow {name} completed successfully.",
    )
    print(f"[ALPHA ENGINE] Workflow {name} completed successfully.")
    publish_system_status(
        f"workflow:{name}",
        "success",
        detail=f"Workflow {name} completed successfully.",
        mode=describe_performance_profile(),
    )
    return True


def run_daily_startup_once(now=None):
    current = now or now_market_time()
    today = market_date_string(current)
    state = load_startup_state()
    if state.get("last_daily_startup_date") == today:
        print(f"[ALPHA ENGINE] Daily startup workflow already completed for {today}.")
        return True

    print(f"[ALPHA ENGINE] Running once-per-day startup workflow for {today}: {STARTUP_WORKFLOW}")
    with cycle_scope(
        "daily-startup",
        session=get_market_session(current).name,
        metadata={"workflow": STARTUP_WORKFLOW, "market_date": today},
    ):
        success = run_workflow(
            STARTUP_WORKFLOW,
            continue_on_failures={"broker-sync", "context", "journal"},
        )
    state["last_daily_startup_date"] = today
    state["last_daily_startup_success"] = bool(success)
    state["last_daily_startup_finished_at"] = now_market_time().isoformat()
    save_startup_state(state)
    if not success:
        print("[ALPHA ENGINE] Daily startup workflow had failures; entering engine loop anyway.")
    return success


def seconds_until_daily_category_refresh(now=None):
    current = now or now_market_time()
    refresh_time = parse_time(MASTER_DAILY_CATEGORY_REFRESH_TIME)
    scheduled = current.replace(
        hour=refresh_time.hour,
        minute=refresh_time.minute,
        second=0,
        microsecond=0,
    )
    return max(0, int((scheduled - current).total_seconds()))


def run_daily_category_refresh_once(now=None, force=False):
    current = now or now_market_time()
    today = market_date_string(current)
    state = load_startup_state()
    state_key = "last_daily_category_refresh_date"

    if not force and state.get(state_key) == today:
        return True

    if not force and seconds_until_daily_category_refresh(current) > 0:
        return False

    print(
        f"[ALPHA ENGINE] Running daily category refresh for {today} "
        f"at/after {MASTER_DAILY_CATEGORY_REFRESH_TIME}: {DAILY_CATEGORY_REFRESH_WORKFLOW}"
    )
    with cycle_scope(
        "daily-category-refresh",
        session=get_market_session(current).name,
        metadata={"workflow": DAILY_CATEGORY_REFRESH_WORKFLOW, "market_date": today},
    ):
        success = run_workflow(DAILY_CATEGORY_REFRESH_WORKFLOW)
    state[state_key] = today
    state["last_daily_category_refresh_success"] = bool(success)
    state["last_daily_category_refresh_finished_at"] = now_market_time().isoformat()
    save_startup_state(state)
    if not success:
        print("[ALPHA ENGINE] Daily category refresh completed with failures.")
    return bool(success)


def seconds_until_evening_review(now=None):
    current = now or now_market_time()
    review_time = parse_time(MASTER_EVENING_REVIEW_TIME)
    scheduled = current.replace(
        hour=review_time.hour,
        minute=review_time.minute,
        second=0,
        microsecond=0,
    )
    return max(0, int((scheduled - current).total_seconds()))


def run_evening_review_once(now=None, force=False):
    current = now or now_market_time()
    today = market_date_string(current)
    state = load_startup_state()
    state_key = "last_evening_review_date"

    if not force and state.get(state_key) == today:
        return True

    if not force and seconds_until_evening_review(current) > 0:
        return False

    print(
        f"[ALPHA ENGINE] Running evening review for {today} "
        f"at/after {MASTER_EVENING_REVIEW_TIME}: {EVENING_REVIEW_WORKFLOW}"
    )
    with cycle_scope(
        "evening-review",
        session=get_market_session(current).name,
        metadata={"workflow": EVENING_REVIEW_WORKFLOW, "market_date": today},
    ):
        success = run_workflow(
            EVENING_REVIEW_WORKFLOW,
            continue_on_failures={"journal", "post-trade-review", "strategy-optimizer", "briefing"},
        )
    state[state_key] = today
    state["last_evening_review_success"] = bool(success)
    state["last_evening_review_finished_at"] = now_market_time().isoformat()
    save_startup_state(state)
    if not success:
        print("[ALPHA ENGINE] Evening review completed with failures.")
    return bool(success)


def seconds_until_pure_training_review(now=None):
    current = now or now_market_time()
    review_time = parse_time(PURE_TRAINING_REVIEW_TIME)
    scheduled = current.replace(
        hour=review_time.hour,
        minute=review_time.minute,
        second=0,
        microsecond=0,
    )
    return max(0, int((scheduled - current).total_seconds()))


def run_pure_training_cycle_once(now=None, force=False):
    current = now or now_market_time()
    today = market_date_string(current)
    state = load_startup_state()
    state_key = "last_pure_training_cycle_date"

    if not force and state.get(state_key) == today:
        return True

    if not force and seconds_until_daily_category_refresh(current) > 0:
        return False

    print(
        f"[ALPHA ENGINE] Running experimental cycle for {today} "
        f"at/after {MASTER_DAILY_CATEGORY_REFRESH_TIME}: {EXPERIMENTAL_CYCLE_WORKFLOW}"
    )
    with cycle_scope(
        EXPERIMENTAL_CYCLE_WORKFLOW,
        session=get_market_session(current).name,
        metadata={"workflow": EXPERIMENTAL_CYCLE_WORKFLOW, "market_date": today},
    ):
        success = run_workflow(
            EXPERIMENTAL_CYCLE_WORKFLOW,
            continue_on_failures={"context", "experimental-monitor", "broker-sync", "journal", "briefing"},
        )
    state[state_key] = today
    state["last_pure_training_cycle_success"] = bool(success)
    state["last_pure_training_cycle_finished_at"] = now_market_time().isoformat()
    save_startup_state(state)
    if not success:
        print("[ALPHA ENGINE] Experimental cycle completed with failures.")
    return bool(success)


def run_pure_training_review_once(now=None, force=False):
    current = now or now_market_time()
    today = market_date_string(current)
    state = load_startup_state()
    state_key = "last_pure_training_review_date"

    if not force and state.get(state_key) == today:
        return True

    if not force and seconds_until_pure_training_review(current) > 0:
        return False

    print(
        f"[ALPHA ENGINE] Running experimental review for {today} "
        f"at/after {PURE_TRAINING_REVIEW_TIME}: {EXPERIMENTAL_REVIEW_WORKFLOW}"
    )
    with cycle_scope(
        EXPERIMENTAL_REVIEW_WORKFLOW,
        session=get_market_session(current).name,
        metadata={"workflow": EXPERIMENTAL_REVIEW_WORKFLOW, "market_date": today},
    ):
        success = run_workflow(
            EXPERIMENTAL_REVIEW_WORKFLOW,
            continue_on_failures={
                "experimental-monitor",
                "journal",
                "post-trade-review",
                "strategy-optimizer",
                "experimental-adapt",
                "briefing",
            },
        )
    state[state_key] = today
    state["last_pure_training_review_success"] = bool(success)
    state["last_pure_training_review_finished_at"] = now_market_time().isoformat()
    save_startup_state(state)
    if not success:
        print("[ALPHA ENGINE] Experimental review completed with failures.")
    return bool(success)


def run_pre_market_sweep():
    """Runs once before the market opens to establish the daily baseline."""
    return run_phase("premarket")


def run_intraday_pulse():
    """Runs during market hours to catch live price action."""
    return run_phase("intraday")


def run_intraday_training_cycle():
    """Runs scanner pulse plus supporting training jobs."""
    return run_workflow("training-cycle")


def maybe_run_opening_scan(now, opening_scan_date):
    if not OPEN_SCANNER_ENABLED or opening_scan_date == now.date():
        return opening_scan_date

    try:
        from opening_momentum_scanner import is_opening_scan_ready

        if is_opening_scan_ready(now):
            print("[ALPHA ENGINE] Opening-bell window detected.")
            if run_scanner("open"):
                return now.date()
    except Exception as exc:
        print(f"[ALPHA ENGINE] Opening-bell scan check failed: {exc}")

    return opening_scan_date


def master_engine(force_daily_category_refresh=False, force_evening_review=False):
    print("[ALPHA ENGINE] Master Autonomous Controller Online.")
    publish_system_status(
        "master_scanner",
        "running",
        detail="Master autonomous controller online.",
        mode=describe_performance_profile(),
    )
    pre_market_done = False
    startup_date = None
    opening_scan_date = None
    force_category_refresh = force_daily_category_refresh
    force_review = force_evening_review

    while True:
        now = now_market_time()
        session = get_market_session(now)
        publish_system_status(
            "master_scanner",
            "running",
            detail=f"Market session: {session.name}.",
            market_session=session.name,
            mode=describe_performance_profile(),
        )
        if startup_date != now.date():
            run_daily_startup_once(now)
            startup_date = now.date()
            pre_market_done = session.name != "premarket"
            opening_scan_date = None

        run_daily_category_refresh_once(now, force=force_category_refresh)
        force_category_refresh = False
        run_evening_review_once(now, force=force_review)
        force_review = False

        if session.name == "premarket":
            if not pre_market_done and now.weekday() < 5:
                with cycle_scope(
                    "premarket-sweep",
                    session=session.name,
                    metadata={"market_date": market_date_string(now)},
                ):
                    run_pre_market_sweep()
                pre_market_done = True
            else:
                print(f"[{now.strftime('%I:%M %p')}] Waiting for opening bell...")
                sleep_with_heartbeat(60, "Waiting for opening bell.", session=session)

            if SCAN_EXTENDED_HOURS:
                with cycle_scope(
                    "premarket-loop",
                    session=session.name,
                    metadata={"workflow": "training-cycle", "market_date": market_date_string(now)},
                ):
                    run_intraday_training_cycle()
                sleep_seconds = adjust_poll_interval(SCANNER_INTERVAL_SECONDS)
                cooldown_minutes = max(1, sleep_seconds // 60)
                print(f"\n[{now.strftime('%I:%M %p')}] Premarket loop complete. Cooling down for {cooldown_minutes} minutes.")
                publish_system_status(
                    "master_scanner",
                    "sleeping",
                    detail=f"Premarket cooldown for {cooldown_minutes} minute(s).",
                    market_session=session.name,
                    mode=describe_performance_profile(),
                    metadata={"sleep_seconds": sleep_seconds},
                )
                sleep_with_heartbeat(
                    max(60, sleep_seconds),
                    f"Premarket cooldown for {cooldown_minutes} minute(s).",
                    session=session,
                )

        elif (
            session.is_regular
            or (SCAN_EXTENDED_HOURS and session.is_extended)
            or (SCAN_GLOBAL_OVERNIGHT and session.is_global_overnight)
        ):
            print(f"[{now.strftime('%I:%M %p')}] Market session: {session.name}")
            with cycle_scope(
                "market-loop",
                session=session.name,
                metadata={"workflow": "training-cycle", "market_date": market_date_string(now)},
            ):
                if session.is_regular:
                    opening_scan_date = maybe_run_opening_scan(now, opening_scan_date)
                run_intraday_training_cycle()
            sleep_seconds = adjust_poll_interval(SCANNER_INTERVAL_SECONDS)
            cooldown_minutes = max(1, sleep_seconds // 60)
            print(f"\n[{now.strftime('%I:%M %p')}] Loop complete. Cooling down for {cooldown_minutes} minutes.")
            publish_system_status(
                "master_scanner",
                "sleeping",
                detail=f"Market loop cooldown for {cooldown_minutes} minute(s).",
                market_session=session.name,
                mode=describe_performance_profile(),
                metadata={"sleep_seconds": sleep_seconds},
            )
            sleep_with_heartbeat(
                max(60, sleep_seconds),
                f"Market loop cooldown for {cooldown_minutes} minute(s).",
                session=session,
            )

        else:
            if now.hour >= 16 and pre_market_done:
                print("\nMarket closed. Resetting for tomorrow.")
                pre_market_done = False

            print(f"[{now.strftime('%I:%M %p')}] Market is closed. Engine sleeping...")
            sleep_seconds = 900
            seconds_to_category_refresh = seconds_until_daily_category_refresh(now)
            seconds_to_evening_review = seconds_until_evening_review(now)
            if seconds_to_category_refresh > 0:
                sleep_seconds = min(sleep_seconds, max(60, seconds_to_category_refresh))
            if seconds_to_evening_review > 0:
                sleep_seconds = min(sleep_seconds, max(60, seconds_to_evening_review))
            publish_system_status(
                "master_scanner",
                "sleeping",
                detail="Market closed; waiting for next scheduled wake.",
                market_session=session.name,
                mode=describe_performance_profile(),
                metadata={"sleep_seconds": sleep_seconds},
            )
            sleep_with_heartbeat(
                sleep_seconds,
                "Market closed; waiting for next scheduled wake.",
                session=session,
            )


def pure_training_engine(force_daily_category_refresh=False, force_evening_review=False):
    print("[ALPHA ENGINE] Experimental Controller Online.")
    publish_system_status(
        "experimental_engine",
        "running",
        detail="Experimental controller online.",
        mode=describe_performance_profile(),
    )
    force_cycle = force_daily_category_refresh
    force_review = force_evening_review

    while True:
        now = now_market_time()
        session = get_market_session(now)
        publish_system_status(
            "experimental_engine",
            "running",
            detail=f"Experimental session: {session.name}.",
            market_session=session.name,
            mode=describe_performance_profile(),
        )

        run_pure_training_cycle_once(now, force=force_cycle)
        force_cycle = False
        run_pure_training_review_once(now, force=force_review)
        force_review = False

        active_session = (
            session.is_regular
            or (SCAN_EXTENDED_HOURS and session.is_extended)
            or (SCAN_GLOBAL_OVERNIGHT and session.is_global_overnight)
            or session.name == "premarket"
        )

        if active_session:
            with cycle_scope(
                "pure-training-monitor",
                session=session.name,
                metadata={
                    "workflow": "experimental-monitor",
                    "market_date": market_date_string(now),
                    "monitor_interval_seconds": PURE_TRAINING_MONITOR_INTERVAL_SECONDS,
                },
            ):
                run_operation("experimental-monitor")

            sleep_seconds = max(60, int(PURE_TRAINING_MONITOR_INTERVAL_SECONDS))
            print(
                f"\n[{now.strftime('%I:%M %p')}] Experimental monitor complete. "
                f"Cooling down for {max(1, sleep_seconds // 60)} minute(s)."
            )
            sleep_with_heartbeat(
                sleep_seconds,
                "Experimental monitor cooldown.",
                session=session,
            )
            continue

        print(f"[{now.strftime('%I:%M %p')}] Experimental engine is waiting for the next scheduled event.")
        sleep_seconds = 900
        seconds_to_category_refresh = seconds_until_daily_category_refresh(now)
        seconds_to_review = seconds_until_pure_training_review(now)
        if seconds_to_category_refresh > 0:
            sleep_seconds = min(sleep_seconds, max(60, seconds_to_category_refresh))
        if seconds_to_review > 0:
            sleep_seconds = min(sleep_seconds, max(60, seconds_to_review))
        sleep_with_heartbeat(
            sleep_seconds,
            "Experimental engine waiting for next scheduled wake.",
            session=session,
        )


def parse_args():
    choices = ["engine", "training-engine", "experimental-engine", "pure-training-engine", *PHASES.keys(), *SCANNERS.keys(), *OPERATIONS.keys(), *WORKFLOWS.keys()]
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
    parser.add_argument(
        "--force-daily-startup",
        action="store_true",
        help="When running the engine, run daily-cycle startup even if today's marker exists.",
    )
    parser.add_argument(
        "--force-daily-category-refresh",
        action="store_true",
        help="When running the engine, run category-universe/categories even if today's marker exists.",
    )
    parser.add_argument(
        "--force-evening-review",
        action="store_true",
        help="When running the engine, run review-cycle even if today's marker exists.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.target in {"engine", "training-engine", "experimental-engine", "pure-training-engine"}:
        if args.force_daily_startup and STARTUP_STATE_PATH.exists():
            state = load_startup_state()
            state.pop("last_daily_startup_date", None)
            save_startup_state(state)
        if args.force_daily_category_refresh and STARTUP_STATE_PATH.exists():
            state = load_startup_state()
            if args.target in {"experimental-engine", "pure-training-engine"}:
                state.pop("last_pure_training_cycle_date", None)
            else:
                state.pop("last_daily_category_refresh_date", None)
            save_startup_state(state)
        if args.force_evening_review and STARTUP_STATE_PATH.exists():
            state = load_startup_state()
            if args.target in {"experimental-engine", "pure-training-engine"}:
                state.pop("last_pure_training_review_date", None)
            else:
                state.pop("last_evening_review_date", None)
            save_startup_state(state)
        if args.target in {"experimental-engine", "pure-training-engine"}:
            pure_training_engine(
                force_daily_category_refresh=args.force_daily_category_refresh,
                force_evening_review=args.force_evening_review,
            )
        else:
            master_engine(
                force_daily_category_refresh=args.force_daily_category_refresh,
                force_evening_review=args.force_evening_review,
            )
    else:
        now = now_market_time()
        session = get_market_session(now)
        with cycle_scope(
            f"manual-{args.target}",
            session=session.name,
            metadata={
                "target": args.target,
                "dry_run": args.dry_run,
                "mark_missing_signals": args.mark_missing_signals,
                "market_date": market_date_string(now),
            },
        ):
            success = run_target(
                args.target,
                dry_run=args.dry_run,
                mark_missing_signals=args.mark_missing_signals,
            )
        raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
