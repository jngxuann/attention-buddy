"""
Pipeline orchestration for Attention Buddy.

Constructs the canonical ``attention_buddy_input.v1`` payload that
will eventually be passed to the WorkBuddy expert team.

Flow:
    message
    -> retrieve relevant preferences
    -> construct founder_memory_context
    -> construct attention_buddy_input.v1
    -> Atlas -> Clio -> deterministic pipeline.v1 assembly
    -> persist pipeline run (only when real response exists)
"""

import uuid
from datetime import datetime, timezone

from business_knowledge import get_relevant_business_knowledge
from database import supabase
from preferences import get_relevant_preferences
from agents.atlas import run_atlas, validate_ae_v1
from agents.clio import run_clio, validate_cl_v1
from groq_client import GroqError
from gmail import send_gmail_message, describe_gmail_error


# ── controlled vocabularies ──────────────────────────────────────────

VALID_DECISIONS = {"AUTO_HANDLE", "APPROVAL_REQUIRED", "ESCALATE_NOW"}
VALID_URGENCY = {"low", "medium", "high", "critical"}
VALID_SENTIMENT = {"positive", "neutral", "negative", "mixed"}
VALID_RISK = {"low", "medium", "high", "critical"}
VALID_INFERENCE_CONFIDENCE = {"low", "medium", "high"}
VALID_COMM_STATUS = {
    "PLANNED", "READY", "AWAITING_APPROVAL",
    "EXECUTED", "HELD", "FAILED",
}
VALID_MEMORY_STATUSES = {
    "OBSERVED", "EMERGING", "ESTABLISHED",
    "EXPLICIT", "CONFLICTED",
}

# Maximum number of Business Knowledge records the pipeline retrieves for
# a message (used by both /api/pipeline/preview and /api/pipeline/process).
PIPELINE_BUSINESS_KNOWLEDGE_LIMIT = 7

# ── helpers ──────────────────────────────────────────────────────────

def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class ApprovalError(Exception):
    """Controlled founder-approval / draft-editing error with an HTTP status."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


# ── founder memory context ───────────────────────────────────────────

def build_founder_memory_context(message, founder_id=None):
    """
    Retrieve relevant preferences for a message and build the
    ``founder_memory_context`` block.

    Returns
    -------
    dict
        {
            "retrieval_status": "COMPLETE",
            "preferences": [...],
            "memory_conflict": false,
            "conflicting_preference_ids": [],
        }
    """
    message_context = {
        "subject": message.get("subject"),
        "body": message.get("body_verbatim"),
        "sender": message.get("sender_name"),
        "channel": message.get("channel"),
    }
    # Strip None values
    message_context = {
        k: v for k, v in message_context.items() if v is not None
    }

    retrieval = get_relevant_preferences(
        message_context=message_context,
        founder_id=founder_id,
        limit=5,
    )

    # Transform keys to match the canonical founder_memory_context shape
    compact_prefs = []
    for p in retrieval.get("preferences", []):
        compact_prefs.append({
            "preference_id": p.get("id"),
            "rule": p.get("rule"),
            "scope": p.get("scope"),
            "memory_status": p.get("memory_status"),
            "confidence": p.get("confidence"),
            "relevance_score": p.get("relevance_score"),
        })

    return {
        "retrieval_status": "COMPLETE",
        "preferences": compact_prefs,
        "memory_conflict": retrieval.get("memory_conflict") or False,
        "conflicting_preference_ids": (
            retrieval.get("conflicting_preference_ids") or []
        ),
    }


# ── business knowledge context ──────────────────────────────────────

def build_business_context(message, founder_id=None):
    """
    Retrieve relevant Business Knowledge for a message and build the
    ``business_context`` block.

    Business Knowledge is factual/policy information, retrieved
    independently of founder memory.  Empty Business Knowledge is valid:
    the pipeline never fails merely because nothing matched.

    Returns
    -------
    dict
        {
            "retrieval_status": "COMPLETE",
            "knowledge": [...],
        }
    """
    retrieval = get_relevant_business_knowledge(
        subject=message.get("subject") or "",
        body=message.get("body_verbatim") or "",
        founder_id=founder_id,
        limit=PIPELINE_BUSINESS_KNOWLEDGE_LIMIT,
    )

    return {
        "retrieval_status": "COMPLETE",
        "knowledge": retrieval.get("knowledge", []),
    }


# ── canonical pipeline input ─────────────────────────────────────────

def build_pipeline_input(message, founder_id=None):
    """
    Build the full ``attention_buddy_input.v1`` object for a message.

    Parameters
    ----------
    message : dict
        A row from the ``messages`` table.
    founder_id : str | None

    Returns
    -------
    dict
        The canonical pipeline input.
    """
    memory_ctx = build_founder_memory_context(message, founder_id=founder_id)
    business_ctx = build_business_context(message, founder_id=founder_id)

    return {
        "schema_version": "attention_buddy_input.v1",

        "message": {
            "id": message.get("id"),
            "channel": message.get("channel"),
            "sender_name": message.get("sender_name"),
            "sender_address": message.get("sender_address"),
            "subject": message.get("subject"),
            "body_verbatim": message.get("body_verbatim"),
            "received_at": message.get("received_at"),
        },

        "founder_memory_context": memory_ctx,

        "business_context": business_ctx,
    }


def build_attention_buddy_input(message_id, founder_id=None):
    """Load a message and build the single canonical agent input."""
    response = supabase.table("messages").select("*").eq("id", message_id).limit(1).execute()
    if not response.data:
        raise ValueError("Message not found")
    return build_pipeline_input(response.data[0], founder_id=founder_id)


# ── pipeline.v1 validation ───────────────────────────────────────────

def validate_pipeline_v1(response):
    """
    Validate a pipeline.v1 response from the WorkBuddy team.

    Checks controlled vocabularies and structural constraints.
    Returns a list of error strings (empty list = valid).

    Never silently translates invalid values.
    """
    errors = []

    if not isinstance(response, dict):
        return ["Response is not a JSON object"]

    # ── attention_decision ───────────────────────────────────────
    decision = response.get("attention_decision")
    if decision is not None and decision not in VALID_DECISIONS:
        errors.append(
            f"Invalid attention_decision: '{decision}'. "
            f"Must be one of: {sorted(VALID_DECISIONS)}"
        )

    # ── attention_score ──────────────────────────────────────────
    score = response.get("attention_score")
    if score is not None:
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or score < 0
            or score > 1
        ):
            errors.append(
                f"Invalid attention_score: {score}. "
                f"Must be a number between 0 and 1."
            )

    # ── urgency ──────────────────────────────────────────────────
    urgency = response.get("urgency")
    if urgency is not None and urgency not in VALID_URGENCY:
        errors.append(
            f"Invalid urgency: '{urgency}'. "
            f"Must be one of: {sorted(VALID_URGENCY)}"
        )

    # ── sentiment ────────────────────────────────────────────────
    sentiment = response.get("sentiment")
    if sentiment is not None and sentiment not in VALID_SENTIMENT:
        errors.append(
            f"Invalid sentiment: '{sentiment}'. "
            f"Must be one of: {sorted(VALID_SENTIMENT)}"
        )

    # ── risk_severity ────────────────────────────────────────────
    risk = response.get("risk_severity")
    if risk is not None and risk not in VALID_RISK:
        errors.append(
            f"Invalid risk_severity: '{risk}'. "
            f"Must be one of: {sorted(VALID_RISK)}"
        )

    # ── inference_confidence ─────────────────────────────────────
    inf_conf = response.get("inference_confidence")
    if inf_conf is not None and inf_conf not in VALID_INFERENCE_CONFIDENCE:
        errors.append(
            f"Invalid inference_confidence: '{inf_conf}'. "
            f"Must be one of: {sorted(VALID_INFERENCE_CONFIDENCE)}"
        )

    # ── communication_status ─────────────────────────────────────
    comm = response.get("communication_status")
    if comm is not None and comm not in VALID_COMM_STATUS:
        errors.append(
            f"Invalid communication_status: '{comm}'. "
            f"Must be one of: {sorted(VALID_COMM_STATUS)}"
        )

    # ── ui_summary ───────────────────────────────────────────────
    ui = response.get("ui_summary")
    if ui is not None:
        if not isinstance(ui, dict):
            errors.append("ui_summary must be a JSON object")
        else:
            # route must equal attention_decision
            route = ui.get("route")
            if route is not None and route != decision:
                errors.append(
                    f"ui_summary.route '{route}' does not match "
                    f"attention_decision '{decision}'"
                )

            # attention_score must be between 0 and 1
            score = ui.get("attention_score")
            if score is not None:
                if not isinstance(score, (int, float)) or score < 0 or score > 1:
                    errors.append(
                        f"ui_summary.attention_score must be 0-1, got {score}"
                    )

            # required_decisions must be an array
            req = ui.get("required_decisions")
            if req is not None and not isinstance(req, list):
                errors.append("ui_summary.required_decisions must be an array")

            # optional_recommendations must be an array
            opt = ui.get("optional_recommendations")
            if opt is not None and not isinstance(opt, list):
                errors.append(
                    "ui_summary.optional_recommendations must be an array"
                )

            # send_status must use communication-status vocabulary
            send = ui.get("send_status")
            if send is not None and send not in VALID_COMM_STATUS:
                errors.append(
                    f"Invalid ui_summary.send_status: '{send}'. "
                    f"Must be one of: {sorted(VALID_COMM_STATUS)}"
                )

    return errors


def assemble_pipeline_v1(attention_input, atlas, clio):
    """Deterministically assemble pipeline.v1; the model never creates it."""
    decision = atlas["attention_decision"]
    draft = clio.get("draft") or {}
    draft_available = isinstance(draft.get("body"), str) and bool(draft["body"].strip())
    communication_status = {
        "AUTO_HANDLE": "READY" if draft_available else "PLANNED",
        "APPROVAL_REQUIRED": "AWAITING_APPROVAL",
        "ESCALATE_NOW": "HELD",
    }[decision]
    plan = atlas["response_plan"]
    return {
        "schema_version": "pipeline.v1", "attention_decision": decision,
        "attention_score": atlas["attention_score"],
        "communication_status": communication_status,
        "ae_v1": atlas, "cl_v1": clio, "mie_v1": None,
        "ui_summary": {
            "route": decision, "attention_score": atlas["attention_score"],
            "headline": decision.replace("_", " ").title(),
            "why_founder_is_needed": plan["required_founder_decisions"],
            "required_decisions": plan["required_founder_decisions"],
            "optional_recommendations": plan["optional_recommendations"],
            "missing_information": plan["missing_information"],
            "draft_available": draft_available, "send_status": communication_status,
            "learning_update": None,
        },
    }


# ── persistence ──────────────────────────────────────────────────────

def persist_pipeline_run(message_id, validated_pipeline):
    """
    Persist a VALID pipeline.v1 response as a ``pipeline_runs`` row.

    Only call this when a real, validated pipeline response exists.
    Never call for mock, NOT_CONFIGURED, or invalid responses.

    Parameters
    ----------
    message_id : str
    validated_pipeline : dict
        A pipeline.v1 response that passed ``validate_pipeline_v1``.

    Returns
    -------
    dict | None
        The created pipeline_run row, or None on failure.
    """
    if not message_id or not validated_pipeline:
        return None

    ui_summary = validated_pipeline.get("ui_summary") or {}

    row = {
        "id": str(uuid.uuid4()),
        "message_id": message_id,
        "attention_decision": validated_pipeline.get("attention_decision"),
        "attention_score": validated_pipeline.get("attention_score"),
        "urgency": validated_pipeline.get("urgency"),
        "sentiment": validated_pipeline.get("sentiment"),
        "risk_severity": validated_pipeline.get("risk_severity"),
        "inference_confidence": validated_pipeline.get(
            "inference_confidence"
        ),
        "communication_status": validated_pipeline.get(
            "communication_status"
        ),
        "ui_summary": ui_summary,
        "ae_v1": validated_pipeline.get("ae_v1"),
        "cl_v1": validated_pipeline.get("cl_v1"),
        "pipeline_v1": validated_pipeline,
        "mie_v1": None,
        "created_at": _now_iso(),
    }

    # Strip None values to avoid inserting Python None into
    # columns that may have defaults.
    row = {k: v for k, v in row.items() if v is not None}

    try:
        resp = (
            supabase
            .table("pipeline_runs")
            .insert(row)
            .execute()
        )
        return resp.data[0] if resp.data else row
    except (KeyError, TypeError, AttributeError, ConnectionError):
        return None


# ── process message ──────────────────────────────────────────────────

def process_message(message_id, founder_id=None):
    """
    Full orchestration: load message, retrieve memory, construct
    input, then run Atlas and Clio.

    Returns
    -------
    dict
        {
            "status": "COMPLETE" or controlled agent failure,
            "pipeline_input": {...},
            "pipeline_run": {...} | None,
            "validation_errors": [...] | None,
        }
    """
    # 1. Load message from Supabase
    msg_resp = (
        supabase
        .table("messages")
        .select("*")
        .eq("id", message_id)
        .limit(1)
        .execute()
    )

    if not msg_resp.data:
        return {
            "status": "ERROR",
            "detail": "Message not found",
            "pipeline_input": None,
            "pipeline_run": None,
            "validation_errors": None,
        }

    message = msg_resp.data[0]

    # 2. Build pipeline input
    pipeline_input = build_pipeline_input(message, founder_id=founder_id)

    try:
        atlas, atlas_usage = run_atlas(pipeline_input)
    except GroqError as exc:
        return {"status": "ATLAS_FAILED", "detail": str(exc), "pipeline_input": pipeline_input, "pipeline_run": None, "validation_errors": None}
    validation_errors = validate_ae_v1(atlas)
    if validation_errors:
        return {"status": "ATLAS_VALIDATION_FAILED", "pipeline_input": pipeline_input, "pipeline_run": None, "validation_errors": validation_errors}
    try:
        clio, clio_usage = run_clio(pipeline_input, atlas)
    except GroqError as exc:
        return {"status": "CLIO_FAILED", "detail": str(exc), "pipeline_input": pipeline_input, "pipeline_run": None, "validation_errors": None}
    validation_errors = validate_cl_v1(clio)
    if validation_errors:
        return {"status": "CLIO_VALIDATION_FAILED", "pipeline_input": pipeline_input, "pipeline_run": None, "validation_errors": validation_errors}
    wb_result = assemble_pipeline_v1(pipeline_input, atlas, clio)
    validation_errors = validate_pipeline_v1(wb_result)

    if validation_errors:
        return {
            "status": "VALIDATION_FAILED",
            "pipeline_input": pipeline_input,
            "pipeline_run": None,
            "validation_errors": validation_errors,
        }

    pipeline_run = persist_pipeline_run(message_id, wb_result)

    return {
        "status": "COMPLETE",
        "pipeline_input": pipeline_input,
        "pipeline_run": pipeline_run,
        "validation_errors": None,
        "pipeline": wb_result,
        "usage": {"atlas": atlas_usage, "clio": clio_usage},
    }


# ── founder approval + draft editing ────────────────────────────────

def message_exists(message_id):
    resp = (
        supabase
        .table("messages")
        .select("id")
        .eq("id", message_id)
        .limit(1)
        .execute()
    )
    return bool(resp.data)


def get_message(message_id):
    """Return the full messages row for a message id, or None."""
    resp = (
        supabase
        .table("messages")
        .select("*")
        .eq("id", message_id)
        .limit(1)
        .execute()
    )
    return resp.data[0] if resp.data else None


def get_latest_pipeline_run(message_id):
    """Return the latest pipeline_runs row for a message, or None."""
    resp = (
        supabase
        .table("pipeline_runs")
        .select("*")
        .eq("message_id", message_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return resp.data[0] if resp.data else None


def get_current_draft(run):
    """Resolve the current working draft for a pipeline run.

    The founder-edited draft (stored in ui_summary.founder_draft) takes
    precedence over the original Clio draft (cl_v1.draft).  cl_v1 is never
    mutated, so the original AI draft stays intact for traceability.
    """
    ui = run.get("ui_summary") or {}
    founder_draft = ui.get("founder_draft")
    if isinstance(founder_draft, dict):
        return {
            "subject": founder_draft.get("subject") or "",
            "body": founder_draft.get("body") or "",
            "edited": bool(founder_draft.get("edited")),
            "source": "founder",
        }
    clio = run.get("cl_v1") or {}
    draft = clio.get("draft") or {}
    return {
        "subject": draft.get("subject") or "",
        "body": draft.get("body") or "",
        "edited": False,
        "source": "clio",
    }


def _update_run(run_id, fields):
    """Update a pipeline_runs row and return the updated row (or None)."""
    resp = (
        supabase
        .table("pipeline_runs")
        .update(fields)
        .eq("id", run_id)
        .execute()
    )
    return resp.data[0] if resp.data else None


def _require_approval_editable(message_id):
    """Load and validate that a message's current draft can be edited."""
    if not message_exists(message_id):
        raise ApprovalError("Message not found", 404)
    run = get_latest_pipeline_run(message_id)
    if not run:
        raise ApprovalError("No pipeline run for this message", 409)
    if run.get("attention_decision") != "APPROVAL_REQUIRED":
        raise ApprovalError(
            "Draft editing is only available for APPROVAL_REQUIRED messages",
            409,
        )
    if run.get("communication_status") != "AWAITING_APPROVAL":
        raise ApprovalError(
            "Draft can only be edited while awaiting approval", 409
        )
    return run


def update_working_draft(message_id, subject, body):
    """Persist a founder-edited draft without touching Atlas or Clio."""
    run = _require_approval_editable(message_id)

    body = (body or "").strip()
    if not body:
        raise ApprovalError("Draft body must be a non-empty string", 422)
    subject = (subject or "").strip()

    ui = dict(run.get("ui_summary") or {})
    ui["founder_draft"] = {
        "subject": subject,
        "body": body,
        "edited": True,
        "edited_at": _now_iso(),
    }

    updated = _update_run(run["id"], {"ui_summary": ui})
    saved_run = updated or {**run, "ui_summary": ui}

    return {
        "success": True,
        "message_id": message_id,
        "draft": {"subject": subject, "body": body},
        "communication_status": saved_run.get("communication_status"),
        "pipeline": saved_run,
    }


def _approval_response(message_id, run, ui, already_approved):
    current = get_current_draft(run)
    return {
        "success": True,
        "message_id": message_id,
        "attention_decision": run.get("attention_decision"),
        "communication_status": run.get("communication_status") or "READY",
        "draft": {
            "subject": current.get("subject") or "",
            "body": current.get("body") or "",
        },
        "draft_available": bool((current.get("body") or "").strip()),
        "send_status": ui.get("send_status") or "READY",
        "already_approved": already_approved,
        "pipeline": run,
    }


def approve_working_draft(message_id):
    """Approve the current draft and move AWAITING_APPROVAL -> READY.

    This changes only communication state; the historical Atlas decision
    (ae_v1 / attention_decision) is never mutated.
    """
    if not message_exists(message_id):
        raise ApprovalError("Message not found", 404)
    run = get_latest_pipeline_run(message_id)
    if not run:
        raise ApprovalError("No pipeline run for this message", 409)

    decision = run.get("attention_decision")
    comm = run.get("communication_status")
    ui = dict(run.get("ui_summary") or {})

    if decision == "ESCALATE_NOW":
        raise ApprovalError("Escalated messages cannot be approved", 409)
    if decision != "APPROVAL_REQUIRED":
        raise ApprovalError(
            "Only APPROVAL_REQUIRED messages can be approved", 409
        )
    if comm == "HELD":
        raise ApprovalError("Held messages cannot be approved", 409)
    if comm == "EXECUTED":
        raise ApprovalError("Already executed; cannot approve again", 409)

    if ui.get("approved") and comm == "READY":
        # Idempotent: already approved, return the current approved state.
        return _approval_response(message_id, run, ui, already_approved=True)

    if comm != "AWAITING_APPROVAL":
        raise ApprovalError(f"Cannot approve from status '{comm}'", 409)

    current = get_current_draft(run)
    if not (current.get("body") or "").strip():
        raise ApprovalError("No draft to approve", 409)

    ui["approved"] = True
    ui["approved_at"] = _now_iso()
    ui["send_status"] = "READY"

    updated = _update_run(
        run["id"],
        {"communication_status": "READY", "ui_summary": ui},
    )
    saved_run = updated or {
        **run,
        "communication_status": "READY",
        "ui_summary": ui,
    }

    return _approval_response(message_id, saved_run, ui, already_approved=False)


def _send_response(message_id, run, ui, already_sent):
    current = get_current_draft(run)
    return {
        "success": True,
        "message_id": message_id,
        "communication_status": run.get("communication_status") or "EXECUTED",
        "send_status": ui.get("send_status") or "EXECUTED",
        "draft": {
            "subject": current.get("subject") or "",
            "body": current.get("body") or "",
        },
        "sent_at": ui.get("sent_at"),
        "gmail_message_id": ui.get("gmail_message_id"),
        "gmail_thread_id": ui.get("gmail_thread_id"),
        "already_sent": already_sent,
        "pipeline": run,
    }


def send_approved_draft(message_id):
    """Manually send the current READY draft via Gmail.

    Only a message whose communication state is READY (and whose
    ui_summary.send_status is READY) with a non-empty current draft may
    be sent.  On Gmail success the state moves READY -> EXECUTED; on
    failure the draft stays available for manual retry.
    """
    message = get_message(message_id)
    if not message:
        raise ApprovalError("Message not found", 404)
    run = get_latest_pipeline_run(message_id)
    if not run:
        raise ApprovalError("No pipeline run for this message", 409)

    comm = run.get("communication_status")
    ui = dict(run.get("ui_summary") or {})
    send_status = ui.get("send_status")

    if comm == "EXECUTED" or send_status == "EXECUTED":
        # Idempotent: never send the same message twice.
        return _send_response(message_id, run, ui, already_sent=True)

    if comm != "READY":
        raise ApprovalError("Only READY messages can be sent", 409)
    if send_status != "READY":
        raise ApprovalError("Message is not cleared to send", 409)

    current = get_current_draft(run)
    body = (current.get("body") or "").strip()
    if not body:
        raise ApprovalError("No draft to send", 409)

    recipient = (message.get("sender_address") or "").strip()
    if not recipient:
        raise ApprovalError("No recipient address", 422)

    subject = (
        (current.get("subject") or "").strip()
        or (message.get("subject") or "").strip()
    )
    thread_id = message.get("thread_ref")

    try:
        sent = send_gmail_message(
            recipient, subject, body, thread_id=thread_id
        )
    except Exception as exc:
        # Keep READY so the founder can retry manually. Never leak tokens.
        raise ApprovalError(describe_gmail_error(exc), 502)

    ui["send_status"] = "EXECUTED"
    ui["sent_at"] = _now_iso()
    ui["gmail_message_id"] = sent.get("id")
    ui["gmail_thread_id"] = sent.get("threadId") or thread_id

    updated = _update_run(
        run["id"],
        {"communication_status": "EXECUTED", "ui_summary": ui},
    )
    saved_run = updated or {
        **run,
        "communication_status": "EXECUTED",
        "ui_summary": ui,
    }

    return _send_response(message_id, saved_run, ui, already_sent=False)
