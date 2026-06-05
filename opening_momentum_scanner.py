import argparse
import os
import time
from pathlib import Path
from statistics import median

import pandas as pd
import pytz
import yfinance as yf

from config import (
    MARKET_TIMEZONE,
    OPEN_SCANNER_COOLDOWN_MINUTES,
    OPEN_SCANNER_ENABLED,
    OPEN_SCANNER_END,
    OPEN_SCANNER_MAX_TICKERS,
    OPEN_SCANNER_MIN_CONFIDENCE,
    OPEN_SCANNER_MIN_MOVE_PCT,
    OPEN_SCANNER_MIN_PRICE,
    OPEN_SCANNER_MIN_RECOVERY_PCT,
    OPEN_SCANNER_MIN_RVOL,
    OPEN_SCANNER_MINUTES_AFTER_OPEN,
    OPEN_SCANNER_START,
    get_supabase_client,
)
from market_session import now_market_time, parse_time
from signal_utils import insert_signal_with_cooldown

CHANNEL = "OPENING_MOMENTUM"
INDEX_TICKERS = ["SPY", "QQQ", "IWM"]


def load_daily_targets():
    target_path = Path(__file__).resolve().with_name("daily_targets.txt")
    if not target_path.exists():
        print("No daily_targets.txt found. Run python master_scanner.py categories first.")
        return []

    content = target_path.read_text(encoding="utf-8").strip()
    if not content:
        return []

    tickers = []
    seen = set()
    for item in content.split(","):
        ticker = item.strip().upper()
        if ticker and ticker not in seen:
            tickers.append(ticker)
            seen.add(ticker)
    return tickers


def market_tz():
    return pytz.timezone(MARKET_TIMEZONE)


def combine_date_time(current, time_text):
    parsed = parse_time(time_text)
    return current.replace(
        hour=parsed.hour,
        minute=parsed.minute,
        second=0,
        microsecond=0,
    )


def is_opening_scan_ready(now=None):
    current = now or now_market_time()
    if current.weekday() >= 5:
        return False

    start = combine_date_time(current, OPEN_SCANNER_START)
    ready_at = start + pd.Timedelta(minutes=max(0, OPEN_SCANNER_MINUTES_AFTER_OPEN))
    end = combine_date_time(current, OPEN_SCANNER_END)
    return ready_at <= current <= end


def opening_window_label(now=None):
    current = now or now_market_time()
    start = combine_date_time(current, OPEN_SCANNER_START)
    ready_at = start + pd.Timedelta(minutes=max(0, OPEN_SCANNER_MINUTES_AFTER_OPEN))
    end = combine_date_time(current, OPEN_SCANNER_END)
    return f"{ready_at.strftime('%I:%M %p')} - {end.strftime('%I:%M %p')} {MARKET_TIMEZONE}"


def normalize_frame_index(frame):
    if frame is None or frame.empty:
        return pd.DataFrame()

    result = frame.copy()
    index = pd.to_datetime(result.index)
    tz = market_tz()
    if index.tz is None:
        index = index.tz_localize(tz)
    else:
        index = index.tz_convert(tz)
    result.index = index
    return result


def extract_ticker_frame(raw, ticker, ticker_count):
    if raw is None or raw.empty:
        return pd.DataFrame()

    frame = pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        level_zero = [str(value).upper() for value in raw.columns.get_level_values(0)]
        level_one = [str(value).upper() for value in raw.columns.get_level_values(1)]
        ticker_upper = ticker.upper()

        if ticker_upper in level_zero:
            frame = raw[ticker].copy()
        elif ticker_upper in level_one:
            frame = raw.xs(ticker, axis=1, level=1).copy()
    elif ticker_count == 1:
        frame = raw.copy()

    if frame.empty:
        return pd.DataFrame()

    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(-1)

    frame.columns = [str(column).title() for column in frame.columns]
    if "Close" not in frame.columns:
        return pd.DataFrame()

    return normalize_frame_index(frame.dropna(subset=["Close"]))


def download_intraday(tickers):
    return yf.download(
        tickers=tickers,
        period="5d",
        interval="1m",
        group_by="ticker",
        auto_adjust=False,
        prepost=False,
        progress=False,
        threads=True,
    )


def download_daily(tickers):
    return yf.download(
        tickers=tickers,
        period="7d",
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        prepost=False,
        progress=False,
        threads=True,
    )


def current_day_frame(frame, current_date):
    if frame.empty:
        return pd.DataFrame()
    return frame[frame.index.date == current_date]


def previous_close(daily_frame, current_date):
    if daily_frame.empty or "Close" not in daily_frame.columns:
        return None

    frame = normalize_frame_index(daily_frame)
    prior = frame[frame.index.date < current_date]
    if prior.empty:
        return None

    value = prior["Close"].dropna().iloc[-1]
    return float(value) if pd.notna(value) else None


def pct_change(value, base):
    if base is None or base == 0 or value is None:
        return 0.0
    return ((float(value) - float(base)) / float(base)) * 100.0


def calculate_vwap(frame):
    volume = frame.get("Volume")
    close = frame.get("Close")
    if volume is None or close is None or frame.empty:
        return float(close.iloc[-1]) if close is not None and not close.empty else None

    volume_sum = float(volume.fillna(0).sum())
    if volume_sum <= 0:
        return float(close.iloc[-1])
    return float((close * volume.fillna(0)).sum() / volume_sum)


def calculate_open_rvol(full_frame, today_frame, current_date):
    if full_frame.empty or today_frame.empty or "Volume" not in full_frame.columns:
        return 1.0

    elapsed_rows = max(1, len(today_frame))
    today_volume = float(today_frame["Volume"].fillna(0).sum())
    if today_volume <= 0:
        return 1.0

    prior_volumes = []
    for day, group in full_frame[full_frame.index.date < current_date].groupby(full_frame[full_frame.index.date < current_date].index.date):
        sample = group.head(elapsed_rows)
        volume = float(sample.get("Volume", pd.Series(dtype=float)).fillna(0).sum())
        if volume > 0:
            prior_volumes.append(volume)

    if not prior_volumes:
        return 1.0

    baseline = median(prior_volumes)
    if baseline <= 0:
        return 1.0
    return round(today_volume / baseline, 2)


def score_candidate(setup, metrics):
    rvol_points = min(12.0, max(0.0, metrics["rvol"] - 1.0) * 5.0)
    vwap_points = min(6.0, max(0.0, metrics["vwap_delta_pct"]) * 3.0)
    relative_points = min(8.0, max(0.0, metrics["relative_strength_pct"]) * 3.0)

    if setup == "dip_reversal":
        move_points = min(12.0, abs(min(metrics["gap_pct"], metrics["low_from_open_pct"])) * 2.0)
        recovery_points = min(10.0, metrics["recovery_from_low_pct"] * 4.0)
        score = 56.0 + rvol_points + move_points + recovery_points + vwap_points + relative_points
    elif setup == "strength_continuation":
        move_points = min(14.0, max(metrics["gap_pct"], metrics["open_move_pct"], metrics["high_from_open_pct"]) * 2.5)
        hold_points = min(8.0, max(0.0, 0.5 + metrics["pullback_from_high_pct"]) * 5.0)
        score = 57.0 + rvol_points + move_points + hold_points + vwap_points + relative_points
    else:
        move_points = min(14.0, abs(min(metrics["open_move_pct"], metrics["low_from_open_pct"])) * 2.5)
        weakness_points = min(8.0, abs(min(0.0, metrics["relative_strength_pct"])) * 3.0)
        below_vwap_points = min(6.0, abs(min(0.0, metrics["vwap_delta_pct"])) * 3.0)
        score = 56.0 + rvol_points + move_points + weakness_points + below_vwap_points

    return round(max(0.0, min(100.0, score)), 2)


def detect_opening_setup(ticker, full_frame, daily_frame, market_move_pct, current_date):
    today = current_day_frame(full_frame, current_date)
    if len(today) < 3:
        return None

    open_price = float(today["Open"].dropna().iloc[0]) if "Open" in today.columns else float(today["Close"].iloc[0])
    current_price = float(today["Close"].dropna().iloc[-1])
    high_price = float(today["High"].dropna().max()) if "High" in today.columns else current_price
    low_price = float(today["Low"].dropna().min()) if "Low" in today.columns else current_price

    if current_price < OPEN_SCANNER_MIN_PRICE:
        return None

    prev_close = previous_close(daily_frame, current_date) or open_price
    vwap = calculate_vwap(today) or current_price
    rvol = calculate_open_rvol(full_frame, today, current_date)

    gap_pct = pct_change(open_price, prev_close)
    open_move_pct = pct_change(current_price, open_price)
    high_from_open_pct = pct_change(high_price, open_price)
    low_from_open_pct = pct_change(low_price, open_price)
    recovery_from_low_pct = pct_change(current_price, low_price)
    pullback_from_high_pct = pct_change(current_price, high_price)
    vwap_delta_pct = pct_change(current_price, vwap)
    relative_strength_pct = open_move_pct - market_move_pct

    metrics = {
        "price": round(current_price, 2),
        "open_price": round(open_price, 2),
        "prev_close": round(prev_close, 2),
        "vwap": round(vwap, 2),
        "gap_pct": round(gap_pct, 2),
        "open_move_pct": round(open_move_pct, 2),
        "high_from_open_pct": round(high_from_open_pct, 2),
        "low_from_open_pct": round(low_from_open_pct, 2),
        "recovery_from_low_pct": round(recovery_from_low_pct, 2),
        "pullback_from_high_pct": round(pullback_from_high_pct, 2),
        "vwap_delta_pct": round(vwap_delta_pct, 2),
        "relative_strength_pct": round(relative_strength_pct, 2),
        "market_move_pct": round(market_move_pct, 2),
        "rvol": rvol,
        "bars": len(today),
    }

    min_move = max(0.1, OPEN_SCANNER_MIN_MOVE_PCT)
    min_recovery = max(0.1, OPEN_SCANNER_MIN_RECOVERY_PCT)

    if rvol < OPEN_SCANNER_MIN_RVOL:
        return None

    if (
        (gap_pct <= -0.6 or low_from_open_pct <= -min_move)
        and recovery_from_low_pct >= min_recovery
        and vwap_delta_pct >= -0.15
        and relative_strength_pct >= -0.75
    ):
        setup = "dip_reversal"
        action = "BUY"
        title = "Opening dip reversal"
    elif (
        (gap_pct >= min_move or open_move_pct >= min_move or high_from_open_pct >= min_move)
        and vwap_delta_pct >= 0
        and pullback_from_high_pct >= -0.45
        and relative_strength_pct >= 0.35
    ):
        setup = "strength_continuation"
        action = "BUY"
        title = "Opening strength continuation"
    elif (
        open_move_pct <= -min_move
        and recovery_from_low_pct <= max(0.25, min_recovery * 0.75)
        and vwap_delta_pct <= -0.2
        and relative_strength_pct <= -0.5
    ):
        setup = "breakdown"
        action = "SELL"
        title = "Opening breakdown"
    else:
        return None

    confidence = score_candidate(setup, metrics)
    if confidence < OPEN_SCANNER_MIN_CONFIDENCE:
        return None

    metrics["confidence"] = confidence
    return {
        "ticker": ticker,
        "action": action,
        "setup": setup,
        "title": title,
        "metrics": metrics,
    }


def build_signal_payload(candidate):
    ticker = candidate["ticker"]
    action = candidate["action"]
    metrics = candidate["metrics"]
    memo = (
        f"{candidate['title']} detected by the opening-bell scanner.\n\n"
        f"Opening metrics for {ticker}:\n"
        f"- Price: ${metrics['price']:.2f} | Open: ${metrics['open_price']:.2f} | Prev close: ${metrics['prev_close']:.2f}\n"
        f"- Gap: {metrics['gap_pct']:.2f}% | Move from open: {metrics['open_move_pct']:.2f}% | Market move: {metrics['market_move_pct']:.2f}%\n"
        f"- Low from open: {metrics['low_from_open_pct']:.2f}% | Recovery from low: {metrics['recovery_from_low_pct']:.2f}% | Pullback from high: {metrics['pullback_from_high_pct']:.2f}%\n"
        f"- VWAP: ${metrics['vwap']:.2f} | VWAP delta: {metrics['vwap_delta_pct']:.2f}% | Opening RVOL: {metrics['rvol']:.2f}x\n\n"
        "This signal is meant for the volatile regular-market open. "
        "The execution bridge and Qwen execution gate still perform the final routing review before any IBKR order is sent."
    )

    price = metrics["price"]
    return {
        "ticker": ticker,
        "action_type": action,
        "confidence_score": metrics["confidence"],
        "investment_memo": memo,
        "status": "pending",
        "price_at_signal": price,
        "rvol": metrics["rvol"],
        "bid": round(price * 0.999, 2),
        "ask": round(price * 1.001, 2),
        "channel": CHANNEL,
    }


def calculate_market_move(intraday_raw, current_date):
    moves = []
    for ticker in INDEX_TICKERS:
        frame = extract_ticker_frame(intraday_raw, ticker, len(INDEX_TICKERS))
        today = current_day_frame(frame, current_date)
        if len(today) < 2:
            continue
        open_price = float(today["Open"].dropna().iloc[0]) if "Open" in today.columns else float(today["Close"].iloc[0])
        current_price = float(today["Close"].dropna().iloc[-1])
        moves.append(pct_change(current_price, open_price))
    if not moves:
        return 0.0
    return round(sum(moves) / len(moves), 2)


def run_opening_momentum_scan(force=False, dry_run=False, max_tickers=None):
    print("[OPEN SCANNER] Opening-bell momentum scanner online.")
    print(f"[OPEN SCANNER] Window: {opening_window_label()}")
    print(
        "[OPEN SCANNER] Thresholds: "
        f"min_move={OPEN_SCANNER_MIN_MOVE_PCT:.2f}% "
        f"min_recovery={OPEN_SCANNER_MIN_RECOVERY_PCT:.2f}% "
        f"min_rvol={OPEN_SCANNER_MIN_RVOL:.2f}x "
        f"min_confidence={OPEN_SCANNER_MIN_CONFIDENCE}"
    )

    if not OPEN_SCANNER_ENABLED and not force:
        print("[OPEN SCANNER] Disabled by OPEN_SCANNER_ENABLED=false.")
        return True

    if not force and not is_opening_scan_ready():
        print("[OPEN SCANNER] Outside the configured opening window; skipping.")
        return True

    watchlist = load_daily_targets()
    if not watchlist:
        print("[OPEN SCANNER] No daily category targets loaded.")
        return False

    ticker_limit = max_tickers or OPEN_SCANNER_MAX_TICKERS
    if ticker_limit > 0:
        watchlist = watchlist[:ticker_limit]

    current = now_market_time()
    current_date = current.date()
    tickers_for_market = list(dict.fromkeys([*watchlist, *INDEX_TICKERS]))

    print(f"[OPEN SCANNER] Downloading 1-minute open data for {len(watchlist)} target ticker(s)...")
    intraday_raw = download_intraday(tickers_for_market)
    daily_raw = download_daily(watchlist)
    market_move_pct = calculate_market_move(intraday_raw, current_date)
    print(f"[OPEN SCANNER] Opening market baseline move: {market_move_pct:.2f}%")

    candidates = []
    for ticker in watchlist:
        intraday_frame = extract_ticker_frame(intraday_raw, ticker, len(tickers_for_market))
        daily_frame = extract_ticker_frame(daily_raw, ticker, len(watchlist))
        candidate = detect_opening_setup(ticker, intraday_frame, daily_frame, market_move_pct, current_date)
        if candidate:
            candidates.append(candidate)

    if not candidates:
        print("[OPEN SCANNER] No opening-bell setups met the current filters.")
        return True

    candidates.sort(key=lambda item: item["metrics"]["confidence"], reverse=True)
    print(f"[OPEN SCANNER] {len(candidates)} candidate setup(s) passed filters.")

    if dry_run:
        for candidate in candidates:
            metrics = candidate["metrics"]
            print(
                f"DRY RUN: {candidate['action']} {candidate['ticker']} "
                f"{candidate['title']} confidence={metrics['confidence']:.1f} "
                f"price=${metrics['price']:.2f} rvol={metrics['rvol']:.2f}x"
            )
        return True

    supabase = get_supabase_client()
    sent = 0
    for candidate in candidates:
        payload = build_signal_payload(candidate)
        try:
            if insert_signal_with_cooldown(
                supabase,
                payload,
                channel=CHANNEL,
                cooldown_minutes=OPEN_SCANNER_COOLDOWN_MINUTES,
                context_fragments=[candidate["setup"]],
            ):
                sent += 1
                print(
                    f"[OPEN SCANNER] SIGNAL TRANSMITTED: "
                    f"{payload['action_type']} {payload['ticker']} "
                    f"confidence={payload['confidence_score']:.1f}"
                )
        except Exception as exc:
            print(f"[OPEN SCANNER] Supabase upload error for {candidate['ticker']}: {exc}")
        time.sleep(0.2)

    print(f"[OPEN SCANNER] Complete. Sent {sent}/{len(candidates)} signal(s).")
    return True


def parse_args():
    parser = argparse.ArgumentParser(description="Opening-bell momentum scanner.")
    parser.add_argument("--force", action="store_true", help="Run even outside the configured opening window.")
    parser.add_argument("--dry-run", action="store_true", help="Print candidates without writing to Supabase.")
    parser.add_argument("--max-tickers", type=int, default=None, help="Limit the number of daily targets scanned.")
    return parser.parse_args()


def main():
    args = parse_args()
    success = run_opening_momentum_scan(
        force=args.force,
        dry_run=args.dry_run,
        max_tickers=args.max_tickers,
    )
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
