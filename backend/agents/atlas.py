from pathlib import Path
from groq_client import generate_json_with_usage

VALID_DECISIONS = {"AUTO_HANDLE", "APPROVAL_REQUIRED", "ESCALATE_NOW"}

def build_atlas_payload(attention_input):
    message = attention_input.get("message") or {}
    memory = attention_input.get("founder_memory_context") or {}
    business = attention_input.get("business_context") or {}
    return {"message": {k: message.get(k) for k in ("channel", "sender_name", "subject", "body_verbatim")}, "founder_memory_context": {"preferences": memory.get("preferences") or [], "memory_conflict": bool(memory.get("memory_conflict")), "conflicting_preference_ids": memory.get("conflicting_preference_ids") or []}, "business_context": [{k: x.get(k) for k in ("category", "title", "content")} for x in business.get("knowledge") or []]}

def validate_ae_v1(value):
    if not isinstance(value, dict): return ["ae.v1 must be an object"]
    errors = []
    if value.get("schema_version") != "ae.v1": errors.append("schema_version must be ae.v1")
    if value.get("attention_decision") not in VALID_DECISIONS: errors.append("invalid attention_decision")
    score = value.get("attention_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1: errors.append("attention_score must be a number from 0 to 1")
    if not isinstance(value.get("founder_input_required"), bool): errors.append("founder_input_required must be boolean")
    evidence = value.get("evidence")
    if not isinstance(evidence, dict) or any(not isinstance(evidence.get(k), list) for k in ("message_evidence", "business_rule_evidence", "founder_memory_evidence")): errors.append("evidence lists are required")
    plan = value.get("response_plan")
    if not isinstance(plan, dict) or any(not isinstance(plan.get(k), list) for k in ("required_founder_decisions", "optional_recommendations", "missing_information")): errors.append("response_plan lists are required")
    return errors

def run_atlas(attention_input):
    prompt = Path(__file__).with_name("prompts").joinpath("atlas.md").read_text(encoding="utf-8")
    result, usage = generate_json_with_usage(prompt, build_atlas_payload(attention_input))
    # Some JSON-mode responses wrap the requested object with result/data.
    # Keep usage separate and give the validator only the ae.v1 candidate.
    if isinstance(result, dict) and isinstance(result.get("result"), dict):
        result = result["result"]
    elif isinstance(result, dict) and isinstance(result.get("data"), dict):
        result = result["data"]
    return result, usage
