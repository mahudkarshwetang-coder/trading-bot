import json
import re
import time
from urllib.parse import urlsplit, urlunsplit

import requests

from config import (
    EXECUTION_GATE_ENABLED,
    EXECUTION_GATE_FAIL_OPEN,
    EXECUTION_GATE_KEEP_ALIVE,
    EXECUTION_GATE_MIN_SCORE,
    EXECUTION_GATE_NUM_PREDICT,
    EXECUTION_GATE_RETRY_ATTEMPTS,
    EXECUTION_GATE_RETRY_BACKOFF_SECONDS,
    EXECUTION_GATE_TIMEOUT_SECONDS,
    OLLAMA_MODEL,
    OLLAMA_URL,
)
from llm_metrics import record_llm_metric
from performance_governor import adjust_ollama_runtime, gaming_budget_pause, print_profile_notice


DEFAULT_EXECUTION_DECISION = {
    "approved": False,
    "execution_score": 0,
    "urgency": "low",
    "risk_reward_ok": False,
    "price_chase_risk": "unknown",
    "position_ok": False,
    "session_risk": "unknown",
    "rationale": "Execution gate unavailable.",
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


def normalize_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "approved", "pass"}
    return bool(value)


def normalize_choice(value, allowed, default):
    normalized = str(value or default).strip().lower()
    return normalized if normalized in allowed else default


def normalize_execution_decision(raw):
    if not isinstance(raw, dict):
        raw = {}

    execution_score = clamp_score(raw.get("execution_score"))
    decision = {
        "approved": normalize_bool(raw.get("approved", execution_score >= EXECUTION_GATE_MIN_SCORE)),
        "execution_score": execution_score,
        "urgency": normalize_choice(raw.get("urgency"), {"low", "medium", "high"}, "low"),
        "risk_reward_ok": normalize_bool(raw.get("risk_reward_ok", execution_score >= EXECUTION_GATE_MIN_SCORE)),
        "price_chase_risk": normalize_choice(raw.get("price_chase_risk"), {"low", "medium", "high", "unknown"}, "unknown"),
        "position_ok": normalize_bool(raw.get("position_ok", True)),
        "session_risk": normalize_choice(raw.get("session_risk"), {"low", "medium", "high", "unknown"}, "unknown"),
        "rationale": str(raw.get("rationale") or "").strip(),
    }
    decision["approved"] = (
        decision["approved"]
        and decision["execution_score"] >= EXECUTION_GATE_MIN_SCORE
        and decision["risk_reward_ok"]
        and decision["position_ok"]
        and decision["price_chase_risk"] != "high"
        and decision["session_risk"] != "high"
    )
    return decision


def build_execution_prompt(context):
    return f"""
You are the final execution-quality gate for a paper-trading stock bot before it sends a bracket order to Interactive Brokers TWS.

This is paper-trading training mode. Approve decent, explainable setups that are actionable now, while still blocking weak or unsafe trades.

Evaluate:
1. Signal materiality and whether the memo/catalyst supports execution now.
2. Whether current price, entry order type, estimated entry price, stop loss, and take profit create acceptable risk/reward.
3. Whether the trade appears to chase price movement or stale/generic news.
4. Whether the session type increases risk.
5. Whether the position state supports the action.

Important constraints:
- The configured pass threshold is {EXECUTION_GATE_MIN_SCORE}. This is intentionally about 20% looser than the prior strict threshold for paper training.
- If entry_order_type is MKT, the parent order is intended to execute immediately at market. Judge slippage/session risk from liquidity, session type, price_delta_from_signal_pct, and whether the setup is already extended.
- If entry_limit is present, it is a marketable limit cap used to improve fill odds. Do not call price_chase_risk high merely because BUY entry_limit is above current_price or SELL entry_limit is below current_price.
- Judge price chase using price_delta_from_signal_pct, the memo, and whether the setup already had a large move. If price_at_signal is missing, do not mark high chase solely from the entry order mechanics.
- A BUY may be approved with medium urgency when the catalyst is specific enough, risk/reward is defined, position/session checks are acceptable, and price_chase_risk is low or medium.
- A BUY should still be blocked on weak/generic commentary, unrelated ticker evidence, stale news, very low scanner confidence, or true high price-chase risk.
- A SELL must only be approved when position state supports it.
- If evidence is unclear or the memo references unrelated tickers as the main catalyst, block the order.
- Do not invent outside facts.

Execution context:
{json.dumps(context, ensure_ascii=True, indent=2)}

Return strict JSON only:
{{
  "approved": true,
  "execution_score": 0,
  "urgency": "low|medium|high",
  "risk_reward_ok": true,
  "price_chase_risk": "low|medium|high|unknown",
  "position_ok": true,
  "session_risk": "low|medium|high|unknown",
  "rationale": "one concise sentence"
}}
""".strip()


def apply_execution_to_memo(signal, decision):
    memo = signal.get("investment_memo") or ""
    gate_block = (
        "Qwen Execution Gate:\n"
        f"- Decision: {'APPROVED' if decision['approved'] else 'BLOCKED'}\n"
        f"- Execution Score: {decision['execution_score']}\n"
        f"- Urgency: {decision['urgency']}\n"
        f"- Risk/Reward OK: {decision['risk_reward_ok']}\n"
        f"- Price Chase Risk: {decision['price_chase_risk']}\n"
        f"- Position OK: {decision['position_ok']}\n"
        f"- Session Risk: {decision['session_risk']}\n"
        f"- Rationale: {decision['rationale']}\n\n"
    )
    signal["investment_memo"] = gate_block + memo
    return signal


def review_execution_quality(signal, context):
    if not EXECUTION_GATE_ENABLED:
        decision = dict(DEFAULT_EXECUTION_DECISION)
        decision.update(
            {
                "approved": True,
                "execution_score": 100,
                "urgency": "medium",
                "risk_reward_ok": True,
                "price_chase_risk": "unknown",
                "position_ok": True,
                "session_risk": "unknown",
                "rationale": "Execution gate disabled.",
            }
        )
        return True, decision, signal

    ticker = context.get("ticker")
    action = context.get("action")
    print(f"   Qwen execution gate reviewing {action} {ticker}...")
    print_profile_notice("execution_gate", prefix="   [PERFORMANCE]")

    runtime = adjust_ollama_runtime(
        "execution_gate",
        keep_alive=EXECUTION_GATE_KEEP_ALIVE,
        timeout_seconds=EXECUTION_GATE_TIMEOUT_SECONDS,
        num_predict=EXECUTION_GATE_NUM_PREDICT,
    )
    if runtime["profile"].active:
        gaming_budget_pause("execution_gate", estimated_work_seconds=0.5, critical=True)

    prompt = build_execution_prompt(context)
    request_payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        "keep_alive": runtime["keep_alive"],
        "think": False,
        "options": {
            "temperature": 0.0,
            "num_predict": max(64, int(runtime["num_predict"])),
        },
    }
    chat_payload = {
        "model": request_payload["model"],
        "messages": [{"role": "user", "content": prompt}],
        "format": request_payload["format"],
        "stream": False,
        "keep_alive": request_payload["keep_alive"],
        "think": False,
        "options": request_payload["options"],
    }

    attempts = max(1, int(EXECUTION_GATE_RETRY_ATTEMPTS))
    timeout_seconds = max(10, int(runtime["timeout_seconds"]))
    backoff_seconds = max(1, int(EXECUTION_GATE_RETRY_BACKOFF_SECONDS))
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
                    response = requests.post(OLLAMA_URL, json=chat_payload, timeout=timeout_seconds)
                else:
                    response = requests.post(OLLAMA_URL, json=request_payload, timeout=timeout_seconds)
                response.raise_for_status()
                data = response.json()
                raw_json_string = extract_ollama_text(data)
                if not raw_json_string and not ollama_uses_chat_endpoint():
                    chat_response = requests.post(ollama_chat_url(), json=chat_payload, timeout=timeout_seconds)
                    chat_response.raise_for_status()
                    chat_data = chat_response.json()
                    raw_json_string = extract_ollama_text(chat_data)
                if not raw_json_string:
                    raise RuntimeError("Ollama returned empty response content.")

                decision = normalize_execution_decision(extract_json_object(raw_json_string))
                record_llm_metric(
                    source="execution_quality_gate",
                    task="execution_gate",
                    model=OLLAMA_MODEL,
                    duration_seconds=time.perf_counter() - started_at,
                    success=True,
                    endpoint=endpoint_label,
                    item_count=1,
                    batch_size=1,
                    attempts=attempt,
                    timeout_seconds=timeout_seconds,
                    num_predict=max(64, int(runtime["num_predict"])),
                    prompt_chars=len(prompt),
                    response_chars=len(raw_json_string),
                    extra={"ticker": ticker, "action": action},
                )
                break
            except Exception as exc:
                last_error = exc
                if attempt < attempts:
                    wait_seconds = backoff_seconds * attempt
                    print(
                        f"   Qwen execution gate retry {attempt}/{attempts - 1} "
                        f"in {wait_seconds}s for {action} {ticker}..."
                    )
                    time.sleep(wait_seconds)
                    continue
                raise last_error
    except Exception as exc:
        record_llm_metric(
            source="execution_quality_gate",
            task="execution_gate",
            model=OLLAMA_MODEL,
            duration_seconds=time.perf_counter() - started_at,
            success=False,
            endpoint=endpoint_label,
            item_count=1,
            batch_size=1,
            attempts=attempt or attempts,
            timeout_seconds=timeout_seconds,
            num_predict=max(64, int(runtime["num_predict"])),
            prompt_chars=len(prompt),
            response_chars=len(raw_json_string),
            error=exc,
            extra={"ticker": ticker, "action": action},
        )
        decision = dict(DEFAULT_EXECUTION_DECISION)
        if EXECUTION_GATE_FAIL_OPEN:
            decision.update(
                {
                    "approved": True,
                    "execution_score": EXECUTION_GATE_MIN_SCORE,
                    "risk_reward_ok": True,
                    "position_ok": True,
                    "rationale": f"Execution gate unavailable; fail-open enabled: {exc}",
                }
            )
            return True, decision, apply_execution_to_memo(signal, decision)

        decision.update({"approved": False, "rationale": f"Execution gate unavailable: {exc}"})
        return False, decision, apply_execution_to_memo(signal, decision)

    apply_execution_to_memo(signal, decision)
    print(
        "   Qwen execution gate: "
        f"{'APPROVED' if decision['approved'] else 'BLOCKED'} "
        f"score={decision['execution_score']} urgency={decision['urgency']} "
        f"price_chase={decision['price_chase_risk']} session_risk={decision['session_risk']}"
    )
    if decision.get("rationale"):
        print(f"   Qwen execution rationale: {decision['rationale']}")
    return decision["approved"], decision, signal
