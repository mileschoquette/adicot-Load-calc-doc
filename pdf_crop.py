"""PDF snippet cropper for the Adicot intake pipeline.

Apps Script extracts work-order fields from a client drawing PDF and, for each
field it located, records WHERE on the page it sits as a normalized box:

    "_sources": { "roofRValue": { "page": 2, "box": [x, y, w, h] }, ... }

box = [x, y, w, h] as fractions of the page (0.0-1.0), origin top-left.

This module renders the cited pages with PyMuPDF and crops one small JPEG per
box. The model's boxes are DIRECTIONALLY right (correct region) but
DIMENSIONALLY imprecise (a vision model can't measure to 1%), so every box is
PADDED generously before cropping — a loose-but-complete crop beats a tight one
that clips the value. Boxes that land on the same spot are deduped to one image.

Pure logic — no Flask, no Drive, no network. app.py's /crop route calls
crop_sources() and hands the base64 results back to Apps Script, which uploads
them to the project's Drive folder.
"""

from __future__ import annotations

import base64
import io
from typing import Optional

import fitz  # PyMuPDF


# ── Tunables ─────────────────────────────────────────────────────────────────

DPI = 150                  # render resolution; matches the intake gotcha note
PAD_W = 0.40               # widen each box by this fraction of its width (each side gets half)
PAD_H = 0.60               # heighten each box by this fraction of its height
MAX_WIDTH_PX = 500         # cap crop width; tall crops scale to keep aspect
JPEG_QUALITY = 58          # ~55-60% per the intake gotcha note
MIN_BOX = 0.005            # ignore degenerate boxes smaller than this fraction
DEDUP_TOL = 0.02           # boxes whose padded corners are within this (page-fraction) are "the same"


# ── Box math ─────────────────────────────────────────────────────────────────

def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _pad_box(box):
    """Return a padded (x, y, w, h) in page fractions, clamped to the page.

    Padding turns an imprecise center-hint into a crop that still contains the
    whole note even if the model's box was too tight or slightly off.
    """
    x, y, w, h = box
    px = w * PAD_W / 2.0
    py = h * PAD_H / 2.0
    nx = _clamp(x - px)
    ny = _clamp(y - py)
    nx2 = _clamp(x + w + px)
    ny2 = _clamp(y + h + py)
    return (nx, ny, nx2 - nx, ny2 - ny)


def _valid_box(box) -> bool:
    if not (isinstance(box, (list, tuple)) and len(box) == 4):
        return False
    try:
        x, y, w, h = (float(v) for v in box)
    except (TypeError, ValueError):
        return False
    if w < MIN_BOX or h < MIN_BOX:
        return False
    if x < -0.05 or y < -0.05 or x > 1.05 or y > 1.05:
        return False
    return True


def _dedup_key(page: int, padded) -> tuple:
    """Quantize a padded box so near-identical boxes collapse to one crop."""
    x, y, w, h = padded
    q = lambda v: round(v / DEDUP_TOL)
    return (page, q(x), q(y), q(w), q(h))


# ── Rendering ────────────────────────────────────────────────────────────────

def _render_page(doc, page_index: int):
    """Render one page to a PyMuPDF Pixmap at DPI. page_index is 0-based."""
    page = doc.load_page(page_index)
    zoom = DPI / 72.0
    return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)


def _crop_to_jpeg(pix, padded) -> bytes:
    """Crop a padded (x,y,w,h) page-fraction box out of a rendered Pixmap and
    return JPEG bytes, scaled so width <= MAX_WIDTH_PX."""
    x, y, w, h = padded
    L = int(round(x * pix.width))
    T = int(round(y * pix.height))
    R = int(round((x + w) * pix.width))
    B = int(round((y + h) * pix.height))
    L, T = max(0, L), max(0, T)
    R, B = min(pix.width, R), min(pix.height, B)
    if R <= L or B <= T:
        return b""

    # Crop via an intersected Pixmap, then optionally downscale.
    clip = fitz.IRect(L, T, R, B)
    sub = fitz.Pixmap(pix, pix.width, pix.height, clip)  # copy restricted to clip

    crop_w = R - L
    if crop_w > MAX_WIDTH_PX:
        factor = MAX_WIDTH_PX / crop_w
        new_w = MAX_WIDTH_PX
        new_h = max(1, int(round((B - T) * factor)))
        # shrink: render sub to a scaled pixmap
        sub.shrink(max(1, round(crop_w / MAX_WIDTH_PX)))

    return sub.tobytes(output="jpeg", jpg_quality=JPEG_QUALITY)


# ── Public entry point ───────────────────────────────────────────────────────

def crop_sources(pdf_bytes: bytes, sources: dict,
                 only_fields: Optional[list] = None) -> dict:
    """Crop one JPEG per source box.

    pdf_bytes   : the raw client drawing PDF.
    sources     : the _sources object {field: {page, box}}.
    only_fields : optional whitelist — crop only these fields (e.g. the fields
                  that survived into the final CMS record). Others are skipped.

    Returns:
      {
        "ok": bool,
        "crops": { field: { "b64": <jpeg base64>, "page": <1-based> } },
        "shared": { field: field },   # field -> the field whose crop it reuses
        "errors": [ {field, message} ],
        "page_count": int,
      }
    """
    out = {"ok": False, "crops": {}, "shared": {}, "errors": [],
           "page_count": 0}

    if not sources or not isinstance(sources, dict):
        out["errors"].append({"field": None, "message": "no _sources provided"})
        return out

    wl = set(only_fields) if only_fields else None

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        out["errors"].append({"field": None, "message": f"open failed: {e}"})
        return out

    out["page_count"] = doc.page_count
    rendered: dict = {}        # page_index -> Pixmap (lazy, reused)
    by_key: dict = {}          # dedup_key -> first field that produced it

    for field, src in sources.items():
        if wl is not None and field not in wl:
            continue
        if not isinstance(src, dict):
            out["errors"].append({"field": field, "message": "source not an object"})
            continue
        box = src.get("box")
        page_1 = src.get("page", 1)
        try:
            page_1 = int(page_1)
        except (TypeError, ValueError):
            page_1 = 1
        if not _valid_box(box):
            out["errors"].append({"field": field, "message": f"bad box {box}"})
            continue
        page_idx = page_1 - 1
        if page_idx < 0 or page_idx >= doc.page_count:
            out["errors"].append({"field": field,
                                  "message": f"page {page_1} out of range (doc has {doc.page_count})"})
            continue

        padded = _pad_box(box)
        key = _dedup_key(page_idx, padded)
        if key in by_key:
            out["shared"][field] = by_key[key]   # reuse the earlier field's crop
            continue

        if page_idx not in rendered:
            try:
                rendered[page_idx] = _render_page(doc, page_idx)
            except Exception as e:
                out["errors"].append({"field": field, "message": f"render page {page_1}: {e}"})
                continue

        try:
            jpeg = _crop_to_jpeg(rendered[page_idx], padded)
        except Exception as e:
            out["errors"].append({"field": field, "message": f"crop failed: {e}"})
            continue
        if not jpeg:
            out["errors"].append({"field": field, "message": "empty crop"})
            continue

        out["crops"][field] = {
            "b64": base64.b64encode(jpeg).decode("ascii"),
            "page": page_1,
        }
        by_key[key] = field

    doc.close()
    out["ok"] = bool(out["crops"])
    return out


def overlay_pages(pdf_bytes: bytes, sources: dict,
                  only_fields: Optional[list] = None) -> dict:
    """Debug aid: render each cited page full-size with every (padded) box drawn
    as a red rectangle and the field name labeled. Lets you SEE whether boxes
    land on the right spot on the real drawing before trusting the crops.

    Returns { "ok": bool, "pages": { page_1based: <jpeg base64> }, "errors": [] }
    """
    out = {"ok": False, "pages": {}, "errors": []}
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        out["errors"].append({"field": None, "message": f"open failed: {e}"})
        return out

    wl = set(only_fields) if only_fields else None
    # group boxes by page
    per_page: dict = {}
    for field, src in (sources or {}).items():
        if wl is not None and field not in wl:
            continue
        if not isinstance(src, dict):
            continue
        box = src.get("box")
        if not _valid_box(box):
            continue
        try:
            page_1 = int(src.get("page", 1))
        except (TypeError, ValueError):
            page_1 = 1
        per_page.setdefault(page_1, []).append((field, box))

    for page_1, items in per_page.items():
        idx = page_1 - 1
        if idx < 0 or idx >= doc.page_count:
            continue
        page = doc.load_page(idx)
        rect = page.rect
        for field, box in items:
            px, py, pw, ph = _pad_box(box)
            r = fitz.Rect(px * rect.width, py * rect.height,
                          (px + pw) * rect.width, (py + ph) * rect.height)
            page.draw_rect(r, color=(1, 0, 0), width=1.5)
            page.insert_text(fitz.Point(r.x0 + 2, max(8, r.y0 - 3)),
                             field, fontsize=7, color=(1, 0, 0))
        zoom = DPI / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        out["pages"][page_1] = base64.b64encode(
            pix.tobytes(output="jpeg", jpg_quality=70)).decode("ascii")

    doc.close()
    out["ok"] = bool(out["pages"])
    return out
