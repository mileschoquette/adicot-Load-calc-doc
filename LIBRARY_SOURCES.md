# Room Type Library — Sources & Provenance

`room_types.json` is the reusable Room Type library that feeds the **DM Setup Generator**
(`dm_setup_generator.py`) and can back the app's space-type resolution. It holds **356 unique
room types**. This document records where every value came from and — importantly — which fields
are code-sourced versus which are engineering defaults.

## Where the types come from

| `source` tag | Types | Standard / table (edition) | Cross-checked against |
|---|--:|---|---|
| `170` | 161 | ASHRAE/ASHE **170-2021** Table 7-1 (inpatient) + Tables 8-1/8-2 (outpatient) | Mann+Hummel 170-2021 reproduction; ASHRAE Addenda p & j to 170-2017; ASHRAE 170-2021 errata (May 10 2024) |
| `FBC` | 99 | **FBC 2023** Mechanical Table 403.3.1.1 (= IMC 2021) | UpCodes FBC Mechanical 2023; IMC 2021 (row-by-row) |
| `621` | 92 | ASHRAE **62.1-2022** Table 6-1 | 62.1-2022 base standard PDF; ASHRAE Addendum ab |
| DM-verified | 18 | Real job file `dm_hvac.dm` `tblRoomS` | the Access DB itself (via mdbtools) |

Each row carries a `code_ref` string (e.g. `"ASHRAE 170-2021 Table 7-1"`) and an `origin` tag
(`dm-verified` = exact DM codes with real load values; `code-import` = mapped from the standard).
Every value was cross-checked by research against **two independent authoritative sources**, with
disagreements flagged rather than silently resolved (see Caveats).

> **170-2021 note:** the 2021 edition split the old single table — Table 7-1 is inpatient only;
> outpatient spaces (dental, exam, urgent care) moved to Tables 8-1/8-2. All are included.

## Field provenance — code-sourced vs defaulted

**Code-sourced (authoritative, per each row's `code_ref`):**
- Ventilation outdoor air — 62.1/FBC: `Rp` cfm/person + `Ra` cfm/ft²; 170: min outdoor ACH.
- Minimum total air changes (170 only) → `min_supply_air`.
- Occupant density (62.1/FBC) → converted to ft²/person.
- Design temperature / RH range (170) → kept as `code_temp_f` / `code_rh_pct` metadata.
- Pressure relationship (170) and exhaust rate (FBC) → library metadata only.

**Defaulted — NOT specified by the ventilation tables (see `_defaults` in the JSON):**
- Lighting: `0.5 W/ft²`
- Equipment: `0` (add nameplate loads per project, e.g. a sterilizer)
- People sensible / latent: `250 / 200 BTU/h` per person (seated / light-work default)
- Infiltration: `0.25 ACH`

Review the defaulted fields per project — they affect DM load calcs.

## How fields map into Design Master (`tblRoomS`)

- `pressure_relationship` and `exhaust` have **no DM room-type column** — they are metadata for the
  app's ventilation schedule and are **not** written by the generator.
- DM temp/RH columns (`iCoolingTemp`/`iHeatingTemp`/`iRelativeHumidity`) are left **NULL** so the
  room inherits from its zone/building (matching how the real job file's types behaved). The code's
  design range lives in `code_temp_f` / `code_rh_pct` for reference.
- 170 imported types have **people = 0** (the 170 tables give no occupant density); set per room.

Enum legend (also in the JSON header): ventilation type `0=ACH, 1=CFM/person, 2=CFM/ft², 3=none,
5=same-as-cooling`; `min_supply_air`/infiltration `0=ACH, 3=none`; people `0=count, 1=ft²/person`;
lighting `1=W/ft²`; equipment sensible `0=total W, 1=W/ft²`.

## Known caveats / disagreements

- **ASHRAE 170 burn unit (wound intensive care) pressure:** used the 2021 value **positive**; an
  earlier draft addendum tabled it as NR. Confirm against a purchased 170-2021 copy — it's a
  critical space.
- **170 recirculation column** is single-sourced for a few reworded inpatient rows.
- Post-2021 ASHRAE 170 addenda (o, p, t, w) are **excluded** (edition = 2021 as requested); the
  May-2024 errata corrections **are** applied.
- 62.1 §6.2.1 defers healthcare occupancies to Standard 170, so the `621` set intentionally
  contains no patient rooms.
- 62.1-2022 Addendum-ab-only row "Educational Facilities — Corridors (age 5 plus)" was **excluded**
  (not in the 2022 base print).

## Regenerating / updating

The library was assembled deterministically from per-standard JSON extracts by a mapping script
(`assemble.py`, kept in the session scratchpad). To change editions or defaults, re-run that
mapping against fresh table extracts. `dm_setup_generator.load_room_types()` fails loud on a
malformed file — this JSON is load-bearing for signed output.
