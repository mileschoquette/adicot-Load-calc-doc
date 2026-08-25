"""Gmail draft client — creates real Gmail drafts via domain-wide delegation.

Used by the "Save & Send to Client" work-order action: rather than sending an
email directly, this creates a REAL Gmail draft sitting in the sender
mailbox's Drafts folder — exactly like the existing Apps Script intake
pipeline's createClientDraft()/createQuestionsEmailDraft(). Nothing the app
does ever sends an email on its own; a human always opens Gmail and clicks
Send. This module has no "send" function on purpose.

Authentication
--------------
Reuses the same GOOGLE_SERVICE_ACCOUNT_JSON service account already used by
gdrive_client.py and sheets_client.py, but impersonates a specific mailbox via
domain-wide delegation (Credentials.with_subject()). That requires a one-time
manual step in the Google Workspace Admin console (Security > API controls >
Domain-wide Delegation): add the service account's Client ID with the scope
https://www.googleapis.com/auth/gmail.compose, and enable the Gmail API on the
GCP project. Without that grant, every call here fails (typically 401/403)
and degrades to None, same as any other missing-credential case in this app —
don't raise from here.

GMAIL_DRAFT_SENDER (env, default "agc@adicot.com") is the mailbox the draft is
created in. That mailbox must be a real user in the same Google Workspace
domain that granted the delegation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from email.message import EmailMessage
from html import escape
from typing import Optional

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<]+")


def _plain_to_html(text: str) -> str:
    """Turn a plain-text body into an HTML equivalent with real <a href>
    links. Gmail's compose/draft EDITOR (unlike its read view for a received
    message) does not auto-linkify bare URLs in plain text, so a draft built
    with only a text/plain part shows the link as dead text until it's sent.
    Building a text/html alternative with an actual anchor tag makes it
    clickable in the draft itself, not just after sending."""
    escaped = escape(text, quote=False)   # neutralize <, >, & in the original text first

    def _link(m: "re.Match") -> str:
        url = m.group(0)   # already-escaped URL text — safe to embed as-is
        return f'<a href="{url}">{url}</a>'

    linked = _URL_RE.sub(_link, escaped)
    return linked.replace("\n", "<br>")

_SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]


def _default_sender() -> str:
    return os.environ.get("GMAIL_DRAFT_SENDER", "agc@adicot.com")


# Cached per-sender service instance (per worker) — domain-wide delegation
# credentials are subject-specific, so we can't share gdrive_client's/
# sheets_client's cached service objects even though the underlying key is
# the same.
_service_cache: dict = {}


def _build_service(sender: str):
    if sender in _service_cache:
        return _service_cache[sender]

    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        log.warning("GOOGLE_SERVICE_ACCOUNT_JSON not set; gmail_client disabled.")
        return None

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as e:
        log.error("Google API libraries not installed: %s", e)
        return None

    try:
        info = json.loads(creds_json)
    except json.JSONDecodeError as e:
        log.error("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: %s", e)
        return None

    try:
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=_SCOPES,
        ).with_subject(sender)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        _service_cache[sender] = service
        return service
    except Exception as e:
        log.error("Failed to build Gmail service for %s: %s", sender, e)
        return None


def create_draft(to: list[str], subject: str, body: str,
                  sender: Optional[str] = None) -> Optional[str]:
    """Create a Gmail draft in `sender`'s mailbox (default GMAIL_DRAFT_SENDER,
    "agc@adicot.com"). Returns the draft id on success, None on any error —
    missing credentials, domain-wide delegation not granted, Gmail API not
    enabled, network failure, etc. — logged and swallowed, never raised,
    matching the degrade-gracefully contract used by every other integration
    in this app (wix_client, gdrive_client, sheets_client)."""
    sender = sender or _default_sender()
    service = _build_service(sender)
    if service is None:
        return None

    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(body)                                    # text/plain part
    msg.add_alternative(_plain_to_html(body), subtype="html") # text/html part with real <a href> links
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    try:
        draft = service.users().drafts().create(
            userId="me", body={"message": {"raw": raw}},
        ).execute()
        draft_id = draft.get("id")
        log.info("Gmail draft created in %s (id %s)", sender, draft_id)
        return draft_id
    except Exception as e:
        log.error("Gmail create_draft failed for sender=%s: %s", sender, e)
        return None
