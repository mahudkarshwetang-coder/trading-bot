import argparse
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import requests

from config import (
    OLLAMA_MODEL,
    OLLAMA_URL,
    TECH_NEWS_INTERVAL_SECONDS,
    TECH_NEWS_LIMIT_PER_TICKER,
    TECH_NEWS_LLM_ENABLED,
    TECH_NEWS_LLM_MAX_ITEMS,
    TECH_NEWS_LLM_TIMEOUT_SECONDS,
    TECH_NEWS_MAX_WORKERS,
    TECH_NEWS_OUTPUT_PATH,
    TECH_NEWS_TICKERS,
)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
YAHOO_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
SEEN_CACHE_PATH = "data/tech_news_seen.json"
DEFAULT_SEEN_LIMIT = 2000

IMPACT_KEYWORDS = {
    "AI": ["ai", "artificial intelligence", "gpu", "accelerator", "data center"],
    "Chips": ["chip", "semiconductor", "foundry", "wafer", "memory"],
    "Earnings": ["earnings", "revenue", "profit", "guidance", "forecast"],
    "Analyst": ["upgrade", "downgrade", "price target", "outperform", "underperform"],
    "Regulation": ["antitrust", "regulator", "sec", "doj", "ftc", "tariff", "export"],
    "Deal": ["acquire", "acquisition", "merger", "partnership", "contract"],
    "Risk": ["lawsuit", "probe", "ban", "recall", "layoffs", "slump"],
}

DEFAULT_LLM_ANALYSIS = {
    "sentiment": "neutral",
    "impact_score": 0,
    "urgency_score": 0,
    "summary": "",
    "why_it_matters": "",
    "suggested_action": "watch",
}


def parse_tickers(value):
    raw = value or ""
    tickers = []
    seen = set()
    for item in raw.replace("\n", ",").split(","):
        ticker = item.strip().upper()
        if ticker and ticker not in seen:
            tickers.append(ticker)
            seen.add(ticker)
    return tickers


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def entry_time(entry):
    raw = entry.get("published") or entry.get("updated")
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except Exception:
            pass
    return utc_now_iso()


def fingerprint(ticker, title, link):
    key = f"{ticker}|{title}|{link}".encode("utf-8", errors="ignore")
    return hashlib.sha256(key).hexdigest()


def detect_tags(title, summary):
    haystack = f"{title} {summary}".lower()
    tags = []
    for label, words in IMPACT_KEYWORDS.items():
        if any(word in haystack for word in words):
            tags.append(label)
    return tags


def fetch_ticker_news(ticker, limit):
    url = YAHOO_RSS_URL.format(ticker=ticker)
    response = requests.get(url, headers=HEADERS, timeout=15)
    response.raise_for_status()
    feed = feedparser.parse(response.text)

    items = []
    for entry in feed.entries[:limit]:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        summary = (entry.get("summary") or "").strip()
        if not title:
            continue
        items.append(
            {
                "id": fingerprint(ticker, title, link),
                "ticker": ticker,
                "title": title,
                "link": link,
                "source": entry.get("source", {}).get("title") if isinstance(entry.get("source"), dict) else "",
                "published_at": entry_time(entry),
                "tags": detect_tags(title, summary),
                "fetched_at": utc_now_iso(),
            }
        )
    return items


def build_llm_prompt(items):
    headline_block = []
    for index, item in enumerate(items, start=1):
        headline_block.append(
            {
                "index": index,
                "ticker": item["ticker"],
                "title": item["title"],
                "published_at": item["published_at"],
                "tags": item["tags"],
            }
        )

    return f"""
You are a market news analyst monitoring technology stocks for a paper-trading research system.

Analyze these fresh headlines and return strict JSON only. Do not include markdown.

For each headline, return:
- index: same index from the input
- sentiment: bullish, bearish, or neutral
- impact_score: integer 0-100 for likely stock/sector impact
- urgency_score: integer 0-100 for how quickly a trader should pay attention
- summary: one short sentence
- why_it_matters: one short sentence explaining the market relevance
- suggested_action: one of watch, investigate, alert

Important:
- Be conservative. Most headlines are watch, not alert.
- Do not invent facts beyond the headline.
- Higher urgency is for earnings, guidance, regulation, major AI/chip news, security incidents, or material deals.

Input headlines:
{json.dumps(headline_block, ensure_ascii=True, indent=2)}

Return this exact shape:
{{
  "items": [
    {{
      "index": 1,
      "sentiment": "neutral",
      "impact_score": 0,
      "urgency_score": 0,
      "summary": "short sentence",
      "why_it_matters": "short sentence",
      "suggested_action": "watch"
    }}
  ]
}}
""".strip()


def extract_json_object(text):
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


def clamp_score(value):
    try:
        return max(0, min(100, int(round(float(value)))))
    except Exception:
        return 0


def normalize_llm_item(raw):
    analysis = dict(DEFAULT_LLM_ANALYSIS)
    if not isinstance(raw, dict):
        return analysis

    sentiment = str(raw.get("sentiment", "neutral")).lower().strip()
    if sentiment not in {"bullish", "bearish", "neutral"}:
        sentiment = "neutral"

    suggested_action = str(raw.get("suggested_action", "watch")).lower().strip()
    if suggested_action not in {"watch", "investigate", "alert"}:
        suggested_action = "watch"

    analysis.update(
        {
            "sentiment": sentiment,
            "impact_score": clamp_score(raw.get("impact_score")),
            "urgency_score": clamp_score(raw.get("urgency_score")),
            "summary": str(raw.get("summary") or "").strip(),
            "why_it_matters": str(raw.get("why_it_matters") or "").strip(),
            "suggested_action": suggested_action,
        }
    )
    return analysis


def enrich_with_ollama(items, enabled=True, max_items=TECH_NEWS_LLM_MAX_ITEMS):
    for item in items:
        item["llm_analysis"] = dict(DEFAULT_LLM_ANALYSIS)

    if not enabled or not items:
        return items

    batch = items[:max(1, max_items)]
    print(f"[TECH NEWS] Asking {OLLAMA_MODEL} to analyze {len(batch)} headline(s)...")

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": build_llm_prompt(batch),
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.0},
    }

    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=TECH_NEWS_LLM_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        parsed = extract_json_object(data.get("response", "{}"))
        llm_items = parsed.get("items", []) if isinstance(parsed, dict) else []
    except Exception as exc:
        print(f"[TECH NEWS] Ollama analysis unavailable: {exc}")
        return items

    by_index = {}
    for raw in llm_items:
        try:
            by_index[int(raw.get("index"))] = normalize_llm_item(raw)
        except Exception:
            continue

    for index, item in enumerate(batch, start=1):
        item["llm_analysis"] = by_index.get(index, dict(DEFAULT_LLM_ANALYSIS))

    return items


def load_seen(path=SEEN_CACHE_PATH):
    try:
        with open(path, "r", encoding="utf-8") as seen_file:
            data = json.load(seen_file)
            if isinstance(data, list):
                return set(data)
    except FileNotFoundError:
        return set()
    except Exception as exc:
        print(f"Could not read seen cache: {exc}")
    return set()


def save_seen(seen, path=SEEN_CACHE_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    trimmed = list(seen)[-DEFAULT_SEEN_LIMIT:]
    with open(path, "w", encoding="utf-8") as seen_file:
        json.dump(trimmed, seen_file, indent=2)


def append_items(items, output_path=TECH_NEWS_OUTPUT_PATH):
    if not items:
        return
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "a", encoding="utf-8") as output_file:
        for item in items:
            output_file.write(json.dumps(item, ensure_ascii=True) + "\n")


def print_news_item(item):
    tags = f" [{' | '.join(item['tags'])}]" if item["tags"] else ""
    analysis = item.get("llm_analysis") or {}
    llm_line = ""
    if analysis.get("summary") or analysis.get("impact_score") or analysis.get("urgency_score"):
        llm_line = (
            f" | {analysis.get('sentiment', 'neutral')}"
            f" impact={analysis.get('impact_score', 0)}"
            f" urgency={analysis.get('urgency_score', 0)}"
            f" action={analysis.get('suggested_action', 'watch')}"
        )
    print(f"{item['published_at']}  {item['ticker']:<5} {item['title']}{tags}")
    if llm_line:
        print(f"       Qwen{llm_line}")
    if analysis.get("summary"):
        print(f"       Summary: {analysis['summary']}")
    if analysis.get("why_it_matters"):
        print(f"       Why it matters: {analysis['why_it_matters']}")
    if item["link"]:
        print(f"       {item['link']}")


def scan_once(tickers, limit_per_ticker, workers, seen, llm_enabled=True):
    print(f"\n[TECH NEWS] Scanning {len(tickers)} tech tickers for latest headlines...")
    fresh_items = []

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(fetch_ticker_news, ticker, limit_per_ticker): ticker
            for ticker in tickers
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                items = future.result()
            except Exception as exc:
                print(f"[TECH NEWS] {ticker}: fetch failed: {exc}")
                continue

            for item in items:
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                fresh_items.append(item)

    fresh_items.sort(key=lambda item: item["published_at"], reverse=True)

    if not fresh_items:
        print("[TECH NEWS] No new headlines since last scan.")
        return []

    enrich_with_ollama(fresh_items, enabled=llm_enabled)

    print(f"[TECH NEWS] {len(fresh_items)} new headline(s):")
    for item in fresh_items:
        print_news_item(item)
    append_items(fresh_items)
    return fresh_items


def parse_args():
    parser = argparse.ArgumentParser(description="Standalone latest-news monitor for tech stocks.")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit.")
    parser.add_argument("--tickers", default=TECH_NEWS_TICKERS, help="Comma-separated ticker list.")
    parser.add_argument("--interval", type=int, default=TECH_NEWS_INTERVAL_SECONDS)
    parser.add_argument("--limit-per-ticker", type=int, default=TECH_NEWS_LIMIT_PER_TICKER)
    parser.add_argument("--workers", type=int, default=TECH_NEWS_MAX_WORKERS)
    parser.add_argument("--reset-seen", action="store_true", help="Ignore prior headline cache for this run.")
    parser.add_argument("--no-llm", action="store_true", help="Disable local Ollama/Qwen headline analysis.")
    return parser.parse_args()


def main():
    args = parse_args()
    tickers = parse_tickers(args.tickers)
    if not tickers:
        raise SystemExit("No tech tickers configured.")

    seen = set() if args.reset_seen else load_seen()
    print("[TECH NEWS] Monitor online.")
    print(f"[TECH NEWS] Output: {TECH_NEWS_OUTPUT_PATH}")
    print(f"[TECH NEWS] Tickers: {', '.join(tickers)}")
    llm_enabled = TECH_NEWS_LLM_ENABLED and not args.no_llm
    print(f"[TECH NEWS] Ollama analysis: {'ON' if llm_enabled else 'OFF'} ({OLLAMA_MODEL})")

    while True:
        scan_once(tickers, args.limit_per_ticker, args.workers, seen, llm_enabled=llm_enabled)
        save_seen(seen)
        if args.once:
            break
        print(f"[TECH NEWS] Sleeping {args.interval} seconds...")
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    main()
