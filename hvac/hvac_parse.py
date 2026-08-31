"""HVAC Loads pipeline — HTML parsing layer.

Parses a Design Master HTML export into an HVACReport of raw dataclasses.
Split out of hvac_pipeline.py; re-exported from there for backward compat.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup


def is_zone(location: str) -> bool:
    """SupplyAirRow/load-row locations are either 'Zone ...' or 'Room ...' after parsing."""
    return location.strip().lower().startswith("zone")


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
        # Strip thousands separators first: "2,225 CFM 389 CFM" is 2614, not 2 + 225 + 389.
        text = (self.ventilation_cfm_text or "").replace(",", "")
        nums = re.findall(r"-?\d+(?:\.\d+)?", text)
        if not nums:
            return None
        return sum(float(n) for n in nums)


assert RoomInfoP1("SANCTUARY", "Public-Religious Worship", 6483, 12,
                  "5 CFM / person 0.06 CFM / ft 2", "2,225 CFM 389 CFM",
                  "", "", "", "").vbz_cfm == 2614


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


def _extract_room_vent_rows(table, zone_name: str) -> list[RoomVent]:
    out = []
    for row in table.find_all("tr"):
        cells = _data_cells(row)
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
            zd=nums[9],
            evz=nums[10],
        ))
    return out


def parse_vent_table(table, zone_name: str) -> list[RoomVent]:
    return _extract_room_vent_rows(table, zone_name)


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
            report.room_vent.extend(parse_vent_table(table, zone))
    return report
