"""SMTP email client — the app's only outbound-email path.

Wraps stdlib smtplib for the one operation the HVAC tool needs: sending the
daily jobs digest. Returns False on any error (missing config, auth failure,
network) rather than raising, matching the degrade-gracefully contract used
by wix_client/gdrive_client/quickbooks_client — but logs loudly, since a
silently-failing daily email defeats its purpose.

Reads SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD, and
SMTP_FROM (optional, defaults to SMTP_USER) from the environment.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)


def send_email(to: list[str], subject: str, body: str) -> bool:
    """Send a plain-text email via SMTP. Returns True on success, False on any
    error (missing config, auth failure, network) — logs the reason either way."""
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("SMTP_FROM") or user

    if not (host and user and password and sender):
        log.error("SMTP not configured (need SMTP_HOST/SMTP_USER/SMTP_PASSWORD); email not sent.")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
        return True
    except Exception as e:
        log.error("SMTP send failed: %s", e)
        return False
