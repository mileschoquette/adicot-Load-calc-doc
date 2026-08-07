import argparse

from .sizing import commercial_zone_oa_cfm, residential_oa_cfm
from .performance import ErvPerformance
from .load_impact import compute_erv_impact


def build_parser() -> argparse.ArgumentParser:
    # top-level parser with size-commercial, size-residential, and impact subcommands
    parser = argparse.ArgumentParser(prog="erv-calc")
    sub = parser.add_subparsers(dest="command", required=True)

    p_comm = sub.add_parser("size-commercial", help="ASHRAE 62.1 zone outdoor air CFM")
    p_comm.add_argument("--rp", type=float, required=True, help="cfm/person")
    p_comm.add_argument("--pz", type=float, required=True, help="occupants")
    p_comm.add_argument("--ra", type=float, required=True, help="cfm/ft2")
    p_comm.add_argument("--az", type=float, required=True, help="zone area, ft2")
    p_comm.add_argument("--ez", type=float, default=1.0, help="zone air distribution effectiveness")

    p_res = sub.add_parser("size-residential", help="ASHRAE 62.2 whole-dwelling CFM")
    p_res.add_argument("--floor-area", type=float, required=True, help="ft2")
    p_res.add_argument("--bedrooms", type=int, required=True)

    p_imp = sub.add_parser("impact", help="ERV outdoor-air load impact using a constant SRE/latent effectiveness")
    p_imp.add_argument("--cfm", type=float, required=True)
    p_imp.add_argument("--t1", type=float, required=True, help="entering outdoor air, F")
    p_imp.add_argument("--w1", type=float, required=True, help="entering outdoor air humidity ratio, gr/lb")
    p_imp.add_argument("--t3", type=float, required=True, help="entering exhaust/return air, F")
    p_imp.add_argument("--w3", type=float, required=True, help="entering exhaust/return humidity ratio, gr/lb")
    p_imp.add_argument("--sre", type=float, required=True)
    p_imp.add_argument("--latent-eff", type=float, default=0.0)

    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)

    if args.command == "size-commercial":
        cfm = commercial_zone_oa_cfm(args.rp, args.pz, args.ra, args.az, args.ez)
        print(f"required outdoor air: {cfm:.1f} cfm")

    elif args.command == "size-residential":
        cfm = residential_oa_cfm(args.floor_area, args.bedrooms)
        print(f"required outdoor air: {cfm:.1f} cfm")

    elif args.command == "impact":
        performance = ErvPerformance.from_constant(sre=args.sre, latent_eff=args.latent_eff)
        result = compute_erv_impact(
            args.cfm, args.t1, args.w1, args.t3, args.w3,
            performance, airflow_frac=1.0, season="summer",
        )
        print(f"raw sensible:   {result.raw_sensible_btuh:.0f} Btu/hr")
        print(f"net sensible:   {result.net_sensible_btuh:.0f} Btu/hr")
        print(f"sensible saved: {result.sensible_reduction_btuh:.0f} Btu/hr")
        print(f"raw latent:     {result.raw_latent_btuh:.0f} Btu/hr")
        print(f"net latent:     {result.net_latent_btuh:.0f} Btu/hr")
        print(f"latent saved:   {result.latent_reduction_btuh:.0f} Btu/hr")


if __name__ == "__main__":
    main()
