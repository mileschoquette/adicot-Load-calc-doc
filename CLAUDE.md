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

The three signed schedules are generated as Excel first (`pdf/schedule_xlsx.py`), then converted to spreadsheet-origin PDFs via headless LibreOffice (`pdf/xlsx_to_pdf.py`) — this is what makes them import cleanly into AutoCAD. If LibreOffice isn't present (e.g. local dev on macOS), conversion returns `None` and `build_all_pdfs` falls back to the legacy ReportLab renderers so a PDF still gets produced. The Combined PDF is never an Excel file — it stays a PyMuPDF merge of the converted PDFs + charts + HTML appendix.

## Environment variables

| Var | Purpose |
|---|---|
| `APP_PASSWORD` | Basic-auth password. Unset = no auth (local dev). |
| `SECRET_KEY` | Flask session key; auto-generated if unset. |
| `JOBS_DIR` | Per-job workspace root. Default `./jobs`; Render uses `/var/data/jobs` (persistent disk). |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full Drive service-account JSON as a string. Also authenticates the Sheets CMS (`integrations/sheets_client.py`). Missing → Drive features and the project CMS degrade silently. |
| `GOOGLE_SHEETS_SPREADSHEET_ID` | Spreadsheet ID backing the project CMS (`_cms` in `core.py`). Missing → CMS features degrade silently (empty project list). |
| `WIX_API_KEY`, `WIX_SITE_ID` | Wix creds used only by the spec engine now (`spec/spec_data.py`, spec Parts/Sections/Clauses). Missing → spec content falls back to the bundled seed. The project CMS no longer uses Wix. |
| `CROP_TOKEN` | Shared token for the `/crop` route (Apps Script intake). Distinct from basic auth. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | SMTP creds for the daily jobs digest email (`integrations/email_client.py`). `SMTP_PORT` defaults to `587`; `SMTP_FROM` defaults to `SMTP_USER`. Missing → digest silently fails to send (logged as an error). |
| `PUBLIC_BASE_URL` | Base URL used to build job links in the daily digest email. Defaults to `https://adicot-load-calc-doc.onrender.com`. |
| `PORT` | Set by Render. |

## Architecture

`app.py` is a slim entry point: it builds the Flask app, sets config, registers every blueprint, and starts the daily-digest scheduler. It holds no route handlers and no shared state itself.

- `core.py` — connective tissue nearly every blueprint imports from: the `@_require_auth` basic-auth decorator, job-path/meta helpers (`_job_dir`, `_load_meta`, `_save_meta`, `_load_report`, `_parse_and_persist`, ...), the `@_require_parsed` decorator, the CMS backend indirection (`_cms`, always `sheets_client` — the project CMS has fully migrated off Wix), the JSON-registry load/save pairs (stage/notes/assigned/manual-tasks/due-date/calendar-events/invoice), the `_build_cms_entries()` builder used by both the dashboard and the daily digest, and the optional-feature import flags (`HAS_EQUIP_SELECTOR`, `HAS_ERV`, `HAS_DEHUMID`, `HAS_DM_SETUP_GENERATOR`, `HAS_EQUIP_SCHEDULE`, each with a matching `_*_IMPORT_ERROR`). `core.py` never imports from `blueprints/*`; blueprints import from `core`, not the other way around.
- `blueprints/` — one Flask `Blueprint` module per route group:
  - `dashboard.py` — landing list (`/`), Calendar tab + due-date/event CRUD, job-list stage/notes/assigned/manual-tasks CRUD, temp-jobs index (`/jobs`, `/past-jobs` redirect), and both delete flows (local workspace, CMS record).
  - `job_lifecycle.py` — per-job home tab (★ Work Order / parse), the client portal, the PDF-generation/regeneration routes (including the combined-PDF + chart-appendix helpers that `quality.py`'s Charts tab reuses), re-scrape-from-Drive, the HTML-parse pipeline entry point, and file downloads.
  - `quality.py` — Duct Sizing, Quality Check, and Charts tabs.
  - `spec_bp.py` — Specifications tab (named `spec_bp`, not `spec.py`, to avoid shadowing the `spec/` package).
  - `dm_setup.py` — Generate DM Setup tab (.vbs + load-calc JSON generator).
  - `equipment.py` — Equipment Selection tab (A/C, heat pump, ERV, dehumidifier).
  - `quickbooks.py` — QuickBooks OAuth admin and invoice creation/attachment.
  - `misc.py` — public legal pages, all `/debug/*` diagnostics, the Drive-availability API, and `/crop`.

Each request loads/saves a per-job JSON state file, then re-runs the relevant module against it. There is no database.

Every route's endpoint name is namespaced by its blueprint (Flask always does this, e.g. `job_lifecycle.job_star`, not `job_star`); every `url_for(...)` call, in Python and in the Jinja templates, uses the blueprint-qualified form (or a same-blueprint `.viewname` shorthand).

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

`job_id` is `secure_filename`-validated and resolved with a parent-containment check (`_job_dir` in `core.py`); keep that pattern for any new job-scoped routes to prevent path traversal.

### Module map (logic lives here, NOT in `app.py` or `blueprints/*`)

- `hvac/hvac_pipeline.py` — the big one. Parses the DM HTML (BeautifulSoup), computes loads. Public entry: `build_all_pdfs(html_path, config, engineer, firm, out_dir, zone_overrides)` — for each of the three schedules it builds an `.xlsx` (`pdf/schedule_xlsx.py`), converts it to PDF (`pdf/xlsx_to_pdf.py`), and falls back to the in-module ReportLab renderers (`build_ventilation_schedule_pdf` etc.) if conversion is unavailable. Also exposes `STATE_TABLE` (per-state codes used by the spec engine).
- `pdf/schedule_xlsx.py` — openpyxl renderers for the three signed schedules (Ventilation, Air Balance, Load Summary) with print setup (US Letter, portrait, fit-to-width, repeating header rows). Duck-types the `ComputedReport` dataclasses; lazy-imports a couple of `hvac_pipeline` helpers to avoid a circular import.
- `pdf/xlsx_to_pdf.py` — `convert(xlsx, pdf)` via headless LibreOffice (`soffice --convert-to pdf`), per-call throwaway user profile so concurrent runs don't collide. Returns `None` (never raises) when LibreOffice is missing or conversion fails — the pipeline then uses the ReportLab fallback.
- `spec/spec_engine.py` — pure spec renderer. Pipeline: `eval_condition` → `resolve_fields` → `resolve_placeholders` → `build_spec` (filters empty sections, renumbers PART-scoped). Numbering is never stored.
- `spec/spec_data.py` — loads Spec Parts/Sections/Clauses from Wix (collections `Import5/6/7`), falls back to bundled `spec_seed.json` when Wix is unreachable.
- `spec/spec_docx.py` — renders a `RenderedSpec` to .docx (python-docx, Calibri, B&W).
- `hvac/hvac_selector.py` — Carrier split-system A/C and heat pump selector from `equipment_db.xlsx` (pandas + openpyxl). Import is wrapped in try/except in `core.py` — feature degrades cleanly if pandas/xlsx are unavailable.
- `pdf/charts.py` — matplotlib (`Agg` backend, headless-safe). `render_all_charts(report, out_dir)` writes a fixed set of PNGs.
- `hvac/duct_sizing.py` — writes the Duct Sizing xlsx sheet with the same CHECK/deficiency formulas as the legacy workbook.
- `pdf/pdf_crop.py` — coordinate-based PDF cropper (PyMuPDF). Crops by normalized bbox, NOT by text search — the coordinate approach exists because the earlier section-title search broke on graphic title blocks.
- `hvac/validators.py` — strict HTML-vs-Wix comparison. Numbers-only, unit-agnostic, R↔U auto-conversion, empty Wix values skipped silently.
- `integrations/sheets_client.py` — Google Sheets CMS client and `core._cms`'s sole backend (`list_projects`, `get_project`, `update_project`, `append_project`, `delete_project`). Reads/writes the `GOOGLE_SHEETS_SPREADSHEET_ID` spreadsheet's "Projects" tab via `GOOGLE_SERVICE_ACCOUNT_JSON`, short TTL cache so new rows show up without a restart. Returns `None`/`[]`/`False` on any error, same degrade-gracefully contract `wix_client.py` used to have.
- `integrations/wix_client.py` — mostly-read Wix Data v2 wrapper (`list_projects`, `get_project`, `delete_project`). No longer used for the project CMS (`core._cms` is now always `sheets_client`); kept only because `spec/spec_data.py` still imports its `_credentials`/`_headers` helpers for the spec engine's own Wix collections (`Import5/6/7`).
- `integrations/gdrive_client.py` — Drive read+write. Path convention: `1-Jobs/{Company}/{Job No}/4-Design/dm_hvac-loads1.html` (read) and `…/6-Submit/*.pdf` (write). `{Company}` is the first hyphen token of Job No. **1-Jobs must live on a Shared Drive** — service accounts have no personal quota. All calls use `corpora="allDrives"` + `includeItemsFromAllDrives=True` + `supportsAllDrives=True`. 15-min folder-id cache.
- `integrations/email_client.py` — SMTP wrapper (`send_email`), stdlib `smtplib` only, no new dependency. Returns `False`/logs on any error (missing creds, auth failure, network) rather than raising.
- `integrations/daily_digest.py` — builds and sends the daily jobs-list digest, reusing `core._build_cms_entries()`/`core._entry_bucket()`/`core._BUCKET_RANK` (a plain top-level `import core`, no circular-import risk, since `core.py` has no dependency on this module). Runs an in-process background thread (`start_scheduler()`, called unconditionally at the bottom of `app.py`) that checks the clock every 5 minutes and sends once per day at 7 AM `America/New_York`, deduped via `jobs/digest_state.json`. Not a Render Cron Job — a separate Cron Job resource can't mount the same persistent disk this app's registries live on.
- `archive/docs-snippets/app_spec_routes.py` — dead paste-in snippet of spec routes, confirmed unused and already drifted from the real code (references a nonexistent `spec_dxf` module); the live routes are in `blueprints/spec_bp.py` (`/job/<id>/spec*`). Kept only as historical reference.
- `archive/docs-snippets/crop_route.py` — dead paste-in reference, confirmed unused and already drifted (documents a `"box"` JSON field the real route never accepts, only `"bbox"`); the live `/crop` route is in `blueprints/misc.py`. Kept only as historical reference.
- `archive/wix-snapshot/` — snapshots of the intake pipeline's Google Apps Script (`AdicotProjects.gs`, deployed separately, not run from this repo) plus two Wix-hosted Velo files (`admin-review-page.velo.js`, `projects.web.js`), all confirmed decommissioned. `AdicotProjects.gs`'s `_buildAdminReviewLink()` points admins at `/job/<id>/star` on Render, and review, client answers, and client sign-off now all happen in Flask (`job_star_save()`, then `/portal/<token>` in `job_lifecycle.py`), writing straight to the Sheet via `_cms.update_project()`. All the dead code that used to serve the retired Wix page or POST back into this script (`createClientDraft`, `createQuestionsEmailDraft`, `_handleSaveAndApprove`, `handleClientSigned`, `handleClientAnswers`, and their `doPost` actions) has been removed from the snapshot — `doPost` now has no live actions. The two `.velo.js`/`.web.js` files themselves should be unpublished/deleted in the Wix Editor (a manual step outside this repo) but are kept here as historical reference.

### Auth model

Two systems, intentionally separate:
1. `@_require_auth` decorator (`core.py`) — HTTP basic auth (`adicot` / `$APP_PASSWORD`) on every interactive route. No-op when `APP_PASSWORD` unset.
2. `/crop` route (`blueprints/misc.py`) — token auth via `X-Crop-Token` header or `?token=` query, checked by `_crop_authorized`. It is exempt from `@_require_auth` because Apps Script can't do basic auth cleanly. The route also bypasses Flask's 5 MB `MAX_CONTENT_LENGTH` by reading raw body (40 MB ceiling).

### Patterns to preserve

- **External integrations degrade silently.** `sheets_client`, `wix_client`, `gdrive_client`, and `hvac_selector` all return empty/None on missing creds or import failures rather than raising. Don't add hard requires.
- **Pipeline failures are caught and logged to `pdf_error.log` in the job dir**, with the traceback also printed to stdout for Render logs. PDF generation runs under `redirect_stdout(io.StringIO())` to swallow the pipeline's noisy prints.
- **Job IDs are validated** via `secure_filename` AND a parent-containment check on the resolved path. Re-use `_job_dir(job_id)` (from `core.py`) for any new job-scoped route — don't hand-roll the path join.
- **Numbering in the spec engine is computed at render time, never stored.** Sections are PART-scoped two-digit (`1.01`), clauses lettered `A..Z, AA..`.
- **`/crop` reads raw body directly** — don't add it to a generic JSON-body decorator.
- **`core.py` must not import from `blueprints/*`.** Blueprints import from `core`; `app.py` imports `core` and every blueprint module to register it. A new blueprint whose routes need to be reached via `url_for()` from another blueprint's Python code needs the full `blueprintname.viewname` form; same-blueprint redirects can use the relative `.viewname` shorthand.
