"""
Checkpoint and push the Trading Bot and SignalCenter repos.

Usage:
    python repo_sync.py push
    python repo_sync.py watch --interval 300
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_REPOS = [
    ("Trading Bot", ROOT),
    ("SignalCenter", ROOT.parent / "SignalCenter"),
]

ALWAYS_SKIP_PATTERNS = [
    ".env",
    ".env.*",
    "*.log",
    "__pycache__/*",
    ".pytest_cache/*",
    ".mypy_cache/*",
    ".ruff_cache/*",
    ".venv/*",
    "venv/*",
    "env/*",
    "data/*",
    "logs/*",
    "chroma_db/*",
    "DerivedData/*",
    "build/*",
    ".build/*",
    "*.xcuserstate",
    "xcuserdata/*",
]

MACRO_STATE_PATH = "strategy_vault/00_daily_macro_state.txt"


@dataclass
class RepoResult:
    name: str
    path: Path
    changed: bool
    committed: bool
    pushed: bool
    skipped: list[str]
    message: str


def run_git(repo: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def normalize_path(path: str) -> str:
    return path.replace("\\", "/").strip()


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def get_branch(repo: Path) -> str:
    result = run_git(repo, ["branch", "--show-current"])
    return result.stdout.strip()


def ahead_behind(repo: Path) -> tuple[int, int]:
    branch = get_branch(repo)
    upstream = f"origin/{branch}"
    run_git(repo, ["fetch", "origin"], check=False)
    result = run_git(repo, ["rev-list", "--left-right", "--count", f"{upstream}...HEAD"], check=False)
    if result.returncode != 0:
        return (0, 0)
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        return (0, 0)
    behind = int(parts[0])
    ahead = int(parts[1])
    return behind, ahead


def parse_status(repo: Path) -> list[str]:
    result = run_git(repo, ["status", "--porcelain=v1", "-z"])
    raw = result.stdout
    if not raw:
        return []

    entries = [entry for entry in raw.split("\0") if entry]
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        status = entry[:2]
        path = normalize_path(entry[3:])
        if status.startswith("R") or status.startswith("C"):
            index += 1
            if index < len(entries):
                path = normalize_path(entries[index])
        paths.append(path)
        index += 1
    return sorted(set(paths))


def is_skipped_by_pattern(path: str) -> bool:
    normalized = normalize_path(path)
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in ALWAYS_SKIP_PATTERNS)


def has_bad_macro_state(repo: Path, path: str) -> bool:
    if normalize_path(path) != MACRO_STATE_PATH:
        return False
    file_path = repo / path
    if not file_path.exists():
        return False
    try:
        return "nan%" in file_path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return True


def safe_paths(repo: Path, paths: list[str]) -> tuple[list[str], list[str]]:
    safe: list[str] = []
    skipped: list[str] = []
    for path in paths:
        if is_skipped_by_pattern(path):
            skipped.append(f"{path} (ignored safety pattern)")
            continue
        if has_bad_macro_state(repo, path):
            skipped.append(f"{path} (contains nan% macro values)")
            continue
        safe.append(path)
    return safe, skipped


def staged_changes(repo: Path) -> bool:
    result = run_git(repo, ["diff", "--cached", "--quiet"], check=False)
    return result.returncode != 0


def run_checks(repo_name: str, repo: Path, no_checks: bool) -> tuple[bool, str]:
    if no_checks:
        return True, "checks skipped"

    if repo_name == "Trading Bot":
        targets = [name for name in ("health_check.py", "ticker_intelligence.py", "repo_sync.py") if (repo / name).exists()]
        if targets:
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", *targets],
                cwd=repo,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            if result.returncode != 0:
                return False, result.stdout.strip()
        return True, "python compile checks passed"

    if repo_name == "SignalCenter":
        if sys.platform.startswith("win"):
            return True, "Swift compile skipped on Windows"
        result = subprocess.run(
            ["xcodebuild", "-project", "SignalCenter.xcodeproj", "-scheme", "SignalCenter", "-destination", "generic/platform=iOS", "build"],
            cwd=repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if result.returncode != 0:
            return False, result.stdout.strip()[-3000:]
        return True, "xcodebuild passed"

    return True, "no checks configured"


def checkpoint_repo(repo_name: str, repo: Path, message: str, no_checks: bool, dry_run: bool) -> RepoResult:
    if not repo.exists() or not is_git_repo(repo):
        return RepoResult(repo_name, repo, False, False, False, [], "not a git repo; skipped")

    changed_paths = parse_status(repo)
    if not changed_paths:
        return RepoResult(repo_name, repo, False, False, False, [], "clean")

    safe, skipped = safe_paths(repo, changed_paths)
    if not safe:
        return RepoResult(repo_name, repo, True, False, False, skipped, "no safe paths to stage")

    behind, _ = ahead_behind(repo)
    if behind > 0:
        return RepoResult(repo_name, repo, True, False, False, skipped, f"remote is {behind} commit(s) ahead; pull first")

    if dry_run:
        return RepoResult(repo_name, repo, True, False, False, skipped, f"dry run safe paths: {', '.join(safe)}")

    run_git(repo, ["add", "--", *safe])
    if not staged_changes(repo):
        return RepoResult(repo_name, repo, True, False, False, skipped, "nothing staged after safety filters")

    checks_ok, checks_message = run_checks(repo_name, repo, no_checks)
    if not checks_ok:
        run_git(repo, ["restore", "--staged", "--", *safe], check=False)
        return RepoResult(repo_name, repo, True, False, False, skipped, f"checks failed: {checks_message}")

    run_git(repo, ["commit", "-m", message])
    branch = get_branch(repo)
    push = run_git(repo, ["push", "origin", branch], check=False)
    if push.returncode != 0:
        return RepoResult(repo_name, repo, True, True, False, skipped, push.stdout.strip())

    return RepoResult(repo_name, repo, True, True, True, skipped, checks_message)


def checkpoint_all(args: argparse.Namespace) -> list[RepoResult]:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    message = args.message or f"Auto sync {timestamp}"
    results: list[RepoResult] = []
    for repo_name, repo in DEFAULT_REPOS:
        results.append(checkpoint_repo(repo_name, repo, message, args.no_checks, args.dry_run))
    return results


def print_results(results: list[RepoResult]) -> None:
    for result in results:
        status = "pushed" if result.pushed else "committed" if result.committed else "skipped"
        print(f"[{result.name}] {status}: {result.message}")
        for skipped in result.skipped:
            print(f"  skipped: {skipped}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Checkpoint and push Trading Bot plus SignalCenter.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    push = subparsers.add_parser("push", help="Commit and push safe changes once.")
    push.add_argument("-m", "--message", help="Commit message. Defaults to Auto sync timestamp.")
    push.add_argument("--dry-run", action="store_true", help="Show what would be staged without committing.")
    push.add_argument("--no-checks", action="store_true", help="Skip lightweight validation checks.")

    watch = subparsers.add_parser("watch", help="Run push checkpoints repeatedly.")
    watch.add_argument("--interval", type=int, default=300, help="Seconds between checkpoints.")
    watch.add_argument("-m", "--message", help="Commit message. Defaults to Auto sync timestamp.")
    watch.add_argument("--dry-run", action="store_true", help="Show what would be staged without committing.")
    watch.add_argument("--no-checks", action="store_true", help="Skip lightweight validation checks.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "push":
        print_results(checkpoint_all(args))
        return 0

    if args.command == "watch":
        interval = max(60, int(args.interval))
        print(f"Repo sync watch online. Interval: {interval}s. Press Ctrl+C to stop.")
        try:
            while True:
                print_results(checkpoint_all(args))
                time.sleep(interval)
        except KeyboardInterrupt:
            print("Repo sync watch stopped.")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
