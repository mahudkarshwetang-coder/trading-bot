import os
from collections import defaultdict

from config import (
    CATEGORY_MIN_SCORE,
    CATEGORY_TARGET_LIMIT,
    CATEGORY_TARGETS_PER_CATEGORY,
    get_supabase_client,
)

supabase = get_supabase_client()

DEFAULT_CATEGORIES = ["Energy", "Logistics", "Infrastructure", "Materials"]
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
        count = 0
        for row in rows:
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
