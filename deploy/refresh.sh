#!/usr/bin/env bash
# Keep the dashboard current: catch the block cache up to the chain tip, then
# rebuild the DB tables the site reads. The live server does NOT need restarting —
# it reads the tables on each request, so the next page load shows fresh data.
#
# Run it from cron or the systemd timer in deploy/. Credentials (EL_RPC_URL /
# EL_RPC_TOKEN / RPC_VERIFY) must be in the environment (systemd EnvironmentFile,
# or `set -a; . /etc/blockspace/env; set +a` before calling for cron).
set -euo pipefail
cd "$(dirname "$0")/.."                 # repo root (this script lives in deploy/)

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

# Last 6h covers the gap since the previous run (with overlap); only new blocks
# are fetched, the rest are cache hits.
"$PY" executionRewards.py --hours 6 --complete
# --report-gaps logs any missing/short interior days (empty Overview calendar
# cells) so a census gap shows up in the refresh log instead of by eye.
"$PY" build_history.py --report-gaps
echo "refresh complete: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
