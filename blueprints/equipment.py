"""Equipment Selection tab — Carrier A/C and heat pump selector, plus the
optional ERV / dehumidifier load-adjustment glue."""

from __future__ import annotations

import traceback
import uuid

from flask import Blueprint, abort, flash, redirect, render_template, request, send_from_directory, url_for

from core import (
    HAS_DEHUMID, HAS_EQUIP_SCHEDULE, HAS_EQUIP_SELECTOR, HAS_ERV,
    _DEHUMID_IMPORT_ERROR, _EQUIP_IMPORT_ERROR, _ERV_IMPORT_ERROR,
    dh, eng, equip_schedule, erv_catalog, erv_performance,
    _job_dir, _load_meta, _load_report, _require_auth, _require_parsed, _save_meta,
)

equipment = Blueprint("equipment", __name__)


def _equip_type_map() -> dict:
    """(ac_types, hp_types) per eq_type; None = not run. Built lazily since it
    references hvac_selector (core.eng), which may not have imported
    (HAS_EQUIP_SELECTOR)."""
    return {
        "ac_single": ([eng.AC_SINGLE], None),
        "ac_two":    ([eng.AC_TWO],   None),
        "hp_single": (None, [eng.HP_SINGLE]),
        "hp_two":    (None, [eng.HP_TWO]),
        "ac":        ([eng.AC_SINGLE, eng.AC_TWO], None),
        "hp":        (None, [eng.HP_SINGLE, eng.HP_TWO]),
        # "all" → run both AC and HP, return side-by-side
        "all":       ([eng.AC_SINGLE, eng.AC_TWO], [eng.HP_SINGLE, eng.HP_TWO]),
    }


def _build_equip_zones(job_id: str) -> list[dict]:
    """Pull zone loads from report.json and convert Btuh → kBtu/h."""
    report = _load_report(job_id)
    zones = []
    for lt in report.get("load_total_system", []):
        name = lt.get("location", "")
        if not name:
            continue
        tc  = (lt.get("cool_total_btuh")    or 0) / 1000
        shc = (lt.get("cool_sensible_btuh") or 0) / 1000
        htg = (lt.get("heat_btuh")          or 0) / 1000
        if tc <= 0:
            continue
        zones.append({"name": name, "tc": tc, "shc": shc, "htg": htg})
    return zones


def _build_equip_conditions(job_id: str) -> dict:
    """Pull outdoor design conditions from report.json."""
    report = _load_report(job_id)
    proj = report.get("project") or {}
    return {
        "odb": proj.get("osa_high_db_f"),
        "owb": proj.get("osa_high_wb_f"),
    }


def _build_erv_models() -> list[dict]:
    """Catalog entries for the ERV model dropdown."""
    if not HAS_ERV:
        return []
    return [
        {"model": e.model, "manufacturer": e.manufacturer, "delivered_cfm": e.unit.delivered_cfm}
        for e in erv_catalog.CATALOG
    ]


def _build_dehumid_models() -> list[dict]:
    """Catalog rows for the dehumidifier model dropdown."""
    if not HAS_DEHUMID:
        return []
    df = dh.load_database()
    return [
        {"model": row["model"], "manufacturer": row["manufacturer"],
         "category": row["category"], "rated_capacity_pints_day": row["rated_capacity_pints_day"]}
        for _, row in df.iterrows()
    ]


def _erv_conditions_from_report(report: dict) -> dict:
    """Outside Air / Final Room Conditions psychrometric points from a report
    dict, in the shape erv_calculator.load_impact.compute_erv_impact expects
    (humidity ratio converted lb/lb -> grains). Empty dict if unavailable."""
    psychs = report.get("psychrometrics") or []
    if not psychs:
        return {}
    points = psychs[0].get("points", [])
    outside = next((p for p in points if "outside air" in (p.get("label") or "").lower()), None)
    final = next((p for p in points if "final room" in (p.get("label") or "").lower()), None)
    if not outside or not final:
        return {}
    if outside.get("dry_bulb_f") is None or final.get("dry_bulb_f") is None:
        return {}
    return {
        "cfm": outside.get("airflow_cfm"),
        "t1_f": outside.get("dry_bulb_f"),
        "w1_gr": (outside.get("humidity_ratio") or 0) * 7000,
        "t3_f": final.get("dry_bulb_f"),
        "w3_gr": (final.get("humidity_ratio") or 0) * 7000,
    }


@equipment.route("/job/<job_id>/equip")
@_require_auth
@_require_parsed
def job_equip(job_id: str):
    """Equipment Selection tab — pre-filled from load calc."""
    _job_dir(job_id)
    meta = _load_meta(job_id)
    zones = _build_equip_zones(job_id)
    conds = _build_equip_conditions(job_id)
    last  = meta.get("equip_inputs", {})
    last_erv = last.get("erv", {})
    last_dehumid = last.get("dehumid", {})
    erv_conds = _erv_conditions_from_report(_load_report(job_id))

    return render_template(
        "job_equip.html",
        active_tab="equip", job_id=job_id, meta=meta,
        zones=zones,
        zone_names=[z["name"] for z in zones],
        odb=last.get("odb") or conds.get("odb"),
        owb=last.get("owb") or conds.get("owb"),
        edb=last.get("edb", 80),
        ewb=last.get("ewb", 67),
        cap_min=last.get("cap_min", 100),
        cap_max=last.get("cap_max", 115),
        eq_type=last.get("eq_type", "all"),
        results=None,
        xlsx_name=None,
        has_erv=HAS_ERV, erv_import_error=_ERV_IMPORT_ERROR,
        has_dehumid=HAS_DEHUMID, dehumid_import_error=_DEHUMID_IMPORT_ERROR,
        erv_models=_build_erv_models(), dehumid_models=_build_dehumid_models(),
        include_erv=last_erv.get("include", False),
        erv_zone=last_erv.get("zone"), erv_model=last_erv.get("model"),
        erv_season=last_erv.get("season", "summer"),
        erv_airflow_frac=last_erv.get("airflow_frac", 1.0),
        erv_cfm_default=erv_conds.get("cfm"),
        include_dehumid=last_dehumid.get("include", False),
        dehumid_zone=last_dehumid.get("zone"), dehumid_model=last_dehumid.get("model"),
        dehumid_units=last_dehumid.get("units", 1),
        erv_result=None, dehumid_result=None,
    )


@equipment.route("/job/<job_id>/equip/run", methods=["POST"])
@_require_auth
def job_equip_run(job_id: str):
    """Run equipment selection and render results."""
    job_dir = _job_dir(job_id)
    meta = _load_meta(job_id)

    if not HAS_EQUIP_SELECTOR:
        flash(f"Equipment selector unavailable: {_EQUIP_IMPORT_ERROR}")
        return redirect(url_for(".job_equip", job_id=job_id))

    def _f(key, default=None):
        v = (request.form.get(key) or "").strip()
        return float(v) if v else default

    odb     = _f("odb")
    owb     = _f("owb")
    edb     = _f("edb", 80.0)
    ewb     = _f("ewb", 67.0)
    cap_min = _f("cap_min", 100.0)
    cap_max = _f("cap_max", 115.0)
    eq_type = request.form.get("eq_type", "all")

    if odb is None or owb is None:
        flash("Outdoor dry bulb and wet bulb are required.")
        return redirect(url_for(".job_equip", job_id=job_id))

    names = request.form.getlist("zone_name")
    tcs   = request.form.getlist("zone_tc")
    shcs  = request.form.getlist("zone_shc")
    htgs  = request.form.getlist("zone_htg")

    zones_input   = []
    zones_display = []
    for i, name in enumerate(names):
        tc_v  = float(tcs[i])  if i < len(tcs)  and tcs[i]  else 0
        shc_v = float(shcs[i]) if i < len(shcs) and shcs[i] else 0
        htg_v = float(htgs[i]) if i < len(htgs) and htgs[i] else 0
        if tc_v <= 0:
            continue
        zones_input.append({
            "name": name,
            "total_cooling_kbtu":   tc_v,
            "sensible_cooling_kbtu": shc_v,
            "total_heating_kbtu":   htg_v,
        })
        zones_display.append({"name": name, "tc": tc_v, "shc": shc_v, "htg": htg_v})

    if not zones_input:
        flash("No zones with a cooling load found.")
        return redirect(url_for(".job_equip", job_id=job_id))

    # ── ERV / dehumidifier load adjustments — applied to a selected zone's
    # ── tc/shc BEFORE AC/HP selection runs, so picking different equipment
    # ── actually changes the resulting AC/HP sizing. Each is independently
    # ── optional and never aborts the AC/HP run on failure. ──────────────
    include_erv = request.form.get("include_erv") == "on"
    include_dehumid = request.form.get("include_dehumid") == "on"
    erv_zone_input = request.form.get("erv_zone", "").strip()
    erv_model_input = request.form.get("erv_model", "").strip()
    erv_season = request.form.get("erv_season", "summer").strip() or "summer"
    erv_airflow_frac = _f("erv_airflow_frac", 1.0)
    dehumid_zone_input = request.form.get("dehumid_zone", "").strip()
    dehumid_model_input = request.form.get("dehumid_model", "").strip()
    dehumid_units = int(_f("dehumid_units", 1) or 1)

    erv_result = None
    dehumid_result = None

    def _zone_entries(name):
        return [z for z in zones_input if z["name"] == name], [z for z in zones_display if z["name"] == name]

    if include_dehumid and HAS_DEHUMID and dehumid_zone_input and dehumid_model_input:
        try:
            df = dh.load_database()
            rows = df[df["model"] == dehumid_model_input]
            if rows.empty:
                raise ValueError(f"no dehumidifier catalog entry for model '{dehumid_model_input}'")
            model_row = rows.iloc[0]
            config = dh.DehumidConfig()
            adj = equip_schedule.dehumid_adjustment(model_row, dehumid_units, config)
            input_rows, display_rows = _zone_entries(dehumid_zone_input)
            for zi, zd in zip(input_rows, display_rows):
                new_tc, new_shc = equip_schedule.apply_load_deltas(
                    zi["total_cooling_kbtu"], zi["sensible_cooling_kbtu"], adj)
                zi["total_cooling_kbtu"] = new_tc
                zi["sensible_cooling_kbtu"] = new_shc
                zd["tc"] = new_tc
                zd["shc"] = new_shc
            dehumid_result = {
                "zone": dehumid_zone_input,
                "manufacturer": model_row["manufacturer"], "model": dehumid_model_input,
                "units": dehumid_units, **adj,
            }
        except Exception:
            traceback.print_exc()
            flash("Dehumidifier load adjustment failed — AC/HP sizing uses the un-adjusted zone load.")

    if include_erv and HAS_ERV and erv_zone_input and erv_model_input:
        try:
            entry = erv_catalog.get_by_model(erv_model_input)
            conds = _erv_conditions_from_report(_load_report(job_id))
            if not conds:
                raise ValueError("no Outside Air / Final Room Conditions psychrometric data in report.json")
            cfm = conds["cfm"] or entry.unit.delivered_cfm
            adj = equip_schedule.erv_adjustment(
                cfm, conds["t1_f"], conds["w1_gr"], conds["t3_f"], conds["w3_gr"],
                entry.performance, erv_airflow_frac, erv_season,
            )
            input_rows, display_rows = _zone_entries(erv_zone_input)
            for zi, zd in zip(input_rows, display_rows):
                new_tc, new_shc = equip_schedule.apply_load_deltas(
                    zi["total_cooling_kbtu"], zi["sensible_cooling_kbtu"], adj)
                zi["total_cooling_kbtu"] = new_tc
                zi["sensible_cooling_kbtu"] = new_shc
                zd["tc"] = new_tc
                zd["shc"] = new_shc
            erv_result = {
                "zone": erv_zone_input,
                "manufacturer": entry.manufacturer, "model": erv_model_input,
                "cfm": cfm, "season": erv_season, "airflow_frac": erv_airflow_frac,
                "frost_risk": erv_performance.check_frost_risk(conds["t1_f"]) if erv_season == "winter" else False,
                **adj,
            }
        except Exception:
            traceback.print_exc()
            flash("ERV load adjustment failed — AC/HP sizing uses the un-adjusted zone load.")

    meta["equip_inputs"] = {
        "odb": odb, "owb": owb, "edb": edb, "ewb": ewb,
        "cap_min": cap_min, "cap_max": cap_max, "eq_type": eq_type,
        "erv": {
            "include": include_erv, "zone": erv_zone_input, "model": erv_model_input,
            "season": erv_season, "airflow_frac": erv_airflow_frac,
        },
        "dehumid": {
            "include": include_dehumid, "zone": dehumid_zone_input, "model": dehumid_model_input,
            "units": dehumid_units,
        },
    }
    _save_meta(job_id, meta)

    # Run selection — "all" gives AC + HP side by side; others give one result per zone
    try:
        ac_types, hp_types = _equip_type_map().get(eq_type, ([eng.AC_SINGLE, eng.AC_TWO], [eng.HP_SINGLE, eng.HP_TWO]))
        multi_mode = (ac_types is not None and hp_types is not None)

        if multi_mode:
            results = eng.select_equipment_multi(
                zones_input, odb, owb, edb, ewb,
                cap_min, cap_max,
                ac_types=ac_types, hp_types=hp_types,
            )
        else:
            equipment_types = ac_types if ac_types else hp_types
            raw = eng.select_equipment(
                zones_input, odb, owb, edb, ewb,
                cap_min, cap_max,
                equipment_types=equipment_types,
            )
            # Normalise to same shape as multi for template reuse
            results = []
            for r in raw:
                is_hp = equipment_types and any(t in [eng.HP_SINGLE, eng.HP_TWO] for t in equipment_types)
                results.append({
                    "zone":   r["zone"],
                    "tc_load": r["tc_load"],
                    "shc_load": r["shc_load"],
                    "ac":  r["selected"] if not is_hp else None,
                    "ac_out_of_bounds": r.get("out_of_bounds", False) if not is_hp else False,
                    "ac_all_candidates": r.get("all_candidates", []) if not is_hp else [],
                    "hp":  r["selected"] if is_hp else None,
                    "hp_out_of_bounds": r.get("out_of_bounds", False) if is_hp else False,
                    "hp_all_candidates": r.get("all_candidates", []) if is_hp else [],
                    "multi_mode": False,
                })
            multi_mode = False

        if multi_mode:
            for r in results:
                r["multi_mode"] = True
    except Exception:
        tb = traceback.format_exc()
        print(tb, flush=True)
        flash("Equipment selection failed — check Render logs.")
        return redirect(url_for(".job_equip", job_id=job_id))

    # Flag HP heating data gaps (for any HP selections)
    hp_series = {"GH5SAN5", "GH8TAN5"}
    for res in results:
        for key in ("ac", "hp"):
            sel = res.get(key)
            if sel:
                sel["htg_data_missing"] = bool(
                    sel.get("odu_series") in hp_series
                    and sel.get("htg_load_kbtu")
                    and sel.get("htg_cap_kbtu") is None
                )

    # Write Excel schedule into job's out/ folder
    project_name = meta.get("project_name", "Project")
    safe  = project_name.replace(" ", "_").replace("/", "-")
    token = uuid.uuid4().hex[:6]
    xlsx_name = f"{safe}_{token}_schedule.xlsx"
    xlsx_path = job_dir / "out" / xlsx_name
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)

    # write_excel_schedule expects the old shape: [{selected, zone, tc_load, shc_load}, ...]
    # Flatten multi-mode results (AC + HP) into separate rows for the schedule.
    try:
        flat_results = []
        for r in results:
            if r.get("multi_mode"):
                # Add AC row then HP row
                for sel_key, oob_key, label in [
                    ("ac", "ac_out_of_bounds", "A/C"),
                    ("hp", "hp_out_of_bounds", "Heat Pump"),
                ]:
                    flat_results.append({
                        "zone": f"{r['zone']} ({label})",
                        "tc_load": r["tc_load"],
                        "shc_load": r["shc_load"],
                        "selected": r[sel_key],
                        "next_smaller": None,
                        "next_larger": None,
                        "all_candidates": r.get(f"{sel_key}_all_candidates", []),
                        "htg_data_missing": (r[sel_key] or {}).get("htg_data_missing", False) if r[sel_key] else False,
                    })
            else:
                sel = r.get("ac") or r.get("hp")
                flat_results.append({
                    "zone": r["zone"],
                    "tc_load": r["tc_load"],
                    "shc_load": r["shc_load"],
                    "selected": sel,
                    "next_smaller": None,
                    "next_larger": None,
                    "all_candidates": r.get("ac_all_candidates") or r.get("hp_all_candidates") or [],
                    "htg_data_missing": (sel or {}).get("htg_data_missing", False) if sel else False,
                })

        if HAS_EQUIP_SCHEDULE:
            equip_schedule.write_combined_schedule(
                flat_results, erv_result, dehumid_result,
                str(xlsx_path), project_name, odb, owb, cap_min, cap_max,
            )
        else:
            eng.write_excel_schedule(
                flat_results, str(xlsx_path), project_name, odb, owb, cap_min, cap_max
            )
    except Exception:
        traceback.print_exc()
        flash("Excel schedule generation failed — check Render logs.")
        xlsx_name = None

    return render_template(
        "job_equip.html",
        active_tab="equip", job_id=job_id, meta=meta,
        zones=zones_display,
        zone_names=[z["name"] for z in zones_display],
        odb=odb, owb=owb, edb=edb, ewb=ewb,
        cap_min=cap_min, cap_max=cap_max, eq_type=eq_type,
        results=results,
        xlsx_name=xlsx_name,
        has_erv=HAS_ERV, erv_import_error=_ERV_IMPORT_ERROR,
        has_dehumid=HAS_DEHUMID, dehumid_import_error=_DEHUMID_IMPORT_ERROR,
        erv_models=_build_erv_models(), dehumid_models=_build_dehumid_models(),
        include_erv=include_erv, erv_zone=erv_zone_input, erv_model=erv_model_input,
        erv_season=erv_season, erv_airflow_frac=erv_airflow_frac,
        erv_cfm_default=None,
        include_dehumid=include_dehumid, dehumid_zone=dehumid_zone_input,
        dehumid_model=dehumid_model_input, dehumid_units=dehumid_units,
        erv_result=erv_result, dehumid_result=dehumid_result,
    )


@equipment.route("/job/<job_id>/equip/download/<path:fname>")
@_require_auth
def job_equip_download(job_id: str, fname: str):
    """Download a generated equipment schedule."""
    if "/" in fname or "\\" in fname or ".." in fname:
        abort(400)
    job_dir  = _job_dir(job_id)
    out_dir  = job_dir / "out"
    if not (out_dir / fname).exists():
        abort(404)
    return send_from_directory(out_dir, fname, as_attachment=True)
