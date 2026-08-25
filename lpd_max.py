"""Lighting Power Density (LPD) code maximums by occupancy type.

Source: Florida Building Code, Energy Conservation (FBC-EC 2023, 8th ed.), which
adopts the ASHRAE 90.1-2019 space-by-space method for commercial lighting power
allowances. These are the same values already embedded as display strings in
archive/admin-review.html's LIGHTING_MAXES table (verified 2026-06-19) — kept here as a
single numeric source of truth so the Flask-side portal can compute the same
code-max badge server-side instead of duplicating the table in JS.

Keyed by the exact "Occupancy Type" dropdown option strings used across the app
(archive/admin-review.html, templates/portal.html). "Residential" and "Other" have no
FBC-EC space-by-space entry in the source table and intentionally resolve to
None — the calling code should skip the badge rather than guess a value.

Edition matters: if Adicot's jurisdiction adopts a newer FBC-EC / ASHRAE 90.1
edition, re-verify every value below before relying on it.
"""

from __future__ import annotations

from typing import Optional

LPD_MAX_W_PER_SF: dict[str, float] = {
    "Dining / Fast food":    0.76,
    "Food prep / Kitchen":   1.21,
    "Office":                0.82,
    "Retail":                1.26,
    "Medical office":        1.91,
    "Assembly / Classrooms": 1.11,
    "Warehouse":             0.66,
    # "Residential" and "Other" have no FBC-EC space-by-space entry — omitted
    # intentionally rather than guessing a value.
}


def lpd_max_for(occupancy_type: str) -> Optional[float]:
    """Return the FBC-EC lighting power density max (W/sf) for an occupancy
    type, or None if there's no code-max entry for it (e.g. "Residential",
    "Other", empty, or unrecognized)."""
    if not occupancy_type:
        return None
    return LPD_MAX_W_PER_SF.get(occupancy_type)
