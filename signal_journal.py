import argparse
import csv
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import SIGNAL_JOURNAL_PATH, get_supabase_client

HORIZONS = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "5d": timedelta(days=5),
}

BASE_FIELDS = [
    "journal_id",
    "signal_id",
    "created_at_utc",
    "ticker",
    "action_type",
    "channel",
    "status",
    "confidence_score",
    "price_at_signal",
    "rsi",
    "sma_15",
    "rvol",
    "bid",
    "ask",
    "investment_memo_excerpt",
]

OUTCOME_FIELDS = []
for horizon in HORIZONS:
    OUTCOME_FIELDS.extend(
        [
            f"price_after_{horizon}",
            f"return_{horizon}_pct",
            f"correct_{horizon}",
            f"evaluated_at_{horizon}",
        ]
    )

FIELDNAMES = BASE_FIELDS + OUTCOME_FIELDS

NUMERIC_FIELDS = {
    "confidence_score",
    "price_at_signal",
    "rsi",
    "sma_15",
    "rvol",
    "bid",
    "ask",
    "price_after_15m",
    "return_15m_pct",
    "price_after_1h",
    "return_1h_pct",
    "price_after_1d",
    "return_1d_pct",
    "price_after_5d",
    "return_5d_pct",
}

BOOLEAN_FIELDS = {
    "correct_15m",
    "correct_1h",
    "correct_1d",
    "correct_5d",
}

TIMESTAMP_FIELDS = {
    "created_at_utc",
    "evaluated_at_15m",
    "evaluated_at_1h",
    "evaluated_at_1d",
    "evaluated_at_5d",
}


def now_utc():
    return datetime.now(timezone.utc)


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


def format_float(value):
    if value is None:
        return ""
    return f"{float(value):.4f}"


def journal_path():
    return Path(SIGNAL_JOURNAL_PATH).expanduser()


def ensure_journal_file():
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
            writer.writeheader()
    return path


def read_rows():
    path = ensure_journal_file()
    with path.open("r", newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def write_rows(rows):
    path = ensure_journal_file()
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in FIELDNAMES})


def row_to_supabase_payload(row):
    payload = {}
    for key in FIELDNAMES:
        value = row.get(key)
        if value in (None, ""):
            payload[key] = None
        elif key in NUMERIC_FIELDS:
            payload[key] = to_float(value)
        elif key in BOOLEAN_FIELDS:
            payload[key] = str(value).lower() == "true"
        elif key in TIMESTAMP_FIELDS:
            payload[key] = str(value)
        else:
            payload[key] = value

    payload["updated_at"] = now_utc().isoformat()
    return payload


def sync_rows_to_supabase(rows):
    if not rows:
        print("No signal journal rows to sync.")
        return 0

    payloads = [row_to_supabase_payload(row) for row in rows]
    try:
        supabase = get_supabase_client()
        for start in range(0, len(payloads), 100):
            batch = payloads[start:start + 100]
            supabase.table("signal_journal").upsert(batch, on_conflict="journal_id").execute()
        print(f"Synced {len(payloads)} signal journal row(s) to Supabase.")
        return len(payloads)
    except Exception as exc:
        print(f"Signal journal Supabase sync failed: {exc}")
        return 0


def extract_signal_id(insert_response):
    data = getattr(insert_response, "data", None)
    if isinstance(data, list) and data:
        return data[0].get("id")
    return None


def latest_price(ticker):
    try:
        import yfinance as yf

        history = yf.Ticker(ticker).history(period="1d", interval="1m", prepost=True)
        if history.empty:
            history = yf.Ticker(ticker).history(period="5d", interval="1d")
        if history.empty:
            return None
        return float(history["Close"].dropna().iloc[-1])
    except Exception:
        return None


def resolve_signal_price(payload):
    price = to_float(payload.get("price_at_signal"))
    if price:
        return price

    bid = to_float(payload.get("bid"))
    ask = to_float(payload.get("ask"))
    if bid and ask:
        return (bid + ask) / 2

    ticker = payload.get("ticker")
    if ticker:
        return latest_price(ticker)
    return None


def signal_already_journaled(rows, signal_id):
    if not signal_id:
        return False
    return any(str(row.get("signal_id")) == str(signal_id) for row in rows)


def record_signal(payload, insert_response=None):
    """Append a newly accepted signal to the local training journal."""
    rows = read_rows()
    signal_id = extract_signal_id(insert_response)
    if signal_already_journaled(rows, signal_id):
        return False

    memo = str(payload.get("investment_memo") or "")
    row = {field: "" for field in FIELDNAMES}
    row.update(
        {
            "journal_id": str(uuid.uuid4()),
            "signal_id": signal_id or "",
            "created_at_utc": now_utc().isoformat(),
            "ticker": payload.get("ticker", ""),
            "action_type": payload.get("action_type", ""),
            "channel": payload.get("channel", ""),
            "status": payload.get("status", ""),
            "confidence_score": payload.get("confidence_score", ""),
            "price_at_signal": format_float(resolve_signal_price(payload)),
            "rsi": payload.get("rsi", ""),
            "sma_15": payload.get("sma_15", ""),
            "rvol": payload.get("rvol", ""),
            "bid": payload.get("bid", ""),
            "ask": payload.get("ask", ""),
            "investment_memo_excerpt": memo[:500].replace("\r", " ").replace("\n", " "),
        }
    )
    rows.append(row)
    write_rows(rows)
    sync_rows_to_supabase([row])
    print(f"Journaled signal: {row['action_type']} {row['ticker']} via {row['channel']}")
    return True


def load_intraday_history(ticker, cache):
    if ticker in cache:
        return cache[ticker]

    try:
        import yfinance as yf

        history = yf.Ticker(ticker).history(period="7d", interval="5m", prepost=True)
        if not history.empty:
            if history.index.tz is None:
                history.index = history.index.tz_localize(timezone.utc)
            else:
                history.index = history.index.tz_convert(timezone.utc)
    except Exception:
        history = None

    cache[ticker] = history
    return history


def price_at_or_after(ticker, target_time, cache):
    history = load_intraday_history(ticker, cache)
    if history is None or history.empty:
        return None

    future = history[history.index >= target_time]
    if future.empty:
        return None
    close = future["Close"].dropna()
    if close.empty:
        return None
    return float(close.iloc[0])


def directional_return_pct(action, start_price, end_price):
    raw_return = ((end_price - start_price) / start_price) * 100
    if str(action).upper() == "SELL":
        return -raw_return
    return raw_return


def update_outcomes():
    rows = read_rows()
    cache = {}
    updated = 0
    current_time = now_utc()

    for row in rows:
        created_at = parse_datetime(row.get("created_at_utc"))
        ticker = row.get("ticker")
        start_price = to_float(row.get("price_at_signal"))
        if not created_at or not ticker or not start_price:
            continue

        for horizon, delta in HORIZONS.items():
            if row.get(f"price_after_{horizon}"):
                continue

            target_time = created_at + delta
            if current_time < target_time:
                continue

            end_price = price_at_or_after(ticker, target_time, cache)
            if end_price is None:
                continue

            signal_return = directional_return_pct(row.get("action_type"), start_price, end_price)
            row[f"price_after_{horizon}"] = format_float(end_price)
            row[f"return_{horizon}_pct"] = format_float(signal_return)
            row[f"correct_{horizon}"] = "true" if signal_return > 0 else "false"
            row[f"evaluated_at_{horizon}"] = current_time.isoformat()
            updated += 1

    write_rows(rows)
    if updated:
        sync_rows_to_supabase(rows)
    print(f"Updated {updated} journal outcome field(s).")
    return updated


def sync_journal():
    return sync_rows_to_supabase(read_rows())


def summarize(horizon):
    rows = read_rows()
    return_field = f"return_{horizon}_pct"
    correct_field = f"correct_{horizon}"
    groups = defaultdict(list)

    for row in rows:
        value = to_float(row.get(return_field))
        if value is None:
            continue
        channel = row.get("channel") or "UNKNOWN"
        groups[channel].append((value, row.get(correct_field) == "true"))

    if not groups:
        print(f"No evaluated {horizon} outcomes yet.")
        return

    print(f"Signal Journal Summary ({horizon})")
    print("-" * 40)
    for channel, results in sorted(groups.items()):
        count = len(results)
        avg_return = sum(value for value, _ in results) / count
        win_rate = sum(1 for _, correct in results if correct) / count * 100
        print(f"{channel}: {count} signal(s), avg directional return {avg_return:.2f}%, win rate {win_rate:.1f}%")


def parse_args():
    parser = argparse.ArgumentParser(description="Track and score generated trading signals.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("update", help="Update forward returns for journaled signals.")
    subparsers.add_parser("sync", help="Sync local journal rows to Supabase.")

    summary_parser = subparsers.add_parser("summary", help="Show performance by signal channel.")
    summary_parser.add_argument("--horizon", choices=HORIZONS.keys(), default="1h")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.command == "update":
        update_outcomes()
    elif args.command == "sync":
        sync_journal()
    elif args.command == "summary":
        summarize(args.horizon)


if __name__ == "__main__":
    main()
