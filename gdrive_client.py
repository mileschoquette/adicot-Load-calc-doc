"""Google Drive read-only client.

Fetches the Design Master HVAC HTML export for a given Wix project from the
firm's Google Drive, following the standard path convention:

    G:\\My Drive\\1-Jobs\\{Company}\\{Job No}\\4-Design\\dm_hvac-loads1.html

Where:
  - {Company} = the first hyphen-separated token of Job No
                ("2YA-Dr Bermudez" → "2YA")
  - {Job No}  = the full Wix Job No, used verbatim as the folder name

The public entry point is `find_html(job_no)`, which returns a (filename,
bytes) tuple or None if the file can't be found / read. Caller decides
what to do on None (the Flask app falls back to manual upload).

Authentication
--------------
Reads credentials from the GOOGLE_SERVICE_ACCOUNT_JSON env var. That var
should contain the entire JSON key file as a string. If it's missing or
malformed, find_html() returns None silently — the tool degrades to
manual-upload mode.

Caching
-------
We cache the folder ID for each level we've walked (1-Jobs, each company
subfolder, each job folder) for 15 minutes. Drive folder IDs are stable;
the cache just avoids hitting the API for paths we've recently resolved.
Per-job HTML content is NOT cached — every fetch is fresh, since engineers
re-export from DM and we don't want stale results.

Drive API quirks
----------------
- Drive doesn't have real paths. Files have IDs and parent references.
  To resolve "1-Jobs/2YA/2YA-Dr Bermudez/4-Design/dm_hvac-loads1.html"
  we make 5 sequential API calls walking the hierarchy.
- Folder name matching is case-sensitive and exact (no trim). If someone
  names a folder "2YA-Dr Bermudez " (trailing space), the lookup fails.
- A folder/file with no parent or in trash is invisible to us.
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
from typing import Optional

log = logging.getLogger(__name__)

# These libraries are part of google-api-python-client. We import lazily
# inside the functions so that importing this module doesn't crash if the
# package isn't installed yet — the Flask app keeps booting and the
# gdrive feature just doesn't activate.
_HTML_FILENAME = "dm_hvac-loads1.html"
_ROOT_FOLDER_NAME = "1-Jobs"
_DESIGN_FOLDER_NAME = "4-Design"

_FOLDER_MIME = "application/vnd.google-apps.folder"

# Cache: maps "parent_id/child_name" → child_folder_id, with TTL
_folder_cache: dict[str, tuple[float, str]] = {}
_CACHE_TTL_SECONDS = 15 * 60

# Cached service instance (per worker). Service objects are thread-safe
# for our usage (we only read).
_service = None


def _build_service():
    """Create (or return cached) authenticated Drive service.

    Returns None if credentials aren't set or libraries aren't installed,
    so callers can fall back gracefully.
    """
    global _service
    if _service is not None:
        return _service

    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        log.warning("GOOGLE_SERVICE_ACCOUNT_JSON not set; gdrive disabled.")
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
        # Drive.readonly is sufficient — we only download files. Using a
        # narrower scope than full Drive access reduces blast radius if
        # the credential ever leaked.
        SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES,
        )
        _service = build("drive", "v3", credentials=creds,
                         cache_discovery=False)
        return _service
    except Exception as e:
        log.error("Failed to build Drive service: %s", e)
        return None


def _cache_get(parent_id: str, name: str) -> Optional[str]:
    key = f"{parent_id}/{name}"
    entry = _folder_cache.get(key)
    if not entry:
        return None
    ts, val = entry
    if time.time() - ts > _CACHE_TTL_SECONDS:
        del _folder_cache[key]
        return None
    return val


def _cache_set(parent_id: str, name: str, child_id: str) -> None:
    _folder_cache[f"{parent_id}/{name}"] = (time.time(), child_id)


def invalidate_cache() -> None:
    """Force re-resolution of all folder paths. Useful for /debug routes."""
    _folder_cache.clear()


def _find_child(service, parent_id: str, name: str,
                mime_type: Optional[str] = None) -> Optional[dict]:
    """Find a direct child of parent_id with the given exact name.

    Returns the child's file metadata dict, or None if not found.
    If mime_type is given, only matches that mime type. For folders,
    pass mime_type=_FOLDER_MIME so we don't accidentally match files.

    Note: Drive's `name` query is exact but allows multiple matches if
    two siblings share a name. We take the first.
    """
    # Drive's query language. Single quotes around name need escaping:
    # if the folder is "Dr O'Brien" the apostrophe must be escaped as \\'.
    safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
    q_parts = [
        f"'{parent_id}' in parents",
        f"name = '{safe_name}'",
        "trashed = false",
    ]
    if mime_type:
        q_parts.append(f"mimeType = '{mime_type}'")
    q = " and ".join(q_parts)

    try:
        resp = service.files().list(
            q=q,
            fields="files(id, name, mimeType)",
            pageSize=2,           # we only need to know if there's 1+ match
            spaces="drive",
            corpora="user",       # search My Drive (not shared drives)
        ).execute()
    except Exception as e:
        log.error("Drive query failed for '%s' in %s: %s", name, parent_id, e)
        return None

    files = resp.get("files", [])
    if not files:
        return None
    if len(files) > 1:
        log.warning("Multiple matches for '%s' in %s; using first.", name, parent_id)
    return files[0]


def _resolve_folder(service, parent_id: str, name: str) -> Optional[str]:
    """Find a subfolder by name, returning its ID. Cached."""
    cached = _cache_get(parent_id, name)
    if cached:
        return cached
    found = _find_child(service, parent_id, name, mime_type=_FOLDER_MIME)
    if not found:
        return None
    _cache_set(parent_id, name, found["id"])
    return found["id"]


def _parse_company_from_job_no(job_no: str) -> Optional[str]:
    """Pull the company token off the front of a Job No.

    "2YA-Dr Bermudez" → "2YA"
    "BPCI-Smith Industrial" → "BPCI"
    "YA-260526" → "YA"           (date-style; not the new convention but
                                  we tolerate it so old jobs don't crash)
    "" → None
    "NoHyphen" → None             (can't determine company)
    """
    if not job_no:
        return None
    if "-" not in job_no:
        return None
    return job_no.split("-", 1)[0].strip() or None


def find_html(job_no: str) -> Optional[tuple[str, bytes]]:
    """Locate and download the DM HVAC HTML for a given Job No.

    Walks: My Drive → 1-Jobs → {company} → {job_no} → 4-Design → file
    Returns (filename, bytes) or None if anything in that chain fails.

    On None, the caller (Flask /upload-from-drive route) should respond
    with a "couldn't find it, here's where we looked, upload manually"
    message rather than a 500 — this is an expected outcome for jobs
    that don't follow the convention yet.
    """
    if not job_no:
        log.info("find_html called with empty job_no")
        return None

    company = _parse_company_from_job_no(job_no)
    if not company:
        log.info("Could not parse company from job_no '%s'", job_no)
        return None

    service = _build_service()
    if service is None:
        return None

    # Step 1: find the 1-Jobs root folder in My Drive
    # The service account sees this folder because we shared it explicitly.
    # Its "parent" from the service account's perspective is its own root,
    # so we search globally for a folder with the right name that the SA
    # can see — there should only be one (the one we shared).
    try:
        resp = service.files().list(
            q=(f"name = '{_ROOT_FOLDER_NAME}' "
               f"and mimeType = '{_FOLDER_MIME}' "
               f"and trashed = false"),
            fields="files(id, name)",
            pageSize=5,
            spaces="drive",
            corpora="user",
        ).execute()
    except Exception as e:
        log.error("Drive lookup for 1-Jobs root failed: %s", e)
        return None

    root_files = resp.get("files", [])
    if not root_files:
        log.error("'%s' folder not visible to service account. "
                  "Verify it's shared with the service account email.",
                  _ROOT_FOLDER_NAME)
        return None
    if len(root_files) > 1:
        log.warning("Multiple '%s' folders visible; using first.",
                    _ROOT_FOLDER_NAME)
    one_jobs_id = root_files[0]["id"]

    # Step 2: walk down through company → job → 4-Design
    company_id = _resolve_folder(service, one_jobs_id, company)
    if not company_id:
        log.info("Company folder '%s' not found under 1-Jobs", company)
        return None

    job_id = _resolve_folder(service, company_id, job_no)
    if not job_id:
        log.info("Job folder '%s' not found under 1-Jobs/%s", job_no, company)
        return None

    design_id = _resolve_folder(service, job_id, _DESIGN_FOLDER_NAME)
    if not design_id:
        log.info("'%s' folder not found in 1-Jobs/%s/%s",
                 _DESIGN_FOLDER_NAME, company, job_no)
        return None

    # Step 3: find the HTML file in the design folder
    html_file = _find_child(service, design_id, _HTML_FILENAME)
    if not html_file:
        log.info("'%s' not found in 1-Jobs/%s/%s/%s",
                 _HTML_FILENAME, company, job_no, _DESIGN_FOLDER_NAME)
        return None

    # Step 4: download the file's bytes
    try:
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError:
        log.error("googleapiclient.http not available")
        return None

    try:
        request = service.files().get_media(fileId=html_file["id"])
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return (_HTML_FILENAME, buf.getvalue())
    except Exception as e:
        log.error("Failed to download file %s: %s", html_file["id"], e)
        return None


def diagnose(job_no: str) -> dict:
    """Return a structured diagnosis of why find_html might be failing.

    Used by the /debug/gdrive-fetch route to give us visibility into
    each step of the lookup chain without having to read server logs.
    Returns a dict with status indicators at each level.
    """
    out: dict = {
        "job_no": job_no,
        "credentials": "unknown",
        "company_parsed": None,
        "one_jobs_found": None,
        "company_folder_found": None,
        "job_folder_found": None,
        "design_folder_found": None,
        "html_file_found": None,
        "file_size_bytes": None,
        "error": None,
    }

    if not os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        out["credentials"] = "missing"
        out["error"] = "GOOGLE_SERVICE_ACCOUNT_JSON env var not set"
        return out
    out["credentials"] = "set"

    company = _parse_company_from_job_no(job_no)
    out["company_parsed"] = company
    if not company:
        out["error"] = f"could not parse company from job_no '{job_no}'"
        return out

    service = _build_service()
    if service is None:
        out["error"] = "failed to build Drive service (check credentials)"
        return out

    try:
        resp = service.files().list(
            q=(f"name = '{_ROOT_FOLDER_NAME}' "
               f"and mimeType = '{_FOLDER_MIME}' "
               f"and trashed = false"),
            fields="files(id, name)",
            pageSize=5,
            spaces="drive",
            corpora="user",
        ).execute()
    except Exception as e:
        out["error"] = f"Drive API call failed: {e}"
        return out

    root_files = resp.get("files", [])
    if not root_files:
        out["one_jobs_found"] = False
        out["error"] = ("'1-Jobs' folder not visible to service account. "
                        "Verify sharing.")
        return out
    out["one_jobs_found"] = True
    one_jobs_id = root_files[0]["id"]

    company_id = _resolve_folder(service, one_jobs_id, company)
    out["company_folder_found"] = bool(company_id)
    if not company_id:
        return out

    job_id = _resolve_folder(service, company_id, job_no)
    out["job_folder_found"] = bool(job_id)
    if not job_id:
        return out

    design_id = _resolve_folder(service, job_id, _DESIGN_FOLDER_NAME)
    out["design_folder_found"] = bool(design_id)
    if not design_id:
        return out

    html_file = _find_child(service, design_id, _HTML_FILENAME)
    out["html_file_found"] = bool(html_file)
    if not html_file:
        return out

    # Try the download too so we know that part works
    try:
        from googleapiclient.http import MediaIoBaseDownload
        request = service.files().get_media(fileId=html_file["id"])
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        out["file_size_bytes"] = len(buf.getvalue())
    except Exception as e:
        out["error"] = f"Download failed: {e}"

    return out
