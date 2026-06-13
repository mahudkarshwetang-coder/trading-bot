import argparse
from collections import Counter
from datetime import datetime, timezone

from config import EXPERIMENTAL_LOCAL_LOG_PATH, get_supabase_client
from local_data_recorder import append_local_event
from signal_source_hub import latest_source_run
from system_status import publish_system_status


SOURCE = "experimental_findings"
SETTINGS_TABLE = "bot_settings"


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def latest_experimental_result(supabase):
    response = (
        supabase.table(SETTINGS_TABLE)
        .select("experimental_last_result")
        .eq("id", 1)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    if not rows:
        return {}
    result = rows[0].get("experimental_last_result")
    return result if isinstance(result, dict) else {}


def compact_source_summary(source_run):
    if not isinstance(source_run, dict) or not source_run:
        return {}
    return {
        "generated_at": source_run.get("generated_at"),
        "source_counts": source_run.get("source_counts") or {},
        "candidate_count": source_run.get("candidate_count") or 0,
        "overlap_count": source_run.get("overlap_count") or 0,
        "overlap_tickers": (source_run.get("overlap_tickers") or [])[:40],
        "warnings": (source_run.get("warnings") or [])[:10],
        "top_candidates": (source_run.get("top_candidates") or [])[:20],
    }


def build_findings(result):
    outcomes = result.get("outcomes") if isinstance(result.get("outcomes"), list) else []
    candidates = [row for row in outcomes if row.get("status") in {"candidate", "dry_run", "sent"}]
    skipped = [row for row in outcomes if row.get("status") == "skipped"]
    skip_reasons = Counter(str(row.get("reason") or "unknown") for row in skipped)
    candidate_symbols = [row.get("ticker") for row in candidates if row.get("ticker")]
    avg_volatility = None
    vol_values = [
        float(row["monthly_volatility_pct"])
        for row in candidates
        if isinstance(row.get("monthly_volatility_pct"), (int, float))
    ]
    if vol_values:
        avg_volatility = round(sum(vol_values) / len(vol_values), 2)

    enough_to_automate = False
    automation_reason = "manual review required until experimental baskets have repeated outcome samples"
    if result.get("sent", 0) >= 20 and result.get("status") == "success":
        automation_reason = "execution sample exists, but keep manual review until post-trade reviews confirm edge"

    source_summary = compact_source_summary(latest_source_run())

    return {
        "generated_at": utc_now_iso(),
        "source": SOURCE,
        "run_id": result.get("run_id"),
        "basket_status": result.get("status"),
        "execution_mode": result.get("execution_mode"),
        "candidate_count": len(candidates),
        "skipped_count": len(skipped),
        "candidate_tickers": candidate_symbols[:80],
        "average_monthly_volatility_pct": avg_volatility,
        "top_skip_reasons": [
            {"reason": reason, "count": count}
            for reason, count in skip_reasons.most_common(8)
        ],
        "automation_ready": enough_to_automate,
        "automation_reason": automation_reason,
        "source_summary": source_summary,
        "next_step": (
            "Review the candidate list on iPad, then run experimental-execute if the basket looks acceptable."
            if candidates
            else "No executable candidates were found; rebuild category universe or loosen filters before executing."
        ),
    }


def publish_findings(supabase, result, findings, dry_run=False):
    payload = dict(result or {})
    payload["findings"] = findings
    if findings.get("source_summary"):
        payload["source_summary"] = findings["source_summary"]
    append_local_event(
        "experimental_findings_generated",
        findings,
        source=SOURCE,
        path=EXPERIMENTAL_LOCAL_LOG_PATH,
    )
    if dry_run:
        print(findings)
        return True

    supabase.table(SETTINGS_TABLE).update(
        {
            "experimental_last_result": payload,
            "experimental_last_requested_at": findings["generated_at"],
        }
    ).eq("id", 1).execute()
    publish_system_status(
        "experimental_findings",
        "success",
        detail=f"Generated findings for {findings['candidate_count']} candidate(s).",
        metadata=findings,
    )
    return True


def run_experimental_findings(dry_run=False):
    supabase = get_supabase_client()
    result = latest_experimental_result(supabase)
    if not result:
        findings = {
            "generated_at": utc_now_iso(),
            "source": SOURCE,
            "candidate_count": 0,
            "skipped_count": 0,
            "automation_ready": False,
            "automation_reason": "no experimental basket has been built yet",
            "source_summary": compact_source_summary(latest_source_run()),
            "next_step": "Run experimental-build first.",
        }
    else:
        findings = build_findings(result)
    print(
        "[EXPERIMENTAL FINDINGS] "
        f"candidates={findings.get('candidate_count', 0)} "
        f"skipped={findings.get('skipped_count', 0)} "
        f"automation_ready={findings.get('automation_ready')}"
    )
    return publish_findings(supabase, result, findings, dry_run=dry_run)


def parse_args():
    parser = argparse.ArgumentParser(description="Summarize experimental basket findings before execution.")
    parser.add_argument("--dry-run", action="store_true", help="Print findings without updating Supabase.")
    return parser.parse_args()


def main():
    args = parse_args()
    success = run_experimental_findings(dry_run=args.dry_run)
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
