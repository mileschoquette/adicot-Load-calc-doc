from dataclasses import dataclass


def sensible_load(cfm: float, t1_f: float, t3_f: float) -> float:
    # Btu/hr outdoor-air sensible load; t1 = entering outdoor air, t3 = entering exhaust/return
    return 1.08 * cfm * (t1_f - t3_f)


def latent_load(cfm: float, w1_gr: float, w3_gr: float) -> float:
    # Btu/hr outdoor-air latent load, humidity ratio difference in grains/lb
    return 0.68 * cfm * (w1_gr - w3_gr)


def erv_leaving_conditions(t1_f: float, w1_gr: float, t3_f: float, w3_gr: float,
                            sre: float, latent_eff: float) -> tuple[float, float]:
    # leaving supply air state (T2, W2) after the ERV core, per AHRI 1060 effectiveness definitions
    t2 = t1_f - sre * (t1_f - t3_f)
    w2 = w1_gr - latent_eff * (w1_gr - w3_gr)
    return t2, w2


@dataclass
class ErvImpactResult:
    raw_sensible_btuh: float
    raw_latent_btuh: float
    net_sensible_btuh: float
    net_latent_btuh: float
    sensible_reduction_btuh: float
    latent_reduction_btuh: float
    t2_f: float
    w2_gr: float
    effectiveness: object  # EffectivenessResult from performance.py


def compute_erv_impact(cfm: float, t1_f: float, w1_gr: float, t3_f: float, w3_gr: float,
                        performance, airflow_frac: float, season: str) -> ErvImpactResult:
    # raw vs. net (post-ERV) outdoor-air loads and the resulting sensible/latent reduction
    eff = performance.effectiveness_at(airflow_frac, season)
    raw_s = sensible_load(cfm, t1_f, t3_f)
    raw_l = latent_load(cfm, w1_gr, w3_gr)
    t2, w2 = erv_leaving_conditions(t1_f, w1_gr, t3_f, w3_gr, eff.sre, eff.latent_eff)
    net_s = sensible_load(cfm, t2, t3_f)
    net_l = latent_load(cfm, w2, w3_gr)
    return ErvImpactResult(raw_s, raw_l, net_s, net_l, raw_s - net_s, raw_l - net_l, t2, w2, eff)


def total_block_load(envelope_btuh: float, internal_btuh: float,
                      net_oa_sensible_btuh: float, net_oa_latent_btuh: float) -> dict:
    # combines envelope + internal + net (post-ERV) outdoor-air load for equipment sizing
    total_sensible = envelope_btuh + internal_btuh + net_oa_sensible_btuh
    total = total_sensible + net_oa_latent_btuh
    return {"total_sensible_btuh": total_sensible, "total_btuh": total, "tons": total / 12000}
