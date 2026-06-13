import json
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from config import (
    CYCLE_LOG_ENABLED,
    CYCLE_LOG_EVENT_LIMIT,
    CYCLE_LOG_PATH,
    CYCLE_LOG_PUSH_SUPABASE,
    CYCLE_LOG_SIGNAL_LIMIT,
    get_supabase_client,
)

MISSING_TABLE_WARNED = False


def utc_now():
    return datetime.now(timezone.utc)


def utc_now_iso():
    return utc_now().isoformat()


def new_cycle_id(prefix="cycle"):
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def seconds_between(started_at, finished_at):
    start = parse_datetime(started_at)
    finish = parse_datetime(finished_at)
    if not start or not finish:
        return None
    return round(max(0.0, (finish - start).total_seconds()), 3)


def append_local_record(record):
    if not CYCLE_LOG_ENABLED:
        return False
    try:
        path = Path(CYCLE_LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")
        return True
    except Exception as exc:
        print(f"[CYCLE LOG] Local write skipped: {exc}")
        return False


def log_cycle_event(
    cycle_id,
    event_type,
    status,
    component,
    target=None,
    market_session=None,
    mode=None,
    started_at=None,
    finished_at=None,
    detail=None,
    error=None,
    metadata=None,
):
    if not CYCLE_LOG_ENABLED:
        return None

    now = utc_now_iso()
    record = {
        "record_type": "cycle_event",
        "cycle_id": cycle_id,
        "event_type": event_type,
        "status": status,
        "component": component,
        "target": target,
        "market_session": market_session,
        "mode": mode,
        "started_at": started_at or now,
        "finished_at": finished_at,
        "duration_seconds": seconds_between(started_at, finished_at),
        "detail": detail,
        "error": error,
        "metadata": metadata or {},
        "logged_at": now,
    }
    append_local_record(record)
    return record


def is_missing_scanner_cycles_table(exc):
    message = str(exc)
    return (
        "scanner_cycles" in message
        and (
            "PGRST205" in message
            or "schema cache" in message
            or "Could not find the table" in message
        )
    )


def safe_supabase():
    try:
        return get_supabase_client()
    except Exception as exc:
        print(f"[CYCLE LOG] Supabase client unavailable: {exc}")
        return None


def fetch_rows_since(supabase, table, timestamp_column, started_at, columns, limit):
    if not supabase or not started_at:
        return []
    try:
        response = (
            supabase.table(table)
            .select(columns)
            .gte(timestamp_column, started_at)
            .order(timestamp_column, desc=True)
            .limit(max(1, int(limit)))
            .execute()
        )
        return response.data or []
    except Exception as exc:
        print(f"[CYCLE LOG] {table} delta read skipped: {exc}")
        return []


def summarize_signals(rows):
    statuses = Counter(str(row.get("status") or "unknown") for row in rows)
    actions = Counter(str(row.get("action_type") or "unknown") for row in rows)
    channels = Counter(str(row.get("channel") or row.get("source") or "unknown") for row in rows)
    tickers = [row.get("ticker") for row in rows if row.get("ticker")]
    return {
        "count": len(rows),
        "by_status": dict(statuses),
        "by_action": dict(actions),
        "by_channel": dict(channels),
        "tickers": tickers[:25],
        "recent": rows[:10],
    }


def summarize_trade_events(rows):
    event_types = Counter(str(row.get("event_type") or "unknown") for row in rows)
    statuses = Counter(str(row.get("status") or "unknown") for row in rows)
    tickers = [row.get("ticker") for row in rows if row.get("ticker")]
    return {
        "count": len(rows),
        "by_event_type": dict(event_types),
        "by_status": dict(statuses),
        "tickers": tickers[:25],
        "recent": rows[:10],
    }


def build_cycle_deltas(started_at):
    supabase = safe_supabase()
    signal_rows = fetch_rows_since(
        supabase,
        "market_signals",
        "created_at",
        started_at,
        "id,ticker,action_type,status,confidence_score,channel,created_at",
        CYCLE_LOG_SIGNAL_LIMIT,
    )
    event_rows = fetch_rows_since(
        supabase,
        "trade_events",
        "occurred_at",
        started_at,
        "id,signal_id,ticker,action_type,event_type,status,quantity,price,source,note,occurred_at",
        CYCLE_LOG_EVENT_LIMIT,
    )
    return {
        "signals": summarize_signals(signal_rows),
        "trade_events": summarize_trade_events(event_rows),
    }


def push_cycle_summary(record):
    global MISSING_TABLE_WARNED

    if not CYCLE_LOG_PUSH_SUPABASE:
        return False

    supabase = safe_supabase()
    if not supabase:
        return False

    payload = {
        "cycle_id": record.get("cycle_id"),
        "cycle_type": record.get("cycle_type"),
        "status": record.get("status"),
        "market_session": record.get("market_session"),
        "mode": record.get("mode"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "duration_seconds": record.get("duration_seconds"),
        "workflows": record.get("workflows") or [],
        "scanners": record.get("scanners") or [],
        "operations": record.get("operations") or [],
        "phases": record.get("phases") or [],
        "signal_summary": (record.get("deltas") or {}).get("signals") or {},
        "trade_event_summary": (record.get("deltas") or {}).get("trade_events") or {},
        "errors": record.get("errors") or [],
        "metadata": record.get("metadata") or {},
    }

    try:
        supabase.table("scanner_cycles").upsert(payload, on_conflict="cycle_id").execute()
        return True
    except Exception as exc:
        if is_missing_scanner_cycles_table(exc):
            if not MISSING_TABLE_WARNED:
                print("[CYCLE LOG] scanner_cycles table missing; run supabase/scanner_cycles.sql for iPad cycle history.")
                MISSING_TABLE_WARNED = True
            return False
        print(f"[CYCLE LOG] Supabase summary write skipped: {exc}")
        return False


def log_cycle_summary(
    cycle_id,
    cycle_type,
    status,
    started_at,
    finished_at=None,
    market_session=None,
    mode=None,
    workflows=None,
    scanners=None,
    operations=None,
    phases=None,
    errors=None,
    metadata=None,
    include_deltas=True,
):
    if not CYCLE_LOG_ENABLED:
        return None

    finished = finished_at or utc_now_iso()
    deltas = build_cycle_deltas(started_at) if include_deltas else {}
    record = {
        "record_type": "cycle_summary",
        "cycle_id": cycle_id,
        "cycle_type": cycle_type,
        "status": status,
        "market_session": market_session,
        "mode": mode,
        "started_at": started_at,
        "finished_at": finished,
        "duration_seconds": seconds_between(started_at, finished),
        "workflows": workflows or [],
        "scanners": scanners or [],
        "operations": operations or [],
        "phases": phases or [],
        "errors": errors or [],
        "deltas": deltas,
        "metadata": metadata or {},
        "logged_at": utc_now_iso(),
    }
    append_local_record(record)
    push_cycle_summary(record)
    return record
