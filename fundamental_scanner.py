import time
import os
import requests
import pandas as pd
import yfinance as yf

from config import get_supabase_client

# --- LOAD SECRETS ---
supabase = get_supabase_client()

# --- SCREENING PARAMETERS ---
MIN_MARKET_CAP = 10_000_000_000  
MIN_PE = 5.0 
MAX_PE = 40.0 
MAX_RESULTS = 10 

def get_sp500_tickers():
    print("🌐 Fetching S&P 500 roster from reliable index source...")
    url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv"
    try:
        df = pd.read_csv(url)
        tickers = df['Symbol'].str.replace('.', '-', regex=False).tolist()
        print(f"✅ Successfully loaded {len(tickers)} tickers.")
        return tickers
    except Exception as e:
        print(f"🚨 Failed to load tickers: {e}")
        return []

def fundamental_screen():
    print("🕵️‍♂️ Initiating Deep Fundamental Screen...")
    tickers = get_sp500_tickers()
    passed_stocks = []
    
    print(f"📊 Scanning mega-cap stocks...")
    
    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            pe_ratio = info.get('trailingPE') or info.get('forwardPE')
            market_cap = info.get('marketCap')
            
            if pe_ratio is None or market_cap is None:
                continue
                
            if market_cap >= MIN_MARKET_CAP and MIN_PE <= pe_ratio <= MAX_PE:
                passed_stocks.append({'ticker': ticker, 'pe': pe_ratio})
                print(f"   ✅ {ticker} passed! (P/E: {pe_ratio:.2f})")
                
            time.sleep(0.05) 
        except Exception:
            continue

    passed_stocks.sort(key=lambda x: x['pe'])
    return [stock['ticker'] for stock in passed_stocks[:MAX_RESULTS]]

def update_cloud_watchlist(tickers):
    if not tickers:
        print("⚠️ No stocks passed fundamental filter.")
        return
        
    print(f"\n☁️ Pushing winners to Supabase: {tickers}")
    try:
        supabase.table("bot_settings").update({"watchlist": tickers}).eq("id", 1).execute()
        print("✅ iPad Command Center Watchlist successfully updated.")
    except Exception as e:
        print(f"🚨 Supabase Update Error: {e}")

def save_local_targets(tickers):
    """Saves the fundamentally sound tickers to a local text file for intraday scanners to read."""
    if not tickers:
        return
        
    file_path = os.path.join(os.path.dirname(__file__), "daily_targets.txt")
    try:
        with open(file_path, "w") as f:
            f.write(",".join(tickers))
        print(f"📁 Local Handoff Complete: Saved {len(tickers)} targets to {file_path} for intraday routing.")
    except Exception as e:
        print(f"🚨 Failed to save local targets: {e}")
def run_fundamental_scan():
    winning_tickers = fundamental_screen()
    update_cloud_watchlist(winning_tickers)
    save_local_targets(winning_tickers)
    print("Fundamental sequence complete.")


if __name__ == "__main__":
    winning_tickers = fundamental_screen()
    update_cloud_watchlist(winning_tickers)
    save_local_targets(winning_tickers) 
    print("🏁 Fundamental sequence complete.")
