import time
import requests
import yfinance as yf

from config import MASSIVE_API_KEY

class RateLimitedConcierge:
    def __init__(self):
        self.last_call = 0
        self.min_interval = 12.5  # 12 seconds + 0.5s safety buffer

    def _wait_if_needed(self):
        elapsed = time.time() - self.last_call
        if elapsed < self.min_interval:
            sleep_time = self.min_interval - elapsed
            print(f"   ⏳ Rate limiter: Sleeping {sleep_time:.2f}s to respect 5-calls/min limit...")
            time.sleep(sleep_time)

    def get_market_data(self, ticker):
        """
        Attempts to use Massive.com first, then falls back to Yahoo Finance.
        """
        self._wait_if_needed()
        
        # --- ATTEMPT MASSIVE.COM ---
        try:
            url = f"https://api.massive.com/v2/aggs/ticker/{ticker}/prev?apiKey={MASSIVE_API_KEY}"
            response = requests.get(url, timeout=5)
            self.last_call = time.time() # Reset timer
            
            if response.status_code == 200:
                data = response.json()['results'][0]
                return {"price": data['c'], "source": "massive"}
        except Exception:
            pass

        # --- FALLBACK TO YAHOO ---
        # Yahoo Finance does not have a 5/min limit, so we don't need the timer here.
        stock = yf.Ticker(ticker)
        price = stock.history(period="1d")['Close'].iloc[-1]
        return {"price": price, "source": "yahoo"}

# Create a global instance to share the timer across your scripts
concierge = RateLimitedConcierge()
