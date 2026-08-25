"""HVAC Loads pipeline — legacy ReportLab PDF fallback.

Used only when LibreOffice is unavailable (e.g. local dev on macOS) — see
build_all_pdfs() in hvac_pipeline.py, which converts schedule_xlsx.py's
spreadsheets to PDF via LibreOffice and falls back to the renderers here if
that conversion fails. Keep these renderers in sync with schedule_xlsx.py's
build_*_xlsx functions (cross-reference comments on both sides).

Split out of hvac_pipeline.py; re-exported from there for backward compat.
"""

from __future__ import annotations
from pathlib import Path
import datetime

from reportlab.lib.pagesizes import letter
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER

from hvac.hvac_compute import ProjectConfig

BLACK = colors.black
GREY  = colors.HexColor("#7f7f7f")
LIGHT_GREY = colors.HexColor("#f2f2f2")

styles = getSampleStyleSheet()
TITLE = ParagraphStyle("Title", fontName="Helvetica-Bold", fontSize=18,
                       alignment=TA_CENTER, spaceAfter=4, textColor=BLACK)
SUBTITLE = ParagraphStyle("Sub", fontName="Helvetica", fontSize=11,
                          alignment=TA_CENTER, spaceAfter=14, textColor=BLACK)
SECTION = ParagraphStyle("Section", fontName="Helvetica-Bold", fontSize=12,
                         alignment=TA_CENTER, textColor=BLACK)
BODY = ParagraphStyle("Body", fontName="Helvetica", fontSize=9, leading=11)
SMALL = ParagraphStyle("Small", fontName="Helvetica", fontSize=8, leading=10)
FOOTER_BLOCK = ParagraphStyle("FooterBlock", fontName="Helvetica", fontSize=9,
                              alignment=TA_CENTER, leading=12)

# Wrapped headers (need <super>2</super> markup for ft² etc.)
HDR_STYLE = ParagraphStyle("hdr", fontName="Helvetica-Bold", fontSize=9,
                           textColor=BLACK, alignment=TA_CENTER, leading=11)
HDR_LEFT  = ParagraphStyle("hdr_l", parent=HDR_STYLE, alignment=TA_LEFT)
HDR_RIGHT = ParagraphStyle("hdr_r", parent=HDR_STYLE, alignment=TA_RIGHT)

# Body-cell styles that wrap long names (Zone, Room) instead of overflowing.
CELL_LEFT  = ParagraphStyle("cell_l", fontName="Helvetica", fontSize=9,
                            textColor=BLACK, alignment=TA_LEFT, leading=11)
CELL_RIGHT = ParagraphStyle("cell_r", parent=CELL_LEFT, alignment=TA_RIGHT)


def _hdr(text, style=HDR_STYLE):
    """Build a Paragraph header cell, converting U+00B2 / U+00B3 to <super> tags."""
    text = text.replace("²", "<super>2</super>").replace("³", "<super>3</super>")
    return Paragraph(text, style)


def _cell(text, style=CELL_LEFT):
    """Build a Paragraph body cell for long text that needs to wrap."""
    # XML-escape for ReportLab Paragraph
    text = (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))
    return Paragraph(text, style)


def _fmt_int(v):
    if v is None or v == "":
        return "-"
    try:
        return f"{int(round(float(v))):,}"
    except (ValueError, TypeError):
        return str(v)


def _fmt_num(v, decimals=2):
    if v is None or v == "":
        return "-"
    try:
        f = float(v)
        if f == int(f):
            return f"{int(f):,}"
        return f"{f:,.{decimals}f}"
    except (ValueError, TypeError):
        return str(v)


def _fmt_date(d=None):
    """Format a date as '18-May-2026'."""
    d = d or datetime.date.today()
    return d.strftime("%d-%b-%Y")


def _parse_calc_date(text: str) -> datetime.date | None:
    """Parse Design Master's calc_date strings like 'March 26, 2026, 2:05 p.m.' → date."""
    if not text:
        return None
    # Strip the time portion if present (everything after the second comma)
    parts = text.split(",")
    if len(parts) >= 2:
        date_str = (parts[0] + "," + parts[1]).strip()
    else:
        date_str = text.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None



def grains_water_difference(report) -> float | None:
    """(Outside humidity ratio - Final room humidity ratio) * 7000 from Zone Default psychrometrics."""
    if not report.psychrometrics:
        return None
    psy = report.psychrometrics[0]  # Zone Default
    outside_w = None
    final_w = None
    for pt in psy.points:
        lbl = (pt.label or "").lower()
        if "outside air" in lbl:
            outside_w = pt.humidity_ratio
        elif "final room conditions" in lbl:
            final_w = pt.humidity_ratio
    if outside_w is None or final_w is None:
        return None
    return (outside_w - final_w) * 7000


def build_ventilation_schedule_pdf(computed, report, out_path: Path,
                                   project_name: str | None = None):
    project_name = project_name or computed.project_name
    doc = SimpleDocTemplate(
        str(out_path), pagesize=letter,
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
        title=f"{project_name} — Ventilation Schedule",
    )

    story = []
    story.append(Paragraph("VENTILATION SCHEDULE", TITLE))
    story.append(Paragraph(computed.mechanical_code, SUBTITLE))

    # Header rows: 4 rows of headers, then data
    # Columns: Room | Room Type | Rp | Pz | Ra | Az | Vbz
    # Room and Room Type are placed in h0 and span down through h3 via SPAN style.
    h0 = [_hdr("Room", HDR_LEFT), _hdr("Room Type", HDR_LEFT),
          _hdr("Outdoor Air, Occupants"), "",
          _hdr("Outdoor Air, Area"), "",
          _hdr("Ventilation Rate")]
    h1 = ["", "", _hdr("Rate"), _hdr("People"), _hdr("Rate"), _hdr("Area"), ""]
    h2 = ["", "",
          _hdr("[CFM/<br/>person]"), "",
          _hdr("[cfm/ft<super>2</super>]"), _hdr("[ft<super>2</super>]"),
          _hdr("[CFM]")]
    h3 = ["", "",
          _hdr("R<sub>p</sub>"), _hdr("P<sub>z</sub>"),
          _hdr("R<sub>a</sub>"), _hdr("A<sub>z</sub>"),
          _hdr("Vbz*")]

    data = [h0, h1, h2, h3]

    total_people = 0
    for r in computed.rooms:
        rp = _fmt_num(r.rp_cfm_per_person, 1) if r.rp_cfm_per_person else "0"
        pz = _fmt_int(r.pz_people) if r.pz_people else "0"
        ra = r.ra_display or "0"   # already-formatted string ("0.06" or "2 ACH")
        az = _fmt_int(r.area_ft2)
        vbz = _fmt_int(r.vent_cfm)
        data.append([_cell(r.room), _cell(r.room_type), rp, pz, ra, az, vbz])
        if r.pz_people:
            total_people += int(r.pz_people)

    # Footer total = sum of per-zone Bldg Ventilation OA (computed.total_vent_oa_cfm),
    # which is itself the sum of per-room Vbz. This keeps the schedule footer and the
    # Building Air Balance in agreement (both Σ Vbz).
    total_oa_rounded = int(round(computed.total_vent_oa_cfm))
    footer = ["", "", "", f"{total_people} Occupants", "", "",
              f"Total Min. OA {total_oa_rounded} CFM"]
    data.append(footer)

    # Column widths: total = 7.5 in usable
    col_widths = [1.3, 2.2, 0.85, 0.7, 0.85, 0.6, 1.0]
    col_widths = [w * inch for w in col_widths]

    n_hdr = 4
    t = Table(data, colWidths=col_widths, repeatRows=n_hdr)
    n_rows = len(data)
    last_row = n_rows - 1
    t.setStyle(TableStyle([
        # Room and Room Type span all 4 header rows (their labels live in h0)
        ("SPAN", (0, 0), (0, 3)),
        ("SPAN", (1, 0), (1, 3)),
        # spans for grouped headers (row 0)
        ("SPAN", (2, 0), (3, 0)),    # "Outdoor Air, Occupants" spans cols 2-3
        ("SPAN", (4, 0), (5, 0)),    # "Outdoor Air, Area" spans cols 4-5
        ("SPAN", (6, 0), (6, 0)),    # "Ventilation Rate" col 6
        # box around header block
        ("BOX", (0, 0), (-1, n_hdr - 1), 0.5, BLACK),
        ("GRID", (0, 0), (-1, n_hdr - 1), 0.5, BLACK),
        ("VALIGN", (0, 0), (-1, n_hdr - 1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, n_hdr - 1), "CENTER"),
        # data rows
        ("FONTNAME", (0, n_hdr), (-1, last_row - 1), "Helvetica"),
        ("FONTSIZE", (0, n_hdr), (-1, last_row - 1), 9),
        ("ALIGN", (0, n_hdr), (1, last_row - 1), "LEFT"),
        ("ALIGN", (2, n_hdr), (-1, last_row - 1), "RIGHT"),
        ("VALIGN", (0, n_hdr), (-1, last_row - 1), "MIDDLE"),
        ("BOX", (0, n_hdr), (-1, last_row - 1), 0.5, BLACK),
        ("LINEBELOW", (0, n_hdr - 1), (-1, n_hdr - 1), 0.5, BLACK),
        ("INNERGRID", (0, n_hdr), (-1, last_row - 1), 0.25, GREY),
        # footer row (last): no grid lines inside, just italics
        ("FONTNAME", (0, last_row), (-1, last_row), "Helvetica"),
        ("FONTSIZE", (0, last_row), (-1, last_row), 9),
        ("ALIGN", (3, last_row), (3, last_row), "CENTER"),
        ("ALIGN", (6, last_row), (6, last_row), "RIGHT"),
        ("LINEABOVE", (0, last_row), (-1, last_row), 0.5, BLACK),
        ("BOX", (0, last_row), (-1, last_row), 0.5, BLACK),
    ]))
    story.append(t)

    doc.build(story)
    return out_path


def build_air_balance_pdf(computed, out_path: Path,
                          project_name: str | None = None,
                          config: ProjectConfig | None = None):
    project_name = project_name or computed.project_name
    show_imc_footnote = config.bldg_exhaust_all_toilet if config else False
    doc = SimpleDocTemplate(
        str(out_path), pagesize=letter,
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
        title=f"{project_name} — Building Air Balance",
    )

    story = []
    story.append(Paragraph("Building Air Balance", TITLE))
    story.append(Spacer(1, 0.05 * inch))

    h1 = [_hdr("Zone", HDR_LEFT), _hdr("Supply"), _hdr("Return"),
          _hdr("Bldg Ventilation"), _hdr("Bldg Exhaust"), _hdr("Air Balance")]
    h2 = ["", _hdr("[cfm]"), _hdr("[cfm]"), _hdr("[cfm]"), _hdr("[cfm]"), _hdr("[cfm]")]
    data = [h1, h2]

    for z in computed.zones:
        data.append([
            _cell(f"  {z.zone_name}"),
            _fmt_int(z.supply_cfm),
            _fmt_int(z.return_cfm),
            _fmt_int(z.vent_oa_cfm),
            "-" if z.bldg_exhaust_cfm == 0 else _fmt_int(z.bldg_exhaust_cfm),
            _fmt_int(z.air_balance_cfm),
        ])

    # Blank padding rows to match reference's white space before totals
    # (keep this in sync with build_air_balance_xlsx in schedule_xlsx.py).
    while len(data) - 2 < 4:
        data.append(["", "", "", "", "", ""])

    data.append([
        "", "", "Totals:",
        _fmt_int(computed.total_vent_oa_cfm),
        "-" if computed.total_bldg_exhaust_cfm == 0 else _fmt_int(computed.total_bldg_exhaust_cfm),
        _fmt_int(computed.total_air_balance_cfm),
    ])

    col_widths = [2.4, 1.0, 1.0, 1.3, 1.0, 1.0]
    col_widths = [w * inch for w in col_widths]

    n_hdr = 2
    n_rows = len(data)
    totals_row = n_rows - 1
    t = Table(data, colWidths=col_widths, repeatRows=n_hdr)
    t.setStyle(TableStyle([
        # full grid in box around the whole table
        ("BOX", (0, 0), (-1, -1), 0.5, BLACK),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, BLACK),
        # headers
        ("VALIGN", (0, 0), (-1, n_hdr - 1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, n_hdr - 1), "CENTER"),
        ("ALIGN", (0, 0), (0, n_hdr - 1), "LEFT"),
        # data
        ("FONTNAME", (0, n_hdr), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, n_hdr), (-1, -1), 10),
        ("ALIGN", (1, n_hdr), (-1, -1), "RIGHT"),
        ("ALIGN", (0, n_hdr), (0, -1), "LEFT"),
        ("VALIGN", (0, n_hdr), (-1, -1), "MIDDLE"),
        # totals row label "Totals:" right-aligned
        ("ALIGN", (2, totals_row), (2, totals_row), "RIGHT"),
    ]))
    story.append(t)

    if show_imc_footnote:
        story.append(Spacer(1, 0.05 * inch))
        note_style = ParagraphStyle("note", fontName="Helvetica", fontSize=9, leading=11,
                                    borderWidth=0.5, borderColor=BLACK,
                                    borderPadding=4)
        story.append(Paragraph(
            "All building exhaust is intermittent toilet/accessory exhaust per IMC Table 403.4.2. "
            "No continuous exhaust.",
            note_style,
        ))

    doc.build(story)
    return out_path


def build_load_summary_cover(computed, report, config, engineer, firm,
                              out_path: Path, project_name: str | None = None):
    project_name = project_name or computed.project_name
    doc = SimpleDocTemplate(
        str(out_path), pagesize=letter,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
        title=f"{project_name} — Load Summary",
    )

    story = []
    story.append(Paragraph("HEATING AND COOLING LOAD SUMMARY SHEET", TITLE))
    story.append(Paragraph(computed.energy_code, SUBTITLE))

    # Engineer block — right-aligned labels, bold values, with underlines below each value
    lic_label = f"{engineer.state_full} Registered Professional Engineer:"
    eng_rows = [
        ["Calculations Performed by:",
         Paragraph(f"<b>{engineer.name}</b>", ParagraphStyle("v", fontName="Helvetica-Bold", fontSize=10))],
        ["Contact:",
         Paragraph(f'<font color="blue"><u>{engineer.email}</u></font>  '
                   f'<b>{engineer.phone}</b>',
                   ParagraphStyle("v", fontName="Helvetica", fontSize=10))],
        [lic_label,
         Paragraph(f"<b>Lic. No.: {computed.license_number}</b>",
                   ParagraphStyle("v", fontName="Helvetica-Bold", fontSize=10))],
        ["Date:",
         Paragraph(f"{_fmt_date(_parse_calc_date(computed.calc_date))}",
                   ParagraphStyle("v", fontName="Helvetica", fontSize=10))],
    ]
    label_style = ParagraphStyle("lbl", fontName="Helvetica-Bold", fontSize=10,
                                 alignment=TA_RIGHT, leading=12)
    eng_data = [[Paragraph(lbl, label_style), val] for lbl, val in eng_rows]

    eng_tbl = Table(eng_data, colWidths=[3.2 * inch, 3.8 * inch])
    eng_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        # underline below the value cell in each row
        ("LINEBELOW", (1, 0), (1, -1), 0.4, BLACK),
    ]))
    story.append(eng_tbl)
    story.append(Spacer(1, 0.2 * inch))

    # Project block — bordered, right-aligned bold labels, left-aligned values
    rh_str = f"{int(computed.indoor_rh)}%" if computed.indoor_rh else "-"
    gwd = grains_water_difference(report)
    gwd_str = f"{gwd:.2f} [grains moisture/lb dry air]" if gwd is not None else "-"
    proj_address = (getattr(config, "project_address", None)
                    or computed.project_address
                    or computed.weather_station)

    proj_rows = [
        ("Project Name", computed.project_name),
        ("Address", proj_address),
        ("Weather Station", computed.weather_station),
        ("Sizing Method", "CLTD"),
        ("Outdoor Dry Bulb", f"{_fmt_int(computed.osa_high_db_f)}° F" if computed.osa_high_db_f else "-"),
        ("Outdoor Wet Bulb", f"{_fmt_int(computed.osa_high_wb_f)}° F" if computed.osa_high_wb_f else "-"),
        ("Indoor Dry Bulb",  f"{_fmt_int(computed.indoor_dry_bulb_f)}° F" if computed.indoor_dry_bulb_f else "-"),
        ("RH", rh_str),
        ("Grains Water Difference", gwd_str),
    ]
    proj_data = [[Paragraph(f"<b>{lbl}</b>",
                            ParagraphStyle("pl", fontName="Helvetica-Bold", fontSize=10,
                                           alignment=TA_RIGHT)),
                  Paragraph(str(val), ParagraphStyle("pv", fontName="Helvetica", fontSize=10))]
                 for lbl, val in proj_rows]

    proj_tbl = Table(proj_data, colWidths=[2.0 * inch, 5.0 * inch])
    proj_tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, BLACK),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, BLACK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(proj_tbl)
    story.append(Spacer(1, 0.05 * inch))

    # Load table
    load_hdr = [
        _hdr("Zone"),
        _hdr("Area<br/>[ft<super>2</super>]"),
        _hdr("Total Cooling<br/>[Btu/h]"),
        _hdr("Total Sensible Gain<br/>[Btu/h]"),
        _hdr("Total Latent Gain<br/>[Btu/h]"),
        _hdr("Total Heating<br/>[Btu/h]"),
    ]
    load_data = [load_hdr]
    for z in computed.zones:
        load_data.append([
            _cell(f"  {z.zone_name}"),
            f"{_fmt_int(z.area_ft2)} ft2",
            _fmt_int(z.cooling_total_btuh),
            _fmt_int(z.cooling_sensible_btuh),
            _fmt_int(z.cooling_latent_btuh),
            _fmt_int(z.heating_btuh),
        ])

    load_tbl = Table(load_data,
                     colWidths=[2.0 * inch, 0.9 * inch, 1.0 * inch,
                                1.1 * inch, 1.0 * inch, 1.0 * inch],
                     repeatRows=1)
    load_tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, BLACK),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, BLACK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 1), (0, -1), "LEFT"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(load_tbl)

    # Firm footer block at bottom of page — using onFirstPage callback
    def _firm_footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 10)
        width, _ = doc_.pagesize
        canvas.drawCentredString(width / 2, 0.55 * inch, firm.line1)
        canvas.drawCentredString(width / 2, 0.40 * inch, firm.line2)
        canvas.restoreState()

    doc.build(story, onFirstPage=_firm_footer, onLaterPages=_firm_footer)
    return out_path
