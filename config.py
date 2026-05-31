import os

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_float(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT = env_int("IBKR_PORT", 7497)
IBKR_CLIENT_ID = env_int("IBKR_CLIENT_ID", 10)
IBKR_SYNC_CLIENT_ID = env_int("IBKR_SYNC_CLIENT_ID", IBKR_CLIENT_ID + 1)
IBKR_CONTEXT_CLIENT_ID = env_int("IBKR_CONTEXT_CLIENT_ID", IBKR_CLIENT_ID + 2)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")

DRY_RUN = env_bool("DRY_RUN", True)
MAX_DRAWDOWN_PCT = env_float("MAX_DRAWDOWN_PCT", 2.0)
SIGNAL_COOLDOWN_MINUTES = env_int("SIGNAL_COOLDOWN_MINUTES", 240)
SIGNAL_JOURNAL_PATH = os.getenv("SIGNAL_JOURNAL_PATH", "data/signal_journal.csv")
SCANNER_INTERVAL_SECONDS = env_int("SCANNER_INTERVAL_SECONDS", 600)
BROKER_SYNC_INTERVAL_SECONDS = env_int("BROKER_SYNC_INTERVAL_SECONDS", 30)
BROKER_SYNC_MARK_MISSING_SIGNALS = env_bool("BROKER_SYNC_MARK_MISSING_SIGNALS", False)
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY")
MASSIVE_PREV_CLOSE_ENABLED = env_bool("MASSIVE_PREV_CLOSE_ENABLED", True)
SIGNAL_CONTEXT_LIMIT = env_int("SIGNAL_CONTEXT_LIMIT", 50)
ENERGY_TARGET_LIMIT = env_int("ENERGY_TARGET_LIMIT", 50)
ENERGY_MIN_SCORE = env_float("ENERGY_MIN_SCORE", 70.0)


def get_supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY in .env.")
    return create_client(SUPABASE_URL, SUPABASE_KEY)
