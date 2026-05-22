# Adicot HVAC Loads PDF Pipeline — Flask App

A local web UI for converting Design Master HVAC HTML exports into the three signed PDF deliverables.

## Install

```bash
pip install flask reportlab beautifulsoup4 lxml
```

Tested with Python 3.10+.

## Run

```bash
python app.py
```

Open <http://localhost:5000> in any browser. The app binds to `0.0.0.0:5000`, so you can also reach it from an iPad or another machine on the same LAN (use `http://<your-laptop-ip>:5000`).

## How it works

1. **New Job** page: upload the Design Master HTML export, enter the project address, and (optionally) override zone display names, tonnage, supply CFM, or merge HTML zones into one deliverable row.
2. **Results** page shows:
   - Download links for the three PDFs: Ventilation Schedule, Air Balance, Load Summary
   - A console-style preview of all three tables so you can sanity-check the numbers without opening the PDFs
   - A record of every setting used to generate the job
3. **Past Jobs** page lists every job by project name and lets you re-open or delete.

## Workspace layout

Every job gets a directory under `jobs/<job_id>/`:

- `<original_filename>.html` — the uploaded HTML
- `out/<project>-Ventilation.pdf`, `out/<project>-Air_Balance.pdf`, `out/<project>-Load.pdf`
- `meta.json` — config + preview text used to render the Results page

To clear all jobs: `rm -rf jobs/`.

## Files

- `app.py` — Flask entry point (routes, request handling)
- `hvac_pipeline.py` — All pipeline logic (parsing, calc engine, PDF rendering, console preview).
   This is the same code as the notebook, extracted to a library module.
- `templates/` — Jinja2 HTML templates
- `static/style.css` — UI styling

## Adjusting the pipeline

To tweak parsing, calc logic, or PDF formatting, edit `hvac_pipeline.py` directly. The Flask app re-imports the module on each request only in debug mode; in production it'll cache the module, so restart the server after edits.

Zone overrides syntax in `hvac_pipeline.py`:

```python
zone_overrides = {
    "Zone Left Clinic North": {
        "display_name": "Zone RTU-2: SURGERY/TREAT/HYG",
        "tons": 3.5,
        "supply_cfm": 1400,
        "merge_with": "Zone Left Clinic South",  # combines into one row
    },
}
```

The Flask UI exposes all four override fields. The `merge_with` field consolidates multiple HTML zones into a single deliverable row by zone name.
