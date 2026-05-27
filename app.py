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
                   abort, redirect, url_for, flash, Response, jsonify)
from werkzeug.utils import secure_filename

import hvac_pipeline as hp
from charts import render_all_charts
import wix_client
import validators
import gdrive_client


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
def _build_wix_dropdown_entries() -> list[dict]:
    """Get the project list from Wix and shape it for the autocomplete.

    Each entry has:
        _id              — the Wix item ID (submitted as wix_item_id when picked)
        projectAddress   — the canonical address from Wix (used to auto-fill the
                           input when a suggestion is selected)
        label            — what shows in the datalist; format depends on whether
                           the project has an address filled in yet

    Suppress entries with no usable identifier (no jobNo AND no address) since
    they'd appear as a blank line in the dropdown.
    """
    entries = []
    for p in wix_client.list_projects():
        addr = (p.get("projectAddress") or "").strip()
        job_no = (p.get("jobNo") or "").strip()
        title = (p.get("title") or "").strip()
        if not addr and not job_no:
            continue
        # Label format: "<address> — <jobNo>" if address exists, otherwise
        # "<title> — <jobNo>" so the engineer still has something to pick.
        if addr:
            label = f"{addr} — {job_no}" if job_no else addr
        else:
            display = title or "(untitled)"
            label = f"{display} — {job_no}" if job_no else display
        entries.append({
            "_id":            p.get("_id", ""),
            "projectAddress": addr,
            "label":          label,
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


# ─── Routes: Duct Sizing tab (editable in-browser, no persistence) ───
def _is_zone_loc(loc: str) -> bool:
    return loc.strip().lower().startswith("zone")


def _room_type_tag(loc: str) -> str:
    """Cheap room-type classifier used for the deficiency-exemption logic (matches the JS)."""
    low = (loc or "").lower()
    if "bath" in low:                       return "bath"
    if "rr" in low or "restroom" in low:    return "rr or corridor"
    if "toilet" in low:                     return "toilet"
    if "wic" in low:                        return "WIC"
    if "corridor" in low:                   return "Corridor"
    return ""


@app.route("/job/<job_id>/duct")
@_require_auth
def job_duct(job_id: str):
    """Editable duct-sizing table. Live recalcs in the browser.

    The Current column is preloaded with the room's Required value (the load
    calc's authoritative target), NOT the supply_air "current" column from the
    HTML — that column is usually 0 in fresh exports and isn't useful here.
    Edits are ephemeral; engineers update Design Master and re-run to commit.
    """
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

        # Preload Current = Required for rooms. Zone Current is computed by
        # JS as the sum of its room Currents (so it'll also equal Required
        # on initial render — meaning no zone shows CHECK until the engineer
        # actually changes something).
        if is_zone:
            current_raw = None    # JS fills in
        else:
            try:
                current_raw = int(required_raw) if float(required_raw).is_integer() \
                              else float(required_raw)
            except (TypeError, ValueError):
                current_raw = 0

        rows.append({
            "zone_index":   current_zone_index,
            "is_zone":      is_zone,
            "location":     loc if is_zone
                            else f"   Room {loc.replace('Room ', '', 1).strip()}",
            "required":     f"{required_raw:,.0f}",
            "required_raw": required_raw,
            "current":      f"{current_raw:,.0f}" if current_raw is not None else "",
            "current_raw":  current_raw,
            "room_type":    _room_type_tag(loc),
        })

    return render_template(
        "job_duct.html",
        active_tab="duct", job_id=job_id, meta=meta, supply_rows=rows,
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


# ── TEMPORARY: Wix client smoke test ──
# DELETE after Phase 1 checkpoint passes.
# Hit /debug/wix-projects in the browser to confirm wix_client.list_projects()
# returns real data from Wix. Show the first 20 entries + the count.
@app.route("/debug/wix-projects")
@_require_auth
def _debug_wix_projects():
    wix_client.invalidate_cache()  # always hit the live API on this debug route
    projects = wix_client.list_projects()
    return jsonify({
        "count": len(projects),
        "credentials_set": {
            "WIX_API_KEY": bool(os.environ.get("WIX_API_KEY")),
            "WIX_SITE_ID": bool(os.environ.get("WIX_SITE_ID")),
        },
        "first_20": projects[:20],
    })


# ── TEMPORARY: Google Drive client smoke test ──
# DELETE after the Drive fetch is verified.
# Usage: /debug/gdrive-fetch?job_no=TEST-Smoke%20Test
# Returns a structured diagnosis of each step of the path lookup so we can see
# WHICH step is failing without having to read server logs.
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

    # Form fields
    project_address = request.form.get("project_address", "").strip()
    toilet_exhaust  = request.form.get("toilet_exhaust_cfm", "70").strip()
    engineer_state  = request.form.get("engineer_state", "Florida").strip()
    engineer_name   = request.form.get("engineer_name",
                                       "Adrienne Gould-Choquette").strip()
    engineer_email  = request.form.get("engineer_email", "agc@adicot.com").strip()
    engineer_phone  = request.form.get("engineer_phone", "(804-787-0468)").strip()
    # Wix linkage: present only when the engineer picked a project from the
    # autocomplete (strict-pick). Empty string means "no Wix validation".
    wix_item_id     = request.form.get("wix_item_id", "").strip()

    # Build the dataclass inputs that hvac_pipeline.build_all_pdfs expects
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
    firm = hp.FirmInfo()  # uses module defaults

    preview_buf = io.StringIO()
    report = None
    computed = None
    try:
        with redirect_stdout(preview_buf):
            results = hp.build_all_pdfs(
                html_path=html_path,
                config=config,
                engineer=engineer,
                firm=firm,
                out_dir=out_dir,
            )
            computed = results.get("computed")
            # Re-parse the HTML once to also get the raw `report` for charts + duct sizing.
            # build_all_pdfs doesn't return the raw report, only the computed one.
            html_text = html_path.read_text(encoding="latin-1")
            report = hp.parse_report(html_text)

            # Console preview of all three deliverables
            hp.print_deliverables(results, report, config, engineer)
    except Exception:
        tb = traceback.format_exc()
        (job_dir / "error.log").write_text(tb)
        print("=" * 60, flush=True)
        print(f"PIPELINE FAILURE for job {job_id}:", flush=True)
        print(tb, flush=True)
        print("=" * 60, flush=True)
        flash("The pipeline failed — check the Render logs for the traceback.")
        return redirect(url_for("index"))

    # ── Persist parsed report so all tabs can read it without re-parsing ──
    if report is not None:
        try:
            (job_dir / "report.json").write_text(
                json.dumps(asdict(report), indent=2, default=str)
            )
        except Exception:
            traceback.print_exc()

    # ── Charts ──
    if report is not None:
        try:
            render_all_charts(report, charts_dir)
        except Exception:
            traceback.print_exc()

    # ── Snapshot the Wix record at job-run time, so the validator can compare ──
    # HTML against what Wix said WHEN THIS JOB RAN, not against whatever Wix
    # says now if the project gets edited later. Mismatches are also computed
    # here and frozen into meta.json — they don't get re-evaluated on page
    # reload, which means old jobs keep showing their original validation
    # results even if either side changes.
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
                # Validator crash shouldn't kill the upload. Log and surface
                # the failure as a single "validator broke" mismatch so the
                # engineer sees something on the results page.
                print(f"WARNING: validator.compare failed: {e}", flush=True)
                traceback.print_exc()
                mismatches = [{
                    "field": "(validator)",
                    "wix_value": "",
                    "html_values": [],
                    "summary": f"Validator failed: {e}",
                }]

    # ── meta.json ──
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
        "wix_item_id":     wix_item_id,        # "" if no Wix link
        "wix_snapshot":    wix_snapshot,       # None if no Wix link or fetch failed
        "mismatches":      mismatches,         # [] when no Wix link or all agree
    }
    (job_dir / "meta.json").write_text(json.dumps(meta, indent=2, default=str))

    return redirect(url_for("results", job_id=job_id))



if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    print(f"Auth: {'enabled' if APP_PASSWORD else 'DISABLED (no APP_PASSWORD)'}")
    app.run(host="0.0.0.0", port=port, debug=debug)
