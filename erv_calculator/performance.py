from dataclasses import dataclass
from typing import Optional


@dataclass
class RatingPoint:
    season: str               # "summer" or "winter"
    airflow_frac: float       # 0.75 or 1.0 per AHRI 1060 test points
    sre: float                 # sensible recovery efficiency, 0-1
    latent_eff: float = 0.0    # 0 for HRV (sensible-only) cores
    total_eff: Optional[float] = None


@dataclass
class EffectivenessResult:
    sre: float
    latent_eff: float
    total_eff: Optional[float]
    extrapolated: bool = False


class ErvPerformance:
    def __init__(self, points=None, constant=None):
        self._points = points
        self._constant = constant

    @classmethod
    def from_rating_table(cls, points: list) -> "ErvPerformance":
        return cls(points=points)

    @classmethod
    def from_constant(cls, sre: float, latent_eff: float = 0.0, total_eff: Optional[float] = None) -> "ErvPerformance":
        return cls(constant=EffectivenessResult(sre, latent_eff, total_eff))

    def effectiveness_at(self, airflow_frac: float, season: str) -> EffectivenessResult:
        # linear interpolation over airflow fraction within the matching season's rating points
        if self._constant is not None:
            return self._constant
        pts = sorted((p for p in self._points if p.season == season), key=lambda p: p.airflow_frac)
        if not pts:
            raise ValueError(f"no rating points for season '{season}'")
        if len(pts) == 1:
            only = pts[0]
            return EffectivenessResult(only.sre, only.latent_eff, only.total_eff,
                                        extrapolated=(airflow_frac != only.airflow_frac))
        lo, hi = pts[0], pts[-1]
        f = min(max(airflow_frac, lo.airflow_frac), hi.airflow_frac)
        extrapolated = f != airflow_frac
        t = (f - lo.airflow_frac) / (hi.airflow_frac - lo.airflow_frac)
        sre = lo.sre + t * (hi.sre - lo.sre)
        latent = lo.latent_eff + t * (hi.latent_eff - lo.latent_eff)
        total = None
        if lo.total_eff is not None and hi.total_eff is not None:
            total = lo.total_eff + t * (hi.total_eff - lo.total_eff)
        return EffectivenessResult(sre, latent, total, extrapolated)


def check_frost_risk(t_out_f: float, threshold_f: float = -10.0) -> bool:
    # flags that defrost cycling may derate SRE below rated value; does not modify results
    return t_out_f < threshold_f
