# Docker image for the Adicot HVAC PDF pipeline.
#
# We deploy via Docker (rather than Render's native Python runtime) solely so we
# can install LibreOffice, which renders the three schedule .xlsx files to
# spreadsheet-origin PDFs (see xlsx_to_pdf.py). Everything else — the persistent
# jobs disk, env vars, auth — is unchanged from the previous runtime.
FROM python:3.12.6-slim

# LibreOffice Calc (headless) for xlsx -> pdf, plus Arial-metric fonts so the
# rendered PDFs match the intended look. --no-install-recommends keeps the
# image lean.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-calc \
        libreoffice-core \
        fonts-liberation \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    SOFFICE_BIN=/usr/bin/soffice

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Single worker keeps the memory peak low so a LibreOffice conversion won't OOM
# on Render's Starter plan. Render injects $PORT.
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120
