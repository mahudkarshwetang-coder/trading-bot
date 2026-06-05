import argparse
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests

from config import (
    OLLAMA_MODEL,
    OLLAMA_URL,
    TECH_NEWS_INTERVAL_SECONDS,
    TECH_NEWS_LIMIT_PER_TICKER,
    TECH_NEWS_LLM_BATCH_SIZE,
    TECH_NEWS_LLM_DEFAULT_RETRY_LIMIT,
    TECH_NEWS_LLM_ENABLED,
    TECH_NEWS_LLM_MAX_ITEMS,
    TECH_NEWS_LLM_NUM_PREDICT,
    TECH_NEWS_LLM_NUM_PREDICT_PER_ITEM,
    TECH_NEWS_LLM_RETRY_ATTEMPTS,
    TECH_NEWS_LLM_RETRY_BACKOFF_SECONDS,
    TECH_NEWS_LLM_SINGLE_NUM_PREDICT,
    TECH_NEWS_LLM_SINGLE_TIMEOUT_SECONDS,
    TECH_NEWS_LLM_TIMEOUT_SECONDS,
    TECH_NEWS_LLM_KEEP_ALIVE,
    TECH_NEWS_LLM_WARMUP,
    TECH_NEWS_MAX_WORKERS,
    TECH_NEWS_OUTPUT_PATH,
    TECH_NEWS_RELEVANCE_FILTER_ENABLED,
    TECH_NEWS_RELEVANCE_MIN_SCORE,
    TECH_NEWS_TICKERS,
)
from llm_metrics import record_llm_metric
from performance_governor import (
    adjust_ollama_runtime,
    adjust_poll_interval,
    gaming_budget_pause,
    print_profile_notice,
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

COMPANY_ALIASES = {
    "AAPL": ["apple"],
    "MSFT": ["microsoft"],
    "NVDA": ["nvidia"],
    "AMD": ["advanced micro devices"],
    "AVGO": ["broadcom"],
    "AMZN": ["amazon"],
    "META": ["meta", "facebook"],
    "GOOGL": ["alphabet", "google"],
    "TSLA": ["tesla"],
    "ORCL": ["oracle"],
    "CRM": ["salesforce"],
    "ADBE": ["adobe"],
    "NFLX": ["netflix"],
    "INTC": ["intel"],
    "QCOM": ["qualcomm"],
    "MU": ["micron"],
    "IBM": ["ibm", "international business machines"],
    "TSM": ["tsmc", "taiwan semiconductor"],
    "ASML": ["asml"],
    "ARM": ["arm holdings", "arm ltd"],
    "PLTR": ["palantir"],
    "SMCI": ["super micro", "supermicro"],
    "NOW": ["servicenow"],
    "SNOW": ["snowflake"],
    "PANW": ["palo alto", "palo alto networks"],
    "CRWD": ["crowdstrike"],
    "DDOG": ["datadog"],
    "NET": ["cloudflare"],
}

GENERIC_MARKET_HEADLINE_HINTS = (
    "stock market today",
    "dow jones futures",
    "s&p 500",
    "nasdaq",
    "equity indexes",
    "sector update",
)


class OllamaOutputParseError(RuntimeError):
    """Raised when Ollama returns malformed JSON despite JSON mode/schema."""


def is_default_llm_analysis(analysis):
    if not isinstance(analysis, dict):
        return True
    try:
        impact = int(float(analysis.get("impact_score", 0) or 0))
    except Exception:
        impact = 0
    try:
        urgency = int(float(analysis.get("urgency_score", 0) or 0))
    except Exception:
        urgency = 0
    return (
        str(analysis.get("sentiment", "neutral")).strip().lower() == "neutral"
        and impact == 0
        and urgency == 0
        and str(analysis.get("summary") or "").strip() == ""
        and str(analysis.get("why_it_matters") or "").strip() == ""
        and str(analysis.get("suggested_action", "watch")).strip().lower() == "watch"
    )


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


def canonicalize_link(link):
    raw = str(link or "").strip()
    if not raw:
        return raw
    try:
        parsed = urlsplit(raw)
        filtered_query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in {
                "tsrc",
                "utm_source",
                "utm_medium",
                "utm_campaign",
                "utm_term",
                "utm_content",
                "guccounter",
                "guce_referrer",
                "guce_referrer_sig",
            }
        ]
        clean_query = urlencode(filtered_query, doseq=True)
        return urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path,
                clean_query,
                "",
            )
        )
    except Exception:
        return raw


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

    output_text = str(response_data.get("output") or "").strip()
    if output_text:
        return output_text

    content_text = response_data.get("content")
    if isinstance(content_text, str) and content_text.strip():
        return content_text.strip()
    return ""


def compact_preview(text, max_chars=220):
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(compact) <= max_chars:
        return compact
    return f"{compact[:max_chars]}..."


def request_ollama_text(payload, chat_payload, timeout):
    if ollama_uses_chat_endpoint():
        response = requests.post(
            OLLAMA_URL,
            json=chat_payload,
            timeout=timeout,
        )
    else:
        response = requests.post(
            OLLAMA_URL,
            json=payload,
            timeout=timeout,
        )
    response.raise_for_status()
    try:
        data = response.json()
        raw_text = extract_ollama_text(data)
    except Exception:
        raw_text = str(response.text or "").strip()
    if not raw_text and not ollama_uses_chat_endpoint():
        chat_response = requests.post(
            ollama_chat_url(),
            json=chat_payload,
            timeout=timeout,
        )
        chat_response.raise_for_status()
        try:
            chat_data = chat_response.json()
            raw_text = extract_ollama_text(chat_data)
        except Exception:
            raw_text = str(chat_response.text or "").strip()
    return str(raw_text or "")


def fingerprint(title, link):
    key = f"{str(title or '').strip().lower()}|{str(link or '').strip().lower()}".encode(
        "utf-8",
        errors="ignore",
    )
    return hashlib.sha256(key).hexdigest()


def detect_tags(title, summary):
    haystack = f"{title} {summary}".lower()
    tags = []
    for label, words in IMPACT_KEYWORDS.items():
        if any(word in haystack for word in words):
            tags.append(label)
    return tags


def ticker_relevance_score(item):
    ticker = str(item.get("ticker") or "").upper().strip()
    title = str(item.get("title") or "")
    summary = str(item.get("summary") or "")
    source = str(item.get("source") or "")
    text = f"{title} {summary}"
    title_lower = title.lower()
    text_lower = text.lower()
    source_lower = source.lower()

    score = 0
    if ticker and re.search(rf"\b{re.escape(ticker)}\b", title, flags=re.IGNORECASE):
        score += 5
    if ticker and re.search(rf"\b{re.escape(ticker)}\b", summary, flags=re.IGNORECASE):
        score += 3

    for alias in COMPANY_ALIASES.get(ticker, []):
        alias_lower = alias.lower()
        if alias_lower in title_lower:
            score += 4
        elif alias_lower in text_lower:
            score += 2
        if alias_lower in source_lower:
            score += 1

    if any(hint in title_lower for hint in GENERIC_MARKET_HEADLINE_HINTS):
        score -= 1

    if score < 0:
        return 0
    return score


def relevance_filter(items, min_score):
    threshold = max(0, int(min_score))
    kept = []
    dropped = []
    for item in items:
        score = ticker_relevance_score(item)
        item["relevance_score"] = score
        if score >= threshold:
            kept.append(item)
        else:
            dropped.append(item)
    return kept, dropped


def fetch_ticker_news(ticker, limit):
    url = YAHOO_RSS_URL.format(ticker=ticker)
    response = requests.get(url, headers=HEADERS, timeout=15)
    response.raise_for_status()
    feed = feedparser.parse(response.content)

    items = []
    for entry in feed.entries[:limit]:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        canonical_link = canonicalize_link(link)
        summary = (entry.get("summary") or "").strip()
        if not title:
            continue
        items.append(
            {
                "id": fingerprint(title, canonical_link or link),
                "ticker": ticker,
                "tickers": [ticker],
                "title": title,
                "link": canonical_link or link,
                "source": entry.get("source", {}).get("title") if isinstance(entry.get("source"), dict) else "",
                "summary": summary,
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
                "title": item["title"][:180],
                "tags": item["tags"],
            }
        )

    return (
        "You are a conservative tech-stock headline classifier.\n"
        "Use only the provided headlines. No external facts.\n"
        "Return strict JSON only with key 'items'. No markdown.\n"
        "For each input index, return: index, sentiment(bullish|bearish|neutral), "
        "impact_score(0-100 int), urgency_score(0-100 int), summary(max 12 words), "
        "why_it_matters(max 12 words), suggested_action(watch|investigate|alert).\n"
        "Most items should be 'watch'.\n"
        f"Input: {json.dumps(headline_block, ensure_ascii=True, separators=(',', ':'))}\n"
        "Output shape: "
        "{\"items\":[{\"index\":1,\"sentiment\":\"neutral\",\"impact_score\":0,"
        "\"urgency_score\":0,\"summary\":\"\",\"why_it_matters\":\"\","
        "\"suggested_action\":\"watch\"}]}"
    )


def build_llm_response_schema():
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "sentiment": {
                            "type": "string",
                            "enum": ["bullish", "bearish", "neutral"],
                        },
                        "impact_score": {"type": "integer"},
                        "urgency_score": {"type": "integer"},
                        "summary": {"type": "string"},
                        "why_it_matters": {"type": "string"},
                        "suggested_action": {
                            "type": "string",
                            "enum": ["watch", "investigate", "alert"],
                        },
                    },
                    "required": [
                        "index",
                        "sentiment",
                        "impact_score",
                        "urgency_score",
                        "summary",
                        "why_it_matters",
                        "suggested_action",
                    ],
                },
            }
        },
        "required": ["items"],
    }


def warm_ollama_model(keep_alive, timeout_seconds):
    try:
        if ollama_uses_chat_endpoint():
            payload = {
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": "Return strict JSON: {\"ok\": true}"}],
                "format": "json",
                "stream": False,
                "keep_alive": keep_alive,
                "think": False,
                "options": {
                    "temperature": 0.0,
                    "num_predict": 24,
                },
            }
        else:
            payload = {
                "model": OLLAMA_MODEL,
                "prompt": "Return strict JSON: {\"ok\": true}",
                "format": "json",
                "stream": False,
                "keep_alive": keep_alive,
                "think": False,
                "options": {
                    "temperature": 0.0,
                    "num_predict": 24,
                },
            }
        requests.post(OLLAMA_URL, json=payload, timeout=max(20, int(timeout_seconds))).raise_for_status()
        print(f"[TECH NEWS] Ollama warm-up complete ({keep_alive}).")
    except Exception as exc:
        print(f"[TECH NEWS] Ollama warm-up skipped: {exc}")


def analyze_batch_with_ollama(
    batch,
    timeout_seconds,
    num_predict,
    num_predict_per_item,
    keep_alive,
    retry_attempts,
    retry_backoff_seconds,
):
    predict_budget = max(
        48,
        int(num_predict),
        int(num_predict_per_item) * max(1, len(batch)),
    )
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": build_llm_prompt(batch),
        "format": build_llm_response_schema(),
        "stream": False,
        "keep_alive": keep_alive,
        "think": False,
        "options": {
            "temperature": 0.0,
            "num_predict": predict_budget,
        },
    }
    chat_payload = {
        "model": payload["model"],
        "messages": [{"role": "user", "content": payload["prompt"]}],
        "format": payload["format"],
        "stream": False,
        "keep_alive": payload["keep_alive"],
        "think": False,
        "options": payload["options"],
    }
    json_mode_payload = dict(payload)
    json_mode_payload["format"] = "json"
    json_mode_chat_payload = dict(chat_payload)
    json_mode_chat_payload["format"] = "json"
    attempts = max(1, int(retry_attempts))
    timeout = max(10, int(timeout_seconds))
    backoff = max(1, int(retry_backoff_seconds))
    last_error = None
    started_at = time.perf_counter()
    endpoint_label = "chat" if ollama_uses_chat_endpoint() else "generate"

    for attempt in range(1, attempts + 1):
        try:
            raw_text = request_ollama_text(payload, chat_payload, timeout)
            if not raw_text:
                raise OllamaOutputParseError("Ollama returned empty response content.")
            try:
                parsed = extract_json_object(raw_text)
            except Exception as parse_exc:
                try:
                    fallback_text = request_ollama_text(
                        json_mode_payload,
                        json_mode_chat_payload,
                        timeout,
                    )
                    parsed = extract_json_object(fallback_text)
                except Exception as fallback_exc:
                    preview = compact_preview(raw_text)
                    raise OllamaOutputParseError(
                        f"Malformed JSON from Ollama: {parse_exc}; "
                        f"fallback json-mode failed: {fallback_exc}; "
                        f"raw preview: {preview or '[empty]'}"
                    ) from parse_exc
            llm_items = parsed_items(parsed)
            record_llm_metric(
                source="tech_news_monitor",
                task="headline_batch",
                model=OLLAMA_MODEL,
                duration_seconds=time.perf_counter() - started_at,
                success=True,
                endpoint=endpoint_label,
                item_count=len(batch),
                batch_size=len(batch),
                attempts=attempt,
                timeout_seconds=timeout,
                num_predict=predict_budget,
                prompt_chars=len(payload["prompt"]),
                response_chars=len(raw_text),
            )
            break
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                wait_seconds = backoff * attempt
                print(
                    f"[TECH NEWS] Ollama retry {attempt}/{attempts - 1} "
                    f"in {wait_seconds}s for batch size {len(batch)}..."
                )
                time.sleep(wait_seconds)
                continue
            record_llm_metric(
                source="tech_news_monitor",
                task="headline_batch",
                model=OLLAMA_MODEL,
                duration_seconds=time.perf_counter() - started_at,
                success=False,
                endpoint=endpoint_label,
                item_count=len(batch),
                batch_size=len(batch),
                attempts=attempt,
                timeout_seconds=timeout,
                num_predict=predict_budget,
                prompt_chars=len(payload["prompt"]),
                error=exc,
            )
            raise last_error

    by_index = {}
    for raw in llm_items:
        try:
            by_index[int(raw.get("index"))] = normalize_llm_item(raw)
        except Exception:
            continue
    return by_index


def extract_json_object(text):
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    cleaned = cleaned.replace("<|im_start|>", "").replace("<|im_end|>", "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    decoder = json.JSONDecoder()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    for marker in ("{", "["):
        start = cleaned.find(marker)
        if start < 0:
            continue
        try:
            parsed, _ = decoder.raw_decode(cleaned[start:])
            if isinstance(parsed, list):
                return {"items": parsed}
            return parsed
        except Exception:
            continue

    raise json.JSONDecodeError("No valid JSON object found", cleaned, 0)


def clamp_score(value):
    try:
        return max(0, min(100, int(round(float(value)))))
    except Exception:
        return 0


def parsed_items(parsed):
    if isinstance(parsed, list):
        return parsed
    if not isinstance(parsed, dict):
        return []

    items = parsed.get("items")
    if isinstance(items, list):
        return items

    required = {
        "index",
        "sentiment",
        "impact_score",
        "urgency_score",
        "summary",
        "why_it_matters",
        "suggested_action",
    }
    if required.issubset(parsed.keys()):
        return [parsed]

    for key in ("result", "output", "analysis", "data"):
        nested = parsed.get(key)
        if isinstance(nested, list):
            return nested
        if isinstance(nested, dict) and isinstance(nested.get("items"), list):
            return nested.get("items")
    return []


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


def enrich_with_ollama(
    items,
    enabled=True,
    max_items=TECH_NEWS_LLM_MAX_ITEMS,
    batch_size=TECH_NEWS_LLM_BATCH_SIZE,
    default_retry_limit=TECH_NEWS_LLM_DEFAULT_RETRY_LIMIT,
    timeout_seconds=TECH_NEWS_LLM_TIMEOUT_SECONDS,
    num_predict=TECH_NEWS_LLM_NUM_PREDICT,
    num_predict_per_item=TECH_NEWS_LLM_NUM_PREDICT_PER_ITEM,
    single_timeout_seconds=TECH_NEWS_LLM_SINGLE_TIMEOUT_SECONDS,
    single_num_predict=TECH_NEWS_LLM_SINGLE_NUM_PREDICT,
    llm_retry_attempts=TECH_NEWS_LLM_RETRY_ATTEMPTS,
    llm_retry_backoff_seconds=TECH_NEWS_LLM_RETRY_BACKOFF_SECONDS,
    llm_keep_alive=TECH_NEWS_LLM_KEEP_ALIVE,
):
    for item in items:
        item["llm_analysis"] = dict(DEFAULT_LLM_ANALYSIS)

    if not enabled or not items:
        return items

    target = items[:max(1, max_items)]
    step = max(1, batch_size)
    print(
        f"[TECH NEWS] Asking {OLLAMA_MODEL} to analyze {len(target)} headline(s) "
        f"in batch size {step}..."
    )

    completed_batches = 0
    completed_items = 0
    default_retry_attempts = 0
    default_retry_recovered = 0
    max_default_retries = max(0, int(default_retry_limit))
    force_single_mode = False
    llm_disabled_for_run = False
    single_mode_failures = 0
    max_single_mode_failures = max(5, min(12, max(1, len(target) // 3)))
    for start in range(0, len(target), step):
        if llm_disabled_for_run:
            break
        batch = target[start:start + step]

        if force_single_mode and len(batch) > 1:
            print(
                f"[TECH NEWS] Single-headline mode active for "
                f"items {start + 1}-{start + len(batch)} after prior JSON parse failures."
            )
            for single in batch:
                try:
                    by_index_single = analyze_batch_with_ollama(
                        [single],
                        single_timeout_seconds,
                        single_num_predict,
                        single_num_predict,
                        llm_keep_alive,
                        llm_retry_attempts,
                        llm_retry_backoff_seconds,
                    )
                except Exception as single_exc:
                    print(
                        f"[TECH NEWS] Single-headline mode failed for "
                        f"{single.get('ticker', '?')}: {single_exc}"
                    )
                    single_mode_failures += 1
                    if single_mode_failures >= max_single_mode_failures:
                        llm_disabled_for_run = True
                        print(
                            "[TECH NEWS] Too many single-headline LLM failures. "
                            "Skipping LLM analysis for remaining headlines this run."
                        )
                        break
                    continue

                analysis = by_index_single.get(1, dict(DEFAULT_LLM_ANALYSIS))
                single["llm_analysis"] = analysis
                completed_items += 1
                single_mode_failures = 0
            continue

        try:
            by_index = analyze_batch_with_ollama(
                batch,
                timeout_seconds,
                num_predict,
                num_predict_per_item,
                llm_keep_alive,
                llm_retry_attempts,
                llm_retry_backoff_seconds,
            )
        except Exception as exc:
            print(
                f"[TECH NEWS] Ollama batch {start + 1}-{start + len(batch)} failed: {exc}"
            )
            if isinstance(exc, OllamaOutputParseError) and len(batch) > 1 and not force_single_mode:
                force_single_mode = True
                print(
                    "[TECH NEWS] Detected malformed multi-headline JSON. "
                    "Switching remaining batches to single-headline mode."
                )
            if len(batch) > 1:
                print("[TECH NEWS] Retrying failed batch one headline at a time...")
                for single in batch:
                    try:
                        by_index_single = analyze_batch_with_ollama(
                            [single],
                            single_timeout_seconds,
                            single_num_predict,
                            single_num_predict,
                            llm_keep_alive,
                            llm_retry_attempts,
                            llm_retry_backoff_seconds,
                        )
                    except Exception as single_exc:
                        print(
                            f"[TECH NEWS] Single-headline retry failed for "
                            f"{single.get('ticker', '?')}: {single_exc}"
                        )
                        single_mode_failures += 1
                        if single_mode_failures >= max_single_mode_failures:
                            llm_disabled_for_run = True
                            print(
                                "[TECH NEWS] Too many single-headline LLM failures. "
                                "Skipping LLM analysis for remaining headlines this run."
                            )
                            break
                        continue

                    analysis = by_index_single.get(1, dict(DEFAULT_LLM_ANALYSIS))
                    single["llm_analysis"] = analysis
                    completed_items += 1
                    single_mode_failures = 0
            continue

        for index, item in enumerate(batch, start=1):
            if index in by_index:
                item["llm_analysis"] = by_index[index]
                completed_items += 1
        completed_batches += 1

        for item in batch:
            analysis = item.get("llm_analysis") or {}
            if not is_default_llm_analysis(analysis):
                continue
            if default_retry_attempts >= max_default_retries:
                continue
            default_retry_attempts += 1
            try:
                retry_index = analyze_batch_with_ollama(
                    [item],
                    single_timeout_seconds,
                    single_num_predict,
                    single_num_predict,
                    llm_keep_alive,
                    llm_retry_attempts,
                    llm_retry_backoff_seconds,
                )
                retry_analysis = retry_index.get(1, dict(DEFAULT_LLM_ANALYSIS))
            except Exception as retry_exc:
                print(
                    f"[TECH NEWS] Default-output retry failed for "
                    f"{item.get('ticker', '?')}: {retry_exc}"
                )
                continue

            item["llm_analysis"] = retry_analysis
            if not is_default_llm_analysis(retry_analysis):
                default_retry_recovered += 1

    if completed_batches == 0 and completed_items == 0:
        print("[TECH NEWS] Ollama analysis unavailable across all batches; using defaults.")
    else:
        print(
            f"[TECH NEWS] Ollama analysis completed: {completed_items} headline(s) "
            f"across {completed_batches} full batch(es)."
        )
    if llm_disabled_for_run:
        print("[TECH NEWS] LLM circuit breaker activated for this run.")
    if default_retry_attempts > 0:
        print(
            f"[TECH NEWS] Default-output retries: attempted {default_retry_attempts}, "
            f"recovered {default_retry_recovered}."
        )

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
    tickers = item.get("tickers") or []
    ticker_label = ",".join(tickers[:4]) if tickers else item["ticker"]
    if tickers and len(tickers) > 4:
        ticker_label += f"+{len(tickers) - 4}"
    ticker_label = ticker_label[:28]
    analysis = item.get("llm_analysis") or {}
    llm_line = ""
    if analysis.get("summary") or analysis.get("impact_score") or analysis.get("urgency_score"):
        llm_line = (
            f" | {analysis.get('sentiment', 'neutral')}"
            f" impact={analysis.get('impact_score', 0)}"
            f" urgency={analysis.get('urgency_score', 0)}"
            f" action={analysis.get('suggested_action', 'watch')}"
        )
    print(f"{item['published_at']}  {ticker_label:<8} {item['title']}{tags}")
    if llm_line:
        print(f"       Qwen{llm_line}")
    if analysis.get("summary"):
        print(f"       Summary: {analysis['summary']}")
    if analysis.get("why_it_matters"):
        print(f"       Why it matters: {analysis['why_it_matters']}")
    if item["link"]:
        print(f"       {item['link']}")


def scan_once(
    tickers,
    limit_per_ticker,
    workers,
    seen,
    llm_enabled=True,
    llm_max_items=TECH_NEWS_LLM_MAX_ITEMS,
    llm_batch_size=TECH_NEWS_LLM_BATCH_SIZE,
    llm_default_retry_limit=TECH_NEWS_LLM_DEFAULT_RETRY_LIMIT,
    llm_timeout=TECH_NEWS_LLM_TIMEOUT_SECONDS,
    llm_num_predict=TECH_NEWS_LLM_NUM_PREDICT,
    llm_num_predict_per_item=TECH_NEWS_LLM_NUM_PREDICT_PER_ITEM,
    llm_single_timeout=TECH_NEWS_LLM_SINGLE_TIMEOUT_SECONDS,
    llm_single_num_predict=TECH_NEWS_LLM_SINGLE_NUM_PREDICT,
    llm_retry_attempts=TECH_NEWS_LLM_RETRY_ATTEMPTS,
    llm_retry_backoff_seconds=TECH_NEWS_LLM_RETRY_BACKOFF_SECONDS,
    llm_keep_alive=TECH_NEWS_LLM_KEEP_ALIVE,
    relevance_filter_enabled=TECH_NEWS_RELEVANCE_FILTER_ENABLED,
    relevance_min_score=TECH_NEWS_RELEVANCE_MIN_SCORE,
):
    runtime = adjust_ollama_runtime(
        "tech_news_monitor",
        keep_alive=llm_keep_alive,
        timeout_seconds=llm_timeout,
        num_predict=llm_num_predict,
        batch_size=llm_batch_size,
        max_items=llm_max_items,
        workers=workers,
    )
    if runtime["profile"].active:
        print_profile_notice("tech_news_scan", prefix="[TECH NEWS]")
        workers = runtime["workers"]
        llm_keep_alive = runtime["keep_alive"]
        llm_timeout = runtime["timeout_seconds"]
        llm_num_predict = runtime["num_predict"]
        llm_batch_size = runtime["batch_size"]
        llm_max_items = runtime["max_items"]
        llm_num_predict_per_item = min(int(llm_num_predict_per_item), int(runtime["num_predict"]))
        llm_single_timeout = min(int(llm_single_timeout), int(runtime["timeout_seconds"]))
        llm_single_num_predict = min(int(llm_single_num_predict), int(runtime["num_predict"]))

    gaming_budget_pause("tech_news_fetch", estimated_work_seconds=max(1.0, len(tickers) / 20.0))
    print(f"\n[TECH NEWS] Scanning {len(tickers)} tech tickers for latest headlines...")
    fresh_items_by_id = {}

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
                existing = fresh_items_by_id.get(item["id"])
                if existing:
                    for ticker in item.get("tickers", []):
                        if ticker not in existing["tickers"]:
                            existing["tickers"].append(ticker)
                    existing["tags"] = sorted(set(existing.get("tags", [])) | set(item.get("tags", [])))
                    if item["published_at"] > existing["published_at"]:
                        existing["published_at"] = item["published_at"]
                    if not existing.get("source") and item.get("source"):
                        existing["source"] = item["source"]
                else:
                    fresh_items_by_id[item["id"]] = item

    fresh_items = list(fresh_items_by_id.values())
    fresh_items.sort(key=lambda item: item["published_at"], reverse=True)
    for item in fresh_items:
        seen.add(item["id"])

    if not fresh_items:
        print("[TECH NEWS] No new headlines since last scan.")
        return []

    filtered_items = fresh_items
    if relevance_filter_enabled:
        filtered_items, dropped_items = relevance_filter(fresh_items, relevance_min_score)
        if dropped_items:
            print(
                f"[TECH NEWS] Relevance filter kept {len(filtered_items)}/{len(fresh_items)} headline(s); "
                f"dropped {len(dropped_items)} weak ticker matches (min_score={int(relevance_min_score)})."
            )

    if not filtered_items:
        print("[TECH NEWS] All new headlines were low-relevance after filtering.")
        return []

    for item in filtered_items:
        item.pop("summary", None)
        item.pop("relevance_score", None)

    gaming_budget_pause("tech_news_ollama", estimated_work_seconds=max(1.0, len(filtered_items) / 4.0))
    enrich_with_ollama(
        filtered_items,
        enabled=llm_enabled,
        max_items=llm_max_items,
        batch_size=llm_batch_size,
        default_retry_limit=llm_default_retry_limit,
        timeout_seconds=llm_timeout,
        num_predict=llm_num_predict,
        num_predict_per_item=llm_num_predict_per_item,
        single_timeout_seconds=llm_single_timeout,
        single_num_predict=llm_single_num_predict,
        llm_retry_attempts=llm_retry_attempts,
        llm_retry_backoff_seconds=llm_retry_backoff_seconds,
        llm_keep_alive=llm_keep_alive,
    )

    print(f"[TECH NEWS] {len(filtered_items)} new headline(s):")
    for item in filtered_items:
        print_news_item(item)
    append_items(filtered_items)
    return filtered_items


def parse_args():
    parser = argparse.ArgumentParser(description="Standalone latest-news monitor for tech stocks.")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit.")
    parser.add_argument("--tickers", default=TECH_NEWS_TICKERS, help="Comma-separated ticker list.")
    parser.add_argument("--interval", type=int, default=TECH_NEWS_INTERVAL_SECONDS)
    parser.add_argument("--limit-per-ticker", type=int, default=TECH_NEWS_LIMIT_PER_TICKER)
    parser.add_argument("--workers", type=int, default=TECH_NEWS_MAX_WORKERS)
    parser.add_argument("--llm-max-items", type=int, default=TECH_NEWS_LLM_MAX_ITEMS)
    parser.add_argument("--llm-batch-size", type=int, default=TECH_NEWS_LLM_BATCH_SIZE)
    parser.add_argument("--llm-default-retry-limit", type=int, default=TECH_NEWS_LLM_DEFAULT_RETRY_LIMIT)
    parser.add_argument("--llm-timeout", type=int, default=TECH_NEWS_LLM_TIMEOUT_SECONDS)
    parser.add_argument("--llm-num-predict", type=int, default=TECH_NEWS_LLM_NUM_PREDICT)
    parser.add_argument("--llm-num-predict-per-item", type=int, default=TECH_NEWS_LLM_NUM_PREDICT_PER_ITEM)
    parser.add_argument("--llm-single-timeout", type=int, default=TECH_NEWS_LLM_SINGLE_TIMEOUT_SECONDS)
    parser.add_argument("--llm-single-num-predict", type=int, default=TECH_NEWS_LLM_SINGLE_NUM_PREDICT)
    parser.add_argument("--llm-retry-attempts", type=int, default=TECH_NEWS_LLM_RETRY_ATTEMPTS)
    parser.add_argument("--llm-retry-backoff", type=int, default=TECH_NEWS_LLM_RETRY_BACKOFF_SECONDS)
    parser.add_argument("--llm-keep-alive", default=TECH_NEWS_LLM_KEEP_ALIVE)
    parser.add_argument("--relevance-min-score", type=int, default=TECH_NEWS_RELEVANCE_MIN_SCORE)
    parser.add_argument("--no-relevance-filter", action="store_true", help="Disable ticker/headline relevance filtering.")
    parser.add_argument("--reset-seen", action="store_true", help="Ignore prior headline cache for this run.")
    parser.add_argument("--no-llm-warmup", action="store_true", help="Skip Ollama warm-up call before scanning.")
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
    relevance_enabled = TECH_NEWS_RELEVANCE_FILTER_ENABLED and not args.no_relevance_filter
    print(f"[TECH NEWS] Ollama analysis: {'ON' if llm_enabled else 'OFF'} ({OLLAMA_MODEL})")
    print(
        f"[TECH NEWS] Relevance filter: {'ON' if relevance_enabled else 'OFF'} "
        f"(min_score={args.relevance_min_score})"
    )
    if llm_enabled:
        startup_runtime = adjust_ollama_runtime(
            "tech_news_monitor_startup",
            keep_alive=args.llm_keep_alive,
            timeout_seconds=args.llm_timeout,
            num_predict=args.llm_num_predict,
            batch_size=args.llm_batch_size,
            max_items=args.llm_max_items,
            workers=args.workers,
            warmup=TECH_NEWS_LLM_WARMUP and not args.no_llm_warmup,
        )
        if startup_runtime["profile"].active:
            print_profile_notice("tech_news_startup", prefix="[TECH NEWS]")
        print(
            "[TECH NEWS] LLM runtime: "
            f"keep_alive={args.llm_keep_alive}, batch_size={args.llm_batch_size}, "
            f"timeout={args.llm_timeout}s, retries={args.llm_retry_attempts}, "
            f"predict_base={args.llm_num_predict}, per_item={args.llm_num_predict_per_item}"
        )
        if startup_runtime.get("warmup"):
            warm_ollama_model(startup_runtime["keep_alive"], startup_runtime["timeout_seconds"])
        elif TECH_NEWS_LLM_WARMUP and not args.no_llm_warmup:
            print("[TECH NEWS] Ollama warm-up skipped by performance governor.")

    while True:
        scan_once(
            tickers,
            args.limit_per_ticker,
            args.workers,
            seen,
            llm_enabled=llm_enabled,
            llm_max_items=args.llm_max_items,
            llm_batch_size=args.llm_batch_size,
            llm_default_retry_limit=args.llm_default_retry_limit,
            llm_timeout=args.llm_timeout,
            llm_num_predict=args.llm_num_predict,
            llm_num_predict_per_item=args.llm_num_predict_per_item,
            llm_single_timeout=args.llm_single_timeout,
            llm_single_num_predict=args.llm_single_num_predict,
            llm_retry_attempts=args.llm_retry_attempts,
            llm_retry_backoff_seconds=args.llm_retry_backoff,
            llm_keep_alive=args.llm_keep_alive,
            relevance_filter_enabled=relevance_enabled,
            relevance_min_score=args.relevance_min_score,
        )
        save_seen(seen)
        if args.once:
            break
        sleep_seconds = adjust_poll_interval(args.interval)
        if sleep_seconds != args.interval:
            print(f"[TECH NEWS] Sleeping {sleep_seconds} seconds (performance governor adjusted from {args.interval}).")
        else:
            print(f"[TECH NEWS] Sleeping {args.interval} seconds...")
        time.sleep(max(30, sleep_seconds))


if __name__ == "__main__":
    main()
