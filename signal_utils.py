from datetime import datetime, timedelta, timezone
import re

from config import SIGNAL_COOLDOWN_MINUTES

ACTIVE_DUPLICATE_STATUSES = {"pending", "approved"}


def normalize_signal_text(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def parse_supabase_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def matching_signal_query(supabase, ticker, action, channel=None):
    query = (
        supabase.table("market_signals")
        .select("id,status,created_at,investment_memo")
        .eq("ticker", ticker)
        .eq("action_type", action)
    )
    if channel:
        query = query.eq("channel", channel)
    return query


def active_signal_exists(supabase, ticker, action, channel=None):
    for status in ACTIVE_DUPLICATE_STATUSES:
        response = (
            matching_signal_query(supabase, ticker, action, channel)
            .eq("status", status)
            .limit(1)
            .execute()
        )
        if response.data:
            return status
    return None


def same_news_context_exists(supabase, ticker, action, channel=None, context_fragments=None):
    fragments = [normalize_signal_text(fragment) for fragment in context_fragments or [] if fragment]
    fragments = [fragment for fragment in fragments if fragment]
    if not fragments:
        return False

    response = matching_signal_query(supabase, ticker, action, channel).limit(100).execute()
    for row in response.data or []:
        memo = normalize_signal_text(row.get("investment_memo"))
        if memo and all(fragment in memo for fragment in fragments):
            return True
    return False


def signal_recently_exists(supabase, ticker, action, channel=None, cooldown_minutes=None):
    """Best-effort duplicate guard for repeated scanner loops."""
    minutes = cooldown_minutes or SIGNAL_COOLDOWN_MINUTES
    threshold = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    try:
        response = (
            matching_signal_query(supabase, ticker, action, channel)
            .gte("created_at", threshold.isoformat())
            .limit(100)
            .execute()
        )
        for row in response.data or []:
            created_at = parse_supabase_datetime(row.get("created_at"))
            if created_at and created_at >= threshold:
                return True
        return False
    except Exception as exc:
        print(f"Duplicate-signal check failed for {ticker}: {exc}")
        return False


def insert_signal_with_cooldown(
    supabase,
    payload,
    channel=None,
    cooldown_minutes=None,
    context_fragments=None,
):
    ticker = payload.get("ticker")
    action = payload.get("action_type")
    minutes = cooldown_minutes or SIGNAL_COOLDOWN_MINUTES

    try:
        active_status = active_signal_exists(supabase, ticker, action, channel)
        if active_status:
            print(f"Skipping duplicate {action} signal for {ticker}; existing {active_status} signal is still active.")
            return False

        if same_news_context_exists(supabase, ticker, action, channel, context_fragments):
            print(f"Skipping duplicate {action} signal for {ticker}; news context has not changed.")
            return False
    except Exception as exc:
        print(f"Extended duplicate-signal check failed for {ticker}: {exc}")

    if signal_recently_exists(supabase, ticker, action, channel, minutes):
        print(f"Skipping duplicate {action} signal for {ticker}; cooldown is {minutes} minutes.")
        return False

    response = supabase.table("market_signals").insert(payload).execute()
    try:
        from signal_journal import record_signal

        record_signal(payload, response)
    except Exception as exc:
        print(f"Signal journal write failed for {ticker}: {exc}")
    return True
