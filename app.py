"""HVAC Loads Pipeline — Flask entry point.

This module only builds the Flask app and registers blueprints; the actual
route handlers live in blueprints/*.py, and the shared state/helpers they
depend on (auth, job-path resolution, the CMS-entries builder, JSON
registries, optional-feature import flags) live in core.py. See CLAUDE.md
for the full module map.

Flask always namespaces a blueprint's endpoints as "blueprintname.viewname"
(the `endpoint=` kwarg on @blueprint.route only renames the part after the
dot — it can't drop the blueprint prefix). Every `url_for(...)` call that
used to reference a bare view-function name — in Python and in the Jinja
templates — was updated to the "blueprintname.viewname" form (or, for a
same-blueprint redirect, the relative ".viewname" shorthand) when the routes
moved into blueprints/*.py.

Job identity: CMS jobs use their Wix item id as the job_id (so re-opening a
CMS project reuses its workspace and saved settings); temp jobs use a
"temp_" prefix. The six work tabs are gated on report.json existing (see
core._require_parsed).

Environment variables:
    APP_PASSWORD                    shared password (username always "adicot"); unset = no auth
    SECRET_KEY                      Flask session key; auto-generated if unset
    JOBS_DIR                        where per-job workspaces live; default ./jobs
    GOOGLE_SERVICE_ACCOUNT_JSON     Drive service account creds (JSON blob)
    WIX_API_KEY, WIX_SITE_ID        Wix CMS credentials
    PORT                            set by Render/host
"""

from __future__ import annotations

import os
import secrets

from flask import Flask

import core
from blueprints.dashboard import dashboard
from blueprints.job_lifecycle import job_lifecycle
from blueprints.quality import quality
from blueprints.spec_bp import spec_bp
from blueprints.dm_setup import dm_setup
from blueprints.equipment import equipment
from blueprints.quickbooks import quickbooks
from blueprints.misc import misc

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(16))

for bp in (dashboard, job_lifecycle, quality, spec_bp, dm_setup, equipment, quickbooks, misc):
    app.register_blueprint(bp)


# Started unconditionally at import time (not gated behind __main__) so it
# also runs under gunicorn in production, not just `python app.py` locally.
from integrations import daily_digest
daily_digest.start_scheduler()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    print(f"Auth: {'enabled' if core.APP_PASSWORD else 'DISABLED (no APP_PASSWORD)'}\"")
    app.run(host="0.0.0.0", port=port, debug=debug)
