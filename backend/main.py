import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from business_knowledge import (
    get_active_business_knowledge,
    get_relevant_business_knowledge,
)
from database import supabase
from gmail import fetch_recent_messages, import_recent_messages
from learning import process_founder_feedback
from pipeline import build_pipeline_input, build_attention_buddy_input, process_message
from pipeline import ApprovalError, update_working_draft, approve_working_draft, get_current_draft, send_approved_draft
from agents.atlas import run_atlas, validate_ae_v1
from agents.clio import run_clio, validate_cl_v1
from groq_client import GroqError
from preferences import get_active_preferences, get_relevant_preferences


app = FastAPI(
    title="Attention Buddy API",
    version="0.1.0"
)


# Allow React frontend during local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


VALID_DECISIONS = {"AUTO_HANDLE", "APPROVAL_REQUIRED", "ESCALATE_NOW"}
VALID_ACTION_TYPES = {"APPROVED", "EDITED", "OVERRIDDEN", "REJECTED", "DELEGATED"}
VALID_MEMORY_STATUSES = {"OBSERVED", "EMERGING", "ESTABLISHED", "EXPLICIT", "CONFLICTED"}
VALID_CONFIDENCE = {"low", "medium", "high"}


class FounderFeedbackRequest(BaseModel):
    pipeline_run_id: str
    original_decision: str
    final_decision: str
    action_type: str
    founder_explanation: Optional[str] = None
    original_draft: Optional[str] = None
    final_draft: Optional[str] = None
    apply_to_similar: bool = False


@app.get("/")
def root():
    return {
        "name": "Attention Buddy API",
        "status": "running"
    }


@app.get("/api/messages")
def get_messages():
    """
    Returns messages together with their latest pipeline run.
    """

    messages_response = (
        supabase
        .table("messages")
        .select("*")
        .order("created_at", desc=True)
        .execute()
    )

    messages = messages_response.data or []

    results = []

    for message in messages:

        pipeline_response = (
            supabase
            .table("pipeline_runs")
            .select(
                "id,"
                "attention_decision,"
                "attention_score,"
                "communication_status,"
                "ui_summary,"
                "cl_v1,"
                "created_at"
            )
            .eq("message_id", message["id"])
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )

        pipeline_runs = pipeline_response.data or []

        latest_pipeline = (
            pipeline_runs[0]
            if pipeline_runs
            else None
        )

        results.append({
            "id": message["id"],
            "channel": message.get("channel"),
            "external_id": message.get("external_id"),
            "thread_ref": message.get("thread_ref"),
            "sender_name": message.get("sender_name"),
            "sender_address": message.get("sender_address"),
            "subject": message.get("subject"),
            "body_verbatim": message.get("body_verbatim"),
            "received_at": message.get("received_at"),
            "processing_status": message.get(
                "processing_status"
            ),
            "pipeline": latest_pipeline
        })

    return {
        "count": len(results),
        "messages": results
    }


@app.get("/api/messages/{message_id}")
def get_message(message_id: str):

    message_response = (
        supabase
        .table("messages")
        .select("*")
        .eq("id", message_id)
        .limit(1)
        .execute()
    )

    messages = message_response.data or []

    if not messages:
        raise HTTPException(
            status_code=404,
            detail="Message not found"
        )

    message = messages[0]

    pipeline_response = (
        supabase
        .table("pipeline_runs")
        .select("*")
        .eq("message_id", message_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    pipeline_runs = pipeline_response.data or []

    pipeline = pipeline_runs[0] if pipeline_runs else None
    draft = None
    approval = None
    if pipeline:
        current = get_current_draft(pipeline)
        draft = {"subject": current.get("subject"), "body": current.get("body"), "edited": current.get("edited")}
        approval = {"required": pipeline.get("attention_decision") == "APPROVAL_REQUIRED" and pipeline.get("communication_status") == "AWAITING_APPROVAL", "completed": bool((pipeline.get("ui_summary") or {}).get("approved")), "attention_decision": pipeline.get("attention_decision"), "communication_status": pipeline.get("communication_status")}
    return {
        "message": message,
        "pipeline": pipeline,
        "draft": draft,
        "approval": approval,
    }


@app.get("/api/dashboard/summary")
def get_dashboard_summary():

    response = (
        supabase
        .table("pipeline_runs")
        .select(
            "attention_decision,"
            "attention_score,"
            "communication_status"
        )
        .execute()
    )

    runs = response.data or []

    summary = {
        "total": len(runs),
        "auto_handled": 0,
        "approval_required": 0,
        "escalated": 0
    }

    for run in runs:
        decision = run.get("attention_decision")

        if decision == "AUTO_HANDLE":
            summary["auto_handled"] += 1

        elif decision == "APPROVAL_REQUIRED":
            summary["approval_required"] += 1

        elif decision == "ESCALATE_NOW":
            summary["escalated"] += 1

    return summary


@app.post("/api/feedback")
def create_feedback(payload: FounderFeedbackRequest):

    # Validate controlled vocabulary for decisions
    if payload.original_decision not in VALID_DECISIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid original_decision: '{payload.original_decision}'. "
                f"Must be one of: {', '.join(sorted(VALID_DECISIONS))}"
            )
        )

    if payload.final_decision not in VALID_DECISIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid final_decision: '{payload.final_decision}'. "
                f"Must be one of: {', '.join(sorted(VALID_DECISIONS))}"
            )
        )

    # Validate controlled vocabulary for action type
    if payload.action_type not in VALID_ACTION_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid action_type: '{payload.action_type}'. "
                f"Must be one of: {', '.join(sorted(VALID_ACTION_TYPES))}"
            )
        )

    # Verify pipeline_run_id exists
    pipeline_check = (
        supabase
        .table("pipeline_runs")
        .select("id")
        .eq("id", payload.pipeline_run_id)
        .limit(1)
        .execute()
    )

    if not pipeline_check.data:
        raise HTTPException(
            status_code=404,
            detail="Pipeline run not found"
        )

    # Insert feedback
    feedback_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    response = (
        supabase
        .table("founder_feedback")
        .insert({
            "id": feedback_id,
            "pipeline_run_id": payload.pipeline_run_id,
            "original_decision": payload.original_decision,
            "final_decision": payload.final_decision,
            "action_type": payload.action_type,
            "founder_explanation": payload.founder_explanation,
            "original_draft": payload.original_draft,
            "final_draft": payload.final_draft,
            "apply_to_similar": payload.apply_to_similar,
            "created_at": now,
        })
        .execute()
    )

    feedback_row = response.data[0] if response.data else None

    # Process learning from this feedback (only if insert succeeded)
    learning_result = {}
    if feedback_row:
        learning_result = process_founder_feedback(feedback_row)

    return {
        "success": True,
        "feedback": feedback_row,
        "learning_event": learning_result.get("learning_event"),
        "preference_update": learning_result.get("preference_update"),
    }


@app.get("/api/learning-events")
def get_learning_events():
    response = (
        supabase
        .table("learning_events")
        .select("*")
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )

    return {
        "count": len(response.data or []),
        "events": response.data or [],
    }


@app.get("/api/preferences")
def get_preferences():
    response = (
        supabase
        .table("founder_preferences")
        .select("*")
        .eq("active", True)
        .execute()
    )

    prefs = response.data or []

    # Sort: EXPLICIT > ESTABLISHED > EMERGING > OBSERVED > CONFLICTED
    status_order = {
        "EXPLICIT": 0,
        "ESTABLISHED": 1,
        "EMERGING": 2,
        "OBSERVED": 3,
        "CONFLICTED": 4,
    }

    prefs.sort(
        key=lambda p: p.get("last_reinforced_at") or p.get("created_at") or "",
        reverse=True,
    )
    prefs.sort(
        key=lambda p: status_order.get(p.get("memory_status"), 99)
    )

    return {
        "count": len(prefs),
        "preferences": prefs,
    }


class PreferencesRelevantRequest(BaseModel):
    founder_id: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    sender: Optional[str] = None
    channel: Optional[str] = None
    intent: Optional[str] = None
    product_service: Optional[str] = None
    limit: int = 7


class PipelinePreviewRequest(BaseModel):
    message_id: str
    founder_id: Optional[str] = None


class PipelineProcessRequest(BaseModel):
    message_id: str
    founder_id: Optional[str] = None


class DraftUpdateRequest(BaseModel):
    subject: Optional[str] = None
    body: str


@app.get("/api/preferences/active")
def get_active_preferences_endpoint(founder_id: Optional[str] = None):
    """
    Return only live-routing-eligible preferences.

    Safety gate: active=True AND memory_status IN ('ESTABLISHED','EXPLICIT').
    """
    prefs = get_active_preferences(founder_id=founder_id)

    # Compact response — only fields needed for reasoning
    compact = []
    for p in prefs:
        compact.append({
            "id": p.get("id"),
            "rule": p.get("rule"),
            "scope": p.get("scope"),
            "memory_status": p.get("memory_status"),
            "confidence": p.get("confidence"),
            "supporting_observations": p.get("supporting_observations"),
            "active": p.get("active"),
            "last_reinforced_at": p.get("last_reinforced_at"),
        })

    return {
        "count": len(compact),
        "preferences": compact,
    }


@app.post("/api/preferences/relevant")
def get_relevant_preferences_endpoint(payload: PreferencesRelevantRequest):
    """
    Return active preferences ranked by relevance to the given message
    context.  Only ESTABLISHED and EXPLICIT preferences with active=True
    are eligible.
    """
    message_context = {
        "subject": payload.subject,
        "body": payload.body,
        "sender": payload.sender,
        "channel": payload.channel,
        "intent": payload.intent,
        "product_service": payload.product_service,
    }

    # Remove None values so the scoring function doesn't see "None" strings
    message_context = {k: v for k, v in message_context.items() if v is not None}

    return get_relevant_preferences(
        message_context=message_context,
        founder_id=payload.founder_id,
        limit=payload.limit,
    )


# ── Business Knowledge endpoints ────────────────────────────────────


class BusinessKnowledgeRelevantRequest(BaseModel):
    founder_id: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    limit: int = 5


def _compact_business_knowledge(record):
    """
    Compact a raw business_knowledge row for the read-only API/frontend.
    Never exposes internal DB metadata (created_at, updated_at, etc.).
    """
    return {
        "knowledge_id": record.get("id"),
        "category": record.get("category"),
        "title": record.get("title"),
        "content": record.get("content"),
        "priority": record.get("priority"),
        "source_type": record.get("source_type"),
        "source_reference": record.get("source_reference"),
        "keywords": record.get("keywords") or [],
    }


@app.get("/api/business-knowledge")
def get_business_knowledge(founder_id: Optional[str] = None):
    """
    Return active Business Knowledge for the current demo/null founder
    context.  Respects active flag, effective dates, and founder
    isolation (mirrors the founder-preference layer).
    """
    records = get_active_business_knowledge(founder_id=founder_id)
    knowledge = [_compact_business_knowledge(r) for r in records]

    return {
        "count": len(knowledge),
        "knowledge": knowledge,
    }


@app.post("/api/business-knowledge/relevant")
def get_relevant_business_knowledge_endpoint(
    payload: BusinessKnowledgeRelevantRequest,
):
    """
    Return active Business Knowledge ranked by deterministic relevance to
    the given message subject/body.
    """
    if payload.limit < 1:
        raise HTTPException(
            status_code=400,
            detail="limit must be >= 1",
        )

    return get_relevant_business_knowledge(
        subject=payload.subject or "",
        body=payload.body or "",
        founder_id=payload.founder_id,
        limit=payload.limit,
    )


# ── Pipeline endpoints ───────────────────────────────────────────────


@app.post("/api/pipeline/preview")
def pipeline_preview(payload: PipelinePreviewRequest):
    """
    Preview the full ``attention_buddy_input.v1`` payload for a message.

    Does NOT call WorkBuddy.  Returns the message, founder memory
    context, and business-context placeholder that will be sent when
    processing is invoked.
    """
    # Verify message exists
    msg_resp = (
        supabase
        .table("messages")
        .select("*")
        .eq("id", payload.message_id)
        .limit(1)
        .execute()
    )

    if not msg_resp.data:
        raise HTTPException(status_code=404, detail="Message not found")

    pipeline_input = build_pipeline_input(
        msg_resp.data[0],
        founder_id=payload.founder_id,
    )

    return pipeline_input


@app.post("/api/pipeline/process")
def pipeline_process(payload: PipelineProcessRequest):
    """
    Full pipeline: load message, retrieve memory, construct input, then
    run Atlas and Clio. Invalid or failed agent outputs are never persisted.
    """
    result = process_message(
        payload.message_id,
        founder_id=payload.founder_id,
    )

    if result["status"] == "ERROR":
        raise HTTPException(
            status_code=404,
            detail=result.get("detail", "Message not found"),
        )

    return result


@app.post("/api/groq/atlas-test")
def atlas_test(payload: PipelinePreviewRequest):
    """Non-persistent Atlas diagnostic; it does not alter message state."""
    try:
        attention_input = build_attention_buddy_input(payload.message_id, payload.founder_id)
        atlas, usage = run_atlas(attention_input)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except GroqError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    errors = validate_ae_v1(atlas)
    if errors:
        raise HTTPException(status_code=502, detail={"validation_errors": errors})
    return {"attention_input": attention_input, "ae_v1": atlas, "usage": usage}


@app.post("/api/groq/clio-test")
def clio_test(payload: PipelinePreviewRequest):
    """Non-persistent Atlas + Clio diagnostic."""
    try:
        attention_input = build_attention_buddy_input(payload.message_id, payload.founder_id)
        atlas, atlas_usage = run_atlas(attention_input)
        errors = validate_ae_v1(atlas)
        if errors:
            raise HTTPException(status_code=502, detail={"atlas_validation_errors": errors})
        clio, clio_usage = run_clio(attention_input, atlas)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except GroqError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    errors = validate_cl_v1(clio)
    if errors:
        raise HTTPException(status_code=502, detail={"clio_validation_errors": errors})
    return {"attention_input": attention_input, "ae_v1": atlas, "cl_v1": clio, "usage": {"atlas": atlas_usage, "clio": clio_usage}}


@app.patch("/api/messages/{message_id}/draft")
def update_draft(message_id: str, payload: DraftUpdateRequest):
    """Persist a founder-edited draft without rerunning Atlas or Clio."""
    try:
        result = update_working_draft(message_id, payload.subject, payload.body)
    except ApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return result


@app.post("/api/messages/{message_id}/approve")
def approve_draft(message_id: str):
    """Approve the current draft: AWAITING_APPROVAL -> READY."""
    try:
        result = approve_working_draft(message_id)
    except ApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return result


@app.post("/api/messages/{message_id}/send")
def send_draft(message_id: str):
    """Manually send the current approved/ready draft via Gmail."""
    try:
        result = send_approved_draft(message_id)
    except ApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return result


# ── Temporary Gmail read-only test endpoint ──────────────────────────


@app.get("/api/gmail/test")
def gmail_test():
    """
    TEMPORARY read-only Gmail test endpoint (Phase 1).

    Fetches up to 5 recent Gmail messages via gmail.readonly only.
    Does not import, classify, or send anything.
    """
    try:
        messages = fetch_recent_messages(limit=5)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "count": len(messages),
        "messages": messages,
    }


@app.post("/api/gmail/sync")
def gmail_sync(limit: int = 10):
    """
    TEMPORARY Gmail -> Supabase ingestion endpoint (Phase 2).

    Reads recent Gmail messages (read-only) and inserts unseen ones into
    the messages table with channel="email" and processing_status="PENDING".
    Does NOT auto-process or call WorkBuddy.
    """
    return import_recent_messages(limit)
