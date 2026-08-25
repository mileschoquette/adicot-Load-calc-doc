"""Render an .xlsx to .pdf with headless LibreOffice.

Excel's own "print to PDF" produces spreadsheet-origin vector PDFs that import
cleanly into AutoCAD; ReportLab-drawn PDFs do not. On the server we can't run
Excel, so we use LibreOffice headless (`soffice --convert-to pdf`), which
renders the same page setup (print area, fit-to-width, margins) baked into the
workbook by schedule_xlsx.py.

Degrades gracefully: convert() returns None if LibreOffice isn't installed
(e.g. local dev on macOS without it) or a conversion fails, so callers can fall
back to another renderer. Never raises.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional

# Common install locations, checked after $SOFFICE_BIN and $PATH.
_FALLBACK_BINS = (
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)


def _find_soffice() -> Optional[str]:
    env = os.environ.get("SOFFICE_BIN")
    if env and Path(env).exists():
        return env
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    for path in _FALLBACK_BINS:
        if Path(path).exists():
            return path
    return None


def available() -> bool:
    """True if a LibreOffice binary can be located."""
    return _find_soffice() is not None


def convert(xlsx_path, out_pdf_path=None, timeout: int = 120) -> Optional[Path]:
    """Convert xlsx_path to PDF. Returns the PDF path, or None on any failure.

    out_pdf_path defaults to the xlsx path with a .pdf suffix. A throwaway,
    per-call LibreOffice user profile is used so concurrent conversions can't
    collide on the profile lock.
    """
    xlsx_path = Path(xlsx_path)
    soffice = _find_soffice()
    if not soffice or not xlsx_path.exists():
        return None

    out_pdf_path = Path(out_pdf_path) if out_pdf_path else xlsx_path.with_suffix(".pdf")
    outdir = out_pdf_path.parent
    outdir.mkdir(parents=True, exist_ok=True)

    profile = Path(tempfile.gettempdir()) / f"lo_profile_{uuid.uuid4().hex}"
    cmd = [
        soffice, "--headless", "--norestore", "--nolockcheck", "--nodefault",
        f"-env:UserInstallation=file://{profile}",
        "--convert-to", "pdf:calc_pdf_Export",
        "--outdir", str(outdir), str(xlsx_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        produced = outdir / (xlsx_path.stem + ".pdf")
        if proc.returncode != 0 or not produced.exists():
            print(f"[xlsx_to_pdf] soffice failed for {xlsx_path.name} "
                  f"(rc={proc.returncode}): {proc.stderr[:500]}", flush=True)
            return None
        if produced != out_pdf_path:
            os.replace(produced, out_pdf_path)
        return out_pdf_path
    except subprocess.TimeoutExpired:
        print(f"[xlsx_to_pdf] soffice timed out after {timeout}s for {xlsx_path.name}",
              flush=True)
        return None
    except Exception as e:  # noqa: BLE001 - never let conversion sink the pipeline
        print(f"[xlsx_to_pdf] conversion error for {xlsx_path.name}: {e}", flush=True)
        return None
    finally:
        shutil.rmtree(profile, ignore_errors=True)
