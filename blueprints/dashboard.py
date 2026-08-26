"""Landing list, Calendar tab, and the job-list stage/notes/assigned/manual-
tasks CRUD routes that back it. Also owns the temp-jobs index and both
"delete a job" flavors (local workspace vs. the Wix/CMS record)."""

from __future__ import annotations

import calendar as pycalendar
import datetime
import json
import secrets
import shutil

from flask import Blueprint, flash, redirect, render_template, request, url_for, jsonify

from core import (
    JOBS_DIR, _require_auth, _safe_job_path, _job_dir,
    _load_stage_registry, _save_stage_registry,
    _load_notes_registry, _save_notes_registry,
    _load_assigned_registry, _save_assigned_registry,
    _load_manual_tasks_registry, _save_manual_tasks_registry,
    _load_due_date_registry, _save_due_date_registry,
    _load_calendar_events_registry, _save_calendar_events_registry,
    _load_invoice_registry, _save_invoice_registry,
    VALID_STAGES, _BUCKET_RANK, _build_cms_entries, _entry_bucket,
    _cms,
)

dashboard = Blueprint("dashboard", __name__)

# Who's on the hook for a job, keyed by Wix item id → list of person keys.
# Any subset of the three is allowed; absence of a key means unassigned.
VALID_ASSIGNEES = {"miles", "phoebe", "adi"}

# Manually-added Calendar tab events, not tied to any job — a flat list of
# {"id", "title", "date", "notes", "assigned_to", "created_at"}.
# assigned_to is "everyone" or one of VALID_ASSIGNEES.
VALID_EVENT_ASSIGNEES = VALID_ASSIGNEES | {"everyone"}


@dashboard.route("/")
@_require_auth
def index():
    """Landing page — the list of CMS (Wix) projects, plus Run a Temp Job.
    Grouped by job-list bucket (green, yellow, red, unset, expired, invoiced),
    alphabetical within each group; invoices.html keeps _build_cms_entries()'s
    plain alphabetical order since this re-sort happens only here."""
    entries = _build_cms_entries()
    for e in entries:
        e["bucket"] = _entry_bucket(e)
    entries.sort(key=lambda e: (
        _BUCKET_RANK.get(e["bucket"], 99),
        (e["address"] or e["title"] or e["job_no"]).lower(),
    ))
    manual_tasks = _load_manual_tasks_registry()
    return render_template("dashboard.html", projects=entries, manual_tasks=manual_tasks)


@dashboard.route("/job/<wix_id>/stage", methods=["POST"])
@_require_auth
def set_job_stage(wix_id: str):
    """Set (or clear, via stage=unset) a project's job-list stage."""
    if not wix_id:
        return jsonify({"ok": False, "error": "Missing project id."}), 400
    stage = request.form.get("stage", "").strip().lower()
    if stage != "unset" and stage not in VALID_STAGES:
        return jsonify({"ok": False, "error": "Invalid stage."}), 400
    reg = _load_stage_registry()
    if stage == "unset":
        reg.pop(wix_id, None)
    else:
        reg[wix_id] = {"stage": stage,
                        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    _save_stage_registry(reg)
    return jsonify({"ok": True, "stage": None if stage == "unset" else stage})


@dashboard.route("/job/<wix_id>/notes", methods=["POST"])
@_require_auth
def add_job_note(wix_id: str):
    """Append a note to a project's history. Notes are never edited or deleted
    once added — the list is a running log, not a single mutable field."""
    if not wix_id:
        return jsonify({"ok": False, "error": "Missing project id."}), 400
    text = request.form.get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Note can't be empty."}), 400
    reg = _load_notes_registry()
    note = {"text": text, "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    reg.setdefault(wix_id, []).append(note)
    _save_notes_registry(reg)
    return jsonify({"ok": True, "note": note})


@dashboard.route("/job/<wix_id>/assigned", methods=["POST"])
@_require_auth
def set_job_assigned(wix_id: str):
    """Set (or clear) which of Miles/Phoebe/Adi are assigned to a project."""
    if not wix_id:
        return jsonify({"ok": False, "error": "Missing project id."}), 400
    assigned = [a for a in request.form.getlist("assigned") if a in VALID_ASSIGNEES]
    reg = _load_assigned_registry()
    if assigned:
        reg[wix_id] = assigned
    else:
        reg.pop(wix_id, None)
    _save_assigned_registry(reg)
    return jsonify({"ok": True, "assigned": assigned})


@dashboard.route("/manual-tasks/<person>", methods=["POST"])
@_require_auth
def add_manual_task(person: str):
    """Add a free-standing task (not tied to any job) for Miles/Phoebe/Adi,
    surfaced in that person's next daily digest until deleted."""
    if person not in VALID_ASSIGNEES:
        return jsonify({"ok": False, "error": "Invalid person."}), 400
    text = request.form.get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Task can't be empty."}), 400
    reg = _load_manual_tasks_registry()
    task = {"id": secrets.token_hex(6), "text": text,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    reg.setdefault(person, []).append(task)
    _save_manual_tasks_registry(reg)
    return jsonify({"ok": True, "task": task})


@dashboard.route("/manual-tasks/<person>/<task_id>/delete", methods=["POST"])
@_require_auth
def delete_manual_task(person: str, task_id: str):
    """Remove a manual task once it's done or no longer needed."""
    if person not in VALID_ASSIGNEES:
        return jsonify({"ok": False, "error": "Invalid person."}), 400
    reg = _load_manual_tasks_registry()
    tasks = reg.get(person, [])
    remaining = [t for t in tasks if t.get("id") != task_id]
    if remaining:
        reg[person] = remaining
    else:
        reg.pop(person, None)
    _save_manual_tasks_registry(reg)
    return jsonify({"ok": True})


@dashboard.route("/calendar")
@_require_auth
def calendar_page():
    """Site-wide Calendar tab — every CMS job's signed/due date plus
    manually-added events, for a single month at a time (?month=YYYY-MM,
    default this month). Shows every project regardless of stage/invoiced/
    expired status, so past jobs stay visible on the calendar."""
    today = datetime.date.today()
    month_param = request.args.get("month", "").strip()
    try:
        year, month = (int(x) for x in month_param.split("-", 1))
        month_start = datetime.date(year, month, 1)
    except (ValueError, TypeError):
        month_start = today.replace(day=1)

    entries = _build_cms_entries()
    events = _load_calendar_events_registry()

    signed_by_date, due_by_date = {}, {}
    for e in entries:
        if e["signed_date"]:
            signed_by_date.setdefault(e["signed_date"], []).append(e)
        if e["due_date"]:
            due_by_date.setdefault(e["due_date"], []).append(e)
    events_by_date = {}
    for ev in events:
        events_by_date.setdefault(ev["date"], []).append(ev)

    # Sunday-first weeks spanning the month, including leading/trailing days
    # from adjacent months so the grid is always a whole number of weeks.
    weeks = pycalendar.Calendar(firstweekday=6).monthdatescalendar(
        month_start.year, month_start.month)

    next_month = (month_start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    prev_month = (month_start - datetime.timedelta(days=1)).replace(day=1)

    return render_template(
        "calendar.html",
        month_start=month_start,
        prev_month=prev_month.strftime("%Y-%m"),
        next_month=next_month.strftime("%Y-%m"),
        today=today.isoformat(),
        weeks=weeks,
        signed_by_date=signed_by_date,
        due_by_date=due_by_date,
        events_by_date=events_by_date,
        valid_event_assignees=sorted(VALID_EVENT_ASSIGNEES),
    )


@dashboard.route("/calendar/jobs/<wix_id>/due-date", methods=["POST"])
@_require_auth
def set_job_due_date(wix_id: str):
    """Override a job's computed due date, or clear the override (empty
    due_date) to fall back to signed date + 15 work days."""
    if not wix_id:
        return jsonify({"ok": False, "error": "Missing project id."}), 400
    due_date = request.form.get("due_date", "").strip()
    if due_date:
        try:
            datetime.date.fromisoformat(due_date)
        except ValueError:
            return jsonify({"ok": False, "error": "Invalid date."}), 400
    reg = _load_due_date_registry()
    if due_date:
        reg[wix_id] = {"due_date": due_date}
    else:
        reg.pop(wix_id, None)
    _save_due_date_registry(reg)
    return jsonify({"ok": True, "due_date": due_date or None})


@dashboard.route("/calendar/events", methods=["POST"])
@_require_auth
def add_calendar_event():
    """Add a manual Calendar tab event (not tied to any job)."""
    title = request.form.get("title", "").strip()
    date_str = request.form.get("date", "").strip()
    notes = request.form.get("notes", "").strip()
    assigned_to = request.form.get("assigned_to", "everyone").strip()
    if not title:
        return jsonify({"ok": False, "error": "Title can't be empty."}), 400
    try:
        datetime.date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid date."}), 400
    if assigned_to not in VALID_EVENT_ASSIGNEES:
        return jsonify({"ok": False, "error": "Invalid assignee."}), 400
    events = _load_calendar_events_registry()
    event = {"id": secrets.token_hex(6), "title": title, "date": date_str,
              "notes": notes, "assigned_to": assigned_to,
              "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    events.append(event)
    _save_calendar_events_registry(events)
    return jsonify({"ok": True, "event": event})


@dashboard.route("/calendar/events/<event_id>", methods=["POST"])
@_require_auth
def update_calendar_event(event_id: str):
    """Edit an existing manual Calendar tab event."""
    title = request.form.get("title", "").strip()
    date_str = request.form.get("date", "").strip()
    notes = request.form.get("notes", "").strip()
    assigned_to = request.form.get("assigned_to", "everyone").strip()
    if not title:
        return jsonify({"ok": False, "error": "Title can't be empty."}), 400
    try:
        datetime.date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid date."}), 400
    if assigned_to not in VALID_EVENT_ASSIGNEES:
        return jsonify({"ok": False, "error": "Invalid assignee."}), 400
    events = _load_calendar_events_registry()
    for ev in events:
        if ev.get("id") == event_id:
            ev.update(title=title, date=date_str, notes=notes, assigned_to=assigned_to)
            _save_calendar_events_registry(events)
            return jsonify({"ok": True, "event": ev})
    return jsonify({"ok": False, "error": "Event not found."}), 404


@dashboard.route("/calendar/events/<event_id>/delete", methods=["POST"])
@_require_auth
def delete_calendar_event(event_id: str):
    """Remove a manual Calendar tab event."""
    events = _load_calendar_events_registry()
    remaining = [e for e in events if e.get("id") != event_id]
    if len(remaining) == len(events):
        return jsonify({"ok": False, "error": "Event not found."}), 404
    _save_calendar_events_registry(remaining)
    return jsonify({"ok": True})


@dashboard.route("/job/<wix_id>/delete-cms", methods=["POST"])
@_require_auth
def delete_cms_project(wix_id: str):
    """Delete a project from Wix (source of truth for the landing list) first;
    only clean up local artifacts if that succeeds, so a failed Wix delete
    doesn't silently orphan local data for a project that will still show up
    next page load."""
    if not wix_id:
        return jsonify({"ok": False, "error": "Missing project id."}), 400
    if not _cms.delete_project(wix_id):
        return jsonify({"ok": False, "error": "Could not delete from Wix. If this "
                         "is the first attempt, check that the Wix API key has "
                         "\"Manage\" permission for Data Items."}), 502
    _cms.invalidate_cache()
    shutil.rmtree(_safe_job_path(wix_id), ignore_errors=True)
    stage_reg = _load_stage_registry()
    if stage_reg.pop(wix_id, None) is not None:
        _save_stage_registry(stage_reg)
    notes_reg = _load_notes_registry()
    if notes_reg.pop(wix_id, None) is not None:
        _save_notes_registry(notes_reg)
    assigned_reg = _load_assigned_registry()
    if assigned_reg.pop(wix_id, None) is not None:
        _save_assigned_registry(assigned_reg)
    inv_reg = _load_invoice_registry()
    if inv_reg.pop(wix_id, None) is not None:
        _save_invoice_registry(inv_reg)
    return jsonify({"ok": True})


@dashboard.route("/jobs")
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
    return render_template("temp_jobs.html", active_tab="jobs", job_id=None, entries=entries)


@dashboard.route("/past-jobs")
@_require_auth
def _legacy_past_jobs():
    return redirect(url_for(".past_jobs"), code=301)


@dashboard.route("/job/<job_id>/delete", methods=["POST"])
@_require_auth
def delete_job(job_id: str):
    """Delete a job's workspace directory. _job_dir validates the id and 404s on
    a bad/unknown id, so this can't be used for path traversal."""
    job_dir = _job_dir(job_id)
    shutil.rmtree(job_dir, ignore_errors=True)
    flash("Job deleted.")
    return redirect(url_for(".past_jobs"))
