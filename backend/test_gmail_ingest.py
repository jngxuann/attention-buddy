"""
Unit tests for Gmail -> Supabase ingestion (Phase 2).

Covers:
  - new message insert (channel=email, processing_status=PENDING)
  - duplicate Gmail message skipped on second sync
  - same content, different Gmail IDs -> both import
  - same thread, different Gmail IDs -> both import
  - internal_date -> UTC ISO conversion
  - missing internal_date -> received_at None
  - missing sender name -> sender_name None
  - missing thread id -> thread_ref None
  - processing_status always PENDING
  - no pipeline_run created, no WorkBuddy call, no Gmail write method
  - limit clamped to 1..50
  - one failed import does not abort the others

All Supabase and Gmail access is mocked; no real credentials are used.
"""

import unittest
from unittest.mock import MagicMock, patch

import gmail
from postgrest.exceptions import APIError


def _resp(data):
    m = MagicMock()
    m.data = data
    return m


def _make_supabase_mock(select_rows=None, insert_row=None, insert_exc=None):
    """Return a mock Supabase client supporting the two call shapes used
    by ingestion: select(id).eq(...).eq(...).limit(1).execute() and
    insert(row).execute()."""
    client = MagicMock()
    table = client.table.return_value

    sel = MagicMock()
    sel.eq.return_value = sel
    sel.limit.return_value = sel
    sel.execute.return_value = _resp(select_rows or [])
    table.select.return_value = sel

    ins = MagicMock()
    if insert_exc is not None:
        ins.execute.side_effect = insert_exc
    else:
        ins.execute.return_value = _resp([insert_row] if insert_row else [])
    table.insert.return_value = ins

    return client


def _msg(**overrides):
    msg = {
        "gmail_message_id": "gm-1",
        "gmail_thread_id": "gt-1",
        "sender_name": "jingxuan",
        "sender_address": "jngxuann@gmail.com",
        "subject": "Corporate gift box enquiry",
        "body_verbatim": "We need 150 customised gift boxes with our company logo.",
        "internal_date": "1786618526000",
        "gmail_date_header": "Thu, 13 Aug 2026 18:55:26 +0800",
    }
    msg.update(overrides)
    return msg


class InternalDateConversionTests(unittest.TestCase):
    def test_internal_date_to_utc_iso(self):
        self.assertEqual(
            gmail._internal_date_to_iso("1786618526000"),
            "2026-08-13T10:55:26+00:00",
        )

    def test_internal_date_accepts_int(self):
        self.assertEqual(
            gmail._internal_date_to_iso(1786618526000),
            "2026-08-13T10:55:26+00:00",
        )

    def test_missing_internal_date_is_none(self):
        self.assertIsNone(gmail._internal_date_to_iso(None))
        self.assertIsNone(gmail._internal_date_to_iso(""))

    def test_invalid_internal_date_is_none(self):
        self.assertIsNone(gmail._internal_date_to_iso("not-a-number"))
        self.assertIsNone(gmail._internal_date_to_iso("99999999999999999999"))


class MappingAndInsertTests(unittest.TestCase):
    def test_new_message_inserts_with_pending_status(self):
        sb = _make_supabase_mock(select_rows=[], insert_row={"id": "uuid-1"})
        with patch.object(gmail, "fetch_recent_messages", return_value=[_msg()]), \
                patch.object(gmail, "supabase", sb):
            result = gmail.import_recent_messages(limit=10)

        self.assertEqual(result["fetched_count"], 1)
        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual(result["imported"][0]["id"], "uuid-1")
        self.assertEqual(result["imported"][0]["external_id"], "gm-1")

        inserted = sb.table.return_value.insert.call_args[0][0]
        self.assertEqual(inserted["channel"], "email")
        self.assertEqual(inserted["external_id"], "gm-1")
        self.assertEqual(inserted["thread_ref"], "gt-1")
        self.assertEqual(inserted["sender_name"], "jingxuan")
        self.assertEqual(inserted["sender_address"], "jngxuann@gmail.com")
        self.assertEqual(inserted["subject"], "Corporate gift box enquiry")
        self.assertEqual(inserted["processing_status"], "PENDING")
        self.assertEqual(inserted["received_at"], "2026-08-13T10:55:26+00:00")

    def test_missing_sender_name_is_none(self):
        sb = _make_supabase_mock(select_rows=[], insert_row={"id": "uuid-1"})
        with patch.object(
            gmail, "fetch_recent_messages", return_value=[_msg(sender_name=None)]
        ), patch.object(gmail, "supabase", sb):
            gmail.import_recent_messages(limit=10)

        inserted = sb.table.return_value.insert.call_args[0][0]
        self.assertIsNone(inserted["sender_name"])
        self.assertEqual(inserted["sender_address"], "jngxuann@gmail.com")

    def test_missing_thread_id_is_none(self):
        sb = _make_supabase_mock(select_rows=[], insert_row={"id": "uuid-1"})
        with patch.object(
            gmail, "fetch_recent_messages", return_value=[_msg(gmail_thread_id=None)]
        ), patch.object(gmail, "supabase", sb):
            gmail.import_recent_messages(limit=10)

        inserted = sb.table.return_value.insert.call_args[0][0]
        self.assertIsNone(inserted["thread_ref"])

    def test_processing_status_always_pending(self):
        sb = _make_supabase_mock(select_rows=[], insert_row={"id": "u"})
        msgs = [_msg(), _msg(gmail_message_id="gm-2")]
        with patch.object(gmail, "fetch_recent_messages", return_value=msgs), \
                patch.object(gmail, "supabase", sb):
            result = gmail.import_recent_messages(limit=10)

        self.assertEqual(result["imported_count"], 2)
        for call in sb.table.return_value.insert.call_args_list:
            self.assertEqual(call[0][0]["processing_status"], "PENDING")


class DeduplicationTests(unittest.TestCase):
    def test_same_message_imported_once_then_skipped(self):
        first = _make_supabase_mock(select_rows=[], insert_row={"id": "uuid-1"})
        with patch.object(gmail, "fetch_recent_messages", return_value=[_msg()]), \
                patch.object(gmail, "supabase", first):
            r1 = gmail.import_recent_messages(limit=10)
        self.assertEqual(r1["imported_count"], 1)

        second = _make_supabase_mock(select_rows=[{"id": "uuid-1"}])
        with patch.object(gmail, "fetch_recent_messages", return_value=[_msg()]), \
                patch.object(gmail, "supabase", second):
            r2 = gmail.import_recent_messages(limit=10)

        self.assertEqual(r2["imported_count"], 0)
        self.assertEqual(r2["skipped_count"], 1)
        self.assertEqual(r2["skipped_external_ids"], ["gm-1"])
        second.table.return_value.insert.assert_not_called()

    def test_same_content_different_ids_both_import(self):
        sb = _make_supabase_mock(select_rows=[], insert_row={"id": "uuid-1"})
        msgs = [_msg(gmail_message_id="gm-1"), _msg(gmail_message_id="gm-2")]
        with patch.object(gmail, "fetch_recent_messages", return_value=msgs), \
                patch.object(gmail, "supabase", sb):
            result = gmail.import_recent_messages(limit=10)

        self.assertEqual(result["imported_count"], 2)
        ids = [c[0][0]["external_id"]
               for c in sb.table.return_value.insert.call_args_list]
        self.assertEqual(sorted(ids), ["gm-1", "gm-2"])

    def test_same_thread_different_ids_both_import(self):
        sb = _make_supabase_mock(select_rows=[], insert_row={"id": "uuid-1"})
        msgs = [
            _msg(gmail_message_id="gm-1", gmail_thread_id="gt-1"),
            _msg(gmail_message_id="gm-2", gmail_thread_id="gt-1"),
        ]
        with patch.object(gmail, "fetch_recent_messages", return_value=msgs), \
                patch.object(gmail, "supabase", sb):
            result = gmail.import_recent_messages(limit=10)

        self.assertEqual(result["imported_count"], 2)
        ids = [c[0][0]["external_id"]
               for c in sb.table.return_value.insert.call_args_list]
        self.assertEqual(sorted(ids), ["gm-1", "gm-2"])

    def test_unique_violation_treated_as_skip(self):
        exc = APIError({
            "code": "23505",
            "message": "duplicate key value violates unique constraint",
        })
        sb = _make_supabase_mock(select_rows=[], insert_exc=exc)
        with patch.object(gmail, "fetch_recent_messages", return_value=[_msg()]), \
                patch.object(gmail, "supabase", sb):
            result = gmail.import_recent_messages(limit=10)

        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["skipped_external_ids"], ["gm-1"])
        self.assertEqual(result["errors"], [])


class SafetyAndSideEffectTests(unittest.TestCase):
    def test_no_pipeline_run_created(self):
        sb = _make_supabase_mock(select_rows=[], insert_row={"id": "u"})
        with patch.object(gmail, "fetch_recent_messages", return_value=[_msg()]), \
                patch.object(gmail, "supabase", sb):
            gmail.import_recent_messages(limit=10)

        tables = [c[0][0] for c in sb.table.call_args_list]
        self.assertTrue(tables)
        self.assertTrue(all(t == "messages" for t in tables))
        self.assertNotIn("pipeline_runs", tables)

    def test_import_does_not_start_processing_runtime(self):
        sb = _make_supabase_mock(select_rows=[], insert_row={"id": "u"})
        with patch.object(gmail, "fetch_recent_messages", return_value=[_msg()]), \
                patch.object(gmail, "supabase", sb):
            gmail.import_recent_messages(limit=10)
        tables = [c[0][0] for c in sb.table.call_args_list]
        self.assertNotIn("pipeline_runs", tables)

    def test_no_gmail_write_method_occurs(self):
        service = MagicMock()
        messages_api = service.users().messages()
        messages_api.list.return_value.execute.return_value = {
            "messages": [{"id": "gm-1"}]
        }
        messages_api.get.return_value.execute.return_value = {
            "id": "gm-1",
            "threadId": "gt-1",
            "internalDate": "1786618526000",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "jingxuan <jngxuann@gmail.com>"},
                    {"name": "Subject", "value": "Corporate gift box enquiry"},
                ],
                "body": {"data": "SGVsbG8="},
            },
        }
        sb = _make_supabase_mock(select_rows=[], insert_row={"id": "u"})

        with patch.object(gmail, "get_gmail_service", return_value=service), \
                patch.object(gmail, "supabase", sb):
            result = gmail.import_recent_messages(limit=10)

        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(messages_api.list.call_count, 1)
        self.assertEqual(messages_api.get.call_count, 1)
        for name in (
            "send", "modify", "delete", "trash", "untrash", "insert", "compose",
        ):
            self.assertEqual(
                getattr(messages_api, name).call_count,
                0,
                f"{name} must never be invoked",
            )


class LimitClampingTests(unittest.TestCase):
    def test_clamp_limit(self):
        self.assertEqual(gmail._clamp_limit(1000), 50)
        self.assertEqual(gmail._clamp_limit(0), 1)
        self.assertEqual(gmail._clamp_limit(-5), 1)
        self.assertEqual(gmail._clamp_limit(7), 7)
        self.assertEqual(gmail._clamp_limit("12"), 12)

    def test_import_passes_clamped_limit_to_fetch(self):
        with patch.object(gmail, "fetch_recent_messages", return_value=[]) as fetch, \
                patch.object(gmail, "supabase", _make_supabase_mock()):
            gmail.import_recent_messages(limit=1000)
        fetch.assert_called_once_with(50)


class PartialFailureTests(unittest.TestCase):
    def test_one_failure_does_not_abort_others(self):
        msgs = [_msg(gmail_message_id="gm-1"), _msg(gmail_message_id="gm-2")]

        def fake_insert(normalized, received_at):
            if normalized["gmail_message_id"] == "gm-1":
                raise RuntimeError("boom")
            return {"id": "uuid-2"}

        with patch.object(gmail, "fetch_recent_messages", return_value=msgs), \
                patch.object(gmail, "_already_imported", return_value=False), \
                patch.object(gmail, "_insert_gmail_message", side_effect=fake_insert):
            result = gmail.import_recent_messages(limit=10)

        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["imported"][0]["external_id"], "gm-2")
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["external_id"], "gm-1")
        self.assertIn("boom", result["errors"][0]["error"])


if __name__ == "__main__":
    unittest.main()
