"""Daily jobs-list email digest — content + in-process scheduler.

Sends once a day at SEND_HOUR (America/New_York) to RECIPIENTS, grouped into
the same buckets shown on the Jobs landing page (green/yellow/red/unset),
excluding invoiced and expired projects. Runs as a background thread inside
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
RECIPIENTS = ["mfc@adicot.com", "agc@adicot.com", "pc@adicot.com"]
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


def _render_body(entries: list[dict]) -> str:
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
            if e.get("notes"):
                lines.append(f"      note: {e['notes'][-1]['text']}")
        lines.append("")
    if not lines:
        lines = ["No open jobs need attention today."]
    return "\n".join(lines)


def send_daily_digest() -> bool:
    """Build and send today's digest. Returns True if the email was sent."""
    import app  # deferred: app.py is fully loaded by the time this runs

    entries = app._build_cms_entries()
    for e in entries:
        e["bucket"] = app._entry_bucket(e)
    included = [e for e in entries if e["bucket"] not in EXCLUDED_BUCKETS]
    included.sort(key=lambda e: (
        app._BUCKET_RANK.get(e["bucket"], 99),
        (e["address"] or e["title"] or e["job_no"]).lower(),
    ))

    body = _render_body(included)
    subject = f"Adicot Jobs Daily Digest — {datetime.date.today().isoformat()}"
    ok = email_client.send_email(RECIPIENTS, subject, body)
    if not ok:
        log.error("Daily digest email failed to send.")
    return ok


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
