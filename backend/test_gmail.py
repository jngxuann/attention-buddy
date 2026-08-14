"""
Unit tests for the read-only Gmail integration (Phase 1).

Covers:
  - From header parsing (name + address)
  - missing sender name
  - plain-text body extraction
  - multipart body extraction
  - nested multipart body extraction
  - missing body -> ""
  - base64url decoding (with and without padding)
  - missing credentials.json -> clear error
  - fetch_recent_messages normalization
  - no Gmail send/modify/delete/trash method is ever invoked

No real Gmail account, credentials.json, or token.json is required:
the Gmail API client is mocked throughout.
"""

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import gmail


def b64(text):
    """Encode a UTF-8 string the way Gmail does (base64url, unpadded)."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _headers(*pairs):
    return [{"name": name, "value": value} for name, value in pairs]


class FromHeaderTests(unittest.TestCase):
    def test_name_and_address_parsed(self):
        name, address = gmail.parse_sender("Sarah Tan <sarah@example.com>")
        self.assertEqual(name, "Sarah Tan")
        self.assertEqual(address, "sarah@example.com")

    def test_missing_sender_name(self):
        name, address = gmail.parse_sender("sarah@example.com")
        self.assertIsNone(name)
        self.assertEqual(address, "sarah@example.com")

    def test_empty_from_header(self):
        self.assertEqual(gmail.parse_sender(""), (None, None))

    def test_none_from_header(self):
        self.assertEqual(gmail.parse_sender(None), (None, None))

    def test_get_header_case_insensitive(self):
        headers = _headers(("From", "a@b.com"), ("subject", "Hi"))
        self.assertEqual(gmail.get_header(headers, "Subject"), "Hi")
        self.assertIsNone(gmail.get_header(headers, "Date"))


class BodyExtractionTests(unittest.TestCase):
    def test_plain_text_body(self):
        msg = {
            "payload": {
                "mimeType": "text/plain",
                "body": {"data": b64("Hello plain world")},
            }
        }
        self.assertEqual(gmail.extract_body(msg), "Hello plain world")

    def test_multipart_alternative_prefers_plain(self):
        msg = {
            "payload": {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64("plain text")}},
                    {"mimeType": "text/html", "body": {"data": b64("<p>html</p>")}},
                ],
            }
        }
        self.assertEqual(gmail.extract_body(msg), "plain text")

    def test_nested_multipart_extraction(self):
        msg = {
            "payload": {
                "mimeType": "multipart/mixed",
                "parts": [
                    {
                        "mimeType": "multipart/alternative",
                        "parts": [
                            {"mimeType": "text/plain", "body": {"data": b64("nested plain")}},
                            {"mimeType": "text/html", "body": {"data": b64("<b>nested html</b>")}},
                        ],
                    },
                    {"mimeType": "image/png", "body": {"attachmentId": "att-1"}},
                ],
            }
        }
        self.assertEqual(gmail.extract_body(msg), "nested plain")

    def test_missing_body_returns_empty_string(self):
        self.assertEqual(gmail.extract_body({"payload": {}}), "")
        self.assertEqual(gmail.extract_body({}), "")

    def test_html_fallback_when_no_plain(self):
        msg = {
            "payload": {
                "mimeType": "text/html",
                "body": {"data": b64("<p>Only <b>html</b></p>")},
            }
        }
        self.assertEqual(gmail.extract_body(msg), "Only html")


class Base64UrlTests(unittest.TestCase):
    def test_decode_standard(self):
        self.assertEqual(gmail.decode_base64url(b64("hello")), "hello")

    def test_decode_with_padding(self):
        self.assertEqual(gmail.decode_base64url(b64("hello") + "=="), "hello")

    def test_decode_empty_and_invalid(self):
        self.assertEqual(gmail.decode_base64url(""), "")
        self.assertEqual(gmail.decode_base64url(None), "")
        self.assertEqual(gmail.decode_base64url("!!!not-base64!!!"), "")


class MissingCredentialsTests(unittest.TestCase):
    def test_missing_credentials_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_creds = Path(tmp) / "credentials.json"
            with patch.object(gmail, "TOKEN_FILE", Path(tmp) / "token.json"), \
                    patch.object(gmail, "CREDENTIALS_FILE", fake_creds):
                with self.assertRaises(FileNotFoundError) as ctx:
                    gmail.get_gmail_service()
            message = str(ctx.exception)
            self.assertIn("credentials", message.lower())
            self.assertIn(str(fake_creds), message)


class ScopeSafetyTests(unittest.TestCase):
    def test_scopes_are_readonly_plus_send(self):
        self.assertIn(
            "https://www.googleapis.com/auth/gmail.readonly", gmail.SCOPES
        )
        self.assertIn(
            "https://www.googleapis.com/auth/gmail.send", gmail.SCOPES
        )
        # Minimum additional scope only: never modify/delete/compose access.
        for scope in gmail.SCOPES:
            self.assertTrue(
                scope.endswith("readonly") or scope.endswith("send")
            )


class FetchRecentMessagesTests(unittest.TestCase):
    def _full_message(self):
        return {
            "id": "m1",
            "threadId": "t1",
            "internalDate": "1690000000000",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "Sarah Tan <sarah@example.com>"},
                    {"name": "Subject", "value": "Hello"},
                    {"name": "Date", "value": "Mon, 24 Jul 2023 10:00:00 +0000"},
                ],
                "body": {"data": b64("Body text")},
            },
        }

    def test_normalize_message_shape(self):
        result = gmail.normalize_message(self._full_message())
        self.assertEqual(result["gmail_message_id"], "m1")
        self.assertEqual(result["gmail_thread_id"], "t1")
        self.assertEqual(result["sender_name"], "Sarah Tan")
        self.assertEqual(result["sender_address"], "sarah@example.com")
        self.assertEqual(result["subject"], "Hello")
        self.assertEqual(result["body_verbatim"], "Body text")
        self.assertEqual(result["internal_date"], "1690000000000")
        self.assertEqual(
            result["gmail_date_header"], "Mon, 24 Jul 2023 10:00:00 +0000"
        )

    def test_missing_transport_metadata_is_none(self):
        msg = {
            "id": "m2",
            "payload": {"headers": [], "body": {"data": b64("x")}},
        }
        result = gmail.normalize_message(msg)
        self.assertEqual(result["gmail_message_id"], "m2")
        self.assertIsNone(result["gmail_thread_id"])
        self.assertIsNone(result["internal_date"])
        self.assertIsNone(result["sender_name"])
        self.assertIsNone(result["sender_address"])
        self.assertIsNone(result["subject"])
        self.assertIsNone(result["gmail_date_header"])

    def test_fetch_recent_messages_normalization(self):
        service = MagicMock()
        messages_api = service.users().messages()
        messages_api.list.return_value.execute.return_value = {
            "messages": [{"id": "m1"}]
        }
        messages_api.get.return_value.execute.return_value = self._full_message()

        with patch.object(gmail, "get_gmail_service", return_value=service):
            result = gmail.fetch_recent_messages(limit=5)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["gmail_message_id"], "m1")
        messages_api.list.assert_called_once_with(userId="me", maxResults=5)
        messages_api.get.assert_called_once_with(
            userId="me", id="m1", format="full"
        )

    def test_no_write_methods_invoked(self):
        service = MagicMock()
        messages_api = service.users().messages()
        messages_api.list.return_value.execute.return_value = {
            "messages": [{"id": "m1"}]
        }
        messages_api.get.return_value.execute.return_value = self._full_message()

        with patch.object(gmail, "get_gmail_service", return_value=service):
            gmail.fetch_recent_messages(limit=5)

        self.assertEqual(messages_api.list.call_count, 1)
        self.assertEqual(messages_api.get.call_count, 1)

        for method_name in (
            "send", "modify", "delete", "trash", "untrash",
            "insert", "compose", "batchDelete", "batchModify",
        ):
            self.assertEqual(
                getattr(messages_api, method_name).call_count,
                0,
                f"{method_name} must never be invoked",
            )


if __name__ == "__main__":
    unittest.main()
