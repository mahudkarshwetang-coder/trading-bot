import importlib.util
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

import requests

from config import (
    DRY_RUN,
    IBKR_HOST,
    IBKR_PORT,
    OLLAMA_URL,
    SUPABASE_KEY,
    SUPABASE_URL,
    get_supabase_client,
)

ROOT = Path(__file__).resolve().parent

REQUIRED_ENV = ["SUPABASE_URL", "SUPABASE_KEY"]
OPTIONAL_ENV = [
    "DRY_RUN",
    "IBKR_HOST",
    "IBKR_PORT",
    "IBKR_CLIENT_ID",
    "MAX_DRAWDOWN_PCT",
    "OLLAMA_URL",
    "OLLAMA_MODEL",
    "SCANNER_INTERVAL_SECONDS",
    "SIGNAL_COOLDOWN_MINUTES",
    "MASSIVE_API_KEY",
]

REQUIRED_PACKAGES = [
    "yfinance",
    "dotenv",
    "supabase",
    "ib_insync",
    "requests",
    "pandas",
    "feedparser",
    "chromadb",
    "sentence_transformers",
    "pytz",
    "urllib3",
]


class HealthReport:
    def __init__(self):
        self.failures = 0
        self.warnings = 0

    def ok(self, label, detail=""):
        self._line("OK", label, detail)

    def warn(self, label, detail=""):
        self.warnings += 1
        self._line("WARN", label, detail)

    def fail(self, label, detail=""):
        self.failures += 1
        self._line("FAIL", label, detail)

    def _line(self, status, label, detail):
        suffix = f" - {detail}" if detail else ""
        print(f"[{status}] {label}{suffix}")

    def exit_code(self):
        return 1 if self.failures else 0


def check_env(report):
    env_path = ROOT / ".env"
    if env_path.exists():
        report.ok(".env file", str(env_path))
    else:
        report.fail(".env file", "missing")

    for name in REQUIRED_ENV:
        if os.getenv(name):
            report.ok(f"env:{name}", "set")
        else:
            report.fail(f"env:{name}", "missing")

    unset_optional = [name for name in OPTIONAL_ENV if os.getenv(name) is None]
    if unset_optional:
        report.warn("optional env", f"using defaults for {', '.join(unset_optional)}")
    else:
        report.ok("optional env", "all configured")

    report.ok("DRY_RUN", "ON" if DRY_RUN else "OFF")
    if not DRY_RUN:
        report.warn("live trading mode", "DRY_RUN is false; main.py can place IBKR orders")


def check_dependencies(report):
    missing = []
    for package in REQUIRED_PACKAGES:
        if importlib.util.find_spec(package) is None:
            missing.append(package)

    if missing:
        report.fail("python dependencies", f"missing: {', '.join(missing)}")
    else:
        report.ok("python dependencies", "all required packages importable")


def check_local_assets(report):
    targets = ROOT / "daily_targets.txt"
    if targets.exists():
        content = targets.read_text(encoding="utf-8", errors="replace").strip()
        count = len([item for item in content.split(",") if item.strip()])
        if count:
            report.ok("daily_targets.txt", f"{count} ticker(s)")
        else:
            report.warn("daily_targets.txt", "file exists but is empty")
    else:
        report.warn("daily_targets.txt", "missing; run fundamental_scanner.py first")

    vault = ROOT / "strategy_vault"
    if vault.exists() and vault.is_dir():
        docs = list(vault.glob("*.txt")) + list(vault.glob("*.md"))
        if docs:
            report.ok("strategy_vault", f"{len(docs)} strategy document(s)")
        else:
            report.warn("strategy_vault", "no .txt or .md strategy documents found")
    else:
        report.warn("strategy_vault", "missing; llm_scanner.py will create it")

    chroma = ROOT / "chroma_db" / "chroma.sqlite3"
    if chroma.exists():
        report.ok("chroma_db", "persistent database found")
    else:
        report.warn("chroma_db", "missing; llm_scanner.py will create/sync it")


def check_supabase(report):
    if not SUPABASE_URL or not SUPABASE_KEY:
        report.fail("Supabase", "credentials missing; skipping connectivity check")
        return

    try:
        supabase = get_supabase_client()
        settings = supabase.table("bot_settings").select("*").eq("id", 1).limit(1).execute()
        report.ok("Supabase bot_settings", f"{len(settings.data or [])} row(s)")

        signals = supabase.table("market_signals").select("id,status,ticker").limit(1).execute()
        report.ok("Supabase market_signals", f"{len(signals.data or [])} sample row(s)")
        
        try:
            events = supabase.table("trade_events").select("id,ticker,event_type").limit(1).execute()
            report.ok("Supabase trade_events", f"{len(events.data or [])} sample row(s)")
        except Exception:
            report.warn("Supabase trade_events", "missing; run supabase/trade_events.sql for outcome analytics")
    except Exception as exc:
        report.fail("Supabase", str(exc))


def ollama_tags_url():
    parsed = urlparse(OLLAMA_URL)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/api/tags"


def check_ollama(report):
    tags_url = ollama_tags_url()
    if not tags_url:
        report.fail("Ollama URL", f"invalid OLLAMA_URL={OLLAMA_URL}")
        return

    try:
        response = requests.get(tags_url, timeout=5)
        if response.status_code == 200:
            models = response.json().get("models", [])
            report.ok("Ollama", f"reachable; {len(models)} model(s) listed")
        else:
            report.warn("Ollama", f"reachable but returned HTTP {response.status_code}")
    except Exception as exc:
        report.warn("Ollama", f"not reachable at {tags_url}: {exc}")


def check_ibkr_port(report):
    try:
        with socket.create_connection((IBKR_HOST, IBKR_PORT), timeout=3):
            report.ok("IBKR socket", f"{IBKR_HOST}:{IBKR_PORT} reachable")
    except Exception as exc:
        report.warn("IBKR socket", f"{IBKR_HOST}:{IBKR_PORT} not reachable: {exc}")


def main():
    report = HealthReport()
    print("Alpha Engine Health Check")
    print("=" * 32)

    check_env(report)
    check_dependencies(report)
    check_local_assets(report)
    check_supabase(report)
    check_ollama(report)
    check_ibkr_port(report)

    print("=" * 32)
    if report.failures:
        print(f"Result: {report.failures} failure(s), {report.warnings} warning(s).")
    else:
        print(f"Result: ready with {report.warnings} warning(s).")

    raise SystemExit(report.exit_code())


if __name__ == "__main__":
    main()
