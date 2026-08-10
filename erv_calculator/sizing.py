from dataclasses import dataclass


def commercial_zone_oa_cfm(rp: float, pz: float, ra: float, az: float, ez: float = 1.0) -> float:
    # ASHRAE 62.1 zone outdoor air: Vbz = Rp*Pz + Ra*Az, Voz = Vbz/Ez, cfm
    vbz = rp * pz + ra * az
    return vbz / ez


def residential_oa_cfm(floor_area_ft2: float, bedrooms: int) -> float:
    # ASHRAE 62.2 whole-dwelling ventilation: 0.03*area + 7.5*(bedrooms+1), cfm
    return 0.03 * floor_area_ft2 + 7.5 * (bedrooms + 1)


def apply_oacf(measured_cfm: float, oacf: float = 1.0) -> float:
    # corrects catalog measured airflow to actual delivered airflow, cfm
    return measured_cfm * oacf


def check_balance(supply_cfm: float, exhaust_cfm: float, tolerance_pct: float = 10.0) -> dict:
    # supply/exhaust airflow imbalance check, base = average of the two flows
    avg = (supply_cfm + exhaust_cfm) / 2
    imbalance_pct = abs(supply_cfm - exhaust_cfm) / avg * 100
    return {"imbalance_pct": imbalance_pct, "balanced": imbalance_pct <= tolerance_pct}


@dataclass
class ErvUnit:
    name: str
    delivered_cfm: float  # read manually off the manufacturer fan curve at design external static pressure
    oacf: float = 1.0


def meets_requirement(unit: ErvUnit, required_cfm: float) -> bool:
    # true if the unit's OACF-corrected delivered airflow covers the required airflow
    return unit.delivered_cfm * unit.oacf >= required_cfm
