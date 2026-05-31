import os
from typing import List

from config import ENERGY_MIN_SCORE, ENERGY_TARGET_LIMIT, get_supabase_client

supabase = get_supabase_client()


def fetch_energy_targets(limit: int = ENERGY_TARGET_LIMIT, min_score: float = ENERGY_MIN_SCORE) -> List[str]:
    """Load the strongest active energy-universe tickers from Supabase."""
    print(
        f"⚡ Loading energy universe targets from Supabase "
        f"(limit={limit}, min_score={min_score})..."
    )
    try:
        response = (
            supabase.table("energy_universe")
            .select("ticker, company_name, exchange, energy_theme, energy_purity_score, market_cap")
            .eq("active", True)
            .gte("energy_purity_score", min_score)
            .order("energy_purity_score", desc=True)
            .order("market_cap", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception as exc:
        print(f"🚨 Failed to load energy universe targets: {exc}")
        return []

    rows = response.data or []
    if not rows:
        print("⚠️ No energy universe rows matched the target filters.")
        return []

    tickers = []
    for row in rows:
        ticker = row.get("ticker")
        if not ticker:
            continue
        tickers.append(ticker)
        print(
            f"   ✅ {ticker:<6} {row.get('exchange', ''):<6} "
            f"{row.get('energy_theme', ''):<22} "
            f"score={row.get('energy_purity_score')} "
            f"{row.get('company_name', '')}"
        )

    print(f"✅ Loaded {len(tickers)} energy targets.")
    return tickers


def update_cloud_watchlist(tickers: List[str]) -> None:
    if not tickers:
        print("⚠️ No energy targets to push to Supabase watchlist.")
        return

    print(f"\n☁️ Pushing energy watchlist to Supabase: {tickers}")
    try:
        supabase.table("bot_settings").update({"watchlist": tickers}).eq("id", 1).execute()
        print("✅ iPad Command Center Watchlist updated with energy targets.")
    except Exception as exc:
        print(f"🚨 Supabase watchlist update error: {exc}")


def save_local_targets(tickers: List[str]) -> None:
    """Save selected energy tickers for intraday scanners that read daily_targets.txt."""
    if not tickers:
        return

    file_path = os.path.join(os.path.dirname(__file__), "daily_targets.txt")
    try:
        with open(file_path, "w") as target_file:
            target_file.write(",".join(tickers))
        print(f"📁 Saved {len(tickers)} energy targets to {file_path}.")
    except Exception as exc:
        print(f"🚨 Failed to save energy targets locally: {exc}")


def run_energy_target_scan() -> None:
    tickers = fetch_energy_targets()
    update_cloud_watchlist(tickers)
    save_local_targets(tickers)
    print("⚡ Energy target sequence complete.")


if __name__ == "__main__":
    run_energy_target_scan()
