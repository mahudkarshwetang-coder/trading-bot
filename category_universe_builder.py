import argparse
import math
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf

from config import CATEGORY_MIN_SCORE, CATEGORY_UNIVERSE_LIMIT, get_supabase_client
from performance_governor import print_compute_notice

YAHOO_DELAY_SECONDS = 0.15

CATEGORY_RULES: Dict[str, Dict[str, Dict[str, object]]] = {
    "Nuclear Energy": {
        "Uranium / Fuel Cycle": {
            "keywords": [
                "uranium", "uranium mining", "nuclear fuel", "fuel cycle",
                "enrichment", "conversion", "yellowcake", "u3o8",
            ],
            "score": 91,
        },
        "Reactors / Utilities": {
            "keywords": [
                "nuclear power", "nuclear generation", "nuclear plant",
                "nuclear reactor", "reactor", "baseload power", "clean firm power",
            ],
            "score": 90,
        },
        "SMR / Advanced Nuclear": {
            "keywords": [
                "small modular reactor", "smr", "advanced reactor", "microreactor",
                "modular reactor", "nuclear battery", "fast reactor",
            ],
            "score": 92,
        },
        "Nuclear Services / Components": {
            "keywords": [
                "nuclear services", "nuclear engineering", "reactor components",
                "nuclear safety", "nuclear technology", "nuclear instrumentation",
            ],
            "score": 84,
        },
    },
    "Energy": {
        "Traditional Energy": {
            "keywords": [
                "oil", "gas", "petroleum", "crude", "natural gas", "lng", "pipeline",
                "midstream", "refining", "drilling", "oilfield", "coal", "hydrocarbon",
            ],
            "score": 88,
        },
        "Clean Energy": {
            "keywords": [
                "renewable", "solar", "wind", "hydro", "geothermal", "biofuel",
                "clean energy", "sustainable energy", "power generation",
            ],
            "score": 84,
        },
        "Nuclear / Uranium": {
            "keywords": ["nuclear", "uranium", "reactor", "nuclear fuel", "enrichment"],
            "score": 90,
        },
        "Storage / Hydrogen": {
            "keywords": ["battery", "energy storage", "hydrogen", "fuel cell", "electrolyzer"],
            "score": 80,
        },
    },
    "Logistics": {
        "Rail / Trucking": {
            "keywords": [
                "railroad", "railway", "trucking", "truckload", "freight", "intermodal",
                "less-than-truckload", "transportation services",
            ],
            "score": 86,
        },
        "Shipping / Marine": {
            "keywords": ["shipping", "marine", "vessel", "container", "tanker", "dry bulk", "port"],
            "score": 84,
        },
        "Air Cargo / Delivery": {
            "keywords": ["air cargo", "parcel", "courier", "delivery", "logistics network"],
            "score": 82,
        },
        "Warehousing / Supply Chain": {
            "keywords": ["warehouse", "warehousing", "supply chain", "distribution center", "fulfillment"],
            "score": 78,
        },
    },
    "Infrastructure": {
        "Construction / Engineering": {
            "keywords": [
                "construction", "engineering", "civil construction",
                "civil infrastructure", "public infrastructure",
                "transportation infrastructure", "water infrastructure",
                "utility infrastructure", "infrastructure contractor",
                "project management", "epc", "design-build",
            ],
            "score": 86,
        },
        "Industrial Equipment": {
            "keywords": [
                "machinery", "heavy equipment", "construction equipment", "crane",
                "industrial equipment", "automation equipment",
            ],
            "score": 80,
        },
        "Grid / Utilities": {
            "keywords": [
                "electric utility", "transmission", "distribution", "substation",
                "grid", "transformer", "power infrastructure",
            ],
            "score": 82,
        },
        "Telecom / Data Infrastructure": {
            "keywords": ["tower", "fiber", "broadband", "data center", "network infrastructure"],
            "score": 76,
        },
    },
    "Materials": {
        "Steel / Cement / Aggregates": {
            "keywords": [
                "steel", "cement", "concrete", "aggregates", "asphalt", "rebar",
                "building materials", "construction materials",
            ],
            "score": 86,
        },
        "Copper / Aluminum": {
            "keywords": ["copper", "aluminum", "aluminium", "smelting", "wire rod"],
            "score": 84,
        },
        "Critical Minerals": {
            "keywords": [
                "lithium", "graphite", "cobalt", "nickel", "rare earth", "uranium",
                "battery metals", "critical minerals", "vanadium",
            ],
            "score": 84,
        },
        "Chemicals / Inputs": {
            "keywords": ["chemical", "resin", "polymer", "fertilizer", "industrial gases", "specialty materials"],
            "score": 74,
        },
    },
    "AI Chips": {
        "AI Accelerators / GPUs": {
            "keywords": [
                "graphics processor", "gpu", "accelerator", "ai accelerator",
                "artificial intelligence accelerator", "machine learning accelerator",
                "inference", "training chip", "parallel computing", "cuda",
            ],
            "score": 90,
        },
        "Semiconductors / Foundry": {
            "keywords": [
                "semiconductor", "integrated circuit", "microprocessor", "system on a chip",
                "asic", "fabless", "foundry", "wafer", "chip design",
            ],
            "score": 88,
        },
        "Semiconductor Equipment": {
            "keywords": [
                "semiconductor equipment", "lithography", "etch", "deposition",
                "metrology", "wafer fabrication", "process control", "photomask",
            ],
            "score": 86,
        },
        "Memory / Interconnect": {
            "keywords": [
                "memory", "dram", "hbm", "high bandwidth memory", "nand",
                "ethernet", "networking chip", "optical interconnect", "switching",
                "data center connectivity",
            ],
            "score": 84,
        },
        "AI Data Center Infrastructure": {
            "keywords": [
                "ai infrastructure", "data center", "server", "rack", "liquid cooling",
                "power management", "optical networking", "high performance computing",
                "cloud infrastructure", "compute infrastructure",
            ],
            "score": 80,
        },
    },
    "Data Center Power & Grid Infrastructure": {
        "Grid Equipment / Electrification": {
            "keywords": [
                "grid modernization", "electrical grid", "power grid", "transmission",
                "distribution", "substation", "transformer", "switchgear",
                "circuit breaker", "power electronics", "electrification",
                "electrical equipment", "power systems", "grid automation",
                "smart grid",
            ],
            "score": 92,
        },
        "Data Center Power / Cooling": {
            "keywords": [
                "data center power", "data centre power", "critical power",
                "power distribution unit", "pdu", "uninterruptible power supply",
                "ups", "backup power", "generator", "thermal management",
                "liquid cooling", "cooling systems", "rack power",
                "data center cooling", "data centre cooling",
            ],
            "score": 91,
        },
        "Electrical Contractors / EPC": {
            "keywords": [
                "electrical contractor", "electrical construction",
                "engineering procurement construction", "epc", "utility infrastructure",
                "power infrastructure", "grid interconnection", "high-voltage",
                "mission critical", "data center construction",
            ],
            "score": 88,
        },
        "Power Generation / Capacity": {
            "keywords": [
                "independent power producer", "merchant power", "power generation",
                "electricity generation", "electric utility", "capacity market",
                "baseload", "dispatchable power", "natural gas generation",
                "gas turbine", "turbine", "distributed generation",
            ],
            "score": 87,
        },
        "Data Center REITs / Operators": {
            "keywords": [
                "data center reit", "data centre reit", "colocation",
                "interconnection", "hyperscale", "data center services",
                "data centre services", "digital infrastructure",
            ],
            "score": 84,
        },
    },
    "Aerospace Defense & Security": {
        "Defense Primes / Platforms": {
            "keywords": [
                "aerospace and defense", "defense contractor", "defence contractor",
                "military aircraft", "combat systems", "naval", "shipbuilding",
                "armored vehicle", "weapons systems", "defense systems",
                "defence systems", "military systems",
            ],
            "score": 90,
        },
        "Missiles / Air Defense": {
            "keywords": [
                "missile", "missile defense", "missile defence", "air defense",
                "air defence", "munitions", "ordnance", "hypersonic",
                "rocket systems", "precision weapons", "interceptor",
            ],
            "score": 92,
        },
        "Drones / Autonomous Defense": {
            "keywords": [
                "unmanned", "drone", "uav", "uas", "autonomous systems",
                "counter-drone", "counter uas", "loitering munition",
                "robotic combat", "autonomous defense",
            ],
            "score": 89,
        },
        "Space / Satellites / ISR": {
            "keywords": [
                "satellite", "space systems", "geospatial intelligence",
                "isr", "intelligence surveillance reconnaissance",
                "surveillance", "reconnaissance", "radar", "remote sensing",
                "space defense", "space defence",
            ],
            "score": 88,
        },
        "Electronic Warfare / Secure Comms": {
            "keywords": [
                "electronic warfare", "signals intelligence", "secure communications",
                "tactical communications", "command and control", "c4isr",
                "c5isr", "avionics", "military electronics", "defense electronics",
            ],
            "score": 87,
        },
    },
    "Cybersecurity & AI Security": {
        "Cloud / Endpoint / Network Security": {
            "keywords": [
                "cybersecurity", "cyber security", "endpoint security",
                "cloud security", "network security", "firewall", "zero trust",
                "threat detection", "threat intelligence", "security operations",
                "security platform", "extended detection and response", "xdr",
            ],
            "score": 92,
        },
        "Identity / Access / Machine Identity": {
            "keywords": [
                "identity security", "identity access", "access management",
                "privileged access", "authentication", "machine identity",
                "secrets management", "certificate management",
                "identity governance",
            ],
            "score": 89,
        },
        "AI Security / Governance": {
            "keywords": [
                "ai security", "artificial intelligence security", "model security",
                "llm security", "ai governance", "secure ai", "prompt injection",
                "model risk", "ai risk management", "security for ai",
            ],
            "score": 90,
        },
        "Application / Data Security": {
            "keywords": [
                "application security", "appsec", "data security", "data protection",
                "vulnerability management", "devsecops", "secure software",
                "data loss prevention", "dlp", "runtime security",
            ],
            "score": 86,
        },
    },
}

SECTOR_BOOSTS = {
    "energy": 12,
    "industrials": 10,
    "basic materials": 12,
    "utilities": 8,
    "technology": 3,
    "communication services": 2,
    "real estate": 3,
}

EXCLUDED_TERMS = [
    "biotechnology", "pharmaceutical", "bank", "insurance", "asset management",
    "closed-end fund", "reit", "restaurant", "apparel", "nuclear medicine",
    "radiopharmaceutical", "medical isotope",
]

EXCLUSION_OVERRIDE_TERMS = [
    "energy", "logistics", "infrastructure", "materials", "mining",
    "semiconductor", "chip", "integrated circuit", "data center",
    "ai infrastructure", "high performance computing", "uranium",
    "nuclear power", "nuclear reactor", "small modular reactor",
    "nuclear fuel", "reactor", "grid modernization", "power grid",
    "transformer", "switchgear", "critical power", "liquid cooling",
    "data center power", "data center reit", "defense contractor",
    "aerospace and defense", "missile", "munitions", "drone", "uav",
    "satellite", "electronic warfare", "secure communications",
]


def clean_text(value):
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def normalize_ticker(symbol):
    return clean_text(symbol).upper().replace(".", "-")


def fetch_listed_tickers():
    url = "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/all/all_tickers.txt"
    tickers = pd.read_csv(url, header=None, names=["ticker"])
    tickers["ticker"] = tickers["ticker"].map(normalize_ticker)
    tickers = tickers[tickers["ticker"].str.len() > 0]
    tickers = tickers[~tickers["ticker"].str.contains(r"[\^/ ]", regex=True)]
    return tickers.drop_duplicates().reset_index(drop=True)


def fetch_yahoo_info(ticker):
    try:
        info = yf.Ticker(ticker).info
        if not info or info.get("quoteType") not in {"EQUITY", None}:
            return None
        return info
    except Exception as exc:
        print(f"   {ticker}: Yahoo lookup failed: {exc}")
        return None
    finally:
        time.sleep(YAHOO_DELAY_SECONDS)


def infer_exchange(info):
    exchange = clean_text(info.get("exchange") or info.get("fullExchangeName")).upper()
    if "NASDAQ" in exchange or exchange in {"NMS", "NGM", "NCM"}:
        return "NASDAQ"
    if "NYSE" in exchange or exchange in {"NYQ", "NYE"}:
        return "NYSE"
    return None


def classify_categories(info):
    name = clean_text(info.get("longName") or info.get("shortName"))
    sector = clean_text(info.get("sector"))
    industry = clean_text(info.get("industry"))
    description = clean_text(info.get("longBusinessSummary"))
    haystack = " ".join([name, sector, industry, description]).lower()

    if any(term in haystack for term in EXCLUDED_TERMS):
        if not any(core in haystack for core in EXCLUSION_OVERRIDE_TERMS):
            return []

    rows = []
    sector_boost = SECTOR_BOOSTS.get(sector.lower(), 0)
    for category, themes in CATEGORY_RULES.items():
        for theme, rule in themes.items():
            keywords = [keyword for keyword in rule["keywords"] if keyword in haystack]
            if not keywords:
                continue
            score = min(100.0, float(rule["score"]) + sector_boost + min(10, len(keywords) * 2))
            rows.append(
                {
                    "category": category,
                    "theme": theme,
                    "category_score": round(score, 2),
                    "matched_keywords": sorted(set(keywords)),
                }
            )
    return rows


def to_records(ticker, info, classifications):
    now = datetime.now(timezone.utc).isoformat()
    company_name = clean_text(info.get("longName") or info.get("shortName") or ticker)
    exchange = infer_exchange(info)
    records = []
    for classification in classifications:
        records.append(
            {
                "ticker": ticker,
                "company_name": company_name,
                "exchange": exchange,
                "sector": clean_text(info.get("sector")) or None,
                "industry": clean_text(info.get("industry")) or None,
                "category": classification["category"],
                "theme": classification["theme"],
                "category_score": classification["category_score"],
                "market_cap": info.get("marketCap"),
                "average_volume": info.get("averageVolume") or info.get("averageDailyVolume10Day"),
                "country": clean_text(info.get("country")) or None,
                "website": clean_text(info.get("website")) or None,
                "description": clean_text(info.get("longBusinessSummary"))[:4000] or None,
                "matched_keywords": classification["matched_keywords"],
                "source": "category_universe_builder:yfinance",
                "active": True,
                "last_seen": now,
                "last_updated": now,
            }
        )
    return records


def upsert_records(records, dry_run=False):
    if not records:
        print("No category records to upsert.")
        return
    if dry_run:
        print(f"Dry run: would upsert {len(records)} category_universe rows.")
        for record in records[:30]:
            print(f"   {record['ticker']:<6} {record['category']:<15} {record['theme']:<28} {record['category_score']:>5} {record['company_name']}")
        if len(records) > 30:
            print(f"   ... and {len(records) - 30} more")
        return

    supabase = get_supabase_client()
    for start in range(0, len(records), 100):
        batch = records[start:start + 100]
        supabase.table("category_universe").upsert(batch, on_conflict="ticker,category,theme").execute()
        print(f"Upserted {start + len(batch)}/{len(records)} category rows.")


def build_category_universe(limit=CATEGORY_UNIVERSE_LIMIT, min_score=CATEGORY_MIN_SCORE, dry_run=False):
    print_compute_notice(
        "category_universe_builder",
        f"broad Yahoo category-universe rebuild across up to {limit} ticker candidates",
        prefix="[CATEGORY UNIVERSE]",
    )
    listings = fetch_listed_tickers()
    print(f"Loaded {len(listings)} ticker candidates. Scanning up to {limit}.")
    records = []
    scanned = 0

    for ticker in listings["ticker"].head(limit):
        scanned += 1
        info = fetch_yahoo_info(ticker)
        if not info:
            continue
        if infer_exchange(info) not in {"NASDAQ", "NYSE"}:
            continue

        classifications = [
            item for item in classify_categories(info)
            if item["category_score"] >= min_score
        ]
        if not classifications:
            continue

        ticker_records = to_records(ticker, info, classifications)
        records.extend(ticker_records)
        best = max(ticker_records, key=lambda row: row["category_score"])
        print(f"   {ticker:<6} {best['category']:<15} {best['theme']:<28} score={best['category_score']} {best['company_name']}")

    records.sort(key=lambda row: (row["category_score"], row.get("market_cap") or 0), reverse=True)
    print(f"Scan complete: {len(records)} category rows from {scanned} scanned tickers.")
    upsert_records(records, dry_run=dry_run)
    return records


def parse_args():
    parser = argparse.ArgumentParser(description="Build and sync dynamic category universes from broad Yahoo tickers.")
    parser.add_argument("--limit", type=int, default=CATEGORY_UNIVERSE_LIMIT)
    parser.add_argument("--min-score", type=float, default=CATEGORY_MIN_SCORE)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    build_category_universe(limit=args.limit, min_score=args.min_score, dry_run=args.dry_run)
