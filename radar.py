from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from config import (
    RADAR_ALLOW_YAHOO_TRENDING_FALLBACK,
    RADAR_MIN_ABS_MOVE_PCT,
    RADAR_TARGET_LIMIT,
    get_supabase_client,
)

supabase = get_supabase_client()
ROOT = Path(__file__).resolve().parent
DAILY_TARGETS_PATH = ROOT / "daily_targets.txt"


def load_daily_targets():
    try:
        text = DAILY_TARGETS_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return []
    except Exception as exc:
        print(f"Could not read daily targets for radar: {exc}")
        return []

    tickers = []
    seen = set()
    for item in text.split(","):
        ticker = item.strip().upper()
        if ticker and ticker not in seen:
            tickers.append(ticker)
            seen.add(ticker)
    return tickers


def close_series_for_ticker(raw, ticker, multi_ticker):
    try:
        if multi_ticker:
            frame = raw[ticker]
        else:
            frame = raw
        close = frame.get("Close") if isinstance(frame, pd.DataFrame) else None
        return close.dropna() if close is not None else None
    except Exception:
        return None


def get_daily_target_movers():
    tickers = load_daily_targets()
    if not tickers:
        print("No daily category targets found for radar.")
        return []

    print(f"Scanning {len(tickers)} daily category targets for radar movers...")
    try:
        raw = yf.download(
            tickers,
            period="5d",
            interval="1d",
            progress=False,
            threads=True,
            auto_adjust=False,
        )
    except Exception as exc:
        print(f"Radar daily-target download failed: {exc}")
        return []

    if raw is None or raw.empty:
        print("Radar daily-target download returned no data.")
        return []

    multi_ticker = isinstance(raw.columns, pd.MultiIndex)
    movers = []
    for ticker in tickers:
        close = close_series_for_ticker(raw, ticker, multi_ticker)
        if close is None or len(close) < 2:
            continue
        try:
            latest = float(close.iloc[-1])
            previous = float(close.iloc[-2])
        except Exception:
            continue
        if previous <= 0:
            continue
        pct_change = ((latest - previous) / previous) * 100.0
        if abs(pct_change) < RADAR_MIN_ABS_MOVE_PCT:
            continue
        movers.append(
            {
                "ticker": ticker,
                "pct_change": pct_change,
                "abs_change": abs(pct_change),
            }
        )

    movers.sort(key=lambda row: row["abs_change"], reverse=True)
    selected = [row["ticker"] for row in movers[: max(1, int(RADAR_TARGET_LIMIT))]]
    if selected:
        summary = ", ".join(
            f"{row['ticker']} {row['pct_change']:+.2f}%"
            for row in movers[: max(1, int(RADAR_TARGET_LIMIT))]
        )
        print(f"Dynamic radar movers: {summary}")
    else:
        print(
            "No daily target moved enough for radar "
            f"(min_abs_move={RADAR_MIN_ABS_MOVE_PCT:g}%)."
        )
    return selected


def get_yahoo_trending_tickers():
    print("Sweeping Yahoo Finance public trending endpoint...")
    url = "https://query1.finance.yahoo.com/v1/finance/trending/US?count=10"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        quotes = data["finance"]["result"][0]["quotes"]
        tickers = [
            str(q.get("symbol") or "").upper()
            for q in quotes
            if q.get("symbol") and "-" not in q["symbol"] and "^" not in q["symbol"]
        ]
        return tickers[: max(1, int(RADAR_TARGET_LIMIT))]
    except Exception as exc:
        print(f"Yahoo trending radar fallback failed: {exc}")
        return []


def get_trending_tickers():
    target_movers = get_daily_target_movers()
    if target_movers:
        return target_movers

    if RADAR_ALLOW_YAHOO_TRENDING_FALLBACK:
        return get_yahoo_trending_tickers()

    return []


def update_cloud_watchlist(tickers):
    """Updates only the radar watchlist column; never overwrites daily category targets."""
    try:
        if not tickers:
            print("No dynamic radar targets found. Clearing stale radar_watchlist.")
            supabase.table("bot_settings").update({"radar_watchlist": []}).eq("id", 1).execute()
            return

        print(f"Cloud radar watchlist update: {tickers}")
        supabase.table("bot_settings").update({"radar_watchlist": tickers}).eq("id", 1).execute()
        print("Cloud radar watchlist successfully updated.")
    except Exception as exc:
        print(f"Supabase radar update error: {exc}")


def run_radar_scan():
    print("Initiating dynamic category-target radar")
    print("-" * 50)

    hot_tickers = get_trending_tickers()
    update_cloud_watchlist(hot_tickers)

    print("-" * 50)
    print("Radar sequence complete. The system is primed for the LLM Scanner.")


if __name__ == "__main__":
    run_radar_scan()
