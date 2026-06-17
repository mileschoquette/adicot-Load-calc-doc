"""Google Drive read+write client.

Fetches the Design Master HVAC HTML export for a given Wix project from the
firm's Google Drive, AND writes generated PDF deliverables back to the same
project's 6-Submit folder.

Path conventions:

    [Adicot Shared Drive]\\1-Job\\{Company}\\{Job No}\\4-Design\\dm_hvac-loads1.html  (read)
    [Adicot Shared Drive]\\1-Job\\{Company}\\{Job No}\\6-Submit\\<file>.pdf            (write)

Where:
    {Company} = the first hyphen-separated token of Job No
                ("2YA-Dr Bermudez" → "2YA")
    {Job No}  = the full Wix Job No, used verbatim as the folder name

Public functions:
    find_html(job_no)              -> (filename, bytes) | None
    get_submit_folder(job_no, ...) -> {"folder_id", "folder_url"} | None
    upload_files(job_no, files)    -> structured report (see docstring)
    diagnose(job_no)               -> structured per-step report
    invalidate_cache()             -> clear cached folder IDs

Authentication
--------------
Reads credentials from the GOOGLE_SERVICE_ACCOUNT_JSON env var. That var
should contain the entire JSON key file as a string. If it's missing or
malformed, all functions return None / [] silently — the Flask app keeps
working without Drive integration.

The service account must have **Editor** access on 1-Job (not just Viewer)
for uploads to work. Read-only access is sufficient for find_html.

The 1-Job folder MUST live on the 'Adicot' Shared Drive (not personal My Drive).
Service accounts do not have personal storage quota and cannot write to
personal Drive folders. Using corpora="allDrives" with
includeItemsFromAllDrives=True and supportsAllDrives=True on every API
call ensures the service account can see and write to Shared Drive folders.

Caching
-------
We cache the folder ID for each level we've walked (1-Job, each company
subfolder, each job folder) for 15 minutes. Drive folder IDs are stable;
the cache just avoids hitting the API for paths we've recently resolved.
Per-job file content is NOT cached.
"""

from __future__ import annotations

import io
import json
import logging
import mimetypes
import os
import time
from typing import Iterable, Optional

log = logging.getLogger(__name__)

_HTML_FILENAME = "dm_hvac-loads1.html"
# Name of the Shared Drive that holds the jobs tree. Overridable via env.
_SHARED_DRIVE_NAME = os.environ.get("GDRIVE_SHARED_DRIVE_NAME", "Adicot")
# Top-level jobs folder inside the Shared Drive. Overridable via env.
_ROOT_FOLDER_NAME = os.environ.get("GDRIVE_ROOT_FOLDER_NAME", "1-job")
_DESIGN_FOLDER_NAME = "4-Design"
_SUBMIT_FOLDER_NAME = "6-Submit"
_FOLDER_MIME = "application/vnd.google-apps.folder"

# Cache: maps "parent_id/child_name" → child_folder_id, with TTL
_folder_cache: dict[str, tuple[float, str]] = {}
# Cache: shared-drive name → drive_id, with TTL
_drive_id_cache: dict[str, tuple[float, str]] = {}
_CACHE_TTL_SECONDS = 15 * 60

# Cached service instance (per worker)
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
        SCOPES = ["https://www.googleapis.com/auth/drive"]
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
    """Force re-resolution of all folder paths."""
    _folder_cache.clear()
    _drive_id_cache.clear()


def _find_child(service, parent_id: str, name: str,
                mime_type: Optional[str] = None) -> Optional[dict]:
    """Find a direct child of parent_id with the given exact name.

    Uses allDrives corpora so the service account can see Shared Drive
    folders (service accounts have no personal quota and cannot access
    personal My Drive content owned by others).
    """
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
            pageSize=2,
            spaces="drive",
            corpora="allDrives",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
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


def _ensure_folder(service, parent_id: str, name: str) -> Optional[str]:
    """Find a subfolder by name, creating it if it doesn't exist.
    Returns the folder ID or None on error."""
    existing = _resolve_folder(service, parent_id, name)
    if existing:
        return existing
    try:
        meta = {"name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id]}
        created = service.files().create(
            body=meta,
            fields="id",
            supportsAllDrives=True,
        ).execute()
        new_id = created["id"]
        _cache_set(parent_id, name, new_id)
        return new_id
    except Exception as e:
        log.error("Could not create folder '%s' under %s: %s", name, parent_id, e)
        return None


def _parse_company_from_job_no(job_no: str) -> Optional[str]:
    """Pull the company token off the front of a Job No.

    "2YA-Dr Bermudez"       → "2YA"
    "BPCI-Smith Industrial" → "BPCI"
    "YA-260526"             → "YA"
    ""                      → None
    "NoHyphen"              → None
    """
    if not job_no:
        return None
    if "-" not in job_no:
        return None
    return job_no.split("-", 1)[0].strip() or None


def _find_shared_drive_id(service) -> Optional[str]:
    """Find the ID of the Shared Drive named _SHARED_DRIVE_NAME. Cached.

    Service accounts cannot write to personal My Drive (no storage quota),
    so the jobs tree must live on a Shared Drive the account is a member of.
    """
    cached = _drive_id_cache.get(_SHARED_DRIVE_NAME)
    if cached:
        ts, val = cached
        if time.time() - ts <= _CACHE_TTL_SECONDS:
            return val
        del _drive_id_cache[_SHARED_DRIVE_NAME]

    try:
        resp = service.drives().list(
            pageSize=100,
            fields="drives(id, name)",
        ).execute()
    except Exception as e:
        log.error("Shared Drive lookup failed: %s", e)
        return None

    for d in resp.get("drives", []):
        if d.get("name") == _SHARED_DRIVE_NAME:
            _drive_id_cache[_SHARED_DRIVE_NAME] = (time.time(), d["id"])
            return d["id"]

    log.error("Shared Drive '%s' not visible to service account. "
              "Verify the account is a member of that Shared Drive.",
              _SHARED_DRIVE_NAME)
    return None


def _find_one_jobs_root(service) -> Optional[str]:
    """Find the root jobs folder (_ROOT_FOLDER_NAME) inside the Adicot
    Shared Drive, returning its folder ID.

    The Shared Drive's top-level items have the drive's ID as their parent,
    so we resolve the root folder as a direct child of the drive root.
    """
    drive_id = _find_shared_drive_id(service)
    if not drive_id:
        return None
    root_id = _resolve_folder(service, drive_id, _ROOT_FOLDER_NAME)
    if not root_id:
        log.error("'%s' folder not found at the root of Shared Drive '%s'.",
                  _ROOT_FOLDER_NAME, _SHARED_DRIVE_NAME)
    return root_id


def _walk_to_job(service, job_no: str) -> Optional[str]:
    """Walk 1-Job → company → job_no, returning the job folder ID."""
    company = _parse_company_from_job_no(job_no)
    if not company:
        return None
    root_id = _find_one_jobs_root(service)
    if not root_id:
        return None
    company_id = _resolve_folder(service, root_id, company)
    if not company_id:
        return None
    return _resolve_folder(service, company_id, job_no)


# ────────────────────────────────────────────────────────────────────
# Public: find_html
# ────────────────────────────────────────────────────────────────────

def find_html(job_no: str) -> Optional[tuple[str, bytes]]:
    """Locate and download the DM HVAC HTML for a given Job No.

    Walks: Adicot Shared Drive → 1-Job → {company} → {job_no} → 4-Design → file

    Returns (filename, bytes) or None if anything in that chain fails.
    """
    if not job_no:
        log.info("find_html called with empty job_no")
        return None

    service = _build_service()
    if service is None:
        return None

    job_id = _walk_to_job(service, job_no)
    if not job_id:
        log.info("Job folder not found for '%s'", job_no)
        return None

    design_id = _resolve_folder(service, job_id, _DESIGN_FOLDER_NAME)
    if not design_id:
        log.info("'%s' folder not found under job '%s'",
                 _DESIGN_FOLDER_NAME, job_no)
        return None

    html_file = _find_child(service, design_id, _HTML_FILENAME)
    if not html_file:
        log.info("'%s' not found in '%s' for job '%s'",
                 _HTML_FILENAME, _DESIGN_FOLDER_NAME, job_no)
        return None

    try:
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError:
        log.error("googleapiclient.http not available")
        return None

    try:
        request = service.files().get_media(fileId=html_file["id"],
                                             supportsAllDrives=True)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return (_HTML_FILENAME, buf.getvalue())
    except Exception as e:
        log.error("Failed to download file %s: %s", html_file["id"], e)
        return None


# ────────────────────────────────────────────────────────────────────
# Public: get_submit_folder
# ────────────────────────────────────────────────────────────────────

def get_submit_folder(job_no: str,
                      create_if_missing: bool = False) -> Optional[dict]:
    """Resolve the 6-Submit folder for the given job.

    Returns {"folder_id": str, "folder_url": str} on success, None on failure.
    If create_if_missing=True, creates the 6-Submit folder when absent.
    """
    if not job_no:
        return None

    service = _build_service()
    if service is None:
        return None

    job_id = _walk_to_job(service, job_no)
    if not job_id:
        return None

    if create_if_missing:
        submit_id = _ensure_folder(service, job_id, _SUBMIT_FOLDER_NAME)
    else:
        submit_id = _resolve_folder(service, job_id, _SUBMIT_FOLDER_NAME)
    if not submit_id:
        return None

    return {
        "folder_id": submit_id,
        "folder_url": f"https://drive.google.com/drive/folders/{submit_id}",
    }


def list_submit_files(job_no: str) -> list[dict]:
    """List files in the job's 6-Submit folder as [{id, name, mimeType}].
    Returns [] on any failure (Drive disabled, folder missing, etc.)."""
    if not job_no:
        return []
    service = _build_service()
    if service is None:
        return []
    submit = get_submit_folder(job_no, create_if_missing=False)
    if not submit:
        return []
    try:
        resp = service.files().list(
            q=f"'{submit['folder_id']}' in parents and trashed = false",
            fields="files(id, name, mimeType)",
            pageSize=200,
            spaces="drive",
            corpora="allDrives",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ).execute()
    except Exception as e:
        log.error("Drive list 6-Submit failed for '%s': %s", job_no, e)
        return []
    return resp.get("files", [])


def download_file_bytes(file_id: str) -> Optional[bytes]:
    """Download a Drive file's bytes by id. None on failure."""
    if not file_id:
        return None
    service = _build_service()
    if service is None:
        return None
    try:
        from googleapiclient.http import MediaIoBaseDownload
        request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()
    except Exception as e:
        log.error("Drive download %s failed: %s", file_id, e)
        return None


# ── Manual folder browse + locate HTML (when auto-search by Job No fails) ──

def one_jobs_root_id() -> Optional[str]:
    """Folder id of the 1-job root on the shared drive — the browser's start point."""
    service = _build_service()
    if service is None:
        return None
    return _find_one_jobs_root(service)


def list_child_folders(parent_id: str) -> list[dict]:
    """Subfolders of a folder as [{id, name}], name-sorted. [] on any failure."""
    if not parent_id:
        return []
    service = _build_service()
    if service is None:
        return []
    try:
        resp = service.files().list(
            q=f"'{parent_id}' in parents and trashed = false "
              f"and mimeType = '{_FOLDER_MIME}'",
            fields="files(id, name)", pageSize=500, spaces="drive",
            corpora="allDrives", includeItemsFromAllDrives=True,
            supportsAllDrives=True, orderBy="name",
        ).execute()
    except Exception as e:
        log.error("Drive list child folders of %s failed: %s", parent_id, e)
        return []
    return [{"id": f["id"], "name": f.get("name", "")} for f in resp.get("files", [])]


def _first_html_child(service, parent_id: str) -> Optional[dict]:
    """First HTML file directly in parent_id, preferring the canonical DM filename."""
    if not parent_id:
        return None
    try:
        resp = service.files().list(
            q=f"'{parent_id}' in parents and trashed = false "
              f"and mimeType != '{_FOLDER_MIME}' and name contains '.htm'",
            fields="files(id, name)", pageSize=20, spaces="drive",
            corpora="allDrives", includeItemsFromAllDrives=True, supportsAllDrives=True,
        ).execute()
    except Exception as e:
        log.error("Drive html-search in %s failed: %s", parent_id, e)
        return None
    files = resp.get("files", [])
    for f in files:                       # prefer dm_hvac-loads1.html if present
        if f.get("name") == _HTML_FILENAME:
            return f
    return files[0] if files else None


def folder_has_html(folder_id: str) -> bool:
    """True if folder_id (or its 4-Design subfolder) contains an HTML file."""
    service = _build_service()
    if service is None or not folder_id:
        return False
    if _first_html_child(service, folder_id):
        return True
    design = _resolve_folder(service, folder_id, _DESIGN_FOLDER_NAME)
    return bool(design and _first_html_child(service, design))


def find_html_in_folder(folder_id: str) -> Optional[tuple[str, bytes]]:
    """Find a DM HTML in a manually-chosen folder — checks the folder itself and
    its 4-Design subfolder. Returns (filename, bytes) or None."""
    service = _build_service()
    if service is None or not folder_id:
        return None
    found = _first_html_child(service, folder_id)
    if not found:
        design = _resolve_folder(service, folder_id, _DESIGN_FOLDER_NAME)
        if design:
            found = _first_html_child(service, design)
    if not found:
        return None
    data = download_file_bytes(found["id"])
    if data is None:
        return None
    return (found.get("name") or _HTML_FILENAME, data)


# ────────────────────────────────────────────────────────────────────
# Public: upload_files
# ────────────────────────────────────────────────────────────────────

def upload_files(job_no: str,
                 files: Iterable[tuple[str, bytes, Optional[str]]]) -> dict:
    """Upload one or more files to 1-Job/{company}/{job_no}/6-Submit/.

    Each `files` item is a tuple of (filename, content_bytes, mime_type_or_None).
    If mime_type is None, it's guessed from the extension.

    The 6-Submit folder is auto-created if it doesn't exist.

    If a file with the same name already exists in 6-Submit, it is OVERWRITTEN
    (the existing Drive file is updated in-place so its file ID, sharing
    settings, and version history are preserved).

    Returns:
        {
          "ok": bool,
          "folder_id": str | None,
          "folder_url": str | None,
          "uploaded": [
              {"name": str, "file_id": str, "web_link": str, "overwritten": bool}
          ],
          "errors": [
              {"name": str | None, "stage": str, "message": str}
          ],
        }
    """
    files = list(files)
    result = {
        "ok": False,
        "folder_id": None,
        "folder_url": None,
        "uploaded": [],
        "errors": [],
    }

    service = _build_service()
    if service is None:
        result["errors"].append({
            "name": None,
            "stage": "auth",
            "message": "GOOGLE_SERVICE_ACCOUNT_JSON missing or invalid",
        })
        return result

    submit = get_submit_folder(job_no, create_if_missing=True)
    if not submit:
        result["errors"].append({
            "name": None,
            "stage": "folder",
            "message": (f"Could not resolve or create 6-Submit folder for "
                        f"job '{job_no}'. Check that 1-Job/<company>/{job_no}/ "
                        f"exists on a Shared Drive and the service account has "
                        f"Editor access."),
        })
        return result

    submit_id = submit["folder_id"]
    result["folder_id"] = submit_id
    result["folder_url"] = submit["folder_url"]

    try:
        from googleapiclient.http import MediaIoBaseUpload
    except ImportError:
        result["errors"].append({
            "name": None,
            "stage": "auth",
            "message": "googleapiclient.http not available",
        })
        return result

    for entry in files:
        try:
            filename, content, mime = entry
        except (ValueError, TypeError):
            result["errors"].append({
                "name": None,
                "stage": "input",
                "message": f"Bad file tuple: {entry!r}",
            })
            continue

        if not mime:
            guessed, _ = mimetypes.guess_type(filename)
            mime = guessed or "application/octet-stream"
            if filename.lower().endswith(".pdf"):
                mime = "application/pdf"

        try:
            existing = _find_child(service, submit_id, filename)

            media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime,
                                      resumable=False)

            if existing:
                updated = service.files().update(
                    fileId=existing["id"],
                    media_body=media,
                    fields="id, webViewLink",
                    supportsAllDrives=True,
                ).execute()
                result["uploaded"].append({
                    "name": filename,
                    "file_id": updated["id"],
                    "web_link": updated.get("webViewLink")
                                or f"https://drive.google.com/file/d/{updated['id']}/view",
                    "overwritten": True,
                })
            else:
                meta = {"name": filename, "parents": [submit_id]}
                created = service.files().create(
                    body=meta,
                    media_body=media,
                    fields="id, webViewLink",
                    supportsAllDrives=True,
                ).execute()
                result["uploaded"].append({
                    "name": filename,
                    "file_id": created["id"],
                    "web_link": created.get("webViewLink")
                                or f"https://drive.google.com/file/d/{created['id']}/view",
                    "overwritten": False,
                })
        except Exception as e:
            log.exception("Drive upload failed for '%s'", filename)
            result["errors"].append({
                "name": filename,
                "stage": "upload",
                "message": f"{type(e).__name__}: {e}",
            })

    result["ok"] = bool(result["uploaded"]) and not result["errors"]
    return result


# ────────────────────────────────────────────────────────────────────
# Public: diagnose
# ────────────────────────────────────────────────────────────────────

def diagnose(job_no: str) -> dict:
    """Return a structured diagnosis of why find_html / upload_files might
    be failing. Used by the /debug/gdrive-fetch route."""
    out: dict = {
        "job_no": job_no,
        "credentials": "unknown",
        "company_parsed": None,
        "one_jobs_found": None,
        "company_folder_found": None,
        "job_folder_found": None,
        "design_folder_found": None,
        "submit_folder_found": None,
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

    root_id = _find_one_jobs_root(service)
    out["one_jobs_found"] = bool(root_id)
    if not root_id:
        out["error"] = (f"'{_ROOT_FOLDER_NAME}' folder not visible to service "
                        f"account. Verify the '{_SHARED_DRIVE_NAME}' Shared "
                        f"Drive exists, contains '{_ROOT_FOLDER_NAME}', and the "
                        f"service account is a member with Editor access.")
        return out

    company_id = _resolve_folder(service, root_id, company)
    out["company_folder_found"] = bool(company_id)
    if not company_id:
        return out

    job_id = _resolve_folder(service, company_id, job_no)
    out["job_folder_found"] = bool(job_id)
    if not job_id:
        return out

    design_id = _resolve_folder(service, job_id, _DESIGN_FOLDER_NAME)
    out["design_folder_found"] = bool(design_id)

    submit_id = _resolve_folder(service, job_id, _SUBMIT_FOLDER_NAME)
    out["submit_folder_found"] = bool(submit_id)

    if design_id:
        html_file = _find_child(service, design_id, _HTML_FILENAME)
        out["html_file_found"] = bool(html_file)

        if html_file:
            try:
                from googleapiclient.http import MediaIoBaseDownload
                request = service.files().get_media(fileId=html_file["id"],
                                                     supportsAllDrives=True)
                buf = io.BytesIO()
                downloader = MediaIoBaseDownload(buf, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                out["file_size_bytes"] = len(buf.getvalue())
            except Exception as e:
                out["error"] = f"Download failed: {e}"

    return out
