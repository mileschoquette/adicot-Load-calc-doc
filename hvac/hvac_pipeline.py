"""HVAC Loads pipeline — orchestration + backward-compat re-exports.

Library module imported by app.py (Flask). Call build_all_pdfs(...) to produce
the three deliverable PDFs from a Design Master HTML export.

The actual parsing/compute/legacy-PDF logic lives in hvac_parse.py,
hvac_compute.py, and hvac_legacy_pdf.py respectively; this module re-exports
everything from all three so `import hvac_pipeline as hp` / `hp.xxx` keeps
working unchanged for every existing call site.
"""

from __future__ import annotations
from pathlib import Path

from hvac.hvac_parse import *
from hvac.hvac_parse import _clean_number, _clean_int, _ft_inches, _data_cells, _data_rows, _simple_rows
from hvac.hvac_compute import *
from hvac.hvac_legacy_pdf import *
from hvac.hvac_legacy_pdf import _hdr, _cell, _fmt_int, _fmt_num, _fmt_date, _parse_calc_date


def _fmt_cfm(v):
    if v is None or v == 0:
        return "-"
    return f"{int(round(float(v))):,}"


def build_all_pdfs(html_path: Path, config: ProjectConfig,
                   engineer: EngineerInfo, firm: FirmInfo,
                   out_dir: Path = Path("./pdfs"),
                   project_name: str | None = None,
                   zone_overrides: dict | None = None) -> dict:
    out_dir.mkdir(exist_ok=True)
    html_text = html_path.read_text(encoding="latin-1")
    report = parse_report(html_text)
    computed = compute(report, config)
    computed = apply_zone_overrides(computed, zone_overrides or {})

    pn = project_name or computed.project_name
    safe = pn.replace(" ", "_").replace("/", "-")

    # Each schedule is rendered as a real spreadsheet (.xlsx), then converted to
    # PDF by LibreOffice so the delivered PDF is spreadsheet-origin (imports
    # cleanly into AutoCAD). If LibreOffice isn't available (local dev) or a
    # conversion fails, fall back to the ReportLab renderer so a PDF always
    # exists. Conversions run one at a time to keep the memory peak low.
    import traceback
    from pdf import schedule_xlsx
    from pdf import xlsx_to_pdf

    schedules = [
        ("Ventilation",
         lambda p: schedule_xlsx.build_ventilation_schedule_xlsx(computed, p, project_name=pn),
         lambda p: build_ventilation_schedule_pdf(computed, report, p, project_name=pn)),
        ("Air_Balance",
         lambda p: schedule_xlsx.build_air_balance_xlsx(computed, p, project_name=pn, config=config),
         lambda p: build_air_balance_pdf(computed, p, project_name=pn, config=config)),
        ("Load",
         lambda p: schedule_xlsx.build_load_summary_xlsx(computed, report, config, engineer, firm, p, project_name=pn),
         lambda p: build_load_summary_cover(computed, report, config, engineer, firm, p, project_name=pn)),
    ]

    out = {"computed": computed, "xlsx": {}}
    keymap = {"Ventilation": "ventilation_schedule",
              "Air_Balance": "air_balance", "Load": "load_summary"}
    for key, build_xlsx, build_pdf_fallback in schedules:
        xlsx_path = out_dir / f"{safe}-{key}.xlsx"
        pdf_path = out_dir / f"{safe}-{key}.pdf"
        try:
            build_xlsx(xlsx_path)
            out["xlsx"][keymap[key]] = xlsx_path
        except Exception:
            traceback.print_exc()
        converted = xlsx_to_pdf.convert(xlsx_path, pdf_path)
        if not converted:
            # LibreOffice missing or conversion failed → keep a PDF deliverable.
            build_pdf_fallback(pdf_path)
        out[keymap[key]] = pdf_path

    return out


def print_ventilation_schedule(computed: ComputedReport) -> None:
    """Print the Ventilation Schedule as a fixed-width text table."""
    print()
    print("=" * 100)
    print("VENTILATION SCHEDULE".center(100))
    print(computed.mechanical_code.center(100))
    print("=" * 100)
    # Column widths
    w_room, w_type, w_rp, w_pz, w_ra, w_az, w_vbz = 22, 38, 6, 5, 8, 6, 6
    header = (f"{'Room':<{w_room}} {'Room Type':<{w_type}} "
              f"{'Rp':>{w_rp}} {'Pz':>{w_pz}} {'Ra':>{w_ra}} {'Az':>{w_az}} {'Vbz':>{w_vbz}}")
    print(header)
    print(f"{'':<{w_room}} {'':<{w_type}} "
          f"{'[CFM/p]':>{w_rp}} {'[#]':>{w_pz}} {'[cfm/ft2]':>{w_ra}} {'[ft2]':>{w_az}} {'[CFM]':>{w_vbz}}")
    print("-" * 100)
    total_people = 0
    for r in computed.rooms:
        rp = f"{r.rp_cfm_per_person:.1f}" if r.rp_cfm_per_person else "0"
        pz = str(int(r.pz_people)) if r.pz_people else "0"
        ra = r.ra_display or "0"
        az = f"{int(round(r.area_ft2))}"
        vbz = f"{int(round(r.vent_cfm))}"
        # Truncate long room/type names for the text table
        room = r.room[:w_room - 1]
        rtype = r.room_type[:w_type - 1]
        print(f"{room:<{w_room}} {rtype:<{w_type}} "
              f"{rp:>{w_rp}} {pz:>{w_pz}} {ra:>{w_ra}} {az:>{w_az}} {vbz:>{w_vbz}}")
        if r.pz_people:
            total_people += int(r.pz_people)
    print("-" * 100)
    total_oa = int(round(computed.total_vent_oa_cfm))
    print(f"{total_people} Occupants".ljust(60) + f"Total Min. OA {total_oa} CFM".rjust(40))


def print_air_balance(computed: ComputedReport) -> None:
    """Print the Building Air Balance as a fixed-width text table."""
    print()
    print("=" * 100)
    print("BUILDING AIR BALANCE".center(100))
    print("=" * 100)
    w_zone = 38
    w_col = 11
    cols = ("Supply", "Return", "Bldg Vent", "Bldg Exh", "Air Bal")
    header = f"{'Zone':<{w_zone}}" + "".join(f"{c:>{w_col}}" for c in cols)
    print(header)
    print(f"{'':<{w_zone}}" + "".join(f"{'[cfm]':>{w_col}}" for _ in cols))
    print("-" * 100)
    for z in computed.zones:
        name = f"{z.zone_name}"[:w_zone - 1]
        print(f"{name:<{w_zone}}"
              f"{_fmt_cfm(z.supply_cfm):>{w_col}}"
              f"{_fmt_cfm(z.return_cfm):>{w_col}}"
              f"{_fmt_cfm(z.vent_oa_cfm):>{w_col}}"
              f"{_fmt_cfm(z.bldg_exhaust_cfm):>{w_col}}"
              f"{_fmt_cfm(z.air_balance_cfm):>{w_col}}")
    print("-" * 100)
    print(f"{'Totals:':>{w_zone}}"
          f"{'':>{w_col}}"
          f"{'':>{w_col}}"
          f"{_fmt_cfm(computed.total_vent_oa_cfm):>{w_col}}"
          f"{_fmt_cfm(computed.total_bldg_exhaust_cfm):>{w_col}}"
          f"{_fmt_cfm(computed.total_air_balance_cfm):>{w_col}}")


def print_load_summary(computed: ComputedReport, report: HVACReport,
                        config: ProjectConfig, engineer: EngineerInfo) -> None:
    """Print the Load Summary cover info as a text block."""
    print()
    print("=" * 100)
    print("HEATING AND COOLING LOAD SUMMARY SHEET".center(100))
    print(computed.energy_code.center(100))
    print("=" * 100)
    print(f"  Calculations Performed by: {engineer.name}")
    print(f"  Contact:                   {engineer.email} {engineer.phone}")
    print(f"  {engineer.state_full} Registered Professional Engineer: Lic. No.: {computed.license_number}")
    print(f"  Date:                      {_fmt_date(_parse_calc_date(computed.calc_date))}")
    print()
    addr = config.project_address or computed.weather_station
    print(f"  {'Project Name':<26}{computed.project_name}")
    print(f"  {'Address':<26}{addr}")
    print(f"  {'Weather Station':<26}{computed.weather_station}")
    print(f"  {'Sizing Method':<26}CLTD")
    if computed.osa_high_db_f:
        print(f"  {'Outdoor Dry Bulb':<26}{int(round(computed.osa_high_db_f))} F")
    if computed.osa_high_wb_f:
        print(f"  {'Outdoor Wet Bulb':<26}{int(round(computed.osa_high_wb_f))} F")
    if computed.indoor_dry_bulb_f:
        print(f"  {'Indoor Dry Bulb':<26}{int(round(computed.indoor_dry_bulb_f))} F")
    if computed.indoor_rh:
        print(f"  {'RH':<26}{int(round(computed.indoor_rh))}%")
    gwd = grains_water_difference(report)
    if gwd is not None:
        print(f"  {'Grains Water Diff':<26}{gwd:.2f} [grains moisture/lb dry air]")
    print()
    # Per-zone loads
    w_zone = 38
    cols = ("Area[ft2]", "Total Cool", "Sensible", "Latent", "Heating")
    print(f"  {'Zone':<{w_zone}}" + "".join(f"{c:>14}" for c in cols))
    print(f"  {'':<{w_zone}}" + "".join(f"{'[Btu/h]':>14}" for _ in cols))
    print(f"  {'-' * (w_zone + 14 * len(cols))}")
    for z in computed.zones:
        name = f"{z.zone_name}"[:w_zone - 1]
        print(f"  {name:<{w_zone}}"
              f"{int(round(z.area_ft2)):>14,}"
              f"{int(round(z.cooling_total_btuh)):>14,}"
              f"{int(round(z.cooling_sensible_btuh)):>14,}"
              f"{int(round(z.cooling_latent_btuh)):>14,}"
              f"{int(round(z.heating_btuh)):>14,}")


def print_deliverables(results: dict, report: HVACReport,
                        config: ProjectConfig, engineer: EngineerInfo) -> None:
    """Print all three deliverables to the console for review."""
    computed = results["computed"]
    print_ventilation_schedule(computed)
    print_air_balance(computed)
    print_load_summary(computed, report, config, engineer)
