#!/usr/bin/env bash
# Production start command for Railway.
#
# Set this as the Custom Start Command in the Railway service settings:
#     bash scripts/start.sh
#
# Migrations run BEFORE uvicorn binds a port, and `set -e` makes a failed
# migration fail the deploy instead of booting an app against a stale schema.
set -euo pipefail

echo "==> Applying database migrations"
python -m scripts.migrate

echo "==> Starting API"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
