import json

import requests

from config import (
    OLLAMA_MODEL,
    OLLAMA_URL,
    SIGNAL_QUALITY_FAIL_OPEN,
    SIGNAL_QUALITY_FILTER_ENABLED,
    SIGNAL_QUALITY_MIN_SCORE,
    SIGNAL_QUALITY_TIMEOUT_SECONDS,
)


DEFAULT_DECISION = {
    "approved": True,
    "quality_score": 0,
    "materiality": "unknown",
    "priced_in_risk": "unknown",
    "scope": "unknown",
    "confidence_ok": False,
    "rationale": "Quality filter unavailable.",
}


def extract_json_object(text):
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


def clamp_score(value):
    try:
        return max(0, min(100, int(round(float(value)))))
    except Exception:
        return 0


def normalize_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "approved", "pass"}
    return bool(value)


def normalize_decision(raw):
    if not isinstance(raw, dict):
        raw = {}

    quality_score = clamp_score(raw.get("quality_score"))
    decision = {
        "approved": normalize_bool(raw.get("approved", quality_score >= SIGNAL_QUALITY_MIN_SCORE)),
        "quality_score": quality_score,
        "materiality": str(raw.get("materiality") or "unknown").strip().lower(),
        "priced_in_risk": str(raw.get("priced_in_risk") or "unknown").strip().lower(),
        "scope": str(raw.get("scope") or "unknown").strip().lower(),
        "confidence_ok": normalize_bool(raw.get("confidence_ok", quality_score >= SIGNAL_QUALITY_MIN_SCORE)),
        "rationale": str(raw.get("rationale") or "").strip(),
    }
    decision["approved"] = decision["approved"] and decision["quality_score"] >= SIGNAL_QUALITY_MIN_SCORE
    return decision


def build_quality_prompt(payload, headlines=None, reasoning=None):
    ticker = payload.get("ticker")
    action = payload.get("action_type")
    confidence = payload.get("confidence_score")
    channel = payload.get("channel")
    memo = payload.get("investment_memo")

    return f"""
You are a conservative signal quality gate for a paper-trading stock scanner.

Before this signal is allowed into the execution queue, judge whether the signal is actually worth sending.

Questions to answer:
1. Is the news or reasoning materially relevant to {ticker}?
2. Is the likely market impact already priced in or stale?
3. Is the catalyst company-specific, sector-wide, macro-only, or unclear?
4. Is the scanner confidence high enough for this evidence?

Be strict. Block weak, stale, generic, duplicated, hype-only, or low-materiality signals.
Do not invent facts beyond the provided headlines/reasoning.

Signal:
{{
  "ticker": {json.dumps(ticker)},
  "action": {json.dumps(action)},
  "channel": {json.dumps(channel)},
  "scanner_confidence": {json.dumps(confidence)},
  "memo": {json.dumps(memo)}
}}

Headlines:
{json.dumps(headlines or [], ensure_ascii=True, indent=2)}

Scanner reasoning:
{json.dumps(reasoning or "", ensure_ascii=True)}

Return strict JSON only:
{{
  "approved": true,
  "quality_score": 0,
  "materiality": "high|medium|low|unknown",
  "priced_in_risk": "low|medium|high|unknown",
  "scope": "company|sector|macro|unclear",
  "confidence_ok": true,
  "rationale": "one concise sentence"
}}
""".strip()


def apply_quality_to_memo(payload, decision):
    memo = payload.get("investment_memo") or ""
    quality_block = (
        "Qwen Quality Gate:\n"
        f"- Decision: {'APPROVED' if decision['approved'] else 'BLOCKED'}\n"
        f"- Quality Score: {decision['quality_score']}\n"
        f"- Materiality: {decision['materiality']}\n"
        f"- Priced-In Risk: {decision['priced_in_risk']}\n"
        f"- Scope: {decision['scope']}\n"
        f"- Confidence OK: {decision['confidence_ok']}\n"
        f"- Rationale: {decision['rationale']}\n\n"
    )
    payload["investment_memo"] = quality_block + memo
    return payload


def review_signal_quality(payload, headlines=None, reasoning=None):
    if not SIGNAL_QUALITY_FILTER_ENABLED:
        return True, dict(DEFAULT_DECISION), payload

    ticker = payload.get("ticker")
    action = payload.get("action_type")
    print(f"   Qwen quality gate reviewing {action} {ticker}...")

    request_payload = {
        "model": OLLAMA_MODEL,
        "prompt": build_quality_prompt(payload, headlines=headlines, reasoning=reasoning),
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.0},
    }

    try:
        response = requests.post(
            OLLAMA_URL,
            json=request_payload,
            timeout=SIGNAL_QUALITY_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
        decision = normalize_decision(extract_json_object(data.get("response", "{}")))
    except Exception as exc:
        if SIGNAL_QUALITY_FAIL_OPEN:
            decision = dict(DEFAULT_DECISION)
            decision.update(
                {
                    "approved": True,
                    "quality_score": SIGNAL_QUALITY_MIN_SCORE,
                    "confidence_ok": True,
                    "rationale": f"Quality filter unavailable; fail-open enabled: {exc}",
                }
            )
            return True, decision, apply_quality_to_memo(payload, decision)

        decision = dict(DEFAULT_DECISION)
        decision.update({"approved": False, "rationale": f"Quality filter unavailable: {exc}"})
        return False, decision, apply_quality_to_memo(payload, decision)

    apply_quality_to_memo(payload, decision)
    print(
        "   Qwen quality gate: "
        f"{'APPROVED' if decision['approved'] else 'BLOCKED'} "
        f"score={decision['quality_score']} materiality={decision['materiality']} "
        f"priced_in={decision['priced_in_risk']} scope={decision['scope']}"
    )
    if decision.get("rationale"):
        print(f"   Qwen rationale: {decision['rationale']}")
    return decision["approved"], decision, payload
