"""Specification DXF builder — AutoCAD MTEXT output (clean / bulletproof).

Renders a spec_engine.RenderedSpec to a .dxf with ONE MTEXT object the engineer
inserts onto a sheet, then grips to resize / reflow.

Formatting philosophy: NO font codes, NO literal tabs (both proved unreliable
across AutoCAD versions). Instead:
  - Section headings stand out by ALL-CAPS + blank line (they already are caps).
  - Clause hanging indent uses MTEXT's paragraph indent code \\pi/\\pl, which is
    the reliable mechanism; the clause letter is followed by spaces, not a tab.
The engineer applies an office text style for bold headings if desired.

Pure output module — no Flask. Requires: ezdxf.
"""

from __future__ import annotations

from pathlib import Path

import ezdxf


_TEXT_HEIGHT = 0.10        # drawing units (inches at 1:1); scale on insert
_BOX_WIDTH   = 7.0         # initial MTEXT reference width; grip to change
_LINE_SPACE  = 1.30
_HANG        = 0.28        # hanging-indent depth (units)


def _esc(text: str) -> str:
    r"""Escape MTEXT specials: backslash and braces only (no font codes used)."""
    return (str(text)
            .replace("\\", "\\\\")
            .replace("{", "\\{")
            .replace("}", "\\}"))


def _mtext_content(rendered_spec, project_name, project_address, code_label) -> str:
    r"""Full MTEXT string. \\P = paragraph break. Hanging indent via \\pi/\\pl."""
    P = "\\P"
    parts = []

    # Title block — ALL CAPS, no font codes
    parts.append(_esc("MECHANICAL SPECIFICATIONS"))
    if code_label:
        parts.append(_esc(code_label) + "  \u00b7 Division 23")
    line = _esc(project_name)
    if project_address:
        line += "  \u2014  " + _esc(project_address)
    parts.append(line)
    parts.append("")  # blank line

    for part in rendered_spec.parts:
        for section in part.sections:
            parts.append(_esc(section.title))      # heading (caps, own line)
            for clause in section.clauses:
                # Hanging indent: first line at 0, wrapped lines indented _HANG.
                # \pi (first-line indent) 0, \pl (left/hanging) _HANG.
                # Letter + two spaces, no tab.
                body = (f"\\pi0,l{_HANG};"
                        + _esc(clause.label) + "  " + _esc(clause.text)
                        + "\\pi0,l0;")
                parts.append(body)
                if clause.note:
                    parts.append(_esc("SPEC NOTE: " + clause.note))
            parts.append("")  # blank line between sections

    return P.join(parts)


def build_specification_dxf(rendered_spec, out_path: Path,
                            project_name: str,
                            project_address: str,
                            code_label: str,
                            text_height: float = _TEXT_HEIGHT,
                            box_width: float = _BOX_WIDTH) -> Path:
    """Write a .dxf with one MTEXT spec block on layer SPEC.

    rendered_spec : spec_engine.RenderedSpec (include_notes=False for sheet copy).
    """
    doc = ezdxf.new("R2018", setup=True)
    msp = doc.modelspace()

    if "SPEC" not in doc.layers:
        doc.layers.add("SPEC", color=7)

    content = _mtext_content(rendered_spec, project_name, project_address, code_label)

    mtext = msp.add_mtext(content, dxfattribs={
        "layer": "SPEC",
        "char_height": text_height,
        "width": box_width,
        "attachment_point": 1,   # top-left
        "line_spacing_factor": _LINE_SPACE,
    })
    mtext.set_location(insert=(0, 0))

    doc.saveas(str(out_path))
    return out_path
