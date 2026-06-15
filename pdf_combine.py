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

import os
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


def pdf_page_count(path: Path) -> Optional[int]:
    """Page count of a PDF, or None if it can't be read."""
    try:
        with fitz.open(path) as d:
            return d.page_count
    except Exception:
        return None


def append_pdf_to_file(target_path: Path, appendix: Optional[bytes]) -> bool:
    """Append appendix PDF bytes to the end of target_path in place.

    Saves to a temp file then atomically replaces the target, so a failure mid-
    write can't corrupt the original. Returns True on success. Never raises."""
    target_path = Path(target_path)
    if not appendix or not target_path.exists():
        return False
    try:
        doc = fitz.open(target_path)
        try:
            with fitz.open(stream=appendix, filetype="pdf") as ap:
                doc.insert_pdf(ap)
            tmp = target_path.with_suffix(".tmp.pdf")
            doc.save(tmp)
        finally:
            doc.close()
        os.replace(tmp, target_path)
        return True
    except Exception:
        return False


def build_combined_pdf(out_dir: Path,
                       charts: Iterable[tuple[Path, str]] = (),
                       appendix: Optional[bytes] = None,
                       load_pages: Optional[int] = None) -> Optional[Path]:
    """Merge the deliverable PDFs in out_dir into <prefix>-Combined.pdf, append
    one page per chart, then (optionally) append the HTML appendix at the very end.

    Args:
        out_dir:    the job's out/ directory containing the deliverable PDFs.
        charts:     ordered iterable of (png_path, caption); each becomes one page.
        appendix:   optional PDF bytes (the rendered DM HTML) appended dead last.
        load_pages: if set, the clean page count of the Load deliverable. The
                    standalone -Load.pdf on disk may already carry the appendix;
                    we insert only its first `load_pages` pages here so the
                    appendix appears once, at the end of the combined.

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
                if load_pages and p.name.endswith("-Load.pdf"):
                    last = min(load_pages, src.page_count) - 1
                    doc.insert_pdf(src, from_page=0, to_page=last)
                else:
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

        # HTML appendix dead last, after the charts.
        if appendix:
            try:
                with fitz.open(stream=appendix, filetype="pdf") as ap:
                    doc.insert_pdf(ap)
            except Exception:
                pass

        doc.save(combined_path)
    finally:
        doc.close()

    return combined_path
