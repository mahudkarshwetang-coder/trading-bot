import argparse
import csv
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

from config import (
    OLLAMA_MODEL,
    OLLAMA_URL,
    POST_TRADE_REVIEW_HORIZON,
    POST_TRADE_REVIEW_LIMIT,
    POST_TRADE_REVIEW_LOOKBACK_DAYS,
    POST_TRADE_REVIEW_OUTPUT_PATH,
    POST_TRADE_REVIEW_PUSH_SUPABASE,
    POST_TRADE_REVIEW_TIMEOUT_SECONDS,
    SIGNAL_JOURNAL_PATH,
    get_supabase_client,
    resolve_ollama_model,
)
from llm_metrics import record_llm_metric
from performance_governor import print_compute_notice

HORIZONS = ["15m", "1h", "1d", "5d"]
PREFERRED_HORIZON_ORDER = ["1d", "5d", "1h", "15m"]
OUTPUT_PATH = Path(POST_TRADE_REVIEW_OUTPUT_PATH)

POST_TRADE_SETUP_MESSAGE = """
Supabase is missing the public.post_trade_reviews table.

Fix:
  1. Open Supabase Dashboard -> SQL Editor.
  2. Run the SQL in supabase/post_trade_reviews.sql from this repo.
  3. Re-run: python post_trade_review.py
"""


def now_utc():
    return datetime.now(timezone.utc)


def utc_now_iso():
    return now_utc().isoformat()


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


def extract_json_object(text):
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


def ollama_chat_url():
    parsed = urlsplit(OLLAMA_URL)
    if not parsed.scheme or not parsed.netloc:
        return "http://localhost:11434/api/chat"
    return urlunsplit((parsed.scheme, parsed.netloc, "/api/chat", "", ""))


def ollama_uses_chat_endpoint():
    parsed = urlsplit(OLLAMA_URL)
    return parsed.path.rstrip("/").lower().endswith("/api/chat")


def extract_ollama_text(response_data):
    if not isinstance(response_data, dict):
        return ""
    response_text = str(response_data.get("response") or "").strip()
    if response_text:
        return response_text
    message = response_data.get("message")
    if isinstance(message, dict):
        content_text = str(message.get("content") or "").strip()
        if content_text:
            return content_text
    return ""


def choose_horizon(journal_row, preferred=POST_TRADE_REVIEW_HORIZON):
    order = [preferred] + [h for h in PREFERRED_HORIZON_ORDER if h != preferred]
    for horizon in order:
        value = to_float(journal_row.get(f"return_{horizon}_pct"))
        if value is not None:
            return horizon, value, to_float(journal_row.get(f"price_after_{horizon}"))
    return None, None, None


def load_local_journal_rows():
    path = Path(SIGNAL_JOURNAL_PATH).expanduser()
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as journal_file:
        return list(csv.DictReader(journal_file))


def journal_map_by_signal_id():
    mapping = {}
    for row in load_local_journal_rows():
        signal_id = str(row.get("signal_id") or "").strip()
        if signal_id:
            mapping[signal_id] = row
    return mapping


def fetch_open_tickers(supabase):
    try:
        response = (
            supabase.table("broker_positions")
            .select("ticker")
            .eq("is_open", True)
            .limit(500)
            .execute()
        )
        return {str(row.get("ticker") or "").upper() for row in (response.data or []) if row.get("ticker")}
    except Exception:
        return set()


def fetch_candidate_signals(supabase, lookback_days, limit):
    cutoff = (now_utc() - timedelta(days=max(1, lookback_days))).isoformat()
    rows = []
    for status in ("closed_external", "executed"):
        response = (
            supabase.table("market_signals")
            .select("id,ticker,action_type,channel,status,confidence_score,investment_memo,execution_price,created_at,updated_at")
            .eq("status", status)
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows.extend(response.data or [])

    unique = {}
    for row in rows:
        unique[str(row.get("id"))] = row
    return list(unique.values())[: limit * 2]


def local_reviewed_signal_ids():
    reviewed = set()
    if not OUTPUT_PATH.exists():
        return reviewed
    with OUTPUT_PATH.open("r", encoding="utf-8") as review_file:
        for line in review_file:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            signal_id = str(item.get("signal_id") or "").strip()
            if signal_id:
                reviewed.add(signal_id)
    return reviewed


def fetch_existing_review_signal_ids(supabase):
    try:
        response = supabase.table("post_trade_reviews").select("signal_id").limit(5000).execute()
        return {str(row.get("signal_id") or "").strip() for row in (response.data or []) if row.get("signal_id")}
    except Exception as exc:
        if missing_table(exc, "post_trade_reviews"):
            raise RuntimeError(POST_TRADE_SETUP_MESSAGE) from exc
        print(f"Existing post-trade review lookup failed: {exc}")
        return set()


def normalize_list(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value:
        return [str(value).strip()]
    return []


def normalize_review(raw):
    if not isinstance(raw, dict):
        raw = {}

    outcome = str(raw.get("overall_outcome") or "mixed").strip().lower()
    if outcome not in {"win", "loss", "mixed", "unknown"}:
        outcome = "mixed"

    return {
        "overall_outcome": outcome,
        "summary": str(raw.get("summary") or "No summary generated.").strip(),
        "what_worked": normalize_list(raw.get("what_worked")),
        "what_failed": normalize_list(raw.get("what_failed")),
        "training_adjustments": normalize_list(raw.get("training_adjustments")),
    }


def build_prompt(signal, journal_row, entry_price, exit_price, pnl_pct, horizon):
    return f"""
You are reviewing completed paper trades to train a trading bot.

Given this trade record, explain what worked and what failed with concrete, scanner-focused lessons.
Stay conservative and avoid hype.

Trade:
{json.dumps({
    "signal_id": signal.get("id"),
    "ticker": signal.get("ticker"),
    "action_type": signal.get("action_type"),
    "channel": signal.get("channel"),
    "status": signal.get("status"),
    "signal_confidence": signal.get("confidence_score"),
    "entry_price": entry_price,
    "exit_price": exit_price,
    "pnl_pct": pnl_pct,
    "outcome_horizon": horizon,
    "signal_reason": signal.get("investment_memo"),
    "journal_snapshot": {
        "rsi": journal_row.get("rsi"),
        "sma_15": journal_row.get("sma_15"),
        "rvol": journal_row.get("rvol"),
        "memo_excerpt": journal_row.get("investment_memo_excerpt"),
    },
}, ensure_ascii=True, indent=2)}

Return strict JSON:
{{
  "overall_outcome": "win|loss|mixed|unknown",
  "summary": "1-2 sentences",
  "what_worked": ["bullet", "bullet"],
  "what_failed": ["bullet", "bullet"],
  "training_adjustments": ["specific scanner/risk changes", "specific scanner/risk changes"]
}}
""".strip()


def ask_qwen_review(signal, journal_row, entry_price, exit_price, pnl_pct, horizon):
    prompt = build_prompt(signal, journal_row, entry_price, exit_price, pnl_pct, horizon)
    model_name = resolve_ollama_model("post_trade_review")
    print_compute_notice(
        "post_trade_review",
        "post-trade review analysis",
        model=model_name,
        prefix="[POST TRADE]",
    )
    payload = {
        "model": model_name,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        "think": False,
        "options": {"temperature": 0.0},
    }
    chat_payload = {
        "model": payload["model"],
        "messages": [{"role": "user", "content": payload["prompt"]}],
        "format": payload["format"],
        "stream": False,
        "think": False,
        "options": payload["options"],
    }
    started_at = time.perf_counter()
    endpoint_label = "chat" if ollama_uses_chat_endpoint() else "generate"
    raw_json_string = ""
    ticker = signal.get("ticker")
    try:
        if ollama_uses_chat_endpoint():
            response = requests.post(OLLAMA_URL, json=chat_payload, timeout=POST_TRADE_REVIEW_TIMEOUT_SECONDS)
        else:
            response = requests.post(OLLAMA_URL, json=payload, timeout=POST_TRADE_REVIEW_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        raw_json_string = extract_ollama_text(data)
        if not raw_json_string and not ollama_uses_chat_endpoint():
            chat_response = requests.post(ollama_chat_url(), json=chat_payload, timeout=POST_TRADE_REVIEW_TIMEOUT_SECONDS)
            chat_response.raise_for_status()
            chat_data = chat_response.json()
            raw_json_string = extract_ollama_text(chat_data)
        if not raw_json_string:
            raise RuntimeError("Ollama returned empty response content.")
        parsed = extract_json_object(raw_json_string)
        record_llm_metric(
            source="post_trade_review",
            task="trade_review",
            model=model_name,
            duration_seconds=time.perf_counter() - started_at,
            success=True,
            endpoint=endpoint_label,
            item_count=1,
            batch_size=1,
            attempts=1,
            timeout_seconds=POST_TRADE_REVIEW_TIMEOUT_SECONDS,
            prompt_chars=len(prompt),
            response_chars=len(raw_json_string),
            extra={"ticker": ticker, "horizon": horizon},
        )
        return normalize_review(parsed), parsed
    except Exception as exc:
        record_llm_metric(
            source="post_trade_review",
            task="trade_review",
            model=model_name,
            duration_seconds=time.perf_counter() - started_at,
            success=False,
            endpoint=endpoint_label,
            item_count=1,
            batch_size=1,
            attempts=1,
            timeout_seconds=POST_TRADE_REVIEW_TIMEOUT_SECONDS,
            prompt_chars=len(prompt),
            response_chars=len(raw_json_string),
            error=exc,
            extra={"ticker": ticker, "horizon": horizon},
        )
        raise


def build_review_record(signal, journal_row, review, raw_payload, entry_price, exit_price, pnl_pct, horizon):
    timestamp = utc_now_iso()
    return {
        "id": str(uuid.uuid4()),
        "signal_id": signal.get("id"),
        "ticker": signal.get("ticker"),
        "action_type": signal.get("action_type"),
        "channel": signal.get("channel"),
        "signal_status": signal.get("status"),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl_pct": pnl_pct,
        "outcome_horizon": horizon,
        "signal_confidence": signal.get("confidence_score"),
        "overall_outcome": review.get("overall_outcome"),
        "review_summary": review.get("summary"),
        "what_worked": review.get("what_worked"),
        "what_failed": review.get("what_failed"),
        "training_adjustments": review.get("training_adjustments"),
        "llm_payload": raw_payload,
        "reviewed_at": timestamp,
        "updated_at": timestamp,
        "source": "qwen_post_trade_review",
    }


def append_local_reviews(records):
    if not records:
        return
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("a", encoding="utf-8") as output_file:
        for record in records:
            output_file.write(json.dumps(record, ensure_ascii=True) + "\n")


def sync_reviews_to_supabase(supabase, records):
    if not records:
        return 0
    try:
        for start in range(0, len(records), 50):
            batch = records[start:start + 50]
            supabase.table("post_trade_reviews").upsert(batch, on_conflict="signal_id").execute()
        print(f"Synced {len(records)} post-trade review row(s) to Supabase.")
        return len(records)
    except Exception as exc:
        if missing_table(exc, "post_trade_reviews"):
            raise RuntimeError(POST_TRADE_SETUP_MESSAGE) from exc
        print(f"Post-trade review sync failed: {exc}")
        return 0


def review_candidates(supabase, candidates, journal_map, open_tickers, reviewed_ids, review_limit):
    created = []
    for signal in candidates:
        if len(created) >= review_limit:
            break

        signal_id = str(signal.get("id") or "")
        if not signal_id or signal_id in reviewed_ids:
            continue

        ticker = str(signal.get("ticker") or "").upper()
        status = str(signal.get("status") or "").lower()
        if status != "closed_external" and ticker in open_tickers:
            continue

        journal_row = journal_map.get(signal_id)
        if not journal_row:
            continue

        horizon, pnl_pct, exit_price = choose_horizon(journal_row)
        if horizon is None or pnl_pct is None:
            continue

        entry_price = to_float(signal.get("execution_price")) or to_float(journal_row.get("price_at_signal"))
        if entry_price is None:
            continue

        print(f"Reviewing closed trade {signal.get('action_type')} {ticker} (signal {signal_id[:8]}...)")
        try:
            review, raw_payload = ask_qwen_review(
                signal,
                journal_row,
                entry_price,
                exit_price,
                pnl_pct,
                horizon,
            )
        except Exception as exc:
            print(f"Qwen post-trade review failed for {ticker}: {exc}")
            continue

        record = build_review_record(
            signal,
            journal_row,
            review,
            raw_payload,
            entry_price,
            exit_price,
            pnl_pct,
            horizon,
        )
        created.append(record)
        reviewed_ids.add(signal_id)
        print(
            f"  Outcome={record['overall_outcome']} pnl={record['pnl_pct']:.2f}% "
            f"horizon={record['outcome_horizon']}"
        )

    return created


def run_post_trade_review(
    lookback_days=POST_TRADE_REVIEW_LOOKBACK_DAYS,
    review_limit=POST_TRADE_REVIEW_LIMIT,
    push_supabase=POST_TRADE_REVIEW_PUSH_SUPABASE,
    dry_run=False,
):
    supabase = get_supabase_client()
    open_tickers = fetch_open_tickers(supabase)
    candidates = fetch_candidate_signals(supabase, lookback_days, review_limit)
    journal_map = journal_map_by_signal_id()

    reviewed_ids = local_reviewed_signal_ids()
    if push_supabase and not dry_run:
        reviewed_ids.update(fetch_existing_review_signal_ids(supabase))

    print(
        f"Post-trade review scan: candidates={len(candidates)} "
        f"open_tickers={len(open_tickers)} already_reviewed={len(reviewed_ids)}"
    )
    records = review_candidates(
        supabase,
        candidates,
        journal_map,
        open_tickers,
        reviewed_ids,
        review_limit,
    )

    if not records:
        print("No new closed trades were eligible for post-trade review.")
        return True

    append_local_reviews(records)
    print(f"Saved {len(records)} post-trade review(s) to {OUTPUT_PATH}.")

    if dry_run:
        print("DRY RUN: Supabase sync skipped.")
        return True

    if not push_supabase:
        print("Supabase sync disabled for post-trade reviews.")
        return True

    sync_reviews_to_supabase(supabase, records)
    return True


def parse_args():
    parser = argparse.ArgumentParser(description="Ask local Qwen to review closed paper trades for training feedback.")
    parser.add_argument("--lookback-days", type=int, default=POST_TRADE_REVIEW_LOOKBACK_DAYS)
    parser.add_argument("--limit", type=int, default=POST_TRADE_REVIEW_LIMIT)
    parser.add_argument("--no-push", action="store_true", help="Skip Supabase sync.")
    parser.add_argument("--dry-run", action="store_true", help="Generate local reviews only.")
    return parser.parse_args()


def main():
    args = parse_args()
    success = run_post_trade_review(
        lookback_days=args.lookback_days,
        review_limit=args.limit,
        push_supabase=not args.no_push,
        dry_run=args.dry_run,
    )
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
