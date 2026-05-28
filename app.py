"""HVAC Loads Pipeline — Flask UI with multi-tab support.

Routes (URL → view function name):
    /                                index           Upload form (no job loaded)
    /upload                          upload          POST handler — parse only (no PDFs yet)
    /results/<job_id>                results         Results page (preview + PDFs section)
    /job/<job_id>/generate-pdfs      generate_pdfs   POST — runs PDF pipeline + Drive push
    /job/<job_id>/duct               job_duct        Duct Sizing tab
    /job/<job_id>/charts             job_charts      Charts tab
    /jobs                            past_jobs       Past jobs index
    /past-jobs                                       301 redirect → /jobs
    /job/<job_id>/file/<name>        download_file   File download (PDFs)
    /job/<job_id>/chart/<name>       download_chart  Inline-serve a chart PNG

Per-job storage layout:
    jobs/<job_id>/
        <original>.html
        meta.json                         (config, console preview, Wix snapshot, pdf state)
        report.json                       (full Phase 1 parsed report)
        out/
            <project>-Ventilation.pdf     (only after Generate PDFs is clicked)
            <project>-Air_Balance.pdf
            <project>-Load.pdf
            charts/
                sensible_vs_latent.png    (built at upload time)
                cooling_breakdown_<n>.png
                air_balance.png
                top_rooms_cooling.png

Flow change vs. the previous version:
- /upload no longer runs hvac_pipeline.build_all_pdfs. It only parses,
  renders charts, runs the Wix validator, and persists meta+report.
- The results page shows the preview (console output of the three deliverables)
  and a "Generate PDFs" button. PDFs aren't written to disk until the user
  clicks it. This lets engineers use the page as a quick check without paying
  the PDF-rendering cost on every run.
- generate_pdfs (POST) runs build_all_pdfs, then — if the job is Wix-linked
  and the project's Job No resolves to a 6-Submit folder on Drive — uploads
  the PDFs there. Browser download links work either way.

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
import secrets
import traceback
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from flask import (Flask, render_template, request, send_from_directory,
                   abort, redirect, url_for, flash, Response, jsonify)
from werkzeug.utils import secure_filename

import hvac_pipeline as hp
from charts import render_all_charts
import wix_client
import validators
import gdrive_client

# ─── Paths ───────────────────────────────────────────────────────────
APP_DIR = Path(__file__).resolve().parent
JOBS_DIR = Path(os.environ.get("JOBS_DIR", APP_DIR / "jobs"))
JOBS_DIR.mkdir(parents=True, exist_ok=True)

# ─── Auth ────────────────────────────────────────────────────────────
APP_USERNAME = "adicot"
APP_PASSWORD = os.environ.get("APP_PASSWORD")  # None = auth disabled (local dev)

# ─── Flask setup ─────────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024   # 5 MB max upload
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(16))


def _require_auth(view):
    """HTTP Basic Auth gate. No-op if APP_PASSWORD is unset."""
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
    """Return job_dir, 404'ing on missing or path-traversal attempts."""
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
    """Return the serialized Phase 1 report, or {} if missing."""
    try:
        return json.loads((_job_dir(job_id) / "report.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ─── Routes: upload form ─────────────────────────────────────────────

def _build_wix_dropdown_entries() -> list[dict]:
    """Get the project list from Wix and shape it for the autocomplete."""
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
    """Landing page — upload form with Wix-backed autocomplete on Address."""
    return render_template(
        "index.html",
        active_tab="upload", job_id=None,
        wix_projects=_build_wix_dropdown_entries(),
    )


# ─── Routes: Results page (preview + deferred PDFs) ──────────────────

@app.route("/results/<job_id>")
@_require_auth
def results(job_id: str):
    """Results page — shows the preview and a Generate PDFs button.

    The PDFs section reads from meta["pdfs"]: empty list until the user
    clicks Generate, then populated. The drive-push status (success or
    failure) is also in meta["drive_push"]."""
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)

    pdfs = []
    out_dir = job_dir / "out"
    if out_dir.exists():
        for p in sorted(out_dir.glob("*.pdf")):
            pdfs.append({"name": p.name, "size_kb": f"{p.stat().st_size / 1024:.0f}"})

    # Resolve the Wix project's job_no (if any) so we can tell the template
    # whether a Drive push is even possible. We don't need the full Wix
    # record again here — meta has the snapshot.
    wix_job_no = ""
    if meta.get("wix_snapshot"):
        wix_job_no = (meta["wix_snapshot"].get("jobNo") or "").strip()

    return render_template(
        "results.html",
        active_tab="pdfs",
        job_id=job_id,
        meta=meta,
        pdfs=pdfs,
        wix_job_no=wix_job_no,
        drive_push=meta.get("drive_push"),
    )


# ─── Routes: Generate PDFs (the new deferred-generation handler) ─────

@app.route("/job/<job_id>/generate-pdfs", methods=["POST"])
@_require_auth
def generate_pdfs(job_id: str):
    """Run the PDF pipeline for this job, then (if Wix-linked) push to Drive.

    Idempotent: regenerating overwrites the prior PDFs and re-pushes to
    Drive. Useful if the engineer edited the HTML / Wix record and wants
    fresh deliverables.
    """
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)
    out_dir = job_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Find the saved HTML file we wrote at /upload time
    html_name = meta.get("html_name")
    if not html_name:
        flash("Missing html_name in meta.json — can't regenerate PDFs.")
        return redirect(url_for("results", job_id=job_id))

    html_path = job_dir / html_name
    if not html_path.exists():
        flash(f"Source HTML missing on disk: {html_name}")
        return redirect(url_for("results", job_id=job_id))

    # Rebuild the dataclass inputs from the saved meta
    cfg_meta = meta.get("config", {})
    try:
        toilet_exh = float(cfg_meta.get("toilet_exhaust_cfm") or 70)
    except (TypeError, ValueError):
        toilet_exh = 70.0

    config = hp.ProjectConfig(
        toilet_exhaust_cfm=toilet_exh,
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

    # Run the PDF pipeline
    try:
        # Discard stdout — preview text was already captured at /upload time
        with redirect_stdout(io.StringIO()):
            hp.build_all_pdfs(
                html_path=html_path,
                config=config,
                engineer=engineer,
                firm=firm,
                out_dir=out_dir,
            )
    except Exception:
        tb = traceback.format_exc()
        (job_dir / "pdf_error.log").write_text(tb)
        print("=" * 60, flush=True)
        print(f"PDF GENERATION FAILURE for job {job_id}:", flush=True)
        print(tb, flush=True)
        print("=" * 60, flush=True)
        meta["pdfs_generated"] = False
        meta["drive_push"] = {
            "status": "skipped",
            "reason": "PDF generation failed",
        }
        _save_meta(job_id, meta)
        flash("PDF generation failed — check the Render logs for the traceback.")
        return redirect(url_for("results", job_id=job_id))

    meta["pdfs_generated"] = True

    # ── Drive push ───────────────────────────────────────────────────
    # Only attempt if the job was Wix-linked AND that Wix record had a Job No
    # we can parse a company from. Otherwise we silently skip — the engineer
    # uses the browser download links.

    drive_push: dict = {"status": "skipped"}
    wix_snapshot = meta.get("wix_snapshot") or {}
    wix_job_no = (wix_snapshot.get("jobNo") or "").strip()

    if not wix_job_no:
        drive_push = {
            "status": "skipped",
            "reason": "no Wix project linked (or Wix project has no Job No)",
        }
    elif gdrive_client._parse_company_from_job_no(wix_job_no) is None:
        drive_push = {
            "status": "skipped",
            "reason": f"could not parse company from Job No '{wix_job_no}'",
        }
    else:
        # Collect the freshly-generated PDFs as (name, bytes, mime) tuples
        pdf_files = []
        for p in sorted(out_dir.glob("*.pdf")):
            try:
                pdf_files.append((p.name, p.read_bytes(), "application/pdf"))
            except Exception as e:
                drive_push.setdefault("read_errors", []).append(
                    {"name": p.name, "message": str(e)}
                )

        if not pdf_files:
            drive_push = {
                "status": "error",
                "reason": "PDF pipeline ran but no .pdf files found in out/",
            }
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
                drive_push = {
                    "status": "error",
                    "reason": f"{type(e).__name__}: {e}",
                    "job_no": wix_job_no,
                }

    meta["drive_push"] = drive_push
    _save_meta(job_id, meta)

    if drive_push["status"] == "success":
        flash(f"PDFs generated and uploaded to Drive ({wix_job_no}/6-Submit).")
    elif drive_push["status"] == "skipped":
        flash("PDFs generated. (Drive upload skipped — use the download links below.)")
    elif drive_push["status"] == "partial":
        flash("PDFs generated. Some Drive uploads failed — see details below.")
    else:
        flash("PDFs generated, but the Drive upload failed. "
              "Use the browser download links and upload manually.")

    return redirect(url_for("results", job_id=job_id))


# ─── Routes: Duct Sizing tab (unchanged from prior version) ──────────

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


# ─── Routes: Charts tab (unchanged) ──────────────────────────────────

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


# ─── Routes: past jobs (unchanged) ───────────────────────────────────

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


# ─── Debug routes (unchanged) ────────────────────────────────────────

@app.route("/debug/wix-projects")
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


# ─── API: check whether a project's HTML exists in Google Drive ──────

@app.route("/api/check-drive")
@_require_auth
def api_check_drive():
    """Called by index.html JS when the engineer picks a Wix project."""
    item_id = request.args.get("wix_item_id", "").strip()
    if not item_id:
        return jsonify({"status": "no_wix_id"}), 400

    record = wix_client.get_project(item_id)
    if not record:
        return jsonify({
            "status": "wix_lookup_failed",
            "message": "Couldn't read the Wix project record.",
        })

    job_no = (record.get("jobNo") or "").strip()
    if not job_no:
        return jsonify({
            "status": "no_job_no",
            "message": "This Wix project has no Job No.",
        })

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
                "job_folder_found", "design_folder_found",
                "html_file_found"):
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


# ─── Routes: file downloads (unchanged) ──────────────────────────────

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


# ─── Routes: POST /upload — parse, render preview, render charts. ────
#     NO PDFs at this stage. The user clicks "Generate PDFs" on the
#     results page to actually render them.

@app.route("/upload", methods=["POST"])
@_require_auth
def upload():
    """Parse HTML, render charts, save report+meta. Does NOT generate PDFs.

    Source of the HTML is either uploaded file or fetched from Drive (same
    logic as the previous version of this route)."""

    project_address = request.form.get("project_address", "").strip()
    toilet_exhaust = request.form.get("toilet_exhaust_cfm", "70").strip()
    engineer_state = request.form.get("engineer_state", "Florida").strip()
    engineer_name = request.form.get("engineer_name",
                                     "Adrienne Gould-Choquette").strip()
    engineer_email = request.form.get("engineer_email", "agc@adicot.com").strip()
    engineer_phone = request.form.get("engineer_phone", "(804-787-0468)").strip()
    wix_item_id = request.form.get("wix_item_id", "").strip()
    use_drive_file = request.form.get("use_drive_file", "").strip() == "1"

    # Decide which source provides the HTML bytes
    f = request.files.get("html_file")
    has_upload = f is not None and f.filename
    drive_bytes: Optional[bytes] = None
    drive_filename: Optional[str] = None

    if has_upload:
        pass  # manual upload wins
    elif use_drive_file and wix_item_id:
        wix_record = wix_client.get_project(wix_item_id)
        job_no = (wix_record or {}).get("jobNo", "").strip() if wix_record else ""
        if not job_no:
            flash("Couldn't look up the Wix project's Job No.")
            return redirect(url_for("index"))
        fetched = gdrive_client.find_html(job_no)
        if fetched is None:
            flash(f"Couldn't fetch the file from Drive for {job_no}. "
                  "Please upload manually.")
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

    # Save the HTML to disk
    if has_upload:
        html_path = job_dir / secure_filename(f.filename)
        f.save(html_path)
    else:
        html_path = job_dir / (drive_filename or "dm_hvac-loads1.html")
        html_path.write_bytes(drive_bytes or b"")

    # Build the dataclass inputs the pipeline will need later (saved
    # into meta so /generate-pdfs can rebuild them).
    config = hp.ProjectConfig(
        toilet_exhaust_cfm=float(toilet_exhaust or 70),
        project_address=project_address,
    )
    engineer = hp.EngineerInfo(
        name=engineer_name,
        email=engineer_email,
        phone=engineer_phone,
        state_full=engineer_state,
    )

    # ── Parse + compute + preview. NO PDF rendering. ──
    #
    # hvac_pipeline.compute() runs the calc engine and returns a
    # ComputedReport. print_deliverables() takes a results dict with
    # {"computed": ComputedReport} and writes the same console preview
    # build_all_pdfs would have produced. We skip build_all_pdfs entirely
    # so no PDFs are written to disk at upload time.
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

    # ── Persist parsed report so all tabs can read it ────────────────
    if report is not None:
        try:
            (job_dir / "report.json").write_text(
                json.dumps(asdict(report), indent=2, default=str)
            )
        except Exception:
            traceback.print_exc()

    # ── Charts (cheap, useful for the preview tabs) ──────────────────
    if report is not None:
        try:
            render_all_charts(report, charts_dir)
        except Exception:
            traceback.print_exc()

    # ── Wix snapshot + validation ────────────────────────────────────
    wix_snapshot = None
    mismatches: list[dict] = []
    if wix_item_id:
        wix_snapshot = wix_client.get_project(wix_item_id)
        if wix_snapshot is None:
            print(f"WARNING: wix_item_id={wix_item_id} but get_project returned None",
                  flush=True)
        elif report is not None:
            try:
                mismatches = validators.compare(report, wix_snapshot)
            except Exception as e:
                print(f"WARNING: validator.compare failed: {e}", flush=True)
                traceback.print_exc()
                mismatches = [{
                    "field": "(validator)",
                    "wix_value": "",
                    "html_values": [],
                    "summary": f"Validator failed: {e}",
                }]

    # ── meta.json ────────────────────────────────────────────────────
    project_name = report.project.project_name if report is not None else "(unknown)"
    meta = {
        "project_name":    project_name,
        "project_address": project_address,
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
            "toilet_exhaust_cfm":      toilet_exhaust,
            "bldg_exhaust_all_toilet": False,
        },
        "zone_overrides": {},
        "wix_item_id":  wix_item_id,
        "wix_snapshot": wix_snapshot,
        "mismatches":   mismatches,
        # NEW fields tracking deferred PDF generation
        "pdfs_generated": False,
        "drive_push":     None,
    }
    _save_meta(job_id, meta)

    return redirect(url_for("results", job_id=job_id))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    print(f"Auth: {'enabled' if APP_PASSWORD else 'DISABLED (no APP_PASSWORD)'}")
    app.run(host="0.0.0.0", port=port, debug=debug)
