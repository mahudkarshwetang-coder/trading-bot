import time
import feedparser
from config import get_supabase_client
from signal_utils import insert_signal_with_cooldown

# --- CONFIGURATION ---
supabase = get_supabase_client()
WATCHLIST = ['AAPL', 'NVDA', 'TSLA', 'MSFT']

# --- THE NLP BRAIN (Lexicon) ---
BULLISH_WORDS = ['surge', 'beat', 'growth', 'upgrade', 'jump', 'record', 'soar', 'buy', 'outperform']
BEARISH_WORDS = ['miss', 'decline', 'drop', 'downgrade', 'fall', 'plunge', 'sell', 'underperform', 'lawsuit']

def analyze_headline_sentiment(headline):
    """Scans a sentence and calculates a basic Bull/Bear momentum score."""
    words = headline.lower().split()
    
    bull_score = sum(1 for word in words if any(bull in word for bull in BULLISH_WORDS))
    bear_score = sum(1 for word in words if any(bear in word for bear in BEARISH_WORDS))
    
    # Calculate net sentiment (-1.0 to 1.0)
    total_matches = bull_score + bear_score
    if total_matches == 0:
        return 0.0
        
    return (bull_score - bear_score) / total_matches

def fetch_and_analyze_news(ticker):
    """Pulls the live Yahoo Finance RSS feed for a specific ticker."""
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
    feed = feedparser.parse(url)
    
    print(f"📰 Scanning {len(feed.entries)} recent headlines for {ticker}...")
    
    total_sentiment = 0.0
    impactful_headlines = []
    
    # Read the top 5 most recent articles
    for entry in feed.entries[:5]:
        score = analyze_headline_sentiment(entry.title)
        total_sentiment += score
        
        if score != 0:
            impactful_headlines.append(entry.title)
            
    # Average the sentiment of the top 5 articles
    avg_sentiment = total_sentiment / 5
    return avg_sentiment, impactful_headlines

def push_signal_to_ipad(ticker, action, score, headlines):
    """Formats the NLP memo and uploads it to Supabase."""
    # Convert sentiment score (-1 to 1) into a confidence percentage
    confidence = abs(score)
    
    # Format the AI Memo with the actual headlines it read
    memo_bullets = "\n- ".join(headlines)
    memo = f"NLP Sentiment Scanner detected extreme {action} momentum based on recent news velocity:\n- {memo_bullets}"
    
    payload = {
        "ticker": ticker,
        "action_type": action,
        "confidence_score": confidence,
        "investment_memo": memo,
        "status": "pending",
        "channel": "SENTIMENT"
    }
    
    try:
        if insert_signal_with_cooldown(supabase, payload, channel="SENTIMENT"):
            print(f"📡 SIGNAL TRANSMITTED: {action} {ticker} sent to iPad Dashboard.")
    except Exception as e:
        print(f"🚨 Supabase Upload Error: {e}")

def run_nlp_scan():
    print("🧠 AI Sentiment Scanner Online. Reading live news feeds...")
    print("-" * 50)
    
    for ticker in WATCHLIST:
        sentiment, headlines = fetch_and_analyze_news(ticker)
        
        print(f"   ↳ Net Sentiment Score: {sentiment:.2f}")
        
        # --- THE TRADING LOGIC ---
        # If the news is overwhelmingly positive
        if sentiment >= 0.4:
            push_signal_to_ipad(ticker, "BUY", sentiment, headlines)
            
        # If the news is overwhelmingly negative
        elif sentiment <= -0.4:
            push_signal_to_ipad(ticker, "SELL", sentiment, headlines)
            
        else:
            print(f"   ↳ State: Neutral/Mixed News. Skipping.")
            
        time.sleep(1) # Be polite to Yahoo's servers
        
    print("-" * 50)
    print("✅ NLP Scan Complete.")

if __name__ == "__main__":
    run_nlp_scan()
