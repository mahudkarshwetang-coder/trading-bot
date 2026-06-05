import importlib.util
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

import requests

from config import (
    DRY_RUN,
    ENTRY_ORDER_TYPE,
    FIXED_ORDER_QUANTITY,
    IBKR_HOST,
    IBKR_NEWS_CLIENT_ID,
    IBKR_PORT,
    OLLAMA_MODEL,
    OLLAMA_URL,
    PERFORMANCE_GAMING_CPU_BUDGET_PCT,
    PERFORMANCE_GAMING_EXTERNAL_PRIORITY,
    PERFORMANCE_GAMING_PROCESS_PRIORITY,
    SUPABASE_KEY,
    SUPABASE_URL,
    get_supabase_client,
)

ROOT = Path(__file__).resolve().parent

REQUIRED_ENV = ["SUPABASE_URL", "SUPABASE_KEY"]
OPTIONAL_ENV = [
    "DRY_RUN",
    "FIXED_ORDER_QUANTITY",
    "ENTRY_ORDER_TYPE",
    "BRACKET_STOP_LOSS_PCT",
    "BRACKET_TAKE_PROFIT_PCT",
    "BUY_REENTRY_DIP_PCT",
    "IBKR_HOST",
    "IBKR_PORT",
    "IBKR_CLIENT_ID",
    "IBKR_SYNC_CLIENT_ID",
    "IBKR_CONTEXT_CLIENT_ID",
    "IBKR_NEWS_CLIENT_ID",
    "MAX_DRAWDOWN_PCT",
    "MARKET_TIMEZONE",
    "PREMARKET_OPEN",
    "REGULAR_MARKET_OPEN",
    "REGULAR_MARKET_CLOSE",
    "AFTER_HOURS_CLOSE",
    "SCAN_EXTENDED_HOURS",
    "ALLOW_EXTENDED_HOURS_TRADING",
    "STOP_OUTSIDE_RTH",
    "GLOBAL_OVERNIGHT_OPEN",
    "GLOBAL_OVERNIGHT_CLOSE",
    "SCAN_GLOBAL_OVERNIGHT",
    "SCAN_SUNDAY_NIGHT",
    "ALLOW_GLOBAL_OVERNIGHT_TRADING",
    "OLLAMA_URL",
    "OLLAMA_MODEL",
    "PERFORMANCE_GOVERNOR_ENABLED",
    "PERFORMANCE_MODE",
    "PERFORMANCE_GAME_PROCESS_NAMES",
    "PERFORMANCE_BUSY_CPU_PCT",
    "PERFORMANCE_LOW_MEMORY_GB",
    "PERFORMANCE_PROFILE_TTL_SECONDS",
    "PERFORMANCE_GAMING_KEEP_ALIVE",
    "PERFORMANCE_GAMING_TIMEOUT_SECONDS",
    "PERFORMANCE_GAMING_MAX_NUM_PREDICT",
    "PERFORMANCE_GAMING_MAX_BATCH_SIZE",
    "PERFORMANCE_GAMING_MAX_LLM_ITEMS",
    "PERFORMANCE_GAMING_MAX_WORKERS",
    "PERFORMANCE_GAMING_MAX_LLM_TICKERS",
    "PERFORMANCE_GAMING_INTERVAL_MULTIPLIER",
    "PERFORMANCE_GAMING_CPU_BUDGET_PCT",
    "PERFORMANCE_GAMING_THROTTLE_ENABLED",
    "PERFORMANCE_GAMING_THROTTLE_MIN_SLEEP_SECONDS",
    "PERFORMANCE_GAMING_THROTTLE_MAX_SLEEP_SECONDS",
    "PERFORMANCE_GAMING_PROCESS_PRIORITY",
    "PERFORMANCE_GAMING_EXTERNAL_PRIORITY",
    "PERFORMANCE_GAMING_EXTERNAL_PROCESSES",
    "PERFORMANCE_RESTORE_PRIORITY_ON_NORMAL",
    "PERFORMANCE_DEFER_TASKS",
    "LLM_METRICS_ENABLED",
    "LLM_METRICS_PATH",
    "LLM_SCANNER_TIMEOUT_SECONDS",
    "LLM_SCANNER_RETRY_ATTEMPTS",
    "LLM_SCANNER_RETRY_BACKOFF_SECONDS",
    "LLM_SCANNER_NUM_PREDICT",
    "LLM_SCANNER_KEEP_ALIVE",
    "LLM_SCANNER_RULES_MAX_CHARS",
    "LLM_SCANNER_MAX_TICKERS",
    "LLM_SCANNER_MAX_CONSECUTIVE_FAILURES",
    "SIGNAL_QUALITY_FILTER_ENABLED",
    "SIGNAL_QUALITY_MIN_SCORE",
    "SIGNAL_QUALITY_TIMEOUT_SECONDS",
    "SIGNAL_QUALITY_FAIL_OPEN",
    "SIGNAL_QUALITY_NUM_PREDICT",
    "SIGNAL_QUALITY_RETRY_ATTEMPTS",
    "SIGNAL_QUALITY_RETRY_BACKOFF_SECONDS",
    "SIGNAL_QUALITY_KEEP_ALIVE",
    "EXECUTION_GATE_ENABLED",
    "EXECUTION_GATE_MIN_SCORE",
    "EXECUTION_GATE_FAIL_OPEN",
    "EXECUTION_GATE_TIMEOUT_SECONDS",
    "EXECUTION_GATE_NUM_PREDICT",
    "EXECUTION_GATE_RETRY_ATTEMPTS",
    "EXECUTION_GATE_RETRY_BACKOFF_SECONDS",
    "EXECUTION_GATE_KEEP_ALIVE",
    "SCANNER_INTERVAL_SECONDS",
    "SIGNAL_COOLDOWN_MINUTES",
    "OPEN_SCANNER_ENABLED",
    "OPEN_SCANNER_START",
    "OPEN_SCANNER_END",
    "OPEN_SCANNER_MINUTES_AFTER_OPEN",
    "OPEN_SCANNER_MAX_TICKERS",
    "OPEN_SCANNER_MIN_PRICE",
    "OPEN_SCANNER_MIN_RVOL",
    "OPEN_SCANNER_MIN_MOVE_PCT",
    "OPEN_SCANNER_MIN_RECOVERY_PCT",
    "OPEN_SCANNER_MIN_CONFIDENCE",
    "OPEN_SCANNER_COOLDOWN_MINUTES",
    "SYNC_BROKER_AFTER_ORDER",
    "SYNC_BROKER_AFTER_ORDER_DELAY_SECONDS",
    "CATEGORY_UNIVERSE_LIMIT",
    "CATEGORY_MIN_SCORE",
    "CATEGORY_TARGET_LIMIT",
    "CATEGORY_TARGETS_PER_CATEGORY",
    "TECH_NEWS_TICKERS",
    "TECH_NEWS_INTERVAL_SECONDS",
    "TECH_NEWS_MAX_WORKERS",
    "TECH_NEWS_LIMIT_PER_TICKER",
    "TECH_NEWS_OUTPUT_PATH",
    "TECH_NEWS_RELEVANCE_FILTER_ENABLED",
    "TECH_NEWS_RELEVANCE_MIN_SCORE",
    "TECH_NEWS_LLM_ENABLED",
    "TECH_NEWS_LLM_MAX_ITEMS",
    "TECH_NEWS_LLM_TIMEOUT_SECONDS",
    "TECH_NEWS_LLM_BATCH_SIZE",
    "TECH_NEWS_LLM_NUM_PREDICT",
    "TECH_NEWS_LLM_NUM_PREDICT_PER_ITEM",
    "TECH_NEWS_LLM_SINGLE_TIMEOUT_SECONDS",
    "TECH_NEWS_LLM_SINGLE_NUM_PREDICT",
    "TECH_NEWS_LLM_DEFAULT_RETRY_LIMIT",
    "TECH_NEWS_LLM_RETRY_ATTEMPTS",
    "TECH_NEWS_LLM_RETRY_BACKOFF_SECONDS",
    "TECH_NEWS_LLM_KEEP_ALIVE",
    "TECH_NEWS_LLM_WARMUP",
    "IBKR_NEWS_ENABLED",
    "IBKR_NEWS_PROVIDERS",
    "IBKR_NEWS_LOOKBACK_MINUTES",
    "IBKR_NEWS_INTERVAL_SECONDS",
    "IBKR_NEWS_MAX_TICKERS",
    "IBKR_NEWS_RESULTS_PER_TICKER",
    "IBKR_NEWS_MIN_CONFIDENCE",
    "IBKR_NEWS_MIN_DIRECTIONAL_SCORE",
    "IBKR_NEWS_COOLDOWN_MINUTES",
    "IBKR_NEWS_OUTPUT_PATH",
    "IBKR_NEWS_SEEN_PATH",
    "IBKR_NEWS_PUSH_SIGNALS",
    "MARKET_BRIEFING_LOOKBACK_HOURS",
    "MARKET_BRIEFING_NEWS_LIMIT",
    "MARKET_BRIEFING_SIGNAL_LIMIT",
    "MARKET_BRIEFING_POSITION_LIMIT",
    "MARKET_BRIEFING_CATEGORY_LIMIT",
    "MARKET_BRIEFING_TIMEOUT_SECONDS",
    "MARKET_BRIEFING_OUTPUT_PATH",
    "MARKET_BRIEFING_PUSH_SUPABASE",
    "POST_TRADE_REVIEW_LOOKBACK_DAYS",
    "POST_TRADE_REVIEW_LIMIT",
    "POST_TRADE_REVIEW_HORIZON",
    "POST_TRADE_REVIEW_TIMEOUT_SECONDS",
    "POST_TRADE_REVIEW_OUTPUT_PATH",
    "POST_TRADE_REVIEW_PUSH_SUPABASE",
    "STRATEGY_OPTIMIZER_LOOKBACK_DAYS",
    "STRATEGY_OPTIMIZER_HORIZON",
    "STRATEGY_OPTIMIZER_MIN_EVALUATED_SIGNALS",
    "STRATEGY_OPTIMIZER_MIN_CHANNEL_SIGNALS",
    "STRATEGY_OPTIMIZER_MIN_CONFIDENCE_FLOOR",
    "STRATEGY_OPTIMIZER_MIN_CONFIDENCE_CEILING",
    "STRATEGY_OPTIMIZER_MAX_STEP",
    "STRATEGY_OPTIMIZER_OUTPUT_PATH",
    "STRATEGY_OPTIMIZER_PUSH_SUPABASE",
    "STRATEGY_OPTIMIZER_AUTO_APPLY",
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
    report.ok("FIXED_ORDER_QUANTITY", str(FIXED_ORDER_QUANTITY))
    report.ok("ENTRY_ORDER_TYPE", ENTRY_ORDER_TYPE)
    if not DRY_RUN:
        report.warn("live trading mode", "DRY_RUN is false; main.py can place IBKR orders")


def check_env_file_consistency(report):
    env_path = ROOT / ".env"
    if not env_path.exists():
        return

    key_counts = {}
    try:
        with env_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key = line.split("=", 1)[0].strip()
                if not key:
                    continue
                key_counts[key] = key_counts.get(key, 0) + 1
    except Exception:
        return

    duplicates = sorted(key for key, count in key_counts.items() if count > 1)
    if duplicates:
        sample = ", ".join(duplicates[:8])
        if len(duplicates) > 8:
            sample = f"{sample}, +{len(duplicates) - 8} more"
        report.warn(".env duplicates", f"duplicate keys found (last value wins): {sample}")


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
        
        try:
            context = supabase.table("signal_context").select("id,ticker,context_score").limit(1).execute()
            report.ok("Supabase signal_context", f"{len(context.data or [])} sample row(s)")
        except Exception:
            report.warn("Supabase signal_context", "missing; run supabase/signal_context.sql for execution-terminal context")

        try:
            category_universe = supabase.table("category_universe").select("ticker,category").limit(1).execute()
            report.ok("Supabase category_universe", f"{len(category_universe.data or [])} sample row(s)")
        except Exception:
            report.warn("Supabase category_universe", "missing; run supabase/category_universe.sql for dynamic category targets")

        try:
            briefings = supabase.table("daily_market_briefings").select("briefing_date,session_type").limit(1).execute()
            report.ok("Supabase daily_market_briefings", f"{len(briefings.data or [])} sample row(s)")
        except Exception:
            report.warn("Supabase daily_market_briefings", "missing; run supabase/daily_market_briefings.sql for iPad briefings")

        try:
            post_trade = supabase.table("post_trade_reviews").select("signal_id,ticker").limit(1).execute()
            report.ok("Supabase post_trade_reviews", f"{len(post_trade.data or [])} sample row(s)")
        except Exception:
            report.warn("Supabase post_trade_reviews", "missing; run supabase/post_trade_reviews.sql for Qwen trade coaching")

        try:
            optimizer_runs = supabase.table("strategy_optimizer_runs").select("run_at").limit(1).execute()
            report.ok("Supabase strategy_optimizer_runs", f"{len(optimizer_runs.data or [])} sample row(s)")
        except Exception:
            report.warn("Supabase strategy_optimizer_runs", "missing; run supabase/strategy_optimizer_runs.sql for optimizer history")
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
            configured = str(OLLAMA_MODEL or "").strip().lower()
            installed_names = {
                str(model.get("name") or "").strip().lower()
                for model in models
                if isinstance(model, dict)
            }
            if configured:
                has_exact = configured in installed_names
                base_name = configured.split(":", 1)[0]
                has_family = any(name.split(":", 1)[0] == base_name for name in installed_names)
                if has_exact or has_family:
                    report.ok("Ollama model", f"configured model available: {OLLAMA_MODEL}")
                else:
                    report.warn(
                        "Ollama model",
                        f"configured model not found: {OLLAMA_MODEL} (installed: {', '.join(sorted(installed_names)) or 'none'})",
                    )
            endpoint_path = urlparse(OLLAMA_URL).path.rstrip("/").lower()
            if configured.startswith("qwen3") and endpoint_path.endswith("/api/generate"):
                report.warn(
                    "Ollama endpoint",
                    "qwen3 models are usually more stable on /api/chat; consider OLLAMA_URL=http://localhost:11434/api/chat",
                )
        else:
            report.warn("Ollama", f"reachable but returned HTTP {response.status_code}")
    except Exception as exc:
        report.warn("Ollama", f"not reachable at {tags_url}: {exc}")


def check_performance_governor(report):
    try:
        from performance_governor import current_performance_profile, describe_performance_profile

        profile = current_performance_profile(force_refresh=True)
        label = "active" if profile.active else "normal"
        details = describe_performance_profile(profile)
        if profile.active:
            details += (
                f"; budget {PERFORMANCE_GAMING_CPU_BUDGET_PCT:g}%"
                f"; priority python={PERFORMANCE_GAMING_PROCESS_PRIORITY}"
                f"/ollama={PERFORMANCE_GAMING_EXTERNAL_PRIORITY}"
            )
        if profile.cpu_pct is not None:
            details += f"; CPU {profile.cpu_pct:.0f}%"
        if profile.free_memory_gb is not None:
            details += f"; free RAM {profile.free_memory_gb:.1f}GB"
        report.ok("performance governor", f"{label}: {details}")
    except Exception as exc:
        report.warn("performance governor", f"status unavailable: {exc}")


def check_ibkr_port(report):
    try:
        with socket.create_connection((IBKR_HOST, IBKR_PORT), timeout=3):
            report.ok("IBKR socket", f"{IBKR_HOST}:{IBKR_PORT} reachable")
    except Exception as exc:
        report.warn("IBKR socket", f"{IBKR_HOST}:{IBKR_PORT} not reachable: {exc}")


def check_ibkr_news_providers(report):
    try:
        from ib_insync import IB

        ib = IB()
        try:
            ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_NEWS_CLIENT_ID + 100, timeout=5, readonly=True)
            providers = ib.reqNewsProviders()
            if providers:
                codes = ", ".join(f"{provider.code}:{provider.name}" for provider in providers)
                report.ok("IBKR news providers", codes)
            else:
                report.warn("IBKR news providers", "none returned by TWS/API")
        finally:
            if ib.isConnected():
                ib.disconnect()
    except Exception as exc:
        report.warn("IBKR news providers", f"not available: {exc}")


def main():
    report = HealthReport()
    print("Alpha Engine Health Check")
    print("=" * 32)

    check_env(report)
    check_env_file_consistency(report)
    check_dependencies(report)
    check_local_assets(report)
    check_supabase(report)
    check_ollama(report)
    check_performance_governor(report)
    check_ibkr_port(report)
    check_ibkr_news_providers(report)

    print("=" * 32)
    if report.failures:
        print(f"Result: {report.failures} failure(s), {report.warnings} warning(s).")
    else:
        print(f"Result: ready with {report.warnings} warning(s).")

    raise SystemExit(report.exit_code())


if __name__ == "__main__":
    main()
