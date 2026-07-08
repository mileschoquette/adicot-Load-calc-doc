"""HVAC Loads Pipeline — Flask UI with multi-tab support.

Routes (URL → view function name):
    /                                index           Jobs-in-CMS landing list (Wix projects)
    /job/new-temp                    new_temp        POST — create a temp-job workspace
    /job/<job_id>/star               job_star        ★ Work Order / parse tab (per-job home)
    /job/<job_id>/parse              job_parse       POST — parse HTML (Drive or upload); unlocks tabs
    /results/<job_id>                results         PDF tab (preview + PDFs section)
    /job/<job_id>/generate-pdfs      generate_pdfs   POST — runs PDF pipeline + Drive push
    /job/<job_id>/commit-settings    commit_settings POST — save settings + regenerate PDFs
    /job/<job_id>/duct               job_duct        Duct Sizing tab
    /job/<job_id>/charts             job_charts      Charts tab
    /job/<job_id>/spec               job_spec        Specifications tab
    /job/<job_id>/spec/save          job_spec_save   POST — save spec inputs and render output
    /job/<job_id>/spec/download-docx job_spec_download_docx POST — generate + download Word Doc
    /jobs                            past_jobs       Temp jobs index (non-CMS, on-disk jobs)
    /past-jobs                                       301 redirect → /jobs
    /job/<job_id>/file/<name>        download_file   File download (PDFs / DXF / DOCX)
    /job/<job_id>/chart/<name>       download_chart  Inline-serve a chart PNG

Job identity: CMS jobs use their Wix item id as the job_id (so re-opening a CMS
project reuses its workspace and saved settings); temp jobs use a "temp_" prefix.
The six work tabs are gated on report.json existing (see _require_parsed).

Per-job storage layout:
    jobs/<job_id>/
        <original>.html
        meta.json
        report.json
        out/
            <project>-Ventilation.pdf
            <project>-Air_Balance.pdf
            <project>-Load.pdf
            charts/
                sensible_vs_latent.png
                cooling_breakdown_<n>.png
                air_balance.png
                top_rooms_cooling.png

Environment variables:
    APP_PASSWORD                    shared password (username always "adicot"); unset = no auth
    SECRET_KEY                      Flask session key; auto-generated if unset
    JOBS_DIR                        where per-job workspaces live; default ./jobs
    GOOGLE_SERVICE_ACCOUNT_JSON     Drive service account creds (JSON blob)
    WIX_API_KEY, WIX_SITE_ID        Wix CMS credentials
    PORT                            set by Render/host
"""

from __future__ import annotations

import datetime
import functools
import io
import json
import os
import re
import secrets
import shutil
import traceback
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from flask import (Flask, render_template, request, send_from_directory,
                   send_file, abort, redirect, url_for, flash, Response, jsonify,
                   session)
from werkzeug.utils import secure_filename

import hvac_pipeline as hp
from charts import render_all_charts
import wix_client
import validators
import gdrive_client
import spec_engine
import spec_data
import spec_docx
import pdf_crop
import pdf_combine
import html_pdf
import room_qc
import roof_check
import quickbooks_client as qbo

# ── Equipment selector (optional — graceful fallback if files not present) ──
try:
    import hvac_selector as eng
    HAS_EQUIP_SELECTOR = True
    _EQUIP_IMPORT_ERROR = None
except Exception as _e:
    HAS_EQUIP_SELECTOR = False
    _EQUIP_IMPORT_ERROR = str(_e)

# ── DM Setup .vbs generator (optional — graceful fallback if module missing) ──
try:
    import dm_setup_generator as dmsg
    HAS_DM_SETUP_GENERATOR = True
    _DM_SETUP_IMPORT_ERROR = None
except Exception as _e:
    HAS_DM_SETUP_GENERATOR = False
    _DM_SETUP_IMPORT_ERROR = str(_e)

# ─── Paths ───────────────────────────────────────────────────────────
APP_DIR = Path(__file__).resolve().parent
JOBS_DIR = Path(os.environ.get("JOBS_DIR", APP_DIR / "jobs"))
JOBS_DIR.mkdir(parents=True, exist_ok=True)

# ─── Auth ────────────────────────────────────────────────────────────
APP_USERNAME = "adicot"
APP_PASSWORD = os.environ.get("APP_PASSWORD")

# ─── Crop route auth (token, not basic-auth) ─────────────────────────
CROP_TOKEN = os.environ.get("CROP_TOKEN")
CROP_MAX_BYTES = 40 * 1024 * 1024   # 40 MB ceiling for the JSON body on this route

# ─── Flask setup ─────────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(16))


def _require_auth(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not APP_PASSWORD:
            return view(*args, **kwargs)
        auth = request.authorization
        if (auth and auth.username and auth.password and
                secrets.compare_digest(auth.username, APP_USERNAME) and
                secrets.compare_digest(auth.password, APP_PASSWORD)):
            return view(*args, **kwargs)
        return Response("Authentication required.", 401,
                        {"WWW-Authenticate": 'Basic realm="Adicot HVAC Pipeline"'})
    return wrapper


# ─── Job helpers ─────────────────────────────────────────────────────

def _safe_job_path(job_id: str) -> Path:
    """Validate job_id and return its workspace path — which may not exist yet.

    Same secure_filename + parent-containment checks as _job_dir, but without the
    existence requirement. Used by the star/parse flow, which creates a workspace
    lazily, and by pages that render before a job has been parsed.
    """
    safe_id = secure_filename(job_id)
    if not safe_id or safe_id != job_id:
        abort(404)
    d = (JOBS_DIR / safe_id).resolve()
    if JOBS_DIR.resolve() not in d.parents:
        abort(404)
    return d


def _job_dir(job_id: str) -> Path:
    d = _safe_job_path(job_id)
    if not d.exists() or not d.is_dir():
        abort(404)
    return d


def _is_parsed(job_dir: Path) -> bool:
    """True once the job has a parsed report on disk (the gate for the work tabs)."""
    return (job_dir / "report.json").exists()


def _require_parsed(view):
    """Redirect to the job's star (W/O) page if it hasn't been parsed yet. The six
    work tabs assume report.json exists; this keeps a hand-typed URL from rendering
    an empty tab."""
    @functools.wraps(view)
    def wrapper(job_id, *args, **kwargs):
        if not _is_parsed(_safe_job_path(job_id)):
            flash("Parse the job first to unlock this tab.")
            return redirect(url_for("job_star", job_id=job_id))
        return view(job_id, *args, **kwargs)
    return wrapper


def _load_meta(job_id: str) -> dict:
    try:
        return json.loads((_job_dir(job_id) / "meta.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_meta(job_id: str, meta: dict) -> None:
    (_job_dir(job_id) / "meta.json").write_text(
        json.dumps(meta, indent=2, default=str)
    )


def _num_or_default(value, default: float) -> float:
    """float(value), but treat only None/'' as 'missing' → default.

    An explicit 0 must survive. `float(value or default)` is WRONG here: 0 and
    0.0 are falsy, so a saved toilet-exhaust of 0 would silently revert to the
    default and the Air Balance PDF would keep showing the un-zeroed exhaust.
    """
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _load_report(job_id: str) -> dict:
    try:
        return json.loads((_job_dir(job_id) / "report.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _render_preview(report, config: "hp.ProjectConfig",
                    engineer: "hp.EngineerInfo") -> str:
    """Render the text 'deliverables' preview (shown on the results page) for the
    given report + settings. Reflects toilet-exhaust and the other config inputs,
    so it must be re-run whenever those change."""
    buf = io.StringIO()
    computed = hp.compute(report, config)
    with redirect_stdout(buf):
        hp.print_deliverables({"computed": computed}, report, config, engineer)
    return buf.getvalue()


def _parse_and_persist(job_dir: Path, html_path: Path,
                       config: "hp.ProjectConfig",
                       engineer: "hp.EngineerInfo"):
    """Parse the DM HTML at html_path, write report.json + charts, and return
    (report, preview_text). Raises on parse failure — the caller decides how to
    surface it. Shared by the upload and rescrape flows."""
    html_text = html_path.read_text(encoding="latin-1")
    report = hp.parse_report(html_text)
    preview = _render_preview(report, config, engineer)

    try:
        (job_dir / "report.json").write_text(
            json.dumps(asdict(report), indent=2, default=str)
        )
    except Exception:
        traceback.print_exc()

    try:
        render_all_charts(report, job_dir / "out" / "charts")
    except Exception:
        traceback.print_exc()

    return report, preview


# ─── State code helper ────────────────────────────────────────────────

def _extract_state_code(address: str) -> str:
    """Pull 2-letter state abbreviation from a US address string.
    e.g. '123 Main St, Miami, FL 33101' -> 'FL'
    """
    if not address:
        return ""
    m = re.search(r'\b([A-Z]{2})\b(?:\s+\d{5}(?:-\d{4})?)?(?:\s*$|,)', address.strip())
    return m.group(1) if m else ""


# ─── Crop route helpers ──────────────────────────────────────────────

def _crop_authorized(req) -> bool:
    """True if the request carries the right token. Checks header then query."""
    if not CROP_TOKEN:
        return False   # not configured -> refuse, don't run open
    supplied = (req.headers.get("X-Crop-Token")
                or req.args.get("token", "")).strip()
    return bool(supplied) and secrets.compare_digest(supplied, CROP_TOKEN)


# ─── Routes: /crop (intake snippet cropper) ──────────────────────────
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
# Requires (top of file):  import pdf_crop
# requirements.txt:        pymupdf>=1.24
# Render env:              CROP_TOKEN = <long random string> (same value in Apps
#                          Script Script Properties as CROP_TOKEN)

@app.route("/crop", methods=["POST"])
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
        import base64 as _b64
        pdf_bytes = _b64.b64decode(pdf_b64)
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


# ─── Routes: upload form ─────────────────────────────────────────────

def _build_cms_entries() -> list[dict]:
    """Projects from the Wix CMS for the landing list, each tagged with whether
    a parsed workspace already exists for it (keyed by the Wix item id)."""
    entries = []
    for p in wix_client.list_projects():
        _id = (p.get("_id") or "").strip()
        if not _id:
            continue
        addr = (p.get("projectAddress") or "").strip()
        job_no = (p.get("jobNo") or "").strip()
        title = (p.get("title") or "").strip()
        # Parsed only if the id survives secure_filename (its workspace dir name).
        parsed = (secure_filename(_id) == _id
                  and (JOBS_DIR / _id / "report.json").exists())
        entries.append({
            "_id": _id, "job_no": job_no, "address": addr,
            "title": title, "parsed": parsed,
        })
    entries.sort(key=lambda e: (e["address"] or e["title"] or e["job_no"]).lower())
    return entries


@app.route("/")
@_require_auth
def index():
    """Landing page — the list of CMS (Wix) projects, plus Run a Temp Job."""
    return render_template("cms_jobs.html", projects=_build_cms_entries())


# ─── Routes: Star (Work Order / parse) tab ───────────────────────────

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
        ("Property Owner", "propertyOwner"),
        ("Owner", "owner"),
        ("Client Name", "clientName"),
        ("Client Company", "clientCompany"),
        ("Client Email", "clientEmail"),
        ("Client Phone", "clientPhone"),
        ("Product / Service", "productService"),
        ("Status", "status"),
        ("Client Code", "clientCode"),
        ("Sub Client", "subClient"),
        ("Community", "community"),
        ("Subdivision", "subdivision"),
        ("Location Disambig", "locationDisambig"),
        ("Lennar Job No", ["lennarJobNo", "lennarJobNumber"]),
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
        ("Weather Data", ["weatherData", "weatherStation"]),
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
        ("Glass U-Factor", "glassU"),
        ("Glass SHGC", "glassSHGC"),
        ("Glass Operable U", "glassOperU"),
        ("Glass Operable SHGC", "glassOperSHGC"),
        ("Sliding Door U", ["glassSGDU", "glassSgdU"]),
        ("Sliding Door SHGC", ["glassSGDSHGC", "glassSgdSHGC"]),
        ("Glass Frame", "glassFrame"),
        ("Glazing Type", "glazingType"),
        ("Glazing Tint", "glazingTint"),
        ("Skylights", "skylights"),
        ("Opaque Door Type", "doorType"),
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


def _work_order_sections(snapshot: Optional[dict]) -> list[dict]:
    """Build the grouped work order from a Wix snapshot. Booleans render Yes/No,
    URL fields render as links, and a section is dropped if all its rows are empty."""
    snap = snapshot or {}
    sections = []
    for title, fields in _WORK_ORDER_SECTIONS:
        rows = []
        has_value = False
        for label, key in fields:
            val, resolved_key = _wo_lookup(snap, key)
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
            rows.append({"label": label, "value": display, "kind": kind})
        if has_value:
            sections.append({"title": title, "rows": rows})
    return sections


@app.route("/job/new-temp", methods=["POST"])
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
    return redirect(url_for("job_star", job_id=job_id))


@app.route("/job/<job_id>/star")
@_require_auth
def job_star(job_id: str):
    """Per-job home tab. For a CMS job it shows the work order + a parse control
    (Drive search, with manual-upload fallback). For a temp job it's the upload
    drop zone. Parsing unlocks the six work tabs."""
    job_dir = _safe_job_path(job_id)

    if job_dir.exists():
        meta = _load_meta(job_id)
        source = meta.get("source") or ("cms" if meta.get("wix_item_id") else "temp")
    else:
        # Not parsed yet — a CMS job opened straight from the landing list.
        record = wix_client.get_project(job_id)
        if not record:
            abort(404)
        source = "cms"
        meta = {
            "wix_item_id":     job_id,
            "wix_snapshot":    record,
            "project_address": (record.get("projectAddress") or "").strip(),
            "engineer": {
                "name":  "Adrienne Gould-Choquette",
                "email": "agc@adicot.com",
                "phone": "(804-787-0468)",
                "state": "Florida",
            },
        }

    parsed = job_dir.exists() and _is_parsed(job_dir)
    wo_sections = _work_order_sections(meta.get("wix_snapshot")) if source == "cms" else None

    return render_template(
        "job_star.html",
        active_tab="star", job_id=job_id, meta=meta,
        source=source, parsed=parsed, wo_sections=wo_sections,
    )


# ─── Routes: Results page ─────────────────────────────────────────────

@app.route("/results/<job_id>")
@_require_auth
@_require_parsed
def results(job_id: str):
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)

    pdfs = []
    out_dir = job_dir / "out"
    if out_dir.exists():
        # The 4 PDFs (3 schedules + Combined) plus the 3 schedule Excel sources
        # that each schedule PDF is rendered from. Equipment/spec files live on
        # their own tabs and are intentionally excluded here.
        deliverables = list(out_dir.glob("*.pdf"))
        for suffix in ("-Ventilation.xlsx", "-Air_Balance.xlsx", "-Load.xlsx"):
            deliverables += out_dir.glob(f"*{suffix}")
        for p in sorted(deliverables):
            pdfs.append({"name": p.name, "size_kb": f"{p.stat().st_size / 1024:.0f}"})

    wix_job_no = ""
    if meta.get("wix_snapshot"):
        wix_job_no = (meta["wix_snapshot"].get("jobNo") or "").strip()

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
        pdfs=pdfs,
        wix_job_no=wix_job_no,
        drive_push=meta.get("drive_push"),
        saved_overrides=saved_overrides,
    )


# ─── Routes: Generate PDFs ────────────────────────────────────────────

@app.route("/job/<job_id>/generate-pdfs", methods=["POST"])
@_require_auth
def generate_pdfs(job_id: str):
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)
    out_dir = job_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    html_name = meta.get("html_name")
    if not html_name:
        flash("Missing html_name in meta.json — can't regenerate PDFs.")
        return redirect(url_for("results", job_id=job_id))

    html_path = job_dir / html_name
    if not html_path.exists():
        flash(f"Source HTML missing on disk: {html_name}")
        return redirect(url_for("results", job_id=job_id))

    cfg_meta = meta.get("config", {})
    toilet_exh = _num_or_default(cfg_meta.get("toilet_exhaust_cfm"), 70)

    config = hp.ProjectConfig(
        toilet_exhaust_cfm=toilet_exh,
        bldg_exhaust_all_toilet=bool(cfg_meta.get("bldg_exhaust_all_toilet", False)),
        project_address=cfg_meta.get("project_address",
                                     meta.get("project_address", "")),
    )
    eng_meta = meta.get("engineer", {})
    engineer = hp.EngineerInfo(
        name=eng_meta.get("name", "Adrienne Gould-Choquette"),
        email=eng_meta.get("email", "agc@adicot.com"),
        phone=eng_meta.get("phone", "(804-787-0468)"),
        state_full=eng_meta.get("state", "Florida"),
    )
    firm = hp.FirmInfo()

    try:
        with redirect_stdout(io.StringIO()):
            hp.build_all_pdfs(
                html_path=html_path,
                config=config,
                engineer=engineer,
                firm=firm,
                out_dir=out_dir,
                zone_overrides=meta.get("zone_overrides") or {},
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
        return redirect(url_for("results", job_id=job_id))

    meta["pdfs_generated"] = True

    # Render the scraped DM HTML once, append it to the standalone Load PDF, and
    # build the combined with the same appendix dead last (after the charts).
    appendix = _html_appendix_bytes(job_dir, meta)
    _append_html_to_load(job_dir, meta, appendix)

    # Build the combined PDF (3 deliverables + selected charts + HTML) before the
    # Drive push so it rides along with the *.pdf upload below.
    _rebuild_combined(job_dir, meta, appendix=appendix)

    drive_push: dict = {"status": "skipped"}
    wix_snapshot = meta.get("wix_snapshot") or {}
    wix_job_no = (wix_snapshot.get("jobNo") or "").strip()
    drive_folder_id = meta.get("drive_folder_id")   # manually chosen job folder, if any

    if not wix_job_no and not drive_folder_id:
        drive_push = {"status": "skipped", "reason": "no Wix project linked (or Wix project has no Job No)"}
    elif not drive_folder_id and gdrive_client._parse_company_from_job_no(wix_job_no) is None:
        drive_push = {"status": "skipped", "reason": f"could not parse company from Job No '{wix_job_no}'"}
    else:
        _XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        upload_targets = list(out_dir.glob("*.pdf"))
        for suffix in ("-Ventilation.xlsx", "-Air_Balance.xlsx", "-Load.xlsx"):
            upload_targets += out_dir.glob(f"*{suffix}")

        pdf_files = []
        for p in sorted(upload_targets):
            mime = _XLSX_MIME if p.suffix == ".xlsx" else "application/pdf"
            try:
                pdf_files.append((p.name, p.read_bytes(), mime))
            except Exception as e:
                drive_push.setdefault("read_errors", []).append({"name": p.name, "message": str(e)})

        if not pdf_files:
            drive_push = {"status": "error", "reason": "PDF pipeline ran but no deliverable files found in out/"}
        else:
            try:
                upload_result = gdrive_client.upload_files(wix_job_no, pdf_files,
                                                           folder_id=drive_folder_id)
                drive_push = {
                    "status": "success" if upload_result["ok"] else "partial",
                    "folder_url": upload_result.get("folder_url"),
                    "uploaded": upload_result.get("uploaded", []),
                    "errors": upload_result.get("errors", []),
                    "job_no": wix_job_no,
                }
                if not upload_result["ok"] and not upload_result.get("uploaded"):
                    drive_push["status"] = "error"
            except Exception as e:
                tb = traceback.format_exc()
                print(f"DRIVE PUSH FAILURE for job {job_id}: {tb}", flush=True)
                drive_push = {"status": "error", "reason": f"{type(e).__name__}: {e}", "job_no": wix_job_no}

    meta["drive_push"] = drive_push
    _save_meta(job_id, meta)

    if drive_push["status"] == "success":
        flash(f"PDFs generated and uploaded to Drive ({wix_job_no}/6-Submit).")
    elif drive_push["status"] == "skipped":
        flash("PDFs generated. (Drive upload skipped — use the download links below.)")
    elif drive_push["status"] == "partial":
        flash("PDFs generated. Some Drive uploads failed — see details below.")
    else:
        flash("PDFs generated, but the Drive upload failed. Use the browser download links and upload manually.")

    return redirect(url_for("results", job_id=job_id))


# ─── Routes: Commit settings + regenerate PDFs ───────────────────────

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


@app.route("/job/<job_id>/commit-settings", methods=["POST"])
@_require_auth
def commit_settings(job_id: str):
    """Save project settings (toilet exhaust, zone overrides) then regenerate PDFs."""
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)

    # Update config
    toilet_cfm = _num_or_default(request.form.get("toilet_exhaust_cfm"), 70)

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
        return redirect(url_for("results", job_id=job_id))

    html_path = job_dir / html_name
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
    return redirect(url_for("results", job_id=job_id))


# ─── Routes: Re-scrape HTML from Drive ────────────────────────────────

@app.route("/job/<job_id>/rescrape", methods=["POST"])
@_require_auth
def rescrape_html(job_id: str):
    """Re-fetch the DM HTML from Drive for jobs originally sourced from Drive,
    then re-parse (report.json + charts + preview + Wix validation). Does NOT
    regenerate PDFs — the user regenerates afterward to pick up the changes."""
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)

    if not meta.get("html_from_drive"):
        flash("This job's HTML wasn't sourced from Drive — re-upload manually to refresh it.")
        return redirect(url_for("results", job_id=job_id))

    wix_item_id = (meta.get("wix_item_id") or "").strip()
    if not wix_item_id:
        flash("No linked Wix project — can't locate the Drive file to re-scrape.")
        return redirect(url_for("results", job_id=job_id))

    wix_record = wix_client.get_project(wix_item_id)
    job_no = (wix_record or {}).get("jobNo", "").strip() if wix_record else ""
    if not job_no:
        flash("Couldn't look up the Wix project's Job No — can't re-scrape from Drive.")
        return redirect(url_for("results", job_id=job_id))

    fetched = gdrive_client.find_html(job_no)
    if fetched is None:
        flash(f"Couldn't fetch the HTML from Drive for {job_no}.")
        return redirect(url_for("results", job_id=job_id))

    drive_filename, drive_bytes = fetched
    html_path = job_dir / (meta.get("html_name") or drive_filename or "dm_hvac-loads1.html")
    html_path.write_bytes(drive_bytes or b"")

    cfg_meta = meta.get("config", {})
    toilet_exh = _num_or_default(cfg_meta.get("toilet_exhaust_cfm"), 70)
    config = hp.ProjectConfig(
        toilet_exhaust_cfm=toilet_exh,
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

    try:
        report, preview = _parse_and_persist(job_dir, html_path, config, engineer)
    except Exception:
        tb = traceback.format_exc()
        (job_dir / "error.log").write_text(tb)
        print(tb, flush=True)
        flash("Re-parsing the refreshed HTML failed — check the Render logs for the traceback.")
        return redirect(url_for("results", job_id=job_id))

    meta["html_name"] = html_path.name
    meta["preview"] = preview
    meta["project_name"] = report.project.project_name

    # Re-run Wix validation against the refreshed report
    wix_snapshot = meta.get("wix_snapshot")
    if wix_snapshot:
        try:
            meta["mismatches"] = validators.compare(report, wix_snapshot)
        except Exception:
            traceback.print_exc()

    _save_meta(job_id, meta)
    flash("Re-scraped the HTML from Drive and re-parsed. Regenerate the PDFs to update the deliverables.")
    return redirect(url_for("results", job_id=job_id))


# ─── Routes: Duct Sizing tab ──────────────────────────────────────────

def _is_zone_loc(loc: str) -> bool:
    return loc.strip().lower().startswith("zone")


def _room_type_tag(loc: str) -> str:
    low = (loc or "").lower()
    if "bath" in low: return "bath"
    if "rr" in low or "restroom" in low: return "rr or corridor"
    if "toilet" in low: return "toilet"
    if "wic" in low: return "WIC"
    if "corridor" in low: return "Corridor"
    return ""


@app.route("/job/<job_id>/duct")
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
        is_zone = _is_zone_loc(loc)
        if is_zone:
            current_zone_index += 1

        required_raw = sa.get("required_supply_cfm") or 0
        if is_zone:
            current_raw = None
        else:
            try:
                current_raw = int(required_raw) if float(required_raw).is_integer() \
                              else float(required_raw)
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


# ─── Routes: Quality Check tab ────────────────────────────────────────

@app.route("/job/<job_id>/quality")
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


@app.route("/job/<job_id>/quality/stories", methods=["POST"])
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
            return redirect(url_for("job_quality", job_id=job_id))
    _save_meta(job_id, meta)
    return redirect(url_for("job_quality", job_id=job_id))


# ─── Routes: Charts tab ───────────────────────────────────────────────

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


@app.route("/job/<job_id>/charts")
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


@app.route("/job/<job_id>/charts/select", methods=["POST"])
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
    wix_job_no = ((meta.get("wix_snapshot") or {}).get("jobNo") or "").strip()
    drive_folder_id = meta.get("drive_folder_id")
    if (combined and combined.exists()
            and (drive_folder_id
                 or (wix_job_no and gdrive_client._parse_company_from_job_no(wix_job_no)))):
        try:
            result = gdrive_client.upload_files(
                wix_job_no,
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
    return redirect(url_for("results", job_id=job_id))


# ─── Routes: Temp jobs ────────────────────────────────────────────────

@app.route("/jobs")
@_require_auth
def past_jobs():
    """Temp jobs — one-off runs not linked to a CMS project. CMS jobs live in the
    landing list (keyed by Wix id), so they're filtered out here."""
    entries = []
    if JOBS_DIR.exists():
        for d in sorted(JOBS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            meta = {}
            try:
                meta = json.loads((d / "meta.json").read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            if meta.get("source") == "cms":
                continue   # CMS jobs are reached from the landing list
            entries.append({
                "job_id": d.name,
                "project_name": meta.get("project_name") or "(unknown)",
                "address": meta.get("project_address", ""),
                "mtime": datetime.datetime.fromtimestamp(
                    d.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    return render_template("jobs.html", active_tab="jobs", job_id=None, entries=entries)


@app.route("/past-jobs")
@_require_auth
def _legacy_past_jobs():
    return redirect(url_for("past_jobs"), code=301)


@app.route("/job/<job_id>/delete", methods=["POST"])
@_require_auth
def delete_job(job_id: str):
    """Delete a job's workspace directory. _job_dir validates the id and 404s on
    a bad/unknown id, so this can't be used for path traversal."""
    job_dir = _job_dir(job_id)
    shutil.rmtree(job_dir, ignore_errors=True)
    flash("Job deleted.")
    return redirect(url_for("past_jobs"))


# ─── Debug routes ─────────────────────────────────────────────────────

@app.route("/debug/equip-status")
@_require_auth
def _debug_equip_status():
    return jsonify({
        "has_equip_selector": HAS_EQUIP_SELECTOR,
        "import_error": _EQUIP_IMPORT_ERROR,
        "hvac_selector_path": str(Path(__file__).parent / "hvac_selector.py"),
        "hvac_selector_exists": (Path(__file__).parent / "hvac_selector.py").exists(),
        "equipment_db_path": str(Path(__file__).parent / "equipment_db.xlsx"),
        "equipment_db_exists": (Path(__file__).parent / "equipment_db.xlsx").exists(),
    })
@_require_auth
def _debug_wix_projects():
    wix_client.invalidate_cache()
    projects = wix_client.list_projects()
    return jsonify({
        "count": len(projects),
        "credentials_set": {
            "WIX_API_KEY": bool(os.environ.get("WIX_API_KEY")),
            "WIX_SITE_ID": bool(os.environ.get("WIX_SITE_ID")),
        },
        "first_20": projects[:20],
    })


@app.route("/debug/gdrive-fetch")
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


# ─── API: check Drive for project HTML ───────────────────────────────

@app.route("/api/check-drive")
@_require_auth
def api_check_drive():
    item_id = request.args.get("wix_item_id", "").strip()
    if not item_id:
        return jsonify({"status": "no_wix_id"}), 400

    record = wix_client.get_project(item_id)
    if not record:
        return jsonify({"status": "wix_lookup_failed", "message": "Couldn't read the Wix project record."})

    job_no = (record.get("jobNo") or "").strip()
    if not job_no:
        return jsonify({"status": "no_job_no", "message": "This Wix project has no Job No."})

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


@app.route("/api/drive/folders")
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


# ─── Routes: public legal pages (required for the QuickBooks app listing) ──
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


@app.route("/legal/<doc>")
def legal_page(doc: str):
    page = _LEGAL_PAGES.get(doc)
    if not page:
        abort(404)
    title, body = page
    return render_template("legal.html", title=title, body=body)


# ─── Routes: QuickBooks connection (OAuth admin) ─────────────────────

@app.route("/quickbooks")
@_require_auth
def quickbooks_admin():
    status = qbo.connection_status()
    company = qbo.company_info() if status.get("connected") else None
    return render_template("quickbooks.html", status=status, company=company)


@app.route("/quickbooks/connect")
@_require_auth
def quickbooks_connect():
    if not qbo.is_configured():
        flash("QuickBooks isn't configured — set QBO_CLIENT_ID, QBO_CLIENT_SECRET, "
              "and QBO_REDIRECT_URI in the environment.")
        return redirect(url_for("quickbooks_admin"))
    state = secrets.token_urlsafe(24)
    session["qbo_state"] = state
    return redirect(qbo.authorize_url(state))


@app.route("/quickbooks/callback")
@_require_auth
def quickbooks_callback():
    if request.args.get("error"):
        flash(f"QuickBooks authorization was cancelled: {request.args.get('error')}")
        return redirect(url_for("quickbooks_admin"))

    state = request.args.get("state", "")
    expected = session.pop("qbo_state", None)
    if not state or state != expected:
        flash("QuickBooks authorization failed (state mismatch) — please try again.")
        return redirect(url_for("quickbooks_admin"))

    code = request.args.get("code", "").strip()
    realm_id = request.args.get("realmId", "").strip()
    if not code or not realm_id:
        flash("QuickBooks authorization failed: missing code or company id.")
        return redirect(url_for("quickbooks_admin"))

    if qbo.exchange_code(code, realm_id):
        flash("Connected to QuickBooks ✓")
    else:
        flash("QuickBooks token exchange failed — check the Render logs.")
    return redirect(url_for("quickbooks_admin"))


@app.route("/quickbooks/disconnect", methods=["POST"])
@_require_auth
def quickbooks_disconnect():
    qbo.disconnect()
    flash("Disconnected from QuickBooks.")
    return redirect(url_for("quickbooks_admin"))


# ─── Invoices tab + QuickBooks invoice creation ──────────────────────

# Maps Wix project id -> created-invoice record, so a project can't be invoiced
# twice and the Invoices tab can show "Invoiced ✓". Lives on the persistent disk,
# scoped per QBO environment so sandbox test invoices never block production ones.

def _invoice_registry_path() -> Path:
    env = (os.environ.get("QBO_ENVIRONMENT") or "sandbox").strip().lower()
    return JOBS_DIR / f"qbo_invoices_{env}.json"


def _load_invoice_registry() -> dict:
    try:
        return json.loads(_invoice_registry_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_invoice_registry(reg: dict) -> None:
    path = _invoice_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2))
    tmp.replace(path)


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


@app.route("/invoices")
@_require_auth
def invoices():
    """Invoice tab — CMS projects with a 'Ready to invoice?' action per row."""
    entries = _build_cms_entries()
    reg = _load_invoice_registry()
    for e in entries:
        e["invoice"] = reg.get(e["_id"])
    return render_template("invoices.html",
                           projects=entries, qbo_status=qbo.connection_status())


@app.route("/api/qbo/lists")
@_require_auth
def api_qbo_lists():
    """Live QBO customers + service items for the modal dropdowns."""
    if not qbo.connection_status().get("connected"):
        return jsonify({"connected": False, "customers": [], "items": []})
    return jsonify({"connected": True,
                    "customers": qbo.list_customers(),
                    "items": qbo.list_service_items()})


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


def _drive_submit_pdfs(job_no: str, folder_id: Optional[str] = None) -> list[dict]:
    """PDFs in the job's Google Drive 6-Submit folder as [{id, name}]. `folder_id`
    (the chosen job folder) overrides the Job-No name walk."""
    pdfs = []
    for f in gdrive_client.list_submit_files(job_no, folder_id=folder_id):
        name = (f.get("name") or "")
        if name.lower().endswith(".pdf") or f.get("mimeType") == "application/pdf":
            pdfs.append({"id": f.get("id"), "name": name})
    pdfs.sort(key=lambda p: p["name"].lower())
    return pdfs


def _attach_drive_pdfs(invoice_id: str, job_no: str, file_ids: list[str],
                       folder_id: Optional[str] = None):
    """Download the selected 6-Submit PDFs from Drive and attach them to the QBO
    invoice. Only ids that belong to this job's 6-Submit folder are honored
    (whitelist). Returns (attached_names, errors)."""
    index = {f["id"]: f["name"]
             for f in _drive_submit_pdfs(job_no, folder_id=folder_id) if f.get("id")}
    attached, errors = [], []
    for fid in file_ids:
        name = index.get(fid)
        if not name:                       # not a file from this project's 6-Submit
            continue
        data = gdrive_client.download_file_bytes(fid)
        if data is None:
            errors.append({"name": name, "error": "Drive download failed"})
            continue
        res = qbo.attach_file(invoice_id, name, data)
        if res.get("ok"):
            attached.append(name)
        else:
            errors.append({"name": name, "error": res.get("error")})
    return attached, errors


@app.route("/api/qbo/prepare/<wix_id>")
@_require_auth
def api_qbo_prepare(wix_id: str):
    """Billing fields + a suggested customer for one project (modal pre-fill)."""
    rec = wix_client.get_project(wix_id) or {}
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
        "company":     company,
        "client_code": code,
        "email":       email,
        "total_cost":  rec.get("totalCost"),
        "description": (rec.get("productService") or rec.get("description") or "").strip(),
        "suggested_customer_id": suggested,
        "already_invoiced": wix_id in _load_invoice_registry(),
        "pdfs": _drive_submit_pdfs(job_no, folder_id=_job_drive_folder_id(wix_id)),
    })


@app.route("/job/<wix_id>/invoice", methods=["POST"])
@_require_auth
def create_invoice_route(wix_id: str):
    """Create the QBO invoice for a project after the engineer confirms the modal."""
    if not qbo.connection_status().get("connected"):
        return jsonify({"ok": False, "error": "Not connected to QuickBooks."}), 400

    reg = _load_invoice_registry()
    if wix_id in reg:
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

    # Attach selected 6-Submit PDFs from Drive (best-effort — a failed attach
    # never undoes the invoice).
    attached, attach_errors = _attach_drive_pdfs(
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


@app.route("/job/<wix_id>/attach", methods=["POST"])
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
        return jsonify({"ok": False, "error": "Select at least one PDF to attach."}), 400

    # Job No drives the Drive 6-Submit lookup; fall back to the live Wix record.
    job_no = rec.get("job_no") or (wix_client.get_project(wix_id) or {}).get("jobNo", "")
    attached, attach_errors = _attach_drive_pdfs(
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


# ─── Routes: file downloads ───────────────────────────────────────────

@app.route("/job/<job_id>/file/<path:filename>")
@_require_auth
def download_file(job_id: str, filename: str):
    job_dir = _job_dir(job_id)
    return send_from_directory(job_dir / "out", filename, as_attachment=True)


@app.route("/job/<job_id>/chart/<path:filename>")
@_require_auth
def download_chart(job_id: str, filename: str):
    job_dir = _job_dir(job_id)
    return send_from_directory(job_dir / "out" / "charts", filename)


# ─── Routes: parse (run the pipeline for a star/temp job) ────────────

@app.route("/job/<job_id>/parse", methods=["POST"])
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
    wix_item_id = "" if is_temp else job_id

    engineer_state = request.form.get("engineer_state", "Florida").strip()
    engineer_name = request.form.get("engineer_name", "Adrienne Gould-Choquette").strip()
    engineer_email = request.form.get("engineer_email", "agc@adicot.com").strip()
    engineer_phone = request.form.get("engineer_phone", "(804-787-0468)").strip()

    f = request.files.get("html_file")
    has_upload = f is not None and f.filename
    drive_bytes: Optional[bytes] = None
    drive_filename: Optional[str] = None

    # For a CMS job the address comes from the Wix record; a temp job can type one.
    wix_record = wix_client.get_project(wix_item_id) if wix_item_id else None
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
    elif wix_item_id:
        if drive_folder_id:
            fetched = gdrive_client.find_html_in_folder(drive_folder_id)
            if fetched is None:
                flash("Couldn't find an HTML in the chosen Drive folder. "
                      "Pick a different folder or upload the file manually.")
                return redirect(url_for("job_star", job_id=job_id))
        else:
            if not job_no:
                flash("Couldn't look up the Wix project's Job No — pick the Drive folder "
                      "manually or upload the HTML.")
                return redirect(url_for("job_star", job_id=job_id))
            fetched = gdrive_client.find_html(job_no)
            if fetched is None:
                flash(f"Couldn't fetch the HTML from Drive for {job_no}. Pick the folder "
                      "manually or upload it.")
                return redirect(url_for("job_star", job_id=job_id))
        drive_filename, drive_bytes = fetched
    else:
        flash("No file uploaded.")
        return redirect(url_for("job_star", job_id=job_id))

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
        return redirect(url_for("job_star", job_id=job_id))

    wix_snapshot = wix_record
    mismatches: list[dict] = []
    if wix_item_id:
        if wix_snapshot is None:
            print(f"WARNING: wix_item_id={wix_item_id} but get_project returned None", flush=True)
        elif report is not None:
            try:
                mismatches = validators.compare(report, wix_snapshot)
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
        "wix_item_id":    wix_item_id,
        "wix_snapshot":   wix_snapshot,
        "mismatches":     mismatches,
        "pdfs_generated": False,
        "drive_push":     None,
    }
    _save_meta(job_id, meta)

    return redirect(url_for("results", job_id=job_id))


# ─── Spec tab ─────────────────────────────────────────────────────────

import re as _re

# Wix API key -> CMS field mapping reference:
#   systemType      -> System Type       (also used for heatType)
#   acMounting      -> AC Mounting
#   coolingEff      -> Cooling Eff       (SEER2 rating)
#   manufacturer    -> Manufacturer      (new field)
#   description     -> Description       (scope text)
#   acNewExisting   -> AC New Existing   (thermostat scope)
#   hasOutsideAir   -> hasOutsideAir     (new boolean field)
#   hasExhaust      -> hasExhaust        (new boolean field)
#   suspCeiling     -> Susp Ceiling      (text: T-bar/Lay-in, GWB, open to roof deck, no)

_STATE_FULL = {
    "FL": "Florida", "AR": "Arkansas", "LA": "Louisiana", "MA": "Massachusetts",
    "OK": "Oklahoma", "PA": "Pennsylvania", "TX": "Texas", "WV": "West Virginia",
    "WY": "Wyoming",
}
_STATE_ABBREV = {v: k for k, v in _STATE_FULL.items()}


def _derive_building_code(base: dict) -> str:
    mc = base.get("mech_code", "")
    m = _re.search(r"(\d{4})", mc)
    yr = m.group(1) if m else ""
    return f"{yr} International Building Code (IBC)".strip()


def _derive_plumbing_code(base: dict) -> str:
    mc = base.get("mech_code", "")
    m = _re.search(r"(\d{4})", mc)
    yr = m.group(1) if m else ""
    return f"{yr} International Plumbing Code (IPC)".strip()


def _spec_state_info(meta: dict) -> tuple[str, dict]:
    """Resolve 2-letter state + STATE_TABLE row for the spec tab.

    Priority:
      1. meta['state_code']       — derived from project_address at upload
      2. wix_snapshot['state']    — Wix record state field
      3. engineer licensed state  — last resort, may differ from project state
    """
    state = (meta.get("state_code") or "").strip().upper()

    if not state:
        snap = meta.get("wix_snapshot") or {}
        state = (snap.get("state") or "").strip().upper()

    if not state:
        eng_state_full = (meta.get("engineer", {}).get("state") or "").strip()
        state = _STATE_ABBREV.get(eng_state_full, "")

    base = dict(hp.STATE_TABLE.get(state, {}))
    si = {
        "state_full":      base.get("state_full", _STATE_FULL.get(state, state)),
        "mech_code":       base.get("mech_code", ""),
        "energy_code":     base.get("energy_code", ""),
        "building_code":   base.get("building_code", _derive_building_code(base)),
        "plumbing_code":   base.get("plumbing_code", _derive_plumbing_code(base)),
        "electrical_code": base.get("electrical_code", "National Electrical Code"),
        "roof_curb_table": base.get("roof_curb_table", ""),
    }
    return state, si


def _spec_cms(meta: dict) -> dict:
    """Pull CMS-sourced spec fields from the Wix snapshot.

    Wix API key      Engine key            Notes
    ---------------- --------------------- ----------------------------------
    systemType       systemType            RTU / split / VRF / package
    systemType       heatType              same field — system type drives both
    acMounting       acMounting            RTU / slab / sidewall / other
    coolingEff       seer2                 SEER2 efficiency rating
    manufacturer     manufacturer          basis-of-design brand
    description      scopeText             plain-English scope sentence
    acNewExisting    thermostatScope       new / existing / new and existing
    hasOutsideAir    hasOutsideAir         boolean
    hasExhaust       hasExhaust            boolean
    suspCeiling      ceilingConcealedGWB   truthy when not blank/no/open
    """
    snap = meta.get("wix_snapshot") or {}
    susp = (snap.get("suspCeiling") or "").strip().lower()
    return {
        "systemType":      snap.get("systemType", ""),
        "heatType":        snap.get("systemType", ""),
        "acMounting":      snap.get("acMounting", ""),
        "seer2":           snap.get("coolingEff", ""),
        "manufacturer":    snap.get("manufacturer", ""),
        "scopeText":       snap.get("description", ""),
        "thermostatScope": snap.get("acNewExisting", ""),
        "hasOutsideAir":   snap.get("hasOutsideAir", False),
        "hasExhaust":      snap.get("hasExhaust", False),
        "ceilingConcealedGWB": susp not in ("", "no", "open to roof deck"),
    }


def _spec_loadcalc(meta: dict, job_id: str) -> dict:
    """Pull computed load values from report.json for the spec."""
    report = _load_report(job_id)
    lt = report.get("load_total_system") or []
    tons = 0.0
    supply = 0.0
    heat = 0.0
    maxcfm = 0.0
    for z in lt:
        try:
            tons += float(z.get("cool_total_tons") or 0)
        except (TypeError, ValueError):
            pass
        try:
            scfm = float(z.get("cool_cfm") or 0)
            supply += scfm
            maxcfm = max(maxcfm, scfm)
        except (TypeError, ValueError):
            pass
        try:
            heat += float(z.get("heat_btuh") or 0)
        except (TypeError, ValueError):
            pass
    proj = report.get("project") or {}
    return {
        "coolingTons":  (f"{tons:g}" if tons else ""),
        "heatingBtuh":  (f"{int(round(heat)):,}" if heat else ""),
        "supplyCFM":    (f"{int(round(supply)):,}" if supply else ""),
        "maxSystemCFM": maxcfm,
        "outdoorDB":    (str(int(round(proj["osa_high_db_f"]))) if proj.get("osa_high_db_f") else ""),
        "outdoorWB":    (str(int(round(proj["osa_high_wb_f"]))) if proj.get("osa_high_wb_f") else ""),
        "indoorDB":     (str(int(round(proj["default_cooling_temp_f"]))) if proj.get("default_cooling_temp_f") else ""),
    }


def _spec_inputs(meta: dict, cms: dict, loadcalc: dict) -> dict:
    """Merge saved engineer edits over CMS/load-calc pre-fills.

    Priority: saved edits > CMS > load calc > hardcoded default.
    """
    saved = meta.get("spec_inputs", {})

    def pick(key, *fallbacks):
        if saved.get(key) not in (None, ""):
            return saved[key]
        for fb in fallbacks:
            if fb not in (None, ""):
                return fb
        return ""

    return {
        "systemType":          pick("systemType", cms.get("systemType")),
        "heatType":            pick("heatType", cms.get("heatType")),
        "acMounting":          pick("acMounting", cms.get("acMounting")),
        "maxSystemCFM":        pick("maxSystemCFM", loadcalc.get("maxSystemCFM")),
        "hasOutsideAir":       saved.get("hasOutsideAir", cms.get("hasOutsideAir", False)),
        "hasExhaust":          saved.get("hasExhaust", cms.get("hasExhaust", False)),
        "ceilingConcealedGWB": saved.get("ceilingConcealedGWB", cms.get("ceilingConcealedGWB", False)),
        "tbMode":              pick("tbMode", "recommend"),
        "hasVavOrFireSmoke":   saved.get("hasVavOrFireSmoke", False),
        "hasExistingControls": saved.get("hasExistingControls", False),
    }


@app.route("/job/<job_id>/spec")
@_require_auth
@_require_parsed
def job_spec(job_id: str):
    """Specifications tab — pre-filled editable inputs + live spec preview."""
    _job_dir(job_id)
    meta = _load_meta(job_id)

    state, state_info = _spec_state_info(meta)
    cms = _spec_cms(meta)
    loadcalc = _spec_loadcalc(meta, job_id)
    inputs = _spec_inputs(meta, cms, loadcalc)

    ctx = spec_engine.build_context(state, state_info, inputs, cms=cms, loadcalc=loadcalc)
    data = spec_data.load_spec_data()
    spec = spec_engine.build_spec(data, ctx, include_notes=False)

    return render_template(
        "job_spec.html",
        active_tab="spec", job_id=job_id, meta=meta,
        inputs=inputs, state=state, state_info=state_info,
        spec=spec, warnings=spec.warnings,
    )


@app.route("/job/<job_id>/spec/save", methods=["POST"])
@_require_auth
def job_spec_save(job_id: str):
    """Persist edited spec inputs, then redirect to the preview."""
    _job_dir(job_id)
    meta = _load_meta(job_id)

    def _cb(name):
        return request.form.get(name) == "on"

    meta["spec_inputs"] = {
        "systemType":          request.form.get("systemType", "").strip(),
        "heatType":            request.form.get("heatType", "").strip(),
        "acMounting":          request.form.get("acMounting", "").strip(),
        "maxSystemCFM":        request.form.get("maxSystemCFM", "").strip(),
        "hasOutsideAir":       _cb("hasOutsideAir"),
        "hasExhaust":          _cb("hasExhaust"),
        "ceilingConcealedGWB": _cb("ceilingConcealedGWB"),
        "tbMode":              request.form.get("tbMode", "recommend").strip(),
        "hasVavOrFireSmoke":   _cb("hasVavOrFireSmoke"),
        "hasExistingControls": _cb("hasExistingControls"),
    }
    _save_meta(job_id, meta)
    return redirect(url_for("job_spec", job_id=job_id))


@app.route("/job/<job_id>/spec/download-docx", methods=["POST"])
@_require_auth
def job_spec_download_docx(job_id: str):
    """Generate the spec .docx and send it directly as a download."""
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)

    state, state_info = _spec_state_info(meta)
    cms = _spec_cms(meta)
    loadcalc = _spec_loadcalc(meta, job_id)
    inputs = _spec_inputs(meta, cms, loadcalc)

    ctx = spec_engine.build_context(state, state_info, inputs, cms=cms, loadcalc=loadcalc)
    data = spec_data.load_spec_data()
    rendered = spec_engine.build_spec(data, ctx, include_notes=False)

    project_name = meta.get("project_name", "Specification")
    safe = project_name.replace(" ", "_").replace("/", "-")
    out_path = job_dir / "out" / f"{safe}-Specifications.docx"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        spec_docx.build_specification_docx(
            rendered, out_path,
            project_name=project_name,
            project_address=meta.get("project_address", ""),
            code_label=state_info.get("mech_code", ""),
        )
    except Exception as e:
        flash(f"Word doc generation failed: {e}")
        return redirect(url_for("job_spec", job_id=job_id))

    return send_file(
        out_path,
        as_attachment=True,
        download_name=out_path.name,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# ─── Routes: Generate DM Setup tab ───────────────────────────────────

# Confirmed DM construction "type code" (iType / mass class) values, per the real
# dm_hvac.dm. The engineer picks one per row (never auto-filled); each row's dropdown
# also includes the .dm's own value so a valid code is always available.
_MASS_CLASS_OPTIONS = {
    "wall": [("Frame", 2), ("Wood stud", 5), ("Block / CMU", 10)],
    "roof": [("Wood deck / vented attic", 4), ("Frame", 2), ("Concrete / masonry", 10)],
    "door": [("Steel, insulated", 2), ("Wood", 5)],
}


def _num(v):
    """First number found in v (e.g. 'R-19' -> 19.0, '0.44' -> 0.44), else None."""
    if v is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


def _r_to_u(v):
    """R-value string -> assembly U (U = 1/R). Values <=1 are treated as already-U."""
    n = _num(v)
    if not n or n <= 0:
        return None
    return round(1.0 / n, 3) if n > 1 else round(n, 3)


def _wix_envelope(snap: dict) -> dict:
    """Envelope spec candidates pulled from the Wix work-order record (if any)."""
    snap = snap or {}
    wc, rc = (snap.get("wallColor") or ""), (snap.get("roofColor") or "")
    return {
        "wall_primary_u": _r_to_u(snap.get("wallRValue")),
        "wall_part_u":    _r_to_u(snap.get("partRValue")),
        "wall_dark":      "dark" in wc.lower(), "wall_has_color": bool(wc),
        "roof_u":         _r_to_u(snap.get("roofRValue")),
        "roof_dark":      "dark" in rc.lower(), "roof_has_color": bool(rc),
        "glass_u":        _num(snap.get("glassU")),
        "glass_shgc":     _num(snap.get("glassSHGC")),
    }


def _mass_options(cat: str, export_itype):
    """(label, code) options for a row's mass-class dropdown, always including the
    .dm's own code so nothing valid is lost."""
    opts = list(_MASS_CLASS_OPTIONS.get(cat, []))
    if export_itype is not None and all(code != export_itype for _, code in opts):
        opts.append((f"As in .dm (type {export_itype})", export_itype))
    return opts


def _dm_setup_construction(report: dict, meta: dict) -> dict:
    """Editable construction-type rows: DM-export list, spec fields prefilled from
    the Wix work-order when available (source-tagged), .dm value as fallback."""
    wix = _wix_envelope(meta.get("wix_snapshot") or {})

    def pick(wix_val, export_val):
        """Return (value, source): Wix wins, then the .dm value, else blank."""
        if wix_val is not None:
            return wix_val, "Wix"
        if export_val is not None:
            return export_val, ".dm"
        return "", ""

    def opaque(cat, items):
        rows = []
        for c in items:
            if not c.get("name"):
                continue
            name = c["name"]
            if cat == "wall":
                wu = wix["wall_part_u"] if "part" in name.lower() else wix["wall_primary_u"]
                wu = wu if wu is not None else wix["wall_primary_u"]
                wdark, wdark_src = (wix["wall_dark"], "Wix") if wix["wall_has_color"] \
                    else ("dark" in (c.get("color") or "").lower(), ".dm")
            elif cat == "roof":
                wu = wix["roof_u"]
                wdark, wdark_src = (wix["roof_dark"], "Wix") if wix["roof_has_color"] \
                    else ("dark" in (c.get("color") or "").lower(), ".dm")
            else:  # door — Wix has no door U/color
                wu = None
                wdark, wdark_src = ("dark" in (c.get("color") or "").lower(), ".dm")
            u, u_src = pick(wu, c.get("u_value"))
            rows.append({
                "name": name, "u": u, "u_source": u_src,
                "dark": bool(wdark), "dark_source": wdark_src,
                "options": _mass_options(cat, c.get("ashrae_type")),
            })
        # Pre-parse (no .dm export yet): synthesize rows from the Wix work-order.
        if not rows:
            def wrow(name, u, dark, has_color):
                return {"name": name,
                        "u": u if u is not None else "", "u_source": "Wix" if u is not None else "",
                        "dark": bool(dark), "dark_source": "Wix" if has_color else "",
                        "options": _mass_options(cat, None)}
            if cat == "wall":
                if wix["wall_primary_u"] is not None:
                    rows.append(wrow("Exterior wall (work order)", wix["wall_primary_u"], wix["wall_dark"], wix["wall_has_color"]))
                if wix["wall_part_u"] is not None:
                    rows.append(wrow("Partition (work order)", wix["wall_part_u"], wix["wall_dark"], wix["wall_has_color"]))
            elif cat == "roof" and wix["roof_u"] is not None:
                rows.append(wrow("Roof (work order)", wix["roof_u"], wix["roof_dark"], wix["roof_has_color"]))
            # door: Wix has no door U — nothing to synthesize
        return rows

    glasses = []
    for c in report.get("glass_types", []):
        if not c.get("name"):
            continue
        u, u_src = pick(wix["glass_u"], c.get("u_value"))
        s, s_src = pick(wix["glass_shgc"], c.get("shgc"))
        glasses.append({"name": c["name"], "u": u, "u_source": u_src,
                        "shgc": s, "shgc_source": s_src})
    if not glasses and wix["glass_u"] is not None:
        glasses.append({"name": "Glazing (work order)",
                        "u": wix["glass_u"], "u_source": "Wix",
                        "shgc": wix["glass_shgc"] if wix["glass_shgc"] is not None else "",
                        "shgc_source": "Wix" if wix["glass_shgc"] is not None else ""})

    return {
        "walls": opaque("wall", report.get("wall_types", [])),
        "roofs": opaque("roof", report.get("roof_types", [])),
        "doors": opaque("door", report.get("door_types", [])),
        "glasses": glasses,
        "from_wix": bool(meta.get("wix_snapshot")),
    }


@app.route("/job/<job_id>/dm-setup")
@_require_auth
def job_dm_setup(job_id: str):
    # Available alongside the work order — no parsed report required. report may be {}.
    _job_dir(job_id)
    meta = _load_meta(job_id)
    report = _load_report(job_id)

    if not HAS_DM_SETUP_GENERATOR:
        flash(f"DM Setup generator unavailable: {_DM_SETUP_IMPORT_ERROR}")
        return redirect(url_for("results", job_id=job_id))

    library = dmsg.list_room_types()                       # [{name, source, summary}]
    lib_names = {rt["name"] for rt in library}
    # Room types this job actually uses (parsed from the DM export)
    used_in_lib = sorted({r.get("name") for r in report.get("rooms_p1", [])
                          if r.get("name")} & lib_names)

    saved = meta.get("dm_setup_inputs", {})
    selected = set(saved.get("selected_room_types") or used_in_lib)

    # Group the library by source for display (170 / FBC / 621 / other)
    order = {"170": 0, "FBC": 1, "621": 2}
    groups: dict[str, list] = {}
    for rt in library:
        groups.setdefault(rt.get("source") or "Other", []).append(rt)
    grouped = sorted(groups.items(),
                     key=lambda kv: (order.get(kv[0], 9), kv[0]))

    con = _dm_setup_construction(report, meta)
    return render_template(
        "job_dm_setup.html",
        active_tab="dm-setup", job_id=job_id, meta=meta,
        parsed=_is_parsed(_job_dir(job_id)),
        grouped=grouped, selected=selected, used_in_lib=used_in_lib,
        lib_count=len(library), **con,
    )


@app.route("/job/<job_id>/dm-setup/generate", methods=["POST"])
@_require_auth
def job_dm_setup_generate(job_id: str):
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)
    report = _load_report(job_id)

    if not HAS_DM_SETUP_GENERATOR:
        flash(f"DM Setup generator unavailable: {_DM_SETUP_IMPORT_ERROR}")
        return redirect(url_for("results", job_id=job_id))

    selected = request.form.getlist("room_types")
    errors: list[str] = []

    def _f(key):
        return (request.form.get(key) or "").strip()

    def read_opaque(cat):
        """Read included wall/roof/door rows from the editable form fields."""
        out = []
        for i in request.form.getlist(f"{cat}_include"):
            name = _f(f"{cat}_name_{i}")
            if not name:
                continue
            u, t = _f(f"{cat}_u_{i}"), _f(f"{cat}_type_{i}")
            if not u or not t:
                errors.append(f"{name}: needs both a U-value and a mass class.")
                continue
            try:
                out.append({"name": name, "description": name, "u": float(u),
                            "itype": int(t),
                            "dark": request.form.get(f"{cat}_dark_{i}") is not None})
            except ValueError:
                errors.append(f"{name}: U-value must be a number.")
        return out

    def read_glass():
        out = []
        for i in request.form.getlist("glass_include"):
            name = _f(f"glass_name_{i}")
            if not name:
                continue
            u, s = _f(f"glass_u_{i}"), _f(f"glass_shgc_{i}")
            if not u:
                errors.append(f"{name}: needs a U-value.")
                continue
            try:
                out.append({"name": name, "description": name,
                            "u": float(u), "shgc": float(s) if s else 0.0})
            except ValueError:
                errors.append(f"{name}: U-value and SHGC must be numbers.")
        return out

    walls = read_opaque("wall")
    roofs = read_opaque("roof")
    doors = read_opaque("door")
    glasses = read_glass()

    if errors:
        for e in errors:
            flash(e)
        return redirect(url_for("job_dm_setup", job_id=job_id))

    if not selected and not (walls or roofs or doors or glasses):
        flash("Select at least one room type or construction type to generate a setup script.")
        return redirect(url_for("job_dm_setup", job_id=job_id))

    # Persist the room-type selection so it repopulates on the next visit.
    # (Construction rows re-prefill from Wix/.dm on each load.)
    meta["dm_setup_inputs"] = {"selected_room_types": selected}
    _save_meta(job_id, meta)

    try:
        vbs = dmsg.render_setup_vbs(
            meta.get("project_name") or job_id, selected,
            wall_types=walls, glass_types=glasses,
            roof_types=roofs, door_types=doors,
        )
    except KeyError as e:
        flash(f"Could not generate setup script: {e}")
        return redirect(url_for("job_dm_setup", job_id=job_id))

    safe = secure_filename(meta.get("project_name") or job_id) or "job"
    out_path = job_dir / "out" / f"{safe}-DM-Setup.vbs"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(vbs, encoding="utf-8")

    return send_file(
        out_path,
        as_attachment=True,
        download_name=out_path.name,
        mimetype="text/vbscript",
    )


# ─── Routes: Equipment Selection tab ─────────────────────────────────

_EQUIP_TYPE_MAP = {
    # single type → one result per zone
    "ac_single": ([eng.AC_SINGLE], None),       # (ac_types, hp_types); None = not run
    "ac_two":    ([eng.AC_TWO],   None),
    "hp_single": (None, [eng.HP_SINGLE]),
    "hp_two":    (None, [eng.HP_TWO]),
    "ac":        ([eng.AC_SINGLE, eng.AC_TWO], None),
    "hp":        (None, [eng.HP_SINGLE, eng.HP_TWO]),
    # "all" → run both AC and HP, return side-by-side
    "all":       ([eng.AC_SINGLE, eng.AC_TWO], [eng.HP_SINGLE, eng.HP_TWO]),
}


def _build_equip_zones(job_id: str) -> list[dict]:
    """Pull zone loads from report.json and convert Btuh → kBtu/h."""
    report = _load_report(job_id)
    zones = []
    for lt in report.get("load_total_system", []):
        name = lt.get("location", "")
        if not name:
            continue
        tc  = (lt.get("cool_total_btuh")    or 0) / 1000
        shc = (lt.get("cool_sensible_btuh") or 0) / 1000
        htg = (lt.get("heat_btuh")          or 0) / 1000
        if tc <= 0:
            continue
        zones.append({"name": name, "tc": tc, "shc": shc, "htg": htg})
    return zones


def _build_equip_conditions(job_id: str) -> dict:
    """Pull outdoor design conditions from report.json."""
    report = _load_report(job_id)
    proj = report.get("project") or {}
    return {
        "odb": proj.get("osa_high_db_f"),
        "owb": proj.get("osa_high_wb_f"),
    }


@app.route("/job/<job_id>/equip")
@_require_auth
@_require_parsed
def job_equip(job_id: str):
    """Equipment Selection tab — pre-filled from load calc."""
    _job_dir(job_id)
    meta = _load_meta(job_id)
    zones = _build_equip_zones(job_id)
    conds = _build_equip_conditions(job_id)
    last  = meta.get("equip_inputs", {})

    return render_template(
        "job_equip.html",
        active_tab="equip", job_id=job_id, meta=meta,
        zones=zones,
        odb=last.get("odb") or conds.get("odb"),
        owb=last.get("owb") or conds.get("owb"),
        edb=last.get("edb", 80),
        ewb=last.get("ewb", 67),
        cap_min=last.get("cap_min", 100),
        cap_max=last.get("cap_max", 115),
        eq_type=last.get("eq_type", "all"),
        results=None,
        xlsx_name=None,
    )


@app.route("/job/<job_id>/equip/run", methods=["POST"])
@_require_auth
def job_equip_run(job_id: str):
    """Run equipment selection and render results."""
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)

    if not HAS_EQUIP_SELECTOR:
        flash(f"Equipment selector unavailable: {_EQUIP_IMPORT_ERROR}")
        return redirect(url_for("job_equip", job_id=job_id))

    def _f(key, default=None):
        v = (request.form.get(key) or "").strip()
        return float(v) if v else default

    odb     = _f("odb")
    owb     = _f("owb")
    edb     = _f("edb", 80.0)
    ewb     = _f("ewb", 67.0)
    cap_min = _f("cap_min", 100.0)
    cap_max = _f("cap_max", 115.0)
    eq_type = request.form.get("eq_type", "all")

    if odb is None or owb is None:
        flash("Outdoor dry bulb and wet bulb are required.")
        return redirect(url_for("job_equip", job_id=job_id))

    names = request.form.getlist("zone_name")
    tcs   = request.form.getlist("zone_tc")
    shcs  = request.form.getlist("zone_shc")
    htgs  = request.form.getlist("zone_htg")

    zones_input   = []
    zones_display = []
    for i, name in enumerate(names):
        tc_v  = float(tcs[i])  if i < len(tcs)  and tcs[i]  else 0
        shc_v = float(shcs[i]) if i < len(shcs) and shcs[i] else 0
        htg_v = float(htgs[i]) if i < len(htgs) and htgs[i] else 0
        if tc_v <= 0:
            continue
        zones_input.append({
            "name": name,
            "total_cooling_kbtu":   tc_v,
            "sensible_cooling_kbtu": shc_v,
            "total_heating_kbtu":   htg_v,
        })
        zones_display.append({"name": name, "tc": tc_v, "shc": shc_v, "htg": htg_v})

    if not zones_input:
        flash("No zones with a cooling load found.")
        return redirect(url_for("job_equip", job_id=job_id))

    meta["equip_inputs"] = {
        "odb": odb, "owb": owb, "edb": edb, "ewb": ewb,
        "cap_min": cap_min, "cap_max": cap_max, "eq_type": eq_type,
    }
    _save_meta(job_id, meta)

    # Run selection — "all" gives AC + HP side by side; others give one result per zone
    try:
        ac_types, hp_types = _EQUIP_TYPE_MAP.get(eq_type, ([eng.AC_SINGLE, eng.AC_TWO], [eng.HP_SINGLE, eng.HP_TWO]))
        multi_mode = (ac_types is not None and hp_types is not None)

        if multi_mode:
            results = eng.select_equipment_multi(
                zones_input, odb, owb, edb, ewb,
                cap_min, cap_max,
                ac_types=ac_types, hp_types=hp_types,
            )
        else:
            equipment_types = ac_types if ac_types else hp_types
            raw = eng.select_equipment(
                zones_input, odb, owb, edb, ewb,
                cap_min, cap_max,
                equipment_types=equipment_types,
            )
            # Normalise to same shape as multi for template reuse
            results = []
            for r in raw:
                is_hp = equipment_types and any(t in [eng.HP_SINGLE, eng.HP_TWO] for t in equipment_types)
                results.append({
                    "zone":   r["zone"],
                    "tc_load": r["tc_load"],
                    "shc_load": r["shc_load"],
                    "ac":  r["selected"] if not is_hp else None,
                    "ac_out_of_bounds": r.get("out_of_bounds", False) if not is_hp else False,
                    "ac_all_candidates": r.get("all_candidates", []) if not is_hp else [],
                    "hp":  r["selected"] if is_hp else None,
                    "hp_out_of_bounds": r.get("out_of_bounds", False) if is_hp else False,
                    "hp_all_candidates": r.get("all_candidates", []) if is_hp else [],
                    "multi_mode": False,
                })
            multi_mode = False

        if multi_mode:
            for r in results:
                r["multi_mode"] = True
    except Exception:
        tb = traceback.format_exc()
        print(tb, flush=True)
        flash("Equipment selection failed — check Render logs.")
        return redirect(url_for("job_equip", job_id=job_id))

    # Flag HP heating data gaps (for any HP selections)
    hp_series = {"GH5SAN5", "GH8TAN5"}
    for res in results:
        for key in ("ac", "hp"):
            sel = res.get(key)
            if sel:
                sel["htg_data_missing"] = bool(
                    sel.get("odu_series") in hp_series
                    and sel.get("htg_load_kbtu")
                    and sel.get("htg_cap_kbtu") is None
                )

    # Write Excel schedule into job's out/ folder
    project_name = meta.get("project_name", "Project")
    safe  = project_name.replace(" ", "_").replace("/", "-")
    import uuid as _uuid
    token = _uuid.uuid4().hex[:6]
    xlsx_name = f"{safe}_{token}_schedule.xlsx"
    xlsx_path = job_dir / "out" / xlsx_name
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)

    # write_excel_schedule expects the old shape: [{selected, zone, tc_load, shc_load}, ...]
    # Flatten multi-mode results (AC + HP) into separate rows for the schedule.
    try:
        flat_results = []
        for r in results:
            if r.get("multi_mode"):
                # Add AC row then HP row
                for sel_key, oob_key, label in [
                    ("ac", "ac_out_of_bounds", "A/C"),
                    ("hp", "hp_out_of_bounds", "Heat Pump"),
                ]:
                    flat_results.append({
                        "zone": f"{r['zone']} ({label})",
                        "tc_load": r["tc_load"],
                        "shc_load": r["shc_load"],
                        "selected": r[sel_key],
                        "next_smaller": None,
                        "next_larger": None,
                        "all_candidates": r.get(f"{sel_key}_all_candidates", []),
                        "htg_data_missing": (r[sel_key] or {}).get("htg_data_missing", False) if r[sel_key] else False,
                    })
            else:
                sel = r.get("ac") or r.get("hp")
                flat_results.append({
                    "zone": r["zone"],
                    "tc_load": r["tc_load"],
                    "shc_load": r["shc_load"],
                    "selected": sel,
                    "next_smaller": None,
                    "next_larger": None,
                    "all_candidates": r.get("ac_all_candidates") or r.get("hp_all_candidates") or [],
                    "htg_data_missing": (sel or {}).get("htg_data_missing", False) if sel else False,
                })

        eng.write_excel_schedule(
            flat_results, str(xlsx_path), project_name, odb, owb, cap_min, cap_max
        )
    except Exception:
        traceback.print_exc()
        flash("Excel schedule generation failed — check Render logs.")
        xlsx_name = None

    return render_template(
        "job_equip.html",
        active_tab="equip", job_id=job_id, meta=meta,
        zones=zones_display,
        odb=odb, owb=owb, edb=edb, ewb=ewb,
        cap_min=cap_min, cap_max=cap_max, eq_type=eq_type,
        results=results,
        xlsx_name=xlsx_name,
    )


@app.route("/job/<job_id>/equip/download/<path:fname>")
@_require_auth
def job_equip_download(job_id: str, fname: str):
    """Download a generated equipment schedule."""
    if "/" in fname or "\\" in fname or ".." in fname:
        abort(400)
    job_dir  = _job_dir(job_id)
    out_dir  = job_dir / "out"
    if not (out_dir / fname).exists():
        abort(404)
    return send_from_directory(out_dir, fname, as_attachment=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    print(f"Auth: {'enabled' if APP_PASSWORD else 'DISABLED (no APP_PASSWORD)'}\"")
    app.run(host="0.0.0.0", port=port, debug=debug)
