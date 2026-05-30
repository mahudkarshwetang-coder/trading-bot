import time
import subprocess
import math
import yfinance as yf
from datetime import datetime, timezone
from ib_insync import IB, Stock

from config import (
    DRY_RUN,
    IBKR_CLIENT_ID,
    IBKR_HOST,
    IBKR_PORT,
    MAX_DRAWDOWN_PCT,
    get_supabase_client,
)

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

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

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
        df = stock.history(period="1d")
        if not df.empty:
            web_price = round(float(df['Close'].iloc[-1]), 2)
            print(f"   ↳ Web Oracle successfully extracted price: ${web_price}")
            return web_price
    except Exception as e:
        print(f"   🚨 Web Oracle fallback failed for {contract.symbol}: {e}")
        
    return 0.0

def calculate_dynamic_quantity(entry_price, stop_loss_price, risk_percentage=1.0):
    """Calculates exact share quantity based on account size and stop-loss distance."""
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

def route_bracket_order(ticker, action):
    """Generates a 3-Leg Marketable Bracket Order: Entry (Padded LMT) + Stop Loss + Take Profit."""
    print(f"📦 Generating Bracket Order block for {ticker}...")
    
    contract = Stock(ticker, 'SMART', 'USD')
    ib.qualifyContracts(contract)
    
    current_price = get_current_price(contract)
    if current_price == 0.0:
        print(f"🚨 Aborting Trade: Zero price data available for {ticker}.")
        return False, 0.0, False, None
        
    actual_baseline = round(current_price, 2)
    
    if action == "BUY":
        entry_price = round(current_price * 1.01, 2)
        take_profit = round(current_price * 1.06, 2)  
        stop_loss   = round(current_price * 0.98, 2)  
    else: 
        entry_price = round(current_price * 0.99, 2)
        take_profit = round(current_price * 0.94, 2)  
        stop_loss   = round(current_price * 1.02, 2)  
        
    quantity = calculate_dynamic_quantity(entry_price, stop_loss, risk_percentage=1.0)
        
    print(f"   ↳ Marketable Matrix | Entry LMT: ${entry_price:.2f} (True Baseline: ${actual_baseline:.2f})")
    print(f"   ↳ Risk Matrix calculated | TP: ${take_profit:.2f} | SL: ${stop_loss:.2f}")

    bracket = ib.bracketOrder(
        action, 
        quantity, 
        limitPrice=entry_price, 
        takeProfitPrice=take_profit, 
        stopLossPrice=stop_loss
    )
    
    for leg in bracket: leg.tif = 'GTC'          
    bracket[0].outsideRth = True  
    bracket[1].outsideRth = True  
    bracket[2].outsideRth = False 
        
    bracket[0].transmit = False  
    bracket[1].transmit = False  
    bracket[2].transmit = True   
    
    if DRY_RUN:
        print(f"DRY RUN: Prepared {action} {quantity} {ticker}; order was not sent to IBKR.")
        return True, actual_baseline, False, quantity

    try:
        for order in bracket:
            ib.placeOrder(contract, order)
        print(f"🚀 BRACKET DEPLOYED: {action} {quantity} {ticker} with full risk management.")
        return True, actual_baseline, True, quantity
    except Exception as e:
        print(f"🚨 Exchange Routing Failure for {ticker}: {e}")
        return False, 0.0, False, quantity

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
                    supabase.table("bot_settings").update({"force_scanner": False}).eq("id", 1).execute()

            # 3. --- TRADE EXECUTION LISTENER ---
            # This catches BOTH auto-approved BUYs and manually approved SELLs from the iPad
            trade_resp = supabase.table("market_signals").select("*").eq("status", "approved").execute()
            if trade_resp.data:
                for signal in trade_resp.data:
                    ticker, action, signal_id = signal["ticker"], signal["action_type"], signal["id"]
                    
                    print(f"\n🔔 ROUTING APPROVED SIGNAL: {ticker} ({action})")
                    
                    success, fill_price, live_order_sent, quantity = route_bracket_order(ticker, action)
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
                    else:
                        supabase.table("market_signals").update({"status": "failed"}).eq("id", signal_id).execute()
                        record_trade_event(
                            signal,
                            "routing_failed",
                            status="failed",
                            price=fill_price if fill_price else None,
                            quantity=quantity,
                        )
                        print(f"❌ Loop Defused: {ticker} routing aborted.")

        except Exception as e:
            print(f"⚠️ Error polling database: {e}")
            
        time.sleep(2)

if __name__ == "__main__":
    print("⚡ Starting Alpha Engine Order Routing Bridge [BRACKET MODE - ARMED]")
    print(f"Training Safety: DRY_RUN is {'ON' if DRY_RUN else 'OFF'}")
    print("-" * 50)
    connect_to_broker()
    print("-" * 50)
    listen_for_commands()
