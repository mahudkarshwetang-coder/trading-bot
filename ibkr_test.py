import argparse

import requests
import urllib3

from config import get_supabase_client

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

IBKR_BASE_URL = "https://localhost:5000/v1/api"
supabase = get_supabase_client()


def check_ibkr_authentication():
    """Pings the IBKR gateway to ensure the session is active."""
    endpoint = f"{IBKR_BASE_URL}/iserver/auth/status"
    try:
        response = requests.post(endpoint, verify=False, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("authenticated", False):
            print("IBKR Gateway: authenticated.")
            return True

        print("IBKR Gateway: not authenticated.")
        return False
    except requests.exceptions.ConnectionError:
        print("IBKR Gateway: cannot reach the server.")
        return False


def check_for_approved_signals():
    """Polls Supabase for any signals marked as approved."""
    print("Checking Supabase for approved signals...")
    try:
        response = supabase.table("market_signals").select("*").eq("status", "approved").execute()
        signals = response.data
        if not signals:
            print("No approved signals found.")
            return None

        print(f"Found {len(signals)} approved signal(s).")
        return signals
    except Exception as exc:
        print(f"Supabase error: {exc}")
        return None


def dry_run_execution(signals, write=False):
    """Formats payloads and optionally updates Supabase, but never sends live orders."""
    print("Safe mode: formatting mock orders for dry run.")

    for signal in signals:
        ibkr_payload = {
            "secType": "STK",
            "ticker": signal["ticker"],
            "cOID": str(signal["id"]),
            "listingExchange": "SMART",
            "side": signal["action_type"],
            "origOrderType": "MKT",
            "quantity": 1,
            "tif": "DAY",
        }

        print(f"Mock payload for {signal['ticker']}:")
        print(ibkr_payload)
        print("Order blocked: dry run only.")

        if write:
            print("WRITE MODE: Updating Supabase status to executed...")
            supabase.table("market_signals").update({"status": "executed"}).eq("id", signal["id"]).execute()
            print("Supabase updated.")
        else:
            print("READ ONLY: Supabase status was not changed. Pass --write to test writes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry-run approved Supabase signals against IBKR gateway.")
    parser.add_argument("--write", action="store_true", help="Mark approved signals as executed after formatting.")
    args = parser.parse_args()

    print("Starting AI Trading Middleware test in safe mode...")
    if check_ibkr_authentication():
        approved_signals = check_for_approved_signals()
        if approved_signals:
            dry_run_execution(approved_signals, write=args.write)
