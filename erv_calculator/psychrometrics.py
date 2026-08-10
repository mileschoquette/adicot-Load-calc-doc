import math

P_ATM_PSIA = 14.696
GRAINS_PER_LB = 7000.0


def saturation_pressure_psia(t_f: float) -> float:
    # Magnus-Tetens approximation of water vapor saturation pressure, psia
    t_c = (t_f - 32) * 5 / 9
    pws_kpa = 0.61094 * math.exp(17.625 * t_c / (t_c + 243.04))
    return pws_kpa * 0.1450377


def humidity_ratio(t_f: float, rh_pct: float, p_atm_psia: float = P_ATM_PSIA) -> float:
    # humidity ratio W, lb water / lb dry air, from dry-bulb T and RH%
    pw = (rh_pct / 100) * saturation_pressure_psia(t_f)
    return 0.622 * pw / (p_atm_psia - pw)


def enthalpy_btu_per_lb(t_f: float, w: float) -> float:
    # moist air enthalpy, Btu/lb dry air (ASHRAE formula)
    return 0.240 * t_f + w * (1061 + 0.444 * t_f)
