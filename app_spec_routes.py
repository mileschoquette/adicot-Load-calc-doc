# ============================================================================
# === Specifications tab — routes ============================================
# Paste this block into app.py with the other route definitions.
# Add near the top with the other local imports:
#
#     import spec_engine
#     import spec_data
#     import spec_dxf
#
# Follows the job_duct / job_charts pattern: read meta + CMS, render a template,
# post-back to save inputs, separate handler to generate the .dxf.
#
# Data sources per the locked design:
#   pink  (equipment selection, CMS): systemType, heatType, acMounting, maxCFM
#   blue  (proposal/spec, CMS):        hasOutsideAir, hasExhaust, ceilingConcealedGWB
#   yellow(user selects, spec time):   tbMode, hasVavOrFireSmoke, hasExistingControls
# All fields pre-fill from CMS where known and are editable on the tab.
# ============================================================================

# State-name lookup for STATE_TABLE rows that lack a full name.
_STATE_FULL = {
    "FL": "Florida", "AR": "Arkansas", "LA": "Louisiana", "MA": "Massachusetts",
    "OK": "Oklahoma", "PA": "Pennsylvania", "TX": "Texas", "WV": "West Virginia",
    "WY": "Wyoming",
}


def _derive_building_code(base: dict) -> str:
    mc = base.get("mech_code", "")
    import re as _re
    m = _re.search(r"(\d{4})", mc)
    yr = m.group(1) if m else ""
    return (f"{yr} International Building Code (IBC)").strip()


def _derive_plumbing_code(base: dict) -> str:
    mc = base.get("mech_code", "")
    import re as _re
    m = _re.search(r"(\d{4})", mc)
    yr = m.group(1) if m else ""
    return (f"{yr} International Plumbing Code (IPC)").strip()


def _spec_state_info(meta: dict) -> tuple[str, dict]:
    """Resolve 2-letter state + a STATE_TABLE row extended with spec-only codes.

    State comes from meta['state_code'] (saved at /upload), else the Wix snapshot.
    FL is fully specified in STATE_TABLE; other states get derived IBC/IPC labels
    the engineer can override on the tab.
    """
    state = (meta.get("state_code") or "").strip().upper()
    if not state:
        snap = meta.get("wix_snapshot") or {}
        state = (snap.get("state") or "").strip().upper()

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
    """Pull the CMS-sourced spec fields from the job's Wix snapshot.

    Maps the Wix record keys to the engine's expected cms.* keys. Keys absent
    from the snapshot simply come back empty and the tab shows the default.
    Adjust the .get() field names to match the actual Projects collection keys.
    """
    snap = meta.get("wix_snapshot") or {}
    return {
        # pink — equipment selection
        "systemType":  snap.get("systemType", ""),
        "heatType":    snap.get("heatType", ""),
        "acMounting":  snap.get("acMounting", ""),
        # seer2 / manufacturer also from equipment selection
        "seer2":       snap.get("seer2", ""),
        "manufacturer": snap.get("manufacturer", ""),
        "scopeText":   snap.get("scopeText", ""),
        "thermostatScope": snap.get("thermostatScope", ""),
    }


def _spec_loadcalc(meta: dict, job_id: str) -> dict:
    """Pull computed load values for the equipment performance clause.

    Reads the saved report's load_total_system roll-up (the same data the
    load-summary PDF uses). Returns the few values the spec needs.
    """
    report = _load_report(job_id)
    lt = report.get("load_total_system") or []
    # Sum tons / supply CFM across zones; take design conditions from project.
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
        "coolingTons": (f"{tons:g}" if tons else ""),
        "heatingBtuh": (f"{int(round(heat)):,}" if heat else ""),
        "supplyCFM":   (f"{int(round(supply)):,}" if supply else ""),
        "maxSystemCFM": maxcfm,
        "outdoorDB":   (str(int(round(proj['osa_high_db_f']))) if proj.get("osa_high_db_f") else ""),
        "outdoorWB":   (str(int(round(proj['osa_high_wb_f']))) if proj.get("osa_high_wb_f") else ""),
        "indoorDB":    (str(int(round(proj['default_cooling_temp_f']))) if proj.get("default_cooling_temp_f") else ""),
    }


def _spec_inputs(meta: dict, cms: dict, loadcalc: dict) -> dict:
    """Merge saved spec inputs over CMS/load-calc pre-fills for the form.

    Saved inputs (engineer edits) win; otherwise pre-fill from CMS / load calc.
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
        # pink
        "systemType":  pick("systemType", cms.get("systemType")),
        "heatType":    pick("heatType", cms.get("heatType")),
        "acMounting":  pick("acMounting", cms.get("acMounting")),
        "maxSystemCFM": pick("maxSystemCFM", loadcalc.get("maxSystemCFM")),
        # blue (defaults False unless saved/CMS says otherwise)
        "hasOutsideAir":      saved.get("hasOutsideAir", meta.get("wix_snapshot", {}).get("hasOutsideAir", False)),
        "hasExhaust":         saved.get("hasExhaust", meta.get("wix_snapshot", {}).get("hasExhaust", False)),
        "ceilingConcealedGWB": saved.get("ceilingConcealedGWB", meta.get("wix_snapshot", {}).get("ceilingConcealedGWB", False)),
        # yellow (user selects; tbMode defaults recommend)
        "tbMode":             pick("tbMode", "recommend"),
        "hasVavOrFireSmoke":  saved.get("hasVavOrFireSmoke", False),
        "hasExistingControls": saved.get("hasExistingControls", False),
    }


@app.route("/job/<job_id>/spec")
@_require_auth
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
    spec = spec_engine.build_spec(data, ctx, include_notes=True)  # review view keeps notes

    return render_template(
        "job_spec.html",
        active_tab="spec", job_id=job_id, meta=meta,
        inputs=inputs, state=state, state_info=state_info,
        spec=spec, warnings=spec.warnings,
    )


@app.route("/job/<job_id>/spec/save", methods=["POST"])
@_require_auth
def job_spec_save(job_id: str):
    """Persist edited spec inputs, then re-render the preview."""
    _job_dir(job_id)
    meta = _load_meta(job_id)

    def _cb(name):
        return request.form.get(name) == "on"

    meta["spec_inputs"] = {
        # pink (editable overrides)
        "systemType":  request.form.get("systemType", "").strip(),
        "heatType":    request.form.get("heatType", "").strip(),
        "acMounting":  request.form.get("acMounting", "").strip(),
        "maxSystemCFM": request.form.get("maxSystemCFM", "").strip(),
        # blue
        "hasOutsideAir":       _cb("hasOutsideAir"),
        "hasExhaust":          _cb("hasExhaust"),
        "ceilingConcealedGWB": _cb("ceilingConcealedGWB"),
        # yellow
        "tbMode":              request.form.get("tbMode", "recommend").strip(),
        "hasVavOrFireSmoke":   _cb("hasVavOrFireSmoke"),
        "hasExistingControls": _cb("hasExistingControls"),
    }
    _save_meta(job_id, meta)
    return redirect(url_for("job_spec", job_id=job_id))


@app.route("/job/<job_id>/spec/dxf", methods=["POST"])
@_require_auth
def job_spec_dxf(job_id: str):
    """Generate the spec .dxf (sheet copy, notes stripped) into out/."""
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)
    out_dir = job_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    state, state_info = _spec_state_info(meta)
    cms = _spec_cms(meta)
    loadcalc = _spec_loadcalc(meta, job_id)
    inputs = _spec_inputs(meta, cms, loadcalc)

    ctx = spec_engine.build_context(state, state_info, inputs, cms=cms, loadcalc=loadcalc)
    data = spec_data.load_spec_data()
    spec = spec_engine.build_spec(data, ctx, include_notes=False)  # sheet copy: no notes

    project_name = meta.get("project_name", "Specification")
    safe = project_name.replace(" ", "_").replace("/", "-")
    out_path = out_dir / f"{safe}-Specifications.dxf"

    spec_dxf.build_specification_dxf(
        spec, out_path,
        project_name=project_name,
        project_address=meta.get("project_address", ""),
        code_label=state_info.get("mech_code", ""),
    )
    return redirect(url_for("job_spec", job_id=job_id))
