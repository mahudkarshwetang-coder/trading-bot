import argparse
import math
import time
from datetime import datetime, timezone

from ib_insync import IB

from config import (
    BROKER_SYNC_INTERVAL_SECONDS,
    BROKER_SYNC_MARK_MISSING_SIGNALS,
    IBKR_HOST,
    IBKR_PORT,
    IBKR_SYNC_CLIENT_ID,
    get_supabase_client,
)

BROKER_POSITIONS_SETUP_MESSAGE = """
Supabase is missing the public.broker_positions table.

Fix:
  1. Open Supabase Dashboard -> SQL Editor.
  2. Run the SQL in supabase/broker_positions.sql from this repo.
  3. Re-run: python broker_sync.py

Tip:
  python broker_sync.py --dry-run only checks IBKR and does not require the table.
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


def is_missing_broker_positions_table(exc):
    message = str(exc)
    return (
        "broker_positions" in message
        and (
            "PGRST205" in message
            or "schema cache" in message
            or "Could not find the table" in message
        )
    )


def connect_to_ibkr():
    ib = IB()
    ib.connect(
        IBKR_HOST,
        IBKR_PORT,
        clientId=IBKR_SYNC_CLIENT_ID,
        readonly=True,
        timeout=10,
    )
    ib.reqMarketDataType(4)
    print(f"Connected to IBKR for broker sync on {IBKR_HOST}:{IBKR_PORT}.")
    return ib


def get_market_price(ib, contract):
    try:
        tickers = ib.reqTickers(contract)
        ib.sleep(0.25)
        if not tickers:
            return None

        ticker = tickers[0]
        price = clean_float(ticker.marketPrice())
        if price and price > 0:
            return price

        close = clean_float(ticker.close)
        if close and close > 0:
            return close
    except Exception as exc:
        print(f"Price lookup skipped for {contract.symbol}: {exc}")

    return None


def build_position_snapshot(ib):
    positions = ib.positions()
    synced_at = utc_now_iso()
    snapshots = []

    for item in positions:
        contract = item.contract
        quantity = clean_float(item.position) or 0.0
        if quantity == 0:
            continue

        ticker = contract.symbol
        avg_cost = clean_float(item.avgCost)
        market_price = get_market_price(ib, contract)
        market_value = None
        unrealized_pnl = None

        if market_price is not None:
            market_value = market_price * quantity
            if avg_cost is not None:
                unrealized_pnl = (market_price - avg_cost) * quantity

        snapshots.append(
            {
                "account": item.account,
                "ticker": ticker,
                "con_id": contract.conId or None,
                "sec_type": contract.secType,
                "exchange": contract.exchange,
                "currency": contract.currency,
                "quantity": quantity,
                "avg_cost": avg_cost,
                "market_price": market_price,
                "market_value": market_value,
                "unrealized_pnl": unrealized_pnl,
                "side": "LONG" if quantity > 0 else "SHORT",
                "source": "ibkr",
                "is_open": True,
                "synced_at": synced_at,
                "updated_at": synced_at,
            }
        )

    return snapshots


def upsert_open_positions(supabase, snapshots):
    if not snapshots:
        return

    supabase.table("broker_positions").upsert(
        snapshots,
        on_conflict="account,ticker",
    ).execute()


def mark_missing_positions_closed(supabase, snapshots):
    accounts = sorted({row["account"] for row in snapshots})

    if not accounts:
        response = (
            supabase.table("broker_positions")
            .select("account,ticker")
            .eq("is_open", True)
            .execute()
        )
        accounts = sorted({row["account"] for row in response.data or []})

    closed_rows = []
    now = utc_now_iso()

    for account in accounts:
        active_tickers = {
            row["ticker"]
            for row in snapshots
            if row["account"] == account
        }
        response = (
            supabase.table("broker_positions")
            .select("ticker")
            .eq("account", account)
            .eq("is_open", True)
            .execute()
        )

        for row in response.data or []:
            ticker = row["ticker"]
            if ticker in active_tickers:
                continue

            supabase.table("broker_positions").update(
                {
                    "quantity": 0,
                    "market_value": 0,
                    "unrealized_pnl": 0,
                    "is_open": False,
                    "synced_at": now,
                    "updated_at": now,
                }
            ).eq("account", account).eq("ticker", ticker).execute()
            closed_rows.append((account, ticker))

    return closed_rows


def mark_missing_signals_closed(supabase, active_tickers):
    response = (
        supabase.table("market_signals")
        .select("id,ticker")
        .eq("status", "executed")
        .execute()
    )
    closed_count = 0

    for signal in response.data or []:
        ticker = signal.get("ticker")
        if ticker in active_tickers:
            continue

        supabase.table("market_signals").update(
            {"status": "closed_external"}
        ).eq("id", signal["id"]).execute()
        closed_count += 1

    return closed_count


def sync_once(ib, supabase, mark_signals=False, dry_run=False):
    snapshots = build_position_snapshot(ib)
    active_tickers = {row["ticker"] for row in snapshots}

    print(f"IBKR active positions: {len(snapshots)}")
    for row in snapshots:
        print(
            f"  {row['ticker']}: {row['side']} {row['quantity']:.4g} "
            f"avg={row['avg_cost']} mark={row['market_price']}"
        )

    if dry_run:
        print("DRY RUN: Supabase was not updated.")
        return

    try:
        upsert_open_positions(supabase, snapshots)
        closed_rows = mark_missing_positions_closed(supabase, snapshots)
    except Exception as exc:
        if is_missing_broker_positions_table(exc):
            raise RuntimeError(BROKER_POSITIONS_SETUP_MESSAGE) from exc
        raise

    if closed_rows:
        print(f"Marked {len(closed_rows)} stale broker position(s) closed.")

    if mark_signals:
        closed_signals = mark_missing_signals_closed(supabase, active_tickers)
        if closed_signals:
            print(f"Marked {closed_signals} stale executed signal(s) closed_external.")

    print("Broker snapshot synced to Supabase.")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Sync current IBKR paper positions into Supabase broker_positions."
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Keep syncing on BROKER_SYNC_INTERVAL_SECONDS.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=BROKER_SYNC_INTERVAL_SECONDS,
        help="Loop interval in seconds.",
    )
    parser.add_argument(
        "--mark-missing-signals",
        action="store_true",
        default=BROKER_SYNC_MARK_MISSING_SIGNALS,
        help="Mark executed market_signals not found in IBKR as closed_external.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read IBKR positions and print the snapshot without writing Supabase.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    supabase = get_supabase_client()
    ib = connect_to_ibkr()

    try:
        while True:
            try:
                sync_once(
                    ib,
                    supabase,
                    mark_signals=args.mark_missing_signals,
                    dry_run=args.dry_run,
                )
            except RuntimeError as exc:
                if "public.broker_positions" in str(exc):
                    print(exc)
                    raise SystemExit(2) from None
                raise

            if not args.loop:
                break

            time.sleep(max(5, args.interval))
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
