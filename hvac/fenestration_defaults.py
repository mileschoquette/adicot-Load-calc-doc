"""Default fenestration and opaque-door U-factors and SHGC, by assembly type.

Source: IECC / Florida Building Code, Energy Conservation, Tables C303.1.3(1),
C303.1.3(2) and C303.1.3(3) — the prescriptive defaults used when a project has
no NFRC-rated product data. Transcribed 2026-09-04.

Same contract as hvac/lpd_max.py: an input with no row in the table resolves to
None rather than a guessed value, and the caller skips the fill instead of
substituting a wrong figure. That matters here because the tables only cover
single and double glazing, clear and tinted — triple pane, low-e and reflective
have no entry, and the engineer enters U/SHGC directly for those.

Edition matters: if Adicot's jurisdiction adopts a newer code, re-verify every
value below before relying on it.
"""

from __future__ import annotations

from typing import Optional

# Table C303.1.3(1) — default glazed window, glass door and skylight U-factors.
# {frame type: {(assembly, glazing): U}}. Glazed Block is listed in the table as
# a frame type with a single value and no double-glazed or skylight column, so
# it's handled separately in the lookups below rather than sitting here.
GLAZED_U: dict[str, dict[tuple[str, str], float]] = {
    "Metal":                   {("window", "Single"): 1.2,  ("window", "Double"): 0.8,
                                ("skylight", "Single"): 2.0, ("skylight", "Double"): 1.3},
    "Metal with Thermal Break": {("window", "Single"): 1.1,  ("window", "Double"): 0.65,
                                ("skylight", "Single"): 1.9, ("skylight", "Double"): 1.1},
    "Nonmetal or Metal Clad":  {("window", "Single"): 0.95, ("window", "Double"): 0.55,
                                ("skylight", "Single"): 1.75, ("skylight", "Double"): 1.05},
}

# Table C303.1.3(3) — default SHGC by glazing and tint. Applies to windows,
# glass doors and skylights alike, which is why there's no assembly dimension.
GLAZED_SHGC: dict[tuple[str, str], float] = {
    ("Single", "Clear"):  0.8,
    ("Single", "Tinted"): 0.7,
    ("Double", "Clear"):  0.7,
    ("Double", "Tinted"): 0.6,
}

# Glazed Block spans both tables: U 0.6 from C303.1.3(1) (window only) and
# SHGC 0.6 from C303.1.3(3), where it is its own glazing column.
GLAZED_BLOCK = "Glazed Block"
GLAZED_BLOCK_U = 0.6
GLAZED_BLOCK_SHGC = 0.6

# Table C303.1.3(2) — default opaque door U-factors.
DOOR_U: dict[str, float] = {
    "Uninsulated Metal":         1.2,
    "Insulated Metal (Rolling)": 0.9,
    "Insulated Metal (Other)":   0.6,
    "Wood":                      0.5,
    "Insulated, nonmetal edge, max 45% glazing, any glazing double pane": 0.35,
}

FRAME_TYPES = list(GLAZED_U) + [GLAZED_BLOCK]
GLAZING_TYPES = ["Single", "Double"]
TINTS = ["Clear", "Tinted"]
DOOR_TYPES = list(DOOR_U)


def _defaults(assembly: str, frame: str, glazing: str, tint: str) -> Optional[dict]:
    """Shared body of glass_defaults/skylight_defaults."""
    frame, glazing, tint = (frame or "").strip(), (glazing or "").strip(), (tint or "").strip()
    if frame == GLAZED_BLOCK:
        # No skylight row exists for glazed block.
        if assembly != "window":
            return None
        return {"u": GLAZED_BLOCK_U, "shgc": GLAZED_BLOCK_SHGC}
    u = GLAZED_U.get(frame, {}).get((assembly, glazing))
    shgc = GLAZED_SHGC.get((glazing, tint))
    if u is None or shgc is None:
        return None
    return {"u": u, "shgc": shgc}


def glass_defaults(frame: str, glazing: str, tint: str) -> Optional[dict]:
    """{'u', 'shgc'} for a window or glass door, or None when any input has no
    row in the table (blank, "Triple", "Low-E", an unrecognised frame, ...)."""
    return _defaults("window", frame, glazing, tint)


def skylight_defaults(frame: str, glazing: str, tint: str) -> Optional[dict]:
    """{'u', 'shgc'} for a skylight, or None. Note skylight U-factors are much
    higher than the window values for the same frame."""
    return _defaults("skylight", frame, glazing, tint)


def door_u(door_type: str) -> Optional[float]:
    """Default U-factor for an opaque door type, or None if not in the table."""
    return DOOR_U.get((door_type or "").strip())
