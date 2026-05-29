import os
import yfinance as yf
from datetime import datetime

# The 11 GICS Sector SPDR ETFs
SECTORS = {
    'XLK': 'Technology (AAPL, MSFT, NVDA)',
    'XLF': 'Financials (BRK.B, JPM, V)',
    'XLV': 'Health Care (LLY, UNH, JNJ)',
    'XLY': 'Consumer Discretionary (AMZN, TSLA, HD)',
    'XLC': 'Communication Services (META, GOOGL, NFLX)',
    'XLI': 'Industrials (GE, CAT, UBER)',
    'XLP': 'Consumer Staples (WMT, PG, KO)',
    'XLE': 'Energy (XOM, CVX, COP)',
    'XLU': 'Utilities (NEE, SO, DUK)',
    'XLRE': 'Real Estate (PLD, AMT, EQIX)',
    'XLB': 'Materials (LIN, SHW, FCX)'
}

def analyze_sectors():
    print("🌍 Sweeping GICS Sector ETFs for Macro Context...")
    macro_report = f"# DAILY MACROECONOMIC STATE ({datetime.now().strftime('%Y-%m-%d')})\n"
    macro_report += "CRITICAL CONTEXT: When scoring any stock today, you MUST consider the health of its broader sector below.\n\n"

    for ticker, name in SECTORS.items():
        try:
            etf = yf.Ticker(ticker)
            df = etf.history(period="1mo", interval="1d")
            if df.empty or len(df) < 15:
                continue

            current_price = df['Close'].iloc[-1]
            sma_15 = df['Close'].rolling(window=15).mean().iloc[-1]
            
            # Basic momentum calculation: Price vs 15-day Moving Average
            trend = "BULLISH" if current_price > sma_15 else "BEARISH"
            diff_pct = ((current_price - sma_15) / sma_15) * 100
            
            macro_report += f"- {name}: The {ticker} ETF is currently {trend} (Price is {abs(diff_pct):.2f}% {'above' if current_price > sma_15 else 'below'} its 15-day average).\n"
        except Exception as e:
            print(f"⚠️ Failed to fetch {ticker}: {e}")
            
    # Add a hard fast rule for the AI
    macro_report += "\nFIRM MANDATE: If a sector is BEARISH today, heavily discount any bullish news for individual stocks within that sector. Do not fight the broader market trend."
    
    return macro_report

def update_vault(report):
    """Saves the daily report directly into the RAG brain."""
    vault_path = "./strategy_vault"
    if not os.path.exists(vault_path):
        os.makedirs(vault_path)
        
    file_path = os.path.join(vault_path, "00_daily_macro_state.txt")
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(report)
        
    print(f"✅ Macro State injected into Vector Vault: {file_path}")
def run_macro_scan():
    report = analyze_sectors()
    update_vault(report)
    print("\n--- INJECTED CONTEXT ---")
    print(report)


if __name__ == "__main__":
    report = analyze_sectors()
    update_vault(report)
    print("\n--- INJECTED CONTEXT ---")
    print(report)
