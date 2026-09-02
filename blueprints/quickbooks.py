"""QuickBooks Online connection (OAuth admin) and invoice creation/attachment,
including the Drive 6-Submit file picker the invoice modal uses."""

from __future__ import annotations

import json
import mimetypes
import secrets
from typing import Optional

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from integrations import gdrive_client
from integrations import quickbooks_client as qbo

from core import (
    _cms, _load_invoice_registry, _require_auth, _safe_job_path,
    _save_invoice_registry,
)

quickbooks = Blueprint("quickbooks", __name__)


@quickbooks.route("/quickbooks")
@_require_auth
def quickbooks_admin():
    status = qbo.connection_status()
    company = qbo.company_info() if status.get("connected") else None
    return render_template("quickbooks.html", status=status, company=company)


@quickbooks.route("/quickbooks/connect")
@_require_auth
def quickbooks_connect():
    if not qbo.is_configured():
        flash("QuickBooks isn't configured — set QBO_CLIENT_ID, QBO_CLIENT_SECRET, "
              "and QBO_REDIRECT_URI in the environment.")
        return redirect(url_for(".quickbooks_admin"))
    state = secrets.token_urlsafe(24)
    session["qbo_state"] = state
    return redirect(qbo.authorize_url(state))


@quickbooks.route("/quickbooks/callback")
@_require_auth
def quickbooks_callback():
    if request.args.get("error"):
        flash(f"QuickBooks authorization was cancelled: {request.args.get('error')}")
        return redirect(url_for(".quickbooks_admin"))

    state = request.args.get("state", "")
    expected = session.pop("qbo_state", None)
    if not state or state != expected:
        flash("QuickBooks authorization failed (state mismatch) — please try again.")
        return redirect(url_for(".quickbooks_admin"))

    code = request.args.get("code", "").strip()
    realm_id = request.args.get("realmId", "").strip()
    if not code or not realm_id:
        flash("QuickBooks authorization failed: missing code or company id.")
        return redirect(url_for(".quickbooks_admin"))

    if qbo.exchange_code(code, realm_id):
        flash("Connected to QuickBooks ✓")
    else:
        flash("QuickBooks token exchange failed — check the Render logs.")
    return redirect(url_for(".quickbooks_admin"))


@quickbooks.route("/quickbooks/disconnect", methods=["POST"])
@_require_auth
def quickbooks_disconnect():
    qbo.disconnect()
    flash("Disconnected from QuickBooks.")
    return redirect(url_for(".quickbooks_admin"))


def _suggest_customer_id(customers: list[dict], company: str, code: str,
                         email: str) -> Optional[str]:
    """Best-guess QBO customer for a CMS project, matching (in priority order)
    email → exact company/name → client-code prefix → partial company. The modal
    pre-selects this; the engineer confirms. Returns a customer id or None."""
    email = (email or "").strip().lower()
    company = (company or "").strip().lower()
    code = (code or "").strip().lower()

    if email:
        for c in customers:
            if (c.get("email") or "").strip().lower() == email:
                return c["id"]
    if company:
        for c in customers:
            if company in ((c.get("name") or "").lower(), (c.get("company") or "").lower()):
                return c["id"]
    if code:
        for c in customers:
            nm = (c.get("name") or "").lower()
            if nm == code or nm.startswith(code + " ") or nm.startswith(code + "-"):
                return c["id"]
    if company:
        for c in customers:
            nm, comp = (c.get("name") or "").lower(), (c.get("company") or "").lower()
            if company in nm or company in comp:
                return c["id"]
    return None


@quickbooks.route("/api/qbo/lists")
@_require_auth
def api_qbo_lists():
    """Live QBO customers + service items for the modal dropdowns."""
    if not qbo.connection_status().get("connected"):
        return jsonify({"connected": False, "customers": [], "items": []})
    return jsonify({"connected": True,
                    "customers": qbo.list_customers(),
                    "items": qbo.list_service_items()})


@quickbooks.route("/api/qbo/customers", methods=["POST"])
@_require_auth
def api_qbo_create_customer():
    """Create a new QBO customer from the invoice modal's inline 'add client' form."""
    if not qbo.connection_status().get("connected"):
        return jsonify({"ok": False, "error": "Not connected to QuickBooks."}), 400
    display_name = request.form.get("display_name", "").strip()
    company = request.form.get("company", "").strip()
    email = request.form.get("email", "").strip()
    if not display_name:
        return jsonify({"ok": False, "error": "Display name is required."}), 400
    result = qbo.create_customer(display_name, company_name=company, email=email)
    return jsonify(result), (200 if result.get("ok") else 502)


def _job_drive_folder_id(wix_id: str) -> Optional[str]:
    """The manually-chosen Drive job-folder id saved on a project (from its job
    meta), or None. Safe to call even if the job has no workspace yet."""
    p = _safe_job_path(wix_id)
    if not p.exists():
        return None
    try:
        return json.loads((p / "meta.json").read_text()).get("drive_folder_id")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _drive_submit_files(job_no: str, folder_id: Optional[str] = None) -> list[dict]:
    """Every file in the job's Google Drive 6-Submit folder as [{id, name}], any
    type. `folder_id` (the chosen job folder) overrides the Job-No name walk."""
    files = [{"id": f.get("id"), "name": f.get("name") or ""}
             for f in gdrive_client.list_submit_files(job_no, folder_id=folder_id)]
    files.sort(key=lambda p: p["name"].lower())
    return files


def _attach_drive_files(invoice_id: str, job_no: str, file_ids: list[str],
                        folder_id: Optional[str] = None):
    """Download the selected 6-Submit files from Drive and attach them to the QBO
    invoice. Only ids that belong to this job's 6-Submit folder are honored
    (whitelist). Returns (attached_names, errors)."""
    index = {f["id"]: f["name"]
             for f in _drive_submit_files(job_no, folder_id=folder_id) if f.get("id")}
    attached, errors = [], []
    for fid in file_ids:
        name = index.get(fid)
        if not name:                       # not a file from this project's 6-Submit
            continue
        data = gdrive_client.download_file_bytes(fid)
        if data is None:
            errors.append({"name": name, "error": "Drive download failed"})
            continue
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        res = qbo.attach_file(invoice_id, name, data, content_type=content_type)
        if res.get("ok"):
            attached.append(name)
        else:
            errors.append({"name": name, "error": res.get("error")})
    return attached, errors


@quickbooks.route("/api/qbo/prepare/<wix_id>")
@_require_auth
def api_qbo_prepare(wix_id: str):
    """Billing fields + a suggested customer for one project (modal pre-fill)."""
    rec = _cms.get_project(wix_id) or {}
    client_name = (rec.get("clientName") or "").strip()
    company = (rec.get("clientCompany") or "").strip()
    code = (rec.get("clientCode") or "").strip()
    email = (rec.get("clientEmail") or "").strip()
    job_no = (rec.get("jobNo") or "").strip()
    # Fall back to the Job No's leading token as a client code (e.g. "2YA-ALM" → "2YA").
    if not code and "-" in job_no:
        code = job_no.split("-", 1)[0].strip()

    suggested = None
    if qbo.connection_status().get("connected"):
        suggested = _suggest_customer_id(qbo.list_customers(), company, code, email)

    return jsonify({
        "job_no":      job_no,
        "client_name": client_name,
        "company":     company,
        "client_code": code,
        "email":       email,
        "total_cost":  rec.get("totalCost"),
        "description": (rec.get("productService") or rec.get("description") or "").strip(),
        "suggested_customer_id": suggested,
        # A manual flag is a placeholder, not a real invoice — it must not
        # block the modal, or a hand-flagged job could never be billed properly.
        "already_invoiced": bool(_load_invoice_registry().get(wix_id, {}).get("invoice_id")),
        "pdfs": _drive_submit_files(job_no, folder_id=_job_drive_folder_id(wix_id)),
    })


@quickbooks.route("/job/<wix_id>/invoice", methods=["POST"])
@_require_auth
def create_invoice_route(wix_id: str):
    """Create the QBO invoice for a project after the engineer confirms the modal."""
    if not qbo.connection_status().get("connected"):
        return jsonify({"ok": False, "error": "Not connected to QuickBooks."}), 400

    reg = _load_invoice_registry()
    # Only a real QBO invoice blocks a second one. A manual flag is overwritten
    # by the invoice it was standing in for.
    if reg.get(wix_id, {}).get("invoice_id"):
        return jsonify({"ok": False, "error": "This project has already been invoiced.",
                        "invoice": reg[wix_id]}), 409

    customer_id = request.form.get("customer_id", "").strip()
    item_id     = request.form.get("item_id", "").strip()
    amount      = request.form.get("amount", "").strip()
    description = request.form.get("description", "").strip()
    job_no      = request.form.get("job_no", "").strip()
    if not customer_id or not item_id or not amount:
        return jsonify({"ok": False,
                        "error": "Customer, service item, and amount are all required."}), 400

    memo = f"Job No: {job_no}" if job_no else ""

    # Server-side duplicate guard: if an invoice already carries this Job No memo
    # in QBO, don't create a second one (covers a lost/stale local registry).
    if memo:
        existing = qbo.find_invoice_by_memo(memo)
        if existing:
            rec = {"invoice_id": existing["id"], "doc_number": existing.get("doc_number"),
                   "url": qbo.invoice_url(existing["id"]), "job_no": job_no,
                   "customer_id": customer_id, "note": "matched existing invoice by Job No"}
            reg[wix_id] = rec
            _save_invoice_registry(reg)
            return jsonify({"ok": False, "error": "An invoice with this Job No already "
                            "exists in QuickBooks.", "invoice": rec}), 409

    # Fill the QBO "Job No" + "Project" custom fields from the Job No itself:
    #   Job No  → full job number       (e.g. "2YA-Yarbrough")
    #   Project → job number minus the company-code prefix (e.g. "Yarbrough")
    custom_fields = {}
    if job_no:
        project = job_no.split("-", 1)[1].strip() if "-" in job_no else job_no
        custom_fields = {"Job No": job_no, "Project": project}

    result = qbo.create_invoice(customer_id, item_id, amount,
                                description=description, memo=memo,
                                custom_fields=custom_fields)
    if not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error")}), 502

    invoice_id = result["invoice_id"]

    # Attach selected 6-Submit files from Drive (best-effort — a failed attach
    # never undoes the invoice).
    attached, attach_errors = _attach_drive_files(
        invoice_id, job_no, request.form.getlist("pdfs"),
        folder_id=_job_drive_folder_id(wix_id))

    rec = {
        "invoice_id":  invoice_id,
        "doc_number":  result.get("doc_number"),
        "total":       result.get("total"),
        "url":         qbo.invoice_url(invoice_id),
        "job_no":      job_no,
        "customer_id": customer_id,
        "attached":    attached,
    }
    reg[wix_id] = rec
    _save_invoice_registry(reg)
    return jsonify({"ok": True, "invoice": rec, "attached": attached,
                    "attach_errors": attach_errors})


@quickbooks.route("/job/<wix_id>/attach", methods=["POST"])
@_require_auth
def attach_to_invoice_route(wix_id: str):
    """Attach more PDFs to an already-created invoice (the 'Update' / Attach flow)."""
    if not qbo.connection_status().get("connected"):
        return jsonify({"ok": False, "error": "Not connected to QuickBooks."}), 400

    reg = _load_invoice_registry()
    rec = reg.get(wix_id)
    if not rec or not rec.get("invoice_id"):
        return jsonify({"ok": False, "error": "No invoice on record for this project."}), 404

    selected = request.form.getlist("pdfs")
    if not selected:
        return jsonify({"ok": False, "error": "Select at least one file to attach."}), 400

    # Job No drives the Drive 6-Submit lookup; fall back to the live Wix record.
    job_no = rec.get("job_no") or (_cms.get_project(wix_id) or {}).get("jobNo", "")
    attached, attach_errors = _attach_drive_files(
        rec["invoice_id"], job_no, selected, folder_id=_job_drive_folder_id(wix_id))

    if not attached:
        return jsonify({"ok": False,
                        "error": "Nothing attached." + (f" {attach_errors[0]['error']}"
                                 if attach_errors else ""),
                        "attach_errors": attach_errors}), 502

    rec["attached"] = sorted(set(rec.get("attached") or []) | set(attached))
    reg[wix_id] = rec
    _save_invoice_registry(reg)
    return jsonify({"ok": True, "attached": attached, "attach_errors": attach_errors})
