import argparse
import json
from collections import defaultdict
from pathlib import Path

from config import LLM_METRICS_PATH


def percentile(values, pct):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * pct))
    return ordered[index]


def load_records(path):
    records = []
    metrics_path = Path(path)
    if not metrics_path.exists():
        return records

    with metrics_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    return records


def summarize(records):
    groups = defaultdict(list)
    for record in records:
        key = (
            record.get("source") or "unknown",
            record.get("task") or "unknown",
            record.get("model") or "unknown",
        )
        groups[key].append(record)
    return groups


def print_summary(records, limit):
    if not records:
        print("No LLM metrics found yet.")
        return

    rows = []
    for (source, task, model), group in summarize(records).items():
        durations = [float(item.get("duration_ms") or 0.0) for item in group]
        successes = [item for item in group if item.get("success")]
        failures = len(group) - len(successes)
        attempts = [int(item.get("attempts") or 0) for item in group]
        rows.append(
            {
                "source": source,
                "task": task,
                "model": model,
                "count": len(group),
                "success_rate": round((len(successes) / len(group)) * 100, 1),
                "failures": failures,
                "avg_ms": round(sum(durations) / len(durations), 1),
                "p50_ms": round(percentile(durations, 0.50), 1),
                "p95_ms": round(percentile(durations, 0.95), 1),
                "avg_attempts": round(sum(attempts) / len(attempts), 2),
            }
        )

    rows.sort(key=lambda row: (row["failures"], row["p95_ms"]), reverse=True)
    if limit:
        rows = rows[:limit]

    print("LLM Metrics Summary")
    print("=" * 92)
    print(
        f"{'source':<24} {'task':<18} {'count':>5} {'ok%':>6} "
        f"{'fail':>5} {'avg':>8} {'p50':>8} {'p95':>8} {'att':>5}"
    )
    print("-" * 92)
    for row in rows:
        print(
            f"{row['source']:<24} {row['task']:<18} {row['count']:>5} "
            f"{row['success_rate']:>5.1f}% {row['failures']:>5} "
            f"{row['avg_ms']:>7.1f} {row['p50_ms']:>8.1f} "
            f"{row['p95_ms']:>8.1f} {row['avg_attempts']:>5.2f}"
        )


def parse_args():
    parser = argparse.ArgumentParser(description="Summarize local Ollama/Qwen performance metrics.")
    parser.add_argument("--path", default=LLM_METRICS_PATH)
    parser.add_argument("--last", type=int, default=0, help="Only summarize the last N metric rows.")
    parser.add_argument("--limit", type=int, default=0, help="Only print the top N grouped rows.")
    return parser.parse_args()


def main():
    args = parse_args()
    records = load_records(args.path)
    if args.last and args.last > 0:
        records = records[-args.last:]
    print_summary(records, args.limit)


if __name__ == "__main__":
    main()
