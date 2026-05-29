from datetime import datetime, timedelta, timezone

from config import SIGNAL_COOLDOWN_MINUTES


def signal_recently_exists(supabase, ticker, action, channel=None, cooldown_minutes=None):
    """Best-effort duplicate guard for repeated scanner loops."""
    minutes = cooldown_minutes or SIGNAL_COOLDOWN_MINUTES
    threshold = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    try:
        query = (
            supabase.table("market_signals")
            .select("id,created_at")
            .eq("ticker", ticker)
            .eq("action_type", action)
            .gte("created_at", threshold.isoformat())
        )
        if channel:
            query = query.eq("channel", channel)

        response = query.limit(1).execute()
        return bool(response.data)
    except Exception as exc:
        print(f"Duplicate-signal check failed for {ticker}: {exc}")
        return False


def insert_signal_with_cooldown(supabase, payload, channel=None, cooldown_minutes=None):
    ticker = payload.get("ticker")
    action = payload.get("action_type")

    if signal_recently_exists(supabase, ticker, action, channel, cooldown_minutes):
        minutes = cooldown_minutes or SIGNAL_COOLDOWN_MINUTES
        print(f"Skipping duplicate {action} signal for {ticker}; cooldown is {minutes} minutes.")
        return False

    supabase.table("market_signals").insert(payload).execute()
    return True
