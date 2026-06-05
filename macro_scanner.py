import math
import os
from datetime import datetime

import yfinance as yf


# The 11 GICS Sector SPDR ETFs.
SECTORS = {
    "XLK": "Technology (AAPL, MSFT, NVDA)",
    "XLF": "Financials (BRK.B, JPM, V)",
    "XLV": "Health Care (LLY, UNH, JNJ)",
    "XLY": "Consumer Discretionary (AMZN, TSLA, HD)",
    "XLC": "Communication Services (META, GOOGL, NFLX)",
    "XLI": "Industrials (GE, CAT, UBER)",
    "XLP": "Consumer Staples (WMT, PG, KO)",
    "XLE": "Energy (XOM, CVX, COP)",
    "XLU": "Utilities (NEE, SO, DUK)",
    "XLRE": "Real Estate (PLD, AMT, EQIX)",
    "XLB": "Materials (LIN, SHW, FCX)",
}


def analyze_sectors():
    print("[MACRO] Sweeping GICS Sector ETFs for macro context...")
    macro_report = f"# DAILY MACROECONOMIC STATE ({datetime.now().strftime('%Y-%m-%d')})\n"
    macro_report += "CRITICAL CONTEXT: When scoring any stock today, you MUST consider the health of its broader sector below.\n\n"
    valid_rows = 0

    for ticker, name in SECTORS.items():
        try:
            etf = yf.Ticker(ticker)
            df = etf.history(period="1mo", interval="1d")
            if df.empty:
                print(f"[MACRO] Skipping {ticker}: empty history.")
                continue

            close_series = df["Close"].dropna()
            if len(close_series) < 15:
                print(f"[MACRO] Skipping {ticker}: insufficient non-null close history.")
                continue

            current_price = float(close_series.iloc[-1])
            sma_15 = float(close_series.rolling(window=15).mean().iloc[-1])
            if not (math.isfinite(current_price) and math.isfinite(sma_15)) or sma_15 <= 0:
                print(f"[MACRO] Skipping {ticker}: invalid price/SMA values.")
                continue

            diff_pct = ((current_price - sma_15) / sma_15) * 100
            if not math.isfinite(diff_pct):
                print(f"[MACRO] Skipping {ticker}: invalid trend percentage.")
                continue

            trend = "BULLISH" if current_price > sma_15 else "BEARISH"
            direction = "above" if current_price > sma_15 else "below"
            macro_report += (
                f"- {name}: The {ticker} ETF is currently {trend} "
                f"(Price is {abs(diff_pct):.2f}% {direction} its 15-day average).\n"
            )
            valid_rows += 1
        except Exception as exc:
            print(f"[MACRO] Failed to fetch {ticker}: {exc}")

    if valid_rows == 0:
        print("[MACRO] No valid sector rows produced; preserving existing vault macro state.")
        return None

    macro_report += f"\nVALIDATION: {valid_rows}/{len(SECTORS)} sector ETFs produced finite price/SMA values."
    macro_report += "\nFIRM MANDATE: If a sector is BEARISH today, heavily discount any bullish news for individual stocks within that sector. Do not fight the broader market trend."
    return macro_report


def update_vault(report):
    """Save a valid daily report directly into the RAG brain."""
    if not report:
        return False

    vault_path = "./strategy_vault"
    if not os.path.exists(vault_path):
        os.makedirs(vault_path)

    file_path = os.path.join(vault_path, "00_daily_macro_state.txt")
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write(report)

    print(f"[MACRO] Macro state injected into Vector Vault: {file_path}")
    return True


def run_macro_scan():
    report = analyze_sectors()
    if not update_vault(report):
        return False
    print("\n--- INJECTED CONTEXT ---")
    print(report)
    return True


if __name__ == "__main__":
    report = analyze_sectors()
    if update_vault(report):
        print("\n--- INJECTED CONTEXT ---")
        print(report)
