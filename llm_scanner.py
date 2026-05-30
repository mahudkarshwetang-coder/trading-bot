import os
import time
import json
import feedparser
import requests
import yfinance as yf
import pandas as pd
import chromadb
from chromadb.utils import embedding_functions

from config import OLLAMA_MODEL, OLLAMA_URL, get_supabase_client
from signal_utils import insert_signal_with_cooldown

# --- CONFIGURATION ---
supabase = get_supabase_client()

# --- LOCAL VECTOR DATABASE (RAG) SETUP ---
print("🗄️ Initializing ChromaDB Strategy Vault...")
chroma_client = chromadb.PersistentClient(path="./chroma_db")
sentence_transformer_ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
strategy_collection = chroma_client.get_or_create_collection(
    name="quant_rules", 
    embedding_function=sentence_transformer_ef
)

# --- RAG MODULE (NEW) ---
def ingest_strategy_documents():
    """Reads your text files and loads them into the Vector DB."""
    vault_path = "./strategy_vault"
    if not os.path.exists(vault_path):
        os.makedirs(vault_path)
        print("📁 Created strategy_vault directory.")
        return

    documents, ids = [], []
    for filename in os.listdir(vault_path):
        if filename.endswith(".txt") or filename.endswith(".md"):
            with open(os.path.join(vault_path, filename), 'r', encoding='utf-8') as file:
                documents.append(file.read())
                ids.append(filename)
                
    if documents:
        strategy_collection.upsert(documents=documents, ids=ids)
        print(f"🧠 Vector Database Synced: Loaded {len(documents)} proprietary strategy documents.")

def retrieve_custom_rules(query_text):
    """Searches the database for rules relevant to the current stock/news."""
    if strategy_collection.count() == 0: return ""
    results = strategy_collection.query(query_texts=[query_text], n_results=1)
    return results['documents'][0][0] if results['documents'] and results['documents'][0] else ""

# --- YOUR ORIGINAL CORE FUNCTIONS ---
def fetch_recent_headlines(ticker):
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
    feed = feedparser.parse(url)
    return [entry.title for entry in feed.entries[:5]]

def ask_local_analyst(ticker, headlines):
    news_block = "\n".join([f"- {h}" for h in headlines])
    
    # --- RAG INTERCEPT (The new logic plugged into your function) ---
    custom_rules = retrieve_custom_rules(f"Trading rules for {ticker} given events: {news_block}")
    rule_injection = ""
    if custom_rules:
        rule_injection = f"### CRITICAL PROPRIETARY FIRM RULES ###\n{custom_rules}\n#######################################\n"
        print(f"   🔐 Secret firm rules retrieved for {ticker}.")
    
    prompt = f"""
    You are an elite quantitative hedge fund analyst. 
    {rule_injection}
    Read the following 5 recent news headlines for {ticker}:
    {news_block}
    
    Analyze the subtext, context, and financial implications while STRICTLY following the firm rules above. 
    Calculate a net momentum sentiment score between -1.0 (extremely bearish) and 1.0 (extremely bullish).
    
    Return your response strictly as JSON. Do not hardcode the score.
    {{"score": <float>, "reasoning": "<brief explanation>"}}
    """
    
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.0}
    }
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=60)
        response_data = response.json()
        raw_json_string = response_data.get('response', '{}')
        analysis = json.loads(raw_json_string.strip())
        return float(analysis.get('score', 0.0)), str(analysis.get('reasoning', 'No reasoning provided.'))
    except Exception as e:
        print(f"⚠️ Local AI Parsing Error for {ticker}: {e}")
        return 0.0, "Failed to analyze locally."

def calculate_technicals(ticker):
    print(f"🧮 Generating Microstructure Matrix for {ticker}...")
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period="1mo", interval="1d")
        if df.empty or len(df) < 15:
            return None, None, None, None, None, None
            
        df['SMA_15'] = df['Close'].rolling(window=15).mean()
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI_14'] = 100 - (100 / (1 + rs))
        
        avg_volume = df['Volume'].mean()
        latest_volume = df['Volume'].iloc[-1]
        rvol = round(float(latest_volume / avg_volume), 2) if avg_volume > 0 else 1.0
        
        latest_price = round(float(df['Close'].iloc[-1]), 2)
        latest_sma = round(float(df['SMA_15'].iloc[-1]), 2)
        latest_rsi = round(float(df['RSI_14'].iloc[-1]), 1)
        
        bid_price = round(latest_price * 0.9995, 2)
        ask_price = round(latest_price * 1.0005, 2)
        
        return latest_rsi, latest_sma, latest_price, rvol, bid_price, ask_price
    except Exception as e:
        print(f"⚠️ Technical Matrix Failure for {ticker}: {e}")
        return None, None, None, None, None, None

def status_for_signal(action, is_autonomous):
    if is_autonomous and action == "BUY":
        return "approved"
    return "pending"

def push_signal_to_ipad(ticker, action, score, reasoning, headlines, channel, target_status):
    confidence = abs(score)
    memo_bullets = "\n- ".join(headlines)
    full_memo = f"LLM Analyst Reasoning:\n{reasoning}\n\nBased on these headlines:\n- {memo_bullets}"
    
    rsi_val, sma_val, price_val, rvol_val, bid_val, ask_val = calculate_technicals(ticker)
    
    payload = {
        "ticker": ticker,
        "action_type": action,
        "confidence_score": confidence,
        "investment_memo": full_memo,
        "status": target_status,
        "rsi": rsi_val,
        "sma_15": sma_val,
        "price_at_signal": price_val,
        "rvol": rvol_val,
        "bid": bid_val,
        "ask": ask_val,
        "channel": channel
    }
    
    try:
        if insert_signal_with_cooldown(
            supabase,
            payload,
            channel=channel,
            context_fragments=headlines,
        ):
            routing_msg = "AWAITING MANUAL REVIEW" if target_status == "pending" else "ROUTED DIRECTLY TO BROKER"
            print(f"📡 SIGNAL TRANSMITTED via [{channel}]: {action} {ticker} | {routing_msg}")
    except Exception as e:
        print(f"🚨 Supabase Upload Error: {e}")

def run_llm_scan():
    print("🧠 Fetching configuration profile from iPad Command Center...")
    
    # --- RAG SYNC TRIGGER ---
    ingest_strategy_documents()
    
    try:
        settings_resp = supabase.table("bot_settings").select("*").eq("id", 1).execute()
        if not settings_resp.data:
            print("🚨 Critical: Configuration profile not found in cloud.")
            return
            
        config = settings_resp.data[0]
        if not config["is_active"]:
            print("💤 System Standdown: The AI Scanner is toggled OFF. Exiting.")
            return
            
        min_confidence = config["min_confidence"]
        is_autonomous = config.get("auto_execute", config.get("autonomous_execution", False))
        
        if is_autonomous:
            print("⚠️ AUTONOMOUS MODE ENGAGED: BUY signals can bypass review; SELL signals remain pending.")
            
        manual_list = config.get("watchlist", [])
        radar_list = config.get("radar_watchlist", [])
        earnings_list = config.get("earnings_watchlist", [])
        
        ticker_channels = {}
        for t in manual_list: ticker_channels[t] = "VAULT"
        for t in radar_list: ticker_channels[t] = "VOLATILITY"
        for t in earnings_list: ticker_channels[t] = "EARNINGS"
        
        combined_watchlist = list(ticker_channels.keys())
        
        print(f"✅ Target Pool Synced: {len(combined_watchlist)} Tickers loaded.")
        print("-" * 50)
        
        for ticker in combined_watchlist:
            print(f"📰 Fetching news for {ticker}...")
            headlines = fetch_recent_headlines(ticker)
            if not headlines:
                continue
                
            print(f"🤖 Asking Qwen 2.5 Coder to analyze context locally...")
            score, reasoning = ask_local_analyst(ticker, headlines)
            
            if score >= min_confidence: 
                action = "BUY"
                push_signal_to_ipad(
                    ticker,
                    action,
                    score,
                    reasoning,
                    headlines,
                    ticker_channels[ticker],
                    status_for_signal(action, is_autonomous),
                )
            elif score <= -min_confidence:
                action = "SELL"
                push_signal_to_ipad(
                    ticker,
                    action,
                    score,
                    reasoning,
                    headlines,
                    ticker_channels[ticker],
                    status_for_signal(action, is_autonomous),
                )
            else:
                print(f"   ↳ State: Neutral Market Subtext. Skipping.")
            time.sleep(1.5)
            
    except Exception as e:
        print(f"🚨 Connection Failure during setup scan: {e}")
    print("✅ Dynamic LLM Intelligence Scan Complete.")

if __name__ == "__main__":
    run_llm_scan()
