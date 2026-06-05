import argparse
import json
import os
import socket
import subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    ALLOW_EXTENDED_HOURS_TRADING,
    ALLOW_GLOBAL_OVERNIGHT_TRADING,
    DRY_RUN,
    EXECUTION_GATE_ENABLED,
    EXECUTION_GATE_MIN_SCORE,
    FIXED_ORDER_QUANTITY,
    IBKR_HOST,
    IBKR_PORT,
    MARKET_TIMEZONE,
    get_supabase_client,
)
from market_session import get_market_session


ROUTEABLE_STATUS = "approved"
PENDING_STATUS = "pending"
EXECUTED_STATUSES = {"executed", "dry_run"}
BLOCKED_PREFIX = "blocked"


def utc_now():
    return datetime.now(timezone.utc)


def since_iso(hours):
    return (utc_now() - timedelta(hours=max(1, int(hours)))).isoformat()


def bool_text(value):
    return "ON" if bool(value) else "OFF"


def trim(value, width=110):
    text = " ".join(str(value or "").split())
    if len(text) <= width:
        return text
    return text[: max(0, width - 3)].rstrip() + "..."


def short_time(value):
    if not value:
        return ""
    text = str(value).replace("T", " ")
    return text[:19]


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def check_socket(host, port, timeout=2.0):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, "reachable"
    except Exception as exc:
        return False, str(exc)


def powershell_process_query(script_names):
    quoted = "|".join(name.replace(".", r"\.") for name in script_names)
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -match 'python' -and "
        f"$_.CommandLine -match '({quoted})' }} | "
        "Select-Object ProcessId,CreationDate,CommandLine | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=8,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    parsed = json.loads(result.stdout)
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return parsed
    return []


def find_runtime_processes():
    if os.name != "nt":
        return []
    try:
        return powershell_process_query(["main.py", "master_scanner.py"]) or []
    except Exception:
        return []


def parse_powershell_json_date(value):
    text = str(value or "")
    match = None
    try:
        import re

        match = re.search(r"/Date\((\d+)\)/", text)
    except Exception:
        match = None
    if match:
        try:
            return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc)
        except Exception:
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def latest_mtime(paths):
    newest = None
    for path in paths:
        try:
            mtime = datetime.fromtimestamp(Path(path).stat().st_mtime, tz=timezone.utc)
        except Exception:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return newest


def process_for_script(processes, script_name):
    script_name = script_name.lower()
    for process in processes:
        if script_name in str(process.get("CommandLine") or "").lower():
            return process
    return None


def process_is_stale(process, paths):
    if not process:
        return False, None, None
    started_at = parse_powershell_json_date(process.get("CreationDate"))
    changed_at = latest_mtime(paths)
    if not started_at or not changed_at:
        return False, started_at, changed_at
    return started_at < changed_at, started_at, changed_at
    try:
        return powershell_process_query(["main.py", "master_scanner.py"])
    except Exception:
        return []


def fetch_bot_settings(supabase):
    try:
        rows = supabase.table("bot_settings").select("*").eq("id", 1).execute().data or []
        return rows[0] if rows else {}
    except Exception as exc:
        return {"_error": str(exc)}


def fetch_signals(supabase, hours, limit):
    query = (
        supabase.table("market_signals")
        .select(
            "id,ticker,action_type,status,channel,confidence_score,"
            "price_at_signal,execution_price,created_at,investment_memo"
        )
        .gte("created_at", since_iso(hours))
        .order("created_at", desc=True)
        .limit(limit)
    )
    return query.execute().data or []


def fetch_status_rows(supabase, status, limit=50):
    return (
        supabase.table("market_signals")
        .select(
            "id,ticker,action_type,status,channel,confidence_score,"
            "price_at_signal,execution_price,created_at,investment_memo"
        )
        .eq("status", status)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data
        or []
    )


def fetch_trade_events(supabase, hours, limit):
    try:
        return (
            supabase.table("trade_events")
            .select("ticker,action_type,event_type,status,price,quantity,note,occurred_at")
            .gte("occurred_at", since_iso(hours))
            .order("occurred_at", desc=True)
            .limit(limit)
            .execute()
            .data
            or []
        )
    except Exception as exc:
        return [{"_error": str(exc)}]


def print_runtime_section(processes, ibkr_ok, ibkr_message, settings):
    main_process = process_for_script(processes, "main.py")
    scanner_process = process_for_script(processes, "master_scanner.py")
    main_running = bool(main_process)
    scanner_running = bool(scanner_process)
    main_stale, main_started_at, changed_at = process_is_stale(
        main_process,
        ["main.py", "config.py", "execution_quality_gate.py"],
    )
    session = get_market_session()

    print("Runtime")
    print("=" * 92)
    print(f"Execution bridge main.py: {'RUNNING' if main_running else 'NOT RUNNING'}")
    if main_started_at:
        print(f"main.py started at:       {main_started_at.isoformat()}")
    if main_stale:
        print(
            "main.py reload needed:    YES "
            f"(code/config changed at {changed_at.isoformat() if changed_at else 'unknown'})"
        )
    print(f"Master scanner:           {'RUNNING' if scanner_running else 'NOT RUNNING'}")
    print(f"IBKR socket {IBKR_HOST}:{IBKR_PORT}: {'OK' if ibkr_ok else 'NOT REACHABLE'} ({ibkr_message})")
    print(f"Current market session:   {session.name}")
    print(f"DRY_RUN:                  {bool_text(DRY_RUN)}")
    print(f"Fixed order quantity:     {FIXED_ORDER_QUANTITY if FIXED_ORDER_QUANTITY > 0 else 'dynamic'}")
    print(f"Execution gate:           {bool_text(EXECUTION_GATE_ENABLED)} min_score={EXECUTION_GATE_MIN_SCORE}")
    print(f"Extended-hours routing:   {bool_text(ALLOW_EXTENDED_HOURS_TRADING)}")
    print(f"Global overnight routing: {bool_text(ALLOW_GLOBAL_OVERNIGHT_TRADING)}")

    if settings.get("_error"):
        print(f"Bot settings:             ERROR {settings['_error']}")
    else:
        auto_execute = settings.get("auto_execute", settings.get("autonomous_execution", False))
        print(
            "Bot settings:             "
            f"is_active={settings.get('is_active')} "
            f"auto_execute={auto_execute} "
            f"min_confidence={settings.get('min_confidence')}"
        )
    print()
    return main_running, scanner_running, main_stale


def print_counts(signals, events):
    status_counts = Counter(str(row.get("status") or "unknown").lower() for row in signals)
    action_status_counts = Counter(
        (str(row.get("action_type") or "?"), str(row.get("status") or "unknown").lower())
        for row in signals
    )
    event_counts = Counter(
        (str(row.get("event_type") or "?"), str(row.get("status") or "?"))
        for row in events
        if not row.get("_error")
    )

    print("Recent Counts")
    print("=" * 92)
    print(f"Signals by status:        {dict(status_counts)}")
    print(f"Signals by action/status: {dict(action_status_counts)}")
    if events and events[0].get("_error"):
        print(f"Trade events:             ERROR {events[0]['_error']}")
    else:
        print(f"Trade events:             {dict(event_counts)}")
    print()


def print_signal_rows(title, rows, limit=10, include_memo=False):
    print(title)
    print("=" * 92)
    if not rows:
        print("None.")
        print()
        return

    for row in rows[:limit]:
        print(
            f"{short_time(row.get('created_at'))} | "
            f"{str(row.get('ticker') or ''):<6} "
            f"{str(row.get('action_type') or ''):<4} "
            f"{str(row.get('status') or ''):<26} "
            f"{str(row.get('channel') or ''):<12} "
            f"conf={row.get('confidence_score')} "
            f"sig_px={row.get('price_at_signal')} "
            f"exec_px={row.get('execution_price')}"
        )
        if include_memo:
            print(f"  {trim(row.get('investment_memo'), 150)}")
    print()


def print_event_rows(title, rows, limit=10):
    print(title)
    print("=" * 92)
    rows = [row for row in rows if not row.get("_error")]
    if not rows:
        print("None.")
        print()
        return

    for row in rows[:limit]:
        print(
            f"{short_time(row.get('occurred_at'))} | "
            f"{str(row.get('ticker') or ''):<6} "
            f"{str(row.get('action_type') or ''):<4} "
            f"{str(row.get('event_type') or ''):<22} "
            f"status={row.get('status')} qty={row.get('quantity')} px={row.get('price')}"
        )
        if row.get("note"):
            print(f"  {trim(row.get('note'), 150)}")
    print()


def build_diagnosis(
    main_running,
    main_stale,
    ibkr_ok,
    settings,
    approved_rows,
    pending_rows,
    signals,
    events,
):
    reasons = []
    auto_execute = settings.get("auto_execute", settings.get("autonomous_execution", False))
    is_active = settings.get("is_active", True)

    if not main_running:
        reasons.append("Execution bridge is not running. Start it with: python main.py")
    elif main_stale:
        reasons.append("Execution bridge is running with older code/config. Restart main.py to load the current gate rules.")
    if not ibkr_ok:
        reasons.append("IBKR/TWS socket is not reachable, so live routing cannot happen.")
    if not is_active:
        reasons.append("bot_settings.is_active is false, so main.py will halt.")

    if approved_rows:
        reasons.append(f"{len(approved_rows)} approved signal(s) are waiting for main.py to process.")
    else:
        reasons.append("No approved signals are currently waiting to route.")

    pending_buy = [row for row in pending_rows if str(row.get("action_type") or "").upper() == "BUY"]
    pending_sell = [row for row in pending_rows if str(row.get("action_type") or "").upper() == "SELL"]
    if pending_buy and auto_execute:
        reasons.append(f"{len(pending_buy)} pending BUY signal(s) should auto-approve while main.py is running.")
    elif pending_buy:
        reasons.append(f"{len(pending_buy)} pending BUY signal(s) need approval because auto_execute is off.")
    if pending_sell:
        reasons.append(f"{len(pending_sell)} pending SELL signal(s) are awaiting manual review/position safety.")

    recent_blocked = [row for row in signals if str(row.get("status") or "").lower().startswith(BLOCKED_PREFIX)]
    if recent_blocked:
        blocked_counts = Counter(str(row.get("status") or "").lower() for row in recent_blocked)
        reasons.append(f"Recent blocked signals: {dict(blocked_counts)}")

    order_sent_events = [row for row in events if row.get("event_type") == "order_sent"]
    dry_run_events = [row for row in events if row.get("event_type") == "dry_run"]
    blocked_events = [row for row in events if row.get("event_type") == "routing_blocked"]
    if order_sent_events:
        last = order_sent_events[0]
        reasons.append(
            f"Last live order event: {last.get('ticker')} {last.get('action_type')} "
            f"at {short_time(last.get('occurred_at'))}."
        )
    elif dry_run_events:
        last = dry_run_events[0]
        reasons.append(
            f"Last dry-run route: {last.get('ticker')} {last.get('action_type')} "
            f"at {short_time(last.get('occurred_at'))}."
        )
    elif blocked_events:
        last = blocked_events[0]
        reasons.append(
            f"Latest routing attempt was blocked: {last.get('ticker')} {last.get('action_type')} "
            f"status={last.get('status')}."
        )
    else:
        reasons.append("No recent routing event was found in trade_events.")

    return reasons


def print_diagnosis(reasons):
    print("Why No Order Is Routing Right Now")
    print("=" * 92)
    for index, reason in enumerate(reasons, start=1):
        print(f"{index}. {reason}")
    print()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Read-only order routing status report.")
    parser.add_argument("--hours", type=int, default=24, help="Lookback window for recent signals/events.")
    parser.add_argument("--limit", type=int, default=50, help="Maximum recent signals/events to inspect.")
    parser.add_argument("--show-memos", action="store_true", help="Print short signal memos for recent rows.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    supabase = get_supabase_client()

    processes = find_runtime_processes()
    ibkr_ok, ibkr_message = check_socket(IBKR_HOST, IBKR_PORT)
    settings = fetch_bot_settings(supabase)
    signals = fetch_signals(supabase, args.hours, args.limit)
    approved_rows = fetch_status_rows(supabase, ROUTEABLE_STATUS, limit=args.limit)
    pending_rows = fetch_status_rows(supabase, PENDING_STATUS, limit=args.limit)
    events = fetch_trade_events(supabase, args.hours, args.limit)

    print("Alpha Engine Routing Status")
    print(f"Lookback: last {args.hours} hour(s)")
    print(f"Generated: {utc_now().isoformat()}")
    print(f"Market timezone: {MARKET_TIMEZONE}")
    print()

    main_running, _, main_stale = print_runtime_section(processes, ibkr_ok, ibkr_message, settings)
    print_counts(signals, events)
    print_signal_rows("Approved Queue", approved_rows, limit=12, include_memo=args.show_memos)
    print_signal_rows("Pending Manual Queue", pending_rows, limit=12, include_memo=args.show_memos)

    blocked_rows = [
        row for row in signals if str(row.get("status") or "").lower().startswith(BLOCKED_PREFIX)
    ]
    print_signal_rows("Recent Blocked Signals", blocked_rows, limit=12, include_memo=True)

    print_event_rows(
        "Recent Routing Events",
        [row for row in events if row.get("event_type") in {"order_sent", "dry_run", "routing_blocked", "routing_failed"}],
        limit=12,
    )

    diagnosis = build_diagnosis(
        main_running=main_running,
        main_stale=main_stale,
        ibkr_ok=ibkr_ok,
        settings=settings,
        approved_rows=approved_rows,
        pending_rows=pending_rows,
        signals=signals,
        events=[row for row in events if not row.get("_error")],
    )
    print_diagnosis(diagnosis)


if __name__ == "__main__":
    main()
