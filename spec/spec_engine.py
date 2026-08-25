"""Specification renderer — pure logic, no Flask, no Wix.

Imported by app.py (spec tab route) and hvac_pipeline.py (PDF builder).

Jobs:
  1. eval_condition()       — decide whether a clause/section is included
  2. resolve_fields()       — resolve a clause's editable fields from sources,
                              compute auto-sentences (roof curb, DX, T&B mode)
  3. resolve_placeholders() — swap {{token}} from the merged context + field values
  4. build_spec()           — filter, drop empty sections, RENUMBER clean

Two layers of project-specific content:
  - conditions : JSON rule deciding whether a clause/section appears at all.
  - fields     : JSON array of editable values inside the body. Each:
                 {key, label, source, default, type}
                 source: cms.X | loadcalc.X | manual | auto.X
                 Missing value falls back to default (choice B); only shows the
                 visible [[token]] hole when there is no default either.

Numbering is NEVER stored. Section numbers are PART-scoped two-digit (1.01...);
clauses lettered A, B, C ... AA.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


# == 1. CONDITION EVALUATION ==========================================

def eval_condition(cond, ctx: dict) -> bool:
    """Return True if a clause/section with this condition should be included."""
    if cond is None or cond == "":
        return True
    if isinstance(cond, str):
        try:
            cond = json.loads(cond)
        except (ValueError, TypeError):
            return True
    if not isinstance(cond, dict) or not cond:
        return True

    if isinstance(cond.get("all"), list):
        return all(eval_condition(c, ctx) for c in cond["all"])
    if isinstance(cond.get("any"), list):
        return any(eval_condition(c, ctx) for c in cond["any"])
    if cond.get("not") is not None:
        return not eval_condition(cond["not"], ctx)

    actual = ctx.get(cond.get("field"))
    op = cond.get("op")
    val = cond.get("value")

    if op == "==":      return actual == val
    if op == "!=":      return actual != val
    if op == "in":      return isinstance(val, list) and actual in val
    if op == "not_in":  return isinstance(val, list) and actual not in val
    if op == "truthy":  return bool(actual)
    if op == "falsy":   return not actual
    if op in (">", ">=", "<", "<="):
        try:
            a, b = float(actual), float(val)
        except (TypeError, ValueError):
            return False
        if op == ">":  return a > b
        if op == ">=": return a >= b
        if op == "<":  return a < b
        if op == "<=": return a <= b
    return True


# == 2. FIELD RESOLUTION ==============================================

_HEAT_PUMP_VALUES = {"heat pump", "heatpump", "hp"}
_ROOF_MOUNTS = {"rtu", "roof", "rooftop"}


def _norm(key: str) -> str:
    return key.replace(".", "_")


def _source_value(source: str, ctx: dict):
    """Pull a raw value for a field source from the merged context."""
    if not source or source == "manual":
        return None
    if source.startswith("cms."):
        return ctx.get(_norm(source[4:]))
    if source.startswith("loadcalc."):
        return ctx.get(_norm(source))
    if source.startswith("auto."):
        return None
    return ctx.get(_norm(source))


def _auto_sentence(key: str, ctx: dict) -> str:
    """Compute an auto.X field value."""
    if key == "roofCurbSentence":
        mount = str(ctx.get("acMounting", "")).strip().lower()
        if mount in _ROOF_MOUNTS or ctx.get("hasRoofEquipment"):
            table = ctx.get("code_roofCurbTable") or "[[code.roofCurbTable]]"
            return ("For roof-mounted equipment, set on a factory curb at the "
                    f"height required by {table}.")
        return ""
    if key == "dxProtectionSentence":
        heat = str(ctx.get("heatType", "")).strip().lower()
        if heat in _HEAT_PUMP_VALUES:
            return ""
        return ("Provide DX-equipment protection - anti-short-cycle, head- and "
                "pressure-controls - and electric-heat capacities per schedule.")
    if key == "tbModeClause":
        mode = str(ctx.get("tbMode", "recommend")).strip().lower()
        if mode == "require":
            return ("An independent AABC- or NEBB-certified agency shall test and "
                    "balance all HVAC equipment.")
        if mode == "recommend":
            return "Independent AABC/NEBB testing and balancing is recommended."
        return ""
    return ""


def resolve_fields(clause: dict, ctx: dict, warnings: list) -> dict:
    """Resolve a clause's fields into {normalized_key: value_or_None}.

    Choice B: use source value if present, else default; None only when no
    default either (becomes a visible [[token]] hole).
    """
    raw = clause.get("fields")
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if not isinstance(raw, list):
        return {}

    out = {}
    for fdef in raw:
        key = fdef.get("key")
        if not key:
            continue
        source = fdef.get("source", "manual")
        default = fdef.get("default", "")

        if source.startswith("auto."):
            base = key.split(".")[-1] if "." in key else key
            out[_norm(key)] = _auto_sentence(base, ctx)
            continue

        val = _source_value(source, ctx)
        if val is None or val == "":
            val = default
        out[_norm(key)] = val if (val is not None and val != "") else None
    return out


# == 3. PLACEHOLDER RESOLUTION ========================================

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def resolve_placeholders(text: str, ctx: dict, warnings: list) -> str:
    """Replace {{token}} from ctx. None/missing -> visible [[token]] + warn.
    "" (intentional omission, e.g. an auto-sentence) -> empty, no warn."""
    if not text:
        return ""

    def repl(m):
        token = m.group(1)
        key = _norm(token)
        if key in ctx:
            val = ctx[key]
            if val is None:
                warnings.append(f"Missing value for {{{{{token}}}}}")
                return f"[[{token}]]"
            return str(val)
        warnings.append(f"Missing value for {{{{{token}}}}}")
        return f"[[{token}]]"

    out = _PLACEHOLDER_RE.sub(repl, text)
    out = re.sub(r"  +", " ", out).strip()
    return out


# == 4. NUMBERING =====================================================

def _pad2(n: int) -> str:
    return f"{n:02d}"


def _letter(i: int) -> str:
    s = ""
    i += 1
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


assert _letter(0) == "A"
assert _letter(25) == "Z"
assert _letter(26) == "AA"
assert _pad2(3) == "03"


# == 5. BUILD =========================================================

@dataclass
class RenderedClause:
    label: str
    text: str
    note: str = ""


@dataclass
class RenderedSection:
    num: str
    title: str
    clauses: list = field(default_factory=list)


@dataclass
class RenderedPart:
    num: int
    title: str
    sections: list = field(default_factory=list)


@dataclass
class RenderedSpec:
    parts: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def build_spec(data: dict, ctx: dict, include_notes: bool = True) -> RenderedSpec:
    """Filter by conditions, resolve fields + placeholders, renumber clean.

    include_notes=True keeps SPEC NOTES (review view); False strips them
    (client-facing PDF).
    """
    warnings: list = []

    included = [
        c for c in data.get("clauses", [])
        if c.get("active", True) is not False and eval_condition(c.get("conditions"), ctx)
    ]
    included.sort(key=lambda c: c.get("sortOrder", 0))

    clauses_by_section: dict = {}
    for c in included:
        clauses_by_section.setdefault(c.get("sectionKey"), []).append(c)

    spec = RenderedSpec(warnings=warnings)

    parts_sorted = sorted(data.get("parts", []), key=lambda p: p.get("sortOrder", 0))
    for p_idx, part in enumerate(parts_sorted):
        part_num = p_idx + 1

        secs = [
            s for s in data.get("sections", [])
            if s.get("partKey") == part.get("partKey")
            and eval_condition(s.get("conditions"), ctx)
        ]
        secs.sort(key=lambda s: s.get("sortOrder", 0))

        rendered_sections = []
        for s in secs:
            raw_clauses = clauses_by_section.get(s.get("sectionKey"), [])
            if not raw_clauses:
                continue
            rc = []
            for c in raw_clauses:
                field_vals = resolve_fields(c, ctx, warnings)
                local = dict(ctx)
                local.update(field_vals)
                text = resolve_placeholders(c.get("body", ""), local, warnings)
                if not text:
                    continue
                rc.append(RenderedClause(
                    label="", text=text,
                    note=(c.get("note", "") if include_notes else ""),
                ))
            if not rc:
                continue
            for i, clause in enumerate(rc):
                clause.label = f"{_letter(i)}."
            rendered_sections.append(
                RenderedSection(num="", title=s.get("title", ""), clauses=rc)
            )

        if not rendered_sections:
            continue
        for s_idx, rs in enumerate(rendered_sections):
            rs.num = f"{part_num}.{_pad2(s_idx + 1)}"
        spec.parts.append(
            RenderedPart(num=part_num, title=part.get("title", ""), sections=rendered_sections)
        )

    return spec


# == 6. CONTEXT BUILDER ===============================================

_SPLIT_LIKE = {"split", "VRF", "mini-split", "DX-split"}


def build_context(state: str, state_info: dict, inputs: dict,
                  cms: dict = None, loadcalc: dict = None) -> dict:
    """Build the flat ctx the engine reads.

    state, state_info : 2-letter code + STATE_TABLE[state] row (codes/license)
    inputs            : spec-tab form inputs (toggles, manual fields, tbMode, counts)
    cms               : CMS Projects record (systemType, heatType, seer2,
                        manufacturer, acMounting, thermostatScope, scopeText...)
    loadcalc          : computed values (coolingTons, heatingBtuh, supplyCFM,
                        outdoorDB, outdoorWB, indoorDB, oaCFM, maxSystemCFM...)
    """
    si = state_info or {}
    inp = inputs or {}
    cms = cms or {}
    lc = loadcalc or {}

    system_type = inp.get("systemType") or cms.get("systemType") or ""
    split_like = system_type in _SPLIT_LIKE

    def _b(key, default):
        v = inp.get(key)
        return default if v is None else bool(v)

    try:
        if inp.get("maxSystemCFM") not in (None, ""):
            max_cfm = float(inp.get("maxSystemCFM"))
        else:
            max_cfm = float(lc.get("maxSystemCFM") or 0.0)
    except (TypeError, ValueError):
        max_cfm = 0.0

    return {
        "state": si.get("state_full", state),
        "systemType": system_type,
        "maxSystemCFM": max_cfm,
        # conditional flags
        "hasRoofEquipment":     _b("hasRoofEquipment", system_type in ("RTU", "package")),
        "hasRefrigerantLines":  _b("hasRefrigerantLines", split_like),
        "hasUndergroundRefrig": _b("hasUndergroundRefrig", False),
        "hasVavOrFireSmoke":    _b("hasVavOrFireSmoke", False),
        "hasExistingControls":  _b("hasExistingControls", False),
        "hasExhaust":           _b("hasExhaust", False),
        "hasOutsideAir":        _b("hasOutsideAir", False),
        "ceilingConcealedGWB":  _b("ceilingConcealedGWB", False),
        # manual fields / counts (approved defaults)
        "tbMode":          inp.get("tbMode") or "recommend",
        "boundCopies":     inp.get("boundCopies") or "1",
        "spareFilterSets": inp.get("spareFilterSets") or "1",
        "flexDuctMaxLen":  inp.get("flexDuctMaxLen") or "10'-0\"",
        "exhaustSpecial":  inp.get("exhaustSpecial") or "",
        # CMS-sourced
        "heatType":        inp.get("heatType") or cms.get("heatType") or "",
        "seer2":           inp.get("seer2") or cms.get("seer2") or "",
        "manufacturer":    inp.get("manufacturer") or cms.get("manufacturer") or "",
        "acMounting":      inp.get("acMounting") or cms.get("acMounting") or "",
        "thermostatScope": inp.get("thermostatScope") or cms.get("thermostatScope") or "new and existing",
        "scopeText":       inp.get("scopeText") or cms.get("scopeText") or "",
        # load-calc (normalized for loadcalc.X tokens)
        "loadcalc_coolingTons": lc.get("coolingTons", ""),
        "loadcalc_heatingBtuh": lc.get("heatingBtuh", ""),
        "loadcalc_supplyCFM":   lc.get("supplyCFM", ""),
        "loadcalc_oaCFM":       lc.get("oaCFM", ""),
        "loadcalc_outdoorDB":   lc.get("outdoorDB", ""),
        "loadcalc_outdoorWB":   lc.get("outdoorWB", ""),
        "loadcalc_indoorDB":    lc.get("indoorDB", ""),
        # codes from STATE_TABLE row
        "code_fbc":           si.get("building_code", ""),
        "code_fmc":           si.get("mech_code", ""),
        "code_fecc":          si.get("energy_code", ""),
        "code_fpc":           si.get("plumbing_code", ""),
        "code_nec":           si.get("electrical_code", "National Electrical Code"),
        "code_roofCurbTable": si.get("roof_curb_table", ""),
    }
