import argparse
import math
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
import yfinance as yf

from config import get_supabase_client


# Broad keyword map for traditional, transition, clean, and futuristic energy exposure.
# The builder uses sector/industry/name/description matching, then assigns the strongest theme.
ENERGY_RULES: Dict[str, Dict[str, object]] = {
    "Traditional Energy": {
        "category": "Traditional Energy",
        "keywords": [
            "oil", "gas", "petroleum", "crude", "natural gas", "lng", "shale",
            "refining", "refinery", "midstream", "pipeline", "drilling", "offshore",
            "oilfield", "coal", "thermal coal", "metallurgical coal", "royalty trust",
            "exploration", "production", "hydrocarbon", "energy services",
        ],
        "score": 90,
    },
    "Electric Utilities": {
        "category": "Electric Utilities",
        "keywords": [
            "electric utility", "electric utilities", "regulated utility", "power generation",
            "independent power", "electricity", "transmission", "distribution utility",
            "utility holding", "utilities", "power producer",
        ],
        "score": 75,
    },
    "Renewable Power": {
        "category": "Clean Energy",
        "keywords": [
            "renewable", "renewables", "solar", "photovoltaic", "wind", "hydro",
            "hydroelectric", "geothermal", "biofuel", "biomass", "clean energy",
            "green energy", "sustainable energy", "renewable power",
        ],
        "score": 85,
    },
    "Solar": {
        "category": "Clean Energy",
        "keywords": ["solar", "photovoltaic", "pv module", "solar inverter", "microinverter"],
        "score": 88,
    },
    "Wind": {
        "category": "Clean Energy",
        "keywords": ["wind", "wind turbine", "offshore wind", "onshore wind"],
        "score": 84,
    },
    "Nuclear / Uranium": {
        "category": "Nuclear Energy",
        "keywords": [
            "nuclear", "uranium", "small modular reactor", "smr", "reactor",
            "nuclear fuel", "enrichment", "centrus", "radioisotope",
        ],
        "score": 87,
    },
    "Hydrogen / Fuel Cells": {
        "category": "Futuristic Energy",
        "keywords": ["hydrogen", "fuel cell", "electrolyzer", "electrolyser", "green hydrogen"],
        "score": 84,
    },
    "Battery / Storage": {
        "category": "Energy Storage",
        "keywords": [
            "battery", "batteries", "energy storage", "grid storage", "lithium-ion",
            "lithium ion", "solid-state battery", "flow battery", "storage systems",
        ],
        "score": 82,
    },
    "EV Charging": {
        "category": "Energy Infrastructure",
        "keywords": [
            "ev charging", "electric vehicle charging", "charging network", "charging station",
            "charging infrastructure", "fast charging", "chargepoint",
        ],
        "score": 78,
    },
    "Grid / Electrification": {
        "category": "Energy Infrastructure",
        "keywords": [
            "smart grid", "grid", "microgrid", "electrification", "power electronics",
            "inverter", "substation", "transformer", "metering", "demand response",
        ],
        "score": 74,
    },
    "Carbon Capture": {
        "category": "Energy Transition",
        "keywords": [
            "carbon capture", "ccus", "carbon sequestration", "direct air capture",
            "carbon removal", "carbon management",
        ],
        "score": 76,
    },
    "Critical Minerals": {
        "category": "Energy Materials",
        "keywords": [
            "lithium", "graphite", "cobalt", "nickel", "rare earth", "copper",
            "battery metals", "critical minerals", "vanadium",
        ],
        "score": 70,
    },
}

SECTOR_BOOSTS = {
    "energy": 20,
    "utilities": 12,
    "basic materials": 4,
    "industrials": 3,
    "technology": 2,
}

EXCLUDED_TERMS = [
    "financial services", "bank", "insurance", "biotechnology", "pharmaceutical",
    "restaurant", "apparel", "real estate investment trust",
]

DEFAULT_MIN_SCORE = 65
DEFAULT_LIMIT = 500
YAHOO_DELAY_SECONDS = 0.15


def clean_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def normalize_ticker(symbol: str) -> str:
    return clean_text(symbol).upper().replace(".", "-")


def fetch_listed_tickers() -> pd.DataFrame:
    """Fetch a broad active listing set, then keep Nasdaq and NYSE common listings."""
    url = "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/all/all_tickers.txt"
    tickers = pd.read_csv(url, header=None, names=["ticker"])
    tickers["ticker"] = tickers["ticker"].map(normalize_ticker)
    tickers = tickers[tickers["ticker"].str.len() > 0]
    tickers = tickers[~tickers["ticker"].str.contains(r"[\^/ ]", regex=True)]
    tickers = tickers.drop_duplicates().reset_index(drop=True)
    return tickers


def fetch_yahoo_info(ticker: str) -> Optional[Dict[str, object]]:
    try:
        info = yf.Ticker(ticker).info
        if not info or info.get("quoteType") not in {"EQUITY", None}:
            return None
        return info
    except Exception as exc:
        print(f"   ⚠️ {ticker}: Yahoo lookup failed: {exc}")
        return None


def infer_exchange(info: Dict[str, object]) -> Optional[str]:
    exchange = clean_text(info.get("exchange") or info.get("fullExchangeName")).upper()
    if "NASDAQ" in exchange or exchange in {"NMS", "NGM", "NCM"}:
        return "NASDAQ"
    if "NYSE" in exchange or exchange in {"NYQ", "NYE"}:
        return "NYSE"
    return None


def classify_energy(info: Dict[str, object]) -> Tuple[Optional[Dict[str, object]], List[str]]:
    name = clean_text(info.get("longName") or info.get("shortName"))
    sector = clean_text(info.get("sector"))
    industry = clean_text(info.get("industry"))
    description = clean_text(info.get("longBusinessSummary"))
    haystack = " ".join([name, sector, industry, description]).lower()

    if any(term in haystack for term in EXCLUDED_TERMS):
        # Do not hard-exclude utilities/energy companies just because a generic word appears.
        if "energy" not in haystack and "utility" not in haystack and "power" not in haystack:
            return None, []

    matches: List[Tuple[str, Dict[str, object], List[str]]] = []
    for theme, rule in ENERGY_RULES.items():
        keywords = [kw for kw in rule["keywords"] if kw in haystack]
        if keywords:
            matches.append((theme, rule, keywords))

    if not matches:
        return None, []

    best_theme, best_rule, best_keywords = max(matches, key=lambda item: item[1]["score"] + len(item[2]) * 2)
    score = float(best_rule["score"]) + min(10, len(best_keywords) * 2)
    sector_boost = SECTOR_BOOSTS.get(sector.lower(), 0)
    score = min(100.0, score + sector_boost)

    subcategory = industry or best_theme
    result = {
        "category": best_rule["category"],
        "subcategory": subcategory,
        "energy_theme": best_theme,
        "energy_purity_score": round(score, 2),
    }
    all_keywords = sorted({kw for _, _, keywords in matches for kw in keywords})
    return result, all_keywords


def to_record(ticker: str, info: Dict[str, object], classification: Dict[str, object], keywords: List[str]) -> Dict[str, object]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "ticker": ticker,
        "company_name": clean_text(info.get("longName") or info.get("shortName") or ticker),
        "exchange": infer_exchange(info),
        "sector": clean_text(info.get("sector")) or None,
        "industry": clean_text(info.get("industry")) or None,
        "category": classification["category"],
        "subcategory": classification["subcategory"],
        "energy_theme": classification["energy_theme"],
        "energy_purity_score": classification["energy_purity_score"],
        "market_cap": info.get("marketCap"),
        "country": clean_text(info.get("country")) or None,
        "website": clean_text(info.get("website")) or None,
        "description": clean_text(info.get("longBusinessSummary"))[:4000] or None,
        "matched_keywords": keywords,
        "source": "energy_universe_builder:yfinance",
        "active": True,
        "last_seen": now,
        "last_updated": now,
    }


def upsert_records(records: List[Dict[str, object]], dry_run: bool = False) -> None:
    if not records:
        print("⚠️ No energy records to upsert.")
        return

    if dry_run:
        print(f"🧪 Dry run: would upsert {len(records)} energy_universe rows.")
        for record in records[:20]:
            print(
                f"   {record['ticker']:<6} {record['exchange']:<6} "
                f"{record['energy_theme']:<22} {record['energy_purity_score']:>5} "
                f"{record['company_name']}"
            )
        if len(records) > 20:
            print(f"   ... and {len(records) - 20} more")
        return

    supabase = get_supabase_client()
    batch_size = 100
    for start in range(0, len(records), batch_size):
        batch = records[start:start + batch_size]
        supabase.table("energy_universe").upsert(batch, on_conflict="ticker").execute()
        print(f"☁️ Upserted {start + len(batch)}/{len(records)} records to Supabase.")


def build_energy_universe(limit: int, min_score: float, dry_run: bool) -> List[Dict[str, object]]:
    print("🌐 Loading active US ticker candidates...")
    listings = fetch_listed_tickers()
    print(f"✅ Loaded {len(listings)} ticker candidates. Scanning up to {limit}.")

    records: List[Dict[str, object]] = []
    scanned = 0

    for ticker in listings["ticker"].head(limit):
        scanned += 1
        info = fetch_yahoo_info(ticker)
        if not info:
            continue

        exchange = infer_exchange(info)
        if exchange not in {"NASDAQ", "NYSE"}:
            continue

        classification, keywords = classify_energy(info)
        if not classification:
            continue

        if classification["energy_purity_score"] < min_score:
            continue

        record = to_record(ticker, info, classification, keywords)
        records.append(record)
        print(
            f"   ✅ {ticker:<6} {exchange:<6} {classification['energy_theme']:<22} "
            f"score={classification['energy_purity_score']:<5} {record['company_name']}"
        )
        time.sleep(YAHOO_DELAY_SECONDS)

    records.sort(key=lambda row: (row["energy_purity_score"], row["ticker"]), reverse=True)
    print(f"🏁 Scan complete: {len(records)} energy-related tickers found from {scanned} scanned.")
    upsert_records(records, dry_run=dry_run)
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and sync a broad NYSE/Nasdaq energy stock universe.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Maximum ticker candidates to inspect.")
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE, help="Minimum energy purity score to keep.")
    parser.add_argument("--dry-run", action="store_true", help="Print matches without writing to Supabase.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    build_energy_universe(limit=args.limit, min_score=args.min_score, dry_run=args.dry_run)
