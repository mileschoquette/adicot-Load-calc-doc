"""HVAC Loads pipeline — extracted from parse_dm_hvac.ipynb.

Library module imported by app.py (Flask). Call build_all_pdfs(...) to produce
the three deliverable PDFs from a Design Master HTML export.
"""

from __future__ import annotations
import json
import re
import math
from dataclasses import dataclass, field, asdict
from pathlib import Path

from bs4 import BeautifulSoup, Tag



@dataclass
class ProjectInfo:
    project_name: str = ""
    project_location: str = ""
    default_heating_temp_f: float | None = None
    default_cooling_temp_f: float | None = None
    default_relative_humidity_pct: float | None = None
    heating_sf_room_pct: float | None = None
    heating_sf_vent_pct: float | None = None
    cooling_sf_room_pct: float | None = None
    cooling_sf_vent_pct: float | None = None
    floor_slab_heat_loss_coef: float | None = None
    calc_date: str = ""
    osa_low_f: float | None = None
    osa_daily_range_f: float | None = None
    latitude_deg: float | None = None
    elevation_ft: float | None = None
    osa_high_db_f: float | None = None
    osa_high_wb_f: float | None = None
    osa_high_month: str = ""


@dataclass
class RoofType:
    name: str
    u_value: float
    ashrae_type: int | None
    color: str
    description: str


@dataclass
class Roof:
    location: str
    type_name: str
    area_ft2: float


@dataclass
class WallType:
    name: str
    u_value: float
    ashrae_type: int | None
    color: str
    description: str


@dataclass
class Wall:
    room_number: str
    length_ft: float
    height_ft: float
    area_ft2: float
    type_name: str
    facing_direction: str
    on_perimeter: str


@dataclass
class DoorType:
    name: str
    u_value: float
    ashrae_type: int | None
    color: str
    description: str


@dataclass
class Door:
    room_number: str
    area_ft2: float
    type_name: str
    facing_direction: str


@dataclass
class GlassType:
    name: str
    u_value: float
    shgc: float
    description: str


@dataclass
class Glass:
    room_number: str
    area_ft2: float
    type_name: str
    facing_direction: str
    shaded: bool


@dataclass
class RoomInfoP1:
    number: str
    name: str                                  # this is actually the room TYPE in Design Master's export
    area_ft2: float | None
    ceiling_height_ft: float | None
    ventilation_rule: str                       # e.g. "2 AC / hour" or "5 CFM / person 0.06 CFM / ft 2"
    ventilation_cfm_text: str                   # e.g. "33 CFM" or "0 CFM 18 CFM" (sum of Rp×Pz and Ra×Az)
    infiltration_rule: str
    cooling_temp: str
    heating_temp: str
    relative_humidity: str

    @property
    def vbz_cfm(self) -> float | None:
        """Resolved breathing-zone OA in CFM. Sums the numbers in ventilation_cfm_text."""
        import re
        nums = re.findall(r"-?\d+(?:\.\d+)?", self.ventilation_cfm_text or "")
        if not nums:
            return None
        return sum(float(n) for n in nums)


@dataclass
class RoomInfoP2:
    number: str
    lighting_load: str
    equipment_sensible: str
    equipment_latent: str
    people: str
    sensible_per_person: str
    latent_per_person: str
    glass_zone_type: str


@dataclass
class SupplyAirRow:
    location: str
    current_supply_cfm: float | None
    required_supply_cfm: float | None
    cooling_peak: str
    cooling_supply_temp_f: float | None
    cooling_sensible_load_btuh: float | None
    cooling_supply_cfm: float | None
    cooling_osa_cfm: float | None
    cooling_osa_pct: float | None
    heating_temp_diff: str
    heating_load_btuh: float | None
    heating_supply_cfm: float | None
    heating_osa_cfm: float | None
    heating_osa_pct: float | None


@dataclass
class SystemVentParams:
    """ASHRAE 62.1 Section-6 system-level ventilation values."""
    zone_name: str
    vps_cfm: float | None = None
    ez: float | None = None
    xs: float | None = None
    ep: float | None = None
    d: float | None = None
    er: float | None = None
    vou_cfm: float | None = None
    fa: float | None = None
    ev: float | None = None
    fb: float | None = None
    vot_cfm: float | None = None
    fc: float | None = None


@dataclass
class RoomVent:
    """Per-room ventilation row (ASHRAE 62.1)."""
    zone_name: str
    room: str
    room_type: str
    rp_cfm_per_person: float | None
    pz_people: float | None
    rp_pz_cfm: float | None
    ra_cfm_per_ft2: float | None
    az_ft2: float | None
    ra_az_cfm: float | None
    vbz_cfm: float | None
    voz_cfm: float | None
    vdz_cfm: float | None
    zd: float | None
    evz: float | None

@dataclass
class CoolingLoadSystem:
    location: str
    peak_month: str
    peak_time: str
    roof_btuh: float | None
    roof_pct: float | None
    wall_btuh: float | None
    wall_pct: float | None
    glass_btuh: float | None
    glass_pct: float | None
    vent_sensible_btuh: float | None
    vent_sensible_pct: float | None
    vent_latent_btuh: float | None
    vent_latent_pct: float | None
    infil_sensible_btuh: float | None
    infil_sensible_pct: float | None
    infil_latent_btuh: float | None
    infil_latent_pct: float | None


@dataclass
class CoolingLoadRoom:
    location: str
    peak: str
    roof_btuh: float | None
    roof_pct: float | None
    wall_btuh: float | None
    wall_pct: float | None
    glass_btuh: float | None
    glass_pct: float | None
    lighting_btuh: float | None
    lighting_pct: float | None
    equipment_sensible_btuh: float | None
    equipment_sensible_pct: float | None
    equipment_latent_btuh: float | None
    equipment_latent_pct: float | None
    people_sensible_btuh: float | None
    people_sensible_pct: float | None
    people_latent_btuh: float | None
    people_latent_pct: float | None
    infil_sensible_btuh: float | None
    infil_sensible_pct: float | None
    infil_latent_btuh: float | None
    infil_latent_pct: float | None


@dataclass
class HeatingLoad:
    location: str
    roof_btuh: float | None
    roof_pct: float | None
    wall_btuh: float | None
    wall_pct: float | None
    glass_btuh: float | None
    glass_pct: float | None
    slab_btuh: float | None
    slab_pct: float | None
    vent_btuh: float | None
    vent_pct: float | None
    infil_btuh: float | None
    infil_pct: float | None


@dataclass
class LoadTotal:
    """Used for both 'Load Total Summary - System' and '- Room'."""
    location: str
    area_ft2: float | None
    cool_cfm: float | None
    cool_peak_month: str
    cool_peak_time: str
    cool_total_btuh: float | None
    cool_sensible_btuh: float | None
    cool_latent_btuh: float | None
    cool_total_tons: float | None
    cool_sensible_tons: float | None
    cool_latent_tons: float | None
    cool_ft2_per_ton: float | None
    cool_cfm_per_ton: float | None
    cool_cfm_per_ft2: float | None
    heat_cfm: float | None
    heat_btuh: float | None
    heat_kw: float | None
    heat_cfm_per_ft2: float | None


@dataclass
class PsychrometricPoint:
    label: str
    airflow_cfm: float | None
    dry_bulb_f: float | None
    wet_bulb_f: float | None
    humidity_ratio: float | None
    total_btuh: float | None
    sensible_btuh: float | None
    latent_btuh: float | None


@dataclass
class Psychrometrics:
    zone_name: str
    points: list[PsychrometricPoint] = field(default_factory=list)


@dataclass
class HVACReport:
    project: ProjectInfo = field(default_factory=ProjectInfo)
    roof_types: list[RoofType] = field(default_factory=list)
    roofs: list[Roof] = field(default_factory=list)
    wall_types: list[WallType] = field(default_factory=list)
    walls: list[Wall] = field(default_factory=list)
    door_types: list[DoorType] = field(default_factory=list)
    doors: list[Door] = field(default_factory=list)
    glass_types: list[GlassType] = field(default_factory=list)
    glass: list[Glass] = field(default_factory=list)
    rooms_p1: list[RoomInfoP1] = field(default_factory=list)
    rooms_p2: list[RoomInfoP2] = field(default_factory=list)
    supply_air: list[SupplyAirRow] = field(default_factory=list)
    system_vent_params: list[SystemVentParams] = field(default_factory=list)
    room_vent: list[RoomVent] = field(default_factory=list)
    cooling_load_system: list[CoolingLoadSystem] = field(default_factory=list)
    cooling_load_room: list[CoolingLoadRoom] = field(default_factory=list)
    heating_load: list[HeatingLoad] = field(default_factory=list)
    load_total_system: list[LoadTotal] = field(default_factory=list)
    load_total_room: list[LoadTotal] = field(default_factory=list)
    psychrometrics: list[Psychrometrics] = field(default_factory=list)


def _txt(cell) -> str:
    if cell is None:
        return ""
    return cell.get_text(" ", strip=True).replace("\xa0", " ")


def _clean_number(s) -> float | None:
    if s is None:
        return None
    s = str(s).strip()
    if not s or s == "-":
        return None
    cleaned = s.replace(",", "")
    m = re.search(r"-?(?:\d+(?:\.\d+)?|\.\d+)", cleaned)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _clean_int(s) -> int | None:
    f = _clean_number(s)
    return int(f) if f is not None else None


def _ft_inches(s) -> float | None:
    if not s:
        return None
    m = re.search(r"(-?\d+)'\s*-?\s*(\d+)\"?", str(s))
    if m:
        return int(m.group(1)) + int(m.group(2)) / 12.0
    return _clean_number(s)


def _data_cells(row) -> list:
    """Cells carrying data: 'otherData' for normal rows, 'boldData' for critical zones."""
    return row.find_all("td", class_=lambda c: c in ("otherData", "boldData"))


def _data_rows(table) -> list:
    return [r for r in table.find_all("tr") if _data_cells(r)]


def _simple_rows(table) -> list[list[str]]:
    return [[_txt(c) for c in _data_cells(r)] for r in _data_rows(table)]


assert _clean_number("1,600 CFM") == 1600.0
assert _clean_number("93° F") == 93.0
assert _clean_number("-") is None
assert _ft_inches("19'-2\"") == 19 + 2/12

def parse_project_info(table) -> ProjectInfo:
    info = ProjectInfo()
    pairs: list[tuple[str, str]] = []
    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"])
        i = 0
        while i < len(cells):
            c = cells[i]
            klass = c.get("class", [])
            if "project" in klass:
                label = _txt(c).rstrip(":").strip()
                j = i + 1
                while j < len(cells):
                    cj = cells[j]
                    if "otherData" in cj.get("class", []) or "boldData" in cj.get("class", []):
                        pairs.append((label, _txt(cj)))
                        i = j
                        break
                    j += 1
            i += 1

    def find(needle):
        for k, v in pairs:
            if needle.lower() in k.lower():
                return v
        return None

    info.project_name = find("Project Name") or ""
    info.project_location = find("Project Location") or ""
    info.default_heating_temp_f = _clean_number(find("Default Heating Temperature"))
    info.default_cooling_temp_f = _clean_number(find("Default Cooling Temperature"))
    info.default_relative_humidity_pct = _clean_number(find("Default Relative Humidity"))
    info.heating_sf_room_pct = _clean_number(find("Heating Safety Factor (Room)"))
    info.heating_sf_vent_pct = _clean_number(find("Heating Safety Factor (Ventilation)"))
    info.cooling_sf_room_pct = _clean_number(find("Cooling Safety Factor (Room)"))
    info.cooling_sf_vent_pct = _clean_number(find("Cooling Safety Factor (Ventilation)"))
    info.floor_slab_heat_loss_coef = _clean_number(find("Floor Slab Heat Loss"))
    info.calc_date = find("Calculation Date") or ""
    info.osa_low_f = _clean_number(find("OSA Low"))
    info.osa_daily_range_f = _clean_number(find("OSA Daily Range"))
    info.latitude_deg = _clean_number(find("Latitude"))
    elev = find("Elevation")
    if elev:
        info.elevation_ft = _ft_inches(elev) or _clean_number(elev)
    months = ("January","February","March","April","May","June",
              "July","August","September","October","November","December")
    for row in table.find_all("tr"):
        ths = row.find_all("th", class_="project")
        if ths and _txt(ths[0]) in months:
            info.osa_high_month = _txt(ths[0])
            tds = _data_cells(row)
            if len(tds) >= 2:
                info.osa_high_db_f = _clean_number(_txt(tds[0]))
                info.osa_high_wb_f = _clean_number(_txt(tds[1]))
            break
    return info


def parse_roof_types(t):
    return [RoofType(c[0], _clean_number(c[1]) or 0.0, _clean_int(c[2]), c[3], c[4])
            for c in _simple_rows(t) if len(c) >= 5]

def parse_roofs(t):
    return [Roof(c[0], c[1], _clean_number(c[2]) or 0.0)
            for c in _simple_rows(t) if len(c) >= 3]

def parse_wall_types(t):
    return [WallType(c[0], _clean_number(c[1]) or 0.0, _clean_int(c[2]), c[3], c[4])
            for c in _simple_rows(t) if len(c) >= 5]

def parse_walls(t):
    out = []
    for c in _simple_rows(t):
        if len(c) >= 6:
            out.append(Wall(
                room_number=c[0],
                length_ft=_ft_inches(c[1]) or 0.0,
                height_ft=_ft_inches(c[2]) or 0.0,
                area_ft2=_clean_number(c[3]) or 0.0,
                type_name=c[4],
                facing_direction=c[5],
                on_perimeter=c[6] if len(c) > 6 else "",
            ))
    return out

def parse_door_types(t):
    return [DoorType(c[0], _clean_number(c[1]) or 0.0, _clean_int(c[2]), c[3], c[4])
            for c in _simple_rows(t) if len(c) >= 5]

def parse_doors(t):
    return [Door(c[0], _clean_number(c[1]) or 0.0, c[2], c[3])
            for c in _simple_rows(t) if len(c) >= 4]

def parse_glass_types(t):
    return [GlassType(c[0], _clean_number(c[1]) or 0.0, _clean_number(c[2]) or 0.0, c[3])
            for c in _simple_rows(t) if len(c) >= 4]

def parse_glass(t):
    out = []
    for c in _simple_rows(t):
        if len(c) >= 4:
            out.append(Glass(
                room_number=c[0],
                area_ft2=_clean_number(c[1]) or 0.0,
                type_name=c[2],
                facing_direction=c[3],
                shaded=(c[4].strip().lower() == "x") if len(c) > 4 else False,
            ))
    return out


def parse_room_info_p1(t):
    out = []
    for c in _simple_rows(t):
        if len(c) < 8:
            continue
        # Column layout in Design Master's Room Info Part 1:
        #   c0=Number  c1=Name(=Type)  c2=Area  c3=CeilingHeight
        #   c4=Vent rule  c5=Vent cooling CFM  c6=heating="Same as cooling"  c7=Vent heating CFM
        #   c8=Infil rule  c9=Infil cooling CFM  c10="Same as cooling"  c11=Infil heating CFM
        #   c-3,c-2,c-1 = cooling temp / heating temp / relative humidity
        out.append(RoomInfoP1(
            number=c[0],
            name=c[1] if len(c) > 1 else "",
            area_ft2=_clean_number(c[2]) if len(c) > 2 else None,
            ceiling_height_ft=_ft_inches(c[3]) if len(c) > 3 else None,
            ventilation_rule=c[4] if len(c) > 4 else "",
            ventilation_cfm_text=c[5] if len(c) > 5 else "",
            infiltration_rule=c[8] if len(c) > 8 else "",
            cooling_temp=c[-3],
            heating_temp=c[-2],
            relative_humidity=c[-1],
        ))
    return out


def parse_room_info_p2(t):
    """Parse Room Info Part 2. Column count varies depending on whether the optional
    'X ft 2 / person' density column is included for that row. We count from the right:
        c[-1]   = glass_zone_type ("C")
        c[-2]   = latent btuh/person ("475")
        c[-3]   = sensible btuh/person ("275")
        c[-4]   = people count ("1 person" or "0 people")
        c[-5]   = density ("200 ft 2 / person" or "") - optional
    Lighting + equipment columns are at the left and we don't critically need them here.
    """
    out = []
    for c in _simple_rows(t):
        if len(c) < 6:
            continue
        out.append(RoomInfoP2(
            number=c[0],
            lighting_load=c[1] if len(c) > 1 else "",
            equipment_sensible=c[2] if len(c) > 2 else "",
            equipment_latent=c[3] if len(c) > 3 else "",
            people=c[-4] if len(c) >= 4 else "",
            sensible_per_person=c[-3] if len(c) >= 3 else "",
            latent_per_person=c[-2] if len(c) >= 2 else "",
            glass_zone_type=c[-1] if c else "",
        ))
    return out


def parse_supply_air(t):
    out = []
    for c in _simple_rows(t):
        if len(c) < 6:
            continue
        def at(i): return c[i] if i < len(c) else ""
        out.append(SupplyAirRow(
            location=at(0),
            current_supply_cfm=_clean_number(at(1)),
            required_supply_cfm=_clean_number(at(2)),
            cooling_peak=at(3),
            cooling_supply_temp_f=_clean_number(at(4)),
            cooling_sensible_load_btuh=_clean_number(at(5)),
            cooling_supply_cfm=_clean_number(at(6)),
            cooling_osa_cfm=_clean_number(at(7)),
            cooling_osa_pct=_clean_number(at(8)),
            heating_temp_diff=at(9),
            heating_load_btuh=_clean_number(at(10)),
            heating_supply_cfm=_clean_number(at(11)),
            heating_osa_cfm=_clean_number(at(12)),
            heating_osa_pct=_clean_number(at(13)),
        ))
    return out


_VENT_LABEL_PATTERNS = [
    ("vps_cfm",  re.compile(r"System Primary Airflow", re.I)),
    ("xs",       re.compile(r"Average Outdoor Air Fraction", re.I)),
    ("d",        re.compile(r"Occupant Diversity", re.I)),
    ("vou_cfm",  re.compile(r"Uncorrected Air Intake", re.I)),
    ("ev",       re.compile(r"System Ventilation Efficiency", re.I)),
    ("vot_cfm",  re.compile(r"Outdoor Air Intake", re.I)),
    ("ez",       re.compile(r"Zone Air Distribution Effectiveness", re.I)),
    ("ep",       re.compile(r"Primary Air Fraction", re.I)),
    ("er",       re.compile(r"Secondary Air Fraction", re.I)),
    ("fa",       re.compile(r"Fraction of Supply Air to Zone from Outside Zone", re.I)),
    ("fb",       re.compile(r"Fraction of Supply Air to Zone from Fully Mixed", re.I)),
    ("fc",       re.compile(r"Fraction of Outdoor Air to Zone from Outside Zone", re.I)),
]


def _extract_vent_params(table, zone_name: str) -> SystemVentParams:
    p = SystemVentParams(zone_name=zone_name)
    cells = table.find_all("td", class_=lambda c: c in ("otherData", "boldData"))
    texts = [_txt(c) for c in cells]
    i = 0
    while i < len(texts):
        label = texts[i]
        for attr, pat in _VENT_LABEL_PATTERNS:
            if pat.search(label):
                j = i + 1
                while j < len(texts) and not texts[j]:
                    j += 1
                if j < len(texts):
                    setattr(p, attr, _clean_number(texts[j]))
                break
        i += 1
    return p


def _extract_room_vent_rows(table, zone_name: str) -> list[RoomVent]:
    out = []
    for row in table.find_all("tr"):
        cells = row.find_all("td", class_=lambda c: c in ("otherData", "boldData"))
        if len(cells) < 13:
            continue
        texts = [_txt(c) for c in cells]
        if _clean_number(texts[0]) is not None:
            continue
        nums = [_clean_number(t) for t in texts[2:13]]
        if sum(n is not None for n in nums) < 6:
            continue
        out.append(RoomVent(
            zone_name=zone_name,
            room=texts[0],
            room_type=texts[1],
            rp_cfm_per_person=nums[0],
            pz_people=nums[1],
            rp_pz_cfm=nums[2],
            ra_cfm_per_ft2=nums[3],
            az_ft2=nums[4],
            ra_az_cfm=nums[5],
            vbz_cfm=nums[6],
            voz_cfm=nums[7],
            vdz_cfm=nums[8],
            zd=nums[9],
            evz=nums[10],
        ))
    return out


def parse_vent_table(table, zone_name: str):
    return _extract_vent_params(table, zone_name), _extract_room_vent_rows(table, zone_name)


def parse_cooling_load_system(t):
    out = []
    for c in _simple_rows(t):
        if len(c) < 10:
            continue
        def at(i): return c[i] if i < len(c) else ""
        out.append(CoolingLoadSystem(
            location=at(0), peak_month=at(1), peak_time=at(2),
            roof_btuh=_clean_number(at(3)), roof_pct=_clean_number(at(4)),
            wall_btuh=_clean_number(at(5)), wall_pct=_clean_number(at(6)),
            glass_btuh=_clean_number(at(7)), glass_pct=_clean_number(at(8)),
            vent_sensible_btuh=_clean_number(at(9)), vent_sensible_pct=_clean_number(at(10)),
            vent_latent_btuh=_clean_number(at(11)), vent_latent_pct=_clean_number(at(12)),
            infil_sensible_btuh=_clean_number(at(13)), infil_sensible_pct=_clean_number(at(14)),
            infil_latent_btuh=_clean_number(at(15)), infil_latent_pct=_clean_number(at(16)),
        ))
    return out


def parse_cooling_load_room(t):
    out = []
    for c in _simple_rows(t):
        if len(c) < 10:
            continue
        def at(i): return c[i] if i < len(c) else ""
        out.append(CoolingLoadRoom(
            location=at(0), peak=at(1),
            roof_btuh=_clean_number(at(2)), roof_pct=_clean_number(at(3)),
            wall_btuh=_clean_number(at(4)), wall_pct=_clean_number(at(5)),
            glass_btuh=_clean_number(at(6)), glass_pct=_clean_number(at(7)),
            lighting_btuh=_clean_number(at(8)), lighting_pct=_clean_number(at(9)),
            equipment_sensible_btuh=_clean_number(at(10)), equipment_sensible_pct=_clean_number(at(11)),
            equipment_latent_btuh=_clean_number(at(12)), equipment_latent_pct=_clean_number(at(13)),
            people_sensible_btuh=_clean_number(at(14)), people_sensible_pct=_clean_number(at(15)),
            people_latent_btuh=_clean_number(at(16)), people_latent_pct=_clean_number(at(17)),
            infil_sensible_btuh=_clean_number(at(18)), infil_sensible_pct=_clean_number(at(19)),
            infil_latent_btuh=_clean_number(at(20)), infil_latent_pct=_clean_number(at(21)),
        ))
    return out


def parse_heating_load(t):
    out = []
    for c in _simple_rows(t):
        if len(c) < 6:
            continue
        def at(i): return c[i] if i < len(c) else ""
        out.append(HeatingLoad(
            location=at(0),
            roof_btuh=_clean_number(at(1)), roof_pct=_clean_number(at(2)),
            wall_btuh=_clean_number(at(3)), wall_pct=_clean_number(at(4)),
            glass_btuh=_clean_number(at(5)), glass_pct=_clean_number(at(6)),
            slab_btuh=_clean_number(at(7)), slab_pct=_clean_number(at(8)),
            vent_btuh=_clean_number(at(9)), vent_pct=_clean_number(at(10)),
            infil_btuh=_clean_number(at(11)), infil_pct=_clean_number(at(12)),
        ))
    return out


def parse_load_total(t):
    out = []
    for c in _simple_rows(t):
        if len(c) < 10:
            continue
        def at(i): return c[i] if i < len(c) else ""
        peak_2 = at(2)
        if ":" in at(4) or "a.m." in at(4) or "p.m." in at(4):
            cool_cfm = _clean_number(peak_2)
            peak_m, peak_t = at(3), at(4)
            base = 5
        else:
            cool_cfm = _clean_number(peak_2)
            peak_m, peak_t = at(3), ""
            base = 4
        out.append(LoadTotal(
            location=at(0),
            area_ft2=_clean_number(at(1)),
            cool_cfm=cool_cfm,
            cool_peak_month=peak_m,
            cool_peak_time=peak_t,
            cool_total_btuh=_clean_number(at(base)),
            cool_sensible_btuh=_clean_number(at(base + 1)),
            cool_latent_btuh=_clean_number(at(base + 2)),
            cool_total_tons=_clean_number(at(base + 3)),
            cool_sensible_tons=_clean_number(at(base + 4)),
            cool_latent_tons=_clean_number(at(base + 5)),
            cool_ft2_per_ton=_clean_number(at(base + 6)),
            cool_cfm_per_ton=_clean_number(at(base + 7)),
            cool_cfm_per_ft2=_clean_number(at(base + 8)),
            heat_cfm=_clean_number(at(base + 9)),
            heat_btuh=_clean_number(at(base + 10)),
            heat_kw=_clean_number(at(base + 11)),
            heat_cfm_per_ft2=_clean_number(at(base + 12)),
        ))
    return out


def parse_psychrometrics(t, zone_name: str) -> Psychrometrics:
    psy = Psychrometrics(zone_name=zone_name)
    # The psychrometric table puts row labels in <td class="psychLabel">, not "otherData".
    # Walk every row, collecting psychLabel + otherData cells in order.
    for tr in t.find_all("tr"):
        cells = tr.find_all("td", class_=lambda c: c in ("psychLabel", "otherData", "boldData"))
        if len(cells) < 2:
            continue
        label = _txt(cells[0])
        if not label:
            continue
        nums = [_clean_number(_txt(c)) for c in cells[1:]]
        while len(nums) < 7:
            nums.append(None)
        psy.points.append(PsychrometricPoint(
            label=label,
            airflow_cfm=nums[0], dry_bulb_f=nums[1], wet_bulb_f=nums[2],
            humidity_ratio=nums[3], total_btuh=nums[4],
            sensible_btuh=nums[5], latent_btuh=nums[6],
        ))
    return psy


def parse_report(html: str) -> HVACReport:
    soup = BeautifulSoup(html, "lxml")
    report = HVACReport()
    for table in soup.find_all("table"):
        mh = table.find("th", class_="mainHeader")
        sh = table.find("th", class_="subheader") if not mh else None
        title = _txt(mh or sh) if (mh or sh) else ""
        prefix = title.split("(")[0].strip().lower()

        if prefix == "project information":
            report.project = parse_project_info(table)
        elif prefix == "roof types":
            report.roof_types = parse_roof_types(table)
        elif prefix == "roofs":
            report.roofs = parse_roofs(table)
        elif prefix == "wall types":
            report.wall_types = parse_wall_types(table)
        elif prefix == "walls":
            report.walls = parse_walls(table)
        elif prefix == "door types":
            report.door_types = parse_door_types(table)
        elif prefix == "doors":
            report.doors = parse_doors(table)
        elif prefix == "glass types":
            report.glass_types = parse_glass_types(table)
        elif prefix == "glass":
            report.glass = parse_glass(table)
        elif prefix == "room information, part 1":
            report.rooms_p1 = parse_room_info_p1(table)
        elif prefix == "room information, part 2":
            report.rooms_p2 = parse_room_info_p2(table)
        elif prefix == "supply air requirements":
            report.supply_air = parse_supply_air(table)
        elif prefix == "cooling load details - system":
            report.cooling_load_system = parse_cooling_load_system(table)
        elif prefix == "cooling load details - room":
            report.cooling_load_room = parse_cooling_load_room(table)
        elif prefix == "heating load details - system and room":
            report.heating_load = parse_heating_load(table)
        elif prefix == "load total summary - system":
            report.load_total_system = parse_load_total(table)
        elif prefix == "load total summary - room":
            report.load_total_room = parse_load_total(table)
        elif prefix.startswith("psychrometrics"):
            zone = title.split("-", 1)[1].strip() if "-" in title else title
            report.psychrometrics.append(parse_psychrometrics(table, zone))
        elif sh and "ventilation" in prefix:
            # Strip " Ventilation" suffix from the original (cased) title to get the zone name.
            # E.g. "Zone RTU-2: SURGERY/TREAT/HYG Ventilation" → "Zone RTU-2: SURGERY/TREAT/HYG"
            zone = title.rstrip()
            if zone.lower().endswith(" ventilation"):
                zone = zone[:-len(" ventilation")].strip()
            params, room_rows = parse_vent_table(table, zone)
            report.system_vent_params.append(params)
            report.room_vent.extend(room_rows)
    return report


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
    import re
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


from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, PageBreak)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
import datetime

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


engineer = EngineerInfo()
firm = FirmInfo()


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

    # blank padding rows to match reference's white space before totals
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




# === Appended: apply_zone_overrides + build_all_pdfs ===
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

    pdf1 = build_ventilation_schedule_pdf(
        computed, report, out_dir / f"{safe}-Ventilation.pdf", project_name=pn)
    pdf2 = build_air_balance_pdf(
        computed, out_dir / f"{safe}-Air_Balance.pdf", project_name=pn, config=config)
    final_load = build_load_summary_cover(
        computed, report, config, engineer, firm,
        out_dir / f"{safe}-Load.pdf", project_name=pn)

    return {
        "ventilation_schedule": pdf1,
        "air_balance": pdf2,
        "load_summary": final_load,
        "computed": computed,
    }



# === Appended: console preview functions ===
def _fmt_cfm(v):
    if v is None or v == 0:
        return "-"
    return f"{int(round(float(v))):,}"


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


