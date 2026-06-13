import json
import os
from collections import defaultdict
from pathlib import Path

from config import (
    CATEGORY_MIN_SCORE,
    CATEGORY_TARGET_LIMIT,
    CATEGORY_TARGETS_PER_CATEGORY,
    PURE_TRAINING_ADAPTATION_PATH,
    get_supabase_client,
)

supabase = get_supabase_client()

DEFAULT_CATEGORIES = [
    "Nuclear Energy",
    "Data Center Power & Grid Infrastructure",
    "AI Chips",
    "Cybersecurity & AI Security",
    "Aerospace Defense & Security",
    "Energy",
    "Logistics",
    "Infrastructure",
    "Materials",
]
SETUP_MESSAGE = (
    "Missing Supabase table: public.category_universe. "
    "Run supabase/category_universe.sql in the Supabase SQL editor, then run "
    "`python master_scanner.py category-universe` before selecting categories."
)


class CategoryUniverseMissing(RuntimeError):
    pass


def is_missing_category_universe(exc):
    message = str(exc)
    return (
        "category_universe" in message
        and (
            "PGRST205" in message
            or "schema cache" in message
            or "Could not find the table" in message
        )
    )


def fetch_category_rows(category, per_category, min_score):
    try:
        response = (
            supabase.table("category_universe")
            .select("ticker, company_name, category, theme, category_score, market_cap, average_volume")
            .eq("active", True)
            .eq("category", category)
            .gte("category_score", min_score)
            .order("category_score", desc=True)
            .order("market_cap", desc=True)
            .limit(per_category * 2)
            .execute()
        )
    except Exception as exc:
        if is_missing_category_universe(exc):
            raise CategoryUniverseMissing(SETUP_MESSAGE) from exc
        raise
    return response.data or []


def load_pure_training_adjustments():
    path = Path(PURE_TRAINING_ADAPTATION_PATH)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"Pure training adaptation ignored; could not read {path}: {exc}")
        return {}

    if not isinstance(data, dict):
        return {}
    return data


def adjustment_for_row(row, adjustments):
    ticker = str(row.get("ticker") or "").upper()
    category = row.get("category") or ""
    ticker_adjustments = adjustments.get("ticker_adjustments") or {}
    category_adjustments = adjustments.get("category_adjustments") or {}
    cooldowns = adjustments.get("cooldowns") or {}

    if ticker in cooldowns:
        return -100.0, "cooldown"

    adjustment = 0.0
    reasons = []
    ticker_delta = ticker_adjustments.get(ticker)
    if isinstance(ticker_delta, (int, float)):
        adjustment += float(ticker_delta)
        reasons.append(f"ticker {ticker_delta:+.1f}")
    category_delta = category_adjustments.get(category)
    if isinstance(category_delta, (int, float)):
        adjustment += float(category_delta)
        reasons.append(f"category {category_delta:+.1f}")
    return adjustment, ", ".join(reasons)


def adjusted_score(row, adjustments):
    try:
        base_score = float(row.get("category_score") or 0.0)
    except (TypeError, ValueError):
        base_score = 0.0
    adjustment, reason = adjustment_for_row(row, adjustments)
    score = max(0.0, min(100.0, base_score + adjustment))
    row["_adjusted_category_score"] = score
    row["_adjustment_reason"] = reason
    return score


def safe_number(value):
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def fetch_category_targets(
    limit=CATEGORY_TARGET_LIMIT,
    per_category=CATEGORY_TARGETS_PER_CATEGORY,
    min_score=CATEGORY_MIN_SCORE,
):
    print(
        "Loading dynamic category targets "
        f"(limit={limit}, per_category={per_category}, min_score={min_score})..."
    )
    selected = []
    seen = set()
    adjustments = load_pure_training_adjustments()
    if adjustments:
        generated_at = adjustments.get("generated_at") or "unknown"
        print(f"Pure training adaptation loaded from {PURE_TRAINING_ADAPTATION_PATH} ({generated_at}).")

    for category in DEFAULT_CATEGORIES:
        try:
            rows = fetch_category_rows(category, per_category, min_score)
        except CategoryUniverseMissing as exc:
            print(exc)
            return None
        except Exception as exc:
            print(f"Failed to load {category} targets: {exc}")
            continue

        print(f"Category pool: {category} -> {len(rows)} candidate row(s)")
        if adjustments:
            rows = sorted(
                rows,
                key=lambda row: (
                    adjusted_score(row, adjustments),
                    safe_number(row.get("market_cap")),
                ),
                reverse=True,
            )
        count = 0
        for row in rows:
            ticker = row.get("ticker")
            if not ticker or ticker in seen:
                continue
            if row.get("_adjustment_reason") == "cooldown":
                print(f"   {ticker:<6} {category:<15} skipped by pure-training cooldown")
                continue
            selected.append(row)
            seen.add(ticker)
            count += 1
            score = row.get("_adjusted_category_score", row.get("category_score"))
            reason = row.get("_adjustment_reason")
            score_text = f"score={score}"
            if reason:
                score_text += f" ({reason})"
            print(
                f"   {ticker:<6} {category:<15} {row.get('theme', ''):<28} "
                f"{score_text} {row.get('company_name', '')}"
            )
            if count >= per_category or len(selected) >= limit:
                break
        if len(selected) >= limit:
            break

    if not selected:
        print("No category universe rows matched the target filters.")
        return []

    category_counts = defaultdict(int)
    for row in selected:
        category_counts[row.get("category") or "Uncategorized"] += 1

    count_summary = ", ".join(
        f"{category}: {category_counts.get(category, 0)}"
        for category in DEFAULT_CATEGORIES
    )
    print(f"Category coverage: {count_summary}")
    print(f"Loaded {len(selected)} dynamic category targets.")
    return selected


def update_cloud_watchlist(tickers):
    try:
        supabase.table("bot_settings").update({"watchlist": tickers}).eq("id", 1).execute()
        if tickers:
            print("iPad Command Center Watchlist updated with dynamic category targets.")
        else:
            print("iPad Command Center Watchlist cleared; no category targets are available.")
    except Exception as exc:
        print(f"Supabase watchlist update error: {exc}")


def save_local_targets(tickers):
    file_path = os.path.join(os.path.dirname(__file__), "daily_targets.txt")
    try:
        with open(file_path, "w", encoding="utf-8") as target_file:
            target_file.write(",".join(tickers))
        if tickers:
            print(f"Saved {len(tickers)} category targets to {file_path}.")
        else:
            print(f"Cleared local category targets at {file_path}.")
    except Exception as exc:
        print(f"Failed to save category targets locally: {exc}")


def run_category_target_scan():
    rows = fetch_category_targets()
    if rows is None:
        update_cloud_watchlist([])
        save_local_targets([])
        print("Dynamic category target sequence failed; category universe setup is incomplete.")
        return False

    tickers = [row["ticker"] for row in rows if row.get("ticker")]
    update_cloud_watchlist(tickers)
    save_local_targets(tickers)
    print("Dynamic category target sequence complete.")
    return tickers if tickers else False


if __name__ == "__main__":
    run_category_target_scan()
