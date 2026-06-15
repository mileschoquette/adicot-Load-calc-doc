"""Combine the three deliverable PDFs into one, with optional chart pages.

The pipeline writes three standalone PDFs into a job's out/ directory:

    <prefix>-Ventilation.pdf
    <prefix>-Air_Balance.pdf
    <prefix>-Load.pdf

build_combined_pdf() concatenates those three (in the order Load, Air Balance,
Ventilation) into

    <prefix>-Combined.pdf

and appends one landscape-letter page per selected chart, scaled to fit with
its caption beneath it. The merge reuses the already-rendered deliverable pages
verbatim (via PyMuPDF insert_pdf), so the combined copy is byte-identical to the
standalones — no re-rendering, no risk of drift.

Pure logic — no Flask, no Drive, no network. app.py calls build_combined_pdf().
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

import fitz  # PyMuPDF

# Deliverable suffixes, in the order they should appear in the combined PDF.
_DELIVERABLE_SUFFIXES = ("-Load.pdf", "-Air_Balance.pdf", "-Ventilation.pdf")
_COMBINED_SUFFIX = "-Combined.pdf"

# US Letter, landscape (points: 1 in = 72 pt).
_PAGE_W, _PAGE_H = 792.0, 612.0
_MARGIN = 40.0
_CAPTION_H = 28.0


def _find_deliverables(out_dir: Path) -> tuple[Optional[str], list[Path]]:
    """Return (prefix, ordered_paths) for the deliverable PDFs present in out_dir.

    prefix is taken from the first deliverable found (the filename with its
    suffix stripped). ordered_paths follows _DELIVERABLE_SUFFIXES order, skipping
    any that are missing.
    """
    prefix: Optional[str] = None
    paths: list[Path] = []
    for suffix in _DELIVERABLE_SUFFIXES:
        matches = sorted(out_dir.glob(f"*{suffix}"))
        if not matches:
            continue
        chosen = matches[0]
        paths.append(chosen)
        if prefix is None:
            prefix = chosen.name[: -len(suffix)]
    return prefix, paths


def build_combined_pdf(out_dir: Path,
                       charts: Iterable[tuple[Path, str]] = ()) -> Optional[Path]:
    """Merge the deliverable PDFs in out_dir into <prefix>-Combined.pdf and append
    one page per chart.

    Args:
        out_dir: the job's out/ directory containing the deliverable PDFs.
        charts:  ordered iterable of (png_path, caption); each becomes one page.

    Returns the path to the combined PDF, or None if no deliverable PDFs exist.
    """
    out_dir = Path(out_dir)
    prefix, parts = _find_deliverables(out_dir)
    if not parts:
        return None

    combined_path = out_dir / f"{prefix}{_COMBINED_SUFFIX}"

    doc = fitz.open()
    try:
        for p in parts:
            with fitz.open(p) as src:
                doc.insert_pdf(src)

        for png_path, caption in charts:
            png_path = Path(png_path)
            if not png_path.exists():
                continue
            page = doc.new_page(width=_PAGE_W, height=_PAGE_H)
            img_rect = fitz.Rect(_MARGIN, _MARGIN,
                                 _PAGE_W - _MARGIN,
                                 _PAGE_H - _MARGIN - _CAPTION_H)
            try:
                page.insert_image(img_rect, filename=str(png_path),
                                  keep_proportion=True)
            except Exception:
                # A bad/missing image shouldn't sink the whole combined PDF.
                continue
            cap_rect = fitz.Rect(_MARGIN, _PAGE_H - _MARGIN - _CAPTION_H,
                                 _PAGE_W - _MARGIN, _PAGE_H - _MARGIN)
            page.insert_textbox(cap_rect, caption or "",
                                fontsize=12, fontname="helv",
                                align=fitz.TEXT_ALIGN_CENTER)

        doc.save(combined_path)
    finally:
        doc.close()

    return combined_path
