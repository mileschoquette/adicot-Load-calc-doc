"""HTML ↔ Wix project validator.

Compares the parsed Design Master HVAC report against the Wix CMS project
record the engineer selected from the autocomplete. Returns a list of
mismatches that the results page can render as a yellow warning banner.

Comparison philosophy (per design call):
  - Strict: any HTML value that differs from Wix flags a mismatch
  - Numbers-only: extract digits from both sides, sum them, compare exact
  - Unit-agnostic: we don't check units, just the numeric content
  - R↔U conversion: HTML stores glass U-values; the corresponding HTML R-value
                    is computed as 1/U on the fly before comparison
  - Empty-skip: if the Wix value is empty / 0 / "Unknown", the field isn't
                ready to validate yet and we skip it silently. Avoids noisy
                "you forgot to fill in Wix" warnings while the CMS gets
                populated.

The result is a list of `Mismatch` dataclasses, one per field that disagrees.
Each carries enough info for the template to render a row in a small table:

    field        — human-readable display name ("Roof R Value")
    wix_value    — the value Wix had (display string)
    html_values  — the value(s) the HTML had (list of display strings;
                   usually one entry but multi-type cases produce more)
    summary      — one-line human description used in the banner
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class Mismatch:
    field: str
    wix_value: str
    html_values: list[str] = field(default_factory=list)
    summary: str = ""


# ──────────────────────────────────────────────────────────────────────
# Numeric extraction
# ──────────────────────────────────────────────────────────────────────
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?|-?\.\d+")


def _extract_numbers(value: Any) -> list[float]:
    """Return every numeric token found in `value`, in order.

    Hyphens that follow a letter (as in "R-19") are part of the label,
    not a sign — so we strip them before extracting numbers. Standalone
    "-5.0" stays negative.

    Examples:
        "R-19"            → [19]
        "R-19 + R-5"      → [19, 5]
        "-5.0"            → [-5.0]
        "75°F"            → [75]
        "50%"             → [50]
        "0.30"            → [0.30]
        "Unknown"         → []
        ""                → []
        None              → []
        0.0               → [0.0]   (zero is a number — caller decides what to do)
        19                → [19]
        "0.04 CFM/SF"     → [0.04]
    """
    if value is None:
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    s = str(value).strip()
    if not s:
        return []
    # Strip hyphens immediately preceded by a letter so "R-19" → "R19" → 19,
    # while preserving "-5" → -5 (no leading letter).
    s = re.sub(r"(?<=[A-Za-z])-", "", s)
    return [float(m) for m in _NUM_RE.findall(s)]


def _sum_numbers(value: Any) -> Optional[float]:
    """Sum all numeric tokens in a value. Returns None if no numbers found."""
    nums = _extract_numbers(value)
    if not nums:
        return None
    return sum(nums)


def _is_blank_wix_value(value: Any) -> bool:
    """True when the Wix field hasn't been filled in yet.

    Treat empty string, None, "Unknown" (case-insensitive), 0, and 0.0
    all as "not yet populated" — those values mean "skip the comparison",
    not "compare to zero".
    """
    if value is None:
        return True
    if isinstance(value, (int, float)):
        return value == 0
    s = str(value).strip().lower()
    return s == "" or s == "unknown" or s == "0" or s == "0.0"


# ──────────────────────────────────────────────────────────────────────
# Comparison primitive
# ──────────────────────────────────────────────────────────────────────
def _compare_numeric(
    *,
    field_name: str,
    wix_value: Any,
    html_values: list[Any],
    transform_html=None,
    use_first: bool = False,
    tolerance: float = 1e-9,
    display_fmt=None,
) -> Optional[Mismatch]:
    """Compare a Wix numeric value against a list of HTML numeric values.

    - Skips if Wix is blank (returns None — no mismatch).
    - transform_html, if given, is applied to each HTML value BEFORE numeric
      extraction. Used for R↔U conversion: pass `lambda u: 1/u if u else None`
      to convert a U-value to an R-value before comparing to a Wix R-value.
    - use_first: when True, compare only the FIRST numeric token from each
      side, not the sum. Useful for fields where unit text contains digits
      that aren't values — e.g. "0.04 CFM / ft 2" should compare on 0.04,
      not 0.04 + 2. Default False keeps the sum behavior for R-value
      fields where layered values ("R-19 + R-5") should add up.
    - tolerance: absolute numeric tolerance for "equal". Default near-zero
      (true exact match). R-value comparisons use a small tolerance (~0.1)
      because converting U→R amplifies precision artifacts: U=0.0526 in DM's
      HTML rounds to R=19.011, not exactly R-19.
    - display_fmt: optional callable(parsed_num, raw_value) -> str that
      formats the HTML value for display in the mismatch summary. For R-value
      comparisons this returns "R-19.0" instead of the raw U-value "0.0526",
      so engineers see values in the units they're used to comparing.
    - Strict: returns Mismatch if ANY html value disagrees with wix.
    """
    if _is_blank_wix_value(wix_value):
        return None

    def _parse(v):
        nums = _extract_numbers(v)
        if not nums:
            return None
        return nums[0] if use_first else sum(nums)

    wix_num = _parse(wix_value)
    if wix_num is None:
        return Mismatch(
            field=field_name,
            wix_value=str(wix_value),
            html_values=[str(v) for v in html_values],
            summary=f"{field_name}: Wix value '{wix_value}' has no parseable number.",
        )

    html_nums: list[Optional[float]] = []
    html_display: list[str] = []
    for raw in html_values:
        v = transform_html(raw) if transform_html else raw
        if v is None:
            html_nums.append(None)
            html_display.append("(missing)")
            continue
        n = _parse(v)
        html_nums.append(n)
        if display_fmt and n is not None:
            html_display.append(display_fmt(n, raw))
        else:
            html_display.append(str(raw))

    differences = [
        n for n in html_nums if n is None or abs(n - wix_num) > tolerance
    ]
    if not differences:
        return None

    return Mismatch(
        field=field_name,
        wix_value=str(wix_value),
        html_values=html_display,
        summary=(f"{field_name}: Wix has {wix_value}, "
                 f"HTML has {', '.join(html_display) or '(no values)'}."),
    )


# ──────────────────────────────────────────────────────────────────────
# Field-specific comparators
# ──────────────────────────────────────────────────────────────────────
_R_TOLERANCE = 0.1   # R-value units. Tighter than this and 1/U rounding starts
                     # creating false positives; looser and real-spec changes
                     # like R-19 vs R-21 would be hidden.

# Display formatter for R-value comparisons: show the *converted* R value
# (rounded to 1 decimal, like "R-19.0") rather than the raw HTML U-value.
# Engineers compare in R-space, so the message should be in R-space too.
def _r_display(r_num: float, raw_u_value) -> str:
    return f"R-{r_num:.1f}"


def _compare_roof_r(report, wix) -> Optional[Mismatch]:
    """Wix.roofRValue (R) vs report.roof_types[*].u_value (U → R via 1/U)."""
    roof_us = [rt.u_value for rt in getattr(report, "roof_types", [])]
    return _compare_numeric(
        field_name="Roof R Value",
        wix_value=wix.get("roofRValue"),
        html_values=roof_us,
        transform_html=lambda u: (1.0 / u) if u else None,
        tolerance=_R_TOLERANCE,
        display_fmt=_r_display,
    )


def _compare_wall_r(report, wix) -> Optional[Mismatch]:
    """Wix.wallRValue vs report.wall_types[*].u_value (→ R)."""
    wall_us = [wt.u_value for wt in getattr(report, "wall_types", [])]
    return _compare_numeric(
        field_name="Wall R Value",
        wix_value=wix.get("wallRValue"),
        html_values=wall_us,
        transform_html=lambda u: (1.0 / u) if u else None,
        tolerance=_R_TOLERANCE,
        display_fmt=_r_display,
    )


def _compare_part_r(report, wix) -> Optional[Mismatch]:
    """Wix.partRValue (partition R) vs HTML walls flagged as partitions.

    Design Master tags walls by type and direction. There's no perfect
    "partition" flag, but a wall whose description includes 'partition' or
    'interior' is a strong signal. For now, compare against ALL wall types
    matching either keyword. If neither type-name matches, skip silently —
    we'd rather miss a comparison than flag every regular wall as a partition
    mismatch.
    """
    parts = [
        wt.u_value
        for wt in getattr(report, "wall_types", [])
        if "partition" in (wt.description or "").lower()
        or "interior"  in (wt.description or "").lower()
        or "partition" in (wt.name        or "").lower()
        or "interior"  in (wt.name        or "").lower()
    ]
    if not parts:
        return None
    return _compare_numeric(
        field_name="Part R Value",
        wix_value=wix.get("partRValue"),
        html_values=parts,
        transform_html=lambda u: (1.0 / u) if u else None,
        tolerance=_R_TOLERANCE,
        display_fmt=_r_display,
    )


def _compare_glass_u(report, wix) -> Optional[Mismatch]:
    """Wix.glassU vs report.glass_types[*].u_value (direct numeric compare)."""
    glass_us = [gt.u_value for gt in getattr(report, "glass_types", [])]
    return _compare_numeric(
        field_name="Glass U",
        wix_value=wix.get("glassU"),
        html_values=glass_us,
    )


def _compare_glass_shgc(report, wix) -> Optional[Mismatch]:
    """Wix.glassSHGC vs report.glass_types[*].shgc."""
    shgcs = [gt.shgc for gt in getattr(report, "glass_types", [])]
    return _compare_numeric(
        field_name="Glass SHGC",
        wix_value=wix.get("glassSHGC"),
        html_values=shgcs,
    )


def _compare_indoor_temp(report, wix) -> Optional[Mismatch]:
    """Wix.indoorTemp vs report.project.default_cooling_temp_f."""
    proj = getattr(report, "project", None)
    cooling_temp = getattr(proj, "default_cooling_temp_f", None) if proj else None
    return _compare_numeric(
        field_name="Indoor Temp",
        wix_value=wix.get("indoorTemp"),
        html_values=[cooling_temp],
    )


def _compare_indoor_rh(report, wix) -> Optional[Mismatch]:
    """Wix.indoorRH (e.g. "50%") vs report.project.default_relative_humidity_pct."""
    proj = getattr(report, "project", None)
    rh = getattr(proj, "default_relative_humidity_pct", None) if proj else None
    return _compare_numeric(
        field_name="Indoor RH",
        wix_value=wix.get("indoorRH"),
        html_values=[rh],
    )


def _compare_infiltration(report, wix) -> Optional[Mismatch]:
    """Wix.infiltration vs the infiltration rule on the first parsed room.

    Per-room infiltration rules in DM are usually all the same value for a
    given building. We compare against the first non-empty rule we find.
    """
    rooms = getattr(report, "rooms_p1", [])
    first_rule = next(
        (r.infiltration_rule for r in rooms
         if (r.infiltration_rule or "").strip()),
        None,
    )
    if not first_rule:
        return None
    return _compare_numeric(
        field_name="Infiltration",
        wix_value=wix.get("infiltration"),
        html_values=[first_rule],
        use_first=True,
    )


# ──────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────
_COMPARATORS = [
    _compare_roof_r,
    _compare_wall_r,
    _compare_part_r,
    _compare_glass_u,
    _compare_glass_shgc,
    _compare_indoor_temp,
    _compare_indoor_rh,
    _compare_infiltration,
]


def compare(report, wix_project: Optional[dict]) -> list[dict]:
    """Run every comparator. Returns mismatches as JSON-serializable dicts.

    Returns [] when:
      - wix_project is None (no Wix link on this job)
      - wix_project is empty
      - all fields agree or all Wix values are blank
    """
    if not wix_project:
        return []
    mismatches: list[Mismatch] = []
    for fn in _COMPARATORS:
        try:
            m = fn(report, wix_project)
        except Exception as e:
            # Don't let a bad field crash the whole banner. Log into the
            # results as a mismatch so we know which comparator misfired.
            m = Mismatch(
                field=fn.__name__.replace("_compare_", ""),
                wix_value="(error)",
                html_values=[],
                summary=f"Validator error in {fn.__name__}: {e}",
            )
        if m is not None:
            mismatches.append(m)
    return [asdict(m) for m in mismatches]
