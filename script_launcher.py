import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import (
    SCRIPT_LAUNCHER_ENABLED,
    SCRIPT_LAUNCHER_INTERVAL_SECONDS,
    SCRIPT_LAUNCHER_TABLE,
    get_supabase_client,
)
from local_data_recorder import append_local_event
from system_status import publish_system_status


ROOT = Path(__file__).resolve().parent
SOURCE = "script_launcher"


@dataclass(frozen=True)
class LaunchCommand:
    key: str
    title: str
    command: tuple[str, ...]
    long_running: bool = False


LAUNCH_COMMANDS = {
    "execution-bridge": LaunchCommand(
        key="execution-bridge",
        title="Execution Bridge",
        command=("python", "main.py"),
        long_running=True,
    ),
    "master-engine": LaunchCommand(
        key="master-engine",
        title="Master Scanner Engine",
        command=("python", "master_scanner.py"),
        long_running=True,
    ),
    "training-engine": LaunchCommand(
        key="training-engine",
        title="Training Engine",
        command=("python", "master_scanner.py", "training-engine"),
        long_running=True,
    ),
    "experimental-engine": LaunchCommand(
        key="experimental-engine",
        title="Experimental Engine",
        command=("python", "master_scanner.py", "experimental-engine"),
        long_running=True,
    ),
    "pure-training-engine": LaunchCommand(
        key="pure-training-engine",
        title="Pure Training Engine",
        command=("python", "master_scanner.py", "pure-training-engine"),
        long_running=True,
    ),
    "daily-cycle": LaunchCommand(
        key="daily-cycle",
        title="Master Daily Cycle",
        command=("python", "master_scanner.py", "daily-cycle"),
    ),
    "radar-scan": LaunchCommand(
        key="radar-scan",
        title="Radar Scan",
        command=("python", "radar.py"),
    ),
    "earnings-radar": LaunchCommand(
        key="earnings-radar",
        title="Earnings Radar",
        command=("python", "earnings_radar.py"),
    ),
    "llm-scanner": LaunchCommand(
        key="llm-scanner",
        title="LLM Scanner",
        command=("python", "llm_scanner.py"),
    ),
    "context-enrichment": LaunchCommand(
        key="context-enrichment",
        title="Context Enrichment",
        command=("python", "context_enrichment.py"),
    ),
    "category-refresh": LaunchCommand(
        key="category-refresh",
        title="Category Universe Refresh",
        command=("python", "master_scanner.py", "daily-category-refresh"),
    ),
    "pure-training-cycle": LaunchCommand(
        key="pure-training-cycle",
        title="Pure Training Basket",
        command=("python", "master_scanner.py", "pure-training-cycle"),
    ),
    "experimental-cycle": LaunchCommand(
        key="experimental-cycle",
        title="Experimental Build + Findings",
        command=("python", "master_scanner.py", "experimental-cycle"),
    ),
    "experimental-build": LaunchCommand(
        key="experimental-build",
        title="Experimental Build",
        command=("python", "master_scanner.py", "experimental-build"),
    ),
    "experimental-findings": LaunchCommand(
        key="experimental-findings",
        title="Experimental Findings",
        command=("python", "master_scanner.py", "experimental-findings"),
    ),
    "experimental-execute": LaunchCommand(
        key="experimental-execute",
        title="Experimental Execute",
        command=("python", "master_scanner.py", "experimental-execute"),
    ),
    "pure-training-review": LaunchCommand(
        key="pure-training-review",
        title="Pure Training Review",
        command=("python", "master_scanner.py", "pure-training-review"),
    ),
    "experimental-review": LaunchCommand(
        key="experimental-review",
        title="Experimental Review",
        command=("python", "master_scanner.py", "experimental-review"),
    ),
    "broker-sync-loop": LaunchCommand(
        key="broker-sync-loop",
        title="Broker Sync Loop",
        command=("python", "broker_sync.py", "--loop", "--interval", "300"),
        long_running=True,
    ),
    "tech-news-monitor": LaunchCommand(
        key="tech-news-monitor",
        title="Tech News Monitor",
        command=("python", "tech_news_monitor.py"),
        long_running=True,
    ),
    "health-check": LaunchCommand(
        key="health-check",
        title="Health Check",
        command=("python", "health_check.py"),
    ),
}


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def ps_single_quote(value):
    return str(value).replace("'", "''")


def command_to_powershell(command):
    return " ".join(f"& '{ps_single_quote(part)}'" if index == 0 else f"'{ps_single_quote(part)}'" for index, part in enumerate(command))


def command_match_parts(command):
    parts = [str(part) for part in command if str(part).strip()]
    if parts and parts[0].lower() in {"python", "python.exe", "py", "py.exe"}:
        parts = parts[1:]
    return parts


def active_pids_for_command(command):
    parts = command_match_parts(command)
    if not parts:
        return []

    patterns = "@(" + ",".join(f"'{ps_single_quote(part)}'" for part in parts) + ")"
    script = (
        f"$patterns = {patterns}; "
        "$self = $PID; "
        "Get-CimInstance Win32_Process -Filter \"name = 'python.exe' OR name = 'pythonw.exe'\" "
        "| Where-Object { "
        "$line = [string]$_.CommandLine; "
        "$ok = $true; "
        "foreach ($pattern in $patterns) { if ($line -notlike \"*$pattern*\") { $ok = $false } }; "
        "$ok -and [int]$_.ProcessId -ne [int]$self "
        "} | ForEach-Object { $_.ProcessId }"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception as exc:
        print(f"[SCRIPT LAUNCHER] Duplicate check skipped for {' '.join(command)}: {exc}")
        return []

    pids = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return sorted(set(pids))


def launch_script_key(script_key, requested_by="local", request_id=None):
    command = LAUNCH_COMMANDS.get(script_key)
    if command is None:
        raise ValueError(f"Unknown script key: {script_key}")

    existing_pids = active_pids_for_command(command.command)
    if existing_pids:
        pid = existing_pids[0]
        payload = {
            "request_id": request_id,
            "script_key": script_key,
            "title": command.title,
            "command": list(command.command),
            "pid": pid,
            "existing_pids": existing_pids,
            "requested_by": requested_by,
            "checked_at": utc_now_iso(),
        }
        append_local_event("script_launch_duplicate_blocked", payload, source=SOURCE)
        publish_system_status(
            "script_launcher",
            "success",
            detail=f"{command.title} is already running; duplicate launch blocked.",
            metadata=payload,
        )
        print(
            f"[SCRIPT LAUNCHER] {command.title} already running "
            f"(pid(s): {', '.join(str(item) for item in existing_pids)}). Duplicate launch blocked."
        )
        return pid

    window_title = f"Alpha Engine - {command.title}"
    script = (
        f"$Host.UI.RawUI.WindowTitle = '{ps_single_quote(window_title)}'; "
        f"Set-Location -LiteralPath '{ps_single_quote(ROOT)}'; "
        f"Write-Host '[SCRIPT LAUNCHER] {ps_single_quote(command.title)}'; "
        f"{command_to_powershell(command.command)}"
    )
    proc = subprocess.Popen(
        ["powershell.exe", "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=str(ROOT),
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )
    payload = {
        "request_id": request_id,
        "script_key": script_key,
        "title": command.title,
        "command": list(command.command),
        "pid": proc.pid,
        "requested_by": requested_by,
        "launched_at": utc_now_iso(),
    }
    append_local_event("script_window_launched", payload, source=SOURCE)
    publish_system_status(
        "script_launcher",
        "success",
        detail=f"Launched {command.title} in a PowerShell window.",
        metadata=payload,
    )
    print(f"[SCRIPT LAUNCHER] Launched {command.title} ({script_key}) in PowerShell window, pid={proc.pid}.")
    return proc.pid


def update_request(supabase, request_id, payload):
    supabase.table(SCRIPT_LAUNCHER_TABLE).update(payload).eq("id", request_id).execute()


def fetch_pending_requests(supabase, limit=5):
    response = (
        supabase.table(SCRIPT_LAUNCHER_TABLE)
        .select("*")
        .eq("status", "pending")
        .order("requested_at", desc=False)
        .limit(limit)
        .execute()
    )
    return response.data or []


def process_request(supabase, row):
    request_id = row.get("id")
    script_key = row.get("script_key")
    requested_by = row.get("requested_by") or "signalcenter"
    try:
        update_request(
            supabase,
            request_id,
            {
                "status": "launching",
                "launcher_host": "windows",
                "updated_at": utc_now_iso(),
            },
        )
        pid = launch_script_key(script_key, requested_by=requested_by, request_id=request_id)
        update_request(
            supabase,
            request_id,
            {
                "status": "launched",
                "pid": pid,
                "launched_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
                "error": None,
            },
        )
        return True
    except Exception as exc:
        error = str(exc)
        print(f"[SCRIPT LAUNCHER] Request failed: {script_key}: {error}")
        try:
            update_request(
                supabase,
                request_id,
                {
                    "status": "error",
                    "error": error,
                    "updated_at": utc_now_iso(),
                },
            )
        except Exception:
            pass
        publish_system_status(
            "script_launcher",
            "error",
            detail=f"Script launch failed for {script_key}.",
            error=error,
            metadata={"request_id": request_id, "script_key": script_key},
        )
        return False


def run_launcher(once=False, interval_seconds=SCRIPT_LAUNCHER_INTERVAL_SECONDS):
    if not SCRIPT_LAUNCHER_ENABLED:
        print("[SCRIPT LAUNCHER] Disabled by SCRIPT_LAUNCHER_ENABLED=false.")
        return False

    supabase = get_supabase_client()
    print("[SCRIPT LAUNCHER] Online. Watching Supabase for iPad script launch requests.")
    publish_system_status(
        "script_launcher",
        "running",
        detail="Watching for script launch requests.",
        metadata={"table": SCRIPT_LAUNCHER_TABLE, "interval_seconds": interval_seconds},
    )
    while True:
        try:
            publish_system_status(
                "script_launcher",
                "running",
                detail="Watching for script launch requests.",
                metadata={"table": SCRIPT_LAUNCHER_TABLE, "interval_seconds": interval_seconds},
            )
            rows = fetch_pending_requests(supabase)
            for row in rows:
                process_request(supabase, row)
        except Exception as exc:
            print(f"[SCRIPT LAUNCHER] Poll failed: {exc}")
            publish_system_status("script_launcher", "error", detail="Poll failed.", error=str(exc))

        if once:
            return True
        time.sleep(max(1, int(interval_seconds)))


def parse_args():
    parser = argparse.ArgumentParser(description="Launch approved Trading Bot scripts in their own PowerShell windows.")
    parser.add_argument("--once", action="store_true", help="Process pending requests once, then exit.")
    parser.add_argument("--interval", type=int, default=SCRIPT_LAUNCHER_INTERVAL_SECONDS, help="Polling interval in seconds.")
    parser.add_argument("--launch", choices=sorted(LAUNCH_COMMANDS), help="Launch one allowed script immediately.")
    parser.add_argument("--list", action="store_true", help="List allowed script keys.")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.list:
        for command in LAUNCH_COMMANDS.values():
            kind = "long-running" if command.long_running else "one-shot"
            print(f"{command.key:<22} {kind:<13} {' '.join(command.command)}")
        return

    if args.launch:
        launch_script_key(args.launch, requested_by="local-cli")
        return

    ok = run_launcher(once=args.once, interval_seconds=args.interval)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
