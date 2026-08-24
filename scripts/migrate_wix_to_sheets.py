#!/usr/bin/env python3
"""One-off migration: copy every Wix Projects record into the new Google Sheet.

Run manually, once, by a human:

    python scripts/migrate_wix_to_sheets.py

Not imported by app.py. Reads from Wix via the existing `wix_client` module
(unchanged); writes to Sheets directly (not via `sheets_client.append_project`,
which appends one row at a time) so every migrated row lands in exactly ONE
Sheets API append() call instead of one call per project.

Requires env vars:
    WIX_API_KEY = "<same value already configured for the production Flask app>"
    WIX_SITE_ID = "<same value already configured for the production Flask app>"
    GOOGLE_SERVICE_ACCOUNT_JSON = "<full service-account JSON key, single string>"
    GOOGLE_SHEETS_SPREADSHEET_ID = "<the target spreadsheet id, from its URL>"
    GOOGLE_SHEETS_WORKSHEET_NAME = "Projects"

Output:
    Prints a summary (found / migrated / skipped).
    Writes scripts/migration_id_map.json: {legacy_wix_id: new_id, ...}
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wix_client  # noqa: E402  (repo root on sys.path, see above)
from sheets_client import SHEET_COLUMNS  # noqa: E402

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_WORKSHEET_NAME = os.environ.get("GOOGLE_SHEETS_WORKSHEET_NAME", "Projects")


def _plain_date(value):
    """Same unwrap wix_client._plain_date() does for Wix's {"$date": ...} shape."""
    if isinstance(value, dict):
        return value.get("$date") or ""
    if isinstance(value, str):
        return value
    return ""


def _build_sheets_service():
    """Same auth pattern as gdrive_client._build_service(), scoped to Sheets."""
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        print("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON not set.", file=sys.stderr)
        return None

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _row_for_record(wix_record: dict, new_id: str) -> list[str]:
    """Build one Sheet row (in SHEET_COLUMNS order) from a full Wix record dict."""
    row = []
    for col in SHEET_COLUMNS:
        if col == "_id":
            row.append(new_id)
            continue
        if col == "legacy_wix_id":
            row.append(str(wix_record.get("_id") or ""))
            continue
        val = wix_record.get(col)
        if col in ("createdDate", "signedDate") or (isinstance(val, dict) and "$date" in val):
            val = _plain_date(val if val is not None else wix_record.get("_" + col))
        if val is None:
            val = ""
        elif isinstance(val, bool):
            val = "TRUE" if val else "FALSE"
        elif not isinstance(val, str):
            val = str(val)
        row.append(val)
    return row


def main() -> int:
    print("Fetching project list from Wix...")
    light_records = wix_client.list_projects()
    print(f"Found {len(light_records)} project(s) in Wix.")

    rows: list[list[str]] = []
    id_map: dict[str, str] = {}
    skipped = 0

    for light in light_records:
        wix_id = (light.get("_id") or "").strip()
        if not wix_id:
            skipped += 1
            continue
        try:
            full = wix_client.get_project(wix_id)
            if not full:
                print(f"  WARNING: get_project({wix_id}) returned nothing; skipping.")
                skipped += 1
                continue
            new_id = secrets.token_hex(8)
            rows.append(_row_for_record(full, new_id))
            id_map[wix_id] = new_id
        except Exception as e:  # noqa: BLE001 - one bad record must not kill the run
            print(f"  WARNING: failed to migrate Wix record {wix_id}: {e}")
            skipped += 1
            continue

    print(f"Built {len(rows)} row(s) to migrate ({skipped} skipped).")

    if not rows:
        print("Nothing to migrate. Exiting.")
        return 0

    spreadsheet_id = os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID")
    if not spreadsheet_id:
        print("ERROR: GOOGLE_SHEETS_SPREADSHEET_ID not set.", file=sys.stderr)
        return 1

    service = _build_sheets_service()
    if service is None:
        return 1

    print(f"Appending {len(rows)} row(s) to '{_WORKSHEET_NAME}' in ONE API call...")
    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{_WORKSHEET_NAME}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()

    map_path = Path(__file__).resolve().parent / "migration_id_map.json"
    map_path.write_text(json.dumps(id_map, indent=2))

    print("Done.")
    print(f"  Found in Wix:  {len(light_records)}")
    print(f"  Migrated:      {len(rows)}")
    print(f"  Skipped:       {skipped}")
    print(f"  ID map written to: {map_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
