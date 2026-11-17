#!/usr/bin/env bash
# Stop the local (non-Docker) cluster: supervisor + nodes, then Nginx.
cd "$(dirname "$0")/.."
[ -f runtime/supervisor.pid ] && kill "$(cat runtime/supervisor.pid)" 2>/dev/null && rm -f runtime/supervisor.pid
nginx -s quit -c "$(pwd)/nginx/nginx.local.conf" -p "$(pwd)/" 2>/dev/null || true
echo "stopped"
