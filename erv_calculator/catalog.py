from dataclasses import dataclass

from .performance import ErvPerformance, RatingPoint
from .sizing import ErvUnit, meets_requirement


@dataclass
class CatalogEntry:
    manufacturer: str
    model: str
    unit: ErvUnit
    performance: ErvPerformance
    source: str  # URL or spec-sheet citation for the published performance data


CATALOG: list[CatalogEntry] = [
    # Residential ERV. Rated 35-131 cfm @ 0.2 in. wg; delivered_cfm taken at that ESP.
    # OACF not published on this spec sheet -> defaulted to 1.0.
    # Winter SRE is HVI-certified (32F); summer sre is manufacturer's "Apparent Sensible
    # Effectiveness" (explicitly marked "not certified by HVI" on the sheet, since HVI/AHRI
    # only certifies Total Recovery Efficiency for cooling on this unit) while summer
    # latent_eff/total_eff are HVI-certified.
    CatalogEntry(
        manufacturer="Broan-NuTone",
        model="B130E65RS",
        unit=ErvUnit(name="B130E65RS", delivered_cfm=131, oacf=1.0),
        performance=ErvPerformance.from_rating_table([
            RatingPoint("winter", 66 / 131, sre=0.67, latent_eff=0.56),
            RatingPoint("winter", 110 / 131, sre=0.63, latent_eff=0.49),
            RatingPoint("summer", 51 / 131, sre=0.68, latent_eff=0.66, total_eff=0.63),
            RatingPoint("summer", 110 / 131, sre=0.57, latent_eff=0.54, total_eff=0.52),
        ]),
        source="Broan-NuTone Spec_Sheet_B130E65RT_B130E65RS_1.pdf, Energy Performance table "
               "(https://broan-nutone.com/getmedia/9ad358fd-b2d5-4d7f-85b9-42b0ab3bb601/"
               "Spec_Sheet_B130E65RT_B130E65RS_1.pdf)",
    ),
    # Residential HRV (sensible-only core, no moisture transfer) -> latent_eff=0.0 for all points.
    # Rated 35-159 cfm @ 0.2 in. wg. OACF not published -> defaulted to 1.0.
    # Sheet publishes winter (32F) data only; no summer/cooling row is given, so no summer
    # RatingPoint is included here rather than guessing one.
    CatalogEntry(
        manufacturer="Broan-NuTone",
        model="B160H65RS",
        unit=ErvUnit(name="B160H65RS", delivered_cfm=159, oacf=1.0),
        performance=ErvPerformance.from_rating_table([
            RatingPoint("winter", 64 / 159, sre=0.68, latent_eff=0.0),
            RatingPoint("winter", 131 / 159, sre=0.55, latent_eff=0.0),
        ]),
        source="Broan-NuTone Spec_Sheet_B160H65RT_B160H65RS_1.pdf, Energy Performance table "
               "(https://www.broan-nutone.com/getmedia/a357182a-34da-4cdd-aa51-1055727625cd/"
               "Spec_Sheet_B160H65RT_B160H65RS_1.pdf)",
    ),
    # Residential ERV. delivered_cfm is net supply airflow at 0.3 in. wg (75 Pa), within the
    # unit's published 50-140 cfm range. OACF not published on this sheet -> defaulted to 1.0.
    # Only winter is published with a standalone SRE; cooling is only published as a combined
    # Total Recovery Efficiency (no separate sensible/latent split), so no summer RatingPoint
    # is included rather than inventing a sensible value.
    CatalogEntry(
        manufacturer="RenewAire",
        model="EV130",
        unit=ErvUnit(name="EV130", delivered_cfm=140, oacf=1.0),
        performance=ErvPerformance.from_rating_table([
            RatingPoint("winter", 100 / 140, sre=0.72, latent_eff=0.64),
        ]),
        source="RenewAire RES HVI Tested/Certified per CSA C439 performance sheet, EV130 table "
               "(https://renewaire.com/wp-content/uploads/2025/11/Res-HVI-Certifications-1125.pdf)",
    ),
    # Light-commercial ERV. delivered_cfm is the manufacturer's stated "average airflow" of
    # 468 cfm @ 0.4 in. wg (100 Pa). OACF not published -> defaulted to 1.0. Both airflow test
    # points and both seasons publish sensible, latent, and total effectiveness.
    CatalogEntry(
        manufacturer="Fantech",
        model="SER450",
        unit=ErvUnit(name="SER450", delivered_cfm=468, oacf=1.0),
        performance=ErvPerformance.from_rating_table([
            RatingPoint("winter", 300 / 468, sre=0.63, latent_eff=0.46, total_eff=0.59),
            RatingPoint("winter", 225 / 468, sre=0.66, latent_eff=0.51, total_eff=0.64),
            RatingPoint("summer", 300 / 468, sre=0.63, latent_eff=0.42, total_eff=0.58),
            RatingPoint("summer", 225 / 468, sre=0.69, latent_eff=0.48, total_eff=0.63),
        ]),
        source="Fantech Specification Sheet SER 450, item #444568, Energy performance table "
               "(https://stepimassets.blob.core.windows.net/dsassetsprod/"
               "444568_SER450_SPEC_SHEET_EN_20190426_002655862.PDF)",
    ),
    # Light-commercial ERV. delivered_cfm is the manufacturer's stated "average airflow" of
    # 1179 cfm @ 0.4 in. wg (100 Pa). OACF not published -> defaulted to 1.0.
    CatalogEntry(
        manufacturer="Fantech",
        model="SER1100",
        unit=ErvUnit(name="SER1100", delivered_cfm=1179, oacf=1.0),
        performance=ErvPerformance.from_rating_table([
            RatingPoint("winter", 840 / 1179, sre=0.54, latent_eff=0.35, total_eff=0.50),
            RatingPoint("winter", 630 / 1179, sre=0.57, latent_eff=0.40, total_eff=0.54),
            RatingPoint("summer", 840 / 1179, sre=0.51, latent_eff=0.32, total_eff=0.50),
            RatingPoint("summer", 630 / 1179, sre=0.60, latent_eff=0.37, total_eff=0.53),
        ]),
        source="Fantech Specification Sheet SER 1100, item #444573, Energy performance table "
               "(https://cdn.lsicloud.net/mcndistinc/datasheets/"
               "SystemairAB-Fantech_00807_1_2_SPEC.pdf)",
    ),
    # Light-commercial ERV. delivered_cfm is the manufacturer's stated "average airflow" of
    # 1300 cfm @ 0.4 in. wg (100 Pa). OACF not published -> defaulted to 1.0.
    CatalogEntry(
        manufacturer="Fantech",
        model="SER1300",
        unit=ErvUnit(name="SER1300", delivered_cfm=1300, oacf=1.0),
        performance=ErvPerformance.from_rating_table([
            RatingPoint("winter", 840 / 1300, sre=0.54, latent_eff=0.35, total_eff=0.50),
            RatingPoint("winter", 630 / 1300, sre=0.57, latent_eff=0.40, total_eff=0.54),
            RatingPoint("summer", 840 / 1300, sre=0.51, latent_eff=0.32, total_eff=0.49),
            RatingPoint("summer", 630 / 1300, sre=0.60, latent_eff=0.37, total_eff=0.53),
        ]),
        source="Fantech Specification Sheet SER 1300, item #444575, Energy performance table "
               "(https://www.hvacquick.com/catalog_files/Fantech_SER1300_Specs.pdf)",
    ),
    # Large-commercial ERV (SEMCO FV Preconditioner Series). AHRI 1060 certifies the energy
    # recovery wheel core rather than the packaged cabinet, so delivered_cfm is the certificate's
    # "Leaving Supply Air Flow (SCFM)" test point and the pressure figure is the wheel's pressure
    # drop (0.19 in. wg), not a full-cabinet ESP. OACF is the value at the 0 in. wg pressure
    # differential test (Test 1); the same certificate shows OACF rising to 1.16 at 1.0 in. wg.
    # Sensible/latent/total effectiveness below are the certificate's non-net ("gross") ratings,
    # consistent with how other entries in this catalog report effectiveness (not the certificate's
    # separate "Net" row, which further derates for purge-air cross leakage).
    CatalogEntry(
        manufacturer="SEMCO",
        model="FV-3000",
        unit=ErvUnit(name="FV-3000", delivered_cfm=2000, oacf=1.06),
        performance=ErvPerformance.from_rating_table([
            RatingPoint("winter", 1.0, sre=0.75, latent_eff=0.73, total_eff=0.74),
            RatingPoint("winter", 0.75, sre=0.77, latent_eff=0.77, total_eff=0.77),
            RatingPoint("summer", 1.0, sre=0.74, latent_eff=0.73, total_eff=0.73),
            RatingPoint("summer", 0.75, sre=0.77, latent_eff=0.76, total_eff=0.76),
        ]),
        source="AHRI Certificate of Product Ratings, SEMCO FV-3000 (SEMCO LLC/Flakt Woods AB), "
               "AHRI Certified Reference Number 6483469, Certificate No. 130637390041291322, "
               "dated 12/22/2014 "
               "(https://2204845.fs1.hubspotusercontent-na1.net/hubfs/2204845/"
               "SEMCO_FV_3000_AHRI_Certificate.pdf)",
    ),
    # Large-commercial ERV (SEMCO FV Preconditioner Series). Same wheel media/technology and
    # caveats as FV-3000 above (component-level AHRI cert, pressure figure is wheel pressure drop,
    # OACF at 0 in. wg, gross rather than net effectiveness).
    CatalogEntry(
        manufacturer="SEMCO",
        model="FV-4000",
        unit=ErvUnit(name="FV-4000", delivered_cfm=2400, oacf=1.06),
        performance=ErvPerformance.from_rating_table([
            RatingPoint("winter", 1.0, sre=0.75, latent_eff=0.73, total_eff=0.74),
            RatingPoint("winter", 0.75, sre=0.77, latent_eff=0.77, total_eff=0.77),
            RatingPoint("summer", 1.0, sre=0.74, latent_eff=0.73, total_eff=0.73),
            RatingPoint("summer", 0.75, sre=0.77, latent_eff=0.76, total_eff=0.76),
        ]),
        source="AHRI Certificate of Product Ratings, SEMCO FV-4000 (SEMCO LLC/Flakt Woods AB), "
               "AHRI Certified Reference Number 6483470, Certificate No. 130637390112897158, "
               "dated 12/22/2014 "
               "(https://www.semcohvac.com/hubfs/SEMCO_FV_4000_AHRI_Certificate.pdf)",
    ),
    # Large-commercial ERV (SEMCO FV Preconditioner Series). Same wheel media/technology and
    # caveats as FV-3000 above.
    CatalogEntry(
        manufacturer="SEMCO",
        model="FV-5000",
        unit=ErvUnit(name="FV-5000", delivered_cfm=3000, oacf=1.06),
        performance=ErvPerformance.from_rating_table([
            RatingPoint("winter", 1.0, sre=0.75, latent_eff=0.73, total_eff=0.74),
            RatingPoint("winter", 0.75, sre=0.77, latent_eff=0.77, total_eff=0.77),
            RatingPoint("summer", 1.0, sre=0.74, latent_eff=0.73, total_eff=0.73),
            RatingPoint("summer", 0.75, sre=0.77, latent_eff=0.76, total_eff=0.76),
        ]),
        source="AHRI Certificate of Product Ratings, SEMCO FV-5000 (SEMCO LLC/Flakt Woods AB), "
               "AHRI Certified Reference Number 6483471, Certificate No. 130637390178730846, "
               "dated 12/22/2014 "
               "(https://www.semcohvac.com/hubfs/SEMCO_FV_5000_AHRI_Certificate.pdf)",
    ),
    # Large-commercial ERV (SEMCO FV Preconditioner Series, largest cabinet size). Model name on
    # the AHRI certificate covers both the FV-7500 and FV-9000 cabinet sizes (they share the same
    # certified wheel); delivered_cfm is that certificate's tested Leaving Supply Air Flow.
    # Same wheel media/technology and caveats as FV-3000 above.
    CatalogEntry(
        manufacturer="SEMCO",
        model="FV-7500/9000",
        unit=ErvUnit(name="FV-7500/9000", delivered_cfm=4500, oacf=1.06),
        performance=ErvPerformance.from_rating_table([
            RatingPoint("winter", 1.0, sre=0.75, latent_eff=0.73, total_eff=0.74),
            RatingPoint("winter", 0.75, sre=0.77, latent_eff=0.77, total_eff=0.77),
            RatingPoint("summer", 1.0, sre=0.74, latent_eff=0.73, total_eff=0.73),
            RatingPoint("summer", 0.75, sre=0.77, latent_eff=0.76, total_eff=0.76),
        ]),
        source="AHRI Certificate of Product Ratings, SEMCO FV-7500/9000 (SEMCO LLC/Flakt Woods AB), "
               "AHRI Certified Reference Number 6483472, Certificate No. 130637390332862798, "
               "dated 12/22/2014 "
               "(https://www.semcohvac.com/hubfs/SEMCO_FV_7500_9000_AHRI_Certificate.pdf)",
    ),
]


def find_units_meeting(required_cfm: float) -> list[CatalogEntry]:
    # candidate units whose OACF-corrected delivered airflow covers the requirement
    return [e for e in CATALOG if meets_requirement(e.unit, required_cfm)]


def get_by_model(model: str) -> CatalogEntry:
    # exact model-name lookup; raises ValueError if no entry matches
    for entry in CATALOG:
        if entry.model == model:
            return entry
    raise ValueError(f"no catalog entry for model '{model}'")
