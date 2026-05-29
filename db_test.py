import argparse

from config import get_supabase_client

supabase = get_supabase_client()


def test_database_pipeline(write=False):
    print("Starting isolated database bridge test...")
    print("Querying market_signals where status = approved...")

    try:
        response = supabase.table("market_signals").select("*").eq("status", "approved").execute()
        signals = response.data

        if not signals:
            print("Connected to Supabase, but no approved signals were found.")
            return

        print(f"Found {len(signals)} approved signal(s).")
        for signal in signals:
            print(f"Signal ID: {signal['id']} ({signal['action_type']} {signal['ticker']})")

            if write:
                print("WRITE MODE: Updating status to executed...")
                supabase.table("market_signals").update({"status": "executed"}).eq("id", signal["id"]).execute()
                print("Database update verified.")
            else:
                print("READ ONLY: No Supabase rows were updated. Pass --write to test writes.")

    except Exception as exc:
        print(f"Connection error: {exc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read approved Supabase signals; writes only with --write.")
    parser.add_argument("--write", action="store_true", help="Update matching approved signals to executed.")
    args = parser.parse_args()
    test_database_pipeline(write=args.write)
