"""Specifications tab. Named spec_bp (not spec.py) to avoid shadowing the
spec/ package (spec_engine, spec_data, spec_docx) created when the flat
modules were grouped into subpackages."""

from __future__ import annotations

import re

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for

from hvac import hvac_pipeline as hp
from spec import spec_data
from spec import spec_docx
from spec import spec_engine

from core import _job_dir, _load_meta, _load_report, _require_auth, _require_parsed, _save_meta

spec_bp = Blueprint("spec_bp", __name__)

# Wix API key -> CMS field mapping reference:
#   systemType      -> System Type       (also used for heatType)
#   acMounting      -> AC Mounting
#   coolingEff      -> Cooling Eff       (SEER2 rating)
#   manufacturer    -> Manufacturer      (new field)
#   description     -> Description       (scope text)
#   acNewExisting   -> AC New Existing   (thermostat scope)
#   hasOutsideAir   -> hasOutsideAir     (new boolean field)
#   hasExhaust      -> hasExhaust        (new boolean field)
#   suspCeiling     -> Susp Ceiling      (text: T-bar/Lay-in, GWB, open to roof deck, no)

_STATE_FULL = {
    "FL": "Florida", "AR": "Arkansas", "LA": "Louisiana", "MA": "Massachusetts",
    "OK": "Oklahoma", "PA": "Pennsylvania", "TX": "Texas", "WV": "West Virginia",
    "WY": "Wyoming",
}
_STATE_ABBREV = {v: k for k, v in _STATE_FULL.items()}


def _derive_building_code(base: dict) -> str:
    mc = base.get("mech_code", "")
    m = re.search(r"(\d{4})", mc)
    yr = m.group(1) if m else ""
    return f"{yr} International Building Code (IBC)".strip()


def _derive_plumbing_code(base: dict) -> str:
    mc = base.get("mech_code", "")
    m = re.search(r"(\d{4})", mc)
    yr = m.group(1) if m else ""
    return f"{yr} International Plumbing Code (IPC)".strip()


def _spec_state_info(meta: dict) -> tuple[str, dict]:
    """Resolve 2-letter state + STATE_TABLE row for the spec tab.

    Priority:
      1. meta['state_code']       — derived from project_address at upload
      2. cms_snapshot['state']    — Wix record state field
      3. engineer licensed state  — last resort, may differ from project state
    """
    state = (meta.get("state_code") or "").strip().upper()

    if not state:
        snap = meta.get("cms_snapshot") or {}
        state = (snap.get("state") or "").strip().upper()

    if not state:
        eng_state_full = (meta.get("engineer", {}).get("state") or "").strip()
        state = _STATE_ABBREV.get(eng_state_full, "")

    base = dict(hp.STATE_TABLE.get(state, {}))
    si = {
        "state_full":      base.get("state_full", _STATE_FULL.get(state, state)),
        "mech_code":       base.get("mech_code", ""),
        "energy_code":     base.get("energy_code", ""),
        "building_code":   base.get("building_code", _derive_building_code(base)),
        "plumbing_code":   base.get("plumbing_code", _derive_plumbing_code(base)),
        "electrical_code": base.get("electrical_code", "National Electrical Code"),
        "roof_curb_table": base.get("roof_curb_table", ""),
    }
    return state, si


def _spec_cms(meta: dict) -> dict:
    """Pull CMS-sourced spec fields from the Wix snapshot.

    Wix API key      Engine key            Notes
    ---------------- --------------------- ----------------------------------
    systemType       systemType            RTU / split / VRF / package
    systemType       heatType              same field — system type drives both
    acMounting       acMounting            RTU / slab / sidewall / other
    coolingEff       seer2                 SEER2 efficiency rating
    manufacturer     manufacturer          basis-of-design brand
    description      scopeText             plain-English scope sentence
    acNewExisting    thermostatScope       new / existing / new and existing
    hasOutsideAir    hasOutsideAir         boolean
    hasExhaust       hasExhaust            boolean
    suspCeiling      ceilingConcealedGWB   truthy when not blank/no/open
    """
    snap = meta.get("cms_snapshot") or {}
    susp = (snap.get("suspCeiling") or "").strip().lower()
    return {
        "systemType":      snap.get("systemType", ""),
        "heatType":        snap.get("systemType", ""),
        "acMounting":      snap.get("acMounting", ""),
        "seer2":           snap.get("coolingEff", ""),
        "manufacturer":    snap.get("manufacturer", ""),
        "scopeText":       snap.get("description", ""),
        "thermostatScope": snap.get("acNewExisting", ""),
        "hasOutsideAir":   snap.get("hasOutsideAir", False),
        "hasExhaust":      snap.get("hasExhaust", False),
        "ceilingConcealedGWB": susp not in ("", "no", "open to roof deck"),
    }


def _spec_loadcalc(meta: dict, job_id: str) -> dict:
    """Pull computed load values from report.json for the spec."""
    report = _load_report(job_id)
    lt = report.get("load_total_system") or []
    tons = 0.0
    supply = 0.0
    heat = 0.0
    maxcfm = 0.0
    for z in lt:
        try:
            tons += float(z.get("cool_total_tons") or 0)
        except (TypeError, ValueError):
            pass
        try:
            scfm = float(z.get("cool_cfm") or 0)
            supply += scfm
            maxcfm = max(maxcfm, scfm)
        except (TypeError, ValueError):
            pass
        try:
            heat += float(z.get("heat_btuh") or 0)
        except (TypeError, ValueError):
            pass
    proj = report.get("project") or {}
    return {
        "coolingTons":  (f"{tons:g}" if tons else ""),
        "heatingBtuh":  (f"{int(round(heat)):,}" if heat else ""),
        "supplyCFM":    (f"{int(round(supply)):,}" if supply else ""),
        "maxSystemCFM": maxcfm,
        "outdoorDB":    (str(int(round(proj["osa_high_db_f"]))) if proj.get("osa_high_db_f") else ""),
        "outdoorWB":    (str(int(round(proj["osa_high_wb_f"]))) if proj.get("osa_high_wb_f") else ""),
        "indoorDB":     (str(int(round(proj["default_cooling_temp_f"]))) if proj.get("default_cooling_temp_f") else ""),
    }


def _spec_inputs(meta: dict, cms: dict, loadcalc: dict) -> dict:
    """Merge saved engineer edits over CMS/load-calc pre-fills.

    Priority: saved edits > CMS > load calc > hardcoded default.
    """
    saved = meta.get("spec_inputs", {})

    def pick(key, *fallbacks):
        if saved.get(key) not in (None, ""):
            return saved[key]
        for fb in fallbacks:
            if fb not in (None, ""):
                return fb
        return ""

    return {
        "systemType":          pick("systemType", cms.get("systemType")),
        "heatType":            pick("heatType", cms.get("heatType")),
        "acMounting":          pick("acMounting", cms.get("acMounting")),
        "maxSystemCFM":        pick("maxSystemCFM", loadcalc.get("maxSystemCFM")),
        "hasOutsideAir":       saved.get("hasOutsideAir", cms.get("hasOutsideAir", False)),
        "hasExhaust":          saved.get("hasExhaust", cms.get("hasExhaust", False)),
        "ceilingConcealedGWB": saved.get("ceilingConcealedGWB", cms.get("ceilingConcealedGWB", False)),
        "tbMode":              pick("tbMode", "recommend"),
        "hasVavOrFireSmoke":   saved.get("hasVavOrFireSmoke", False),
        "hasExistingControls": saved.get("hasExistingControls", False),
    }


@spec_bp.route("/job/<job_id>/spec")
@_require_auth
@_require_parsed
def job_spec(job_id: str):
    """Specifications tab — pre-filled editable inputs + live spec preview."""
    _job_dir(job_id)
    meta = _load_meta(job_id)

    state, state_info = _spec_state_info(meta)
    cms = _spec_cms(meta)
    loadcalc = _spec_loadcalc(meta, job_id)
    inputs = _spec_inputs(meta, cms, loadcalc)

    ctx = spec_engine.build_context(state, state_info, inputs, cms=cms, loadcalc=loadcalc)
    data = spec_data.load_spec_data()
    spec = spec_engine.build_spec(data, ctx, include_notes=False)

    return render_template(
        "jobs/spec.html",
        active_tab="spec", job_id=job_id, meta=meta,
        inputs=inputs, state=state, state_info=state_info,
        spec=spec, warnings=spec.warnings,
    )


@spec_bp.route("/job/<job_id>/spec/save", methods=["POST"])
@_require_auth
def job_spec_save(job_id: str):
    """Persist edited spec inputs, then redirect to the preview."""
    _job_dir(job_id)
    meta = _load_meta(job_id)

    def _cb(name):
        return request.form.get(name) == "on"

    meta["spec_inputs"] = {
        "systemType":          request.form.get("systemType", "").strip(),
        "heatType":            request.form.get("heatType", "").strip(),
        "acMounting":          request.form.get("acMounting", "").strip(),
        "maxSystemCFM":        request.form.get("maxSystemCFM", "").strip(),
        "hasOutsideAir":       _cb("hasOutsideAir"),
        "hasExhaust":          _cb("hasExhaust"),
        "ceilingConcealedGWB": _cb("ceilingConcealedGWB"),
        "tbMode":              request.form.get("tbMode", "recommend").strip(),
        "hasVavOrFireSmoke":   _cb("hasVavOrFireSmoke"),
        "hasExistingControls": _cb("hasExistingControls"),
    }
    _save_meta(job_id, meta)
    return redirect(url_for(".job_spec", job_id=job_id))


@spec_bp.route("/job/<job_id>/spec/download-docx", methods=["POST"])
@_require_auth
def job_spec_download_docx(job_id: str):
    """Generate the spec .docx and send it directly as a download."""
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)

    state, state_info = _spec_state_info(meta)
    cms = _spec_cms(meta)
    loadcalc = _spec_loadcalc(meta, job_id)
    inputs = _spec_inputs(meta, cms, loadcalc)

    ctx = spec_engine.build_context(state, state_info, inputs, cms=cms, loadcalc=loadcalc)
    data = spec_data.load_spec_data()
    rendered = spec_engine.build_spec(data, ctx, include_notes=False)

    project_name = meta.get("project_name", "Specification")
    safe = project_name.replace(" ", "_").replace("/", "-")
    out_path = job_dir / "out" / f"{safe}-Specifications.docx"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        spec_docx.build_specification_docx(
            rendered, out_path,
            project_name=project_name,
            project_address=meta.get("project_address", ""),
            code_label=state_info.get("mech_code", ""),
        )
    except Exception as e:
        flash(f"Word doc generation failed: {e}")
        return redirect(url_for(".job_spec", job_id=job_id))

    return send_file(
        out_path,
        as_attachment=True,
        download_name=out_path.name,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
