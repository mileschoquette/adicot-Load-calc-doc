"""Wix CMS client — mostly read-only, plus one write operation.

Wraps the Wix Data v2 REST API for the operations the HVAC tool needs:
  - list_projects() — returns lightweight project records for the autocomplete
  - get_project(item_id) — returns one full project record for the validator
  - delete_project(item_id) — permanently deletes a project (the only write
    call in this module; requires "Manage" scope on the WIX_API_KEY, unlike
    the read calls above)

All functions return None / [] / False on any error (network, auth, bad
response) so callers can degrade gracefully when Wix is unreachable or
under-permissioned. Don't raise from here.

Reads WIX_API_KEY and WIX_SITE_ID from environment. If either is missing,
list_projects() returns [] and get_project() returns None — the tool keeps
working without Wix integration in that case.

A small in-memory TTL cache on list_projects() means the upload page can call
it on every page load without hitting the Wix API more than once every 5 min.
The cache is per-worker, which is fine — gunicorn workers stay warm and Wix
data doesn't change quickly.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ── Config from env ──────────────────────────────────────────────────
_WIX_API_BASE = "https://www.wixapis.com/wix-data/v2"
_PROJECTS_COLLECTION_ID = "Projects"

# How long list_projects() results stay cached. 5 minutes is short enough that
# new Wix entries appear in the dropdown without a server restart, long enough
# that we don't hammer the API.
_LIST_CACHE_TTL_SECONDS = 300

# How long any one HTTP call is allowed to take before we give up. Wix is
# usually fast; if it's slow, we'd rather show "no projects" than block the
# upload page for 30 seconds.
_HTTP_TIMEOUT_SECONDS = 6


def _credentials() -> Optional[tuple[str, str]]:
    """Return (api_key, site_id) if both are set in env, else None."""
    api_key = os.environ.get("WIX_API_KEY")
    site_id = os.environ.get("WIX_SITE_ID")
    if not api_key or not site_id:
        return None
    return api_key, site_id


def _plain_date(value) -> str:
    """Wix returns _createdDate as {"$date": "2026-08-10T14:16:56.926Z"} (Mongo-style
    extended JSON), not a plain ISO string. Unwrap that shape; pass through a plain
    string as-is; anything else (missing, unrecognized shape) becomes ""."""
    if isinstance(value, dict):
        return value.get("$date") or ""
    if isinstance(value, str):
        return value
    return ""


def _headers() -> Optional[dict]:
    creds = _credentials()
    if not creds:
        return None
    api_key, site_id = creds
    return {
        "Authorization": api_key,
        "wix-site-id": site_id,
        "Content-Type": "application/json",
    }


# ── In-memory cache for list_projects ────────────────────────────────
_list_cache: dict = {"data": None, "fetched_at": 0.0}


def _cache_fresh() -> bool:
    return (_list_cache["data"] is not None
            and time.time() - _list_cache["fetched_at"] < _LIST_CACHE_TTL_SECONDS)


def invalidate_cache() -> None:
    """Force the next list_projects() call to hit Wix. Useful for /debug or tests."""
    _list_cache["data"] = None
    _list_cache["fetched_at"] = 0.0


# ── Public API ───────────────────────────────────────────────────────
def list_projects() -> list[dict]:
    """Return a list of lightweight project records for the upload-page autocomplete.

    Each entry has the keys the dropdown needs (and nothing else, to keep the
    HTML response small): _id, projectAddress, jobNo, title, createdDate,
    signedDate.

    Returns [] on any failure — including missing credentials. The autocomplete
    just shows no suggestions in that case and the engineer can type freely.
    """
    if _cache_fresh():
        return _list_cache["data"]

    headers = _headers()
    if headers is None:
        log.warning("Wix credentials not set; list_projects() returning [].")
        return []

    try:
        # Wix Data: query items. We page through with cursor pagination in case
        # there are more than 100 projects eventually (Wix returns max 100 per
        # page). Limit per call: 100, the API max for items.query.
        items: list[dict] = []
        cursor: Optional[str] = None
        page = 0
        # Hard cap to avoid runaway loops if Wix returns a buggy cursor.
        # 10 pages × 100 items = 1000 projects, more than enough.
        while page < 10:
            body: dict = {
                "dataCollectionId": _PROJECTS_COLLECTION_ID,
                "query": {
                    "paging": {"limit": 100},
                    # Only fetch the fields the autocomplete and calendar need.
                    "fields": ["_id", "projectAddress", "jobNo", "title", "_createdDate", "signedDate"],
                },
            }
            if cursor:
                body["query"]["cursorPaging"] = {"cursor": cursor}
                # When using cursorPaging, drop the offset 'paging' field
                body["query"].pop("paging", None)

            resp = requests.post(
                f"{_WIX_API_BASE}/items/query",
                headers=headers,
                json=body,
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
            if not resp.ok:
                log.error("Wix list_projects HTTP %s: %s",
                          resp.status_code, resp.text[:300])
                # If we got SOME items before the error, return what we have
                # rather than nothing.
                break

            payload = resp.json()
            for item in payload.get("dataItems", []):
                data = item.get("data", {}) or {}
                items.append({
                    "_id":            data.get("_id") or item.get("id") or "",
                    "projectAddress": data.get("projectAddress") or "",
                    "jobNo":          data.get("jobNo") or "",
                    "title":          data.get("title") or "",
                    "createdDate":    _plain_date(data.get("_createdDate") or item.get("_createdDate")),
                    "signedDate":     _plain_date(data.get("signedDate")),
                })

            cursors = payload.get("pagingMetadata", {}).get("cursors") or {}
            next_cursor = cursors.get("next")
            if not next_cursor or not payload.get("pagingMetadata", {}).get("hasNext"):
                break
            cursor = next_cursor
            page += 1

        _list_cache["data"] = items
        _list_cache["fetched_at"] = time.time()
        return items

    except requests.RequestException as e:
        log.error("Wix list_projects request failed: %s", e)
        # Don't poison the cache with the failure; next call will retry.
        return []


def get_project(item_id: str) -> Optional[dict]:
    """Fetch one full project record by its Wix _id.

    Returns the raw data dict (every Wix field for that project) or None on
    any error. Not cached — we always want fresh data when running validation
    against a job, since project specs can change in Wix between jobs.
    """
    if not item_id:
        return None
    headers = _headers()
    if headers is None:
        log.warning("Wix credentials not set; get_project() returning None.")
        return None

    try:
        resp = requests.get(
            f"{_WIX_API_BASE}/items/{item_id}",
            headers=headers,
            params={"dataCollectionId": _PROJECTS_COLLECTION_ID},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        if not resp.ok:
            log.error("Wix get_project(%s) HTTP %s: %s",
                      item_id, resp.status_code, resp.text[:300])
            return None
        payload = resp.json()
        # Wix wraps single-item responses in {"dataItem": {"data": {...}}}
        item = payload.get("dataItem") or payload
        return item.get("data") or item

    except requests.RequestException as e:
        log.error("Wix get_project(%s) request failed: %s", item_id, e)
        return None


def delete_project(item_id: str) -> bool:
    """Permanently delete a project from the Wix CMS. Returns True on success,
    False on any error (network, auth, missing "Manage" permission, etc.) —
    same degrade-gracefully contract as the rest of this module. Note: unlike
    list_projects()/get_project(), this requires the WIX_API_KEY to have
    Manage (write) access to Data Items, not just Read — a 403 here likely
    means that scope hasn't been granted in the Wix dashboard yet."""
    if not item_id:
        return False
    headers = _headers()
    if headers is None:
        log.warning("Wix credentials not set; delete_project() returning False.")
        return False

    try:
        resp = requests.delete(
            f"{_WIX_API_BASE}/items/{item_id}",
            headers=headers,
            params={"dataCollectionId": _PROJECTS_COLLECTION_ID},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        if not resp.ok:
            log.error("Wix delete_project(%s) HTTP %s: %s",
                      item_id, resp.status_code, resp.text[:300])
        return resp.ok

    except requests.RequestException as e:
        log.error("Wix delete_project(%s) request failed: %s", item_id, e)
        return False
