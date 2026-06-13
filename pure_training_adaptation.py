import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from config import (
    EXPERIMENTAL_LOCAL_LOG_PATH,
    PURE_TRAINING_ADAPTATION_PATH,
    get_supabase_client,
)
from local_data_recorder import append_local_event

SOURCE = "pure_training_adaptation"


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean_ticker(value):
    return str(value or "").strip().upper()


def finite_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_local_events(path=EXPERIMENTAL_LOCAL_LOG_PATH, limit=2000):
    target = Path(path)
    if not target.exists():
        return []

    lines = target.read_text(encoding="utf-8").splitlines()
    events = []
    for line in lines[-limit:]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def latest_training_tickers(events):
    for event in reversed(events):
        if event.get("event_type") not in {"experimental_run_finished", "pure_training_run_finished"}:
            continue
        payload = event.get("payload") or {}
        outcomes = payload.get("outcomes") or []
        tickers = []
        for outcome in outcomes:
            if outcome.get("status") != "sent":
                continue
            ticker = clean_ticker(outcome.get("ticker"))
            if ticker:
                tickers.append(ticker)
        if tickers:
            return sorted(set(tickers)), payload.get("run_id")
    return [], None


def fetch_open_positions(supabase, tickers):
    if not tickers:
        return []
    try:
        response = (
            supabase.table("broker_positions")
            .select("ticker,quantity,avg_cost,market_price,market_value,unrealized_pnl,is_open,synced_at")
            .in_("ticker", tickers)
            .eq("is_open", True)
            .execute()
        )
        return response.data or []
    except Exception as exc:
        print(f"[PURE TRAINING ADAPT] Broker position lookup skipped: {exc}")
        return []


def fetch_category_map(supabase, tickers):
    category_map = {}
    if not tickers:
        return category_map
    try:
        response = (
            supabase.table("category_universe")
            .select("ticker,category")
            .in_("ticker", tickers)
            .eq("active", True)
            .execute()
        )
        for row in response.data or []:
            ticker = clean_ticker(row.get("ticker"))
            category = row.get("category")
            if ticker and category and ticker not in category_map:
                category_map[ticker] = category
    except Exception as exc:
        print(f"[PURE TRAINING ADAPT] Category lookup skipped: {exc}")
    return category_map


def pnl_percent(row):
    avg_cost = finite_float(row.get("avg_cost"))
    market_price = finite_float(row.get("market_price"))
    quantity = finite_float(row.get("quantity"))
    if not avg_cost or not market_price or not quantity:
        return None
    direction = 1 if quantity > 0 else -1
    return ((market_price - avg_cost) / avg_cost) * 100.0 * direction


def bounded_delta(pct):
    if pct is None:
        return 0.0
    if pct >= 3.0:
        return 5.0
    if pct >= 1.5:
        return 3.0
    if pct >= 0.5:
        return 1.5
    if pct <= -2.0:
        return -5.0
    if pct <= -1.0:
        return -3.0
    if pct <= -0.5:
        return -1.5
    return 0.0


def build_adaptation(positions, category_map, source_run_id=None):
    ticker_adjustments = {}
    cooldowns = {}
    category_samples = defaultdict(list)
    samples = []

    for row in positions:
        ticker = clean_ticker(row.get("ticker"))
        if not ticker:
            continue
        pct = pnl_percent(row)
        delta = bounded_delta(pct)
        if delta:
            ticker_adjustments[ticker] = delta
        if pct is not None and pct <= -2.0:
            cooldowns[ticker] = {
                "reason": "pure_training_position_down_2pct_or_more",
                "pnl_percent": round(pct, 3),
                "synced_at": row.get("synced_at"),
            }

        category = category_map.get(ticker)
        if category and pct is not None:
            category_samples[category].append(pct)

        samples.append(
            {
                "ticker": ticker,
                "category": category,
                "quantity": row.get("quantity"),
                "avg_cost": row.get("avg_cost"),
                "market_price": row.get("market_price"),
                "unrealized_pnl": row.get("unrealized_pnl"),
                "pnl_percent": None if pct is None else round(pct, 3),
                "ticker_adjustment": delta,
            }
        )

    category_adjustments = {}
    for category, pcts in category_samples.items():
        if not pcts:
            continue
        average = sum(pcts) / len(pcts)
        delta = bounded_delta(average)
        if delta:
            category_adjustments[category] = delta

    return {
        "generated_at": utc_now_iso(),
        "source": SOURCE,
        "source_run_id": source_run_id,
        "ticker_adjustments": ticker_adjustments,
        "category_adjustments": category_adjustments,
        "cooldowns": cooldowns,
        "samples": samples,
    }


def write_adaptation(adaptation, dry_run=False):
    target = Path(PURE_TRAINING_ADAPTATION_PATH)
    if dry_run:
        print(json.dumps(adaptation, indent=2, sort_keys=True))
        return True

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(adaptation, indent=2, sort_keys=True), encoding="utf-8")
    append_local_event(
        "experimental_adaptation_written",
        adaptation,
        source=SOURCE,
        path=EXPERIMENTAL_LOCAL_LOG_PATH,
    )
    print(f"[EXPERIMENTAL ADAPT] Wrote adaptation file: {target}")
    return True


def run_pure_training_adaptation(dry_run=False):
    events = read_local_events()
    tickers, run_id = latest_training_tickers(events)
    print(f"[EXPERIMENTAL ADAPT] Latest sent basket: {len(tickers)} ticker(s).")
    if not tickers:
        adaptation = build_adaptation([], {}, source_run_id=run_id)
        return write_adaptation(adaptation, dry_run=dry_run)

    supabase = get_supabase_client()
    positions = fetch_open_positions(supabase, tickers)
    category_map = fetch_category_map(supabase, tickers)
    adaptation = build_adaptation(positions, category_map, source_run_id=run_id)
    print(
        "[EXPERIMENTAL ADAPT] Adjustments: "
        f"{len(adaptation['ticker_adjustments'])} ticker, "
        f"{len(adaptation['category_adjustments'])} category, "
        f"{len(adaptation['cooldowns'])} cooldown."
    )
    return write_adaptation(adaptation, dry_run=dry_run)


def parse_args():
    parser = argparse.ArgumentParser(description="Build pure-training shortlist adjustments from paper performance.")
    parser.add_argument("--dry-run", action="store_true", help="Print adaptation JSON without writing it.")
    return parser.parse_args()


def main():
    args = parse_args()
    success = run_pure_training_adaptation(dry_run=args.dry_run)
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
