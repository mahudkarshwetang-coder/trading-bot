import argparse
import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytz
import yfinance as yf
from ib_insync import IB, Stock

from config import (
    IBKR_HOST,
    IBKR_NEWS_CLIENT_ID,
    IBKR_NEWS_COOLDOWN_MINUTES,
    IBKR_NEWS_ENABLED,
    IBKR_NEWS_INTERVAL_SECONDS,
    IBKR_NEWS_LOOKBACK_MINUTES,
    IBKR_NEWS_MAX_TICKERS,
    IBKR_NEWS_MIN_CONFIDENCE,
    IBKR_NEWS_MIN_DIRECTIONAL_SCORE,
    IBKR_NEWS_OUTPUT_PATH,
    IBKR_NEWS_PROVIDERS,
    IBKR_NEWS_PUSH_SIGNALS,
    IBKR_NEWS_RESULTS_PER_TICKER,
    IBKR_NEWS_SEEN_PATH,
    IBKR_PORT,
    MARKET_TIMEZONE,
    get_supabase_client,
)
from market_session import now_market_time
from signal_quality_filter import review_signal_quality
from signal_utils import insert_signal_with_cooldown

CHANNEL = "IBKR_NEWS"
ROOT = Path(__file__).resolve().parent

POSITIVE_TERMS = [
    "upgraded", "initiated with buy", "initiated at buy",
    "initiated with outperform", "initiated at outperform", "outperform",
    "overweight", "buy rating", "raised target", "target raised",
    "price target raised", "beat", "beats", "strong demand",
    "raises guidance", "raised guidance", "contract award", "wins contract",
    "approval", "partnership", "record revenue", "above consensus",
]
NEGATIVE_TERMS = [
    "downgraded", "underperform", "underweight",
    "sell rating", "lowered target", "target lowered", "price target lowered",
    "cut target", "miss", "misses", "lowers guidance", "lowered guidance",
    "lawsuit", "investigation", "probe", "recall", "sec charges",
    "bankruptcy", "offering", "dilution", "below consensus", "weak demand",
]
NEUTRAL_DAMPENERS = [
    "reiterated", "maintained", "neutral", "hold rating", "equal weight",
    "market perform", "sector perform",
]


def utc_now():
    return datetime.now(timezone.utc)


def market_tz():
    return pytz.timezone(MARKET_TIMEZONE)


def ib_datetime(value):
    return value.astimezone(market_tz()).strftime("%Y%m%d %H:%M:%S")


def normalize_news_time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = market_tz().localize(dt)
    return dt.astimezone(timezone.utc)


def clean_headline(value):
    text = html.unescape(str(value or "")).strip()
    text = re.sub(r"^\{[^}]+\}", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text.lstrip("! ").strip()


def parse_tickers(values):
    tickers = []
    seen = set()
    for value in values:
        for part in str(value or "").replace("\n", ",").split(","):
            ticker = part.strip().upper()
            if ticker and ticker not in seen:
                tickers.append(ticker)
                seen.add(ticker)
    return tickers


def load_daily_targets():
    target_path = ROOT / "daily_targets.txt"
    if not target_path.exists():
        return []
    return parse_tickers([target_path.read_text(encoding="utf-8", errors="replace")])


def load_supabase_watchlists(supabase):
    tickers = []
    try:
        response = (
            supabase.table("bot_settings")
            .select("watchlist,radar_watchlist,earnings_watchlist")
            .eq("id", 1)
            .execute()
        )
        if response.data:
            row = response.data[0]
            for key in ("watchlist", "radar_watchlist", "earnings_watchlist"):
                tickers.extend(row.get(key) or [])
    except Exception as exc:
        print(f"[IBKR NEWS] Supabase watchlist load skipped: {exc}")
    return parse_tickers(tickers)


def load_watchlist(supabase=None, max_tickers=None):
    tickers = []
    tickers.extend(load_daily_targets())
    if supabase is not None:
        tickers.extend(load_supabase_watchlists(supabase))
    unique = parse_tickers(tickers)
    if max_tickers and max_tickers > 0:
        unique = unique[:max_tickers]
    return unique


def load_seen(path):
    file_path = Path(path)
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(str(item) for item in data)
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[IBKR NEWS] Seen-state read skipped: {exc}")
    return set()


def save_seen(path, seen, max_items=10000):
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(seen)[-max_items:]
    file_path.write_text(json.dumps(ordered, ensure_ascii=True, indent=2), encoding="utf-8")


def append_jsonl(path, rows):
    if not rows:
        return
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def provider_map(providers):
    return {provider.code: provider.name for provider in providers}


def select_provider_codes(providers, configured):
    available = provider_map(providers)
    if not available:
        return "", {}

    requested = []
    configured = str(configured or "").strip()
    if configured:
        requested = [
            item.strip().upper()
            for item in re.split(r"[,+\s]+", configured)
            if item.strip()
        ]
    else:
        requested = list(available)

    selected = [code for code in requested if code in available]
    missing = [code for code in requested if code not in available]
    if missing:
        print(f"[IBKR NEWS] Configured provider(s) not available from TWS: {', '.join(missing)}")
    return "+".join(selected), {code: available[code] for code in selected}


def qualify_contracts(ib, tickers):
    contracts = [Stock(ticker, "SMART", "USD") for ticker in tickers]
    qualified = {}
    try:
        results = ib.qualifyContracts(*contracts)
        for contract in results:
            if contract.conId:
                qualified[str(contract.symbol).upper()] = contract
    except Exception as exc:
        print(f"[IBKR NEWS] Bulk contract qualification fell back to one-by-one: {exc}")
        for ticker in tickers:
            try:
                result = ib.qualifyContracts(Stock(ticker, "SMART", "USD"))
                if result and result[0].conId:
                    qualified[ticker] = result[0]
            except Exception as inner_exc:
                print(f"[IBKR NEWS] Contract qualification failed for {ticker}: {inner_exc}")
    return qualified


def score_headline(headline, provider_code):
    lower = headline.lower()
    positive = [term for term in POSITIVE_TERMS if term in lower]
    negative = [term for term in NEGATIVE_TERMS if term in lower]
    dampeners = [term for term in NEUTRAL_DAMPENERS if term in lower]

    if re.search(r"initiated.{0,90}(buy|outperform|overweight)", lower):
        positive.append("initiated positive analyst rating")
    if re.search(r"initiated.{0,90}(sell|underperform|underweight)", lower):
        negative.append("initiated negative analyst rating")
    if re.search(r"reiterated.{0,90}(buy|outperform|overweight)", lower):
        positive.append("reiterated positive analyst rating")
    if re.search(r"reiterated.{0,90}(sell|underperform|underweight)", lower):
        negative.append("reiterated negative analyst rating")

    directional_score = len(positive) - len(negative)
    if provider_code == "BRFUPDN" and ("upgraded" in lower or "downgraded" in lower):
        directional_score += 1 if directional_score > 0 else -1

    if directional_score > 0:
        action = "BUY"
        terms = positive
    elif directional_score < 0:
        action = "SELL"
        terms = negative
    else:
        action = "WATCH"
        terms = []

    confidence = 45 + min(35, abs(directional_score) * 12)
    if provider_code == "BRFUPDN":
        confidence += 6
    if dampeners and abs(directional_score) <= 1:
        confidence -= 8
    confidence = max(0, min(95, int(round(confidence))))

    return {
        "action": action,
        "directional_score": directional_score,
        "confidence": confidence,
        "matched_terms": sorted(set(terms)),
        "dampeners": sorted(set(dampeners)),
    }


def fetch_price_snapshot(ticker):
    try:
        data = yf.Ticker(ticker).history(period="5d", interval="1m", prepost=True)
        if data.empty:
            data = yf.Ticker(ticker).history(period="5d", interval="1d")
        if data.empty:
            return {}
        close = float(data["Close"].dropna().iloc[-1])
        volume = data["Volume"].dropna()
        rvol = None
        if len(volume) > 10:
            avg_volume = float(volume.tail(100).mean())
            latest_volume = float(volume.iloc[-1])
            if avg_volume > 0:
                rvol = round(latest_volume / avg_volume, 2)
        return {
            "price_at_signal": round(close, 2),
            "rvol": rvol,
            "bid": round(close * 0.999, 2),
            "ask": round(close * 1.001, 2),
        }
    except Exception as exc:
        print(f"[IBKR NEWS] Price snapshot skipped for {ticker}: {exc}")
        return {}


def build_signal_payload(item):
    ticker = item["ticker"]
    action = item["action"]
    confidence = item["confidence"]
    headline = item["headline"]
    provider = item["provider_code"]
    memo = (
        f"IBKR News Catalyst Scanner detected {action} pressure for {ticker}.\n\n"
        f"Provider: {provider} ({item.get('provider_name') or 'unknown'})\n"
        f"Published: {item.get('published_at')}\n"
        f"Article ID: {item.get('article_id')}\n"
        f"Headline: {headline}\n\n"
        f"Matched catalyst terms: {', '.join(item.get('matched_terms') or []) or 'directional headline'}\n"
        f"Directional score: {item.get('directional_score')}\n\n"
        "This signal came from the IBKR/TWS news feed. It still requires Qwen signal-quality review, "
        "iPad/main.py approval flow, and the final Qwen execution gate before any order is sent."
    )
    payload = {
        "ticker": ticker,
        "action_type": action,
        "confidence_score": confidence,
        "investment_memo": memo,
        "status": "pending",
        "channel": CHANNEL,
    }
    payload.update(fetch_price_snapshot(ticker))
    return payload


def should_push_signal(item):
    if item["action"] not in {"BUY", "SELL"}:
        return False
    if abs(item["directional_score"]) < max(1, IBKR_NEWS_MIN_DIRECTIONAL_SCORE):
        return False
    return item["confidence"] >= IBKR_NEWS_MIN_CONFIDENCE


def push_signal(supabase, item, dry_run=False):
    if dry_run:
        return False, "dry_run"

    payload = build_signal_payload(item)
    approved, decision, payload = review_signal_quality(
        payload,
        headlines=[item["headline"]],
        reasoning=(
            f"IBKR provider={item['provider_code']} article_id={item['article_id']} "
            f"directional_score={item['directional_score']} matched_terms={item.get('matched_terms')}"
        ),
    )
    if not approved:
        return False, f"qwen_blocked:{decision.get('quality_score')}:{decision.get('rationale')}"

    inserted = insert_signal_with_cooldown(
        supabase,
        payload,
        channel=CHANNEL,
        cooldown_minutes=IBKR_NEWS_COOLDOWN_MINUTES,
        context_fragments=[item["article_id"], item["headline"]],
    )
    return bool(inserted), "inserted" if inserted else "duplicate_or_cooldown"


def fetch_recent_news_for_contract(ib, contract, provider_codes, start, end, total_results):
    try:
        return ib.reqHistoricalNews(
            contract.conId,
            provider_codes,
            ib_datetime(start),
            "",
            total_results,
            [],
        )
    except Exception as exc:
        print(f"[IBKR NEWS] Historical news failed for {contract.symbol}: {exc}")
        return []


def scan_once(
    ib,
    supabase,
    tickers,
    provider_codes,
    provider_names,
    seen,
    lookback_minutes,
    results_per_ticker,
    dry_run=False,
    push_signals=True,
):
    end = now_market_time()
    start = end - timedelta(minutes=max(1, int(lookback_minutes)))
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)

    contracts = qualify_contracts(ib, tickers)
    output_rows = []
    signal_count = 0
    new_count = 0

    for ticker in tickers:
        contract = contracts.get(ticker)
        if not contract:
            continue
        articles = fetch_recent_news_for_contract(
            ib,
            contract,
            provider_codes,
            start,
            end,
            max(1, int(results_per_ticker)),
        )
        for article in articles:
            published_at = normalize_news_time(article.time)
            if not published_at or published_at < start_utc or published_at > end_utc + timedelta(minutes=5):
                continue

            headline = clean_headline(article.headline)
            article_id = str(article.articleId or "").strip()
            provider_code = str(article.providerCode or "").strip()
            key = f"{ticker}|{provider_code}|{article_id or headline}"
            if key in seen:
                continue
            seen.add(key)
            new_count += 1

            score = score_headline(headline, provider_code)
            row = {
                "id": key,
                "ticker": ticker,
                "provider_code": provider_code,
                "provider_name": provider_names.get(provider_code, ""),
                "article_id": article_id,
                "headline": headline,
                "raw_headline": str(article.headline or ""),
                "published_at": published_at.isoformat(),
                "fetched_at": utc_now().isoformat(),
                **score,
                "signal_pushed": False,
                "signal_note": "",
            }

            if push_signals and should_push_signal(row):
                pushed, note = push_signal(supabase, row, dry_run=dry_run)
                row["signal_pushed"] = pushed
                row["signal_note"] = note
                if pushed:
                    signal_count += 1
            elif not should_push_signal(row):
                row["signal_note"] = "not_directional_enough"
            elif not push_signals:
                row["signal_note"] = "push_signals_disabled"

            output_rows.append(row)
            print(
                f"[IBKR NEWS] {ticker:<6} {provider_code:<8} {row['action']:<5} "
                f"conf={row['confidence']:<3} pushed={row['signal_pushed']} | {headline}"
            )

    append_jsonl(IBKR_NEWS_OUTPUT_PATH, output_rows)
    return {
        "new_headlines": new_count,
        "saved_rows": len(output_rows),
        "signals_pushed": signal_count,
    }


def connect_ibkr():
    ib = IB()
    ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_NEWS_CLIENT_ID, timeout=10, readonly=True)
    return ib


def run_ibkr_news_scanner(
    once=False,
    dry_run=False,
    force=False,
    reset_seen=False,
    max_tickers=IBKR_NEWS_MAX_TICKERS,
    lookback_minutes=IBKR_NEWS_LOOKBACK_MINUTES,
    interval_seconds=IBKR_NEWS_INTERVAL_SECONDS,
    providers=IBKR_NEWS_PROVIDERS,
    push_signals=IBKR_NEWS_PUSH_SIGNALS,
):
    print("[IBKR NEWS] Scanner online.")
    if not IBKR_NEWS_ENABLED and not force:
        print("[IBKR NEWS] Disabled by IBKR_NEWS_ENABLED=false.")
        return True

    if dry_run:
        print("[IBKR NEWS] DRY RUN: no Supabase signals will be written.")

    seen = set() if reset_seen else load_seen(IBKR_NEWS_SEEN_PATH)
    supabase = None if dry_run else get_supabase_client()
    if dry_run:
        supabase_for_watchlist = get_supabase_client()
    else:
        supabase_for_watchlist = supabase

    ib = connect_ibkr()
    try:
        provider_list = ib.reqNewsProviders()
        provider_codes, provider_names = select_provider_codes(provider_list, providers)
        if not provider_codes:
            print("[IBKR NEWS] No available IBKR news provider codes. Scanner cannot run.")
            return False

        print(f"[IBKR NEWS] Providers: {provider_codes}")
        print(
            f"[IBKR NEWS] Runtime: lookback={lookback_minutes}m interval={interval_seconds}s "
            f"max_tickers={max_tickers} push_signals={push_signals and not dry_run}"
        )

        while True:
            started = time.time()
            tickers = load_watchlist(supabase_for_watchlist, max_tickers=max_tickers)
            if not tickers:
                print("[IBKR NEWS] No watchlist tickers available.")
                summary = {"new_headlines": 0, "saved_rows": 0, "signals_pushed": 0}
            else:
                print(f"[IBKR NEWS] Scanning {len(tickers)} ticker(s) for recent IBKR headlines...")
                summary = scan_once(
                    ib,
                    supabase,
                    tickers,
                    provider_codes,
                    provider_names,
                    seen,
                    lookback_minutes,
                    IBKR_NEWS_RESULTS_PER_TICKER,
                    dry_run=dry_run,
                    push_signals=push_signals and not dry_run,
                )
                if not dry_run:
                    save_seen(IBKR_NEWS_SEEN_PATH, seen)

            print(
                "[IBKR NEWS] Scan complete: "
                f"{summary['new_headlines']} new headline(s), "
                f"{summary['signals_pushed']} signal(s) pushed."
            )
            if once:
                return True

            elapsed = time.time() - started
            sleep_for = max(15, int(interval_seconds) - elapsed)
            print(f"[IBKR NEWS] Sleeping {sleep_for:.0f}s...")
            time.sleep(sleep_for)
    finally:
        if ib.isConnected():
            ib.disconnect()


def parse_args():
    parser = argparse.ArgumentParser(description="IBKR/TWS news catalyst scanner.")
    parser.add_argument("--once", action="store_true", help="Run one polling pass and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Print/save headlines without writing Supabase signals.")
    parser.add_argument("--force", action="store_true", help="Run even if IBKR_NEWS_ENABLED=false.")
    parser.add_argument("--reset-seen", action="store_true", help="Ignore local seen-state for this run.")
    parser.add_argument("--max-tickers", type=int, default=IBKR_NEWS_MAX_TICKERS)
    parser.add_argument("--lookback-minutes", type=int, default=IBKR_NEWS_LOOKBACK_MINUTES)
    parser.add_argument("--interval", type=int, default=IBKR_NEWS_INTERVAL_SECONDS)
    parser.add_argument("--providers", default=IBKR_NEWS_PROVIDERS)
    parser.add_argument("--no-push-signals", action="store_true", help="Do not create market_signals rows.")
    return parser.parse_args()


def main():
    args = parse_args()
    success = run_ibkr_news_scanner(
        once=args.once,
        dry_run=args.dry_run,
        force=args.force,
        reset_seen=args.reset_seen,
        max_tickers=args.max_tickers,
        lookback_minutes=args.lookback_minutes,
        interval_seconds=args.interval,
        providers=args.providers,
        push_signals=not args.no_push_signals,
    )
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
