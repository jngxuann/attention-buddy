"""
Deterministic learning processor for founder feedback.

Inspects founder feedback and determines whether it contains a reusable
preference signal.  Uses rule-based logic only — no LLM calls.
"""

from datetime import datetime, timezone
import json
import uuid

from database import supabase


# ── controlled vocabularies ──────────────────────────────────────────

VALID_MEMORY_STATUSES = {
    "OBSERVED",
    "EMERGING",
    "ESTABLISHED",
    "EXPLICIT",
    "CONFLICTED",
}

VALID_CONFIDENCE = {"low", "medium", "high"}

LEARNING_CATEGORIES = {
    "ATTENTION_PREFERENCE",
    "AUTHORITY_PREFERENCE",
    "COMMUNICATION_PREFERENCE",
    "OBSERVED_CORRECTION",
}

# ── helpers ──────────────────────────────────────────────────────────

def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _infer_learning_category(feedback):
    """
    Decide which learning bucket this feedback falls into based on the
    action type and whether the decision or draft changed.
    """
    decision_changed = (
        feedback.get("original_decision")
        != feedback.get("final_decision")
    )

    draft_changed = False
    if feedback.get("original_draft") and feedback.get("final_draft"):
        draft_changed = (
            feedback["original_draft"] != feedback["final_draft"]
        )

    action = feedback.get("action_type", "")

    if action in ("OVERRIDDEN",):
        return "ATTENTION_PREFERENCE"

    if action in ("EDITED",) and draft_changed:
        return "COMMUNICATION_PREFERENCE"

    if action in ("APPROVED", "REJECTED", "DELEGATED"):
        if decision_changed:
            return "AUTHORITY_PREFERENCE"
        return "OBSERVED_CORRECTION"

    return "OBSERVED_CORRECTION"


def _build_learning_payload(feedback, category, pipeline_run_id):
    return {
        "original_ai_decision": feedback.get("original_decision"),
        "final_founder_decision": feedback.get("final_decision"),
        "action_type": feedback.get("action_type"),
        "founder_explanation": feedback.get("founder_explanation"),
        "decision_changed": (
            feedback.get("original_decision")
            != feedback.get("final_decision")
        ),
        "draft_changed": (
            feedback.get("original_draft") is not None
            and feedback.get("final_draft") is not None
            and feedback["original_draft"]
            != feedback["final_draft"]
        ),
        "inferred_learning_category": category,
        "source_pipeline_run_id": pipeline_run_id,
    }


# ── main processor ───────────────────────────────────────────────────

def process_founder_feedback(feedback_row):
    """
    Inspect a single founder_feedback row and:

    1. Classify durability:
       - OBSERVED   (no explanation)        → learning event only
       - EMERGING   (explanation, no opt-in) → candidate preference (inactive)
       - EXPLICIT   (opt-in + explanation)   → active preference immediately
    2. Create a ``learning_events`` row.
    3. When justified, create or update a ``founder_preferences`` row.
       EMERGING preferences are inactive candidates; they promote to
       ESTABLISHED (active) after 3+ supporting observations.
    4. Return the learning_event row and the preference row (or None).

    Returns
    -------
    dict
        {
            "learning_event": {...},
            "preference_update": {...} | None,
        }
    """

    explanation = (feedback_row.get("founder_explanation") or "").strip()
    apply_to_similar = feedback_row.get("apply_to_similar", False)
    action_type = feedback_row.get("action_type", "")
    pipeline_run_id = feedback_row.get("pipeline_run_id", "")

    # ── 1. classify memory_status & confidence ───────────────────

    # EXPLICIT only when the founder both opts in ("Apply this to similar
    # messages") AND provides a non-empty explanation expressing a
    # reusable rule. Everything else stays OBSERVED or EMERGING.
    if apply_to_similar and explanation:
        memory_status = "EXPLICIT"
        confidence = "high"
    elif explanation:
        memory_status = "EMERGING"
        confidence = "medium"
    else:
        memory_status = "OBSERVED"
        confidence = "low"

    # ── 2. infer learning category ───────────────────────────────

    category = _infer_learning_category(feedback_row)

    # ── 3. build payload ─────────────────────────────────────────

    learning_payload = _build_learning_payload(
        feedback_row, category, pipeline_run_id
    )

    # ── 4. insert learning_event ─────────────────────────────────

    # Determine founder_id — try to get it from the pipeline run's
    # associated message, otherwise use a default.
    founder_id = _resolve_founder_id(pipeline_run_id)

    learning_event_row = {
        "id": str(uuid.uuid4()),
        "founder_id": founder_id,
        "feedback_id": feedback_row.get("id"),
        "event_type": category,
        "memory_status": memory_status,
        "confidence": confidence,
        "learning_event_payload": learning_payload,
        "created_at": _now_iso(),
    }

    learning_response = (
        supabase
        .table("learning_events")
        .insert(learning_event_row)
        .execute()
    )

    learning_event = (
        learning_response.data[0]
        if learning_response.data
        else learning_event_row
    )

    # ── 5. create / update founder_preference (only when justified) ──

    preference_update = None

    # Only create a preference when the feedback is explicit or emerging
    # with a real explanation. OBSERVED corrections don't become rules.
    if memory_status in ("EXPLICIT", "EMERGING") and explanation:
        preference_update = _upsert_preference(
            founder_id=founder_id,
            explanation=explanation,
            category=category,
            memory_status=memory_status,
            confidence=confidence,
            learning_event_id=learning_event["id"],
            pipeline_run_id=pipeline_run_id,
            feedback_row=feedback_row,
        )

    return {
        "learning_event": learning_event,
        "preference_update": preference_update,
    }


# ── preference upsert logic ──────────────────────────────────────────

def _resolve_source_message_id(pipeline_run_id):
    """
    Resolve a real message UUID from a pipeline_run_id.

    Returns the message UUID string, or None if unresolvable.
    Never fabricates an ID.
    """
    if not pipeline_run_id:
        return None
    try:
        resp = (
            supabase
            .table("pipeline_runs")
            .select("message_id")
            .eq("id", pipeline_run_id)
            .limit(1)
            .execute()
        )
        if resp.data and resp.data[0].get("message_id"):
            return resp.data[0]["message_id"]
    except (KeyError, TypeError, IndexError, AttributeError, ConnectionError):
        pass
    return None


def _upsert_preference(
    founder_id,
    explanation,
    category,
    memory_status,
    confidence,
    learning_event_id,
    pipeline_run_id,
    feedback_row,
):
    """
    Create or update a founder_preferences row.

    Checks for any existing preference with the same scope (active or
    inactive) so EMERGING candidates can be reinforced rather than
    duplicated.  When multiple matches exist, active preferences take
    priority.  Handles contradiction detection and EMERGING→ESTABLISHED
    promotion at 3+ observations.
    """

    scope = _derive_scope(category, explanation)
    source_msg_id = _resolve_source_message_id(pipeline_run_id)

    # Check for any existing preference with the same scope — active or
    # inactive.  We fetch all matches and prefer active ones so that
    # ESTABLISHED / EXPLICIT preferences are always found first.
    query = (
        supabase
        .table("founder_preferences")
        .select("*")
        .eq("scope", scope)
    )
    # founder_id may be None (nullable); use is_ for NULL-aware lookup.
    if founder_id is None:
        query = query.is_("founder_id", "null")
    else:
        query = query.eq("founder_id", founder_id)
    existing_response = query.execute()

    candidates = existing_response.data or []
    # Prefer active preferences; if multiple, take the most recently
    # reinforced (or created) one.  ISO-8601 strings sort
    # lexicographically in ascending order, so reverse=True gives
    # newest first.
    candidates.sort(
        key=lambda r: (
            not r.get("active", False),
        ),
        reverse=False,
    )
    # Active first, then newest first among ties.
    candidates.sort(
        key=lambda r: (
            r.get("last_reinforced_at") or r.get("created_at") or ""
        ),
        reverse=True,
    )
    # Active sort is primary, so do it last (Python sort is stable).
    candidates.sort(
        key=lambda r: not r.get("active", False),
    )
    # Among scope-matched candidates, only reinforce if the rule text
    # points in the same direction.  Two semantically different rules
    # (e.g. "I review wholesale" vs "I delegate wholesale") must not
    # accidentally reinforce each other.
    similar = [
        c for c in candidates
        if _rules_are_similar(
            c.get("rule", ""),
            _build_rule_text(explanation, category),
        )
    ]
    existing = similar[0] if similar else None

    if existing:
        # Check for contradiction
        contradiction = _detect_contradiction(
            existing, explanation, memory_status
        )

        if contradiction:
            # Mark existing as CONFLICTED
            new_contradictions = existing.get("contradiction_count", 0) + 1
            (
                supabase
                .table("founder_preferences")
                .update({
                    "memory_status": "CONFLICTED",
                    "contradiction_count": new_contradictions,
                    "active": False,
                })
                .eq("id", existing["id"])
                .execute()
            )

            # Create new preference row for the new direction.
            # EXPLICIT is active immediately; everything else starts
            # inactive (candidate).
            pref_row = {
                "id": str(uuid.uuid4()),
                "founder_id": founder_id,
                "rule": _build_rule_text(explanation, category),
                "scope": scope,
                "memory_status": memory_status,
                "confidence": confidence,
                "supporting_observations": 1,
                "contradiction_count": 0,
                "active": (memory_status == "EXPLICIT"),
                "source_learning_event_ids": [learning_event_id],
                "source_message_ids": [source_msg_id] if source_msg_id else [],
                "last_reinforced_at": _now_iso(),
                "created_at": _now_iso(),
            }
        else:
            # Reinforce existing preference
            new_observations = existing.get("supporting_observations", 0) + 1
            existing_event_ids = existing.get(
                "source_learning_event_ids", []
            ) or []
            if isinstance(existing_event_ids, str):
                try:
                    existing_event_ids = json.loads(existing_event_ids)
                except (json.JSONDecodeError, TypeError):
                    existing_event_ids = []
            existing_event_ids.append(learning_event_id)

            # Append source message ID without duplicates
            existing_msg_ids = existing.get("source_message_ids") or []
            if isinstance(existing_msg_ids, str):
                try:
                    existing_msg_ids = json.loads(existing_msg_ids)
                except (json.JSONDecodeError, TypeError):
                    existing_msg_ids = []
            if source_msg_id and source_msg_id not in existing_msg_ids:
                existing_msg_ids.append(source_msg_id)

            new_memory_status = existing.get("memory_status")
            new_active = existing.get("active", True)
            new_confidence = confidence

            # Promote EMERGING → ESTABLISHED when enough evidence
            # has accumulated (3+ supporting observations).
            if new_observations >= 3:
                new_confidence = "high"
                if existing.get("memory_status") == "EMERGING":
                    new_memory_status = "ESTABLISHED"
                    new_active = True

            (
                supabase
                .table("founder_preferences")
                .update({
                    "supporting_observations": new_observations,
                    "source_learning_event_ids": existing_event_ids,
                    "source_message_ids": existing_msg_ids,
                    "last_reinforced_at": _now_iso(),
                    "confidence": new_confidence,
                    "memory_status": new_memory_status,
                    "active": new_active,
                })
                .eq("id", existing["id"])
                .execute()
            )

            # Return the updated row
            return {
                "id": existing["id"],
                "founder_id": founder_id,
                "rule": existing.get("rule"),
                "scope": scope,
                "memory_status": new_memory_status,
                "confidence": new_confidence,
                "supporting_observations": new_observations,
                "active": new_active,
                "action": "reinforced",
            }
    else:
        # No existing preference — create new.
        # EXPLICIT is active immediately; EMERGING starts as a candidate.
        pref_row = {
            "id": str(uuid.uuid4()),
            "founder_id": founder_id,
            "rule": _build_rule_text(explanation, category),
            "scope": scope,
            "memory_status": memory_status,
            "confidence": confidence,
            "supporting_observations": 1,
            "contradiction_count": 0,
            "active": (memory_status == "EXPLICIT"),
            "source_learning_event_ids": [learning_event_id],
            "source_message_ids": [source_msg_id] if source_msg_id else [],
            "last_reinforced_at": _now_iso(),
            "created_at": _now_iso(),
        }

    # Insert the new preference
    insert_response = (
        supabase
        .table("founder_preferences")
        .insert(pref_row)
        .execute()
    )

    result = insert_response.data[0] if insert_response.data else pref_row
    result["action"] = "created"
    return result


def _derive_scope(category, explanation):
    """Derive a short scope tag from the category and explanation."""
    scope_map = {
        "ATTENTION_PREFERENCE": "attention_routing",
        "AUTHORITY_PREFERENCE": "authority_boundary",
        "COMMUNICATION_PREFERENCE": "communication_style",
    }
    base = scope_map.get(category, "general")

    # Add a hint from the explanation for distinction
    lower = explanation.lower()
    if "wholesale" in lower or "pricing" in lower:
        base = f"{base}_pricing"
    elif "first-time" in lower or "first time" in lower:
        base = f"{base}_first_time"
    elif "urgent" in lower or "emergency" in lower:
        base = f"{base}_urgent"

    return base


def _build_rule_text(explanation, category):
    """
    Build a concise rule description from the founder's explanation.
    Since we don't use an LLM, we preserve the founder's own words.
    """
    # For EXPLICIT rules, use the explanation directly as the rule text
    # with minor cleanup.
    rule = explanation.strip()
    # Capitalize first letter if not already
    if rule and rule[0].islower():
        rule = rule[0].upper() + rule[1:]
    # Ensure it ends with a period
    if rule and not rule.endswith("."):
        rule = rule + "."
    return rule


def _rules_are_similar(rule_a, rule_b):
    """
    Determine whether two rule texts describe the same reusable pattern.

    Uses deterministic word-set overlap (Jaccard similarity) on
    lowercased, punctuation-stripped tokens.  Returns True when the
    overlap exceeds a threshold.

    This prevents "I personally review wholesale" and "I delegate
    wholesale" from reinforcing each other just because they share
    a pricing scope.
    """
    import re

    def _tokenize(text):
        # Lowercase, strip punctuation, split into words, drop very
        # common stop-words that carry no directional signal.
        cleaned = re.sub(r"[^\w\s]", "", text.lower())
        stops = {
            "i", "me", "my", "we", "our", "the", "a", "an",
            "to", "for", "of", "in", "on", "at", "is", "are",
            "this", "that", "it", "be", "and", "or", "with",
        }
        return {w for w in cleaned.split() if w and w not in stops}

    tokens_a = _tokenize(rule_a)
    tokens_b = _tokenize(rule_b)

    if not tokens_a or not tokens_b:
        return False

    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    jaccard = len(intersection) / len(union)

    # Require meaningful word overlap.
    return jaccard >= 0.4


def _detect_contradiction(existing, new_explanation, new_memory_status):
    """
    Simple contradiction detection.
    If the new explanation contains negation language (e.g. 'no longer',
    'don't want to', 'stop') and contradicts the existing rule direction,
    flag it as a contradiction.
    """
    new_lower = new_explanation.lower()
    negation_signals = [
        "no longer", "don't want", "do not want", "stop", "never mind",
        "i changed my mind", "scratch that", "forget",
    ]

    has_negation = any(signal in new_lower for signal in negation_signals)

    # Only flag as contradiction when the new statement explicitly
    # negates the prior rule AND the new statement is itself explicit.
    return has_negation and new_memory_status == "EXPLICIT"


def _resolve_founder_id(pipeline_run_id):
    """
    Try to resolve a founder_id from the pipeline run's message.

    Returns None when no real founder UUID can be resolved — never
    fabricates an ID that could violate a UUID column constraint.
    """
    if not pipeline_run_id:
        return None

    try:
        pipeline_response = (
            supabase
            .table("pipeline_runs")
            .select("message_id")
            .eq("id", pipeline_run_id)
            .limit(1)
            .execute()
        )

        if pipeline_response.data:
            message_id = pipeline_response.data[0].get("message_id")
            if message_id:
                msg_response = (
                    supabase
                    .table("messages")
                    .select("founder_id")
                    .eq("id", message_id)
                    .limit(1)
                    .execute()
                )
                if msg_response.data and msg_response.data[0].get("founder_id"):
                    return msg_response.data[0]["founder_id"]
    except (KeyError, TypeError, IndexError):
        pass

    return None
