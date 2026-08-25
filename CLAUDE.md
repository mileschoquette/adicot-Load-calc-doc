# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this app is

A single-tenant Flask web tool (Adicot Engineering) that ingests a Design Master HVAC HTML export and produces three signed PDFs (Ventilation Schedule, Air Balance, Load Summary) plus a Word spec, equipment selection, duct sizing sheet, and charts. Deployed to Render; gated behind shared HTTP basic auth (`adicot` / `$APP_PASSWORD`).

## Run / deploy

```bash
pip install -r requirements.txt
python app.py                       # local dev, http://localhost:5000
APP_PASSWORD=foo python app.py      # local with auth enabled
```

Production runs on Render as a **Docker** service (`Dockerfile` + `render.yaml`, `runtime: docker`), `gunicorn app:app --workers 1 --timeout 120`. Docker is used so the image can install **LibreOffice**, which renders the three schedule `.xlsx` files to PDF (see below); a single worker keeps the memory peak low enough to avoid OOM on Render Starter. Python 3.12.6 (pinned in `runtime.txt` and the Dockerfile base). No test suite, no linter config.

The three signed schedules are generated as Excel first (`schedule_xlsx.py`), then converted to spreadsheet-origin PDFs via headless LibreOffice (`xlsx_to_pdf.py`) — this is what makes them import cleanly into AutoCAD. If LibreOffice isn't present (e.g. local dev on macOS), conversion returns `None` and `build_all_pdfs` falls back to the legacy ReportLab renderers so a PDF still gets produced. The Combined PDF is never an Excel file — it stays a PyMuPDF merge of the converted PDFs + charts + HTML appendix.

## Environment variables

| Var | Purpose |
|---|---|
| `APP_PASSWORD` | Basic-auth password. Unset = no auth (local dev). |
| `SECRET_KEY` | Flask session key; auto-generated if unset. |
| `JOBS_DIR` | Per-job workspace root. Default `./jobs`; Render uses `/var/data/jobs` (persistent disk). |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full Drive service-account JSON as a string. Missing → Drive features degrade silently. |
| `WIX_API_KEY`, `WIX_SITE_ID` | Wix CMS creds. Missing → project dropdown and spec content fall back to seed/empty. |
| `CROP_TOKEN` | Shared token for the `/crop` route (Apps Script intake). Distinct from basic auth. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | SMTP creds for the daily jobs digest email (`email_client.py`). `SMTP_PORT` defaults to `587`; `SMTP_FROM` defaults to `SMTP_USER`. Missing → digest silently fails to send (logged as an error). |
| `PUBLIC_BASE_URL` | Base URL used to build job links in the daily digest email. Defaults to `https://adicot-load-calc-doc.onrender.com`. |
| `PORT` | Set by Render. |

## Architecture

Flask is thin — `app.py` is a route layer that orchestrates pure-logic modules. Each request loads/saves a per-job JSON state file, then re-runs the relevant module against it. There is no database.

### Per-job storage layout

```
jobs/<job_id>/
    <original>.html        # uploaded Design Master export
    meta.json              # project config, engineer info, zone_overrides, wix_snapshot, spec inputs, equip inputs
    report.json            # parsed HVACReport (output of hvac_pipeline parsing phase)
    out/
        *-Ventilation.pdf, *-Air_Balance.pdf, *-Load.pdf
        charts/*.png
        *.docx, *.dxf, equipment outputs
```

`job_id` is `secure_filename`-validated and resolved with a parent-containment check (`_job_dir` in `app.py`) — keep that pattern for any new job-scoped routes to prevent path traversal.

### Module map (logic lives here, NOT in `app.py`)

- `hvac_pipeline.py` — the big one. Parses the DM HTML (BeautifulSoup), computes loads. Public entry: `build_all_pdfs(html_path, config, engineer, firm, out_dir, zone_overrides)` — for each of the three schedules it builds an `.xlsx` (`schedule_xlsx.py`), converts it to PDF (`xlsx_to_pdf.py`), and falls back to the in-module ReportLab renderers (`build_ventilation_schedule_pdf` etc.) if conversion is unavailable. Also exposes `STATE_TABLE` (per-state codes used by the spec engine).
- `schedule_xlsx.py` — openpyxl renderers for the three signed schedules (Ventilation, Air Balance, Load Summary) with print setup (US Letter, portrait, fit-to-width, repeating header rows). Duck-types the `ComputedReport` dataclasses; lazy-imports a couple of `hvac_pipeline` helpers to avoid a circular import.
- `xlsx_to_pdf.py` — `convert(xlsx, pdf)` via headless LibreOffice (`soffice --convert-to pdf`), per-call throwaway user profile so concurrent runs don't collide. Returns `None` (never raises) when LibreOffice is missing or conversion fails — the pipeline then uses the ReportLab fallback.
- `spec_engine.py` — pure spec renderer. Pipeline: `eval_condition` → `resolve_fields` → `resolve_placeholders` → `build_spec` (filters empty sections, renumbers PART-scoped). Numbering is never stored.
- `spec_data.py` — loads Spec Parts/Sections/Clauses from Wix (collections `Import5/6/7`), falls back to bundled `spec_seed.json` when Wix is unreachable.
- `spec_docx.py` — renders a `RenderedSpec` to .docx (python-docx, Calibri, B&W).
- `hvac_selector.py` — Carrier split-system A/C and heat pump selector from `equipment_db.xlsx` (pandas + openpyxl). Import is wrapped in try/except in `app.py` — feature degrades cleanly if pandas/xlsx are unavailable.
- `charts.py` — matplotlib (`Agg` backend, headless-safe). `render_all_charts(report, out_dir)` writes a fixed set of PNGs.
- `duct_sizing.py` — writes the Duct Sizing xlsx sheet with the same CHECK/deficiency formulas as the legacy workbook.
- `pdf_crop.py` — coordinate-based PDF cropper (PyMuPDF). Crops by normalized bbox, NOT by text search — the coordinate approach exists because the earlier section-title search broke on graphic title blocks.
- `validators.py` — strict HTML-vs-Wix comparison. Numbers-only, unit-agnostic, R↔U auto-conversion, empty Wix values skipped silently.
- `wix_client.py` — mostly-read Wix Data v2 wrapper with a 5-minute per-worker TTL cache (`list_projects`, `get_project`), plus one write call (`delete_project`, requires "Manage" scope on `WIX_API_KEY` — the read calls only need "Read"). Returns `None`/`[]`/`False` on any error (don't raise from here).
- `gdrive_client.py` — Drive read+write. Path convention: `1-Jobs/{Company}/{Job No}/4-Design/dm_hvac-loads1.html` (read) and `…/6-Submit/*.pdf` (write). `{Company}` is the first hyphen token of Job No. **1-Jobs must live on a Shared Drive** — service accounts have no personal quota. All calls use `corpora="allDrives"` + `includeItemsFromAllDrives=True` + `supportsAllDrives=True`. 15-min folder-id cache.
- `email_client.py` — SMTP wrapper (`send_email`), stdlib `smtplib` only, no new dependency. Returns `False`/logs on any error (missing creds, auth failure, network) rather than raising.
- `daily_digest.py` — builds and sends the daily jobs-list digest, reusing `app._build_cms_entries()`/`app._entry_bucket()` (via a deferred import to avoid a circular import with `app.py`). Runs an in-process background thread (`start_scheduler()`, started at the bottom of `app.py`) that checks the clock every 5 minutes and sends once per day at 7 AM `America/New_York`, deduped via `jobs/digest_state.json`. Not a Render Cron Job — a separate Cron Job resource can't mount the same persistent disk this app's registries live on.
- `archive/docs-snippets/app_spec_routes.py` — dead paste-in snippet of spec routes, confirmed unused and already drifted from the real code (references a nonexistent `spec_dxf` module); the live routes are in `app.py` (`/job/<id>/spec*`). Kept only as historical reference.
- `archive/docs-snippets/crop_route.py` — dead paste-in reference, confirmed unused and already drifted (documents a `"box"` JSON field the real route never accepts, only `"bbox"`); the live `/crop` route is in `app.py`. Kept only as historical reference.

### Auth model

Two systems, intentionally separate:
1. `@_require_auth` decorator — HTTP basic auth (`adicot` / `$APP_PASSWORD`) on every interactive route. No-op when `APP_PASSWORD` unset.
2. `/crop` route — token auth via `X-Crop-Token` header or `?token=` query, checked by `_crop_authorized`. It is exempt from `@_require_auth` because Apps Script can't do basic auth cleanly. The route also bypasses Flask's 5 MB `MAX_CONTENT_LENGTH` by reading raw body (40 MB ceiling).

### Patterns to preserve

- **External integrations degrade silently.** `wix_client`, `gdrive_client`, and `hvac_selector` all return empty/None on missing creds or import failures rather than raising. Don't add hard requires.
- **Pipeline failures are caught and logged to `pdf_error.log` in the job dir**, with the traceback also printed to stdout for Render logs. PDF generation runs under `redirect_stdout(io.StringIO())` to swallow the pipeline's noisy prints.
- **Job IDs are validated** via `secure_filename` AND a parent-containment check on the resolved path. Re-use `_job_dir(job_id)` for any new job-scoped route — don't hand-roll the path join.
- **Numbering in the spec engine is computed at render time, never stored.** Sections are PART-scoped two-digit (`1.01`), clauses lettered `A..Z, AA..`.
- **`/crop` reads raw body directly** — don't add it to a generic JSON-body decorator.
