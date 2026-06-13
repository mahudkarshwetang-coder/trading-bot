import argparse
import json
from datetime import datetime, timezone

from config import get_supabase_client

_disabled_reason = None


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def publish_system_status(
    component,
    status,
    detail=None,
    mode=None,
    market_session=None,
    run_id=None,
    started_at=None,
    finished_at=None,
    error=None,
    metadata=None,
    quiet=True,
):
    """Best-effort heartbeat publisher. Never let status telemetry stop trading work."""
    global _disabled_reason
    if _disabled_reason:
        return False

    now = utc_now_iso()
    payload = {
        "component": component,
        "status": status,
        "detail": detail,
        "mode": mode,
        "market_session": market_session,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "heartbeat_at": now,
        "error": error,
        "metadata": metadata or {},
    }

    if status in {"running", "sleeping", "deferred"} and not started_at:
        payload["started_at"] = now
    if status in {"success", "error", "skipped"} and not finished_at:
        payload["finished_at"] = now

    try:
        supabase = get_supabase_client()
        supabase.table("system_status").upsert(payload, on_conflict="component").execute()
        return True
    except Exception as exc:
        _disabled_reason = str(exc)
        if not quiet:
            print(f"System status sync disabled: {exc}")
        return False


def fetch_system_status(limit=25):
    supabase = get_supabase_client()
    response = (
        supabase.table("system_status")
        .select("*")
        .order("heartbeat_at", desc=True)
        .limit(limit)
        .execute()
    )
    return response.data or []


def parse_args():
    parser = argparse.ArgumentParser(description="Publish or inspect bot system status heartbeats.")
    parser.add_argument("--component", default="manual", help="Component name to update.")
    parser.add_argument("--status", default="success", help="Status value to publish.")
    parser.add_argument("--detail", default=None, help="Human-readable status detail.")
    parser.add_argument("--metadata", default=None, help="Optional JSON metadata object.")
    parser.add_argument("--list", action="store_true", help="List recent status rows instead of publishing.")
    parser.add_argument("--limit", type=int, default=25)
    return parser.parse_args()


def main():
    args = parse_args()
    if args.list:
        rows = fetch_system_status(limit=args.limit)
        for row in rows:
            print(
                f"{row.get('heartbeat_at')}  {row.get('component'):<28} "
                f"{row.get('status'):<9} {row.get('detail') or ''}"
            )
        return

    metadata = {}
    if args.metadata:
        metadata = json.loads(args.metadata)
        if not isinstance(metadata, dict):
            raise ValueError("--metadata must be a JSON object")

    ok = publish_system_status(
        component=args.component,
        status=args.status,
        detail=args.detail,
        metadata=metadata,
        quiet=False,
    )
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
