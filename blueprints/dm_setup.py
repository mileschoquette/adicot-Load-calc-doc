"""Generate DM Setup tab, builds a Design Master setup .vbs (plus a
load-calc JSON payload) from the CMS work order and/or the parsed .dm
export."""

from __future__ import annotations

import io
import re
import zipfile

from flask import Blueprint, abort, flash, redirect, request, render_template, send_file, url_for
from werkzeug.utils import secure_filename

from core import (
    HAS_DM_SETUP_GENERATOR, _DM_SETUP_IMPORT_ERROR, dmsg,
    _cms, _is_parsed, _load_meta, _load_report, _require_auth, _safe_job_path, _save_meta,
)

dm_setup = Blueprint("dm_setup", __name__)

# Confirmed DM construction "type code" (iType / mass class) values, per the real
# dm_hvac.dm. The engineer picks one per row (never auto-filled); each row's dropdown
# also includes the .dm's own value so a valid code is always available.
_MASS_CLASS_OPTIONS = {
    "wall": [("Frame", 2), ("Wood stud", 5), ("Block / CMU", 10)],
    "roof": [("Wood deck / vented attic", 4), ("Frame", 2), ("Concrete / masonry", 10)],
    "door": [("Steel, insulated", 2), ("Wood", 5)],
}

# Maps each portal.html construction dropdown answer to its DM mass-class code.
# "Other," "Storefront/glass," "None," and blank stay unmapped (falls through
# to manual pick, same as today).
_WIX_MASS_CLASS_MAP = {
    "wall": {"CMU": 10, "CMU + rigid insul": 10, "ICF": 10,
             "Steel stud + batt": 2, "Steel stud + rigid insul": 2, "Wood frame": 5},
    "roof": {"Steel deck": 2, "Metal frame": 2, "Concrete deck": 10,
             "Wood deck": 4, "Wood frame": 4},
    "door": {"Insulated Metal": 2, "Hollow metal": 2, "Solid wood": 5},
}


def _num(v):
    """First number found in v (e.g. 'R-19' -> 19.0, '0.44' -> 0.44), else None."""
    if v is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


def _r_to_u(v):
    """R-value string -> assembly U (U = 1/R). Values <=1 are treated as already-U."""
    n = _num(v)
    if not n or n <= 0:
        return None
    return round(1.0 / n, 3) if n > 1 else round(n, 3)


def _cms_envelope(snap: dict) -> dict:
    """Envelope spec candidates pulled from the CMS work-order record (if any)."""
    snap = snap or {}
    wc, rc = (snap.get("wallColor") or ""), (snap.get("roofColor") or "")
    return {
        "wall_primary_u": _r_to_u(snap.get("wallRValue")),
        "wall_part_u":    _r_to_u(snap.get("partRValue")),
        "wall_dark":      "dark" in wc.lower(), "wall_has_color": bool(wc),
        "roof_u":         _r_to_u(snap.get("roofRValue")),
        "roof_dark":      "dark" in rc.lower(), "roof_has_color": bool(rc),
        "glass_u":        _num(snap.get("glassU")),
        "glass_shgc":     _num(snap.get("glassSHGC")),
        "wall_itype":     _WIX_MASS_CLASS_MAP["wall"].get(snap.get("wallConstruction")),
        "roof_itype":     _WIX_MASS_CLASS_MAP["roof"].get(snap.get("deckType")),
        "door_itype":     _WIX_MASS_CLASS_MAP["door"].get(snap.get("doorType")),
    }


def _mass_options(cat: str, export_itype):
    """(label, code) options for a row's mass-class dropdown, always including the
    .dm's own code so nothing valid is lost."""
    opts = list(_MASS_CLASS_OPTIONS.get(cat, []))
    if export_itype is not None and all(code != export_itype for _, code in opts):
        opts.append((f"As in .dm (type {export_itype})", export_itype))
    return opts


def _dm_setup_construction(report: dict, meta: dict) -> dict:
    """Editable construction-type rows: DM-export list, spec fields prefilled from
    the CMS work-order when available (source-tagged), .dm value as fallback."""
    wix = _cms_envelope(meta.get("cms_snapshot") or {})

    def pick(wix_val, export_val):
        """Return (value, source): CMS wins, then the .dm value, else blank."""
        if wix_val is not None:
            return wix_val, "CMS"
        if export_val is not None:
            return export_val, ".dm"
        return "", ""

    def opaque(cat, items):
        itype_key = f"{cat}_itype"
        rows = []
        for c in items:
            if not c.get("name"):
                continue
            name = c["name"]
            if cat == "wall":
                wu = wix["wall_part_u"] if "part" in name.lower() else wix["wall_primary_u"]
                wu = wu if wu is not None else wix["wall_primary_u"]
                wdark, wdark_src = (wix["wall_dark"], "CMS") if wix["wall_has_color"] \
                    else ("dark" in (c.get("color") or "").lower(), ".dm")
            elif cat == "roof":
                wu = wix["roof_u"]
                wdark, wdark_src = (wix["roof_dark"], "CMS") if wix["roof_has_color"] \
                    else ("dark" in (c.get("color") or "").lower(), ".dm")
            else:  # door — CMS has no door U/color
                wu = None
                wdark, wdark_src = ("dark" in (c.get("color") or "").lower(), ".dm")
            u, u_src = pick(wu, c.get("u_value"))
            itype, itype_src = pick(wix[itype_key], c.get("ashrae_type"))
            rows.append({
                "name": name, "u": u, "u_source": u_src,
                "dark": bool(wdark), "dark_source": wdark_src,
                "itype": itype, "itype_source": itype_src,
                "options": _mass_options(cat, c.get("ashrae_type")),
            })
        # Pre-parse (no .dm export yet): synthesize rows from the CMS work-order.
        if not rows:
            def wrow(name, u, dark, has_color):
                itype = wix[itype_key]
                return {"name": name,
                        "u": u if u is not None else "", "u_source": "CMS" if u is not None else "",
                        "dark": bool(dark), "dark_source": "CMS" if has_color else "",
                        "itype": itype if itype is not None else "", "itype_source": "CMS" if itype is not None else "",
                        "options": _mass_options(cat, None)}
            if cat == "wall":
                if wix["wall_primary_u"] is not None:
                    rows.append(wrow("Exterior wall (work order)", wix["wall_primary_u"], wix["wall_dark"], wix["wall_has_color"]))
                if wix["wall_part_u"] is not None:
                    rows.append(wrow("Partition (work order)", wix["wall_part_u"], wix["wall_dark"], wix["wall_has_color"]))
            elif cat == "roof" and wix["roof_u"] is not None:
                rows.append(wrow("Roof (work order)", wix["roof_u"], wix["roof_dark"], wix["roof_has_color"]))
            elif cat == "door" and wix["door_itype"] is not None:
                rows.append(wrow("Door (work order)", None, False, False))
            # door: CMS has no door U — nothing to synthesize beyond itype above
        return rows

    glasses = []
    for c in report.get("glass_types", []):
        if not c.get("name"):
            continue
        u, u_src = pick(wix["glass_u"], c.get("u_value"))
        s, s_src = pick(wix["glass_shgc"], c.get("shgc"))
        glasses.append({"name": c["name"], "u": u, "u_source": u_src,
                        "shgc": s, "shgc_source": s_src})
    if not glasses and wix["glass_u"] is not None:
        glasses.append({"name": "Glazing (work order)",
                        "u": wix["glass_u"], "u_source": "CMS",
                        "shgc": wix["glass_shgc"] if wix["glass_shgc"] is not None else "",
                        "shgc_source": "CMS" if wix["glass_shgc"] is not None else ""})

    return {
        "walls": opaque("wall", report.get("wall_types", [])),
        "roofs": opaque("roof", report.get("roof_types", [])),
        "doors": opaque("door", report.get("door_types", [])),
        "glasses": glasses,
        "from_cms": bool(meta.get("cms_snapshot")),
    }


def _dm_setup_job(job_id: str):
    """Resolve (job_path, meta, report, parsed) for the DM Setup tab. Works even
    when the project has no workspace/HTML yet — falls back to the live Wix record
    exactly like job_star, so the tab opens straight from the work order."""
    job_path = _safe_job_path(job_id)
    if job_path.exists():
        return job_path, _load_meta(job_id), _load_report(job_id), _is_parsed(job_path)
    record = _cms.get_project(job_id)
    if not record:
        abort(404)
    meta = {
        "cms_item_id": job_id,
        "cms_snapshot": record,
        "project_name": (record.get("title") or "").strip() or job_id,
        "project_address": (record.get("projectAddress") or "").strip(),
    }
    return job_path, meta, {}, False


_MONTH_NAMES = ("January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December")


def _dm_setup_settings(meta: dict, report: dict | None = None) -> dict:
    """Prefill for the editable project settings. Weather station, latitude,
    elevation, osa_low_dry, and osa_daily_range are prefilled from the work
    order (CMS snapshot) first, falling back to the parsed .dm export
    (report.json) only when the work order is blank; overlaid with any
    previously saved values. The tblMonth cooling design condition is
    prefilled from the parsed .dm export only. The remaining site/solar
    fields (longitude, standard meridian, dehumidification humidity ratio,
    clear-sky tau) have no DM field at all — they only ever come from a
    prior save."""
    snap = meta.get("cms_snapshot") or {}
    proj = (report or {}).get("project") or {}

    month_name = proj.get("osa_high_month") or ""
    month_num = ""
    if month_name in _MONTH_NAMES:
        month_num = str(_MONTH_NAMES.index(month_name) + 1)

    ps = {
        "project_name": meta.get("project_name") or snap.get("title") or "",
        "weather_station": snap.get("weatherStation") or proj.get("project_location") or "",
        "latitude": snap.get("latitude") or proj.get("latitude_deg", ""),
        "elevation": snap.get("elevation") or proj.get("elevation_ft", ""),
        "osa_low_dry": snap.get("osaLowDry") or proj.get("osa_low_f", ""),
        "osa_daily_range": snap.get("osaDailyRange") or proj.get("osa_daily_range_f", ""),
        "cooling_design_month": month_num,
        "cooling_design_db": proj.get("osa_high_db_f", ""),
        "cooling_design_wb": proj.get("osa_high_wb_f", ""),
        "longitude": "", "standard_meridian": "",
        "heating_design_percentile": "", "cooling_design_percentile": "",
        "dehumid_humidity_ratio": "", "clear_sky_taub": "", "clear_sky_taud": "",
    }
    for k, v in list(ps.items()):
        if v is None:
            ps[k] = ""
    saved = (meta.get("dm_setup_inputs") or {}).get("project_settings") or {}
    for k, v in saved.items():
        if v not in (None, ""):
            ps[k] = v
    return ps


@dm_setup.route("/job/<job_id>/dm-setup")
@_require_auth
def job_dm_setup(job_id: str):
    if not HAS_DM_SETUP_GENERATOR:
        flash(f"DM Setup generator unavailable: {_DM_SETUP_IMPORT_ERROR}")
        return redirect(url_for("job_lifecycle.job_star", job_id=job_id))
    # Works with just the work order — no workspace/HTML required.
    _job_path, meta, report, parsed = _dm_setup_job(job_id)

    library = dmsg.list_room_types()                       # [{name, source, summary}]
    lib_names = {rt["name"] for rt in library}
    # Room types this job actually uses (parsed from the DM export)
    used_in_lib = sorted({r.get("name") for r in report.get("rooms_p1", [])
                          if r.get("name")} & lib_names)

    saved = meta.get("dm_setup_inputs", {})
    selected = set(saved.get("selected_room_types") or used_in_lib)

    # Group the library by source for display (170 / FBC / 621 / other)
    order = {"170": 0, "FBC": 1, "621": 2}
    groups: dict[str, list] = {}
    for rt in library:
        groups.setdefault(rt.get("source") or "Other", []).append(rt)
    grouped = sorted(groups.items(),
                     key=lambda kv: (order.get(kv[0], 9), kv[0]))

    con = _dm_setup_construction(report, meta)
    return render_template(
        "jobs/dm_setup.html",
        active_tab="dm-setup", job_id=job_id, meta=meta,
        parsed=parsed, mass_options=_MASS_CLASS_OPTIONS,
        settings=_dm_setup_settings(meta, report),
        grouped=grouped, selected=selected, used_in_lib=used_in_lib,
        lib_count=len(library), **con,
    )


@dm_setup.route("/job/<job_id>/dm-setup/generate", methods=["POST"])
@_require_auth
def job_dm_setup_generate(job_id: str):
    if not HAS_DM_SETUP_GENERATOR:
        flash(f"DM Setup generator unavailable: {_DM_SETUP_IMPORT_ERROR}")
        return redirect(url_for("job_lifecycle.job_star", job_id=job_id))
    job_path, meta, _report, _parsed = _dm_setup_job(job_id)

    selected = request.form.getlist("room_types")
    errors: list[str] = []

    def _f(key):
        return (request.form.get(key) or "").strip()

    def read_opaque(cat):
        """Read included wall/roof/door rows from the editable form fields."""
        out = []
        for i in request.form.getlist(f"{cat}_include"):
            name = _f(f"{cat}_name_{i}")
            if not name:
                continue
            u, t = _f(f"{cat}_u_{i}"), _f(f"{cat}_type_{i}")
            if not u or not t:
                errors.append(f"{name}: needs both a U-value and a mass class.")
                continue
            try:
                out.append({"name": name, "description": name, "u": float(u),
                            "itype": int(t),
                            "dark": request.form.get(f"{cat}_dark_{i}") is not None})
            except ValueError:
                errors.append(f"{name}: U-value must be a number.")
        return out

    def read_glass():
        out = []
        for i in request.form.getlist("glass_include"):
            name = _f(f"glass_name_{i}")
            if not name:
                continue
            u, s = _f(f"glass_u_{i}"), _f(f"glass_shgc_{i}")
            if not u:
                errors.append(f"{name}: needs a U-value.")
                continue
            try:
                out.append({"name": name, "description": name,
                            "u": float(u), "shgc": float(s) if s else 0.0})
            except ValueError:
                errors.append(f"{name}: U-value and SHGC must be numbers.")
        return out

    walls = read_opaque("wall")
    roofs = read_opaque("roof")
    doors = read_opaque("door")
    glasses = read_glass()

    # Project/site settings — only some of these (dmsg.PROJECT_SETTING_KEYS +
    # COOLING_MONTH_FIELDS) have a home in the .dm; the rest (SITE_ONLY_FIELDS)
    # are saved to meta.json / the load-calc payload only. Non-empty fields only.
    project_settings = {f: _f(f"ps_{f}") for f in dmsg.ALL_SETTING_FIELDS}
    project_settings = {k: v for k, v in project_settings.items() if v}

    if errors:
        for e in errors:
            flash(e)
        return redirect(url_for(".job_dm_setup", job_id=job_id))

    if not selected and not (walls or roofs or doors or glasses) and not project_settings:
        flash("Select at least one room type, construction type, or project setting to generate a setup script.")
        return redirect(url_for(".job_dm_setup", job_id=job_id))

    # Use the edited project name for the file/dialog if provided.
    proj_name = project_settings.get("project_name") or meta.get("project_name") or job_id
    try:
        vbs = dmsg.render_setup_vbs(
            proj_name, selected,
            wall_types=walls, glass_types=glasses,
            roof_types=roofs, door_types=doors,
            project_settings=project_settings,
        )
    except KeyError as e:
        flash(f"Could not generate setup script: {e}")
        return redirect(url_for(".job_dm_setup", job_id=job_id))

    safe = secure_filename(proj_name) or "job"
    vbs_name = f"{safe}-DM-Setup.vbs"
    json_name = f"{safe}-loadcalc.json"

    # Second output: the load-calc payload (same selection), for Adicot's own
    # RTS calc / review UI. Same inputs as the .vbs, so the two never diverge.
    try:
        payload = dmsg.render_setup_json(
            proj_name, selected,
            wall_types=walls, glass_types=glasses,
            roof_types=roofs, door_types=doors,
            project_settings=project_settings,
        )
    except KeyError as e:
        flash(f"Could not generate load-calc file: {e}")
        return redirect(url_for(".job_dm_setup", job_id=job_id))

    if job_path.exists():
        # Existing workspace: persist both artifacts + the selection & settings.
        meta["dm_setup_inputs"] = {"selected_room_types": selected,
                                   "project_settings": project_settings}
        _save_meta(job_id, meta)
        out_dir = job_path / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / vbs_name).write_text(vbs, encoding="utf-8")
        # Stable name in the job root so the Load Calc tab can find it later.
        (job_path / "loadcalc_input.json").write_text(payload, encoding="utf-8")

    # Deliver BOTH files in one download so the engineer gets the DM script and
    # the load-calc input together.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(vbs_name, vbs)
        z.writestr(json_name, payload)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f"{safe}-DM-Setup.zip", mimetype="application/zip")
