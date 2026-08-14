from pathlib import Path
from groq_client import generate_json_with_usage

def build_clio_payload(attention_input, atlas_output):
    message = attention_input.get("message") or {}; business = attention_input.get("business_context") or {}; memory = attention_input.get("founder_memory_context") or {}
    return {"message": {k: message.get(k) for k in ("sender_name", "subject", "body_verbatim")}, "atlas": {k: atlas_output.get(k) for k in ("attention_decision", "founder_input_required", "response_plan")}, "business_context": [{k: x.get(k) for k in ("category", "title", "content")} for x in business.get("knowledge") or []], "founder_memory_context": {"preferences": memory.get("preferences") or []}}

def validate_cl_v1(value):
    if not isinstance(value, dict): return ["cl.v1 must be an object"]
    errors = []
    if value.get("schema_version") != "cl.v1": errors.append("schema_version must be cl.v1")
    if value.get("action") not in {"DRAFT", "HOLD"}: errors.append("invalid action")
    draft = value.get("draft")
    if not isinstance(draft, dict) or not isinstance(draft.get("body"), str): errors.append("draft.body must be a string")
    grounding = value.get("grounding")
    if not isinstance(grounding, dict) or any(not isinstance(grounding.get(k), list) for k in ("business_rules_used", "founder_preferences_used")): errors.append("grounding lists are required")
    if not isinstance(value.get("approval_required"), bool): errors.append("approval_required must be boolean")
    if not isinstance(value.get("unresolved_items"), list): errors.append("unresolved_items must be a list")
    return errors

def run_clio(attention_input, atlas_output):
    prompt = Path(__file__).with_name("prompts").joinpath("clio.md").read_text(encoding="utf-8")
    result, usage = generate_json_with_usage(prompt, build_clio_payload(attention_input, atlas_output))
    if isinstance(result, dict) and isinstance(result.get("result"), dict):
        result = result["result"]
    elif isinstance(result, dict) and isinstance(result.get("data"), dict):
        result = result["data"]
    return result, usage
