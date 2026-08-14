"""
Gmail integration for Attention Buddy.

This module authenticates with Gmail using an OAuth 2.0 Desktop App
client.  It exposes a read-only fetch/ingest path plus an explicit,
manual-only send path.  Nothing is ever sent automatically, and the
module never archives, labels, marks-read, or deletes anything.

Scopes requested:
  https://www.googleapis.com/auth/gmail.readonly
  https://www.googleapis.com/auth/gmail.send
"""

import base64
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from database import supabase
from postgrest.exceptions import APIError

# ── scope ────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

# ── paths (resolved relative to this file, never the CWD) ────────────

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"


# ── OAuth / service construction ─────────────────────────────────────

def _load_token_credentials():
    """Return saved credentials from token.json, or None if unavailable.

    Scopes are read from the token file itself (not forced) so we can
    detect when an old read-only token lacks the send scope.
    """
    if not TOKEN_FILE.exists():
        return None
    try:
        return Credentials.from_authorized_user_file(str(TOKEN_FILE))
    except (ValueError, OSError):
        # Corrupt/empty token file — fall through to a fresh flow.
        return None


def _save_credentials(creds):
    """Persist credentials to token.json without printing anything."""
    TOKEN_FILE.write_text(creds.to_json())


def _has_required_scopes(creds):
    """True when the credentials actually grant every required scope."""
    granted = set(creds.scopes or [])
    return set(SCOPES).issubset(granted)


def get_gmail_service():
    """
    Build and return an authenticated Gmail API service.

    Order of operations:
      1. Reuse token.json if present, valid, and grants every scope.
      2. Refresh expired credentials when a refresh token is available
         and the token already grants every scope.
      3. Otherwise start a local OAuth browser flow (read + send scopes)
         and persist the resulting token to token.json.  A stale
         read-only token therefore triggers a one-time re-authorization.
    """
    creds = _load_token_credentials()

    if creds is not None and creds.valid and _has_required_scopes(creds):
        return build("gmail", "v1", credentials=creds)

    if (
        creds is not None
        and creds.expired
        and creds.refresh_token
        and _has_required_scopes(creds)
    ):
        creds.refresh(Request())
        _save_credentials(creds)
        return build("gmail", "v1", credentials=creds)

    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            "Google OAuth client credentials not found. "
            f"Expected a downloaded OAuth 2.0 Desktop App credentials file at: "
            f"{CREDENTIALS_FILE}. "
            "Create a 'Desktop app' OAuth client in Google Cloud Console and "
            "download its JSON to that exact path, then retry."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_FILE), SCOPES
    )
    creds = flow.run_local_server(port=0)
    _save_credentials(creds)
    return build("gmail", "v1", credentials=creds)


def describe_gmail_error(exc):
    """Build a token-free, safe description of a Gmail API error."""
    status_code = getattr(getattr(exc, "resp", None), "status", None)
    reason = None
    content = getattr(exc, "content", None)
    if isinstance(content, bytes):
        try:
            import json as _json

            payload = _json.loads(content.decode("utf-8", errors="replace"))
            reason = ((payload or {}).get("error") or {}).get("message")
        except (ValueError, UnicodeDecodeError):
            reason = None
    elif isinstance(content, dict):
        reason = ((content or {}).get("error") or {}).get("message")
    if reason:
        return f"Gmail send failed ({status_code}): {reason}"
    if status_code:
        return f"Gmail send failed (HTTP {status_code})"
    return "Gmail send failed"


# ── sending (explicit, manual only) ────────────────────────────────

def build_mime_message(to, subject, body):
    """Build a plain-text MIME email message."""
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    return message


def send_gmail_message(to, subject, body, *, thread_id=None):
    """
    Send a plain-text email through the Gmail API.

    Returns the Gmail message resource for the sent message, or raises
    the underlying Gmail error (never silently swallows it).  Threading
    is preserved by passing the stored Gmail ``threadId`` when available;
    no metadata is fabricated.
    """
    service = get_gmail_service()
    mime = build_mime_message(to, subject, body)
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
    payload = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    return (
        service.users()
        .messages()
        .send(userId="me", body=payload)
        .execute()
    )


# ── header parsing ───────────────────────────────────────────────────

def get_header(headers, name):
    """
    Return the value of the named Gmail header, or None if absent.

    ``headers`` is the ``payload.headers`` list from a Gmail message,
    i.e. ``[{"name": "From", "value": "..."}, ...]``.
    """
    for header in headers or []:
        if (header.get("name") or "").lower() == name.lower():
            return header.get("value")
    return None


def parse_sender(from_header):
    """
    Split a Gmail ``From`` header into ``(sender_name, sender_address)``.

    Neither field is invented: if a component is unavailable it is None.
    """
    if not from_header:
        return None, None
    name, address = parseaddr(from_header)
    sender_name = name.strip() if name and name.strip() else None
    sender_address = address.strip() if address and address.strip() else None
    return sender_name, sender_address


# ── body extraction ──────────────────────────────────────────────────

def decode_base64url(data):
    """
    Safely decode Gmail's base64url-encoded body data to UTF-8 text.

    Returns "" on empty/invalid input and never raises.
    """
    if not data:
        return ""
    if isinstance(data, str):
        data = data.encode("ascii", errors="ignore")
    # Gmail may omit base64 padding.
    data = data + b"=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode(data)
    except (ValueError, TypeError):
        return ""
    return raw.decode("utf-8", errors="replace")


class _TextExtractor(HTMLParser):
    """Minimal HTML -> plain text converter (stdlib only)."""

    _BLOCK_TAGS = {
        "p", "div", "br", "li", "tr", "td", "th",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote", "section", "article", "ul", "ol",
    }

    def __init__(self):
        super().__init__()
        self._chunks = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self):
        return "".join(self._chunks)


def _html_to_text(html):
    """Derive readable plain text from an HTML string."""
    extractor = _TextExtractor()
    try:
        extractor.feed(html or "")
        extractor.close()
    except Exception:
        return ""
    # Collapse blank runs to a single newline, strip per-line whitespace.
    lines = []
    for line in extractor.text().splitlines():
        stripped = line.strip()
        if stripped:
            lines.append(stripped)
    return "\n".join(lines)


def _iter_parts(payload):
    """Yield every leaf MIME part under ``payload`` (recursively)."""
    if not payload:
        return
    parts = payload.get("parts")
    if parts:
        for part in parts:
            yield from _iter_parts(part)
    else:
        yield payload


def extract_body(message):
    """
    Extract a plain-text body from a Gmail message (format="full").

    Preference order: text/plain, then a readable derivation from
    text/html, then "".
    """
    payload = message.get("payload") or {}
    plain = None
    html = None

    for part in _iter_parts(payload):
        mime_type = (part.get("mimeType") or "").lower()
        body = part.get("body") or {}
        data = body.get("data")
        if mime_type == "text/plain" and plain is None and data:
            plain = decode_base64url(data)
        elif mime_type == "text/html" and html is None and data:
            html = decode_base64url(data)

    if plain is not None:
        return plain
    if html is not None:
        return _html_to_text(html)
    return ""


# ── fetching / normalization ─────────────────────────────────────────

def normalize_message(message):
    """Normalize a Gmail message (format="full") into a plain dict."""
    payload = message.get("payload") or {}
    headers = payload.get("headers") or []

    from_value = get_header(headers, "From")
    sender_name, sender_address = parse_sender(from_value)

    return {
        "gmail_message_id": message.get("id"),
        "gmail_thread_id": message.get("threadId"),
        "sender_name": sender_name,
        "sender_address": sender_address,
        "subject": get_header(headers, "Subject"),
        "body_verbatim": extract_body(message),
        "internal_date": message.get("internalDate"),
        "gmail_date_header": get_header(headers, "Date"),
    }


def fetch_recent_messages(limit: int = 5):
    """
    Read-only: fetch up to ``limit`` recent Gmail messages.

    Returns normalized dicts only.  Does not import, classify, or send.
    """
    service = get_gmail_service()

    list_response = (
        service.users()
        .messages()
        .list(userId="me", maxResults=limit)
        .execute()
    )

    results = []
    for item in list_response.get("messages", []) or []:
        message_id = item.get("id")
        if not message_id:
            continue
        full = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        results.append(normalize_message(full))

    return results


# ── ingestion (Gmail -> Supabase messages) ────────────────────────────

MIN_LIMIT = 1
MAX_LIMIT = 50


def _clamp_limit(limit):
    """Clamp a sync limit into the allowed [MIN_LIMIT, MAX_LIMIT] range."""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return MAX_LIMIT
    return max(MIN_LIMIT, min(value, MAX_LIMIT))


def _internal_date_to_iso(internal_date):
    """
    Convert Gmail internalDate (epoch milliseconds) to a timezone-aware
    UTC ISO timestamp.  Returns None when missing or unparseable.
    """
    if not internal_date:
        return None
    try:
        return datetime.fromtimestamp(
            int(internal_date) / 1000, tz=timezone.utc
        ).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _already_imported(external_id):
    """True if a message with this (channel=email, external_id) exists."""
    if not external_id:
        return False
    response = (
        supabase.table("messages")
        .select("id")
        .eq("channel", "email")
        .eq("external_id", external_id)
        .limit(1)
        .execute()
    )
    return bool(response.data)


def _is_unique_violation(exc):
    """True for a Postgres unique-violation error (23505)."""
    return getattr(exc, "code", None) == "23505"


def _describe_error(exc):
    """Build a token-free description of an exception."""
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None)
    if code and message:
        return f"{code}: {message}"
    if message:
        return str(message)
    if code:
        return f"error code {code}"
    return str(exc) or repr(exc)


def _insert_gmail_message(normalized, received_at):
    """Insert one normalized Gmail message into Supabase. Read-only on Gmail."""
    row = {
        "channel": "email",
        "external_id": normalized.get("gmail_message_id"),
        "thread_ref": normalized.get("gmail_thread_id"),
        "sender_name": normalized.get("sender_name"),
        "sender_address": normalized.get("sender_address"),
        "subject": normalized.get("subject"),
        "body_verbatim": normalized.get("body_verbatim"),
        "received_at": received_at,
        "processing_status": "PENDING",
    }
    response = supabase.table("messages").insert(row).execute()
    return response.data[0] if response.data else None


def import_recent_messages(limit: int = 10):
    """
    Read Gmail and insert unseen messages into Supabase (channel="email").

    Deduplicates on (channel, external_id) where external_id is the real
    Gmail message ID.  Never auto-processes: processing_status stays
    PENDING and no pipeline run is created.

    Returns a structured summary.  Never exposes OAuth tokens.
    """
    limit = _clamp_limit(limit)
    fetched = fetch_recent_messages(limit)

    result = {
        "fetched_count": len(fetched),
        "imported_count": 0,
        "skipped_count": 0,
        "imported": [],
        "skipped_external_ids": [],
        "errors": [],
    }

    for message in fetched:
        external_id = message.get("gmail_message_id")
        if not external_id:
            result["errors"].append({
                "external_id": None,
                "error": "Gmail message missing id; skipped",
            })
            continue

        received_at = _internal_date_to_iso(message.get("internal_date"))

        try:
            if _already_imported(external_id):
                result["skipped_count"] += 1
                result["skipped_external_ids"].append(external_id)
                continue

            row = _insert_gmail_message(message, received_at)
            result["imported_count"] += 1
            result["imported"].append({
                "id": (row or {}).get("id"),
                "external_id": external_id,
                "subject": message.get("subject"),
            })
        except APIError as exc:
            if _is_unique_violation(exc):
                # Race with a concurrent sync: treat as already imported.
                result["skipped_count"] += 1
                result["skipped_external_ids"].append(external_id)
            else:
                result["errors"].append({
                    "external_id": external_id,
                    "error": _describe_error(exc),
                })
        except Exception as exc:  # noqa: BLE001 - isolate per-message failures
            result["errors"].append({
                "external_id": external_id,
                "error": _describe_error(exc),
            })

    return result
