import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import IBKR_NEWS_OUTPUT_PATH, IBKR_NEWS_LOOKBACK_MINUTES
from .common import SignalSourceCandidate, SourceRunResult, clean_ticker


def parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def load_recent_news_rows(lookback_minutes):
    path = Path(IBKR_NEWS_OUTPUT_PATH)
    if not path.exists():
        return [], f"{IBKR_NEWS_OUTPUT_PATH} not found"

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, int(lookback_minutes)))
    rows = []
    warning = ""
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                published_at = parse_time(row.get("published_at") or row.get("fetched_at"))
                if published_at and published_at >= cutoff:
                    rows.append(row)
    except Exception as exc:
        warning = f"IBKR news feed read failed: {exc}"
    return rows, warning


def run_ibkr_news_source(limit=160):
    result = SourceRunResult(source="ibkr_news")
    rows, warning = load_recent_news_rows(IBKR_NEWS_LOOKBACK_MINUTES)
    if warning:
        result.warnings.append(warning)

    seen = set()
    for row in reversed(rows):
        ticker = clean_ticker(row.get("ticker"))
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        confidence = row.get("confidence")
        try:
            score = float(confidence or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        action = str(row.get("action") or "WATCH").upper()
        result.candidates.append(
            SignalSourceCandidate(
                ticker=ticker,
                source=result.source,
                score=score,
                action=action if action in {"BUY", "SELL", "WATCH"} else "WATCH",
                reason=row.get("headline") or "recent IBKR news catalyst",
                metadata={
                    "provider_code": row.get("provider_code"),
                    "provider_name": row.get("provider_name"),
                    "article_id": row.get("article_id"),
                    "published_at": row.get("published_at"),
                    "directional_score": row.get("directional_score"),
                    "signal_pushed": row.get("signal_pushed"),
                },
            )
        )
        if len(result.candidates) >= limit:
            break

    if not result.candidates and not warning:
        result.warnings.append("no recent IBKR news rows in lookback window")
    result.metadata["lookback_minutes"] = IBKR_NEWS_LOOKBACK_MINUTES
    result.metadata["candidate_count"] = len(result.candidates)
    return result.finish()
