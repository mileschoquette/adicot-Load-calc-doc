"""PDF snippet cropper for the Adicot intake pipeline — BBOX version.

Why this exists / why it works this way
---------------------------------------
A drawing sheet has the review-page values scattered across it: SF and occupant
counts in the title block, lighting W/SF in the LIGHTING NOTES, equipment in the
EQUIPMENT SCHEDULE, etc. We want one cropped JPEG per value showing where it
lives on the sheet.

The earlier SECTION-TITLE approach asked the model to name the titled section a
value sits under, then searched the page for that title text with PyMuPDF. That
only works when the model's label happens to be literal searchable text on the
page. It broke the moment a sheet's title block was graphic (model returned
"TITLE BLOCK", which is a description, not printed text) — 0 crops, every field
errored. Too fragile across drafters.

This version crops by COORDINATES. The model already sees the rasterized page,
so it points at where the value sits with a normalized bounding box:

  "_sources": { "sf": { "page": 1, "bbox": [0.71, 0.93, 0.10, 0.04] }, ... }

bbox = [x, y, w, h], all fractions of page size, origin TOP-LEFT. No text
search, no guessing a label that may not exist. THIS module pads the box a touch
(values sit among dimension lines / leaders) and crops it at DPI.

Neither side searches text. The model points; PyMuPDF crops the pixels.

Pure logic — no Flask, no Drive, no network. app.py's /crop route calls
crop_sources() / overlay_pages().
"""

from __future__ import annotations

import base64
from typing import Optional

import fitz  # PyMuPDF


# ── Tunables ─────────────────────────────────────────────────────────────────

DPI = 300
JPEG_QUALITY = 85
MAX_WIDTH_PX = 1400

# Pad each bbox by this fraction of PAGE size on every side, so the crop has a
# little breathing room around the value instead of clipping it tight.
PAD_X = 0.03
PAD_Y = 0.018

# Round normalized bbox coords to this many decimals when building the dedup key.
# Two boxes that land within ~1% of each other share one crop.
DEDUP_DECIMALS = 2


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def _parse_bbox(src: dict):
    """Pull [x, y, w, h] out of a source object. Accepts a 'bbox' list or
    explicit x/y/w/h keys. Returns a tuple of 4 floats or None if malformed."""
    raw = src.get("bbox")
    if raw is None and all(k in src for k in ("x", "y", "w", "h")):
        raw = [src["x"], src["y"], src["w"], src["h"]]
    if raw is None:
        return None
    try:
        x, y, w, h = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
    except (TypeError, ValueError, IndexError):
        return None
    if w <= 0 or h <= 0:
        return None
    return (x, y, w, h)


def _region_from_bbox(bbox, page_rect: fitz.Rect) -> fitz.Rect:
    """Convert a normalized [x, y, w, h] (top-left origin) into a padded,
    clamped fitz.Rect in page coordinates."""
    pw, ph = page_rect.width, page_rect.height
    x, y, w, h = bbox
    x0 = (x - PAD_X) * pw
    y0 = (y - PAD_Y) * ph
    x1 = (x + w + PAD_X) * pw
    y1 = (y + h + PAD_Y) * ph
    return fitz.Rect(_clamp(x0, 0, pw), _clamp(y0, 0, ph),
                     _clamp(x1, 0, pw), _clamp(y1, 0, ph))


def _crop_region_jpeg(page, region: fitz.Rect) -> bytes:
    """Render only `region` of the page at DPI and return JPEG bytes,
    downscaled so width <= MAX_WIDTH_PX."""
    zoom = DPI / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=region, alpha=False)
    if pix.width > MAX_WIDTH_PX:
        shrink = max(1, round(pix.width / MAX_WIDTH_PX))
        if shrink > 1:
            pix.shrink(shrink)
    return pix.tobytes(output="jpeg", jpg_quality=JPEG_QUALITY)


def _dedup_key(page_idx, bbox):
    return (page_idx,
            round(bbox[0], DEDUP_DECIMALS), round(bbox[1], DEDUP_DECIMALS),
            round(bbox[2], DEDUP_DECIMALS), round(bbox[3], DEDUP_DECIMALS))


# ── Public entry points ──────────────────────────────────────────────────────

def crop_sources(pdf_bytes: bytes, sources: dict,
                 only_fields: Optional[list] = None) -> dict:
    """Crop one JPEG per field, located by normalized bbox.

    sources : { field: { "page": n, "bbox": [x, y, w, h] } }   (coords 0..1)
    only_fields : optional whitelist of fields to crop.

    Fields pointing at the same (page, rounded bbox) share one crop. Returns:
      { ok, crops:{field:{b64,page,bbox}}, shared:{field:field},
        errors:[{field,message}], page_count }
    """
    out = {"ok": False, "crops": {}, "shared": {}, "errors": [], "page_count": 0}
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

    by_box = {}
    page_cache = {}

    for field, src in sources.items():
        if wl is not None and field not in wl:
            continue
        if not isinstance(src, dict):
            out["errors"].append({"field": field, "message": "source not an object"})
            continue

        bbox = _parse_bbox(src)
        if bbox is None:
            out["errors"].append({"field": field,
                                  "message": "missing or malformed bbox"})
            continue

        try:
            page_1 = int(src.get("page", 1))
        except (TypeError, ValueError):
            page_1 = 1
        page_idx = page_1 - 1
        if page_idx < 0 or page_idx >= doc.page_count:
            out["errors"].append({"field": field,
                                  "message": f"page {page_1} out of range"})
            continue

        key = _dedup_key(page_idx, bbox)
        if key in by_box:
            out["shared"][field] = by_box[key]
            continue

        page = page_cache.get(page_idx)
        if page is None:
            page = doc.load_page(page_idx)
            page_cache[page_idx] = page

        region = _region_from_bbox(bbox, page.rect)
        if region.is_empty or region.width < 1 or region.height < 1:
            out["errors"].append({"field": field,
                                  "message": f"empty region from bbox {bbox}"})
            continue

        try:
            jpeg = _crop_region_jpeg(page, region)
        except Exception as e:
            out["errors"].append({"field": field, "message": f"crop failed: {e}"})
            continue
        if not jpeg:
            out["errors"].append({"field": field, "message": "empty crop"})
            continue

        out["crops"][field] = {
            "b64": base64.b64encode(jpeg).decode("ascii"),
            "page": page_1,
            "bbox": list(bbox),
        }
        by_box[key] = field

    doc.close()
    out["ok"] = bool(out["crops"])
    return out


def overlay_pages(pdf_bytes: bytes, sources: dict,
                  only_fields: Optional[list] = None) -> dict:
    """Debug: draw each bbox as a red rectangle on the full page (labeled with
    field names), so targeting can be eyeballed on the real drawing. Boxes that
    are malformed or off-page are listed in errors."""
    out = {"ok": False, "pages": {}, "errors": []}
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        out["errors"].append({"field": None, "message": f"open failed: {e}"})
        return out

    wl = set(only_fields) if only_fields else None
    regions_by_page = {}
    seen = {}

    for field, src in (sources or {}).items():
        if wl is not None and field not in wl:
            continue
        if not isinstance(src, dict):
            continue
        bbox = _parse_bbox(src)
        if bbox is None:
            out["errors"].append({"field": field, "message": "missing or malformed bbox"})
            continue
        try:
            page_1 = int(src.get("page", 1))
        except (TypeError, ValueError):
            page_1 = 1
        idx = page_1 - 1
        if idx < 0 or idx >= doc.page_count:
            out["errors"].append({"field": field, "message": f"page {page_1} out of range"})
            continue
        page = doc.load_page(idx)
        key = _dedup_key(idx, bbox)
        if key in seen:
            lst = regions_by_page[idx]
            i = seen[key]
            lst[i] = (lst[i][0] + ", " + field, lst[i][1])
            continue
        region = _region_from_bbox(bbox, page.rect)
        regions_by_page.setdefault(idx, [])
        seen[key] = len(regions_by_page[idx])
        regions_by_page[idx].append((field, region))

    for idx, items in regions_by_page.items():
        page = doc.load_page(idx)
        for label, region in items:
            page.draw_rect(region, color=(1, 0, 0), width=1.5)
            page.insert_text(fitz.Point(region.x0 + 2, max(8, region.y0 - 3)),
                             label, fontsize=7, color=(1, 0, 0))
        zoom = DPI / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        out["pages"][idx + 1] = base64.b64encode(
            pix.tobytes(output="jpeg", jpg_quality=70)).decode("ascii")

    doc.close()
    out["ok"] = bool(out["pages"])
    return out
