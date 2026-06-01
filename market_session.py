from dataclasses import dataclass
from datetime import datetime, time

import pytz

from config import (
    AFTER_HOURS_CLOSE,
    GLOBAL_OVERNIGHT_CLOSE,
    GLOBAL_OVERNIGHT_OPEN,
    MARKET_TIMEZONE,
    PREMARKET_OPEN,
    REGULAR_MARKET_CLOSE,
    REGULAR_MARKET_OPEN,
    SCAN_SUNDAY_NIGHT,
)


@dataclass(frozen=True)
class MarketSession:
    name: str
    is_open: bool
    is_regular: bool
    is_extended: bool
    is_global_overnight: bool


def parse_time(value):
    hour, minute = str(value).split(":", maxsplit=1)
    return time(hour=int(hour), minute=int(minute))


def now_market_time():
    return datetime.now(pytz.timezone(MARKET_TIMEZONE))


def get_market_session(now=None):
    current = now or now_market_time()
    current_time = current.time()
    premarket_open = parse_time(PREMARKET_OPEN)
    regular_open = parse_time(REGULAR_MARKET_OPEN)
    regular_close = parse_time(REGULAR_MARKET_CLOSE)
    after_hours_close = parse_time(AFTER_HOURS_CLOSE)
    global_overnight_open = parse_time(GLOBAL_OVERNIGHT_OPEN)
    global_overnight_close = parse_time(GLOBAL_OVERNIGHT_CLOSE)

    if is_global_overnight_time(current, current_time, global_overnight_open, global_overnight_close):
        return MarketSession("global_overnight", True, False, False, True)

    if current.weekday() >= 5:
        return MarketSession("closed", False, False, False, False)

    if premarket_open <= current_time < regular_open:
        return MarketSession("premarket", True, False, True, False)
    if regular_open <= current_time <= regular_close:
        return MarketSession("regular", True, True, False, False)
    if regular_close < current_time <= after_hours_close:
        return MarketSession("after_hours", True, False, True, False)
    return MarketSession("closed", False, False, False, False)


def is_global_overnight_time(current, current_time, session_open, session_close):
    """Sunday-Thursday evening through the next early morning, configurable."""
    weekday = current.weekday()

    if session_open < session_close:
        if session_open <= current_time <= session_close:
            return weekday in {0, 1, 2, 3, 6}
        return False

    if current_time >= session_open:
        if weekday == 6 and not SCAN_SUNDAY_NIGHT:
            return False
        return weekday in {0, 1, 2, 3, 6}
    if current_time < session_close:
        if weekday == 0 and not SCAN_SUNDAY_NIGHT:
            return False
        return weekday in {0, 1, 2, 3, 4}
    return False


def is_regular_market_open(now=None):
    return get_market_session(now).is_regular


def is_extended_market_open(now=None):
    return get_market_session(now).is_extended


def is_any_trading_session_open(now=None, include_extended=True):
    session = get_market_session(now)
    if session.is_regular:
        return True
    return include_extended and (session.is_extended or session.is_global_overnight)
