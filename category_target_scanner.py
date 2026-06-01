import os
from collections import defaultdict

from config import (
    CATEGORY_MIN_SCORE,
    CATEGORY_TARGET_LIMIT,
    CATEGORY_TARGETS_PER_CATEGORY,
    get_supabase_client,
)

supabase = get_supabase_client()


def fetch_category_targets(
    limit=CATEGORY_TARGET_LIMIT,
    per_category=CATEGORY_TARGETS_PER_CATEGORY,
    min_score=CATEGORY_MIN_SCORE,
):
    print(
        "Loading dynamic category targets "
        f"(limit={limit}, per_category={per_category}, min_score={min_score})..."
    )
    try:
        response = (
            supabase.table("category_universe")
            .select("ticker, company_name, category, theme, category_score, market_cap, average_volume")
            .eq("active", True)
            .gte("category_score", min_score)
            .order("category_score", desc=True)
            .order("market_cap", desc=True)
            .limit(max(limit * 4, 100))
            .execute()
        )
    except Exception as exc:
        print(f"Failed to load category targets: {exc}")
        return []

    rows = response.data or []
    if not rows:
        print("No category universe rows matched the target filters.")
        return []

    by_category = defaultdict(list)
    for row in rows:
        by_category[row.get("category") or "Uncategorized"].append(row)

    selected = []
    seen = set()
    for category in sorted(by_category):
        count = 0
        for row in by_category[category]:
            ticker = row.get("ticker")
            if not ticker or ticker in seen:
                continue
            selected.append(row)
            seen.add(ticker)
            count += 1
            print(
                f"   {ticker:<6} {category:<15} {row.get('theme', ''):<28} "
                f"score={row.get('category_score')} {row.get('company_name', '')}"
            )
            if count >= per_category or len(selected) >= limit:
                break
        if len(selected) >= limit:
            break

    print(f"Loaded {len(selected)} dynamic category targets.")
    return selected


def update_cloud_watchlist(tickers):
    if not tickers:
        print("No category targets to push to Supabase watchlist.")
        return
    try:
        supabase.table("bot_settings").update({"watchlist": tickers}).eq("id", 1).execute()
        print("iPad Command Center Watchlist updated with dynamic category targets.")
    except Exception as exc:
        print(f"Supabase watchlist update error: {exc}")


def save_local_targets(tickers):
    if not tickers:
        return
    file_path = os.path.join(os.path.dirname(__file__), "daily_targets.txt")
    try:
        with open(file_path, "w", encoding="utf-8") as target_file:
            target_file.write(",".join(tickers))
        print(f"Saved {len(tickers)} category targets to {file_path}.")
    except Exception as exc:
        print(f"Failed to save category targets locally: {exc}")


def run_category_target_scan():
    rows = fetch_category_targets()
    tickers = [row["ticker"] for row in rows if row.get("ticker")]
    update_cloud_watchlist(tickers)
    save_local_targets(tickers)
    print("Dynamic category target sequence complete.")


if __name__ == "__main__":
    run_category_target_scan()
