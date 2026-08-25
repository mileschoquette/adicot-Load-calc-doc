"""Public legal pages (required for the QuickBooks app listing), the /debug/*
diagnostics, the Drive-availability API used by the ★ tab, and the /crop
intake-snippet cropper used by the Apps Script side of project intake."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from flask import Blueprint, abort, jsonify, render_template, request
from werkzeug.utils import secure_filename

from integrations import daily_digest
from integrations import gdrive_client
from pdf import pdf_crop

from core import CROP_MAX_BYTES, HAS_EQUIP_SELECTOR, _EQUIP_IMPORT_ERROR, _cms, _crop_authorized, _is_stale, _require_auth

misc = Blueprint("misc", __name__)


# ─── /crop (intake snippet cropper) ────────────────────────────────────
# Adicot intake snippet cropper. Apps Script POSTs a client drawing PDF plus the
# _sources boxes; this returns one small JPEG per box (base64). Apps Script then
# uploads the crops to the project's Drive folder. This route does NO Drive work.
#
# Auth: token (CROP_TOKEN env var), NOT the basic-auth used elsewhere — Apps
# Script can't do basic auth cleanly. The route is exempt from @_require_auth.
#
# Size: the global MAX_CONTENT_LENGTH (5 MB) is too small for drawing PDFs, so
# this route reads the raw body itself and is not bound by request.form parsing.
# Send ONE PDF per request (keeps the base64 well under Apps Script's 50 MB cap).
#
# Requires: from pdf import pdf_crop
# requirements.txt:        pymupdf>=1.24
# Render env:              CROP_TOKEN = <long random string> (same value in Apps
#                          Script Script Properties as CROP_TOKEN)

@misc.route("/crop", methods=["POST"])
def crop_route():
    """Body (JSON):
        {
          "pdf_b64":  "<base64 of one drawing PDF>",
          "sources":  { field: { "page": n, "bbox": [x,y,w,h] }, ... },
          "fields":   ["roofRValue", ...]   // optional whitelist (final-record fields)
          "overlay":  false                 // optional; true = debug page overlay
        }
    Returns crop_sources() output, or overlay_pages() output when overlay=true.
    """
    if not _crop_authorized(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    raw = request.get_data(cache=False, as_text=False)
    if not raw:
        return jsonify({"ok": False, "error": "empty body"}), 400
    if len(raw) > CROP_MAX_BYTES:
        return jsonify({"ok": False, "error": "payload too large"}), 413

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as e:
        return jsonify({"ok": False, "error": f"bad json: {e}"}), 400

    pdf_b64 = payload.get("pdf_b64") or ""
    sources = payload.get("sources") or {}
    only_fields = payload.get("fields") or None
    overlay = bool(payload.get("overlay"))

    if not pdf_b64:
        return jsonify({"ok": False, "error": "no pdf_b64"}), 400
    try:
        pdf_bytes = base64.b64decode(pdf_b64)
    except Exception as e:
        return jsonify({"ok": False, "error": f"bad pdf_b64: {e}"}), 400

    try:
        if overlay:
            result = pdf_crop.overlay_pages(pdf_bytes, sources, only_fields=only_fields)
        else:
            result = pdf_crop.crop_sources(pdf_bytes, sources, only_fields=only_fields)
    except Exception as e:
        return jsonify({"ok": False, "error": f"crop failed: {e}"}), 500

    return jsonify(result)


# ─── Debug routes ──────────────────────────────────────────────────────

@misc.route("/debug/equip-status")
@_require_auth
def _debug_equip_status():
    return jsonify({
        "has_equip_selector": HAS_EQUIP_SELECTOR,
        "import_error": _EQUIP_IMPORT_ERROR,
        "hvac_selector_path": str(Path(__file__).parent.parent / "hvac" / "hvac_selector.py"),
        "hvac_selector_exists": (Path(__file__).parent.parent / "hvac" / "hvac_selector.py").exists(),
        "equipment_db_path": str(Path(__file__).parent.parent / "equipment_db.xlsx"),
        "equipment_db_exists": (Path(__file__).parent.parent / "equipment_db.xlsx").exists(),
    })


# NOTE: this route used to be defined twice (two different view functions,
# both bound to GET /debug/wix-projects) — a leftover from an earlier pass at
# the auto-expire diagnostic. This is the consolidated version: it keeps the
# cache-invalidating, credentials-checking general Wix-connectivity shape
# (the other candidate was a narrower one-off for validating _is_stale()
# against real createdDate values, explicitly marked "safe to remove once
# expiry is confirmed working"). Its is_stale angle is folded in below so
# nothing is lost.
@misc.route("/debug/wix-projects")
@_require_auth
def debug_wix_projects():
    """Diagnostic: raw list_projects() output (first 20) plus whether the Wix
    credentials are configured and each project's computed is_stale(), so Wix
    connectivity/auto-expire behavior can be checked without direct API access."""
    _cms.invalidate_cache()
    projects = _cms.list_projects()
    return jsonify({
        "count": len(projects),
        "credentials_set": {
            "WIX_API_KEY": bool(os.environ.get("WIX_API_KEY")),
            "WIX_SITE_ID": bool(os.environ.get("WIX_SITE_ID")),
        },
        "first_20": [
            {**p, "is_stale": _is_stale(p.get("createdDate"))}
            for p in projects[:20]
        ],
    })


@misc.route("/debug/get-project/<wix_id>")
@_require_auth
def debug_get_project(wix_id: str):
    """Temporary diagnostic: shows what _cms.get_project() returns for a
    given id, and whether it's found in list_projects() too, to debug why
    /job/<id>/star 404s for a project that appears in the digest/landing list."""
    return jsonify({
        "wix_id": wix_id,
        "secure_filename_matches": secure_filename(wix_id) == wix_id,
        "in_list_projects": any(p.get("_id") == wix_id for p in _cms.list_projects()),
        "get_project_result": _cms.get_project(wix_id),
    })


@misc.route("/debug/send-digest", methods=["GET", "POST"])
@_require_auth
def debug_send_digest():
    """Temporary: manually fire today's jobs digest email on demand, to verify
    SMTP config and content without waiting for the 7 AM scheduler tick. Accepts
    GET too so it can be triggered by just visiting the URL in a browser."""
    ok = daily_digest.send_daily_digest()
    return jsonify({"ok": ok})


@misc.route("/debug/gdrive-fetch")
@_require_auth
def _debug_gdrive_fetch():
    job_no = request.args.get("job_no", "").strip()
    if not job_no:
        return jsonify({
            "error": "pass a ?job_no= query parameter",
            "example": "/debug/gdrive-fetch?job_no=2YA-Dr%20Bermudez",
        }), 400
    gdrive_client.invalidate_cache()
    return jsonify(gdrive_client.diagnose(job_no))


# ─── API: check Drive for project HTML ────────────────────────────────

@misc.route("/api/check-drive")
@_require_auth
def api_check_drive():
    item_id = request.args.get("cms_item_id", "").strip()
    if not item_id:
        return jsonify({"status": "no_cms_id"}), 400

    record = _cms.get_project(item_id)
    if not record:
        return jsonify({"status": "cms_lookup_failed", "message": "Couldn't read the CMS project record."})

    job_no = (record.get("jobNo") or "").strip()
    if not job_no:
        return jsonify({"status": "no_job_no", "message": "This CMS project has no Job No."})

    company = gdrive_client._parse_company_from_job_no(job_no)
    expected_path = (f"1-job/{company}/{job_no}/4-Design/dm_hvac-loads1.html"
                     if company else f"1-job/?/{job_no}/4-Design/dm_hvac-loads1.html")

    diag = gdrive_client.diagnose(job_no)

    if diag.get("html_file_found") and diag.get("file_size_bytes"):
        return jsonify({
            "status": "found",
            "filename": "dm_hvac-loads1.html",
            "size_bytes": diag["file_size_bytes"],
            "path": expected_path,
            "job_no": job_no,
        })

    where_failed = "unknown"
    for key in ("one_jobs_found", "company_folder_found",
                "job_folder_found", "design_folder_found", "html_file_found"):
        if diag.get(key) is False:
            where_failed = key
            break

    return jsonify({
        "status": "not_found",
        "expected_path": expected_path,
        "where_failed": where_failed,
        "error": diag.get("error"),
        "job_no": job_no,
    })


@misc.route("/api/drive/folders")
@_require_auth
def api_drive_folders():
    """Folder browser data: subfolders of `parent` (or the 1-job root if omitted),
    plus whether the parent itself already contains an HTML. Used by the ★ page
    when the auto Job-No search can't find the file."""
    parent = request.args.get("parent", "").strip()
    if not parent:
        root = gdrive_client.one_jobs_root_id()
        if not root:
            return jsonify({"ok": False,
                            "error": "Couldn't reach the 1-job root on Google Drive."})
        return jsonify({"ok": True, "parent_id": root, "is_root": True,
                        "folders": gdrive_client.list_child_folders(root),
                        "has_html": False})
    return jsonify({"ok": True, "parent_id": parent, "is_root": False,
                    "folders": gdrive_client.list_child_folders(parent),
                    "has_html": gdrive_client.folder_has_html(parent)})


# ─── Public legal pages (required for the QuickBooks app listing) ─────
# These are intentionally NOT behind @_require_auth — Intuit (and the public)
# must be able to load them to verify the EULA / Privacy Policy URLs.

_EULA_HTML = """
<p><em>Last updated: 2026.</em></p>
<p>This End-User License Agreement ("Agreement") governs use of the internal HVAC
load-calculation and invoicing application (the "Application") operated by
Adicot, Inc. ("Adicot"). The Application is provided solely for the internal
business use of Adicot and its authorized personnel.</p>
<h3>License</h3>
<p>Adicot grants authorized users a limited, non-transferable, revocable license
to use the Application for preparing engineering deliverables and managing
invoices for Adicot's own projects. The Application is not offered to or licensed
for use by the general public.</p>
<h3>Acceptable use</h3>
<p>Users may not attempt to access data they are not authorized to view, disrupt
the Application, or use it for any unlawful purpose.</p>
<h3>Third-party services</h3>
<p>The Application integrates with third-party services (including Intuit
QuickBooks Online, Google Drive, and Wix) under Adicot's own accounts and solely
to perform Adicot's internal workflows.</p>
<h3>No warranty</h3>
<p>The Application is provided "as is" without warranties of any kind. Adicot is
not liable for any damages arising from its use.</p>
<h3>Contact</h3>
<p>Questions: <a href="mailto:agc@adicot.com">agc@adicot.com</a>.</p>
"""

_PRIVACY_HTML = """
<p><em>Last updated: 2026.</em></p>
<p>Adicot, Inc. ("we", "us") operates this internal application to prepare
engineering deliverables and create invoices for our own projects. This policy
explains what data the Application accesses and how it is used.</p>
<h3>Information we access</h3>
<ul>
  <li><strong>Project &amp; client information</strong> from our own systems
      (Wix CMS and Google Drive) used to generate engineering documents.</li>
  <li><strong>QuickBooks Online data</strong> — accessed via Intuit's API under
      our own QuickBooks company, limited to customers, products/services, and
      invoices, solely to create and manage invoices in our own books.</li>
</ul>
<h3>How we use it</h3>
<p>Data is used only to perform Adicot's internal engineering and billing
workflows. We do not sell or share it with third parties, and we do not use
QuickBooks data for advertising or any purpose beyond invoicing within our own
QuickBooks company.</p>
<h3>Storage &amp; security</h3>
<p>Access tokens and operational data are stored on Adicot's hosted
infrastructure and protected behind authentication. Access is limited to
authorized Adicot personnel.</p>
<h3>Data retention &amp; revocation</h3>
<p>The QuickBooks connection can be disconnected at any time from within the
Application or from QuickBooks, which revokes its access.</p>
<h3>Contact</h3>
<p>Questions: <a href="mailto:agc@adicot.com">agc@adicot.com</a>.</p>
"""

_LEGAL_PAGES = {
    "eula":    ("End-User License Agreement", _EULA_HTML),
    "privacy": ("Privacy Policy", _PRIVACY_HTML),
}


@misc.route("/legal/<doc>")
def legal_page(doc: str):
    page = _LEGAL_PAGES.get(doc)
    if not page:
        abort(404)
    title, body = page
    return render_template("legal.html", title=title, body=body)
