"""Roof-area vs. total-floor-area sanity check.

A building's total conditioned floor area divided by its roof (footprint) area
should land near the number of stories:

    ratio = total floor area / roof area  ≈  N stories

If floors were identical and fully stacked, ratio would equal N exactly; real
floors differ in size, so we allow a margin. The engineer supplies N (number of
stories) and the check passes when the ratio is within 10% of N:

    | ratio - N | < 0.1 * N

When it fails, the site just flags the engineer to eyeball the roof geometry
(e.g. in AutoCAD) — it does not try to list individual rooms.

Areas are read from the parsed report dict (report.json shape):
  * total floor area = sum of the zone rows' area_ft2 (Load Total Summary - System)
  * roof area        = sum of the Roofs table area_ft2

Pure logic — no Flask, no network.
"""
from __future__ import annotations

TOLERANCE_FRAC = 0.1  # allowed deviation from N stories, as a fraction of N


def _zone_total_area(report: dict) -> float:
    """Sum of per-zone floor areas, matching compute()'s zone filter."""
    total = 0.0
    for z in report.get("load_total_system") or []:
        cool = z.get("cool_total_btuh")
        area = z.get("area_ft2")
        if (cool in (0, None)) and (area in (0, None)):
            continue  # skip non-zone / empty rows
        total += area or 0.0
    return total


def _roof_total_area(report: dict) -> float:
    return sum((r.get("area_ft2") or 0.0) for r in (report.get("roofs") or []))


def check_roof_area(report: dict, num_stories) -> dict:
    """Run the roof/total-area ratio check.

    Returns a dict the Quality Check template can render directly. `ran` is
    False (with `reason` set) when the check can't be evaluated — no roof data,
    no zone area, or no story count entered.
    """
    total_area = _zone_total_area(report)
    roof_area = _roof_total_area(report)

    result = {
        "ran": False,
        "num_stories": num_stories,
        "total_area": total_area,
        "roof_area": roof_area,
        "ratio": None,
        "tolerance": None,
        "low": None,
        "high": None,
        "ok": None,
        "reason": "",
    }

    # Parse the engineer's story count (blank/absent = not entered yet).
    if num_stories in (None, ""):
        result["reason"] = "Enter the number of stories to run this check."
        return result
    try:
        n = float(num_stories)
    except (TypeError, ValueError):
        result["reason"] = "Number of stories must be a number."
        return result
    if n <= 0:
        result["reason"] = "Number of stories must be greater than zero."
        return result

    if not roof_area:
        result["reason"] = "No roof area found in the parsed data."
        return result
    if not total_area:
        result["reason"] = "No zone floor area found in the parsed data."
        return result

    ratio = total_area / roof_area
    tol = TOLERANCE_FRAC * n
    result.update({
        "ran": True,
        "ratio": ratio,
        "tolerance": tol,
        "low": n - tol,
        "high": n + tol,
        "ok": abs(ratio - n) < tol,
    })
    return result
