"""Dehumidifier sizing calculator.

Consumes the same parsed-report dict shape produced by Adicot's
hvac_parse.HVACReport (via dataclasses.asdict) -- report["project"],
report["load_total_system"], report["load_total_room"], report["psychrometrics"] --
so it can be wired into that pipeline later without renaming any fields.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent / "dehumidifier_database.csv"
_DB_CACHE: dict[str, pd.DataFrame] = {}

LATENT_BTU_PER_PINT = 1054.0  # BTU released per pint of moisture condensed (HVAC School sizing convention)
GRAINS_PER_LB = 7000.0
VENT_LATENT_CONSTANT = 0.68  # the "0.68 x CFM x delta-grains" rule of thumb, sea level/standard indoor temps
SENSIBLE_BTU_PER_PINT_REMOVED = 1053.0  # sensible heat a dehumidifier adds back per pint of moisture removed


@dataclass
class ZoneTotals:
    location: str
    area_ft2: float
    cool_cfm: float
    total_btuh: float
    sensible_btuh: float
    latent_btuh: float
    tons: float


def _totals_from_rows(rows: list[dict]) -> list[ZoneTotals]:
    return [
        ZoneTotals(
            location=r.get("location", ""),
            area_ft2=r.get("area_ft2") or 0.0,
            cool_cfm=r.get("cool_cfm") or 0.0,
            total_btuh=r.get("cool_total_btuh") or 0.0,
            sensible_btuh=r.get("cool_sensible_btuh") or 0.0,
            latent_btuh=r.get("cool_latent_btuh") or 0.0,
            tons=r.get("cool_total_tons") or 0.0,
        )
        for r in rows
    ]


def zone_totals(report: dict) -> list[ZoneTotals]:
    return _totals_from_rows(report.get("load_total_system", []))


def room_totals(report: dict) -> list[ZoneTotals]:
    return _totals_from_rows(report.get("load_total_room", []))


def building_totals(report: dict) -> ZoneTotals:
    """Sums load_total_system rows (one per zone) into a single building total."""
    zones = zone_totals(report)
    if not zones:
        raise ValueError("report has no load_total_system rows")
    return ZoneTotals(
        location="Building Total",
        area_ft2=sum(z.area_ft2 for z in zones),
        cool_cfm=sum(z.cool_cfm for z in zones),
        total_btuh=sum(z.total_btuh for z in zones),
        sensible_btuh=sum(z.sensible_btuh for z in zones),
        latent_btuh=sum(z.latent_btuh for z in zones),
        tons=sum(z.tons for z in zones),
    )


def latent_btuh_to_pints_per_day(btuh: float) -> float:
    return (btuh / LATENT_BTU_PER_PINT) * 24.0


def grains_cross_check(report: dict) -> dict | None:
    """Independent re-derivation of the ventilation-air latent load, for comparison
    against the load calc's own "Outside Air" psychrometric point. Mirrors the
    grains_water_difference() formula already used by the target pipeline:
    grains = (W_outside - W_room) * 7000, then Q_latent = 0.68 * OA_CFM * grains.
    """
    psychs = report.get("psychrometrics") or []
    if not psychs:
        return None
    points = psychs[0].get("points", [])
    outside_point = final_point = None
    for pt in points:
        label = (pt.get("label") or "").lower()
        if "outside air" in label:
            outside_point = pt
        elif "final room conditions" in label:
            final_point = pt
    if outside_point is None or final_point is None:
        return None

    outside_w = outside_point.get("humidity_ratio")
    final_w = final_point.get("humidity_ratio")
    oa_cfm = outside_point.get("airflow_cfm")
    reported_latent_btuh = outside_point.get("latent_btuh")
    if outside_w is None or final_w is None or oa_cfm is None:
        return None

    grains_diff = (outside_w - final_w) * GRAINS_PER_LB
    estimated_latent_btuh = VENT_LATENT_CONSTANT * oa_cfm * grains_diff

    result = {
        "grains_water_difference": grains_diff,
        "oa_cfm": oa_cfm,
        "estimated_vent_latent_btuh": estimated_latent_btuh,
        "reported_vent_latent_btuh": reported_latent_btuh,
    }
    if reported_latent_btuh:
        result["pct_diff"] = abs(estimated_latent_btuh - reported_latent_btuh) / reported_latent_btuh
    return result


@dataclass
class DehumidConfig:
    ac_latent_share: float = 0.15  # ACCA Manual S: AC assumed to cover this fraction of design latent load on its own
    derate_pct_aham: float = 0.20  # haircut applied to AHAM 80/60-rated capacity to estimate real delivered capacity
    categories: list[str] | None = None  # None = search entire catalog


@dataclass
class DehumidSizing:
    total_latent_btuh: float
    total_pints_day: float
    ac_latent_share_pct: float
    ac_share_pints_day: float
    dehumidifier_target_pints_day: float


def size_dehumidifier(totals: ZoneTotals, config: DehumidConfig) -> DehumidSizing:
    total_pints_day = latent_btuh_to_pints_per_day(totals.latent_btuh)
    ac_share = total_pints_day * config.ac_latent_share
    return DehumidSizing(
        total_latent_btuh=totals.latent_btuh,
        total_pints_day=total_pints_day,
        ac_latent_share_pct=config.ac_latent_share * 100.0,
        ac_share_pints_day=ac_share,
        dehumidifier_target_pints_day=total_pints_day - ac_share,
    )


def load_database(csv_path: Path | str | None = None) -> pd.DataFrame:
    path = Path(csv_path) if csv_path else DB_PATH
    key = str(path)
    if key not in _DB_CACHE:
        _DB_CACHE[key] = pd.read_csv(path)
    return _DB_CACHE[key]


def derated_capacity(row: pd.Series, config: DehumidConfig) -> float:
    cap = row["rated_capacity_pints_day"]
    if pd.isna(cap):
        return 0.0
    if row.get("test_standard") == "AHAM_80_60":
        return cap * (1 - config.derate_pct_aham)
    return cap


@dataclass
class ModelRecommendation:
    manufacturer: str
    model: str
    category: str
    rated_capacity_pints_day: float
    derated_capacity_pints_day: float
    units: int
    coverage_pct: float
    status: str  # "ok" or "undersized_catalog"
    source_url: str


def recommend_models(
    target_pints_day: float,
    total_pints_day: float,
    config: DehumidConfig,
    n: int = 3,
) -> list[ModelRecommendation]:
    df = load_database()
    if config.categories:
        df = df[df["category"].isin(config.categories)]
    if df.empty:
        return []

    df = df.copy()
    df["derated_capacity_pints_day"] = df.apply(lambda r: derated_capacity(r, config), axis=1)
    df = df.sort_values("derated_capacity_pints_day")

    sufficient = df[df["derated_capacity_pints_day"] >= target_pints_day]
    recs: list[ModelRecommendation] = []

    if not sufficient.empty:
        for _, row in sufficient.head(n).iterrows():
            recs.append(ModelRecommendation(
                manufacturer=row["manufacturer"],
                model=row["model"],
                category=row["category"],
                rated_capacity_pints_day=row["rated_capacity_pints_day"],
                derated_capacity_pints_day=row["derated_capacity_pints_day"],
                units=1,
                coverage_pct=min(100.0, row["derated_capacity_pints_day"] / total_pints_day * 100.0),
                status="ok",
                source_url=row["source_url"],
            ))
    else:
        # nothing in the catalog alone meets the target -- fall back to N of the largest available unit
        best = df.iloc[-1]
        cap = best["derated_capacity_pints_day"]
        units = math.ceil(target_pints_day / cap) if cap > 0 else 0
        recs.append(ModelRecommendation(
            manufacturer=best["manufacturer"],
            model=best["model"],
            category=best["category"],
            rated_capacity_pints_day=best["rated_capacity_pints_day"],
            derated_capacity_pints_day=cap,
            units=units,
            coverage_pct=min(100.0, (cap * units) / total_pints_day * 100.0) if cap else 0.0,
            status="undersized_catalog",
            source_url=best["source_url"],
        ))
    return recs


def sensible_heat_penalty(pints_day_removed: float) -> float:
    """BTU/hr of sensible heat the dehumidifier adds back to the space as it removes moisture."""
    return (pints_day_removed * SENSIBLE_BTU_PER_PINT_REMOVED) / 24.0


DEFAULT_CAVEATS = [
    "Capacity figures are nameplate/derated estimates, not manufacturer performance-curve data at this project's "
    "actual design RH/temp -- verify against the manufacturer's submittal before final spec.",
    "Design-day peak latent load is used as a conservative sizing proxy per ACCA Manual S. In practice, supplemental "
    "dehumidification duty concentrates in part-load and shoulder-season hours, not the coincident AC design-day peak.",
]


@dataclass
class DehumidResult:
    totals: ZoneTotals
    sizing: DehumidSizing
    recommendations: list[ModelRecommendation]
    sensible_heat_added_btuh: float
    caveats: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"Building: {self.totals.location} ({self.totals.area_ft2:,.0f} ft^2, {self.totals.cool_cfm:,.0f} CFM)",
            f"Design total cooling load: {self.totals.total_btuh:,.0f} BTU/h ({self.totals.tons:.1f} tons)",
            f"  Sensible: {self.totals.sensible_btuh:,.0f} BTU/h",
            f"  Latent:   {self.totals.latent_btuh:,.0f} BTU/h -> {self.sizing.total_pints_day:,.1f} pints/day design moisture load",
            "",
            f"AC assumed share (Manual S, {self.sizing.ac_latent_share_pct:.0f}%): {self.sizing.ac_share_pints_day:,.1f} pints/day",
            f"Dehumidifier target capacity: {self.sizing.dehumidifier_target_pints_day:,.1f} pints/day",
            "",
            "Recommended equipment:",
        ]
        if not self.recommendations:
            lines.append("  No models in catalog match the requested category filter.")
        for rec in self.recommendations:
            qty = f"{rec.units}x " if rec.units > 1 else ""
            flag = "  [CATALOG UNDERSIZED - see caveats]" if rec.status == "undersized_catalog" else ""
            lines.append(
                f"  {qty}{rec.manufacturer} {rec.model} ({rec.category}) - "
                f"{rec.derated_capacity_pints_day:,.0f} pt/day derated (rated {rec.rated_capacity_pints_day:,.0f}) - "
                f"covers {rec.coverage_pct:.0f}% of total design latent load{flag}"
            )
        lines.append("")
        lines.append(
            f"Sensible heat added back to space by dehumidifier: ~{self.sensible_heat_added_btuh:,.0f} BTU/h "
            "(AC must absorb this; not used to resize AC here)"
        )
        if self.caveats:
            lines.append("")
            lines.append("Caveats:")
            for c in self.caveats:
                lines.append(f"  - {c}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "totals": self.totals.__dict__,
            "sizing": self.sizing.__dict__,
            "recommendations": [r.__dict__ for r in self.recommendations],
            "sensible_heat_added_btuh": self.sensible_heat_added_btuh,
            "caveats": self.caveats,
        }


def compute_dehumidification(report: dict, config: DehumidConfig | None = None) -> DehumidResult:
    config = config or DehumidConfig()
    totals = building_totals(report)
    sizing = size_dehumidifier(totals, config)
    recs = recommend_models(sizing.dehumidifier_target_pints_day, sizing.total_pints_day, config)
    sensible_penalty = sensible_heat_penalty(sizing.dehumidifier_target_pints_day)

    caveats = list(DEFAULT_CAVEATS)

    cross_check = grains_cross_check(report)
    if cross_check is not None and cross_check.get("pct_diff") is not None and cross_check["pct_diff"] > 0.15:
        caveats.append(
            f"Grains-based ventilation-latent cross-check ({cross_check['estimated_vent_latent_btuh']:,.0f} BTU/h) "
            f"diverges from the load calc's own outside-air latent figure "
            f"({cross_check['reported_vent_latent_btuh']:,.0f} BTU/h) by "
            f"{cross_check['pct_diff']*100:.0f}% -- worth a manual sanity check on the psychrometrics input."
        )

    if not recs or recs[0].status == "undersized_catalog":
        caveats.append(
            "No single catalog model reaches the target capacity; the multi-unit fallback assumes identical units "
            "operating in parallel, which may not be practical for this space -- consider a custom-quoted larger "
            "commercial unit or splitting dehumidification across multiple zones."
        )

    return DehumidResult(
        totals=totals,
        sizing=sizing,
        recommendations=recs,
        sensible_heat_added_btuh=sensible_penalty,
        caveats=caveats,
    )
