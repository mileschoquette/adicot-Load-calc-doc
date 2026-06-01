"""Specification Word Doc builder.

Renders a spec_engine.RenderedSpec to a formatted .docx using python-docx.
Notes are stripped (pass include_notes=False from the route, same as DXF).

Add to requirements.txt:
    python-docx
"""

from __future__ import annotations
from pathlib import Path

from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement


def _add_bottom_border(paragraph):
    """Add a light grey bottom rule under a paragraph (section heading style)."""
    pPr = paragraph._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "4")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "CCCCCC")
    pBdr.append(bottom)
    pPr.append(pBdr)


def build_specification_docx(rendered_spec, out_path: Path,
                              project_name: str,
                              project_address: str,
                              code_label: str) -> Path:
    """Write a formatted .docx spec to out_path and return out_path."""

    doc = Document()

    # ── Page margins: 1 inch all around ──────────────────────────────
    for section in doc.sections:
        section.top_margin    = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin   = Inches(1)
        section.right_margin  = Inches(1)

    # ── Title block ───────────────────────────────────────────────────
    title_p = doc.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title_p.add_run("MECHANICAL SPECIFICATIONS")
    title_run.bold = True
    title_run.font.size = Pt(14)

    if code_label:
        sub_p = doc.add_paragraph()
        sub_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sub_p.add_run(f"{code_label}  \u00b7  Division 23").font.size = Pt(10)

    proj_line = project_name
    if project_address:
        proj_line += f"  \u2014  {project_address}"
    addr_p = doc.add_paragraph()
    addr_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    addr_p.add_run(proj_line).font.size = Pt(10)

    doc.add_paragraph()  # blank line after title block

    # ── Spec content ──────────────────────────────────────────────────
    for part in rendered_spec.parts:
        for section in part.sections:

            # Section heading
            sec_p = doc.add_paragraph()
            sec_run = sec_p.add_run(f"{section.num}  {section.title}")
            sec_run.bold = True
            sec_run.font.size = Pt(10)
            sec_run.font.color.rgb = RGBColor(0x1A, 0x3A, 0x5C)
            _add_bottom_border(sec_p)

            # Clauses
            for clause in section.clauses:
                c_p = doc.add_paragraph(style="Normal")
                c_p.paragraph_format.left_indent        = Inches(0.4)
                c_p.paragraph_format.first_line_indent  = Inches(-0.4)
                c_p.paragraph_format.space_after        = Pt(3)

                label_run = c_p.add_run(f"{clause.label}  ")
                label_run.bold = True
                label_run.font.size = Pt(9)

                text_run = c_p.add_run(clause.text)
                text_run.font.size = Pt(9)

            doc.add_paragraph()  # blank line between sections

    doc.save(str(out_path))
    return out_path
