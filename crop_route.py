# =============================================================================
# /crop route — paste this block into app.py
# =============================================================================
# Adicot intake snippet cropper. Apps Script POSTs a client drawing PDF plus the
# _sources boxes; this returns one small JPEG per box (base64). Apps Script then
# uploads the crops to the project's Drive folder. This route does NO Drive work.
#
# Auth: token (CROP_TOKEN env var), NOT the basic-auth used elsewhere — Apps
# Script can't do basic auth cleanly. The route is exempt from @_require_auth.
#
# Size: the global MAX_CONTENT_LENGTH (5 MB) is too small for drawing PDFs, so
# this route reads the raw body itself and is not bound by request.form parsing.
# Send ONE PDF per request (keeps the base64 well under Apps Script's 50 MB cap).
#
# Add near the top of app.py with the other imports:
#     import pdf_crop
# Add to requirements.txt:
#     pymupdf>=1.24
# Set on Render (Environment):
#     CROP_TOKEN = <a long random string>   (same value goes in Apps Script
#                                             Script Properties as CROP_TOKEN)
# =============================================================================

import pdf_crop

CROP_TOKEN = os.environ.get("CROP_TOKEN")
CROP_MAX_BYTES = 40 * 1024 * 1024   # 40 MB ceiling for the JSON body on this route


def _crop_authorized(req) -> bool:
    """True if the request carries the right token. Checks header then query."""
    if not CROP_TOKEN:
        return False   # not configured -> refuse, don't run open
    supplied = (req.headers.get("X-Crop-Token")
                or req.args.get("token", "")).strip()
    return bool(supplied) and secrets.compare_digest(supplied, CROP_TOKEN)


@app.route("/crop", methods=["POST"])
def crop_route():
    """Body (JSON):
        {
          "pdf_b64":  "<base64 of one drawing PDF>",
          "sources":  { field: { "page": n, "box": [x,y,w,h] }, ... },
          "fields":   ["roofRValue", ...]   // optional whitelist (final-record fields)
          "overlay":  false                 // optional; true = debug page overlay
        }
    Returns crop_sources() output, or overlay_pages() output when overlay=true.
    """
    if not _crop_authorized(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    raw = request.get_data(cache=False, as_text=False)
    if not raw:
        return jsonify({"ok": False, "error": "empty body"}), 400
    if len(raw) > CROP_MAX_BYTES:
        return jsonify({"ok": False, "error": "payload too large"}), 413

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as e:
        return jsonify({"ok": False, "error": f"bad json: {e}"}), 400

    pdf_b64 = payload.get("pdf_b64") or ""
    sources = payload.get("sources") or {}
    only_fields = payload.get("fields") or None
    overlay = bool(payload.get("overlay"))

    if not pdf_b64:
        return jsonify({"ok": False, "error": "no pdf_b64"}), 400
    try:
        import base64 as _b64
        pdf_bytes = _b64.b64decode(pdf_b64)
    except Exception as e:
        return jsonify({"ok": False, "error": f"bad pdf_b64: {e}"}), 400

    try:
        if overlay:
            result = pdf_crop.overlay_pages(pdf_bytes, sources, only_fields=only_fields)
        else:
            result = pdf_crop.crop_sources(pdf_bytes, sources, only_fields=only_fields)
    except Exception as e:
        return jsonify({"ok": False, "error": f"crop failed: {e}"}), 500

    return jsonify(result)
