import json
import os
from datetime import datetime, timezone


DEFAULT_METRICS_PATH = "data/llm_metrics.jsonl"


def env_bool(name, default=True):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def metrics_enabled():
    return env_bool("LLM_METRICS_ENABLED", True)


def metrics_path():
    return os.getenv("LLM_METRICS_PATH", DEFAULT_METRICS_PATH)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def record_llm_metric(
    source,
    task,
    model,
    duration_seconds,
    success,
    endpoint="",
    item_count=1,
    batch_size=1,
    attempts=1,
    timeout_seconds=None,
    num_predict=None,
    prompt_chars=None,
    response_chars=None,
    error="",
    extra=None,
):
    if not metrics_enabled():
        return

    path = metrics_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    record = {
        "timestamp": utc_now_iso(),
        "source": source,
        "task": task,
        "model": model,
        "success": bool(success),
        "duration_ms": round(float(duration_seconds) * 1000, 2),
        "endpoint": endpoint,
        "item_count": int(item_count or 0),
        "batch_size": int(batch_size or 0),
        "attempts": int(attempts or 0),
        "timeout_seconds": timeout_seconds,
        "num_predict": num_predict,
        "prompt_chars": prompt_chars,
        "response_chars": response_chars,
        "error": str(error or "")[:500],
    }
    if extra:
        record["extra"] = extra

    try:
        with open(path, "a", encoding="utf-8") as metrics_file:
            metrics_file.write(json.dumps(record, ensure_ascii=True) + "\n")
    except Exception:
        pass
