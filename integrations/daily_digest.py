"""Daily jobs-list email digest — content + in-process scheduler.

Sends once a day at SEND_HOUR (America/New_York), one personalized email per
person in PERSON_EMAILS, grouped into the same buckets shown on the Jobs
landing page (green/yellow/red/unset), excluding invoiced and expired
projects, plus a shared deadlines section for jobs due in exactly 2 weeks, 1
week, or today (job_due_dates.json overrides, else signed date + 15 work
days — see calendar_utils.py) and any calendar_events.json entry due today.
Each person's copy also gets a "YOUR TASKS" section combining jobs
assigned to them (job_assigned.json), their own free-standing manual
tasks (manual_tasks.json), and any calendar event due today assigned
specifically to them. Runs as a background thread inside
the existing single-worker web service rather than a separate Render Cron
Job, since a separate Cron Job resource can't mount the same persistent disk
this app keeps job_stages.json / qbo_invoices_*.json on.

Pulls _build_cms_entries()/_entry_bucket()/_BUCKET_RANK from core.py (not
app.py) — core.py has no dependency on this module, so this is a plain
top-level import with no circular-import risk, unlike the old deferred
`import app` this replaced when app.py was split into blueprints.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import calendar_utils
import core
from integrations import email_client

log = logging.getLogger(__name__)

JOBS_DIR = Path(os.environ.get("JOBS_DIR", "./jobs"))
PERSON_EMAILS = core.PERSON_EMAILS
EXCLUDED_BUCKETS = {"invoiced", "expired"}
SEND_HOUR = 7
SEND_TZ = ZoneInfo("America/New_York")
CHECK_INTERVAL_SECONDS = 300

_BUCKET_LABELS = {
    "green": "Ready to submit",
    "yellow": "Needs work",
    "red": "Waiting on client",
    "unset": "Unset",
}

# Job due-date offsets (days until due) that earn a mention in the digest —
# a heads-up at 2 weeks out, another at 1 week out, and a final one the day
# it's actually due.
_DEADLINE_LABELS = {14: "DUE IN 2 WEEKS", 7: "DUE IN 1 WEEK", 0: "DUE TODAY"}


def _state_path() -> Path:
    return JOBS_DIR / "digest_state.json"


def _already_sent_today(today: str) -> bool:
    try:
        return json.loads(_state_path().read_text()).get("last_sent") == today
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def _mark_sent(today: str) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_sent": today}))


def _load_manual_tasks() -> dict:
    try:
        return json.loads((JOBS_DIR / "manual_tasks.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _load_calendar_events() -> list[dict]:
    try:
        return json.loads((JOBS_DIR / "calendar_events.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _deadlines_by_offset(entries: list[dict], today: datetime.date) -> dict[int, list[dict]]:
    """Jobs whose due date is exactly 14, 7, or 0 days out today — the three
    offsets that earn a digest mention (see _DEADLINE_LABELS)."""
    by_offset: dict[int, list[dict]] = {offset: [] for offset in _DEADLINE_LABELS}
    for e in entries:
        if not e.get("due_date"):
            continue
        due = datetime.date.fromisoformat(e["due_date"])
        days = calendar_utils.days_until(due, today)
        if days in by_offset:
            by_offset[days].append(e)
    return by_offset


def _render_body(entries: list[dict], deadlines_by_offset: dict[int, list[dict]],
                  shared_events_today: list[dict], person: str | None = None,
                  manual_tasks: list[dict] | None = None,
                  person_events_today: list[dict] | None = None) -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "https://adicot-load-calc-doc.onrender.com")
    lines = []
    # Deadlines and shared calendar events are identical for every recipient,
    # same as the bucket list below — only the "YOUR TASKS" section differs.
    for offset, label in _DEADLINE_LABELS.items():
        group = deadlines_by_offset.get(offset, [])
        if not group:
            continue
        lines.append(label)
        for e in group:
            job_label = e["job_no"] or e["address"] or e["title"] or "(untitled)"
            lines.append(f"  {job_label} — {base}/job/{e['_id']}/star")
        lines.append("")
    if shared_events_today:
        lines.append("TODAY'S CALENDAR EVENTS")
        for ev in shared_events_today:
            lines.append(f"  {ev['title']}")
            if ev.get("notes"):
                lines.append(f"      note: {ev['notes']}")
        lines.append("")
    for bucket in ("green", "yellow", "red", "unset"):
        group = [e for e in entries if e["bucket"] == bucket]
        if not group:
            continue
        lines.append(_BUCKET_LABELS[bucket].upper())
        for e in group:
            label = e["job_no"] or e["address"] or e["title"] or "(untitled)"
            lines.append(f"  {label} — {base}/job/{e['_id']}/star")
        lines.append("")
    # Personal section is appended (not folded into the buckets above) so every
    # recipient still gets the same shared list; only Miles/Phoebe/Adi's own
    # copy gets this extra section, and only when they actually have a task.
    if person:
        mine = [e for e in entries if person in e.get("assigned_to", [])]
        tasks = manual_tasks or []
        events = person_events_today or []
        if mine or tasks or events:
            lines.append("YOUR TASKS:")
            for e in mine:
                label = e["job_no"] or e["address"] or e["title"] or "(untitled)"
                lines.append(f"  {label} — {base}/job/{e['_id']}/star")
                if e.get("notes"):
                    lines.append(f"      note: {e['notes'][-1]['text']}")
            for t in tasks:
                lines.append(f"  - {t['text']}")
            for ev in events:
                lines.append(f"  - {ev['title']} (due today)")
                if ev.get("notes"):
                    lines.append(f"      note: {ev['notes']}")
            lines.append("")
    if not lines:
        lines = ["No open jobs need attention today."]
    return "\n".join(lines)


def send_daily_digest() -> bool:
    """Build and send today's digest — one personalized email per person in
    PERSON_EMAILS, each with the full shared list plus their own "Your tasks"
    section if they have any assignments. Returns True only if every send
    succeeded."""
    entries = core._build_cms_entries()
    for e in entries:
        e["bucket"] = core._entry_bucket(e)
    included = [e for e in entries if e["bucket"] not in EXCLUDED_BUCKETS]
    included.sort(key=lambda e: (
        core._BUCKET_RANK.get(e["bucket"], 99),
        (e["address"] or e["title"] or e["job_no"]).lower(),
    ))

    today = datetime.date.today()
    deadlines_by_offset = _deadlines_by_offset(included, today)

    events_today = [ev for ev in _load_calendar_events() if ev.get("date") == today.isoformat()]
    shared_events_today = [ev for ev in events_today if ev.get("assigned_to") == "everyone"]

    manual_tasks_reg = _load_manual_tasks()
    subject = f"Adicot Jobs Daily Digest — {today.isoformat()}"
    ok_all = True
    for key, email in PERSON_EMAILS.items():
        person_events_today = [ev for ev in events_today if ev.get("assigned_to") == key]
        body = _render_body(included, deadlines_by_offset, shared_events_today, person=key,
                             manual_tasks=manual_tasks_reg.get(key, []),
                             person_events_today=person_events_today)
        if not email_client.send_email([email], subject, body):
            log.error("Daily digest email failed to send to %s.", key)
            ok_all = False
    return ok_all


def _scheduler_loop():
    while True:
        try:
            now = datetime.datetime.now(SEND_TZ)
            today = now.date().isoformat()
            if now.hour >= SEND_HOUR and not _already_sent_today(today):
                if send_daily_digest():
                    _mark_sent(today)
        except Exception:
            log.exception("daily digest scheduler tick failed")
        time.sleep(CHECK_INTERVAL_SECONDS)


def start_scheduler() -> None:
    threading.Thread(target=_scheduler_loop, daemon=True).start()
