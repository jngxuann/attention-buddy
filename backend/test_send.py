"""Gmail send tests (mocked Supabase + Gmail; zero live API/Groq calls)."""
import unittest
from unittest.mock import patch

from pipeline import ApprovalError, send_approved_draft


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
            for row in self.supabase.pipeline_runs:
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


def make_message(
    message_id="m1",
    sender_address="jamie@example.com",
    subject="Saturday collection",
    thread_ref="thread-123",
):
    return {
        "id": message_id,
        "channel": "email",
        "external_id": "gmail-msg-123",
        "thread_ref": thread_ref,
        "sender_name": "Jamie",
        "sender_address": sender_address,
        "subject": subject,
        "body_verbatim": "Hi there",
        "received_at": "2026-08-14T00:00:00+00:00",
        "processing_status": "PROCESSED",
    }


def make_run(
    message_id="m1",
    decision="AUTO_HANDLE",
    status="READY",
    draft_body="AI prepared draft",
    founder_draft=None,
    send_status="READY",
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
        "attention_score": 0.5,
        "draft_available": bool(draft_body),
        "send_status": send_status,
    }
    if founder_draft is not None:
        ui["founder_draft"] = founder_draft
    return {
        "id": run_id,
        "message_id": message_id,
        "attention_decision": decision,
        "attention_score": 0.5,
        "communication_status": status,
        "ae_v1": {"schema_version": "ae.v1", "attention_decision": decision},
        "cl_v1": cl_v1,
        "ui_summary": ui,
        "created_at": "2026-08-14T00:00:00+00:00",
    }


def _supabase(message=None, run=None):
    return FakeSupabase(
        messages=[message] if message else [],
        pipeline_runs=[run] if run else [],
    )


SENT = {"id": "sent-gmail-1", "threadId": "thread-123"}


class SendGatesTests(unittest.TestCase):
    def test_ready_with_draft_can_send(self):
        sup = _supabase(message=make_message(), run=make_run())
        with patch("pipeline.supabase", sup), \
             patch("pipeline.send_gmail_message", return_value=SENT):
            result = send_approved_draft("m1")
        self.assertTrue(result["success"])
        self.assertEqual(result["communication_status"], "EXECUTED")

    def test_send_uses_correct_recipient_subject_body(self):
        sup = _supabase(message=make_message(sender_address="jamie@example.com"), run=make_run())
        with patch("pipeline.supabase", sup), \
             patch("pipeline.send_gmail_message", return_value=SENT) as send:
            send_approved_draft("m1")
        args, kwargs = send.call_args
        self.assertEqual(args[0], "jamie@example.com")  # recipient
        self.assertEqual(args[1], "Re: Hello")           # subject
        self.assertEqual(args[2], "AI prepared draft")   # body

    def test_founder_edited_draft_takes_precedence(self):
        founder_draft = {
            "subject": "Edited subject",
            "body": "Founder edited body",
            "edited": True,
            "edited_at": "now",
        }
        sup = _supabase(
            message=make_message(),
            run=make_run(founder_draft=founder_draft),
        )
        with patch("pipeline.supabase", sup), \
             patch("pipeline.send_gmail_message", return_value=SENT) as send:
            send_approved_draft("m1")
        args, _ = send.call_args
        self.assertEqual(args[1], "Edited subject")
        self.assertEqual(args[2], "Founder edited body")

    def test_success_transitions_ready_to_executed(self):
        sup = _supabase(message=make_message(), run=make_run())
        with patch("pipeline.supabase", sup), \
             patch("pipeline.send_gmail_message", return_value=SENT):
            send_approved_draft("m1")
        row = sup.pipeline_runs[0]
        self.assertEqual(row["communication_status"], "EXECUTED")
        self.assertEqual(row["ui_summary"]["send_status"], "EXECUTED")
        self.assertEqual(row["ui_summary"]["gmail_message_id"], "sent-gmail-1")
        self.assertEqual(row["ui_summary"]["gmail_thread_id"], "thread-123")

    def test_threading_preserved(self):
        sup = _supabase(
            message=make_message(thread_ref="thread-123"),
            run=make_run(),
        )
        with patch("pipeline.supabase", sup), \
             patch("pipeline.send_gmail_message", return_value=SENT) as send:
            send_approved_draft("m1")
        self.assertEqual(send.call_args.kwargs.get("thread_id"), "thread-123")

    def test_gmail_failure_does_not_mark_executed(self):
        sup = _supabase(message=make_message(), run=make_run())
        with patch("pipeline.supabase", sup), \
             patch("pipeline.send_gmail_message", side_effect=Exception("boom")):
            with self.assertRaises(ApprovalError) as ctx:
                send_approved_draft("m1")
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(sup.pipeline_runs[0]["communication_status"], "READY")
        self.assertEqual(sup.pipeline_runs[0]["ui_summary"]["send_status"], "READY")

    def test_awaiting_approval_cannot_send(self):
        sup = _supabase(
            message=make_message(),
            run=make_run(decision="APPROVAL_REQUIRED", status="AWAITING_APPROVAL", send_status="AWAITING_APPROVAL"),
        )
        with patch("pipeline.supabase", sup), \
             patch("pipeline.send_gmail_message") as send:
            with self.assertRaises(ApprovalError):
                send_approved_draft("m1")
        send.assert_not_called()

    def test_held_cannot_send(self):
        sup = _supabase(
            message=make_message(),
            run=make_run(decision="ESCALATE_NOW", status="HELD", send_status="HELD"),
        )
        with patch("pipeline.supabase", sup), \
             patch("pipeline.send_gmail_message") as send:
            with self.assertRaises(ApprovalError):
                send_approved_draft("m1")
        send.assert_not_called()

    def test_escalate_now_cannot_bypass_held(self):
        # Even if send_status were READY, communication_status HELD blocks send.
        sup = _supabase(
            message=make_message(),
            run=make_run(decision="ESCALATE_NOW", status="HELD", send_status="READY"),
        )
        with patch("pipeline.supabase", sup), \
             patch("pipeline.send_gmail_message") as send:
            with self.assertRaises(ApprovalError):
                send_approved_draft("m1")
        send.assert_not_called()

    def test_executed_cannot_send_twice(self):
        run = make_run(status="EXECUTED", send_status="EXECUTED")
        sup = _supabase(message=make_message(), run=run)
        with patch("pipeline.supabase", sup), \
             patch("pipeline.send_gmail_message") as send:
            result = send_approved_draft("m1")
        self.assertTrue(result["already_sent"])
        send.assert_not_called()

    def test_missing_draft_cannot_send(self):
        sup = _supabase(
            message=make_message(),
            run=make_run(draft_body="", send_status="READY"),
        )
        with patch("pipeline.supabase", sup), \
             patch("pipeline.send_gmail_message") as send:
            with self.assertRaises(ApprovalError):
                send_approved_draft("m1")
        send.assert_not_called()

    def test_empty_draft_body_cannot_send(self):
        founder_draft = {"subject": "S", "body": "   ", "edited": True}
        sup = _supabase(
            message=make_message(),
            run=make_run(founder_draft=founder_draft, send_status="READY"),
        )
        with patch("pipeline.supabase", sup), \
             patch("pipeline.send_gmail_message") as send:
            with self.assertRaises(ApprovalError):
                send_approved_draft("m1")
        send.assert_not_called()

    def test_unknown_message_handled_cleanly(self):
        sup = _supabase(message=None, run=None)
        with patch("pipeline.supabase", sup), \
             patch("pipeline.send_gmail_message") as send:
            with self.assertRaises(ApprovalError) as ctx:
                send_approved_draft("missing")
        self.assertEqual(ctx.exception.status_code, 404)
        send.assert_not_called()

    def test_send_status_not_ready_blocks(self):
        sup = _supabase(
            message=make_message(),
            run=make_run(status="READY", send_status="PLANNED"),
        )
        with patch("pipeline.supabase", sup), \
             patch("pipeline.send_gmail_message") as send:
            with self.assertRaises(ApprovalError):
                send_approved_draft("m1")
        send.assert_not_called()


class SendNoExternalCallsTests(unittest.TestCase):
    def test_send_does_not_call_atlas_clio_or_groq(self):
        sup = _supabase(message=make_message(), run=make_run())
        with patch("pipeline.supabase", sup), \
             patch("pipeline.send_gmail_message", return_value=SENT), \
             patch("pipeline.run_atlas") as atlas, \
             patch("pipeline.run_clio") as clio, \
             patch("groq_client.generate_json_with_usage") as groq, \
             patch("gmail.get_gmail_service") as svc:
            send_approved_draft("m1")
        atlas.assert_not_called()
        clio.assert_not_called()
        groq.assert_not_called()
        svc.assert_not_called()  # Gmail service never built during send


if __name__ == "__main__":
    unittest.main()
