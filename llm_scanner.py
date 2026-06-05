import os
import time
import json
import re
import feedparser
import requests
import yfinance as yf
import pandas as pd
import chromadb
from chromadb.utils import embedding_functions
from urllib.parse import urlsplit, urlunsplit

from config import (
    LLM_SCANNER_KEEP_ALIVE,
    LLM_SCANNER_MAX_CONSECUTIVE_FAILURES,
    LLM_SCANNER_MAX_TICKERS,
    LLM_SCANNER_NUM_PREDICT,
    LLM_SCANNER_RETRY_ATTEMPTS,
    LLM_SCANNER_RETRY_BACKOFF_SECONDS,
    LLM_SCANNER_RULES_MAX_CHARS,
    LLM_SCANNER_TIMEOUT_SECONDS,
    OLLAMA_MODEL,
    OLLAMA_URL,
    get_supabase_client,
)
from llm_metrics import record_llm_metric
from performance_governor import (
    adjust_ollama_runtime,
    adjust_ticker_count,
    gaming_budget_pause,
    print_profile_notice,
)
from signal_quality_filter import review_signal_quality
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

MARKET_CONTEXT_SYMBOLS = {
    "SPY", "QQQ", "DIA", "IWM", "VIX",
    "XLK", "XLE", "XLI", "XLB", "XLF", "XLV", "XLU", "XLY", "XLP",
    "SMH", "SOXX", "KRE", "TLT", "HYG", "LQD",
}

NON_TICKER_ACRONYMS = {
    "ADR", "AI", "API", "CEO", "CFO", "CPI", "EPS", "ETF", "FDA", "FOMC",
    "GDP", "IPO", "LLM", "LNG", "M&A", "OPEC", "R&D", "SEC", "USD",
}


def extract_json_object(text):
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    cleaned = cleaned.replace("<|im_start|>", "").replace("<|im_end|>", "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    decoder = json.JSONDecoder()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    for marker in ("{", "["):
        start = cleaned.find(marker)
        if start < 0:
            continue
        try:
            parsed, _ = decoder.raw_decode(cleaned[start:])
            if isinstance(parsed, list):
                return {"items": parsed}
            return parsed
        except Exception:
            continue

    raise json.JSONDecodeError("No valid JSON object found", cleaned, 0)


def trim_rules(text, max_chars=LLM_SCANNER_RULES_MAX_CHARS):
    value = str(text or "").strip()
    limit = max(200, int(max_chars))
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n[Rules truncated for latency]"


def ollama_chat_url():
    parsed = urlsplit(OLLAMA_URL)
    if not parsed.scheme or not parsed.netloc:
        return "http://localhost:11434/api/chat"
    return urlunsplit((parsed.scheme, parsed.netloc, "/api/chat", "", ""))


def ollama_uses_chat_endpoint():
    parsed = urlsplit(OLLAMA_URL)
    return parsed.path.rstrip("/").lower().endswith("/api/chat")


def extract_ollama_text(response_data):
    if not isinstance(response_data, dict):
        return ""
    response_text = str(response_data.get("response") or "").strip()
    if response_text:
        return response_text
    message = response_data.get("message")
    if isinstance(message, dict):
        content_text = str(message.get("content") or "").strip()
        if content_text:
            return content_text
    return ""


def build_analysis_schema():
    return {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The ticker being analyzed.",
            },
            "score": {
                "type": "number",
                "description": "Net momentum score from -1.0 bearish to 1.0 bullish.",
            },
            "reasoning": {
                "type": "string",
                "description": "One short sentence explaining the score.",
            },
        },
        "required": ["ticker", "score", "reasoning"],
        "additionalProperties": False,
    }


def uppercase_symbols(text):
    return set(re.findall(r"\b[A-Z][A-Z0-9]{1,5}\b", str(text or "")))


def find_unrelated_ticker_mentions(ticker, reasoning, headlines):
    ticker = str(ticker or "").upper()
    allowed = {ticker, *MARKET_CONTEXT_SYMBOLS, *NON_TICKER_ACRONYMS}
    headline_symbols = uppercase_symbols(" ".join(headlines or []))
    allowed.update(headline_symbols)
    return sorted(symbol for symbol in uppercase_symbols(reasoning) if symbol not in allowed)


def validate_analysis_scope(ticker, analysis, reasoning, headlines):
    expected = str(ticker or "").upper()
    returned = str(analysis.get("ticker") or expected).strip().upper()
    if returned and returned != expected:
        return f"returned ticker {returned} instead of {expected}"

    unrelated = find_unrelated_ticker_mentions(expected, reasoning, headlines)
    if unrelated:
        return f"referenced unrelated ticker(s): {', '.join(unrelated[:5])}"

    return ""


def warm_ollama_model():
    """Warm model weights into memory to reduce first-request latency spikes."""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": "Return strict JSON: {\"ok\": true}",
        "format": "json",
        "stream": False,
        "keep_alive": LLM_SCANNER_KEEP_ALIVE,
        "options": {
            "temperature": 0.0,
            "num_predict": 32,
        },
    }
    try:
        requests.post(OLLAMA_URL, json=payload, timeout=max(20, LLM_SCANNER_TIMEOUT_SECONDS)).raise_for_status()
        print("🔥 Ollama warm-up complete.")
    except Exception as exc:
        print(f"⚠️ Ollama warm-up skipped: {exc}")

def warm_ollama_model_safe():
    """Compatibility warm-up that supports both /api/generate and /api/chat."""
    runtime = adjust_ollama_runtime(
        "llm_scanner_warmup",
        keep_alive=LLM_SCANNER_KEEP_ALIVE,
        timeout_seconds=LLM_SCANNER_TIMEOUT_SECONDS,
        num_predict=32,
        warmup=True,
    )
    if runtime["profile"].active:
        print_profile_notice("llm_scanner_warmup", prefix="[LLM SCANNER]")
    if not runtime.get("warmup"):
        print("[LLM SCANNER] Ollama warm-up skipped by performance governor.")
        return

    try:
        if ollama_uses_chat_endpoint():
            payload = {
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": "Return strict JSON: {\"ok\": true}"}],
                "format": "json",
                "stream": False,
                "keep_alive": runtime["keep_alive"],
                "think": False,
                "options": {"temperature": 0.0, "num_predict": 32},
            }
            requests.post(OLLAMA_URL, json=payload, timeout=max(20, int(runtime["timeout_seconds"]))).raise_for_status()
        else:
            payload = {
                "model": OLLAMA_MODEL,
                "prompt": "Return strict JSON: {\"ok\": true}",
                "format": "json",
                "stream": False,
                "keep_alive": runtime["keep_alive"],
                "think": False,
                "options": {"temperature": 0.0, "num_predict": 32},
            }
            requests.post(OLLAMA_URL, json=payload, timeout=max(20, int(runtime["timeout_seconds"]))).raise_for_status()
        print("[LLM SCANNER] Ollama warm-up complete.")
    except Exception as exc:
        print(f"[LLM SCANNER] Ollama warm-up skipped: {exc}")


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
        compact_rules = trim_rules(custom_rules, max_chars=LLM_SCANNER_RULES_MAX_CHARS)
        rule_injection = f"### CRITICAL PROPRIETARY FIRM RULES ###\n{compact_rules}\n#######################################\n"
        print(f"   🔐 Secret firm rules retrieved for {ticker}.")
    
    prompt = f"""
You are an elite quantitative hedge fund analyst.
{rule_injection}
Ticker: {ticker}
Recent headlines:
{news_block}

Analyze the subtext, context, and financial implications while strictly following the firm rules above.
Calculate a net momentum sentiment score between -1.0, extremely bearish, and 1.0, extremely bullish.
Do not base the score on a different company or ticker. If the evidence is about another company, return score 0.0.
Do not mention unrelated tickers in reasoning unless they appear in the headlines or are broad market/sector proxies.

Return only minified JSON with:
{{"ticker": "{ticker}", "score": <float>, "reasoning": "<one sentence, max 18 words>"}}
"""

    runtime = adjust_ollama_runtime(
        "llm_scanner",
        keep_alive=LLM_SCANNER_KEEP_ALIVE,
        timeout_seconds=LLM_SCANNER_TIMEOUT_SECONDS,
        num_predict=max(120, int(LLM_SCANNER_NUM_PREDICT)),
    )
    if runtime["profile"].active:
        print_profile_notice("llm_scanner_analysis", prefix="[LLM SCANNER]")

    num_predict = max(64, int(runtime["num_predict"]))
    gaming_budget_pause("llm_scanner_qwen", estimated_work_seconds=2.0)
    
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "format": build_analysis_schema(),
        "stream": False,
        "keep_alive": runtime["keep_alive"],
        "think": False,
        "options": {
            "temperature": 0.0,
            "num_predict": num_predict,
        }
    }
    chat_payload = {
        "model": payload["model"],
        "messages": [{"role": "user", "content": payload["prompt"]}],
        "format": payload["format"],
        "stream": False,
        "keep_alive": payload["keep_alive"],
        "think": False,
        "options": payload["options"],
    }
    json_mode_payload = dict(payload)
    json_mode_payload["format"] = "json"
    json_mode_chat_payload = dict(chat_payload)
    json_mode_chat_payload["format"] = "json"
    attempts = max(1, int(LLM_SCANNER_RETRY_ATTEMPTS))
    timeout_seconds = max(20, int(runtime["timeout_seconds"]))
    backoff_seconds = max(1, int(LLM_SCANNER_RETRY_BACKOFF_SECONDS))
    last_error = None
    started_at = time.perf_counter()
    endpoint_label = "chat" if ollama_uses_chat_endpoint() else "generate"
    raw_json_string = ""
    attempt = 0

    for attempt in range(1, attempts + 1):
        try:
            if ollama_uses_chat_endpoint():
                response = requests.post(OLLAMA_URL, json=chat_payload, timeout=timeout_seconds)
            else:
                response = requests.post(OLLAMA_URL, json=payload, timeout=timeout_seconds)
            response.raise_for_status()
            response_data = response.json()
            raw_json_string = extract_ollama_text(response_data)
            if not raw_json_string and not ollama_uses_chat_endpoint():
                chat_response = requests.post(ollama_chat_url(), json=chat_payload, timeout=timeout_seconds)
                chat_response.raise_for_status()
                chat_data = chat_response.json()
                raw_json_string = extract_ollama_text(chat_data)
            if not raw_json_string:
                raise RuntimeError("Ollama returned empty response content.")
            try:
                analysis = extract_json_object(raw_json_string)
            except Exception:
                if ollama_uses_chat_endpoint():
                    fallback_response = requests.post(
                        OLLAMA_URL,
                        json=json_mode_chat_payload,
                        timeout=timeout_seconds,
                    )
                else:
                    fallback_response = requests.post(
                        OLLAMA_URL,
                        json=json_mode_payload,
                        timeout=timeout_seconds,
                    )
                fallback_response.raise_for_status()
                fallback_data = fallback_response.json()
                raw_json_string = extract_ollama_text(fallback_data)
                if not raw_json_string:
                    raise RuntimeError("Ollama returned empty response content after JSON fallback.")
                analysis = extract_json_object(raw_json_string)

            score = float(analysis.get("score", 0.0))
            score = max(-1.0, min(1.0, score))
            reasoning = str(analysis.get("reasoning", "No reasoning provided.")).strip()
            scope_error = validate_analysis_scope(ticker, analysis, reasoning, headlines)
            if scope_error:
                print(
                    f"[LLM SCANNER] Neutralized {ticker}: "
                    f"local analysis {scope_error}."
                )
                score = 0.0
                reasoning = f"Rejected local analysis: {scope_error}."
            record_llm_metric(
                source="llm_scanner",
                task="ticker_sentiment",
                model=OLLAMA_MODEL,
                duration_seconds=time.perf_counter() - started_at,
                success=True,
                endpoint=endpoint_label,
                item_count=1,
                batch_size=1,
                attempts=attempt,
                timeout_seconds=timeout_seconds,
                num_predict=num_predict,
                prompt_chars=len(prompt),
                response_chars=len(raw_json_string),
                extra={"ticker": ticker, "headline_count": len(headlines)},
            )
            return score, reasoning
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                wait_seconds = backoff_seconds * attempt
                print(
                    f"   ⚠️ Local AI timeout/error for {ticker} "
                    f"(attempt {attempt}/{attempts}): {exc}. Retrying in {wait_seconds}s..."
                )
                time.sleep(wait_seconds)
                continue
            break

    record_llm_metric(
        source="llm_scanner",
        task="ticker_sentiment",
        model=OLLAMA_MODEL,
        duration_seconds=time.perf_counter() - started_at,
        success=False,
        endpoint=endpoint_label,
        item_count=1,
        batch_size=1,
        attempts=attempt or attempts,
        timeout_seconds=timeout_seconds,
        num_predict=num_predict,
        prompt_chars=len(prompt),
        response_chars=len(raw_json_string),
        error=last_error,
        extra={"ticker": ticker, "headline_count": len(headlines)},
    )
    print(f"⚠️ Local AI Parsing Error for {ticker}: {last_error}")
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
    confidence = round(abs(score) * 100.0, 2)
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
        approved, decision, payload = review_signal_quality(
            payload,
            headlines=headlines,
            reasoning=reasoning,
        )
        if not approved:
            print(
                f"Qwen quality gate blocked {action} {ticker}: "
                f"score={decision['quality_score']} reason={decision['rationale']}"
            )
            return

        if insert_signal_with_cooldown(
            supabase,
            payload,
            channel=channel,
            context_fragments=headlines,
        ):
            routing_msg = "AWAITING MANUAL REVIEW" if target_status == "pending" else "APPROVED FOR EXECUTION BRIDGE"
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
        max_tickers = max(0, int(LLM_SCANNER_MAX_TICKERS))
        if max_tickers > 0 and len(combined_watchlist) > max_tickers:
            original_count = len(combined_watchlist)
            combined_watchlist = combined_watchlist[:max_tickers]
            print(
                f"[LLM SCANNER] Ticker cap active: "
                f"{original_count} -> {len(combined_watchlist)} "
                f"(LLM_SCANNER_MAX_TICKERS={max_tickers})"
            )
        governed_limit = adjust_ticker_count(len(combined_watchlist))
        if governed_limit < len(combined_watchlist):
            original_count = len(combined_watchlist)
            combined_watchlist = combined_watchlist[:governed_limit]
            print_profile_notice("llm_scanner_ticker_cap", prefix="[LLM SCANNER]")
            print(
                f"[LLM SCANNER] Performance ticker cap active: "
                f"{original_count} -> {len(combined_watchlist)}"
            )

        if combined_watchlist:
            warm_ollama_model_safe()
        
        print(f"✅ Target Pool Synced: {len(combined_watchlist)} Tickers loaded.")
        print("-" * 50)

        consecutive_llm_failures = 0
        max_consecutive_failures = max(0, int(LLM_SCANNER_MAX_CONSECUTIVE_FAILURES))

        for ticker in combined_watchlist:
            print(f"📰 Fetching news for {ticker}...")
            headlines = fetch_recent_headlines(ticker)
            if not headlines:
                continue
                
            print(f"[LLM SCANNER] Asking {OLLAMA_MODEL} to analyze context locally...")
            score, reasoning = ask_local_analyst(ticker, headlines)

            if reasoning == "Failed to analyze locally.":
                consecutive_llm_failures += 1
                if (
                    max_consecutive_failures > 0
                    and consecutive_llm_failures >= max_consecutive_failures
                ):
                    print(
                        "[LLM SCANNER] Stopping LLM pass after "
                        f"{consecutive_llm_failures} consecutive local analysis failures. "
                        "Check Ollama/model output before retrying."
                    )
                    break
            else:
                consecutive_llm_failures = 0

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
