import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from config import (
    SIGNAL_SOURCE_MAX_CANDIDATES,
    SIGNAL_SOURCE_OUTPUT_PATH,
    SIGNAL_SOURCE_USE_IBKR_NEWS,
    SIGNAL_SOURCE_USE_IBKR_SCANNER,
    SIGNAL_SOURCE_USE_YAHOO_UNIVERSE,
    SIGNAL_SOURCES_ENABLED,
    get_supabase_client,
)
from local_data_recorder import append_local_event
from signal_sources.common import clean_ticker
from signal_sources.ibkr_news_source import run_ibkr_news_source
from signal_sources.ibkr_scanner_source import run_ibkr_scanner_source
from signal_sources.yahoo_universe_source import run_yahoo_universe_source
from system_status import publish_system_status

SOURCE = "signal_source_hub"
SETTINGS_TABLE = "bot_settings"


SOURCE_RUNNERS = {
    "yahoo": run_yahoo_universe_source,
    "yahoo-universe": run_yahoo_universe_source,
    "ibkr-scanner": run_ibkr_scanner_source,
    "ibkr-news": run_ibkr_news_source,
}


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def default_sources():
    sources = []
    if SIGNAL_SOURCE_USE_YAHOO_UNIVERSE:
        sources.append("yahoo-universe")
    if SIGNAL_SOURCE_USE_IBKR_SCANNER:
        sources.append("ibkr-scanner")
    if SIGNAL_SOURCE_USE_IBKR_NEWS:
        sources.append("ibkr-news")
    return sources


def parse_sources(raw):
    if not raw:
        return default_sources()
    return [item.strip().lower() for item in str(raw).replace(";", ",").split(",") if item.strip()]


def append_jsonl(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str, sort_keys=True) + "\n")


def latest_source_run(path=SIGNAL_SOURCE_OUTPUT_PATH):
    target = Path(path)
    if not target.exists():
        return {}
    last = ""
    try:
        with target.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last = line
        return json.loads(last) if last else {}
    except Exception:
        return {}


def merge_candidates(source_results, limit):
    by_ticker = {}
    source_counts = defaultdict(int)
    warnings = []

    for result in source_results:
        source_name = result.get("source") or "unknown"
        warnings.extend(result.get("warnings") or [])
        for candidate in result.get("candidates") or []:
            ticker = clean_ticker(candidate.get("ticker"))
            if not ticker:
                continue
            source_counts[source_name] += 1
            entry = by_ticker.setdefault(
                ticker,
                {
                    "ticker": ticker,
                    "sources": [],
                    "best_score": 0.0,
                    "actions": [],
                    "reasons": [],
                    "categories": [],
                    "themes": [],
                    "metadata": {},
                },
            )
            if source_name not in entry["sources"]:
                entry["sources"].append(source_name)
            try:
                score = float(candidate.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            entry["best_score"] = max(entry["best_score"], score)
            action = str(candidate.get("action") or "").upper()
            if action and action not in entry["actions"]:
                entry["actions"].append(action)
            reason = candidate.get("reason")
            if reason and reason not in entry["reasons"]:
                entry["reasons"].append(reason)
            category = candidate.get("category")
            if category and category not in entry["categories"]:
                entry["categories"].append(category)
            theme = candidate.get("theme")
            if theme and theme not in entry["themes"]:
                entry["themes"].append(theme)
            entry["metadata"][source_name] = candidate.get("metadata") or {}

    merged = sorted(
        by_ticker.values(),
        key=lambda row: (len(row["sources"]), row["best_score"], row["ticker"]),
        reverse=True,
    )
    overlap = [row["ticker"] for row in merged if len(row["sources"]) > 1]
    return merged[:limit], dict(source_counts), overlap[:80], warnings


def update_experimental_source_summary(summary, dry_run=False):
    if dry_run:
        return True
    try:
        supabase = get_supabase_client()
        response = (
            supabase.table(SETTINGS_TABLE)
            .select("experimental_last_result")
            .eq("id", 1)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        payload = rows[0].get("experimental_last_result") if rows else {}
        if not isinstance(payload, dict):
            payload = {}
        payload["source_summary"] = summary
        supabase.table(SETTINGS_TABLE).update(
            {
                "experimental_last_result": payload,
                "experimental_last_requested_at": summary.get("generated_at") or utc_now_iso(),
            }
        ).eq("id", 1).execute()
        return True
    except Exception as exc:
        print(f"[SIGNAL SOURCES] Supabase source summary update skipped: {exc}")
        return False


def run_signal_source_hub(sources=None, limit=SIGNAL_SOURCE_MAX_CANDIDATES, dry_run=False, push_supabase=True):
    if not SIGNAL_SOURCES_ENABLED:
        print("[SIGNAL SOURCES] Disabled by SIGNAL_SOURCES_ENABLED=false.")
        return True

    source_names = parse_sources(sources)
    started_at = utc_now_iso()
    results = []
    print(f"[SIGNAL SOURCES] Running source hub: {', '.join(source_names) or 'none'}")

    for name in source_names:
        runner = SOURCE_RUNNERS.get(name)
        if not runner:
            results.append(
                {
                    "source": name,
                    "candidates": [],
                    "warnings": [f"unknown source: {name}"],
                    "metadata": {},
                    "started_at": utc_now_iso(),
                    "finished_at": utc_now_iso(),
                }
            )
            continue
        try:
            results.append(runner(limit=limit).to_dict())
        except Exception as exc:
            results.append(
                {
                    "source": name,
                    "candidates": [],
                    "warnings": [f"{name} failed: {exc}"],
                    "metadata": {},
                    "started_at": utc_now_iso(),
                    "finished_at": utc_now_iso(),
                }
            )

    merged, source_counts, overlap, warnings = merge_candidates(results, limit)
    summary = {
        "generated_at": utc_now_iso(),
        "started_at": started_at,
        "source": SOURCE,
        "enabled_sources": source_names,
        "source_counts": source_counts,
        "candidate_count": len(merged),
        "overlap_count": len(overlap),
        "overlap_tickers": overlap,
        "warnings": warnings[:20],
        "top_candidates": merged[: min(40, len(merged))],
    }
    append_jsonl(SIGNAL_SOURCE_OUTPUT_PATH, summary)
    append_local_event("signal_source_hub_run", summary, source=SOURCE)
    if push_supabase:
        update_experimental_source_summary(summary, dry_run=dry_run)
    publish_system_status(
        "signal_source_hub",
        "success",
        detail=f"Signal sources found {len(merged)} unique candidate(s), {len(overlap)} overlap ticker(s).",
        metadata={
            "source_counts": source_counts,
            "overlap_count": len(overlap),
            "warnings": warnings[:8],
        },
    )
    print(
        "[SIGNAL SOURCES] Complete: "
        f"{len(merged)} unique ticker(s), overlap={len(overlap)}, counts={source_counts}"
    )
    if warnings:
        print(f"[SIGNAL SOURCES] Warnings: {'; '.join(warnings[:3])}")
    return True


def parse_args():
    parser = argparse.ArgumentParser(description="Run normalized market signal sources.")
    parser.add_argument("--sources", help="Comma-separated sources: yahoo-universe,ibkr-scanner,ibkr-news")
    parser.add_argument("--limit", type=int, default=SIGNAL_SOURCE_MAX_CANDIDATES)
    parser.add_argument("--dry-run", action="store_true", help="Do not update Supabase.")
    parser.add_argument("--no-push-supabase", action="store_true", help="Only write local source log.")
    return parser.parse_args()


def main():
    args = parse_args()
    success = run_signal_source_hub(
        sources=args.sources,
        limit=max(1, int(args.limit)),
        dry_run=args.dry_run,
        push_supabase=not args.no_push_supabase,
    )
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
