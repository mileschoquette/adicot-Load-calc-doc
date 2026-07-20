"""Design Master (.dm) setup-script generator.

Renders a standalone VBScript that an engineer downloads and runs next to a
job's ``dm_hvac*.dm`` file. The script inserts the selected construction
schedules (wall/glass/roof/door) and Room Types into the Access/Jet database
via ADO/OLEDB, so the engineer doesn't re-enter them by hand in Design Master.

Design notes (see dm_schema_findings.md for the Phase 0 schema spike):
- Construction schedules and Room Types are all *flat* single-table inserts.
  ``tblRoomS`` Room Types are HVAC-standalone (ixElecRoomS / ixPlumbRoomS are
  always NULL), so no linked rows are needed.
- Room Type enum codes are DM-native and stored verbatim in room_types.json;
  this module replays them, it does not derive them.
- Exhaust and pressure relationship are NOT DM room-type fields, so they are
  not written here (they live elsewhere in DM / drive the app's schedule only).
- Safety behaviors mirror the hand-written reference (setup_pikos.vbs): backup
  before write, OLEDB provider fallback chain, per-insert error counting, plus
  insert-if-not-exists by name (the reference lacked dedup — re-running it
  duplicated types).

The web UI is expected to let the engineer pick *which* Room Types to bring in;
``render_setup_vbs`` renders only the subset of names passed to it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Sequence

_LIBRARY_PATH = Path(__file__).with_name("room_types.json")

# Column order for the tblRoomS INSERT. ixElecRoomS/ixPlumbRoomS are omitted on
# purpose (nullable, always NULL for HVAC-only types). ixRoomS is supplied at
# runtime by the script (MAX+1), so it is not in this list.
_ROOMS_VALUE_COLUMNS = [
    "sName", "bCalculateHeating", "bCalculateCooling",
    "iHeatingTemp", "iCoolingTemp", "iRelativeHumidity",
    "iLtgWattsType", "dLtgWatts",
    "iEquSensibleWattsType", "dEquSensibleWatts", "dEquLatentBTUH",
    "iVentilationCoolingType1", "dVentilationCoolingValue1",
    "iVentilationCoolingType2", "dVentilationCoolingValue2",
    "iVentilationHeatingType1", "dVentilationHeatingValue1",
    "iVentilationHeatingType2", "dVentilationHeatingValue2",
    "iInfiltrationCoolingType", "dInfiltrationCoolingValue",
    "iInfiltrationHeatingType", "dInfiltrationHeatingValue",
    "iPeopleType", "dPeopleValue", "dPeopleSensible", "dPeopleLatent",
    "sGlassZoneType", "iMinSupplyAirType", "dMinSupplyAirValue",
]
_ROOMS_COLUMNS_SQL = "ixRoomS," + ",".join(_ROOMS_VALUE_COLUMNS)


# --------------------------------------------------------------------------- #
# Library loading
# --------------------------------------------------------------------------- #
def load_room_types(path: Path | None = None) -> dict[str, dict]:
    """Return the Room Type library keyed by name. Fails loud on a bad file:
    this JSON is load-bearing, so a missing/corrupt library should surface, not
    silently degrade like the app's external integrations."""
    doc = json.loads((path or _LIBRARY_PATH).read_text())
    return {rt["name"]: rt for rt in doc["room_types"]}


def list_room_types(path: Path | None = None) -> list[dict]:
    """Compact records for populating the selection UI: name, source, summary."""
    out = []
    for rt in load_room_types(path).values():
        out.append({
            "name": rt["name"],
            "source": rt.get("source"),
            "summary": _summarize(rt),
        })
    return out


def _summarize(rt: dict) -> str:
    """One-line human description of a Room Type's key driver (for the UI)."""
    parts = []
    for slot in rt["ventilation_cooling"]:
        parts.append(_rule_text(slot["type"], slot["value"]))
    oa = " + ".join(p for p in parts if p)
    ms = rt["min_supply_air"]
    total = _rule_text(ms["type"], ms["value"], total=True)
    bits = [b for b in (oa and f"OA {oa}", total and f"min {total}") if b]
    return "; ".join(bits) or "no ventilation rule"


def _rule_text(t: int | None, v, total: bool = False) -> str:
    if t == 0:
        return f"{_g(v)} ACH"
    if t == 1:
        return f"{_g(v)} cfm/person"
    if t == 2:
        return f"{_g(v)} cfm/ft²"
    return ""  # 3 = none, 5 = same-as-cooling


def _g(v) -> str:
    if v is None:
        return "0"
    f = float(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


# --------------------------------------------------------------------------- #
# VBScript rendering
# --------------------------------------------------------------------------- #
def _sql_str(s: str) -> str:
    """A SQL string literal for embedding inside a VBS double-quoted string."""
    return "'" + str(s).replace("'", "''") + "'"


def _vbs_str(s: str) -> str:
    """A VBScript string literal (double-quoted, "" to escape a quote). Use for
    values passed as VBS arguments, NOT embedded in SQL — a lone ' would start
    a VBScript comment."""
    return '"' + str(s).replace('"', '""') + '"'


def _sql_val(v) -> str:
    """Format a Python value as a SQL literal token (NULL / number / string)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f == int(f) else repr(f)
    return _sql_str(v)


def _room_values_tokens(rt: dict) -> list[str]:
    """The VALUES tokens for a Room Type, in _ROOMS_VALUE_COLUMNS order,
    excluding the runtime-computed ixRoomS."""
    vc, vh = rt["ventilation_cooling"], rt["ventilation_heating"]
    inf_c, inf_h = rt["infiltration_cooling"], rt["infiltration_heating"]
    ltg, equ, ppl, ms = rt["lighting"], rt["equipment"], rt["people"], rt["min_supply_air"]
    ordered = [
        rt["name"], rt["calculate_heating"], rt["calculate_cooling"],
        rt["heating_temp_f"], rt["cooling_temp_f"], rt["relative_humidity"],
        ltg["type"], ltg["value"],
        equ["sensible_type"], equ["sensible_value"], equ["latent_btuh"],
        vc[0]["type"], vc[0]["value"], vc[1]["type"], vc[1]["value"],
        vh[0]["type"], vh[0]["value"], vh[1]["type"], vh[1]["value"],
        inf_c["type"], inf_c["value"], inf_h["type"], inf_h["value"],
        ppl["type"], ppl["value"], ppl["sensible_btuh"], ppl["latent_btuh"],
        rt["glass_zone_type"], ms["type"], ms["value"],
    ]
    return [_sql_val(v) for v in ordered]


def _insert_block(table: str, ix_col: str, name: str, cols_sql: str,
                  value_tokens: Sequence[str]) -> str:
    """A VBS insert-if-not-exists block. ``ix`` (the new index) is spliced in at
    runtime; the remaining VALUES are baked in at generation time."""
    vals = ",".join(value_tokens)
    sql = (f'"INSERT INTO {table} ({cols_sql}) VALUES (" & ix & ",{vals})"')
    return (
        f'If Not RowExists("{table}", {_vbs_str(name)}) Then\n'
        f'  ix = NextIx("{table}", "{ix_col}")\n'
        f'  s = {sql}\n'
        f'  conn.Execute s\n'
        f'  If Err.Number=0 Then\n'
        f'    nIns=nIns+1 : logf.WriteLine "OK  {table}  " & ix & "  " & {_vbs_str(name)}\n'
        f'  Else\n'
        f'    nErr=nErr+1\n'
        f'    logf.WriteLine "ERR {table}  [" & Err.Number & "] " & Err.Description\n'
        f'    logf.WriteLine "    SQL: " & s\n'
        f'    Err.Clear\n'
        f'  End If\n'
        f'Else\n'
        f'  nSkip=nSkip+1 : logf.WriteLine "SKIP {table}  (exists)  " & {_vbs_str(name)}\n'
        f'End If'
    )


# Editable project-level settings (tblGlobal key/value). friendly field -> (sKey, is_numeric).
# Writing these overwrites the DM project (e.g. renames it / sets the weather station).
PROJECT_SETTING_KEYS = {
    "project_name":    ("bldg-name", False),
    "weather_station": ("bldg-city", False),
    "latitude":        ("bldg-latitude", True),
    "elevation":       ("bldg-elevation", True),
    "osa_low_dry":     ("bldg-osaLowDry", True),
    "osa_daily_range": ("bldg-osaDailyRange", True),
}


def _global_calls(project_settings: dict | None) -> tuple[str, int]:
    """Build the SetGlobalText/SetGlobalNum VBS calls (and their count) for the
    non-empty project settings provided."""
    if not project_settings:
        return "", 0
    lines = []
    for field, (key, is_num) in PROJECT_SETTING_KEYS.items():
        if field not in project_settings:
            continue
        val = project_settings[field]
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        if is_num:
            try:
                f = float(val)
            except (TypeError, ValueError):
                continue
            num = int(f) if f == int(f) else f
            lines.append(f"  SetGlobalNum {_vbs_str(key)}, {num}")
        else:
            lines.append(f"  SetGlobalText {_vbs_str(key)}, {_vbs_str(str(val))}")
    return "\n".join(lines), len(lines)


def render_setup_vbs(
    job_name: str,
    room_type_names: Iterable[str],
    *,
    wall_types: Sequence[dict] = (),
    glass_types: Sequence[dict] = (),
    roof_types: Sequence[dict] = (),
    door_types: Sequence[dict] = (),
    project_settings: dict | None = None,
    library: dict[str, dict] | None = None,
) -> str:
    """Render the setup ``.vbs`` for the selected types.

    ``room_type_names`` is the engineer-selected subset (only these are written).
    Construction-type dicts:
      wall/roof/door: {name, description, u, itype, dark}
      glass:          {name, description, u, shgc}
    ``project_settings`` (optional) maps PROJECT_SETTING_KEYS fields to values to
    overwrite in tblGlobal (project name, weather station, weather numerics).
    """
    lib = library or load_room_types()

    unknown = [n for n in room_type_names if n not in lib]
    if unknown:
        raise KeyError(f"room types not in library: {unknown}")

    blocks: list[str] = []

    def con_block(table, ix_col, cols, t):
        vt = [_sql_val(t["name"]), _sql_val(t.get("description", t["name"])),
              _sql_val(t["u"]), _sql_val(t["itype"]), _sql_val(bool(t["dark"]))]
        blocks.append(_insert_block(table, ix_col, t["name"], cols, vt))

    for t in wall_types:
        con_block("tblWallS", "ixWallS", "ixWallS,sName,sDescription,dU,iType,bDark", t)
    for t in roof_types:
        con_block("tblRoofS", "ixRoofS", "ixRoofS,sName,sDescription,dU,iType,bDark", t)
    for t in door_types:
        con_block("tblDoorS", "ixDoorS", "ixDoorS,sName,sDescription,dU,iType,bDark", t)
    for t in glass_types:
        vt = [_sql_val(t["name"]), _sql_val(t.get("description", t["name"])),
              _sql_val(t["u"]), _sql_val(t["shgc"])]
        blocks.append(_insert_block("tblGlassS", "ixGlassS", t["name"],
                                    "ixGlassS,sName,sDescription,dU,dSHGC", vt))

    for name in room_type_names:
        blocks.append(_insert_block("tblRoomS", "ixRoomS", name,
                                    _ROOMS_COLUMNS_SQL, _room_values_tokens(lib[name])))

    n_con = len(wall_types) + len(glass_types) + len(roof_types) + len(door_types)
    n_room = sum(1 for _ in room_type_names)
    indented = "\n\n".join("  " + b.replace("\n", "\n  ") for b in blocks)
    globals_vbs, n_set = _global_calls(project_settings)
    safe_job = str(job_name).replace('"', "'")

    return _TEMPLATE.format(job=safe_job, n_con=n_con, n_room=n_room, n_set=n_set,
                            globals=globals_vbs, blocks=indented)


# --------------------------------------------------------------------------- #
# Load-calc payload (the SECOND output) — same selection, shaped for Adicot's
# own RTS load calc + review UI instead of Design Master. Mirrors the
# render_setup_vbs inputs 1:1, but emits the FULL Room Type records (not just
# names) so the calc has the ventilation/lighting/people data, and construction
# types verbatim. One source drives both DM and our engine.
# --------------------------------------------------------------------------- #
def build_loadcalc_payload(
    job_name: str,
    room_type_names: Iterable[str],
    *,
    wall_types: Sequence[dict] = (),
    glass_types: Sequence[dict] = (),
    roof_types: Sequence[dict] = (),
    door_types: Sequence[dict] = (),
    project_settings: dict | None = None,
    library: dict[str, dict] | None = None,
) -> dict:
    lib = library or load_room_types()
    unknown = [n for n in room_type_names if n not in lib]
    if unknown:
        raise KeyError(f"room types not in library: {unknown}")
    return {
        "job": str(job_name),
        "project_settings": dict(project_settings or {}),
        "room_types": [lib[n] for n in room_type_names],
        "wall_types": [dict(t) for t in wall_types],
        "roof_types": [dict(t) for t in roof_types],
        "door_types": [dict(t) for t in door_types],
        "glass_types": [dict(t) for t in glass_types],
    }


def render_setup_json(job_name: str, room_type_names: Iterable[str], **kwargs) -> str:
    """JSON string of build_loadcalc_payload — the load-calc counterpart to
    render_setup_vbs. Same args, so a route can render both from one selection."""
    return json.dumps(build_loadcalc_payload(job_name, room_type_names, **kwargs),
                      indent=2)


_TEMPLATE = r'''' Design Master setup script for: {job}
' Generated by Adicot Engineering (dm_setup_generator). Close the drawing in
' AutoCAD before running. Inserts {n_con} construction type(s) and {n_room}
' Room Type(s). Existing types with the same name are skipped (safe to re-run).
Option Explicit
Dim fso, conn, f, fld, dmFile, ix, nIns, nSkip, nErr, nSet, s, logf
nIns=0 : nSkip=0 : nErr=0 : nSet=0

Set fso = CreateObject("Scripting.FileSystemObject")
Set fld = fso.GetFolder(fso.GetParentFolderName(WScript.ScriptFullName))
dmFile = ""
For Each f In fld.Files
  If LCase(Left(f.Name,7))="dm_hvac" And LCase(Right(f.Name,3))=".dm" Then dmFile=f.Path
Next
If dmFile="" Then MsgBox "No dm_hvac*.dm found in this folder." : WScript.Quit 1

If MsgBox("Add {n_con} construction type(s), {n_room} Room Type(s), and update {n_set} project setting(s) in:" & vbCrLf & _
    dmFile & "?" & vbCrLf & vbCrLf & "A backup is made first. Close the drawing in AutoCAD.", _
    vbYesNo+vbQuestion, "DM Setup: {job}") <> vbYes Then WScript.Quit 0

fso.CopyFile dmFile, dmFile & ".setup_backup.bak", True

Set logf = fso.CreateTextFile(fso.GetParentFolderName(dmFile) & "\dm_setup_log.txt", True)
logf.WriteLine "DM Setup log - " & Now
logf.WriteLine dmFile
logf.WriteLine "--------------------------------------------------"

Set conn = CreateObject("ADODB.Connection")
On Error Resume Next
conn.Open "Provider=Microsoft.ACE.OLEDB.16.0;Data Source=" & dmFile
If Err.Number<>0 Then Err.Clear : conn.Open "Provider=Microsoft.ACE.OLEDB.12.0;Data Source=" & dmFile
If Err.Number<>0 Then Err.Clear : conn.Open "Provider=Microsoft.Jet.OLEDB.4.0;Data Source=" & dmFile
If Err.Number<>0 Then MsgBox "No Access OLEDB provider available." & vbCrLf & _
    "Run with 32-bit cscript, or install the Access Database Engine.", vbCritical : WScript.Quit 1
On Error GoTo 0

Function NextIx(tbl, ixcol)
  Dim r, v
  Set r = conn.Execute("SELECT MAX(" & ixcol & ") FROM " & tbl)
  v = r(0).Value : r.Close
  If IsNull(v) Then v = 0
  NextIx = v + 1
End Function

Function RowExists(tbl, nm)
  Dim r
  Set r = conn.Execute("SELECT COUNT(*) FROM " & tbl & " WHERE sName='" & Replace(nm,"'","''") & "'")
  RowExists = (r(0).Value > 0) : r.Close
End Function

' Overwrite (or insert) a project-level setting in tblGlobal.
Sub SetGlobalText(k, v)
  Dim c, gix : On Error Resume Next : Err.Clear
  c = conn.Execute("SELECT COUNT(*) FROM tblGlobal WHERE sKey='" & Replace(k,"'","''") & "'")(0).Value
  If c > 0 Then
    conn.Execute "UPDATE tblGlobal SET bNumeric=0, sText='" & Replace(v,"'","''") & "' WHERE sKey='" & Replace(k,"'","''") & "'"
  Else
    gix = NextIx("tblGlobal","ixGlobal")
    conn.Execute "INSERT INTO tblGlobal (ixGlobal,sKey,bNumeric,sText) VALUES (" & gix & ",'" & Replace(k,"'","''") & "',0,'" & Replace(v,"'","''") & "')"
  End If
  If Err.Number=0 Then nSet=nSet+1 : logf.WriteLine "SET tblGlobal  " & k & " = " & v Else nErr=nErr+1 : logf.WriteLine "ERR tblGlobal  " & k & " [" & Err.Number & "] " & Err.Description : Err.Clear
End Sub

Sub SetGlobalNum(k, v)
  Dim c, gix : On Error Resume Next : Err.Clear
  c = conn.Execute("SELECT COUNT(*) FROM tblGlobal WHERE sKey='" & Replace(k,"'","''") & "'")(0).Value
  If c > 0 Then
    conn.Execute "UPDATE tblGlobal SET bNumeric=1, dNumber=" & v & " WHERE sKey='" & Replace(k,"'","''") & "'"
  Else
    gix = NextIx("tblGlobal","ixGlobal")
    conn.Execute "INSERT INTO tblGlobal (ixGlobal,sKey,bNumeric,dNumber) VALUES (" & gix & ",'" & Replace(k,"'","''") & "',1," & v & ")"
  End If
  If Err.Number=0 Then nSet=nSet+1 : logf.WriteLine "SET tblGlobal  " & k & " = " & v Else nErr=nErr+1 : logf.WriteLine "ERR tblGlobal  " & k & " [" & Err.Number & "] " & Err.Description : Err.Clear
End Sub

On Error Resume Next
{globals}
{blocks}
On Error GoTo 0

conn.Close
logf.WriteLine "--------------------------------------------------"
logf.WriteLine "Inserted " & nIns & ", Skipped " & nSkip & ", Settings " & nSet & ", Errors " & nErr
logf.Close
MsgBox "Done." & vbCrLf & _
  "Inserted: " & nIns & vbCrLf & _
  "Skipped (already present): " & nSkip & vbCrLf & _
  "Settings updated: " & nSet & vbCrLf & _
  "Errors: " & nErr & vbCrLf & vbCrLf & _
  "Log: " & fso.GetParentFolderName(dmFile) & "\dm_setup_log.txt" & vbCrLf & _
  "Backup: " & dmFile & ".setup_backup.bak", vbInformation, "DM Setup complete"
'''
