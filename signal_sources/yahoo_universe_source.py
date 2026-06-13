from pathlib import Path

from config import CATEGORY_MIN_SCORE, get_supabase_client
from .common import SignalSourceCandidate, SourceRunResult, clean_ticker


def load_daily_targets():
    path = Path("daily_targets.txt")
    if not path.exists():
        return []
    tickers = []
    seen = set()
    for raw in path.read_text(encoding="utf-8", errors="replace").replace("\n", ",").split(","):
        ticker = clean_ticker(raw)
        if ticker and ticker not in seen:
            tickers.append(ticker)
            seen.add(ticker)
    return tickers


def load_category_rows(limit):
    supabase = get_supabase_client()
    response = (
        supabase.table("category_universe")
        .select("ticker,company_name,category,theme,category_score,market_cap,average_volume")
        .eq("active", True)
        .gte("category_score", CATEGORY_MIN_SCORE)
        .order("category_score", desc=True)
        .order("market_cap", desc=True)
        .limit(limit)
        .execute()
    )
    return response.data or []


def run_yahoo_universe_source(limit=160):
    result = SourceRunResult(source="yahoo_universe")
    seen = set()
    try:
        rows = load_category_rows(limit)
        for row in rows:
            ticker = clean_ticker(row.get("ticker"))
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            result.candidates.append(
                SignalSourceCandidate(
                    ticker=ticker,
                    source=result.source,
                    score=float(row.get("category_score") or 0.0),
                    action="WATCH",
                    reason="category_universe shortlisted by Yahoo-derived fundamentals/themes",
                    category=row.get("category") or "",
                    theme=row.get("theme") or "",
                    metadata={
                        "company_name": row.get("company_name"),
                        "market_cap": row.get("market_cap"),
                        "average_volume": row.get("average_volume"),
                    },
                )
            )
            if len(result.candidates) >= limit:
                break
    except Exception as exc:
        result.warnings.append(f"category_universe unavailable: {exc}")

    if not result.candidates:
        tickers = load_daily_targets()
        if not tickers:
            result.warnings.append("daily_targets.txt is missing or empty")
        for ticker in tickers[:limit]:
            result.candidates.append(
                SignalSourceCandidate(
                    ticker=ticker,
                    source=result.source,
                    score=50.0,
                    action="WATCH",
                    reason="daily_targets fallback",
                )
            )

    result.metadata["candidate_count"] = len(result.candidates)
    return result.finish()
