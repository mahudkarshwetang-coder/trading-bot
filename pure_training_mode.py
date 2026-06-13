import argparse
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf
from ib_insync import IB, LimitOrder, MarketOrder, Stock, StopOrder

from broker_sync import build_position_snapshot, sync_once
from config import (
    ALLOW_EXTENDED_HOURS_TRADING,
    ALLOW_GLOBAL_OVERNIGHT_TRADING,
    BRACKET_STOP_LOSS_PCT,
    BRACKET_TAKE_PROFIT_PCT,
    DRY_RUN,
    EXPERIMENTAL_LOCAL_LOG_PATH,
    EXPERIMENTAL_MIN_MONTHLY_VOLATILITY_PCT,
    EXPERIMENTAL_ORDER_REF_PREFIX,
    EXPERIMENTAL_SESSION_DUPLICATE_GUARD,
    EXPERIMENTAL_USE_IBKR_MARKET_DATA,
    IBKR_EXPERIMENTAL_CLIENT_ID,
    IBKR_HOST,
    IBKR_PORT,
    PURE_TRAINING_LOCAL_LOG_PATH,
    PURE_TRAINING_MAX_ACCOUNT_CAD,
    PURE_TRAINING_MAX_POSITION_PER_TICKER,
    PURE_TRAINING_MAX_TICKERS_PER_RUN,
    PURE_TRAINING_MIN_CASH_BUFFER_CAD,
    PURE_TRAINING_MODE_ENABLED,
    PURE_TRAINING_ORDER_QUANTITY,
    PURE_TRAINING_ORDER_REF_PREFIX,
    PURE_TRAINING_SYNC_EVERY_ORDERS,
    PURE_TRAINING_USD_CAD_RATE,
    STOP_OUTSIDE_RTH,
    get_supabase_client,
)
from local_data_recorder import append_local_event
from market_session import get_market_session
from system_status import publish_system_status


SOURCE = "pure_training_mode"
SETTINGS_TABLE = "bot_settings"


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean_ticker(value):
    ticker = str(value or "").strip().upper()
    return "".join(char for char in ticker if char.isalnum() or char in {".", "-"})


def finite_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def append_training_event(event_type, payload):
    append_local_event(
        event_type,
        payload,
        source=SOURCE,
        path=EXPERIMENTAL_LOCAL_LOG_PATH,
    )


def load_shortlist(supabase):
    tickers = []
    try:
        response = supabase.table(SETTINGS_TABLE).select("watchlist").eq("id", 1).limit(1).execute()
        rows = response.data or []
        if rows and isinstance(rows[0].get("watchlist"), list):
            tickers.extend(rows[0]["watchlist"])
    except Exception as exc:
        print(f"[EXPERIMENTAL] Supabase watchlist lookup skipped: {exc}")

    if not tickers:
        target_path = Path("daily_targets.txt")
        if target_path.exists():
            tickers.extend(target_path.read_text(encoding="utf-8").replace("\n", ",").split(","))

    cleaned = []
    seen = set()
    for raw in tickers:
        ticker = clean_ticker(raw)
        if not ticker or ticker in seen:
            continue
        cleaned.append(ticker)
        seen.add(ticker)

    if PURE_TRAINING_MAX_TICKERS_PER_RUN > 0:
        cleaned = cleaned[:PURE_TRAINING_MAX_TICKERS_PER_RUN]
    return cleaned


def connect_to_ibkr():
    last_error = None
    for offset in range(10):
        client_id = IBKR_EXPERIMENTAL_CLIENT_ID + offset
        ib = IB()
        try:
            ib.connect(
                IBKR_HOST,
                IBKR_PORT,
                clientId=client_id,
                readonly=False,
                timeout=15,
            )
            ib.reqMarketDataType(4)
            print(f"[EXPERIMENTAL] Connected to IBKR on {IBKR_HOST}:{IBKR_PORT} with clientId={client_id}.")
            return ib
        except Exception as exc:
            last_error = exc
            try:
                ib.disconnect()
            except Exception:
                pass
            print(f"[EXPERIMENTAL] IBKR clientId={client_id} unavailable: {exc}")
            time.sleep(0.5)
    raise RuntimeError(f"Could not connect to IBKR using client IDs {IBKR_EXPERIMENTAL_CLIENT_ID}-{IBKR_EXPERIMENTAL_CLIENT_ID + 9}: {last_error}")


def account_summary(ib):
    values = {}
    for item in ib.accountSummary():
        tag = item.tag
        currency = item.currency or "BASE"
        value = finite_float(item.value)
        if value is None:
            continue
        values.setdefault(tag, {})[currency] = value
    return values


def preferred_account_value(summary, tag):
    values = summary.get(tag) or {}
    for currency in ("CAD", "BASE", "USD"):
        value = values.get(currency)
        if value is not None:
            return value, currency
    if values:
        currency, value = next(iter(values.items()))
        return value, currency
    return 0.0, "UNKNOWN"


def current_positions_by_ticker(ib):
    positions = {}
    for item in ib.positions():
        ticker = clean_ticker(getattr(item.contract, "symbol", ""))
        if not ticker:
            continue
        positions[ticker] = positions.get(ticker, 0.0) + float(item.position or 0.0)
    return positions


def current_open_buy_orders_by_ticker(ib):
    tickers = set()
    try:
        ib.reqOpenOrders()
        ib.sleep(0.5)
        for trade in ib.openTrades():
            ticker = clean_ticker(getattr(trade.contract, "symbol", ""))
            action = str(getattr(trade.order, "action", "") or "").upper()
            status = str(getattr(trade.orderStatus, "status", "") or "").lower()
            if ticker and action == "BUY" and status not in {"cancelled", "inactive", "filled"}:
                tickers.add(ticker)
    except Exception as exc:
        print(f"[EXPERIMENTAL] Open order duplicate guard skipped: {exc}")
    return tickers


def monthly_volatility_pct(ticker):
    try:
        history = yf.Ticker(ticker).history(period="1mo", interval="1d")
        if history.empty or not {"High", "Low", "Close"}.issubset(history.columns):
            return None

        high = finite_float(history["High"].dropna().max())
        low = finite_float(history["Low"].dropna().min())
        close = finite_float(history["Close"].dropna().iloc[-1])
        if not high or not low or not close or close <= 0:
            return None
        return round(((high - low) / close) * 100.0, 2)
    except Exception as exc:
        print(f"[EXPERIMENTAL] Monthly volatility lookup failed for {ticker}: {exc}")
        return None


def estimate_current_exposure(ib, positions):
    exposure = 0.0
    for ticker, quantity in positions.items():
        if quantity <= 0:
            continue
        contract = Stock(ticker, "SMART", "USD")
        try:
            ib.qualifyContracts(contract)
        except Exception:
            continue
        price, _ = get_market_price(ib, contract)
        if price:
            exposure += abs(quantity) * price
    return exposure


def usd_to_cad(value):
    return float(value or 0.0) * max(0.0, float(PURE_TRAINING_USD_CAD_RATE or 0.0))


def get_yahoo_market_price(symbol):
    try:
        yf_ticker = yf.Ticker(symbol)
        fast_info = getattr(yf_ticker, "fast_info", {}) or {}
        for key in ("last_price", "regular_market_price", "previous_close"):
            price = finite_float(fast_info.get(key) if hasattr(fast_info, "get") else None)
            if price and price > 0:
                return price, f"yfinance:{key}"

        for period, interval in (("1d", "1m"), ("5d", "1d")):
            history = yf_ticker.history(period=period, interval=interval)
            if not history.empty and "Close" in history.columns:
                price = finite_float(history["Close"].dropna().iloc[-1])
                if price and price > 0:
                    return price, f"yfinance:history:{period}:{interval}"
    except Exception as exc:
        print(f"[EXPERIMENTAL] Yahoo price lookup failed for {symbol}: {exc}")
    return None, None


def get_ibkr_market_price(ib, contract):
    ticker = None
    try:
        ticker = ib.reqMktData(contract, "", False, False)
        ib.sleep(1.0)
        price = finite_float(ticker.marketPrice()) or finite_float(ticker.last) or finite_float(ticker.close)
        if price and price > 0:
            return price, "ibkr"
    except Exception as exc:
        print(f"[EXPERIMENTAL] IBKR price lookup failed for {contract.symbol}: {exc}")
    finally:
        if ticker is not None:
            try:
                ib.cancelMktData(contract)
            except Exception:
                pass

    return None, None


def get_market_price(ib, contract):
    price, source = get_yahoo_market_price(contract.symbol)
    if price:
        return price, source

    if EXPERIMENTAL_USE_IBKR_MARKET_DATA:
        return get_ibkr_market_price(ib, contract)

    return None, None


def build_training_bracket(ib, quantity, baseline_price, run_id, ticker):
    parent_order_id = ib.client.getReqId()
    take_profit, stop_loss = training_bracket_prices(baseline_price)
    order_ref = f"{EXPERIMENTAL_ORDER_REF_PREFIX}-{run_id[:8]}-{ticker}"
    parent = MarketOrder(
        "BUY",
        quantity,
        orderId=parent_order_id,
        transmit=False,
        tif="GTC",
    )
    parent.outsideRth = ALLOW_EXTENDED_HOURS_TRADING or ALLOW_GLOBAL_OVERNIGHT_TRADING
    parent.orderRef = order_ref

    take_profit_order = LimitOrder(
        "SELL",
        quantity,
        take_profit,
        orderId=ib.client.getReqId(),
        parentId=parent_order_id,
        transmit=False,
        tif="GTC",
    )
    take_profit_order.outsideRth = parent.outsideRth
    take_profit_order.orderRef = order_ref

    stop_loss_order = StopOrder(
        "SELL",
        quantity,
        stop_loss,
        orderId=ib.client.getReqId(),
        parentId=parent_order_id,
        transmit=True,
        tif="GTC",
    )
    stop_loss_order.outsideRth = STOP_OUTSIDE_RTH
    stop_loss_order.orderRef = order_ref
    return [parent, take_profit_order, stop_loss_order], take_profit, stop_loss


def training_bracket_prices(baseline_price):
    take_profit = round(baseline_price * (1 + BRACKET_TAKE_PROFIT_PCT / 100.0), 2)
    stop_loss = round(baseline_price * (1 - BRACKET_STOP_LOSS_PCT / 100.0), 2)
    return take_profit, stop_loss


def record_trade_event(supabase, payload):
    append_training_event("pure_training_trade_event", payload)
    try:
        supabase.table("trade_events").insert(payload).execute()
    except Exception as exc:
        print(f"[EXPERIMENTAL] Supabase trade event skipped for {payload.get('ticker')}: {exc}")


def update_force_flag(supabase, value):
    payload = {
        "force_pure_training_run": value,
        "force_experimental_run": value,
    }
    try:
        supabase.table(SETTINGS_TABLE).update(payload).eq("id", 1).execute()
    except Exception as exc:
        print(f"[EXPERIMENTAL] Could not update force flag: {exc}")


def update_run_result(supabase, result):
    finished_at = result.get("finished_at") or utc_now_iso()
    try:
        supabase.table(SETTINGS_TABLE).update(
            {
                "pure_training_last_result": result,
                "pure_training_last_requested_at": finished_at,
                "experimental_last_result": result,
                "experimental_last_requested_at": finished_at,
            }
        ).eq("id", 1).execute()
    except Exception as exc:
        print(f"[EXPERIMENTAL] Could not update run result: {exc}")


def run_pure_training_monitor(dry_run=False):
    started_at = utc_now_iso()
    supabase = get_supabase_client()
    print("[EXPERIMENTAL] Broker monitor snapshot starting.")
    ib = connect_to_ibkr()
    try:
        summary = account_summary(ib)
        net_liq, net_currency = preferred_account_value(summary, "NetLiquidation")
        available_funds, funds_currency = preferred_account_value(summary, "AvailableFunds")
        snapshots = build_position_snapshot(ib)
        open_positions = [row for row in snapshots if row.get("is_open")]
        exposure_usd = sum(abs(finite_float(row.get("market_value")) or 0.0) for row in open_positions)
        unrealized_pnl = sum(finite_float(row.get("unrealized_pnl")) or 0.0 for row in open_positions)
        payload = {
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "dry_run": bool(dry_run),
            "net_liquidation": net_liq,
            "net_liquidation_currency": net_currency,
            "available_funds": available_funds,
            "available_funds_currency": funds_currency,
            "open_position_count": len(open_positions),
            "exposure_usd": exposure_usd,
            "exposure_cad_estimate": usd_to_cad(exposure_usd),
            "unrealized_pnl": unrealized_pnl,
            "positions": open_positions,
        }
        append_training_event("pure_training_monitor_snapshot", payload)
        publish_system_status(
            "pure_training_monitor",
            "success",
            detail=f"Pure training monitor synced {len(open_positions)} open position(s).",
            metadata={
                "open_position_count": len(open_positions),
                "exposure_cad_estimate": payload["exposure_cad_estimate"],
                "unrealized_pnl": unrealized_pnl,
                "dry_run": bool(dry_run),
            },
        )
        print(
            f"[EXPERIMENTAL] Monitor: positions={len(open_positions)} "
            f"exposure={payload['exposure_cad_estimate']:,.2f} CAD est "
            f"unrealized_pnl={unrealized_pnl:,.2f}."
        )
        sync_once(ib, supabase, mark_signals=False, dry_run=dry_run)
        return True
    finally:
        ib.disconnect()


def run_pure_training_mode(dry_run=None, tickers=None, reset_force_flag=True, execution_mode="execute"):
    build_only = execution_mode == "build"
    effective_dry_run = True if build_only else (DRY_RUN if dry_run is None else bool(dry_run))
    run_id = uuid.uuid4().hex
    started_at = utc_now_iso()
    supabase = get_supabase_client()

    if reset_force_flag:
        update_force_flag(supabase, False)

    shortlist = [clean_ticker(ticker) for ticker in (tickers or load_shortlist(supabase))]
    shortlist = [ticker for ticker in shortlist if ticker]
    print("[EXPERIMENTAL] Basket builder online." if build_only else "[EXPERIMENTAL] Basket executor online.")
    print(
        f"[EXPERIMENTAL] Qty={PURE_TRAINING_ORDER_QUANTITY} SL={BRACKET_STOP_LOSS_PCT:.2f}% "
        f"TP={BRACKET_TAKE_PROFIT_PCT:.2f}% cap={PURE_TRAINING_MAX_ACCOUNT_CAD:,.0f} CAD "
        f"FX={PURE_TRAINING_USD_CAD_RATE:.2f} min_monthly_vol={EXPERIMENTAL_MIN_MONTHLY_VOLATILITY_PCT:.2f}%"
    )
    print(f"[EXPERIMENTAL] Loaded {len(shortlist)} shortlisted ticker(s).")

    append_training_event(
        "experimental_run_started",
        {
            "run_id": run_id,
            "started_at": started_at,
            "dry_run": effective_dry_run,
            "execution_mode": execution_mode,
            "mode_enabled": PURE_TRAINING_MODE_ENABLED,
            "profile": "experimental",
            "tickers": shortlist,
            "quantity": PURE_TRAINING_ORDER_QUANTITY,
            "stop_loss_percent": BRACKET_STOP_LOSS_PCT,
            "take_profit_percent": BRACKET_TAKE_PROFIT_PCT,
            "max_account_cad": PURE_TRAINING_MAX_ACCOUNT_CAD,
            "usd_cad_rate": PURE_TRAINING_USD_CAD_RATE,
            "min_monthly_volatility_pct": EXPERIMENTAL_MIN_MONTHLY_VOLATILITY_PCT,
            "session_duplicate_guard": EXPERIMENTAL_SESSION_DUPLICATE_GUARD,
        },
    )

    if not shortlist:
        print("[EXPERIMENTAL] No shortlisted tickers found. Run category-universe/categories first.")
        update_run_result(
            supabase,
            {
                "run_id": run_id,
                "status": "skipped",
                "reason": "no_shortlist",
                "finished_at": utc_now_iso(),
                "dry_run": effective_dry_run,
                "execution_mode": execution_mode,
            },
        )
        return False

    ib = connect_to_ibkr()
    sent = 0
    skipped = 0
    candidates = 0
    planned_notional = 0.0
    outcomes = []
    try:
        summary = account_summary(ib)
        net_liq, net_currency = preferred_account_value(summary, "NetLiquidation")
        available_funds, funds_currency = preferred_account_value(summary, "AvailableFunds")
        positions = current_positions_by_ticker(ib)
        open_buy_orders = current_open_buy_orders_by_ticker(ib)
        current_exposure = estimate_current_exposure(ib, positions)
        current_exposure_cad = usd_to_cad(current_exposure)
        print(
            f"[EXPERIMENTAL] Account net liquidation: {net_liq:,.2f} {net_currency}; "
            f"available funds: {available_funds:,.2f} {funds_currency}"
        )
        print(
            f"[EXPERIMENTAL] Estimated open long exposure: "
            f"{current_exposure:,.2f} USD / {current_exposure_cad:,.2f} CAD."
        )

        if available_funds <= PURE_TRAINING_MIN_CASH_BUFFER_CAD:
            print("[EXPERIMENTAL] Available funds are below the configured cash buffer; no orders sent.")
            update_run_result(
                supabase,
                {
                    "run_id": run_id,
                    "status": "blocked",
                    "reason": "cash_buffer",
                    "available_funds": available_funds,
                    "currency": funds_currency,
                    "finished_at": utc_now_iso(),
                    "dry_run": effective_dry_run,
                    "execution_mode": execution_mode,
                },
            )
            return False

        for ticker in shortlist:
            held = positions.get(ticker, 0.0)
            if EXPERIMENTAL_SESSION_DUPLICATE_GUARD and (held > 0 or ticker in open_buy_orders):
                skipped += 1
                reason = "duplicate guard: ticker already held or has an open BUY order this session"
                print(f"[EXPERIMENTAL] Skip {ticker}: {reason}.")
                outcomes.append({"ticker": ticker, "status": "skipped", "reason": reason, "held": held})
                continue

            remaining_allowed = max(0, PURE_TRAINING_MAX_POSITION_PER_TICKER - max(0, int(held)))
            quantity = min(PURE_TRAINING_ORDER_QUANTITY, remaining_allowed)
            if quantity <= 0:
                skipped += 1
                reason = f"existing position {held:g} already meets max per ticker"
                print(f"[EXPERIMENTAL] Skip {ticker}: {reason}.")
                outcomes.append({"ticker": ticker, "status": "skipped", "reason": reason})
                continue

            contract = Stock(ticker, "SMART", "USD")
            try:
                ib.qualifyContracts(contract)
            except Exception as exc:
                skipped += 1
                reason = f"contract qualification failed: {exc}"
                print(f"[EXPERIMENTAL] Skip {ticker}: {reason}")
                outcomes.append({"ticker": ticker, "status": "skipped", "reason": reason})
                continue

            price, price_source = get_market_price(ib, contract)
            if not price:
                skipped += 1
                reason = "no usable price"
                print(f"[EXPERIMENTAL] Skip {ticker}: {reason}.")
                outcomes.append({"ticker": ticker, "status": "skipped", "reason": reason})
                continue

            volatility = monthly_volatility_pct(ticker)
            if volatility is None or volatility < EXPERIMENTAL_MIN_MONTHLY_VOLATILITY_PCT:
                skipped += 1
                reason = (
                    "monthly volatility below threshold"
                    if volatility is not None
                    else "monthly volatility unavailable"
                )
                print(
                    f"[EXPERIMENTAL] Skip {ticker}: {reason} "
                    f"({volatility if volatility is not None else 'n/a'}% < "
                    f"{EXPERIMENTAL_MIN_MONTHLY_VOLATILITY_PCT:.2f}%)."
                )
                outcomes.append(
                    {
                        "ticker": ticker,
                        "status": "skipped",
                        "reason": reason,
                        "monthly_volatility_pct": volatility,
                        "required_monthly_volatility_pct": EXPERIMENTAL_MIN_MONTHLY_VOLATILITY_PCT,
                    }
                )
                continue

            order_notional = price * quantity
            order_notional_cad = usd_to_cad(order_notional)
            projected_notional = planned_notional + order_notional
            projected_notional_cad = usd_to_cad(projected_notional)
            projected_total_exposure_cad = current_exposure_cad + projected_notional_cad
            if projected_notional_cad > max(0, available_funds - PURE_TRAINING_MIN_CASH_BUFFER_CAD):
                skipped += 1
                reason = "available funds guard"
                print(f"[EXPERIMENTAL] Skip {ticker}: {reason}.")
                outcomes.append({"ticker": ticker, "status": "skipped", "reason": reason})
                continue
            if projected_total_exposure_cad > PURE_TRAINING_MAX_ACCOUNT_CAD:
                skipped += 1
                reason = "account exposure cap"
                print(f"[EXPERIMENTAL] Skip {ticker}: {reason}.")
                outcomes.append({"ticker": ticker, "status": "skipped", "reason": reason})
                continue

            take_profit, stop_loss = training_bracket_prices(price)
            bracket = None if build_only else build_training_bracket(ib, quantity, price, run_id, ticker)[0]
            candidates += 1
            planned_notional = projected_notional
            status = "candidate" if build_only else ("dry_run" if effective_dry_run else "sent")
            event_type = (
                "experimental_basket_candidate"
                if build_only
                else ("pure_training_order_prepared" if effective_dry_run else "pure_training_order_sent")
            )
            payload = {
                "signal_id": None,
                "ticker": ticker,
                "action_type": "BUY",
                "event_type": event_type,
                "status": status,
                "quantity": quantity,
                "price": price,
                "source": SOURCE,
                "note": (
                    f"run_id={run_id}; price_source={price_source}; "
                    f"tp={take_profit}; sl={stop_loss}; held_before={held:g}; "
                    f"notional_cad={order_notional_cad:.2f}; monthly_volatility_pct={volatility}"
                ),
                "occurred_at": utc_now_iso(),
            }

            print(
                f"[EXPERIMENTAL] {ticker}: {'CANDIDATE' if build_only else 'BUY'} {quantity} MKT; "
                f"baseline={price:.2f} TP={take_profit:.2f} SL={stop_loss:.2f}"
            )
            if build_only:
                print(f"[EXPERIMENTAL] PREVIEW: candidate published for {ticker}; order not sent.")
            elif effective_dry_run:
                print(f"[EXPERIMENTAL] DRY RUN: order not sent for {ticker}.")
            else:
                for order in bracket:
                    ib.placeOrder(contract, order)
                sent += 1
                positions[ticker] = held + quantity
                print(f"[EXPERIMENTAL] Sent bracket order for {ticker}.")

            if build_only:
                append_training_event("experimental_basket_candidate", payload)
            else:
                record_trade_event(supabase, payload)
            outcomes.append(
                {
                    "ticker": ticker,
                    "status": payload["status"],
                    "quantity": quantity,
                    "price": price,
                    "take_profit": take_profit,
                    "stop_loss": stop_loss,
                    "price_source": price_source,
                    "monthly_volatility_pct": volatility,
                    "notional_cad": order_notional_cad,
                }
            )

            if not effective_dry_run and PURE_TRAINING_SYNC_EVERY_ORDERS > 0 and sent % PURE_TRAINING_SYNC_EVERY_ORDERS == 0:
                print("[EXPERIMENTAL] Periodic broker sync...")
                sync_once(ib, supabase, mark_signals=False, dry_run=False)
            time.sleep(0.2)

        if not effective_dry_run:
            print("[EXPERIMENTAL] Final broker sync...")
            sync_once(ib, supabase, mark_signals=False, dry_run=False)

        result = {
            "run_id": run_id,
            "status": "success",
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "dry_run": effective_dry_run,
            "execution_mode": execution_mode,
            "sent": sent,
            "skipped": skipped,
            "candidates": candidates,
            "planned_notional": planned_notional,
            "planned_notional_cad": usd_to_cad(planned_notional),
            "starting_exposure": current_exposure,
            "starting_exposure_cad": current_exposure_cad,
            "projected_total_exposure_cad": current_exposure_cad + usd_to_cad(planned_notional),
            "usd_cad_rate": PURE_TRAINING_USD_CAD_RATE,
            "outcomes": outcomes,
        }
        result["profile"] = "experimental"
        result["min_monthly_volatility_pct"] = EXPERIMENTAL_MIN_MONTHLY_VOLATILITY_PCT
        if build_only:
            result["status"] = "built"
            append_training_event("experimental_basket_built", result)
        else:
            append_training_event("experimental_run_finished", result)
        update_run_result(supabase, result)
        print(
            f"[EXPERIMENTAL] {'Build' if build_only else 'Run'} complete: "
            f"candidates={candidates}, sent={sent}, skipped={skipped}, dry_run={effective_dry_run}."
        )
        return True
    finally:
        ib.disconnect()


def run_experimental_basket_build(dry_run=True, tickers=None, reset_force_flag=True):
    return run_pure_training_mode(
        dry_run=True,
        tickers=tickers,
        reset_force_flag=reset_force_flag,
        execution_mode="build",
    )


def run_experimental_basket_execute(dry_run=None, tickers=None, reset_force_flag=True):
    return run_pure_training_mode(
        dry_run=dry_run,
        tickers=tickers,
        reset_force_flag=reset_force_flag,
        execution_mode="execute",
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Route the current category shortlist as a small paper-training basket.")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["build", "execute", "run", "monitor"],
        help="Build a preview basket, execute the basket, or record a broker monitor snapshot.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Prepare and log orders without sending them to IBKR.")
    parser.add_argument("--tickers", help="Comma-separated ticker override for a small test run.")
    parser.add_argument("--keep-force-flag", action="store_true", help="Do not reset force_pure_training_run in bot_settings.")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.command == "monitor":
        success = run_pure_training_monitor(dry_run=args.dry_run)
        raise SystemExit(0 if success else 1)

    tickers = args.tickers.split(",") if args.tickers else None
    if args.command == "build":
        success = run_experimental_basket_build(
            tickers=tickers,
            reset_force_flag=not args.keep_force_flag,
        )
    else:
        success = run_experimental_basket_execute(
            dry_run=args.dry_run or DRY_RUN,
            tickers=tickers,
            reset_force_flag=not args.keep_force_flag,
        )
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
