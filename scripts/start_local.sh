#!/usr/bin/env bash
# Start TatkalX without Docker: Nginx on :8080 + supervisor running nodes A/B/C on :8001-8003.
# Prerequisites: PostgreSQL and Redis running, migrations applied, frontend built (see README).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
mkdir -p runtime/logs runtime/nginx-tmp
PY=${PYTHON:-.venv/bin/python}
(cd backend && ../$PY -m alembic upgrade head)
$PY scripts/seed_data.py
nginx -c "$(pwd)/nginx/nginx.local.conf" -p "$(pwd)/" 2>/dev/null || nginx -s reload -c "$(pwd)/nginx/nginx.local.conf" -p "$(pwd)/"
echo "Nginx load balancer on http://localhost:8080"
exec $PY scripts/cluster.py "$@"
