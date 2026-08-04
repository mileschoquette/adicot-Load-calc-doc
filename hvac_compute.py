"""HVAC Loads pipeline — compute layer.

Turns a parsed HVACReport + ProjectConfig into a ComputedReport (the rows
that populate the Ventilation Schedule / Building Air Balance / Load Summary).
Split out of hvac_pipeline.py; re-exported from there for backward compat.
"""

from __future__ import annotations
import re
import math
from dataclasses import dataclass, field

from hvac_parse import HVACReport, RoomVent, RoomInfoP2, _clean_number


@dataclass
class ProjectConfig:
    toilet_exhaust_cfm: float = 70.0
    ceiling_height_ft: float | None = None     # None = use per-room values from HTML (default)
    bldg_exhaust_all_toilet: bool = False      # M40 — True = add IMC 403.4.2 footnote only (no longer zeros exhaust)
    enable_45_ton_snap: bool = True            # I21
    cfm_per_ton: float = 400.0                 # standard mech rule of thumb
    project_address: str = ""                  # free-text per project (edit per job)


# Canonical space-type definitions: {SOURCE}-{CATEGORY}-{SPACE}
# SOURCE = code reference sheet: FBC (Florida Building Code 403.3.1.1),
#          170 (ASHRAE Std 170 healthcare), 621 (ASHRAE Std 62.1 Table 6.5)
# CATEGORY = bucket within that code
# SPACE    = leaf space name
SPACE_TYPE_TABLE_RAW = [
    # (canonical name, oa_rule, exh_rule)

    # === ASHRAE 170 (healthcare outpatient) — ACH-based outdoor air ===
    ("170-Gen Outpatient-Class 1 Imaging",                  "2 ACH",  None),
    ("170-Gen Outpatient-Dental Treatment",                 "2 ACH",  None),
    ("170-Gen Outpatient-Gen Exam Rm",                      "2 ACH",  None),
    ("170-Gen Outpatient-Lab Wrk Rm",                       "2 ACH",  None),
    ("170-Special Outpatient-Clean Wrk Rm/Storage",         "2 ACH",  None),
    ("170-Special Outpatient-Sterile Proc Clean Wrk Rm (+)", "2 ACH", None),

    # === FBC / Office support spaces — toilets and work rooms ===
    ("FBC-Office-Toilet (50/70)",        None,    "TOILET"),  # uses project's toilet_exhaust_cfm
    ("FBC-Office-Toilet (50)",           None,    "TOILET"),
    ("FBC-Office-Toilet (100)",          None,    "100"),
    ("FBC-Office-Toilet (300)",          None,    "300"),
    ("FBC-Public-Toilet Rooms",          None,    "TOILET"),
    ("FBC-Office-Work Rooms (Copy/Print)", "0.06", "0.5/SF"),

    # === ASHRAE 62.1 Table 6.5 — exhaust-only spaces ===
    ("621-Office-Copy/Print",             None,    "0.5/SF"),
]

# Legacy aliases — old Design Master room-type strings that map to the canonical names
# above. This lets HTMLs exported before the naming change still resolve correctly.
SPACE_TYPE_ALIASES = {
    "170 Gen Outpatient-Class 1 Imaging":                          "170-Gen Outpatient-Class 1 Imaging",
    "170 Gen Outpatient-Dental treat.":                            "170-Gen Outpatient-Dental Treatment",
    "170 Gen Outpatient-Gen Exam Rm":                              "170-Gen Outpatient-Gen Exam Rm",
    "170 Gen Outpatient-Lab Wrk Rm":                               "170-Gen Outpatient-Lab Wrk Rm",
    "170 Special. Outpatient-Clean workroom/storage":              "170-Special Outpatient-Clean Wrk Rm/Storage",
    "170 Special. Outpatient-Sterile Processing Clean wrkrm (+)":  "170-Special Outpatient-Sterile Proc Clean Wrk Rm (+)",
    "Misc-Copy/Print (Exh:0.5)":                                   "621-Office-Copy/Print",
    "FBC Public, Toilet rooms *50/70 Exh":                         "FBC-Public-Toilet Rooms",
    "FBC Public-Toilet rooms (Exh 50/70)":                         "FBC-Public-Toilet Rooms",
    "FBC Toilet *50/70 Exh":                                       "FBC-Office-Toilet (50/70)",
    "FBC Work Rooms, Copy, printing *0.5 Exh":                     "FBC-Office-Work Rooms (Copy/Print)",
    "Misc-Toilet *50/70 Exh":                                      "FBC-Office-Toilet (50/70)",
    "Misc-Toilet (Exh:50)":                                        "FBC-Office-Toilet (50)",
    "Misc-Toilet (Exh:100)":                                       "FBC-Office-Toilet (100)",
    "Misc-Toilet (Exh:300)":                                       "FBC-Office-Toilet (300)",
}

SPACE_TYPE_OVERRIDES = {name: {"oa": oa, "exh": exh}
                        for name, oa, exh in SPACE_TYPE_TABLE_RAW}


def resolve_space_type(name: str) -> str | None:
    """Return the canonical space-type key for a given room-type string.
    Handles both new canonical names and legacy aliases. Returns None if not found."""
    if name in SPACE_TYPE_OVERRIDES:
        return name
    if name in SPACE_TYPE_ALIASES:
        return SPACE_TYPE_ALIASES[name]
    return None



STATE_TABLE = {
    "AR": {"license": "20731",     "mech_code": "2021 International Mechanical Code (IMC)",
                                   "energy_code": "2009 International Energy Conservation Code (IECC)"},
    "FL": {"license": "77100",     "mech_code": "2023 Florida Building Code, Mechanical",
                                   "energy_code": "2023 Florida Building Code, Energy Conservation"},
    "LA": {"license": "PE.0046611","mech_code": "2021 International Mechanical Code (IMC)",
                                   "energy_code": "2021 International Energy Conservation Code (IECC)"},
    "MA": {"license": "59876",     "mech_code": "2015 International Mechanical Code (IMC)",
                                   "energy_code": "2020 Massachusetts Energy Code"},
    "OK": {"license": "32968",     "mech_code": "2018 International Mechanical Code with 2021 OK Amendments",
                                   "energy_code": "2009 International Energy Conservation Code (IECC)"},
    "PA": {"license": "PE098610",  "mech_code": "2018 International Mechanical Code(IMC)",
                                   "energy_code": "2015 International Energy Conservation Code (IECC)"},
    "TX": {"license": "144791",    "mech_code": "2018 International Mechanical Code (IMC)",
                                   "energy_code": "2015 International Energy Conservation Code (IECC)"},
    "WV": {"license": "27173",     "mech_code": "2018 International Mechanical Code (IMC)",
                                   "energy_code": "2015 International Energy Conservation Code (IECC)"},
    "WY": {"license": "19826",     "mech_code": "2021 International Mechanical Code (IMC)",
                                   "energy_code": "2018 International Energy Conservation Code (IECC)"},
}


def extract_state_from_location(loc: str) -> str | None:
    """Replicates Excel: LEFT(RIGHT(B12, LEN(B12)-FIND(", ", B12)), 3) → ' XX' (with leading space).
    The Excel lookup table keys also have the leading space. We strip it and match against
    our 2-letter STATE_TABLE keys.

    Location strings look like 'ORLANDO EXECUTIVE, FL, USA (WMO: 722053), ...'.
    Excel finds the first ', ', takes everything from the comma's space onward, then the first 3 chars: ' FL'.
    """
    if not loc:
        return None
    idx = loc.find(", ")
    if idx < 0:
        return None
    # Take everything from the comma's position onward (Excel RIGHT includes the leading space)
    after = loc[idx + 1:]   # starts with ' '
    raw3 = after[:3]        # e.g. ' FL'
    code_ = raw3.strip().upper()
    return code_ if code_ in STATE_TABLE else None


def excel_ceiling(value: float, significance: float) -> float:
    """Replicates Excel CEILING(value, significance) — round UP to nearest multiple."""
    if significance == 0:
        return 0.0
    return math.ceil(value / significance) * significance


def tonnage_snap(cooling_btuh: float | None, enable_45_snap: bool = True) -> float:
    """Replicates Print-Load!I22:
        IF($I$21=TRUE,
           IF(AND(CEILING(C22/12000,0.5)>4, CEILING(C22/12000,0.5)<=5), 5,
              CEILING(C22/12000,0.5)),
           "")
    Cooling btu/h → equipment tonnage rounded up to nearest 0.5, with (4, 5] → 5.
    """
    if cooling_btuh is None or cooling_btuh <= 0:
        return 0.0
    tons = excel_ceiling(cooling_btuh / 12000, 0.5)
    if enable_45_snap and 4 < tons <= 5:
        return 5.0
    return tons


# A 5-ton unit's supply airflow is trimmed to 1850 CFM rather than the nominal
# 5 × 400 = 2000, to stay below the 2000 CFM code threshold. Every other size
# uses the flat cfm_per_ton rule of thumb — including units above 5 tons
# (e.g. 7.5 ton → 3000), which can't be brought under 2000 anyway.
_FIVE_TON_SUPPLY_CFM = 1850.0


def cfm_from_tons(tons: float, cfm_per_ton: float = 400.0) -> float:
    if cfm_per_ton == 400.0 and round(tons, 3) == 5.0:
        return _FIVE_TON_SUPPLY_CFM
    return tons * cfm_per_ton


# Sanity tests
assert tonnage_snap(47100) == 4.0       # 3.925 → 4.0
assert tonnage_snap(47200) == 4.0       # 3.933 → 4.0
assert tonnage_snap(54000) == 5.0       # 4.5 → 5.0 (snap fires)
assert tonnage_snap(60000) == 5.0       # exactly 5.0
assert tonnage_snap(66000) == 5.5       # snap doesn't apply above 5
assert tonnage_snap(54000, enable_45_snap=False) == 4.5
assert cfm_from_tons(4.0) == 1600.0
assert cfm_from_tons(5.0) == 1850.0     # 5-ton trimmed below the 2000 CFM threshold
assert cfm_from_tons(5.5) == 2200.0     # above 5 tons: nominal 400 CFM/ton

_ACH_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*ACH\s*$", re.I)
_PER_SF_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*/\s*SF\s*$", re.I)
_NUMBER_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*$")


def evaluate_rule(rule: str | None, area_ft2: float, ceiling_ft: float,
                  toilet_exh_cfm: float,
                  supply_cfm: float | None = None) -> float | None:
    """Evaluate an override rule string. Returns CFM or None.

    `supply_cfm` is the room's supply airflow; only consulted for the "ALL" rule
    (exhaust 100% of the air supplied to the room). When the rule is "ALL" and no
    supply is known, returns None (no exhaust applied).
    """
    if rule is None or rule == "":
        return None
    s = str(rule).strip()
    if s.upper() == "TOILET":
        return toilet_exh_cfm
    if s.upper() == "ALL":
        return supply_cfm
    m = _ACH_RE.match(s)
    if m:
        return float(m.group(1)) * ceiling_ft * area_ft2 / 60.0
    m = _PER_SF_RE.match(s)
    if m:
        return float(m.group(1)) * area_ft2
    m = _NUMBER_RE.match(s)
    if m:
        return float(m.group(1))
    return None


# Tests
assert evaluate_rule("2 ACH", 474, 11.5, 70) == 2 * 11.5 * 474 / 60
assert evaluate_rule("0.5/SF", 100, 11.5, 70) == 50.0
assert evaluate_rule("100", 50, 11.5, 70) == 100.0
assert evaluate_rule("TOILET", 52, 9, 70) == 70.0
assert evaluate_rule("ALL", 100, 11.5, 70, supply_cfm=435) == 435.0   # 100% of supply
assert evaluate_rule("ALL", 100, 11.5, 70) is None                    # no supply known


def parse_exhaust_rule(name: str | None) -> str | None:
    """Derive a room's exhaust rule straight from its room-type name tag.

    A room with exhaust always carries a tag in its name; this reads it:
      - "ACH:all" / "Exh:all"     -> exhaust 100% of the room's supply air
      - "<n> ACH" / "ACH:<n>"     -> air changes/hour (takes precedence over Exh)
      - an Exh tag with two slashed numbers, e.g. "50/70" -> toilet rule
        (the displayed value comes from the toilet-exhaust input)
      - "Exh:<n>" with n <= 10    -> <n> CFM per ft²
      - "Exh:<n>" with n  > 10    -> <n> CFM (a fixed per-room total)

    Returns a rule string for evaluate_rule(), or None when the name has no tag.
    """
    if not name:
        return None
    # "all" = exhaust 100% of supply air. The tag is an exh/ach keyword next to the
    # word "all", in either order and with or without a colon:
    #   "exh all", "Exh:all", "exhaust all", "ACH all", "ACH:all", "all Exh", ...
    # Checked first; an explicit "all" wins over every other reading.
    if (re.search(r'\b(?:exhaust|exh|ach)\b\s*:?\s*\ball\b', name, re.I)
            or re.search(r'\ball\b\s*(?:exhaust|exh|ach)\b', name, re.I)):
        return "ALL"
    # ACH overrides Exh — accept "6 ACH" or "ACH:6".
    m = (re.search(r'(\d+(?:\.\d+)?)\s*ACH', name, re.I)
         or re.search(r'ACH[:\s]*(\d+(?:\.\d+)?)', name, re.I))
    if m:
        return f"{m.group(1)} ACH"
    if re.search(r'exh', name, re.I):
        # Slashed pair (e.g. 50/70) is the toilet convention -> use the toilet input.
        if re.search(r'\d+\s*/\s*\d+', name):
            return "TOILET"
        # A single number, before or after the Exh tag: "(Exh:1.0)" or "*0.5 Exh".
        m = (re.search(r'exh[^0-9]*?(\d+(?:\.\d+)?)', name, re.I)
             or re.search(r'(\d+(?:\.\d+)?)\s*exh', name, re.I))
        if m:
            n = float(m.group(1))
            return f"{m.group(1)}/SF" if n <= 10 else m.group(1)
    return None


# Tests — name-tag parsing
assert parse_exhaust_rule("Misc-Janitors closets, trash rooms, recycling (Exh:1.0)") == "1.0/SF"
assert parse_exhaust_rule("Misc-Kitchenettes (cooking) (Exh:0.3)") == "0.3/SF"
assert parse_exhaust_rule("Sports-Gym, sports Arena (Play Area) (Exh:0.5)") == "0.5/SF"
assert parse_exhaust_rule("FBC Public, Toilet rooms *50/70 Exh") == "TOILET"
assert parse_exhaust_rule("Misc-Toilet (Exh:50/70)") == "TOILET"
assert parse_exhaust_rule("Some Room (Exh:100)") == "100"          # >10 -> fixed CFM
assert parse_exhaust_rule("Lab Exhaust 6 ACH") == "6 ACH"          # ACH wins
assert parse_exhaust_rule("Lab (ACH:6) (Exh:0.5)") == "6 ACH"      # ACH overrides Exh
assert parse_exhaust_rule("FBC Offices-Office Spaces") is None     # no tag -> no exhaust
assert parse_exhaust_rule("DENTAL23 Lab/Sterilization (per 170 lab, exh all)") == "ALL"  # real tag: "exh all"
assert parse_exhaust_rule("Paint spray booths (Exh:all)") == "ALL"  # colon form
assert parse_exhaust_rule("Some Room (ACH all)") == "ALL"          # ACH all -> 100%
assert parse_exhaust_rule("Some Room (ACH:all)") == "ALL"          # ACH:all -> 100%
assert parse_exhaust_rule("Booth, exhaust all") == "ALL"           # full word "exhaust"
assert parse_exhaust_rule("All other locker rooms (Exh:0.5)") == "0.5/SF"  # 'all' in name, not the rule
assert parse_exhaust_rule("All other locker rooms") is None        # 'all' in name, no tag
assert evaluate_rule(None, 100, 11.5, 70) is None

@dataclass
class ComputedRoom:
    """One row of the per-room ventilation/exhaust schedule."""
    zone_name: str
    room: str
    room_type: str
    area_ft2: float
    rp_cfm_per_person: float | None
    pz_people: float | None
    ra_cfm_per_ft2: float | None
    ra_display: str            # display text for Ra column: "0.06", "2 ACH", or "0"
    vent_cfm: float            # G column on Print-Ventilation
    vent_source: str           # 'ashrae' | 'override' | 'none'
    exh_rate_label: str        # display label e.g. "70", "0.5/SF" or ""
    exh_cfm: float | None      # J column on Print-Ventilation


@dataclass
class ComputedZone:
    """One row of the Building Air Balance section."""
    zone_name: str
    area_ft2: float
    cooling_total_btuh: float
    cooling_sensible_btuh: float
    cooling_latent_btuh: float
    heating_btuh: float
    supply_cfm: float          # cooling CFM straight from the DM HTML (per-system total)
    return_cfm: float          # same as supply
    vent_oa_cfm: float         # P col, ceiling-of-5
    bldg_exhaust_cfm: float    # S col
    air_balance_cfm: float     # T col = P - S


@dataclass
class ComputedReport:
    project_name: str
    project_address: str       # B88 free-text in workbook; we put project_location here
    weather_station: str       # the full Project Location string
    state_code: str            # 2-letter
    license_number: str
    mechanical_code: str
    energy_code: str
    calc_date: str
    osa_high_db_f: float | None
    osa_high_wb_f: float | None
    osa_low_f: float | None
    indoor_dry_bulb_f: float | None
    indoor_rh: float | None
    rooms: list[ComputedRoom] = field(default_factory=list)
    zones: list[ComputedZone] = field(default_factory=list)
    total_vent_oa_cfm: float = 0.0
    total_bldg_exhaust_cfm: float = 0.0
    total_air_balance_cfm: float = 0.0


def _zone_of_room(report: HVACReport, room_name: str) -> str:
    """Find which zone (LoadTotal location) a room belongs to.

    In the HTML, rooms aren't explicitly tagged with a zone, but RoomVent rows are.
    We use that as the source of truth.
    """
    for rv in report.room_vent:
        if rv.room == room_name:
            return rv.zone_name
    return ""


def _resolve_vent_cfm(rv: RoomVent, area_ft2: float, ceiling_ft: float,
                      cfg: ProjectConfig) -> tuple[float, str]:
    """Per-room ventilation CFM. Returns (cfm, source)."""
    canonical = resolve_space_type(rv.room_type)
    override = SPACE_TYPE_OVERRIDES.get(canonical) if canonical else None
    if override and override["oa"] is not None:
        cfm = evaluate_rule(override["oa"], area_ft2, ceiling_ft, cfg.toilet_exhaust_cfm)
        if cfm is not None:
            return cfm, "override"
    # ASHRAE 62.1: Vbz = Rp*Pz + Ra*Az
    rp = rv.rp_cfm_per_person or 0
    pz = rv.pz_people or 0
    ra = rv.ra_cfm_per_ft2 or 0
    return rp * pz + ra * area_ft2, "ashrae"


def _resolve_exh(rv: RoomVent, area_ft2: float, ceiling_ft: float,
                 cfg: ProjectConfig,
                 supply_cfm: float | None = None) -> tuple[str, float | None]:
    """Per-room exhaust. Returns (rate_label, cfm)."""
    rule = parse_exhaust_rule(rv.room_type)
    if rule is None:
        canonical = resolve_space_type(rv.room_type)
        override = SPACE_TYPE_OVERRIDES.get(canonical) if canonical else None
        rule = override["exh"] if override else None
    if rule is None:
        return "", None
    # Display label: TOILET resolves to the project's toilet_exhaust_cfm number;
    # ALL (100% of supply) shows "100%".
    if rule == "TOILET":
        label = str(int(cfg.toilet_exhaust_cfm))
    elif rule == "ALL":
        label = "100%"
    else:
        label = rule
    cfm = evaluate_rule(rule, area_ft2, ceiling_ft, cfg.toilet_exhaust_cfm,
                        supply_cfm=supply_cfm)
    return label, cfm


def _build_room_zone_map(report: HVACReport) -> dict[str, str]:
    """Build a dict mapping each room name → its zone name.

    Source: the cooling_load_room or load_total_room tables, which are structured as
    [Zone row, Room row, Room row, ..., Zone row, Room row, ...]. The "Room " prefix
    on the location distinguishes a room from a zone.
    """
    mapping: dict[str, str] = {}
    current_zone = None
    for entry in report.load_total_room:
        loc = entry.location
        if loc.startswith("Room "):
            room_name = loc[len("Room "):]
            if current_zone is not None:
                mapping[room_name] = current_zone
        else:
            current_zone = loc
    return mapping


def _parse_vent_rule(rule_text: str) -> tuple[float | None, float | None, str]:
    """Parse a Ventilation rule string from Room Info Part 1.

    Returns (rp, ra, display_rule) where rp/ra are extracted numeric values
    or None if the rule is ACH-based / non-parseable, and display_rule is the
    string to show in the Ra column of the vent schedule (e.g. "2 ACH" preserved).

    Examples:
      "5 CFM / person 0.06 CFM / ft 2"  -> rp=5.0, ra=0.06, display="0.06"
      "2 AC / hour"                     -> rp=None, ra=None, display="2 ACH"
      "0.06 CFM / ft 2"                 -> rp=None, ra=0.06, display="0.06"
      "5 CFM / person"                  -> rp=5.0, ra=None, display="0"
      "Direct"                          -> rp=None, ra=None, display="0"
      ""                                -> rp=None, ra=None, display="0"
    """
    if not rule_text:
        return None, None, "0"
    text = rule_text.strip()
    # ACH-based rule
    m_ach = re.search(r"(-?\d+(?:\.\d+)?)\s*AC(?:H|\s*/\s*hour)", text, re.I)
    if m_ach:
        return None, None, f"{m_ach.group(1)} ACH"
    # Rp: "5 CFM / person"
    rp = None
    m_rp = re.search(r"(-?\d+(?:\.\d+)?)\s*CFM\s*/\s*person", text, re.I)
    if m_rp:
        rp = float(m_rp.group(1))
    # Ra: "0.06 CFM / ft 2" (the space before "2" comes from BS4 splitting <sup>2</sup>)
    ra = None
    m_ra = re.search(r"(-?\d+(?:\.\d+)?)\s*CFM\s*/\s*ft", text, re.I)
    if m_ra:
        ra = float(m_ra.group(1))
    display = f"{ra:g}" if ra is not None else "0"
    return rp, ra, display


def compute(report: HVACReport, cfg: ProjectConfig) -> ComputedReport:
    # Project / state resolution
    state = extract_state_from_location(report.project.project_location) or ""
    state_info = STATE_TABLE.get(state, {})

    # Index room_vent rows by room name (where they exist — only critical-zone rooms)
    rv_by_room: dict[str, RoomVent] = {rv.room: rv for rv in report.room_vent}
    room_zone_map = _build_room_zone_map(report)

    out = ComputedReport(
        project_name=report.project.project_name,
        project_address=report.project.project_location,
        weather_station=report.project.project_location,
        state_code=state,
        license_number=state_info.get("license", ""),
        mechanical_code=state_info.get("mech_code", ""),
        energy_code=state_info.get("energy_code", ""),
        calc_date=report.project.calc_date,
        osa_high_db_f=report.project.osa_high_db_f,
        osa_high_wb_f=report.project.osa_high_wb_f,
        osa_low_f=report.project.osa_low_f,
        indoor_dry_bulb_f=report.project.default_cooling_temp_f,
        indoor_rh=report.project.default_relative_humidity_pct,
    )

    # Per-room ventilation/exhaust — iterate full room list from Part 1
    # Build a Part 2 lookup for Pz fallback
    p2_by_room: dict[str, RoomInfoP2] = {r.number: r for r in report.rooms_p2}
    # Per-room supply CFM (the "Supply CFM" column), for the "exhaust all" rule.
    # Supply Air locations look like "Room <name>"; strip the prefix to match r1.number.
    supply_by_room: dict[str, float | None] = {}
    for s in report.supply_air:
        loc = re.sub(r'^\s*Room\s+', '', (s.location or '').strip(), flags=re.I)
        if loc:
            supply_by_room[loc] = s.required_supply_cfm
    for r1 in report.rooms_p1:
        zone = room_zone_map.get(r1.number, "")
        # Parse the Part 1 rule string to get displayable Rp/Ra
        rp_parsed, ra_parsed, ra_display = _parse_vent_rule(r1.ventilation_rule)
        # Authoritative Vbz: from room_vent if present (precise area + workbook calc), else from Part 1
        rv = rv_by_room.get(r1.number)
        if rv is not None:
            area = rv.az_ft2 if rv.az_ft2 is not None else (r1.area_ft2 or 0.0)
            rp = rv.rp_cfm_per_person if rv.rp_cfm_per_person is not None else rp_parsed
            pz = rv.pz_people
            ra = ra_parsed
            vbz = rv.vbz_cfm if rv.vbz_cfm is not None else (r1.vbz_cfm or 0.0)
        else:
            area = r1.area_ft2 or 0.0
            rp = rp_parsed
            pz = None
            ra = ra_parsed
            vbz = r1.vbz_cfm or 0.0

        # Fallback Pz from Part 2's "people" string (e.g. "1 person", "9 people")
        if pz is None or pz == 0:
            p2 = p2_by_room.get(r1.number)
            if p2 and p2.people:
                pz_parsed = _clean_number(p2.people)
                if pz_parsed is not None and pz_parsed > 0:
                    pz = pz_parsed

        # Override application: if a SPACE_TYPE_OVERRIDE exists, recompute Vbz from the rule
        ceiling = r1.ceiling_height_ft or 0.0
        canonical = resolve_space_type(r1.name)
        override = SPACE_TYPE_OVERRIDES.get(canonical) if canonical else None
        if override and override["oa"] is not None:
            override_cfm = evaluate_rule(override["oa"], area, ceiling, cfg.toilet_exhaust_cfm)
            if override_cfm is not None:
                vbz = override_cfm

        # Exhaust resolution — read the exhaust tag straight from the room-type
        # name (ACH / Exh per-SF / Exh fixed / toilet 50-70). Any room with a tag
        # gets exhaust, so the building exhaust isn't limited to toilets. Fall back
        # to the hardcoded space-type table only when the name carries no tag.
        exh_label = ""
        exh_cfm = None
        rule = parse_exhaust_rule(r1.name)
        if rule is None and override and override["exh"] is not None:
            rule = override["exh"]
        if rule is not None:
            room_supply = supply_by_room.get(r1.number)
            if rule == "TOILET":
                exh_label = str(int(cfg.toilet_exhaust_cfm))
            elif rule == "ALL":
                exh_label = "100%"
            else:
                exh_label = rule
            exh_cfm = evaluate_rule(rule, area, ceiling, cfg.toilet_exhaust_cfm,
                                    supply_cfm=room_supply)

        out.rooms.append(ComputedRoom(
            zone_name=zone,
            room=r1.number,
            room_type=r1.name,
            area_ft2=area,
            rp_cfm_per_person=rp,
            pz_people=pz,
            ra_cfm_per_ft2=ra,
            ra_display=ra_display,
            vent_cfm=vbz,
            vent_source="override" if override and override["oa"] else "html",
            exh_rate_label=exh_label,
            exh_cfm=exh_cfm,
        ))

    # Per-zone roll-up (from Load Total Summary - System rows)
    for lt in report.load_total_system:
        if (lt.cool_total_btuh in (0, None)) and (lt.area_ft2 in (0, None)):
            continue
        # Supply airflow comes straight from the Design Master HTML (the per-system
        # cooling CFM), NOT from nominal tonnage. The tonnage snap is only a rough
        # equipment-size hint and isn't always what we actually specify.
        supply_cfm = lt.cool_cfm or 0.0

        # Bldg Ventilation OA per zone = sum of per-room breathing-zone OA (Vbz),
        # matching the per-room Ventilation Schedule and the signed deliverables.
        # Do NOT substitute the system-level Vot here: zones sharing one system
        # all carry the same Vot, which collapses the per-zone split (e.g. both
        # zones showing 195 instead of 174 / 212). And do NOT round to the
        # nearest 5 — the schedule reports the raw Vbz sum (174, not 175).
        zone_room_cfms = [r.vent_cfm for r in out.rooms if r.zone_name == lt.location]
        vent_oa = sum(zone_room_cfms)

        zone_room_exhs = [r.exh_cfm or 0.0 for r in out.rooms if r.zone_name == lt.location]
        # Bldg Exhaust always reflects the actual computed exhaust (driven by the
        # toilet-exhaust input + per-room rules); a zone with no exhaust shows "-".
        # The bldg_exhaust_all_toilet checkbox is INDEPENDENT — it only adds the
        # IMC 403.4.2 footnote, it no longer zeros this column.
        bldg_exh = excel_ceiling(sum(zone_room_exhs), 5)

        out.zones.append(ComputedZone(
            zone_name=lt.location,
            area_ft2=lt.area_ft2 or 0.0,
            cooling_total_btuh=lt.cool_total_btuh or 0.0,
            cooling_sensible_btuh=lt.cool_sensible_btuh or 0.0,
            cooling_latent_btuh=lt.cool_latent_btuh or 0.0,
            heating_btuh=lt.heat_btuh or 0.0,
            supply_cfm=supply_cfm,
            return_cfm=supply_cfm,
            vent_oa_cfm=vent_oa,
            bldg_exhaust_cfm=bldg_exh,
            air_balance_cfm=vent_oa - bldg_exh,
        ))

    out.total_vent_oa_cfm = sum(z.vent_oa_cfm for z in out.zones)
    out.total_bldg_exhaust_cfm = sum(z.bldg_exhaust_cfm for z in out.zones)
    out.total_air_balance_cfm = sum(z.air_balance_cfm for z in out.zones)

    return out


@dataclass
class EngineerInfo:
    name: str = "Adrienne Gould-Choquette"
    email: str = "agc@adicot.com"
    phone: str = "(804-787-0468)"
    state_full: str = "Florida"


@dataclass
class FirmInfo:
    line1: str = "Adicot, Inc. | Professional Engineering Services"
    line2: str = "1 Devonshire Pl PH 102, Boston, MA 02109 | www.adicot.com"


def apply_zone_overrides(computed: ComputedReport, overrides: dict) -> ComputedReport:
    """Apply zone display overrides: rename, override supply_cfm, merge zones.

    overrides format: {html_zone_name: {display_name?, supply_cfm?, merge_with?}}
    Returns a NEW ComputedReport (does not mutate input).
    """
    import copy
    if not overrides:
        return computed

    new = copy.deepcopy(computed)

    # First pass: handle merges. Build a map of html_zone → target_zone (where it merges to).
    merge_map: dict[str, str] = {}
    for src_zone, ov in overrides.items():
        target = ov.get("merge_with")
        if target:
            merge_map[src_zone] = target

    # Walk zones in HTML order, merge as we go
    merged: dict[str, ComputedZone] = {}
    order: list[str] = []
    for z in new.zones:
        target_name = merge_map.get(z.zone_name, z.zone_name)
        if target_name in merged:
            # merge values
            m = merged[target_name]
            m.area_ft2 += z.area_ft2
            m.cooling_total_btuh += z.cooling_total_btuh
            m.cooling_sensible_btuh += z.cooling_sensible_btuh
            m.cooling_latent_btuh += z.cooling_latent_btuh
            m.heating_btuh += z.heating_btuh
            m.vent_oa_cfm += z.vent_oa_cfm
            m.bldg_exhaust_cfm += z.bldg_exhaust_cfm
            m.air_balance_cfm += z.air_balance_cfm
            # Supply is the HTML per-system cooling CFM, so summing the merged
            # zones' values is the correct merged supply airflow.
            m.supply_cfm += z.supply_cfm
            m.return_cfm = m.supply_cfm
        else:
            z.zone_name = target_name  # adopt the target name
            merged[target_name] = z
            order.append(target_name)

    # Second pass: apply per-zone display_name / tons / supply_cfm overrides
    for html_name, ov in overrides.items():
        # Find which (possibly merged) entry corresponds
        target_name = merge_map.get(html_name, html_name)
        z = merged.get(target_name)
        if z is None:
            continue
        if "display_name" in ov:
            z.zone_name = ov["display_name"]
            # Also rename rooms' zone_name so per-room rollup remains consistent
            for r in new.rooms:
                if r.zone_name == html_name or r.zone_name == target_name:
                    r.zone_name = ov["display_name"]
            # Update the merged dict key
            merged[ov["display_name"]] = z
            if target_name in merged and target_name != ov["display_name"]:
                del merged[target_name]
            order = [ov["display_name"] if x == target_name else x for x in order]
        if "supply_cfm" in ov:
            z.supply_cfm = float(ov["supply_cfm"])
            z.return_cfm = z.supply_cfm

    # Rebuild the zones list in order
    new.zones = [merged[name] for name in order if name in merged]
    new.total_vent_oa_cfm = sum(z.vent_oa_cfm for z in new.zones)
    new.total_bldg_exhaust_cfm = sum(z.bldg_exhaust_cfm for z in new.zones)
    new.total_air_balance_cfm = sum(z.air_balance_cfm for z in new.zones)
    return new
