"""Google Sheets CMS client — replaces wix_client.py's role as the project database.

Wraps the Sheets API v4 for the operations the HVAC tool needs, backed by one
worksheet with one row per job:
  - list_projects()          — returns every project record (autocomplete, landing list)
  - get_project(item_id)     — returns one full project record
  - update_project(item_id, fields) — merges `fields` onto an existing row
  - append_project(fields)   — creates a new row, mints and returns its id
  - delete_project(item_id)  — permanently deletes a row

Batching contract
------------------
Every write in this module is ONE Sheets API call per operation, never one
call per cell. `update_project` reads the row once (from the cached full-sheet
read, or a fresh one if stale), merges the changed fields onto it in memory,
and pushes the WHOLE row back with a single `values().update()` call over one
A1 range. `append_project` builds the full row in memory and pushes it with a
single `values().append()` call. This matters because Sheets enforces a
per-minute write-request quota per project/user — writing 70 fields as 70
separate `update_cell`-style calls would burn that quota in a fraction of a
second on a busy day; writing them as one ranged call does not.

All functions return None / [] / False on any error (network, auth, bad
response, missing credentials) so callers can degrade gracefully when Sheets
is unreachable or under-permissioned — same contract as wix_client.py. Don't
raise from here.

Reads GOOGLE_SERVICE_ACCOUNT_JSON from environment (the same service-account
key gdrive_client.py already uses — Drive and Sheets can share one service
account, just need to be granted access to the target spreadsheet). Also
reads GOOGLE_SHEETS_SPREADSHEET_ID (required) and GOOGLE_SHEETS_WORKSHEET_NAME
(defaults to "Projects"). If credentials or the spreadsheet id are missing,
every function degrades to []/None/False — the tool keeps working without
Sheets integration in that case, same as it does today without Wix.

The header row (SHEET_COLUMNS below) is the single source of truth for column
order. It is written once by a human when the sheet is created; this module
never rewrites the header, it only reads/writes data rows beneath it.

A small in-memory TTL cache on list_projects() (same 5-minute window
wix_client.py uses) means the landing page and job_star() can call it on
every page load without hitting the Sheets API more than once every 5 min.
The cache is per-worker, which is fine — gunicorn workers stay warm.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ── Config from env ──────────────────────────────────────────────────
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_DEFAULT_WORKSHEET_NAME = "Projects"

# How long list_projects() results stay cached. Same rationale as
# wix_client.py: short enough that new rows (from intake, or the Apps Script
# writing directly into the Sheet) appear quickly, long enough that we don't
# hammer the API.
_LIST_CACHE_TTL_SECONDS = 30

# Header row — exact column order in the sheet. '_id' is column 0 on purpose:
# every existing call site (app.py) does record.get('_id'), carried over
# unchanged from the Wix _id convention, so the returned dicts must use this
# exact key to stay drop-in compatible with the code that used to call
# wix_client.
SHEET_COLUMNS = [
    "_id", "legacy_wix_id", "createdDate", "status", "workOrderComplete",
    "proposalSigned", "reviewComplete", "signedDate", "signedBy", "signedTitle",
    "gcAccepted", "totalCost", "jobNo", "title", "projectAddress",
    "propertyOwner", "owner", "clientName", "clientCompany", "clientEmail",
    "clientPhone", "productService", "clientCode", "subClient", "community",
    "subdivision", "locationDisambig", "lennarJobNo", "engagementDays",
    "buildingStatus", "sf", "occupants", "orientation", "indoorTemp",
    "indoorRH", "weatherStation", "deckType", "roofCover", "roofColor",
    "roofRValue", "insulPosition", "suspCeiling", "atticCond", "ceilingHeight",
    "wallFinish", "wallConstruction", "wallColor", "wallRValue", "wallHeight",
    "partConstruction", "partRValue", "floorType", "floorRValue", "glassU",
    # glassOperU..glassSGDSHGC and skylightU/skylightSHGC below are retired: the
    # work order now carries one glass U and one glass SHGC for all glass. The
    # names stay because rows are read and written positionally, so dropping a
    # column would shift every later one.
    "glassSHGC", "glassOperU", "glassOperSHGC", "glassSGDU", "glassSGDSHGC",
    "glassFrame", "glazingType", "glazingTint", "skylights", "doorType",
    "occupancyType", "lpdSpaceType", "lightingWattsPerSF", "equipWattsPerSF",
    "heatGenEquipment", "infiltration", "changeRate", "acNewExisting",
    "acMounting", "systemType", "hvacType", "heatType", "coolingEff",
    "heatingEff", "efficiencyTier", "manufacturer", "hasOutsideAir",
    "hasExhaust", "hasStrip", "heatStripCOP", "hwType", "hwEfficiency",
    "hwCapacityGal", "description", "projectFolder", "driveFolderUrl",
    "driveFolderId", "snippetRoofRValue", "snippetWallConstruction",
    "snippetGlassValues", "snippetCeilingHeight", "snippetLightingWsf",
    "snippetProjectAddress",
    "projectCity", "projectState", "projectZip", "projectCounty",
    "latitude", "elevation", "numStories",
    "extLightDescription", "extLightCategory", "extLightNumLuminaires",
    "extLightWattsPerLuminaire", "extLightAreaLengthUnits", "extLightControlType",
    "osaLowDry", "osaDailyRange",
    "awardPercent",
    "osaHighMonth", "osaHighDry", "osaHighWet",
    "glassMethod", "skylightU", "skylightSHGC", "doorU",
]
_COL_INDEX = {name: i for i, name in enumerate(SHEET_COLUMNS)}


def _worksheet_name() -> str:
    return os.environ.get("GOOGLE_SHEETS_WORKSHEET_NAME") or _DEFAULT_WORKSHEET_NAME


def _spreadsheet_id() -> Optional[str]:
    return os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID") or None


# ── Auth / service build (mirrors gdrive_client._build_service, but its own
#    cached instance — deliberately not sharing gdrive_client's, so this
#    module has no import-time dependency on gdrive_client) ──────────────
_service = None


def _build_service():
    global _service
    if _service is not None:
        return _service

    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        log.warning("GOOGLE_SERVICE_ACCOUNT_JSON not set; sheets_client disabled.")
        return None
    if not _spreadsheet_id():
        log.warning("GOOGLE_SHEETS_SPREADSHEET_ID not set; sheets_client disabled.")
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
        )
        _service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return _service
    except Exception as e:
        log.error("Failed to build Sheets service: %s", e)
        return None


# ── In-memory cache for list_projects ────────────────────────────────
_list_cache: dict = {"data": None, "fetched_at": 0.0}


def _cache_fresh() -> bool:
    return (_list_cache["data"] is not None
            and time.time() - _list_cache["fetched_at"] < _LIST_CACHE_TTL_SECONDS)


def invalidate_cache() -> None:
    """Force the next list_projects() call to hit Sheets. Called after every
    successful write so readers see the change immediately, and available for
    /debug routes or tests."""
    _list_cache["data"] = None
    _list_cache["fetched_at"] = 0.0


def _row_to_dict(row: list) -> dict:
    """Zip a raw sheet row against SHEET_COLUMNS; short rows fill '' for
    trailing columns (Sheets omits trailing empty cells from a values.get)."""
    padded = row + [""] * (len(SHEET_COLUMNS) - len(row))
    return dict(zip(SHEET_COLUMNS, padded[:len(SHEET_COLUMNS)]))


def _dict_to_row(record: dict) -> list:
    return [record.get(col, "") for col in SHEET_COLUMNS]


def _col_letter(idx: int) -> str:
    """0-based column index -> A1 column letters (0 -> 'A', 25 -> 'Z', 26 -> 'AA')."""
    letters = ""
    n = idx + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _last_col_letter() -> str:
    return _col_letter(len(SHEET_COLUMNS) - 1)


# ── Public API ───────────────────────────────────────────────────────

def list_projects() -> list[dict]:
    """Return every project record as a list of dicts keyed by SHEET_COLUMNS.

    Returns [] on any failure — including missing credentials/spreadsheet id.
    Callers (the landing list, job_star, etc.) just see no projects in that
    case, matching wix_client.list_projects()'s degrade-gracefully contract.
    """
    if _cache_fresh():
        return _list_cache["data"]

    service = _build_service()
    if service is None:
        return []

    try:
        rng = f"{_worksheet_name()}!A2:{_last_col_letter()}"
        resp = service.spreadsheets().values().get(
            spreadsheetId=_spreadsheet_id(),
            range=rng,
        ).execute()
        rows = resp.get("values", [])
        items = [_row_to_dict(r) for r in rows if any(c not in (None, "") for c in r)]

        _list_cache["data"] = items
        _list_cache["fetched_at"] = time.time()
        return items

    except Exception as e:
        log.error("Sheets list_projects failed: %s", e)
        return []


def get_project(item_id: str) -> Optional[dict]:
    """Fetch one full project record by its _id.

    Served from the list_projects() cache (refreshing it first if stale), so
    this stays a single batched read per cache window rather than a
    per-lookup API call. Returns None if not found or on any error.
    """
    if not item_id:
        return None
    items = list_projects()
    for item in items:
        if item.get("_id") == item_id:
            return item
    return None


def _find_row_number(service, item_id: str) -> Optional[int]:
    """1-based sheet row number (including the header) for item_id, or None.
    Always does a fresh read (not the cache) since callers need this right
    before a write."""
    try:
        rng = f"{_worksheet_name()}!A2:A"
        resp = service.spreadsheets().values().get(
            spreadsheetId=_spreadsheet_id(),
            range=rng,
        ).execute()
        ids = resp.get("values", [])
        for i, row in enumerate(ids):
            if row and row[0] == item_id:
                return i + 2  # +1 for header row, +1 for 1-based indexing
        return None
    except Exception as e:
        log.error("Sheets row lookup for '%s' failed: %s", item_id, e)
        return None


def _sheet_id_for_worksheet(service) -> Optional[int]:
    """Numeric sheetId (needed for batchUpdate/deleteDimension) for the
    configured worksheet name."""
    try:
        meta = service.spreadsheets().get(
            spreadsheetId=_spreadsheet_id(),
            fields="sheets.properties",
        ).execute()
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == _worksheet_name():
                return props.get("sheetId")
        return None
    except Exception as e:
        log.error("Sheets metadata lookup failed: %s", e)
        return None


def update_project(item_id: str, fields: dict) -> bool:
    """Merge `fields` onto item_id's existing row and write the WHOLE row back
    in exactly one values().update() call — never one call per changed field.

    Returns False if item_id isn't found or on any error. On success,
    invalidates the cache so the next read reflects the change.
    """
    if not item_id or not fields:
        return False
    service = _build_service()
    if service is None:
        return False

    row_num = _find_row_number(service, item_id)
    if row_num is None:
        log.warning("Sheets update_project: '%s' not found.", item_id)
        return False

    current = get_project(item_id) or {"_id": item_id}
    merged = {**current, **fields, "_id": item_id}
    row_values = _dict_to_row(merged)

    try:
        rng = f"{_worksheet_name()}!A{row_num}:{_last_col_letter()}{row_num}"
        service.spreadsheets().values().update(
            spreadsheetId=_spreadsheet_id(),
            range=rng,
            valueInputOption="RAW",
            body={"values": [row_values]},
        ).execute()
        invalidate_cache()
        return True
    except Exception as e:
        log.error("Sheets update_project('%s') failed: %s", item_id, e)
        return False


def append_project(fields: dict) -> Optional[str]:
    """Create a new row, minting a fresh _id. Returns the new id, or None on
    any error (including missing credentials). Writes the full row in exactly
    one values().append() call."""
    service = _build_service()
    if service is None:
        return None

    new_id = secrets.token_hex(8)
    record = {col: "" for col in SHEET_COLUMNS}
    record.update(fields)
    record["_id"] = new_id
    record.setdefault("status", "Pending Review")
    if not record.get("status"):
        record["status"] = "Pending Review"
    if not record.get("createdDate"):
        record["createdDate"] = datetime.now(timezone.utc).isoformat()

    row_values = _dict_to_row(record)

    try:
        rng = f"{_worksheet_name()}!A:{_last_col_letter()}"
        service.spreadsheets().values().append(
            spreadsheetId=_spreadsheet_id(),
            range=rng,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row_values]},
        ).execute()
        invalidate_cache()
        return new_id
    except Exception as e:
        log.error("Sheets append_project failed: %s", e)
        return None


def delete_project(item_id: str) -> bool:
    """Permanently delete a project row. Returns True on success, False on
    any error — same degrade-gracefully contract as wix_client.delete_project."""
    if not item_id:
        return False
    service = _build_service()
    if service is None:
        return False

    row_num = _find_row_number(service, item_id)
    if row_num is None:
        log.warning("Sheets delete_project: '%s' not found.", item_id)
        return False

    sheet_id = _sheet_id_for_worksheet(service)
    if sheet_id is None:
        log.error("Sheets delete_project: could not resolve sheetId for '%s'.",
                  _worksheet_name())
        return False

    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=_spreadsheet_id(),
            body={"requests": [{
                "deleteDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": row_num - 1,   # 0-based
                        "endIndex": row_num,
                    }
                }
            }]},
        ).execute()
        invalidate_cache()
        return True
    except Exception as e:
        log.error("Sheets delete_project('%s') failed: %s", item_id, e)
        return False
