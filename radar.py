import requests
from config import get_supabase_client

# --- CONFIGURATION ---
supabase = get_supabase_client()

def get_trending_tickers():
    """Sweeps the Yahoo Finance backend for the top trending US tickers."""
    print("📡 Sweeping Yahoo Finance for market volatility...")
    
    # We ask for 10 just in case some are cryptos or indices we need to filter out
    url = "https://query1.finance.yahoo.com/v1/finance/trending/US?count=10"
    
    # We spoof a standard web browser so Yahoo doesn't block the automated request
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    
    try:
        response = requests.get(url, headers=headers)
        data = response.json()
        quotes = data['finance']['result'][0]['quotes']
        
        # Extract the symbols. 
        # We filter out '-' (Crypto like BTC-USD) and '^' (Indices like ^GSPC)
        tickers = [q['symbol'] for q in quotes if '-' not in q['symbol'] and '^' not in q['symbol']]
        
        # Grab the top 5 hottest stocks
        hot_tickers = tickers[:5]
        return hot_tickers
        
    except Exception as e:
        print(f"🚨 Radar Sweep Failed: {e}")
        return []

def update_cloud_watchlist(tickers):
    """Overwrites the iPad's bot_settings table with the new targets."""
    if not tickers:
        print("⚠️ No tickers found. Aborting cloud update.")
        return
        
    print(f"☁️ Pushing fresh targets to Supabase: {tickers}")
    try:
        # ⚠️ SURGICAL FIX: Overwrite the new radar column, leave the manual list alone!
        supabase.table("bot_settings").update({"radar_watchlist": tickers}).eq("id", 1).execute()
        print("✅ Cloud Radar Watchlist successfully updated.")
    except Exception as e:
        print(f"🚨 Supabase Update Error: {e}")
def run_radar_scan():
    print("Initiating Option A: Volatility Radar Engine")
    print("-" * 50)

    hot_tickers = get_trending_tickers()
    update_cloud_watchlist(hot_tickers)

    print("-" * 50)
    print("Radar sequence complete. The system is primed for the LLM Scanner.")


if __name__ == "__main__":
    print("🚀 Initiating Option A: Volatility Radar Engine")
    print("-" * 50)
    
    hot_tickers = get_trending_tickers()
    update_cloud_watchlist(hot_tickers)
    
    print("-" * 50)
    print("🏁 Radar sequence complete. The system is primed for the LLM Scanner.")
