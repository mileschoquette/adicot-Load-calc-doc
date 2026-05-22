"""HVAC Loads PDF Pipeline — Flask UI.

Local dev:  python app.py        → http://localhost:5000
Production: gunicorn app:app     (see Procfile)

Environment variables (production):
  APP_PASSWORD   — required. Sets the shared password for HTTP basic auth.
                   Username is always "adicot". If unset, auth is disabled
                   (fine for local dev, NOT fine if the URL is public).
  SECRET_KEY     — Flask session key. Auto-generated if unset, but persistent
                   sessions need a stable value across restarts.
  JOBS_DIR       — Where to store per-job workspaces. Defaults to ./jobs.
                   On Render, point at a persistent disk mount like /var/data/jobs.
  PORT           — Listen port (set automatically by Render/Heroku-style hosts).

The engineer uploads a Design Master HTML export, fills in the project address
and any zone overrides, and gets the three deliverable PDFs plus a console-style
preview of all the numbers for sanity check.
"""
from __future__ import annotations

import functools
import io
import json
import os
import secrets
import shutil
import traceback
from contextlib import redirect_stdout
from pathlib import Path

from flask import (Flask, render_template, request, send_from_directory,
                   abort, redirect, url_for, flash, Response)
from werkzeug.utils import secure_filename

import hvac_pipeline as hp

# === Paths ===
APP_DIR = Path(__file__).resolve().parent
JOBS_DIR = Path(os.environ.get("JOBS_DIR", APP_DIR / "jobs"))
JOBS_DIR.mkdir(parents=True, exist_ok=True)

# === Auth ===
APP_USERNAME = "adicot"
APP_PASSWORD = os.environ.get("APP_PASSWORD")          # None = auth disabled

# === Flask setup ===
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024     # 5 MB per upload
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(16))


def _require_auth(view):
    """Decorator: HTTP basic auth gate around a view function.

    No-op if APP_PASSWORD is not set in the environment (local dev convenience).
    Use `secrets.compare_digest` to avoid timing-leak comparisons.
    """
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not APP_PASSWORD:
            return view(*args, **kwargs)
        auth = request.authorization
        if (auth and auth.username and auth.password and
                secrets.compare_digest(auth.username, APP_USERNAME) and
                secrets.compare_digest(auth.password, APP_PASSWORD)):
            return view(*args, **kwargs)
        return Response(
            "Authentication required.", 401,
            {"WWW-Authenticate": 'Basic realm="Adicot HVAC Pipeline"'},
        )
    return wrapper


def _new_job_id() -> str:
    """Short, URL-safe job ID."""
    return secrets.token_urlsafe(8)


def _job_dir(job_id: str) -> Path:
    d = JOBS_DIR / job_id
    if not d.exists():
        abort(404)
    return d


def _parse_zone_overrides_form(form) -> dict:
    """Build a zone_overrides dict from the override-form rows.

    Form fields look like:
      ov_match_0, ov_display_0, ov_tons_0, ov_supply_0, ov_merge_0
      ov_match_1, ov_display_1, ...

    Empty or whitespace-only `ov_match_N` rows are ignored.
    """
    overrides: dict[str, dict] = {}
    i = 0
    while f"ov_match_{i}" in form:
        match = (form.get(f"ov_match_{i}") or "").strip()
        if not match:
            i += 1
            continue
        ov: dict = {}
        disp = (form.get(f"ov_display_{i}") or "").strip()
        if disp:
            ov["display_name"] = disp
        tons = (form.get(f"ov_tons_{i}") or "").strip()
        if tons:
            try:
                ov["tons"] = float(tons)
            except ValueError:
                pass
        supply = (form.get(f"ov_supply_{i}") or "").strip()
        if supply:
            try:
                ov["supply_cfm"] = float(supply)
            except ValueError:
                pass
        merge = (form.get(f"ov_merge_{i}") or "").strip()
        if merge:
            ov["merge_with"] = merge
        overrides[match] = ov
        i += 1
    return overrides


@app.route("/", methods=["GET"])
@_require_auth
def index():
    """Upload form."""
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
@_require_auth
def upload():
    """Receive HTML + form fields, run pipeline, redirect to results page."""
    file = request.files.get("html_file")
    if not file or not file.filename:
        flash("Please upload a Design Master HTML file.", "error")
        return redirect(url_for("index"))

    if not file.filename.lower().endswith((".html", ".htm")):
        flash("File must be a .html or .htm export from Design Master.", "error")
        return redirect(url_for("index"))

    # Create job workspace
    job_id = _new_job_id()
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir()
    (job_dir / "out").mkdir()

    # Save the upload
    safe_name = secure_filename(file.filename) or "input.html"
    html_path = job_dir / safe_name
    file.save(html_path)

    # Build config from form
    config = hp.ProjectConfig()
    config.project_address = (request.form.get("project_address") or "").strip()
    config.bldg_exhaust_all_toilet = bool(request.form.get("bldg_exhaust_all_toilet"))
    config.toilet_exhaust_cfm = float(request.form.get("toilet_exhaust_cfm") or 70)

    # Engineer info — defaults from EngineerInfo, but allow override via form
    engineer = hp.EngineerInfo(
        name=(request.form.get("engineer_name") or hp.EngineerInfo().name).strip(),
        email=(request.form.get("engineer_email") or hp.EngineerInfo().email).strip(),
        phone=(request.form.get("engineer_phone") or hp.EngineerInfo().phone).strip(),
        state_full=(request.form.get("engineer_state") or hp.EngineerInfo().state_full).strip(),
    )

    zone_overrides = _parse_zone_overrides_form(request.form)

    # Run the pipeline
    try:
        results = hp.build_all_pdfs(
            html_path, config, engineer, hp.FirmInfo(),
            out_dir=job_dir / "out",
            zone_overrides=zone_overrides,
        )
    except Exception as exc:
        tb = traceback.format_exc()
        (job_dir / "error.log").write_text(tb)
        flash(f"Pipeline failed: {exc}", "error")
        return redirect(url_for("index"))

    # Capture the console-preview output
    buf = io.StringIO()
    with redirect_stdout(buf):
        hp.print_deliverables(results, hp.parse_report(html_path.read_text(encoding="latin-1")),
                              config, engineer)
    preview_text = buf.getvalue()

    # Persist metadata for the results page
    meta = {
        "project_name": results["computed"].project_name,
        "html_name": safe_name,
        "files": {k: str(v.relative_to(job_dir)) for k, v in results.items()
                  if k != "computed"},
        "preview": preview_text,
        "config": {
            "project_address": config.project_address,
            "bldg_exhaust_all_toilet": config.bldg_exhaust_all_toilet,
            "toilet_exhaust_cfm": config.toilet_exhaust_cfm,
        },
        "engineer": {
            "name": engineer.name, "email": engineer.email,
            "phone": engineer.phone, "state": engineer.state_full,
        },
        "zone_overrides": zone_overrides,
    }
    (job_dir / "meta.json").write_text(json.dumps(meta, indent=2, default=str))

    return redirect(url_for("results", job_id=job_id))


@app.route("/results/<job_id>", methods=["GET"])
@_require_auth
def results(job_id: str):
    """Show preview + download links."""
    job_dir = _job_dir(job_id)
    meta_path = job_dir / "meta.json"
    if not meta_path.exists():
        abort(404)
    meta = json.loads(meta_path.read_text())
    return render_template("results.html", job_id=job_id, meta=meta)


@app.route("/download/<job_id>/<path:filename>", methods=["GET"])
@_require_auth
def download(job_id: str, filename: str):
    """Serve a deliverable PDF."""
    job_dir = _job_dir(job_id)
    return send_from_directory(job_dir, filename, as_attachment=True)


@app.route("/jobs", methods=["GET"])
@_require_auth
def list_jobs():
    """List of past jobs (most recent first)."""
    entries = []
    for d in sorted(JOBS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        entries.append({
            "job_id": d.name,
            "project_name": meta.get("project_name", "(unknown)"),
            "address": meta.get("config", {}).get("project_address", ""),
            "mtime": d.stat().st_mtime,
        })
    return render_template("jobs.html", entries=entries)


@app.route("/delete/<job_id>", methods=["POST"])
@_require_auth
def delete_job(job_id: str):
    """Delete a job workspace."""
    job_dir = _job_dir(job_id)
    shutil.rmtree(job_dir)
    flash("Job deleted.", "info")
    return redirect(url_for("list_jobs"))


@app.errorhandler(413)
def too_large(_):
    flash("File too large (5 MB max).", "error")
    return redirect(url_for("index"))


if __name__ == "__main__":
    # Local dev entry point. In production, gunicorn imports `app` directly
    # and this block is skipped.
    port = int(os.environ.get("PORT", 5000))
    print("HVAC Loads PDF Pipeline")
    print(f"Workspace: {JOBS_DIR}")
    print(f"Auth:      {'enabled (username adicot)' if APP_PASSWORD else 'DISABLED (set APP_PASSWORD to enable)'}")
    print(f"Open http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
