"""QuickBooks Online client — OAuth 2.0 + Accounting API (single-tenant).

Mirrors wix_client / gdrive_client: returns structured results / None instead of
raising, so the app degrades gracefully when QBO isn't configured or reachable.

Environment variables
---------------------
    QBO_CLIENT_ID, QBO_CLIENT_SECRET   OAuth2 app creds (Intuit Developer portal)
    QBO_ENVIRONMENT                    'sandbox' (default) or 'production'
    QBO_REDIRECT_URI                   must EXACTLY match the Intuit app redirect URI
    QBO_TOKEN_PATH                     token store path; default <JOBS_DIR>/qbo_tokens.json

Token lifecycle
---------------
Access tokens (~1 h) and refresh tokens (~100 d) plus the company realm id persist
to a JSON file on the Render persistent disk. Access tokens auto-refresh on demand;
refresh tokens ROTATE on every refresh, so the file must be writable at runtime.
A cross-process file lock (fcntl) serializes refreshes so the two gunicorn workers
can't invalidate each other's rotated refresh token.
"""

from __future__ import annotations

import base64
import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

# ── Intuit endpoints (same for sandbox + production) ─────────────────
_AUTH_URL   = "https://appcenter.intuit.com/connect/oauth2"
_TOKEN_URL  = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
_REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
_SCOPE      = "com.intuit.quickbooks.accounting"
_MINORVERSION = "73"
_HTTP_TIMEOUT = 20


# ── Config (read fresh from env so Render restarts/changes take effect) ──

def _env() -> str:
    return (os.environ.get("QBO_ENVIRONMENT") or "sandbox").strip().lower()


def _api_base() -> str:
    return ("https://quickbooks.api.intuit.com" if _env() == "production"
            else "https://sandbox-quickbooks.api.intuit.com")


def is_configured() -> bool:
    """True if the OAuth app creds + redirect URI are all set."""
    return bool(os.environ.get("QBO_CLIENT_ID")
                and os.environ.get("QBO_CLIENT_SECRET")
                and os.environ.get("QBO_REDIRECT_URI"))


# ── Token storage ────────────────────────────────────────────────────

def _token_path() -> Path:
    explicit = os.environ.get("QBO_TOKEN_PATH")
    if explicit:
        return Path(explicit)
    jobs = os.environ.get("JOBS_DIR", "./jobs")
    return Path(jobs) / "qbo_tokens.json"


@contextmanager
def _lock():
    """Cross-process exclusive lock around token read/refresh/write."""
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path.with_suffix(".lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def _load_tokens() -> dict:
    try:
        return json.loads(_token_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_tokens(tok: dict) -> None:
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(tok, indent=2))
    tmp.replace(path)          # atomic on POSIX


# ── OAuth: authorize + token exchange/refresh ────────────────────────

def authorize_url(state: str) -> Optional[str]:
    """Build the Intuit consent URL. None if QBO isn't configured."""
    if not is_configured():
        return None
    params = {
        "client_id":     os.environ["QBO_CLIENT_ID"],
        "response_type": "code",
        "scope":         _SCOPE,
        "redirect_uri":  os.environ["QBO_REDIRECT_URI"],
        "state":         state,
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


def _basic_auth() -> str:
    raw = f"{os.environ['QBO_CLIENT_ID']}:{os.environ['QBO_CLIENT_SECRET']}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _post_token(data: dict) -> Optional[dict]:
    try:
        resp = requests.post(
            _TOKEN_URL, data=data,
            headers={"Authorization": _basic_auth(),
                     "Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=_HTTP_TIMEOUT,
        )
        if not resp.ok:
            log.error("QBO token request %s: %s", resp.status_code, resp.text[:300])
            return None
        return resp.json()
    except requests.RequestException as e:
        log.error("QBO token request error: %s", e)
        return None


def _store_from_payload(payload: dict, realm_id: str, prev: Optional[dict] = None) -> dict:
    now = time.time()
    prev = prev or {}
    tok = {
        "access_token":  payload["access_token"],
        "refresh_token": payload.get("refresh_token", prev.get("refresh_token")),
        "realm_id":      realm_id or prev.get("realm_id"),
        "expires_at":    now + int(payload.get("expires_in", 3600)) - 60,
        "refresh_expires_at": now + int(payload.get(
            "x_refresh_token_expires_in", 100 * 24 * 3600)),
    }
    _save_tokens(tok)
    return tok


def exchange_code(code: str, realm_id: str) -> bool:
    """Exchange an authorization code for tokens (the one-time connect)."""
    if not is_configured():
        return False
    payload = _post_token({
        "grant_type":   "authorization_code",
        "code":         code,
        "redirect_uri": os.environ["QBO_REDIRECT_URI"],
    })
    if not payload:
        return False
    with _lock():
        _store_from_payload(payload, realm_id)
    return True


def get_access_token() -> Optional[tuple[str, str]]:
    """Return (access_token, realm_id), refreshing if the access token expired.
    None if not connected or refresh fails."""
    with _lock():
        tok = _load_tokens()
        if not tok.get("refresh_token") or not tok.get("realm_id"):
            return None
        if time.time() < tok.get("expires_at", 0):
            return tok["access_token"], tok["realm_id"]
        payload = _post_token({
            "grant_type":    "refresh_token",
            "refresh_token": tok["refresh_token"],
        })
        if not payload:
            log.error("QBO refresh failed; connection may need re-authorizing.")
            return None
        tok = _store_from_payload(payload, tok["realm_id"], prev=tok)
        return tok["access_token"], tok["realm_id"]


# ── Authenticated API calls ──────────────────────────────────────────

def _request(method: str, path: str, json_body: Optional[dict] = None):
    """Make an authenticated v3 API call. Returns the requests.Response, or None
    if not connected / network error. Callers check resp.ok."""
    auth = get_access_token()
    if not auth:
        return None
    access, realm = auth
    url = f"{_api_base()}/v3/company/{realm}/{path}"
    try:
        return requests.request(
            method, url,
            headers={"Authorization": f"Bearer {access}",
                     "Accept": "application/json",
                     "Content-Type": "application/json"},
            params={"minorversion": _MINORVERSION},
            json=json_body, timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        log.error("QBO API %s %s error: %s", method, path, e)
        return None


def company_info() -> Optional[dict]:
    """Fetch CompanyInfo for the connected realm — used to verify the connection
    and show the company name. None if not connected / error."""
    auth = get_access_token()
    if not auth:
        return None
    _, realm = auth
    resp = _request("GET", f"companyinfo/{realm}")
    if resp is None or not resp.ok:
        if resp is not None:
            log.error("QBO companyinfo %s: %s", resp.status_code, resp.text[:300])
        return None
    return (resp.json() or {}).get("CompanyInfo")


def connection_status() -> dict:
    """Lightweight status for the admin page (no network call)."""
    if not is_configured():
        return {"configured": False, "connected": False, "environment": _env()}
    tok = _load_tokens()
    return {
        "configured":  True,
        "connected":   bool(tok.get("refresh_token") and tok.get("realm_id")),
        "environment": _env(),
        "realm_id":    tok.get("realm_id"),
    }


def disconnect() -> bool:
    """Revoke the refresh token at Intuit and delete the local token file."""
    with _lock():
        tok = _load_tokens()
        rt = tok.get("refresh_token")
        if rt and is_configured():
            try:
                requests.post(
                    _REVOKE_URL, json={"token": rt},
                    headers={"Authorization": _basic_auth(),
                             "Accept": "application/json",
                             "Content-Type": "application/json"},
                    timeout=_HTTP_TIMEOUT,
                )
            except requests.RequestException as e:
                log.error("QBO revoke error (continuing to clear local): %s", e)
        try:
            _token_path().unlink()
        except FileNotFoundError:
            pass
    return True


# ── Customers & Items (populate the invoice dropdowns) ───────────────

def _query(stmt: str) -> Optional[dict]:
    """Run a QBO SQL-like query, returning the QueryResponse dict (or None)."""
    auth = get_access_token()
    if not auth:
        return None
    access, realm = auth
    try:
        resp = requests.get(
            f"{_api_base()}/v3/company/{realm}/query",
            headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
            params={"query": stmt, "minorversion": _MINORVERSION},
            timeout=_HTTP_TIMEOUT,
        )
        if not resp.ok:
            log.error("QBO query %s: %s", resp.status_code, resp.text[:300])
            return None
        return (resp.json() or {}).get("QueryResponse", {})
    except requests.RequestException as e:
        log.error("QBO query error: %s", e)
        return None


def list_customers() -> list[dict]:
    """Active QBO customers as [{id, name, company, email}], sorted by name.
    Empty list if not connected / error."""
    qr = _query("select Id, DisplayName, CompanyName, PrimaryEmailAddr from Customer "
                "where Active = true order by DisplayName maxresults 1000")
    if qr is None:
        return []
    out = []
    for c in qr.get("Customer", []):
        out.append({
            "id":      c.get("Id"),
            "name":    c.get("DisplayName") or c.get("CompanyName") or "",
            "company": c.get("CompanyName") or "",
            "email":   (c.get("PrimaryEmailAddr") or {}).get("Address", ""),
        })
    return out


def list_service_items() -> list[dict]:
    """Active QBO Service items as [{id, name, price}], sorted by name.
    Empty list if not connected / error."""
    qr = _query("select Id, Name, UnitPrice from Item "
                "where Active = true and Type = 'Service' order by Name maxresults 1000")
    if qr is None:
        return []
    return [{"id": i.get("Id"), "name": i.get("Name"), "price": i.get("UnitPrice")}
            for i in qr.get("Item", [])]


# ── Invoice creation ─────────────────────────────────────────────────

def invoice_url(invoice_id: str) -> str:
    """Deep link to view an invoice in the QBO UI (env-aware)."""
    base = ("https://app.qbo.intuit.com" if _env() == "production"
            else "https://app.sandbox.qbo.intuit.com")
    return f"{base}/app/invoice?txnId={invoice_id}"


def find_invoice_by_memo(memo: str) -> Optional[dict]:
    """Look for an existing invoice whose PrivateNote matches `memo` (our Job-No
    stamp) — a server-side guard against creating a duplicate even if the local
    registry is out of sync. Returns {id, doc_number} or None."""
    if not memo:
        return None
    safe = memo.replace("'", "\\'")
    qr = _query(f"select Id, DocNumber, PrivateNote from Invoice "
                f"where PrivateNote = '{safe}' maxresults 5")
    if not qr:
        return None
    invs = qr.get("Invoice", [])
    if not invs:
        return None
    return {"id": invs[0].get("Id"), "doc_number": invs[0].get("DocNumber")}


def create_invoice(customer_id: str, item_id: str, amount, *,
                   description: str = "", memo: str = "") -> dict:
    """Create a single-line invoice (saved, NOT emailed) against an existing
    QBO customer + service item. Non-taxable. Returns:
        {ok: True, invoice_id, doc_number, total}  on success
        {ok: False, error: str}                    on failure
    """
    if not (customer_id and item_id):
        return {"ok": False, "error": "customer_id and item_id are required"}
    try:
        amt = round(float(amount), 2)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"invalid amount: {amount!r}"}
    if amt <= 0:
        return {"ok": False, "error": "amount must be greater than 0"}

    line = {
        "DetailType": "SalesItemLineDetail",
        "Amount": amt,
        "SalesItemLineDetail": {
            "ItemRef": {"value": str(item_id)},
            "TaxCodeRef": {"value": "NON"},      # non-taxable
        },
    }
    if description:
        line["Description"] = description[:4000]
    body: dict = {"Line": [line], "CustomerRef": {"value": str(customer_id)}}
    if memo:
        body["CustomerMemo"] = {"value": memo}   # shows on the invoice
        body["PrivateNote"] = memo               # internal; used by find_invoice_by_memo

    resp = _request("POST", "invoice", json_body=body)
    if resp is None:
        return {"ok": False, "error": "not connected to QuickBooks / network error"}
    if not resp.ok:
        return {"ok": False, "error": f"QuickBooks {resp.status_code}: {resp.text[:400]}"}
    inv = (resp.json() or {}).get("Invoice", {})
    return {"ok": True, "invoice_id": inv.get("Id"),
            "doc_number": inv.get("DocNumber"), "total": inv.get("TotalAmt")}
