"""Pure date math for the job-due-date calendar — no Flask/Wix/JSON-file
knowledge, so daily_digest.py can import it directly without the deferred
`import app` dance used for everything else in that module."""

from __future__ import annotations

import datetime

WORK_DAYS_TO_DUE = 15


def add_work_days(start: datetime.date, n: int) -> datetime.date:
    """start + n work days (Mon-Fri only, no holiday calendar)."""
    d = start
    added = 0
    while added < n:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:  # Mon=0 .. Sun=6
            added += 1
    return d


def due_date_for(entered: datetime.date, override: str | None) -> datetime.date:
    """The job's due date: an explicit override if set, else 15 work days
    after it entered the queue."""
    if override:
        return datetime.date.fromisoformat(override)
    return add_work_days(entered, WORK_DAYS_TO_DUE)


def days_until(due: datetime.date, today: datetime.date) -> int:
    return (due - today).days
