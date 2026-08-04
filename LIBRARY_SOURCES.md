# Room Type Library — Sources & Provenance

`room_types.json` is the reusable Room Type library that feeds the **DM Setup Generator**
(`dm_setup_generator.py`). It contains **322 room types** drawn from exactly three code sources —
nothing else (the earlier DM-verified job types and any other entries were removed on request).

## Naming convention

Each name **encodes the ventilation** and **ends with the building code**, so the code and the
governing rate are readable at a glance (and parseable downstream):

| Source | Pattern | Examples |
|---|---|---|
| `621` / `FBC` (cfm-based) | `<space> <Rp>/<Ra> [*<exh> Exh] (CODE)` | `Office spaces 5/0.06 (FBC)` · `Art classroom 10/0.18 (621)` · `Toilet rooms - public *50/70 Exh (FBC)` · `Science laboratories 10/0.18 *1.0 Exh (FBC)` |
| `170` (ACH-based) | `<space> <outdoorACH>/<totalACH> ACH [Exh] (170)` | `Operating room 4/20 ACH (170)` · `Examination/observation 2/4 ACH (170)` · `AII room 2/12 ACH Exh (170)` |

- `Rp` = outdoor air cfm/person, `Ra` = outdoor air cfm/ft² (0 shown when a rate isn't required).
- `*<n> Exh` = exhaust: cfm/ft² (e.g. `*0.7 Exh`) or per-unit (e.g. `*50/70 Exh`, intermittent/continuous).
- `170` ACH pair is `min outdoor / min total`; trailing `Exh` marks rooms whose air is fully exhausted.

## Where the rooms come from

| `source` | Count | Table (edition) | Cross-checked against |
|---|--:|---|---|
| `621` | 89 | ASHRAE **62.1-2022** Table 6-1 | 62.1-2022 base PDF + Addendum ab |
| `FBC` | 92 | **FBC 2023** Mechanical Table 403.3.1.1 (= IMC 2021) | the pasted FBC table (authoritative) + IMC 2021 |
| `170` | 141 | ASHRAE **170-2021** Table 7-1 (inpatient) + 8-1/8-2 (outpatient) | Mann+Hummel 170-2021 + ASHRAE Addenda p/j + May-2024 errata |

Cross-reference-only rows (`"… (see …)"`) are dropped. 17 names that appear identically in both the
170 inpatient and outpatient tables (same space + same ACH) are de-duplicated.

## Field provenance — code-sourced vs defaulted

**Code-sourced:** outdoor-air rate (Rp/Ra or outdoor ACH), total ACH (170) → `min_supply_air`,
occupant density → ft²/person, design temp/RH range (170, kept as `code_temp_f`/`code_rh_pct`
metadata), pressure relationship (170) and exhaust (FBC) → metadata only.

**Defaulted (NOT in the code tables — see `_defaults`):** lighting `0.5 W/ft²`, equipment `0`,
people sensible/latent `250 / 200 BTU/h`, infiltration `0.25 ACH`. Review per project.

## How fields map into Design Master (`tblRoomS`)

`pressure_relationship` and `exhaust` have no DM room-type column (metadata only). DM temp/RH are
left NULL (inherit from zone). 170 imported types have people = 0 (no density in the 170 tables).
Enum legend is in the JSON header.

## Downstream note — name-based ventilation parsing

The room name now carries the ventilation (`Rp/Ra`, `outdoor/total ACH`, `*exh Exh`). For the app to
**calculate ventilation from these names** on re-import, `hvac_pipeline`'s space-type resolver
(`resolve_space_type` / OA-rule + `parse_exhaust_rule`) must understand this grammar — the older
scheme expected canonical table names with rules like `"2 ACH"`/`"0.06"`. Teaching the parser the
`Rp/Ra` and `outdoor/total ACH` forms is a required follow-up if you rely on name-based calc.

## Regenerating

Built deterministically from the three source extracts by a mapping script (session scratchpad
`rebuild.py`). `dm_setup_generator.load_room_types()` fails loud on a malformed file.
