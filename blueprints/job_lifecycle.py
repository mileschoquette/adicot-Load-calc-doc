"""Per-job lifecycle: create/open a job, the Work Order (★) tab, the client
portal, parsing the DM HTML, and generating/regenerating the PDF
deliverables (including the combined-PDF + chart-appendix bookkeeping that
the Charts tab in blueprints/quality.py also reuses)."""

from __future__ import annotations

import datetime
import io
import os
import re
import secrets
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Optional

from flask import (Blueprint, abort, flash, redirect, render_template,
                    request, send_from_directory, url_for)
from werkzeug.utils import secure_filename

from hvac import hvac_pipeline as hp
from hvac import fenestration_defaults
from hvac import lpd_max
from hvac import validators
from integrations import email_client
from integrations import gdrive_client
from integrations import gmail_client
from integrations import portal_tokens
from integrations import quickbooks_client as qbo
from pdf import html_pdf
from pdf import pdf_combine

from core import (
    JOBS_DIR, MONTH_NAMES, PERSON_EMAILS, PORTAL_TOKEN_SECRET, _cms, _extract_state_code, _is_parsed, _job_dir,
    _load_invoice_registry, _load_meta, _num_or_default,
    _parse_and_persist, _render_preview, _require_auth, _require_parsed,
    _safe_job_path, _save_meta,
)

job_lifecycle = Blueprint("job_lifecycle", __name__)


def _refresh_cms_snapshot(job_id: str, meta: dict) -> dict:
    """Re-fetch a CMS job's live Sheet record into meta['cms_snapshot'] so
    edits made outside this page load (intake, or another admin's save) show
    up without requiring a full HTML reparse. No-op on a fetch failure so a
    transient Sheets error doesn't blank out the last-known snapshot."""
    record = _cms.get_project(meta.get("cms_item_id") or job_id)
    if record:
        meta["cms_snapshot"] = record
        _save_meta(job_id, meta)
    return meta

# ─── Work order field layout ──────────────────────────────────────────
# The work order, grouped to mirror the intake form. Each entry is (label, key)
# where key is the Wix CMS field key — or a list of candidate keys (first one
# present wins), used for newer fields whose exact key spelling isn't pinned down
# in the Velo source. Booleans render Yes/No; URL-valued keys render as links.
_WO_LINK_KEYS = {
    "driveFolderUrl", "snippetRoofRValue", "snippetWallConstruction",
    "snippetGlassValues", "snippetCeilingHeight", "snippetLightingWsf",
    "snippetProjectAddress",
}

_WORK_ORDER_SECTIONS = [
    ("Project & Client", [
        ("Job No", "jobNo"),
        ("Title", "title"),
        ("Project Address", "projectAddress"),
        ("City", "projectCity"),
        ("State", "projectState"),
        ("Zip", "projectZip"),
        ("County", "projectCounty"),
        ("Property Owner", "propertyOwner"),
        ("Client Name", "clientName"),
        ("Client Company", "clientCompany"),
        ("Client Email", "clientEmail"),
        ("Client Phone", "clientPhone"),
        ("Product / Service", "productService"),
        ("Price", "totalCost"),
        ("% Due at Award", "awardPercent"),
        ("Status", "status"),
        ("Client Code", "clientCode"),
        ("Sub Client", "subClient"),
        ("Community", "community"),
        ("Subdivision", "subdivision"),
        ("Location Disambig", "locationDisambig"),
        ("Engagement Days", "engagementDays"),
        ("Review Complete", "reviewComplete"),
        ("Signed Date", "signedDate"),
    ]),
    ("Building Basics", [
        ("Building Status", "buildingStatus"),
        ("Approx. Area (SF)", "sf"),
        ("Total Occupants", "occupants"),
        ("Primary Orientation", "orientation"),
        ("Indoor Design Temp (°F)", "indoorTemp"),
        ("Indoor Design RH (%)", "indoorRH"),
        ("Weather Station", "weatherStation"),
        ("Site Latitude", "latitude"),
        ("Site Elevation", "elevation"),
        ("Winter Design Dry-Bulb", "osaLowDry"),
        ("Summer Mean Daily Range", "osaDailyRange"),
        ("Hottest Month", "osaHighMonth"),
        ("Cooling Design Dry-Bulb 1% (°F)", "osaHighDry"),
        ("Mean Coincident Wet-Bulb 1% (°F)", "osaHighWet"),
        ("Number of Stories", "numStories"),
    ]),
    ("Roof & Ceiling", [
        ("Deck / Frame Type", "deckType"),
        ("Roof Covering", "roofCover"),
        ("Roof Color", "roofColor"),
        ("Roof R-Value", "roofRValue"),
        ("Insulation Position", "insulPosition"),
        ("Suspended Ceiling", "suspCeiling"),
        ("Attic / Plenum Condition", "atticCond"),
        ("Ceiling Height", "ceilingHeight"),
    ]),
    ("Walls, Floor & Glass", [
        ("Wall Finish", "wallFinish"),
        ("Wall Construction", "wallConstruction"),
        ("Wall Color", "wallColor"),
        ("Wall R-Value", "wallRValue"),
        ("Wall Height", "wallHeight"),
        ("Partition Construction", "partConstruction"),
        ("Partition R-Value", "partRValue"),
        ("Floor Type", "floorType"),
        ("Floor R-Value", "floorRValue"),
        ("Glass Entry Method", "glassMethod"),
        ("Glass Frame", "glassFrame"),
        ("Glazing Type", "glazingType"),
        ("Glazing Tint", "glazingTint"),
        ("Glass U-Factor", "glassU"),
        ("Glass SHGC", "glassSHGC"),
        ("Glass Operable U", "glassOperU"),
        ("Glass Operable SHGC", "glassOperSHGC"),
        ("Sliding Door U", ["glassSGDU", "glassSgdU"]),
        ("Sliding Door SHGC", ["glassSGDSHGC", "glassSgdSHGC"]),
        ("Skylights", "skylights"),
        ("Skylight U", "skylightU"),
        ("Skylight SHGC", "skylightSHGC"),
        ("Opaque Door Type", "doorType"),
        ("Opaque Door U", "doorU"),
    ]),
    ("Internal Loads", [
        ("Occupancy Type", "occupancyType"),
        ("LPD Space Type", "lpdSpaceType"),
        ("Lighting W/SF", "lightingWattsPerSF"),
        ("Equipment W/SF", "equipWattsPerSF"),
        ("Heat Generating Equipment", "heatGenEquipment"),
        ("Infiltration", "infiltration"),
        ("Change Rate", "changeRate"),
    ]),
    ("HVAC System", [
        ("New / Existing", "acNewExisting"),
        ("Mounting", "acMounting"),
        ("System Type", "systemType"),
        ("HVAC Type", "hvacType"),
        ("Heat Type", "heatType"),
        ("Cooling Eff", "coolingEff"),
        ("Heating Eff", "heatingEff"),
        ("Efficiency Tier", ["efficiencyTier", "efficiencytier"]),
        ("Manufacturer", "manufacturer"),
        ("Outside Air", "hasOutsideAir"),
        ("Exhaust", "hasExhaust"),
        ("Heat Strip", "hasStrip"),
        ("Heat Strip COP", "heatStripCOP"),
    ]),
    ("Water Heating", [
        ("HW Type", "hwType"),
        ("HW Efficiency", "hwEfficiency"),
        ("HW Capacity (Gal)", "hwCapacityGal"),
    ]),
    ("Exterior Lighting", [
        ("Description", "extLightDescription"),
        ("Category", "extLightCategory"),
        ("Number of Luminaires", "extLightNumLuminaires"),
        ("Watts per Luminaire", "extLightWattsPerLuminaire"),
        ("Area / Length / Units", "extLightAreaLengthUnits"),
        ("Control Type", "extLightControlType"),
    ]),
    ("Description / Notes", [
        ("Description", "description"),
    ]),
    ("Drive & Source Snippets", [
        ("Project Folder", "projectFolder"),
        ("Drive Folder", "driveFolderUrl"),
        ("Drive Folder ID", "driveFolderId"),
        ("Snippet — Roof R Value", "snippetRoofRValue"),
        ("Snippet — Wall Construction", "snippetWallConstruction"),
        ("Snippet — Glass Values", "snippetGlassValues"),
        ("Snippet — Ceiling Height", "snippetCeilingHeight"),
        ("Snippet — Lighting W/SF", "snippetLightingWsf"),
        ("Snippet — Project Address", "snippetProjectAddress"),
    ]),
]

_US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL",
    "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME",
    "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]

# ─── Fee structure ────────────────────────────────────────────────────
# awardPercent is the share of the fee due at award, entered as free text on
# the Work Order. The fee card and the client's payment-terms checkbox on
# portal.html are both derived from it, so a blank or unparseable value means
# 0: the all-due-at-delivery terms the proposal used before this field existed.
_HOURLY_NOTE = "Design changes are billed at $225/hr."


def _num(value) -> Optional[float]:
    """Lenient parse of a free-text money or percent string ("3,200", "50%").
    Keeps a leading minus so a negative reaches the caller's clamp instead of
    silently flipping sign."""
    try:
        return float(re.sub(r"[^\d.-]", "", str(value or "")))
    except ValueError:
        return None


def _fmt_money(amount: Optional[float]) -> str:
    """$3,200 for whole dollars, $3,200.50 otherwise, em dash for None."""
    if amount is None:
        return "\u2014"
    return f"${amount:,.0f}" if amount == int(amount) else f"${amount:,.2f}"


def _fee_terms(record: dict) -> dict:
    """Split totalCost by the record's awardPercent and pick the proposal
    wording that matches. Both fields are free text, so both parse leniently:
    an out-of-range percent clamps to 0-100, and an unparseable price leaves
    the amounts as em dashes while the wording still reflects the split."""
    pct = min(max(_num(record.get("awardPercent")) or 0.0, 0.0), 100.0)
    if pct >= 100:
        note = "Due in full at award, before work begins"
        terms = ("the full fee is due at award, before Adicot begins work. "
                 f"{_HOURLY_NOTE}")
    elif pct <= 0:
        note = "Due upon Adicot submitting client-approved documents"
        terms = ("full fee due upon Adicot submitting client-approved documents. "
                 f"{_HOURLY_NOTE}")
    else:
        note = (f"{pct:g}% due at award, balance due upon Adicot submitting "
                "client-approved documents")
        terms = (f"{pct:g}% of the fee is due at award and the balance upon Adicot "
                 f"submitting client-approved documents. {_HOURLY_NOTE}")

    total = _num(record.get("totalCost"))
    award = None if total is None else round(total * pct / 100, 2)
    return {
        "note":          note,
        "terms_label":   f"I agree to the payment terms: {terms}",
        "total_disp":    _fmt_money(total),
        "award_disp":    _fmt_money(award),
        "delivery_disp": _fmt_money(None if total is None else round(total - award, 2)),
    }


# The two ways to fill in glass properties, offered by the glassMethod dropdown
# and read by _apply_code_defaults below.
GLASS_DIRECT = "Enter U & SHGC directly"
GLASS_LOOKUP = "Look up from window type"

# The one list of allowed dropdown values. Drives jobs/star.html's admin
# dropdowns AND portal.html's client-facing selects (passed in by
# _render_portal), so the two pages can no longer drift apart — they used to
# keep separate hand-copied lists. Booleans use Yes/No since
# _work_order_sections() already renders bool values that way. Every field not
# listed here stays a plain text input.
#
# KEEP IN SYNC with the extraction prompt in archive/wix-snapshot/
# AdicotProjects.gs, which enumerates these same values so intake writes
# "Metal frame" rather than "metal". That file is a separate deployment and
# can't import this list.
_FIELD_OPTIONS = {
    "buildingStatus": ["Tenant Buildout", "New Construction", "New Addition", "Renovation"],
    "orientation": ["North", "Northeast", "East", "Southeast", "South", "Southwest", "West", "Northwest"],
    "deckType": ["Steel deck", "Concrete deck", "Wood deck", "Metal frame", "Wood frame", "Other"],
    "roofCover": ["TPO", "EPDM", "BUR/Modified bitumen", "Metal", "Tile", "Shingle", "Other"],
    "roofColor": ["Light", "Medium", "Dark"],
    "suspCeiling": ["Suspended ACT (T-bar)", "GWB", "Open to structure",
                    "Conditioned space above — no roof load", "Other"],
    "atticCond": ["Sealed/conditioned plenum", "Vented attic", "None (flat roof)", "Other"],
    "wallFinish": ["Stucco", "EIFS", "Brick", "Metal panel", "Other"],
    "wallConstruction": ["CMU", "CMU + rigid insul", "ICF", "Steel stud + batt",
                          "Steel stud + rigid insul", "Wood frame", "Other"],
    "wallColor": ["Light", "Medium", "Dark"],
    "partConstruction": ["NA", "Metal stud (steel frame)", "Wood stud", "Concrete Block / CMU"],
    "floorType": ["Slab on grade", "Floor over conditioned space", "Floor over unconditioned space",
                  "Wood frame over unconditioned space", "Elevated / exterior exposure"],
    "doorType": list(fenestration_defaults.DOOR_TYPES),
    "occupancyType": ["Dining / Fast food", "Food prep / Kitchen", "Office", "Retail", "Medical office",
                       "Assembly / Classrooms", "Warehouse", "Residential", "Other"],
    "infiltration": ["Tight", "Average", "Loose"],
    "acNewExisting": ["New", "Existing — reuse", "Mix of both"],
    "acMounting": ["Ground-level slab", "Rooftop", "Sidewall", "Other"],
    "hvacType": ["Split", "Package", "Mini-split", "VRF", "PTAC", "Other"],
    "heatType": ["Heat Pump", "Heat Strip", "Gas Furnace", "Other"],
    "hasOutsideAir": ["Yes", "No"],
    "hasExhaust": ["Yes", "No"],
    "hasStrip": ["Yes", "No"],
    "reviewComplete": ["Yes", "No"],
    "skylights": ["Yes", "No"],
    "osaHighMonth": list(MONTH_NAMES),
    "glassMethod": [GLASS_DIRECT, GLASS_LOOKUP],
    "glassFrame": list(fenestration_defaults.FRAME_TYPES),
    # "Triple" has no row in C303.1.3; it's offered so real jobs can be recorded,
    # and the lookup simply returns nothing for it.
    "glazingType": list(fenestration_defaults.GLAZING_TYPES) + ["Triple"],
    "glazingTint": list(fenestration_defaults.TINTS),
    "projectState": _US_STATES,
}

# Sections worth asking the client about in the "Save & Send to Client" draft
# email's missing-info block. Excludes free-text notes and internal
# Drive/snippet links, which aren't something the client can usefully fill in.
_EMAIL_MISSING_INFO_SECTIONS = {
    "Project & Client",
    "Building Basics", "Roof & Ceiling", "Walls, Floor & Glass",
    "Internal Loads", "HVAC System", "Water Heating", "Exterior Lighting",
}

# CRM/bookkeeping fields living inside an otherwise client-facing section.
# Adicot fills these in, so listing them as things "we still need from you"
# would be asking the client for our own paperwork, and for Project & Client
# would put the fee and the award split in front of them. Applied across every
# section since none of these keys appear outside Project & Client.
_EMAIL_SKIP_KEYS = {
    "jobNo", "title", "productService", "totalCost", "awardPercent", "status",
    "clientCode", "subClient", "community", "subdivision", "locationDisambig",
    "engagementDays", "reviewComplete", "signedDate",
    # ASHRAE design conditions: table lookups intake does from the project
    # address, not anything a client could answer if asked.
    "weatherStation", "latitude", "elevation", "osaLowDry", "osaDailyRange",
    "osaHighMonth", "osaHighDry", "osaHighWet",
}


def _wo_lookup(snap: dict, key):
    """Return the snapshot value for a field key (or first present of a list)."""
    keys = key if isinstance(key, (list, tuple)) else [key]
    for k in keys:                       # prefer a key with a real value
        if snap.get(k) not in (None, ""):
            return snap[k], k
    for k in keys:                       # else surface an explicit empty/false
        if k in snap:
            return snap[k], k
    return None, keys[0]


def _work_order_sections(snapshot: Optional[dict], include_empty: bool = False) -> list[dict]:
    """Build the grouped work order from a CMS (Wix or Sheets) snapshot. Booleans
    render Yes/No, URL fields render as links, and a section is dropped if all its
    rows are empty — unless include_empty=True (used for the editable admin form
    on job_star, where blank fields still need to render as empty inputs).

    Each row carries a 'key': the canonical (first/primary) field key even when
    the source entry lists alias keys — that's the name the editable form posts
    back under, and the single key sheets_client.update_project() writes to."""
    snap = snapshot or {}
    sections = []
    for title, fields in _WORK_ORDER_SECTIONS:
        rows = []
        has_value = False
        for label, key in fields:
            val, resolved_key = _wo_lookup(snap, key)
            canonical_key = key[0] if isinstance(key, (list, tuple)) else key
            if isinstance(val, bool):
                kind, display = "text", ("Yes" if val else "No")
                has_value = True
            else:
                display = ("" if val is None else str(val)).strip()
                if display and resolved_key in _WO_LINK_KEYS and display.startswith("http"):
                    kind = "link"
                else:
                    kind = "text"
                if display:
                    has_value = True
            rows.append({"label": label, "value": display, "kind": kind, "key": canonical_key,
                         "options": _FIELD_OPTIONS.get(canonical_key)})
        if has_value or include_empty:
            sections.append({"title": title, "rows": rows})
    return sections


def _missing_info_block(record: dict) -> str:
    """Plain-text, section-grouped bullet list of empty client-facing fields,
    for the 'Save & Send to Client' draft email. Empty string if nothing's
    missing."""
    lines = []
    for sec in _work_order_sections(record, include_empty=True):
        if sec["title"] not in _EMAIL_MISSING_INFO_SECTIONS:
            continue
        missing = [row["label"] for row in sec["rows"]
                   if not row["value"] and row["key"] not in _EMAIL_SKIP_KEYS]
        if missing:
            lines.append(f"{sec['title']}:")
            lines.extend(f"- {label}" for label in missing)
            lines.append("")
    if not lines:
        return ""
    while lines and lines[-1] == "":
        lines.pop()
    return "A few things we still need from you:\n\n" + "\n".join(lines) + "\n\n"


@job_lifecycle.route("/job/new-temp", methods=["POST"])
@_require_auth
def new_temp():
    """Create an empty temp-job workspace and land on its star page (upload mode)."""
    job_id = "temp_" + secrets.token_hex(6)
    job_dir = JOBS_DIR / job_id
    (job_dir / "out" / "charts").mkdir(parents=True, exist_ok=True)
    meta = {
        "source":          "temp",
        "project_name":    "(temp job)",
        "project_address": "",
        "engineer": {
            "name":  "Adrienne Gould-Choquette",
            "email": "agc@adicot.com",
            "phone": "(804-787-0468)",
            "state": "Florida",
        },
        "config": {
            "project_address":         "",
            "toilet_exhaust_cfm":      "70",
            "bldg_exhaust_all_toilet": False,
        },
        "zone_overrides": {},
        "pdfs_generated": False,
        "drive_push":     None,
    }
    _save_meta(job_id, meta)
    return redirect(url_for(".job_star", job_id=job_id))


@job_lifecycle.route("/job/<job_id>/star")
@_require_auth
def job_star(job_id: str):
    """Per-job home tab. For a CMS job it shows the work order + a parse control
    (Drive search, with manual-upload fallback). For a temp job it's the upload
    drop zone. Parsing unlocks the six work tabs."""
    job_dir = _safe_job_path(job_id)

    if job_dir.exists():
        meta = _load_meta(job_id)
        source = meta.get("source") or ("cms" if meta.get("cms_item_id") else "temp")
        if source == "cms":
            meta = _refresh_cms_snapshot(job_id, meta)
    else:
        # Not parsed yet — a CMS job opened straight from the landing list.
        record = _cms.get_project(job_id)
        if not record:
            abort(404)
        source = "cms"
        meta = {
            "cms_item_id":     job_id,
            "cms_snapshot":    record,
            "project_address": (record.get("projectAddress") or "").strip(),
            "engineer": {
                "name":  "Adrienne Gould-Choquette",
                "email": "agc@adicot.com",
                "phone": "(804-787-0468)",
                "state": "Florida",
            },
        }

    parsed = job_dir.exists() and _is_parsed(job_dir)
    wo_sections = (_work_order_sections(meta.get("cms_snapshot"), include_empty=True)
                   if source == "cms" else None)

    return render_template(
        "jobs/star.html",
        active_tab="star", job_id=job_id, meta=meta,
        source=source, parsed=parsed, wo_sections=wo_sections,
    )


# ─── Code-default fenestration fill ───────────────────────────────────
# Which numeric field each lookup result feeds. Table C303.1.3(1) gives one
# U-factor for "window and glass door" combined, so fixed, operable and sliding
# door all take the same pair.
_GLASS_TARGETS = [("glassU", "glassSHGC"), ("glassOperU", "glassOperSHGC"),
                  ("glassSGDU", "glassSGDSHGC")]


def _apply_code_defaults(fields: dict, record: dict) -> None:
    """Fill U/SHGC from the C303.1.3 tables, in place, before the save writes.

    Only touches a value whose driving input changed on this save, or that is
    currently blank. That's what keeps a hand-edited number: change the window
    type and the figures follow, overtype one and it survives every later save
    until the type changes again. Recomputing unconditionally would silently
    revert every manual override."""
    def merged(key):
        return (fields.get(key, record.get(key)) or "").strip()

    def changed(*keys):
        return any(k in fields and (fields[k] or "").strip() != (record.get(k) or "").strip()
                   for k in keys)

    def put(key, value):
        if changed_driver or not merged(key):
            fields[key] = str(value)

    if merged("glassMethod") == GLASS_LOOKUP:
        frame, glazing, tint = merged("glassFrame"), merged("glazingType"), merged("glazingTint")
        changed_driver = changed("glassFrame", "glazingType", "glazingTint", "glassMethod")

        glass = fenestration_defaults.glass_defaults(frame, glazing, tint)
        if glass:
            for u_key, shgc_key in _GLASS_TARGETS:
                put(u_key, glass["u"])
                put(shgc_key, glass["shgc"])

        # Skylight U differs sharply from the window value for the same frame.
        sky = fenestration_defaults.skylight_defaults(frame, glazing, tint)
        if sky and merged("skylights") == "Yes":
            put("skylightU", sky["u"])
            put("skylightSHGC", sky["shgc"])

    # A door isn't glass, so this runs whatever the glass entry method is.
    door = fenestration_defaults.door_u(merged("doorType"))
    if door is not None:
        changed_driver = changed("doorType")
        put("doorU", door)


@job_lifecycle.route("/job/<job_id>/star/save", methods=["POST"])
@_require_auth
def job_star_save(job_id: str):
    """Save admin edits to a CMS job's work order — this is what turns
    jobs/star.html from a read-only mirror into the actual editable admin form.
    Collects every posted field into ONE dict and writes it in a single
    _cms.update_project() call, never one write per field. If the form's
    action is 'save_and_send', also mints a 180-day magic-link token and
    creates a Gmail draft containing it (folded into the same update_project()
    call by setting status alongside the other fields, not a second write) —
    never auto-sent; a human opens Gmail and clicks Send."""
    action = request.form.get("action", "save")
    fields = {}
    for _title, section_fields in _WORK_ORDER_SECTIONS:
        for _label, key in section_fields:
            canonical_key = key[0] if isinstance(key, (list, tuple)) else key
            if canonical_key in request.form:
                fields[canonical_key] = request.form.get(canonical_key, "")

    record = _cms.get_project(job_id) or {}
    _apply_code_defaults(fields, record)
    client_email = (record.get("clientEmail") or "").strip()
    send_link = False
    if action == "save_and_send":
        if not PORTAL_TOKEN_SECRET:
            flash("PORTAL_TOKEN_SECRET isn't configured — saving without sending a client link.")
        elif not client_email:
            flash("This record has no client email on file — saving without sending a client link.")
        else:
            fields["status"] = "Pending Client Approval"
            send_link = True

    if not _cms.update_project(job_id, fields):
        flash("Could not save the work order — the CMS record wasn't found or the write failed.")
        return redirect(url_for(".job_star", job_id=job_id))

    old_code = (record.get("clientCode") or "").strip()
    new_code = (fields.get("clientCode") or "").strip()
    if new_code and new_code != old_code:
        existing_folder_id = record.get("driveFolderId")
        if existing_folder_id:
            if gdrive_client.move_project_folder(existing_folder_id, new_code):
                flash(f"Moved this project's Drive folder into {new_code}/.")
            else:
                flash("Client Code saved, but the Drive folder couldn't be moved automatically — move it by hand.")
        else:
            job_no = record.get("jobNo") or job_id
            pf = gdrive_client.create_project_folder(job_no, new_code)
            if pf:
                _cms.update_project(job_id, {"driveFolderId": pf["folder_id"], "driveFolderUrl": pf["folder_url"]})
                flash(f"Created this project's Drive folder under {new_code}/.")
            else:
                flash("Client Code saved, but the Drive folder couldn't be created automatically — create it by hand.")

    if send_link:
        token = portal_tokens.make_token(job_id, PORTAL_TOKEN_SECRET, days_valid=180)
        base = os.environ.get("PUBLIC_BASE_URL", "https://adicot-load-calc-doc.onrender.com")
        portal_url = f"{base}/portal/{token}"
        merged = {**record, **fields}
        missing_block = _missing_info_block(merged)
        subject = (f"Your Adicot project specifications: "
                   f"{record.get('jobNo') or record.get('title') or job_id}")
        body = (
            f"Hi {record.get('clientName') or ''},\n\n"
            "Please review and complete your project specifications, then sign to authorize "
            f"Adicot to begin work:\n\n{portal_url}\n\n"
            "This link is valid for 180 days.\n\n"
            f"{missing_block}"
            "Thanks,\nAdicot, Inc."
        )
        # Creates a real Gmail draft, never auto-sends — a human opens Gmail
        # and clicks Send. See integrations/gmail_client.py for the
        # domain-wide-delegation setup this requires.
        if gmail_client.create_draft([client_email], subject, body):
            flash(f"Saved. A draft to {client_email} is waiting in Gmail — open it and hit Send when ready.")
        else:
            flash("Saved, but creating the Gmail draft failed — check the Gmail API / domain-wide delegation setup.")
    else:
        flash("Work order saved.")

    return redirect(url_for(".job_star", job_id=job_id))


def _portal_code_checks(record: dict) -> dict:
    """Inline code-min badges for the client portal form. Currently just the
    ASHRAE 90.1 / FBC-EC lighting power density max, keyed by the record's
    occupancy type — see hvac/lpd_max.py."""
    checks = {}
    max_lpd = lpd_max.lpd_max_for(record.get("occupancyType") or "")
    if max_lpd is not None:
        checks["lightingWattsPerSF"] = {"max": max_lpd, "label": "ASHRAE 90.1 / FBC-EC"}
    return checks


def _canonical_options(record: dict) -> dict:
    """Snap free-text dropdown values onto the exact option they match apart
    from case or surrounding space, so intake's "slab on grade" selects the
    real "Slab on grade" instead of falling through as unrecognised. Render-only
    — the Sheet is never rewritten as a side effect of viewing a page. A value
    that matches nothing is left exactly as it is, for portal.html to surface
    as its own option rather than silently drop."""
    out = dict(record)
    for key, opts in _FIELD_OPTIONS.items():
        val = (out.get(key) or "").strip()
        if not val:
            continue
        out[key] = next((o for o in opts if o.lower() == val.lower()), out[key])
    return out


def _render_portal(record: dict, token: str, signed: bool):
    """Every portal render goes through here so the fee split and the code-min
    badges stay in sync across all five exit paths (signed, saved, rejected,
    just-signed, plain GET)."""
    return render_template("portal.html", job=_canonical_options(record),
                           token=token, signed=signed,
                           code_checks={} if signed else _portal_code_checks(record),
                           fee=_fee_terms(record), opts=_FIELD_OPTIONS)


def _notify_staff_signed(job_id: str, record: dict, signer_name: str, signer_title: str) -> None:
    """Internal alert to Miles/Adi/Phoebe the moment a client signs — a
    plain SMTP send (not a Gmail draft) since this is staff-only, not
    client-facing, and doesn't need a human review step before it goes out."""
    base = os.environ.get("PUBLIC_BASE_URL", "https://adicot-load-calc-doc.onrender.com")
    job_label = record.get("jobNo") or record.get("title") or job_id
    subject = f"{job_label} — signed, now in queue"
    body = (
        f"{signer_name} ({signer_title}) just signed the work order for {job_label}.\n"
        f"This job is now in queue.\n\n"
        f"View: {base}/job/{job_id}/star"
    )
    email_client.send_email(list(PERSON_EMAILS.values()), subject, body)


@job_lifecycle.route("/portal/<token>", methods=["GET", "POST"])
def portal(token: str):
    """Client-facing work order / proposal / e-signature page — replaces Wix's
    admin-review.html (client mode, see archive/admin-review.html) and the Wix Members magic-link login. The
    signed token IS the authentication; this route is deliberately NOT behind
    @_require_auth, matching the header comment in templates/portal.html."""
    job_id = portal_tokens.verify_token(token, PORTAL_TOKEN_SECRET or "")
    if not job_id:
        abort(404)

    record = _cms.get_project(job_id)
    if not record:
        abort(404)

    already_signed = record.get("status") == "Current Work"

    if request.method == "POST":
        if already_signed:
            # Locked — re-render the success state rather than accept a second
            # submission over a still-valid link.
            return _render_portal(record, token, signed=True)

        # Collect every posted work-order field once — shared by both the
        # "save progress" and "sign" actions below, and written in exactly
        # ONE batched _cms.update_project() call either way.
        posted_fields = {}
        for _title, section_fields in _WORK_ORDER_SECTIONS:
            for _label, key in section_fields:
                canonical_key = key[0] if isinstance(key, (list, tuple)) else key
                if canonical_key in request.form:
                    posted_fields[canonical_key] = request.form.get(canonical_key, "")

        action = request.form.get("action", "sign")

        if action == "save_progress":
            # Partial save — no terms/signature required. The same magic
            # link brings the client back to finish later; status stays
            # whatever it already was (does NOT flip to Current Work).
            _cms.update_project(job_id, posted_fields)
            record = _cms.get_project(job_id) or record
            flash("Progress saved. You can come back to this same link anytime to finish.")
            return _render_portal(record, token, signed=False)

        terms_ok = all(request.form.get(f"agree_terms_{i}") for i in (1, 2, 3))
        signer_name = (request.form.get("signer_name") or "").strip()
        signer_title = (request.form.get("signer_title") or "").strip()
        if not (terms_ok and signer_name and signer_title):
            flash("Please check all three agreement boxes and enter your name and title before signing.")
            return _render_portal(record, token, signed=False)

        # Everything the client answered PLUS the signature, in one batched
        # write — never one call per field.
        posted_fields.update({
            "status":            "Current Work",
            "proposalSigned":    True,
            "workOrderComplete": True,
            "reviewComplete":    True,
            "signedDate":        datetime.datetime.utcnow().isoformat(),
            "signedBy":          signer_name,
            "signedTitle":       signer_title,
            "gcAccepted":        True,
        })
        _cms.update_project(job_id, posted_fields)
        record = _cms.get_project(job_id) or record
        _notify_staff_signed(job_id, record, signer_name, signer_title)
        return _render_portal(record, token, signed=True)

    return _render_portal(record, token, signed=already_signed)


@job_lifecycle.route("/job/<job_id>/invoice", methods=["GET"])
@_require_auth
def job_invoice(job_id: str):
    """Per-job invoice tab (CMS jobs only) — create/attach a QuickBooks invoice."""
    job_dir = _safe_job_path(job_id)

    if job_dir.exists():
        meta = _load_meta(job_id)
        source = meta.get("source") or ("cms" if meta.get("cms_item_id") else "temp")
        if source == "cms":
            meta = _refresh_cms_snapshot(job_id, meta)
    else:
        record = _cms.get_project(job_id)
        if not record:
            abort(404)
        source = "cms"
        meta = {
            "cms_item_id":     job_id,
            "cms_snapshot":    record,
            "project_address": (record.get("projectAddress") or "").strip(),
        }

    if source != "cms":
        abort(404)

    invoice = _load_invoice_registry().get(job_id)

    return render_template(
        "jobs/invoice.html",
        active_tab="invoice", job_id=job_id, meta=meta,
        qbo_status=qbo.connection_status(), invoice=invoice,
    )


# ─── Results page ─────────────────────────────────────────────────────

@job_lifecycle.route("/results/<job_id>")
@_require_auth
@_require_parsed
def results(job_id: str):
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)
    if meta.get("cms_item_id"):
        meta = _refresh_cms_snapshot(job_id, meta)

    deliverables = _deliverable_rows(job_dir / "out")

    cms_job_no = ""
    if meta.get("cms_snapshot"):
        cms_job_no = (meta["cms_snapshot"].get("jobNo") or "").strip()

    # Shape saved zone_overrides back into an enumerated list for the template
    # zone_overrides is {html_zone_name: {display_name?, supply_cfm?, merge_with?}}
    raw_overrides = meta.get("zone_overrides") or {}
    saved_overrides = [
        (i, {"match": zone, **ov})
        for i, (zone, ov) in enumerate(raw_overrides.items())
    ]

    return render_template(
        "results.html",
        active_tab="pdfs",
        job_id=job_id,
        meta=meta,
        deliverables=deliverables,
        cms_job_no=cms_job_no,
        drive_push=meta.get("drive_push"),
        saved_overrides=saved_overrides,
    )


# ─── Generate PDFs ────────────────────────────────────────────────────

def _config_and_engineer_from_meta(meta: dict) -> tuple["hp.ProjectConfig", "hp.EngineerInfo"]:
    """Build (ProjectConfig, EngineerInfo) from a job's meta.json, with the
    studio's standard defaults for anything not yet saved."""
    cfg_meta = meta.get("config", {})
    config = hp.ProjectConfig(
        toilet_exhaust_cfm=_num_or_default(cfg_meta.get("toilet_exhaust_cfm"), 70),
        bldg_exhaust_all_toilet=bool(cfg_meta.get("bldg_exhaust_all_toilet", False)),
        project_address=cfg_meta.get("project_address", meta.get("project_address", "")),
    )
    eng_meta = meta.get("engineer", {})
    engineer = hp.EngineerInfo(
        name=eng_meta.get("name", "Adrienne Gould-Choquette"),
        email=eng_meta.get("email", "agc@adicot.com"),
        phone=eng_meta.get("phone", "(804-787-0468)"),
        state_full=eng_meta.get("state", "Florida"),
    )
    return config, engineer


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _push_deliverables_to_drive(cms_job_no: str, drive_folder_id: str | None,
                                 targets: list[Path]) -> dict:
    """Upload the given out_dir files to the job's Drive 6-Submit folder.
    Returns a dict shaped for meta["drive_push"] / the results.html banner."""
    if not cms_job_no and not drive_folder_id:
        return {"status": "skipped", "reason": "no Wix project linked (or Wix project has no Job No)"}
    if not drive_folder_id and gdrive_client._parse_company_from_job_no(cms_job_no) is None:
        return {"status": "skipped", "reason": f"could not parse company from Job No '{cms_job_no}'"}

    drive_push: dict = {"status": "skipped"}
    pdf_files = []
    for p in sorted(targets):
        mime = _XLSX_MIME if p.suffix == ".xlsx" else "application/pdf"
        try:
            pdf_files.append((p.name, p.read_bytes(), mime))
        except Exception as e:
            drive_push.setdefault("read_errors", []).append({"name": p.name, "message": str(e)})

    if not pdf_files:
        return {"status": "error", "reason": "No deliverable files found to upload"}

    try:
        upload_result = gdrive_client.upload_files(cms_job_no, pdf_files, folder_id=drive_folder_id)
        drive_push = {
            "status": "success" if upload_result["ok"] else "partial",
            "folder_url": upload_result.get("folder_url"),
            "uploaded": upload_result.get("uploaded", []),
            "errors": upload_result.get("errors", []),
            "job_no": cms_job_no,
        }
        if not upload_result["ok"] and not upload_result.get("uploaded"):
            drive_push["status"] = "error"
    except Exception as e:
        tb = traceback.format_exc()
        print(f"DRIVE PUSH FAILURE: {tb}", flush=True)
        drive_push = {"status": "error", "reason": f"{type(e).__name__}: {e}", "job_no": cms_job_no}
    return drive_push


# ─── Generatable deliverables ─────────────────────────────────────────
# The 4 PDFs (3 schedules + Combined) plus the 3 schedule Excel sources each
# schedule PDF is converted from. Equipment/spec files live on their own tabs
# and are deliberately excluded. Single source for the Generate form's
# checkboxes, for resolving a checked key back to a file on disk, and for the
# Drive upload set.
#
# Keys match what hvac_pipeline.build_all_pdfs(select=...) expects, except
# "combined_pdf", which this blueprint builds itself via _rebuild_combined.
_DELIVERABLES = [
    ("ventilation_xlsx", "Ventilation Schedule", "Excel", "-Ventilation.xlsx"),
    ("ventilation_pdf",  "Ventilation Schedule", "PDF",   "-Ventilation.pdf"),
    ("air_balance_xlsx", "Air Balance",          "Excel", "-Air_Balance.xlsx"),
    ("air_balance_pdf",  "Air Balance",          "PDF",   "-Air_Balance.pdf"),
    ("load_xlsx",        "Load Summary",         "Excel", "-Load.xlsx"),
    ("load_pdf",         "Load Summary",         "PDF",   "-Load.pdf"),
    ("combined_pdf",     "Combined",             "PDF",   "-Combined.pdf"),
]
_DELIVERABLE_KEYS = [d[0] for d in _DELIVERABLES]


def _deliverable_path(out_dir: Path, suffix: str) -> Optional[Path]:
    """The existing file for a deliverable suffix, or None. The <prefix> is
    derived from the project name inside the pipeline, so it can only be
    discovered by globbing, never predicted before the first generate."""
    return next(iter(sorted(out_dir.glob(f"*{suffix}"))), None)


def _deliverable_rows(out_dir: Path) -> list[dict]:
    """One row per deliverable for the Generate form: always present so the
    first run is selectable too, carrying name/size only once the file exists."""
    rows = []
    for key, label, kind, suffix in _DELIVERABLES:
        path = _deliverable_path(out_dir, suffix) if out_dir.exists() else None
        rows.append({
            "key": key, "label": label, "kind": kind,
            "name": path.name if path else None,
            "size_kb": f"{path.stat().st_size / 1024:.0f}" if path else None,
        })
    return rows


# ─── Combined-PDF / chart-appendix helpers ────────────────────────────
# Shared with blueprints/quality.py's Charts tab (job_charts / job_charts_select),
# which imports _available_charts and _rebuild_combined from here rather than
# duplicating this bookkeeping.

_CHART_CAPTIONS = {
    "sensible_vs_latent.png":  "Cooling Load — Sensible vs Latent by Zone",
    "air_balance.png":         "Air Balance — Supply vs Outside Air by Zone",
    "top_rooms_cooling.png":   "Top Rooms by Cooling Load",
}


def _caption_for(filename: str) -> str:
    if filename in _CHART_CAPTIONS:
        return _CHART_CAPTIONS[filename]
    if filename.startswith("cooling_breakdown_"):
        return "Cooling Load Breakdown by Component"
    return filename


def _available_charts(job_dir: Path) -> list[dict]:
    """Ordered list of {name, caption} for the chart PNGs that exist for a job.
    The order here is the display order on the Charts tab and the append order
    in the combined PDF."""
    charts_dir = job_dir / "out" / "charts"
    charts = []
    if charts_dir.exists():
        order = ["sensible_vs_latent.png"]
        order += sorted(p.name for p in charts_dir.glob("cooling_breakdown_*.png"))
        order += ["air_balance.png", "top_rooms_cooling.png"]
        for name in order:
            if (charts_dir / name).exists():
                charts.append({"name": name, "caption": _caption_for(name)})
    return charts


def _html_appendix_bytes(job_dir: Path, meta: dict) -> Optional[bytes]:
    """Render the job's scraped DM HTML to PDF bytes for the Load/Combined
    appendix, or None if there's no HTML or rendering fails."""
    html_name = meta.get("html_name")
    if not html_name:
        return None
    html_path = job_dir / html_name
    if not html_path.exists():
        return None
    return html_pdf.render_html_to_pdf_bytes(html_path)


def _rebuild_combined(job_dir: Path, meta: dict,
                      appendix: Optional[bytes] = None) -> Optional[Path]:
    """(Re)build <prefix>-Combined.pdf from the three deliverables, the charts
    selected in meta['combined_charts'], then the HTML appendix at the very end.
    Returns the path or None. Never raises — combining is best-effort and must
    not break PDF generation.

    The standalone -Load.pdf on disk carries the appendix too (see generate_pdfs),
    so we pass meta['load_clean_pages'] to insert only its deliverable pages here —
    keeping the appendix to a single copy, dead last."""
    out_dir = job_dir / "out"
    selected = set(meta.get("combined_charts") or [])
    charts_dir = out_dir / "charts"
    ordered = [(charts_dir / c["name"], c["caption"])
               for c in _available_charts(job_dir) if c["name"] in selected]
    if appendix is None:
        appendix = _html_appendix_bytes(job_dir, meta)
    try:
        return pdf_combine.build_combined_pdf(
            out_dir, ordered,
            appendix=appendix,
            load_pages=meta.get("load_clean_pages"),
        )
    except Exception:
        traceback.print_exc()
        return None


def _append_html_to_load(job_dir: Path, meta: dict,
                         appendix: Optional[bytes]) -> None:
    """Append the HTML appendix to the standalone -Load.pdf and record the clean
    (pre-appendix) page count in meta so the combiner can skip the duplicate.
    Run once per full regenerate, on a freshly-written clean Load PDF."""
    meta["load_clean_pages"] = None
    if not appendix:
        return
    load_pdf = next((job_dir / "out").glob("*-Load.pdf"), None)
    if load_pdf is None:
        return
    clean = pdf_combine.pdf_page_count(load_pdf)
    if pdf_combine.append_pdf_to_file(load_pdf, appendix):
        meta["load_clean_pages"] = clean


@job_lifecycle.route("/job/<job_id>/generate-pdfs", methods=["POST"])
@_require_auth
def generate_pdfs(job_id: str):
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)
    out_dir = job_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    html_name = meta.get("html_name")
    if not html_name:
        flash("Missing html_name in meta.json — can't regenerate PDFs.")
        return redirect(url_for(".results", job_id=job_id))

    html_path = job_dir / html_name
    if not html_path.exists():
        flash(f"Source HTML missing on disk: {html_name}")
        return redirect(url_for(".results", job_id=job_id))

    config, engineer = _config_and_engineer_from_meta(meta)
    firm = hp.FirmInfo()

    # Which deliverables this run should build. The form always posts
    # has_selection; its absence means a non-form caller, so build everything
    # rather than silently doing nothing. Present-but-empty is a real "nothing
    # checked" and must not be treated as "all".
    if "has_selection" in request.form:
        select = set(request.form.getlist("gen")) & set(_DELIVERABLE_KEYS)
        if not select:
            flash("Nothing selected — pick at least one file to generate.")
            return redirect(url_for(".results", job_id=job_id))
    else:
        select = set(_DELIVERABLE_KEYS)

    try:
        with redirect_stdout(io.StringIO()):
            hp.build_all_pdfs(
                html_path=html_path,
                config=config,
                engineer=engineer,
                firm=firm,
                out_dir=out_dir,
                zone_overrides=meta.get("zone_overrides") or {},
                select=select,
            )
    except Exception:
        tb = traceback.format_exc()
        (job_dir / "pdf_error.log").write_text(tb)
        print("=" * 60, flush=True)
        print(f"PDF GENERATION FAILURE for job {job_id}:", flush=True)
        print(tb, flush=True)
        print("=" * 60, flush=True)
        meta["pdfs_generated"] = False
        meta["drive_push"] = {"status": "skipped", "reason": "PDF generation failed"}
        _save_meta(job_id, meta)
        flash("PDF generation failed — check the Render logs for the traceback.")
        return redirect(url_for(".results", job_id=job_id))

    meta["pdfs_generated"] = any(
        _deliverable_path(out_dir, sfx) for _k, _l, _kind, sfx in _DELIVERABLES
        if sfx.endswith(".pdf"))

    # Render the scraped DM HTML once, append it to the standalone Load PDF, and
    # build the combined with the same appendix dead last (after the charts).
    appendix = _html_appendix_bytes(job_dir, meta)

    # Only when the Load PDF was actually rebuilt. _append_html_to_load must run
    # on a freshly written clean Load PDF: re-running it on an untouched one
    # would append a second copy of the appendix, and it resets
    # meta['load_clean_pages'], which the combiner needs to keep the appendix to
    # a single copy. Skipping it leaves the previous run's value intact.
    if "load_pdf" in select:
        _append_html_to_load(job_dir, meta, appendix)

    # Combines whatever deliverable PDFs are on disk, so a partial run correctly
    # mixes what was just rebuilt with what was already there.
    if "combined_pdf" in select:
        _rebuild_combined(job_dir, meta, appendix=appendix)

    cms_snapshot = meta.get("cms_snapshot") or {}
    cms_job_no = (cms_snapshot.get("jobNo") or "").strip()
    drive_folder_id = meta.get("drive_folder_id")   # manually chosen job folder, if any

    # Push exactly what was selected (and actually landed on disk). An
    # unchecked deliverable is left alone in Drive as well as locally.
    upload_targets = [
        path for key, _l, _kind, sfx in _DELIVERABLES if key in select
        for path in [_deliverable_path(out_dir, sfx)] if path
    ]

    if not upload_targets:
        meta["drive_push"] = {"status": "skipped", "reason": "no files selected for Drive upload"}
        _save_meta(job_id, meta)
        flash("PDFs generated. No files were selected to upload to Drive.")
        return redirect(url_for(".results", job_id=job_id))

    drive_push = _push_deliverables_to_drive(cms_job_no, drive_folder_id, upload_targets)
    meta["drive_push"] = drive_push
    _save_meta(job_id, meta)

    if drive_push["status"] == "success":
        flash(f"PDFs generated and uploaded to Drive ({cms_job_no}/6-Submit).")
    elif drive_push["status"] == "skipped":
        flash("PDFs generated. (Drive upload skipped — use the download links below.)")
    elif drive_push["status"] == "partial":
        flash("PDFs generated. Some Drive uploads failed — see details below.")
    else:
        flash("PDFs generated, but the Drive upload failed. Use the browser download links and upload manually.")

    return redirect(url_for(".results", job_id=job_id))


# ─── Commit settings + regenerate PDFs ────────────────────────────────

def _parse_zone_overrides(form) -> dict:
    """Parse zone override rows from the results form into the meta dict format.

    Form fields: ov_match_N, ov_display_N, ov_supply_N, ov_merge_N
    Returns {html_zone_name: {display_name?, supply_cfm?, merge_with?}}
    """
    # Collect all index suffixes present
    indices = set()
    for key in form.keys():
        for prefix in ("ov_match_", "ov_display_", "ov_supply_", "ov_merge_"):
            if key.startswith(prefix):
                indices.add(key[len(prefix):])

    overrides = {}
    for idx in sorted(indices):
        match = form.get(f"ov_match_{idx}", "").strip()
        if not match:
            continue  # ignore rows with no match key
        ov = {}
        display = form.get(f"ov_display_{idx}", "").strip()
        supply  = form.get(f"ov_supply_{idx}", "").strip()
        merge   = form.get(f"ov_merge_{idx}", "").strip()
        if display: ov["display_name"] = display
        if supply:
            try: ov["supply_cfm"] = float(supply)
            except ValueError: pass
        if merge: ov["merge_with"] = merge
        overrides[match] = ov
    return overrides


@job_lifecycle.route("/job/<job_id>/commit-settings", methods=["POST"])
@_require_auth
@_require_parsed
def commit_settings(job_id: str):
    """Save project settings (toilet exhaust, zone overrides) then regenerate PDFs."""
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)

    # Update config
    toilet_cfm = _num_or_default(request.form.get("toilet_exhaust_cfm"), 70)

    meta.setdefault("config", {})
    meta["config"]["toilet_exhaust_cfm"] = toilet_cfm
    meta["config"]["bldg_exhaust_all_toilet"] = (
        request.form.get("bldg_exhaust_all_toilet") == "on"
    )

    # Update zone overrides
    meta["zone_overrides"] = _parse_zone_overrides(request.form)
    _save_meta(job_id, meta)

    # Now re-run the PDF pipeline with the updated settings
    out_dir = job_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    html_name = meta.get("html_name")
    if not html_name or not (job_dir / html_name).exists():
        flash("Settings saved, but source HTML is missing — can't regenerate PDFs.")
        return redirect(url_for(".results", job_id=job_id))

    html_path = job_dir / html_name
    config, engineer = _config_and_engineer_from_meta(meta)
    firm = hp.FirmInfo()

    # Refresh the on-screen preview so it reflects the new settings. The results
    # page renders meta["preview"]; build_all_pdfs writes only the PDFs, so
    # without this the display would keep the old toilet-exhaust values.
    try:
        report = hp.parse_report(html_path.read_text(encoding="latin-1"))
        meta["preview"] = _render_preview(report, config, engineer)
    except Exception:
        traceback.print_exc()

    try:
        with redirect_stdout(io.StringIO()):
            # No select= here on purpose: changed settings invalidate all three
            # schedules, so this path always rebuilds the full set. Picking a
            # subset is the Generate form's job.
            hp.build_all_pdfs(
                html_path=html_path,
                config=config,
                engineer=engineer,
                firm=firm,
                out_dir=out_dir,
                zone_overrides=meta.get("zone_overrides") or {},
            )
        meta["pdfs_generated"] = True
        appendix = _html_appendix_bytes(job_dir, meta)
        _append_html_to_load(job_dir, meta, appendix)
        _rebuild_combined(job_dir, meta, appendix=appendix)
        flash("Settings saved and PDFs regenerated.")
    except Exception:
        tb = traceback.format_exc()
        (job_dir / "pdf_error.log").write_text(tb)
        print(tb, flush=True)
        meta["pdfs_generated"] = False
        flash("Settings saved, but PDF regeneration failed — check Render logs.")

    _save_meta(job_id, meta)
    return redirect(url_for(".results", job_id=job_id))


# ─── Re-scrape HTML from Drive ─────────────────────────────────────────

@job_lifecycle.route("/job/<job_id>/rescrape", methods=["POST"])
@_require_auth
def rescrape_html(job_id: str):
    """Re-fetch the DM HTML from Drive for jobs originally sourced from Drive,
    then re-parse (report.json + charts + preview + Wix validation). Does NOT
    regenerate PDFs — the user regenerates afterward to pick up the changes."""
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)

    if not meta.get("html_from_drive"):
        flash("This job's HTML wasn't sourced from Drive — re-upload manually to refresh it.")
        return redirect(url_for(".results", job_id=job_id))

    cms_item_id = (meta.get("cms_item_id") or "").strip()
    if not cms_item_id:
        flash("No linked Wix project — can't locate the Drive file to re-scrape.")
        return redirect(url_for(".results", job_id=job_id))

    wix_record = _cms.get_project(cms_item_id)
    job_no = (wix_record or {}).get("jobNo", "").strip() if wix_record else ""
    if not job_no:
        flash("Couldn't look up the Wix project's Job No — can't re-scrape from Drive.")
        return redirect(url_for(".results", job_id=job_id))

    fetched = gdrive_client.find_html(job_no)
    if fetched is None:
        flash(f"Couldn't fetch the HTML from Drive for {job_no}.")
        return redirect(url_for(".results", job_id=job_id))

    drive_filename, drive_bytes = fetched
    html_path = job_dir / (meta.get("html_name") or drive_filename or "dm_hvac-loads1.html")
    html_path.write_bytes(drive_bytes or b"")

    config, engineer = _config_and_engineer_from_meta(meta)

    try:
        report, preview = _parse_and_persist(job_dir, html_path, config, engineer)
    except Exception:
        tb = traceback.format_exc()
        (job_dir / "error.log").write_text(tb)
        print(tb, flush=True)
        flash("Re-parsing the refreshed HTML failed — check the Render logs for the traceback.")
        return redirect(url_for(".results", job_id=job_id))

    meta["html_name"] = html_path.name
    meta["preview"] = preview
    meta["project_name"] = report.project.project_name

    # Re-run Wix validation against the refreshed report
    cms_snapshot = meta.get("cms_snapshot")
    if cms_snapshot:
        try:
            meta["mismatches"] = validators.compare(report, cms_snapshot)
        except Exception:
            traceback.print_exc()

    _save_meta(job_id, meta)
    flash("Re-scraped the HTML from Drive and re-parsed. Regenerate the PDFs to update the deliverables.")
    return redirect(url_for(".results", job_id=job_id))


# ─── Parse (run the pipeline for a star/temp job) ─────────────────────

@job_lifecycle.route("/job/<job_id>/parse", methods=["POST"])
@_require_auth
def job_parse(job_id: str):
    """Parse a job's Design Master HTML (from Drive or a manual upload) and unlock
    the work tabs. Serves both CMS jobs (job_id == Wix item id) and temp jobs
    (job_id starts with 'temp_'). Re-parsing a CMS job reuses its workspace, so
    saved settings (toilet exhaust, zone overrides, spec/equip inputs) carry over.
    """
    job_dir = _safe_job_path(job_id)
    existing = _load_meta(job_id) if job_dir.exists() else {}

    is_temp = job_id.startswith("temp_") or existing.get("source") == "temp"
    source = "temp" if is_temp else "cms"
    cms_item_id = "" if is_temp else job_id

    engineer_state = request.form.get("engineer_state", "Florida").strip()
    engineer_name = request.form.get("engineer_name", "Adrienne Gould-Choquette").strip()
    engineer_email = request.form.get("engineer_email", "agc@adicot.com").strip()
    engineer_phone = request.form.get("engineer_phone", "(804-787-0468)").strip()

    f = request.files.get("html_file")
    has_upload = f is not None and f.filename
    drive_bytes: Optional[bytes] = None
    drive_filename: Optional[str] = None

    # For a CMS job the address comes from the Wix record; a temp job can type one.
    wix_record = _cms.get_project(cms_item_id) if cms_item_id else None
    if is_temp:
        project_address = request.form.get("project_address", "").strip()
    else:
        project_address = ((wix_record or {}).get("projectAddress") or "").strip()

    job_no = ((wix_record or {}).get("jobNo") or "").strip()
    # A manually-chosen Drive folder (this submit, or remembered from a prior one)
    # overrides the auto Job-No path search.
    # A freshly-picked folder is normalized to the JOB folder (the one holding
    # 4-Design + 6-Submit), so picking 4-Design by mistake still works. A
    # remembered folder is already normalized.
    _picked = request.form.get("drive_folder_id", "").strip()
    if _picked:
        drive_folder_id = gdrive_client.resolve_job_folder(_picked)
    else:
        drive_folder_id = existing.get("drive_folder_id") or ""
    drive_folder_name = (request.form.get("drive_folder_name", "").strip()
                         or (existing.get("drive_folder_name") or ""))

    if has_upload:
        pass
    elif cms_item_id:
        if drive_folder_id:
            fetched = gdrive_client.find_html_in_folder(drive_folder_id)
            if fetched is None:
                flash("Couldn't find an HTML in the chosen Drive folder. "
                      "Pick a different folder or upload the file manually.")
                return redirect(url_for(".job_star", job_id=job_id))
        else:
            if not job_no:
                flash("Couldn't look up the Wix project's Job No — pick the Drive folder "
                      "manually or upload the HTML.")
                return redirect(url_for(".job_star", job_id=job_id))
            fetched = gdrive_client.find_html(job_no)
            if fetched is None:
                flash(f"Couldn't fetch the HTML from Drive for {job_no}. Pick the folder "
                      "manually or upload it.")
                return redirect(url_for(".job_star", job_id=job_id))
        drive_filename, drive_bytes = fetched
    else:
        flash("No file uploaded.")
        return redirect(url_for(".job_star", job_id=job_id))

    out_dir = job_dir / "out"
    (out_dir / "charts").mkdir(parents=True, exist_ok=True)

    if has_upload:
        html_path = job_dir / secure_filename(f.filename)
        f.save(html_path)
    else:
        html_path = job_dir / (drive_filename or "dm_hvac-loads1.html")
        html_path.write_bytes(drive_bytes or b"")

    # Reuse any saved exhaust settings on a re-parse so the preview matches.
    prev_cfg = existing.get("config") or {}
    toilet_exh = _num_or_default(prev_cfg.get("toilet_exhaust_cfm"), 70)

    config = hp.ProjectConfig(
        toilet_exhaust_cfm=toilet_exh,
        bldg_exhaust_all_toilet=bool(prev_cfg.get("bldg_exhaust_all_toilet", False)),
        project_address=project_address,
    )
    engineer = hp.EngineerInfo(
        name=engineer_name,
        email=engineer_email,
        phone=engineer_phone,
        state_full=engineer_state,
    )

    report = None
    try:
        report, preview = _parse_and_persist(job_dir, html_path, config, engineer)
    except Exception:
        tb = traceback.format_exc()
        (job_dir / "error.log").write_text(tb)
        print("=" * 60, flush=True)
        print(f"PARSE FAILURE for job {job_id}:", flush=True)
        print(tb, flush=True)
        print("=" * 60, flush=True)
        flash("Parsing the HTML failed — check the Render logs for the traceback.")
        return redirect(url_for(".job_star", job_id=job_id))

    cms_snapshot = wix_record
    mismatches: list[dict] = []
    if cms_item_id:
        if cms_snapshot is None:
            print(f"WARNING: cms_item_id={cms_item_id} but get_project returned None", flush=True)
        elif report is not None:
            try:
                mismatches = validators.compare(report, cms_snapshot)
            except Exception as e:
                print(f"WARNING: validator.compare failed: {e}", flush=True)
                traceback.print_exc()
                mismatches = [{"field": "(validator)", "wix_value": "", "html_values": [], "summary": f"Validator failed: {e}"}]

    project_name = report.project.project_name if report is not None else "(unknown)"
    state_code = _extract_state_code(project_address)

    # Carry forward settings the engineer may have tuned on a previous parse.
    meta = {
        "source":          source,
        "project_name":    project_name,
        "project_address": project_address,
        "state_code":      state_code,
        "html_name":       html_path.name,
        "html_from_drive": not has_upload,
        "drive_folder_id": (drive_folder_id or None) if not has_upload else None,
        "drive_folder_name": (drive_folder_name or None) if not has_upload else None,
        "preview":         preview,
        "engineer": {
            "name":  engineer_name,
            "email": engineer_email,
            "phone": engineer_phone,
            "state": engineer_state,
        },
        "config": {
            "project_address":         project_address,
            "toilet_exhaust_cfm":      prev_cfg.get("toilet_exhaust_cfm", "70"),
            "bldg_exhaust_all_toilet": bool(prev_cfg.get("bldg_exhaust_all_toilet", False)),
        },
        "zone_overrides": existing.get("zone_overrides") or {},
        "combined_charts": existing.get("combined_charts") or [],
        "spec_inputs":    existing.get("spec_inputs") or {},
        "equip_inputs":   existing.get("equip_inputs") or {},
        "cms_item_id":    cms_item_id,
        "cms_snapshot":   cms_snapshot,
        "mismatches":     mismatches,
        "pdfs_generated": False,
        "drive_push":     None,
    }
    _save_meta(job_id, meta)

    return redirect(url_for(".results", job_id=job_id))


# ─── File downloads ────────────────────────────────────────────────────

@job_lifecycle.route("/job/<job_id>/file/<path:filename>")
@_require_auth
def download_file(job_id: str, filename: str):
    job_dir = _job_dir(job_id)
    return send_from_directory(job_dir / "out", filename, as_attachment=True)


@job_lifecycle.route("/job/<job_id>/chart/<path:filename>")
@_require_auth
def download_chart(job_id: str, filename: str):
    job_dir = _job_dir(job_id)
    return send_from_directory(job_dir / "out" / "charts", filename)
