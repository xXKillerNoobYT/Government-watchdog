# DEPLOY-2026 (GOV-722) — code-only image for the long-running serving surfaces.
#
# INV-7 / plan D4: this image carries CODE and the migration SQL ONLY. The
# registry database and the raw source store are NEVER baked into an image layer
# — they are volume mounts at runtime (see docker-compose.yml). Nothing private
# lives in the image.
FROM python:3.12-slim

# Reproducible deps. The four serving entrypoints are stdlib-only; requirements
# are installed for the shared civic-domain modules they import (reproducibility
# over leanness).
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Code + schema only. No Database/*.db, no Docs/Source-Data — the .dockerignore
# keeps registry/raw data out of the build context entirely.
COPY scripts/ /app/scripts/
COPY Database/migrations/ /app/Database/migrations/
COPY deploy/mcp_stdio.py /app/deploy/mcp_stdio.py

# Run as an unprivileged user; the mounted data dir is writable to this uid.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data && chown appuser:appuser /data
USER appuser

ENV PYTHONUNBUFFERED=1 \
    GW_DB_PATH=/data/gov_watchdog.db

# Default is a no-op help print; docker-compose overrides `command:` per service.
CMD ["python", "-c", "print('DEPLOY-2026 image — set a service command (see docker-compose.yml)')"]
