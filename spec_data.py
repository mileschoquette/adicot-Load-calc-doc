"""Spec clause data source.

Reads the three CMS collections (Spec Parts / Sections / Clauses) via the Wix
Data v2 REST API, using the same credentials and patterns as wix_client.py.
Falls back to a bundled JSON file (spec_seed.json) if Wix is unreachable or
credentials are unset — so the spec tab keeps working offline, the same way
wix_client.list_projects() degrades to [].

Collection IDs (Wix auto-assigned at import; tracked here):
    Spec Parts    -> Import5
    Spec Sections -> Import6
    Spec Clauses  -> Import7

The shape returned by load_spec_data() matches what spec_engine.build_spec()
expects: {"parts": [...], "sections": [...], "clauses": [...]}.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

_WIX_API_BASE = "https://www.wixapis.com/wix-data/v2"

# Collection IDs as assigned by Wix on import (see module docstring).
_PARTS_ID = "Import5"
_SECTIONS_ID = "Import6"
_CLAUSES_ID = "Import7"

_HTTP_TIMEOUT_SECONDS = 6
_CACHE_TTL_SECONDS = 600  # spec text changes rarely; 10 min is plenty

_SEED_PATH = Path(__file__).resolve().parent / "spec_seed.json"

# In-memory per-worker cache of the merged data bundle.
_cache: dict = {"data": None, "fetched_at": 0.0}


def _credentials() -> Optional[tuple[str, str]]:
    api_key = os.environ.get("WIX_API_KEY")
    site_id = os.environ.get("WIX_SITE_ID")
    if not api_key or not site_id:
        return None
    return api_key, site_id


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


def invalidate_cache() -> None:
    _cache["data"] = None
    _cache["fetched_at"] = 0.0


def _cache_fresh() -> bool:
    return (_cache["data"] is not None
            and time.time() - _cache["fetched_at"] < _CACHE_TTL_SECONDS)


def _query_all(collection_id: str, headers: dict) -> list[dict]:
    """Return every data row from a collection (cursor-paged, capped)."""
    items: list[dict] = []
    cursor: Optional[str] = None
    page = 0
    while page < 10:
        body: dict = {
            "dataCollectionId": collection_id,
            "query": {"paging": {"limit": 100}},
        }
        if cursor:
            body["query"]["cursorPaging"] = {"cursor": cursor}
            body["query"].pop("paging", None)
        resp = requests.post(
            f"{_WIX_API_BASE}/items/query",
            headers=headers, json=body, timeout=_HTTP_TIMEOUT_SECONDS,
        )
        if not resp.ok:
            log.error("Wix spec query %s HTTP %s: %s",
                      collection_id, resp.status_code, resp.text[:300])
            break
        payload = resp.json()
        for it in payload.get("dataItems", []):
            items.append(it.get("data", {}) or {})
        meta = payload.get("pagingMetadata", {})
        nxt = (meta.get("cursors") or {}).get("next")
        if not nxt or not meta.get("hasNext"):
            break
        cursor = nxt
        page += 1
    return items


def _load_seed() -> dict:
    """Bundled fallback. Always present in the repo."""
    try:
        return json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.error("spec_seed.json unreadable: %s", e)
        return {"parts": [], "sections": [], "clauses": []}


def load_spec_data(force_refresh: bool = False) -> dict:
    """Return {"parts","sections","clauses"} for spec_engine.build_spec().

    Tries Wix first; falls back to the bundled seed on any failure. Cached
    per-worker for _CACHE_TTL_SECONDS.
    """
    if not force_refresh and _cache_fresh():
        return _cache["data"]

    headers = _headers()
    if headers is None:
        log.warning("Wix credentials unset; spec data from bundled seed.")
        data = _load_seed()
        _cache["data"] = data
        _cache["fetched_at"] = time.time()
        return data

    try:
        parts = _query_all(_PARTS_ID, headers)
        sections = _query_all(_SECTIONS_ID, headers)
        clauses = _query_all(_CLAUSES_ID, headers)
        # If Wix returned nothing (e.g. all queries failed), fall back.
        if not clauses:
            log.warning("Wix returned no clauses; using bundled seed.")
            data = _load_seed()
        else:
            data = {"parts": parts, "sections": sections, "clauses": clauses}
        _cache["data"] = data
        _cache["fetched_at"] = time.time()
        return data
    except requests.RequestException as e:
        log.error("Wix spec fetch failed: %s — using bundled seed.", e)
        return _load_seed()
