import argparse
import math
from datetime import datetime, timezone

import requests
import yfinance as yf
from ib_insync import IB

from config import (
    IBKR_CONTEXT_CLIENT_ID,
    IBKR_HOST,
    IBKR_PORT,
    MASSIVE_API_KEY,
    SIGNAL_CONTEXT_LIMIT,
    get_supabase_client,
)

SEC_HEADERS = {
    "User-Agent": "SignalCenter trading bot contact@example.com",
    "Accept-Encoding": "gzip, deflate",
}

CONTEXT_SETUP_MESSAGE = """
Supabase is missing the public.signal_context table.

Fix:
  1. Open Supabase Dashboard -> SQL Editor.
  2. Run the SQL in supabase/signal_context.sql from this repo.
  3. Re-run: python context_enrichment.py
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


def missing_context_table(exc):
    message = str(exc)
    return (
        "signal_context" in message
        and (
            "PGRST205" in message
            or "schema cache" in message
            or "Could not find the table" in message
        )
    )


def connect_ibkr():
    ib = IB()
    try:
        ib.connect(
            IBKR_HOST,
            IBKR_PORT,
            clientId=IBKR_CONTEXT_CLIENT_ID,
            readonly=True,
            timeout=6,
        )
        ib.reqMarketDataType(4)
        print(f"Connected to IBKR for signal context on {IBKR_HOST}:{IBKR_PORT}.")
        return ib
    except Exception as exc:
        print(f"IBKR context unavailable: {exc}")
        return None


def get_positions_by_ticker(ib):
    positions = {}
    if ib is None:
        return positions

    try:
        for item in ib.positions():
            symbol = item.contract.symbol.upper()
            current = positions.get(symbol, {"quantity": 0.0, "avg_cost": None})
            current["quantity"] += clean_float(item.position) or 0.0
            current["avg_cost"] = clean_float(item.avgCost)
            positions[symbol] = current
    except Exception as exc:
        print(f"IBKR position lookup skipped: {exc}")

    return positions


def get_pending_or_approved_signals(supabase, limit):
    rows = []
    for status in ("pending", "approved"):
        response = (
            supabase.table("market_signals")
            .select("id,ticker,action_type,status,confidence_score,created_at")
            .eq("status", status)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows.extend(response.data or [])

    unique = {}
    for row in rows:
        unique[row["id"]] = row
    return list(unique.values())[:limit]


def fetch_massive_prev_close(ticker):
    if not MASSIVE_API_KEY:
        return {}

    url = f"https://api.massive.com/v2/aggs/ticker/{ticker}/prev"
    try:
        response = requests.get(url, params={"apiKey": MASSIVE_API_KEY}, timeout=8)
        response.raise_for_status()
        payload = response.json()
        result = (payload.get("results") or [{}])[0]
        return {
            "prev_close": clean_float(result.get("c")),
            "volume": clean_float(result.get("v")),
            "source": "massive",
        }
    except Exception as exc:
        print(f"Massive previous close skipped for {ticker}: {exc}")
        return {}


def fetch_yfinance_quote(ticker):
    try:
        stock = yf.Ticker(ticker)
        fast = stock.fast_info
        history = stock.history(period="2d", interval="1d")
        price = clean_float(getattr(fast, "last_price", None) or fast.get("last_price"))
        bid = clean_float(getattr(fast, "bid", None) or fast.get("bid"))
        ask = clean_float(getattr(fast, "ask", None) or fast.get("ask"))
        prev_close = clean_float(getattr(fast, "previous_close", None) or fast.get("previous_close"))
        volume = clean_float(getattr(fast, "last_volume", None) or fast.get("last_volume"))

        if price is None and not history.empty:
            price = clean_float(history["Close"].iloc[-1])
        if prev_close is None and len(history) >= 2:
            prev_close = clean_float(history["Close"].iloc[-2])
        if volume is None and not history.empty:
            volume = clean_float(history["Volume"].iloc[-1])

        return {
            "price": price,
            "bid": bid,
            "ask": ask,
            "prev_close": prev_close,
            "volume": volume,
            "source": "yfinance",
        }
    except Exception as exc:
        print(f"Yahoo quote skipped for {ticker}: {exc}")
        return {"source": "unavailable"}


def fetch_quote(ticker):
    yahoo = fetch_yfinance_quote(ticker)
    massive = fetch_massive_prev_close(ticker)

    prev_close = massive.get("prev_close") or yahoo.get("prev_close")
    volume = massive.get("volume") or yahoo.get("volume")
    bid = yahoo.get("bid")
    ask = yahoo.get("ask")
    spread = None
    if bid is not None and ask is not None and ask >= bid:
        spread = ask - bid

    return {
        "quote_price": yahoo.get("price") or prev_close,
        "quote_bid": bid,
        "quote_ask": ask,
        "quote_spread": spread,
        "quote_prev_close": prev_close,
        "quote_volume": volume,
        "quote_source": massive.get("source") or yahoo.get("source"),
        "quote_at": utc_now_iso(),
    }


def fetch_sec_ticker_map():
    try:
        response = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=SEC_HEADERS,
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        return {
            item["ticker"].upper(): str(item["cik_str"]).zfill(10)
            for item in payload.values()
        }
    except Exception as exc:
        print(f"SEC ticker map unavailable: {exc}")
        return {}


def fetch_sec_context(ticker, ticker_map):
    cik = ticker_map.get(ticker.upper())
    if not cik:
        return {
            "latest_filing_type": None,
            "latest_filing_date": None,
            "latest_filing_title": None,
            "latest_filing_url": None,
            "sec_risk_flags": ["SEC ticker mapping unavailable"],
            "sec_risk_score": 0.15,
            "catalyst_summary": "No SEC filing context available.",
        }

    try:
        response = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers=SEC_HEADERS,
            timeout=10,
        )
        response.raise_for_status()
        recent = response.json().get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accession_numbers = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])
        descriptions = recent.get("primaryDocDescription", [])

        latest = None
        risk_flags = []
        risk_score = 0.0
        watched_forms = {"8-K", "10-Q", "10-K", "S-1", "S-3", "424B5", "424B3", "SC 13G", "SC 13D", "4"}

        for index, form in enumerate(forms):
            if form not in watched_forms:
                continue
            accession = accession_numbers[index].replace("-", "")
            primary_doc = primary_docs[index]
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{primary_doc}"
            latest = {
                "type": form,
                "date": dates[index],
                "title": descriptions[index] or form,
                "url": url,
            }
            break

        if latest:
            form = latest["type"]
            title = latest["title"].lower()
            if form in {"S-1", "S-3", "424B5", "424B3"}:
                risk_flags.append("Offering/dilution filing")
                risk_score += 0.45
            if form == "8-K":
                risk_flags.append("Recent 8-K catalyst")
                risk_score += 0.12
            if "going concern" in title:
                risk_flags.append("Going concern language")
                risk_score += 0.5
            if form == "4":
                risk_flags.append("Recent insider transaction")
                risk_score += 0.12

        return {
            "latest_filing_type": latest["type"] if latest else None,
            "latest_filing_date": latest["date"] if latest else None,
            "latest_filing_title": latest["title"] if latest else None,
            "latest_filing_url": latest["url"] if latest else None,
            "sec_risk_flags": risk_flags,
            "sec_risk_score": min(risk_score, 1.0),
            "catalyst_summary": (
                f"Latest watched SEC filing: {latest['type']} on {latest['date']}."
                if latest else "No recent watched SEC filing found."
            ),
        }
    except Exception as exc:
        print(f"SEC context skipped for {ticker}: {exc}")
        return {
            "latest_filing_type": None,
            "latest_filing_date": None,
            "latest_filing_title": None,
            "latest_filing_url": None,
            "sec_risk_flags": ["SEC fetch failed"],
            "sec_risk_score": 0.2,
            "catalyst_summary": "SEC filing context could not be refreshed.",
        }


def fetch_macro_context():
    try:
        spy = yf.Ticker("SPY").history(period="30d", interval="1d")
        qqq = yf.Ticker("QQQ").history(period="30d", interval="1d")
        vix = yf.Ticker("^VIX").history(period="5d", interval="1d")
        if spy.empty or qqq.empty or vix.empty:
            raise RuntimeError("macro series missing")

        spy_trend = clean_float(spy["Close"].iloc[-1] / spy["Close"].rolling(20).mean().iloc[-1] - 1)
        qqq_trend = clean_float(qqq["Close"].iloc[-1] / qqq["Close"].rolling(20).mean().iloc[-1] - 1)
        vix_level = clean_float(vix["Close"].iloc[-1])

        if vix_level and vix_level >= 25:
            regime = "Risk Off"
        elif spy_trend and qqq_trend and spy_trend > 0 and qqq_trend > 0:
            regime = "Risk On"
        else:
            regime = "Mixed"

        return {
            "macro_regime": regime,
            "macro_summary": f"SPY 20D trend {spy_trend or 0:.1%}, QQQ 20D trend {qqq_trend or 0:.1%}, VIX {vix_level or 0:.1f}.",
        }
    except Exception as exc:
        print(f"Macro context skipped: {exc}")
        return {
            "macro_regime": "Unknown",
            "macro_summary": "Macro context unavailable.",
        }


def context_score(signal, position, quote, sec_context, macro_context):
    score = 0.55
    action = signal.get("action_type")
    quantity = position.get("quantity", 0.0)

    if action == "SELL" and quantity <= 0:
        score -= 0.35
    elif action == "SELL":
        score += 0.1

    spread = quote.get("quote_spread")
    price = quote.get("quote_price")
    if spread and price and price > 0:
        spread_pct = spread / price
        if spread_pct > 0.01:
            score -= 0.15
        elif spread_pct < 0.002:
            score += 0.05

    score -= (sec_context.get("sec_risk_score") or 0) * 0.35

    if macro_context.get("macro_regime") == "Risk On" and action == "BUY":
        score += 0.08
    elif macro_context.get("macro_regime") == "Risk Off" and action == "BUY":
        score -= 0.1

    return max(0.0, min(1.0, score))


def build_context(signal, positions, ticker_map, macro_context):
    ticker = signal["ticker"].upper()
    position = positions.get(ticker, {"quantity": 0.0, "avg_cost": None})
    quantity = position.get("quantity") or 0.0
    action = signal.get("action_type")
    quote = fetch_quote(ticker)
    sec_context = fetch_sec_context(ticker, ticker_map)
    sell_allowed = action != "SELL" or quantity > 0
    no_short_reason = None
    if action == "SELL" and quantity <= 0:
        no_short_reason = "No positive long IBKR position; short selling blocked."

    payload = {
        "signal_id": signal["id"],
        "ticker": ticker,
        "broker_quantity": quantity,
        "broker_avg_cost": position.get("avg_cost"),
        "broker_position_side": "LONG" if quantity > 0 else ("SHORT" if quantity < 0 else "FLAT"),
        "sell_allowed": sell_allowed,
        "no_short_block_reason": no_short_reason,
        **quote,
        **sec_context,
        **macro_context,
        "context_score": context_score(signal, position, quote, sec_context, macro_context),
        "source_summary": {
            "broker": "ibkr" if positions else "unavailable",
            "quote": quote.get("quote_source") or "unavailable",
            "sec": "sec.gov",
            "macro": "yfinance",
        },
        "updated_at": utc_now_iso(),
    }
    return payload


def sync_context(limit=SIGNAL_CONTEXT_LIMIT, dry_run=False):
    supabase = get_supabase_client()
    ib = connect_ibkr()
    positions = get_positions_by_ticker(ib)
    ticker_map = fetch_sec_ticker_map()
    macro_context = fetch_macro_context()
    signals = get_pending_or_approved_signals(supabase, limit)

    print(f"Refreshing execution context for {len(signals)} signal(s).")
    rows = []
    for signal in signals:
        row = build_context(signal, positions, ticker_map, macro_context)
        rows.append(row)
        print(
            f"  {row['ticker']}: score={row['context_score']:.2f} "
            f"broker={row['broker_position_side']} quote={row.get('quote_price')}"
        )

    if ib is not None:
        ib.disconnect()

    if dry_run:
        print("DRY RUN: Supabase was not updated.")
        return rows

    if not rows:
        return rows

    try:
        supabase.table("signal_context").upsert(rows, on_conflict="signal_id").execute()
    except Exception as exc:
        if missing_context_table(exc):
            raise RuntimeError(CONTEXT_SETUP_MESSAGE) from exc
        raise

    print("Signal execution context synced to Supabase.")
    return rows


def parse_args():
    parser = argparse.ArgumentParser(description="Enrich pending/approved signals with broker, quote, SEC, and macro context.")
    parser.add_argument("--limit", type=int, default=SIGNAL_CONTEXT_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        sync_context(limit=args.limit, dry_run=args.dry_run)
    except RuntimeError as exc:
        if "public.signal_context" in str(exc):
            print(exc)
            raise SystemExit(2) from None
        raise


if __name__ == "__main__":
    main()
