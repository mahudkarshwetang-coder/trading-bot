import argparse
import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests

from config import (
    MARKET_BRIEFING_CATEGORY_LIMIT,
    MARKET_BRIEFING_LOOKBACK_HOURS,
    MARKET_BRIEFING_NEWS_LIMIT,
    MARKET_BRIEFING_OUTPUT_PATH,
    MARKET_BRIEFING_POSITION_LIMIT,
    MARKET_BRIEFING_PUSH_SUPABASE,
    MARKET_BRIEFING_SIGNAL_LIMIT,
    MARKET_BRIEFING_TIMEOUT_SECONDS,
    MARKET_TIMEZONE,
    OLLAMA_MODEL,
    OLLAMA_URL,
    TECH_NEWS_OUTPUT_PATH,
    IBKR_NEWS_OUTPUT_PATH,
    get_supabase_client,
)
from llm_metrics import record_llm_metric

DEFAULT_CATEGORIES = [
    "Nuclear Energy",
    "Data Center Power & Grid Infrastructure",
    "AI Chips",
    "Cybersecurity & AI Security",
    "Aerospace Defense & Security",
    "Energy",
    "Logistics",
    "Infrastructure",
    "Materials",
]
CATEGORY_SNAPSHOT_PATH = Path("data/category_brief_snapshot.json")
DEFAULT_OUTPUT_PATH = Path(MARKET_BRIEFING_OUTPUT_PATH)
LATEST_MORNING_JSON = Path("data/latest_market_briefing_morning.json")
LATEST_EVENING_JSON = Path("data/latest_market_briefing_evening.json")
LATEST_MORNING_MD = Path("data/latest_market_briefing_morning.md")
LATEST_EVENING_MD = Path("data/latest_market_briefing_evening.md")
MACRO_STATE_PATH = Path("strategy_vault/00_daily_macro_state.txt")

BRIEFING_TABLE_SETUP_MESSAGE = """
Supabase is missing the public.daily_market_briefings table.

Fix:
  1. Open Supabase Dashboard -> SQL Editor.
  2. Run the SQL in supabase/daily_market_briefings.sql from this repo.
  3. Re-run: python daily_market_briefing.py --session morning
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


def safe_float(value):
    try:
        return float(value)
    except Exception:
        return None


def market_tz():
    try:
        return ZoneInfo(MARKET_TIMEZONE)
    except Exception:
        return timezone.utc


def resolve_session(session):
    if session in {"morning", "evening"}:
        return session
    local_now = datetime.now(market_tz())
    return "morning" if local_now.hour < 13 else "evening"


def local_briefing_date():
    return datetime.now(market_tz()).date().isoformat()


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def save_json(path, data):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")


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


def fetch_category_rows(supabase, category, limit):
    response = (
        supabase.table("category_universe")
        .select("ticker,company_name,category,theme,category_score,market_cap,last_updated")
        .eq("active", True)
        .eq("category", category)
        .order("category_score", desc=True)
        .order("market_cap", desc=True)
        .limit(limit)
        .execute()
    )
    return response.data or []


def get_category_universe_context(supabase, per_category):
    summary = {
        "available": True,
        "error": None,
        "categories": {},
        "shifts": {},
        "snapshot_at": utc_now_iso(),
    }

    try:
        for category in DEFAULT_CATEGORIES:
            rows = fetch_category_rows(supabase, category, per_category)
            summary["categories"][category] = rows
    except Exception as exc:
        if missing_table(exc, "category_universe"):
            summary.update({"available": False, "error": "category_universe table missing"})
            return summary
        raise

    previous_snapshot = load_json(CATEGORY_SNAPSHOT_PATH) or {}
    previous_map = previous_snapshot.get("category_tickers", {})

    current_map = {}
    for category in DEFAULT_CATEGORIES:
        current_map[category] = [
            str(row.get("ticker") or "").upper()
            for row in summary["categories"].get(category, [])
            if row.get("ticker")
        ]
        previous = previous_map.get(category, [])
        current = current_map[category]
        entrants = [ticker for ticker in current if ticker not in previous]
        exits = [ticker for ticker in previous if ticker not in current]
        summary["shifts"][category] = {
            "entrants": entrants[:5],
            "exits": exits[:5],
            "unchanged_count": len([ticker for ticker in current if ticker in previous]),
        }

    save_json(
        CATEGORY_SNAPSHOT_PATH,
        {
            "snapshot_at": summary["snapshot_at"],
            "category_tickers": current_map,
        },
    )
    return summary


def load_recent_tech_news(lookback_hours, limit):
    path = Path(TECH_NEWS_OUTPUT_PATH)
    if not path.exists():
        return {"available": False, "error": "tech news feed file missing", "items": []}

    cutoff = now_utc() - timedelta(hours=max(1, lookback_hours))
    items = []
    try:
        with path.open("r", encoding="utf-8") as news_file:
            for line in news_file:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                published = parse_datetime(item.get("published_at") or item.get("fetched_at"))
                if published and published < cutoff:
                    continue
                item["_published_at"] = published.isoformat() if published else ""
                items.append(item)
    except Exception as exc:
        return {"available": False, "error": f"tech news read failed: {exc}", "items": []}

    items.sort(key=lambda row: row.get("_published_at", ""), reverse=True)
    unique = []
    seen = set()
    for item in items:
        key = item.get("id") or f"{item.get('ticker')}|{item.get('title')}|{item.get('published_at')}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= limit:
            break
    return {"available": True, "error": None, "items": unique}


def load_recent_ibkr_news(lookback_hours, limit):
    path = Path(IBKR_NEWS_OUTPUT_PATH)
    if not path.exists():
        return {"available": False, "error": "IBKR news feed file missing", "items": []}

    cutoff = now_utc() - timedelta(hours=max(1, lookback_hours))
    items = []
    try:
        with path.open("r", encoding="utf-8") as news_file:
            for line in news_file:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                published = parse_datetime(item.get("published_at") or item.get("fetched_at"))
                if published and published < cutoff:
                    continue
                item["_published_at"] = published.isoformat() if published else ""
                items.append(item)
    except Exception as exc:
        return {"available": False, "error": f"IBKR news read failed: {exc}", "items": []}

    items.sort(key=lambda row: row.get("_published_at", ""), reverse=True)
    unique = []
    seen = set()
    for item in items:
        key = item.get("id") or f"{item.get('ticker')}|{item.get('provider_code')}|{item.get('article_id')}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= limit:
            break
    return {"available": True, "error": None, "items": unique}


def fetch_open_signals(supabase, limit):
    payload = {"available": True, "error": None, "rows": [], "summary": {}}
    rows = []
    try:
        for status in ("pending", "approved"):
            response = (
                supabase.table("market_signals")
                .select("id,ticker,action_type,status,channel,confidence_score,created_at")
                .eq("status", status)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            rows.extend(response.data or [])
    except Exception as exc:
        payload.update({"available": False, "error": f"market_signals read failed: {exc}"})
        return payload

    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    rows = rows[:limit]
    payload["rows"] = rows
    payload["summary"] = {
        "status_counts": dict(Counter(row.get("status") or "unknown" for row in rows)),
        "action_counts": dict(Counter(row.get("action_type") or "unknown" for row in rows)),
        "channel_counts": dict(Counter(row.get("channel") or "unknown" for row in rows)),
    }
    return payload


def fetch_positions(supabase, limit):
    payload = {"available": True, "error": None, "rows": [], "summary": {}}
    try:
        response = (
            supabase.table("broker_positions")
            .select("account,ticker,quantity,avg_cost,market_price,market_value,unrealized_pnl,side,synced_at")
            .eq("is_open", True)
            .order("market_value", desc=True)
            .limit(limit)
            .execute()
        )
        rows = response.data or []
    except Exception as exc:
        if missing_table(exc, "broker_positions"):
            payload.update({"available": False, "error": "broker_positions table missing"})
            return payload
        payload.update({"available": False, "error": f"broker_positions read failed: {exc}"})
        return payload

    total_market_value = 0.0
    total_unrealized = 0.0
    for row in rows:
        total_market_value += safe_float(row.get("market_value")) or 0.0
        total_unrealized += safe_float(row.get("unrealized_pnl")) or 0.0

    payload["rows"] = rows
    payload["summary"] = {
        "open_positions": len(rows),
        "total_market_value": round(total_market_value, 2),
        "total_unrealized_pnl": round(total_unrealized, 2),
    }
    return payload


def read_macro_context():
    if not MACRO_STATE_PATH.exists():
        return {"available": False, "error": "macro state file missing", "text": "", "summary": {}}

    text = MACRO_STATE_PATH.read_text(encoding="utf-8", errors="replace")
    bullish = text.upper().count("BULLISH")
    bearish = text.upper().count("BEARISH")
    if bullish > bearish:
        bias = "risk_on"
    elif bearish > bullish:
        bias = "risk_off"
    else:
        bias = "mixed"

    return {
        "available": True,
        "error": None,
        "text": text[:8000],
        "summary": {
            "bullish_mentions": bullish,
            "bearish_mentions": bearish,
            "bias": bias,
        },
    }


def compact_category_section(category_context):
    compact = {}
    for category in DEFAULT_CATEGORIES:
        rows = category_context.get("categories", {}).get(category, [])
        compact[category] = [
            {
                "ticker": row.get("ticker"),
                "theme": row.get("theme"),
                "category_score": row.get("category_score"),
                "company_name": row.get("company_name"),
            }
            for row in rows[:10]
        ]
    return compact


def compact_news(news_items):
    compact = []
    for item in news_items:
        compact.append(
            {
                "ticker": item.get("ticker"),
                "title": item.get("title"),
                "published_at": item.get("published_at"),
                "tags": item.get("tags") or [],
                "llm_analysis": item.get("llm_analysis") or {},
            }
        )
    return compact


def compact_ibkr_news(news_items):
    compact = []
    for item in news_items:
        compact.append(
            {
                "ticker": item.get("ticker"),
                "headline": item.get("headline"),
                "published_at": item.get("published_at"),
                "provider_code": item.get("provider_code"),
                "action": item.get("action"),
                "confidence": item.get("confidence"),
                "signal_pushed": item.get("signal_pushed"),
                "signal_note": item.get("signal_note"),
            }
        )
    return compact


def build_ollama_prompt(session_type, briefing_date, context):
    return f"""
You are a concise portfolio strategist creating a {session_type} market briefing for a paper-trading desk.
Use only the provided data. Do not use external facts.

Cover:
1. Category universe shifts.
2. Tech news and IBKR catalyst highlights.
3. Open signal queue quality and risks.
4. Current positions.
5. Macro context.

Return strict JSON only:
{{
  "title": "string",
  "tone": "risk_on|risk_off|mixed",
  "topline": "1-2 sentence summary",
  "sections": {{
    "category_universe_shifts": ["bullet", "bullet"],
    "tech_news": ["bullet", "bullet"],
    "open_signals": ["bullet", "bullet"],
    "positions": ["bullet", "bullet"],
    "macro_context": ["bullet", "bullet"]
  }},
  "action_items": ["bullet", "bullet", "bullet"]
}}

Constraints:
- Be conservative and practical.
- No guarantees or hype language.
- If a data source is missing, mention it plainly.
- Keep each bullet under 25 words.

Briefing date: {briefing_date}
Session: {session_type}

Input data:
{json.dumps(context, ensure_ascii=True, indent=2)}
""".strip()


def normalize_briefing(raw, session_type, briefing_date):
    if not isinstance(raw, dict):
        raw = {}

    sections = raw.get("sections")
    if not isinstance(sections, dict):
        sections = {}

    normalized_sections = {}
    for section in ("category_universe_shifts", "tech_news", "open_signals", "positions", "macro_context"):
        value = sections.get(section, [])
        if not isinstance(value, list):
            value = [str(value)] if value else []
        normalized_sections[section] = [str(item).strip() for item in value if str(item).strip()]

    action_items = raw.get("action_items", [])
    if not isinstance(action_items, list):
        action_items = [str(action_items)] if action_items else []
    action_items = [str(item).strip() for item in action_items if str(item).strip()]

    tone = str(raw.get("tone") or "mixed").strip().lower()
    if tone not in {"risk_on", "risk_off", "mixed"}:
        tone = "mixed"

    return {
        "title": str(raw.get("title") or f"{session_type.title()} Market Briefing ({briefing_date})").strip(),
        "tone": tone,
        "topline": str(raw.get("topline") or "Data-driven market briefing generated from current scanner context.").strip(),
        "sections": normalized_sections,
        "action_items": action_items,
    }


def ask_ollama_for_briefing(session_type, briefing_date, context):
    prompt = build_ollama_prompt(session_type, briefing_date, context)
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        "think": False,
        "options": {"temperature": 0.1},
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
    try:
        if ollama_uses_chat_endpoint():
            response = requests.post(OLLAMA_URL, json=chat_payload, timeout=MARKET_BRIEFING_TIMEOUT_SECONDS)
        else:
            response = requests.post(OLLAMA_URL, json=payload, timeout=MARKET_BRIEFING_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        raw_json_string = extract_ollama_text(data)
        if not raw_json_string and not ollama_uses_chat_endpoint():
            chat_response = requests.post(ollama_chat_url(), json=chat_payload, timeout=MARKET_BRIEFING_TIMEOUT_SECONDS)
            chat_response.raise_for_status()
            chat_data = chat_response.json()
            raw_json_string = extract_ollama_text(chat_data)
        if not raw_json_string:
            raise RuntimeError("Ollama returned empty response content.")
        parsed = extract_json_object(raw_json_string)
        record_llm_metric(
            source="daily_market_briefing",
            task="market_briefing",
            model=OLLAMA_MODEL,
            duration_seconds=time.perf_counter() - started_at,
            success=True,
            endpoint=endpoint_label,
            item_count=1,
            batch_size=1,
            attempts=1,
            timeout_seconds=MARKET_BRIEFING_TIMEOUT_SECONDS,
            prompt_chars=len(prompt),
            response_chars=len(raw_json_string),
            extra={"session_type": session_type, "briefing_date": str(briefing_date)},
        )
        return normalize_briefing(parsed, session_type, briefing_date)
    except Exception as exc:
        record_llm_metric(
            source="daily_market_briefing",
            task="market_briefing",
            model=OLLAMA_MODEL,
            duration_seconds=time.perf_counter() - started_at,
            success=False,
            endpoint=endpoint_label,
            item_count=1,
            batch_size=1,
            attempts=1,
            timeout_seconds=MARKET_BRIEFING_TIMEOUT_SECONDS,
            prompt_chars=len(prompt),
            response_chars=len(raw_json_string),
            error=exc,
            extra={"session_type": session_type, "briefing_date": str(briefing_date)},
        )
        raise


def fallback_briefing(session_type, briefing_date, context, reason):
    category_lines = []
    for category in DEFAULT_CATEGORIES:
        shift = context["category_universe"]["shifts"].get(category, {})
        entrants = shift.get("entrants") or []
        exits = shift.get("exits") or []
        if entrants or exits:
            category_lines.append(
                f"{category}: entrants {', '.join(entrants[:3]) or 'none'}; exits {', '.join(exits[:3]) or 'none'}."
            )

    if not category_lines:
        category_lines.append("Category rankings were stable versus the prior snapshot.")

    news_lines = []
    for item in context["tech_news"]["highlights"][:5]:
        news_lines.append(f"{item.get('ticker')}: {item.get('title')}")
    for item in context.get("ibkr_news", {}).get("highlights", [])[:5]:
        news_lines.append(
            f"IBKR {item.get('ticker')}: {item.get('action')} {item.get('headline')}"
        )
    if not news_lines:
        news_lines.append("No recent tech or IBKR catalyst news items in the lookback window.")

    signal_summary = context["open_signals"]["summary"]
    open_signal_lines = [
        f"Pending: {signal_summary.get('status_counts', {}).get('pending', 0)}",
        f"Approved: {signal_summary.get('status_counts', {}).get('approved', 0)}",
    ]

    position_summary = context["positions"]["summary"]
    position_lines = [
        f"Open positions: {position_summary.get('open_positions', 0)}",
        f"Total market value: {position_summary.get('total_market_value', 0)}",
        f"Unrealized PnL: {position_summary.get('total_unrealized_pnl', 0)}",
    ]

    macro_summary = context["macro_context"]["summary"]
    macro_lines = [
        f"Macro bias: {macro_summary.get('bias', 'mixed')}",
        f"Bullish mentions: {macro_summary.get('bullish_mentions', 0)}",
        f"Bearish mentions: {macro_summary.get('bearish_mentions', 0)}",
    ]

    return {
        "title": f"{session_type.title()} Market Briefing ({briefing_date})",
        "tone": str(macro_summary.get("bias", "mixed")),
        "topline": f"Fallback briefing generated because Ollama analysis was unavailable: {reason}",
        "sections": {
            "category_universe_shifts": category_lines[:6],
            "tech_news": news_lines[:6],
            "open_signals": open_signal_lines,
            "positions": position_lines,
            "macro_context": macro_lines,
        },
        "action_items": [
            "Review top entrants for liquidity and spread quality before new signals.",
            "Prioritize open approved signals with the strongest supporting news.",
            "Re-run briefing after Ollama is online for richer narrative context.",
        ],
    }


def render_markdown(briefing, session_type, briefing_date):
    lines = [
        f"# {briefing['title']}",
        "",
        f"- Date: {briefing_date}",
        f"- Session: {session_type}",
        f"- Tone: {briefing['tone']}",
        "",
        f"## Topline",
        briefing["topline"],
        "",
    ]

    section_titles = {
        "category_universe_shifts": "Category Universe Shifts",
        "tech_news": "Tech News",
        "open_signals": "Open Signals",
        "positions": "Positions",
        "macro_context": "Macro Context",
    }

    for key in ("category_universe_shifts", "tech_news", "open_signals", "positions", "macro_context"):
        lines.append(f"## {section_titles[key]}")
        bullets = briefing["sections"].get(key) or ["No notable update."]
        for bullet in bullets:
            lines.append(f"- {bullet}")
        lines.append("")

    lines.append("## Action Items")
    action_items = briefing.get("action_items") or ["No immediate action item."]
    for item in action_items:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def append_jsonl(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output_file:
        output_file.write(json.dumps(record, ensure_ascii=True) + "\n")


def write_outputs(record, session_type):
    append_jsonl(DEFAULT_OUTPUT_PATH, record)

    latest_json = LATEST_MORNING_JSON if session_type == "morning" else LATEST_EVENING_JSON
    latest_md = LATEST_MORNING_MD if session_type == "morning" else LATEST_EVENING_MD
    latest_json.parent.mkdir(parents=True, exist_ok=True)

    latest_json.write_text(json.dumps(record, ensure_ascii=True, indent=2), encoding="utf-8")
    latest_md.write_text(record["briefing_markdown"], encoding="utf-8")

    print(f"Saved briefing JSONL to {DEFAULT_OUTPUT_PATH}.")
    print(f"Updated latest briefing JSON: {latest_json}")
    print(f"Updated latest briefing Markdown: {latest_md}")


def push_briefing_to_supabase(supabase, record):
    try:
        supabase.table("daily_market_briefings").upsert(
            [record],
            on_conflict="briefing_date,session_type",
        ).execute()
        print("Daily market briefing synced to Supabase.")
        return True
    except Exception as exc:
        if missing_table(exc, "daily_market_briefings"):
            print(BRIEFING_TABLE_SETUP_MESSAGE)
            return False
        print(f"Briefing Supabase sync failed: {exc}")
        return False


def build_source_summary(context):
    category_available = context["category_context"]["available"]
    news_available = context["tech_news"]["available"]
    ibkr_news_available = context.get("ibkr_news", {}).get("available", False)
    signals_available = context["open_signals"]["available"]
    positions_available = context["positions"]["available"]
    macro_available = context["macro_context"]["available"]

    return {
        "category_universe_available": category_available,
        "category_universe_rows": sum(
            len(context["category_context"]["categories"].get(category, []))
            for category in DEFAULT_CATEGORIES
        ) if category_available else 0,
        "tech_news_available": news_available,
        "tech_news_items": len(context["tech_news"]["items"]) if news_available else 0,
        "ibkr_news_available": ibkr_news_available,
        "ibkr_news_items": len(context.get("ibkr_news", {}).get("items", [])) if ibkr_news_available else 0,
        "open_signals_available": signals_available,
        "open_signals_items": len(context["open_signals"]["rows"]) if signals_available else 0,
        "positions_available": positions_available,
        "positions_items": len(context["positions"]["rows"]) if positions_available else 0,
        "macro_available": macro_available,
    }


def run_daily_market_briefing(
    session="auto",
    lookback_hours=MARKET_BRIEFING_LOOKBACK_HOURS,
    news_limit=MARKET_BRIEFING_NEWS_LIMIT,
    signal_limit=MARKET_BRIEFING_SIGNAL_LIMIT,
    position_limit=MARKET_BRIEFING_POSITION_LIMIT,
    category_limit=MARKET_BRIEFING_CATEGORY_LIMIT,
    push_supabase=MARKET_BRIEFING_PUSH_SUPABASE,
    dry_run=False,
):
    session_type = resolve_session(session)
    briefing_date = local_briefing_date()
    supabase = get_supabase_client()

    print(f"Generating {session_type} market briefing for {briefing_date}...")
    category_context = get_category_universe_context(supabase, category_limit)
    tech_news = load_recent_tech_news(lookback_hours, news_limit)
    ibkr_news = load_recent_ibkr_news(lookback_hours, news_limit)
    open_signals = fetch_open_signals(supabase, signal_limit)
    positions = fetch_positions(supabase, position_limit)
    macro_context = read_macro_context()

    llm_context = {
        "briefing_date": briefing_date,
        "session_type": session_type,
        "category_universe": {
            "available": category_context["available"],
            "error": category_context["error"],
            "top_candidates": compact_category_section(category_context),
            "shifts": category_context["shifts"],
        },
        "tech_news": {
            "available": tech_news["available"],
            "error": tech_news["error"],
            "highlights": compact_news(tech_news["items"]),
        },
        "ibkr_news": {
            "available": ibkr_news["available"],
            "error": ibkr_news["error"],
            "highlights": compact_ibkr_news(ibkr_news["items"]),
        },
        "open_signals": {
            "available": open_signals["available"],
            "error": open_signals["error"],
            "summary": open_signals["summary"],
            "top_rows": open_signals["rows"][:10],
        },
        "positions": {
            "available": positions["available"],
            "error": positions["error"],
            "summary": positions["summary"],
            "top_rows": positions["rows"][:10],
        },
        "macro_context": {
            "available": macro_context["available"],
            "error": macro_context["error"],
            "summary": macro_context["summary"],
            "text_excerpt": macro_context["text"][:2500],
        },
    }

    try:
        briefing = ask_ollama_for_briefing(session_type, briefing_date, llm_context)
    except Exception as exc:
        print(f"Ollama briefing generation failed: {exc}")
        briefing = fallback_briefing(session_type, briefing_date, llm_context, str(exc))
    briefing_markdown = render_markdown(briefing, session_type, briefing_date)

    source_summary = build_source_summary(
        {
            "category_context": category_context,
            "tech_news": tech_news,
            "ibkr_news": ibkr_news,
            "open_signals": open_signals,
            "positions": positions,
            "macro_context": macro_context,
        }
    )

    timestamp = utc_now_iso()
    record = {
        "briefing_date": briefing_date,
        "session_type": session_type,
        "title": briefing["title"],
        "tone": briefing["tone"],
        "topline": briefing["topline"],
        "briefing_markdown": briefing_markdown,
        "briefing_payload": briefing,
        "source_summary": source_summary,
        "generated_at": timestamp,
        "updated_at": timestamp,
    }

    write_outputs(record, session_type)
    print("\n--- DAILY MARKET BRIEFING ---")
    print(briefing_markdown)

    if dry_run:
        print("DRY RUN: Supabase briefing sync skipped.")
        return True

    if not push_supabase:
        print("Supabase briefing sync disabled by configuration/flag.")
        return True

    return push_briefing_to_supabase(supabase, record)


def parse_args():
    parser = argparse.ArgumentParser(description="Generate a morning/evening Qwen market briefing for the iPad.")
    parser.add_argument("--session", choices=["auto", "morning", "evening"], default="auto")
    parser.add_argument("--lookback-hours", type=int, default=MARKET_BRIEFING_LOOKBACK_HOURS)
    parser.add_argument("--news-limit", type=int, default=MARKET_BRIEFING_NEWS_LIMIT)
    parser.add_argument("--signal-limit", type=int, default=MARKET_BRIEFING_SIGNAL_LIMIT)
    parser.add_argument("--position-limit", type=int, default=MARKET_BRIEFING_POSITION_LIMIT)
    parser.add_argument("--category-limit", type=int, default=MARKET_BRIEFING_CATEGORY_LIMIT)
    parser.add_argument("--no-push", action="store_true", help="Skip Supabase sync.")
    parser.add_argument("--dry-run", action="store_true", help="Generate briefing but do not write Supabase.")
    return parser.parse_args()


def main():
    args = parse_args()
    success = run_daily_market_briefing(
        session=args.session,
        lookback_hours=args.lookback_hours,
        news_limit=args.news_limit,
        signal_limit=args.signal_limit,
        position_limit=args.position_limit,
        category_limit=args.category_limit,
        push_supabase=not args.no_push,
        dry_run=args.dry_run,
    )
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
