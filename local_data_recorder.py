import json
from datetime import datetime, timezone
from pathlib import Path

from config import LOCAL_DATA_CAPTURE_ENABLED, LOCAL_DATA_CAPTURE_PATH


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def append_local_event(event_type, payload=None, source=None, path=None):
    """Best-effort local training-data recorder. Never let logging stop trading."""
    if not LOCAL_DATA_CAPTURE_ENABLED:
        return False

    record = {
        "logged_at": utc_now_iso(),
        "event_type": event_type,
        "source": source,
        "payload": payload or {},
    }

    try:
        target = Path(path or LOCAL_DATA_CAPTURE_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")
        return True
    except Exception as exc:
        print(f"[LOCAL DATA] write skipped for {event_type}: {exc}")
        return False
