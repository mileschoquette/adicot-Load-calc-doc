"""HVAC Loads Pipeline — Flask UI with multi-tab support.

Routes (URL → view function name):
  /                          index            Upload form (no job loaded)
  /upload                    upload           POST handler — parse, render all deliverables
  /results/<job_id>          results          PDFs tab (the original results page)
  /job/<job_id>/duct         job_duct         NEW — Duct Sizing tab
  /job/<job_id>/charts       job_charts       NEW — Charts tab
  /jobs                      past_jobs        Past jobs index
  /past-jobs                                  301 redirect → /jobs (kept for old bookmarks)
  /job/<job_id>/file/<name>  download_file    File download (PDFs + xlsx)
  /job/<job_id>/chart/<name> download_chart   NEW — inline-serve a chart PNG

Per-job storage layout (additions marked NEW):

  jobs/<job_id>/
    <original>.html
    meta.json                       # config + console preview (existing)
    report.json              NEW    # full Phase 1 parsed report (asdict)
    out/
      <project>-Ventilation.pdf
      <project>-Air_Balance.pdf
      <project>-Load.pdf
      <project>-duct-sizing.xlsx    NEW
      charts/                       NEW
        sensible_vs_latent.png
        cooling_breakdown_<n>.png
        air_balance.png
        top_rooms_cooling.png

Environment variables (unchanged from previous version):
  APP_PASSWORD  — shared password (username always "adicot"); unset = no auth
  SECRET_KEY    — Flask session key; auto-generated if unset
  JOBS_DIR      — where per-job workspaces live; default ./jobs
  PORT          — set by Render/host
"""
from __future__ import annotations

import functools
import io
import json
import os
import secrets
import traceback
from contextlib import redirect_stdout
from dataclasses import asdict, is_dataclass
from pathlib import Path

from flask import (Flask, render_template, request, send_from_directory,
                   abort, redirect, url_for, flash, Response)
from werkzeug.utils import secure_filename
from openpyxl import Workbook

import hvac_pipeline as hp
from charts import render_all_charts
from duct_sizing import write_duct_sizing


# ─── Paths ───
APP_DIR = Path(__file__).resolve().parent
JOBS_DIR = Path(os.environ.get("JOBS_DIR", APP_DIR / "jobs"))
JOBS_DIR.mkdir(parents=True, exist_ok=True)

# ─── Auth ───
APP_USERNAME = "adicot"
APP_PASSWORD = os.environ.get("APP_PASSWORD")  # None = auth disabled (local dev)

# ─── Flask setup ───
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB max upload
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


# ─── Job helpers ───
def _job_dir(job_id: str) -> Path:
    """Return job_dir, 404'ing on missing or path-traversal attempts."""
    safe_id = secure_filename(job_id)
    if not safe_id or safe_id != job_id:
        abort(404)
    d = (JOBS_DIR / safe_id).resolve()
    if not d.exists() or not d.is_dir():
        abort(404)
    # Make sure the resolved path actually sits under JOBS_DIR
    if JOBS_DIR.resolve() not in d.parents:
        abort(404)
    return d


def _load_meta(job_id: str) -> dict:
    try:
        return json.loads((_job_dir(job_id) / "meta.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _load_report(job_id: str) -> dict:
    """Return the serialized Phase 1 report, or {} if missing.

    Older jobs (uploaded before this update) won't have report.json — those
    will degrade gracefully to empty preview tables on the new tabs.
    """
    try:
        return json.loads((_job_dir(job_id) / "report.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ─── Routes: upload form ───
@app.route("/")
@_require_auth
def index():
    """Landing page — upload form. No job loaded."""
    return render_template("index.html", active_tab="upload", job_id=None)


# ─── Routes: PDFs tab (existing /results/<job_id>) ───
@app.route("/results/<job_id>")
@_require_auth
def results(job_id: str):
    """Tab 1 — list the three PDF deliverables for this job."""
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)
    out_dir = job_dir / "out"

    pdfs = []
    if out_dir.exists():
        for p in sorted(out_dir.glob("*.pdf")):
            pdfs.append({"name": p.name, "size_kb": f"{p.stat().st_size / 1024:.0f}"})

    return render_template(
        "results.html",
        active_tab="pdfs", job_id=job_id, meta=meta, pdfs=pdfs,
    )


# ─── Routes: NEW — Duct Sizing tab ───
@app.route("/job/<job_id>/duct")
@_require_auth
def job_duct(job_id: str):
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)
    report = _load_report(job_id)

    out_dir = job_dir / "out"
    duct_files = list(out_dir.glob("*-duct-sizing.xlsx"))
    duct_xlsx_name = duct_files[0].name if duct_files else None

    # Build the preview rows. Mark each as zone vs room here in Python
    # rather than in the template so the template only handles layout.
    rows = []
    for sa in report.get("supply_air", []):
        loc = (sa.get("location") or "").strip()
        is_zone = loc.lower().startswith("zone")
        low = loc.lower()
        if "bath" in low:                      rt = "bath"
        elif "rr" in low or "restroom" in low: rt = "rr or corridor"
        elif "toilet" in low:                  rt = "toilet"
        elif "wic" in low:                     rt = "WIC"
        elif "corridor" in low:                rt = "Corridor"
        else:                                  rt = ""
        rows.append({
            "location": loc if is_zone
                        else f"   Room {loc.replace('Room ', '', 1).strip()}",
            "required": f"{sa.get('required_supply_cfm') or 0:,.0f}",
            "current":  f"{sa.get('current_supply_cfm')  or 0:,.0f}",
            "room_type": rt,
            "is_zone": is_zone,
        })

    return render_template(
        "job_duct.html",
        active_tab="duct", job_id=job_id, meta=meta,
        duct_xlsx_name=duct_xlsx_name, supply_rows=rows,
    )


# ─── Routes: NEW — Charts tab ───
_CHART_CAPTIONS = {
    "sensible_vs_latent.png": "Cooling Load — Sensible vs Latent by Zone",
    "air_balance.png":        "Air Balance — Supply vs Outside Air by Zone",
    "top_rooms_cooling.png":  "Top Rooms by Cooling Load",
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


# ─── Routes: past jobs ───
@app.route("/jobs")
@_require_auth
def past_jobs():
    """Tab 4 — list every job, newest first."""
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
            "mtime":   d.stat().st_mtime,
        })

    return render_template("jobs.html", active_tab="jobs", job_id=None, jobs=jobs)


@app.route("/past-jobs")
@_require_auth
def _legacy_past_jobs():
    """Old route — 301 to the new one so existing bookmarks still work."""
    return redirect(url_for("past_jobs"), code=301)


# ─── Routes: file downloads ───
@app.route("/job/<job_id>/file/<path:filename>")
@_require_auth
def download_file(job_id: str, filename: str):
    """Serve a file from this job's out/ directory."""
    job_dir = _job_dir(job_id)
    return send_from_directory(job_dir / "out", filename, as_attachment=True)


@app.route("/job/<job_id>/chart/<path:filename>")
@_require_auth
def download_chart(job_id: str, filename: str):
    """Inline-serve a chart PNG so the <img> tag can render it directly."""
    job_dir = _job_dir(job_id)
    return send_from_directory(job_dir / "out" / "charts", filename)


# ─── Routes: POST /upload — the workhorse ───
@app.route("/upload", methods=["POST"])
@_require_auth
def upload():
    """Parse HTML, render all deliverables (PDFs + xlsx + charts), redirect to PDFs tab."""
    f = request.files.get("html_file")
    if not f or not f.filename:
        flash("No file uploaded.")
        return redirect(url_for("index"))

    job_id = secrets.token_urlsafe(8)
    job_dir = JOBS_DIR / job_id
    out_dir = job_dir / "out"
    charts_dir = out_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)

    html_path = job_dir / secure_filename(f.filename)
    f.save(html_path)

    # Form fields — pass through to hvac_pipeline.build_all
    project_address = request.form.get("project_address", "").strip()
    toilet_exhaust  = request.form.get("toilet_exhaust_cfm", "0").strip()
    engineer = {
        "name":  request.form.get("engineer_name", "").strip(),
        "email": request.form.get("engineer_email", "").strip(),
        "phone": request.form.get("engineer_phone", "").strip(),
        "state": request.form.get("engineer_state", "Florida").strip(),
    }

    preview_buf = io.StringIO()
    try:
        with redirect_stdout(preview_buf):
            # build_all must return (report, pdf_paths). If your version returns
            # just paths, edit hvac_pipeline.py to also return the report object.
            result = hp.build_all(
                html_path=html_path,
                output_dir=out_dir,
                project_address=project_address,
                toilet_exhaust_cfm=float(toilet_exhaust or 0),
                engineer=engineer,
            )
            # Accept either (report, paths) or just paths from older versions.
            # If it's just paths, the new tabs degrade — we just skip the
            # duct-xlsx and chart generation and tell the user.
            if isinstance(result, tuple) and len(result) == 2:
                report, _pdf_paths = result
            else:
                report = None
    except Exception:
        (job_dir / "error.log").write_text(traceback.format_exc())
        flash("The pipeline failed — check error.log in the job directory.")
        return redirect(url_for("index"))

    # ── NEW: persist parsed report so all tabs can read it without re-parsing ──
    if report is not None:
        try:
            (job_dir / "report.json").write_text(
                json.dumps(asdict(report), indent=2, default=str)
            )
        except Exception:
            traceback.print_exc()

    # ── NEW: duct sizing xlsx ──
    if report is not None:
        try:
            duct_wb = Workbook()
            del duct_wb[duct_wb.sheetnames[0]]
            write_duct_sizing(duct_wb, report.supply_air)
            project_slug = (
                (report.project.project_name or "project").split()[0].replace("/", "_")
            )
            duct_wb.save(out_dir / f"{project_slug}-duct-sizing.xlsx")
        except Exception:
            traceback.print_exc()

    # ── NEW: charts ──
    if report is not None:
        try:
            render_all_charts(report, charts_dir)
        except Exception:
            traceback.print_exc()

    # ── meta.json (last, so preview captures everything that happened) ──
    project_name = (
        report.project.project_name if report is not None else "(unknown)"
    )
    meta = {
        "project_name":    project_name,
        "project_address": project_address,
        "toilet_exhaust":  toilet_exhaust,
        "engineer":        engineer,
        "preview":         preview_buf.getvalue(),
    }
    (job_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    return redirect(url_for("results", job_id=job_id))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    print(f"Auth: {'enabled' if APP_PASSWORD else 'DISABLED (no APP_PASSWORD)'}")
    app.run(host="0.0.0.0", port=port, debug=debug)
