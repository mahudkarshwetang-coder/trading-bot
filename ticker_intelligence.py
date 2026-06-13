import argparse
import html
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser
import requests
import yfinance as yf

from config import (
    IBKR_NEWS_OUTPUT_PATH,
    SIGNAL_CONTEXT_LIMIT,
    TECH_NEWS_OUTPUT_PATH,
    TECH_NEWS_TICKERS,
    TICKER_INTEL_CHATTER_LIMIT,
    TICKER_INTEL_NEWS_LIMIT,
    TICKER_INTEL_OUTPUT_PATH,
    TICKER_INTEL_PUSH_SUPABASE,
    TICKER_INTEL_SIGNAL_LOOKBACK_DAYS,
    TICKER_INTEL_WATCHLIST_LIMIT,
    get_supabase_client,
)
from performance_governor import print_compute_notice

YAHOO_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
REDDIT_RSS_URL = "https://www.reddit.com/search.rss?q={query}&sort=new&t=week"
REDDIT_SUBREDDITS = ("stocks", "wallstreetbets", "investing", "options", "SecurityAnalysis")
SEC_HEADERS = {
    "User-Agent": "SignalCenter trading bot contact@example.com",
    "Accept-Encoding": "gzip, deflate",
}

BULLISH_WORDS = (
    "surge",
    "beat",
    "growth",
    "upgrade",
    "jump",
    "record",
    "soar",
    "buy",
    "outperform",
    "bullish",
    "strong",
    "expands",
    "raises",
)
BEARISH_WORDS = (
    "miss",
    "decline",
    "drop",
    "downgrade",
    "fall",
    "plunge",
    "sell",
    "underperform",
    "lawsuit",
    "bearish",
    "cuts",
    "warning",
    "weak",
)

SETUP_MESSAGE = """
Supabase is missing the public.ticker_intelligence table.

Fix:
  1. Open Supabase Dashboard -> SQL Editor.
  2. Run the SQL in supabase/ticker_intelligence.sql from this repo.
  3. Re-run: python ticker_intelligence.py --ticker NVDA
"""


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean_float(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def clamp(value, minimum, maximum):
    if value is None:
        return None
    return max(minimum, min(maximum, value))


def parse_tickers(value):
    raw = str(value or "")
    tickers = []
    seen = set()
    for item in raw.replace("\n", ",").split(","):
        ticker = item.strip().upper()
        if ticker and ticker not in seen:
            tickers.append(ticker)
            seen.add(ticker)
    return tickers


def normalize_ticker(value):
    ticker = str(value or "").strip().upper()
    if not ticker:
        return ""
    return re.sub(r"[^A-Z0-9.\-]", "", ticker)


def ordered_unique(items):
    output = []
    seen = set()
    for item in items:
        if item and item not in seen:
            output.append(item)
            seen.add(item)
    return output


def safe_get_supabase_client(required):
    try:
        return get_supabase_client()
    except Exception as exc:
        if required:
            raise
        print(f"Supabase client unavailable; continuing without DB features: {exc}")
        return None


def missing_intelligence_table(exc):
    message = str(exc)
    return (
        "ticker_intelligence" in message
        and (
            "PGRST205" in message
            or "schema cache" in message
            or "Could not find the table" in message
        )
    )


def strip_html(text):
    value = html.unescape(str(text or ""))
    return re.sub(r"<[^>]+>", " ", value).strip()


def score_sentiment(text):
    lowered = str(text or "").lower()
    if not lowered:
        return 0.0
    bullish = sum(lowered.count(word) for word in BULLISH_WORDS)
    bearish = sum(lowered.count(word) for word in BEARISH_WORDS)
    total = bullish + bearish
    if total == 0:
        return 0.0
    return (bullish - bearish) / total


def sentiment_label(score):
    score = clean_float(score)
    if score is None:
        return "unknown"
    if score >= 0.25:
        return "bullish"
    if score <= -0.25:
        return "bearish"
    return "neutral"


def parse_date_iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date().isoformat()
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).date().isoformat()
        except Exception:
            return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return None


def parse_entry_timestamp(entry):
    raw = entry.get("published") or entry.get("updated")
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except Exception:
            pass
    published = entry.get("published_parsed")
    if published:
        try:
            return datetime(*published[:6], tzinfo=timezone.utc).isoformat()
        except Exception:
            pass
    return utc_now_iso()


def get_fast_info_value(fast_info, key):
    if fast_info is None:
        return None
    try:
        value = getattr(fast_info, key)
        if value is not None:
            return value
    except Exception:
        pass
    try:
        return fast_info.get(key)
    except Exception:
        return None


def compute_rsi_14(history):
    if history is None or history.empty or "Close" not in history:
        return None
    close = history["Close"].astype(float)
    if len(close) < 15:
        return None
    delta = close.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = clean_float(gains.rolling(14).mean().iloc[-1])
    avg_loss = clean_float(losses.rolling(14).mean().iloc[-1])
    if avg_gain is None or avg_loss is None:
        return None
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return clean_float(100 - (100 / (1 + rs)))


def extract_company_aliases(info):
    aliases = []
    name_candidates = [
        str(info.get("shortName") or "").strip(),
        str(info.get("longName") or "").strip(),
    ]
    for name in name_candidates:
        if not name:
            continue
        clean_name = re.sub(r"[^A-Za-z0-9\s&.-]", " ", name)
        clean_name = re.sub(r"\s+", " ", clean_name).strip()
        if clean_name:
            aliases.append(clean_name)
            tokens = [token for token in clean_name.split() if len(token) > 2]
            if tokens:
                aliases.append(tokens[0])
                if len(tokens) >= 2:
                    aliases.append(f"{tokens[0]} {tokens[1]}")
    return ordered_unique(aliases)


def fetch_market_news(ticker, limit):
    feed = feedparser.parse(YAHOO_RSS_URL.format(ticker=ticker))
    items = []
    scores = []
    for entry in (feed.entries or [])[: max(1, int(limit))]:
        title = str(entry.get("title") or "").strip()
        summary = strip_html(entry.get("summary") or entry.get("description") or "")
        source = entry.get("source")
        if isinstance(source, dict):
            source = source.get("title")
        score = score_sentiment(f"{title} {summary}".strip())
        scores.append(score)
        items.append(
            {
                "title": title,
                "summary": summary[:350],
                "link": str(entry.get("link") or "").strip(),
                "source": str(source or "").strip(),
                "published_at": parse_entry_timestamp(entry),
                "sentiment_score": round(score, 4),
                "sentiment_label": sentiment_label(score),
            }
        )
    avg = sum(scores) / len(scores) if scores else 0.0
    return items, clamp(clean_float(avg), -1.0, 1.0)


def fetch_public_chatter(ticker, aliases, limit):
    query_terms = [f'"{ticker}"', f'"${ticker}"']
    for alias in aliases[:3]:
        alias_clean = str(alias).strip()
        if 2 <= len(alias_clean) <= 30:
            query_terms.append(f'"{alias_clean}"')
    subreddit_filter = " OR ".join([f"subreddit:{name}" for name in REDDIT_SUBREDDITS])
    query = f"({' OR '.join(query_terms)}) ({subreddit_filter})"
    url = REDDIT_RSS_URL.format(query=quote_plus(query))
    feed = feedparser.parse(url)

    ticker_regex = re.compile(rf"(\${re.escape(ticker)}\b|\b{re.escape(ticker)}\b)", flags=re.IGNORECASE)
    alias_regexes = [
        re.compile(rf"\b{re.escape(str(alias).strip())}\b", flags=re.IGNORECASE)
        for alias in aliases[:3]
        if str(alias).strip()
    ]
    items = []
    scores = []
    for entry in (feed.entries or []):
        title = str(entry.get("title") or "").strip()
        summary = strip_html(entry.get("summary") or entry.get("description") or "")
        blob = f"{title} {summary}"
        if not ticker_regex.search(blob):
            alias_match = any(regex.search(blob) for regex in alias_regexes)
            if not alias_match:
                continue
        score = score_sentiment(blob)
        scores.append(score)
        items.append(
            {
                "title": title,
                "summary": summary[:320],
                "link": str(entry.get("link") or "").strip(),
                "author": str(entry.get("author") or "").strip(),
                "published_at": parse_entry_timestamp(entry),
                "sentiment_score": round(score, 4),
                "sentiment_label": sentiment_label(score),
            }
        )
        if len(items) >= max(1, int(limit)):
            break

    avg = sum(scores) / len(scores) if scores else 0.0
    return items, clamp(clean_float(avg), -1.0, 1.0)


def llm_sentiment_to_score(value):
    label = str(value or "").strip().lower()
    if label in {"bullish", "positive"}:
        return 1.0
    if label in {"bearish", "negative"}:
        return -1.0
    return 0.0


def load_tech_news_context(ticker, limit):
    path = TECH_NEWS_OUTPUT_PATH
    if not path or not os.path.exists(path):
        return [], 0.0

    matches = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                related = [str(item).upper() for item in (payload.get("tickers") or []) if str(item).strip()]
                primary = str(payload.get("ticker") or "").upper()
                if ticker != primary and ticker not in related:
                    continue

                llm = payload.get("llm_analysis") or {}
                matches.append(
                    {
                        "title": str(payload.get("title") or "").strip(),
                        "link": str(payload.get("link") or "").strip(),
                        "published_at": str(payload.get("published_at") or "").strip(),
                        "fetched_at": str(payload.get("fetched_at") or "").strip(),
                        "tags": payload.get("tags") or [],
                        "sentiment": str(llm.get("sentiment") or "neutral").lower(),
                        "impact_score": clean_float(llm.get("impact_score")),
                        "urgency_score": clean_float(llm.get("urgency_score")),
                        "summary": str(llm.get("summary") or "").strip(),
                        "why_it_matters": str(llm.get("why_it_matters") or "").strip(),
                        "suggested_action": str(llm.get("suggested_action") or "watch").strip(),
                    }
                )
    except Exception as exc:
        print(f"Tech news context load skipped for {ticker}: {exc}")
        return [], 0.0

    matches.sort(key=lambda item: (item.get("published_at") or "", item.get("fetched_at") or ""), reverse=True)
    limited = matches[: max(1, int(limit))]
    if not limited:
        return [], 0.0
    limited_scores = [llm_sentiment_to_score(item.get("sentiment")) for item in limited]
    avg = sum(limited_scores) / len(limited_scores)
    return limited, clamp(clean_float(avg), -1.0, 1.0)


def load_ibkr_news_context(ticker, limit):
    path = IBKR_NEWS_OUTPUT_PATH
    if not path or not os.path.exists(path):
        return [], 0.0

    matches = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                if ticker != str(payload.get("ticker") or "").upper():
                    continue
                matches.append(
                    {
                        "headline": str(payload.get("headline") or "").strip(),
                        "published_at": str(payload.get("published_at") or "").strip(),
                        "provider_code": str(payload.get("provider_code") or "").strip(),
                        "provider_name": str(payload.get("provider_name") or "").strip(),
                        "action": str(payload.get("action") or "WATCH").upper(),
                        "confidence": clean_float(payload.get("confidence")),
                        "signal_pushed": bool(payload.get("signal_pushed")),
                        "signal_note": str(payload.get("signal_note") or "").strip(),
                    }
                )
    except Exception as exc:
        print(f"IBKR news context load skipped for {ticker}: {exc}")
        return [], 0.0

    matches.sort(key=lambda item: item.get("published_at") or "", reverse=True)
    limited = matches[: max(1, int(limit))]
    if not limited:
        return [], 0.0

    scores = []
    for item in limited:
        confidence = (clean_float(item.get("confidence")) or 50.0) / 100.0
        if item.get("action") == "BUY":
            scores.append(confidence)
        elif item.get("action") == "SELL":
            scores.append(-confidence)
        else:
            scores.append(0.0)
    avg = sum(scores) / len(scores)
    return limited, clamp(clean_float(avg), -1.0, 1.0)


def fetch_short_interest(info, current_price):
    shares_short = clean_float(info.get("sharesShort"))
    short_ratio = clean_float(info.get("shortRatio"))
    short_pct_float = clean_float(info.get("shortPercentOfFloat"))
    short_pct_outstanding = clean_float(info.get("sharesPercentSharesOut"))
    short_date = parse_date_iso(info.get("dateShortInterest"))

    parts = []
    if shares_short is not None:
        parts.append(f"{shares_short:,.0f} shares short")
    if short_pct_float is not None:
        pct = short_pct_float * 100 if short_pct_float <= 1 else short_pct_float
        parts.append(f"{pct:.2f}% of float")
    if short_ratio is not None:
        parts.append(f"{short_ratio:.2f} days-to-cover")
    if current_price is not None and shares_short is not None:
        parts.append(f"${shares_short * current_price:,.0f} short interest notional")
    summary = " | ".join(parts) if parts else "Short interest snapshot unavailable."

    return {
        "short_shares": shares_short,
        "short_ratio": short_ratio,
        "short_pct_float": short_pct_float,
        "short_pct_outstanding": short_pct_outstanding,
        "short_interest_as_of": short_date,
        "short_interest_summary": summary,
    }


def fetch_options_sentiment(stock):
    try:
        expiries = list(stock.options or [])
        if not expiries:
            return {
                "options_expiry": None,
                "put_call_oi_ratio": None,
                "put_call_volume_ratio": None,
                "options_sentiment": "unavailable",
            }
        expiry = expiries[0]
        chain = stock.option_chain(expiry)
        put_oi = clean_float(chain.puts["openInterest"].fillna(0).sum())
        call_oi = clean_float(chain.calls["openInterest"].fillna(0).sum())
        put_volume = clean_float(chain.puts["volume"].fillna(0).sum())
        call_volume = clean_float(chain.calls["volume"].fillna(0).sum())

        oi_ratio = None
        if call_oi and call_oi > 0:
            oi_ratio = put_oi / call_oi
        volume_ratio = None
        if call_volume and call_volume > 0:
            volume_ratio = put_volume / call_volume

        sentiment = "neutral"
        active_ratio = oi_ratio if oi_ratio is not None else volume_ratio
        if active_ratio is not None:
            if active_ratio >= 1.2:
                sentiment = "bearish"
            elif active_ratio <= 0.8:
                sentiment = "bullish"

        return {
            "options_expiry": parse_date_iso(expiry),
            "put_call_oi_ratio": clean_float(oi_ratio),
            "put_call_volume_ratio": clean_float(volume_ratio),
            "options_sentiment": sentiment,
        }
    except Exception as exc:
        symbol = getattr(stock, "ticker", "unknown")
        print(f"Options chain skipped for {symbol}: {exc}")
        return {
            "options_expiry": None,
            "put_call_oi_ratio": None,
            "put_call_volume_ratio": None,
            "options_sentiment": "unavailable",
        }


def fetch_signal_summary(supabase, ticker, lookback_days):
    empty = {
        "rows": [],
        "signal_count": 0,
        "buy_count": 0,
        "sell_count": 0,
        "bias_score": 0.0,
        "channels": {},
        "status_breakdown": {},
    }
    if supabase is None:
        return empty

    since = (datetime.now(timezone.utc) - timedelta(days=max(1, int(lookback_days)))).isoformat()
    try:
        response = (
            supabase.table("market_signals")
            .select("action_type,channel,status,confidence_score,created_at")
            .eq("ticker", ticker)
            .gte("created_at", since)
            .order("created_at", desc=True)
            .limit(max(40, int(SIGNAL_CONTEXT_LIMIT)))
            .execute()
        )
        rows = response.data or []
    except Exception as exc:
        print(f"Signal summary skipped for {ticker}: {exc}")
        return empty

    buy_count = 0
    sell_count = 0
    channels = {}
    statuses = {}
    for row in rows:
        action = str(row.get("action_type") or "").upper()
        if action == "BUY":
            buy_count += 1
        elif action == "SELL":
            sell_count += 1

        channel = str(row.get("channel") or "UNKNOWN").upper()
        channels[channel] = channels.get(channel, 0) + 1

        status = str(row.get("status") or "unknown").lower()
        statuses[status] = statuses.get(status, 0) + 1

    total = buy_count + sell_count
    bias = 0.0 if total == 0 else (buy_count - sell_count) / total
    return {
        "rows": rows[:10],
        "signal_count": len(rows),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "bias_score": clamp(clean_float(bias), -1.0, 1.0) or 0.0,
        "channels": channels,
        "status_breakdown": statuses,
    }


def fetch_sec_ticker_map():
    try:
        response = requests.get("https://www.sec.gov/files/company_tickers.json", headers=SEC_HEADERS, timeout=12)
        response.raise_for_status()
        payload = response.json()
        return {item["ticker"].upper(): str(item["cik_str"]).zfill(10) for item in payload.values()}
    except Exception as exc:
        print(f"SEC ticker map unavailable: {exc}")
        return {}


def fetch_sec_snapshot(ticker, ticker_map, sec_cache):
    ticker = ticker.upper()
    if ticker in sec_cache:
        return sec_cache[ticker]

    cik = ticker_map.get(ticker)
    if not cik:
        snapshot = {
            "latest_filing_type": None,
            "latest_filing_date": None,
            "latest_filing_title": None,
            "latest_filing_url": None,
            "risk_flags": ["SEC ticker mapping unavailable"],
            "risk_score": 0.15,
        }
        sec_cache[ticker] = snapshot
        return snapshot

    try:
        response = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers=SEC_HEADERS,
            timeout=12,
        )
        response.raise_for_status()
        recent = response.json().get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        docs = recent.get("primaryDocument", [])
        titles = recent.get("primaryDocDescription", [])

        watched_forms = {"8-K", "10-Q", "10-K", "S-1", "S-3", "424B5", "424B3", "SC 13G", "SC 13D", "4"}
        latest = None
        for idx, form in enumerate(forms):
            if form not in watched_forms:
                continue
            accession = str(accessions[idx]).replace("-", "")
            document = str(docs[idx] or "")
            latest = {
                "latest_filing_type": form,
                "latest_filing_date": parse_date_iso(dates[idx]),
                "latest_filing_title": str(titles[idx] or form),
                "latest_filing_url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{document}",
            }
            break

        risk_flags = []
        risk_score = 0.0
        if latest:
            filing_type = latest["latest_filing_type"]
            title_lower = str(latest["latest_filing_title"] or "").lower()
            if filing_type in {"S-1", "S-3", "424B5", "424B3"}:
                risk_flags.append("Potential dilution/financing filing")
                risk_score += 0.45
            if filing_type == "8-K":
                risk_flags.append("Recent catalyst filing")
                risk_score += 0.12
            if "going concern" in title_lower:
                risk_flags.append("Going concern language")
                risk_score += 0.5
            if filing_type == "4":
                risk_flags.append("Recent insider transaction filing")
                risk_score += 0.12

        snapshot = {
            "latest_filing_type": latest["latest_filing_type"] if latest else None,
            "latest_filing_date": latest["latest_filing_date"] if latest else None,
            "latest_filing_title": latest["latest_filing_title"] if latest else None,
            "latest_filing_url": latest["latest_filing_url"] if latest else None,
            "risk_flags": risk_flags,
            "risk_score": clamp(clean_float(risk_score), 0.0, 1.0) or 0.0,
        }
        sec_cache[ticker] = snapshot
        return snapshot
    except Exception as exc:
        print(f"SEC snapshot skipped for {ticker}: {exc}")
        snapshot = {
            "latest_filing_type": None,
            "latest_filing_date": None,
            "latest_filing_title": None,
            "latest_filing_url": None,
            "risk_flags": ["SEC fetch failed"],
            "risk_score": 0.2,
        }
        sec_cache[ticker] = snapshot
        return snapshot


def fetch_market_snapshot(ticker):
    stock = yf.Ticker(ticker)
    info = {}
    try:
        info = stock.get_info() or {}
    except Exception:
        try:
            info = stock.info or {}
        except Exception:
            info = {}

    fast = getattr(stock, "fast_info", None)
    history = stock.history(period="3mo", interval="1d")

    price = clean_float(get_fast_info_value(fast, "last_price"))
    previous_close = clean_float(get_fast_info_value(fast, "previous_close"))
    volume = clean_float(get_fast_info_value(fast, "last_volume"))

    if history is not None and not history.empty:
        if price is None:
            price = clean_float(history["Close"].iloc[-1])
        if previous_close is None and len(history) >= 2:
            previous_close = clean_float(history["Close"].iloc[-2])
        if volume is None:
            volume = clean_float(history["Volume"].iloc[-1])

    change_pct = None
    if price is not None and previous_close not in (None, 0):
        change_pct = ((price / previous_close) - 1.0) * 100.0

    return_1d = None
    return_1m = None
    avg_volume = clean_float(info.get("averageVolume")) or clean_float(info.get("averageDailyVolume10Day"))
    if history is not None and not history.empty:
        if len(history) >= 2:
            start = clean_float(history["Close"].iloc[-2])
            end = clean_float(history["Close"].iloc[-1])
            if start not in (None, 0) and end is not None:
                return_1d = ((end / start) - 1.0) * 100.0
        if len(history) >= 22:
            start = clean_float(history["Close"].iloc[-22])
            end = clean_float(history["Close"].iloc[-1])
            if start not in (None, 0) and end is not None:
                return_1m = ((end / start) - 1.0) * 100.0
        if avg_volume is None and len(history) >= 20:
            avg_volume = clean_float(history["Volume"].tail(20).mean())

    snapshot = {
        "ticker": ticker,
        "company_name": str(info.get("shortName") or info.get("longName") or ticker),
        "exchange": str(info.get("exchange") or get_fast_info_value(fast, "exchange") or "").strip(),
        "sector": str(info.get("sector") or "").strip(),
        "industry": str(info.get("industry") or "").strip(),
        "market_cap": clean_float(info.get("marketCap")),
        "quote_price": price,
        "quote_change_pct": clean_float(change_pct),
        "volume": volume,
        "avg_volume": avg_volume,
        "beta": clean_float(info.get("beta")),
        "trailing_pe": clean_float(info.get("trailingPE")),
        "forward_pe": clean_float(info.get("forwardPE")),
        "rsi_14": compute_rsi_14(history),
        "return_1d_pct": clean_float(return_1d),
        "return_1m_pct": clean_float(return_1m),
    }
    return snapshot, stock, info


def compute_overall_sentiment(news_score, chatter_score, tech_score, ibkr_score, signal_bias):
    components = []
    for value, weight in (
        (news_score, 0.30),
        (chatter_score, 0.15),
        (tech_score, 0.20),
        (ibkr_score, 0.20),
        (signal_bias, 0.15),
    ):
        numeric = clean_float(value)
        if numeric is None:
            continue
        components.append((numeric, weight))

    if not components:
        return 0.0

    weighted_sum = sum(value * weight for value, weight in components)
    total_weight = sum(weight for _, weight in components)
    if total_weight <= 0:
        return 0.0
    return clamp(weighted_sum / total_weight, -1.0, 1.0) or 0.0


def build_row(ticker, supabase, sec_ticker_map, sec_cache, news_limit, chatter_limit, signal_lookback_days):
    snapshot, stock, info = fetch_market_snapshot(ticker)
    aliases = extract_company_aliases(info)

    short_interest = fetch_short_interest(info, snapshot.get("quote_price"))
    options = fetch_options_sentiment(stock)
    market_news_items, market_news_score = fetch_market_news(ticker, news_limit)
    chatter_items, chatter_score = fetch_public_chatter(ticker, aliases, chatter_limit)
    tech_items, tech_score = load_tech_news_context(ticker, limit=max(news_limit, 6))
    ibkr_items, ibkr_score = load_ibkr_news_context(ticker, limit=max(news_limit, 6))
    signal_summary = fetch_signal_summary(supabase, ticker, signal_lookback_days)
    sec_snapshot = fetch_sec_snapshot(ticker, sec_ticker_map, sec_cache)

    overall_score = compute_overall_sentiment(
        market_news_score,
        chatter_score,
        tech_score,
        ibkr_score,
        signal_summary.get("bias_score"),
    )
    generated_at = utc_now_iso()

    row = {
        "ticker": ticker,
        **snapshot,
        **short_interest,
        **options,
        "market_news_sentiment": market_news_score,
        "market_news_count": len(market_news_items),
        "market_news_items": market_news_items,
        "public_chatter_sentiment": chatter_score,
        "public_chatter_count": len(chatter_items),
        "public_chatter_items": chatter_items,
        "tech_news_sentiment": tech_score,
        "tech_news_count": len(tech_items),
        "tech_news_items": tech_items,
        "ibkr_news_sentiment": ibkr_score,
        "ibkr_news_count": len(ibkr_items),
        "ibkr_news_items": ibkr_items,
        "signal_bias_score": signal_summary.get("bias_score"),
        "signal_count": signal_summary.get("signal_count"),
        "signal_summary": {
            "buy_count": signal_summary.get("buy_count"),
            "sell_count": signal_summary.get("sell_count"),
            "channels": signal_summary.get("channels"),
            "status_breakdown": signal_summary.get("status_breakdown"),
            "recent_rows": signal_summary.get("rows"),
        },
        "sec_snapshot": sec_snapshot,
        "overall_sentiment_score": overall_score,
        "overall_sentiment_label": sentiment_label(overall_score),
        "source_summary": {
            "quote": "yfinance",
            "market_news": "yahoo_rss",
            "public_chatter": "reddit_rss",
            "tech_news_context": TECH_NEWS_OUTPUT_PATH,
            "ibkr_news_context": IBKR_NEWS_OUTPUT_PATH,
            "signals": "supabase.market_signals" if supabase is not None else "unavailable",
            "sec": "sec.gov",
        },
        "generated_at": generated_at,
        "updated_at": generated_at,
    }
    return row


def ensure_output_dir(path):
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)


def append_output_rows(rows, path):
    if not path:
        return
    ensure_output_dir(path)
    with open(path, "a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True))
            handle.write("\n")


def load_watchlist_tickers(supabase, limit):
    tickers = []
    tickers.extend(parse_tickers(TECH_NEWS_TICKERS))

    try:
        with open("daily_targets.txt", "r", encoding="utf-8") as target_file:
            tickers.extend(parse_tickers(target_file.read()))
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"daily_targets.txt read skipped: {exc}")

    if supabase is not None:
        try:
            response = (
                supabase.table("bot_settings")
                .select("watchlist,radar_watchlist,earnings_watchlist")
                .eq("id", 1)
                .execute()
            )
            if response.data:
                config = response.data[0]
                for key in ("watchlist", "radar_watchlist", "earnings_watchlist"):
                    tickers.extend([str(item).upper() for item in (config.get(key) or []) if str(item).strip()])
        except Exception as exc:
            print(f"Supabase watchlist load skipped: {exc}")

        try:
            response = (
                supabase.table("live_holdings")
                .select("ticker")
                .eq("active", True)
                .execute()
            )
            tickers.extend([str(row.get("ticker", "")).upper() for row in (response.data or []) if str(row.get("ticker", "")).strip()])
        except Exception as exc:
            print(f"Live holdings load skipped: {exc}")

    normalized = [normalize_ticker(ticker) for ticker in tickers]
    normalized = [ticker for ticker in normalized if ticker]
    return ordered_unique(normalized)[: max(1, int(limit))]


def sync_ticker_intelligence(
    tickers=None,
    news_limit=TICKER_INTEL_NEWS_LIMIT,
    chatter_limit=TICKER_INTEL_CHATTER_LIMIT,
    signal_lookback_days=TICKER_INTEL_SIGNAL_LOOKBACK_DAYS,
    watchlist_limit=TICKER_INTEL_WATCHLIST_LIMIT,
    dry_run=False,
    push_supabase=TICKER_INTEL_PUSH_SUPABASE,
):
    print_compute_notice(
        "ticker_intelligence",
        "ticker intelligence dossier sync",
        prefix="[TICKER INTEL]",
    )
    supabase = safe_get_supabase_client(required=bool(push_supabase))
    requested = []
    for ticker in tickers or []:
        normalized = normalize_ticker(ticker)
        if normalized:
            requested.append(normalized)

    if not requested:
        requested = load_watchlist_tickers(supabase, watchlist_limit)

    requested = ordered_unique(requested)
    if not requested:
        print("No tickers provided and no watchlist tickers found.")
        return []

    print(f"Building ticker intelligence for {len(requested)} ticker(s): {', '.join(requested)}")
    sec_ticker_map = fetch_sec_ticker_map()
    sec_cache = {}
    rows = []
    for ticker in requested:
        try:
            row = build_row(
                ticker=ticker,
                supabase=supabase,
                sec_ticker_map=sec_ticker_map,
                sec_cache=sec_cache,
                news_limit=news_limit,
                chatter_limit=chatter_limit,
                signal_lookback_days=signal_lookback_days,
            )
            rows.append(row)
            print(
                f"  {ticker}: sentiment={row['overall_sentiment_label']} "
                f"({row['overall_sentiment_score']:.2f}) "
                f"news={row['market_news_count']} chatter={row['public_chatter_count']} "
                f"short_ratio={row.get('short_ratio')}"
            )
        except Exception as exc:
            print(f"Ticker intelligence failed for {ticker}: {exc}")

    if not rows:
        print("No ticker intelligence rows were produced.")
        return rows

    append_output_rows(rows, TICKER_INTEL_OUTPUT_PATH)

    if dry_run:
        print("DRY RUN: Supabase was not updated.")
        return rows

    if push_supabase:
        if supabase is None:
            raise RuntimeError("SUPABASE_URL/SUPABASE_KEY are required when TICKER_INTEL_PUSH_SUPABASE=true.")
        try:
            supabase.table("ticker_intelligence").upsert(rows, on_conflict="ticker").execute()
        except Exception as exc:
            if missing_intelligence_table(exc):
                raise RuntimeError(SETUP_MESSAGE) from exc
            raise
        print("Ticker intelligence synced to Supabase.")
    else:
        print("Supabase push disabled by configuration.")
    return rows


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build ticker intelligence snapshots for iPad search (news, chatter, short interest, context)."
    )
    parser.add_argument("--ticker", action="append", default=[], help="Ticker symbol. Repeat for multiple tickers.")
    parser.add_argument("--tickers", default="", help="Comma-separated ticker symbols.")
    parser.add_argument("--limit-news", type=int, default=TICKER_INTEL_NEWS_LIMIT)
    parser.add_argument("--limit-chatter", type=int, default=TICKER_INTEL_CHATTER_LIMIT)
    parser.add_argument("--signal-lookback-days", type=int, default=TICKER_INTEL_SIGNAL_LOOKBACK_DAYS)
    parser.add_argument("--watchlist-limit", type=int, default=TICKER_INTEL_WATCHLIST_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-push-supabase",
        action="store_true",
        help="Build output locally but do not upsert public.ticker_intelligence.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    tickers = []
    tickers.extend(args.ticker or [])
    tickers.extend(parse_tickers(args.tickers))
    try:
        sync_ticker_intelligence(
            tickers=tickers,
            news_limit=max(1, int(args.limit_news)),
            chatter_limit=max(1, int(args.limit_chatter)),
            signal_lookback_days=max(1, int(args.signal_lookback_days)),
            watchlist_limit=max(1, int(args.watchlist_limit)),
            dry_run=bool(args.dry_run),
            push_supabase=False if args.no_push_supabase else bool(TICKER_INTEL_PUSH_SUPABASE),
        )
    except RuntimeError as exc:
        if "public.ticker_intelligence" in str(exc):
            print(exc)
            raise SystemExit(2) from None
        raise


if __name__ == "__main__":
    main()
