"""Signed, expiring magic-link tokens for the client portal.

Why not itsdangerous: tokens must be MINTED in two places — Flask (Python, via
notifyProjectsSheet()'s successor routes and the admin "send to client" action)
and Google Apps Script (JavaScript, in AdicotProjects.gs's notifyProjectsSheet(),
which creates the very first token when a job is intaken from email). itsdangerous
is a Python-only serialization format with no practical Apps Script equivalent, so
a token it mints could not be minted (only verified) from Apps Script. This module
instead uses a minimal, language-agnostic scheme: HMAC-SHA256 over a plain
"job_id.expiry_unix_ts" string, both sides base64url-encoded. Any runtime with an
HMAC-SHA256 primitive and base64url (Python's hmac/base64, or Apps Script's
Utilities.computeHmacSha256Signature/base64EncodeWebSafe) can mint or verify it.

Token shape:  base64url(payload) + "." + base64url(HMAC-SHA256(secret, base64url(payload)))
where payload = f"{job_id}.{expiry_unix_ts}"

This module is a pure function of its arguments — it does not read any env vars.
Callers (app.py, the migration script, etc.) pass the shared secret in explicitly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def make_token(job_id: str, secret: str, days_valid: int = 180) -> str:
    """Build a signed magic-link token for job_id, valid for days_valid days."""
    expiry_ts = int(time.time()) + days_valid * 86400
    payload = f"{job_id}.{expiry_ts}"
    payload_b64 = _b64url_encode(payload.encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"),
                   hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}"


def verify_token(token: str, secret: str) -> Optional[str]:
    """Return the job_id encoded in token if its signature is valid and it
    hasn't expired, else None. Never raises — any malformed/tampered/garbage
    input just returns None."""
    try:
        payload_b64, sig_b64 = token.split(".", 1)

        expected_sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"),
                                 hashlib.sha256).digest()
        expected_sig_b64 = _b64url_encode(expected_sig)
        if not hmac.compare_digest(sig_b64, expected_sig_b64):
            return None

        payload = _b64url_decode(payload_b64).decode("utf-8")
        job_id, expiry_ts_str = payload.rsplit(".", 1)
        expiry_ts = int(expiry_ts_str)

        if time.time() >= expiry_ts:
            return None
        return job_id
    except Exception:
        return None


if __name__ == "__main__":
    secret = "throwaway-test-secret"

    token = make_token("job123", secret, days_valid=180)
    result = verify_token(token, secret)
    print("PASS" if result == "job123" else f"FAIL (round-trip got {result!r})")

    # Flip one character in the signature portion to simulate tampering.
    payload_part, sig_part = token.rsplit(".", 1)
    flipped_char = "a" if sig_part[0] != "a" else "b"
    tampered = payload_part + "." + flipped_char + sig_part[1:]
    tampered_result = verify_token(tampered, secret)
    print("PASS" if tampered_result is None else f"FAIL (tampered token verified as {tampered_result!r})")
