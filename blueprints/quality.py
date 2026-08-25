"""Duct Sizing, Quality Check, and Charts tabs. Charts-tab combined-PDF
rebuilding reuses the helpers defined in blueprints/job_lifecycle.py (the
same bookkeeping the PDF-generation routes there use) rather than
duplicating it here."""

from __future__ import annotations

import traceback

from flask import Blueprint, flash, redirect, render_template, request, url_for

from hvac import hvac_pipeline as hp
from hvac import roof_check
from hvac import room_qc
from integrations import gdrive_client

from core import _job_dir, _load_meta, _load_report, _require_auth, _require_parsed, _save_meta
from blueprints.job_lifecycle import _available_charts, _rebuild_combined

quality = Blueprint("quality", __name__)


# ─── Duct Sizing tab ───────────────────────────────────────────────────

def _room_type_tag(loc: str) -> str:
    low = (loc or "").lower()
    if "bath" in low: return "bath"
    if "rr" in low or "restroom" in low: return "rr or corridor"
    if "toilet" in low: return "toilet"
    if "wic" in low: return "WIC"
    if "corridor" in low: return "Corridor"
    return ""


@quality.route("/job/<job_id>/duct")
@_require_auth
@_require_parsed
def job_duct(job_id: str):
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)
    report = _load_report(job_id)

    rows = []
    current_zone_index = -1
    for sa in report.get("supply_air", []):
        loc = (sa.get("location") or "").strip()
        is_zone = hp.is_zone(loc)
        if is_zone:
            current_zone_index += 1

        required_raw = sa.get("required_supply_cfm") or 0
        if is_zone:
            current_raw = None
        else:
            current_val = sa.get("current_supply_cfm") or 0
            try:
                current_raw = int(current_val) if float(current_val).is_integer() \
                              else float(current_val)
            except (TypeError, ValueError):
                current_raw = 0

        rows.append({
            "zone_index": current_zone_index,
            "is_zone": is_zone,
            "location": loc if is_zone
                        else f"    Room {loc.replace('Room ', '', 1).strip()}",
            "required": f"{required_raw:,.0f}",
            "required_raw": required_raw,
            "current": f"{current_raw:,.0f}" if current_raw is not None else "",
            "current_raw": current_raw,
            "room_type": _room_type_tag(loc),
        })

    return render_template(
        "job_duct.html",
        active_tab="duct", job_id=job_id, meta=meta, supply_rows=rows,
    )


# ─── Quality Check tab ─────────────────────────────────────────────────

@quality.route("/job/<job_id>/quality")
@_require_auth
@_require_parsed
def job_quality(job_id: str):
    """Room name vs. room-type ('definition') consistency check.

    Compares each room's typed name (Room Info Part 1 'Number' column) against
    the ventilation type it was assigned ('Name' column) and surfaces anything
    that doesn't confidently match, so the engineer can confirm it."""
    meta = _load_meta(job_id)
    report = _load_report(job_id)
    qc = room_qc.check_rooms(report.get("rooms_p1") or [])

    flagged = qc["flagged"]
    groups = {
        "mismatch":           [f for f in flagged if f["status"] == "mismatch"],
        "missing_definition": [f for f in flagged if f["status"] == "missing_definition"],
        "unverified":         [f for f in flagged if f["status"] == "unverified"],
    }

    roof = roof_check.check_roof_area(report, meta.get("num_stories"))

    return render_template(
        "job_quality.html",
        active_tab="quality", job_id=job_id, meta=meta,
        checked=qc["checked"],
        flagged_count=len(flagged),
        ok_count=qc["checked"] - len(flagged),
        groups=groups,
        roof=roof,
    )


@quality.route("/job/<job_id>/quality/stories", methods=["POST"])
@_require_auth
@_require_parsed
def job_quality_stories(job_id: str):
    """Save the engineer-entered number of stories, then re-run the checks."""
    meta = _load_meta(job_id)
    raw = (request.form.get("num_stories") or "").strip()
    if raw == "":
        meta.pop("num_stories", None)
    else:
        try:
            meta["num_stories"] = int(float(raw))
        except ValueError:
            flash("Number of stories must be a whole number.")
            return redirect(url_for(".job_quality", job_id=job_id))
    _save_meta(job_id, meta)
    return redirect(url_for(".job_quality", job_id=job_id))


# ─── Charts tab ─────────────────────────────────────────────────────────

@quality.route("/job/<job_id>/charts")
@_require_auth
@_require_parsed
def job_charts(job_id: str):
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)
    charts = _available_charts(job_dir)
    selected = meta.get("combined_charts") or []
    return render_template(
        "job_charts.html",
        active_tab="charts", job_id=job_id, meta=meta,
        charts=charts, selected=selected,
    )


@quality.route("/job/<job_id>/charts/select", methods=["POST"])
@_require_auth
def job_charts_select(job_id: str):
    """Save the chart selection, rebuild the combined PDF, and re-push just the
    combined to Drive so 6-Submit stays in sync with the selection."""
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)

    available = {c["name"] for c in _available_charts(job_dir)}
    selected = [n for n in request.form.getlist("charts") if n in available]
    meta["combined_charts"] = selected
    _save_meta(job_id, meta)

    combined = _rebuild_combined(job_dir, meta)

    # Re-push only the combined PDF to Drive (if the job is linked to Wix).
    pushed = False
    cms_job_no = ((meta.get("cms_snapshot") or {}).get("jobNo") or "").strip()
    drive_folder_id = meta.get("drive_folder_id")
    if (combined and combined.exists()
            and (drive_folder_id
                 or (cms_job_no and gdrive_client._parse_company_from_job_no(cms_job_no)))):
        try:
            result = gdrive_client.upload_files(
                cms_job_no,
                [(combined.name, combined.read_bytes(), "application/pdf")],
                folder_id=drive_folder_id,
            )
            pushed = bool(result.get("ok"))
        except Exception:
            traceback.print_exc()

    n = len(selected)
    msg = f"Combined PDF rebuilt with {n} chart{'s' if n != 1 else ''}."
    if pushed:
        msg += " Re-uploaded to Drive."
    flash(msg)
    return redirect(url_for("job_lifecycle.results", job_id=job_id))
