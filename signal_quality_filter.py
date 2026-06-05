import json
import re
import time
from urllib.parse import urlsplit, urlunsplit

import requests

from config import (
    OLLAMA_MODEL,
    OLLAMA_URL,
    SIGNAL_QUALITY_FAIL_OPEN,
    SIGNAL_QUALITY_FILTER_ENABLED,
    SIGNAL_QUALITY_KEEP_ALIVE,
    SIGNAL_QUALITY_MIN_SCORE,
    SIGNAL_QUALITY_NUM_PREDICT,
    SIGNAL_QUALITY_RETRY_ATTEMPTS,
    SIGNAL_QUALITY_RETRY_BACKOFF_SECONDS,
    SIGNAL_QUALITY_TIMEOUT_SECONDS,
)
from llm_metrics import record_llm_metric
from performance_governor import adjust_ollama_runtime, gaming_budget_pause, print_profile_notice


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
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


def ollama_chat_url():
    parsed = urlsplit(OLLAMA_URL)
    if not parsed.scheme or not parsed.netloc:
        return "http://localhost:11434/api/chat"
    return urlunsplit((parsed.scheme, parsed.netloc, "/api/chat", "", ""))


def ollama_uses_chat_endpoint():
    parsed = urlsplit(OLLAMA_URL)
    return parsed.path.rstrip("/").lower().endswith("/api/chat")


def extract_ollama_text(response_data):
    if not isinstance(response_data, dict):
        return ""
    response_text = str(response_data.get("response") or "").strip()
    if response_text:
        return response_text
    message = response_data.get("message")
    if isinstance(message, dict):
        content_text = str(message.get("content") or "").strip()
        if content_text:
            return content_text
    return ""


def clamp_score(value):
    try:
        return max(0, min(100, int(round(float(value)))))
    except Exception:
        return 0


def normalize_confidence_pct(value):
    """
    Normalize scanner confidence into 0-100 percent scale.
    Supports both legacy 0-1 and native 0-100 payloads.
    """
    try:
        numeric = float(value)
    except Exception:
        return 0.0

    if numeric <= 1.0:
        numeric *= 100.0
    return round(max(0.0, min(100.0, numeric)), 2)


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
    confidence = normalize_confidence_pct(payload.get("confidence_score"))
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

    payload["confidence_score"] = normalize_confidence_pct(payload.get("confidence_score"))

    ticker = payload.get("ticker")
    action = payload.get("action_type")
    print(f"   Qwen quality gate reviewing {action} {ticker}...")
    print_profile_notice("signal_quality", prefix="   [PERFORMANCE]")

    runtime = adjust_ollama_runtime(
        "signal_quality",
        keep_alive=SIGNAL_QUALITY_KEEP_ALIVE,
        timeout_seconds=SIGNAL_QUALITY_TIMEOUT_SECONDS,
        num_predict=SIGNAL_QUALITY_NUM_PREDICT,
    )
    if runtime["profile"].active:
        gaming_budget_pause("signal_quality", estimated_work_seconds=0.5, critical=True)

    request_payload = {
        "model": OLLAMA_MODEL,
        "prompt": build_quality_prompt(payload, headlines=headlines, reasoning=reasoning),
        "format": "json",
        "stream": False,
        "keep_alive": runtime["keep_alive"],
        "think": False,
        "options": {
            "temperature": 0.0,
            "num_predict": max(48, int(runtime["num_predict"])),
        },
    }
    chat_payload = {
        "model": request_payload["model"],
        "messages": [{"role": "user", "content": request_payload["prompt"]}],
        "format": request_payload["format"],
        "stream": False,
        "keep_alive": request_payload["keep_alive"],
        "think": False,
        "options": request_payload["options"],
    }

    attempts = max(1, int(SIGNAL_QUALITY_RETRY_ATTEMPTS))
    timeout_seconds = max(10, int(runtime["timeout_seconds"]))
    backoff_seconds = max(1, int(SIGNAL_QUALITY_RETRY_BACKOFF_SECONDS))
    started_at = time.perf_counter()
    endpoint_label = "chat" if ollama_uses_chat_endpoint() else "generate"
    raw_json_string = ""
    attempt = 0

    try:
        last_error = None
        decision = None

        for attempt in range(1, attempts + 1):
            try:
                if ollama_uses_chat_endpoint():
                    response = requests.post(
                        OLLAMA_URL,
                        json=chat_payload,
                        timeout=timeout_seconds,
                    )
                else:
                    response = requests.post(
                        OLLAMA_URL,
                        json=request_payload,
                        timeout=timeout_seconds,
                    )
                response.raise_for_status()
                data = response.json()
                raw_json_string = extract_ollama_text(data)
                if not raw_json_string and not ollama_uses_chat_endpoint():
                    chat_response = requests.post(
                        ollama_chat_url(),
                        json=chat_payload,
                        timeout=timeout_seconds,
                    )
                    chat_response.raise_for_status()
                    chat_data = chat_response.json()
                    raw_json_string = extract_ollama_text(chat_data)
                if not raw_json_string:
                    raise RuntimeError("Ollama returned empty response content.")
                decision = normalize_decision(extract_json_object(raw_json_string))
                record_llm_metric(
                    source="signal_quality_filter",
                    task="quality_gate",
                    model=OLLAMA_MODEL,
                    duration_seconds=time.perf_counter() - started_at,
                    success=True,
                    endpoint=endpoint_label,
                    item_count=1,
                    batch_size=1,
                    attempts=attempt,
                    timeout_seconds=timeout_seconds,
                    num_predict=max(48, int(runtime["num_predict"])),
                    prompt_chars=len(request_payload["prompt"]),
                    response_chars=len(raw_json_string),
                    extra={"ticker": ticker, "action": action},
                )
                break
            except Exception as exc:
                last_error = exc
                if attempt < attempts:
                    wait_seconds = backoff_seconds * attempt
                    print(
                        f"   Qwen quality gate retry {attempt}/{attempts - 1} "
                        f"in {wait_seconds}s for {action} {ticker}..."
                    )
                    time.sleep(wait_seconds)
                    continue
                raise last_error
    except Exception as exc:
        record_llm_metric(
            source="signal_quality_filter",
            task="quality_gate",
            model=OLLAMA_MODEL,
            duration_seconds=time.perf_counter() - started_at,
            success=False,
            endpoint=endpoint_label,
            item_count=1,
            batch_size=1,
            attempts=attempt or attempts,
            timeout_seconds=timeout_seconds,
            num_predict=max(48, int(runtime["num_predict"])),
            prompt_chars=len(request_payload["prompt"]),
            response_chars=len(raw_json_string),
            error=exc,
            extra={"ticker": ticker, "action": action},
        )
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
