"""Shared state and connective-tissue helpers for every blueprint.

Holds the things nearly every route group depends on: the basic-auth
decorator, job-path/meta helpers, the CMS-entries builder used by both the
dashboard and the daily digest, the JSON-registry load/save pairs, and the
optional-feature import flags (equipment selector, ERV/dehumid, DM setup
generator). Blueprints import what they need from here; this module must
never import from `blueprints/*` (that would be a real circular import,
unlike the deferred one this replaces, see integrations/daily_digest.py).
"""

from __future__ import annotations

import datetime
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

from flask import Response, abort, flash, redirect, request, url_for
from werkzeug.utils import secure_filename

import calendar_utils
from hvac import hvac_pipeline as hp
from pdf.charts import render_all_charts
from integrations import sheets_client

# ── Equipment selector (optional — graceful fallback if files not present) ──
try:
    from hvac import hvac_selector as eng
    HAS_EQUIP_SELECTOR = True
    _EQUIP_IMPORT_ERROR = None
except Exception as _e:
    eng = None
    HAS_EQUIP_SELECTOR = False
    _EQUIP_IMPORT_ERROR = str(_e)

# ── Combined equipment schedule glue (ERV + dehumidifier adjustments) ──
try:
    from hvac import equip_schedule
    HAS_EQUIP_SCHEDULE = True
    _EQUIP_SCHEDULE_IMPORT_ERROR = None
except Exception as _e:
    equip_schedule = None
    HAS_EQUIP_SCHEDULE = False
    _EQUIP_SCHEDULE_IMPORT_ERROR = str(_e)

# ── ERV sizing (optional — graceful fallback if module missing) ──
try:
    from erv_calculator import catalog as erv_catalog
    from erv_calculator import performance as erv_performance
    HAS_ERV = HAS_EQUIP_SCHEDULE
    _ERV_IMPORT_ERROR = None
except Exception as _e:
    erv_catalog = None
    erv_performance = None
    HAS_ERV = False
    _ERV_IMPORT_ERROR = str(_e)

# ── Dehumidifier sizing (optional — graceful fallback if pandas/module missing) ──
try:
    from hvac import dehumid_calc as dh
    HAS_DEHUMID = HAS_EQUIP_SCHEDULE
    _DEHUMID_IMPORT_ERROR = None
except Exception as _e:
    dh = None
    HAS_DEHUMID = False
    _DEHUMID_IMPORT_ERROR = str(_e)

# ── DM Setup .vbs generator (optional — graceful fallback if module missing) ──
try:
    from hvac import dm_setup_generator as dmsg
    HAS_DM_SETUP_GENERATOR = True
    _DM_SETUP_IMPORT_ERROR = None
except Exception as _e:
    dmsg = None
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

# ─── CMS backend ───────────────────────────────────────────────────────
# _cms is the single indirection point every CMS read/write goes through.
_cms = sheets_client

# ─── Client portal magic-link secret (see integrations/portal_tokens.py) ──
# Must match the PORTAL_TOKEN_SECRET Script Property set in the Apps Script
# project (AdicotProjects.gs mints tokens with the same value).
PORTAL_TOKEN_SECRET = os.environ.get("PORTAL_TOKEN_SECRET")

# ─── Internal staff email addresses (Miles/Adi/Phoebe) ───────────────
# Shared by the daily digest and any other internal notification (e.g. the
# client-signed alert in blueprints/job_lifecycle.py) so there's one place
# to update if an address changes.
PERSON_EMAILS = {"miles": "mfc@adicot.com", "adi": "agc@adicot.com", "phoebe": "pc@adicot.com"}

# ─── Calendar month names ────────────────────────────────────────────
# One list, because two places have to agree on it: the Hottest Month
# dropdown on the work order stores a name, and _dm_setup_settings() turns
# that name back into DM's 1-12 month number via MONTH_NAMES.index(). If the
# lists ever drifted the lookup would quietly yield "" and the SetCoolingMonth
# call would vanish from the generated .vbs.
MONTH_NAMES = ("January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December")


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
            return redirect(url_for("job_lifecycle.job_star", job_id=job_id))
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


# ─── Job-list stage/bucket helpers ────────────────────────────────────
# Job-list workflow stage, keyed by Wix item id. Independent of QuickBooks
# invoicing (see _invoice_registry_path below) — a project's stage and its
# invoiced-ness are separate facts and neither is cleaned up when the other
# changes. Absence of a key (or a value outside VALID_STAGES) means "unset";
# there is no stored "unset" literal.
VALID_STAGES = {"green", "yellow", "red"}
_STAGE_RANK = {"green": 0, "yellow": 1, "red": 2}

# A project with no stage ever set, whose Wix record is this old, is treated
# as "expired" — sorted last and hidden by default, same as invoiced.
EXPIRE_AFTER_DAYS = 30
_BUCKET_RANK = {"green": 0, "yellow": 1, "red": 2, "unset": 3, "expired": 4, "invoiced": 5}


def _is_stale(created_date_str) -> bool:
    """True if created_date_str (a Wix _createdDate ISO string) is EXPIRE_AFTER_DAYS
    or more in the past. Missing/unparseable dates never count as stale, so a
    project never silently expires just because we couldn't read its date."""
    if not created_date_str or not isinstance(created_date_str, str):
        return False
    try:
        created = datetime.datetime.fromisoformat(created_date_str.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=datetime.timezone.utc)
        age = datetime.datetime.now(datetime.timezone.utc) - created
    except (ValueError, TypeError, AttributeError):
        return False
    return age.days >= EXPIRE_AFTER_DAYS


def _entry_bucket(e: dict) -> str:
    """Which job-list filter bucket a project falls into. Invoiced takes
    priority over expired, which takes priority over its plain stage."""
    if e["invoiced"]:
        return "invoiced"
    if e["expired"]:
        return "expired"
    return e["stage"] or "unset"


def _stage_registry_path() -> Path:
    return JOBS_DIR / "job_stages.json"


def _load_stage_registry() -> dict:
    try:
        return json.loads(_stage_registry_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_stage_registry(reg: dict) -> None:
    path = _stage_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2))
    tmp.replace(path)


# Per-job note history, keyed by Wix item id. Append-only list of
# {"text", "created_at"} — notes are never edited or deleted once added.
def _notes_registry_path() -> Path:
    return JOBS_DIR / "job_notes.json"


def _load_notes_registry() -> dict:
    try:
        return json.loads(_notes_registry_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_notes_registry(reg: dict) -> None:
    path = _notes_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2))
    tmp.replace(path)


def _assigned_registry_path() -> Path:
    return JOBS_DIR / "job_assigned.json"


def _load_assigned_registry() -> dict:
    try:
        return json.loads(_assigned_registry_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_assigned_registry(reg: dict) -> None:
    path = _assigned_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2))
    tmp.replace(path)


# Free-standing (non-job-scoped) tasks a person jots down for tomorrow's
# digest, keyed by person key → list of {"id", "text", "created_at"}. Tasks
# persist until deleted — they're a to-do list, not an append-only log.
def _manual_tasks_registry_path() -> Path:
    return JOBS_DIR / "manual_tasks.json"


def _load_manual_tasks_registry() -> dict:
    try:
        return json.loads(_manual_tasks_registry_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_manual_tasks_registry(reg: dict) -> None:
    path = _manual_tasks_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2))
    tmp.replace(path)


# Due-date override per job, keyed by Wix item id → {"due_date": "YYYY-MM-DD"}.
# Absence of a key means the due date is the computed default (signed date +
# calendar_utils.WORK_DAYS_TO_DUE work days) — the override only exists once
# someone edits it on the Calendar tab.
def _due_date_registry_path() -> Path:
    return JOBS_DIR / "job_due_dates.json"


def _load_due_date_registry() -> dict:
    try:
        return json.loads(_due_date_registry_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_due_date_registry(reg: dict) -> None:
    path = _due_date_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2))
    tmp.replace(path)


def _calendar_events_registry_path() -> Path:
    return JOBS_DIR / "calendar_events.json"


def _load_calendar_events_registry() -> list:
    try:
        return json.loads(_calendar_events_registry_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_calendar_events_registry(events: list) -> None:
    path = _calendar_events_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(events, indent=2))
    tmp.replace(path)


# Maps Wix project id -> created-invoice record, so a project can't be invoiced
# twice and the Invoices tab can show "Invoiced ✓". Lives on the persistent disk,
# scoped per QBO environment so sandbox test invoices never block production ones.
# An entry carrying "manual": True is a hand-set flag for work billed outside
# this app — no QBO invoice exists behind it, so it has no invoice_id/url. Every
# consumer of this registry treats both kinds as invoiced; only the QuickBooks
# routes and the invoice tab look at the marker.
# Read by the dashboard (_build_cms_entries / delete_cms_project) as well as by
# the invoice tab and the quickbooks blueprint, so it lives here rather than in
# blueprints/quickbooks.py.

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


def _parse_wix_date(date_str) -> Optional[datetime.date]:
    """A Wix date field's ISO string as a plain date, or None if missing/
    unparseable (mirrors the tolerant parsing in _is_stale())."""
    if not date_str or not isinstance(date_str, str):
        return None
    try:
        return datetime.datetime.fromisoformat(
            date_str.replace("Z", "+00:00")).date()
    except (ValueError, TypeError, AttributeError):
        return None


def _build_cms_entries() -> list[dict]:
    """Projects from the Wix CMS for the landing list, each tagged with whether
    a parsed workspace already exists for it, its job-list stage, whether
    it's already been invoiced (all keyed by the Wix item id), and its
    Calendar tab signed/due dates."""
    stage_reg = _load_stage_registry()
    inv_reg = _load_invoice_registry()
    notes_reg = _load_notes_registry()
    assigned_reg = _load_assigned_registry()
    due_date_reg = _load_due_date_registry()
    entries = []
    for p in _cms.list_projects():
        _id = (p.get("_id") or "").strip()
        if not _id:
            continue
        addr = (p.get("projectAddress") or "").strip()
        job_no = (p.get("jobNo") or "").strip()
        title = (p.get("title") or "").strip()
        # Parsed only if the id survives secure_filename (its workspace dir name).
        parsed = (secure_filename(_id) == _id
                  and (JOBS_DIR / _id / "report.json").exists())
        raw_stage = stage_reg.get(_id, {}).get("stage")
        stage = raw_stage if raw_stage in VALID_STAGES else None
        # Calendar tab date of record is when the client signed the proposal,
        # not when the Wix record was created — a project can sit unsigned for
        # a while before it's actually queued up for engineering.
        signed_date = _parse_wix_date(p.get("signedDate"))
        due_override = due_date_reg.get(_id, {}).get("due_date")
        due_date = (calendar_utils.due_date_for(signed_date, due_override)
                    if signed_date else None)
        entries.append({
            "_id": _id, "job_no": job_no, "address": addr,
            "title": title, "parsed": parsed,
            "stage": stage,
            "expired": stage is None and _is_stale(p.get("createdDate")),
            "invoiced": _id in inv_reg,
            "invoiced_manual": bool(inv_reg.get(_id, {}).get("manual")),
            "notes": notes_reg.get(_id, []),
            "assigned_to": assigned_reg.get(_id, []),
            "signed_date": signed_date.isoformat() if signed_date else None,
            "due_date": due_date.isoformat() if due_date else None,
            "due_date_overridden": bool(due_override),
        })
    entries.sort(key=lambda e: (e["address"] or e["title"] or e["job_no"]).lower())
    return entries
