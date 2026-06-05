import time
import subprocess
import math
import yfinance as yf
from datetime import datetime, timedelta, timezone
from ib_insync import IB, LimitOrder, MarketOrder, Stock, StopOrder

from config import (
    ALLOW_EXTENDED_HOURS_TRADING,
    ALLOW_GLOBAL_OVERNIGHT_TRADING,
    BRACKET_STOP_LOSS_PCT,
    BRACKET_TAKE_PROFIT_PCT,
    BUY_REENTRY_DIP_PCT,
    DRY_RUN,
    ENTRY_ORDER_TYPE,
    FIXED_ORDER_QUANTITY,
    GLOBAL_OVERNIGHT_OPEN,
    IBKR_CLIENT_ID,
    IBKR_HOST,
    IBKR_PORT,
    MAX_DRAWDOWN_PCT,
    PREMARKET_OPEN,
    STOP_OUTSIDE_RTH,
    SYNC_BROKER_AFTER_ORDER,
    SYNC_BROKER_AFTER_ORDER_DELAY_SECONDS,
    get_supabase_client,
)
from execution_quality_gate import review_execution_quality
from market_session import get_market_session, now_market_time, parse_time

# --- CONFIGURATION & SECURITY ---
try:
    supabase = get_supabase_client()
except RuntimeError as exc:
    print(f"CRITICAL: {exc}")
    exit(1)
ib = IB()

# Global variable to hold our PnL subscription
account_pnl = None
SYSTEM_HALTED = False
TRADE_EVENTS_SETUP_WARNED = False
NO_SHORT_SELL_STATUS = "blocked_no_position"
EXTENDED_HOURS_BLOCK_STATUS = "blocked_extended_hours"
GLOBAL_OVERNIGHT_BLOCK_STATUS = "blocked_global_overnight"
REBUY_DIP_BLOCK_STATUS = "blocked_rebuy_no_dip"
EXECUTION_GATE_BLOCK_STATUS = "blocked_execution_gate"

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_supabase_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def float_or_none(value):
    try:
        result = float(value)
        if math.isnan(result):
            return None
        return result
    except Exception:
        return None


def trading_session_start_utc(now=None):
    """Returns the UTC timestamp for the start of the current market session window."""
    local_now = now or now_market_time()
    session = get_market_session(local_now)

    if session.is_global_overnight:
        open_time = parse_time(GLOBAL_OVERNIGHT_OPEN)
        start = local_now.replace(hour=open_time.hour, minute=open_time.minute, second=0, microsecond=0)
        # Overnight session spans evening -> next early morning
        if local_now.time() < open_time:
            start -= timedelta(days=1)
        return start.astimezone(timezone.utc)

    premarket_open = parse_time(PREMARKET_OPEN)
    start = local_now.replace(hour=premarket_open.hour, minute=premarket_open.minute, second=0, microsecond=0)
    if local_now.time() < premarket_open:
        start -= timedelta(days=1)
    return start.astimezone(timezone.utc)


def last_buy_price_this_session(ticker):
    """
    Returns the most recent BUY execution price for a ticker in the current trading
    session window, or None when no prior BUY exists.
    """
    session_start_utc = trading_session_start_utc()
    try:
        response = (
            supabase.table("market_signals")
            .select("id,status,created_at,execution_price,price_at_signal")
            .eq("ticker", ticker)
            .eq("action_type", "BUY")
            .order("created_at", desc=True)
            .limit(200)
            .execute()
        )
    except Exception as exc:
        print(f"⚠️ Re-entry lookup failed for {ticker}: {exc}")
        return None, None

    for row in response.data or []:
        status = str(row.get("status") or "").lower()
        if status not in {"executed", "dry_run"}:
            continue

        created_at = parse_supabase_datetime(row.get("created_at"))
        if not created_at or created_at < session_start_utc:
            continue

        price = float_or_none(row.get("execution_price")) or float_or_none(row.get("price_at_signal"))
        if price and price > 0:
            return price, created_at
    return None, None

def is_missing_trade_events_table(exc):
    message = str(exc)
    return (
        "trade_events" in message
        and (
            "PGRST205" in message
            or "schema cache" in message
            or "Could not find the table" in message
        )
    )

def record_trade_event(signal, event_type, status=None, price=None, quantity=None, note=None):
    """Best-effort analytics write. Missing table should never stop execution routing."""
    global TRADE_EVENTS_SETUP_WARNED

    payload = {
        "signal_id": signal.get("id"),
        "ticker": signal.get("ticker"),
        "action_type": signal.get("action_type"),
        "event_type": event_type,
        "status": status,
        "quantity": quantity,
        "price": price,
        "source": "execution_bridge",
        "note": note,
        "occurred_at": utc_now_iso(),
    }

    try:
        supabase.table("trade_events").insert(payload).execute()
    except Exception as exc:
        if is_missing_trade_events_table(exc):
            if not TRADE_EVENTS_SETUP_WARNED:
                print("⚠️ trade_events table missing; run supabase/trade_events.sql to enable outcome analytics.")
                TRADE_EVENTS_SETUP_WARNED = True
            return
        print(f"⚠️ Trade event write skipped for {payload['ticker']}: {exc}")

def sync_broker_snapshot(label="manual request", delay_seconds=0.0):
    """Best-effort broker position sync for the iPad Ledger."""
    delay = max(0.0, float(delay_seconds or 0.0))
    if delay:
        print(f"   Broker sync queued in {delay:.1f}s so IBKR can update positions...")
        ib.sleep(delay)

    try:
        from broker_sync import sync_once

        print(f"   Syncing IBKR broker snapshot to Supabase ({label})...")
        sync_once(
            ib,
            supabase,
            mark_signals=False,
            dry_run=False,
        )
        return True
    except Exception as exc:
        print(f"Broker sync skipped ({label}): {exc}")
        return False


def sync_broker_snapshot_after_order(ticker, action):
    """Best-effort broker position sync for the iPad Ledger after a live order is sent."""
    if not SYNC_BROKER_AFTER_ORDER:
        return False

    return sync_broker_snapshot(
        label=f"after {action} {ticker}",
        delay_seconds=SYNC_BROKER_AFTER_ORDER_DELAY_SECONDS,
    )


def current_position_quantity(ticker):
    """Return current IBKR position quantity for a symbol, or 0 if not held."""
    normalized = str(ticker or "").upper()
    try:
        total = 0.0
        for item in ib.positions():
            if item.contract.symbol.upper() == normalized:
                total += float(item.position)
        return total
    except Exception as exc:
        print(f"⚠️ Position lookup failed for {ticker}: {exc}")
        return 0.0

def can_route_sell(ticker):
    quantity = current_position_quantity(ticker)
    if quantity > 0:
        return True, quantity
    return False, quantity


def percent_delta(current, reference):
    try:
        current_value = float(current)
        reference_value = float(reference)
        if reference_value <= 0:
            return None
        return round(((current_value - reference_value) / reference_value) * 100.0, 2)
    except Exception:
        return None

def connect_to_broker():
    """Attempts connection to the local running IBKR TWS instance."""
    global account_pnl
    try:
        ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID)
        ib.reqMarketDataType(4) 
        print("✅ Connected to Interactive Brokers Paper Trading Terminal.")
        
        # Subscribe to continuous PnL updates for the circuit breaker
        accounts = ib.managedAccounts()
        if accounts:
            account_pnl = ib.reqPnL(accounts[0])
            print(f"🛡️ Global Circuit Breaker Armed on account: {accounts[0]}")
            
    except Exception as e:
        print(f"🚨 Broker Connection Failed: {e}")
        exit(1)

def check_circuit_breaker(max_drawdown_pct=2.0):
    """
    Monitors Daily PnL. If daily losses exceed X% of total account value,
    it triggers a hard stop, disabling the bot locally and in the cloud.
    """
    global account_pnl, SYSTEM_HALTED
    
    if account_pnl is None or account_pnl.dailyPnL is None or math.isnan(account_pnl.dailyPnL):
        return False # Data not yet available from IBKR
        
    try:
        account_summary = ib.accountSummary()
        net_liq = 0.0
        for item in account_summary:
            if item.tag == 'NetLiquidation':
                net_liq = float(item.value)
                break
                
        if net_liq > 0:
            max_loss_dollar = net_liq * (max_drawdown_pct / 100.0)
            current_pnl = float(account_pnl.dailyPnL)
            
            # Check if our losses are worse than our max allowed limit
            if current_pnl <= -max_loss_dollar:
                print("\n" + "!"*50)
                print(f"🚨 CRITICAL: GLOBAL CIRCUIT BREAKER TRIPPED! 🚨")
                print(f"   Daily PnL: ${current_pnl:.2f}")
                print(f"   Max Allowed Drawdown: -${max_loss_dollar:.2f}")
                print("   Action: Halting all Alpha Engine executions immediately.")
                print("!"*50 + "\n")
                
                # Update Supabase so the iPad Dashboard shows the system is offline
                try:
                    supabase.table("bot_settings").update({"is_active": False}).eq("id", 1).execute()
                except Exception:
                    pass
                    
                SYSTEM_HALTED = True
                return True
                
    except Exception as e:
        print(f"⚠️ Circuit Breaker calculation error: {e}")
        
    return False

def get_current_price(contract):
    """Fetches market price from IBKR with an automatic Web Oracle fallback."""
    try:
        tickers = ib.reqTickers(contract)
        ib.sleep(0.5) 
        price = tickers[0].marketPrice()
        
        if not math.isnan(price) and price > 0:
            return price
            
        price = tickers[0].close
        if not math.isnan(price) and price > 0:
            return price
    except Exception:
        pass
        
    print(f"   ⚠️ IBKR Data Wall hit for {contract.symbol}. Engaging Web Oracle...")
    try:
        stock = yf.Ticker(contract.symbol)
        df = stock.history(period="1d", interval="1m", prepost=True)
        if df.empty:
            df = stock.history(period="5d", interval="1d")
        if not df.empty:
            web_price = round(float(df['Close'].iloc[-1]), 2)
            print(f"   ↳ Web Oracle successfully extracted price: ${web_price}")
            return web_price
    except Exception as e:
        print(f"   🚨 Web Oracle fallback failed for {contract.symbol}: {e}")
        
    return 0.0

def calculate_dynamic_quantity(entry_price, stop_loss_price, risk_percentage=1.0):
    """Calculates exact share quantity based on account size and stop-loss distance."""
    if FIXED_ORDER_QUANTITY > 0:
        print(f"   ⚖️ Position Size Override | Fixed Qty: {FIXED_ORDER_QUANTITY}")
        return FIXED_ORDER_QUANTITY

    try:
        account_summary = ib.accountSummary()
        net_liquidation = 0.0
        for item in account_summary:
            if item.tag == 'NetLiquidation':
                net_liquidation = float(item.value)
                break
                
        if net_liquidation <= 0: return 1
            
        dollar_risk = net_liquidation * (risk_percentage / 100.0)
        risk_per_share = abs(entry_price - stop_loss_price)
        
        if risk_per_share <= 0: return 1
            
        raw_quantity = dollar_risk / risk_per_share
        
        max_capital_allowed = net_liquidation * 0.25
        max_shares_allowed = max_capital_allowed / entry_price
        
        final_quantity = min(raw_quantity, max_shares_allowed)
        final_quantity = math.floor(final_quantity)
        
        if final_quantity < 1: return 1 
            
        print(f"   ⚖️ Position Size Calc | Bal: ${net_liquidation:.0f} | Risk/Share: ${risk_per_share:.2f} | Target Qty: {final_quantity}")
        return final_quantity

    except Exception as e:
        print(f"🚨 Sizing Calculation Failed: {e}")
        return 1

def opposite_action(action):
    return "SELL" if action == "BUY" else "BUY"


def build_bracket_orders(action, quantity, entry_price, take_profit, stop_loss):
    entry_type = (ENTRY_ORDER_TYPE or "MKT").upper()
    if entry_type == "MKT":
        parent_order_id = ib.client.getReqId()
        parent = MarketOrder(
            action,
            quantity,
            orderId=parent_order_id,
            transmit=False,
            tif="GTC",
        )
        parent.outsideRth = True

        exit_action = opposite_action(action)
        take_profit_order = LimitOrder(
            exit_action,
            quantity,
            take_profit,
            orderId=ib.client.getReqId(),
            parentId=parent_order_id,
            transmit=False,
            tif="GTC",
        )
        take_profit_order.outsideRth = True

        stop_loss_order = StopOrder(
            exit_action,
            quantity,
            stop_loss,
            orderId=ib.client.getReqId(),
            parentId=parent_order_id,
            transmit=True,
            tif="GTC",
        )
        stop_loss_order.outsideRth = STOP_OUTSIDE_RTH
        return [parent, take_profit_order, stop_loss_order]

    bracket = ib.bracketOrder(
        action,
        quantity,
        limitPrice=entry_price,
        takeProfitPrice=take_profit,
        stopLossPrice=stop_loss,
    )
    for leg in bracket:
        leg.tif = "GTC"
    bracket[0].outsideRth = True
    bracket[1].outsideRth = True
    bracket[2].outsideRth = STOP_OUTSIDE_RTH
    bracket[0].transmit = False
    bracket[1].transmit = False
    bracket[2].transmit = True
    return bracket


def route_bracket_order(ticker, action, signal=None):
    """Generates a bracket order: market entry + stop loss + take profit."""
    print(f"📦 Generating Bracket Order block for {ticker}...")
    session = get_market_session()
    held_quantity = None
    if session.is_global_overnight and not DRY_RUN and not ALLOW_GLOBAL_OVERNIGHT_TRADING:
        print(f"Global overnight live routing is disabled. Refusing {action} {ticker}.")
        return False, 0.0, False, None, GLOBAL_OVERNIGHT_BLOCK_STATUS, "Live routing disabled during global overnight session."
    if session.is_extended and not DRY_RUN and not ALLOW_EXTENDED_HOURS_TRADING:
        print(f"Extended-hours live routing is disabled. Refusing {action} {ticker} during {session.name}.")
        return False, 0.0, False, None, EXTENDED_HOURS_BLOCK_STATUS, f"Live routing disabled during {session.name}."
    
    if action == "SELL":
        allowed, held_quantity = can_route_sell(ticker)
        if not allowed:
            print(f"🛡️ NO-SHORT SAFETY: Refusing SELL {ticker}; current long position is {held_quantity}.")
            return False, 0.0, False, None, NO_SHORT_SELL_STATUS, "No long position for SELL."
    
    contract = Stock(ticker, 'SMART', 'USD')
    ib.qualifyContracts(contract)
    
    current_price = get_current_price(contract)
    if current_price == 0.0:
        print(f"🚨 Aborting Trade: Zero price data available for {ticker}.")
        return False, 0.0, False, None, None, "No valid market price."
        
    actual_baseline = round(current_price, 2)
    last_buy_price = None
    last_buy_time = None

    if action == "BUY":
        last_buy_price, last_buy_time = last_buy_price_this_session(ticker)
        dip_pct = max(0.0, BUY_REENTRY_DIP_PCT)
        if last_buy_price and dip_pct > 0:
            reentry_price_cap = round(last_buy_price * (1 - (dip_pct / 100.0)), 2)
            if actual_baseline > reentry_price_cap:
                note = (
                    f"Repeat BUY blocked for {ticker}: current ${actual_baseline:.2f} must be <= "
                    f"${reentry_price_cap:.2f} ({dip_pct:.1f}% dip from prior session buy "
                    f"${last_buy_price:.2f} at {last_buy_time.isoformat() if last_buy_time else 'unknown'})."
                )
                print(f"[RE-ENTRY BLOCK] {note}")
                return False, actual_baseline, False, None, REBUY_DIP_BLOCK_STATUS, note
    
    stop_loss_pct = max(0.1, BRACKET_STOP_LOSS_PCT)
    take_profit_pct = max(0.1, BRACKET_TAKE_PROFIT_PCT)
    entry_order_type = (ENTRY_ORDER_TYPE or "MKT").upper()
    if action == "BUY":
        entry_price = actual_baseline if entry_order_type == "MKT" else round(current_price * 1.01, 2)
        take_profit = round(current_price * (1 + (take_profit_pct / 100.0)), 2)
        stop_loss = round(current_price * (1 - (stop_loss_pct / 100.0)), 2)
    else: 
        entry_price = actual_baseline if entry_order_type == "MKT" else round(current_price * 0.99, 2)
        take_profit = round(current_price * (1 - (take_profit_pct / 100.0)), 2)
        stop_loss = round(current_price * (1 + (stop_loss_pct / 100.0)), 2)
        
    quantity = calculate_dynamic_quantity(entry_price, stop_loss, risk_percentage=1.0)
    if action == "SELL" and held_quantity is not None and held_quantity < quantity:
        quantity = math.floor(held_quantity)
        if quantity < 1:
            print(f"🛡️ NO-SHORT SAFETY: Refusing SELL {ticker}; held quantity is below 1 share.")
            return False, 0.0, False, None, NO_SHORT_SELL_STATUS, "Held quantity below 1 share."
        print(f"   🛡️ SELL quantity reduced to held position: {quantity}")
        
    if entry_order_type == "MKT":
        print(f"   ↳ Entry Matrix | Entry MKT at market (Baseline: ${actual_baseline:.2f})")
    else:
        print(f"   ↳ Marketable Matrix | Entry LMT: ${entry_price:.2f} (True Baseline: ${actual_baseline:.2f})")
    print(
        f"   ↳ Risk Matrix calculated | TP: ${take_profit:.2f} ({take_profit_pct:.2f}%) "
        f"| SL: ${stop_loss:.2f} ({stop_loss_pct:.2f}%)"
    )

    signal_payload = signal or {}
    execution_context = {
        "signal_id": signal_payload.get("id"),
        "ticker": ticker,
        "action": action,
        "channel": signal_payload.get("channel"),
        "signal_status": signal_payload.get("status"),
        "scanner_confidence": signal_payload.get("confidence_score"),
        "price_at_signal": signal_payload.get("price_at_signal"),
        "current_price": actual_baseline,
        "price_delta_from_signal_pct": percent_delta(actual_baseline, signal_payload.get("price_at_signal")),
        "entry_order_type": entry_order_type,
        "entry_limit": entry_price if entry_order_type != "MKT" else None,
        "estimated_entry_price": entry_price,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "take_profit_pct": take_profit_pct,
        "stop_loss_pct": stop_loss_pct,
        "quantity": quantity,
        "session": {
            "name": session.name,
            "is_regular": session.is_regular,
            "is_extended": session.is_extended,
            "is_global_overnight": session.is_global_overnight,
        },
        "position": {
            "held_quantity": held_quantity,
        },
        "repeat_buy_guard": {
            "last_buy_price_this_session": last_buy_price,
            "last_buy_time": last_buy_time.isoformat() if last_buy_time else None,
            "required_dip_pct": BUY_REENTRY_DIP_PCT,
        },
        "risk_settings": {
            "fixed_order_quantity": FIXED_ORDER_QUANTITY,
            "dry_run": DRY_RUN,
            "stop_outside_rth": STOP_OUTSIDE_RTH,
            "allow_extended_hours": ALLOW_EXTENDED_HOURS_TRADING,
            "allow_global_overnight": ALLOW_GLOBAL_OVERNIGHT_TRADING,
        },
        "investment_memo": signal_payload.get("investment_memo"),
    }
    gate_approved, gate_decision, updated_signal = review_execution_quality(signal_payload, execution_context)
    if signal_payload.get("id") and updated_signal.get("investment_memo"):
        try:
            supabase.table("market_signals").update({"investment_memo": updated_signal["investment_memo"]}).eq("id", signal_payload["id"]).execute()
        except Exception as exc:
            print(f"   Execution gate memo update skipped for {ticker}: {exc}")
    if not gate_approved:
        note = (
            f"Qwen execution gate blocked {action} {ticker}: "
            f"score={gate_decision.get('execution_score')} "
            f"reason={gate_decision.get('rationale')}"
        )
        return False, actual_baseline, False, quantity, EXECUTION_GATE_BLOCK_STATUS, note

    bracket = build_bracket_orders(action, quantity, entry_price, take_profit, stop_loss)
    
    if DRY_RUN:
        print(f"DRY RUN: Prepared {action} {quantity} {ticker}; order was not sent to IBKR.")
        return True, actual_baseline, False, quantity, None, None

    try:
        for order in bracket:
            ib.placeOrder(contract, order)
        print(f"🚀 BRACKET DEPLOYED: {action} {quantity} {ticker} with full risk management.")
        return True, actual_baseline, True, quantity, None, None
    except Exception as e:
        print(f"🚨 Exchange Routing Failure for {ticker}: {e}")
        return False, 0.0, False, quantity, None, str(e)

def listen_for_commands():
    """Polls Supabase for trade approvals, autonomous rules, and circuit breakers."""
    global SYSTEM_HALTED
    print("👀 Execution Bridge online. Monitoring cloud for orders and risk thresholds...")
    
    # We use a memory set to avoid spamming the console with pending SELL logs every 2 seconds
    logged_pending_sells = set()

    while True:
        try:
            # 1. --- CIRCUIT BREAKER CHECK ---
            if SYSTEM_HALTED:
                print("🛑 SYSTEM LOCKED: Circuit Breaker active. Needs manual restart.")
                time.sleep(10)
                continue
                
            if check_circuit_breaker(max_drawdown_pct=MAX_DRAWDOWN_PCT):
                continue
                
            ib.sleep(0.5)

            # 2. --- SYSTEM SETTINGS & AUTONOMOUS LOGIC ---
            settings_resp = supabase.table("bot_settings").select("*").eq("id", 1).execute()
            if settings_resp.data:
                settings = settings_resp.data[0]
                
                # Master Off Switch
                if not settings.get("is_active", True):
                    SYSTEM_HALTED = True
                    print("\n🛑 IPAD OVERRIDE: Master Switch toggled OFF. Halting bot.")
                    continue
                
                # The "Semi-Autonomous" Logic
                is_auto = settings.get("auto_execute", settings.get("autonomous_execution", False))
                if is_auto:
                    pending_resp = supabase.table("market_signals").select("*").eq("status", "pending").execute()
                    if pending_resp.data:
                        for signal in pending_resp.data:
                            ticker, action, sig_id = signal["ticker"], signal["action_type"], signal["id"]
                            
                            if action == "BUY":
                                print(f"\n⚡ AUTONOMOUS MODE: Auto-approving BUY signal for {ticker}")
                                supabase.table("market_signals").update({"status": "approved"}).eq("id", sig_id).execute()
                            elif action == "SELL" and sig_id not in logged_pending_sells:
                                print(f"\n🛡️ AUTONOMOUS SAFEGUARD: Left SELL signal for {ticker} in iPad queue for manual review.")
                                logged_pending_sells.add(sig_id)

                # Manual Dashboard Overrides
                if settings.get("force_radar"):
                    subprocess.run(["python", "radar.py"])
                    supabase.table("bot_settings").update({"force_radar": False}).eq("id", 1).execute()
                if settings.get("force_earnings"):
                    subprocess.run(["python", "earnings_radar.py"])
                    supabase.table("bot_settings").update({"force_earnings": False}).eq("id", 1).execute()
                if settings.get("force_scanner"):
                    subprocess.run(["python", "llm_scanner.py"])
                    subprocess.run(["python", "context_enrichment.py"])
                    supabase.table("bot_settings").update({"force_scanner": False}).eq("id", 1).execute()
                if settings.get("force_broker_sync"):
                    print("\n🔄 IPAD OVERRIDE: Manual IBKR ledger sync requested.")
                    sync_broker_snapshot(label="iPad manual ledger sync")
                    supabase.table("bot_settings").update({"force_broker_sync": False}).eq("id", 1).execute()

            # 3. --- TRADE EXECUTION LISTENER ---
            # This catches BOTH auto-approved BUYs and manually approved SELLs from the iPad
            trade_resp = supabase.table("market_signals").select("*").eq("status", "approved").execute()
            if trade_resp.data:
                for signal in trade_resp.data:
                    ticker, action, signal_id = signal["ticker"], signal["action_type"], signal["id"]
                    
                    print(f"\n🔔 ROUTING APPROVED SIGNAL: {ticker} ({action})")
                    session = get_market_session()
                    if session.is_global_overnight and not DRY_RUN and not ALLOW_GLOBAL_OVERNIGHT_TRADING:
                        supabase.table("market_signals").update({"status": GLOBAL_OVERNIGHT_BLOCK_STATUS}).eq("id", signal_id).execute()
                        record_trade_event(
                            signal,
                            "blocked_global_overnight",
                            status=GLOBAL_OVERNIGHT_BLOCK_STATUS,
                            note="Live routing blocked during global_overnight; set ALLOW_GLOBAL_OVERNIGHT_TRADING=true to enable.",
                        )
                        print(f"Global overnight live routing blocked for {ticker}.")
                        continue

                    if session.is_extended and not DRY_RUN and not ALLOW_EXTENDED_HOURS_TRADING:
                        supabase.table("market_signals").update({"status": EXTENDED_HOURS_BLOCK_STATUS}).eq("id", signal_id).execute()
                        record_trade_event(
                            signal,
                            "blocked_extended_hours",
                            status=EXTENDED_HOURS_BLOCK_STATUS,
                            note=f"Live routing blocked during {session.name}; set ALLOW_EXTENDED_HOURS_TRADING=true to enable.",
                        )
                        print(f"Extended-hours live routing blocked for {ticker}.")
                        continue

                    if action == "SELL":
                        allowed, held_quantity = can_route_sell(ticker)
                        if not allowed:
                            supabase.table("market_signals").update({"status": NO_SHORT_SELL_STATUS}).eq("id", signal_id).execute()
                            record_trade_event(
                                signal,
                                "blocked_no_position",
                                status=NO_SHORT_SELL_STATUS,
                                quantity=held_quantity,
                                note="SELL blocked because no positive long IBKR position exists.",
                            )
                            print(f"🛡️ NO-SHORT SAFETY: {ticker} SELL blocked; no long position available.")
                            continue
                    
                    success, fill_price, live_order_sent, quantity, blocked_status, blocked_note = route_bracket_order(ticker, action, signal=signal)
                    if success:
                        next_status = "executed" if live_order_sent else "dry_run"
                        supabase.table("market_signals").update({"status": next_status, "execution_price": fill_price}).eq("id", signal_id).execute()
                        record_trade_event(
                            signal,
                            "order_sent" if live_order_sent else "dry_run",
                            status=next_status,
                            price=fill_price,
                            quantity=quantity,
                        )
                        print(f"Cloud State Updated: {ticker} marked {next_status} at ${fill_price}.")
                        if live_order_sent:
                            sync_broker_snapshot_after_order(ticker, action)
                    else:
                        final_status = blocked_status or "failed"
                        supabase.table("market_signals").update({"status": final_status}).eq("id", signal_id).execute()
                        record_trade_event(
                            signal,
                            "routing_blocked" if blocked_status else "routing_failed",
                            status=final_status,
                            price=fill_price if fill_price else None,
                            quantity=quantity,
                            note=blocked_note,
                        )
                        if blocked_status:
                            print(f"Blocked {ticker} {action}: {blocked_note or blocked_status}")
                        else:
                            print(f"❌ Loop Defused: {ticker} routing aborted.")

        except Exception as e:
            print(f"⚠️ Error polling database: {e}")
            
        time.sleep(2)

if __name__ == "__main__":
    print("⚡ Starting Alpha Engine Order Routing Bridge [BRACKET MODE - ARMED]")
    print(f"Training Safety: DRY_RUN is {'ON' if DRY_RUN else 'OFF'}")
    print(f"Fixed Order Quantity: {FIXED_ORDER_QUANTITY if FIXED_ORDER_QUANTITY > 0 else 'dynamic sizing'}")
    print(f"Entry Order Type: {ENTRY_ORDER_TYPE}")
    print(f"Bracket Risk: SL {BRACKET_STOP_LOSS_PCT:.2f}% | TP {BRACKET_TAKE_PROFIT_PCT:.2f}%")
    print(f"BUY Re-entry Guard: require {BUY_REENTRY_DIP_PCT:.2f}% dip in same session")
    print(f"Extended Hours Trading: {'ON' if ALLOW_EXTENDED_HOURS_TRADING else 'OFF'}")
    print(f"Global Overnight Trading: {'ON' if ALLOW_GLOBAL_OVERNIGHT_TRADING else 'OFF'}")
    print("-" * 50)
    connect_to_broker()
    print("-" * 50)
    listen_for_commands()
