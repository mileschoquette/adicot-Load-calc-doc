"""
HVAC charts — server-side PNG renderer for the Flask UI's Charts tab.

Given a parsed HVACReport (Phase 1 output), produces a fixed set of charts
as PNG files in `out_dir`. Each chart is a separate file so the template
can lay them out independently.

Chart inventory (matches the agreed list from the design):
  1. sensible_vs_latent.png       — stacked bar per zone, cooling load
  2. cooling_breakdown_<i>.png    — one pie per zone (load component breakdown)
  3. air_balance.png              — grouped bar per zone (supply / OA)
                                    NOTE: return + exhaust are Phase-2 values;
                                    this chart is supply + OA only until we
                                    plumb in the ComputedReport.
  4. top_rooms_cooling.png        — top 10 rooms by total cooling btuh

All charts use matplotlib's `Agg` backend so they work in a headless server.
No GUI, no display calls. matplotlib is the only chart dep.

Usage:
    from charts import render_all_charts
    rendered = render_all_charts(report, out_dir=Path("jobs/<id>/out/charts"))
    # rendered is a list[Path] of files actually written
"""

from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

import matplotlib
matplotlib.use("Agg")  # must be set before pyplot import — headless safe
import matplotlib.pyplot as plt


# Adicot brand-ish palette — tweak when the rest of the UI gets styled
_PALETTE = {
    "sensible":  "#1f77b4",
    "latent":    "#ff7f0e",
    "supply":    "#2ca02c",
    "oa":        "#9467bd",
    "return":    "#17becf",
    "exhaust":   "#d62728",
    # for the breakdown pie — one color per load component
    "roof":      "#8c564b",
    "wall":      "#7f7f7f",
    "glass":     "#1f77b4",
    "vent":      "#9467bd",
    "infil":     "#bcbd22",
    "lighting":  "#ff7f0e",
    "equipment": "#2ca02c",
    "people":    "#e377c2",
}

# Default figure size — wide enough for ~6 zones without crowding
_FIGSIZE = (10, 5)
_DPI = 110


def _is_zone(location: str) -> bool:
    return location.strip().lower().startswith("zone")


def _shorten_zone(name: str, n: int = 22) -> str:
    """Trim 'Zone RTU-2: SURGERY/TREAT/HYG' to fit on an x-axis label."""
    s = name.replace("Zone ", "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _save(fig: plt.Figure, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=_DPI, bbox_inches="tight")
    plt.close(fig)
    return path


# ──────────────────────────────────────────────────────────────────────
# Chart 1: cooling sensible vs latent, stacked bar per zone
# ──────────────────────────────────────────────────────────────────────
def chart_sensible_vs_latent(report, out_path: Path) -> Path:
    """Stacked bar: total cooling sensible vs latent btuh per zone.

    Pulls from load_total_system (one row per zone), which already
    has the sensible/latent split computed by Design Master.
    """
    rows = [lt for lt in report.load_total_system if _is_zone(lt.location)]
    if not rows:
        return None  # nothing to plot — caller skips this chart

    zones = [_shorten_zone(r.location) for r in rows]
    sensible = [r.cool_sensible_btuh or 0 for r in rows]
    latent   = [r.cool_latent_btuh or 0 for r in rows]

    fig, ax = plt.subplots(figsize=_FIGSIZE)
    ax.bar(zones, sensible, label="Sensible", color=_PALETTE["sensible"])
    ax.bar(zones, latent, bottom=sensible, label="Latent", color=_PALETTE["latent"])

    # Annotate each bar with the total btuh — engineers read totals first,
    # then care about the split
    for i, (s, l) in enumerate(zip(sensible, latent)):
        ax.text(i, s + l, f"{s + l:,.0f}", ha="center", va="bottom",
                fontsize=9, fontweight="bold")

    ax.set_ylabel("Cooling Load (BTU/hr)")
    ax.set_title("Cooling Load: Sensible vs Latent by Zone")
    ax.legend(loc="upper right")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))

    return _save(fig, out_path)


# ──────────────────────────────────────────────────────────────────────
# Chart 2: cooling load breakdown pie, one per zone
# ──────────────────────────────────────────────────────────────────────
def chart_cooling_breakdown(report, out_dir: Path) -> list[Path]:
    """One pie per zone showing how the cooling load splits across components.

    Sums sensible + latent for vent/infil so each component appears once
    rather than twice. Filters out zero-value slices so labels stay readable.
    """
    rows = [cl for cl in report.cooling_load_system]
    out_paths = []

    for i, cl in enumerate(rows):
        # Build component dict — combine sensible + latent for vent/infil
        # so the pie reads as "components of total cooling", not "breakdown by sensible/latent within each component"
        components = {
            "Roof":      cl.roof_btuh or 0,
            "Wall":      cl.wall_btuh or 0,
            "Glass":     cl.glass_btuh or 0,
            "Vent":      (cl.vent_sensible_btuh or 0) + (cl.vent_latent_btuh or 0),
            "Infil":     (cl.infil_sensible_btuh or 0) + (cl.infil_latent_btuh or 0),
        }
        # System-level cooling rows don't have lighting/equipment/people —
        # those are room-level. So roll those up from cooling_load_room
        # filtered to rooms whose zone (via supply_air) matches this zone.
        # Simplest: sum across all room rows for this zone's totals — but
        # cooling_load_room doesn't tag its zone. So instead we use the
        # fact that the first room row matching the zone name *is* the zone
        # row (it's repeated). Skip the lighting/eq/ppl piece at the system
        # level for now and just show envelope + air components.
        # If you want the full breakdown including lighting/eq/ppl, that
        # data lives in cooling_load_room — would need zone-tagging to roll up.
        # Drop zero/negative slices so the pie doesn't have label collisions
        components = {k: v for k, v in components.items() if v > 0}
        if not components:
            continue

        labels = list(components.keys())
        sizes  = list(components.values())
        colors = [_PALETTE[k.lower()] for k in labels]

        fig, ax = plt.subplots(figsize=(7, 6))
        wedges, texts, autotexts = ax.pie(
            sizes, labels=labels, colors=colors,
            autopct=lambda pct: f"{pct:.0f}%" if pct >= 3 else "",
            startangle=90, textprops={"fontsize": 10},
        )
        for at in autotexts:
            at.set_color("white")
            at.set_fontweight("bold")
        ax.set_title(f"Cooling Load Breakdown\n{cl.location}", fontsize=11)

        path = out_dir / f"cooling_breakdown_{i}.png"
        out_paths.append(_save(fig, path))

    return out_paths


# ──────────────────────────────────────────────────────────────────────
# Chart 3: air balance per zone — supply + OA (Phase-1 only)
# ──────────────────────────────────────────────────────────────────────
def chart_air_balance(report, out_path: Path) -> Path:
    """Grouped bar: Supply CFM vs OA CFM per zone.

    Return and exhaust are Phase-2 computed values and aren't available
    here. When Phase 2 output gets plumbed into the Flask app, extend
    this function to take a ComputedReport instead and add the missing
    series.
    """
    # Build zone → (supply, oa) from supply_air. Supply CFM on a zone row
    # is the required value (Design Master computed sum across rooms).
    # OA is the cooling_osa_cfm column on the same zone row.
    zone_rows = [sa for sa in report.supply_air if _is_zone(sa.location)]
    if not zone_rows:
        return None

    zones = [_shorten_zone(z.location) for z in zone_rows]
    supply = [z.required_supply_cfm or 0 for z in zone_rows]
    oa     = [z.cooling_osa_cfm or 0 for z in zone_rows]

    import numpy as np
    x = np.arange(len(zones))
    width = 0.35

    fig, ax = plt.subplots(figsize=_FIGSIZE)
    bars_s = ax.bar(x - width/2, supply, width, label="Supply CFM", color=_PALETTE["supply"])
    bars_o = ax.bar(x + width/2, oa,     width, label="OA CFM",     color=_PALETTE["oa"])

    for bars in (bars_s, bars_o):
        for b in bars:
            h = b.get_height()
            if h > 0:
                ax.text(b.get_x() + b.get_width() / 2, h,
                        f"{h:,.0f}", ha="center", va="bottom", fontsize=9)

    ax.set_ylabel("CFM")
    ax.set_title("Air Balance: Supply vs Outside Air by Zone")
    ax.set_xticks(x)
    ax.set_xticklabels(zones)
    ax.legend(loc="upper right")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    return _save(fig, out_path)


# ──────────────────────────────────────────────────────────────────────
# Chart 4: top 10 rooms by total cooling btuh
# ──────────────────────────────────────────────────────────────────────
def chart_top_rooms_cooling(report, out_path: Path, top_n: int = 10) -> Path:
    """Horizontal bar chart of top N rooms by total cooling btuh.

    Uses load_total_room, filtered to actual rooms (zones are excluded
    so the chart shows room-level magnitudes, not zone roll-ups).
    """
    rooms = [lt for lt in report.load_total_room if not _is_zone(lt.location)]
    rooms = [r for r in rooms if (r.cool_total_btuh or 0) > 0]
    rooms.sort(key=lambda r: r.cool_total_btuh or 0, reverse=True)
    rooms = rooms[:top_n]
    if not rooms:
        return None

    # Reverse for horizontal bar — biggest at the top
    rooms = list(reversed(rooms))
    labels = [r.location.replace("Room ", "") for r in rooms]
    values = [r.cool_total_btuh for r in rooms]

    fig, ax = plt.subplots(figsize=(10, max(4, 0.4 * len(rooms) + 2)))
    bars = ax.barh(labels, values, color=_PALETTE["sensible"])
    for b, v in zip(bars, values):
        ax.text(v, b.get_y() + b.get_height() / 2, f"  {v:,.0f}",
                va="center", fontsize=9)

    ax.set_xlabel("Cooling Load (BTU/hr)")
    ax.set_title(f"Top {len(rooms)} Rooms by Cooling Load")
    ax.grid(axis="x", linestyle="--", alpha=0.4)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))

    return _save(fig, out_path)


# ──────────────────────────────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────────────────────────────
def render_all_charts(report, out_dir: Path) -> list[Path]:
    """Render every chart for the report. Returns the list of files actually written.

    Skips any chart whose underlying data is empty — the template can
    iterate the returned list and only show what exists.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rendered: list[Path] = []

    p = chart_sensible_vs_latent(report, out_dir / "sensible_vs_latent.png")
    if p: rendered.append(p)

    rendered.extend(chart_cooling_breakdown(report, out_dir))

    p = chart_air_balance(report, out_dir / "air_balance.png")
    if p: rendered.append(p)

    p = chart_top_rooms_cooling(report, out_dir / "top_rooms_cooling.png")
    if p: rendered.append(p)

    return rendered


if __name__ == "__main__":
    # Smoke test against the real HTML
    import runpy
    ns = runpy.run_path("phase1.py")
    out = Path("charts_test")
    rendered = render_all_charts(ns["report"], out)
    print(f"Rendered {len(rendered)} charts:")
    for p in rendered:
        print(f"  {p} ({p.stat().st_size:,} bytes)")
