"""PDF snippet cropper for the Adicot intake pipeline — SECTION-TITLE version.

Why this exists / why it works this way
---------------------------------------
A drawing sheet has labeled, boxed sections: "HEATING AND COOLING LOAD SUMMARY",
"VENTILATION SCHEDULE", "DIFFUSER, GRILLE SCHEDULE", a title block, general
notes, etc. Each review-page value lives inside one of those titled sections.

The extraction model is GOOD at reading which section a value sits in (reading
text) and BAD at guessing pixel coordinates (we proved this: boxes drifted run
to run and landed on the wrong side of wide E-size sheets). So we split the work:

  * The model returns, per field, the SECTION TITLE the value lives under:
        "_sources": { "sf": { "page": 1, "section": "HEATING AND COOLING LOAD SUMMARY" }, ... }
  * THIS module searches the page for that title text with PyMuPDF
    (page.search_for -> real coordinates) and crops a generous region starting
    at the title and extending down/right to capture the section body.

Neither side guesses pixels. The model names a label; PyMuPDF finds where that
label actually is. Result: a snippet that reliably shows "your value is in this
block — start here," which is the stated goal.

Pure logic — no Flask, no Drive, no network. app.py's /crop route calls
crop_sources() / overlay_pages().
"""

from __future__ import annotations

import base64
import difflib
import re
from typing import Optional

import fitz  # PyMuPDF


# ── Tunables ─────────────────────────────────────────────────────────────────

DPI = 150
JPEG_QUALITY = 60
MAX_WIDTH_PX = 700              # section crops are wider than field crops

# How big a region to crop around a found title, as fractions of PAGE size.
# The title sits at the top of its section; the body extends below and to the
# right of the title text. These are generous on purpose.
REGION_DOWN = 0.22             # extend this far DOWN the page from the title top
REGION_RIGHT = 0.30            # extend this far RIGHT from the title left edge
REGION_UP = 0.015              # small lead-in above the title so it's included
REGION_LEFT = 0.01             # small lead-in left of the title

FUZZY_CUTOFF = 0.72            # min ratio for fuzzy title matching when exact fails


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def _norm(s: str) -> str:
    """Normalize a title for matching: upper, collapse whitespace, strip junk."""
    s = (s or "").upper()
    s = s.replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _page_text_lines(page):
    """Return [(text, fitz.Rect), ...] for text lines on the page."""
    out = []
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            txt = "".join(sp.get("text", "") for sp in spans).strip()
            if not txt:
                continue
            x0 = min(sp["bbox"][0] for sp in spans)
            y0 = min(sp["bbox"][1] for sp in spans)
            x1 = max(sp["bbox"][2] for sp in spans)
            y1 = max(sp["bbox"][3] for sp in spans)
            out.append((txt, fitz.Rect(x0, y0, x1, y1)))
    return out


def _find_title_rect(page, title: str):
    """Locate a section title on the page. Try exact search_for first, then a
    fuzzy line-by-line match (drawings often have odd spacing/case). Returns a
    fitz.Rect or None."""
    if not title:
        return None

    # 1) direct search (fast, exact)
    rects = page.search_for(title)
    if rects:
        return rects[0]

    # 2) try the "&" form
    rects = page.search_for(title.replace(" AND ", " & "))
    if rects:
        return rects[0]

    # 3) fuzzy: compare normalized title against every text line
    want = _norm(title)
    best = None
    best_ratio = 0.0
    for txt, rect in _page_text_lines(page):
        cand = _norm(txt)
        if not cand:
            continue
        if want and (want in cand or cand in want):
            return rect
        ratio = difflib.SequenceMatcher(None, want, cand).ratio()
        if ratio > best_ratio:
            best_ratio, best = ratio, rect
    if best is not None and best_ratio >= FUZZY_CUTOFF:
        return best
    return None


def _region_from_title(title_rect: fitz.Rect, page_rect: fitz.Rect) -> fitz.Rect:
    """Given the title's rectangle, build the section region to crop (page
    coordinates), extending down and right to capture the section body."""
    pw, ph = page_rect.width, page_rect.height
    x0 = title_rect.x0 - REGION_LEFT * pw
    y0 = title_rect.y0 - REGION_UP * ph
    x1 = title_rect.x0 + REGION_RIGHT * pw
    y1 = title_rect.y0 + REGION_DOWN * ph
    x1 = max(x1, title_rect.x1 + REGION_LEFT * pw)
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


# ── Public entry points ──────────────────────────────────────────────────────

def crop_sources(pdf_bytes: bytes, sources: dict,
                 only_fields: Optional[list] = None) -> dict:
    """Crop one section JPEG per field.

    sources : { field: { "page": n, "section": "TITLE TEXT" } }
    only_fields : optional whitelist of fields to crop.

    Fields pointing at the same (page, section) share one crop. Returns:
      { ok, crops:{field:{b64,page,section}}, shared:{field:field},
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

    by_section = {}
    page_cache = {}

    for field, src in sources.items():
        if wl is not None and field not in wl:
            continue
        if not isinstance(src, dict):
            out["errors"].append({"field": field, "message": "source not an object"})
            continue
        title = src.get("section") or src.get("title") or ""
        if not title:
            out["errors"].append({"field": field, "message": "no section title"})
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

        key = (page_idx, _norm(title))
        if key in by_section:
            out["shared"][field] = by_section[key]
            continue

        page = page_cache.get(page_idx)
        if page is None:
            page = doc.load_page(page_idx)
            page_cache[page_idx] = page

        title_rect = _find_title_rect(page, title)
        if title_rect is None:
            out["errors"].append({"field": field,
                                  "message": f"section not found: {title!r}"})
            continue

        region = _region_from_title(title_rect, page.rect)
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
            "section": title,
        }
        by_section[key] = field

    doc.close()
    out["ok"] = bool(out["crops"])
    return out


def overlay_pages(pdf_bytes: bytes, sources: dict,
                  only_fields: Optional[list] = None) -> dict:
    """Debug: draw each found section region as a red rectangle on the full page
    (labeled with field names), so targeting can be eyeballed on the real
    drawing. Sections that can't be located are listed in errors."""
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
        title = src.get("section") or src.get("title") or ""
        if not title:
            continue
        try:
            page_1 = int(src.get("page", 1))
        except (TypeError, ValueError):
            page_1 = 1
        idx = page_1 - 1
        if idx < 0 or idx >= doc.page_count:
            continue
        page = doc.load_page(idx)
        key = (idx, _norm(title))
        if key in seen:
            lst = regions_by_page[idx]
            i = seen[key]
            lst[i] = (lst[i][0] + ", " + field, lst[i][1])
            continue
        title_rect = _find_title_rect(page, title)
        if title_rect is None:
            out["errors"].append({"field": field, "message": f"section not found: {title!r}"})
            continue
        region = _region_from_title(title_rect, page.rect)
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
