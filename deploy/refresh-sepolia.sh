#!/usr/bin/env bash
# Keep the Sepolia census cache current: catch blocks_cache_sepolia.sqlite up to
# the chain tip. Unlike the mainnet refresh this does NOT run build_history.py —
# the dashboard is mainnet-only and reads blocks_cache.sqlite, so there is nothing
# to rebuild for Sepolia; this just extends the census cache.
#
# Run it from the systemd timer in deploy/ (blockspace-refresh-sepolia.timer) or
# from cron. Credentials come from the environment (systemd EnvironmentFile, or
# `set -a; . /etc/blockspace/env; set +a` before calling for cron). Sepolia uses
# the shared EL_RPC_TOKEN unless SEPOLIA_EL_RPC_TOKEN is set.
#
# NOTE: run the initial multi-day census by hand first (see the header of
# executionRewards.py / the deploy notes); this timer only fills the recent gap.
set -euo pipefail
cd "$(dirname "$0")/.."                 # repo root (this script lives in deploy/)

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

# Last 6h covers the gap since the previous run (with overlap); only new blocks
# are fetched, the rest are cache hits. --reverse fills newest-first, so an
# interrupted run still leaves a contiguous recent window.
"$PY" executionRewards.py --sepolia --complete --hours 6 --reverse
echo "sepolia refresh complete: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
