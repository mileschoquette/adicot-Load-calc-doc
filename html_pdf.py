"""Render the scraped Design Master HTML export to PDF pages.

Used to append the raw load-calc HTML as an appendix to the Load deliverable and
the Combined PDF. Renders with PyMuPDF's Story HTML engine, which needs no extra
system dependencies (important for Render — wkhtmltopdf/WeasyPrint don't deploy
cleanly there).

The DM export references an external stylesheet (dm_hvac-loads.css) that the
scrape does NOT save, so a raw render shows a "stylesheet missing" banner and
unstyled tables. We strip that <link> and the banner <div>, then apply our own
compact table styling instead.

Pure logic — no Flask, no Drive, no network. Never raises; returns None on any
failure, since appending the appendix is best-effort and must not break PDF
generation.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF

# Design Master's own stylesheet (dm_hvac-loads.css). It's the standard DM export
# stylesheet — identical across projects — so we bundle it here rather than rely
# on it being present in each project's Drive folder. The trailing .MissingStyle
# rule is what normally hides the "stylesheet missing" banner; we also strip the
# banner outright below in case the layout engine ignores `visibility`.
_APPENDIX_CSS = """
body { font-family: serif; font-size: 12px; }
table {
  border: 4px solid #000000;
  margin: 0 0 12px 0;
  border-collapse: collapse;
  font-size: 12px;
  font-family: serif;
}
thead { display: table-header-group; }
th { font-weight: bold; }
.regularSize { font-size: 12px; }
th.mainHeader {
  text-align: center;
  font-size: 24px;
  font-weight: bold;
  border-width: 4px 2px 2px 2px;
  border-style: solid;
  border-color: #000000;
}
th.subheader { text-align: center; font-size: 18px; border: 2px solid #000000; }
td.subheader { text-align: center; font-weight: bold; border: 2px solid #000000; }
tfoot { display: table-footer-group; }
br.pageBreak { page-break-after: always; }
th.project { text-align: left; font-size: 12px; border: 1px solid #CCCCCC; }
th.otherHeader { text-align: center; border: 1px solid #000000; }
td.otherData { border: 1px solid #CCCCCC; }
td.boldData { border: 1px solid #CCCCCC; font-weight: bold; }
td.firstZone { border-width: 2px 1px 1px 1px; border-style: solid; border-color: #000000; }
td.psychlabel { font-weight: bold; border: 1px solid #CCCCCC; }
.MissingStyle { visibility: hidden; font-size: 1px; }

/* Portrait fit: the deliverables are portrait Letter, so the appendix matches.
   DM's native 12px overflows portrait width on the widest tables (Room Info,
   Cooling Load Details), so scale the type down just enough to fit cleanly.
   These rules come last, so they win over the DM sizes above. */
body { font-size: 8px; }
table { font-size: 8px; }
.regularSize { font-size: 8px; }
th.project { font-size: 8px; }
th.mainHeader { font-size: 15px; }
th.subheader { font-size: 11px; }
"""

# DM's external stylesheet link and its "stylesheet missing" fallback banner.
_LINK_RE   = re.compile(r'<link[^>]*dm_hvac-loads\.css[^>]*>', re.I)
_BANNER_RE = re.compile(r'<div\s+class=["\']?MissingStyle["\']?[^>]*>.*?</div>',
                        re.I | re.S)


def _clean_html(html: str) -> str:
    html = _LINK_RE.sub("", html)
    html = _BANNER_RE.sub("", html)
    return html


def render_html_to_pdf_bytes(html_path: Path) -> Optional[bytes]:
    """Render the DM HTML at html_path to landscape-Letter PDF bytes.

    Returns the PDF bytes, or None if the file is missing/empty or rendering
    fails. Never raises.
    """
    try:
        html = Path(html_path).read_text(encoding="latin-1")
    except Exception:
        return None
    if not html.strip():
        return None

    html = _clean_html(html)
    mediabox = fitz.paper_rect("letter")             # portrait Letter (matches deliverables)
    where = mediabox + (28, 28, -28, -28)            # ~0.4" margins for table room

    try:
        story = fitz.Story(html=html, user_css=_APPENDIX_CSS)
        buf = io.BytesIO()
        writer = fitz.DocumentWriter(buf)
        more = 1
        guard = 0
        while more:
            dev = writer.begin_page(mediabox)
            more, _ = story.place(where)
            story.draw(dev)
            writer.end_page()
            guard += 1
            if guard > 1000:                         # runaway-page backstop
                break
        writer.close()
        data = buf.getvalue()
        return data or None
    except Exception:
        return None
