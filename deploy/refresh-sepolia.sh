#!/usr/bin/env bash
# Keep the Sepolia census cache current: catch blocks_cache_sepolia.sqlite up to
# the chain tip, then rebuild the Sepolia dashboard tables. The live server reads
# the tables per request, so it does not need restarting; the next page load on
# the Sepolia network toggle shows fresh data.
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
# Rebuild the Sepolia dashboard tables (daily_percentiles / summary / bid_winnable)
# into blocks_cache_sepolia.sqlite so the site's Sepolia toggle stays current.
# --incremental parses only blocks past the watermark (falls back to a full scan
# the first time). Run a plain `build_history.py --sepolia` once after a gap backfill.
"$PY" build_history.py --sepolia --incremental --report-gaps
echo "sepolia refresh complete: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
