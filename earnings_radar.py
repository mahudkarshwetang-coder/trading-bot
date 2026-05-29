import requests
import re
from datetime import datetime
from config import get_supabase_client

# --- CONFIGURATION ---
supabase = get_supabase_client()

def get_todays_earnings():
    """Scrapes the Yahoo Finance calendar for companies reporting today."""
    # --- SIMULATION TIME-TRAVEL SWAP ---
    # To test the parser against a guaranteed heavy volume day, uncomment the line below 
    # and comment out the standard datetime line directly underneath it.
    # today = "2026-05-20"  # Nvidia (NVDA) heavy reporting date simulation
    
    today = datetime.now().strftime('%Y-%m-%d')
    print(f"📡 Scanning Earnings Calendar for {today}...")
    
    url = f"https://finance.yahoo.com/calendar/earnings?day={today}"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    
    try:
        response = requests.get(url, headers=headers)
        
        # --- QUANTITATIVE TELEMETRY BLOCKS ---
        print(f"   ↳ Server Response Code: {response.status_code}")
        print(f"   ↳ Document Payload Density: {len(response.text):,} characters")
        
        # Catch explicit anti-bot or server errors instantly
        if response.status_code != 200:
            print(f"🚨 Network Alert: HTTP Error {response.status_code}. Yahoo Finance rejected the signature.")
            return []
            
        # Catch silent bot block deflections (where the server returns 200 but serves a tiny 2KB empty captcha page)
        if len(response.text) < 10000:
            print("⚠️ Telemetry Warning: Document payload is suspiciously lightweight. You might be hitting an anti-bot wall.")
            return []
        # -------------------------------------
        
        # RegEx logic to extract pure stock symbols from the raw HTML links
        tickers = re.findall(r'href="/quote/([A-Z]+)\?', response.text)
        
        # Remove duplicates while preserving order
        unique_tickers = list(dict.fromkeys(tickers))
        
        # Grab the top 5 most highly-anticipated reports
        hot_earnings = unique_tickers[:5]
        return hot_earnings
        
    except Exception as e:
        print(f"🚨 Earnings Sweep Failed: {e}")
        return []

def update_cloud_earnings(tickers):
    """Pushes the earnings targets strictly to the third lane in Supabase."""
    if not tickers:
        print("⚠️ No earnings found for today. Aborting cloud update.")
        return
        
    print(f"☁️ Pushing earnings targets to Supabase: {tickers}")
    try:
        # Overwrite ONLY the new earnings_watchlist column
        supabase.table("bot_settings").update({"earnings_watchlist": tickers}).eq("id", 1).execute()
        print("✅ iPad Command Center Earnings Watchlist successfully updated.")
    except Exception as e:
        print(f"🚨 Supabase Update Error: {e}")
def run_earnings_scan():
    print("Initiating Option B: Earnings Radar Engine")
    print("-" * 50)

    earnings_tickers = get_todays_earnings()
    update_cloud_earnings(earnings_tickers)

    print("-" * 50)
    print("Earnings sequence complete.")


if __name__ == "__main__":
    print("🚀 Initiating Option B: Earnings Radar Engine")
    print("-" * 50)
    
    earnings_tickers = get_todays_earnings()
    update_cloud_earnings(earnings_tickers)
    
    print("-" * 50)
    print("🏁 Earnings sequence complete.")
