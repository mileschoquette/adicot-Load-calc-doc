"""HVAC Loads Pipeline — Flask UI with multi-tab support.

Routes (URL → view function name):
    /                                index           Upload form (no job loaded)
    /upload                          upload          POST handler — parse only (no PDFs yet)
    /results/<job_id>                results         Results page (preview + PDFs section)
    /job/<job_id>/generate-pdfs      generate_pdfs   POST — runs PDF pipeline + Drive push
    /job/<job_id>/commit-settings    commit_settings POST — save settings + regenerate PDFs
    /job/<job_id>/duct               job_duct        Duct Sizing tab
    /job/<job_id>/charts             job_charts      Charts tab
    /job/<job_id>/spec               job_spec        Specifications tab
    /job/<job_id>/spec/save          job_spec_save   POST — save spec inputs and render output
    /job/<job_id>/spec/download-docx job_spec_download_docx POST — generate + download Word Doc
    /jobs                            past_jobs       Past jobs index
    /past-jobs                                       301 redirect → /jobs
    /job/<job_id>/file/<name>        download_file   File download (PDFs / DXF / DOCX)
    /job/<job_id>/chart/<name>       download_chart  Inline-serve a chart PNG

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

import functools
import io
import json
import os
import re
import secrets
import traceback
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from flask import (Flask, render_template, request, send_from_directory,
                   send_file, abort, redirect, url_for, flash, Response, jsonify)
from werkzeug.utils import secure_filename

import hvac_pipeline as hp
from charts import render_all_charts
import wix_client
import validators
import gdrive_client
import spec_engine
import spec_data
import spec_docx

# ── Equipment selector (optional — graceful fallback if files not present) ──
try:
    import hvac_selector as eng
    HAS_EQUIP_SELECTOR = True
    _EQUIP_IMPORT_ERROR = None
except Exception as _e:
    HAS_EQUIP_SELECTOR = False
    _EQUIP_IMPORT_ERROR = str(_e)

# ─── Paths ───────────────────────────────────────────────────────────
APP_DIR = Path(__file__).resolve().parent
JOBS_DIR = Path(os.environ.get("JOBS_DIR", APP_DIR / "jobs"))
JOBS_DIR.mkdir(parents=True, exist_ok=True)

# ─── Auth ────────────────────────────────────────────────────────────
APP_USERNAME = "adicot"
APP_PASSWORD = os.environ.get("APP_PASSWORD")

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

def _job_dir(job_id: str) -> Path:
    safe_id = secure_filename(job_id)
    if not safe_id or safe_id != job_id:
        abort(404)
    d = (JOBS_DIR / safe_id).resolve()
    if not d.exists() or not d.is_dir():
        abort(404)
    if JOBS_DIR.resolve() not in d.parents:
        abort(404)
    return d


def _load_meta(job_id: str) -> dict:
    try:
        return json.loads((_job_dir(job_id) / "meta.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_meta(job_id: str, meta: dict) -> None:
    (_job_dir(job_id) / "meta.json").write_text(
        json.dumps(meta, indent=2, default=str)
    )


def _load_report(job_id: str) -> dict:
    try:
        return json.loads((_job_dir(job_id) / "report.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ─── State code helper ────────────────────────────────────────────────

def _extract_state_code(address: str) -> str:
    """Pull 2-letter state abbreviation from a US address string.
    e.g. '123 Main St, Miami, FL 33101' -> 'FL'
    """
    if not address:
        return ""
    m = re.search(r'\b([A-Z]{2})\b(?:\s+\d{5}(?:-\d{4})?)?(?:\s*$|,)', address.strip())
    return m.group(1) if m else ""


# ─── Routes: upload form ─────────────────────────────────────────────

def _build_wix_dropdown_entries() -> list[dict]:
    entries = []
    for p in wix_client.list_projects():
        addr = (p.get("projectAddress") or "").strip()
        job_no = (p.get("jobNo") or "").strip()
        title = (p.get("title") or "").strip()
        if not addr and not job_no:
            continue
        if addr:
            label = f"{addr} — {job_no}" if job_no else addr
        else:
            display = title or "(untitled)"
            label = f"{display} — {job_no}" if job_no else display
        entries.append({
            "_id": p.get("_id", ""),
            "projectAddress": addr,
            "label": label,
        })
    return entries


@app.route("/")
@_require_auth
def index():
    return render_template(
        "index.html",
        active_tab="upload", job_id=None,
        wix_projects=_build_wix_dropdown_entries(),
    )


# ─── Routes: Results page ─────────────────────────────────────────────

@app.route("/results/<job_id>")
@_require_auth
def results(job_id: str):
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)

    pdfs = []
    out_dir = job_dir / "out"
    if out_dir.exists():
        for p in sorted(out_dir.glob("*.pdf")):
            pdfs.append({"name": p.name, "size_kb": f"{p.stat().st_size / 1024:.0f}"})

    wix_job_no = ""
    if meta.get("wix_snapshot"):
        wix_job_no = (meta["wix_snapshot"].get("jobNo") or "").strip()

    # Shape saved zone_overrides back into an enumerated list for the template
    # zone_overrides is {html_zone_name: {display_name?, tons?, supply_cfm?, merge_with?}}
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
    try:
        toilet_exh = float(cfg_meta.get("toilet_exhaust_cfm") or 70)
    except (TypeError, ValueError):
        toilet_exh = 70.0

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

    drive_push: dict = {"status": "skipped"}
    wix_snapshot = meta.get("wix_snapshot") or {}
    wix_job_no = (wix_snapshot.get("jobNo") or "").strip()

    if not wix_job_no:
        drive_push = {"status": "skipped", "reason": "no Wix project linked (or Wix project has no Job No)"}
    elif gdrive_client._parse_company_from_job_no(wix_job_no) is None:
        drive_push = {"status": "skipped", "reason": f"could not parse company from Job No '{wix_job_no}'"}
    else:
        pdf_files = []
        for p in sorted(out_dir.glob("*.pdf")):
            try:
                pdf_files.append((p.name, p.read_bytes(), "application/pdf"))
            except Exception as e:
                drive_push.setdefault("read_errors", []).append({"name": p.name, "message": str(e)})

        if not pdf_files:
            drive_push = {"status": "error", "reason": "PDF pipeline ran but no .pdf files found in out/"}
        else:
            try:
                upload_result = gdrive_client.upload_files(wix_job_no, pdf_files)
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

    Form fields: ov_match_N, ov_display_N, ov_tons_N, ov_supply_N, ov_merge_N
    Returns {html_zone_name: {display_name?, tons?, supply_cfm?, merge_with?}}
    """
    # Collect all index suffixes present
    indices = set()
    for key in form.keys():
        for prefix in ("ov_match_", "ov_display_", "ov_tons_", "ov_supply_", "ov_merge_"):
            if key.startswith(prefix):
                indices.add(key[len(prefix):])

    overrides = {}
    for idx in sorted(indices):
        match = form.get(f"ov_match_{idx}", "").strip()
        if not match:
            continue  # ignore rows with no match key
        ov = {}
        display = form.get(f"ov_display_{idx}", "").strip()
        tons    = form.get(f"ov_tons_{idx}", "").strip()
        supply  = form.get(f"ov_supply_{idx}", "").strip()
        merge   = form.get(f"ov_merge_{idx}", "").strip()
        if display: ov["display_name"] = display
        if tons:
            try: ov["tons"] = float(tons)
            except ValueError: pass
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
    try:
        toilet_cfm = float(request.form.get("toilet_exhaust_cfm", "70") or 70)
    except (TypeError, ValueError):
        toilet_cfm = 70.0

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
        toilet_exhaust_cfm=float(cfg_meta.get("toilet_exhaust_cfm") or 70),
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
        flash("Settings saved and PDFs regenerated.")
    except Exception:
        tb = traceback.format_exc()
        (job_dir / "pdf_error.log").write_text(tb)
        print(tb, flush=True)
        meta["pdfs_generated"] = False
        flash("Settings saved, but PDF regeneration failed — check Render logs.")

    _save_meta(job_id, meta)
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


@app.route("/job/<job_id>/charts")
@_require_auth
def job_charts(job_id: str):
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)
    charts_dir = job_dir / "out" / "charts"

    charts = []
    if charts_dir.exists():
        order = ["sensible_vs_latent.png"]
        order += sorted(p.name for p in charts_dir.glob("cooling_breakdown_*.png"))
        order += ["air_balance.png", "top_rooms_cooling.png"]
        for name in order:
            if (charts_dir / name).exists():
                charts.append({"name": name, "caption": _caption_for(name)})

    return render_template(
        "job_charts.html",
        active_tab="charts", job_id=job_id, meta=meta, charts=charts,
    )


# ─── Routes: Past jobs ────────────────────────────────────────────────

@app.route("/jobs")
@_require_auth
def past_jobs():
    jobs = []
    for d in sorted(JOBS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        meta = {}
        try:
            meta = json.loads((d / "meta.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        jobs.append({
            "id": d.name,
            "project": meta.get("project_name", "(unknown)"),
            "address": meta.get("project_address", ""),
            "mtime": d.stat().st_mtime,
        })
    return render_template("jobs.html", active_tab="jobs", job_id=None, jobs=jobs)


@app.route("/past-jobs")
@_require_auth
def _legacy_past_jobs():
    return redirect(url_for("past_jobs"), code=301)


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
    expected_path = (f"1-Jobs/{company}/{job_no}/4-Design/dm_hvac-loads1.html"
                     if company else f"1-Jobs/?/{job_no}/4-Design/dm_hvac-loads1.html")

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


# ─── Routes: POST /upload ─────────────────────────────────────────────

@app.route("/upload", methods=["POST"])
@_require_auth
def upload():
    project_address = request.form.get("project_address", "").strip()
    engineer_state = request.form.get("engineer_state", "Florida").strip()
    engineer_name = request.form.get("engineer_name", "Adrienne Gould-Choquette").strip()
    engineer_email = request.form.get("engineer_email", "agc@adicot.com").strip()
    engineer_phone = request.form.get("engineer_phone", "(804-787-0468)").strip()
    wix_item_id = request.form.get("wix_item_id", "").strip()
    use_drive_file = request.form.get("use_drive_file", "").strip() == "1"

    f = request.files.get("html_file")
    has_upload = f is not None and f.filename
    drive_bytes: Optional[bytes] = None
    drive_filename: Optional[str] = None

    if has_upload:
        pass
    elif use_drive_file and wix_item_id:
        wix_record = wix_client.get_project(wix_item_id)
        job_no = (wix_record or {}).get("jobNo", "").strip() if wix_record else ""
        if not job_no:
            flash("Couldn't look up the Wix project's Job No.")
            return redirect(url_for("index"))
        fetched = gdrive_client.find_html(job_no)
        if fetched is None:
            flash(f"Couldn't fetch the file from Drive for {job_no}. Please upload manually.")
            return redirect(url_for("index"))
        drive_filename, drive_bytes = fetched
    else:
        flash("No file uploaded.")
        return redirect(url_for("index"))

    job_id = secrets.token_urlsafe(8)
    job_dir = JOBS_DIR / job_id
    out_dir = job_dir / "out"
    charts_dir = out_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)

    if has_upload:
        html_path = job_dir / secure_filename(f.filename)
        f.save(html_path)
    else:
        html_path = job_dir / (drive_filename or "dm_hvac-loads1.html")
        html_path.write_bytes(drive_bytes or b"")

    config = hp.ProjectConfig(
        toilet_exhaust_cfm=70.0,
        project_address=project_address,
    )
    engineer = hp.EngineerInfo(
        name=engineer_name,
        email=engineer_email,
        phone=engineer_phone,
        state_full=engineer_state,
    )

    preview_buf = io.StringIO()
    report = None
    try:
        html_text = html_path.read_text(encoding="latin-1")
        report = hp.parse_report(html_text)
        computed = hp.compute(report, config)
        with redirect_stdout(preview_buf):
            hp.print_deliverables({"computed": computed}, report, config, engineer)
    except Exception:
        tb = traceback.format_exc()
        (job_dir / "error.log").write_text(tb)
        print("=" * 60, flush=True)
        print(f"PARSE FAILURE for job {job_id}:", flush=True)
        print(tb, flush=True)
        print("=" * 60, flush=True)
        flash("Parsing the HTML failed — check the Render logs for the traceback.")
        return redirect(url_for("index"))

    if report is not None:
        try:
            (job_dir / "report.json").write_text(
                json.dumps(asdict(report), indent=2, default=str)
            )
        except Exception:
            traceback.print_exc()

    if report is not None:
        try:
            render_all_charts(report, charts_dir)
        except Exception:
            traceback.print_exc()

    wix_snapshot = None
    mismatches: list[dict] = []
    if wix_item_id:
        wix_snapshot = wix_client.get_project(wix_item_id)
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

    meta = {
        "project_name":    project_name,
        "project_address": project_address,
        "state_code":      state_code,
        "html_name":       html_path.name,
        "preview":         preview_buf.getvalue(),
        "engineer": {
            "name":  engineer_name,
            "email": engineer_email,
            "phone": engineer_phone,
            "state": engineer_state,
        },
        "config": {
            "project_address":         project_address,
            "toilet_exhaust_cfm":      "70",
            "bldg_exhaust_all_toilet": False,
        },
        "zone_overrides": {},
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
