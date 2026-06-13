import argparse
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    STRATEGY_OPTIMIZER_AUTO_APPLY,
    STRATEGY_OPTIMIZER_HORIZON,
    STRATEGY_OPTIMIZER_LOOKBACK_DAYS,
    STRATEGY_OPTIMIZER_MAX_STEP,
    STRATEGY_OPTIMIZER_MIN_CHANNEL_SIGNALS,
    STRATEGY_OPTIMIZER_MIN_CONFIDENCE_CEILING,
    STRATEGY_OPTIMIZER_MIN_CONFIDENCE_FLOOR,
    STRATEGY_OPTIMIZER_MIN_EVALUATED_SIGNALS,
    STRATEGY_OPTIMIZER_OUTPUT_PATH,
    STRATEGY_OPTIMIZER_PUSH_SUPABASE,
    get_supabase_client,
)
from performance_governor import print_compute_notice

HORIZONS = ("15m", "1h", "1d", "5d")
MISSING_TABLE_HINT = (
    "Supabase is missing public.strategy_optimizer_runs; "
    "run supabase/strategy_optimizer_runs.sql if you want cloud optimizer history."
)


def utc_now():
    return datetime.now(timezone.utc)


def utc_now_iso():
    return utc_now().isoformat()


def parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "win"}
    return bool(value)


def missing_table(exc, table_name):
    message = str(exc)
    return (
        table_name in message
        and (
            "PGRST205" in message
            or "schema cache" in message
            or "Could not find the table" in message
        )
    )


def select_recent(supabase, table, order_column, lookback_days, limit=5000):
    threshold = utc_now() - timedelta(days=max(1, int(lookback_days)))
    query = supabase.table(table).select("*").order(order_column, desc=True).limit(limit)
    if order_column:
        query = query.gte(order_column, threshold.isoformat())
    return query.execute().data or []


def get_bot_settings(supabase):
    response = supabase.table("bot_settings").select("*").eq("id", 1).limit(1).execute()
    if response.data:
        return response.data[0]
    return {}


def confidence_to_pct(value):
    numeric = to_float(value)
    if numeric is None:
        return None
    if numeric <= 1:
        return numeric * 100
    return numeric


def confidence_from_pct(percent, reference_value):
    reference = to_float(reference_value)
    if reference is not None and reference <= 1:
        return round(percent / 100.0, 4)
    return round(percent, 2)


def configured_bound_to_pct(value):
    numeric = to_float(value)
    if numeric is None:
        return None
    if numeric <= 1:
        return numeric * 100
    return numeric


def choose_outcome(row, preferred_horizon):
    ordered = []
    for horizon in (preferred_horizon, "1h", "15m", "1d", "5d"):
        if horizon in HORIZONS and horizon not in ordered:
            ordered.append(horizon)

    for horizon in ordered:
        return_pct = to_float(row.get(f"return_{horizon}_pct"))
        if return_pct is None:
            continue
        correct = row.get(f"correct_{horizon}")
        return {
            "horizon": horizon,
            "return_pct": return_pct,
            "correct": to_bool(correct) if correct is not None else return_pct > 0,
        }
    return None


def evaluated_signal_rows(rows, preferred_horizon):
    evaluated = []
    for row in rows:
        outcome = choose_outcome(row, preferred_horizon)
        if not outcome:
            continue
        normalized = dict(row)
        normalized.update(outcome)
        normalized["confidence_pct"] = confidence_to_pct(row.get("confidence_score"))
        normalized["channel"] = str(row.get("channel") or "UNKNOWN").upper()
        normalized["action_type"] = str(row.get("action_type") or "UNKNOWN").upper()
        normalized["ticker"] = str(row.get("ticker") or "").upper()
        evaluated.append(normalized)
    return evaluated


def summarize_group(rows):
    returns = [float(row["return_pct"]) for row in rows]
    wins = [row for row in rows if row.get("correct")]
    confidences = [row["confidence_pct"] for row in rows if row.get("confidence_pct") is not None]
    if not returns:
        return {
            "count": 0,
            "win_rate_pct": 0.0,
            "avg_return_pct": 0.0,
            "median_return_pct": 0.0,
            "avg_confidence_pct": None,
        }
    return {
        "count": len(rows),
        "win_rate_pct": round((len(wins) / len(rows)) * 100.0, 2),
        "avg_return_pct": round(sum(returns) / len(returns), 4),
        "median_return_pct": round(statistics.median(returns), 4),
        "avg_confidence_pct": round(sum(confidences) / len(confidences), 2) if confidences else None,
    }


def group_stats(rows, key_fn, min_count=1):
    groups = defaultdict(list)
    for row in rows:
        key = key_fn(row)
        if not key:
            continue
        groups[str(key)].append(row)

    stats = {}
    for key, group in groups.items():
        if len(group) < min_count:
            continue
        stats[key] = summarize_group(group)
    return dict(sorted(stats.items(), key=lambda item: item[1]["count"], reverse=True))


def load_category_map(supabase, tickers):
    ticker_list = sorted({ticker for ticker in tickers if ticker})
    if not ticker_list:
        return {}

    category_map = defaultdict(set)
    for start in range(0, len(ticker_list), 100):
        batch = ticker_list[start:start + 100]
        try:
            response = (
                supabase.table("category_universe")
                .select("ticker,category")
                .in_("ticker", batch)
                .execute()
            )
        except Exception:
            return {}
        for row in response.data or []:
            ticker = str(row.get("ticker") or "").upper()
            category = str(row.get("category") or "").strip()
            if ticker and category:
                category_map[ticker].add(category)
    return {ticker: sorted(categories) for ticker, categories in category_map.items()}


def category_stats(rows, category_map, min_count):
    expanded = []
    for row in rows:
        categories = category_map.get(row.get("ticker")) or []
        for category in categories:
            item = dict(row)
            item["category"] = category
            expanded.append(item)
    return group_stats(expanded, lambda row: row.get("category"), min_count=min_count)


def trade_event_stats(events):
    event_counter = Counter(str(row.get("event_type") or "unknown") for row in events)
    status_counter = Counter(str(row.get("status") or "unknown") for row in events)
    tickers = Counter(str(row.get("ticker") or "").upper() for row in events if row.get("ticker"))
    return {
        "count": len(events),
        "by_event_type": dict(event_counter.most_common()),
        "by_status": dict(status_counter.most_common()),
        "top_tickers": dict(tickers.most_common(10)),
    }


def broker_position_stats(positions):
    open_positions = [row for row in positions if row.get("is_open")]
    total_market_value = sum(to_float(row.get("market_value")) or 0.0 for row in open_positions)
    total_unrealized_pnl = sum(to_float(row.get("unrealized_pnl")) or 0.0 for row in open_positions)
    long_count = sum(1 for row in open_positions if (to_float(row.get("quantity")) or 0.0) > 0)
    short_count = sum(1 for row in open_positions if (to_float(row.get("quantity")) or 0.0) < 0)
    ranked = sorted(
        open_positions,
        key=lambda row: abs(to_float(row.get("unrealized_pnl")) or 0.0),
        reverse=True,
    )
    top_positions = []
    for row in ranked[:10]:
        top_positions.append(
            {
                "ticker": row.get("ticker"),
                "side": row.get("side"),
                "quantity": to_float(row.get("quantity")),
                "market_value": to_float(row.get("market_value")),
                "unrealized_pnl": to_float(row.get("unrealized_pnl")),
            }
        )
    return {
        "open_count": len(open_positions),
        "long_count": long_count,
        "short_count": short_count,
        "total_market_value": round(total_market_value, 2),
        "total_unrealized_pnl": round(total_unrealized_pnl, 2),
        "top_positions_by_abs_pnl": top_positions,
    }


def normalize_json_list(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except Exception:
            pass
        return [value.strip()] if value.strip() else []
    return []


def post_trade_lesson_stats(reviews):
    outcomes = Counter(str(row.get("overall_outcome") or "unknown") for row in reviews)
    worked = Counter()
    failed = Counter()
    adjustments = Counter()
    for row in reviews:
        worked.update(normalize_json_list(row.get("what_worked")))
        failed.update(normalize_json_list(row.get("what_failed")))
        adjustments.update(normalize_json_list(row.get("training_adjustments")))
    return {
        "count": len(reviews),
        "outcomes": dict(outcomes.most_common()),
        "top_worked": dict(worked.most_common(8)),
        "top_failed": dict(failed.most_common(8)),
        "top_adjustments": dict(adjustments.most_common(8)),
    }


def recommend_min_confidence(current_value, overall_stats, evaluated_count):
    current_pct = confidence_to_pct(current_value)
    if current_pct is None:
        return None

    floor_pct = configured_bound_to_pct(STRATEGY_OPTIMIZER_MIN_CONFIDENCE_FLOOR) or 55.0
    ceiling_pct = configured_bound_to_pct(STRATEGY_OPTIMIZER_MIN_CONFIDENCE_CEILING) or 95.0
    max_step_pct = configured_bound_to_pct(STRATEGY_OPTIMIZER_MAX_STEP) or 3.0
    max_step_pct = max(0.5, min(5.0, max_step_pct))

    if evaluated_count < int(STRATEGY_OPTIMIZER_MIN_EVALUATED_SIGNALS):
        return {
            "setting": "min_confidence",
            "current": current_value,
            "recommended": current_value,
            "action": "hold",
            "reason": f"Only {evaluated_count} evaluated signal(s); need at least {STRATEGY_OPTIMIZER_MIN_EVALUATED_SIGNALS}.",
        }

    win_rate = float(overall_stats.get("win_rate_pct") or 0.0)
    avg_return = float(overall_stats.get("avg_return_pct") or 0.0)
    target_pct = current_pct
    action = "hold"

    if win_rate < 42.0 or avg_return < -0.25:
        target_pct = min(ceiling_pct, current_pct + max_step_pct)
        action = "tighten"
        reason = f"Recent evaluated signals are weak: win_rate={win_rate:.1f}%, avg_return={avg_return:.2f}%."
    elif win_rate > 58.0 and avg_return > 0.15:
        target_pct = max(floor_pct, current_pct - max_step_pct)
        action = "loosen"
        reason = f"Recent evaluated signals are strong: win_rate={win_rate:.1f}%, avg_return={avg_return:.2f}%."
    else:
        reason = f"Recent signals are mixed: win_rate={win_rate:.1f}%, avg_return={avg_return:.2f}%."

    recommended = confidence_from_pct(target_pct, current_value)
    return {
        "setting": "min_confidence",
        "current": current_value,
        "recommended": recommended,
        "action": action if recommended != current_value else "hold",
        "reason": reason,
    }


def channel_recommendations(channel_stats):
    recommendations = []
    min_count = int(STRATEGY_OPTIMIZER_MIN_CHANNEL_SIGNALS)
    for channel, stats in channel_stats.items():
        count = int(stats.get("count") or 0)
        if count < min_count:
            continue
        win_rate = float(stats.get("win_rate_pct") or 0.0)
        avg_return = float(stats.get("avg_return_pct") or 0.0)
        if win_rate < 40.0 or avg_return < -0.25:
            recommendations.append(
                {
                    "type": "channel",
                    "target": channel,
                    "action": "tighten_or_review",
                    "reason": f"{channel} underperformed: n={count}, win_rate={win_rate:.1f}%, avg_return={avg_return:.2f}%.",
                }
            )
        elif win_rate > 60.0 and avg_return > 0.15:
            recommendations.append(
                {
                    "type": "channel",
                    "target": channel,
                    "action": "favor",
                    "reason": f"{channel} is working: n={count}, win_rate={win_rate:.1f}%, avg_return={avg_return:.2f}%.",
                }
            )
    return recommendations


def build_optimizer_record(supabase, lookback_days, preferred_horizon, apply_changes=False):
    settings = get_bot_settings(supabase)
    journal_rows = select_recent(supabase, "signal_journal", "created_at_utc", lookback_days)
    events = select_recent(supabase, "trade_events", "occurred_at", lookback_days)
    try:
        positions = supabase.table("broker_positions").select("*").eq("is_open", True).limit(500).execute().data or []
    except Exception:
        positions = []
    try:
        reviews = select_recent(supabase, "post_trade_reviews", "reviewed_at", lookback_days)
    except Exception:
        reviews = []

    evaluated = evaluated_signal_rows(journal_rows, preferred_horizon)
    overall_stats = summarize_group(evaluated)
    channel_summary = group_stats(
        evaluated,
        lambda row: f"{row.get('channel')}:{row.get('action_type')}",
        min_count=1,
    )
    category_map = load_category_map(supabase, [row.get("ticker") for row in evaluated])
    category_summary = category_stats(evaluated, category_map, min_count=1)

    min_confidence_rec = recommend_min_confidence(
        settings.get("min_confidence"),
        overall_stats,
        len(evaluated),
    )
    recommendations = []
    if min_confidence_rec:
        recommendations.append(min_confidence_rec)
    recommendations.extend(channel_recommendations(channel_summary))

    recommended_settings = {}
    if min_confidence_rec and min_confidence_rec.get("action") in {"tighten", "loosen"}:
        recommended_settings["min_confidence"] = min_confidence_rec["recommended"]

    record = {
        "run_at": utc_now_iso(),
        "lookback_days": int(lookback_days),
        "preferred_horizon": preferred_horizon,
        "sample_count": len(journal_rows),
        "evaluated_count": len(evaluated),
        "current_settings": {
            "min_confidence": settings.get("min_confidence"),
            "is_active": settings.get("is_active"),
            "auto_execute": settings.get("auto_execute"),
        },
        "recommended_settings": recommended_settings,
        "overall_stats": overall_stats,
        "channel_stats": channel_summary,
        "category_stats": category_summary,
        "trade_event_stats": trade_event_stats(events),
        "broker_position_stats": broker_position_stats(positions),
        "post_trade_lessons": post_trade_lesson_stats(reviews),
        "recommendations": recommendations,
        "applied": False,
        "apply_result": {},
        "source": "strategy_optimizer",
    }

    if apply_changes:
        record["apply_result"] = apply_recommendations(supabase, settings, recommended_settings)
        record["applied"] = bool(record["apply_result"].get("applied"))

    return record


def apply_recommendations(supabase, current_settings, recommended_settings):
    if not recommended_settings:
        return {"applied": False, "reason": "No safe setting changes recommended."}

    updates = {}
    if "min_confidence" in recommended_settings:
        current = current_settings.get("min_confidence")
        recommended = recommended_settings["min_confidence"]
        if recommended != current:
            updates["min_confidence"] = recommended

    if not updates:
        return {"applied": False, "reason": "Recommended values already match current settings."}

    supabase.table("bot_settings").update(updates).eq("id", 1).execute()
    return {"applied": True, "updated": updates}


def append_local_record(record):
    path = Path(STRATEGY_OPTIMIZER_OUTPUT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output_file:
        output_file.write(json.dumps(record, ensure_ascii=True) + "\n")


def push_optimizer_record(supabase, record):
    payload = dict(record)
    payload.pop("overall_stats", None)
    try:
        supabase.table("strategy_optimizer_runs").insert(payload).execute()
        return True
    except Exception as exc:
        if missing_table(exc, "strategy_optimizer_runs"):
            print(MISSING_TABLE_HINT)
            return False
        print(f"Strategy optimizer Supabase sync failed: {exc}")
        return False


def print_report(record):
    print("Strategy Optimizer Report")
    print("=" * 80)
    print(
        f"Lookback: {record['lookback_days']}d | "
        f"Horizon: {record['preferred_horizon']} | "
        f"Evaluated: {record['evaluated_count']}/{record['sample_count']}"
    )
    overall = record.get("overall_stats") or {}
    print(
        "Overall: "
        f"win_rate={overall.get('win_rate_pct', 0):.1f}% "
        f"avg_return={overall.get('avg_return_pct', 0):.2f}% "
        f"median={overall.get('median_return_pct', 0):.2f}%"
    )
    positions = record.get("broker_position_stats") or {}
    if positions:
        print(
            "Open positions: "
            f"{positions.get('open_count', 0)} | "
            f"unrealized_pnl=${positions.get('total_unrealized_pnl', 0):.2f}"
        )
    current = record["current_settings"].get("min_confidence")
    recommended = record["recommended_settings"].get("min_confidence", current)
    print(f"min_confidence: current={current} recommended={recommended}")

    print("\nTop channel/action stats")
    for channel, stats in list(record.get("channel_stats", {}).items())[:10]:
        print(
            f"  {channel:<24} n={stats['count']:<3} "
            f"win={stats['win_rate_pct']:>5.1f}% avg={stats['avg_return_pct']:>6.2f}%"
        )

    if record.get("recommendations"):
        print("\nRecommendations")
        for rec in record["recommendations"]:
            print(f"  - {rec.get('action')}: {rec.get('target') or rec.get('setting')} | {rec.get('reason')}")
    else:
        print("\nRecommendations: none")

    apply_result = record.get("apply_result") or {}
    if apply_result:
        print(f"\nApply result: {apply_result}")


def run_strategy_optimizer(
    lookback_days=STRATEGY_OPTIMIZER_LOOKBACK_DAYS,
    preferred_horizon=STRATEGY_OPTIMIZER_HORIZON,
    apply_changes=STRATEGY_OPTIMIZER_AUTO_APPLY,
    push_supabase=STRATEGY_OPTIMIZER_PUSH_SUPABASE,
    dry_run=False,
):
    print_compute_notice(
        "strategy_optimizer",
        "strategy optimizer feedback review",
        prefix="[OPTIMIZER]",
    )
    supabase = get_supabase_client()
    record = build_optimizer_record(
        supabase,
        lookback_days=lookback_days,
        preferred_horizon=preferred_horizon,
        apply_changes=bool(apply_changes and not dry_run),
    )
    append_local_record(record)
    if push_supabase and not dry_run:
        push_optimizer_record(supabase, record)
    print_report(record)
    return True


def parse_args():
    parser = argparse.ArgumentParser(description="Analyze trade feedback and recommend cautious bot setting adjustments.")
    parser.add_argument("--lookback-days", type=int, default=STRATEGY_OPTIMIZER_LOOKBACK_DAYS)
    parser.add_argument("--horizon", default=STRATEGY_OPTIMIZER_HORIZON, choices=HORIZONS)
    parser.add_argument("--apply", action="store_true", help="Apply safe recommendations to bot_settings.")
    parser.add_argument("--dry-run", action="store_true", help="Do not apply settings or push optimizer run to Supabase.")
    parser.add_argument("--no-push-supabase", action="store_true", help="Only write local optimizer JSONL history.")
    return parser.parse_args()


def main():
    args = parse_args()
    run_strategy_optimizer(
        lookback_days=args.lookback_days,
        preferred_horizon=args.horizon,
        apply_changes=args.apply,
        push_supabase=not args.no_push_supabase,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
