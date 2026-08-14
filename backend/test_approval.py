"""Founder approval + draft editing tests (mocked Supabase, no live Groq/Gmail)."""
import unittest
from unittest.mock import patch

import pipeline
from pipeline import (
    ApprovalError,
    update_working_draft,
    approve_working_draft,
    get_current_draft,
    get_latest_pipeline_run,
)


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeBuilder:
    def __init__(self, supabase, table_name):
        self.supabase = supabase
        self.table_name = table_name
        self._filters = {}
        self._update_fields = None
        self._limit = None

    def select(self, *_cols):
        return self

    def eq(self, column, value):
        self._filters[column] = value
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def update(self, fields):
        self._update_fields = fields
        return self

    def execute(self):
        if self._update_fields is not None:
            rows = self.supabase.pipeline_runs
            for row in rows:
                if all(row.get(c) == v for c, v in self._filters.items()):
                    row.update(self._update_fields)
                    self.supabase.last_update = (row["id"], dict(self._update_fields))
                    return FakeResponse([row])
            return FakeResponse([])

        rows = (
            self.supabase.messages
            if self.table_name == "messages"
            else self.supabase.pipeline_runs
        )
        out = [
            row
            for row in rows
            if all(row.get(c) == v for c, v in self._filters.items())
        ]
        if self._limit:
            out = out[: self._limit]
        return FakeResponse(out)


class FakeSupabase:
    def __init__(self, messages=None, pipeline_runs=None):
        self.messages = messages or []
        self.pipeline_runs = pipeline_runs or []
        self.last_update = None

    def table(self, name):
        return FakeBuilder(self, name)


def make_run(
    decision="APPROVAL_REQUIRED",
    status="AWAITING_APPROVAL",
    draft_body="AI prepared draft",
    founder_draft=None,
    approved=False,
    message_id="m1",
    run_id="run-1",
):
    cl_v1 = {
        "schema_version": "cl.v1",
        "action": "DRAFT" if draft_body else "HOLD",
        "draft": {"subject": "Re: Hello", "body": draft_body},
        "grounding": {"business_rules_used": [], "founder_preferences_used": []},
        "approval_required": decision == "APPROVAL_REQUIRED",
        "unresolved_items": [],
    }
    ui = {
        "route": decision,
        "attention_score": 0.6,
        "draft_available": bool(draft_body),
        "send_status": status,
    }
    if founder_draft is not None:
        ui["founder_draft"] = founder_draft
    if approved:
        ui["approved"] = True
        ui["approved_at"] = "2026-08-14T10:00:00+00:00"
    return {
        "id": run_id,
        "message_id": message_id,
        "attention_decision": decision,
        "attention_score": 0.6,
        "communication_status": status,
        "ae_v1": {"schema_version": "ae.v1", "attention_decision": decision},
        "cl_v1": cl_v1,
        "ui_summary": ui,
        "created_at": "2026-08-14T00:00:00+00:00",
    }


def _supabase(run=None, message_exists=True):
    runs = [run] if run else []
    messages = [{"id": run["message_id"]}] if (run and message_exists) else []
    if run and not message_exists:
        messages = []
    return FakeSupabase(messages=messages, pipeline_runs=runs)


class DraftEditingTests(unittest.TestCase):
    def test_edit_persists_founder_draft(self):
        sup = _supabase(run=make_run())
        with patch("pipeline.supabase", sup):
            result = update_working_draft("m1", "Updated subject", "Founder-edited response")
        self.assertTrue(result["success"])
        fd = sup.pipeline_runs[0]["ui_summary"]["founder_draft"]
        self.assertEqual(fd["subject"], "Updated subject")
        self.assertEqual(fd["body"], "Founder-edited response")
        self.assertTrue(fd["edited"])

    def test_edit_does_not_approve(self):
        sup = _supabase(run=make_run())
        with patch("pipeline.supabase", sup):
            result = update_working_draft("m1", "S", "B")
        self.assertEqual(result["communication_status"], "AWAITING_APPROVAL")
        self.assertNotIn("approved", sup.pipeline_runs[0]["ui_summary"])

    def test_edit_rejects_empty_body(self):
        sup = _supabase(run=make_run())
        with patch("pipeline.supabase", sup):
            with self.assertRaises(ApprovalError):
                update_working_draft("m1", "S", "   ")

    def test_edit_rejects_unknown_message(self):
        sup = _supabase(run=make_run(), message_exists=False)
        with patch("pipeline.supabase", sup):
            with self.assertRaises(ApprovalError) as ctx:
                update_working_draft("m1", "S", "B")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_edit_rejects_non_approval_required(self):
        sup = _supabase(run=make_run(decision="AUTO_HANDLE", status="READY"))
        with patch("pipeline.supabase", sup):
            with self.assertRaises(ApprovalError):
                update_working_draft("m1", "S", "B")

    def test_edit_rejects_after_approval(self):
        sup = _supabase(run=make_run(status="READY", approved=True))
        with patch("pipeline.supabase", sup):
            with self.assertRaises(ApprovalError):
                update_working_draft("m1", "S", "B")

    def test_edit_does_not_call_groq_or_agents(self):
        sup = _supabase(run=make_run())
        with patch("pipeline.supabase", sup), \
             patch("pipeline.run_atlas") as atlas, \
             patch("pipeline.run_clio") as clio, \
             patch("groq_client.generate_json_with_usage") as groq:
            update_working_draft("m1", "S", "B")
        atlas.assert_not_called()
        clio.assert_not_called()
        groq.assert_not_called()


class ApprovalTests(unittest.TestCase):
    def test_approve_transitions_to_ready(self):
        sup = _supabase(run=make_run())
        with patch("pipeline.supabase", sup):
            result = approve_working_draft("m1")
        self.assertTrue(result["success"])
        self.assertEqual(result["communication_status"], "READY")
        self.assertEqual(result["send_status"], "READY")
        self.assertEqual(sup.pipeline_runs[0]["communication_status"], "READY")

    def test_approve_preserves_attention_decision_and_ae_v1(self):
        run = make_run()
        original_ae = dict(run["ae_v1"])
        sup = _supabase(run=run)
        with patch("pipeline.supabase", sup):
            result = approve_working_draft("m1")
        self.assertEqual(result["attention_decision"], "APPROVAL_REQUIRED")
        self.assertEqual(
            sup.pipeline_runs[0]["attention_decision"], "APPROVAL_REQUIRED"
        )
        self.assertEqual(sup.pipeline_runs[0]["ae_v1"], original_ae)

    def test_approve_preserves_edited_draft(self):
        founder_draft = {
            "subject": "Edited",
            "body": "Edited body",
            "edited": True,
            "edited_at": "now",
        }
        sup = _supabase(run=make_run(founder_draft=founder_draft))
        with patch("pipeline.supabase", sup):
            result = approve_working_draft("m1")
        self.assertEqual(result["draft"]["body"], "Edited body")
        ui = sup.pipeline_runs[0]["ui_summary"]
        self.assertTrue(ui["approved"])
        self.assertEqual(ui["founder_draft"]["body"], "Edited body")

    def test_approve_without_draft_rejected(self):
        sup = _supabase(run=make_run(draft_body=""))
        with patch("pipeline.supabase", sup):
            with self.assertRaises(ApprovalError):
                approve_working_draft("m1")

    def test_escalate_now_cannot_approve(self):
        sup = _supabase(run=make_run(decision="ESCALATE_NOW", status="HELD"))
        with patch("pipeline.supabase", sup):
            with self.assertRaises(ApprovalError):
                approve_working_draft("m1")

    def test_held_cannot_approve(self):
        sup = _supabase(run=make_run(status="HELD"))
        with patch("pipeline.supabase", sup):
            with self.assertRaises(ApprovalError):
                approve_working_draft("m1")

    def test_executed_cannot_approve(self):
        sup = _supabase(run=make_run(status="EXECUTED"))
        with patch("pipeline.supabase", sup):
            with self.assertRaises(ApprovalError):
                approve_working_draft("m1")

    def test_auto_handle_cannot_approve(self):
        sup = _supabase(run=make_run(decision="AUTO_HANDLE", status="READY"))
        with patch("pipeline.supabase", sup):
            with self.assertRaises(ApprovalError):
                approve_working_draft("m1")

    def test_unknown_message_rejected(self):
        sup = _supabase(run=make_run(), message_exists=False)
        with patch("pipeline.supabase", sup):
            with self.assertRaises(ApprovalError) as ctx:
                approve_working_draft("m1")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_double_approval_is_idempotent(self):
        sup = _supabase(run=make_run())
        with patch("pipeline.supabase", sup):
            first = approve_working_draft("m1")
            second = approve_working_draft("m1")
        self.assertFalse(first["already_approved"])
        self.assertTrue(second["already_approved"])
        self.assertEqual(second["communication_status"], "READY")
        # Only one pipeline run row exists (no duplicates created)
        self.assertEqual(len(sup.pipeline_runs), 1)

    def test_approve_does_not_call_groq_or_agents(self):
        sup = _supabase(run=make_run())
        with patch("pipeline.supabase", sup), \
             patch("pipeline.run_atlas") as atlas, \
             patch("pipeline.run_clio") as clio, \
             patch("groq_client.generate_json_with_usage") as groq:
            approve_working_draft("m1")
        atlas.assert_not_called()
        clio.assert_not_called()
        groq.assert_not_called()


class CurrentDraftTests(unittest.TestCase):
    def test_founder_draft_takes_precedence(self):
        run = make_run(founder_draft={"subject": "E", "body": "Edited", "edited": True})
        draft = get_current_draft(run)
        self.assertEqual(draft["body"], "Edited")
        self.assertTrue(draft["edited"])
        self.assertEqual(draft["source"], "founder")

    def test_falls_back_to_clio_draft(self):
        run = make_run()
        draft = get_current_draft(run)
        self.assertEqual(draft["body"], "AI prepared draft")
        self.assertFalse(draft["edited"])
        self.assertEqual(draft["source"], "clio")

    def test_get_latest_pipeline_run(self):
        sup = _supabase(run=make_run())
        with patch("pipeline.supabase", sup):
            run = get_latest_pipeline_run("m1")
        self.assertEqual(run["id"], "run-1")


class MessageDetailTests(unittest.TestCase):
    def test_get_message_returns_edited_draft_and_approval(self):
        founder_draft = {"subject": "E", "body": "Edited body", "edited": True}
        run = make_run(founder_draft=founder_draft, approved=True, status="READY")
        sup = _supabase(run=run)
        with patch("main.supabase", sup):
            import main
            result = main.get_message("m1")
        self.assertEqual(result["draft"]["body"], "Edited body")
        self.assertTrue(result["draft"]["edited"])
        self.assertTrue(result["approval"]["completed"])
        self.assertEqual(result["approval"]["communication_status"], "READY")
        self.assertEqual(result["approval"]["attention_decision"], "APPROVAL_REQUIRED")

    def test_get_message_approval_required_pending(self):
        run = make_run()
        sup = _supabase(run=run)
        with patch("main.supabase", sup):
            import main
            result = main.get_message("m1")
        self.assertTrue(result["approval"]["required"])
        self.assertFalse(result["approval"]["completed"])


if __name__ == "__main__":
    unittest.main()
