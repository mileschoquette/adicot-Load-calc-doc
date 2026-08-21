"""Daily jobs-list email digest — content + in-process scheduler.

Sends once a day at SEND_HOUR (America/New_York), one personalized email per
person in PERSON_EMAILS, grouped into the same buckets shown on the Jobs
landing page (green/yellow/red/unset), excluding invoiced and expired
projects. Each person's copy also gets a "YOUR TASKS" section combining jobs
assigned to them (job_assigned.json) with their own free-standing manual
tasks (manual_tasks.json), when they have either. Runs as a background thread inside
the existing single-worker web service rather than a separate Render Cron
Job, since a separate Cron Job resource can't mount the same persistent disk
this app keeps job_stages.json / qbo_invoices_*.json on.

send_daily_digest() does a deferred `import app` (not a top-level import) to
avoid a circular import: app.py imports this module at load time to start
the scheduler thread, so app.py must finish loading first. By the time the
scheduler actually fires (long after app.py is fully loaded), the deferred
import is just a sys.modules cache hit.
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

import email_client

log = logging.getLogger(__name__)

JOBS_DIR = Path(os.environ.get("JOBS_DIR", "./jobs"))
PERSON_EMAILS = {"miles": "mfc@adicot.com", "adi": "agc@adicot.com", "phoebe": "pc@adicot.com"}
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


def _render_body(entries: list[dict], person: str | None = None,
                  manual_tasks: list[dict] | None = None) -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "https://adicot-load-calc-doc.onrender.com")
    lines = []
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
        if mine or tasks:
            lines.append("YOUR TASKS:")
            for e in mine:
                label = e["job_no"] or e["address"] or e["title"] or "(untitled)"
                lines.append(f"  {label} — {base}/job/{e['_id']}/star")
                if e.get("notes"):
                    lines.append(f"      note: {e['notes'][-1]['text']}")
            for t in tasks:
                lines.append(f"  - {t['text']}")
            lines.append("")
    if not lines:
        lines = ["No open jobs need attention today."]
    return "\n".join(lines)


def send_daily_digest() -> bool:
    """Build and send today's digest — one personalized email per person in
    PERSON_EMAILS, each with the full shared list plus their own "Your tasks"
    section if they have any assignments. Returns True only if every send
    succeeded."""
    import app  # deferred: app.py is fully loaded by the time this runs

    entries = app._build_cms_entries()
    for e in entries:
        e["bucket"] = app._entry_bucket(e)
    included = [e for e in entries if e["bucket"] not in EXCLUDED_BUCKETS]
    included.sort(key=lambda e: (
        app._BUCKET_RANK.get(e["bucket"], 99),
        (e["address"] or e["title"] or e["job_no"]).lower(),
    ))

    manual_tasks_reg = _load_manual_tasks()
    subject = f"Adicot Jobs Daily Digest — {datetime.date.today().isoformat()}"
    ok_all = True
    for key, email in PERSON_EMAILS.items():
        body = _render_body(included, person=key, manual_tasks=manual_tasks_reg.get(key, []))
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
